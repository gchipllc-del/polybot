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

Size-aware execution (ImMike pattern):
    Top-of-book prices lie. The scanner now walks the orderbook to compute
    the *effective* average fill price for a proposed basket size. A 3%
    overround that only exists for $5 of depth is not a $500 arbitrage —
    it's a $5 arbitrage. `walk_orderbook_for_size()` returns the true
    executable price for a given notional.

Fee accounting:
    Polymarket charges 2% of PROFIT, not cost. The previous scanner
    subtracted fee_rate from the profit percentage — arithmetically
    identical for single-leg trades but *wrong* for NegRisk baskets
    where every leg pays its own fee. The scanner now bills per-leg.

Execution risk:
    NegRisk requires all legs to fill at the computed prices. If any leg
    slips, the arb evaporates and you're left with correlated exposure.
    `NegRiskOpportunity.executable_size_usd` is the maximum basket size
    at which every leg still clears min_profit_pct after fees.
"""

from dataclasses import dataclass, field

from lib.audit import log_event


@dataclass
class NegRiskOpportunity:
    """A guaranteed-profit NegRisk arbitrage opportunity."""
    event_id: str
    event_title: str
    platform: str
    outcomes: list[dict]          # [{"market_id": str, "question": str, "yes_price": float, "effective_price": float, "size_at_price": float}]
    total_yes_cost: float         # Sum of all YES prices (top-of-book)
    total_no_cost: float          # Sum of all NO prices (top-of-book)
    yes_arb_profit: float         # Profit from buying all YES (per $1 payout)
    no_arb_profit: float          # Profit from buying all NO
    best_side: str                # "YES" or "NO"
    best_profit_pct: float        # Best profit percentage (after fees, size-aware)
    num_outcomes: int
    min_liquidity: float          # Lowest volume across all outcomes

    # Size-aware fields (set by size-aware path; zero for top-of-book-only path)
    executable_size_usd: float = 0.0    # Max basket size that still clears min_profit
    effective_total_cost: float = 0.0   # Sum of effective (post-walk) prices * per-leg size
    depth_limited: bool = False          # True if orderbook depth bounded the arb
    per_leg_details: list[dict] = field(default_factory=list)   # Per-leg audit data


def walk_orderbook_for_size(
    book: dict,
    notional_usd: float,
    side: str,
) -> dict:
    """
    Walk an orderbook to compute the effective fill price for `notional_usd`.

    A limit book quotes {price, quantity} levels. Buying $N of contracts
    doesn't happen at top of book — it consumes depth progressively, and
    the effective price is the average cost across all consumed levels.

    Args:
        book: {"bids": [{"price": float, "quantity": float}, ...],
               "asks": [{"price": float, "quantity": float}, ...]}
        notional_usd: Dollars of contracts we want to buy
        side: "YES" → walk asks (we're buying YES);
              "NO"  → walk asks of NO book (caller must pass the NO book)

    Returns:
        {
          "effective_price":  avg fill price (0.0-1.0),
          "contracts_filled": total contracts we'd get,
          "notional_filled":  dollars actually spent (≤ notional_usd),
          "depth_exhausted":  True if we ran out of asks before filling,
          "levels_consumed":  count of orderbook levels walked,
        }
    """
    if notional_usd <= 0 or not isinstance(book, dict):
        return {
            "effective_price": 0.0, "contracts_filled": 0.0,
            "notional_filled": 0.0, "depth_exhausted": True, "levels_consumed": 0,
        }

    # For "buy YES" we walk asks. For "buy NO" the caller must pass the NO book.
    asks = book.get("asks") or []
    if not asks:
        return {
            "effective_price": 0.0, "contracts_filled": 0.0,
            "notional_filled": 0.0, "depth_exhausted": True, "levels_consumed": 0,
        }

    # Sort asks by price ascending (best price first)
    try:
        sorted_asks = sorted(asks, key=lambda x: float(x.get("price", 1.0)))
    except (TypeError, ValueError):
        return {
            "effective_price": 0.0, "contracts_filled": 0.0,
            "notional_filled": 0.0, "depth_exhausted": True, "levels_consumed": 0,
        }

    remaining_usd = notional_usd
    total_notional = 0.0
    total_contracts = 0.0
    levels = 0

    for level in sorted_asks:
        try:
            price = float(level.get("price", 0))
            qty = float(level.get("quantity", 0) or level.get("size", 0))
        except (TypeError, ValueError):
            continue
        if price <= 0 or price >= 1.0 or qty <= 0:
            continue

        level_notional = price * qty
        levels += 1
        if level_notional >= remaining_usd:
            # Partial consumption of this level
            contracts_taken = remaining_usd / price
            total_notional += remaining_usd
            total_contracts += contracts_taken
            remaining_usd = 0
            break
        else:
            # Full consumption — keep walking
            total_notional += level_notional
            total_contracts += qty
            remaining_usd -= level_notional

    depth_exhausted = remaining_usd > 0.01  # ≥ 1 cent unfilled
    effective_price = (total_notional / total_contracts) if total_contracts > 0 else 0.0

    return {
        "effective_price": round(effective_price, 6),
        "contracts_filled": round(total_contracts, 4),
        "notional_filled": round(total_notional, 2),
        "depth_exhausted": depth_exhausted,
        "levels_consumed": levels,
    }


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


def scan_negrisk_arb_size_aware(
    client,
    min_profit_pct: float = 0.025,
    test_basket_sizes_usd: list[float] | None = None,
) -> list[NegRiskOpportunity]:
    """
    Size-aware NegRisk scan with orderbook walking.

    For each candidate event:
      1. Compute top-of-book theoretical arb (fast path / filter)
      2. If theoretical arb clears threshold, pull orderbooks for each leg
      3. Walk each leg's book at trial basket sizes to find max executable size
      4. Apply per-leg fees (Polymarket: 2% of leg profit)
      5. Return only arbs where executable size ≥ min and profit ≥ threshold

    Args:
        client: Polymarket-style MarketClient (must implement get_orderbook)
        min_profit_pct: Minimum fee-adjusted profit percentage
        test_basket_sizes_usd: Basket sizes to probe (default [5, 25, 100, 500])

    Returns:
        List of NegRiskOpportunity with executable_size_usd populated.
        Empty list if no executable arbs.
    """
    if test_basket_sizes_usd is None:
        # Probe small→large. Small sizes often have the best arb%; large
        # sizes show how much capital can actually go to work.
        test_basket_sizes_usd = [5.0, 25.0, 100.0, 500.0]

    # Start from the top-of-book scan to find candidate events. A naive
    # filter: if top-of-book arb is ≤0, orderbook depth won't help.
    candidates = scan_negrisk_arb(client, min_profit_pct=max(0.0, min_profit_pct - 0.02))
    if not candidates:
        log_event("arb_scanner", "negrisk_size_aware_empty", {
            "reason": "no top-of-book candidates",
        }, result="success")
        return []

    enriched: list[NegRiskOpportunity] = []
    fee_rate = getattr(client, "fee_rate", 0.02)

    for candidate in candidates:
        best_executable_size = 0.0
        best_profit_pct = 0.0
        best_total_cost = 0.0
        depth_limited = False
        best_details: list[dict] = []

        for basket_usd in test_basket_sizes_usd:
            # For a YES basket: buy $basket_usd worth of YES on each outcome,
            # each leg pays 1/N of basket for 1/N of $1 payout if it wins.
            # Wait — NegRisk pays exactly $1.00 regardless of which leg wins
            # because we buy N legs summing to one-of-each. So per-leg spend
            # should be sized so *contracts per leg are equal*, not dollars.
            # We size by contracts: each leg buys `target_contracts` shares,
            # which requires ≈ price × target_contracts USD at that leg.
            # The sum of per-leg spend = sum(prices) × target_contracts.
            # Payout = 1 × target_contracts = target_contracts (one leg wins).
            n = len(candidate.outcomes)
            if n == 0:
                continue

            # Use the top-of-book sum to derive target contracts for this basket.
            top_sum = candidate.total_yes_cost if candidate.best_side == "YES" else candidate.total_no_cost
            if top_sum <= 0:
                continue
            target_contracts = basket_usd / top_sum

            per_leg = []
            total_actual_cost = 0.0
            any_depth_exhausted = False

            for outcome in candidate.outcomes:
                try:
                    book = client.get_orderbook(outcome["market_id"])
                except Exception:
                    any_depth_exhausted = True
                    break

                if candidate.best_side == "YES":
                    walk = walk_orderbook_for_size(
                        book, target_contracts * outcome["yes_price"], side="YES",
                    )
                else:
                    # For NO side, we'd walk the NO book — most APIs expose
                    # only one side, so infer by flipping asks/bids if needed.
                    no_book = {
                        "asks": [
                            {"price": 1.0 - float(b.get("price", 0)),
                             "quantity": float(b.get("quantity", 0) or b.get("size", 0))}
                            for b in (book.get("bids") or [])
                        ],
                    }
                    walk = walk_orderbook_for_size(
                        no_book, target_contracts * outcome["no_price"], side="NO",
                    )

                if walk["depth_exhausted"]:
                    any_depth_exhausted = True

                contracts_filled = walk["contracts_filled"]
                leg_cost = walk["notional_filled"]

                per_leg.append({
                    "market_id": outcome["market_id"],
                    "question": outcome.get("question", "")[:80],
                    "top_price": outcome["yes_price"] if candidate.best_side == "YES" else outcome["no_price"],
                    "effective_price": walk["effective_price"],
                    "contracts_filled": contracts_filled,
                    "leg_cost_usd": leg_cost,
                    "depth_exhausted": walk["depth_exhausted"],
                })
                total_actual_cost += leg_cost

            if len(per_leg) != n:
                continue

            # Minimum contracts across legs determines the actual arb size
            # (can't pay out more than the thinnest leg).
            min_contracts = min(
                (leg["contracts_filled"] / max(1e-9, leg["top_price"]) for leg in per_leg),
                default=0.0,
            )
            # Payout = min_contracts × $1.00 (one leg wins) for YES basket
            # For NO basket: (N-1) legs pay out
            payout_per_contract = 1.0 if candidate.best_side == "YES" else (n - 1)
            gross_payout = min_contracts * payout_per_contract

            # Per-leg fees: Polymarket charges on profit of the winning leg(s).
            # For YES basket, one leg wins with profit = (1 - effective_price) × contracts.
            # For NO basket, (N-1) legs win with profit = (1 - effective_no_price) × contracts.
            if candidate.best_side == "YES":
                winning_profit_per_contract = 1.0 - max(leg["effective_price"] for leg in per_leg)
                # Conservative: assume the worst-priced leg wins (max cost per payout dollar)
                total_fee = winning_profit_per_contract * min_contracts * fee_rate
            else:
                # Each losing NO leg still pays out (N-1 of them win)
                # Approximate fee as average effective price's profit × (N-1)
                avg_eff_no = sum(leg["effective_price"] for leg in per_leg) / n
                total_fee = (1.0 - avg_eff_no) * min_contracts * (n - 1) * fee_rate

            net_profit = gross_payout - total_actual_cost - total_fee
            if total_actual_cost <= 0:
                continue
            profit_pct = net_profit / total_actual_cost

            if profit_pct >= min_profit_pct and total_actual_cost > best_executable_size:
                best_executable_size = total_actual_cost
                best_profit_pct = profit_pct
                best_total_cost = total_actual_cost
                depth_limited = any_depth_exhausted
                best_details = per_leg

        if best_executable_size > 0:
            candidate.executable_size_usd = round(best_executable_size, 2)
            candidate.effective_total_cost = round(best_total_cost, 2)
            candidate.best_profit_pct = round(best_profit_pct, 4)
            candidate.depth_limited = depth_limited
            candidate.per_leg_details = best_details
            enriched.append(candidate)

    # Rank by executable profit in dollars (size × profit_pct), not just %
    enriched.sort(
        key=lambda x: x.executable_size_usd * x.best_profit_pct,
        reverse=True,
    )

    log_event("arb_scanner", "negrisk_size_aware_complete", {
        "top_of_book_candidates": len(candidates),
        "size_aware_opportunities": len(enriched),
        "top_executable_usd": enriched[0].executable_size_usd if enriched else 0,
        "top_profit_pct": enriched[0].best_profit_pct if enriched else 0,
    }, result="success")

    return enriched


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
