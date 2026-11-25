#!/usr/bin/env python3
"""sleeve_paper - forward-only paper trading across many Kalshi sleeves.

The crypto paper trader (scripts/paper_trader.py) tests rules on 15-min crypto. This does
the same, to the same standard, on every other sleeve worth watching - and nothing here
places a real order.

WHAT IT TRADES - PRE-REGISTERED RULES ONLY. Rules live in an append-only registry
(data/sleeve_rules.jsonl). Each carries a `named_at` stamp; only trades opened AFTER it
count, and a rule is never edited - it can only be retired by a later row. Two sources:
  register-probes       the favorite-longshot probe pair for each top sleeve: buy the
                        favorite at 80-95c, and its mirror, the longshot at 5-20c, in the
                        mid-life band. The most documented pricing bias in betting
                        markets, tested the same way everywhere - a small, theory-driven
                        family instead of a grid (docs/SLEEVES.md says why that matters).
  register-nominations  clean nominations from `sleeve_scan.py screen`.

HOW IT TRADES - the house realism, shared code where it exists:
  * enter at the ASK on the rule's side, pay the real ceil-to-cent Kalshi fee
  * the fill gate: refuse when the captured order book has no liftable size behind the
    quote (shadow_book.liftable_depth - the parser fixed 2026-09-25); a sized order needs
    its whole size AT the ask, the price it is booked at (the book's one-tick skew
    tolerance is for 1-lots only); entries whose depth lookup failed are admitted but
    COUNTED as ungated, never hidden
  * ONE bet per (event, rule): every strike of an event resolves on one outcome
  * the rule's own ORDER SIZE (10 contracts for every rule registered since sizes were
    stored): Kalshi rounds the fee per order UP to the cent, so a 1-lot at 99c pays a 1c
    fee on a 1c win - a 1-lot test would judge the rounding, not the rule
  * each entry is stamped with the news regime (lib/news_regime.py) - a stamp only
  * settle only from Kalshi's own result

ONE HYPOTHESIS = ONE RULE = ONE TEST. A rule may span many series (a friend's method on
seven weather cities is one rule); splitting it per series would multiply the tests and
divide each one's sample. Pooling ASSUMES the series settle independently: each series'
event is its own bet, so series on one underlying (two S&P 500 series) must never be
pooled - a shared outcome would count twice and the exact p would be too small. `report`
and `register-rule` say so whenever a rule spans several series.

HOW IT IS JUDGED - `report`: per rule, the forward record after named_at, per EVENT and
per CONTRACT, with EXACT p-values (forward_stats: a favorite that hasn't lost yet is not
proof). Benjamini-Hochberg runs across ALL forward rules at once - the command center's
family: every sleeve rule, RETIRED ones included, and the crypto rules on fill-checked
trades - so adding sleeves never lowers the bar, and retiring one never raises another.

  py scripts/sleeve_paper.py register-probes [--top 20 | --series A,B]   # the FLB probe pair
  py scripts/sleeve_paper.py register-nominations         # after sleeve_scan screen
  py scripts/sleeve_paper.py register-rule --name F9799 --series KXHIGHNY,KXHIGHCHI \
        --side favorite --lo 0.97 --hi 0.995 --band any [--contracts 10] \
        [--min-hours 6 --max-hours 12]                    # an outside method, named now
  py scripts/sleeve_paper.py run                          # loop (PolybotSleeves task)
  py scripts/sleeve_paper.py cycle                        # one pass
  py scripts/sleeve_paper.py report
  py scripts/sleeve_paper.py rules
  py scripts/sleeve_paper.py retire <NAME> [<NAME> ...] | --prefix F9799_
  py scripts/sleeve_paper.py selftest
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
DATA = Path(os.environ.get("SLEEVE_PAPER_DIR") or (ROOT / "data"))
RULES = DATA / "sleeve_rules.jsonl"
LEDGER = DATA / "paper_sleeves.jsonl"
HEARTBEAT = DATA / "sleeve_paper_heartbeat.json"
CATALOG = Path(os.environ.get("SLEEVE_DIR") or (ROOT / "data" / "sleeves")) / "catalog.json"
SCREEN = CATALOG.parent / "screen.json"

VERSION = 2                # 2: sized orders need all n contracts AT the ask (2026-11-16)
CONTRACTS = 1             # the size of rules (and positions) from before sizes were stored
NEW_RULE_CONTRACTS = 10   # stamped on every rule registered from now on - see contracts_of
CYCLE_S = 120
SLEEP_S = 0.35
MAX_SPREAD = 0.15
MAX_OPEN_PER_RULE = 50
# covered by scripts/paper_trader.py - never double-trade a sleeve
EXCLUDED_SERIES = {"KXBTC15M", "KXETH15M"}
LIFE_BANDS = (("early", 0.00, 0.25), ("mid", 0.25, 0.75),
              ("late", 0.75, 0.95), ("final", 0.95, 1.0001))
PROBES = (("FLB_FAV", "favorite", 0.80, 0.95,
           "favorite-longshot bias: favorites underpriced - buy the favorite at 80-95c"),
          ("FLB_LONG", "longshot", 0.05, 0.20,
           "mirror control: longshots overpriced - buying them at 5-20c should LOSE"))
MIN_EVENTS_VERDICT = 100
MIN_TESTABLE_EVENTS = 30
VERDICT_TEXT = {
    "collecting": "collecting ({n}/{need} events)",
    "edge": "EDGE - survives the forward family correction",
    "promising": "promising - significant, needs {need} events",
    "negative": "NEGATIVE (significant)",
    "positive_ns": "positive, not significant",
    "negative_ns": "negative, not significant",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _ts(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# ── prices: the FIELD NAME decides the unit, never a magnitude guess ──────────

def price(m: dict, name: str) -> float | None:
    """Kalshi serves `<name>_dollars` as dollar strings and `<name>` as integer CENTS.
    Guessing the unit from the magnitude is how a 1-cent bid once read as $1.00."""
    v = m.get(f"{name}_dollars")
    if v is not None:
        try:
            x = float(v)
            return x if math.isfinite(x) else None
        except (TypeError, ValueError):
            return None
    v = m.get(name)
    if v is None:
        return None
    try:
        x = float(v) / 100.0
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def band_of(frac: float) -> str | None:
    for name, lo, hi in LIFE_BANDS:
        if lo <= frac < hi:
            return name
    return None


def event_of(m: dict) -> str:
    ev = m.get("event_ticker")
    if ev:
        return str(ev)
    parts = str(m.get("ticker") or "").rsplit("-", 1)
    return parts[0] if len(parts) == 2 else str(m.get("ticker"))


# ── registry: append-only ─────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                rows.append(d)
    return rows


def _append(path: Path, rows: list[dict]) -> None:
    """Append rows in ONE write, never onto a torn line: a process killed mid-append
    leaves the file without its final newline, and the next row glued to that line was
    lost with it - an entry that then never settled (lib.jsonl_io.append_lines)."""
    if not rows:
        return
    from lib.jsonl_io import append_lines
    append_lines(path, [json.dumps(r, separators=(",", ":"), default=str) for r in rows])


def active_rules(path: Path = RULES) -> dict[str, dict]:
    """name -> rule, for registered and not retired. First registration of a name wins:
    a later row with the same name can never silently redefine a rule."""
    rules: dict[str, dict] = {}
    retired: set[str] = set()
    for r in _load_jsonl(path):
        if r.get("t") == "rule" and r.get("name") and r["name"] not in rules:
            rules[r["name"]] = r
        elif r.get("t") == "retire":
            retired.add(r.get("name"))
    return {n: r for n, r in rules.items() if n not in retired}


def all_registered(path: Path = RULES) -> set[str]:
    return {r["name"] for r in _load_jsonl(path) if r.get("t") == "rule" and r.get("name")}


def registered_rules(path: Path = RULES) -> dict[str, dict]:
    """name -> rule for EVERY registered name, retired ones included (first registration
    wins, as in active_rules)."""
    rules: dict[str, dict] = {}
    for r in _load_jsonl(path):
        if r.get("t") == "rule" and r.get("name") and r["name"] not in rules:
            rules[r["name"]] = r
    return rules


def series_of(rule: dict) -> list[str]:
    """The series a rule trades: one (older rules store a string) or many (a list). Either
    way it is ONE hypothesis and ONE test."""
    s = rule.get("series")
    items = s if isinstance(s, list) else [s]
    return list(dict.fromkeys(x for x in items if x))


def series_label(rule: dict) -> str:
    s = series_of(rule)
    return ",".join(s) if len(s) <= 2 else f"{s[0]} +{len(s) - 1} more"


def contracts_of(rule_or_pos: dict) -> int:
    """Order size of a rule or a position. Absent on anything from before sizes were
    stored, which traded CONTRACTS."""
    try:
        return max(1, int(rule_or_pos.get("contracts") or CONTRACTS))
    except (TypeError, ValueError):
        return CONTRACTS


def register(rules: list[dict], path: Path = RULES, now: datetime | None = None) -> list[str]:
    """Append new rules (never re-register an existing name). Returns names added."""
    now = now or _now()
    have = all_registered(path)
    new = []
    for r in rules:
        if r["name"] in have:
            continue
        new.append(dict(r, t="rule", named_at=_iso(now), v=VERSION))
        have.add(r["name"])
    _append(path, new)
    return [r["name"] for r in new]


def probe_rules(series_list: list[str]) -> list[dict]:
    out = []
    for s in series_list:
        if s in EXCLUDED_SERIES:
            continue
        for tag, side, lo, hi, thesis in PROBES:
            out.append({"name": f"{tag}_{s}", "series": s, "band": "mid", "side": side,
                        "lo": lo, "hi": hi, "contracts": NEW_RULE_CONTRACTS,
                        "source": "probe", "thesis": thesis})
    return out


def nomination_rules(screen: dict) -> list[dict]:
    out = []
    for c in screen.get("nominated") or []:
        if c.get("verdict") != "NOMINATED" or c.get("series") in EXCLUDED_SERIES:
            continue                      # phantom-risk nominations are never traded
        lo_c, hi_c = (int(x) for x in str(c["bucket"]).rstrip("c").split("-"))
        out.append({"name": f"NOM_{c['series']}_{c['band']}_{c['side']}_{c['bucket']}",
                    "series": c["series"], "band": c["band"], "side": c["side"],
                    "lo": lo_c / 100.0, "hi": hi_c / 100.0, "contracts": NEW_RULE_CONTRACTS,
                    "source": "sleeve_scan",
                    "thesis": f"scanner nomination, out-of-sample p={c.get('p_out'):.2g}"})
    return out


# ── evaluation (pure) ─────────────────────────────────────────────────────────

def candidates(m: dict, now_ts: float, rules: list[dict]) -> list[dict]:
    """Which rules fire on this open market right now. Pure."""
    o, c = _ts(m.get("open_time")), _ts(m.get("close_time"))
    if o is None or c is None or not (o < now_ts < c):
        return []
    band = band_of((now_ts - o) / (c - o))
    hours_left = (c - now_ts) / 3600.0
    yb, ya = price(m, "yes_bid"), price(m, "yes_ask")
    if band is None or yb is None or ya is None:
        return []
    if not (0.01 <= yb < ya <= 0.99 and ya - yb <= MAX_SPREAD):
        return []                          # unformed / placeholder book
    na = round(1.0 - yb, 4)               # buying NO = selling YES at the bid
    fav = ("yes", ya) if ya >= na else ("no", na)
    lng = ("no", na) if ya >= na else ("yes", ya)
    out = []
    for r in rules:
        if r["band"] not in (band, "any"):        # "any": the rule trades at any time
            continue
        if not (r.get("min_h", 0.0) <= hours_left <= r.get("max_h", 1e9)):
            continue                              # optional hours-before-close window
        side, ask = {"favorite": fav, "longshot": lng, "yes": ("yes", ya),
                     "no": ("no", na)}.get(r["side"], (None, None))
        if side is None or not (r["lo"] <= ask < r["hi"]):
            continue
        out.append({"rule": r["name"], "side": side, "price": round(ask, 4),
                    "band": band, "life_frac": round((now_ts - o) / (c - o), 3)})
    return out


def kalshi_fee(p: float, contracts: int = CONTRACTS) -> float:
    """The real fee for ONE ORDER of `contracts` at price p (ceil to the cent per order)."""
    from shadow_book import kalshi_taker_fee
    return kalshi_taker_fee(p, contracts)


def settle(pos: dict, result: str) -> dict:
    won = result == pos["side"]
    n, fee = contracts_of(pos), pos.get("fee", 0.0)
    pnl = (n * (1.0 - pos["price"]) if won else -n * pos["price"]) - fee
    return {"t": "close", "ts": _iso(_now()), "ticker": pos["ticker"], "rule": pos["rule"],
            "event": pos["event"], "series": pos["series"], "side": pos["side"],
            "price": pos["price"], "result": result, "won": won, "pnl": round(pnl, 4)}


def open_positions(rows: list[dict]) -> dict[tuple, dict]:
    opened = {(r["ticker"], r["rule"]): r for r in rows if r.get("t") == "open"}
    for r in rows:
        if r.get("t") == "close":
            opened.pop((r["ticker"], r["rule"]), None)
    return opened


# ── network (injectable) ──────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None) -> dict:
    import requests
    r = requests.get(url, params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_open(series: str, get=_get, cap: int = 1000) -> list[dict]:
    out, cursor = [], None
    while len(out) < cap:
        params = {"series_ticker": series, "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        d = get(f"{KALSHI}/markets", params)
        ms = d.get("markets", []) or []
        out.extend(ms)
        cursor = d.get("cursor")
        if not cursor or not ms:
            break
        time.sleep(SLEEP_S)
    return out[:cap]


def fetch_market(ticker: str, get=_get) -> dict:
    d = get(f"{KALSHI}/markets/{ticker}")
    return d.get("market", d) or {}


def _default_depth_fn():
    try:
        import paper_trader
        return paper_trader._depth_fetcher()
    except Exception:  # noqa: BLE001
        return None


def _default_regime():
    try:
        import paper_trader
        return paper_trader._regime_now()
    except Exception:  # noqa: BLE001
        return {"regime": "unknown", "mult": 0.5}


# ── one cycle ─────────────────────────────────────────────────────────────────

def run_cycle(get=_get, now: datetime | None = None, rules_path: Path = RULES,
              ledger: Path = LEDGER, depth_fn=None, regime_fn=None,
              heartbeat: Path | None = HEARTBEAT) -> dict:
    now = now or _now()
    rules = active_rules(rules_path)
    rows = _load_jsonl(ledger)
    live = open_positions(rows)
    used = {(r.get("event"), r.get("rule")) for r in rows if r.get("t") == "open"}
    per_rule_open: dict[str, int] = {}
    for (_, rule) in live:
        per_rule_open[rule] = per_rule_open.get(rule, 0) + 1
    depth_fn = _default_depth_fn() if depth_fn is None else depth_fn
    regime = (regime_fn or _default_regime)()
    new, skipped, ungated, errors = [], {}, 0, 0

    by_series: dict[str, list] = {}
    for r in rules.values():
        for s_ in series_of(r):
            by_series.setdefault(s_, []).append(r)
    named = {n: _ts(r["named_at"]) for n, r in rules.items()}
    for series, rs in sorted(by_series.items()):
        try:
            markets = fetch_open(series, get=get)
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"[warn] {series}: {type(e).__name__}: {str(e)[:80]}")
            continue
        for m in markets:
            tk = m.get("ticker")
            if not tk:
                continue
            ev = event_of(m)
            for cand in candidates(m, now.timestamp(), rs):
                key = (ev, cand["rule"])
                if key in used:
                    continue                          # one bet per event per rule
                if now.timestamp() < (named.get(cand["rule"]) or 0):
                    continue
                if per_rule_open.get(cand["rule"], 0) >= MAX_OPEN_PER_RULE:
                    skipped["open_cap"] = skipped.get("open_cap", 0) + 1
                    continue
                n = contracts_of(rules[cand["rule"]])
                # liftable_depth counts resting size up to one tick PAST the price (a
                # tolerance for quote-vs-book skew). Harmless for a 1-lot - a live quote
                # always has 1 at the ask - but a sized order booked at the ask must find
                # all n AT the ask: probe one tick inside it. 1-lots keep the old probe.
                probe = cand["price"] if n == CONTRACTS else round(cand["price"] - 0.01, 4)
                depth = depth_fn(tk, cand["side"], probe) if depth_fn else None
                if depth is not None and depth < n:
                    skipped["no_depth"] = skipped.get("no_depth", 0) + 1
                    continue
                if depth is None:
                    ungated += 1
                entry = dict(cand, t="open", ts=_iso(now), ticker=tk, event=ev, series=series,
                             contracts=n, fee=kalshi_fee(cand["price"], n),
                             depth=depth, close_time=m.get("close_time"),
                             regime=regime["regime"], regime_mult=regime["mult"], v=VERSION)
                new.append(entry)
                used.add(key)
                per_rule_open[cand["rule"]] = per_rule_open.get(cand["rule"], 0) + 1
        time.sleep(SLEEP_S if get is _get else 0)

    closes = []
    for (tk, rule), pos in list(live.items()) + [((e["ticker"], e["rule"]), e) for e in new]:
        ct = _ts(pos.get("close_time"))
        if ct is not None and ct > now.timestamp():
            continue                                   # not closed yet - don't ask
        try:
            m = fetch_market(tk, get=get)
        except Exception:  # noqa: BLE001
            continue
        if m.get("result") in ("yes", "no"):
            closes.append(settle(pos, m["result"]))
    _append(ledger, new + closes)
    summary = {"ts": _iso(now), "rules": len(rules), "series": len(by_series),
               "opened": len(new), "closed": len(closes), "skipped": skipped,
               "ungated": ungated, "errors": errors, "regime": regime["regime"],
               "open_now": len(open_positions(rows + new + closes))}
    if heartbeat is not None:
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        heartbeat.write_text(json.dumps(summary), encoding="utf-8")
    return summary


# ── report ────────────────────────────────────────────────────────────────────

def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


PB_EXACT_STEPS = 3_000_000    # the exact DP's work budget, n * min(k, n-k): ~0.2 s


def _pb_exact(n: int, k: int) -> bool:
    """Whether the exact DP runs for P(K <= k) over n trades. It costs n * min(k, n-k)
    steps: always up to 200 of the rarer outcome, and beyond that within PB_EXACT_STEPS -
    favorite and longshot rules (a skewed K, where approximations go wrong) to thousands
    of events. Past it, in practice mid-price rules with thousands of events (nearly
    symmetric), the skewness-corrected normal is used and a refresh stays fast."""
    r = min(k, n - k)
    return r <= 200 or n * r <= PB_EXACT_STEPS


def _pb_head(qs: list[float], m: int) -> list[float]:
    """[P(K=0), ..., P(K=m)] exactly, by dynamic programming truncated at m."""
    dp = [1.0] + [0.0] * m
    for q in qs:
        r = 1.0 - q
        dp = [dp[0] * r] + [a * r + b * q for a, b in zip(dp[1:], dp)]
    return dp


def _pb_skew_cdf(qs: list[float], k: int) -> float:
    """P(K <= k) by the normal limit with a continuity correction AND a skewness
    correction (the first Edgeworth term). The plain normal is 15-45% off at p ~ 1e-3
    for favorites and longshots, whose K is skewed (anti-conservative for longshots);
    corrected, the error where _pb_exact hands over is under 1% at p >= 2e-3 and a few
    percent at 5e-4."""
    mu = sum(qs)
    var = sum(q * (1.0 - q) for q in qs)
    if var <= 0:
        return 1.0 if k + 0.5 >= mu else 0.0
    sd = math.sqrt(var)
    g = sum(q * (1.0 - q) * (1.0 - 2.0 * q) for q in qs) / sd ** 3
    x = (k + 0.5 - mu) / sd
    phi = math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
    return min(1.0, max(0.0, _norm_cdf(x) + g * (1.0 - x * x) * phi / 6.0))


def _pb_cdf(qs: list[float], k: int) -> float:
    """P(K <= k), K = the number of successes among independent Bernoulli(q_i): the
    Poisson-binomial CDF, exact by dynamic programming (truncated at k, or run from the
    other side when that is shorter) wherever _pb_exact allows; the skewness-corrected
    normal past it."""
    n = len(qs)
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if not _pb_exact(n, k):
        return _pb_skew_cdf(qs, k)
    if k > n - k:                       # P(K <= k) = 1 - P(n-K <= n-k-1)
        return min(1.0, max(0.0, 1.0 - _pb_cdf([1.0 - q for q in qs], n - k - 1)))
    return min(1.0, max(0.0, sum(_pb_head(qs, k))))


def _pb_tails(qs: list[float], k: int) -> tuple[float, float]:
    """(P(K <= k), P(K >= k)) for 0 <= k <= n: forward_stats' p and p_neg, from ONE
    exact pass (half the cost of two _pb_cdf calls - a refresh scores every rule)."""
    n = len(qs)
    if not _pb_exact(n, k):
        return _pb_skew_cdf(qs, k), 1.0 - _pb_skew_cdf(qs, k - 1)
    flip = k > n - k                    # run from the shorter side: W = n - K
    head = _pb_head([1.0 - q for q in qs] if flip else qs, n - k if flip else k)
    inner = sum(head[:-1])              # P(K <= k-1), or P(W <= n-k-1) = P(K >= k+1)
    total = inner + head[-1]            # P(K <= k),   or P(W <= n-k)   = P(K >= k)
    lo, hi = (1.0 - inner, total) if flip else (total, 1.0 - inner)
    return min(1.0, max(0.0, lo)), min(1.0, max(0.0, hi))


def _house_shaped(t: dict) -> bool:
    """A settled hold-to-expiry trade: a win paid 1-ask-fee per contract, a loss cost
    ask+fee. Anything else (a test fixture, an early exit) gets the normal test."""
    want = (1.0 - t["ask"] - t["fee_pc"]) if t["won"] else (-t["ask"] - t["fee_pc"])
    return abs(t["pnl_pc"] - want) < 0.005


def forward_stats(trades: list[dict]) -> dict:
    """The one scorer for every forward ledger - this report and the command center.

    trades: dicts with event, won, ask (price per contract) and pnl (dollars at the real
    size); optional pnl_pc / fee_pc (per contract - default: a 1-lot).

    Judged per EVENT and per CONTRACT, so order size never changes a verdict. Under the
    house rule - one bet per (event, rule), held to settlement - every outcome is a win or
    a loss that differ by exactly $1 per contract, so a rule's total is decided by HOW
    MANY trades lost. With no edge, a trade at ask A and fee f loses with probability
    1-(A+f), so both p-values are exact Poisson-binomial tails (_pb_tails; past the exact
    DP's work budget - mid-price rules with thousands of events - a skewness-corrected
    normal, within 1% of exact at p >= 0.002):
        p      = P(this few losses or fewer | no edge)   evidence the rule makes money
        p_neg  = P(this many losses or more | no edge)   evidence it loses money
    A normal approximation goes badly wrong on 97-99c favorites, where losses are rare and
    large: 100 straight wins at 98c (10-lot) has p = 0.982**100 = 0.16 - ordinary luck.
    Any other shape falls back to sleeve_scan.p_value, the null-variance-floored test."""
    from forecast_replay import mean_ci
    rows = []
    for t in trades:
        ask = float(t["ask"])
        fee_pc = t.get("fee_pc")
        rows.append(dict(t, ask=ask, pnl_pc=float(t.get("pnl_pc", t["pnl"])),
                         fee_pc=float(kalshi_fee(ask, 1) if fee_pc is None else fee_pc)))
    ev: dict[str, list[dict]] = {}
    for t in rows:
        ev.setdefault(str(t["event"]), []).append(t)
    per = [sum(x["pnl_pc"] for x in v) / len(v) for v in ev.values()]
    m, lo, hi = mean_ci(per)
    n = len(per)
    if n == 0:
        p = p_neg = 1.0
    elif all(len(v) == 1 for v in ev.values()) and all(_house_shaped(t) for t in rows):
        # [0, 1], not [0.001, 0.999]: a 1-lot at 99c pays a 1c fee, so with no edge it
        # cannot lose (q = 0) - a 0.001 floor shrank p by 0.999 per such win, for nothing
        qs = [1.0 - min(1.0, max(0.0, t["ask"] + t["fee_pc"])) for t in rows]
        k = sum(1 for t in rows if not t["won"])
        p, p_neg = _pb_tails(qs, k)
    elif n >= 2:
        from sleeve_scan import p_value
        se = (hi - m) / 1.96
        p, p_neg = p_value(m, se, rows), p_value(-m, se, rows)
    else:
        p = p_neg = 1.0
    return {"n": n, "trades": len(rows), "mean": m, "lo": lo, "hi": hi, "p": p,
            "p_neg": p_neg,
            "wr": (sum(t["won"] for t in rows) / len(rows)) if rows else None,
            "net": sum(t["pnl"] for t in rows)}


def survivors(stats: list[dict]) -> set[int]:
    """Indices whose p survives Benjamini-Hochberg across every testable rule at once."""
    from sleeve_scan import bh_select
    testable = [i for i, s_ in enumerate(stats) if s_["n"] >= MIN_TESTABLE_EVENTS]
    if not testable:
        return set()
    return {testable[j] for j in bh_select([stats[i]["p"] for i in testable])}


def classify(st: dict, survives: bool) -> str:
    """The verdict category - one standard for this report and the command center."""
    if st["n"] < MIN_TESTABLE_EVENTS:
        return "collecting"
    if survives and st["mean"] > 0:
        return "edge" if st["n"] >= MIN_EVENTS_VERDICT else "promising"
    if st["p_neg"] < 0.025:
        return "negative"
    return "positive_ns" if st["mean"] > 0 else "negative_ns"


def duplicate_closes(rows: list[dict]) -> dict[str, int]:
    """rule -> close rows beyond the first for one (ticker, rule). Two traders writing one
    ledger leave them (the single-instance lock in main() now prevents that); the ledger
    is append-only, so they stay - trades_of scores each bet once and `report` says how
    many rows that left out."""
    seen: set[tuple] = set()
    out: dict[str, int] = {}
    for r in rows:
        if r.get("t") != "close":
            continue
        key = (r.get("ticker"), r.get("rule"))
        if key in seen:
            out[str(key[1])] = out.get(str(key[1]), 0) + 1
        seen.add(key)
    return out


def trades_of(rows: list[dict], rules: dict[str, dict]) -> dict[str, list[dict]]:
    """rule -> its closed trades opened AFTER its named_at, in forward_stats' shape.

    ONE trade per (ticker, rule): a second close row for the same bet is ignored. One
    duplicated close used to count that trade twice AND give its event two trades, which
    moved the whole rule off the exact test onto the approximate one (120 straight wins
    at 97-99c read p = 0.025 instead of 0.113)."""
    opens = {(r.get("ticker"), r.get("rule")): r for r in rows if r.get("t") == "open"}
    out: dict[str, list[dict]] = {name: [] for name in rules}
    settled: set[tuple] = set()
    for r in rows:
        name = r.get("rule")
        if r.get("t") != "close" or name not in rules:
            continue
        if (r.get("ticker"), name) in settled:
            continue                                  # the same bet, closed twice: one trade
        settled.add((r.get("ticker"), name))
        o = opens.get((r.get("ticker"), name)) or {}
        if (_ts(o.get("ts")) or 0) < (_ts(rules[name].get("named_at")) or 0):
            continue                                  # only trades after pre-registration
        n = contracts_of(o)
        ask = float(r.get("price") or o.get("price") or 0.5)
        fee = o.get("fee")
        fee = float(kalshi_fee(ask, n) if fee is None else fee)
        out[name].append({"rule": name, "event": r.get("event") or o.get("event"),
                          "series": r.get("series") or o.get("series"),
                          "won": bool(r.get("won")), "ask": ask, "pnl": float(r["pnl"]),
                          "pnl_pc": float(r["pnl"]) / n, "fee_pc": fee / n,
                          "depth": o.get("depth"), "ts": _ts(r.get("ts")) or 0.0})
    return out


def retired_stats(rows: list[dict], path: Path = RULES) -> list[dict]:
    """forward_stats of every RETIRED rule's record after its named_at. Never shown, but
    kept in the false-discovery family: retiring is decided after seeing results, and
    dropping those tests would loosen the bar for every rule still running."""
    live = active_rules(path)
    gone = {n: r for n, r in registered_rules(path).items() if n not in live}
    return [forward_stats(ts) for _, ts in sorted(trades_of(rows, gone).items())]


def family_extra(rows: list[dict], path: Path = RULES) -> list[dict]:
    """The rest of the ONE forward family the command center corrects over: the retired
    sleeve rules' records and the crypto rules' fill-checked records (built by the command
    center's own code - a plain module; Flask loads only when its server starts)."""
    from lib.command_center import crypto_family
    return retired_stats(rows, path) + crypto_family()


def evaluate(rows: list[dict], rules: dict[str, dict], extra_stats=()) -> list[dict]:
    """Per rule: closed trades after named_at, per EVENT and per CONTRACT, exact p-values
    (forward_stats), BH across every rule with enough events - these rules plus
    extra_stats, the forward_stats of every other member of the forward family (retired
    and crypto rules: family_extra). Only `rules` are classified and returned."""
    by_rule = trades_of(rows, rules)
    names = sorted(rules)
    stats = [forward_stats(by_rule[nm]) for nm in names]
    keep = survivors(stats + list(extra_stats))     # rules come first: index i is names[i]
    out = []
    for i, name in enumerate(names):
        st, rule, ts = stats[i], rules[name], by_rule[name]
        cat = classify(st, i in keep)
        per_series: dict[str, list] = {}
        for t in ts:
            b = per_series.setdefault(str(t["series"]), [0, 0.0])
            b[0] += 1
            b[1] += t["pnl"]
        out.append({"rule": name, "series": series_label(rule), "source": rule.get("source"),
                    "contracts": contracts_of(rule), "events": st["n"], "trades": st["trades"],
                    "wr": st["wr"], "net": round(st["net"], 2), "mean": st["mean"],
                    "lo": st["lo"], "hi": st["hi"], "p": st["p"], "p_neg": st["p_neg"],
                    "category": cat,
                    "verdict": VERDICT_TEXT[cat].format(n=st["n"], need=MIN_EVENTS_VERDICT),
                    "by_series": {k: (v[0], round(v[1], 2)) for k, v in sorted(per_series.items())},
                    "gated_share": (sum(1 for t in ts if t["depth"] is not None) / len(ts))
                    if ts else None})
    return out


def cmd_report() -> int:
    rules = active_rules(RULES)
    rows = _load_jsonl(LEDGER)
    print("=" * 78)
    print("SLEEVE PAPER - forward-only, out of sample, every sleeve. NO REAL ORDERS.")
    print("=" * 78)
    if not rules:
        print("no rules registered. Run: py scripts/sleeve_scan.py discover, then")
        print("py scripts/sleeve_paper.py register-probes")
        return 0
    extra = family_extra(rows, RULES)
    res = evaluate(rows, rules, extra)
    hb = json.loads(HEARTBEAT.read_text(encoding="utf-8")) if HEARTBEAT.exists() else {}
    print(f"{len(rules)} active rules on "
          f"{len({x for r in rules.values() for x in series_of(r)})} "
          f"sleeves | last cycle {str(hb.get('ts', 'never'))[:19]} | open now "
          f"{hb.get('open_now', 0)} | regime {hb.get('regime', '?')}")
    print()
    print(f"  {'rule':30} {'events':>6} {'WR':>6} {'net$':>9} {'c/ct':>6} "
          f"{'95% CI (c/ct)':>15} {'p':>6}  verdict")
    for r in sorted(res, key=lambda x: (-x["events"], x["rule"])):
        wr = f"{r['wr']:.1%}" if r["wr"] is not None else "-"
        ci = f"[{100 * r['lo']:+.1f},{100 * r['hi']:+.1f}]" if r["events"] >= 2 else "-"
        pv = f"{r['p']:.3f}" if r["events"] else "-"
        print(f"  {r['rule'][:30]:30} {r['events']:>6} {wr:>6} {r['net']:>+9.2f} "
              f"{100 * r['mean']:>+6.1f} {ci:>15} {pv:>6}  {r['verdict']}")
        if len(r["by_series"]) > 1:
            print("      by series (descriptive, not tested): " + "  ".join(
                f"{k} {n}ev {v:+.2f}" for k, (n, v) in r["by_series"].items()))
    print()
    dups = duplicate_closes(rows)
    if dups:
        print(f"LEDGER: {sum(dups.values())} duplicate close row(s) are NOT counted above ("
              + ", ".join(f"{k} {v}" for k, v in sorted(dups.items())) + "). Two traders once")
        print("wrote this ledger at the same time; every bet is scored once, on its first close")
        print("row (`py scripts/diagnostics.py` reports them as structure defects).")
        print()
    pooled = sorted(n for n, r in rules.items() if len(series_of(r)) > 1)
    if pooled:
        more = f" +{len(pooled) - 3} more" if len(pooled) > 3 else ""
        print(f"POOLED SERIES ARE ASSUMED INDEPENDENT ({', '.join(pooled[:3])}{more}): a "
              f"pooled rule counts")
        print("each series' event as its own bet, so its p holds only if those series settle")
        print("independently. Series on one underlying (two S&P 500 series, a BTC range and a")
        print("BTC above/below series) share outcomes: one bust day counts twice, p too small.")
        print()
    testable = (sum(r["events"] >= MIN_TESTABLE_EVENTS for r in res)
                + sum(s_["n"] >= MIN_TESTABLE_EVENTS for s_ in extra))
    print("c/ct = cents per contract per event, after fees. p = the EXACT chance of a")
    print("record this good with no edge (a favorite that hasn't lost yet is not proof).")
    print("Benjamini-Hochberg runs across ALL forward rules, the command center's family:")
    print("these, the retired rules' records and the crypto rules' fill-checked records -")
    print(f"{testable} rules with {MIN_TESTABLE_EVENTS}+ events now. Adding sleeves never "
          f"lowers the bar, and retiring")
    print("a rule never raises another's verdict. EDGE needs 100+ events AND survival of")
    print("that correction. Live money stays off until a rule earns EDGE here.")
    print("=" * 78)
    return 0


def cmd_rules() -> int:
    rules = active_rules(RULES)
    retired = all_registered(RULES) - set(rules)
    print(f"{len(rules)} active, {len(retired)} retired")
    for n, r in sorted(rules.items()):
        win = (f" {r['min_h']:g}-{r['max_h']:g}h" if r.get("max_h", 1e9) < 1e9 or
               r.get("min_h", 0.0) > 0 else "")
        print(f"  {n:30} {r['side']:8} {r['lo']:.2f}-{r['hi']:.3g} {r['band']:5}{win} "
              f"x{contracts_of(r)} on {series_label(r)}  named {r['named_at'][:16]}"
              f"  [{r.get('source')}]")
    return 0


# ── selftest ──────────────────────────────────────────────────────────────────

def _selftest() -> int:
    import tempfile
    from datetime import timedelta
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)

    # the unit comes from the field name: 1 CENT is never a dollar
    assert price({"yes_bid": 1}, "yes_bid") == 0.01
    assert price({"yes_bid_dollars": "0.0100", "yes_bid": 99}, "yes_bid") == 0.01
    assert price({"yes_bid": None}, "yes_bid") is None

    def mk(tk, ev, ya_c, yb_c, opened_h=-10, closes_h=10, result=""):
        return {"ticker": tk, "event_ticker": ev, "yes_ask": ya_c, "yes_bid": yb_c,
                "open_time": _iso(now + timedelta(hours=opened_h)),
                "close_time": _iso(now + timedelta(hours=closes_h)), "result": result}

    with tempfile.TemporaryDirectory() as td:
        rp, lp = Path(td) / "rules.jsonl", Path(td) / "ledger.jsonl"
        added = register(probe_rules(["KXINX", "KXBTC15M"]), rp, now - timedelta(days=1))
        assert added == ["FLB_FAV_KXINX", "FLB_LONG_KXINX"]          # crypto-15 excluded
        assert register(probe_rules(["KXINX"]), rp, now) == []         # never re-registered
        rules = active_rules(rp)
        assert set(rules) == {"FLB_FAV_KXINX", "FLB_LONG_KXINX"}

        # mid band (life fraction 0.5): favorite at 88c fires FAV, 12c longshot fires LONG
        m1 = mk("KXINX-E1-A", "KXINX-E1", 88, 86)
        c1 = candidates(m1, now.timestamp(), list(rules.values()))
        assert {c["rule"] for c in c1} == {"FLB_FAV_KXINX", "FLB_LONG_KXINX"}
        fav = next(c for c in c1 if c["rule"] == "FLB_FAV_KXINX")
        assert fav["side"] == "yes" and fav["price"] == 0.88
        assert candidates(mk("X", "E", 88, 86, opened_h=-1, closes_h=19),
                          now.timestamp(), list(rules.values())) == []   # early band
        assert candidates(mk("X", "E", 99, 1), now.timestamp(),
                          list(rules.values())) == []                      # unformed shell

        markets = [m1, mk("KXINX-E1-B", "KXINX-E1", 90, 88),   # same event: no 2nd bet
                   mk("KXINX-E2-A", "KXINX-E2", 85, 83)]

        def fake_get(url, params=None):
            if url.endswith("/markets"):
                return {"markets": markets, "cursor": None}
            tk = url.rsplit("/", 1)[-1]
            return {"market": {"ticker": tk, "result": "yes"}}

        depth = {"KXINX-E2-A": 0}                    # E2's book is empty behind the quote
        s = run_cycle(fake_get, now, rp, lp, depth_fn=lambda t, sd, p: depth.get(t, 50),
                      regime_fn=lambda: {"regime": "cautious", "mult": 0.5}, heartbeat=None)
        opens = [r for r in _load_jsonl(lp) if r.get("t") == "open"]
        assert s["opened"] == 2 and len(opens) == 2, s          # E1 FAV + E1 LONG only
        assert {(o["event"], o["rule"]) for o in opens} == {
            ("KXINX-E1", "FLB_FAV_KXINX"), ("KXINX-E1", "FLB_LONG_KXINX")}
        assert s["skipped"].get("no_depth") == 2                 # E2 refused, both rules
        assert all(o["regime"] == "cautious" and o["contracts"] == 10 for o in opens)
        assert all(o["fee"] == kalshi_fee(o["price"], 10) for o in opens)  # ONE order's fee
        s2 = run_cycle(fake_get, now, rp, lp, depth_fn=lambda t, sd, p: 50,
                       regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
        assert {o["event"] for o in _load_jsonl(lp) if o.get("t") == "open"} == {
            "KXINX-E1", "KXINX-E2"} and s2["opened"] == 2        # E1 never re-entered

        # settlement after close, 10-lots: YES won -> FAV (yes @.88, fee .08) +1.12,
        # LONG (no @.14, fee .09) -1.49
        later = now + timedelta(hours=11)
        s3 = run_cycle(fake_get, later, rp, lp, depth_fn=lambda t, sd, p: 50,
                       regime_fn=lambda: {"regime": "good", "mult": 1.0}, heartbeat=None)
        closes = {(c["ticker"], c["rule"]): c for c in _load_jsonl(lp) if c.get("t") == "close"}
        assert s3["closed"] == 4, s3
        assert abs(closes[("KXINX-E1-A", "FLB_FAV_KXINX")]["pnl"] - (1.20 - 0.08)) < 1e-9
        long_row = closes[("KXINX-E1-A", "FLB_LONG_KXINX")]
        assert long_row["side"] == "no" and abs(long_row["pnl"] - (-1.40 - 0.09)) < 1e-9
        assert not open_positions(_load_jsonl(lp))

        # retire: append-only, and a retired rule stops trading
        _append(rp, [{"t": "retire", "name": "FLB_LONG_KXINX", "ts": _iso(now)}])
        assert set(active_rules(rp)) == {"FLB_FAV_KXINX"}

        # evaluation: per event; too few events -> collecting, never a verdict
        res = {r["rule"]: r for r in evaluate(_load_jsonl(lp), active_rules(rp))}
        assert res["FLB_FAV_KXINX"]["events"] == 2
        assert res["FLB_FAV_KXINX"]["verdict"].startswith("collecting")
        # per CONTRACT: E1 won 1.12 on a 10-lot, E2 (yes @.85, fee .09) won 1.41
        assert abs(res["FLB_FAV_KXINX"]["mean"] - (0.112 + 0.141) / 2) < 1e-9

    # exact p: 100 straight wins on 98c 10-lots is ordinary luck under no edge
    fee10 = kalshi_fee(0.98, 10)
    wins = [{"event": f"E{i}", "won": True, "ask": 0.98, "fee_pc": fee10 / 10,
             "pnl": 10 * 0.02 - fee10, "pnl_pc": (10 * 0.02 - fee10) / 10} for i in range(100)]
    st = forward_stats(wins)
    assert abs(st["p"] - (0.98 + fee10 / 10) ** 100) < 1e-9 and st["p"] > 0.15, st
    assert classify(st, st["p"] < 0.05) == "positive_ns"

    # nominations: clean only, phantom-risk never registered
    noms = nomination_rules({"nominated": [
        {"series": "KXA", "band": "mid", "side": "no", "bucket": "80-90c",
         "verdict": "NOMINATED", "p_out": 1e-5},
        {"series": "KXB", "band": "late", "side": "yes", "bucket": "05-10c",
         "verdict": "NOMINATED - phantom risk", "p_out": 1e-6}]})
    assert [n["name"] for n in noms] == ["NOM_KXA_mid_no_80-90c"]
    assert noms[0]["lo"] == 0.80 and noms[0]["hi"] == 0.90
    print("selftest OK")
    return 0


def _lock_dir() -> Path | None:
    """Where the single-instance lock lives. The lock guards a LEDGER: the shared lock
    folder for the real one (None = lib.single_instance's default, or POLYBOT_LOCK_DIR),
    a folder next to the ledger when this trader was pointed somewhere else - a scratch
    run or a test must neither wait for the live trader nor take its lock."""
    if os.environ.get("POLYBOT_LOCK_DIR") or LEDGER.parent == ROOT / "data":
        return None
    return LEDGER.parent / "locks"


def _only_trader() -> bool:
    """Take the single-instance lock for the life of this process. False = another
    sleeve trader is running on this ledger: stand down. run_cycle reads the ledger at
    the start and appends at the end, so a second process inside that window writes
    every entry and settlement of the cycle a second time - and the ledger is
    append-only. Called from main() only, for `run` and `cycle`: tests and the selftest
    call run_cycle directly and must never be blocked by the live task. If locking
    itself is broken, hold() says so and the trader runs unlocked (fail open)."""
    from lib.single_instance import AlreadyRunning, hold
    try:
        hold("sleeve_paper", _lock_dir())
    except AlreadyRunning as e:
        print(f"not starting: {e}. One sleeve trader per ledger - a second one would "
              f"write every entry and settlement twice.")
        return False
    return True


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "selftest":
        return _selftest()
    if cmd == "register-probes":
        # --series A,B,C: the probe pair on sleeves YOU chose (pick ones that settle often
        # and are still listing events). Otherwise the catalog's top N by recent settled
        # events - which can be one-off elections that will never list another event.
        if "--series" in args and args.index("--series") + 1 < len(args):
            cat = list(dict.fromkeys(x.strip() for x in
                                     args[args.index("--series") + 1].split(",") if x.strip()))
        else:
            if not CATALOG.exists():
                print("run `py scripts/sleeve_scan.py discover` first (or pass --series A,B)")
                return 1
            top = int(args[args.index("--top") + 1]) if "--top" in args else 20
            cat = [r["series"] for r in json.loads(CATALOG.read_text(encoding="utf-8"))
                   if r["series"] not in EXCLUDED_SERIES][:top]
        skipped = [x for x in cat if x in EXCLUDED_SERIES]
        cat = [x for x in cat if x not in EXCLUDED_SERIES]
        added = register(probe_rules(cat), RULES)
        print(f"registered {len(added)} probe rules on {len(cat)} sleeves (named now)"
              + (f"; skipped {', '.join(skipped)} (traded by paper_trader)" if skipped else ""))
        return 0
    if cmd == "register-nominations":
        if not SCREEN.exists():
            print("run `py scripts/sleeve_scan.py screen` first")
            return 1
        added = register(nomination_rules(json.loads(SCREEN.read_text(encoding="utf-8"))), RULES)
        print(f"registered {len(added)} nomination rule(s): {', '.join(added) or '-'}")
        return 0
    if cmd == "register-rule":
        # A named outside hypothesis (e.g. a friend's method), pre-registered NOW as ONE
        # rule over the series given - or every catalog series starting with --series-prefix.
        def opt(name, default=None):
            return args[args.index(name) + 1] if name in args else default
        prefix, side = opt("--name"), opt("--side", "favorite")
        if not prefix or side not in ("favorite", "longshot", "yes", "no"):
            print("usage: register-rule --name NAME (--series A,B | --series-prefix KXHIGH)"
                  " --side favorite|longshot|yes|no --lo 0.97 --hi 0.995 [--band any]"
                  " [--contracts 10] [--min-hours 6 --max-hours 12] [--thesis TEXT]")
            return 1
        if opt("--series"):
            series = [x.strip() for x in opt("--series").split(",") if x.strip()]
        elif opt("--series-prefix"):
            if not CATALOG.exists():
                print("--series-prefix reads the series catalog, which does not exist yet.")
                print("Run `py scripts/sleeve_scan.py discover` first, or name the series:")
                print("  --series KXHIGHNY,KXHIGHCHI,KXHIGHMIA,...")
                return 1
            series = [r["series"] for r in json.loads(CATALOG.read_text(encoding="utf-8"))
                      if r["series"].startswith(opt("--series-prefix"))]
        else:
            print("give --series A,B (or --series-prefix after sleeve_scan.py discover)")
            return 1
        series = [x for x in dict.fromkeys(series) if x not in EXCLUDED_SERIES]
        if not series:
            print("no series left to trade (the crypto 15-min series belong to paper_trader)")
            return 1
        rule = {"name": prefix, "series": series, "band": opt("--band", "any"),
                "side": side, "lo": float(opt("--lo", 0.97)), "hi": float(opt("--hi", 0.995)),
                "contracts": int(opt("--contracts", NEW_RULE_CONTRACTS)),
                "min_h": float(opt("--min-hours", 0.0)), "max_h": float(opt("--max-hours", 1e9)),
                "source": "named", "thesis": opt("--thesis", f"{prefix}: outside hypothesis")}
        if not register([rule], RULES):
            print(f"{prefix} is already registered - a rule is never redefined. "
                  f"Pick a new name (`py scripts/sleeve_paper.py rules` lists them).")
            return 1
        win = (f", {rule['min_h']:g}-{rule['max_h']:g}h before close"
               if rule["max_h"] < 1e9 or rule["min_h"] > 0 else "")
        print(f"registered {prefix} (named now): ONE rule, ONE test - {side} "
              f"{rule['lo']:g}-{rule['hi']:g}, band {rule['band']}{win}, "
              f"{rule['contracts']} contracts/order, on {len(series)} series: "
              f"{', '.join(series)}")
        if len(series) > 1:
            print("NOTE: each series' event is its own bet - the exact p ASSUMES these series")
            print("settle independently. Never pool series on one underlying (e.g. two S&P 500")
            print("series): their shared outcomes would count twice.")
        return 0
    if cmd == "retire" and len(args) > 1:
        live = active_rules(RULES)
        if args[1] == "--prefix" and len(args) > 2:
            names = sorted(n for n in live if n.startswith(args[2]))
        else:
            names = args[1:]
        missing = [n for n in names if n not in live]
        if not names or missing:
            print(f"no active rule named {', '.join(missing) or args[-1]} - nothing retired "
                  f"(`py scripts/sleeve_paper.py rules` lists them)")
            return 1
        _append(RULES, [{"t": "retire", "name": n, "ts": _iso(_now())} for n in names])
        print(f"retired {len(names)}: {', '.join(names)} (their records stay in the ledger)")
        return 0
    if cmd == "rules":
        return cmd_rules()
    if cmd == "report":
        return cmd_report()
    if cmd in ("cycle", "run") and not _only_trader():
        return 1
    if cmd == "cycle":
        print(run_cycle())
        return 0
    if cmd == "run":
        print(f"sleeve paper - every {CYCLE_S}s -> {LEDGER}. FORWARD-ONLY, no real orders.")
        while True:
            t0 = time.time()
            try:
                s = run_cycle()
                sk = " ".join(f"{k}={v}" for k, v in s["skipped"].items())
                ug = f" UNGATED={s['ungated']}" if s["ungated"] else ""
                print(f"[{s['ts'][:19]}] rules={s['rules']} opened={s['opened']} "
                      f"closed={s['closed']} open_now={s['open_now']} "
                      f"regime={s['regime']} {sk}{ug}")
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[warn] cycle failed: {type(e).__name__}: {e}")
            time.sleep(max(1.0, CYCLE_S - (time.time() - t0)))
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
