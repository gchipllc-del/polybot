"""sleeve_paper - the trader keeps one honest ledger when things around it go wrong:
a second copy of itself, a torn write, a duplicated row. (Audit findings, 2026-11-25.)"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import sleeve_paper as sp  # noqa: E402
from lib import single_instance as si  # noqa: E402

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
GOOD = {"regime": "good", "mult": 1.0}
RUN_CYCLE = sp.run_cycle             # the real one, whatever a test puts in its place


def mk(tk, ev, ya_c, yb_c, opened_h=-10, closes_h=10, **kw):
    return dict({"ticker": tk, "event_ticker": ev, "yes_ask": ya_c, "yes_bid": yb_c,
                 "open_time": (NOW + timedelta(hours=opened_h)).isoformat(),
                 "close_time": (NOW + timedelta(hours=closes_h)).isoformat()}, **kw)


def held(tk, rule="R", closed_h=-1.0, **kw):
    """An open ledger row: a 10-lot NO at 98c whose market closed `closed_h` hours ago."""
    ev = tk.rsplit("-", 1)[0]
    return dict({"t": "open", "ts": (NOW + timedelta(hours=closed_h - 2)).isoformat(),
                 "ticker": tk, "rule": rule, "event": ev, "series": ev.split("-")[0],
                 "side": "no", "price": 0.98, "contracts": 10, "fee": 0.02, "depth": 50.0,
                 "close_time": (NOW + timedelta(hours=closed_h)).isoformat(), "v": 2}, **kw)


def favorites(rp, name="R", series=("S",), **kw):
    sp.register([dict({"name": name, "series": list(series), "band": "any",
                       "side": "favorite", "lo": 0.97, "hi": 0.995, "contracts": 10}, **kw)],
                rp, NOW - timedelta(days=30))


def cycle(get, rp, lp, at=NOW, depth=50, **kw):
    kw.setdefault("heartbeat", None)
    return sp.run_cycle(get, at, rp, lp, depth_fn=lambda t, s, p: depth,
                        regime_fn=lambda: GOOD, **kw)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """main() pointed at a scratch folder - registry, ledger, heartbeat AND the lock."""
    for name, file in (("RULES", "rules.jsonl"), ("LEDGER", "ledger.jsonl"),
                       ("HEARTBEAT", "hb.json")):
        monkeypatch.setattr(sp, name, tmp_path / file)
    monkeypatch.delenv("POLYBOT_LOCK_DIR", raising=False)
    yield tmp_path
    si.release("sleeve_paper")


def run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["sleeve_paper.py", *argv])
    return sp.main()


def offline(monkeypatch, get, calls=None, at=NOW, depth=50, regime=lambda: GOOD):
    """main()'s run_cycle with the network, the clock, the order book and the news regime
    replaced - and nothing else: the paths, the state and the log are main()'s own."""
    def run_cycle(**kw):
        assert {"rules_path", "ledger", "heartbeat", "log"} <= set(kw), kw   # never the
        if calls is not None:                                # import-time default paths
            calls.append(kw)
        return RUN_CYCLE(get, at() if callable(at) else at, depth_fn=lambda t, s, p: depth,
                         regime_fn=regime, **kw)
    monkeypatch.setattr(sp, "run_cycle", run_cycle)


class Clock:
    """Stands in for the `time` module inside sleeve_paper: the run loop's end-of-cycle
    sleep moves it to the next cycle and stops the loop after `cycles` of them."""

    def __init__(self, cycles):
        self.t, self.cycles, self.n = NOW.timestamp(), cycles, 0

    def time(self):
        return self.t

    def now(self):
        return datetime.fromtimestamp(self.t, tz=timezone.utc)

    def sleep(self, seconds):
        self.t += seconds
        if seconds >= 1.0:
            self.n += 1
            if self.n >= self.cycles:
                raise KeyboardInterrupt


def run_loop(monkeypatch, cycles, get, **kw):
    """The real `run` loop for `cycles` cycles on a virtual clock."""
    clock = Clock(cycles)
    monkeypatch.setattr(sp, "time", clock)
    monkeypatch.setattr(sp, "_now", clock.now)
    offline(monkeypatch, get, at=clock.now, **kw)
    with pytest.raises(KeyboardInterrupt):
        run_main(monkeypatch, "run")
    return clock


OLD_KEYS = ("ts", "rules", "series", "opened", "closed", "skipped", "ungated", "errors",
            "regime", "open_now")


# ── one trader per ledger ─────────────────────────────────────────────────────

HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from lib import single_instance as si
si.hold("sleeve_paper", Path(sys.argv[2]))
print("HOLDING", flush=True)
time.sleep(60)
"""


def test_a_second_trader_stands_down(paths, monkeypatch, capsys):
    """run_cycle reads the ledger first and appends last. A second process inside that
    window (a hand-run `cycle` while the task is up) wrote every entry and settlement of
    the cycle twice: 11 duplicate opens and 4 duplicate closes in the audit's two-process
    run. `run` and `cycle` now stand down while another trader holds this ledger."""
    monkeypatch.setattr(sp, "run_cycle",
                        lambda *a, **k: pytest.fail("a second trader ran a cycle"))
    other = subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT), str(paths / "locks")],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "HOLDING"
        for cmd in ("cycle", "run"):
            assert run_main(monkeypatch, cmd) == 1
            out = capsys.readouterr().out
            assert out.startswith("not starting: sleeve_paper is already running")
            assert f"pid {other.pid}" in out and len(out.splitlines()) == 1
    finally:
        other.kill()
        other.wait(timeout=10)


def test_the_lock_is_taken_in_main_only_and_follows_the_ledger(paths, monkeypatch, capsys):
    """Tests and the selftest call run_cycle directly and must never be blocked by (or
    block) the live task: only main() locks, and a ledger outside the repo's data folder
    is guarded by a lock next to it, not by the live trader's."""
    favorites(sp.RULES)
    cycle(lambda url, params=None: {"markets": []}, sp.RULES, sp.LEDGER)
    assert "sleeve_paper" not in si._HELD                    # run_cycle itself: no lock
    seen = []
    offline(monkeypatch, lambda url, params=None: {"markets": []}, seen)
    assert run_main(monkeypatch, "cycle") == 0 and len(seen) == 1
    assert "sleeve_paper" in si._HELD and (paths / "locks" / "sleeve_paper.lock").exists()
    assert run_main(monkeypatch, "cycle") == 0               # our own lock never blocks us
    for cmd in ("report", "rules"):                          # reading never needs the lock
        si.release("sleeve_paper")
        assert run_main(monkeypatch, cmd) == 0 and "sleeve_paper" not in si._HELD
    monkeypatch.setattr(sp, "LEDGER", ROOT / "data" / "paper_sleeves.jsonl")
    assert sp._lock_dir() is None                            # the real ledger: shared folder
    monkeypatch.setenv("POLYBOT_LOCK_DIR", str(paths / "elsewhere"))
    monkeypatch.setattr(sp, "LEDGER", paths / "ledger.jsonl")
    assert sp._lock_dir() is None                            # an explicit lock folder wins


# ── a torn write ──────────────────────────────────────────────────────────────

def test_a_torn_last_line_costs_one_row_not_the_next_entry(tmp_path):
    """A process killed mid-append leaves the ledger without its final newline. The next
    row was glued to the torn line and lost with it: the cycle said opened=1, the entry
    was unreadable, and once the quote left the band it was never made at all."""
    rp, lp = tmp_path / "rules.jsonl", tmp_path / "ledger.jsonl"
    favorites(rp)
    sp._append(lp, [held("S-E0-A")])
    with open(lp, "ab") as f:
        f.write(b'{"t":"close","ts":"2026-11-20T11:5')        # torn: no newline
    listed = [mk("S-E1-A", "S-E1", 3, 2)]                     # NO favorite at 98c

    def get(url, params=None):
        if url.endswith("/markets"):
            return {"markets": listed}
        return {"market": {"result": "no"}}

    s = cycle(get, rp, lp)
    assert (s["opened"], s["closed"]) == (1, 1)
    rows = sp._load_jsonl(lp)
    assert [(r["t"], r["ticker"]) for r in rows] == [
        ("open", "S-E0-A"), ("open", "S-E1-A"), ("close", "S-E0-A")]
    listed.clear()                                            # the quote has left the band
    s2 = cycle(get, rp, lp, at=NOW + timedelta(minutes=2))
    assert (s2["opened"], s2["closed"]) == (0, 0)             # nothing left to repeat
    assert set(sp.open_positions(sp._load_jsonl(lp))) == {("S-E1-A", "R")}
    assert lp.read_bytes().count(b"\n") == 4                  # the torn line stands alone


def test_registry_appends_survive_a_torn_line_too(tmp_path):
    rp = tmp_path / "rules.jsonl"
    favorites(rp, "A")
    with open(rp, "ab") as f:
        f.write(b'{"t":"rule","name":"TORN')
    favorites(rp, "B")
    sp._append(rp, [{"t": "retire", "name": "A"}])
    assert set(sp.active_rules(rp)) == {"B"} and sp.all_registered(rp) == {"A", "B"}


# ── a duplicated close row ────────────────────────────────────────────────────

def _hourly_record(n=120, losses=()):
    """n settled HOURLY_FAV-shaped bets: 10-lot NO favorites at 97-99c, one per event."""
    rows = []
    for i in range(n):
        price = (0.97, 0.98, 0.99, 0.98)[i % 4]
        o = held(f"S{i % 4}-E{i // 4}-T1", "HOURLY_FAV", price=price,
                 fee=sp.kalshi_fee(price, 10))
        rows += [o, sp.settle(o, "yes" if i in losses else "no")]
    return rows


def test_a_duplicated_close_row_is_scored_once():
    """One duplicated close counted its trade twice AND gave that event two trades, which
    moved the whole rule from the exact test to the approximate one: 120 straight wins at
    97-99c read p = 0.025 (EDGE on its own) instead of the exact 0.113."""
    rules = {"HOURLY_FAV": {"name": "HOURLY_FAV", "series": ["S0", "S1", "S2", "S3"],
                            "named_at": (NOW - timedelta(days=30)).isoformat()}}
    clean = _hourly_record()
    last_close = clean[-1]
    doubled = clean + [dict(last_close, ts=(NOW + timedelta(minutes=2)).isoformat())]
    a, b = sp.evaluate(clean, rules)[0], sp.evaluate(doubled, rules)[0]
    assert (a["events"], a["trades"]) == (120, 120) and abs(a["p"] - 0.1128) < 5e-4
    assert b == a                                             # same record, same verdict
    assert sp.duplicate_closes(clean) == {}
    assert sp.duplicate_closes(doubled) == {"HOURLY_FAV": 1}
    # a loss doubled is still one loss, and a close with no second copy is untouched
    lossy = _hourly_record(losses={40})
    twice = lossy + [dict(r) for r in lossy if r["t"] == "close" and not r["won"]]
    assert sp.evaluate(twice, rules)[0] == sp.evaluate(lossy, rules)[0]
    assert len(sp.trades_of(twice, rules)["HOURLY_FAV"]) == 120


def test_report_says_how_many_duplicate_closes_it_left_out(paths, monkeypatch, capsys):
    import lib.command_center as cc
    monkeypatch.setattr(cc, "CRYPTO_LEDGER", paths / "crypto.jsonl")
    favorites(sp.RULES, "HOURLY_FAV", ["S0", "S1", "S2", "S3"])
    rows = _hourly_record(8)
    sp._append(sp.LEDGER, rows)
    assert run_main(monkeypatch, "report") == 0
    assert "duplicate close" not in capsys.readouterr().out   # a clean ledger: no note
    sp._append(sp.LEDGER, [rows[1], rows[3], rows[3]])
    assert run_main(monkeypatch, "report") == 0
    out = capsys.readouterr().out
    assert "3 duplicate close row(s) are NOT counted above (HOURLY_FAV 3)" in out
    line = next(l for l in out.splitlines() if l.strip().startswith("HOURLY_FAV "))
    assert line.split()[1] == "8"                             # 8 events, not 11


# ── the fill check, shown where verdicts are read ─────────────────────────────

def test_report_shows_per_rule_how_many_positions_had_no_fill_check(paths, monkeypatch, capsys):
    """An entry whose order-book lookup fails is admitted 'ungated', and sleeve verdicts
    count every trade - but `report` printed no ungated figure at all: a ledger with 231
    of 231 positions booked unchecked read like any other."""
    import lib.command_center as cc
    monkeypatch.setattr(cc, "CRYPTO_LEDGER", paths / "crypto.jsonl")
    for name, series in (("GATED", "KXA"), ("LEAKY", "KXB"), ("IDLE", "KXC")):
        favorites(sp.RULES, name, [series])

    def get(url, params=None):
        if url.endswith("/markets"):
            s = params["series_ticker"]
            return {"markets": [] if s == "KXC" else [
                mk(f"{s}-E{i}-A", f"{s}-E{i}", 98, 97, closes_h=1 + i) for i in range(6)]}
        return {"market": {"result": "yes"}}

    def depth(tk, side, price):                 # KXB's lookups fail: admitted, unchecked
        return None if tk.startswith("KXB") else 500.0

    s = sp.run_cycle(get, NOW, sp.RULES, sp.LEDGER, depth_fn=depth, regime_fn=lambda: GOOD,
                     heartbeat=None)
    assert (s["opened"], s["ungated"]) == (12, 6)
    s = sp.run_cycle(get, NOW + timedelta(hours=4, minutes=30), sp.RULES, sp.LEDGER,
                     depth_fn=depth, regime_fn=lambda: GOOD, heartbeat=None)
    assert s["closed"] == 8                      # four of each rule's six have settled
    # an unchecked position from BEFORE the rule was named is not part of its record
    sp._append(sp.LEDGER, [held("KXA-OLD-A", "GATED", depth=None,
                                ts=(NOW - timedelta(days=40)).isoformat())])
    res = {r["rule"]: r for r in sp.evaluate(sp._load_jsonl(sp.LEDGER),
                                             sp.active_rules(sp.RULES))}
    figures = {n: (r["ungated_settled"], r["ungated_open"], r["open"]) for n, r in res.items()}
    assert figures == {"GATED": (0, 0, 2), "LEAKY": (4, 2, 2), "IDLE": (0, 0, 0)}
    assert res["LEAKY"]["gated_share"] == 0.0 and res["GATED"]["gated_share"] == 1.0

    assert run_main(monkeypatch, "report") == 0
    lines = capsys.readouterr().out.splitlines()
    head = next(l for l in lines if l.split()[:2] == ["rule", "events"])
    assert head.split()[-2:] == ["ungated", "verdict"]
    cells = {n: next(l for l in lines if l.strip().startswith(n + " ")).split()
             for n in ("GATED", "LEAKY", "IDLE")}
    # rule, events, WR, net, c/ct, CI, p, ungated, verdict...
    assert {n: c[7] for n, c in cells.items()} == {"GATED": "0", "LEAKY": "6", "IDLE": "0"}
    assert all(" ".join(c[8:]).startswith("collecting") for c in cells.values())
    detail = [l.strip() for l in lines if "no fill check:" in l]
    assert detail == ["no fill check: 4 of 4 settled trades (counted in the verdict), "
                      "2 of 2 still open"]
    assert lines.index(next(l for l in lines if "no fill check:" in l)) == \
        lines.index(next(l for l in lines if l.strip().startswith("LEAKY "))) + 1
    assert any(l.startswith("ungated = positions booked with NO fill check") for l in lines)


# ── alive is not the same as trading: the heartbeat, the cycle line, the log ──

def _exchange(down=(), results=None):
    """Two series, one 98c NO favorite listed in each; `down` series refuse the list
    call, and a settlement poll answers from `results` or fails."""
    results = {} if results is None else results

    def get(url, params=None):
        if url.endswith("/markets"):
            s = params["series_ticker"]
            if s in down:
                raise ConnectionError(f"{s} refused")
            return {"markets": [mk(f"{s}-E9-A", f"{s}-E9", 3, 2)]}
        tk = url.rsplit("/", 1)[-1]
        if tk not in results:
            raise ConnectionError("poll refused")
        return {"market": {"result": results[tk]}}
    return get


def test_failed_requests_are_counted_with_their_streaks(tmp_path):
    """Every Kalshi call failing for six hours wrote 180 heartbeats that the watchdog read
    as healthy and 180 cycle lines identical to a quiet night's; the 720 failed
    settlement polls were counted nowhere at all."""
    rp, lp = tmp_path / "rules.jsonl", tmp_path / "ledger.jsonl"
    favorites(rp, series=["SA", "SB"])
    sp._append(lp, [held("SA-E0-A"), held("SA-E1-A"), held("SB-E0-A")])
    state: dict = {}
    for n in (1, 2, 3):                                   # the exchange is unreachable
        s = cycle(_exchange(down={"SA", "SB"}), rp, lp, state=state,
                  at=NOW + timedelta(minutes=2 * n))
        assert (s["errors"], s["series"], s["poll_errors"]) == (2, 2, 3)
        assert s["all_failed_streak"] == n
        assert s["series_fail_streak"] == {"SA": n, "SB": n}
        assert (s["opened"], s["closed"], s["open_now"], s["cycle_error"]) == (0, 0, 3, None)
    line = sp.cycle_line(s)
    assert line.startswith("[2026-11-20T12:06:00] rules=1 opened=0 closed=0 open_now=3 "
                           "regime=good errors=2/2 all_failed_streak=3 poll_errors=3 took=")
    # one series answers again: not "everything failed" any more, the other keeps counting
    s = cycle(_exchange(down={"SB"}, results={"SA-E0-A": "no"}), rp, lp, state=state,
              at=NOW + timedelta(minutes=8))
    assert (s["errors"], s["all_failed_streak"]) == (1, 0)
    assert s["series_fail_streak"] == {"SB": 4} and s["poll_errors"] == 2
    assert (s["opened"], s["closed"]) == (1, 1)
    # all healthy: the counters are back to zero and the line says nothing about errors
    s = cycle(_exchange(results={"SA-E1-A": "no", "SB-E0-A": "yes"}), rp, lp, state=state,
              at=NOW + timedelta(minutes=10))
    assert (s["errors"], s["poll_errors"], s["all_failed_streak"]) == (0, 0, 0)
    assert s["series_fail_streak"] == {} and (s["opened"], s["closed"]) == (1, 2)
    line = sp.cycle_line(s)
    assert "errors" not in line and "streak" not in line and " took=" in line
    # without a state to carry, a cycle counts from zero - it never invents a streak
    s = cycle(_exchange(down={"SA", "SB"}), rp, lp, at=NOW + timedelta(minutes=12))
    assert s["all_failed_streak"] == 1 and s["series_fail_streak"] == {"SA": 1, "SB": 1}


def test_a_series_no_longer_traded_drops_out_of_the_streaks(tmp_path):
    rp, lp = tmp_path / "rules.jsonl", tmp_path / "ledger.jsonl"
    favorites(rp, "A", ["SA"])
    favorites(rp, "B", ["SB"])
    state: dict = {}
    s = cycle(_exchange(down={"SA", "SB"}), rp, lp, state=state)
    assert s["series_fail_streak"] == {"SA": 1, "SB": 1}
    sp._append(rp, [{"t": "retire", "name": "B"}])
    s = cycle(_exchange(down={"SA", "SB"}), rp, lp, state=state)
    assert s["series_fail_streak"] == {"SA": 2} and s["all_failed_streak"] == 2
    sp._append(rp, [{"t": "retire", "name": "A"}])         # nothing left to ask
    s = cycle(_exchange(down={"SA", "SB"}), rp, lp, state=state)
    assert (s["series"], s["all_failed_streak"], s["series_fail_streak"]) == (0, 0, {})


def test_a_healthy_heartbeat_keeps_every_old_key_and_value(tmp_path):
    """The watchdog and the command center index the ten original keys (and the dashboards
    keep running old code after a pull): they stay, with the values they always had."""
    rp, lp, hb = tmp_path / "rules.jsonl", tmp_path / "ledger.jsonl", tmp_path / "hb.json"
    favorites(rp, series=["SA", "SB"])
    sp._append(lp, [held("SA-E0-A")])
    s = cycle(_exchange(results={"SA-E0-A": "no"}), rp, lp, heartbeat=hb, depth=9)
    on_disk = json.loads(hb.read_text(encoding="utf-8"))
    assert on_disk == s and tuple(s)[:10] == OLD_KEYS
    assert {k: s[k] for k in OLD_KEYS} == {
        "ts": NOW.isoformat(), "rules": 1, "series": 2, "opened": 0, "closed": 1,
        "skipped": {"no_depth": 2}, "ungated": 0, "errors": 0, "regime": "good",
        "open_now": 0}
    assert (s["poll_errors"], s["all_failed_streak"], s["series_fail_streak"],
            s["cycle_error"], s["failed_cycles"]) == (0, 0, {}, None, 0)
    assert isinstance(s["took_s"], float) and s["took_s"] >= 0
    assert not list(tmp_path.glob("*.tmp*"))               # the swap leaves nothing behind


def test_a_cycle_that_raises_still_writes_the_heartbeat_with_every_key(tmp_path):
    """A cycle that died left the last good heartbeat in place: ten minutes later the
    watchdog could only say 'alive but not cycling', and the exception text had gone
    with the console. A heartbeat holding ONLY the failure would be worse - today's
    watchdog reads a missing `rules` as 'no sleeve rules yet - run register-probes' and
    dies on a missing `series`. So: every old key, carried over where the failed cycle
    never got far enough to know it."""
    rp, lp, hb = tmp_path / "rules.jsonl", tmp_path / "ledger.jsonl", tmp_path / "hb.json"
    favorites(rp, series=["SA", "SB"])
    sp._append(lp, [held("SA-E0-A"), held("SB-E0-A", closed_h=5)])
    good = cycle(_exchange(), rp, lp, heartbeat=hb)
    assert (good["rules"], good["series"], good["opened"], good["open_now"]) == (1, 2, 2, 4)

    def boom():
        raise RuntimeError("regime file unreadable")

    state: dict = {}
    for n in (1, 2):                 # dies after reading the registry and the ledger
        with pytest.raises(RuntimeError, match="regime file unreadable"):
            sp.run_cycle(_exchange(), NOW + timedelta(minutes=2 * n), rp, lp,
                         depth_fn=lambda t, s, p: 50, regime_fn=boom, heartbeat=hb,
                         state=state)
        s = json.loads(hb.read_text(encoding="utf-8"))
        assert tuple(s)[:10] == OLD_KEYS
        assert s["ts"] == (NOW + timedelta(minutes=2 * n)).isoformat()
        assert s["cycle_error"] == "RuntimeError: regime file unreadable"
        assert s["failed_cycles"] == n
        assert (s["rules"], s["open_now"], s["regime"]) == (1, 4, "good")   # known / carried
        assert (s["opened"], s["closed"], s["errors"]) == (0, 0, 0)
    assert state["last"] == s

    # dies before it knows anything (the registry cannot be read): all carried over,
    # here from the heartbeat FILE - a one-shot `cycle` has no earlier cycle in memory
    with pytest.raises(OSError):
        cycle(_exchange(), tmp_path, lp, heartbeat=hb, at=NOW + timedelta(minutes=6))
    s = json.loads(hb.read_text(encoding="utf-8"))
    assert tuple(s)[:10] == OLD_KEYS and s["cycle_error"].split(":")[0].endswith("Error")
    assert (s["rules"], s["series"], s["open_now"], s["regime"]) == (1, 2, 4, "good")
    assert s["failed_cycles"] == 1 and s["ts"] == (NOW + timedelta(minutes=6)).isoformat()

    # and the next good cycle clears it
    s = cycle(_exchange(), rp, lp, heartbeat=hb, state=state, at=NOW + timedelta(minutes=8))
    assert (s["cycle_error"], s["failed_cycles"]) == (None, 0)
    assert json.loads(hb.read_text(encoding="utf-8")) == s

    # a failed cycle with no heartbeat to carry from says 0 / unknown, and still raises
    with pytest.raises(OSError):
        cycle(_exchange(), tmp_path, lp, heartbeat=tmp_path / "fresh" / "hb.json")
    s = json.loads((tmp_path / "fresh" / "hb.json").read_text(encoding="utf-8"))
    assert (s["rules"], s["series"], s["open_now"], s["regime"]) == (0, 0, 0, "unknown")


def test_the_run_loop_logs_what_it_prints(paths, monkeypatch, capsys):
    """The scheduled task runs in a hidden window with nothing redirected: 1,980 warnings
    in a six-hour outage went to a console nobody could read. `run` now appends its cycle
    lines and warnings to a log file as well."""
    favorites(sp.RULES, series=["SA"])
    seq = iter([_exchange(), _exchange(down={"SA"}), _exchange(), _exchange()])
    current = {}

    def get(url, params=None):
        return current["get"](url, params)

    cycles = []

    def regime():
        if not cycles or cycles[-1] != sp._now():          # first call of a new cycle
            cycles.append(sp._now())
            current["get"] = next(seq)
        if len(cycles) == 3:
            raise RuntimeError("regime file unreadable")
        return GOOD

    run_loop(monkeypatch, 4, get, regime=regime)
    printed = capsys.readouterr().out.splitlines()
    logged = (paths / "sleeve_paper.log").read_text(encoding="utf-8").splitlines()
    assert [l[21:] for l in logged] == printed              # the same lines, UTC-stamped
    assert [l[:21] for l in logged] == [f"2026-11-20T12:{m}:00Z " for m in
                                        ("00", "00", "02", "02", "04", "06")]
    assert printed[0].startswith("sleeve paper - every 120s -> ")
    assert printed[1].startswith("[2026-11-20T12:00:00] rules=1 opened=1 closed=0 open_now=1 "
                                 "regime=good took=")
    assert printed[2] == "[warn] SA: ConnectionError: SA refused"
    assert printed[3].startswith("[2026-11-20T12:02:00] rules=1 opened=0 closed=0 open_now=1 "
                                 "regime=good errors=1/1 all_failed_streak=1 took=")
    assert printed[4] == "[warn] cycle failed (1 in a row): RuntimeError: regime file unreadable"
    assert printed[5].startswith("[2026-11-20T12:06:00] rules=1 opened=0 closed=0 open_now=1 ")
    assert len(printed) == 6
    hb = json.loads(sp.HEARTBEAT.read_text(encoding="utf-8"))
    assert (hb["cycle_error"], hb["failed_cycles"], hb["all_failed_streak"]) == (None, 0, 0)


def test_one_shot_cycle_logs_its_line_and_its_failure(paths, monkeypatch, capsys):
    favorites(sp.RULES, series=["SA"])
    sp._append(sp.LEDGER, [held("SA-E0-A")])
    offline(monkeypatch, _exchange())
    assert run_main(monkeypatch, "cycle") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("[warn] 1 settlement poll(s) failed - first SA-E0-A")
    assert out[1].startswith("{'ts': '2026-11-20T12:00:00+00:00', 'rules': 1, ")   # as before
    assert len(out) == 2
    log = paths / "sleeve_paper.log"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and lines[0].endswith(
        "[warn] 1 settlement poll(s) failed - first SA-E0-A: ConnectionError: poll refused")
    assert " rules=1 opened=1 closed=0 open_now=2 regime=good poll_errors=1 took=" in lines[1]

    def boom():
        raise RuntimeError("regime file unreadable")
    offline(monkeypatch, _exchange(), regime=boom)
    with pytest.raises(RuntimeError):
        run_main(monkeypatch, "cycle")
    assert log.read_text(encoding="utf-8").splitlines()[-1].endswith(
        "[warn] cycle failed: RuntimeError: regime file unreadable")
    assert json.loads(sp.HEARTBEAT.read_text(encoding="utf-8"))["cycle_error"]


def test_the_log_follows_the_ledger_like_the_lock(paths, monkeypatch):
    assert sp._log_path() == paths / "sleeve_paper.log"     # a scratch ledger: its own log
    monkeypatch.setattr(sp, "LEDGER", ROOT / "data" / "paper_sleeves.jsonl")
    assert sp._log_path() == sp.LOG                          # the real ledger: the real log
    assert sp.LOG.name == "sleeve_paper.log" and sp.LOG.parent.name == "logs"


def test_a_log_or_console_that_cannot_be_written_never_stops_a_cycle(tmp_path, monkeypatch):
    rp, lp = tmp_path / "rules.jsonl", tmp_path / "ledger.jsonl"
    favorites(rp, series=["SA", "SB"])
    locked = tmp_path / "a_folder_not_a_file.log"
    locked.mkdir()

    class Closed:
        def write(self, text):
            raise ValueError("I/O operation on closed file")

        def flush(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(sys, "stdout", Closed())
    s = cycle(_exchange(down={"SB"}), rp, lp, log=locked)   # a warning it cannot emit
    monkeypatch.undo()
    assert (s["opened"], s["errors"]) == (1, 1)
    assert [r["ticker"] for r in sp._load_jsonl(lp)] == ["SA-E9-A"]


def test_a_heartbeat_that_cannot_be_swapped_or_written(tmp_path, monkeypatch, capsys):
    """Windows refuses the swap while a reader has the heartbeat open: write it in place
    then, as before. A heartbeat that cannot be written at all is a warning, not a failed
    cycle - the rows are already in the ledger."""
    rp, lp, hb = tmp_path / "rules.jsonl", tmp_path / "ledger.jsonl", tmp_path / "hb.json"
    favorites(rp, series=["SA"])

    def refused(src, dst):
        raise PermissionError(13, "the process cannot access the file")
    monkeypatch.setattr(os, "replace", refused)
    s = cycle(_exchange(), rp, lp, heartbeat=hb)
    assert json.loads(hb.read_text(encoding="utf-8")) == s and s["opened"] == 1
    assert not list(tmp_path.glob("*.tmp*"))
    monkeypatch.undo()

    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    s = cycle(_exchange(), rp, lp, heartbeat=blocker / "hb.json", at=NOW + timedelta(hours=11))
    assert s["closed"] == 0 and s["poll_errors"] == 1        # the cycle ran to its end
    assert "[warn] heartbeat not written:" in capsys.readouterr().out


# ── one malformed thing costs that one thing ──────────────────────────────────

def _three_cities(tmp_path, extra_rows=()):
    """One rule over three series, a 98c favorite listed in each, two bets already held."""
    rp, lp = tmp_path / "rules.jsonl", tmp_path / "ledger.jsonl"
    favorites(rp, series=["SA", "SB", "SC"])
    sp._append(lp, [held("SA-E0-A"), held("SB-E0-A")] + list(extra_rows))
    return rp, lp


def _listing(junk=None, payloads=None):
    """A 98c favorite in each series (plus `junk` entries in SC's list - the LAST series
    asked, after SA's and SB's entries are decided); settlement answers from `payloads`."""
    payloads = {} if payloads is None else payloads

    def get(url, params=None):
        if url.endswith("/markets"):
            s = params["series_ticker"]
            ms = [mk(f"{s}-E9-A", f"{s}-E9", 3, 2)]
            return {"markets": (list(junk) + ms) if (junk and s == "SC") else ms}
        return payloads.get(url.rsplit("/", 1)[-1], {"market": {"result": "no"}})
    return get


def test_a_malformed_market_costs_that_market_only(tmp_path, capsys):
    """One null entry in ONE series' market list raised AttributeError after the other
    series' entries had passed the fill gate; nothing was written, and every cycle
    repeated it for as long as the entry was listed."""
    for junk in ([None], ["text"], [7], [["x"]], [None, "text", 7, ["x"]]):
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        d.mkdir()
        rp, lp = _three_cities(d)
        s = cycle(_listing(junk=junk), rp, lp)
        assert (s["opened"], s["closed"], s["market_errors"]) == (3, 2, len(junk)), junk
        assert s["cycle_error"] is None and s["errors"] == 0
        rows = sp._load_jsonl(lp)[2:]
        assert [(r["t"], r["ticker"]) for r in rows] == [
            ("open", "SA-E9-A"), ("open", "SB-E9-A"), ("open", "SC-E9-A"),
            ("close", "SA-E0-A"), ("close", "SB-E0-A")]
    out = capsys.readouterr().out
    assert "[warn] 4 market(s) or position(s) skipped - first a SC market: AttributeError" in out
    assert " market_errors=4 " in sp.cycle_line(s)
    # the same cycle with clean input writes the same rows: a guard changes nothing valid
    clean_rp, clean_lp = _three_cities(tmp_path / "clean")
    c = cycle(_listing(), clean_rp, clean_lp)
    assert (c["opened"], c["closed"], c["market_errors"]) == (3, 2, 0)
    def sans_clock(path):                 # a close row is stamped with the wall clock
        return [{k: v for k, v in r.items() if (k, r["t"]) != ("ts", "close")}
                for r in sp._load_jsonl(path)]
    assert sans_clock(clean_lp) == sans_clock(lp)
    assert "market_errors" not in sp.cycle_line(c)


def test_a_settlement_answer_that_is_not_an_object_skips_that_position(tmp_path):
    for payload in ({"market": "gone"}, {"market": ["x"]}):
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        d.mkdir()
        rp, lp = _three_cities(d)
        s = cycle(_listing(payloads={"SA-E0-A": payload}), rp, lp)
        assert (s["opened"], s["closed"], s["market_errors"], s["poll_errors"]) == (3, 1, 1, 0)
        assert {k[0] for k in sp.open_positions(sp._load_jsonl(lp))} == {
            "SA-E0-A", "SA-E9-A", "SB-E9-A", "SC-E9-A"}      # SA-E0-A waits, SB-E0-A settled
    # shapes that were already handled stay handled, and count as nothing
    for payload in ({"market": None}, {"market": {"result": 7}}, {"market": {}}):
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        d.mkdir()
        rp, lp = _three_cities(d)
        s = cycle(_listing(payloads={"SA-E0-A": payload}), rp, lp)
        assert (s["opened"], s["closed"], s["market_errors"], s["poll_errors"]) == (3, 1, 0, 0)


def test_ledger_rows_without_a_ticker_or_a_rule_are_no_positions(tmp_path):
    """A row that is valid JSON but lacks ticker or rule raised KeyError at the top of
    every cycle: no trading and no settling until someone removed it by hand."""
    junk = [{"t": "open", "ts": NOW.isoformat()}, {"t": "close", "ticker": "X"},
            {"t": "close", "rule": "R"}, {"t": "open", "ticker": ["x"], "rule": "R"},
            {"t": "open", "ticker": "T", "rule": "R", "event": ["e"]},
            {"t": "open", "ticker": "", "rule": "R"}, {"t": "open", "ticker": "T2", "rule": None}]
    rp, lp = _three_cities(tmp_path, extra_rows=junk)
    rows = sp._load_jsonl(lp)
    assert set(sp.open_positions(rows)) == {("SA-E0-A", "R"), ("SB-E0-A", "R"), ("T", "R")}
    s = cycle(_listing(), rp, lp)
    assert (s["opened"], s["closed"], s["cycle_error"]) == (3, 2, None)
    # ("T", "R") IS a position - it has a ticker and a rule - but nothing to settle it
    # with (no side, no price): it stays open and is COUNTED every cycle, never hidden
    assert s["market_errors"] == 1 and s["open_now"] == 4
    again = cycle(_listing(), rp, lp, at=NOW + timedelta(minutes=2))
    assert (again["opened"], again["closed"], again["market_errors"]) == (0, 0, 1)
    # a close row that comes BEFORE its open still closes it (two passes, as before)
    o = held("S-E5-A")
    assert sp.open_positions([sp.settle(o, "no"), o]) == {}


GOOD_ROW = {"band": "any", "side": "favorite", "lo": 0.97, "hi": 0.995, "contracts": 10}


def test_rule_problem_is_exactly_what_a_cycle_would_raise_on():
    named = {"named_at": NOW.isoformat()}
    ok = dict(GOOD_ROW, name="R", series=["S"], **named)
    for usable in (ok, dict(ok, series="S"), dict(ok, series=None), dict(ok, min_h=6, max_h=12.0),
                   {k: v for k, v in ok.items() if k != "contracts"}, dict(ok, lo=0, hi=1),
                   dict(ok, named_at="not a date"), dict(ok, band="unheard-of"),
                   dict(ok, side="sideways")):
        assert sp.rule_problem(usable) is None, usable
        sp.candidates(mk("A", "E", 98, 96), NOW.timestamp(), [usable])     # never raises
    for field in ("band", "side", "lo", "hi", "named_at"):
        row = {k: v for k, v in ok.items() if k != field}
        assert sp.rule_problem(row) == f"lacks {field}"
    assert sp.rule_problem({"name": "R"}) == "lacks band, side, lo, hi, named_at"
    for field, value in (("lo", "0.97"), ("hi", None), ("min_h", None), ("max_h", None),
                         ("max_h", "12"), ("side", ["yes"]), ("series", [["S"]])):
        assert sp.rule_problem(dict(ok, **{field: value})) == f"has an unusable {field}"
    assert sp.rule_problem(dict(ok, lo=None, max_h=None)) == "has an unusable lo, max_h"


def test_a_registry_row_that_cannot_be_traded_stops_only_that_rule(tmp_path, capsys):
    """A registry row without band or named_at, or with max_h null, raised KeyError or
    TypeError in every cycle - for ALL rules. Now the other rules trade and settle, the
    rows are written, and the cycle still ends in an error that names the rule: a rule
    silently left out would stop its forward test while everything else looked normal."""
    rp, lp, hb = tmp_path / "rules.jsonl", tmp_path / "ledger.jsonl", tmp_path / "hb.json"
    favorites(rp, "GOOD", ["SA", "SB"])
    named = (NOW - timedelta(days=30)).isoformat()
    no_band = {k: v for k, v in GOOD_ROW.items() if k != "band"}
    sp._append(rp, [dict(no_band, t="rule", name="NOBAND", series=["SA"], named_at=named),
                    dict(GOOD_ROW, t="rule", name="NONAME_AT", series=["SC"]),
                    dict(GOOD_ROW, t="rule", name="NULLMAX", series=["SA"], named_at=named,
                         max_h=None)])
    assert set(sp.unusable_rules(sp.active_rules(rp))) == {"NOBAND", "NONAME_AT", "NULLMAX"}
    sp._append(lp, [held("SA-E0-A", "GOOD"), held("SC-E0-A", "NOBAND")])
    state: dict = {}
    with pytest.raises(sp.RegistryError) as err:
        cycle(_listing(), rp, lp, heartbeat=hb, state=state)
    msg = str(err.value)
    assert msg.startswith("3 rule(s) NOT TRADED - registry row unusable: NOBAND lacks band; "
                          "NONAME_AT lacks named_at; NULLMAX has an unusable max_h.")
    assert msg.endswith("The other 1 rule(s) traded as usual.")
    rows = sp._load_jsonl(lp)[2:]
    # GOOD entered SA and SB (never SC, which only the unusable rules list) and settled;
    # the unusable rule's OLD position settled too - its bet was made, it is owed its result
    assert [(r["t"], r["ticker"], r["rule"]) for r in rows] == [
        ("open", "SA-E9-A", "GOOD"), ("open", "SB-E9-A", "GOOD"),
        ("close", "SA-E0-A", "GOOD"), ("close", "SC-E0-A", "NOBAND")]
    s = json.loads(hb.read_text(encoding="utf-8"))
    assert (s["rules"], s["series"], s["opened"], s["closed"], s["errors"]) == (4, 2, 2, 2, 0)
    assert s["cycle_error"].startswith("RegistryError: 3 rule(s) NOT TRADED")
    assert s["failed_cycles"] == 1 and s["open_now"] == 2
    assert s["bad_rules"] == {"NOBAND": "lacks band", "NONAME_AT": "lacks named_at",
                              "NULLMAX": "has an unusable max_h"}
    with pytest.raises(sp.RegistryError):                    # and again, every cycle
        cycle(_listing(), rp, lp, heartbeat=hb, state=state, at=NOW + timedelta(minutes=2))
    assert json.loads(hb.read_text(encoding="utf-8"))["failed_cycles"] == 2
    assert len(sp._load_jsonl(lp)) == 6                      # nothing entered twice
    # the remedy: retire the unusable rows - the registry is append-only
    sp._append(rp, [{"t": "retire", "name": n} for n in ("NOBAND", "NONAME_AT", "NULLMAX")])
    s = cycle(_listing(), rp, lp, heartbeat=hb, state=state, at=NOW + timedelta(minutes=4))
    assert (s["cycle_error"], s["failed_cycles"], s["bad_rules"], s["rules"]) == (None, 0, {}, 1)


def test_rules_and_report_name_an_unusable_registry_row(paths, monkeypatch, capsys):
    import lib.command_center as cc
    monkeypatch.setattr(cc, "CRYPTO_LEDGER", paths / "crypto.jsonl")
    favorites(sp.RULES, "GOOD", ["SA"])
    sp._append(sp.RULES, [dict(GOOD_ROW, t="rule", name="NULLMAX", series=["SB"], max_h=None,
                               named_at=NOW.isoformat()),
                          {"t": "rule", "name": "BARE", "named_at": 5}])
    assert run_main(monkeypatch, "rules") == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == ("3 active, 0 retired - 2 active rule(s) NOT TRADED "
                                   "(registry row unusable)")
    assert "BARE" in out and "NOT TRADED: its registry row lacks band, side, lo, hi." in out
    assert "NOT TRADED: its registry row has an unusable max_h." in out
    assert any(l.split()[:2] == ["GOOD", "favorite"] for l in out.splitlines())
    assert run_main(monkeypatch, "report") == 0
    out = capsys.readouterr().out
    assert "NOT TRADED: NULLMAX - its registry row has an unusable max_h" in out
    assert "NOT TRADED: BARE - its registry row lacks band, side, lo, hi" in out
    sp._append(sp.RULES, [{"t": "retire", "name": "NULLMAX"}, {"t": "retire", "name": "BARE"}])
    assert run_main(monkeypatch, "report") == 0
    assert "NOT TRADED" not in capsys.readouterr().out


class _SleepFails:
    """`time` inside sleeve_paper: the pause after the Nth series' markets raises - a
    failure outside every per-item guard, in the middle of a cycle."""

    def __init__(self, after):
        self.after, self.calls = after, 0

    def time(self):
        return 0.0

    def sleep(self, seconds):
        self.calls += 1
        if self.calls == self.after:
            raise RuntimeError("something nobody guarded against")


def test_rows_already_decided_are_recorded_when_the_cycle_then_fails(tmp_path, monkeypatch):
    """Rows were appended once, at the very end: a cycle that raised after four entries
    had passed the fill gate wrote none of them, and repeated the order-book lookups on
    the next cycle for as long as the cause lasted."""
    rp, lp = _three_cities(tmp_path)
    hb = tmp_path / "hb.json"
    monkeypatch.setattr(sp, "time", _SleepFails(after=2))    # dies after SA and SB, before SC
    with pytest.raises(RuntimeError, match="nobody guarded"):
        cycle(_listing(), rp, lp, heartbeat=hb)
    monkeypatch.undo()
    assert [(r["t"], r["ticker"]) for r in sp._load_jsonl(lp)[2:]] == [
        ("open", "SA-E9-A"), ("open", "SB-E9-A")]            # decided before it died
    s = json.loads(hb.read_text(encoding="utf-8"))
    assert (s["opened"], s["closed"], s["open_now"]) == (2, 0, 4)
    assert s["cycle_error"] == "RuntimeError: something nobody guarded against"
    # the next cycle starts from the ledger: SC is entered, nothing is entered twice
    s = cycle(_listing(), rp, lp, at=NOW + timedelta(minutes=2))
    assert (s["opened"], s["closed"]) == (1, 2)
    opens = [r["ticker"] for r in sp._load_jsonl(lp) if r["t"] == "open"]
    assert sorted(opens) == ["SA-E0-A", "SA-E9-A", "SB-E0-A", "SB-E9-A", "SC-E9-A"]


def test_an_append_that_fails_is_never_retried(tmp_path, monkeypatch):
    """Appending the decided rows before re-raising must not become appending them
    twice: when the append itself fails, part of it may already be in the file."""
    rp, lp = _three_cities(tmp_path)
    hb = tmp_path / "hb.json"
    real, calls = sp._append, []

    def disk_full(path, rows):
        calls.append(len(rows))
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(sp, "_append", disk_full)
    with pytest.raises(OSError, match="No space left"):
        cycle(_listing(), rp, lp, heartbeat=hb)
    assert calls == [5]                                      # one attempt, 3 opens + 2 closes
    s = json.loads(hb.read_text(encoding="utf-8"))
    assert (s["opened"], s["closed"], s["open_now"]) == (0, 0, 2)     # nothing is claimed
    assert s["cycle_error"].startswith("OSError: [Errno 28]")
    # a cycle that failed AND could not append reports the failure that came first
    monkeypatch.setattr(sp, "time", _SleepFails(after=1))
    with pytest.raises(RuntimeError, match="nobody guarded"):
        cycle(_listing(), rp, lp, heartbeat=hb)
    assert calls == [5, 1]
    monkeypatch.undo()
    assert sp._append is real and len(sp._load_jsonl(lp)) == 2
    s = cycle(_listing(), rp, lp, at=NOW + timedelta(minutes=2))      # the disk has room again
    assert (s["opened"], s["closed"]) == (3, 2) and len(sp._load_jsonl(lp)) == 7


def test_a_trader_that_can_read_no_market_at_all_still_fails_loudly(tmp_path, capsys):
    """The per-market guard is for the odd malformed entry. If NOT ONE listed market can
    be handled (a changed payload, a bug), a counter in a fresh heartbeat would hide a
    trader that has stopped trading: that stays a failed cycle - after the positions
    already held have been settled and recorded."""
    rp, lp = _three_cities(tmp_path)
    hb = tmp_path / "hb.json"

    def get(url, params=None):
        if url.endswith("/markets"):
            return {"markets": ["not", "markets"]}
        return {"market": {"result": "no"}}

    with pytest.raises(RuntimeError) as err:
        cycle(get, rp, lp, heartbeat=hb)
    assert str(err.value).startswith("none of the 6 market(s) listed this cycle could be "
                                     "handled - first a SA market: AttributeError")
    s = json.loads(hb.read_text(encoding="utf-8"))
    assert (s["opened"], s["closed"], s["market_errors"], s["failed_cycles"]) == (0, 2, 6, 1)
    assert s["cycle_error"].startswith("RuntimeError: none of the 6 market(s)")
    assert [r["t"] for r in sp._load_jsonl(lp)[2:]] == ["close", "close"]
    # one readable market among the junk and it is the odd entry again, not a failed cycle
    s = cycle(_listing(junk=["junk"]), rp, lp, at=NOW + timedelta(minutes=2))
    assert (s["opened"], s["market_errors"], s["cycle_error"]) == (3, 1, None)
    # an empty listing (or a market with no ticker) is nothing to handle, not a failure
    s = cycle(lambda url, params=None: {"markets": [{}]}, rp, lp, at=NOW + timedelta(minutes=4))
    assert (s["market_errors"], s["cycle_error"]) == (0, None)


def test_the_run_loop_says_what_a_failed_cycle_recorded(paths, monkeypatch, capsys):
    favorites(sp.RULES, "GOOD", ["SA"])
    sp._append(sp.RULES, [dict(GOOD_ROW, t="rule", name="NULLMAX", series=["SB"], max_h=None,
                               named_at=NOW.isoformat())])
    run_loop(monkeypatch, 2, _listing())
    out = [l for l in capsys.readouterr().out.splitlines() if l.startswith("[warn] cycle")]
    assert out[0].startswith("[warn] cycle failed (1 in a row): RegistryError: 1 rule(s) NOT "
                             "TRADED - registry row unusable: NULLMAX has an unusable max_h.")
    assert out[0].endswith("[recorded first: opened=1 closed=0]")
    assert out[1].startswith("[warn] cycle failed (2 in a row): RegistryError:")
    assert out[1].endswith("traded as usual.") and len(out) == 2
    assert [r["rule"] for r in sp._load_jsonl(sp.LEDGER)] == ["GOOD"]
