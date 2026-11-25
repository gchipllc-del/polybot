"""single_instance - one live process per job; a lock problem never stops a bot."""
import errno
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import single_instance as si  # noqa: E402

HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from lib import single_instance as si
from pathlib import Path
try:
    si.hold("job", Path(sys.argv[2]))
except si.AlreadyRunning as e:
    print("STOOD DOWN:", e, flush=True)
    raise SystemExit(3)
print("HOLDING", flush=True)
time.sleep(60)
"""


def _spawn(tmp_path):
    return subprocess.Popen([sys.executable, "-c", HOLDER, str(ROOT), str(tmp_path)],
                            stdout=subprocess.PIPE, text=True)


def test_a_second_process_stands_down_and_the_lock_dies_with_its_holder(tmp_path):
    a = _spawn(tmp_path)
    try:
        assert a.stdout.readline().strip() == "HOLDING"
        b = _spawn(tmp_path)
        out, _ = b.communicate(timeout=20)
        assert b.returncode == 3 and "already running" in out and f"pid {a.pid}" in out
        with pytest.raises(si.AlreadyRunning):
            si.hold("job", tmp_path)                        # this process, too
    finally:
        a.kill()
        a.wait(timeout=10)
    for _ in range(50):                                     # killed, never cleaned up:
        try:                                                # the OS released the lock
            assert si.hold("job", tmp_path) is True
            break
        except si.AlreadyRunning:
            time.sleep(0.1)
    else:
        raise AssertionError("lock outlived its holder")
    assert si.hold("job", tmp_path) is True                 # idempotent in the holder
    si.release("job")


def test_a_broken_lock_fails_open(tmp_path, capsys):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    assert si.hold("job2", blocker / "locks") is False      # cannot create: run unlocked
    assert "running without it" in capsys.readouterr().err

    def boom(fd, **kw):
        raise OSError(errno.ENOLCK, "no locks available")
    orig = si._lock_fd
    si._lock_fd = boom
    try:
        assert si.hold("job3", tmp_path) is False           # lock call broke: fail open
    finally:
        si._lock_fd = orig
    assert "job3" not in si._HELD


class _FakeMsvcrt:
    LK_NBLCK, LK_UNLCK = 2, 0

    def __init__(self, err=None):
        self.err, self.positions = err, []

    def locking(self, fd, mode, nbytes):
        self.positions.append(os.lseek(fd, 0, os.SEEK_CUR))
        if self.err is not None:
            raise OSError(self.err, "locking failed")


def test_windows_branch_locks_byte_zero_and_tells_held_from_broken(tmp_path):
    """msvcrt locks from the CURRENT position: two processes at different offsets would
    lock different bytes and both carry on. Always byte 0."""
    fd = os.open(str(tmp_path / "w.lock"), os.O_RDWR | os.O_CREAT)
    try:
        os.write(fd, b"12345")                              # position is now 5
        ok = _FakeMsvcrt()
        assert si._lock_fd(fd, windows=True, msvcrt_mod=ok) is True and ok.positions == [0]
        held = _FakeMsvcrt(errno.EACCES)
        assert si._lock_fd(fd, windows=True, msvcrt_mod=held) is False
        with pytest.raises(OSError):
            si._lock_fd(fd, windows=True, msvcrt_mod=_FakeMsvcrt(errno.ENOLCK))
    finally:
        os.close(fd)


def test_lock_handle_is_not_inherited_by_children(tmp_path):
    """A child that inherited the handle would keep the lock alive after its parent died
    (the dashboards outlive-their-supervisor bug, in lock form)."""
    assert si.hold("parent_job", tmp_path) is True
    try:
        assert os.get_inheritable(si._HELD["parent_job"]) is False
    finally:
        si.release("parent_job")
