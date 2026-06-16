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
LOG = ROOT / "data" / "bucket_arb_scan.jsonl"          # locked arbs only
COLLECT_LOG = ROOT / "data" / "bucket_arb_collect.jsonl"  # every event's margin

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


def _ladder_calc(markets: list, coef: float) -> dict:
    """Cost + per-leg fees for sweeping a ladder's YES side and NO side, plus
    whether each side is fully executable (every leg quoted)."""
    yes_asks = [_leg_quotes(m)[0] for m in markets]
    no_asks = [_leg_quotes(m)[1] for m in markets]
    return {
        "n": len(markets),
        "yes_cost": sum(yes_asks), "yes_exec": all(0 < a < 100 for a in yes_asks),
        "yes_fee": sum(fee_cents(a, coef) for a in yes_asks),
        "no_cost": sum(no_asks), "no_exec": all(0 < a < 100 for a in no_asks),
        "no_fee": sum(fee_cents(a, coef) for a in no_asks),
    }


def event_margins(event_ticker: str, markets: list, mutually_exclusive: bool,
                  coef: float) -> dict | None:
    """Per-event sweep margins in cents (POSITIVE = locked profit, negative = how
    far from profitable). None if the event has no structural guarantee. This is
    what `collect` logs every run, so a week of scans yields the distribution of
    how close ladders actually get — the thing that tells us if the lane is real,
    not just whether a perfect lock happened to exist this second."""
    if not mutually_exclusive or len(markets) < 2:
        return None
    c = _ladder_calc(markets, coef)
    n = c["n"]
    return {
        "event": event_ticker, "legs": n,
        "no_margin_cents": ((n - 1) * 100 - c["no_cost"] - c["no_fee"]
                            if c["no_exec"] else None),
        "yes_margin_cents": (100 - c["yes_cost"] - c["yes_fee"]
                             if c["yes_exec"] else None),
        "no_cost_cents": c["no_cost"], "no_fee_cents": c["no_fee"],
        "yes_cost_cents": c["yes_cost"], "yes_fee_cents": c["yes_fee"],
    }


def scan_event(event_ticker: str, markets: list, mutually_exclusive: bool,
               coef: float) -> list:
    """Return locked-arb opportunities (margin > 0) for one event's ladder."""
    em = event_margins(event_ticker, markets, mutually_exclusive, coef)
    if em is None:
        return []
    out = []
    if em["no_margin_cents"] is not None and em["no_margin_cents"] > 0:
        out.append({"event": event_ticker, "type": "NO_SWEEP", "legs": em["legs"],
                    "cost_cents": em["no_cost_cents"], "fee_cents": em["no_fee_cents"],
                    "profit_cents": em["no_margin_cents"],
                    "guarantee": "mutual-exclusivity", "assumes_exhaustive": False})
    if em["yes_margin_cents"] is not None and em["yes_margin_cents"] > 0:
        out.append({"event": event_ticker, "type": "YES_SWEEP", "legs": em["legs"],
                    "cost_cents": em["yes_cost_cents"], "fee_cents": em["yes_fee_cents"],
                    "profit_cents": em["yes_margin_cents"],
                    "guarantee": "needs-exhaustive-ladder", "assumes_exhaustive": True})
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


def scan_series(series_ticker: str) -> tuple[list, list]:
    """One fetch per series → (locked arbs, per-event margins). The margins feed
    `collect`; the arbs feed the live display + the locked-arb log."""
    coef = fee_coef(series_ticker)
    hits, margins = [], []
    for ev in fetch_open_events(series_ticker):
        ms = ev.get("markets", []) or []
        me = bool(ev.get("mutually_exclusive"))
        et = ev.get("event_ticker", "")
        em = event_margins(et, ms, me, coef)
        if em is not None:
            margins.append({**em, "series": series_ticker, "coef": coef})
        for opp in scan_event(et, ms, me, coef):
            hits.append({**opp, "series": series_ticker, "coef": coef})
    return hits, margins


def eval_collected() -> int:
    """Summarize the accumulated near-miss distribution: have ladders ever come
    close to a locked arb, how close, and how often executable?"""
    if not COLLECT_LOG.exists():
        print("no collection yet — run `bucket_arb.py --collect` on a schedule first.")
        return 0
    rows = [json.loads(l) for l in COLLECT_LOG.read_text().splitlines() if l.strip()]
    no = [r["no_margin_cents"] for r in rows if r.get("no_margin_cents") is not None]
    yes = [r["yes_margin_cents"] for r in rows if r.get("yes_margin_cents") is not None]
    print(f"=== bucket-arb collection — {len(rows)} event-observations ===")
    print(f"  NO-sweep executable on {len(no)}/{len(rows)} obs, "
          f"YES-sweep on {len(yes)}/{len(rows)}")
    for name, xs in (("NO_SWEEP (robust)", no), ("YES_SWEEP (needs-exhaustive)", yes)):
        if not xs:
            print(f"  {name}: never executable in sample")
            continue
        pos = sum(1 for x in xs if x > 0)
        near = sum(1 for x in xs if -5 <= x <= 0)
        print(f"  {name}: best {max(xs):+d}¢ · median {sorted(xs)[len(xs)//2]:+d}¢ · "
              f"locked(>0) {pos} · within 5¢ of lock {near}")
    print("\nREAD: a healthy efficient market sits a few ¢ negative (fees). If best "
          "stays well below 0 over many days, the lane is dead — walk away. If it "
          "repeatedly pokes >0 AND fills, it's real.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", help="comma-separated series tickers to scan")
    ap.add_argument("--category", help="scan all series in a Kalshi category")
    ap.add_argument("--max-series", type=int, default=40,
                    help="cap series scanned in --category mode (default 40)")
    ap.add_argument("--min-profit", type=int, default=1,
                    help="only report opportunities with >= this profit in cents")
    ap.add_argument("--collect", action="store_true",
                    help="log every event's sweep margin (not just locks) to build "
                         "the distribution — what scheduled runs should do")
    ap.add_argument("--eval", action="store_true", help="summarize collected margins")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    if args.eval:
        raise SystemExit(eval_collected())

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

    all_hits, all_margins, scanned = [], [], 0
    for s in targets:
        hits, margins = scan_series(s)
        all_hits.extend(h for h in hits if h["profit_cents"] >= args.min_profit)
        all_margins.extend(margins)
        scanned += 1
        if hits:
            print(f"  {s}: {len(hits)} opportunity(ies)", file=sys.stderr)
    print(f"scanned {scanned} series, {len(all_margins)} mutually-exclusive events",
          file=sys.stderr)

    ts = datetime.now(timezone.utc).isoformat()
    if all_hits:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            for h in all_hits:
                fh.write(json.dumps({"ts": ts, **h}) + "\n")
    if args.collect and all_margins:
        COLLECT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with COLLECT_LOG.open("a") as fh:
            for m in all_margins:
                fh.write(json.dumps({"ts": ts, **m}) + "\n")
        best = max((m["no_margin_cents"] for m in all_margins
                    if m.get("no_margin_cents") is not None), default=None)
        print(f"  collected {len(all_margins)} margins "
              f"(best NO-sweep this run: {best:+d}¢)" if best is not None
              else f"  collected {len(all_margins)} margins", file=sys.stderr)
    all_hits.sort(key=lambda h: -h["profit_cents"])


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

    # event_margins: the efficient ladder is a NEAR-MISS — margin slightly
    # negative (eaten by fees), NOT None, so `collect` records the distribution.
    em = event_margins("EFF", eff, me, 0.07)
    assert em is not None and em["no_margin_cents"] < 0, em
    assert event_margins("NX", eff, False, 0.07) is None      # no guarantee → skip
    # an unquoted NO leg → no_margin is None (not executable) but YES side can
    # still report; the dict is still produced for collection
    em2 = event_margins("GAP", gap, me, 0.07)
    assert em2 is not None and em2["no_margin_cents"] is None, em2
    print(f"event_margins OK (efficient ladder near-miss {em['no_margin_cents']:+d}¢)")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
