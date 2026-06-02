"""Regression test: the 15-min isotonic calibrator must fit ONLY on terminally
resolved trades (won/lost), excluding path-dependent early exits
(won_early = take-profit, cut_loss = stop-loss). Audit finding 2026-06-01.
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lib.kalshi_calibration as kc


def test_load_resolved_excludes_early_exits(tmp_path, monkeypatch):
    log = tmp_path / "k15.jsonl"
    rows = [
        {"status": "won", "confidence": 0.8},
        {"status": "lost", "confidence": 0.4},
        {"status": "won_early", "confidence": 0.9},   # TP — must be excluded
        {"status": "cut_loss", "confidence": 0.3},    # SL — must be excluded
        {"status": "open", "confidence": 0.5},        # not resolved
        {"status": "won", "confidence": 0.7},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(kc, "_PAPER_PATH", log)

    loaded = kc._load_resolved_trades()
    statuses = sorted(r["status"] for r in loaded)
    assert statuses == ["lost", "won", "won"]  # the 3 terminal ones only
    assert all(r["status"] in ("won", "lost") for r in loaded)
    assert not any(r["status"] in ("won_early", "cut_loss") for r in loaded)


def test_win_count_matches_terminal_only(tmp_path, monkeypatch):
    # 25 terminal trades so we clear MIN_SAMPLES and actually fit; interleave
    # early-exits that, if counted, would inflate the win rate.
    log = tmp_path / "k15.jsonl"
    rows = []
    for i in range(25):
        rows.append({"status": "won" if i % 2 == 0 else "lost",
                     "confidence": 0.5 + (i % 5) * 0.08})
    # add early-exits that would skew if (wrongly) included
    for _ in range(10):
        rows.append({"status": "won_early", "confidence": 0.95})
        rows.append({"status": "cut_loss", "confidence": 0.95})
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(kc, "_PAPER_PATH", log)

    cal = kc.fit_calibrator(force=True)
    # n_samples counts only the 25 terminal trades, not the 20 early-exits.
    assert cal["n_samples"] == 25
    assert cal["is_identity"] is False


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
