"""
Kelly Criterion — optimal position sizing for binary outcomes.

In prediction markets, contracts pay $1.00 if correct, $0.00 if wrong.
The Kelly fraction tells us the mathematically optimal bet size to maximize
long-term compound growth rate.

Full Kelly is too aggressive — always use fractional Kelly (0.25 to 0.75).
"""

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent.parent / "config" / "strategy.yaml"


def _load_strategy() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def kelly_fraction(our_prob: float, market_prob: float) -> float:
    """
    Calculate the full Kelly fraction for a binary prediction market bet.

    For a YES bet at price `market_prob`:
      - Cost per contract: market_prob
      - Payout if correct: 1.00
      - Net profit if correct: 1.00 - market_prob
      - Net odds (b): (1.00 - market_prob) / market_prob
      - Kelly: (p * b - q) / b  where p = our_prob, q = 1 - our_prob

    Args:
        our_prob: Our estimated probability of YES (0.0 to 1.0)
        market_prob: Current market price / implied probability (0.0 to 1.0)

    Returns:
        Full Kelly fraction (can be negative = don't bet, or > 1.0 = very strong edge).
        Negative means the market has the edge, not us.
    """
    if market_prob <= 0 or market_prob >= 1:
        return 0.0
    if our_prob <= 0 or our_prob >= 1:
        return 0.0

    b = (1.0 - market_prob) / market_prob  # net odds
    p = our_prob
    q = 1.0 - p

    f = (p * b - q) / b
    return f


def fractional_kelly(
    our_prob: float,
    market_prob: float,
    fraction: float | None = None,
) -> float:
    """
    Fractional Kelly — multiply full Kelly by a safety factor.

    Quarter Kelly (0.25) is the conservative start for a $50 bankroll.
    Hermes can tune this up to 0.75 as calibration improves.

    Args:
        our_prob: Our estimated probability
        market_prob: Market price
        fraction: Kelly fraction multiplier (default from strategy.yaml)

    Returns:
        Fractional Kelly bet size as fraction of bankroll (0.0 to 1.0, clamped).
    """
    if fraction is None:
        strategy = _load_strategy()
        fraction = strategy.get("kelly_multiplier", 0.25)

    full_k = kelly_fraction(our_prob, market_prob)

    if full_k <= 0:
        return 0.0

    return min(full_k * fraction, 1.0)


def kelly_bet_size(
    bankroll: float,
    our_prob: float,
    market_prob: float,
    fraction: float | None = None,
    max_per_market_pct: float | None = None,
) -> float:
    """
    Calculate the dollar amount to bet on a single market.

    Clamps by:
    1. Fractional Kelly sizing
    2. Circuit breaker max_per_market_pct
    3. Never bet more than bankroll

    Args:
        bankroll: Current total bankroll in dollars
        our_prob: Our probability estimate
        market_prob: Market price
        fraction: Kelly multiplier (default from config)
        max_per_market_pct: Max fraction of bankroll per market (default from config)

    Returns:
        Dollar amount to bet (0.0 if no edge).
    """
    if bankroll <= 0:
        return 0.0

    if max_per_market_pct is None:
        strategy = _load_strategy()
        max_per_market_pct = strategy.get("max_per_market_pct", 0.15)

    frac_k = fractional_kelly(our_prob, market_prob, fraction)
    if frac_k <= 0:
        return 0.0

    kelly_amount = bankroll * frac_k
    max_amount = bankroll * max_per_market_pct

    return min(kelly_amount, max_amount, bankroll)


def min_edge_for_trade(market_prob: float, fee_rate: float = 0.07) -> float:
    """
    Calculate the minimum edge needed to overcome fees and have positive EV.

    On Kalshi, fee is 7% of profit (only on wins).
    On Polymarket, fee is ~2% of winnings.

    The edge must exceed: fee_rate * expected_profit_per_dollar

    For simplicity, we require our edge to exceed the fee-adjusted break-even.

    Args:
        market_prob: Current market price
        fee_rate: Platform fee rate on profit (0.07 for Kalshi, 0.02 for Polymarket)

    Returns:
        Minimum edge (probability points) needed for a profitable trade.
    """
    if market_prob <= 0 or market_prob >= 1:
        return 1.0  # Can't trade at boundary prices

    # Net odds after fees
    gross_profit = 1.0 - market_prob
    net_profit = gross_profit * (1.0 - fee_rate)

    # Break-even probability: we need to win often enough to cover cost
    # Cost per contract: market_prob
    # Net payout per win: market_prob + net_profit = market_prob + (1 - market_prob)(1 - fee_rate)
    # Break-even: p_break * net_payout = market_prob (cost)
    net_payout = market_prob + net_profit
    break_even_prob = market_prob / net_payout if net_payout > 0 else 1.0

    # Minimum edge = break-even probability - market probability
    min_edge = break_even_prob - market_prob

    # Add a small buffer (2%) for estimation uncertainty
    return min_edge + 0.02


def expected_value(our_prob: float, market_prob: float, fee_rate: float = 0.07) -> float:
    """
    Calculate expected value per dollar bet.

    Args:
        our_prob: Our probability estimate
        market_prob: Market price (cost per contract)
        fee_rate: Platform fee rate on profit

    Returns:
        Expected value per dollar. Positive = profitable.
    """
    if market_prob <= 0 or market_prob >= 1:
        return 0.0

    gross_profit = 1.0 - market_prob
    net_profit = gross_profit * (1.0 - fee_rate)

    # EV = p(win) * net_profit - p(loss) * cost
    ev = our_prob * net_profit - (1.0 - our_prob) * market_prob

    return ev
