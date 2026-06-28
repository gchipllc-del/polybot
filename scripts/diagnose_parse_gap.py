#!/usr/bin/env python3
"""Diagnose the forecast-skill parse gap: WHY do ~46% of weather markets not
join to truth? Buckets every market by failure reason and shows the worst
offenders, so the parser fix is written against real strings — not guesses.

  python scripts/diagnose_parse_gap.py

Failure reasons (first one that applies, in join order):
  unknown_series   series prefix not in SERIES_CITY  → a city/series we don't map
  no_date          ticker/event has no -DDMONYY date
  no_strike        no -T<num> suffix and no number in the YES subtitle
  no_truth_city    series maps to a city with NO weather_truth rows at all
  no_truth_day     city has truth, but not for this date
  no_forecast_temp truth row exists but forecast_temp is null
  joined           OK
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from join_weather_trials import (SERIES_CITY, parse_series, parse_event_date,  # noqa: E402
                                 parse_strike2, build_truth_index, _load_jsonl)


def classify(r, truth, truth_cities):
    tk = r.get("market_ticker") or r.get("ticker") or ""
    series = parse_series(tk)
    sc = SERIES_CITY.get(series)
    if not sc:
        return "unknown_series", series
    date = parse_event_date(r.get("event_ticker", ""), tk)
    if date is None:
        return "no_date", series
    kind, strike = parse_strike2(tk, r.get("yes_sub_title", ""))
    if strike is None:
        return "no_strike", series
    city = sc[0]
    if city not in truth_cities:
        return "no_truth_city", series
    info = truth.get((city, date))
    if info is None:
        return "no_truth_day", series
    if info.get("forecast_temp") is None:
        return "no_forecast_temp", series
    return "joined", series


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markets", type=Path,
                    default=ROOT / "data" / "backtest" / "becker_kalshi_weather.jsonl")
    ap.add_argument("--truth", type=Path,
                    default=ROOT / "data" / "backtest" / "weather_truth.jsonl")
    ap.add_argument("--examples", type=int, default=4, help="example tickers per series")
    args = ap.parse_args()

    truth = build_truth_index(_load_jsonl(args.truth))
    truth_cities = {c for (c, _d) in truth}

    # One row per market (dedup so counts match the join), earliest sample.
    seen = {}
    with open(args.markets) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(r.get("result", "")).lower() not in ("yes", "no"):
                continue
            tk = r.get("market_ticker") or r.get("ticker") or ""
            t = str(r.get("sample_at") or "")
            if tk not in seen or t < seen[tk][1]:
                seen[tk] = (r, t)

    reasons = Counter()
    # reason -> series -> [count, example tickers]
    by_reason_series = defaultdict(lambda: defaultdict(lambda: [0, []]))
    unknown_prefix_counts = Counter()
    for tk, (r, _t) in seen.items():
        reason, series = classify(r, truth, truth_cities)
        reasons[reason] += 1
        slot = by_reason_series[reason][series]
        slot[0] += 1
        if len(slot[1]) < args.examples:
            slot[1].append(tk)
        if reason == "unknown_series":
            unknown_prefix_counts[series] += 1

    total = sum(reasons.values())
    print(f"=== parse-gap diagnosis over {total} unique settled weather markets ===\n")
    print(f"{'reason':>18} {'count':>7} {'share':>7}")
    for reason, n in reasons.most_common():
        print(f"{reason:>18} {n:>7} {n/total*100:>6.1f}%")

    print(f"\n--- UNKNOWN SERIES (cities/series not in SERIES_CITY) — top 25 by volume ---")
    print("    (these are the biggest recoverable win: add the prefix→city mapping)")
    for series, n in unknown_prefix_counts.most_common(25):
        ex = by_reason_series["unknown_series"][series][1]
        print(f"  {series:>16}  {n:>6}   e.g. {', '.join(ex[:2])}")

    for reason in ("no_strike", "no_date", "no_truth_city", "no_truth_day",
                   "no_forecast_temp"):
        d = by_reason_series.get(reason)
        if not d:
            continue
        print(f"\n--- {reason} — top series + examples ---")
        for series, (n, ex) in sorted(d.items(), key=lambda kv: -kv[1][0])[:8]:
            print(f"  {series:>16}  {n:>6}   e.g. {', '.join(ex[:args.examples])}")

    known = {c for (c, _dir) in SERIES_CITY.values()}
    missing_truth = sorted(known - truth_cities)
    if missing_truth:
        print(f"\n!! mapped cities with NO truth rows (fetch_backtest_data _cities gap): "
              f"{', '.join(missing_truth)}")

    band_width_check(seen, truth)


def band_width_check(seen, truth):
    """Empirically verify the B<mid> band-width assumption against Kalshi's
    own settlements: for joined band markets, which half-width makes
    |actual − mid| ≤ hw best agree with result==YES? Imperfect agreement is
    expected (Open-Meteo reanalysis vs NWS CLI), but the right width should
    dominate. Always tests the configured BAND_HALF_WIDTH so the verdict
    reflects the live constant, not a hardcoded value."""
    from join_weather_trials import BAND_HALF_WIDTH
    n = 0
    agree = {hw: 0 for hw in sorted({0.5, 1.0, 1.5, BAND_HALF_WIDTH})}
    for tk, (r, _t) in seen.items():
        kind, strike = parse_strike2(tk, r.get("yes_sub_title", ""))
        if kind != "band" or strike is None:
            continue
        sc = SERIES_CITY.get(parse_series(tk))
        date = parse_event_date(r.get("event_ticker", ""), tk)
        if not sc or date is None:
            continue
        info = truth.get((sc[0], date))
        if info is None or info.get("actual_temp") is None:
            continue
        actual = float(info["actual_temp"])
        result_yes = str(r.get("result", "")).lower() == "yes"
        n += 1
        for hw in agree:
            if (abs(actual - strike) <= hw) == result_yes:
                agree[hw] += 1
    if n < 50:
        print(f"\n(band-width check: only {n} band markets joined — skipped)")
        return
    print(f"\n--- band-width check over {n} B-strike markets (vs Kalshi settlement) ---")
    for hw in sorted(agree):
        print(f"  |actual − mid| ≤ {hw}: agrees with result {agree[hw]/n*100:.1f}%")
    best = max(agree, key=agree.get)
    ok = abs(best - BAND_HALF_WIDTH) < 1e-9
    print(f"  -> best width: ±{best}  (configured BAND_HALF_WIDTH = ±{BAND_HALF_WIDTH})"
          + ("  — model OK" if ok else
             f"  !! MISMATCH — set BAND_HALF_WIDTH = {best} in join_weather_trials"))


if __name__ == "__main__":
    main()
