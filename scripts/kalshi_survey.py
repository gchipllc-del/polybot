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
# Classification is CATEGORY-FIRST: Kalshi already labels each series with a
# category, so that's the authoritative signal — far more reliable than scanning
# free-text titles, where generic words ("high", "low", "win") bleed across every
# category. Each Kalshi category maps to a (data_source, verdict, base).
CATEGORY_RULES = {
    "Climate and Weather":     ("NWS / Open-Meteo forecasts", "PROVEN", 0.80),
    "Economics":               ("consensus + released gov data", "CANDIDATE", 0.55),
    "Companies":               ("analyst estimates / filings", "CANDIDATE", 0.45),
    "Transportation":          ("ops data (FAA delays / fuel)", "CANDIDATE", 0.40),
    "Financials":              ("index price feed", "EFFICIENT", 0.05),
    "Crypto":                  ("spot price feed", "EFFICIENT", 0.05),
    "Sports":                  ("sports models / market odds", "SHARP", 0.30),
    "Politics":                ("polling aggregates", "SHARP", 0.25),
    "Science and Technology":  ("no clean public predictor", "THIN", 0.10),
    "Health":                  ("no clean public predictor", "THIN", 0.10),
    "Entertainment":           ("no clean public predictor", "THIN", 0.10),
    "World":                   ("no clean public predictor", "THIN", 0.10),
}
DATA_THIN = ("no clean public predictor", "THIN", 0.10)

# SPECIFIC keyword overrides — only used to RE-classify a series whose title makes
# its true type unambiguous despite its filed category (e.g. a Bitcoin market that
# lives under "Financials", or a Fed-rate market under "Financials"). Tokens here
# must be specific enough that they can't appear in an unrelated market's title.
# NO generic words ("high", "low", "win", "up", "above") — that was the old bug.
KEYWORD_OVERRIDES = [
    (("bitcoin", "ethereum", "solana", "dogecoin", "litecoin", " btc ", " eth "),
     ("spot price feed", "EFFICIENT", 0.05)),
    (("cpi ", "inflation", "nonfarm", "payroll", "unemployment", "jobless",
      "fed funds", "interest rate", "gdp ", "ppi "),
     ("consensus + released gov data", "CANDIDATE", 0.55)),
]

# How often the family resolves → how fast you can gather a sample.
CADENCE_SCORE = {"hourly": 1.0, "daily": 0.9, "weekly": 0.6, "monthly": 0.35,
                 "quarterly": 0.2, "yearly": 0.08, "one-off": 0.05, "": 0.3}


def classify(category: str, title: str, ticker: str):
    """Return (data_source, verdict, base_score). Category-first; specific-keyword
    overrides only reclassify when a title is unambiguous. Pad the haystack with
    spaces so token boundaries (" btc ", "cpi ") match at the ends too."""
    hay = f" {title} {ticker} ".lower()
    for keys, res in KEYWORD_OVERRIDES:
        if any(k in hay for k in keys):
            return res
    return CATEGORY_RULES.get(category, DATA_THIN)


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
                                "votes": defaultdict(int), "cadence": 0.0,
                                "examples": []})
    for s in series_rows:
        cat = s.get("category", "")
        src, verdict, base = classify(cat, s.get("title", ""), s.get("ticker", ""))
        f = fams[cat or "Uncategorized"]
        f["n_series"] += 1
        f["open"] += int(s.get("open_markets", 0) or 0)
        f["vol"] += float(s.get("volume", 0) or 0)
        f["cadence"] = max(f["cadence"], cadence_score(s.get("frequency", "")))
        # Tally each series' verdict; the FAMILY verdict is the modal one, so a
        # single stray title can't promote the whole category (the old argmax bug).
        f["votes"][(src, verdict, base)] += 1
        if len(f["examples"]) < 3:
            f["examples"].append(s.get("ticker", ""))
    out = []
    for cat, f in fams.items():
        (src, verdict, base), _ = max(f["votes"].items(), key=lambda kv: kv[1])
        liq = liquidity_score(f["open"], f["vol"]) if deep else 0.5
        out.append({
            "category": cat, "n_series": f["n_series"], "open": f["open"],
            "volume": int(f["vol"]), "verdict": verdict, "data": src,
            "cadence": round(f["cadence"], 2), "liq": liq,
            "priority": priority(base, f["cadence"], liq, verdict),
            "examples": f["examples"]})
    out.sort(key=lambda r: -r["priority"])
    return out


def inspect_markets(series_ticker: str) -> int:
    """Show the OPEN market ladder for one series — strike, live yes/no quotes,
    volume, close — so we can see whether there's a tradeable, mispriceable
    structure before building a forward-collector for it."""
    from fetch_backtest_data import _kalshi_get
    try:
        data = _kalshi_get("/markets", {"series_ticker": series_ticker,
                                        "status": "open", "limit": 200})
    except Exception as e:
        print(f"! {series_ticker}: {e}", file=sys.stderr)
        return 1
    ms = data.get("markets", []) or []
    if not ms:
        print(f"no OPEN markets for {series_ticker} (closed window now? wrong ticker?).")
        return 1
    print(f"=== {series_ticker}: {len(ms)} open markets ===")
    print(f"{'ticker':30} {'strike':>16} {'ybid':>5} {'yask':>5} {'vol':>6}  close")
    for m in sorted(ms, key=lambda x: (x.get("close_time", ""),
                                       _safe_float(x.get("floor_strike")))):
        sub = m.get("subtitle") or m.get("yes_sub_title") or ""
        fl, cp = m.get("floor_strike"), m.get("cap_strike")
        strike = sub or (f"{fl}–{cp}" if (fl is not None or cp is not None) else "?")
        print(f"{str(m.get('ticker',''))[:30]:30} {str(strike)[:16]:>16} "
              f"{str(m.get('yes_bid')):>5} {str(m.get('yes_ask')):>5} "
              f"{str(m.get('volume') or 0):>6}  {str(m.get('close_time',''))[:16]}")
    print("\n  READ: a wide ladder with two-sided quotes + a slow, forecastable "
          "underlying = the setup for a data-edge collector. All-or-nothing extreme "
          "prices (0.01/0.99) leave no room after fees.")
    return 0


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def drill_category(category: str, deep: bool, cap: int) -> int:
    """List every series in ONE category — cadence, liquidity, verdict, title —
    sorted by cadence then liquidity, so we can see what's actually tradeable and
    how often it resolves before committing a forward-collector to it."""
    series = fetch_series(category)
    if not series:
        print(f"no series for '{category}' — run on home IP with Kalshi auth, and "
              f"check the exact category name (e.g. 'Economics', 'Transportation').")
        return 1
    rows, sampled = [], 0
    for s in series:
        tk, title, freq = s.get("ticker", ""), s.get("title", ""), s.get("frequency", "")
        _, verdict, _ = classify(category, title, tk)
        open_n = vol = 0
        if deep and sampled < cap:
            open_n, vol = sample_liquidity(tk)
            sampled += 1
        rows.append({"ticker": tk, "title": title, "freq": freq,
                     "cad": cadence_score(freq), "verdict": verdict,
                     "open": open_n, "vol": vol})
    rows.sort(key=lambda r: (-r["cad"], -r["open"]))
    print(f"=== DRILL: {category} — {len(rows)} series (sorted by cadence, then liquidity) ===")
    print(f"{'cadence':>7} {'freq':>9} {'open':>5} {'vol':>8} {'verdict':>10}  ticker  ·  title")
    for r in rows[:60]:
        print(f"{r['cad']:>7.2f} {str(r['freq'])[:9]:>9} {r['open']:>5} {int(r['vol']):>8} "
              f"{r['verdict']:>10}  {r['ticker'][:18]:18} {r['title'][:42]}")
    fast = sum(1 for r in rows if r["cad"] >= 0.6)   # weekly-or-faster
    print(f"\n  {fast}/{len(rows)} series resolve WEEKLY-OR-FASTER (cadence ≥ 0.6) — only "
          f"these can reach PSR significance in a practical window.")
    print("  CADENCE IS THE GATE: a monthly/yearly family can't be validated in "
          "reasonable time even if a real edge exists. Favor the fast resolvers.")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deep", action="store_true",
                    help="also sample per-series open-market volume. Only samples "
                         "families that could have an edge (skips EFFICIENT/THIN, "
                         "which are floored regardless) so it stays fast.")
    ap.add_argument("--deep-cap", type=int, default=60,
                    help="max series per category to sample liquidity for (default 60)")
    ap.add_argument("--drill", help="list every series in ONE category (cadence, "
                    "liquidity, verdict, title) instead of the cross-category survey")
    ap.add_argument("--markets", help="show the OPEN market ladder (strikes + live "
                    "quotes) for ONE series ticker, e.g. KXAAAGASD")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    if args.markets:
        raise SystemExit(inspect_markets(args.markets))
    if args.drill:
        raise SystemExit(drill_category(args.drill, args.deep, args.deep_cap))

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
    # classify routing — category-first
    assert classify("Climate and Weather", "NY High Temp", "KXHIGHNY")[1] == "PROVEN"
    assert classify("Crypto", "Bitcoin above", "KXBTC")[1] == "EFFICIENT"
    assert classify("Economics", "CPI year-over-year", "KXCPI")[1] == "CANDIDATE"
    assert classify("Sports", "NFL game winner", "KXNFL")[1] == "SHARP"
    assert classify("Entertainment", "Oscar best picture", "KXOSCAR")[1] == "THIN"
    print("classify routing OK")
    # REGRESSION: generic "high"/"low"/"win" must NOT pull non-weather into PROVEN
    # (the bug — these titles all contain weather-rule trigger words).
    assert classify("Politics", "Lowest approval rating", "KXAPPROVE")[1] == "SHARP"
    assert classify("Financials", "S&P 500 new all-time high", "KXSPX")[1] == "EFFICIENT"
    assert classify("Sports", "Will the home team win big", "KXWIN")[1] == "SHARP"
    assert classify("Entertainment", "Highest-grossing film", "KXFILM")[1] == "THIN"
    # specific overrides DO reclassify mis-filed series (crypto/econ under Financials)
    assert classify("Financials", "Bitcoin above 100k", "KXBTCHIGH")[1] == "EFFICIENT"
    assert classify("Financials", "Fed funds rate cut", "KXFED")[1] == "CANDIDATE"
    print("cross-category bleed regression OK")
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
    # REGRESSION: a single stray Bitcoin series in Politics must NOT promote the
    # whole Politics family — modal verdict stays SHARP, not EFFICIENT/PROVEN.
    pol = [{"category": "Politics", "title": f"Senate race {i}", "ticker": f"KXSEN{i}",
            "frequency": "yearly", "open_markets": 5, "volume": 100} for i in range(6)]
    pol.append({"category": "Politics", "title": "Bitcoin mentioned in debate",
                "ticker": "KXBTCDEBATE", "frequency": "yearly",
                "open_markets": 1, "volume": 1})
    pol_row = aggregate(pol, deep=True)[0]
    assert pol_row["verdict"] == "SHARP", pol_row   # modal, not stray-promoted
    print("modal-verdict aggregation OK (stray series can't promote a family)")
    print("PASS" if ok else "*** FAIL ***")
    return 0 if ok else 1


if __name__ == "__main__":
    main()
