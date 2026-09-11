"""Tests for the vendored tradingcore fallback shim (vendor/tradingcore).

The real tradingcore is a sibling repo present only on the original dev machine. Its
absence on a fresh clone was the most common cause of breakage in this project — it
aborted pip installs, 500'd the dashboards, and failed 44 tests. lib/__init__.py now falls
back to this shim when the real package is missing.

These tests pin the two things that matter:
  1. the API SHAPE matches how the repo actually calls it (notably brier_score() with no
     arguments, and record-list arguments rather than parallel arrays) — the mismatch that
     made the shim's first version fail;
  2. the math is textbook-correct, since these numbers gate real decisions.
"""
import math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lib  # noqa: F401  (bootstraps the fallback onto sys.path)
from tradingcore.audit import log_event, get_recent_events
from tradingcore.calibration import (brier_score, log_loss, calibration_curve,
                                     source_accuracy, record_forecast)
from tradingcore.kelly import (expected_value, kelly_fraction, fractional_kelly,
                               kelly_bet_size, kelly_bet_size_slippage_aware,
                               min_edge_for_trade, ensemble_dampener)


# ── API shape (how the repo really calls these) ──────────────────────────────

def test_brier_score_callable_with_no_args():
    # lib/forecaster.py and lib/dashboard_data.py call brier_score() bare.
    v = brier_score()
    assert isinstance(v, float)          # nan when nothing recorded, never a raise


def test_metrics_accept_record_lists():
    # lib/backtest.py passes a list of dicts, not parallel arrays.
    recs = [{"our_probability": 0.9, "outcome": True},
            {"our_probability": 0.1, "outcome": False}]
    assert abs(brier_score(recs) - 0.01) < 1e-12
    assert isinstance(log_loss(recs), float)


def test_no_arg_calls_never_raise():
    for fn in (brier_score, log_loss, calibration_curve, source_accuracy):
        fn()


# ── math correctness ─────────────────────────────────────────────────────────

def test_brier_is_mean_squared_error():
    recs = [{"forecast": 1.0, "outcome": True}, {"forecast": 0.0, "outcome": False}]
    assert brier_score(recs) == 0.0                      # perfect
    recs = [{"forecast": 0.0, "outcome": True}]
    assert brier_score(recs) == 1.0                      # maximally wrong
    recs = [{"forecast": 0.5, "outcome": True}, {"forecast": 0.5, "outcome": False}]
    assert abs(brier_score(recs) - 0.25) < 1e-12         # coin flip


def test_log_loss_matches_formula():
    recs = [{"forecast": 0.9, "outcome": True}]
    assert abs(log_loss(recs) - (-math.log(0.9))) < 1e-9


def test_calibration_curve_bins_and_realized():
    recs = ([{"forecast": 0.05, "outcome": False}] * 4 +
            [{"forecast": 0.95, "outcome": True}] * 6)
    curve = calibration_curve(recs, bins=10)
    assert len(curve) == 2
    lo, hi = curve[0], curve[-1]
    assert lo["n"] == 4 and lo["realized"] == 0.0
    assert hi["n"] == 6 and hi["realized"] == 1.0


def test_expected_value_binary():
    # p=0.6 at price 0.5: 0.6*0.5 - 0.4*0.5 = +0.10
    assert abs(expected_value(0.6, 0.5) - 0.10) < 1e-12
    # fair price -> zero edge
    assert abs(expected_value(0.5, 0.5)) < 1e-12


def test_kelly_fraction_textbook():
    # b = (1-0.5)/0.5 = 1 ; f = (p(b+1)-1)/b = 0.2 at p=0.6
    assert abs(kelly_fraction(0.6, 0.5) - 0.2) < 1e-12
    # no edge or negative edge -> never bet
    assert kelly_fraction(0.5, 0.5) == 0.0
    assert kelly_fraction(0.3, 0.5) == 0.0


def test_kelly_bet_size_scales_with_bankroll_and_fraction():
    full = kelly_bet_size(0.6, 0.5, bankroll=1000, fraction=1.0)
    half = kelly_bet_size(0.6, 0.5, bankroll=1000, fraction=0.5)
    assert abs(full - 200.0) < 1e-9 and abs(half - 100.0) < 1e-9


def test_slippage_aware_sizes_smaller():
    a = kelly_bet_size(0.6, 0.5, 1000, 0.25)
    b = kelly_bet_size_slippage_aware(0.6, 0.5, 1000, 0.25, slippage=0.05)
    assert b < a, "paying up must reduce size"


def test_min_edge_covers_friction():
    assert min_edge_for_trade(0.5, fee=0.02, spread=0.02, margin=0.01) == 0.02 + 0.01 + 0.01


def test_ensemble_dampener_bounds():
    assert ensemble_dampener(0) == 0.0
    assert 0.0 <= ensemble_dampener(1) <= 1.0
    assert ensemble_dampener(10) > ensemble_dampener(1)   # more sources -> less damping


# ── audit log ────────────────────────────────────────────────────────────────

def test_audit_roundtrip(tmp_path, monkeypatch):
    import tradingcore.audit as au
    monkeypatch.setattr(au, "_LOG", tmp_path / "audit.jsonl")
    au.log_event("unit", "thing_happened", {"k": 1}, result="ok")
    got = au.get_recent_events(limit=5)
    assert got and got[0]["event"] == "thing_happened" and got[0]["source"] == "unit"


def test_audit_never_raises_on_unwritable_path(monkeypatch):
    import tradingcore.audit as au
    monkeypatch.setattr(au, "_LOG", Path("/nonexistent-root/x/y.jsonl"))
    au.log_event("unit", "should_not_raise")      # audit must never take a caller down


def test_record_forecast_then_score(tmp_path, monkeypatch):
    import tradingcore.calibration as cal
    monkeypatch.setattr(cal, "_LOG", tmp_path / "fc.jsonl")
    cal.record_forecast(source="s", forecast=0.8, outcome=True)
    cal.record_forecast(source="s", forecast=0.2, outcome=False)
    assert abs(cal.brier_score() - 0.04) < 1e-12      # (0.04+0.04)/2
