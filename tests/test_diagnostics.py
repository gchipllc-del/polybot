"""diagnostics - each detector must fire on its planted corruption, and ONLY it.

A validation layer that has never been seen to catch its target is decoration. These
tests plant one specific defect per check against a clean fixture and assert the
right alarm - and no other - goes off. Several pin defects adversarial review found
in the FIRST version of this layer: a permanent watchdog FAIL from one old outage,
null-price feed breaks passing as clean, P&L recomputed from the close instead of
its open, torn ledger lines ignored, and closes never checked against the
collector's own settlements.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import diagnostics as dg  # noqa: E402


def _failed(s0, led):
    return [name for name, ok, _ in dg.run_all(s0, led) if not ok]


def _edit(rows, i, **kw):
    out = [dict(r) for r in rows]
    out[i].update(kw)
    return out


def test_clean_fixture_passes_everything():
    s0, led = dg._fixture()
    assert _failed(s0, led) == []


# ── A: input validation ───────────────────────────────────────────────────────

def test_negative_spot_trips_input_validation():
    s0, led = dg._fixture()
    assert _failed(_edit(s0, 0, spot=-5.0), led) == ["A1 obs bounds"]


def test_crossed_book_counts_as_bad_data():
    s0, led = dg._fixture()
    assert _failed(_edit(s0, 0, yes_ask=0.30, no_ask=0.30), led) == ["A1 obs bounds"]


def test_null_asks_feed_break_is_caught():
    """The likeliest feed failure: a renamed field -> the collector writes null asks
    -> every loader silently skips the row. The first A1 counted these as CLEAN."""
    s0, led = dg._fixture()
    bad = _edit(_edit(s0, 0, yes_ask=None), 1, no_ask=None)
    assert _failed(bad, led) == ["A1 obs bounds"]


def test_all_garbage_file_fails():
    s0, led = dg._fixture()
    assert _failed([{"_unparseable": True}] * 5, led) == ["A1 obs bounds"]


def test_non_numeric_and_nan_prices_are_bad_not_crashes():
    s0, led = dg._fixture()
    assert _failed(_edit(s0, 0, spot="abc"), led) == ["A1 obs bounds"]
    assert _failed(_edit(s0, 0, strike=float("nan")), led) == ["A1 obs bounds"]


def test_fresh_garbage_not_diluted_by_old_history():
    """A1 judges RECENT rows: 10,000 clean old rows must not hide a fresh break."""
    old = [{"t": "obs", "ts": "2026-08-01T00:00:00+00:00", "series": "S",
            "ticker": "T", "yes_ask": 0.5, "no_ask": 0.52}] * 10000
    fresh = [{"t": "obs", "ts": "2026-09-01T00:00:00+00:00", "series": "S",
              "ticker": "T", "yes_ask": None, "no_ask": None}] * 50
    ok, detail = dg.check_a1_obs_bounds(old + fresh)
    assert not ok, detail


def test_contradictory_settlement_is_caught():
    s0, led = dg._fixture()
    bad = s0 + [{"t": "settle", "ticker": "KXBTC15M-W1-T1", "result": "yes"}]
    assert _failed(bad, led) == ["A2 settle consistency"]


# ── B: conservation ───────────────────────────────────────────────────────────

def test_orphan_close_breaks_structure():
    s0, led = dg._fixture()
    ghost = {"t": "close", "ticker": "GHOST", "rule": "R", "side": "yes",
             "price": 0.5, "result": "yes", "won": True, "pnl": 0.48}
    assert _failed(s0, led + [ghost]) == ["B1 ledger structure"]


def test_torn_ledger_line_breaks_structure():
    """A power loss mid-append can merge two rows into one unparseable line; if one
    was an open, that trade never settles and vanishes from the P&L."""
    s0, led = dg._fixture()
    assert _failed(s0, led + [{"_unparseable": True}]) == ["B1 ledger structure"]


def test_one_stolen_cent_breaks_conservation():
    s0, led = dg._fixture()
    assert _failed(s0, _edit(led, 1, pnl=-0.07)) == ["B2 ledger conservation"]


def test_off_grid_pnl_is_flagged_not_rounded_away():
    s0, led = dg._fixture()
    assert _failed(s0, _edit(led, 1, pnl=-0.080000437)) == ["B2 ledger conservation"]


def test_close_price_must_match_its_open():
    """Review scenario (a): a close rewritten to price 0.80 with P&L consistent WITH
    0.80 passed the first version, because it recomputed from the close itself."""
    s0, led = dg._fixture()
    assert _failed(s0, _edit(led, 3, price=0.80, pnl=0.19)) == ["B2 ledger conservation"]


def test_side_flip_with_consistent_pnl_is_caught():
    """Review scenario (b): a -0.08 loss rewritten as side=no, won, +0.92 passed
    every check in the first version. Two fields corrupted -> two alarms."""
    s0, led = dg._fixture()
    bad = _edit(led, 1, side="no", won=True, pnl=0.92)
    assert set(_failed(s0, bad)) == {"B2 ledger conservation",
                                     "C2 ledger field identity"}


# ── C: identity / inversion ───────────────────────────────────────────────────

def test_won_flag_contradiction_is_identity_only():
    """P&L is now recomputed from result vs the OPEN's side, so a flipped `won`
    flag alone leaves conservation intact - exactly one alarm, the right one."""
    s0, led = dg._fixture()
    assert _failed(s0, _edit(led, 3, won=False)) == ["C2 ledger field identity"]


def test_invalid_result_fails_instead_of_being_skipped():
    s0, led = dg._fixture()
    assert _failed(s0, _edit(led, 1, result="YES")) == ["C2 ledger field identity"]


def test_close_must_agree_with_collector_settlement():
    """The ledger says YES, the collector recorded NO. P&L is kept self-consistent,
    so only the cross-file join can see it."""
    s0, led = dg._fixture()
    bad = _edit(led, 1, result="yes", won=True, pnl=0.92)
    assert _failed(s0, bad) == ["C2 ledger field identity"]


def test_model_identities_hold_on_production_code():
    ok, detail = dg.check_c1_model_identities()
    assert ok, detail


# ── D: edge cases ─────────────────────────────────────────────────────────────

def test_edge_cases_never_raise():
    ok, detail = dg.check_d1_edge_cases()
    assert ok, detail


def test_recent_collector_hole_fails():
    s0, led = dg._fixture()
    gap = s0[:2] + [dict(s0[1], ts="2026-09-04T00:01:00+00:00")] + s0[2:]
    assert _failed(gap, led) == ["D2 collector gaps"]


# ── the design rule: recent evidence fails, history informs ───────────────────

def test_old_defects_do_not_turn_the_watchdog_permanently_red():
    """THE critical review finding: one 30h outage in August made the first D2 fail
    for the life of the file - a permanently red watchdog cannot report anything
    new. Old defects must pass the watchdog AND still be reported."""
    s0, led = dg._history_world()
    res = {name: (ok, detail) for name, ok, detail in dg.run_all(s0, led)}
    assert all(ok for ok, _ in res.values()), res
    assert "history" in res["D2 collector gaps"][1]
    assert "history" in res["B2 ledger conservation"][1]


def test_same_defect_made_recent_fails_again():
    s0, led = dg._history_world()
    hot = _edit(led, 3, pnl=0.13)                   # the recent close, one cent off
    assert _failed(s0, hot) == ["B2 ledger conservation"]


def test_offender_leads_the_detail():
    """The watchdog truncates; the first version's cut landed mid-number and
    printed a wrong value. The offending ticker must come first."""
    s0, led = dg._fixture()
    detail = {n: d for n, _, d in dg.run_all(s0, _edit(led, 1, pnl=-0.07))}
    assert "KXBTC15M-W1-T1" in detail["B2 ledger conservation"][:120]


def test_exact_unit_conversion_refuses_drift():
    assert dg._u(0.07) == 700
    assert dg._u(0.9070) == 9070            # Kalshi's sub-cent ticks stay exact
    assert dg._u(-0.08) == -800
    assert dg._u(0.080000437) is None       # off-grid: flagged, not rounded
    assert dg._u(None) is None
    assert dg._u("garbage") is None
