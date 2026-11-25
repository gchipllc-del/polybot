"""The Kalshi balance on the dashboards: one cached value per process, fetched on its own
thread - never inside a page request, never between the snapshot's file panels.

It used to be fetched inline: on every request of the weather page (a 20 s poll) and in
every crypto snapshot rebuild. A Kalshi that accepted the connection and never answered
held each of them for 15 s, and both tabs open made about 5,800 signed calls a day.

Kalshi is stood in for by stubs on lib.kalshi_auth; nothing here touches the network.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import lib.kalshi_auth as ka  # noqa: E402
import lib.kalshi_dashboard as kd  # noqa: E402
import lib.kalshi_weather_dashboard as wd  # noqa: E402


def _join_fetches(timeout=5.0):
    for t in threading.enumerate():
        if t.name == "kalshi-balance":
            t.join(timeout)


class Kalshi:
    """A stand-in for the signed API: counts calls, can be slow, silent or failing."""

    def __init__(self, monkeypatch, configured=True):
        self.calls, self.timeouts = [], []
        self.gate = threading.Event()       # clear = Kalshi accepts and does not answer
        self.gate.set()
        self.fail = None
        self.cents = 1234567
        self.configured = configured
        monkeypatch.setattr(ka, "status", lambda: {"can_sign": self.configured})
        monkeypatch.setattr(ka, "can_sign", lambda: self.configured)
        monkeypatch.setattr(ka, "signed_get", self.signed_get)

    def signed_get(self, path, *, params=None, timeout=15):
        self.calls.append(path)
        self.timeouts.append(timeout)
        self.gate.wait(3)                   # (a bound for a test gone wrong, not a delay)
        if self.fail:
            raise self.fail
        return {"balance": self.cents, "portfolio_value": 0}


@pytest.fixture
def kalshi(monkeypatch):
    _join_fetches()
    monkeypatch.setattr(kd, "_BAL", {}, raising=False)        # a process that just started
    stub = Kalshi(monkeypatch)
    yield stub
    stub.gate.set()
    _join_fetches()


def _settled():
    """The account panel once the fetch that is due, or in flight, has ended."""
    kd._account_payload()
    _join_fetches()
    return kd._account_payload()


def test_a_request_never_waits_for_kalshi(kalshi):
    kalshi.gate.clear()                                 # accepts, never answers
    t0 = time.monotonic()
    first = kd._account_payload()
    assert time.monotonic() - t0 < 1.5                  # was 15 s: the call's own timeout
    for _ in range(20):                                 # a poll every 20 s, twenty times
        kd._account_payload()
    assert time.monotonic() - t0 < 1.5
    assert first["pending"] is True and first["balance_dollars"] is None
    assert first["error"] is None                       # not known yet is not an error
    assert len(kalshi.calls) <= 1                       # one fetch in flight, not twenty-one
    kalshi.gate.set()
    acc = _settled()
    assert acc["balance_dollars"] == 12345.67 and acc["pending"] is False
    assert acc["error"] is None and acc["fetched_at"] and 0 <= acc["age_s"] < 5
    assert kalshi.calls == ["/portfolio/balance"]


def test_one_signed_call_per_ttl_however_many_requests_and_rebuilds(kalshi, monkeypatch):
    assert _settled()["balance_dollars"] == 12345.67
    for name in ("_live_markets_payload", "_paper_payload", "_cron_health_payload",
                 "_stage0_payload", "_shadow_payload", "_trading_status_payload",
                 "_paper_crypto_payload"):
        monkeypatch.setattr(kd, name, lambda: {})
    for _ in range(40):
        kd._account_payload()
        assert kd._build_all()["account"]["balance_dollars"] == 12345.67
    _join_fetches()
    assert len(kalshi.calls) == 1                       # was 80: one per call
    kalshi.cents = 2000000
    monkeypatch.setattr(kd, "BALANCE_TTL_S", 0.0)       # the cached value is now old
    assert kd._account_payload()["balance_dollars"] == 12345.67      # served at once...
    assert _wait_for(lambda: len(kalshi.calls) == 2)                 # ...and refreshed
    monkeypatch.setattr(kd, "BALANCE_TTL_S", 300.0)
    assert _settled()["balance_dollars"] == 20000.0
    assert 3.0 <= kd.BALANCE_TIMEOUT_S <= 5.0 and set(kalshi.timeouts) == {kd.BALANCE_TIMEOUT_S}


def _wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_a_failed_fetch_keeps_the_last_balance_with_its_age(kalshi, monkeypatch):
    good = _settled()
    assert good["balance_dollars"] == 12345.67
    kalshi.fail = TimeoutError("Read timed out. (read timeout=4.0)")
    monkeypatch.setattr(kd, "BALANCE_TTL_S", 0.0)
    kd._account_payload()                               # old enough: starts a fetch
    monkeypatch.setattr(kd, "BALANCE_TTL_S", 300.0)
    acc = _settled()
    assert acc["balance_dollars"] == 12345.67           # the last one that came back
    assert acc["fetched_at"] == good["fetched_at"] and acc["age_s"] >= good["age_s"]
    assert "Read timed out" in acc["error"] and acc["pending"] is False
    # a failure is retried after BALANCE_RETRY_S - sooner than the TTL, but not at once
    n = len(kalshi.calls)
    for _ in range(10):
        kd._account_payload()
    _join_fetches()
    assert len(kalshi.calls) == n and kd.BALANCE_RETRY_S < kd.BALANCE_TTL_S
    kalshi.fail = None
    monkeypatch.setattr(kd, "BALANCE_RETRY_S", 0.0)
    kd._account_payload()
    back = _settled()
    assert back["error"] is None and back["fetched_at"] > good["fetched_at"]


def test_without_keys_the_panel_says_so_and_kalshi_is_never_called(kalshi):
    kalshi.configured = False
    acc = _settled()
    assert acc["error"] == "auth not configured (run kalshi-auth-status)"
    assert acc["balance_dollars"] is None and acc["pending"] is False
    assert acc["auth"] == {"can_sign": False} and kalshi.calls == []


def test_the_weather_page_reads_the_cache_too(kalshi, monkeypatch):
    """Its /api/all made the signed call inside EVERY request, with nothing cached."""
    for name in ("_live_markets_payload", "_paper_payload", "_cron_health_payload"):
        monkeypatch.setattr(wd, name, lambda: {})
    kalshi.gate.clear()
    client = wd.make_app().test_client()
    t0 = time.monotonic()
    bodies = [client.get("/api/all").get_json()]
    assert time.monotonic() - t0 < 1.5                  # was 15 s for each request
    bodies += [client.get("/api/all").get_json() for _ in range(2)]
    assert time.monotonic() - t0 < 1.5
    assert all(b["account"]["pending"] for b in bodies) and len(kalshi.calls) <= 1
    kalshi.gate.set()
    _join_fetches()
    assert client.get("/api/all").get_json()["account"]["balance_dollars"] == 12345.67
    assert kd._account_payload()["balance_dollars"] == 12345.67      # one cache, one call
    assert len(kalshi.calls) == 1


def test_a_fetch_thread_that_cannot_start_is_said_and_tried_again(kalshi, monkeypatch):
    class NoNewThreads(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")
    with monkeypatch.context() as m:
        m.setattr(kd.threading, "Thread", NoNewThreads)
        acc = kd._account_payload()
    assert "could not start" in acc["error"] and "can't start new thread" in acc["error"]
    assert acc["pending"] is False and kalshi.calls == []
    monkeypatch.setattr(kd, "BALANCE_RETRY_S", 0.0)
    kd._account_payload()
    assert _settled()["balance_dollars"] == 12345.67


def test_a_rebuild_reads_the_account_after_the_file_panels(monkeypatch):
    """The signed call sat between the file panels, so a silent Kalshi stretched every
    rebuild by 15 s. The panel is now a cache read, and the last one built - by then the
    fetch started at boot is in, so the first snapshot already carries the balance."""
    order = []
    for key, name in (("live", "_live_markets_payload"), ("paper", "_paper_payload"),
                      ("account", "_account_payload"), ("cron", "_cron_health_payload"),
                      ("stage0", "_stage0_payload"), ("shadow", "_shadow_payload"),
                      ("trading", "_trading_status_payload"),
                      ("paper_crypto", "_paper_crypto_payload")):
        monkeypatch.setattr(kd, name, lambda key=key: order.append(key) or {})
    body = kd._build_all()
    assert order[-1] == "account" and len(order) == 8 and "account" in body


def test_both_pages_show_the_balance_with_its_age_as_text():
    for page in (kd._TEMPLATE, wd._TEMPLATE):
        assert "renderAccount(data.account" in page
        assert "fetching the balance" in page and "acc.fetched_at" in page
        assert "the last fetch failed, so this is the last balance that came back" in page
        block = page[page.index("function renderAccount"):page.index("async function refresh")]
        assert "innerHTML" not in block and "textContent" in block
