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


def _crypto_ledger(path, rules=3, n=40, losses=7, glowing_prefix=0):
    """Forward crypto rules with n fill-checked (v4 gated) windows each - ordinary records.
    glowing_prefix adds that many PRE-FIX windows per rule, all wins - the real ledger's
    shape, where the pre-fix era looked great on fills the book could not give."""
    rows = []
    for j in range(rules):
        for i in range(glowing_prefix):
            tk = f"KXBTC15M-26AUG{j:02d}{i:04d}-B{j}"
            rows.append({"t": "open", "ts": T0.isoformat(), "ticker": tk, "rule": f"C{j}",
                         "price": 0.85, "contracts": 1, "fee": 0.01})       # no v: pre-fix
            rows.append({"t": "close", "ts": T0.isoformat(), "ticker": tk, "rule": f"C{j}",
                         "price": 0.85, "won": True, "pnl": 0.14})
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
    # the crypto members are judged on their FILL-CHECKED record on both surfaces: a
    # glowing pre-fix era must not make them survive and loosen S_FAV90's threshold
    _crypto_ledger(cc.CRYPTO_LEDGER, glowing_prefix=400)
    line, verdict = _both_verdicts(sp, "S_FAV90", capsys)
    assert line.endswith("positive, not significant") and verdict == "no edge yet"
    cc._CACHE.update(t=0.0, payload=None)
    dec = {r["rule"]: r["decision"] for r in cc.build_payload()["rules"]
           if r["venue"] == "crypto"}
    assert cc.crypto_family() == [dec[k] for k in sorted(dec)]


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


def test_scores_are_reused_until_a_record_changes(monkeypatch):
    """The page rebuilds on every poll; exact tails are costly on long records. A record
    is scored once until one of its trades settles, and a sleeve rule (whose decision
    record IS its full record) is scored once, not twice."""
    import sleeve_paper as sp
    calls = []
    real = sp.forward_stats
    monkeypatch.setattr(sp, "forward_stats", lambda ts: calls.append(len(ts)) or real(ts))
    cc._STATS_MEMO.clear()
    trades = [dict(_t("S1", f"E{i}", 0.14 if i % 10 else -0.86, venue="sleeves"),
                   pnl_pc=0.14 if i % 10 else -0.86, fee_pc=0.01) for i in range(60)]
    first = cc.score_rules(trades)
    assert len(calls) == 1                             # all == decision: scored once
    assert cc.score_rules(trades) == first and len(calls) == 1      # reused
    more = trades + [dict(trades[0], event="E999")]
    cc.score_rules(more)
    assert len(calls) == 2                             # a new settlement: rescored


# ── live activity: registration labels, open bets, trader freshness, the reason ──

def _sleeve_bet(sp, rule, series, i, at, result=None, ask=0.97, lots=10):
    """One sleeve paper bet opened at `at`: still open, or settled when result is given."""
    o = {"t": "open", "ts": at.isoformat(), "ticker": f"{series}-E{i:03d}-B1", "rule": rule,
         "event": f"{series}-E{i:03d}", "series": series, "side": "yes", "price": ask,
         "contracts": lots, "fee": sp.kalshi_fee(ask, lots), "depth": 40}
    return [o] if result is None else [o, sp.settle(o, result)]


TEMPS = ["KXTEMPNYCHS", "KXTEMPCHIHS", "KXTEMPMIAHS", "KXTEMPLAXHS"]


def _register(sp, name, series, at=T0):
    sp.register([{"name": name, "series": series, "band": "any", "side": "favorite",
                  "lo": 0.90, "hi": 0.99, "contracts": 10}], sp.RULES, at)


def test_every_sleeve_rule_is_labelled_from_its_registration(tmp_path, monkeypatch):
    """A rule with nothing settled has no trade to take a label from, so HOURLY_FAV (four
    temperature series, bets open, none settled) read just 'sleeve'. Every sleeve rule is
    labelled from its registration, traded or not; crypto rules keep their venue."""
    sp = _isolate(tmp_path, monkeypatch)
    _register(sp, "HOURLY_FAV", TEMPS)
    _register(sp, "SOLO", "KXHIGHNY")             # older rules store one series as a string
    sp._append(sp.LEDGER, _sleeve_bet(sp, "HOURLY_FAV", "KXTEMPNYCHS", 0, T0 + timedelta(hours=1)))
    _crypto_ledger(cc.CRYPTO_LEDGER, rules=1, n=3)
    cc._CACHE.update(t=0.0, payload=None)
    rules = {r["rule"]: r for r in cc.build_payload()["rules"]}
    assert rules["HOURLY_FAV"]["decision"]["n"] == 0
    assert rules["HOURLY_FAV"]["series"] == "KXTEMPNYCHS +3 more"
    assert rules["SOLO"]["series"] == "KXHIGHNY"
    assert rules["C0"]["venue"] == "crypto"           # the page labels it "crypto 15-min"


def test_open_bets_are_counted_per_rule_and_in_total(tmp_path, monkeypatch):
    """A record moves only when a bet settles, so a rule with bets open and none settled
    looked idle. Open = an open row with no close for the same (ticker, rule): shown per
    rule and in total for both venues. A retired rule's open bets are no shown rule's,
    and a crypto rule whose first bets are all still open is listed (n = 0)."""
    sp = _isolate(tmp_path, monkeypatch)
    _register(sp, "F9799", ["KXHIGHNY", "KXHIGHCHI"])
    _register(sp, "HOURLY_FAV", TEMPS)
    _register(sp, "F9799_MIA", "KXHIGHMIA")
    sp._append(sp.RULES, [{"t": "retire", "name": "F9799_MIA", "ts": T0.isoformat()}])
    at = T0 + timedelta(hours=1)
    rows = (_sleeve_bet(sp, "F9799", "KXHIGHNY", 0, at, "yes")
            + _sleeve_bet(sp, "F9799", "KXHIGHCHI", 1, at, "no")
            + _sleeve_bet(sp, "F9799", "KXHIGHNY", 2, at)
            + _sleeve_bet(sp, "F9799", "KXHIGHCHI", 3, at)
            + [r for i in range(3) for r in _sleeve_bet(sp, "HOURLY_FAV", TEMPS[i], i, at)]
            + _sleeve_bet(sp, "F9799_MIA", "KXHIGHMIA", 4, at))
    sp._append(sp.LEDGER, rows)
    _crypto_ledger(cc.CRYPTO_LEDGER, rules=2, n=40)       # C0, C1: every bet settled
    with cc.CRYPTO_LEDGER.open("a", encoding="utf-8") as f:
        for rule, tk in (("C0", "KXBTC15M-26NOV170315-B1"), ("NEW", "KXBTC15M-26NOV170315-B2"),
                         ("NEW", "KXETH15M-26NOV170315-B3")):
            f.write(json.dumps({"t": "open", "ts": at.isoformat(), "ticker": tk, "rule": rule,
                                "price": 0.9, "contracts": 1, "fee": 0.01, "v": 4,
                                "depth": 20}) + "\n")
    cc._CACHE.update(t=0.0, payload=None)
    p = cc.build_payload()
    shown = {r["rule"]: r for r in p["rules"]}
    assert {k: shown[k]["open"] for k in ("F9799", "HOURLY_FAV", "C0", "C1", "NEW")} == {
        "F9799": 2, "HOURLY_FAV": 3, "C0": 1, "C1": 0, "NEW": 2}
    assert shown["F9799"]["decision"]["n"] == 2 and "F9799_MIA" not in shown
    assert shown["NEW"]["venue"] == "crypto" and shown["NEW"]["decision"]["n"] == 0
    k = p["kpi"]
    assert (k["open"], k["open_crypto"], k["open_sleeves"], k["open_retired"]) == (8, 3, 5, 1)


def test_trader_freshness_fresh_late_and_missing(tmp_path, monkeypatch):
    """Is each paper trader still working? The sleeve trader's heartbeat (a 2-min cycle;
    late past the watchdog's 10-min bar) and the crypto trader's newest ledger row (it
    keeps no heartbeat; a row lands only when a bet opens or settles, so quiet past 20).
    A trader that never ran says so instead of looking fine."""
    sp = _isolate(tmp_path, monkeypatch)

    def traders():
        cc._CACHE.update(t=0.0, payload=None)
        return {t["venue"]: t for t in cc.build_payload()["traders"]}

    t = traders()                                              # a machine with neither
    assert t["sleeves"]["status"] == t["crypto"]["status"] == "missing"
    assert "never cycled" in t["sleeves"]["note"] and "no ledger rows" in t["crypto"]["note"]
    now = datetime.now(timezone.utc)

    def at(minutes_ago, rows=1):
        """Heartbeat and newest crypto row `minutes_ago`; older rows an hour apart."""
        stamp = lambda m: (now - timedelta(minutes=m)).isoformat()  # noqa: E731
        sp.HEARTBEAT.write_text(json.dumps({"ts": stamp(minutes_ago), "rules": 2}))
        cc.CRYPTO_LEDGER.write_text("".join(json.dumps(
            {"t": "open", "ts": stamp(minutes_ago + 60 * (rows - 1 - i)),
             "ticker": f"KXBTC15M-26NOV17{i:04d}-B1", "rule": "C0", "price": 0.9,
             "contracts": 1, "fee": 0.01, "v": 4, "depth": 20}) + "\n" for i in range(rows)))
        return traders()

    t = at(2, rows=3)                                         # the newest row counts
    assert (t["sleeves"]["status"], t["crypto"]["status"]) == ("good", "good")
    assert 1.5 < t["sleeves"]["age_min"] < 3 and 1.5 < t["crypto"]["age_min"] < 3
    assert t["sleeves"]["what"] == "last cycle" and t["crypto"]["what"] == "newest ledger row"
    t = at(14)                       # a stalled sleeve trader; a gap between crypto bets
    assert (t["sleeves"]["status"], t["crypto"]["status"]) == ("warning", "good")
    assert "late" in t["sleeves"]["note"]
    t = at(45)
    assert (t["sleeves"]["status"], t["crypto"]["status"]) == ("warning", "warning")
    assert 44 < t["sleeves"]["age_min"] < 47 and "quiet" in t["crypto"]["note"]
    sp.HEARTBEAT.write_text("{not json")
    t = traders()
    assert t["sleeves"]["status"] == "missing" and "unreadable" in t["sleeves"]["note"]


def test_health_puts_the_reason_for_unhealthy_first(tmp_path, monkeypatch):
    """healthcheck.py --log writes its lines in CHECK order, so a warning from an early
    check sat above the failure that made the run unhealthy - and past the 20-line cap a
    failure was cut entirely. FAIL first, then WARN, OK last, and the tile is told which
    checks failed. The lines below are the watchdog's own format."""
    log = tmp_path / "hc.log"
    monkeypatch.setattr(cc, "HEALTH_LOG", log)
    now = datetime.now(timezone.utc)
    earlier = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
    latest = now.isoformat(timespec="seconds")
    log.write_text("\n".join([
        f"{earlier} UNHEALTHY fails=1 warns=0",
        "    [FAIL] ports: command center NOT listening on 5155",      # an older run
        f"{latest} UNHEALTHY fails=1 warns=2",
        "    [WARN] tasks: PolybotPaper was 'Ready' (dead) - REPAIRED, restarted",
        "    [OK  ] deps: all dashboard/collector imports resolve",
        "    [FAIL] data: stage0 log STALE 18 min (cycle is 60s) - collector alive but "
        "not collecting",
        "    [WARN] news: calendar unreadable: line 4 - fix news_calendar.yaml"]) + "\n")
    h = cc.health_panel()
    assert h["state"] == "unhealthy"
    assert [l.split("]")[0] + "]" for l in h["lines"]] == ["[FAIL]", "[WARN]", "[WARN]", "[OK  ]"]
    assert h["lines"][0].startswith("[FAIL] data: stage0 log STALE")
    assert h["lines"][1].startswith("[WARN] tasks:")            # the watchdog's order within
    assert (h["fails"], h["warns"], h["failing"]) == (1, 2, ["data"])
    # 22 warnings ahead of two failures: the cap keeps the failures, not the warnings
    log.write_text("\n".join([f"{latest} UNHEALTHY fails=2 warns=22"]
                             + [f"    [WARN] news: warning {i}" for i in range(22)]
                             + ["    [FAIL] sleeves: sleeve paper heartbeat 14 min old",
                                "    [FAIL] data: stage0 log STALE 18 min"]) + "\n")
    h = cc.health_panel()
    assert len(h["lines"]) == 20 and h["lines"][0].startswith("[FAIL] sleeves:")
    assert h["lines"][1].startswith("[FAIL] data:") and h["failing"] == ["sleeves", "data"]
    assert (h["fails"], h["warns"]) == (2, 22)
