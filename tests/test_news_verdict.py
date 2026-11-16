"""news_agent report - how the news agent is JUDGED, each property pinned.

The verdict rule is pre-registered (scripts/news_agent.py docstring): a cluster is one
ET day x one regime, and the agent earns its keep only when good days beat cautious days
per window over at least MIN_CLUSTERS clusters of each. These tests pin the places the
review found the code drifting from that rule. Fixtures go through the real
vol_replay.ledger_rows join, the way `report` reads the paper ledger.
"""
import json
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


# ── a window straddling a regime update splits by regime (review #9) ──────────

def test_window_straddling_a_regime_update_splits_by_regime():
    """PolybotNews writes a new regime once an hour. The 15-minute window that
    straddles it can hold an early entry under the old regime and a late one under the
    new. Booked whole to the first trade's regime, the cautious -0.90 counted as GOOD
    while the counterfactual halved it as cautious."""
    rows = [
        {"t": "open", "ts": "2026-12-01T15:16:00+00:00", "rule": "CONTROL_midprice",
         "ticker": "KXBTC15M-26DEC011030-64000", "regime": nr.GOOD},
        {"t": "open", "ts": "2026-12-01T15:29:00+00:00", "rule": "H1_settlement_lag",
         "ticker": "KXBTC15M-26DEC011030-64100", "regime": nr.CAUTIOUS},
        {"t": "close", "ts": "2026-12-01T15:31:00+00:00", "rule": "CONTROL_midprice",
         "ticker": "KXBTC15M-26DEC011030-64000", "pnl": 0.48, "won": True},
        {"t": "close", "ts": "2026-12-01T15:31:00+00:00", "rule": "H1_settlement_lag",
         "ticker": "KXBTC15M-26DEC011030-64100", "pnl": -0.90, "won": False},
    ]
    trades = vr.ledger_rows(rows)["trades"]
    assert len({t["window"] for t in trades}) == 1           # one window, two stamps
    ev = na.evaluate(trades)
    assert ev["clusters"] == (1, 1)
    assert ev["table"][nr.GOOD][:2] == (1, 0.48)
    assert ev["table"][nr.CAUTIOUS][:2] == (1, -0.90)
    assert ev["n_windows"] == 2


def test_same_regime_trades_in_a_window_stay_one_observation():
    """The split is by regime only: same-stamp trades in a window are still ONE bet."""
    at = _utc(date(2026, 12, 1), 10, 16)
    ev = na.evaluate(_trades([(at, nr.GOOD, 0.4, "A", 64000),
                              (at + timedelta(minutes=10), nr.GOOD, -0.2, "B", 64100)]))
    assert ev["n_windows"] == 1 and ev["clusters"] == (1, 0)
    assert abs(ev["table"][nr.GOOD][1] - 0.1) < 1e-12


# ── ONE look: the verdict is evaluated once and frozen (review #10) ───────────

D0 = date(2027, 1, 4)                    # 100 ET days from here stay on EST


def _null_days(n: int = 40) -> list:
    """n ET days alternating good / cautious with the SAME P&L pattern: 20 + 20
    clusters after 40 days, good minus cautious exactly 0 -> NO DIFFERENCE."""
    return [(_utc(D0 + timedelta(days=i), 10), nr.GOOD if i % 2 == 0 else nr.CAUTIOUS,
             0.05 * ((i // 2) % 3 - 1)) for i in range(n)]


def _diverging_days(start: int = 40, n: int = 60) -> list:
    """Later days on which cautious days run far worse than good ones."""
    out = []
    for i in range(start, start + n):
        caut = i % 2 == 1
        out.append((_utc(D0 + timedelta(days=i), 10), nr.CAUTIOUS if caut else nr.GOOD,
                    (-0.20 if caut else 0.10) + 0.01 * (i % 4)))
    return out


def _report(monkeypatch, tmp_path, capsys, spec) -> str:
    led = tmp_path / "paper_crypto15.jsonl"
    led.write_text("".join(json.dumps(r) + "\n" for r in _rows(spec)), encoding="utf-8")
    monkeypatch.setattr(vr, "LEDGER", led)
    monkeypatch.setattr(na, "VERDICT_PATH", tmp_path / "news_verdict.json", raising=False)
    monkeypatch.setattr(nr, "load_history", lambda *a, **k: [])
    assert na.cmd_report() == 0
    return capsys.readouterr().out


def _verdict(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("VERDICT:"))


def test_the_rule_would_flip_on_the_running_totals():
    """The fixture the tests below rely on: at the evaluation point the rule says NO
    DIFFERENCE; applied to everything collected 60 days later it says EARNS ITS KEEP."""
    assert na.evaluate(_trades(_null_days()))["verdict"].startswith("NO DIFFERENCE")
    late = na.evaluate(_trades(_null_days() + _diverging_days()))
    assert late["clusters"] == (50, 50) and late["verdict"].startswith("EARNS ITS KEEP")


def test_verdict_is_evaluated_once_and_frozen(monkeypatch, tmp_path, capsys):
    """The report re-tested a fixed 95% interval on every run, so the first time noise
    cleared zero it printed EARNS ITS KEEP or INVERTED (~20% of null runs checked daily,
    not 5%). Now the first report at the evaluation point freezes the verdict and later
    reports print that one, with the running numbers labelled descriptive."""
    first = _report(monkeypatch, tmp_path, capsys, _null_days(41))   # + 1 later day
    assert _verdict(first).startswith("VERDICT: NO DIFFERENCE"), _verdict(first)
    later = _report(monkeypatch, tmp_path, capsys, _null_days() + _diverging_days())
    assert _verdict(later).startswith("VERDICT: NO DIFFERENCE"), _verdict(later)
    assert "descriptive" in later and "frozen" in later
    vfile = tmp_path / "news_verdict.json"
    rec = json.loads(vfile.read_text(encoding="utf-8"))
    assert rec["verdict"].startswith("NO DIFFERENCE") and rec["clusters"] == [20, 20]
    assert rec["eval_day_et"] == (D0 + timedelta(days=39)).isoformat()


def test_frozen_file_is_never_rewritten(monkeypatch, tmp_path, capsys):
    _report(monkeypatch, tmp_path, capsys, _null_days(41))
    vfile = tmp_path / "news_verdict.json"
    frozen = vfile.read_bytes()
    for extra in (20, 40, 60):
        _report(monkeypatch, tmp_path, capsys, _null_days() + _diverging_days(n=extra))
        assert vfile.read_bytes() == frozen


def test_the_evaluation_point_does_not_move_with_when_report_runs(monkeypatch, tmp_path,
                                                                  capsys):
    """A report first run 60 days after the gate was met evaluates the data through the
    first ET day both regimes had MIN_CLUSTERS clusters - not everything since. So how
    often (or when) the operator runs the report cannot pick the evaluation point."""
    out = _report(monkeypatch, tmp_path, capsys, _null_days() + _diverging_days())
    assert _verdict(out).startswith("VERDICT: NO DIFFERENCE"), _verdict(out)
    rec = json.loads((tmp_path / "news_verdict.json").read_text(encoding="utf-8"))
    assert rec["eval_day_et"] == (D0 + timedelta(days=39)).isoformat()
    assert rec["clusters"] == [20, 20] and rec["trades"] == 40


def test_collecting_writes_no_verdict(monkeypatch, tmp_path, capsys):
    out = _report(monkeypatch, tmp_path, capsys, _null_days(30))
    assert _verdict(out) == ("VERDICT: COLLECTING - 15 good-day and 15 cautious-day "
                             "clusters (need 20 of each)")
    assert not (tmp_path / "news_verdict.json").exists()


def test_an_unreadable_verdict_file_is_left_alone(monkeypatch, tmp_path, capsys):
    """A frozen verdict is a record: a damaged file is reported, never overwritten. The
    verdict shown is recomputed at the SAME evaluation point - not a second look."""
    vfile = tmp_path / "news_verdict.json"
    vfile.write_text("{not json", encoding="utf-8")
    out = _report(monkeypatch, tmp_path, capsys, _null_days() + _diverging_days())
    assert _verdict(out).startswith("VERDICT: NO DIFFERENCE"), _verdict(out)
    assert "NOT FROZEN" in out and "cannot be read" in out
    assert vfile.read_text(encoding="utf-8") == "{not json"


def test_freeze_verdict_states(tmp_path):
    vp = tmp_path / "v.json"
    assert na.freeze_verdict(_trades(_null_days(38)), vp) is None
    assert not vp.exists()
    first = na.freeze_verdict(_trades(_null_days(41)), vp, now=datetime(2027, 2, 13,
                                                                         tzinfo=timezone.utc))
    assert first["state"] == "new" and first["frozen_at"] == "2027-02-13T00:00:00+00:00"
    stored = na.freeze_verdict(_trades(_null_days() + _diverging_days()), vp)
    assert stored["state"] == "stored"
    assert {k: v for k, v in stored.items() if k != "state"} == \
        {k: v for k, v in first.items() if k != "state"}
    assert na.load_verdict(vp)["verdict"] == first["verdict"]
    assert na.load_verdict(tmp_path / "missing.json") is None


def test_evaluation_cut_is_the_first_day_both_regimes_reach_the_gate():
    day, upto = na.evaluation_cut(_trades(_null_days() + _diverging_days()))
    assert day == (D0 + timedelta(days=39)).isoformat() and len(upto) == 40
    assert na.evaluation_cut(_trades(_null_days(39))) == (None, [])
    # unknown-regime days count toward neither regime
    unk = [(_utc(D0 + timedelta(days=100 + i), 10), nr.UNKNOWN, 0.0) for i in range(30)]
    assert na.evaluation_cut(_trades(_null_days(38) + unk))[0] is None


# ── the evaluation day must be COMPLETE before the verdict freezes (verifier) ─

def test_a_report_during_the_evaluation_day_freezes_nothing(monkeypatch, tmp_path, capsys):
    """A report run on the evaluation day itself saw only part of it: it froze one
    verdict, and the same ledger reported after the day froze another. Now it says
    PENDING and writes nothing; after the day, it freezes exactly what a fresh
    recomputation (a lost file) freezes."""
    vfile = tmp_path / "news_verdict.json"
    out = _report(monkeypatch, tmp_path, capsys, _null_days())          # D is the last day
    assert _verdict(out).startswith("VERDICT: PENDING"), _verdict(out)
    assert not vfile.exists()
    _report(monkeypatch, tmp_path, capsys, _null_days(41))               # D is over
    frozen = json.loads(vfile.read_text(encoding="utf-8"))
    vfile.unlink()                                                       # file lost
    _report(monkeypatch, tmp_path, capsys, _null_days() + _diverging_days())
    again = json.loads(vfile.read_text(encoding="utf-8"))
    drop = ("frozen_at",)
    assert {k: v for k, v in frozen.items() if k not in drop} == \
        {k: v for k, v in again.items() if k not in drop}


def test_an_unsettled_position_holds_the_freeze_until_it_is_stuck(tmp_path):
    vp = tmp_path / "v.json"
    day = (D0 + timedelta(days=39)).isoformat()
    hold = na.freeze_verdict(_trades(_null_days(41)), vp, unsettled_days={day})
    assert hold["state"] == "pending" and "not settled" in hold["note"] and not vp.exists()
    # trades from two later ET days have settled: the position is stuck - counted, not awaited
    done = na.freeze_verdict(_trades(_null_days(42)), vp, unsettled_days={day})
    assert done["state"] == "new" and done["unsettled_left_out"] == 1
    assert done["eval_day_et"] == day and done["clusters"] == [20, 20]
