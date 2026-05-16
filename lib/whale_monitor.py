"""
Whale monitor — short-window large-trade pressure on Binance.US.

For each cron cycle we open a brief WebSocket connection to Binance.US's
public trade stream, collect every trade for ``collect_seconds`` (default
15), filter to "whale-sized" trades by USD notional, and compute a
directional pressure signal:

    pressure = (large_buy_vol - large_sell_vol) / total_vol_in_window

Pressure ∈ [-1, +1]. Positive = whales accumulating, negative =
distributing. Designed to slot into ``compute_indicators_for_window``
as a 4th composite contribution alongside RSI, theo_delta_gap, and
market_agreement.

**Honest caveat:** this is not pure latency arb at 60s cron cadence.
It's "did whales just move the market in the last 15s and might the
prediction market not have caught up yet?" The window between whale
trade and prediction-market reprice is on the order of 1-10 seconds,
so we capture some — not all — of that edge.

**Phase 3 upgrade path:** promote to a long-running daemon that
maintains a rolling 60-second whale-trade buffer. Then the signal
cycle reads from the buffer instead of opening a new WS each tick.
That cuts our reaction time from 15s to ~1s and unlocks the real
latency edge. For Phase 2 (measurement), the brief-connection
approach is sufficient.

WebSocket: ``wss://stream.binance.us:9443/ws/<symbol>@trade``
Stream message shape (key fields):
    {"e": "trade", "E": ts_ms, "s": "BTCUSDT", "t": id,
     "p": "<price>", "q": "<qty>", "T": trade_ts_ms,
     "m": <bool: buyer_is_maker (i.e. seller initiated)>}

Buyer-as-taker = aggressive BUY (lifting offers, bullish).
Seller-as-taker = aggressive SELL (hitting bids, bearish).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from lib.audit import log_event

BINANCE_US_WS = "wss://stream.binance.us:9443/ws"

# What counts as a "whale" trade in USD notional. **Tuned for
# Binance.US** which is much thinner than Binance.com (which geo-
# blocks US users). $100k on Binance.com would be appropriate; on
# Binance.US that captures ~1 trade per minute. Drop to $25k which
# is still significant on Binance.US (~top 5% of trades by size)
# but gives us multiple samples per cron cycle.
#
# **Phase 3 upgrade path:** add Coinbase Pro WebSocket as a second
# source for deeper US-accessible whale data. Coinbase has $2-5B
# daily BTC volume vs Binance.US's $50-200M, so we'd raise the
# threshold to $100k+ there.
DEFAULT_WHALE_USD_THRESHOLD = 25_000.0

# Collection window per cron tick. Trade-off:
#   * Longer = more samples, more reliable signal
#   * Shorter = cron tick finishes faster, more cron cycles per minute
# 8s is the smallest window that consistently catches at least one
# whale on Binance.US during US session hours. The cron is 60s so
# this still leaves 52s budget for the rest of the pipeline.
DEFAULT_COLLECT_SECONDS = 8

WHALE_LOG_PATH = Path(__file__).parent.parent / "data" / "whale_trades.jsonl"


@dataclass
class WhaleSnapshot:
    """One sample of recent whale activity. Returned by collect_whale_trades."""
    symbol: str
    collected_at_ts: int          # unix seconds when we started collecting
    window_seconds: int           # how long we listened
    total_trades_seen: int
    n_whales: int
    largest_trade_usd: float
    buy_vol_usd: float            # aggressive (taker) buys
    sell_vol_usd: float           # aggressive (taker) sells
    pressure: float               # [-1, +1] — positive = whale accumulation
    last_whale_age_s: float | None  # seconds since most recent whale trade,
                                    # or None if no whales seen


def collect_whale_trades(
    *,
    symbol: str = "BTCUSDT",
    collect_seconds: int = DEFAULT_COLLECT_SECONDS,
    whale_usd_threshold: float = DEFAULT_WHALE_USD_THRESHOLD,
    persist: bool = True,
) -> WhaleSnapshot:
    """Open a brief WebSocket connection, collect trades, return pressure.

    Returns a ``WhaleSnapshot`` even on failure (with zeros) so callers
    can degrade gracefully. ``persist=True`` appends each whale trade
    seen to ``data/whale_trades.jsonl`` for later analysis.
    """
    import websocket

    url = f"{BINANCE_US_WS}/{symbol.lower()}@trade"
    started_ts = int(time.time())
    deadline = started_ts + collect_seconds

    total = 0
    n_whales = 0
    buy_vol = 0.0
    sell_vol = 0.0
    largest = 0.0
    last_whale_ts: int | None = None

    try:
        ws = websocket.create_connection(url, timeout=10)
    except Exception as e:
        log_event("whale_monitor", "ws_connect_failed",
                  {"symbol": symbol, "error": str(e)[:200]},
                  result="degraded")
        return WhaleSnapshot(
            symbol=symbol, collected_at_ts=started_ts,
            window_seconds=collect_seconds, total_trades_seen=0,
            n_whales=0, largest_trade_usd=0.0,
            buy_vol_usd=0.0, sell_vol_usd=0.0,
            pressure=0.0, last_whale_age_s=None,
        )

    try:
        while time.time() < deadline:
            # settimeout so a quiet stream doesn't hang us past deadline
            ws.settimeout(max(1.0, deadline - time.time()))
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                break
            except Exception:
                break
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if msg.get("e") != "trade":
                continue
            try:
                price = float(msg["p"])
                qty = float(msg["q"])
            except (KeyError, ValueError, TypeError):
                continue
            usd = price * qty
            total += 1
            if usd < whale_usd_threshold:
                continue
            # m=True → buyer was maker → seller initiated (aggressive SELL)
            # m=False → buyer was taker → aggressive BUY
            is_aggressive_buy = not bool(msg.get("m"))
            n_whales += 1
            if usd > largest:
                largest = usd
            if is_aggressive_buy:
                buy_vol += usd
            else:
                sell_vol += usd
            trade_ts_ms = int(msg.get("T", msg.get("E", time.time() * 1000)))
            last_whale_ts = trade_ts_ms // 1000
            if persist:
                _append_whale(symbol, msg, usd, is_aggressive_buy)
    finally:
        try:
            ws.close()
        except Exception:
            pass

    total_vol = buy_vol + sell_vol
    pressure = (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0
    last_age = (started_ts + collect_seconds - last_whale_ts) if last_whale_ts else None

    snapshot = WhaleSnapshot(
        symbol=symbol,
        collected_at_ts=started_ts,
        window_seconds=collect_seconds,
        total_trades_seen=total,
        n_whales=n_whales,
        largest_trade_usd=round(largest, 2),
        buy_vol_usd=round(buy_vol, 2),
        sell_vol_usd=round(sell_vol, 2),
        pressure=round(pressure, 4),
        last_whale_age_s=last_age,
    )
    log_event("whale_monitor", "snapshot", {
        "symbol": symbol,
        "n_whales": n_whales,
        "total_trades": total,
        "pressure": snapshot.pressure,
        "buy_vol": snapshot.buy_vol_usd,
        "sell_vol": snapshot.sell_vol_usd,
        "largest_usd": snapshot.largest_trade_usd,
    })
    return snapshot


def _append_whale(symbol: str, msg: dict, usd: float, is_buy: bool) -> None:
    """Best-effort persistence of each whale trade seen."""
    try:
        WHALE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(WHALE_LOG_PATH, "a") as f:
            f.write(json.dumps({
                "symbol": symbol,
                "ts_ms": int(msg.get("T", 0)),
                "price": float(msg.get("p", 0) or 0),
                "qty": float(msg.get("q", 0) or 0),
                "usd": round(usd, 2),
                "side": "BUY" if is_buy else "SELL",
                "trade_id": msg.get("t"),
            }) + "\n")
    except OSError:
        pass


def whale_pressure_to_indicator_value(snapshot: WhaleSnapshot) -> float:
    """Convert a snapshot to a [-1, +1] indicator value for the composite.

    Returns 0.0 when:
      * No whales seen (insufficient signal)
      * Last whale > 60s ago (stale)
      * Total trades < 3 (suspicious — feed might be glitching)

    Otherwise: clamp pressure to ±1.0 directly. Bonus weight for
    recency — a whale 5s ago is more actionable than 50s ago.
    """
    if snapshot.n_whales == 0:
        return 0.0
    if snapshot.total_trades_seen < 3:
        return 0.0
    if snapshot.last_whale_age_s is not None and snapshot.last_whale_age_s > 60:
        return 0.0
    base = max(-1.0, min(1.0, snapshot.pressure))
    # Recency multiplier: 0s = 1.0, 30s = 0.5, 60s = 0.0
    if snapshot.last_whale_age_s is None:
        recency = 1.0
    else:
        recency = max(0.0, 1.0 - snapshot.last_whale_age_s / 60.0)
    return base * recency
