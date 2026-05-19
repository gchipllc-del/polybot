"""
Bayesian Model Averaging — weight independent forecast sources by
historical Brier score.

We now have multiple sources that each produce a probability of YES:
  • Composite confidence (the 7-indicator weighted sum, calibrated)
  • Kronos foundation-model forecast (P(close > strike))
  • Conformal-prediction-derived confidence
  • Market-implied YES price (the actual Kalshi quote)

BMA combines them weighted by recent track record. A model that's
been well-calibrated (low Brier) gets more vote; a model that's
been miscalibrated gets dampened.

Why this matters: equal-weight averaging is wrong when models have
different accuracy. BMA's information-theoretic answer: weight by
exp(-Brier), normalize.

Formula:
    p_BMA(YES) = Σ w_i × p_i(YES)
    w_i = exp(-Brier_i) / Σ exp(-Brier_j)

Brier is computed over a rolling window of resolved trades. Below
MIN_SAMPLES per model, that model contributes with the default
weight (1.0), effectively equal-weight.

Public API:
    bma_combine(model_probs, model_briers) -> (p_yes, meta)

Where:
    model_probs = {"composite": 0.72, "kronos": 0.68, "conformal": 0.81, ...}
    model_briers = {"composite": 0.18, "kronos": 0.22, ...}  # rolling Brier per model

The caller is responsible for fetching the recent Brier per model
(this module just does the weighting math). A helper
`brier_score_per_model()` reads the recent resolved trades and
computes per-model Brier from logged model_p_yes columns.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Optional


_ROOT = Path(__file__).resolve().parent.parent
_PAPER_PATH = _ROOT / "data" / "kalshi_15min_paper.jsonl"
_BMA_CACHE_PATH = _ROOT / "data" / "kalshi_bma_weights.json"


MIN_RESOLVED_FOR_BRIER = 10   # need >=10 resolved trades to compute per-model Brier
DEFAULT_BRIER_FALLBACK = 0.25  # used when a model has < MIN_RESOLVED_FOR_BRIER
CACHE_TTL_SECONDS = 3600


def _resolve_outcome_to_yes(status: str) -> Optional[int]:
    """Map paper-trade status to whether YES won. Returns None for
    void / open / unresolved."""
    if status in ("won", "won_early"):
        return 1
    if status in ("lost", "cut_loss"):
        return 0
    return None


def brier_score_per_model(
    *,
    model_field_map: Optional[dict] = None,
) -> dict[str, dict]:
    """Compute per-model rolling Brier score from resolved paper trades.

    The Kalshi paper-trade record carries the bot's own composite
    confidence as `confidence`. As we add more model sources, each
    should log its own p(YES) into a named field (e.g. `kronos_p_yes`,
    `conformal_p_yes`).

    Args:
        model_field_map: { model_name: field_name } indicating which
            paper-trade-record field carries each model's p(YES). If
            None, defaults to {"composite": "confidence"} (the only
            model whose p(YES) was historically logged).

    Returns:
        { model_name: { "brier": float, "n": int, "weight": float } }

    The `weight` is the BMA softmax weight: exp(-Brier) / normalizer.
    Models with insufficient data get DEFAULT_BRIER_FALLBACK Brier
    (neutral 0.25) for weight purposes.
    """
    if model_field_map is None:
        model_field_map = {"composite": "confidence"}

    # Load resolved trades
    if not _PAPER_PATH.exists():
        return {}
    rows: list[dict] = []
    try:
        with open(_PAPER_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("status") in ("won", "won_early", "lost", "cut_loss"):
                    rows.append(r)
    except OSError:
        return {}

    out: dict[str, dict] = {}
    for model_name, field in model_field_map.items():
        sq_errs = []
        for r in rows:
            outcome = _resolve_outcome_to_yes(r.get("status"))
            if outcome is None:
                continue
            # The composite-confidence field is the bot's |signal|; it
            # represents conviction in direction, NOT P(YES). For a YES
            # trade, confidence ≈ P(YES); for a NO trade, P(YES) =
            # 1 - confidence. The side field tells us which.
            p_yes_raw = r.get(field)
            if p_yes_raw is None:
                continue
            try:
                p_raw = float(p_yes_raw)
            except (ValueError, TypeError):
                continue
            side = r.get("side")
            if side == "YES":
                p_yes = p_raw
            elif side == "NO":
                p_yes = 1.0 - p_raw
            else:
                continue
            sq_errs.append((p_yes - outcome) ** 2)

        n = len(sq_errs)
        if n < MIN_RESOLVED_FOR_BRIER:
            out[model_name] = {
                "brier": DEFAULT_BRIER_FALLBACK,
                "n": n,
                "is_default": True,
            }
        else:
            brier = sum(sq_errs) / n
            out[model_name] = {
                "brier": round(brier, 4),
                "n": n,
                "is_default": False,
            }

    # Compute softmax weights from -Brier
    neg_briers = {m: -float(d["brier"]) for m, d in out.items()}
    if neg_briers:
        # Numerical stability: subtract max before exp
        max_v = max(neg_briers.values())
        exps = {m: math.exp(v - max_v) for m, v in neg_briers.items()}
        z = sum(exps.values())
        for m in out:
            out[m]["weight"] = round(exps[m] / z, 4) if z > 0 else (1.0 / len(out))

    return out


def bma_combine(
    model_probs: dict[str, float],
    *,
    model_field_map: Optional[dict] = None,
    brier_overrides: Optional[dict[str, float]] = None,
) -> tuple[float, dict]:
    """Combine multiple models' P(YES) estimates into a single BMA estimate.

    Args:
        model_probs: { model_name: p_yes }
        model_field_map: passed to brier_score_per_model
        brier_overrides: optionally override per-model Brier without
            re-reading the log (useful for tests)

    Returns:
        (p_bma, meta) where p_bma is the weighted average, meta has
        per-model weights and source Briers.
    """
    if not model_probs:
        return 0.5, {"reason": "no_models", "models": []}

    # Get per-model Brier weights
    if brier_overrides is not None:
        per_model = {m: {"brier": brier_overrides.get(m, DEFAULT_BRIER_FALLBACK)}
                     for m in model_probs}
        # Compute weights
        neg = {m: -float(d["brier"]) for m, d in per_model.items()}
        max_v = max(neg.values())
        exps = {m: math.exp(v - max_v) for m, v in neg.items()}
        z = sum(exps.values())
        for m in per_model:
            per_model[m]["weight"] = exps[m] / z if z > 0 else 1.0 / len(per_model)
    else:
        per_model = brier_score_per_model(model_field_map=model_field_map)
        # If a model in model_probs isn't in per_model (no field map
        # entry), give it the fallback Brier.
        for m in model_probs:
            if m not in per_model:
                per_model[m] = {
                    "brier": DEFAULT_BRIER_FALLBACK,
                    "n": 0,
                    "weight": 0.0,
                    "is_default": True,
                }
        # Re-softmax over all models we have weights for
        neg = {m: -float(per_model[m]["brier"]) for m in model_probs}
        max_v = max(neg.values())
        exps = {m: math.exp(v - max_v) for m, v in neg.items()}
        z = sum(exps.values())
        for m in model_probs:
            per_model[m]["weight"] = exps[m] / z if z > 0 else 1.0 / len(model_probs)

    # Weighted average
    p_bma = sum(per_model[m]["weight"] * float(p) for m, p in model_probs.items())
    p_bma = max(0.0, min(1.0, p_bma))

    return p_bma, {
        "p_bma": round(p_bma, 4),
        "n_models": len(model_probs),
        "models": {
            m: {"p": round(model_probs[m], 4),
                "brier": per_model[m]["brier"],
                "weight": round(per_model[m]["weight"], 4)}
            for m in model_probs
        },
    }
