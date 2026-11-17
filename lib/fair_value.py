"""fair_value — price a Kalshi 15-min strike from the spot history the collector already
logs, so the paper trader can choose the BEST strike in a window instead of all of them.

Why this is not curve-fitting: nothing here is fitted to observed outcomes. It is the same
zero-drift, fat-tailed distribution model already in lib/binary_justify.py (G1), fed by
realized volatility measured from spot. It answers "what is this contract worth?" — a
measurement — and never "what has been winning lately?".

Why it matters: a 15-min window lists many strikes, and every one of them resolves on the
SAME price move. Buying all of them is one bet at 8x size, not eight bets. Given a fair
value we can take the single most mispriced strike and skip the redundant rest — strictly
better risk per unit of evidence, with no change to the underlying edge hypothesis.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_TAIL_LINES = 4000        # plenty for a 300-bar vol estimate; keeps reads cheap
_MIN_RETURNS = 30


def _tail(path: Path, n: int) -> list[str]:
    """Last n lines without loading a large file into memory."""
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block, data, lines = 8192, b"", 0
            pos = size
            while pos > 0 and lines <= n:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step)
                data = chunk + data
                lines = data.count(b"\n")
            return data.decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def spot_series(log_path: Path, series: str, limit: int = 300) -> list[float]:
    """Chronological spot prices for one series, from the collector's own observations.
    Deduped per timestamp — many strikes share a cycle and repeat the same spot."""
    seen: dict[str, float] = {}
    for line in _tail(log_path, _TAIL_LINES):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("t") != "obs" or d.get("series") != series:
            continue
        sp, ts = d.get("spot"), d.get("ts")
        if sp is None or not ts:
            continue
        try:
            seen[ts] = float(sp)
        except (TypeError, ValueError):
            continue
    out = [seen[k] for k in sorted(seen)]
    return out[-limit:]


def sigma_from_spots(spots: list[float]) -> float | None:
    """EWMA volatility per bar from a spot series. None when too few samples."""
    if len(spots) < _MIN_RETURNS + 1:
        return None
    rets = []
    for a, b in zip(spots, spots[1:]):
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < _MIN_RETURNS:
        return None
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lib.binary_justify import ewma_sigma
    s = ewma_sigma(rets)
    return s if s > 0 else None


def fair_p_yes(spot: float, strike: float, sigma_bar: float, minutes_left: float,
               bar_minutes: float = 1.0) -> float:
    """P(settle >= strike) — the YES side's fair value under the G1 model."""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from lib.binary_justify import fair_p_above
    bars = max(0.0, minutes_left / max(bar_minutes, 1e-9))
    return fair_p_above(spot, strike, sigma_bar, bars, nu=4.0)


def edge_for(side: str, ask: float, spot: float, strike: float, sigma_bar: float,
             minutes_left: float, bar_minutes: float = 1.0) -> float | None:
    """Raw edge (fair - price) for buying `side` at `ask`. None if unpriceable.
    Positive = the contract looks cheap versus the volatility model."""
    if sigma_bar is None or sigma_bar <= 0 or spot is None or strike is None:
        return None
    try:
        p_yes = fair_p_yes(float(spot), float(strike), float(sigma_bar),
                           float(minutes_left), bar_minutes)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    p_side = p_yes if side == "yes" else (1.0 - p_yes)
    return p_side - float(ask)
