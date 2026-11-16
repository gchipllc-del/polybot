"""news_agent report - how the news agent is JUDGED, each property pinned.

The verdict rule is pre-registered (scripts/news_agent.py docstring): a cluster is one
ET day x one regime, and the agent earns its keep only when good days beat cautious days
per window over at least MIN_CLUSTERS clusters of each. These tests pin the places the
review found the code drifting from that rule. Fixtures go through the real
vol_replay.ledger_rows join, the way `report` reads the paper ledger.
"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import news_agent as na  # noqa: E402
import vol_replay as vr  # noqa: E402
from lib import news_regime as nr  # noqa: E402


def _utc(day: date, hh: int, mm: int = 0) -> datetime:
    """ET wall-clock time on `day` -> the UTC instant (EDT or EST, as on that date)."""
    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)
    utc = local + timedelta(hours=5)
    if nr.et_offset_hours(utc) == -4:
        utc = local + timedelta(hours=4)
    return utc


def _ticker(open_utc: datetime, strike: int) -> str:
    """SERIES-YYMMMDDHHMM-STRIKE, named (like Kalshi's) by the window's ET close."""
    et = open_utc + timedelta(hours=nr.et_offset_hours(open_utc))
    close = et.replace(second=0, microsecond=0) + timedelta(minutes=15 - et.minute % 15)
    return f"KXBTC15M-{close:%y%b%d%H%M}".upper() + f"-{strike}"


def _rows(trades) -> list[dict]:
    """[(open_utc, regime, pnl[, rule[, strike]])] -> paper-ledger rows as the trader
    writes them: each open stamped (UTC ts) with the regime in force, then its close.
    Closes follow in open order, as settle_positions emits them."""
    opens, closes = [], []
    for at, regime, pnl, *rest in trades:
        rule = rest[0] if rest else "R"
        strike = rest[1] if len(rest) > 1 else 64000
        tk = _ticker(at, strike)
        opens.append({"t": "open", "ts": at.isoformat(), "ticker": tk, "rule": rule,
                      "regime": regime, "regime_mult": nr.SIZE_MULT[regime],
                      "contracts": 1})
        closes.append({"t": "close", "ts": (at + timedelta(minutes=16)).isoformat(),
                       "ticker": tk, "rule": rule, "pnl": pnl, "won": pnl > 0})
    return opens + closes


def _trades(trades) -> list[dict]:
    return vr.ledger_rows(_rows(trades))["trades"]


# ── clusters are ET days of the OPEN, not UTC dates (review #2/#8/#13) ─────────

def test_one_et_day_is_one_cluster_across_utc_midnight():
    """10:00 and 21:00 ET on one EST day are 15:00Z that day and 02:00Z the next. As
    UTC dates they made one calendar-cautious ET day count as two clusters."""
    d = date(2026, 12, 9)
    trades = _trades([(_utc(d, 10), nr.CAUTIOUS, -0.5), (_utc(d, 21), nr.CAUTIOUS, -0.5)])
    assert [t["date"] for t in trades] == ["2026-12-09", "2026-12-10"]  # UTC: unchanged
    assert na.evaluate(trades)["clusters"] == (0, 1)


def test_cluster_gate_needs_twenty_et_days_not_ten():
    """36 ET days, every third one calendar-cautious, a trade each morning and evening
    (ET). With UTC-date clusters, 12 cautious ET days passed the 20-cluster gate as 24
    and the verdict went decisive; on ET days it is 24 good / 12 cautious: COLLECTING."""
    spec = []
    for i in range(36):
        d = date(2027, 1, 4) + timedelta(days=i)
        caut = i % 3 == 2
        base = -0.10 if caut else 0.05
        for hh, k in ((10, 0), (21, 1)):
            spec.append((_utc(d, hh), nr.CAUTIOUS if caut else nr.GOOD,
                         base + 0.01 * ((i + k) % 5)))
    ev = na.evaluate(_trades(spec))
    assert ev["clusters"] == (24, 12)
    assert ev["verdict"].startswith("COLLECTING"), ev["verdict"]


@pytest.mark.parametrize("open_ts, day", [
    ("2026-07-01T03:30:00+00:00", "2026-06-30"),    # 23:30 EDT
    ("2026-07-01T04:30:00+00:00", "2026-07-01"),    # 00:30 EDT
    ("2026-12-01T04:30:00+00:00", "2026-11-30"),    # 23:30 EST
    ("2026-12-01T05:30:00+00:00", "2026-12-01"),    # 00:30 EST
    ("2026-12-01T04:30:00Z", "2026-11-30"),          # 'Z' suffix
    ("2026-12-01T04:30:00", "2026-11-30"),           # naive = UTC, as the trader writes
    ("2026-12-01T00:30:00-04:00", "2026-11-30"),     # any offset: converted to UTC first
])
def test_cluster_day_is_the_et_day_of_the_open(open_ts, day):
    assert na.cluster_day({"open_ts": open_ts, "date": "1999-01-01"}) == day


def test_trades_without_an_open_ts_fall_back_to_their_date():
    """Callers (and older fixtures) that build trades with a 'date' only keep working;
    an unreadable open_ts is no better than none."""
    assert na.cluster_day({"date": "2026-12-09"}) == "2026-12-09"
    assert na.cluster_day({"open_ts": None, "date": "2026-12-09"}) == "2026-12-09"
    assert na.cluster_day({"open_ts": "garbage-value", "date": "2026-12-09"}) == "2026-12-09"
    assert na.cluster_day({"open_ts": "2026-12-09", "date": "2026-12-09"}) == "2026-12-09"


def test_ledger_rows_carry_the_open_timestamp():
    """vol_replay's own reports keep their UTC 'date'; the open ts rides alongside."""
    at = _utc(date(2026, 12, 9), 21)
    t = _trades([(at, nr.GOOD, 0.1)])[0]
    assert t["open_ts"] == at.isoformat() and t["date"] == "2026-12-10"
    assert vr._per_window([t]) == [("2026-12-10", 0.1)]
