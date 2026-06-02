"""Kalshi hourly-weather signal pipeline.

Compares Kalshi temperature-strike markets against NWS (National Weather
Service) hourly forecasts. The edge: NWS forecasts are produced by the
official US weather model + human meteorologists; their RMSE on
next-1-to-6-hour temperature is ~2-3°F. Kalshi prices these markets
based on observed retail flow which often lags or over-weights extreme
outcomes — when our normal-distribution model around the NWS point
estimate disagrees with the Kalshi price by enough, we trade.

Supported series (all "hourly directional temperature" markets):
    KXTEMPNYCH  — NYC (Central Park)
    KXTEMPCHIH  — Chicago (O'Hare)
    KXTEMPDCH   — DC (Reagan National)
    KXTEMPBOSH  — Boston (Logan)
    KXTEMPLAXH  — Los Angeles
    KXTEMPMIAH  — Miami

Strikes encode the "above X°F at the close time" threshold (Kalshi
ticker format ...-T62.99 = "temp will be 62.99°F or above").
"""

from __future__ import annotations

import json
import math
import re
import urllib.request

from lib.forecaster_ensemble import skill_weighted_point as _ensemble_point
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from tradingcore import log_event

KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"
NWS_USER_AGENT = "polybot-weather-scanner (jesse@gchipllc.com)"
SIGNAL_PATH = Path(__file__).parent.parent / "data" / "weather_signal.jsonl"

# Per-city registry: Kalshi series → station lat/lon (the location whose
# observations Kalshi uses for resolution) + label. Coordinates chosen
# to match the actual station used by Kalshi (e.g. KXTEMPNYCH resolves on
# Central Park, not LaGuardia).
CITIES = {
    "nyc": {
        "series": "KXTEMPNYCH",
        "lat": 40.7831, "lon": -73.9712,
        "label": "NYC (Central Park)",
    },
    "chicago": {
        "series": "KXTEMPCHIH",
        "lat": 41.9742, "lon": -87.9073,
        "label": "Chicago (O'Hare)",
    },
    "dc": {
        "series": "KXTEMPDCH",
        "lat": 38.8512, "lon": -77.0402,
        "label": "DC (Reagan National)",
    },
    "boston": {
        "series": "KXTEMPBOSH",
        "lat": 42.3656, "lon": -71.0096,
        "label": "Boston (Logan)",
    },
    "lax": {
        "series": "KXTEMPLAXH",
        "lat": 33.9425, "lon": -118.4081,
        "label": "Los Angeles (LAX)",
    },
    "miami": {
        "series": "KXTEMPMIAH",
        "lat": 25.7959, "lon": -80.2870,
        "label": "Miami (Miami Intl)",
    },
}

# NWS hourly temperature forecast σ (RMSE) in degrees F. The error grows
# with forecast horizon; using a flat 2.5°F under-weights short-horizon
# certainty (the model is much better at +1h than +12h). Calibrated from
# NOAA's public verification stats; refine via post-trade error tracking.
def horizon_aware_sigma_f(lead_hours: float) -> float:
    if lead_hours is None or lead_hours <= 0:
        return 1.5    # we're past the forecast time — use observation σ
    if lead_hours <= 1:    return 1.5
    if lead_hours <= 3:    return 2.0
    if lead_hours <= 6:    return 2.5
    if lead_hours <= 12:   return 3.0
    if lead_hours <= 24:   return 4.0
    return 5.0            # >24h is a long bet; bot won't take these anyway

# Open-Meteo: free second-source forecast for ensemble cross-check.
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

# Weight given to OBSERVATION blend vs FORECAST blend when both available.
# At T-2min, the observation tells us nearly everything; at T-12h, the
# observation tells us very little about the future temp.
def obs_blend_weight(lead_hours: float) -> float:
    """Weight on observation vs forecast in the blended point estimate."""
    if lead_hours is None or lead_hours <= 0:    return 1.0
    if lead_hours <= 0.25:                       return 0.95   # 15 min
    if lead_hours <= 1:                          return 0.70
    if lead_hours <= 3:                          return 0.40
    if lead_hours <= 6:                          return 0.20
    return 0.0                                                  # ignore obs at long lead


# Trend awareness is gated behind a strategy flag. It shifts the point
# estimate (projects temp forward instead of anchoring to the lagging
# current reading), so it can change trade DIRECTION — it must stay OFF
# in live until backtested + approved. The σ-disagreement floor is NOT
# gated (it only ever widens σ → strictly more conservative).
_STRATEGY_PATH = Path(__file__).resolve().parent.parent / "config" / "weather_strategy.yaml"


def _trend_aware_enabled() -> bool:
    """Read weather_trend_aware from weather_strategy.yaml (default False).
    Re-read per cycle so flipping the flag takes effect on the next pass.
    Any failure → False (trend is an enhancement, never load-bearing)."""
    try:
        import yaml
        if not _STRATEGY_PATH.exists():
            return False
        with open(_STRATEGY_PATH) as f:
            data = yaml.safe_load(f) or {}
        return bool(data.get("weather_trend_aware", False)) if isinstance(data, dict) else False
    except Exception:
        return False


@dataclass
class WeatherSample:
    sample_at: str
    city: str
    market_ticker: str
    event_ticker: str
    title: str
    close_time: str
    seconds_to_close: float
    strike_f: float                    # threshold in Fahrenheit
    yes_ask: float | None
    yes_bid: float | None
    no_ask: float | None
    no_bid: float | None
    nws_forecast_f: float | None       # NWS point forecast at close_time
    forecast_lead_hours: float | None
    nws_p_yes: float | None            # our P(temp ≥ strike)
    market_p_yes: float | None         # = yes_ask (what market thinks)
    edge: float | None                 # nws_p_yes - market_p_yes


# ── NWS hourly forecast fetch ────────────────────────────────────────

def _nws_grid_for(lat: float, lon: float) -> dict | None:
    """One-time resolution from lat/lon → NWS gridded forecast URL.
    NWS asks us to cache this so we don't hit /points every cycle."""
    try:
        req = urllib.request.Request(
            f"https://api.weather.gov/points/{lat},{lon}",
            headers={"User-Agent": NWS_USER_AGENT},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return {
            "office": data["properties"]["gridId"],
            "x": data["properties"]["gridX"],
            "y": data["properties"]["gridY"],
            "forecast_hourly_url": data["properties"]["forecastHourly"],
        }
    except Exception as e:
        log_event("weather_signal", "nws_grid_failed",
                  {"lat": lat, "lon": lon, "error": str(e)[:200]},
                  result="degraded")
        return None


# Cache grid lookups in-process. NWS recommends caching for hours/days.
_GRID_CACHE: dict[tuple[float, float], dict] = {}


def _fetch_hourly_forecast(lat: float, lon: float) -> list[dict] | None:
    """Pull the NWS hourly forecast periods for a coord. Returns periods
    list (each has startTime, temperature, temperatureUnit)."""
    key = (round(lat, 4), round(lon, 4))
    grid = _GRID_CACHE.get(key)
    if grid is None:
        grid = _nws_grid_for(lat, lon)
        if grid:
            _GRID_CACHE[key] = grid
        else:
            return None
    try:
        req = urllib.request.Request(
            grid["forecast_hourly_url"], headers={"User-Agent": NWS_USER_AGENT}
        )
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return data["properties"]["periods"]
    except Exception as e:
        log_event("weather_signal", "nws_forecast_failed",
                  {"lat": lat, "lon": lon, "error": str(e)[:200]},
                  result="degraded")
        return None


def _forecast_at(periods: list[dict], target_iso: str) -> tuple[float | None, float | None]:
    """Return (temp_F, lead_hours) for the period covering target_iso.
    NWS periods are 1-hour buckets. Falls back to closest period."""
    try:
        target = datetime.fromisoformat(target_iso.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    best = None
    for p in periods:
        try:
            start = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        delta = abs((target - start).total_seconds())
        if best is None or delta < best[0]:
            best = (delta, p)
    if best is None:
        return None, None
    _, p = best
    temp = p.get("temperature")
    if temp is None:
        return None, None
    if p.get("temperatureUnit") == "C":
        temp = temp * 9 / 5 + 32   # normalize to F
    start = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
    lead_hours = (start - datetime.now(timezone.utc)).total_seconds() / 3600.0
    return float(temp), round(lead_hours, 2)


def _p_above_strike(forecast_f: float, strike_f: float,
                    sigma_f: float) -> float:
    """P(actual_temp ≥ strike | point forecast = forecast_f) under a
    normal forecast-error model with the given std-dev. Clipped to
    [0.02, 0.98] so the bot doesn't bet absurd certainty on tail outcomes."""
    if sigma_f <= 0:
        return 1.0 if forecast_f >= strike_f else 0.0
    z = (forecast_f - strike_f) / sigma_f
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return max(0.02, min(0.98, p))


# ── Current-temperature observation fetch ────────────────────────────

def _fetch_current_temp_f(lat: float, lon: float) -> float | None:
    """Pull the closest NWS station's latest observed temperature (°F).
    Used to anchor short-horizon predictions to reality."""
    try:
        req = urllib.request.Request(
            f"https://api.weather.gov/points/{lat},{lon}/stations",
            headers={"User-Agent": NWS_USER_AGENT},
        )
        stations = json.loads(urllib.request.urlopen(req, timeout=10).read())
        feats = stations.get("features", [])
        if not feats:
            return None
        station_id = feats[0]["properties"]["stationIdentifier"]
        req = urllib.request.Request(
            f"https://api.weather.gov/stations/{station_id}/observations/latest",
            headers={"User-Agent": NWS_USER_AGENT},
        )
        obs = json.loads(urllib.request.urlopen(req, timeout=10).read())
        temp_c = obs["properties"]["temperature"]["value"]
        if temp_c is None:
            return None
        return float(temp_c) * 9 / 5 + 32
    except Exception:
        return None


def _fetch_recent_obs(lat: float, lon: float, limit: int = 4) -> list[tuple[datetime, float]]:
    """Recent observed temps (°F) at the closest NWS station, newest first.
    Used to measure the *realized* temperature trend (dT/dt) so the model can
    project where temp is HEADING, not just where it sits now. Returns [] on
    any failure — trend is an enhancement, never allowed to take a cycle down."""
    try:
        req = urllib.request.Request(
            f"https://api.weather.gov/points/{lat},{lon}/stations",
            headers={"User-Agent": NWS_USER_AGENT},
        )
        stations = json.loads(urllib.request.urlopen(req, timeout=10).read())
        feats = stations.get("features", [])
        if not feats:
            return []
        station_id = feats[0]["properties"]["stationIdentifier"]
        req = urllib.request.Request(
            f"https://api.weather.gov/stations/{station_id}/observations?limit={int(limit)}",
            headers={"User-Agent": NWS_USER_AGENT},
        )
        obs = json.loads(urllib.request.urlopen(req, timeout=10).read())
        out: list[tuple[datetime, float]] = []
        for feat in obs.get("features", []):
            props = feat.get("properties", {}) or {}
            tval = (props.get("temperature") or {}).get("value")
            tstr = props.get("timestamp")
            if tval is None or not tstr:
                continue
            try:
                ts = datetime.fromisoformat(tstr.replace("Z", "+00:00"))
            except ValueError:
                continue
            out.append((ts, float(tval) * 9 / 5 + 32))
        out.sort(key=lambda x: x[0], reverse=True)   # newest first
        return out
    except Exception:
        return []


# ── Open-Meteo cross-check (free second-source forecast) ────────────

def _fetch_open_meteo_temp(lat: float, lon: float, target_iso: str) -> float | None:
    """Hourly temperature forecast from Open-Meteo (DEFAULT model). Kept as a
    fallback second-opinion for callers (e.g. the trend backtester) that don't
    use the multi-model ensemble. The live sleeve uses
    _fetch_open_meteo_models_temp instead."""
    try:
        req = urllib.request.Request(
            f"{OPEN_METEO_BASE}?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m&temperature_unit=fahrenheit"
            "&forecast_days=2"
        )
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        target_iso_short = target_iso[:13]  # match to nearest hour
        for t, temp in zip(times, temps):
            if t[:13] == target_iso_short:
                return float(temp)
    except Exception:
        pass
    return None


# Named NWP models pulled as SEPARATE ensemble members (mirrors the daily
# sleeve). Per-model hourly skill at NYC Central Park (scripts/forecaster_accuracy
# .py hourly, 21-day ERA5 verification): ECMWF 0.82°F ≪ GFS 1.65 < ICON 1.78
# < GEM 2.04 — ECMWF ~2× more accurate, so GEM is excluded and the default
# (== GFS for US points) is replaced by the named set.
_OM_MODELS_HOURLY = ["ecmwf_ifs025", "icon_seamless", "gfs_seamless"]

# Inverse-MAE weights for the hourly ensemble. NWS is the settlement source for
# KXTEMPNYCH and the sleeve's proven primary; set co-equal with the measured-best
# (ECMWF) as a defensible prior (unverified over the same window). Re-derive via
# the script as data accrues.
_HOURLY_FORECASTER_MAE = {
    "nws": 0.82, "ecmwf_ifs025": 0.82, "gfs_seamless": 1.65, "icon_seamless": 1.78,
}


def _fetch_open_meteo_models_series(lat: float, lon: float) -> dict:
    """Per-model hourly temperature SERIES (ECMWF/ICON/GFS) for the next ~2 days,
    fetched ONCE per city. Returns {model: {hour_prefix(YYYY-MM-DDTHH): temp_f}}
    for O(1) per-market lookup — a city's ~24 markets all index THIS one result
    instead of each making its own API call. Best-effort: {} on failure, and the
    blend degrades to NWS-only (or the legacy single fetch) per market."""
    out: dict = {}
    try:
        req = urllib.request.Request(
            f"{OPEN_METEO_BASE}?latitude={lat}&longitude={lon}"
            "&hourly=temperature_2m&temperature_unit=fahrenheit&forecast_days=2"
            f"&models={','.join(_OM_MODELS_HOURLY)}"
        )
        h = json.loads(urllib.request.urlopen(req, timeout=10).read()).get("hourly", {})
        times = h.get("time", [])
        for mdl in _OM_MODELS_HOURLY:
            arr = h.get(f"temperature_2m_{mdl}")
            if not arr:
                continue
            by_hour = {t[:13]: float(v) for t, v in zip(times, arr) if v is not None}
            if by_hour:
                out[mdl] = by_hour
    except Exception:
        pass
    return out


def _models_at(series: dict, target_iso: str) -> dict:
    """Index a per-model hourly series (from _fetch_open_meteo_models_series) to
    {model: temp_f} at the target close hour."""
    tgt = target_iso[:13]
    out = {}
    for mdl, by_hour in (series or {}).items():
        v = by_hour.get(tgt)
        if v is not None:
            out[mdl] = v
    return out


def _fetch_open_meteo_models_temp(lat: float, lon: float, target_iso: str) -> dict:
    """Per-model temps at ONE target hour. Thin wrapper (fetch series + index)
    for callers outside the per-city loop; the loop indexes a once-per-city
    series via _models_at instead, to avoid a redundant fetch per market."""
    return _models_at(_fetch_open_meteo_models_series(lat, lon), target_iso)


# Cap on |dT/dt| we'll trust. Surface-air temps rarely move faster than this;
# a steeper computed slope means noisy obs or a sensor glitch — clamp it.
MAX_TREND_F_PER_HR = 8.0

# Surgical trend gate: only let the trend PROJECT the observation forward when
# the forecast disagrees with the current reading by at least this much (°F).
# Small gap = stable air, the raw-obs anchor is fine (and is where the
# profitable cheap-NO book lives); large gap = temperature moving fast and the
# obs is stale (the 66°-obs vs 74°-forecast miss that lost a live NO bet).
TREND_DISAGREE_GATE_F = 4.0


def _temp_trend_f_per_hr(
    *, periods: list[dict], now_iso: str, nws_close_f: float,
    lead_hours: float, obs_series: list[tuple[datetime, float]] | None,
) -> tuple[float, dict]:
    """Estimate the temperature trend (°F/hr) heading into close.

    Two sources, each covering the other's blind spot:
      • Forecast slope = (NWS@close − NWS@now) / lead. Free (reuses the hourly
        periods) and *turning-point aware* — the forecast already bends at the
        diurnal peak, so it won't project a rise straight past the peak.
      • Realized slope = least-squares dT/dt over the last few station obs.
        Responsive: catches warming running AHEAD of the forecast (the exact
        failure mode that cost us the 68°F NO bet).

    Combine (turning-point guard): if both agree in sign, trust the steeper
    magnitude (realized warming can outrun a stale forecast); if they disagree
    in sign we're near a turning point — trust the forecast, which knows the
    peak is coming. Result clamped to ±MAX_TREND_F_PER_HR."""
    meta: dict = {}
    fc_trend = None
    if lead_hours and lead_hours > 0:
        fc_now, _ = _forecast_at(periods, now_iso)
        if fc_now is not None:
            fc_trend = (nws_close_f - fc_now) / lead_hours
            meta["fc_now_f"] = round(fc_now, 2)
            meta["fc_trend_f_per_hr"] = round(fc_trend, 3)

    obs_trend = None
    if obs_series and len(obs_series) >= 2:
        # Least-squares slope over recent obs; x = hours relative to newest.
        t0 = obs_series[0][0]
        xs = [(t - t0).total_seconds() / 3600.0 for t, _ in obs_series]
        ys = [v for _, v in obs_series]
        n = len(xs)
        mx = sum(xs) / n
        my = sum(ys) / n
        den = sum((x - mx) ** 2 for x in xs)
        if den > 1e-9:
            obs_trend = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
            meta["obs_trend_f_per_hr"] = round(obs_trend, 3)
            meta["obs_window_hr"] = round(-min(xs), 2)

    if fc_trend is None and obs_trend is None:
        return 0.0, meta
    if fc_trend is None:
        trend = obs_trend
    elif obs_trend is None:
        trend = fc_trend
    elif (fc_trend >= 0) == (obs_trend >= 0):
        trend = obs_trend if abs(obs_trend) > abs(fc_trend) else fc_trend
    else:
        trend = fc_trend   # opposite signs → turning point; forecast knows the peak
    trend = max(-MAX_TREND_F_PER_HR, min(MAX_TREND_F_PER_HR, trend))
    meta["trend_f_per_hr"] = round(trend, 3)
    return trend, meta


def _blended_forecast(
    *, city: str, nws_forecast_f: float, open_meteo_f: float | None,
    current_obs_f: float | None, lead_hours: float,
    trend_f_per_hr: float | None = None, trend_k: float = 0.5,
    model_forecasts: dict | None = None,
) -> tuple[float, float, dict]:
    """Combine NWS forecast + Open-Meteo + current observation into a
    single point estimate + σ. Returns (point_f, sigma_f, meta).

    Applies per-city bias correction (forecast += bias_f) and empirical σ
    (replaces horizon prior) once we have enough settled trades. See
    lib/weather_calibration for sample-count thresholds.

    trend_f_per_hr: when supplied (trend awareness ON), the current
    observation is projected forward to close (obs + trend·lead) before the
    obs/forecast blend, and σ is inflated for the extrapolation. When None,
    behaviour is identical to the anchor-to-current-obs model. The
    σ-disagreement floor below runs regardless of this flag."""
    meta = {
        "nws_f": nws_forecast_f,
        "open_meteo_f": open_meteo_f,
        "model_forecasts": model_forecasts,
        "current_obs_f": current_obs_f,
        "lead_hours": lead_hours,
    }

    # Pull per-city calibration. Fall back gracefully when the module
    # can't load (import error, missing data file, etc.) — we never want
    # a calibration miss to take the signal cycle down.
    try:
        from lib.weather_calibration import get_calibration
        cal = get_calibration(city)
    except Exception:
        cal = {"bias_f": 0.0, "sigma_f": None, "n": 0}
    meta["calibration"] = cal

    # Start with NWS as the canonical forecast, then add bias correction.
    # bias_f = mean(actual - forecast), so a positive bias means past
    # forecasts under-predicted → bump the new forecast up by that much.
    forecast_blend = nws_forecast_f + float(cal.get("bias_f") or 0.0)
    base_sigma = horizon_aware_sigma_f(lead_hours)
    # Half the NWS/Open-Meteo spread — a floor on how wrong the blend could
    # be when the two models disagree. Stays 0 when there's no second source.
    disagreement_sigma = 0.0

    # Skill-weighted MULTI-MODEL ensemble (ECMWF/ICON/GFS + bias-corrected NWS),
    # inverse-MAE weighted with median outlier-rejection — the same robust
    # combiner as the daily sleeve. ECMWF measured ~2× more accurate hourly at
    # NYC (0.82°F vs GFS 1.65), and Open-Meteo's old "default" source was just
    # GFS. This upgrades ONLY the forecast component; the current-obs anchor +
    # trend below (where the cheap-NO edge lives, #161) are byte-for-byte
    # unchanged. Falls back to the legacy single-Open-Meteo path when no
    # per-model dict is supplied (e.g. the trend backtester).
    if model_forecasts:
        bias = float(cal.get("bias_f") or 0.0)
        contributions = {"nws": nws_forecast_f + bias}
        for mname, mval in model_forecasts.items():
            if mval is not None and math.isfinite(mval):
                contributions[mname] = float(mval)
        blended, kept = _ensemble_point(
            _HOURLY_FORECASTER_MAE, contributions,
            outlier_reject_f=5.0, default_mae=1.6)
        if blended is not None:
            forecast_blend = blended
        meta["ensemble_used"] = kept
        dropped = sorted(set(contributions) - set(kept))
        if dropped:
            meta["ensemble_dropped"] = dropped
        # σ from the CREDIBLE (kept) members only. A rejected bust is dropped
        # from the point, so it must NOT also inflate σ — double-counting its
        # badness would suppress an otherwise high-conviction consensus trade
        # (3 models agreeing shouldn't be made "uncertain" by 1 rogue outlier).
        # When models GENUINELY disagree (none clearly rogue) nothing is dropped,
        # so the full spread still widens σ as real uncertainty.
        kept_vals = [contributions[n] for n in kept]
        if len(kept_vals) > 1:
            spread = max(kept_vals) - min(kept_vals)
            disagreement_sigma = spread / 2.0
            meta["model_spread_f"] = round(spread, 2)
            if spread <= 1.0:
                base_sigma *= 0.80   # consensus → tighter
            elif spread <= 2.0:
                base_sigma *= 0.90
            elif spread > 4.0:
                base_sigma *= 1.30   # credible models fight → wider σ
    elif open_meteo_f is not None:
        # Legacy 2-model path (NWS + Open-Meteo default). Kept for callers that
        # don't pass model_forecasts; identical to pre-ensemble behavior.
        bias = float(cal.get("bias_f") or 0.0)
        adj_open_meteo = open_meteo_f + bias
        diff = abs(nws_forecast_f - open_meteo_f)
        forecast_blend = ((nws_forecast_f + bias) + adj_open_meteo) / 2.0
        if diff <= 1.0:
            base_sigma *= 0.80   # high agreement → tighter
        elif diff <= 2.0:
            base_sigma *= 0.90
        elif diff > 4.0:
            base_sigma *= 1.30   # disagreement → wider σ
        disagreement_sigma = diff / 2.0
        meta["model_disagreement_f"] = round(diff, 2)

    # Anchor to where the temperature is HEADING at short lead times. With
    # trend awareness on, project the current obs forward to close
    # (obs + trend·lead); otherwise anchor to the raw current obs.
    point = forecast_blend
    if current_obs_f is not None and lead_hours is not None:
        w = obs_blend_weight(lead_hours)
        if w > 0:
            anchor = current_obs_f
            # Surgical disagreement gate: only project the observation forward
            # along the trend when the forecast STRONGLY disagrees with the
            # current reading (|forecast − obs| ≥ gate) — temp moving fast, raw
            # obs stale. On stable air (small gap) keep anchoring to the raw
            # obs so the profitable cheap-NO book is left undisturbed.
            disagree = abs(forecast_blend - current_obs_f)
            meta["fc_obs_gap_f"] = round(disagree, 2)
            if (trend_f_per_hr is not None and lead_hours > 0
                    and disagree >= TREND_DISAGREE_GATE_F):
                anchor = current_obs_f + trend_f_per_hr * lead_hours
                meta["projected_obs_f"] = round(anchor, 2)
                meta["trend_f_per_hr"] = round(trend_f_per_hr, 3)
                meta["trend_gate"] = "on"
            elif trend_f_per_hr is not None:
                meta["trend_gate"] = "off_small_gap"
            point = w * anchor + (1 - w) * forecast_blend
            meta["obs_blend_weight"] = round(w, 2)

    # Empirical σ from settled trades wins over the horizon prior once we
    # have ≥ MIN_SIGMA_SAMPLES samples — the prior is conservative on
    # purpose (it assumes worst-case NWS RMSE), but real data shows tighter
    # variance for stable cities (NYC ~0.9°F vs the prior's 1.5-2.5°F).
    emp_sigma = cal.get("sigma_f")
    if emp_sigma is not None:
        # Floor at 0.5°F so a freakishly tight sample window doesn't make
        # the model bet absurd certainty.
        base_sigma = max(0.5, float(emp_sigma))
        meta["sigma_source"] = "empirical"
    else:
        meta["sigma_source"] = "horizon_prior"

    # ── σ-disagreement floor (safety; runs regardless of trend flag) ──
    # A calm-day empirical σ (NYC ≈0.9°F) must never HIDE a live model fight.
    # When NWS and Open-Meteo split, half that spread is a floor on how wrong
    # the blend could be — so we bet that uncertainty, not false certainty.
    # Root-caused from a 68°F NO loss where a 5.4°F NWS/OM split was collapsed
    # to σ≈0.9 → P(yes)=0.02 → overconfident NO that lost.
    if disagreement_sigma > base_sigma:
        base_sigma = disagreement_sigma
        meta["sigma_source"] += "+disagreement_floor"
    meta["disagreement_sigma_f"] = round(disagreement_sigma, 2)

    # ── Trend-extrapolation σ (only when projecting) ──────────────────
    # Projecting obs forward adds uncertainty that grows with both slope and
    # lead. Variances add: σ² += (k·|trend|·lead)².
    if trend_f_per_hr is not None and lead_hours and lead_hours > 0:
        trend_unc = trend_k * abs(trend_f_per_hr) * lead_hours
        if trend_unc > 0:
            base_sigma = math.sqrt(base_sigma ** 2 + trend_unc ** 2)
            meta["trend_sigma_add_f"] = round(trend_unc, 2)

    meta["point_f"] = round(point, 2)
    meta["sigma_f"] = round(base_sigma, 2)
    return point, base_sigma, meta


# ── Discovery ────────────────────────────────────────────────────────

def discover_temp_markets(series: str, max_hours_out: int = 12) -> list[dict]:
    """Find open temp markets for a city's series."""
    import requests
    try:
        events = requests.get(
            f"{KALSHI_HOST}/events",
            params={"series_ticker": series, "status": "open", "limit": 20},
            timeout=15,
        ).json().get("events", [])
    except Exception as e:
        log_event("weather_signal", "kalshi_events_failed",
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
            # Parse strike from ticker (...-T62.99)
            mt = re.search(r"T(\-?\d+(?:\.\d+)?)$", m.get("ticker", ""))
            if not mt:
                continue
            m["_parsed_strike"] = float(mt.group(1))
            m["_seconds_to_close"] = secs
            m["_close_iso"] = close_iso
            qualified.append(m)
    return qualified


# ── Sampling ─────────────────────────────────────────────────────────

def sample_signals_for_city(city_key: str) -> list[WeatherSample]:
    cfg = CITIES.get(city_key)
    if cfg is None:
        return []
    markets = discover_temp_markets(cfg["series"])
    if not markets:
        return []
    periods = _fetch_hourly_forecast(cfg["lat"], cfg["lon"])
    if periods is None:
        return []
    # Fetch current observation ONCE per city per cycle (anchors short-horizon forecasts).
    current_obs_f = _fetch_current_temp_f(cfg["lat"], cfg["lon"])
    now_iso = datetime.now(timezone.utc).isoformat()

    # Trend awareness (gated, default OFF). When on, pull the recent obs
    # series once per city so each market's estimate can project temp forward
    # to close instead of anchoring to a lagging reading. See _blended_forecast
    # and config/weather_strategy.yaml: weather_trend_aware.
    trend_aware = _trend_aware_enabled()
    # Fetch the recent-obs series EVERY cycle (not only when trend is live):
    # the shadow A/B below computes the trend-aware estimate even while
    # weather_trend_aware is OFF, and needs the obs slope to do it.
    obs_series = _fetch_recent_obs(cfg["lat"], cfg["lon"])

    # Per-model Open-Meteo hourly series (ECMWF/ICON/GFS), fetched ONCE per city
    # and indexed per market below — avoids a redundant API call for each of the
    # city's ~24 markets (they all read overlapping hours of the same series).
    om_models_series = _fetch_open_meteo_models_series(cfg["lat"], cfg["lon"])

    def to_frac(v):
        if v is None:
            return None
        f = float(v)
        return f if f <= 1.0 else f / 100.0

    samples = []
    for m in markets:
        strike_f = float(m["_parsed_strike"])
        nws_forecast_f, lead_h = _forecast_at(periods, m["_close_iso"])
        if nws_forecast_f is None:
            continue
        # Per-model second-source forecasts (ECMWF/ICON/GFS) for the
        # skill-weighted ensemble — indexed from the once-per-city series (no
        # per-market fetch). open_meteo_f headline = ECMWF. Only when the whole
        # series fetch failed do we fall back to the legacy single per-market
        # call; if the series exists but lacks THIS hour, blend on NWS-only.
        model_forecasts = _models_at(om_models_series, m["_close_iso"])
        if model_forecasts:
            open_meteo_f = model_forecasts.get("ecmwf_ifs025")
        elif not om_models_series:
            open_meteo_f = _fetch_open_meteo_temp(
                cfg["lat"], cfg["lon"], m["_close_iso"])
        else:
            open_meteo_f = None
        # Trend (°F/hr) heading into THIS market's close. Computed EVERY cycle
        # (not just when live) so the shadow A/B can evaluate trend-aware
        # against real outcomes while weather_trend_aware is still OFF.
        trend_f, trend_meta = _temp_trend_f_per_hr(
            periods=periods, now_iso=now_iso, nws_close_f=nws_forecast_f,
            lead_hours=lead_h or 0, obs_series=obs_series,
        )
        # LIVE estimate — trend applied ONLY when weather_trend_aware is ON, so
        # live behavior is unchanged until the shadow validates. Drives the
        # acted-on p_yes / edge.
        point_f, sigma_f, blend_meta = _blended_forecast(
            city=city_key, nws_forecast_f=nws_forecast_f,
            open_meteo_f=open_meteo_f, current_obs_f=current_obs_f,
            lead_hours=lead_h or 0,
            trend_f_per_hr=(trend_f if trend_aware else None),
            model_forecasts=model_forecasts,
        )
        if trend_meta:
            blend_meta["trend"] = trend_meta
        p_yes = _p_above_strike(point_f, strike_f, sigma_f)
        # SHADOW estimate — gated trend-aware ALWAYS computed + logged, NEVER
        # acted on. Compare shadow vs outcomes over the next batch of trades;
        # flip weather_trend_aware live only once it beats live on P&L.
        shadow_point, shadow_sigma, shadow_meta = _blended_forecast(
            city=city_key, nws_forecast_f=nws_forecast_f,
            open_meteo_f=open_meteo_f, current_obs_f=current_obs_f,
            lead_hours=lead_h or 0, trend_f_per_hr=trend_f,
            model_forecasts=model_forecasts,
        )
        shadow_p_yes = _p_above_strike(shadow_point, strike_f, shadow_sigma)

        ya = to_frac(m.get("yes_ask_dollars") or m.get("yes_ask"))
        yb = to_frac(m.get("yes_bid_dollars") or m.get("yes_bid"))
        na = to_frac(m.get("no_ask_dollars")  or m.get("no_ask"))
        nb = to_frac(m.get("no_bid_dollars")  or m.get("no_bid"))
        edge = (p_yes - ya) if ya is not None else None
        # Keep both raw + blended in indicators dict for observability.
        ws = WeatherSample(
            sample_at=now_iso, city=city_key,
            market_ticker=m.get("ticker", ""),
            event_ticker=m.get("event_ticker", ""),
            title=(m.get("title") or "")[:200],
            close_time=m.get("_close_iso", ""),
            seconds_to_close=m["_seconds_to_close"],
            strike_f=strike_f,
            yes_ask=ya, yes_bid=yb, no_ask=na, no_bid=nb,
            nws_forecast_f=point_f, forecast_lead_hours=lead_h,
            nws_p_yes=p_yes, market_p_yes=ya, edge=edge,
        )
        # Attach blend metadata as extra fields on the asdict'd record.
        # We return raw dicts (not WeatherSample) so downstream paper +
        # persistence layers can read the new fields without dataclass
        # churn. run_signal_cycle / persist_samples handle dicts already.
        d = asdict(ws)
        d["blend_meta"] = blend_meta
        d["raw_nws_forecast_f"] = nws_forecast_f
        d["open_meteo_f"] = open_meteo_f
        d["current_obs_f"] = current_obs_f
        # Shadow trend-aware decision (logged, not acted on) for the A/B.
        # The realized-trend fields are the KEY discriminator under test: when
        # the forecast disagrees with the obs, is the temperature actually
        # MOVING toward the forecast (→ forecast right, flip was correct) or
        # sitting flat while the forecast over/under-predicts (→ obs right, keep
        # the cheap-NO bet)? Entry features alone can't separate the two; the
        # realized obs trend is the one feature that might. Forward data tells
        # us whether `trend_confirms` cleanly splits should-flip from should-keep.
        obs_tr = trend_meta.get("obs_trend_f_per_hr")
        fc_blend_pt = shadow_meta.get("point_f")
        trend_confirms = None
        if obs_tr is not None and current_obs_f is not None and nws_forecast_f is not None:
            need_dir = nws_forecast_f - current_obs_f   # >0: temp must rise to hit forecast
            trend_confirms = (need_dir >= 0) == (obs_tr >= 0) and abs(obs_tr) >= 0.5
        d["shadow_trendaware"] = {
            "point_f": round(shadow_point, 2),
            "sigma_f": round(shadow_sigma, 2),
            "p_yes": round(shadow_p_yes, 4),
            "trend_f_per_hr": trend_meta.get("trend_f_per_hr"),
            "obs_trend_f_per_hr": obs_tr,
            "fc_trend_f_per_hr": trend_meta.get("fc_trend_f_per_hr"),
            "obs_window_hr": trend_meta.get("obs_window_hr"),
            "trend_confirms": trend_confirms,
            "fc_obs_gap_f": shadow_meta.get("fc_obs_gap_f"),
            "trend_gate": shadow_meta.get("trend_gate"),
            # would the gated trend-aware model have taken the OTHER side?
            "differs_from_live": (shadow_p_yes >= 0.5) != (p_yes >= 0.5),
        }
        samples.append(d)
    return samples


def sample_signals() -> list[dict]:
    out: list[dict] = []
    for city in CITIES:
        try:
            out.extend(sample_signals_for_city(city))
        except Exception as e:
            log_event("weather_signal", "city_sample_failed",
                      {"city": city, "error": str(e)[:200]}, result="degraded")
    out.sort(key=lambda s: -abs(s.get("edge") or 0))  # biggest-edge first
    return out


def persist_samples(samples: list[dict]) -> None:
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_PATH, "a") as f:
        for s in samples:
            f.write(json.dumps(s) + "\n")
    # Bounded retention — signal logs are diagnostic tails; keep them from
    # growing without limit (they had hit 40MB and feed the dashboard readers).
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
            from lib.weather_paper import record_paper_trades_from_samples
            new = record_paper_trades_from_samples(samples)
            n_paper = len(new)
        except Exception as e:
            log_event("weather_signal", "paper_record_failed",
                      {"error": str(e)[:200]}, result="degraded")

    settle_summary = {}
    if settle_paper_trades:
        try:
            from lib.weather_paper import settle_paper_trades as _settle
            settle_summary = _settle()
        except Exception as e:
            log_event("weather_signal", "paper_settle_failed",
                      {"error": str(e)[:200]}, result="degraded")

    log_event("weather_signal", "signal_cycle", {
        "n_markets": len(samples),
        "n_cities": len({s.get("city") for s in samples}),
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
