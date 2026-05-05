"""
Calibration Tracking — measure forecast accuracy over time.

A well-calibrated bot means: when we say 70%, events happen ~70% of the time.
This is THE metric that determines if our edge is real.

Key metrics:
- Brier Score: Mean squared error of probability forecasts. Perfect=0, random=0.25
- Log Loss: Penalizes confident wrong predictions heavily
- Calibration Curve: Predicted vs actual frequency by probability bucket
- Source Accuracy: Which sources (LLM, base rate, news) are most accurate
"""

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
CALIBRATION_FILE = DATA_DIR / "calibration_log.json"


def _load_forecasts() -> list[dict]:
    if not CALIBRATION_FILE.exists():
        return []
    with open(CALIBRATION_FILE, "r") as f:
        return json.load(f)


def _save_forecasts(forecasts: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CALIBRATION_FILE, "w") as f:
        json.dump(forecasts, f, indent=2)


def record_forecast(
    market_id: str,
    platform: str,
    question: str,
    our_probability: float,
    market_probability: float,
    side: str,
    sources: dict[str, float] | None = None,
    outcome: bool | None = None,
) -> dict:
    """
    Record a forecast for later calibration analysis.

    Call at trade entry with outcome=None.
    Call again at resolution with outcome=True/False.

    Args:
        market_id: Market identifier
        platform: Which platform
        question: Market question text
        our_probability: Our estimated probability of YES
        market_probability: Market price at time of forecast
        side: Which side we traded (YES/NO)
        sources: Individual source estimates {"llm": 0.65, "base_rate": 0.70, ...}
        outcome: True if YES resolved, False if NO resolved, None if unresolved

    Returns:
        The recorded forecast entry.
    """
    forecasts = _load_forecasts()

    entry = {
        "market_id": market_id,
        "platform": platform,
        "question": question,
        "our_probability": our_probability,
        "market_probability": market_probability,
        "side": side,
        "sources": sources or {},
        "outcome": outcome,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Update existing entry if we're recording resolution
    if outcome is not None:
        for i, f in enumerate(forecasts):
            if f["market_id"] == market_id and f["outcome"] is None:
                forecasts[i]["outcome"] = outcome
                forecasts[i]["resolved_at"] = datetime.now(timezone.utc).isoformat()
                _save_forecasts(forecasts)
                return forecasts[i]

    forecasts.append(entry)
    _save_forecasts(forecasts)
    return entry


def brier_score(forecasts: list[dict] | None = None) -> float:
    """
    Brier Score — mean squared error of probability forecasts.

    Perfect calibration = 0.0
    Random guessing = 0.25
    Always wrong with confidence = 1.0

    Lower is better.
    """
    if forecasts is None:
        forecasts = _load_forecasts()

    resolved = [f for f in forecasts if f.get("outcome") is not None]
    if not resolved:
        return -1.0  # No resolved forecasts

    total = 0.0
    for f in resolved:
        p = f["our_probability"]
        o = 1.0 if f["outcome"] else 0.0
        total += (p - o) ** 2

    return total / len(resolved)


def log_loss(forecasts: list[dict] | None = None) -> float:
    """
    Logarithmic scoring rule — penalizes confident wrong predictions heavily.

    A forecast of 0.99 for an event that doesn't happen costs much more
    than a forecast of 0.55 for the same non-event.

    Lower is better. 0 = perfect.
    """
    if forecasts is None:
        forecasts = _load_forecasts()

    resolved = [f for f in forecasts if f.get("outcome") is not None]
    if not resolved:
        return -1.0

    total = 0.0
    eps = 1e-10  # Avoid log(0)

    for f in resolved:
        p = max(min(f["our_probability"], 1.0 - eps), eps)
        o = 1.0 if f["outcome"] else 0.0
        total -= o * math.log(p) + (1.0 - o) * math.log(1.0 - p)

    return total / len(resolved)


def calibration_curve(
    forecasts: list[dict] | None = None,
    n_bins: int = 10,
) -> dict[str, dict]:
    """
    Calibration curve — predicted vs actual frequency by probability bucket.

    Bins forecasts into ranges (0-10%, 10-20%, etc.) and compares
    predicted probability vs actual outcome frequency.

    Well-calibrated = predicted ~= actual for each bucket.

    Returns:
        {"0.0-0.1": {"predicted_mean": 0.05, "actual_rate": 0.03, "count": 10}, ...}
    """
    if forecasts is None:
        forecasts = _load_forecasts()

    resolved = [f for f in forecasts if f.get("outcome") is not None]
    if not resolved:
        return {}

    bins: dict[str, list] = defaultdict(list)
    bin_width = 1.0 / n_bins

    for f in resolved:
        p = f["our_probability"]
        bin_idx = min(int(p / bin_width), n_bins - 1)
        low = bin_idx * bin_width
        high = low + bin_width
        bin_key = f"{low:.1f}-{high:.1f}"
        bins[bin_key].append({
            "predicted": p,
            "actual": 1.0 if f["outcome"] else 0.0,
        })

    curve = {}
    for bin_key, entries in sorted(bins.items()):
        predicted_mean = sum(e["predicted"] for e in entries) / len(entries)
        actual_rate = sum(e["actual"] for e in entries) / len(entries)
        curve[bin_key] = {
            "predicted_mean": round(predicted_mean, 4),
            "actual_rate": round(actual_rate, 4),
            "count": len(entries),
            "gap": round(abs(predicted_mean - actual_rate), 4),
        }

    return curve


def source_accuracy(forecasts: list[dict] | None = None) -> dict[str, dict]:
    """
    Break down accuracy by source (LLM, base rate, news, Metaculus).

    Hermes uses this to adjust source weights — if LLM is more accurate
    than base rates, increase llm_weight.

    Returns:
        {"llm": {"brier": 0.15, "count": 20}, "base_rate": {"brier": 0.22, ...}}
    """
    if forecasts is None:
        forecasts = _load_forecasts()

    resolved = [f for f in forecasts if f.get("outcome") is not None and f.get("sources")]
    if not resolved:
        return {}

    source_scores: dict[str, list[float]] = defaultdict(list)

    for f in resolved:
        outcome = 1.0 if f["outcome"] else 0.0
        for source_name, source_prob in f.get("sources", {}).items():
            score = (source_prob - outcome) ** 2
            source_scores[source_name].append(score)

    results = {}
    for source_name, scores in source_scores.items():
        results[source_name] = {
            "brier": round(sum(scores) / len(scores), 4),
            "count": len(scores),
        }

    return dict(sorted(results.items(), key=lambda x: x[1]["brier"]))


def print_calibration_report():
    """Print a formatted calibration report to terminal."""
    forecasts = _load_forecasts()
    resolved = [f for f in forecasts if f.get("outcome") is not None]
    unresolved = [f for f in forecasts if f.get("outcome") is None]

    print("=" * 60)
    print("  POLYBOT CALIBRATION REPORT")
    print("=" * 60)
    print(f"  Total forecasts:    {len(forecasts)}")
    print(f"  Resolved:           {len(resolved)}")
    print(f"  Pending:            {len(unresolved)}")

    if not resolved:
        print("\n  No resolved forecasts yet. Trade more!")
        return

    # Win/loss
    wins = sum(1 for f in resolved
               if (f["side"] == "YES" and f["outcome"])
               or (f["side"] == "NO" and not f["outcome"]))
    losses = len(resolved) - wins
    win_rate = wins / len(resolved) if resolved else 0

    print(f"\n  Win Rate:           {win_rate:.1%} ({wins}W / {losses}L)")

    # Brier score
    bs = brier_score(forecasts)
    quality = "Excellent" if bs < 0.10 else "Good" if bs < 0.15 else "Fair" if bs < 0.20 else "Poor"
    print(f"  Brier Score:        {bs:.4f} ({quality})")
    print(f"  Log Loss:           {log_loss(forecasts):.4f}")

    # Calibration curve
    print(f"\n  --- Calibration Curve ---")
    curve = calibration_curve(forecasts)
    for bin_key, data in curve.items():
        bar_len = int(data["actual_rate"] * 20)
        bar = "#" * bar_len + "." * (20 - bar_len)
        print(f"  {bin_key}: pred={data['predicted_mean']:.2f} "
              f"actual={data['actual_rate']:.2f} [{bar}] n={data['count']}")

    # Source accuracy
    sa = source_accuracy(forecasts)
    if sa:
        print(f"\n  --- Source Accuracy (lower Brier = better) ---")
        for source, data in sa.items():
            print(f"  {source:20s}: Brier={data['brier']:.4f}  (n={data['count']})")

    print("=" * 60)


# ─── Per-provider / per-persona breakdown + Shapley weights (T1.3, 2026-04-26) ───
# Adapted from PolySwarm's Shapley decomposition (Barot & Borkhatariya 2026).
# After enough resolved trades accumulate, we can compute each provider's
# realized Brier and re-weight the ensemble such that worse-calibrated
# providers get less influence on aggregated forecasts.

def provider_brier_breakdown(
    forecasts: list[dict] | None = None,
    min_samples: int = 5,
) -> dict[str, dict]:
    """
    Brier per individual LLM provider (gemini/groq/cerebras/...).

    Reads forecast entries' `provider_predictions` field — populated by
    record_forecast() when individual provider samples are available
    (callers pass them through after llm_analyst.analyze_market). For
    legacy entries without that field, returns nothing.
    """
    if forecasts is None:
        forecasts = _load_forecasts()

    resolved = [f for f in forecasts
                if f.get("outcome") is not None
                and f.get("provider_predictions")]
    if not resolved:
        return {}

    by_provider: dict[str, list[float]] = defaultdict(list)
    for f in resolved:
        outcome = 1.0 if f["outcome"] else 0.0
        for prov, pred in f.get("provider_predictions", {}).items():
            try:
                p = float(pred.get("probability") if isinstance(pred, dict) else pred)
                by_provider[prov].append((p - outcome) ** 2)
            except (TypeError, ValueError):
                continue

    out = {}
    for prov, scores in by_provider.items():
        if len(scores) < min_samples:
            continue
        out[prov] = {
            "brier": round(sum(scores) / len(scores), 4),
            "count": len(scores),
        }
    return dict(sorted(out.items(), key=lambda x: x[1]["brier"]))


def persona_brier_breakdown(
    forecasts: list[dict] | None = None,
    min_samples: int = 5,
) -> dict[str, dict]:
    """Brier per persona (analyst / economist / contrarian / quant / historian).

    Uses each sample's `persona` field (tagged by llm_analyst.analyze_market
    after T1.2 persona swarm). Filters to >=min_samples per persona to
    avoid noise from low-volume personas.
    """
    if forecasts is None:
        forecasts = _load_forecasts()

    resolved = [f for f in forecasts
                if f.get("outcome") is not None
                and f.get("provider_predictions")]
    if not resolved:
        return {}

    by_persona: dict[str, list[float]] = defaultdict(list)
    for f in resolved:
        outcome = 1.0 if f["outcome"] else 0.0
        for prov, pred in f.get("provider_predictions", {}).items():
            if not isinstance(pred, dict):
                continue
            persona = pred.get("persona") or "unknown"
            try:
                p = float(pred.get("probability"))
                by_persona[persona].append((p - outcome) ** 2)
            except (TypeError, ValueError):
                continue

    out = {}
    for persona, scores in by_persona.items():
        if len(scores) < min_samples:
            continue
        out[persona] = {
            "brier": round(sum(scores) / len(scores), 4),
            "count": len(scores),
        }
    return dict(sorted(out.items(), key=lambda x: x[1]["brier"]))


def compute_shapley_weights(
    forecasts: list[dict] | None = None,
    min_samples: int = 8,
    floor: float = 0.05,
) -> dict[str, float]:
    """
    Compute new ensemble weights from realized provider Brier scores.

    Inverse-Brier weighting: providers with lower (better) Brier get
    higher weight. Capped at `floor` minimum so a temporarily-bad
    provider doesn't get permanently zeroed out (random small samples
    can have very high Brier just from variance).

    Returns: {provider_name: weight}, weights summing to 1.0. Empty
    dict if not enough data yet.
    """
    breakdown = provider_brier_breakdown(forecasts, min_samples=min_samples)
    if not breakdown:
        return {}

    # Inverse-Brier transform: weight = 1 / (brier + epsilon).
    # Brier 0.10 -> weight ~10, Brier 0.25 -> weight 4.
    eps = 0.01
    inv = {p: 1.0 / (d["brier"] + eps) for p, d in breakdown.items()}
    total = sum(inv.values())
    raw = {p: w / total for p, w in inv.items()}

    # Apply floor and renormalize
    floored = {p: max(w, floor) for p, w in raw.items()}
    total_f = sum(floored.values())
    return {p: round(w / total_f, 4) for p, w in floored.items()}


def print_advanced_calibration_report():
    """Extended report with per-provider + per-persona + Shapley weights."""
    forecasts = _load_forecasts()
    print_calibration_report()  # the existing summary
    pb = provider_brier_breakdown(forecasts)
    if pb:
        print(f"\n  --- Per-Provider Brier (T1.3) ---")
        for prov, d in pb.items():
            print(f"  {prov:20s}: Brier={d['brier']:.4f}  (n={d['count']})")
    else:
        print("\n  Per-provider Brier: insufficient data "
              "(need >=5 resolved samples per provider with provider_predictions)")

    pp = persona_brier_breakdown(forecasts)
    if pp:
        print(f"\n  --- Per-Persona Brier (T1.2) ---")
        for persona, d in pp.items():
            print(f"  {persona:20s}: Brier={d['brier']:.4f}  (n={d['count']})")

    weights = compute_shapley_weights(forecasts)
    if weights:
        print(f"\n  --- Recommended Provider Weights (Shapley) ---")
        for prov, w in sorted(weights.items(), key=lambda x: -x[1]):
            print(f"  {prov:20s}: {w*100:5.1f}%")
        print(f"\n  Apply via: edit forecasting.llm_providers in strategy.yaml")
