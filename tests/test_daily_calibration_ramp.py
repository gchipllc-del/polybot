"""Regression: the BTC daily BSM correction must NOT snap on as a cliff and must
stay within the tightened [CF_MIN, CF_MAX] clamp. Audit #170 (2026-06-01).

Before: factor jumped 1.0 -> stored (e.g. 0.30) in one trade at n==10, which on
a near-money book could flip the chosen side overnight. Now it ramps smoothly
1.0 -> stored across n in [MIN_BIAS_SAMPLES, RAMP_FULL_N] and is clamped to
[0.6, 1.4] (the best live runs used ~0.80).
"""
import importlib
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lib.kalshi_daily_calibration as cal


def _seed(tmp_path, n, yes_rate, theo=0.50):
    importlib.reload(cal)
    st = tmp_path / "cal.json"
    cal.STATE_PATH = st
    raw = yes_rate / theo
    stored = round(max(cal.CF_MIN, min(cal.CF_MAX, raw)), 4)
    st.write_text(json.dumps({"btc": {
        "samples": [{"theo": theo, "yes": 1 if i < round(yes_rate * n) else 0}
                    for i in range(n)],
        "n": n, "observed_yes_rate": round(yes_rate, 4),
        "mean_theo": theo, "correction_factor": stored,
    }}))
    return cal


def test_identity_below_min_samples(tmp_path):
    c = _seed(tmp_path, 8, 0.15)
    info = c.get_correction("btc")
    assert info["applied"] is False
    assert info["correction_factor"] == 1.0


def test_no_cliff_at_min_samples(tmp_path):
    # At exactly n=MIN_BIAS_SAMPLES the ramp weight is 0 -> factor still 1.0,
    # so there is no instantaneous jump from identity.
    c = _seed(tmp_path, cal.MIN_BIAS_SAMPLES, 0.15)  # raw 0.30 -> stored 0.6
    info = c.get_correction("btc")
    assert info["applied"] is True
    assert abs(info["correction_factor"] - 1.0) < 1e-9, "must start at identity, no cliff"


def test_ramps_smoothly_to_stored(tmp_path):
    # Halfway through the ramp (n=20 with [10,30]) the factor is the midpoint
    # between 1.0 and the stored 0.6 -> ~0.8.
    c = _seed(tmp_path, 20, 0.15)  # raw 0.30 -> clamped stored 0.6
    info = c.get_correction("btc")
    assert 0.78 <= info["correction_factor"] <= 0.82


def test_clamp_floor_blocks_pathological_haircut(tmp_path):
    # Even at full ramp, a raw factor of 0.30 is clamped to CF_MIN=0.6 — the
    # old behavior (0.30, a 70% haircut) is now impossible.
    c = _seed(tmp_path, cal.RAMP_FULL_N, 0.15)  # raw 0.30
    info = c.get_correction("btc")
    assert info["correction_factor"] >= cal.CF_MIN
    assert info["correction_factor"] == cal.CF_MIN  # fully ramped to the floor


def test_good_run_factor_passes_through(tmp_path):
    # The proven good-run regime (~0.80) is inside the clamp and reachable.
    c = _seed(tmp_path, cal.RAMP_FULL_N, 0.40)  # 0.40/0.50 = 0.80
    info = c.get_correction("btc")
    assert abs(info["correction_factor"] - 0.80) < 1e-6


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
