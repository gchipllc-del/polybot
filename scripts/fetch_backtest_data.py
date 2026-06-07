#!/usr/bin/env python3
"""Pull as much backtest data as possible into one normalized place.

Sources (run any subset, or `all`):

  becker      Guide/normalize the Jon-Becker dataset — the largest public
              Kalshi+Polymarket market & trade history. The bulk download
              (36 GiB) is done by Becker's own `make setup`; this command
              prints those steps, then NORMALIZES the resulting parquet into
              the JSONL schema becker_edge.py / build_trials.py expect.

  kalshi      Pull fresh SETTLED Kalshi markets straight from the live API
              (residential IP only — datacenters are geo-blocked). Good for
              recent markets not yet in the Becker snapshot.

  openmeteo   For each weather city the bot trades, pull the historical
              FORECAST (what the forecast SAID at the time — no look-ahead)
              plus the actual observed max/min, for weather settlement.

Outputs land in data/backtest/ as JSONL:
  becker_kalshi_all.jsonl      every settled Kalshi market   (becker_edge input)
  becker_kalshi_weather.jsonl  weather-only subset
  kalshi_settled.jsonl         fresh live-API settled markets
  kalshi_settled_weather.jsonl weather-only subset
  weather_truth.jsonl          per-city/day forecast + actual highs/lows

Then, e.g.:
  python scripts/becker_edge.py data/backtest/becker_kalshi_weather.jsonl \\
      --price-col market_p_yes --result-col result --time-col sample_at \\
      --market-col market_ticker --sweep --inspect
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "backtest"

# Weather series prefixes Kalshi uses (hourly KXTEMP*, daily KXHIGHT*/KXLOWT*).
WEATHER_PREFIXES = ("KXTEMP", "KXHIGHT", "KXLOWT", "KXHIGH", "KXLOW")

BECKER_REPO = "https://github.com/jon-becker/prediction-market-analysis"


# ── City registries (import the live ones; fall back to an embedded copy so
#    this script stays standalone even if the bot modules can't import) ──────
def _cities() -> dict:
    cities: dict = {}
    try:
        from lib.weather_signal import CITIES as HOURLY
        cities.update({k: {**v, "scope": "hourly"} for k, v in HOURLY.items()})
    except Exception:
        cities.update({
            "nyc":     {"series": "KXTEMPNYCH", "lat": 40.7831, "lon": -73.9712, "label": "NYC (Central Park)", "scope": "hourly"},
            "chicago": {"series": "KXTEMPCHIH", "lat": 41.9742, "lon": -87.9073, "label": "Chicago (O'Hare)", "scope": "hourly"},
            "dc":      {"series": "KXTEMPDCH",  "lat": 38.8512, "lon": -77.0402, "label": "DC (Reagan)", "scope": "hourly"},
            "boston":  {"series": "KXTEMPBOSH", "lat": 42.3656, "lon": -71.0096, "label": "Boston (Logan)", "scope": "hourly"},
            "lax":     {"series": "KXTEMPLAXH", "lat": 33.9425, "lon": -118.4081, "label": "LA (LAX)", "scope": "hourly"},
            "miami":   {"series": "KXTEMPMIAH", "lat": 25.7959, "lon": -80.2870, "label": "Miami (Intl)", "scope": "hourly"},
        })
    try:
        from lib.weather_daily_signal import DAILY_CITIES as DAILY
        cities.update({k: {**v, "scope": "daily"} for k, v in DAILY.items()})
    except Exception:
        cities.update({
            "dal_high": {"series": "KXHIGHTDAL", "direction": "max", "lat": 32.8998, "lon": -97.0403, "label": "Dallas Max", "scope": "daily"},
            "phx_high": {"series": "KXHIGHTPHX", "direction": "max", "lat": 33.4373, "lon": -112.0078, "label": "Phoenix Max", "scope": "daily"},
            "atl_high": {"series": "KXHIGHTATL", "direction": "max", "lat": 33.6407, "lon": -84.4277, "label": "Atlanta Max", "scope": "daily"},
            "sea_high": {"series": "KXHIGHTSEA", "direction": "max", "lat": 47.4502, "lon": -122.3088, "label": "Seattle Max", "scope": "daily"},
            "chi_low":  {"series": "KXLOWTCHI", "direction": "min", "lat": 41.9742, "lon": -87.9073, "label": "Chicago Min", "scope": "daily"},
            "den_low":  {"series": "KXLOWTDEN", "direction": "min", "lat": 39.8561, "lon": -104.6737, "label": "Denver Min", "scope": "daily"},
            "dc_low":   {"series": "KXLOWTDC",  "direction": "min", "lat": 38.8512, "lon": -77.0402, "label": "DC Min", "scope": "daily"},
            "lax_low":  {"series": "KXLOWTLAX", "direction": "min", "lat": 33.9425, "lon": -118.4081, "label": "LA Min", "scope": "daily"},
        })
    return cities


def _is_weather(ticker: str, event: str = "") -> bool:
    s = f"{ticker} {event}".upper()
    return any(p in s for p in WEATHER_PREFIXES)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


# ── Becker normalize (pure logic on dict rows, so it's testable) ───────────
def normalize_kalshi(trades_rows, markets_rows, weather_only=False) -> list[dict]:
    """Join trade prices to each market's settled result → becker_edge rows.

    trades_rows: dicts with ticker, yes_price (cents), created_time
    markets_rows: dicts with ticker, result ('yes'/'no'/''), title, event_ticker
    Output rows: market_ticker, market_p_yes (0-1), result, sample_at, title.
    Only settled markets (result yes/no) are emitted.
    """
    result_by = {}
    meta_by = {}
    for m in markets_rows:
        tk = m.get("ticker")
        if not tk:
            continue
        res = str(m.get("result", "") or "").lower()
        if res in ("yes", "no"):
            result_by[tk] = res
            meta_by[tk] = {"title": m.get("title", ""),
                           "event_ticker": m.get("event_ticker", ""),
                           "yes_sub_title": m.get("yes_sub_title", "")}
    out = []
    for t in trades_rows:
        tk = t.get("ticker")
        if tk not in result_by:
            continue
        meta = meta_by.get(tk, {})
        if weather_only and not _is_weather(tk, meta.get("event_ticker", "")):
            continue
        try:
            price = float(t.get("yes_price")) / 100.0
        except (TypeError, ValueError):
            continue
        if not (0.0 < price < 1.0):
            continue
        out.append({
            "market_ticker": tk,
            "market_p_yes": round(price, 4),
            "result": result_by[tk],
            "sample_at": _iso(t.get("created_time")),
            "title": meta.get("title", ""),
            "event_ticker": meta.get("event_ticker", ""),
            "yes_sub_title": meta.get("yes_sub_title", ""),
        })
    return out


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, str):
        return v
    try:
        return v.isoformat()  # pandas/py datetime
    except Exception:
        return str(v)


def _read_parquet_dir(path: Path, columns: list[str]) -> list[dict]:
    """Read every .parquet under `path`, projecting `columns`. Returns dicts."""
    try:
        import pandas as pd
    except ImportError:
        print("  ! pandas/pyarrow required: pip install pandas pyarrow", file=sys.stderr)
        return []
    files = sorted(path.rglob("*.parquet"))
    if not files:
        print(f"  ! no parquet under {path}", file=sys.stderr)
        return []
    frames = []
    for fp in files:
        try:
            df = pd.read_parquet(fp, columns=[c for c in columns])
            frames.append(df)
        except Exception as e:
            # Column set may vary per file; retry without projection.
            try:
                frames.append(pd.read_parquet(fp))
            except Exception:
                print(f"  ! skip {fp.name}: {e}", file=sys.stderr)
    if not frames:
        return []
    df = pd.concat(frames, ignore_index=True)
    return df.to_dict("records")


def cmd_becker(args) -> None:
    repo = Path(args.becker_path).expanduser()
    kalshi_dir = repo / "data" / "kalshi"
    if not (kalshi_dir / "markets").exists() or not (kalshi_dir / "trades").exists():
        print(f"Becker Kalshi data not found under {kalshi_dir}.")
        print("One-time KALSHI-ONLY extract (downloads 33.5 GiB, writes only Kalshi):\n")
        print(f"  brew install zstd")
        print(f"  mkdir -p {repo} && cd {repo}")
        print(f"  curl -L https://s3.jbecker.dev/data.tar.zst | zstd -dc --long=31 | tar -x 'data/kalshi/*'\n")
        print(f"(See {BECKER_REPO} for the full dataset / Polymarket too.)")
        print("Then re-run:  python scripts/fetch_backtest_data.py becker")
        return
    print(f"Reading Becker parquet from {kalshi_dir} …")
    markets = _read_parquet_dir(kalshi_dir / "markets",
                                ["ticker", "result", "title", "event_ticker",
                                 "yes_sub_title"])
    trades = _read_parquet_dir(kalshi_dir / "trades",
                               ["ticker", "yes_price", "created_time"])
    print(f"  loaded {len(markets)} market rows, {len(trades)} trade rows")
    all_rows = normalize_kalshi(trades, markets, weather_only=False)
    wx_rows = normalize_kalshi(trades, markets, weather_only=True)
    _write_jsonl(OUT_DIR / "becker_kalshi_all.jsonl", all_rows)
    _write_jsonl(OUT_DIR / "becker_kalshi_weather.jsonl", wx_rows)
    print(f"  -> {len(all_rows)} rows  data/backtest/becker_kalshi_all.jsonl")
    print(f"  -> {len(wx_rows)} rows  data/backtest/becker_kalshi_weather.jsonl")


# ── Live Kalshi settled-markets pull ───────────────────────────────────────
def _kalshi_get(path: str, params: dict):
    """Try the bot's signed client first, fall back to unauthenticated GET."""
    try:
        from lib.kalshi_auth import signed_get, can_sign
        if can_sign():
            return signed_get(path, params=params)
    except Exception:
        pass
    import requests
    base = "https://api.elections.kalshi.com/trade-api/v2"
    r = requests.get(f"{base}{path}", params=params, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_kalshi_settled(series: str | None = None, max_markets: int = 5000) -> list[dict]:
    rows, cursor, pulled = [], None, 0
    while pulled < max_markets:
        params = {"limit": 1000, "status": "settled"}
        if series:
            params["series_ticker"] = series
        if cursor:
            params["cursor"] = cursor
        data = _kalshi_get("/markets", params)
        markets = data.get("markets", []) or []
        if not markets:
            break
        for m in markets:
            res = str(m.get("result", "") or "").lower()
            if res not in ("yes", "no"):
                continue
            # last_price (cents) is the settlement-time price; fall back to mid.
            price = m.get("last_price")
            if price is None:
                yb, ya = m.get("yes_bid"), m.get("yes_ask")
                price = ((yb or 0) + (ya or 0)) / 2 if (yb or ya) else None
            if price is None:
                continue
            rows.append({
                "market_ticker": m.get("ticker", ""),
                "market_p_yes": round(float(price) / 100.0, 4),
                "result": res,
                "sample_at": m.get("close_time", ""),
                "title": m.get("title", ""),
                "event_ticker": m.get("event_ticker", ""),
            })
        pulled += len(markets)
        cursor = data.get("cursor")
        if not cursor:
            break
    return rows


def cmd_kalshi(args) -> None:
    print("Pulling settled Kalshi markets from the live API …")
    try:
        rows = fetch_kalshi_settled(series=args.series, max_markets=args.max_markets)
    except Exception as e:
        print(f"  ! Kalshi pull failed: {e}\n"
              f"    (datacenter IPs are geo-blocked — run this on your home machine.)",
              file=sys.stderr)
        return
    wx = [r for r in rows if _is_weather(r["market_ticker"], r["event_ticker"])]
    _write_jsonl(OUT_DIR / "kalshi_settled.jsonl", rows)
    _write_jsonl(OUT_DIR / "kalshi_settled_weather.jsonl", wx)
    print(f"  -> {len(rows)} rows  data/backtest/kalshi_settled.jsonl")
    print(f"  -> {len(wx)} rows  data/backtest/kalshi_settled_weather.jsonl")


# ── Kalshi candlestick top-up (recent settled markets the snapshot can't price) ─
def _cs_close(candle: dict) -> float | None:
    """Extract the close price (0-1) from one candlestick, handling both the
    new `*_dollars` shape and the historical cents shape. Returns None if the
    period had no trade (close absent)."""
    price = candle.get("price") or {}
    if price.get("close_dollars") is not None:
        return float(price["close_dollars"])              # already dollars 0-1
    if price.get("close") is not None:
        return float(price["close"]) / 100.0              # cents -> dollars
    # Fall back to the mid of yes_bid/yes_ask close if no trade printed.
    yb = (candle.get("yes_bid") or {})
    ya = (candle.get("yes_ask") or {})
    b = yb.get("close_dollars", (yb.get("close") or 0) / 100.0 if yb.get("close") is not None else None)
    a = ya.get("close_dollars", (ya.get("close") or 0) / 100.0 if ya.get("close") is not None else None)
    if b is not None and a is not None:
        return round((float(b) + float(a)) / 2.0, 4)
    return None


def parse_candlesticks(payload: dict, ticker: str, result: str,
                       event_ticker: str = "") -> list[dict]:
    """PURE: candlestick payload -> becker_edge rows (one per priced period).
    Lets becker_edge --market-col earliest collapse to the entry price."""
    out = []
    for c in payload.get("candlesticks", []) or []:
        close = _cs_close(c)
        if close is None or not (0.0 < close < 1.0):
            continue
        ts = c.get("end_period_ts")
        sample_at = (datetime.fromtimestamp(ts, timezone.utc).isoformat()
                     if isinstance(ts, (int, float)) else None)
        out.append({
            "market_ticker": ticker,
            "market_p_yes": round(close, 4),
            "result": result,
            "sample_at": sample_at,
            "event_ticker": event_ticker,
        })
    return out


def _list_settled_weather_markets(series: str, max_markets: int) -> list[dict]:
    """Settled markets for one weather series, with the fields the candlestick
    pull needs (ticker, result, open/close ts for the window)."""
    rows, cursor, pulled = [], None, 0
    while pulled < max_markets:
        params = {"limit": 1000, "status": "settled", "series_ticker": series}
        if cursor:
            params["cursor"] = cursor
        data = _kalshi_get("/markets", params)
        markets = data.get("markets", []) or []
        if not markets:
            break
        for m in markets:
            res = str(m.get("result", "") or "").lower()
            if res not in ("yes", "no"):
                continue
            rows.append({
                "ticker": m.get("ticker", ""),
                "result": res,
                "event_ticker": m.get("event_ticker", ""),
                "open_ts": m.get("open_time"),
                "close_ts": m.get("close_time"),
            })
        pulled += len(markets)
        cursor = data.get("cursor")
        if not cursor:
            break
    return rows


def _to_epoch(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def cmd_kalshi_candles(args) -> None:
    import time
    print("Pulling per-market candlesticks for settled WEATHER markets …")
    cities = _cities()
    series_list = sorted({c["series"] for c in cities.values() if c.get("series")})
    all_rows: list[dict] = []
    for series in series_list:
        try:
            markets = _list_settled_weather_markets(series, args.max_per_series)
        except Exception as e:
            print(f"  ! {series}: list failed ({e}) — geo-blocked? run on home IP",
                  file=sys.stderr)
            continue
        print(f"  {series}: {len(markets)} settled markets")
        for m in markets:
            start = _to_epoch(m.get("open_ts"))
            end = _to_epoch(m.get("close_ts"))
            params = {"period_interval": args.period}
            if start:
                params["start_ts"] = start
            if end:
                params["end_ts"] = end
            try:
                payload = _kalshi_get(
                    f"/series/{series}/markets/{m['ticker']}/candlesticks", params)
            except Exception as e:
                print(f"    ! {m['ticker']}: {e}", file=sys.stderr)
                continue
            all_rows.extend(parse_candlesticks(payload, m["ticker"], m["result"],
                                               m.get("event_ticker", "")))
            time.sleep(args.rate_sleep)  # be polite to the API
    _write_jsonl(OUT_DIR / "kalshi_candles_weather.jsonl", all_rows)
    print(f"  -> {len(all_rows)} candle-rows  data/backtest/kalshi_candles_weather.jsonl")


# ── Open-Meteo forecast + actuals (pure aggregation is testable) ───────────
def daily_extremes_from_hourly(hourly: dict, direction: str = "both") -> dict:
    """Collapse an Open-Meteo hourly payload {time:[...], temperature_2m:[...]}
    into per-date {date: {high, low}} in the payload's units."""
    times = hourly.get("time", []) or []
    temps = hourly.get("temperature_2m", []) or []
    by_day: dict[str, list[float]] = {}
    for t, v in zip(times, temps):
        if v is None:
            continue
        day = str(t)[:10]
        by_day.setdefault(day, []).append(float(v))
    out = {}
    for day, vals in by_day.items():
        if not vals:
            continue
        out[day] = {"high": round(max(vals), 1), "low": round(min(vals), 1)}
    return out


def fetch_openmeteo(args) -> list[dict]:
    import requests
    cities = _cities()
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=args.days)
    rows = []
    for key, c in cities.items():
        common = {
            "latitude": c["lat"], "longitude": c["lon"],
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "temperature_unit": "fahrenheit", "timezone": "auto",
        }
        fc_high, fc_low, ac_high, ac_low = {}, {}, {}, {}
        try:  # historical FORECAST (what the forecast said — no look-ahead)
            fc = requests.get("https://historical-forecast-api.open-meteo.com/v1/forecast",
                              params={**common, "hourly": "temperature_2m"}, timeout=30).json()
            ext = daily_extremes_from_hourly(fc.get("hourly", {}))
            fc_high = {d: v["high"] for d, v in ext.items()}
            fc_low = {d: v["low"] for d, v in ext.items()}
        except Exception as e:
            print(f"  ! {key} forecast: {e}", file=sys.stderr)
        try:  # actual observed extremes (reanalysis archive)
            ac = requests.get("https://archive-api.open-meteo.com/v1/archive",
                              params={**common, "daily": "temperature_2m_max,temperature_2m_min"},
                              timeout=30).json()
            d = ac.get("daily", {})
            for day, hi, lo in zip(d.get("time", []), d.get("temperature_2m_max", []),
                                   d.get("temperature_2m_min", [])):
                ac_high[day] = hi
                ac_low[day] = lo
        except Exception as e:
            print(f"  ! {key} actuals: {e}", file=sys.stderr)

        days = sorted(set(fc_high) | set(ac_high))
        for day in days:
            rows.append({
                "city": key, "series": c.get("series"), "label": c.get("label"),
                "scope": c.get("scope"), "direction": c.get("direction"),
                "date": day, "lat": c["lat"], "lon": c["lon"],
                "forecast_high_f": fc_high.get(day), "forecast_low_f": fc_low.get(day),
                "actual_high_f": ac_high.get(day), "actual_low_f": ac_low.get(day),
            })
    return rows


def cmd_openmeteo(args) -> None:
    print(f"Pulling Open-Meteo forecast + actuals for {args.days} days …")
    try:
        rows = fetch_openmeteo(args)
    except Exception as e:
        print(f"  ! Open-Meteo pull failed: {e}", file=sys.stderr)
        return
    _write_jsonl(OUT_DIR / "weather_truth.jsonl", rows)
    print(f"  -> {len(rows)} city-days  data/backtest/weather_truth.jsonl")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source",
                    choices=["becker", "kalshi", "kalshi-candles", "openmeteo", "all"])
    ap.add_argument("--becker-path", default="~/becker",
                    help="dir containing the extracted Becker data/kalshi/ "
                         "(default ~/becker)")
    ap.add_argument("--series", default=None,
                    help="kalshi: limit to one series_ticker (e.g. KXHIGHTDAL)")
    ap.add_argument("--max-markets", type=int, default=5000,
                    help="kalshi: cap markets pulled (default 5000)")
    ap.add_argument("--max-per-series", type=int, default=400,
                    help="kalshi-candles: cap settled markets per series (default 400)")
    ap.add_argument("--period", type=int, default=60, choices=[1, 60, 1440],
                    help="kalshi-candles: candle interval in minutes (default 60)")
    ap.add_argument("--rate-sleep", type=float, default=0.25,
                    help="kalshi-candles: seconds to sleep between API calls (default 0.25)")
    ap.add_argument("--days", type=int, default=120,
                    help="openmeteo: lookback window in days (default 120)")
    args = ap.parse_args()

    if args.source in ("becker", "all"):
        cmd_becker(args)
    if args.source in ("kalshi", "all"):
        cmd_kalshi(args)
    if args.source in ("kalshi-candles", "all"):
        cmd_kalshi_candles(args)
    if args.source in ("openmeteo", "all"):
        cmd_openmeteo(args)


if __name__ == "__main__":
    main()
