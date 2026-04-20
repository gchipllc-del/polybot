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


# ── Slippage-Aware Sizing ─────────────────────────────────────────
# A naive Kelly bet in a thin orderbook pays the last traded price only
# on the first contract; the rest are filled at progressively worse
# prices. This can eat 50%+ of a narrow edge instantly. The functions
# below model price impact and solve for bet size that *accounts for
# its own impact* — so the "effective price" after filling is still
# profitable.


def estimate_slippage(
    bet_usd: float,
    market_prob: float,
    volume_24h: float,
    market_type: str = "CPMM",
) -> float:
    """
    Estimate the effective fill price for a bet of size `bet_usd`.

    Two simple impact models:
      - CPMM (Manifold):     sqrt(bet / pool_proxy) — convex impact
      - CLOB (Kalshi/Poly):  linear in depth — top-of-book walk

    Both clamped so a tiny bet in a thin market still executes, and a
    huge bet can't produce a >15% impact (we'd refuse to trade it well
    before then via circuit breakers).

    Args:
        bet_usd: Dollar size of the proposed bet
        market_prob: Current mid-price (0.0-1.0)
        volume_24h: Recent daily volume in USD (proxy for book depth)
        market_type: "CPMM" or "CLOB"

    Returns:
        Effective fill price (0.0-1.0). Equal to market_prob for zero-size bet.
    """
    if bet_usd <= 0:
        return market_prob
    if market_prob <= 0 or market_prob >= 1:
        return market_prob

    # Volume as proxy for liquidity — floor at $50 to avoid divide-by-near-zero
    depth = max(volume_24h, 50.0)

    if market_type.upper() == "CPMM":
        # CPMM pool depth estimated as ~5× daily volume (Manifold rough proxy)
        pool = depth * 5.0
        # Price impact ≈ bet / (bet + 2*pool)
        impact = bet_usd / (bet_usd + 2.0 * pool)
    else:
        # CLOB: assume top-of-book holds 10% of daily volume; then linear decay
        top_book = depth * 0.10
        if bet_usd <= top_book:
            impact = 0.5 * (bet_usd / top_book) * 0.01  # Up to 0.5% impact for top-of-book fills
        else:
            remainder = bet_usd - top_book
            # Each additional $1 of volume in excess walks price 0.5% per top-book
            impact = 0.005 + (remainder / top_book) * 0.01

    # Cap impact — anything above 15% means we're clearly too big; break circuit
    impact = max(0.0, min(impact, 0.15))

    # For YES side, effective price = market + impact (pay more)
    # For NO side, effective price of the NO contract = (1 - market) + impact (same direction)
    # We model market_prob as the side being bought, so always additive.
    return min(0.99, market_prob + impact)


def kelly_bet_size_slippage_aware(
    bankroll: float,
    our_prob: float,
    market_prob: float,
    volume_24h: float = 1000.0,
    market_type: str = "CPMM",
    fraction: float | None = None,
    max_per_market_pct: float | None = None,
    max_iterations: int = 6,
    tolerance_usd: float = 0.25,
) -> dict:
    """
    Solve for a Kelly bet size that accounts for its own price impact.

    Iterates: propose bet → compute effective price → recompute edge →
    re-size Kelly. Converges when bet size changes < `tolerance_usd`.

    The returned dict exposes the convergence so the scanner/UI can show
    slippage-adjusted numbers rather than the naive top-of-book figures.

    Args:
        bankroll: Dollars available
        our_prob: Our probability estimate (for the side we're buying)
        market_prob: Top-of-book market price (for the side we're buying)
        volume_24h: Recent daily volume (depth proxy)
        market_type: "CPMM" (Manifold) or "CLOB" (Kalshi/Polymarket)
        fraction, max_per_market_pct: as in kelly_bet_size()
        max_iterations: Fixed-point iteration cap
        tolerance_usd: Convergence threshold

    Returns:
        {
          "bet_usd":          final dollar bet size,
          "effective_price":  price we'd actually fill at,
          "slippage_pct":     (effective - market) / market,
          "naive_kelly_usd":  what naive Kelly would have said,
          "edge_post_slip":   edge after accounting for slippage,
          "converged":        bool,
          "iterations":       int,
        }
    """
    if bankroll <= 0 or market_prob <= 0 or market_prob >= 1:
        return {"bet_usd": 0.0, "effective_price": market_prob, "slippage_pct": 0.0,
                "naive_kelly_usd": 0.0, "edge_post_slip": 0.0,
                "converged": True, "iterations": 0}

    naive_bet = kelly_bet_size(bankroll, our_prob, market_prob,
                                fraction=fraction, max_per_market_pct=max_per_market_pct)
    if naive_bet <= 0:
        return {"bet_usd": 0.0, "effective_price": market_prob, "slippage_pct": 0.0,
                "naive_kelly_usd": 0.0, "edge_post_slip": our_prob - market_prob,
                "converged": True, "iterations": 0}

    bet = naive_bet
    converged = False
    effective_price = market_prob

    for i in range(max_iterations):
        effective_price = estimate_slippage(bet, market_prob, volume_24h, market_type)
        new_bet = kelly_bet_size(bankroll, our_prob, effective_price,
                                  fraction=fraction, max_per_market_pct=max_per_market_pct)
        if new_bet <= 0:
            bet = 0.0
            effective_price = market_prob
            converged = True
            break
        if abs(new_bet - bet) < tolerance_usd:
            bet = new_bet
            converged = True
            break
        bet = new_bet
    else:
        # No convergence — be conservative, scale down 20%
        bet = bet * 0.80

    effective_price = estimate_slippage(bet, market_prob, volume_24h, market_type)
    slippage_pct = (effective_price - market_prob) / market_prob if market_prob > 0 else 0.0
    edge_post_slip = our_prob - effective_price

    return {
        "bet_usd": round(bet, 2),
        "effective_price": round(effective_price, 4),
        "slippage_pct": round(slippage_pct, 4),
        "naive_kelly_usd": round(naive_bet, 2),
        "edge_post_slip": round(edge_post_slip, 4),
        "converged": converged,
        "iterations": i + 1 if converged else max_iterations,
    }
