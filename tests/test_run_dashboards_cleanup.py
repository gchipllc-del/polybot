"""Dashboards must not outlive their supervisor: on Windows, Stop-ScheduledTask killed
the supervisor but its dashboards kept their ports with OLD code, so every restart's
fresh dashboards failed to bind and pulled fixes never showed."""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import run_dashboards as rd  # noqa: E402

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:5153         0.0.0.0:0              LISTENING       4411
  TCP    127.0.0.1:5155         0.0.0.0:0              ABHÖREN         4433
  TCP    [::1]:5154             [::]:0                 LISTENING       4422
  TCP    127.0.0.1:5153         127.0.0.1:61000        ESTABLISHED     4411
  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       999
  TCP    127.0.0.1:5156         0.0.0.0:0              LISTENING       4444
"""


def test_listeners_on_our_ports_any_locale():
    assert rd._listening_pids(NETSTAT, {5153, 5154, 5155}) == {4411, 4422, 4433}


def _fake_run(cmdlines, calls):
    def run(args, **kw):
        calls.append(args)
        if args[0] == "netstat":
            return types.SimpleNamespace(stdout=NETSTAT)
        if args[0] == "powershell":
            pid = int(args[-1].split("ProcessId=")[1].split("'")[0])
            return types.SimpleNamespace(stdout=cmdlines.get(pid, "") + "\n")
        return types.SimpleNamespace(stdout="")
    return run


def test_only_our_dashboards_are_stopped(monkeypatch):
    monkeypatch.setattr(rd.time, "sleep", lambda s: None)
    calls = []
    cmdlines = {4411: r'"C:\Python312\python.exe" C:\polybot\main.py kalshi-dashboard --port=5153',
                4422: r'"C:\Python312\python.exe" C:\other\main.py something-else',
                4433: r'"C:\Python312\python.exe" C:\polybot\main.py command-center --port=5155'}
    assert rd._sweep_stale(run=_fake_run(cmdlines, calls), windows=True) == 2
    killed = [a[2] for a in calls if a[0] == "taskkill"]
    assert killed == ["4411", "4433"]                  # 4422 is not ours: left alone


def test_no_sweep_off_windows():
    calls = []
    assert rd._stale_dashboards({5153}, run=_fake_run({}, calls), windows=False) == []
    assert calls == []
    if rd.os.name != "nt":
        assert rd._kill_on_close_job() is None and rd._adopt(None, object()) is False
