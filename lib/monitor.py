"""
Position Monitor — continuous monitoring loop for prediction markets.

Checks:
    1. Price drift — market moved significantly against us
    2. Edge erosion — our edge has disappeared
    3. Stop loss — position dropped past stop loss threshold
    4. Take profit — position hit take profit target
    5. Resolution detection — market resolved, needs settlement
    6. Stale positions — no market activity in 24+ hours

Runs on a configurable interval (default 120s). Missed checks trigger
alerts; 10 consecutive misses trigger kill switch.

Security:
    - Read-only monitoring — never places orders directly
    - Missed check detection for reliability monitoring
    - All findings logged to audit trail
    - Exit signals returned to caller for order gate processing
"""

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from lib.audit import log_event
from lib.market_client import MarketClient, MarketInfo, get_active_clients

DATA_DIR = Path(__file__).parent.parent / "data"
POSITIONS_PATH = DATA_DIR / "positions.json"
CONFIG_PATH = Path(__file__).parent.parent / "config"


def _load_settings() -> dict:
    with open(CONFIG_PATH / "settings.yaml", "r") as f:
        return yaml.safe_load(f)


def _load_strategy() -> dict:
    with open(CONFIG_PATH / "strategy.yaml", "r") as f:
        return yaml.safe_load(f)


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


@dataclass
class ExitSignal:
    """A signal to exit a position, returned by the monitor."""
    market_id: str
    platform: str
    reason: str              # "stop_loss", "take_profit", "edge_gone", "price_drift", "resolved", "stale"
    side: str                # Current side of our position
    entry_price: float
    current_price: float
    urgency: str             # "immediate" (stop loss, resolved) or "advisory" (drift, stale)
    details: dict


def check_position(
    position: dict,
    current_market: MarketInfo | None,
    strategy: dict,
) -> ExitSignal | None:
    """
    Run all exit checks on a single position.

    Args:
        position: Position dict from positions.json
        current_market: Live market data (None if market not found)
        strategy: Strategy config

    Returns:
        ExitSignal if position should be exited, None if OK.
    """
    exits = strategy.get("exits", {})
    market_id = position.get("market_id", "")
    platform = position.get("platform", "")
    side = position.get("side", "YES")
    entry_price = position.get("entry_price", 0)

    # If market not found, flag as stale
    if current_market is None:
        return ExitSignal(
            market_id=market_id, platform=platform,
            reason="market_not_found", side=side,
            entry_price=entry_price, current_price=0,
            urgency="advisory",
            details={"note": "Market not found on platform — may have been delisted"},
        )

    current_price = current_market.yes_price if side == "YES" else current_market.no_price

    # Update current price in position
    position["current_price"] = current_price
    position["last_checked"] = datetime.now(timezone.utc).isoformat()

    # ── 1. Resolution Detection ───────────────────────────────────
    if current_market.status == "resolved":
        return ExitSignal(
            market_id=market_id, platform=platform,
            reason="resolved", side=side,
            entry_price=entry_price, current_price=current_price,
            urgency="immediate",
            details={"outcome": current_market.outcome, "resolution_source": current_market.resolution_source},
        )

    # ── 2. Stop Loss ─────────────────────────────────────────────
    stop_loss_pct = exits.get("stop_loss_pct", 0.25)
    price_drop = entry_price - current_price
    drop_pct = price_drop / entry_price if entry_price > 0 else 0

    if drop_pct >= stop_loss_pct:
        return ExitSignal(
            market_id=market_id, platform=platform,
            reason="stop_loss", side=side,
            entry_price=entry_price, current_price=current_price,
            urgency="immediate",
            details={"drop_pct": round(drop_pct, 4), "threshold": stop_loss_pct},
        )

    # ── 3. Take Profit ───────────────────────────────────────────
    take_profit = exits.get("take_profit_price", 0.95)
    if current_price >= take_profit:
        return ExitSignal(
            market_id=market_id, platform=platform,
            reason="take_profit", side=side,
            entry_price=entry_price, current_price=current_price,
            urgency="immediate",
            details={"price": current_price, "threshold": take_profit},
        )

    # ── 4. Early Exit (gain threshold) ───────────────────────────
    early_exit = exits.get("early_exit_threshold", 0.15)
    gain_pct = (current_price - entry_price) / entry_price if entry_price > 0 else 0
    if gain_pct >= early_exit:
        return ExitSignal(
            market_id=market_id, platform=platform,
            reason="early_exit", side=side,
            entry_price=entry_price, current_price=current_price,
            urgency="advisory",
            details={"gain_pct": round(gain_pct, 4), "threshold": early_exit},
        )

    # ── 5. Edge Gone ─────────────────────────────────────────────
    edge_buffer = exits.get("edge_gone_buffer", 0.03)
    our_prob = position.get("our_probability", 0)
    if our_prob > 0:
        market_prob = current_market.yes_price
        current_edge = our_prob - market_prob if side == "YES" else (1 - our_prob) - (1 - market_prob)
        if current_edge < -edge_buffer:
            return ExitSignal(
                market_id=market_id, platform=platform,
                reason="edge_gone", side=side,
                entry_price=entry_price, current_price=current_price,
                urgency="advisory",
                details={"current_edge": round(current_edge, 4), "buffer": edge_buffer},
            )

    # ── 6. Price Drift Alert ─────────────────────────────────────
    drift_threshold = exits.get("price_drift_alert", 0.10)
    if abs(current_price - entry_price) / max(entry_price, 0.01) >= drift_threshold:
        # Only alert, don't exit — this is informational
        log_event("monitor", "price_drift", {
            "market_id": market_id,
            "entry": entry_price,
            "current": current_price,
            "drift_pct": round(abs(current_price - entry_price) / max(entry_price, 0.01), 4),
        })

    # ── 7. Stale Position ────────────────────────────────────────
    stale_hours = exits.get("stale_position_hours", 24)
    last_activity = position.get("last_activity", position.get("opened_at", ""))
    if last_activity:
        try:
            last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
            hours_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
            if hours_since > stale_hours and current_market.volume_24h < 100:
                return ExitSignal(
                    market_id=market_id, platform=platform,
                    reason="stale", side=side,
                    entry_price=entry_price, current_price=current_price,
                    urgency="advisory",
                    details={"hours_since_activity": round(hours_since, 1), "volume_24h": current_market.volume_24h},
                )
        except (ValueError, TypeError):
            pass

    return None


def run_monitoring_cycle() -> dict:
    """
    Run one full monitoring cycle across all positions.

    Returns:
        {
            "positions_checked": int,
            "exit_signals": [ExitSignal, ...],
            "errors": [str, ...],
            "timestamp": str,
        }
    """
    strategy = _load_strategy()
    positions = _load_positions()
    open_positions = [p for p in positions if p.get("status", "open") == "open"]

    if not open_positions:
        return {
            "positions_checked": 0,
            "exit_signals": [],
            "errors": [],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    clients = get_active_clients()
    client_map: dict[str, MarketClient] = {c.platform_name: c for c in clients}

    exit_signals: list[ExitSignal] = []
    errors: list[str] = []

    for pos in open_positions:
        market_id = pos.get("market_id", "")
        platform = pos.get("platform", "")

        client = client_map.get(platform)
        if not client:
            errors.append(f"No client for platform '{platform}' (market {market_id})")
            continue

        # Fetch current market data
        current_market = None
        try:
            current_market = client.get_market(market_id)
        except Exception as e:
            errors.append(f"Failed to fetch {market_id} on {platform}: {str(e)[:100]}")

        # Run checks
        signal = check_position(pos, current_market, strategy)
        if signal:
            exit_signals.append(signal)
            log_event("monitor", f"exit_signal_{signal.reason}", {
                "market_id": signal.market_id,
                "platform": signal.platform,
                "side": signal.side,
                "entry": signal.entry_price,
                "current": signal.current_price,
                "urgency": signal.urgency,
            })

    # Save updated positions (with current prices)
    _save_positions(positions)

    log_event("monitor", "cycle_complete", {
        "positions_checked": len(open_positions),
        "exit_signals": len(exit_signals),
        "errors": len(errors),
    }, result="success")

    return {
        "positions_checked": len(open_positions),
        "exit_signals": exit_signals,
        "errors": errors,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def start_monitoring_loop():
    """
    Start the continuous monitoring loop.

    Runs check cycles at the configured interval. Tracks missed checks
    for reliability alerting. Caller handles KeyboardInterrupt.
    """
    settings = _load_settings()
    interval = settings["monitoring"]["check_interval_seconds"]
    missed_alert = settings["monitoring"]["missed_check_alert"]
    missed_kill = settings["monitoring"]["missed_check_kill"]

    consecutive_misses = 0

    log_event("monitor", "loop_started", {
        "interval_seconds": interval,
        "missed_alert_threshold": missed_alert,
        "missed_kill_threshold": missed_kill,
    })

    print(f"Monitoring loop started (every {interval}s)")
    print(f"  Missed check alert at {missed_alert}, kill switch at {missed_kill}")

    while True:
        cycle_start = time.time()

        try:
            result = run_monitoring_cycle()
            consecutive_misses = 0

            n_checked = result["positions_checked"]
            n_signals = len(result["exit_signals"])
            n_errors = len(result["errors"])

            print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                  f"Checked {n_checked} positions | "
                  f"{n_signals} exit signals | {n_errors} errors")

            # Print exit signals
            for sig in result["exit_signals"]:
                urgency_marker = "!!!" if sig.urgency == "immediate" else "..."
                print(f"  {urgency_marker} [{sig.reason}] {sig.market_id} "
                      f"{sig.side} {sig.entry_price:.2f}->{sig.current_price:.2f}")

            for err in result["errors"]:
                print(f"  ERR: {err}")

        except Exception as e:
            consecutive_misses += 1
            log_event("monitor", "cycle_failed", {
                "error": str(e)[:200],
                "consecutive_misses": consecutive_misses,
            }, result="failed")

            print(f"  MISS #{consecutive_misses}: {e}")

            if consecutive_misses >= missed_kill:
                print(f"  KILL SWITCH: {consecutive_misses} consecutive misses!")
                log_event("monitor", "kill_switch_triggered", {
                    "reason": f"{consecutive_misses} consecutive monitoring failures",
                })
                from lib.kill_switch import activate_kill_switch
                activate_kill_switch(f"monitoring_missed_{consecutive_misses}")
                return

            if consecutive_misses >= missed_alert:
                log_event("monitor", "missed_check_alert", {
                    "consecutive_misses": consecutive_misses,
                })

        # Sleep for remainder of interval
        elapsed = time.time() - cycle_start
        sleep_time = max(0, interval - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)
