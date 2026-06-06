"""Paper-trade recorder for the Kalshi weather strategy.

Edge model: we trade when our NWS-derived P(YES) differs from the
Kalshi market price by more than MIN_EDGE. Sizing is half-Kelly against
the NWS probability. Settlement compares the actual NWS-reported
observed temp at close_time against the strike (approximated by the
latest hourly observation since we don't have access to the official
Kalshi-resolution data feed).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from tradingcore import log_event

ROOT = Path(__file__).resolve().parent.parent
# Output + config are env-overridable so a SECOND, parallel paper instance can
# run the same code against a different strategy profile and write to a separate
# ledger (the "original/ungated" A-B replica). Env unset → production defaults,
# i.e. ZERO behavior change for the live hourly sleeve.
PAPER_LOG = Path(os.environ.get("WEATHER_PAPER_LOG") or (ROOT / "data" / "weather_paper.jsonl"))
STRATEGY_PATH = Path(os.environ.get("WEATHER_STRATEGY_PATH") or (ROOT / "config" / "weather_strategy.yaml"))
# HARD live kill-switch for the shadow A-B instance. When WEATHER_PAPER_ONLY=1
# this process is physically incapable of placing a real Kalshi order, no matter
# what the global live config says. Default off → live sleeve unaffected.
_PAPER_ONLY = os.environ.get("WEATHER_PAPER_ONLY") == "1"

DEFAULT_BANKROLL = 1000.0
DEFAULT_MIN_TRADE_USD = 1.0
DEFAULT_MAX_TRADE_USD = 5.0
DEFAULT_KELLY_MULTIPLIER = 0.5
MIN_EDGE_THRESHOLD = 0.10        # |nws_p_yes - market_p_yes| ≥ 10pp
MAX_FILL_FOR_BUY = 0.45
EXTREME_PRICE_FLOOR = 0.05
EXTREME_PRICE_CEIL = 0.95
KALSHI_PROFIT_FEE = 0.07

# ── LIVE-ONLY trend-aware veto (2026-06-02, the "cheap-NO gauge") ────────────
# The bot's live weather entries are gated on the obs-anchored nws_p_yes, which
# lags a moving temperature and produces the false-edge "traps" that caused the
# 06-01 live losses (e.g. bet NO/temp-stays-below while temp is actually rising
# through the strike). The live trade log proves the real edge is narrow: the
# fill<=0.15 cheap-NO bucket made +$225 / 50% WR, while everything else was
# net ~breakeven (after removing the corrupt 05-25 cluster).
#
# This VETO does not create or resize any trade. It only BLOCKS a live order
# the bot would otherwise place, unless the trade ALSO matches the proven
# winner shape on the TREND-AWARE view (the shadow_trendaware block the signal
# module already computes per sample):
#   * side == NO
#   * live fill <= WEATHER_LIVE_VETO_MAX_FILL (cheap)
#   * trend_confirms is True (obs trajectory agrees with NO)
#   * cushion: projected temp stays clear of the strike by >= min sigma
# Paper recording is UNAFFECTED — only the is_live order branch consults it.
# OFF by default; enable with `weather_live_trend_veto: true` in
# config/weather_strategy.yaml. No live behavior changes until that flag is set.
WEATHER_LIVE_VETO_MAX_FILL = 0.15
WEATHER_LIVE_VETO_MIN_CUSHION_SIGMA = 0.75


def _live_trend_veto(s: dict, side: str, raw_fill_live: float,
                     max_fill: float = WEATHER_LIVE_VETO_MAX_FILL,
                     min_cushion_sigma: float = WEATHER_LIVE_VETO_MIN_CUSHION_SIGMA
                     ) -> tuple[bool, str]:
    """Return (allow_live, reason). allow_live=True means the live order may
    proceed; False means VETO (paper still records). Pure read of the sample's
    shadow_trendaware block — no I/O, no side effects."""
    if side != "NO":
        return False, "veto_not_no_side"
    try:
        if raw_fill_live is None or float(raw_fill_live) > max_fill:
            return False, "veto_fill_too_high"
    except (TypeError, ValueError):
        return False, "veto_bad_fill"
    sh = s.get("shadow_trendaware") or {}
    if not sh.get("trend_confirms"):
        return False, "veto_trend_not_confirmed"
    point = sh.get("point_f")
    sigma = sh.get("sigma_f")
    strike = s.get("strike_f")
    if point is None or not sigma or strike is None:
        return False, "veto_no_trend_data"
    # NO wins when temp stays BELOW strike → cushion = (strike - projected)/sigma
    cushion = (float(strike) - float(point)) / float(sigma)
    if cushion < min_cushion_sigma:
        return False, "veto_thin_cushion"
    return True, "trend_ok"


def _load_overrides() -> dict:
    """Read weather_strategy.yaml overrides (written by hermes_weather).
    Returns {} if file missing/unreadable so module-level defaults win.
    Called per-cycle (not just at import) so a fresh Hermes write takes
    effect on the very next signal pass."""
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
    """Module-defaults overlaid with whatever weather_strategy.yaml has.
    Single source of truth for the gates inside record_paper_trades_from_samples."""
    o = _load_overrides()
    return {
        "min_edge_threshold":  float(o.get("min_edge_threshold",  MIN_EDGE_THRESHOLD)),
        "max_fill_for_buy":    float(o.get("max_fill_for_buy",    MAX_FILL_FOR_BUY)),
        "max_trade_usd":       float(o.get("default_max_trade_usd", DEFAULT_MAX_TRADE_USD)),
        "kelly_multiplier":    float(o.get("default_kelly_multiplier", DEFAULT_KELLY_MULTIPLIER)),
        # 2026-05-26 PM HALT FIX: forecast-direction gate.
        # Forensic on 2 losing live trades (lost $11.59) showed the bot
        # bet NO when NWS point-forecast pointed YES (forecast 67.7°F vs
        # 66.99°F strike). Pure probability edge passed; directional
        # incoherence wasn't checked. Gate: bet direction must agree
        # with which side of the strike the forecast falls on.
        #
        # `forecast_buffer_f`: skip when |forecast - strike| < buffer.
        # 0.0 = strict directional only. Default 0.5°F captures normal
        # NWS forecast error and prevents trades right at the boundary
        # (where direction is essentially a coin flip).
        #
        # Backtest of 79 settled trades, last 7d:
        #   no gate       → N=79  WR=41%  +$788  (the bleed risk)
        #   buffer=0.0    → N=36  WR=67%  +$606
        #   buffer=0.5    → N=21  WR=67%  +$388  ← default
        #   buffer=1.0    → N= 9  WR=89%  +$289
        "forecast_buffer_f":   float(o.get("forecast_buffer_f",   0.5)),
        # 2026-05-28: YES-specific tighter buffer. Backtest of 16 YES
        # trades showed winners had +0.68°F avg gap, losers +0.06°F. Edge
        # was identical (~0.30 both groups) — the FORECAST-STRIKE GAP
        # discriminates. Asymmetric is right: NO benefits from "forecast
        # under strike with normal error room", YES needs forecast WELL
        # ABOVE strike (forecasts often miss low). If forecast_buffer_f_yes
        # is None, falls back to the symmetric forecast_buffer_f.
        "forecast_buffer_f_yes": (None if o.get("forecast_buffer_f_yes") is None
                                   else float(o.get("forecast_buffer_f_yes"))),
        # Master switch for the forecast-direction (coherence) gate below.
        # True (default) = production 2026-05-26 HALT-fix behaviour. False =
        # pure probability-edge trading (the pre-HALT "original" behaviour),
        # used ONLY by the ungated PAPER A/B replica. Re-enables the exact
        # against-the-forecast bleed the HALT fix removed, so it is paper-only.
        "forecast_dir_gate": bool(o.get("forecast_dir_gate", True)),
        # 2026-05-26 PM: disable YES side. NO trades had 77% WR (+$608)
        # while YES had 40% WR (-$2). Same pattern as BTC ended up at —
        # one side is the moneymaker, the other is structural drag.
        # Override with False in yaml to re-enable YES if regime changes.
        "weather_no_side_only": bool(o.get("weather_no_side_only", True)),
        # 2026-06-02: LIVE-only trend-aware veto (the cheap-NO gauge). When
        # true, a live order is blocked unless it also matches the proven
        # winner shape on the trend-aware view (see _live_trend_veto). Paper
        # recording is unaffected. Default False → no live behavior change.
        "weather_live_trend_veto": bool(o.get("weather_live_trend_veto", False)),
        "weather_live_veto_max_fill": float(o.get("weather_live_veto_max_fill", WEATHER_LIVE_VETO_MAX_FILL)),
        # ── Timing + margin gates (2026-06-06; RESTRICT-ONLY, default OFF) ──
        # Lever 1 (trade late): only enter inside a [min, max] seconds-to-close
        # window. For hourly a tight max (e.g. 7200 = 2h) keeps entries where the
        # live-observation anchor dominates the forecast. Defaults wide-open
        # (0 .. 1e12) so behavior is UNCHANGED until set in weather_strategy.yaml.
        "min_seconds_to_close": float(o.get("min_seconds_to_close", 0.0)),
        "max_seconds_to_close": float(o.get("max_seconds_to_close", 1e12)),
        # Lever 2 (margin clears noise): require |forecast - strike| >=
        # min_margin_sigma * sigma, so a normal forecast miss can't flip the
        # outcome. 0.0 = off; ~1.5 means the forecast must sit 1.5σ from the
        # strike on our side. RESTRICT-ONLY (only ever removes trades).
        "min_margin_sigma": float(o.get("min_margin_sigma", 0.0)),
    }


@dataclass
class WeatherPaperTrade:
    trade_id: str
    city: str
    market_ticker: str
    event_ticker: str
    title: str
    side: str
    fill_price: float
    our_size: float
    notional: float
    strike_f: float
    nws_forecast_f: float
    nws_p_yes: float
    market_p_yes: float
    edge: float
    close_time: str
    opened_at: str
    status: str
    resolved_at: str = ""
    paper_pnl: float = 0.0
    actual_temp_f: float | None = None
    kelly_fraction: float = 0.0
    half_kelly_fraction: float = 0.0
    # 2026-05-25 PM: live-execution fields, mirroring kalshi_daily_paper.
    # Populated only when kalshi_live_executor.place_live_order returns a
    # real Kalshi order (live mode enabled + weather in allowlist + all
    # safety gates pass). settle_paper_trades feeds the outcome into the
    # executor's daily-loss + kill-switch state.
    is_live: bool = False
    live_order_id: str = ""
    live_contracts: int = 0
    live_notional_usd: float = 0.0


def _load_all() -> list[dict]:
    if not PAPER_LOG.exists():
        return []
    out = []
    with open(PAPER_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _save_all(records: list[dict]) -> None:
    PAPER_LOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = PAPER_LOG.with_suffix(PAPER_LOG.suffix + ".tmp")
    with open(tmp, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    tmp.replace(PAPER_LOG)


def _kelly_size(*, p_win: float, fill: float, bankroll: float,
                multiplier: float, floor: float, cap: float) -> tuple[float, dict]:
    if fill <= 0 or fill >= 1:
        return 0.0, {"kelly_fraction": 0.0, "half_kelly_fraction": 0.0}
    b = (1 - fill) / fill
    q = 1 - p_win
    kelly = (b * p_win - q) / b
    half = kelly * multiplier
    if half <= 0:
        return 0.0, {"kelly_fraction": round(kelly, 4),
                     "half_kelly_fraction": round(half, 4)}
    notional = max(floor, min(cap, bankroll * half))
    return round(notional, 4), {"kelly_fraction": round(kelly, 4),
                                 "half_kelly_fraction": round(half, 4)}


def record_paper_trades_from_samples(samples: list[dict]) -> list[WeatherPaperTrade]:
    if not samples:
        return []
    existing = _load_all()
    open_tickers = {r["market_ticker"] for r in existing if r.get("status") == "open"}
    now_iso = datetime.now(timezone.utc).isoformat()
    new_trades: list[WeatherPaperTrade] = []
    skip_counts: dict[str, int] = {}
    # Pull live params (YAML overrides win over module defaults). One read
    # per cycle is cheap and ensures Hermes' changes take effect immediately.
    params = _effective_params()
    eff_min_edge = params["min_edge_threshold"]
    eff_max_fill = params["max_fill_for_buy"]
    eff_max_usd  = params["max_trade_usd"]
    eff_kelly    = params["kelly_multiplier"]

    # Running total of notional committed to live orders within this scan
    # cycle. Mirrors kalshi_daily_paper's balance-race fix — Kalshi balance
    # lags placements by ~1s, so without this stacking multiple in-cycle
    # weather trades each see the same starting balance.
    committed_in_cycle: float = 0.0
    # Running COUNT of live weather orders placed this cycle. Threads to the
    # per-asset concurrent-count gate (e.g. weather cap = 3) so the cap holds
    # before Kalshi's positions list catches up. All weather cities share the
    # single "weather" asset bucket, so this is the in-cycle weather count.
    committed_count_in_cycle: int = 0
    # Cycle-level cache of the live cash balance — the per-trade cap can be
    # bankroll-relative (15% of available cash). Fetched at most once/cycle.
    _live_balance_cache: dict = {}

    for s in samples:
        ticker = s.get("market_ticker", "")
        if not ticker or ticker in open_tickers:
            skip_counts["dup_open"] = skip_counts.get("dup_open", 0) + 1
            continue
        # ── Lever 1: trade-late timing window (restrict-only; off by default) ──
        # Skip entries outside [min, max] seconds-to-close. A tight max means we
        # only bet once the temperature has nearly settled (obs anchor strong),
        # which is where the hourly edge is real rather than forecast-variance.
        _stc = s.get("seconds_to_close")
        if _stc is not None:
            _stc = float(_stc)
            if (_stc < params["min_seconds_to_close"]
                    or _stc > params["max_seconds_to_close"]):
                skip_counts["outside_timing_window"] = (
                    skip_counts.get("outside_timing_window", 0) + 1)
                continue
        nws_p = s.get("nws_p_yes")
        market_p = s.get("market_p_yes")
        if nws_p is None or market_p is None:
            skip_counts["missing_data"] = skip_counts.get("missing_data", 0) + 1
            continue
        edge = nws_p - market_p  # positive = NWS thinks YES more likely than market
        if abs(edge) < eff_min_edge:
            skip_counts["edge_too_small"] = skip_counts.get("edge_too_small", 0) + 1
            continue

        # Direction = which side has positive edge?
        if edge > 0:
            side, fill, p_win = "YES", s.get("yes_ask"), nws_p
        else:
            side, fill, p_win = "NO", s.get("no_ask"), 1 - nws_p

        # ── Forecast-direction gate (2026-05-26 PM HALT FIX) ─────────
        # Refuse the trade if NWS point-forecast doesn't agree with the
        # bet direction (with optional buffer to skip boundary cases).
        # Forensic on the $11.59 live loss showed Trade #2 bet NO while
        # the forecast (67.7°F) was clearly ABOVE the strike (66.99°F) —
        # bot bet against its own forecast on pure probability edge.
        forecast_f = float(s.get("nws_forecast_f") or 0)
        strike_f   = float(s.get("strike_f") or 0)
        # 2026-05-28: asymmetric buffer. NO uses base; YES uses the
        # optional tighter `forecast_buffer_f_yes` if set, else falls
        # back to base. Backtest showed YES winners had +0.68°F avg
        # gap vs losers +0.06°F — YES needs more upside conviction
        # than NO because forecasts often miss low (warmer than
        # predicted) more than they miss high.
        buf_no  = params["forecast_buffer_f"]
        buf_yes = params.get("forecast_buffer_f_yes") or buf_no
        if params["forecast_dir_gate"]:
            if side == "YES":
                # YES wins if temp >= strike. Need forecast clearly above.
                if forecast_f < (strike_f + buf_yes):
                    skip_counts["forecast_dir_yes"] = (
                        skip_counts.get("forecast_dir_yes", 0) + 1
                    )
                    continue
            else:   # NO
                # NO wins if temp < strike. Need forecast clearly below.
                if forecast_f > (strike_f - buf_no):
                    skip_counts["forecast_dir_no"] = (
                        skip_counts.get("forecast_dir_no", 0) + 1
                    )
                    continue

        # ── Lever 2: margin must clear forecast noise (restrict-only; off) ──
        # Require the forecast to sit min_margin_sigma * sigma from the strike,
        # so a normal forecast miss (~σ) can't flip our side. sigma is the
        # signal's blended estimate. Off (0.0) → no change.
        _mms = params["min_margin_sigma"]
        if _mms > 0:
            _sigma = (s.get("blend_meta") or {}).get("sigma_f")
            try:
                _sigma = float(_sigma) if _sigma is not None else None
            except (TypeError, ValueError):
                _sigma = None
            if _sigma and _sigma > 0 and abs(forecast_f - strike_f) < _mms * _sigma:
                skip_counts["margin_below_sigma"] = (
                    skip_counts.get("margin_below_sigma", 0) + 1)
                continue

        # YES side disable gate. Backtest showed weather YES = 40% WR /
        # -$2; NO = 77% WR / +$608. Mirror of BTC where one side was
        # the moneymaker. Override `weather_no_side_only: false` in yaml
        # if regime changes / re-enables YES.
        if side == "YES" and params["weather_no_side_only"]:
            skip_counts["yes_disabled"] = skip_counts.get("yes_disabled", 0) + 1
            continue
        if fill is None:
            # Compute opposite from yes side if needed
            if side == "NO" and s.get("yes_ask") is not None:
                fill = round(1.0 - float(s["yes_ask"]), 4)
        if fill is None:
            skip_counts["no_fill"] = skip_counts.get("no_fill", 0) + 1
            continue
        fill = float(fill)
        if not (EXTREME_PRICE_FLOOR <= fill <= EXTREME_PRICE_CEIL):
            skip_counts["extreme_price"] = skip_counts.get("extreme_price", 0) + 1
            continue
        if fill > eff_max_fill:
            skip_counts["fill_too_high"] = skip_counts.get("fill_too_high", 0) + 1
            continue

        # SLIPPAGE MODEL (paper-only — matches kalshi_daily_paper v2).
        # 2026-05-27 BUGFIX: preserve raw_fill_live for the real Kalshi
        # order. Previously `fill` was clobbered with the slipped value
        # and sent as the live limit price.
        import random
        raw_fill_live = fill
        base_slip = 0.01
        size_slip = (min(eff_max_usd, 3.0) * 0.5 / 3.0) * 0.01
        latency_drift = random.uniform(-0.005, 0.01)
        total_slip = base_slip + size_slip + latency_drift
        # Two-sided: occasional price improvement (negative slip) allowed.
        fill_paper = max(0.01, min(0.99, raw_fill_live + total_slip))

        # NOTE (2026-06-01): a disagreement-aware DOWNSIZE was tried here and
        # REVERTED. Validation showed the high-disagreement cohort (|forecast −
        # obs| ≥ 4°F) is the MOST profitable cohort — 47 trades, 57% WR,
        # +$1,382 (71% of all profit, +$29/trade vs +$6.59 for the stable
        # cohort). Downsizing it to cap the occasional miss (the 71° loss) would
        # have cut ~$900 of profit. We also proved winners and losers are
        # indistinguishable on entry features, so we can't selectively downsize
        # only the losers. Leave sizing full; the realized-trend discriminator
        # (logged in the signal shadow) is the only path to a selective fix.
        notional, meta = _kelly_size(
            p_win=p_win, fill=fill_paper, bankroll=DEFAULT_BANKROLL,
            multiplier=eff_kelly,
            floor=DEFAULT_MIN_TRADE_USD, cap=eff_max_usd,
        )
        if notional <= 0:
            skip_counts["kelly_no_edge"] = skip_counts.get("kelly_no_edge", 0) + 1
            continue
        contracts = round(notional / fill_paper, 4)   # paper-only sizing

        # ── Live execution branch (2026-05-25 PM) ─────────────────────
        # Mirrors kalshi_daily_paper's live wiring. If kalshi_live_executor
        # says live mode is on AND "weather" is in the live_assets allowlist
        # AND all safety gates pass, place a real Kalshi order. The executor
        # returns None on refusal — paper-only recording still happens (no
        # double-spend), trade just gets is_live=False.
        #
        # Paper Kelly notional defaults to $12.50; live cap is $6 in
        # settings.yaml. Downsize live contracts to fit the live cap so
        # the trade_size gate doesn't refuse every order.
        live_order = None
        try:
            from lib.kalshi_live_executor import (
                is_live_enabled, _load_live_config, place_live_order,
                effective_max_trade_usd,
            )
            # TREND-AWARE VETO (opt-in via weather_strategy.yaml
            # `weather_live_trend_veto: true`). Blocks live orders that don't
            # match the proven cheap-NO + trend-confirmed + positive-cushion
            # shape. Paper recording below is unaffected. Default off → no live
            # behavior change until explicitly enabled.
            _veto_on = bool(params.get("weather_live_trend_veto", False))
            _veto_ok, _veto_reason = (True, "veto_disabled")
            if _veto_on:
                _veto_ok, _veto_reason = _live_trend_veto(
                    s, side, raw_fill_live,
                    max_fill=params["weather_live_veto_max_fill"])
                if not _veto_ok and is_live_enabled():
                    # Live order blocked by the gauge; paper still records below.
                    skip_counts[_veto_reason] = skip_counts.get(_veto_reason, 0) + 1
            if is_live_enabled() and (not _veto_on or _veto_ok) and not _PAPER_ONLY:
                live_cfg = _load_live_config()
                # Per-trade cap may be bankroll-relative — size off live cash
                # (net of in-cycle commitments). Fetch balance once per cycle.
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
                    # Fail closed: bankroll-% sizing requested but balance
                    # unreadable → skip live order (paper still records).
                    max_live_contracts = 0
                else:
                    _avail = (_bal0 - committed_in_cycle) if _bal0 is not None else None
                    live_cap_usd = effective_max_trade_usd(live_cfg, available_balance=_avail)
                    # Live sizing uses RAW ask, not slipped fill.
                    max_live_contracts = int(live_cap_usd / raw_fill_live) if raw_fill_live > 0 else 0
                live_contracts = max(0, min(int(contracts), max_live_contracts))
                if live_contracts >= 1:
                    live_order = place_live_order(
                        market_ticker=ticker,
                        side=side,
                        fill_price=raw_fill_live,   # real ask, NOT slipped
                        contracts=live_contracts,
                        metadata={
                            "p_win":          p_win,
                            "kelly_fraction": meta["kelly_fraction"],
                            "strike_f":       s.get("strike_f"),
                            "nws_forecast_f": s.get("nws_forecast_f"),
                            "edge":           round(edge, 4),
                            "close_time":     str(s.get("close_time", "")),
                            "city":           s.get("city"),
                            # Required by the executor's asset-allowlist
                            # gate — must match the live_assets entry.
                            "asset":          "weather",
                            "paper_contracts":    int(contracts),
                            "paper_notional_usd": round(fill_paper * contracts, 4),
                            "downsized_to_cap":   live_contracts < int(contracts),
                        },
                        committed_in_cycle=committed_in_cycle,
                        committed_count_in_cycle=committed_count_in_cycle,
                    )
                    if live_order is not None:
                        committed_in_cycle += float(
                            live_order.get("notional_usd") or 0.0
                        )
                        committed_count_in_cycle += 1
        except Exception as e:
            log_event("weather_signal", "live_branch_exception",
                      {"ticker": ticker, "error": str(e)[:200]},
                      result="degraded")

        # fill_price stored on the record matches the cohort:
        #   live trade → raw_fill_live, paper → fill_paper
        recorded_fill = raw_fill_live if live_order is not None else fill_paper
        # For LIVE trades, mirror notional with live_notional_usd for
        # downstream consistency (audit pass 2 fix).
        if live_order is not None:
            recorded_notional = float(live_order.get("notional_usd") or 0.0)
        else:
            recorded_notional = round(recorded_fill * contracts, 4)
        trade = WeatherPaperTrade(
            trade_id=f"{ticker[:24]}_{int(datetime.now(timezone.utc).timestamp())}",
            city=s.get("city", "?"),
            market_ticker=ticker,
            event_ticker=str(s.get("event_ticker", ""))[:60],
            title=str(s.get("title", ""))[:200],
            side=side, fill_price=recorded_fill, our_size=contracts,
            notional=recorded_notional,
            strike_f=float(s.get("strike_f", 0)),
            nws_forecast_f=float(s.get("nws_forecast_f", 0)),
            nws_p_yes=float(nws_p), market_p_yes=float(market_p),
            edge=round(edge, 4),
            close_time=str(s.get("close_time", "")),
            opened_at=now_iso, status="open",
            kelly_fraction=meta["kelly_fraction"],
            half_kelly_fraction=meta["half_kelly_fraction"],
            # Live-trade metadata (populated only when live_order placed)
            is_live=bool(live_order),
            live_order_id=str(live_order.get("order_id", "")) if live_order else "",
            # Book ACTUAL filled quantity/notional (not requested) so settlement +
            # daily-loss halt + kill-switch count real risk on partial fills.
            # Degrades to requested when the client omits the field (legacy-safe).
            live_contracts=int(live_order.get("filled_quantity", live_order.get("contracts", 0))) if live_order else 0,
            live_notional_usd=float(live_order.get("filled_notional_usd", live_order.get("notional_usd", 0.0))) if live_order else 0.0,
        )
        new_trades.append(trade)
        log_event("weather_signal", "paper_trade_opened", {
            "ticker": ticker, "city": s.get("city"), "side": side,
            "fill": fill, "strike_f": s.get("strike_f"),
            "nws_forecast_f": s.get("nws_forecast_f"),
            "nws_p_yes": round(nws_p, 3), "edge": round(edge, 3),
            "notional": notional,
        }, result="success")

    # 2026-05-28: one-line skip summary so a 0-trade cycle isn't silent.
    if skip_counts:
        total_skipped = sum(skip_counts.values())
        detail = ", ".join(
            f"{k}={v}" for k, v in
            sorted(skip_counts.items(), key=lambda kv: kv[1], reverse=True)
        )
        print(f"  Skipped {total_skipped}: {detail}")

    # 2026-06-02: persist a LIVE-VETO activity snapshot so the dashboard can
    # show the cheap-NO gauge working in real time. Only the veto_* reasons are
    # surfaced (the trades the gauge blocked from going live this cycle). Atomic
    # write; failures never break the trade path.
    try:
        if params.get("weather_live_trend_veto"):
            _write_veto_activity(skip_counts)
    except Exception:
        pass

    if new_trades:
        _save_all(existing + [asdict(t) for t in new_trades])
    return new_trades


VETO_ACTIVITY_PATH = ROOT / "data" / "weather_live_veto_activity.json"


def _write_veto_activity(skip_counts: dict) -> None:
    """Atomically record this cycle's veto outcomes for dashboard display.
    Keeps a rolling 24h-ish history (last 200 cycles) of veto-reason counts."""
    import os
    from datetime import datetime, timezone
    veto = {k: v for k, v in (skip_counts or {}).items() if k.startswith("veto_")}
    hist = []
    if VETO_ACTIVITY_PATH.exists():
        try:
            with open(VETO_ACTIVITY_PATH) as f:
                hist = (json.load(f) or {}).get("cycles", [])
        except (OSError, json.JSONDecodeError):
            hist = []
    hist.append({"at": datetime.now(timezone.utc).isoformat(),
                 "vetoed": sum(veto.values()), "reasons": veto})
    hist = hist[-200:]
    # Aggregate the rolling window for an at-a-glance tile.
    agg: dict[str, int] = {}
    for c in hist:
        for k, v in (c.get("reasons") or {}).items():
            agg[k] = agg.get(k, 0) + int(v)
    payload = {
        "updated_at": hist[-1]["at"],
        "enabled": True,
        "last_cycle_vetoed": hist[-1]["vetoed"],
        "last_cycle_reasons": hist[-1]["reasons"],
        "window_vetoed_total": sum(agg.values()),
        "window_reasons": agg,
        "cycles": hist,
    }
    VETO_ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = VETO_ACTIVITY_PATH.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, VETO_ACTIVITY_PATH)


def _fetch_actual_temp(city: str, target_iso: str) -> float | None:
    """Pull the NWS observed temp closest to target_iso. NWS observations
    endpoint requires the station code; we look up the closest station
    by city's coords."""
    import urllib.request
    from lib.weather_signal import CITIES
    cfg = CITIES.get(city)
    if not cfg:
        return None
    try:
        # Find the nearest official station for this city
        req = urllib.request.Request(
            f"https://api.weather.gov/points/{cfg['lat']},{cfg['lon']}/stations",
            headers={"User-Agent": "polybot-weather-scanner"}
        )
        stations = json.loads(urllib.request.urlopen(req, timeout=10).read())
        feats = stations.get("features", [])
        if not feats:
            return None
        station_id = feats[0]["properties"]["stationIdentifier"]
        # Get latest observations
        req = urllib.request.Request(
            f"https://api.weather.gov/stations/{station_id}/observations/latest",
            headers={"User-Agent": "polybot-weather-scanner"}
        )
        obs = json.loads(urllib.request.urlopen(req, timeout=10).read())
        temp_c = obs["properties"]["temperature"]["value"]
        if temp_c is None:
            return None
        return temp_c * 9 / 5 + 32
    except Exception:
        return None


def settle_paper_trades() -> dict:
    records = _load_all()
    open_ones = [r for r in records if r.get("status") == "open"]
    if not open_ones:
        return {"settled_now": 0, "total_open": 0}

    import requests
    now = datetime.now(timezone.utc)
    settled = 0
    # Per-cycle NWS observation cache. Each _fetch_actual_temp call costs
    # 2 HTTP requests (station lookup + observation). Without this, when N
    # weather trades for the same city settle in one cycle, we hit NWS 2N
    # times. Now we hit it once per (city, close_time) pair.
    _temp_cache: dict[tuple[str, str], float | None] = {}
    def fetch_actual(city: str, close_iso: str) -> float | None:
        key = (city, close_iso)
        if key not in _temp_cache:
            _temp_cache[key] = _fetch_actual_temp(city, close_iso)
        return _temp_cache[key]
    for r in open_ones:
        try:
            ct = datetime.fromisoformat(r["close_time"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if now < ct:
            continue

        # 2026-05-26 PM: try Kalshi resolution FIRST for ALL trades.
        # We learned the hard way that NWS observation can disagree with
        # Kalshi's settlement source: yesterday both weather positions
        # marked LOST per NWS (temp 68°F > strikes) but Kalshi resolved
        # them as voids/wins (cash returned +$13 vs expected -$11.59).
        # That divergence is why paper looked profitable while live didn't.
        ticker = r.get("market_ticker", "")
        side = r.get("side", "YES")
        we_won = None
        kresult = ""
        if ticker:
            try:
                kresp = requests.get(
                    f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}",
                    timeout=10,
                )
                kdata = kresp.json() if kresp.text else {}
                kmarket = kdata.get("market", {}) if isinstance(kdata, dict) else {}
                kresult = (kmarket.get("result") or "").lower()
            except Exception:
                kresult = ""
        if kresult in ("yes", "no"):
            we_won = (side == "YES" and kresult == "yes") or \
                     (side == "NO" and kresult == "no")
            r["settled_via"] = "kalshi_result"

        is_live_trade = bool(r.get("is_live"))
        if we_won is None and is_live_trade:
            # Live trades MUST use Kalshi resolution. Skip until resolved.
            # 2026-05-27 BUGFIX: void timeout — if >24h past close and
            # Kalshi still has no result, mark void to free the per-asset
            # budget slot. Otherwise the slot gets held forever.
            try:
                hours_past_close = (now - ct).total_seconds() / 3600.0
            except Exception:
                hours_past_close = 0.0
            if hours_past_close > 24:
                r["status"] = "void"
                r["paper_pnl"] = 0.0
                r["resolved_at"] = now.isoformat()
                r["settled_via"] = "void_kalshi_no_call_after_24h"
                log_event("weather_signal", "live_trade_marked_void",
                          {"ticker": r.get("market_ticker"),
                           "hours_past_close": round(hours_past_close, 1)},
                          result="degraded")
                settled += 1
            continue

        if we_won is None:
            # PAPER fallback: NWS observation. Marked so we know to
            # discount it when comparing paper vs live performance.
            actual = fetch_actual(r.get("city", ""), r["close_time"])
            if actual is None:
                continue
            strike = float(r.get("strike_f", 0))
            market_yes_won = actual >= strike
            we_won = (side == "YES" and market_yes_won) or (side == "NO" and not market_yes_won)
            r["actual_temp_f"] = round(actual, 1)
            r["settled_via"] = "nws_observation"

        # Before reading the recorded fill, correct it to Kalshi's TRUE final
        # fill. place_live_order captures only the immediate (taker) fill; a
        # resting order that fills later as a maker order shows 0 there and
        # would otherwise settle as no_fill/$0 despite real money at stake
        # (5 such weather NO bets hid -$57.62 on 2026-05-29). Read-only; on
        # failure it leaves the recorded values intact.
        is_live_trade = bool(r.get("is_live"))
        if is_live_trade:
            try:
                from lib.kalshi_live_executor import reconcile_live_fill
                reconcile_live_fill(r)
            except Exception as e:
                log_event("weather_signal", "live_fill_reconcile_failed",
                          {"ticker": r.get("market_ticker"), "error": str(e)[:200]},
                          result="degraded")
        strike = float(r.get("strike_f", 0))
        fill = float(r.get("fill_price") or 0)
        # For LIVE trades, settle against the actual live notional/contracts
        # (cap-downsized at placement). For paper-only, use the full Kelly
        # notional. Mirrors kalshi_daily_paper's effective_size handling so
        # paper_pnl reflects what real money won/lost on live trades.
        if is_live_trade:
            effective_size = float(r.get("live_contracts") or 0)
            effective_notional = float(r.get("live_notional_usd") or 0)
        else:
            effective_size = float(r.get("our_size") or 0)
            effective_notional = float(r.get("notional") or 0)
        # A live order is a limit buy; if it never crossed the book it filled
        # 0 contracts. That risked $0 and won/lost nothing — labeling it
        # won/lost pollutes win-rate AND the weather calibrator that drives
        # Hermes. Settle it as "no_fill" (excluded from WR aggregation) but
        # still let the calibration block below run on (forecast, actual).
        no_fill = is_live_trade and effective_size == 0
        if no_fill:
            r["paper_pnl"] = 0.0
            r["status"] = "no_fill"
        elif we_won:
            gross = effective_size * (1 - fill)
            r["paper_pnl"] = round(gross * (1 - KALSHI_PROFIT_FEE), 4)
            r["status"] = "won"
        else:
            r["paper_pnl"] = round(-effective_notional, 4)
            r["status"] = "lost"
        # actual_temp_f + settled_via are now set inside the branching
        # above (kalshi_result OR nws_observation). Don't overwrite here.
        r["resolved_at"] = now.isoformat()

        # Feed live trade outcomes into the executor's rolling-loss state +
        # early-warning monitor (mirrors kalshi_daily_paper's settle path).
        # Wrapped so failure can't block weather settlement.
        # Skip no-fills: they risked $0, so they must not count toward the
        # consecutive-loss / kill-switch feed (phantom streaks otherwise).
        if is_live_trade and not no_fill:
            try:
                from lib.kalshi_live_executor import (
                    record_outcome, check_warning_signals,
                )
                record_outcome(
                    market_ticker=r.get("market_ticker", ""),
                    pnl=float(r.get("paper_pnl", 0)),
                    opened_at=r.get("opened_at", ""),
                )
                try:
                    from lib.kalshi_client import KalshiClient
                    cur_bal = KalshiClient().get_balance()
                    check_warning_signals(balance=float(cur_bal))
                except Exception as e2:
                    log_event("weather_signal", "early_warning_check_failed",
                              {"error": str(e2)[:200]}, result="degraded")
            except Exception as e:
                log_event("weather_signal", "live_outcome_record_failed",
                          {"ticker": r.get("market_ticker"), "error": str(e)[:200]},
                          result="degraded")
        # Feed the (forecast, actual) pair to the per-city calibrator so
        # the next signal cycle can correct NWS bias and tighten σ. The
        # forecast we stored is the BLENDED point estimate, which is what
        # we actually traded against — that's the bias we care about, not
        # raw NWS. Wrapped in try/except so calibration failures never
        # block settlement.
        # 2026-05-27 BUGFIX: when Kalshi resolved the market, the NWS
        # observation branch is skipped → `actual` is never bound → this
        # block silently NameError'd, breaking weather calibration learning.
        # Now we re-fetch the observation specifically for calibration
        # (separate concern from settlement). If it's not available, skip.
        try:
            from lib.weather_calibration import record_error
            recorded_forecast = r.get("nws_forecast_f")
            city = r.get("city")
            # Prefer already-stored actual_temp_f (set by the NWS branch);
            # otherwise re-fetch since Kalshi-resolved settles don't have it.
            actual_for_cal = r.get("actual_temp_f")
            if city and recorded_forecast is not None and actual_for_cal is None:
                actual_for_cal = fetch_actual(city, r["close_time"])
                if actual_for_cal is not None:
                    r["actual_temp_f"] = round(actual_for_cal, 1)
            if city and recorded_forecast is not None and actual_for_cal is not None:
                record_error(city, float(recorded_forecast), float(actual_for_cal))
        except Exception as e:
            log_event("weather_signal", "calibration_record_failed",
                      {"trade_id": r.get("trade_id"), "error": str(e)[:200]},
                      result="degraded")
        settled += 1

    if settled:
        _save_all(records)
        log_event("weather_signal", "paper_settled_batch", {"settled_now": settled})
    return {"settled_now": settled,
            "total_open": sum(1 for r in records if r.get("status") == "open")}
