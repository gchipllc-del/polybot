"""Tests for the Hermes significance guard (lib/hermes_significance.py).

The guard's defining property is that it is CONSERVATIVE: applied as a
post-filter it may only drop a recommendation or shrink its confidence — never
strengthen or add one. These tests pin that property plus the small-sample
behavior that motivated it.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.hermes_significance import (
    wilson_bounds, sample_factor, wr_distinguishable_from,
    temper_recommendations, MIN_ACT_N, FULL_CONFIDENCE_N,
)


def test_wilson_bounds_basics():
    lo, hi = wilson_bounds(0, 0)
    assert (lo, hi) == (0.0, 1.0)  # no data => know nothing
    lo, hi = wilson_bounds(2, 4)   # 50% on n=4: very wide
    assert lo < 0.25 and hi > 0.75
    lo, hi = wilson_bounds(15, 30)  # 50% on n=30: tighter
    assert lo > 0.30 and hi < 0.70
    # interval always brackets the point estimate
    for wins, n in [(1, 4), (7, 10), (20, 30), (49, 50)]:
        lo, hi = wilson_bounds(wins, n)
        assert lo <= wins / n <= hi


def test_sample_factor_monotone_and_bounded():
    assert sample_factor(0) == 0.0
    assert sample_factor(4) < sample_factor(15) < sample_factor(30)
    assert sample_factor(FULL_CONFIDENCE_N) == 1.0
    assert sample_factor(1000) == 1.0  # capped at 1


def test_wr_distinguishable_requires_real_evidence():
    # 1/4 losing read: cannot conclude it's truly below 50%
    assert wr_distinguishable_from(1, 4, ref=0.50, direction="below") is False
    # 2/30 losing read: upper bound well under 50% -> distinguishable
    assert wr_distinguishable_from(2, 30, ref=0.50, direction="below") is True
    # 28/30 winning read: lower bound above 50% -> distinguishable above
    assert wr_distinguishable_from(28, 30, ref=0.50, direction="above") is True


def test_temper_drops_tiny_sample_recs():
    recs = [{"param": "btc__theo_align_min_yes", "direction": "increase",
             "confidence": 0.9, "reason": "YES side WR 25% on 4 trades"}]
    out = temper_recommendations(recs, {"YES": {"n": 4, "wins": 1}}, n_total=8)
    # n_eff=4 < MIN_ACT_N -> dropped -> only the synthetic hold remains
    assert len(out) == 1 and out[0]["direction"] == "hold"


def test_temper_shrinks_but_keeps_adequate_sample():
    recs = [{"param": "btc__min_confidence", "direction": "increase",
             "confidence": 1.0, "reason": "WR 30% on 20 trades"}]
    out = temper_recommendations(recs, {}, n_total=20)
    assert len(out) == 1
    kept = out[0]
    assert kept["param"] == "btc__min_confidence"
    # confidence scaled by sample_factor(20) = 20/30 ~= 0.667
    assert 0.60 < kept["confidence"] < 0.70
    assert kept["confidence"] < 1.0  # never strengthened


def test_temper_is_conservative_never_strengthens():
    # Property test: for every rec, output confidence <= input confidence,
    # and the output never contains a param the input lacked.
    recs = [
        {"param": "a", "direction": "increase", "confidence": 0.8, "reason": "WR x on 50 trades"},
        {"param": "b", "direction": "decrease", "confidence": 0.5, "reason": "NO side y on 15 trades"},
        {"param": "none", "direction": "hold", "confidence": 0.0, "reason": "ok"},
    ]
    in_params = {r["param"] for r in recs}
    out = temper_recommendations(recs, {"NO": {"n": 15}}, n_total=50)
    out_params = {r["param"] for r in out}
    assert out_params <= in_params | {"none"}
    by_in = {r["param"]: r["confidence"] for r in recs}
    for r in out:
        if r["param"] in by_in:
            assert r["confidence"] <= by_in[r["param"]] + 1e-9


def test_holds_pass_through_untouched():
    recs = [{"param": "none", "direction": "hold", "confidence": 0.0, "reason": "ok"}]
    out = temper_recommendations(recs, {}, n_total=3)
    assert out == recs


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
