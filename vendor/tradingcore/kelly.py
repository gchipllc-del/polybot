"""Fallback Kelly sizing — standard formulas, conservative where ambiguous."""
from __future__ import annotations


def expected_value(p_win: float, price: float, payout: float = 1.0) -> float:
    """EV per contract of a binary bought at `price` that pays `payout` on win."""
    return p_win * (payout - price) - (1.0 - p_win) * price


def kelly_fraction(p_win: float, price: float, payout: float = 1.0) -> float:
    """Kelly fraction for a binary. b = net odds = (payout-price)/price."""
    if price <= 0 or price >= payout:
        return 0.0
    b = (payout - price) / price
    f = (p_win * (b + 1.0) - 1.0) / b
    return max(0.0, min(1.0, f))


def fractional_kelly(p_win: float, price: float, fraction: float = 0.25,
                     payout: float = 1.0) -> float:
    return kelly_fraction(p_win, price, payout) * max(0.0, fraction)


def kelly_bet_size(p_win: float, price: float, bankroll: float,
                   fraction: float = 0.25, payout: float = 1.0) -> float:
    return max(0.0, bankroll * fractional_kelly(p_win, price, fraction, payout))


def kelly_bet_size_slippage_aware(p_win: float, price: float, bankroll: float,
                                  fraction: float = 0.25, slippage: float = 0.01,
                                  payout: float = 1.0) -> float:
    """Size on the price you expect to actually PAY, not the quote."""
    return kelly_bet_size(p_win, min(price + max(0.0, slippage), payout * 0.999),
                          bankroll, fraction, payout)


def min_edge_for_trade(price: float, fee: float = 0.0, spread: float = 0.0,
                       margin: float = 0.0) -> float:
    """Minimum probability edge that clears friction."""
    return abs(fee) + abs(spread) / 2.0 + abs(margin)


def ensemble_dampener(n_sources: int, agreement: float = 1.0) -> float:
    """Shrink sizing when few sources agree. 1 source -> 0.5; many agreeing -> ~1."""
    if n_sources <= 0:
        return 0.0
    base = 1.0 - 1.0 / (1.0 + float(n_sources))
    return max(0.0, min(1.0, base * max(0.0, min(1.0, agreement))))
