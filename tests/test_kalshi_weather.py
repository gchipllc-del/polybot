"""Unit tests for the Kalshi weather sleeve — the pure forecast/pricing
core and the paper-trade entry logic. No network required.

The forecast blend + bucket-probability math is the whole edge of this
sleeve, so it's tested directly. Recording is tested against synthetic
samples so the filter chain and Kelly sizing are exercised offline.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from lib.weather_forecast import (
    HourlyForecast, blend_point_forecasts, bucket_probability,
    forecast_bucket_fair_value,
)


def test_bucket_probability_centered_band():
    # Forecast dead-center of a 4°-wide band should be the most likely.
    p_center = bucket_probability(mu=63.0, sigma=3.0, floor=62, cap=64)
    p_off = bucket_probability(mu=70.0, sigma=3.0, floor=62, cap=64)
    assert 0.0 < p_off < p_center < 1.0


def test_bucket_probability_one_sided():
    # "65 or above" with forecast far above should approach 1.
    hi = bucket_probability(mu=80.0, sigma=3.0, floor=65, cap=None)
    lo = bucket_probability(mu=50.0, sigma=3.0, floor=65, cap=None)
    assert hi > 0.99
    assert lo < 0.01
    # "62 or below" mirrors.
    below = bucket_probability(mu=50.0, sigma=3.0, floor=None, cap=62)
    assert below > 0.99


def test_bucket_probability_continuity_correction():
    # A single-degree bucket [63,63] => covers [62.5,63.5], width 1.
    p = bucket_probability(mu=63.0, sigma=3.0, floor=63, cap=63)
    # ~ pdf width 1 / sigma sqrt(2pi) ≈ 0.13 for sigma 3; just sanity-range it.
    assert 0.08 < p < 0.18


def test_bucket_probability_degenerate_sigma():
    assert bucket_probability(mu=63, sigma=0, floor=62, cap=64) == 1.0
    assert bucket_probability(mu=70, sigma=0, floor=62, cap=64) == 0.0


def test_blend_widens_sigma_on_disagreement():
    agree = blend_point_forecasts({"a": 63.0, "b": 63.0}, hours_out=1)
    disagree = blend_point_forecasts({"a": 60.0, "b": 66.0}, hours_out=1)
    assert agree is not None and disagree is not None
    assert disagree.mu == pytest.approx(63.0)
    # Disagreement must inflate sigma vs the agreeing case.
    assert disagree.sigma > agree.sigma


def test_blend_none_when_empty():
    assert blend_point_forecasts({}, hours_out=1) is None
    assert blend_point_forecasts({"a": None}, hours_out=1) is None


def test_blend_lead_time_grows_sigma():
    near = blend_point_forecasts({"a": 63.0}, hours_out=1)
    far = blend_point_forecasts({"a": 63.0}, hours_out=24)
    assert far.sigma > near.sigma


def test_forecast_bucket_fair_value_hourly_point():
    now = datetime.now(timezone.utc)
    target = now + timedelta(hours=2)
    # Two sources agreeing on 64° at the target hour.
    src = [
        HourlyForecast("nws", [(target, 64.0)]),
        HourlyForecast("open_meteo", [(target, 64.0)]),
    ]
    res = forecast_bucket_fair_value(
        src, target_time=target, floor=63, cap=65, daily_high=False)
    assert res is not None
    fair, blended = res
    assert blended.n_sources == 2
    assert 0.3 < fair < 0.9  # centered band, moderate sigma


def test_forecast_bucket_daily_high_uses_max():
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=5)
    pts = [(now + timedelta(hours=h), 50.0 + h) for h in range(6)]  # rises to 55
    src = [HourlyForecast("nws", pts)]
    res = forecast_bucket_fair_value(
        src, target_time=end, floor=54, cap=56, daily_high=True)
    assert res is not None
    fair, blended = res
    # Daily max is 55 (not the ~50 point reading), confirming max_through.
    assert blended.mu == pytest.approx(55.0)
    # Band straddles the max; with a multi-hour lead sigma it's the single
    # most likely few-degree band but still well under certainty.
    assert fair > 0.25
    # And a band far from the max must be much less likely.
    far, _ = forecast_bucket_fair_value(
        src, target_time=end, floor=44, cap=46, daily_high=True)
    assert far < fair


def test_record_paper_trade_picks_underpriced_side(tmp_path, monkeypatch):
    import lib.kalshi_weather_paper as wp
    monkeypatch.setattr(wp, "PAPER_PATH", tmp_path / "wx.jsonl")

    now = datetime.now(timezone.utc)
    close = (now + timedelta(hours=2)).isoformat()
    # Fair YES 0.80 but market only asks 0.55 → big YES edge → buy YES.
    samples = [{
        "city": "ny", "market_ticker": "KXHIGHNY-T1", "event_ticker": "E1",
        "title": "High temp 63-64", "seconds_to_close": 7200,
        "yes_ask": 0.55, "yes_bid": 0.53, "no_ask": 0.44,
        "fair_yes": 0.80, "forecast_mu": 64.0, "forecast_sigma": 3.0,
        "floor_strike": 63, "cap_strike": 64, "close_time": close,
    }]
    params = {"min_edge": 0.08, "max_spread": 0.06, "bankroll": 1000.0,
              "min_seconds_to_close": 120, "max_seconds_to_close": 6 * 3600}
    trades = wp.record_paper_trades_from_samples(samples, params=params)
    assert len(trades) == 1
    assert trades[0].side == "YES"
    assert trades[0].edge == pytest.approx(0.25, abs=1e-6)
    assert trades[0].our_size > 0


def test_record_paper_trade_skips_when_no_edge(tmp_path, monkeypatch):
    import lib.kalshi_weather_paper as wp
    monkeypatch.setattr(wp, "PAPER_PATH", tmp_path / "wx.jsonl")
    now = datetime.now(timezone.utc)
    close = (now + timedelta(hours=2)).isoformat()
    # Market fairly priced: fair 0.55, yes_ask 0.55, no_ask 0.45 → no edge.
    samples = [{
        "city": "ny", "market_ticker": "KXHIGHNY-T2", "event_ticker": "E1",
        "title": "High temp", "seconds_to_close": 7200,
        "yes_ask": 0.55, "yes_bid": 0.54, "no_ask": 0.45,
        "fair_yes": 0.55, "close_time": close,
        "floor_strike": 63, "cap_strike": 64,
    }]
    params = {"min_edge": 0.08, "bankroll": 1000.0,
              "min_seconds_to_close": 120, "max_seconds_to_close": 6 * 3600}
    trades = wp.record_paper_trades_from_samples(samples, params=params)
    assert trades == []


def test_settle_books_pnl(tmp_path, monkeypatch):
    import lib.kalshi_weather_paper as wp
    path = tmp_path / "wx.jsonl"
    monkeypatch.setattr(wp, "PAPER_PATH", path)
    row = {
        "trade_id": "T", "city": "ny", "market_ticker": "KXHIGHNY-T3",
        "event_ticker": "E1", "title": "x", "side": "YES", "fill_price": 0.5,
        "our_size": 10, "notional": 5.0, "fair_yes": 0.8, "edge": 0.3,
        "forecast_mu": 64, "forecast_sigma": 3, "floor_strike": 63,
        "cap_strike": 64, "close_time": "", "opened_at": "2026-06-01T00:00:00",
        "status": "open",
    }
    path.write_text(json.dumps(row) + "\n")

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"market": {"result": "yes"}}

    monkeypatch.setattr(wp, "_load_params", lambda: {})
    import types
    fake_requests = types.SimpleNamespace(get=lambda *a, **k: _Resp())
    monkeypatch.setitem(__import__("sys").modules, "requests", fake_requests)

    out = wp.settle_paper_trades()
    assert out["settled_now"] == 1
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    assert rows[0]["status"] == "won"
    # (1 - 0.5) * 10 * 0.93 = 4.65
    assert rows[0]["paper_pnl"] == pytest.approx(4.65, abs=1e-6)
