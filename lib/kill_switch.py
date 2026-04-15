"""
KILL SWITCH — Emergency full liquidation across ALL platforms.
Callable from Telegram, CLI, or cron failsafe.

Actions (in order):
1. Cancel all pending orders on all platforms
2. Close all open positions at market on all platforms
3. Send emergency alert
4. Log everything
"""

from lib.audit import log_event
from lib.market_client import get_active_clients


def activate_kill_switch(reason: str = "manual") -> dict:
    """
    Nuclear option. Closes everything on every platform.

    Use when:
    - Monitoring cron missed 10+ checks
    - Unrecoverable error detected
    - Manual emergency from Telegram / CLI
    - Anomalous trading activity detected
    - Bankroll dropped below survival threshold
    """
    log_event("kill_switch", "activated", {
        "reason": reason,
    }, result="pending")

    results = {
        "reason": reason,
        "platforms": {},
        "total_orders_cancelled": 0,
        "total_positions_closed": 0,
        "errors": [],
    }

    clients = get_active_clients()

    if not clients:
        results["errors"].append("No active platform clients found")
        log_event("kill_switch", "no_clients", {
            "reason": reason,
        }, result="failed")
        return results

    for client in clients:
        platform = client.platform_name
        platform_result = {
            "orders_cancelled": 0,
            "positions_closed": 0,
            "errors": [],
        }

        # Step 1: Cancel all pending orders
        try:
            cancelled = client.cancel_all_orders()
            platform_result["orders_cancelled"] = max(cancelled, 0)
        except Exception as e:
            platform_result["errors"].append(f"cancel_orders: {e}")

        # Step 2: Close all positions at market
        try:
            closed = client.close_all_positions()
            platform_result["positions_closed"] = closed
        except Exception as e:
            platform_result["errors"].append(f"close_positions: {e}")

        results["platforms"][platform] = platform_result
        results["total_orders_cancelled"] += platform_result["orders_cancelled"]
        results["total_positions_closed"] += platform_result["positions_closed"]
        results["errors"].extend(
            [f"[{platform}] {e}" for e in platform_result["errors"]]
        )

    # Step 3: Log final result
    status = "success" if not results["errors"] else "partial"
    log_event("kill_switch", "completed", results, result=status)

    return results


if __name__ == "__main__":
    import sys

    reason = sys.argv[1] if len(sys.argv) > 1 else "manual_cli"
    print(f"ACTIVATING KILL SWITCH: {reason}")
    result = activate_kill_switch(reason)

    for platform, pr in result["platforms"].items():
        print(f"  [{platform}] Orders cancelled: {pr['orders_cancelled']}, "
              f"Positions closed: {pr['positions_closed']}")

    if result["errors"]:
        print(f"Errors: {result['errors']}")
    else:
        print("Clean shutdown complete across all platforms.")
