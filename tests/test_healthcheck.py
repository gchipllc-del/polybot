"""healthcheck - the watchdog's judgements, each one pinned.

The watchdog is the layer that is supposed to notice when everything else has gone
quiet, so every test here is a scenario in which it once said the wrong thing. Windows
Task Scheduler is faked through the module's own seams (hc.IS_WIN, hc._ps); nothing here
starts PowerShell.
"""
import json
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
    return out


@pytest.fixture
def windows(monkeypatch):
    """check_tasks believes it is on Windows; the test installs its own scheduler."""
    monkeypatch.setattr(hc, "IS_WIN", True)

    def install(sched):
        monkeypatch.setattr(hc, "_ps", sched)
        return sched
    return install


def _by_status(results, status):
    return [msg for st, _, msg in results if st == status]


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
