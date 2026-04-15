"""
Polybot — Prediction Market Trading Bot
CLI entry point. Every command routes through here.

Usage:
    python main.py scan              # Scan markets, score candidates, propose trades
    python main.py monitor           # Start continuous position monitoring
    python main.py forecast <id>     # Run forecaster on a specific market
    python main.py arb               # Cross-platform arbitrage scan
    python main.py kill [reason]     # Emergency kill switch
    python main.py status            # Current positions, bankroll, calibration
    python main.py calibrate         # Print calibration report
    python main.py hermes            # Run self-optimization
    python main.py hermes --dry-run  # Analysis only, no changes
    python main.py dashboard         # Launch web dashboard
    python main.py chaos             # Run chaos tests
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"


def _load_settings() -> dict:
    with open(CONFIG_DIR / "settings.yaml", "r") as f:
        return yaml.safe_load(f)


def _load_strategy() -> dict:
    with open(CONFIG_DIR / "strategy.yaml", "r") as f:
        return yaml.safe_load(f)


def _load_positions() -> list[dict]:
    pos_file = DATA_DIR / "positions.json"
    if pos_file.exists():
        with open(pos_file, "r") as f:
            return json.load(f)
    return []


def _save_positions(positions: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "positions.json", "w") as f:
        json.dump(positions, f, indent=2)


def cmd_scan():
    """Scan markets across all active platforms, score candidates, propose trades."""
    from lib.audit import log_event
    from lib.market_client import get_active_clients

    log_event("startup", "scan_started", {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    settings = _load_settings()
    strategy = _load_strategy()
    clients = get_active_clients()

    if not clients:
        print("No active platform clients. Check config/settings.yaml")
        return

    print(f"Scanning {len(clients)} platform(s): {[c.platform_name for c in clients]}")

    total_markets = 0
    for client in clients:
        try:
            markets = client.get_markets(status="open", limit=50)
            total_markets += len(markets)
            print(f"  [{client.platform_name}] {len(markets)} open markets found")

            for market in markets[:5]:  # Show top 5 for now
                print(f"    {market.yes_price:.0%} YES | {market.question[:60]}")
        except Exception as e:
            print(f"  [{client.platform_name}] Error: {e}")

    print(f"\nTotal markets scanned: {total_markets}")
    print("(Forecasting engine not yet active — Phase 2 will score and trade)")


def cmd_monitor():
    """Start continuous position monitoring loop."""
    from lib.audit import log_event

    log_event("startup", "monitor_started", {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    settings = _load_settings()
    interval = settings["monitoring"]["check_interval_seconds"]

    print(f"Starting monitoring loop (every {interval}s)")
    print("(Full monitoring engine coming in Phase 4)")

    positions = _load_positions()
    print(f"Tracking {len(positions)} open positions")


def cmd_forecast(market_id: str):
    """Run forecasting engine on a specific market."""
    print(f"Forecasting market: {market_id}")
    print("(Forecasting engine coming in Phase 2)")


def cmd_arb():
    """Scan for NegRisk arbitrage + near-resolution harvesting + cross-platform arb."""
    from lib.market_client import get_active_clients
    from lib.negrisk_scanner import scan_negrisk_arb, scan_near_resolution

    clients = get_active_clients()
    if not clients:
        print("No active platform clients. Check config/settings.yaml")
        return

    print("=" * 60)
    print("  ARBITRAGE SCANNER")
    print("=" * 60)

    # NegRisk Arbitrage
    print("\n--- NegRisk Arbitrage (Guaranteed Profit) ---")
    for client in clients:
        opportunities = scan_negrisk_arb(client)
        if opportunities:
            for opp in opportunities[:5]:
                print(f"  [{opp.platform}] {opp.best_profit_pct:.1%} profit | "
                      f"{opp.num_outcomes} outcomes | {opp.event_title[:50]}")
        else:
            print(f"  [{client.platform_name}] No NegRisk opportunities found")

    # Near-Resolution Harvesting
    print("\n--- Near-Resolution Harvesting (Low Risk) ---")
    for client in clients:
        near_res = scan_near_resolution(client)
        if near_res:
            for nr in near_res[:5]:
                print(f"  [{nr['platform']}] {nr['profit_pct']}% in {nr['hours_to_resolution']}h | "
                      f"{nr['side']} @ {nr['price']:.2f} | {nr['question'][:40]}")
        else:
            print(f"  [{client.platform_name}] No near-resolution opportunities")

    print("\n(Cross-platform price arb coming in Phase 5)")
    print("=" * 60)


def cmd_kill(reason: str = "manual_cli"):
    """Emergency kill switch — close everything."""
    from lib.kill_switch import activate_kill_switch

    print(f"ACTIVATING KILL SWITCH: {reason}")
    result = activate_kill_switch(reason)

    for platform, pr in result.get("platforms", {}).items():
        print(f"  [{platform}] Cancelled: {pr['orders_cancelled']}, "
              f"Closed: {pr['positions_closed']}")

    if result["errors"]:
        for err in result["errors"]:
            print(f"  ERROR: {err}")
    else:
        print("Clean shutdown complete.")


def cmd_status():
    """Print current portfolio status."""
    from lib.market_client import get_active_clients

    settings = _load_settings()
    strategy = _load_strategy()

    print("=" * 60)
    print("  POLYBOT — Prediction Market Trading Bot")
    print("=" * 60)
    print(f"  Mode:           {settings['mode']}")
    print(f"  Growth Phase:   {strategy['growth']['phase']}")
    print(f"  Kelly:          {strategy['kelly_multiplier']}x")
    print(f"  Min Edge:       {strategy['scoring']['min_edge']:.0%}")
    print(f"  Min Score:      {strategy['scoring']['min_composite_score']}/9")
    print()

    # Show balances from each platform
    clients = get_active_clients()
    total_balance = 0.0
    for client in clients:
        try:
            balance = client.get_balance()
            total_balance += balance
            print(f"  [{client.platform_name}] Balance: ${balance:.2f}")
        except Exception as e:
            print(f"  [{client.platform_name}] Balance: unavailable ({e})")

    print(f"\n  Total Bankroll: ${total_balance:.2f}")

    # Show positions
    positions = _load_positions()
    if positions:
        print(f"\n  Open Positions: {len(positions)}")
        for pos in positions:
            side = pos.get("side", "?")
            question = pos.get("question", "")[:40]
            entry = pos.get("entry_price", 0)
            current = pos.get("current_price", 0)
            pnl = (current - entry) * pos.get("quantity", 0)
            print(f"    {side} @ {entry:.2f} -> {current:.2f} ({pnl:+.2f}) | {question}")
    else:
        print("\n  No open positions")

    print()
    print("=" * 60)


def cmd_calibrate():
    """Print calibration report."""
    from lib.calibration import print_calibration_report
    print_calibration_report()


def cmd_hermes(dry_run: bool = False):
    """Run Hermes self-optimization."""
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"Hermes Optimizer [{mode}]")
    print("(Hermes optimizer coming in Phase 5)")


def cmd_dashboard():
    """Launch web dashboard."""
    print("Starting Polybot Dashboard on http://localhost:5050")
    print("(Dashboard coming in Phase 4)")


def cmd_chaos():
    """Run chaos tests to verify safety systems."""
    from lib.audit import log_event
    from lib.circuit_breaker import CircuitBreakerTripped, run_all_checks
    from lib.order_gate import OrderIntent, step1_propose, step2_validate

    print("CHAOS TEST: Verifying safety systems")
    print("-" * 40)
    passed = 0
    failed = 0

    # Test 1: Circuit breaker — daily loss
    try:
        run_all_checks(
            order_value=5.0, bankroll=50.0, current_daily_pnl=-15.0,
            current_open_positions=0, quantity=1,
        )
        print("FAIL: Daily loss breaker did not trip")
        failed += 1
    except CircuitBreakerTripped:
        print("PASS: Daily loss breaker trips correctly")
        passed += 1

    # Test 2: Circuit breaker — position size
    try:
        run_all_checks(
            order_value=20.0, bankroll=50.0, current_daily_pnl=0.0,
            current_open_positions=0, quantity=1,
        )
        print("FAIL: Position size breaker did not trip")
        failed += 1
    except CircuitBreakerTripped:
        print("PASS: Position size breaker trips correctly")
        passed += 1

    # Test 3: Duplicate order detection
    intent = OrderIntent(
        market_id="TEST-001", platform="manifold", question="Test?",
        side="YES", order_type="limit", quantity=1, limit_price=0.50,
    )
    try:
        step1_propose(intent)
        # Same intent again within 60s window
        intent2 = OrderIntent(
            market_id="TEST-001", platform="manifold", question="Test?",
            side="YES", order_type="limit", quantity=1, limit_price=0.50,
        )
        step1_propose(intent2)
        print("FAIL: Duplicate detection did not block")
        failed += 1
    except ValueError:
        print("PASS: Duplicate order detection works")
        passed += 1

    # Test 4: Low score rejection
    low_score_intent = OrderIntent(
        market_id="TEST-002", platform="manifold", question="Low score?",
        side="YES", order_type="limit", quantity=1, limit_price=0.50,
        composite_score=2,
    )
    try:
        step1_propose(low_score_intent)
        step2_validate(low_score_intent, bankroll=50.0, current_daily_pnl=0.0,
                       current_open_positions=0)
        print("FAIL: Low score was not rejected")
        failed += 1
    except ValueError as e:
        if "score" in str(e).lower():
            print("PASS: Low score rejection works")
            passed += 1
        else:
            print(f"FAIL: Wrong error: {e}")
            failed += 1

    # Test 5: Unvalidated execution blocked
    try:
        raw_intent = OrderIntent(
            market_id="TEST-003", platform="manifold", question="Not validated?",
            side="YES", order_type="limit", quantity=1, limit_price=0.50,
        )
        from lib.order_gate import step3_execute
        step3_execute(raw_intent, None)
        print("FAIL: Unvalidated execution was not blocked")
        failed += 1
    except RuntimeError:
        print("PASS: Unvalidated execution blocked")
        passed += 1

    # Test 6: Secret redaction in audit
    event = log_event("chaos_test", "secret_test", {
        "api_key": "sk-test-12345",
        "market_id": "TEST-004",
        "secret_token": "tok_abc",
    })
    if event["details"]["api_key"] == "***REDACTED***":
        print("PASS: Secret redaction works")
        passed += 1
    else:
        print("FAIL: Secrets not redacted")
        failed += 1

    print("-" * 40)
    print(f"Results: {passed} passed, {failed} failed out of {passed + failed}")

    if failed == 0:
        print("\nAll safety systems operational.")
    else:
        print(f"\nWARNING: {failed} safety test(s) failed!")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    command = sys.argv[1].lower()

    if command == "scan":
        cmd_scan()
    elif command == "monitor":
        cmd_monitor()
    elif command == "forecast":
        if len(sys.argv) < 3:
            print("Usage: python main.py forecast <market_id>")
            return
        cmd_forecast(sys.argv[2])
    elif command == "arb":
        cmd_arb()
    elif command == "kill":
        reason = sys.argv[2] if len(sys.argv) > 2 else "manual_cli"
        cmd_kill(reason)
    elif command == "status":
        cmd_status()
    elif command == "calibrate":
        cmd_calibrate()
    elif command == "hermes":
        dry_run = "--dry-run" in sys.argv
        cmd_hermes(dry_run)
    elif command == "dashboard":
        cmd_dashboard()
    elif command == "chaos":
        cmd_chaos()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()
