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


def test_a_bad_byte_in_screen_json_costs_only_the_nominations(tmp_path, monkeypatch):
    """screen.json was read strictly: one undecodable byte raised out of the nominations
    panel and the whole /api/command body became {'error': 'UnicodeDecodeError ...'} -
    no rules, no traders, no watchdog verdict, for the least important input."""
    _isolate(tmp_path, monkeypatch)
    row = {"series": "KXINXU", "band": "mid", "side": "yes", "bucket": "80-90c",
           "n_out": 142, "mean_out": 0.021, "p_out": 0.004, "mean_out_traded": 0.016,
           "verdict": "NOMINATED"}
    text = json.dumps({"nominated": [row]}).encode()
    for at in (text.index(b"KXINXU") + 2,           # inside a string: the row survives
               text.index(b"[")):                    # on the structure: no longer JSON
        cc.SCREEN.write_bytes(text[:at] + b"\xff" + text[at + 1:])
        cc._CACHE.update(t=0.0, payload=None)
        body = cc.make_app().test_client().get("/api/command").get_json()
        assert "error" not in body and body["kpi"]["rules"] == 0, body
        assert body["health"]["state"] == "unknown" and len(body["traders"]) == 2
        assert len(body["nominations"]) == (1 if at != text.index(b"[") else 0)


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
    # the crypto trader's heartbeat too: never the live one in data/
    monkeypatch.setattr(cc, "CRYPTO_HEARTBEAT", tmp_path / "crypto_hb.json", raising=False)
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


def test_open_bets_match_on_ticker_and_rule(tmp_path, monkeypatch):
    """Two rules can hold the same market (H1 and H5 both buy late in one window). One
    rule's settlement must not clear the other rule's open bet."""
    _isolate(tmp_path, monkeypatch)
    tk, at = "KXBTC15M-26NOV170330-B7", (T0 + timedelta(hours=1)).isoformat()
    rows = [{"t": "open", "ts": at, "ticker": tk, "rule": r, "price": 0.9, "contracts": 1,
             "fee": 0.01, "v": 4, "depth": 20} for r in ("H1", "H5")]
    rows.append({"t": "close", "ts": at, "ticker": tk, "rule": "H1", "price": 0.9,
                 "won": True, "pnl": 0.09})
    cc.CRYPTO_LEDGER.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    cc._CACHE.update(t=0.0, payload=None)
    shown = {r["rule"]: r for r in cc.build_payload()["rules"]}
    assert shown["H1"]["open"] == 0 and shown["H5"]["open"] == 1


def test_an_unhealthy_verdict_never_reads_as_all_clear():
    """A truncated log can carry an UNHEALTHY header with no [FAIL] line under it; the
    tile once fell through to 'every check passed'."""
    assert "no failing check was logged" in cc._PAGE
    i = cc._PAGE.index('hl.state === "unhealthy" ? "unhealthy, but no failing')
    assert i < cc._PAGE.index('"every check passed"')


def test_each_rule_shows_what_it_won_and_what_it_lost(tmp_path, monkeypatch):
    """The net hides the shape: 97-99c favorites win dimes and lose ~$9.80. Each rule
    carries won / lost / number of losses on its decision record, and the KPI totals."""
    sp = _isolate(tmp_path, monkeypatch)
    _register(sp, "F9799", ["KXHIGHNY"])
    at = T0 + timedelta(hours=1)
    rows = (_sleeve_bet(sp, "F9799", "KXHIGHNY", 0, at, "yes")
            + _sleeve_bet(sp, "F9799", "KXHIGHNY", 1, at, "yes")
            + _sleeve_bet(sp, "F9799", "KXHIGHNY", 2, at, "no"))
    sp._append(sp.LEDGER, rows)
    closes = [r for r in rows if r.get("t") == "close"]
    won = round(sum(r["pnl"] for r in closes if r["pnl"] > 0), 2)
    lost = round(-sum(r["pnl"] for r in closes if r["pnl"] < 0), 2)
    assert won > 0 and lost > 0
    cc._CACHE.update(t=0.0, payload=None)
    p = cc.build_payload()
    r = next(x for x in p["rules"] if x["rule"] == "F9799")
    assert r["money"] == {"won": won, "lost": lost, "losses": 1}
    assert round(r["money"]["won"] - r["money"]["lost"], 2) == round(r["decision"]["net"], 2)
    assert (p["kpi"]["won"], p["kpi"]["lost"]) == (won, lost)
    assert "`won ${dollars(m.won)}`" in cc._PAGE and "`lost ${dollars(m.lost)}`" in cc._PAGE


# ── one panel, one guard: an unreadable input costs its own panel, never the page ──

NOM = {"series": "KXINXU", "band": "mid", "side": "yes", "bucket": "80-90c", "n_out": 142,
       "mean_out": 0.021, "p_out": 0.004, "mean_out_traded": 0.016, "verdict": "NOMINATED"}


def _unreadable(path):
    """Make `path` fail to open the way a file another program holds does (an OSError).
    chmod cannot do it for a test that runs as root; a directory in the file's place
    fails on every system."""
    if path.exists():
        path.unlink()
    path.mkdir()


def _nothing_scored_yet(monkeypatch):
    monkeypatch.setattr(cc, "_GOOD", {"scored": None, "at": None}, raising=False)
    cc._CACHE.update(t=0.0, payload=None)


def _rebuild():
    cc._CACHE.update(t=0.0, payload=None)
    return cc.build_payload()


def test_an_unreadable_ledger_keeps_the_last_good_rules_and_every_other_panel(tmp_path,
                                                                               monkeypatch):
    """A ledger or the registry another program holds open (PermissionError on Windows)
    used to replace the WHOLE /api/command body with one error line: no rules, no trader
    chips, no watchdog verdict - the page blank exactly when something is wrong. Now the
    scored block keeps its last good values, marked with their time, and says why."""
    sp = _isolate(tmp_path, monkeypatch)
    _nothing_scored_yet(monkeypatch)
    _sleeve_rule(sp, "S_FAV90", losses=4)
    _crypto_ledger(cc.CRYPTO_LEDGER)
    cc.SCREEN.write_text(json.dumps({"nominated": [NOM]}), encoding="utf-8")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cc.HEALTH_LOG.write_text(f"{stamp} healthy fails=0 warns=0\n", encoding="utf-8")
    good = cc.build_payload()
    assert good["errors"] == [] and good["scored_at"] == good["generated"]
    assert good["kpi"]["rules"] == 4 and len(good["rules"]) == 4
    for ledger in (cc.CRYPTO_LEDGER, sp.LEDGER, sp.RULES):
        saved = ledger.read_bytes()
        _unreadable(ledger)
        p = _rebuild()
        assert "error" not in cc.make_app().test_client().get("/api/command").get_json()
        assert [e["panel"] for e in p["errors"]] == ["rules"], p["errors"]
        assert "could not be read" in p["errors"][0]["error"]
        # the last good block, WHOLE, with its own time - never one ledger scored alone:
        # every rule is judged in one family, so half of it would give other verdicts
        assert (p["rules"], p["kpi"], p["equity"]) == (good["rules"], good["kpi"],
                                                       good["equity"])
        assert p["scored_at"] == good["generated"] < p["generated"]
        # and every other panel is still there
        assert p["health"]["state"] == "healthy" and p["nominations"] == [NOM]
        crypto = next(t for t in p["traders"] if t["venue"] == "crypto")
        if ledger is cc.CRYPTO_LEDGER:               # unknown - not "no rows yet"
            assert crypto["status"] == "missing" and "could not be read" in crypto["note"]
        else:
            assert crypto["ts"] is not None
        ledger.rmdir()
        ledger.write_bytes(saved)
    back = _rebuild()
    assert back["errors"] == [] and back["scored_at"] == back["generated"]
    assert back["kpi"] == good["kpi"]


def test_before_any_good_build_an_unreadable_ledger_reads_unknown_never_zero(tmp_path,
                                                                             monkeypatch):
    """On a first load there is no last good block. An empty one would print 'Rules that
    have earned an edge: 0 of 0' - unknown shown as a number. The three scored panels are
    null, the error is named, and the page has words for it."""
    _isolate(tmp_path, monkeypatch)
    _nothing_scored_yet(monkeypatch)
    _unreadable(cc.CRYPTO_LEDGER)
    p = cc.build_payload()
    assert (p["kpi"], p["rules"], p["equity"], p["scored_at"]) == (None, None, None, None)
    assert [e["panel"] for e in p["errors"]] == ["rules"]
    assert p["health"]["state"] == "unknown" and p["regime"]["current"]["regime"] == "unknown"
    assert p["nominations"] == [] and len(p["traders"]) == 2
    body = cc.make_app().test_client().get("/api/command").get_json()
    assert "error" not in body and body["kpi"] is None and body["live_money"] == "OFF"
    # the page: a chip per error, and "Unknown" tiles instead of numbers
    assert 'id="notices"' in cc._PAGE and "for (const e of DATA.errors || [])" in cc._PAGE
    assert 'unknownTile("Rules that have earned an edge"' in cc._PAGE
    assert cc._PAGE.index("if (!k) {") < cc._PAGE.index("String(k.edge)")
    assert "The rules could not be read, so none is scored" in cc._PAGE
    assert "if (!DATA.equity) {" in cc._PAGE


def test_a_malformed_ledger_row_stops_the_scoring_and_nothing_else(tmp_path, monkeypatch):
    """A crypto close row whose pnl is not a number: float() raised and the whole payload
    was that error. The row is NOT skipped - dropping a trade would change a rule's record
    without a word. The scored block stops, says why, and keeps its last good values."""
    _isolate(tmp_path, monkeypatch)
    _nothing_scored_yet(monkeypatch)
    _crypto_ledger(cc.CRYPTO_LEDGER, rules=1, n=3)
    good = cc.build_payload()
    assert good["errors"] == [] and good["rules"][0]["all"]["n"] == 3
    with cc.CRYPTO_LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": "close", "ts": T0.isoformat(), "ticker": "KXBTC15M-X-00",
                            "rule": "C0", "price": 0.9, "won": True, "pnl": "n/a"}) + "\n")
    p = _rebuild()
    assert [e["panel"] for e in p["errors"]] == ["rules"] and "n/a" in p["errors"][0]["error"]
    assert p["rules"] == good["rules"] and p["scored_at"] == good["generated"]
    assert len(p["traders"]) == 2 and p["health"]["state"] == "unknown"


def test_a_screen_json_that_is_not_the_scanners_costs_only_the_nominations(tmp_path,
                                                                          monkeypatch):
    """A top-level list or a null entry in screen.json raised AttributeError out of the
    nominations panel and took the whole payload with it. Now whatever is not a nomination
    is skipped, the rest is shown, and a note says the file is damaged - a damaged file
    must not read as 'nothing nominated' in silence."""
    _isolate(tmp_path, monkeypatch)
    _nothing_scored_yet(monkeypatch)
    cases = [('[{"series": "KXA"}]', [], "not the scanner's output"),
             ('{"nominated": [null, %s, 7]}' % json.dumps(NOM), [NOM], "2 of 3 entries"),
             ('{"nominated": {"series": "KXA"}}', [], "not a list"),
             ('{"nominated": [{"series": "KX', [], "could not be read"),
             ('{"nominated": [%s]}' % json.dumps(NOM), [NOM], None),
             ('{"nominated": null}', [], None), ('{"fdr_q": 0.1}', [], None)]
    for text, shown, note in cases:
        cc.SCREEN.write_text(text, encoding="utf-8")
        p = _rebuild()
        assert p["nominations"] == shown and cc.nominations_panel() == shown, text
        notes = [e["error"] for e in p["errors"] if e["panel"] == "nominations"]
        assert len(notes) == (0 if note is None else 1), (text, notes)
        assert note is None or note in notes[0], (text, notes)
        # nothing else is touched
        assert [e for e in p["errors"] if e["panel"] != "nominations"] == []
        assert p["kpi"]["rules"] == 0 and len(p["traders"]) == 2
    cc.SCREEN.unlink()                                 # no file: the scanner has not run
    p = _rebuild()
    assert p["nominations"] == [] and p["errors"] == []


def test_every_other_panel_fails_alone_too(tmp_path, monkeypatch):
    """Regime, health and the trader chips each sit under their own guard: one raising
    leaves its 'unknown' shape and its name in errors, and the rest of the payload."""
    _isolate(tmp_path, monkeypatch)
    _nothing_scored_yet(monkeypatch)
    for fn, panel in (("regime_panel", "regime"), ("health_panel", "health"),
                      ("trader_freshness", "traders")):
        def broken(*a, _panel=panel, **k):
            raise RuntimeError(f"{_panel} broke")
        with monkeypatch.context() as m:
            m.setattr(cc, fn, broken)
            p = _rebuild()
        assert [(e["panel"], e["error"]) for e in p["errors"]] == [
            (panel, f"could not be read - RuntimeError: {panel} broke")]
        assert p["kpi"]["rules"] == 0 and p["rules"] == [] and p["scored_at"] == p["generated"]
        assert p["regime"]["current"]["regime"] == "unknown" and p["health"]["state"] == "unknown"
        assert isinstance(p["traders"], list) and p["nominations"] == []
        if panel == "regime":
            assert p["regime"]["unread"] is True and "regime broke" in p["regime"]["current"]["why"]
        if panel == "health":
            assert "health broke" in p["health"]["lines"][0]


# ── the heartbeats say whether a trader is WORKING, not only that it is alive ──

OLD_KEYS = {"rules": 2, "series": 11, "opened": 0, "closed": 0, "skipped": {}, "ungated": 0,
            "errors": 0, "regime": "good", "open_now": 7}


def _stamp(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _chips(crypto_rows=()):
    return {t["venue"]: t for t in cc.trader_freshness(list(crypto_rows))}


def test_a_sleeve_trader_that_is_alive_but_failing_reads_failing(tmp_path, monkeypatch):
    """A heartbeat a minute old only proves the process is alive. A trader whose cycle
    raises, or whose every market-list call fails cycle after cycle, wrote a fresh
    heartbeat each time and the chip said all was well. It now reads 'failing', in words.
    A heartbeat from before these keys existed is unknown on them - never a fault."""
    sp = _isolate(tmp_path, monkeypatch)

    def sleeve(minutes_ago=1, **more):
        sp.HEARTBEAT.write_text(json.dumps(dict(OLD_KEYS, ts=_stamp(minutes_ago), **more)),
                                encoding="utf-8")
        return _chips()["sleeves"]

    t = sleeve()                                           # the old ten keys only
    assert (t["status"], t["note"]) == ("good", None)
    t = sleeve(all_failed_streak=0, cycle_error=None, failed_cycles=0, poll_errors=0,
               market_errors=0, series_fail_streak={}, took_s=4.2,
               overdue={"n": 0, "oldest": None, "oldest_h": None})
    assert (t["status"], t["note"]) == ("good", None)      # every new key, nothing wrong
    t = sleeve(cycle_error="RuntimeError: regime unreadable", failed_cycles=4)
    assert t["status"] == "failing" and 0.5 < t["age_min"] < 2
    assert t["note"] == ("failing - its last cycle raised RuntimeError: regime unreadable "
                         "(4 cycles in a row)")
    t = sleeve(errors=11, all_failed_streak=3, cycle_error=None)
    assert t["status"] == "failing"
    assert t["note"] == "failing - every market list call has failed for 3 cycles in a row"
    t = sleeve(errors=11, all_failed_streak=2, cycle_error=None)       # two is a blip
    assert (t["status"], t["note"]) == ("good", None)
    t = sleeve(cycle_error="RuntimeError: x", failed_cycles=1, all_failed_streak=7)
    assert t["note"] == ("failing - its last cycle raised RuntimeError: x; every market "
                         "list call has failed for 7 cycles in a row")
    for odd in ("many", None, True, 2.5, [3]):             # not a count: unknown
        assert sleeve(all_failed_streak=odd)["status"] == "good", odd
    t = sleeve(minutes_ago=14, cycle_error="OSError: disk full", failed_cycles=1)
    assert t["status"] == "failing"                         # stopped AND its last word
    assert t["note"] == ("late - it cycles every 2 min · failing - its last cycle raised "
                         "OSError: disk full")
    t = sleeve(overdue={"n": 2, "oldest": "KXHIGHNY-26NOV20-B58", "oldest_h": 61.4})
    assert t["status"] == "warning"
    assert t["note"] == ("2 open positions overdue - more than a day past their close with "
                         "no result (oldest KXHIGHNY-26NOV20-B58, 61 h)")
    for odd in (None, "3", [1], {"n": "x"}, {"oldest": "T"}):          # unknown, no fault
        assert sleeve(overdue=odd)["status"] == "good", odd
    # the page: its own icon for failing, and the note printed whatever the status
    assert 'failing: ["cross", "var(--critical)"]' in cc._PAGE
    assert '(t.note ? ` · ${t.note}` : "")' in cc._PAGE


def test_the_crypto_trader_is_read_from_its_heartbeat_when_it_has_one(tmp_path, monkeypatch):
    """The crypto trader had no heartbeat, so its newest ledger row stood in - and a row
    is written only when a bet opens or settles, so a quiet market and a dead trader
    looked alike. With a heartbeat the chip says when the last CYCLE ran and what it
    reported; without one (a trader still running the older code) nothing changes."""
    _isolate(tmp_path, monkeypatch)
    row = {"t": "open", "ts": _stamp(45), "ticker": "KXBTC15M-26NOV250115-00", "rule": "H1",
           "price": 0.9, "contracts": 1, "fee": 0.01, "v": 4, "depth": 20}

    def crypto(text=None, **hb):
        cc.CRYPTO_HEARTBEAT.write_text(json.dumps(hb) if text is None else text,
                                       encoding="utf-8")
        return _chips([row])["crypto"]

    t = _chips([row])["crypto"]                             # no heartbeat: as before
    assert (t["what"], t["status"]) == ("newest ledger row", "warning") and "quiet" in t["note"]
    t = crypto(ts=_stamp(1), took_s=3.1, opened=0, closed=0, open_now=2, skipped={},
               ungated=0, errors=0, last_error=None, clock_skew_s=0.4, quote_lag_s={},
               overdue={"n": 0, "oldest": None, "oldest_h": None})
    assert (t["what"], t["status"], t["note"]) == ("last cycle", "good", None)
    assert 0.5 < t["age_min"] < 2 and t["limit_min"] == cc.CRYPTO_LATE_MIN
    t = crypto(ts=_stamp(1))                                # only a time: still enough
    assert (t["what"], t["status"], t["note"]) == ("last cycle", "good", None)
    t = crypto(ts=_stamp(9))                                # the watchdog's bar is 10
    assert (t["status"], t["note"]) == ("good", None)
    t = crypto(ts=_stamp(14))
    assert (t["status"], t["note"]) == ("warning", "late - it cycles every minute")
    # ...unless rows were written AFTER that heartbeat: it is trading, and it is the
    # heartbeat that is not being written. The ledger answers, and the chip says why.
    recent = dict(row, ts=_stamp(2), ticker="KXBTC15M-26NOV250130-00")
    cc.CRYPTO_HEARTBEAT.write_text(json.dumps({"ts": _stamp(14)}), encoding="utf-8")
    t = _chips([row, recent])["crypto"]
    assert (t["what"], t["status"]) == ("newest ledger row", "warning") and 1 < t["age_min"] < 3
    assert t["note"] == "its heartbeat is 14 min old and is not being written"
    t = crypto(ts=_stamp(1), errors=6, last_error="markets KXBTC15M: ConnectionError: refused")
    assert t["status"] == "warning" and t["note"] == (
        "6 requests failed in its last cycle (last: markets KXBTC15M: ConnectionError: refused)")
    t = crypto(ts=_stamp(1), errors=1, overdue={"n": 8, "oldest": "KXBTC15M-26NOV230115-00",
                                                "oldest_h": 50.2})
    assert t["status"] == "warning" and t["note"] == (
        "1 request failed in its last cycle · 8 open positions overdue - more than an hour "
        "past their close with no result (oldest KXBTC15M-26NOV230115-00, 50 h)")
    for unreadable in ("{not json", "[1, 2]", '{"took_s": 3.1}', '{"ts": "yesterday"}'):
        t = crypto(text=unreadable)                         # unknown: the ledger stands in
        assert (t["what"], t["status"]) == ("newest ledger row", "warning"), unreadable
    # a heartbeat also answers for a ledger that cannot be read
    cc.CRYPTO_HEARTBEAT.write_text(json.dumps({"ts": _stamp(1)}), encoding="utf-8")
    t = {x["venue"]: x for x in cc.trader_freshness(None)}["crypto"]
    assert (t["what"], t["status"]) == ("last cycle", "good")
    cc.CRYPTO_HEARTBEAT.unlink()
    t = {x["venue"]: x for x in cc.trader_freshness(None)}["crypto"]
    assert t["status"] == "missing" and "could not be read" in t["note"]
