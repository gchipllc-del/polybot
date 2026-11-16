"""command_center - the dashboard must tell the truth the rest of the system tells.

Pinned: crypto rules are judged ONLY on fill-checked trades (a big pre-fix record must not
read as an edge); every rule shares one family correction; panels degrade on missing data
instead of 500ing; labels are inserted as text, never HTML.
"""
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import lib.command_center as cc  # noqa: E402


def _t(rule, event, pnl, era="v4 gated", venue="crypto", ts=1.0, ask=0.85):
    return {"rule": rule, "venue": venue, "event": event, "pnl": pnl, "won": pnl > 0,
            "ask": ask, "ts": ts, "era": era}


def _edge_trades(rule, n, venue="crypto", era="v4 gated"):
    # ~9c edge on 85c favorites: 94% wins
    return [_t(rule, f"{rule}-E{i}", 0.14 if (i % 100) < 94 else -0.86, era, venue, ts=i)
            for i in range(n)]


def test_crypto_verdict_uses_fill_checked_trades_only():
    """5,000 glowing PRE-FIX trades must not make an edge: the fill gate was a no-op."""
    pre = _edge_trades("H5", 5000, era="pre-fix")
    rows = cc.score_rules(pre)
    r = rows[0]
    assert r["decision"]["n"] == 0 and r["verdict"].startswith("collecting")
    assert r["all"]["n"] == 5000                        # the all-era record is still shown


def test_real_fill_checked_edge_is_recognised():
    rows = cc.score_rules(_edge_trades("H5", 150))
    assert rows[0]["status"] == "good" and rows[0]["verdict"].startswith("EDGE")


def test_one_family_correction_across_all_rules():
    """The same 40-event marginal edge that passes alone must not pass when it is one of
    many rules tested at once."""
    marginal = [_t("M", f"M-E{i}", 0.14 if (i % 100) < 94 else -0.86) for i in range(40)]
    alone = cc.score_rules(marginal)[0]
    crowd = marginal + [t for k in range(30) for t in
                        [_t(f"N{k}", f"N{k}-E{i}", 0.02 if i % 2 else -0.03, ask=0.5)
                         for i in range(40)]]
    in_crowd = next(r for r in cc.score_rules(crowd) if r["rule"] == "M")
    assert alone["decision"]["p"] == pytest.approx(in_crowd["decision"]["p"])
    assert not in_crowd["verdict"].startswith("EDGE")


def test_negative_rules_sort_last_and_edges_first():
    trades = _edge_trades("GOOD", 150) + [_t("BAD", f"B-E{i}", -0.2, ask=0.5) for i in range(60)]
    rows = cc.score_rules(trades, {"EMPTY_SLEEVE_RULE"})
    assert rows[0]["rule"] == "GOOD" and rows[-1]["rule"] == "BAD"
    assert rows[-1]["status"] == "critical"
    empty = next(r for r in rows if r["rule"] == "EMPTY_SLEEVE_RULE")
    assert empty["decision"]["n"] == 0 and empty["status"] == "neutral"


def test_equity_series_per_venue_cumulative():
    t0, day = 1_790_000_000, 86400
    trades = [_t("A", "e1", 1.0, ts=t0), _t("A", "e2", -0.5, ts=t0 + day),
              _t("S", "e3", 2.0, venue="sleeves", ts=t0 + day),
              _t("A", "e4", 9.0, ts=0.0)]          # 0.0 = no timestamp: cannot be dated
    eq = cc.equity_series(trades)
    assert [p[1] for p in eq["crypto"]] == [1.0, 0.5]
    assert [p[1] for p in eq["sleeves"]] == [2.0]


def test_health_panel_states(tmp_path, monkeypatch):
    log = tmp_path / "hc.log"
    monkeypatch.setattr(cc, "HEALTH_LOG", log)
    assert cc.health_panel()["state"] == "unknown"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.write_text(f"{now} UNHEALTHY fails=1 warns=0\n    [FAIL] data: stage0 log STALE\n")
    h = cc.health_panel()
    assert h["state"] == "unhealthy" and "STALE" in h["lines"][0]
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    log.write_text(f"{old} healthy fails=0 warns=0\n")
    assert cc.health_panel()["state"] == "stale"


def test_payload_on_an_empty_machine_never_crashes(tmp_path, monkeypatch):
    import sleeve_paper as sp
    from lib import news_regime as nr
    monkeypatch.setattr(cc, "CRYPTO_LEDGER", tmp_path / "none.jsonl")
    monkeypatch.setattr(cc, "HEALTH_LOG", tmp_path / "none.log")
    monkeypatch.setattr(cc, "SCREEN", tmp_path / "none.json")
    monkeypatch.setattr(sp, "RULES", tmp_path / "rules.jsonl")
    monkeypatch.setattr(sp, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(nr, "REGIME_LOG", tmp_path / "regime.jsonl")
    cc._CACHE.update(t=0.0, payload=None)
    p = cc.build_payload()
    assert p["kpi"]["rules"] == 0 and p["kpi"]["edge"] == 0 and p["live_money"] == "OFF"
    assert p["nominations"] == [] and p["health"]["state"] == "unknown"


def test_routes_serve_page_and_json(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "CRYPTO_LEDGER", tmp_path / "none.jsonl")
    cc._CACHE.update(t=0.0, payload=None)
    client = cc.make_app().test_client()
    page = client.get("/")
    assert page.status_code == 200 and b"Polybot command center" in page.data
    api = client.get("/api/command")
    assert api.status_code == 200 and "kpi" in api.get_json()


def test_labels_are_inserted_as_text_never_html():
    """Rule names, headlines and watchdog lines come from files - untrusted. The page
    must never build DOM from strings."""
    assert "innerHTML" not in cc._PAGE and "outerHTML" not in cc._PAGE
    assert "insertAdjacentHTML" not in cc._PAGE
