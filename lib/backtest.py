"""
Backtesting Engine — historical replay + Monte Carlo simulation.

Two modes:
    1. Historical Replay: feed recorded forecasts back through the pipeline,
       check what would have happened if we followed the strategy exactly.
    2. Monte Carlo: simulate thousands of bankroll paths given our calibration
       data to estimate ruin probability, expected growth, and time-to-target.

This is how we validate before putting real money at risk.

Security:
    - All file reads use safe JSON loading with size limits
    - No external API calls — purely local computation
    - Results logged to audit trail
    - No secrets in simulation data
"""

import json
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from lib.audit import log_event
from lib.kelly import expected_value, fractional_kelly, kelly_bet_size

DATA_DIR = Path(__file__).parent.parent / "data"
CONFIG_DIR = Path(__file__).parent.parent / "config"
BACKTEST_DIR = DATA_DIR / "backtests"

MAX_FILE_SIZE = 10_000_000  # 10MB — refuse to load anything larger


def _load_strategy() -> dict:
    with open(CONFIG_DIR / "strategy.yaml", "r") as f:
        return yaml.safe_load(f)


def _safe_load_json(path: Path, max_size: int = MAX_FILE_SIZE) -> list | dict:
    """Load JSON with size guard."""
    if not path.exists():
        return []
    if path.stat().st_size > max_size:
        raise ValueError(f"File too large: {path} ({path.stat().st_size} bytes)")
    with open(path, "r") as f:
        return json.load(f)


# ── Data Structures ──────────────────────────────────────────────

@dataclass
class SimulatedTrade:
    """A single trade in a backtest or Monte Carlo run."""
    market_id: str
    side: str                      # YES or NO
    our_prob: float
    market_prob: float
    bet_size: float
    outcome: bool                  # True = won
    pnl: float                     # Dollar P/L after fees
    bankroll_after: float
    fee_paid: float
    edge: float
    composite_score: int = 0


@dataclass
class BacktestResult:
    """Results of a historical replay or Monte Carlo simulation."""
    mode: str                      # "historical" or "monte_carlo"
    starting_bankroll: float
    ending_bankroll: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    total_pnl: float
    total_fees: float
    max_drawdown: float            # Worst peak-to-trough decline
    max_drawdown_pct: float
    sharpe_ratio: float            # Risk-adjusted return (annualized)
    time_to_target: int | None     # Trades needed to reach target (MC only)
    ruin_probability: float        # P(bankroll < $5) across MC paths
    trades: list[SimulatedTrade] = field(default_factory=list)
    bankroll_curve: list[float] = field(default_factory=list)
    # Monte Carlo specific
    mc_paths: int = 0
    mc_median_final: float = 0.0
    mc_p10_final: float = 0.0      # 10th percentile (bad luck)
    mc_p90_final: float = 0.0      # 90th percentile (good luck)


# ── Fee Calculation ──────────────────────────────────────────────

PLATFORM_FEES = {
    "kalshi": 0.07,
    "polymarket": 0.02,
    "manifold": 0.0,
}


def _calculate_trade_pnl(
    side: str,
    our_prob: float,
    market_prob: float,
    bet_size: float,
    outcome: bool,
    fee_rate: float,
) -> tuple[float, float]:
    """
    Calculate P/L and fees for a single trade.

    Returns:
        (pnl, fee_paid) tuple.
    """
    if side == "YES":
        contracts = bet_size / market_prob if market_prob > 0 else 0
        if outcome:
            gross = contracts * (1.0 - market_prob)
            fee = gross * fee_rate
            pnl = gross - fee
        else:
            pnl = -bet_size
            fee = 0.0
    else:
        no_price = 1.0 - market_prob
        contracts = bet_size / no_price if no_price > 0 else 0
        if not outcome:
            gross = contracts * market_prob
            fee = gross * fee_rate
            pnl = gross - fee
        else:
            pnl = -bet_size
            fee = 0.0

    return round(pnl, 4), round(fee, 4)


# ── Historical Replay ────────────────────────────────────────────

def replay_historical(
    starting_bankroll: float = 50.0,
    kelly_multiplier: float | None = None,
    max_per_market_pct: float | None = None,
    min_composite_score: int | None = None,
    min_edge: float | None = None,
) -> BacktestResult:
    """
    Replay all resolved forecasts through the strategy, simulating
    what our bankroll path would have been.

    Uses recorded forecasts from calibration_log.json — each has
    our_probability, market_probability, side, outcome, and sources.

    Args:
        starting_bankroll: Initial bankroll to simulate from
        kelly_multiplier: Override Kelly fraction (default from strategy)
        max_per_market_pct: Override max per-market bet size
        min_composite_score: Override minimum composite score filter
        min_edge: Override minimum edge filter

    Returns:
        BacktestResult with full trade log and bankroll curve.
    """
    strategy = _load_strategy()

    if kelly_multiplier is None:
        kelly_multiplier = strategy.get("kelly_multiplier", 0.25)
    if max_per_market_pct is None:
        max_per_market_pct = strategy.get("max_per_market_pct", 0.15)
    if min_composite_score is None:
        min_composite_score = strategy.get("scoring", {}).get("min_composite_score", 6)
    if min_edge is None:
        min_edge = strategy.get("scoring", {}).get("min_edge", 0.08)

    # Load resolved forecasts
    cal_file = DATA_DIR / "calibration_log.json"
    forecasts = _safe_load_json(cal_file)
    if not isinstance(forecasts, list):
        forecasts = []

    resolved = [f for f in forecasts if f.get("outcome") is not None]
    if not resolved:
        return BacktestResult(
            mode="historical",
            starting_bankroll=starting_bankroll,
            ending_bankroll=starting_bankroll,
            total_trades=0, wins=0, losses=0, win_rate=0.0,
            total_pnl=0.0, total_fees=0.0,
            max_drawdown=0.0, max_drawdown_pct=0.0,
            sharpe_ratio=0.0, time_to_target=None,
            ruin_probability=0.0,
        )

    # Sort by timestamp
    resolved.sort(key=lambda f: f.get("timestamp", ""))

    bankroll = starting_bankroll
    peak = bankroll
    max_dd = 0.0
    trades: list[SimulatedTrade] = []
    curve: list[float] = [bankroll]
    returns: list[float] = []

    for f in resolved:
        our_prob = f.get("our_probability", 0.5)
        market_prob = f.get("market_probability", 0.5)
        side = f.get("side", "YES")
        outcome = f.get("outcome", False)
        platform = f.get("platform", "kalshi")

        # Calculate edge
        if side == "YES":
            edge = our_prob - market_prob
        else:
            edge = (1.0 - our_prob) - (1.0 - market_prob)

        # Apply filters
        if abs(edge) < min_edge:
            continue

        # Kelly sizing
        if side == "YES":
            bet = kelly_bet_size(bankroll, our_prob, market_prob,
                                 fraction=kelly_multiplier,
                                 max_per_market_pct=max_per_market_pct)
        else:
            bet = kelly_bet_size(bankroll, 1.0 - our_prob, 1.0 - market_prob,
                                 fraction=kelly_multiplier,
                                 max_per_market_pct=max_per_market_pct)

        if bet <= 0:
            continue

        # Minimum bet ($1 or available bankroll)
        bet = max(bet, min(1.0, bankroll))
        bet = min(bet, bankroll)

        if bankroll < 1.0:
            break  # Ruin

        # Execute trade
        fee_rate = PLATFORM_FEES.get(platform, 0.07)
        pnl, fee_paid = _calculate_trade_pnl(side, our_prob, market_prob, bet, outcome, fee_rate)

        bankroll += pnl
        bankroll = max(bankroll, 0.0)

        # Track drawdown
        peak = max(peak, bankroll)
        dd = peak - bankroll
        max_dd = max(max_dd, dd)

        # Track return for Sharpe
        ret = pnl / bet if bet > 0 else 0.0
        returns.append(ret)

        trade = SimulatedTrade(
            market_id=f.get("market_id", "unknown"),
            side=side,
            our_prob=our_prob,
            market_prob=market_prob,
            bet_size=bet,
            outcome=outcome,
            pnl=pnl,
            bankroll_after=round(bankroll, 2),
            fee_paid=fee_paid,
            edge=round(edge, 4),
        )
        trades.append(trade)
        curve.append(round(bankroll, 2))

    # Compute summary stats
    wins = sum(1 for t in trades if t.outcome)
    losses = len(trades) - wins
    win_rate = wins / len(trades) if trades else 0.0
    total_pnl = bankroll - starting_bankroll
    total_fees = sum(t.fee_paid for t in trades)
    max_dd_pct = max_dd / peak if peak > 0 else 0.0

    # Sharpe ratio (annualized assuming ~2 trades/day, 365 trading days)
    if len(returns) >= 2:
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_ret = math.sqrt(variance) if variance > 0 else 1.0
        sharpe = (mean_ret / std_ret) * math.sqrt(730)  # ~730 trades/year
    else:
        sharpe = 0.0

    result = BacktestResult(
        mode="historical",
        starting_bankroll=starting_bankroll,
        ending_bankroll=round(bankroll, 2),
        total_trades=len(trades),
        wins=wins,
        losses=losses,
        win_rate=round(win_rate, 4),
        total_pnl=round(total_pnl, 2),
        total_fees=round(total_fees, 2),
        max_drawdown=round(max_dd, 2),
        max_drawdown_pct=round(max_dd_pct, 4),
        sharpe_ratio=round(sharpe, 2),
        time_to_target=None,
        ruin_probability=1.0 if bankroll < 5.0 else 0.0,
        trades=trades,
        bankroll_curve=curve,
    )

    log_event("backtest", "historical_replay", {
        "starting_bankroll": starting_bankroll,
        "ending_bankroll": result.ending_bankroll,
        "total_trades": result.total_trades,
        "win_rate": result.win_rate,
        "total_pnl": result.total_pnl,
        "max_drawdown_pct": result.max_drawdown_pct,
        "sharpe": result.sharpe_ratio,
    }, result="success")

    return result


# ── Monte Carlo Simulation ───────────────────────────────────────

def monte_carlo(
    starting_bankroll: float = 50.0,
    target_bankroll: float = 25000.0,
    num_paths: int = 1000,
    trades_per_path: int = 500,
    win_rate: float | None = None,
    avg_edge: float | None = None,
    kelly_multiplier: float | None = None,
    max_per_market_pct: float | None = None,
    fee_rate: float = 0.05,
    seed: int | None = None,
) -> BacktestResult:
    """
    Monte Carlo simulation of bankroll growth paths.

    Simulates `num_paths` independent bankroll trajectories, each making
    `trades_per_path` trades with the given win rate and edge.

    If win_rate and avg_edge are not provided, they're estimated from
    historical calibration data.

    Args:
        starting_bankroll: Initial bankroll
        target_bankroll: Goal bankroll ($25,000)
        num_paths: Number of simulation paths
        trades_per_path: Trades per path
        win_rate: Probability of winning each trade (estimated from data if None)
        avg_edge: Average edge per trade in probability points
        kelly_multiplier: Kelly fraction
        max_per_market_pct: Max bet size as fraction of bankroll
        fee_rate: Average platform fee rate
        seed: Random seed for reproducibility

    Returns:
        BacktestResult with Monte Carlo statistics.
    """
    if seed is not None:
        random.seed(seed)

    strategy = _load_strategy()

    if kelly_multiplier is None:
        kelly_multiplier = strategy.get("kelly_multiplier", 0.25)
    if max_per_market_pct is None:
        max_per_market_pct = strategy.get("max_per_market_pct", 0.15)

    # Estimate parameters from historical data if not provided
    if win_rate is None or avg_edge is None:
        cal_file = DATA_DIR / "calibration_log.json"
        forecasts = _safe_load_json(cal_file) if cal_file.exists() else []
        resolved = [f for f in forecasts if f.get("outcome") is not None]

        if resolved:
            wins = sum(1 for f in resolved
                       if (f.get("side") == "YES" and f["outcome"])
                       or (f.get("side") == "NO" and not f["outcome"]))
            if win_rate is None:
                win_rate = wins / len(resolved)

            if avg_edge is None:
                edges = []
                for f in resolved:
                    mp = f.get("market_probability", 0.5)
                    op = f.get("our_probability", 0.5)
                    if f.get("side") == "YES":
                        edges.append(op - mp)
                    else:
                        edges.append((1.0 - op) - (1.0 - mp))
                avg_edge = sum(edges) / len(edges) if edges else 0.08
        else:
            # No data — use conservative defaults
            if win_rate is None:
                win_rate = 0.55
            if avg_edge is None:
                avg_edge = 0.08

    # Clamp to reasonable bounds
    win_rate = max(0.30, min(win_rate, 0.90))
    avg_edge = max(0.01, min(avg_edge, 0.50))

    # Run simulations
    final_bankrolls: list[float] = []
    times_to_target: list[int] = []
    ruin_count = 0
    all_max_dd: list[float] = []

    # Track a representative path (median seed)
    representative_curve: list[float] = []

    for path_idx in range(num_paths):
        bankroll = starting_bankroll
        peak = bankroll
        max_dd = 0.0
        reached_target = False
        target_trade = None
        curve: list[float] = [bankroll]

        for trade_num in range(trades_per_path):
            if bankroll < 1.0:
                break  # Ruin

            # Simulate a market opportunity
            # Market price = our_prob - edge (the market underprices)
            our_prob = max(0.10, min(0.90, 0.50 + avg_edge))
            market_prob = max(0.05, min(0.95, our_prob - avg_edge))

            # Kelly sizing
            bet = kelly_bet_size(
                bankroll, our_prob, market_prob,
                fraction=kelly_multiplier,
                max_per_market_pct=max_per_market_pct,
            )
            bet = max(bet, min(1.0, bankroll))
            bet = min(bet, bankroll)

            if bet <= 0:
                continue

            # Outcome is random with our win_rate
            won = random.random() < win_rate

            if won:
                gross = bet * (1.0 - market_prob) / market_prob
                fee = gross * fee_rate
                pnl = gross - fee
            else:
                pnl = -bet

            bankroll += pnl
            bankroll = max(bankroll, 0.0)
            curve.append(round(bankroll, 2))

            # Track drawdown
            peak = max(peak, bankroll)
            dd = (peak - bankroll) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)

            # Check target
            if bankroll >= target_bankroll and not reached_target:
                reached_target = True
                target_trade = trade_num + 1

        final_bankrolls.append(bankroll)
        all_max_dd.append(max_dd)

        if bankroll < 5.0:
            ruin_count += 1
        if target_trade is not None:
            times_to_target.append(target_trade)

        # Save median path curve
        if path_idx == num_paths // 2:
            representative_curve = curve

    # Compute statistics
    final_bankrolls.sort()
    median_idx = len(final_bankrolls) // 2
    p10_idx = len(final_bankrolls) // 10
    p90_idx = int(len(final_bankrolls) * 0.9)

    median_final = final_bankrolls[median_idx]
    p10_final = final_bankrolls[p10_idx]
    p90_final = final_bankrolls[p90_idx]
    mean_final = sum(final_bankrolls) / len(final_bankrolls)

    ruin_prob = ruin_count / num_paths
    avg_max_dd = sum(all_max_dd) / len(all_max_dd) if all_max_dd else 0.0

    median_ttt = None
    if times_to_target:
        times_to_target.sort()
        median_ttt = times_to_target[len(times_to_target) // 2]

    # Sharpe from final bankrolls
    if len(final_bankrolls) >= 2:
        returns = [(b - starting_bankroll) / starting_bankroll for b in final_bankrolls]
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1)
        std_ret = math.sqrt(variance) if variance > 0 else 1.0
        sharpe = mean_ret / std_ret
    else:
        sharpe = 0.0

    result = BacktestResult(
        mode="monte_carlo",
        starting_bankroll=starting_bankroll,
        ending_bankroll=round(median_final, 2),
        total_trades=trades_per_path,
        wins=int(win_rate * trades_per_path),
        losses=int((1.0 - win_rate) * trades_per_path),
        win_rate=round(win_rate, 4),
        total_pnl=round(median_final - starting_bankroll, 2),
        total_fees=0.0,  # Not tracked per-trade in MC
        max_drawdown=round(avg_max_dd * starting_bankroll, 2),
        max_drawdown_pct=round(avg_max_dd, 4),
        sharpe_ratio=round(sharpe, 2),
        time_to_target=median_ttt,
        ruin_probability=round(ruin_prob, 4),
        bankroll_curve=representative_curve,
        mc_paths=num_paths,
        mc_median_final=round(median_final, 2),
        mc_p10_final=round(p10_final, 2),
        mc_p90_final=round(p90_final, 2),
    )

    log_event("backtest", "monte_carlo_complete", {
        "num_paths": num_paths,
        "trades_per_path": trades_per_path,
        "win_rate": win_rate,
        "avg_edge": avg_edge,
        "median_final": result.mc_median_final,
        "ruin_probability": result.ruin_probability,
        "time_to_target": result.time_to_target,
    }, result="success")

    return result


# ── Reporting ────────────────────────────────────────────────────

def print_backtest_report(result: BacktestResult):
    """Print a formatted backtest report to terminal."""
    print("=" * 60)
    title = "HISTORICAL REPLAY" if result.mode == "historical" else "MONTE CARLO SIMULATION"
    print(f"  POLYBOT BACKTEST — {title}")
    print("=" * 60)

    print(f"  Starting Bankroll:  ${result.starting_bankroll:,.2f}")
    print(f"  Ending Bankroll:    ${result.ending_bankroll:,.2f}")
    print(f"  Total P/L:          ${result.total_pnl:+,.2f}")
    print(f"  Total Trades:       {result.total_trades}")
    print(f"  Win Rate:           {result.win_rate:.1%} ({result.wins}W / {result.losses}L)")
    print(f"  Max Drawdown:       ${result.max_drawdown:,.2f} ({result.max_drawdown_pct:.1%})")
    print(f"  Sharpe Ratio:       {result.sharpe_ratio:.2f}")

    if result.total_fees > 0:
        print(f"  Fees Paid:          ${result.total_fees:,.2f}")

    if result.mode == "monte_carlo":
        print(f"\n  --- Monte Carlo Statistics ({result.mc_paths:,} paths) ---")
        print(f"  Median Final:       ${result.mc_median_final:,.2f}")
        print(f"  10th Percentile:    ${result.mc_p10_final:,.2f}  (bad luck)")
        print(f"  90th Percentile:    ${result.mc_p90_final:,.2f}  (good luck)")
        print(f"  Ruin Probability:   {result.ruin_probability:.1%}")

        if result.time_to_target is not None:
            print(f"  Trades to $25k:     ~{result.time_to_target:,} (median of successful paths)")
            # Rough time estimate: 2 trades/day average
            days = result.time_to_target / 2
            print(f"  Estimated Time:     ~{days:.0f} days ({days/30:.1f} months)")
        else:
            print(f"  Trades to $25k:     Not reached in {result.total_trades} trades")

    # Bankroll curve summary
    if result.bankroll_curve and len(result.bankroll_curve) > 1:
        curve = result.bankroll_curve
        low = min(curve)
        high = max(curve)
        print(f"\n  Bankroll Range:     ${low:,.2f} — ${high:,.2f}")

    print("=" * 60)


def save_backtest(result: BacktestResult) -> Path:
    """Save backtest result to data/backtests/ for later analysis."""
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"backtest_{result.mode}_{timestamp}.json"
    path = BACKTEST_DIR / filename

    data = {
        "mode": result.mode,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "starting_bankroll": result.starting_bankroll,
        "ending_bankroll": result.ending_bankroll,
        "total_trades": result.total_trades,
        "wins": result.wins,
        "losses": result.losses,
        "win_rate": result.win_rate,
        "total_pnl": result.total_pnl,
        "total_fees": result.total_fees,
        "max_drawdown": result.max_drawdown,
        "max_drawdown_pct": result.max_drawdown_pct,
        "sharpe_ratio": result.sharpe_ratio,
        "time_to_target": result.time_to_target,
        "ruin_probability": result.ruin_probability,
        "bankroll_curve": result.bankroll_curve,
        "mc_paths": result.mc_paths,
        "mc_median_final": result.mc_median_final,
        "mc_p10_final": result.mc_p10_final,
        "mc_p90_final": result.mc_p90_final,
    }

    # Atomic write
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(path)

    return path
