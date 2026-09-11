"""Tests for scripts/forecast_replay.py — the conditional (forecast-vs-price) weather test.

This module is the one place in the project that can say "trade weather", so the tests
pin the things that would make it say so wrongly:

  * LOOKAHEAD. The single failure mode that would turn a 2°F forecast error into a 0°F
    one and manufacture an edge out of nothing. Pinned twice: mu_at must ignore future
    best-match temperatures in strict mode, and must refuse a day with no previous-day
    run rather than silently falling back.
  * THE WALK-FORWARD CUT. Window ids start with the city, so sorting them splits Austin
    from New York instead of past from future. Pinned as a chronological date cut.
  * PER-WINDOW INDEPENDENCE. Eight buckets of one city-day resolve on ONE temperature.
    Counting them as eight samples is how a backtest lies about its t-stat.
  * THE SEARCH CORRECTION. 24 cells at the textbook 95% produces a "significant" cell by
    luck about once per run; the correction is what stops that becoming a trade.
"""
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (str(_ROOT), str(_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import forecast_replay as fr


# ── tickers and the market ladder ────────────────────────────────────────────

def test_date_comes_from_the_ticker_not_utc():
    # A New York market day ends at 00:00 local = 04:00 UTC the NEXT day. Reading the
    # date off close_time would file a third of the sample under the wrong day.
    assert fr.parse_ticker("KXHIGHNY-26SEP11-B75.5") == ("KXHIGHNY", "2026-09-11", "B75.5")
    assert fr.parse_ticker("KXHIGHLAX-26JAN02-T88") == ("KXHIGHLAX", "2026-01-02", "T88")
    assert fr.parse_ticker("nonsense") is None
    assert fr.parse_ticker("") is None


def test_bucket_caps_recovered_from_neighbours():
    ms = [{"kind": "B", "floor": f, "lo": None, "hi": None} for f in (70.0, 72.0, 74.0)]
    ms.append({"kind": "T", "floor": 76.0, "lo": None, "hi": None})
    fr.infer_buckets(ms)
    assert [(m["lo"], m["hi"]) for m in ms[:3]] == [(70.0, 72.0), (72.0, 74.0), (74.0, 76.0)]
    assert ms[3]["hi"] is None            # threshold market: unbounded above


def test_bucket_ladder_probabilities_are_a_distribution():
    ms = [{"kind": "B", "floor": 60.0 + 2 * i, "lo": None, "hi": None} for i in range(20)]
    fr.infer_buckets(ms)
    total = sum(fr.fair_p(m, 80.5, 3.0) for m in ms)
    assert 0.98 < total <= 1.0            # a covering ladder must sum to ~1
    peak = max(ms, key=lambda m: fr.fair_p(m, 80.5, 3.0))
    assert peak["lo"] <= 80.5 < peak["hi"]


def test_realized_high_recovered_from_settlement():
    ms = [{"kind": "B", "floor": f, "result": r, "lo": None, "hi": None}
          for f, r in ((70.0, "no"), (72.0, "yes"), (74.0, "no"))]
    fr.infer_buckets(ms)
    assert fr.realized_mid(ms) == 73.0     # midpoint of the bucket that settled YES
    for m in ms:
        m["result"] = "no"
    assert fr.realized_mid(ms) is None     # no winner -> no truth, not a guess


# ── the lookahead guard ──────────────────────────────────────────────────────

DAY = {"temp": [50.0] * 12 + [90.0] * 12,    # what actually happened
       "prev": [50.0] * 12 + [70.0] * 12,    # what yesterday's run predicted
       "utc_offset": -14400}


def test_strict_mode_ignores_the_future_it_cannot_know():
    # At 06:00 a trader knows the overnight lows and yesterday's forecast — not that the
    # afternoon will reach 90.
    assert fr.mu_at(DAY, 6.0, strict=True) == 70.0
    assert fr.mu_at(DAY, 6.0, strict=False) == 90.0     # the contamination, made visible


def test_strict_mode_still_uses_what_has_already_happened():
    # By 20:00 the high is in the past and observed — using it is not lookahead.
    assert fr.mu_at(DAY, 20.0, strict=True) == 90.0


def test_strict_mode_refuses_a_day_with_no_previous_run():
    assert fr.mu_at({"temp": [60.0] * 24, "prev": None}, 8.0, strict=True) is None


def test_missing_previous_run_is_recorded_as_none_not_zeros():
    rows = fr.rows_from_response("KXHIGHNY", {
        "hourly": {"time": ["2026-03-01T00:00"], "temperature_2m": [40.0]}})
    assert rows[0]["prev"] is None


def test_response_parsing_keeps_local_days_intact():
    resp = {"utc_offset_seconds": -18000,
            "hourly": {"time": [f"2026-03-0{d}T{h:02d}:00" for d in (1, 2)
                                for h in range(24)],
                       "temperature_2m": [40.0 + h for d in (1, 2) for h in range(24)],
                       "temperature_2m_previous_day1": [39.0 + h for d in (1, 2)
                                                        for h in range(24)]}}
    rows = fr.rows_from_response("KXHIGHNY", resp)
    assert [r["date"] for r in rows] == ["2026-03-01", "2026-03-02"]
    assert rows[0]["temp"][23] == 63.0 and rows[1]["prev"][0] == 39.0


# ── statistics ───────────────────────────────────────────────────────────────

def test_mean_ci_straddles_zero_on_a_coin_flip():
    m, lo, hi = fr.mean_ci([-0.5, 0.5] * 30)
    assert lo < 0 < hi and abs(m) < 1e-9


def test_mean_ci_clears_zero_on_a_small_consistent_gain():
    _, lo, _ = fr.mean_ci([0.05] * 100 + [0.06] * 100)
    assert lo > 0


def test_search_correction_widens_with_the_number_of_cells():
    assert abs(fr.z_for(0.05) - 1.96) < 0.01
    assert fr.z_for(0.05 / 24) > fr.z_for(0.05 / 4) > fr.z_for(0.05)


def test_a_handful_of_days_can_never_be_actionable():
    # The exact shape the efficient-world fixture produced: 4 out-of-sample city-days,
    # a huge mean, an interval clear of zero — and still not a result.
    assert fr.cell_verdict(5, 0.38, 4, 0.55, 0.21, gate_ok=True).startswith("thin")
    # the same cell with real support is allowed through
    assert fr.cell_verdict(40, 0.10, 40, 0.09, 0.02, gate_ok=True) == "HELD (act)"
    # ...unless the forecast lost the gate, which overrides everything
    assert fr.cell_verdict(40, 0.10, 40, 0.09, 0.02, gate_ok=False).startswith("blocked")
    # positive-then-negative is noise, not an edge
    assert fr.cell_verdict(40, 0.10, 40, -0.05, -0.2, gate_ok=True).startswith("FLIPPED")
    # positive in both halves but the interval touches zero: keep collecting
    assert fr.cell_verdict(40, 0.10, 40, 0.02, -0.01, gate_ok=True).startswith("persists")


def test_brier_scores_the_textbook_cases():
    assert fr.brier([(1.0, True), (0.0, False)]) == 0.0
    assert abs(fr.brier([(0.5, True), (0.5, False)]) - 0.25) < 1e-12
    assert fr.brier([(0.0, True)]) == 1.0


def test_taker_fee_ceils_to_the_cent():
    assert fr.taker_fee(0.50) == 0.02 and fr.taker_fee(0.03) == 0.01
    assert fr.taker_fee(0.0) == 0.0 and fr.taker_fee(1.0) == 0.0


# ── end to end ───────────────────────────────────────────────────────────────

def _held(rows, _z, gate_ok: bool):
    """Cells the TOOL would call actionable — via the tool's own rule, never a re-statement
    of it here, so a loosened rule can't pass tests that were pinning the strict one."""
    return [r for r in rows
            if fr.cell_verdict(r[2], r[3], r[4], r[5], r[6], gate_ok).startswith("HELD")]


def _write_world(tmp_path, beatable: bool, days: int = 60):
    """A miniature of the real data: 2 cities, a bucket ladder per day, quotes through
    the day. `beatable` anchors the book to climatology; otherwise it prices the forecast.
    """
    wx, fc = tmp_path / "wx.jsonl", tmp_path / "fc.jsonl"
    base = datetime(2026, 5, 1, tzinfo=timezone.utc)
    import random
    random.seed(11)

    def p_bucket(lo, hi, mu, s):
        n = lambda x: 0.5 * (1 + math.erf((x - mu) / (s * math.sqrt(2))))
        return n(hi) - n(lo)

    with open(wx, "w") as f, open(fc, "w") as g:
        for d in range(days):
            dt = base + timedelta(days=d)
            day = dt.date().isoformat()
            dcode = dt.strftime("%y%b%d").upper()
            for city, climo in (("KXHIGHNY", 74.0), ("KXHIGHCHI", 71.0)):
                truth = climo + random.gauss(0, 7.0)
                fcst = truth + random.gauss(0, 3.0)
                temp = [truth - 15 if h < 12 else truth for h in range(24)]
                prev = [fcst - 15 if h < 12 else fcst for h in range(24)]
                g.write(json.dumps({"t": "fcday", "series": city, "date": day,
                                    "utc_offset": -14400, "temp": temp,
                                    "prev": prev}) + "\n")
                lo0 = math.floor((climo - 12) / 2) * 2.0
                for i in range(13):
                    floor = lo0 + 2.0 * i
                    won = floor <= truth < floor + 2.0
                    tk = f"{city}-{dcode}-B{floor}"
                    f.write(json.dumps({"t": "market", "series": city, "ticker": tk,
                                        "result": "yes" if won else "no",
                                        "floor_strike": floor,
                                        "close_time": day + "T23:59:00Z"}) + "\n")
                    mu, s = ((climo, 7.0) if beatable else (fcst, 3.0))
                    p = max(0.02, min(0.97, p_bucket(floor, floor + 2.0, mu, s)))
                    yb = max(1, int(round((p - 0.015) * 100)))
                    ya = min(99, max(yb + 1, int(round((p + 0.015) * 100))))
                    for hh in (13, 16):       # 09:00 and 12:00 local
                        ts = int((dt + timedelta(hours=hh)).timestamp())
                        f.write(json.dumps({"t": "candle", "series": city, "ticker": tk,
                                            "end_period_ts": ts, "yes_bid": yb,
                                            "yes_ask": ya}) + "\n")
    return wx, fc


def test_loads_streamed_backfill_into_city_days(tmp_path):
    wx, fc = _write_world(tmp_path, beatable=True, days=10)
    events = fr.load_backfill(wx)
    assert len(events) == 20                      # 10 days x 2 cities
    ev = events[("KXHIGHNY", "2026-05-01")]
    assert len(ev["markets"]) == 13
    assert all(m["lo"] is not None for m in ev["markets"])


def test_banding_is_local_time_not_utc(tmp_path):
    wx, fc = _write_world(tmp_path, beatable=True, days=10)
    entries = fr.build_entries(fr.load_backfill(wx), fr.load_forecasts(fc), strict=True)
    # 13:00 and 16:00 UTC are 09:00 and 12:00 in New York in May
    assert {e["band"] for e in entries} == {"morning", "midday"}


def test_walk_forward_cut_is_chronological_not_alphabetical(tmp_path):
    wx, fc = _write_world(tmp_path, beatable=True, days=20)
    entries = fr.build_entries(fr.load_backfill(wx), fr.load_forecasts(fc), strict=True)
    a, b, cut = fr.split_by_date(entries)
    assert a and b and cut
    assert max(e["date"] for e in a) <= cut < min(e["date"] for e in b)
    # both cities must appear on both sides - a by-city split would fail this
    assert {e["series"] for e in a} == {e["series"] for e in b} == {"KXHIGHNY", "KXHIGHCHI"}


def test_one_bet_per_city_day_however_many_buckets_qualify(tmp_path):
    wx, fc = _write_world(tmp_path, beatable=True, days=20)
    events = fr.load_backfill(wx)
    entries = fr.build_entries(events, fr.load_forecasts(fc), strict=True)
    cal, _, _ = fr.calibrate(entries)
    pnl = fr.score(entries, cal, "morning", 0.03)
    windows = {e["window"] for e in entries if e["band"] == "morning"}
    assert 0 < len(pnl) <= len(windows)           # never more samples than city-days
    assert len(windows) == 40


def test_finds_a_real_conditional_edge(tmp_path):
    wx, fc = _write_world(tmp_path, beatable=True, days=120)
    entries = fr.build_entries(fr.load_backfill(wx), fr.load_forecasts(fc), strict=True)
    first, second, _ = fr.split_by_date(entries)
    cal, mae, _ = fr.calibrate(first)
    assert mae < 4.0
    bm, bp, n = fr.calibration_check(second, cal)
    assert bm < bp, (bm, bp)                      # forecast beats a climatology book
    assert _held(*fr._grid(first, second, cal), gate_ok=True), "planted edge went unfound"


def test_finds_nothing_when_the_book_already_knows(tmp_path):
    """The test that matters most: the book priced AT the forecast's fair value must
    produce no tradeable cell. A tool that cannot return 'nothing' is not a test."""
    wx, fc = _write_world(tmp_path, beatable=False, days=120)
    entries = fr.build_entries(fr.load_backfill(wx), fr.load_forecasts(fc), strict=True)
    first, second, _ = fr.split_by_date(entries)
    cal, _, _ = fr.calibrate(first)
    bm, bp, _ = fr.calibration_check(second, cal)
    rows, z = fr._grid(first, second, cal)
    assert not _held(rows, z, gate_ok=bm < bp), (bm, bp, rows)


def test_strict_and_diagnostic_disagree_when_lookahead_matters(tmp_path):
    """The lookahead variant must look BETTER — that gap is the thing being guarded."""
    wx, fc = _write_world(tmp_path, beatable=False, days=60)
    events, forecasts = fr.load_backfill(wx), fr.load_forecasts(fc)
    strict = fr.build_entries(events, forecasts, strict=True)
    leaky = fr.build_entries(events, forecasts, strict=False)
    cs, _, _ = fr.calibrate(strict)
    cl, _, _ = fr.calibrate(leaky)
    _, sig_s, _ = cs["morning"]
    _, sig_l, _ = cl["morning"]
    assert sig_l < sig_s, (sig_l, sig_s)          # lookahead "forecasts" are too good
