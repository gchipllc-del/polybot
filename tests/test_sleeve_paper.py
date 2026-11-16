"""sleeve_paper - forward paper trading across sleeves, held to the crypto trader's bar."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import sleeve_paper as sp  # noqa: E402

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def mk(tk, ev, ya_c, yb_c, opened_h=-10, closes_h=10, **kw):
    return dict({"ticker": tk, "event_ticker": ev, "yes_ask": ya_c, "yes_bid": yb_c,
                 "open_time": (NOW + timedelta(hours=opened_h)).isoformat(),
                 "close_time": (NOW + timedelta(hours=closes_h)).isoformat()}, **kw)


def _getter(markets, result="yes"):
    def get(url, params=None):
        if url.endswith("/markets"):
            return {"markets": markets, "cursor": None}
        return {"market": {"ticker": url.rsplit("/", 1)[-1], "result": result}}
    return get


def _setup(tmp_path, series=("KXINX",)):
    rp, lp = tmp_path / "rules.jsonl", tmp_path / "ledger.jsonl"
    sp.register(sp.probe_rules(list(series)), rp, NOW - timedelta(days=1))
    return rp, lp


# ── prices: unit from the field name ──────────────────────────────────────────

def test_one_cent_is_never_a_dollar():
    assert sp.price({"yes_bid": 1}, "yes_bid") == 0.01
    assert sp.price({"yes_bid": 100}, "yes_bid") == 1.0
    assert sp.price({"yes_bid_dollars": "0.0100", "yes_bid": 99}, "yes_bid") == 0.01
    assert sp.price({"yes_bid_dollars": "abc"}, "yes_bid") is None


# ── registry ──────────────────────────────────────────────────────────────────

def test_registry_is_append_only_and_first_definition_wins(tmp_path):
    rp = tmp_path / "rules.jsonl"
    assert sp.register([{"name": "R", "series": "S", "band": "mid", "side": "yes",
                         "lo": 0.1, "hi": 0.2}], rp, NOW) == ["R"]
    assert sp.register([{"name": "R", "series": "S", "band": "late", "side": "no",
                         "lo": 0.5, "hi": 0.9}], rp, NOW) == []
    assert sp.active_rules(rp)["R"]["band"] == "mid"           # never redefined
    sp._append(rp, [{"t": "retire", "name": "R"}])
    assert sp.active_rules(rp) == {} and sp.all_registered(rp) == {"R"}


def test_probes_skip_the_crypto_sleeve_already_traded():
    names = [r["name"] for r in sp.probe_rules(["KXINX", "KXBTC15M", "KXETH15M"])]
    assert names == ["FLB_FAV_KXINX", "FLB_LONG_KXINX"]


def test_only_clean_nominations_become_rules():
    rules = sp.nomination_rules({"nominated": [
        {"series": "A", "band": "mid", "side": "no", "bucket": "80-90c",
         "verdict": "NOMINATED", "p_out": 1e-5},
        {"series": "B", "band": "late", "side": "yes", "bucket": "05-10c",
         "verdict": "NOMINATED - phantom risk", "p_out": 1e-6}]})
    assert [r["name"] for r in rules] == ["NOM_A_mid_no_80-90c"]


# ── entry logic ───────────────────────────────────────────────────────────────

def test_life_band_and_side_selection(tmp_path):
    rp, _ = _setup(tmp_path)
    rules = list(sp.active_rules(rp).values())
    fired = {c["rule"]: c for c in sp.candidates(mk("A", "E", 88, 86), NOW.timestamp(), rules)}
    assert fired["FLB_FAV_KXINX"]["side"] == "yes" and fired["FLB_FAV_KXINX"]["price"] == 0.88
    assert fired["FLB_LONG_KXINX"]["side"] == "no" and fired["FLB_LONG_KXINX"]["price"] == 0.14
    early = mk("A", "E", 88, 86, opened_h=-1, closes_h=19)      # life fraction 0.05
    assert sp.candidates(early, NOW.timestamp(), rules) == []
    closed = mk("A", "E", 88, 86, opened_h=-20, closes_h=-1)
    assert sp.candidates(closed, NOW.timestamp(), rules) == []


def test_placeholder_and_wide_books_never_trade(tmp_path):
    rp, _ = _setup(tmp_path)
    rules = list(sp.active_rules(rp).values())
    assert sp.candidates(mk("A", "E", 99, 1), NOW.timestamp(), rules) == []
    assert sp.candidates(mk("A", "E", 90, 70), NOW.timestamp(), rules) == []   # 20c wide


def test_one_bet_per_event_per_rule_across_cycles(tmp_path):
    rp, lp = _setup(tmp_path)
    get = _getter([mk("KXINX-E1-A", "KXINX-E1", 88, 86), mk("KXINX-E1-B", "KXINX-E1", 90, 88)])
    for _ in range(3):
        sp.run_cycle(get, NOW, rp, lp, depth_fn=lambda t, s, p: 5,
                     regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    opens = [r for r in sp._load_jsonl(lp) if r.get("t") == "open"]
    assert sorted(r["rule"] for r in opens) == ["FLB_FAV_KXINX", "FLB_LONG_KXINX"]


def test_depth_gate_refuses_and_failed_lookups_are_counted(tmp_path):
    rp, lp = _setup(tmp_path)
    get = _getter([mk("KXINX-E1-A", "KXINX-E1", 88, 86)])
    s = sp.run_cycle(get, NOW, rp, lp, depth_fn=lambda t, s, p: 0,
                     regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    assert s["opened"] == 0 and s["skipped"]["no_depth"] == 2
    s2 = sp.run_cycle(get, NOW, rp, lp, depth_fn=lambda t, s, p: None,
                      regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    assert s2["opened"] == 2 and s2["ungated"] == 2


def test_rules_named_after_the_trade_never_count(tmp_path):
    rp, lp = tmp_path / "r.jsonl", tmp_path / "l.jsonl"
    sp.register(sp.probe_rules(["KXINX"]), rp, NOW + timedelta(hours=1))   # named later
    s = sp.run_cycle(_getter([mk("KXINX-E1-A", "KXINX-E1", 88, 86)]), NOW, rp, lp,
                     depth_fn=lambda t, s, p: 5,
                     regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    assert s["opened"] == 0


# ── settlement + evaluation ───────────────────────────────────────────────────

def test_settlement_pnl_includes_the_real_fee(tmp_path):
    rp, lp = _setup(tmp_path)
    get = _getter([mk("KXINX-E1-A", "KXINX-E1", 88, 86)], result="yes")
    sp.run_cycle(get, NOW, rp, lp, depth_fn=lambda t, s, p: 5,
                 regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    sp.run_cycle(get, NOW + timedelta(hours=11), rp, lp, depth_fn=lambda t, s, p: 5,
                 regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    closes = {c["rule"]: c for c in sp._load_jsonl(lp) if c.get("t") == "close"}
    assert abs(closes["FLB_FAV_KXINX"]["pnl"] - 0.11) < 1e-9      # +0.12 - 1c fee
    assert abs(closes["FLB_LONG_KXINX"]["pnl"] + 0.15) < 1e-9     # -0.14 - 1c fee


def test_markets_still_open_are_not_polled_for_settlement(tmp_path):
    rp, lp = _setup(tmp_path)
    calls = []

    def get(url, params=None):
        calls.append(url)
        if url.endswith("/markets"):
            return {"markets": [mk("KXINX-E1-A", "KXINX-E1", 88, 86)], "cursor": None}
        return {"market": {"result": "yes"}}
    sp.run_cycle(get, NOW, rp, lp, depth_fn=lambda t, s, p: 5,
                 regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    assert all(u.endswith("/markets") for u in calls)


def test_ledger_passes_the_integrity_checks(tmp_path):
    """The sleeve ledger shares row shapes with the crypto ledger, so the diagnostics
    double-entry checks apply unchanged."""
    import diagnostics as dg
    rp, lp = _setup(tmp_path)
    get = _getter([mk("KXINX-E1-A", "KXINX-E1", 88, 86)], result="no")
    for h in (0, 11):
        sp.run_cycle(get, NOW + timedelta(hours=h), rp, lp, depth_fn=lambda t, s, p: 5,
                     regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    rows = sp._load_jsonl(lp)
    for name, ok, detail in (("B1", *dg.check_b1_ledger_structure(rows)),
                             ("B2", *dg.check_b2_ledger_conservation(rows)),
                             ("C2", *dg.check_c2_ledger_field_identity(rows, []))):
        assert ok, (name, detail)


def test_verdict_needs_events_and_the_family_correction(tmp_path):
    rules = {"R": {"name": "R", "series": "S", "named_at": "2026-01-01T00:00:00+00:00"}}
    rows = []
    for i in range(150):                       # 150 events, a real ~9c edge on 85c favorites
        won = (i % 100) < 94
        tk = f"S-E{i}-A"
        rows.append({"t": "open", "ticker": tk, "rule": "R", "event": f"S-E{i}",
                     "ts": "2026-02-01T00:00:00+00:00", "price": 0.85, "side": "yes",
                     "fee": 0.01, "depth": 5})
        rows.append({"t": "close", "ticker": tk, "rule": "R", "event": f"S-E{i}",
                     "price": 0.85, "side": "yes", "won": won,
                     "pnl": 0.14 if won else -0.86, "result": "yes" if won else "no"})
    r = sp.evaluate(rows, rules)[0]
    assert r["events"] == 150 and r["verdict"].startswith("EDGE"), r
    few = sp.evaluate(rows[:40], rules)[0]
    assert few["verdict"].startswith("collecting")


def test_any_band_rule_trades_at_any_time(tmp_path):
    rp = tmp_path / "r.jsonl"
    sp.register([{"name": "F9799_KXHIGHNY", "series": "KXHIGHNY", "band": "any",
                  "side": "favorite", "lo": 0.97, "hi": 0.995}], rp, NOW - timedelta(days=1))
    rules = list(sp.active_rules(rp).values())
    early = mk("A", "E", 98, 96, opened_h=-1, closes_h=19)      # life fraction 0.05
    late = mk("A", "E", 98, 96, opened_h=-19, closes_h=0.5)     # life fraction 0.97
    for m in (early, late):
        fired = sp.candidates(m, NOW.timestamp(), rules)
        assert [c["rule"] for c in fired] == ["F9799_KXHIGHNY"] and fired[0]["price"] == 0.98
