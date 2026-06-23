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


def loglik(pairs, sigma) -> float:
    """Σ log P(outcome | Φ(margin/σ)).  pairs = [(margin = qc_high−strike, yes_outcome)]."""
    if sigma <= 0:
        return float("-inf")
    ll = 0.0
    for m, y in pairs:
        p = _clip(_N.cdf(m / sigma))
        ll += (log(p) if y else log(1.0 - p))
    return ll


def fit_sigma(pairs, lo=0.3, hi=6.0, step=0.05):
    """Grid-search MLE for σ."""
    best_s, best_ll = None, float("-inf")
    s = lo
    while s <= hi + 1e-9:
        ll = loglik(pairs, s)
        if ll > best_ll:
            best_ll, best_s = ll, round(s, 2)
        s += step
    return best_s, best_ll


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
    sigma, ll = fit_sigma(pairs)
    eff_margin = _N.inv_cdf(MIN_LOCK_PROB) * sigma
    print(f"  n={len(pairs)} settled above-strike locks")
    print(f"  fitted σ̂ = {sigma:.2f}°F   (current default SETTLE_SIGMA_F = {DEFAULT_SIGMA})")
    print(f"  → at MIN_LOCK_PROB={MIN_LOCK_PROB:.0%} that's a {eff_margin:.1f}°F effective lock margin")
    print(f"  log-likelihood: fitted {ll:.2f}  vs  default σ={DEFAULT_SIGMA} {loglik(pairs, DEFAULT_SIGMA):.2f}")

    # Calibration: bucket by |margin| and show predicted (at σ̂) vs realized hit-rate.
    print("\n  calibration at σ̂ (predicted vs realized P(settled>strike)):")
    print("   margin band    n   pred   realized")
    bands = [(-99, -3), (-3, -1), (-1, 1), (1, 3), (3, 99)]
    for lo_b, hi_b in bands:
        grp = [(m, y) for m, y in pairs if lo_b <= m < hi_b]
        if not grp:
            continue
        pred = sum(_N.cdf(m / sigma) for m, _ in grp) / len(grp)
        real = sum(y for _, y in grp) / len(grp)
        print(f"   [{lo_b:+3.0f},{hi_b:+3.0f})  {len(grp):3d}  {pred:5.2f}    {real:5.2f}")

    print("\n  Read: set SETTLE_SIGMA_F to σ̂ if it differs from the default. If σ̂ > ~3°F, that's")
    print("  likely a wrong-station bias, not sensor noise — run asos_tracker probe on the misses.")


def _selftest() -> int:
    # Recover a known σ from synthetic data: outcome ~ Bernoulli(Φ(margin/σ_true)).
    import random
    rng = random.Random(7)
    true_sigma = 2.0
    pairs = []
    for _ in range(4000):
        m = rng.uniform(-6, 6)
        p = _N.cdf(m / true_sigma)
        pairs.append((m, 1 if rng.random() < p else 0))
    sigma, _ = fit_sigma(pairs)
    assert abs(sigma - true_sigma) <= 0.3, sigma          # MLE recovers σ within grid noise
    # loglik is maximized near the truth (better than a far-off σ)
    assert loglik(pairs, true_sigma) > loglik(pairs, 0.5), "truth should beat tiny σ"
    assert loglik(pairs, true_sigma) > loglik(pairs, 5.0), "truth should beat huge σ"
    print(f"fit_sigma recovered σ≈{sigma} (true {true_sigma}) OK")
    print("PASS")
    return 0


if __name__ == "__main__":
    main()
