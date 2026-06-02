"""Hermes for Polybot KXBTCD DAILY — scientific-method auto-tuner for the
Kalshi daily-Bitcoin paper-trading pipeline.

Same shape as hermes_kalshi (15-min) and hermes_weather: diagnose →
pick one change → apply → ledger → review after the next window.

The 5 tunable knobs:
  - min_confidence              (lib/kalshi_daily_paper.DEFAULT_MIN_CONFIDENCE)
  - max_fill_for_buy            (lib/kalshi_daily_paper.MAX_FILL_FOR_BUY)
  - default_max_trade_usd       (per-trade cap)
  - default_kelly_multiplier    (sizing aggression)
  - min_strike_distance_sigmas  (strike-gate: how far from spot a strike
                                 must be to admit directional edge)

Persisted to config/kalshi_daily_strategy.yaml; kalshi_daily_paper.py
re-reads that file every signal cycle, so a write takes effect on the
next cycle.

Run via:
    python main.py kalshi-daily-hermes-cycle        # review (default)
    python main.py kalshi-daily-hermes-cycle --live
    python main.py kalshi-daily-hermes-cycle --set-mode live
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import yaml

from tradingcore import log_event

ROOT = Path(__file__).resolve().parent.parent
DAILY_STRATEGY_PATH = ROOT / "config" / "kalshi_daily_strategy.yaml"
PAPER_PATH = ROOT / "data" / "kalshi_daily_paper.jsonl"
LEDGER_PATH = ROOT / "data" / "hermes_daily_experiments.jsonl"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"


# ─── Parameter space ───────────────────────────────────────────────────
# 2026-05-27 EXPANSION: added the post-rebuild gates so Hermes can tune
# the strategy that actually fires now. Tuple shape:
#   (default, lo, hi, step, target_scope)
# where target_scope is:
#   "global"  → writes to top-level YAML key (legacy)
#   "btc"     → writes to per_asset_overrides.btc.<key> (per-asset)
#   "weather" → writes to per_asset_overrides.weather.<key>
#
# IMPORTANT: per-asset writes are essential. Global writes get OVERRIDDEN
# by per_asset_overrides at read time, so Hermes experiments on global
# `min_confidence` are silently ineffective for BTC/SPY/weather.
PARAM_SPACE = {
    # — Original 5 (global, cycle-level params) —
    "max_fill_for_buy":            (0.45, 0.35, 0.55,  0.05,  "global"),
    "default_max_trade_usd":       (5.0,  5.0,  15.0,  2.5,   "global"),
    "default_kelly_multiplier":    (0.5,  0.25, 0.75,  0.125, "global"),
    "min_strike_distance_sigmas":  (0.25, 0.15, 0.50,  0.05,  "global"),

    # — Per-BTC: the calibrated gates that actually fire now —
    # Naming convention: `<asset>__<key>` to avoid namespace collision when
    # multiple assets get the same logical knob (e.g. btc__min_confidence
    # vs weather__min_confidence). set_param parses the prefix.
    "btc__min_confidence":         (0.35, 0.20, 0.50,  0.05,  "btc"),
    "btc__theo_align_min_yes":     (0.45, 0.40, 0.55,  0.05,  "btc"),
    "btc__theo_align_max_for_no":  (0.40, 0.30, 0.50,  0.05,  "btc"),
    "btc__min_composite_abs":      (0.0,  0.0,  4.0,   1.0,   "btc"),
    "btc__skip_fill_low":          (0.30, 0.25, 0.35,  0.05,  "btc"),
    "btc__skip_fill_high":         (0.40, 0.35, 0.50,  0.05,  "btc"),
    "btc__whale_veto_threshold":   (0.5,  0.3,  0.8,   0.1,   "btc"),
}


# ─── YAML I/O ──────────────────────────────────────────────────────────

def _load_strategy_yaml() -> dict:
    if not DAILY_STRATEGY_PATH.exists():
        return {}
    try:
        with open(DAILY_STRATEGY_PATH) as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _save_strategy_yaml(data: dict) -> None:
    """Persist Hermes' chosen overrides. Belt-and-suspenders against a
    partial / corrupted optimizer run poisoning live trading: refuses
    empty / non-dict payloads, round-trip parses the tmp file, and on
    ANY failure the existing YAML stays untouched so the next signal
    cycle keeps using the last-good config. kalshi_daily_paper.py
    re-reads this file every cycle — a torn or garbage write would
    silently change trading."""
    if not isinstance(data, dict) or not data:
        log_event("hermes_daily", "yaml_write_refused",
                  {"reason": "empty_or_invalid_payload",
                   "type": type(data).__name__}, result="degraded")
        return
    DAILY_STRATEGY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DAILY_STRATEGY_PATH.with_suffix(DAILY_STRATEGY_PATH.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        with open(tmp) as f:
            parsed = yaml.safe_load(f)
        if parsed != data:
            log_event("hermes_daily", "yaml_write_refused",
                      {"reason": "roundtrip_mismatch"}, result="degraded")
            try: tmp.unlink()
            except OSError: pass
            return
        tmp.replace(DAILY_STRATEGY_PATH)
    except Exception as e:
        log_event("hermes_daily", "yaml_write_failed",
                  {"error": str(e)[:200]}, result="degraded")
        try: tmp.unlink()
        except OSError: pass


def _spec(name: str) -> tuple:
    """Return (default, lo, hi, step, scope) for a PARAM_SPACE entry.
    Handles both legacy 4-tuples and the new 5-tuples (with scope)."""
    spec = PARAM_SPACE[name]
    if len(spec) == 5:
        return spec
    return (*spec, "global")  # legacy default


def _split_scoped_key(name: str) -> tuple[str, str]:
    """Parse a PARAM_SPACE key into (yaml_key, asset_scope).
    "btc__min_confidence" → ("min_confidence", "btc")
    "min_confidence"      → ("min_confidence", "global")
    """
    spec = _spec(name)
    scope = spec[4]
    if "__" in name:
        return name.split("__", 1)[1], scope
    return name, scope


def get_current_params() -> dict:
    """Read current effective values for every tunable. Per-asset params
    are pulled from per_asset_overrides.<asset>.<key>; falls back to global
    if not set; falls back to default if still missing."""
    strategy = _load_strategy_yaml()
    out = {}
    for name, spec in PARAM_SPACE.items():
        default = spec[0]
        scope = spec[4] if len(spec) == 5 else "global"
        yaml_key, _ = _split_scoped_key(name)
        if scope == "global":
            out[name] = float(strategy.get(yaml_key, default))
        else:
            per = (strategy.get("per_asset_overrides") or {}).get(scope) or {}
            # Per-asset wins, then global fallback, then default.
            if yaml_key in per and per[yaml_key] is not None:
                out[name] = float(per[yaml_key])
            elif yaml_key in strategy and strategy[yaml_key] is not None:
                out[name] = float(strategy[yaml_key])
            else:
                out[name] = float(default)
    return out


def set_param(name: str, value) -> None:
    """Persist a parameter. Honors per-asset scope — `btc__min_confidence`
    is written under per_asset_overrides.btc.min_confidence."""
    if name not in PARAM_SPACE:
        raise ValueError(f"unknown daily hermes param: {name}")
    yaml_key, scope = _split_scoped_key(name)
    strategy = _load_strategy_yaml()
    if scope == "global":
        strategy[yaml_key] = float(value)
    else:
        per = strategy.setdefault("per_asset_overrides", {})
        bucket = per.setdefault(scope, {})
        bucket[yaml_key] = float(value)
    _save_strategy_yaml(strategy)


def _clamp(name: str, value: float) -> float:
    _, lo, hi, step, *_ = _spec(name)
    # Some knobs (currently just default_max_trade_usd) scale their
    # upper bound with bankroll — see _dynamic_ceiling.
    hi = _dynamic_ceiling(name, hi)
    # Discrete sizing for default_max_trade_usd (snap to $0.50 increments)
    if name == "default_max_trade_usd":
        return round(max(lo, min(hi, value)) / 0.5) * 0.5
    return round(max(lo, min(hi, value)), 4)


# ─── Dynamic bankroll-aware ceilings ───────────────────────────────────
#
# 2026-05-28: per-trade cap (default_max_trade_usd) used to be hard-capped
# at $15. With paper cum_pnl growing, that ceiling was blocking Hermes
# from sizing up. Now scales with bankroll (2% per single trade), with a
# hard $500 outer cap regardless of bankroll.
#
# Safety:
#   - Hermes still enforces step-wise $2.50 increments
#   - 48h experiment + auto-rollback validates each bump
#   - Live mode separately capped by settings.yaml.max_trade_usd
#     and live_asset_budgets — this knob primarily affects paper sizing

_STARTING_BANKROLL = 50.0
_BANKROLL_FRACTION = 0.02
_ABSOLUTE_CAP_USD = 500.0


def _read_cum_paper_pnl() -> float:
    """Sum of paper_pnl across both BTC-daily and weather paper logs."""
    total = 0.0
    for fname in ("weather_paper.jsonl", "kalshi_daily_paper.jsonl"):
        p = ROOT / "data" / fname
        if not p.exists():
            continue
        try:
            with open(p) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("status") in ("won", "lost"):
                        total += float(rec.get("paper_pnl") or 0.0)
        except OSError:
            continue
    return total


def _bankroll_proxy() -> float:
    """Best-guess current bankroll = $50 seed + cumulative paper PnL."""
    return _STARTING_BANKROLL + _read_cum_paper_pnl()


def _dynamic_ceiling(name: str, static_hi: float) -> float:
    """Bankroll-aware upper bound for sizing knobs.

    Only default_max_trade_usd scales today. All other params return
    their static PARAM_SPACE ceiling unchanged."""
    if name == "default_max_trade_usd":
        bankroll = _bankroll_proxy()
        scaled = bankroll * _BANKROLL_FRACTION
        return min(_ABSOLUTE_CAP_USD, max(static_hi, scaled))
    return static_hi


# ─── Scoring ───────────────────────────────────────────────────────────

def _load_paper_trades() -> list[dict]:
    if not PAPER_PATH.exists():
        return []
    out = []
    try:
        with open(PAPER_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def compute_daily_goal_metrics(window_days: int = 7) -> dict:
    """Scoring snapshot for one Hermes cycle on KXBTCD daily.

    Same shape as the kalshi-15min / weather metrics so the ledger
    machinery in tradingcore.hermes_ledger can consume it directly."""
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

    closed = [t for t in window if t.get("status") in ("won", "lost")]
    wins = sum(1 for t in closed if t.get("status") == "won")
    losses = sum(1 for t in closed if t.get("status") == "lost")
    n = len(closed)
    pnl = sum(float(t.get("paper_pnl") or 0.0) for t in closed)
    deployed = sum(float(t.get("notional") or 0.0) for t in closed)
    wr = (wins / n) if n > 0 else None
    roi = (pnl / deployed) if deployed > 0 else 0.0

    # Lifetime drawdown
    all_closed = sorted(
        [t for t in trades if t.get("status") in ("won", "lost")],
        key=lambda r: r.get("opened_at", ""),
    )
    cum = peak = 0.0
    for t in all_closed:
        cum += float(t.get("paper_pnl") or 0.0)
        if cum > peak:
            peak = cum
    drawdown_from_peak = max(0.0, peak - cum)

    # Per-side breakdown — YES vs NO. Useful for catching directional bias
    # that the diagnoser can act on (e.g. if NO trades have 0% WR while
    # YES is 50%, we know the composite is mis-signed somewhere).
    by_side: dict[str, dict] = {}
    for t in closed:
        side = t.get("side", "?")
        b = by_side.setdefault(side, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        if t.get("status") == "won":
            b["wins"] += 1
        b["pnl"] += float(t.get("paper_pnl") or 0.0)
    for s, b in by_side.items():
        b["wr"] = round(b["wins"] / b["n"], 4) if b["n"] else None
        b["pnl"] = round(b["pnl"], 4)

    days = max(window_days, 1)
    return {
        "window_days": window_days,
        "n_trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wr, 4) if wr is not None else None,
        "rolling_pnl": round(pnl, 4),
        "rolling_30d_pnl": round(pnl, 4),
        "deployed": round(deployed, 4),
        "roi": round(roi, 4),
        "trades_per_day": round(n / days, 2),
        "cumulative_pnl_lifetime": round(cum, 4),
        "peak_pnl_lifetime": round(peak, 4),
        "drawdown_from_peak": round(drawdown_from_peak, 4),
        "by_side": by_side,
        "goal_distance_pct": round(max(0.0, 0.05 - roi) / 0.05, 4)
            if roi < 0.05 else 0.0,
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── Diagnosis ─────────────────────────────────────────────────────────

def diagnose(metrics: dict) -> list[dict]:
    """Per-cycle diagnosis rules for KXBTCD daily."""
    recs: list[dict] = []
    n = metrics.get("n_trades", 0)
    wr = metrics.get("win_rate")
    roi = metrics.get("roi", 0)
    tpd = metrics.get("trades_per_day", 0)
    dd = metrics.get("drawdown_from_peak", 0)
    by_side = metrics.get("by_side") or {}

    if n < 5:
        return [{
            "param": "none", "direction": "hold", "confidence": 0.0,
            "reason": f"insufficient trades ({n} < 5)",
        }]

    # Low WR + negative ROI → raise BTC confidence floor (per-asset target).
    if wr is not None and wr < 0.30 and roi < 0:
        recs.append({
            "param": "btc__min_confidence",
            "direction": "increase",
            "confidence": min(1.0, (0.30 - wr) * 3 + abs(roi) * 4),
            "reason": (
                f"WR {wr:.0%} + ROI {roi:+.1%} — demand higher signal confidence"
            ),
        })
        # Also tighten alignment gate when WR is bad — only fire on stronger
        # directional conviction.
        recs.append({
            "param": "btc__theo_align_min_yes",
            "direction": "increase",
            "confidence": min(1.0, (0.30 - wr) * 2.5),
            "reason": (
                f"WR {wr:.0%} — tighten YES alignment gate (require higher theo)"
            ),
        })

    # High WR + low throughput + positive ROI → loosen BTC confidence floor.
    if wr is not None and wr > 0.50 and tpd < 2.0 and roi > 0:
        recs.append({
            "param": "btc__min_confidence",
            "direction": "decrease",
            "confidence": min(1.0, (wr - 0.50) * 3),
            "reason": (
                f"WR {wr:.0%} on {tpd:.1f} trades/day — loosen confidence"
            ),
        })

    # 2026-05-27 NEW: directional alignment gate — if both YES and NO have
    # data and one is far worse, widen its alignment threshold. If overall
    # WR is decent but YES specifically losing, tighten theo_align_min_yes.
    yes = by_side.get("YES") or {}
    no  = by_side.get("NO")  or {}
    yn = yes.get("n", 0); nn = no.get("n", 0)
    yw = yes.get("wr"); nw = no.get("wr")
    if yn >= 4 and yw is not None and yw < 0.35:
        recs.append({
            "param": "btc__theo_align_min_yes",
            "direction": "increase",
            "confidence": min(1.0, (0.35 - yw) * 3),
            "reason": (
                f"YES side WR {yw:.0%} on {yn} trades — tighten YES alignment"
            ),
        })
    if nn >= 4 and nw is not None and nw < 0.35:
        recs.append({
            "param": "btc__theo_align_max_for_no",
            "direction": "decrease",
            "confidence": min(1.0, (0.35 - nw) * 3),
            "reason": (
                f"NO side WR {nw:.0%} on {nn} trades — tighten NO alignment"
            ),
        })

    # 2026-05-27 NEW: composite-magnitude floor. If WR is low across many
    # trades, require stronger signal magnitude (filter out marginal setups).
    if n >= 10 and wr is not None and wr < 0.35:
        recs.append({
            "param": "btc__min_composite_abs",
            "direction": "increase",
            "confidence": min(1.0, (0.35 - wr) * 2.5),
            "reason": (
                f"WR {wr:.0%} on {n} trades — require stronger composite magnitude"
            ),
        })

    # Negative ROI but reasonable WR → R:R is the problem. Tighten fill.
    if roi < -0.10 and wr is not None and wr >= 0.30:
        recs.append({
            "param": "max_fill_for_buy",
            "direction": "decrease",
            "confidence": min(1.0, abs(roi) * 4),
            "reason": (
                f"ROI {roi:+.1%} with WR {wr:.0%} — pay less per contract"
            ),
        })

    # Tighten strike-gate when WR is low — strikes too close to spot
    # are coin flips, more sigma distance buys directional clarity.
    if wr is not None and wr < 0.30 and n >= 10:
        recs.append({
            "param": "min_strike_distance_sigmas",
            "direction": "increase",
            "confidence": min(1.0, (0.30 - wr) * 2.5),
            "reason": (
                f"WR {wr:.0%} on {n} trades — strikes may be too close to spot"
            ),
        })

    # Strong positive ROI sustained → scale up trade cap.
    if roi > 0.15 and n >= 15:
        recs.append({
            "param": "default_max_trade_usd",
            "direction": "increase",
            "confidence": min(1.0, roi * 3),
            "reason": (
                f"ROI {roi:+.1%} over {n} trades — scale up per-trade cap"
            ),
        })

    # Big drawdown → pull back Kelly multiplier. Daily P&L magnitudes
    # are bigger than weather/15min so we use a higher threshold ($50).
    if dd > 50.0 and n >= 10:
        recs.append({
            "param": "default_kelly_multiplier",
            "direction": "decrease",
            "confidence": min(1.0, dd / 100.0),
            "reason": (
                f"drawdown ${dd:.0f} from peak — reduce Kelly aggression"
            ),
        })

    # Directional asymmetry catch (both sides ≥5 trades, ≥25pp WR gap):
    # tighten BTC confidence so weak-side trades stop firing as easily.
    # Already have per-side alignment rules above; this is the global-conf
    # defensive layer when the asymmetry is large.
    if yn >= 5 and nn >= 5 and yw is not None and nw is not None:
        wr_gap = abs(yw - nw)
        weak_side = "NO" if yw > nw else "YES"
        weak_wr = nw if yw > nw else yw
        if wr_gap >= 0.25 and weak_wr < 0.30:
            recs.append({
                "param": "btc__min_confidence",
                "direction": "increase",
                "confidence": min(1.0, wr_gap * 2 + (0.30 - weak_wr) * 2),
                "reason": (
                    f"directional asymmetry: YES {yes.get('wins',0)}/{yn} ({yw*100:.0f}%) "
                    f"vs NO {no.get('wins',0)}/{nn} ({nw*100:.0f}%) — "
                    f"weak {weak_side} side dragging composite"
                ),
            })

    if not recs:
        recs.append({
            "param": "none", "direction": "hold", "confidence": 0.0,
            "reason": (
                f"WR {wr}, ROI {roi:+.1%}, dd ${dd:.0f} — within tolerance"
            ),
        })
    # Significance guard (2026-06-01): the rules above fire on per-side samples
    # as small as n>=4, where a WR point-estimate has ~15-25pp standard error.
    # temper_recommendations is CONSERVATIVE — it can only drop a rec whose
    # effective sample is < MIN_ACT_N or shrink its confidence by sample size,
    # never strengthen or add one. So it can only make the optimizer MORE
    # cautious about touching live params off noise.
    from lib.hermes_significance import temper_recommendations
    return temper_recommendations(recs, by_side, n)


# ─── Scientific cycle ──────────────────────────────────────────────────

Mode = Literal["review", "live"]


def get_mode() -> Mode:
    try:
        with open(SETTINGS_PATH) as f:
            s = yaml.safe_load(f) or {}
        m = str(s.get("daily_hermes_mode", "review")).lower().strip()
        return "live" if m == "live" else "review"
    except OSError:
        return "review"


def set_mode(mode: Mode) -> None:
    mode = "live" if mode == "live" else "review"
    with open(SETTINGS_PATH) as f:
        s = yaml.safe_load(f) or {}
    s["daily_hermes_mode"] = mode
    with open(SETTINGS_PATH, "w") as f:
        yaml.safe_dump(s, f, default_flow_style=False, sort_keys=False)


def pick_one_change(recs: list[dict]) -> dict | None:
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


def _count_trades_since(opened_iso: str) -> int:
    """Count paper trades whose entry (opened_at) is at/after `opened_iso` —
    i.e. genuine POST-treatment trades for an experiment opened then. Used to
    gate the keep/rollback decision on real post-change evidence rather than a
    trailing window that still mostly contains pre-change (baseline) trades."""
    try:
        cutoff = datetime.fromisoformat(str(opened_iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return 0
    n = 0
    for t in _load_paper_trades():
        ts = t.get("opened_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt >= cutoff and t.get("status") in ("won", "lost"):
            n += 1
    return n


def close_prior_experiments(
    min_age_hours: float = 48.0, keep_threshold_delta: float = 0.001,
    min_post_samples: int = 20,
) -> list[dict]:
    """48h window — daily-resolved markets need at least 2 daily cycles
    of post-treatment data to evaluate fairly.

    Significance guard (2026-06-01): an experiment is only GRADED once at least
    `min_post_samples` trades have settled SINCE it opened. Until then the
    ledger holds it open as 'inconclusive' rather than keeping/reverting off a
    handful of post-change trades blended with baseline noise. The prior
    behavior graded on a trailing 7-day window that still mostly contained
    pre-change trades, so a 0.1pp goal-distance flicker flipped the verdict."""
    from tradingcore.hermes_ledger import (
        list_open_experiments, close_experiment,
    )

    now = datetime.now(timezone.utc)
    open_exps = list_open_experiments(ledger_path=LEDGER_PATH)
    current = compute_daily_goal_metrics()
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
        # Report genuine post-treatment trade count so the ledger can hold the
        # experiment open until there's enough evidence to grade it.
        post = dict(current)
        post["n_post_trades"] = _count_trades_since(exp["opened_at"])
        result = close_experiment(
            exp["experiment_id"],
            post_metrics=post,
            keep_threshold_delta=keep_threshold_delta,
            min_post_samples=min_post_samples,
            ledger_path=LEDGER_PATH,
        )
        # Only revert YAML on a genuine rollback; 'inconclusive' keeps status
        # 'open' (we wait for more data) so we must NOT revert then.
        if result and result.get("status") == "rolled_back":
            set_param(result["param"], result["old_value"])
        if result:
            closed.append(result)
    return closed


def run_cycle(force_mode: Mode | None = None) -> dict:
    from tradingcore.hermes_ledger import open_experiment

    mode = force_mode or get_mode()
    closed = close_prior_experiments()
    metrics = compute_daily_goal_metrics()
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
    lines.append(f"KXBTCD DAILY HERMES CYCLE  —  mode={report.get('mode')}")
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
    # Show dynamic ceiling so it's obvious why Hermes is/isn't scaling
    # default_max_trade_usd. Bankroll = seed + paper cum_pnl across both
    # weather and BTC-daily paper logs.
    bankroll = _bankroll_proxy()
    static_hi = _spec("default_max_trade_usd")[2]
    dyn_hi = _dynamic_ceiling("default_max_trade_usd", static_hi)
    lines.append(
        f"Bankroll proxy: ${bankroll:,.0f}  "
        f"→ max_trade_usd ceiling: ${dyn_hi:,.1f}  "
        f"(static=${static_hi:.1f}, scaled={_BANKROLL_FRACTION*100:.0f}% of bankroll, "
        f"hard-cap=${_ABSOLUTE_CAP_USD:.0f})"
    )
    by_side = m.get("by_side") or {}
    if by_side:
        breakdown = " · ".join(
            f"{s} {b['wins']}/{b['n']}({(b.get('wr') or 0)*100:.0f}%) ${b['pnl']:+.1f}"
            for s, b in sorted(by_side.items())
        )
        lines.append(f"By side: {breakdown}")
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
    "compute_daily_goal_metrics", "diagnose",
    "get_mode", "set_mode",
    "pick_one_change", "close_prior_experiments",
    "run_cycle", "render_cycle",
]


if __name__ == "__main__":
    print(render_cycle(run_cycle()))
