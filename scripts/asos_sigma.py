#!/usr/bin/env python3
"""asos_sigma — measure the "degree or two" the thermometer/CLI is uncertain by.

asos_tracker models the settled daily max as Normal(observed_high, σ) and locks when
P(correct side) = Φ((observed − strike)/σ) ≥ MIN_LOCK_PROB. That σ (SETTLE_SIGMA_F,
default 1.5°F) is currently a guess. This fits it from the data: across settled
above-strike locks it finds the σ that best calibrates Φ((qc_high − strike)/σ) to the
realized win/lost outcomes (max-likelihood), so you can replace the guess with a number.
Read-only.

  python scripts/asos_sigma.py                 # fit σ from data/asos_lock.jsonl
  python scripts/asos_sigma.py --selftest

NOTE: a consistently-wrong station inflates the fitted σ (systematic misses look like
noise). If σ̂ comes back large (>~3°F), suspect a station mismatch (run asos_tracker
probe) rather than just widening σ.
"""
import argparse
import json
from math import log
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "asos_lock.jsonl"
_N = NormalDist()
MIN_LOCK_PROB = 0.95          # keep in sync with asos_tracker.MIN_LOCK_PROB
DEFAULT_SIGMA = 1.5           # asos_tracker.SETTLE_SIGMA_F


def _clip(p, eps=1e-6):
    return min(max(p, eps), 1.0 - eps)


def loglik(pairs, bias, sigma) -> float:
    """Σ log P(outcome | Φ((margin − bias)/σ)).  pairs = [(margin = qc_high−strike, yes)].
    `bias` > 0 means our reading runs HOT (margin overstated): the true margin is margin−bias."""
    if sigma <= 0:
        return float("-inf")
    ll = 0.0
    for m, y in pairs:
        p = _clip(_N.cdf((m - bias) / sigma))
        ll += (log(p) if y else log(1.0 - p))
    return ll


def fit_sigma(pairs, lo=0.3, hi=6.0, step=0.05):
    """Grid-search MLE for σ with bias fixed at 0 (the naive fit — kept for comparison)."""
    best_s, best_ll = None, float("-inf")
    s = lo
    while s <= hi + 1e-9:
        ll = loglik(pairs, 0.0, s)
        if ll > best_ll:
            best_ll, best_s = ll, round(s, 2)
        s += step
    return best_s, best_ll


def fit_bias_sigma(pairs, b_lo=-2.0, b_hi=8.0, b_step=0.25,
                   s_lo=0.3, s_hi=4.0, s_step=0.1):
    """Joint grid-search MLE for (bias, σ). Separating bias from σ is the whole point:
    a one-directional reading error shows up as bias (fixable in the pipeline), while the
    residual σ is the true sensor/CLI noise that the distance-aware lock should use."""
    best, best_ll = (None, None), float("-inf")
    b = b_lo
    while b <= b_hi + 1e-9:
        s = s_lo
        while s <= s_hi + 1e-9:
            ll = loglik(pairs, b, s)
            if ll > best_ll:
                best_ll, best = ll, (round(b, 2), round(s, 2))
            s += s_step
        b += b_step
    return best[0], best[1], best_ll


def load_pairs(path: Path) -> list:
    """(margin = qc_high−strike, yes_outcome) for settled above-strike locks."""
    pairs = []
    if not path.exists():
        return pairs
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("kind") != "above" or str(r.get("status")) not in ("won", "lost"):
            continue
        high = r.get("qc_high", r.get("realized_high"))
        strike = r.get("strike")
        if high is None or strike is None:
            continue
        margin = float(high) - float(strike)
        y = 1 if str(r.get("result")).lower() == "yes" else 0
        pairs.append((margin, y))
    return pairs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=str(LOG))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())

    pairs = load_pairs(Path(args.path))
    print("=== asos_sigma — fitting the settled-max uncertainty σ ===")
    if len(pairs) < 8:
        print(f"  only {len(pairs)} settled above-strike locks — need ~8+ for a stable fit. Keep collecting.")
        return
    s_only, ll_s = fit_sigma(pairs)                       # naive (bias≡0) fit, for contrast
    bias, sigma, ll_bs = fit_bias_sigma(pairs)            # joint fit — separates bias from noise
    eff_margin = _N.inv_cdf(MIN_LOCK_PROB) * sigma
    print(f"  n={len(pairs)} settled above-strike locks")
    print(f"  σ-only fit (assumes zero bias): σ̂ = {s_only:.2f}°F   (LL {ll_s:.1f})")
    print(f"  JOINT fit:  bias = {bias:+.2f}°F   residual σ̂ = {sigma:.2f}°F   (LL {ll_bs:.1f})")
    print(f"  current default SETTLE_SIGMA_F = {DEFAULT_SIGMA}")
    if abs(bias) >= 2.0:
        print(f"  ⚠ bias {bias:+.2f}°F dominates — our reading runs {'HOT' if bias > 0 else 'COLD'} by ~{abs(bias):.0f}°F.")
        print(f"    This is a DATA-PIPELINE bug (wrong day-window / settlement source), NOT σ. The σ-only")
        print(f"    fit inflates σ̂ to {s_only:.1f} just to absorb it. Fixing the bias is what rescues the sleeve;")
        print(f"    then the true sensor σ ≈ {sigma:.1f}°F is what the distance-aware lock should use.")
    else:
        print(f"  bias is small — σ̂ ≈ {sigma:.1f}°F is genuine sensor/CLI noise; set SETTLE_SIGMA_F to it.")

    # Calibration: predicted under the JOINT (bias-corrected) model vs realized hit-rate.
    print("\n  calibration — bias-corrected model vs realized P(settled>strike):")
    print("   margin band    n   pred(joint)   realized")
    for lo_b, hi_b in [(-99, -3), (-3, -1), (-1, 1), (1, 3), (3, 99)]:
        grp = [(m, y) for m, y in pairs if lo_b <= m < hi_b]
        if not grp:
            continue
        pred = sum(_N.cdf((m - bias) / sigma) for m, _ in grp) / len(grp)
        real = sum(y for _, y in grp) / len(grp)
        print(f"   [{lo_b:+3.0f},{hi_b:+3.0f})  {len(grp):3d}     {pred:5.2f}       {real:5.2f}")

    print("\n  Read: if bias ≫ 0 the lock sleeve is reading a hotter max than Kalshi settles on —")
    print("  fix the day-window in asos_tracker.fetch_iem_today before trusting any lock again.")


def _selftest() -> int:
    import random
    rng = random.Random(7)
    # 1) zero-bias data: σ-only fit recovers σ.
    true_sigma = 2.0
    clean = []
    for _ in range(4000):
        m = rng.uniform(-6, 6)
        clean.append((m, 1 if rng.random() < _N.cdf(m / true_sigma) else 0))
    sigma, _ = fit_sigma(clean)
    assert abs(sigma - true_sigma) <= 0.3, sigma
    # 2) biased data: outcome ~ Bernoulli(Φ((margin − b_true)/σ_true)). Joint fit must
    #    recover BOTH, and the σ-only fit must inflate σ (the bug we're diagnosing).
    b_true, s_true = 4.0, 1.5
    biased = []
    for _ in range(6000):
        m = rng.uniform(-4, 10)
        biased.append((m, 1 if rng.random() < _N.cdf((m - b_true) / s_true) else 0))
    b_hat, s_hat, _ = fit_bias_sigma(biased)
    assert abs(b_hat - b_true) <= 0.5 and abs(s_hat - s_true) <= 0.4, (b_hat, s_hat)
    s_only, _ = fit_sigma(biased)
    assert s_only > s_hat + 1.0, (s_only, s_hat)          # σ-only absorbs the bias → inflated
    print(f"fit_sigma σ≈{sigma} (true 2.0); joint bias≈{b_hat}/σ≈{s_hat} (true 4.0/1.5), "
          f"σ-only inflates to {s_only} OK")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
