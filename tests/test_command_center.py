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


def _hold(rule, event, won, era="v4 gated", venue="crypto", ask=0.85):
    """A 1-lot held to settlement: a win pays 1-ask-fee, a loss costs ask+fee (1c at 85c)."""
    pnl = (1 - ask - 0.01) if won else (-ask - 0.01)
    return {"rule": rule, "venue": venue, "event": event, "pnl": pnl, "won": won,
            "ask": ask, "ts": 1.0, "era": era}


def test_one_family_correction_across_all_rules():
    """The family must DECIDE the verdict: a 100-event edge that passes alone (85c, 8
    losses: exact p = 0.049 <= q = 0.10) must not pass as one of 31 rules tested at once
    (BH rank-1 bar 0.10/31 = 0.0032). Same record, same p - only the family differs. (A
    40-event fixture could never be EDGE, so it pinned the event floor, not the family.)"""
    marginal = [_hold("M", f"M-E{i}", i >= 8) for i in range(100)]
    alone = cc.score_rules(marginal)[0]
    assert alone["verdict"].startswith("EDGE") and alone["status"] == "good"
    crowd = marginal + [_hold(f"N{k}", f"N{k}-E{i}", i >= 14)
                        for k in range(30) for i in range(100)]
    in_crowd = next(r for r in cc.score_rules(crowd) if r["rule"] == "M")
    assert alone["decision"]["p"] == pytest.approx(in_crowd["decision"]["p"])
    assert in_crowd["status"] != "good" and in_crowd["verdict"] == "no edge yet"


def test_the_family_takes_the_fill_checked_record_not_the_all_era_one():
    """5,000 glowing PRE-FIX trades and 150 ordinary fill-checked ones: the all-era p is
    tiny, the fill-checked one is not. Only the fill-checked record may enter the family
    - or the live-money verdict rests on fills that never happened."""
    pre = [_hold("H", f"H-P{i}", i % 100 < 94, era="pre-fix") for i in range(5000)]
    gated = [_hold("H", f"H-G{i}", i >= 20) for i in range(150)]
    r = cc.score_rules(pre + gated)[0]
    assert r["all"]["p"] < 1e-6 and r["decision"]["n"] == 150 and r["decision"]["mean"] > 0
    assert r["status"] != "good" and not r["verdict"].startswith("EDGE")


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


T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _isolate(tmp_path, monkeypatch):
    import sleeve_paper as sp
    from lib import news_regime as nr
    for mod, name, file in ((cc, "CRYPTO_LEDGER", "crypto.jsonl"), (cc, "HEALTH_LOG", "hc.log"),
                            (cc, "SCREEN", "screen.json"), (sp, "RULES", "rules.jsonl"),
                            (sp, "LEDGER", "ledger.jsonl"), (sp, "HEARTBEAT", "hb.json"),
                            (nr, "REGIME_LOG", "regime.jsonl")):
        monkeypatch.setattr(mod, name, tmp_path / file)
    return sp


def _sleeve_rule(sp, name, losses, n=100, ask=0.90, lots=10):
    """A registered 10-lot favorite rule with n settled events after named_at."""
    sp.register([{"name": name, "series": f"KX{name}", "band": "any", "side": "favorite",
                  "lo": 0.85, "hi": 0.95, "contracts": lots}], sp.RULES, T0)
    rows = []
    for i in range(n):
        o = {"t": "open", "ts": (T0 + timedelta(days=1, hours=i)).isoformat(),
             "ticker": f"KX{name}-E{i:03d}-A", "rule": name, "event": f"KX{name}-E{i:03d}",
             "series": f"KX{name}", "side": "yes", "price": ask, "contracts": lots,
             "fee": sp.kalshi_fee(ask, lots), "depth": 40}
        rows += [o, sp.settle(o, "yes" if i >= losses else "no")]
    sp._append(sp.LEDGER, rows)


def _crypto_ledger(path, rules=3, n=40, losses=7):
    """Forward crypto rules with n fill-checked (v4 gated) windows each - ordinary records."""
    rows = []
    for j in range(rules):
        for i in range(n):
            tk, won = f"KXBTC15M-26SEP{j:02d}{i:04d}-B{j}", i >= losses
            rows.append({"t": "open", "ts": T0.isoformat(), "ticker": tk, "rule": f"C{j}",
                         "price": 0.85, "contracts": 1, "fee": 0.01, "v": 4, "depth": 20})
            rows.append({"t": "close", "ts": T0.isoformat(), "ticker": tk, "rule": f"C{j}",
                         "price": 0.85, "won": won, "pnl": 0.14 if won else -0.86})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _both_verdicts(sp, name, capsys):
    """(the `sleeve_paper.py report` line, the command center's verdict) for one rule."""
    sp.cmd_report()
    line = next(l.strip() for l in capsys.readouterr().out.splitlines()
                if l.strip().startswith(name + " "))
    cc._CACHE.update(t=0.0, payload=None)
    return line, next(r for r in cc.build_payload()["rules"] if r["rule"] == name)["verdict"]


def test_report_and_command_center_correct_over_one_family(tmp_path, monkeypatch, capsys):
    """The report once corrected over the sleeve rules alone and the command center over
    crypto + sleeves, so the same trades read EDGE on one and 'no edge yet' on the other.
    S_FAV90 (100 events at 90c, 4 losses: exact p = 0.039) is EDGE only on its own."""
    sp = _isolate(tmp_path, monkeypatch)
    _sleeve_rule(sp, "S_FAV90", losses=4)
    line, verdict = _both_verdicts(sp, "S_FAV90", capsys)
    assert line.endswith("EDGE - survives the forward family correction")      # m = 1
    assert verdict.startswith("EDGE")
    _crypto_ledger(cc.CRYPTO_LEDGER)                   # three more forward rules: m = 4
    line, verdict = _both_verdicts(sp, "S_FAV90", capsys)
    assert line.endswith("positive, not significant") and verdict == "no edge yet"


def test_retiring_rules_never_raises_another_rules_verdict(tmp_path, monkeypatch, capsys):
    """Retirement is decided after seeing results: dropping the retired rules' tests from
    the family shrank m and turned R0 into EDGE with no new trade. Their records stay in
    the family; they are just not shown."""
    sp = _isolate(tmp_path, monkeypatch)
    for j in range(5):                                 # R0 p = 0.039, R1-R4 ordinary
        _sleeve_rule(sp, f"R{j}", losses=4 if j == 0 else 10)
    before = _both_verdicts(sp, "R0", capsys)
    assert before[0].endswith("positive, not significant") and before[1] == "no edge yet"
    monkeypatch.setattr(sys, "argv", ["sleeve_paper.py", "retire", "R1", "R2", "R3", "R4"])
    assert sp.main() == 0
    capsys.readouterr()
    assert _both_verdicts(sp, "R0", capsys) == before
    cc._CACHE.update(t=0.0, payload=None)
    assert [r["rule"] for r in cc.build_payload()["rules"]] == ["R0"]   # retired: not shown


def test_dashboard_never_counts_a_trade_opened_before_named_at(tmp_path, monkeypatch):
    sp = _isolate(tmp_path, monkeypatch)
    sp.register([{"name": "R", "series": "KXA", "band": "any", "side": "favorite",
                  "lo": 0.80, "hi": 0.90}], sp.RULES, T0)
    o = {"t": "open", "ts": (T0 - timedelta(minutes=1)).isoformat(), "ticker": "KXA-E1-A",
         "rule": "R", "event": "KXA-E1", "series": "KXA", "side": "yes", "price": 0.85,
         "fee": 0.01, "depth": 5}
    sp._append(sp.LEDGER, [o, sp.settle(o, "yes")])
    cc._CACHE.update(t=0.0, payload=None)
    p = cc.build_payload()
    assert p["rules"][0]["decision"]["n"] == 0 and p["kpi"]["events"] == 0


def test_a_rule_spanning_many_series_scores_and_counts(tmp_path, monkeypatch):
    """One hypothesis over seven cities is one rule: the payload must count its series
    and label it - a list-valued series once crashed the series count."""
    import sleeve_paper as sp
    from lib import news_regime as nr
    monkeypatch.setattr(cc, "CRYPTO_LEDGER", tmp_path / "none.jsonl")
    monkeypatch.setattr(cc, "HEALTH_LOG", tmp_path / "none.log")
    monkeypatch.setattr(cc, "SCREEN", tmp_path / "none.json")
    monkeypatch.setattr(sp, "RULES", tmp_path / "rules.jsonl")
    monkeypatch.setattr(sp, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(nr, "REGIME_LOG", tmp_path / "regime.jsonl")
    cities = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA"]
    sp.register([{"name": "F9799", "series": cities, "band": "any", "side": "favorite",
                  "lo": 0.97, "hi": 0.995, "contracts": 10}], sp.RULES,
                datetime(2026, 11, 1, tzinfo=timezone.utc))
    rows = []
    for i, c in enumerate(cities):
        tk, ev = f"{c}-26NOV0{i + 2}-B50", f"{c}-26NOV0{i + 2}"
        o = {"t": "open", "ts": f"2026-11-0{i + 2}T12:00:00+00:00", "ticker": tk,
             "rule": "F9799", "event": ev, "series": c, "side": "no", "price": 0.98,
             "contracts": 10, "fee": 0.02, "depth": 40}
        rows += [o, sp.settle(o, "no")]
    sp._append(sp.LEDGER, rows)
    cc._CACHE.update(t=0.0, payload=None)
    p = cc.build_payload()
    assert p["kpi"]["sleeves"] == 3 and p["kpi"]["events"] == 3
    r = next(x for x in p["rules"] if x["rule"] == "F9799")
    assert r["series"] == "KXHIGHNY +2 more" and r["decision"]["n"] == 3
    assert abs(r["decision"]["mean"] - 0.018) < 1e-9          # per contract: 2c - 0.2c fee
    assert abs(r["decision"]["p"] - 0.982 ** 3) < 1e-9          # exact: 3/3 is luck
