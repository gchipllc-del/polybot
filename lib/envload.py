"""envload — load the repo's .env into os.environ for scripts that need credentials.

Extracted because the Stage-0 collector silently ran WITHOUT it: order-book depth capture
calls kalshi_auth.can_sign(), which reads os.environ, so with no .env loaded the collector
recorded every observation with no depth at all ("fills n/a" in the shadow book) even
though the machine had working Kalshi credentials. Any script that touches auth must call
load_env() first.

Never overwrites variables already set in the real environment.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> bool:
    """Parse KEY=VALUE lines from .env into os.environ. Returns True if a file was read.
    Also applies PYTHONPATH entries to sys.path so vendored siblings resolve."""
    p = path or (ROOT / ".env")
    if not p.exists():
        return False
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not k:
            continue
        os.environ.setdefault(k, v)
        if k == "PYTHONPATH":
            for part in v.split(os.pathsep):
                if part and part not in sys.path:
                    sys.path.insert(0, part)
    return True
