"""healthcheck - the watchdog's judgements, each one pinned.

The watchdog is the layer that is supposed to notice when everything else has gone
quiet, so every test here is a scenario in which it once said the wrong thing. Windows
Task Scheduler is faked through the module's own seams (hc.IS_WIN, hc._ps); nothing here
starts PowerShell.
"""
import sys
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
