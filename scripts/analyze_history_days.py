#!/usr/bin/env python3
"""Reconstruct the live fade rule across the full Becker weather history, grouped
BY DAY, to answer the one question 2 live days can't: is this a real PRICING edge
or just a directional weather bet (does net P&L go red on hot / high-YES-rate days)?

Applies the exact live rule — fade overpriced YES, buy NO at taker (1-price),
in the 0.10-0.90 band, net of the Kalshi fee — to an out-of-sample slice (fit
the calibration on the earlier 60% by time, trade the later 40%), then groups
the traded markets by event-date and correlates each day's net P&L with how
"hot" the day ran (its YES-rate).

  python scripts/analyze_history_days.py data/backtest/becker_kalshi_weather.jsonl
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from becker_edge import fit_calibration, fair_prob          # noqa: E402
from weather_fade import kalshi_fee, FILL_FLOOR, FILL_CEIL    # noqa: E402

import json  # noqa: E402


def _event_date(ticker: str) -> str:
    m = re.search(r"-(\d{2}[A-Z]{3}\d{2})", ticker or "")
    return m.group(1) if m else "?"


def _date_sort_key(d: str):
    """26JUN08 -> sortable (year, month, day)."""
    m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", d)
    if not m:
        return (99, 99, 99)
    mon = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6, "JUL": 7,
           "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}.get(m.group(2), 99)
    return (int(m.group(1)), mon, int(m.group(3)))


def load_markets(path: Path):
    """One row per market (earliest priced sample): (price, yes_bool, ticker, date, t)."""
    by_tk: dict = {}
    with open(path) as f:
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
            if not isinstance(p, (int, float)) or res not in ("yes", "no"):
                continue
            tk = r.get("market_ticker") or r.get("ticker") or ""
            t = str(r.get("sample_at") or "")
            cur = by_tk.get(tk)
            if cur is None or t < cur[4]:
                by_tk[tk] = (float(p), res == "yes", tk, _event_date(tk), t)
    return list(by_tk.values())


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def reconstruct(markets, centers, rates, thr):
    """Apply the fade rule; return per-day {net, n, yes} for the traded markets."""
    days: dict = {}
    for p, yes, tk, date, t in markets:
        fair = fair_prob(p, centers, rates)
        edge = fair - p
        if edge > -thr:                       # YES not overpriced enough
            continue
        if not (FILL_FLOOR <= p <= FILL_CEIL):
            continue
        fill = 1.0 - p                        # taker NO fill (historical proxy)
        won = (not yes)                       # NO wins when YES did not happen
        fee = kalshi_fee(1.0, fill)           # 1 contract
        pnl = ((1.0 - fill) if won else -fill) - fee
        b = days.setdefault(date, {"net": 0.0, "n": 0, "yes": 0})
        b["net"] += pnl
        b["n"] += 1
        b["yes"] += 1 if yes else 0
    return days


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("data", type=Path,
                    nargs="?", default=ROOT / "data" / "backtest" / "becker_kalshi_weather.jsonl")
    ap.add_argument("--split", type=float, default=0.6, help="fit fraction (time-split)")
    ap.add_argument("--bins", type=int, default=20)
    args = ap.parse_args()

    if not args.data.exists():
        raise SystemExit(f"not found: {args.data} — run fetch_backtest_data.py becker first")
    markets = load_markets(args.data)
    if len(markets) < 100:
        raise SystemExit(f"only {len(markets)} markets — need the full Becker weather file")

    # OOS time-split: fit calibration on earlier, trade later (no leakage)
    markets.sort(key=lambda m: (m[4] == "", m[4]))
    k = int(len(markets) * args.split)
    fit, trade = markets[:k], markets[k:]
    centers, rates, _ = fit_calibration([(p, yes) for p, yes, *_ in fit], args.bins)
    n_days_total = len({m[3] for m in trade})
    print(f"=== historical per-day fade reconstruction (OOS) — {len(trade)} markets, "
          f"{n_days_total} distinct days ===")
    print(f"   (calibration fit on earlier {args.split:.0%} by time; traded on the rest, "
          f"net of Kalshi fee)\n")

    print(f"{'thr':>5} {'days':>5} {'green%':>7} {'trades':>7} {'net$':>9} "
          f"{'EV/ct':>7} {'corr(net,YESrate)':>18} {'hot-day net':>11} {'cool-day net':>12}")
    headline = None
    for thr in (0.03, 0.05, 0.08, 0.10, 0.12):
        days = reconstruct(trade, centers, rates, thr)
        if not days:
            continue
        nets = [d["net"] for d in days.values()]
        rates = [d["yes"] / d["n"] for d in days.values()]
        ntr = sum(d["n"] for d in days.values())
        net = sum(nets)
        green = sum(1 for x in nets if x > 0) / len(nets) * 100
        corr = pearson(rates, nets)
        med = sorted(rates)[len(rates) // 2]
        hot = [d["net"] for d, r in zip(days.values(), rates) if r >= med]
        cool = [d["net"] for d, r in zip(days.values(), rates) if r < med]
        hot_avg = sum(hot) / len(hot) if hot else 0
        cool_avg = sum(cool) / len(cool) if cool else 0
        corr_s = f"{corr:+.2f}" if corr is not None else "  n/a"
        print(f"{thr:>5.2f} {len(days):>5} {green:>6.0f}% {ntr:>7} {net:>+9.2f} "
              f"{net/ntr:>+7.3f} {corr_s:>18} {hot_avg:>+11.2f} {cool_avg:>+12.2f}")
        if thr == 0.05:
            headline = (corr, hot_avg, cool_avg, green, net, ntr)

    print("\nHOW TO READ THIS — the verdict on pricing-edge vs directional-bet:")
    print("  • corr(net, YES-rate) STRONGLY NEGATIVE  → fade loses on hot days → it's a")
    print("    DIRECTIONAL WEATHER BET (dangerous: correlated drawdowns, June-8 risk is real).")
    print("  • corr near 0 AND net positive on BOTH hot & cool days → a real PRICING EDGE")
    print("    that doesn't care which way the weather broke. THAT is what you want to see.")
    print("  • green% = share of distinct days that were net-positive after fees. >55-60%")
    print("    across hundreds of days = the edge is real and not longshot-dependent.")
    if headline and headline[0] is not None:
        corr, hot_avg, cool_avg, green, net, ntr = headline
        verdict = ("DIRECTIONAL BET (hot days lose)" if corr < -0.4
                   else "REAL PRICING EDGE (robust to weather)" if (corr > -0.2 and net > 0 and green > 55)
                   else "MIXED / inconclusive — read the table")
        print(f"\n  @ thr 0.05: corr {corr:+.2f}, green {green:.0f}%, net ${net:+.2f} → {verdict}")


if __name__ == "__main__":
    main()
