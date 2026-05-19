"""
Kalshi 15-min BTC signal — parallel to ``btc_5min_signal.py`` but for
Kalshi's KXBTC15M series. The mechanic is structurally identical:

  * Each market opens with a strike = BTC price at window-open
  * Title: "BTC price up in next 15 mins?"
  * strike_type: greater_or_equal → YES wins if BTC ≥ strike at close
  * 15-minute window, auto-settling on Binance.US-derived index

Why a separate module from ``btc_5min_signal``:
  * Different exchange API (Kalshi vs Polymarket Gamma)
  * Different ticker structure (KXBTC15M-<YYMMM><DD><HHMM> vs slug)
  * Strike comes from market metadata directly (no need to infer the
    window-open price from klines — Kalshi tells us exactly)
  * 15-minute windows give us 3× more klines to work with, so RSI
    and EMA cross are more reliable signals here than on Polymarket

Auth: market browsing is PUBLIC — no API key needed for sampling.
Phase 3 (real orders) will use the kalshi_auth signing path.

Persistence: ``data/kalshi_15min_signal.jsonl`` (mirrors the
Polymarket-side jsonl so the same analysis tooling works on both).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from tradingcore.audit import log_event
from lib.btc_5min_signal import (
    fetch_binance_btc_price, fetch_binance_klines,
    compute_indicators_for_window,
)

SIGNAL_PATH = Path(__file__).parent.parent / "data" / "kalshi_15min_signal.jsonl"
KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"
ASSETS_CONFIG_PATH = Path(__file__).parent.parent / "config" / "kalshi_assets.yaml"


def load_assets_config() -> dict:
    """Read the asset registry YAML. Returns a dict keyed by asset
    shortname ("btc", "eth", "sol", ...) → {series, binance_symbol,
    min_confidence, enabled}. Empty dict on parse failure (caller
    handles gracefully).
    """
    import yaml
    if not ASSETS_CONFIG_PATH.exists():
        return {}
    try:
        with open(ASSETS_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("assets", {}) or {}
    except (yaml.YAMLError, OSError) as e:
        log_event("kalshi_15min", "assets_config_load_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return {}


def enabled_assets() -> dict:
    """Return only the assets with ``enabled: true``."""
    return {
        k: v for k, v in load_assets_config().items()
        if v and v.get("enabled")
    }


@dataclass
class KalshiFifteenMinSample:
    """One snapshot of one 15-min Kalshi crypto market.

    ``asset`` identifies which crypto this is (btc/eth/sol/...) so the
    paper module and reports can demux per asset. ``spot_usd`` is the
    price of THAT asset's underlying on Binance.US — not BTC.
    """
    sample_at: str
    asset: str                   # "btc", "eth", "sol", etc — registry key
    market_ticker: str           # e.g. KXBTC15M-26MAY150830-30
    event_ticker: str            # parent event
    title: str
    open_time: str               # ISO from Kalshi
    close_time: str              # ISO from Kalshi
    seconds_to_close: float
    strike: float                # floor_strike — underlying at window-open
    yes_bid: float | None        # dollars, 0..1
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume_24h: float
    spot_usd: float              # underlying's spot at sample time
    indicators: dict | None = None


# ── Discovery ────────────────────────────────────────────────────────

def discover_15min_markets(
    series_ticker: str,
    *,
    max_seconds_out: int = 900,
) -> list[dict]:
    """Find currently-live Kalshi 15-min markets for ``series_ticker``.

    Two-step fetch — events first, then markets per event — because
    Kalshi separates the date container (event) from the strike-priced
    binary (market). Asset-agnostic: pass KXBTC15M, KXETH15M, KXSOL15M,
    etc. Default ``max_seconds_out=900`` covers a full 15-min window.
    """
    import requests

    try:
        r = requests.get(
            f"{KALSHI_HOST}/events",
            params={"series_ticker": series_ticker,
                    "status": "open", "limit": 50},
            timeout=15,
        )
        r.raise_for_status()
        events = r.json().get("events", []) or []
    except Exception as e:
        log_event("kalshi_15min", "events_fetch_failed",
                  {"series": series_ticker, "error": str(e)[:200]},
                  result="degraded")
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
            close_iso = m.get("close_time") or ""
            try:
                close_dt = datetime.fromisoformat(
                    close_iso.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                continue
            seconds_to_close = (close_dt - now).total_seconds()
            if seconds_to_close < -60 or seconds_to_close > max_seconds_out:
                continue

            strike = m.get("floor_strike")
            if strike is None:
                continue

            # Kalshi has both *_bid/*_ask in cents and *_bid_dollars/*_ask_dollars
            # as the canonical 0..1 floats. Prefer the dollars form.
            def _as_price(val):
                if val is None:
                    return None
                try:
                    f = float(val)
                except (ValueError, TypeError):
                    return None
                # If looks like cents (>1.0), convert
                return f / 100.0 if f > 1.5 else f

            qualified.append({
                "ticker": m.get("ticker", ""),
                "event_ticker": et,
                "title": m.get("title", "")[:200],
                "open_time": m.get("open_time", ""),
                "close_time": close_iso,
                "seconds_to_close": seconds_to_close,
                "strike": float(strike),
                "yes_bid": _as_price(m.get("yes_bid_dollars")
                                     if m.get("yes_bid_dollars") is not None
                                     else m.get("yes_bid")),
                "yes_ask": _as_price(m.get("yes_ask_dollars")
                                     if m.get("yes_ask_dollars") is not None
                                     else m.get("yes_ask")),
                "no_bid": _as_price(m.get("no_bid_dollars")
                                    if m.get("no_bid_dollars") is not None
                                    else m.get("no_bid")),
                "no_ask": _as_price(m.get("no_ask_dollars")
                                    if m.get("no_ask_dollars") is not None
                                    else m.get("no_ask")),
                "last_price": _as_price(m.get("last_price_dollars")
                                        if m.get("last_price_dollars") is not None
                                        else m.get("last_price")),
                "volume_24h": float(m.get("volume_24h_fp", 0) or 0),
            })
    qualified.sort(key=lambda x: x["seconds_to_close"])
    return qualified


# ── Indicators (15-min variant) ──────────────────────────────────────

def compute_kalshi_indicators(
    *,
    klines: list[dict],
    strike: float,
    current_spot: float,
    hours_to_close: float | None = None,
    market_yes_price: float | None = None,
    annual_vol: float = 0.55,
    whale_pressure: float | None = None,
    asset: str = "btc",
    asset_cfg: dict | None = None,
) -> dict:
    """Compute the 4-indicator composite for a Kalshi 15-min market.

    Strike IS the window-open price (Kalshi pegs it there at market
    creation), so the Greeks helper uses it directly as the BSM strike.

    Indicators: RSI, theo_delta_gap, market_agreement, whale_pressure,
    (optional) Kronos foundation-model forecast.

    When `asset_cfg.kronos.enabled = true`, this also calls Kronos
    (lib/kalshi_kronos.py) to get a 5th signal: P(close > strike) from
    Monte Carlo path simulation. The result is logged on the sample
    so calibration analysis can compare Kronos's view to actual outcomes.

    Direction labels are YES/NO since Kalshi markets are framed as
    "BTC ≥ strike at close?" rather than "BTC up?".
    """
    # Optional Kronos 5th-signal lookup. Failures are absorbed and the
    # composite proceeds without Kronos (returns None signal).
    kronos_signal: float | None = None
    kronos_meta: dict = {}
    if asset_cfg and asset_cfg.get("kronos", {}).get("enabled"):
        try:
            from lib.kalshi_kronos import kronos_signed_signal
            kronos_signal, kronos_meta = kronos_signed_signal(
                strike=strike,
                horizon_bars=int(asset_cfg["kronos"].get("horizon_bars", 3)),
                interval=asset_cfg["kronos"].get("interval", "5m"),
                sample_count=int(asset_cfg["kronos"].get("sample_count", 5)),
                ticker=asset_cfg["kronos"].get("ticker", "BTC-USD"),
                model_size=asset_cfg["kronos"].get("model_size", "small"),
            )
        except Exception:
            kronos_signal = None
            kronos_meta = {"reason": "kronos_unavailable"}

    kronos_weight = float(
        (asset_cfg or {}).get("kronos", {}).get("weight", 3.0)
    )

    # PARALLEL PREFETCH: OFI and funding are independent REST calls;
    # firing them concurrently drops wall time from sum(individual)
    # to max(individual). MTF is included too so the same prefetch
    # serves the downstream paper-trader gate (no extra latency).
    # Falls open silently — any failure leaves that signal as None.
    of_enabled = bool((asset_cfg or {}).get("orderflow", {}).get("enabled"))
    fund_enabled = bool((asset_cfg or {}).get("funding_rate", {}).get("enabled"))
    of_symbol = (asset_cfg or {}).get("orderflow", {}).get("symbol", "BTCUSDT")
    of_depth = int((asset_cfg or {}).get("orderflow", {}).get("depth_levels", 10))

    of_signal: float | None = None
    of_meta: dict = {}
    funding_signal: float | None = None
    funding_meta: dict = {}

    if of_enabled or fund_enabled:
        try:
            from lib.kalshi_prefetch import prefetch_market_data
            data = prefetch_market_data(
                symbol=of_symbol,
                primary_direction=None,    # MTF deferred to paper-trader gate
                enable_orderflow=of_enabled,
                enable_funding=fund_enabled,
                enable_mtf=False,
                orderflow_depth=of_depth,
            )
            if of_enabled:
                of_signal, of_meta = data["orderflow"]
            if fund_enabled:
                funding_signal, funding_meta = data["funding"]
        except Exception:
            of_signal = None; of_meta = {"reason": "prefetch_failed"}
            funding_signal = None; funding_meta = {"reason": "prefetch_failed"}

    of_weight = float(
        (asset_cfg or {}).get("orderflow", {}).get("weight", 2.0)
    )
    funding_weight = float(
        (asset_cfg or {}).get("funding_rate", {}).get("weight", 1.5)
    )

    base = compute_indicators_for_window(
        klines=klines,
        window_open_price=strike,
        current_spot=current_spot,
        hours_to_close=hours_to_close,
        market_yes_price=market_yes_price,
        annual_vol=annual_vol,
        whale_pressure=whale_pressure,
        kronos_signal=kronos_signal,
        kronos_weight=kronos_weight,
        orderflow_signal=of_signal,
        orderflow_weight=of_weight,
        funding_signal=funding_signal,
        funding_weight=funding_weight,
    )
    base["strike"] = strike
    base["current_spot"] = current_spot
    base["direction"] = (
        "YES" if base["composite"] > 0
        else "NO" if base["composite"] < 0
        else "FLAT"
    )
    base["kronos_meta"] = kronos_meta
    base["orderflow_meta"] = of_meta
    base["funding_meta"] = funding_meta
    return base


# ── Sampling ─────────────────────────────────────────────────────────

def sample_signals_for_asset(
    asset: str,
    asset_cfg: dict,
    *,
    max_seconds_out: int = 900,
    with_indicators: bool = True,
) -> list[KalshiFifteenMinSample]:
    """Sample every live Kalshi market for ONE asset.

    Caller passes the asset shortname + its registry entry. Returns
    the per-asset samples — empty list if no markets are live or
    Binance fetch fails (caller logs + moves on).
    """
    series = asset_cfg.get("series")
    binance_symbol = asset_cfg.get("binance_symbol")
    annual_vol = float(asset_cfg.get("annual_vol", 0.55))
    if not series or not binance_symbol:
        return []

    spot = fetch_binance_btc_price(symbol=binance_symbol)
    if spot is None:
        return []
    markets = discover_15min_markets(series, max_seconds_out=max_seconds_out)
    if not markets:
        return []

    klines = (
        fetch_binance_klines(symbol=binance_symbol)
        if with_indicators else None
    )

    # Whale snapshot — one brief WebSocket connection per asset per
    # cycle. Computed once and reused across all markets for this asset
    # (all KXBTC15M markets share the same underlying Binance order
    # flow). Costs ~8s of wallclock; cron has 60s budget so this fits.
    whale_indicator_value: float | None = None
    if with_indicators and klines:
        try:
            from lib.whale_monitor import (
                collect_whale_trades, whale_pressure_to_indicator_value,
            )
            snap = collect_whale_trades(symbol=binance_symbol)
            whale_indicator_value = whale_pressure_to_indicator_value(snap)
        except Exception as e:
            log_event("kalshi_15min", "whale_collect_failed",
                      {"symbol": binance_symbol, "error": str(e)[:200]},
                      result="degraded")

    now_iso = datetime.now(timezone.utc).isoformat()
    out: list[KalshiFifteenMinSample] = []
    for m in markets:
        indicators = None
        if klines:
            # Pick a single market-YES estimate to compare against the
            # Greeks model. Preference order: last trade price (most
            # informative), then mid of bid/ask, then yes_ask alone.
            yes_bid = m.get("yes_bid")
            yes_ask = m.get("yes_ask")
            last_price = m.get("last_price")
            if last_price is not None:
                market_yes = float(last_price)
            elif yes_bid is not None and yes_ask is not None:
                market_yes = (float(yes_bid) + float(yes_ask)) / 2.0
            elif yes_ask is not None:
                market_yes = float(yes_ask)
            else:
                market_yes = None
            hours_to_close = max(m["seconds_to_close"] / 3600.0, 0.0)

            indicators = compute_kalshi_indicators(
                klines=klines,
                strike=m["strike"],
                current_spot=spot,
                hours_to_close=hours_to_close,
                market_yes_price=market_yes,
                annual_vol=annual_vol,
                whale_pressure=whale_indicator_value,
                asset=asset,
                asset_cfg=asset_cfg,
            )
        out.append(KalshiFifteenMinSample(
            sample_at=now_iso,
            asset=asset,
            market_ticker=m["ticker"],
            event_ticker=m["event_ticker"],
            title=m["title"],
            open_time=m["open_time"],
            close_time=m["close_time"],
            seconds_to_close=round(m["seconds_to_close"], 2),
            strike=m["strike"],
            yes_bid=m["yes_bid"], yes_ask=m["yes_ask"],
            no_bid=m["no_bid"], no_ask=m["no_ask"],
            last_price=m["last_price"],
            volume_24h=m["volume_24h"],
            spot_usd=spot,
            indicators=indicators,
        ))
    return out


def sample_signals(
    *,
    max_seconds_out: int = 900,
    with_indicators: bool = True,
) -> list[KalshiFifteenMinSample]:
    """Multi-asset sweep — every enabled asset in config/kalshi_assets.yaml.

    Single round trip to Binance.US + Kalshi per asset (no batching;
    Binance has separate symbols, Kalshi has separate series).
    Empty list if no assets enabled or no markets live anywhere.
    """
    out: list[KalshiFifteenMinSample] = []
    for asset, cfg in enabled_assets().items():
        out.extend(sample_signals_for_asset(
            asset, cfg,
            max_seconds_out=max_seconds_out,
            with_indicators=with_indicators,
        ))
    # Sort by closest-to-resolving across all assets
    out.sort(key=lambda s: s.seconds_to_close)
    return out


def persist_samples(samples: list[KalshiFifteenMinSample]) -> None:
    if not samples:
        return
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_PATH, "a") as f:
        for s in samples:
            f.write(json.dumps(asdict(s)) + "\n")


def run_signal_cycle(
    *,
    max_seconds_out: int = 900,
    record_paper_trades: bool = True,
    settle_paper_trades: bool = True,
) -> dict:
    """Full multi-asset sweep: every enabled asset → discover + sample
    + persist + (paper record with per-asset min_confidence + settle).

    Per-asset ``min_confidence`` from the registry: BTC=0.35, ETH=0.45,
    SOL=0.50 by default. ETH/SOL spreads are wider so a higher bar
    avoids fee drag eating any edge.
    """
    samples = sample_signals(max_seconds_out=max_seconds_out)
    persist_samples(samples)

    # Group samples by asset for per-asset min_confidence
    by_asset: dict[str, list] = {}
    for s in samples:
        by_asset.setdefault(s.asset, []).append(asdict(s))

    n_paper_opened = 0
    cfg = load_assets_config()
    if record_paper_trades and samples:
        try:
            from lib.kalshi_15min_paper import record_paper_trades_from_samples
            for asset, asset_samples in by_asset.items():
                asset_min_conf = float(
                    cfg.get(asset, {}).get("min_confidence", 0.35)
                )
                new_trades = record_paper_trades_from_samples(
                    asset_samples, min_confidence=asset_min_conf,
                )
                n_paper_opened += len(new_trades)
        except Exception as e:
            log_event("kalshi_15min", "paper_record_failed",
                      {"error": str(e)[:200]}, result="degraded")

    # Check open paper trades for intra-window exit BEFORE settling.
    # Order matters: a trade hitting TP/SL mid-window should lock
    # in profit/cut loss; the settlement path will then skip it
    # because status is no longer "open".
    exit_summary = {}
    try:
        from lib.kalshi_15min_paper import check_open_trades_for_exit
        exit_summary = check_open_trades_for_exit()
    except Exception as e:
        log_event("kalshi_15min", "intra_window_check_failed",
                  {"error": str(e)[:200]}, result="degraded")

    settle_summary = {}
    if settle_paper_trades:
        try:
            from lib.kalshi_15min_paper import (
                settle_paper_trades as _settle_paper_trades,
            )
            settle_summary = _settle_paper_trades()
        except Exception as e:
            log_event("kalshi_15min", "paper_settle_failed",
                      {"error": str(e)[:200]}, result="degraded")

    if samples:
        nearest = samples[0]
        log_event("kalshi_15min", "signal_cycle", {
            "n_markets": len(samples),
            "n_assets": len(by_asset),
            "assets": sorted(by_asset.keys()),
            "nearest_asset": nearest.asset,
            "nearest_seconds_to_close": nearest.seconds_to_close,
            "nearest_strike": nearest.strike,
            "paper_trades_opened": n_paper_opened,
            "paper_intra_window_tp": exit_summary.get("tp_exits", 0),
            "paper_intra_window_sl": exit_summary.get("sl_exits", 0),
            "paper_settled": settle_summary.get("settled_now", 0),
        })
    else:
        log_event("kalshi_15min", "no_active_markets",
                  {"enabled_assets": sorted(enabled_assets().keys())},
                  result="degraded")

    return {
        "n_markets": len(samples),
        "n_assets": len(by_asset),
        "by_asset_counts": {a: len(s) for a, s in by_asset.items()},
        "nearest_seconds_to_close": samples[0].seconds_to_close if samples else None,
        "samples": [asdict(s) for s in samples],
        "paper_trades_opened": n_paper_opened,
        "settle_summary": settle_summary,
    }
