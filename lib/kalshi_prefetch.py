"""
Parallel Market-Data Prefetch — fire the independent REST signal
fetches concurrently so wall-clock latency is max(individual) rather
than sum.

This module orchestrates the slow-but-independent network sources
used by the Kalshi signal pipeline:
  • Order-flow imbalance (Binance.US depth)
  • Funding rate (OKX swap)
  • Multi-timeframe direction votes (Binance.US klines × 3)

Each of these takes 200-600ms on its own; serial total is ~1.1s.
Run them in a ThreadPoolExecutor and total wall-time drops to roughly
the slowest individual call (~250-300ms). Python's GIL is released
during socket I/O so threads are the right primitive — no asyncio
needed.

The returned dict is "shareable" across multiple Kalshi markets in
the same scan cycle (#E in the optimization plan). Different markets
within one cycle look at the same spot/orderbook/funding state, so
firing once per cycle instead of once per market is correct.

Usage:
    from lib.kalshi_prefetch import prefetch_market_data

    data = prefetch_market_data(
        symbol="BTCUSDT",
        primary_direction="YES",  # for MTF check; can be None to skip
        enable_orderflow=True,
        enable_funding=True,
        enable_mtf=True,
    )
    # data["orderflow"] -> (signal, meta) or (None, meta_with_reason)
    # data["funding"]   -> (signal, meta)
    # data["mtf"]       -> (passes_bool, meta)
"""

from __future__ import annotations

import time
from typing import Optional

from tradingcore.audit import log_event


def prefetch_market_data(
    symbol: str = "BTCUSDT",
    *,
    primary_direction: Optional[str] = None,
    enable_orderflow: bool = True,
    enable_funding: bool = True,
    enable_mtf: bool = True,
    orderflow_depth: int = 10,
    mtf_required: int = 2,
) -> dict:
    """Fire all enabled REST signal fetches in parallel and return a
    merged dict.

    Returns:
        {
            "orderflow":  (signal_or_None, meta),     # signed [-1,+1]
            "funding":    (signal_or_None, meta),     # signed [-1,+1]
            "mtf":        (passes_bool, meta),         # gate result
            "wall_clock_ms": int,
            "fetched_at": iso timestamp,
        }

    Each entry is independent — a failure in one returns
    (None, meta_with_reason) for that slot without affecting the others.
    """
    from concurrent.futures import ThreadPoolExecutor, Future

    started = time.time()
    out: dict = {
        "orderflow": (None, {"reason": "disabled"}),
        "funding": (None, {"reason": "disabled"}),
        "mtf": (False, {"reason": "disabled"}),
    }

    tasks: list[tuple[str, Future]] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        if enable_orderflow:
            from lib.kalshi_orderflow import compute_order_flow_imbalance
            tasks.append(("orderflow",
                          ex.submit(compute_order_flow_imbalance,
                                    symbol=symbol, depth_levels=orderflow_depth)))
        if enable_funding:
            from lib.kalshi_funding_rate import funding_rate_signal
            tasks.append(("funding",
                          ex.submit(funding_rate_signal, symbol=symbol)))
        if enable_mtf and primary_direction in ("YES", "NO", "UP", "DOWN"):
            from lib.kalshi_multi_timeframe import check_multi_timeframe_agreement
            tasks.append(("mtf",
                          ex.submit(check_multi_timeframe_agreement,
                                    primary_direction=primary_direction,
                                    symbol=symbol,
                                    required_agreement=mtf_required)))

        # Collect results; never let one task's failure block the others.
        for name, fut in tasks:
            try:
                # Tight timeout per task — slower-than-this means we
                # should fall through rather than block the whole cycle.
                out[name] = fut.result(timeout=10)
            except Exception as e:
                log_event("kalshi_prefetch", "task_failed",
                          {"task": name, "error": str(e)[:200]},
                          result="degraded")
                out[name] = (None, {"reason": "task_failed",
                                    "error": str(e)[:200]})

    wall_ms = int((time.time() - started) * 1000)
    out["wall_clock_ms"] = wall_ms
    out["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    log_event("kalshi_prefetch", "completed",
              {"wall_clock_ms": wall_ms,
               "symbol": symbol,
               "n_tasks": len(tasks)})
    return out
