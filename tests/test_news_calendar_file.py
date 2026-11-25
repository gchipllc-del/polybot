"""The shipped config/news_calendar.yaml - the one input the news agent cannot discover.
A wrong or malformed date silently makes an event day read as an ordinary one, so the
file itself is pinned: parseable dates, known release times, no duplicates."""
from datetime import date
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import news_regime as nr  # noqa: E402

CAL = ROOT / "config" / "news_calendar.yaml"


def _events():
    return yaml.safe_load(CAL.read_text(encoding="utf-8"))["events"]


def test_every_entry_is_well_formed():
    evs = _events()
    assert len(evs) >= 10
    for e in evs:
        assert isinstance(e["date"], str) and date.fromisoformat(e["date"]), e
        assert e["impact"] in ("high", "medium", "low"), e
        want = "14:00" if e["event"].startswith("FOMC") else "08:30"
        assert e["time_et"] == want, e
    keys = [(e["date"], e["event"]) for e in evs]
    assert len(keys) == len(set(keys)) and keys == sorted(keys)


def test_known_2026_dates_are_present():
    days = {(e["date"], e["event"].split()[0]) for e in _events()}
    assert ("2026-12-09", "FOMC") in days      # Fed's published 2026 schedule
    assert ("2026-12-10", "CPI") in days
    assert ("2026-12-04", "Jobs") in days


def test_fomc_day_reads_cautious_and_ppi_alone_does_not():
    evs = [{"date": e["date"], "time_et": e["time_et"], "event": e["event"],
            "impact": e["impact"]} for e in _events()]
    assert nr.calendar_vote(evs, "2026-12-09")["vote"] == nr.CAUTIOUS
    ppi_only = next(e["date"] for e in evs if e["event"] == "PPI release")
    assert not any(e["date"] == ppi_only and e["impact"] == "high" for e in evs)
    assert nr.calendar_vote(evs, ppi_only)["vote"] is None
