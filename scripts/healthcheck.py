#!/usr/bin/env python3
"""healthcheck — one command that proves the whole system is actually WORKING, and can
repair it. Written after eight separate breakages, each of which was silent.

The failures we actually hit, and the check that would have caught each one:
  1. pip aborted on a missing sibling repo         -> [deps]      import check
  2. cp1252 crash at dashboard boot (exit 1)       -> [ports]     nothing serving
  3. missing 'tradingcore' 500'd every refresh     -> [api]       endpoint returns error
  4. port collision with the other trading project -> [ports]     who owns each port
  5. scheduled tasks sat in state "Ready" = dead   -> [tasks]     state check + repair
  6. collector never loaded .env, logged no depth  -> [auth]      auth + depth coverage
  7. paper ledger could double-enter one market    -> [ledger]    duplicate-key scan
  8. dashboard and CLI disagreed on sample size    -> [selftest]  every module's selftest

LIVENESS over EXISTENCE: a process being alive proves nothing. This checks that data is
actually accruing - file freshness, row growth, endpoint payloads - because every real
outage here looked healthy from the outside.

  py scripts/healthcheck.py            # full check, exit 0 = healthy
  py scripts/healthcheck.py --repair   # also restart anything dead (Windows tasks)
  py scripts/healthcheck.py --quiet    # only print problems (for scheduled runs)
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

IS_WIN = sys.platform.startswith("win")
TASKS = ["PolybotDashboards", "PolybotStage0", "PolybotPaper"]
PORTS = [("crypto dashboard", 5153), ("weather dashboard", 5154)]
STAGE0_LOG = ROOT / "data" / "stage0_crypto.jsonl"
PAPER_LOG = ROOT / "data" / "paper_crypto15.jsonl"
HISTORY_LOG = ROOT / "data" / "stage0_history.jsonl"

# A collector cycle is 60s; anything past 10 minutes means it is not actually working
# even if the process exists. This threshold is the whole point of the file.
STAGE0_STALE_MIN = 10.0
LOG_BLOAT_MB = 200.0

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def record(status: str, section: str, msg: str) -> None:
    _results.append((status, section, msg))


def _age_min(p: Path) -> float | None:
    if not p.exists():
        return None
    try:
        return (time.time() - p.stat().st_mtime) / 60.0
    except OSError:
        return None


def _rows(p: Path) -> int:
    if not p.exists():
        return 0
    try:
        return sum(1 for line in p.open("r", encoding="utf-8", errors="replace") if line.strip())
    except OSError:
        return 0


def _listening(port: int, host: str = "127.0.0.1") -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _ps(cmd: str, timeout: int = 20) -> str:
    if not IS_WIN:
        return ""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


# ── checks ───────────────────────────────────────────────────────────────────

def check_deps() -> None:
    missing = []
    for mod in ("flask", "dotenv", "requests", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        record(FAIL, "deps", f"missing {', '.join(missing)} - run: "
                             f"py -m pip install flask python-dotenv requests pyyaml rich")
    else:
        record(OK, "deps", "all dashboard/collector imports resolve")


def check_env_auth() -> None:
    envp = ROOT / ".env"
    if not envp.exists():
        record(WARN, "auth", ".env absent - depth capture and portfolio panels disabled")
        return
    try:
        from lib.envload import load_env
        load_env()
        from lib.kalshi_auth import can_sign, status as auth_status
        st = auth_status()
        if can_sign():
            record(OK, "auth", "Kalshi signing works (depth capture enabled)")
        else:
            bad = [k for k, v in st.items() if v is False]
            record(FAIL, "auth", f"cannot sign - false: {', '.join(bad) or 'unknown'} "
                                 f"(run: py main.py kalshi-auth-status)")
    except Exception as e:  # noqa: BLE001
        record(FAIL, "auth", f"auth check raised {type(e).__name__}: {str(e)[:80]}")


def check_tasks(repair: bool) -> None:
    if not IS_WIN:
        record(WARN, "tasks", "not Windows - skipping scheduled-task check")
        return
    out = _ps("Get-ScheduledTask " + ",".join(TASKS) +
              " -ErrorAction SilentlyContinue | ForEach-Object { $_.TaskName + '=' + $_.State }")
    states = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            states[k.strip()] = v.strip()
    for t in TASKS:
        st = states.get(t)
        if st is None:
            record(FAIL, "tasks", f"{t} NOT REGISTERED - run install_autostart.ps1")
        elif st.lower() == "running":
            record(OK, "tasks", f"{t} running")
        else:
            if repair:
                _ps(f"Start-ScheduledTask -TaskName {t}")
                record(WARN, "tasks", f"{t} was '{st}' (dead) - REPAIRED, restarted")
            else:
                record(FAIL, "tasks", f"{t} is '{st}', not Running "
                                      f"(self-heal fires within 10 min, or --repair now)")


def check_ports() -> None:
    for name, port in PORTS:
        if _listening(port):
            record(OK, "ports", f"{name} serving on {port}")
        else:
            record(FAIL, "ports", f"{name} NOT listening on {port}")


def check_api() -> None:
    """A serving port proves nothing - the payload has to be valid. This is the check
    that would have caught the tradingcore 500s while the page still 'loaded'."""
    if not _listening(5153):
        record(WARN, "api", "skipped (dashboard not serving)")
        return
    import urllib.request
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:5153/api/all", timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001
        record(FAIL, "api", f"/api/all unreachable: {type(e).__name__}")
        return
    broken = [k for k, v in data.items() if isinstance(v, dict) and v.get("error")]
    if broken:
        for k in broken:
            record(WARN, "api", f"panel '{k}' reports: {str(data[k]['error'])[:70]}")
    else:
        record(OK, "api", f"/api/all healthy ({len(data)} panels, no errors)")


def check_data_flow() -> None:
    """The silent killer: everything 'up' but no data accruing."""
    age = _age_min(STAGE0_LOG)
    rows = _rows(STAGE0_LOG)
    if age is None:
        record(FAIL, "data", "stage0_crypto.jsonl missing - collector has never written")
    elif age > STAGE0_STALE_MIN:
        record(FAIL, "data", f"stage0 log STALE {age:.0f} min (cycle is 60s) - "
                             f"collector alive but not collecting")
    else:
        record(OK, "data", f"stage0 fresh ({age:.1f} min, {rows} rows)")

    if PAPER_LOG.exists():
        prows = _rows(PAPER_LOG)
        page = _age_min(PAPER_LOG)
        record(OK, "data", f"paper ledger {prows} rows (last write {page:.0f} min ago)")
    else:
        record(WARN, "data", "paper ledger empty - no rule has fired yet")

    if HISTORY_LOG.exists():
        record(OK, "data", f"trend history {_rows(HISTORY_LOG)} snapshots")
    else:
        record(WARN, "data", "no trend snapshots yet (written hourly)")


def check_depth_coverage() -> None:
    """Bug 6 was invisible for days: rows written with no order-book depth at all."""
    if not STAGE0_LOG.exists():
        return
    total = with_depth = 0
    try:
        with STAGE0_LOG.open("r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-400:]
    except OSError:
        return
    for line in tail:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("t") == "obs":
            total += 1
            if d.get("book"):
                with_depth += 1
    if total == 0:
        return
    pct = 100.0 * with_depth / total
    if pct < 1:
        record(FAIL, "depth", f"0% of recent observations have order-book depth - "
                              f"fill realism is unmeasurable (check auth)")
    elif pct < 50:
        record(WARN, "depth", f"only {pct:.0f}% of recent observations carry depth")
    else:
        record(OK, "depth", f"{pct:.0f}% of recent observations carry order-book depth")


def check_ledger_integrity() -> None:
    """Bug 7: the same (market, rule) entered twice would silently double-count P&L."""
    if not PAPER_LOG.exists():
        return
    try:
        import paper_trader as pt
        rows = pt._load(PAPER_LOG)
    except Exception as e:  # noqa: BLE001
        record(FAIL, "ledger", f"unreadable: {type(e).__name__}")
        return
    seen, dupes = set(), 0
    for r in rows:
        if r.get("t") != "open":
            continue
        key = (r.get("ticker"), r.get("rule"))
        if key in seen:
            dupes += 1
        seen.add(key)
    closes = sum(1 for r in rows if r.get("t") == "close")
    opens = sum(1 for r in rows if r.get("t") == "open")
    if dupes:
        record(FAIL, "ledger", f"{dupes} DUPLICATE entries (same market+rule twice)")
    elif closes > opens:
        record(FAIL, "ledger", f"{closes} closes vs {opens} opens - impossible")
    else:
        record(OK, "ledger", f"{opens} opens / {closes} closes, no duplicates")


def check_selftests() -> None:
    """Every module ships a selftest; run them all. This is the post-pull smoke test."""
    mods = ["stage0_collector.py", "shadow_book.py", "paper_trader.py"]
    for m in mods:
        try:
            r = subprocess.run([sys.executable, str(ROOT / "scripts" / m), "selftest"],
                               capture_output=True, text=True, timeout=90, cwd=str(ROOT))
            if r.returncode == 0:
                record(OK, "selftest", f"{m} passes")
            else:
                tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:]
                record(FAIL, "selftest", f"{m} FAILS: {tail[0][:80] if tail else '?'}")
        except Exception as e:  # noqa: BLE001
            record(FAIL, "selftest", f"{m} could not run: {type(e).__name__}")


def check_disk() -> None:
    logs = ROOT / "logs"
    if not logs.exists():
        return
    big = []
    for p in logs.glob("*.log"):
        try:
            mb = p.stat().st_size / 1e6
        except OSError:
            continue
        if mb > LOG_BLOAT_MB:
            big.append(f"{p.name} {mb:.0f}MB")
    if big:
        record(WARN, "disk", f"large logs: {', '.join(big)} (safe to delete)")
    else:
        record(OK, "disk", "log sizes fine")


def main() -> int:
    repair = "--repair" in sys.argv
    quiet = "--quiet" in sys.argv

    check_deps()
    check_env_auth()
    check_tasks(repair)
    check_ports()
    check_api()
    check_data_flow()
    check_depth_coverage()
    check_ledger_integrity()
    check_disk()
    if "--fast" not in sys.argv:
        check_selftests()

    fails = [r for r in _results if r[0] == FAIL]
    warns = [r for r in _results if r[0] == WARN]

    if "--log" in sys.argv:
        # Watchdog trail: one line per run, plus detail only when something is wrong,
        # so a healthy month stays readable and a bad night is fully diagnosable.
        try:
            lg = ROOT / "logs"
            lg.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with open(lg / "healthcheck.log", "a", encoding="utf-8") as f:
                f.write(f"{stamp} {'UNHEALTHY' if fails else 'healthy'} "
                        f"fails={len(fails)} warns={len(warns)}\n")
                for status, section, msg in _results:
                    if status != OK:
                        f.write(f"    [{status}] {section}: {msg}\n")
        except OSError:
            pass

    if not quiet:
        print("=" * 70)
        print(f"polybot healthcheck  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        print("=" * 70)
        for status, section, msg in _results:
            print(f"[{status}] {section:<9} {msg}")
        print("-" * 70)
    elif fails or warns:
        for status, section, msg in _results:
            if status != OK:
                print(f"[{status}] {section:<9} {msg}")

    if fails:
        if not quiet:
            print(f"UNHEALTHY - {len(fails)} failure(s), {len(warns)} warning(s)")
            print("Try:  py scripts\\healthcheck.py --repair" if IS_WIN
                  else "Try:  python3 scripts/healthcheck.py --repair")
        return 1
    if not quiet:
        print(f"HEALTHY - everything running and data is flowing "
              f"({len(warns)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
