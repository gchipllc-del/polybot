"""jsonl_io - append without gluing rows, tail in one read, follow only what is new."""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib import jsonl_io as jio  # noqa: E402


def _rows(path):
    out = []
    for line in path.read_bytes().decode("utf-8", "replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def test_append_is_one_lf_terminated_write_per_call(tmp_path):
    p = tmp_path / "sub" / "l.jsonl"                       # parent is created
    assert jio.append_lines(p, ['{"a":1}', '{"a":2}\n']) == 2
    assert p.read_bytes() == b'{"a":1}\n{"a":2}\n'
    assert jio.append_lines(p, []) == 0 and p.read_bytes().count(b"\n") == 2


def test_a_torn_last_line_costs_one_row_not_two(tmp_path):
    """A crash mid-write leaves the file without its final newline. The next row used to
    be glued onto the torn one, and BOTH were unreadable."""
    p = tmp_path / "l.jsonl"
    p.write_bytes(b'{"a":1}\n{"a":2,"b"')                   # torn
    jio.append_lines(p, ['{"a":3}'])
    assert [r["a"] for r in _rows(p)] == [1, 3]             # only the torn row is lost
    assert p.read_bytes().endswith(b'\n{"a":3}\n')


def test_tail_reads_the_end_and_drops_the_partial_first_line(tmp_path):
    p = tmp_path / "l.jsonl"
    jio.append_lines(p, [json.dumps({"i": i}) for i in range(1000)])
    lines = jio.tail_lines(p, 200)
    assert json.loads(lines[-1]) == {"i": 999} and all(json.loads(x) for x in lines)
    assert len(jio.tail_lines(p, 10 ** 9)) == 1000          # whole file: nothing dropped
    assert jio.tail_lines(tmp_path / "missing.jsonl", 100) == []
    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(b'{"a":1}\n\xff\xfe junk\n{"a":2}\n')
    assert len(jio.tail_lines(bad, 10 ** 6)) == 3           # replaced, never raised


def test_follower_returns_only_new_complete_lines(tmp_path):
    p = tmp_path / "l.jsonl"
    f = jio.Follower(p)
    assert f.read_new() == (False, [])                      # no file yet: nothing, no state
    jio.append_lines(p, ["a", "b"])
    assert f.read_new() == (True, ["a", "b"])               # first real pass starts at the top
    assert f.read_new() == (False, [])
    with open(p, "ab") as fh:
        fh.write(b"c\ntorn")                                # a writer caught mid-row
    assert f.read_new() == (False, ["c"])
    with open(p, "ab") as fh:
        fh.write(b"-done\r\nd\n")                           # completed; CRLF rows are fine
    assert f.read_new() == (False, ["torn-done", "d"])


def test_follower_restarts_when_the_file_shrinks_is_replaced_or_rewritten(tmp_path):
    p = tmp_path / "l.jsonl"
    jio.append_lines(p, ["a", "b", "c"])
    f = jio.Follower(p)
    assert f.read_new() == (True, ["a", "b", "c"])
    p.write_bytes(b"x\n")                                   # truncated / rotated
    assert f.read_new() == (True, ["x"])
    other = tmp_path / "new.jsonl"
    other.write_bytes(b"y\nz\n")
    os.replace(other, p)                                    # replaced by another file
    assert f.read_new() == (True, ["y", "z"])
    st = os.stat(p)
    p.write_bytes(b"Y\nZ\n")                                # same size, edited in place
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert f.read_new() == (True, ["Y", "Z"])
    p.unlink()
    assert f.read_new() == (True, [])                       # gone: drop the state
    jio.append_lines(p, ["again"])
    assert f.read_new() == (True, ["again"])
    f.rewind()
    assert f.read_new() == (True, ["again"])                # forced full pass


def test_follower_stopping_early_loses_nothing_and_bad_bytes_never_raise(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_bytes(b"one\ntwo\n\xff\xfe\nfour\n")
    f = jio.Follower(p)
    restarted, lines = f.poll()
    assert restarted and next(lines) == "one"               # consumer stops after one line
    restarted, lines = f.poll()
    got = list(lines)
    assert not restarted and got[0] == "two" and got[-1] == "four" and len(got) == 3


def test_follower_streams_lines_longer_than_a_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(jio, "_CHUNK", 7)
    p = tmp_path / "l.jsonl"
    jio.append_lines(p, ["x" * 50, "y" * 3, "z" * 20])
    assert jio.Follower(p).read_new() == (True, ["x" * 50, "y" * 3, "z" * 20])
