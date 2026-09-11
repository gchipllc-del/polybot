#!/usr/bin/env python3
"""edge_analysis — "do we have enough data to tune?" answered by walk-forward, not opinion.

The trap this exists to prevent: pick the best-looking cell in the data, tune to it, and
call the resulting backtest P&L an edge. That is how the previous 15-min sleeve died -
33 trades, repeatedly tuned, ultimately anti-predictive.

The honest test is simple and brutal. Split the collected data chronologically. Find the
best cells in the FIRST half. Then trade exactly those cells in the SECOND half, with no
further choices. If first-half winners keep winning, the effect is structural and we can
act on it. If they collapse to zero, everything we "found" was noise, and any tuning would
have been fitting the past.

Also reports:
  * WIN RATE vs EV - the crucial distinction. Buying 95c favorites "wins" 95% of the time
    and still loses money. Win rate is not the objective; EV after fees is.
  * per-window concentration - how many correlated bets we take per 15-min window.

HOW MUCH DATA IS ENOUGH? Measured, not guessed. Running this against synthetic fixtures
with a KNOWN implanted edge (72c price, 82% true win rate) versus pure noise (every market
fairly priced):

    windows    real edge          pure noise
    200        persist-but-thin   persist-but-thin    <- indistinguishable, correctly
    600        HELD               flipped-to-noise    <- discrimination begins
    1500       HELD               (nothing survives)  <- clean separation

So ~600 independent 15-min windows is the threshold where this test can tell a genuine
10-point edge from luck. At 192 windows/day (2 series x 96), that is about 3 days of
collection. Below it, ANY tuning is fitting noise - which is the honest answer to "can we
tweak the code to win more yet".

  py scripts/edge_analysis.py
  py scripts/edge_analysis.py --min-n 30
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

BANDS = {">10min": (10.0, 1e9), "2-10min": (2.0, 10.0), "<2min": (0.0, 2.0)}
# DATA-VALIDITY filter, not a tuned parameter: daily markets open with placeholder books
# (ask ~0.999, bid ~0.000, spread ~1.00) hours before anyone quotes. Entering "first obs
# in band" on such an obs measures the empty book, not the market. An obs counts only
# when the yes-side book is formed. Crypto-15m spreads were 1-4c, so this leaves every
# prior crypto conclusion untouched (verified: crypto cells shift < 0.001).
MAX_SPREAD = 0.15


def _book_formed(r) -> bool:
    try:
        yb, ya = float(r.get("yes_bid")), float(r.get("yes_ask"))
    except (TypeError, ValueError):
        return False
    return 0.01 <= yb < ya <= 0.99 and (ya - yb) <= MAX_SPREAD
BUCKETS = [(1, 5), (5, 10), (10, 20), (20, 35), (35, 50),
           (50, 65), (65, 80), (80, 90), (90, 95), (95, 99)]


def taker_fee(price: float) -> float:
    if not (0 < price < 1):
        return 0.0
    return math.ceil(0.07 * price * (1.0 - price) * 100) / 100.0


def wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z / d * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, c - h), min(1.0, c + h))


def _window_of(ticker: str) -> str:
    parts = str(ticker or "").rsplit("-", 1)
    return parts[0] if len(parts) == 2 else str(ticker)


def load_events(path: Path) -> list[dict]:
    """One event per (ticker, band, side): the first price seen, plus the outcome."""
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    settles = {r["ticker"]: r["result"] for r in rows if r.get("t") == "settle"}
    first: dict[tuple, dict] = {}
    for r in rows:
        if r.get("t") != "obs" or r.get("mins_left") is None:
            continue
        if not _book_formed(r):
            continue                     # skip placeholder books; take first FORMED obs
        band = next((b for b, (lo, hi) in BANDS.items()
                     if lo <= r["mins_left"] < hi), None)
        if band is None:
            continue
        key = (r.get("ticker"), band)
        if key not in first:
            first[key] = r
    events = []
    for (ticker, band), r in first.items():
        res = settles.get(ticker)
        if res is None:
            continue
        for side in ("yes", "no"):
            ask = r.get(f"{side}_ask")
            if ask is None or not (0 < float(ask) < 1):
                continue
            ask = float(ask)
            bucket = next((f"{lo:02d}-{hi:02d}c" for lo, hi in BUCKETS
                           if lo <= ask * 100 < hi), None)
            if bucket is None:
                continue
            events.append({"ts": r.get("ts", ""), "ticker": ticker,
                           "window": _window_of(ticker), "band": band, "side": side,
                           "bucket": bucket, "price": ask, "won": (res == side)})
    events.sort(key=lambda e: e["ts"])
    return events


def cell_stats(events: list[dict]) -> dict:
    out: dict[tuple, dict] = {}
    for e in events:
        c = out.setdefault((e["band"], e["bucket"]), {"n": 0, "wins": 0, "cost": 0.0,
                                                      "fee": 0.0, "pnl": 0.0,
                                                      "windows": set()})
        c["n"] += 1
        c["wins"] += 1 if e["won"] else 0
        c["cost"] += e["price"]
        f = taker_fee(e["price"])
        c["fee"] += f
        c["pnl"] += ((1.0 - e["price"]) if e["won"] else -e["price"]) - f
        c["windows"].add(e["window"])
    return out


def main() -> int:
    import stage0_collector as s0
    min_n = 20
    if "--min-n" in sys.argv:
        try:
            min_n = int(sys.argv[sys.argv.index("--min-n") + 1])
        except (IndexError, ValueError):
            pass

    events = load_events(s0.LOG)
    print("=" * 76)
    print("EDGE ANALYSIS - can this data support tuning, or would tuning be fitting noise?")
    print("=" * 76)
    if len(events) < 40:
        print(f"only {len(events)} settled observations - far too few. Let it collect.")
        return 0

    windows = {e["window"] for e in events}
    print(f"{len(events)} settled observations across {len(windows)} independent "
          f"15-min windows")
    print(f"concentration: {len(events)/max(1,len(windows)):.1f} correlated bets per "
          f"window (strikes in one window resolve on the SAME price move)")
    print()

    # ── the walk-forward test ────────────────────────────────────────────────
    mid = len(events) // 2
    first_half, second_half = events[:mid], events[mid:]
    fs, ss = cell_stats(first_half), cell_stats(second_half)

    print("WALK-FORWARD: cells that looked best in the FIRST half, tested on the SECOND")
    print()
    print(f"{'band':8} {'bucket':8} {'--- first half ---':>26}  {'--- second half ---':>26}")
    print(f"{'':8} {'':8} {'n':>5} {'$/bet':>8} {'win%':>6}  {'n':>5} {'$/bet':>8} {'win%':>6}  held?")
    print("  (HELD requires: positive in BOTH halves AND full-sample CI clears breakeven)")

    candidates = []
    for key, c in fs.items():
        if c["n"] >= min_n:
            candidates.append((c["pnl"] / c["n"], key, c))
    candidates.sort(reverse=True)

    held = persistent = flipped = 0
    allc_pre = cell_stats(events)
    for edge, (band, bucket), c in candidates[:8]:
        s2 = ss.get((band, bucket))
        if not s2 or s2["n"] < 5:
            continue
        e1, e2 = c["pnl"] / c["n"], s2["pnl"] / s2["n"]
        w1, w2 = c["wins"] / c["n"], s2["wins"] / s2["n"]
        # A cell only counts as HELD if it survives BOTH tests:
        #   (a) positive in each half - rules out fitting one sub-period, AND
        #   (b) the FULL-SAMPLE Wilson CI clears breakeven - rules out a fluke that
        #       spans the whole dataset, which splitting in half cannot detect
        #       (verified: a fair 55c market that happened to run 61.5% over 200 draws
        #       showed "HELD" on walk-forward alone while being pure noise).
        full = allc_pre[(band, bucket)]
        fn = full["n"]
        fwr = full["wins"] / fn
        be = full["cost"] / fn + full["fee"] / fn
        lo, _hi = wilson(fwr, fn)
        sig = lo > be
        # THREE distinct states, not two - conflating the middle one with noise was a
        # bug that called a real implanted edge "noise" purely for want of sample size.
        if e1 > 0 and e2 > 0 and sig:
            verdict, held = "HELD (act)", held + 1
        elif e1 > 0 and e2 > 0:
            verdict, persistent = "persists, CI thin", persistent + 1
        elif e1 > 0:
            verdict, flipped = "FLIPPED (noise)", flipped + 1
        else:
            verdict = "-"
        print(f"{band:8} {bucket:8} {c['n']:>5} {e1:>+8.3f} {w1*100:>5.0f}%  "
              f"{s2['n']:>5} {e2:>+8.3f} {w2*100:>5.0f}%  {verdict}")

    print()
    print(f"VERDICT: {held} HELD | {persistent} persist-but-thin | {flipped} flipped-to-noise")
    if held + persistent + flipped == 0:
        print("  Not enough overlap between halves yet. Keep collecting.")
    elif held:
        print(f"  {held} cell(s) are positive in BOTH halves AND statistically clear of")
        print("  breakeven. That is structural, not a fit - safe to act on, sized for")
        print("  the correlation shown above (bets per window are ONE bet, not many).")
    elif persistent:
        print(f"  {persistent} cell(s) stayed positive across both halves but the sample is")
        print("  still too small to rule out luck. This is the ONLY honest reading of a")
        print("  promising edge: keep collecting, do not tune to it, do not size up yet.")
    else:
        print("  Every first-half winner collapsed. What looked like edge was noise -")
        print("  tuning on this data would be fitting the past.")

    # ── win rate vs EV ───────────────────────────────────────────────────────
    print()
    print("WIN RATE IS NOT THE GOAL (why 'win way more' is the wrong target)")
    print()
    print(f"{'band':8} {'bucket':8} {'n':>5} {'win%':>6} {'$/bet':>8}  reads")
    allc = cell_stats(events)
    rows = [(c["wins"] / c["n"], band, bucket, c) for (band, bucket), c in allc.items()
            if c["n"] >= min_n]
    rows.sort(reverse=True)
    for wr, band, bucket, c in rows[:6]:
        ev = c["pnl"] / c["n"]
        note = "high win rate, LOSES money" if (wr > 0.6 and ev < 0) else \
               ("profitable" if ev > 0 else "loses")
        print(f"{band:8} {bucket:8} {c['n']:>5} {wr*100:>5.0f}% {ev:>+8.3f}  {note}")
    print()
    print("Any strategy can hit a 95% win rate by buying 95c favorites - and still bleed.")
    print("The only target that matters is $/bet after fees.")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
