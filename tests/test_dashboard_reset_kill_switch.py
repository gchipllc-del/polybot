"""Tests for the token-gated kill-switch reset endpoint (2026-06-01).

The dashboard binds to 0.0.0.0 and is otherwise unauthenticated, so this — the
only money-affecting control — MUST fail closed without the secret and only act
with the correct token. Real live state is never touched (executor STATE_PATH is
redirected to a temp file).
"""
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _client_with_token(token: str | None):
    if token is None:
        os.environ.pop("KILL_SWITCH_RESET_TOKEN", None)
    else:
        os.environ["KILL_SWITCH_RESET_TOKEN"] = token
    import lib.dashboard_web as dw
    importlib.reload(dw)
    return dw.app.test_client()


def test_disabled_when_no_token():
    c = _client_with_token(None)
    r = c.post("/api/kalshi_live/reset_kill_switch", json={"token": "x"})
    assert r.status_code == 503
    assert r.get_json()["ok"] is False


def test_rejects_wrong_token():
    c = _client_with_token("right")
    r = c.post("/api/kalshi_live/reset_kill_switch", json={"token": "wrong"})
    assert r.status_code == 403


def test_rejects_missing_body():
    c = _client_with_token("right")
    r = c.post("/api/kalshi_live/reset_kill_switch")
    assert r.status_code == 403


def test_correct_token_clears_state(tmp_path):
    # Redirect executor state to a temp file with the kill switch tripped.
    os.environ["KILL_SWITCH_RESET_TOKEN"] = "good"
    import lib.kalshi_live_executor as ex
    st = tmp_path / "state.json"
    st.write_text(json.dumps({"kill_switch_tripped": True,
                              "consecutive_losses": 6}))
    ex.STATE_PATH = st
    import lib.dashboard_web as dw
    importlib.reload(dw)
    c = dw.app.test_client()
    r = c.post("/api/kalshi_live/reset_kill_switch", json={"token": "good"})
    assert r.status_code == 200
    j = r.get_json()
    assert j["ok"] is True and j["was_tripped"] is True
    after = json.loads(st.read_text())
    assert after["kill_switch_tripped"] is False
    assert after["consecutive_losses"] == 0


def test_header_token_also_accepted(tmp_path):
    os.environ["KILL_SWITCH_RESET_TOKEN"] = "good"
    import lib.kalshi_live_executor as ex
    st = tmp_path / "state.json"
    st.write_text(json.dumps({"kill_switch_tripped": True, "consecutive_losses": 5}))
    ex.STATE_PATH = st
    import lib.dashboard_web as dw
    importlib.reload(dw)
    c = dw.app.test_client()
    r = c.post("/api/kalshi_live/reset_kill_switch",
               headers={"X-Reset-Token": "good"})
    assert r.status_code == 200


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
