#!/usr/bin/env python3
"""run_dashboards — start both Kalshi dashboards on ANY OS (Windows/macOS/Linux).

The launchd installer only works on macOS; this is the portable path. One command:

    python scripts/run_dashboards.py            # start both, ctrl-c stops both
    python scripts/run_dashboards.py --check    # just verify python/deps/repo state

Self-locating (derives the repo root from this file), installs missing Python deps
from requirements.txt on first run, loads .env if present, then serves:

    crypto  (15-min sleeve)  ->  http://127.0.0.1:5053
    weather (temp sleeve)    ->  http://127.0.0.1:5054

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
DASHBOARDS = [
    ("crypto", "kalshi-dashboard", 5053),
    ("weather", "kalshi-weather-dashboard", 5054),
]


def ensure_deps() -> None:
    """Install requirements.txt only if a core import is missing (fast when healthy)."""
    try:
        import flask, dotenv, requests, yaml  # noqa: F401
        return
    except ImportError:
        pass
    print("first run on this machine — installing python deps from requirements.txt ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r",
                           str(ROOT / "requirements.txt")])


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

    procs = []
    for name, cmd, port in DASHBOARDS:
        log = open(ROOT / "logs" / f"dash_{name}.log", "a")
        p = subprocess.Popen([sys.executable, str(ROOT / "main.py"), cmd, f"--port={port}"],
                             cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
        procs.append((name, port, p))
        print(f"  {name:8} -> http://127.0.0.1:{port}   (log: logs/dash_{name}.log)")

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
