"""Paper-trade recorder for the Kalshi DAILY max/min weather strategy.

Pilot scope (2026-05-28): paper only. Mirrors the hourly weather_paper.py
gate structure but with:

  - Conservative defaults (smaller per-trade cap, wider buffer to weed
    out boundary cases since 24h forecasts have wider σ than 1h).
  - No live execution path — even if `live: true` is configured
    elsewhere, this module never places real orders. Once the pilot
    shows real edge, we can wire kalshi_live_executor in a follow-up.
  - Settlement reads Kalshi market resolution directly (status="settled"
    + result field), so we don't have to re-fetch NWS daily summaries.

Config file: config/weather_daily_strategy.yaml (created if missing on
first call to _load_overrides). Hermes_weather won't touch this — the
pilot stays manual until we have enough data to justify auto-tuning.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tradingcore import log_event

ROOT = Path(__file__).resolve().parent.parent
PAPER_LOG = ROOT / "data" / "weather_daily_paper.jsonl"
STRATEGY_PATH = ROOT / "config" / "weather_daily_strategy.yaml"

# ── Conservative defaults for the pilot ───────────────────────────────
DEFAULT_BANKROLL = 1000.0
DEFAULT_MIN_TRADE_USD = 1.0
DEFAULT_MAX_TRADE_USD = 5.0           # smaller than hourly ($7.5) — longer hold
DEFAULT_KELLY_MULTIPLIER = 0.25       # quarter-Kelly
MIN_EDGE_THRESHOLD = 0.10             # 10pp
MAX_FILL_FOR_BUY = 0.45
EXTREME_PRICE_FLOOR = 0.05
EXTREME_PRICE_CEIL = 0.95
# Forecast-direction gate. Daily forecasts have wider σ (~3-4°F vs 1-3°F
# hourly), so we need a bigger gap before taking the bet. Starting at
# 2°F for NO and 3°F for YES (mirrors the YES-tighter pattern from the
# hourly module that was validated by 16-trade backtest).
DEFAULT_FORECAST_BUFFER_F = 2.0
DEFAULT_FORECAST_BUFFER_F_YES = 3.0
KALSHI_PROFIT_FEE = 0.07


def _load_overrides() -> dict:
    if not STRATEGY_PATH.exists():
        return {}
    try:
        import yaml
        with open(STRATEGY_PATH) as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _effective_params() -> dict:
    o = _load_overrides()
    return {
        "min_edge_threshold":     float(o.get("min_edge_threshold",     MIN_EDGE_THRESHOLD)),
        "max_fill_for_buy":       float(o.get("max_fill_for_buy",       MAX_FILL_FOR_BUY)),
        "max_trade_usd":          float(o.get("default_max_trade_usd",  DEFAULT_MAX_TRADE_USD)),
        "kelly_multiplier":       float(o.get("default_kelly_multiplier", DEFAULT_KELLY_MULTIPLIER)),
        "forecast_buffer_f":      float(o.get("forecast_buffer_f",      DEFAULT_FORECAST_BUFFER_F)),
        "forecast_buffer_f_yes":  float(o.get("forecast_buffer_f_yes",  DEFAULT_FORECAST_BUFFER_F_YES)),
        "no_side_only":           bool(o.get("no_side_only", False)),
    }


@dataclass
class DailyWeatherPaperTrade:
    trade_id: str
    city_key: str
    direction: str
    market_ticker: str
    event_ticker: str
    title: str
    side: str
    fill_price: float
    our_size: float            # contracts
    notional: float            # USD at fill
    strike_f: float
    forecast_f: float
    nws_p_yes: float
    market_p_yes: float
    edge: float
    close_time: str
    opened_at: str
    status: str                # "open" | "won" | "lost" | "void"
    resolved_at: str
    paper_pnl: float
    kelly_fraction: float
    half_kelly_fraction: float
    # Schema/version tag for the ENTRY logic that produced this record. Records
    # written before the strike-type fix (2026-05-31) were stamped
    # "pre_strike_type_fix" by the backfill and must be excluded from go-forward
    # WR validation — their entry side was unreliable (less-type prob inverted,
    # between buckets dropped). New records carry the post-fix tag below.
    # 2026-06-01: bumped to "blended_v2" when the daily sleeve adopted the
    # NWS+Open-Meteo blended engine (ensemble σ, observation anchor, per-city
    # calibration). Validation should bucket blended_v2 separately from the
    # earlier strike_type_aware_v1 (NWS-only) records.
    entry_schema: str = "blended_v2"


def _kelly_size(*, p_win: float, fill: float, bankroll: float,
                multiplier: float, floor: float, cap: float):
    """Half-Kelly with floor + cap. fill ∈ (0,1). Returns (notional, meta)."""
    if fill <= 0 or fill >= 1:
        return 0.0, {"kelly_fraction": 0.0, "scaled": 0.0}
    payoff_odds = (1 - fill) / fill
    if payoff_odds <= 0:
        return 0.0, {"kelly_fraction": 0.0, "scaled": 0.0}
    edge = p_win * payoff_odds - (1 - p_win)
    kelly_frac = edge / payoff_odds if payoff_odds > 0 else 0.0
    if kelly_frac <= 0:
        return 0.0, {"kelly_fraction": kelly_frac, "scaled": 0.0}
    scaled = max(0.0, kelly_frac * multiplier)
    notional = max(0.0, scaled * bankroll)
    notional = min(notional, cap)
    notional = max(notional, floor) if notional > 0 else 0.0
    return round(notional, 4), {
        "kelly_fraction": round(kelly_frac, 4),
        "scaled": round(scaled, 4),
    }


# ── Atomic JSONL append + load ────────────────────────────────────────

def _load_all() -> list[dict]:
    if not PAPER_LOG.exists():
        return []
    out = []
    try:
        with open(PAPER_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _append(rec: dict) -> None:
    PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(PAPER_LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _rewrite_all(records: list[dict]) -> None:
    PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAPER_LOG.with_suffix(PAPER_LOG.suffix + ".tmp")
    with open(tmp, "w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    tmp.replace(PAPER_LOG)


# ── Recording new trades ──────────────────────────────────────────────

def record_paper_trades_from_samples(samples: list[dict]) -> list[DailyWeatherPaperTrade]:
    if not samples:
        return []
    existing = _load_all()
    open_tickers = {r["market_ticker"] for r in existing if r.get("status") == "open"}
    now_iso = datetime.now(timezone.utc).isoformat()
    params = _effective_params()
    new_trades: list[DailyWeatherPaperTrade] = []
    skip_counts: dict[str, int] = {}

    for s in samples:
        ticker = s.get("market_ticker", "")
        if not ticker or ticker in open_tickers:
            skip_counts["dup_open"] = skip_counts.get("dup_open", 0) + 1
            continue
        nws_p = s.get("nws_p_yes")
        market_p = s.get("market_p_yes")
        if nws_p is None or market_p is None:
            skip_counts["missing_data"] = skip_counts.get("missing_data", 0) + 1
            continue
        edge = nws_p - market_p
        if abs(edge) < params["min_edge_threshold"]:
            skip_counts["edge_too_small"] = skip_counts.get("edge_too_small", 0) + 1
            continue

        # Side selection
        if edge > 0:
            side, fill, p_win = "YES", s.get("yes_ask"), nws_p
        else:
            side, fill, p_win = "NO", s.get("no_ask"), 1 - nws_p

        # Forecast-direction gate (asymmetric buffer for daily forecasts).
        # Keyed off the STRIKE-TYPE-AWARE signed margin from the signal module
        # (yes_margin_f > 0 => forecast sits inside the YES zone). The old gate
        # hard-coded "YES = temp >= strike", which silently inverted every
        # 'less'-type market and dropped 'between' buckets — the root cause of
        # the fake +0.97 edges and the 3W/14L paper bleed.
        forecast_f = float(s.get("forecast_f") or 0)
        strike_f = float(s.get("strike_f") or 0)
        yes_margin = s.get("yes_margin_f")
        buf_no = params["forecast_buffer_f"]
        buf_yes = params["forecast_buffer_f_yes"]
        if yes_margin is None:
            # No type-aware margin (older sample schema or unknown type) — do
            # NOT fall back to the buggy >= comparison; skip rather than guess.
            skip_counts["no_margin"] = skip_counts.get("no_margin", 0) + 1
            continue
        yes_margin = float(yes_margin)
        if side == "YES":
            # Require the forecast to clear the YES zone by buf_yes °F.
            if yes_margin < buf_yes:
                skip_counts["forecast_dir_yes"] = skip_counts.get("forecast_dir_yes", 0) + 1
                continue
        else:
            # NO side: forecast must sit at least buf_no °F INSIDE the NO zone
            # (i.e. margin-toward-yes more negative than -buf_no).
            if yes_margin > -buf_no:
                skip_counts["forecast_dir_no"] = skip_counts.get("forecast_dir_no", 0) + 1
                continue

        # YES disable (defensive — daily YES may have same issue as hourly)
        if side == "YES" and params["no_side_only"]:
            skip_counts["yes_disabled"] = skip_counts.get("yes_disabled", 0) + 1
            continue

        if fill is None and side == "NO" and s.get("yes_ask") is not None:
            fill = round(1.0 - float(s["yes_ask"]), 4)
        if fill is None:
            skip_counts["no_fill"] = skip_counts.get("no_fill", 0) + 1
            continue
        fill = float(fill)
        if not (EXTREME_PRICE_FLOOR <= fill <= EXTREME_PRICE_CEIL):
            skip_counts["extreme_price"] = skip_counts.get("extreme_price", 0) + 1
            continue
        if fill > params["max_fill_for_buy"]:
            skip_counts["fill_too_high"] = skip_counts.get("fill_too_high", 0) + 1
            continue

        notional, meta = _kelly_size(
            p_win=p_win, fill=fill, bankroll=DEFAULT_BANKROLL,
            multiplier=params["kelly_multiplier"],
            floor=DEFAULT_MIN_TRADE_USD, cap=params["max_trade_usd"],
        )
        if notional <= 0:
            skip_counts["kelly_no_edge"] = skip_counts.get("kelly_no_edge", 0) + 1
            continue
        contracts = round(notional / fill, 4)

        trade = DailyWeatherPaperTrade(
            trade_id=f"{ticker}_{int(datetime.now(timezone.utc).timestamp())}",
            city_key=s.get("city_key", "?"),
            direction=s.get("direction", "?"),
            market_ticker=ticker,
            event_ticker=s.get("event_ticker", ""),
            title=s.get("title", "")[:200],
            side=side,
            fill_price=round(fill, 4),
            our_size=contracts,
            notional=round(notional, 4),
            strike_f=strike_f,
            forecast_f=forecast_f,
            nws_p_yes=round(float(nws_p), 4),
            market_p_yes=round(float(market_p), 4),
            edge=round(float(edge), 4),
            close_time=str(s.get("close_time", "")),
            opened_at=now_iso,
            status="open",
            resolved_at="",
            paper_pnl=0.0,
            kelly_fraction=meta["kelly_fraction"],
            half_kelly_fraction=meta["scaled"],
        )
        rec = asdict(trade)
        # Keep the RAW NWS forecast (pre-bias, pre-blend) so settlement can feed
        # per-city calibration the raw forecast error. Calibrating on the
        # bias-corrected blend would drive bias_f → 0 and silently disable the
        # correction (feedback loop). Falls back to the blended point if absent.
        rec["nws_forecast_f"] = s.get("nws_forecast_f", forecast_f)
        _append(rec)
        new_trades.append(trade)

    log_event("weather_daily_paper", "record_cycle", {
        "samples": len(samples),
        "new_trades": len(new_trades),
        "skip_counts": skip_counts,
    })
    return new_trades


# ── Settlement against Kalshi resolution ──────────────────────────────

def _kalshi_market_result(ticker: str) -> str | None:
    """Hit Kalshi /markets/{ticker} and return result ∈ {"yes","no",""}
    when settled, else None for still-open. No auth needed for public
    market endpoints."""
    import requests
    try:
        r = requests.get(
            f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}",
            timeout=10,
        )
        if r.status_code != 200:
            return None
        m = r.json().get("market") or {}
        status = m.get("status", "")
        if status not in ("settled", "finalized"):
            return None
        result = m.get("result", "").lower()
        return result if result in ("yes", "no") else None
    except Exception:
        return None


def _record_daily_calibration(rec: dict, om_cache: dict) -> None:
    """On settle, record (raw NWS forecast, actual observed extreme) for the
    trade's city so lib.weather_calibration learns per-city bias + σ. Uses
    Open-Meteo's realized daily max/min for the resolved date (it carries past
    days). Calibrates on the RAW NWS forecast, not the bias-corrected blend, so
    bias_f stays meaningful. Best-effort — any miss is a silent no-op and must
    never block settlement."""
    from lib.weather_daily_signal import (
        DAILY_CITIES, _fetch_open_meteo_daily, _parse_event_date)
    from lib.weather_calibration import record_error
    city_key = rec.get("city_key")
    cfg = DAILY_CITIES.get(city_key)
    raw_fc = rec.get("nws_forecast_f")
    if cfg is None or raw_fc is None:
        return
    obs_date = _parse_event_date(rec.get("event_ticker", ""))
    if obs_date is None:
        return
    om = om_cache.get(city_key)
    if om is None:
        om = _fetch_open_meteo_daily(cfg["lat"], cfg["lon"])
        om_cache[city_key] = om
    actual = (om.get("daily") or {}).get(
        obs_date.isoformat(), {}).get(cfg["direction"])
    if actual is None:
        return
    record_error(city_key, float(raw_fc), float(actual))


def settle_paper_trades() -> dict:
    """Walk all open trades; resolve any whose Kalshi market has settled.
    Also voids any trade whose close_time was >36h ago and still hasn't
    settled (freeing budget for new trades)."""
    records = _load_all()
    if not records:
        return {"settled_now": 0, "total_open": 0}
    now = datetime.now(timezone.utc)
    settled_now = 0
    voided_now = 0
    pnl_this_cycle = 0.0
    _om_cache: dict = {}   # city_key -> Open-Meteo data, fetched once per cycle
    for rec in records:
        if rec.get("status") != "open":
            continue
        ticker = rec.get("market_ticker", "")
        if not ticker:
            continue
        # Void trades older than 36h with no settlement to free up budget.
        try:
            ct = datetime.fromisoformat(rec["close_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            ct = None
        if ct is not None and now > ct + timedelta(hours=36):
            rec["status"] = "void"
            rec["resolved_at"] = now.isoformat()
            rec["paper_pnl"] = 0.0
            voided_now += 1
            continue
        # Don't even hit Kalshi until close_time has passed.
        if ct is not None and now < ct:
            continue
        result = _kalshi_market_result(ticker)
        if result is None:
            continue
        side = rec.get("side", "")
        fill = float(rec.get("fill_price") or 0)
        size = float(rec.get("our_size") or 0)
        won = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
        if won:
            # Payoff = $1 per contract minus the fill we paid; minus Kalshi profit fee.
            gross = size * (1.0 - fill)
            fee = max(0.0, gross * KALSHI_PROFIT_FEE)
            pnl = gross - fee
            rec["status"] = "won"
        else:
            pnl = -float(rec.get("notional") or 0)
            rec["status"] = "lost"
        rec["resolved_at"] = now.isoformat()
        rec["paper_pnl"] = round(pnl, 4)
        rec["kalshi_result"] = result
        # Feed per-city calibration the raw forecast error (best-effort).
        try:
            _record_daily_calibration(rec, _om_cache)
        except Exception:
            pass
        settled_now += 1
        pnl_this_cycle += pnl

    _rewrite_all(records)

    total_open = sum(1 for r in records if r.get("status") == "open")
    log_event("weather_daily_paper", "settle_cycle", {
        "settled_now": settled_now,
        "voided_now": voided_now,
        "total_open": total_open,
        "pnl_this_cycle": round(pnl_this_cycle, 4),
    })
    return {
        "settled_now": settled_now,
        "voided_now": voided_now,
        "total_open": total_open,
        "paper_pnl_this_cycle": round(pnl_this_cycle, 4),
    }
