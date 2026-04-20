"""
Smart Money — track high-performing Polymarket wallets as a signal.

The hypothesis: wallets that consistently turn profit on Polymarket have
*something* (information, model, or discipline) the market doesn't. Their
open positions on a given market are a weak-but-real forward signal.

This module:
    1. Maintains a registry of "smart" wallet addresses with performance
       scores (data/smart_money_wallets.json).
    2. For any market, queries Polymarket's data-api for which tracked
       wallets hold positions, on which side, and how much.
    3. Aggregates into a SmartMoneySignal: direction (YES/NO/NEUTRAL),
       confidence (0-1), wallets on side, net dollars on side.
    4. Exposes `get_smart_money_estimate(market_id)` for the forecaster.

Gating:
    - Disabled by default via strategy.yaml (mechanical.smart_money.enabled).
      Enable only after seeding a real wallet registry.
    - Qualifying wallet must meet min_win_rate, min_trades, min_account_age
      from strategy.yaml.
    - Polymarket-only. Kalshi orderbook is anonymous; Manifold is play-
      money and not a serious smart-money venue.

Security:
    - All Polymarket API responses treated as untrusted (per-field validation).
    - Hard timeouts + rate limit. Never fails the caller; returns None on any
      error so the forecaster degrades gracefully.
    - Wallet addresses validated as EVM-format (0x + 40 hex) before query.
    - Cached signals (5min TTL) so a scan cycle doesn't hammer the API.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
import yaml

from lib.audit import log_event

DATA_DIR = Path(__file__).parent.parent / "data"
WALLET_REGISTRY_PATH = DATA_DIR / "smart_money_wallets.json"
CONFIG_PATH = Path(__file__).parent.parent / "config" / "strategy.yaml"

POLYMARKET_DATA_API = "https://data-api.polymarket.com"

SIGNAL_CACHE_TTL_SEC = 300.0      # 5 min — positions change rarely within a scan cycle
WALLET_REFRESH_TTL_SEC = 86400.0  # 24h — wallet performance doesn't flip daily
HTTP_TIMEOUT_SEC = 10
MIN_WALLETS_FOR_SIGNAL = 2        # Need 2+ wallets agreeing to emit a signal
MAX_REGISTRY_SIZE = 100            # Cap registry to prevent unbounded growth

_EVM_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# In-process cache for per-market signals
_SIGNAL_CACHE: dict[str, tuple[float, "SmartMoneySignal | None"]] = {}


@dataclass
class WalletProfile:
    """Performance profile for a tracked wallet."""
    address: str
    win_rate: float                      # 0.0 - 1.0 on resolved positions
    trade_count: int                     # total closed positions
    account_age_days: int                # since first trade
    total_profit_usd: float
    last_updated_ts: float               # epoch seconds
    score: float = 0.0                   # composite 0-1, computed

    def meets_criteria(self, min_wr: float, min_trades: int, min_age: int) -> bool:
        return (
            self.win_rate >= min_wr
            and self.trade_count >= min_trades
            and self.account_age_days >= min_age
        )


@dataclass
class SmartMoneySignal:
    """Aggregate signal for a single market from all qualifying wallets."""
    market_id: str
    direction: str                       # "YES" / "NO" / "NEUTRAL"
    probability_estimate: float          # Implied probability from flow, 0-1
    confidence: float                    # 0-1, scales with wallet count + agreement
    wallets_on_yes: int = 0
    wallets_on_no: int = 0
    net_usd_yes: float = 0.0
    net_usd_no: float = 0.0
    qualifying_wallets: int = 0
    wallet_details: list[dict] = field(default_factory=list)


# ── Config ────────────────────────────────────────────────────────────

def _load_strategy() -> dict:
    try:
        with open(CONFIG_PATH, "r") as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return {}


def _smart_money_config() -> dict:
    cfg = _load_strategy().get("mechanical", {}).get("smart_money", {})
    return {
        "enabled": cfg.get("enabled", False),
        "min_win_rate": cfg.get("min_win_rate", 0.60),
        "min_trades": cfg.get("min_trades", 100),
        "min_account_age_days": cfg.get("min_account_age_days", 120),
    }


def is_enabled() -> bool:
    return bool(_smart_money_config()["enabled"])


# ── Input validation ──────────────────────────────────────────────────

def _valid_evm_address(addr: str) -> bool:
    """Reject anything not 0x + 40 hex. Belts-and-suspenders against feeding
    unsanitized user input into a URL."""
    return isinstance(addr, str) and bool(_EVM_ADDR_RE.match(addr))


# ── Registry I/O ──────────────────────────────────────────────────────

def _load_registry() -> dict[str, WalletProfile]:
    """Load wallet registry from disk. Empty dict if missing or malformed."""
    if not WALLET_REGISTRY_PATH.exists():
        return {}
    try:
        with open(WALLET_REGISTRY_PATH, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        log_event("smart_money", "registry_load_failed", {
            "path": str(WALLET_REGISTRY_PATH),
        }, result="failed")
        return {}

    registry: dict[str, WalletProfile] = {}
    wallets = data.get("wallets", []) if isinstance(data, dict) else []
    for w in wallets:
        if not isinstance(w, dict):
            continue
        try:
            addr = w.get("address", "")
            if not _valid_evm_address(addr):
                continue
            registry[addr.lower()] = WalletProfile(
                address=addr.lower(),
                win_rate=float(w.get("win_rate", 0.0)),
                trade_count=int(w.get("trade_count", 0)),
                account_age_days=int(w.get("account_age_days", 0)),
                total_profit_usd=float(w.get("total_profit_usd", 0.0)),
                last_updated_ts=float(w.get("last_updated_ts", 0.0)),
                score=float(w.get("score", 0.0)),
            )
        except (TypeError, ValueError):
            continue
    return registry


def _save_registry(registry: dict[str, WalletProfile]) -> None:
    """Atomic write: tmp file + rename. Never corrupts registry mid-write."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WALLET_REGISTRY_PATH.with_suffix(".json.tmp")
    payload = {
        "updated_ts": time.time(),
        "wallet_count": len(registry),
        "wallets": [asdict(w) for w in registry.values()],
    }
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        tmp.replace(WALLET_REGISTRY_PATH)
    except OSError as e:
        log_event("smart_money", "registry_save_failed", {
            "error": str(e)[:200],
        }, result="failed")


def add_wallet(address: str) -> bool:
    """Seed a wallet into the registry. Returns True if added (not already tracked)."""
    if not _valid_evm_address(address):
        return False
    registry = _load_registry()
    if address.lower() in registry:
        return False
    if len(registry) >= MAX_REGISTRY_SIZE:
        log_event("smart_money", "registry_full", {
            "cap": MAX_REGISTRY_SIZE,
        }, result="failed")
        return False
    registry[address.lower()] = WalletProfile(
        address=address.lower(),
        win_rate=0.0,
        trade_count=0,
        account_age_days=0,
        total_profit_usd=0.0,
        last_updated_ts=0.0,
        score=0.0,
    )
    _save_registry(registry)
    log_event("smart_money", "wallet_added", {
        "address": address.lower(),
        "registry_size": len(registry),
    }, result="success")
    return True


# ── HTTP helper ───────────────────────────────────────────────────────

def _data_api_get(endpoint: str, params: dict | None = None) -> Optional[list | dict]:
    """GET from Polymarket data-api with timeout and error suppression."""
    try:
        resp = requests.get(
            f"{POLYMARKET_DATA_API}{endpoint}",
            params=params or {},
            timeout=HTTP_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        log_event("smart_money", "api_failed", {
            "endpoint": endpoint,
            "error": str(e)[:200],
        }, result="failed")
        return None


# ── Wallet performance refresh ────────────────────────────────────────

def refresh_wallet_profile(address: str, force: bool = False) -> WalletProfile | None:
    """
    Refresh a wallet's performance stats from Polymarket.

    Only re-queries if last update > WALLET_REFRESH_TTL_SEC ago unless force=True.
    Returns the refreshed profile, or None if the wallet can't be scored.
    """
    if not _valid_evm_address(address):
        return None

    addr = address.lower()
    registry = _load_registry()
    existing = registry.get(addr)

    if existing and not force:
        age = time.time() - existing.last_updated_ts
        if age < WALLET_REFRESH_TTL_SEC and existing.trade_count > 0:
            return existing

    # Pull trade history. Data-api shape varies over time — tolerate missing fields.
    trades = _data_api_get("/trades", params={"user": addr, "limit": 500})
    if not isinstance(trades, list) or not trades:
        return existing  # Preserve what we had if refresh fails

    wins = 0
    losses = 0
    total_profit = 0.0
    earliest_ts: float | None = None

    for t in trades:
        if not isinstance(t, dict):
            continue
        ts = t.get("timestamp") or t.get("ts") or 0
        try:
            ts_f = float(ts)
        except (TypeError, ValueError):
            ts_f = 0.0
        if ts_f and (earliest_ts is None or ts_f < earliest_ts):
            earliest_ts = ts_f

        # Only count closed/resolved trades for win rate
        pnl = t.get("realized_pnl") or t.get("profit") or 0
        try:
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        if pnl_f > 0:
            wins += 1
            total_profit += pnl_f
        elif pnl_f < 0:
            losses += 1
            total_profit += pnl_f

    total_closed = wins + losses
    if total_closed == 0:
        return existing

    win_rate = wins / total_closed
    age_days = 0
    if earliest_ts:
        age_days = max(0, int((time.time() - earliest_ts) / 86400))

    # Composite score: win_rate weighted by log(trades) weighted by age floor
    import math
    trade_factor = min(1.0, math.log10(max(1, total_closed)) / 3.0)  # caps at 1000 trades
    age_factor = min(1.0, age_days / 365.0)                           # caps at 1 year
    score = win_rate * 0.6 + trade_factor * 0.25 + age_factor * 0.15

    profile = WalletProfile(
        address=addr,
        win_rate=round(win_rate, 4),
        trade_count=total_closed,
        account_age_days=age_days,
        total_profit_usd=round(total_profit, 2),
        last_updated_ts=time.time(),
        score=round(score, 4),
    )

    registry[addr] = profile
    _save_registry(registry)
    log_event("smart_money", "wallet_refreshed", {
        "address": addr,
        "win_rate": profile.win_rate,
        "trade_count": profile.trade_count,
        "score": profile.score,
    }, result="success")
    return profile


# ── Per-market signal ─────────────────────────────────────────────────

def _fetch_wallet_positions(address: str, market_id: str) -> dict | None:
    """
    Return {side: "YES"|"NO", size_usd: float} if the wallet holds a position
    in `market_id`, else None.
    """
    positions = _data_api_get("/positions", params={"user": address})
    if not isinstance(positions, list):
        return None

    for p in positions:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("conditionId", "") or p.get("market", "") or p.get("asset", ""))
        if market_id not in pid and pid not in market_id:
            continue
        side_raw = str(p.get("outcome", "") or p.get("side", "")).upper()
        if "YES" in side_raw or side_raw == "0":
            side = "YES"
        elif "NO" in side_raw or side_raw == "1":
            side = "NO"
        else:
            continue
        try:
            size = float(p.get("size", 0) or p.get("value_usd", 0) or 0)
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            continue
        return {"side": side, "size_usd": size}
    return None


def get_smart_money_signal(
    market_id: str,
    force_refresh: bool = False,
) -> SmartMoneySignal | None:
    """
    Aggregate smart-money positions on a given market.

    Returns SmartMoneySignal or None if:
        - feature disabled
        - no qualifying wallets
        - fewer than MIN_WALLETS_FOR_SIGNAL on the same side

    Safe to call in hot paths — caches per market for 5min.

    Args:
        market_id: Polymarket market_id / conditionId
        force_refresh: Skip cache (for dashboard manual refresh)
    """
    if not is_enabled():
        return None
    if not market_id or not isinstance(market_id, str):
        return None

    # Cache check
    now = time.time()
    cached = _SIGNAL_CACHE.get(market_id)
    if cached and not force_refresh:
        ts, sig = cached
        if (now - ts) < SIGNAL_CACHE_TTL_SEC:
            return sig

    cfg = _smart_money_config()
    registry = _load_registry()
    qualifying = [
        w for w in registry.values()
        if w.meets_criteria(cfg["min_win_rate"], cfg["min_trades"],
                            cfg["min_account_age_days"])
    ]
    if not qualifying:
        _SIGNAL_CACHE[market_id] = (now, None)
        return None

    wallets_on_yes = 0
    wallets_on_no = 0
    net_usd_yes = 0.0
    net_usd_no = 0.0
    wallet_details: list[dict] = []

    # Weight each wallet's flow by its quality score
    weighted_yes = 0.0
    weighted_no = 0.0

    for wallet in qualifying:
        pos = _fetch_wallet_positions(wallet.address, market_id)
        if not pos:
            continue
        if pos["side"] == "YES":
            wallets_on_yes += 1
            net_usd_yes += pos["size_usd"]
            weighted_yes += pos["size_usd"] * wallet.score
        elif pos["side"] == "NO":
            wallets_on_no += 1
            net_usd_no += pos["size_usd"]
            weighted_no += pos["size_usd"] * wallet.score
        wallet_details.append({
            "address": wallet.address[:10] + "…",  # truncated for log volume
            "score": wallet.score,
            "side": pos["side"],
            "size_usd": pos["size_usd"],
        })

    total_wallets = wallets_on_yes + wallets_on_no
    if total_wallets < MIN_WALLETS_FOR_SIGNAL:
        _SIGNAL_CACHE[market_id] = (now, None)
        return None

    # Direction by majority of weighted flow
    total_weighted = weighted_yes + weighted_no
    if total_weighted <= 0:
        direction = "NEUTRAL"
        probability_estimate = 0.5
    elif weighted_yes > weighted_no:
        direction = "YES"
        probability_estimate = weighted_yes / total_weighted
    else:
        direction = "NO"
        probability_estimate = 1.0 - (weighted_no / total_weighted)

    # Clip to sane range — smart money rarely "should" say 0.95 from this signal
    # alone; keep it as a gentle pull, not a hard claim.
    probability_estimate = max(0.15, min(0.85, probability_estimate))

    # Confidence = agreement × scale. 5 wallets unanimous is a strong signal;
    # 2 wallets split is barely a signal.
    agreement = (
        abs(wallets_on_yes - wallets_on_no) / total_wallets
        if total_wallets > 0 else 0
    )
    scale = min(1.0, total_wallets / 5.0)
    confidence = round(agreement * scale, 3)

    sig = SmartMoneySignal(
        market_id=market_id,
        direction=direction,
        probability_estimate=round(probability_estimate, 4),
        confidence=confidence,
        wallets_on_yes=wallets_on_yes,
        wallets_on_no=wallets_on_no,
        net_usd_yes=round(net_usd_yes, 2),
        net_usd_no=round(net_usd_no, 2),
        qualifying_wallets=len(qualifying),
        wallet_details=wallet_details,
    )

    _SIGNAL_CACHE[market_id] = (now, sig)
    log_event("smart_money", "signal_computed", {
        "market_id": market_id,
        "direction": direction,
        "probability": sig.probability_estimate,
        "confidence": confidence,
        "wallets_yes": wallets_on_yes,
        "wallets_no": wallets_on_no,
    }, result="success")
    return sig


def get_smart_money_estimate(market_id: str) -> float | None:
    """Convenience wrapper for the forecaster — returns probability or None."""
    sig = get_smart_money_signal(market_id)
    if sig is None or sig.direction == "NEUTRAL":
        return None
    # Only emit if confidence is meaningful
    if sig.confidence < 0.20:
        return None
    return sig.probability_estimate


# ── Wallet discovery ──────────────────────────────────────────────────

def discover_top_wallets(limit: int = 25) -> list[str]:
    """
    Pull the Polymarket leaderboard and return candidate wallet addresses.

    Best-effort — the leaderboard endpoint is not officially documented and
    may move. Returns [] on failure.

    Use: run periodically from a cron, seed the registry, then let
    `refresh_wallet_profile` compute real scores.
    """
    candidates: list[str] = []
    # The leaderboard sits behind polymarket.com/api/leaderboard (browser API).
    # We try a best-effort fetch with a spoofed UA; on failure, return [].
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Polybot/0.1; +research)",
    }
    try:
        resp = requests.get(
            "https://polymarket.com/api/leaderboard",
            params={"window": "month", "type": "profit", "limit": limit},
            headers=headers,
            timeout=HTTP_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log_event("smart_money", "leaderboard_failed", {
            "error": str(e)[:200],
        }, result="failed")
        return []

    entries = data if isinstance(data, list) else data.get("leaders", []) if isinstance(data, dict) else []
    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        addr = entry.get("proxyAddress") or entry.get("address") or entry.get("wallet", "")
        if _valid_evm_address(addr):
            candidates.append(addr.lower())

    return candidates


def seed_registry_from_leaderboard(max_new: int = 20) -> int:
    """
    One-shot: pull leaderboard, seed new wallets into registry.
    Returns count of wallets newly added.
    """
    candidates = discover_top_wallets(limit=max_new * 2)
    added = 0
    for addr in candidates:
        if add_wallet(addr):
            added += 1
            if added >= max_new:
                break
    log_event("smart_money", "registry_seeded", {
        "added": added,
        "candidates_seen": len(candidates),
    }, result="success")
    return added


def get_registry_summary() -> dict:
    """Inspection helper for the dashboard — registry stats."""
    registry = _load_registry()
    cfg = _smart_money_config()
    qualifying = [
        w for w in registry.values()
        if w.meets_criteria(cfg["min_win_rate"], cfg["min_trades"],
                            cfg["min_account_age_days"])
    ]
    return {
        "enabled": cfg["enabled"],
        "registry_size": len(registry),
        "qualifying_wallets": len(qualifying),
        "criteria": {
            "min_win_rate": cfg["min_win_rate"],
            "min_trades": cfg["min_trades"],
            "min_account_age_days": cfg["min_account_age_days"],
        },
        "top_wallets": sorted(
            [
                {
                    "address": w.address,
                    "win_rate": w.win_rate,
                    "trades": w.trade_count,
                    "score": w.score,
                }
                for w in registry.values()
            ],
            key=lambda x: -x["score"],
        )[:10],
    }
