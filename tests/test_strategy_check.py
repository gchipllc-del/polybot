"""strategy_check - an outside method tested as ONE pre-specified hypothesis."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import strategy_check as sc  # noqa: E402


def _file(tmp_path, p_win, name="w", n_days=400, series="KXT"):
    p = tmp_path / f"{name}.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in sc._world(p_win, n_days=n_days):
            for k in ("series", "ticker", "event_ticker"):        # another city's file
                if k in r:
                    r[k] = r[k].replace("KXT", series)
            f.write(json.dumps(r) + "\n")
    return p


def _iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _quote_file(path, quote, n=40, span_h=2.0, step_s=60, result="yes", series="KXT",
                final_hours=None):
    """n daily events of one 20h market each, a candle every `step_s` seconds over only the
    last `span_h` hours - by default the `final/` shape (1-minute bars, last 2h); span_h=20,
    step_s=3600 is a full-life hourly file. quote(seconds_left) -> (yes_bid, yes_ask) in c.
    final_hours: the window the backfill records on each market row (None: not recorded)."""
    t0 = 1_780_000_000
    with open(path, "w", encoding="utf-8") as f:
        for d in range(n):
            o = t0 + d * 86400
            c = o + 20 * 3600
            tk = f"{series}-D{d:04d}-B0"
            row = {"t": "market", "series": series, "ticker": tk,
                   "event_ticker": f"{series}-D{d:04d}", "result": result,
                   "open_time": _iso(o), "close_time": _iso(c)}
            if final_hours is not None:
                row["final_hours"] = final_hours
            f.write(json.dumps(row) + "\n")
            for end in range(c - int(span_h * 3600) + step_s, c + 1, step_s):
                yb, ya = quote(c - end)
                f.write(json.dumps({"t": "candle", "series": series, "ticker": tk,
                                    "end_period_ts": end, "yes_bid": {"close": yb},
                                    "yes_ask": {"close": ya}}) + "\n")
    return path


def test_fee_is_per_order_and_rounds_up():
    assert sc.order_fee(0.98, 10) == 0.02          # 0.0137 -> 2c for the whole order
    assert sc.order_fee(0.98, 1) == 0.01           # one contract: half the 2c profit
    assert sc.order_fee(0.0, 10) == 0.0


def test_first_touch_and_event_independence(tmp_path):
    trades = sc.trades_from(sc.scan(_file(tmp_path, 0.995), 0.97, 0.99), 10)
    assert len(trades) == 1600 and {t["hours"] for t in trades} == {18.0}
    assert sc.summarize(trades, 10)["events"] == 400


def test_real_edge_reads_positive(tmp_path):
    s = sc.summarize(sc.trades_from(sc.scan(_file(tmp_path, 0.995), 0.97, 0.99), 10), 10)
    assert s["per_contract_event"] > 0 and sc.verdict(s).startswith("POSITIVE")


def test_clear_loser_reads_negative_and_significant(tmp_path):
    s = sc.summarize(sc.trades_from(sc.scan(_file(tmp_path, 0.93), 0.97, 0.99), 10), 10)
    assert sc.verdict(s).startswith("NEGATIVE AND SIGNIFICANT")


def test_quiet_loser_cannot_be_called_proven_either_way(tmp_path):
    """A 97-99c method losing ~1.7c/contract runs long win streaks and is not even
    provably negative in 400 days - the reason 'it hasn't lost yet' proves nothing."""
    s = sc.summarize(sc.trades_from(sc.scan(_file(tmp_path, 0.965), 0.97, 0.99), 10), 10)
    assert s["per_contract_event"] < 0 and s["best_streak"] > 100
    assert sc.verdict(s) == "NEGATIVE - no evidence it makes money after fees."


def test_hours_window_filters_entries(tmp_path):
    assert not sc.scan(_file(tmp_path, 0.995), 0.97, 0.99, 0.0, 4.0)["entries"]


# ── one bet per event, many files, the hourly-candle trap ─────────────────────

def _trade(i, won, ask=0.98, contracts=10):
    fee = sc.order_fee(ask, contracts)
    pnl = (contracts * (1 - ask) if won else -contracts * ask) - fee
    return {"ticker": f"T{i}", "series": "S", "event": f"E{i}", "side": "no", "ask": ask,
            "won": won, "fee": fee, "pnl": round(pnl, 4), "ts": 1_780_000_000 + i,
            "hours": 0.5, "trade_ok": True, "contracts": contracts}


def test_one_per_event_keeps_each_events_first_entry_and_is_exact(tmp_path):
    every = sc.trades_from(sc.scan(_file(tmp_path, 0.995), 0.97, 0.99), 10)
    first = sc.first_per_event(every)
    assert len(every) == 1600 and len(first) == 400              # 4 strikes -> 1 bet
    assert len({t["event"] for t in first}) == 400
    assert not sc.summarize(every, 10)["exact"] and sc.summarize(first, 10)["exact"]


def test_one_per_event_keeps_the_earliest_entry_not_a_later_one():
    """Strike B1 first touches 18h before close and loses; strike B2 touches 0.5h before
    close and wins. The forward trader's one bet on the event is B1, so the backtest must
    keep B1 whatever order the trades arrive in - keeping B2 is look-ahead toward wins."""
    t0 = 1_780_000_000
    b1 = dict(_trade(0, won=False), ticker="E-B1", event="E", ts=t0, hours=18.0)
    b2 = dict(_trade(1, won=True), ticker="E-B2", event="E", ts=t0 + 63000, hours=0.5)
    other = dict(_trade(2, won=True), ticker="F-B1", event="F", ts=t0 + 30000, hours=9.0)
    for trades in ([b1, b2, other], [b2, other, b1], [other, b2, b1]):
        assert sc.first_per_event(trades) == [b1, other]


def test_exact_tail_calls_three_losses_in_thirty_at_98c_negative():
    s = sc.summarize([_trade(i, won=i >= 3) for i in range(30)], 10)
    assert s["exact"] and s["p_neg"] < 0.025 and s["hi"] > 0     # interval alone: unsure
    assert sc.verdict(s).startswith("NEGATIVE AND SIGNIFICANT")


def test_a_perfect_record_at_98c_is_not_proof():
    s = sc.summarize([_trade(i, won=True) for i in range(100)], 10)
    assert abs(s["p"] - 0.982 ** 100) < 1e-9
    assert sc.verdict(s).startswith("POSITIVE BUT NOT PROVEN")


def test_folders_and_lists_of_files_merge(tmp_path):
    d = tmp_path / "final"
    d.mkdir()
    a = _file(d, 0.995, "A", n_days=10, series="KXA")             # two cities, 40 markets
    b = _file(d, 0.995, "B", n_days=10, series="KXB")             # and 40 entries each
    assert sc.expand_files(str(d)) == [a, b] == sc.expand_files(f"{a},{b}")
    both = sc.scan_many([a, b], 0.97, 0.99)
    # every file is read, and every market and entry of each one is kept
    assert len(both["markets"]) == len(both["entries"]) == 80 and both["orphans"] == 0
    assert {tk.split("-")[0] for tk in both["entries"]} == {"KXA", "KXB"}
    assert {t["series"] for t in sc.trades_from(both, 10)} == {"KXA", "KXB"}
    # the same tickers twice: merged by ticker, nothing double counted
    twice = sc.scan_many([a, a], 0.97, 0.99)
    assert len(twice["markets"]) == len(twice["entries"]) == 40


def test_a_ticker_in_two_files_keeps_its_earliest_touch(tmp_path):
    """`--file data/sleeves,data/sleeves/final`: the same tickers, hourly over the whole
    life and 1-minute over the last 2h. The merged entry must be each market's FIRST
    touch - the one the forward trader takes - whatever order the files are read in.
    Here YES sits at 97c 5h out (the real first touch, a loser) and NO at 98c 0.5h
    out (a winner): keeping the later file's entry turns every loss into a win."""
    hourly = _quote_file(tmp_path / "KXT.jsonl",
                         lambda left: (95, 97) if left == 5 * 3600 else (40, 45),
                         n=10, span_h=20, step_s=3600, result="no")
    (tmp_path / "final").mkdir()
    final = _quote_file(tmp_path / "final" / "KXT.jsonl",
                        lambda left: (2, 4) if left == 1800 else (40, 45), n=10, result="no")
    assert sc.expand_files(f"{tmp_path},{tmp_path / 'final'}") == [hourly, final]
    for files in ([hourly, final], [final, hourly]):
        res = sc.scan_many(files, 0.97, 0.99, 0.0, 12.0)
        assert len(res["entries"]) == 10
        assert {(e["hours"], e["side"], e["ask"]) for e in res["entries"].values()} == {
            (5.0, "yes", 0.97)}
        assert not any(t["won"] for t in sc.trades_from(res, 10))


def test_hourly_candles_warn_before_a_last_hour_test(tmp_path, capsys):
    p = _file(tmp_path, 0.995)                       # candles 6 hours apart
    assert sc.cmd_favorites([p], 0.97, 0.99, 10, 0.0, 1.0) == 0
    assert "HOURLY candles" in capsys.readouterr().out
    assert sc.cmd_favorites([p], 0.97, 0.99, 10, 0.0, 1e9) == 0
    assert "HOURLY candles" not in capsys.readouterr().out    # no window: no warning


def test_minute_candles_never_trigger_the_hourly_warning(tmp_path, capsys):
    """The recommended last-hour test - --max-hours 1 on 1-minute final/ candles - must
    not be told its data is hourly: that is exactly the data the warning asks for."""
    p = _quote_file(tmp_path / "m.jsonl", lambda left: (2, 4))   # 1 minute apart, last 2h
    assert sc.scan_many([p], 0.97, 0.99, 0.0, 1.0)["hourly_files"] == []
    assert sc.cmd_favorites([p], 0.97, 0.99, 10, 0.0, 1.0) == 0
    assert "HOURLY candles" not in capsys.readouterr().out


def test_hourly_warning_votes_per_file_and_names_the_hourly_files(tmp_path, capsys):
    """A folder mixing resolutions (a short-life series fetched at 1 minute next to hourly
    ones) must still warn: one dense minute file outvotes any number of hourly files
    when candle gaps are summed, so the vote is per FILE and the warning names them."""
    d = tmp_path / "mixed"
    d.mkdir()
    _file(d, 0.995, "A_hourly", n_days=20)                  # 80 markets, 6h apart
    _quote_file(d / "B_minute.jsonl", lambda left: (40, 45))  # 40 x 119 minute gaps
    files = sc.expand_files(str(d))
    assert sc.cmd_favorites(files, 0.97, 0.99, 10, 0.0, 1.0) == 0
    warning = [ln for ln in capsys.readouterr().out.splitlines() if "HOURLY candles" in ln]
    assert len(warning) == 1
    assert "A_hourly.jsonl" in warning[0] and "B_minute.jsonl" not in warning[0]


def test_no_entry_on_the_bar_ending_at_the_close(tmp_path):
    """The bar ending AT the close quotes the settled price, and the forward trader only
    buys while now < close. A market that shows the band only on that bar is never
    entered - in a last-hour window or with none - while one minute earlier still is."""
    at_close = _quote_file(tmp_path / "c.jsonl",
                           lambda left: (98, 99) if left <= 0 else (88, 90))
    for max_h in (1.0, 1e9):
        assert sc.scan(at_close, 0.97, 0.99, 0.0, max_h)["entries"] == {}
    a_minute_before = _quote_file(tmp_path / "m.jsonl",
                                  lambda left: (98, 99) if left <= 60 else (88, 90))
    e = sc.scan(a_minute_before, 0.97, 0.99, 0.0, 1.0)["entries"]
    assert len(e) == 40 and {x["hours"] for x in e.values()} == {1 / 60}


def test_data_that_starts_after_the_window_opens_is_flagged(tmp_path, capsys):
    """2h of final/ candles tested over 3h, or over the whole life, cannot see a first
    touch 2.5h out - the test would see only survivors - so it must say so. The backfill
    records each fetch's window (final_hours); older rows are judged off their first
    candle, but only inside the market's life: in candles alone, a book that formed late
    looks the same as a history cut short. A full-life twin of the tickers covers them."""
    def quiet(left):
        return (40, 45)
    rec = _quote_file(tmp_path / "rec.jsonl", quiet, n=5, final_hours=2)
    old = _quote_file(tmp_path / "old.jsonl", quiet, n=5)          # window not recorded
    twin = _quote_file(tmp_path / "twin.jsonl", quiet, n=5, span_h=20, step_s=3600)
    for files, max_h, late in (([rec], 2.0, False), ([rec], 3.0, True), ([rec], 1e9, True),
                               ([old], 2.0, False), ([old], 3.0, True), ([old], 1e9, False),
                               ([rec, twin], 3.0, False), ([rec, twin], 1e9, False),
                               ([twin], 1e9, False)):
        assert sc.cmd_favorites(files, 0.97, 0.99, 10, 0.0, max_h) == 0
        assert ("data starts AFTER" in capsys.readouterr().out) is late, (files, max_h)
    for p in (rec, old):
        res = sc.scan_many([p], 0.97, 0.99, 0.0, 3.0)
        assert set(sc.late_data(res, 3.0).values()) == {1.0}         # 1h of the 3 unseen


def test_orphan_candles_are_noted_when_there_are_any(tmp_path, capsys):
    """Candles with no market row before them (a backfill cut off mid-write) were counted
    by scan() and never mentioned. A note now says how many - a note, not an error:
    after the market is re-fetched the leftovers stay counted but do no harm."""
    p = _quote_file(tmp_path / "m.jsonl", lambda left: (40, 45), n=3)
    assert sc.cmd_favorites([p], 0.97, 0.99, 10, 0.0, 1e9) == 0
    assert "candle row(s) have no settled market row" not in capsys.readouterr().out
    lines = p.read_text(encoding="utf-8").splitlines()
    cut = [ln for ln in lines if '"market"' not in ln or "D0001" not in ln]   # lose a row
    p.write_text("\n".join(cut) + "\n", encoding="utf-8")
    assert sc.cmd_favorites([p], 0.97, 0.99, 10, 0.0, 1e9) == 0
    out = capsys.readouterr().out
    assert "note: 120 candle row(s) have no settled market row before them" in out
