#!/usr/bin/env python3
"""Join Kalshi weather markets ↔ Open-Meteo truth into forecast-skill trials.

Takes the two products of fetch_backtest_data.py:
  * data/backtest/becker_kalshi_weather.jsonl  (price + real settled result)
  * data/backtest/weather_truth.jsonl          (per city/day forecast + actual)

…parses each weather market's city / date / strike from its ticker, looks up
the forecast (what the forecast SAID — no look-ahead) and the actual extreme for
that city+day, and emits trial rows carrying BOTH:

  market_p_yes   — what the market priced
  forecast_p_yes — what our NWS-style normal-CDF model would have said
  actual_yes     — what actually happened vs the strike
  result         — Kalshi's real settlement (ground truth)
  edge           — forecast_p_yes − market_p_yes

So you can ask two different questions over the SAME deep history:
  * favorite-longshot price calibration  → run becker_edge on market_p_yes
  * forecast SKILL                        → does forecast_p_yes predict result?

Usage:
  python scripts/join_weather_trials.py \\
      --markets data/backtest/becker_kalshi_weather.jsonl \\
      --truth   data/backtest/weather_truth.jsonl \\
      --out     data/backtest/weather_trials.jsonl --sigma 3.0
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}

# series prefix → (city key matching weather_truth, direction). Mirrors the
# bot's CITIES / DAILY_CITIES registries.
SERIES_CITY = {
    "KXTEMPNYCH": ("nyc", "max"), "KXTEMPCHIH": ("chicago", "max"),
    "KXTEMPDCH": ("dc", "max"), "KXTEMPBOSH": ("boston", "max"),
    "KXTEMPLAXH": ("lax", "max"), "KXTEMPMIAH": ("miami", "max"),
    "KXHIGHTDAL": ("dal_high", "max"), "KXHIGHTPHX": ("phx_high", "max"),
    "KXHIGHTATL": ("atl_high", "max"), "KXHIGHTSEA": ("sea_high", "max"),
    "KXLOWTCHI": ("chi_low", "min"), "KXLOWTDEN": ("den_low", "min"),
    "KXLOWTDC": ("dc_low", "min"), "KXLOWTLAX": ("lax_low", "min"),
    # Historical Becker high-temp series (older KXHIGH* form, 8 cities). Keys
    # match fetch_backtest_data._cities() so weather_truth lines up.
    "KXHIGHAUS": ("aus_high", "max"), "KXHIGHCHI": ("chi_high", "max"),
    "KXHIGHNY": ("ny_high", "max"), "KXHIGHDEN": ("den_high", "max"),
    "KXHIGHMIA": ("mia_high", "max"), "KXHIGHPHIL": ("phil_high", "max"),
    "KXHIGHLAX": ("lax_high", "max"), "KXHIGHHOU": ("hou_high", "max"),
    # Oldest pre-KX prefixes (2021–2024, ~13k markets — the 46% parse gap):
    # same four cities, no "KX". HIGHNY0 is an early NY series variant.
    "HIGHNY": ("ny_high", "max"), "HIGHCHI": ("chi_high", "max"),
    "HIGHAUS": ("aus_high", "max"), "HIGHMIA": ("mia_high", "max"),
    "HIGHNY0": ("ny_high", "max"),
}


def parse_series(ticker: str) -> str:
    """Leading series prefix of a market ticker (before the first '-')."""
    return (ticker or "").split("-", 1)[0]


def parse_event_date(event_ticker: str, ticker: str = "") -> str | None:
    """Kalshi encodes the date as -DDMONYY (e.g. -26JUN05 → 2026-06-05)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or ticker or "")
    if not m:
        return None
    yy, mon, dd = int(m.group(1)), m.group(2), int(m.group(3))
    mo = MONTHS.get(mon)
    if not mo:
        return None
    return f"20{yy:02d}-{mo:02d}-{dd:02d}"


def parse_strike(ticker: str, yes_sub_title: str = "") -> float | None:
    """Strike in °F. Threshold markets end in -T<num> (the bot's own regex).
    Falls back to the first number in the YES subtitle (e.g. '≥95°')."""
    m = re.search(r"T(-?\d+(?:\.\d+)?)$", ticker or "")
    if m:
        return float(m.group(1))
    m = re.search(r"(-?\d+(?:\.\d+)?)", yes_sub_title or "")
    return float(m.group(1)) if m else None


def parse_strike2(ticker: str, yes_sub_title: str = "") -> tuple[str | None, float | None]:
    """(kind, strike): 'above' for -T<n> thresholds, 'band' for -B<mid> bucket
    markets (old HIGH* series: B48.5 settles YES on an integer temp of 48 or
    49, i.e. mid ± 1°F continuous). Subtitle-number fallback is 'above'."""
    m = re.search(r"T(-?\d+(?:\.\d+)?)$", ticker or "")
    if m:
        return "above", float(m.group(1))
    m = re.search(r"B(-?\d+(?:\.\d+)?)$", ticker or "")
    if m:
        return "band", float(m.group(1))
    m = re.search(r"(-?\d+(?:\.\d+)?)", yes_sub_title or "")
    return ("above", float(m.group(1))) if m else (None, None)


def p_above(forecast_f: float, strike_f: float, sigma_f: float) -> float:
    """P(actual ≥ strike | forecast) under a normal error model, clipped to
    [0.02, 0.98]. Same math as weather_signal._p_above_strike."""
    if sigma_f <= 0:
        return 1.0 if forecast_f >= strike_f else 0.0
    z = (forecast_f - strike_f) / sigma_f
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return max(0.02, min(0.98, p))


# B<mid> is a 1°F-wide bucket, e.g. B90.5 = "90-91°" → [90.0, 91.0] around the
# .5 midpoint, so the HALF-width is 0.5. Verified empirically against Kalshi's
# own settlements (diagnose_parse_gap band-width check): ±0.5 agreed 71.4% of
# 19,340 B-markets vs 66.2% at ±1.0 vs 61.3% at ±1.5 — monotone, narrower wins.
BAND_HALF_WIDTH = 0.5


def forecast_p_yes(kind: str, strike_f: float, forecast_f: float,
                   sigma_f: float) -> float:
    """P(YES | forecast) for either market kind, clipped to [0.02, 0.98]."""
    if kind == "band":
        lo, hi = strike_f - BAND_HALF_WIDTH, strike_f + BAND_HALF_WIDTH
        p = p_above(forecast_f, lo, sigma_f) - p_above(forecast_f, hi, sigma_f)
        return max(0.02, min(0.98, p))
    return p_above(forecast_f, strike_f, sigma_f)


def actual_is_yes(kind: str, strike_f: float, actual_f: float) -> bool:
    if kind == "band":
        return abs(actual_f - strike_f) <= BAND_HALF_WIDTH
    return actual_f >= strike_f


def build_truth_index(truth_rows: list[dict]) -> dict:
    """(city, date) → {forecast_temp, actual_temp} picking high/low by the
    city's direction (max→high, min→low)."""
    idx = {}
    for r in truth_rows:
        city, date = r.get("city"), r.get("date")
        if not city or not date:
            continue
        direction = (r.get("direction") or "max")
        if direction == "min":
            fc, ac = r.get("forecast_low_f"), r.get("actual_low_f")
        else:
            fc, ac = r.get("forecast_high_f"), r.get("actual_high_f")
        idx[(city, date)] = {"forecast_temp": fc, "actual_temp": ac,
                             "direction": direction}
    return idx


def join_trials(market_rows: list[dict], truth_rows: list[dict],
                sigma: float = 3.0) -> tuple[list[dict], dict]:
    """Join markets→truth into forecast-skill trial rows. Returns (rows, stats)."""
    truth = build_truth_index(truth_rows)
    out, stats = [], {"in": len(market_rows), "no_parse": 0,
                      "no_truth": 0, "no_temp": 0, "joined": 0}
    for r in market_rows:
        tk = r.get("market_ticker", "")
        series = parse_series(tk)
        sc = SERIES_CITY.get(series)
        date = parse_event_date(r.get("event_ticker", ""), tk)
        kind, strike = parse_strike2(tk, r.get("yes_sub_title", ""))
        if not sc or date is None or strike is None:
            stats["no_parse"] += 1
            continue
        city, direction = sc
        t = truth.get((city, date))
        if t is None:
            stats["no_truth"] += 1
            continue
        fc, ac = t["forecast_temp"], t["actual_temp"]
        if fc is None:
            stats["no_temp"] += 1
            continue
        fc_p = round(forecast_p_yes(kind, strike, float(fc), sigma), 4)
        actual_yes = (1 if (ac is not None
                            and actual_is_yes(kind, strike, float(ac))) else 0)
        mkt = r.get("market_p_yes")
        stats["joined"] += 1
        out.append({
            "market_ticker": tk, "city": city, "date": date,
            "direction": direction, "strike_f": strike, "strike_kind": kind,
            "market_p_yes": mkt,
            "forecast_temp_f": fc, "actual_temp_f": ac,
            "forecast_p_yes": fc_p,
            "actual_yes": actual_yes,
            "result": r.get("result"),
            "edge": (round(fc_p - mkt, 4) if isinstance(mkt, (int, float)) else None),
            "sample_at": r.get("sample_at"),
        })
    return out, stats


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--markets", type=Path,
                    default=ROOT / "data" / "backtest" / "becker_kalshi_weather.jsonl")
    ap.add_argument("--truth", type=Path,
                    default=ROOT / "data" / "backtest" / "weather_truth.jsonl")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "data" / "backtest" / "weather_trials.jsonl")
    ap.add_argument("--sigma", type=float, default=3.0,
                    help="forecast-error std-dev in °F (daily ~3-4, hourly ~2-3)")
    args = ap.parse_args()

    markets = _load_jsonl(args.markets)
    truth = _load_jsonl(args.truth)
    if not markets:
        print(f"! no market rows at {args.markets} — run fetch_backtest_data.py becker first")
        return
    if not truth:
        print(f"! no truth rows at {args.truth} — run fetch_backtest_data.py openmeteo first")
        return
    rows, stats = join_trials(markets, truth, sigma=args.sigma)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"joined {stats['joined']}/{stats['in']} market rows -> {args.out}")
    print(f"  dropped: {stats['no_parse']} unparseable, "
          f"{stats['no_truth']} no truth city/day, {stats['no_temp']} no forecast temp")
    # Quick forecast-skill read: do the rows the model called >50% actually win?
    conf = [r for r in rows if isinstance(r["forecast_p_yes"], (int, float))]
    if conf:
        called_yes = [r for r in conf if r["forecast_p_yes"] >= 0.5]
        hit = sum(1 for r in called_yes if r["actual_yes"] == 1)
        print(f"  forecast skill: of {len(called_yes)} 'YES-likely' calls, "
              f"{hit} hit ({(hit/len(called_yes)*100 if called_yes else 0):.1f}%)")


if __name__ == "__main__":
    main()
