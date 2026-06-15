#!/usr/bin/env python3
"""kalshi_survey — scan the WHOLE Kalshi catalog for market families where an
edge could come from GATHERING + PROCESSING PUBLIC DATA (the property that made
weather work and BTC fail), not from speed, news, or insider flow.

It does NOT trade and does NOT prove an edge — it's a map. For every series it
pulls, it scores three things that decide whether a data edge is even possible:

  1. CADENCE   — how often the family resolves. Frequent = many independent
                 shots = you can actually validate + accumulate a sample.
                 (Politics resolves once a year; weather resolves daily.)
  2. DATA      — is there a public dataset/model that predicts the outcome, and
                 is the market plausibly NOT already pricing it perfectly?
                 (weather→forecasts: yes; crypto→spot price: yes but EFFICIENT.)
  3. LIQUIDITY — is there volume/open-interest to actually fill paper-real?

Output: families ranked by a priority score = cadence × data-edge × liquidity,
so the top of the list is "frequent, data-predictable, liquid" — the next thing
worth forward-collecting and validating the same way we did weather.

  python scripts/kalshi_survey.py             # catalog scan (fast)
  python scripts/kalshi_survey.py --deep      # also sample per-series volume
  python scripts/kalshi_survey.py --selftest
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# Kalshi catalog categories (the API filters /series by these strings).
CATEGORIES = ["Climate and Weather", "Economics", "Financials", "Crypto",
              "Politics", "Sports", "Companies", "Science and Technology",
              "Health", "Entertainment", "World", "Transportation"]

# Data-edge hypotheses: (keywords, public_data_source, verdict, base) matched
# against series category+title+ticker. base ∈ [0,1] = how promising a pure
# data-processing edge is, BEFORE cadence/liquidity. Verdicts:
#   PROVEN    — we already validated a (marginal) edge here
#   EFFICIENT — we tested it; the market prices the data already → no edge
#   CANDIDATE — public data predicts it; worth collecting + testing
#   SHARP     — public data exists but pros arb it hard; low odds
#   THIN      — no clean gatherable predictor → skip
EDGE_RULES = [
    (("high", "low", "temp", "weather", "rain", "snow", "climate"),
     "NWS / Open-Meteo forecasts", "PROVEN", 0.80),
    (("bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "doge"),
     "spot price feed", "EFFICIENT", 0.05),
    (("cpi", "inflation", "pce", "jobs", "payroll", "unemployment", "gdp",
      "fed", "rate", "jobless", "ppi", "retail sales"),
     "consensus + released gov data", "CANDIDATE", 0.55),
    (("nfl", "nba", "mlb", "nhl", "soccer", "tennis", "ufc", "golf", "game",
      "match", "win", "playoff"),
     "sports models / market odds", "SHARP", 0.30),
    (("election", "president", "senate", "house", "governor", "poll", "primary",
      "approval", "nominee"),
     "polling aggregates", "SHARP", 0.25),
    (("earnings", "revenue", "stock", "ipo", "company", "ceo"),
     "analyst estimates / filings", "CANDIDATE", 0.45),
    (("gas", "oil", "gasoline", "egg", "price of"),
     "commodity / price series", "CANDIDATE", 0.50),
]
DATA_THIN = ("no clean public predictor", "THIN", 0.10)

# How often the family resolves → how fast you can gather a sample.
CADENCE_SCORE = {"hourly": 1.0, "daily": 0.9, "weekly": 0.6, "monthly": 0.35,
                 "quarterly": 0.2, "yearly": 0.08, "one-off": 0.05, "": 0.3}


def classify(category: str, title: str, ticker: str):
    """Return (data_source, verdict, base_score) for a series."""
    hay = f"{category} {title} {ticker}".lower()
    for keys, src, verdict, base in EDGE_RULES:
        if any(k in hay for k in keys):
            return src, verdict, base
    return DATA_THIN


def cadence_score(frequency: str) -> float:
    return CADENCE_SCORE.get((frequency or "").lower(), 0.3)


def liquidity_score(open_markets: int, volume: float) -> float:
    """0..1 from open market count + total volume (log-ish, saturating)."""
    if open_markets <= 0:
        return 0.0
    v = min(1.0, (volume or 0) / 50000.0)      # ~50k contracts = saturated
    m = min(1.0, open_markets / 50.0)
    return round(0.5 * v + 0.5 * m, 3)


def priority(base: float, cadence: float, liq: float, verdict: str) -> float:
    """Final rank: data-edge × cadence × liquidity. EFFICIENT/THIN are floored
    near zero regardless of liquidity (a liquid efficient market is still no edge)."""
    if verdict in ("EFFICIENT", "THIN"):
        return round(base * 0.1, 4)
    return round(base * (0.4 + 0.6 * cadence) * (0.3 + 0.7 * liq), 4)


# ── live fetch (needs home IP; thin wrappers) ───────────────────────────────

def fetch_series(category: str) -> list:
    from fetch_backtest_data import _kalshi_get
    out, cursor = [], None
    for _ in range(20):
        params = {"category": category, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            data = _kalshi_get("/series", params)
        except Exception as e:
            print(f"  ! {category}: {e}", file=sys.stderr)
            break
        out.extend(data.get("series", []) or [])
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def sample_liquidity(series_ticker: str) -> tuple:
    """(open_market_count, total_volume) from a single open-markets page."""
    from fetch_backtest_data import _kalshi_get
    try:
        data = _kalshi_get("/markets", {"series_ticker": series_ticker,
                                        "status": "open", "limit": 200})
    except Exception:
        return 0, 0.0
    ms = data.get("markets", []) or []
    return len(ms), sum(float(m.get("volume") or 0) for m in ms)


def aggregate(series_rows: list, deep: bool):
    """series_rows: list of dicts with category/title/ticker/frequency
    (+ open_markets/volume if deep). Returns ranked family summaries."""
    fams = defaultdict(lambda: {"n_series": 0, "open": 0, "vol": 0.0,
                                "verdict": None, "src": None, "base": 0.0,
                                "cadence": 0.0, "examples": []})
    for s in series_rows:
        cat = s.get("category", "")
        src, verdict, base = classify(cat, s.get("title", ""), s.get("ticker", ""))
        f = fams[cat or "Uncategorized"]
        f["n_series"] += 1
        f["open"] += int(s.get("open_markets", 0) or 0)
        f["vol"] += float(s.get("volume", 0) or 0)
        f["cadence"] = max(f["cadence"], cadence_score(s.get("frequency", "")))
        # keep the most promising verdict/src seen in the family
        if base > f["base"]:
            f["base"], f["verdict"], f["src"] = base, verdict, src
        if len(f["examples"]) < 3:
            f["examples"].append(s.get("ticker", ""))
    out = []
    for cat, f in fams.items():
        liq = liquidity_score(f["open"], f["vol"]) if deep else 0.5
        out.append({
            "category": cat, "n_series": f["n_series"], "open": f["open"],
            "volume": int(f["vol"]), "verdict": f["verdict"], "data": f["src"],
            "cadence": round(f["cadence"], 2), "liq": liq,
            "priority": priority(f["base"], f["cadence"], liq, f["verdict"]),
            "examples": f["examples"]})
    out.sort(key=lambda r: -r["priority"])
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deep", action="store_true",
                    help="also sample per-series open-market volume. Only samples "
                         "families that could have an edge (skips EFFICIENT/THIN, "
                         "which are floored regardless) so it stays fast.")
    ap.add_argument("--deep-cap", type=int, default=60,
                    help="max series per category to sample liquidity for (default 60)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())

    rows, sampled = [], 0
    for cat in CATEGORIES:
        series = fetch_series(cat)
        cap = args.deep_cap
        for s in series:
            rec = {"category": cat, "title": s.get("title", ""),
                   "ticker": s.get("ticker", ""), "frequency": s.get("frequency", "")}
            # Only spend API calls on families where liquidity can move the rank.
            # EFFICIENT (crypto) / THIN (entertainment…) are floored either way.
            _, verdict, _ = classify(cat, rec["title"], rec["ticker"])
            if args.deep and verdict not in ("EFFICIENT", "THIN") and cap > 0:
                n, v = sample_liquidity(rec["ticker"])
                rec["open_markets"], rec["volume"] = n, v
                cap -= 1
                sampled += 1
            rows.append(rec)
        print(f"  {cat}: {len(series)} series"
              f"{' (liq-sampled '+str(args.deep_cap-cap)+')' if args.deep else ''}",
              file=sys.stderr)
    if args.deep:
        print(f"  liquidity sampled on {sampled} edge-candidate series total",
              file=sys.stderr)
    if not rows:
        print("no series returned — run on home IP with Kalshi auth.")
        return

    ranked = aggregate(rows, deep=args.deep)
    print(f"\n=== Kalshi data-edge survey — {len(rows)} series across "
          f"{len(ranked)} categories ===")
    print(f"{'category':<26} {'series':>6} {'cadence':>7} {'verdict':>10} "
          f"{'pri':>5}  data source")
    for r in ranked:
        print(f"{r['category'][:26]:<26} {r['n_series']:>6} {r['cadence']:>7.2f} "
              f"{str(r['verdict']):>10} {r['priority']:>5.2f}  {r['data']}")
    print("\nHOW TO READ: high priority = frequent + data-predictable + (if --deep) "
          "liquid.\n  PROVEN=weather (validated marginal)  CANDIDATE=worth forward-"
          "collecting\n  SHARP=pros arb it (low odds)  EFFICIENT/THIN=skip.")
    cands = [r for r in ranked if r["verdict"] == "CANDIDATE"]
    if cands:
        print("\nTop CANDIDATEs to forward-collect next (same playbook as weather):")
        for r in cands[:5]:
            print(f"  • {r['category']} — {r['data']} "
                  f"(e.g. {', '.join(x for x in r['examples'] if x)})")
    print("\nNOTE: this only finds where an edge is POSSIBLE. Proving it still means "
          "collect → calibrate → significance-test, exactly like weather. No shortcut.")


def selftest() -> int:
    ok = True
    # classify routing
    assert classify("Climate and Weather", "NY High Temp", "KXHIGHNY")[1] == "PROVEN"
    assert classify("Crypto", "Bitcoin above", "KXBTC")[1] == "EFFICIENT"
    assert classify("Economics", "CPI year-over-year", "KXCPI")[1] == "CANDIDATE"
    assert classify("Sports", "NFL game winner", "KXNFL")[1] == "SHARP"
    assert classify("Entertainment", "Oscar best picture", "KXOSCAR")[1] == "THIN"
    print("classify routing OK")
    # cadence
    assert cadence_score("hourly") > cadence_score("daily") > cadence_score("yearly")
    assert cadence_score("") == 0.3 and cadence_score(None) == 0.3
    print("cadence_score OK")
    # priority: efficient floored below a candidate even with great liquidity
    eff = priority(0.05, 1.0, 1.0, "EFFICIENT")
    cand = priority(0.55, 0.9, 0.8, "CANDIDATE")
    weather = priority(0.80, 0.9, 0.8, "PROVEN")
    assert eff < cand < weather, (eff, cand, weather)
    print(f"priority ordering OK (efficient {eff} < candidate {cand} < weather {weather})")
    # aggregate end-to-end on synthetic series
    rows = [
        {"category": "Climate and Weather", "title": "NY High", "ticker": "KXHIGHNY",
         "frequency": "daily", "open_markets": 40, "volume": 30000},
        {"category": "Crypto", "title": "BTC above", "ticker": "KXBTC",
         "frequency": "hourly", "open_markets": 60, "volume": 90000},
        {"category": "Economics", "title": "CPI", "ticker": "KXCPI",
         "frequency": "monthly", "open_markets": 10, "volume": 8000},
    ]
    ranked = aggregate(rows, deep=True)
    assert ranked[0]["category"] == "Climate and Weather", [r["category"] for r in ranked]
    assert ranked[-1]["category"] == "Crypto", ranked   # efficient sinks despite volume
    print("aggregate ranking OK (weather top, crypto bottom despite high volume)")
    print("PASS" if ok else "*** FAIL ***")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
