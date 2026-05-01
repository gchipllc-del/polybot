"""
Defense-in-depth test isolation for polybot.

Mirrors the traderbot conftest after the 2026-05-01 test-pollution incident
(where mid-refactor tests wrote into the live positions.json + audit_log).
Polybot has the same risk pattern: every module declares its own
POSITIONS_PATH / TRADE_HISTORY_PATH / AUDIT_FILE module global, and tests
that forget to monkeypatch one of them silently mutate live data.

This conftest auto-redirects EVERY test's writes to per-session tmp paths.
Tests that explicitly patch a module's path still work — their patch
overrides the conftest.
"""

import importlib
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def _isolated_dirs(tmp_path_factory):
    return {
        "data": tmp_path_factory.mktemp("isolated_data"),
        "logs": tmp_path_factory.mktemp("isolated_logs"),
    }


@pytest.fixture(autouse=True)
def _isolate_live_writes(_isolated_dirs, monkeypatch):
    data = _isolated_dirs["data"]
    logs = _isolated_dirs["logs"]
    pos = data / "positions.json"
    hist = data / "trade_history.json"

    pos_modules = ("lib.resolution_tracker", "lib.monitor",
                   "lib.dashboard_data", "agents.risk_agent",
                   "agents.compliance_agent")
    for name in pos_modules:
        try:
            mod = importlib.import_module(name)
            monkeypatch.setattr(mod, "POSITIONS_PATH", pos, raising=False)
        except ImportError:
            pass

    hist_modules = ("lib.resolution_tracker", "lib.dashboard_data",
                    "agents.hermes_optimizer", "agents.compliance_agent")
    for name in hist_modules:
        try:
            mod = importlib.import_module(name)
            monkeypatch.setattr(mod, "TRADE_HISTORY_PATH", hist, raising=False)
        except ImportError:
            pass

    try:
        from lib import audit
        monkeypatch.setattr(audit, "AUDIT_FILE",
                            logs / "audit_log.jsonl", raising=False)
        if hasattr(audit, "LOG_DIR"):
            monkeypatch.setattr(audit, "LOG_DIR", logs, raising=False)
    except ImportError:
        pass

    yield
