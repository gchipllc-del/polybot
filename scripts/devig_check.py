#!/usr/bin/env python3
"""devig_check — the second sports edge that runs ALONGSIDE the lock: take a SHARP
sportsbook's 2-way moneyline (Pinnacle by default), strip its vig to a fair
probability, and flag when Kalshi's YES price disagrees by more than a threshold.

Why this complements sports_lock: the lock only fires in garbage time (a near-decided
game). This works the WHOLE game — wherever a sharp book's devigged fair price and
Kalshi's price diverge — AND it doubles as an independent second opinion on lock
signals ("is the gap real, or are we the ones who are wrong?").

Devig is done four ways (multiplicative / additive / power / Shin) so we get a
ROBUSTNESS BAND, not a single point — if the methods disagree a lot the line is
ambiguous and we skip. Sharp fair prob ≈ the market's best estimate of truth; an edge
only exists if Kalshi is mispriced *relative to that*, net of fees.

READ-ONLY / paper. Logs signals for forward-collection; places NO orders.
Odds via The Odds API (free key in env ODDS_API_KEY); Kalshi via the repo client.

  python scripts/devig_check.py probe nba KXNBAGAMES   # RUN FIRST: odds + devig + Kalshi ladder, VERIFY mapping
  python scripts/devig_check.py scan  nba KXNBAGAMES   # flag Kalshi YES mispriced vs sharp fair prob
  python scripts/devig_check.py selftest

⚠ Edge here depends on Kalshi being inefficient *relative to the sharp book* — it may
  not be. That's the point of pairing this with pypbo/netcal: forward-collect, then
  prove (or kill) the edge by per-day PSR before any real money.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
LOG = ROOT / "data" / "devig_check.jsonl"

# our league code → The Odds API sport key
SPORTS = {
    "nba":   "basketball_nba",
    "wnba":  "basketball_wnba",
    "ncaab": "basketball_ncaab",
    "nfl":   "americanfootball_nfl",
    "ncaaf": "americanfootball_ncaaf",
    "nhl":   "icehockey_nhl",
}
SHARP_BOOK = "pinnacle"     # the sharpest 2-way moneyline; fall back to consensus if absent
MIN_EDGE = 0.03             # require fair − market ≥ this (prob units) before flagging
MAX_BAND = 0.04             # if the 4 devig methods span more than this, the line is ambiguous → skip
DEVIG_METHODS = ("multiplicative", "additive", "power", "shin")


# ── pure devig math (testable, no network) ───────────────────────────────────

def implied_from_decimal(odds: float):
    """Decimal odds → raw implied probability (includes vig). None if invalid."""
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    return (1.0 / o) if o > 1.0 else None


def _norm_mult(ios: list) -> list:
    s = sum(ios)
    return [x / s for x in ios] if s > 0 else ios


def devig_multiplicative(ios: list) -> list:
    """Scale all inverse-odds so they sum to 1 (the naive baseline)."""
    return _norm_mult(ios)


def devig_additive(ios: list) -> list:
    """Subtract the overround equally from each (better near 50/50)."""
    n = len(ios)
    excess = (sum(ios) - 1.0) / n
    out = [max(1e-9, x - excess) for x in ios]
    return _norm_mult(out)            # tiny renorm to kill clamp drift


def devig_power(ios: list) -> list:
    """Find k with Σ ioᵢ^k = 1, fairᵢ = ioᵢ^k (shrinks favorites less — handles
    favorite-longshot bias). Bisect k; ioᵢ<1 so the sum is monotone decreasing in k."""
    if sum(ios) <= 1.0:
        return _norm_mult(ios)
    lo, hi = 0.01, 50.0
    for _ in range(80):
        k = (lo + hi) / 2
        s = sum(x ** k for x in ios)
        if s > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    return _norm_mult([x ** k for x in ios])


def devig_shin(ios: list) -> list:
    """Shin (1992/93): assumes a proportion z of insider money and backs out the
    fair probs. Bisect z∈[0,0.49) so Σ pᵢ(z)=1, with
        pᵢ(z) = (√(z² + 4(1−z)·ioᵢ²/B) − z) / (2(1−z)),  B = Σ ioᵢ."""
    B = sum(ios)
    if B <= 1.0:
        return _norm_mult(ios)

    def p_of(z):
        return [(math.sqrt(z * z + 4 * (1 - z) * (x * x) / B) - z) / (2 * (1 - z))
                for x in ios]

    lo, hi = 0.0, 0.49
    for _ in range(80):
        z = (lo + hi) / 2
        s = sum(p_of(z))
        if s > 1.0:                   # too much prob → need more insider shrink
            lo = z
        else:
            hi = z
    return _norm_mult(p_of((lo + hi) / 2))


def fair_probs(odds: list, method: str = "shin") -> list:
    """Decimal odds (≥2 outcomes) → devigged fair probabilities by `method`."""
    ios = [implied_from_decimal(o) for o in odds]
    if any(x is None for x in ios):
        return []
    return {"multiplicative": devig_multiplicative, "additive": devig_additive,
            "power": devig_power, "shin": devig_shin}[method](ios)


def fair_band(odds_home: float, odds_away: float):
    """Two-way market → dict(method→p_home) + consensus(mean) + band(max−min).
    The band is our ambiguity gauge: tight = methods agree = trustworthy."""
    per = {}
    for m in DEVIG_METHODS:
        fp = fair_probs([odds_home, odds_away], m)
        if fp:
            per[m] = fp[0]            # p_home
    if not per:
        return None
    vals = list(per.values())
    consensus = sum(vals) / len(vals)
    return {"per_method": per, "consensus": round(consensus, 4),
            "band": round(max(vals) - min(vals), 4)}


def kalshi_fee(price: float, contracts: int = 1) -> float:
    """Kalshi taker fee in dollars: ceil(0.07·C·P·(1−P)) cents."""
    p = max(0.0, min(1.0, price))
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100.0


def devig_signal(fair_yes, kalshi_yes, band=None,
                 min_edge: float = MIN_EDGE, max_band: float = MAX_BAND):
    """Is Kalshi's YES mispriced vs the sharp fair prob of the YES outcome?
    Returns (side, our_prob, gross_edge, net_edge) or None. net_edge subtracts the
    Kalshi taker fee at the side's entry price. Skips when the devig methods disagree
    (band > max_band) or the gap is under min_edge."""
    if fair_yes is None or kalshi_yes is None:
        return None
    if band is not None and band > max_band:
        return None                                   # ambiguous line → don't trust
    diff = fair_yes - kalshi_yes
    if diff >= min_edge:                              # YES underpriced on Kalshi → buy YES
        net = diff - kalshi_fee(kalshi_yes)
        return ("YES", round(fair_yes, 4), round(diff, 4), round(net, 4))
    if -diff >= min_edge:                             # YES overpriced → buy NO at (1−yes)
        net = (-diff) - kalshi_fee(1.0 - kalshi_yes)
        return ("NO", round(1.0 - fair_yes, 4), round(-diff, 4), round(net, 4))
    return None


# ── live fetch (needs network) ───────────────────────────────────────────────

def fetch_odds(sport_key: str, api_key: str, region: str = "us") -> list:
    """The Odds API v4 h2h (moneyline), decimal. Returns
    [dict(home, away, commence, books={bookkey:(home_odds, away_odds)})]."""
    import requests
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    params = {"apiKey": api_key, "regions": region, "markets": "h2h",
              "oddsFormat": "decimal"}
    r = requests.get(url, params=params, timeout=25)
    r.raise_for_status()
    out = []
    for ev in r.json() or []:
        home, away = ev.get("home_team"), ev.get("away_team")
        if not home or not away:
            continue
        books = {}
        for bk in ev.get("bookmakers", []) or []:
            h2h = next((m for m in bk.get("markets", []) if m.get("key") == "h2h"), None)
            if not h2h:
                continue
            od = {o.get("name"): o.get("price") for o in h2h.get("outcomes", []) or []}
            if home in od and away in od:
                books[bk.get("key")] = (od[home], od[away])
        if books:
            out.append({"home": home, "away": away,
                        "commence": ev.get("commence_time", ""), "books": books})
    return out


def event_fair_home(ev: dict):
    """Pick the sharp book (Pinnacle) if present, else devig EACH book and take the
    median consensus. Returns (p_home, band, source) or (None, None, None)."""
    books = ev.get("books", {})
    if SHARP_BOOK in books:
        fb = fair_band(*books[SHARP_BOOK])
        return (fb["consensus"], fb["band"], SHARP_BOOK) if fb else (None, None, None)
    cons = []
    for oh, oa in books.values():
        fb = fair_band(oh, oa)
        if fb:
            cons.append(fb["consensus"])
    if not cons:
        return None, None, None
    cons.sort()
    med = cons[len(cons) // 2]
    band = round(max(cons) - min(cons), 4)            # cross-book spread as ambiguity
    return med, band, f"consensus({len(cons)})"


def _api_key():
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        print("! set ODDS_API_KEY (free key from the-odds-api.com) in env/.env",
              file=sys.stderr)
    return key


def cmd_probe(args) -> None:
    from sports_lock import fetch_market_ladder, _norm
    sk = SPORTS.get(args.league)
    if not sk:
        print(f"unknown league {args.league}; known: {', '.join(SPORTS)}")
        return
    key = _api_key()
    if not key:
        return
    print(f"=== PROBE {args.league} ({sk}) ↔ Kalshi {args.series} ===")
    evs = fetch_odds(sk, key)
    print(f"Odds API: {len(evs)} events. Sharp/consensus devig (p_home):")
    for ev in evs[:12]:
        ph, band, src = event_fair_home(ev)
        bs = f"p_home={ph:.2f} band={band:.2f} [{src}]" if ph is not None else "no h2h"
        print(f"  {ev['away']} @ {ev['home']}  {bs}")
    ladder = fetch_market_ladder(args.series)
    print(f"\nKalshi ladder ({len(ladder)} markets):")
    for tk, title, sub, yes_p in ladder[:12]:
        print(f"  {tk[:34]:34} yes~{yes_p}  «{sub or title}»")
    print("\nCHECK: do Odds-API team names match the Kalshi yes_sub_title teams, and is "
          "the devigged p_home sane (favorite > 0.5)? If yes, trust scan.")


def cmd_scan(args) -> None:
    from sports_lock import fetch_market_ladder, _norm
    sk = SPORTS.get(args.league)
    if not sk:
        print(f"unknown league {args.league}; known: {', '.join(SPORTS)}")
        return
    key = _api_key()
    if not key:
        return
    ts = datetime.now(timezone.utc).isoformat()
    try:
        evs = fetch_odds(sk, key)
    except Exception as e:
        print(f"! Odds API {args.league}: {e}", file=sys.stderr)
        return
    ladder = fetch_market_ladder(args.series)
    hits, matched = [], 0
    for tk, title, sub, yes_p in ladder:
        if yes_p is None:
            continue
        subk = _norm(sub)
        # find the odds event whose home OR away team is this market's YES team
        fair_yes = band = ev_used = side_team = None
        for ev in evs:
            hk, ak = _norm(ev["home"]), _norm(ev["away"])
            ph, bnd, src = event_fair_home(ev)
            if ph is None:
                continue
            if hk and hk in subk:
                fair_yes, band, ev_used, side_team = ph, bnd, ev, "home"
                break
            if ak and ak in subk:
                fair_yes, band, ev_used, side_team = 1.0 - ph, bnd, ev, "away"
                break
        if fair_yes is None:
            continue
        matched += 1
        sig = devig_signal(fair_yes, yes_p, band)
        if sig is None:
            continue
        s_side, our_p, gross, net = sig
        rec = {"ts": ts, "league": args.league, "series": args.series, "ticker": tk,
               "game": f"{ev_used['away']} @ {ev_used['home']}", "yes_team_side": side_team,
               "fair_yes": round(fair_yes, 4), "kalshi_yes": yes_p, "band": band,
               "side": s_side, "gross_edge": gross, "net_edge": net}
        hits.append(rec)
    if hits:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            for h in hits:
                f.write(json.dumps(h) + "\n")
    flagged = [h for h in hits if h["net_edge"] is not None and h["net_edge"] > 0]
    print(f"=== devig scan {args.league}/{args.series} — {len(ladder)} markets, "
          f"{matched} matched to odds, {len(flagged)} mispriced (net edge>0 after fee) ===")
    for h in sorted(flagged, key=lambda x: -x["net_edge"])[:20]:
        print(f"  {h['ticker'][:30]:30} {h['side']:>3} fair {h['fair_yes']:.2f} "
              f"vs kalshi {h['kalshi_yes']:.2f}  gross {h['gross_edge']:+.2f} "
              f"net {h['net_edge']:+.2f} (band {h['band']})")
    print("\n  Logged to data/devig_check.jsonl. NOTE: assumes the sharp book is right and "
          "Kalshi is wrong — VALIDATE by per-day PSR before trusting. Paper only.")


def selftest() -> int:
    # a fair (vig-free) 2-way book: all methods return the input untouched
    for m in DEVIG_METHODS:
        fp = fair_probs([1.5, 3.0], m)               # ios 0.6667 / 0.3333, sum 1.0
        assert abs(fp[0] - 2 / 3) < 1e-3 and abs(sum(fp) - 1) < 1e-9, (m, fp)
    print("devig (no-vig passthrough) OK")
    # symmetric vig (1.90/1.90): every method → 0.5/0.5
    for m in DEVIG_METHODS:
        fp = fair_probs([1.90, 1.90], m)
        assert abs(fp[0] - 0.5) < 1e-6, (m, fp)
    print("devig (symmetric → 0.5) OK")
    # asymmetric vig: each method sums to 1, keeps the favorite, stays in (0,1)
    band = fair_band(1.45, 2.80)                      # overround ~1.047
    for m in DEVIG_METHODS:
        assert abs(sum(fair_probs([1.45, 2.80], m)) - 1.0) < 1e-9, m
    for p in band["per_method"].values():
        assert 0.5 < p < 0.75, band
    assert band["band"] < 0.05 and band["consensus"] > 0.5, band
    print(f"fair_band OK (consensus {band['consensus']}, band {band['band']})")
    # multiplicative vs additive differ on a vig'd book (so the band is meaningful)
    assert abs(fair_probs([1.45, 2.80], "multiplicative")[0]
               - fair_probs([1.45, 2.80], "additive")[0]) > 1e-3
    print("methods diverge on vig (band is informative) OK")
    # fee: ceil(0.07·P(1−P)) cents — at P=0.5 → ceil(1.75)=2¢
    assert kalshi_fee(0.5) == 0.02, kalshi_fee(0.5)
    print("kalshi_fee OK")
    # signal: fair 0.60 vs kalshi 0.50 → buy YES, gross 0.10, net 0.10−fee(.50)=0.08
    sig = devig_signal(0.60, 0.50)
    assert sig[0] == "YES" and sig[2] == 0.10 and abs(sig[3] - 0.08) < 1e-9, sig
    # fair 0.40 vs kalshi 0.55 → buy NO, gross 0.15
    sig = devig_signal(0.40, 0.55)
    assert sig[0] == "NO" and sig[2] == 0.15, sig
    # under threshold → None; wide band → None
    assert devig_signal(0.52, 0.50) is None
    assert devig_signal(0.60, 0.50, band=0.10) is None
    print("devig_signal OK (YES/NO, fee-netted, threshold + band skips)")
    print("PASS")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["probe", "scan", "selftest"])
    ap.add_argument("league", nargs="?", help=f"one of: {', '.join(SPORTS)}")
    ap.add_argument("series", nargs="?", help="Kalshi series ticker, e.g. KXNBAGAMES")
    args = ap.parse_args()
    if args.mode == "selftest":
        raise SystemExit(selftest())
    if not args.league or not args.series:
        ap.error(f"{args.mode} needs a league and a Kalshi series, e.g. {args.mode} nba KXNBAGAMES")
    (cmd_probe if args.mode == "probe" else cmd_scan)(args)


if __name__ == "__main__":
    main()
