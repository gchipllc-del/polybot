"""Regression: calibration must only record direction-correct (above-semantics)
markets. theoretical_yes_raw is always P(S>strike), so feeding below/between
(KXINX SPY) markets teaches a backwards mapping. Audit cal-safety #1, 2026-06-01.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lib.kalshi_daily_paper as kp


def test_crypto_above_markets_are_above():
    assert kp._kalshi_ticker_is_above("KXBTCD-26JUN0117-T73249.99") is True
    assert kp._kalshi_ticker_is_above("KXETHD-26JUN0117-T3400") is True
    assert kp._kalshi_ticker_is_above("KXSOLD-26JUN0117-T180") is True


def test_index_between_and_below_are_not_above():
    # KXINX between (B-prefix) — SPY
    assert kp._kalshi_ticker_is_above("KXINX-26MAY28H1600-B7512") is False
    # KXINX threshold (T-prefix) is a BELOW market for the index series
    assert kp._kalshi_ticker_is_above("KXINX-26JUN01H1600-T7500") is False


def test_unknown_defaults_to_above_for_btc_safety():
    # Legacy/unknown shape -> assume crypto above so BTC behavior is unchanged
    assert kp._kalshi_ticker_is_above("WEIRDTICKER") is True
    assert kp._kalshi_ticker_is_above("") is True


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
