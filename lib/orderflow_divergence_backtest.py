"""Backtest the orderflow exhaustion-divergence detector against the
historical kalshi signal log + actual market resolutions.

The detector flags two regimes:
  +score  → seller_exhaustion (bullish hint)
  -score  → buyer_exhaustion  (bearish hint)
   0      → no divergence

For each historical bar that produced a divergence score, this module:
  1. Looks at the same Kalshi market the bot was sampling.
  2. Pulls the market's actual resolution from the Kalshi public API
     (the same call kalshi_signal_replay uses).
  3. Records whether the divergence direction was right or wrong:
       seller_exhaustion + market_resolved_yes  → CORRECT (bullish hit)
       buyer_exhaustion  + market_resolved_no   → CORRECT (bearish hit)
       otherwise                                → WRONG

Outputs:
  - n_divergence_events
  - directional_accuracy (correct / total)
  - 95% Wilson confidence interval on accuracy
  - breakdown by score magnitude bucket (does stronger divergence
    correlate with stronger accuracy?)

Use this BEFORE wiring divergence into the composite. If accuracy < 55%
the signal is noise; if ≥ 60% on n ≥ 30 events it's worth adding to the
composite with a moderate weight.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

try:
    import requests
except ImportError:
    requests = None

ROOT = Path(__file__).resolve().parent.parent
SIGNAL_LOG = ROOT / "data" / "kalshi_15min_signal.jsonl"

# Reuse helpers from the live module
from lib.orderflow_divergence import compute_divergence


def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = wins / n
    denom = 1 + z ** 2 / n
    centre = (p + z ** 2 / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z ** 2 / (4 * n ** 2)) / denom
    return max(0.0, centre - spread), min(1.0, centre + spread)


def _fetch_kalshi_resolution(ticker: str) -> str | None:
    """Get the resolved YES/NO/None for a Kalshi market. Cached crudely
    by file (in-process dict)."""
    if requests is None:
        return None
    if not ticker:
        return None
    url = f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}"
    try:
        r = requests.get(url, timeout=8)
        if r.status_code != 200:
            return None
        m = r.json().get("market", {}) or {}
        result = m.get("result", "")
        if result in ("yes", "no"):
            return result.upper()
        return None
    except Exception:
        return None


def _load_signal_log(asset: str = "btc") -> list[dict]:
    if not SIGNAL_LOG.exists():
        return []
    rows = []
    with open(SIGNAL_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("asset") == asset:
                    rows.append(r)
            except json.JSONDecodeError:
                continue
    return rows


def _orderflow_from_row(row: dict) -> float | None:
    """Pull the raw orderflow value (un-weighted) from a signal row.
    The composite stores orderflow contribution = signed_value × weight(2),
    so divide by 2 to recover the signed value in [-1, +1].
    """
    contribs = (row.get("indicators") or {}).get("contribs") or {}
    if "orderflow" not in contribs:
        return None
    try:
        return float(contribs["orderflow"]) / 2.0
    except (TypeError, ValueError):
        return None


def run_backtest(
    asset: str = "btc",
    window_size: int = 5,
    min_score_threshold: float = 0.0,
    max_markets_to_resolve: int = 200,
    require_distinct_markets: bool = True,
) -> dict:
    """Walk the signal log, compute divergence on each rolling window,
    record events, then resolve each unique market via Kalshi API.

    Parameters
    ----------
    window_size : int
        Number of samples to feed into compute_divergence per check.
    min_score_threshold : float
        Only count events whose |score| ≥ this threshold.
    max_markets_to_resolve : int
        Cap on API calls (Kalshi public, no auth). 200 = ~3 min.
    require_distinct_markets : bool
        If True, dedupe events by market_ticker (keep the highest-|score|
        per market). Otherwise count every divergence event separately.
    """
    rows = _load_signal_log(asset)
    if len(rows) < window_size + 1:
        return {"error": "insufficient_signal_history",
                "n_rows": len(rows)}

    events: list[dict] = []
    for i in range(window_size, len(rows)):
        window = rows[i - window_size:i]
        prices = [float(r.get("spot_usd") or 0) for r in window if r.get("spot_usd")]
        flows = [_orderflow_from_row(r) for r in window]
        flows = [f for f in flows if f is not None]
        if len(prices) < 3 or len(flows) < 3:
            continue
        div = compute_divergence(prices, flows)
        score = div.get("score", 0.0)
        if abs(score) < min_score_threshold or div.get("kind") == "none":
            continue
        # The event's "market" is the kalshi market_ticker on the current sample
        cur = rows[i]
        market = cur.get("market_ticker") or ""
        sample_at = cur.get("sample_at", "")
        events.append({
            "sample_idx": i,
            "sample_at": sample_at,
            "market_ticker": market,
            "score": score,
            "kind": div.get("kind"),
            "price_change_pct": div.get("price_change_pct"),
            "orderflow_mean": div.get("orderflow_mean"),
        })

    # Dedupe by market — keep highest |score| per ticker so we resolve once
    if require_distinct_markets:
        best_by_market: dict[str, dict] = {}
        for e in events:
            t = e["market_ticker"]
            if not t:
                continue
            cur = best_by_market.get(t)
            if cur is None or abs(e["score"]) > abs(cur["score"]):
                best_by_market[t] = e
        events_to_resolve = list(best_by_market.values())
    else:
        events_to_resolve = events

    # Cap by API budget — prefer highest-score events
    events_to_resolve.sort(key=lambda e: -abs(e["score"]))
    events_to_resolve = events_to_resolve[:max_markets_to_resolve]

    correct = 0
    wrong = 0
    unresolved = 0
    by_bucket: dict[str, dict] = {}
    detailed = []
    for idx, e in enumerate(events_to_resolve):
        # Throttle: ~5 requests/sec is plenty for Kalshi
        if idx > 0 and idx % 5 == 0:
            time.sleep(0.1)
        outcome = _fetch_kalshi_resolution(e["market_ticker"])
        e["resolution"] = outcome
        if outcome is None:
            unresolved += 1
            continue
        kind = e["kind"]
        # seller_exhaustion (score > 0) is a bullish hint → YES win = correct
        # buyer_exhaustion  (score < 0) is a bearish hint → NO win = correct
        if kind == "seller_exhaustion":
            is_correct = (outcome == "YES")
        elif kind == "buyer_exhaustion":
            is_correct = (outcome == "NO")
        else:
            continue
        e["correct"] = is_correct
        if is_correct:
            correct += 1
        else:
            wrong += 1

        # Bucket by score magnitude
        s = abs(e["score"])
        b = (
            "0.0-0.3" if s < 0.3
            else "0.3-0.6" if s < 0.6
            else "0.6-0.9" if s < 0.9
            else "0.9-1.0"
        )
        bb = by_bucket.setdefault(b, {"n": 0, "correct": 0})
        bb["n"] += 1
        if is_correct:
            bb["correct"] += 1
        detailed.append(e)

    total_resolved = correct + wrong
    accuracy = correct / total_resolved if total_resolved else None
    ci_lo, ci_hi = (
        _wilson_ci(correct, total_resolved) if total_resolved else (0.0, 0.0)
    )

    # Add bucket WRs
    for b in by_bucket.values():
        b["wr"] = round(b["correct"] / b["n"], 4) if b["n"] else None

    return {
        "asset": asset,
        "window_size": window_size,
        "min_score_threshold": min_score_threshold,
        "n_rows": len(rows),
        "n_events_total": len(events),
        "n_unique_markets": (
            len({e["market_ticker"] for e in events if e["market_ticker"]})
            if require_distinct_markets else None
        ),
        "n_resolved": total_resolved,
        "n_unresolved": unresolved,
        "correct": correct,
        "wrong": wrong,
        "accuracy": round(accuracy, 4) if accuracy is not None else None,
        "wilson_ci_95": (round(ci_lo, 4), round(ci_hi, 4)),
        "by_bucket": by_bucket,
        "events": detailed[:50],  # cap output
    }


def render(result: dict) -> str:
    if "error" in result:
        return f"ERROR: {result['error']}"
    lines = []
    lines.append("=" * 70)
    lines.append(
        f"ORDERFLOW DIVERGENCE BACKTEST — {result['asset'].upper()}"
    )
    lines.append("=" * 70)
    lines.append(f"Signal log size:     {result['n_rows']}")
    lines.append(f"Window size:         {result['window_size']}")
    lines.append(f"Min |score|:         {result['min_score_threshold']}")
    lines.append(f"Divergence events:   {result['n_events_total']}")
    if result.get("n_unique_markets") is not None:
        lines.append(f"Unique markets:      {result['n_unique_markets']}")
    lines.append(f"Resolved (Kalshi):   {result['n_resolved']}")
    lines.append(f"Unresolved:          {result['n_unresolved']}")
    lines.append("")
    if result["accuracy"] is not None:
        acc = result["accuracy"]
        ci = result["wilson_ci_95"]
        lines.append(f"DIRECTIONAL ACCURACY: {acc:.1%}")
        lines.append(f"95% Wilson CI:        [{ci[0]:.1%}, {ci[1]:.1%}]")
        lines.append(f"  Correct / Wrong:    {result['correct']} / {result['wrong']}")
        # Verdict
        ci_lo = ci[0]
        if result["n_resolved"] >= 30 and ci_lo >= 0.55:
            lines.append("VERDICT: ✓ Edge demonstrated. Wire into composite "
                         "with moderate weight (2.0).")
        elif acc > 0.55:
            lines.append("VERDICT: ~ Positive accuracy but CI too wide / "
                         "sample too small for confidence.")
        else:
            lines.append("VERDICT: ✗ No clear edge. Keep observe-only.")
    else:
        lines.append("No resolved events to evaluate.")
    lines.append("")
    if result.get("by_bucket"):
        lines.append("By score magnitude bucket:")
        for b in sorted(result["by_bucket"].keys()):
            bb = result["by_bucket"][b]
            wr = bb.get("wr")
            wr_s = f"{wr*100:.1f}%" if wr is not None else "n/a"
            lines.append(f"  |score| {b}:  n={bb['n']:>3}  WR={wr_s}")
    lines.append("")
    return "\n".join(lines)


__all__ = ["run_backtest", "render"]


if __name__ == "__main__":
    import sys
    asset = "btc"
    window = 5
    thresh = 0.0
    for arg in sys.argv[1:]:
        if arg.startswith("--asset="):
            asset = arg.split("=", 1)[1].lower()
        elif arg.startswith("--window="):
            window = int(arg.split("=", 1)[1])
        elif arg.startswith("--min-score="):
            thresh = float(arg.split("=", 1)[1])
    result = run_backtest(asset=asset, window_size=window,
                          min_score_threshold=thresh)
    print(render(result))
