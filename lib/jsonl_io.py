"""jsonl_io - the three things every append-only JSONL file here needs, done once.

  append_lines(path, lines)    ONE write per call, and never onto a torn line: if a crash
                               left the file without its final newline, a newline goes
                               first. Otherwise the next row is glued to the torn one and
                               BOTH are lost to every reader.
  tail_lines(path, max_bytes)  the last N bytes as lines, in one seek + read.
  Follower(path)               only what was appended since the last look, whole lines
                               only. Re-reading a 100 MB log every cycle to learn what
                               changed is where the bots' CPU and memory went.

Lines are written LF-terminated; readers accept LF and CRLF (older rows were written in
text mode on Windows). Nothing here parses JSON - callers decide what a row means.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Iterator

_CHUNK = 1 << 20


def _needs_newline(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() == 0:
                return False
            f.seek(-1, os.SEEK_END)
            return f.read(1) != b"\n"
    except OSError:
        return False


def append_lines(path: Path, lines: Iterable[str]) -> int:
    """Append `lines` in one write (each gets a newline if it lacks one). A file left
    without its final newline by a torn write gets one first, so only the torn row is
    lost - not the next good one as well. Returns the number of lines written."""
    out = [ln if ln.endswith("\n") else ln + "\n" for ln in lines]
    if not out:
        return 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ("\n" if _needs_newline(path) else "") + "".join(out)
    with open(path, "ab") as f:
        f.write(data.encode("utf-8"))
        f.flush()
    return len(out)


def tail_lines(path: Path, max_bytes: int) -> list[str]:
    """The last `max_bytes` of a file as lines, in ONE seek + read. The first line is
    dropped when the read started mid-file (it is probably partial). Undecodable bytes
    are replaced, never raised; a missing or unreadable file is []."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if start > 0 else lines


class Follower:
    """Incremental reader of an append-only text file.

        restarted, lines = follower.poll()
        if restarted:
            state.clear()            # these lines start from the top of the file
        for line in lines:
            ...

    * Only COMPLETE lines are returned; a torn last line waits for its newline.
    * `restarted` is True on the first poll and whenever the file shrank, was replaced
      by another file, or was rewritten in place (its mtime moved without it growing):
      the lines then start from the top, and state built from earlier lines must go.
    * Undecodable bytes are replaced, never raised: one bad byte must not stop a bot.
    * lines is a generator that streams the file in chunks; the position advances only
      past lines actually consumed, so stopping early loses nothing.

    It cannot see an old row edited in place when a later append hides the mtime change.
    Callers whose numbers must survive a hand repair call rewind() now and then (or once
    a day) to force a full pass."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.offset = 0
        self._ident: tuple | None = None       # (st_dev, st_ino) of the file we follow
        self._size = -1
        self._mtime = -1
        self._started = False

    def rewind(self) -> None:
        """Force the next poll() to start from the top (restarted=True)."""
        self._started = False

    def _stat(self):
        try:
            return os.stat(self.path)
        except OSError:
            return None

    def poll(self) -> tuple[bool, Iterator[str]]:
        st = self._stat()
        if st is None:                         # no file (yet, or any more)
            had = self._started and (self.offset > 0 or self._ident is not None)
            self.offset, self._ident, self._size, self._mtime = 0, None, -1, -1
            self._started = True
            return had, iter(())
        ident = (st.st_dev, st.st_ino)
        restarted = (not self._started
                     or self._ident is None
                     or ident != self._ident                       # replaced
                     or st.st_size < self.offset                   # shrank
                     or (st.st_size == self._size and st.st_mtime_ns != self._mtime
                         and st.st_size == self.offset))           # rewritten in place
        if restarted:
            self.offset = 0
        self._started, self._ident = True, ident
        self._size, self._mtime = st.st_size, st.st_mtime_ns
        return restarted, self._lines(st.st_size)

    def _lines(self, upto: int) -> Iterator[str]:
        if upto <= self.offset:
            return
        try:
            f = open(self.path, "rb")
        except OSError:
            return
        with f:
            f.seek(self.offset)
            pending = b""
            remaining = upto - self.offset
            while remaining > 0:
                chunk = f.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                pending += chunk
                while True:
                    nl = pending.find(b"\n")
                    if nl < 0:
                        break
                    raw, pending = pending[:nl], pending[nl + 1:]
                    self.offset += nl + 1
                    yield raw.rstrip(b"\r").decode("utf-8", "replace")
            # whatever is left in `pending` is a torn line: not consumed, read again later

    def read_new(self) -> tuple[bool, list[str]]:
        """poll(), with the lines collected into a list."""
        restarted, lines = self.poll()
        return restarted, list(lines)
