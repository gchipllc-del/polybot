"""Kalshi DAILY max/min weather signal pipeline.

Parallel to lib/weather_signal.py (hourly directional), but trades the
Kalshi KXHIGHT* (daily max) and KXLOWT* (daily min) markets. Same edge
logic — NWS forecast vs market consensus — but with different mechanics:

  - Forecast source: NWS `/forecast` (12-period day/night) instead of
    `/forecast/hourly`.
  - Settlement: 12-30h ahead instead of 1-3h.
  - σ: ~3-4°F vs ~2-3°F (forecast horizon is longer).

Each city has a "direction" ∈ {"max", "min"} that picks which NWS
forecast period is used as the point estimate. The market YES question
is always "Will today's max/min be ≥ X°F?", so P(YES) = P(temp ≥ strike)
under a normal model around the point forecast.

Pilot scope (2026-05-28): 8 cities. All paper-only. After 24-48h of
samples we'll see if the edge holds enough to consider live execution.
"""
from __future__ import annotations

import json
import math
import re
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradingcore import log_event

# Reuse shared NWS/Kalshi plumbing from the hourly module.
from lib.weather_signal import (
    KALSHI_HOST,
    NWS_USER_AGENT,
    _nws_grid_for,
    _GRID_CACHE,
)

SIGNAL_PATH = Path(__file__).parent.parent / "data" / "weather_daily_signal.jsonl"


# Pilot city set: mix of HIGH and LOW, geographic + climate diversity.
# Coords are major-airport / canonical-station lat/lon to match the
# NWS station Kalshi resolves against (verified against KXHIGHT* /
# KXLOWT* settlement-source URLs).
DAILY_CITIES = {
    # ── Daily maximums ──
    "dal_high": {
        "series": "KXHIGHTDAL", "direction": "max",
        "lat": 32.8998, "lon": -97.0403,
        "label": "Dallas (DFW) Daily Max",
    },
    "phx_high": {
        "series": "KXHIGHTPHX", "direction": "max",
        "lat": 33.4373, "lon": -112.0078,
        "label": "Phoenix (PHX) Daily Max",
    },
    "atl_high": {
        "series": "KXHIGHTATL", "direction": "max",
        "lat": 33.6407, "lon": -84.4277,
        "label": "Atlanta (ATL) Daily Max",
    },
    "sea_high": {
        "series": "KXHIGHTSEA", "direction": "max",
        "lat": 47.4502, "lon": -122.3088,
        "label": "Seattle (SEA) Daily Max",
    },
    # ── Daily minimums ──
    "chi_low": {
        "series": "KXLOWTCHI", "direction": "min",
        "lat": 41.9742, "lon": -87.9073,
        "label": "Chicago (ORD) Daily Min",
    },
    "den_low": {
        "series": "KXLOWTDEN", "direction": "min",
        "lat": 39.8561, "lon": -104.6737,
        "label": "Denver (DEN) Daily Min",
    },
    "dc_low": {
        "series": "KXLOWTDC", "direction": "min",
        "lat": 38.8512, "lon": -77.0402,
        "label": "DC (DCA) Daily Min",
    },
    "lax_low": {
        "series": "KXLOWTLAX", "direction": "min",
        "lat": 33.9425, "lon": -118.4081,
        "label": "Los Angeles (LAX) Daily Min",
    },
}


def daily_sigma_f(
    lead_hours: float | None, seconds_to_close: float | None = None
) -> float:
    """Forecast-uncertainty σ (°F), collapsing as the observation window closes.

    Two regimes, combined by taking the TIGHTER (smaller) σ:

      • horizon σ — conservative NWS daily-forecast RMSE, keyed off lead time to
        the forecast period. Wide when the extreme is still in the future.
        (Per-city EMPIRICAL σ, calibrated from realized forecast-vs-actual
        error, will layer in here in a follow-up once enough post-fix samples
        settle — the v2 of the 2026-06-01 calibration work.)

      • settlement-collapse σ — by the time a daily market nears settlement the
        day's max/min has physically OCCURRED, so the outcome is essentially
        known and σ must collapse toward reporting noise (~0.4°F). This is keyed
        off seconds_to_close, NOT lead_hours: near the day boundary the
        period-matcher can return a *positive* lead even hours before close
        (max markets at 0-6h-to-close showed median lead +8.2h), so lead_hours
        is an unreliable "has it been observed?" proxy.

    2026-06-01: the settlement collapse was added to stop the frozen 2.5°F floor
    from manufacturing fake near-close 'edges' against already-resolved markets
    (the model claimed ~30% on $0.01 contracts the thermometer had settled).
    CAVEAT: this assumes NWS updates the period forecast toward the observed
    extreme as the day progresses; if it lags, a tight σ on a stale forecast
    could overshoot. Thresholds are deliberately conservative pending the
    empirical-σ validation; paper-only sleeve, so exposure is bounded.
    """
    if lead_hours is None or lead_hours <= 0:
        horizon = 2.5
    elif lead_hours <= 12:
        horizon = 3.0
    elif lead_hours <= 24:
        horizon = 3.5
    elif lead_hours <= 48:
        horizon = 4.5
    else:
        horizon = 5.5

    if seconds_to_close is None:
        return horizon
    h = seconds_to_close / 3600.0
    if h <= 3:
        settle = 0.4      # locked in — only reporting/rounding noise remains
    elif h <= 8:
        settle = 1.0      # afternoon high / pre-dawn low almost certainly past
    elif h <= 14:
        settle = 2.0      # extreme likely already occurred
    else:
        return horizon    # too far out for settlement info — trust the forecast
    return min(horizon, settle)


# ── Strike-type-aware probability ────────────────────────────────────
# Kalshi KXHIGHT*/KXLOWT* markets come in THREE strike_type flavors, and
# the YES question differs for each. The old code parsed the T<num> ticker
# suffix and ALWAYS computed P(temp >= strike), which (a) inverted every
# 'less' market — pricing a near-certain NO as a near-certain YES, the
# source of the fake +0.97 edges and the 14 paper losses — and (b) silently
# dropped every 'between' bucket (the B<num> tickers never matched the regex).
# These helpers read strike_type/floor_strike/cap_strike straight from the
# market dict so P(YES) is always the probability of the ACTUAL YES event.

_PROB_FLOOR = 0.02
_PROB_CEIL = 0.98


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    """P(T <= x) for T ~ Normal(mu, sigma)."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def p_yes_for_strike(
    strike_type: str, floor_f: float | None, cap_f: float | None,
    forecast_f: float, sigma_f: float,
) -> float | None:
    """Probability the market's YES event resolves, under Normal(forecast, σ).

    YES event by strike_type:
      greater : actual >  floor_strike      -> P = 1 - CDF(floor)
      less    : actual <  cap_strike        -> P = CDF(cap)
      between : floor <= actual <= cap       -> P = CDF(cap) - CDF(floor)

    Returns None if the strike bounds needed for the type are missing
    (caller skips the market rather than guessing — guessing caused the bug).
    Clipped to [0.02, 0.98] so the bot never bets absurd tail certainty.
    """
    st = (strike_type or "").lower()
    if st in ("greater", "greater_or_equal"):
        if floor_f is None:
            return None
        p = 1.0 - _norm_cdf(floor_f, forecast_f, sigma_f)
    elif st in ("less", "less_or_equal"):
        if cap_f is None:
            return None
        p = _norm_cdf(cap_f, forecast_f, sigma_f)
    elif st == "between":
        if floor_f is None or cap_f is None:
            return None
        p = _norm_cdf(cap_f, forecast_f, sigma_f) - _norm_cdf(floor_f, forecast_f, sigma_f)
    else:
        return None
    return max(_PROB_FLOOR, min(_PROB_CEIL, p))


def margin_toward_yes_f(
    strike_type: str, floor_f: float | None, cap_f: float | None,
    forecast_f: float,
) -> float | None:
    """Signed °F margin of the forecast toward the YES outcome.

    Positive => forecast sits inside the YES zone by this many °F; negative
    => inside the NO zone. This is the SINGLE source of truth the paper-module
    forecast-direction gate keys off, so the gate can't re-introduce the
    per-type inversion (old gate hard-coded 'YES = temp >= strike').

      greater (YES = T > floor)        : forecast - floor
      less    (YES = T < cap)          : cap - forecast
      between (YES = floor <= T <= cap): min(forecast - floor, cap - forecast)
    """
    st = (strike_type or "").lower()
    if st in ("greater", "greater_or_equal"):
        return None if floor_f is None else (forecast_f - floor_f)
    if st in ("less", "less_or_equal"):
        return None if cap_f is None else (cap_f - forecast_f)
    if st == "between":
        if floor_f is None or cap_f is None:
            return None
        return min(forecast_f - floor_f, cap_f - forecast_f)
    return None


@dataclass
class DailyWeatherSample:
    sample_at: str
    city_key: str
    direction: str
    market_ticker: str
    event_ticker: str
    title: str
    close_time: str
    seconds_to_close: float
    strike_f: float
    strike_type: str
    floor_strike_f: float | None
    cap_strike_f: float | None
    yes_margin_f: float | None
    yes_ask: float | None
    yes_bid: float | None
    no_ask: float | None
    no_bid: float | None
    forecast_f: float | None
    forecast_lead_hours: float | None
    nws_p_yes: float | None
    market_p_yes: float | None
    edge: float | None


# ── NWS daily forecast fetch ─────────────────────────────────────────

def _fetch_daily_forecast_periods(lat: float, lon: float) -> list[dict] | None:
    """Pull NWS `/forecast` 12-period day/night forecast for a coord.
    Returns list of periods (each has startTime, temperature, isDaytime,
    temperatureUnit). Caches the grid lookup in shared _GRID_CACHE."""
    key = (round(lat, 4), round(lon, 4))
    grid = _GRID_CACHE.get(key)
    if grid is None:
        grid = _nws_grid_for(lat, lon)
        if grid:
            _GRID_CACHE[key] = grid
        else:
            return None
    # Daily forecast URL = forecast_hourly_url with /hourly stripped.
    daily_url = grid["forecast_hourly_url"].replace("/forecast/hourly", "/forecast")
    try:
        req = urllib.request.Request(daily_url, headers={"User-Agent": NWS_USER_AGENT})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return data["properties"]["periods"]
    except Exception as e:
        log_event("weather_daily_signal", "nws_daily_fetch_failed",
                  {"lat": lat, "lon": lon, "error": str(e)[:200]},
                  result="degraded")
        return None


def _forecast_for_day(
    periods: list[dict], direction: str, close_dt: datetime,
) -> tuple[float | None, float | None]:
    """Find the NWS forecast period matching the calendar day Kalshi will
    settle against. Returns (forecast_F, lead_hours).

    For direction="max": daytime period whose date == close date - 1 day
        (because the market closes at midnight LOCAL the day AFTER the
        observation period, so "today's max" resolves at next-day close).
    For direction="min": nighttime period whose date == close date - 1 day
        (overnight low of the observation calendar day).

    NWS startTime is in UTC; we compare dates in local-of-station tz
    indirectly by allowing matches across the close_dt date and the
    day before."""
    target_date = close_dt.date()
    candidate_dates = [target_date - timedelta(days=1), target_date]
    now = datetime.now(timezone.utc)
    chosen = None
    for p in periods:
        try:
            start = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        is_day = p.get("isDaytime", True)
        if direction == "max" and not is_day:
            continue
        if direction == "min" and is_day:
            continue
        if start.date() not in candidate_dates:
            continue
        # A legitimate observation period must START before the market closes —
        # the day's max/min is observed during the obs window, which precedes
        # the settlement boundary. Near/after close the real obs-day period
        # drops out of the NWS feed (it is in the past), and the next match in
        # forward order is TOMORROW's period (start >= close_dt). Accepting it
        # silently compared a NEXT-DAY forecast against an already-resolved
        # market — lead_hours went POSITIVE near close (e.g. +7.6h at 2.6h to
        # close) and forecast_f missed the settled high by ~11°F, manufacturing
        # a persistent fake edge no σ fix could remove. Skip it: no obs-day
        # forecast => no tradeable signal (caller drops the market). 2026-06-01.
        if start >= close_dt:
            continue
        # Take the FIRST match (closest period in forward order).
        chosen = (start, p)
        break
    if chosen is None:
        return None, None
    start, p = chosen
    temp = p.get("temperature")
    if temp is None:
        return None, None
    if p.get("temperatureUnit") == "C":
        temp = temp * 9 / 5 + 32
    lead_hours = (start - now).total_seconds() / 3600.0
    return float(temp), round(lead_hours, 2)


# ── Discovery ────────────────────────────────────────────────────────

def discover_daily_markets(series: str, max_hours_out: int = 48) -> list[dict]:
    """Find open daily-temp markets for a city's series."""
    import requests
    try:
        events = requests.get(
            f"{KALSHI_HOST}/events",
            params={"series_ticker": series, "status": "open", "limit": 20},
            timeout=15,
        ).json().get("events", [])
    except Exception as e:
        log_event("weather_daily_signal", "kalshi_events_failed",
                  {"series": series, "error": str(e)[:200]}, result="degraded")
        return []

    qualified: list[dict] = []
    now = datetime.now(timezone.utc)
    for ev in events:
        et = ev.get("event_ticker", "")
        if not et:
            continue
        try:
            markets = requests.get(
                f"{KALSHI_HOST}/markets",
                params={"event_ticker": et, "status": "open", "limit": 50},
                timeout=15,
            ).json().get("markets", [])
        except Exception:
            continue
        for m in markets:
            close_iso = m.get("close_time", "")
            try:
                ct = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            secs = (ct - now).total_seconds()
            if secs <= 0 or secs > max_hours_out * 3600:
                continue
            # Strike bounds come from the market's OWN fields, not the ticker
            # suffix — the suffix is T<n> for greater/less but B<n> for
            # between, and (critically) the suffix number does NOT tell you
            # the YES direction. floor_strike/cap_strike + strike_type do.
            strike_type = (m.get("strike_type") or "").lower()
            floor_s = m.get("floor_strike")
            cap_s = m.get("cap_strike")
            floor_f = float(floor_s) if floor_s is not None else None
            cap_f = float(cap_s) if cap_s is not None else None
            # Need the bound(s) the type depends on; skip ambiguous markets
            # rather than guess (guessing a missing bound caused the bug).
            if strike_type in ("greater", "greater_or_equal") and floor_f is None:
                continue
            if strike_type in ("less", "less_or_equal") and cap_f is None:
                continue
            if strike_type == "between" and (floor_f is None or cap_f is None):
                continue
            if strike_type not in (
                "greater", "greater_or_equal", "less", "less_or_equal", "between"
            ):
                continue
            # Representative strike for display/logging + the legacy strike_f
            # field: the active bound for one-sided markets, midpoint for bands.
            if strike_type == "between":
                rep_strike = (floor_f + cap_f) / 2.0
            elif strike_type in ("greater", "greater_or_equal"):
                rep_strike = floor_f
            else:
                rep_strike = cap_f
            m["_parsed_strike"] = rep_strike
            m["_strike_type"] = strike_type
            m["_floor_f"] = floor_f
            m["_cap_f"] = cap_f
            m["_seconds_to_close"] = secs
            m["_close_iso"] = close_iso
            qualified.append(m)
    return qualified


# ── Sampling ─────────────────────────────────────────────────────────

def _to_frac(v):
    if v is None:
        return None
    f = float(v)
    return f if f <= 1.0 else f / 100.0


def _coerce_float(*candidates, default: float = 0.0) -> float:
    """First non-None candidate coerced to float (SDK 2.1.x *_fp fields are
    strings). Returns *default* if all are None or unparseable."""
    for c in candidates:
        if c is None:
            continue
        try:
            return float(c)
        except (TypeError, ValueError):
            continue
    return default


# ── Open-Meteo second source + observation anchor ───────────────────────
# Ported 2026-06-01 from the LIVE hourly sleeve's blended engine
# (lib/weather_signal._blended_forecast). The daily sleeve had been NWS-only
# and single-model — the root reason it was miscalibrated. This brings the
# proven machinery to the daily max/min markets:
#   • two-model ensemble (NWS + Open-Meteo) with agreement-based σ,
#   • per-city empirical bias/σ via lib.weather_calibration,
#   • observation anchoring: as the obs day elapses, blend toward the
#     ALREADY-OBSERVED extreme (a hard directional bound for max/min),
#   • a σ-disagreement floor so a calm empirical σ can't hide a model fight.
_OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _parse_event_date(event_ticker: str):
    """KXHIGHTDAL-26JUN01 -> date(2026, 6, 1). The event ticker carries the
    canonical LOCAL observation date Kalshi settles against — far more robust
    than tz-converting an NWS UTC period start. Returns None if unparseable."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if not m:
        return None
    yy, mon, dd = m.groups()
    mo = _MONTHS.get(mon)
    if mo is None:
        return None
    try:
        return datetime(2000 + int(yy), mo, int(dd)).date()
    except ValueError:
        return None


def _fetch_open_meteo_daily(lat: float, lon: float) -> dict:
    """Open-Meteo forecast — the second model source for the daily sleeve.
    One call returns BOTH the ensemble daily extremes (the cross-check vs NWS)
    and an hourly series spanning yesterday→+2d (to compute the observed
    extreme-so-far for the observation anchor). Returns {} on any failure —
    the second source is an enhancement, never load-bearing."""
    try:
        url = (f"{_OPEN_METEO_BASE}?latitude={lat}&longitude={lon}"
               "&daily=temperature_2m_max,temperature_2m_min"
               "&hourly=temperature_2m&temperature_unit=fahrenheit"
               "&timezone=auto&past_days=1&forecast_days=3")
        data = json.loads(urllib.request.urlopen(
            urllib.request.Request(url), timeout=10).read())
        daily: dict[str, dict] = {}
        d = data.get("daily", {}) or {}
        for ds, mx, mn in zip(d.get("time", []),
                              d.get("temperature_2m_max", []),
                              d.get("temperature_2m_min", [])):
            daily[ds] = {"max": mx, "min": mn}
        hourly = []
        h = data.get("hourly", {}) or {}
        for ts, t in zip(h.get("time", []), h.get("temperature_2m", [])):
            if t is not None:
                hourly.append((ts, float(t)))
        return {"daily": daily, "hourly": hourly,
                "utc_offset_s": int(data.get("utc_offset_seconds", 0) or 0)}
    except Exception:
        return {}


def _observed_extreme_and_weight(om: dict, obs_date, direction: str):
    """From Open-Meteo's hourly series, the extreme OBSERVED SO FAR on the
    observation date plus an anchor weight in [0,1] for how locked-in it is.

    The observed extreme is a hard directional bound: the final max can only
    be >= the max seen so far; the final min only <= the min seen so far. The
    weight ramps with how much of the diurnal window has elapsed — a daytime
    high is essentially set by late afternoon, while a calendar-day low isn't
    safe until the day is nearly over. Returns (None, 0.0) when unavailable."""
    hourly = om.get("hourly") or []
    if not hourly or obs_date is None:
        return None, 0.0
    now_local = (datetime.now(timezone.utc)
                 + timedelta(seconds=om.get("utc_offset_s", 0))).replace(tzinfo=None)
    ds = obs_date.isoformat()
    observed = []
    for ts, t in hourly:
        if not ts.startswith(ds):
            continue
        try:
            hr = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if hr <= now_local:
            observed.append((hr.hour, t))
    if not observed:
        return None, 0.0
    temps = [t for _, t in observed]
    ext = max(temps) if direction == "max" else min(temps)
    latest_hr = max(h for h, _ in observed)
    if direction == "max":
        w = (latest_hr - 6) / 11.0     # high forms ~06:00–17:00 local
    else:
        w = latest_hr / 22.0           # calendar-day low not safe until late
    return ext, max(0.0, min(1.0, w))


def _blended_daily_forecast(
    *, city_key: str, direction: str, nws_forecast_f: float,
    open_meteo_f: float | None, observed_extreme_f: float | None,
    obs_weight: float, lead_hours: float | None, seconds_to_close: float | None,
) -> tuple[float, float, dict]:
    """Combine NWS + Open-Meteo + observed extreme + per-city calibration into
    a single (point_f, sigma_f, meta). Faithful adaptation of the hourly
    sleeve's _blended_forecast for daily max/min markets."""
    meta = {"nws_f": round(nws_forecast_f, 2), "open_meteo_f": open_meteo_f,
            "observed_extreme_f": observed_extreme_f,
            "obs_weight": round(obs_weight, 2)}
    try:
        from lib.weather_calibration import get_calibration
        cal = get_calibration(city_key)
    except Exception:
        cal = {"bias_f": 0.0, "sigma_f": None, "n": 0}
    bias = float(cal.get("bias_f") or 0.0)
    meta["calibration"] = cal

    # NWS canonical + bias correction; settlement-collapsing horizon prior σ.
    forecast_blend = nws_forecast_f + bias
    base_sigma = daily_sigma_f(lead_hours, seconds_to_close)
    disagreement_sigma = 0.0

    # Two-model ensemble: average the (bias-corrected) models; tighten σ on
    # agreement, widen on conflict.
    if open_meteo_f is not None:
        adj_om = open_meteo_f + bias
        diff = abs(nws_forecast_f - open_meteo_f)
        forecast_blend = ((nws_forecast_f + bias) + adj_om) / 2.0
        if diff <= 1.0:
            base_sigma *= 0.80
        elif diff <= 2.0:
            base_sigma *= 0.90
        elif diff > 4.0:
            base_sigma *= 1.30
        disagreement_sigma = diff / 2.0
        meta["model_disagreement_f"] = round(diff, 2)

    # Observation correction. The observed extreme-so-far is a ONE-DIRECTIONAL
    # bound: it can only RAISE a max estimate / LOWER a min estimate (reality
    # already beat the forecast) — it must NEVER drag the point the wrong way
    # before the afternoon peak / overnight trough has even formed. (An earlier
    # weighted blend did exactly that: a morning temp pulled an 86°F max
    # forecast down to 78°F → confident wrong-side NO bets, the original bleed.)
    # Only once the diurnal window has passed AND we're near settlement do we
    # trust the observed extreme as the realized answer over a stale forecast,
    # and collapse σ.
    point = forecast_blend
    if observed_extreme_f is not None and obs_weight > 0:
        window_done = obs_weight >= 0.9
        near_settle = (seconds_to_close is not None
                       and seconds_to_close <= 6 * 3600)
        if window_done and near_settle:
            point = observed_extreme_f
            base_sigma = max(0.3, base_sigma * 0.2)
            meta["obs_locked"] = True
        elif direction == "max":
            point = max(forecast_blend, observed_extreme_f)
        else:
            point = min(forecast_blend, observed_extreme_f)
        meta["obs_anchored"] = True

    # Empirical per-city σ takes over once we have enough settled samples —
    # but only when it is TIGHTER than the (already settlement-collapsed)
    # prior, so a calm day-ahead σ can't undo the near-close collapse.
    emp_sigma = cal.get("sigma_f")
    if emp_sigma is not None:
        base_sigma = min(base_sigma, max(0.5, float(emp_sigma)))
        meta["sigma_source"] = "empirical"
    else:
        meta["sigma_source"] = "horizon_prior"

    # σ-disagreement floor (safety): a model fight is a real source of error;
    # never let a tight σ hide it.
    if disagreement_sigma > base_sigma:
        base_sigma = disagreement_sigma
        meta["sigma_source"] += "+disagreement_floor"

    meta["point_f"] = round(point, 2)
    meta["sigma_f"] = round(base_sigma, 2)
    return point, base_sigma, meta


def sample_signals_for_daily_city(city_key: str) -> list[dict]:
    cfg = DAILY_CITIES.get(city_key)
    if cfg is None:
        return []
    markets = discover_daily_markets(cfg["series"])
    if not markets:
        return []
    periods = _fetch_daily_forecast_periods(cfg["lat"], cfg["lon"])
    if periods is None:
        return []
    # Second model source + observation series (fetched ONCE per city, shared
    # across all of the city's strike markets). Degrades to NWS-only if empty.
    om = _fetch_open_meteo_daily(cfg["lat"], cfg["lon"])
    now_iso = datetime.now(timezone.utc).isoformat()
    samples = []
    for m in markets:
        strike_f = float(m["_parsed_strike"])
        strike_type = m.get("_strike_type", "")
        floor_f = m.get("_floor_f")
        cap_f = m.get("_cap_f")
        try:
            close_dt = datetime.fromisoformat(m["_close_iso"].replace("Z", "+00:00"))
        except ValueError:
            continue
        nws_f, lead_h = _forecast_for_day(periods, cfg["direction"], close_dt)
        if nws_f is None:
            continue
        # Blend NWS with Open-Meteo + observed extreme + per-city calibration.
        obs_date = _parse_event_date(m.get("event_ticker", ""))
        om_daily = (om.get("daily") or {}).get(obs_date.isoformat()) if obs_date else None
        open_meteo_f = om_daily.get(cfg["direction"]) if om_daily else None
        observed_extreme_f, obs_w = _observed_extreme_and_weight(
            om, obs_date, cfg["direction"])
        fc_f, sigma_f, blend_meta = _blended_daily_forecast(
            city_key=city_key, direction=cfg["direction"], nws_forecast_f=nws_f,
            open_meteo_f=open_meteo_f, observed_extreme_f=observed_extreme_f,
            obs_weight=obs_w, lead_hours=lead_h,
            seconds_to_close=m["_seconds_to_close"],
        )
        # Type-aware P(YES): probability the market's ACTUAL yes event resolves.
        p_yes = p_yes_for_strike(strike_type, floor_f, cap_f, fc_f, sigma_f)
        if p_yes is None:
            continue
        yes_margin = margin_toward_yes_f(strike_type, floor_f, cap_f, fc_f)

        ya = _to_frac(m.get("yes_ask_dollars") or m.get("yes_ask"))
        yb = _to_frac(m.get("yes_bid_dollars") or m.get("yes_bid"))
        na = _to_frac(m.get("no_ask_dollars") or m.get("no_ask"))
        nb = _to_frac(m.get("no_bid_dollars") or m.get("no_bid"))
        edge = (p_yes - ya) if ya is not None else None

        ws = DailyWeatherSample(
            sample_at=now_iso, city_key=city_key, direction=cfg["direction"],
            market_ticker=m.get("ticker", ""),
            event_ticker=m.get("event_ticker", ""),
            title=(m.get("title") or "")[:200],
            close_time=m.get("_close_iso", ""),
            seconds_to_close=m["_seconds_to_close"],
            strike_f=strike_f,
            strike_type=strike_type,
            floor_strike_f=floor_f,
            cap_strike_f=cap_f,
            yes_margin_f=yes_margin,
            yes_ask=ya, yes_bid=yb, no_ask=na, no_bid=nb,
            forecast_f=fc_f, forecast_lead_hours=lead_h,
            nws_p_yes=p_yes, market_p_yes=ya, edge=edge,
        )
        d = asdict(ws)
        d["sigma_f"] = sigma_f
        # Forecast-blend transparency: raw NWS, Open-Meteo, observed extreme,
        # model disagreement, calibration, and σ source all live here.
        d["nws_forecast_f"] = nws_f
        d["open_meteo_f"] = open_meteo_f
        d["forecast_blend"] = blend_meta
        # SDK 2.1.x renamed liquidity fields to *_fp / *_dollars; fall back to
        # the legacy names so a future liquidity filter sees real volume (not 0,
        # which would reject every market). These daily-temp books ARE liquid
        # (near-money strikes run 800-10k contracts/24h). The *_fp fields come
        # back as STRINGS, so coerce to float (a numeric gate would crash on str).
        d["volume_24h"] = _coerce_float(
            m.get("volume_24h_fp"), m.get("volume_24h"), default=0.0)
        d["open_interest"] = _coerce_float(
            m.get("open_interest_fp"), m.get("open_interest"), default=0.0)
        samples.append(d)
    return samples


def sample_signals() -> list[dict]:
    out: list[dict] = []
    for city in DAILY_CITIES:
        try:
            out.extend(sample_signals_for_daily_city(city))
        except Exception as e:
            log_event("weather_daily_signal", "city_sample_failed",
                      {"city": city, "error": str(e)[:200]}, result="degraded")
    out.sort(key=lambda s: -abs(s.get("edge") or 0))
    return out


def persist_samples(samples: list[dict]) -> None:
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_PATH, "a") as f:
        for s in samples:
            f.write(json.dumps(s) + "\n")
    # Bounded retention (diagnostic tail; keep from growing without limit).
    try:
        from lib.log_rotation import rotate_if_needed
        rotate_if_needed(SIGNAL_PATH)
    except Exception:
        pass


def run_signal_cycle(record_paper_trades: bool = True,
                     settle_paper_trades: bool = True) -> dict:
    samples = sample_signals()
    persist_samples(samples)
    n_paper = 0
    if record_paper_trades and samples:
        try:
            from lib.weather_daily_paper import record_paper_trades_from_samples
            new = record_paper_trades_from_samples(samples)
            n_paper = len(new)
        except Exception as e:
            log_event("weather_daily_signal", "paper_record_failed",
                      {"error": str(e)[:200]}, result="degraded")

    settle_summary = {}
    if settle_paper_trades:
        try:
            from lib.weather_daily_paper import settle_paper_trades as _settle
            settle_summary = _settle()
        except Exception as e:
            log_event("weather_daily_signal", "paper_settle_failed",
                      {"error": str(e)[:200]}, result="degraded")

    log_event("weather_daily_signal", "signal_cycle", {
        "n_markets": len(samples),
        "n_cities": len({s.get("city_key") for s in samples}),
        "paper_trades_opened": n_paper,
        "paper_settled": settle_summary.get("settled_now", 0),
        "max_edge": max((abs(s.get("edge") or 0) for s in samples), default=0),
    })
    return {
        "n_markets": len(samples),
        "paper_trades_opened": n_paper,
        "settle_summary": settle_summary,
        "samples": list(samples),
    }
