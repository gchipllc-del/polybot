"""healthcheck - the watchdog's judgements, each one pinned.

The watchdog is the layer that is supposed to notice when everything else has gone
quiet, so every test here is a scenario in which it once said the wrong thing. Windows
Task Scheduler is faked through the module's own seams (hc.IS_WIN, hc._ps); nothing here
starts PowerShell.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import healthcheck as hc  # noqa: E402

ALL_RUNNING = {t: "Running" for t in hc.TASKS}


class FakeScheduler:
    """Stands in for PowerShell behind hc._ps: answers Get-ScheduledTask from `states`,
    applies Start-/Stop-ScheduledTask, echoes a quoted literal the way PowerShell does,
    and records every command it was sent."""

    def __init__(self, states, start_works=True, stop_works=True, answers=True):
        self.states = dict(states)
        self.start_works, self.stop_works, self.answers = start_works, stop_works, answers
        self.commands = []

    def __call__(self, cmd, timeout=20):
        self.commands.append(cmd)
        if not self.answers:              # a timeout or an error: _ps returns ""
            return ""
        out = []
        for part in (p.strip() for p in cmd.split(";")):
            if part.startswith("Get-ScheduledTask"):
                names = part.split()[1].split(",")
                out += [f"{n}={self.states[n]}" for n in names if n in self.states]
            elif part.startswith("Start-ScheduledTask"):
                name = part.split()[-1]
                if self.start_works and name in self.states:
                    self.states[name] = "Running"
            elif part.startswith("Stop-ScheduledTask"):
                name = part.split()[-1]
                if self.stop_works and name in self.states:
                    self.states[name] = "Ready"
            elif part.startswith("'") and part.endswith("'"):
                out.append(part[1:-1])
        return "\n".join(out)

    @property
    def sent(self):
        """Everything except the read-only queries."""
        return [c for c in self.commands if not c.startswith("Get-ScheduledTask")]


@pytest.fixture
def results(monkeypatch):
    out = []
    monkeypatch.setattr(hc, "_results", out)
    monkeypatch.setattr(hc, "_scan_memo", {}, raising=False)
    return out


def _by_status(results, status):
    return [msg for st, _, msg in results if st == status]


def _said(results, section):
    """[(status, message)] recorded under one section."""
    return [(st, msg) for st, sec, msg in results if sec == section]


class World:
    """A scratch repo root for the watchdog - its own data/ and logs/ - and a clock the
    test moves. Files are aged against that clock, never the real one."""

    def __init__(self, root):
        self.root = root
        self.now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc).timestamp()

    def path(self, name):
        return self.root / "data" / name

    def age(self, name, minutes):
        """Make data/<name> (created empty if absent) `minutes` old on the test clock."""
        p = self.path(name)
        if not p.exists():
            p.write_text("", encoding="utf-8")
        t = self.now - minutes * 60
        os.utime(p, (t, t))
        return p

    def write(self, name, content, age_min=0.0):
        text = content if isinstance(content, str) else json.dumps(content)
        self.path(name).write_text(text, encoding="utf-8")
        return self.age(name, age_min)

    def tick(self, minutes):
        self.now += minutes * 60

    def next_run(self, minutes=15.0):
        """Finish this watchdog run and begin the next one `minutes` later, the way two
        scheduled runs follow each other (main() calls the same two functions)."""
        hc._end_run()
        self.tick(minutes)
        hc._results.clear()
        hc._begin_run()


@pytest.fixture
def world(tmp_path, monkeypatch, results):
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(hc, "ROOT", tmp_path)
    for attr, name in (("STAGE0_LOG", "stage0_crypto.jsonl"),
                       ("PAPER_LOG", "paper_crypto15.jsonl"),
                       ("HISTORY_LOG", "stage0_history.jsonl")):
        monkeypatch.setattr(hc, attr, tmp_path / "data" / name)
    monkeypatch.setattr(hc, "_state", {}, raising=False)
    monkeypatch.setattr(hc, "_state_ok", False, raising=False)
    monkeypatch.setattr(hc, "_state_kept", False, raising=False)
    monkeypatch.setenv("POLYBOT_LOCK_DIR", str(tmp_path / "data" / "locks"))
    w = World(tmp_path)
    monkeypatch.setattr(hc, "_now", lambda: w.now, raising=False)
    return w


@pytest.fixture
def windows(world, monkeypatch):
    """check_tasks believes it is on Windows, in a scratch root (no real data file or
    lock can colour the answer); the test installs its own scheduler."""
    monkeypatch.setattr(hc, "IS_WIN", True)

    def install(sched):
        monkeypatch.setattr(hc, "_ps", sched)
        return sched
    return install


# ── [tasks]: an answer that never came is not four missing tasks ──────────────

def test_an_empty_task_scheduler_answer_is_one_warning_not_four_missing_tasks(
        results, windows):
    """news-health#7: _ps returns '' on any failure, its 20 s timeout included. That used
    to log four 'NOT REGISTERED - run install_autostart.ps1' FAILs for tasks that were
    running, and sent the owner to re-run the installer for nothing."""
    sched = windows(FakeScheduler(ALL_RUNNING, answers=False))
    hc.check_tasks(repair=True)
    assert _by_status(results, hc.FAIL) == []
    warns = _by_status(results, hc.WARN)
    assert len(warns) == 1 and "could not query Task Scheduler" in warns[0], results
    assert sched.sent == []                       # and nothing was "repaired" blind


def test_a_task_absent_from_a_real_answer_is_not_registered(results, windows):
    """The other half: PowerShell DID answer (the marker line is there), so a task that
    is not in the list really is unregistered - all four of them included."""
    windows(FakeScheduler({t: "Running" for t in hc.TASKS if t != "PolybotPaper"}))
    hc.check_tasks(repair=False)
    assert _by_status(results, hc.FAIL) == [
        "PolybotPaper NOT REGISTERED - run install_autostart.ps1"]
    assert len(_by_status(results, hc.OK)) == 3

    results.clear()
    windows(FakeScheduler({}))                    # answered: nothing is registered
    hc.check_tasks(repair=False)
    assert _by_status(results, hc.FAIL) == [
        f"{t} NOT REGISTERED - run install_autostart.ps1" for t in hc.TASKS]


def test_an_answer_without_its_marker_never_reports_a_task_as_missing(results, windows):
    """States but no marker (an answer cut short): what is listed is judged, what is not
    listed is unknown - one warning, never NOT REGISTERED."""
    windows(lambda cmd, timeout=20: "PolybotStage0=Running\nPolybotPaper=Running")
    hc.check_tasks(repair=False)
    assert _by_status(results, hc.FAIL) == []
    assert _by_status(results, hc.OK) == ["PolybotStage0 running", "PolybotPaper running"]
    warns = _by_status(results, hc.WARN)
    assert len(warns) == 1 and "PolybotDashboards" in warns[0] and "PolybotSleeves" in warns[0]


# ── [ledger]: recent evidence fails, history informs ──────────────────────────

NEWEST = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)     # the ledger's last row


def _bet(ticker, hours_ago, rule="H1_settlement_lag", closed=True):
    """One paper position in the crypto ledger's row shapes: its open, and its close."""
    t = NEWEST - timedelta(hours=hours_ago, minutes=14)
    o = {"t": "open", "ts": t.isoformat(), "ticker": ticker, "rule": rule, "side": "yes",
         "price": 0.85, "contracts": 1, "fee": 0.01, "mins_left": 12.0}
    c = {"t": "close", "ts": (t + timedelta(minutes=14)).isoformat(), "ticker": ticker,
         "rule": rule, "side": "yes", "price": 0.85, "result": "yes", "won": True,
         "pnl": 0.14}
    return [o, c] if closed else [o]


def _ledger(path, rows):
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows),
                    encoding="utf-8")
    return path


def _crypto_ledger(tmp_path, monkeypatch, rows):
    p = _ledger(tmp_path / "paper_crypto15.jsonl", rows)
    monkeypatch.setattr(hc, "PAPER_LOG", p)
    return p


def test_an_old_duplicate_is_a_warning_that_names_it_not_a_failure_for_ever(
        results, tmp_path, monkeypatch):
    """news-health#7: the ledger is append-only, so one duplicate open from two months
    ago failed [ledger] on every run for good - and a watchdog that is always red cannot
    report the next outage. diagnostics B1 judges the last 24 h; so does this now."""
    import diagnostics as dg
    old = _bet("KXBTC15M-26SEP250245-00", 61 * 24)
    rows = old[:1] + [dict(old[0])] + old[1:] + _bet("KXBTC15M-NEW1", 2) + _bet("KXBTC15M-NEW2", 0)
    p = _crypto_ledger(tmp_path, monkeypatch, rows)
    hc.check_ledger_integrity()
    assert _by_status(results, hc.FAIL) == []
    (warn,) = _by_status(results, hc.WARN)
    assert "1 duplicate open" in warn and "KXBTC15M-26SEP250245-00" in warn, warn
    assert dg.check_b1_ledger_structure(dg._load(p))[0] is True      # the same verdict

    # ...and it stays a warning however much clean trading follows: the count is still
    # reported, because the scored record still carries the row
    more = [r for k in range(50) for r in _bet(f"KXBTC15M-LATER{k}", -k - 1)]
    _ledger(p, rows + more)
    results.clear()
    hc.check_ledger_integrity()
    assert _by_status(results, hc.FAIL) == [] and len(_by_status(results, hc.WARN)) == 1


def test_a_duplicate_from_the_last_day_still_fails(results, tmp_path, monkeypatch):
    """Two overlapping cycles both wrote the same open ten minutes ago: that is today's
    problem and stays a FAIL - here and in diagnostics B1, by the same 24-hour window."""
    import diagnostics as dg
    new = _bet("KXBTC15M-26NOV251200-00", 0, closed=False)
    p = _crypto_ledger(tmp_path, monkeypatch,
                       _bet("KXBTC15M-OLD", 61 * 24) + new + [dict(new[0])])
    hc.check_ledger_integrity()
    (fail,) = _by_status(results, hc.FAIL)
    assert "1 duplicate open" in fail and "KXBTC15M-26NOV251200-00" in fail, fail
    assert dg.check_b1_ledger_structure(dg._load(p))[0] is False


def test_an_old_duplicated_close_is_counted_too(results, tmp_path, monkeypatch):
    """A duplicated close is the one that changes a score (the trade counts twice). The
    old scan only looked at opens, and its 'more closes than opens' test went red and
    green with the number of positions that happened to be open."""
    old = _bet("KXBTC15M-OLD", 61 * 24)
    _crypto_ledger(tmp_path, monkeypatch, old + [dict(old[1])] + _bet("KXBTC15M-NEW", 0))
    hc.check_ledger_integrity()
    assert _by_status(results, hc.FAIL) == []
    (warn,) = _by_status(results, hc.WARN)
    assert "1 duplicate close" in warn and "counts that trade twice" in warn, warn


def test_a_clean_ledger_reads_as_before(results, tmp_path, monkeypatch):
    _crypto_ledger(tmp_path, monkeypatch, _bet("KXBTC15M-A", 30) + _bet("KXBTC15M-B", 0))
    hc.check_ledger_integrity()
    assert results == [(hc.OK, "ledger", "2 opens / 2 closes, no duplicates")]


# ── [news]: the hand-kept calendar runs out ───────────────────────────────────

_CALENDAR = """events:
  - {date: "2026-12-04", time_et: "08:30", event: "Jobs report (NFP)", impact: high}
  - {date: "2026-12-09", time_et: "14:00", event: "FOMC rate decision + projections", impact: high}
  - {date: "2026-12-10", time_et: "08:30", event: "CPI release", impact: high}
  - {date: "2026-12-15", time_et: "08:30", event: "PPI release", impact: medium}
  - {date: "2027-01-27", time_et: "14:00", event: "FOMC rate decision", impact: high}
  - {date: "2027-12-08", time_et: "14:00", event: "FOMC rate decision + projections", impact: high}
"""


def _calendar_at(tmp_path, monkeypatch, results, utc):
    """What [news] says about the calendar with the clock at `utc`: (statuses, warnings)."""
    from lib import news_regime as nr
    p = tmp_path / "news_calendar.yaml"
    p.write_text(_CALENDAR, encoding="utf-8")
    monkeypatch.setattr(nr, "CALENDAR_PATH", p)
    monkeypatch.setattr(nr, "_utcnow", lambda: utc)
    results.clear()
    hc.check_news_calendar()
    return [st for st, _, _ in results], _by_status(results, hc.WARN)


def test_the_calendar_check_warns_when_a_release_family_has_run_out(
        results, tmp_path, monkeypatch):
    """news-health#5: the shipped calendar lists no jobs report or CPI after 10 December
    2026. The check said 'calendar readable (12 listed)' and would have said so in 2028:
    it never compared a date with today, and release days were stamped as ordinary."""
    def at(y, m, d, h=17):
        return _calendar_at(tmp_path, monkeypatch, results,
                            datetime(y, m, d, h, tzinfo=timezone.utc))

    assert at(2026, 11, 25) == ([hc.OK], [])            # every family has a date ahead
    assert at(2026, 12, 4) == ([hc.OK], [])             # the last jobs report is TODAY
    statuses, (warn,) = at(2026, 12, 5)
    assert statuses == [hc.OK, hc.WARN]                 # still readable - and run out
    which, advice = warn.split(" - ", 1)
    assert which == "calendar ran out for Jobs (last listed 2026-12-04)", warn
    assert "news_calendar.yaml" in advice
    _, (warn,) = at(2027, 1, 15)
    assert warn.split(" - ", 1)[0] == (
        "calendar ran out for Jobs (last listed 2026-12-04), CPI (last listed 2026-12-10), "
        "PPI (last listed 2026-12-15)")                 # FOMC is listed through 2027-12-08
    _, (warn,) = at(2027, 12, 9)
    assert "FOMC (last listed 2027-12-08)" in warn.split(" - ", 1)[0]


def test_the_calendar_runs_out_on_the_eastern_day_not_the_utc_one(
        results, tmp_path, monkeypatch):
    """Calendar dates are US/Eastern days. 03:00 UTC on the 5th is still the evening of
    the 4th in New York - the jobs-report day itself - so nothing has run out yet."""
    late_evening_et = datetime(2026, 12, 5, 3, 0, tzinfo=timezone.utc)
    assert _calendar_at(tmp_path, monkeypatch, results, late_evening_et) == ([hc.OK], [])
    after_midnight_et = datetime(2026, 12, 5, 6, 0, tzinfo=timezone.utc)
    assert len(_calendar_at(tmp_path, monkeypatch, results, after_midnight_et)[1]) == 1


# ── [data] / [paper]: the crypto paper trader is judged by the work it books ──

def _paper_line(results):
    (line,) = [(st, msg) for st, sec, msg in results if sec == "data" and "paper ledger" in msg]
    return line


def test_a_silent_crypto_ledger_is_judged_not_only_printed(results, world):
    """news-health#1: the ledger's age was printed inside an OK line and never judged. A
    paper trader that had written nothing for three days read 'paper ledger 73603 rows
    (last write 4320 min ago)' and the run was logged healthy, exit 0."""
    world.write("paper_crypto15.jsonl", "{}\n", age_min=3 * 1440)
    hc.check_data_flow()
    status, msg = _paper_line(results)
    assert status == hc.FAIL and "SILENT 4320 min" in msg, msg
    assert "no heartbeat" in msg                  # and it says what it cannot know

    for age, want in ((5, hc.OK), (20, hc.OK), (21, hc.WARN), (60, hc.WARN), (61, hc.FAIL)):
        world.age("paper_crypto15.jsonl", age)
        results.clear()
        hc.check_data_flow()
        assert _paper_line(results)[0] == want, (age, _paper_line(results))
    assert _paper_line(results)[1].startswith("paper ledger SILENT 61 min")


def test_after_a_night_asleep_the_silence_is_judged_from_the_wake_up(results, world):
    """The fix risk: when the PC wakes, the ledger is as old as the night and the watchdog
    runs at once. A gap in the watchdog's own 15-minute chain is the wake-up; the
    ledger's silence counts from there - a grace period, not an amnesty."""
    world.write("paper_crypto15.jsonl", "{}\n", age_min=1)
    hc._begin_run()                               # 12:00, all well
    hc.check_data_flow()
    assert _paper_line(results)[0] == hc.OK
    world.next_run(8 * 60)                        # asleep for eight hours, then a run
    hc.check_data_flow()
    status, msg = _paper_line(results)
    assert status == hc.OK and "last write 481 min ago" in msg and "off or asleep" in msg
    world.next_run(15)                            # 15 min awake, still nothing booked
    hc.check_data_flow()
    assert _paper_line(results)[0] == hc.OK
    world.next_run(15)                            # 30 min awake
    hc.check_data_flow()
    assert _paper_line(results)[0] == hc.WARN
    world.next_run(15)
    world.next_run(15)
    world.next_run(15)                            # 75 min awake: the grace is over
    hc.check_data_flow()
    status, msg = _paper_line(results)
    assert status == hc.FAIL and "SILENT 556 min" in msg and "until 75 min ago" in msg, msg


def test_without_a_usable_state_file_there_is_no_grace(results, world):
    """Failing safe: the grace needs proof of a gap. On the very first run, or when the
    state file cannot be kept, the full age is judged - a watchdog must never be made
    blind by its own bookkeeping - and an unwritable file is said out loud."""
    world.write("paper_crypto15.jsonl", "{}\n", age_min=3 * 1440)
    hc._begin_run()                               # the first run there has ever been
    hc.check_data_flow()
    assert _paper_line(results)[0] == hc.FAIL
    hc._end_run()
    assert _said(results, "watchdog") == []       # the file was written: nothing to say

    (world.root / "logs" / hc.STATE_NAME).unlink()
    (world.root / "logs").rmdir()
    (world.root / "logs").write_text("not a folder", encoding="utf-8")
    world.tick(8 * 60)                            # a gap - but it cannot be recorded
    results.clear()
    hc._begin_run()
    hc.check_data_flow()
    assert _paper_line(results)[0] == hc.FAIL
    hc._end_run()
    ((status, msg),) = _said(results, "watchdog")
    assert status == hc.WARN and hc.STATE_NAME in msg


def test_runs_by_hand_never_look_like_a_wake_up(results, world):
    """The grace hangs on a gap in the SCHEDULED runs. If every run counted, someone
    who runs the healthcheck by hand once a day would look like a PC that has just woken
    up, every time, and a dead trader's silence would never be judged."""
    world.write("paper_crypto15.jsonl", "{}\n", age_min=3 * 1440)
    for _ in range(3):                            # by hand, a day apart, no scheduled task
        results.clear()
        hc._begin_run(scheduled=False)
        hc.check_data_flow()
        assert _paper_line(results)[0] == hc.FAIL
        hc._end_run()
        assert _said(results, "watchdog") == []   # nothing kept, and nothing to complain of
        world.tick(1440)
    assert not (world.root / "logs" / hc.STATE_NAME).exists()

    # a run by hand BETWEEN two scheduled runs judges like them and does not move the chain
    world.age("paper_crypto15.jsonl", 1)
    hc._begin_run()
    world.next_run(8 * 60)                        # asleep; the scheduled run at the wake-up
    hc._end_run()
    world.tick(5)
    results.clear()
    hc._begin_run(scheduled=False)                # five minutes later, by hand
    hc.check_data_flow()
    status, msg = _paper_line(results)
    assert status == hc.OK and "off or asleep until 5 min ago" in msg
    hc._end_run()
    assert hc._read_json(world.root / "logs" / hc.STATE_NAME)["last_run"] == world.now - 300


TODAYS_CRYPTO_HEARTBEAT = {          # the shared contract's keys, a healthy cycle
    "ts": "2026-11-25T11:59:00+00:00", "took_s": 2.1, "opened": 1, "closed": 0,
    "open_now": 3, "skipped": {"window_cap": 2}, "ungated": 0, "errors": 0,
    "last_error": None, "clock_skew_s": 0.4, "quote_lag_s": {"KXBTC15M": 0.3},
    "overdue": {"n": 0, "oldest": None, "oldest_h": None}}


def test_a_missing_crypto_heartbeat_is_unknown_not_a_fault(results, world):
    """Shared contract: the trader keeps its old code after a pull until it is
    restarted, so the file may simply not exist yet."""
    world.write("paper_crypto15.jsonl", "{}\n", age_min=2)
    hc.check_crypto_paper()
    assert [st for st, _ in _said(results, "paper")] == [hc.OK]

    results.clear()                               # ...and one with only some keys
    world.write(hc.PAPER_HB, {"ts": "2026-11-25T11:59:00+00:00"}, age_min=1)
    hc.check_crypto_paper()
    assert [st for st, _ in _said(results, "paper")] == [hc.OK]


def test_a_cycling_trader_that_books_nothing_is_still_a_failure(results, world):
    """The verifier's two stalls: every request failing, and every slot held by a
    position that will never settle. Both cycles finish normally, so a fresh heartbeat
    says 'cycling' - the ledger says 'not working', and the ledger is the judge. The
    heartbeat's counters say why."""
    world.write("paper_crypto15.jsonl", "{}\n", age_min=75)
    world.write(hc.PAPER_HB, dict(TODAYS_CRYPTO_HEARTBEAT, opened=0, open_now=8, errors=6,
                                  skipped={"max_concurrent": 2},
                                  last_error="ConnectionError: 429"), age_min=0.5)
    hc.check_data_flow()
    hc.check_crypto_paper()
    status, msg = _paper_line(results)
    assert status == hc.FAIL and "IS cycling" in msg and "booking nothing" in msg, msg
    assert all(x in msg for x in ("open_now 8", "errors 6", "max_concurrent=2",
                                  "ConnectionError: 429")), msg
    assert hc.FAIL not in [st for st, _ in _said(results, "paper")]   # it IS cycling


def test_a_stale_crypto_heartbeat_means_not_cycling(results, world):
    world.write("paper_crypto15.jsonl", "{}\n", age_min=31)
    world.write(hc.PAPER_HB, TODAYS_CRYPTO_HEARTBEAT, age_min=30)
    hc.check_crypto_paper()
    ((status, msg),) = _said(results, "paper")
    assert status == hc.FAIL and "30 min old" in msg and "not cycling" in msg

    # but a trader that is booking rows is working, whatever its heartbeat file says
    world.age("paper_crypto15.jsonl", 2)
    results.clear()
    hc.check_crypto_paper()
    ((status, msg),) = _said(results, "paper")
    assert status == hc.WARN and "heartbeat is not being written" in msg

    results.clear()
    world.path(hc.PAPER_HB).write_text("{torn", encoding="utf-8")
    hc.check_crypto_paper()
    assert _said(results, "paper") == [(hc.WARN, "heartbeat unreadable")]


def test_failing_requests_warn_only_when_seen_on_two_runs(results, world):
    """One failed request in the one cycle in fifteen that the watchdog reads would flap
    the log. Seen again at the next run, it is a pattern."""
    world.write("paper_crypto15.jsonl", "{}\n", age_min=1)
    failing = dict(TODAYS_CRYPTO_HEARTBEAT, errors=2, last_error="ReadTimeout: spot")
    hc._begin_run()
    world.write(hc.PAPER_HB, failing, age_min=0.5)
    hc.check_crypto_paper()
    assert [st for st, _ in _said(results, "paper")] == [hc.OK]
    world.next_run(15)
    world.write(hc.PAPER_HB, failing, age_min=0.5)
    hc.check_crypto_paper()
    warn = [m for st, m in _said(results, "paper") if st == hc.WARN]
    assert len(warn) == 1 and "requests are failing: 2" in warn[0] and "ReadTimeout" in warn[0]
    world.next_run(15)                            # one clean cycle starts the count again
    world.write(hc.PAPER_HB, TODAYS_CRYPTO_HEARTBEAT, age_min=0.5)
    hc.check_crypto_paper()
    world.next_run(15)
    world.write(hc.PAPER_HB, failing, age_min=0.5)
    hc.check_crypto_paper()
    assert [st for st, _ in _said(results, "paper")] == [hc.OK]


# ── [sleeves]: a fresh heartbeat is not a working trader ──────────────────────

TODAYS_SLEEVE_HEARTBEAT = {          # exactly what sleeve_paper wrote before the audit
    "ts": "2026-11-25T11:58:00+00:00", "rules": 2, "series": 11, "opened": 0, "closed": 0,
    "skipped": {}, "ungated": 0, "errors": 0, "regime": "good", "open_now": 15}
NEW_SLEEVE_HEARTBEAT = dict(         # the shared contract's new keys, a healthy cycle
    TODAYS_SLEEVE_HEARTBEAT, poll_errors=0, market_errors=0, all_failed_streak=0,
    series_fail_streak={}, cycle_error=None, failed_cycles=0,
    overdue={"n": 0, "oldest": None, "oldest_h": None}, took_s=4.2)


def _sleeves(world, results, heartbeat, age_min=1.0):
    """What [sleeves] says about this heartbeat: [(status, message)]."""
    world.write(hc.SLEEVE_HB, heartbeat, age_min=age_min)
    results.clear()
    hc.check_sleeve_paper()
    return _said(results, "sleeves")


def test_todays_heartbeat_still_reads_exactly_as_before(results, world):
    """Version skew: PolybotSleeves keeps its old code after a pull until restarted. Its
    heartbeat has none of the new keys, and that is unknown - never a fault."""
    assert _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT) == [
        (hc.OK, "2 rules on 11 sleeves, 15 open (cycle 1 min ago)")]
    assert _sleeves(world, results, NEW_SLEEVE_HEARTBEAT) == [
        (hc.OK, "2 rules on 11 sleeves, 15 open (cycle 1 min ago)")]
    assert _sleeves(world, results, dict(TODAYS_SLEEVE_HEARTBEAT, ungated=3)) == [
        (hc.OK, "2 rules on 11 sleeves, 15 open, 3 ungated (cycle 1 min ago)")]
    ((status, msg),) = _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT, age_min=120)
    assert status == hc.FAIL and "120 min old" in msg and "not cycling" in msg
    ((status, msg),) = _sleeves(world, results, dict(TODAYS_SLEEVE_HEARTBEAT, rules=0))
    assert status == hc.WARN and "register-probes" in msg


def test_every_request_failing_is_a_failure_once_it_is_a_streak(results, world):
    """sleeve-trader#1, case 1: every Kalshi call raised for six hours, the heartbeat
    said errors=11 for 11 series, and the watchdog answered '[OK] sleeves: 2 rules on 11
    sleeves, 18 open (cycle 0 min ago)' on 24 of 24 runs. A streak counted by the trader
    is a FAIL; one bad cycle is not."""
    dead = dict(NEW_SLEEVE_HEARTBEAT, errors=11)
    assert [st for st, _ in _sleeves(world, results, dict(dead, all_failed_streak=2))] != [hc.FAIL]
    ((status, msg),) = _sleeves(world, results, dict(dead, all_failed_streak=3))
    assert status == hc.FAIL and "EVERY series list call failed for 3 cycles in a row" in msg
    ((status, msg),) = _sleeves(world, results, dict(dead, all_failed_streak=180))
    assert status == hc.FAIL and "180 cycles" in msg and "15 open" in msg


def test_a_trader_that_counts_no_streak_is_judged_on_two_runs_in_a_row(results, world):
    """The same outage with the trader still on its old code: errors == series is all
    the heartbeat says. One such cycle is not a verdict (the fix risk); the same thing
    at the next watchdog run is."""
    dead = dict(TODAYS_SLEEVE_HEARTBEAT, errors=11)
    hc._begin_run()
    assert [st for st, _ in _sleeves(world, results, dead)] == [hc.OK]
    world.next_run(15)
    ((status, msg),) = _sleeves(world, results, dead)
    assert status == hc.FAIL and "already at the previous check" in msg, msg
    world.next_run(15)                            # recovered: the count starts again
    assert [st for st, _ in _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT)] == [hc.OK]
    world.next_run(15)
    assert [st for st, _ in _sleeves(world, results, dead)] == [hc.OK]


def test_a_cycle_that_raised_is_reported_with_its_cause(results, world):
    """sleeve-trader#1, case 5: a cycle that raises used to leave the heartbeat stale -
    'alive but not cycling', ten minutes late and with the cause lost on a hidden
    console. The trader now writes the heartbeat anyway; this reads it."""
    failed = dict(NEW_SLEEVE_HEARTBEAT, cycle_error="AttributeError: 'NoneType' object "
                                                    "has no attribute 'get'")
    ((status, msg),) = _sleeves(world, results, dict(failed, failed_cycles=3))
    assert status == hc.FAIL and "3 cycles in a row" in msg and "AttributeError" in msg
    ((status, msg),) = _sleeves(world, results, dict(failed, failed_cycles=1))
    assert status == hc.WARN and "1 in a row" in msg and "AttributeError" in msg
    failed.pop("failed_cycles")                   # no streak told: the error is all we know
    assert _sleeves(world, results, failed)[0][0] == hc.FAIL


def test_a_failed_cycle_heartbeat_of_any_shape_neither_crashes_nor_misleads(results, world):
    """The verifier's fix risk: fed a failed-cycle heartbeat without `series`, the check
    raised KeyError and the whole watchdog run died; without `rules` it advised
    'register-probes' - wrong, and that command registers rules."""
    for shape in ({"ts": "2026-11-25T11:58:00+00:00", "cycle_error": "KeyError: 'band'",
                   "failed_cycles": 30},
                  {"ts": "2026-11-25T11:58:00+00:00", "rules": 2,
                   "cycle_error": "KeyError: 'band'", "failed_cycles": 30}):
        ((status, msg),) = _sleeves(world, results, shape)
        assert status == hc.FAIL and "KeyError: 'band'" in msg and "register" not in msg
    # and a heartbeat that merely lacks `series` still reads
    ((status, msg),) = _sleeves(world, results, {"ts": "x", "rules": 2, "open_now": 4})
    assert status == hc.OK and msg == "2 rules on ? sleeves, 4 open (cycle 1 min ago)"


def test_one_series_failing_for_cycles_on_end_is_a_warning(results, world):
    """sleeve-trader#1, case 2: a replaced series ticker. The rest trade, so nothing
    else shows it."""
    hb = dict(NEW_SLEEVE_HEARTBEAT, errors=2,
              series_fail_streak={"KXTEMPNYCHS": 41, "KXHIGHCHI": 2, "KXTEMPMIAH": 3})
    warn, ok = _sleeves(world, results, hb)
    assert warn[0] == hc.WARN and ok[0] == hc.OK
    assert "2 of 11 series" in warn[1] and "KXTEMPNYCHS x41, KXTEMPMIAH x3" in warn[1]
    assert "KXHIGHCHI" not in warn[1]             # two cycles is not a streak yet


def test_per_cycle_counters_warn_only_when_seen_on_two_runs(results, world):
    """Failed settlement polls and skipped markets are per-cycle counts: 'a single
    failed poll in the one cycle the watchdog happens to read will flap'."""
    hb = dict(NEW_SLEEVE_HEARTBEAT, poll_errors=4, market_errors=1, errors=1)
    hc._begin_run()
    assert [st for st, _ in _sleeves(world, results, hb)] == [hc.OK]
    world.next_run(15)
    said = _sleeves(world, results, hb)
    warns = [m for st, m in said if st == hc.WARN]
    assert len(warns) == 3 and said[-1][0] == hc.OK
    assert any("settlement polls are failing: 4" in m for m in warns)
    assert any("1 market(s) or position(s) were skipped" in m for m in warns)
    assert any("list calls are failing: 1 of 11 series" in m for m in warns)


def test_a_rule_at_its_open_position_cap_is_a_warning(results, world):
    """sleeve-trader#1, case 4: results stopped arriving, the rule reached its cap of 50
    and skipped every event from then on - '[OK] ... 64 open' on 64 of 64 runs."""
    hb = dict(TODAYS_SLEEVE_HEARTBEAT, skipped={"open_cap": 16, "no_depth": 3}, open_now=64)
    warn, ok = _sleeves(world, results, hb)
    assert warn[0] == hc.WARN and "open-position cap" in warn[1] and "16 entries" in warn[1]
    assert ok == (hc.OK, "2 rules on 11 sleeves, 64 open (cycle 1 min ago)")
    # the fill gate refusing candidates is the gate working, not a fault
    assert [st for st, _ in _sleeves(world, results, dict(
        TODAYS_SLEEVE_HEARTBEAT, skipped={"no_depth": 44}))] == [hc.OK]


# ── [sleeves]: the quiet-rule check - the one that needs no cause ─────────────

HOUR, DAY = 3600.0, 86400.0


def _iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _opens(rule, times, tag="", **extra):
    """Sleeve-ledger open rows for one rule at the given epoch times (`tag` keeps the
    tickers of a second batch apart from the first)."""
    return [dict({"t": "open", "ts": _iso(t), "rule": rule, "ticker": f"{rule}{tag}-{i}-T1",
                  "event": f"{rule}{tag}-{i}", "series": "KXTEMPNYCHS", "side": "yes",
                  "price": 0.98, "contracts": 10, "fee": 0.02, "depth": 50.0,
                  "close_time": _iso(t + HOUR)}, **extra)
            for i, t in enumerate(times)]


def _settled(opens):
    """The same positions, each followed by its close row (settled an hour later)."""
    closes = [{"t": "close", "ts": _iso(_epoch_of(o) + HOUR + 300), "ticker": o["ticker"],
               "rule": o["rule"], "event": o["event"], "series": o["series"],
               "side": o["side"], "price": o["price"], "result": "yes", "won": True,
               "pnl": 0.18} for o in opens]
    return sorted(opens + closes, key=_epoch_of)


def _epoch_of(row):
    return datetime.fromisoformat(row["ts"]).timestamp()


def _every(now, step_h, days=7.0, last_h=0.0):
    """Entry times every `step_h` hours over `days`, the last one `last_h` hours ago."""
    n = int(days * 24 / step_h)
    return [now - last_h * HOUR - k * step_h * HOUR for k in range(n, -1, -1)]


def _quiet_names(rows, now, idle=(), rules=("HOURLY_FAV", "F9799")):
    return [(name, round(quiet, 1), round(limit, 1))
            for name, quiet, limit, _ in hc._quiet_rules(rows, rules, now, idle)]


def test_a_rule_that_has_gone_quiet_for_longer_than_its_own_rhythm_is_named(world):
    """sleeve-trader#1 cases 2-4: the hourly series list nothing, or the fill gate
    refuses everything, or a band nothing reaches - errors=0, a clean heartbeat, and a
    rule that has simply stopped opening positions. The ledger is the only witness."""
    now = world.now
    hourly = _opens("HOURLY_FAV", _every(now, 0.25, last_h=3.0))      # 4 an hour, then silence
    daily = _opens("F9799", _every(now, 3.5, last_h=5.0))             # ~7 a day
    assert _quiet_names(hourly + daily, now) == [("HOURLY_FAV", 3.0, 2.0)]
    # and a rule that is not in the registry (retired) is nobody's alarm
    assert _quiet_names(hourly, now, rules=("F9799",)) == []
    # a longer gap seen this week is the rule's own rhythm: the limit is twice it
    bursty = _opens("HOURLY_FAV", [t for t in _every(now, 0.25, last_h=9.0)
                                   if not now - 80 * HOUR < t < now - 75 * HOUR])
    assert _quiet_names(bursty, now) == []                            # 9 h < 2 x 5 h
    assert _quiet_names(bursty, now + 2 * HOUR) == [("HOURLY_FAV", 11.0, 10.0)]


def test_a_once_a_day_rule_gets_a_full_day_before_it_counts_as_quiet(world):
    """The verifier's false alarms: a daily-high rule whose entries mostly come when the
    next day's markets open. On a day when ALL of them do, the next entry is 24 h away -
    and '1.5 x the longest gap this week' (often 5-10 h) cried wolf 28 times in 864
    healthy runs. A rule making fewer than 24 entries a day is not hourly: 26 h floor."""
    now = world.now
    assert _quiet_names(_opens("F9799", _every(now, 3.5, last_h=25.9)), now) == []
    assert _quiet_names(_opens("F9799", _every(now, 3.5, last_h=26.1)), now) == [
        ("F9799", 26.1, 26.0)]
    # every entry at the daily open, seven cities at once: 24 h apart, 48 h to alarm
    at_open = [now - 23.9 * HOUR - d * DAY for d in range(6, -1, -1) for _ in range(7)]
    assert _quiet_names(_opens("F9799", at_open), now) == []
    assert _quiet_names(_opens("F9799", at_open), now + 25 * HOUR) == [
        ("F9799", 48.9, 48.0)]


def test_a_rule_without_a_rhythm_yet_is_not_judged(world):
    """The verifier's first false alarms: 16 on the first night, when a few hours of
    entries were the whole history. A rule needs 20 entries AND two days behind it."""
    now = world.now
    first_day = _opens("HOURLY_FAV", _every(now, 0.25, days=1.5, last_h=6.0))
    assert _quiet_names(first_day, now) == []
    sparse = _opens("F9799", _every(now, 12.0, last_h=60.0))          # 15 entries in 7 days
    assert _quiet_names(sparse, now) == []
    two_days = _opens("HOURLY_FAV", _every(now, 0.25, days=2.1, last_h=3.0))
    assert _quiet_names(two_days, now) == [("HOURLY_FAV", 3.0, 2.0)]
    # forty entries in one burst a few days ago (a weekly release): no rhythm to break
    burst = _opens("FLB_FAV", [now - 3 * DAY + k * 60 for k in range(40)])
    assert _quiet_names(burst, now, rules=("FLB_FAV",)) == []


def test_time_the_trader_was_down_is_not_the_rules_rhythm(world):
    """The verifier's third weakness: a 10-hour host outage became the rule's longest
    gap and lifted its limit to 15 h for a week - the same six-hour fault two days later
    went unseen. Idle spans are subtracted from the gaps AND from the quiet time."""
    now = world.now
    off0, off1 = now - 2 * DAY - 10 * HOUR, now - 2 * DAY           # the PC was off
    times = [t for t in _every(now, 0.25, last_h=3.0) if not off0 < t < off1]
    rows = _opens("HOURLY_FAV", times)
    assert _quiet_names(rows, now) == []                            # blinded by the outage
    assert _quiet_names(rows, now, idle=[[off0, off1]]) == [("HOURLY_FAV", 3.0, 2.0)]
    # and waking up is not "quiet": asleep since the last entry, nothing is overdue
    asleep = _opens("HOURLY_FAV", _every(now, 0.25, last_h=8.0))
    assert _quiet_names(asleep, now) == [("HOURLY_FAV", 8.0, 2.0)]
    assert _quiet_names(asleep, now, idle=[[now - 8 * HOUR, now]]) == []


def _registry(world, *names, retired=()):
    rows = [{"t": "rule", "name": n, "series": ["KXTEMPNYCHS"], "band": "any",
             "side": "favorite", "lo": 0.97, "hi": 0.995, "contracts": 10,
             "named_at": "2026-11-01T00:00:00+00:00", "v": 2} for n in names]
    rows += [{"t": "retire", "name": n, "ts": "2026-11-20T00:00:00+00:00"} for n in retired]
    _ledger(world.path("sleeve_rules.jsonl"), rows)


def test_the_sleeve_check_warns_about_a_quiet_rule_and_stays_healthy(results, world):
    """Through check_sleeve_paper, from the files the trader writes. A WARN only: the
    verdict stays 'healthy' - it is a pointer for the owner, not an outage."""
    now = world.now
    _registry(world, "HOURLY_FAV", "F9799", "OLD_RULE", retired=("OLD_RULE",))
    _ledger(world.path("paper_sleeves.jsonl"),
            _opens("HOURLY_FAV", _every(now, 0.25, last_h=5.2))
            + _opens("F9799", _every(now, 3.5, last_h=1.0))
            + _opens("OLD_RULE", _every(now, 0.25, last_h=30.0)))
    said = _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT)
    assert [st for st, _ in said] == [hc.WARN, hc.OK]
    assert said[0][1].startswith("HOURLY_FAV has opened no position for 5.2 h")
    assert "longest gap in the last 7 days was 0.2 h (alarm past 2.0 h)" in said[0][1]


def test_a_night_asleep_is_remembered_as_idle_not_as_quiet(results, world):
    """End to end with the state file: the gap in the watchdog's own chain is recorded
    as time the trader was not cycling. Nothing is said at the wake-up, and that night
    does not become the rule's 'longest gap' afterwards."""
    _registry(world, "HOURLY_FAV")
    start = world.now
    _ledger(world.path("paper_sleeves.jsonl"),
            _settled(_opens("HOURLY_FAV", _every(start, 0.25))))
    hc._begin_run()
    assert [st for st, _ in _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT)] == [hc.OK]
    world.next_run(8 * 60)                        # asleep for 8 h; the watchdog runs first
    assert [st for st, _ in _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT)] == [hc.OK]
    assert hc._state["sleeve_idle"] == [[start, world.now]]
    # trading again for four hours, then three hours with nothing opened
    back = world.now
    _ledger(world.path("paper_sleeves.jsonl"), _settled(
        _opens("HOURLY_FAV", _every(start, 0.25))
        + _opens("HOURLY_FAV", [back + k * 0.25 * HOUR for k in range(1, 17)], tag="b")))
    for _ in range(28):                           # 7 h of 15-minute runs
        world.next_run(15)
    said = _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT)
    assert [st for st, _ in said] == [hc.WARN, hc.OK], said
    assert "HOURLY_FAV has opened no position for 3.0 h" in said[0][1]
    assert "was 0.2 h (alarm past 2.0 h)" in said[0][1]              # not 8 h -> 16 h


# ── [sleeves]: positions booked without a fill check ──────────────────────────

def test_positions_booked_without_a_fill_check_in_the_last_day_are_counted(results, world):
    """sleeve-trader#2 (watchdog half): a failed order-book lookup books the candidate
    with depth null - 'admitted but COUNTED as ungated, never hidden'. But the count
    lived in one cycle's heartbeat: with the gate off for a whole day (114 ungated
    entries) the watchdog mentioned it in 2 of 96 runs, both on OK lines that --quiet
    --log never writes. The ledger remembers every one of them."""
    now = world.now
    _registry(world, "HOURLY_FAV", "F9799")
    rows = (_opens("HOURLY_FAV", [now - h * HOUR for h in (30, 23, 20, 9, 2)])
            + _opens("HOURLY_FAV", [now - 25 * HOUR], "b", depth=None)   # yesterday's
            + _opens("HOURLY_FAV", [now - 22 * HOUR, now - 3 * HOUR], "c", depth=None)
            + _opens("F9799", [now - 5 * HOUR], depth=None))
    _ledger(world.path("paper_sleeves.jsonl"), rows)
    said = _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT)       # this cycle: ungated=0
    assert [st for st, _ in said] == [hc.WARN, hc.OK]
    assert said[0][1].startswith("3 of the 7 positions opened in the last 24 h were "
                                 "booked without a fill check (depth null: "
                                 "HOURLY_FAV 2 of 6, F9799 1 of 1)"), said[0][1]
    assert said[0][1].endswith("in the whole ledger: 4 of 9 (HOURLY_FAV 3 of 8, "
                               "F9799 1 of 1)")

    # none in the last day: no warning - but the record still carries the old one, and
    # the full (not --quiet) output says so
    _ledger(world.path("paper_sleeves.jsonl"),
            [r for r in rows if r["depth"] is not None or _epoch_of(r) < now - DAY])
    assert _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT) == [
        (hc.OK, "fill check: 1 of 6 positions in the whole ledger were booked without "
                "one (HOURLY_FAV 1 of 6), none in the last 24 h"),
        (hc.OK, "2 rules on 11 sleeves, 15 open (cycle 1 min ago)")]
    _ledger(world.path("paper_sleeves.jsonl"), [r for r in rows if r["depth"] is not None])
    assert [st for st, _ in _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT)] == [hc.OK]


# ── overdue positions: open long after their market closed ────────────────────

def test_overdue_sleeve_positions_from_the_heartbeat_are_named(results, world):
    """sleeve-trader#3 (watchdog half): a market that never reports yes or no was polled
    720 times a day without end, kept one of its rule's 50 slots, and showed nowhere -
    heartbeat errors=0, watchdog OK. The trader now counts them; this says so."""
    hb = dict(NEW_SLEEVE_HEARTBEAT,
              overdue={"n": 2, "oldest": "KXHIGHNY-26NOV20-B52", "oldest_h": 97.5})
    warn, ok = _sleeves(world, results, hb)
    assert warn[0] == hc.WARN and ok[0] == hc.OK
    assert warn[1].startswith("2 open positions more than 24 h past their close with no "
                              "result (oldest KXHIGHNY-26NOV20-B52, 98 h)"), warn[1]
    assert [st for st, _ in _sleeves(world, results, NEW_SLEEVE_HEARTBEAT)] == [hc.OK]


def test_overdue_sleeve_positions_are_found_in_the_ledger_when_the_trader_cannot_say(
        results, world):
    """A trader still on its old code writes no `overdue`. The ledger carries every open
    position's close_time, so the watchdog can count them itself - once it has seen
    them at two runs, because right after a wake-up every open position looks late."""
    now = world.now
    _registry(world, "F9799")
    stuck = _opens("F9799", [now - 60 * HOUR], "stuck", close_time=_iso(now - 30 * HOUR))
    waiting = _opens("F9799", [now - 40 * HOUR], "wait", close_time=_iso(now - 23 * HOUR))
    running = _opens("F9799", [now - 2 * HOUR], "run", close_time=_iso(now + 20 * HOUR))
    _ledger(world.path("paper_sleeves.jsonl"),
            _settled(_opens("F9799", [now - 70 * HOUR])) + stuck + waiting + running)
    hc._begin_run()
    assert [st for st, _ in _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT)] == [hc.OK]
    world.next_run(15)
    warn, ok = _sleeves(world, results, TODAYS_SLEEVE_HEARTBEAT)
    assert warn[0] == hc.WARN and "1 open position more than 24 h past its close" in warn[1]
    assert "(oldest F9799stuck-0-T1, 30 h)" in warn[1], warn[1]
    # the trader's own count, once it writes one, is the one that is used
    assert [st for st, _ in _sleeves(world, results, NEW_SLEEVE_HEARTBEAT)] == [hc.OK]


def test_stranded_crypto_positions_are_named_with_or_without_a_heartbeat(results, world):
    """crypto-trader#1 (watchdog half): after an outage longer than about 50 hours the
    positions that were open never settle and use up the 8-position cap; the watchdog
    said '[OK] ledger: 886 opens / 878 closes, no duplicates'. Each open row carries ts
    and mins_left, so this needs no trader change."""
    stranded = [row for h in (73, 72, 71)                           # opened, never closed
                for row in _bet(f"KXBTC15M-26NOV22{75 - h:02d}15-00", h, closed=False)]
    fresh = _bet("KXBTC15M-26NOV251200-00", 0, closed=False)        # its window is still open
    _ledger(hc.PAPER_LOG, _bet("KXBTC15M-OLD", 90) + stranded + fresh)
    world.age("paper_crypto15.jsonl", 1)
    hc._begin_run()
    hc.check_crypto_paper()
    assert [st for st, _ in _said(results, "paper")] == [hc.OK]      # first sighting
    world.next_run(15)
    world.age("paper_crypto15.jsonl", 1)
    hc.check_crypto_paper()
    (warn,) = [m for st, m in _said(results, "paper") if st == hc.WARN]
    assert warn.startswith("3 open positions more than 60 min past their window "
                           "(oldest KXBTC15M-26NOV220215-00, 73.3 h)"), warn

    # with a heartbeat that counts them, the trader's own number is reported at once
    results.clear()
    world.write(hc.PAPER_HB, dict(TODAYS_CRYPTO_HEARTBEAT, open_now=8, overdue={
        "n": 8, "oldest": "KXBTC15M-26NOV220315-15", "oldest_h": 71.9}), age_min=0.5)
    hc.check_crypto_paper()
    (warn,) = [m for st, m in _said(results, "paper") if st == hc.WARN]
    assert warn.startswith("8 open positions more than 60 min past their window "
                           "(oldest KXBTC15M-26NOV220315-15, 71.9 h)")


def test_positions_that_are_late_only_because_the_pc_slept_are_not_reported(results, world):
    """At the wake-up the ledger's open positions are as late as the night. The trader
    settles them in its first cycle; the watchdog, which runs at once, must not warn."""
    _ledger(hc.PAPER_LOG, _bet("KXBTC15M-26NOV251145-00", 0, closed=False))
    world.age("paper_crypto15.jsonl", 1)
    hc._begin_run()
    hc.check_crypto_paper()
    world.next_run(8 * 60)                        # asleep; that window closed 8 h ago
    hc.check_crypto_paper()
    assert [st for st, _ in _said(results, "paper")] == [hc.OK]
    _ledger(hc.PAPER_LOG, _bet("KXBTC15M-26NOV251145-00", 0))       # settled on waking
    world.next_run(15)
    hc.check_crypto_paper()
    assert [st for st, _ in _said(results, "paper")] == [hc.OK]


# ── main(), run after run: what PolybotHealth actually logs ────────────────────

FRESH = {"stage0_crypto.jsonl": 0.5, "stage0_history.jsonl": 30.0,
         "paper_crypto15.jsonl": 1.0, "sleeve_paper_heartbeat.json": 1.0}


class Host:
    """The scheduled task in miniature: main() with the task's own flags, every 15
    minutes on the test clock. Files listed in `fresh` are re-aged before each run (a
    working bot keeps writing); anything a test breaks, it removes from `fresh`."""

    def __init__(self, world, results, monkeypatch):
        self.world, self.results = world, results
        self.fresh = dict(FRESH)
        obs = {"t": "obs", "ts": "2026-11-25T11:59:30+00:00", "series": "KXBTC15M",
               "ticker": "KXBTC15M-W-1", "yes_ask": 0.5, "no_ask": 0.52,
               "book": {"yes": [[0.5, 10]]}}
        world.write("stage0_crypto.jsonl", json.dumps(obs) + "\n")
        world.write("stage0_history.jsonl", "{}\n")
        _ledger(world.path("paper_crypto15.jsonl"), _bet("KXBTC15M-A", 1))
        world.write("sleeve_paper_heartbeat.json", TODAYS_SLEEVE_HEARTBEAT)
        # not what these tests are about, and they read the real repo's files or ports
        for name in ("check_deps", "check_ports", "check_api", "check_model_integrity",
                     "check_news_regime", "check_news_calendar", "check_env_auth"):
            monkeypatch.setattr(hc, name, lambda: None)
        monkeypatch.setattr(sys, "argv", ["healthcheck.py", "--repair", "--quiet", "--log",
                                          "--fast"])
        self.first = True

    def run(self, minutes=15.0):
        """One scheduled run -> (exit code, [(status, section, message)] not OK)."""
        if not self.first:
            self.world.tick(minutes)
        self.first = False
        for name, age in self.fresh.items():
            self.world.age(name, age)
        self.results.clear()
        rc = hc.main()
        return rc, [r for r in self.results if r[0] != hc.OK]

    def log(self):
        return (self.world.root / "logs" / "healthcheck.log").read_text(encoding="utf-8")


@pytest.fixture
def host(world, results, monkeypatch, capsys):
    return Host(world, results, monkeypatch)


def test_the_scheduled_run_goes_red_when_the_paper_trader_stops_booking(host):
    """news-health#1 end to end, the way the task runs it. Before: 'healthy fails=0' on
    every run while the ledger sat untouched for three days."""
    rc, bad = host.run()
    assert rc == 0 and [r for r in bad if r[1] != "tasks"] == [], bad
    del host.fresh["paper_crypto15.jsonl"]        # from now on nothing is booked
    seen = []
    for _ in range(6):                            # 16, 31, 46, 61, 76, 91 min of silence
        rc, bad = host.run()
        seen.append((rc, [st for st, sec, msg in bad if "paper ledger" in msg]))
    assert seen == [(0, []), (0, [hc.WARN]), (0, [hc.WARN]),
                    (1, [hc.FAIL]), (1, [hc.FAIL]), (1, [hc.FAIL])]
    last = host.log().splitlines()
    head = max(i for i, line in enumerate(last) if not line.startswith(" "))
    assert "UNHEALTHY fails=1" in last[head]
    assert any("[FAIL] data: paper ledger SILENT 91 min" in line for line in last[head:])
    assert (host.world.root / "logs" / hc.STATE_NAME).exists()


def test_one_check_raising_does_not_take_the_run_down(host, monkeypatch):
    """A watchdog that dies logs nothing, and nothing reads as 'no news'."""
    def boom():
        raise KeyError("series")
    monkeypatch.setattr(hc, "check_sleeve_paper", boom)
    rc, bad = host.run()
    assert rc == 1
    assert (hc.FAIL, "watchdog") in [(st, sec) for st, sec, _ in bad]
    assert "boom raised KeyError" in host.log() and "UNHEALTHY" in host.log()
    assert any(sec == "disk" for _, sec, _ in host.results)        # later checks still ran


# ── --repair: a task that is Running but stalled, and a start that does not take ──

@pytest.fixture
def scheduler(host, windows, monkeypatch):
    """A Windows host in good order: the four tasks Running, PolybotNews Ready between
    its hourly runs, every bot holding its single-instance lock, and no real waiting."""
    sched = windows(FakeScheduler(dict(ALL_RUNNING, PolybotNews="Ready")))
    task_of = {lock: task for task, lock in getattr(hc, "TASK_LOCK", {}).items()}
    monkeypatch.setattr(hc, "_sleep", lambda seconds: None, raising=False)
    monkeypatch.setattr(hc, "_lock_held",          # a bot holds its lock while its task runs
                        lambda name: sched.states.get(task_of[name]) == "Running",
                        raising=False)
    return sched


def _tasks(bad):
    return [(st, msg) for st, sec, msg in bad if sec == "tasks"]


def _stop_start(task):
    return [f"Stop-ScheduledTask -TaskName {task}", f"Start-ScheduledTask -TaskName {task}"]


def test_a_running_but_stalled_collector_is_restarted_on_the_second_run(host, scheduler):
    """news-health#2: collector log three hours stale, PolybotStage0 'Running'. Three
    runs each logged '[FAIL] data: stage0 log STALE 180 min ... alive but not collecting'
    and sent no command at all: check_tasks only acted on tasks that were NOT Running,
    and the 10-minute repeat trigger is a no-op while the task runs."""
    assert host.run()[0] == 0 and scheduler.sent == []
    del host.fresh["stage0_crypto.jsonl"]
    host.world.age("stage0_crypto.jsonl", 165)
    rc, bad = host.run()                                   # one look is not a verdict
    assert rc == 1 and scheduler.sent == []
    ((status, msg),) = _tasks(bad)
    assert status == hc.WARN and "looks stalled (stage0 log 180 min old)" in msg
    assert "restarted if nothing has been written by the next run" in msg
    rc, bad = host.run()                                   # still nothing written
    assert scheduler.sent == _stop_start("PolybotStage0")
    ((status, msg),) = _tasks(bad)
    assert status == hc.WARN and "RESTARTED (stop + start), now Running" in msg
    assert scheduler.commands[-1].startswith("Get-ScheduledTask PolybotStage0 ")  # re-queried
    host.fresh["stage0_crypto.jsonl"] = 0.5                # the new collector writes
    rc, bad = host.run()
    assert rc == 0 and _tasks(bad) == [] and len(scheduler.sent) == 2


def test_a_restart_that_does_not_help_is_not_repeated_for_an_hour_and_then_fails(
        host, scheduler):
    """The cap: at most one restart per task per hour (an exchange pause lasts hours and
    a restart cures none of it). And the second in a row is a FAIL - restarting is not
    working, a person is needed."""
    host.run()
    del host.fresh["stage0_crypto.jsonl"]
    host.world.age("stage0_crypto.jsonl", 60)
    host.run()
    host.run()                                             # restart no. 1
    assert scheduler.sent == _stop_start("PolybotStage0")
    for minutes in (15, 30, 45):
        _, bad = host.run()
        ((status, msg),) = _tasks(bad)
        assert status == hc.WARN and f"restarted {minutes} min ago and is still stalled" in msg
        assert len(scheduler.sent) == 2
    _, bad = host.run()                                    # an hour on: once more
    assert scheduler.sent == _stop_start("PolybotStage0") * 2
    ((status, msg),) = _tasks(bad)
    assert status == hc.FAIL and "attempt 2 in a row" in msg
    assert "py scripts\\stage0_collector.py collect" in msg


def test_waking_up_does_not_restart_anything(host, scheduler):
    """The fix risk: right after the PC wakes, every bot's evidence is as old as the
    night and the watchdog runs at once. Nothing seen before the gap counts, the first
    run after it is a first look, and by the next one the bots have written."""
    host.run()
    for name in ("stage0_crypto.jsonl", "sleeve_paper_heartbeat.json", "paper_crypto15.jsonl"):
        del host.fresh[name]
    _, bad = host.run()                                    # a first look, 16 min stale
    assert len(_tasks(bad)) == 2                           # collector and sleeves noted
    _, bad = host.run(8 * 60)                              # asleep 8 h, then this run
    assert scheduler.sent == [] and _tasks(bad) == []      # 8 h old - and just woken
    host.fresh.update(FRESH)                               # everything resumes...
    del host.fresh["sleeve_paper_heartbeat.json"]          # ...except the sleeve trader
    _, bad = host.run()                                    # 15 min awake: a first look
    assert scheduler.sent == []
    ((status, msg),) = _tasks(bad)
    assert status == hc.WARN and msg.startswith("PolybotSleeves is Running but its bot "
                                                "looks stalled (heartbeat 511 min old)")
    _, bad = host.run()                                    # 30 min awake: it is restarted
    assert scheduler.sent == _stop_start("PolybotSleeves")


def test_a_slow_bot_that_keeps_writing_is_never_restarted(host, scheduler):
    """Stale at two looks is not enough when it WROTE in between: that is a slow cycle
    (a sleeve cycle in which every request times out takes over ten minutes), not a
    stall, and restarting a working trader drops the cycle in flight."""
    host.run()
    host.fresh["sleeve_paper_heartbeat.json"] = 11.0       # always 11 min old when looked at,
    for _ in range(6):                                     # i.e. written between every two runs
        rc, bad = host.run()
        assert any("looks stalled (heartbeat 11 min old)" in m for _, m in _tasks(bad))
    assert scheduler.sent == []


def test_a_bot_without_the_single_instance_lock_is_not_restarted_automatically(
        host, scheduler, monkeypatch):
    """The serious risk: if Stop-ScheduledTask ends the launcher but leaves python.exe,
    the Start adds a second trader on the same ledger. Only a bot that holds the lock
    is restarted - a survivor then keeps the lock and the new copy stands down."""
    monkeypatch.setattr(hc, "_lock_held", lambda name: False)      # code from before the lock
    host.run()
    del host.fresh["sleeve_paper_heartbeat.json"]
    host.world.age("sleeve_paper_heartbeat.json", 120)
    for _ in range(6):                                     # said at once, and never done
        _, bad = host.run()
        ((status, msg),) = _tasks(bad)
        assert status == hc.WARN and "NOT restarted automatically" in msg
        assert "Stop-ScheduledTask PolybotSleeves" in msg and "leftover python.exe" in msg
    assert scheduler.sent == []


def test_a_process_that_outlives_the_stop_blocks_the_restart_and_is_named(
        host, scheduler, monkeypatch):
    """The lock at work: python.exe survives Stop-ScheduledTask and still holds the
    lock. A new copy could only stand down at it, so none is started - no second trader -
    and the watchdog says which process is in the way, now and at every run after."""
    monkeypatch.setattr(hc, "_lock_held", lambda name: True)        # the survivor's lock
    monkeypatch.setattr(hc, "_lock_holder", lambda name: "a process (pid 4321)")
    host.run()
    del host.fresh["sleeve_paper_heartbeat.json"]
    host.world.age("sleeve_paper_heartbeat.json", 60)
    host.run()
    _, bad = host.run()
    assert scheduler.sent == ["Stop-ScheduledTask -TaskName PolybotSleeves"]
    ((status, msg),) = _tasks(bad)
    assert status == hc.FAIL and "pid 4321" in msg and "outlived the stop" in msg
    _, bad = host.run()                                             # the task is Ready now
    ((status, msg),) = _tasks(bad)
    assert status == hc.FAIL and "cannot be started: a process (pid 4321) still holds" in msg
    assert len(scheduler.sent) == 1                                 # and no pointless start


def test_without_its_state_file_the_watchdog_restarts_no_running_task(host, scheduler):
    (host.world.root / "logs").rmdir()
    (host.world.root / "logs").write_text("not a folder", encoding="utf-8")
    del host.fresh["stage0_crypto.jsonl"]
    host.world.age("stage0_crypto.jsonl", 180)
    for _ in range(4):
        _, bad = host.run()
        assert any("NOT restarted" in m for _, m in _tasks(bad))
    assert scheduler.sent == []


def test_a_dead_task_is_started_and_the_start_is_checked(host, scheduler):
    """The case --repair was written for still works: state Ready, one start, healthy."""
    scheduler.states["PolybotStage0"] = "Ready"
    rc, bad = host.run()
    assert scheduler.sent == ["Start-ScheduledTask -TaskName PolybotStage0"]
    assert _tasks(bad) == [(hc.WARN, "PolybotStage0 was 'Ready' (dead) - REPAIRED, restarted")]
    assert scheduler.commands[-1].startswith("Get-ScheduledTask PolybotStage0 ")
    rc, bad = host.run()
    assert rc == 0 and _tasks(bad) == [] and len(scheduler.sent) == 1


def test_a_start_that_does_not_take_is_never_called_repaired(host, scheduler):
    """news-health#2, second half: a task that is Ready and will NOT start (disabled,
    python moved, crash at import) logged 'REPAIRED, restarted' on every run - three
    runs, three identical lines, exit code 0."""
    scheduler.states["PolybotPaper"] = "Ready"
    scheduler.start_works = False
    rc, bad = host.run()
    ((status, msg),) = _tasks(bad)
    assert status == hc.WARN and "NOT repaired (attempt 1)" in msg and "REPAIRED," not in msg
    for attempt in (2, 3):
        rc, bad = host.run()
        ((status, msg),) = _tasks(bad)
        assert rc == 1 and status == hc.FAIL, (attempt, msg)
        assert f"will not start (attempt {attempt} in a row) - REPAIR FAILED" in msg
        assert "py scripts\\paper_trader.py run" in msg
    assert scheduler.sent == ["Start-ScheduledTask -TaskName PolybotPaper"] * 3
    assert "UNHEALTHY" in host.log().splitlines()[-2]


def test_a_task_that_starts_and_dies_again_fails_on_the_second_repair(host, scheduler):
    """Asking for the state right after a start can still read Running for a task that
    crashes at import a moment later. Counting attempts across runs catches it: the
    second repair in a row is a FAIL, and the row ends only after a healthy hour."""
    scheduler.states["PolybotSleeves"] = "Ready"
    _, bad = host.run()
    assert _tasks(bad)[0] == (hc.WARN, "PolybotSleeves was 'Ready' (dead) - REPAIRED, restarted")
    scheduler.states["PolybotSleeves"] = "Ready"           # it crashed again
    rc, bad = host.run()
    ((status, msg),) = _tasks(bad)
    assert rc == 1 and status == hc.FAIL and "attempt 2 in a row" in msg
    assert "15 min after the last start" in msg and "not staying up" in msg
    for _ in range(5):                                     # 75 healthy minutes
        rc, bad = host.run()
        assert rc == 0 and _tasks(bad) == []
    scheduler.states["PolybotSleeves"] = "Ready"           # a new, separate death
    _, bad = host.run()
    assert _tasks(bad)[0][0] == hc.WARN and "REPAIRED, restarted" in _tasks(bad)[0][1]


def test_a_death_after_a_night_asleep_is_a_new_incident(host, scheduler):
    """'In a row' is about runs that follow each other. A task repaired in the evening
    and found dead again when the PC wakes eight hours later is started as a first
    attempt - and fails only if THAT start does not hold either."""
    scheduler.states["PolybotStage0"] = "Ready"
    _, bad = host.run()
    assert "REPAIRED, restarted" in _tasks(bad)[0][1]
    host.run()
    scheduler.states["PolybotStage0"] = "Ready"
    _, bad = host.run(8 * 60)                              # the wake-up run
    assert _tasks(bad) == [(hc.WARN, "PolybotStage0 was 'Ready' (dead) - REPAIRED, restarted")]
    scheduler.states["PolybotStage0"] = "Ready"            # ...and it does not hold
    _, bad = host.run()
    assert _tasks(bad)[0][0] == hc.FAIL and "attempt 2 in a row" in _tasks(bad)[0][1]


def test_a_task_that_is_down_while_its_bot_is_alive_is_not_started(
        host, scheduler, monkeypatch):
    """The bot runs outside the task (started by hand, or a python.exe that outlived a
    stop) and holds the lock. A start could only stand down at the lock - or, for code
    from before the lock, would have been a second trader on the ledger."""
    scheduler.states["PolybotPaper"] = "Ready"
    monkeypatch.setattr(hc, "_lock_held", lambda name: True)
    rc, bad = host.run()
    ((status, msg),) = _tasks(bad)
    assert status == hc.WARN and "running outside Task Scheduler" in msg and rc == 0
    del host.fresh["paper_crypto15.jsonl"]                 # ...and now it stops working
    host.world.age("paper_crypto15.jsonl", 3 * 1440)
    rc, bad = host.run()
    ((status, msg),) = _tasks(bad)
    assert status == hc.FAIL and "still holds its single-instance lock" in msg
    assert scheduler.sent == []


def _regime_log(world, monkeypatch, hours_old):
    from lib import news_regime as nr
    p = world.path("news_regime.jsonl")
    p.write_text(json.dumps({"t": "regime", "ts": _iso(world.now - hours_old * HOUR),
                             "regime": "good", "mult": 1.0, "reasons": []}) + "\n",
                 encoding="utf-8")
    monkeypatch.setattr(nr, "REGIME_LOG", p)


def test_a_hung_news_run_is_stopped_and_started_but_a_ready_task_is_left_alone(
        host, scheduler, monkeypatch):
    """PolybotNews runs hourly and exits: Ready is normal and is never 'repaired'. A run
    that hangs stays Running, every later hourly start is ignored, and the regime goes
    stale for good - that, seen at two runs, is stopped and started."""
    _regime_log(host.world, monkeypatch, hours_old=5.0)
    for _ in range(4):                                     # Ready with a stale regime
        host.run()
    assert scheduler.sent == []
    scheduler.states["PolybotNews"] = "Running"            # a run that never returns
    _, bad = host.run()
    assert scheduler.sent == [] and _tasks(bad) == []      # first look
    _, bad = host.run()
    assert scheduler.sent == _stop_start("PolybotNews")
    ((status, msg),) = _tasks(bad)
    assert status == hc.WARN and "regime 6.2 h old" in msg and "stopped and started" in msg

    scheduler.commands.clear()                             # Running with a FRESH regime
    _regime_log(host.world, monkeypatch, hours_old=0.2)
    for _ in range(6):
        host.run()
    assert scheduler.sent == []


def test_an_unanswered_query_says_how_long_it_has_been_unanswered(host, scheduler):
    scheduler.answers = False
    _, bad = host.run()
    assert "could not query Task Scheduler" in _tasks(bad)[0][1]
    assert "min now" not in _tasks(bad)[0][1]
    host.run()
    _, bad = host.run()
    assert "(no answer for 30 min now)" in _tasks(bad)[0][1] and _tasks(bad)[0][0] == hc.WARN


def test_the_lock_probe_tells_a_held_lock_from_a_free_one(tmp_path, monkeypatch):
    """The probe behind 'only a bot that holds the lock is restarted', against the real
    lib.single_instance on this OS, then through its Windows branch with a fake msvcrt."""
    import errno
    from lib import single_instance as si
    monkeypatch.setenv("POLYBOT_LOCK_DIR", str(tmp_path))
    assert hc._lock_held("paper_trader") is False          # no lock file: nobody holds it
    assert si.hold("paper_trader") is True
    try:
        assert hc._lock_held("paper_trader") is True
        assert hc._lock_holder("paper_trader") == f"a process (pid {os.getpid()})"
    finally:
        si.release("paper_trader")
    assert hc._lock_held("paper_trader") is False
    assert si.hold("paper_trader") is True                 # and the probe left it free
    si.release("paper_trader")

    class FakeMsvcrt:
        LK_NBLCK, LK_UNLCK = 2, 0

        def __init__(self, err=None):
            self.err, self.calls = err, []

        def locking(self, fd, mode, nbytes):
            self.calls.append((mode, os.lseek(fd, 0, os.SEEK_CUR)))
            if self.err is not None:
                raise OSError(self.err, "locking failed")

    free = FakeMsvcrt()
    assert hc._lock_held("paper_trader", windows=True, msvcrt_mod=free) is False
    assert free.calls == [(2, 0), (0, 0)]                  # took byte 0, gave byte 0 back
    assert hc._lock_held("paper_trader", windows=True,
                         msvcrt_mod=FakeMsvcrt(errno.EACCES)) is True
    assert hc._lock_held("paper_trader", windows=True,
                         msvcrt_mod=FakeMsvcrt(errno.ENOLCK)) is None
