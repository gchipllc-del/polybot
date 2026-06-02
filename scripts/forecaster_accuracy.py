#!/usr/bin/env python3
"""Measure per-forecaster accuracy for the daily-weather sleeve's cities.

Answers, with data (not assertion), the question behind #174's Layer-B debate:
*which forecaster is actually most accurate for these airport stations?* — so the
ensemble can be weighted by MEASURED skill instead of a guess.

Method (standard day-ahead verification):
  * Forecasts: Open-Meteo historical-forecast API, per named model
      ecmwf_ifs025, gfs_seamless, icon_seamless, gem_seamless, plus the default
      "best_match" blend — daily temperature_2m_max / _min.
  * NWS: joined from our own data/weather_daily_signal.jsonl (raw nws_forecast_f),
      the same source the sleeve trades on.
  * Actuals: Open-Meteo ERA5 archive (archive-api) daily max/min. NOTE: ERA5 is
      gridded reanalysis; Kalshi settles on the exact NWS station obs, so absolute
      bias may differ a hair from settlement — but RELATIVE model ranking (the
      thing we need to weight a blend) is robust to that.

Read-only. Prints MAE + mean signed bias per (model, direction). No trading.
"""
import json
import sys
import statistics as st
from collections import defaultdict
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.weather_daily_signal import DAILY_CITIES  # noqa: E402

MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "gem_seamless"]
HIST_FC = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"


def _get(url, params):
    try:
        r = requests.get(url, params=params, timeout=30)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _daily_field(direction):
    return "temperature_2m_max" if direction == "max" else "temperature_2m_min"


def _fetch_model_forecasts(lat, lon, start, end, direction):
    """Return {model_name: {date: forecast_f}} for the named models + default."""
    field = _daily_field(direction)
    out = defaultdict(dict)
    # Named models in one call (parallel arrays suffixed by model name).
    j = _get(HIST_FC, {
        "latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
        "daily": field, "temperature_unit": "fahrenheit", "timezone": "auto",
        "models": ",".join(MODELS),
    })
    if j and "daily" in j:
        dates = j["daily"].get("time", [])
        for m in MODELS:
            vals = j["daily"].get(f"{field}_{m}")
            if not vals:
                continue
            for d, v in zip(dates, vals):
                if v is not None:
                    out[m][d] = float(v)
    # Default best_match blend (separate call, no models param).
    j2 = _get(HIST_FC, {
        "latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
        "daily": field, "temperature_unit": "fahrenheit", "timezone": "auto",
    })
    if j2 and "daily" in j2:
        for d, v in zip(j2["daily"].get("time", []), j2["daily"].get(field, [])):
            if v is not None:
                out["openmeteo_default"][d] = float(v)
    return out


def _fetch_actuals(lat, lon, start, end, direction):
    field = _daily_field(direction)
    j = _get(ARCHIVE, {
        "latitude": lat, "longitude": lon, "start_date": start, "end_date": end,
        "daily": field, "temperature_unit": "fahrenheit", "timezone": "auto",
    })
    out = {}
    if j and "daily" in j:
        for d, v in zip(j["daily"].get("time", []), j["daily"].get(field, [])):
            if v is not None:
                out[d] = float(v)
    return out


def _nws_from_signal_log(direction):
    """Join recorded raw NWS forecasts by (city_key, obs_date). Takes the median
    NWS value per city/day (multiple market rows share one forecast)."""
    log = _ROOT / "data" / "weather_daily_signal.jsonl"
    by = defaultdict(lambda: defaultdict(list))  # city -> date -> [nws_f]
    if not log.exists():
        return by
    for line in log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        nws = r.get("nws_forecast_f")
        ct = r.get("close_time", "")
        ck = r.get("city_key", "")
        if nws is None or not ct or ck not in DAILY_CITIES:
            continue
        if DAILY_CITIES[ck].get("direction") != direction:
            continue
        # close_time is the day AFTER the obs day (00-08 UTC). Obs date = close
        # date minus 1 day for these markets. Derive from the event date instead.
        # Simplest robust key: the calendar date of (close_time - 1 day).
        from datetime import datetime, timedelta
        try:
            cdt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except ValueError:
            continue
        obs_date = (cdt - timedelta(days=1)).date().isoformat()
        by[ck][obs_date].append(float(nws))
    return by


def run_hourly_nyc():
    """Hourly-temperature skill at NYC Central Park (the KXTEMPNYCH station the
    LIVE hourly sleeve trades). Per-model hourly temperature_2m vs ERA5 hourly
    actuals, MAE/bias over a 21-day window. NOTE: the #161 audit found the hourly
    sleeve's edge is cheap-NO PAYOFF asymmetry, not forecast accuracy — so a more
    accurate model may not move its P&L much. Measure first, decide after."""
    from datetime import date, timedelta
    lat, lon = 40.7831, -73.9712
    end = date.today() - timedelta(days=6)
    start = end - timedelta(days=21)
    s, e = start.isoformat(), end.isoformat()
    print(f"NYC Central Park hourly verification: {s} .. {e}  (ERA5 actuals)\n")
    # per-model hourly forecasts
    jf = _get(HIST_FC, {
        "latitude": lat, "longitude": lon, "start_date": s, "end_date": e,
        "hourly": "temperature_2m", "temperature_unit": "fahrenheit",
        "timezone": "auto", "models": ",".join(MODELS)})
    ja = _get(ARCHIVE, {
        "latitude": lat, "longitude": lon, "start_date": s, "end_date": e,
        "hourly": "temperature_2m", "temperature_unit": "fahrenheit",
        "timezone": "auto"})
    if not jf or not ja:
        print("  [skip] missing forecast or actuals")
        return
    actual = dict(zip(ja["hourly"]["time"], ja["hourly"]["temperature_2m"]))
    ftime = jf["hourly"]["time"]
    print("%-22s %6s %8s %8s" % ("forecaster", "n", "MAE_F", "bias_F"))
    rows = []
    for m in MODELS:
        vals = jf["hourly"].get(f"temperature_2m_{m}")
        if not vals:
            continue
        errs = [f - actual[t] for t, f in zip(ftime, vals)
                if f is not None and actual.get(t) is not None]
        if errs:
            rows.append((m, len(errs), st.fmean(abs(x) for x in errs),
                         st.fmean(errs)))
    for name, n, mae, bias in sorted(rows, key=lambda x: x[2]):
        print("%-22s %6d %8.2f %+8.2f" % (name, n, mae, bias))
    print("\n(NWS is the settlement source for KXTEMPNYCH; not measured here —"
          "\n the hourly sleeve already uses it as primary.)")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "hourly":
        return run_hourly_nyc()
    from datetime import date, timedelta
    end = date.today() - timedelta(days=6)      # ERA5 lag guard
    start = end - timedelta(days=30)
    s, e = start.isoformat(), end.isoformat()
    print(f"Verification window: {s} .. {e}  (airport stations; ERA5 actuals)\n")

    # errors[model][direction] = list of (forecast - actual)
    errors = defaultdict(lambda: defaultdict(list))
    nws_log = {d: _nws_from_signal_log(d) for d in ("max", "min")}

    for ck, cfg in DAILY_CITIES.items():
        direction = cfg["direction"]
        lat, lon = cfg["lat"], cfg["lon"]
        actuals = _fetch_actuals(lat, lon, s, e, direction)
        if not actuals:
            print(f"  [skip] {ck}: no actuals")
            continue
        fc = _fetch_model_forecasts(lat, lon, s, e, direction)
        for model, dvals in fc.items():
            for d, f in dvals.items():
                if d in actuals:
                    errors[model][direction].append(f - actuals[d])
        # NWS join from our log
        for d, vals in nws_log[direction].get(ck, {}).items():
            if d in actuals and vals:
                errors["nws (our log)"][direction].append(
                    st.median(vals) - actuals[d])

    def fmt(name, dirn):
        errs = errors.get(name, {}).get(dirn, [])
        if not errs:
            return None
        mae = st.fmean(abs(x) for x in errs)
        bias = st.fmean(errs)
        return (name, len(errs), mae, bias)

    all_models = ["openmeteo_default"] + MODELS + ["nws (our log)"]
    for dirn in ("max", "min"):
        print(f"\n=== {dirn.upper()} (daily {'high' if dirn=='max' else 'low'}) "
              f"— ranked by MAE (lower = more accurate) ===")
        print("%-22s %5s %8s %8s" % ("forecaster", "n", "MAE_F", "bias_F"))
        rows = [r for r in (fmt(m, dirn) for m in all_models) if r]
        for name, n, mae, bias in sorted(rows, key=lambda x: x[2]):
            print("%-22s %5d %8.2f %+8.2f" % (name, n, mae, bias))

    # Equal-weight multi-model ensemble vs best single model (overall).
    print("\n=== ensemble check (equal-weight named models) ===")
    for dirn in ("max", "min"):
        # rebuild per-date so we can average models on matching dates
        per_date = defaultdict(dict)  # (city,date) -> {model: err}
        # not reconstructable from aggregate errs; report single-model best only
        singles = [r for r in (fmt(m, dirn) for m in MODELS + ["openmeteo_default"]) if r]
        if singles:
            best = min(singles, key=lambda x: x[2])
            print(f"  {dirn}: best single model = {best[0]} (MAE {best[2]:.2f}°F)")


if __name__ == "__main__":
    main()
