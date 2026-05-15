"""
Kalshi 15-min BTC signal — parallel to ``btc_5min_signal.py`` but for
Kalshi's KXBTC15M series. The mechanic is structurally identical:

  * Each market opens with a strike = BTC price at window-open
  * Title: "BTC price up in next 15 mins?"
  * strike_type: greater_or_equal → YES wins if BTC ≥ strike at close
  * 15-minute window, auto-settling on Binance.US-derived index

Why a separate module from ``btc_5min_signal``:
  * Different exchange API (Kalshi vs Polymarket Gamma)
  * Different ticker structure (KXBTC15M-<YYMMM><DD><HHMM> vs slug)
  * Strike comes from market metadata directly (no need to infer the
    window-open price from klines — Kalshi tells us exactly)
  * 15-minute windows give us 3× more klines to work with, so RSI
    and EMA cross are more reliable signals here than on Polymarket

Auth: market browsing is PUBLIC — no API key needed for sampling.
Phase 3 (real orders) will use the kalshi_auth signing path.

Persistence: ``data/kalshi_15min_signal.jsonl`` (mirrors the
Polymarket-side jsonl so the same analysis tooling works on both).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from lib.audit import log_event
from lib.btc_5min_signal import (
    fetch_binance_btc_price, fetch_binance_klines,
    _ema, _rsi,
)

SIGNAL_PATH = Path(__file__).parent.parent / "data" / "kalshi_15min_signal.jsonl"
KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES = "KXBTC15M"

# Ticker example: KXBTC15M-26MAY150830-30 (date suffix + strike-level marker)
TICKER_RE = re.compile(r"^KXBTC15M-(\d{2}[A-Z]{3}\d{2}\d{4})(?:-\d+)?$")


@dataclass
class KalshiFifteenMinSample:
    """One snapshot of one 15-min Kalshi BTC market."""
    sample_at: str
    market_ticker: str           # e.g. KXBTC15M-26MAY150830-30
    event_ticker: str            # parent event
    title: str
    open_time: str               # ISO from Kalshi
    close_time: str              # ISO from Kalshi
    seconds_to_close: float
    strike: float                # floor_strike — BTC price at window-open
    yes_bid: float | None        # dollars, 0..1 (Kalshi returns cents normally,
    yes_ask: float | None        # but _dollars suffix returns the float directly)
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume_24h: float
    spot_usd: float              # Binance.US spot at sample time
    indicators: dict | None = None


# ── Discovery ────────────────────────────────────────────────────────

def discover_15min_btc_markets(
    *,
    max_seconds_out: int = 900,
) -> list[dict]:
    """Find currently-live Kalshi BTC 15-min markets resolving within
    ``max_seconds_out`` seconds.

    Two-step fetch — events first, then markets per event — because
    Kalshi separates the date container (event) from the strike-priced
    binary (market). Default ``max_seconds_out=900`` covers a full
    15-min window so we see every sample of every live market.
    """
    import requests

    try:
        r = requests.get(
            f"{KALSHI_HOST}/events",
            params={"series_ticker": KALSHI_SERIES,
                    "status": "open", "limit": 50},
            timeout=15,
        )
        r.raise_for_status()
        events = r.json().get("events", []) or []
    except Exception as e:
        log_event("kalshi_15min", "events_fetch_failed",
                  {"error": str(e)[:200]}, result="degraded")
        return []

    now = datetime.now(timezone.utc)
    qualified: list[dict] = []

    for e in events:
        et = e.get("event_ticker", "")
        if not et:
            continue
        try:
            mr = requests.get(
                f"{KALSHI_HOST}/markets",
                params={"event_ticker": et, "status": "open"},
                timeout=15,
            )
            mr.raise_for_status()
            markets = mr.json().get("markets", []) or []
        except Exception:
            continue

        for m in markets:
            if m.get("status") != "active":
                continue
            close_iso = m.get("close_time") or ""
            try:
                close_dt = datetime.fromisoformat(
                    close_iso.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                continue
            seconds_to_close = (close_dt - now).total_seconds()
            if seconds_to_close < -60 or seconds_to_close > max_seconds_out:
                continue

            strike = m.get("floor_strike")
            if strike is None:
                continue

            # Kalshi has both *_bid/*_ask in cents and *_bid_dollars/*_ask_dollars
            # as the canonical 0..1 floats. Prefer the dollars form.
            def _as_price(val):
                if val is None:
                    return None
                try:
                    f = float(val)
                except (ValueError, TypeError):
                    return None
                # If looks like cents (>1.0), convert
                return f / 100.0 if f > 1.5 else f

            qualified.append({
                "ticker": m.get("ticker", ""),
                "event_ticker": et,
                "title": m.get("title", "")[:200],
                "open_time": m.get("open_time", ""),
                "close_time": close_iso,
                "seconds_to_close": seconds_to_close,
                "strike": float(strike),
                "yes_bid": _as_price(m.get("yes_bid_dollars")
                                     if m.get("yes_bid_dollars") is not None
                                     else m.get("yes_bid")),
                "yes_ask": _as_price(m.get("yes_ask_dollars")
                                     if m.get("yes_ask_dollars") is not None
                                     else m.get("yes_ask")),
                "no_bid": _as_price(m.get("no_bid_dollars")
                                    if m.get("no_bid_dollars") is not None
                                    else m.get("no_bid")),
                "no_ask": _as_price(m.get("no_ask_dollars")
                                    if m.get("no_ask_dollars") is not None
                                    else m.get("no_ask")),
                "last_price": _as_price(m.get("last_price_dollars")
                                        if m.get("last_price_dollars") is not None
                                        else m.get("last_price")),
                "volume_24h": float(m.get("volume_24h_fp", 0) or 0),
            })
    qualified.sort(key=lambda x: x["seconds_to_close"])
    return qualified


# ── Indicators (15-min variant) ──────────────────────────────────────

def compute_kalshi_indicators(
    *,
    klines: list[dict],
    strike: float,
    current_spot: float,
) -> dict:
    """Same 6-indicator composite as the Polymarket 5-min path, but
    uses the Kalshi-provided strike directly as the window-open price
    (no klines-search needed).

    Composite is signed: positive = expect BTC closes ABOVE strike → YES.
    Negative = expect BTC closes BELOW strike → NO.
    """
    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]

    # 1. Window Delta — % move from strike (= window open) to now.
    #    Same 0.10% saturation as the 5-min variant but using a 15-min
    #    window we expect slightly larger absolute moves; keep the
    #    threshold the same for cross-platform comparability.
    if strike > 0:
        window_delta_pct = (current_spot - strike) / strike * 100.0
    else:
        window_delta_pct = None

    # 2-6: identical to btc_5min_signal.compute_indicators
    if len(klines) >= 2:
        m1 = klines[-1]["close"] - klines[-1]["open"]
        m2 = klines[-2]["close"] - klines[-2]["open"]
        micro_momentum = (1 if m1 > 0 else -1 if m1 < 0 else 0) + \
                         (1 if m2 > 0 else -1 if m2 < 0 else 0)
    else:
        micro_momentum = 0

    if len(closes) >= 3:
        acceleration = (closes[-1] - closes[-2]) - (closes[-2] - closes[-3])
    else:
        acceleration = 0.0

    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    ema_cross = 0.0
    if ema9 is not None and ema21 is not None and ema21 > 0:
        ema_cross = (ema9 - ema21) / ema21 * 100.0

    rsi = _rsi(closes, 14)

    if len(volumes) >= 15:
        recent = volumes[-1]
        trailing_avg = sum(volumes[-15:-1]) / 14
        vol_surge = recent / trailing_avg if trailing_avg > 0 else 1.0
    else:
        vol_surge = 1.0

    contribs: dict[str, float] = {}
    if window_delta_pct is not None:
        contribs["window_delta"] = max(-1.0, min(1.0, window_delta_pct / 0.10)) * 6.0
    else:
        contribs["window_delta"] = 0.0
    contribs["micro_momentum"] = (micro_momentum / 2.0) * 2.0
    contribs["acceleration"] = max(-1.0, min(1.0, acceleration / 50.0)) * 1.5
    contribs["ema_cross"] = max(-1.0, min(1.0, ema_cross / 0.05)) * 1.0
    if rsi is not None:
        rsi_norm = (50.0 - rsi) / 20.0
        contribs["rsi"] = max(-1.5, min(1.5, rsi_norm)) * 1.0
    else:
        contribs["rsi"] = 0.0
    direction_sign = 1 if micro_momentum > 0 else -1 if micro_momentum < 0 else 0
    if vol_surge >= 1.5 and direction_sign != 0:
        contribs["volume_surge"] = direction_sign * 1.0
    else:
        contribs["volume_surge"] = 0.0

    composite = sum(contribs.values())
    max_possible = 13.0
    return {
        "strike": strike,
        "current_spot": current_spot,
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
        "direction": "YES" if composite > 0 else "NO" if composite < 0 else "FLAT",
    }


# ── Sampling ─────────────────────────────────────────────────────────

def sample_signals(
    *,
    max_seconds_out: int = 900,
    with_indicators: bool = True,
) -> list[KalshiFifteenMinSample]:
    """One sweep — spot + klines + every qualifying Kalshi 15-min BTC market."""
    spot = fetch_binance_btc_price()
    if spot is None:
        return []
    markets = discover_15min_btc_markets(max_seconds_out=max_seconds_out)
    if not markets:
        return []

    klines = fetch_binance_klines() if with_indicators else None
    now_iso = datetime.now(timezone.utc).isoformat()
    out: list[KalshiFifteenMinSample] = []
    for m in markets:
        indicators = None
        if klines:
            indicators = compute_kalshi_indicators(
                klines=klines, strike=m["strike"], current_spot=spot,
            )
        out.append(KalshiFifteenMinSample(
            sample_at=now_iso,
            market_ticker=m["ticker"],
            event_ticker=m["event_ticker"],
            title=m["title"],
            open_time=m["open_time"],
            close_time=m["close_time"],
            seconds_to_close=round(m["seconds_to_close"], 2),
            strike=m["strike"],
            yes_bid=m["yes_bid"], yes_ask=m["yes_ask"],
            no_bid=m["no_bid"], no_ask=m["no_ask"],
            last_price=m["last_price"],
            volume_24h=m["volume_24h"],
            spot_usd=spot,
            indicators=indicators,
        ))
    return out


def persist_samples(samples: list[KalshiFifteenMinSample]) -> None:
    if not samples:
        return
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SIGNAL_PATH, "a") as f:
        for s in samples:
            f.write(json.dumps(asdict(s)) + "\n")


def run_signal_cycle(
    *,
    max_seconds_out: int = 900,
    record_paper_trades: bool = True,
    settle_paper_trades: bool = True,
) -> dict:
    """Full sweep: discover + sample + persist + (paper record + settle)."""
    samples = sample_signals(max_seconds_out=max_seconds_out)
    persist_samples(samples)

    n_paper_opened = 0
    if record_paper_trades and samples:
        try:
            from lib.kalshi_15min_paper import record_paper_trades_from_samples
            new_trades = record_paper_trades_from_samples(
                [asdict(s) for s in samples]
            )
            n_paper_opened = len(new_trades)
        except Exception as e:
            log_event("kalshi_15min", "paper_record_failed",
                      {"error": str(e)[:200]}, result="degraded")

    settle_summary = {}
    if settle_paper_trades:
        try:
            from lib.kalshi_15min_paper import (
                settle_paper_trades as _settle_paper_trades,
            )
            settle_summary = _settle_paper_trades()
        except Exception as e:
            log_event("kalshi_15min", "paper_settle_failed",
                      {"error": str(e)[:200]}, result="degraded")

    if samples:
        nearest = samples[0]
        log_event("kalshi_15min", "signal_cycle", {
            "n_markets": len(samples),
            "nearest_seconds_to_close": nearest.seconds_to_close,
            "nearest_strike": nearest.strike,
            "spot": nearest.spot_usd,
            "paper_trades_opened": n_paper_opened,
            "paper_settled": settle_summary.get("settled_now", 0),
        })
    else:
        log_event("kalshi_15min", "no_active_markets", {}, result="degraded")

    return {
        "n_markets": len(samples),
        "nearest_seconds_to_close": samples[0].seconds_to_close if samples else None,
        "spot_usd": samples[0].spot_usd if samples else None,
        "samples": [asdict(s) for s in samples],
        "paper_trades_opened": n_paper_opened,
        "settle_summary": settle_summary,
    }
