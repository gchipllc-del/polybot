"""paper_trader - the crypto forward trader's safety net: what a crash, a second copy or a
stalled cycle may cost, each pinned. No test here changes what a frozen rule trades."""
import json
import os
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import formatdate
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import paper_trader as pt  # noqa: E402
import shadow_book as sb  # noqa: E402
import stage0_collector as s0  # noqa: E402
from lib import single_instance as si  # noqa: E402

T0 = datetime(2026, 11, 25, 12, 0, 20, tzinfo=timezone.utc)
CLOSE = datetime(2026, 11, 25, 12, 15, tzinfo=timezone.utc)
GOOD = {"regime": "good", "mult": 1.0}


CLOCK = {"now": T0}           # what the trader's wall clock reads; cycle() moves it


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """Only the frozen rules, no fair-value history, and a wall clock that agrees with
    the injected `now`: nothing here may depend on the overlay file, the collector log
    or the date of the machine the suite runs on."""
    monkeypatch.setattr(pt, "_rules",
                        lambda: (list(sb.RULES), sb.BANDS, sb.kalshi_taker_fee))
    monkeypatch.setattr(s0, "LOG", tmp_path / "no_stage0.jsonl")
    monkeypatch.setitem(CLOCK, "now", T0)
    monkeypatch.setattr(pt, "_now_iso", lambda: CLOCK["now"].isoformat())


def exchange(yes_ask, close=CLOSE, settled=()):
    """One market per series per window, the live venue's shape."""
    def get(url, params=None):
        params = params or {}
        if url.endswith("/events"):
            return {"events": [{"event_ticker": "EV-" + params["series_ticker"]}]}
        if url.endswith("/markets") and "event_ticker" in params:
            s = params["event_ticker"][3:]
            return {"markets": [{"ticker": f"{s}-26NOV251215-15", "floor_strike": 101000.0,
                                 "close_time": close.isoformat(),
                                 "yes_ask_dollars": yes_ask,
                                 "no_ask_dollars": round(1.02 - yes_ask, 2)}]}
        if url.endswith("/markets"):
            return {"markets": [m for m in settled
                                if m["ticker"].startswith(params["series_ticker"])]}
        if "coinbase" in url:
            return {"data": {"amount": "101100.0"}}
        raise AssertionError(url)
    return get


def cycle(path, yes_ask, minute=0, **kw):
    kw.setdefault("get", exchange(yes_ask))
    kw.setdefault("depth_fn", lambda t, s, p: 100.0)
    kw.setdefault("regime_fn", lambda: GOOD)
    CLOCK["now"] = T0 + timedelta(minutes=minute)
    return pt.run_cycle(now=CLOCK["now"], path=path, **kw)


def tear_last_row(path: Path, tail: bytes = b"") -> None:
    """What a power cut in the middle of an append leaves: half of the last row and no
    newline (on Windows often followed by a run of NUL bytes)."""
    data = path.read_bytes()
    last = data.rstrip(b"\n").rfind(b"\n") + 1
    path.write_bytes(data[: last + (len(data) - last) // 2] + tail)


def opens(path):
    return [(r["series"], r["price"]) for r in pt._load(path) if r.get("t") == "open"]


# ── a torn last row costs that row, not the next one as well ─────────────────

@pytest.mark.parametrize("tail", [b"", b"\x00" * 300], ids=["cut", "cut+NULs"])
def test_a_torn_last_row_does_not_swallow_the_next_cycles_row(tmp_path, tail):
    """The first row written after a torn one is the re-entry of the very bet whose row
    was torn. Glued onto the fragment it was unreadable too, so the bet was taken a THIRD
    time a cycle later at yet another price - or never, once its market had closed."""
    p = tmp_path / "paper.jsonl"
    assert cycle(p, 0.55, 0)["opened"] == 2          # CONTROL on both series at 0.55
    tear_last_row(p, tail)                           # the ETH row is cut mid-write
    assert cycle(p, 0.58, 1)["opened"] == 1          # restart: the torn bet, re-taken
    assert opens(p) == [("KXBTC15M", 0.55), ("KXETH15M", 0.58)]
    assert cycle(p, 0.61, 2)["opened"] == 0          # the ledger holds it: taken once
    assert opens(p) == [("KXBTC15M", 0.55), ("KXETH15M", 0.58)]


def test_append_writes_whole_lf_rows_and_nothing_for_no_rows(tmp_path):
    p = tmp_path / "sub" / "paper.jsonl"
    pt._append([], p)
    assert not p.exists()                            # an idle cycle creates nothing
    pt._append([{"t": "open", "ticker": "T", "rule": "R"}, {"t": "close"}], p)
    assert p.read_bytes() == b'{"t":"open","ticker":"T","rule":"R"}\n{"t":"close"}\n'


# ── one trader per ledger ─────────────────────────────────────────────────────

HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from lib import single_instance as si
si.hold(sys.argv[2])
print("HOLDING", flush=True)
time.sleep(60)
"""


@pytest.fixture
def lock_dir(tmp_path, monkeypatch):
    d = tmp_path / "locks"
    monkeypatch.setenv("POLYBOT_LOCK_DIR", str(d))
    yield d
    si.release("paper_trader")


def live_twin(lock_dir):
    """Another OS process that holds the trader's lock, as the scheduled task does."""
    twin = subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT), "paper_trader"],
                            env=dict(os.environ, POLYBOT_LOCK_DIR=str(lock_dir)),
                            stdout=subprocess.PIPE, text=True)
    assert twin.stdout.readline().strip() == "HOLDING"
    return twin


def stop(twin):
    twin.kill()
    twin.wait(timeout=10)
    twin.stdout.close()


@pytest.mark.parametrize("cmd", ["cycle", "run"])
def test_a_second_trader_stands_down_before_it_trades(cmd, lock_dir, monkeypatch, capsys):
    """Two overlapping cycles both read the ledger before either appends, so both book
    the same entries: 4 open rows for 2 positions with two real processes."""
    def never(*a, **k):
        raise AssertionError("a second copy must not trade")
    monkeypatch.setattr(pt, "run_cycle", never)
    monkeypatch.setattr(pt, "run_loop", never)
    monkeypatch.setattr(sys, "argv", ["paper_trader.py", cmd])
    twin = live_twin(lock_dir)
    try:
        assert pt.main() == 1
    finally:
        stop(twin)
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("not starting: paper_trader is already "
                                               f"running (pid {twin.pid})")


def test_the_only_trader_takes_the_lock_and_a_real_second_process_exits_nonzero(
        lock_dir, tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(pt, "run_cycle", lambda **kw: ran.append(kw) or {
        "opened": 0, "closed": 0, "open_now": 0, "skipped": {}})
    monkeypatch.setattr(sys, "argv", ["paper_trader.py", "cycle"])
    assert pt.main() == 0 and len(ran) == 1
    assert "paper_trader" in si._HELD                 # held until this process exits
    # the real command line, as Task Scheduler or a second console would start it. If it
    # did NOT stand down it would trade: so it gets a scratch ledger and a dead proxy.
    env = dict(os.environ, POLYBOT_LOCK_DIR=str(lock_dir),
               PAPER_CRYPTO_LEDGER=str(tmp_path / "twin_ledger.jsonl"),
               PAPER_CRYPTO_HEARTBEAT=str(tmp_path / "twin_heartbeat.json"),
               STAGE0_LOG=str(tmp_path / "twin_stage0.jsonl"),
               HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9",
               NO_PROXY="")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "paper_trader.py"), "cycle"],
                       env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 1 and "already running" in r.stdout, (r.stdout, r.stderr)
    assert not (tmp_path / "twin_ledger.jsonl").exists()


def test_the_lock_never_blocks_a_cycle_function_a_report_or_the_selftest(
        lock_dir, tmp_path, monkeypatch, capsys):
    """Tests, the selftest the watchdog runs every 15 minutes and the read-only report
    must all work beside the live trader: the lock lives in main() for run/cycle only."""
    twin = live_twin(lock_dir)
    try:
        assert cycle(tmp_path / "p.jsonl", 0.55)["opened"] == 2      # run_cycle itself
        monkeypatch.setattr(pt, "LEDGER", tmp_path / "p.jsonl")
        monkeypatch.setattr(sys, "argv", ["paper_trader.py", "report"])
        assert pt.main() == 0
        assert "PAPER TRADER" in capsys.readouterr().out
        monkeypatch.setattr(pt, "_selftest", lambda: 0)
        monkeypatch.setattr(sys, "argv", ["paper_trader.py", "selftest"])
        assert pt.main() == 0
    finally:
        stop(twin)
    assert "paper_trader" not in si._HELD


# ── the heartbeat: what the last completed cycle did ─────────────────────────

def dead(url, params=None):
    raise ConnectionError("Max retries exceeded with url: " + url)


def heartbeat_of(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_completed_cycle_leaves_a_heartbeat_with_its_counters(tmp_path):
    p, hb = tmp_path / "paper.jsonl", tmp_path / "status" / "paper_crypto_heartbeat.json"
    c = cycle(p, 0.55, heartbeat=hb)
    got = heartbeat_of(hb)
    lag = got.pop("quote_lag_s")
    assert got == {"ts": T0.isoformat(), "took_s": c["took_s"], "opened": 2, "closed": 0,
                   "open_now": 2, "skipped": {}, "ungated": 0, "errors": 0,
                   "last_error": None, "clock_skew_s": None,
                   "overdue": {"n": 0, "oldest": None, "oldest_h": None}}
    assert sorted(lag) == ["KXBTC15M", "KXETH15M"]
    assert isinstance(got["took_s"], float) and 0 <= got["took_s"] < 5
    assert [f.name for f in hb.parent.iterdir()] == [hb.name]      # no temp file left
    # the next cycle replaces it with its own numbers: one position settles
    settled = [{"ticker": "KXBTC15M-26NOV251215-15", "result": "yes"}]
    cycle(p, 0.55, 1, get=exchange(0.55, settled=settled), heartbeat=hb)
    got = heartbeat_of(hb)
    assert (got["ts"], got["opened"], got["closed"], got["open_now"]) == (
        (T0 + timedelta(minutes=1)).isoformat(), 0, 1, 1)


def test_a_trader_whose_every_request_fails_no_longer_looks_like_a_quiet_market(
        tmp_path, capsys):
    """The cycle completes, raises nothing and writes no ledger row - on disk it was
    indistinguishable from a healthy trader with nothing to do, for as long as it lasted."""
    p, hb = tmp_path / "paper.jsonl", tmp_path / "hb.json"
    c = cycle(p, 0.55, get=dead, heartbeat=hb)
    assert (c["opened"], c["closed"], c["open_now"]) == (0, 0, 0) and not p.exists()
    got = heartbeat_of(hb)
    assert got["errors"] == 4                        # 2 market lists + 2 settlement sweeps
    assert got["last_error"].startswith("GET /markets: ConnectionError: Max retries")
    assert c["errors"] == 4 and c["last_error"] == got["last_error"]
    assert capsys.readouterr().out.count("[warn]") == 4       # the console lines stay


def test_failed_spot_requests_are_counted_and_change_nothing_else(tmp_path):
    def no_spot(url, params=None):
        if "coinbase" in url:
            raise TimeoutError("read timed out")
        return exchange(0.55)(url, params)
    p, hb = tmp_path / "paper.jsonl", tmp_path / "hb.json"
    assert cycle(p, 0.55, get=no_spot, heartbeat=hb)["opened"] == 2
    got = heartbeat_of(hb)
    assert got["errors"] == 2 and got["opened"] == 2
    assert got["last_error"] == "GET /v2/prices/ETH-USD/spot: TimeoutError: read timed out"
    assert [r["spot"] for r in pt._load(p)] == [None, None]     # as before the counting


@pytest.fixture
def signed_lookups_fail(monkeypatch):
    """Kalshi auth 'configured', every signed order-book request rejected."""
    import lib.envload
    import lib.kalshi_auth as ka

    def rejected(path, **kw):
        raise PermissionError("401 Client Error: Unauthorized for url: " + path)
    monkeypatch.setattr(lib.envload, "load_env", lambda *a, **k: False)
    monkeypatch.setattr(ka, "can_sign", lambda: True)
    monkeypatch.setattr(ka, "signed_get", rejected)


def test_rejected_order_book_requests_are_counted(tmp_path, signed_lookups_fail):
    """Each failed lookup returned None and the entry went in ungated; the failure
    itself was swallowed. It is still admitted ungated - and now it is counted."""
    p, hb = tmp_path / "paper.jsonl", tmp_path / "hb.json"
    c = pt.run_cycle(get=exchange(0.55), now=T0, path=p, regime_fn=lambda: GOOD,
                     heartbeat=hb)                   # depth_fn=None: the real fetcher
    assert (c["opened"], c["ungated"]) == (2, 2)
    got = heartbeat_of(hb)
    assert got["errors"] == 2 and got["ungated"] == 2
    assert got["last_error"].startswith(
        "orderbook KXETH15M-26NOV251215-15: PermissionError: 401 Client Error")


def test_the_depth_lookup_other_traders_share_is_unchanged(signed_lookups_fail):
    """sleeve_paper calls _depth_fetcher() with no arguments: a failed lookup must still
    answer None, and a callback that itself breaks must not break the lookup."""
    assert pt._depth_fetcher()("T", "yes", 0.5) is None

    def broken(text):
        raise RuntimeError("counter broke")
    assert pt._depth_fetcher(on_error=broken)("T", "yes", 0.5) is None


def test_a_heartbeat_that_cannot_be_written_never_costs_the_cycle(tmp_path, monkeypatch,
                                                                  capsys):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    p = tmp_path / "paper.jsonl"
    assert cycle(p, 0.55, heartbeat=blocker / "hb.json")["opened"] == 2
    assert len(opens(p)) == 2                        # the ledger was written first
    assert "[warn] heartbeat not written" in capsys.readouterr().out

    # Windows: os.replace is refused while a reader holds the file open. Retried...
    hb, real, calls = tmp_path / "hb.json", os.replace, []

    def busy_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied")
        return real(src, dst)
    monkeypatch.setattr(pt.time, "sleep", lambda s: None)
    monkeypatch.setattr(pt.os, "replace", busy_once)
    assert pt._write_heartbeat(hb, {"ts": "x"}) is True and heartbeat_of(hb) == {"ts": "x"}

    # ... and given up on quietly if it stays refused: the old heartbeat stays whole
    def busy(src, dst):
        raise PermissionError(13, "Access is denied")
    monkeypatch.setattr(pt.os, "replace", busy)
    assert pt._write_heartbeat(hb, {"ts": "y"}) is False and heartbeat_of(hb) == {"ts": "x"}
    assert "[warn] heartbeat not written: PermissionError" in capsys.readouterr().out


def test_the_heartbeat_is_swapped_in_whole_never_written_in_place(tmp_path, monkeypatch):
    hb, seen, real = tmp_path / "paper_crypto_heartbeat.json", [], os.replace
    hb.write_text('{"ts":"old"}', encoding="utf-8")

    def spy(src, dst):
        seen.append((Path(src).name, json.loads(Path(src).read_text(encoding="utf-8")),
                     heartbeat_of(hb)))
        return real(src, dst)
    monkeypatch.setattr(pt.os, "replace", spy)
    cycle(tmp_path / "paper.jsonl", 0.55, heartbeat=hb)
    (tmp_name, new, old), = seen
    assert old == {"ts": "old"} and new["opened"] == 2      # complete before the swap
    assert tmp_name == "paper_crypto_heartbeat.tmp.json"    # matches data/*heartbeat*.json


def test_only_the_real_trader_writes_the_live_heartbeat(tmp_path, monkeypatch, lock_dir,
                                                        capsys):
    """A bare run_cycle (tests, the selftest the watchdog runs every 15 minutes, a
    research script) must never overwrite the live status file; `cycle` and the run loop
    must always write it."""
    hb = tmp_path / "live_heartbeat.json"
    monkeypatch.setattr(pt, "HEARTBEAT", hb)
    monkeypatch.setattr(pt, "LEDGER", tmp_path / "live.jsonl")
    monkeypatch.setattr(pt, "_depth_fetcher", lambda on_error=None: (lambda t, s, p: 100.0))
    monkeypatch.setattr(pt, "_regime_now", lambda: GOOD)
    cycle(tmp_path / "other.jsonl", 0.55)
    assert not hb.exists()

    # `cycle` and `run` read the REAL clock: give them a market that closes 13 minutes
    # from now, whenever this test runs, and row stamps from the same clock
    real_now = pt.datetime.now(timezone.utc)
    monkeypatch.setattr(pt, "_now_iso", lambda: pt.datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(s0, "_get", exchange(0.55, close=real_now + timedelta(minutes=13)))
    monkeypatch.setattr(sys, "argv", ["paper_trader.py", "cycle"])
    assert pt.main() == 0 and heartbeat_of(hb)["opened"] == 2
    hb.unlink()

    def one_cycle_then_stop(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(pt.time, "sleep", one_cycle_then_stop)
    monkeypatch.setattr(s0, "_get", dead)
    with pytest.raises(KeyboardInterrupt):
        pt.run_loop()
    got = heartbeat_of(hb)
    assert got["errors"] == 4 and got["open_now"] == 2
    line = [ln for ln in capsys.readouterr().out.splitlines() if "opened=" in ln][-1]
    assert line.endswith("ERRORS=4")                 # the loop's own line says so too


# ── the clock and the quote lag: measured, never corrected ───────────────────

class Resp:
    def __init__(self, body, headers):
        self._body, self.headers = body, headers

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_the_heartbeat_measures_the_pc_clock_against_kalshi_and_corrects_nothing(
        tmp_path, monkeypatch):
    """Every entry's time band comes from the PC clock, read once per cycle, and nothing
    compared it with anything. Now the heartbeat carries Kalshi's Date header minus the
    PC clock - and the ledger is byte-identical whatever that header says."""
    import requests
    monkeypatch.setattr(s0, "_CLOCK_SAMPLES", deque(maxlen=64))
    monkeypatch.setattr(pt, "_now_iso", lambda: "2026-11-25T12:00:21+00:00")

    def served_with(headers):
        def fake_requests_get(url, params=None, timeout=None):
            return Resp(exchange(0.55)(url, params), headers)
        return fake_requests_get
    ledgers, beats = [], []
    for name, headers in (("an_hour_off", {"Date": formatdate(time.time() + 3600,
                                                               usegmt=True)}),
                          ("no_date", {})):
        monkeypatch.setattr(requests, "get", served_with(headers))
        p, hb = tmp_path / f"{name}.jsonl", tmp_path / f"{name}_hb.json"
        c = pt.run_cycle(now=T0, path=p, depth_fn=lambda t, s, pr: 100.0,
                         regime_fn=lambda: GOOD, heartbeat=hb)      # get: the real _get
        assert c["opened"] == 2
        ledgers.append(p.read_bytes())
        beats.append(heartbeat_of(hb))
    assert abs(beats[0]["clock_skew_s"] - 3600) < 1.1 # Kalshi an hour ahead of the PC
    assert beats[1]["clock_skew_s"] is None           # not measurable: unknown, not 0
    assert ledgers[0] == ledgers[1]                   # same bets, mins_left and bands
    assert [r["mins_left"] for r in pt._load(tmp_path / "an_hour_off.jsonl")] == [14.67] * 2


def test_quote_lag_is_the_time_from_the_clock_reading_to_each_series_quote(tmp_path):
    """The clock is read once, before the ledger is loaded and before any request; a
    quote that arrives seconds later is still banded by it. Measured per series."""
    def slow(url, params=None):
        time.sleep(0.04)
        return exchange(0.55)(url, params)
    hb = tmp_path / "hb.json"
    c = cycle(tmp_path / "paper.jsonl", 0.55, get=slow, heartbeat=hb)
    lag = heartbeat_of(hb)["quote_lag_s"]
    assert lag == c["quote_lag_s"] and sorted(lag) == ["KXBTC15M", "KXETH15M"]
    # lower bounds only, each with 20 ms of slack: the Windows monotonic clock ticks
    # every 15.6 ms
    assert lag["KXBTC15M"] >= 0.06                    # the event list, then its markets
    assert lag["KXETH15M"] >= lag["KXBTC15M"] + 0.10  # + BTC's spot and ETH's two calls
    assert heartbeat_of(hb)["took_s"] >= lag["KXETH15M"] + 0.10   # + ETH spot, 2 sweeps


def test_a_series_whose_quotes_never_arrived_has_no_lag_rather_than_a_zero(tmp_path):
    def eth_down(url, params=None):
        if (params or {}).get("series_ticker") == "KXETH15M" and url.endswith("/events"):
            raise ConnectionError("reset by peer")
        return exchange(0.55)(url, params)
    hb = tmp_path / "hb.json"
    cycle(tmp_path / "paper.jsonl", 0.55, get=eth_down, heartbeat=hb)
    got = heartbeat_of(hb)
    assert list(got["quote_lag_s"]) == ["KXBTC15M"] and got["errors"] == 1


# ── stranded positions: counted in the heartbeat, nothing else changes ───────

def stranded(i, hours_past_close, rule="CONTROL_midprice"):
    """An entry row as the trader writes it, whose window closed that long before T0."""
    close = T0 - timedelta(hours=hours_past_close)
    return {"t": "open", "ts": (close - timedelta(minutes=12)).isoformat(),
            "ticker": f"KXBTC15M-OLD{i}-15", "series": "KXBTC15M", "rule": rule,
            "band": ">10min", "side": "yes", "price": 0.55, "contracts": 1, "fee": 0.02,
            "mins_left": 12.0, "v": 4}


def test_overdue_counts_open_positions_more_than_an_hour_past_their_close():
    live = {(r["ticker"], r["rule"]): r for r in (
        stranded(0, 0.5), stranded(1, 1.0), stranded(2, 1.02), stranded(3, 66.0))}
    assert pt.overdue_positions(live, T0) == {
        "n": 2, "oldest": "KXBTC15M-OLD3-15", "oldest_h": 66.0}
    assert pt.overdue_positions({}, T0) == {"n": 0, "oldest": None, "oldest_h": None}
    # a row whose close time cannot be worked out is unknown, not overdue - and no crash
    odd = {("A", "R"): {"t": "open", "ticker": "A", "rule": "R"},
           ("B", "R"): {"t": "open", "ts": "not a time", "mins_left": 3.0},
           ("C", "R"): {"t": "open", "ts": T0.isoformat(), "mins_left": None},
           ("D", "R"): dict(stranded(4, 5.0), ts=stranded(4, 5.0)["ts"].replace(
               "+00:00", "Z"))}                      # a Z stamp is read like +00:00
    assert pt.overdue_positions(odd, T0) == {"n": 1, "oldest": "D", "oldest_h": 5.0}


def test_stranded_positions_show_in_the_heartbeat_and_nothing_else_changes(tmp_path):
    """After an outage longer than the settled sweep reaches back (200 markets = 50 h)
    the open positions are never offered a result again. They kept all 8 slots, the
    trader opened nothing, and no file said so. Now the heartbeat does. DETECTION ONLY:
    they stay open and still hold their slots exactly as before."""
    p, hb = tmp_path / "paper.jsonl", tmp_path / "hb.json"
    rows = [stranded(i, 66.0 + i) for i in range(8)]
    pt._append(rows, p)
    before = p.read_bytes()
    c = cycle(p, 0.55, heartbeat=hb)
    assert (c["opened"], c["closed"], c["open_now"]) == (0, 0, 8)
    assert c["skipped"] == {"max_concurrent": 2}     # the cap still counts them
    assert p.read_bytes() == before                  # not settled, not voided, not touched
    assert heartbeat_of(hb)["overdue"] == {
        "n": 8, "oldest": "KXBTC15M-OLD7-15", "oldest_h": 73.0}
    assert c["overdue"] == heartbeat_of(hb)["overdue"]


def test_a_late_position_that_settles_this_cycle_is_not_overdue(tmp_path):
    """The PC wakes after 30 hours: everything that closed meanwhile is settled by the
    first cycle, so the first heartbeat must not raise a false alarm."""
    p, hb = tmp_path / "paper.jsonl", tmp_path / "hb.json"
    pt._append([stranded(0, 30.0), stranded(1, 30.0)], p)
    settled = [{"ticker": "KXBTC15M-OLD0-15", "result": "yes"},
               {"ticker": "KXBTC15M-OLD1-15", "result": "no"}]
    c = cycle(p, 0.55, get=exchange(0.55, settled=settled), heartbeat=hb)
    assert c["closed"] == 2 and heartbeat_of(hb)["overdue"]["n"] == 0


def test_the_loop_line_names_overdue_positions(tmp_path, monkeypatch, lock_dir, capsys):
    monkeypatch.setattr(pt, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setattr(pt, "LEDGER", tmp_path / "live.jsonl")
    monkeypatch.setattr(pt, "_depth_fetcher", lambda on_error=None: (lambda t, s, p: 100.0))
    monkeypatch.setattr(pt, "_regime_now", lambda: GOOD)
    monkeypatch.setattr(pt, "run_cycle", lambda **kw: _real_cycle(now=T0, **kw))
    monkeypatch.setattr(s0, "_get", exchange(0.55))
    pt._append([stranded(i, 66.0) for i in range(3)], pt.LEDGER)

    def one_cycle_then_stop(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(pt.time, "sleep", one_cycle_then_stop)
    with pytest.raises(KeyboardInterrupt):
        pt.run_loop()
    line = [ln for ln in capsys.readouterr().out.splitlines() if "opened=" in ln][-1]
    assert "opened=2" in line and line.endswith("OVERDUE=3")


_real_cycle = pt.run_cycle


def test_an_overdue_count_that_cannot_be_worked_out_is_a_missing_key_not_a_null(
        tmp_path, monkeypatch):
    """Readers treat a missing key as unknown. A null block would only trip them up, and
    a status figure must never be able to fail a cycle whose rows are already written."""
    def broken(live, now):
        raise RuntimeError("cannot tell")
    monkeypatch.setattr(pt, "overdue_positions", broken)
    p, hb = tmp_path / "paper.jsonl", tmp_path / "hb.json"
    c = cycle(p, 0.55, heartbeat=hb)
    assert c["opened"] == 2 and len(opens(p)) == 2 and c["overdue"] is None
    got = heartbeat_of(hb)
    assert "overdue" not in got and got["opened"] == 2


def test_counting_a_failed_request_never_replaces_its_error(tmp_path, monkeypatch):
    """The settle sweep's own handler must still see the request's exception even if the
    bookkeeping around it breaks."""
    def broken(what, e):
        raise RuntimeError("bookkeeping broke")
    monkeypatch.setattr(pt, "_request_error", broken)
    seen = []

    def failing_sweep(series, get):
        try:
            get("https://x.invalid/trade-api/v2/markets", {"series_ticker": series})
        except Exception as e:  # noqa: BLE001
            seen.append(type(e).__name__)
            raise
    monkeypatch.setattr(s0, "fetch_settled", failing_sweep)

    def sweep_down(url, params=None):
        if "x.invalid" in url:
            raise ConnectionError("down")
        return exchange(0.55)(url, params)
    c = cycle(tmp_path / "paper.jsonl", 0.55, get=sweep_down)
    assert seen == ["ConnectionError", "ConnectionError"]
    assert c["opened"] == 2 and c["errors"] == 2 and c["last_error"] == "request failed"
