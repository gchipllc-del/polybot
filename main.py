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
    python main.py backtest          # Replay historical trades
    python main.py backtest --mc     # Monte Carlo simulation (1000 paths)
    python main.py news <question>   # Test news sentiment for a query
    python main.py kronos <ticker>   # Kronos zero-shot price forecast
    python main.py kronos-prob <ticker> <target> [above|below]  # Price probability
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
    import os

    from lib.audit import log_event
    from lib.market_client import get_active_clients
    from lib.market_scanner import get_top_candidates, print_scan_report, scan_all_markets

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

    # LLM requires ANTHROPIC_API_KEY — run without it if missing
    llm_enabled = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not llm_enabled:
        print("  (LLM analysis disabled — set ANTHROPIC_API_KEY to enable)")

    # TODO: pull real bankroll from platform balances once live
    bankroll = 50.0

    candidates = scan_all_markets(
        clients=clients,
        llm_enabled=llm_enabled,
        bankroll=bankroll,
    )

    print_scan_report(candidates)

    # Show top tradeable opportunities
    top = get_top_candidates(candidates)
    if top:
        print(f"\nRecommended trades ({len(top)}):")
        for c in top:
            f = c.forecast
            print(f"  {f.best_side} {c.market.question[:50]}")
            print(f"    Edge: {f.edge:+.1%} | Score: {f.composite_score}/9 "
                  f"| Kelly: ${c.kelly_bet_usd:.2f} | EV: ${f.expected_value:.3f}")
    else:
        print("\nNo trades meet criteria this cycle.")


def cmd_monitor():
    """Start continuous position monitoring loop."""
    from lib.monitor import start_monitoring_loop

    positions = _load_positions()
    open_count = len([p for p in positions if p.get("status") == "open"])
    print(f"Tracking {open_count} open positions")

    try:
        start_monitoring_loop()
    except KeyboardInterrupt:
        print("\nMonitoring stopped.")


def cmd_forecast(market_id: str):
    """Run forecasting engine on a specific market."""
    import os

    from lib.forecaster import estimate_probability
    from lib.market_client import get_active_clients

    print(f"Forecasting market: {market_id}")

    clients = get_active_clients()
    market = None
    for client in clients:
        try:
            market = client.get_market(market_id)
            break
        except Exception:
            continue

    if not market:
        print(f"Market {market_id} not found on any active platform.")
        return

    # LLM analysis if API key available
    llm_estimate = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from lib.llm_analyst import analyze_market
            analysis = analyze_market(
                market_id=market.market_id,
                question=market.question,
                description=market.description,
                market_price=market.yes_price,
                category=market.category,
                resolution_date=market.resolution_date,
            )
            llm_estimate = analysis.probability
            print(f"\nLLM Analysis:")
            print(f"  Probability: {analysis.probability:.0%}")
            print(f"  Confidence:  {analysis.confidence:.0%}")
            print(f"  Reference:   {analysis.reference_class}")
            for factor in analysis.key_factors:
                print(f"  Factor:      {factor}")
        except RuntimeError as e:
            print(f"  LLM unavailable: {e}")

    fee_rates = {"kalshi": 0.07, "polymarket": 0.02, "manifold": 0.0}
    fee_rate = fee_rates.get(market.platform, 0.07)

    result = estimate_probability(
        market=market,
        llm_estimate=llm_estimate,
        fee_rate=fee_rate,
    )

    print(f"\nForecast Result:")
    print(f"  Our Probability: {result.probability:.0%}")
    print(f"  Market Price:    {result.market_probability:.0%}")
    print(f"  Edge:            {result.edge:+.1%} ({result.best_side})")
    print(f"  Confidence:      {result.confidence:.0%}")
    print(f"  Composite Score: {result.composite_score}/9 "
          f"(evidence={result.evidence_score} calibration={result.calibration_score} edge={result.edge_score})")
    print(f"  Kelly Fraction:  {result.kelly_fraction:.2%}")
    print(f"  Expected Value:  ${result.expected_value:.4f}/dollar")
    print(f"\n  Bayesian Chain:")
    for step in result.bayesian_chain:
        print(f"    {step}")


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


def cmd_status(full: bool = False):
    """Print current portfolio status using Rich terminal dashboard."""
    from lib.dashboard_terminal import render_terminal_dashboard
    render_terminal_dashboard(include_calibration=full)


def cmd_calibrate():
    """Print calibration report."""
    from lib.calibration import print_calibration_report
    print_calibration_report()


def cmd_hermes(dry_run: bool = False):
    """Run Hermes self-optimization."""
    from agents.hermes_optimizer import print_optimization_report, run_optimization

    settings = _load_settings()
    lookback = settings.get("hermes", {}).get("lookback_days", 14)

    result = run_optimization(lookback_days=lookback, dry_run=dry_run)
    print_optimization_report(result)


def cmd_backtest(monte_carlo_mode: bool = False, paths: int = 1000, trades: int = 500):
    """Run historical replay or Monte Carlo simulation."""
    from lib.backtest import (
        monte_carlo,
        print_backtest_report,
        replay_historical,
        save_backtest,
    )

    if monte_carlo_mode:
        print(f"Running Monte Carlo simulation ({paths:,} paths, {trades} trades each)...")
        result = monte_carlo(
            starting_bankroll=50.0,
            target_bankroll=25000.0,
            num_paths=paths,
            trades_per_path=trades,
        )
    else:
        print("Replaying historical trades...")
        result = replay_historical(starting_bankroll=50.0)

    print_backtest_report(result)

    # Save results
    path = save_backtest(result)
    print(f"\nResults saved: {path.name}")


def cmd_news(question: str):
    """Test news sentiment for a given question."""
    from lib.news_feed import get_news_sentiment

    print(f"Fetching news sentiment for: {question[:80]}")
    result = get_news_sentiment(
        market_id="CLI_TEST",
        question=question,
        category="other",
    )

    print(f"\n  Query:       {result.query}")
    print(f"  Sentiment:   {result.sentiment:.2f} ({'YES-leaning' if result.sentiment > 0.55 else 'NO-leaning' if result.sentiment < 0.45 else 'Neutral'})")
    print(f"  Confidence:  {result.confidence:.2f}")
    print(f"  Articles:    {result.article_count} from {result.sources_queried}")
    print(f"  Cached:      {result.cached}")

    if result.articles:
        print(f"\n  Top Articles:")
        for a in result.articles[:5]:
            print(f"    [{a.source}] {a.title[:70]}")
            print(f"      relevance={a.relevance:.2f}")


def cmd_kronos(ticker: str, pred_bars: int = 30, interval: str = "1d"):
    """Run Kronos zero-shot price forecast for a ticker."""
    from lib.kronos_forecaster import predict_price, print_forecast_report

    strategy = _load_strategy()
    kronos_cfg = strategy.get("kronos", {})

    print(f"Loading Kronos model and fetching {ticker} data...")
    forecast = predict_price(
        ticker=ticker,
        pred_bars=pred_bars,
        interval=interval,
        lookback=kronos_cfg.get("default_lookback", 400),
        sample_count=kronos_cfg.get("sample_count", 10),
        temperature=kronos_cfg.get("temperature", 0.8),
        model_name=kronos_cfg.get("model_name", "NeoQuasar/Kronos-base"),
    )
    print_forecast_report(forecast)


def cmd_kronos_prob(ticker: str, target: float, direction: str = "above", horizon: int = 30):
    """Estimate probability that price crosses a target."""
    from lib.kronos_forecaster import price_to_probability, print_probability_report

    strategy = _load_strategy()
    kronos_cfg = strategy.get("kronos", {})

    print(f"Running Kronos MC probability: Will {ticker} be {direction} ${target:,.2f}?")
    result = price_to_probability(
        ticker=ticker,
        target_price=target,
        direction=direction,
        horizon_bars=horizon,
        interval="1d",
        sample_count=kronos_cfg.get("sample_count", 10),
        model_name=kronos_cfg.get("model_name", "NeoQuasar/Kronos-base"),
    )
    print_probability_report(result)


def cmd_dashboard(port: int = 5050):
    """Launch web dashboard."""
    from lib.dashboard_web import run_dashboard
    run_dashboard(port=port)


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
        full = "--full" in sys.argv
        cmd_status(full=full)
    elif command == "calibrate":
        cmd_calibrate()
    elif command == "hermes":
        dry_run = "--dry-run" in sys.argv
        cmd_hermes(dry_run)
    elif command == "backtest":
        mc_mode = "--mc" in sys.argv
        paths = 1000
        trades = 500
        for arg in sys.argv[2:]:
            if arg.startswith("--paths="):
                paths = int(arg.split("=")[1])
            elif arg.startswith("--trades="):
                trades = int(arg.split("=")[1])
        cmd_backtest(monte_carlo_mode=mc_mode, paths=paths, trades=trades)
    elif command == "news":
        if len(sys.argv) < 3:
            print("Usage: python main.py news <question>")
            return
        question = " ".join(sys.argv[2:])
        cmd_news(question)
    elif command == "kronos":
        if len(sys.argv) < 3:
            print("Usage: python main.py kronos <ticker> [--bars=30] [--interval=1d]")
            return
        ticker = sys.argv[2]
        bars = 30
        interval = "1d"
        for arg in sys.argv[3:]:
            if arg.startswith("--bars="):
                bars = int(arg.split("=")[1])
            elif arg.startswith("--interval="):
                interval = arg.split("=")[1]
        cmd_kronos(ticker, pred_bars=bars, interval=interval)
    elif command == "kronos-prob":
        if len(sys.argv) < 4:
            print("Usage: python main.py kronos-prob <ticker> <target_price> [above|below] [--horizon=30]")
            return
        ticker = sys.argv[2]
        target = float(sys.argv[3])
        direction = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] in ("above", "below") else "above"
        horizon = 30
        for arg in sys.argv[4:]:
            if arg.startswith("--horizon="):
                horizon = int(arg.split("=")[1])
        cmd_kronos_prob(ticker, target, direction, horizon)
    elif command == "dashboard":
        port = 5050
        for arg in sys.argv[2:]:
            if arg.startswith("--port"):
                port = int(sys.argv[sys.argv.index(arg) + 1]) if "=" not in arg else int(arg.split("=")[1])
        cmd_dashboard(port=port)
    elif command == "chaos":
        cmd_chaos()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()
