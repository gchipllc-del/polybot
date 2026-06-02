"""Tests for the LIVE-only trend-aware veto (the cheap-NO gauge) in
lib/weather_paper.py. Audit/edge-work 2026-06-02.

The veto must:
  * pass a real cheap-NO trend-confirmed positive-cushion setup,
  * block YES, expensive fills, unconfirmed trends, and thin cushion,
  * default OFF (params flag) so live behavior is unchanged until enabled.
It is a PURE function over the sample's shadow_trendaware block.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.weather_paper import _live_trend_veto, _effective_params


def _sample(point_f, sigma_f, strike_f, trend_confirms=True):
    return {
        "strike_f": strike_f,
        "shadow_trendaware": {
            "point_f": point_f, "sigma_f": sigma_f,
            "trend_confirms": trend_confirms,
        },
    }


def test_default_flag_is_off():
    # No live behavior change unless explicitly enabled in yaml.
    assert _effective_params().get("weather_live_trend_veto") in (False, None) or \
        _effective_params()["weather_live_trend_veto"] is False


def test_passes_cheap_no_with_cushion():
    # NO bet, cheap fill, temp projected well BELOW strike (cushion > 0.75σ).
    # strike 60, projected 56, sigma 2 -> cushion = (60-56)/2 = 2.0σ
    s = _sample(point_f=56.0, sigma_f=2.0, strike_f=60.0, trend_confirms=True)
    ok, reason = _live_trend_veto(s, "NO", raw_fill_live=0.12)
    assert ok is True and reason == "trend_ok"


def test_blocks_yes_side():
    s = _sample(point_f=56.0, sigma_f=2.0, strike_f=60.0)
    ok, reason = _live_trend_veto(s, "YES", raw_fill_live=0.12)
    assert ok is False and reason == "veto_not_no_side"


def test_blocks_expensive_fill():
    s = _sample(point_f=56.0, sigma_f=2.0, strike_f=60.0)
    ok, reason = _live_trend_veto(s, "NO", raw_fill_live=0.30)  # > 0.15
    assert ok is False and reason == "veto_fill_too_high"


def test_blocks_unconfirmed_trend():
    s = _sample(point_f=56.0, sigma_f=2.0, strike_f=60.0, trend_confirms=False)
    ok, reason = _live_trend_veto(s, "NO", raw_fill_live=0.12)
    assert ok is False and reason == "veto_trend_not_confirmed"


def test_blocks_thin_cushion():
    # Temp projected just below strike: strike 56, projected 55.5, sigma 2 ->
    # cushion = 0.25σ < 0.75σ required. This is the near-money coin-flip trap.
    s = _sample(point_f=55.5, sigma_f=2.0, strike_f=56.0, trend_confirms=True)
    ok, reason = _live_trend_veto(s, "NO", raw_fill_live=0.12)
    assert ok is False and reason == "veto_thin_cushion"


def test_blocks_negative_cushion_trap():
    # The exact live-board trap: temp projected ABOVE the strike, so NO loses.
    # strike 53.9, projected 54.4 -> cushion negative.
    s = _sample(point_f=54.4, sigma_f=1.8, strike_f=53.9, trend_confirms=True)
    ok, reason = _live_trend_veto(s, "NO", raw_fill_live=0.03)
    assert ok is False and reason == "veto_thin_cushion"


def test_missing_trend_data_blocks():
    s = {"strike_f": 60.0, "shadow_trendaware": {}}
    ok, reason = _live_trend_veto(s, "NO", raw_fill_live=0.12)
    assert ok is False  # no trend_confirms -> blocked


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
