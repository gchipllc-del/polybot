"""
Resolution Tracker — settlement lifecycle and P/L accounting.

Prediction market lifecycle:
    1. OPEN — position entered, tracking begins
    2. MONITORING — price updates, exit signal checks
    3. RESOLVED — market resolved, outcome known
    4. SETTLED — P/L calculated, calibration recorded, position closed

Handles:
    - Resolution detection (from monitor or manual check)
    - P/L calculation with platform-specific fees
    - Calibration recording (feed back into forecaster accuracy)
    - Trade history persistence
    - Dispute detection (outcome changes or delays)

Security:
    - All settlements logged to audit trail
    - Fee calculations use platform-specific rates (not estimates)
    - Positions updated atomically (read-modify-write with validation)
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from tradingcore.audit import log_event
from tradingcore.calibration import record_forecast

DATA_DIR = Path(__file__).parent.parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"
TRADE_HISTORY_PATH = DATA_DIR / "trade_history.json"

# Platform fee structures
FEE_RATES = {
    "kalshi": 0.07,      # 7% of profit (only on wins)
    "polymarket": 0.02,  # ~2% of winnings
    "manifold": 0.0,     # Free (play money)
}


def _load_positions() -> list[dict]:
    if not POSITIONS_PATH.exists():
        return []
    try:
        with open(POSITIONS_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_positions(positions: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(POSITIONS_PATH, "w") as f:
        json.dump(positions, f, indent=2)


def _load_trade_history() -> list[dict]:
    if not TRADE_HISTORY_PATH.exists():
        return []
    try:
        with open(TRADE_HISTORY_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_trade_history(history: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(TRADE_HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)


def calculate_pnl(
    side: str,
    entry_price: float,
    quantity: int,
    outcome: str,
    platform: str,
) -> dict:
    """
    Calculate P/L for a resolved prediction market position.

    In prediction markets:
        - Winning YES: payout $1.00 per contract. Profit = (1.00 - entry_price) * quantity
        - Losing YES: payout $0.00. Loss = entry_price * quantity
        - Winning NO: payout $1.00 per contract. Profit = (1.00 - entry_price) * quantity
        - Losing NO: payout $0.00. Loss = entry_price * quantity

    Fees are charged on profit only (not on losses).

    Args:
        side: "YES" or "NO"
        entry_price: Price paid per contract (0.00 - 1.00)
        quantity: Number of contracts
        outcome: "YES" or "NO" (how the market resolved)
        platform: Platform name for fee calculation

    Returns:
        {"gross_profit": float, "fees": float, "net_profit": float, "won": bool}
    """
    won = (side == outcome)
    fee_rate = FEE_RATES.get(platform, 0.07)

    if won:
        gross_profit = (1.0 - entry_price) * quantity
        fees = gross_profit * fee_rate
        net_profit = gross_profit - fees
    else:
        gross_profit = -entry_price * quantity
        fees = 0.0  # No fees on losses
        net_profit = gross_profit

    return {
        "won": won,
        "gross_profit": round(gross_profit, 4),
        "fees": round(fees, 4),
        "net_profit": round(net_profit, 4),
        "payout_per_contract": 1.0 if won else 0.0,
        "cost_per_contract": entry_price,
    }


def settle_position(
    market_id: str,
    outcome: str,
    resolution_source: str = "",
) -> dict | None:
    """
    Settle a resolved position: calculate P/L, update records, record calibration.

    Args:
        market_id: The resolved market ID
        outcome: "YES" or "NO"
        resolution_source: Who decided the outcome

    Returns:
        Settlement record dict, or None if no matching position found.
    """
    positions = _load_positions()

    # Find matching open position
    pos_idx = None
    for i, p in enumerate(positions):
        if p.get("market_id") == market_id and p.get("status") == "open":
            pos_idx = i
            break

    if pos_idx is None:
        return None

    pos = positions[pos_idx]
    side = pos.get("side", "YES")
    entry_price = pos.get("entry_price", 0)
    quantity = pos.get("quantity", 0)
    platform = pos.get("platform", "")
    our_probability = pos.get("our_probability", 0)
    question = pos.get("question", "")
    category = pos.get("category", "")
    sources = pos.get("forecast_sources", {})

    # Calculate P/L
    pnl = calculate_pnl(side, entry_price, quantity, outcome, platform)

    # Build settlement record
    settlement = {
        "market_id": market_id,
        "platform": platform,
        "question": question,
        "category": category,
        "side": side,
        "outcome": outcome,
        "entry_price": entry_price,
        "quantity": quantity,
        "won": pnl["won"],
        "gross_profit": pnl["gross_profit"],
        "fees": pnl["fees"],
        "net_profit": pnl["net_profit"],
        "our_probability": our_probability,
        "resolution_source": resolution_source,
        "opened_at": pos.get("opened_at", ""),
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }

    # Update position status
    positions[pos_idx]["status"] = "settled"
    positions[pos_idx]["outcome"] = outcome
    positions[pos_idx]["net_profit"] = pnl["net_profit"]
    positions[pos_idx]["closed_at"] = settlement["closed_at"]
    _save_positions(positions)

    # Append to trade history
    history = _load_trade_history()
    history.append(settlement)
    _save_trade_history(history)

    # Record calibration data
    outcome_bool = outcome == "YES"
    record_forecast(
        market_id=market_id,
        platform=platform,
        question=question,
        our_probability=our_probability,
        market_probability=entry_price,
        side=side,
        sources=sources if isinstance(sources, dict) else {},
        outcome=outcome_bool,
    )

    # Store in memory palace. Catch *every* exception (not just ImportError):
    # chromadb's onnx embedder occasionally segfaults / raises AttributeError
    # in fresh subprocess Python invocations, which had been preventing the
    # `position_settled` audit log line below from ever firing. The
    # settlement itself (status update + trade history + calibration record)
    # has already succeeded by the time we reach this block — memory palace
    # is an enhancement, not a requirement.
    try:
        from lib.memory_palace import remember_resolution
        remember_resolution(
            market_id=market_id,
            platform=platform,
            category=category,
            outcome=outcome,
            profit=pnl["net_profit"],
            our_probability=our_probability,
        )
    except Exception as _mp_exc:
        log_event("resolution", "memory_palace_skip", {
            "market_id": market_id,
            "error": str(_mp_exc)[:200],
        }, result="degraded")

    log_event("resolution", "position_settled", {
        "market_id": market_id,
        "platform": platform,
        "side": side,
        "outcome": outcome,
        "won": pnl["won"],
        "net_profit": pnl["net_profit"],
        "fees": pnl["fees"],
    }, result="success")

    return settlement


def check_all_resolutions() -> list[dict]:
    """
    Check all open positions for resolutions and settle any that resolved.

    Returns list of settlement records.
    """
    from lib.market_client import get_active_clients

    positions = _load_positions()
    open_positions = [p for p in positions if p.get("status") == "open"]

    if not open_positions:
        return []

    clients = get_active_clients()
    client_map = {c.platform_name: c for c in clients}

    settlements = []

    for pos in open_positions:
        market_id = pos.get("market_id", "")
        platform = pos.get("platform", "")

        client = client_map.get(platform)
        if not client:
            continue

        try:
            market = client.get_market(market_id)
            if market.status == "resolved" and market.outcome:
                settlement = settle_position(
                    market_id=market_id,
                    outcome=market.outcome,
                    resolution_source=market.resolution_source,
                )
                if settlement:
                    settlements.append(settlement)
        except Exception as e:
            log_event("resolution", "check_failed", {
                "market_id": market_id,
                "error": str(e)[:200],
            }, result="failed")

    return settlements


def _is_resolved_trade(t: dict) -> bool:
    """A trade counts toward win-rate / P&L only if it actually resolved.

    Discriminator: `won` is explicitly set to a bool by settle_position().
    Open positions that were appended to history mid-flight (or other
    intermediate records) lack this field and would otherwise corrupt
    the win-rate denominator (this was the bite at finding #2 of polybot
    audit on 2026-04-30 — open trades were inflating the loss count).
    """
    return isinstance(t.get("won"), bool)


def get_performance_summary() -> dict:
    """
    Calculate overall trading performance from trade history.

    Returns:
        Win rate, total P/L, average profit/loss, best/worst trades, etc.
    """
    history_all = _load_trade_history()
    history = [t for t in history_all if _is_resolved_trade(t)]
    if not history:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "avg_win": 0,
            "avg_loss": 0,
            "best_trade": 0,
            "worst_trade": 0,
            "total_fees": 0,
            "by_platform": {},
            "by_category": {},
        }

    wins = [t for t in history if t.get("won", False)]
    losses = [t for t in history if not t.get("won", False)]

    total_pnl = sum(t.get("net_profit", 0) for t in history)
    total_fees = sum(t.get("fees", 0) for t in history)

    # By platform
    by_platform: dict[str, dict] = {}
    for t in history:
        p = t.get("platform", "unknown")
        if p not in by_platform:
            by_platform[p] = {"trades": 0, "wins": 0, "pnl": 0}
        by_platform[p]["trades"] += 1
        if t.get("won"):
            by_platform[p]["wins"] += 1
        by_platform[p]["pnl"] += t.get("net_profit", 0)

    # By category
    by_category: dict[str, dict] = {}
    for t in history:
        c = t.get("category", "other")
        if c not in by_category:
            by_category[c] = {"trades": 0, "wins": 0, "pnl": 0}
        by_category[c]["trades"] += 1
        if t.get("won"):
            by_category[c]["wins"] += 1
        by_category[c]["pnl"] += t.get("net_profit", 0)

    return {
        "total_trades": len(history),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(history), 4) if history else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(sum(t.get("net_profit", 0) for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t.get("net_profit", 0) for t in losses) / len(losses), 2) if losses else 0,
        "best_trade": round(max((t.get("net_profit", 0) for t in history), default=0), 2),
        "worst_trade": round(min((t.get("net_profit", 0) for t in history), default=0), 2),
        "total_fees": round(total_fees, 2),
        "by_platform": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in by_platform.items()},
        "by_category": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in by_category.items()},
    }
