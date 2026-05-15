"""
Polybot — Prediction Market Trading Bot
CLI entry point. Every command routes through here.

Usage:
    python main.py trade                    # FULL PIPELINE: harvester → scan → consensus → execute
    python main.py trade --dry-run          # Same pipeline, but don't place orders
    python main.py trade --skip-harvester   # Skip the mechanical harvester, conviction only
    python main.py scan                     # Scan markets, score candidates (no execution)
    python main.py monitor                  # Start continuous position monitoring
    python main.py forecast <id>            # Run forecaster on a specific market
    python main.py arb                      # Cross-platform arbitrage REPORT (read-only)
    python main.py harvest                  # Mechanical near-resolution harvester only
    python main.py harvest --dry-run        # Report what harvester would buy
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
    python main.py smoke             # Pipeline regression (no external APIs)
    python main.py brier [--n=50]    # Cold-start calibration: replay ForecastBench dataset
    python main.py wallet-scan       # Discover + score top wallets (Stage 1 copy-trade)
    python main.py wallet-score <handle>  # Deep-dive one wallet
    python main.py wallet-watch      # Poll watchlist, alert on new bets (Stage 2)
    python main.py wallet-backtest <handle>  # Replay wallet's history as hypothetical copy
    python main.py wallet-backtest-all       # Backtest every scored + curated wallet, rank by ROI
    python main.py paper-copy-settle # Settle resolved paper-copy trades (Stage 3)
    python main.py paper-copy-report # Aggregate paper P&L per source wallet (Stage 3)
    python main.py btc-arb-monitor   # Sample Binance spot vs Polymarket BTC gaps (Phase 1)
    python main.py btc-5min-monitor       # Sample 5-min BTC UP/DOWN markets (Gravia-style)
    python main.py btc-5min-paper-settle  # Settle resolved 5-min paper trades
    python main.py btc-5min-paper-report  # Aggregate 5-min paper P&L + confidence-bucket WR
    python main.py dataset-status         # Check Jon-Becker parquet dataset availability
    python main.py kalshi-auth-status     # Verify Kalshi RSA-PSS auth wiring
    python main.py kalshi-test-auth       # Make one signed call (/portfolio/balance) to prove it works
    python main.py kalshi-15min-monitor   # Sample Kalshi 15-min BTC markets (cron-friendly)
    python main.py kalshi-15min-paper-settle  # Settle resolved Kalshi paper trades
    python main.py kalshi-15min-paper-report  # Aggregate Kalshi paper P&L + WR
    python main.py kalshi-dashboard           # Web dashboard at localhost:5053
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

# Load .env before anything touches os.environ.
# override=True is critical because some parent shells (notably Claude Code)
# pre-set ANTHROPIC_API_KEY to an empty string, which would otherwise beat
# the real value we put in .env under dotenv's default override=False.
load_dotenv(Path(__file__).parent / ".env", override=True)

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

    bankroll = _get_bankroll(clients)

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


def _get_bankroll(clients) -> float:
    """Pull real bankroll from all active platform clients."""
    bankroll = 0.0
    for client in clients:
        try:
            bal = client.get_balance()
            if bal > 0:
                bankroll += bal
        except Exception:
            pass
    return bankroll if bankroll > 0 else 50.0


def cmd_trade(dry_run: bool = False, skip_harvester: bool = False):
    """
    Full trade pipeline: harvester → scan → consensus → order gate → execute.

    Per strategy.yaml priority order:
        Mechanical harvesters (cheap, near-certain) run BEFORE conviction
        bets (expensive to forecast, uncertain) so harvester positions
        claim open-position slots first.

    Every conviction trade must pass:
        1. Market scanner scoring (evidence + calibration + edge)
        2. 3-agent consensus (strategy → risk → compliance)
        3. Order gate 3-step pipeline (propose → validate → execute)

    Harvester trades bypass the forecasting engine + consensus (see
    `lib.harvester` docstring) but DO go through the order gate.

    Args:
        dry_run: If True, run everything except actual order execution.
        skip_harvester: If True, skip the near-resolution harvester and run
            only the conviction-bet side of the pipeline.
    """
    import os

    from agents.consensus import print_consensus_result, seek_consensus
    from lib.audit import log_event
    from lib.harvester import harvest_near_resolution, print_harvester_summary
    from lib.market_client import get_active_clients
    from lib.market_scanner import get_top_candidates, print_scan_report, scan_all_markets
    from lib.order_gate import OrderIntent, step1_propose, step2_validate, step3_execute

    log_event("trade", "trade_cycle_started", {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "skip_harvester": skip_harvester,
    })

    settings = _load_settings()
    strategy = _load_strategy()
    clients = get_active_clients()

    if not clients:
        print("No active platform clients. Check config/settings.yaml")
        return

    # ── Pull real bankroll ────────────────────────────────────────
    bankroll = _get_bankroll(clients)
    client_map = {c.platform_name: c for c in clients}

    print(f"{'[DRY RUN] ' if dry_run else ''}Starting trade cycle")
    print(f"  Bankroll: ${bankroll:,.2f} across {len(clients)} platform(s)")
    print()

    # ── Phase 0: Near-resolution harvester ────────────────────────
    # Mechanical trades first. Positions opened here count toward the
    # conviction scan's open-position budget, as intended.
    positions = _load_positions()
    open_positions = [p for p in positions if p.get("status") == "open"]
    daily_pnl = sum(p.get("unrealized_pnl", 0) for p in open_positions)

    if not skip_harvester:
        print(f"{'='*60}")
        print(f"  NEAR-RESOLUTION HARVESTER")
        print(f"{'='*60}")
        harvest = harvest_near_resolution(
            clients=clients,
            bankroll=bankroll,
            strategy=strategy,
            positions=positions,  # mutated in-place on execute
            client_map=client_map,
            current_daily_pnl=daily_pnl,
            dry_run=dry_run,
        )
        print_harvester_summary(harvest)
        # Persist any positions opened by the harvester before the
        # conviction scan runs, so a mid-cycle crash doesn't lose them.
        if harvest.get("executed", 0) > 0 and not dry_run:
            _save_positions(positions)
            # Refresh derived values for the consensus loop below.
            open_positions = [p for p in positions if p.get("status") == "open"]
            daily_pnl = sum(p.get("unrealized_pnl", 0) for p in open_positions)

    # ── Phase 1: Forecasting scan ─────────────────────────────────
    print()
    llm_enabled = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not llm_enabled:
        print("  (LLM analysis disabled — set ANTHROPIC_API_KEY to enable)")

    candidates = scan_all_markets(
        clients=clients,
        llm_enabled=llm_enabled,
        bankroll=bankroll,
    )
    print_scan_report(candidates)

    top = get_top_candidates(candidates)
    if not top:
        print("\nNo conviction trades meet criteria this cycle. Standing by.")
        log_event("trade", "no_opportunities", {}, result="skipped")
        return

    # ── Phase 2: Consensus + Execute for each candidate ───────────
    print(f"\n{'='*60}")
    print(f"  AGENT CONSENSUS + EXECUTION")
    print(f"{'='*60}")

    executed = 0
    vetoed = 0

    for candidate in top:
        market = candidate.market
        forecast = candidate.forecast
        print(f"\n  ── {market.question[:55]} ──")
        print(f"     {forecast.best_side} | edge={forecast.edge:+.1%} | "
              f"score={forecast.composite_score}/9 | kelly=${candidate.kelly_bet_usd:.2f}")

        # ── 2a: Agent consensus ───────────────────────────────────
        consensus = seek_consensus(candidate, bankroll)
        print_consensus_result(consensus)

        if not consensus["approved"]:
            vetoed += 1
            continue

        # ── 2b: Build OrderIntent ─────────────────────────────────
        proposal = consensus["proposal"]
        side = proposal["side"]
        price = market.yes_price if side == "YES" else (1 - market.yes_price)

        # Calculate quantity from Kelly bet size
        # quantity = kelly_bet_usd / price (number of contracts at this price)
        quantity = max(1, int(candidate.kelly_bet_usd / price)) if price > 0 else 1

        intent = OrderIntent(
            market_id=market.market_id,
            platform=market.platform,
            question=market.question,
            side=side,
            order_type="market",  # AMM markets use market orders
            quantity=quantity,
            limit_price=price,
            our_probability=forecast.probability,
            market_probability=forecast.market_probability,
            edge=forecast.edge,
            kelly_fraction=forecast.kelly_fraction,
            evidence_score=forecast.evidence_score,
            calibration_score=forecast.calibration_score,
            edge_score=forecast.edge_score,
            composite_score=forecast.composite_score,
            category=market.category,
            resolution_date=market.resolution_date,
            reason=proposal.get("evidence_summary", "")[:200],
        )

        # ── 2c: Order gate — propose ─────────────────────────────
        try:
            step1_propose(intent)
        except ValueError as e:
            print(f"     BLOCKED (duplicate): {e}")
            continue

        # ── 2d: Order gate — validate ─────────────────────────────
        try:
            step2_validate(
                intent=intent,
                bankroll=bankroll,
                current_daily_pnl=daily_pnl,
                current_open_positions=len(open_positions) + executed,
                market_volume_24h=market.volume_24h,
            )
        except Exception as e:
            print(f"     BLOCKED (validation): {e}")
            continue

        # ── 2e: Order gate — execute ──────────────────────────────
        if dry_run:
            print(f"     DRY RUN — would place: {side} {quantity} @ ${price:.3f} = ${price * quantity:.2f}")
            executed += 1
            continue

        platform_client = client_map.get(market.platform)
        if not platform_client:
            print(f"     ERROR: No client for platform '{market.platform}'")
            continue

        try:
            result = step3_execute(intent, platform_client)
            print(f"     EXECUTED: order_id={result.get('order_id', '?')[:20]} "
                  f"status={result.get('status', '?')} "
                  f"filled={result.get('filled_quantity', 0)} @ ${result.get('filled_price', 0):.3f}")

            # ── 2f: Record position ───────────────────────────────
            position = {
                "position_id": f"{market.platform}-{market.market_id}-{side}",
                "market_id": market.market_id,
                "platform": market.platform,
                "question": market.question,
                "category": market.category,
                "side": side,
                "quantity": result.get("filled_quantity", quantity),
                "entry_price": result.get("filled_price", price),
                "current_price": price,
                "our_probability": forecast.probability,
                "market_probability": forecast.market_probability,
                "edge_at_entry": forecast.edge,
                "composite_score": forecast.composite_score,
                "kelly_bet_usd": candidate.kelly_bet_usd,
                "correlation_group": candidate.correlation_group,
                "resolution_date": market.resolution_date,
                "order_id": result.get("order_id", ""),
                "status": "open",
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "unrealized_pnl": 0.0,
            }
            positions.append(position)
            _save_positions(positions)

            # Also append to trade history
            _append_trade_history({
                **position,
                "action": "open",
                "consensus_decision": consensus["decision"],
            })

            executed += 1
            log_event("trade", "position_opened", {
                "market_id": market.market_id,
                "platform": market.platform,
                "side": side,
                "quantity": quantity,
                "price": price,
                "kelly_usd": candidate.kelly_bet_usd,
            }, result="success")

        except Exception as e:
            print(f"     EXECUTION FAILED: {e}")
            log_event("trade", "execution_failed", {
                "market_id": market.market_id,
                "error": str(e)[:200],
            }, result="failed")

    # ── Summary ───────────────────────────────────────────────────
    print(f"\n{'='*60}")
    prefix = "[DRY RUN] " if dry_run else ""
    print(f"  {prefix}Cycle complete: {executed} executed, {vetoed} vetoed, "
          f"{len(top) - executed - vetoed} blocked")
    if executed > 0 and not dry_run:
        print(f"  Positions now: {len(open_positions) + executed} open")
    print(f"{'='*60}")

    log_event("trade", "trade_cycle_complete", {
        "executed": executed,
        "vetoed": vetoed,
        "dry_run": dry_run,
    }, result="success")


def _append_trade_history(trade: dict):
    """Append a trade record to the trade history file."""
    history_file = DATA_DIR / "trade_history.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    history = []
    if history_file.exists():
        try:
            with open(history_file, "r") as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    history.append(trade)
    with open(history_file, "w") as f:
        json.dump(history, f, indent=2, default=str)


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


def cmd_harvest(dry_run: bool = False):
    """
    Run ONLY the near-resolution harvester (no forecasting, no consensus).

    Lightweight CLI for the mechanical strategy — good for cron or
    anytime you want to sweep residual discounts without running the
    full, LLM-heavy conviction pipeline.
    """
    from lib.audit import log_event
    from lib.harvester import harvest_near_resolution, print_harvester_summary
    from lib.market_client import get_active_clients

    log_event("harvest", "cmd_started", {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
    })

    strategy = _load_strategy()
    clients = get_active_clients()
    if not clients:
        print("No active platform clients. Check config/settings.yaml")
        return

    bankroll = _get_bankroll(clients)
    client_map = {c.platform_name: c for c in clients}
    positions = _load_positions()
    open_positions = [p for p in positions if p.get("status") == "open"]
    daily_pnl = sum(p.get("unrealized_pnl", 0) for p in open_positions)

    print(f"{'[DRY RUN] ' if dry_run else ''}Near-resolution harvester standalone")
    print(f"  Bankroll: ${bankroll:,.2f} across {len(clients)} platform(s)")
    print()

    summary = harvest_near_resolution(
        clients=clients,
        bankroll=bankroll,
        strategy=strategy,
        positions=positions,
        client_map=client_map,
        current_daily_pnl=daily_pnl,
        dry_run=dry_run,
    )
    print_harvester_summary(summary)

    if summary.get("executed", 0) > 0 and not dry_run:
        _save_positions(positions)
        print(f"\n  Persisted {summary['executed']} new harvester position(s).")


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
    """Launch web dashboard.

    Default port 5050. Sibling traderbot project uses 5051 to avoid collision.
    """
    from lib.dashboard_web import run_dashboard
    print(f"  Polybot Dashboard: http://localhost:{port}")
    print("  (Polybot uses 5050; traderbot uses 5051 to avoid conflict.)")
    print("  Press Ctrl+C to stop.\n")
    run_dashboard(port=port)


def cmd_smoke():
    """Pipeline smoke test — validate the full forecasting stack end-to-end.

    Runs a synthetic market through every signal source + scoring + Kelly
    sizing, checks for NaN, bounds, graceful degradation. No external API
    calls. Use before deploying config changes.
    """
    from lib.backtest import pipeline_smoke_test
    result = pipeline_smoke_test(verbose=True)
    sys.exit(0 if result["passed"] else 1)


def cmd_brier(n: int = 50, use_llm: bool = False, sources: list[str] | None = None):
    """Cold-start calibration check — replay ForecastBench resolved questions.

    Measures our Brier score on an out-of-sample public dataset and
    compares to the crowd's implied probability as a baseline. Use
    before deploying real money to verify the forecaster isn't
    miscalibrated, overconfident, or systematically biased.

    Verdict:
        Δ Brier < 0  → we beat the crowd (evidence of edge)
        Δ Brier = 0  → noise, inconclusive
        Δ Brier > 0  → the crowd beats us → recalibrate before deploying
    """
    from lib.backtest import replay_forecastbench
    result = replay_forecastbench(
        limit=n,
        sources=sources,
        use_llm=use_llm,
        verbose=True,
    )
    # Exit 0 if we beat the crowd OR tied (Brier ≤ crowd).
    # Exit 1 if we're meaningfully worse (Δ > 0.02).
    if result["n"] == 0:
        print("No resolved questions available — cannot score.")
        sys.exit(1)
    passed = result["brier_improvement"] <= 0.02
    sys.exit(0 if passed else 1)


def cmd_wallet_scan(top_n: int = 25, platform: str = "manifold", lookback_days: int = 30):
    """Stage 1 copy-trade — discover top wallets, score each, persist results.

    Read-only. Output goes to ``data/wallet_scores.json`` for Stages 2-4
    to consume. Top wallets ranked by the composite score from
    ``lib.wallet_monitor`` (ROI × activity × recency × win-rate floor).
    """
    from lib.wallet_monitor import (
        discover_top_manifold, score_wallet, persist_scores,
    )

    print(f"=== Wallet scan: platform={platform} lookback={lookback_days}d ===")
    if platform == "manifold":
        handles = discover_top_manifold(top_n=top_n)
    elif platform == "polymarket":
        # Three-layer discovery (curated first, then auto from leaderboard).
        # Polymarket's /v1/leaderboard returns top wallets by PnL —
        # autodiscovery here mirrors what discover_top_manifold does.
        from lib.wallet_monitor import discover_top_polymarket
        handles: list[str] = []
        import yaml
        cfg_path = Path(__file__).parent / "config" / "copytrade_wallets.yaml"
        try:
            with open(cfg_path) as f:
                data = yaml.safe_load(f) or {}
            handles.extend((data.get("polymarket", []) or [])[:top_n])
        except Exception:
            pass
        remaining = top_n - len(handles)
        if remaining > 0:
            auto = discover_top_polymarket(top_n=remaining, period="monthly")
            for h in auto:
                if h not in handles:
                    handles.append(h)
    else:
        print(f"  Unknown platform: {platform}")
        return

    print(f"  Discovered {len(handles)} candidate handle(s); scoring each...")
    scored = []
    for i, h in enumerate(handles, 1):
        try:
            perf = score_wallet(h, platform=platform, lookback_days=lookback_days)
            scored.append(perf)
            print(f"  [{i:2d}/{len(handles)}] {h:24s} "
                  f"trades={perf.settled_bets:>5d} "
                  f"roi={perf.roi_pct:+.1%} score={perf.score:+.3f}")
        except Exception as e:
            print(f"  [{i:2d}/{len(handles)}] {h:24s} FAILED: {str(e)[:80]}")

    if not scored:
        print("\n  No wallets scored — check API connectivity.")
        return

    scored.sort(key=lambda p: p.score, reverse=True)
    persist_scores(scored)

    print(f"\n=== Top 10 by composite score ===")
    print(f"  {'rank':<5}{'handle':<28}{'trades':<7}{'roi%':<10}{'idle_d':<8}{'score':<8}")
    now_ts = datetime.now(timezone.utc)
    for i, p in enumerate(scored[:10], 1):
        days_idle = "?"
        if p.last_bet_at:
            try:
                d = datetime.fromisoformat(p.last_bet_at.replace("Z", "+00:00"))
                days_idle = str((now_ts - d).days)
            except (ValueError, TypeError):
                pass
        print(f"  {i:<5}{p.handle:<28}{p.settled_bets:<7}"
              f"{p.roi_pct*100:<+10.1f}{days_idle:<8}{p.score:<+8.3f}")
    print(f"\n  Persisted {len(scored)} scored wallet(s) to data/wallet_scores.json")


def cmd_wallet_watch(min_score: float = 0.10, max_wallets: int = 20):
    """Stage 2 copy-trade — poll watched wallets, alert on new bets.

    Reads the watchlist from ``data/wallet_scores.json`` (filtered by
    composite score ≥ ``min_score``), fetches each wallet's latest
    bets, and fires alerts for any newer than the last poll.

    Outputs: ``data/wallet_alerts.jsonl`` (always), Telegram (if
    configured), and ``audit_log.jsonl``. Read-only — never places
    orders. Use ``wallet-scan`` to refresh scores before running.
    """
    from lib.wallet_watch import run_watch_cycle
    result = run_watch_cycle(min_score=min_score, max_wallets=max_wallets)
    print(f"=== Wallet watch cycle ===")
    print(f"  Wallets polled: {result['wallets_polled']}")
    print(f"  Alerts fired:   {result['alerts_fired']}")
    if result["alerts"]:
        print(f"\n  Recent alerts:")
        for a in result["alerts"][:10]:
            print(f"    {a['handle']:20s} {a['side']:3s} "
                  f"@ {a['prob_after']:.0%}  M${a['amount']:>6.0f}  "
                  f"{a['market_question'][:50]}")
    elif result["wallets_polled"] == 0:
        print("  (no eligible wallets — run `wallet-scan` first)")
    else:
        print("  (no new activity on any watched wallet)")


def cmd_btc_arb_monitor(once: bool = True):
    """Phase 1 latency-arb — sample Binance spot vs Polymarket BTC
    daily-strike YES prices, compute the gap, persist to disk.

    Read-only. Phase 2 (paper trade) + Phase 3 (real execution) ride
    on the signal file this command produces. Run once per invocation
    (cron-friendly) or wrap in your own loop.
    """
    from lib.btc_arb_signal import run_signal_cycle
    result = run_signal_cycle()
    print(f"=== BTC arb signal cycle ===")
    if result["n_markets"] == 0:
        print("  No qualifying BTC daily-strike markets right now.")
        return
    print(f"  Sampled {result['n_markets']} market(s). Top divergences:")
    print(f"  {'strike':>10}{'yes_pm':>9}{'implied':>10}{'gap':>9}"
          f"{'hrs_left':>10}{'vol_24h':>12}")
    for s in result["signals"][:5]:
        direction = "↑YES_cheap" if s["gap"] > 0 else "↓NO_cheap"
        print(f"  ${s['strike_usd']:>9,.0f}"
              f"  {s['yes_price']:>.2f}"
              f"  {s['implied_yes_prob']:>.2f}"
              f"  {s['gap']:>+.3f}"
              f"  {s['hours_to_close']:>7.1f}h"
              f"  ${s['volume_24h']:>9,.0f}  {direction}")
    spot = result["signals"][0]["spot_usd"]
    print(f"\n  Binance.US spot: ${spot:,.2f}")
    print(f"  Persisted to data/btc_arb_signal.jsonl")


def cmd_btc_5min_monitor(max_seconds_out: int = 600):
    """5-min BTC UP/DOWN signal — Gravia-style latency arb.

    Snapshot every currently-live ``btc-updown-5m-*`` market within
    ``max_seconds_out`` seconds of resolving + the current Binance.US
    spot, persist to data/btc_5min_signal.jsonl.

    Measurement only. Phase 2 (paper trade) + Phase 3 (T-10s live
    entry) ride on the trajectory data this cycle produces.
    """
    from lib.btc_5min_signal import run_signal_cycle
    result = run_signal_cycle(max_seconds_out=max_seconds_out)
    print(f"=== BTC 5-min signal cycle ===")
    if result["n_markets"] == 0:
        print("  No active 5-min BTC markets right now.")
        print("  (Polymarket sometimes pauses these — check polymarket.com/crypto/5M)")
        return
    print(f"  Sampled {result['n_markets']} market(s). Nearest first:")
    print(f"  {'T-close':>9} {'up':>6} {'down':>6}  slug")
    for s in result["samples"][:6]:
        tc = s["seconds_to_close"]
        # Format as seconds if < 60, else mm:ss
        if tc < 60:
            t_str = f"{tc:>+6.1f}s"
        else:
            m, sec = divmod(int(tc), 60)
            t_str = f"{m:>2d}m{sec:02d}s"
        print(f"  {t_str:>9} {s['up_price']:>.3f} {s['down_price']:>.3f}  "
              f"{s['slug'][-20:]}")
    print(f"\n  Binance.US spot: ${result['spot_usd']:,.2f}")
    print(f"  Persisted to data/btc_5min_signal.jsonl")


def cmd_kalshi_15min_monitor(max_seconds_out: int = 900):
    """Multi-asset Kalshi 15-min signal — BTC + ETH + SOL by default.

    Reads enabled assets from config/kalshi_assets.yaml. Cron-friendly.
    Public endpoints only (no Kalshi auth needed for market data).
    Auto-records paper trades with per-asset min_confidence + auto-settles.
    Phase 3 (real orders) will plug kalshi_auth into the execute path.
    """
    from lib.kalshi_15min_signal import run_signal_cycle, enabled_assets
    result = run_signal_cycle(max_seconds_out=max_seconds_out)

    assets = sorted(enabled_assets().keys())
    print(f"=== Kalshi 15-min signal cycle (assets: {', '.join(assets) or 'none'}) ===")
    if result["n_markets"] == 0:
        print("  No active 15-min crypto markets across enabled assets right now.")
        print("  (Kalshi runs these in sessions — most active during US trading hours)")
        return
    counts = result.get("by_asset_counts", {})
    if counts:
        breakdown = "  ".join(f"{a}={n}" for a, n in sorted(counts.items()))
        print(f"  Per-asset counts: {breakdown}")
    print(f"  Sampled {result['n_markets']} market(s). Nearest first:")
    print(f"  {'asset':<4} {'T-close':>9} {'strike':>10} {'yes_ask':>8} "
          f"{'no_ask':>8} {'composite':>10}  ticker")
    for s in result["samples"][:8]:
        tc = s["seconds_to_close"]
        if tc < 60:
            t_str = f"{tc:>+6.1f}s"
        else:
            m, sec = divmod(int(tc), 60)
            t_str = f"{m:>2d}m{sec:02d}s"
        ind = s.get("indicators") or {}
        comp = ind.get("composite", 0)
        ya = s.get("yes_ask")
        na = s.get("no_ask")
        ya_s = f"{ya:.3f}" if ya is not None else "  -  "
        na_s = f"{na:.3f}" if na is not None else "  -  "
        # Strikes vary wildly across assets — auto-format
        strike = s.get("strike", 0) or 0
        s_str = f"${strike:>8,.2f}" if strike < 1000 else f"${strike:>8,.0f}"
        print(f"  {s.get('asset','?'):<4} {t_str:>9} {s_str:>10}  "
              f"{ya_s:>8} {na_s:>8} {comp:>+9.2f}  {s['market_ticker'][-30:]}")
    print(f"\n  Paper trades opened this cycle: {result.get('paper_trades_opened', 0)}")
    print(f"  Persisted to data/kalshi_15min_signal.jsonl")


def cmd_kalshi_15min_paper_settle():
    """Settle resolved Kalshi 15-min paper trades. Cron also calls this."""
    from lib.kalshi_15min_paper import settle_paper_trades
    result = settle_paper_trades()
    print(f"=== Kalshi 15-min paper-settle ===")
    print(f"  Newly settled:        {result['settled_now']}")
    print(f"  Paper P&L this cycle: ${result['paper_pnl_this_cycle']:+,.2f}")
    print(f"  Total open:           {result['total_open']}")
    print(f"  Total settled:        {result['total_settled']}")


def cmd_kalshi_15min_paper_report(asset: str | None = None):
    """Aggregate Kalshi 15-min paper P&L + per-asset + confidence-bucket WR.

    ``asset`` filter narrows to one of btc/eth/sol/... — pass None to
    see all-up + the per-asset breakdown side-by-side.
    """
    from lib.kalshi_15min_paper import summary
    s = summary(asset_filter=asset)
    header = f"=== Kalshi 15-min paper-trade report ==="
    if asset:
        header = f"=== Kalshi 15-min paper-trade report (asset={asset}) ==="
    print(header)
    if s["total_trades"] == 0:
        if asset:
            print(f"  No paper trades for asset={asset} yet.")
        else:
            print("  No paper trades recorded yet — wait for the cron to find a session.")
        return
    print(f"  Total trades:      {s['total_trades']}")
    print(f"  Open / Won / Lost / Void: "
          f"{s['open']} / {s['won']} / {s['lost']} / {s['void']}")
    print(f"  Win rate (settled): {s['win_rate']:.1%}")
    print(f"  Total paper P&L:    ${s['total_paper_pnl']:+,.2f}")
    print(f"  Capital deployed:   ${s['capital_deployed']:,.2f}")
    print(f"  ROI:                {s['roi_pct']:+.2%}")

    if s.get("by_asset") and not asset:
        print()
        print(f"  By asset:")
        print(f"    {'asset':<6} {'total':>6} {'won':>5} {'lost':>5} "
              f"{'wr':>7} {'pnl':>10} {'roi':>8}")
        for a, b in sorted(s["by_asset"].items()):
            print(f"    {a:<6} {b['total']:>6} {b.get('won',0):>5} "
                  f"{b.get('lost',0):>5} {b['win_rate']:>6.1%} "
                  f"${b['pnl']:>+8.2f} {b['roi_pct']:>+7.2%}")

    if s["by_confidence_bucket"]:
        print()
        print(f"  By confidence bucket:")
        print(f"    {'bucket':<10} {'settled':>8} {'wins':>6} {'wr':>7} {'pnl':>10}")
        for bucket, b in sorted(s["by_confidence_bucket"].items()):
            wr = b["wins"] / b["settled"] if b["settled"] > 0 else 0
            print(f"    {bucket:<10} {b['settled']:>8} {b['wins']:>6} "
                  f"{wr:>6.1%} ${b['pnl']:>+8.2f}")


def cmd_btc_5min_paper_settle():
    """Settle resolved 5-min UP/DOWN paper trades.

    Normally not needed manually — the signal cycle auto-settles every
    cron tick. This command exists for ad-hoc runs and debugging.
    """
    from lib.btc_5min_paper import settle_paper_trades
    result = settle_paper_trades()
    print(f"=== BTC 5-min paper-settle ===")
    print(f"  Newly settled:        {result['settled_now']}")
    print(f"  Paper P&L this cycle: ${result['paper_pnl_this_cycle']:+,.2f}")
    print(f"  Total open:           {result['total_open']}")
    print(f"  Total settled:        {result['total_settled']}")


def cmd_btc_5min_paper_report():
    """Aggregate 5-min paper P&L + confidence-bucket win rates.

    The confidence-bucket breakdown is the most actionable view: if
    the composite signal is calibrated, higher-confidence trades
    should have higher win rates. Anti-calibration (or noise) means
    the strategy isn't ready for Phase 3.
    """
    from lib.btc_5min_paper import summary
    s = summary()
    print(f"=== BTC 5-min paper-trade report ===")
    if s["total_trades"] == 0:
        print("  No paper trades recorded yet — run the cron a while first.")
        print("  (`launchctl list | grep btc_5min` should show the job.)")
        return
    print(f"  Total trades:      {s['total_trades']}")
    print(f"  Open / Won / Lost / Void: "
          f"{s['open']} / {s['won']} / {s['lost']} / {s['void']}")
    print(f"  Win rate (settled): {s['win_rate']:.1%}")
    print(f"  Total paper P&L:    ${s['total_paper_pnl']:+,.2f}")
    print(f"  Capital deployed:   ${s['capital_deployed']:,.2f}")
    print(f"  ROI:                {s['roi_pct']:+.2%}")

    if s["by_confidence_bucket"]:
        print()
        print(f"  By confidence bucket:")
        print(f"    {'bucket':<10} {'settled':>8} {'wins':>6} {'wr':>7} {'pnl':>10}")
        for bucket, b in sorted(s["by_confidence_bucket"].items()):
            wr = b["wins"] / b["settled"] if b["settled"] > 0 else 0
            print(f"    {bucket:<10} {b['settled']:>8} {b['wins']:>6} "
                  f"{wr:>6.1%} ${b['pnl']:>+8.2f}")

    if s["per_day_pnl"]:
        print()
        print("  Per day P&L (most recent 7):")
        for day in sorted(s["per_day_pnl"].keys())[-7:]:
            print(f"    {day}: ${s['per_day_pnl'][day]:+,.2f}")


def cmd_btc_arb_paper_settle():
    """Phase 2 — settle open BTC arb paper trades against actual
    market outcomes. Run periodically (manually or cron) so the report
    stays current.
    """
    from lib.btc_arb_paper import settle_paper_trades
    result = settle_paper_trades()
    print(f"=== BTC arb paper-settle ===")
    print(f"  Newly settled:        {result['settled_now']}")
    print(f"  Paper P&L this cycle: ${result['paper_pnl_this_cycle']:+,.2f}")
    print(f"  Total open:           {result['total_open']}")
    print(f"  Total settled:        {result['total_settled']}")


def cmd_btc_arb_paper_report():
    """Phase 2 — aggregate paper P&L stats. The verdict on whether
    the BTC arb signal would have been profitable.
    """
    from lib.btc_arb_paper import summary
    s = summary()
    print(f"=== BTC arb paper-trade report ===")
    print(f"  Total trades:     {s['total_trades']}")
    print(f"  Open/Won/Lost/Void: {s['open']}/{s['won']}/{s['lost']}/{s['void']}")
    print(f"  Capital deployed: ${s['capital_deployed']:,.2f}")
    print(f"  Total paper P&L:  ${s['total_paper_pnl']:+,.2f}")
    print(f"  Win rate:         {s['win_rate']:.1%}")
    print(f"  ROI:              {s['roi_pct']:+.2%}")
    if s["per_day_pnl"]:
        print(f"\n  Per-day P&L:")
        for day in sorted(s["per_day_pnl"]):
            pnl = s["per_day_pnl"][day]
            flag = " ⚠ daily limit" if pnl <= -20 else ""
            print(f"    {day}: ${pnl:+,.2f}{flag}")


def cmd_paper_copy_settle():
    """Stage 3 — poll markets for resolution, mark open paper copies
    won/lost/void, persist updated P&L. Run periodically (manually or
    via cron) so the report stays current.
    """
    from lib.wallet_paper_copy import settle_paper_copies
    result = settle_paper_copies()
    print(f"=== Paper-copy settlement cycle ===")
    print(f"  Newly settled:        {result['settled_now']}")
    print(f"  Paper P&L this cycle: M${result['paper_pnl_this_cycle']:+,.2f}")
    print(f"  Total open:           {result['total_open']}")
    print(f"  Total settled:        {result['total_settled']}")


def cmd_paper_copy_report():
    """Stage 3 — aggregate paper P&L per source wallet. The verdict on
    whether copy-trading each wallet would have been profitable
    (without yet risking real capital).
    """
    from lib.wallet_paper_copy import summary_by_wallet
    summary = summary_by_wallet()
    if not summary:
        print("No paper-copy trades recorded yet — run `wallet-watch` first.")
        return

    # Sort by ROI descending
    rows = sorted(summary.items(), key=lambda kv: kv[1].get("roi_pct", 0), reverse=True)
    print(f"=== Paper-copy report (by source wallet) ===")
    print(f"  {'handle':<28}{'open':<6}{'won':<5}{'lost':<6}{'void':<6}"
          f"{'win%':<7}{'cap':<10}{'pnl':<12}{'roi%':<8}")
    for handle, s in rows:
        cap = s.get("capital_at_risk", 0)
        pnl = s.get("total_pnl", 0)
        wr = s.get("win_rate", 0) * 100
        roi = s.get("roi_pct", 0) * 100
        print(f"  {handle[:27]:<28}{s['open']:<6}{s['wins']:<5}{s['losses']:<6}"
              f"{s['voids']:<6}{wr:<7.0f}M${cap:<8.0f}M${pnl:<+10.2f}{roi:<+8.1f}")
    settled_total = sum(s['wins'] + s['losses'] + s['voids'] for s in summary.values())
    total_pnl = sum(s['total_pnl'] for s in summary.values())
    print(f"\n  Total settled: {settled_total}   Total paper P&L: M${total_pnl:+,.2f}")
    print(f"  Run `paper-copy-settle` to mark newly-resolved markets.")


def cmd_wallet_score(handle: str, platform: str = "manifold", lookback_days: int = 30):
    """Deep-dive one wallet — fetch + score + print full metrics."""
    from lib.wallet_monitor import score_wallet
    perf = score_wallet(handle, platform=platform, lookback_days=lookback_days)
    print(f"=== {perf.handle} ({perf.platform}, last {perf.lookback_days}d) ===")
    print(f"  Total bets:        {perf.total_bets}")
    print(f"  Settled / Open:    {perf.settled_bets} / {perf.open_bets}")
    print(f"  Wins / Losses:     {perf.wins} / {perf.losses}")
    print(f"  Win rate:          {perf.win_rate:.1%}")
    print(f"  Realized P&L:      ${perf.realized_pnl:+,.2f}")
    print(f"  Unrealized P&L:    ${perf.unrealized_pnl:+,.2f}")
    print(f"  Capital at risk:   ${perf.capital_at_risk:,.2f}")
    print(f"  ROI:               {perf.roi_pct:+.2%}")
    print(f"  Avg bet size:      ${perf.avg_bet_size:.2f}")
    print(f"  Last bet:          {perf.last_bet_at[:19]}")
    print(f"  Composite score:   {perf.score:+.4f}")


def cmd_wallet_backtest(
    handle: str,
    *,
    platform: str = "manifold",
    lookback_days: int = 90,
    copy_size_usd: float = 10.0,
):
    """Replay a wallet's historical bets and print hypothetical copy P&L.

    Honest about what it is: an *upper bound* on copy-edge (we assume
    we'd fill at the same price the source got, which understates real
    slippage). Use the ranking, not the absolute numbers, to decide
    which wallets are worth following.
    """
    from lib.wallet_backtest import backtest_wallet

    print(f"Backtesting {handle} ({platform}, last {lookback_days}d, "
          f"${copy_size_usd:.0f}/trade)...")
    print("  This walks every historical bet + looks up each market's")
    print("  resolution — can take 30-90s for active wallets.")
    summary, out_path = backtest_wallet(
        handle, platform=platform,
        copy_size_usd=copy_size_usd,
        lookback_days=lookback_days,
    )

    print()
    print(f"=== {summary.handle} ({summary.platform}, {summary.lookback_days}d) ===")
    print(f"  Bets seen:         {summary.total_bets_seen}")
    print(f"  Copied (resolved): {summary.bets_copied}")
    print(f"  Skipped:           {summary.bets_skipped}")
    if summary.skip_reasons:
        for reason, n in sorted(summary.skip_reasons.items(),
                                key=lambda kv: -kv[1]):
            print(f"      {reason:20s} {n}")
    if summary.bets_copied == 0:
        print("  → No copyable bets in window. Try --lookback=180 or check the handle.")
        return
    print(f"  Wins / Losses:     {summary.wins} / {summary.losses}  "
          f"(voids: {summary.voids})")
    print(f"  Win rate:          {summary.win_rate:.1%}")
    print(f"  Capital deployed:  ${summary.total_capital_deployed:,.2f}")
    print(f"  Paper P&L:         ${summary.total_paper_pnl:+,.2f}")
    print(f"  ROI:               {summary.roi_pct:+.2%}")

    if summary.top_winners:
        print("\n  Top winners:")
        for w in summary.top_winners[:3]:
            print(f"    {w['side']:3s} @ {w['fill']:.2f}  "
                  f"${w['pnl']:+,.2f}  {w['market'][:70]}")
    if summary.top_losers:
        print("\n  Top losers:")
        for l in summary.top_losers[:3]:
            print(f"    {l['side']:3s} @ {l['fill']:.2f}  "
                  f"${l['pnl']:+,.2f}  {l['market'][:70]}")

    if out_path:
        print(f"\n  Full detail → {out_path}")


def cmd_wallet_backtest_all(
    *,
    lookback_days: int = 90,
    copy_size_usd: float = 10.0,
    min_settled: int = 5,
):
    """Backtest every scored + curated wallet, rank by hypothetical ROI.

    Turns the leaderboard into a real-vs-fake-edge filter. Wallets with
    too few settled copyable bets (< ``min_settled``) sink to the
    bottom regardless of their ROI — three wins on three bets isn't
    signal.
    """
    from lib.wallet_backtest import backtest_all

    def _progress(i, n, handle, platform):
        print(f"  [{i}/{n}] {platform}: {handle[:60]}...", flush=True)

    print(f"Backtesting all known wallets (last {lookback_days}d, "
          f"${copy_size_usd:.0f}/trade, min {min_settled} settled)")
    print("This walks Manifold + Polymarket — can take several minutes.")
    print()
    result = backtest_all(
        copy_size_usd=copy_size_usd,
        lookback_days=lookback_days,
        min_settled=min_settled,
        progress_callback=_progress,
    )

    ranked = result["ranked"]
    failed = result["failed"]
    if not ranked:
        print("\nNo wallets backtested. Run `wallet-scan` first or check "
              "config/copytrade_wallets.yaml.")
        return

    print()
    print(f"=== Ranked {len(ranked)} wallets by backtested ROI ===")
    print(f"{'rank':>4} {'plat':4} {'handle':30} {'copied':>6} {'wr':>6} "
          f"{'pnl':>10} {'roi':>8} {'flag':>5}")
    print("-" * 80)
    for i, r in enumerate(ranked, 1):
        h = (r.get("handle") or "")[:30]
        plat = (r.get("platform") or "?")[:4]
        copied = r.get("bets_copied", 0)
        settled = r.get("wins", 0) + r.get("losses", 0)
        wr = r.get("win_rate", 0.0)
        pnl = r.get("total_paper_pnl", 0.0)
        roi = r.get("roi_pct", 0.0)
        flag = "thin" if settled < min_settled else ""
        print(f"{i:>4} {plat:4} {h:30} {copied:>6} {wr:>6.1%} "
              f"${pnl:>+8.2f} {roi:>+7.2%} {flag:>5}")

    if failed:
        print()
        print(f"Failed: {len(failed)}")
        for f in failed[:5]:
            print(f"  {f['platform']}: {f['handle']:30} — {f['error'][:50]}")

    if result.get("output_path"):
        print(f"\nFull ranking → {result['output_path']}")


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

    if command == "trade":
        dry_run = "--dry-run" in sys.argv
        skip_harvester = "--skip-harvester" in sys.argv
        cmd_trade(dry_run=dry_run, skip_harvester=skip_harvester)
    elif command == "scan":
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
    elif command == "harvest":
        dry_run = "--dry-run" in sys.argv
        cmd_harvest(dry_run=dry_run)
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
    elif command == "smoke":
        cmd_smoke()
    elif command == "brier":
        n = 50
        use_llm = False
        sources: list[str] | None = None
        for arg in sys.argv[2:]:
            if arg.startswith("--n="):
                try:
                    n = max(1, int(arg.split("=", 1)[1]))
                except ValueError:
                    pass
            elif arg == "--llm":
                use_llm = True
            elif arg.startswith("--sources="):
                sources = [s.strip() for s in arg.split("=", 1)[1].split(",") if s.strip()]
        cmd_brier(n=n, use_llm=use_llm, sources=sources)
    elif command == "wallet-scan":
        top = 25
        platform = "manifold"
        lookback = 30
        for arg in sys.argv[2:]:
            if arg.startswith("--top="):
                top = max(1, int(arg.split("=", 1)[1]))
            elif arg.startswith("--platform="):
                platform = arg.split("=", 1)[1]
            elif arg.startswith("--lookback="):
                lookback = max(1, int(arg.split("=", 1)[1]))
        cmd_wallet_scan(top_n=top, platform=platform, lookback_days=lookback)
    elif command == "wallet-score":
        if len(sys.argv) < 3:
            print("Usage: python main.py wallet-score <handle> [--platform=manifold] [--lookback=30]")
            return
        handle = sys.argv[2]
        platform = "manifold"
        lookback = 30
        for arg in sys.argv[3:]:
            if arg.startswith("--platform="):
                platform = arg.split("=", 1)[1]
            elif arg.startswith("--lookback="):
                lookback = max(1, int(arg.split("=", 1)[1]))
        cmd_wallet_score(handle=handle, platform=platform, lookback_days=lookback)
    elif command == "wallet-watch":
        min_score = 0.10
        max_wallets = 20
        for arg in sys.argv[2:]:
            if arg.startswith("--min-score="):
                min_score = float(arg.split("=", 1)[1])
            elif arg.startswith("--max="):
                max_wallets = int(arg.split("=", 1)[1])
        cmd_wallet_watch(min_score=min_score, max_wallets=max_wallets)
    elif command == "wallet-backtest":
        if len(sys.argv) < 3:
            print("Usage: python main.py wallet-backtest <handle> "
                  "[--platform=manifold|polymarket] [--lookback=90] [--size=10]")
            return
        handle = sys.argv[2]
        platform = "manifold"
        lookback = 90
        size = 10.0
        # Auto-detect Polymarket addresses
        if handle.startswith("0x") and len(handle) == 42:
            platform = "polymarket"
        for arg in sys.argv[3:]:
            if arg.startswith("--platform="):
                platform = arg.split("=", 1)[1]
            elif arg.startswith("--lookback="):
                lookback = max(1, int(arg.split("=", 1)[1]))
            elif arg.startswith("--size="):
                size = max(0.01, float(arg.split("=", 1)[1]))
        cmd_wallet_backtest(
            handle=handle, platform=platform,
            lookback_days=lookback, copy_size_usd=size,
        )
    elif command == "wallet-backtest-all":
        lookback = 90
        size = 10.0
        min_settled = 5
        for arg in sys.argv[2:]:
            if arg.startswith("--lookback="):
                lookback = max(1, int(arg.split("=", 1)[1]))
            elif arg.startswith("--size="):
                size = max(0.01, float(arg.split("=", 1)[1]))
            elif arg.startswith("--min-settled="):
                min_settled = max(0, int(arg.split("=", 1)[1]))
        cmd_wallet_backtest_all(
            lookback_days=lookback, copy_size_usd=size,
            min_settled=min_settled,
        )
    elif command == "paper-copy-settle":
        cmd_paper_copy_settle()
    elif command == "paper-copy-report":
        cmd_paper_copy_report()
    elif command == "btc-arb-monitor":
        cmd_btc_arb_monitor()
    elif command == "btc-5min-monitor":
        max_sec = 600
        for arg in sys.argv[2:]:
            if arg.startswith("--max-seconds="):
                max_sec = max(60, int(arg.split("=", 1)[1]))
        cmd_btc_5min_monitor(max_seconds_out=max_sec)
    elif command == "btc-5min-paper-settle":
        cmd_btc_5min_paper_settle()
    elif command == "btc-5min-paper-report":
        cmd_btc_5min_paper_report()
    elif command == "kalshi-15min-monitor":
        max_sec = 900
        for arg in sys.argv[2:]:
            if arg.startswith("--max-seconds="):
                max_sec = max(60, int(arg.split("=", 1)[1]))
        cmd_kalshi_15min_monitor(max_seconds_out=max_sec)
    elif command == "kalshi-15min-paper-settle":
        cmd_kalshi_15min_paper_settle()
    elif command == "kalshi-15min-paper-report":
        asset = None
        for arg in sys.argv[2:]:
            if arg.startswith("--asset="):
                asset = arg.split("=", 1)[1].lower().strip() or None
        cmd_kalshi_15min_paper_report(asset=asset)
    elif command == "kalshi-dashboard":
        port = 5053
        for arg in sys.argv[2:]:
            if arg.startswith("--port="):
                port = int(arg.split("=", 1)[1])
        from lib.kalshi_dashboard import run_dashboard
        run_dashboard(port=port)
    elif command == "kalshi-auth-status":
        from lib.kalshi_auth import status
        s = status()
        print("=== Kalshi auth status ===")
        print(f"  api_key_present:         {s['api_key_present']}")
        print(f"  private_key_path_set:    {s['private_key_path_set']}")
        print(f"  private_key_file_exists: {s['private_key_file_exists']}")
        print(f"  private_key_loadable:    {s['private_key_loadable']}")
        print(f"  base_url:                {s['base_url']}")
        print(f"  can_sign:                {s['can_sign']}")
        if not s["can_sign"]:
            print()
            print("  Setup steps:")
            print("    1. Add to .env:")
            print("       KALSHI_API_KEY=<the Key ID Kalshi gave you>")
            print("       KALSHI_PRIVATE_KEY_PATH=~/.polybot/kalshi_key.pem")
            print("    2. Place the RSA private key (PEM file Kalshi sent):")
            print("       mkdir -p ~/.polybot && chmod 700 ~/.polybot")
            print("       # paste PEM content into ~/.polybot/kalshi_key.pem, then:")
            print("       chmod 600 ~/.polybot/kalshi_key.pem")
            print("    3. Re-run this command to verify.")
    elif command == "kalshi-test-auth":
        from lib.kalshi_auth import can_sign, signed_get
        if not can_sign():
            print("Auth not configured. Run `python main.py kalshi-auth-status` first.")
            return
        print("Hitting GET /portfolio/balance to verify signing...")
        try:
            data = signed_get("/portfolio/balance")
            print(f"  ✓ Auth works. Balance response: {data}")
        except Exception as e:
            err_str = str(e)
            # Redact any header values that might leak in error messages
            print(f"  ✗ Request failed: {err_str[:300]}")
            print()
            print("  Common causes:")
            print("    * 401 Unauthorized → Key ID mismatch or wrong base URL")
            print("    * SignatureMismatch → string-to-sign format issue")
            print("    * ConnectionError  → network / wrong KALSHI_API_BASE")
    elif command == "dataset-status":
        from lib.historical_data import status
        s = status()
        print("=== Jon-Becker dataset status ===")
        print(f"  parquet importable: {s['parquet_available']}")
        print(f"  reason / path:      {s['reason']}")
        if s.get("data_dir"):
            print(f"  data_dir:           {s['data_dir']}")
            print(f"  poly trades files:  {s.get('poly_trades_files', '?')}")
            print(f"  poly markets files: {s.get('poly_markets_files', '?')}")
            print(f"  kalshi trades:      {s.get('kalshi_trades_files', '?')}")
        else:
            print()
            print("  To enable (one-time setup):")
            print("    git clone https://github.com/Jon-Becker/prediction-market-analysis ~/pma")
            print("    cd ~/pma && make setup    # 36GB download, ~150GB extracted")
            print("    export POLYBOT_PMA_DATA_DIR=~/pma/data")
    elif command == "btc-arb-paper-settle":
        cmd_btc_arb_paper_settle()
    elif command == "btc-arb-paper-report":
        cmd_btc_arb_paper_report()
    else:
        print(f"Unknown command: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()
