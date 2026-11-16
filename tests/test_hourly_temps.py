"""hourly_temps: settlements, index reading at entry, the conditional pick. Fakes only."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import hourly_temps as ht  # noqa: E402

T0 = 1_794_798_000.0          # an exact hour (2026-11-16 03:00Z)


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _market(ev, settle, strike, value, result=None):
    res = result or ("yes" if value > strike else "no")
    return {"ticker": f"{ev}-T{strike}", "event_ticker": ev, "strike_type": "greater",
            "floor_strike": strike, "open_time": _iso(settle - 3600),
            "close_time": _iso(settle), "result": res, "expiration_value": f"{value:.2f}"}


def test_selftest_passes():
    assert ht._selftest() == 0


def test_margin_is_distance_to_the_strike_line_on_the_favored_side():
    assert ht.favored(66.99, 68.0) == ("yes", 1.0)       # '67 or above' at 68
    assert ht.favored(68.99, 68.0) == ("no", 1.0)        # '69 or above' at 68
    assert ht.favored(67.99, 68.0) == ("yes", 0.0)       # sitting on the line
    assert ht.favored(40.99, 38.84) == ("no", 2.16)      # index values are not integers
    assert ht.favored(35.99, 39.56) == ("yes", 3.56)


def test_fetch_settled_paginates_and_event_rows_groups():
    pages = [{"markets": [_market("S-A", T0, 66.99, 68.0), _market("S-A", T0, 68.99, 68.0)],
              "cursor": "c1"},
             {"markets": [_market("S-B", T0 + 3600, 66.99, 66.0)], "cursor": ""}]
    calls = []

    def get(url, params):
        calls.append(dict(params))
        return pages[len(calls) - 1]
    ms = ht.fetch_settled("S", get=get, sleep_s=0)
    assert len(ms) == 3 and calls[1]["cursor"] == "c1"
    assert all(c["status"] == "settled" for c in calls)
    evs = ht.event_rows(ms)
    assert [e["event"] for e in evs] == ["S-A", "S-B"]
    assert evs[0]["value"] == 68.0 and evs[0]["settle_ts"] == T0
    assert [s["strike"] for s in evs[0]["strikes"]] == [66.99, 68.99]
    assert all(ht.consistent(e) for e in evs)
    bad = ht.event_rows([_market("S-C", T0, 66.99, 68.0, result="no")])
    assert not ht.consistent(bad[0])


def test_parse_iem_csv_both_formats_skip_missing():
    a = "station,valid,tmpf\nLAX,2026-11-16 02:53,68.00\nLAX,2026-11-16 02:55,M\n"
    b = "station,station_name,valid(UTC),tmpf\nMIA,MIAMI,2026-11-16 02:00,77\n"
    assert ht.parse_iem_csv(a) == {"LAX": [(T0 - 7 * 60, 68.0)]}
    assert ht.parse_iem_csv(b)["MIA"] == [(T0 - 3600, 77.0)]


def test_fetch_1min_chunks_the_range():
    seen = []

    def get_text(url, params):
        seen.append((params["sts"], params["ets"]))
        return "station,station_name,valid(UTC),tmpf\nMIA,MIAMI,%s,70\n" % (
            params["sts"].replace("T", " ").replace("Z", ""))
    obs = ht.fetch_1min("MIA", T0, T0 + 25 * 86400, get_text=get_text, chunk_days=10)
    assert len(seen) == 3 and len(obs) == 3


def test_reading_at_entry_uses_previous_value_and_delayed_proxy_change():
    evs = [{"event": "S-0", "settle_ts": T0, "value": 50.0, "strikes": []},
           {"event": "S-1", "settle_ts": T0 + 3600, "value": 53.0, "strikes": []},
           {"event": "S-3", "settle_ts": T0 + 3 * 3600, "value": 40.0, "strikes": []}]
    prev = ht.prev_values(evs)
    assert prev == {"S-1": 50.0}                 # S-3 has no previous hour
    one = {"A": [(T0, 60.0), (T0 + 24 * 60, 61.0), (T0 + 25 * 60, 64.0)],
           "B": [(T0, 40.0), (T0 + 25 * 60, 42.0)]}
    e = evs[1]
    assert ht.reading_at_entry(e, 60, prev, one, ["A", "B"]) == 50.0
    # entry 30m before, the app shows the index 5 minutes late -> change to T0+25m
    assert ht.reading_at_entry(e, 30, prev, one, ["A", "B"]) == 53.0
    assert ht.reading_at_entry(evs[2], 30, prev, one, ["A"]) is None
    # a station missing at either end is left out (no mix shift)
    one["C"] = [(T0 + 25 * 60, 99.0)]
    assert ht.reading_at_entry(e, 30, prev, one, ["A", "B", "C"]) == 53.0
    assert ht.quote_time(T0 + 3600, 60) == T0 + 300
    assert ht.quote_time(T0 + 3600, 30) == T0 + 1800


def _ev(strikes, value, settle=T0):
    return {"event": "S-X", "settle_ts": settle, "value": value,
            "strikes": [{"ticker": f"S-X-T{k}", "strike": k,
                         "result": "yes" if value > k else "no"} for k in strikes]}


def test_pick_largest_in_band_margin_and_statuses():
    e = _ev([63.99, 64.99, 65.99, 66.99, 68.99, 70.99], value=68.0)
    q = T0 - 1800
    c = {  # (end_ts, yes_bid, yes_ask, lo, hi, vol)
        "S-X-T63.99": [(q - 30, 0.99, 1.0, None, None, 0)],   # yes priced out
        "S-X-T64.99": [(q - 30, 0.98, 0.99, 0.99, 0.99, 3), (q + 30, 0.98, 0.99, 0.99, 0.99, 3)],
        "S-X-T65.99": [(q - 30, 0.96, 0.98, None, None, 0)],
        "S-X-T66.99": [(q - 30, 0.90, 0.95, None, None, 0)],  # margin 1 only
        "S-X-T68.99": [(q - 30, 0.02, 0.04, None, None, 0)],  # NO at 98c, margin 1
        "S-X-T70.99": [(q - 30, 0.0, 0.01, None, None, 0)],   # NO priced out (no bid)
    }
    got = ht.pick(e, 68.0, c, q, 2.0)
    assert got["status"] == "bet" and got["strike"] == 64.99 and got["side"] == "yes"
    assert got["margin"] == 3.0 and got["ask"] == 0.99 and got["won"] and got["trade_ok"]
    assert ht.pick(e, 68.0, c, q, 5.0)["status"] == "none"
    only_out = {k: v for k, v in c.items() if k in ("S-X-T63.99", "S-X-T70.99")}
    assert ht.pick(e, 68.0, only_out, q, 2.0)["status"] == "priced_out"
    assert ht.pick(e, 68.0, {}, q, 2.0)["status"] == "no_quote"
    stale = {"S-X-T64.99": [(q - 3600, 0.98, 0.99, None, None, 0)]}
    assert ht.pick(e, 68.0, stale, q, 2.0)["status"] == "no_quote"


def test_trade_confirmed_needs_a_print_through_the_price():
    q = T0
    yes_ok = [(q + 60, 0.97, 0.98, 0.98, 0.98, 5)]
    assert ht.trade_confirmed(yes_ok, q, "yes", 0.98)
    assert not ht.trade_confirmed([(q + 60, 0.97, 0.98, 0.99, 0.99, 5)], q, "yes", 0.98)
    assert not ht.trade_confirmed([(q + 60, 0.97, 0.98, 0.98, 0.98, 0)], q, "yes", 0.98)
    # buying NO at 0.98 = selling YES at 0.02: needs a YES print >= 0.02
    assert ht.trade_confirmed([(q + 60, 0.02, 0.03, 0.02, 0.03, 1)], q, "no", 0.98)


def test_score_fee_breakeven_and_exact_p():
    bets = [{"event": f"E{i}", "ask": 0.98, "won": True} for i in range(50)]
    t = ht.score(bets)
    # 10-lot fee at 98c: ceil(0.07*10*.98*.02*100)/100 = $0.02 -> 0.2c per contract
    assert abs(t["breakeven"] - 0.982) < 1e-9
    assert abs(t["pnl_pc"] - 0.018) < 1e-9 and t["wins"] == 50
    assert abs(t["p"] - 0.982 ** 50) < 1e-6           # 50 straight wins is weak evidence
    m = ht.score(bets, maker=True)
    assert abs(m["pnl_pc"] - 0.02) < 1e-9 and m["p"] < t["p"]
    lost = ht.score(bets + [{"event": "L", "ask": 0.98, "won": False}])
    assert lost["pnl_pc"] < t["pnl_pc"] and lost["p"] > t["p"]


def test_flip_table_counts_strikes_and_events():
    e1 = _ev([63.99, 66.99, 69.99], value=68.0)
    e1["event"] = "E1"
    e2 = _ev([63.99, 66.99, 69.99], value=70.5)
    e2["event"] = "E2"
    rows = ht.flip_table([e1, e2], {"E1": 68.0, "E2": 68.0}, edges=(0, 2, 4))
    # reading 68 for both: 66.99 yes m1, 69.99 no m2, 63.99 yes m4
    by = {r["lo"]: r for r in rows}
    assert by[0]["strikes"] == 2 and by[0]["flips"] == 0
    assert by[2]["strikes"] == 2 and by[2]["flips"] == 1 and by[2]["events_flipped"] == 1
    assert by[4]["strikes"] == 2 and by[4]["flips"] == 0


def test_calibrate_and_run_conditional_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(ht, "OUT_DIR", tmp_path)
    s = "KXTEMPMIAH"
    evs = []
    for i in range(4):
        settle = T0 + i * 3600
        evs.append({"event": f"{s}-H{i}", "series": s, "open_ts": settle - 3600,
                    "settle_ts": settle, "value": 70.0 + i,
                    "strikes": [{"ticker": f"{s}-H{i}-T{k}", "strike": k,
                                 "result": "yes" if 70.0 + i > k else "no"}
                                for k in (65.99, 66.99, 74.99, 76.99)]})
    (tmp_path / f"settle_{s}.jsonl").write_text(
        "\n".join(json.dumps(e) for e in evs) + "\n", encoding="utf-8")
    # proxy: MIA warms 1 degF per hour, linearly
    rows = ["station,valid,tmpf"]
    for k in range(0, 4 * 60 + 1):
        ts = T0 - 3600 + k * 60
        rows.append(f"MIA,{datetime.fromtimestamp(ts, tz=timezone.utc):%Y-%m-%d %H:%M},"
                    f"{69.0 + k / 60.0:.2f}")
    (tmp_path / "asos1min_MIA.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    cal = ht.calibrate(s, evs, ht.load_1min(ht.PROXY[s]))
    assert cal["persistence"]["n"] == 3 and abs(cal["persistence"]["mae"] - 1.0) < 1e-9
    assert cal["proxy"]["mae"] < 1e-6

    final = tmp_path / "final"
    final.mkdir()
    lines = []
    for e in evs:
        for st in e["strikes"]:
            for mins in range(0, 61):
                ts = e["settle_ts"] - 3600 + mins * 60
                yes_fav = st["result"] == "yes"
                yb, ya = (0.97, 0.98) if yes_fav else (0.02, 0.03)
                lines.append(json.dumps({"t": "candle", "series": s, "ticker": st["ticker"],
                                         "end_period_ts": ts,
                                         "yes_bid": {"close_dollars": f"{yb:.4f}"},
                                         "yes_ask": {"close_dollars": f"{ya:.4f}"},
                                         "price": {}, "volume_fp": "0.00"}))
    (final / f"{s}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = ht.run_conditional([s], final, margins=(2.0,), entries=(60, 30))
    c60 = res[(60, 2.0)]
    assert c60["counts"].get("no_reading") == 1          # the first hour has no previous
    assert len(c60["bets"]) == 3 and all(b["won"] for b in c60["bets"])
    assert all(b["ask"] == 0.98 for b in c60["bets"])
    assert len(res[(30, 2.0)]["bets"]) == 3
    txt = ht.report_conditional(res)
    assert "60m" in txt and "30m" in txt
