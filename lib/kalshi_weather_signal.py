"""
Kalshi weather sleeve — discovery + forecast-priced sampling.

Parallel to ``kalshi_15min_signal`` but for Kalshi temperature markets
(KXHIGH<CITY> and friends). The mechanic differs from crypto in one key
way: the edge comes from a *weather forecast*, not a Greeks model on a
traded underlying. Each market is a temperature BUCKET ("63-64°",
"65° or above", ...) and we price its fair YES as the blended-forecast
probability that the resolving temperature lands in that bucket.

Pipeline per cycle:
  1. For every enabled city's series, discover open bucket markets
     (events → markets, same two-hop shape as the crypto sleeve).
  2. Fetch + blend the forecast ONCE per city (all that city's buckets
     share one forecast).
  3. Price each bucket → fair_yes, edge vs market.
  4. Persist samples to data/kalshi_weather_signal.jsonl.

Auth: discovery + sampling are PUBLIC (no key). Real orders (Phase 3)
ride the kalshi_auth signing path, gated behind live_migration_approved.

NETWORK NOTE: hits both Kalshi and the weather providers — blocked in the
dev sandbox, runs on the host cron.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from tradingcore.audit import log_event
except Exception:  # pragma: no cover
    def log_event(*_a, **_k):
        return None

from lib.weather_forecast import fetch_all_sources, forecast_bucket_fair_value

SIGNAL_PATH = Path(__file__).parent.parent / "data" / "kalshi_weather_signal.jsonl"
KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "kalshi_weather.yaml"


def load_config() -> dict:
    """Read the weather registry YAML. Empty-ish dict on failure so the
    caller degrades gracefully (logs + skips the cycle)."""
    import yaml
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        log_event("kalshi_weather", "config_load_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return {}


def enabled_cities(cfg: dict) -> dict:
    return {
        k: v for k, v in (cfg.get("cities") or {}).items()
        if v and v.get("enabled") and v.get("lat") is not None
        and v.get("lon") is not None
    }


def _match_city(text: str, cities: dict) -> str | None:
    """Identify which registry city a series/title belongs to by alias."""
    up = (text or "").upper()
    for key, c in cities.items():
        for alias in (c.get("aliases") or []):
            if alias and alias.upper() in up:
                return key
    return None


def _as_price(m: dict, key: str) -> float | None:
    """Field-aware Kalshi price: prefer the canonical *_dollars float;
    otherwise the cents integer always divides by 100."""
    dollars = m.get(key + "_dollars")
    if dollars is not None:
        try:
            return float(dollars)
        except (ValueError, TypeError):
            return None
    cents = m.get(key)
    if cents is None:
        return None
    try:
        return float(cents) / 100.0
    except (ValueError, TypeError):
        return None


@dataclass
class WeatherSample:
    """One snapshot of one Kalshi temperature bucket, with its forecast
    fair value and the edge vs the market."""
    sample_at: str
    city: str
    label: str
    series_ticker: str
    market_ticker: str
    event_ticker: str
    title: str
    floor_strike: float | None
    cap_strike: float | None
    daily_high: bool
    close_time: str
    seconds_to_close: float
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    # Forecast-derived
    fair_yes: float | None
    edge_yes: float | None       # fair_yes - yes_ask (how underpriced YES is)
    edge_no: float | None        # (1-fair_yes) - no_ask (how underpriced NO is)
    forecast_mu: float | None
    forecast_sigma: float | None
    n_sources: int
    per_source: dict = field(default_factory=dict)


# ── Discovery ────────────────────────────────────────────────────────

def discover_city_markets(series_ticker: str) -> list[dict]:
    """Open bucket markets for one weather series (events → markets).

    Returns raw Kalshi market dicts (augmented with event_ticker), open
    status only. Empty list on any failure (logged, non-fatal).
    """
    import requests

    try:
        r = requests.get(
            f"{KALSHI_HOST}/events",
            params={"series_ticker": series_ticker, "status": "open", "limit": 50},
            timeout=15,
        )
        r.raise_for_status()
        events = r.json().get("events", []) or []
    except Exception as e:
        log_event("kalshi_weather", "events_fetch_failed",
                  {"series": series_ticker, "error": str(e)[:200]},
                  result="degraded")
        return []

    out: list[dict] = []
    for ev in events:
        et = ev.get("event_ticker", "")
        if not et:
            continue
        try:
            mr = requests.get(
                f"{KALSHI_HOST}/markets",
                params={"event_ticker": et, "status": "open"},
                timeout=15,
            )
            mr.raise_for_status()
            markets = mr.json().get("markets", []) or []
        except Exception:
            continue
        for m in markets:
            if m.get("status") != "active":
                continue
            m["_event_ticker"] = et
            m["_event_title"] = ev.get("title", "")
            out.append(m)
    return out


def _bucket_strikes(m: dict) -> tuple[float | None, float | None]:
    """Extract (floor, cap) °F for a Kalshi temperature bucket.

    Kalshi uses floor_strike / cap_strike with a strike_type. One-sided
    buckets leave one end None:
      * greater / greater_or_equal → "X or above"  → (floor, None)
      * less / less_or_equal       → "X or below"  → (None, cap)
      * between/bucket             → "X to Y"       → (floor, cap)
    """
    floor = m.get("floor_strike")
    cap = m.get("cap_strike")
    st = str(m.get("strike_type", "")).lower()

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    floor, cap = _f(floor), _f(cap)
    if "greater" in st:
        return floor, None
    if "less" in st:
        # Some payloads put the threshold in floor_strike for less-than;
        # prefer cap, fall back to floor.
        return None, cap if cap is not None else floor
    return floor, cap


def sample_city(city_key: str, city_cfg: dict, params: dict) -> list[WeatherSample]:
    """Discover + price every open bucket for one city. Forecast fetched
    once and reused across all of the city's buckets."""
    # A city can map to several series (the registry lists series
    # globally); sample_all pre-resolves which series belong to this city.
    markets = []
    for s in params.get("_series_for_city", {}).get(city_key, []):
        markets.extend(discover_city_markets(s))
    if not markets:
        return []

    sources = fetch_all_sources(
        float(city_cfg["lat"]), float(city_cfg["lon"]),
        sources=params.get("sources"),
    )
    if not sources:
        log_event("kalshi_weather", "no_forecast_sources",
                  {"city": city_key}, result="degraded")
        # Still emit samples (market data) but without fair value.
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    out: list[WeatherSample] = []

    for m in markets:
        close_iso = m.get("close_time") or ""
        try:
            close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        seconds_to_close = (close_dt - now).total_seconds()
        if seconds_to_close < -60:
            continue

        floor, cap = _bucket_strikes(m)
        title = str(m.get("title", ""))
        daily_high = "HIGH" in title.upper() or "HIGH" in str(
            m.get("_event_title", "")).upper()

        fair = edge_yes = edge_no = mu = sigma = None
        n_src = 0
        per_source: dict = {}
        if sources and (floor is not None or cap is not None):
            res = forecast_bucket_fair_value(
                sources, target_time=close_dt,
                floor=floor, cap=cap, daily_high=daily_high,
            )
            if res is not None:
                fair, blended = res
                mu, sigma = blended.mu, blended.sigma
                n_src = blended.n_sources
                per_source = blended.per_source
                yes_ask = _as_price(m, "yes_ask")
                no_ask = _as_price(m, "no_ask")
                if yes_ask is not None:
                    edge_yes = round(fair - yes_ask, 4)
                if no_ask is not None:
                    edge_no = round((1.0 - fair) - no_ask, 4)

        out.append(WeatherSample(
            sample_at=now_iso,
            city=city_key,
            label=city_cfg.get("label", city_key),
            series_ticker=m.get("series_ticker", "") or "",
            market_ticker=m.get("ticker", ""),
            event_ticker=m.get("_event_ticker", ""),
            title=title[:200],
            floor_strike=floor,
            cap_strike=cap,
            daily_high=daily_high,
            close_time=close_iso,
            seconds_to_close=round(seconds_to_close, 2),
            yes_bid=_as_price(m, "yes_bid"),
            yes_ask=_as_price(m, "yes_ask"),
            no_bid=_as_price(m, "no_bid"),
            no_ask=_as_price(m, "no_ask"),
            last_price=_as_price(m, "last_price"),
            fair_yes=round(fair, 4) if fair is not None else None,
            edge_yes=edge_yes,
            edge_no=edge_no,
            forecast_mu=round(mu, 2) if mu is not None else None,
            forecast_sigma=round(sigma, 2) if sigma is not None else None,
            n_sources=n_src,
            per_source={k: round(v, 1) for k, v in per_source.items()},
        ))
    return out


def sample_all() -> list[WeatherSample]:
    """Sweep every enabled city across every configured weather series."""
    cfg = load_config()
    cities = enabled_cities(cfg)
    if not cities:
        return []
    params = dict(cfg.get("params") or {})

    # Map each configured series to the city it belongs to (by alias), so
    # sample_city only discovers that city's series.
    all_series = cfg.get("series") or []
    series_for_city: dict[str, list[str]] = {k: [] for k in cities}
    for s in all_series:
        ck = _match_city(s, cities)
        if ck:
            series_for_city[ck].append(s)
    params["_series_for_city"] = series_for_city

    out: list[WeatherSample] = []
    for city_key, city_cfg in cities.items():
        if not series_for_city.get(city_key):
            continue
        try:
            out.extend(sample_city(city_key, city_cfg, params))
        except Exception as e:
            log_event("kalshi_weather", "city_sample_failed",
                      {"city": city_key, "error": str(e)[:200]},
                      result="degraded")
    out.sort(key=lambda s: s.seconds_to_close)
    return out


def persist_samples(samples: list[WeatherSample]) -> None:
    if not samples:
        return
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_PATH, "a") as f:
        for s in samples:
            f.write(json.dumps(asdict(s)) + "\n")


def run_signal_cycle(*, record_paper_trades: bool = True,
                     settle_paper_trades: bool = True) -> dict:
    """Full sweep: discover + price + persist + (paper record + settle)."""
    samples = sample_all()
    persist_samples(samples)

    n_opened = 0
    if record_paper_trades and samples:
        try:
            from lib.kalshi_weather_paper import record_paper_trades_from_samples
            new = record_paper_trades_from_samples([asdict(s) for s in samples])
            n_opened = len(new)
        except Exception as e:
            log_event("kalshi_weather", "paper_record_failed",
                      {"error": str(e)[:200]}, result="degraded")

    settle_summary = {}
    if settle_paper_trades:
        try:
            from lib.kalshi_weather_paper import settle_paper_trades as _settle
            settle_summary = _settle()
        except Exception as e:
            log_event("kalshi_weather", "paper_settle_failed",
                      {"error": str(e)[:200]}, result="degraded")

    by_city: dict[str, int] = {}
    for s in samples:
        by_city[s.city] = by_city.get(s.city, 0) + 1

    log_event("kalshi_weather", "signal_cycle", {
        "n_markets": len(samples),
        "n_cities": len(by_city),
        "cities": sorted(by_city),
        "paper_trades_opened": n_opened,
        "paper_settled": settle_summary.get("settled_now", 0),
    })
    return {
        "n_markets": len(samples),
        "n_cities": len(by_city),
        "by_city": by_city,
        "paper_trades_opened": n_opened,
        "settle_summary": settle_summary,
        "samples": [asdict(s) for s in samples],
    }
