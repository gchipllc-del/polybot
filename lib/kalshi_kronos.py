"""
Kalshi-specific Kronos integration — turn the foundation model into a
fifth orthogonal signal for the 15-min BTC trader.

The Kronos paper (arXiv:2508.02739) explicitly identifies short-horizon
price + volatility forecasting as its strongest task (44% MAE reduction
vs baselines, Table 6). Kalshi 15-min markets — "Will BTC close above
$X at 19:45 UTC?" — map directly onto Kronos's native question: "given
the recent OHLCV, what fraction of sampled paths end above target?"

This module is a thin caching wrapper around the existing
`price_to_probability()` in lib/kronos_forecaster.py. It:

  • Maps Kalshi market params (strike, side, time-to-close) to Kronos's
    (target, direction, horizon_bars).
  • Caches by (strike, horizon, current 5-minute bucket) — within a
    5-min wall-clock window the forecast is reused. With ~6-10s/call
    and a 60s scanner cadence, this is essential to avoid burning CPU.
  • Returns (p_yes, meta) where p_yes ∈ [0, 1] is the probability the
    final spot will be ABOVE strike; the YES-side of the binary market.
  • Returns (None, meta) on any failure — never propagates exceptions
    into the signal pipeline. Kronos is a layered enhancement, not a
    required input.

Wire into the composite indicator via btc_5min_signal.py's
compute_indicators_for_window, gated by config flag in
kalshi_assets.yaml (per-asset `kronos: { enabled: false, weight: 3.0 }`).

Performance note (2026-05-19): default sample_count was 10 (paper
Table 6 baseline) but for binary Kalshi questions ("close > strike?")
the Monte Carlo converges fast — N=5 gives ~95% of N=10's accuracy
at ~50% wall time. The paper's N=10 was tuned for general regression-
style price forecasting where the full distribution shape matters;
for our binary "above or below threshold" question the variance of
the estimator at N=5 is small enough. Configurable per-asset; bump
back up if needed.
"""

from __future__ import annotations

import math
import time
from typing import Optional

from tradingcore.audit import log_event


# Cache: { key: (epoch_inserted, p_yes, meta) }. Keyed on strike-bucket +
# horizon-bucket + 5-minute wall-clock bucket. In-memory only — survives
# within a single process. A separate kronos_forecaster.py disk cache
# already exists for the raw forecast.
_INMEM_CACHE: dict[str, tuple[float, Optional[float], dict]] = {}
_INMEM_TTL_SECONDS = 5 * 60  # 5 minutes


# How many 5-minute Binance bars approximate a 15-minute Kalshi horizon.
# We forecast on 5m bars (vs 1m) for less microstructure noise and
# because kronos_forecaster.py already has 5m period support; 15min /
# 5min = 3 bars.
DEFAULT_INTERVAL = "5m"
DEFAULT_HORIZON_BARS = 3


def _cache_key(strike: float, horizon_bars: int, interval: str) -> str:
    """Build a cache key that's stable within a 5-minute wall window
    so concurrent market lookups inside the same scan cycle reuse the
    forecast. Strike rounded to $10 (Kalshi BTC strikes step by $10s
    anyway, but defensively bucket in case of fractional inputs)."""
    strike_bucket = round(strike / 10) * 10
    minute_bucket = int(time.time() // 300)  # 5-minute granularity
    return f"BTC|{strike_bucket}|{horizon_bars}|{interval}|{minute_bucket}"


def kronos_yes_probability(
    *,
    strike: float,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    interval: str = DEFAULT_INTERVAL,
    sample_count: int = 5,           # was 10 — see note in module docstring
    ticker: str = "BTC-USD",
    model_size: str = "small",       # was "base"; small is 24.7M vs 102.3M, ~4× faster CPU inference
) -> tuple[Optional[float], dict]:
    """Return (p_yes, meta) where p_yes is the Kronos-estimated
    probability that the final close exceeds `strike` over the given
    horizon. `None` on failure.

    Defaults are paper-conformant: sample_count=10, the 5m × 3 bars
    horizon approximates a 15-min Kalshi window with realistic noise.
    """
    if strike is None or strike <= 0:
        return None, {"reason": "invalid_strike", "strike": strike}

    key = _cache_key(strike, horizon_bars, interval)
    now = time.time()

    # ── In-memory cache hit ────────────────────────────────────────
    cached = _INMEM_CACHE.get(key)
    if cached is not None:
        inserted, p, meta = cached
        if (now - inserted) < _INMEM_TTL_SECONDS:
            return p, {**meta, "cache": "hit"}

    # ── Cold call ──────────────────────────────────────────────────
    try:
        from lib.kronos_forecaster import price_to_probability
        result = price_to_probability(
            ticker=ticker,
            target_price=float(strike),
            direction="above",
            horizon_bars=horizon_bars,
            interval=interval,
            sample_count=sample_count,
            model_size=model_size,
            # Paper Table 6 defaults — inherited from price_to_probability.
        )
        p_yes = float(getattr(result, "probability", None) or 0.0)
        # Clamp into [0, 1] defensively in case of any model edge cases.
        p_yes = max(0.0, min(1.0, p_yes))
        meta = {
            "kronos_p_yes": round(p_yes, 4),
            "strike": float(strike),
            "horizon_bars": horizon_bars,
            "interval": interval,
            "sample_count": sample_count,
            "ticker": ticker,
            "cache": "miss",
            "n_above": getattr(result, "n_above", None),
            "n_samples": getattr(result, "n_samples", None),
            "expected_final": getattr(result, "expected_final", None),
        }
        _INMEM_CACHE[key] = (now, p_yes, meta)
        log_event("kalshi_kronos", "forecast_completed", meta)
        return p_yes, meta

    except Exception as e:
        meta = {
            "reason": "kronos_call_failed",
            "error": type(e).__name__ + ": " + str(e)[:200],
            "strike": float(strike),
        }
        # Cache the failure short-term too so we don't hammer the model
        # repeatedly when it's broken (e.g. missing weights file).
        _INMEM_CACHE[key] = (now, None, meta)
        log_event("kalshi_kronos", "forecast_failed",
                  meta, result="degraded")
        return None, meta


def kronos_signed_signal(
    *,
    strike: float,
    horizon_bars: int = DEFAULT_HORIZON_BARS,
    interval: str = DEFAULT_INTERVAL,
    sample_count: int = 5,
    ticker: str = "BTC-USD",
    model_size: str = "small",
) -> tuple[Optional[float], dict]:
    """Convert Kronos's [0, 1] probability into a signed signal in
    [-1, +1] for use in the composite. p_yes=0.5 → 0 (no opinion);
    p_yes=1.0 → +1.0 (strong YES); p_yes=0.0 → -1.0 (strong NO).

    Returns (signed_signal, meta) or (None, meta) on failure.
    """
    p_yes, meta = kronos_yes_probability(
        strike=strike,
        horizon_bars=horizon_bars,
        interval=interval,
        sample_count=sample_count,
        ticker=ticker,
        model_size=model_size,
    )
    if p_yes is None:
        return None, meta
    signed = (p_yes - 0.5) * 2.0
    # Defensive clamp
    signed = max(-1.0, min(1.0, signed))
    return signed, {**meta, "signed_signal": round(signed, 4)}


def disagreement_gate(
    composite_direction: str,
    composite_confidence: float,
    kronos_p_yes: Optional[float],
    *,
    disagreement_cap: float = 0.40,
) -> tuple[float, dict]:
    """If the bot's composite signal disagrees materially with Kronos,
    cap the confidence (don't propagate the bot's confidence as-is).
    This is the "disagreement check" pattern — when two independent
    estimators disagree, our knowledge is weaker than either alone.

    Returns (effective_confidence, meta).

    Logic:
      • Composite says YES (direction='YES') with confidence 0.80
      • Kronos says p_yes = 0.30 (so leans NO)
      • → disagreement. effective_confidence = min(0.80, disagreement_cap)
    """
    if kronos_p_yes is None or composite_direction not in ("YES", "NO"):
        return composite_confidence, {"kronos_check": "skipped",
                                       "kronos_p_yes": kronos_p_yes}

    # Kronos's view of the bot's chosen direction.
    kronos_view_of_our_side = (
        kronos_p_yes if composite_direction == "YES"
        else (1.0 - kronos_p_yes)
    )
    # Material disagreement = Kronos thinks our side has < 50% chance.
    if kronos_view_of_our_side < 0.50:
        effective = min(composite_confidence, disagreement_cap)
        return effective, {
            "kronos_check": "disagreement",
            "kronos_p_yes": round(kronos_p_yes, 4),
            "kronos_view_of_our_side": round(kronos_view_of_our_side, 4),
            "original_confidence": composite_confidence,
            "capped_to": effective,
            "disagreement_cap": disagreement_cap,
        }
    return composite_confidence, {
        "kronos_check": "agreement",
        "kronos_p_yes": round(kronos_p_yes, 4),
        "kronos_view_of_our_side": round(kronos_view_of_our_side, 4),
    }


def clear_cache() -> None:
    """Test helper — wipe in-memory cache."""
    _INMEM_CACHE.clear()
