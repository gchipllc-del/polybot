"""Fallback Kelly sizing for binary contracts - written against THIS repo's call sites.

The contract is how polybot actually calls these functions, verified site by site
(tests/test_tradingcore_fallback.py replays every one, with the file:line it came from):

  lib/forecaster.py:558      fractional_kelly(trade_prob, trade_market_prob)
  lib/forecaster.py:559      expected_value(trade_prob, trade_market_prob, fee_rate)
  lib/forecaster.py:344      min_edge_for_trade(market_prob, fee_rate)
  agents/risk_agent.py:171   min_edge_for_trade(market_prob, fee_rate)
  lib/backtest.py:265,481    kelly_bet_size(bankroll, p, q, fraction=, max_per_market_pct=)
  lib/backtest.py:251,1026   kelly_bet_size_slippage_aware(bankroll=, our_prob=, ...) -> dict
  lib/market_scanner.py:534  kelly_bet_size_slippage_aware(... defaults) -> dict
  lib/market_scanner.py:572  ensemble_dampener(spread=, mild_threshold=, strong_threshold=, floor=)
  lib/kalshi_weather_paper.py:129  kelly_fraction(our_prob, fill)

The first version of this shim invented its own signatures and failed 6 of these sites
(4 crashes, and a SIGN-FLIPPED expected value: fee_rate was read as the payout). A
comparison against an independently rebuilt core exposed it; this file fixes it.

FEES. Callers pass fee_rate = 0.07 by default (lib/forecaster.py:771, backtest.py:288) -
Kalshi's fee coefficient: fee per contract = rate * P * (1 - P). That reading is used
here, so EV and the minimum edge agree with the exchange instead of charging 7% of the
price (which overstates a 90c favorite's fee ~10x). Continuous - the exact ceil-to-cent
fee lives in lib/binary_justify.kalshi_taker_fee for the research pipeline.
"""
from __future__ import annotations

import math


def _p(x) -> float:
    x = float(x)
    if not math.isfinite(x) or not 0.0 <= x <= 1.0:
        raise ValueError(f"probability/price must be in [0, 1], got {x!r}")
    return x


def fee_per_contract(market_prob: float, fee_rate: float = 0.0) -> float:
    """Kalshi-shaped fee: rate * P * (1 - P) on a contract bought at P."""
    q = _p(market_prob)
    return max(0.0, float(fee_rate)) * q * (1.0 - q)


def expected_value(our_prob: float, market_prob: float, fee_rate: float = 0.0) -> float:
    """EV per $1-payout contract bought at `market_prob`, after fees:
    p*(1-q) - (1-p)*q - fee  ==  p - q - fee."""
    p, q = _p(our_prob), _p(market_prob)
    return p - q - fee_per_contract(q, fee_rate)


def kelly_fraction(our_prob: float, market_prob: float) -> float:
    """Full Kelly for a binary paying 1 at price q: (p - q) / (1 - q), never negative."""
    p, q = _p(our_prob), _p(market_prob)
    if q >= 1.0:
        return 0.0
    return max(0.0, min(1.0, (p - q) / (1.0 - q)))


def fractional_kelly(our_prob: float, market_prob: float, fraction: float = 0.25) -> float:
    return kelly_fraction(our_prob, market_prob) * max(0.0, min(1.0, float(fraction)))


def kelly_bet_size(bankroll: float, our_prob: float, market_prob: float,
                   fraction: float = 0.25, max_per_market_pct: float = 0.05,
                   **_ignored) -> float:
    """Dollars to stake: fractional Kelly, capped at max_per_market_pct of bankroll."""
    b = max(0.0, float(bankroll))
    cap = b * max(0.0, min(1.0, float(max_per_market_pct)))
    return min(cap, b * fractional_kelly(our_prob, market_prob, fraction))


# Price impact per dollar staked, relative to 24h volume. A conservative HEURISTIC, not an
# exchange-calibrated impact model: CPMM pools (Manifold) move twice as fast as a CLOB.
_IMPACT_COEF = {"CLOB": 1.0, "CPMM": 2.0}
_MAX_IMPACT = 0.25


def kelly_bet_size_slippage_aware(bankroll: float, our_prob: float, market_prob: float,
                                  volume_24h: float = 0.0, market_type: str = "CLOB",
                                  fraction: float = 0.25, max_per_market_pct: float = 0.05,
                                  **_ignored) -> dict:
    """Size on the price you will actually PAY. The stake moves the price and the price
    moves the stake, so iterate to a fixed point; `converged` reports whether it did."""
    p, q = _p(our_prob), _p(market_prob)
    naive = kelly_bet_size(bankroll, p, q, fraction, max_per_market_pct)
    coef = _IMPACT_COEF.get(str(market_type).upper(), 1.0)
    vol = max(0.0, float(volume_24h or 0.0))

    def impact(bet: float) -> float:
        if bet <= 0:
            return 0.0
        if vol <= 0:
            return _MAX_IMPACT                 # unknown liquidity: assume the worst
        return min(_MAX_IMPACT, coef * bet / vol)

    bet, converged = naive, False
    for _ in range(60):
        eff = min(0.999, q + impact(bet))
        nxt = kelly_bet_size(bankroll, p, eff, fraction, max_per_market_pct)
        if abs(nxt - bet) < 1e-6:
            bet, converged = nxt, True
            break
        bet = 0.5 * (bet + nxt)                # damped: the map can oscillate
    eff = min(0.999, q + impact(bet))
    return {"bet_usd": bet, "naive_kelly_usd": naive, "effective_price": eff,
            "slippage_pct": (eff - q) / q if q > 0 else 0.0,
            "edge_post_slip": p - eff, "converged": converged}


def min_edge_for_trade(market_prob: float, fee_rate: float = 0.0) -> float:
    """The break-even edge a trade at q must clear. A trade with edge e = p - q has
    expected_value = e - fee, so it breaks even exactly at e = this value (its fee)."""
    return fee_per_contract(market_prob, fee_rate)


def ensemble_dampener(spread: float, mild_threshold: float = 0.10,
                      strong_threshold: float = 0.20, floor: float = 0.5) -> float:
    """Kelly multiplier from forecaster disagreement: 1.0 while the ensemble's spread is
    at or below `mild_threshold`, `floor` at or above `strong_threshold`, linear between."""
    s = max(0.0, float(spread))
    lo, hi = float(mild_threshold), float(strong_threshold)
    f = max(0.0, min(1.0, float(floor)))
    if s <= lo:
        return 1.0
    if hi <= lo or s >= hi:
        return f
    return 1.0 - (1.0 - f) * (s - lo) / (hi - lo)
