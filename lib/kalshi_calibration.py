"""
Isotonic Calibration — convert raw composite confidence into actual
empirical win probability.

The bot's confidence score is in [0, 1] but it's NOT a probability —
it's the ratio of composite to max_possible weighted-sum. When the bot
says "confidence 0.70," is that historically a 70% win rate? Almost
certainly not. Calibration measures the gap and corrects it.

This module fits a monotonic (isotonic) regression from raw confidence
→ realized win rate over a rolling window of resolved trades. Once
fit, the calibrator is a lookup: `calibrated_prob = f(raw_confidence)`.

Why isotonic vs other calibration methods:
  • Doesn't assume a functional form (vs Platt scaling which assumes
    sigmoid). Works for arbitrarily-shaped miscalibration curves.
  • Monotonic — higher raw confidence ALWAYS maps to higher calibrated
    probability, which is the correct prior. Avoids weird inversions.
  • Cheap and parameter-free.

The standard implementation is sklearn.isotonic.IsotonicRegression but
we don't have sklearn-class deps loose; this is a pure-Python
implementation of the "pool-adjacent-violators" (PAV) algorithm, ~60
LOC, no deps.

Usage:
    from lib.kalshi_calibration import fit_calibrator, calibrate

    calibrator = fit_calibrator()  # reads resolved paper trades
    p_calibrated = calibrate(raw_conf=0.72, calibrator=calibrator)

The calibrator is fit on demand and cached for 1h. Refit triggered
automatically when N new resolved trades have arrived since last fit.

Below MIN_SAMPLES (default 20) resolved trades, returns the identity
function (calibrated == raw) — we don't trust calibration on small
samples.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from tradingcore.audit import log_event


_ROOT = Path(__file__).resolve().parent.parent
_PAPER_PATH = _ROOT / "data" / "kalshi_15min_paper.jsonl"
_CALIB_CACHE_PATH = _ROOT / "data" / "kalshi_calibrator.json"


MIN_SAMPLES = 20    # need at least this many resolved trades to fit
N_BUCKETS = 10      # confidence-bucket granularity for the empirical curve
CACHE_TTL_SECONDS = 3600  # refit every hour at most


def _load_resolved_trades() -> list[dict]:
    """Read TERMINALLY-resolved paper trades from the Kalshi 15-min log.

    Calibration maps entry-confidence -> P(the model's directional call was
    right at EXPIRY). Only trades held to resolution (status won/lost) measure
    that. We deliberately EXCLUDE the path-dependent early exits:
      * won_early = take-profit fired (price touched TP before expiry)
      * cut_loss  = stop-loss fired (price touched SL before expiry)
    Those labels reflect the intra-window PRICE PATH and the TP/SL distances,
    not whether entry confidence predicted the terminal YES/NO outcome. Fitting
    on them learns 'P(TP fires before SL)' — a function of the exit policy, not
    of confidence calibration — and a tighter stop would mechanically depress
    the realized win-rate at every confidence bucket, making the model look
    overconfident when it's the exit policy talking. (Audit finding, 2026-06-01.
    In the live 15-min log these early exits were 52% of resolved trades.)
    """
    if not _PAPER_PATH.exists():
        return []
    rows = []
    try:
        with open(_PAPER_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("status") in ("won", "lost"):
                    rows.append(r)
    except OSError:
        pass
    return rows


def _isotonic_pav(xs: list[float], ys: list[float]) -> list[tuple[float, float]]:
    """Pool-Adjacent-Violators algorithm — pure-Python isotonic regression.

    Returns a list of (x_anchor, y_anchor) pairs defining a piecewise-
    linear monotonic mapping. Interpolate between anchors at query time.

    Given (xs, ys) sorted by xs ascending, returns the monotonically-
    non-decreasing y values that minimize squared error to original ys.

    Standard PAV: walk through ys, pool any segment whose mean would
    decrease backwards, repeat until monotone.
    """
    n = len(xs)
    if n == 0:
        return []
    # Each block: (sum_y, weight=count, start_idx, end_idx)
    blocks: list[list[float]] = []
    for i, y in enumerate(ys):
        blocks.append([float(y), 1.0, float(i), float(i)])
        # Merge backwards while monotonicity is violated.
        while len(blocks) >= 2:
            cur = blocks[-1]
            prev = blocks[-2]
            if cur[0] / cur[1] < prev[0] / prev[1]:
                blocks[-2] = [
                    prev[0] + cur[0],
                    prev[1] + cur[1],
                    prev[2],
                    cur[3],
                ]
                blocks.pop()
            else:
                break

    # Expand pooled means back to anchors at the START of each pool.
    anchors: list[tuple[float, float]] = []
    for b in blocks:
        s, w, start, _end = b
        mean = s / w
        anchors.append((xs[int(start)], mean))
    # Make sure last anchor covers up to the max x too — append last x
    # if not already.
    if anchors[-1][0] < xs[-1]:
        anchors.append((xs[-1], anchors[-1][1]))
    return anchors


def fit_calibrator(force: bool = False) -> dict:
    """Fit (or load cached) isotonic mapping from raw confidence → win prob.

    Returns a calibrator dict:
        {
            "anchors": [(x, y), ...],   # piecewise-linear monotone map
            "n_samples": int,
            "fitted_at": iso timestamp,
            "is_identity": bool,        # True if too few samples; passthrough
        }
    """
    # Try cache
    now = time.time()
    if not force and _CALIB_CACHE_PATH.exists():
        try:
            with open(_CALIB_CACHE_PATH) as f:
                cached = json.load(f)
            if (now - cached.get("fitted_at_epoch", 0)) < CACHE_TTL_SECONDS:
                # Tuple roundtrip — JSON converts to list
                cached["anchors"] = [tuple(a) for a in cached.get("anchors", [])]
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    trades = _load_resolved_trades()
    n = len(trades)
    if n < MIN_SAMPLES:
        cal = {
            "anchors": [],
            "n_samples": n,
            "fitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "fitted_at_epoch": now,
            "is_identity": True,
            "reason": f"insufficient_samples (have {n}, need {MIN_SAMPLES})",
        }
        return cal

    # Sort by confidence; bin into N_BUCKETS for stability.
    sorted_trades = sorted(trades, key=lambda r: float(r.get("confidence") or 0))
    # Bin into N_BUCKETS equal-count groups → bucket center is x, win rate is y
    bucket_size = max(1, n // N_BUCKETS)
    xs: list[float] = []
    ys: list[float] = []
    i = 0
    while i < n:
        end = min(i + bucket_size, n)
        chunk = sorted_trades[i:end]
        x_mean = sum(float(r.get("confidence") or 0) for r in chunk) / len(chunk)
        # _load_resolved_trades now yields only terminal won/lost, so a win is
        # exactly status=="won" (won_early/cut_loss are excluded upstream).
        wins = sum(1 for r in chunk if r.get("status") == "won")
        y_rate = wins / len(chunk)
        xs.append(x_mean)
        ys.append(y_rate)
        i = end

    anchors = _isotonic_pav(xs, ys)

    cal = {
        "anchors": anchors,
        "n_samples": n,
        "fitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "fitted_at_epoch": now,
        "is_identity": False,
        "buckets_raw": list(zip(xs, ys)),
    }
    # Persist
    try:
        _CALIB_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # JSON can't store tuples — convert to lists for storage
        out = {**cal, "anchors": [list(a) for a in anchors],
               "buckets_raw": [list(b) for b in cal["buckets_raw"]]}
        tmp = _CALIB_CACHE_PATH.with_suffix(_CALIB_CACHE_PATH.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(out, f)
        tmp.replace(_CALIB_CACHE_PATH)
    except OSError:
        pass

    log_event("kalshi_calibration", "fit_complete", {
        "n_samples": n, "n_anchors": len(anchors),
    })
    return cal


def calibrate(raw_conf: float, calibrator: Optional[dict] = None) -> float:
    """Map raw confidence to calibrated probability via piecewise-linear
    interpolation over the fitted anchors.

    Returns:
        Calibrated probability in [0, 1]. Returns raw_conf unchanged
        when the calibrator hasn't been fit yet (identity mode).
    """
    if raw_conf is None:
        return 0.5
    raw_conf = max(0.0, min(1.0, float(raw_conf)))

    if calibrator is None:
        calibrator = fit_calibrator()
    if calibrator.get("is_identity") or not calibrator.get("anchors"):
        return raw_conf

    anchors = calibrator["anchors"]
    # Anchors is sorted by x by construction.
    # Find the bracketing pair (x_lo, y_lo), (x_hi, y_hi).
    if raw_conf <= anchors[0][0]:
        return float(anchors[0][1])
    if raw_conf >= anchors[-1][0]:
        return float(anchors[-1][1])
    for i in range(len(anchors) - 1):
        x_lo, y_lo = anchors[i]
        x_hi, y_hi = anchors[i + 1]
        if x_lo <= raw_conf <= x_hi:
            if x_hi == x_lo:
                return float(y_lo)
            t = (raw_conf - x_lo) / (x_hi - x_lo)
            return float(y_lo + t * (y_hi - y_lo))
    # Shouldn't reach here
    return raw_conf


def calibration_report() -> str:
    """Human-readable summary for the dashboard / CLI."""
    cal = fit_calibrator()
    if cal.get("is_identity"):
        return (
            f"calibration: IDENTITY (n={cal['n_samples']} resolved; "
            f"need {MIN_SAMPLES}+)"
        )
    lines = [
        f"calibration: ACTIVE  n={cal['n_samples']}  anchors={len(cal['anchors'])}",
        f"fitted_at: {cal['fitted_at']}",
        "raw → calibrated map (anchors):",
    ]
    for x, y in cal["anchors"]:
        lines.append(f"  {x:.3f} → {y:.3f}  (delta {y - x:+.3f})")
    return "\n".join(lines)
