"""Read-only Kalshi PERPS funding + basis logger — Phase 0 "measure-first".

Appends, to data/perps_funding_log.jsonl, two record types per run:
  * type="funding"  — each REALIZED 8h funding payment (dedup'd by ticker+time),
                      so we build the funding series robustly even if the API's
                      /historical retention is short.
  * type="snapshot" — per-run basis/liquidity: est funding, mark, implied perp
                      price, spot, basis%, bid/ask, open interest, 24h volume.

PURE MEASUREMENT — there is NO trading path here (no place_order import). It only
issues read-only GETs to /trade-api/v2/margin/* and writes a local log. The goal
is the decision GATE in memory perps_roadmap.md: is the funding edge real (after
hedge cost), or near-zero? Built 2026-06-04.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from lib.kalshi_client import KalshiClient

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "data" / "perps_funding_log.jsonl"
# Perps we can compute a basis for (need a spot reference). Funding itself is
# logged for ALL perps regardless.
SPOT_SYM = {"KXBTCPERP": "BTCUSDT", "KXETHPERP": "ETHUSDT"}
BASE = "/trade-api/v2/margin"


def _load() -> list[dict]:
    out: list[dict] = []
    if LOG.exists():
        for ln in LOG.read_text().splitlines():
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    return out


def _append(records: list[dict]) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def log_once(throttle_min: int = 55) -> dict:
    """Fetch + append one round of funding/snapshot rows. Self-throttles so it's
    safe to call from a 5-min cron (funding only updates every 8h)."""
    now = datetime.now(timezone.utc)
    existing = _load()

    # Throttle: skip if we logged a snapshot < throttle_min ago.
    snap_ts = [r.get("run_ts", "") for r in existing if r.get("type") == "snapshot"]
    if snap_ts:
        last = max(snap_ts)
        try:
            if last and (now - datetime.fromisoformat(last)).total_seconds() < throttle_min * 60:
                return {"status": "throttled", "last": last}
        except Exception:
            pass

    seen = {(r.get("ticker"), r.get("funding_time"))
            for r in existing if r.get("type") == "funding"}
    kc = KalshiClient()

    def get(path):
        return kc._signed_request(method="GET", path=path)

    try:
        mk = get(f"{BASE}/markets?limit=50")
    except Exception as e:
        return {"status": "error", "error": str(e)[:180]}
    markets = mk.get("markets", []) if isinstance(mk, dict) else []

    spot_cache: dict = {}
    def spot(sym):
        if sym not in spot_cache:
            try:
                from lib.kalshi_daily_signal import _fetch_spot
                spot_cache[sym] = _fetch_spot(sym)
            except Exception:
                spot_cache[sym] = None
        return spot_cache[sym]

    run_ts = now.isoformat()
    new: list[dict] = []
    n_fund = 0
    for m in markets:
        tk = m.get("ticker")
        if not tk:
            continue
        cs = _f(m.get("contract_size"))
        try:
            est = get(f"{BASE}/funding_rates/estimate?ticker={tk}")
        except Exception:
            est = {}
        mark = _f(est.get("mark_price"))
        sp = spot(SPOT_SYM[tk]) if tk in SPOT_SYM else None
        implied = (mark / cs) if (mark is not None and cs) else None
        basis_pct = ((implied / sp - 1) * 100) if (implied and sp) else None
        new.append({
            "type": "snapshot", "run_ts": run_ts, "ticker": tk,
            "est_funding_rate": est.get("funding_rate"),
            "mark_price": mark, "contract_size": cs,
            "implied_perp_usd": round(implied, 2) if implied else None,
            "spot_usd": sp,
            "basis_pct": round(basis_pct, 4) if basis_pct is not None else None,
            "bid": m.get("bid"), "ask": m.get("ask"),
            "open_interest": m.get("open_interest"),
            "volume_24h": m.get("volume_24h"),
            "next_funding_time": est.get("next_funding_time"),
        })
        # realized funding history (dedup-append new periods)
        try:
            h = get(f"{BASE}/funding_rates/historical?ticker={tk}&limit=30")
            for fr in (h.get("funding_rates", []) if isinstance(h, dict) else []):
                ft = fr.get("funding_time")
                if ft and (tk, ft) not in seen:
                    seen.add((tk, ft))
                    new.append({
                        "type": "funding", "ticker": tk, "funding_time": ft,
                        "funding_rate": fr.get("funding_rate"),
                        "mark_price": fr.get("mark_price"), "logged_at": run_ts,
                    })
                    n_fund += 1
        except Exception:
            pass

    _append(new)
    return {"status": "logged", "perps": len(markets),
            "new_funding_pts": n_fund, "run_ts": run_ts}


def report() -> None:
    rows = _load()
    funding = [r for r in rows if r.get("type") == "funding"]
    snaps = [r for r in rows if r.get("type") == "snapshot"]
    byt: dict[str, list] = defaultdict(list)
    for r in funding:
        byt[r["ticker"]].append(r)
    span = ""
    if snaps:
        ts = sorted(r.get("run_ts", "") for r in snaps)
        span = f"{ts[0][:16]} → {ts[-1][:16]}"
    print(f"=== PERPS FUNDING LOG — {len(funding)} realized funding pts, "
          f"{len(snaps)} snapshots  [{span}] ===")
    print(f"{'ticker':14s} {'n':>3s} {'%≠0':>5s} {'mean/8h%':>9s} "
          f"{'ann%*':>7s} {'last_basis%':>11s}  (*if it persisted)")
    for tk in sorted(byt):
        fr = [float(r.get("funding_rate") or 0) for r in byt[tk]]
        nz = sum(1 for x in fr if abs(x) > 1e-9)
        mean = sum(fr) / len(fr) if fr else 0.0
        ann = mean * 3 * 365 * 100  # 3 funding periods/day
        sb = [r for r in snaps if r["ticker"] == tk and r.get("basis_pct") is not None]
        lb = f"{sb[-1]['basis_pct']:+.3f}" if sb else "--"
        print(f"{tk:14s} {len(fr):3d} {100*nz/len(fr) if fr else 0:4.0f}% "
              f"{mean*100:+8.4f}% {ann:+6.1f}% {lb:>11}")
    print("\nGATE: if mean/8h ≈ 0 and %≠0 is low across perps → no funding edge "
          "to capture (don't build). Revisit when funding turns consistently nonzero.")
