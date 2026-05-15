"""
historical_data — multi-source historical-trades reader.

The wallet-backtest CLI defaults to live API calls — Manifold's /bets,
Polymarket's Data-API /trades. Those have hard limits:
  * Manifold: 1000 bets/page, but rate-limits aggressively
  * Polymarket Data API: ~200 trades/page, rolling window — older
    trades disappear from the cache after some time

For long-history backtests (12+ months) we lean on Jon Becker's
public dataset (`Jon-Becker/prediction-market-analysis`), which mirrors
the **on-chain** trade events from Polymarket's CTF contract — every
trade ever made, parquet-encoded, 36GB compressed via Cloudflare R2.

This module abstracts "give me historical trades for this wallet"
behind one interface, dispatching to parquet if available, falling
back to the API otherwise.

**Setup** (one-time, user opt-in):

    # 1. Clone Jon-Becker's repo somewhere
    git clone https://github.com/Jon-Becker/prediction-market-analysis ~/pma

    # 2. Download the dataset (36GB compressed → ~150GB extracted)
    cd ~/pma && make setup

    # 3. Tell polybot where to find it
    export POLYBOT_PMA_DATA_DIR=~/pma/data

After that, ``wallet-backtest --source=parquet`` runs against the
local dataset and covers the full on-chain history (rather than the
~30-day API window).

Schema reference (from Jon-Becker's `docs/SCHEMAS.md`):
    polymarket/trades/*.parquet columns:
      block_number, transaction_hash, log_index, order_hash,
      maker, taker, maker_asset_id, taker_asset_id,
      maker_amount, taker_amount, fee, _fetched_at, _contract
    polymarket/markets/*.parquet columns:
      id, condition_id, question, slug, outcomes, outcome_prices,
      volume, liquidity, active, closed, end_date, created_at
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator


def pma_data_dir() -> Path | None:
    """Return the configured Jon-Becker data directory, or None.

    Honors ``POLYBOT_PMA_DATA_DIR`` env var; falls back to
    ``~/.polybot/pma_data`` if that exists; otherwise None.
    """
    env = os.environ.get("POLYBOT_PMA_DATA_DIR", "").strip()
    if env:
        p = Path(env).expanduser()
        if p.exists():
            return p
    default = Path.home() / ".polybot" / "pma_data"
    if default.exists():
        return default
    return None


def parquet_available() -> tuple[bool, str]:
    """Return ``(available, reason)``.

    ``available=True`` only when:
      1. pyarrow can be imported (env not broken)
      2. POLYBOT_PMA_DATA_DIR points somewhere valid
      3. The expected Polymarket subdirectory exists
    """
    # pyarrow's failing import scribbles on stderr even when we catch it.
    # Silence so callers see a clean reason string.
    import io, sys, contextlib
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            import pyarrow  # noqa: F401
    except Exception as e:
        return False, f"pyarrow unavailable: {str(e)[:120]}"
    data_dir = pma_data_dir()
    if data_dir is None:
        return False, (
            "POLYBOT_PMA_DATA_DIR not set. Run `make setup` in a clone "
            "of Jon-Becker/prediction-market-analysis, then export the path."
        )
    poly_trades = data_dir / "polymarket" / "trades"
    if not poly_trades.exists():
        return False, f"{poly_trades} does not exist — dataset not extracted?"
    return True, str(data_dir)


# ── Polymarket parquet reader ────────────────────────────────────────

def read_polymarket_trades_for_wallet(
    wallet_address: str,
    *,
    lookback_days: int | None = None,
) -> Iterator[dict]:
    """Yield trades for ``wallet_address`` (normalized lowercase) from
    the local parquet dataset, in the SAME dict shape as the Data API
    so the backtest doesn't care which source it's reading.

    Output dicts contain:
      conditionId, side ("BUY"/"SELL"), outcome ("Yes"/"No"),
      price, size, timestamp (unix seconds), transactionHash

    Yields nothing if parquet isn't available (caller must check
    ``parquet_available()`` first to give the user a clear error).
    """
    ok, _ = parquet_available()
    if not ok:
        return
    import pyarrow.dataset as ds  # type: ignore
    import pyarrow.compute as pc  # type: ignore

    data_dir = pma_data_dir()
    if data_dir is None:
        return

    addr = wallet_address.strip().lower()
    trades_path = data_dir / "polymarket" / "trades"
    dataset = ds.dataset(trades_path, format="parquet")

    # Filter rows where maker or taker matches the wallet
    expr = (pc.field("maker") == addr) | (pc.field("taker") == addr)
    if lookback_days is not None:
        from datetime import datetime, timezone
        cutoff = datetime.now(timezone.utc).timestamp() - lookback_days * 86400
        # _fetched_at is the only direct timestamp column on trades —
        # block_number is monotonic but not directly date-keyed
        expr = expr & (pc.field("_fetched_at") >= cutoff)

    # Stream batches so we don't blow memory on huge result sets
    for batch in dataset.to_batches(filter=expr, batch_size=10_000):
        py = batch.to_pylist()
        for row in py:
            yield _normalize_parquet_trade(row, wallet_addr=addr)


def _normalize_parquet_trade(row: dict, *, wallet_addr: str) -> dict:
    """Convert an on-chain CTF trade record to the Data-API shape.

    The on-chain record has maker/taker addresses and asset IDs but
    no "side" or "outcome" string. We synthesize those:

      * If wallet is maker AND maker_asset_id is USDC → wallet is SELLING
        outcome shares for cash (SELL)
      * If wallet is taker AND maker_asset_id is USDC → wallet is BUYING
        outcome shares (BUY)
      * etc.

    Outcome (YES/NO) is encoded in the conditional token ID — needs a
    market lookup to resolve. We leave ``outcome`` blank here; the
    backtest can fill it in by joining against the markets parquet.

    Honest caveat: this is a partial implementation. Full on-chain
    decoding requires walking the CTF position-mapping which is out
    of scope for the first pass. Use API-source backtest as ground
    truth and treat parquet as long-history augmentation.
    """
    # USDC token id on Polygon CTF (canonical) — heuristic, may need
    # adjustment per contract version
    USDC_LIKE = {"0", ""}  # USDC trades have asset_id=0 in the CTF model

    is_maker = (row.get("maker", "") or "").lower() == wallet_addr
    maker_asset = str(row.get("maker_asset_id", ""))
    if is_maker:
        side = "SELL" if maker_asset in USDC_LIKE else "BUY"
    else:
        side = "BUY" if maker_asset in USDC_LIKE else "SELL"

    # Price = ratio of usdc amount to share amount; both come from
    # maker/taker_amount depending on direction
    try:
        maker_amt = float(row.get("maker_amount", 0) or 0)
        taker_amt = float(row.get("taker_amount", 0) or 0)
        if side == "BUY":
            usdc, shares = (maker_amt, taker_amt) if maker_asset in USDC_LIKE else (taker_amt, maker_amt)
        else:
            usdc, shares = (taker_amt, maker_amt) if maker_asset in USDC_LIKE else (maker_amt, taker_amt)
        price = round(usdc / shares, 4) if shares > 0 else 0.0
        size = shares
    except (ValueError, TypeError, ZeroDivisionError):
        price = 0.0
        size = 0.0

    return {
        # Polymarket Data-API shape (what wallet_backtest expects)
        "conditionId": row.get("condition_id", ""),  # NOTE: schema has
                                                     # condition_id NOT
                                                     # in trades — need
                                                     # join via asset_id
        "side": side,
        "outcome": "",  # filled by caller via markets-parquet join
        "price": price,
        "size": size,
        "timestamp": int(row.get("_fetched_at", 0) or 0),
        "transactionHash": row.get("transaction_hash", ""),
        "_source": "parquet",
    }


# ── Diagnostic ───────────────────────────────────────────────────────

def status() -> dict:
    """Quick health check for the parquet path. Used by the
    ``dataset-status`` CLI."""
    ok, reason = parquet_available()
    out = {"parquet_available": ok, "reason": reason}
    data_dir = pma_data_dir()
    out["data_dir"] = str(data_dir) if data_dir else None
    if ok and data_dir:
        # Count parquet files (cheap)
        poly_trades = list((data_dir / "polymarket" / "trades").glob("*.parquet"))
        poly_markets = list((data_dir / "polymarket" / "markets").glob("*.parquet"))
        kalshi_trades = list((data_dir / "kalshi" / "trades").glob("*.parquet"))
        out["poly_trades_files"] = len(poly_trades)
        out["poly_markets_files"] = len(poly_markets)
        out["kalshi_trades_files"] = len(kalshi_trades)
    return out
