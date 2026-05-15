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

def sample_signals(*, max_seconds_out: int = 600) -> list[FiveMinSample]:
    """One sweep — spot price + every qualifying market.

    Cheap (~1-2s for 2 HTTP calls + however many active markets we find).
    Designed to be called from a tight loop or a launchd cron.
    """
    spot = fetch_binance_btc_price()
    if spot is None:
        return []
    markets = discover_5min_btc_markets(max_seconds_out=max_seconds_out)
    if not markets:
        return []

    now_iso = datetime.now(timezone.utc).isoformat()
    out: list[FiveMinSample] = []
    for m in markets:
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

def run_signal_cycle(*, max_seconds_out: int = 600) -> dict:
    """One full sweep: discover + sample + persist + log.

    Returns a summary dict for the cron caller. No paper trading or
    real execution here — purely measurement, exactly like Phase 1 of
    btc_arb_signal.
    """
    samples = sample_signals(max_seconds_out=max_seconds_out)
    persist_samples(samples)

    if samples:
        nearest = samples[0]
        log_event("btc_5min", "signal_cycle", {
            "n_markets": len(samples),
            "nearest_seconds_to_close": nearest.seconds_to_close,
            "nearest_up_price": nearest.up_price,
            "spot": nearest.spot_usd,
        })
    else:
        log_event("btc_5min", "no_active_markets", {}, result="degraded")

    return {
        "n_markets": len(samples),
        "nearest_seconds_to_close": samples[0].seconds_to_close if samples else None,
        "spot_usd": samples[0].spot_usd if samples else None,
        "samples": [asdict(s) for s in samples],
    }
