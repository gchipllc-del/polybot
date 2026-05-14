"""
wallet_backtest.py — historical replay of a wallet's bets.

For each historical bet:
  1. Look up the market's eventual resolution
  2. Compute: if we had copied with $copy_size_usd, what would our P&L be?
  3. Aggregate across all resolved bets

This turns Stage-3's 30-day paper-copy wait into hours of compute —
giving Jesse a backwards-looking signal *per wallet* TODAY.

No real orders. No paper-trade state mutation. Pure measurement.

**Honest caveat:** historical fills assume we'd land at the same
``probAfter``/trade-price the source wallet got. In production we'd
react after them, so real slippage is slightly worse. The backtest is
an *upper bound* on copy-edge — useful for filtering "wallets to follow"
vs "wallets that look great but were lucky once".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path

from lib.audit import log_event

BACKTEST_DIR = Path(__file__).parent.parent / "data" / "wallet_backtests"

# Copy sizing — same defaults as wallet_paper_copy.py so the backtest
# matches what Stage 3 would actually do.
DEFAULT_COPY_SIZE_USD = 10.0
DEFAULT_MAX_PER_TRADE = 10.0
EXTREME_PRICE_FLOOR = 0.05
EXTREME_PRICE_CEIL = 0.95


@dataclass
class BacktestTrade:
    """One historical bet, replayed as a hypothetical paper copy."""
    source_handle: str
    platform: str
    market_id: str
    market_question: str
    bet_id: str
    source_side: str             # "YES" | "NO"
    source_fill_price: float     # what the source wallet got
    source_amount: float
    bet_time: str                # ISO
    resolution_time: str
    market_outcome: str          # "YES" | "NO" | "VOID" | "OPEN"

    # Our hypothetical copy
    copied: bool = False
    skip_reason: str = ""
    our_size_usd: float = 0.0
    our_contracts: float = 0.0
    our_fill_price: float = 0.0
    paper_pnl: float = 0.0
    paper_status: str = "skipped"  # "won" | "lost" | "void" | "skipped" | "open"


@dataclass
class BacktestSummary:
    """Aggregate result for one wallet's backtest."""
    handle: str
    platform: str
    backtested_at: str
    lookback_days: int
    copy_size_usd: float

    total_bets_seen: int = 0
    bets_resolved: int = 0
    bets_copied: int = 0
    bets_skipped: int = 0

    wins: int = 0
    losses: int = 0
    voids: int = 0

    total_paper_pnl: float = 0.0
    total_capital_deployed: float = 0.0
    win_rate: float = 0.0
    roi_pct: float = 0.0

    per_day_pnl: dict = field(default_factory=dict)
    skip_reasons: dict = field(default_factory=dict)
    top_winners: list = field(default_factory=list)
    top_losers: list = field(default_factory=list)


# ── Manifold backtest ────────────────────────────────────────────────

def _backtest_manifold(
    handle: str,
    *,
    copy_size_usd: float,
    lookback_days: int,
    max_pages: int = 20,
) -> list[BacktestTrade]:
    """Walk Manifold bets for ``handle``, compute hypothetical copy P&L."""
    from lib.manifold_client import ManifoldClient

    client = ManifoldClient()
    handle = handle.lstrip("@")
    cutoff_ms = int(
        (datetime.now(timezone.utc).timestamp() - lookback_days * 86400) * 1000
    )

    all_bets: list[dict] = []
    before: str | None = None
    for _ in range(max_pages):
        params: dict = {"username": handle, "limit": 1000}
        if before:
            params["before"] = before
        try:
            page = client._get("/bets", params) or []
        except Exception as e:
            log_event("wallet_backtest", "manifold_fetch_failed",
                      {"handle": handle, "error": str(e)[:200]},
                      result="degraded")
            break
        if not page:
            break
        all_bets.extend(page)
        oldest = page[-1].get("createdTime", 0) or 0
        if oldest < cutoff_ms:
            break
        before = page[-1].get("id")
        if not before:
            break

    # Filter to lookback window + non-cancelled
    bets = [
        b for b in all_bets
        if (b.get("createdTime", 0) or 0) >= cutoff_ms
        and not b.get("isCancelled")
    ]

    # Cache /market/{id} lookups — one call per unique contract
    market_cache: dict[str, dict] = {}
    trades: list[BacktestTrade] = []

    for b in bets:
        cid = b.get("contractId")
        if not cid:
            continue
        if cid not in market_cache:
            try:
                m = client._get(f"/market/{cid}") or {}
                market_cache[cid] = m if isinstance(m, dict) else {}
            except Exception:
                market_cache[cid] = {}
        market = market_cache.get(cid, {})

        question = (market.get("question") or "")[:200]
        is_resolved = bool(market.get("isResolved"))
        resolution = str(market.get("resolution") or "").upper()
        resolution_time = ""
        if market.get("resolutionTime"):
            try:
                rt = int(market["resolutionTime"])
                resolution_time = datetime.fromtimestamp(
                    rt / 1000, tz=timezone.utc
                ).isoformat()
            except (ValueError, TypeError):
                pass

        side = str(b.get("outcome", "")).upper()
        fill_price = float(b.get("probAfter", 0) or 0)
        amount = float(b.get("amount", 0) or 0)
        bet_time = ""
        ct = b.get("createdTime")
        if ct:
            try:
                bet_time = datetime.fromtimestamp(
                    ct / 1000, tz=timezone.utc
                ).isoformat()
            except (ValueError, TypeError):
                pass

        t = BacktestTrade(
            source_handle=handle, platform="manifold",
            market_id=cid, market_question=question,
            bet_id=str(b.get("id", "")),
            source_side=side, source_fill_price=fill_price,
            source_amount=amount, bet_time=bet_time,
            resolution_time=resolution_time,
            market_outcome=resolution if is_resolved else "OPEN",
        )

        if not is_resolved:
            t.skip_reason = "still_open"
        elif side not in ("YES", "NO"):
            # Multi-outcome contracts (numeric/free-response) — skip
            t.skip_reason = "non_binary_bet"
        elif not (EXTREME_PRICE_FLOOR <= fill_price <= EXTREME_PRICE_CEIL):
            t.skip_reason = "extreme_price"
        elif resolution in ("CANCEL", "MKT", ""):
            # MKT = resolved to a custom probability; treat as void
            # since copy-trading semantics don't translate cleanly.
            t.copied = True
            t.our_size_usd = min(copy_size_usd, DEFAULT_MAX_PER_TRADE)
            t.our_fill_price = fill_price
            t.our_contracts = round(t.our_size_usd / fill_price, 4)
            t.paper_status = "void"
            t.paper_pnl = 0.0
        else:
            # Settled YES/NO — compute P&L
            t.copied = True
            t.our_size_usd = min(copy_size_usd, DEFAULT_MAX_PER_TRADE)
            t.our_fill_price = fill_price
            t.our_contracts = round(t.our_size_usd / fill_price, 4)
            if resolution == side:
                t.paper_status = "won"
                # Each winning share pays 1; we paid our_size_usd
                t.paper_pnl = round(t.our_contracts * 1.0 - t.our_size_usd, 4)
            else:
                t.paper_status = "lost"
                t.paper_pnl = round(-t.our_size_usd, 4)
        trades.append(t)

    return trades


# ── Polymarket backtest ──────────────────────────────────────────────

POLYMARKET_DATA_API = "https://data-api.polymarket.com"
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"


def _backtest_polymarket(
    wallet_address: str,
    *,
    copy_size_usd: float,
    lookback_days: int,
    max_pages: int = 20,
) -> list[BacktestTrade]:
    """Walk Polymarket trades for ``wallet_address``, compute hypothetical
    copy P&L. Only BUY trades count as entries — SELLs are exits.
    """
    import requests

    addr = wallet_address.strip().lower()
    if not addr.startswith("0x") or len(addr) != 42:
        log_event("wallet_backtest", "bad_polymarket_address",
                  {"address": wallet_address[:80]}, result="degraded")
        return []

    cutoff_ts = datetime.now(timezone.utc).timestamp() - lookback_days * 86400
    all_trades: list[dict] = []
    offset = 0
    page_size = 200
    for _ in range(max_pages):
        try:
            r = requests.get(
                f"{POLYMARKET_DATA_API}/trades",
                params={"user": addr, "limit": page_size, "offset": offset},
                timeout=20,
            )
            r.raise_for_status()
            page = r.json() or []
        except Exception as e:
            log_event("wallet_backtest", "polymarket_fetch_failed",
                      {"address": addr[:10], "error": str(e)[:200]},
                      result="degraded")
            break
        if not page:
            break
        all_trades.extend(page)
        # Stop once we've crossed the lookback cutoff
        last_ts = page[-1].get("timestamp", 0)
        if isinstance(last_ts, str):
            try:
                last_ts = float(last_ts)
            except ValueError:
                last_ts = 0
        if last_ts and last_ts < cutoff_ts:
            break
        offset += page_size

    # Filter to lookback window
    in_window: list[dict] = []
    for tr in all_trades:
        ts = tr.get("timestamp", 0)
        if isinstance(ts, str):
            try:
                ts = float(ts)
            except ValueError:
                continue
        if ts and ts >= cutoff_ts:
            in_window.append(tr)

    # Cache resolutions — one Gamma call per unique conditionId
    cond_cache: dict[str, dict] = {}
    trades: list[BacktestTrade] = []

    for tr in in_window:
        cid = tr.get("conditionId") or tr.get("market") or ""
        if not cid:
            continue
        if cid not in cond_cache:
            # Gamma's /markets defaults to closed=false. We must
            # explicitly ask for closed markets when backtesting since
            # most copyable trades are on now-resolved markets. If
            # closed=true returns empty, fall back to closed=false so
            # in-flight trades are still visible (they'll be tagged
            # OPEN downstream).
            cond_cache[cid] = {}
            for closed_flag in ("true", "false"):
                try:
                    r = requests.get(
                        f"{POLYMARKET_GAMMA}/markets",
                        params={"condition_ids": cid, "closed": closed_flag},
                        timeout=15,
                    )
                    r.raise_for_status()
                    data = r.json() or []
                except Exception:
                    continue
                if isinstance(data, list) and data:
                    cond_cache[cid] = data[0]
                    break
        m = cond_cache.get(cid, {})

        question = (m.get("question") or "")[:200]
        is_closed = bool(m.get("closed"))
        outcomes_raw = m.get("outcomePrices") or "[]"
        try:
            outcomes = (
                json.loads(outcomes_raw)
                if isinstance(outcomes_raw, str)
                else outcomes_raw
            )
            yes_final = float(outcomes[0]) if outcomes else None
        except (json.JSONDecodeError, ValueError, TypeError):
            yes_final = None

        market_outcome = "OPEN"
        if is_closed and yes_final is not None:
            if yes_final >= 0.98:
                market_outcome = "YES"
            elif yes_final <= 0.02:
                market_outcome = "NO"
            else:
                market_outcome = "VOID"

        buy_sell = str(tr.get("side") or "").upper()
        outcome = str(tr.get("outcome") or "").upper()
        price = float(tr.get("price", 0) or 0)
        size = float(tr.get("size", 0) or 0)

        ts = tr.get("timestamp", 0)
        if isinstance(ts, str):
            try:
                ts = float(ts)
            except ValueError:
                ts = 0
        bet_time = (
            datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            if ts else ""
        )

        t = BacktestTrade(
            source_handle=addr, platform="polymarket",
            market_id=cid, market_question=question,
            bet_id=str(tr.get("transactionHash", "") or tr.get("id", ""))[:80],
            source_side=outcome,
            source_fill_price=price,
            source_amount=round(size * price, 4),
            bet_time=bet_time,
            resolution_time=str(m.get("endDate", "")),
            market_outcome=market_outcome,
        )

        if market_outcome == "OPEN":
            t.skip_reason = "still_open"
        elif buy_sell != "BUY":
            # Only mirror entries; SELLs are exits we can't replicate
            # without knowing the wallet's intent at that moment.
            t.skip_reason = "sell_not_entry"
        elif outcome not in ("YES", "NO"):
            t.skip_reason = "unknown_outcome"
        elif not (EXTREME_PRICE_FLOOR <= price <= EXTREME_PRICE_CEIL):
            t.skip_reason = "extreme_price"
        elif market_outcome == "VOID":
            t.copied = True
            t.our_size_usd = min(copy_size_usd, DEFAULT_MAX_PER_TRADE)
            t.our_fill_price = price
            t.our_contracts = round(t.our_size_usd / price, 4)
            t.paper_status = "void"
            t.paper_pnl = 0.0
        else:
            t.copied = True
            t.our_size_usd = min(copy_size_usd, DEFAULT_MAX_PER_TRADE)
            t.our_fill_price = price
            t.our_contracts = round(t.our_size_usd / price, 4)
            if market_outcome == outcome:
                t.paper_status = "won"
                t.paper_pnl = round(t.our_contracts * 1.0 - t.our_size_usd, 4)
            else:
                t.paper_status = "lost"
                t.paper_pnl = round(-t.our_size_usd, 4)
        trades.append(t)

    return trades


# ── Aggregation + persistence ────────────────────────────────────────

def _aggregate(
    trades: list[BacktestTrade],
    *,
    handle: str,
    platform: str,
    backtested_at: str,
    lookback_days: int,
    copy_size_usd: float,
) -> BacktestSummary:
    s = BacktestSummary(
        handle=handle, platform=platform,
        backtested_at=backtested_at, lookback_days=lookback_days,
        copy_size_usd=copy_size_usd,
        total_bets_seen=len(trades),
    )
    for t in trades:
        if not t.copied:
            s.bets_skipped += 1
            s.skip_reasons[t.skip_reason] = (
                s.skip_reasons.get(t.skip_reason, 0) + 1
            )
            continue
        s.bets_copied += 1
        s.bets_resolved += 1
        s.total_paper_pnl += t.paper_pnl
        s.total_capital_deployed += t.our_size_usd
        if t.paper_status == "won":
            s.wins += 1
        elif t.paper_status == "lost":
            s.losses += 1
        else:
            s.voids += 1
        day = t.bet_time[:10] if t.bet_time else "unknown"
        s.per_day_pnl[day] = round(
            s.per_day_pnl.get(day, 0.0) + t.paper_pnl, 4
        )
    settled = s.wins + s.losses
    s.win_rate = round(s.wins / settled, 4) if settled > 0 else 0.0
    s.roi_pct = (
        round(s.total_paper_pnl / s.total_capital_deployed, 4)
        if s.total_capital_deployed > 0 else 0.0
    )
    s.total_paper_pnl = round(s.total_paper_pnl, 4)
    s.total_capital_deployed = round(s.total_capital_deployed, 4)

    copied = [t for t in trades if t.copied]
    winners = sorted(copied, key=lambda t: t.paper_pnl, reverse=True)[:5]
    losers = sorted(copied, key=lambda t: t.paper_pnl)[:5]
    s.top_winners = [
        {"market": t.market_question[:100],
         "side": t.source_side, "pnl": t.paper_pnl,
         "fill": t.our_fill_price}
        for t in winners
    ]
    s.top_losers = [
        {"market": t.market_question[:100],
         "side": t.source_side, "pnl": t.paper_pnl,
         "fill": t.our_fill_price}
        for t in losers
    ]
    return s


def _persist(summary: BacktestSummary, trades: list[BacktestTrade]) -> Path:
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_handle = "".join(
        c if c.isalnum() else "_" for c in summary.handle
    )[:40]
    fn = BACKTEST_DIR / f"{summary.platform}_{safe_handle}_{ts}.json"
    payload = {
        "summary": asdict(summary),
        "trades": [asdict(t) for t in trades],
    }
    tmp = fn.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(fn)
    return fn


# ── Public entry ─────────────────────────────────────────────────────

def backtest_wallet(
    handle: str,
    *,
    platform: str = "manifold",
    copy_size_usd: float = DEFAULT_COPY_SIZE_USD,
    lookback_days: int = 90,
    persist: bool = True,
) -> tuple[BacktestSummary, Path | None]:
    """Replay a wallet's historical bets and compute hypothetical copy P&L.

    Returns ``(summary, output_path)``. ``output_path`` is None when
    ``persist=False``. Detailed per-trade results are written to
    ``data/wallet_backtests/<platform>_<handle>_<timestamp>.json``.
    """
    started = datetime.now(timezone.utc)
    if platform == "manifold":
        trades = _backtest_manifold(
            handle, copy_size_usd=copy_size_usd,
            lookback_days=lookback_days,
        )
    elif platform == "polymarket":
        trades = _backtest_polymarket(
            handle, copy_size_usd=copy_size_usd,
            lookback_days=lookback_days,
        )
    else:
        raise ValueError(f"Unknown platform: {platform}")

    summary = _aggregate(
        trades, handle=handle, platform=platform,
        backtested_at=started.isoformat(),
        lookback_days=lookback_days, copy_size_usd=copy_size_usd,
    )

    out_path: Path | None = None
    if persist:
        out_path = _persist(summary, trades)

    log_event("wallet_backtest", "completed", {
        "handle": handle, "platform": platform,
        "lookback_days": lookback_days,
        "total_bets_seen": summary.total_bets_seen,
        "bets_copied": summary.bets_copied,
        "win_rate": summary.win_rate,
        "paper_pnl": summary.total_paper_pnl,
        "roi_pct": summary.roi_pct,
    })
    return summary, out_path


# ── Batch ranking ────────────────────────────────────────────────────

RANKING_DIR = Path(__file__).parent.parent / "data" / "wallet_backtest_rankings"


def backtest_all(
    *,
    copy_size_usd: float = DEFAULT_COPY_SIZE_USD,
    lookback_days: int = 90,
    min_settled: int = 5,
    progress_callback=None,
) -> dict:
    """Backtest every known wallet — scored Manifold handles +
    curated Polymarket addresses — and rank by backtested ROI.

    Reads:
      * ``data/wallet_scores.json``  — scored wallets from ``wallet-scan``
      * ``config/copytrade_wallets.yaml`` — curated additions

    Writes:
      * ``data/wallet_backtest_rankings/ranking_<ts>.json``

    Returns a dict with ``ranked`` (list of summaries sorted by ROI),
    ``failed`` (list of wallets that errored), and ``output_path``.

    ``min_settled`` filters out wallets with too few settled copyable
    bets to be statistically meaningful (default: 5). They still appear
    in the rollup file but are sorted to the bottom.
    """
    import yaml

    started = datetime.now(timezone.utc)
    root = Path(__file__).parent.parent

    # Collect targets — dedup by (handle.lower(), platform)
    targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    scores_path = root / "data" / "wallet_scores.json"
    if scores_path.exists():
        try:
            with open(scores_path) as f:
                scored = json.load(f).get("scores", [])
            for s in scored:
                h = s.get("handle")
                p = s.get("platform", "manifold")
                if h:
                    key = (h.lower(), p)
                    if key not in seen:
                        seen.add(key)
                        targets.append((h, p))
        except (json.JSONDecodeError, OSError):
            pass

    yaml_path = root / "config" / "copytrade_wallets.yaml"
    if yaml_path.exists():
        try:
            with open(yaml_path) as f:
                cfg = yaml.safe_load(f) or {}
            for p in ("manifold", "polymarket"):
                for h in (cfg.get(p) or []):
                    if isinstance(h, str) and h.strip():
                        key = (h.strip().lower(), p)
                        if key not in seen:
                            seen.add(key)
                            targets.append((h.strip(), p))
        except (yaml.YAMLError, OSError):
            pass

    if not targets:
        return {"ranked": [], "failed": [], "output_path": None,
                "n_targets": 0}

    results: list[dict] = []
    failed: list[dict] = []
    for i, (handle, platform) in enumerate(targets, 1):
        if progress_callback:
            progress_callback(i, len(targets), handle, platform)
        try:
            summary, _ = backtest_wallet(
                handle, platform=platform,
                copy_size_usd=copy_size_usd,
                lookback_days=lookback_days,
                persist=False,
            )
            results.append(asdict(summary))
        except Exception as e:
            failed.append({"handle": handle, "platform": platform,
                           "error": str(e)[:200]})
            log_event("wallet_backtest", "wallet_failed",
                      {"handle": handle, "platform": platform,
                       "error": str(e)[:200]}, result="degraded")

    # Rank by ROI, but thin samples (< min_settled) sink to the bottom
    # regardless of how good the ROI looks — three lucky wins on three
    # bets isn't signal.
    def _rank_key(r: dict) -> tuple:
        settled = r.get("wins", 0) + r.get("losses", 0)
        if settled < min_settled:
            return (0, r.get("roi_pct", 0.0))  # group 0 = below threshold
        return (1, r.get("roi_pct", 0.0))      # group 1 = above; both ROI-sorted

    ranked = sorted(results, key=_rank_key, reverse=True)

    RANKING_DIR.mkdir(parents=True, exist_ok=True)
    ts = started.strftime("%Y%m%d_%H%M%S")
    out_path = RANKING_DIR / f"ranking_{ts}.json"
    payload = {
        "computed_at": started.isoformat(),
        "lookback_days": lookback_days,
        "copy_size_usd": copy_size_usd,
        "min_settled_for_signal": min_settled,
        "n_targets": len(targets),
        "n_succeeded": len(results),
        "n_failed": len(failed),
        "ranked": ranked,
        "failed": failed,
    }
    tmp = out_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(out_path)

    log_event("wallet_backtest", "batch_completed", {
        "n_targets": len(targets),
        "n_succeeded": len(results),
        "n_failed": len(failed),
    })
    return {"ranked": ranked, "failed": failed,
            "output_path": out_path, "n_targets": len(targets)}
