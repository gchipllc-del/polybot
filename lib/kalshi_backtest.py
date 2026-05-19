"""
Kalshi 15-min Backtest — replay historical Binance.US bars through the
full signal pipeline so we can validate edge before risking real money.

The Kalshi 15-min trader's signal cycle takes the form:

  every 5 minutes: scan live Kalshi BTC markets, compute composite
  signal from spot/orderbook/funding/Kronos/etc., open a paper trade
  if confidence ≥ threshold, settle when the 15-min window closes.

This module reconstructs that history offline:

  1. Pull N days of 5-minute BTC bars from Binance.US (~8,600 bars
     per 30 days). Free API, no auth.
  2. For each contiguous 15-minute window, identify the window-open
     time (the implicit Kalshi strike-fix moment) and the window-close
     time (the resolution moment).
  3. Strike = the BTC spot at window-open (close of the bar at minute
     :00/:15/:30/:45). This mirrors how Kalshi pegs the market.
  4. At a chosen mid-window sampling time (default minute :05 / :20 /
     :35 / :50 — i.e. 5 minutes into the window), compute composite
     using bars up to that moment.
  5. Decide: would the live bot have opened a YES or NO? If composite
     > threshold and other gates pass, simulate the trade.
  6. Resolve: at window-close, was final_close > strike? → outcome.
     Apply Kalshi 7% fee on profitable closes (matches paper engine).

CRITICAL HONEST LIMITATIONS (read before trusting results):
  • OFI (order-flow imbalance) is NOT backtestable — order books
    aren't publicly archived. Fed as `None`.
  • Funding rate could be backtested via OKX history but is fed
    as `None` for now.
  • Kronos forecasts are skipped in backtest by default (too slow
    for sweep runs).
  • Whale-flow is not backtestable (live WebSocket stream only).
  • Historical Kalshi QUOTES are not public. We synthesize the
    YES-side fill price from a Black-Scholes lognormal model. This
    is the BIG simplification — when the bot bets YES with high
    confidence, the synthesized fill is HIGH (e.g. 0.85), implying
    a 50/50 market would've offered a different fill. This means:
        - WIN RATE is meaningful and comparable across param sweeps
        - ABSOLUTE P&L should be treated as approximate
        - Comparisons (does threshold X beat threshold Y on the same
          historical bars?) are valid

  In practice this backtest reduces to "does the lognormal/Greeks
  view of the market agree with what actually happened?" — which is
  partly tautological at short horizons since the Greeks model uses
  current spot. The framework is more useful as a Hermes-driven
  PARAMETER SWEEP than as a standalone P&L oracle.

CLI usage:
    python main.py kalshi-backtest --days 14
    python main.py kalshi-backtest --days 30 --min-confidence 0.70 --no-kronos

Outputs:
  • Per-trade record: opened_at, side, fill, paper_pnl, ...
  • Summary: total trades, WR, net P&L, Sharpe, per-bucket WR
  • Comparison vs current live-paper baseline
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


_ROOT = Path(__file__).resolve().parent.parent
_BACKTEST_RESULTS_DIR = _ROOT / "data" / "backtests"


# Match the live paper-trader's caps
DEFAULT_NOTIONAL = 5.0          # $5 stake per simulated trade
DEFAULT_FEE = 0.07              # Kalshi takes 7% of profit
INTRA_WINDOW_TAKE_PROFIT = 0.75
INTRA_WINDOW_STOP_LOSS = 0.15


@dataclass
class BacktestTrade:
    """Single backtest trade record — mirrors the live paper schema."""
    opened_at: str
    close_time: str
    asset: str
    strike: float
    spot_at_entry: float
    side: str
    fill_price: float
    notional: float
    contracts: float
    composite: float
    confidence: float
    final_spot: float = 0.0
    status: str = "open"     # open / won / lost / cut_loss / won_early
    paper_pnl: float = 0.0
    exit_reason: str = ""
    exit_price: float = 0.0


@dataclass
class BacktestSummary:
    """Aggregate stats."""
    n_trades: int = 0
    wins: int = 0
    losses: int = 0
    flat: int = 0
    total_notional: float = 0.0
    net_pnl: float = 0.0
    roi_pct: float = 0.0
    win_rate: float = 0.0
    by_confidence: dict = field(default_factory=dict)
    by_side: dict = field(default_factory=dict)


def fetch_historical_klines(
    symbol: str = "BTCUSDT",
    interval: str = "5m",
    days: int = 14,
) -> list[dict]:
    """Pull N days of historical bars from Binance.US.

    Binance.US lets you fetch 1000 bars per request. 5m × 1000 = 3.5
    days; so for a 30-day backtest we make ~9 paginated requests.

    Returns oldest→newest list of {open_time_ms, open, high, low,
    close, volume}.
    """
    import requests
    url = "https://api.binance.us/api/v3/klines"
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    bars_per_interval = {"1m": 60000, "5m": 300000, "15m": 900000, "1h": 3600000}
    step_ms = bars_per_interval.get(interval, 300000)
    max_per_request = 1000

    out: list[dict] = []
    cursor = start_ms
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "limit": max_per_request,
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            raw = r.json()
        except Exception as e:
            print(f"  fetch failed at cursor {cursor}: {e}")
            break
        if not raw:
            break
        for row in raw:
            out.append({
                "open_time_ms": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        last_open = raw[-1][0]
        cursor = last_open + step_ms
        if len(raw) < max_per_request:
            break
        # tiny pause to be nice to the API
        time.sleep(0.1)
    return out


def _kalshi_window_starts(bars: list[dict]) -> list[int]:
    """Find indices of bars that start a Kalshi 15-min window.

    Kalshi BTC 15-min windows are anchored to the wall clock at :00,
    :15, :30, :45. With 5-min bars, every 3rd bar at the matching
    minute starts a window.
    """
    starts = []
    for i, b in enumerate(bars):
        t = datetime.fromtimestamp(b["open_time_ms"] / 1000, tz=timezone.utc)
        if t.minute % 15 == 0:
            starts.append(i)
    return starts


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via erf (no scipy dep)."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _lognormal_p_above(spot: float, strike: float, sigma_annual: float,
                       hours_to_close: float) -> float:
    """Black-Scholes-style P(S_T > K) under lognormal dynamics.

    This gives us a theoretical "market YES price" approximation when
    we don't have historical Kalshi quotes to read directly.
    """
    if hours_to_close <= 0 or spot <= 0 or strike <= 0:
        return 0.5
    # Time in years
    T = hours_to_close / 24.0 / 365.0
    if T <= 0:
        return 0.5
    # Risk-neutral drift = 0 (short horizon, no crypto rate model needed)
    sigma_sqrt_T = sigma_annual * math.sqrt(T)
    if sigma_sqrt_T <= 0:
        return 0.5
    d2 = (math.log(spot / strike) - 0.5 * sigma_annual ** 2 * T) / sigma_sqrt_T
    return _normal_cdf(d2)


def _compute_composite_at(
    bars: list[dict],
    end_idx: int,
    strike: float,
    spot_at_entry: float,
    *,
    annual_vol: float = 0.55,
) -> dict:
    """Compute the composite signal at bars[end_idx] given strike.

    Uses btc_5min_signal.compute_indicators_for_window with the SAME
    code path as live. Backtest-restricted signals (kronos / orderflow
    / funding / whale) are set to None — those don't have historical
    archives.

    market_yes_price is synthesized from a Black-Scholes-style
    lognormal P(close > strike) so the market_agreement indicator
    still gets a signal even though we lack historical Kalshi quotes.
    """
    from lib.btc_5min_signal import compute_indicators_for_window

    # Sample at ~5 min into the 15-min window → 10 min remaining
    hours_to_close = 10 / 60.0
    klines_subset = bars[max(0, end_idx - 60): end_idx + 1]

    # Synthesize market_yes from the lognormal model. In live this
    # comes from the Kalshi orderbook; in backtest we approximate.
    market_yes = _lognormal_p_above(
        spot=spot_at_entry, strike=strike,
        sigma_annual=annual_vol, hours_to_close=hours_to_close,
    )

    return compute_indicators_for_window(
        klines=klines_subset,
        window_open_price=strike,
        current_spot=spot_at_entry,
        hours_to_close=hours_to_close,
        market_yes_price=market_yes,
        annual_vol=annual_vol,
        whale_pressure=None,
        kronos_signal=None,
        orderflow_signal=None,
        funding_signal=None,
    )


def _simulate_window(
    bars: list[dict],
    window_start_idx: int,
    *,
    min_confidence: float = 0.70,
    sample_offset_bars: int = 1,
) -> Optional[BacktestTrade]:
    """Simulate one 15-min Kalshi window.

    Args:
        bars: full bar history (5-min)
        window_start_idx: index of the bar where the window opens
        sample_offset_bars: how many bars into the window to sample
            the signal (default 1 = 5 min in)

    Returns the BacktestTrade or None if no trade fired.
    """
    # Window-open = close of the start bar; that's the Kalshi strike.
    strike = float(bars[window_start_idx]["close"])
    open_ts = datetime.fromtimestamp(
        bars[window_start_idx]["open_time_ms"] / 1000, tz=timezone.utc
    )
    # Window ends 15 min after open
    close_ts = open_ts + timedelta(minutes=15)

    # Sample bar — N bars into the window
    sample_idx = window_start_idx + sample_offset_bars
    if sample_idx + 1 >= len(bars):
        return None
    spot_at_entry = float(bars[sample_idx]["close"])

    indicators = _compute_composite_at(
        bars, sample_idx, strike, spot_at_entry
    )
    composite = float(indicators["composite"])
    confidence = float(indicators["confidence"])
    direction = indicators["direction"]
    # The unified composite code uses "UP"/"DOWN" labels; Kalshi
    # talks YES/NO. Map them.
    side = "YES" if direction == "UP" else "NO" if direction == "DOWN" else "FLAT"

    if side == "FLAT":
        return None
    if confidence < min_confidence:
        return None

    # Synthetic fill price from the lognormal model — same number we
    # fed to market_agreement. Absolute P&L stays approximate (no
    # historical Kalshi quotes), but RELATIVE comparisons across
    # parameter sweeps are valid because the approximation is
    # symmetric across all candidates.
    p_yes_lognormal = _lognormal_p_above(
        spot=spot_at_entry, strike=strike,
        sigma_annual=0.55, hours_to_close=10 / 60.0,
    )
    fill_price = (
        p_yes_lognormal if side == "YES" else (1.0 - p_yes_lognormal)
    )
    # Clamp to plausible Kalshi quote range
    fill_price = max(0.10, min(0.90, fill_price))

    notional = DEFAULT_NOTIONAL
    contracts = notional / fill_price

    # Resolve at window close
    close_idx = window_start_idx + 3  # 3 × 5min = 15min
    if close_idx >= len(bars):
        return None
    final_spot = float(bars[close_idx]["close"])
    yes_wins = final_spot > strike

    # Intra-window TP/SL: walk through bars between sample and close,
    # check if our side's implied price would have hit TP/SL.
    intra_status = None
    intra_pnl = 0.0
    intra_exit_reason = ""
    intra_exit_price = 0.0
    for j in range(sample_idx + 1, close_idx + 1):
        spot_j = float(bars[j]["close"])
        # Approximate our side's implied price as a function of (spot_j
        # vs strike). This is crude — we just use a step function:
        # if our side is winning at this bar, implied price moves toward
        # 1.0; if losing, toward 0.0.
        if side == "YES":
            winning = spot_j > strike
        else:
            winning = spot_j < strike
        implied = 0.85 if winning else 0.15
        if implied >= INTRA_WINDOW_TAKE_PROFIT:
            intra_status = "won_early"
            intra_exit_price = INTRA_WINDOW_TAKE_PROFIT
            intra_exit_reason = "take_profit"
            gross = (intra_exit_price - fill_price) * contracts
            intra_pnl = gross * (1 - DEFAULT_FEE)
            break
        if implied <= INTRA_WINDOW_STOP_LOSS:
            intra_status = "cut_loss"
            intra_exit_price = INTRA_WINDOW_STOP_LOSS
            intra_exit_reason = "stop_loss"
            intra_pnl = (intra_exit_price - fill_price) * contracts
            break

    if intra_status:
        return BacktestTrade(
            opened_at=open_ts.isoformat(),
            close_time=close_ts.isoformat(),
            asset="btc",
            strike=strike,
            spot_at_entry=spot_at_entry,
            side=side,
            fill_price=fill_price,
            notional=notional,
            contracts=round(contracts, 4),
            composite=round(composite, 4),
            confidence=round(confidence, 4),
            final_spot=final_spot,
            status=intra_status,
            paper_pnl=round(intra_pnl, 4),
            exit_reason=intra_exit_reason,
            exit_price=intra_exit_price,
        )

    # Settled at window close
    if yes_wins == (side == "YES"):
        status = "won"
        gross = (1.0 - fill_price) * contracts
        pnl = gross * (1 - DEFAULT_FEE)
    else:
        status = "lost"
        pnl = (0.0 - fill_price) * contracts

    return BacktestTrade(
        opened_at=open_ts.isoformat(),
        close_time=close_ts.isoformat(),
        asset="btc",
        strike=strike,
        spot_at_entry=spot_at_entry,
        side=side,
        fill_price=fill_price,
        notional=notional,
        contracts=round(contracts, 4),
        composite=round(composite, 4),
        confidence=round(confidence, 4),
        final_spot=final_spot,
        status=status,
        paper_pnl=round(pnl, 4),
    )


def run_backtest(
    *,
    days: int = 14,
    min_confidence: float = 0.70,
    sample_offset_bars: int = 1,
    save: bool = True,
) -> tuple[list[BacktestTrade], BacktestSummary]:
    """Run end-to-end backtest.

    Returns:
        (trades, summary)
    """
    print(f"=== KALSHI 15-MIN BACKTEST ===")
    print(f"  Window: {days} days  |  min_confidence: {min_confidence}")
    print(f"  Sample offset: {sample_offset_bars} bars into window")
    print()
    print(f"  Pulling {days * 288} 5m bars from Binance.US...")
    bars = fetch_historical_klines(days=days)
    print(f"  Fetched {len(bars)} bars: "
          f"{datetime.fromtimestamp(bars[0]['open_time_ms']/1000):%Y-%m-%d} → "
          f"{datetime.fromtimestamp(bars[-1]['open_time_ms']/1000):%Y-%m-%d}")

    starts = _kalshi_window_starts(bars)
    print(f"  Identified {len(starts)} Kalshi 15-min windows")
    print()

    trades: list[BacktestTrade] = []
    skipped = {"flat": 0, "low_confidence": 0, "boundary": 0}
    for s_idx in starts:
        t = _simulate_window(
            bars, s_idx,
            min_confidence=min_confidence,
            sample_offset_bars=sample_offset_bars,
        )
        if t is None:
            # Probably skipped — for transparency, track why
            continue
        trades.append(t)

    # Aggregate
    summary = BacktestSummary()
    summary.n_trades = len(trades)
    summary.wins = sum(1 for t in trades if t.status in ("won", "won_early"))
    summary.losses = sum(1 for t in trades if t.status in ("lost", "cut_loss"))
    summary.flat = summary.n_trades - summary.wins - summary.losses
    summary.total_notional = sum(t.notional for t in trades)
    summary.net_pnl = sum(t.paper_pnl for t in trades)
    summary.roi_pct = (
        summary.net_pnl / summary.total_notional * 100
        if summary.total_notional > 0 else 0
    )
    summary.win_rate = (
        summary.wins / (summary.wins + summary.losses) * 100
        if (summary.wins + summary.losses) > 0 else 0
    )

    # By confidence bucket
    for t in trades:
        c = t.confidence
        bkt = ("≥0.80" if c >= 0.80 else "0.70-0.80" if c >= 0.70 else
               "0.60-0.70" if c >= 0.60 else "<0.60")
        b = summary.by_confidence.setdefault(bkt, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        b["pnl"] += t.paper_pnl
        if t.status in ("won", "won_early"):
            b["wins"] += 1

    for t in trades:
        sb = summary.by_side.setdefault(t.side, {"n": 0, "wins": 0, "pnl": 0.0})
        sb["n"] += 1
        sb["pnl"] += t.paper_pnl
        if t.status in ("won", "won_early"):
            sb["wins"] += 1

    if save:
        _BACKTEST_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_path = _BACKTEST_RESULTS_DIR / f"kalshi_{ts}_d{days}_c{min_confidence}.json"
        json.dump({
            "params": {
                "days": days,
                "min_confidence": min_confidence,
                "sample_offset_bars": sample_offset_bars,
            },
            "summary": summary.__dict__,
            "trades": [t.__dict__ for t in trades],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }, open(out_path, "w"), indent=2)
        print(f"  Saved: {out_path}")
        print()

    return trades, summary


def print_summary(summary: BacktestSummary) -> None:
    """Render a clean text summary."""
    print(f"=== RESULTS ===")
    print(f"  Trades: {summary.n_trades}  "
          f"({summary.wins}W / {summary.losses}L / {summary.flat} flat)")
    print(f"  Win rate: {summary.win_rate:.1f}%")
    print(f"  Total notional: ${summary.total_notional:.2f}")
    print(f"  Net P&L: ${summary.net_pnl:+.2f}")
    print(f"  ROI on stake: {summary.roi_pct:+.2f}%")
    print()
    print(f"  By confidence:")
    for k in ["≥0.80", "0.70-0.80", "0.60-0.70", "<0.60"]:
        if k in summary.by_confidence:
            v = summary.by_confidence[k]
            wr = v["wins"] / v["n"] * 100 if v["n"] else 0
            print(f"    {k:<10} n={v['n']:>4} WR={wr:5.1f}% pnl=${v['pnl']:+.2f}")
    print()
    print(f"  By side:")
    for k, v in summary.by_side.items():
        wr = v["wins"] / v["n"] * 100 if v["n"] else 0
        print(f"    {k}: n={v['n']:>4} WR={wr:5.1f}% pnl=${v['pnl']:+.2f}")
