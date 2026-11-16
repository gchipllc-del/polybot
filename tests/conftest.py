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

    # The core's own logs. This block used to patch lib.audit, which no longer exists
    # (audit, kelly and calibration moved into tradingcore), so the ImportError was
    # swallowed and every log_event a test reached appended to the live
    # data/audit_events.jsonl, which the dashboard shows as recent activity.
    # vendor/tradingcore reads its path from the module global _LOG at call time. The other
    # names are the pre-move lib.audit / lib.calibration globals, which the real package
    # may still carry. Only names a module actually has get patched. The env vars cover a
    # fresh import and any subprocess a test starts.
    monkeypatch.setenv("TRADINGCORE_AUDIT_LOG", str(logs / "audit_events.jsonl"))
    monkeypatch.setenv("TRADINGCORE_FORECAST_LOG", str(logs / "forecast_records.jsonl"))
    core_paths = {
        "tradingcore.audit": {"_LOG": logs / "audit_events.jsonl",
                              "AUDIT_FILE": logs / "audit_log.jsonl", "LOG_DIR": logs},
        "tradingcore.calibration": {"_LOG": logs / "forecast_records.jsonl",
                                    "CALIBRATION_FILE": data / "calibration_log.json"},
    }
    for name, attrs in core_paths.items():
        try:
            importlib.import_module("lib")    # puts vendor/ on sys.path if the real one is absent
            mod = importlib.import_module(name)
        except ImportError:
            continue
        for attr, path in attrs.items():
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, path)

    yield
