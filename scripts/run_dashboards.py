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
"""
from __future__ import annotations

import os
import subprocess
import sys
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
    url = f"http://{host}:{port}/"
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
    print("=" * 68)
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return check()
    if "--doctor" in sys.argv or "doctor" in sys.argv[1:]:
        return doctor()
    ensure_deps()
    load_env()
    (ROOT / "logs").mkdir(exist_ok=True)

    # Windows: child stdout redirected to a file defaults to cp1252, so the dashboards'
    # unicode banners (─ → ▲) raise UnicodeEncodeError at boot -> exit 1. Force UTF-8.
    child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    def spawn(name: str, cmd: str, port: int):
        log = open(ROOT / "logs" / f"dash_{name}.log", "a", encoding="utf-8")
        return subprocess.Popen([sys.executable, str(ROOT / "main.py"), cmd, f"--port={port}"],
                                cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                                env=child_env)

    # Each dashboard is supervised ON ITS OWN. This used to stop every dashboard when
    # any one exited - harmless with two, but with three it meant a crash in a new page
    # took the working ones down with it, and the scheduled task could not restart them
    # while this launcher stayed alive. Now a dead dashboard is restarted with backoff
    # (15s, 30s, ... capped at 5 min), the others keep serving, and the log says why.
    procs = {}
    for name, cmd, port in DASHBOARDS:
        procs[name] = {"cmd": cmd, "port": port, "p": spawn(name, cmd, port),
                       "fails": 0, "retry_at": 0.0, "started": time.time()}
        print(f"  {name:8} -> http://{DASH_HOST}:{port}   (log: logs/dash_{name}.log)")

    print(f"\n{len(procs)} dashboards running — open the links above in a browser ON THIS"
          " computer.")
    print("press ctrl-c here to stop them all.")
    try:
        while True:
            time.sleep(2)
            now = time.time()
            for name, st in procs.items():
                p = st["p"]
                if p is not None and p.poll() is not None:
                    st["fails"] += 1
                    delay = min(300, 15 * 2 ** (st["fails"] - 1))
                    print(f"\n{name} dashboard exited (code {p.returncode}) — retrying in "
                          f"{delay}s; the others keep running. See logs/dash_{name}.log")
                    st["p"], st["retry_at"] = None, now + delay
                elif p is None and now >= st["retry_at"]:
                    st["p"], st["started"] = spawn(name, st["cmd"], st["port"]), now
                    print(f"{name} dashboard restarted (attempt {st['fails'] + 1})")
                elif p is not None and st["fails"] and now - st["started"] > 600:
                    st["fails"] = 0                       # stable for 10 min: reset backoff
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
