"""
Funding-Rate Divergence — Binance perpetual-futures funding as a
short-term reversal signal.

The premise: BTC perpetual-futures pay funding every 8 hours from
longs to shorts when funding rate is positive (more long demand than
short). When funding gets extreme — say, > +0.05% per 8h annualized to
~+55%/yr — the perp is overheated; one of the most reliable short-term
mean-reversion setups in crypto. Conversely, deeply negative funding
predicts upside bounce.

Why orthogonal to the existing composite: none of the existing
indicators see derivatives positioning. Spot/orderbook/whale-flow
all happen on the SPOT exchange — funding rate is the leveraged
crowd's positioning, which often DIVERGES from spot in instructive
ways.

For Kalshi BTC 15-min:
  • Strongly positive funding (> +0.02%) → mild bearish bias (overheated)
  • Mildly positive (0 to +0.02%) → neutral
  • Negative funding → mild bullish bias (shorts are paying, likely
    squeezed)

Output: signed signal in [-1, +1] for the composite.

Cost: one REST call per scan cycle (cached 30 min — funding only
updates every 8h). No auth required for Binance public futures API.
"""

from __future__ import annotations

import time
from typing import Optional

from tradingcore.audit import log_event


# In-memory cache (no need to persist — refit on process restart is fine).
_INMEM_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_INMEM_TTL_SECONDS = 30 * 60  # 30 minutes; funding updates only every 8h


# Tuning thresholds. Funding rate is the 8-hour rate (positive = longs
# pay shorts). Annualized: 0.01% × 3 settlements/day × 365 ≈ +11%/yr.
# At +0.10% per 8h (+109%/yr annualized) the perp is overheated.
EXTREME_POSITIVE = 0.0005     # +0.05% per 8h = ~+55%/yr  (max negative bias)


def _to_okx_symbol(symbol: str) -> str:
    """Convert a Binance-style perp symbol like 'BTCUSDT' into OKX's
    SWAP format 'BTC-USDT-SWAP'. Handles a few common quote currencies;
    falls back to inserting '-' before the last 4 chars (USDT)."""
    s = symbol.upper().strip()
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote):
            base = s[:-len(quote)]
            return f"{base}-{quote}-SWAP"
    return f"{s}-SWAP"
MILD_POSITIVE    = 0.0001     # +0.01% per 8h = ~+11%/yr  (start downweighting)
MILD_NEGATIVE    = -0.0001    # mirror: shorts paying = bullish
EXTREME_NEGATIVE = -0.0005    # max positive bias


def fetch_funding_rate(symbol: str = "BTCUSDT") -> tuple[Optional[float], dict]:
    """Pull the most-recent perp funding rate from Binance public API.

    Returns (rate, meta) where rate is the 8h funding rate as a decimal
    (0.0001 = 0.01%) or None on failure.
    """
    cache_key = symbol
    now = time.time()
    cached = _INMEM_CACHE.get(cache_key)
    if cached is not None:
        ts, meta = cached
        if (now - ts) < _INMEM_TTL_SECONDS and meta is not None:
            return meta.get("funding_rate"), {**meta, "cache": "hit"}

    # NOTE: Tried several funding-rate sources from US IPs:
    #   • Binance global futures (fapi.binance.com) → HTTP 451 geo-blocked
    #   • Bybit (api.bybit.com) → HTTP 403 forbidden
    #   • OKX (www.okx.com) → HTTP 200 ✓ (this works)
    # We use OKX. Map Kalshi-style symbol "BTCUSDT" to OKX's perpetual
    # naming "BTC-USDT-SWAP" (dash separators, -SWAP suffix).
    okx_symbol = _to_okx_symbol(symbol)
    try:
        import requests
        url = "https://www.okx.com/api/v5/public/funding-rate"
        resp = requests.get(url, params={"instId": okx_symbol}, timeout=5)
        resp.raise_for_status()
        body = resp.json()
    except Exception as e:
        meta = {
            "reason": "fetch_failed",
            "error": type(e).__name__ + ": " + str(e)[:200],
            "symbol": symbol,
            "okx_symbol": okx_symbol,
        }
        _INMEM_CACHE[cache_key] = (now, None)
        log_event("kalshi_funding", "fetch_failed",
                  meta, result="degraded")
        return None, meta

    # OKX response shape: {code: "0", data: [{fundingRate, fundingTime, ...}], msg}
    if not isinstance(body, dict) or body.get("code") != "0":
        return None, {"reason": "okx_error",
                      "code": body.get("code") if isinstance(body, dict) else None,
                      "msg": body.get("msg") if isinstance(body, dict) else str(body)[:100]}

    entries = body.get("data") or []
    if not entries:
        return None, {"reason": "empty_response"}

    entry = entries[0]
    try:
        rate = float(entry.get("fundingRate") or 0)
    except (ValueError, TypeError):
        return None, {"reason": "parse_error"}

    meta = {
        "funding_rate": rate,
        "funding_rate_pct_8h": round(rate * 100, 5),
        "funding_annualized_pct": round(rate * 3 * 365 * 100, 2),
        "funding_time": entry.get("fundingRateTimestamp") or entry.get("fundingTime"),
        "symbol": symbol,
        "interpretation": (
            f"extreme overheated ({rate*100:+.4f}% per 8h)" if rate > EXTREME_POSITIVE else
            f"mildly long-biased ({rate*100:+.4f}%)" if rate > MILD_POSITIVE else
            f"extreme short-squeeze setup ({rate*100:+.4f}%)" if rate < EXTREME_NEGATIVE else
            f"mildly short-biased ({rate*100:+.4f}%)" if rate < MILD_NEGATIVE else
            f"neutral ({rate*100:+.4f}%)"
        ),
        "cache": "miss",
    }
    _INMEM_CACHE[cache_key] = (now, meta)
    return rate, meta


def funding_rate_signal(symbol: str = "BTCUSDT") -> tuple[Optional[float], dict]:
    """Map raw funding rate to a signed signal in [-1, +1] for the
    composite. Extreme positive funding → -1 (bearish reversal); extreme
    negative → +1 (bullish reversal). In between, scaled linearly.

    Returns (signal, meta) or (None, meta) on failure.

    Note: this is a REVERSAL signal, not a momentum signal. The sign
    is INVERTED from the funding sign — high funding (long bias)
    produces NEGATIVE signal (expect reversion down).
    """
    rate, meta = fetch_funding_rate(symbol=symbol)
    if rate is None:
        return None, meta

    # Linear ramp between mild and extreme thresholds; clamp.
    # rate >= EXTREME_POSITIVE → -1.0
    # rate <= EXTREME_NEGATIVE → +1.0
    # rate == 0 → 0
    if rate >= EXTREME_POSITIVE:
        signal = -1.0
    elif rate <= EXTREME_NEGATIVE:
        signal = 1.0
    elif rate > MILD_POSITIVE:
        # Scale linearly between MILD_POSITIVE (signal=0) and
        # EXTREME_POSITIVE (signal=-1).
        t = (rate - MILD_POSITIVE) / (EXTREME_POSITIVE - MILD_POSITIVE)
        signal = -t
    elif rate < MILD_NEGATIVE:
        t = (rate - MILD_NEGATIVE) / (EXTREME_NEGATIVE - MILD_NEGATIVE)
        signal = t  # negative t → positive signal? let me recheck
        # rate < MILD_NEGATIVE → moves toward EXTREME_NEGATIVE
        # MILD_NEGATIVE = -0.0001; EXTREME_NEGATIVE = -0.0005
        # if rate = -0.0003 (between), t = (-0.0003 - -0.0001) / (-0.0005 - -0.0001)
        #                              = -0.0002 / -0.0004 = 0.5 → signal +0.5 ✓
        signal = t
    else:
        signal = 0.0
    signal = max(-1.0, min(1.0, signal))

    return signal, {**meta, "signed_signal": round(signal, 4)}


def clear_cache() -> None:
    """Test helper."""
    _INMEM_CACHE.clear()
