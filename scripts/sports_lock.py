#!/usr/bin/env python3
"""sports_lock — the sports sibling of asos_tracker: read the LIVE score of a game
from a free scoreboard feed, decide when the outcome is mathematically near-LOCKED
(a lead too big to blow with the time left), and flag when Kalshi's moneyline market
still misprices the now-near-certain side.

Same idea as the weather bucket-lock, just a different "settlement instrument":
  • weather  → the day's high is physically set by evening (temps falling) → asos_tracker
  • sports   → a 20-pt lead with 2 min left is a settled game → sports_lock
Both are a fast-OBSERVATION edge ("the number is already decided"), NOT a forecast.
The market is usually slow to crawl the last few cents to 99¢ as a blowout winds down.

Win-prob is a conservative diffusion model of the score margin:
    P(leader holds) = Φ( margin / (σ_league · √time_remaining_fraction) )
σ_league = the std of a full-game margin swing (points/goals/runs). A LOCK only fires
late in the game AND with a margin big enough that the exact σ barely matters — the
near-certainty is robust, like requiring 'evening + clearly off the peak' for weather.

READ-ONLY / paper. Logs lock signals for forward-collection; places NO orders.
Free data via ESPN's public scoreboard JSON (no key). Kalshi via the repo's client.

  python scripts/sports_lock.py scan                         # ALL live sports series (auto-discovered) — the default sweep
  python scripts/sports_lock.py scan --confirm               # ...gating each confirmable league on its 2nd feed
  python scripts/sports_lock.py probe                        # list every live per-game sports series on Kalshi
  python scripts/sports_lock.py probe nba KXNBAGAMES         # one league: ESPN + 2nd feed + ladder, VERIFY mapping
  python scripts/sports_lock.py scan  nba KXNBAGAMES         # one league only
  python scripts/sports_lock.py selftest

⚠ VERIFY two things with `probe` before trusting a signal: (1) the ESPN league matches
  the Kalshi series, and (2) team names line up so the right side is being priced.
The lock's worst failure is acting on one bad live number, so `--confirm` gates each
lock on a SECOND, independent-origin feed (NBA.com CDN / NHLE) agreeing on score+clock.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from math import erf, sqrt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
LOG = ROOT / "data" / "sports_lock.jsonl"

# league → ESPN (sport, league) path + game shape + full-game margin σ (the noisy bit).
# σ = std of a full-game final-margin swing in that sport's scoring units. These are
# rough literature values — keep them CONSERVATIVE (bigger σ = harder to call a lock).
LEAGUES = {
    "nba":   {"sport": "basketball", "league": "nba",            "periods": 4, "period_min": 12.0, "sigma": 14.0},
    "wnba":  {"sport": "basketball", "league": "wnba",           "periods": 4, "period_min": 10.0, "sigma": 13.0},
    "ncaab": {"sport": "basketball", "league": "mens-college-basketball", "periods": 2, "period_min": 20.0, "sigma": 15.0},
    "nfl":   {"sport": "football",   "league": "nfl",            "periods": 4, "period_min": 15.0, "sigma": 15.0},
    "ncaaf": {"sport": "football",   "league": "college-football","periods": 4, "period_min": 15.0, "sigma": 17.0},
    "nhl":   {"sport": "hockey",     "league": "nhl",            "periods": 3, "period_min": 20.0, "sigma": 2.6},
}

LOCK_MAX_FRAC = 0.18    # only consider a lock once ≤ this fraction of game time remains
LOCK_MIN_MARGIN = 6.0   # and the lead is at least this many points (sanity floor vs tiny σ)
BOUNDARY_PROB = 0.04    # don't trust a "lock" whose win-prob is within this of 1.0's complement
NEAR_CERTAIN = 0.98     # prob we assign a locked outcome (not 1.0 — injuries, scoring runs, OT)
CLOCK_TOL_SEC = 40      # two feeds may differ by a possession; agree within this on the clock


# ── pure logic (testable, no network) ────────────────────────────────────────

def win_prob(margin: float, time_frac: float, sigma: float) -> float:
    """P(current leader still leads at the final whistle) under a Brownian margin
    model: the remaining margin change ~ N(0, σ²·time_frac). `margin` is signed from
    the perspective of the team we're asking about (positive = currently ahead)."""
    if time_frac <= 0:
        return 1.0 if margin > 0 else (0.0 if margin < 0 else 0.5)
    if sigma <= 0:
        return 1.0 if margin > 0 else (0.0 if margin < 0 else 0.5)
    z = margin / (sigma * sqrt(time_frac))
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))            # Φ(z)


def time_remaining_frac(period: int, clock_sec: float, cfg: dict) -> float:
    """Fraction of regulation remaining from a count-DOWN game clock. period is
    1-based; clock_sec is seconds left in the current period. Overtime (period >
    regulation) returns a small positive frac (the OT time left vs regulation),
    never 0, so we stay appropriately unsure in OT."""
    periods, pmin = cfg["periods"], cfg["period_min"]
    total = periods * pmin * 60.0
    if total <= 0:
        return 1.0
    if period > periods:                                # overtime: only the OT clock remains
        return max(0.0, min(1.0, clock_sec / total))
    periods_left_after = periods - period               # whole periods after this one
    remaining = clock_sec + periods_left_after * pmin * 60.0
    return max(0.0, min(1.0, remaining / total))


def is_locked(time_frac: float, margin: float, sigma: float,
              max_frac: float = LOCK_MAX_FRAC, min_margin: float = LOCK_MIN_MARGIN) -> bool:
    """A game is 'locked' once it's late AND the lead is big enough that the win-prob
    clears NEAR_CERTAIN — three gates so a single soft one can't fire it alone."""
    if margin is None or time_frac is None:
        return False
    if abs(margin) < min_margin:
        return False
    if time_frac > max_frac:
        return False
    return win_prob(abs(margin), time_frac, sigma) >= NEAR_CERTAIN


def lock_signal(outcome_is_yes, lock_prob: float, market_yes,
                boundary: float = BOUNDARY_PROB):
    """Given a LOCKED game, is the moneyline mispriced? `outcome_is_yes` = does the
    near-certain winner correspond to this market's YES side? (True/False/None).
    Returns (side, certain_prob, edge) or None. edge = our prob − market's implied
    prob for the side we'd buy (positive = mispriced in our favor). market_yes may be
    None (edge=None, signal still shown). Skips if lock_prob isn't actually certain."""
    if outcome_is_yes is None:
        return None
    if lock_prob < NEAR_CERTAIN - boundary:             # not actually locked → skip
        return None
    if outcome_is_yes:                                  # YES near-certain → buy YES
        edge = (lock_prob - market_yes) if market_yes is not None else None
        return ("YES", lock_prob, (round(edge, 3) if edge is not None else None))
    # NO near-certain → buy NO (implied NO cost = market_yes)
    edge = (market_yes - (1.0 - lock_prob)) if market_yes is not None else None
    return ("NO", lock_prob, (round(edge, 3) if edge is not None else None))


def _norm(s: str) -> str:
    """Loose team-name key for matching ESPN ↔ Kalshi (lowercase alnum only)."""
    return "".join(c for c in str(s).lower() if c.isalnum())


def match_team(name: str, candidates: list) -> str | None:
    """Best-effort: return the candidate whose normalized name contains, or is
    contained by, `name`'s. Returns None if no unambiguous hit."""
    key = _norm(name)
    if not key:
        return None
    hits = [c for c in candidates if key and (key in _norm(c) or _norm(c) in key)]
    return hits[0] if len(hits) == 1 else None


def _same_team(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na and nb and (na in nb or nb in na))


def _same_game(p: dict, s: dict) -> bool:
    """Two source records describe the same matchup (home↔home, away↔away)."""
    return (_same_team(p.get("home"), s.get("home"))
            and _same_team(p.get("away"), s.get("away")))


def reconcile(primary: list, secondary: list, clock_tol: float = CLOCK_TOL_SEC) -> list:
    """Two-source confirmation gate: return the PRIMARY game dicts (we keep the primary
    feed's shape downstream) for in-progress games where an independent SECONDARY feed
    agrees on the matchup, the exact score, the period, and the clock (within clock_tol
    seconds). A game seen by only one feed, or where the feeds disagree, is DROPPED —
    so a single glitchy live number can never fire a lock. Pure; no network."""
    kept = []
    for p in primary:
        if p.get("state") != "in":
            continue
        ps, pa, pp, pc = (p.get("home_score"), p.get("away_score"),
                          p.get("period"), p.get("clock_sec"))
        if None in (ps, pa, pp, pc):
            continue
        for s in secondary:
            if s.get("state") != "in" or not _same_game(p, s):
                continue
            if (s.get("home_score") == ps and s.get("away_score") == pa
                    and s.get("period") is not None and int(s["period"]) == int(pp)
                    and s.get("clock_sec") is not None
                    and abs(float(s["clock_sec"]) - float(pc)) <= clock_tol):
                kept.append(p)
            break                                            # matched the game (agree or not)
    return kept


# ── live fetch (needs network) ───────────────────────────────────────────────

def fetch_espn_games(cfg: dict) -> list:
    """In-progress games for a league via ESPN's public scoreboard JSON (no key).
    Returns [dict(home, away, home_score, away_score, period, clock_sec, state)]."""
    import requests
    url = (f"https://site.api.espn.com/apis/site/v2/sports/"
           f"{cfg['sport']}/{cfg['league']}/scoreboard")
    r = requests.get(url, timeout=25)
    r.raise_for_status()
    out = []
    for ev in r.json().get("events", []) or []:
        comp = (ev.get("competitions") or [{}])[0]
        status = comp.get("status", {}) or {}
        stype = (status.get("type") or {})
        state = stype.get("state")                      # pre / in / post
        cs = status.get("clock")                        # seconds left in period (float)
        period = status.get("period")                   # 1-based
        teams = {}
        for c in comp.get("competitors", []) or []:
            ha = c.get("homeAway")
            team = (c.get("team") or {})
            try:
                score = float(c.get("score"))
            except (TypeError, ValueError):
                score = None
            teams[ha] = {"name": team.get("displayName") or team.get("shortDisplayName"),
                         "abbr": team.get("abbreviation"), "score": score}
        if "home" in teams and "away" in teams:
            out.append({
                "home": teams["home"]["name"], "away": teams["away"]["name"],
                "home_abbr": teams["home"]["abbr"], "away_abbr": teams["away"]["abbr"],
                "home_score": teams["home"]["score"], "away_score": teams["away"]["score"],
                "period": period, "clock_sec": cs, "state": state,
            })
    return out


def _iso_clock_to_sec(s: str):
    """NBA gameClock 'PT05M21.00S' → 321.0 seconds. None if unparseable."""
    try:
        body = str(s).upper().split("PT", 1)[1]
        mins = float(body.split("M", 1)[0]) if "M" in body else 0.0
        secs = float(body.split("M", 1)[1].rstrip("S")) if "M" in body else float(body.rstrip("S"))
        return mins * 60.0 + secs
    except (IndexError, ValueError, AttributeError):
        return None


def fetch_nba_cdn_games(cfg: dict, debug: bool = False):
    """SECONDARY (NBA only): NBA.com's live CDN scoreboard — a DIFFERENT origin from
    ESPN, so it's a real independent confirmation. No key. Returns the normalized game
    shape, or None if unavailable (so --confirm can tell 'no second opinion' apart from
    'no agreement'). Pass debug=True (probe does) to print WHY it failed."""
    import requests
    try:
        r = requests.get("https://cdn.nba.com/static/json/liveData/scoreboard/"
                         "todaysScoreboard_00.json", timeout=20)
        r.raise_for_status()
        games = (r.json().get("scoreboard") or {}).get("games") or []
    except Exception as e:
        if debug:
            print(f"  [nba 2nd feed] {type(e).__name__}: {e}", file=sys.stderr)
        return None
    out = []
    for g in games:
        st = {1: "pre", 2: "in", 3: "post"}.get(g.get("gameStatus"))
        h, a = g.get("homeTeam") or {}, g.get("awayTeam") or {}
        out.append({
            "home": h.get("teamName"), "away": a.get("teamName"),
            "home_score": float(h.get("score")) if h.get("score") is not None else None,
            "away_score": float(a.get("score")) if a.get("score") is not None else None,
            "period": g.get("period"), "clock_sec": _iso_clock_to_sec(g.get("gameClock")),
            "state": st,
        })
    return out


def fetch_nhl_api_games(cfg: dict, debug: bool = False):
    """SECONDARY (NHL only): NHLE's public score feed (api-web.nhle.com) — independent
    of ESPN. No key. Returns the normalized shape or None if unavailable. Pass
    debug=True (probe does) to print WHY it failed."""
    import requests
    try:
        r = requests.get("https://api-web.nhle.com/v1/score/now", timeout=20)
        r.raise_for_status()
        games = r.json().get("games") or []
    except Exception as e:
        if debug:
            print(f"  [nhl 2nd feed] {type(e).__name__}: {e}", file=sys.stderr)
        return None
    out = []
    for g in games:
        gs = g.get("gameState")
        st = "in" if gs in ("LIVE", "CRIT") else ("post" if gs in ("FINAL", "OFF") else "pre")
        h, a = g.get("homeTeam") or {}, g.get("awayTeam") or {}
        clk = (g.get("clock") or {}).get("secondsRemaining")
        out.append({
            "home": (h.get("name") or {}).get("default") or h.get("abbrev"),
            "away": (a.get("name") or {}).get("default") or a.get("abbrev"),
            "home_score": float(h.get("score")) if h.get("score") is not None else None,
            "away_score": float(a.get("score")) if a.get("score") is not None else None,
            "period": g.get("period"), "clock_sec": float(clk) if clk is not None else None,
            "state": st,
        })
    return out


# leagues with an independent second feed available for the --confirm gate
SECONDARY = {"nba": fetch_nba_cdn_games, "nhl": fetch_nhl_api_games}


def fetch_market_ladder(series: str) -> list:
    """Open markets for the series → [(ticker, title, yes_sub_title, yes_p)].
    yes_p from the mid when both sides are quoted, else last_price (quote-on-demand
    books are often empty), else None. The title/yes_sub_title carry the team."""
    from fetch_backtest_data import _kalshi_get
    try:
        data = _kalshi_get("/markets", {"series_ticker": series, "status": "open", "limit": 200})
    except Exception as e:
        print(f"  ! {series}: {e}", file=sys.stderr)
        return []
    out = []
    for m in data.get("markets", []) or []:
        tk = m.get("ticker", "")
        yb, ya, last = m.get("yes_bid"), m.get("yes_ask"), m.get("last_price")
        if isinstance(yb, (int, float)) and isinstance(ya, (int, float)) and 0 < yb and ya < 100:
            yes_p = (yb + ya) / 200.0
        elif isinstance(last, (int, float)) and 0 < last < 100:
            yes_p = last / 100.0
        else:
            yes_p = None
        out.append((tk, m.get("title", ""), m.get("yes_sub_title", "") or "", yes_p))
    return out


def infer_league(text: str):
    """Map a Kalshi series ticker/title to one of our league codes (None if we don't
    model it). Order matters: WNBA before NBA (substring); college before the pros it
    contains nothing of. Covers both sleeves — sports_lock skips ones it can't clock
    (e.g. mlb), devig handles those."""
    t = str(text).upper()
    if "WNBA" in t:
        return "wnba"
    if "NBA" in t:
        return "nba"
    if "NHL" in t:
        return "nhl"
    if "NCAAF" in t or "COLLEGE FOOTBALL" in t:
        return "ncaaf"
    if "NCAAB" in t or "NCAAM" in t or "COLLEGE BASKETBALL" in t:
        return "ncaab"
    if "NFL" in t:
        return "nfl"
    if "MLB" in t:
        return "mlb"
    return None


def discover_game_series() -> list:
    """Live Kalshi per-game sports series → [(league, ticker, title)]. Reads
    /series?category=Sports and keeps the ones whose league we recognize AND whose
    ticker looks per-game (contains GAME), so player-props/futures series are skipped.
    Auto-adapts to the season: off-season leagues simply have no live games to lock."""
    from fetch_backtest_data import _kalshi_get
    try:
        data = _kalshi_get("/series", {"category": "Sports", "limit": 200})
    except Exception as e:
        print(f"! discover sports series: {e}", file=sys.stderr)
        return []
    seen, out = set(), []
    for s in data.get("series", []) or []:
        tk = (s.get("ticker") or "")
        league = infer_league(tk + " " + (s.get("title") or ""))
        if league and "GAME" in tk.upper() and tk not in seen:
            seen.add(tk)
            out.append((league, tk, s.get("title") or ""))
    return out


def _eval_game(g: dict, cfg: dict):
    """Pure-ish: given an in-progress game dict, return (leader_name, margin, tf,
    p, locked) or None if it isn't an evaluable in-progress game."""
    if g.get("state") != "in":
        return None
    hs, as_ = g.get("home_score"), g.get("away_score")
    period, cs = g.get("period"), g.get("clock_sec")
    if None in (hs, as_, period, cs):
        return None
    margin = hs - as_
    leader = g["home"] if margin >= 0 else g["away"]
    tf = time_remaining_frac(int(period), float(cs), cfg)
    p = win_prob(abs(margin), tf, cfg["sigma"])
    locked = is_locked(tf, margin, cfg["sigma"])
    return leader, abs(margin), tf, p, locked


def cmd_probe(args) -> None:
    cfg = LEAGUES.get(args.league)
    if not cfg:
        print(f"unknown league {args.league}; known: {', '.join(LEAGUES)}")
        return
    print(f"=== PROBE {args.league} (ESPN {cfg['sport']}/{cfg['league']}) ↔ Kalshi {args.series} ===")
    games = fetch_espn_games(cfg)
    live = [g for g in games if g.get("state") == "in"]
    print(f"ESPN: {len(games)} games, {len(live)} in-progress:")
    for g in games[:12]:
        ev = _eval_game(g, cfg)
        tag = ""
        if ev:
            leader, m, tf, p, locked = ev
            tag = f"  [{leader} +{m:.0f}, {tf*100:.0f}% left, P={p:.2f}{' LOCKED' if locked else ''}]"
        print(f"  {g['state']:>4} {g['away']} @ {g['home']}  "
              f"{g['away_score']}-{g['home_score']} Q{g['period']}{tag}")
    sec = SECONDARY.get(args.league)
    if sec is not None:
        s = sec(cfg, debug=True)
        n = sum(1 for g in (s or []) if g.get("state") == "in")
        print(f"\nSecondary feed ({args.league}): "
              + (f"available, {n} in-progress — scan --confirm will gate on it"
                 if s is not None else "UNAVAILABLE here (network) — verify locally"))
    else:
        print(f"\nSecondary feed: none for {args.league} (only {', '.join(SECONDARY)} "
              "are confirmable; scan --confirm runs this league single-source).")
    ladder = fetch_market_ladder(args.series)
    print(f"\nKalshi ladder ({len(ladder)} markets):")
    for tk, title, sub, yes_p in ladder[:12]:
        print(f"  {tk[:34]:34} yes~{yes_p}  «{sub or title}»")
    print("\nCHECK: do the ESPN teams match the Kalshi team names, and is this the right "
          "league for this series? If yes, trust scan. If team names don't line up, the "
          "side mapping will be wrong — fix match_team or pick the correct series.")


def _write_hits(hits: list) -> None:
    if hits:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            for h in hits:
                f.write(json.dumps(h) + "\n")


def _scan_one(league: str, series: str, ts: str, confirm: bool):
    """Scan ONE league/series → (hits, locked_games), printing the per-league confirm
    note. --confirm gates only where a 2nd feed exists; a league without one runs
    single-source (so 'scan all' still covers every sport)."""
    cfg = LEAGUES.get(league)
    if not cfg:
        return [], 0
    try:
        games = fetch_espn_games(cfg)
    except Exception as e:
        print(f"! ESPN {league}: {e}", file=sys.stderr)
        return [], 0
    if confirm:
        fetcher = SECONDARY.get(league)
        if fetcher is None:
            print(f"  [{league}] --confirm: no 2nd feed — running single-source.")
        else:
            secondary = fetcher(cfg)
            if secondary is None:
                print(f"  [{league}] --confirm: 2nd feed unavailable — suppressing locks.")
                games = []
            else:
                before = sum(1 for g in games if g.get("state") == "in")
                games = reconcile(games, secondary)
                print(f"  [{league}] --confirm: {len(games)}/{before} in-progress games agree.")
    ladder = fetch_market_ladder(series)
    hits, locked_games = [], 0
    for g in games:
        ev = _eval_game(g, cfg)
        if not ev:
            continue
        leader, margin, tf, p, locked = ev
        if not locked:
            continue
        locked_games += 1
        # The yes_sub_title names the YES outcome (e.g. "Boston Celtics"):
        #   YES sub names the leader → buy YES;  YES sub names the loser → buy NO.
        loser = g["home"] if leader == g["away"] else g["away"]
        match, outcome_is_yes = None, None
        for tk, title, sub, yes_p in ladder:
            subk = _norm(sub)
            if _norm(leader) and _norm(leader) in subk:
                match, outcome_is_yes = (tk, title, sub, yes_p), True
                break
            if _norm(loser) and _norm(loser) in subk:
                match, outcome_is_yes = (tk, title, sub, yes_p), False
                break
        if not match or outcome_is_yes is None:
            continue
        tk, title, sub, yes_p = match
        sig = lock_signal(outcome_is_yes, NEAR_CERTAIN, yes_p)
        if sig is None:
            continue
        side, prob, edge = sig
        hits.append({"ts": ts, "league": league, "series": series, "ticker": tk,
                     "game": f"{g['away']} @ {g['home']}", "leader": leader,
                     "margin": margin, "time_frac": round(tf, 3), "win_prob": round(p, 4),
                     "side": side, "market_yes": yes_p, "edge": edge})
    return hits, locked_games


def _print_scan_summary(label: str, hits: list, locked_games: int) -> None:
    flagged = [h for h in hits if h["edge"] is not None and h["edge"] > 0]
    print(f"=== sports lock scan {label} — {locked_games} locked games, "
          f"{len(hits)} matched to markets, {len(flagged)} mispriced (edge>0) ===")
    for h in sorted(flagged, key=lambda x: -x["edge"])[:20]:
        print(f"  {h['ticker'][:30]:30} {h['side']:>3} {h['leader']} +{h['margin']:.0f} "
              f"({h['time_frac']*100:.0f}% left) mkt_yes {h['market_yes']} edge {h['edge']:+.2f}")


def cmd_scan(args) -> None:
    if not LEAGUES.get(args.league):
        print(f"unknown league {args.league}; known: {', '.join(LEAGUES)}")
        return
    ts = datetime.now(timezone.utc).isoformat()
    hits, locked = _scan_one(args.league, args.series, ts, getattr(args, "confirm", False))
    _write_hits(hits)
    _print_scan_summary(f"{args.league}/{args.series}", hits, locked)
    print("\n  Logged to data/sports_lock.jsonl. Paper only — top-of-book + a live score "
          "that can still swing; confirm fillability and team mapping before trusting.")


def cmd_scan_all(args) -> None:
    """Auto-discover every live per-game sports series and scan them all."""
    ts = datetime.now(timezone.utc).isoformat()
    series = discover_game_series()
    modeled = [(lg, tk, ti) for lg, tk, ti in series if lg in LEAGUES]
    skipped = sorted({lg for lg, _, _ in series if lg not in LEAGUES})
    covered = ", ".join(sorted({lg for lg, _, _ in modeled})) or "none live"
    print(f"=== sports lock scan ALL — {len(modeled)} live per-game series ({covered}) ===")
    if skipped:
        print(f"  (skipped {', '.join(skipped)}: no clock model in the lock — devig_check covers them)")
    all_hits, total_locked = [], 0
    for lg, tk, ti in modeled:
        hits, locked = _scan_one(lg, tk, ts, getattr(args, "confirm", False))
        total_locked += locked
        all_hits += hits
        if hits or locked:
            _print_scan_summary(f"{lg}/{tk}", hits, locked)
    _write_hits(all_hits)
    flagged = [h for h in all_hits if h["edge"] is not None and h["edge"] > 0]
    print(f"\nTOTAL across all live series: {total_locked} locked, {len(all_hits)} matched, "
          f"{len(flagged)} mispriced. Logged to data/sports_lock.jsonl. Paper only.")


def cmd_probe_all(args) -> None:
    """List every live per-game sports series Kalshi exposes (the universe scan sweeps)."""
    series = discover_game_series()
    print(f"=== PROBE ALL — {len(series)} per-game sports series on Kalshi ===")
    if not series:
        print("  none discovered (off-season, or run on your home IP with Kalshi auth).")
        return
    for lg, tk, ti in series:
        ladder = fetch_market_ladder(tk)
        cover = "lock+devig" if lg in LEAGUES else "devig-only"
        print(f"  {lg:6} {tk:22} {len(ladder):>3} open  [{cover}]  «{ti}»")
    print("\nThen: `scan` (sweeps all)  |  `probe <league> <series>` for team-mapping detail.")


def selftest() -> int:
    cfg = LEAGUES["nba"]
    # win_prob: big lead late → ~1; tied → 0.5; behind late → ~0
    assert win_prob(20, 0.02, 14.0) > 0.999, win_prob(20, 0.02, 14.0)
    assert abs(win_prob(0, 0.5, 14.0) - 0.5) < 1e-9
    assert win_prob(-20, 0.02, 14.0) < 0.001
    assert win_prob(5, 0.0, 14.0) == 1.0                      # clock at 0, ahead → certain
    print("win_prob OK")
    # time_remaining_frac: start of NBA Q4 with 12:00 left = 1/4 of game remains
    assert abs(time_remaining_frac(4, 12 * 60, cfg) - 0.25) < 1e-9, time_remaining_frac(4, 12 * 60, cfg)
    assert abs(time_remaining_frac(1, 12 * 60, cfg) - 1.0) < 1e-9   # opening tip
    assert abs(time_remaining_frac(4, 0, cfg)) < 1e-9              # buzzer
    assert 0 < time_remaining_frac(5, 60, cfg) < 0.05             # OT: small but nonzero
    print("time_remaining_frac OK")
    # is_locked: late + big lead → locked; late + small lead → not; early + big → not
    assert is_locked(0.02, 18, 14.0) is True                 # ~1 min left, 18 up
    assert is_locked(0.02, 4, 14.0) is False                 # below min-margin floor
    assert is_locked(0.40, 18, 14.0) is False                # too early (40% left)
    assert is_locked(0.08, 10, 14.0) is True                 # 10 up, ~4 min left → P≈0.99
    assert is_locked(0.08, 8, 14.0) is False                 # 8 up, ~4 min left → P≈0.978, honest near-miss
    print("is_locked OK")
    # lock_signal: locked winner is YES → buy YES, edge vs market
    sig = lock_signal(True, NEAR_CERTAIN, market_yes=0.80)
    assert sig == ("YES", 0.98, 0.18), sig
    # locked winner is NOT the YES side → buy NO; market_yes 0.30 → edge 0.30-0.02
    sig = lock_signal(False, NEAR_CERTAIN, market_yes=0.30)
    assert sig[0] == "NO" and abs(sig[2] - (0.30 - 0.02)) < 1e-9, sig
    # unknown side or sub-certain prob → no signal
    assert lock_signal(None, NEAR_CERTAIN, 0.5) is None
    assert lock_signal(True, 0.90, 0.5) is None
    print("lock_signal OK")
    # match_team: unambiguous contains-match; ambiguous → None
    assert match_team("Boston Celtics", ["Celtics", "Lakers"]) == "Celtics"
    assert match_team("Lakers", ["Los Angeles Lakers", "Los Angeles Clippers"]) == "Los Angeles Lakers"
    assert match_team("Foo", ["Bar", "Baz"]) is None
    print("match_team OK")
    # reconcile: agree → kept; score mismatch / clock drift / unseen → dropped
    prim = [{"home": "Los Angeles Lakers", "away": "Boston Celtics", "home_score": 99.0,
             "away_score": 90.0, "period": 4, "clock_sec": 30.0, "state": "in"}]
    agree = [{"home": "Lakers", "away": "Celtics", "home_score": 99.0, "away_score": 90.0,
              "period": 4, "clock_sec": 45.0, "state": "in"}]              # 15s drift < tol
    assert reconcile(prim, agree) == prim, "agreeing feeds should keep the game"
    bad_score = [dict(agree[0], home_score=98.0)]
    assert reconcile(prim, bad_score) == [], "score disagreement must drop the game"
    bad_clock = [dict(agree[0], clock_sec=300.0)]                          # 270s drift > tol
    assert reconcile(prim, bad_clock) == [], "clock disagreement must drop the game"
    assert reconcile(prim, []) == [], "a game the second feed can't see is dropped"
    assert reconcile(prim, [dict(agree[0], state="post")]) == [], "non-live second drop"
    print("reconcile OK")
    # iso clock parse
    assert abs(_iso_clock_to_sec("PT05M21.00S") - 321.0) < 1e-6
    assert abs(_iso_clock_to_sec("PT00M30.0S") - 30.0) < 1e-6
    assert _iso_clock_to_sec("garbage") is None
    print("_iso_clock_to_sec OK")
    # infer_league: specific codes, WNBA before NBA, None for sports we don't model
    assert infer_league("KXNBAGAMES") == "nba"
    assert infer_league("WNBA games") == "wnba"
    assert infer_league("KXNHLGAMES") == "nhl"
    assert infer_league("KXNCAAFGAMES") == "ncaaf"
    assert infer_league("College basketball game") == "ncaab"
    assert infer_league("KXNFLGAMES") == "nfl"
    assert infer_league("KXMLBGAMES") == "mlb"
    assert infer_league("Wimbledon mens winner") is None
    print("infer_league OK")
    print("PASS")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["probe", "scan", "selftest"])
    ap.add_argument("league", nargs="?",
                    help=f"one of: {', '.join(LEAGUES)} — omit (or 'all') to sweep every live series")
    ap.add_argument("series", nargs="?", help="Kalshi series ticker, e.g. KXNBAGAMES")
    ap.add_argument("--confirm", action="store_true",
                    help="gate each confirmable league on an independent 2nd feed agreeing on "
                         f"score+clock (available: {', '.join(SECONDARY)}; others run single-source)")
    args = ap.parse_args()
    if args.mode == "selftest":
        raise SystemExit(selftest())
    all_mode = args.league is None or args.league.lower() == "all"
    if args.mode == "probe":
        cmd_probe_all(args) if all_mode else cmd_probe(args)
    elif all_mode:
        cmd_scan_all(args)
    elif not args.series:
        ap.error("scan needs a series (e.g. scan nba KXNBAGAMES) — or omit league to sweep ALL")
    else:
        cmd_scan(args)


if __name__ == "__main__":
    main()
