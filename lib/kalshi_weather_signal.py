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
    daily_high: bool             # True → resolves on the day's max temp
    is_hourly: bool              # True → short-window hourly temp market
    window_minutes: float | None  # [open, close] span; drives classification
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


def discover_weather_series(configured: list[str]) -> list[str]:
    """Configured series ∪ Kalshi's live "Climate and Weather" series.

    Auto-discovery means we pick up HOURLY temperature series without
    having to enumerate their exact tickers (which we can't verify from
    the sandbox). Falls back to just the configured list on failure.
    """
    import requests

    series = list(dict.fromkeys(configured or []))  # de-dup, keep order
    try:
        r = requests.get(
            f"{KALSHI_HOST}/series",
            params={"category": "Climate and Weather"}, timeout=15,
        )
        r.raise_for_status()
        for s in r.json().get("series", []) or []:
            t = s.get("ticker") or s.get("series_ticker")
            if t and t not in series:
                series.append(t)
    except Exception as e:
        log_event("kalshi_weather", "series_discovery_failed",
                  {"error": str(e)[:200]}, result="degraded")
    return series


def _window_minutes(m: dict, close_dt: datetime) -> float | None:
    """[open, close] span in minutes, or None if open_time is missing."""
    open_iso = m.get("open_time") or ""
    if not open_iso:
        return None
    try:
        open_dt = datetime.fromisoformat(open_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return (close_dt - open_dt).total_seconds() / 60.0


def _classify_hourly(m: dict, close_dt: datetime, hourly_max_min: float) -> tuple[bool, float | None]:
    """Decide hourly vs daily-high by window length.

    Hourly temperature markets run ~1 hour; daily-high markets run a full
    day. When open_time is missing we fall back to the title: a market
    without "HIGH" wording is treated as hourly.
    """
    wm = _window_minutes(m, close_dt)
    if wm is not None:
        return (wm <= hourly_max_min), wm
    text = (str(m.get("title", "")) + " " + str(m.get("_event_title", ""))).upper()
    return ("HIGH" not in text), None


def _price_market(
    m: dict, city_key: str, city_cfg: dict, sources: list,
    *, hourly_max_min: float, now: datetime, now_iso: str,
) -> WeatherSample | None:
    """Price one Kalshi temperature bucket against the blended forecast."""
    close_iso = m.get("close_time") or ""
    try:
        close_dt = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    seconds_to_close = (close_dt - now).total_seconds()
    if seconds_to_close < -60:
        return None

    is_hourly, window_min = _classify_hourly(m, close_dt, hourly_max_min)
    # Hourly markets resolve on the reading AT the close hour; daily-high
    # markets on the day's max → drives which forecast aggregation we use.
    daily_high = not is_hourly
    floor, cap = _bucket_strikes(m)

    fair = edge_yes = edge_no = mu = sigma = None
    n_src = 0
    per_source: dict = {}
    if sources and (floor is not None or cap is not None):
        # For daily-high markets the resolving value is the day's MAX, so
        # the forecast max must span the whole window from market open
        # (a peak earlier in the day still counts), not just from now.
        open_dt = None
        open_iso = m.get("open_time") or ""
        if open_iso:
            try:
                open_dt = datetime.fromisoformat(open_iso.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                open_dt = None
        res = forecast_bucket_fair_value(
            sources, target_time=close_dt,
            floor=floor, cap=cap, daily_high=daily_high,
            window_start=open_dt if daily_high else None,
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

    return WeatherSample(
        sample_at=now_iso,
        city=city_key,
        label=city_cfg.get("label", city_key),
        series_ticker=m.get("series_ticker", "") or "",
        market_ticker=m.get("ticker", ""),
        event_ticker=m.get("_event_ticker", ""),
        title=str(m.get("title", ""))[:200],
        floor_strike=floor,
        cap_strike=cap,
        daily_high=daily_high,
        is_hourly=is_hourly,
        window_minutes=round(window_min, 1) if window_min is not None else None,
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
    )


def sample_all() -> list[WeatherSample]:
    """Sweep every weather series, tag each market to a city, classify
    hourly-vs-daily, filter to ``market_type``, and price.

    Forecast is fetched ONCE per city and reused across that city's
    markets. A discovered market whose city can't be identified is logged
    (deduped) as ``unmapped_city`` so the registry is easy to extend.
    """
    cfg = load_config()
    cities = enabled_cities(cfg)
    if not cities:
        return []
    params = dict(cfg.get("params") or {})
    market_type = str(params.get("market_type", "hourly")).lower()
    hourly_max_min = float(params.get("hourly_max_minutes", 90))

    series_list = discover_weather_series(cfg.get("series") or [])

    # Discover all markets across all series, tagging each to a city.
    markets_by_city: dict[str, list[dict]] = {k: [] for k in cities}
    unmapped: set[str] = set()
    for s in series_list:
        for m in discover_city_markets(s):
            text = " ".join([
                m.get("series_ticker", "") or s,
                str(m.get("_event_title", "")),
                str(m.get("title", "")),
            ])
            ck = _match_city(text, cities)
            if ck is None:
                unmapped.add(m.get("series_ticker", "") or s)
                continue
            markets_by_city[ck].append(m)
    if unmapped:
        log_event("kalshi_weather", "unmapped_city",
                  {"series": sorted(unmapped)[:20]}, result="degraded")

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    out: list[WeatherSample] = []
    for city_key, city_cfg in cities.items():
        markets = markets_by_city.get(city_key) or []
        if not markets:
            continue
        try:
            sources = fetch_all_sources(
                float(city_cfg["lat"]), float(city_cfg["lon"]),
                sources=params.get("sources"),
            )
            if not sources:
                log_event("kalshi_weather", "no_forecast_sources",
                          {"city": city_key}, result="degraded")
            for m in markets:
                samp = _price_market(
                    m, city_key, city_cfg, sources,
                    hourly_max_min=hourly_max_min, now=now, now_iso=now_iso,
                )
                if samp is None:
                    continue
                if market_type == "hourly" and not samp.is_hourly:
                    continue
                if market_type == "daily" and samp.is_hourly:
                    continue
                out.append(samp)
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
