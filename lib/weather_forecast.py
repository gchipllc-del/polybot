"""
Multi-source weather forecast blending + temperature-bucket probability.

This is the *signal core* for the Kalshi weather sleeve. It has two jobs:

  1. Pull an hourly temperature forecast for a (lat, lon) from several
     providers and BLEND them into a consensus, carrying the cross-source
     disagreement as part of the forecast uncertainty.

  2. Turn a consensus forecast (mu, sigma) into the fair YES price of a
     Kalshi temperature bucket — i.e. P(observed temp lands in
     [floor, cap]) — which is what we compare against the market.

Sources (blended when available):
  * NWS   — api.weather.gov, free, US-only, NO key. Default.
  * Open-Meteo — api.open-meteo.com, free, global, NO key. Default.
  * OpenWeather — One Call 3.0, needs OPENWEATHER_API_KEY in env. Optional.

Design notes:
  * Every network call is defensive: timeouts, try/except, and a None
    return on any failure. A dead provider just drops out of the blend;
    the consensus uses whatever sources answered.
  * Pure math (blending, bucket CDF) is separated from I/O so it can be
    unit-tested without the network — see tests/test_kalshi_weather.py.
  * Temperatures are normalized to FAHRENHEIT everywhere (Kalshi temp
    markets are stated in °F).

NETWORK / HOST NOTE: this module reaches external weather APIs, which
are blocked in the CI/dev sandbox. It runs on the host where the cron
lives; allowlist api.weather.gov + api.open-meteo.com there.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

try:  # audit log is part of the shared core; tolerate its absence in tests
    from tradingcore.audit import log_event
except Exception:  # pragma: no cover - exercised only when core is missing
    def log_event(*_a, **_k):
        return None


# Base forecast error (°F, 1σ) for a same-day/next-hour high-temp
# forecast, before adding cross-source disagreement. NWS day-1 high-temp
# MAE is ~2-3°F; we use a slightly fat 3.0 as a prudent prior. The
# blender ADDS inter-source spread on top, so well-disagreeing forecasts
# automatically widen the distribution (and shrink our claimed edge).
DEFAULT_BASE_SIGMA_F = 3.0

# Per-lead-hour error growth. A forecast 12h out is less certain than one
# 1h out. sigma = base + slope * hours_out, capped. Rough, tunable.
SIGMA_PER_HOUR_F = 0.15
MAX_SIGMA_F = 8.0

_USER_AGENT = "polybot-weather-sleeve (contact: set CONTACT_EMAIL in env)"


@dataclass
class HourlyForecast:
    """A provider's hourly temperature series, normalized.

    ``points`` is an ordered list of ``(utc_dt, temp_f)``. Helpers pick
    the temp at a target time or the max over a window — the two shapes
    Kalshi temperature markets resolve on (hourly reading vs daily high).
    """
    source: str
    points: list[tuple[datetime, float]] = field(default_factory=list)

    def temp_at(self, target: datetime) -> float | None:
        """Temp at the hour closest to ``target`` (within 90 min)."""
        if not target.tzinfo:
            target = target.replace(tzinfo=timezone.utc)
        best: tuple[float, float] | None = None  # (abs_seconds, temp)
        for dt, t in self.points:
            diff = abs((dt - target).total_seconds())
            if diff <= 5400 and (best is None or diff < best[0]):
                best = (diff, t)
        return best[1] if best else None

    def max_through(self, end: datetime, *, start: datetime | None = None) -> float | None:
        """Highest forecast temp from ``start`` (default now) through ``end``.

        Used for "daily high" markets, where the contract resolves on the
        max temperature observed over the day rather than a point reading.
        """
        if not end.tzinfo:
            end = end.replace(tzinfo=timezone.utc)
        if start is None:
            start = datetime.now(timezone.utc)
        vals = [t for dt, t in self.points if start <= dt <= end]
        return max(vals) if vals else None


# ── Providers (I/O) ──────────────────────────────────────────────────

def _fetch_nws(lat: float, lon: float, *, timeout: int = 15) -> HourlyForecast | None:
    """NWS hourly forecast. Two hops: /points → forecastHourly URL."""
    import requests

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/geo+json"}
    try:
        pr = requests.get(
            f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}",
            headers=headers, timeout=timeout,
        )
        pr.raise_for_status()
        hourly_url = pr.json()["properties"]["forecastHourly"]
        fr = requests.get(hourly_url, headers=headers, timeout=timeout)
        fr.raise_for_status()
        periods = fr.json()["properties"]["periods"]
    except Exception as e:
        log_event("weather", "nws_fetch_failed",
                  {"lat": lat, "lon": lon, "error": str(e)[:200]},
                  result="degraded")
        return None

    points: list[tuple[datetime, float]] = []
    for p in periods:
        try:
            t = float(p["temperature"])
            if str(p.get("temperatureUnit", "F")).upper().startswith("C"):
                t = t * 9.0 / 5.0 + 32.0
            dt = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
            points.append((dt.astimezone(timezone.utc), t))
        except (KeyError, ValueError, TypeError):
            continue
    return HourlyForecast("nws", points) if points else None


def _fetch_open_meteo(lat: float, lon: float, *, timeout: int = 15) -> HourlyForecast | None:
    """Open-Meteo hourly 2m temperature, requested directly in °F/UTC."""
    import requests

    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "temperature_2m",
                "temperature_unit": "fahrenheit",
                "timezone": "UTC",
                "forecast_days": 2,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        h = r.json()["hourly"]
        times, temps = h["time"], h["temperature_2m"]
    except Exception as e:
        log_event("weather", "open_meteo_fetch_failed",
                  {"lat": lat, "lon": lon, "error": str(e)[:200]},
                  result="degraded")
        return None

    points: list[tuple[datetime, float]] = []
    for ts, t in zip(times, temps):
        if t is None:
            continue
        try:
            dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            points.append((dt, float(t)))
        except (ValueError, TypeError):
            continue
    return HourlyForecast("open_meteo", points) if points else None


def _fetch_openweather(lat: float, lon: float, *, timeout: int = 15) -> HourlyForecast | None:
    """OpenWeather One Call 3.0 hourly — only when OPENWEATHER_API_KEY set."""
    key = os.environ.get("OPENWEATHER_API_KEY", "").strip()
    if not key:
        return None
    import requests

    try:
        r = requests.get(
            "https://api.openweathermap.org/data/3.0/onecall",
            params={
                "lat": lat, "lon": lon, "units": "imperial",
                "exclude": "current,minutely,daily,alerts", "appid": key,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        hourly = r.json().get("hourly", [])
    except Exception as e:
        log_event("weather", "openweather_fetch_failed",
                  {"lat": lat, "lon": lon, "error": str(e)[:200]},
                  result="degraded")
        return None

    points: list[tuple[datetime, float]] = []
    for h in hourly:
        try:
            dt = datetime.fromtimestamp(int(h["dt"]), tz=timezone.utc)
            points.append((dt, float(h["temp"])))
        except (KeyError, ValueError, TypeError):
            continue
    return HourlyForecast("openweather", points) if points else None


_PROVIDERS = {
    "nws": _fetch_nws,
    "open_meteo": _fetch_open_meteo,
    "openweather": _fetch_openweather,
}


def fetch_all_sources(
    lat: float, lon: float, *, sources: list[str] | None = None,
) -> list[HourlyForecast]:
    """Fetch every requested provider, dropping any that fail.

    Default source set is the two no-key providers (nws, open_meteo) plus
    openweather, which self-skips unless a key is configured.
    """
    names = sources or ["nws", "open_meteo", "openweather"]
    out: list[HourlyForecast] = []
    for name in names:
        fn = _PROVIDERS.get(name)
        if fn is None:
            continue
        fc = fn(lat, lon)
        if fc is not None and fc.points:
            out.append(fc)
    return out


# ── Blending (pure) ──────────────────────────────────────────────────

@dataclass
class BlendedForecast:
    """Consensus forecast for a single target the market resolves on.

    ``mu`` is the blended temp (°F); ``sigma`` is total uncertainty
    (base forecast error ⊕ cross-source disagreement, grown by lead
    time). ``n_sources`` and ``per_source`` aid auditing/calibration.
    """
    mu: float
    sigma: float
    n_sources: int
    hours_out: float
    per_source: dict[str, float]


def _lead_sigma(hours_out: float) -> float:
    base = DEFAULT_BASE_SIGMA_F + SIGMA_PER_HOUR_F * max(0.0, hours_out)
    return min(MAX_SIGMA_F, base)


def blend_point_forecasts(
    per_source_temp: dict[str, float], *, hours_out: float,
) -> BlendedForecast | None:
    """Blend each source's point temp into a consensus (mu, sigma).

    mu = simple mean of the available sources. sigma combines the
    lead-time base error with the inter-source standard deviation in
    quadrature, so genuine provider disagreement widens the distribution
    (and correctly shrinks any edge we claim). Returns None if no source
    produced a value.
    """
    temps = [t for t in per_source_temp.values() if t is not None]
    if not temps:
        return None
    mu = sum(temps) / len(temps)
    if len(temps) > 1:
        var = sum((t - mu) ** 2 for t in temps) / (len(temps) - 1)
        spread = math.sqrt(var)
    else:
        spread = 0.0
    base = _lead_sigma(hours_out)
    sigma = math.sqrt(base * base + spread * spread)
    return BlendedForecast(
        mu=mu, sigma=sigma, n_sources=len(temps),
        hours_out=hours_out,
        per_source={k: v for k, v in per_source_temp.items() if v is not None},
    )


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bucket_probability(
    *,
    mu: float,
    sigma: float,
    floor: float | None,
    cap: float | None,
) -> float:
    """Fair YES price for a temperature bucket: P(floor ≤ T ≤ cap).

    Models the observed temperature as Normal(mu, sigma). Buckets are in
    whole °F, so a ±0.5 continuity correction is applied at each edge
    (a "63-64°" bucket covers [62.5, 64.5)). One-sided buckets pass
    ``floor=None`` ("X or below") or ``cap=None`` ("X or above").

    Returns a probability in [0, 1]. With sigma ≤ 0 it degenerates to a
    hard indicator (used only in pathological inputs).
    """
    if sigma <= 0:
        lo = -math.inf if floor is None else floor - 0.5
        hi = math.inf if cap is None else cap + 0.5
        return 1.0 if lo <= mu <= hi else 0.0
    lo_p = 0.0 if floor is None else _norm_cdf((floor - 0.5 - mu) / sigma)
    hi_p = 1.0 if cap is None else _norm_cdf((cap + 0.5 - mu) / sigma)
    return max(0.0, min(1.0, hi_p - lo_p))


def forecast_bucket_fair_value(
    sources: list[HourlyForecast],
    *,
    target_time: datetime,
    floor: float | None,
    cap: float | None,
    daily_high: bool,
    window_start: datetime | None = None,
) -> tuple[float, BlendedForecast] | None:
    """End-to-end: from raw provider series to a bucket's fair YES price.

    ``daily_high`` picks the resolution shape: True → each source's max
    temperature from ``window_start`` (default now) through
    ``target_time`` (daily-high markets); False → the point reading at
    ``target_time`` (hourly markets).

    Returns ``(fair_yes, blended)`` or None if no source had data for the
    target.
    """
    now = datetime.now(timezone.utc)
    hours_out = max(0.0, (target_time - now).total_seconds() / 3600.0)
    per_source: dict[str, float] = {}
    for fc in sources:
        val = (
            fc.max_through(target_time, start=window_start)
            if daily_high else fc.temp_at(target_time)
        )
        if val is not None:
            per_source[fc.source] = val
    blended = blend_point_forecasts(per_source, hours_out=hours_out)
    if blended is None:
        return None
    fair = bucket_probability(
        mu=blended.mu, sigma=blended.sigma, floor=floor, cap=cap,
    )
    return fair, blended
