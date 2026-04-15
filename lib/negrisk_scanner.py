"""
NegRisk Arbitrage Scanner — guaranteed-profit strategy.

In multi-outcome markets (e.g., "Who will win the election?" with 5+ candidates),
the YES prices for all outcomes SHOULD sum to exactly $1.00.

When the sum < $1.00, buying YES on ALL outcomes guarantees profit.
When the sum > $1.00, buying NO on ALL outcomes guarantees profit.

This is the #1 strategy by ROI — practitioners report 36.2% ROI with 100% win rate.
$29M was extracted from Polymarket via NegRisk rebalancing in one year.

Works because:
- Market makers don't perfectly sync across all outcomes
- New outcomes get added (e.g., new candidate enters race)
- Large trades on one outcome don't instantly reprice all others
"""

from dataclasses import dataclass

from lib.audit import log_event


@dataclass
class NegRiskOpportunity:
    """A guaranteed-profit NegRisk arbitrage opportunity."""
    event_id: str
    event_title: str
    platform: str
    outcomes: list[dict]          # [{"market_id": str, "question": str, "yes_price": float}]
    total_yes_cost: float         # Sum of all YES prices (should be ~1.00)
    total_no_cost: float          # Sum of all NO prices
    yes_arb_profit: float         # Profit from buying all YES (per $1 payout)
    no_arb_profit: float          # Profit from buying all NO
    best_side: str                # "YES" or "NO"
    best_profit_pct: float        # Best profit percentage
    num_outcomes: int
    min_liquidity: float          # Lowest volume across all outcomes


def scan_negrisk_arb(client, min_profit_pct: float = 0.025) -> list[NegRiskOpportunity]:
    """
    Scan for NegRisk arbitrage opportunities on a platform.

    Multi-outcome markets where the sum of all YES prices != 1.00.

    Args:
        client: A MarketClient instance (typically Polymarket)
        min_profit_pct: Minimum profit after fees to consider (default 2.5%)

    Returns:
        List of NegRiskOpportunity sorted by profit potential.
    """
    opportunities = []

    try:
        # Fetch all open markets
        markets = client.get_markets(status="open", limit=200)

        # Group markets by event (multi-outcome markets share an event)
        events: dict[str, list] = {}
        for m in markets:
            # Polymarket groups multi-outcome markets by event/condition
            event_key = m.extra.get("series_ticker", "") or m.extra.get("condition_id", "")
            if not event_key:
                continue

            if event_key not in events:
                events[event_key] = []
            events[event_key].append(m)

        # Analyze each multi-outcome event
        for event_key, event_markets in events.items():
            if len(event_markets) < 3:  # Need 3+ outcomes for NegRisk
                continue

            outcomes = []
            total_yes = 0.0
            total_no = 0.0
            min_vol = float("inf")

            for m in event_markets:
                yes_price = m.yes_price
                no_price = m.no_price if m.no_price > 0 else (1.0 - yes_price)

                outcomes.append({
                    "market_id": m.market_id,
                    "question": m.question,
                    "yes_price": yes_price,
                    "no_price": no_price,
                })
                total_yes += yes_price
                total_no += no_price
                min_vol = min(min_vol, m.volume_24h)

            # Check for arbitrage
            # Buying all YES: cost = sum(yes_prices), payout = $1.00 (one must win)
            yes_arb_profit = 1.0 - total_yes  # Positive = profit
            # Buying all NO: cost = sum(no_prices), payout = $(N-1) (all but one pay out)
            no_arb_profit = (len(outcomes) - 1) - total_no  # Positive = profit

            yes_profit_pct = yes_arb_profit / total_yes if total_yes > 0 else 0
            no_profit_pct = no_arb_profit / total_no if total_no > 0 else 0

            best_side = "YES" if yes_profit_pct > no_profit_pct else "NO"
            best_profit_pct = max(yes_profit_pct, no_profit_pct)

            # Account for platform fees
            fee_adjusted_profit = best_profit_pct - client.fee_rate
            if fee_adjusted_profit < min_profit_pct:
                continue

            opp = NegRiskOpportunity(
                event_id=event_key,
                event_title=event_markets[0].question[:80],
                platform=client.platform_name,
                outcomes=outcomes,
                total_yes_cost=total_yes,
                total_no_cost=total_no,
                yes_arb_profit=yes_arb_profit,
                no_arb_profit=no_arb_profit,
                best_side=best_side,
                best_profit_pct=fee_adjusted_profit,
                num_outcomes=len(outcomes),
                min_liquidity=min_vol,
            )
            opportunities.append(opp)

        # Sort by profit potential
        opportunities.sort(key=lambda x: x.best_profit_pct, reverse=True)

        log_event("arb_scanner", "negrisk_scan_complete", {
            "events_scanned": len(events),
            "opportunities_found": len(opportunities),
            "best_profit_pct": opportunities[0].best_profit_pct if opportunities else 0,
        }, result="success")

    except Exception as e:
        log_event("arb_scanner", "negrisk_scan_failed", {
            "error": str(e),
        }, result="failed")

    return opportunities


def scan_near_resolution(client, max_hours: int = 24, min_price: float = 0.92) -> list[dict]:
    """
    Near-Resolution Harvesting — find markets about to resolve with near-certain outcomes.

    Markets priced at 92c+ with < 24h to resolution are almost certainly going to resolve YES.
    Buy at 92-98c, collect $1.00 at resolution. Small edge but nearly guaranteed.

    Practitioners report 5.8% ROI with 100% win rate on this strategy.

    Args:
        client: MarketClient instance
        max_hours: Maximum hours until resolution
        min_price: Minimum YES price to consider (higher = more certain)

    Returns:
        List of near-resolution opportunities.
    """
    from datetime import datetime, timezone, timedelta

    opportunities = []
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=max_hours)

    try:
        markets = client.get_markets(status="open", limit=200)

        for m in markets:
            # Parse resolution date
            if not m.resolution_date:
                continue

            try:
                if isinstance(m.resolution_date, str):
                    # Handle various date formats
                    res_date = datetime.fromisoformat(m.resolution_date.replace("Z", "+00:00"))
                else:
                    res_date = datetime.fromtimestamp(m.resolution_date / 1000, tz=timezone.utc)
            except (ValueError, TypeError):
                continue

            if res_date > cutoff or res_date < now:
                continue

            hours_left = (res_date - now).total_seconds() / 3600

            # Check for near-certain YES
            if m.yes_price >= min_price:
                profit_per_contract = 1.0 - m.yes_price
                profit_pct = profit_per_contract / m.yes_price
                fee_adjusted = profit_pct - client.fee_rate

                if fee_adjusted > 0.005:  # At least 0.5% after fees
                    opportunities.append({
                        "market_id": m.market_id,
                        "platform": m.platform,
                        "question": m.question,
                        "side": "YES",
                        "price": m.yes_price,
                        "hours_to_resolution": round(hours_left, 1),
                        "profit_per_contract": profit_per_contract,
                        "profit_pct": round(fee_adjusted * 100, 2),
                        "volume_24h": m.volume_24h,
                    })

            # Check for near-certain NO
            if m.no_price >= min_price:
                profit_per_contract = 1.0 - m.no_price
                profit_pct = profit_per_contract / m.no_price
                fee_adjusted = profit_pct - client.fee_rate

                if fee_adjusted > 0.005:
                    opportunities.append({
                        "market_id": m.market_id,
                        "platform": m.platform,
                        "question": m.question,
                        "side": "NO",
                        "price": m.no_price,
                        "hours_to_resolution": round(hours_left, 1),
                        "profit_per_contract": profit_per_contract,
                        "profit_pct": round(fee_adjusted * 100, 2),
                        "volume_24h": m.volume_24h,
                    })

        opportunities.sort(key=lambda x: x["profit_pct"], reverse=True)

        log_event("arb_scanner", "near_resolution_scan_complete", {
            "markets_scanned": len(markets),
            "opportunities_found": len(opportunities),
        }, result="success")

    except Exception as e:
        log_event("arb_scanner", "near_resolution_scan_failed", {
            "error": str(e),
        }, result="failed")

    return opportunities
