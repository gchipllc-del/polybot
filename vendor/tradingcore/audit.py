"""Fallback audit log: append-only JSONL, stdlib only, credential-redacting.

The contract is this repo's call sites and readers:
  everywhere              log_event(component, action, details=None, result=..., **extra)
  lib/dashboard_data.py:340  get_recent_events(n) - newest first; it reads e["details"]
  templates/dashboard.html   reads e.timestamp, e.action
so each record carries timestamp / component / action / details / result, plus the
aliases source (= component) and event (= action) for older readers.

REDACTION. Anything logged under a credential-looking key (api_key, secret, token,
password, private_key, authorization, ...) is replaced before it touches disk, at any
depth, and so is any string that looks like a PEM private key - whatever its key. The
first version of this shim wrote such values verbatim; a comparison against an
independently rebuilt core caught it. The project rule is that keys and .pem contents
never land in a file that is not .env.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_LOG = Path(os.environ.get("TRADINGCORE_AUDIT_LOG") or (_ROOT / "data" / "audit_events.jsonl"))

REDACTED = "***REDACTED***"
_SECRET_KEY = re.compile(r"secret|passw|passphrase|api[_-]?key|apikey|token|private[_-]?key|"
                         r"authorization|credential|signature|cookie", re.IGNORECASE)
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def redact(value, key: str = ""):
    """Recursively replace credential-looking values. Pure; returns a new object."""
    if key and _SECRET_KEY.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key) for v in value]
    if isinstance(value, str) and _PEM.search(value):
        return REDACTED
    return value


def log_event(component: str = "", action: str = "", details=None, result: str = "",
              **extra) -> None:
    """Record one audit event. Permissive on purpose - callers across this repo pass
    positional component/action plus assorted kwargs (result=, level=, ...). Never
    raises: audit logging must not take a caller down."""
    rec = {"timestamp": datetime.now(timezone.utc).isoformat(),
           "component": component, "source": component,
           "action": action, "event": action,
           "details": redact(details if details is not None else {}),
           "result": result}
    for k, v in extra.items():
        try:
            json.dumps(v, default=str)
        except (TypeError, ValueError):
            continue
        rec[k] = redact(v, k)
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
    except OSError:
        pass


def get_recent_events(limit: int = 50, source: str | None = None) -> list[dict]:
    """Newest first. `source` filters on component."""
    if not _LOG.exists():
        return []
    try:
        lines = _LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        if source and source not in (d.get("component"), d.get("source")):
            continue
        d.setdefault("details", d.get("data") or {})       # pre-2026-11 records
        d.setdefault("action", d.get("event", ""))
        d.setdefault("timestamp", d.get("ts", ""))
        out.append(d)
        if len(out) >= max(0, int(limit)):
            break
    return out
