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
  py scripts/healthcheck.py --repair   # also start what is dead and restart what is
                                       # Running but stalled (Windows tasks; see [tasks])
  py scripts/healthcheck.py --quiet    # only print problems (for scheduled runs)

Run after run: the scheduled task is a new process every 15 minutes, so what must be
known from the run before (was the PC asleep? was this already so? was a restart tried?)
is kept in logs/healthcheck_state.json by the scheduled (--log) runs. Without it - and
on a run by hand that follows no scheduled run - the watchdog judges every file's full
age and restarts no running task: it is never made blind by its own bookkeeping.
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
NEWS_TASK = "PolybotNews"                # hourly, runs and exits: 'Ready' is its normal state
TASK_CMD = {                             # what each task runs (install_autostart.ps1)
    "PolybotDashboards": "scripts\\run_dashboards.py",
    "PolybotStage0": "scripts\\stage0_collector.py collect",
    "PolybotPaper": "scripts\\paper_trader.py run",
    "PolybotSleeves": "scripts\\sleeve_paper.py run"}
TASK_LOCK = {                            # the lib.single_instance name each one holds
    "PolybotDashboards": "run_dashboards", "PolybotStage0": "stage0_collector",
    "PolybotPaper": "paper_trader", "PolybotSleeves": "sleeve_paper"}
RESTART_EVERY_MIN = 60.0                 # at most one automatic restart per task per hour
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
SLEEVE_CYCLE_MIN = 2.0
# A position still open this long after its market closed is overdue: a crypto window's
# result normally arrives within minutes, a daily-high result within about nine hours.
PAPER_OVERDUE_MIN = 60.0
SLEEVE_OVERDUE_H = 24.0
STREAK_FAIL = 3          # consecutive bad cycles, counted by the trader, before a FAIL
# The quiet-rule check (see _quiet_rules): a rule with enough recent entries to have a
# rhythm has opened nothing for longer than that rhythm allows.
QUIET_WINDOW_DAYS = 7.0
QUIET_MIN_ENTRIES = 20       # entries in the window before a rule has a rhythm to judge
QUIET_MIN_SPAN_H = 48.0      # ...and they must span two days: one night teaches nothing
QUIET_DAYS_SEEN = 3          # ...on three different days (a weekly burst has no rhythm)
QUIET_FLOOR_H = 2.0
QUIET_MULT = 2.0             # alarm past twice the longest gap seen in the window
QUIET_HOURLY_PER_DAY = 24.0  # a rule making fewer entries a day than this is not hourly:
QUIET_DAILY_FLOOR_H = 26.0   # its events open once a day, so a day (+2 h) can pass in peace
IDLE_KEEP_DAYS = 10.0
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


def _jsonl_rows(p: Path) -> list[dict]:
    """Every JSON-object row of a JSONL file; torn or foreign lines are skipped."""
    rows = []
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except OSError:
        pass
    return rows


def _n(d: dict, key: str) -> float | None:
    """A heartbeat number, or None when the key is absent or is not a number. A trader
    that has not been restarted since a pull still writes the older, shorter heartbeat:
    a missing key is UNKNOWN, never a fault."""
    v = d.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _epoch(iso) -> float | None:
    """An ISO timestamp as epoch seconds, or None."""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


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
_state_kept = False      # this run is one that keeps the file (see _begin_run)


def _state_path() -> Path:
    return ROOT / "logs" / STATE_NAME


def _write_state() -> bool:
    """Write the state file whole or not at all (temp file + replace). One retry: on
    Windows a virus scanner or an open editor can hold the file for a moment."""
    p = _state_path()
    tmp = p.with_name(f"healthcheck_state.tmp{os.getpid()}.json")
    for attempt in (1, 2):
        try:
            p.parent.mkdir(exist_ok=True)
            tmp.write_text(json.dumps(_state, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, p)
            return True
        except (TypeError, ValueError):
            return False
        except OSError:
            if attempt == 1:
                _sleep(0.3)
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def _begin_run(scheduled: bool = True) -> None:
    """Load what the previous run left and decide whether this run FOLLOWS it. A gap in
    the watchdog's own 15-minute chain means the PC was off, asleep or logged off: then
    `up_since` is this run (the wake-up, as closely as the watchdog can know it) and
    nothing seen or tried before the gap counts as "the run before". The file is
    rewritten at once: if that fails, this run trusts none of it.

    Only the SCHEDULED runs (the task's --log runs) make the chain. A run by hand
    shortly after one of them reads and updates the rest of the file but does not move
    the chain; a run by hand with no scheduled run just before it reads and writes
    nothing. Otherwise someone who runs this once a day would look, every time, like a
    PC that has just woken up - and the grace would never end."""
    global _state, _state_ok, _state_kept
    now = _now()
    st = _read_json(_state_path()) or {}
    last = _n(st, "last_run")
    follows = last is not None and abs(now - last) <= CHAIN_GAP_MIN * 60
    if not scheduled and not follows:
        _state, _state_ok, _state_kept = {}, False, False
        return
    if not isinstance(st.get("since"), dict):
        st["since"] = {}
    _note_idle(st, now, now)             # (only tidies whatever the file held)
    if not follows:
        st["since"] = {}
        st["repairs"] = {}               # what dies after a night asleep is a new incident
        st["up_since"] = now if last is not None else None
        if last is not None and last < now:
            _note_idle(st, last, now)    # no watchdog = no PC = no trader either
    if scheduled:
        st["last_run"] = now
    _state, _state_kept = st, True
    _state_ok = _write_state()


def _note_idle(st: dict, t0: float, t1: float) -> None:
    """Remember that the sleeve trader was NOT cycling from t0 to t1 (epoch seconds), as
    far as the watchdog could see: the PC was off (a gap in the watchdog's own chain), or
    the heartbeat was stale, or the cycle kept failing. Kept merged and sorted, and only
    for IDLE_KEEP_DAYS - the quiet-rule check subtracts these spans, so an outage does
    not pass for the rule's own rhythm."""
    spans = [[float(a), float(b)] for a, b in
             (x for x in st.get("sleeve_idle") or [] if isinstance(x, list) and len(x) == 2)
             if isinstance(a, (int, float)) and isinstance(b, (int, float))]
    if t1 > t0:
        spans.append([float(t0), float(t1)])
    merged: list[list[float]] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    st["sleeve_idle"] = [x for x in merged if x[1] >= t1 - IDLE_KEEP_DAYS * 86400]


def _end_run() -> None:
    """Save what this run saw. A state file that cannot be kept is said out loud: without
    it the graces and the cross-run confirmations silently never apply."""
    global _state_ok
    if _state_ok:
        _state_ok = _write_state()
    if _state_kept and not _state_ok:
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


def _sleep(seconds: float) -> None:
    """A pause - one seam, so tests do not wait."""
    time.sleep(seconds)


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


# ── [tasks]: is each task running, and is --repair allowed to touch it ────────
#
# "Running" is Task Scheduler's word for "the launcher process exists". The repair this
# file was first given - start a task that is NOT Running - could not help with the
# outage it names in its own messages ("alive but not collecting"): a request that never
# returns leaves the task Running for ever, and the installer's 10-minute repeat trigger
# is a no-op while it does (-MultipleInstances IgnoreNew). So a Running task whose bot
# has stopped working is restarted too - carefully, because a restart can hurt:
#   * never on one look. After a night asleep every file is as old as the night and the
#     watchdog runs at once; an exchange pause lasts hours. The evidence must be stale at
#     two runs with NOTHING written in between, and then at most once an hour per task.
#   * never without the single-instance lock. Stop-ScheduledTask ends the launcher; a
#     python.exe that survives it would trade next to the new copy and double-book an
#     append-only ledger for good. Only a bot that holds lib.single_instance's lock is
#     restarted: whatever survives the stop keeps the lock and the new copy stands down.
#   * never claimed. After any start the state is asked for again, attempts are counted
#     across runs, and the second attempt in a row is a FAIL, not another "REPAIRED".

def _lock_held(name: str, windows: bool | None = None, msvcrt_mod=None) -> bool | None:
    """Does a live process hold lib.single_instance's lock for this job? True - yes.
    False - nobody does (the bot started before the lock existed, or its locking failed
    open). None - cannot tell. The probe takes the lock for an instant when it is free
    and gives it straight back. (windows / msvcrt_mod are test seams, as in the lib.)"""
    try:
        from lib import single_instance as si
        path = si.lock_dir() / f"{name}.lock"
        if not path.exists():
            return False
        fd = os.open(str(path), os.O_RDWR)
    except Exception:  # noqa: BLE001
        return None
    try:
        if not si._lock_fd(fd, windows=windows, msvcrt_mod=msvcrt_mod):
            return True
        if (os.name == "nt") if windows is None else windows:
            if msvcrt_mod is None:
                import msvcrt as msvcrt_mod      # noqa: PLC0415 - Windows only
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt_mod.locking(fd, msvcrt_mod.LK_UNLCK, 1)
        return False                             # (elsewhere closing the file unlocks it)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _lock_holder(name: str) -> str:
    try:
        from lib import single_instance as si
        pid = int((si.lock_dir() / f"{name}.pid").read_text(encoding="utf-8").strip())
        return f"a process (pid {pid})"
    except Exception:  # noqa: BLE001
        return "another process"


def _evidence(task: str) -> tuple[bool, str, float] | None:
    """(stale, what, when it last wrote) - a loop bot's own proof that it is working, or
    None when there is nothing to judge it by. A bot is stale only when NONE of its
    files is fresh: a trader that books rows is working whatever its heartbeat says."""
    def age_of(p: Path) -> float:
        a = _age_min(p)
        return float("inf") if a is None else a

    def too_old(age: float, limit: float) -> bool:
        return _since_wake(age)[0] > limit       # silence counts from the wake-up
    now = _now()
    if task == "PolybotStage0":
        age = age_of(STAGE0_LOG)
        return None if age == float("inf") else (
            too_old(age, STAGE0_STALE_MIN), f"stage0 log {age:.0f} min old", now - age * 60)
    if task == "PolybotSleeves":
        age = age_of(_data(SLEEVE_HB))
        return None if age == float("inf") else (
            too_old(age, SLEEVE_HB_STALE_MIN), f"heartbeat {age:.0f} min old", now - age * 60)
    if task == "PolybotPaper":
        hb, ledger = age_of(_data(PAPER_HB)), age_of(PAPER_LOG)
        newest = min(hb, ledger)
        if newest == float("inf"):
            return None
        if hb != float("inf"):                   # it writes a heartbeat: 10 quiet minutes
            return (too_old(newest, PAPER_HB_STALE_MIN), f"heartbeat {hb:.0f} min old",
                    now - newest * 60)
        return (too_old(ledger, PAPER_QUIET_FAIL_MIN),   # no heartbeat yet: the ledger only
                f"ledger silent {ledger:.0f} min, no heartbeat", now - ledger * 60)
    return None


def _working(task: str) -> bool:
    """Is this task's bot demonstrably doing its job right now?"""
    if task == "PolybotDashboards":
        return all(_listening(port) for _, port in PORTS)
    ev = _evidence(task)
    return ev is not None and not ev[0]


def _repairs() -> dict:
    reps = _state.get("repairs")
    if not isinstance(reps, dict):
        reps = _state["repairs"] = {}
    return reps


def _since_repair_min(task: str) -> float | None:
    rec = _repairs().get(task)
    t = _n(rec, "t") if isinstance(rec, dict) else None
    return None if t is None or not _state_ok else max(0.0, (_now() - t) / 60.0)


def _count_repair(task: str) -> int:
    """Record a repair attempt and return its number IN A ROW. The row ends only when
    the task has then stayed healthy for a full hour (_rested) - a bot that starts,
    crashes a moment later and is 'repaired' again every 15 minutes is not repaired."""
    rec = _repairs().get(task)
    n = (int(_n(rec, "n") or 0) if isinstance(rec, dict) and _state_ok else 0) + 1
    _repairs()[task] = {"t": _now(), "n": n}
    return n


def _rested(task: str) -> None:
    since = _since_repair_min(task)
    if since is None or since >= RESTART_EVERY_MIN:
        _repairs().pop(task, None)


def _state_after_start(task: str) -> str | None:
    """The task's state once a start has had a moment to take - asked up to three
    times, two seconds apart (Task Scheduler reports 'Queued' for an instant)."""
    st = None
    for i in range(3):
        if i:
            _sleep(2.0)
        st = _task_states([task])[0].get(task)
        if st is not None and st.lower() == "running":
            break
    return st


def _by_hand(task: str) -> str:
    return f"run it by hand to see why: py {TASK_CMD[task]}" if task in TASK_CMD else \
        "start it by hand to see why"


def _running_task(t: str, repair: bool) -> None:
    ev = _evidence(t)
    key = f"stalled.{t}"
    if ev is None or not ev[0]:
        _lasting(key, False)
        _rested(t)
        record(OK, "tasks", f"{t} running")
        return
    _, what, written = ev
    first = _n(_state.get("since") or {}, key)
    if first is not None and written > first:
        _forget(key)                     # it has written since the first look: slow, not stalled
    lasted = _lasting(key, True) or 0.0
    stalled = f"{t} is Running but its bot looks stalled ({what})"
    if not repair:
        record(WARN, "tasks", f"{stalled} - the scheduled watchdog (--repair) restarts it "
                              f"when nothing is written between two of its runs")
        return
    if not _state_ok:
        record(WARN, "tasks", f"{stalled} - NOT restarted on a single look: one slow cycle "
                              f"cannot be told from a stall. The scheduled watchdog "
                              f"restarts it when nothing is written between two of its "
                              f"runs; by hand: Stop-ScheduledTask {t}; Start-ScheduledTask {t}")
        return
    lock = TASK_LOCK.get(t)
    if lock and _lock_held(lock) is not True:
        record(WARN, "tasks", f"{stalled} and is NOT restarted automatically: its process "
                              f"does not hold the single-instance lock (it started before "
                              f"the lock existed), so a restart could leave two copies on "
                              f"one ledger. Once, by hand: Stop-ScheduledTask {t}; check "
                              f"Task Manager for a leftover python.exe; Start-ScheduledTask {t}")
        return
    if lasted < CONFIRM_MIN:
        record(WARN, "tasks", f"{stalled} - it is restarted if nothing has been written by "
                              f"the next run")
        return
    since = _since_repair_min(t)
    if since is not None and since < RESTART_EVERY_MIN:
        record(WARN, "tasks", f"{t} was restarted {since:.0f} min ago and is still stalled "
                              f"({what}) - at most one restart an hour, the next in "
                              f"{RESTART_EVERY_MIN - since:.0f} min")
        return
    _ps(f"Stop-ScheduledTask -TaskName {t}")
    for _ in range(5):                   # let Windows end the process and free its lock
        _sleep(2.0)
        if not (lock and _lock_held(lock)):
            break
    else:
        # Still held ten seconds after the stop: the old python.exe outlived it. A start
        # now would only stand down at the lock - say who is in the way instead.
        n = _count_repair(t)
        record(FAIL, "tasks", f"{t} was Running but stalled ({what}); it was stopped, but "
                              f"{_lock_holder(lock)} still holds its single-instance lock - "
                              f"the old process outlived the stop. End it in Task Manager; "
                              f"the task is started at the next run (attempt {n})")
        return
    _ps(f"Start-ScheduledTask -TaskName {t}")
    after = _state_after_start(t)
    n = _count_repair(t)
    if after is not None and after.lower() == "running":
        if n == 1:
            record(WARN, "tasks", f"{t} was Running but stalled ({what}, nothing written "
                                  f"since the last run) - RESTARTED (stop + start), now "
                                  f"Running")
        else:
            record(FAIL, "tasks", f"{t} is still stalled ({what}) after a restart - "
                                  f"restarted again (attempt {n} in a row), but restarting "
                                  f"does not cure it; {_by_hand(t)}")
    elif after is None and n == 1:
        record(WARN, "tasks", f"{t} was Running but stalled ({what}) - stop + start sent, "
                              f"but Task Scheduler did not answer afterwards: not confirmed")
    else:
        record(FAIL, "tasks", f"{t} was Running but stalled ({what}); after stop + start it "
                              f"is '{after or 'unknown'}', not Running - REPAIR FAILED "
                              f"(attempt {n}); {_by_hand(t)}")


def _dead_task(t: str, st: str) -> None:
    """A task that is not Running, with --repair: start it - and say what happened."""
    _lasting(f"stalled.{t}", False)
    lock = TASK_LOCK.get(t)
    if lock and _lock_held(lock):
        # The task is down but its bot is alive: started by hand, or a python.exe that
        # outlived a stop. A start would only stand down at the lock (and, for a bot
        # from before the lock, would have been a second copy).
        who = _lock_holder(lock)
        if _working(t):
            record(WARN, "tasks", f"{t} is '{st}', but its bot is running outside Task "
                                  f"Scheduler ({who} holds its lock and is working) - "
                                  f"nothing to repair; end that process to hand the bot "
                                  f"back to the task")
        else:
            record(FAIL, "tasks", f"{t} is '{st}' and cannot be started: {who} still holds "
                                  f"its single-instance lock and is not working - end that "
                                  f"process (Task Manager); the task is started at the "
                                  f"next run")
        return
    since = _since_repair_min(t)
    _ps(f"Start-ScheduledTask -TaskName {t}")
    after = _state_after_start(t)
    n = _count_repair(t)
    up = after is not None and after.lower() == "running"
    if up and n == 1:
        record(WARN, "tasks", f"{t} was '{st}' (dead) - REPAIRED, restarted")
    elif up:
        ago = f"{since:.0f} min after the last start" if since is not None else "again"
        record(FAIL, "tasks", f"{t} was '{st}' (dead) {ago} - started once more (attempt "
                              f"{n} in a row), but it is not staying up; {_by_hand(t)}")
    elif n == 1 and _state_ok:
        record(WARN, "tasks", f"{t} was '{st}' (dead) - start sent, but it is still "
                              f"'{after or 'unknown'}': NOT repaired (attempt 1), trying "
                              f"again at the next run")
    else:
        record(FAIL, "tasks", f"{t} is '{after or 'unknown'}' and will not start (attempt "
                              f"{n} in a row) - REPAIR FAILED; {_by_hand(t)}, or re-run "
                              f"install_autostart.ps1")


def _regime_age_h() -> float | None:
    """Hours since the news agent last wrote a regime row, or None when it never has."""
    try:
        from lib import news_regime as nr
        hist = nr.load_history(nr.REGIME_LOG, max_bytes=200_000)
        t = _epoch(hist[-1].get("ts")) if hist else None
    except Exception:  # noqa: BLE001
        return None
    return None if t is None else (_now() - t) / 3600.0


def _news_task(st: str | None, repair: bool) -> None:
    """PolybotNews runs once an hour and exits: 'Ready' is its normal state, and it is
    never started from here. A run that HANGS is different - the task stays Running, the
    hourly trigger is ignored while it does, and the regime goes stale for good. So:
    Running with a stale regime at two runs in a row = stop it and start it, at most
    once an hour. The news agent is a soft dependency (entries are stamped 'unknown'
    and trading goes on), so this only ever WARNs."""
    key = f"stalled.{NEWS_TASK}"
    age = _regime_age_h() if st is not None and st.lower() == "running" else None
    try:
        from lib.news_regime import STALE_AFTER_H
    except Exception:  # noqa: BLE001
        STALE_AFTER_H = 3.0
    hung = age is not None and age > STALE_AFTER_H
    lasted = _lasting(key, hung)
    if not hung:
        _rested(NEWS_TASK)
        return
    since = _since_repair_min(NEWS_TASK)
    if not repair or not _state_ok or (lasted or 0.0) < CONFIRM_MIN or \
            (since is not None and since < RESTART_EVERY_MIN):
        return                           # [news] already reports the stale regime
    _ps(f"Stop-ScheduledTask -TaskName {NEWS_TASK}")
    _sleep(2.0)
    _ps(f"Start-ScheduledTask -TaskName {NEWS_TASK}")
    n = _count_repair(NEWS_TASK)
    again = f" (attempt {n} in a row - it keeps hanging)" if n >= 2 else ""
    record(WARN, "tasks", f"{NEWS_TASK} has been Running with the regime {age:.1f} h old - "
                          f"a hung run blocks every later hourly start; stopped and "
                          f"started it{again}")


def check_tasks(repair: bool) -> None:
    if not IS_WIN:
        record(WARN, "tasks", "not Windows - skipping scheduled-task check")
        return
    states, answered = _task_states(TASKS + [NEWS_TASK])
    if not answered and not states:
        # No answer is not "four tasks missing": that sent the owner to re-run the
        # installer while everything was running. Nothing is known, so nothing is repaired.
        lasted = _lasting("tasks.no_answer", True)
        again = f" (no answer for {lasted:.0f} min now)" if lasted else ""
        record(WARN, "tasks", f"could not query Task Scheduler (PowerShell gave no answer)"
                              f"{again} - task states unknown this run, nothing checked or "
                              f"repaired")
        return
    _lasting("tasks.no_answer", False)
    unknown = []
    for t in TASKS:
        st = states.get(t)
        if st is None and not answered:
            unknown.append(t)
        elif st is None:
            record(FAIL, "tasks", f"{t} NOT REGISTERED - run install_autostart.ps1")
        elif st.lower() == "running":
            _running_task(t, repair)
        elif repair:
            _dead_task(t, st)
        else:
            record(FAIL, "tasks", f"{t} is '{st}', not Running "
                                  f"(self-heal fires within 10 min, or --repair now)")
    if unknown:
        record(WARN, "tasks", f"Task Scheduler's answer was cut short - state unknown for "
                              f"{', '.join(unknown)}")
    _news_task(states.get(NEWS_TASK), repair)


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
                "yet, so whether it is cycling at all is unknown)" if not hb.exists() else
                "the crypto paper trader is not booking anything (and its heartbeat is "
                "unreadable)")
    if age > PAPER_HB_STALE_MIN:
        return (f"and its heartbeat is {age:.0f} min old: the crypto paper trader is not "
                f"cycling")
    return (f"the trader IS cycling (heartbeat {age:.0f} min ago) but booking nothing: "
            f"{_paper_counters(s)}")


def _overdue_said(hb: dict | None) -> tuple[int, str | None, float | None] | None:
    """(positions overdue, the oldest one's ticker, its hours past close) as the trader
    counts them in its own heartbeat - or None when this heartbeat does not say (no
    file, or a trader still on the code from before it counted them)."""
    o = hb.get("overdue") if hb else None
    n = _n(o, "n") if isinstance(o, dict) else None
    if n is None:
        return None
    return int(n), (str(o["oldest"]) if o.get("oldest") else None), _n(o, "oldest_h")


def _overdue_in(live, limit_s: float) -> tuple[int, str | None, float | None]:
    """The same three numbers from the ledger: live = [(ticker, close epoch)]."""
    now = _now()
    late = sorted((close, tk) for tk, close in live if now - close > limit_s)
    if not late:
        return 0, None, None
    return len(late), late[0][1], (now - late[0][0]) / 3600.0


def _overdue(hb: dict | None, key: str, from_ledger) -> tuple[int, str | None, float | None]:
    """Overdue positions from the heartbeat when it counts them, else from the ledger -
    then only once seen at two runs in a row: on the run that follows a wake-up every
    open position is as late as the night, and the trader settles them a minute later."""
    said = _overdue_said(hb)
    if said is not None:
        _lasting(key, False)
        return said
    try:
        found = from_ledger()
    except Exception:  # noqa: BLE001 - an unreadable ledger is another check's finding
        found = (0, None, None)
    return found if _confirmed(key, found[0] > 0) else (0, None, None)


def _warn_crypto_overdue(hb: dict | None) -> None:
    """Positions the settle step never closes are invisible everywhere else: after a long
    outage their markets drop off the page the trader settles from, and each one holds a
    position slot for good - at eight of them the trader books nothing at all."""
    n, oldest, hours = _overdue(
        hb, "paper.overdue",
        lambda: _overdue_in(_scan_crypto_ledger()["live"] if PAPER_LOG.exists() else [],
                            PAPER_OVERDUE_MIN * 60))
    if n:
        which = f" (oldest {oldest}, {hours:.1f} h)" if oldest and hours is not None else ""
        record(WARN, "paper", f"{n} open position{'' if n == 1 else 's'} more than "
                              f"{PAPER_OVERDUE_MIN:.0f} min past {'its' if n == 1 else 'their'}"
                              f" window{which} - not settled, and each holds one of the "
                              f"trader's position slots")


def check_crypto_paper() -> None:
    """The crypto paper trader's heartbeat (data/paper_crypto_heartbeat.json), written
    after each completed cycle once the trader runs the code that has it. Until then
    there is no file, and that is UNKNOWN, not a fault: the ledger's age in [data] is
    the judge either way. What the heartbeat adds is the difference between "not
    cycling" and "cycling but not working", and the cycle's own error count."""
    hb = _data(PAPER_HB)
    s, age = _read_json(hb), _age_min(hb)
    if s is None or age is None or age > PAPER_HB_STALE_MIN:
        _forget("paper.errors")                   # no fresh cycle to read a count from
    if not hb.exists():
        _warn_crypto_overdue(None)
        record(OK, "paper", "no heartbeat file yet - judged by the ledger alone (the "
                            "trader writes one once it runs the current code)")
        return
    if s is None or age is None:
        _warn_crypto_overdue(None)
        record(WARN, "paper", "heartbeat unreadable")
        return
    judged, grace = _since_wake(age)
    if judged > PAPER_HB_STALE_MIN:
        ledger_age = _age_min(PAPER_LOG)
        if ledger_age is not None and ledger_age <= PAPER_HB_STALE_MIN:
            _warn_crypto_overdue(None)
            record(WARN, "paper", f"heartbeat {age:.0f} min old but the ledger was written "
                                  f"{ledger_age:.0f} min ago - trading, but its heartbeat "
                                  f"is not being written")
        else:
            record(FAIL, "paper", f"crypto paper heartbeat {age:.0f} min old{grace} (cycle "
                                  f"is 60s) - alive but not cycling")
        return
    if age > PAPER_HB_STALE_MIN:                  # inside the wake-up grace: old news
        _warn_crypto_overdue(None)
        record(OK, "paper", f"heartbeat {age:.0f} min old{grace}")
        return
    errors = _n(s, "errors")
    if _confirmed("paper.errors", bool(errors)):
        last = f" (last: {str(s['last_error'])[:90]})" if s.get("last_error") else ""
        record(WARN, "paper", f"requests are failing: {errors:.0f} in the last cycle, and "
                              f"already at the previous check{last}")
    _warn_crypto_overdue(s)
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


_scan_memo: dict = {}


def _scan_crypto_ledger() -> dict:
    """ONE pass over the crypto paper ledger for the two checks that need it ([ledger]
    and [paper]); the second caller gets the first one's result while the file is
    unchanged. Only counts and the still-open positions are kept, not the rows."""
    st = PAPER_LOG.stat()
    key = (str(PAPER_LOG), st.st_size, st.st_mtime_ns)
    if _scan_memo.get("key") != key:
        import diagnostics as dg
        rows = dg._load(PAPER_LOG)
        start = dg._recent_start(rows)
        opened: dict[tuple, dict] = {}
        closed: set = set()
        opens = closes = 0
        defects: list[tuple[int, str]] = []      # (row index, what) - B1's own kinds
        for i, r in enumerate(rows):
            k = (r.get("ticker"), r.get("rule"))
            if r.get("t") == "open":
                opens += 1
                if k in opened:
                    defects.append((i, f"duplicate open {k[0]}/{k[1]}"))
                opened[k] = r
            elif r.get("t") == "close":
                closes += 1
                if k not in opened:
                    defects.append((i, f"orphan close {k[0]}/{k[1]}"))
                elif k in closed:
                    defects.append((i, f"duplicate close {k[0]}/{k[1]}"))
                closed.add(k)
        live = []                                # (ticker, when its window closed)
        for k, r in opened.items():
            t, mins = _epoch(r.get("ts")), _n(r, "mins_left")
            if k not in closed and t is not None and mins is not None:
                live.append((str(k[0]), t + mins * 60.0))
        _scan_memo.clear()
        _scan_memo.update(key=key, out={
            "opens": opens, "closes": closes, "window_h": dg.RECENT_HOURS, "live": live,
            "recent": [what for i, what in defects if i >= start],
            "old": [what for i, what in defects if i < start]})
    return _scan_memo["out"]


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
        scan = _scan_crypto_ledger()
    except Exception as e:  # noqa: BLE001
        record(FAIL, "ledger", f"unreadable: {type(e).__name__}")
        return
    recent, old = scan["recent"], scan["old"]
    counts = f"{scan['opens']} opens / {scan['closes']} closes"
    window = f"{scan['window_h']:.0f} h"
    if recent:
        record(FAIL, "ledger", f"{_defect_counts(recent)} in the ledger's last {window} - "
                               f"first: {recent[0]} (two traders on one ledger?); {counts}")
    if old:
        record(WARN, "ledger", f"{_defect_counts(old)} older than {window} still in the "
                               f"scored record (first: {old[0]}) - a duplicated close "
                               f"counts that trade twice; {counts}")
    if not recent and not old:
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
        _sleeve_idle_for(age)
        record(FAIL, "sleeves", f"sleeve paper heartbeat {age:.0f} min old (cycle is 2 min)"
                                f" - alive but not cycling")
        return
    if s.get("cycle_error"):
        # The trader now writes its heartbeat even when the cycle raised (it used to go
        # stale and read "alive but not cycling" with the cause lost on a hidden console).
        _forget("sleeves.")
        n = _n(s, "failed_cycles")
        _sleeve_idle_for((n or 1) * SLEEVE_CYCLE_MIN)
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
    if streak or all_now:                         # cycling, but nothing could be listed
        _sleeve_idle_for(streak * SLEEVE_CYCLE_MIN if streak else
                         (_lasting("sleeves.all_failed", True) or 0.0) + SLEEVE_CYCLE_MIN)
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
    for warning in _sleeve_cycle_warnings(s) + _sleeve_ledger_warnings(s):
        record(WARN, "sleeves", warning)
    ug = f", {s['ungated']} ungated" if s.get("ungated") else ""
    record(OK, "sleeves", f"{rules} rules on {s.get('series', '?')} sleeves, "
                          f"{s.get('open_now', 0)} open{ug} (cycle {age:.0f} min ago)")


def _sleeve_idle_for(minutes: float) -> None:
    """The sleeve trader has not been trading for the last `minutes`."""
    now = _now()
    _note_idle(_state, now - minutes * 60.0, now)


def _trading_s(t0: float, t1: float, idle) -> float:
    """Seconds between t0 and t1 not covered by an idle span (idle: merged, sorted)."""
    total = t1 - t0
    for a, b in idle:
        lo, hi = max(a, t0), min(b, t1)
        if hi > lo:
            total -= hi - lo
    return max(0.0, total)


def _quiet_rules(rows: list[dict], rules, now: float,
                 idle=()) -> list[tuple[str, float, float, float]]:
    """[(rule, hours quiet, alarm limit in hours, longest recent gap in hours)] for every
    rule that has gone quiet for longer than its own recent rhythm allows.

    The one check that does not depend on the cause. A replaced series ticker, a fill
    gate refusing every candidate, a price band nothing reaches: each leaves the trader
    cycling with errors=0 and a clean heartbeat, and the only trace is a rule that has
    stopped opening positions.

    A rule is judged once it has a rhythm: QUIET_MIN_ENTRIES entries in the last
    QUIET_WINDOW_DAYS, spanning QUIET_MIN_SPAN_H and QUIET_DAYS_SEEN different days.

        alarm when  quiet > max(floor, QUIET_MULT x its longest gap in the window)

    floor is QUIET_FLOOR_H for a rule that enters at least hourly on average, and
    QUIET_DAILY_FLOOR_H for one that does not: its events open once a day, and on a day
    when every entry comes at the open the next one is a full day away.

    Gaps and the quiet time are counted in TRADING time: `idle` spans (the PC off, the
    trader not cycling) are subtracted, so an overnight outage neither raises the alarm
    nor passes for the rule's own longest gap for the week after.

    This is a WARN and never a FAIL: it is a statistic about a rule's habits. The first
    prototype (1.5 x the longest gap, a 2 h floor, nothing else) raised a false alarm in
    about 1 watchdog run in 80 on simulated healthy weeks and went blind for a week
    after a 10-hour outage; this version raised none in 16,554 simulated runs and still
    named a dead hourly rule 1.2-1.3 h after the fault began."""
    by_rule: dict[str, list[float]] = {name: [] for name in rules}
    for r in rows:
        if r.get("t") == "open" and r.get("rule") in by_rule:
            t = _epoch(r.get("ts"))
            if t is not None and now - QUIET_WINDOW_DAYS * 86400 <= t <= now:
                by_rule[r["rule"]].append(t)
    out = []
    for name, ts in sorted(by_rule.items()):
        ts.sort()
        span = _trading_s(ts[0], now, idle) if ts else 0.0
        if len(ts) < QUIET_MIN_ENTRIES or span < QUIET_MIN_SPAN_H * 3600 or \
                len({int(t // 86400) for t in ts}) < QUIET_DAYS_SEEN:
            continue
        hourly = len(ts) / (span / 86400.0) >= QUIET_HOURLY_PER_DAY
        floor = QUIET_FLOOR_H if hourly else QUIET_DAILY_FLOOR_H
        gap = max(_trading_s(a, b, idle) for a, b in zip(ts, ts[1:]))
        limit = max(floor * 3600, QUIET_MULT * gap)
        quiet = _trading_s(ts[-1], now, idle)
        if quiet > limit:
            out.append((name, quiet / 3600, limit / 3600, gap / 3600))
    return out


def _sleeve_live(rows: list[dict]) -> list[tuple[str, float]]:
    """[(ticker, close epoch)] of the sleeve positions opened and not yet closed."""
    opened = {(r.get("ticker"), r.get("rule")): r for r in rows if r.get("t") == "open"}
    for r in rows:
        if r.get("t") == "close":
            opened.pop((r.get("ticker"), r.get("rule")), None)
    return [(str(k[0]), close) for k, r in opened.items()
            if (close := _epoch(r.get("close_time"))) is not None]


def _sleeve_ledger_warnings(hb: dict) -> list[str]:
    """What only the sleeve LEDGER shows - no heartbeat field can."""
    try:
        import sleeve_paper as sp
        rules = sp.active_rules(_data("sleeve_rules.jsonl"))     # the registry's own reader
        rows = _jsonl_rows(_data("paper_sleeves.jsonl"))
    except Exception as e:  # noqa: BLE001
        return [f"sleeve ledger not checked: {type(e).__name__}: {str(e)[:80]}"]
    out = []
    for name, quiet, limit, gap in _quiet_rules(rows, rules, _now(),
                                                _state.get("sleeve_idle") or ()):
        out.append(f"{name} has opened no position for {quiet:.1f} h - its longest gap in "
                   f"the last {QUIET_WINDOW_DAYS:.0f} days was {gap:.1f} h (alarm past "
                   f"{limit:.1f} h). The trader is cycling, so look at what it is offered: "
                   f"the series' listings, the price band, the fill gate, the position cap")
    # A market that never reports yes or no (voided, settled as a scalar, a ticker the
    # endpoint stops serving) leaves its position open for good: polled every cycle,
    # holding one of its rule's cap slots, errors=0 throughout.
    n, oldest, hours = _overdue(hb, "sleeves.overdue",
                                lambda: _overdue_in(_sleeve_live(rows), SLEEVE_OVERDUE_H * 3600))
    if n:
        which = f" (oldest {oldest}, {hours:.0f} h)" if oldest and hours is not None else ""
        out.append(f"{n} open position{'' if n == 1 else 's'} more than "
                   f"{SLEEVE_OVERDUE_H:.0f} h past {'its' if n == 1 else 'their'} close with "
                   f"no result{which} - still polled every cycle, and each holds a slot of "
                   f"its rule's position cap")
    # The fill gate admits a candidate whose order-book lookup FAILED, and counts it
    # "ungated" - in that one cycle's heartbeat. Sleeve verdicts use every trade, so each
    # of these is a quote nobody may have been able to fill, inside a rule's record.
    now = _now()
    last_day = [r for r in rows if r.get("t") == "open"
                and now - 86400 <= (_epoch(r.get("ts")) or 0.0) <= now]
    unchecked: dict[str, int] = {}
    for r in last_day:
        if r.get("depth") is None:
            unchecked[str(r.get("rule"))] = unchecked.get(str(r.get("rule")), 0) + 1
    if unchecked:
        per_rule = ", ".join(f"{k} {v}" for k, v in sorted(unchecked.items(),
                                                        key=lambda kv: (-kv[1], kv[0])))
        ever = [r for r in rows if r.get("t") == "open"]
        out.append(f"{sum(unchecked.values())} of the {len(last_day)} positions opened in "
                   f"the last 24 h were booked without a fill check (depth null: {per_rule})"
                   f" - their order-book lookup failed or was not made, see [auth]; in the "
                   f"whole ledger: {sum(r.get('depth') is None for r in ever)} of {len(ever)}")
    return out


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

    _begin_run(scheduled="--log" in sys.argv)
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
