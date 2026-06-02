"""Tests for the shared robust multi-model combiner (lib/forecaster_ensemble.py)
and its use in the LIVE hourly sleeve's _blended_forecast.

Context: 2026-06-02 — measured ECMWF ~2× more accurate than GFS (the old
Open-Meteo "default") at both daily extremes and NYC hourly. Both sleeves now
blend NWS + ECMWF/ICON/GFS via this combiner: median outlier-rejection (kills a
busted model) + inverse-MAE weighting (ECMWF leads). The hourly sleeve is REAL
money, so these guard that the ensemble upgrades only the FORECAST and the legacy
single-Open-Meteo path is preserved for callers that don't pass a model dict.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.forecaster_ensemble import median, skill_weighted_point


# ── shared combiner ───────────────────────────────────────────────────

MAE = {"nws": 0.82, "ecmwf_ifs025": 0.82, "gfs_seamless": 1.65, "icon_seamless": 1.78}


def test_median_odd_even():
    assert median([3, 1, 2]) == 2
    assert median([4, 1, 2, 3]) == 2.5


def test_busted_member_rejected():
    # GFS busts to 70 while everyone else is 84-86 -> dropped (>5°F from median).
    pt, kept = skill_weighted_point(
        MAE, {"nws": 86.0, "ecmwf_ifs025": 85.0, "icon_seamless": 84.0,
              "gfs_seamless": 70.0})
    assert "gfs_seamless" not in kept
    assert pt >= 84.0           # not dragged toward 78


def test_inverse_mae_weights_ecmwf_leads():
    # Two members only (no rejection at n=2): ECMWF (0.82) outweighs GFS (1.65),
    # so the point sits closer to ECMWF's 80 than GFS's 86.
    pt, kept = skill_weighted_point(MAE, {"ecmwf_ifs025": 80.0, "gfs_seamless": 86.0})
    assert set(kept) == {"ecmwf_ifs025", "gfs_seamless"}
    assert pt < 83.0            # weighted toward the more-accurate ECMWF


def test_fewer_than_three_no_rejection():
    # With 2 members a far-apart pair is NOT rejected (can't tell which busted) —
    # both weighted. Guards against silently dropping half a 2-source blend.
    pt, kept = skill_weighted_point(MAE, {"nws": 86.0, "gfs_seamless": 70.0})
    assert set(kept) == {"nws", "gfs_seamless"}


def test_empty_and_nonfinite():
    assert skill_weighted_point(MAE, {}) == (None, [])
    pt, kept = skill_weighted_point(MAE, {"nws": float("nan"), "ecmwf_ifs025": 80.0})
    assert kept == ["ecmwf_ifs025"] and pt == 80.0


def test_unknown_source_gets_default_weight():
    # An unnamed source still contributes (default MAE), never crashes.
    pt, kept = skill_weighted_point(MAE, {"ecmwf_ifs025": 80.0, "mystery": 82.0},
                                    default_mae=1.6)
    assert set(kept) == {"ecmwf_ifs025", "mystery"}
    assert 80.0 <= pt <= 82.0


# ── hourly sleeve integration (real-money path) ───────────────────────

def test_models_at_indexes_series_by_hour():
    # The once-per-city batched series is indexed per market by hour-prefix.
    from lib.weather_signal import _models_at
    series = {
        "ecmwf_ifs025": {"2026-06-02T17": 76.0, "2026-06-02T22": 79.0},
        "gfs_seamless": {"2026-06-02T17": 75.0},  # missing the 22:00 hour
    }
    assert _models_at(series, "2026-06-02T17:00:00Z") == {
        "ecmwf_ifs025": 76.0, "gfs_seamless": 75.0}
    # hour present only for ECMWF -> GFS drops out cleanly
    assert _models_at(series, "2026-06-02T22:30:00Z") == {"ecmwf_ifs025": 79.0}
    # hour absent entirely -> empty (blend falls back to NWS-only)
    assert _models_at(series, "2026-06-02T05:00:00Z") == {}
    assert _models_at({}, "2026-06-02T17:00:00Z") == {}


def test_hourly_blend_uses_ensemble_and_drops_bust():
    from lib.weather_signal import _blended_forecast
    pt, sig, meta = _blended_forecast(
        city="nyc", nws_forecast_f=86.0, open_meteo_f=85.0, current_obs_f=None,
        lead_hours=6.0,
        model_forecasts={"ecmwf_ifs025": 85.0, "icon_seamless": 84.0,
                         "gfs_seamless": 70.0})
    assert meta.get("ensemble_dropped") == ["gfs_seamless"]
    assert pt >= 84.0


def test_hourly_legacy_path_unchanged_without_models():
    # No model_forecasts -> the proven NWS+default 2-model path (NOT the
    # ensemble). model_disagreement_f present + ensemble_used absent proves the
    # legacy branch ran. Point ~midpoint of NWS/OM (plus any small learned
    # per-city calibration bias, which is correct, so we don't assert exact).
    from lib.weather_signal import _blended_forecast
    pt, sig, meta = _blended_forecast(
        city="nyc", nws_forecast_f=86.0, open_meteo_f=84.0, current_obs_f=None,
        lead_hours=6.0)
    assert "ensemble_used" not in meta          # ensemble branch did NOT run
    assert meta.get("model_disagreement_f") == 2.0   # legacy 2-model branch ran
    assert 84.5 <= pt <= 86.0                   # ~midpoint ± small bias


def test_dropped_bust_does_not_inflate_sigma_but_genuine_disagreement_does():
    # Two cases that must behave differently (the σ double-count fix):
    #  (a) clean bust dropped -> σ from the tight KEPT consensus (NOT inflated);
    #  (b) genuine 3-way disagreement, nothing rogue -> nothing dropped -> the
    #      full spread widens σ as real uncertainty.
    from lib.weather_signal import _blended_forecast
    # (a) GFS busts to 70; NWS/ECMWF/ICON agree 84-86 -> drop 70, spread ~2
    _, sig_bust, m_bust = _blended_forecast(
        city="nyc", nws_forecast_f=86.0, open_meteo_f=85.0, current_obs_f=None,
        lead_hours=6.0,
        model_forecasts={"ecmwf_ifs025": 85.0, "icon_seamless": 84.0,
                         "gfs_seamless": 70.0})
    # (b) genuine spread 73/76/79 (all within 5°F of median 76 -> none dropped)
    _, sig_dis, m_dis = _blended_forecast(
        city="nyc", nws_forecast_f=76.0, open_meteo_f=76.0, current_obs_f=None,
        lead_hours=6.0,
        model_forecasts={"ecmwf_ifs025": 73.0, "icon_seamless": 76.0,
                         "gfs_seamless": 79.0})
    assert m_bust.get("ensemble_dropped") == ["gfs_seamless"]
    assert m_bust.get("model_spread_f") <= 3.0      # kept consensus (~2°F), NOT 16
    assert m_dis.get("ensemble_dropped") is None    # nothing rogue
    assert m_dis.get("model_spread_f") >= 5.0       # full spread preserved
    assert sig_dis > sig_bust                       # genuine fight => wider σ


def test_hourly_obs_anchor_untouched_by_ensemble():
    # The cheap-NO edge lives in the current-obs anchor (#161). With a strong
    # obs at short lead, the obs blend weight still applies on TOP of the
    # ensemble forecast — i.e. the ensemble only changed the forecast component.
    from lib.weather_signal import _blended_forecast
    pt, sig, meta = _blended_forecast(
        city="nyc", nws_forecast_f=86.0, open_meteo_f=85.0, current_obs_f=70.0,
        lead_hours=1.0,
        model_forecasts={"ecmwf_ifs025": 85.0, "icon_seamless": 84.0})
    assert "obs_blend_weight" in meta   # obs anchor still engaged
    assert pt < 85.0                     # pulled toward the 70° obs at short lead


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
