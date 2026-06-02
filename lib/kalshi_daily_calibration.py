"""Per-asset BSM calibration for the Kalshi daily strategy.

Same pattern as lib/weather_calibration.py: after each settled trade we
record (theo_yes_at_entry, actual_outcome). Once we have ≥ N samples,
compute a per-asset correction factor that maps the BSM model's
theoretical_yes to the empirical hit rate.

Why we need this: cross-asset paper data over the last 7 days shows:
  • BTC: 38% WR (BSM says ~50%, reality 38%) → mild over-estimate
  • ETH:  8% WR (BSM says ~50%, reality  8%) → severe over-estimate
  • SOL:  0% WR (BSM says ~50%, reality  0%) → totally broken
The BSM model assumes near-money strikes are coin-flips. They're not —
near-money strikes systematically LOSE because price drifts during the
session window (and BSM doesn't know which way).

How calibration works:
  1. After each settled trade: record (theo_yes_predicted, won_or_lost)
  2. Keep a rolling window of last 30 settled trades per asset
  3. When window has ≥ MIN_BIAS_SAMPLES, compute:
       observed_yes_rate = wins / total
       theo_yes_correction = observed_yes_rate / mean(theo_yes_predicted)
  4. Next signal cycle multiplies theo_yes by correction factor BEFORE
     deciding which side to bet
  5. If correction << 1.0, the model is over-confident — fewer trades fire
  6. Persisted to data/kalshi_daily_calibration.json keyed by asset

State storage atomic with fsync (same pattern as weather + rate-limit memory).
"""
from __future__ import annotations

import json
import os
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "kalshi_daily_calibration.json"

# Rolling window — 30 trades per asset balances responsiveness with stability.
# BTC currently does ~6 settled trades/day so 30 = ~5 days of history.
ROLLING_WINDOW = 30

# Min samples before a correction factor STARTS to apply. Below this we still
# RECORD outcomes but return the identity (no correction) so we don't
# overfit to tiny samples.
MIN_BIAS_SAMPLES = 10

# Sample count at which the correction reaches FULL strength. Between
# MIN_BIAS_SAMPLES and RAMP_FULL_N the factor is linearly blended from 1.0
# (identity) toward the empirical value, so it can never SNAP on at n=10 and
# flip a side overnight (the old behavior jumped 1.0→factor in one trade).
RAMP_FULL_N = 30

# Hard clamp on the correction factor. Tightened from the old [0.3, 2.0] so no
# single rolling window can apply more than a 40% haircut/boost — keeps the
# correction near the ~0.80 that the best live runs used and prevents the
# S-curve-bending extreme (0.3) the audit flagged.
CF_MIN = 0.6
CF_MAX = 1.4


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(STATE_PATH)
    except OSError:
        pass


def record_outcome(asset: str, theo_yes_at_entry: float, yes_resolved: bool) -> None:
    """Append a (theo_yes, yes_resolved) pair to the asset's rolling window.

    Called from kalshi_daily_paper.settle_paper_trades after each trade
    resolves.

    CONTRACT — do NOT pass chosen-side win/loss here:
      `yes_resolved` MUST be whether the market's YES side resolved TRUE
      (did the underlying close above the strike?), REGARDLESS of which
      side the bot bet. theo_yes is a P(YES) estimate, so calibration must
      map it to the empirical P(YES) — how often YES actually happened.
      NO-side bets benefit automatically: P(NO wins) = 1 - corrected_theo_yes.

      Passing chosen-side-won instead silently INVERTS the mapping (a
      winning NO bet would be recorded as "YES happened"). That was the
      original conflation bug — keep prediction and outcome decoupled
      from side selection.

    The math: record what the model PREDICTED (theo_yes) and what HAPPENED
    (YES true/false). Comparing average predicted P(YES) vs the observed
    YES rate gives the calibration factor."""
    if theo_yes_at_entry is None:
        return
    state = _load_state()
    bucket = state.setdefault(asset, {"samples": []})
    samples = bucket.get("samples") or []
    samples.append({
        "theo": round(float(theo_yes_at_entry), 4),
        "yes": 1 if yes_resolved else 0,
    })
    if len(samples) > ROLLING_WINDOW:
        samples = samples[-ROLLING_WINDOW:]
    bucket["samples"] = samples
    # Compute + cache the derived stats
    bucket["n"] = len(samples)
    if samples:
        # observed_yes_rate = how often YES actually resolved true. This is
        # NOT the bot's trade win rate (the bot bets both sides). Tolerates
        # legacy "won" sample keys written before the Task #140 rename.
        bucket["observed_yes_rate"] = round(
            sum(s.get("yes", s.get("won", 0)) for s in samples) / len(samples), 4)
        bucket["mean_theo"] = round(statistics.mean(s["theo"] for s in samples), 4)
        # Correction factor: scales theo_yes to match the observed YES rate.
        # E.g., model mean P(YES)=50% but YES only happened 25% → factor 0.5.
        # Clamp tightened 2026-06-01 from [0.3, 2.0] → [CF_MIN, CF_MAX] = [0.6,
        # 1.4]. WHY: the old [0.3,2.0] let a tiny biased window apply a 70%
        # haircut to every theo (factor 0.3), which bends the BSM S-curve and
        # can flip the chosen side wholesale. The bot's BEST live runs (05-26/27,
        # +$53/+$62, near-perfect WR) ran with a factor of ~0.80 — a MILD ~20%
        # haircut. A ≤40% clamp keeps the correction in that proven-good
        # neighborhood and makes the pathological 0.30 impossible.
        if bucket["mean_theo"] > 0:
            raw_factor = bucket["observed_yes_rate"] / bucket["mean_theo"]
            bucket["correction_factor"] = round(max(CF_MIN, min(CF_MAX, raw_factor)), 4)
        else:
            bucket["correction_factor"] = 1.0
    state[asset] = bucket
    _save_state(state)


def get_correction(asset: str) -> dict:
    """Return {correction_factor, n, observed_yes_rate, mean_theo, applied}.

    observed_yes_rate is the empirical P(YES) over the rolling window — how
    often the market's YES side actually resolved true — NOT the bot's trade
    win rate (the bot bets both sides). See record_outcome's CONTRACT.

    Returns identity (1.0) below MIN_BIAS_SAMPLES, then SMOOTHLY RAMPS the
    factor from 1.0 toward the stored empirical value as n grows to
    RAMP_FULL_N. This removes the old discontinuity where the factor snapped
    from 1.0 to its full value in a single trade at n==MIN_BIAS_SAMPLES — which
    on a near-money book could flip the chosen side overnight. The effective
    factor is also re-clamped to [CF_MIN, CF_MAX] as a belt-and-suspenders.
    """
    state = _load_state()
    bucket = state.get(asset, {})
    n = int(bucket.get("n", 0) or 0)
    # Tolerate legacy "observed_wr" key written before the Task #140 rename.
    observed_yes_rate = bucket.get("observed_yes_rate", bucket.get("observed_wr"))
    if n < MIN_BIAS_SAMPLES:
        return {"correction_factor": 1.0, "n": n,
                "observed_yes_rate": observed_yes_rate,
                "mean_theo": bucket.get("mean_theo"),
                "applied": False, "ramp": 0.0}
    stored = float(bucket.get("correction_factor", 1.0))
    # Linear ramp weight in [0,1] across [MIN_BIAS_SAMPLES, RAMP_FULL_N].
    span = max(1, RAMP_FULL_N - MIN_BIAS_SAMPLES)
    ramp = max(0.0, min(1.0, (n - MIN_BIAS_SAMPLES) / span))
    effective = 1.0 + (stored - 1.0) * ramp        # eases 1.0 -> stored
    effective = round(max(CF_MIN, min(CF_MAX, effective)), 4)
    return {"correction_factor": effective,
            "n": n,
            "observed_yes_rate": observed_yes_rate,
            "mean_theo": bucket.get("mean_theo"),
            "applied": True, "ramp": round(ramp, 3),
            "stored_factor": round(stored, 4)}


def apply_correction(asset: str, theo_yes_raw: float) -> tuple[float, dict]:
    """Apply the per-asset correction to a raw theo_yes. Returns the
    corrected value AND the correction metadata for logging.

    Bounded to [0.02, 0.98] to keep downstream BSM math sane (same
    clipping the model already does on raw theo_yes)."""
    if theo_yes_raw is None:
        return theo_yes_raw, {"applied": False, "reason": "no_input"}
    info = get_correction(asset)
    if not info.get("applied"):
        return theo_yes_raw, info
    factor = info["correction_factor"]
    corrected = max(0.02, min(0.98, theo_yes_raw * factor))
    return corrected, info


def reset(asset: str | None = None) -> None:
    """Wipe calibration for one asset (or all). Use after major model
    changes that invalidate prior samples."""
    state = _load_state()
    if asset is None:
        state = {}
    else:
        state.pop(asset, None)
    _save_state(state)


def calibration_report() -> str:
    """Human-readable snapshot — used by CLI + daily review."""
    state = _load_state()
    if not state:
        return "  (no calibration data yet)"
    lines = []
    for asset, b in state.items():
        n = b.get("n", 0)
        applied = "✓" if n >= MIN_BIAS_SAMPLES else "✗ (waiting for n≥{})".format(MIN_BIAS_SAMPLES)
        obs = b.get("observed_yes_rate", b.get("observed_wr"))
        mean_theo = b.get("mean_theo")
        factor = b.get("correction_factor", 1.0)
        obs_s = f"{obs*100:.0f}%" if obs is not None else "n/a"
        mean_s = f"{mean_theo:.3f}" if mean_theo is not None else "n/a"
        lines.append(
            f"  {asset:<5}  n={n:<3}  observed_yes={obs_s}  mean_theo={mean_s}  "
            f"correction={factor:.3f}  {applied}"
        )
    return "\n".join(lines)


__all__ = [
    "record_outcome", "get_correction", "apply_correction",
    "reset", "calibration_report",
    "ROLLING_WINDOW", "MIN_BIAS_SAMPLES",
]
