"""
Wallet watch — Stage 2 of the copy-trading roadmap.

Polls a watchlist of high-scoring wallets, detects newly-opened
positions, and fires alerts. Read-only on the trading side — Stage 3
will add paper copy and Stage 4 real execution. The watcher just
*sees* and *announces*.

Outputs (always-on):
  * ``data/wallet_alerts.jsonl`` — append-only event log
  * ``audit_log.jsonl`` — standard ``wallet_watch.alert`` events

Outputs (opt-in, if env vars set):
  * Telegram via ``TELEGRAM_BOT_TOKEN`` + ``TELEGRAM_CHAT_ID``

State persisted to ``data/wallet_watch_state.json`` so consecutive
polls only alert on *new* bets, not the same backlog over and over.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from tradingcore.audit import log_event

STATE_PATH = Path(__file__).parent.parent / "data" / "wallet_watch_state.json"
ALERTS_PATH = Path(__file__).parent.parent / "data" / "wallet_alerts.jsonl"
SCORES_PATH = Path(__file__).parent.parent / "data" / "wallet_scores.json"


@dataclass
class WalletAlert:
    """One bet detected on a watched wallet."""
    handle: str
    platform: str
    market_id: str
    market_question: str
    side: str                    # "YES" or "NO"
    amount: float
    shares: float
    prob_after: float
    created_at: str              # ISO
    bet_id: str


# ── State persistence ────────────────────────────────────────────────

def _load_state() -> dict:
    """Per-wallet ``last_seen_bet_id`` map. Empty on first run."""
    if not STATE_PATH.exists():
        return {"version": 1, "wallets": {}}
    try:
        with open(STATE_PATH) as f:
            return json.load(f) or {"version": 1, "wallets": {}}
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "wallets": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_PATH)


# ── Watchlist selection ──────────────────────────────────────────────

def select_watchlist(min_score: float = 0.10, max_wallets: int = 20) -> list[dict]:
    """Choose which wallets to watch.

    Default: top-N scored wallets with positive composite score above
    ``min_score``. Stage 1's scoring is the gate — wallets that haven't
    been scored yet (or scored negative) don't enter the watchlist.
    """
    if not SCORES_PATH.exists():
        return []
    try:
        with open(SCORES_PATH) as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    scores = payload.get("scores", []) or []
    eligible = [s for s in scores if float(s.get("score", 0)) >= min_score]
    eligible.sort(key=lambda s: float(s.get("score", 0)), reverse=True)
    return eligible[:max_wallets]


# ── Alert sinks ──────────────────────────────────────────────────────

def _persist_alert(alert: WalletAlert) -> None:
    """Append the alert to the JSONL alerts file. Always-on."""
    ALERTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_PATH, "a") as f:
        f.write(json.dumps(asdict(alert)) + "\n")


def _send_telegram(alert: WalletAlert) -> bool:
    """Best-effort Telegram push. No-op if env vars aren't set.

    Returns True if the send succeeded, False otherwise. Failure is
    never propagated — the alert was already persisted to disk.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    placeholders = {"", "your_bot_token_here", "your_chat_id_here"}
    if token in placeholders or chat_id in placeholders:
        return False
    try:
        import requests
        msg = (
            f"🐳 WALLET ALERT — {alert.handle} ({alert.platform})\n"
            f"  Side: {alert.side}  @ {alert.prob_after:.0%}\n"
            f"  Size: {alert.amount:.0f} mana ({alert.shares:.1f} shares)\n"
            f"  Q: {alert.market_question[:160]}"
        )
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=10,
        )
        return True
    except Exception:
        return False


# ── Polling ──────────────────────────────────────────────────────────

def _poll_manifold_wallet(
    handle: str,
    last_seen_bet_id: str | None,
    *,
    client=None,
    limit: int = 25,
) -> list[WalletAlert]:
    """Fetch ``handle``'s most-recent bets; return any newer than
    ``last_seen_bet_id`` as alerts.

    Manifold returns bets newest-first. On first call (no
    ``last_seen_bet_id``), we still return up to ``limit`` alerts so
    the operator gets immediate visibility — they can dismiss
    historical entries from the JSONL log if not wanted.
    """
    if client is None:
        from lib.manifold_client import ManifoldClient
        client = ManifoldClient()

    try:
        bets = client._get("/bets", {"username": handle.lstrip("@"), "limit": limit}) or []
    except Exception as e:
        log_event("wallet_watch", "fetch_failed",
                  {"handle": handle, "error": str(e)[:200]},
                  result="degraded")
        return []

    new_bets: list[dict] = []
    for b in bets:
        bet_id = b.get("id") or b.get("betId")
        if not bet_id:
            continue
        if last_seen_bet_id and bet_id == last_seen_bet_id:
            break  # everything from here back is old
        if b.get("isCancelled"):
            continue
        new_bets.append(b)

    # Resolve market questions for context. One API call per unique contract.
    contract_questions: dict[str, str] = {}
    for b in new_bets:
        cid = b.get("contractId")
        if cid and cid not in contract_questions:
            try:
                m = client._get(f"/market/{cid}")
                contract_questions[cid] = m.get("question", "")[:200] if isinstance(m, dict) else ""
            except Exception:
                contract_questions[cid] = ""

    alerts: list[WalletAlert] = []
    for b in new_bets:
        ct = int(b.get("createdTime", 0) or 0)
        iso = (datetime.fromtimestamp(ct / 1000, tz=timezone.utc).isoformat()
               if ct else "")
        alerts.append(WalletAlert(
            handle=handle, platform="manifold",
            market_id=b.get("contractId", ""),
            market_question=contract_questions.get(b.get("contractId", ""), ""),
            side=str(b.get("outcome", "")).upper(),
            amount=float(b.get("amount", 0) or 0),
            shares=float(b.get("shares", 0) or 0),
            prob_after=float(b.get("probAfter", 0) or 0),
            created_at=iso,
            bet_id=str(b.get("id", b.get("betId", ""))),
        ))
    return alerts


# ── Orchestrator ─────────────────────────────────────────────────────

def run_watch_cycle(
    *,
    min_score: float = 0.10,
    max_wallets: int = 20,
    max_alerts_per_wallet: int = 10,
) -> dict:
    """One polling pass over the watchlist.

    Side effects (in order, all best-effort):
      1. State updated for each polled wallet
      2. Alerts persisted to JSONL
      3. Telegram pushed if configured
      4. Audit-log event per alert

    Returns a summary dict: ``{wallets_polled, alerts_fired, alerts}``.
    """
    watchlist = select_watchlist(min_score=min_score, max_wallets=max_wallets)
    if not watchlist:
        log_event("wallet_watch", "no_watchlist", {
            "reason": "no scored wallets above min_score",
            "min_score": min_score,
        }, result="degraded")
        return {"wallets_polled": 0, "alerts_fired": 0, "alerts": []}

    state = _load_state()
    wallets_state = state.setdefault("wallets", {})

    all_alerts: list[WalletAlert] = []
    polled = 0
    for w in watchlist:
        handle = w.get("handle")
        if not handle:
            continue
        platform = w.get("platform", "manifold")
        # Only Manifold polling implemented in Stage 2; Polymarket lands in 2B.
        if platform != "manifold":
            continue
        polled += 1
        last_seen = wallets_state.get(handle, {}).get("last_bet_id")
        new_alerts = _poll_manifold_wallet(handle, last_seen)
        # Cap per-wallet to avoid alert storms when first watching a new wallet.
        new_alerts = new_alerts[:max_alerts_per_wallet]
        for a in new_alerts:
            _persist_alert(a)
            telegram_ok = _send_telegram(a)
            # Stage 3: record a paper-copy entry so we can measure
            # later whether following this wallet would have been
            # profitable. Best-effort — never blocks the alert flow.
            try:
                from lib.wallet_paper_copy import record_paper_copy_from_alert
                record_paper_copy_from_alert(asdict(a))
            except Exception as e:
                log_event("wallet_watch", "paper_copy_record_failed",
                          {"error": str(e)[:200]}, result="degraded")
            log_event("wallet_watch", "alert", {
                "handle": a.handle, "platform": a.platform,
                "side": a.side, "amount": a.amount,
                "prob_after": a.prob_after,
                "market_id": a.market_id,
                "telegram_sent": telegram_ok,
            })
        all_alerts.extend(new_alerts)
        # Advance "last_seen" to the newest bet we just processed.
        if new_alerts:
            wallets_state[handle] = {
                "last_bet_id": new_alerts[0].bet_id,
                "last_polled_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            # Update poll timestamp even when no new bets — so the
            # operator can see the watcher is alive.
            wallets_state.setdefault(handle, {})["last_polled_at"] = (
                datetime.now(timezone.utc).isoformat()
            )

    _save_state(state)
    log_event("wallet_watch", "cycle_complete", {
        "wallets_polled": polled, "alerts_fired": len(all_alerts),
    })
    return {
        "wallets_polled": polled,
        "alerts_fired": len(all_alerts),
        "alerts": [asdict(a) for a in all_alerts],
    }
