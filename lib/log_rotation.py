"""Bounded-size retention for append-only signal JSONL logs.

WHY THIS EXISTS
---------------
Every signal cycle appends ALL sampled markets (with full nested `indicators`
payloads) to a per-strategy .jsonl, with NO rotation anywhere. On-disk these had
grown to 40MB / 45k lines (weather_signal) and climbing — and they are the input
to the dashboard's full-file readers, so unbounded growth directly inflates
render latency too. (Audit finding, 2026-06-01.)

`rotate_if_needed` keeps only the most recent `max_lines` rows, trimming the head
(oldest) when the file exceeds `max_lines + slack`. Trimming is atomic
(tmp + os.replace) so a concurrent reader never sees a partial file, and only
fires occasionally (when over the high-water mark) so the common append path
stays O(1). Signal logs are diagnostic tails — losing the oldest rows is fine;
settled trade logs (paper/live) are NEVER rotated by this.
"""
from __future__ import annotations

import os
from pathlib import Path

# Default retention: ~7-14 days of signal rows for the busiest strategies.
DEFAULT_MAX_LINES = 20_000
# Only rewrite once we're this many lines OVER the cap, so we amortize the
# trim cost across many cheap appends instead of rewriting every cycle.
DEFAULT_SLACK = 5_000


def _count_lines(path: Path) -> int:
    n = 0
    with open(path, "rb") as f:
        for _ in f:
            n += 1
    return n


def rotate_if_needed(path, max_lines: int = DEFAULT_MAX_LINES,
                     slack: int = DEFAULT_SLACK) -> bool:
    """Trim *path* (a JSONL) to its most recent ``max_lines`` rows if it has
    grown beyond ``max_lines + slack``. Returns True if a trim happened.

    Atomic: writes the kept tail to a temp file and os.replace()s it in, so a
    concurrent reader sees either the old or the new file, never a partial one.
    Best-effort: any OSError is swallowed (rotation must never break the
    signal-persist path it's called from).
    """
    p = Path(path) if not isinstance(path, Path) else path
    try:
        if not p.exists():
            return False
        total = _count_lines(p)
        if total <= max_lines + slack:
            return False
        # Keep only the last max_lines rows.
        with open(p, "r") as f:
            lines = f.readlines()
        keep = lines[-max_lines:]
        tmp = p.with_suffix(p.suffix + ".rot.tmp")
        with open(tmp, "w") as f:
            f.writelines(keep)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except OSError:
        # Never let retention maintenance break the caller's write path.
        try:
            if 'tmp' in dir() and Path(tmp).exists():
                os.remove(tmp)
        except OSError:
            pass
        return False
