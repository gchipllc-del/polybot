"""diagnostics - each detector must fire on its planted corruption, and ONLY it.

A validation layer that has never been seen to catch its target is decoration. These
tests plant one specific defect per check against a clean fixture and assert the
right alarm - and no other - goes off. Mirrors scripts/diagnostics.py selftest so the
suite and the on-host smoke test can never drift apart.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import diagnostics as dg  # noqa: E402


def _clean():
    return dg._fixture()


def _failed(results):
    return [name for name, ok, _ in results if not ok]


def test_clean_fixture_passes_everything():
    s0, led = _clean()
    assert _failed(dg.run_all(s0, led)) == []


def test_negative_spot_trips_input_validation():
    s0, led = _clean()
    bad = [dict(s0[0], spot=-5.0)] + s0[1:]
    assert _failed(dg.run_all(bad, led)) == ["A1 obs bounds"]


def test_crossed_book_counts_as_bad_data():
    s0, led = _clean()
    bad = [dict(s0[0], yes_ask=0.30, no_ask=0.30)] + s0[1:]   # ya+na = 0.60: crossed
    assert _failed(dg.run_all(bad, led)) == ["A1 obs bounds"]


def test_contradictory_settlement_is_caught():
    s0, led = _clean()
    bad = s0 + [{"t": "settle", "ticker": "KXBTC15M-W1-T1", "result": "yes"}]
    assert _failed(dg.run_all(bad, led)) == ["A2 settle consistency"]


def test_orphan_close_breaks_structure():
    s0, led = _clean()
    bad = led + [{"t": "close", "ticker": "GHOST", "rule": "R", "side": "yes",
                  "price": 0.5, "result": "yes", "won": True, "pnl": 0.48}]
    assert _failed(dg.run_all(s0, bad)) == ["B1 ledger structure"]


def test_one_stolen_cent_breaks_conservation():
    s0, led = _clean()
    bad = [dict(r) for r in led]
    bad[1]["pnl"] = -0.07                       # truth is -0.08
    assert _failed(dg.run_all(s0, bad)) == ["B2 ledger conservation"]


def test_off_grid_pnl_is_flagged_not_rounded_away():
    """The exact-arithmetic principle: a value off the $0.0001 grid is itself the
    defect (float drift crept into the ledger), never something to round past."""
    s0, led = _clean()
    bad = [dict(r) for r in led]
    bad[1]["pnl"] = -0.080000437
    assert _failed(dg.run_all(s0, bad)) == ["B2 ledger conservation"]


def test_won_flag_contradiction_fires_identity_and_conservation():
    s0, led = _clean()
    bad = [dict(r) for r in led]
    bad[3]["won"] = False                       # side=no, result=no -> won must be True
    assert set(_failed(dg.run_all(s0, bad))) == {"B2 ledger conservation",
                                                 "C2 ledger field identity"}


def test_collector_outage_gap_surfaces():
    s0, led = _clean()
    gap = s0[:2] + [dict(s0[1], ts="2026-09-04T00:01:00+00:00")] + s0[2:]
    assert _failed(dg.run_all(gap, led)) == ["D2 collector gaps"]


def test_model_identities_hold_on_production_code():
    ok, detail = dg.check_c1_model_identities()
    assert ok, detail


def test_edge_cases_never_raise():
    ok, detail = dg.check_d1_edge_cases()
    assert ok, detail


def test_exact_unit_conversion_refuses_drift():
    assert dg._u(0.07) == 700
    assert dg._u(0.9070) == 9070            # Kalshi's sub-cent ticks stay exact
    assert dg._u(-0.08) == -800
    assert dg._u(0.080000437) is None       # off-grid: flagged, not rounded
    assert dg._u(None) is None
