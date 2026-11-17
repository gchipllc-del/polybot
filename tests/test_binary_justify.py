"""Tests for lib/binary_justify.py — the YES/NO justification engine (restart foundation).

Locks: fair-value math (incl. fat tails), friction rounding, every gate's blocking
behavior, side-neutral verdict selection, and the ledger -> calibration loop where the
model must BEAT price-as-forecast to keep trading.
"""
import math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.binary_justify import (
    ewma_sigma, fair_p_above, kalshi_taker_fee, friction_per_contract,
    justify, record, resolve, calibration_stats, _t_sf,
)


# ── measurement math ─────────────────────────────────────────────────────────

def test_ewma_sigma_constant_returns():
    # constant |r| -> sigma == |r| regardless of lambda
    assert abs(ewma_sigma([0.001] * 50) - 0.001) < 1e-9


def test_ewma_sigma_weights_recent():
    calm_then_wild = [0.0001] * 40 + [0.01] * 10
    wild_then_calm = [0.01] * 10 + [0.0001] * 40
    # lambda=0.94 halves in ~11 bars: recent-wild must read clearly hotter (2.3x here)
    assert ewma_sigma(calm_then_wild) > 2 * ewma_sigma(wild_then_calm)


def test_t_sf_matches_table():
    # one-tailed t critical value: P(T_4 > 2.132) = 0.05
    assert abs(_t_sf(2.132, 4) - 0.05) < 0.002


def test_fair_p_above_atm_is_half():
    # numeric t-integration is good to ~2e-5 — far below anything the gates act on
    p = fair_p_above(64000, 64000, 0.0017, 1.0)
    assert abs(p - 0.5) < 1e-3


def test_fair_p_above_monotone_in_strike():
    ps = [fair_p_above(64000, 64000 * (1 + b / 1e4), 0.0017, 1.0) for b in (0, 5, 25, 50)]
    assert ps == sorted(ps, reverse=True)
    assert ps[2] < 0.15                      # 25bps away on 17bps sigma: small
    assert ps[3] < 0.05                      # 50bps away: tiny


def test_fat_tails_beat_normal_far_out():
    # Student-t must assign MORE probability than normal to far strikes (crypto jumps).
    far = 64000 * 1.01                       # 100bps away
    p_t = fair_p_above(64000, far, 0.0017, 1.0, nu=4.0)
    p_n = fair_p_above(64000, far, 0.0017, 1.0, nu=None)
    assert p_t > p_n


def test_fair_p_degenerate_inputs():
    assert fair_p_above(64000, 63000, 0.0, 1.0) == 1.0    # no vol, spot above strike
    assert fair_p_above(64000, 65000, 0.0017, 0.0) == 0.0  # no time, spot below strike


# ── friction ─────────────────────────────────────────────────────────────────

def test_kalshi_fee_ceils_to_cent():
    assert kalshi_taker_fee(0.03) == 0.01     # 0.07*0.03*0.97=0.20c -> ceil 1c (the 33% tax)
    assert kalshi_taker_fee(0.50) == 0.02     # 1.75c -> 2c
    assert kalshi_taker_fee(0.90) == 0.01     # 0.63c -> 1c


def test_friction_includes_half_spread():
    assert friction_per_contract(0.50, spread=0.02) == kalshi_taker_fee(0.50) + 0.01


# ── the gates ────────────────────────────────────────────────────────────────

RETS = [0.0005 if i % 2 else -0.0005 for i in range(50)]   # sigma = 5bps/bar, n=50


def _base(**over):
    kw = dict(market_id="TEST-1", spot=64000.0, strike=64000 * 1.0015,  # 15bps away
              minutes_left=15.0, returns=RETS, bar_minutes=5.0,
              yes_price=0.30, no_price=0.70, data_age_s=10.0,
              config={"mechanism": "longshot_premium"})
    kw.update(over)
    return kw


def test_buy_no_when_yes_overpriced():
    # sigma_T = 5bps*sqrt(3) ~ 8.7bps; strike 15bps away -> p_fair(YES) small (<0.15).
    # YES asked at 0.30 is way over fair; NO at 0.70 is under its fair (>0.85).
    j = justify(**_base())
    assert j.p_fair < 0.15
    assert j.verdict == "BUY_NO"
    assert j.side_edge_net > 0.03
    assert all(g.passed for g in j.gates)


def test_pass_when_prices_fair():
    # price both sides at fair value -> no edge -> PASS via G2
    j0 = justify(**_base())
    j = justify(**_base(yes_price=round(j0.p_fair, 2), no_price=round(1 - j0.p_fair, 2)))
    assert j.verdict == "PASS"
    assert not [g for g in j.gates if g.name == "G2_edge"][0].passed


def test_g1_blocks_thin_sample():
    j = justify(**_base(returns=RETS[:10]))
    assert j.verdict == "PASS"
    assert not [g for g in j.gates if g.name == "G1_measurement"][0].passed


def test_g1_blocks_stale_data():
    j = justify(**_base(data_age_s=999.0))
    assert j.verdict == "PASS"


def test_g3_blocks_unnamed_mechanism():
    j = justify(**_base(config={}))          # no mechanism named
    assert j.verdict == "PASS"
    assert not [g for g in j.gates if g.name == "G3_mechanism"][0].passed


def test_g5_blocks_blind_zone():
    # ATM strike: dist_z ~ 0 -> inside the measurement's own error band -> refuse,
    # even though a mispriced side might show raw edge. (Weather blind-zone lesson.)
    j = justify(**_base(strike=64000.0, yes_price=0.20, no_price=0.80))
    assert not [g for g in j.gates if g.name == "G5_decisive"][0].passed
    assert j.verdict == "PASS"


def test_g4_provisional_below_min_n():
    j = justify(**_base(calib_stats={"n": 5, "brier_model": 0.9, "brier_price": 0.1}))
    g4 = [g for g in j.gates if g.name == "G4_calibration"][0]
    assert g4.passed and "provisional" in g4.note


def test_g4_blocks_when_model_loses_to_price():
    cs = {"n": 500, "brier_model": 0.30, "brier_price": 0.20}   # market beats us -> stop
    j = justify(**_base(calib_stats=cs))
    assert j.verdict == "PASS"
    assert not [g for g in j.gates if g.name == "G4_calibration"][0].passed


def test_side_neutral_prefers_larger_net_edge():
    # underpriced YES: p_fair small but yes even smaller? make YES the value side:
    # strike BELOW spot -> p_fair(YES) high; YES asked cheap.
    j = justify(**_base(strike=64000 * 0.9985, yes_price=0.60, no_price=0.40))
    assert j.p_fair > 0.85
    assert j.verdict == "BUY_YES"


def test_g6_blocks_stale_quote():
    # spot drifted 20bps since the quote; sigma_T ~ 8.7bps -> drift_z ~ 2.3 >> 0.25 cap.
    j = justify(**_base(spot_at_quote=64000.0, spot=64000 * 1.002))
    assert not [g for g in j.gates if g.name == "G6_staleness"][0].passed
    assert j.verdict == "PASS"


def test_g6_passes_fresh_quote():
    j = justify(**_base(spot_at_quote=64000.0))            # no drift
    assert [g for g in j.gates if g.name == "G6_staleness"][0].passed


def test_g6_untracked_passes_with_note():
    j = justify(**_base())                                  # spot_at_quote not supplied
    g6 = [g for g in j.gates if g.name == "G6_staleness"][0]
    assert g6.passed and "did not supply" in g6.note


def test_g7_blocks_event_blackout():
    j = justify(**_base(event_blackout=True))
    assert not [g for g in j.gates if g.name == "G7_event_blackout"][0].passed
    assert j.verdict == "PASS"


# ── ledger + calibration loop ────────────────────────────────────────────────

def test_ledger_roundtrip_and_calibration(tmp_path):
    lp = tmp_path / "ledger.jsonl"
    # model says 0.9 for A (happens), 0.1 for B (doesn't) — perfect; price said 0.5 both.
    for mid, pf, yp in (("A", 0.9, 0.5), ("B", 0.1, 0.5)):
        j = justify(**_base(market_id=mid))
        j.p_fair, j.yes_price = pf, yp       # inject knowns for the calibration join
        record(j, path=lp)
    resolve("A", ts=1.0, outcome_yes=True, path=lp)
    resolve("B", ts=1.0, outcome_yes=False, path=lp)
    cs = calibration_stats(path=lp)
    assert cs["n"] == 2
    assert cs["brier_model"] < cs["brier_price"]      # model beat the price
    assert cs["skill_vs_price"] > 0


def test_calibration_empty_ledger(tmp_path):
    assert calibration_stats(path=tmp_path / "none.jsonl") == {"n": 0}
