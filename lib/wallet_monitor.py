"""
Wallet monitor — Stage 1 of the copy-trading roadmap.

Fetches trade history for a given wallet (Manifold user or Polymarket
on-chain address), scores performance over a rolling lookback window,
and persists the result for downstream stages to consume.

Stages (per ROADMAP):
  1. Wallet fetcher + scorer            ← this module
  2. Watchlist + Telegram alerts
  3. Paper copy mode
  4. Real copy execution

Design:
  * Platform-agnostic at the API level: ``score_wallet(handle, platform)``
    routes to platform-specific fetchers.
  * Read-only: no orders placed, no state mutated outside ``data/wallet_scores.json``.
  * Pluggable: adding Polymarket later is a new ``_fetch_polymarket_*``
    function; the scoring math stays shared.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.audit import log_event

SCORES_PATH = Path(__file__).parent.parent / "data" / "wallet_scores.json"


@dataclass
class WalletPerformance:
    """One wallet's scored performance over a lookback window.

    All money fields are in USD-equivalent (mana for Manifold; USDC for
    Polymarket). ``roi_pct`` is the simple return = realized_pnl / capital_at_risk.
    """
    handle: str
    platform: str                       # "manifold" | "polymarket"
    lookback_days: int
    total_bets: int
    settled_bets: int
    open_bets: int
    wins: int
    losses: int
    win_rate: float                     # wins / settled_bets
    realized_pnl: float                 # settled bets only
    unrealized_pnl: float               # open bets, marked at current market price
    capital_at_risk: float              # sum of position cost basis
    roi_pct: float                      # realized_pnl / capital_at_risk
    avg_bet_size: float
    last_bet_at: str                    # ISO timestamp
    score: float                        # composite — see _composite_score
    raw_metrics: dict = field(default_factory=dict)


# ── Manifold fetching ────────────────────────────────────────────────

def _fetch_manifold_user(handle: str, *, client=None) -> dict | None:
    """Fetch the public user profile for ``handle``.

    Single API call. Returns the raw user dict (includes balance,
    totalDeposits, fractionResolvedCorrectly, lastBetTime, etc.) or
    None on failure. This is the efficient path for SCORING — much
    cheaper than reconstructing P&L from individual bet records,
    which Manifold's bet objects don't expose (no isResolved/profit
    fields on bets).
    """
    if client is None:
        from lib.manifold_client import ManifoldClient
        client = ManifoldClient()
    handle = handle.lstrip("@")
    try:
        return client._get(f"/user/{handle}")
    except Exception as e:
        log_event("wallet_monitor", "manifold_user_fetch_failed",
                  {"handle": handle, "error": str(e)[:200]},
                  result="degraded")
        return None


def _fetch_manifold_recent_bets(handle: str, *, limit: int = 50,
                                client=None) -> list[dict]:
    """Fetch a wallet's most-recent N bets — for activity counting and
    the Stage 3 copy-trading mirror, NOT for win-rate scoring (those
    bet objects don't expose isResolved; use the user profile for that).
    """
    if client is None:
        from lib.manifold_client import ManifoldClient
        client = ManifoldClient()
    handle = handle.lstrip("@")
    try:
        return client._get("/bets", {"username": handle, "limit": limit}) or []
    except Exception as e:
        log_event("wallet_monitor", "manifold_bets_fetch_failed",
                  {"handle": handle, "error": str(e)[:200]},
                  result="degraded")
        return []


# ── Polymarket fetching (stub) ───────────────────────────────────────

def _fetch_polymarket_trades(wallet_address: str, *, lookback_days: int = 30) -> list[dict]:
    """Polymarket Data-API fetch — placeholder until Stage 1B.

    Polymarket exposes per-wallet trade history at
    ``https://data-api.polymarket.com/trades?user=<wallet>&limit=N``. To
    avoid a half-built fetcher in production, this stub raises until
    the implementation lands.
    """
    raise NotImplementedError(
        "Polymarket wallet fetching not yet implemented — "
        "enable POLY_PRIVATE_KEY in .env and revisit"
    )


# ── Scoring ──────────────────────────────────────────────────────────

def _score_manifold_user(user: dict, recent_bets: list[dict]) -> dict:
    """Compute performance metrics from a Manifold user profile + recent bets.

    The user profile has the authoritative win-rate and net-P&L fields
    (``fractionResolvedCorrectly``, ``balance``, ``totalDeposits``,
    ``lastBetTime``). Individual bet objects don't expose ``isResolved``
    or ``profit``, so we use them only for activity counting and
    average-size estimation.
    """
    balance = float(user.get("balance", 0) or 0)
    total_deposits = float(user.get("totalDeposits", 0) or 0)
    # Net P&L since signup: current balance minus total deposits.
    # Positive = wallet has grown. Mana-denominated.
    net_pnl = balance - total_deposits

    # NOTE: Manifold's ``fractionResolvedCorrectly`` is the wallet's
    # *creator* resolution accuracy (for markets the user created),
    # NOT their trading win rate. There's no public-API field for
    # "fraction of bets won". For Stage 1 we leave win_rate at 0.0 and
    # let the composite score lean on ROI (balance / totalDeposits) —
    # a wallet that's grown materially is empirically a good trader
    # even if we can't compute the bet-level win rate without walking
    # every resolved contract.
    win_rate = 0.0

    # Activity counters — Manifold exposes these aggregated.
    creator = user.get("creatorTraders", {}) or {}
    settled_proxy = int(creator.get("allTime", 0) or 0)  # creator engagement proxy

    # Recency from lastBetTime (epoch ms).
    last_ms = int(user.get("lastBetTime", 0) or 0)
    last_bet_iso = (
        datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).isoformat()
        if last_ms else ""
    )

    # Average bet size from the recent-bets sample.
    sizes: list[float] = []
    open_count = 0
    for b in recent_bets:
        if b.get("isCancelled"):
            continue
        amt = abs(float(b.get("amount", 0) or 0))
        if amt > 0:
            sizes.append(amt)
        if b.get("isFilled") and not b.get("isCancelled"):
            open_count += 1
    avg_size = statistics.mean(sizes) if sizes else 0.0

    # ROI proxy: net P&L over total mana ever deposited. Caps lottery
    # outliers because totalDeposits grows with refills.
    roi = net_pnl / total_deposits if total_deposits > 0 else 0.0

    return {
        "settled": settled_proxy, "open": open_count,
        "wins": 0, "losses": 0,  # bet-level outcomes not available; use win_rate
        "win_rate": win_rate,
        "realized_pnl": net_pnl, "unrealized_pnl": 0.0,
        "cost_basis": total_deposits, "roi_pct": roi,
        "avg_bet_size": avg_size, "last_bet_at": last_bet_iso,
    }


def _composite_score(m: dict) -> float:
    """Single scalar for ranking wallets.

    Stage 1 build: only fields cheaply available from a single user
    profile call. ROI = (balance - totalDeposits) / totalDeposits;
    activity from ``creatorTraders.allTime`` proxy; recency from
    ``lastBetTime``. No bet-level win rate (Manifold doesn't expose
    one efficiently).

    The point isn't to predict — it's to rank. A wallet that's grown
    its balance materially over time, with recent activity, ranks
    above a dormant whale.
    """
    settled = m.get("settled", 0)
    roi = m.get("roi_pct", 0.0)

    # Activity factor: full credit at ≥50 trades total, linear ramp below.
    # Higher floor than before because we're using the all-time count, not
    # last-30d settled — needs more samples to be statistically meaningful.
    activity = min(1.0, settled / 50.0)

    # Recency factor — drops harder for wallets gone idle
    last_iso = m.get("last_bet_at", "")
    recency = 1.0
    if last_iso:
        try:
            last_dt = datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
            days_idle = (datetime.now(timezone.utc) - last_dt).days
            if days_idle > 30:
                recency = 0.25
            elif days_idle > 14:
                recency = 0.50
            elif days_idle > 7:
                recency = 0.80
        except (ValueError, TypeError):
            pass

    # ROI capped at +2.0 (= +200%) so a 50× lottery winner doesn't dominate.
    # Negative ROI flows through linearly — losing wallets correctly score
    # below zero and never make it to copy-trade.
    capped_roi = max(-1.0, min(2.0, roi))

    return round(capped_roi * activity * recency, 4)


# ── Public API ───────────────────────────────────────────────────────

def score_wallet(handle: str, *, platform: str = "manifold",
                 lookback_days: int = 30) -> WalletPerformance:
    """Score one wallet on one platform. Read-only."""
    if platform == "manifold":
        user = _fetch_manifold_user(handle)
        if user is None:
            raise ValueError(f"Manifold user not found: {handle}")
        recent = _fetch_manifold_recent_bets(handle, limit=50)
        metrics = _score_manifold_user(user, recent)
        total_bets = len(recent)
    elif platform == "polymarket":
        trades = _fetch_polymarket_trades(handle, lookback_days=lookback_days)
        # When Polymarket lands, swap _score_polymarket_trades in here.
        raise NotImplementedError("Polymarket scorer pending Stage 1B")
    else:
        raise ValueError(f"Unknown platform: {platform}")

    perf = WalletPerformance(
        handle=handle, platform=platform, lookback_days=lookback_days,
        total_bets=total_bets, settled_bets=metrics["settled"],
        open_bets=metrics["open"], wins=metrics["wins"], losses=metrics["losses"],
        win_rate=round(metrics["win_rate"], 4),
        realized_pnl=round(metrics["realized_pnl"], 2),
        unrealized_pnl=round(metrics["unrealized_pnl"], 2),
        capital_at_risk=round(metrics["cost_basis"], 2),
        roi_pct=round(metrics["roi_pct"], 4),
        avg_bet_size=round(metrics["avg_bet_size"], 2),
        last_bet_at=metrics["last_bet_at"],
        score=_composite_score(metrics),
        raw_metrics=metrics,
    )

    log_event("wallet_monitor", "scored", {
        "handle": handle, "platform": platform,
        "settled": perf.settled_bets, "win_rate": perf.win_rate,
        "roi_pct": perf.roi_pct, "score": perf.score,
    })
    return perf


def discover_top_manifold(*, top_n: int = 25, client=None,
                           seed_handles: list[str] | None = None) -> list[str]:
    """Return a candidate handle list to score.

    Manifold has no public leaderboard endpoint — ``/v0/users`` returns
    new signups, not top traders by profit (verified empirically:
    profitCached comes back None on the list endpoint). Two real
    discovery paths:

      1. Curated seed list — known good traders the user has identified
         (passed via ``seed_handles`` or loaded from
         ``config/copytrade_wallets.yaml``).
      2. Recent-resolution scan (planned for Stage 1B) — walk recently
         resolved high-volume markets and accumulate which wallets
         consistently land on the winning side.

    For Stage 1 MVP we go with #1. Add wallets to the config file as
    you find them.
    """
    if seed_handles:
        return seed_handles[:top_n]
    # Try to load curated list from config
    try:
        import yaml
        cfg_path = Path(__file__).parent.parent / "config" / "copytrade_wallets.yaml"
        if cfg_path.exists():
            with open(cfg_path) as f:
                data = yaml.safe_load(f) or {}
            handles = data.get("manifold", []) or []
            return list(handles)[:top_n]
    except Exception as e:
        log_event("wallet_monitor", "discover_config_load_failed",
                  {"error": str(e)[:200]}, result="degraded")
    return []


def persist_scores(scores: list[WalletPerformance]) -> None:
    """Overwrite the wallet-scores file with the latest scoring pass."""
    SCORES_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "scores": [asdict(s) for s in scores],
    }
    tmp = SCORES_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(SCORES_PATH)


def load_scores() -> list[dict]:
    """Read the latest wallet scores. Empty list if not yet computed."""
    if not SCORES_PATH.exists():
        return []
    try:
        with open(SCORES_PATH) as f:
            return json.load(f).get("scores", []) or []
    except (json.JSONDecodeError, OSError):
        return []
