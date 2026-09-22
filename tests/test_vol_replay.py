"""vol_replay - the conditional crypto test must earn trust the same way the weather
one did: every guard pinned to a fixture that fails without it.

Each test names the defect it prevents. Several were REAL defects caught while
building the tool (see the commit history): the global gate averaging away a
concentrated late-band edge, a band gate opening on a 0.3% rounding-luck 'win', the
fixture book quoting the model's own shape instead of the settlement truth, and a
date-based split collapsing to 190/50 when synthetic windows were packed into 2.5 days.
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import vol_replay as vr  # noqa: E402
from lib.binary_justify import fair_p_above  # noqa: E402


# ── model identity ────────────────────────────────────────────────────────────

def test_fast_cdf_matches_production_integrator():
    """The replay must price with THE production model, not a lookalike."""
    for z in (-4.0, -2.0, -0.5, 0.0, 0.5, 2.0, 4.0):
        direct = fair_p_above(1.0, math.exp(z), 1.0, 1.0, nu=vr.NU)
        fast = vr.p_above_fast(1.0, math.exp(z), 1.0, 1.0)
        assert abs(direct - fast) < 1e-4


def test_fast_cdf_edge_cases():
    assert vr.p_above_fast(100.0, 1.0, 0.001, 10.0) > 0.999   # strike far below
    assert vr.p_above_fast(1.0, 100.0, 0.001, 10.0) < 0.001   # strike far above
    assert vr.p_above_fast(100.0, 50.0, 0.01, 0.0) == 1.0     # expired, above
    assert vr.p_above_fast(None, 50.0, 0.01, 5.0) is None


def test_fast_cdf_table_boundary():
    """z a few ulps under the table's top edge must interpolate, not IndexError
    (adversarial review: (z - _Z_LO)/_Z_STEP rounds up to exactly len(tab)-1)."""
    z = math.nextafter(12.0, 0.0)
    p = vr.p_above_fast(1.0, math.exp(z), 1.0, 1.0)
    assert p is not None and 0.0 <= p < 1e-4


# ── no lookahead ──────────────────────────────────────────────────────────────

def test_sigma_never_uses_the_future():
    """Sigma at time T computed with and without post-T data must be identical."""
    pts = [(float(i * 60), 100.0 * math.exp(0.002 * math.sin(i))) for i in range(120)]
    with_future = vr.TrailingSigma({"S": pts})
    without = vr.TrailingSigma({"S": [p for p in pts if p[0] <= 60 * 60.0]})
    s1 = with_future.at("S", 60 * 60.0)
    s2 = without.at("S", 60 * 60.0)
    assert s1 is not None and abs(s1 - s2) < 1e-15


def test_sigma_requires_warmup():
    pts = [(float(i * 60), 100.0 + i) for i in range(10)]     # < MIN_RETURNS
    assert vr.TrailingSigma({"S": pts}).at("S", 1e9) is None


def test_sigma_skips_outage_gaps():
    """A return spanning a 2-hour collector outage is not a 1-minute return.

    The price JUMPS 50% across the gap - if the dt guard is deleted, that single
    return inflates the EWMA ~4x and this test fails. (The first version barely moved
    the price across the gap and passed with the guard removed - a vacuous test,
    caught by adversarial review.)"""
    calm = [(float(i * 60), 100.0 * (1 + 0.001 * (i % 2))) for i in range(40)]
    jumped = calm + [(calm[-1][0] + 7200.0 + i * 60, 150.0 * (1 + 0.001 * (i % 2)))
                     for i in range(40)]
    s = vr.TrailingSigma({"S": jumped}).at("S", 1e12)
    baseline = vr.TrailingSigma({"S": calm}).at("S", 1e12)
    assert s is not None and s < baseline * 1.5


def test_entries_immune_to_future_spot_perturbation():
    """The docstring's central promise: shifting every spot after a cutoff by +5%
    leaves all entries at or before the cutoff bit-identical."""
    obs, settles, spots = vr._rows_to_parts(vr.make_world("efficient", 30, seed=13))
    a = vr.build_entries(obs, settles, spots)
    cutoff = sorted(e["ts"] for e in a)[len(a) // 2]
    perturbed = {s: [(t, sp * 1.05 if t > cutoff else sp) for t, sp in pts]
                 for s, pts in spots.items()}
    b = vr.build_entries(obs, settles, perturbed)
    ea = [e for e in a if e["ts"] <= cutoff]
    eb = [e for e in b if e["ts"] <= cutoff]
    assert len(ea) == len(eb)
    assert all(x["sigma"] == y["sigma"] for x, y in zip(ea, eb))


# ── data hygiene ──────────────────────────────────────────────────────────────

def test_placeholder_books_rejected():
    assert not vr.book_formed(0.99, 0.99)      # 1c bid / 99c ask shell
    assert not vr.book_formed(None, 0.5)
    assert vr.book_formed(0.07, 0.95)          # ordinary formed book


def test_window_of_strips_strike():
    assert vr.window_of("KXBTC15M-26AUG111200-T64250") == "KXBTC15M-26AUG111200"
    assert vr.window_of("weird") == "weird"


# ── independence: a window is ONE bet ─────────────────────────────────────────

def test_score_never_exceeds_window_count():
    res = vr._run_world(vr.make_world("beatable", 60, seed=3))
    per_band = {}
    for e in res["entries"]:
        per_band.setdefault(e["band"], set()).add(e["window"])
    for band, wins in per_band.items():
        for th in vr.THRESHOLDS:
            assert len(vr.score(res["entries"], res["k"], band, th)) <= len(wins)


# ── the discrimination pair: the tool's licence to be believed ────────────────

def test_finds_nothing_when_the_book_is_true():
    eff = vr._run_world(vr.make_world("efficient", 400, seed=5))
    assert eff["held"] == []


def test_finds_the_planted_edge_and_only_there():
    bt = vr._run_world(vr.make_world("beatable", 400, seed=5))
    assert bt["gates"]["1-2min"][0] or bt["gates"]["<1min"][0]
    assert bt["held"], "planted 8c late mispricing not found at 400 windows"
    assert all(b in ("1-2min", "<1min") for b, _ in bt["held"])


def test_gate_blocks_bands_without_a_real_win():
    """The >10min band of the beatable world is clean; its gate must stay shut even
    when the book's cent-rounding hands the model a hair-thin Brier 'win'."""
    bt = vr._run_world(vr.make_world("beatable", 400, seed=5))
    assert not bt["gates"][">10min"][0]


def test_sub_threshold_plant_is_invisible():
    """A mispricing smaller than spread+fee is not an edge; the tool must not
    manufacture one from it. (The first fixture 'failure' was exactly this, and it
    was the fixture that was wrong.)"""
    rows = vr.make_world("efficient", 150, seed=9)
    # nudge late cheap asks down by 2c - inside friction
    for r in rows:
        if r.get("t") == "obs" and r.get("mins_left", 99) < 2.0:
            if r["yes_ask"] <= r["no_ask"]:
                r["yes_ask"] = round(max(0.01, r["yes_ask"] - 0.02), 2)
            else:
                r["no_ask"] = round(max(0.01, r["no_ask"] - 0.02), 2)
    res = vr._run_world(rows)
    assert res["held"] == []


# ── depth semantics: liquidity is on the OPPOSITE side ────────────────────────

def test_liftable_depth_reads_the_opposite_side():
    """A taker buying YES fills against resting NO bids. Same-side bids are
    competition, not liquidity - counting them (shadow_book._depth_at does) labels
    unfillable books as deep. Adversarial review's exact scenario, pinned."""
    # 1c placeholder YES bid, zero NO liquidity: a YES buy at 7c CANNOT fill
    assert vr.liftable_depth({"yes": [[1, 200]], "no": []}, "yes", 0.07) == 0.0
    # 5 NO bids at 94c: their owners sell YES at 6c -> liftable at a 7c ask
    assert vr.liftable_depth({"no": [[94, 5]]}, "yes", 0.07) == 5.0
    # NO bids at 90c only: selling YES at 10c, NOT liftable at a 7c ask
    assert vr.liftable_depth({"no": [[90, 5]]}, "yes", 0.07) == 0.0
    # buying NO at 17c lifts YES bids >= 83c
    assert vr.liftable_depth({"yes": [[85, 40]]}, "no", 0.17) == 40.0
    # Kalshi serves null for an empty side: captured-and-empty is 0, not unknown
    assert vr.liftable_depth({"yes": [[85, 40]], "no": None}, "yes", 0.07) == 0.0
    assert vr.liftable_depth(None, "yes", 0.07) is None


def test_slice_rows_first_touch_not_hindsight_minimum():
    """The slice entry is the FIRST qualifying observation per window, never a later
    cheaper one - the house convention shadow_book pins. A hindsight minimum-ask pick
    inflates every slice under exactly the mispricing the slices hunt."""
    base = {"series": "KXBTC15M", "band": "1-2min", "won_yes": True,
            "spot": 100.0, "strike": 110.0, "sigma": 0.001, "mins_left": 1.5,
            "book": None, "date": "2026-08-11"}
    e1 = dict(base, ts=1.0, ticker="KXBTC15M-W1-T1", window="KXBTC15M-W1",
              yes_ask=0.10, no_ask=0.92)
    e2 = dict(base, ts=2.0, ticker="KXBTC15M-W1-T1", window="KXBTC15M-W1",
              yes_ask=0.05, no_ask=0.97, band="<1min", mins_left=0.5)
    rows = vr.slice_rows([e1, e2], k=1.0, cut="2026-08-10")
    # every populated slice must reflect the FIRST touch (ask 0.10 -> pnl on a win
    # is 1 - 0.10 - fee(0.10) = 0.89), not the later cheaper 0.05 entry
    series_row = next(r for r in rows if r[0] == "series: KXBTC15M")
    assert series_row[3] == 1                 # one out-of-sample window
    assert abs(series_row[4] - 0.89) < 1e-9, series_row


# ── ledger analysis ───────────────────────────────────────────────────────────

def _ledger(fv_pnl):
    rows = []
    for i, (fv, pnl) in enumerate(fv_pnl):
        rows.append({"t": "open", "ticker": f"KXBTC15M-W{i}-T1", "rule": "R",
                     "fv_edge": fv, "ts": f"2026-08-{11 + i % 15:02d}T00:00:00+00:00"})
        rows.append({"t": "close", "ticker": f"KXBTC15M-W{i}-T1", "rule": "R",
                     "won": pnl > 0, "pnl": pnl})
    return rows


def test_ledger_join_and_bins():
    trades = vr.ledger_rows(_ledger([(0.05, 0.9), (-0.02, -0.08), (None, 0.1)]))["trades"]
    assert [vr._bin_of(t["fv"]) for t in trades] == ["fv 3-8c", "fv < 0", "unstamped"]


def test_ledger_windows_collapse():
    """Two trades in one window are one bet in the per-window view."""
    rows = _ledger([(0.05, 0.9)])
    rows += [{"t": "open", "ticker": "KXBTC15M-W0-T2", "rule": "R2", "fv_edge": 0.05,
              "ts": "2026-08-11T00:00:00+00:00"},
             {"t": "close", "ticker": "KXBTC15M-W0-T2", "rule": "R2", "won": False,
              "pnl": -0.1}]
    trades = vr.ledger_rows(rows)["trades"]
    assert len(vr._per_window(trades)) == 1


# ── frozen grid stays frozen ──────────────────────────────────────────────────

def test_grid_constants_unchanged():
    """The searched grid is part of the pre-registration. Changing it invalidates
    every Bonferroni correction already published - this test makes that loud."""
    assert list(vr.BANDS) == [">10min", "2-10min", "1-2min", "<1min"]
    assert vr.THRESHOLDS == (0.03, 0.05, 0.08, 0.12)
    assert vr.K_GRID[0] == 0.6 and vr.K_GRID[-1] == 1.6
    assert vr.GATE_REL_MARGIN == 0.01 and vr.GATE_MIN_N == 100
    assert vr.SLICE_BANDS == ("1-2min", "<1min")
