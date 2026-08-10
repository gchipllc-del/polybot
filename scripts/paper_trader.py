#!/usr/bin/env python3
"""paper_trader — disciplined FORWARD-ONLY paper trading of the frozen hypotheses.

Why this is not the old composite's mistake: the rules are the ones already frozen in
scripts/shadow_book.py (single source of truth, pre-registered in
docs/CRYPTO15_RESTART.md), and nothing here is fitted to observed data. The point is the
one thing a backtest structurally cannot produce: an OUT-OF-SAMPLE record. Every trade is
stamped at the moment its signal fired, on a market that had not settled yet — so the
ledger this builds is the honest test the shadow book's in-sample replay can never be.

Realism, taken from what the weather sleeve cost us:
  * enter at the ASK actually quoted (taker), never mid
  * pay the real ceil-to-cent Kalshi fee on entry
  * REFUSE the trade when captured order-book depth is smaller than our size -
    an unfillable price is not a price (logged as skip reason "no_depth")
  * one position per (market, rule); a hard cap on concurrent positions
  * settle only from Kalshi's own settlement result

NO REAL ORDERS ARE PLACED. This writes a local JSONL ledger and nothing else. The live
order path stays closed until an out-of-sample rule earns it.

  py scripts/paper_trader.py cycle      # one pass (open + settle)
  py scripts/paper_trader.py run        # loop every 60s
  py scripts/paper_trader.py report
  py scripts/paper_trader.py selftest
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

LEDGER = Path(os.environ.get("PAPER_CRYPTO_LEDGER")
              or (ROOT / "data" / "paper_crypto15.jsonl"))

CONTRACTS = 1              # sizing is not the question under test
MAX_CONCURRENT = 8
CYCLE_S = 60
START_BANKROLL = 500.0     # paper only, for a readable equity number


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rules():
    import shadow_book as sb
    return sb.RULES, sb.BANDS, sb.kalshi_taker_fee


def _load(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def _append(rows: list[dict], path: Path) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def open_positions(rows: list[dict]) -> dict:
    """(ticker, rule) -> open entry row, minus anything already closed."""
    opened = {(r["ticker"], r["rule"]): r for r in rows if r.get("t") == "open"}
    for r in rows:
        if r.get("t") == "close":
            opened.pop((r["ticker"], r["rule"]), None)
    return opened


def evaluate_market(m: dict, mins_left: float, rules, bands, fee_fn,
                    depth_fn=None) -> list[dict]:
    """Which frozen rules fire on this market right now? Returns candidate entries.
    Pure: caller supplies market dict, remaining minutes, and an optional depth lookup."""
    band = None
    for name, (lo, hi) in bands.items():
        if lo <= mins_left < hi:
            band = name
            break
    if band is None:
        return []
    ya, na = m.get("yes_ask"), m.get("no_ask")
    if ya is None or na is None:
        return []
    try:
        ya, na = float(ya), float(na)
    except (TypeError, ValueError):
        return []
    if not (0 < ya < 1 and 0 < na < 1):
        return []
    fav = ("yes", ya) if ya >= na else ("no", na)
    lng = ("no", na) if ya >= na else ("yes", ya)

    out = []
    for rule in rules:
        if rule["band"] != band:
            continue
        side, ask = fav if rule["side"] == "favorite" else lng
        if not (rule["lo"] <= ask < rule["hi"]):
            continue
        depth = depth_fn(m.get("ticker"), side, ask) if depth_fn else None
        entry = {
            "t": "open", "ts": _now_iso(), "ticker": m.get("ticker"),
            "series": m.get("series"), "rule": rule["name"], "band": band,
            "side": side, "price": round(ask, 4), "contracts": CONTRACTS,
            "fee": fee_fn(ask, CONTRACTS), "mins_left": round(mins_left, 2),
            "strike": m.get("strike"), "spot": m.get("spot"), "depth": depth,
        }
        if depth is not None and depth < CONTRACTS:
            entry["skipped"] = "no_depth"
        out.append(entry)
    return out


def settle_positions(rows: list[dict], results: dict) -> list[dict]:
    """Close any open position whose market has a settlement result."""
    closes = []
    for (ticker, rule), pos in open_positions(rows).items():
        res = results.get(ticker)
        if res is None:
            continue
        won = (res == pos["side"])
        c, price, fee = pos["contracts"], pos["price"], pos.get("fee", 0.0)
        pnl = (c * (1.0 - price) if won else -c * price) - fee
        closes.append({"t": "close", "ts": _now_iso(), "ticker": ticker, "rule": rule,
                       "side": pos["side"], "price": price, "result": res,
                       "won": won, "pnl": round(pnl, 4)})
    return closes


# ── live cycle ───────────────────────────────────────────────────────────────

def _depth_fetcher():
    """Order-book depth lookup, when Kalshi auth is configured. Returns
    fn(ticker, side, price) -> contracts available at <= price, or None."""
    try:
        from lib.envload import load_env
        load_env()
        from lib.kalshi_auth import can_sign, signed_get
        if not can_sign():
            return None
        import shadow_book as sb

        def fn(ticker, side, price):
            try:
                ob = signed_get(f"/markets/{ticker}/orderbook", params={"depth": 5})
                book = ob.get("orderbook") or ob
                return sb._depth_at(book, side, price)
            except Exception:
                return None
        return fn
    except Exception:
        return None


def run_cycle(get=None, now=None, path: Path | None = None, depth_fn=None) -> dict:
    import stage0_collector as s0
    rules, bands, fee_fn = _rules()
    p = path or LEDGER
    get = get or s0._get
    now = now or datetime.now(timezone.utc)
    if depth_fn is None:
        depth_fn = _depth_fetcher()

    rows = _load(p)
    live = open_positions(rows)
    seen_closed = {(r["ticker"], r["rule"]) for r in rows if r.get("t") == "close"}
    new_rows, opened, skipped = [], 0, {}
    added: set = set()

    for series in s0.SERIES:
        try:
            markets = s0.fetch_open_markets(series, get=get)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] discovery failed {series}: {e}")
            continue
        spot = s0.fetch_spot(s0.SPOT_SYMBOL[series], get=get)
        for m in markets:
            mins = s0._mins_left(m.get("close_time") or "", now)
            if mins is None or mins <= 0:
                continue
            md = {"ticker": m.get("ticker"), "series": series,
                  "strike": m.get("floor_strike"), "spot": spot,
                  "yes_ask": s0._dollars(m, "yes_ask"), "no_ask": s0._dollars(m, "no_ask")}
            for entry in evaluate_market(md, mins, rules, bands, fee_fn, depth_fn):
                key = (entry["ticker"], entry["rule"])
                # `live` is the ledger at cycle start; `added` catches the same
                # (market, rule) surfacing twice WITHIN this cycle (e.g. the same
                # ticker returned under two series) - without it the ledger would
                # carry duplicate positions for one market.
                if key in live or key in seen_closed or key in added:
                    continue                      # never re-enter the same market/rule
                if entry.get("skipped"):
                    skipped[entry["skipped"]] = skipped.get(entry["skipped"], 0) + 1
                    continue
                if len(live) + opened >= MAX_CONCURRENT:
                    skipped["max_concurrent"] = skipped.get("max_concurrent", 0) + 1
                    continue
                new_rows.append(entry)
                added.add(key)
                opened += 1

    # settle
    results = {}
    for series in s0.SERIES:
        try:
            for m in s0.fetch_settled(series, get=get):
                if m.get("result") in ("yes", "no"):
                    results[m.get("ticker")] = m["result"]
        except Exception as e:  # noqa: BLE001
            print(f"[warn] settle sweep failed {series}: {e}")
    closes = settle_positions(rows + new_rows, results)
    new_rows.extend(closes)
    _append(new_rows, p)
    return {"opened": opened, "closed": len(closes), "skipped": skipped,
            "open_now": len(open_positions(_load(p)))}


def run_loop() -> int:
    print(f"paper trader - every {CYCLE_S}s -> {LEDGER}")
    print("FORWARD-ONLY, no real orders. ctrl-c to stop.")
    while True:
        t0 = time.time()
        try:
            c = run_cycle()
            sk = " ".join(f"{k}={v}" for k, v in c["skipped"].items())
            print(f"[{_now_iso()}] opened={c['opened']} closed={c['closed']} "
                  f"open_now={c['open_now']} {sk}")
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[warn] cycle failed: {e}")
        time.sleep(max(1.0, CYCLE_S - (time.time() - t0)))


# ── report ───────────────────────────────────────────────────────────────────

def _window_of(ticker: str) -> str:
    """Kalshi 15-min tickers are SERIES-YYMMMDDHHMM-STRIKE, so many strikes share ONE
    15-minute window. Outcomes inside a window are driven by the same price move, i.e.
    they are ONE bet, not many. Counting trades as independent samples is the fastest way
    to fool yourself here."""
    parts = str(ticker or "").rsplit("-", 1)
    return parts[0] if len(parts) == 2 else str(ticker)


def build_report(rows: list[dict]) -> dict:
    closed = [r for r in rows if r.get("t") == "close"]
    live = open_positions(rows)
    by_rule: dict[str, dict] = {}
    for r in closed:
        b = by_rule.setdefault(r["rule"], {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["wins"] += 1 if r.get("won") else 0
        b["pnl"] += float(r.get("pnl") or 0)
    total = sum(b["pnl"] for b in by_rule.values())
    n = sum(b["n"] for b in by_rule.values())
    first = next((r.get("ts") for r in rows if r.get("t") == "open" and r.get("ts")), None)
    windows = {_window_of(r.get("ticker")) for r in closed}
    for name, b in by_rule.items():
        b["windows"] = len({_window_of(r.get("ticker")) for r in closed
                            if r.get("rule") == name})
    return {"n_closed": n, "n_open": len(live), "windows": len(windows),
            "net": round(total, 2),
            "equity": round(START_BANKROLL + total, 2),
            "win_rate": (sum(b["wins"] for b in by_rule.values()) / n) if n else None,
            "by_rule": by_rule, "since": first,
            "open_positions": list(live.values())}


def print_report(rep: dict) -> None:
    import math
    print("=" * 72)
    print("PAPER TRADER - forward-only, out-of-sample. NO REAL ORDERS.")
    print("=" * 72)
    if rep["since"]:
        print(f"trading since : {rep['since'][:19]}")
    print(f"closed trades : {rep['n_closed']}     open now: {rep['n_open']}")
    print(f"independent windows: {rep.get('windows', 0)}  <- the REAL sample size")
    wr = "-" if rep["win_rate"] is None else f"{rep['win_rate']*100:.1f}%"
    print(f"win rate      : {wr}")
    print(f"net P&L       : ${rep['net']:+.2f}   (paper equity ${rep['equity']:.2f})")
    print()
    if rep["by_rule"]:
        print("rule                     n  wins  WR     net$    $/trade  windows  95% band*")
        for name, b in sorted(rep["by_rule"].items()):
            w = b["wins"] / b["n"] if b["n"] else 0
            wn = b.get("windows", 0)
            # CI on WINDOWS, not trades - trades inside a window are one correlated bet
            ci = 1.96 * math.sqrt(max(w * (1 - w), 0.01) / wn) if wn else 0
            print(f"{name:<22} {b['n']:>4} {b['wins']:>5} {w*100:5.1f}% {b['pnl']:+7.2f} "
                  f"{b['pnl']/b['n'] if b['n'] else 0:+8.3f} {wn:>8}  +/-{ci*100:5.1f}%")
        print("* CI computed on independent WINDOWS, not trades - many strikes share one")
        print("  15-min window and resolve together, so trade count overstates evidence.")
    else:
        print("no closed trades yet - signals fire only when a frozen rule's price band")
        print("is hit on a live market. Leave the task running.")
    if rep["open_positions"]:
        print()
        print("open positions:")
        for p in rep["open_positions"][:10]:
            print(f"  {p['ticker']:<28} {p['rule']:<22} {p['side']} @ {p['price']:.2f}")
    print()
    print("Out-of-sample: every trade above was stamped BEFORE its market settled.")
    print("This is the record that can earn a live phase; the shadow book's cannot.")
    print("=" * 72)


# ── selftest ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    import shadow_book as sb
    rules, bands, fee_fn = sb.RULES, sb.BANDS, sb.kalshi_taker_fee

    # H1 fires on a <2min favorite at 0.85
    m = {"ticker": "T1", "yes_ask": 0.85, "no_ask": 0.17}
    fired = evaluate_market(m, 1.0, rules, bands, fee_fn)
    names = {e["rule"] for e in fired}
    assert "H1_settlement_lag" in names, names
    e = next(x for x in fired if x["rule"] == "H1_settlement_lag")
    assert e["side"] == "yes" and e["price"] == 0.85 and e["fee"] == 0.01

    # wrong band -> no H1
    assert not any(x["rule"] == "H1_settlement_lag"
                   for x in evaluate_market(m, 30.0, rules, bands, fee_fn))

    # depth gate: thin book marks the entry skipped
    thin = evaluate_market(m, 1.0, rules, bands, fee_fn,
                           depth_fn=lambda t, s, p: 0)
    assert all(x.get("skipped") == "no_depth" for x in thin), thin
    deep = evaluate_market(m, 1.0, rules, bands, fee_fn,
                           depth_fn=lambda t, s, p: 50)
    assert not any(x.get("skipped") for x in deep)

    # settlement math: win pays (1-price)-fee, loss pays -price-fee
    rows = [{"t": "open", "ticker": "T1", "rule": "H1_settlement_lag", "side": "yes",
             "price": 0.85, "contracts": 1, "fee": 0.01}]
    cl = settle_positions(rows, {"T1": "yes"})
    assert len(cl) == 1 and abs(cl[0]["pnl"] - (0.15 - 0.01)) < 1e-9, cl
    cl2 = settle_positions(rows, {"T1": "no"})
    assert abs(cl2[0]["pnl"] - (-0.85 - 0.01)) < 1e-9, cl2

    # a closed position must not reopen, and open_positions must net out
    rows2 = rows + [{"t": "close", "ticker": "T1", "rule": "H1_settlement_lag",
                     "side": "yes", "price": 0.85, "won": True, "pnl": 0.14}]
    assert open_positions(rows2) == {}

    rep = build_report(rows2)
    assert rep["n_closed"] == 1 and abs(rep["net"] - 0.14) < 1e-9, rep
    assert rep["by_rule"]["H1_settlement_lag"]["wins"] == 1

    # full cycle against fake network, then a second cycle must not duplicate
    import tempfile
    from datetime import timedelta
    import stage0_collector as s0
    now = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    def fake_get(url, params=None):
        params = params or {}
        if url.endswith("/events"):
            return {"events": [{"event_ticker": "EV"}]}
        if url.endswith("/markets") and "event_ticker" in params:
            return {"markets": [{"ticker": "KXBTC15M-X", "floor_strike": 64000.0,
                                 "close_time": (now + timedelta(minutes=1)).isoformat(),
                                 "yes_ask_dollars": 0.88, "no_ask_dollars": 0.14}]}
        if url.endswith("/markets"):
            return {"markets": []}
        if "coinbase" in url:
            return {"data": {"amount": "64000"}}
        raise AssertionError(url)

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "paper.jsonl"
        c1 = run_cycle(get=fake_get, now=now, path=p, depth_fn=lambda t, s, pr: 100)
        # both rules in the <2min band fire (favorite 0.88 + longshot 0.14); the
        # fixture serves the SAME ticker under both series, so in-cycle dedup must
        # collapse it to exactly 2 positions, not 4.
        assert c1["opened"] == 2, c1
        assert c1["open_now"] == 2, c1
        c2 = run_cycle(get=fake_get, now=now, path=p, depth_fn=lambda t, s, pr: 100)
        assert c2["opened"] == 0, c2        # no duplicate entries
    print("selftest OK")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "selftest":
        return _selftest()
    if cmd == "cycle":
        c = run_cycle()
        print(f"opened={c['opened']} closed={c['closed']} open_now={c['open_now']} "
              f"skipped={c['skipped']} -> {LEDGER}")
        return 0
    if cmd == "run":
        return run_loop()
    if cmd == "report":
        print_report(build_report(_load(LEDGER)))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
