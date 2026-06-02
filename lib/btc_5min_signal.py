"""
BTC 5-min UP/DOWN signal — the strategy the Gravia post was actually about.

Polymarket auto-generates a fresh BTC binary every 5 minutes. The question
asks "will BTC be higher or lower than the opening price when this
5-minute window closes?" Resolution is automatic and near-instant once
the window closes — no UMA dispute, no waiting days.

Why this is different from ``btc_arb_signal.py``:
  * That module handles daily-strike markets ("BTC above $XX,000 on May 17")
    using a lognormal-vol approximation across days.
  * This module handles 5-minute UP/DOWN markets where the question is
    "did BTC just move up or down". No vol model needed — we know the
    opening price (snapshot at window-open) and can compute "if BTC
    stays flat from here, where would the market be?"

Discovery:
  Gamma surfaces these via ``closed=false, active=true``. The slug
  pattern is ``btc-updown-5m-<unix_ts>`` where ``<unix_ts>`` is the
  END of the 5-minute window (UTC seconds). Question pattern:
  ``"bitcoin up or down - <date>, <hh:mm>-<hh:mm> et"``.

Per-window data we persist:
  * ``data/btc_5min_signal.jsonl`` — one row per sample, one sample
    per market per cycle. Append-only.

Phase 1 (this module) is **measurement only**: snapshot + persist.
Phase 2 will compute gap vs implied-fair-price and record paper trades.
Phase 3 will run live with a T-minus-10-seconds entry rule (proven
pattern across the open-source latency bots — by T-10s the direction
is ~locked, accuracy spikes, and the market hasn't fully repriced).

Reference strategy (synthesized from open-source competitors) — what
Phase 2 should implement, in priority order of signal weight:

  1. **Window Delta** (weight 5-7): current spot vs window-open spot.
     Moves > 0.10% are highest-confidence directional signal. Need to
     snapshot Binance at the window-start tick to know the open.
  2. **Real-time Tick Trend** (weight 2): 2-second polling for
     micro-trends between 1-min candle updates.
  3. **Micro Momentum** (weight 2): direction of last two 1-min candles.
  4. **Acceleration** (weight 1.5): is momentum building or fading?
  5. **EMA 9/21 Crossover** (weight 1).
  6. **RSI 14** (weight 1-2): extremes weighted higher.
  7. **Volume Surge** (weight 1): 1.5x recent average confirms direction.

Risk caps (conservative defaults — Phase 3 will tune):
  * Per-trade size: 1-2% of bankroll (NOT 25% — that's the "Safe" mode
    of the reference bots, which is aggressive by finance-standard math)
  * Min confidence to fire: 50% of max composite score
  * Hard deadline: T-5 seconds (no trades after that)
  * Polymarket taker fee: up to 1.56% (Feb 2026 update)
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from tradingcore.audit import log_event

SIGNAL_PATH = Path(__file__).parent.parent / "data" / "btc_5min_signal.jsonl"

BINANCE_US_TICKER = "https://api.binance.us/api/v3/ticker/price"
BINANCE_US_KLINES = "https://api.binance.us/api/v3/klines"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

# Default annualized vol if caller doesn't specify. Per-asset overrides
# come from config/kalshi_assets.yaml; this is the BTC fallback.
DEFAULT_ANNUAL_VOL = 0.55

# Match `btc-updown-5m-1778883600` (capture the unix-ts suffix).
SLUG_RE = re.compile(r"^btc-updown-5m-(\d{9,11})$")


@dataclass
class FiveMinSample:
    """One snapshot of one 5-min market at one moment."""
    sample_at: str               # ISO of when we polled
    market_id: str               # Polymarket conditionId
    slug: str
    question: str
    window_end_ts: int           # unix seconds — exact resolution time
    seconds_to_close: float      # window_end_ts - now (signed; negative = closed)
    up_price: float              # current YES (=UP) price, 0..1
    down_price: float            # 1 - up_price (Polymarket binaries sum to ~1)
    spot_usd: float              # Binance.US BTC/USDT spot at sample time
    indicators: dict | None = None  # composite + raw indicator values
                                    # (None if klines fetch failed)


# ── Discovery ────────────────────────────────────────────────────────

def fetch_binance_btc_price(symbol: str = "BTCUSDT") -> float | None:
    """Single REST poll. Returns spot in USD, or None on failure.

    Default symbol is BTCUSDT for backward compat — every existing
    caller wanted BTC. New callers (ETH/SOL on Kalshi) pass their own.
    """
    import requests
    try:
        r = requests.get(
            BINANCE_US_TICKER, params={"symbol": symbol}, timeout=8,
        )
        r.raise_for_status()
        px = float(r.json()["price"])
        # Reject a non-finite tick ('NaN'/'Infinity' parse straight through
        # float()) at the source so it can't poison theo/composite downstream.
        if not math.isfinite(px) or px <= 0:
            log_event("btc_5min", "binance_bad_price",
                      {"symbol": symbol, "price": str(px)}, result="degraded")
            return None
        return px
    except Exception as e:
        log_event("btc_5min", "binance_fetch_failed",
                  {"symbol": symbol, "error": str(e)[:200]},
                  result="degraded")
        return None


def fetch_binance_klines(
    *,
    symbol: str = "BTCUSDT",
    limit: int = 50,
) -> list[dict] | None:
    """Pull recent 1-minute candles for ``symbol`` from Binance.US.

    Returns oldest→newest list of dicts so indicators iterate forward
    naturally. ``limit=50`` gives EMA21 ample headroom and RSI14 + 3-tick
    momentum a clean tail. None on failure.
    """
    import requests
    try:
        r = requests.get(
            BINANCE_US_KLINES,
            params={"symbol": symbol, "interval": "1m", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log_event("btc_5min", "klines_fetch_failed",
                  {"symbol": symbol, "error": str(e)[:200]},
                  result="degraded")
        return None
    out: list[dict] = []
    for row in raw:
        try:
            out.append({
                "open_time_ms": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        except (ValueError, IndexError, TypeError):
            continue
    return out


# ── Indicators ───────────────────────────────────────────────────────

def _ema(prices: list[float], period: int) -> float | None:
    """Exponential moving average over the last ``period``+ closes.

    Standard recursion: ema_today = α·price + (1-α)·ema_yesterday
    with α = 2/(period+1). Bootstrap from the simple average of the
    first ``period`` values, then iterate.
    """
    if len(prices) < period:
        return None
    alpha = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = alpha * p + (1 - alpha) * ema
    return ema


def _rsi(prices: list[float], period: int = 14) -> float | None:
    """Standard 14-period RSI on closes. Returns 0..100, None if too
    few samples. Uses Wilder's smoothing (the original formulation).
    """
    if len(prices) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    # Wilder smoothing for the rest
    for i in range(period + 1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _find_window_open_price(
    klines: list[dict],
    window_start_ts: int,
) -> float | None:
    """Find the 1-minute candle that opened exactly at ``window_start_ts``
    (a 5-minute boundary) and return its ``open`` price.

    Falls back to the candle with the closest open_time within ±60s if
    the exact boundary candle isn't present (Binance.US occasionally
    misses ticks).
    """
    target_ms = window_start_ts * 1000
    best: tuple[int, float] | None = None
    for k in klines:
        if k["open_time_ms"] == target_ms:
            return k["open"]
        diff = abs(k["open_time_ms"] - target_ms)
        if diff <= 60_000 and (best is None or diff < best[0]):
            best = (diff, k["open"])
    return best[1] if best else None


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf — no scipy dep."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def compute_greeks(
    *,
    spot: float,
    strike: float,
    hours_to_close: float,
    annual_vol: float = DEFAULT_ANNUAL_VOL,
) -> dict | None:
    """Black-Scholes-style binary delta for a cash-or-nothing call:
    the probability that ``spot`` exceeds ``strike`` at expiry under
    lognormal returns with drift=0.

    This IS the fair YES price for a Kalshi/Polymarket binary market
    that pays $1 if spot > strike at expiry. Drift=0 because we're
    treating the spot as a martingale over the short window —
    consistent with the risk-neutral measure used by every BSM
    derivation, and a defensible simplification for sub-hour windows
    where drift terms are dwarfed by realized vol.

      d  = [ln(S/K) + 0.5·σ²·T] / (σ·√T)
      P(S_T > K) = Φ(d − σ·√T)

    Returns None if inputs are degenerate (zero/negative OR non-finite).
    """
    # NaN/inf guard FIRST: `NaN <= 0` is False, so a NaN spot/strike/vol would
    # slip past the zero-checks below, then math.log(NaN)=NaN propagates to
    # theo_yes -> composite -> confidence (all NaN) and direction silently
    # collapses to FLAT. A malformed exchange tick ('NaN'/'Infinity' parsed via
    # float()) is exactly how that happens. Reject non-finite inputs outright.
    if not all(math.isfinite(x) for x in
               (hours_to_close, spot, strike, annual_vol)):
        return None
    if (hours_to_close <= 0 or spot <= 0 or strike <= 0
            or annual_vol <= 0):
        return None
    T = hours_to_close / (365 * 24)
    sigma_sqrt_T = annual_vol * math.sqrt(T)
    if sigma_sqrt_T <= 0:
        # Already at expiry — collapse to step function
        return {
            "theoretical_yes": 1.0 if spot > strike else 0.0,
            "T_years": T, "sigma_sqrt_T": 0.0,
        }
    d = (math.log(spot / strike) + 0.5 * annual_vol * annual_vol * T) / sigma_sqrt_T
    theo_yes = _norm_cdf(d - sigma_sqrt_T)
    return {
        "theoretical_yes": theo_yes,
        "T_years": T,
        "sigma_sqrt_T": sigma_sqrt_T,
        "d2": d - sigma_sqrt_T,
    }


def compute_realized_vol(
    klines: list[dict],
    *,
    min_samples: int = 15,
    periods_per_year: float = 525_600.0,
) -> float | None:
    """Annualized realized volatility from evenly-spaced kline closes.

    σ_per_bar = std(log returns of last N closes)
    σ_annual  = σ_per_bar × √periods_per_year

    `periods_per_year` MUST match the kline bar interval — passing the
    1-minute default on a different cadence mis-annualizes silently:
        1-minute bars → 525_600   (60 × 24 × 365)   [default]
        1-hour   bars → 8_760     (24 × 365, crypto 24/7)
        1-day    bars → 365       (or 252 for equities)
    e.g. using 525_600 on HOURLY bars inflates σ by √60 ≈ 7.75×, which
    flattens the BSM theo S-curve and manufactures false edges (the
    Task #105 daily-vol bug). Callers on non-minute data must pass the
    matching value explicitly.

    Returns None if we don't have enough samples — caller falls back
    to the configured per-asset value.
    """
    closes = [k["close"] for k in klines if k.get("close", 0) > 0]
    if len(closes) < min_samples + 1:
        return None
    rets = []
    for i in range(1, len(closes)):
        rets.append(math.log(closes[i] / closes[i - 1]))
    if not rets:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)
    sigma_per_bar = math.sqrt(var)
    return sigma_per_bar * math.sqrt(periods_per_year)


def compute_indicators_for_window(
    *,
    klines: list[dict],
    window_open_price: float | None,
    current_spot: float,
    hours_to_close: float | None = None,
    market_yes_price: float | None = None,
    annual_vol: float = DEFAULT_ANNUAL_VOL,
    use_realized_vol: bool = True,
    whale_pressure: float | None = None,
    kronos_signal: float | None = None,
    kronos_weight: float = 3.0,
    orderflow_signal: float | None = None,
    orderflow_weight: float = 2.0,
    funding_signal: float | None = None,
    funding_weight: float = 1.5,
    # ── Composite-shape knobs (2026-05-25 PM, daily-horizon halt fix) ──
    # All three default to the original 15-min-tuned values so existing
    # callers see no behavior change. The daily caller passes lower /
    # wider values to remove the YES bias that was bleeding real money.
    #
    # theo_yes_correction_factor: scales BSM theoretical_yes BEFORE the
    #   gap is computed. The daily caller passes the per-asset calibration
    #   factor (e.g., 0.85 for BTC) so the corrected probability flows
    #   into theo_delta_gap and composite — not just sizing.
    # theo_delta_gap_saturation: denominator that controls how quickly
    #   the gap pins to +/-1.0. Old 0.10 saturates on every near-money
    #   strike (real gaps are typically 0.15-0.30). Daily uses 0.20 so
    #   only genuinely large mispricings dominate.
    # rsi_weight: lowered for daily contracts. RSI mean-reversion works
    #   on minute bars but at the hours-long daily horizon BTC can stay
    #   "oversold" the entire window, contributing structural YES bias.
    theo_yes_correction_factor: float = 1.0,
    theo_delta_gap_saturation: float = 0.10,
    rsi_weight: float = 3.0,
) -> dict:
    """Lean composite: ONLY market-respecting, mean-reversion-aware,
    theoretically-grounded, or order-flow-based indicators. The
    momentum-following indicators (Window Delta, Micro Momentum,
    Acceleration, EMA Cross, Volume Surge) were removed after
    empirical proof that they caused a 22% win rate by buying
    continuation right before reversion.

    Three indicators (market_agreement disabled 2026-05-21):
      * RSI (weight 3) — only mean-reverting indicator; overbought
        → bearish, oversold → bullish.
      * theo_delta_gap (weight 4) — Greeks-based fair-value vs market
        YES, the EV-per-dollar signal.
      * whale_pressure (weight 3) — net taker-side flow from large
        recent trades on Binance.US (or 0 if unavailable). The
        latency-arb edge: if whales just moved spot and the
        prediction market hasn't repriced yet, we have direction.

    When ``use_realized_vol=True`` (default), the Greeks model uses
    realized vol derived from ``klines`` rather than the configured
    constant — adapts to current market state.

    Max possible composite: 12.0 (was 9.0 pre-whale). Asset-agnostic.
    """
    closes = [k["close"] for k in klines]

    # ── Adaptive vol ──────────────────────────────────────────────
    # Vol floor: during quiet 15-min windows, realized vol from a short
    # kline series can drop to 10-15% annualized (vs BTC's long-run
    # ~55%). The BSM Greeks model then thinks the underlying is "stuck"
    # and pegs theoretical_yes to ~85% for any spot-strike gap above
    # 0.10%, producing a structural YES bias in the composite (93% of
    # samples positive in our audit). Floor the realized vol at a
    # fraction of the configured per-asset vol so the model can't be
    # absurdly overconfident in quiet periods. 0.70 keeps the asset-
    # specific calibration (BTC=0.55→0.385, ETH=0.65→0.455, SOL=0.85→
    # 0.595) while preventing the runaway-overconfidence failure mode.
    # Hard fallback if caller passes 0 / negative — the floor below would
    # otherwise collapse to 0 and silently disable the protection.
    if annual_vol <= 0:
        annual_vol = DEFAULT_ANNUAL_VOL

    effective_vol = annual_vol
    if use_realized_vol:
        rv = compute_realized_vol(klines)
        if rv is not None and rv > 0:
            effective_vol = max(rv, annual_vol * 0.70)

    # ── RSI 14 ────────────────────────────────────────────────────
    rsi = _rsi(closes, 14)

    # ── Greeks-based theoretical-delta gap ────────────────────────
    greeks: dict | None = None
    theo_yes_gap = 0.0
    theo_yes_raw: float | None = None    # preserved for calibration learning
    if (hours_to_close is not None and hours_to_close > 0
            and market_yes_price is not None
            and window_open_price is not None):
        greeks = compute_greeks(
            spot=current_spot,
            strike=window_open_price,
            hours_to_close=hours_to_close,
            annual_vol=effective_vol,
        )
        if greeks is not None:
            theo_yes_raw = float(greeks["theoretical_yes"])
            # Apply per-asset calibration BEFORE computing the gap, so
            # the composite is built on the corrected probability — not
            # the BSM's structurally over-bullish raw estimate. Default
            # factor 1.0 → no change for callers (e.g., 15-min path).
            corrected_theo_yes = max(0.02, min(0.98,
                theo_yes_raw * float(theo_yes_correction_factor)))
            # Store both on the greeks dict so downstream code sees the
            # corrected value as `theoretical_yes` (used for sizing) and
            # raw value as `theoretical_yes_raw` (used for recording
            # outcomes into the calibration loop without double-correcting).
            greeks["theoretical_yes_raw"] = theo_yes_raw
            greeks["theoretical_yes"] = corrected_theo_yes
            theo_yes_gap = corrected_theo_yes - float(market_yes_price)

    # ── Compose ────────────────────────────────────────────────────
    contribs: dict[str, float] = {}

    # RSI: 30→+1.0 oversold (bullish), 70→-1.0 overbought (bearish).
    # Weight 1.5 → 2.0 → 3.0 (2026-05-20 PM).
    # Per-indicator WR/PnL analysis on 56 historical trades:
    #   RSI oversold (-2.1, -1.0) on YES: n=11, 90.9% WR, +$7.55
    # → the most predictive single indicator in the composite. Bumping
    # weight 2.0 → 3.0 so its conviction translates more directly into
    # composite when it's strongly oversold/overbought. max_possible
    # now becomes 15.0 (was 14.0) for the 5 active indicators.
    if rsi is not None:
        rsi_norm = (50.0 - rsi) / 20.0
        contribs["rsi"] = max(-1.0, min(1.0, rsi_norm)) * float(rsi_weight)
    else:
        contribs["rsi"] = 0.0

    # theo_delta_gap: saturation default 0.10 (15-min). Daily widens to
    # 0.20 because near-money daily strikes routinely show 0.15-0.30 gaps
    # — a 0.10 saturation pins this to +1.0 on every sample, structurally
    # biasing composite + before any other indicator weighs in.
    _gap_denom = max(1e-6, float(theo_delta_gap_saturation))
    contribs["theo_delta_gap"] = max(-1.0, min(1.0, theo_yes_gap / _gap_denom)) * 4.0

    # market_agreement: DISABLED 2026-05-21. Anti-correlates with
    # theo_delta_gap (r=-0.97 when theoretical_yes≈0.50, r=-0.37
    # overall across 4,619 BTC samples). Both saturate opposite in
    # 32% of samples → composite was structurally cancelling to ~+1
    # exactly when the Greeks model had the most edge. The herd-follow
    # philosophy contradicted the contrarian thesis theo_delta_gap
    # embodies. RSI + whale + orderflow already supply the orthogonal
    # sanity-check role.
    contribs["market_agreement"] = 0.0

    # whale_pressure: pre-normalized to [-1, +1] by the whale_monitor
    # (the recency-weighted indicator value). Weight 3 — doesn't
    # dominate theo_delta_gap.
    if whale_pressure is not None:
        contribs["whale_pressure"] = max(-1.0, min(1.0, float(whale_pressure))) * 3.0
    else:
        contribs["whale_pressure"] = 0.0

    # Kronos foundation-model forecast: a 5th orthogonal signal. The
    # caller (Kalshi signal pipeline) passes a signed value in [-1, +1]
    # derived from Kronos's P(YES) Monte Carlo over a 15-min horizon.
    # Adapted from the Kronos paper (arXiv:2508.02739): short-horizon
    # forecasting is the model's strongest task (44% MAE reduction vs
    # baselines). Optional — when None, doesn't contribute to either
    # composite or max_possible (so confidence ratio stays accurate).
    has_kronos = kronos_signal is not None
    if has_kronos:
        ks = max(-1.0, min(1.0, float(kronos_signal)))
        contribs["kronos"] = ks * float(kronos_weight)
    else:
        contribs["kronos"] = 0.0

    # Order-flow imbalance: signed [-1, +1] from Binance.US top-10 book.
    # Positive = bid-heavy (bullish); negative = ask-heavy (bearish).
    # Microstructure signal — orthogonal to the four base indicators
    # (none of them see the resting order book). Weight 2 (lighter than
    # Kronos because OFI is noisier minute-to-minute).
    has_of = orderflow_signal is not None
    if has_of:
        ofs = max(-1.0, min(1.0, float(orderflow_signal)))
        contribs["orderflow"] = ofs * float(orderflow_weight)
    else:
        contribs["orderflow"] = 0.0

    # Funding-rate divergence: REVERSAL signal from BTC perp funding.
    # High funding (long bias) → -1 (expect mean reversion down).
    # Negative funding (shorts paying) → +1 (squeeze upside).
    # Lighter weight than orderflow because funding is slow-moving
    # (updates every 8h) — secondary signal, not primary.
    has_funding = funding_signal is not None
    if has_funding:
        fs = max(-1.0, min(1.0, float(funding_signal)))
        contribs["funding"] = fs * float(funding_weight)
    else:
        contribs["funding"] = 0.0

    composite = sum(contribs.values())
    # rsi (configurable) + theo (4.0) + whale (3.0); market_agreement disabled 2026-05-21.
    # rsi_weight changed from a fixed 3.0 to a parameter on 2026-05-25 so daily
    # callers can dampen it; max_possible follows so confidence ratio stays correct.
    base_max = float(rsi_weight) + 4.0 + 3.0
    max_possible = (
        base_max
        + (float(kronos_weight) if has_kronos else 0.0)
        + (float(orderflow_weight) if has_of else 0.0)
        + (float(funding_weight) if has_funding else 0.0)
    )

    return {
        "window_open": window_open_price,
        "rsi": rsi,
        "effective_annual_vol": effective_vol,
        "vol_source": "realized" if (use_realized_vol and effective_vol != annual_vol) else "configured",
        # theoretical_yes is the CORRECTED value (or raw if no correction passed).
        # theoretical_yes_raw is the pre-correction value, preserved for the
        # calibration loop to learn raw→actual without double-correcting.
        "theoretical_yes": greeks["theoretical_yes"] if greeks else None,
        "theoretical_yes_raw": greeks.get("theoretical_yes_raw") if greeks else None,
        "theo_yes_correction_factor": float(theo_yes_correction_factor),
        "theo_yes_gap": theo_yes_gap,
        "T_years": greeks["T_years"] if greeks else None,
        "market_yes_price": market_yes_price,
        "whale_pressure": whale_pressure,
        "contribs": contribs,
        "composite": composite,
        "max_possible": max_possible,
        "confidence": abs(composite) / max_possible if max_possible > 0 else 0.0,
        "direction": "UP" if composite > 0 else "DOWN" if composite < 0 else "FLAT",
    }


def compute_indicators(
    *,
    klines: list[dict],
    window_start_ts: int,
    current_spot: float,
    hours_to_close: float | None = None,
    market_yes_price: float | None = None,
    annual_vol: float = DEFAULT_ANNUAL_VOL,
) -> dict:
    """Compute the indicator composite for a Polymarket 5-min window
    by inferring the window-open price from klines, then delegating.

    Thin wrapper around ``compute_indicators_for_window`` so the
    Polymarket and Kalshi paths share the same indicator math —
    including the Greeks ``theo_delta_gap`` when callers supply
    ``hours_to_close`` and ``market_yes_price``.
    """
    window_open = _find_window_open_price(klines, window_start_ts)
    return compute_indicators_for_window(
        klines=klines,
        window_open_price=window_open,
        current_spot=current_spot,
        hours_to_close=hours_to_close,
        market_yes_price=market_yes_price,
        annual_vol=annual_vol,
    )


def discover_5min_btc_markets(
    *,
    max_seconds_out: int = 600,
    require_live: bool = True,
) -> list[dict]:
    """Find currently-open 5-min BTC UP/DOWN markets within
    ``max_seconds_out`` of resolving.

    The Gravia-style edge concentrates in the last ~60s of each window
    (the source repos all converge on a T-10s entry rule). Default
    ``max_seconds_out=600`` captures the full lifecycle of every
    near-term market so we can see whether spreads widen as close
    approaches.

    ``require_live=True`` filters out markets whose ``closed`` flag has
    already flipped (Gamma can lag a few seconds past window-end).
    """
    import requests

    try:
        r = requests.get(
            f"{POLYMARKET_GAMMA}/markets",
            params={
                "closed": "false", "active": "true",
                "limit": 500, "order": "startDate", "ascending": "false",
            },
            timeout=15,
        )
        r.raise_for_status()
        markets = r.json()
    except Exception as e:
        log_event("btc_5min", "gamma_fetch_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return []
    if not isinstance(markets, list):
        return []

    now_ts = datetime.now(timezone.utc).timestamp()
    qualified: list[dict] = []
    for m in markets:
        slug = m.get("slug") or ""
        match = SLUG_RE.match(slug)
        if not match:
            continue
        if require_live and m.get("closed"):
            continue
        try:
            window_end_ts = int(match.group(1))
        except ValueError:
            continue
        seconds_to_close = window_end_ts - now_ts
        # Drop already-resolved windows (slug timestamp passed) and
        # ones too far in the future to care about right now.
        if seconds_to_close < -60 or seconds_to_close > max_seconds_out:
            continue

        # Outcome price snapshot — Polymarket lists outcomes as
        # ["Up", "Down"] and outcomePrices as ["<up>", "<down>"].
        try:
            outcomes = m.get("outcomePrices") or "[]"
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            up_price = float(outcomes[0]) if outcomes else None
        except (json.JSONDecodeError, ValueError, TypeError):
            up_price = None
        if up_price is None or not (0.0 < up_price < 1.0):
            continue

        qualified.append({
            "id": m.get("conditionId") or m.get("id"),
            "slug": slug,
            "question": m.get("question", ""),
            "up_price": up_price,
            "down_price": round(1.0 - up_price, 4),
            "window_end_ts": window_end_ts,
            "seconds_to_close": seconds_to_close,
        })
    # Sort by closest-to-resolving first — that's where the edge lives.
    qualified.sort(key=lambda x: x["seconds_to_close"])
    return qualified


# ── Sampling ─────────────────────────────────────────────────────────

def sample_signals(
    *,
    max_seconds_out: int = 600,
    with_indicators: bool = True,
) -> list[FiveMinSample]:
    """One sweep — spot + klines + every qualifying market.

    Klines are fetched ONCE per cycle and shared across all markets in
    the sweep — they all share the same Binance state. Each market
    gets its own indicator computation (window_start_ts differs per
    market). Cheap: 3 HTTP calls total regardless of how many markets
    are live.
    """
    spot = fetch_binance_btc_price()
    if spot is None:
        return []
    markets = discover_5min_btc_markets(max_seconds_out=max_seconds_out)
    if not markets:
        return []

    klines = fetch_binance_klines() if with_indicators else None

    now_iso = datetime.now(timezone.utc).isoformat()
    out: list[FiveMinSample] = []
    for m in markets:
        indicators = None
        if klines:
            # Window start = window end - 5 minutes
            window_start_ts = m["window_end_ts"] - 300
            hours_to_close = max(m["seconds_to_close"] / 3600.0, 0.0)
            indicators = compute_indicators(
                klines=klines,
                window_start_ts=window_start_ts,
                current_spot=spot,
                hours_to_close=hours_to_close,
                market_yes_price=m["up_price"],
                annual_vol=DEFAULT_ANNUAL_VOL,
            )
        out.append(FiveMinSample(
            sample_at=now_iso,
            market_id=str(m["id"]),
            slug=m["slug"],
            question=m["question"][:140],
            window_end_ts=m["window_end_ts"],
            seconds_to_close=round(m["seconds_to_close"], 2),
            up_price=m["up_price"],
            down_price=m["down_price"],
            spot_usd=spot,
            indicators=indicators,
        ))
    return out


def persist_samples(samples: list[FiveMinSample]) -> None:
    """Append samples as JSONL. Append-only — keep the full trajectory
    so Phase 2 can compute "what was UP price at T-60s vs T-10s" later.
    """
    if not samples:
        return
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_PATH, "a") as f:
        for s in samples:
            f.write(json.dumps(asdict(s)) + "\n")
    # Bounded retention (diagnostic tail; keep from growing without limit).
    try:
        from lib.log_rotation import rotate_if_needed
        rotate_if_needed(SIGNAL_PATH)
    except Exception:
        pass


# ── Public entry ─────────────────────────────────────────────────────

def run_signal_cycle(
    *,
    max_seconds_out: int = 600,
    record_paper_trades: bool = True,
    settle_paper_trades: bool = True,
) -> dict:
    """One full sweep: discover + sample + persist + (paper record + settle) + log.

    ``record_paper_trades=True`` (default) auto-opens a Phase 2 paper
    trade whenever a sample's confidence + entry-window criteria fire.
    ``settle_paper_trades=True`` polls open paper trades for resolution
    every cycle — cheap (only fires if any opens exist).

    Both flags are wired so the launchd cron runs the full pipeline by
    default; set to False from tests / dry-runs.
    """
    samples = sample_signals(max_seconds_out=max_seconds_out)
    persist_samples(samples)

    n_paper_opened = 0
    if record_paper_trades and samples:
        try:
            from lib.btc_5min_paper import record_paper_trades_from_samples
            new_trades = record_paper_trades_from_samples(
                [asdict(s) for s in samples]
            )
            n_paper_opened = len(new_trades)
        except Exception as e:
            log_event("btc_5min", "paper_record_failed",
                      {"error": str(e)[:200]}, result="degraded")

    settle_summary = {}
    if settle_paper_trades:
        try:
            from lib.btc_5min_paper import (
                settle_paper_trades as _settle_paper_trades,
            )
            settle_summary = _settle_paper_trades()
        except Exception as e:
            log_event("btc_5min", "paper_settle_failed",
                      {"error": str(e)[:200]}, result="degraded")

    if samples:
        nearest = samples[0]
        log_event("btc_5min", "signal_cycle", {
            "n_markets": len(samples),
            "nearest_seconds_to_close": nearest.seconds_to_close,
            "nearest_up_price": nearest.up_price,
            "spot": nearest.spot_usd,
            "paper_trades_opened": n_paper_opened,
            "paper_settled": settle_summary.get("settled_now", 0),
        })
    else:
        log_event("btc_5min", "no_active_markets", {}, result="degraded")

    return {
        "n_markets": len(samples),
        "nearest_seconds_to_close": samples[0].seconds_to_close if samples else None,
        "spot_usd": samples[0].spot_usd if samples else None,
        "samples": [asdict(s) for s in samples],
        "paper_trades_opened": n_paper_opened,
        "settle_summary": settle_summary,
    }
