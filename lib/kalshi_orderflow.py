"""
Order-Flow Imbalance — Binance.US top-N order-book directional signal.

When the bid side of the order book has materially more volume than the
ask side at the top N price levels, the path of least resistance is up
(buyers must walk up the ladder to find liquidity faster than sellers
need to walk down). Vice versa for ask-heavy books.

Why it's an *orthogonal* signal in our Kalshi composite:
  • The existing four indicators are derived from price/volatility/
    market-price/whale-trade. None of them sees the resting limit-order
    book directly.
  • OFI is forward-looking microstructure — it shows where liquidity
    sits NOW, not where it just traded. For a 15-minute horizon this
    matters: a heavily-skewed book is more likely to resolve in the
    direction of the imbalance.
  • Cheap: one REST call per scan cycle, no models, deterministic.

Formula:
    bid_vol = sum of volume at top N bid levels
    ask_vol = sum of volume at top N ask levels
    OFI = (bid_vol - ask_vol) / (bid_vol + ask_vol)  ∈ [-1, +1]

Negative OFI = ask-heavy = bearish pressure. Positive OFI = bid-heavy
= bullish pressure.

Returns a signed signal in [-1, +1] for direct use in the composite.
Failures (rate limits, API down) return None — caller falls back to
not using the signal that cycle.
"""

from __future__ import annotations

import time
from typing import Optional

from tradingcore.audit import log_event


# Cache: { symbol: (epoch_inserted, ofi, meta) }
# OFI changes constantly but at our 60s cadence the cache is mainly to
# avoid hammering Binance when multiple Kalshi markets share an asset.
_INMEM_CACHE: dict[str, tuple[float, Optional[float], dict]] = {}
_INMEM_TTL_SECONDS = 30  # 30s — order book turns over fast


def compute_order_flow_imbalance(
    symbol: str = "BTCUSDT",
    *,
    depth_levels: int = 10,
) -> tuple[Optional[float], dict]:
    """Compute OFI from Binance.US order book.

    Returns:
        (ofi, meta) where ofi ∈ [-1, +1] or None on failure.
        Positive = bid-heavy (bullish); negative = ask-heavy (bearish).

    depth_levels = 10 covers the levels real traders care about for
    short-horizon directional bias. Going deeper (50+) starts
    including pesky algorithmic noise and stale orders that don't
    reflect immediate intent.
    """
    cache_key = f"{symbol}|{depth_levels}"
    now = time.time()
    cached = _INMEM_CACHE.get(cache_key)
    if cached is not None:
        ts, val, meta = cached
        if (now - ts) < _INMEM_TTL_SECONDS:
            return val, {**meta, "cache": "hit"}

    try:
        import requests
        # Binance.US public market data — no auth required for depth.
        url = f"https://api.binance.us/api/v3/depth"
        resp = requests.get(
            url,
            params={"symbol": symbol, "limit": min(depth_levels * 5, 100)},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        meta = {
            "reason": "fetch_failed",
            "error": type(e).__name__ + ": " + str(e)[:200],
            "symbol": symbol,
        }
        _INMEM_CACHE[cache_key] = (now, None, meta)
        log_event("kalshi_orderflow", "fetch_failed",
                  meta, result="degraded")
        return None, meta

    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if len(bids) < depth_levels or len(asks) < depth_levels:
        return None, {"reason": "shallow_book",
                      "n_bids": len(bids), "n_asks": len(asks)}

    # Sum top-N volumes. bids/asks are arrays of [price, qty] strings.
    try:
        bid_vol = sum(float(b[1]) for b in bids[:depth_levels])
        ask_vol = sum(float(a[1]) for a in asks[:depth_levels])
    except (ValueError, IndexError) as e:
        return None, {"reason": "parse_error", "error": str(e)[:100]}

    total = bid_vol + ask_vol
    if total <= 0:
        return None, {"reason": "zero_volume"}

    ofi = (bid_vol - ask_vol) / total
    # Defensive clamp
    ofi = max(-1.0, min(1.0, ofi))

    # Best-bid and best-ask for context
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2

    meta = {
        "symbol": symbol,
        "depth_levels": depth_levels,
        "bid_vol_top_n": round(bid_vol, 4),
        "ask_vol_top_n": round(ask_vol, 4),
        "ofi": round(ofi, 4),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": round((best_ask - best_bid) / mid * 10000, 2) if mid > 0 else None,
        "interpretation": (
            f"strong bid-heavy ({ofi:+.2f})" if ofi > 0.30 else
            f"bid-heavy ({ofi:+.2f})" if ofi > 0.10 else
            f"ask-heavy ({ofi:+.2f})" if ofi < -0.10 else
            f"strong ask-heavy ({ofi:+.2f})" if ofi < -0.30 else
            f"balanced ({ofi:+.2f})"
        ),
        "cache": "miss",
    }
    _INMEM_CACHE[cache_key] = (now, ofi, meta)
    return ofi, meta


def clear_cache() -> None:
    """Test helper — wipe in-memory cache."""
    _INMEM_CACHE.clear()
