"""Volume-delta / orderflow exhaustion divergence detector.

Adapted from Mato Conti's "Order Flow" video (2026-05-24). The technique:

  Normal regime:   price ↑ + delta positive   = trend continuation
                   price ↓ + delta negative   = trend continuation
                   (composite signal stays primary-directional)

  Divergent regime:
    price ↑ + delta weak/negative     → BUYER EXHAUSTION → bearish bias
    price ↓ + delta positive/strong   → SELLER EXHAUSTION → bullish bias

These divergences appear at swing reversals — when aggressive buyers
have stopped paying up but price hasn't yet rolled over (or vice versa).
The Mato example showed 61.3% WR on a NQ futures backtest using this
pattern as an entry trigger above the prior session high.

This module is **standalone** — it does not depend on the rest of the
kalshi pipeline. Callers pass in recent prices + orderflow values, get
back a divergence score in [-1, +1] where:

   +x  = seller exhaustion (bullish bias to add)
   -x  = buyer exhaustion (bearish bias to add)
    0  = no divergence, prices and orderflow agree

Wiring into the kalshi composite is via a new contribution slot. The
default weight is moderate (2.0) — same as orderflow itself — because
divergence is event-like (rare but high-information).
"""
from __future__ import annotations

import math
from typing import Sequence


def _slope(values: Sequence[float]) -> float:
    """Crude slope estimate: (last - first) / first. Returns 0 if first <= 0."""
    if len(values) < 2:
        return 0.0
    first, last = values[0], values[-1]
    if first <= 0:
        return 0.0
    return (last - first) / first


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def compute_divergence(
    recent_prices: Sequence[float],
    recent_orderflow: Sequence[float],
    min_samples: int = 3,
    price_change_threshold: float = 0.0005,  # 5 bps min price move
    orderflow_threshold: float = 0.05,        # min OFI magnitude
) -> dict:
    """Compute exhaustion divergence from short price + OFI history.

    Inputs are parallel sequences (oldest → newest). ``recent_prices``
    are spot prices; ``recent_orderflow`` are signed orderflow values
    each in roughly [-1, +1] (the same scale the bot already uses).

    Returns:
        {
          "score": float in [-1, +1]  — divergence score
          "kind":  "seller_exhaustion" | "buyer_exhaustion" | "none"
          "price_change_pct": float
          "orderflow_mean":   float
          "n_samples":        int
          "reason":           str
        }

    The score is non-zero only when:
      - price moved by ≥ price_change_threshold over the window
      - mean orderflow magnitude ≥ orderflow_threshold
      - their signs DISAGREE
    Otherwise (alignment or noise) the score is 0.

    When divergent, score = orderflow_mean (carries OFI's sign), so
    integration into a composite is just `composite += divergence_score
    × divergence_weight`. Negative orderflow during a price up move
    correctly pushes composite negative (bearish exhaustion signal).
    """
    n = min(len(recent_prices), len(recent_orderflow))
    if n < min_samples:
        return {
            "score": 0.0, "kind": "none",
            "price_change_pct": 0.0, "orderflow_mean": 0.0,
            "n_samples": n,
            "reason": f"insufficient_samples({n}<{min_samples})",
        }

    prices = list(recent_prices[-n:])
    flows = list(recent_orderflow[-n:])

    price_change = _slope(prices)
    of_mean = _mean(flows)

    # Both signals need to be meaningful (above noise floor)
    if abs(price_change) < price_change_threshold:
        return {
            "score": 0.0, "kind": "none",
            "price_change_pct": price_change, "orderflow_mean": of_mean,
            "n_samples": n,
            "reason": f"price_change({price_change:.4f}) below threshold",
        }
    if abs(of_mean) < orderflow_threshold:
        return {
            "score": 0.0, "kind": "none",
            "price_change_pct": price_change, "orderflow_mean": of_mean,
            "n_samples": n,
            "reason": f"orderflow_mean({of_mean:.4f}) below threshold",
        }

    # Sign agreement → trend continuation, no divergence
    if (price_change > 0) == (of_mean > 0):
        return {
            "score": 0.0, "kind": "none",
            "price_change_pct": price_change, "orderflow_mean": of_mean,
            "n_samples": n,
            "reason": "signs_agree_trend_continuation",
        }

    # Divergent — return the orderflow_mean as the score (correctly
    # signed: positive when seller exhaustion, negative when buyer
    # exhaustion). Clip to ±1 just in case the caller passes
    # un-normalized OFI.
    score = max(-1.0, min(1.0, of_mean))
    if score > 0:
        kind = "seller_exhaustion"  # bullish reversal hint
    else:
        kind = "buyer_exhaustion"   # bearish reversal hint

    return {
        "score": round(score, 4),
        "kind": kind,
        "price_change_pct": round(price_change, 6),
        "orderflow_mean": round(of_mean, 4),
        "n_samples": n,
        "reason": f"DIVERGENCE: {kind} (price {price_change:+.4f}, OFI {of_mean:+.4f})",
    }


def divergence_from_signal_log(
    asset: str,
    n_lookback: int = 5,
    signal_log_path: str | None = None,
) -> dict:
    """Helper that reads the last N samples for ``asset`` from the
    kalshi signal log and computes divergence on those.

    Used by the live signal pipeline so the contribution can be wired
    in without restructuring the data flow.
    """
    import json
    from pathlib import Path
    if signal_log_path is None:
        path = (
            Path(__file__).resolve().parent.parent
            / "data" / "kalshi_15min_signal.jsonl"
        )
    else:
        path = Path(signal_log_path)
    if not path.exists():
        return compute_divergence([], [])

    # Read the file tail efficiently — for typical hot path we only need
    # the last few hundred lines. For a first cut, read it all. NOTE: signal
    # logs are now bounded by lib/log_rotation (rotate_if_needed in
    # persist_samples), capped at ~20k rows; this is no longer unbounded.
    # Future: switch to reverse-read for further speedup.
    rows = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return compute_divergence([], [])

    asset_rows = [r for r in rows if r.get("asset") == asset][-n_lookback:]
    if len(asset_rows) < 3:
        return compute_divergence([], [])

    prices = [
        float(r.get("spot_usd") or 0)
        for r in asset_rows if r.get("spot_usd")
    ]
    flows = [
        float((r.get("indicators") or {}).get("contribs", {}).get("orderflow") or 0)
        / 2.0  # orderflow contrib is scaled by weight=2, undo for raw sign
        for r in asset_rows
    ]

    return compute_divergence(prices, flows)


def render(result: dict) -> str:
    lines = [
        "=" * 60,
        "ORDERFLOW DIVERGENCE",
        "=" * 60,
        f"n_samples:        {result.get('n_samples', 0)}",
        f"price_change_pct: {result.get('price_change_pct', 0):+.4%}",
        f"orderflow_mean:   {result.get('orderflow_mean', 0):+.4f}",
        f"score:            {result.get('score', 0):+.4f}",
        f"kind:             {result.get('kind', 'none')}",
        f"reason:           {result.get('reason', '')}",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "compute_divergence",
    "divergence_from_signal_log",
    "render",
]


if __name__ == "__main__":
    import sys
    asset = sys.argv[1] if len(sys.argv) > 1 else "btc"
    lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    print(render(divergence_from_signal_log(asset, n_lookback=lookback)))
