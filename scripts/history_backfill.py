#!/usr/bin/env python3
"""history_backfill — months of Kalshi 15-min market history via public endpoints.

Source: the ccxt Kalshi integration (MIT, verified at source level in our repo scan)
wraps GET /series/{series_ticker}/markets/{ticker}/candlesticks with period_interval
(1 = 1-minute) and start_ts/end_ts, and GET /markets supports status=settled with cursor
pagination. Both are PUBLIC (no auth). If candle retention reaches back months, this
multiplies our dataset ~20x for free: per-minute yes_bid/yes_ask paths + settlement
results for every expired KXBTC15M/KXETH15M window since the series listed.

GATED, because retention depth is the one thing the scan could not verify remotely:
  probe    fetch ONE old settled market's candles and show exactly what came back.
           Run this FIRST; if it returns empty for old tickers, stop - no backfill exists.
  run      full backfill: enumerate settled markets (paginated), pull 1-min candles per
           window, append market+candle rows to data/history_backfill.jsonl (resumable -
           already-fetched tickers are skipped).
  export   convert backfilled candles into stage0-format obs+settle rows
           (data/stage0_backfill.jsonl) so edge_analysis / maker_replay can be pointed
           at deep history via the STAGE0_LOG env var. Kept SEPARATE from the live
           collector's file on purpose: candle-derived quotes are minute-OHLC
           approximations, not our snapshot quality - never silently mix provenances.
  selftest fixtures only, no network.

  py scripts/history_backfill.py probe
  py scripts/history_backfill.py run --series KXBTC15M --max-markets 500
  py scripts/history_backfill.py export

Per-venue screening: scope BOTH files by env var so venues never mix -
  $env:BACKFILL_LOG="data\backfill_weather.jsonl"
  $env:BACKFILL_EXPORT="data\stage0_weather.jsonl"
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXBTC15M", "KXETH15M"]
OUT = Path(os.environ.get("BACKFILL_LOG") or (ROOT / "data" / "history_backfill.jsonl"))
EXPORT = Path(os.environ.get("BACKFILL_EXPORT")
              or (ROOT / "data" / "stage0_backfill.jsonl"))
SLEEP_S = 0.35              # polite pacing; these are public endpoints


def _get(url: str, params: dict | None = None) -> dict:
    import requests
    r = requests.get(url, params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


def _iso_to_ts(iso: str) -> int | None:
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return None


def fetch_settled_markets(series: str, get=_get, max_markets: int = 10000) -> list[dict]:
    """All settled markets for a series, cursor-paginated, oldest data included."""
    out, cursor = [], None
    while len(out) < max_markets:
        params = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get(f"{KALSHI}/markets", params)
        ms = data.get("markets", [])
        out.extend(ms)
        cursor = data.get("cursor")
        if not cursor or not ms:
            break
        time.sleep(SLEEP_S)
    return out[:max_markets]


def fetch_candles(series: str, ticker: str, open_iso: str, close_iso: str,
                  get=_get) -> list[dict]:
    """1-minute candles across the market's life. Params per the verified ccxt shape."""
    start = _iso_to_ts(open_iso)
    end = _iso_to_ts(close_iso)
    if start is None or end is None:
        return []
    data = get(f"{KALSHI}/series/{series}/markets/{ticker}/candlesticks",
               {"period_interval": 1, "start_ts": start, "end_ts": end})
    return data.get("candlesticks", []) or data.get("candles", []) or []


def _seen_tickers(path: Path) -> set:
    seen = set()
    if not path.exists():
        return seen
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("t") == "market":
            seen.add(d.get("ticker"))
    return seen


def _dollars(c: dict, *names):
    """Candle price fields arrive as nested OHLC dicts or scalars, cents or dollars,
    under bare or _dollars names (ccxt-verified variability). Returns close-ish value."""
    for n in names:
        v = c.get(f"{n}_dollars", c.get(n))
        if isinstance(v, dict):
            v = v.get("close_dollars", v.get("close"))
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        return v if v <= 1.0 else v / 100.0
    return None


def cmd_probe(get=_get) -> int:
    print("=" * 72)
    print("BACKFILL PROBE - does Kalshi retain candles for long-settled markets?")
    print("=" * 72)
    # RETENTION LADDER. The first probe version enumerated newest-first and stopped at
    # its own 1200-market cap, so "oldest enumerated" (13 days) measured the CAP, not
    # retention. This asks the question directly: request settled markets CLOSED BEFORE
    # 30/60/90/150 days ago via max_close_ts and try candles on one from each rung.
    now_ts = int(time.time())
    for series in SERIES[:1]:                     # one series is enough for the ladder
        for days in (30, 60, 90, 150):
            try:
                data = get(f"{KALSHI}/markets",
                           {"series_ticker": series, "status": "settled", "limit": 5,
                            "max_close_ts": now_ts - days * 86400})
                ms = data.get("markets", [])
            except Exception as e:  # noqa: BLE001
                print(f"{series} closed>{days}d ago: enumeration FAILED ({str(e)[:80]})")
                continue
            if not ms:
                print(f"{series} closed>{days}d ago: no markets returned "
                      f"(series may be younger, or param unsupported)")
                continue
            m = ms[0]
            try:
                cs = fetch_candles(series, m["ticker"], m.get("open_time"),
                                   m.get("close_time"), get=get)
                print(f"{series} closed>{days}d ago: {m['ticker']} "
                      f"(close {str(m.get('close_time'))[:10]}) -> {len(cs)} candles")
            except Exception as e:  # noqa: BLE001
                print(f"{series} closed>{days}d ago: {m['ticker']} candle fetch "
                      f"FAILED ({str(e)[:80]})")
            time.sleep(SLEEP_S)
    print()
    for series in SERIES:
        try:
            ms = fetch_settled_markets(series, get=get, max_markets=1200)
        except Exception as e:  # noqa: BLE001
            print(f"{series}: settled-market enumeration FAILED: {e}")
            continue
        if not ms:
            print(f"{series}: no settled markets returned")
            continue
        ms.sort(key=lambda m: m.get("close_time") or "")
        oldest, newest = ms[0], ms[-1]
        print(f"{series}: {len(ms)} settled markets enumerated "
              f"({str(oldest.get('close_time'))[:10]} .. {str(newest.get('close_time'))[:10]})")
        for label, m in (("OLDEST", oldest), ("NEWEST", newest)):
            tk = m.get("ticker")
            try:
                cs = fetch_candles(series, tk, m.get("open_time"), m.get("close_time"),
                                   get=get)
            except Exception as e:  # noqa: BLE001
                print(f"  {label} {tk}: candle fetch FAILED: {e}")
                continue
            print(f"  {label} {tk} (result={m.get('result')}): {len(cs)} one-minute candles")
            if cs:
                sample = {k: cs[0][k] for k in list(cs[0])[:6]}
                print(f"    sample fields: {sample}")
        print()
    print("READ: if the OLDEST market returns candles, deep backfill is real - run `run`.")
    print("If oldest returns 0 but newest works, retention is shallow; backfill only helps")
    print("a little. If both fail, the endpoint shape changed - paste this output back.")
    return 0


def cmd_run(series_list: list[str], max_markets: int, get=_get) -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    seen = _seen_tickers(OUT)
    print(f"backfill -> {OUT} (resumable; {len(seen)} tickers already stored)")
    total_c = 0
    for series in series_list:
        ms = fetch_settled_markets(series, get=get, max_markets=max_markets)
        todo = [m for m in ms if m.get("ticker") and m["ticker"] not in seen
                and m.get("result") in ("yes", "no")]
        print(f"{series}: {len(ms)} settled, {len(todo)} to fetch")
        with open(OUT, "a", encoding="utf-8") as f:
            for i, m in enumerate(todo):
                tk = m["ticker"]
                try:
                    cs = fetch_candles(series, tk, m.get("open_time"),
                                       m.get("close_time"), get=get)
                except Exception as e:  # noqa: BLE001
                    print(f"  [warn] {tk}: {e}")
                    continue
                f.write(json.dumps({"t": "market", "series": series, "ticker": tk,
                                    "result": m.get("result"),
                                    "open_time": m.get("open_time"),
                                    "close_time": m.get("close_time"),
                                    "floor_strike": m.get("floor_strike"),
                                    "volume": m.get("volume")},
                                   separators=(",", ":")) + "\n")
                for c in cs:
                    f.write(json.dumps({"t": "candle", "series": series, "ticker": tk,
                                        **c}, separators=(",", ":")) + "\n")
                total_c += len(cs)
                if (i + 1) % 25 == 0:
                    print(f"  {i+1}/{len(todo)} markets, {total_c} candles")
                time.sleep(SLEEP_S)
    print(f"done. total new candles: {total_c}")
    return 0


def cmd_export() -> int:
    """Backfill rows -> stage0-format obs+settle, provenance-tagged, separate file."""
    if not OUT.exists():
        print(f"nothing to export - run backfill first ({OUT} missing)")
        return 1
    markets: dict[str, dict] = {}
    candles: dict[str, list] = {}
    for line in OUT.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("t") == "market":
            markets[d["ticker"]] = d
        elif d.get("t") == "candle":
            candles.setdefault(d["ticker"], []).append(d)
    n_obs = n_set = 0
    with open(EXPORT, "w", encoding="utf-8") as f:
        for tk, m in markets.items():
            close_ts = _iso_to_ts(m.get("close_time"))
            if close_ts is None:
                continue
            for c in sorted(candles.get(tk, []),
                            key=lambda c: c.get("end_period_ts") or 0):
                ts = c.get("end_period_ts")
                if ts is None:
                    continue
                yb = _dollars(c, "yes_bid")
                ya = _dollars(c, "yes_ask")
                if yb is None and ya is None:
                    continue
                mins_left = (close_ts - float(ts)) / 60.0
                if mins_left <= 0:
                    continue
                f.write(json.dumps({
                    "t": "obs", "src": "backfill",
                    "ts": datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat(),
                    "series": m.get("series"), "ticker": tk,
                    "strike": m.get("floor_strike"),
                    "mins_left": round(mins_left, 2),
                    "yes_bid": yb, "yes_ask": ya,
                    "no_bid": (round(1 - ya, 4) if ya is not None else None),
                    "no_ask": (round(1 - yb, 4) if yb is not None else None),
                }, separators=(",", ":")) + "\n")
                n_obs += 1
            f.write(json.dumps({"t": "settle", "src": "backfill", "ticker": tk,
                                "series": m.get("series"), "result": m.get("result")},
                               separators=(",", ":")) + "\n")
            n_set += 1
    print(f"exported {n_obs} obs + {n_set} settles -> {EXPORT}")
    print("Analyze deep history WITHOUT touching the live file, e.g. (PowerShell):")
    print('  $env:STAGE0_LOG="data\\stage0_backfill.jsonl"; py scripts\\edge_analysis.py')
    print("NOTE: no-side quotes here are derived as 1 - yes quote (candles carry yes only)")
    print("      and quotes are minute-OHLC closes, coarser than the live collector.")
    return 0


def _selftest() -> int:
    calls = []

    def fake_get(url, params=None):
        calls.append((url, params or {}))
        if url.endswith("/markets"):
            cur = (params or {}).get("cursor")
            if cur is None:
                return {"markets": [{"ticker": "KXBTC15M-A", "result": "yes",
                                     "open_time": "2026-06-01T12:00:00Z",
                                     "close_time": "2026-06-01T12:15:00Z",
                                     "floor_strike": 64000.0, "volume": 50}],
                        "cursor": "c2"}
            return {"markets": [{"ticker": "KXBTC15M-B", "result": "no",
                                 "open_time": "2026-06-01T12:15:00Z",
                                 "close_time": "2026-06-01T12:30:00Z",
                                 "floor_strike": 64100.0, "volume": 10}],
                    "cursor": None}
        if "candlesticks" in url:
            assert (params or {}).get("period_interval") == 1
            return {"candlesticks": [
                {"end_period_ts": 1780315200,
                 "yes_bid": {"close": 82}, "yes_ask": {"close": 86}},
                {"end_period_ts": 1780315260,
                 "yes_bid": {"close": 84}, "yes_ask": {"close": 88}},
            ]}
        raise AssertionError(url)

    ms = fetch_settled_markets("KXBTC15M", get=fake_get)
    assert [m["ticker"] for m in ms] == ["KXBTC15M-A", "KXBTC15M-B"], ms   # pagination
    cs = fetch_candles("KXBTC15M", "KXBTC15M-A",
                       "2026-06-01T12:00:00Z", "2026-06-01T12:15:00Z", get=fake_get)
    assert len(cs) == 2
    # cents -> dollars via nested close
    assert _dollars(cs[0], "yes_bid") == 0.82 and _dollars(cs[0], "yes_ask") == 0.86
    # flat dollar variants too
    assert _dollars({"yes_bid_dollars": 0.5}, "yes_bid") == 0.5
    assert _dollars({"yes_bid": 45}, "yes_bid") == 0.45

    # run + export round-trip through temp files
    import tempfile
    global OUT, EXPORT
    old_out, old_exp = OUT, EXPORT
    with tempfile.TemporaryDirectory() as td:
        OUT, EXPORT = Path(td) / "bf.jsonl", Path(td) / "exp.jsonl"
        try:
            cmd_run(["KXBTC15M"], max_markets=10, get=fake_get)
            rows = [json.loads(l) for l in OUT.read_text().splitlines()]
            assert sum(1 for r in rows if r["t"] == "market") == 2
            assert sum(1 for r in rows if r["t"] == "candle") == 4
            # resume: second run fetches nothing new
            n_before = len(rows)
            cmd_run(["KXBTC15M"], max_markets=10, get=fake_get)
            assert len(OUT.read_text().splitlines()) == n_before
            cmd_export()
            exp = [json.loads(l) for l in EXPORT.read_text().splitlines()]
            obs = [r for r in exp if r["t"] == "obs"]
            setl = [r for r in exp if r["t"] == "settle"]
            assert setl and all(r["src"] == "backfill" for r in exp)
            assert obs and obs[0]["yes_bid"] == 0.82 and obs[0]["no_ask"] == 0.18
            assert all(o["mins_left"] > 0 for o in obs)
        finally:
            OUT, EXPORT = old_out, old_exp
    print("selftest OK")
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "probe"
    if cmd == "selftest":
        return _selftest()
    if cmd == "probe":
        return cmd_probe()
    if cmd == "run":
        series = SERIES
        mm = 10000
        if "--series" in args:
            series = [args[args.index("--series") + 1]]
        if "--max-markets" in args:
            mm = int(args[args.index("--max-markets") + 1])
        return cmd_run(series, mm)
    if cmd == "export":
        return cmd_export()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
