"""
Evidence-gated bet sizing for prediction markets (ported 2026-06-15).

Polybot's history is a cautionary tale: it self-tuned on fabricated
metrics until that was caught. The sizing path, though, still bets
half-Kelly off a single-trade `p_win` with NO consultation of the
realized track record — so a sleeve that has never resolved a profitable
trade can still size to its full cap. This gate fixes that.

It maps the sleeve's realized resolutions to a maximum Kelly multiplier:

    no_measured_edge  → 0.25   n<5, non-positive Sharpe, or PSR < 0.50
    provisional       → 0.40   PSR 0.50–0.95
    evidence_backed   → base   PSR ≥ 0.95 (no extra shrink)

Monotone risk-reducing: returns min(configured multiplier, tier cap) —
it can only shrink a bet, never grow one. As real resolutions accrue and
PSR climbs, the cap self-releases; if performance decays, it self-tightens.

Binary-market adaptation of traderbot's lib/psr_gate: the per-trade
"return" for a prediction market is per-resolution net_profit divided by
capital deployed (entry_price × quantity). PSR/MinTRL come from the
shared lib/hermes_significance module (which already holds the Wilson
interval), feeding that return series.

SHIPS SHADOW: enable via config (kalshi.psr_gate_enabled, default false).
While disabled the caller still logs what the gate WOULD cap to, so the
enable decision rests on visible evidence. polybot trades Kalshi LIVE —
only the operator flips this on.
"""
from __future__ import annotations

import json
from pathlib import Path

from lib.hermes_significance import (
    probabilistic_sharpe_ratio, min_track_record_length,
)

TRADE_HISTORY = Path(__file__).resolve().parent.parent / "data" / "trade_history.json"

TIER_CAPS = {
    "no_measured_edge": 0.25,
    "provisional": 0.40,
    "evidence_backed": None,   # None = no extra shrink beyond the base multiplier
}


def _platform_returns(platform: str | None = None,
                      path: Path = TRADE_HISTORY) -> list[float]:
    """Per-resolution return series = net_profit / capital_deployed.

    platform=None pools all resolved trades; pass 'kalshi' to gate the
    Kalshi sleeve on its OWN record only (the honest default — a sleeve
    earns its size with its own resolutions).
    """
    try:
        trades = json.loads(Path(path).read_text())
    except Exception:
        return []
    out: list[float] = []
    for t in trades:
        if platform is not None and t.get("platform") != platform:
            continue
        if t.get("outcome") is None or not isinstance(t.get("net_profit"), (int, float)):
            continue
        cap = float(t.get("entry_price") or 0) * float(t.get("quantity") or 0)
        if cap > 0:
            out.append(float(t["net_profit"]) / cap)
    return out


def gated_kelly_multiplier(base_multiplier: float,
                           platform: str | None = "kalshi") -> tuple[float, dict]:
    """Return (gated_multiplier, meta). See module docstring for tiers.

    Pure measurement — never raises, never does I/O beyond reading the
    trade-history file. Caller decides whether to apply (live) or just
    log (shadow).
    """
    base = float(base_multiplier or 0.5)
    rets = _platform_returns(platform)
    n = len(rets)
    psr = probabilistic_sharpe_ratio(rets)

    if psr is None:                      # n<5 or zero-variance
        tier = "no_measured_edge"
    elif psr < 0.50:
        tier = "no_measured_edge"
    elif psr < 0.95:
        tier = "provisional"
    else:
        tier = "evidence_backed"

    cap = TIER_CAPS[tier]
    gated = base if cap is None else min(base, cap)
    mintrl = min_track_record_length(rets)
    return gated, {
        "gate": tier,
        "platform": platform or "all",
        "base_multiplier": round(base, 4),
        "gated_multiplier": round(gated, 4),
        "psr": (round(psr, 4) if psr is not None else None),
        "n_resolved": n,
        "min_trades_needed": (None if mintrl is None or mintrl == float("inf")
                              else int(mintrl)),
    }
