#!/usr/bin/env python3
"""run_dashboards — start both Kalshi dashboards on ANY OS (Windows/macOS/Linux).

The launchd installer only works on macOS; this is the portable path. One command:

    python scripts/run_dashboards.py            # start both, ctrl-c stops both
    python scripts/run_dashboards.py --check    # just verify python/deps/repo state

Self-locating (derives the repo root from this file), installs missing Python deps
from requirements.txt on first run, loads .env if present, then serves:

    crypto  (15-min sleeve)  ->  http://127.0.0.1:5153
    weather (temp sleeve)    ->  http://127.0.0.1:5154

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
]
DASH_HOST = os.environ.get("POLYBOT_DASH_HOST", "127.0.0.1")


# What the dashboards actually import. Deliberately NOT `pip install -r
# requirements.txt`: that file pins `tradingcore @ file:../tradingcore` (a sibling repo
# from the original machine), and pip aborts the WHOLE install when the path is missing —
# which bricked first-run on a fresh Windows box. The dashboards don't need tradingcore.
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
    print(f"main.py     : {'OK' if (ROOT / 'main.py').exists() else 'MISSING — wrong folder?'}")
    try:
        import flask  # noqa: F401
        print("deps        : OK")
    except ImportError:
        print("deps        : missing (will auto-install on first run)")
    print(f".env        : {'present' if (ROOT / '.env').exists() else 'absent (read-only mode)'}")
    return 0


def main() -> int:
    if "--check" in sys.argv:
        return check()
    ensure_deps()
    load_env()
    (ROOT / "logs").mkdir(exist_ok=True)

    # Windows: child stdout redirected to a file defaults to cp1252, so the dashboards'
    # unicode banners (─ → ▲) raise UnicodeEncodeError at boot -> exit 1. Force UTF-8.
    child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    procs = []
    for name, cmd, port in DASHBOARDS:
        log = open(ROOT / "logs" / f"dash_{name}.log", "a", encoding="utf-8")
        p = subprocess.Popen([sys.executable, str(ROOT / "main.py"), cmd, f"--port={port}"],
                             cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                             env=child_env)
        procs.append((name, port, p))
        print(f"  {name:8} -> http://{DASH_HOST}:{port}   (log: logs/dash_{name}.log)")

    print("\nboth dashboards running — open the links above in a browser ON THIS computer.")
    print("press ctrl-c here to stop both.")
    try:
        while True:
            time.sleep(2)
            for name, port, p in procs:
                if p.poll() is not None:
                    print(f"\n{name} dashboard exited (code {p.returncode}) — "
                          f"see logs/dash_{name}.log. Stopping the other.")
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        print("\nstopping ...")
        for _, _, p in procs:
            if p.poll() is None:
                p.terminate()
        for _, _, p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
