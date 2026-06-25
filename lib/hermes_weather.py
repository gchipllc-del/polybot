"""Hermes for Polybot WEATHER — scientific-method auto-tuner for the
Kalshi hourly-weather paper-trading pipeline.

Mirrors lib/hermes_kalshi.py exactly (same scientific loop: diagnose →
pick one change → apply → ledger → review after the next window).
What's different here is the parameter space and the diagnosis rules,
both tailored to the weather strategy's leverage points.

The 4 tunable knobs in this first cut:
  - min_edge_threshold           (lib/weather_paper.MIN_EDGE_THRESHOLD)
  - max_fill_for_buy             (lib/weather_paper.MAX_FILL_FOR_BUY)
  - default_max_trade_usd        (sizing cap per trade)
  - default_kelly_multiplier     (sizing aggression — half-Kelly default)

These are persisted to config/weather_strategy.yaml; weather_paper.py
re-reads that file every signal cycle (see _effective_params), so a
Hermes write takes effect on the very next cycle.

NOT tuned here (deliberately):
  - EXTREME_PRICE_FLOOR/CEIL — fixed Kalshi quote bounds
  - σ / bias from weather_calibration — that's a separate learning loop
    that already updates after every settlement
  - horizon_aware_sigma_f buckets in weather_signal — model structure,
    not a per-cycle knob

Run via:
    python main.py weather-hermes-cycle             # review (default)
    python main.py weather-hermes-cycle --live      # apply one change
    python main.py weather-hermes-cycle --set-mode live
    python main.py weather-hermes-cycle --set-mode review
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import yaml

try:
    from tradingcore import log_event
except ImportError:  # tradingcore is vendored only in the live clone; no-op telemetry here
    def log_event(*_a, **_k):
        pass

ROOT = Path(__file__).resolve().parent.parent
WEATHER_STRATEGY_PATH = ROOT / "config" / "weather_strategy.yaml"
PAPER_PATH = ROOT / "data" / "weather_paper.jsonl"
LEDGER_PATH = ROOT / "data" / "hermes_weather_experiments.jsonl"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"


# ─── Parameter space ───────────────────────────────────────────────────
# (default, low, high, step)
#
# Bounds chosen to keep the strategy recognizable: we don't let Hermes
# loosen MIN_EDGE below 5pp (no real edge below that — Kalshi spreads
# alone consume it) or push MAX_FILL above 0.55 (R:R inverts past that).
PARAM_SPACE = {
    "min_edge_threshold":        (0.10, 0.05, 0.20, 0.025),
    "max_fill_for_buy":          (0.45, 0.35, 0.55, 0.05),
    "default_max_trade_usd":     (5.0,  5.0,  15.0, 2.5),
    "default_kelly_multiplier":  (0.5,  0.25, 0.75, 0.125),
    # 2026-05-28: asymmetric forecast-direction gate. NO and YES use
    # different buffers because forecasts miss low more often than high —
    # YES needs more upside conviction. Backtest of 16 historical YES
    # trades: winners had +0.68°F gap, losers +0.06°F. 1.5°F catches the
    # high-conviction subset.
    "forecast_buffer_f":         (1.0,  0.5,  2.0,  0.25),
    "forecast_buffer_f_yes":     (1.5,  1.0,  3.0,  0.5),
}


# ─── YAML I/O ──────────────────────────────────────────────────────────

def _load_strategy_yaml() -> dict:
    if not WEATHER_STRATEGY_PATH.exists():
        return {}
    try:
        with open(WEATHER_STRATEGY_PATH) as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _save_strategy_yaml(data: dict) -> None:
    """Persist Hermes' chosen overrides. Belt-and-suspenders against a
    partial / corrupted optimizer run poisoning live trading: refuses
    empty / non-dict payloads, round-trip parses the tmp file, and on
    ANY failure the existing YAML stays untouched so the next signal
    cycle keeps using the last-good config. weather_paper.py re-reads
    this file every cycle — a torn or garbage write would silently
    change trading."""
    if not isinstance(data, dict) or not data:
        log_event("hermes_weather", "yaml_write_refused",
                  {"reason": "empty_or_invalid_payload",
                   "type": type(data).__name__}, result="degraded")
        return
    WEATHER_STRATEGY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = WEATHER_STRATEGY_PATH.with_suffix(WEATHER_STRATEGY_PATH.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
        with open(tmp) as f:
            parsed = yaml.safe_load(f)
        if parsed != data:
            log_event("hermes_weather", "yaml_write_refused",
                      {"reason": "roundtrip_mismatch"}, result="degraded")
            try: tmp.unlink()
            except OSError: pass
            return
        tmp.replace(WEATHER_STRATEGY_PATH)
    except Exception as e:
        log_event("hermes_weather", "yaml_write_failed",
                  {"error": str(e)[:200]}, result="degraded")
        try: tmp.unlink()
        except OSError: pass


def get_current_params() -> dict:
    """Current value of every tunable, falling back to PARAM_SPACE defaults
    when weather_strategy.yaml is missing or doesn't have the key yet."""
    strategy = _load_strategy_yaml()
    return {
        k: float(strategy.get(k, default))
        for k, (default, *_) in PARAM_SPACE.items()
    }


def set_param(name: str, value) -> None:
    if name not in PARAM_SPACE:
        raise ValueError(f"unknown weather hermes param: {name}")
    strategy = _load_strategy_yaml()
    strategy[name] = float(value)
    _save_strategy_yaml(strategy)


def _clamp(name: str, value: float) -> float:
    _, lo, hi, _step = PARAM_SPACE[name]
    # Some knobs (currently just default_max_trade_usd) scale their
    # upper bound with bankroll — see _dynamic_ceiling.
    hi = _dynamic_ceiling(name, hi)
    if name == "default_max_trade_usd":
        # Keep at multiples of $2.5 so the ledger stays human-readable
        return round(max(lo, min(hi, value)) / 0.5) * 0.5
    return round(max(lo, min(hi, value)), 4)


# ─── Dynamic bankroll-aware ceilings ───────────────────────────────────
#
# 2026-05-28: the per-trade cap (default_max_trade_usd) used to be hard-
# capped at $15. With paper cum_pnl at +$1.5k that ceiling was actively
# blocking Hermes from pushing sizing up despite a +187% ROI signal.
#
# Now we scale the upper bound with bankroll: roughly 2% of bankroll as
# max single-trade cap. Sensible Kelly territory for an aggressive
# growth trader (project goal is $50→$25k).
#
# Safety:
#   - Hermes still enforces step-wise increments ($2.50/cycle)
#   - 48h experiment + auto-rollback if metrics regress
#   - Live mode separately capped by settings.yaml.max_trade_usd
#     + live_asset_budgets — this knob only fully unlocks for paper
#   - Outer cap of $500 prevents runaway scaling beyond what we've
#     validated even when the project hits its $25k goal

_STARTING_BANKROLL = 50.0           # project seed per memory
_BANKROLL_FRACTION = 0.02           # 2% per single trade
_ABSOLUTE_CAP_USD = 500.0           # hard outer ceiling regardless of bankroll


def _read_cum_paper_pnl() -> float:
    """Sum of paper_pnl across both BTC-daily and weather paper logs.
    Used as a bankroll proxy for dynamic sizing ceilings."""
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
    """Best-guess current bankroll = $50 seed + cumulative paper PnL.
    Live Kalshi balance isn't blended in here on purpose — the YAML
    knob feeds paper math first, and live trades have their own
    settings.yaml caps downstream."""
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


def compute_weather_goal_metrics(window_days: int = 7) -> dict:
    """Scoring snapshot for one Hermes cycle on weather.

    Returns a JSON-safe dict slotted directly into the experiment ledger
    as baseline / post snapshots. goal_distance_pct treats a 5%+ ROI over
    the rolling window as 'goal hit' (distance=0), scaled linearly below."""
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

    # Lifetime drawdown vs peak (across all closed trades, not just window).
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

    # Per-city WR breakdown — at this point only NYC has meaningful n,
    # but this gets useful as other cities accumulate samples. The
    # diagnoser doesn't use this yet (too few cities have n>=5), but
    # storing it makes the ledger more diagnosable post-hoc.
    by_city: dict[str, dict] = {}
    for t in closed:
        city = t.get("city", "?")
        b = by_city.setdefault(city, {"n": 0, "wins": 0, "pnl": 0.0})
        b["n"] += 1
        if t.get("status") == "won":
            b["wins"] += 1
        b["pnl"] += float(t.get("paper_pnl") or 0.0)
    for c, b in by_city.items():
        b["wr"] = round(b["wins"] / b["n"], 4) if b["n"] else None
        b["pnl"] = round(b["pnl"], 4)

    # 2026-05-28: per-side WR (YES vs NO). The diagnoser uses these to
    # tune YES vs NO buffers independently. NO is the long-running engine
    # (~75% WR on 28 trades); YES is the high-conviction occasional bet.
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
        "rolling_30d_pnl": round(pnl, 4),   # ledger compatibility alias
        "deployed": round(deployed, 4),
        "roi": round(roi, 4),
        "trades_per_day": round(n / days, 2),
        "cumulative_pnl_lifetime": round(cum, 4),
        "peak_pnl_lifetime": round(peak, 4),
        "drawdown_from_peak": round(drawdown_from_peak, 4),
        "by_city": by_city,
        "by_side": by_side,
        "goal_distance_pct": round(max(0.0, 0.05 - roi) / 0.05, 4)
            if roi < 0.05 else 0.0,
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
    }


# ─── Diagnosis ─────────────────────────────────────────────────────────

def diagnose(metrics: dict) -> list[dict]:
    """Inspect the weather metrics and propose param changes.

    One-rule-per-condition; pick_one_change picks the single highest-
    confidence rec to actually apply. That enforces the one-variable-at-
    a-time discipline that makes the experiment ledger interpretable."""
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

    # Low WR + negative ROI → raise edge threshold (be more selective).
    # We require BOTH conditions because weather strategy buys low-priced
    # contracts on purpose (WR alone can be 25-35% and still profitable);
    # only when ROI confirms the WR drop do we tighten the entry filter.
    if wr is not None and wr < 0.30 and roi < 0:
        recs.append({
            "param": "min_edge_threshold",
            "direction": "increase",
            "confidence": min(1.0, (0.30 - wr) * 3 + abs(roi) * 4),
            "reason": (
                f"WR {wr:.0%} + ROI {roi:+.1%} — raise edge floor to be more selective"
            ),
        })

    # Very high WR + low throughput → too selective, loosen edge floor
    # to capture more trades. Only triggers when we have positive ROI.
    if wr is not None and wr > 0.55 and tpd < 1.0 and roi > 0:
        recs.append({
            "param": "min_edge_threshold",
            "direction": "decrease",
            "confidence": min(1.0, (wr - 0.55) * 3),
            "reason": (
                f"WR {wr:.0%} on {tpd:.1f} trades/day — loosen edge to scale up"
            ),
        })

    # Negative ROI but reasonable WR → bad R:R per trade. Tighten fill cap.
    if roi < -0.10 and wr is not None and wr >= 0.30:
        recs.append({
            "param": "max_fill_for_buy",
            "direction": "decrease",
            "confidence": min(1.0, abs(roi) * 4),
            "reason": (
                f"ROI {roi:+.1%} with WR {wr:.0%} — pay less for each contract"
            ),
        })

    # Strong positive ROI sustained → scale up trade size cap. We require
    # both ROI > 15% AND n >= 15 trades so a lucky streak doesn't pump us
    # into oversized positions.
    if roi > 0.15 and n >= 15:
        recs.append({
            "param": "default_max_trade_usd",
            "direction": "increase",
            "confidence": min(1.0, roi * 3),
            "reason": (
                f"ROI {roi:+.1%} over {n} trades — scale up per-trade cap"
            ),
        })

    # Big drawdown → cut Kelly multiplier (de-aggressive sizing). Triggers
    # at $20 drawdown for weather (smaller dollar amounts than 15-min).
    if dd > 20.0 and n >= 10:
        recs.append({
            "param": "default_kelly_multiplier",
            "direction": "decrease",
            "confidence": min(1.0, dd / 40.0),
            "reason": (
                f"drawdown ${dd:.0f} from peak — pull back Kelly aggression"
            ),
        })

    # 2026-05-28: per-side buffer tuning. YES and NO have different
    # accuracy profiles — NO is the steady ~75% engine (forecast comes
    # in BELOW strike), YES is the high-conviction occasional bet
    # (forecast comes in ABOVE strike). When one side drags, tighten
    # its buffer specifically rather than blanket-tightening the
    # edge floor (which would starve both sides equally).
    by_side = metrics.get("by_side") or {}
    yes = by_side.get("YES") or {}
    no_ = by_side.get("NO") or {}
    yn = yes.get("n", 0); nn = no_.get("n", 0)
    yw = yes.get("wr");   nw = no_.get("wr")

    # YES underperforming → forecast wasn't far enough above strike;
    # require a bigger gap before firing YES. Threshold 0.50 chosen
    # because YES is a long-shot bet (paying ~$0.20-0.35); break-even
    # WR ≈ 35-45%, so <50% means we're systematically too loose.
    if yn >= 4 and yw is not None and yw < 0.50:
        recs.append({
            "param": "forecast_buffer_f_yes",
            "direction": "increase",
            "confidence": min(1.0, (0.50 - yw) * 3),
            "reason": (
                f"YES WR {yw:.0%} on {yn} trades — tighten YES forecast buffer"
            ),
        })

    # NO underperforming → forecast wasn't far enough below strike;
    # need bigger downside gap. NO threshold 0.60 (vs 0.50 YES)
    # because NO contracts cost more (~$0.60-0.75) so break-even WR
    # is higher (~55-65%).
    if nn >= 4 and nw is not None and nw < 0.60:
        recs.append({
            "param": "forecast_buffer_f",
            "direction": "increase",
            "confidence": min(1.0, (0.60 - nw) * 3),
            "reason": (
                f"NO WR {nw:.0%} on {nn} trades — tighten NO forecast buffer"
            ),
        })

    # YES dominating + plenty of samples → buffer might be too tight,
    # we're leaving good YES trades on the table. Only loosen when WR
    # is well above break-even AND we have ROI conviction.
    if yn >= 8 and yw is not None and yw > 0.65 and roi > 0.10:
        recs.append({
            "param": "forecast_buffer_f_yes",
            "direction": "decrease",
            "confidence": min(1.0, (yw - 0.65) * 3),
            "reason": (
                f"YES WR {yw:.0%} on {yn} trades — loosen YES buffer to fire more"
            ),
        })

    # NO dominating → same logic, loosen NO buffer.
    if nn >= 8 and nw is not None and nw > 0.75 and roi > 0.10:
        recs.append({
            "param": "forecast_buffer_f",
            "direction": "decrease",
            "confidence": min(1.0, (nw - 0.75) * 3),
            "reason": (
                f"NO WR {nw:.0%} on {nn} trades — loosen NO buffer to fire more"
            ),
        })

    if not recs:
        recs.append({
            "param": "none", "direction": "hold", "confidence": 0.0,
            "reason": (
                f"WR {wr}, ROI {roi:+.1%}, dd ${dd:.0f} — within tolerance"
            ),
        })
    # Significance guard (2026-06-01) — see hermes_daily.diagnose. CONSERVATIVE:
    # only drops/weakens recs whose per-side sample is too small to distinguish
    # from noise; never strengthens or adds. Weather per-side rules fire at
    # n>=4-8, where WR has ~15-25pp standard error.
    from lib.hermes_significance import temper_recommendations
    return temper_recommendations(recs, by_side, n)


# ─── Scientific cycle ──────────────────────────────────────────────────

Mode = Literal["review", "live"]


def get_mode() -> Mode:
    try:
        with open(SETTINGS_PATH) as f:
            s = yaml.safe_load(f) or {}
        m = str(s.get("weather_hermes_mode", "review")).lower().strip()
        return "live" if m == "live" else "review"
    except OSError:
        return "review"


def set_mode(mode: Mode) -> None:
    mode = "live" if mode == "live" else "review"
    with open(SETTINGS_PATH) as f:
        s = yaml.safe_load(f) or {}
    s["weather_hermes_mode"] = mode
    with open(SETTINGS_PATH, "w") as f:
        yaml.safe_dump(s, f, default_flow_style=False, sort_keys=False)


def pick_one_change(recs: list[dict]) -> dict | None:
    """Pick highest-confidence rec that hasn't recently been rolled back."""
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
    """Count weather paper trades settled (won/lost) with opened_at at/after
    `opened_iso` — genuine POST-treatment trades for gating keep/rollback."""
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
    """Evaluate any still-open experiments older than min_age_hours against
    the current goal metrics. Roll back the param if metrics regressed.

    48h here vs 24h for kalshi-hermes — weather settlements are hourly
    but we only generate a handful of trades per day, so a 48h window
    gives enough post-treatment samples to read the signal.

    Significance guard (2026-06-01): only GRADE once >= min_post_samples trades
    have settled since the experiment opened; otherwise hold it open as
    'inconclusive' (don't keep/revert off blended baseline noise)."""
    from tradingcore.hermes_ledger import (
        list_open_experiments, close_experiment,
    )

    now = datetime.now(timezone.utc)
    open_exps = list_open_experiments(ledger_path=LEDGER_PATH)
    current = compute_weather_goal_metrics()
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
        post = dict(current)
        post["n_post_trades"] = _count_trades_since(exp["opened_at"])
        result = close_experiment(
            exp["experiment_id"],
            post_metrics=post,
            keep_threshold_delta=keep_threshold_delta,
            min_post_samples=min_post_samples,
            ledger_path=LEDGER_PATH,
        )
        if result and result.get("status") == "rolled_back":
            set_param(result["param"], result["old_value"])
        if result:
            closed.append(result)
    return closed


def run_cycle(force_mode: Mode | None = None) -> dict:
    """End-to-end weather Hermes pass."""
    from tradingcore.hermes_ledger import open_experiment

    mode = force_mode or get_mode()
    closed = close_prior_experiments()
    metrics = compute_weather_goal_metrics()
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
    lines.append(f"WEATHER HERMES CYCLE  —  mode={report.get('mode')}")
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
    # Show the dynamic ceiling so it's obvious why Hermes is/isn't
    # scaling default_max_trade_usd. Bankroll = seed + paper cum_pnl.
    bankroll = _bankroll_proxy()
    _, _, static_hi, _ = PARAM_SPACE["default_max_trade_usd"]
    dyn_hi = _dynamic_ceiling("default_max_trade_usd", static_hi)
    lines.append(
        f"Bankroll proxy: ${bankroll:,.0f}  "
        f"→ max_trade_usd ceiling: ${dyn_hi:,.1f}  "
        f"(static=${static_hi:.1f}, scaled={_BANKROLL_FRACTION*100:.0f}% of bankroll, "
        f"hard-cap=${_ABSOLUTE_CAP_USD:.0f})"
    )
    by_city = m.get("by_city") or {}
    if by_city:
        breakdown = " · ".join(
            f"{c} {b['wins']}/{b['n']}({(b.get('wr') or 0)*100:.0f}%) ${b['pnl']:+.1f}"
            for c, b in sorted(by_city.items())
        )
        lines.append(f"By city: {breakdown}")
    by_side = m.get("by_side") or {}
    if by_side:
        side_breakdown = " · ".join(
            f"{s} {b['wins']}/{b['n']}({(b.get('wr') or 0)*100:.0f}%) ${b['pnl']:+.1f}"
            for s, b in sorted(by_side.items())
        )
        lines.append(f"By side: {side_breakdown}")
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
    "compute_weather_goal_metrics", "diagnose",
    "get_mode", "set_mode",
    "pick_one_change", "close_prior_experiments",
    "run_cycle", "render_cycle",
]


if __name__ == "__main__":
    print(render_cycle(run_cycle()))
