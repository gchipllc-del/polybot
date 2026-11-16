"""strategy_check - an outside method tested as ONE pre-specified hypothesis."""
import json
import sys
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
