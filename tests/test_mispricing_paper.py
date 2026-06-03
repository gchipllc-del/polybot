"""Unit tests for the mispricing-edge paper sleeve (lib/mispricing_paper.py).

Deterministic — no network. Verifies the measured-edge selection picks the
underpriced NO side, sizes by Kelly, and never marks a trade live.
"""
import importlib
import json

import lib.mispricing_paper as mp


def test_full_kelly_sign():
    # positive measured edge → positive Kelly; non-positive edge → 0 (clamped)
    assert mp._full_kelly(0.50, 0.20) > 0
    assert mp._full_kelly(0.20, 0.20) == 0.0      # p == fill → no edge
    assert mp._full_kelly(0.10, 0.50) == 0.0      # p < fill → clamped to 0


def test_selection_picks_underpriced_no(tmp_path, monkeypatch):
    # Gauge source: 16/20 wins at fill 0.10 → realized WR 0.80 ≫ price 0.10.
    gsrc = tmp_path / "gauge.jsonl"
    rows = ([{"fill_price": 0.10, "paper_pnl": 1.0}] * 16 +
            [{"fill_price": 0.10, "paper_pnl": -1.0}] * 4)
    gsrc.write_text("\n".join(json.dumps(r) for r in rows))
    out = tmp_path / "out.jsonl"

    monkeypatch.setenv("MISPRICING_GAUGE_SOURCE", str(gsrc))
    monkeypatch.setenv("WEATHER_PAPER_LOG", str(out))
    monkeypatch.setenv("MISPRICING_MIN_EDGE", "0.05")
    importlib.reload(mp)   # re-read env into module-level paths/params

    # One cheap-NO market; no network.
    monkeypatch.setattr(mp, "sample_signals", lambda: [{
        "market_ticker": "KXTEMPNYCH-TEST-T70", "no_ask": 0.10, "yes_ask": 0.90,
        "strike_f": 70.0, "city": "nyc",
        "close_time": "2026-06-02T23:00:00+00:00",
        "title": "test", "event_ticker": "evt",
        "nws_p_yes": 0.5, "market_p_yes": 0.9,
    }])

    new = mp.record_mispricing_trades()
    assert len(new) == 1
    t = new[0]
    assert t["side"] == "NO"
    assert t["edge"] > 0.05            # measured (shrunk) edge cleared the floor
    assert t["is_live"] is False       # PAPER-ONLY — never live
    assert t["entry_schema"] == "mispricing_v1"
    assert out.exists()                # wrote to the isolated ledger

    # Restore module defaults for other tests.
    importlib.reload(mp)


def test_high_fill_and_no_edge_are_skipped(tmp_path, monkeypatch):
    gsrc = tmp_path / "gauge.jsonl"
    # No history → gauge claims no edge → measured_edge == 0 → skipped.
    gsrc.write_text("")
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("MISPRICING_GAUGE_SOURCE", str(gsrc))
    monkeypatch.setenv("WEATHER_PAPER_LOG", str(out))
    importlib.reload(mp)
    monkeypatch.setattr(mp, "sample_signals", lambda: [{
        "market_ticker": "KXTEMPNYCH-TEST-T70", "no_ask": 0.10, "yes_ask": 0.90,
        "strike_f": 70.0, "city": "nyc", "close_time": "2026-06-02T23:00:00+00:00",
        "title": "t", "event_ticker": "e", "nws_p_yes": 0.5, "market_p_yes": 0.9,
    }])
    new = mp.record_mispricing_trades()
    assert new == []                   # no measured edge → no trade
    importlib.reload(mp)


def _gauge_src(tmp_path):
    g = tmp_path / "g.jsonl"
    g.write_text("\n".join(json.dumps({"fill_price": 0.10, "paper_pnl": 1.0}) for _ in range(16))
                 + "\n" + "\n".join(json.dumps({"fill_price": 0.10, "paper_pnl": -1.0}) for _ in range(4)))
    return g


_SAMPLE = [{
    "market_ticker": "KXTEMPNYCH-X-T70", "no_ask": 0.10, "yes_ask": 0.90,
    "strike_f": 70.0, "city": "nyc", "close_time": "2026-06-02T23:00:00+00:00",
    "title": "t", "event_ticker": "e", "nws_p_yes": 0.5, "market_p_yes": 0.9,
}]


def test_live_routes_through_executor_as_weather(tmp_path, monkeypatch):
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("MISPRICING_GAUGE_SOURCE", str(_gauge_src(tmp_path)))
    monkeypatch.setenv("WEATHER_PAPER_LOG", str(out))
    monkeypatch.setenv("MISPRICING_MIN_EDGE", "0.05")
    monkeypatch.setenv("MISPRICING_LIVE", "1")
    monkeypatch.setenv("MISPRICING_LIVE_BUDGET", "40")
    importlib.reload(mp)

    import lib.kalshi_live_executor as ex
    import lib.kalshi_client as kc
    monkeypatch.setattr(ex, "is_live_enabled", lambda: True)
    monkeypatch.setattr(ex, "_load_live_config", lambda: {})
    monkeypatch.setattr(ex, "effective_max_trade_usd", lambda cfg, available_balance=None: 20.0)
    calls = []
    def fake_place(**kw):
        calls.append(kw)
        ct, px = kw["contracts"], kw["fill_price"]
        return {"order_id": "ord1", "contracts": ct, "notional_usd": round(px * ct, 2),
                "filled_quantity": ct, "filled_notional_usd": round(px * ct, 2)}
    monkeypatch.setattr(ex, "place_live_order", fake_place)
    class FakeKC:
        def get_balance(self): return 200.0
    monkeypatch.setattr(kc, "KalshiClient", FakeKC)
    monkeypatch.setattr(mp, "sample_signals", lambda: list(_SAMPLE))

    new = mp.record_mispricing_trades()
    assert len(new) == 1 and new[0]["is_live"] is True
    assert new[0]["live_contracts"] >= 1
    assert len(calls) == 1
    assert calls[0]["metadata"]["asset"] == "weather"   # correct bucket + shared dedup
    assert calls[0]["side"] == "NO"
    importlib.reload(mp)


def test_live_self_cap_blocks_when_budget_used(tmp_path, monkeypatch):
    out = tmp_path / "out.jsonl"
    # An existing OPEN live trade already at the $40 cap.
    out.write_text(json.dumps({"market_ticker": "KXOLD", "status": "open",
                               "is_live": True, "live_notional_usd": 40.0}) + "\n")
    monkeypatch.setenv("MISPRICING_GAUGE_SOURCE", str(_gauge_src(tmp_path)))
    monkeypatch.setenv("WEATHER_PAPER_LOG", str(out))
    monkeypatch.setenv("MISPRICING_MIN_EDGE", "0.05")
    monkeypatch.setenv("MISPRICING_LIVE", "1")
    monkeypatch.setenv("MISPRICING_LIVE_BUDGET", "40")
    importlib.reload(mp)

    import lib.kalshi_live_executor as ex
    monkeypatch.setattr(ex, "is_live_enabled", lambda: True)
    monkeypatch.setattr(ex, "_load_live_config", lambda: {})
    placed = []
    monkeypatch.setattr(ex, "place_live_order", lambda **kw: placed.append(kw) or {})
    monkeypatch.setattr(mp, "sample_signals", lambda: list(_SAMPLE))

    new = mp.record_mispricing_trades()
    assert placed == []                       # self-cap blocked the live order
    assert new and new[0]["is_live"] is False  # fell through to paper
    importlib.reload(mp)


def test_live_off_by_default_is_paper(tmp_path, monkeypatch):
    out = tmp_path / "out.jsonl"
    monkeypatch.setenv("MISPRICING_GAUGE_SOURCE", str(_gauge_src(tmp_path)))
    monkeypatch.setenv("WEATHER_PAPER_LOG", str(out))
    monkeypatch.setenv("MISPRICING_MIN_EDGE", "0.05")
    monkeypatch.delenv("MISPRICING_LIVE", raising=False)   # not armed
    importlib.reload(mp)
    monkeypatch.setattr(mp, "sample_signals", lambda: list(_SAMPLE))
    new = mp.record_mispricing_trades()
    assert new and new[0]["is_live"] is False   # default = paper
    importlib.reload(mp)
