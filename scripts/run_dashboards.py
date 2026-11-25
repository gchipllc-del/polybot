#!/usr/bin/env python3
"""run_dashboards — start both Kalshi dashboards on ANY OS (Windows/macOS/Linux).

The launchd installer only works on macOS; this is the portable path. One command:

    python scripts/run_dashboards.py            # start both, ctrl-c stops both
    python scripts/run_dashboards.py --check    # just verify python/deps/repo state

Self-locating (derives the repo root from this file), installs missing Python deps
from requirements.txt on first run, loads .env if present, then serves:

    crypto  (15-min sleeve)  ->  http://127.0.0.1:5153
    weather (temp sleeve)    ->  http://127.0.0.1:5154
    command center           ->  http://127.0.0.1:5155   (every rule, one standard)

Notes for a fresh machine: the pages come up immediately, but panels are empty until
the paper bots have produced data files, and portfolio panels need .env Kalshi keys
(copy .env from the old machine for full function). 127.0.0.1 only works in a browser
on THIS computer — that is expected, not a bug.

One copy at a time: a second copy started while one is alive prints "not starting" and
exits 1 (on Windows its startup sweep would stop the first copy's dashboards and leave
two supervisors fighting over the ports). To run it by hand, stop the scheduled one
first: Stop-ScheduledTask PolybotDashboards.

A dashboard that EXITS is restarted with backoff. One that hangs with its port still
open can be restarted too - set POLYBOT_DASH_PROBE=1 (see PROBE_* below); it is off by
default.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Ports live in the 51xx block to stay clear of the openclaw wheel-trader dashboards
# (5000/5050/5051/8080). Override per machine without code edits:
#   POLYBOT_CRYPTO_DASH_PORT / POLYBOT_WEATHER_DASH_PORT / POLYBOT_DASH_HOST
DASHBOARDS = [
    ("crypto", "kalshi-dashboard", int(os.environ.get("POLYBOT_CRYPTO_DASH_PORT", 5153))),
    ("weather", "kalshi-weather-dashboard", int(os.environ.get("POLYBOT_WEATHER_DASH_PORT", 5154))),
    ("command", "command-center", int(os.environ.get("POLYBOT_COMMAND_DASH_PORT", 5155))),
]
DASH_HOST = os.environ.get("POLYBOT_DASH_HOST", "127.0.0.1")


# What the dashboards actually import - deliberately minimal and fast. (requirements.txt
# once pinned `tradingcore @ file:../tradingcore`, a sibling repo from the original
# machine, and pip aborted the WHOLE install when the path was missing - which bricked
# first-run on a fresh Windows box. That pin is now commented out and lib/ falls back to
# vendor/tradingcore, but the dashboards still only install what they import.)
DASH_DEPS = ["flask>=3.0.0", "python-dotenv>=1.0.0", "requests>=2.31.0",
             "pyyaml>=6.0", "rich>=13.7.0"]


def ensure_deps() -> None:
    """Install only the dashboard deps, and only if an import is missing."""
    try:
        import flask, dotenv, requests, yaml  # noqa: F401
        return
    except ImportError:
        pass
    print("first run on this machine — installing dashboard deps ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", *DASH_DEPS])


def load_env() -> None:
    envp = ROOT / ".env"
    if not envp.exists():
        print("note: no .env found — dashboards run read-only (portfolio panels show n/a).")
        return
    for line in envp.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def check() -> int:
    print(f"python      : {sys.version.split()[0]}  ({sys.executable})")
    print(f"repo root   : {ROOT}")
    print(f"main.py     : {'OK' if (ROOT / 'main.py').exists() else 'MISSING - wrong folder?'}")
    try:
        import flask  # noqa: F401
        print("deps        : OK")
    except ImportError:
        print("deps        : missing (will auto-install on first run)")
    print(f".env        : {'present' if (ROOT / '.env').exists() else 'absent (read-only mode)'}")
    return 0


# ── doctor: one command that says WHY the pages are unreachable ──────────────
# Stdlib only, ASCII only — it must run even when deps/encoding are the problem.

def _port_listening(host: str, port: int, timeout: float = 0.6) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _http_probe(host: str, port: int, timeout: float = 3.0):
    """Returns (status_code, note). Bypasses any proxy env — a system proxy is itself
    a known reason 127.0.0.1 pages 'do not work' in a browser."""
    import urllib.request
    import urllib.error
    url = f"http://{f'[{host}]' if ':' in host else host}:{port}/"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            return r.status, "served"
    except urllib.error.HTTPError as e:
        return e.code, "served (http error)"
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:60]}"


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             cwd=str(ROOT))
        return (out.stdout or out.stderr or "").strip()
    except Exception as e:  # noqa: BLE001
        return f"({type(e).__name__})"


def _tail(p: Path, n: int = 15) -> str:
    if not p.exists():
        return "(no log file)"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"(unreadable: {e})"
    return "\n".join(f"      | {l}" for l in lines[-n:]) or "      | (empty)"


def doctor() -> int:
    is_win = sys.platform.startswith("win")
    print("=" * 68)
    print("polybot dashboard doctor")
    print("=" * 68)

    # 1. which code is actually on disk
    print("\n[1] code version on disk")
    head = _run(["git", "log", "--oneline", "-1"])
    branch = _run(["git", "branch", "--show-current"])
    print(f"      branch : {branch}")
    print(f"      HEAD   : {head}")
    src = (ROOT / "scripts" / "run_dashboards.py").read_text(encoding="utf-8",
                                                             errors="replace")
    new_ports = "5153" in src
    print(f"      ports in this file: {'5153/5154 (NEW)' if new_ports else '5053/5054 (OLD - you have not pulled)'}")
    behind = _run(["git", "status", "-sb"]).splitlines()[:1]
    if behind:
        print(f"      status : {behind[0]}")

    # 2. what is listening where
    print("\n[2] ports (is anything actually serving?)")
    host = DASH_HOST
    watch = [(name, host, port) for name, _, port in DASHBOARDS] + [
             ("crypto OLD", host, 5053), ("weather OLD", host, 5054),
             ("openclaw", "127.0.0.1", 5050), ("openclaw", "127.0.0.1", 5051)]
    for name, h, p in watch:
        if _port_listening(h, p):
            code, note = _http_probe(h, p)
            print(f"      {h}:{p:<5} LISTENING  ({name})  http={code} {note}")
        else:
            print(f"      {h}:{p:<5} -          ({name})")

    # 3. scheduled tasks (windows)
    print("\n[3] background tasks")
    if is_win:
        out = _run(["powershell", "-NoProfile", "-Command",
                    "Get-ScheduledTask PolybotDashboards,PolybotStage0,PolybotPaper,"
                    "PolybotSleeves,PolybotNews,PolybotHealth "
                    "-ErrorAction SilentlyContinue | "
                    "Select-Object TaskName,State | Format-Table -AutoSize | Out-String"])
        print("\n".join(f"      {l}" for l in out.splitlines() if l.strip()) or "      (none found)")
    else:
        print("      (not Windows - skipping scheduled-task check)")

    # 4. logs
    print("\n[4] dashboard logs (last lines)")
    for name in ("crypto", "weather", "command"):
        print(f"    logs/dash_{name}.log:")
        print(_tail(ROOT / "logs" / f"dash_{name}.log"))

    # 5. verdict
    print("\n[5] verdict")
    up = {name: _port_listening(host, port) for name, _, port in DASHBOARDS}
    serving_new = any(up.values())
    serving_old = any(_port_listening(host, p) for p in (5053, 5054))
    down = [n for n, ok in up.items() if not ok]
    if serving_new and not down:
        print("      OK - every dashboard is serving:")
        for name, _, port in DASHBOARDS:
            print(f"        {name:8} http://{host}:{port}")
        print("      If the browser still fails, it is browser-side: try a different")
        print("      browser, or check for a system/corporate proxy that hijacks localhost.")
    elif serving_new:
        # Some up, some down. The usual cause: the launcher running now started BEFORE
        # the pull, so it only knows the dashboards that existed then (it reads its list
        # once, at start). The other cause: that dashboard crashes at boot - its log in
        # [4] shows the traceback, and the launcher retries it on its own.
        print(f"      CAUSE: not serving: {', '.join(down)}. Either the running launcher")
        print("      predates your last pull (it only starts the dashboards it knew about")
        print("      when it started), or that dashboard crashes at boot - see [4].")
        print("      FIX (run each line separately, then wait ~15 seconds):")
        print("        Stop-ScheduledTask PolybotDashboards")
        print("        Start-ScheduledTask PolybotDashboards")
        print("      Still down? Run it in the foreground and read the error:")
        for name, cmd, port in DASHBOARDS:
            if name in down:
                print(f"        py main.py {cmd} --port={port}")
    elif serving_old and not new_ports:
        print("      CAUSE: old code is running on 5053/5054 - you have not pulled yet.")
        print("      FIX (run each line separately):")
        print("        git pull origin claude/polybot-kalshi-bugs-IyuvP")
        print("        Stop-ScheduledTask PolybotDashboards")
        print("        Start-ScheduledTask PolybotDashboards")
    elif serving_old and new_ports:
        print("      CAUSE: code is updated but the RUNNING process is the old one.")
        print("      FIX (run each line separately):")
        print("        Stop-ScheduledTask PolybotDashboards")
        print("        Start-ScheduledTask PolybotDashboards")
    else:
        print("      CAUSE: nothing is listening - the dashboards are not running or they")
        print("      crashed at boot. Read section [4] above for the traceback, then:")
        print("        Start-ScheduledTask PolybotDashboards")
        print("      or run in the foreground to watch it live:")
        print("        py scripts\\run_dashboards.py" if is_win
              else "        python3 scripts/run_dashboards.py")
        print("      (one copy at a time: if that says 'not starting', the scheduled copy")
        print("       is alive - run Stop-ScheduledTask PolybotDashboards first)")
    print("=" * 68)
    return 0


# ── a dashboard must never outlive its supervisor ─────────────────────────────
#
# Stop-ScheduledTask terminates this supervisor outright (no Ctrl-C, no cleanup), and on
# Windows the dashboards it started survived it: they kept their ports with OLD code,
# and every restart's fresh dashboards failed to bind and retried forever - which is why
# pulled dashboard fixes never showed up. Two guards:
#   * a Job Object with KILL_ON_JOB_CLOSE: the OS kills every dashboard when this
#     process dies, however it dies;
#   * a startup sweep: leftovers from a supervisor that predates the job object are
#     stopped before spawning - only processes whose command line is OUR dashboard.

DASH_CMDS = tuple(cmd for _, cmd, _ in DASHBOARDS)


def _kill_on_close_job():
    """A Windows Job Object that kills its processes when this supervisor exits. None on
    other systems, or if Windows refuses (then dashboards behave as before)."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class _BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000     # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info),   # ExtendedLimit
                                           ctypes.sizeof(info)):
            return None
        return job
    except Exception:  # noqa: BLE001 - never stop the dashboards over this
        return None


def _adopt(job, proc) -> bool:
    """Put a freshly spawned dashboard into the job (Windows only)."""
    if job is None:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        return bool(k32.AssignProcessToJobObject(job, int(proc._handle)))
    except Exception:  # noqa: BLE001
        return False


def _listening_pids(netstat_out: str, ports) -> set:
    """PIDs listening on any of `ports`, from `netstat -ano` output. The state column is
    localized on non-English Windows, so a listener is recognized by its foreign
    address (0.0.0.0:0 / [::]:0) as well as by the word LISTENING."""
    pids = set()
    for line in netstat_out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING" and parts[2] not in ("0.0.0.0:0", "[::]:0"):
            continue
        try:
            port, pid = int(parts[1].rsplit(":", 1)[1]), int(parts[-1])
        except (ValueError, IndexError):
            continue
        if port in ports and pid > 0:
            pids.add(pid)
    return pids


def _stale_dashboards(ports, run=subprocess.run, windows: bool | None = None) -> list:
    """[(pid, command line)] of OUR dashboards already holding our ports - leftovers of
    an earlier supervisor. Anything else on those ports is left alone."""
    if not (os.name == "nt" if windows is None else windows):
        return []
    try:
        out = run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                  timeout=20).stdout
    except Exception:  # noqa: BLE001
        return []
    found = []
    for pid in sorted(_listening_pids(out or "", ports)):
        try:
            cmd = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                       f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                      capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception:  # noqa: BLE001
            continue
        if "main.py" in cmd and any(c in cmd.split() for c in DASH_CMDS):
            found.append((pid, cmd))
    return found


def _sweep_stale(run=subprocess.run, windows: bool | None = None) -> int:
    ports = {port for _, _, port in DASHBOARDS}
    stale = _stale_dashboards(ports, run=run, windows=windows)
    for pid, cmd in stale:
        print(f"  stopping a leftover dashboard from an earlier run: pid {pid} ({cmd[-60:]})")
        try:
            run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=20)
        except Exception:  # noqa: BLE001
            pass
    if stale:
        time.sleep(2)                                  # let Windows release the ports
    return len(stale)


# ── one supervisor at a time ──────────────────────────────────────────────────
#
# A second copy (started by hand while the scheduled task runs) used to go straight to
# the startup sweep, which cannot tell a LIVE supervisor's dashboards from leftovers: on
# Windows it stopped all of them and took the ports, and the first supervisor then
# relaunched its three dashboards into "port already in use" for as long as both ran -
# and after the second copy was closed the pages stayed down until the next retry (up
# to 5 minutes). Off Windows the newcomer lost instead, and retried for ever.

def _stand_down() -> str | None:
    """Take the supervisor's single-instance lock for the life of this process. None =
    go ahead (we hold it, or locking is unavailable here and we run without it - a lock
    problem must never keep the pages down); otherwise the one line to print. The OS
    frees the lock when its holder dies, so after a killed supervisor the next one
    starts, and the sweep below still clears what that one left behind."""
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from lib.single_instance import AlreadyRunning, hold
    except Exception as e:  # noqa: BLE001
        print(f"[warn] single-instance lock unavailable ({type(e).__name__}) - "
              f"running without it", file=sys.stderr)
        return None
    try:
        hold("run_dashboards")
    except AlreadyRunning as e:
        return (f"not starting: {e}. The dashboards are served by that copy. To run them "
                f"here instead, stop it first (Stop-ScheduledTask PolybotDashboards, or "
                f"close its window), then start this again.")
    return None


# ── a dashboard that hangs ────────────────────────────────────────────────────
#
# A hung dashboard keeps its port open, so nothing that only looks at the port (this
# supervisor's exit watch, the watchdog's port check) sees it. The probe asks each page
# for "/" once a minute; one that misses PROBE_MISSES in a row is stopped, and the loop
# below restarts it like one that exited.
#
# OFF unless POLYBOT_DASH_PROBE=1. While the crypto dashboard rebuilds its snapshot its
# page shell has been measured at 0.5 s today and 3.3 s at five times today's data, so
# the timeout is generous and three misses a minute apart are needed - but until that
# rebuild is made cheap a busy dashboard and a hung one are too close to tell apart by
# default: a wrong kill sends the page back through "warming".

PROBE_EVERY_S = 60.0
PROBE_TIMEOUT_S = 20.0
PROBE_MISSES = 3
PROBE_GRACE_S = 120.0            # after a (re)start: imports, bind, first snapshot


def _probe_enabled() -> bool:
    return os.environ.get("POLYBOT_DASH_PROBE", "").strip().lower() in ("1", "true", "yes", "on")


def _answers(host: str, port: int, timeout: float | None = None) -> bool:
    """Did the page answer at all? Any HTTP status counts: a page that answers 500 is
    broken, not hung, and a restart would not mend it."""
    host = {"0.0.0.0": "127.0.0.1", "": "127.0.0.1", "::": "::1"}.get(host, host)
    timeout = PROBE_TIMEOUT_S if timeout is None else timeout
    return _http_probe(host, port, timeout)[0] is not None


def _probe_loop(procs: dict, answers=_answers, sleep=time.sleep, clock=time.time,
                rounds: int | None = None) -> None:
    """The prober (its own thread, so a slow answer never delays noticing an exit): it
    only COUNTS consecutive misses in st["misses"]; _supervise_once does the stopping."""
    n = 0
    while rounds is None or n < rounds:
        n += 1
        for st in list(procs.values()):
            p = st["p"]
            if p is None or p.poll() is not None or clock() - st["started"] < PROBE_GRACE_S:
                st["misses"] = 0                  # not running, or still starting up
                continue
            ok = answers(DASH_HOST, st["port"])
            if st["p"] is p:                      # not restarted while we waited
                st["misses"] = 0 if ok else st.get("misses", 0) + 1
        sleep(PROBE_EVERY_S)


def _supervise_once(procs: dict, spawn, now: float, say=print) -> None:
    """One pass over the dashboards. Each is supervised ON ITS OWN: a dead one is
    restarted with backoff (15s, 30s, ... capped at 5 min) while the others keep serving,
    and one the prober has given up on is stopped, to be restarted the same way."""
    for name, st in procs.items():
        p = st["p"]
        if p is not None and p.poll() is None and st.get("misses", 0) >= PROBE_MISSES:
            say(f"\n{name} dashboard has not answered {PROBE_MISSES} probes in a row "
                f"({PROBE_TIMEOUT_S:.0f}s each, {PROBE_EVERY_S:.0f}s apart) with its port open — "
                f"stopping it; it restarts like one that exited.")
            st["misses"] = 0
            try:
                p.kill()                          # it is not answering: no polite stop
                p.wait(timeout=10)
            except Exception:  # noqa: BLE001 - still alive: asked again next pass
                pass
        if p is not None and p.poll() is not None:
            st["fails"] += 1
            delay = min(300, 15 * 2 ** (st["fails"] - 1))
            say(f"\n{name} dashboard exited (code {p.returncode}) — retrying in "
                f"{delay}s; the others keep running. See logs/dash_{name}.log")
            st["p"], st["retry_at"] = None, now + delay
        elif p is None and now >= st["retry_at"]:
            st["p"], st["started"], st["misses"] = spawn(name, st["cmd"], st["port"]), now, 0
            say(f"{name} dashboard restarted (attempt {st['fails'] + 1})")
        elif p is not None and st["fails"] and now - st["started"] > 600:
            st["fails"] = 0                       # stable for 10 min: reset backoff


def main() -> int:
    if "--check" in sys.argv:
        return check()
    if "--doctor" in sys.argv or "doctor" in sys.argv[1:]:
        return doctor()
    ensure_deps()
    load_env()
    refusal = _stand_down()          # BEFORE the sweep: it would stop a live copy's pages
    if refusal:
        print(refusal)
        return 1
    (ROOT / "logs").mkdir(exist_ok=True)

    # Windows: child stdout redirected to a file defaults to cp1252, so the dashboards'
    # unicode banners (─ → ▲) raise UnicodeEncodeError at boot -> exit 1. Force UTF-8.
    child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    _sweep_stale()
    job = _kill_on_close_job()           # kept open for this process's whole life

    def spawn(name: str, cmd: str, port: int):
        log = open(ROOT / "logs" / f"dash_{name}.log", "a", encoding="utf-8")
        p = subprocess.Popen([sys.executable, str(ROOT / "main.py"), cmd, f"--port={port}"],
                             cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                             env=child_env)
        _adopt(job, p)
        return p

    # Each dashboard is supervised ON ITS OWN. This used to stop every dashboard when
    # any one exited - harmless with two, but with three it meant a crash in a new page
    # took the working ones down with it, and the scheduled task could not restart them
    # while this launcher stayed alive. Now a dead dashboard is restarted with backoff
    # (15s, 30s, ... capped at 5 min), the others keep serving, and the log says why.
    procs = {}
    for name, cmd, port in DASHBOARDS:
        procs[name] = {"cmd": cmd, "port": port, "p": spawn(name, cmd, port),
                       "fails": 0, "retry_at": 0.0, "started": time.time(), "misses": 0}
        print(f"  {name:8} -> http://{DASH_HOST}:{port}   (log: logs/dash_{name}.log)")

    print(f"\n{len(procs)} dashboards running — open the links above in a browser ON THIS"
          " computer.")
    if _probe_enabled():
        threading.Thread(target=_probe_loop, args=(procs,), name="dash-probe",
                         daemon=True).start()
        print(f"liveness probe ON: a page that does not answer {PROBE_MISSES} probes in a "
              f"row ({PROBE_TIMEOUT_S:.0f}s each, {PROBE_EVERY_S:.0f}s apart) is restarted.")
    print("press ctrl-c here to stop them all.")
    try:
        while True:
            time.sleep(2)
            _supervise_once(procs, spawn, time.time())
    except KeyboardInterrupt:
        print("\nstopping ...")
        for st in procs.values():
            if st["p"] is not None and st["p"].poll() is None:
                st["p"].terminate()
        for st in procs.values():
            if st["p"] is None:
                continue
            try:
                st["p"].wait(timeout=5)
            except subprocess.TimeoutExpired:
                st["p"].kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
