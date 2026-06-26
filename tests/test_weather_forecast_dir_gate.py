"""Tests for the forecast-direction (coherence) gate in lib/weather_paper.py — the
2026-05-26 PM HALT FIX, extracted to the pure helper `_forecast_dir_ok`.

Locks the behavior that stopped the against-forecast bleed (a NO bet placed while the
NWS point-forecast pointed YES). On the paper ledger the gate cut against-forecast NO
trades from 45/59 (pre-gate) to 2/103 (post-gate), so a silent regression here would
re-open a real, money-losing hole.

The gate must:
  * pass a NO bet only when the forecast is clearly BELOW strike (by >= buffer),
  * pass a YES bet only when the forecast is clearly ABOVE strike (by >= buffer),
  * refuse the boundary/against-forecast cases with the right skip reason,
  * honor the asymmetric YES buffer (forecast_buffer_f_yes) when set,
  * be a no-op when forecast_dir_gate is disabled (the pre-HALT "original" behavior).
Pure function — no I/O, no ledger writes.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.weather_paper import _forecast_dir_ok, _effective_params


def _params(**over):
    p = {"forecast_dir_gate": True, "forecast_buffer_f": 0.5, "forecast_buffer_f_yes": None}
    p.update(over)
    return p


# ── NO side: forecast must be clearly BELOW strike ───────────────────────────

def test_no_passes_when_forecast_clearly_below():
    # strike 55, forecast 54.0 → 1.0°F below, beyond the 0.5°F buffer → trade allowed.
    assert _forecast_dir_ok(54.0, 55.0, "NO", _params()) == (True, "ok")


def test_no_blocked_at_buffer_boundary():
    # forecast 54.8 is only 0.2°F below strike → inside the 0.5°F buffer → refuse.
    ok, reason = _forecast_dir_ok(54.8, 55.0, "NO", _params())
    assert ok is False and reason == "forecast_dir_no"


def test_no_blocked_against_forecast():
    # The exact HALT-FIX case: bet NO while the forecast (56) is ABOVE the strike (55).
    ok, reason = _forecast_dir_ok(56.0, 55.0, "NO", _params())
    assert ok is False and reason == "forecast_dir_no"


def test_no_buffer_is_exclusive_at_exact_edge():
    # forecast exactly strike-buffer (54.5) is NOT > (strike-buffer) → allowed.
    assert _forecast_dir_ok(54.5, 55.0, "NO", _params()) == (True, "ok")


# ── YES side: forecast must be clearly ABOVE strike ──────────────────────────

def test_yes_passes_when_forecast_clearly_above():
    assert _forecast_dir_ok(56.0, 55.0, "YES", _params()) == (True, "ok")


def test_yes_blocked_within_buffer():
    ok, reason = _forecast_dir_ok(55.2, 55.0, "YES", _params())
    assert ok is False and reason == "forecast_dir_yes"


def test_yes_asymmetric_buffer_is_tighter():
    # With forecast_buffer_f_yes=1.0, YES needs forecast >= strike+1.0. 55.8 is short.
    p = _params(forecast_buffer_f_yes=1.0)
    assert _forecast_dir_ok(55.8, 55.0, "YES", p)[0] is False
    assert _forecast_dir_ok(56.0, 55.0, "YES", p) == (True, "ok")


def test_yes_buffer_falls_back_to_base_when_yes_unset():
    # forecast_buffer_f_yes None → uses base 0.5; 55.6 clears strike+0.5.
    assert _forecast_dir_ok(55.6, 55.0, "YES", _params()) == (True, "ok")


# ── master switch ────────────────────────────────────────────────────────────

def test_gate_off_allows_against_forecast():
    # Disabling the gate restores pre-HALT behavior: even an against-forecast NO passes.
    p = _params(forecast_dir_gate=False)
    assert _forecast_dir_ok(56.0, 55.0, "NO", p) == (True, "gate_off")


def test_code_default_gate_is_on(monkeypatch):
    # Safe-by-default: with NO yaml override the gate is ON (the HALT fix is the default).
    import lib.weather_paper as wp
    monkeypatch.setattr(wp, "_load_overrides", lambda: {})
    assert wp._effective_params()["forecast_dir_gate"] is True


def test_default_buffer_is_half_degree(monkeypatch):
    # The verified-optimal buffer (0.5°F) is the default; widening it loses winners.
    import lib.weather_paper as wp
    monkeypatch.setattr(wp, "_load_overrides", lambda: {})
    assert wp._effective_params()["forecast_buffer_f"] == 0.5
