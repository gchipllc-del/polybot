"""Significance guards for the Hermes self-optimization loops (READ-ONLY math).

WHY THIS EXISTS
---------------
The daily/weather `diagnose()` functions propose live-parameter changes off
very small per-side samples (rules fire at n>=4-10). A win-rate measured on
4-10 binary trades has a standard error of ~15-25pp, so a "WR 30%" read is
statistically indistinguishable from a true 50% rate — the optimizer was
treating an unlucky 1-in-4 streak as a real directional bias and tightening
(or loosening) live gates against noise.

This module provides two pure helpers used to make every recommendation
*confidence-aware of sample size*. It is deliberately CONSERVATIVE: applied as
a post-filter it can only DROP or WEAKEN a recommendation, never create or
strengthen one — so wiring it in can never make the bot trade more aggressively
than the un-guarded code would have.

  wilson_bounds(wins, n)  -> (lo, hi) 95% Wilson score interval for a binomial
                             proportion. Used to ask "is this WR distinguishable
                             from a coin flip given n?".
  sample_factor(n)        -> in [0,1], = min(1, n / FULL_CONFIDENCE_N). Shrinks a
                             recommendation's confidence toward 0 for tiny n so a
                             4-trade read can never outrank a 30-trade read in
                             pick_one_change().

No I/O, no global state — just math, so it is trivially safe to import anywhere.
"""
from __future__ import annotations

import math

# Sample size at which a recommendation earns its FULL stated confidence.
# Below this, confidence is scaled down linearly. 30 is the usual rule-of-thumb
# floor for a proportion estimate to start behaving.
FULL_CONFIDENCE_N = 30

# Minimum trades before a per-rule recommendation is allowed to ACT at all.
# Rules in diagnose() historically fired at n>=4; this raises the floor.
MIN_ACT_N = 12

_Z95 = 1.95996398454  # z for 95% two-sided


def wilson_bounds(wins: int, n: int, z: float = _Z95) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion.

    Returns (lo, hi), both clamped to [0,1]. For n<=0 returns (0.0, 1.0) — i.e.
    "we know nothing", which by construction makes any significance test fail
    (the interval spans every hypothesis). Wilson is used instead of the naive
    normal interval because it stays sane at small n and near 0/1.
    """
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denom
    lo = max(0.0, center - half)
    hi = min(1.0, center + half)
    return lo, hi


def sample_factor(n: int, full_n: int = FULL_CONFIDENCE_N) -> float:
    """Shrink factor in [0,1] for a recommendation's confidence given sample n.

    = min(1, n / full_n). A 4-trade rec is multiplied by ~0.13, a 15-trade rec
    by 0.5, a 30+-trade rec by 1.0. Guarantees small-sample recs sink to the
    bottom of pick_one_change()'s confidence-sorted list.
    """
    if n <= 0:
        return 0.0
    return min(1.0, n / float(full_n))


def wr_distinguishable_from(wins: int, n: int, ref: float = 0.50,
                            direction: str = "below") -> bool:
    """Is the observed win-rate *significantly* on one side of `ref`?

    direction="below": True only if the Wilson UPPER bound < ref (we can be
        ~95% confident the true WR is below ref — justifies tightening a gate).
    direction="above": True only if the Wilson LOWER bound > ref (justifies
        loosening / scaling up).
    Anything inside the interval -> False (inconclusive; don't act on noise).
    """
    lo, hi = wilson_bounds(wins, n)
    if direction == "below":
        return hi < ref
    if direction == "above":
        return lo > ref
    return False


def temper_recommendations(recs: list[dict], by_side: dict | None,
                           n_total: int) -> list[dict]:
    """Post-filter that makes a diagnose() rec list sample-size-aware.

    CONSERVATIVE BY CONSTRUCTION — for each rec it only ever:
      * scales `confidence` DOWN by sample_factor(n_effective), or
      * drops the rec entirely when n_effective < MIN_ACT_N.
    It never raises a confidence and never adds a rec, so a caller that swaps
    `recs = temper_recommendations(recs, ...)` in front of pick_one_change()
    can only become MORE cautious than before.

    n_effective: if the rec names a specific side (reason mentions 'YES'/'NO'
    and that side is in by_side) we use that side's n; otherwise n_total. This
    is best-effort (reasons are free text) and only ever makes the guard
    *tighter* when it can identify a small per-side sample.
    """
    by_side = by_side or {}
    out: list[dict] = []
    for r in recs:
        if r.get("param") in (None, "none") or r.get("direction") == "hold":
            out.append(r)  # pass holds through untouched
            continue
        n_eff = n_total
        reason = str(r.get("reason", ""))
        for side in ("YES", "NO"):
            sb = by_side.get(side)
            if sb and isinstance(sb, dict) and f" {side} " in f" {reason} ":
                n_eff = min(n_eff, int(sb.get("n", n_total)))
                break
        if n_eff < MIN_ACT_N:
            continue  # drop: not enough evidence to touch a live param
        r = dict(r)
        r["confidence"] = round(
            float(r.get("confidence", 0.0) or 0.0) * sample_factor(n_eff), 4)
        r["_n_effective"] = n_eff
        out.append(r)
    if not out:
        out.append({
            "param": "none", "direction": "hold", "confidence": 0.0,
            "reason": f"no recommendation cleared the significance guard "
                      f"(min n={MIN_ACT_N}, total n={n_total})",
        })
    return out
