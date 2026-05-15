"""
BTC 5-min UP/DOWN signal — the strategy the Gravia post was actually about.

Polymarket auto-generates a fresh BTC binary every 5 minutes. The question
asks "will BTC be higher or lower than the opening price when this
5-minute window closes?" Resolution is automatic and near-instant once
the window closes — no UMA dispute, no waiting days.

Why this is different from ``btc_arb_signal.py``:
  * That module handles daily-strike markets ("BTC above $XX,000 on May 17")
    using a lognormal-vol approximation across days.
  * This module handles 5-minute UP/DOWN markets where the question is
    "did BTC just move up or down". No vol model needed — we know the
    opening price (snapshot at window-open) and can compute "if BTC
    stays flat from here, where would the market be?"

Discovery:
  Gamma surfaces these via ``closed=false, active=true``. The slug
  pattern is ``btc-updown-5m-<unix_ts>`` where ``<unix_ts>`` is the
  END of the 5-minute window (UTC seconds). Question pattern:
  ``"bitcoin up or down - <date>, <hh:mm>-<hh:mm> et"``.

Per-window data we persist:
  * ``data/btc_5min_signal.jsonl`` — one row per sample, one sample
    per market per cycle. Append-only.

Phase 1 (this module) is **measurement only**: snapshot + persist.
Phase 2 will compute gap vs implied-fair-price and record paper trades.
Phase 3 will run live with a T-minus-10-seconds entry rule (proven
pattern across the open-source latency bots — by T-10s the direction
is ~locked, accuracy spikes, and the market hasn't fully repriced).

Reference strategy (synthesized from open-source competitors) — what
Phase 2 should implement, in priority order of signal weight:

  1. **Window Delta** (weight 5-7): current spot vs window-open spot.
     Moves > 0.10% are highest-confidence directional signal. Need to
     snapshot Binance at the window-start tick to know the open.
  2. **Real-time Tick Trend** (weight 2): 2-second polling for
     micro-trends between 1-min candle updates.
  3. **Micro Momentum** (weight 2): direction of last two 1-min candles.
  4. **Acceleration** (weight 1.5): is momentum building or fading?
  5. **EMA 9/21 Crossover** (weight 1).
  6. **RSI 14** (weight 1-2): extremes weighted higher.
  7. **Volume Surge** (weight 1): 1.5x recent average confirms direction.

Risk caps (conservative defaults — Phase 3 will tune):
  * Per-trade size: 1-2% of bankroll (NOT 25% — that's the "Safe" mode
    of the reference bots, which is aggressive by finance-standard math)
  * Min confidence to fire: 50% of max composite score
  * Hard deadline: T-5 seconds (no trades after that)
  * Polymarket taker fee: up to 1.56% (Feb 2026 update)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from lib.audit import log_event

SIGNAL_PATH = Path(__file__).parent.parent / "data" / "btc_5min_signal.jsonl"

BINANCE_US_TICKER = "https://api.binance.us/api/v3/ticker/price"
BINANCE_US_KLINES = "https://api.binance.us/api/v3/klines"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

# Match `btc-updown-5m-1778883600` (capture the unix-ts suffix).
SLUG_RE = re.compile(r"^btc-updown-5m-(\d{9,11})$")


@dataclass
class FiveMinSample:
    """One snapshot of one 5-min market at one moment."""
    sample_at: str               # ISO of when we polled
    market_id: str               # Polymarket conditionId
    slug: str
    question: str
    window_end_ts: int           # unix seconds — exact resolution time
    seconds_to_close: float      # window_end_ts - now (signed; negative = closed)
    up_price: float              # current YES (=UP) price, 0..1
    down_price: float            # 1 - up_price (Polymarket binaries sum to ~1)
    spot_usd: float              # Binance.US BTC/USDT spot at sample time
    indicators: dict | None = None  # composite + raw indicator values
                                    # (None if klines fetch failed)


# ── Discovery ────────────────────────────────────────────────────────

def fetch_binance_btc_price() -> float | None:
    """Single REST poll. Returns spot in USD, or None on failure."""
    import requests
    try:
        r = requests.get(
            BINANCE_US_TICKER, params={"symbol": "BTCUSDT"}, timeout=8,
        )
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log_event("btc_5min", "binance_fetch_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return None


def fetch_binance_klines(*, limit: int = 50) -> list[dict] | None:
    """Pull recent BTC/USDT 1-minute candles from Binance.US.

    Returns oldest→newest list of dicts so indicators iterate forward
    naturally. ``limit=50`` gives EMA21 ample headroom and RSI14 + 3-tick
    momentum a clean tail. None on failure.
    """
    import requests
    try:
        r = requests.get(
            BINANCE_US_KLINES,
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log_event("btc_5min", "klines_fetch_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return None
    out: list[dict] = []
    for row in raw:
        try:
            out.append({
                "open_time_ms": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            })
        except (ValueError, IndexError, TypeError):
            continue
    return out


# ── Indicators ───────────────────────────────────────────────────────

def _ema(prices: list[float], period: int) -> float | None:
    """Exponential moving average over the last ``period``+ closes.

    Standard recursion: ema_today = α·price + (1-α)·ema_yesterday
    with α = 2/(period+1). Bootstrap from the simple average of the
    first ``period`` values, then iterate.
    """
    if len(prices) < period:
        return None
    alpha = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = alpha * p + (1 - alpha) * ema
    return ema


def _rsi(prices: list[float], period: int = 14) -> float | None:
    """Standard 14-period RSI on closes. Returns 0..100, None if too
    few samples. Uses Wilder's smoothing (the original formulation).
    """
    if len(prices) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    # Wilder smoothing for the rest
    for i in range(period + 1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _find_window_open_price(
    klines: list[dict],
    window_start_ts: int,
) -> float | None:
    """Find the 1-minute candle that opened exactly at ``window_start_ts``
    (a 5-minute boundary) and return its ``open`` price.

    Falls back to the candle with the closest open_time within ±60s if
    the exact boundary candle isn't present (Binance.US occasionally
    misses ticks).
    """
    target_ms = window_start_ts * 1000
    best: tuple[int, float] | None = None
    for k in klines:
        if k["open_time_ms"] == target_ms:
            return k["open"]
        diff = abs(k["open_time_ms"] - target_ms)
        if diff <= 60_000 and (best is None or diff < best[0]):
            best = (diff, k["open"])
    return best[1] if best else None


def compute_indicators(
    *,
    klines: list[dict],
    window_start_ts: int,
    current_spot: float,
) -> dict:
    """Compute the 6 indicators the open-source latency bots converge on.

    Tick-trend (the 7th) is skipped because a 60s cron can't poll
    sub-minute; it lives in Phase 3's tight-polling daemon. Weights
    come from the synthesized strategy doc — Window Delta dominates.

    Returns a dict of raw indicator values plus the composite score.
    Composite is signed: positive = UP, negative = DOWN.
    """
    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]

    # 1. Window Delta — dominant signal. % move from window open to now.
    window_open = _find_window_open_price(klines, window_start_ts)
    if window_open and window_open > 0:
        window_delta_pct = (current_spot - window_open) / window_open * 100.0
    else:
        window_delta_pct = None

    # 2. Micro Momentum — sum of last 2 candles' direction (-2 to +2)
    if len(klines) >= 2:
        m1 = klines[-1]["close"] - klines[-1]["open"]
        m2 = klines[-2]["close"] - klines[-2]["open"]
        micro_momentum = (1 if m1 > 0 else -1 if m1 < 0 else 0) + \
                         (1 if m2 > 0 else -1 if m2 < 0 else 0)
    else:
        micro_momentum = 0

    # 3. Acceleration — is momentum building or fading?
    if len(closes) >= 3:
        d1 = closes[-1] - closes[-2]
        d2 = closes[-2] - closes[-3]
        acceleration = d1 - d2
    else:
        acceleration = 0.0

    # 4. EMA 9 / EMA 21 crossover
    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    ema_cross = 0.0
    if ema9 is not None and ema21 is not None and ema21 > 0:
        ema_cross = (ema9 - ema21) / ema21 * 100.0

    # 5. RSI 14
    rsi = _rsi(closes, 14)

    # 6. Volume Surge — last bar vs trailing avg
    if len(volumes) >= 15:
        recent = volumes[-1]
        trailing_avg = sum(volumes[-15:-1]) / 14
        vol_surge = recent / trailing_avg if trailing_avg > 0 else 1.0
    else:
        vol_surge = 1.0

    # ── Compose ────────────────────────────────────────────────────
    # Each contribution is normalized to roughly [-1, +1] then weighted.
    contribs: dict[str, float] = {}

    # Window delta: 0.10% → max-strength bullish (per the reference
    # strategy). Saturate at ±0.10%.
    if window_delta_pct is not None:
        contribs["window_delta"] = max(-1.0, min(1.0, window_delta_pct / 0.10)) * 6.0
    else:
        contribs["window_delta"] = 0.0

    # Micro momentum: already -2..+2, scale to -1..+1 then weight 2
    contribs["micro_momentum"] = (micro_momentum / 2.0) * 2.0

    # Acceleration in absolute USD — normalize by typical 1-min std (~$50)
    contribs["acceleration"] = max(-1.0, min(1.0, acceleration / 50.0)) * 1.5

    # EMA cross %: 0.05% delta ≈ max strength
    contribs["ema_cross"] = max(-1.0, min(1.0, ema_cross / 0.05)) * 1.0

    # RSI: 30/70 are extremes; map 30→+1.5, 70→-1.5 (oversold = bullish)
    if rsi is not None:
        rsi_norm = (50.0 - rsi) / 20.0  # 30→+1.0, 70→-1.0
        contribs["rsi"] = max(-1.5, min(1.5, rsi_norm)) * 1.0
    else:
        contribs["rsi"] = 0.0

    # Volume surge: confirms direction; 1.5x → +1 weight if direction
    # agrees, 0 otherwise. Direction comes from micro_momentum sign.
    direction_sign = 1 if micro_momentum > 0 else -1 if micro_momentum < 0 else 0
    if vol_surge >= 1.5 and direction_sign != 0:
        contribs["volume_surge"] = direction_sign * 1.0
    else:
        contribs["volume_surge"] = 0.0

    # Composite + max possible score (for normalization downstream)
    composite = sum(contribs.values())
    max_possible = 6.0 + 2.0 + 1.5 + 1.0 + 1.5 + 1.0  # = 13.0

    return {
        "window_open": window_open,
        "window_delta_pct": window_delta_pct,
        "micro_momentum": micro_momentum,
        "acceleration": acceleration,
        "ema9": ema9, "ema21": ema21, "ema_cross_pct": ema_cross,
        "rsi": rsi,
        "vol_surge_ratio": vol_surge,
        "contribs": contribs,
        "composite": composite,
        "max_possible": max_possible,
        "confidence": abs(composite) / max_possible if max_possible > 0 else 0.0,
        "direction": "UP" if composite > 0 else "DOWN" if composite < 0 else "FLAT",
    }


def discover_5min_btc_markets(
    *,
    max_seconds_out: int = 600,
    require_live: bool = True,
) -> list[dict]:
    """Find currently-open 5-min BTC UP/DOWN markets within
    ``max_seconds_out`` of resolving.

    The Gravia-style edge concentrates in the last ~60s of each window
    (the source repos all converge on a T-10s entry rule). Default
    ``max_seconds_out=600`` captures the full lifecycle of every
    near-term market so we can see whether spreads widen as close
    approaches.

    ``require_live=True`` filters out markets whose ``closed`` flag has
    already flipped (Gamma can lag a few seconds past window-end).
    """
    import requests

    try:
        r = requests.get(
            f"{POLYMARKET_GAMMA}/markets",
            params={
                "closed": "false", "active": "true",
                "limit": 500, "order": "startDate", "ascending": "false",
            },
            timeout=15,
        )
        r.raise_for_status()
        markets = r.json()
    except Exception as e:
        log_event("btc_5min", "gamma_fetch_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return []
    if not isinstance(markets, list):
        return []

    now_ts = datetime.now(timezone.utc).timestamp()
    qualified: list[dict] = []
    for m in markets:
        slug = m.get("slug") or ""
        match = SLUG_RE.match(slug)
        if not match:
            continue
        if require_live and m.get("closed"):
            continue
        try:
            window_end_ts = int(match.group(1))
        except ValueError:
            continue
        seconds_to_close = window_end_ts - now_ts
        # Drop already-resolved windows (slug timestamp passed) and
        # ones too far in the future to care about right now.
        if seconds_to_close < -60 or seconds_to_close > max_seconds_out:
            continue

        # Outcome price snapshot — Polymarket lists outcomes as
        # ["Up", "Down"] and outcomePrices as ["<up>", "<down>"].
        try:
            outcomes = m.get("outcomePrices") or "[]"
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            up_price = float(outcomes[0]) if outcomes else None
        except (json.JSONDecodeError, ValueError, TypeError):
            up_price = None
        if up_price is None or not (0.0 < up_price < 1.0):
            continue

        qualified.append({
            "id": m.get("conditionId") or m.get("id"),
            "slug": slug,
            "question": m.get("question", ""),
            "up_price": up_price,
            "down_price": round(1.0 - up_price, 4),
            "window_end_ts": window_end_ts,
            "seconds_to_close": seconds_to_close,
        })
    # Sort by closest-to-resolving first — that's where the edge lives.
    qualified.sort(key=lambda x: x["seconds_to_close"])
    return qualified


# ── Sampling ─────────────────────────────────────────────────────────

def sample_signals(
    *,
    max_seconds_out: int = 600,
    with_indicators: bool = True,
) -> list[FiveMinSample]:
    """One sweep — spot + klines + every qualifying market.

    Klines are fetched ONCE per cycle and shared across all markets in
    the sweep — they all share the same Binance state. Each market
    gets its own indicator computation (window_start_ts differs per
    market). Cheap: 3 HTTP calls total regardless of how many markets
    are live.
    """
    spot = fetch_binance_btc_price()
    if spot is None:
        return []
    markets = discover_5min_btc_markets(max_seconds_out=max_seconds_out)
    if not markets:
        return []

    klines = fetch_binance_klines() if with_indicators else None

    now_iso = datetime.now(timezone.utc).isoformat()
    out: list[FiveMinSample] = []
    for m in markets:
        indicators = None
        if klines:
            # Window start = window end - 5 minutes
            window_start_ts = m["window_end_ts"] - 300
            indicators = compute_indicators(
                klines=klines,
                window_start_ts=window_start_ts,
                current_spot=spot,
            )
        out.append(FiveMinSample(
            sample_at=now_iso,
            market_id=str(m["id"]),
            slug=m["slug"],
            question=m["question"][:140],
            window_end_ts=m["window_end_ts"],
            seconds_to_close=round(m["seconds_to_close"], 2),
            up_price=m["up_price"],
            down_price=m["down_price"],
            spot_usd=spot,
            indicators=indicators,
        ))
    return out


def persist_samples(samples: list[FiveMinSample]) -> None:
    """Append samples as JSONL. Append-only — keep the full trajectory
    so Phase 2 can compute "what was UP price at T-60s vs T-10s" later.
    """
    if not samples:
        return
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_PATH, "a") as f:
        for s in samples:
            f.write(json.dumps(asdict(s)) + "\n")


# ── Public entry ─────────────────────────────────────────────────────

def run_signal_cycle(
    *,
    max_seconds_out: int = 600,
    record_paper_trades: bool = True,
    settle_paper_trades: bool = True,
) -> dict:
    """One full sweep: discover + sample + persist + (paper record + settle) + log.

    ``record_paper_trades=True`` (default) auto-opens a Phase 2 paper
    trade whenever a sample's confidence + entry-window criteria fire.
    ``settle_paper_trades=True`` polls open paper trades for resolution
    every cycle — cheap (only fires if any opens exist).

    Both flags are wired so the launchd cron runs the full pipeline by
    default; set to False from tests / dry-runs.
    """
    samples = sample_signals(max_seconds_out=max_seconds_out)
    persist_samples(samples)

    n_paper_opened = 0
    if record_paper_trades and samples:
        try:
            from lib.btc_5min_paper import record_paper_trades_from_samples
            new_trades = record_paper_trades_from_samples(
                [asdict(s) for s in samples]
            )
            n_paper_opened = len(new_trades)
        except Exception as e:
            log_event("btc_5min", "paper_record_failed",
                      {"error": str(e)[:200]}, result="degraded")

    settle_summary = {}
    if settle_paper_trades:
        try:
            from lib.btc_5min_paper import (
                settle_paper_trades as _settle_paper_trades,
            )
            settle_summary = _settle_paper_trades()
        except Exception as e:
            log_event("btc_5min", "paper_settle_failed",
                      {"error": str(e)[:200]}, result="degraded")

    if samples:
        nearest = samples[0]
        log_event("btc_5min", "signal_cycle", {
            "n_markets": len(samples),
            "nearest_seconds_to_close": nearest.seconds_to_close,
            "nearest_up_price": nearest.up_price,
            "spot": nearest.spot_usd,
            "paper_trades_opened": n_paper_opened,
            "paper_settled": settle_summary.get("settled_now", 0),
        })
    else:
        log_event("btc_5min", "no_active_markets", {}, result="degraded")

    return {
        "n_markets": len(samples),
        "nearest_seconds_to_close": samples[0].seconds_to_close if samples else None,
        "spot_usd": samples[0].spot_usd if samples else None,
        "samples": [asdict(s) for s in samples],
        "paper_trades_opened": n_paper_opened,
        "settle_summary": settle_summary,
    }
