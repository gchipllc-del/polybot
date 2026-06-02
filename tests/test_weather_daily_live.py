"""Tests for the DAILY-weather live execution path (#160).

The daily sleeve routes real orders through kalshi_live_executor (all rails:
allowlist, balance floor, daily-loss halt, 5-loss kill switch, per-asset budget,
concurrent cap, 24h dedup). These guard the money-path wiring:
  * KXHIGHT*/KXLOWT* map to the dedicated 'weather_daily' budget bucket (the
    KXLOWT* no-budget gap is closed);
  * the entry branch records is_live + ACTUAL filled qty when an order places,
    and stays paper (is_live=False) when the executor refuses;
  * settlement uses live_contracts/live_notional for live rows, VOIDS a 0-fill
    live order, and feeds record_outcome (the kill switch).
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_ticker_maps_to_weather_daily_bucket():
    from lib.kalshi_live_executor import _ticker_to_asset
    assert _ticker_to_asset("KXHIGHTATL-26JUN01-B84.5") == "weather_daily"
    assert _ticker_to_asset("KXLOWTCHI-26JUN01-B55.5") == "weather_daily"  # was None
    # hourly + btc unaffected
    assert _ticker_to_asset("KXTEMPNYCH-26JUN0211-T56") == "weather"
    assert _ticker_to_asset("KXBTCD-26JUN01-T100000") == "btc"


def _no_sample(ticker="KXHIGHTATL-26JUN01-B84.5"):
    # Fully-valid NO setup that clears every entry gate under code defaults.
    return {
        "market_ticker": ticker, "event_ticker": "KXHIGHTATL-26JUN01",
        "title": "max temp 84-85?", "city_key": "atl_high", "direction": "max",
        "close_time": "2035-06-02T05:00:00Z",   # far future → passes close-time guard
        "strike_f": 84.5, "forecast_f": 78.0, "nws_forecast_f": 78.0,
        "yes_margin_f": -5.0,                 # deep in NO zone
        "nws_p_yes": 0.20, "market_p_yes": 0.32,  # edge -0.12 (in [0.10,0.40])
        "yes_ask": 0.32, "no_ask": 0.30,
    }


def _patch_common(monkeypatch, tmp_path):
    import lib.weather_daily_paper as wp
    import lib.kalshi_live_executor as ex
    monkeypatch.setattr(wp, "PAPER_LOG", tmp_path / "daily.jsonl")
    monkeypatch.setattr(wp, "_load_overrides", lambda: {})       # code defaults
    monkeypatch.setattr(ex, "_load_live_config", lambda: {"max_trade_bankroll_pct": 0.0})
    monkeypatch.setattr(ex, "effective_max_trade_usd", lambda cfg, **kw: 5.0)

    class _FakeKC:
        def get_balance(self):
            return 246.0
    monkeypatch.setattr("lib.kalshi_client.KalshiClient", _FakeKC)
    return wp, ex


def test_entry_places_live_and_records_actual_fill(monkeypatch, tmp_path):
    wp, ex = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(ex, "is_live_enabled", lambda: True)
    placed = {}

    def _fake_place(**kw):
        placed.update(kw)
        n = kw["contracts"]
        return {"order_id": "OID123", "contracts": n,
                "notional_usd": round(kw["fill_price"] * n, 4),
                "filled_quantity": n,
                "filled_notional_usd": round(kw["fill_price"] * n, 4)}
    monkeypatch.setattr(ex, "place_live_order", _fake_place)

    trades = wp.record_paper_trades_from_samples([_no_sample()])
    assert len(trades) == 1
    t = trades[0]
    assert t.is_live is True and t.live_order_id == "OID123"
    assert t.live_contracts >= 1 and t.live_notional_usd > 0
    # the executor was told the right asset + a capped (<= $5) size
    assert placed["metadata"]["asset"] == "weather_daily"
    assert placed["fill_price"] * placed["contracts"] <= 5.0 + 1e-9
    assert placed["side"] == "NO"


def test_closed_market_is_skipped(monkeypatch, tmp_path):
    # #160 follow-up: the first live order FAILED 404 on a market that had
    # closed 11h earlier. A sample whose close_time is in the past must be
    # skipped entirely (no paper row, no live attempt).
    wp, ex = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(ex, "is_live_enabled", lambda: True)
    def _boom(**kw):
        raise AssertionError("must NOT attempt an order on a closed market")
    monkeypatch.setattr(ex, "place_live_order", _boom)
    s = _no_sample()
    s["close_time"] = "2020-01-01T05:00:00Z"   # long past → closed
    trades = wp.record_paper_trades_from_samples([s])
    assert trades == []


def test_entry_refused_records_paper_only(monkeypatch, tmp_path):
    # Executor refuses (e.g. budget/allowlist) -> None -> trade stays paper.
    wp, ex = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(ex, "is_live_enabled", lambda: True)
    monkeypatch.setattr(ex, "place_live_order", lambda **kw: None)
    trades = wp.record_paper_trades_from_samples([_no_sample()])
    assert len(trades) == 1 and trades[0].is_live is False


def test_entry_noop_when_live_disabled(monkeypatch, tmp_path):
    wp, ex = _patch_common(monkeypatch, tmp_path)
    monkeypatch.setattr(ex, "is_live_enabled", lambda: False)
    def _boom(**kw):
        raise AssertionError("place_live_order must NOT be called when live disabled")
    monkeypatch.setattr(ex, "place_live_order", _boom)
    trades = wp.record_paper_trades_from_samples([_no_sample()])
    assert len(trades) == 1 and trades[0].is_live is False


def _write(p, rows):
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_settle_uses_live_size_voids_nofill_and_records_outcome(monkeypatch, tmp_path):
    import lib.weather_daily_paper as wp
    monkeypatch.setattr(wp, "PAPER_LOG", tmp_path / "daily.jsonl")
    monkeypatch.setattr(wp, "_kalshi_market_result", lambda t: "no")  # NO bets win
    monkeypatch.setattr(wp, "_record_daily_calibration", lambda rec, cache: None)
    outcomes = []
    import lib.kalshi_live_executor as ex
    monkeypatch.setattr(ex, "record_outcome",
                        lambda **kw: outcomes.append(kw))

    past = "2026-06-01T05:00:00Z"
    base = dict(side="NO", fill_price=0.30, our_size=10, notional=3.0,
                status="open", close_time=past, opened_at="2026-05-31T18:00:00Z",
                market_ticker="KXHIGHTATL-26JUN01-B84.5")
    rows = [
        # live winner: 12 contracts filled -> P&L on 12, NOT the paper our_size=10
        {**base, "is_live": True, "live_contracts": 12, "live_notional_usd": 3.6,
         "market_ticker": "KXHIGHTATL-26JUN01-B84.5"},
        # live 0-fill -> VOID (not a loss)
        {**base, "is_live": True, "live_contracts": 0, "live_notional_usd": 0.0,
         "market_ticker": "KXLOWTCHI-26JUN01-B55.5"},
        # paper trade -> settles on our_size, no record_outcome
        {**base, "market_ticker": "KXHIGHTSEA-26JUN01-B77.5"},
    ]
    _write(wp.PAPER_LOG, rows)
    monkeypatch.setattr(wp, "datetime", __import__("datetime").datetime)  # ensure real now
    out = wp.settle_paper_trades()

    recs = [json.loads(l) for l in wp.PAPER_LOG.read_text().splitlines() if l.strip()]
    by_tk = {r["market_ticker"]: r for r in recs}
    live_win = by_tk["KXHIGHTATL-26JUN01-B84.5"]
    nofill = by_tk["KXLOWTCHI-26JUN01-B55.5"]
    paper = by_tk["KXHIGHTSEA-26JUN01-B77.5"]
    # live winner P&L computed on 12 contracts: 12*(1-0.30)*(1-fee 0.07)=7.812
    assert live_win["status"] == "won"
    assert abs(live_win["paper_pnl"] - round(12 * 0.70 * 0.93, 4)) < 0.02
    # 0-fill live order voided, not lost
    assert nofill["status"] == "void" and nofill["paper_pnl"] == 0.0
    # paper trade settled on our_size=10
    assert paper["status"] == "won"
    assert abs(paper["paper_pnl"] - round(10 * 0.70 * 0.93, 4)) < 0.02
    # record_outcome fed ONLY for the live winner (not void, not paper)
    assert len(outcomes) == 1 and outcomes[0]["pnl"] > 0


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
