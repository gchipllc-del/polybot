"""single_instance - at most one live process per named job.

Two copies of a paper trader writing one ledger double-book entries and settlements, and
the ledger is append-only: the damage is permanent (in the audit one duplicated close row
moved a rule's p-value from 0.11 to 0.02). `hold(name)` takes an OS-level lock on
<lock dir>/<name>.lock for the life of the process:

  * the OS releases it when the process dies, however it dies - it can never go stale;
  * a second process finds it held and stands down (AlreadyRunning);
  * if locking itself fails for any other reason (permissions, antivirus, an odd
    filesystem) we FAIL OPEN and run unlocked: a lock problem must never stop a bot,
    only a live twin may.

Call it from main() for the long-running commands only (run / collect / cycle), never
inside a cycle function: tests and selftests call those directly and must not be blocked
by the live scheduled task.

    from lib.single_instance import AlreadyRunning, hold
    try:
        hold("sleeve_paper")
    except AlreadyRunning as e:
        print(f"not starting: {e}")
        return 1
"""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_HELD: dict[str, int] = {}          # name -> open descriptor, kept until the process exits


class AlreadyRunning(RuntimeError):
    """Another live process holds this job's lock."""


def lock_dir() -> Path:
    return Path(os.environ.get("POLYBOT_LOCK_DIR") or (ROOT / "data" / "locks"))


def _lock_fd(fd: int, windows: bool | None = None, msvcrt_mod=None) -> bool:
    """True = we now hold the lock; False = another process holds it. Any other failure
    raises OSError and the caller fails open. (windows / msvcrt_mod are test seams.)"""
    if (os.name == "nt") if windows is None else windows:
        if msvcrt_mod is None:
            import msvcrt as msvcrt_mod            # noqa: PLC0415 - Windows only
        # msvcrt locks bytes starting at the CURRENT file position: every process must
        # lock the same byte, so always byte 0 - never wherever a write left the position
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt_mod.locking(fd, msvcrt_mod.LK_NBLCK, 1)
            return True
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
    import fcntl                                   # noqa: PLC0415 - not on Windows
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return False
        raise


def _pid_file(directory: Path, name: str) -> Path:
    # a SEPARATE file: on Windows the locked byte cannot even be read by another process
    return directory / f"{name}.pid"


def _holder_note(directory: Path, name: str) -> str:
    try:
        return f" (pid {int(_pid_file(directory, name).read_text(encoding='utf-8').strip())})"
    except (OSError, ValueError):
        return ""


def _warn(name: str, e: BaseException) -> None:
    print(f"[warn] could not take the single-instance lock for {name} "
          f"({type(e).__name__}: {str(e)[:80]}) - running without it", file=sys.stderr)


def hold(name: str, directory: Path | None = None) -> bool:
    """Take the lock for `name` and keep it until this process exits.

    True   we hold it.
    False  locking is unavailable here (failed open): running, but unprotected.
    Raises AlreadyRunning when another live process holds it."""
    if name in _HELD:
        return True
    d = Path(directory) if directory is not None else lock_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(d / f"{name}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError as e:
        _warn(name, e)
        return False
    try:
        ours = _lock_fd(fd)
    except Exception as e:  # noqa: BLE001 - anything but "held by another": fail open
        os.close(fd)
        _warn(name, e)
        return False
    if not ours:
        os.close(fd)
        raise AlreadyRunning(f"{name} is already running{_holder_note(d, name)}")
    _HELD[name] = fd                # never closed on purpose: the lock lives with the process
    try:
        _pid_file(d, name).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass                        # the note is a courtesy; the lock is what matters
    return True


def release(name: str) -> None:
    """Give the lock back early (tests; a process normally just exits)."""
    fd = _HELD.pop(name, None)
    if fd is None:
        return
    try:
        if os.name == "nt":
            import msvcrt                          # noqa: PLC0415
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl                           # noqa: PLC0415
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass
