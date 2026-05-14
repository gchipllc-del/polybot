"""
BTC arb signal — Phase 1 of the latency-arb stack.

Captures the gap between Binance's BTC spot price and Polymarket's
daily-strike BTC binary markets. Read-only. Phase 2 (paper trade)
and Phase 3 (real execution) ride on the signal data this module
persists.

What we monitor:
  * **Spot price** from Binance.US (api.binance.us — Binance global
    is geo-blocked from the US, HTTP 451).
  * **Polymarket BTC daily-strike markets** via the Gamma API,
    e.g. "Will Bitcoin be above \$82,000 on May 14?"
  * **Implied probability** computed from spot + strike + remaining
    time + a lognormal-vol approximation.
  * **The gap** between our implied probability and Polymarket's
    quoted YES price. When the gap is wide and persistent, that's
    the latency-arb signal.

Honest caveat: Gravia's setup catches sub-100ms gaps via WebSocket;
our REST-polled, ~500ms-1s cycle catches the slower tail. We capture
a fraction of the theoretical edge — that fraction is what Phase 2's
paper trading will measure.

Persistence: ``data/btc_arb_signal.jsonl`` — one row per signal
sample. Bot of any process can read it without re-fetching.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from lib.audit import log_event

SIGNAL_PATH = Path(__file__).parent.parent / "data" / "btc_arb_signal.jsonl"

# Binance.US — required because binance.com geo-blocks US users (HTTP 451).
BINANCE_US_TICKER = "https://api.binance.us/api/v3/ticker/price"

# Polymarket Gamma — public, no auth needed for reads.
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

# Annualized BTC volatility for the lognormal pricing approximation.
# Realized 2025-2026 σ has been around 50-65%; we use 55% as a stable
# midpoint. Hermes-style tuning can drift this once we have data.
DEFAULT_ANNUAL_VOL = 0.55


@dataclass
class BtcArbSignal:
    """One snapshot — one moment in time, one Polymarket market."""
    sample_at: str                 # ISO timestamp
    spot_usd: float                # Binance.US BTC/USDT last price
    market_id: str                 # Polymarket conditionId
    question: str
    strike_usd: float              # extracted from the question
    yes_price: float               # current Polymarket YES quote
    implied_yes_prob: float        # our fair value from spot+strike+T+σ
    gap: float                     # signed; positive = YES is too cheap
    abs_gap: float                 # for ranking
    hours_to_close: float
    volume_24h: float


# ── Data fetching ───────────────────────────────────────────────────

def fetch_binance_btc_price() -> float | None:
    """Single REST poll. Returns spot in USD, or None on failure."""
    import requests
    try:
        r = requests.get(BINANCE_US_TICKER,
                         params={"symbol": "BTCUSDT"}, timeout=8)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as e:
        log_event("btc_arb", "binance_fetch_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return None


def fetch_polymarket_btc_strikes(
    *, max_hours_out: float = 720.0,
) -> list[dict]:
    """Find currently-active Polymarket BTC daily-strike markets that
    resolve within ``max_hours_out`` hours.

    Filters to questions like:
      "Will the price of Bitcoin be above $XX,000 on <date>?"
    which are simple binary daily-strikes with predictable settlement.
    """
    import re
    import requests

    try:
        r = requests.get(
            f"{POLYMARKET_GAMMA}/markets",
            params={
                "closed": "false", "active": "true",
                "limit": 200, "order": "volume24hr", "ascending": "false",
            },
            timeout=15,
        )
        r.raise_for_status()
        markets = r.json()
    except Exception as e:
        log_event("btc_arb", "gamma_fetch_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return []

    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() + max_hours_out * 3600
    # Match strikes like ``$150k``, ``$95,000``, ``$1.5M``. Capture the
    # numeric and any suffix so we can correctly multiply by 1k/1M.
    strike_re = re.compile(r"\$([\d,.]+)\s*([kKmM]?)\b")

    qualified: list[dict] = []
    for m in markets:
        q = (m.get("question") or "").lower()
        # Accept multiple strike-binary phrasings:
        #   "above $X on <date>" — daily strikes (the cleanest)
        #   "reach $X in <month>" / "hit $X by <date>" — survival probs
        if not ("bitcoin" in q or "btc" in q):
            continue
        if not any(kw in q for kw in ("above", "reach", "hit", "below")):
            continue
        end_iso = m.get("endDate") or ""
        try:
            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if end_dt.timestamp() > cutoff or end_dt < now:
            continue
        strike_match = strike_re.search(m.get("question", ""))
        if not strike_match:
            continue
        try:
            base = float(strike_match.group(1).replace(",", ""))
            suffix = (strike_match.group(2) or "").lower()
            multiplier = {"k": 1_000, "m": 1_000_000}.get(suffix, 1)
            strike = base * multiplier
        except ValueError:
            continue
        # Sanity: BTC strikes should be in [\$10k, \$1M]; anything outside
        # that range is almost certainly a parsing error or unrelated
        # market like "BTC pizza for \$1".
        if not (10_000 <= strike <= 1_000_000):
            continue
        # Need an outcome-price snapshot
        try:
            outcomes = m.get("outcomePrices") or "[]"
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            yes_price = float(outcomes[0]) if outcomes else None
        except (json.JSONDecodeError, TypeError, ValueError):
            yes_price = None
        if yes_price is None or not (0.0 < yes_price < 1.0):
            continue
        qualified.append({
            "id": m.get("conditionId") or m.get("id"),
            "question": m.get("question", ""),
            "strike": strike,
            "yes_price": yes_price,
            "end_iso": end_iso,
            "hours_to_close": (end_dt - now).total_seconds() / 3600,
            "volume_24h": float(m.get("volume24hr", 0) or 0),
        })
    return qualified


# ── Pricing math ────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf — no scipy dep."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def implied_above_probability(
    spot: float,
    strike: float,
    hours_to_close: float,
    *,
    annual_vol: float = DEFAULT_ANNUAL_VOL,
) -> float:
    """Lognormal approximation: P(BTC > strike at T) given current spot.

    Standard Black-Scholes-style:
        d = (ln(spot/strike) + 0.5σ²T) / (σ√T)
        P(S_T > K) = Φ(d - σ√T)          [risk-neutral, drift=0]

    We use Φ(d - σ√T) (rather than Φ(d)) because the question asks
    "will price be ABOVE strike", which is the lower-tail of the
    risk-neutral measure — easier reframed as the survival prob.

    Returns a value in (0, 1). At strike==spot with σ√T ≈ 0.005
    (5 minutes), Φ ≈ 0.498; perfectly intuitive.
    """
    if hours_to_close <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    T = hours_to_close / (365 * 24)
    sigma_sqrt_T = annual_vol * math.sqrt(T)
    if sigma_sqrt_T <= 0:
        return 1.0 if spot > strike else 0.0
    d = (math.log(spot / strike) + 0.5 * annual_vol * annual_vol * T) / sigma_sqrt_T
    return _norm_cdf(d - sigma_sqrt_T)


# ── Signal computation ─────────────────────────────────────────────

def compute_signals(*, annual_vol: float = DEFAULT_ANNUAL_VOL) -> list[BtcArbSignal]:
    """One signal sample across all qualifying BTC strike markets.

    Returns the per-market signal list, sorted by ``abs_gap`` desc
    (largest divergence first).
    """
    spot = fetch_binance_btc_price()
    if spot is None:
        return []
    markets = fetch_polymarket_btc_strikes()
    if not markets:
        return []

    now_iso = datetime.now(timezone.utc).isoformat()
    out: list[BtcArbSignal] = []
    for m in markets:
        implied = implied_above_probability(
            spot=spot, strike=m["strike"],
            hours_to_close=m["hours_to_close"],
            annual_vol=annual_vol,
        )
        gap = implied - float(m["yes_price"])
        out.append(BtcArbSignal(
            sample_at=now_iso,
            spot_usd=spot,
            market_id=str(m["id"]),
            question=m["question"][:140],
            strike_usd=m["strike"],
            yes_price=float(m["yes_price"]),
            implied_yes_prob=round(implied, 4),
            gap=round(gap, 4),
            abs_gap=round(abs(gap), 4),
            hours_to_close=round(m["hours_to_close"], 2),
            volume_24h=m["volume_24h"],
        ))
    out.sort(key=lambda s: s.abs_gap, reverse=True)
    return out


def persist_signals(signals: list[BtcArbSignal]) -> None:
    """Append each signal as a JSONL row. Append-only — historical
    samples are kept so we can analyze gap persistence later.
    """
    if not signals:
        return
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_PATH, "a") as f:
        for s in signals:
            f.write(json.dumps(asdict(s)) + "\n")


def run_signal_cycle(*, annual_vol: float = DEFAULT_ANNUAL_VOL) -> dict:
    """One full sample: compute, persist, log. Returns summary dict.

    The signal cycle is intentionally cheap (~1-2s with 2 HTTP calls)
    so it can run inside a tight loop or be called from a launchd
    cron at any cadence.
    """
    signals = compute_signals(annual_vol=annual_vol)
    persist_signals(signals)
    if signals:
        top = signals[0]
        log_event("btc_arb", "signal_cycle", {
            "n_markets": len(signals),
            "max_abs_gap": top.abs_gap,
            "top_market_id": top.market_id[:16],
            "top_strike": top.strike_usd,
            "spot": top.spot_usd,
        })
    return {
        "n_markets": len(signals),
        "max_abs_gap": signals[0].abs_gap if signals else 0.0,
        "signals": [asdict(s) for s in signals],
    }
