#!/usr/bin/env python3
"""Fade-overpriced-YES weather strategy — the edge the becker_edge backtest validated.

The backtest verdict (13 months, 8 cities, 2,708 independent events, OOS):
Kalshi weather YES favorites are systematically OVERPRICED at day-ahead entry.
The tradeable play is: at OPEN, when the empirical calibration says a market's
YES price is too high, BUY NO at the real order-book price and hold to settle.
The edge is gone by near-close, so this targets day-ahead markets only.

This module is the decision engine + a paper loop that books trades at the LIVE
order book (no_ask) — the one thing the backtest couldn't test (real fills).
Stdlib-only; reuses becker_edge's calibration + fetch_backtest_data's Kalshi
client. Records to a paper ledger; NEVER places a real order.

  python scripts/weather_fade.py scan      # paper-book day-ahead fades, live book
  python scripts/weather_fade.py settle     # resolve & P&L open paper trades
  python scripts/weather_fade.py report      # scorecard (judge real fills here)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "data" / "weather_fade_paper.jsonl"
CALIB_SRC = ROOT / "data" / "backtest" / "becker_kalshi_weather.jsonl"

# Day-ahead window: only enter markets this far from close (the edge is at open).
MIN_HOURS_TO_CLOSE = 6.0
MAX_HOURS_TO_CLOSE = 40.0
DEFAULT_THR = 0.05            # require |fair − price| ≥ 5pp (best EV in backtest)
DEFAULT_BANKROLL = 143.0
DEFAULT_RISK_PCT = 0.02       # ~$2.86/trade at $143
DEFAULT_MAX_TRADE_USD = 3.0
FILL_FLOOR, FILL_CEIL = 0.10, 0.90   # liquid band the edge was validated on


# ── Calibration (reused from becker_edge so live == backtest math) ──────────
def load_calibration(src: Path, bins: int = 20):
    """Fit the empirical price→P(YES) calibration on the historical weather
    markets — the exact curve becker_edge used. Returns (centers, rates)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from becker_edge import fit_calibration
    pairs = []
    with open(src) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            p = r.get("market_p_yes")
            res = str(r.get("result", "")).lower()
            if isinstance(p, (int, float)) and res in ("yes", "no"):
                pairs.append((float(p), res == "yes"))
    if not pairs:
        raise SystemExit(f"no calibration pairs in {src} — run fetch_backtest_data.py becker first")
    centers, rates, _ = fit_calibration(pairs, bins)
    return centers, rates


def _fair(price, centers, rates):
    from becker_edge import fair_prob
    return fair_prob(price, centers, rates)


# ── Decision engine (pure — fully testable) ─────────────────────────────────
def fade_decision(quote: dict, centers, rates, *,
                  thr: float = DEFAULT_THR, bankroll: float = DEFAULT_BANKROLL,
                  risk_pct: float = DEFAULT_RISK_PCT,
                  max_trade_usd: float = DEFAULT_MAX_TRADE_USD,
                  fill_floor: float = FILL_FLOOR, fill_ceil: float = FILL_CEIL) -> dict | None:
    """Given one market quote, return a paper NO order if YES is overpriced
    enough to fade — else None. `quote` needs: ticker, yes_price (0-1 market
    YES), no_ask (0-1 real NO fill). Optional: event_ticker, close_ts, hours_to_close.

    Buys NO (fades YES) when the calibration's fair P(YES) is ≥ thr BELOW the
    market YES price, on liquid prices only, at the real no_ask fill.
    """
    yp = quote.get("yes_price")
    no_ask = quote.get("no_ask")
    if not isinstance(yp, (int, float)) or not isinstance(no_ask, (int, float)):
        return None
    if not (fill_floor <= yp <= fill_ceil):      # liquid band only
        return None
    if not (0.01 < no_ask < 0.99):               # sane NO fill
        return None
    fair = _fair(float(yp), centers, rates)
    edge = fair - yp                             # negative ⇒ YES overpriced
    if edge > -thr:                              # not overpriced enough
        return None
    notional = min(max_trade_usd, bankroll * risk_pct)
    size = round(notional / no_ask, 2)
    if size <= 0:
        return None
    # EV/contract of buying NO at no_ask under our fair model:
    ev_ct = round((1.0 - fair) - no_ask, 4)
    return {
        "ticker": quote.get("ticker"),
        "event_ticker": quote.get("event_ticker", ""),
        "side": "NO",
        "yes_price": round(float(yp), 4),
        "fair_yes": round(fair, 4),
        "edge": round(edge, 4),               # how overpriced YES was (signed)
        "fill_price": round(float(no_ask), 4),
        "our_size": size,
        "notional": round(size * float(no_ask), 2),
        "ev_per_contract": ev_ct,
        "hours_to_close": quote.get("hours_to_close"),
    }


def decide_batch(quotes, centers, rates, **kw) -> list[dict]:
    out = []
    for q in quotes:
        d = fade_decision(q, centers, rates, **kw)
        if d:
            out.append(d)
    return out


# ── Live scan (reuses the proven Kalshi client; needs home-IP / auth) ───────
# Daily HIGH-temp series. The original 8 (KXHIGH*, no T) are what the edge was
# calibrated on; the KXHIGHT* set are additional cities from Kalshi's live
# catalog (same daily-high market type → favorite-longshot bias should transfer;
# the per-city report breakdown confirms each). Empty/closed series are harmless
# (the scan just gets 0 markets). LOWS are deliberately excluded — never in the
# calibration, so they're collected/validated separately, not traded.
WEATHER_SERIES = [
    # validated 8 (calibration set)
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHDEN",
    "KXHIGHAUS", "KXHIGHPHIL", "KXHIGHHOU",
    # additional live high-temp cities (same market type, per-city tracked)
    "KXHIGHTATL", "KXHIGHTBOS", "KXHIGHTDAL", "KXHIGHTDC", "KXHIGHTLV",
    "KXHIGHTMIN", "KXHIGHTNOLA", "KXHIGHTOKC", "KXHIGHTPHX", "KXHIGHTSATX",
    "KXHIGHTSEA", "KXHIGHTSFO",
]
VALIDATED_SERIES = {"KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX",
                    "KXHIGHDEN", "KXHIGHAUS", "KXHIGHPHIL", "KXHIGHHOU"}

# Hourly weather series (current temp). No Becker history exists for these, so we
# COLLECT their price→outcome data forward to backtest the hourly edge later.
HOURLY_SERIES = ["KXTEMPNYCH", "KXTEMPCHIH", "KXTEMPDCH", "KXTEMPBOSH",
                 "KXTEMPLAXH", "KXTEMPMIAH"]
HOURLY_MIN_H, HOURLY_MAX_H = 0.5, 12.0   # hourly markets close within hours
COLLECT_LEDGER = ROOT / "data" / "hourly_weather_collect.jsonl"
SCAN_STATUS = ROOT / "data" / "weather_fade_scan_status.json"   # last-run health


def _best_bid(levels) -> float | None:
    """Highest bid price among orderbook levels. Levels may be [price, size] or
    {"price":...}, and price may arrive as a string (orderbook_fp dollars come
    as strings) — so cast to float and skip anything unparseable."""
    best = None
    for lvl in levels or []:
        try:
            raw = lvl[0] if isinstance(lvl, (list, tuple)) else lvl.get("price")
            p = float(raw) if raw is not None else None
        except (TypeError, ValueError, AttributeError, IndexError):
            continue
        if p is not None and (best is None or p > best):
            best = p
    return best


def _ob_sides(resp: dict):
    """Locate the (yes_levels, no_levels, scale) in a Kalshi orderbook response,
    handling both the modern `orderbook_fp.{yes_dollars,no_dollars}` (prices in
    DOLLARS 0-1) and the legacy `orderbook.{yes,no}` (CENTS) shapes — plus a
    bare sub-dict (for tests). scale converts level prices to 0-1."""
    r = resp or {}
    for cont in (r.get("orderbook_fp") or {}, r.get("orderbook") or {}, r):
        if not isinstance(cont, dict):
            continue
        if cont.get("yes_dollars") is not None or cont.get("no_dollars") is not None:
            return cont.get("yes_dollars"), cont.get("no_dollars"), 1.0   # dollars
    for cont in (r.get("orderbook") or {}, r.get("orderbook_fp") or {}, r):
        if not isinstance(cont, dict):
            continue
        if cont.get("yes") is not None or cont.get("no") is not None:
            return cont.get("yes"), cont.get("no"), 0.01                  # cents
    return None, None, 1.0


def orderbook_bests(resp: dict) -> tuple:
    """(best_yes_bid, best_no_bid) in 0-1, or (None, None)."""
    yl, nl, sc = _ob_sides(resp)
    yb, nb = _best_bid(yl), _best_bid(nl)
    return (round(yb * sc, 4) if yb is not None else None,
            round(nb * sc, 4) if nb is not None else None)


def parse_orderbook(resp: dict) -> tuple:
    """Derive (yes_price_mid, no_ask) in 0-1 from a Kalshi orderbook response.

    Resting BIDS only on each side; asks are the complement:
      yes_ask = 1 − best_no_bid   (buy YES = sell NO to the top NO bid)
      no_ask  = 1 − best_yes_bid  (buy NO  = sell YES to the top YES bid)
    no_ask is None when there's no YES bid to lift (can't take NO there).
    Accepts the full /orderbook response or a bare orderbook dict; handles both
    the `orderbook_fp` (dollars) and legacy (cents) shapes.
    """
    yb, nb = orderbook_bests(resp)
    ya = (1.0 - nb) if nb is not None else None
    no_ask = (1.0 - yb) if yb is not None else None
    if yb is not None and ya is not None:
        yes_price = (yb + ya) / 2.0
    else:
        yes_price = yb if yb is not None else ya
    return (round(yes_price, 4) if yes_price is not None else None,
            round(no_ask, 4) if no_ask is not None else None)


def _open_weather_quotes(series_list, min_h: float = MIN_HOURS_TO_CLOSE,
                         max_h: float = MAX_HOURS_TO_CLOSE) -> list[dict]:
    """OPEN markets in the [min_h, max_h]-to-close window with REAL order-book
    quotes. The /markets snapshot returns null bid/ask for weather, so we list
    markets there but read prices from the /markets/{ticker}/orderbook endpoint."""
    import time
    sys.path.insert(0, str(ROOT / "scripts"))
    from fetch_backtest_data import _kalshi_get
    now = datetime.now(timezone.utc)

    # 1) list open markets, keep only the day-ahead window (limits orderbook calls)
    candidates = []
    for series in series_list:
        cursor = None
        for _ in range(20):
            params = {"limit": 1000, "status": "open", "series_ticker": series}
            if cursor:
                params["cursor"] = cursor
            data = _kalshi_get("/markets", params)
            for m in data.get("markets", []) or []:
                ct = m.get("close_time")
                hrs = None
                if ct:
                    try:
                        hrs = (datetime.fromisoformat(str(ct).replace("Z", "+00:00")) - now).total_seconds() / 3600.0
                    except Exception:
                        hrs = None
                if hrs is not None and min_h <= hrs <= max_h:
                    candidates.append({"ticker": m.get("ticker"),
                                       "event_ticker": m.get("event_ticker", ""),
                                       "close_ts": ct, "hours_to_close": hrs})
            cursor = data.get("cursor")
            if not cursor:
                break

    # 2) read the real book for each (rate-limited)
    quotes = []
    for c in candidates:
        try:
            resp = _kalshi_get(f"/markets/{c['ticker']}/orderbook", {})
        except Exception:
            resp = {}
        yp, na = parse_orderbook(resp)
        yb, nb = orderbook_bests(resp)
        quotes.append({**c, "yes_price": yp, "no_ask": na,
                       "yes_bid_c": yb, "no_bid_c": nb})   # now 0-1, not cents
        time.sleep(0.1)
    return quotes


def _load_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    rows = []
    with open(LEDGER) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def _append_ledger(rows: list[dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def cmd_scan(args) -> None:
    centers, rates = load_calibration(Path(args.calibration), args.bins)
    try:
        quotes = _open_weather_quotes(WEATHER_SERIES)
    except Exception as e:
        print(f"! live scan failed ({e}) — run on home IP with Kalshi auth", file=sys.stderr)
        return
    # day-ahead window only (the edge is at open, not near close)
    day_ahead = [q for q in quotes if q.get("hours_to_close") is not None
                 and MIN_HOURS_TO_CLOSE <= q["hours_to_close"] <= MAX_HOURS_TO_CLOSE]

    # Diagnostic: show the live edge for EVERY day-ahead market (not just
    # qualifiers), so you can see the distribution and whether the calibration
    # maps sanely onto live mid prices.
    if getattr(args, "show", False):
        # Dump EVERY day-ahead market with raw book + derived prices, so we can
        # tell "genuinely unfillable/extreme" from "window/parse artifact".
        n_empty = n_onesided = n_inband = n_oob = 0
        rows = []
        for q in day_ahead:
            yb, nb = q.get("yes_bid_c"), q.get("no_bid_c")
            yp, na = q.get("yes_price"), q.get("no_ask")
            if yb is None and nb is None:
                n_empty += 1
            elif na is None:
                n_onesided += 1   # NO bids but no YES bid → can't buy NO
            edge = (_fair(float(yp), centers, rates) - yp) if isinstance(yp, (int, float)) else None
            in_band = (isinstance(yp, (int, float)) and isinstance(na, (int, float))
                       and FILL_FLOOR <= yp <= FILL_CEIL)
            if in_band:
                n_inband += 1
            elif isinstance(yp, (int, float)):
                n_oob += 1
            rows.append((edge if edge is not None else 0.0, yb, nb, yp, na, edge,
                         in_band and edge is not None and edge <= -args.thr, q.get("ticker")))
        rows.sort(key=lambda r: r[0])
        print(f"--- {len(day_ahead)} day-ahead markets: {n_empty} empty book, "
              f"{n_onesided} one-sided (no YES bid → can't buy NO), "
              f"{n_inband} in-band {FILL_FLOOR}-{FILL_CEIL}, {n_oob} out-of-band ---")
        print(f"  {'ticker':26} {'ybid':>4} {'nbid':>4} {'yes':>5} {'no_ask':>6} {'edge':>7}")
        for _, yb, nb, yp, na, edge, fade, tk in rows[:30]:
            f = lambda v, p=2: (f"{v:.{p}f}" if isinstance(v, (int, float)) else "  -")
            print(f"  {str(tk)[:26]:26} {str(yb if yb is not None else '-'):>4} "
                  f"{str(nb if nb is not None else '-'):>4} {f(yp):>5} {f(na):>6} "
                  f"{(f'{edge:+.3f}' if edge is not None else '   -'):>7}{'  FADE' if fade else ''}")

    open_tickers = {r["ticker"] for r in _load_ledger() if r.get("status") == "open"}
    decisions = decide_batch(day_ahead, centers, rates, thr=args.thr,
                             bankroll=args.bankroll)
    now_iso = datetime.now(timezone.utc).isoformat()
    new = []
    for d in decisions:
        if d["ticker"] in open_tickers:
            continue
        d.update({"status": "open", "opened_at": now_iso, "result": "",
                  "resolved_at": "", "paper_pnl": 0.0, "is_live": False})
        new.append(d)
    _append_ledger(new)
    print(f"scan: {len(quotes)} open markets, {len(day_ahead)} day-ahead, "
          f"{len(decisions)} qualify, {len(new)} new paper fades booked "
          f"(bankroll ${args.bankroll:.0f}, thr {args.thr}).")
    for d in new[:20]:
        print(f"  NO {d['ticker']:24} yes={d['yes_price']:.2f} fair={d['fair_yes']:.2f} "
              f"edge={d['edge']:+.3f} fill={d['fill_price']:.2f} x{d['our_size']} "
              f"EV/ct {d['ev_per_contract']:+.3f}")

    # Write run-health so the dashboard can show the scan is ALIVE and WHY it
    # did/didn't book — even when 0 fades qualify (the common case).
    n_empty = n_onesided = n_inband = 0
    for q in day_ahead:
        yb, nb, yp, na = (q.get("yes_bid_c"), q.get("no_bid_c"),
                          q.get("yes_price"), q.get("no_ask"))
        if yb is None and nb is None:
            n_empty += 1
        elif na is None:
            n_onesided += 1
        elif isinstance(yp, (int, float)) and FILL_FLOOR <= yp <= FILL_CEIL:
            n_inband += 1
    try:
        SCAN_STATUS.parent.mkdir(parents=True, exist_ok=True)
        SCAN_STATUS.write_text(json.dumps({
            "ts": now_iso, "thr": args.thr, "bankroll": args.bankroll,
            "open_markets": len(quotes), "day_ahead": len(day_ahead),
            "empty_book": n_empty, "one_sided": n_onesided, "in_band": n_inband,
            "qualified": len(decisions), "booked": len(new),
        }))
    except Exception:
        pass


def cmd_settle(args) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from fetch_backtest_data import _kalshi_get
    rows = _load_ledger()
    if not rows:
        print("no paper trades yet.")
        return
    changed = 0
    pnl_now = 0.0
    for r in rows:
        if r.get("status") != "open":
            continue
        try:
            m = _kalshi_get(f"/markets/{r['ticker']}", {}).get("market", {})
        except Exception:
            continue
        res = str(m.get("result", "") or "").lower()
        if res not in ("yes", "no"):
            continue
        # We bought NO: win if market resolved NO.
        won = (res == "no")
        fill = float(r["fill_price"]); size = float(r["our_size"])
        r["paper_pnl"] = round((size * (1.0 - fill)) if won else (-size * fill), 2)
        r["status"] = "won" if won else "lost"
        r["result"] = res
        r["resolved_at"] = datetime.now(timezone.utc).isoformat()
        pnl_now += r["paper_pnl"]
        changed += 1
    if changed:
        with open(LEDGER, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    print(f"settled {changed} trades, P&L this run ${pnl_now:+.2f}")


def _city_of(ticker: str) -> str:
    """City code from a weather ticker's series prefix (KXHIGHTATL→ATL,
    KXHIGHNY→NY). Longer prefixes checked first so the 'T' isn't kept."""
    s = (ticker or "").split("-")[0]
    for p in ("KXHIGHT", "KXHIGH", "KXLOWT", "KXLOW", "KXTEMP"):
        if s.startswith(p):
            return s[len(p):] or s
    return s


def cmd_report(args) -> None:
    rows = _load_ledger()
    closed = [r for r in rows if r.get("status") in ("won", "lost")]
    openn = [r for r in rows if r.get("status") == "open"]
    won = sum(1 for r in closed if r["status"] == "won")
    net = sum(float(r.get("paper_pnl", 0) or 0) for r in closed)
    inv = sum(float(r.get("notional", 0) or 0) for r in closed)
    print("=== weather-fade paper scorecard (real-fill test) ===")
    print(f"  open {len(openn)} · closed {len(closed)} · "
          f"WR {(won/len(closed)*100 if closed else 0):.1f}% ({won}W/{len(closed)-won}L)")
    print(f"  net paper P&L ${net:+.2f} on ${inv:.2f} invested "
          f"(ROI {(net/inv*100 if inv else 0):+.1f}%)")
    # Per-city breakdown — essential now that the scan spans many cities, so a
    # new city carrying or dragging the edge is visible (not hidden in the agg).
    if closed:
        by_city: dict = {}
        for r in closed:
            c = _city_of(r.get("ticker") or r.get("market_ticker") or "")
            b = by_city.setdefault(c, {"w": 0, "l": 0, "net": 0.0})
            b["w" if r["status"] == "won" else "l"] += 1
            b["net"] += float(r.get("paper_pnl", 0) or 0)
        print("  per-city (closed):")
        for c in sorted(by_city, key=lambda k: by_city[k]["net"]):
            b = by_city[c]
            n = b["w"] + b["l"]
            tag = "" if c in {_city_of(s + "-x") for s in VALIDATED_SERIES} else " *new"
            print(f"    {c:6} {b['w']}W/{b['l']}L  net ${b['net']:+.2f}  "
                  f"({b['w']/n*100:.0f}% WR){tag}")
        print("    (* = city outside the validated 8; watch these earn their place)")
    print("  ↑ judge the edge by THIS (real order-book fills), not the backtest.")


def cmd_probe(args) -> None:
    """Snapshot weather-book liquidity now and append to a probe log, so running
    it on a short interval reveals WHEN these books are quoted/takeable. Cheap;
    no calibration needed."""
    try:
        quotes = _open_weather_quotes(WEATHER_SERIES)
    except Exception as e:
        print(f"! probe failed ({e}) — run on home IP", file=sys.stderr)
        return
    now = datetime.now(timezone.utc).isoformat()
    log = ROOT / "data" / "weather_fade_book_probe.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    depth = takeable = 0
    with open(log, "a") as f:
        for q in quotes:
            yb, nb, yp, na = (q.get("yes_bid_c"), q.get("no_bid_c"),
                              q.get("yes_price"), q.get("no_ask"))
            has_depth = (yb is not None) or (nb is not None)
            tk_ok = (isinstance(na, (int, float)) and isinstance(yp, (int, float))
                     and FILL_FLOOR <= yp <= FILL_CEIL)
            depth += has_depth
            takeable += tk_ok
            f.write(json.dumps({"ts": now, "ticker": q.get("ticker"),
                                "hours_to_close": q.get("hours_to_close"),
                                "yes_bid": yb, "no_bid": nb, "yes_price": yp,
                                "no_ask": na, "has_depth": has_depth,
                                "takeable": tk_ok}) + "\n")
    print(f"probe {now}: {len(quotes)} day-ahead markets, "
          f"{depth} with depth, {takeable} takeable (in-band w/ no_ask) "
          f"-> {log}")


def collect_new_rows(quotes, seen_tickers, now_iso) -> list[dict]:
    """PURE: from current hourly quotes, return becker_edge-format rows for
    markets we haven't recorded yet (one EARLIEST entry price per market).
    Outcome is filled later by collect-settle."""
    out = []
    for q in quotes:
        tk = q.get("ticker")
        yp = q.get("yes_price")
        if not tk or tk in seen_tickers or not isinstance(yp, (int, float)):
            continue
        if not (0.0 < yp < 1.0):
            continue
        out.append({"market_ticker": tk, "market_p_yes": round(float(yp), 4),
                    "result": "", "sample_at": now_iso,
                    "event_ticker": q.get("event_ticker", ""),
                    "close_ts": q.get("close_ts"), "status": "open"})
        seen_tickers.add(tk)
    return out


def cmd_collect(args) -> None:
    """Forward-collect HOURLY weather price→outcome data (no Becker history
    exists for KXTEMP*). Records each market's earliest entry price once; run it
    hourly. After a few weeks, becker_edge on this file tests the hourly edge."""
    try:
        quotes = _open_weather_quotes(HOURLY_SERIES, HOURLY_MIN_H, HOURLY_MAX_H)
    except Exception as e:
        print(f"! collect failed ({e}) — run on home IP", file=sys.stderr)
        return
    existing = []
    if COLLECT_LEDGER.exists():
        with open(COLLECT_LEDGER) as f:
            existing = [json.loads(l) for l in f if l.strip()]
    seen = {r.get("market_ticker") for r in existing}
    new = collect_new_rows(quotes, seen, datetime.now(timezone.utc).isoformat())
    COLLECT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with open(COLLECT_LEDGER, "a") as f:
        for r in new:
            f.write(json.dumps(r) + "\n")
    n_open = sum(1 for r in existing if r.get("status") == "open") + len(new)
    n_closed = sum(1 for r in existing if r.get("status") in ("won", "lost", "settled"))
    print(f"collect: {len(quotes)} open hourly markets, {len(new)} new recorded "
          f"-> {COLLECT_LEDGER} (total: {n_open} awaiting outcome, {n_closed} settled)")


def cmd_collect_settle(args) -> None:
    """Fill outcomes for collected hourly markets that have settled, so the file
    becomes a becker_edge-ready (price, result) dataset."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from fetch_backtest_data import _kalshi_get
    if not COLLECT_LEDGER.exists():
        print("no collected hourly markets yet.")
        return
    with open(COLLECT_LEDGER) as f:
        rows = [json.loads(l) for l in f if l.strip()]
    changed = 0
    for r in rows:
        if r.get("status") != "open":
            continue
        try:
            m = _kalshi_get(f"/markets/{r['market_ticker']}", {}).get("market", {})
        except Exception:
            continue
        res = str(m.get("result", "") or "").lower()
        if res in ("yes", "no"):
            r["result"] = res
            r["status"] = res  # 'yes'/'no' — becker_edge reads result directly
            r["resolved_at"] = datetime.now(timezone.utc).isoformat()
            changed += 1
    if changed:
        with open(COLLECT_LEDGER, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    settled = sum(1 for r in rows if r.get("result") in ("yes", "no"))
    print(f"collect-settle: resolved {changed} this run; {settled} total settled "
          f"of {len(rows)}. Backtest when settled is large enough:")
    print(f"  python scripts/becker_edge.py {COLLECT_LEDGER} --price-col market_p_yes "
          f"--result-col result --time-col sample_at --market-col market_ticker --sweep")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["scan", "settle", "report", "probe",
                                    "collect", "collect-settle"])
    ap.add_argument("--calibration", default=str(CALIB_SRC),
                    help="historical weather jsonl to fit the calibration on")
    ap.add_argument("--bins", type=int, default=20)
    ap.add_argument("--thr", type=float, default=DEFAULT_THR)
    ap.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL)
    ap.add_argument("--show", action="store_true",
                    help="scan: print the live edge for every day-ahead market "
                         "(diagnose firing rate + calibration-vs-live mapping)")
    args = ap.parse_args()
    {"scan": cmd_scan, "settle": cmd_settle, "report": cmd_report,
     "probe": cmd_probe, "collect": cmd_collect,
     "collect-settle": cmd_collect_settle}[args.cmd](args)


if __name__ == "__main__":
    main()
