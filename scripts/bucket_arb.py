#!/usr/bin/env python3
"""bucket_arb — read-only scanner for STRUCTURAL (prediction-free) arbitrage on
Kalshi's mutually-exclusive bucket ladders (weather KXHIGH*, S&P/Nasdaq index
ranges, crypto range ladders). It places NO orders — it logs dislocations so we
can watch, exactly like the weather sleeves, whether the edge is real and big
enough to clear fees AND fill before the event closes.

THE MATH (one event = one ladder of buckets, at most one settles YES):

  NO-SWEEP (overpriced ladder)  — buy 1 NO on every bucket.
     Mutual exclusivity alone guarantees ≥ N-1 of them settle YES-for-NO, so the
     payout is at least (N-1)*$1 no matter what. Locked profit if
        (N-1)*100  −  Σ(no_ask)  −  Σ(no_fee)   >  0
     This is the ROBUST one: it needs only `mutually_exclusive`, NOT exhaustiveness.

  YES-SWEEP (underpriced ladder) — buy 1 YES on every bucket.
     Exactly one bucket pays $1 — but ONLY if the ladder is also collectively
     EXHAUSTIVE (the buckets tile every possible outcome). Locked profit if
        100  −  Σ(yes_ask)  −  Σ(yes_fee)   >  0
     Flagged separately and marked ⚠ assumes-exhaustive, because a ladder that's
     mutually exclusive but has an uncovered gap can pay $0 and the "arb" is a loss.

FEES (Kalshi taker schedule): per contract, round UP to the next cent,
  fee = ceil( coef * P * (1-P) )  with P in cents.  coef = 0.07 general,
  0.035 for S&P-500 / Nasdaq-100 ladders. We cross the spread (take) to lock an
  arb, so the taker rate is the right one. No settlement fee.

  python scripts/bucket_arb.py                      # scan weather ladders
  python scripts/bucket_arb.py --category Financials --max-series 60
  python scripts/bucket_arb.py --series KXHIGHNY,KXHIGHLAX
  python scripts/bucket_arb.py --selftest
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
LOG = ROOT / "data" / "bucket_arb_scan.jsonl"

# S&P-500 / Nasdaq-100 ladders get the discounted fee coefficient.
DISCOUNTED_PREFIXES = ("INX", "KXINX", "NASDAQ100", "KXNASDAQ100", "KXNDQ")


def fee_coef(series_ticker: str) -> float:
    t = (series_ticker or "").upper()
    return 0.035 if any(t.startswith(p) for p in DISCOUNTED_PREFIXES) else 0.07


def fee_cents(price_cents: int, coef: float) -> int:
    """Taker fee for one contract at `price_cents`, rounded UP to the next cent.
    0 outside a tradeable price (no fee on a leg you can't actually buy)."""
    p = price_cents
    if p <= 0 or p >= 100:
        return 0
    return math.ceil(coef * p * (100 - p) / 100.0)


def _leg_quotes(m: dict) -> tuple[int, int]:
    """(yes_ask, no_ask) in cents for a market, deriving the NO side from the
    YES book if Kalshi didn't return it explicitly (no_ask = 100 - yes_bid)."""
    yes_ask = int(m.get("yes_ask") or 0)
    no_ask = int(m.get("no_ask") or 0)
    if no_ask <= 0:
        yes_bid = int(m.get("yes_bid") or 0)
        no_ask = (100 - yes_bid) if yes_bid > 0 else 0
    return yes_ask, no_ask


def scan_event(event_ticker: str, markets: list, mutually_exclusive: bool,
               coef: float) -> list:
    """Return locked-arb opportunities for one event's bucket ladder. Empty if
    none, or if the event isn't mutually exclusive (no structural guarantee)."""
    if not mutually_exclusive or len(markets) < 2:
        return []
    n = len(markets)
    yes_asks = [_leg_quotes(m)[0] for m in markets]
    no_asks = [_leg_quotes(m)[1] for m in markets]
    out = []

    # NO-SWEEP — robust (needs only mutual exclusivity). Every leg must be buyable.
    if all(0 < a < 100 for a in no_asks):
        cost = sum(no_asks)
        fees = sum(fee_cents(a, coef) for a in no_asks)
        profit = (n - 1) * 100 - cost - fees
        if profit > 0:
            out.append({"event": event_ticker, "type": "NO_SWEEP", "legs": n,
                        "cost_cents": cost, "fee_cents": fees,
                        "profit_cents": profit, "guarantee": "mutual-exclusivity",
                        "assumes_exhaustive": False})

    # YES-SWEEP — needs exhaustiveness too; flag but mark the assumption.
    if all(0 < a < 100 for a in yes_asks):
        cost = sum(yes_asks)
        fees = sum(fee_cents(a, coef) for a in yes_asks)
        profit = 100 - cost - fees
        if profit > 0:
            out.append({"event": event_ticker, "type": "YES_SWEEP", "legs": n,
                        "cost_cents": cost, "fee_cents": fees,
                        "profit_cents": profit, "guarantee": "needs-exhaustive-ladder",
                        "assumes_exhaustive": True})
    return out


# ── live fetch (needs home IP / Kalshi auth) ────────────────────────────────

def fetch_open_events(series_ticker: str) -> list:
    """Open events for a series WITH nested markets, so we get the whole ladder
    and the mutually_exclusive flag in one call per page."""
    from fetch_backtest_data import _kalshi_get
    out, cursor = [], None
    for _ in range(20):
        params = {"series_ticker": series_ticker, "status": "open",
                  "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            data = _kalshi_get("/events", params)
        except Exception as e:
            print(f"  ! {series_ticker}: {e}", file=sys.stderr)
            break
        out.extend(data.get("events", []) or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def series_in_category(category: str, cap: int) -> list:
    from fetch_backtest_data import _kalshi_get
    out, cursor = [], None
    while len(out) < cap:
        params = {"category": category, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            data = _kalshi_get("/series", params)
        except Exception as e:
            print(f"  ! {category}: {e}", file=sys.stderr)
            break
        out.extend(s.get("ticker", "") for s in (data.get("series", []) or []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return [s for s in out if s][:cap]


def scan_series(series_ticker: str) -> list:
    coef = fee_coef(series_ticker)
    hits = []
    for ev in fetch_open_events(series_ticker):
        ms = ev.get("markets", []) or []
        me = bool(ev.get("mutually_exclusive"))
        for opp in scan_event(ev.get("event_ticker", ""), ms, me, coef):
            opp["series"] = series_ticker
            opp["coef"] = coef
            hits.append(opp)
    return hits


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", help="comma-separated series tickers to scan")
    ap.add_argument("--category", help="scan all series in a Kalshi category")
    ap.add_argument("--max-series", type=int, default=40,
                    help="cap series scanned in --category mode (default 40)")
    ap.add_argument("--min-profit", type=int, default=1,
                    help="only report opportunities with >= this profit in cents")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())

    if args.series:
        targets = [s.strip() for s in args.series.split(",") if s.strip()]
    elif args.category:
        targets = series_in_category(args.category, args.max_series)
    else:
        # default: the proven, clean-ladder family
        targets = series_in_category("Climate and Weather", args.max_series)
    if not targets:
        print("no series to scan — run on home IP with Kalshi auth.")
        return

    all_hits, scanned = [], 0
    for s in targets:
        hits = [h for h in scan_series(s) if h["profit_cents"] >= args.min_profit]
        all_hits.extend(hits)
        scanned += 1
        if hits:
            print(f"  {s}: {len(hits)} opportunity(ies)", file=sys.stderr)
    print(f"scanned {scanned} series", file=sys.stderr)

    if all_hits:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        with LOG.open("a") as fh:
            for h in all_hits:
                fh.write(json.dumps({"ts": ts, **h}) + "\n")
    all_hits.sort(key=lambda h: -h["profit_cents"])

    print(f"\n=== bucket-arb scan — {len(all_hits)} locked opportunit"
          f"{'y' if len(all_hits)==1 else 'ies'} ===")
    if not all_hits:
        print("none. An efficient ladder sums to ~100¢; dislocations are rare and "
              "fleeting — that's expected. Keep scanning on a schedule.")
        return
    print(f"{'event':<26} {'type':<9} {'legs':>4} {'cost¢':>6} {'fee¢':>5} "
          f"{'profit¢':>7}  guarantee")
    for h in all_hits:
        flag = " ⚠" if h["assumes_exhaustive"] else ""
        print(f"{h['event'][:26]:<26} {h['type']:<9} {h['legs']:>4} "
              f"{h['cost_cents']:>6} {h['fee_cents']:>5} {h['profit_cents']:>7}"
              f"  {h['guarantee']}{flag}")
    print("\n⚠ YES_SWEEP profits assume the ladder is EXHAUSTIVE (buckets tile every "
          "outcome). Verify there are no gaps before trusting those.\nNO_SWEEP is "
          "guaranteed by mutual exclusivity alone.\nNOTE: top-of-book prices only — "
          "confirm DEPTH (can you fill every leg?) before believing any of these.")


def selftest() -> int:
    # fee shape: 2¢ at mid general, 1¢ on S&P, 0 at the boundaries
    assert fee_cents(50, 0.07) == 2, fee_cents(50, 0.07)
    assert fee_cents(50, 0.035) == 1, fee_cents(50, 0.035)
    assert fee_cents(0, 0.07) == 0 and fee_cents(100, 0.07) == 0
    assert fee_cents(10, 0.07) == 1                  # ceil(0.07*10*90/100)=ceil(0.63)
    assert fee_coef("KXINXU") == 0.035 and fee_coef("KXHIGHNY") == 0.07
    print("fee model OK")

    me = True
    # efficient ladder: NO asks sum to (N-1)*100 exactly → no arb
    eff = [{"no_ask": 67, "yes_ask": 34}, {"no_ask": 67, "yes_ask": 34},
           {"no_ask": 66, "yes_ask": 35}]   # no_asks sum 200 == (3-1)*100
    assert scan_event("EFF", eff, me, 0.07) == [], scan_event("EFF", eff, me, 0.07)
    print("efficient ladder → no false arb OK")

    # overpriced ladder: NO asks cheap → NO_SWEEP locks profit
    over = [{"no_ask": 60, "yes_ask": 45}, {"no_ask": 60, "yes_ask": 45},
            {"no_ask": 60, "yes_ask": 45}]   # Σno=180 < 200; fees 3*ceil(.07*60*40/100=1.68→2)=6
    res = scan_event("OVER", over, me, 0.07)
    no = [r for r in res if r["type"] == "NO_SWEEP"]
    assert no and no[0]["profit_cents"] == (3-1)*100 - 180 - 6 == 14, res
    assert no[0]["assumes_exhaustive"] is False
    print(f"NO_SWEEP detection OK (profit {no[0]['profit_cents']}¢)")

    # underpriced ladder: YES asks sum < 100 → YES_SWEEP, marked assumes-exhaustive
    under = [{"yes_ask": 30, "yes_bid": 1}, {"yes_ask": 30, "yes_bid": 1},
             {"yes_ask": 30, "yes_bid": 1}]   # Σyes=90<100; fees small
    res = scan_event("UNDER", under, me, 0.07)
    yes = [r for r in res if r["type"] == "YES_SWEEP"]
    assert yes and yes[0]["assumes_exhaustive"] is True, res
    assert yes[0]["profit_cents"] == 100 - 90 - 3 * fee_cents(30, 0.07), yes
    print(f"YES_SWEEP detection OK (profit {yes[0]['profit_cents']}¢, flagged exhaustive)")

    # NOT mutually exclusive → never a structural arb, even if cheap
    assert scan_event("NX", over, False, 0.07) == []
    # unquoted leg (no ask) → that sweep is not executable
    gap = [{"no_ask": 0, "yes_ask": 45}, {"no_ask": 60, "yes_ask": 45}]
    assert all(r["type"] != "NO_SWEEP" for r in scan_event("GAP", gap, me, 0.07))
    print("guards OK (non-exclusive skipped, unquoted leg not swept)")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
