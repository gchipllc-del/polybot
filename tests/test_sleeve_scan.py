"""sleeve_scan - the Kalshi-wide edge search must not fool itself.

Pinned: event independence, life-fraction banding, the false-discovery control across
the WHOLE search, the phantom-fill flag, and the 'it never lost' trap the measured
detection table exposed (zero-variance samples read as p = 0 'proof').
"""
import json
import sys
from pathlib import Path

import pytest

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


def test_final_hours_resume_refetches_a_shorter_stored_window(tmp_path, monkeypatch, capsys):
    """--final-hours 1 then --final-hours 2 must not keep the 1h window: a ticker already on
    file is not 'done' unless its recorded window covers the one asked for. A row written
    before windows were recorded is unknown, so it is fetched again too; a market whose
    window already reached its open has nothing older to fetch."""
    import history_backfill as hb
    from datetime import datetime, timezone
    monkeypatch.setattr(ss, "SLEEVE_DIR", tmp_path)
    monkeypatch.setattr(ss, "CATALOG", tmp_path / "catalog.json")
    monkeypatch.setattr(hb, "SLEEP_S", 0)
    long_, short_ = "KXT-26NOV16H19-T45", "KXT-26NOV16H19-T50"      # 14h and 1h of life
    mk = [{"ticker": long_, "event_ticker": "KXT-26NOV16H19", "result": "no",
           "open_time": "2026-11-16T10:00:00Z", "close_time": "2026-11-17T00:00:00Z"},
          {"ticker": short_, "event_ticker": "KXT-26NOV16H19", "result": "yes",
           "open_time": "2026-11-16T23:00:00Z", "close_time": "2026-11-17T00:00:00Z"}]
    calls = []

    def get(url, params=None):
        if url.endswith("/markets"):
            return {"markets": mk, "cursor": None}
        calls.append((url.split("/")[-2], params["start_ts"]))
        return {"candlesticks": [{"end_period_ts": params["start_ts"] + 60,
                                  "yes_bid": {"close": 40}, "yes_ask": {"close": 45}}]}

    out = tmp_path / "final" / "KXT.jsonl"
    out.parent.mkdir()
    out.write_text(json.dumps({"t": "market", "series": "KXT", "ticker": long_,
                               "result": "no", "open_time": mk[0]["open_time"],
                               "close_time": mk[0]["close_time"]}) + "\n",
                   encoding="utf-8")                  # legacy row: window not recorded
    close = int(datetime(2026, 11, 17, tzinfo=timezone.utc).timestamp())
    for hours, fetched in ((1, [(long_, close - 3600), (short_, close - 3600)]),
                           (2, [(long_, close - 7200)]), (2, []), (1, [])):
        del calls[:]
        assert ss.cmd_backfill(["KXT"], 5, get=get, final_hours=hours) == 0
        assert calls == fetched, hours
    assert "1 stored with a window under 2h" in capsys.readouterr().out
    rows = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert [(r["ticker"], r.get("final_hours")) for r in rows if r["t"] == "market"] == [
        (long_, None), (long_, 1), (short_, 1), (long_, 2)]


def test_candle_volume_reads_renamed_field():
    from sleeve_scan import candle_volume
    assert candle_volume({"volume": 5}) == 5
    assert candle_volume({"volume_fp": "12.00"}) == 12.0
    assert candle_volume({"volume_fp": "0.00"}) == 0.0
    assert candle_volume({}) == 0.0


def test_find_searches_ticker_title_and_category_any_case(tmp_path, monkeypatch, capsys):
    import json as _json
    cat = tmp_path / "catalog.json"
    cat.write_text(_json.dumps([
        {"series": "KXTEMPNYCH", "category": "Climate and Weather", "events": 40,
         "title": "Temperature in New York City on Nov 15 at 7pm EST?"},
        {"series": "KXHIGHNY", "category": "Climate and Weather", "events": 9,
         "title": "Highest temperature in NYC today?"},
        {"series": "KXBTC15M", "category": "Crypto", "events": 54, "title": "BTC 15 min"},
        {"series": "KXESOCCERGAME", "category": "Sports", "events": 299,
         "title": "eSoccer: Stuttgart vs Mönchengladbach"}], indent=1), encoding="utf-8")
    monkeypatch.setattr(ss, "CATALOG", cat)
    assert ss.cmd_find("TEMPERATURE") == 0
    out = capsys.readouterr().out
    assert "2 series match" in out and "KXTEMPNYCH" in out and "KXHIGHNY" in out
    assert "KXBTC15M" not in out
    assert ss.cmd_find("mönchen") == 0 and "KXESOCCERGAME" in capsys.readouterr().out
    # in no title: "kxhigh" only in a ticker, "weather" and "crypto" only in categories
    assert ss.cmd_find("kxhigh") == 0
    out = capsys.readouterr().out
    assert "1 series match" in out and "KXHIGHNY" in out
    assert ss.cmd_find("WEATHER") == 0
    out = capsys.readouterr().out
    assert "2 series match" in out and "KXTEMPNYCH" in out and "KXHIGHNY" in out
    assert ss.cmd_find("crypto") == 0 and "1 series match" in capsys.readouterr().out
    monkeypatch.setattr(ss, "CATALOG", tmp_path / "missing.json")
    assert ss.cmd_find("x") == 1


# ── backfill: nothing skipped quietly ─────────────────────────────────────────

class _Resp:
    def __init__(self, status, reason=""):
        self.status_code, self.reason, self.headers = status, reason, {}


def _refused(status=429):
    import requests
    return requests.HTTPError(f"{status} Error", response=_Resp(
        status, "Too Many Requests" if status == 429 else ""))


def _backfill_api(series_events: dict, fail: dict | None = None):
    """get() for cmd_backfill. series_events: series -> {event: n_strikes}. `fail` maps a
    series (its listing) or a ticker (its candles) to the exception raised for it."""
    fail = fail or {}
    log: list[str] = []

    def get(url, params=None):
        if url.endswith("/markets"):
            s = params["series_ticker"]
            log.append(f"list {s}")
            if s in fail:
                raise fail[s]
            ms = [{"ticker": f"{ev}-T{k}", "event_ticker": ev, "result": "no",
                   "open_time": "2026-11-16T10:00:00Z",
                   "close_time": f"2026-11-{17 + i:02d}T00:00:00Z"}
                  for i, (ev, n) in enumerate(series_events[s].items()) for k in range(n)]
            return {"markets": ms, "cursor": None}
        tk = url.split("/")[-2]
        log.append(tk)
        if tk in fail:
            raise fail[tk]
        return {"candlesticks": [{"end_period_ts": params["start_ts"] + 60,
                                  "yes_bid": {"close": 40}, "yes_ask": {"close": 45}}]}

    return get, log


@pytest.fixture
def bf(tmp_path, monkeypatch):
    """sleeve_scan pointed at tmp_path, every pause recorded instead of slept."""
    import time
    import history_backfill as hb
    monkeypatch.setattr(ss, "SLEEVE_DIR", tmp_path)
    monkeypatch.setattr(ss, "CATALOG", tmp_path / "catalog.json")
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    return hb, waits


def test_backfill_counts_paces_and_reports_a_market_it_could_not_fetch(bf, tmp_path, capsys):
    """One strike missing from an event can turn its first touch from a loss into a win.
    It used to print '[warn] <ticker>: HTTPError' (no status), skip the pause, and the
    run returned 0. Now: status named, paced, counted per series and event, exit 1."""
    hb, waits = bf
    get, log = _backfill_api({"KXA": {"KXA-E1": 3, "KXA-E2": 2}}, fail={"KXA-E2-T1": _refused()})
    assert ss.cmd_backfill(["KXA"], 10, get=get) == 1
    out = capsys.readouterr().out
    assert "[warn] KXA-E2-T1: HTTP 429 Too Many Requests - not fetched" in out
    assert len(waits) == 5                    # every candle request paced, failed or not
    assert "4 fetched, 1 NOT fetched in 1 event(s)" in out and "NOT COMPLETE" in out
    get2, log2 = _backfill_api({"KXA": {"KXA-E1": 3, "KXA-E2": 2}})
    assert ss.cmd_backfill(["KXA"], 10, get=get2) == 0    # the re-run fetches the one
    assert log2 == ["list KXA", "KXA-E2-T1"] and "COMPLETE:" in capsys.readouterr().out


def test_backfill_survives_a_failed_listing(bf, capsys):
    """A 429 on the third series' listing used to end a 13-series run in a traceback."""
    hb, _ = bf
    get, log = _backfill_api({"KXA": {"KXA-E1": 1}, "KXB": {"KXB-E1": 1},
                              "KXC": {"KXC-E1": 1}}, fail={"KXB": _refused()})
    assert ss.cmd_backfill(["KXA", "KXB", "KXC"], 10, get=get) == 1
    assert log == ["list KXA", "KXA-E1-T0", "list KXB", "list KXC", "KXC-E1-T0"]
    out = capsys.readouterr().out
    assert "could not list its settled markets (HTTP 429 Too Many Requests)" in out


def test_backfill_stops_when_kalshi_stops_answering(bf, capsys):
    hb, _ = bf
    tks = [f"KXA-E1-T{k}" for k in range(8)]
    get, log = _backfill_api({"KXA": {"KXA-E1": 8}, "KXB": {"KXB-E1": 1}},
                             fail={tk: ConnectionResetError("reset") for tk in tks[1:]})
    assert ss.cmd_backfill(["KXA", "KXB"], 10, get=get) == 1
    assert log == ["list KXA"] + tks[:1 + hb.GIVE_UP_AFTER]
    out = capsys.readouterr().out
    assert "[stop]" in out and "1 fetched, 7 NOT fetched in 1 event(s)" in out
    assert "NOT started" in out


def test_backfill_reports_markets_left_out_by_the_per_event_cap(bf, capsys):
    """The 15-per-event cap is the frozen screen design and stays; it used to drop the
    16th strike without a word. Counted now, and not a failure."""
    hb, _ = bf
    get, log = _backfill_api({"KXA": {"KXA-E1": 16, "KXA-E2": 22, "KXA-E3": 4}})
    assert ss.cmd_backfill(["KXA"], 10, get=get) == 0
    assert len(log) == 1 + 15 + 15 + 4
    out = capsys.readouterr().out
    assert "8 left out by the 15-per-event cap in 2 event(s)" in out and "COMPLETE:" in out
