"""
Conformal Prediction — distribution-free prediction intervals with
formal coverage guarantees.

Standard forecasters give you a point estimate ("BTC will close at
$76,800") or a maybe-calibrated probability ("70% chance above
strike"). Conformal prediction gives you something stronger:

    "I am 90% confident the final close will be in [$76,300, $77,100]."

The interval is computed without ANY distributional assumptions. As
long as future calibration errors are exchangeable with historical
errors (a much weaker condition than i.i.d. normality), the interval
is guaranteed to contain the true value at the stated rate.

For Kalshi 15-min markets this is gold:
  • If the strike is OUTSIDE the 90% interval on the favorable side
    (e.g. strike $76,000, our interval [$76,300, $77,100] → 90%+
    confident YES), the trade has very strong directional support.
  • If the strike is INSIDE the interval, we're in coin-flip territory
    — even our best estimator can't be confident which side wins.

Method: split conformal prediction over residuals from a forward-
window forecast. The "forecaster" here is just a naive AR(1)-style
predictor (last close + drift). The conformity scores are
|actual - predicted| from historical 15-min windows.

Public API:
    fit_conformal(historical_bars, alpha=0.10) -> dict (calibrator)
    predict_interval(current_price, calibrator) -> (lo, hi, meta)
    confidence_from_interval(strike, side, lo, hi) -> float

This is the simplest valid conformal implementation; sophisticated
variants exist (CV+, jackknife+, locally-adaptive) but split CP is
the right starting point — robust, fast, and the math is transparent.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Optional


_ROOT = Path(__file__).resolve().parent.parent
_CALIB_PATH = _ROOT / "data" / "kalshi_conformal.json"
DEFAULT_ALPHA = 0.10        # 90% prediction interval
DEFAULT_HORIZON_MIN = 15
DEFAULT_WINDOW_DAYS = 30    # how far back to look for calibration residuals
MIN_CALIBRATION_SAMPLES = 50
_CACHE_TTL_SECONDS = 3600


def _naive_forecast(close_now: float, klines: list[dict], horizon_bars: int) -> float:
    """Trivial baseline forecaster: last close + recent-drift × horizon.

    For BTC 15-min on 5m bars, "recent drift" = mean log-return of the
    last K bars × horizon. We use a weak forecaster intentionally — the
    point of conformal is that you don't need a great forecaster; you
    need a stable one whose residuals are exchangeable.
    """
    closes = [float(k.get("close") or k.get(4)) for k in klines if k]
    if len(closes) < 10:
        return float(close_now)
    # Use last 12 bars of log-returns for drift estimate (=1h on 5m bars)
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(max(1, len(closes) - 12), len(closes))
            if closes[i - 1] > 0]
    if not rets:
        return float(close_now)
    drift = sum(rets) / len(rets)
    return float(close_now * math.exp(drift * horizon_bars))


def fit_conformal(
    bars: list[dict],
    *,
    alpha: float = DEFAULT_ALPHA,
    horizon_bars: int = 3,  # 3 × 5min = 15min
) -> dict:
    """Fit a conformal predictor from historical bars.

    Method: walk forward through `bars`, at each step compute the
    naive forecast for `horizon_bars` ahead and the absolute error
    |actual - predicted|. Collect all errors. The (1-alpha) quantile
    of those errors is the half-width of the prediction interval.

    With alpha=0.10, the 90th percentile of historical errors becomes
    our half-width; future intervals [point ± half_width] will contain
    the true close ~90% of the time IF the future error distribution
    matches the historical one.

    Returns:
        {
            "half_width": float (dollars, the ± band around point forecast),
            "n_residuals": int,
            "alpha": float,
            "horizon_bars": int,
            "fitted_at_epoch": float,
            "is_identity": bool (True if too few samples),
        }
    """
    n = len(bars)
    if n < MIN_CALIBRATION_SAMPLES + horizon_bars + 12:
        return {
            "half_width": None,
            "n_residuals": 0,
            "alpha": alpha,
            "horizon_bars": horizon_bars,
            "fitted_at_epoch": time.time(),
            "is_identity": True,
            "reason": f"insufficient_bars: have {n}, need ~{MIN_CALIBRATION_SAMPLES + horizon_bars + 12}",
        }

    residuals: list[float] = []
    for i in range(12, n - horizon_bars):
        history = bars[: i + 1]
        actual = float(bars[i + horizon_bars].get("close")
                       or bars[i + horizon_bars][4])
        last_close = float(bars[i].get("close") or bars[i][4])
        predicted = _naive_forecast(last_close, history, horizon_bars)
        if predicted > 0 and actual > 0:
            residuals.append(abs(actual - predicted))

    if len(residuals) < MIN_CALIBRATION_SAMPLES:
        return {
            "half_width": None,
            "n_residuals": len(residuals),
            "alpha": alpha,
            "horizon_bars": horizon_bars,
            "fitted_at_epoch": time.time(),
            "is_identity": True,
            "reason": "insufficient_valid_residuals",
        }

    residuals.sort()
    # 1-alpha quantile (e.g., alpha=0.10 → 90th percentile).
    q_idx = int(math.ceil((1 - alpha) * (len(residuals) + 1))) - 1
    q_idx = max(0, min(q_idx, len(residuals) - 1))
    half_width = residuals[q_idx]

    cal = {
        "half_width": float(half_width),
        "n_residuals": len(residuals),
        "alpha": alpha,
        "horizon_bars": horizon_bars,
        "fitted_at_epoch": time.time(),
        "is_identity": False,
    }
    # Persist
    try:
        _CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
        json.dump(cal, open(_CALIB_PATH, "w"))
    except OSError:
        pass
    return cal


def load_calibrator() -> Optional[dict]:
    """Load cached calibrator if fresh, else None (caller refits)."""
    if not _CALIB_PATH.exists():
        return None
    try:
        cal = json.load(open(_CALIB_PATH))
        if (time.time() - cal.get("fitted_at_epoch", 0)) < _CACHE_TTL_SECONDS:
            return cal
    except (OSError, json.JSONDecodeError):
        pass
    return None


def predict_interval(
    current_price: float,
    klines: list[dict],
    *,
    calibrator: Optional[dict] = None,
    horizon_bars: int = 3,
) -> tuple[Optional[float], Optional[float], dict]:
    """Return (lo, hi, meta) — the (1-alpha) prediction interval for the
    close at `horizon_bars` ahead. (None, None, meta) when calibrator
    isn't fit yet.

    point_forecast = _naive_forecast(current_price, klines, horizon_bars)
    interval = [point - half_width, point + half_width]
    """
    if calibrator is None:
        calibrator = load_calibrator()
    if calibrator is None or calibrator.get("is_identity"):
        return None, None, {
            "reason": "calibrator_not_fit",
            "is_identity": (calibrator or {}).get("is_identity", True),
        }

    half_width = float(calibrator["half_width"])
    point = _naive_forecast(current_price, klines, horizon_bars)
    lo = point - half_width
    hi = point + half_width
    return lo, hi, {
        "point_forecast": round(point, 2),
        "half_width": round(half_width, 2),
        "lo": round(lo, 2),
        "hi": round(hi, 2),
        "alpha": calibrator["alpha"],
        "n_residuals": calibrator["n_residuals"],
    }


def confidence_from_interval(
    strike: float,
    side: str,
    lo: Optional[float],
    hi: Optional[float],
) -> tuple[Optional[float], dict]:
    """Map (strike, side, interval) → a confidence in [0, 1].

    Logic:
      • If betting YES (we want close > strike):
          - strike < lo  → strike is below entire interval → very confident YES (≥1 - alpha)
          - strike > hi  → strike above entire interval → confident NO (≤alpha), so YES confidence is LOW
          - strike inside → interpolate; closer to lo → more confident YES

      • Symmetric for NO.

    Returns (confidence_for_our_side, meta) or (None, meta) on failure.
    """
    if lo is None or hi is None or strike is None or side not in ("YES", "NO"):
        return None, {"reason": "missing_inputs"}
    if hi <= lo:
        return None, {"reason": "degenerate_interval"}

    if side == "YES":
        # P(close > strike) approximate:
        if strike <= lo:
            confidence = 0.95
            note = "strike below interval — very confident YES"
        elif strike >= hi:
            confidence = 0.05
            note = "strike above interval — confident NO (low YES conf)"
        else:
            # Linear interp: at lo → 0.95, at hi → 0.05
            t = (strike - lo) / (hi - lo)
            confidence = 0.95 - t * 0.90
            note = f"strike inside interval ({t:.0%} from lo)"
    else:  # NO
        if strike >= hi:
            confidence = 0.95
            note = "strike above interval — very confident NO"
        elif strike <= lo:
            confidence = 0.05
            note = "strike below interval — confident YES (low NO conf)"
        else:
            t = (hi - strike) / (hi - lo)
            confidence = 0.95 - t * 0.90
            note = f"strike inside interval ({t:.0%} from hi)"

    return float(max(0.0, min(1.0, confidence))), {
        "side": side, "strike": strike, "lo": lo, "hi": hi,
        "note": note,
    }
