"""The crypto dashboard's /api/all must answer at once: every panel was recomputed per
request (a full read of a 190k-row collector log among them), past the healthcheck's
8 s budget - the persistent '[FAIL] api /api/all unreachable: TimeoutError'."""
import sys
import time
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
