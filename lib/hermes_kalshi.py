"""Hermes for Polybot Kalshi — scientific-method auto-tuner for the
15-min crypto paper-trading pipeline.

Mirrors the traderbot Hermes scientific loop but with a Kalshi-specific
parameter space, scoring, and apply layer.

The 5 tunable knobs in this first cut:
  - btc.min_confidence            (per-asset, in kalshi_assets.yaml)
  - CONTRARIAN_FLIP_THRESHOLD     (module-level constant)
  - DEFAULT_MIN_SECONDS_TO_CLOSE  (module-level constant)
  - MAX_FILL_FOR_BUY              (module-level constant)
  - DEFAULT_MAX_TRADE_USD         (module-level constant)

YAML-backed values (min_confidence) get edited in-place. Module-level
constants are tuned via a small kalshi_strategy.yaml overrides layer
this module owns; kalshi_15min_paper.py reads them at import time so
the next signal cycle picks up the new value.

Scoring uses the paper-trade log to compute:
  - rolling 7d WR / ROI / drawdown
  - trades-per-day throughput
  - direction-accuracy on closed markets

Run via:
    python main.py kalshi-hermes-cycle           # review mode (default)
    python main.py kalshi-hermes-cycle --live    # apply one change
    python main.py kalshi-hermes-ledger          # see experiment history
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import yaml

ROOT = Path(__file__).resolve().parent.parent
KALSHI_ASSETS_PATH = ROOT / "config" / "kalshi_assets.yaml"
KALSHI_STRATEGY_PATH = ROOT / "config" / "kalshi_strategy.yaml"  # NEW
PAPER_PATH = ROOT / "data" / "kalshi_15min_paper.jsonl"
LEDGER_PATH = ROOT / "data" / "hermes_kalshi_experiments.jsonl"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"


# ─── Parameter space ───────────────────────────────────────────────────

# (name, default_if_missing, low_bound, high_bound, step)
BTC_MIN_CONF_BOUNDS = (0.20, 0.50, 0.05)
PARAM_SPACE = {
    "btc_min_confidence":          (0.30, *BTC_MIN_CONF_BOUNDS),
    "contrarian_flip_threshold":   (1.0, 0.5, 3.0, 0.5),
    "default_min_seconds_to_close": (180.0, 120.0, 300.0, 30.0),
    "max_fill_for_buy":            (0.45, 0.35, 0.55, 0.05),
    "default_max_trade_usd":       (5.0, 5.0, 25.0, 5.0),
}


# ─── Current-value reads ──────────────────────────────────────────────

def _load_assets_yaml() -> dict:
    if not KALSHI_ASSETS_PATH.exists():
        return {}
    with open(KALSHI_ASSETS_PATH) as f:
        return yaml.safe_load(f) or {}


def _save_assets_yaml(data: dict) -> None:
    with open(KALSHI_ASSETS_PATH, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


def _load_strategy_yaml() -> dict:
    if not KALSHI_STRATEGY_PATH.exists():
        return {}
    try:
        with open(KALSHI_STRATEGY_PATH) as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _save_strategy_yaml(data: dict) -> None:
    KALSHI_STRATEGY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(KALSHI_STRATEGY_PATH, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)


def get_current_params() -> dict:
    """Read the current value of every tunable param. Falls back to
    PARAM_SPACE defaults if neither YAML has the value yet.
    """
    assets = _load_assets_yaml()
    strategy = _load_strategy_yaml()
    out = {}
    out["btc_min_confidence"] = float(
        (assets.get("assets", {}).get("btc", {}) or {})
        .get("min_confidence", PARAM_SPACE["btc_min_confidence"][0])
    )
    for k in (
        "contrarian_flip_threshold",
        "default_min_seconds_to_close",
        "max_fill_for_buy",
        "default_max_trade_usd",
    ):
        out[k] = float(strategy.get(k, PARAM_SPACE[k][0]))
    return out


def set_param(name: str, value) -> None:
    """Persist a new param value to the right YAML."""
    if name not in PARAM_SPACE:
        raise ValueError(f"unknown param: {name}")
    if name == "btc_min_confidence":
        assets = _load_assets_yaml()
        btc = assets.setdefault("assets", {}).setdefault("btc", {})
        btc["min_confidence"] = float(value)
        _save_assets_yaml(assets)
        return
    # Module-level constants → kalshi_strategy.yaml
    strategy = _load_strategy_yaml()
    strategy[name] = float(value)
    _save_strategy_yaml(strategy)


def _clamp(name: str, value: float) -> float:
    _, lo, hi, _step = PARAM_SPACE[name]
    if name == "default_min_seconds_to_close":
        return float(int(max(lo, min(hi, value))))
    if name == "default_max_trade_usd":
        return float(int(max(lo, min(hi, value))))
    return round(max(lo, min(hi, value)), 4)


# ─── Scoring (Kalshi-paper goal metrics) ──────────────────────────────

def _load_paper_trades() -> list[dict]:
    if not PAPER_PATH.exists():
        return []
    out = []
    with open(PAPER_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def compute_kalshi_goal_metrics(window_days: int = 7) -> dict:
    """Scoring snapshot for one Hermes cycle on the Kalshi pipeline.

    Returns a JSON-safe dict slotted directly into the experiment ledger
    as baseline / post snapshots. goal_distance_pct is defined relative
    to the polybot Kalshi target: positive ROI over the window.
    """
    trades = _load_paper_trades()
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    window = []
    for t in trades:
        ts = t.get("resolved_at") or t.get("opened_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt >= cutoff:
            window.append(t)
    closed = [
        t for t in window
        if t.get("status") in ("won", "won_early", "lost", "cut_loss")
    ]
    wins = sum(1 for t in closed if t.get("status") in ("won", "won_early"))
    losses = sum(1 for t in closed if t.get("status") in ("lost", "cut_loss"))
    n = len(closed)
    pnl = sum(float(t.get("paper_pnl") or 0.0) for t in closed)
    deployed = sum(float(t.get("notional") or 0.0) for t in closed)
    wr = (wins / n) if n > 0 else None
    roi = (pnl / deployed) if deployed > 0 else 0.0

    # Drawdown over cumulative paper-PnL across ALL trades since start
    all_closed = sorted(
        [t for t in trades
         if t.get("status") in ("won", "won_early", "lost", "cut_loss")],
        key=lambda r: r.get("opened_at", ""),
    )
    cum = 0.0
    peak = 0.0
    for t in all_closed:
        cum += float(t.get("paper_pnl") or 0.0)
        if cum > peak:
            peak = cum
    drawdown_from_peak = max(0.0, peak - cum)

    # Direction accuracy (independent of stop firing)
    # Note: this needs API lookup for true resolution — we use the
    # paper status as a proxy (won/lost reflect both signal accuracy AND
    # stop timing). Future work: query Kalshi result field for purer
    # directional accuracy.
    days = max(window_days, 1)
    return {
        "window_days": window_days,
        "n_trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wr, 4) if wr is not None else None,
        "rolling_pnl": round(pnl, 4),
        "rolling_30d_pnl": round(pnl, 4),  # ledger compatibility alias
        "deployed": round(deployed, 4),
        "roi": round(roi, 4),
        "trades_per_day": round(n / days, 2),
        "cumulative_pnl_lifetime": round(cum, 4),
        "peak_pnl_lifetime": round(peak, 4),
        "drawdown_from_peak": round(drawdown_from_peak, 4),
        # Lower is better (closer to "ROI ≥ target"); positive ROI → 0,
        # neutral → 1.0, negative → > 1.0
        "goal_distance_pct": round(max(0.0, 0.05 - roi) / 0.05, 4)
            if roi < 0.05 else 0.0,
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── Diagnosis (Kalshi-specific) ──────────────────────────────────────

def diagnose(metrics: dict) -> list[dict]:
    """Inspect the kalshi metrics and propose param changes.

    One-rule-fires-per-condition design — pick_one_change in the
    scientific wrapper enforces the actual one-variable-at-a-time
    discipline across the bundle returned here.
    """
    recs: list[dict] = []
    n = metrics.get("n_trades", 0)
    wr = metrics.get("win_rate")
    roi = metrics.get("roi", 0)
    tpd = metrics.get("trades_per_day", 0)
    dd = metrics.get("drawdown_from_peak", 0)

    if n < 5:
        return [{
            "param": "none", "direction": "hold", "confidence": 0.0,
            "reason": f"insufficient trades ({n} < 5)",
        }]

    # Low WR → tighten the entry filter (more selective)
    if wr is not None and wr < 0.45:
        recs.append({
            "param": "btc_min_confidence",
            "direction": "increase",
            "confidence": min(1.0, (0.45 - wr) * 4),
            "reason": f"WR {wr:.0%} low — raise BTC confidence floor",
        })
    elif wr is not None and wr > 0.70 and tpd < 5:
        # High WR + low volume → can be less selective
        recs.append({
            "param": "btc_min_confidence",
            "direction": "decrease",
            "confidence": min(1.0, (wr - 0.70) * 4),
            "reason": f"WR {wr:.0%} on {tpd:.1f} trades/day — loosen filter",
        })

    # Negative ROI → tighten the R:R gate
    if roi < -0.05:
        recs.append({
            "param": "max_fill_for_buy",
            "direction": "decrease",
            "confidence": min(1.0, abs(roi) * 4),
            "reason": f"ROI {roi:+.1%} negative — demand better R:R per fill",
        })

    # Big drawdown → cut max trade size (preserve paper bankroll)
    if dd > 50.0 and n >= 10:
        recs.append({
            "param": "default_max_trade_usd",
            "direction": "decrease",
            "confidence": min(1.0, dd / 100.0),
            "reason": f"drawdown ${dd:.0f} from peak — cap per-trade exposure",
        })

    # Throughput too low + positive ROI → loosen seconds-to-close
    if tpd < 1.0 and roi > 0 and n >= 10:
        recs.append({
            "param": "default_min_seconds_to_close",
            "direction": "decrease",
            "confidence": 0.5,
            "reason": (
                f"throughput {tpd:.1f}/day but ROI {roi:+.1%} — "
                "widen entry window"
            ),
        })

    # No clear signal
    if not recs:
        recs.append({
            "param": "none", "direction": "hold", "confidence": 0.0,
            "reason": f"WR {wr}, ROI {roi:+.1%}, drawdown ${dd:.0f} — within tolerance",
        })
    return recs


# ─── Scientific cycle ─────────────────────────────────────────────────

Mode = Literal["review", "live"]


def get_mode() -> Mode:
    try:
        with open(SETTINGS_PATH) as f:
            s = yaml.safe_load(f) or {}
        m = str(s.get("kalshi_hermes_mode", "review")).lower().strip()
        return "live" if m == "live" else "review"
    except OSError:
        return "review"


def set_mode(mode: Mode) -> None:
    mode = "live" if mode == "live" else "review"
    with open(SETTINGS_PATH) as f:
        s = yaml.safe_load(f) or {}
    s["kalshi_hermes_mode"] = mode
    with open(SETTINGS_PATH, "w") as f:
        yaml.safe_dump(s, f, default_flow_style=False, sort_keys=False)


def pick_one_change(recs: list[dict]) -> dict | None:
    """Pick the single highest-confidence non-recently-failed rec."""
    from tradingcore.hermes_ledger import recently_rolled_back

    eligible = []
    for r in recs:
        if r.get("param") in (None, "none"):
            continue
        if r.get("direction") == "hold":
            continue
        if recently_rolled_back(r["param"], ledger_path=LEDGER_PATH):
            continue
        eligible.append(r)
    if not eligible:
        return None
    eligible.sort(
        key=lambda r: (-(r.get("confidence", 0.0) or 0.0), r.get("param", "")),
    )
    return eligible[0]


def close_prior_experiments(
    min_age_hours: float = 24.0, keep_threshold_delta: float = 0.001,
) -> list[dict]:
    """Evaluate any still-open kalshi experiments older than min_age_hours
    against the current goal metrics. Roll back if regressed.
    """
    from tradingcore.hermes_ledger import (
        list_open_experiments, close_experiment,
    )

    now = datetime.now(timezone.utc)
    open_exps = list_open_experiments(ledger_path=LEDGER_PATH)
    current = compute_kalshi_goal_metrics()
    closed = []
    for exp in open_exps:
        try:
            opened = datetime.fromisoformat(
                exp["opened_at"].replace("Z", "+00:00")
            )
        except (KeyError, ValueError):
            continue
        if (now - opened).total_seconds() / 3600.0 < min_age_hours:
            continue
        result = close_experiment(
            exp["experiment_id"],
            post_metrics=current,
            keep_threshold_delta=keep_threshold_delta,
            ledger_path=LEDGER_PATH,
        )
        if result and result["status"] == "rolled_back":
            # Revert
            set_param(result["param"], result["old_value"])
        if result:
            closed.append(result)
    return closed


def run_cycle(force_mode: Mode | None = None) -> dict:
    """End-to-end Kalshi Hermes pass:
      1. Close prior experiments
      2. Score current state
      3. Diagnose
      4. Pick ONE change
      5. If mode=live: apply + open experiment
    """
    from tradingcore.hermes_ledger import open_experiment

    mode = force_mode or get_mode()
    closed = close_prior_experiments()
    metrics = compute_kalshi_goal_metrics()
    recs = diagnose(metrics)
    pick = pick_one_change(recs)

    applied = None
    exp = None
    if mode == "live" and pick is not None:
        params = get_current_params()
        old = params.get(pick["param"])
        if old is None:
            return {
                "mode": mode, "metrics": metrics, "recs": recs, "pick": pick,
                "applied": None, "experiment": None,
                "closed_experiments": closed,
                "error": f"unknown param {pick['param']}",
            }
        _, lo, hi, step = PARAM_SPACE[pick["param"]]
        new = old + step if pick["direction"] == "increase" else old - step
        new = _clamp(pick["param"], new)
        if new != old:
            set_param(pick["param"], new)
            applied = {"param": pick["param"], "old": old, "new": new,
                       "reason": pick["reason"]}
            exp = open_experiment(
                param=pick["param"],
                old_value=old, new_value=new,
                reason=pick["reason"],
                baseline_metrics=metrics,
                expected_direction="up",
                ledger_path=LEDGER_PATH,
            )

    return {
        "mode": mode,
        "cycle_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "recs": recs,
        "pick": pick,
        "applied": applied,
        "experiment": exp,
        "closed_experiments": closed,
    }


def render_cycle(report: dict) -> str:
    m = report.get("metrics", {})
    pick = report.get("pick")
    applied = report.get("applied")
    closed = report.get("closed_experiments", []) or []
    lines = []
    lines.append("=" * 70)
    lines.append(f"KALSHI HERMES CYCLE  —  mode={report.get('mode')}")
    lines.append("=" * 70)
    wr = m.get("win_rate")
    wr_s = f"{wr*100:.1f}%" if wr is not None else "n/a"
    lines.append(
        f"7d window: n={m.get('n_trades')}  WR={wr_s}  "
        f"ROI={m.get('roi', 0)*100:+.1f}%  "
        f"PnL=${m.get('rolling_pnl', 0):+.2f}  "
        f"throughput={m.get('trades_per_day', 0):.1f}/day"
    )
    lines.append(
        f"Lifetime: cum_pnl=${m.get('cumulative_pnl_lifetime', 0):+.2f}  "
        f"peak=${m.get('peak_pnl_lifetime', 0):+.2f}  "
        f"drawdown=${m.get('drawdown_from_peak', 0):+.2f}"
    )
    if closed:
        lines.append("")
        lines.append(f"Closed {len(closed)} experiment(s):")
        for e in closed:
            mark = "✓" if e["status"] == "kept" else "✗"
            lines.append(
                f"  {mark} {e['param']}: {e['old_value']} → {e['new_value']}  "
                f"({e.get('verdict')})"
            )
    lines.append("")
    if applied:
        lines.append(
            f"APPLIED: {applied['param']}  "
            f"{applied['old']} → {applied['new']}"
        )
        lines.append(f"  reason: {applied['reason']}")
    elif pick:
        lines.append(
            f"PICKED (review mode, no change applied): "
            f"{pick['param']} → {pick['direction']}"
        )
        lines.append(f"  reason: {pick['reason']}")
    else:
        lines.append("No change picked this cycle.")
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "PARAM_SPACE", "LEDGER_PATH",
    "get_current_params", "set_param",
    "compute_kalshi_goal_metrics", "diagnose",
    "get_mode", "set_mode",
    "pick_one_change", "close_prior_experiments",
    "run_cycle", "render_cycle",
]


if __name__ == "__main__":
    print(render_cycle(run_cycle()))
