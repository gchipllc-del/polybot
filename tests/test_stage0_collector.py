"""stage0_collector - one damaged line, one crash or one second copy must not stop or
corrupt collection. What is collected is frozen; these pin only how it survives faults."""
import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import stage0_collector as s0  # noqa: E402
from lib import single_instance as si  # noqa: E402

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


# ── one damaged line must not stop collection, the snapshot or the reports ───

BTC = "KXBTC15M-26NOV251215-15"
# a UTF-16 byte-order mark and a cp1252 e-acute: what another writer leaves behind
STRAY = b"\xff\xfe stray bytes from another writer \xe9\n"


def test_one_undecodable_byte_does_not_stop_collection(tmp_path):
    """_seen_settles is the first thing a cycle does and it decoded strictly: with one
    stray byte anywhere in the log EVERY cycle raised before its first request and
    appended nothing, while the task stayed 'Running' - until a hand repair."""
    p = tmp_path / "s0.jsonl"
    assert collect(p, 0.55, 0) == {"obs": 2, "settles": 0}
    with open(p, "ab") as f:
        f.write(STRAY)
    size = p.stat().st_size
    assert collect(p, 0.58, 1, [(BTC, "yes")]) == {"obs": 2, "settles": 1}
    assert p.stat().st_size > size                     # the cycle wrote its rows
    # ... and the settle logged AFTER the damaged line is still known: no duplicate
    assert collect(p, 0.58, 2, [(BTC, "yes")]) == {"obs": 2, "settles": 0}
    rows = readable(p)
    assert sum(r["t"] == "obs" for r in rows) == 6
    assert [r["ticker"] for r in rows if r["t"] == "settle"] == [BTC]


def test_a_damaged_log_still_snapshots_and_reports(tmp_path, monkeypatch, capsys):
    p, hp = tmp_path / "s0.jsonl", tmp_path / "hist.jsonl"
    collect(p, 0.55, 0)
    with open(p, "ab") as f:
        f.write(STRAY)
    collect(p, 0.58, 1, [(BTC, "yes")])
    s0.write_snapshot(path=p, history=hp)              # the hourly read of the whole log
    assert [s["joined"] for s in readable(hp)] == [1]
    monkeypatch.setattr(s0, "LOG", p)
    monkeypatch.setattr(sys, "argv", ["stage0_collector.py", "report"])
    assert s0.main() == 0
    assert "joined market-band observations: 1" in capsys.readouterr().out
    with open(hp, "ab") as f:                          # the trend history, damaged too
        f.write(STRAY)
    s0.print_trend(history=hp)
    assert "stage0 trend - 1 snapshots" in capsys.readouterr().out


def test_a_line_that_is_json_but_not_a_row_does_not_stop_collection(tmp_path):
    """Same symptom, other trigger: a hand edit or another writer leaves a line that
    parses but is not an object. The per-cycle read must skip it, not raise on it."""
    p = tmp_path / "s0.jsonl"
    collect(p, 0.55, 0, [(BTC, "yes")])
    with open(p, "ab") as f:
        f.write(b'null\n123\n"text"\n[1,2]\n\n')
    assert collect(p, 0.58, 1, [(BTC, "yes")]) == {"obs": 2, "settles": 0}


# ── one collector per log ─────────────────────────────────────────────────────

HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from lib import single_instance as si
si.hold(sys.argv[2])
print("HOLDING", flush=True)
time.sleep(60)
"""


@pytest.fixture
def lock_dir(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    monkeypatch.setenv("POLYBOT_LOCK_DIR", str(d))
    yield d
    si.release("stage0_collector")


def live_twin(lock_dir):
    """Another OS process that holds the collector's lock, as the scheduled task does."""
    twin = subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT), "stage0_collector"],
                            env=dict(os.environ, POLYBOT_LOCK_DIR=str(lock_dir)),
                            stdout=subprocess.PIPE, text=True)
    assert twin.stdout.readline().strip() == "HOLDING"
    return twin


def stop(twin):
    twin.kill()
    twin.wait(timeout=10)
    twin.stdout.close()


@pytest.mark.parametrize("cmd", ["collect", "once"])
def test_a_second_collector_stands_down_before_it_collects(cmd, lock_dir, monkeypatch,
                                                           capsys):
    """Two collectors on one log write every observation and every settlement twice
    (4 settle rows for 2 settlements with two real processes)."""
    def never(*a, **k):
        raise AssertionError("a second copy must not collect")
    monkeypatch.setattr(s0, "run_cycle", never)
    monkeypatch.setattr(s0, "collect_loop", never)
    monkeypatch.setattr(sys, "argv", ["stage0_collector.py", cmd])
    twin = live_twin(lock_dir)
    try:
        assert s0.main() == 1
    finally:
        stop(twin)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("not starting: stage0_collector is already "
                                               f"running (pid {twin.pid})")


def test_the_only_collector_takes_the_lock_and_a_real_second_process_exits_nonzero(
        lock_dir, tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(s0, "run_cycle", lambda **kw: ran.append(kw) or {"obs": 0,
                                                                         "settles": 0})
    monkeypatch.setattr(sys, "argv", ["stage0_collector.py", "once"])
    assert s0.main() == 0 and len(ran) == 1
    assert "stage0_collector" in si._HELD             # held until this process exits
    # the real command line. If it did NOT stand down it would collect: so it gets a
    # scratch log and a dead proxy.
    env = dict(os.environ, POLYBOT_LOCK_DIR=str(lock_dir),
               STAGE0_LOG=str(tmp_path / "twin_stage0.jsonl"),
               STAGE0_HISTORY=str(tmp_path / "twin_hist.jsonl"),
               HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
               NO_PROXY="")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "stage0_collector.py"),
                        "once"], env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1 and "already running" in r.stdout, (r.stdout, r.stderr)
    assert not (tmp_path / "twin_stage0.jsonl").exists()


def test_the_lock_never_blocks_a_cycle_function_or_a_read_only_command(
        lock_dir, tmp_path, monkeypatch, capsys):
    """Tests, the selftest the watchdog runs and the report must all work beside the
    live collector: the lock lives in main() for collect/once only."""
    twin = live_twin(lock_dir)
    try:
        p = tmp_path / "s0.jsonl"
        assert collect(p, 0.55) == {"obs": 2, "settles": 0}          # run_cycle itself
        monkeypatch.setattr(s0, "LOG", p)
        monkeypatch.setattr(s0, "HISTORY", tmp_path / "hist.jsonl")
        for cmd, said in (("report", "stage0 report"), ("trend", "no trajectory history"),
                          ("snapshot", "snapshot appended")):
            monkeypatch.setattr(sys, "argv", ["stage0_collector.py", cmd])
            assert s0.main() == 0
            assert said in capsys.readouterr().out
        monkeypatch.setattr(s0, "_selftest", lambda: 0)
        monkeypatch.setattr(sys, "argv", ["stage0_collector.py", "selftest"])
        assert s0.main() == 0
    finally:
        stop(twin)
    assert "stage0_collector" not in si._HELD


# ── the PC clock against Kalshi's: measured, never used ──────────────────────

class Resp:
    """What requests.get returns, as far as _get looks at it."""

    def __init__(self, body=None, headers=None, status=200):
        self._body, self.status = body, status
        if headers is not None:
            self.headers = headers

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"{self.status} Server Error")

    def json(self):
        return self._body


@pytest.fixture
def samples(monkeypatch):
    fresh = deque(maxlen=64)
    monkeypatch.setattr(s0, "_CLOCK_SAMPLES", fresh)
    return fresh


PC = 1_800_000_000.0                                  # a PC clock reading, whole second


def dated(offset_s, **extra):
    return Resp(headers=dict({"Date": formatdate(PC + offset_s, usegmt=True)}, **extra))


def test_clock_skew_is_the_date_header_minus_the_pc_clock(samples):
    """Nothing compared the PC clock with anything: _get threw the Date header away. A
    10 s error can put a third of the entries in the wrong time band, unnoticed."""
    mark = time.monotonic()
    assert s0.clock_skew_since(mark) is None          # no answer yet: unknown, not zero
    # Kalshi stamped :10 while the PC, half-way through a 0.2 s request, read :00.1.
    # The header is in whole seconds, so its centre is :10.5.
    s0._note_server_clock(s0.KALSHI + "/events", dated(10), PC, PC + 0.2)
    assert s0.clock_skew_since(mark) == pytest.approx(10.4)
    assert s0.clock_skew_since(time.monotonic() + 1) is None      # nothing since then
    samples.clear()
    s0._note_server_clock(s0.KALSHI + "/events", dated(-7), PC, PC + 0.2)
    assert s0.clock_skew_since(mark) == pytest.approx(-6.6)       # the PC clock is ahead


def test_the_quickest_answer_of_the_cycle_is_the_one_believed(samples):
    mark = time.monotonic()
    s0._note_server_clock(s0.KALSHI + "/markets", dated(2), PC, PC + 3.0)     # +-2.0 s
    s0._note_server_clock(s0.KALSHI + "/markets", dated(0), PC, PC + 0.1)     # +-0.55 s
    s0._note_server_clock(s0.KALSHI + "/markets", dated(1), PC, PC + 1.0)
    assert s0.clock_skew_since(mark) == pytest.approx(0.45)


def test_what_is_not_a_clean_kalshi_reading_leaves_no_sample_and_never_raises(samples):
    k = s0.KALSHI + "/markets"
    s0._note_server_clock("https://api.coinbase.com/v2/prices/BTC-USD/spot", dated(10),
                          PC, PC + 0.1)               # not Kalshi's clock
    s0._note_server_clock(k, dated(10), PC, PC + 9.0)  # too slow to say anything about 5 s
    s0._note_server_clock(k, dated(10, Age="3"), PC, PC + 0.1)    # held by a cache
    s0._note_server_clock(k, Resp(headers={}), PC, PC + 0.1)      # no Date header
    s0._note_server_clock(k, Resp(headers={"Date": "yesterday-ish"}), PC, PC + 0.1)
    s0._note_server_clock(k, Resp(), PC, PC + 0.1)    # a test double with no headers
    s0._note_server_clock(k, None, PC, PC + 0.1)
    s0._note_server_clock(k, dated(10), PC + 5, PC)   # the clock stepped back mid-request
    assert list(samples) == []
    s0._note_server_clock(k, dated(10, Age="0"), PC, PC + 0.1)    # Age 0 is a fresh answer
    assert len(samples) == 1


def test_get_keeps_the_date_header_and_still_behaves_as_before(samples, monkeypatch):
    import requests
    calls = []

    def fake_requests_get(url, params=None, timeout=None):
        calls.append((url, params, timeout))
        return answers.pop(0)
    monkeypatch.setattr(requests, "get", fake_requests_get)
    mark, url = time.monotonic(), s0.KALSHI + "/markets"
    ahead = {"Date": formatdate(time.time() + 10, usegmt=True)}
    answers = [Resp({"markets": [1]}, ahead), Resp({"markets": [2]}),
               Resp({"markets": []}, ahead, status=503)]
    assert s0._get(url, {"limit": 200}) == {"markets": [1]}
    assert calls == [(url, {"limit": 200}, 15)]       # the same request as ever
    assert abs(s0.clock_skew_since(mark) - 10) < 1.1  # whole-second header: +-0.5 s
    assert s0._get(url) == {"markets": [2]}           # a double without headers: fine
    with pytest.raises(RuntimeError):                 # an error answer still raises...
        s0._get(url)
    assert len(samples) == 2                          # ...and its Date still counted


def test_the_collector_only_measures_the_clock_it_never_corrects_its_rows(samples,
                                                                          tmp_path,
                                                                          monkeypatch):
    """The rows' ts and mins_left come from the PC clock whatever Kalshi's says: using
    the server time would move observations between the frozen time bands."""
    import requests

    def served_with(headers):
        def fake_requests_get(url, params=None, timeout=None):
            return Resp(exchange(0.55)(url, params), headers)
        return fake_requests_get
    logs = []
    for name, headers in (("an_hour_off", {"Date": formatdate(time.time() + 3600,
                                                               usegmt=True)}),
                          ("no_date", {})):
        monkeypatch.setattr(requests, "get", served_with(headers))
        p = tmp_path / f"{name}.jsonl"
        assert s0.run_cycle(now=T0, path=p, fetch_book=lambda t: None) == {
            "obs": 2, "settles": 0}
        logs.append(p.read_bytes())
    assert logs[0] == logs[1]
    assert [r["mins_left"] for r in readable(tmp_path / "an_hour_off.jsonl")] == [
        pytest.approx(14 + 40 / 60)] * 2
    assert abs(s0.clock_skew_since(0.0) - 3600) < 1.1
    assert len(samples) == 6          # 2 series x 3 Kalshi calls; Coinbase's 2 leave none
