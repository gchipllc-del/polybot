"""Tests for the opt-in significance guard added to tradingcore.hermes_ledger
.close_experiment (2026-06-01).

Pins two properties:
  1. BACKWARD COMPAT: with min_post_samples=None (the traderbot path), behavior
     is identical to before — grade immediately on goal-distance delta.
  2. NEW GUARD: with min_post_samples set, an experiment with too few
     post-treatment trades is held OPEN ('inconclusive'), and only graded once
     the post-sample count is met.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tradingcore.hermes_ledger import open_experiment, close_experiment


def _open(tmp):
    return open_experiment(
        param="btc__min_confidence", old_value=0.30, new_value=0.35,
        reason="test", baseline_metrics={"goal_distance_pct": 0.50,
                                          "rolling_30d_pnl": 0.0},
        ledger_path=tmp,
    )


def test_backward_compat_grades_immediately(tmp_path):
    led = tmp_path / "exp.jsonl"
    exp = _open(led)
    # improved goal distance, no min_post_samples -> KEEP immediately (old behavior)
    r = close_experiment(exp["experiment_id"],
                         post_metrics={"goal_distance_pct": 0.40},
                         keep_threshold_delta=0.001, ledger_path=led)
    assert r["status"] == "kept" and r["verdict"] == "improved"


def test_backward_compat_rollback_immediately(tmp_path):
    led = tmp_path / "exp.jsonl"
    exp = _open(led)
    r = close_experiment(exp["experiment_id"],
                         post_metrics={"goal_distance_pct": 0.55},  # regressed
                         keep_threshold_delta=0.001, ledger_path=led)
    assert r["status"] == "rolled_back" and r["verdict"] == "regressed"


def test_guard_holds_open_when_insufficient_post_samples(tmp_path):
    led = tmp_path / "exp.jsonl"
    exp = _open(led)
    # Big apparent improvement, but only 3 post-treatment trades -> DON'T grade.
    r = close_experiment(exp["experiment_id"],
                         post_metrics={"goal_distance_pct": 0.10, "n_post_trades": 3},
                         keep_threshold_delta=0.001, min_post_samples=20,
                         ledger_path=led)
    assert r["status"] == "open"  # held, not kept
    assert r["verdict"] == "inconclusive_insufficient_post_samples"
    assert r["post_progress"]["n_post"] == 3 and r["post_progress"]["need"] == 20


def test_guard_grades_once_enough_post_samples(tmp_path):
    led = tmp_path / "exp.jsonl"
    exp = _open(led)
    # First pass: too few -> held open
    close_experiment(exp["experiment_id"],
                     post_metrics={"goal_distance_pct": 0.40, "n_post_trades": 5},
                     min_post_samples=20, ledger_path=led)
    # Later pass: enough post-trades -> now graded (improved -> kept)
    r = close_experiment(exp["experiment_id"],
                         post_metrics={"goal_distance_pct": 0.40, "n_post_trades": 25},
                         keep_threshold_delta=0.001, min_post_samples=20,
                         ledger_path=led)
    assert r["status"] == "kept" and r["verdict"] == "improved"


def test_guard_missing_count_treated_as_zero(tmp_path):
    led = tmp_path / "exp.jsonl"
    exp = _open(led)
    # min_post_samples set but post_metrics omits n_post_trades -> treat as 0 -> held
    r = close_experiment(exp["experiment_id"],
                         post_metrics={"goal_distance_pct": 0.10},
                         min_post_samples=20, ledger_path=led)
    assert r["status"] == "open"
    assert r["verdict"] == "inconclusive_insufficient_post_samples"


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
