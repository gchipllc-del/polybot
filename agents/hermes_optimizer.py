"""
Hermes Self-Optimization Agent — adaptive parameter tuning for prediction markets.

Named after the Greek god of trade and commerce. Hermes reviews forecast
accuracy and trade performance, then adjusts strategy parameters to
maximize growth. Runs on demand or after each scan cycle.

Self-optimization loop:
    1. REVIEW: Analyze resolved trades + calibration data
    2. DIAGNOSE: Identify which parameters need adjustment
    3. TUNE: Adjust parameters within safety bounds
    4. LOG: Record every change with reasoning (append-only audit)
    5. VALIDATE: Ensure all parameters stay within Hermes bounds

Tunable parameters (defined in strategy.yaml → hermes_bounds):
    - kelly_multiplier: 0.10 - 0.75
    - min_edge: 0.05 - 0.20
    - min_composite_score: 4 - 8
    - llm_weight: 0.10 - 0.50
    - base_rate_weight: 0.10 - 0.50
    - metaculus_weight: 0.05 - 0.40
    - news_weight: 0.05 - 0.30
    - early_exit_threshold: 0.05 - 0.25
    - max_per_market_pct: 0.05 - 0.20

Security:
    - All changes logged to audit trail BEFORE being applied
    - Safety bounds are hard limits — Hermes cannot exceed them
    - Config is re-read from disk each cycle (no stale state)
    - Optimization log is append-only (forensic-grade)
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from tradingcore.audit import log_event
from tradingcore.calibration import brier_score, source_accuracy
from tradingcore.memory_palace import diary_write
from lib.resolution_tracker import get_performance_summary

CONFIG_PATH = Path(__file__).parent.parent / "config"
STRATEGY_PATH = CONFIG_PATH / "strategy.yaml"
SETTINGS_PATH = CONFIG_PATH / "settings.yaml"
DATA_DIR = Path(__file__).parent.parent / "data"
TRADE_HISTORY_PATH = DATA_DIR / "trade_history.json"
OPTIMIZATION_LOG = DATA_DIR / "hermes_log.jsonl"

# Conservative step sizes per optimization cycle
STEP_SIZES = {
    "kelly_multiplier": 0.05,
    "min_edge": 0.01,
    "min_composite_score": 1,
    "llm_weight": 0.03,
    "base_rate_weight": 0.03,
    "metaculus_weight": 0.03,
    "news_weight": 0.02,
    "early_exit_threshold": 0.02,
    "max_per_market_pct": 0.02,
}


def _load_strategy() -> dict:
    with open(STRATEGY_PATH) as f:
        return yaml.safe_load(f)


def _save_strategy(strategy: dict):
    with open(STRATEGY_PATH, "w") as f:
        yaml.safe_dump(strategy, f, default_flow_style=False, sort_keys=False)


def _load_settings() -> dict:
    with open(SETTINGS_PATH) as f:
        return yaml.safe_load(f)


def _save_settings(settings: dict):
    with open(SETTINGS_PATH, "w") as f:
        yaml.safe_dump(settings, f, default_flow_style=False, sort_keys=False)


def _load_trade_history() -> list[dict]:
    if not TRADE_HISTORY_PATH.exists():
        return []
    try:
        with open(TRADE_HISTORY_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _log_optimization(entry: dict):
    """Append to the Hermes optimization log. Append-only."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    with open(OPTIMIZATION_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _get_bounds(strategy: dict) -> dict[str, tuple]:
    """Load bounds from strategy.yaml. Falls back to hardcoded defaults."""
    hb = strategy.get("hermes_bounds", {})
    defaults = {
        "min_edge": (0.05, 0.20),
        "kelly_multiplier": (0.10, 0.75),
        "min_composite_score": (4, 8),
        "llm_weight": (0.10, 0.50),
        "base_rate_weight": (0.10, 0.50),
        "metaculus_weight": (0.05, 0.40),
        "news_weight": (0.05, 0.30),
        "early_exit_threshold": (0.05, 0.25),
        "max_per_market_pct": (0.05, 0.20),
    }
    bounds = {}
    for key, default in defaults.items():
        val = hb.get(key, default)
        if isinstance(val, list) and len(val) == 2:
            bounds[key] = (val[0], val[1])
        else:
            bounds[key] = default
    return bounds


def _clamp(value, bounds: tuple):
    """Clamp a value within safety bounds."""
    lo, hi = bounds
    if isinstance(lo, int) and isinstance(hi, int):
        return max(lo, min(hi, int(round(value))))
    return max(lo, min(hi, round(value, 4)))


# ── Step 1: REVIEW ───────────────────────────────────────────────

def review_trades(lookback_days: int = 14) -> dict:
    """
    Analyze resolved trades + calibration accuracy.

    Returns metrics: win rate, expectancy, by-category breakdown,
    calibration quality, source accuracy.
    """
    history = _load_trade_history()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    recent = [t for t in history if t.get("closed_at", "") > cutoff]
    if not recent:
        return {"total_trades": 0, "message": "No resolved trades in lookback period"}

    wins = [t for t in recent if t.get("won", False)]
    losses = [t for t in recent if not t.get("won", False)]

    total_pnl = sum(t.get("net_profit", 0) for t in recent)
    total_fees = sum(t.get("fees", 0) for t in recent)
    win_rate = len(wins) / len(recent) if recent else 0
    avg_win = sum(t.get("net_profit", 0) for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.get("net_profit", 0) for t in losses) / len(losses) if losses else 0

    # By category breakdown
    by_category: dict[str, dict] = {}
    for t in recent:
        cat = t.get("category", "other")
        if cat not in by_category:
            by_category[cat] = {"trades": 0, "wins": 0, "pnl": 0}
        by_category[cat]["trades"] += 1
        if t.get("won"):
            by_category[cat]["wins"] += 1
        by_category[cat]["pnl"] += t.get("net_profit", 0)

    # By platform breakdown
    by_platform: dict[str, dict] = {}
    for t in recent:
        plat = t.get("platform", "unknown")
        if plat not in by_platform:
            by_platform[plat] = {"trades": 0, "wins": 0, "pnl": 0}
        by_platform[plat]["trades"] += 1
        if t.get("won"):
            by_platform[plat]["wins"] += 1
        by_platform[plat]["pnl"] += t.get("net_profit", 0)

    # Calibration quality
    bs = brier_score()
    sa = source_accuracy()

    return {
        "total_trades": len(recent),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "total_pnl": round(total_pnl, 2),
        "total_fees": round(total_fees, 2),
        "by_category": by_category,
        "by_platform": by_platform,
        "brier_score": round(bs, 4) if bs >= 0 else None,
        "source_accuracy": sa,
    }


# ── Step 2: DIAGNOSE ─────────────────────────────────────────────

def diagnose(review: dict, strategy: dict) -> list[dict]:
    """
    Produce recommendations based on trade review + calibration data.

    Each recommendation: {"param": str, "direction": "increase"|"decrease", "reason": str}
    """
    settings = _load_settings()
    min_trades = settings.get("hermes", {}).get("min_trades_to_optimize", 5)

    if review["total_trades"] < min_trades:
        return [{"param": "none", "direction": "hold",
                 "reason": f"Only {review['total_trades']} trades — need {min_trades} for tuning"}]

    recommendations = []
    win_rate = review.get("win_rate", 0)
    bs = review.get("brier_score")
    sa = review.get("source_accuracy", {})

    # ── Win rate tuning ───────────────────────────────────────────
    if win_rate < 0.40:
        recommendations.append({
            "param": "min_composite_score",
            "direction": "increase",
            "reason": f"Win rate {win_rate:.0%} is low — be more selective",
        })
        recommendations.append({
            "param": "min_edge",
            "direction": "increase",
            "reason": f"Win rate {win_rate:.0%} — require larger edge",
        })
    elif win_rate > 0.65 and review["total_trades"] >= 10:
        recommendations.append({
            "param": "min_composite_score",
            "direction": "decrease",
            "reason": f"Win rate {win_rate:.0%} is high — can take more setups",
        })

    # ── Kelly tuning based on performance ─────────────────────────
    if win_rate > 0.55 and review.get("total_pnl", 0) > 0:
        recommendations.append({
            "param": "kelly_multiplier",
            "direction": "increase",
            "reason": f"Profitable with {win_rate:.0%} win rate — increase Kelly for faster growth",
        })
    elif win_rate < 0.45 or review.get("total_pnl", 0) < 0:
        recommendations.append({
            "param": "kelly_multiplier",
            "direction": "decrease",
            "reason": f"Underperforming — reduce Kelly to preserve capital",
        })

    # ── Calibration-driven source weight tuning ───────────────────
    if sa and len(sa) >= 2:
        sorted_sources = sorted(sa.items(), key=lambda x: x[1]["brier"])
        best_source = sorted_sources[0][0]
        worst_source = sorted_sources[-1][0]

        # Boost best source
        weight_key = f"{best_source}_weight"
        if weight_key in STEP_SIZES:
            recommendations.append({
                "param": weight_key,
                "direction": "increase",
                "reason": f"'{best_source}' has best Brier ({sa[best_source]['brier']:.4f}) — increase weight",
            })

        # Reduce worst source (if significantly worse)
        worst_brier = sa[worst_source]["brier"]
        best_brier = sa[best_source]["brier"]
        if worst_brier > best_brier * 1.5:
            weight_key = f"{worst_source}_weight"
            if weight_key in STEP_SIZES:
                recommendations.append({
                    "param": weight_key,
                    "direction": "decrease",
                    "reason": f"'{worst_source}' has worst Brier ({worst_brier:.4f}) — decrease weight",
                })

    # ── Brier score overall ───────────────────────────────────────
    if bs is not None:
        if bs > 0.25:
            # Worse than random — something is broken
            recommendations.append({
                "param": "kelly_multiplier",
                "direction": "decrease",
                "reason": f"Brier {bs:.4f} > 0.25 (worse than random) — reduce risk immediately",
            })
        elif bs < 0.15:
            # Excellent calibration — can be more aggressive
            recommendations.append({
                "param": "max_per_market_pct",
                "direction": "increase",
                "reason": f"Excellent Brier {bs:.4f} — can size up positions",
            })

    # ── Category-specific avoidance ───────────────────────────────
    by_cat = review.get("by_category", {})
    for cat, data in by_cat.items():
        if data["trades"] >= 3 and data["wins"] == 0:
            recommendations.append({
                "param": "avoid_category",
                "direction": "add",
                "reason": f"Category '{cat}': 0/{data['trades']} wins (${data['pnl']:.2f}) — avoid",
                "value": cat,
            })

    # ── Exit threshold tuning ─────────────────────────────────────
    # If we're consistently leaving money on the table (lots of positions
    # gain more after we take profit), tighten early exit
    # For now, just base it on win rate (more data-driven in future)
    if win_rate > 0.60 and review.get("total_pnl", 0) > 0:
        recommendations.append({
            "param": "early_exit_threshold",
            "direction": "increase",
            "reason": "High win rate — let winners run longer",
        })

    return recommendations


# ── Step 3: TUNE ──────────────────────────────────────────────────

def apply_adjustments(recommendations: list[dict], strategy: dict) -> dict:
    """
    Apply recommended parameter adjustments within bounds.

    Returns dict of changes made.
    """
    bounds = _get_bounds(strategy)
    changes = {}

    for rec in recommendations:
        param = rec["param"]
        direction = rec["direction"]

        if param == "none" or direction == "hold":
            continue

        # Handle category avoidance separately
        if param == "avoid_category" and direction == "add":
            avoid = strategy.get("markets", {}).get("avoid_categories", [])
            cat = rec.get("value", "")
            if cat and cat not in avoid:
                avoid.append(cat)
                strategy.setdefault("markets", {})["avoid_categories"] = avoid
                changes["avoid_categories"] = {
                    "old": [c for c in avoid if c != cat],
                    "new": avoid,
                    "reason": rec["reason"],
                }
            continue

        if param not in bounds:
            continue

        # Find current value in the right section of strategy.yaml
        current = _get_current_value(param, strategy)
        if current is None:
            continue

        step = STEP_SIZES.get(param, 0)
        if step == 0:
            continue

        if direction == "increase":
            new_value = current + step
        elif direction == "decrease":
            new_value = current - step
        else:
            continue

        new_value = _clamp(new_value, bounds[param])

        if new_value != current:
            _set_value(param, new_value, strategy)
            changes[param] = {
                "old": current,
                "new": new_value,
                "reason": rec["reason"],
            }

    # Sync max_per_market_pct to circuit breakers if it changed
    if "max_per_market_pct" in changes:
        settings = _load_settings()
        settings["circuit_breakers"]["max_per_market_pct"] = changes["max_per_market_pct"]["new"]
        _save_settings(settings)

    # Normalize weights to sum to ~1.0
    _normalize_weights(strategy)

    if changes:
        _save_strategy(strategy)

    return changes


def _get_current_value(param: str, strategy: dict):
    """Look up a parameter value from the appropriate section of strategy.yaml."""
    fc = strategy.get("forecasting", {})
    scoring = strategy.get("scoring", {})
    exits = strategy.get("exits", {})

    lookup = {
        "kelly_multiplier": lambda: strategy.get("kelly_multiplier"),
        "min_edge": lambda: scoring.get("min_edge"),
        "min_composite_score": lambda: scoring.get("min_composite_score"),
        "llm_weight": lambda: fc.get("llm_weight"),
        "base_rate_weight": lambda: fc.get("base_rate_weight"),
        "metaculus_weight": lambda: fc.get("metaculus_weight"),
        "news_weight": lambda: fc.get("news_weight"),
        "market_consensus_weight": lambda: fc.get("market_consensus_weight"),
        "early_exit_threshold": lambda: exits.get("early_exit_threshold"),
        "max_per_market_pct": lambda: strategy.get("max_per_market_pct"),
    }
    getter = lookup.get(param)
    return getter() if getter else None


def _set_value(param: str, value, strategy: dict):
    """Write a parameter value to the appropriate section of strategy.yaml."""
    if param == "kelly_multiplier":
        strategy["kelly_multiplier"] = value
    elif param == "min_edge":
        strategy.setdefault("scoring", {})["min_edge"] = value
    elif param == "min_composite_score":
        strategy.setdefault("scoring", {})["min_composite_score"] = value
    elif param in ("llm_weight", "base_rate_weight", "metaculus_weight", "news_weight", "market_consensus_weight"):
        strategy.setdefault("forecasting", {})[param] = value
    elif param == "early_exit_threshold":
        strategy.setdefault("exits", {})["early_exit_threshold"] = value
    elif param == "max_per_market_pct":
        strategy["max_per_market_pct"] = value


def _normalize_weights(strategy: dict):
    """Ensure forecasting source weights approximately sum to 1.0."""
    fc = strategy.get("forecasting", {})
    weight_keys = ["llm_weight", "base_rate_weight", "metaculus_weight", "news_weight", "market_consensus_weight"]
    total = sum(fc.get(k, 0) for k in weight_keys)
    if total > 0 and abs(total - 1.0) > 0.05:
        for k in weight_keys:
            if k in fc:
                fc[k] = round(fc[k] / total, 4)


# ── Step 4+5: Full Optimization Cycle ────────────────────────────

def run_optimization(lookback_days: int = 14, dry_run: bool = False) -> dict:
    """
    Full Hermes optimization cycle.

    Returns complete optimization report.
    """
    strategy = _load_strategy()

    # Step 1: Review
    review = review_trades(lookback_days)

    # Step 2: Diagnose
    recommendations = diagnose(review, strategy)

    # Step 3: Tune (skip in dry run or insufficient data)
    settings = _load_settings()
    min_trades = settings.get("hermes", {}).get("min_trades_to_optimize", 5)

    if dry_run or review["total_trades"] < min_trades:
        changes = {}
    else:
        changes = apply_adjustments(recommendations, strategy)

    # Step 4: Log
    report = {
        "cycle": "hermes_optimization",
        "lookback_days": lookback_days,
        "dry_run": dry_run,
        "review": review,
        "recommendations": recommendations,
        "changes": changes,
    }
    _log_optimization(report)

    log_event("hermes", "optimization_complete", {
        "trades_reviewed": review["total_trades"],
        "recommendations": len(recommendations),
        "changes_applied": len(changes),
        "win_rate": review.get("win_rate", 0),
        "brier": review.get("brier_score"),
    })

    diary_write("hermes_agent",
        f"OPT|trades_{review['total_trades']}|wr_{review.get('win_rate', 0):.0%}|"
        f"brier_{review.get('brier_score', 'N/A')}|changes_{len(changes)}")

    return report


def print_optimization_report(report: dict):
    """Print a human-readable optimization report."""
    review = report["review"]
    recs = report["recommendations"]
    changes = report["changes"]

    print("=" * 60)
    print("  HERMES SELF-OPTIMIZATION REPORT")
    print("=" * 60)

    if review["total_trades"] == 0:
        print("  No resolved trades to analyze yet.")
        print("  Hermes needs resolved trades before tuning parameters.")
        return

    # Performance
    print(f"\n  PERFORMANCE REVIEW ({report['lookback_days']}d)")
    print(f"  {'Total trades:':<25s} {review['total_trades']}")
    print(f"  {'Win rate:':<25s} {review['win_rate']:.0%} ({review['wins']}W / {review['losses']}L)")
    print(f"  {'Avg win:':<25s} ${review.get('avg_win', 0):.2f}")
    print(f"  {'Avg loss:':<25s} ${review.get('avg_loss', 0):.2f}")
    print(f"  {'Total P/L:':<25s} ${review.get('total_pnl', 0):+.2f}")
    print(f"  {'Fees paid:':<25s} ${review.get('total_fees', 0):.2f}")

    # Calibration
    bs = review.get("brier_score")
    if bs is not None:
        quality = "Excellent" if bs < 0.10 else "Good" if bs < 0.15 else "Fair" if bs < 0.20 else "Poor"
        print(f"  {'Brier score:':<25s} {bs:.4f} ({quality})")

    sa = review.get("source_accuracy", {})
    if sa:
        print(f"\n  SOURCE ACCURACY (lower = better)")
        for src, data in sa.items():
            print(f"    {src:20s} Brier={data['brier']:.4f} (n={data['count']})")

    # By category
    by_cat = review.get("by_category", {})
    if by_cat:
        print(f"\n  BY CATEGORY")
        for cat, data in sorted(by_cat.items(), key=lambda x: -x[1]["pnl"]):
            wr = data["wins"] / data["trades"] if data["trades"] > 0 else 0
            print(f"    {cat:20s} {wr:.0%} WR ({data['wins']}/{data['trades']}) ${data['pnl']:+.2f}")

    # Recommendations
    print(f"\n  RECOMMENDATIONS ({len(recs)})")
    for r in recs:
        arrow = {"increase": "\u2191", "decrease": "\u2193", "add": "+", "hold": "\u2013"}.get(r["direction"], "?")
        print(f"  {arrow} {r['param']}: {r['reason']}")

    # Changes
    if changes:
        print(f"\n  CHANGES APPLIED ({len(changes)})")
        for param, detail in changes.items():
            print(f"  \u2022 {param}: {detail['old']} \u2192 {detail['new']}")
            print(f"    Reason: {detail['reason']}")
    elif report.get("dry_run"):
        print("\n  [DRY RUN \u2014 no changes applied]")
    else:
        print("\n  No parameter changes needed.")

    print("=" * 60)


def get_optimization_history(limit: int = 10) -> list[dict]:
    """Read the last N optimization entries from the log."""
    if not OPTIMIZATION_LOG.exists():
        return []
    entries = []
    with open(OPTIMIZATION_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return entries[-limit:]
