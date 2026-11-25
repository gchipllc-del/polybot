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
TASKS = ["PolybotDashboards", "PolybotStage0", "PolybotPaper", "PolybotSleeves"]
QUERY_DONE = "polybot-task-query-done"   # echoed after the task list: PowerShell answered
PORTS = [("crypto dashboard", 5153), ("weather dashboard", 5154), ("command center", 5155)]
STAGE0_LOG = ROOT / "data" / "stage0_crypto.jsonl"
PAPER_LOG = ROOT / "data" / "paper_crypto15.jsonl"
HISTORY_LOG = ROOT / "data" / "stage0_history.jsonl"

# A collector cycle is 60s; anything past 10 minutes means it is not actually working
# even if the process exists. This threshold is the whole point of the file.
STAGE0_STALE_MIN = 10.0
# The crypto paper trader appends a row whenever a bet opens or settles - about one every
# two minutes on the host. 20 quiet minutes is the command center's own bar (a whole
# 15-minute window with neither, plus settlement lag); an hour is thirty usual gaps.
PAPER_QUIET_WARN_MIN = 20.0
PAPER_QUIET_FAIL_MIN = 60.0
PAPER_HB = "paper_crypto_heartbeat.json"   # written by paper_trader after each cycle
PAPER_HB_STALE_MIN = 10.0                  # its cycle is 60s, like the collector's
SLEEVE_HB = "sleeve_paper_heartbeat.json"  # written by sleeve_paper every cycle (2 min)
SLEEVE_HB_STALE_MIN = 10.0
STREAK_FAIL = 3          # consecutive bad cycles, counted by the trader, before a FAIL
LOG_BLOAT_MB = 200.0

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def record(status: str, section: str, msg: str) -> None:
    _results.append((status, section, msg))


def _now() -> float:
    """The clock, in one place - tests move it."""
    return time.time()


def _data(name: str) -> Path:
    return ROOT / "data" / name


def _age_min(p: Path) -> float | None:
    if not p.exists():
        return None
    try:
        return (_now() - p.stat().st_mtime) / 60.0
    except OSError:
        return None


def _read_json(p: Path) -> dict | None:
    """A JSON object from a file, or None when it is missing, torn or not an object."""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _n(d: dict, key: str) -> float | None:
    """A heartbeat number, or None when the key is absent or is not a number. A trader
    that has not been restarted since a pull still writes the older, shorter heartbeat:
    a missing key is UNKNOWN, never a fault."""
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ── what the watchdog remembers between runs ──────────────────────────────────
#
# PolybotHealth is a new process every 15 minutes, and some judgements need the run
# before: "the PC has only just woken up" (after a night asleep every file is as old as
# the night, and the watchdog runs the moment the PC wakes) and "this was already so at
# the last run" (one bad cycle caught by chance is not a fault). So it keeps a small JSON
# file under logs/. The file is advisory, and failing safe is the rule: with no usable
# file there is NO wake-up grace and nothing counts as seen before - never a watchdog
# made blind by its own bookkeeping.

STATE_NAME = "healthcheck_state.json"
CHAIN_GAP_MIN = 40.0     # runs are 15 min apart: a longer silence = the PC was off/asleep
CONFIRM_MIN = 10.0       # "still so at the next run" = first seen at least this long ago

_state: dict = {}        # this run's copy: main() loads it, checks update it, main() saves
_state_ok = False        # this run read the file AND could write it back


def _state_path() -> Path:
    return ROOT / "logs" / STATE_NAME


def _write_state() -> bool:
    p = _state_path()
    try:
        p.parent.mkdir(exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(_state, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, p)               # never a half-written file for the next run
        return True
    except (OSError, TypeError, ValueError):
        return False


def _begin_run() -> None:
    """Load what the previous run left and decide whether this run FOLLOWS it. A gap in
    the watchdog's own 15-minute chain means the PC was off, asleep or logged off: then
    `up_since` is this run (the wake-up, as closely as the watchdog can know it) and
    nothing seen before the gap counts as "the run before". The file is rewritten at
    once: if that fails, this run trusts none of it."""
    global _state, _state_ok
    now = _now()
    st = _read_json(_state_path()) or {}
    last = _n(st, "last_run")
    if not isinstance(st.get("since"), dict):
        st["since"] = {}
    if last is None or abs(now - last) > CHAIN_GAP_MIN * 60:
        st["since"] = {}
        st["up_since"] = now if last is not None else None
    st["last_run"] = now
    _state = st
    _state_ok = _write_state()


def _end_run() -> None:
    """Save what this run saw. A state file that cannot be kept is said out loud: without
    it the graces and the cross-run confirmations silently never apply."""
    global _state_ok
    if _state_ok:
        _state_ok = _write_state()
    if not _state_ok:
        record(WARN, "watchdog", f"cannot keep logs/{STATE_NAME} between runs - no wake-up "
                                 f"grace and no 'still so at the next run' until it can "
                                 f"be written")


def _awake_min() -> float | None:
    """Minutes since the PC is known to have come back - the first run after a gap in
    the watchdog's own chain. None = not known (no usable state file, or no gap since it
    was created): then no grace is given."""
    up = _n(_state, "up_since") if _state_ok else None
    return None if up is None else max(0.0, (_now() - up) / 60.0)


def _since_wake(age_min: float) -> tuple[float, str]:
    """(the minutes to judge, a note for the message). Nothing can have been written
    while the PC was off, so after a night asleep a file is as old as the night - and the
    watchdog runs at once. Its silence is therefore judged from the wake-up, not from the
    last write. With the wake-up unknown the full age is judged."""
    awake = _awake_min()
    if awake is None or awake >= age_min:
        return age_min, ""
    return awake, f"; the PC was off or asleep until {awake:.0f} min ago, judged from then"


def _lasting(key: str, bad: bool) -> float | None:
    """How long a condition has been seen at EVERY run, in minutes: None when it is not
    there now, 0.0 at a first sighting. One run that does not see it starts the count
    again, and so does a gap in the chain. Without a usable state file every sighting is
    a first one."""
    seen = _state.setdefault("since", {})
    if not bad:
        seen.pop(key, None)
        return None
    first = _n(seen, key)
    if first is None or not _state_ok:
        seen[key] = _now()
        return 0.0
    return max(0.0, (_now() - first) / 60.0)


def _forget(prefix: str) -> None:
    """Drop every sighting under a prefix: a run that could not look has not seen it."""
    seen = _state.get("since")
    if isinstance(seen, dict):
        for key in [k for k in seen if k.startswith(prefix)]:
            del seen[key]


def _confirmed(key: str, bad: bool) -> bool:
    """True when `bad` holds now and was already so at a run at least CONFIRM_MIN ago,
    and at every run since. The watchdog reads one cycle out of many: a counter that is
    non-zero in the single cycle it happens to see must not flap the log."""
    lasted = _lasting(key, bad)
    return lasted is not None and lasted >= CONFIRM_MIN


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


def _task_states(names: list[str]) -> tuple[dict[str, str], bool]:
    """({task: state}, answered). PowerShell prints one name=state line per registered
    task and then QUERY_DONE. `answered` is whether that last line arrived: _ps returns ''
    on any failure (its timeout included), and an empty answer is also exactly what four
    unregistered tasks look like - only the marker tells the two apart."""
    out = _ps("Get-ScheduledTask " + ",".join(names) +
              " -ErrorAction SilentlyContinue | ForEach-Object { $_.TaskName + '=' + $_.State }"
              f"; '{QUERY_DONE}'")
    states, answered = {}, False
    for line in out.splitlines():
        line = line.strip()
        if line == QUERY_DONE:
            answered = True
        elif "=" in line:
            k, v = line.split("=", 1)
            states[k.strip()] = v.strip()
    return states, answered


def check_tasks(repair: bool) -> None:
    if not IS_WIN:
        record(WARN, "tasks", "not Windows - skipping scheduled-task check")
        return
    states, answered = _task_states(TASKS)
    if not answered and not states:
        # No answer is not "four tasks missing": that sent the owner to re-run the
        # installer while everything was running. Nothing is known, so nothing is repaired.
        record(WARN, "tasks", "could not query Task Scheduler (PowerShell gave no answer) - "
                              "task states unknown this run, nothing checked or repaired")
        return
    unknown = []
    for t in TASKS:
        st = states.get(t)
        if st is None and not answered:
            unknown.append(t)
        elif st is None:
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
    if unknown:
        record(WARN, "tasks", f"Task Scheduler's answer was cut short - state unknown for "
                              f"{', '.join(unknown)}")


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
        _judge_paper_ledger()
    else:
        record(WARN, "data", "paper ledger empty - no rule has fired yet")

    if HISTORY_LOG.exists():
        record(OK, "data", f"trend history {_rows(HISTORY_LOG)} snapshots")
    else:
        record(WARN, "data", "no trend snapshots yet (written hourly)")


def _judge_paper_ledger() -> None:
    """The crypto ledger's age used to be printed inside an OK line and never judged: a
    trader dead for three days read 'paper ledger 73603 rows (last write 4320 min ago)'
    and the run was logged healthy. The ledger is the WORK - a heartbeat only proves
    cycling, and a trader whose every request fails, or whose slots are all held by
    positions that will never settle, cycles perfectly and books nothing. So this stays
    the judge even when a heartbeat exists; the heartbeat only explains it."""
    prows = _rows(PAPER_LOG)
    age = _age_min(PAPER_LOG)
    if age is None:
        record(WARN, "data", "paper ledger unreadable")
        return
    judged, grace = _since_wake(age)
    if judged <= PAPER_QUIET_WARN_MIN:
        record(OK, "data", f"paper ledger {prows} rows (last write {age:.0f} min ago{grace})")
        return
    silent = judged > PAPER_QUIET_FAIL_MIN
    record(FAIL if silent else WARN, "data",
           f"paper ledger {'SILENT' if silent else 'quiet'} {age:.0f} min{grace} (a row is "
           f"written whenever a bet opens or settles - normally every few minutes) - "
           f"{_paper_trader_story()}")


def _paper_counters(s: dict) -> str:
    """The last cycle's counters, as far as this heartbeat carries them."""
    parts = []
    for key in ("opened", "closed", "open_now", "errors", "ungated"):
        v = _n(s, key)
        if v is not None:
            parts.append(f"{key} {v:.0f}")
    skipped = s.get("skipped")
    if isinstance(skipped, dict) and skipped:
        parts.append("skipped " + " ".join(f"{k}={v}" for k, v in sorted(skipped.items())))
    if s.get("last_error"):
        parts.append(f"last error: {str(s['last_error'])[:90]}")
    return ", ".join(parts) or "its heartbeat carries no counters"


def _paper_trader_story() -> str:
    """Why the crypto ledger is quiet, as far as the trader's heartbeat can say."""
    hb = _data(PAPER_HB)
    s, age = _read_json(hb), _age_min(hb)
    if s is None or age is None:
        return ("the crypto paper trader is not booking anything (it writes no heartbeat "
                "yet, so whether it is cycling at all is unknown)")
    if age > PAPER_HB_STALE_MIN:
        return (f"and its heartbeat is {age:.0f} min old: the crypto paper trader is not "
                f"cycling")
    return (f"the trader IS cycling (heartbeat {age:.0f} min ago) but booking nothing: "
            f"{_paper_counters(s)}")


def check_crypto_paper() -> None:
    """The crypto paper trader's heartbeat (data/paper_crypto_heartbeat.json), written
    after each completed cycle once the trader runs the code that has it. Until then
    there is no file, and that is UNKNOWN, not a fault: the ledger's age in [data] is
    the judge either way. What the heartbeat adds is the difference between "not
    cycling" and "cycling but not working", and the cycle's own error count."""
    hb = _data(PAPER_HB)
    if not hb.exists():
        record(OK, "paper", "no heartbeat file yet - judged by the ledger alone (the "
                            "trader writes one once it runs the current code)")
        return
    s, age = _read_json(hb), _age_min(hb)
    if s is None or age is None:
        record(WARN, "paper", "heartbeat unreadable")
        return
    judged, grace = _since_wake(age)
    if judged > PAPER_HB_STALE_MIN:
        ledger_age = _age_min(PAPER_LOG)
        if ledger_age is not None and ledger_age <= PAPER_HB_STALE_MIN:
            record(WARN, "paper", f"heartbeat {age:.0f} min old but the ledger was written "
                                  f"{ledger_age:.0f} min ago - trading, but its heartbeat "
                                  f"is not being written")
        else:
            record(FAIL, "paper", f"crypto paper heartbeat {age:.0f} min old{grace} (cycle "
                                  f"is 60s) - alive but not cycling")
        return
    if age > PAPER_HB_STALE_MIN:                  # inside the wake-up grace: old news
        record(OK, "paper", f"heartbeat {age:.0f} min old{grace}")
        return
    errors = _n(s, "errors")
    if _confirmed("paper.errors", bool(errors)):
        last = f" (last: {str(s['last_error'])[:90]})" if s.get("last_error") else ""
        record(WARN, "paper", f"requests are failing: {errors:.0f} in the last cycle, and "
                              f"already at the previous check{last}")
    skew = _n(s, "clock_skew_s")
    record(OK, "paper", f"cycling (heartbeat {age:.1f} min ago): {_paper_counters(s)}"
                        + (f", clock {skew:+.1f}s vs Kalshi" if skew is not None else ""))


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


def _defect_counts(whats: list[str]) -> str:
    """'2 duplicate opens, 1 orphan close' from diagnostics-style defect labels."""
    n: dict[str, int] = {}
    for w in whats:
        kind = " ".join(w.split()[:2])
        n[kind] = n.get(kind, 0) + 1
    return ", ".join(f"{c} {k}{'' if c == 1 else 's'}" for k, c in n.items())


def check_ledger_integrity() -> None:
    """Bug 7: the same (market, rule) entered twice would silently double-count P&L.

    RECENT EVIDENCE FAILS, HISTORY INFORMS - diagnostics' rule, and its window: the last
    24 h of the ledger's own timeline, the one B1 judges. The ledger is append-only, so a
    duplicate that failed this check on every run kept the watchdog red for good, and a
    watchdog that is always red cannot report the next outage. A defect from the last
    day FAILs here (and in [model] B1). An older one is a WARN that still counts it,
    because the scored record still carries the row - and a duplicated close counts that
    trade twice."""
    if not PAPER_LOG.exists():
        return
    try:
        import diagnostics as dg
        rows = dg._load(PAPER_LOG)
        start = dg._recent_start(rows)
    except Exception as e:  # noqa: BLE001
        record(FAIL, "ledger", f"unreadable: {type(e).__name__}")
        return
    opened, closed = set(), set()
    opens = closes = 0
    defects: list[tuple[int, str]] = []          # (row index, what) - B1's own kinds
    for i, r in enumerate(rows):
        key = (r.get("ticker"), r.get("rule"))
        if r.get("t") == "open":
            opens += 1
            if key in opened:
                defects.append((i, f"duplicate open {key[0]}/{key[1]}"))
            opened.add(key)
        elif r.get("t") == "close":
            closes += 1
            if key not in opened:
                defects.append((i, f"orphan close {key[0]}/{key[1]}"))
            elif key in closed:
                defects.append((i, f"duplicate close {key[0]}/{key[1]}"))
            closed.add(key)
    recent = [what for i, what in defects if i >= start]
    old = [what for i, what in defects if i < start]
    counts = f"{opens} opens / {closes} closes"
    window = f"{dg.RECENT_HOURS:.0f} h"
    if recent:
        record(FAIL, "ledger", f"{_defect_counts(recent)} in the ledger's last {window} - "
                               f"first: {recent[0]} (two traders on one ledger?); {counts}")
    if old:
        record(WARN, "ledger", f"{_defect_counts(old)} older than {window} still in the "
                               f"scored record (first: {old[0]}) - a duplicated close "
                               f"counts that trade twice; {counts}")
    if not defects:
        record(OK, "ledger", f"{counts}, no duplicates")


def check_model_integrity() -> None:
    """The A/B/C/D layer (scripts/diagnostics.py): input bounds, exact-cents ledger
    conservation, pricing-model identities, edge-case behavior - run against the LIVE
    files every watchdog cycle. A process can be alive, fresh, and serving while the
    ledger quietly stops conserving; this is the check that notices the same day."""
    try:
        import diagnostics as dg
        results = (dg.run_all(dg._load(dg.STAGE0), dg._load(dg.LEDGER))
                   + dg.run_sleeve_ledger(dg._load(dg.SLEEVE_LEDGER)))
    except Exception as e:  # noqa: BLE001
        record(FAIL, "model", f"diagnostics raised {type(e).__name__}: {str(e)[:70]}")
        return
    bad = [(name, detail) for name, ok, detail in results if not ok]
    if bad:
        # 200, not 80: an unattended night's log line is the only record, and the
        # 80-char cut once landed mid-number and printed a WRONG recomputed value.
        # diagnostics puts the offender first, so 200 always keeps it.
        for name, detail in bad:
            record(FAIL, "model", f"{name}: {detail[:200]}")
    else:
        record(OK, "model", f"{len(results)} integrity checks pass "
                            f"(bounds, conservation, identities, edge cases)")


def check_news_regime() -> None:
    """The news agent (lib/news_regime.py) is a SOFT dependency: when it is down the
    paper trader stamps 'unknown' and keeps trading, so this can WARN but never FAIL."""
    try:
        from lib.news_regime import REGIME_LOG, current_regime
    except Exception as e:  # noqa: BLE001
        record(WARN, "news", f"news agent unavailable: {type(e).__name__}")
        return
    if not REGIME_LOG.exists():
        record(WARN, "news", "news agent has never run - rerun install_autostart.ps1 "
                             "(adds the hourly PolybotNews task)")
        return
    r = current_regime(REGIME_LOG)
    try:                       # are the LIVE data layers voting, or only the fallbacks?
        from lib.news_regime import load_history
        votes = ((load_history(REGIME_LOG, max_bytes=200_000) or [{}])[-1]
                 .get("votes") or {})
        silent = {k: (votes.get(k) or {}).get("why", "no vote")
                  for k in ("headlines", "volatility")
                  if (votes.get(k) or {}).get("vote") is None}
    except Exception:  # noqa: BLE001
        silent = {}
    if r["regime"] == "unknown":
        record(WARN, "news", r["why"][:120])
    elif len(silent) == 2:
        record(WARN, "news", f"regime {r['regime'].upper()} but no live data layer voted - "
                             f"headlines: {silent['headlines'][:50]}; "
                             f"volatility: {silent['volatility'][:50]}")
    else:
        record(OK, "news", f"regime {r['regime'].upper()} (size x{r['mult']}) - "
                           f"{r['why'][:80]}")


def check_news_calendar() -> None:
    """config/news_calendar.yaml is the one news input kept by hand, and an entry the
    agent cannot read makes its day look unlisted (FOMC day at full size). So a YAML
    error or an unreadable entry is a WARN here - read from the file now, not from
    the last hourly regime row.

    It also RUNS OUT. A release family (the first word of the event name: Jobs, CPI, PPI,
    FOMC) with no date left from today on means every later release day of that kind is
    stamped like an ordinary one - and 'readable' said nothing about it, in any year."""
    try:
        from lib import news_regime as nr
        events, problems = nr.read_calendar(nr.CALENDAR_PATH)
        today = nr.et_date(nr._utcnow())           # calendar dates are US/Eastern days
    except Exception as e:  # noqa: BLE001
        record(WARN, "news", f"calendar check raised {type(e).__name__}")
        return
    if problems:
        more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
        record(WARN, "news", f"calendar unreadable: {problems[0][:100]}{more} - fix "
                             f"{nr.CALENDAR_PATH.name}")
    else:
        record(OK, "news", f"calendar readable ({len(events)} listed)")
    last: dict[str, str] = {}
    for e in events:
        family = e["event"].split()[0]
        last[family] = max(last.get(family, ""), e["date"])
    ran_out = [f"{family} (last listed {d})"
               for family, d in sorted(last.items(), key=lambda kv: (kv[1], kv[0])) if d < today]
    if ran_out:
        record(WARN, "news", f"calendar ran out for {', '.join(ran_out)} - add the next dates "
                             f"to {nr.CALENDAR_PATH.name} (BLS release schedule for jobs / "
                             f"CPI / PPI, the Fed's calendar for FOMC); until then those "
                             f"days are stamped like ordinary ones")


def check_sleeve_paper() -> None:
    """The multi-sleeve paper trader writes a heartbeat every cycle (2 min). Alive but
    with no rules is a setup step, not a fault - say exactly what to run.

    A fresh heartbeat only proves the loop turns. The trader once went six hours with
    every Kalshi request failing while this line read 'OK ... (cycle 0 min ago)', so
    the heartbeat's own account of the cycle is judged too: a cycle that raised, list
    calls failing for every series, one series failing for cycles on end, settlement
    polls failing, a rule stuck at its open-position cap. Fields a trader running older
    code does not write yet are simply unknown. FAILs need a streak - the watchdog reads
    one cycle in seven, and one bad cycle is not an outage."""
    hb = _data(SLEEVE_HB)
    if not hb.exists():
        _forget("sleeves.")
        record(WARN, "sleeves", "sleeve paper trader has never cycled - rerun "
                                "install_autostart.ps1 (adds PolybotSleeves)")
        return
    age, s = _age_min(hb), _read_json(hb)
    if s is None or age is None:
        _forget("sleeves.")
        record(WARN, "sleeves", "heartbeat unreadable")
        return
    if age > SLEEVE_HB_STALE_MIN:
        _forget("sleeves.")
        record(FAIL, "sleeves", f"sleeve paper heartbeat {age:.0f} min old (cycle is 2 min)"
                                f" - alive but not cycling")
        return
    if s.get("cycle_error"):
        # The trader now writes its heartbeat even when the cycle raised (it used to go
        # stale and read "alive but not cycling" with the cause lost on a hidden console).
        _forget("sleeves.")
        n = _n(s, "failed_cycles")
        why = str(s["cycle_error"])[:140]
        if n is not None and n < STREAK_FAIL:
            record(WARN, "sleeves", f"the last sleeve cycle failed ({n:.0f} in a row): {why}")
        else:
            row = f"{n:.0f} cycles in a row" if n is not None else "the last cycle"
            record(FAIL, "sleeves", f"the sleeve trader's cycle is FAILING ({row}): {why} - "
                                    f"nothing is entered or settled while this lasts")
        return
    rules, series, errors = s.get("rules"), _n(s, "series"), _n(s, "errors")
    streak = _n(s, "all_failed_streak")
    all_now = bool(errors and series and errors >= series)
    # a trader that does not count the streak itself: two watchdog runs in a row instead
    all_seen_twice = _confirmed("sleeves.all_failed", streak is None and all_now)
    if (streak is not None and streak >= STREAK_FAIL) or all_seen_twice:
        how = (f"for {streak:.0f} cycles in a row" if streak is not None
               else "in the last cycle, and already at the previous check")
        record(FAIL, "sleeves", f"EVERY series list call failed {how} - nothing can be "
                                f"entered ({s.get('open_now', '?')} open); is Kalshi "
                                f"reachable from this PC?")
        return
    if not rules:
        record(WARN, "sleeves", "no sleeve rules yet - run: py scripts\\sleeve_scan.py "
                                "discover, then py scripts\\sleeve_paper.py register-probes")
        return
    for warning in _sleeve_cycle_warnings(s):
        record(WARN, "sleeves", warning)
    ug = f", {s['ungated']} ungated" if s.get("ungated") else ""
    record(OK, "sleeves", f"{rules} rules on {s.get('series', '?')} sleeves, "
                          f"{s.get('open_now', 0)} open{ug} (cycle {age:.0f} min ago)")


def _sleeve_cycle_warnings(s: dict) -> list[str]:
    """What the last sleeve cycle says went wrong short of a failure. Per-cycle counters
    (failed polls, skipped markets, failed list calls) must be seen on two runs in a
    row; a streak the trader counted itself, and a full cap, are states already."""
    out = []
    errors, series = _n(s, "errors"), s.get("series", "?")
    streaks = s.get("series_fail_streak")
    stuck = sorted((k for k, v in streaks.items()
                    if isinstance(v, (int, float)) and v >= STREAK_FAIL),
                   key=lambda k: (-streaks[k], k)) if isinstance(streaks, dict) else []
    errors_seen_twice = _confirmed("sleeves.errors", bool(errors))
    if stuck:
        shown = ", ".join(f"{k} x{streaks[k]:.0f}" for k in stuck[:4])
        more = f" (+{len(stuck) - 4} more)" if len(stuck) > 4 else ""
        out.append(f"list calls keep failing for {len(stuck)} of {series} series - cycles "
                   f"in a row: {shown}{more}; their events are not being entered")
    elif errors_seen_twice:
        out.append(f"list calls are failing: {errors:.0f} of {series} series in the last "
                   f"cycle, and already at the previous check")
    polls = _n(s, "poll_errors")
    if _confirmed("sleeves.poll_errors", bool(polls)):
        out.append(f"settlement polls are failing: {polls:.0f} in the last cycle, and "
                   f"already at the previous check - closed positions are not being settled")
    skipped_markets = _n(s, "market_errors")
    if _confirmed("sleeves.market_errors", bool(skipped_markets)):
        out.append(f"{skipped_markets:.0f} market(s) or position(s) were skipped in the last "
                   f"cycle because handling them raised, and already at the previous check")
    skipped = s.get("skipped")
    cap = _n(skipped, "open_cap") if isinstance(skipped, dict) else None
    if cap:
        out.append(f"a rule is at its open-position cap: {cap:.0f} entr"
                   f"{'y' if cap == 1 else 'ies'} refused in the last cycle "
                   f"({s.get('open_now', '?')} open) - results are not arriving, or "
                   f"positions are stranded")
    return out


def check_selftests() -> None:
    """Every module ships a selftest; run them all. This is the post-pull smoke test."""
    mods = ["stage0_collector.py", "shadow_book.py", "paper_trader.py",
            "diagnostics.py", "news_agent.py", "sleeve_scan.py", "sleeve_paper.py",
            "strategy_check.py"]
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


def _safely(check, *args) -> None:
    """One check that raises must not end the run: a heartbeat missing a key it used to
    carry once killed the whole watchdog with a KeyError, and a watchdog that dies logs
    nothing - which reads as 'no news'. The failure is recorded and the rest still run."""
    try:
        check(*args)
    except Exception as e:  # noqa: BLE001
        record(FAIL, "watchdog", f"{check.__name__} raised {type(e).__name__}: "
                                 f"{str(e)[:100]} - that check did not finish")


def main() -> int:
    repair = "--repair" in sys.argv
    quiet = "--quiet" in sys.argv

    _begin_run()
    _safely(check_deps)
    _safely(check_env_auth)
    _safely(check_tasks, repair)
    _safely(check_ports)
    _safely(check_api)
    _safely(check_data_flow)
    _safely(check_crypto_paper)
    _safely(check_depth_coverage)
    _safely(check_ledger_integrity)
    _safely(check_model_integrity)
    _safely(check_news_regime)
    _safely(check_news_calendar)
    _safely(check_sleeve_paper)
    _safely(check_disk)
    if "--fast" not in sys.argv:
        _safely(check_selftests)
    _end_run()

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
