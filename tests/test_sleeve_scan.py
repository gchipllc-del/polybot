"""sleeve_scan - the Kalshi-wide edge search must not fool itself.

Pinned: event independence, life-fraction banding, the false-discovery control across
the WHOLE search, the phantom-fill flag, and the 'it never lost' trap the measured
detection table exposed (zero-variance samples read as p = 0 'proof').
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import sleeve_scan as ss  # noqa: E402


def _entries(tmp_path, specs):
    out = []
    for name, mode, n, seed, bias in specs:
        p = tmp_path / f"{name}.jsonl"
        ss._write(ss.make_sleeve(name, mode, n_events=n, seed=seed, bias=bias), p)
        for series, sl in ss.load_sleeve_file(p).items():
            out.extend(ss.entries_for(sl, series))
    return out


def test_buckets_bands_and_intervals():
    assert ss.bucket_of(0.01) == "01-05c" and ss.bucket_of(0.989) == "95-99c"
    assert ss.bucket_of(0.995) is None and ss.bucket_of(0.005) is None
    assert ss.band_of(0.0) == "early" and ss.band_of(0.5) == "mid"
    assert ss.band_of(0.9) == "late" and ss.band_of(1.0) == "final"
    assert ss.candle_interval(0, 2 * 3600) == 1
    assert ss.candle_interval(0, 10 * 86400) == 60
    assert ss.candle_interval(0, 120 * 86400) == 1440


def test_event_key_from_field_or_ticker():
    assert ss.event_of({"event_ticker": "EV-1", "ticker": "X-Y-Z"}) == "EV-1"
    assert ss.event_of({"ticker": "KXHIGHNY-26NOV16-B57.5"}) == "KXHIGHNY-26NOV16"


def test_candle_shapes_cents_dollars_nested_flat():
    assert ss._ohlc({"yes_ask": {"close": 42}}, "yes_ask", "close") == 0.42
    assert ss._ohlc({"yes_ask_dollars": {"close_dollars": "0.4200"}}, "yes_ask", "close") == 0.42
    assert ss._ohlc({"yes_ask": 0.42}, "yes_ask", "close") == 0.42
    assert ss._ohlc({"price": {"low": 7}}, "price", "low") == 0.07
    assert ss._ohlc({}, "price", "low") is None


def test_unformed_books_are_dropped(tmp_path):
    rows = [{"t": "market", "series": "S", "ticker": "S-E1-A", "result": "yes",
             "open_time": "2026-11-01T00:00:00+00:00", "close_time": "2026-11-01T06:00:00+00:00"},
            {"t": "candle", "series": "S", "ticker": "S-E1-A", "end_period_ts": 1793500000,
             "yes_bid": {"close": 1}, "yes_ask": {"close": 99}}]          # 1c/99c shell
    p = tmp_path / "s.jsonl"
    ss._write(rows, p)
    assert ss.load_sleeve_file(p)["S"]["candles"] == {}


def test_bh_controls_false_discoveries():
    assert ss.bh_select([0.0001] + [0.5] * 99) == {0}
    assert ss.bh_select([0.3, 0.6, 0.9, 0.5]) == set()
    assert ss.bh_select([0.001, 0.002, 0.003, 0.9]) == {0, 1, 2}


def test_never_lost_is_not_proof():
    """40 straight wins on a 94c favorite: every outcome +5c, sample variance 0. A naive
    test says p = 0. The null-variance floor says: not even close."""
    rows = [{"event": f"E{i}", "ask": 0.94, "day": "d"} for i in range(40)]
    assert ss.p_value(0.05, 0.0, rows) > 0.05


def test_independence_one_observation_per_event(tmp_path):
    res = ss.screen(_entries(tmp_path, [("A", "biased", 400, 3, 0.12)]))
    for c in res["cells"]:
        assert c["n_in"] + c["n_out"] <= 400


def test_pure_noise_search_nominates_nothing(tmp_path):
    entries = _entries(tmp_path, [(f"E{i}", "efficient", 400, 10 + i, 0.0) for i in range(6)])
    res = ss.screen(entries)
    assert res["family"] > 100 and res["nominated"] == []


def test_planted_bias_found_on_the_right_side_and_phantom_flagged(tmp_path):
    entries = _entries(tmp_path, [("EFF", "efficient", 400, 2, 0.0),
                                  ("BIAS", "biased", 400, 3, 0.12),
                                  ("PHAN", "phantom", 400, 4, 0.12)])
    noms = ss.screen(entries)["nominated"]
    clean = {c["series"] for c in noms if c["verdict"] == "NOMINATED"}
    flagged = {c["series"] for c in noms if c["verdict"] != "NOMINATED"}
    assert "BIAS" in clean and "EFF" not in clean | flagged
    assert all(c["side"] == "no" for c in noms if c["series"] == "BIAS")
    assert "PHAN" in flagged and "PHAN" not in clean


def test_small_edge_on_small_sample_is_not_nominated(tmp_path):
    """The honest limit, pinned: a ~1c edge on 160 events cannot be told from noise."""
    res = ss.screen(_entries(tmp_path, [("TINY", "biased", 160, 5, 0.03)]))
    assert not [c for c in res["nominated"] if c["verdict"] == "NOMINATED"]


def test_calibration_summary_excludes_final_band(tmp_path):
    entries = _entries(tmp_path, [("A", "efficient", 80, 1, 0.0)])
    cal = ss.calibration_by_category(entries)
    total = sum(v[0] for buckets in cal.values() for v in buckets.values())
    assert total == sum(1 for e in entries if e["band"] != "final")


def test_final_hours_backfill_fetches_minute_candles_for_the_last_hours(tmp_path, monkeypatch):
    """A last-hour entry cannot be tested on hourly candles (the final bar's close IS the
    settled price), so --final-hours asks for 1-minute candles over the last H hours."""
    import history_backfill as hb
    monkeypatch.setattr(ss, "SLEEVE_DIR", tmp_path)
    monkeypatch.setattr(ss, "CATALOG", tmp_path / "catalog.json")
    monkeypatch.setattr(hb, "SLEEP_S", 0)
    calls = []

    def get(url, params=None):
        if url.endswith("/markets"):
            return {"markets": [{"ticker": "KXT-26NOV16H19-T45", "event_ticker": "KXT-26NOV16H19",
                                 "result": "no", "open_time": "2026-11-16T10:00:00Z",
                                 "close_time": "2026-11-17T00:00:00Z"}], "cursor": None}
        calls.append(params)
        return {"candlesticks": [{"end_period_ts": 1, "yes_bid": {"close": 1},
                                  "yes_ask": {"close": 2}}]}

    assert ss.cmd_backfill(["KXT"], 5, get=get, final_hours=2) == 0
    from datetime import datetime, timezone
    close = int(datetime(2026, 11, 17, tzinfo=timezone.utc).timestamp())
    assert calls == [{"period_interval": 1, "start_ts": close - 7200, "end_ts": close}]
    out = tmp_path / "final" / "KXT.jsonl"
    assert out.exists() and not (tmp_path / "KXT.jsonl").exists()   # kept out of screen
