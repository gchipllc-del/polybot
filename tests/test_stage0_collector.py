"""stage0_collector - one damaged line, one crash or one second copy must not stop or
corrupt collection. What is collected is frozen; these pin only how it survives faults."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import stage0_collector as s0  # noqa: E402

T0 = datetime(2026, 11, 25, 12, 0, 20, tzinfo=timezone.utc)
CLOSE = datetime(2026, 11, 25, 12, 15, tzinfo=timezone.utc)


def exchange(yes_ask, settled=()):
    """One open market per series; `settled` = [(ticker, result), ...] for the sweep."""
    def get(url, params=None):
        params = params or {}
        if url.endswith("/events"):
            return {"events": [{"event_ticker": "EV-" + params["series_ticker"]}]}
        if url.endswith("/markets") and "event_ticker" in params:
            s = params["event_ticker"][3:]
            return {"markets": [{"ticker": f"{s}-26NOV251215-15", "floor_strike": 101000.0,
                                 "close_time": CLOSE.isoformat(),
                                 "yes_ask_dollars": yes_ask,
                                 "no_ask_dollars": round(1.02 - yes_ask, 2)}]}
        if url.endswith("/markets"):
            return {"markets": [{"ticker": tk, "result": res} for tk, res in settled
                                if tk.startswith(params["series_ticker"])]}
        if "coinbase" in url:
            return {"data": {"amount": "101100.0"}}
        raise AssertionError(url)
    return get


def collect(path, yes_ask, minute=0, settled=()):
    return s0.run_cycle(get=exchange(yes_ask, settled), now=T0 + timedelta(minutes=minute),
                        path=path, fetch_book=lambda t: None)


def readable(path):
    out = []
    for line in path.read_bytes().decode("utf-8", "replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def tear_last_row(path: Path) -> None:
    """A power cut in the middle of an append: half of the last row, no newline."""
    data = path.read_bytes()
    last = data.rstrip(b"\n").rfind(b"\n") + 1
    path.write_bytes(data[: last + (len(data) - last) // 2])


# ── a torn last row costs that row, not the next one as well ─────────────────

def test_a_torn_log_tail_costs_one_row_not_two(tmp_path):
    p = tmp_path / "s0.jsonl"
    assert collect(p, 0.55, 0) == {"obs": 2, "settles": 0}
    tear_last_row(p)                                   # the ETH row is cut mid-write
    assert collect(p, 0.58, 1) == {"obs": 2, "settles": 0}
    assert [(r["series"], r["yes_ask"]) for r in readable(p)] == [
        ("KXBTC15M", 0.55), ("KXBTC15M", 0.58), ("KXETH15M", 0.58)]


def test_a_torn_history_tail_does_not_swallow_the_next_snapshot(tmp_path):
    lp, hp = tmp_path / "log.jsonl", tmp_path / "hist.jsonl"
    lp.write_text(json.dumps({"t": "obs", "ticker": "T9", "mins_left": 1.0,
                              "yes_ask": 0.85, "no_ask": 0.17}) + "\n"
                  + json.dumps({"t": "settle", "ticker": "T9", "result": "yes"}) + "\n",
                  encoding="utf-8")
    hp.write_bytes(b'{"ts":"2026-11-25T11:00:00+00:00","joined":0,"cells":[]}\n'
                   b'{"ts":"2026-11-25T12:00:00+00:00","joi')            # torn
    s0.write_snapshot(path=lp, history=hp)
    snaps = readable(hp)
    assert [s["joined"] for s in snaps] == [0, 1]      # the new snapshot is its own line
    assert hp.read_bytes().endswith(b"\n")
