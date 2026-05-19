"""
Multi-Timeframe Agreement Gate — require directional consensus across
multiple timeframes before letting a trade fire.

A signal that says BTC is bullish on 5-min bars but bearish on 1-hour
bars is noise — the timeframes disagree, no real trend. Conversely
when ALL timeframes lean the same way, the move has structural
support and the win rate jumps materially in published studies.

This is a CHEAP filter — adds one ROC/RSI computation per extra
timeframe — and tends to halve false-fire rate at minimal computational
cost.

Three timeframe checks (all on BTC spot from Binance.US):
  • 5-min: short-term momentum (last 12 bars = 1h)
  • 15-min: trade-horizon match (last 8 bars = 2h)
  • 1-hour: macro-trend (last 12 bars = 12h)

Agreement = all three return the same direction sign. Disagreement
on any one → fail gate. Output is a boolean + meta.

Used as a HARD GATE in kalshi_15min_paper.py: trade skipped when
multi-timeframe agreement fails. Tunable via config (can require 2-of-3
instead of 3-of-3 for less strict).
"""

from __future__ import annotations

import time
from typing import Optional

from tradingcore.audit import log_event


_INMEM_CACHE: dict[str, tuple[float, dict]] = {}
_INMEM_TTL_SECONDS = 30  # short — these are sliding signals


# Per-timeframe number of bars to use for the trend check
DEFAULT_LOOKBACKS = {
    "5m": 12,    # 12 × 5min = 60min
    "15m": 8,    # 8 × 15min = 2h
    "1h": 12,    # 12 × 1h = 12h
}


def _fetch_klines(
    symbol: str,
    interval: str,
    limit: int,
) -> list[list]:
    """Fetch klines from Binance.US (public, no auth)."""
    import requests
    url = "https://api.binance.us/api/v3/klines"
    resp = requests.get(
        url,
        params={"symbol": symbol, "interval": interval, "limit": limit + 5},
        timeout=8,
    )
    resp.raise_for_status()
    return resp.json()


def _direction_from_klines(klines: list[list], lookback: int) -> Optional[int]:
    """Return +1 / 0 / -1 directional vote from a klines window.

    Method: compare current close to mean of the last `lookback`
    closes. If current is > mean by > +0.10% → +1 (bullish); < mean
    by > -0.10% → -1 (bearish); inside threshold → 0 (neutral).

    The threshold avoids treating noise as direction — a 0.05% drift
    over an hour is just churn.
    """
    if not klines or len(klines) < lookback + 1:
        return None
    try:
        closes = [float(k[4]) for k in klines[-lookback - 1:]]
    except (IndexError, ValueError, TypeError):
        return None
    if len(closes) < 2:
        return None
    current = closes[-1]
    prior_mean = sum(closes[:-1]) / max(1, len(closes) - 1)
    if prior_mean <= 0:
        return None
    delta = (current - prior_mean) / prior_mean
    if delta > 0.001:
        return 1
    if delta < -0.001:
        return -1
    return 0


def check_multi_timeframe_agreement(
    primary_direction: str,
    symbol: str = "BTCUSDT",
    *,
    timeframes: list[str] = None,
    required_agreement: int = 3,
    lookbacks: dict | None = None,
) -> tuple[bool, dict]:
    """Check whether the primary signal direction agrees with N of the
    given timeframes' independent directional votes.

    Returns:
        (passes, meta) where passes is True if at least
        `required_agreement` timeframes agree with primary_direction.

    primary_direction: "YES" / "NO" / "FLAT" or "UP" / "DOWN".
    """
    if timeframes is None:
        timeframes = ["5m", "15m", "1h"]
    if lookbacks is None:
        lookbacks = DEFAULT_LOOKBACKS

    # Normalize primary direction to ±1
    primary_sign = (
        +1 if primary_direction in ("YES", "UP") else
        -1 if primary_direction in ("NO", "DOWN") else
        0
    )
    if primary_sign == 0:
        return False, {"reason": "primary_direction_neutral",
                       "primary_direction": primary_direction}

    cache_key = f"{symbol}|{','.join(timeframes)}"
    now = time.time()
    cached = _INMEM_CACHE.get(cache_key)
    votes = None
    if cached is not None and (now - cached[0]) < _INMEM_TTL_SECONDS:
        votes = cached[1]["votes"]
    if votes is None:
        # Disk cache check
        try:
            from lib.kalshi_cache import get as _disk_get
            disk_hit = _disk_get("mtf", cache_key, max_age=_INMEM_TTL_SECONDS)
            if disk_hit is not None:
                votes = disk_hit
                _INMEM_CACHE[cache_key] = (now, {"votes": votes})
        except Exception:
            votes = None
    if votes is None:
        # PARALLEL FETCH: 3 timeframes used to serialize ~600ms of network
        # latency. ThreadPoolExecutor fires them concurrently — Python's
        # GIL is released during the requests.get() socket I/O, so threads
        # are the right primitive here (no asyncio needed). Drops to
        # max(individual_latency) ≈ 200ms.
        from concurrent.futures import ThreadPoolExecutor

        def _fetch_one(tf: str) -> tuple[str, Optional[int]]:
            lb = lookbacks.get(tf, 12)
            try:
                klines = _fetch_klines(symbol, tf, lb)
                return tf, _direction_from_klines(klines, lb)
            except Exception as e:
                log_event("kalshi_mtf", "fetch_failed",
                          {"symbol": symbol, "tf": tf,
                           "error": str(e)[:200]}, result="degraded")
                return tf, None

        votes = {}
        with ThreadPoolExecutor(max_workers=len(timeframes)) as ex:
            for tf, vote in ex.map(_fetch_one, timeframes):
                votes[tf] = vote
        _INMEM_CACHE[cache_key] = (now, {"votes": votes})
        # Persist to disk
        try:
            from lib.kalshi_cache import put as _disk_put
            _disk_put("mtf", cache_key, votes)
        except Exception:
            pass

    agree = sum(1 for v in votes.values() if v == primary_sign)
    disagree = sum(1 for v in votes.values()
                   if v is not None and v != primary_sign and v != 0)
    neutral_or_missing = sum(1 for v in votes.values() if v == 0 or v is None)
    passes = agree >= required_agreement

    return passes, {
        "primary_direction": primary_direction,
        "primary_sign": primary_sign,
        "votes": votes,
        "agree": agree,
        "disagree": disagree,
        "neutral_or_missing": neutral_or_missing,
        "required": required_agreement,
        "passes": passes,
        "interpretation": (
            f"{agree}/{len(timeframes)} timeframes agree" +
            (f" — disagree on {disagree}" if disagree else "")
        ),
    }


def clear_cache() -> None:
    _INMEM_CACHE.clear()
