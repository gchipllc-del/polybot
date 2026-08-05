#!/usr/bin/env python3
"""stage0_collector — the $0 test that decides the 15-min crypto restart.

Protocol (docs/CRYPTO15_RESTART.md, FROZEN 2026-08-04 — do NOT tune mid-sample; the old
composite died of iterative tuning):
  * Every cycle (60s), log EVERY open 15-min crypto market: both sides' bid/ask, volume,
    OI, spot, minutes-to-close. Every market, not just interesting ones — selection bias
    killed sleeves before.
  * Sweep settlements and log results. The join of first-touch price -> outcome is the
    entire experiment.
  * After n >= 1500 joined observations, `report` answers with a frozen price-bucket
    table: does ANY bucket's tradeable price differ from realized settlement frequency
    by more than friction? And does the final-2-min band lag the forming settlement?
    - If no bucket clears friction: no edge exists here; the restart moves venues.
    - If a bucket clears: THAT bucket, THAT side, maker-first, is the paper phase.

Runs anywhere python runs (Windows included: ASCII-only output, UTF-8 files, requests
only). Network calls are injectable for the selftest.

  py scripts/stage0_collector.py collect          # long-running loop (ctrl-c stops)
  py scripts/stage0_collector.py once             # single cycle (cron/Task Scheduler)
  py scripts/stage0_collector.py report
  py scripts/stage0_collector.py selftest
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = Path(os.environ.get("STAGE0_LOG") or (ROOT / "data" / "stage0_crypto.jsonl"))

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXBTC15M", "KXETH15M"]                    # frozen
SPOT_SYMBOL = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD"}
CYCLE_S = 60                                          # frozen
# Frozen price buckets (cents, inclusive lower edge) for the report join:
BUCKETS = [(1, 5), (5, 10), (10, 20), (20, 35), (35, 50),
           (50, 65), (65, 80), (80, 90), (90, 95), (95, 99)]
TIME_BANDS = [(10.0, 1e9, ">10min"), (2.0, 10.0, "2-10min"), (0.0, 2.0, "<2min")]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── network (thin, injectable) ───────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> dict:
    import requests
    r = requests.get(url, params=params or {}, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_open_markets(series: str, get=_get) -> list[dict]:
    """Events -> open markets for one series (the kalshi_15min_signal pattern)."""
    out = []
    ev = get(f"{KALSHI}/events", {"series_ticker": series, "status": "open",
                                  "limit": 200})
    for e in ev.get("events", []):
        et = e.get("event_ticker", "")
        if not et:
            continue
        ms = get(f"{KALSHI}/markets", {"event_ticker": et, "status": "open",
                                       "limit": 200})
        out.extend(ms.get("markets", []))
    return out


def fetch_settled(series: str, get=_get) -> list[dict]:
    ms = get(f"{KALSHI}/markets", {"series_ticker": series, "status": "settled",
                                   "limit": 200})
    return ms.get("markets", [])


def fetch_spot(symbol: str, get=_get) -> float | None:
    try:
        d = get(f"https://api.coinbase.com/v2/prices/{symbol}/spot")
        return float(d["data"]["amount"])
    except Exception:
        return None


# ── collection ───────────────────────────────────────────────────────────────

def _dollars(m: dict, name: str) -> float | None:
    v = m.get(f"{name}_dollars", m.get(name))
    if v is None:
        return None
    v = float(v)
    return v if v <= 1.0 else v / 100.0


def _mins_left(close_iso: str, now: datetime) -> float | None:
    try:
        c = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        return (c - now).total_seconds() / 60.0
    except (ValueError, TypeError):
        return None


def _seen_settles(path: Path) -> set:
    if not path.exists():
        return set()
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("t") == "settle":
            seen.add(d.get("ticker"))
    return seen


def run_cycle(get=_get, now: datetime | None = None, path: Path | None = None) -> dict:
    """One collection pass. Returns counters for the log line."""
    p = path or LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now(timezone.utc)
    rows, n_obs, n_settle = [], 0, 0
    seen = _seen_settles(p)

    for series in SERIES:
        spot = fetch_spot(SPOT_SYMBOL[series], get=get)
        try:
            markets = fetch_open_markets(series, get=get)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] discovery failed for {series}: {e}")
            continue
        for m in markets:
            rows.append({
                "t": "obs", "ts": now.isoformat(), "series": series,
                "ticker": m.get("ticker"), "strike": m.get("floor_strike"),
                "close_ts": m.get("close_time"),
                "mins_left": _mins_left(m.get("close_time") or "", now),
                "yes_bid": _dollars(m, "yes_bid"), "yes_ask": _dollars(m, "yes_ask"),
                "no_bid": _dollars(m, "no_bid"), "no_ask": _dollars(m, "no_ask"),
                "volume": m.get("volume"), "oi": m.get("open_interest"),
                "spot": spot,
            })
            n_obs += 1
        try:
            for m in fetch_settled(series, get=get):
                tk = m.get("ticker")
                if tk and tk not in seen and m.get("result") in ("yes", "no"):
                    rows.append({"t": "settle", "ts": now.isoformat(), "series": series,
                                 "ticker": tk, "result": m["result"]})
                    seen.add(tk)
                    n_settle += 1
        except Exception as e:  # noqa: BLE001
            print(f"[warn] settle sweep failed for {series}: {e}")

    if rows:
        with open(p, "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
    return {"obs": n_obs, "settles": n_settle}


def collect_loop() -> int:
    print(f"stage0 collector - every {CYCLE_S}s, series {SERIES} -> {LOG}")
    print("ctrl-c to stop. Frozen protocol: no parameter changes mid-sample.")
    while True:
        t0 = time.time()
        try:
            c = run_cycle()
            print(f"[{_now_iso()}] obs={c['obs']} new_settles={c['settles']}")
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[warn] cycle failed: {e}")
        time.sleep(max(1.0, CYCLE_S - (time.time() - t0)))


# ── report: the join that answers everything ─────────────────────────────────

def kalshi_taker_fee(price: float) -> float:
    import math
    if not (0 < price < 1):
        return 0.0
    return math.ceil(0.07 * price * (1.0 - price) * 100) / 100.0


def build_report(rows: list[dict]) -> dict:
    """First-obs-per-(ticker,band) price vs settlement outcome, frozen buckets."""
    settles = {r["ticker"]: r["result"] for r in rows if r.get("t") == "settle"}
    # first observation per ticker per time band (entry realism: the price you SAW first)
    first: dict[tuple, dict] = {}
    for r in rows:
        if r.get("t") != "obs" or r.get("mins_left") is None:
            continue
        band = next((b[2] for b in TIME_BANDS if b[0] <= r["mins_left"] < b[1]), None)
        if band is None:
            continue
        key = (r["ticker"], band)
        if key not in first:
            first[key] = r

    def bucket_of(price_c: float):
        return next((f"{lo:02d}-{hi:02d}c" for lo, hi in BUCKETS
                     if lo <= price_c < hi), None)

    # each joined obs contributes BOTH sides: buying YES at yes_ask, buying NO at no_ask
    table: dict[tuple, dict] = {}
    joined = 0
    for (ticker, band), r in first.items():
        result = settles.get(ticker)
        if result is None:
            continue
        joined += 1
        for side, ask, won in (("yes", r.get("yes_ask"), result == "yes"),
                               ("no", r.get("no_ask"), result == "no")):
            if ask is None or not (0 < ask < 1):
                continue
            b = bucket_of(ask * 100)
            if b is None:
                continue
            cell = table.setdefault((band, b), {"n": 0, "cost": 0.0, "wins": 0,
                                                "fee": 0.0})
            cell["n"] += 1
            cell["cost"] += ask
            cell["wins"] += 1 if won else 0
            cell["fee"] += kalshi_taker_fee(ask)
    return {"joined": joined, "n_settles": len(settles),
            "n_first_obs": len(first), "table": table}


def print_report(rep: dict) -> None:
    print(f"stage0 report - joined market-band observations: {rep['joined']} "
          f"(settled markets: {rep['n_settles']}, first-obs rows: {rep['n_first_obs']})")
    if rep["joined"] == 0:
        print("nothing joined yet - the collector needs to observe markets BEFORE they")
        print("settle. Leave `collect` running; check back after a few hours.")
        return
    if rep["joined"] < 1500:
        print(f"NOTE: n < 1500 - directional reads only, NO verdict yet (frozen protocol).")
    print()
    print("band     bucket   n     avg_cost  realized  gap      fee   verdict")
    for band in [b[2] for b in TIME_BANDS]:
        for lo, hi in BUCKETS:
            b = f"{lo:02d}-{hi:02d}c"
            cell = rep["table"].get((band, b))
            if not cell or cell["n"] == 0:
                continue
            n = cell["n"]
            cost = cell["cost"] / n
            real = cell["wins"] / n
            fee = cell["fee"] / n
            gap = real - cost           # >0: buying this side at this price was +EV gross
            verdict = "edge?" if gap - fee > 0.01 and n >= 100 else \
                      ("thin-n" if n < 100 else "no")
            print(f"{band:8} {b:8} {n:5} {cost:8.3f} {real:9.3f} {gap:+8.3f} "
                  f"{fee:5.2f}  {verdict}")
    print()
    print("gap = realized win freq - avg cost (per $1 contract), BEFORE fees.")
    print("A bucket only matters if gap - fee stays positive at n >= 100+ AND survives")
    print("the maker/adverse-selection questions in docs/CRYPTO15_RESTART.md.")


# ── selftest (no network) ────────────────────────────────────────────────────

def _selftest() -> int:
    from datetime import timedelta
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

    def fake_get(url, params=None):
        params = params or {}
        if url.endswith("/events"):
            return {"events": [{"event_ticker": "EV1"}]}
        if url.endswith("/markets") and "event_ticker" in params:
            return {"markets": [{
                "ticker": "KXBTC15M-TEST-1", "floor_strike": 64000.0,
                "close_time": (now + timedelta(minutes=5)).isoformat(),
                "yes_ask_dollars": 0.07, "yes_bid_dollars": 0.03,
                "no_ask_dollars": 0.95, "no_bid_dollars": 0.91,
                "volume": 10, "open_interest": 5}]}
        if url.endswith("/markets"):    # settled sweep
            return {"markets": [{"ticker": "KXBTC15M-TEST-0", "result": "no"}]}
        if "coinbase" in url:
            return {"data": {"amount": "64000.00"}}
        raise AssertionError(f"unexpected url {url}")

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s0.jsonl"
        c = run_cycle(get=fake_get, now=now, path=p)
        # 2 series x 1 market; the fixture serves the SAME settled ticker to both
        # series, and the in-cycle seen-set correctly dedupes it to one settle row.
        assert c == {"obs": 2, "settles": 1}, c
        rows = [json.loads(l) for l in p.read_text().splitlines()]
        obs = [r for r in rows if r["t"] == "obs"]
        assert obs[0]["yes_ask"] == 0.07 and obs[0]["mins_left"] == 5.0
        # second cycle must not duplicate the settle (seen-set)
        c2 = run_cycle(get=fake_get, now=now, path=p)
        assert c2["settles"] == 0, c2

        # report join: make the observed market settle 'no' -> buying NO at 0.95 won,
        # buying YES at 0.07 lost.
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": "settle", "ticker": "KXBTC15M-TEST-1",
                                "result": "no"}) + "\n")
        rep = build_report([json.loads(l) for l in p.read_text().splitlines()])
        assert rep["joined"] >= 1
        yes_cell = rep["table"].get(("2-10min", "05-10c"))
        no_cell = rep["table"].get(("2-10min", "95-99c"))
        assert yes_cell and yes_cell["wins"] == 0        # YES @7c lost
        assert no_cell and no_cell["wins"] == no_cell["n"]  # NO @95c won
    assert kalshi_taker_fee(0.07) == 0.01
    print("selftest OK")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "collect":
        return collect_loop()
    if cmd == "once":
        c = run_cycle()
        print(f"obs={c['obs']} new_settles={c['settles']} -> {LOG}")
        return 0
    if cmd == "report":
        rows = []
        if LOG.exists():
            for line in LOG.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        print_report(build_report(rows))
        return 0
    if cmd == "selftest":
        return _selftest()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
