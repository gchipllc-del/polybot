"""Kalshi live-trade executor with safety rails.

Wraps lib.kalshi_client.KalshiClient.place_order() with the safety
gates required for real-money execution. The wrapper enforces:

  1. live_mode toggle      — defaults False; only fires when explicitly
                             enabled in settings.yaml. Belt-and-
                             suspenders against accidental live trades.
  2. Min balance floor     — refuse if order would push account balance
                             below `min_balance_floor_usd` ($35 default
                             on a $50 account = 70% protected).
  3. Daily loss limit      — refuse all new orders for the rest of the
                             UTC day once realized PnL hits -$15.
  4. Max concurrent cap    — refuse if open-position count ≥ 8.
                             Prevents over-deployment.
  5. Cooldown after losses — refuse new orders for 30 min if 3+ losses
                             happened in the last hour. Slows the bot
                             when it's in a bad streak.
  6. Per-trade ceiling     — refuse if notional > max_trade_usd
                             (defaults to $1.50).
  7. Telegram alerts       — every live order (success or refusal) is
                             sent to TELEGRAM_CHAT_ID if configured.

All gates are AND-checked. Any single failure aborts the trade with an
audit-logged + telegram-notified rejection reason. Failures degrade to
paper-only behavior (the caller can still record the simulated trade
locally if they want).

State (rolling losses, last-trade timestamps, cooldown markers) is
persisted to data/kalshi_live_state.json under fcntl lock — same atomic-
write pattern as the rate-limit memory in cryptobot's crypto_llm.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from tradingcore.audit import log_event

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "kalshi_live_state.json"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
# Smoke-test marker file. is_live_enabled() requires this AND the
# settings.yaml flag both be present — so an accidental settings flip
# without a successful smoke test still keeps the bot in paper mode.
SMOKE_MARKER_PATH = ROOT / "data" / "kalshi_live_smoke_passed.marker"
# Reconciliation log — tracks orphans between local + Kalshi state
RECON_LOG_PATH = ROOT / "data" / "kalshi_live_reconciliation.jsonl"
# Shadow-trade log — every trade the bot WANTED to place but a safety
# gate refused. Settled against actual Kalshi outcomes so we can report
# "if cap were $X instead of $Y, you'd have made $Z extra/less". Used to
# decide whether the current cap is too tight or appropriately conservative.
SHADOW_LOG_PATH = ROOT / "data" / "kalshi_live_shadow_trades.jsonl"


# Default safety bounds — tuned for a $50 starting balance. Caller
# (kalshi_daily_paper) reads these via _load_live_config to apply
# YAML overrides, so users can tighten/loosen without code edits.
DEFAULTS = {
    "max_trade_usd":         1.50,
    # Bankroll-relative per-trade ceiling. When > 0, the effective per-trade
    # cap becomes min(max_trade_usd, available_balance * this pct) — so trade
    # size scales WITH the account as it grows/shrinks. `max_trade_usd` then
    # acts as an ABSOLUTE backstop (catastrophe guard against a bad balance
    # read). 0.0 (default) = disabled → pure absolute cap (legacy behavior).
    "max_trade_bankroll_pct": 0.0,
    "max_daily_loss_usd":   15.00,
    # Bankroll-relative daily-loss halt. When > 0, the effective daily-loss
    # limit becomes min(max_daily_loss_usd, balance * this pct) — so the halt
    # threshold scales WITH the account. `max_daily_loss_usd` then acts as an
    # ABSOLUTE backstop (a day can never lose more than this even if a balance
    # misread inflates the pct target). 0.0 (default) = disabled → pure
    # absolute limit (legacy behavior). Set to e.g. 0.30 to halt the day once
    # losses reach 30% of bankroll. Because per-trade sizing is also pct-based
    # (max_trade_bankroll_pct), a daily pct that is ~2× the trade pct keeps the
    # "halt after ~2 full losing trades" tolerance CONSTANT at any bankroll.
    "max_daily_loss_pct":    0.0,
    "max_concurrent":        8,
    "min_balance_floor":    35.00,
    "cooldown_minutes":     30,
    "cooldown_loss_count":  3,
    "cooldown_window_min":  60,
    # Kill switch — distinct from cooldown. Cooldown is temporary
    # (resumes after 30m). Kill switch is HARD: trading halts until
    # the marker file is manually removed by the user.
    "kill_switch_consec_losses": 5,
    "require_smoke_test":   True,
}


def _load_live_config() -> dict:
    """Read live-mode config + overrides from settings.yaml. Defaults
    win if any key is missing — keeps the bot conservative by default."""
    try:
        import yaml
        with open(SETTINGS_PATH) as f:
            s = yaml.safe_load(f) or {}
        # Two-level lookup: kalshi_daily_live: { enabled: false, max_trade_usd: 1.50, ... }
        live = (s.get("kalshi_daily_live") or {}) if isinstance(s, dict) else {}
        cfg = dict(DEFAULTS)
        for k in cfg:
            if k in live and live[k] is not None:
                cfg[k] = type(cfg[k])(live[k])
        cfg["enabled"] = bool(live.get("enabled", False))
        # Per-asset live allowlist. List of lowercase asset codes (btc/eth/sol/spy).
        # Empty/missing means "allow all enabled-in-yaml assets". Setting this
        # lets us paper-trade ETH/SOL widely while only live-trading BTC.
        raw_assets = live.get("live_assets")
        if isinstance(raw_assets, list):
            cfg["live_assets"] = [str(a).lower() for a in raw_assets if a]
        else:
            cfg["live_assets"] = []   # empty = no filter
        # 2026-05-25 PM: HARD per-asset budgets. Dict of {asset: max_notional}.
        # The asset-budget gate refuses if (sum of open positions matching this
        # asset's ticker prefix) + notional > budget. Lets the user reserve
        # capacity per strategy so they don't cannibalize each other on shared
        # balance. Missing/empty → gate is a no-op (legacy behavior).
        raw_budgets = live.get("live_asset_budgets")
        if isinstance(raw_budgets, dict):
            cfg["live_asset_budgets"] = {
                str(k).lower(): float(v)
                for k, v in raw_budgets.items()
                if v is not None
            }
        else:
            cfg["live_asset_budgets"] = {}
        # 2026-05-28 PM: bankroll-relative per-asset budgets. Dict of
        # {asset: pct}. When set for an asset, that asset's effective dollar
        # budget becomes min(live_asset_budgets[asset], balance * pct) so the
        # reservation scales with the account. Missing entry → fall back to the
        # static live_asset_budgets value (which then acts as an absolute
        # backstop). See effective_asset_budget().
        raw_budget_pct = live.get("live_asset_budget_pct")
        if isinstance(raw_budget_pct, dict):
            cfg["live_asset_budget_pct"] = {
                str(k).lower(): float(v)
                for k, v in raw_budget_pct.items()
                if v is not None
            }
        else:
            cfg["live_asset_budget_pct"] = {}
        # 2026-05-28 PM: per-asset CONCURRENT-COUNT caps. Dict of
        # {asset: max_open_positions}. The concurrent gate refuses a new trade
        # for an asset once it already holds this many open Kalshi positions
        # (Kalshi-confirmed + in-cycle commitments). Lets the user lean exposure
        # toward the better-performing strategy — e.g. {weather: 3, btc: 1}.
        # Missing/empty → no per-asset count cap (only the global max_concurrent
        # applies). This is a COUNT cap; live_asset_budgets is the $ cap — both
        # fire (defense in depth).
        raw_asset_conc = live.get("live_asset_max_concurrent")
        if isinstance(raw_asset_conc, dict):
            cfg["live_asset_max_concurrent"] = {
                str(k).lower(): int(v)
                for k, v in raw_asset_conc.items()
                if v is not None
            }
        else:
            cfg["live_asset_max_concurrent"] = {}
        return cfg
    except Exception:
        # Any failure -> live disabled (safest default)
        return {**DEFAULTS, "enabled": False, "live_assets": [],
                "live_asset_budgets": {}, "live_asset_budget_pct": {},
                "live_asset_max_concurrent": {}}


def is_live_enabled() -> bool:
    """Public accessor — used by paper modules to branch on live vs paper.

    BOTH conditions must be true:
      1. config/settings.yaml has kalshi_daily_live.enabled: true
      2. data/kalshi_live_smoke_passed.marker exists (unless
         require_smoke_test=false in settings, which is for debugging)
      3. kill switch is NOT tripped (consecutive_losses < threshold)

    Belt-and-suspenders: a settings edit alone won't make the bot go
    live until smoke_test passes. A panic kill_switch_trip stops trading
    even if settings + marker are both fine."""
    cfg = _load_live_config()
    if not cfg.get("enabled", False):
        return False
    if cfg.get("require_smoke_test", True) and not SMOKE_MARKER_PATH.exists():
        return False
    if _kill_switch_tripped(cfg):
        return False
    return True


def _kill_switch_tripped(cfg: dict) -> bool:
    """Check if either kill-switch condition is hit. Pure read, no side
    effects (the actual trip-event-log is written by the trade-outcome
    recording path)."""
    state = _load_state()
    return bool(state.get("kill_switch_tripped", False))


# ── State persistence ──────────────────────────────────────────────────

def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {"recent_losses": [], "daily_loss_by_date": {}}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"recent_losses": [], "daily_loss_by_date": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(STATE_PATH)
    except OSError:
        pass


# C2 fix (2026-06-01): the docstring claimed live-state writes were "under
# fcntl lock" but there was NONE — two launchd jobs (kalshi_daily + weather,
# both StartInterval=300) settle live losers concurrently, so an unguarded
# load→mutate→save lost updates and undercounted losses against the kill
# switch / daily-loss halt. mutate_state() does the read-modify-write inside a
# single exclusive lock window (mirrors lib/positions_store.mutate).
@contextlib.contextmanager
def _state_lock():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".lock")
    with open(lock_path, "a+") as lock_fd:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)


def mutate_state(fn) -> dict:
    """Transactional read-modify-write of the live state under one lock."""
    with _state_lock():
        state = _load_state()
        new = fn(state)
        if not isinstance(new, dict):
            new = state
        _save_state(new)
        return new


def record_outcome(*, market_ticker: str, pnl: float, opened_at: str) -> None:
    """Called when a live trade closes (won/lost). Updates daily-PnL, rolling
    losses, and the KILL SWITCH — under an exclusive state lock (C2) and
    idempotently per trade (no double-count under concurrent settles).

    H1 fix (2026-06-01): the kill switch used to key off a consecutive-loss
    counter that reset to 0 on ANY win and was fed in interleaved open-time
    order across two processes — so it never tripped (33 losses, never fired).
    It now ALSO trips on N losses within a rolling time window, which does NOT
    reset on wins and is robust to ordering/interleaving — the reachable halt.
    """
    cfg = _load_live_config()
    now = datetime.now(timezone.utc)
    day_key = now.strftime("%Y-%m-%d")
    threshold = int(cfg.get("kill_switch_consec_losses", 5))
    window_h = float(cfg.get("kill_switch_window_hours", 6))
    cutoff = now - timedelta(hours=window_h)
    dedup_key = f"{market_ticker}|{opened_at}"
    fired: dict = {}

    def _apply(state: dict) -> dict:
        # Idempotency: never count the same closed trade twice (concurrent
        # settles / re-runs would otherwise corrupt the loss counters).
        seen = state.setdefault("processed_outcomes", [])
        if dedup_key in seen:
            return state
        seen.append(dedup_key)
        state["processed_outcomes"] = seen[-500:]

        # 1. Daily PnL bucket (UTC days)
        state.setdefault("daily_loss_by_date", {}).setdefault(day_key, 0.0)
        state["daily_loss_by_date"][day_key] += float(pnl)

        # 2. Rolling-window loss tracker
        if pnl < 0:
            state.setdefault("recent_losses", []).append({
                "at": now.isoformat(),
                "market_ticker": market_ticker,
                "pnl": float(pnl),
            })
            state["recent_losses"] = state["recent_losses"][-50:]

        # 3a. Legacy consecutive counter (kept for visibility; resets on wins)
        state["consecutive_losses"] = (
            int(state.get("consecutive_losses", 0)) + 1 if pnl < 0 else 0)

        # 3b. Reachable trip: count REAL losses inside the rolling window —
        #     does NOT reset on a win, robust to settle ordering.
        window_losses = 0
        for l in state.get("recent_losses", []):
            try:
                at = datetime.fromisoformat(str(l.get("at")))
            except (TypeError, ValueError):
                continue
            if at >= cutoff:
                window_losses += 1
        state["losses_in_window"] = window_losses

        if (not state.get("kill_switch_tripped")
                and (state["consecutive_losses"] >= threshold
                     or window_losses >= threshold)):
            state["kill_switch_tripped"] = True
            state["kill_switch_tripped_at"] = now.isoformat()
            fired.update(consec=state["consecutive_losses"], window=window_losses,
                         today=state["daily_loss_by_date"][day_key])
        return state

    mutate_state(_apply)

    # Side-effects OUTSIDE the lock (never hold a flock across network I/O).
    if fired:
        log_event("kalshi_live", "KILL_SWITCH_TRIPPED", {
            "consecutive_losses": fired["consec"],
            "losses_in_window": fired["window"],
            "window_hours": window_h,
            "last_market": market_ticker,
            "today_pnl": fired["today"],
        }, result="critical")
        _send_telegram(
            f"🚨 KALSHI LIVE — KILL SWITCH TRIPPED\n"
            f"  {fired['window']} losses in {window_h:.0f}h "
            f"(consec {fired['consec']})\n"
            f"  today PnL: ${fired['today']:+.2f}\n"
            f"  ALL TRADING HALTED. Investigate before clearing."
        )


def reset_kill_switch() -> None:
    """Manual user action — clear the kill_switch_tripped flag and reset
    consecutive_losses to 0. Bot resumes trading on the next cycle.
    Exposed via `python main.py kalshi-live-reset` CLI."""
    state = _load_state()
    if state.get("kill_switch_tripped"):
        log_event("kalshi_live", "kill_switch_reset", {
            "consec_losses_before": state.get("consecutive_losses", 0),
        }, result="success")
    state["kill_switch_tripped"] = False
    state["consecutive_losses"] = 0
    state["kill_switch_tripped_at"] = ""
    _save_state(state)


# ── Early-warning monitor (2026-05-25 EOD) ──────────────────────────
# After a losing streak where we could have caught trouble at trade #2
# (20% drawdown alert) instead of trade #11 (only the kill switch). Now
# the bot self-monitors and pings Telegram on the FIRST sign of trouble.

# Thresholds — tune in settings.yaml if these fire too often or too rarely.
WARN_DEFAULTS = {
    "drawdown_alert_pct":   15.0,    # % drop from session peak triggers alert
    "consec_loss_alert":    3,       # N losses in a row triggers alert
    "rolling_window":       5,       # last N trades
    "rolling_wr_alert_pct": 20.0,    # % WR in rolling window
}


def check_warning_signals(*, balance: float) -> list[dict]:
    """Called after each newly-settled live trade. Updates session-peak
    balance + rolling-result state, then evaluates three early-warning
    rules. Returns a list of fired warnings (empty if all clear).

    Each warning fires AT MOST ONCE per session — we don't want Telegram
    spam after the user has already been notified. State tracks whether
    each warning has been delivered."""
    state = _load_state()
    warn_state = state.setdefault("warnings", {})

    # Initialize peak if missing
    if "session_peak_balance" not in state or state["session_peak_balance"] is None:
        state["session_peak_balance"] = float(balance)
    peak = float(state["session_peak_balance"])
    if balance > peak:
        peak = balance
        state["session_peak_balance"] = peak

    drawdown_pct = (peak - balance) / peak * 100 if peak > 0 else 0.0
    consec_losses = int(state.get("consecutive_losses", 0) or 0)

    # Compute rolling-window WR from the recent_losses array — we have
    # losses there but not wins. So we approximate: count losses in last
    # WARN_DEFAULTS["rolling_window"] entries since session start. A more
    # accurate version would track wins too, but for "is the bot in
    # trouble" purposes losses-only is a reasonable proxy.
    recent_losses = state.get("recent_losses", []) or []
    rolling_n = WARN_DEFAULTS["rolling_window"]
    losses_in_window = min(len(recent_losses), rolling_n)
    # Implied wins (if any) = N - losses_in_window. Not perfect, but
    # captures the "have we seen mostly losses recently" signal.
    rolling_wr_pct = max(0.0, (rolling_n - losses_in_window) / rolling_n * 100)

    fired: list[dict] = []

    # 1. Drawdown alert
    if drawdown_pct >= WARN_DEFAULTS["drawdown_alert_pct"]:
        if not warn_state.get("drawdown_alerted"):
            fired.append({
                "kind": "drawdown",
                "message": (f"⚠ Drawdown {drawdown_pct:.1f}% from session "
                            f"peak (${peak:.2f} → ${balance:.2f})"),
            })
            warn_state["drawdown_alerted"] = True

    # 2. Consecutive-loss streak alert
    if consec_losses >= WARN_DEFAULTS["consec_loss_alert"]:
        if not warn_state.get(f"consec_alerted_{consec_losses}"):
            fired.append({
                "kind": "consec_loss",
                "message": f"⚠ {consec_losses} losses in a row",
            })
            warn_state[f"consec_alerted_{consec_losses}"] = True

    # 3. Rolling-window WR alert
    if (losses_in_window >= rolling_n
        and rolling_wr_pct <= WARN_DEFAULTS["rolling_wr_alert_pct"]):
        if not warn_state.get("rolling_wr_alerted"):
            fired.append({
                "kind": "rolling_wr",
                "message": (f"⚠ Last {rolling_n} trades: "
                            f"{rolling_wr_pct:.0f}% WR (mostly losses)"),
            })
            warn_state["rolling_wr_alerted"] = True

    state["warnings"] = warn_state
    _save_state(state)

    # Send Telegram alert for each fired warning
    for w in fired:
        log_event("kalshi_live", "early_warning_fired", {
            "kind": w["kind"], "message": w["message"],
            "balance": balance, "peak": peak,
            "drawdown_pct": round(drawdown_pct, 2),
            "consec_losses": consec_losses,
            "rolling_wr_pct": round(rolling_wr_pct, 2),
        }, result="degraded")
        _send_telegram(
            f"🚨 Kalshi LIVE early warning\n"
            f"  {w['message']}\n"
            f"  balance: ${balance:.2f}  peak: ${peak:.2f}\n"
            f"  drawdown: {drawdown_pct:.1f}%  consec_losses: {consec_losses}\n"
            f"  Investigate: python main.py kalshi-live-status"
        )
    return fired


def reset_session_warnings() -> None:
    """Clear all 'alerted' flags + reset session peak. Call after the
    user has investigated and wants the monitor to re-arm. Exposed via
    `python main.py kalshi-live-reset` (alongside kill switch reset)."""
    state = _load_state()
    state["warnings"] = {}
    state["session_peak_balance"] = None   # next trade re-anchors
    _save_state(state)


# ── Pre-trade safety gates ─────────────────────────────────────────────

def _balance_check(cfg: dict, notional_usd: float,
                    committed_in_cycle: float = 0.0,
                    balance: float | None = None) -> tuple[bool, str]:
    """Refuse if placing this trade would leave balance below the floor.

    `committed_in_cycle` is notional already placed earlier in the SAME
    scan cycle but not yet reflected in Kalshi's balance. Without this,
    multiple trades within one cycle each see the same (stale) starting
    balance and over-deploy past the floor.

    `balance` may be passed in by the caller (can_open_trade fetches it once
    and shares it with the trade-size dynamic-cap check) to avoid a second
    network round-trip; if None we fetch it ourselves."""
    if balance is None:
        from lib.kalshi_client import KalshiClient
        try:
            balance = KalshiClient().get_balance()
        except Exception as e:
            return False, f"balance_check_failed:{type(e).__name__}"
    # Effective available balance = current balance minus what we've
    # already committed in this cycle (but Kalshi hasn't yet debited).
    effective_balance = balance - committed_in_cycle
    available_after = effective_balance - notional_usd
    if available_after < cfg["min_balance_floor"]:
        committed_note = (
            f" (incl ${committed_in_cycle:.2f} in-cycle commitments)"
            if committed_in_cycle > 0 else ""
        )
        return False, (
            f"would_breach_floor: balance ${balance:.2f}{committed_note} - "
            f"notional ${notional_usd:.2f} = ${available_after:.2f} < "
            f"${cfg['min_balance_floor']:.2f} floor"
        )
    return True, "ok"


def effective_daily_loss_usd(cfg: dict | None = None,
                             *, balance: float | None = None) -> float:
    """The daily-loss halt threshold (a positive dollar magnitude) in force
    right now.

      * `max_daily_loss_usd` — ABSOLUTE backstop (a day never loses more).
      * `max_daily_loss_pct` — bankroll-relative target (e.g. 0.30 = halt the
                               day once losses reach 30% of bankroll).

    Effective = min(absolute, balance * pct) when pct > 0 and a positive
    balance is known; otherwise the absolute alone. min() keeps it protective
    (halts at whichever is TIGHTER) AND lets it scale up with the account until
    the absolute backstop binds — at which point raise the backstop."""
    if cfg is None:
        cfg = _load_live_config()
    abs_backstop = float(cfg.get("max_daily_loss_usd", 15.0))
    pct = float(cfg.get("max_daily_loss_pct", 0.0) or 0.0)
    if pct > 0 and balance is not None and balance > 0:
        return round(min(abs_backstop, balance * pct), 2)
    return abs_backstop


def _daily_loss_check(cfg: dict, balance: float | None = None) -> tuple[bool, str]:
    """Refuse all new orders once today's realized losses exceed the
    daily-loss limit. Resets at UTC midnight automatically (new day_key).

    The limit may be bankroll-relative (see effective_daily_loss_usd); the
    caller (can_open_trade) threads the balance it already fetched so we don't
    do a second round-trip. If balance is None we use the absolute backstop."""
    state = _load_state()
    today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = float(state.get("daily_loss_by_date", {}).get(today_key, 0.0))
    limit = effective_daily_loss_usd(cfg, balance=balance)
    if today_pnl <= -limit:
        pct = float(cfg.get("max_daily_loss_pct", 0.0) or 0.0)
        basis = (f" ({pct*100:.0f}% of ${balance:.2f} bankroll)"
                 if pct > 0 and balance else "")
        return False, (
            f"daily_loss_limit_hit: today_pnl=${today_pnl:+.2f} <= "
            f"-${limit:.2f}{basis}"
        )
    return True, "ok"


def _concurrent_check(cfg: dict, asset: str | None = None,
                      committed_count_in_cycle: int = 0) -> tuple[bool, str]:
    """Refuse if opening this trade would breach either:
      * the GLOBAL cap (`max_concurrent` total open positions), or
      * the PER-ASSET count cap (`live_asset_max_concurrent[asset]`), e.g.
        at most 3 weather + 1 btc concurrently.

    `committed_count_in_cycle` is the number of live orders ALREADY placed
    earlier in this SAME scan cycle for THIS asset (the caller increments it
    on each success). Kalshi's positions list lags placement by ~1s, so
    without this counter multiple in-cycle trades would each see the same
    stale count and over-open. Because each caller process trades a single
    asset bucket, this per-asset count is also that process's total in-cycle
    count — so we add it to BOTH the global and the per-asset tally.

    Note the per-asset count caps STRUCTURALLY enforce the global cap across
    the separate btc/weather processes (1 + 3 = 4), which the cross-process
    Kalshi read can only approximate (lag)."""
    from lib.kalshi_client import KalshiClient
    try:
        positions = KalshiClient().get_positions()
    except Exception as e:
        return False, f"position_check_failed:{type(e).__name__}"
    # H2 fix: None = positions API unreadable → REFUSE (fail closed). Treating
    # it as [] would let the concurrent cap pass with zero apparent exposure.
    if positions is None:
        return False, "position_check_failed:positions_unreadable"

    # Global cap (Kalshi-confirmed open + this cycle's in-flight placements).
    n_total = len(positions) + int(committed_count_in_cycle)
    if n_total >= cfg["max_concurrent"]:
        cyc = (f" (+{committed_count_in_cycle} in-cycle)"
               if committed_count_in_cycle else "")
        return False, (
            f"max_concurrent_reached: {len(positions)}{cyc} >= "
            f"{cfg['max_concurrent']}"
        )

    # Per-asset count cap (defense in depth alongside the per-asset $ budget).
    asset_caps = cfg.get("live_asset_max_concurrent") or {}
    if asset and asset_caps:
        a = str(asset).lower()
        if a in asset_caps:
            cap = int(asset_caps[a])
            n_asset = sum(
                1 for p in positions if _ticker_to_asset(p.market_id) == a
            ) + int(committed_count_in_cycle)
            if n_asset >= cap:
                cyc = (f" (+{committed_count_in_cycle} in-cycle)"
                       if committed_count_in_cycle else "")
                return False, (
                    f"asset_max_concurrent_reached: {a} "
                    f"{n_asset - int(committed_count_in_cycle)}{cyc} >= {cap}"
                )
    return True, "ok"


def _cooldown_check(cfg: dict) -> tuple[bool, str]:
    """Refuse new orders if ≥N losses happened in the last `cooldown_window_min`
    minutes, until `cooldown_minutes` has passed since the last loss."""
    state = _load_state()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=cfg["cooldown_window_min"])
    cutoff_for_cooldown = now - timedelta(minutes=cfg["cooldown_minutes"])

    recent = []
    last_loss_at: Optional[datetime] = None
    for r in state.get("recent_losses", []):
        try:
            ts = datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if ts >= window_start:
            recent.append(ts)
            if last_loss_at is None or ts > last_loss_at:
                last_loss_at = ts

    if len(recent) >= cfg["cooldown_loss_count"] and last_loss_at is not None:
        if last_loss_at > cutoff_for_cooldown:
            mins_remaining = (
                last_loss_at + timedelta(minutes=cfg["cooldown_minutes"]) - now
            ).total_seconds() / 60.0
            return False, (
                f"cooldown_active: {len(recent)} losses in last "
                f"{cfg['cooldown_window_min']}m; {mins_remaining:.0f}m remaining"
            )
    return True, "ok"


def effective_max_trade_usd(cfg: dict | None = None,
                            *, available_balance: float | None = None) -> float:
    """The per-trade dollar ceiling actually in force right now.

    Two-component model:
      * `max_trade_usd`           — ABSOLUTE backstop (never exceeded).
      * `max_trade_bankroll_pct`  — bankroll-relative target (e.g. 0.15 =
                                     size each trade to 15% of available cash).

    Effective cap = min(absolute, available_balance * pct) when pct > 0 and a
    positive balance is known; otherwise the absolute cap alone. Sizing the
    cap off `available_balance` (cash net of in-cycle commitments) means it
    scales as the account grows AND shrinks naturally within a scan cycle.

    Callers (kalshi_daily_paper / weather_paper) use this to SIZE live
    contracts; the gate (`_trade_size_check`) uses the SAME formula to VET
    them — so a trade sized at the cap is never spuriously refused."""
    if cfg is None:
        cfg = _load_live_config()
    abs_ceiling = float(cfg.get("max_trade_usd", 1.50))
    pct = float(cfg.get("max_trade_bankroll_pct", 0.0) or 0.0)
    if pct > 0 and available_balance is not None and available_balance > 0:
        return round(min(abs_ceiling, available_balance * pct), 2)
    return abs_ceiling


def _trade_size_check(cfg: dict, notional_usd: float,
                      available_balance: float | None = None) -> tuple[bool, str]:
    """Hard ceiling on per-trade size. The ceiling is the EFFECTIVE cap
    (absolute backstop AND/OR bankroll-% target — see effective_max_trade_usd).
    A 1¢ epsilon absorbs float/penny rounding between the sizing-time balance
    and the gate-time balance so we don't refuse a trade we just sized."""
    cap = effective_max_trade_usd(cfg, available_balance=available_balance)
    if notional_usd > cap + 0.01:
        pct = float(cfg.get("max_trade_bankroll_pct", 0.0) or 0.0)
        basis = (f" ({pct*100:.0f}% of ${available_balance:.2f} avail)"
                 if pct > 0 and available_balance else "")
        return False, (
            f"trade_too_large: ${notional_usd:.2f} > ${cap:.2f} cap{basis}"
        )
    return True, "ok"


# Ticker-prefix → asset-code mapping. Used by the asset-budget gate to
# group open Kalshi positions by which strategy holds them. Add new
# entries here when adding a new live-eligible asset.
#
# Match is by PREFIX so we tolerate version suffixes in tickers.
_TICKER_PREFIX_TO_ASSET = {
    "KXBTCD":     "btc",       # BTC daily strike-ladder
    "KXETHD":     "eth",       # ETH daily (paper only currently)
    "KXSOLD":     "sol",       # SOL daily (paper only currently)
    "KXINX":      "spy",       # S&P 500 weekly
    # ── Daily max/min weather → SEPARATE "weather_daily" budget bucket ─
    # Must precede KXHIGH/KXTEMP below so the more-specific prefix wins the
    # insertion-ordered match. These are the daily HIGH (KXHIGHT*) and LOW
    # (KXLOWT*) markets — a distinct sleeve from the hourly KXTEMP* one, given
    # its OWN tiny live budget (config: live_asset_budgets.weather_daily).
    # NOTE: KXLOWT* previously mapped to NOTHING (→ bypassed the budget gate);
    # this closes that gap.
    "KXHIGHT":    "weather_daily",   # daily HIGH-temp (e.g. KXHIGHTATL)
    "KXLOWT":     "weather_daily",   # daily LOW-temp  (e.g. KXLOWTCHI)
    # ── Weather (all cities → "weather" budget bucket) ────────────────
    # Bugfix 2026-05-27: previously only NYC was mapped, allowing the
    # bot to pyramid trades across Chicago/DC/Boston/LA/Miami without
    # the asset-budget gate noticing existing exposure. All 6 cities now
    # share the single weather budget.
    "KXTEMPNYCH": "weather",   # NYC high-temp
    "KXTEMPNYC":  "weather",   # NYC low-temp
    "KXTEMPCHIH": "weather",   # Chicago high-temp
    "KXTEMPCHI":  "weather",   # Chicago low-temp
    "KXTEMPDCH":  "weather",   # DC high-temp
    "KXTEMPDC":   "weather",   # DC low-temp
    "KXTEMPBOSH": "weather",   # Boston high-temp
    "KXTEMPBOS":  "weather",   # Boston low-temp
    "KXTEMPLAXH": "weather",   # LA high-temp
    "KXTEMPLAX":  "weather",   # LA low-temp
    "KXTEMPMIAH": "weather",   # Miami high-temp
    "KXTEMPMIA":  "weather",   # Miami low-temp
    # Catch-all so future cities don't silently bypass the gate. Note
    # ordering: more specific prefixes must come first in the dict to
    # win the prefix match (Python dict iteration is insertion-ordered).
    "KXTEMP":     "weather",   # catch-all for any new KXTEMP* market
    "KXHIGH":     "weather",   # legacy alternate prefix
}


def _ticker_to_asset(ticker: str) -> str | None:
    """Map a Kalshi ticker to its asset code via prefix lookup. Returns
    None for unrecognized prefixes — caller should treat that as 'not
    bucketed by an asset budget'."""
    t = (ticker or "").upper()
    for prefix, asset in _TICKER_PREFIX_TO_ASSET.items():
        if t.startswith(prefix):
            return asset
    return None


def effective_asset_budget(cfg: dict, asset: str | None,
                           *, balance: float | None = None) -> float | None:
    """The dollar budget in force for `asset` right now, or None if this asset
    has no budget configured (→ unbounded by the budget gate).

      * `live_asset_budgets[asset]`      — ABSOLUTE backstop ($).
      * `live_asset_budget_pct[asset]`   — bankroll-relative target (e.g. 0.45
                                           = reserve up to 45% of bankroll).

    When a pct is set for the asset and a positive balance is known, the
    effective budget = min(absolute backstop, balance * pct) (the absolute
    caps the scaling, mirroring the per-trade model). When no pct is set, the
    static absolute is used as-is (legacy). When a pct is set but no static
    backstop exists, the pure pct target is used."""
    budgets = cfg.get("live_asset_budgets") or {}
    pcts = cfg.get("live_asset_budget_pct") or {}
    if not asset:
        return None
    a = str(asset).lower()
    static_b = budgets.get(a)
    pct = float(pcts.get(a, 0.0) or 0.0)
    if pct > 0 and balance is not None and balance > 0:
        scaled = balance * pct
        if static_b is not None:
            return round(min(float(static_b), scaled), 2)
        return round(scaled, 2)
    if static_b is not None:
        return float(static_b)
    return None


def _asset_budget_check(
    cfg: dict, asset: str | None, notional_usd: float,
    committed_in_cycle: float = 0.0, balance: float | None = None,
) -> tuple[bool, str]:
    """HARD per-asset budget gate. If user set `live_asset_budgets` (and/or
    `live_asset_budget_pct`) in settings.yaml, refuse any trade that would push
    this asset's total notional (Kalshi-confirmed open positions + in-cycle
    commitments + this proposed trade) above the EFFECTIVE budget (which may be
    bankroll-relative — see effective_asset_budget).

    No-op if:
      * No budget resolved for this asset (legacy / unbounded)
      * Asset is None (caller didn't pass asset metadata)

    `balance` is threaded from can_open_trade (fetched once) so a pct-based
    budget doesn't trigger a second round-trip."""
    if not asset:
        return True, "ok"
    asset = str(asset).lower()
    budget = effective_asset_budget(cfg, asset, balance=balance)
    if budget is None:
        return True, "ok"

    # Sum existing exposure on Kalshi for tickers belonging to this asset.
    # avg_price × quantity = notional we committed (per-contract paid).
    try:
        from lib.kalshi_client import KalshiClient
        positions = KalshiClient().get_positions()
    except Exception as e:
        return False, f"asset_budget_check_failed:{type(e).__name__}"
    # H2 fix: None = positions unreadable → REFUSE (fail closed), else the
    # per-asset $ budget would pass with zero apparent exposure.
    if positions is None:
        return False, "asset_budget_check_failed:positions_unreadable"

    asset_open_notional = 0.0
    for p in positions:
        ticker_asset = _ticker_to_asset(p.market_id)
        if ticker_asset == asset:
            asset_open_notional += float(p.avg_price or 0) * float(p.quantity or 0)

    proposed_total = asset_open_notional + committed_in_cycle + notional_usd
    if proposed_total > budget:
        return False, (
            f"asset_budget_exceeded: {asset} "
            f"open=${asset_open_notional:.2f} + cycle=${committed_in_cycle:.2f} "
            f"+ new=${notional_usd:.2f} = ${proposed_total:.2f} > "
            f"${budget:.2f} budget"
        )
    return True, "ok"


def can_open_trade(*, notional_usd: float,
                    committed_in_cycle: float = 0.0,
                    asset: str | None = None,
                    committed_count_in_cycle: int = 0) -> tuple[bool, str, dict]:
    """Run all safety gates. Returns (allowed, reason_if_not, config).

    AND-checked: a single failure aborts. `committed_in_cycle` is the
    cumulative notional already placed earlier in the same scan cycle
    (Kalshi balance lags by ~1s per order, so without this multiple
    in-cycle trades see the same starting balance and over-deploy).

    `committed_count_in_cycle` is the same idea for POSITION COUNT — how many
    live orders this asset has already placed this cycle — so the per-asset
    concurrent-count cap holds even before Kalshi's positions list catches up.

    `asset` is required for the per-asset-budget AND per-asset-count gates to
    fire — callers that don't pass it bypass those gates (legacy preserved)."""
    cfg = _load_live_config()
    if not cfg["enabled"]:
        return False, "live_mode_disabled_in_settings_yaml", cfg
    # Defense-in-depth: the kill switch is also enforced via is_live_enabled()
    # (which both live callers gate on), but check it HERE too so this single
    # function is a complete safety boundary — no future caller can place a
    # live order while the kill switch is tripped by bypassing is_live_enabled.
    if _kill_switch_tripped(cfg):
        return False, "kill_switch_tripped", cfg
    # Fetch balance ONCE up front. Two gates need it: the dynamic per-trade
    # cap (trade_size, when max_trade_bankroll_pct is set) and the floor
    # (balance). Sharing one fetch keeps them consistent AND avoids a second
    # round-trip. Fail CLOSED — if balance can't be read, refuse the trade
    # (this is real money; better to skip than to size off a guess).
    balance: float | None = None
    from lib.kalshi_client import KalshiClient
    try:
        balance = KalshiClient().get_balance()
    except Exception as e:
        return False, f"balance:balance_fetch_failed:{type(e).__name__}", cfg
    available = balance - committed_in_cycle
    # Gate order matters for diagnostic value: cheap gates first.
    # Balance gate threads committed_in_cycle so concurrent trades stack.
    # asset_budget runs LAST among external calls because it queries Kalshi
    # positions (network round-trip) — cheap fails first.
    for check_fn, name in [
        (lambda: _trade_size_check(cfg, notional_usd, available_balance=available), "trade_size"),
        (lambda: _daily_loss_check(cfg, balance=balance),                         "daily_loss"),
        (lambda: _cooldown_check(cfg),                                            "cooldown"),
        (lambda: _balance_check(cfg, notional_usd, committed_in_cycle, balance=balance), "balance"),
        (lambda: _concurrent_check(cfg, asset, committed_count_in_cycle),         "concurrent"),
        (lambda: _asset_budget_check(cfg, asset, notional_usd, committed_in_cycle, balance=balance), "asset_budget"),
    ]:
        ok, reason = check_fn()
        if not ok:
            return False, f"{name}:{reason}", cfg
    return True, "ok", cfg


# ── H3 fix (2026-06-01): persistent 24h duplicate guard for the LIVE path ──
# The live Kalshi sleeves bypassed order_gate, so the 24h (platform,market,side)
# dedup added after the -$148.52 Hormuz duplicate did NOT protect real orders
# (every place_order used a fresh uuid client_order_id → Kalshi won't dedup
# identical-intent cycles). These helpers reuse order_gate's EXACT store + key
# format, so the live path and the order_gate path dedup against each other.
def _live_dedup_key(market_ticker: str, side: str) -> str:
    return f"kalshi:{market_ticker}:{side}"


def _live_recent_duplicate(market_ticker: str, side: str) -> bool:
    """True if an identical (market, side) live intent fired within 24h."""
    try:
        import time
        from lib.order_gate import _load_dedup, PERSISTENT_DUPLICATE_WINDOW_SECONDS
        ts = _load_dedup().get(_live_dedup_key(market_ticker, side))
        return ts is not None and (time.time() - float(ts)) < PERSISTENT_DUPLICATE_WINDOW_SECONDS
    except Exception:
        return False   # dedup-store read error must not permanently block trading


def _record_live_intent(market_ticker: str, side: str) -> None:
    """Stamp this (market, side) into the shared 24h dedup store on placement."""
    try:
        import time
        from lib.order_gate import _load_dedup, _save_dedup, PERSISTENT_DUPLICATE_WINDOW_SECONDS
        d = _load_dedup()
        now = time.time()
        d[_live_dedup_key(market_ticker, side)] = now
        # prune expired keys so the file doesn't grow unbounded
        d = {k: v for k, v in d.items()
             if now - float(v) < PERSISTENT_DUPLICATE_WINDOW_SECONDS}
        _save_dedup(d)
    except Exception:
        pass


# ── Alert helpers: local file + best-effort Telegram ──────────────────

LIVE_ALERTS_PATH = ROOT / "logs" / "live_alerts.log"


def _local_alert(message: str) -> None:
    """Append a human-readable alert to logs/live_alerts.log with a UTC
    timestamp. This is the canonical local notification surface — works
    even when Telegram isn't configured. Tail it with:
        tail -f logs/live_alerts.log
    or via the CLI:
        python main.py live-tail
    """
    try:
        LIVE_ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        # Border helps `tail` output look readable when alerts span lines
        with open(LIVE_ALERTS_PATH, "a") as f:
            f.write(f"\n[{ts}]\n{message}\n")
            f.flush()
    except OSError:
        pass


def _send_telegram(message: str) -> None:
    """Send an alert. ALWAYS writes to the local alert log; tries Telegram
    too if env vars are set. The local log is the canonical notification
    surface — Telegram is a bonus. No exception escapes; alerts must
    never block trading."""
    # Local file write first — always-on, doesn't depend on env.
    _local_alert(message)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip().strip('"')
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip().strip('"')
    if not token or not chat_id:
        return   # No telegram configured — file write was enough.
    try:
        import urllib.request
        data = json.dumps({
            "chat_id": chat_id,
            "text": message[:4000],   # Telegram limit
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


# ── Main entry point: place a live order ──────────────────────────────

def place_live_order(*,
    market_ticker: str,
    side: Literal["YES", "NO"],
    fill_price: float,
    contracts: int,
    metadata: Optional[dict] = None,
    committed_in_cycle: float = 0.0,
    committed_count_in_cycle: int = 0,
) -> Optional[dict]:
    """Place a live order on Kalshi if all safety gates pass.

    Returns:
      dict with order details on success
      None on gated refusal (logged + telegram-notified + shadow-tracked)

    Never raises — exceptions are caught, logged, and reported via
    telegram. Caller can treat None as "trade not placed; record paper
    only" without worrying about partial state.

    `committed_in_cycle` is the cumulative notional already placed
    earlier in the SAME scan cycle. The caller maintains this running
    total and increments it on each successful placement; without it
    multiple in-cycle trades see the same starting balance and could
    over-deploy past the floor.

    Shadow tracking: every refused trade is written to SHADOW_LOG_PATH
    with full details (strike, close_time, fill, intended contracts).
    A later settle pass scores them against actual Kalshi outcomes,
    revealing the opportunity cost of the current safety bounds."""
    notional_usd = round(fill_price * contracts, 4)
    metadata = metadata or {}

    # Asset-allowlist gate — runs BEFORE the standard safety gates so
    # the refusal reason is clearly attributed to "asset not approved
    # for live trading" rather than getting buried in a generic check.
    # If live_assets is empty, no filter (any enabled asset trades live).
    cfg_pre = _load_live_config()
    allowed_assets = cfg_pre.get("live_assets") or []
    asset = str(metadata.get("asset", "") or "").lower()
    if allowed_assets and asset and asset not in allowed_assets:
        log_event("kalshi_live", "trade_refused", {
            "market_ticker": market_ticker, "side": side,
            "notional_usd": notional_usd,
            "reason": f"asset_not_live_allowed: {asset} not in {allowed_assets}",
        }, result="degraded")
        # Skip shadow-tracking these — we don't want to track ETH refusals
        # as "missed P&L" when we deliberately chose not to live-trade ETH.
        return None

    allowed, reason, cfg = can_open_trade(
        notional_usd=notional_usd,
        committed_in_cycle=committed_in_cycle,
        asset=asset,   # threads to per-asset budget + count gates
        committed_count_in_cycle=committed_count_in_cycle,
    )
    if not allowed:
        log_event("kalshi_live", "trade_refused", {
            "market_ticker": market_ticker, "side": side,
            "notional_usd": notional_usd, "reason": reason,
        }, result="degraded")
        # Record shadow trade for later settlement-vs-actual comparison.
        # Skip the live_mode_disabled case — that's the default state and
        # would flood the log every cycle while live is off.
        if not reason.startswith("live_mode_disabled"):
            _record_shadow_trade(
                market_ticker=market_ticker, side=side, fill_price=fill_price,
                contracts=contracts, notional_usd=notional_usd,
                refused_reason=reason, metadata=metadata,
            )
            _send_telegram(
                f"⛔ Kalshi LIVE order BLOCKED\n"
                f"  {market_ticker} {side} {contracts}@${fill_price}\n"
                f"  notional: ${notional_usd:.2f}\n"
                f"  reason: {reason}\n"
                f"  (tracked in shadow log — settle later to see opportunity cost)"
            )
        return None

    # H3: persistent 24h duplicate guard (shared with order_gate). Blocks a
    # repeated identical-intent cycle (same market+side) from double-firing
    # real money even after a restart — the live path's missing dedup.
    if _live_recent_duplicate(market_ticker, side):
        log_event("kalshi_live", "trade_refused", {
            "market_ticker": market_ticker, "side": side,
            "notional_usd": notional_usd, "reason": "duplicate_24h",
        }, result="degraded")
        return None

    # All gates passed — attempt the real order.
    try:
        from lib.kalshi_client import KalshiClient
        result = KalshiClient().place_order(
            market_id=market_ticker,
            side=side,
            price=fill_price,
            quantity=contracts,
            order_type="limit",
            action="buy",
        )
    except Exception as e:
        log_event("kalshi_live", "place_order_exception", {
            "market_ticker": market_ticker, "side": side,
            "notional_usd": notional_usd, "error": str(e)[:200],
        }, result="failed")
        _send_telegram(
            f"🔴 Kalshi LIVE order FAILED\n"
            f"  {market_ticker} {side} {contracts}@${fill_price}\n"
            f"  error: {type(e).__name__}: {str(e)[:200]}"
        )
        return None

    # Order accepted by Kalshi — stamp the 24h dedup so a repeat cycle can't
    # re-fire this same (market, side) intent. (H3)
    _record_live_intent(market_ticker, side)

    # ── Fill capture (2026-05-28) ────────────────────────────────────
    # place_order returns Kalshi's immediate order state. A limit buy
    # that crosses the spread fills right away (filled_count == qty); a
    # limit buy resting at/under the offer fills 0 (or partially). We
    # were previously KEEPING ONLY order_id and assuming every order
    # filled in full — fine for liquid near-the-money markets, but the
    # whole premise of the longshot test is that CHEAP limit buys
    # (5-15c) often DON'T fill on thin books. Record requested-vs-actual
    # so real fill rates are measurable. NOTE: this is the IMMEDIATE
    # fill; an order that rests and fills seconds later shows 0 here and
    # is reconciled by reconcile_positions(). getattr defaults degrade
    # safely to the legacy "assume full" if the client omits a field.
    order_status = str(getattr(result, "status", "") or "unknown")
    _fq = getattr(result, "filled_quantity", None)
    filled_qty = int(_fq) if _fq is not None else int(contracts)
    fill_ratio = round(filled_qty / contracts, 4) if contracts else 0.0
    fully_filled = filled_qty >= contracts

    log_event("kalshi_live", "live_order_placed", {
        "market_ticker": market_ticker, "side": side,
        "fill_price": fill_price,
        "contracts": contracts,             # requested
        "filled_quantity": filled_qty,      # actual (Kalshi filled_count)
        "fill_ratio": fill_ratio,
        "order_status": order_status,
        "notional_usd": notional_usd,
        "order_id": getattr(result, "order_id", None),
    }, result="success")

    if not fully_filled:
        # The exact signal the longshot test is built to surface: a
        # cheap limit buy that did not (fully) cross the book.
        log_event("kalshi_live", "partial_or_no_fill", {
            "market_ticker": market_ticker, "side": side,
            "fill_price": fill_price,
            "requested_contracts": contracts,
            "filled_quantity": filled_qty,
            "fill_ratio": fill_ratio,
            "order_status": order_status,
        }, result="degraded")

    fill_note = (
        "" if fully_filled
        else f"\n  ⚠ only {filled_qty}/{contracts} filled "
             f"({fill_ratio:.0%}) — status={order_status}"
    )
    _send_telegram(
        f"✅ Kalshi LIVE order PLACED\n"
        f"  {market_ticker} {side} {contracts}@${fill_price}\n"
        f"  notional: ${notional_usd:.2f}\n"
        f"  order_id: {getattr(result, 'order_id', '?')}"
        f"{fill_note}"
    )

    return {
        "order_id": getattr(result, "order_id", ""),
        "side": side,
        "fill_price": fill_price,
        "contracts": contracts,             # requested (unchanged — callers rely on this)
        "filled_quantity": filled_qty,      # actual immediate fill (Kalshi filled_count)
        "fill_ratio": fill_ratio,
        "order_status": order_status,
        "notional_usd": notional_usd,             # requested notional (fill_price × requested)
        "filled_notional_usd": round(fill_price * filled_qty, 4),  # actual committed (fill_price × filled)
        "metadata": metadata or {},
    }


def get_current_safety_status() -> dict:
    """Snapshot — what would happen if a $1.50 trade were attempted now.
    Used by the CLI and dashboard so the user can see current state."""
    cfg = _load_live_config()
    out = {"config": cfg, "checks": {}, "live_enabled": cfg["enabled"]}
    # Always populate today's realized PnL — even when live is disabled
    # the user wants to see today's running tally on the dashboard.
    # Previously this only got set on the live-enabled path → tile showed
    # $0 after a halt, hiding ongoing settle PnL from in-flight positions.
    state = _load_state()
    today_key_pre = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out["today_pnl"] = round(
        state.get("daily_loss_by_date", {}).get(today_key_pre, 0.0), 2
    )
    if not cfg["enabled"]:
        out["overall_allowed"] = False
        out["overall_reason"] = "live_mode_disabled_in_settings_yaml"
        # Also try to fetch broker balance so the user can verify the
        # halted account hasn't drifted unexpectedly.
        try:
            from lib.kalshi_client import KalshiClient
            out["account_balance"] = KalshiClient().get_balance()
            out["live_positions_count"] = len(KalshiClient().get_positions() or [])
        except Exception as e:
            out["account_balance"] = None
            out["live_positions_count"] = None
            out["query_error"] = str(e)[:200]
        return out

    # Fetch broker state ONCE up front so the dynamic per-trade cap and the
    # gate checks below all reason off the same balance snapshot.
    bal: float | None = None
    try:
        from lib.kalshi_client import KalshiClient
        _client = KalshiClient()
        bal = _client.get_balance()
        out["account_balance"] = bal
        _positions = _client.get_positions() or []
        out["live_positions_count"] = len(_positions)
        # Per-asset open counts — lets the CLI/dashboard show current vs cap
        # (e.g. "btc 1/1, weather 0/3") so a maxed asset is obvious.
        _asset_counts: dict = {}
        for _p in _positions:
            _a = _ticker_to_asset(getattr(_p, "market_id", "") or "")
            if _a:
                _asset_counts[_a] = _asset_counts.get(_a, 0) + 1
        out["live_positions_by_asset"] = _asset_counts
    except Exception as e:
        out["account_balance"] = None
        out["live_positions_count"] = None
        out["live_positions_by_asset"] = {}
        out["query_error"] = str(e)[:200]

    # Effective per-trade ceiling in force right now (absolute backstop
    # and/or bankroll-% target). Surfaced so the CLI/dashboard show the
    # ACTUAL cap a trade would be sized to, not just the static yaml value.
    eff_cap = effective_max_trade_usd(cfg, available_balance=bal)
    out["effective_max_trade_usd"] = eff_cap
    out["max_trade_bankroll_pct"] = float(cfg.get("max_trade_bankroll_pct", 0.0) or 0.0)

    # Effective daily-loss halt (may be bankroll-relative). Surface both the
    # dollar value in force and the pct so the CLI/dashboard show reality.
    out["effective_daily_loss_usd"] = effective_daily_loss_usd(cfg, balance=bal)
    out["max_daily_loss_pct"] = float(cfg.get("max_daily_loss_pct", 0.0) or 0.0)

    # Effective per-asset budgets ($, possibly bankroll-scaled) + count caps.
    out["live_asset_max_concurrent"] = dict(cfg.get("live_asset_max_concurrent") or {})
    _budget_assets = set(cfg.get("live_asset_budgets") or {}) | set(cfg.get("live_asset_budget_pct") or {})
    out["effective_asset_budgets"] = {
        a: effective_asset_budget(cfg, a, balance=bal) for a in sorted(_budget_assets)
    }

    # Try each gate independently for a complete diagnostic snapshot. Test a
    # hypothetical trade AT the effective cap (so trade_size reflects reality).
    notional_test = eff_cap
    checks = [
        ("trade_size", _trade_size_check(cfg, notional_test, available_balance=bal)),
        ("daily_loss", _daily_loss_check(cfg, balance=bal)),
        ("cooldown",   _cooldown_check(cfg)),
        ("balance",    _balance_check(cfg, notional_test, balance=bal)),
        ("concurrent", _concurrent_check(cfg)),
    ]
    for name, (ok, reason) in checks:
        out["checks"][name] = {"ok": ok, "reason": reason}
    out["overall_allowed"] = all(ok for _, (ok, _) in checks)
    out["overall_reason"] = (
        "ok" if out["overall_allowed"]
        else next((f"{n}: {r}" for n, (ok, r) in checks if not ok), "?")
    )
    state = _load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out["today_pnl"] = round(state.get("daily_loss_by_date", {}).get(today, 0.0), 2)
    return out


# ── Reconciliation: sync local positions with Kalshi truth ────────────

def reconcile_live_fill(trade: dict) -> dict:
    """Correct a LIVE trade's recorded fill to Kalshi's TRUE final fill.

    Mutates `trade` in place (and returns it). For an is_live trade with a
    live_order_id, queries get_order and, if the recorded live_contracts
    disagrees with the order's actual fill_count, rewrites:
      • live_contracts    -> true filled quantity
      • live_notional_usd  -> true cost paid (taker + maker fill cost)
      • fill_price         -> true average per-contract cost

    WHY: place_live_order records only the IMMEDIATE (taker) fill from the
    synchronous order response. A limit order that doesn't cross immediately
    rests and may fill later as a MAKER order — which shows 0 in that initial
    response. Without this correction the trade settles as no_fill / $0 even
    though real contracts filled and real money is at stake. On 2026-05-29
    this hid 5 fully-filled weather NO bets that lost -$57.62. At settlement
    the order is final, so this is the authoritative correction point.

    Strictly read-only against Kalshi (get_order). On any failure the recorded
    values are left untouched and `reconcile_fill_status` records why, so the
    caller can settle on best-available data rather than crash."""
    if not trade.get("is_live"):
        return trade
    oid = trade.get("live_order_id")
    if not oid:
        trade["reconcile_fill_status"] = "no_order_id"
        return trade
    try:
        from lib.kalshi_client import KalshiClient
        od = KalshiClient().get_order(oid)
    except Exception:
        od = None
    if not od:
        trade["reconcile_fill_status"] = "query_failed"
        return trade
    try:
        true_fill = int(float(od.get("fill_count_fp", 0) or 0))
    except (TypeError, ValueError):
        trade["reconcile_fill_status"] = "no_fill_field"
        return trade
    recorded = int(float(trade.get("live_contracts") or 0))
    if true_fill == recorded:
        trade["reconcile_fill_status"] = "ok"
        return trade

    def _d(key: str) -> float:
        try:
            return float(od.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    true_cost = round(_d("taker_fill_cost_dollars") + _d("maker_fill_cost_dollars"), 4)
    trade["live_contracts"] = true_fill
    trade["live_notional_usd"] = true_cost
    if true_fill > 0:
        trade["fill_price"] = round(true_cost / true_fill, 4)
    trade["reconcile_fill_status"] = f"corrected:{recorded}->{true_fill}"
    log_event("kalshi_live", "live_fill_reconciled", {
        "market_ticker": trade.get("market_ticker"),
        "order_id": oid,
        "recorded_contracts": recorded,
        "true_contracts": true_fill,
        "true_notional_usd": true_cost,
    }, result="warning")
    return trade


def reconcile_positions() -> dict:
    """Walk the local paper ledgers (daily + weather jsonl) against the
    Kalshi positions API; flag any mismatch. Run after every settle cycle.

    Three categories:
      * matched      — local says open, Kalshi says open (✓)
      * orphan_local — local says open, Kalshi shows nothing.
                       Bot thinks it has a trade that doesn't actually exist.
                       Possible causes: order placement was logged but the
                       trade failed silently; or Kalshi already auto-settled
                       at expiration without us recording the close.
      * orphan_remote — Kalshi shows position, local has no record.
                        Manual order placed via the web UI; we shouldn't
                        touch it (it's the user's, not the bot's).
    """
    from lib.kalshi_client import KalshiClient
    try:
        kalshi_positions = KalshiClient().get_positions() or []
    except Exception as e:
        return {"error": f"kalshi_query_failed:{type(e).__name__}",
                "matched": 0, "orphan_local": 0, "orphan_remote": 0}

    kalshi_by_ticker = {p.market_id: p for p in kalshi_positions}

    # Load local OPEN paper-trades that were placed LIVE — from BOTH the
    # daily ledger AND the weather ledger. Both share the fields reconcile
    # reads (market_ticker / status / is_live), and their tickers never
    # collide (KXBTCD-/KXSPYD-/KXETHD-… vs KXTEMP*/KXHIGH*/KXLOW*), so a
    # single ticker→trade map stays unambiguous. Previously only the daily
    # ledger was walked, so a resting weather order that filled after we
    # recorded its 0 immediate fill was invisible to drift detection.
    paper_logs = [
        ROOT / "data" / "kalshi_daily_paper.jsonl",
        ROOT / "data" / "weather_paper.jsonl",
    ]
    local_live_open: list[dict] = []
    for paper_log in paper_logs:
        if not paper_log.exists():
            continue
        try:
            with open(paper_log) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if t.get("status") == "open" and t.get("is_live"):
                        local_live_open.append(t)
        except OSError:
            pass
    local_by_ticker = {t["market_ticker"]: t for t in local_live_open}

    matched = []
    orphan_local = []
    orphan_remote = []
    for ticker, t in local_by_ticker.items():
        if ticker in kalshi_by_ticker:
            matched.append(ticker)
        else:
            orphan_local.append({
                "ticker": ticker,
                "trade_id": t.get("trade_id"),
                "live_order_id": t.get("live_order_id"),
                "live_notional_usd": t.get("live_notional_usd"),
                "opened_at": t.get("opened_at"),
            })
    for ticker, p in kalshi_by_ticker.items():
        if ticker not in local_by_ticker:
            orphan_remote.append({
                "ticker": ticker, "side": p.side, "quantity": p.quantity,
                "avg_price": p.avg_price,
            })

    summary = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "matched": len(matched),
        "orphan_local": len(orphan_local),
        "orphan_remote": len(orphan_remote),
        "orphan_local_details": orphan_local[:10],   # cap log size
        "orphan_remote_details": orphan_remote[:10],
    }
    # Persist for audit history
    try:
        RECON_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(RECON_LOG_PATH, "a") as f:
            f.write(json.dumps(summary) + "\n")
    except OSError:
        pass
    # Alert if any orphans found
    if orphan_local or orphan_remote:
        _send_telegram(
            f"⚠ Kalshi reconciliation drift detected\n"
            f"  orphan_local:  {len(orphan_local)} (bot thinks it's open, Kalshi shows nothing)\n"
            f"  orphan_remote: {len(orphan_remote)} (Kalshi has positions bot doesn't track)\n"
            f"  matched: {len(matched)}"
        )
    return summary


# ── Smoke test: validate live execution end-to-end ────────────────────

def run_smoke_test() -> dict:
    """End-to-end live-mode validation. Writes the smoke-passed marker
    on success so is_live_enabled() will return True. Steps:

      1. Verify private key loaded + balance fetch returns sane number
      2. Verify can_open_trade() passes all gates against a hypothetical
         max_trade trade
      3. Place a tiny SELL order at $0.99 (which won't fill — far from
         actual book prices for the same reason a $99 stock won't sell
         for $0.01) on a real market, then immediately cancel.

    Returns dict with `passed: bool` + per-step details. The cancel-
    immediately pattern means we never actually transact, but we
    exercise the full auth + order placement + cancellation code path."""
    from lib.kalshi_client import KalshiClient
    out: dict = {"steps": [], "passed": False}

    # Step 1: balance fetch
    try:
        client = KalshiClient()
        balance = client.get_balance()
        if balance <= 0:
            out["steps"].append({"step": "balance_fetch", "ok": False,
                                  "detail": f"balance=${balance}"})
            return out
        out["steps"].append({"step": "balance_fetch", "ok": True,
                              "detail": f"balance=${balance:.2f}"})
        out["balance"] = balance
    except Exception as e:
        out["steps"].append({"step": "balance_fetch", "ok": False,
                              "detail": f"{type(e).__name__}: {e}"})
        return out

    # Step 2: positions fetch
    try:
        positions = client.get_positions() or []
        out["steps"].append({"step": "positions_fetch", "ok": True,
                              "detail": f"{len(positions)} positions"})
    except Exception as e:
        out["steps"].append({"step": "positions_fetch", "ok": False,
                              "detail": f"{type(e).__name__}: {e}"})
        return out

    # Step 3: place-and-cancel cycle. Pick any open BTC daily market;
    # place a buy at $0.01 (won't fill — Kalshi book never quotes that low
    # for active markets), then cancel.
    try:
        from lib.kalshi_daily_signal import enabled_assets, sample_signals_for_asset
        # Try BTC first; it's most reliable.
        btc_cfg = enabled_assets().get("btc")
        if not btc_cfg:
            out["steps"].append({"step": "order_cycle", "ok": False,
                                  "detail": "no btc samples to test against"})
            return out
        samples = sample_signals_for_asset("btc", btc_cfg)
        if not samples:
            out["steps"].append({"step": "order_cycle", "ok": False,
                                  "detail": "no btc samples returned"})
            return out
        target = samples[0]
        ticker = target.market_ticker

        # Place a YES buy at 1¢ — far from market, won't fill
        order = client.place_order(
            market_id=ticker, side="YES", price=0.01, quantity=1,
            order_type="limit", action="buy",
        )
        order_id = order.order_id
        out["steps"].append({"step": "order_placed", "ok": True,
                              "detail": f"id={order_id} on {ticker} @1¢"})

        # Immediately cancel
        cancelled = client.cancel_order(order_id) if order_id else False
        out["steps"].append({"step": "order_cancelled", "ok": cancelled,
                              "detail": f"cancel={cancelled}"})

        if not cancelled:
            # CRITICAL: cancel failed. The order might fill (unlikely
            # at 1¢ but possible). Best to alert + return.
            _send_telegram(
                f"⚠ Kalshi SMOKE TEST: cancel failed for {order_id} on {ticker}\n"
                f"  Order placed at 1¢; check manually if it filled."
            )
            return out

    except Exception as e:
        out["steps"].append({"step": "order_cycle", "ok": False,
                              "detail": f"{type(e).__name__}: {str(e)[:200]}"})
        return out

    # All steps passed
    out["passed"] = True
    out["passed_at"] = datetime.now(timezone.utc).isoformat()

    # Write the marker file so is_live_enabled() will return True
    try:
        SMOKE_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SMOKE_MARKER_PATH, "w") as f:
            json.dump(out, f, indent=2, default=str)
    except OSError as e:
        out["steps"].append({"step": "marker_write", "ok": False,
                              "detail": str(e)})
        out["passed"] = False
        return out

    log_event("kalshi_live", "smoke_test_passed", {
        "balance": out["balance"], "marker": str(SMOKE_MARKER_PATH),
    }, result="success")
    _send_telegram(
        f"✅ Kalshi LIVE smoke test PASSED\n"
        f"  balance: ${out['balance']:.2f}\n"
        f"  all auth + order paths verified\n"
        f"  live trading will engage on next signal cycle"
    )
    return out



# ── Shadow trading: track what WOULD have happened if caps were higher ─

def _record_shadow_trade(*,
    market_ticker: str, side: str, fill_price: float,
    contracts: int, notional_usd: float, refused_reason: str,
    metadata: dict,
) -> None:
    """Append a refused trade to the shadow log so a later settle pass
    can compute the missed P&L. Captures EVERYTHING needed to (a) hit
    Kalshi's market endpoint at close_time and (b) compute hypothetical
    P&L at the original notional + at scaled-up notionals."""
    record = {
        "shadow_id":        f"{market_ticker[:24]}_{int(datetime.now(timezone.utc).timestamp())}",
        "market_ticker":    market_ticker,
        "side":             side,
        "fill_price":       float(fill_price),
        "contracts":        int(contracts),
        "notional_usd":     float(notional_usd),
        "refused_reason":   refused_reason,
        "refused_at":       datetime.now(timezone.utc).isoformat(),
        # Settlement-relevant metadata from the caller (when provided):
        # strike + close_time let us compute actual outcome without
        # re-pulling all the market data. p_win is recorded for
        # post-hoc calibration of the signal itself.
        "strike":           metadata.get("strike"),
        "close_time":       metadata.get("close_time", ""),
        "spot_at_signal":   metadata.get("spot"),
        "p_win_estimated":  metadata.get("p_win"),
        "kelly_fraction":   metadata.get("kelly_fraction"),
        # Settlement fields (filled when settle_shadow_trades runs)
        "status":           "shadow_open",
        "actual_outcome":   None,        # "yes" / "no" / None
        "shadow_pnl":       None,        # what we WOULD have made at notional_usd
        "settled_at":       "",
    }
    try:
        SHADOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SHADOW_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        log_event("kalshi_live", "shadow_record_failed",
                  {"market_ticker": market_ticker, "error": str(e)[:200]},
                  result="degraded")


def _load_shadow_trades() -> list[dict]:
    if not SHADOW_LOG_PATH.exists():
        return []
    out = []
    try:
        with open(SHADOW_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def _save_shadow_trades(records: list[dict]) -> None:
    """Atomic-rewrite the whole shadow log. Used after settle pass."""
    if not records:
        return
    SHADOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SHADOW_LOG_PATH.with_suffix(SHADOW_LOG_PATH.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(SHADOW_LOG_PATH)
    except OSError:
        pass


def settle_shadow_trades() -> dict:
    """Walk every shadow_open trade past its close_time; pull Kalshi's
    actual result; compute what we WOULD have made at the recorded
    contracts/fill. Updates records in place + returns summary."""
    import requests
    KALSHI_PROFIT_FEE = 0.07  # mirror the live fee math

    records = _load_shadow_trades()
    if not records:
        return {"checked": 0, "settled_now": 0}

    now = datetime.now(timezone.utc)
    settled_now = 0
    for r in records:
        if r.get("status") != "shadow_open":
            continue
        close_iso = r.get("close_time", "")
        if not close_iso:
            # No close_time means we can't settle; flag for review.
            r["status"] = "needs_review_no_close_time"
            continue
        try:
            ct = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        except ValueError:
            r["status"] = "needs_review_bad_close_time"
            continue
        if now < ct:
            continue  # not yet expired

        # Pull Kalshi's actual market resolution. The public /markets
        # endpoint returns the `result` field once a market resolves.
        ticker = r.get("market_ticker", "")
        try:
            resp = requests.get(
                f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}",
                timeout=10,
            )
            data = resp.json() if resp.text else {}
        except Exception:
            continue
        market = data.get("market") if isinstance(data, dict) else None
        if not isinstance(market, dict):
            continue
        result = (market.get("result") or "").lower()
        if result not in ("yes", "no"):
            # Still unresolved or voided
            if (now - ct).total_seconds() > 3 * 86400:
                r["status"] = "void_or_unresolved"
            continue

        # Compute what we WOULD have made
        side = str(r.get("side", "YES")).upper()
        we_won = (side == "YES" and result == "yes") or \
                 (side == "NO"  and result == "no")
        fill = float(r.get("fill_price", 0))
        size = float(r.get("contracts", 0))
        if we_won:
            gross = size * (1 - fill)
            shadow_pnl = round(gross * (1 - KALSHI_PROFIT_FEE), 4)
            r["status"] = "shadow_won"
        else:
            shadow_pnl = round(-fill * size, 4)
            r["status"] = "shadow_lost"
        r["actual_outcome"] = result
        r["shadow_pnl"] = shadow_pnl
        r["settled_at"] = now.isoformat()
        settled_now += 1

    if settled_now:
        _save_shadow_trades(records)
        log_event("kalshi_live", "shadow_settled_batch",
                  {"settled_now": settled_now,
                   "total_records": len(records)})
    return {"checked": len(records), "settled_now": settled_now}


def shadow_summary(scale_caps: Optional[list[float]] = None) -> dict:
    """Aggregate shadow-trade outcomes. Optionally shows what the P&L
    would look like at DIFFERENT cap sizes — caller passes scale_caps
    like [1.5, 3.0, 5.0, 10.0] to see how cap size affects opportunity
    cost. Each scale multiplies the recorded notional/contracts."""
    records = _load_shadow_trades()
    settled = [r for r in records if r.get("status") in ("shadow_won", "shadow_lost")]
    pending = [r for r in records if r.get("status") == "shadow_open"]

    by_reason: dict = {}
    for r in records:
        reason_class = (r.get("refused_reason") or "").split(":")[0]
        by_reason.setdefault(reason_class, {"count": 0, "settled_pnl": 0.0,
                                             "would_win": 0, "would_lose": 0})
        by_reason[reason_class]["count"] += 1
        if r.get("status") == "shadow_won":
            by_reason[reason_class]["settled_pnl"] += float(r.get("shadow_pnl", 0))
            by_reason[reason_class]["would_win"] += 1
        elif r.get("status") == "shadow_lost":
            by_reason[reason_class]["settled_pnl"] += float(r.get("shadow_pnl", 0))
            by_reason[reason_class]["would_lose"] += 1

    total_settled_pnl = sum(float(r.get("shadow_pnl", 0)) for r in settled)
    wins = sum(1 for r in settled if r["status"] == "shadow_won")
    wr = (wins / len(settled)) if settled else None

    out = {
        "total_records":   len(records),
        "settled":         len(settled),
        "pending":         len(pending),
        "missed_pnl":      round(total_settled_pnl, 2),
        "would_win_rate":  round(wr, 3) if wr is not None else None,
        "by_refusal_reason": {
            k: {**v, "settled_pnl": round(v["settled_pnl"], 2)}
            for k, v in by_reason.items()
        },
    }

    # Scaled-cap projections: multiply each settled trade's notional &
    # contracts by `scale` and recompute the P&L. Useful for "if my cap
    # were $3 instead of $1.50, I'd have made $X". Linear extrapolation
    # so assumes liquidity holds — true for small notionals on BTC.
    if scale_caps:
        out["scaled_projections"] = []
        for cap in scale_caps:
            # cap is the new "max trade USD". We compare to the original
            # notional_usd to derive a per-trade scale factor.
            cfg = _load_live_config()
            old_cap = float(cfg.get("max_trade_usd", 1.5))
            scale = float(cap) / old_cap if old_cap > 0 else 1.0
            scaled_pnl = sum(float(r.get("shadow_pnl", 0)) * scale
                             for r in settled)
            out["scaled_projections"].append({
                "cap_usd":     cap,
                "scale":       round(scale, 3),
                "scaled_pnl":  round(scaled_pnl, 2),
                "delta_vs_current": round(scaled_pnl - total_settled_pnl, 2),
            })
    return out


__all__ = [
    "is_live_enabled",
    "can_open_trade",
    "place_live_order",
    "effective_max_trade_usd",
    "effective_daily_loss_usd",
    "effective_asset_budget",
    "record_outcome",
    "reset_kill_switch",
    "reconcile_positions",
    "reconcile_live_fill",
    "run_smoke_test",
    "get_current_safety_status",
    "settle_shadow_trades",
    "shadow_summary",
    "SMOKE_MARKER_PATH",
    "SHADOW_LOG_PATH",
    "LIVE_ALERTS_PATH",
    "DEFAULTS",
]
