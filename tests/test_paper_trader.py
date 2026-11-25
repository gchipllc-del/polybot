"""paper_trader - the crypto forward trader's safety net: what a crash, a second copy or a
stalled cycle may cost, each pinned. No test here changes what a frozen rule trades."""
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
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


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """Only the frozen rules, and no fair-value history: nothing here may depend on the
    overlay file or the collector log of the machine the suite runs on."""
    monkeypatch.setattr(pt, "_rules",
                        lambda: (list(sb.RULES), sb.BANDS, sb.kalshi_taker_fee))
    monkeypatch.setattr(s0, "LOG", tmp_path / "no_stage0.jsonl")


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
    return pt.run_cycle(now=T0 + timedelta(minutes=minute), path=path, **kw)


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
        twin.kill()
        twin.wait(timeout=10)
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
        twin.kill()
        twin.wait(timeout=10)
    assert "paper_trader" not in si._HELD
