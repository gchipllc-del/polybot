"""
Cross-process disk cache for short-lived REST signals.

Three signals (OFI, funding, MTF) live in per-module in-memory dicts
that get wiped on process restart. The scanner runs every 60s; if
it's just been restarted, the first cycle pays the full network cost
even when valid cache data was sitting in memory moments ago.

This module adds a thin disk layer between the network call and the
in-memory cache. Each module reads from disk on miss before paying
the REST cost; writes go to both layers.

The disk cache is intentionally per-module-namespaced and per-key —
so OFI for BTCUSDT lives in its own file, separate from funding for
BTCUSDT. This keeps writes atomic (single small file) and parallel-
safe (one writer per file under flock).

Schema (JSON file):
    {
        "key": "BTCUSDT|10",
        "value": (signal, meta),   # whatever the caller stores
        "ts_epoch": 1779184567.12
    }

TTL semantics match each caller's existing in-memory TTL — disk
entries older than the module's TTL are ignored on read (treated as
miss).

Usage:
    from lib.kalshi_cache import get, put
    cached = get("orderflow", "BTCUSDT|10", max_age=30)
    if cached is None:
        # cold path — fetch from network, then:
        put("orderflow", "BTCUSDT|10", (signal, meta))
"""

from __future__ import annotations

import fcntl
import json
import time
from pathlib import Path
from typing import Any, Optional


_ROOT = Path(__file__).resolve().parent.parent
_CACHE_DIR = _ROOT / "data" / "cache" / "kalshi"


def _safe_key(name: str) -> str:
    """Hash-safe filename from arbitrary cache key (preserves readability
    for the common alphanumeric+pipe case)."""
    safe = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_", "."):
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe)[:200]


def _path(namespace: str, key: str) -> Path:
    """Compose the on-disk cache path."""
    return _CACHE_DIR / namespace / (_safe_key(key) + ".json")


def get(namespace: str, key: str, *, max_age: float) -> Optional[Any]:
    """Return the cached value if still fresh, else None.

    `max_age` is in seconds — same TTL the caller uses for its
    in-memory cache. Stale entries are silently ignored (treated as
    miss); they're not auto-deleted to keep this purely read-path.
    """
    p = _path(namespace, key)
    if not p.exists():
        return None
    try:
        with open(p) as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    ts = data.get("ts_epoch")
    if not isinstance(ts, (int, float)):
        return None
    if (time.time() - ts) > max_age:
        return None
    return data.get("value")


def put(namespace: str, key: str, value: Any) -> None:
    """Persist value under (namespace, key). Best-effort — never raises
    into the caller. A failed write just means the next process restart
    pays the cold network cost again."""
    try:
        p = _path(namespace, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        payload = {
            "key": key,
            "value": value,
            "ts_epoch": time.time(),
        }
        with open(tmp, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(payload, f)
                f.flush()
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        tmp.replace(p)
    except (OSError, TypeError):
        # Can't serialize or can't write — fail quietly. Cache is
        # observability, not safety-critical.
        pass


def clear_namespace(namespace: str) -> int:
    """Test helper — wipe all entries in a namespace. Returns count."""
    d = _CACHE_DIR / namespace
    if not d.exists():
        return 0
    n = 0
    for p in d.glob("*.json"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n
