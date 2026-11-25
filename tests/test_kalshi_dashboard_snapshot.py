"""The crypto dashboard's /api/all must answer at once: every panel was recomputed per
request (a full read of a 190k-row collector log among them), past the healthcheck's
8 s budget - the persistent '[FAIL] api /api/all unreachable: TimeoutError'."""
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import lib.kalshi_dashboard as kd  # noqa: E402

SECTIONS = {"live": "_live_markets_payload", "paper": "_paper_payload",
            "account": "_account_payload", "cron": "_cron_health_payload",
            "stage0": "_stage0_payload", "shadow": "_shadow_payload",
            "trading": "_trading_status_payload", "paper_crypto": "_paper_crypto_payload"}


def _setup(monkeypatch, calls, slow=0.0, broken=()):
    # a rebuild an earlier test left running would land in THIS test's snapshot
    for t in threading.enumerate():
        if t.name == "api-all-refresh":
            t.join(5.0)
    monkeypatch.setattr(kd, "_ALL", {"payload": None, "t": 0.0, "busy": False})
    monkeypatch.setattr(kd, "ALL_TTL_S", 30.0)
    for key, fname in SECTIONS.items():
        def fn(key=key):
            calls.append(key)
            if key in broken:
                raise RuntimeError(f"{key} feed down")
            if key == "stage0" and slow:
                time.sleep(slow)
            return {"panel": key, "n": len(calls)}
        monkeypatch.setattr(kd, fname, fn)


def _wait_idle(timeout=5.0):
    end = time.monotonic() + timeout
    while kd._ALL["busy"] and time.monotonic() < end:
        time.sleep(0.02)


def test_first_request_builds_then_the_snapshot_is_reused(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    first = kd._all_payload(wait_s=3)
    assert set(SECTIONS) <= set(first) and len(calls) == 8
    _wait_idle()
    second = kd._all_payload(wait_s=3)
    assert len(calls) == 8 and second["stage0"] == first["stage0"]       # no recompute


def test_a_stale_snapshot_is_served_at_once_while_one_rebuild_runs(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, slow=0.6)
    old = kd._all_payload(wait_s=3)
    _wait_idle()
    monkeypatch.setattr(kd, "ALL_TTL_S", 0.0)                            # now stale
    t0 = time.monotonic()
    served = kd._all_payload(wait_s=3)
    again = kd._all_payload(wait_s=3)                                    # still rebuilding
    assert time.monotonic() - t0 < 0.4                 # not the 0.6 s rebuild
    assert served["stage0"] == old["stage0"] == again["stage0"]
    assert len(calls) <= 16                            # one rebuild in flight, not two
    _wait_idle()
    assert kd._all_payload(wait_s=3)["stage0"] != old["stage0"]          # replaced
    _wait_idle()                   # (that request started one more: let it finish here)


def test_a_broken_panel_degrades_to_its_own_error(monkeypatch):
    calls = []
    _setup(monkeypatch, calls, broken=("account",))
    out = kd._all_payload(wait_s=3)
    assert out["account"] == {"error": "account feed down"}
    assert out["live"]["panel"] == "live"


def test_route_serves_the_snapshot(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    r = kd.make_app().test_client().get("/api/all")
    assert r.status_code == 200 and set(SECTIONS) <= set(r.get_json())


def test_stage0_panel_survives_an_undecodable_byte(tmp_path, monkeypatch):
    """The collector log is append-only, so one stray byte (a disk fault, a foreign
    writer) stays for good - and the strict read left the Stage-0 panel in permanent
    error. The damaged line is skipped like a torn one; the rest still joins."""
    import json

    import stage0_collector as s0
    log = tmp_path / "stage0_crypto.jsonl"
    tk = "KXBTC15M-26NOV250115-00"
    rows = [{"t": "obs", "ts": "2026-11-25T01:06:00+00:00", "ticker": tk, "mins_left": 9.0,
             "yes_ask": 0.55, "no_ask": 0.47},
            {"t": "settle", "ts": "2026-11-25T01:16:00+00:00", "ticker": tk, "result": "yes"}]
    log.write_bytes(json.dumps(rows[0]).encode() + b"\n\xff\n"
                    + json.dumps(rows[1]).encode() + b"\n")
    monkeypatch.setattr(s0, "LOG", log)
    panel = kd._stage0_payload()
    assert "error" not in panel, panel
    assert (panel["joined"], panel["n_settles"]) == (1, 1)


# ── what the page is told when the server does not know ──────────────────────────────

class _NoNewThreads(threading.Thread):
    """What a process short of memory gets from Thread.start()."""

    def start(self):
        raise RuntimeError("can't start new thread")


def _serve_fresh(monkeypatch):
    """Read the snapshot without starting another rebuild."""
    monkeypatch.setattr(kd, "ALL_TTL_S", 30.0)
    return kd._all_payload(wait_s=3)


def test_a_rebuild_thread_that_cannot_start_does_not_freeze_the_snapshot(monkeypatch):
    """busy was set BEFORE the thread was started. When start() failed (a process short
    of memory) the request answered 500, the flag stayed set with no thread alive, and no
    rebuild ever ran again: every later poll got HTTP 200 with the same old numbers."""
    calls = []
    _setup(monkeypatch, calls)
    old = kd._all_payload(wait_s=3)
    _wait_idle()
    monkeypatch.setattr(kd, "ALL_TTL_S", 0.0)                            # now stale
    with monkeypatch.context() as m:
        m.setattr(kd.threading, "Thread", _NoNewThreads)
        served = kd._all_payload(wait_s=3)             # used to raise -> HTTP 500
    assert served["stage0"] == old["stage0"]           # the last snapshot, still served
    assert "can't start new thread" in served["last_rebuild_error"]
    assert served["rebuild_started"] is None and not kd._ALL["busy"]
    kd._all_payload(wait_s=3)                          # threads start again: so does it
    _wait_idle()
    final = _serve_fresh(monkeypatch)
    assert final["stage0"] != old["stage0"] and final["last_rebuild_error"] is None
    assert final["rebuild_started"] is None


def test_a_first_snapshot_that_cannot_start_says_so_at_once(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    with monkeypatch.context() as m:
        m.setattr(kd.threading, "Thread", _NoNewThreads)
        t0 = time.monotonic()
        body = kd._all_payload(wait_s=3)
    assert time.monotonic() - t0 < 1.0                 # nothing is building: do not wait
    assert set(body) == {"warming", "ts", "rebuild_started", "last_rebuild_error"}
    assert "can't start new thread" in body["warming"]["error"]
    assert "can't start new thread" in body["last_rebuild_error"]
    assert body["rebuild_started"] is None and calls == []
    assert set(SECTIONS) <= set(kd._all_payload(wait_s=3))               # and recovers
    _wait_idle()


def test_a_busy_flag_with_no_live_thread_does_not_block_rebuilds(monkeypatch):
    """A rebuild in flight is judged by its THREAD, not by the flag (which can be left
    set) and not by a timer (which would start a second rebuild on top of a slow one)."""
    calls = []
    _setup(monkeypatch, calls)
    first = kd._all_payload(wait_s=3)
    _wait_idle()
    kd._ALL["busy"] = True                             # left behind: no thread is alive
    monkeypatch.setattr(kd, "ALL_TTL_S", 0.0)
    kd._all_payload(wait_s=3)
    _wait_idle()
    assert _serve_fresh(monkeypatch)["stage0"] != first["stage0"]


def test_the_body_says_whether_a_rebuild_is_in_flight(monkeypatch):
    """age_s alone cannot tell a stuck snapshot from an idle page: a lone request after
    a quiet stretch always gets an old snapshot. rebuild_started (the rebuild running
    now) and last_rebuild_error (why the latest one failed) say what the server is doing."""
    calls = []
    _setup(monkeypatch, calls, slow=0.6)
    before = datetime.now(timezone.utc)
    warming = kd._all_payload(wait_s=0.05)             # the first build is still running
    assert set(warming) == {"warming", "ts", "rebuild_started", "last_rebuild_error"}
    started = datetime.fromisoformat(warming["rebuild_started"])
    assert before <= started <= datetime.now(timezone.utc)
    assert warming["last_rebuild_error"] is None
    _wait_idle()
    idle = kd._all_payload(wait_s=3)
    assert idle["rebuild_started"] is None and idle["last_rebuild_error"] is None
    monkeypatch.setattr(kd, "ALL_TTL_S", 0.0)
    served = kd._all_payload(wait_s=3)                 # stale: this request starts one
    assert datetime.fromisoformat(served["rebuild_started"]) >= started
    again = kd._all_payload(wait_s=3)                  # the same rebuild, not a second
    assert again["rebuild_started"] == served["rebuild_started"] and len(calls) <= 16
    _wait_idle()
    assert _serve_fresh(monkeypatch)["rebuild_started"] is None


def test_a_rebuild_that_raises_is_reported_in_the_body(monkeypatch):
    calls = []
    _setup(monkeypatch, calls)
    good = kd._build_all

    def broken():
        raise MemoryError("out of memory")
    monkeypatch.setattr(kd, "_build_all", broken)
    body = kd._all_payload(wait_s=3)
    assert "warming" in body and "MemoryError: out of memory" in body["last_rebuild_error"]
    assert "MemoryError" in body["warming"]["error"] and not kd._ALL["busy"]
    monkeypatch.setattr(kd, "_build_all", good)
    body = kd._all_payload(wait_s=3)
    _wait_idle()
    assert set(SECTIONS) <= set(body) and _serve_fresh(monkeypatch)["last_rebuild_error"] is None


def test_an_unreadable_ledger_is_not_reported_as_paper_off(tmp_path, monkeypatch):
    """_trading_status_payload swallowed the read error and reported zero rows: with the
    ledger locked the page said 'PAPER OFF ... PAPER ARMED - waiting for a frozen rule to
    fire' while the Paper book panel beside it showed the error. Unknown is not zero."""
    import json

    import paper_trader as pt
    ledger = tmp_path / "paper_crypto15.jsonl"
    monkeypatch.setattr(pt, "LEDGER", ledger)
    assert kd._trading_status_payload()["paper"] is False              # no ledger: armed
    ledger.write_text(json.dumps({"t": "open", "ticker": "T", "rule": "H1"}) + "\n[1, 2]\n",
                      encoding="utf-8")
    ok = kd._trading_status_payload()                  # a damaged line is not a trade
    assert (ok["paper"], ok["paper_rows"]) == (True, 1)
    ledger.unlink()
    ledger.mkdir()                 # cannot be opened, as a file another program holds
    panel = kd._build_all()["trading"]
    assert set(panel) == {"error"} and panel["error"], panel
    # the page says UNKNOWN for it, as text
    assert "UNKNOWN - the paper ledger could not be read: ' + tr.error" in kd._TEMPLATE


def test_the_page_says_warming_instead_of_drawing_zeros():
    """Until the first snapshot exists the body is {'warming': ...}. The page drew every
    panel from it: 'PAPER OFF', 'closed 0 trades', 'joined observations: 0' under a fresh
    'last refresh' time. It must say it is warming up and leave the panels alone."""
    page = kd._TEMPLATE
    at = page.index("if (data.warming) {")
    assert at < page.index("// Account") and at < page.index("// Trading status")
    block = page[at:page.index("// Account")]
    assert "WARMING UP" in block and "return;" in block and "setTimeout(refresh" in block
    # and a snapshot that has stopped moving cannot pass for a fresh one
    assert "'last refresh: '" not in page
    for said in ("STALE - ", "data.age_s", "data.last_rebuild_error", "data.rebuild_started"):
        assert said in page, said
