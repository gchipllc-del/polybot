"""sleeve_paper - the trader keeps one honest ledger when things around it go wrong:
a second copy of itself, a torn write, a duplicated row. (Audit findings, 2026-11-25.)"""
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
    monkeypatch.setattr(sp, "run_cycle", lambda *a, **k: seen.append(1) or {"opened": 0})
    assert run_main(monkeypatch, "cycle") == 0 and seen == [1]
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
