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
# Mirrors the live Kalshi account so paper sizing matches real-money sizing.
DEFAULT_BANKROLL = 233.0
DEFAULT_MIN_TRADE_USD = 1.0
DEFAULT_MAX_TRADE_USD = 5.0           # smaller than hourly ($7.5) — longer hold
DEFAULT_KELLY_MULTIPLIER = 0.25       # quarter-Kelly
MIN_EDGE_THRESHOLD = 0.10             # 10pp
# Market-disagreement ceiling (the between-market bleed fix, #174). A NO bet
# against a market that prices the YES side meaningfully is almost always us
# FADING the market's superior short-horizon view: the live order book embeds
# fresher forecast info (incl. up-revised highs) than our day-ahead wide-σ
# model, which STRUCTURALLY underprices narrow between-bands. Every loser in the
# settled pre-fix sample was a violent disagreement (we said 2-9% YES, the
# market said 44-95%, and the market was right). When our probability differs
# from the market's by more than this many points, we don't believe our own
# edge — skip. Robust regardless of which internal path corrupted forecast_f.
MAX_DISAGREEMENT_EDGE = 0.40
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
# Close-time guard (#160 follow-up). Never ENTER a market within this many
# seconds of its close (or already closed). The daily sleeve's first live order
# FAILED with HTTP 404 market_not_found because the scanner handed it a market
# that had closed ~11h earlier. 600s = don't open a position that can't even
# outlive the next 10-min launchd cycle. Tunable via `min_seconds_to_close`.
# Daily markets open ~24-30h pre-close, so this only blocks the closed/closing
# tail — never the normal trading window (winners historically led 7-28h).
MIN_SECONDS_TO_CLOSE = 600


def reset_paper(log_path: Path | None = None, include_live: bool = False) -> dict:
    """Zero the DAILY-weather paper ledger. This sleeve is paper-only, but the
    reset is defensive and mirrors the hourly one:

    Default (``include_live=False``): clears paper rows, preserves any
    is_live=true rows (typically none). ``include_live=True`` clears everything
    for a clean-slate model test. The full ledger is archived first (reversible).
    """
    path = Path(log_path) if log_path else PAPER_LOG
    empty = {"cleared_paper_trades": 0, "cleared_paper_pnl": 0.0,
             "cleared_live_trades": 0, "cleared_live_pnl": 0.0,
             "kept_live_trades": 0, "kept_live_pnl": 0.0,
             "archived_to": None, "ledger": str(path)}
    if not path.exists():
        return empty

    rows: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    live = [r for r in rows if bool(r.get("is_live"))]
    paper = [r for r in rows if not bool(r.get("is_live"))]
    paper_pnl = sum(float(r.get("paper_pnl", 0) or 0) for r in paper)
    live_pnl = sum(float(r.get("paper_pnl", 0) or 0) for r in live)

    keep = [] if include_live else live
    to_clear = rows if include_live else paper

    if not to_clear:
        return {**empty, "kept_live_trades": len(live),
                "kept_live_pnl": round(live_pnl, 2), "ledger": str(path)}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = path.with_name(f"{path.stem}.{stamp}.bak.jsonl")
    path.replace(archive)

    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for r in keep:
            f.write(json.dumps(r) + "\n")
    tmp.replace(path)

    return {
        "cleared_paper_trades": len(paper),
        "cleared_paper_pnl": round(paper_pnl, 2),
        "cleared_live_trades": len(live) if include_live else 0,
        "cleared_live_pnl": round(live_pnl, 2) if include_live else 0.0,
        "kept_live_trades": 0 if include_live else len(live),
        "kept_live_pnl": 0.0 if include_live else round(live_pnl, 2),
        "archived_to": str(archive),
        "ledger": str(path),
    }


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
        "max_disagreement_edge":  float(o.get("max_disagreement_edge",  MAX_DISAGREEMENT_EDGE)),
        "min_seconds_to_close":   float(o.get("min_seconds_to_close",   MIN_SECONDS_TO_CLOSE)),
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
    # Live-trade metadata (#160). Populated ONLY when kalshi_live_executor placed
    # a real order; is_live=False rows are paper. Settlement reads live_contracts/
    # live_notional_usd for is_live rows so P&L + kill-switch count REAL fills
    # (not the paper-intended size); a 0-fill live order settles void.
    is_live: bool = False
    live_order_id: str = ""
    live_contracts: int = 0
    live_notional_usd: float = 0.0
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
    # Paper bankroll mirrors the live account (signed balance → config → default),
    # resolved once per cycle so paper sizing tracks the real account size.
    try:
        from lib.account_balance import live_account_balance
        eff_bankroll = live_account_balance()
    except Exception:
        eff_bankroll = DEFAULT_BANKROLL
    # Live execution per-cycle state (#160). committed_* stop over-deploy before
    # Kalshi's balance/positions lag catches up; balance is fetched at most once.
    committed_in_cycle = 0.0
    committed_count_in_cycle = 0
    _live_balance_cache: dict = {}

    for s in samples:
        ticker = s.get("market_ticker", "")
        if not ticker or ticker in open_tickers:
            skip_counts["dup_open"] = skip_counts.get("dup_open", 0) + 1
            continue
        # Close-time guard (#160 follow-up): only ever attempt genuinely OPEN
        # markets. Recompute seconds-to-close at RECORD time (the sample's value
        # can be minutes stale). Skip if closed/closing, or if close_time is
        # unparseable (fail closed — real money). Prevents the HTTP 404
        # market_not_found the first live attempt hit on a market that had
        # closed ~11h earlier. Applies to paper + live alike.
        try:
            _ct = datetime.fromisoformat(
                str(s.get("close_time", "")).replace("Z", "+00:00"))
            _secs_to_close = (_ct - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError):
            _secs_to_close = None
        if _secs_to_close is None or _secs_to_close <= params["min_seconds_to_close"]:
            skip_counts["market_closing_or_closed"] = skip_counts.get("market_closing_or_closed", 0) + 1
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
        # Market-disagreement sanity gate (#174 between-market bleed fix). A huge
        # gap between our probability and the market's is not a huge edge — it is
        # us fading a market that has fresher forecast info. Investigation of the
        # settled sample: every loser was a violent disagreement (we said 2-9%
        # YES on a narrow between-band, the market said 44-95%, and the market
        # was right — the band DID settle in). Our day-ahead wide-σ model
        # structurally underprices narrow bands; the live book embeds the
        # up-revised forecast. Above this ceiling we distrust our own number.
        # This is the robust catch independent of whichever internal path (cold
        # Open-Meteo blend, stale forecast, future calibration drift) corrupted
        # forecast_f. Tunable via config; re-tune on POST-fix paper, not the
        # pre-fix legacy sample.
        if abs(edge) > params["max_disagreement_edge"]:
            skip_counts["disagreement_too_large"] = skip_counts.get("disagreement_too_large", 0) + 1
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
            p_win=p_win, fill=fill, bankroll=eff_bankroll,
            multiplier=params["kelly_multiplier"],
            floor=DEFAULT_MIN_TRADE_USD, cap=params["max_trade_usd"],
        )
        if notional <= 0:
            skip_counts["kelly_no_edge"] = skip_counts.get("kelly_no_edge", 0) + 1
            continue
        contracts = round(notional / fill, 4)

        # ── Live execution branch (#160, 2026-06-02) ──────────────────────
        # Routes through kalshi_live_executor, which enforces EVERY rail
        # (asset allowlist, balance floor, daily-loss halt, 5-loss kill switch,
        # per-asset budget, concurrent cap, 24h dedup). The daily sleeve's own
        # gates (edge/disagreement/forecast-dir/fill) already ran above.
        # asset="weather_daily" → its OWN tiny budget. Refusal → live_order is
        # None and the trade records paper-only (is_live=False; no double-spend).
        # Until "weather_daily" is in settings.live_assets the allowlist gate
        # makes this a guaranteed no-op — safe to ship BEFORE enabling.
        live_order = None
        try:
            from lib.kalshi_live_executor import (
                is_live_enabled, _load_live_config, place_live_order,
                effective_max_trade_usd,
            )
            if is_live_enabled():
                live_cfg = _load_live_config()
                if not _live_balance_cache.get("fetched"):
                    try:
                        from lib.kalshi_client import KalshiClient as _KC
                        _live_balance_cache["bal"] = _KC().get_balance()
                    except Exception:
                        _live_balance_cache["bal"] = None
                    _live_balance_cache["fetched"] = True
                _bal0 = _live_balance_cache.get("bal")
                _pct = float(live_cfg.get("max_trade_bankroll_pct", 0.0) or 0.0)
                if _pct > 0 and _bal0 is None:
                    max_live_contracts = 0   # fail closed: pct sizing needs balance
                else:
                    _avail = (_bal0 - committed_in_cycle) if _bal0 is not None else None
                    live_cap_usd = effective_max_trade_usd(live_cfg, available_balance=_avail)
                    max_live_contracts = int(live_cap_usd / fill) if fill > 0 else 0
                live_contracts = max(0, min(int(contracts), max_live_contracts))
                if live_contracts >= 1:
                    live_order = place_live_order(
                        market_ticker=ticker, side=side, fill_price=fill,
                        contracts=live_contracts,
                        metadata={
                            "asset": "weather_daily",  # MUST match live_assets + _ticker_to_asset
                            "p_win": p_win, "strike_f": strike_f,
                            "forecast_f": forecast_f,
                            "nws_forecast_f": s.get("nws_forecast_f"),
                            "edge": round(float(edge), 4),
                            "close_time": str(s.get("close_time", "")),
                            "city": s.get("city_key"),
                            "kelly_fraction": meta["kelly_fraction"],
                            "paper_contracts": int(contracts),
                        },
                        committed_in_cycle=committed_in_cycle,
                        committed_count_in_cycle=committed_count_in_cycle,
                    )
                    if live_order is not None:
                        committed_in_cycle += float(live_order.get("notional_usd") or 0.0)
                        committed_count_in_cycle += 1
        except Exception as e:
            log_event("weather_daily_paper", "live_branch_exception",
                      {"ticker": ticker, "error": str(e)[:200]}, result="degraded")

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
            # Live metadata — populated only when place_live_order placed a real
            # order. Book ACTUAL filled qty/notional so settlement + kill-switch
            # count real risk on partial fills (legacy-safe .get fallbacks).
            is_live=bool(live_order),
            live_order_id=str(live_order.get("order_id", "")) if live_order else "",
            live_contracts=int(live_order.get("filled_quantity", live_order.get("contracts", 0))) if live_order else 0,
            live_notional_usd=float(live_order.get("filled_notional_usd", live_order.get("notional_usd", 0.0))) if live_order else 0.0,
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
        # LIVE trades (#160) settle on the ACTUAL filled size/notional; paper on
        # the intended paper size. Keeps live P&L, the daily-loss halt, and the
        # kill-switch counting REAL risk (not the paper-intended quantity).
        is_live_trade = bool(rec.get("is_live"))
        if is_live_trade:
            size = float(rec.get("live_contracts") or 0)
            loss_notional = float(rec.get("live_notional_usd") or 0)
        else:
            size = float(rec.get("our_size") or 0)
            loss_notional = float(rec.get("notional") or 0)
        # A live order that filled 0 contracts → nothing at risk → void, NOT loss.
        if is_live_trade and size == 0:
            rec["status"] = "void"
            rec["resolved_at"] = now.isoformat()
            rec["paper_pnl"] = 0.0
            rec["kalshi_result"] = result
            voided_now += 1
            continue
        won = (side == "YES" and result == "yes") or (side == "NO" and result == "no")
        if won:
            # Payoff = $1 per contract minus the fill we paid; minus Kalshi profit fee.
            gross = size * (1.0 - fill)
            fee = max(0.0, gross * KALSHI_PROFIT_FEE)
            pnl = gross - fee
            rec["status"] = "won"
        else:
            pnl = -loss_notional
            rec["status"] = "lost"
        rec["resolved_at"] = now.isoformat()
        rec["paper_pnl"] = round(pnl, 4)
        rec["kalshi_result"] = result
        # LIVE outcomes feed the executor's kill-switch (5 consecutive losses),
        # daily-loss tally, and warning signals — the safety rails the user
        # asked for. Best-effort; never block settlement.
        if is_live_trade:
            try:
                from lib.kalshi_live_executor import record_outcome
                record_outcome(market_ticker=ticker, pnl=round(pnl, 4),
                               opened_at=str(rec.get("opened_at", "")))
            except Exception:
                pass
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
