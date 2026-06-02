"""Tests for lib/log_rotation.rotate_if_needed — bounded retention for the
append-only signal JSONL logs (audit finding 2026-06-01)."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.log_rotation import rotate_if_needed


def _write_lines(p: Path, n: int):
    with open(p, "w") as f:
        for i in range(n):
            f.write(f'{{"i": {i}}}\n')


def test_no_rotation_under_cap(tmp_path):
    p = tmp_path / "sig.jsonl"
    _write_lines(p, 100)
    assert rotate_if_needed(p, max_lines=1000, slack=100) is False
    assert sum(1 for _ in open(p)) == 100  # untouched


def test_no_rotation_within_slack(tmp_path):
    p = tmp_path / "sig.jsonl"
    _write_lines(p, 1050)
    # 1050 <= max(1000) + slack(100) -> no trim yet (amortization)
    assert rotate_if_needed(p, max_lines=1000, slack=100) is False
    assert sum(1 for _ in open(p)) == 1050


def test_rotation_trims_to_last_max_lines(tmp_path):
    p = tmp_path / "sig.jsonl"
    _write_lines(p, 1200)  # over 1000+100
    assert rotate_if_needed(p, max_lines=1000, slack=100) is True
    lines = [l for l in open(p)]
    assert len(lines) == 1000
    # kept the MOST RECENT rows: first kept line is i=200, last is i=1199
    import json
    assert json.loads(lines[0])["i"] == 200
    assert json.loads(lines[-1])["i"] == 1199


def test_rotation_missing_file_is_noop(tmp_path):
    p = tmp_path / "nope.jsonl"
    assert rotate_if_needed(p, max_lines=10, slack=1) is False


def test_no_tmp_left_behind(tmp_path):
    p = tmp_path / "sig.jsonl"
    _write_lines(p, 2000)
    rotate_if_needed(p, max_lines=500, slack=100)
    leftovers = list(tmp_path.glob("*.rot.tmp"))
    assert leftovers == []


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
