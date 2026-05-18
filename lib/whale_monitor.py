"""
Whale monitor — short-window large-trade pressure via Coinbase Pro.

**2026-05-18 venue migration:** moved from Binance.US to Coinbase Pro
after confirming Binance.US WebSocket trade stream stopped delivering
messages (connects but receives 0). REST API still works; only the
WS feed is broken (silent timeout). Coinbase Pro WS works fine, has
~10-50x deeper BTC volume (~$2-5B/day vs Binance.US's $50-200M), and
is fully US-accessible.

For each cron cycle we open a brief WebSocket connection to Coinbase
Pro's ``matches`` channel, collect every trade for ``collect_seconds``
(default 8), filter to "whale-sized" trades by USD notional, and
compute a directional pressure signal:

    pressure = (large_buy_vol - large_sell_vol) / total_vol_in_window

Pressure ∈ [-1, +1]. Positive = whales accumulating, negative =
distributing. Designed to slot into ``compute_indicators_for_window``
as a 4th composite contribution alongside RSI, theo_delta_gap, and
market_agreement.

**Honest caveat:** this is not pure latency arb at 60s cron cadence.
It's "did whales just move the market in the last 8s and might the
prediction market not have caught up yet?" The window between whale
trade and prediction-market reprice is on the order of 1-10 seconds,
so we capture some — not all — of that edge.

**Phase 3 upgrade path:** promote to a long-running daemon that
maintains a rolling 60-second whale-trade buffer. Then the signal
cycle reads from the buffer instead of opening a new WS each tick.
That cuts our reaction time from 8s to ~1s and unlocks the real
latency edge. For Phase 2 (measurement), the brief-connection
approach is sufficient.

WebSocket: ``wss://ws-feed.exchange.coinbase.com``
Subscribe message: {"type":"subscribe","channels":[{"name":"matches",
                    "product_ids":["BTC-USD"]}]}
Trade message (``type=="match"``):
    {"type":"match","trade_id":<int>,"side":"buy"|"sell",
     "size":"<float-as-string>","price":"<float-as-string>",
     "product_id":"BTC-USD","time":"<ISO>"}

side = TAKER side (the aggressor). "buy" = aggressive buyer lifted
the ask → bullish pressure. "sell" = aggressive seller hit the bid →
bearish pressure. This is what we want directly — no inversion needed
like Binance's ``m`` field required.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tradingcore.audit import log_event

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"

# Map our internal symbol convention (BTCUSDT) to Coinbase's
# product_id (BTC-USD). Easy substring transform — USDT→USD swap
# loses a stablecoin-pair distinction but for whale-direction
# detection that doesn't matter.
def _to_coinbase_product(symbol: str) -> str:
    """BTCUSDT → BTC-USD, ETHUSDT → ETH-USD, SOLUSDT → SOL-USD."""
    s = symbol.upper().replace("USDT", "USD").replace("USDC", "USD")
    if "-" in s:
        return s
    # Find boundary between asset and quote currency (always USD)
    if s.endswith("USD"):
        return f"{s[:-3]}-USD"
    return s


# Whale threshold — calibrated against real Coinbase BTC-USD
# distribution (probed 2026-05-18):
#   median  $39
#   mean    $458
#   P95     $2,069
#   P99     $5,912
# True institutional whales trade on Coinbase Prime / OTC desks
# (not this retail-facing matches stream). What we CAN measure here
# is "informed retail flow direction" — the top 5% of trades that
# represent meaningful conviction. $2k catches top 5%, gives us
# 10-15 samples per 8s window = reliable directional signal.
DEFAULT_WHALE_USD_THRESHOLD = 2_000.0

# 8s window — Coinbase BTC-USD does 100-300 trades in 8s during
# active hours, so even at 1% whale rate we expect 1-3 whales per
# cycle. The cron is 60s so 8s of WS time leaves 52s budget for
# everything else.
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

    product = _to_coinbase_product(symbol)
    started_ts = int(time.time())
    deadline = started_ts + collect_seconds

    total = 0
    n_whales = 0
    buy_vol = 0.0
    sell_vol = 0.0
    largest = 0.0
    last_whale_ts: int | None = None

    try:
        ws = websocket.create_connection(COINBASE_WS, timeout=10)
        # Coinbase requires an explicit subscribe message — unlike
        # Binance.US which auto-subscribes from the URL path.
        ws.send(json.dumps({
            "type": "subscribe",
            "channels": [{"name": "matches", "product_ids": [product]}],
        }))
    except Exception as e:
        log_event("whale_monitor", "ws_connect_failed",
                  {"symbol": symbol, "product": product,
                   "error": str(e)[:200]},
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
            # Coinbase sends "subscriptions" ack first, then "match"
            # events. Also occasional "heartbeat" or "ticker" — skip
            # everything but "match".
            if msg.get("type") != "match":
                continue
            try:
                price = float(msg["price"])
                qty = float(msg["size"])
            except (KeyError, ValueError, TypeError):
                continue
            usd = price * qty
            total += 1
            if usd < whale_usd_threshold:
                continue
            # Coinbase: `side` is the taker's side directly.
            # "buy" = aggressive buyer lifted ask → bullish
            # "sell" = aggressive seller hit bid → bearish
            is_aggressive_buy = (str(msg.get("side", "")).lower() == "buy")
            n_whales += 1
            if usd > largest:
                largest = usd
            if is_aggressive_buy:
                buy_vol += usd
            else:
                sell_vol += usd
            # Coinbase trade timestamps are ISO; convert to unix seconds
            iso = msg.get("time", "")
            try:
                last_whale_ts = int(datetime.fromisoformat(
                    iso.replace("Z", "+00:00")
                ).timestamp())
            except (ValueError, TypeError):
                last_whale_ts = int(time.time())
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
    """Best-effort persistence of each whale trade. Coinbase shape:
    {price, size, side, time, trade_id, product_id, ...}.
    """
    try:
        WHALE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Convert ISO timestamp to ms for forward compat with old rows
        iso = msg.get("time", "")
        ts_ms = 0
        try:
            ts_ms = int(datetime.fromisoformat(
                iso.replace("Z", "+00:00")
            ).timestamp() * 1000)
        except (ValueError, TypeError):
            pass
        with open(WHALE_LOG_PATH, "a") as f:
            f.write(json.dumps({
                "symbol": symbol,
                "venue": "coinbase",
                "product_id": msg.get("product_id", ""),
                "ts_ms": ts_ms,
                "price": float(msg.get("price", 0) or 0),
                "qty": float(msg.get("size", 0) or 0),
                "usd": round(usd, 2),
                "side": "BUY" if is_buy else "SELL",
                "trade_id": msg.get("trade_id"),
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
