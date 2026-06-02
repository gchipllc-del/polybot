"""Tests for the /api/paper_vs_live reality-gap panel + the live_only cut of
_strategy_pnl_stats. The panel exists so a big PAPER P&L isn't misread as a live
forecast: paper books every at-bat at hypothetical sizing assuming fills; live
takes only the gate/budget-eligible subset. Capture% = live ÷ paper at-bats.
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_live_only_cut_separates_paper_and_live(tmp_path):
    # 3 paper + 2 live settled rows -> paper cut counts 3, live cut counts 2.
    import lib.dashboard_web as dw
    p = tmp_path / "t.jsonl"
    rows = [
        {"status": "won",  "paper_pnl": 5.0, "notional": 10, "opened_at": "2026-06-01T00:00:00+00:00"},
        {"status": "lost", "paper_pnl": -10.0, "notional": 10, "opened_at": "2026-06-01T01:00:00+00:00"},
        {"status": "won",  "paper_pnl": 3.0, "notional": 10, "opened_at": "2026-06-01T02:00:00+00:00"},
        {"status": "won",  "paper_pnl": 2.0, "notional": 5, "is_live": True, "opened_at": "2026-06-01T03:00:00+00:00"},
        {"status": "lost", "paper_pnl": -5.0, "notional": 5, "is_live": True, "opened_at": "2026-06-01T04:00:00+00:00"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    paper = dw._strategy_pnl_stats(p, "t")                  # default: paper-only
    live = dw._strategy_pnl_stats(p, "t", live_only=True)   # live-only
    combined = dw._strategy_pnl_stats(p, "t", include_live=True)
    assert paper["closed"] == 3 and live["closed"] == 2 and combined["closed"] == 5
    assert paper["won"] == 2 and live["won"] == 1
    # paper P&L excludes the live rows and vice-versa
    assert round(paper["net_pnl"], 2) == -2.0     # 5 -10 +3
    assert round(live["net_pnl"], 2) == -3.0      # 2 -5


def test_endpoint_shape_and_capture(tmp_path, monkeypatch):
    # Point the endpoint's sleeves at a controlled file so capture% is exact:
    # 4 paper closed, 1 live closed -> capture 25.0%.
    import lib.dashboard_web as dw
    p = tmp_path / "sleeve.jsonl"
    rows = (
        [{"status": "won", "paper_pnl": 1.0, "notional": 10,
          "opened_at": f"2026-06-01T0{i}:00:00+00:00"} for i in range(4)]
        + [{"status": "lost", "paper_pnl": -5.0, "notional": 5, "is_live": True,
            "opened_at": "2026-06-01T05:00:00+00:00"}]
    )
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    real = dw._strategy_pnl_stats
    monkeypatch.setattr(dw, "_strategy_pnl_stats",
                        lambda path, label, **kw: real(p, label, **kw))

    c = dw.app.test_client()
    r = c.get("/api/paper_vs_live")
    assert r.status_code == 200
    d = r.get_json()
    assert "sleeves" in d and "note" in d and len(d["sleeves"]) >= 1
    s = d["sleeves"][0]
    assert set(s) >= {"label", "paper", "live", "capture_pct"}
    assert s["paper"]["closed"] == 4 and s["live"]["closed"] == 1
    assert s["capture_pct"] == 25.0


def test_endpoint_runs_against_live_logs():
    # Smoke test against the real logs: shape holds, capture in [0,100] or None.
    import lib.dashboard_web as dw
    d = dw.app.test_client().get("/api/paper_vs_live").get_json()
    for s in d["sleeves"]:
        cap = s["capture_pct"]
        assert cap is None or (0.0 <= cap <= 100.0)
        assert s["live"]["closed"] <= s["paper"]["closed"] + s["live"]["closed"]


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
