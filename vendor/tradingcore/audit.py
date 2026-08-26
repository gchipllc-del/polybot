"""Fallback audit log: append-only JSONL, no external deps."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_LOG = Path(os.environ.get("TRADINGCORE_AUDIT_LOG") or (_ROOT / "data" / "audit_events.jsonl"))


def log_event(source: str = "", event: str = "", data=None, result: str = "", **extra) -> None:
    """Record one audit event. Signature is permissive on purpose — callers across this
    repo pass positional source/event plus assorted kwargs (result=, level=, ...)."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "source": source,
           "event": event, "data": data, "result": result}
    if extra:
        rec.update({k: v for k, v in extra.items() if _jsonable(v)})
    try:
        _LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
    except OSError:
        pass          # audit logging must never take down a caller


def _jsonable(v) -> bool:
    try:
        json.dumps(v, default=str)
        return True
    except (TypeError, ValueError):
        return False


def get_recent_events(limit: int = 50, source: str | None = None) -> list[dict]:
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
        if source and d.get("source") != source:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out
