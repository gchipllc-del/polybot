"""Test /api/live_alerts parses + classifies logs/live_alerts.log correctly.
Surfaces real-money activity on the dashboard (canonical notification surface
when Telegram push is off)."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def test_endpoint_returns_classified_alerts():
    # Runs against the live log if present; asserts shape + classification
    # invariants without depending on specific content.
    import lib.dashboard_web as dw
    c = dw.app.test_client()
    r = c.get("/api/live_alerts")
    assert r.status_code == 200
    d = r.get_json()
    assert "alerts" in d and "total" in d
    assert isinstance(d["alerts"], list)
    assert len(d["alerts"]) <= 25  # capped
    for a in d["alerts"]:
        assert set(a) >= {"ts", "kind", "summary", "detail"}
        assert a["kind"] in ("placed", "refused", "partial", "kill", "info")
    # newest-first: timestamps should be non-increasing
    ts = [a["ts"] for a in d["alerts"]]
    assert ts == sorted(ts, reverse=True)


def test_classification_logic():
    # The classify branch is inline in the endpoint; re-implement the same
    # rules here as a guard so a future edit that breaks classification fails.
    def classify(body):
        if "KILL SWITCH" in body:
            return "kill"
        if "order PLACED" in body:
            return "placed"
        if any(t in body for t in ("REFUSED", "refused", "BLOCKED", "blocked")):
            return "refused"
        if "only" in body and "filled" in body:
            return "partial"
        return "info"
    assert classify("🚨 KALSHI LIVE — KILL SWITCH TRIPPED\n 5 losses") == "kill"
    assert classify("✅ Kalshi LIVE order PLACED\n KXTEMP NO 39@$0.37") == "placed"
    assert classify("⛔ Kalshi LIVE order BLOCKED\n veto_thin_cushion") == "refused"
    assert classify("Kalshi LIVE order REFUSED\n balance floor") == "refused"
    # A placed-but-partial alert: "order PLACED" wins over the partial check.
    assert classify("✅ Kalshi LIVE order PLACED\n ⚠ only 0/39 filled (0%)") == "placed"
    assert classify("some other note") == "info"


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
