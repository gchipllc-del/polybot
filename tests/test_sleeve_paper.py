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
        sp.run_cycle(get, NOW, rp, lp, depth_fn=lambda t, s, p: 50,
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
                     depth_fn=lambda t, s, p: 50,
                     regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    assert s["opened"] == 0


# ── settlement + evaluation ───────────────────────────────────────────────────

def test_settlement_pnl_includes_the_real_fee(tmp_path):
    rp, lp = _setup(tmp_path)
    get = _getter([mk("KXINX-E1-A", "KXINX-E1", 88, 86)], result="yes")
    sp.run_cycle(get, NOW, rp, lp, depth_fn=lambda t, s, p: 50,
                 regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    sp.run_cycle(get, NOW + timedelta(hours=11), rp, lp, depth_fn=lambda t, s, p: 50,
                 regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    closes = {c["rule"]: c for c in sp._load_jsonl(lp) if c.get("t") == "close"}
    # probes are 10-lots: ONE order's fee, ceil to the cent - .08 at 88c, .09 at 14c
    assert abs(closes["FLB_FAV_KXINX"]["pnl"] - (1.20 - 0.08)) < 1e-9
    assert abs(closes["FLB_LONG_KXINX"]["pnl"] - (-1.40 - 0.09)) < 1e-9


def test_markets_still_open_are_not_polled_for_settlement(tmp_path):
    rp, lp = _setup(tmp_path)
    calls = []

    def get(url, params=None):
        calls.append(url)
        if url.endswith("/markets"):
            return {"markets": [mk("KXINX-E1-A", "KXINX-E1", 88, 86)], "cursor": None}
        return {"market": {"result": "yes"}}
    sp.run_cycle(get, NOW, rp, lp, depth_fn=lambda t, s, p: 50,
                 regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
    assert all(u.endswith("/markets") for u in calls)


def test_ledger_passes_the_integrity_checks(tmp_path):
    """The sleeve ledger shares row shapes with the crypto ledger, so the diagnostics
    double-entry checks apply unchanged."""
    import diagnostics as dg
    rp, lp = _setup(tmp_path)
    get = _getter([mk("KXINX-E1-A", "KXINX-E1", 88, 86)], result="no")
    for h in (0, 11):
        sp.run_cycle(get, NOW + timedelta(hours=h), rp, lp, depth_fn=lambda t, s, p: 50,
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


def test_the_family_correction_decides_an_edge_verdict():
    """The same 100-event record (85c, 8 losses: exact p = 0.049) is EDGE alone and not
    as one of 31 forward rules - whether the other 30 are shown here or come in as
    extra_stats (retired or crypto rules). The family, not the record, decides."""
    rules = {n: {"name": n, "series": "S", "named_at": "2026-01-01T00:00:00+00:00"}
             for n in ["M"] + [f"N{k}" for k in range(30)]}
    rows = []
    for name in rules:
        for i in range(100):
            won, tk = i >= (8 if name == "M" else 14), f"{name}-E{i}-A"
            rows.append({"t": "open", "ticker": tk, "rule": name, "event": f"{name}-E{i}",
                         "ts": "2026-02-01T00:00:00+00:00", "price": 0.85, "fee": 0.01,
                         "depth": 5})
            rows.append({"t": "close", "ticker": tk, "rule": name, "event": f"{name}-E{i}",
                         "price": 0.85, "won": won, "pnl": 0.14 if won else -0.86})
    only_m = {"M": rules["M"]}
    alone = sp.evaluate(rows, only_m)[0]
    assert alone["category"] == "edge" and alone["verdict"].startswith("EDGE")
    res = {r["rule"]: r for r in sp.evaluate(rows, rules)}
    assert res["M"]["p"] == alone["p"] and res["M"]["category"] == "positive_ns"
    crowd = [sp.forward_stats(ts) for n, ts in sp.trades_of(rows, rules).items() if n != "M"]
    assert sp.evaluate(rows, only_m, crowd)[0]["category"] == "positive_ns"


def test_trades_opened_before_named_at_never_count():
    """Pre-registration is the whole point: a position opened before the rule was named
    (e.g. a rebuilt registry re-registering old names over an older ledger) never reaches
    the verdict - not in the report, not in the dashboard (both go through trades_of)."""
    rules = {"R": {"name": "R", "series": "S", "named_at": "2026-11-10T00:00:00+00:00"}}
    before = {"t": "open", "ticker": "S-E1-A", "rule": "R", "event": "S-E1", "series": "S",
              "ts": "2026-11-09T23:59:00+00:00", "price": 0.85, "side": "yes", "fee": 0.01}
    at = dict(before, ticker="S-E2-A", event="S-E2", ts="2026-11-10T00:00:00+00:00")
    rows = [before, sp.settle(before, "yes"), at, sp.settle(at, "yes")]
    assert [t["event"] for t in sp.trades_of(rows, rules)["R"]] == ["S-E2"]
    assert sp.trades_of(rows[:2], rules) == {"R": []}
    assert sp.evaluate(rows[:2], rules)[0]["events"] == 0


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


def test_hours_before_close_window(tmp_path):
    rp = tmp_path / "r.jsonl"
    sp.register([{"name": "F9799L_KXHIGHNY", "series": "KXHIGHNY", "band": "any",
                  "side": "favorite", "lo": 0.97, "hi": 0.995, "min_h": 6.0, "max_h": 12.0}],
                rp, NOW - timedelta(days=1))
    rules = list(sp.active_rules(rp).values())
    in_window = mk("A", "E", 98, 96, opened_h=-30, closes_h=8)
    too_early = mk("A", "E", 98, 96, opened_h=-10, closes_h=20)
    too_late = mk("A", "E", 98, 96, opened_h=-30, closes_h=3)
    assert [c["rule"] for c in sp.candidates(in_window, NOW.timestamp(), rules)] == \
        ["F9799L_KXHIGHNY"]
    assert sp.candidates(too_early, NOW.timestamp(), rules) == []
    assert sp.candidates(too_late, NOW.timestamp(), rules) == []


def test_rules_without_hours_fields_still_load(tmp_path):
    """Rules registered before the hours window existed have no min_h/max_h."""
    rp, _ = _setup(tmp_path)
    assert all("min_h" not in r for r in sp.active_rules(rp).values())
    assert sp.candidates(mk("A", "E", 88, 86), NOW.timestamp(),
                         list(sp.active_rules(rp).values()))


# ── order size, one rule over many series, exact judging ──────────────────────

def _getter_by_series(by_series, result="yes"):
    def get(url, params=None):
        if url.endswith("/markets"):
            return {"markets": by_series.get((params or {}).get("series_ticker"), []),
                    "cursor": None}
        return {"market": {"ticker": url.rsplit("/", 1)[-1], "result": result}}
    return get


def _cycle(get, rp, lp, at=NOW, depth=50):
    return sp.run_cycle(get, at, rp, lp, depth_fn=lambda t, s, p: depth,
                        regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)


def test_rules_registered_before_sizes_still_trade_one_contract(tmp_path):
    """F9799_KXHIGHNY and the other per-city rules were registered without a size. They
    keep trading exactly as defined: a 1-lot paying a 1-lot's fee."""
    rp, lp = tmp_path / "r.jsonl", tmp_path / "l.jsonl"
    sp.register([{"name": "OLD_KXINX", "series": "KXINX", "band": "any", "side": "favorite",
                  "lo": 0.80, "hi": 0.95}], rp, NOW - timedelta(days=1))
    get = _getter([mk("KXINX-E1-A", "KXINX-E1", 88, 86)], result="yes")
    _cycle(get, rp, lp, depth=1)
    _cycle(get, rp, lp, at=NOW + timedelta(hours=11), depth=1)
    rows = sp._load_jsonl(lp)
    o = next(r for r in rows if r.get("t") == "open")
    c = next(r for r in rows if r.get("t") == "close")
    assert o["contracts"] == 1 and o["fee"] == 0.01 and abs(c["pnl"] - 0.11) < 1e-9


def test_a_ten_lot_needs_ten_deep_and_pays_one_orders_fee(tmp_path):
    # why sizes matter: a 1-lot at 99c pays a 1c fee on a 1c win - it can never profit
    assert sp.kalshi_fee(0.99, 1) == 0.01
    rp, lp = tmp_path / "r.jsonl", tmp_path / "l.jsonl"
    sp.register([{"name": "BIG", "series": "KXINX", "band": "any", "side": "favorite",
                  "lo": 0.97, "hi": 0.995, "contracts": 10}], rp, NOW - timedelta(days=1))
    get = _getter([mk("KXINX-E1-A", "KXINX-E1", 98, 96)])
    s = _cycle(get, rp, lp, depth=9)
    assert s["opened"] == 0 and s["skipped"]["no_depth"] == 1
    assert _cycle(get, rp, lp, depth=10)["opened"] == 1
    o = next(r for r in sp._load_jsonl(lp) if r.get("t") == "open")
    assert o["contracts"] == 10 and o["fee"] == 0.02     # ceil(0.07*10*.98*.02) - not 10c


def test_a_sized_order_needs_its_whole_size_at_the_ask_not_a_tick_worse(tmp_path):
    """liftable_depth tolerates one tick of quote/book skew. A 10-lot is booked entirely AT
    the ask, so size one tick worse must not admit it: 4 at 98c + 50 at 99c really fills
    4 @ .98 + 6 @ .99, not the 10 @ .98 the ledger would book. Legacy 1-lots keep the
    tolerant gate. Real shadow_book depth on live-shaped books, both sides."""
    import shadow_book as sb
    thin_yes = {"orderbook_fp": {"yes_dollars": [["0.9600", "30.00"]],
                                 "no_dollars": [["0.0100", "50.00"], ["0.0200", "4.00"]]}}
    thin_no = {"orderbook_fp": {"yes_dollars": [["0.0100", "50.00"], ["0.0200", "4.00"]]}}
    deep_yes = {"orderbook_fp": {"no_dollars": [["0.0100", "50.00"], ["0.0200", "12.00"]]}}
    yes_fav, no_fav = mk("KXINX-E1-A", "KXINX-E1", 98, 96), mk("KXINX-E1-A", "KXINX-E1", 4, 2)
    base = {"series": "KXINX", "band": "any", "side": "favorite", "lo": 0.97, "hi": 0.995}

    def opens(book, quote, **size):
        rp, lp = tmp_path / "r.jsonl", tmp_path / "l.jsonl"
        for p in (rp, lp):
            p.unlink(missing_ok=True)
        sp.register([dict(base, name="R", **size)], rp, NOW - timedelta(days=1))
        sp.run_cycle(_getter([quote]), NOW, rp, lp,
                     depth_fn=lambda t, s, p: sb._depth_at(book, s, p),
                     regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
        return [(r["contracts"], r["price"], r["depth"]) for r in sp._load_jsonl(lp)
                if r.get("t") == "open"]

    assert sb.liftable_depth(thin_yes, "yes", 0.98) == 54        # the tolerant count
    assert opens(thin_yes, yes_fav, contracts=10) == []           # only 4 at the ask
    assert opens(thin_no, no_fav, contracts=10) == []             # same on the NO side
    assert opens(deep_yes, yes_fav, contracts=10) == [(10, 0.98, 12.0)]   # 12 at the ask
    assert opens(thin_yes, yes_fav) == [(1, 0.98, 54.0)]          # legacy 1-lot: unchanged
    assert opens(thin_no, no_fav) == [(1, 0.98, 54.0)]


def test_the_fee_is_exact_at_every_order_size(tmp_path):
    """Kalshi's fee is ceil(7 * C * P * (100-P) / 10000) cents. Where that is a whole number
    of cents, float noise once rounded it up a cent: a 4-lot at 50c paid 8c, not 7c
    (sizes 4, 8, 12, 25, 50, 100 ... at 10-80c), and the history check (order_fee)
    disagreed with the forward ledger for the same rule."""
    import strategy_check as sc
    assert sp.kalshi_fee(0.50, 4) == 0.07
    for c in range(1, 101):
        for pc in range(1, 100):
            exact = -(-7 * c * pc * (100 - pc) // 10000) / 100.0
            assert sp.kalshi_fee(pc / 100, c) == exact == sc.order_fee(pc / 100, c), (c, pc)
    rp, lp = tmp_path / "r.jsonl", tmp_path / "l.jsonl"            # the ledger books it
    sp.register([{"name": "MID4", "series": "KXINX", "band": "any", "side": "yes",
                  "lo": 0.45, "hi": 0.55, "contracts": 4}], rp, NOW - timedelta(days=1))
    _cycle(_getter([mk("KXINX-E1-A", "KXINX-E1", 50, 48)]), rp, lp)
    assert [r["fee"] for r in sp._load_jsonl(lp) if r.get("t") == "open"] == [0.07]


def test_one_rule_spans_many_series_and_is_one_test(tmp_path):
    rp, lp = tmp_path / "r.jsonl", tmp_path / "l.jsonl"
    sp.register([{"name": "F9799", "series": ["KXHIGHNY", "KXHIGHCHI"], "band": "any",
                  "side": "favorite", "lo": 0.97, "hi": 0.995, "contracts": 10}],
                rp, NOW - timedelta(days=1))
    get = _getter_by_series({
        "KXHIGHNY": [mk("KXHIGHNY-26NOV20-B50", "KXHIGHNY-26NOV20", 3, 2)],     # NO fav, 98c
        "KXHIGHCHI": [mk("KXHIGHCHI-26NOV20-B40", "KXHIGHCHI-26NOV20", 98, 96)]},  # YES fav
        result="no")
    s = _cycle(get, rp, lp)
    assert s["opened"] == 2 and s["series"] == 2
    _cycle(get, rp, lp, at=NOW + timedelta(hours=11))
    res = sp.evaluate(sp._load_jsonl(lp), sp.active_rules(rp))
    assert len(res) == 1                                       # ONE rule, ONE test
    r = res[0]
    assert r["rule"] == "F9799" and r["events"] == 2 and r["contracts"] == 10
    assert r["series"] == "KXHIGHNY,KXHIGHCHI"
    assert r["by_series"]["KXHIGHNY"][1] > 0 > r["by_series"]["KXHIGHCHI"][1]


def test_pooled_series_say_they_are_assumed_independent(tmp_path, monkeypatch, capsys):
    """One bet per (event, rule) counts each pooled series' event as its own bet, so the
    exact p assumes the series settle independently - false for two series on one
    underlying (one bust day would count twice). Registration and the report must say so
    for every rule that pools series, and only for those."""
    import lib.command_center as cc
    for name, file in (("RULES", "rules.jsonl"), ("LEDGER", "ledger.jsonl"),
                       ("HEARTBEAT", "hb.json")):
        monkeypatch.setattr(sp, name, tmp_path / file)
    monkeypatch.setattr(cc, "CRYPTO_LEDGER", tmp_path / "crypto.jsonl")

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["sleeve_paper.py", *argv])
        assert sp.main() == 0
        return capsys.readouterr().out

    assert "independent" not in run("register-rule", "--name", "SOLO", "--series", "KXHIGHNY")
    assert "independent" not in run("report")
    out = run("register-rule", "--name", "POOL", "--series", "KXINX,KXINXU")
    assert "the exact p ASSUMES these series" in out and "settle independently" in out
    line = next(l for l in run("report").splitlines() if l.startswith("POOLED SERIES"))
    assert "ASSUMED INDEPENDENT (POOL)" in line and "SOLO" not in line


def test_poisson_binomial_tail_matches_brute_force():
    import itertools
    import random
    rng = random.Random(7)
    for _ in range(25):
        n = rng.randint(1, 9)
        qs = [rng.uniform(0.001, 0.7) for _ in range(n)]
        for k in range(-1, n + 1):
            brute = 0.0
            for outcome in itertools.product((0, 1), repeat=n):
                if sum(outcome) <= k:
                    pr = 1.0
                    for q, o in zip(qs, outcome):
                        pr *= q if o else 1.0 - q
                    brute += pr
            assert abs(sp._pb_cdf(qs, k) - brute) < 1e-12, (qs, k)
            if 0 <= k <= n:                   # forward_stats' two tails, one pass
                at_least = 1.0 - (sp._pb_cdf(qs, k - 1) if k else 0.0)
                lo, hi = sp._pb_tails(qs, k)
                assert abs(lo - brute) < 1e-12 and abs(hi - at_least) < 1e-12, (qs, k)


def _binom_tails(n, q, k):
    """Exact (P(K <= k), P(K >= k)) for K ~ Binomial(n, q), summed in log space."""
    import math
    lg, lq, lr = math.lgamma, math.log(q), math.log1p(-q)
    pmf = [math.exp(lg(n + 1) - lg(j + 1) - lg(n - j + 1) + j * lq + (n - j) * lr)
           for j in range(n + 1)]
    return sum(pmf[:k + 1]), sum(pmf[k:])


def test_poisson_binomial_normal_limit_is_accurate_where_used():
    """Past the exact DP's budget the skewness-corrected normal answers - for mid-price
    rules, and for skewed ones only with hundreds of the rarer outcome. There it must hold
    the BH-relevant tails to 1%: a 7c longshot rule over 12,000 events (the plain normal
    is ~10% low at p ~ 2e-3 there, the anti-conservative way), and 40c 1-lots."""
    q_long = 1 - (0.07 + sp.kalshi_fee(0.07, 10) / 10)
    for n, q, ks in ((12000, q_long, (11017, 11033, 11045)), (8000, 0.4, (3076, 3100, 3118))):
        qs = [q] * n
        for k in ks:
            exact = _binom_tails(n, q, k)[0]
            assert not sp._pb_exact(n, k) and 1e-3 < exact < 0.05, (n, k, exact)
            assert abs(sp._pb_cdf(qs, k) / exact - 1) < 0.01, (n, k)
            assert abs(sp._pb_tails(qs, k)[0] / exact - 1) < 0.01, (n, k)


def test_records_past_200_of_the_rarer_outcome_get_exact_tails():
    """The normal limit used to take over at 200 of each outcome, whatever the skew - for
    longshot rules it understated p by 15-45% exactly where BH thresholds sit. A 10-lot
    rule on 7c longshots with 2600 events is well inside the exact DP's budget now."""
    q = 1 - (0.07 + sp.kalshi_fee(0.07, 10) / 10)          # the no-edge loss chance
    for wins in (226, 231, 236):                            # exact p ~ 1e-2 .. 2e-3
        st = sp.forward_stats(_house(wins, 2600 - wins, ask=0.07))
        assert abs(st["p"] / _binom_tails(2600, q, 2600 - wins)[0] - 1) < 1e-9, wins
    # 231 wins (+1.4c/contract): exact p = 4.9e-3. Among 22 testable rules (BH rank-1 bar
    # 0.10/22 = 0.0045) that is no edge; the normal limit's 4.1e-3 made it EDGE.
    st = sp.forward_stats(_house(231, 2600 - 231, ask=0.07))
    others = [{"n": 100, "p": 0.5, "p_neg": 0.5, "mean": 0.0}] * 21
    assert sp.classify(st, 0 in sp.survivors([st] + others)) == "positive_ns"
    # the NEGATIVE side too: 90c favorites, 2400 events, 260 losses
    q90 = 1 - (0.90 + sp.kalshi_fee(0.90, 10) / 10)
    st = sp.forward_stats(_house(2400 - 260, 260, ask=0.90))
    assert abs(st["p_neg"] / _binom_tails(2400, q90, 260)[1] - 1) < 1e-9


def _house(wins, losses, ask=0.98, lots=10):
    fee = sp.kalshi_fee(ask, lots)
    out = []
    for i in range(wins + losses):
        won = i < wins
        pnl = (lots * (1 - ask) if won else -lots * ask) - fee
        out.append({"event": f"E{i}", "won": won, "ask": ask, "fee_pc": fee / lots,
                    "pnl": pnl, "pnl_pc": pnl / lots})
    return out


def test_a_hundred_straight_wins_at_98c_is_ordinary_luck():
    """The 'it hasn't lost yet' trap, at the size the friend's method trades."""
    st = sp.forward_stats(_house(100, 0))
    assert abs(st["p"] - 0.982 ** 100) < 1e-9 and st["p"] > 0.15
    assert sp.classify(st, False) == "positive_ns"
    assert sp.classify(sp.forward_stats(_house(40, 0)), False) == "positive_ns"


def test_three_losses_in_thirty_at_98c_is_significantly_negative():
    """Where the normal interval still straddles zero, the exact tail is decisive."""
    import math
    st = sp.forward_stats(_house(27, 3))
    q = 1 - 0.982
    tail = 1 - sum(math.comb(30, j) * q ** j * (1 - q) ** (30 - j) for j in range(3))
    assert abs(st["p_neg"] - tail) < 1e-9 and st["p_neg"] < 0.025
    assert st["hi"] > 0                                # the old interval test would miss it
    assert sp.classify(st, False) == "negative"


def test_a_one_lot_at_99c_is_no_evidence_under_no_edge():
    """A 1-lot at 99c pays a 1c fee: its win nets exactly 0, so with no edge it cannot
    lose (ask + fee = $1.00, loss chance 0). A 0.001 floor on that chance shrank p by
    0.999 per such win for nothing: 200 wins at 99c + 140 at 97c (a pre-size per-city
    rule) read p = 0.048 - EDGE at a 0.05 bar - where the null gives 0.98**140 = 0.059."""
    named = "2026-09-01T00:00:00+00:00"
    rules = {"F9799_KXHIGHNY": {"name": "F9799_KXHIGHNY", "series": "KXHIGHNY",
                                "named_at": named},             # no size: a 1-lot rule
             "OTHER": {"name": "OTHER", "series": "KXX", "named_at": named}}
    rows = []
    for i, (rule, price, won) in enumerate(
            [("F9799_KXHIGHNY", 0.99, True)] * 200 + [("F9799_KXHIGHNY", 0.97, True)] * 140
            + [("OTHER", 0.85, j >= 14) for j in range(100)]):
        o = {"t": "open", "ts": "2026-09-02T00:00:00+00:00", "ticker": f"{rule}-E{i}-A",
             "rule": rule, "event": f"{rule}-E{i}", "series": "S", "side": "yes",
             "price": price, "contracts": 1, "fee": sp.kalshi_fee(price, 1)}
        rows += [o, sp.settle(o, "yes" if won else "no")]
    r = {x["rule"]: x for x in sp.evaluate(rows, rules)}["F9799_KXHIGHNY"]
    assert r["events"] == 340 and abs(r["p"] / 0.98 ** 140 - 1) < 1e-9, r["p"]
    assert r["verdict"] == "positive, not significant"           # 2 rules: BH bar 0.05


def test_non_house_shaped_trades_fall_back_to_the_floored_test():
    from sleeve_scan import p_value
    rows = [{"event": f"E{i}", "won": i % 2 == 0, "ask": 0.5,
             "pnl": 0.02 if i % 2 == 0 else -0.03} for i in range(40)]   # not hold-to-expiry
    st = sp.forward_stats(rows)
    se = (st["hi"] - st["mean"]) / 1.96
    assert st["p"] == p_value(st["mean"], se, rows)


def test_cli_one_rule_per_hypothesis_and_retire_by_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(sp, "RULES", tmp_path / "rules.jsonl")
    sp.register([{"name": f"F9799_{c}", "series": c, "band": "any", "side": "favorite",
                  "lo": 0.97, "hi": 0.995} for c in ("KXHIGHNY", "KXHIGHCHI")], sp.RULES, NOW)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["sleeve_paper.py", *argv])
        return sp.main()

    assert run("retire", "F9799_KXHIGHNY", "NOPE") == 1          # all-or-nothing
    assert set(sp.active_rules(sp.RULES)) == {"F9799_KXHIGHNY", "F9799_KXHIGHCHI"}
    assert run("retire", "--prefix", "F9799_") == 0
    assert sp.active_rules(sp.RULES) == {}
    args = ("register-rule", "--name", "F9799", "--series", "KXHIGHNY,KXHIGHCHI,KXBTC15M",
            "--side", "favorite", "--lo", "0.97", "--hi", "0.995", "--band", "any")
    assert run(*args) == 0
    r = sp.active_rules(sp.RULES)["F9799"]
    assert r["series"] == ["KXHIGHNY", "KXHIGHCHI"] and r["contracts"] == 10
    assert run(*args) == 1                                         # never redefined
