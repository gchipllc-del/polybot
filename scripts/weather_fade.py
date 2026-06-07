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
WEATHER_SERIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX", "KXHIGHDEN",
                  "KXHIGHAUS", "KXHIGHPHIL", "KXHIGHHOU"]


def _open_weather_quotes(series_list) -> list[dict]:
    """Pull OPEN weather markets and turn each into a quote dict. Reuses
    fetch_backtest_data._kalshi_get (signed client → public fallback)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from fetch_backtest_data import _kalshi_get
    now = datetime.now(timezone.utc)
    quotes = []
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
                ya, na = m.get("yes_ask"), m.get("no_ask")
                yb = m.get("yes_bid")
                # market YES estimate = mid of yes bid/ask (cents → 0-1)
                yp = None
                if ya is not None and yb is not None:
                    yp = ((ya + yb) / 2) / 100.0
                quotes.append({
                    "ticker": m.get("ticker"), "event_ticker": m.get("event_ticker", ""),
                    "yes_price": yp,
                    "no_ask": (na / 100.0) if na is not None else None,
                    "close_ts": ct, "hours_to_close": hrs,
                })
            cursor = data.get("cursor")
            if not cursor:
                break
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
    print("  ↑ judge the edge by THIS (real order-book fills), not the backtest.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["scan", "settle", "report"])
    ap.add_argument("--calibration", default=str(CALIB_SRC),
                    help="historical weather jsonl to fit the calibration on")
    ap.add_argument("--bins", type=int, default=20)
    ap.add_argument("--thr", type=float, default=DEFAULT_THR)
    ap.add_argument("--bankroll", type=float, default=DEFAULT_BANKROLL)
    args = ap.parse_args()
    {"scan": cmd_scan, "settle": cmd_settle, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
