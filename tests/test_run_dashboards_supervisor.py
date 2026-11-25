"""run_dashboards: one supervisor at a time, and (when asked) a hung dashboard restarted.

A second copy started by hand while the scheduled task ran went straight to the startup
sweep, which cannot tell a LIVE supervisor's dashboards from leftovers: on Windows it
stopped all three and the two supervisors then fought over the ports for as long as both
ran. And a dashboard that hung with its port open was never restarted: the supervisor
only watched for exits.
"""
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_dashboards as rd  # noqa: E402
from lib import single_instance as si  # noqa: E402

HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from lib import single_instance as si
si.hold("run_dashboards")
print("HOLDING", flush=True)
time.sleep(60)
"""


@pytest.fixture
def lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYBOT_LOCK_DIR", str(tmp_path / "locks"))
    yield tmp_path / "locks"
    si.release("run_dashboards")


def _no_side_effects(monkeypatch, tmp_path, events):
    """main() up to the first spawn, with everything that touches the machine recorded."""
    monkeypatch.setattr(sys, "argv", ["run_dashboards.py"])
    monkeypatch.setattr(sys, "path", list(sys.path))      # main() may add ROOT: undone
    monkeypatch.setattr(rd, "ROOT", tmp_path)
    monkeypatch.setattr(rd, "ensure_deps", lambda: None)
    monkeypatch.setattr(rd, "load_env", lambda: None)
    monkeypatch.setattr(rd, "_sweep_stale", lambda *a, **k: events.append("sweep") or 0)

    def no_spawn(*a, **k):
        events.append("spawn")
        raise AssertionError("dashboards were spawned")
    monkeypatch.setattr(rd.subprocess, "Popen", no_spawn)


def test_a_second_copy_stands_down_before_the_sweep(lock_dir, tmp_path, monkeypatch, capsys):
    first = subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert first.stdout.readline().strip() == "HOLDING"       # a live supervisor
        events = []
        _no_side_effects(monkeypatch, tmp_path, events)
        assert rd.main() == 1                       # non-zero, and nothing else happened
        assert events == []                         # no sweep (it would stop the live
        out = capsys.readouterr().out               # copy's pages), no spawn
        said = [line for line in out.splitlines() if line.strip()]
        assert len(said) == 1 and said[0].startswith("not starting: ")
        assert "already running" in said[0] and f"pid {first.pid}" in said[0]
        assert "Stop-ScheduledTask PolybotDashboards" in said[0]  # what to do about it
        assert not (tmp_path / "logs").exists()
    finally:
        first.kill()
        first.wait(timeout=10)
        first.stdout.close()


def test_the_only_copy_takes_the_lock_then_sweeps(lock_dir, tmp_path, monkeypatch):
    events = []
    _no_side_effects(monkeypatch, tmp_path, events)
    real = rd._stand_down
    monkeypatch.setattr(rd, "_stand_down", lambda: events.append("lock") or real())
    with pytest.raises(AssertionError, match="dashboards were spawned"):
        rd.main()                                   # ran on, as far as the first spawn
    assert events == ["lock", "sweep", "spawn"]
    assert (lock_dir / "run_dashboards.lock").exists() and "run_dashboards" in si._HELD
    assert rd._stand_down() is None                 # held by THIS process: go ahead


def test_a_lock_that_cannot_be_taken_never_keeps_the_pages_down(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setenv("POLYBOT_LOCK_DIR", str(blocker / "locks"))
    assert rd._stand_down() is None                 # fail open: run without the lock
    assert "running without it" in capsys.readouterr().err
    assert "run_dashboards" not in si._HELD


# ── a dashboard that hangs with its port open ─────────────────────────────────────────

class FakeProc:
    def __init__(self, alive=True):
        self.alive, self.returncode, self.kills = alive, (None if alive else 1), 0

    def poll(self):
        return None if self.alive else self.returncode

    def kill(self):
        self.kills += 1
        self.alive, self.returncode = False, -9

    def wait(self, timeout=None):
        return self.returncode


def _st(p, port, started=0.0, **more):
    return dict({"cmd": "x", "port": port, "p": p, "fails": 0, "retry_at": 0.0,
                 "started": started, "misses": 0}, **more)


def test_the_probe_is_off_unless_asked_for(monkeypatch):
    """While the crypto dashboard rebuilds its snapshot a busy page and a hung one are
    too close to tell apart by default; a wrong kill sends the page back to 'warming'."""
    monkeypatch.delenv("POLYBOT_DASH_PROBE", raising=False)
    assert rd._probe_enabled() is False
    for on in ("1", "true", "YES", " on "):
        monkeypatch.setenv("POLYBOT_DASH_PROBE", on)
        assert rd._probe_enabled() is True
    for off in ("", "0", "no", "off"):
        monkeypatch.setenv("POLYBOT_DASH_PROBE", off)
        assert rd._probe_enabled() is False
    # generous: the page shell took 3.3 s at five times today's data while rebuilding
    assert rd.PROBE_TIMEOUT_S >= 15 and rd.PROBE_MISSES >= 3 and rd.PROBE_EVERY_S >= 60


def test_the_prober_counts_misses_in_a_row_for_running_dashboards_only():
    now = 10_000.0
    procs = {"hung": _st(FakeProc(), 1), "fine": _st(FakeProc(), 2),
             "dead": _st(FakeProc(alive=False), 3, misses=2),
             "starting": _st(FakeProc(), 4, started=now - rd.PROBE_GRACE_S + 5, misses=1)}
    asked, sleeps = [], []

    def answers(host, port):
        asked.append(port)
        return port != 1
    rd._probe_loop(procs, answers=answers, sleep=sleeps.append, clock=lambda: now, rounds=2)
    assert asked == [1, 2, 1, 2]                    # never the dead or the starting one
    assert {k: v["misses"] for k, v in procs.items()} == {
        "hung": 2, "fine": 0, "dead": 0, "starting": 0}
    assert sleeps == [rd.PROBE_EVERY_S] * 2
    rd._probe_loop(procs, answers=answers, sleep=sleeps.append, clock=lambda: now, rounds=1)
    assert procs["hung"]["misses"] == 3 == rd.PROBE_MISSES
    rd._probe_loop(procs, answers=lambda h, p: True, sleep=sleeps.append, clock=lambda: now,
                   rounds=1)
    assert procs["hung"]["misses"] == 0             # one answer: the count starts over

    def restarted_meanwhile(host, port):            # the answer is about the OLD process
        procs["hung"]["p"] = FakeProc()
        return False
    rd._probe_loop({"hung": procs["hung"]}, answers=restarted_meanwhile, sleep=sleeps.append,
                   clock=lambda: now, rounds=1)
    assert procs["hung"]["misses"] == 0


def test_a_dashboard_that_stopped_answering_is_stopped_and_restarted_with_backoff():
    hung, fine = FakeProc(), FakeProc()
    procs = {"crypto": _st(hung, 5153, misses=rd.PROBE_MISSES),
             "command": _st(fine, 5155, misses=rd.PROBE_MISSES - 1)}
    said, spawned = [], []

    def spawn(name, cmd, port):
        spawned.append(name)
        return FakeProc()
    rd._supervise_once(procs, spawn, 1000.0, said.append)
    assert hung.kills == 1 and fine.kills == 0      # two misses are not three
    assert "crypto dashboard has not answered 3 probes in a row" in said[0]
    assert "crypto dashboard exited (code -9)" in said[1] and "retrying in 15s" in said[1]
    assert procs["crypto"]["p"] is None and procs["crypto"]["retry_at"] == 1015.0
    rd._supervise_once(procs, spawn, 1014.0, said.append)
    assert spawned == []                            # not before its backoff is over
    rd._supervise_once(procs, spawn, 1015.0, said.append)
    assert spawned == ["crypto"] and procs["crypto"]["misses"] == 0
    assert procs["crypto"]["started"] == 1015.0 and "restarted (attempt 2)" in said[-1]
    assert procs["command"]["p"] is fine


def test_an_exit_is_still_restarted_with_backoff_and_forgiven_after_ten_minutes():
    procs = {"weather": _st(FakeProc(alive=False), 5154)}
    said, delays = [], []

    def spawn(name, cmd, port):
        return FakeProc(alive=False)                # it dies at boot every time
    now = 0.0
    for _ in range(7):
        rd._supervise_once(procs, spawn, now, said.append)        # notices the exit
        delays.append(procs["weather"]["retry_at"] - now)
        now = procs["weather"]["retry_at"]
        rd._supervise_once(procs, spawn, now, said.append)        # restarts it
    assert delays == [15, 30, 60, 120, 240, 300, 300]             # capped at 5 minutes
    procs["weather"]["p"] = FakeProc()                            # this one stays up
    rd._supervise_once(procs, spawn, now + 599, said.append)
    assert procs["weather"]["fails"] == 7
    rd._supervise_once(procs, spawn, now + 601, said.append)
    assert procs["weather"]["fails"] == 0                         # stable: backoff reset


def _serve(handler):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held = []

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            held.append(conn)
            handler(conn)
    threading.Thread(target=loop, daemon=True).start()
    return srv, held


def _reply(status):
    def handler(conn):
        conn.recv(65536)
        conn.sendall(f"HTTP/1.1 {status}\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                     .encode())
        conn.close()
    return handler


def test_answering_means_any_http_answer_and_an_open_port_alone_is_not_one():
    silent, held = _serve(lambda conn: None)        # accepts, never answers: a hung page
    ok, _ = _serve(_reply("200 OK"))
    broken, _ = _serve(_reply("500 Internal Server Error"))
    try:
        port = silent.getsockname()[1]
        assert rd._port_listening("127.0.0.1", port) is True      # all the watchdog saw
        assert rd._answers("127.0.0.1", port, timeout=0.3) is False
        assert rd._answers("127.0.0.1", ok.getsockname()[1], timeout=2) is True
        assert rd._answers("0.0.0.0", ok.getsockname()[1], timeout=2) is True   # bound wide
        # a page that answers 500 is broken, not hung: restarting it would not mend it
        assert rd._answers("127.0.0.1", broken.getsockname()[1], timeout=2) is True
    finally:
        for s in (silent, ok, broken, *held):
            s.close()
    assert rd._answers("127.0.0.1", port, timeout=0.3) is False   # nothing listening
