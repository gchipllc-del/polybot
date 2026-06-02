"""Tests for the #174 between-market bleed fix in the DAILY weather sleeve.

Root cause (verified, not guessed): the settled pre-fix sample's losers were all
NO bets that violently disagreed with the market — our day-ahead wide-σ model
said 2-9% YES on a narrow between-band while the live book said 44-95%, and the
book (with fresher, up-revised forecast info) was right. Two layers fix it:

  Layer A (lib/weather_daily_paper.py): a market-disagreement ceiling — skip when
    |our_p_yes - market_p_yes| exceeds max_disagreement_edge. Robust regardless
    of which internal path corrupted forecast_f.
  Layer B (lib/weather_daily_signal.py): when NWS and Open-Meteo disagree by
    >4°F, anchor the point to NWS canonical (Kalshi settles on the NWS station)
    instead of averaging toward a divergent OM reading.

PAPER-only sleeve; no live money. These guard the go-forward behavior.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Layer A: market-disagreement gate ─────────────────────────────────

def test_code_default_ceiling_is_040(monkeypatch):
    # With NO yaml override the ceiling must be the safe code default (0.40),
    # so behavior is well-defined even if the config file is missing.
    import lib.weather_daily_paper as wp
    monkeypatch.setattr(wp, "_load_overrides", lambda: {})
    assert wp._effective_params()["max_disagreement_edge"] == 0.40


def _no_sample(ticker, *, nws_p_yes, market_p_yes, no_ask=0.25):
    # A fully-valid NO setup: forecast sits deep in the NO zone (yes_margin very
    # negative passes the direction gate for any buffer), cheap fill passes the
    # price gates. The ONLY thing the two test samples vary is market_p_yes,
    # which moves |edge| across the disagreement ceiling.
    return {
        "market_ticker": ticker,
        "event_ticker": "KXHIGHTATL-26JUN01",
        "title": "Will the maximum temperature be 84-85?",
        "city_key": "atl_high", "direction": "max",
        "close_time": "2026-06-02T05:00:00Z",
        "strike_f": 84.5, "forecast_f": 86.0, "nws_forecast_f": 86.0,
        "yes_margin_f": -5.0,                 # deep in NO zone
        "nws_p_yes": nws_p_yes, "market_p_yes": market_p_yes,
        "yes_ask": market_p_yes, "no_ask": no_ask,
    }


def _run_one(monkeypatch, tmp_path, sample):
    """Record a single sample against an isolated paper log; return
    (new_trades, skip_counts)."""
    import lib.weather_daily_paper as wp
    monkeypatch.setattr(wp, "PAPER_LOG", tmp_path / "daily.jsonl")
    monkeypatch.setattr(wp, "_load_overrides", lambda: {})  # code defaults
    captured = {}

    def _cap(mod, ev, payload):
        if ev == "record_cycle":
            captured.update(payload.get("skip_counts", {}))
    monkeypatch.setattr(wp, "log_event", _cap)
    trades = wp.record_paper_trades_from_samples([sample])
    return trades, captured


def test_violent_disagreement_is_skipped(monkeypatch, tmp_path):
    # The exact ATL trap: we say 2% YES, market says 60% -> |edge| 0.58 > 0.40.
    s = _no_sample("KXHIGHTATL-26JUN01-B84.5", nws_p_yes=0.02, market_p_yes=0.60)
    trades, skips = _run_one(monkeypatch, tmp_path, s)
    assert trades == []
    assert skips.get("disagreement_too_large") == 1


def test_moderate_disagreement_trades(monkeypatch, tmp_path):
    # Identical setup, but market_p_yes=0.35 -> |edge| 0.15, inside [0.05, 0.40].
    # Proves the disagreement gate (not some other gate) is what blocked the trap.
    s = _no_sample("KXHIGHTATL-26JUN01-B84.5", nws_p_yes=0.20, market_p_yes=0.35)
    trades, skips = _run_one(monkeypatch, tmp_path, s)
    assert len(trades) == 1
    assert trades[0].side == "NO"
    assert "disagreement_too_large" not in skips


def test_gate_is_symmetric_for_yes(monkeypatch, tmp_path):
    # A wildly-overconfident YES (we say 95%, market says 30% -> |edge| 0.65)
    # must also be blocked — the ceiling is on disagreement magnitude, either
    # direction (even though YES is normally disabled, the gate precedes side).
    s = _no_sample("KXHIGHTATL-26JUN01-B90.5", nws_p_yes=0.95, market_p_yes=0.30)
    s["yes_margin_f"] = 5.0  # deep in YES zone so only the gate can block it
    trades, skips = _run_one(monkeypatch, tmp_path, s)
    assert trades == []
    assert skips.get("disagreement_too_large") == 1


# ── Layer B: robust skill-weighted multi-model ensemble ───────────────

def test_busted_model_is_rejected_not_averaged():
    # The exact #174 bug: GFS busts to 70 while NWS=86, ECMWF=85, ICON=83 and
    # the truth was 84-85. Old code averaged NWS+default(=GFS) -> 78 -> fake NO.
    # The robust ensemble drops the 70 outlier (median-based) and lands on the
    # 83-86 consensus, NOT dragged toward 78. obs (72) is below so can't raise.
    from lib.weather_daily_signal import _blended_daily_forecast
    point, sigma, meta = _blended_daily_forecast(
        city_key="atl_high", direction="max", nws_forecast_f=86.0,
        model_forecasts={"ecmwf_ifs025": 85.0, "icon_seamless": 83.0,
                         "gfs_seamless": 70.0},
        observed_extreme_f=72.0, obs_weight=0.4,
        lead_hours=11.0, seconds_to_close=11 * 3600)
    assert point >= 83.0                              # NOT dragged toward 78
    assert "gfs_seamless" in meta.get("ensemble_dropped", [])
    # σ reflects the KEPT consensus (~3°F spread), NOT inflated by the dropped
    # 16°F bust. A rogue model dropped from the point must not also blow up σ —
    # that would suppress an otherwise high-conviction 3-model consensus trade.
    assert meta.get("model_spread_f") <= 4.0
    assert sigma <= 4.0


def test_consensus_models_blend_ecmwf_leads():
    # Models agree (within 5°F) -> all survive; ECMWF (lowest MAE) carries the
    # most weight, pulling the point below the higher NWS toward the consensus.
    from lib.weather_daily_signal import _blended_daily_forecast
    point, sigma, meta = _blended_daily_forecast(
        city_key="atl_high", direction="max", nws_forecast_f=86.0,
        model_forecasts={"ecmwf_ifs025": 82.0, "icon_seamless": 83.0,
                         "gfs_seamless": 83.0},
        observed_extreme_f=70.0, obs_weight=0.4,
        lead_hours=11.0, seconds_to_close=11 * 3600)
    assert 82.0 <= point <= 84.5                      # ECMWF-led consensus < NWS 86
    assert meta.get("ensemble_dropped") is None       # nothing busted
    assert set(meta.get("ensemble_used", [])) >= {"nws", "ecmwf_ifs025"}


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
