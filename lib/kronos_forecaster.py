"""
Kronos Forecaster — zero-shot price prediction for prediction markets.

Wraps the Kronos foundation model (pretrained on 45+ global exchanges) to:
    1. Fetch historical OHLCV data for any ticker (yfinance)
    2. Run Kronos to generate future candlestick predictions
    3. Convert price forecasts into probability estimates for binary markets
       (e.g., "Will BTC be above $X by date Y?" → P(close > target))
    4. Feed the probability back into Polybot's Bayesian forecaster

The model runs zero-shot — no fine-tuning needed. It understands candlestick
patterns from pretraining on massive cross-market data.

Security:
    - Model weights downloaded from Hugging Face (verified checksums)
    - All yfinance data treated as untrusted (validated on ingest)
    - No API keys needed — public market data + public model weights
    - Rate limiting on data fetches
    - Results cached to avoid redundant GPU/CPU inference
    - No secrets in any log or error message
"""

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from lib.audit import log_event

DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_DIR = DATA_DIR / "kronos_cache"

# Singleton model — loaded once, reused across calls
_predictor = None
_model_lock = False  # Simple single-process guard


# ── Data Structures ──────────────────────────────────────────────

@dataclass
class KronosForecast:
    """Output of a Kronos price prediction."""
    ticker: str
    interval: str                  # "1d", "1h", "5m"
    lookback_bars: int
    pred_bars: int
    predicted_close: list[float]   # Predicted close prices
    predicted_high: list[float]
    predicted_low: list[float]
    current_price: float           # Last known close
    pred_final_close: float        # Predicted close at end of horizon
    pred_high_watermark: float     # Max predicted high
    pred_low_watermark: float      # Min predicted low
    direction: str                 # "bullish", "bearish", "neutral"
    expected_return: float         # (pred_final - current) / current
    confidence: float              # Based on prediction variance across samples


@dataclass
class PriceProbability:
    """Probability that price crosses a target — for prediction markets."""
    ticker: str
    target_price: float
    direction: str                 # "above" or "below"
    probability: float             # 0.0 - 1.0
    confidence: float              # 0.0 - 1.0
    horizon_bars: int
    interval: str
    forecast: KronosForecast | None = None


# ── Model Loading ────────────────────────────────────────────────

def _load_predictor(
    model_name: str = "NeoQuasar/Kronos-base",
    tokenizer_name: str = "NeoQuasar/Kronos-Tokenizer-base",
    max_context: int = 512,
):
    """
    Load Kronos model + tokenizer. Downloads from HuggingFace on first call.

    Uses singleton pattern — heavy models should only load once per process.
    Returns the KronosPredictor instance.
    """
    global _predictor, _model_lock

    if _predictor is not None:
        return _predictor

    if _model_lock:
        raise RuntimeError("Kronos model is already loading — concurrent load blocked")

    _model_lock = True
    try:
        import torch

        from lib.kronos import Kronos, KronosPredictor, KronosTokenizer

        log_event("kronos", "model_loading", {
            "model": model_name,
            "tokenizer": tokenizer_name,
        })

        tokenizer = KronosTokenizer.from_pretrained(tokenizer_name)
        model = Kronos.from_pretrained(model_name)

        # Auto-detect device (MPS on Mac, CUDA on GPU, CPU fallback)
        device = None
        if torch.cuda.is_available():
            device = "cuda:0"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"

        _predictor = KronosPredictor(
            model=model,
            tokenizer=tokenizer,
            device=device,
            max_context=max_context,
        )

        log_event("kronos", "model_loaded", {
            "model": model_name,
            "device": device,
        }, result="success")

        return _predictor

    except Exception as e:
        log_event("kronos", "model_load_failed", {
            "error": str(e)[:200],
        }, result="failed")
        raise
    finally:
        _model_lock = False


# ── Data Fetching ────────────────────────────────────────────────

# Rate limiter for yfinance
_last_fetch = 0.0


def _fetch_ohlcv(
    ticker: str,
    period: str = "6mo",
    interval: str = "1d",
) -> pd.DataFrame:
    """
    Fetch historical OHLCV data via yfinance.

    Args:
        ticker: Stock/crypto symbol (e.g., "AAPL", "BTC-USD")
        period: Lookback period ("6mo", "1y", "2y")
        interval: Bar interval ("1d", "1h", "5m")

    Returns:
        DataFrame with [open, high, low, close, volume] + DatetimeIndex.
    """
    import yfinance as yf

    global _last_fetch

    # Rate limit: 1 request per 0.5s
    elapsed = time.time() - _last_fetch
    if elapsed < 0.5:
        time.sleep(0.5 - elapsed)

    # Sanitize ticker — only allow alphanumeric, hyphens, dots
    clean_ticker = re.sub(r"[^A-Za-z0-9\-.]", "", ticker)[:20]
    if not clean_ticker:
        raise ValueError(f"Invalid ticker: {ticker}")

    try:
        data = yf.download(
            clean_ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
        )
        _last_fetch = time.time()
    except Exception as e:
        raise RuntimeError(f"Failed to fetch {clean_ticker}: {str(e)[:200]}")

    if data is None or data.empty:
        raise RuntimeError(f"No data returned for {clean_ticker}")

    # Normalize column names (yfinance returns capitalized)
    data.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in data.columns]

    # Validate — all data is untrusted
    required = ["open", "high", "low", "close"]
    for col in required:
        if col not in data.columns:
            raise RuntimeError(f"Missing column '{col}' in {clean_ticker} data")

    # Drop NaN rows
    data = data.dropna(subset=required)

    if len(data) < 30:
        raise RuntimeError(f"Insufficient data for {clean_ticker}: {len(data)} bars (need 30+)")

    # Ensure volume exists
    if "volume" not in data.columns:
        data["volume"] = 0.0

    return data


# ── Cache ────────────────────────────────────────────────────────

def _cache_key(ticker: str, interval: str, pred_bars: int) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = f"{ticker}:{interval}:{pred_bars}:{today}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_get(key: str, ttl_minutes: int = 60) -> dict | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        cached_at = datetime.fromisoformat(data.get("cached_at", ""))
        if datetime.now(timezone.utc) - cached_at > timedelta(minutes=ttl_minutes):
            return None
        return data
    except (json.JSONDecodeError, ValueError, KeyError):
        return None


def _cache_put(key: str, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data["cached_at"] = datetime.now(timezone.utc).isoformat()
    path = CACHE_DIR / f"{key}.json"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    tmp.rename(path)


# ── Core Prediction ──────────────────────────────────────────────

def predict_price(
    ticker: str,
    pred_bars: int = 30,
    interval: str = "1d",
    lookback: int = 400,
    sample_count: int = 5,
    temperature: float = 0.8,
    model_name: str = "NeoQuasar/Kronos-base",
) -> KronosForecast:
    """
    Predict future prices for a ticker using Kronos.

    This is the main function for raw price forecasting. For prediction
    market probability estimates, use `price_to_probability()` instead.

    Args:
        ticker: Stock/crypto symbol (e.g., "AAPL", "BTC-USD", "ETH-USD")
        pred_bars: Number of future bars to predict
        interval: Bar interval ("1d" for daily, "1h" for hourly)
        lookback: Number of historical bars to feed the model (max 512)
        sample_count: Number of inference samples to average (more = smoother)
        temperature: Sampling temperature (lower = more conservative)
        model_name: HuggingFace model ID

    Returns:
        KronosForecast with predicted OHLCV and direction.
    """
    # Check cache
    key = _cache_key(ticker, interval, pred_bars)
    cached = _cache_get(key, ttl_minutes=60)
    if cached and "predicted_close" in cached:
        return KronosForecast(
            ticker=cached["ticker"],
            interval=cached["interval"],
            lookback_bars=cached["lookback_bars"],
            pred_bars=cached["pred_bars"],
            predicted_close=cached["predicted_close"],
            predicted_high=cached["predicted_high"],
            predicted_low=cached["predicted_low"],
            current_price=cached["current_price"],
            pred_final_close=cached["pred_final_close"],
            pred_high_watermark=cached["pred_high_watermark"],
            pred_low_watermark=cached["pred_low_watermark"],
            direction=cached["direction"],
            expected_return=cached["expected_return"],
            confidence=cached["confidence"],
        )

    # Fetch data
    period_map = {"1d": "2y", "1h": "60d", "5m": "7d"}
    period = period_map.get(interval, "1y")
    data = _fetch_ohlcv(ticker, period=period, interval=interval)

    # Clamp lookback to available data and model max context
    lookback = min(lookback, len(data), 512)
    data = data.tail(lookback + pred_bars)  # Extra for timestamp generation

    # Prepare input
    hist = data.head(lookback)
    x_df = hist[["open", "high", "low", "close", "volume"]].copy()
    x_df["amount"] = x_df["volume"] * x_df["close"]

    x_timestamp = pd.DatetimeIndex(hist.index)

    # Generate future timestamps
    if interval == "1d":
        freq = pd.tseries.offsets.BDay(1)
    elif interval == "1h":
        freq = pd.tseries.offsets.Hour(1)
    else:
        freq = pd.tseries.offsets.Minute(5)

    last_ts = x_timestamp[-1]
    y_timestamp = pd.date_range(start=last_ts + freq, periods=pred_bars, freq=freq)

    # Load model and predict
    predictor = _load_predictor(model_name=model_name)

    pred_df = predictor.predict(
        df=x_df,
        x_timestamp=x_timestamp,
        y_timestamp=y_timestamp,
        pred_len=pred_bars,
        T=temperature,
        top_p=0.9,
        sample_count=sample_count,
        verbose=False,
    )

    # Extract predictions
    pred_close = pred_df["close"].values.tolist()
    pred_high = pred_df["high"].values.tolist()
    pred_low = pred_df["low"].values.tolist()

    current_price = float(hist["close"].iloc[-1])
    pred_final = float(pred_close[-1])
    high_watermark = float(max(pred_high))
    low_watermark = float(min(pred_low))

    expected_return = (pred_final - current_price) / current_price if current_price > 0 else 0.0

    # Direction determination
    if expected_return > 0.02:
        direction = "bullish"
    elif expected_return < -0.02:
        direction = "bearish"
    else:
        direction = "neutral"

    # Confidence from prediction variance (multi-sample)
    # If sample_count > 1, Kronos internally averages — measure spread of predictions
    pred_range = high_watermark - low_watermark
    normalized_range = pred_range / current_price if current_price > 0 else 1.0
    # Tighter predicted range = higher confidence
    confidence = max(0.1, min(1.0 - normalized_range, 0.9))

    forecast = KronosForecast(
        ticker=ticker,
        interval=interval,
        lookback_bars=lookback,
        pred_bars=pred_bars,
        predicted_close=[round(p, 4) for p in pred_close],
        predicted_high=[round(p, 4) for p in pred_high],
        predicted_low=[round(p, 4) for p in pred_low],
        current_price=round(current_price, 4),
        pred_final_close=round(pred_final, 4),
        pred_high_watermark=round(high_watermark, 4),
        pred_low_watermark=round(low_watermark, 4),
        direction=direction,
        expected_return=round(expected_return, 4),
        confidence=round(confidence, 4),
    )

    # Cache result
    _cache_put(key, {
        "ticker": forecast.ticker,
        "interval": forecast.interval,
        "lookback_bars": forecast.lookback_bars,
        "pred_bars": forecast.pred_bars,
        "predicted_close": forecast.predicted_close,
        "predicted_high": forecast.predicted_high,
        "predicted_low": forecast.predicted_low,
        "current_price": forecast.current_price,
        "pred_final_close": forecast.pred_final_close,
        "pred_high_watermark": forecast.pred_high_watermark,
        "pred_low_watermark": forecast.pred_low_watermark,
        "direction": forecast.direction,
        "expected_return": forecast.expected_return,
        "confidence": forecast.confidence,
    })

    log_event("kronos", "prediction_complete", {
        "ticker": ticker,
        "interval": interval,
        "pred_bars": pred_bars,
        "current_price": forecast.current_price,
        "pred_final_close": forecast.pred_final_close,
        "direction": forecast.direction,
        "expected_return": forecast.expected_return,
    }, result="success")

    return forecast


# ── Price → Probability Conversion ───────────────────────────────

def price_to_probability(
    ticker: str,
    target_price: float,
    direction: str = "above",
    horizon_bars: int = 30,
    interval: str = "1d",
    sample_count: int = 10,
    model_name: str = "NeoQuasar/Kronos-base",
) -> PriceProbability:
    """
    Estimate the probability that a price will be above/below a target.

    This is the key function for prediction market integration.
    For a market like "Will BTC be above $70,000 by June 30?":
        - ticker = "BTC-USD"
        - target_price = 70000
        - direction = "above"
        - horizon_bars = days until June 30

    Uses multi-sample Monte Carlo: runs Kronos multiple times with
    different sampling paths, counts what fraction cross the target.

    Args:
        ticker: Stock/crypto symbol
        target_price: The price threshold
        direction: "above" or "below"
        horizon_bars: How many bars into the future
        interval: Bar interval
        sample_count: Number of independent Kronos samples (more = better probability estimate)
        model_name: HuggingFace model ID

    Returns:
        PriceProbability with the estimated probability.
    """
    if direction not in ("above", "below"):
        raise ValueError(f"direction must be 'above' or 'below', got '{direction}'")

    # Run multiple independent forecasts
    # Each with sample_count=1 so we get independent paths
    crosses = 0
    all_finals = []

    predictor = _load_predictor(model_name=model_name)

    # Fetch data once
    period_map = {"1d": "2y", "1h": "60d", "5m": "7d"}
    period = period_map.get(interval, "1y")
    data = _fetch_ohlcv(ticker, period=period, interval=interval)

    lookback = min(400, len(data), 512)
    hist = data.tail(lookback)

    x_df = hist[["open", "high", "low", "close", "volume"]].copy()
    x_df["amount"] = x_df["volume"] * x_df["close"]
    x_timestamp = pd.DatetimeIndex(hist.index)

    if interval == "1d":
        freq = pd.tseries.offsets.BDay(1)
    elif interval == "1h":
        freq = pd.tseries.offsets.Hour(1)
    else:
        freq = pd.tseries.offsets.Minute(5)

    last_ts = x_timestamp[-1]
    y_timestamp = pd.date_range(start=last_ts + freq, periods=horizon_bars, freq=freq)

    for i in range(sample_count):
        try:
            pred_df = predictor.predict(
                df=x_df,
                x_timestamp=x_timestamp,
                y_timestamp=y_timestamp,
                pred_len=horizon_bars,
                T=0.9,  # Slight temperature for diversity
                top_p=0.92,
                sample_count=1,
                verbose=False,
            )

            final_close = float(pred_df["close"].iloc[-1])
            all_finals.append(final_close)

            # Check if ANY bar in the prediction crosses the target
            if direction == "above":
                if pred_df["high"].max() >= target_price:
                    crosses += 1
            else:
                if pred_df["low"].min() <= target_price:
                    crosses += 1

        except Exception as e:
            log_event("kronos", "sample_failed", {
                "ticker": ticker, "sample": i, "error": str(e)[:200],
            }, result="failed")

    if not all_finals:
        return PriceProbability(
            ticker=ticker,
            target_price=target_price,
            direction=direction,
            probability=0.5,  # No data — neutral
            confidence=0.0,
            horizon_bars=horizon_bars,
            interval=interval,
        )

    # Primary probability: fraction of paths that cross
    raw_prob = crosses / len(all_finals)

    # Secondary: what fraction of final closes beat the target
    if direction == "above":
        final_prob = sum(1 for f in all_finals if f >= target_price) / len(all_finals)
    else:
        final_prob = sum(1 for f in all_finals if f <= target_price) / len(all_finals)

    # Blend: 60% path-crossing (stronger signal), 40% final-close
    probability = 0.6 * raw_prob + 0.4 * final_prob

    # Clamp to avoid extremes with limited samples
    probability = max(0.05, min(probability, 0.95))

    # Confidence from sample agreement
    if len(all_finals) >= 3:
        std = float(np.std(all_finals))
        mean = float(np.mean(all_finals))
        cv = std / abs(mean) if abs(mean) > 0 else 1.0
        confidence = max(0.1, min(1.0 - cv * 2, 0.9))
    else:
        confidence = 0.2

    current_price = float(hist["close"].iloc[-1])

    # Build a summary forecast from averages
    forecast = KronosForecast(
        ticker=ticker,
        interval=interval,
        lookback_bars=lookback,
        pred_bars=horizon_bars,
        predicted_close=[round(f, 4) for f in all_finals],
        predicted_high=[],
        predicted_low=[],
        current_price=round(current_price, 4),
        pred_final_close=round(float(np.mean(all_finals)), 4),
        pred_high_watermark=round(float(max(all_finals)), 4),
        pred_low_watermark=round(float(min(all_finals)), 4),
        direction="bullish" if np.mean(all_finals) > current_price else "bearish",
        expected_return=round((float(np.mean(all_finals)) - current_price) / current_price, 4),
        confidence=round(confidence, 4),
    )

    result = PriceProbability(
        ticker=ticker,
        target_price=target_price,
        direction=direction,
        probability=round(probability, 4),
        confidence=round(confidence, 4),
        horizon_bars=horizon_bars,
        interval=interval,
        forecast=forecast,
    )

    log_event("kronos", "probability_computed", {
        "ticker": ticker,
        "target": target_price,
        "direction": direction,
        "probability": result.probability,
        "confidence": result.confidence,
        "samples": len(all_finals),
        "crosses": crosses,
    }, result="success")

    return result


# ── Market Question Parser ───────────────────────────────────────

# Patterns to extract ticker + target from prediction market questions
_PRICE_PATTERNS = [
    # "Will Bitcoin be above $70,000 by June?"
    re.compile(
        r"will\s+(\w+)\s+(?:be\s+)?(?:close\s+)?(?:above|over|exceed)\s+\$?([\d,]+\.?\d*)",
        re.IGNORECASE,
    ),
    # "Will AAPL close below $150?"
    re.compile(
        r"will\s+(\w+)\s+(?:close\s+)?(?:below|under|fall below)\s+\$?([\d,]+\.?\d*)",
        re.IGNORECASE,
    ),
    # "BTC above $100k"
    re.compile(
        r"(\w+)\s+(?:above|over)\s+\$?([\d,]+\.?\d*[kKmM]?)",
        re.IGNORECASE,
    ),
    # "Will the price of ETH reach $5000?"
    re.compile(
        r"(?:price of|price for)\s+(\w+)\s+(?:reach|hit|exceed)\s+\$?([\d,]+\.?\d*)",
        re.IGNORECASE,
    ),
]

# Common name → ticker mapping
_TICKER_MAP = {
    "bitcoin": "BTC-USD", "btc": "BTC-USD",
    "ethereum": "ETH-USD", "eth": "ETH-USD",
    "solana": "SOL-USD", "sol": "SOL-USD",
    "dogecoin": "DOGE-USD", "doge": "DOGE-USD",
    "xrp": "XRP-USD", "ripple": "XRP-USD",
    "cardano": "ADA-USD", "ada": "ADA-USD",
    "apple": "AAPL", "aapl": "AAPL",
    "google": "GOOGL", "googl": "GOOGL",
    "microsoft": "MSFT", "msft": "MSFT",
    "amazon": "AMZN", "amzn": "AMZN",
    "tesla": "TSLA", "tsla": "TSLA",
    "nvidia": "NVDA", "nvda": "NVDA",
    "meta": "META",
    "sp500": "^GSPC", "s&p": "^GSPC", "s&p500": "^GSPC",
    "nasdaq": "^IXIC",
    "gold": "GC=F", "silver": "SI=F",
    "oil": "CL=F", "crude": "CL=F",
}


def _parse_quantity(s: str) -> float:
    """Parse price strings like '70,000', '100k', '1.5M'."""
    s = s.replace(",", "")
    multiplier = 1.0
    if s.lower().endswith("k"):
        multiplier = 1_000
        s = s[:-1]
    elif s.lower().endswith("m"):
        multiplier = 1_000_000
        s = s[:-1]
    return float(s) * multiplier


def parse_price_market(question: str) -> dict | None:
    """
    Attempt to extract ticker, target price, and direction from a
    prediction market question.

    Returns:
        {"ticker": "BTC-USD", "target": 70000.0, "direction": "above"}
        or None if the question isn't about a price.
    """
    for pattern in _PRICE_PATTERNS:
        match = pattern.search(question)
        if match:
            asset = match.group(1).lower().strip()
            target_str = match.group(2)

            # Resolve ticker
            ticker = _TICKER_MAP.get(asset, asset.upper())

            try:
                target = _parse_quantity(target_str)
            except (ValueError, TypeError):
                continue

            # Determine direction from the matched pattern
            question_lower = question.lower()
            if any(w in question_lower for w in ["below", "under", "fall"]):
                direction = "below"
            else:
                direction = "above"

            return {
                "ticker": ticker,
                "target": target,
                "direction": direction,
            }

    return None


# ── Integration with Polybot Forecaster ──────────────────────────

def get_kronos_estimate(
    market_question: str,
    horizon_days: int = 30,
    sample_count: int = 10,
) -> float | None:
    """
    High-level entry point for the Polybot forecaster pipeline.

    Takes a prediction market question, checks if it's about a price,
    and if so, returns a Kronos-based probability estimate.

    This plugs into estimate_probability() as another Bayesian source.

    Args:
        market_question: The prediction market question text
        horizon_days: Days until market resolution
        sample_count: Number of Kronos samples for MC probability

    Returns:
        Probability (0.0-1.0) or None if the market isn't price-based.
    """
    parsed = parse_price_market(market_question)
    if parsed is None:
        return None  # Not a price-based market — Kronos can't help

    try:
        result = price_to_probability(
            ticker=parsed["ticker"],
            target_price=parsed["target"],
            direction=parsed["direction"],
            horizon_bars=max(1, min(horizon_days, 120)),
            interval="1d",
            sample_count=sample_count,
        )
        return result.probability
    except Exception as e:
        log_event("kronos", "estimate_failed", {
            "question": market_question[:100],
            "error": str(e)[:200],
        }, result="failed")
        return None


# ── CLI Helpers ──────────────────────────────────────────────────

def print_forecast_report(forecast: KronosForecast):
    """Print a formatted Kronos forecast to terminal."""
    print("=" * 60)
    print(f"  KRONOS FORECAST — {forecast.ticker} ({forecast.interval})")
    print("=" * 60)
    print(f"  Current Price:    ${forecast.current_price:,.2f}")
    print(f"  Predicted Close:  ${forecast.pred_final_close:,.2f} ({forecast.pred_bars} bars out)")
    print(f"  Expected Return:  {forecast.expected_return:+.2%}")
    print(f"  Direction:        {forecast.direction.upper()}")
    print(f"  Confidence:       {forecast.confidence:.0%}")
    print(f"  High Watermark:   ${forecast.pred_high_watermark:,.2f}")
    print(f"  Low Watermark:    ${forecast.pred_low_watermark:,.2f}")
    print(f"  Lookback:         {forecast.lookback_bars} bars")

    # Show predicted trajectory (sampled points)
    n = len(forecast.predicted_close)
    if n > 5:
        points = [0, n // 4, n // 2, 3 * n // 4, n - 1]
        print(f"\n  Trajectory:")
        for i in points:
            bar_label = f"T+{i + 1}"
            price = forecast.predicted_close[i]
            change = (price - forecast.current_price) / forecast.current_price
            print(f"    {bar_label:6s}  ${price:,.2f}  ({change:+.2%})")

    print("=" * 60)


def print_probability_report(result: PriceProbability):
    """Print a formatted probability estimate to terminal."""
    print("=" * 60)
    print(f"  KRONOS PROBABILITY — {result.ticker}")
    print("=" * 60)
    print(f"  Question:       Will {result.ticker} be {result.direction} ${result.target_price:,.2f}?")
    print(f"  Probability:    {result.probability:.1%}")
    print(f"  Confidence:     {result.confidence:.0%}")
    print(f"  Horizon:        {result.horizon_bars} {result.interval} bars")

    if result.forecast:
        f = result.forecast
        print(f"\n  Current Price:  ${f.current_price:,.2f}")
        print(f"  Predicted:      ${f.pred_final_close:,.2f} ({f.expected_return:+.2%})")
        print(f"  Direction:      {f.direction.upper()}")
        print(f"  Range:          ${f.pred_low_watermark:,.2f} — ${f.pred_high_watermark:,.2f}")

    print("=" * 60)
