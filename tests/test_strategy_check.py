"""strategy_check - an outside method tested as ONE pre-specified hypothesis."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import strategy_check as sc  # noqa: E402


def _file(tmp_path, p_win, name="w"):
    p = tmp_path / f"{name}.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in sc._world(p_win):
            f.write(json.dumps(r) + "\n")
    return p


def _iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _minute_file(path, quote, n=40, minutes=120, result="yes", series="KXT"):
    """`final/`-style data: n daily events of one 20h market each, with 1-minute candles
    over only the last `minutes`. quote(seconds_left) -> (yes_bid, yes_ask) in cents."""
    t0 = 1_780_000_000
    with open(path, "w", encoding="utf-8") as f:
        for d in range(n):
            o = t0 + d * 86400
            c = o + 20 * 3600
            tk = f"{series}-D{d:04d}-B0"
            f.write(json.dumps({"t": "market", "series": series, "ticker": tk,
                                "event_ticker": f"{series}-D{d:04d}", "result": result,
                                "open_time": _iso(o), "close_time": _iso(c)}) + "\n")
            for k in range(minutes):
                end = c - 60 * minutes + 60 * (k + 1)
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
    a, b = _file(d, 0.995, "A"), _file(d, 0.995, "B")
    assert sc.expand_files(str(d)) == [a, b] == sc.expand_files(f"{a},{b}")
    one, both = sc.scan(a, 0.97, 0.99), sc.scan_many([a, b], 0.97, 0.99)
    # same synthetic tickers in both files: merged by ticker, nothing double counted
    assert len(both["markets"]) == len(one["markets"]) and both["orphans"] == 0


def test_hourly_candles_warn_before_a_last_hour_test(tmp_path, capsys):
    p = _file(tmp_path, 0.995)                       # candles 6 hours apart
    assert sc.cmd_favorites([p], 0.97, 0.99, 10, 0.0, 1.0) == 0
    assert "HOURLY candles" in capsys.readouterr().out
    assert sc.cmd_favorites([p], 0.97, 0.99, 10, 0.0, 1e9) == 0
    assert "HOURLY candles" not in capsys.readouterr().out    # no window: no warning


def test_no_entry_on_the_bar_ending_at_the_close(tmp_path):
    """The bar ending AT the close quotes the settled price, and the forward trader only
    buys while now < close. A market that shows the band only on that bar is never
    entered - in a last-hour window or with none - while one minute earlier still is."""
    at_close = _minute_file(tmp_path / "c.jsonl",
                            lambda left: (98, 99) if left <= 0 else (88, 90))
    for max_h in (1.0, 1e9):
        assert sc.scan(at_close, 0.97, 0.99, 0.0, max_h)["entries"] == {}
    a_minute_before = _minute_file(tmp_path / "m.jsonl",
                                   lambda left: (98, 99) if left <= 60 else (88, 90))
    e = sc.scan(a_minute_before, 0.97, 0.99, 0.0, 1.0)["entries"]
    assert len(e) == 40 and {x["hours"] for x in e.values()} == {1 / 60}
