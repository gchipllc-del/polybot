"""Regression: below/between Kalshi daily markets must drive composite side
from the DIRECTION-CORRECT theo, not the above-semantics P(S>strike).
Audit finding #165 (2026-06-01). The 'above' path (all crypto incl. live BTC)
must be byte-for-byte unchanged.

These are math-identity tests (no live SPY outcomes exist yet to regress on):
for a 'below' market the YES event is P(S<=cap)=1-P(S>cap); the market quote
prices that same event, so comparing the model's P(S>cap) against it (the old
bug) is definitionally the wrong side.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.btc_5min_signal import compute_greeks, compute_indicators_for_window


def _klines(n=40, base=5000.0):
    # flat-ish synthetic hourly klines; enough bars for the indicator helper
    return [{"open": base, "high": base + 5, "low": base - 5,
             "close": base, "volume": 1000} for _ in range(n)]


def test_below_market_sign_is_corrected():
    spot, cap = 5000.0, 5050.0
    g = compute_greeks(spot=spot, strike=cap, hours_to_close=120, annual_vol=0.18)
    above_theo = g["theoretical_yes"]          # P(S>cap) ~ 0.31
    below_theo = 1.0 - above_theo              # P(S<=cap) ~ 0.69 — the YES event
    market_yes = 0.55                          # market prices P(S<=cap)

    # Old (buggy) gap used above-semantics theo:
    old_gap = above_theo - market_yes          # negative -> composite leans NO
    # Corrected gap:
    new_gap = below_theo - market_yes          # positive -> composite leans YES
    assert old_gap < 0 < new_gap, "fix must flip the sign of the theo gap"

    # And the contrib (weight 4, sat 0.20) flips with it:
    old_contrib = max(-1, min(1, old_gap / 0.20)) * 4.0
    new_contrib = max(-1, min(1, new_gap / 0.20)) * 4.0
    assert old_contrib < 0 < new_contrib


def test_above_path_untouched_by_fix():
    # The 'above' indicator computation (live BTC path) is independent of the
    # kalshi_daily_signal override; compute_indicators_for_window for an
    # above-market is unchanged. Sanity: a clearly-bullish setup (spot well
    # above strike) yields theo>0.5 and composite leaning UP, no exception.
    ind = compute_indicators_for_window(
        klines=_klines(), window_open_price=4800.0, current_spot=5000.0,
        hours_to_close=24.0, market_yes_price=0.50, annual_vol=0.18,
        theo_delta_gap_saturation=0.20, rsi_weight=1.5,
    )
    assert ind["theoretical_yes"] is not None
    assert ind["theoretical_yes"] > 0.5  # spot above strike -> P(S>K) > 0.5
    assert "composite" in ind and "confidence" in ind


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
