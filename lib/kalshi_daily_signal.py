"""Kalshi DAILY-crypto signal pipeline (KXBTCD, KXETHD, KXSOLD, KXXRPD).

Same BSM-Greeks approach as the 15-min pipeline but applied to the
daily-resolution strike ladder. Why this is the "easier" surface:

  * 24-hour horizon means signal-to-noise improves by ~√(86400/900) ≈
    9.8× vs the 15-min surface for the same vol assumption.
  * Markets are a STRIKE LADDER (~20 strikes per day per asset) — we
    pick the 1-3 strikes nearest spot, where Kalshi liquidity exists.
  * Same compute_indicators_for_window helper applies — we just feed it
    hours_to_close in the daily range.

Cron-friendly: one cycle takes ~3s; designed for 5-15-minute scan
intervals (vs the 1-minute cadence on 15-min).
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from tradingcore import log_event

from lib.btc_5min_signal import compute_indicators_for_window

KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"
ASSETS_CONFIG_PATH = Path(__file__).parent.parent / "config" / "kalshi_daily_assets.yaml"


# How many strikes to keep per asset per cycle (centered on spot, taking
# the nearest N above + N below). Limits API noise and concentrates on
# the meaty middle of the distribution where edge actually exists.
NEAR_SPOT_STRIKE_COUNT = 5


def load_assets_config() -> dict:
    """Read the daily asset registry. Same shape as kalshi_assets.yaml."""
    import yaml
    if not ASSETS_CONFIG_PATH.exists():
        return {}
    try:
        with open(ASSETS_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("assets", {}) or {}
    except (yaml.YAMLError, OSError) as e:
        log_event("kalshi_daily", "assets_config_load_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return {}


def enabled_assets() -> dict:
    return {k: v for k, v in load_assets_config().items() if v and v.get("enabled")}


@dataclass
class KalshiDailySample:
    sample_at: str
    asset: str
    market_ticker: str
    event_ticker: str
    title: str
    open_time: str
    close_time: str
    seconds_to_close: float
    strike: float
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume_24h: float
    spot_usd: float
    indicators: dict | None = None
    distance_to_spot_pct: float | None = None


# ── Discovery ────────────────────────────────────────────────────────

def discover_daily_markets(series_ticker: str, max_seconds_out: int = 86400) -> list[dict]:
    """Find all currently-open daily markets for the series."""
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
        log_event("kalshi_daily", "events_fetch_failed",
                  {"series": series_ticker, "error": str(e)[:200]}, result="degraded")
        return []

    now = datetime.now(timezone.utc)
    qualified: list[dict] = []
    for e in events:
        et = e.get("event_ticker", "")
        if not et:
            continue
        try:
            mr = requests.get(
                f"{KALSHI_HOST}/markets",
                params={"event_ticker": et, "status": "open", "limit": 100},
                timeout=15,
            )
            mr.raise_for_status()
            markets = mr.json().get("markets", []) or []
        except Exception:
            continue
        for m in markets:
            close_iso = m.get("close_time", "")
            if not close_iso:
                continue
            try:
                ct = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
            except ValueError:
                continue
            secs = (ct - now).total_seconds()
            if secs <= 0 or secs > max_seconds_out:
                continue
            # Parse strike + market DIRECTION from floor/cap fields. Kalshi
            # exposes three market types:
            #
            #   above    : floor=X, cap=None  → YES if S_T ≥ X   (KXBTCD-style)
            #   below    : floor=None, cap=X  → YES if S_T ≤ X   (KXINX T-prefix)
            #   between  : floor=X, cap=Y     → YES if X ≤ S_T ≤ Y  (KXINX B-prefix)
            #
            # The "above" case was the original assumption; without explicit
            # direction handling, below/between markets would size trades
            # backwards. We store the direction for the BSM math downstream.
            floor = m.get("floor_strike")
            cap = m.get("cap_strike")
            if floor is not None and cap is None:
                direction = "above"
                strike = float(floor)
            elif floor is None and cap is not None:
                direction = "below"
                strike = float(cap)
            elif floor is not None and cap is not None:
                direction = "between"
                strike = (float(floor) + float(cap)) / 2.0  # midpoint for proximity sort
            else:
                # Fallback: parse strike from ticker, assume "above" (legacy crypto path).
                import re
                tm = re.search(r"T(\d+(?:\.\d+)?)$", m.get("ticker", ""))
                strike = float(tm.group(1)) if tm else None
                direction = "above"
            m["_parsed_strike"] = float(strike) if strike is not None else None
            m["_direction"] = direction
            m["_floor_strike"] = float(floor) if floor is not None else None
            m["_cap_strike"] = float(cap) if cap is not None else None
            m["_seconds_to_close"] = secs
            m["_close_iso"] = close_iso
            qualified.append(m)
    return qualified


def _pick_near_spot(markets: list[dict], spot: float, n_each_side: int) -> list[dict]:
    """Keep only the N strikes immediately above + N below current spot."""
    with_strike = [m for m in markets if m.get("_parsed_strike") is not None and spot > 0]
    above = sorted([m for m in with_strike if m["_parsed_strike"] >= spot],
                   key=lambda m: m["_parsed_strike"])[:n_each_side]
    below = sorted([m for m in with_strike if m["_parsed_strike"] < spot],
                   key=lambda m: -m["_parsed_strike"])[:n_each_side]
    return above + below


# ── Sampling ─────────────────────────────────────────────────────────

def _fetch_binance_klines(symbol: str, interval: str = "1h", limit: int = 50) -> list[dict]:
    """Recent klines for vol + indicator computation."""
    import requests
    try:
        r = requests.get(
            "https://api.binance.us/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
    except Exception:
        return []
    return [
        {
            "open_time_ms": int(row[0]),
            "open":  float(row[1]),
            "high":  float(row[2]),
            "low":   float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for row in raw
    ]


def _fetch_spot(symbol: str) -> float | None:
    """Spot price for the asset.

    2026-05-25 CRITICAL FIX: Kalshi BTC/ETH daily markets settle on
    CF Benchmarks BRTI/ERTI — an average of Coinbase/Bitstamp/Kraken/
    Gemini USD pairs. Binance USDT is NOT in the panel and can diverge
    by hundreds of dollars during volatility (lost BTC trade had Binance
    @ $76,802 vs BRTI @ $76,048 = $754 gap → bot thought we'd win, Kalshi
    said we lost).

    Resolution: prefer Coinbase USD pair (in the BRTI panel) for any
    symbol we know how to translate. Fall back to Binance only when
    Coinbase translation isn't possible (e.g. obscure tokens). This
    aligns our signal data with Kalshi's settle source.
    """
    import requests
    # Map Binance USDT symbol → Coinbase USD product
    coinbase_map = {
        "BTCUSDT": "BTC-USD",
        "ETHUSDT": "ETH-USD",
        "SOLUSDT": "SOL-USD",
        "XRPUSDT": "XRP-USD",
        "ADAUSDT": "ADA-USD",
        "DOGEUSDT": "DOGE-USD",
    }
    cb_product = coinbase_map.get(symbol)
    if cb_product:
        try:
            r = requests.get(
                f"https://api.exchange.coinbase.com/products/{cb_product}/ticker",
                timeout=8,
            )
            r.raise_for_status()
            return float(r.json()["price"])
        except Exception:
            # Coinbase failed — fall through to Binance as backup
            pass
    # Fallback: Binance (legacy / for symbols not on Coinbase)
    try:
        r = requests.get(
            "https://api.binance.us/api/v3/ticker/price",
            params={"symbol": symbol}, timeout=8,
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


# ── yfinance source helpers (direct HTTP, no yfinance lib) ───────────
# We hit Yahoo's public chart API directly. Avoids the yfinance Python
# package whose curl_cffi native dep is broken on the current macOS env.
# Same JSON shape that yfinance internally consumes.

_YAHOO_CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"


def _fetch_yahoo_chart(symbol: str, *, interval: str = "1d",
                       range_: str = "5d") -> dict | None:
    """Pull Yahoo chart JSON for a symbol. Returns the inner 'result[0]'
    object (has timestamp[] + indicators.quote[0].close[] etc.) or None
    on any failure. Wrapped in defensive try/except — a signal cycle
    must never die because Yahoo timed out."""
    import urllib.request, urllib.parse, json as _json
    # Yahoo requires UA, otherwise sometimes 401s.
    url = f"{_YAHOO_CHART_BASE}/{urllib.parse.quote(symbol)}?interval={interval}&range={range_}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (polybot-daily-signal/1.0)",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    try:
        return data["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return None


def _fetch_yfinance_klines(yf_symbol: str, period: str = "5d",
                            interval: str = "1h") -> list[dict]:
    """Yahoo klines. Returns the same dict shape as the Binance helper
    so downstream vol-estimation is identical. Empty list on failure."""
    result = _fetch_yahoo_chart(yf_symbol, interval=interval, range_=period)
    if result is None:
        return []
    try:
        ts = result["timestamp"]
        quote = result["indicators"]["quote"][0]
        closes = quote.get("close") or []
        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows  = quote.get("low")  or []
        vols  = quote.get("volume") or []
    except (KeyError, IndexError, TypeError):
        return []
    out = []
    for i, t in enumerate(ts):
        # Skip bars where Yahoo returned null (gap)
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        out.append({
            "open_time_ms": int(t * 1000),
            "open":  float(opens[i] or c),
            "high":  float(highs[i] or c),
            "low":   float(lows[i]  or c),
            "close": float(c),
            "volume": float(vols[i] or 0),
        })
    return out


def _fetch_yfinance_spot(yf_symbol: str) -> float | None:
    """Latest close from Yahoo chart API. Used as our spot price for
    non-crypto daily-signal assets. Indices (^GSPC, ^VIX) and ETFs (SPY)
    both work. period='2d' is robust against weekend/holiday gaps."""
    bars = _fetch_yfinance_klines(yf_symbol, period="5d", interval="1d")
    if not bars:
        return None
    return bars[-1]["close"]


def _resolve_data_source(asset_cfg: dict) -> tuple[str, str]:
    """Return (data_source, symbol) for the asset. Backwards-compatible:
    if asset_cfg has only binance_symbol (legacy crypto entries), defaults
    to binance. Otherwise reads explicit `data_source` + matching symbol
    field. Supported sources: 'binance', 'yfinance'."""
    if asset_cfg.get("data_source") == "yfinance":
        return "yfinance", asset_cfg.get("yfinance_symbol", "")
    # default = binance for legacy compatibility
    return "binance", asset_cfg.get("binance_symbol", "")


def _fetch_spot_unified(asset_cfg: dict) -> float | None:
    """Spot price via whichever source the asset config declares."""
    source, symbol = _resolve_data_source(asset_cfg)
    if not symbol:
        return None
    if source == "yfinance":
        return _fetch_yfinance_spot(symbol)
    return _fetch_spot(symbol)


def _fetch_klines_unified(asset_cfg: dict) -> list[dict]:
    """Recent OHLCV bars via whichever source the asset config declares.
    Defaults to ~3 days of hourly bars (matches the existing BTC behavior)."""
    source, symbol = _resolve_data_source(asset_cfg)
    if not symbol:
        return []
    if source == "yfinance":
        return _fetch_yfinance_klines(symbol, period="5d", interval="1h")
    return _fetch_binance_klines(symbol, interval="1h", limit=72)


def sample_signals_for_asset(asset: str, asset_cfg: dict) -> list[KalshiDailySample]:
    series = asset_cfg.get("series")
    source, symbol = _resolve_data_source(asset_cfg)
    if not series or not symbol:
        return []

    # Per-asset horizon. Crypto daily settles ~1 day out; non-crypto
    # series like KXINX (S&P 500) settle ~5 days out (weekly Friday
    # close). Asset config can override; default keeps existing
    # 86400 (1 day) behavior for crypto.
    horizon_secs = int(asset_cfg.get("max_horizon_seconds", 86400))
    raw_markets = discover_daily_markets(series, max_seconds_out=horizon_secs)
    if not raw_markets:
        return []

    spot = _fetch_spot_unified(asset_cfg)
    if spot is None:
        log_event("kalshi_daily", "spot_unavailable",
                  {"asset": asset, "source": source, "symbol": symbol},
                  result="degraded")
        return []

    nearby = _pick_near_spot(raw_markets, spot, NEAR_SPOT_STRIKE_COUNT)
    if not nearby:
        return []

    # Use hourly klines for vol estimation — daily horizon means we don't
    # need minute-precision history.
    klines = _fetch_klines_unified(asset_cfg)
    static_vol = float(asset_cfg.get("annual_vol", 0.55))

    # IMPROVEMENT #1 (2026-05-24): blend realized vol with static config.
    # Hardcoded annual_vol for ETH (0.65) / SOL (0.85) was likely
    # miscalibrated, causing BSM to mis-price strikes and the bot to
    # trade against bad estimates. Realized vol from the last ~3 days
    # of hourly bars adapts to current regime. Floor at 70% of static
    # so a calm-but-fake-quiet period doesn't blow up tail probabilities.
    try:
        from lib.btc_5min_signal import compute_realized_vol
        # Daily path feeds HOURLY klines → annualize with √8760 (24×365,
        # crypto 24/7), NOT the function's 1-minute default. Frequency-aware
        # since Task #139; previously this re-implemented the formula inline
        # because compute_realized_vol hardcoded the 1-min factor.
        realized_vol = compute_realized_vol(klines, periods_per_year=8760)
        if realized_vol is not None:
            # Floor at 70% of static so a quiet window doesn't blow
            # up our extreme-strike pricing. Cap at 200% of static
            # so a single big move doesn't make us refuse all trades.
            annual_vol = max(static_vol * 0.7,
                              min(static_vol * 2.0, realized_vol))
        else:
            annual_vol = static_vol
    except Exception:
        annual_vol = static_vol

    # IMPROVEMENT #2 (2026-05-24): liquidity filter via bid-ask spread.
    # Wide spreads (>8% of mid-price) typically signal that no real
    # traders are present — fills happen at the bot's expense even
    # when math says edge exists. Per-asset can override; default 8%.
    max_spread_pct = float(asset_cfg.get("max_spread_pct", 0.08))

    now_iso = datetime.now(timezone.utc).isoformat()
    samples: list[KalshiDailySample] = []
    # Lazy-import compute_greeks (the raw BSM helper) only when we need
    # direction-aware math — avoids the import cost on the BTC-only path.
    from lib.btc_5min_signal import compute_greeks

    # ── Anti-whale defense (2026-05-25 PM) ─────────────────────────────
    # Collect cycle whale_pressure ONCE per asset scan (not per market) —
    # an 8s WS call is fine at scan cadence (~10 min) but we don't want
    # to repeat for every strike. Stash on asset_cfg so compute_indicators
    # picks it up via the `_cycle_whale_pressure` key.
    #
    # Skipped on non-crypto assets (no Coinbase WS for those).
    asset_cfg["_cycle_whale_pressure"] = None
    if source == "binance" and symbol:
        try:
            from lib.whale_monitor import (
                collect_whale_trades, whale_pressure_to_indicator_value,
            )
            _snap = collect_whale_trades(symbol=symbol)
            _wp = whale_pressure_to_indicator_value(_snap)
            asset_cfg["_cycle_whale_pressure"] = _wp
            log_event("kalshi_daily", "cycle_whale_pressure",
                      {"asset": asset, "whale_pressure": _wp,
                       "n_whales": _snap.n_whales,
                       "buy_vol": _snap.buy_vol_usd,
                       "sell_vol": _snap.sell_vol_usd})
        except Exception as e:
            log_event("kalshi_daily", "whale_collect_failed",
                      {"asset": asset, "error": str(e)[:200]},
                      result="degraded")

    # ── Kronos foundation-model signal (2026-05-27) ──────────────────────
    # Opt-in via YAML: set `kronos_enabled: true` in kalshi_daily_strategy.yaml.
    # Default off so the bot ships unchanged until validated. When enabled,
    # we get a [-1, +1] orthogonal direction signal from a trained transformer
    # — complements the analytic BSM model. ~3s per cycle once warm.
    asset_cfg["_cycle_kronos_signal"] = None
    try:
        from lib.kalshi_daily_paper import _effective_params
        _kp = _effective_params()
        if _kp.get("kronos_enabled", False) and source == "binance" and symbol:
            from lib.kronos_signal import predict_direction as _kr_predict
            # Pass our hourly klines. We predict ~4 hours ahead by default
            # — roughly matches the most actionable subset of daily Kalshi
            # contracts (intra-day closes that are close enough for the
            # signal to be informative).
            _kr_horizon_h = float(_kp.get("kronos_hours_ahead", 4.0))
            _kr_variant   = str(_kp.get("kronos_model_variant", "mini"))
            _kr_signal    = _kr_predict(
                klines, hours_ahead=_kr_horizon_h, model_variant=_kr_variant,
            )
            asset_cfg["_cycle_kronos_signal"] = _kr_signal
            log_event("kalshi_daily", "cycle_kronos_signal",
                      {"asset": asset, "kronos_signal": _kr_signal,
                       "horizon_h": _kr_horizon_h, "variant": _kr_variant})
    except Exception as e:
        log_event("kalshi_daily", "kronos_collect_failed",
                  {"asset": asset, "error": str(e)[:200]},
                  result="degraded")

    for m in nearby:
        strike = float(m["_parsed_strike"])
        direction = m.get("_direction", "above")
        floor = m.get("_floor_strike")
        cap = m.get("_cap_strike")
        secs_close = float(m["_seconds_to_close"])
        hours_to_close = max(secs_close / 3600.0, 0.0)

        # Kalshi's daily-market response carries pricing in dollar-decimal
        # fields (*_dollars) — NOT the integer-cents fields the 15-min API
        # uses. Read both shapes so we work across surfaces.
        def to_frac(v):
            if v is None:
                return None
            f = float(v)
            return f if f <= 1.0 else f / 100.0  # already-fractional OR cents
        yes_ask = to_frac(m.get("yes_ask_dollars") if m.get("yes_ask_dollars") is not None else m.get("yes_ask"))
        yes_bid = to_frac(m.get("yes_bid_dollars") if m.get("yes_bid_dollars") is not None else m.get("yes_bid"))
        no_ask  = to_frac(m.get("no_ask_dollars")  if m.get("no_ask_dollars")  is not None else m.get("no_ask"))
        no_bid  = to_frac(m.get("no_bid_dollars")  if m.get("no_bid_dollars")  is not None else m.get("no_bid"))
        market_yes = yes_ask

        # IMPROVEMENT #2 spread filter — skip illiquid strikes where the
        # bid-ask spread eats theoretical edge. Mid = (yes_ask+yes_bid)/2,
        # spread_pct = (ask - bid) / mid. Wide spreads usually mean no
        # real traders are quoting; we'd fill at our expense. 8% is
        # reasonable for crypto daily (BTC typically <2%, ETH ~3-5%).
        if yes_ask is not None and yes_bid is not None and yes_ask > yes_bid > 0:
            mid = (yes_ask + yes_bid) / 2.0
            spread_pct = (yes_ask - yes_bid) / mid if mid > 0 else 1.0
            if spread_pct > max_spread_pct:
                # Skip the market entirely — won't generate a sample
                continue

        # The indicators helper assumes "above" semantics (P(S > K)). For
        # below/between markets we still call it for the COMPOSITE/CONFIDENCE
        # signal (those are direction-agnostic — they measure information
        # content, not directional probability) — but we OVERRIDE
        # theoretical_yes with the direction-correct value computed
        # separately. Otherwise sizing flips sign on SPY/INX.
        #
        # 2026-05-25 PM HALT FIX: pass per-asset calibration into the
        # indicator so theo_delta_gap uses the CORRECTED theo_yes
        # (fixes the structural YES bias that was bleeding live).
        # Also widen the gap saturation from 0.10 → 0.20 (daily-strike
        # gaps are routinely 0.15-0.30; 0.10 was pinning the contribution
        # to +4.0 on every sample). And dampen RSI from 3.0 → 1.5 because
        # at the hours-long daily horizon BTC can stay "oversold" the
        # entire window, contributing constant YES bias.
        try:
            from lib.kalshi_daily_calibration import get_correction
            _cal = get_correction(asset)
            _cf = float(_cal.get("correction_factor", 1.0) or 1.0)
        except Exception:
            _cf = 1.0
        indicators = compute_indicators_for_window(
            klines=klines, window_open_price=strike, current_spot=spot,
            hours_to_close=hours_to_close, market_yes_price=market_yes,
            annual_vol=annual_vol,
            # 2026-05-29 CRITICAL VOL FIX (Task #105): do NOT let the indicator
            # re-derive realized vol here. compute_realized_vol() annualizes
            # with √525600 — it assumes 1-MINUTE klines — but this daily path
            # feeds HOURLY klines, inflating vol by √60 ≈ 7.75× (observed
            # effective vol ~2.6 / 260% vs BTC's true ~0.45). That uncapped
            # value overrode the correct vol, flattening the BSM theo S-curve
            # ~3× and manufacturing systematic false NO edges on above-strike
            # markets (root cause of the 40% live WR). The daily caller above
            # has ALREADY computed a correctly-annualized (√8760), capped,
            # realized-blended `annual_vol` — use THAT. Offline replay on 4,772
            # historical samples: mean |theo−market| error 0.193 → 0.027 (-86%).
            use_realized_vol=False,
            # 2026-05-25 PM anti-whale defense: pass cycle whale_pressure
            # if available. Computed once per scan by the caller and
            # cached on the asset config — saves 8s × N markets of WS calls.
            whale_pressure=asset_cfg.get("_cycle_whale_pressure"),
            # 2026-05-27: optional Kronos foundation-model signal. Off by
            # default (signal=None). When kronos_enabled YAML is true, we
            # pass the [-1,+1] predicted direction with weight 2.5 (slightly
            # less than whale_pressure's 3.0 — Kronos is research-grade and
            # we want it to influence, not dominate, the composite).
            kronos_signal=asset_cfg.get("_cycle_kronos_signal"),
            kronos_weight=(2.5 if asset_cfg.get("_cycle_kronos_signal") is not None else 0.0),
            orderflow_signal=None, orderflow_weight=0.0,
            funding_signal=None, funding_weight=0.0,
            # Daily-horizon tuning (15-min path keeps defaults via its own caller)
            theo_yes_correction_factor=_cf,
            theo_delta_gap_saturation=0.20,
            rsi_weight=1.5,
        )
        # Stamp the cycle whale_pressure onto every sample so downstream
        # gates (paper module's whale-veto) can use it without re-fetching.
        indicators["cycle_whale_pressure"] = asset_cfg.get("_cycle_whale_pressure")

        # Direction-aware theoretical_yes
        #
        # 2026-06-01 FIX (#165): for below/between markets we must recompute
        # the WHOLE composite from the direction-correct theo, not just patch
        # indicators['theoretical_yes']. compute_indicators_for_window was
        # called with above-semantics (theo=P(S>strike), gap vs the market's
        # yes_ask which prices P(S<=cap)/P(in-band)). Its theo_delta_gap term
        # (weight 4 — the dominant contributor) therefore had the WRONG SIGN,
        # and composite/confidence/direction were built on it. The old code
        # patched only theoretical_yes, leaving the sign-inverted composite to
        # drive side selection downstream (sign(composite) => YES/NO). This
        # rebuilds theo_delta_gap + composite + confidence + direction from the
        # corrected, calibrated theo so the chosen side matches the real edge.
        # NOTE: 'above' (all crypto incl. live BTC) is untouched — pass-through.
        def _recompute_directional(dir_theo_raw: float) -> None:
            """Re-derive composite/confidence/direction for a non-above market
            from a direction-correct RAW theo (P of the market's YES event).
            Applies the same per-asset calibration + saturation the 'above'
            path uses, so below/between are treated identically to above."""
            corrected = max(0.02, min(0.98, float(dir_theo_raw) * _cf))
            indicators["theoretical_yes_raw"] = round(float(dir_theo_raw), 6)
            indicators["theoretical_yes"] = corrected
            mkt = market_yes  # the market's YES ask (matches this YES event)
            if mkt is None:
                return
            gap = corrected - float(mkt)
            indicators["theo_yes_gap"] = gap
            contribs = dict(indicators.get("contribs") or {})
            # Same saturation (0.20) + weight (4.0) as compute_indicators.
            new_gap_contrib = max(-1.0, min(1.0, gap / 0.20)) * 4.0
            contribs["theo_delta_gap"] = new_gap_contrib
            indicators["contribs"] = contribs
            composite = sum(contribs.values())
            indicators["composite"] = composite
            mp = indicators.get("max_possible") or 0.0
            indicators["confidence"] = abs(composite) / mp if mp > 0 else 0.0
            indicators["direction"] = (
                "UP" if composite > 0 else "DOWN" if composite < 0 else "FLAT")

        if direction == "above":
            # Default path — indicators already computed for P(S > strike).
            # Live BTC always lands here; behavior is byte-for-byte unchanged.
            pass
        elif direction == "below" and cap is not None:
            # P(S ≤ K) = 1 - P(S > K)
            g = compute_greeks(spot=spot, strike=cap,
                                hours_to_close=hours_to_close,
                                annual_vol=annual_vol)
            if g is not None:
                _recompute_directional(1.0 - float(g["theoretical_yes"]))
        elif direction == "between" and floor is not None and cap is not None:
            # P(floor ≤ S ≤ cap) = P(S > floor) - P(S > cap)
            g_lo = compute_greeks(spot=spot, strike=floor,
                                   hours_to_close=hours_to_close,
                                   annual_vol=annual_vol)
            g_hi = compute_greeks(spot=spot, strike=cap,
                                   hours_to_close=hours_to_close,
                                   annual_vol=annual_vol)
            if g_lo is not None and g_hi is not None:
                between_yes = max(0.0,
                    float(g_lo["theoretical_yes"]) - float(g_hi["theoretical_yes"]))
                _recompute_directional(between_yes)

        indicators["strike"] = strike
        indicators["current_spot"] = spot
        indicators["market_direction"] = direction
        indicators["floor_strike"] = floor
        indicators["cap_strike"] = cap

        samples.append(KalshiDailySample(
            sample_at=now_iso, asset=asset,
            market_ticker=m.get("ticker", ""),
            event_ticker=m.get("event_ticker", ""),
            title=(m.get("title") or "")[:200],
            open_time=m.get("open_time", ""),
            close_time=m.get("_close_iso", ""),
            seconds_to_close=secs_close, strike=strike,
            yes_bid=yes_bid, yes_ask=yes_ask,
            no_bid=no_bid,   no_ask=no_ask,
            last_price=to_frac(m.get("last_price_dollars") or m.get("last_price")),
            volume_24h=float(m.get("volume_24h_fp", m.get("volume_24h", 0)) or 0),
            spot_usd=spot, indicators=indicators,
            distance_to_spot_pct=round(((spot / strike) - 1) * 100, 4),
        ))
    return samples


def sample_signals() -> list[KalshiDailySample]:
    out: list[KalshiDailySample] = []
    for asset, cfg in enabled_assets().items():
        try:
            out.extend(sample_signals_for_asset(asset, cfg))
        except Exception as e:
            log_event("kalshi_daily", "asset_sample_failed",
                      {"asset": asset, "error": str(e)[:200]}, result="degraded")
    # Sort by seconds_to_close (most urgent first) for easier consumption
    out.sort(key=lambda s: s.seconds_to_close)
    return out


SIGNAL_PATH = Path(__file__).parent.parent / "data" / "kalshi_daily_signal.jsonl"


def persist_samples(samples: list[KalshiDailySample]) -> None:
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_PATH, "a") as f:
        for s in samples:
            f.write(json.dumps(asdict(s)) + "\n")
    # Bounded retention (diagnostic tail; was growing unbounded -> 17MB+).
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
            from lib.kalshi_daily_paper import record_paper_trades_from_samples
            cfg = load_assets_config()
            from collections import defaultdict
            by_asset = defaultdict(list)
            for s in samples:
                by_asset[s.asset].append(asdict(s))
            for asset, asset_samples in by_asset.items():
                min_conf = float(cfg.get(asset, {}).get("min_confidence", 0.20))
                new = record_paper_trades_from_samples(asset_samples, min_confidence=min_conf)
                n_paper += len(new)
        except Exception as e:
            log_event("kalshi_daily", "paper_record_failed",
                      {"error": str(e)[:200]}, result="degraded")

    settle_summary = {}
    if settle_paper_trades:
        try:
            from lib.kalshi_daily_paper import settle_paper_trades as _settle
            settle_summary = _settle()
        except Exception as e:
            log_event("kalshi_daily", "paper_settle_failed",
                      {"error": str(e)[:200]}, result="degraded")

    if samples:
        nearest = samples[0]
        log_event("kalshi_daily", "signal_cycle", {
            "n_markets": len(samples),
            "n_assets": len({s.asset for s in samples}),
            "assets": sorted({s.asset for s in samples}),
            "nearest_asset": nearest.asset,
            "nearest_strike": nearest.strike,
            "nearest_seconds_to_close": nearest.seconds_to_close,
            "paper_trades_opened": n_paper,
            "paper_settled": settle_summary.get("settled_now", 0),
        })
    else:
        log_event("kalshi_daily", "no_active_markets",
                  {"enabled_assets": sorted(enabled_assets().keys())},
                  result="degraded")

    return {
        "n_markets": len(samples),
        "paper_trades_opened": n_paper,
        "settle_summary": settle_summary,
        "samples": [asdict(s) for s in samples],
    }
