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

HOW IT RUNS - `run` loops every 120 s (the PolybotSleeves task), `cycle` is one pass.
Both hold a single-instance lock - one trader per ledger - and append what they print to
logs/sleeve_paper.log. Every cycle writes data/sleeve_paper_heartbeat.json, ALSO a cycle
that raises (`cycle_error` then holds the exception), with the failed list calls and
settlement polls counted and their streaks: alive must never read as trading.

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
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
DATA = Path(os.environ.get("SLEEVE_PAPER_DIR") or (ROOT / "data"))
RULES = DATA / "sleeve_rules.jsonl"
LEDGER = DATA / "paper_sleeves.jsonl"
HEARTBEAT = DATA / "sleeve_paper_heartbeat.json"
# What `run` and `cycle` print is also appended here (see _log_path): the scheduled task
# runs in a hidden window with nothing redirected, so until this file existed a warning
# was gone the moment it was printed.
LOG = Path(os.environ.get("SLEEVE_PAPER_LOG") or (ROOT / "logs" / "sleeve_paper.log"))
CATALOG = Path(os.environ.get("SLEEVE_DIR") or (ROOT / "data" / "sleeves")) / "catalog.json"
SCREEN = CATALOG.parent / "screen.json"

VERSION = 2                # 2: sized orders need all n contracts AT the ask (2026-11-16)
CONTRACTS = 1             # the size of rules (and positions) from before sizes were stored
NEW_RULE_CONTRACTS = 10   # stamped on every rule registered from now on - see contracts_of
CYCLE_S = 120
SLEEP_S = 0.35
MAX_SPREAD = 0.15
MAX_OPEN_PER_RULE = 50
OVERDUE_H = 24.0           # an open position this long past its market's close is "overdue"
OVERDUE_POLL_S = 3600      # ... and is asked about once an hour instead of every cycle,
OVERDUE_ROOM = MAX_OPEN_PER_RULE // 2   # while its rule uses fewer slots than this
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


class RegistryError(ValueError):
    """Active rules whose registry row cannot be traded. Raised at the END of a cycle:
    every other rule has traded and settled, and the rows are in the ledger."""


def rule_problem(r: dict) -> str | None:
    """Why this registry row cannot be traded - None for a usable one. These are exactly
    the rows candidates() and run_cycle raise on: band, side, lo, hi or named_at missing,
    or a lo / hi / min_h / max_h that is not a number. (register-rule writes none of
    these; it takes a hand edit or another writer.)"""
    missing = [k for k in ("band", "side", "lo", "hi", "named_at") if k not in r]
    if missing:
        return "lacks " + ", ".join(missing)
    bad = []
    for key, default in (("lo", 0.0), ("hi", 0.0), ("min_h", 0.0), ("max_h", 1e9)):
        try:
            r.get(key, default) <= 0.5          # noqa: B015 - what candidates() compares
        except TypeError:
            bad.append(key)
    try:
        hash(r["side"])
    except TypeError:
        bad.append("side")
    try:
        series_of(r)
    except TypeError:
        bad.append("series")
    return ("has an unusable " + ", ".join(bad)) if bad else None


def unusable_rules(rules: dict[str, dict]) -> dict[str, str]:
    """name -> why, for every rule whose registry row cannot be traded."""
    out = {}
    for name, r in sorted(rules.items()):
        why = rule_problem(r)
        if why is not None:
            out[name] = why
    return out


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


def _key(*parts) -> tuple | None:
    """The parts as a dict key - None when one is missing or cannot be a key. A ledger
    row is whatever JSON was on the line: a row without a ticker or a rule is no
    position, and one malformed row must not stop every cycle with a KeyError."""
    if not all(parts):
        return None
    try:
        hash(parts)
    except TypeError:
        return None
    return parts


def open_positions(rows: list[dict]) -> dict[tuple, dict]:
    """(ticker, rule) -> the open row of every bet not settled yet. Rows that lack a
    ticker or a rule are skipped."""
    opened: dict[tuple, dict] = {}
    for r in rows:
        key = _key(r.get("ticker"), r.get("rule")) if r.get("t") == "open" else None
        if key is not None:
            opened[key] = r
    for r in rows:
        key = _key(r.get("ticker"), r.get("rule")) if r.get("t") == "close" else None
        if key is not None:
            opened.pop(key, None)
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

def _log_line(log: Path | None, msg: str) -> None:
    """Append one UTC-stamped line to the trader's log. Never raises: a locked or
    unwritable log file is no reason to stop trading."""
    if log is None:
        return
    try:
        from lib.jsonl_io import append_lines
        append_lines(log, [f"{_iso(_now())[:19]}Z {msg}"])
    except Exception:  # noqa: BLE001
        pass


def _say(msg: str, log: Path | None = None) -> None:
    """Print a line and, when a log is given, append it there too. The scheduled task's
    console is a hidden window nobody reads, so the file is the only lasting record of
    what the trader said. Neither half may ever stop a cycle."""
    try:
        print(msg, flush=True)
    except Exception:  # noqa: BLE001
        pass
    _log_line(log, msg)


def cycle_line(s: dict) -> str:
    """One cycle on one line. A healthy line reads as it always did, plus how long the
    cycle took; every failure count that is not zero is named, so six hours of failed
    requests no longer print the same line as six healthy hours."""
    parts = [f"[{s['ts'][:19]}]", f"rules={s['rules']}", f"opened={s['opened']}",
             f"closed={s['closed']}", f"open_now={s['open_now']}", f"regime={s['regime']}"]
    parts += [f"{k}={v}" for k, v in s["skipped"].items()]
    if s["ungated"]:
        parts.append(f"UNGATED={s['ungated']}")
    if s.get("errors"):
        parts.append(f"errors={s['errors']}/{s['series']}")
    if s.get("all_failed_streak"):
        parts.append(f"all_failed_streak={s['all_failed_streak']}")
    if s.get("poll_errors"):
        parts.append(f"poll_errors={s['poll_errors']}")
    if s.get("market_errors"):
        parts.append(f"market_errors={s['market_errors']}")
    if (s.get("overdue") or {}).get("n"):
        parts.append(f"overdue={s['overdue']['n']}")
    if s.get("took_s") is not None:
        parts.append(f"took={s['took_s']:.1f}s")
    return " ".join(parts)


def _write_heartbeat(path: Path, summary: dict, log: Path | None = None) -> None:
    """Write the heartbeat whole or not at all (a temp file, then os.replace), so a
    reader never finds half a file. Where the swap is refused - Windows, while a reader
    has the file open - write it in place as before. A heartbeat that cannot be written
    is a warning, never a failed cycle: the rows are already in the ledger."""
    data = json.dumps(summary)
    tmp = path.with_name(path.stem + ".tmp" + path.suffix)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, path)
        return
    except OSError:
        pass
    try:
        path.write_text(data, encoding="utf-8")
    except OSError as e:
        _say(f"[warn] heartbeat not written: {type(e).__name__}: {str(e)[:80]}", log)
    try:
        tmp.unlink()
    except OSError:
        pass


def _last_heartbeat(state: dict, heartbeat: Path | None) -> dict:
    """The previous heartbeat: this process's last one, else the file's."""
    last = state.get("last")
    if isinstance(last, dict):
        return last
    try:
        d = json.loads(heartbeat.read_text(encoding="utf-8")) if heartbeat else {}
    except (OSError, ValueError):
        d = {}
    return d if isinstance(d, dict) else {}


def _heartbeat_of(c: SimpleNamespace, now: datetime, state: dict, error: Exception | None,
                  took_s: float, heartbeat: Path | None) -> dict:
    """What this cycle did, as the heartbeat. Readers (the watchdog, the command center,
    `report`) index the first ten keys, so ALL of them are ALWAYS there with the meaning
    they always had - also after a cycle that raised: whatever that cycle did not get far
    enough to learn is carried over from the last heartbeat, never reported as zero
    ("0 rules" would send the owner off to register rules he already has)."""
    prev = _last_heartbeat(state, heartbeat) if error is not None else {}
    new, closes = (c.new, c.closes) if c.appended else ([], [])
    regime = c.regime.get("regime") if isinstance(c.regime, dict) else None
    open_now, overdue = None, prev.get("overdue")
    if c.rows is not None:
        try:
            still_open_now = open_positions(c.rows + new + closes)
            open_now, overdue = len(still_open_now), _overdue(still_open_now, now.timestamp())
        except Exception:  # noqa: BLE001 - the cycle already failed; report what we can
            pass
    # consecutive failed list calls, per series and for all of them at once. A cycle that
    # raised before it had asked every series says nothing about the ones it never asked.
    fails = state.setdefault("series_fail", {})
    for series, ok in c.listed.items():
        if ok:
            fails.pop(series, None)
        else:
            fails[series] = fails.get(series, 0) + 1
    if c.list_done:
        for series in [k for k in fails if k not in c.listed]:
            del fails[series]                           # no longer traded
    if any(c.listed.values()) or (c.list_done and not c.listed):
        state["all_failed"] = 0
    elif c.list_done:
        state["all_failed"] = state.get("all_failed", 0) + 1
    state["failed_cycles"] = state.get("failed_cycles", 0) + 1 if error is not None else 0
    summary = {
        "ts": _iso(now),
        "rules": c.rules if c.rules is not None else prev.get("rules", 0),
        "series": c.series if c.series is not None else prev.get("series", 0),
        "opened": len(new), "closed": len(closes), "skipped": c.skipped,
        "ungated": c.ungated, "errors": c.errors,
        "regime": regime if regime is not None else prev.get("regime", "unknown"),
        "open_now": open_now if open_now is not None else prev.get("open_now", 0),
        # ── added 2026-11: why a trader that is alive may not be trading ──
        "poll_errors": c.poll_errors,
        "market_errors": c.market_errors,
        "all_failed_streak": state.get("all_failed", 0),
        "series_fail_streak": dict(sorted(fails.items())),
        "cycle_error": None if error is None else f"{type(error).__name__}: {error}"[:300],
        "failed_cycles": state["failed_cycles"],
        "took_s": took_s,
        "bad_rules": c.bad_rules,
    }
    # absent = unknown: a cycle that died before it read the ledger, with no earlier
    # heartbeat to carry the figure over from
    if overdue is not None:
        summary["overdue"] = overdue
    return summary


_NO_ANSWER = object()          # a settlement poll that failed this cycle


def _overdue(positions: dict, now_ts: float) -> dict:
    """Open positions more than OVERDUE_H hours past their market's close with no yes/no
    result yet: how many, the ticker overdue the longest, and for how many hours. A
    market that never reports yes or no (voided, settled as something else, a ticker the
    endpoint stops serving) is never closed and keeps one of its rule's MAX_OPEN_PER_RULE
    slots for good - fifty of them and the rule stops entering. Nothing said so."""
    late = []
    for (tk, _), pos in positions.items():
        ct = _ts(pos.get("close_time"))
        if ct is not None and now_ts - ct > OVERDUE_H * 3600.0:
            late.append((now_ts - ct, str(tk)))
    if not late:
        return {"n": 0, "oldest": None, "oldest_h": None}
    age, tk = max(late)
    return {"n": len(late), "oldest": tk, "oldest_h": round(age / 3600.0, 1)}


def _trade_and_settle(c: SimpleNamespace, get, now: datetime, rules_path: Path, ledger: Path,
                      depth_fn, regime_fn, log: Path | None, state: dict) -> None:
    """The work of one cycle, up to (not including) the ledger append. Everything it
    decides or counts goes into `c` as it happens, so run_cycle can record the decided
    rows and say what happened even when this raises.

    One malformed thing costs that one thing: a market or a position whose handling
    raises is skipped and counted (market_errors), a registry row that cannot be traded
    is left out and named (bad_rules - and the cycle raises RegistryError at its END, so
    it is never a silent skip). Before, any of these aborted the whole cycle, every
    cycle, and threw away the entries it had already decided."""
    active = active_rules(rules_path)
    c.rules = len(active)
    c.bad_rules = unusable_rules(active)
    rules = {n: r for n, r in active.items() if n not in c.bad_rules}
    rows = c.rows = _load_jsonl(ledger)
    live = open_positions(rows)
    used = {k for k in (_key(r.get("event"), r.get("rule")) for r in rows
                        if r.get("t") == "open") if k is not None}
    per_rule_open: dict[str, int] = {}
    for (_, rule) in live:
        per_rule_open[rule] = per_rule_open.get(rule, 0) + 1
    depth_fn = _default_depth_fn() if depth_fn is None else depth_fn
    regime = c.regime = (regime_fn or _default_regime)()
    new, skipped = c.new, c.skipped

    def skip(what: str, e: Exception) -> None:
        c.market_errors += 1
        if c.first_skipped is None:
            c.first_skipped = f"{what}: {type(e).__name__}: {str(e)[:80]}"

    by_series: dict[str, list] = {}
    for r in rules.values():
        for s_ in series_of(r):
            by_series.setdefault(s_, []).append(r)
    c.series = len(by_series)
    named = {n: _ts(r["named_at"]) for n, r in rules.items()}
    handled = 0                                       # markets looked at without a raise
    for series, rs in sorted(by_series.items()):
        try:
            markets = fetch_open(series, get=get)
        except Exception as e:  # noqa: BLE001
            c.errors += 1
            c.listed[series] = False
            _say(f"[warn] {series}: {type(e).__name__}: {str(e)[:80]}", log)
            continue
        c.listed[series] = True
        for m in markets:
            try:
                tk = m.get("ticker")
                if not tk:
                    handled += 1
                    continue
                ev = event_of(m)
                for cand in candidates(m, now.timestamp(), rs):
                    key = (ev, cand["rule"])
                    if key in used:
                        continue                      # one bet per event per rule
                    if now.timestamp() < (named.get(cand["rule"]) or 0):
                        continue
                    if per_rule_open.get(cand["rule"], 0) >= MAX_OPEN_PER_RULE:
                        skipped["open_cap"] = skipped.get("open_cap", 0) + 1
                        continue
                    n = contracts_of(rules[cand["rule"]])
                    # liftable_depth counts resting size up to one tick PAST the price (a
                    # tolerance for quote-vs-book skew). Harmless for a 1-lot - a live
                    # quote always has 1 at the ask - but a sized order booked at the ask
                    # must find all n AT the ask: probe one tick inside it. 1-lots keep
                    # the old probe.
                    probe = (cand["price"] if n == CONTRACTS
                             else round(cand["price"] - 0.01, 4))
                    depth = depth_fn(tk, cand["side"], probe) if depth_fn else None
                    if depth is not None and depth < n:
                        skipped["no_depth"] = skipped.get("no_depth", 0) + 1
                        continue
                    entry = dict(cand, t="open", ts=_iso(now), ticker=tk, event=ev,
                                 series=series, contracts=n,
                                 fee=kalshi_fee(cand["price"], n), depth=depth,
                                 close_time=m.get("close_time"), regime=regime["regime"],
                                 regime_mult=regime["mult"], v=VERSION)
                    new.append(entry)
                    used.add(key)
                    per_rule_open[cand["rule"]] = per_rule_open.get(cand["rule"], 0) + 1
                    if depth is None:
                        c.ungated += 1                # admitted without a fill check
                handled += 1
            except Exception as e:  # noqa: BLE001 - this market only; the rest still trade
                skip(f"a {series} market", e)
        time.sleep(SLEEP_S if get is _get else 0)
    c.list_done = True
    unhandled = c.market_errors

    closes, first_failed = c.closes, None
    positions = list(live.items()) + [((e["ticker"], e["rule"]), e) for e in new]
    # A position more than OVERDUE_H past its close is asked about once an hour, not every
    # cycle: a market that never reports yes or no was polled 720 times a day for good.
    # A late answer only delays the close row - but until then the position also keeps
    # its cap slot, so this applies ONLY while its rule uses fewer than OVERDUE_ROOM of
    # its MAX_OPEN_PER_RULE slots: a rule at or near its cap keeps the every-cycle poll,
    # and the cap could only bind before the next one if that rule made more than
    # MAX_OPEN_PER_RULE - OVERDUE_ROOM entries in a single cycle. The hour is counted in
    # `state`: a one-shot `cycle`, or the first cycle after a restart, asks about all.
    asked = state.setdefault("overdue_asked", {})     # ticker -> when last asked, overdue
    late, wait = set(), {}                            # tickers overdue / that can wait
    for (tk, rule), pos in positions:
        ct = _ts(pos.get("close_time"))
        if ct is not None and ct > now.timestamp():
            continue
        overdue = ct is not None and now.timestamp() - ct > OVERDUE_H * 3600.0
        if overdue:
            late.add(tk)
        wait[tk] = (wait.get(tk, True) and overdue             # every holder must agree
                    and per_rule_open.get(rule, 0) < OVERDUE_ROOM
                    and now.timestamp() - asked.get(tk, float("-inf")) < OVERDUE_POLL_S)
    for tk in [k for k in asked if k not in late]:
        del asked[tk]                                  # settled, or no longer held
    # ticker -> its market, asked ONCE a cycle however many rules hold it (a probe pair
    # holds every market twice, and used to fetch it twice)
    answers: dict = {}
    for (tk, rule), pos in positions:
        try:
            ct = _ts(pos.get("close_time"))
            if ct is not None and ct > now.timestamp():
                continue                               # not closed yet - don't ask
            if wait.get(tk):
                continue                               # overdue, asked within the hour
            if tk not in answers:
                try:
                    answers[tk] = fetch_market(tk, get=get)
                    if tk in late:
                        asked[tk] = now.timestamp()    # answered: ask again in an hour
                except Exception as e:  # noqa: BLE001 - no answer: ask again next cycle
                    answers[tk] = _NO_ANSWER
                    c.poll_errors += 1                 # counted: it used to vanish here
                    first_failed = first_failed or f"{tk}: {type(e).__name__}: {str(e)[:80]}"
            m = answers[tk]
            if m is _NO_ANSWER:
                continue
            if m.get("result") in ("yes", "no"):
                closes.append(settle(pos, m["result"]))
        except Exception as e:  # noqa: BLE001 - this position only; the rest still settle
            skip(f"position {tk}/{rule}", e)
    if c.poll_errors:                                  # ONE line a cycle, not one a poll
        _say(f"[warn] {c.poll_errors} settlement poll(s) failed - first {first_failed}", log)
    if c.market_errors:
        _say(f"[warn] {c.market_errors} market(s) or position(s) skipped - first "
             f"{c.first_skipped}", log)
    if unhandled and not handled:
        # a guard per market must not turn "the trader can no longer read ANY market"
        # (a changed payload, a bug) into a counter: that stays a failed cycle
        raise RuntimeError(f"none of the {unhandled} market(s) listed this cycle could be "
                           f"handled - first {c.first_skipped}")
    if c.bad_rules:
        raise RegistryError(
            f"{len(c.bad_rules)} rule(s) NOT TRADED - registry row unusable: "
            + "; ".join(f"{n} {why}" for n, why in c.bad_rules.items())
            + f". Retire and re-register under a new name. The other {len(rules)} rule(s) "
              f"traded as usual.")


def run_cycle(get=_get, now: datetime | None = None, rules_path: Path = RULES,
              ledger: Path = LEDGER, depth_fn=None, regime_fn=None,
              heartbeat: Path | None = HEARTBEAT, state: dict | None = None,
              log: Path | None = None) -> dict:
    """One pass: enter what the rules call for, settle what Kalshi has resolved, append
    both to the ledger, write the heartbeat, return it.

    state      a dict the caller keeps from one cycle to the next (the `run` loop does):
               the heartbeat's streaks - consecutive cycles in which a series' list call
               failed, in which EVERY list call failed, in which the cycle raised - are
               counted in it. Without one, each call counts from zero.
    log        also append this cycle's warnings to that file (main() passes LOG; tests
               and the selftest pass nothing and write nothing).

    The heartbeat is written by EVERY cycle, also one that raises: it then carries the
    exception text in `cycle_error` and every key it always had, and the cycle re-raises.
    A cycle that died used to leave the old heartbeat in place - ten minutes later the
    watchdog could only say "alive but not cycling", and the reason was gone with the
    console.

    The rows a cycle has decided are appended even when it then raises: an entry that
    passed the fill gate is a bet the rule made, whatever went wrong afterwards. The
    append is tried ONCE - if it is the append that fails, nothing is retried (a second
    try after a partial write would duplicate rows) and the next cycle starts from
    whatever reached the file."""
    t0 = time.time()
    now = now or _now()
    state = {} if state is None else state
    c = SimpleNamespace(new=[], closes=[], skipped={}, ungated=0, errors=0, poll_errors=0,
                        market_errors=0, first_skipped=None, bad_rules={},
                        rules=None, series=None, regime=None, rows=None, listed={},
                        list_done=False, appended=False)
    error: Exception | None = None
    try:
        _trade_and_settle(c, get, now, rules_path, ledger, depth_fn, regime_fn, log, state)
    except Exception as e:  # noqa: BLE001 - reported in the heartbeat, then re-raised
        error = e
    try:
        _append(ledger, c.new + c.closes)
        c.appended = True
    except Exception as e:  # noqa: BLE001
        error = error or e
    summary = _heartbeat_of(c, now, state, error, round(max(0.0, time.time() - t0), 2),
                            heartbeat)
    state["last"] = summary
    if heartbeat is not None:
        _write_heartbeat(heartbeat, summary, log)
    if error is not None:
        raise error
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


def still_open(rows: list[dict], rules: dict[str, dict]) -> dict[str, list[dict]]:
    """rule -> its positions opened AFTER its named_at and not settled yet: the bets that
    will join the rule's record. (Rows are read with .get - a junk row is no position.)"""
    opens = {(r.get("ticker"), r.get("rule")): r for r in rows
             if r.get("t") == "open" and r.get("ticker") and r.get("rule")}
    for r in rows:
        if r.get("t") == "close":
            opens.pop((r.get("ticker"), r.get("rule")), None)
    out: dict[str, list[dict]] = {name: [] for name in rules}
    for (_, name), o in opens.items():
        if name in rules and (_ts(o.get("ts")) or 0) >= (_ts(rules[name].get("named_at")) or 0):
            out[name].append(o)
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
    waiting = still_open(rows, rules)
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
                    if ts else None,
                    # booked with NO fill check (the depth lookup failed): of the settled
                    # trades the verdict counts, and of the bets still waiting to settle
                    "ungated_settled": sum(1 for t in ts if t["depth"] is None),
                    "open": len(waiting[name]),
                    "ungated_open": sum(1 for o in waiting[name] if o.get("depth") is None)})
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
          f"{'95% CI (c/ct)':>15} {'p':>6} {'ungated':>7}  verdict")
    for r in sorted(res, key=lambda x: (-x["events"], x["rule"])):
        wr = f"{r['wr']:.1%}" if r["wr"] is not None else "-"
        ci = f"[{100 * r['lo']:+.1f},{100 * r['hi']:+.1f}]" if r["events"] >= 2 else "-"
        pv = f"{r['p']:.3f}" if r["events"] else "-"
        ug = r["ungated_settled"] + r["ungated_open"]
        print(f"  {r['rule'][:30]:30} {r['events']:>6} {wr:>6} {r['net']:>+9.2f} "
              f"{100 * r['mean']:>+6.1f} {ci:>15} {pv:>6} {ug:>7}  {r['verdict']}")
        if len(r["by_series"]) > 1:
            print("      by series (descriptive, not tested): " + "  ".join(
                f"{k} {n}ev {v:+.2f}" for k, (n, v) in r["by_series"].items()))
        if ug:
            print(f"      no fill check: {r['ungated_settled']} of {r['trades']} settled trades "
                  f"(counted in the verdict), {r['ungated_open']} of {r['open']} still open")
    print()
    for name, why in unusable_rules(rules).items():
        print(f"NOT TRADED: {name} - its registry row {why}, so the trader leaves it out")
        print("(and says so every cycle). Retire it and register the corrected rule under a")
        print("new name; a registered rule is never edited.")
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
    print("ungated = positions booked with NO fill check: the order book could not be read")
    print("(no credentials, or the lookup failed). They are admitted and counted, never")
    print("hidden - and sleeve verdicts count them, so a rule whose ungated number is not")
    print("small is partly judged on fills nobody checked the book for.")
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
    bad = unusable_rules(rules)
    print(f"{len(rules)} active, {len(retired)} retired"
          + (f" - {len(bad)} active rule(s) NOT TRADED (registry row unusable)" if bad else ""))
    for n, r in sorted(rules.items()):
        if n in bad:
            print(f"  {n:30} NOT TRADED: its registry row {bad[n]}. Retire it and register "
                  f"the corrected rule under a new name.")
            continue
        win = (f" {r['min_h']:g}-{r['max_h']:g}h" if r.get("max_h", 1e9) < 1e9 or
               r.get("min_h", 0.0) > 0 else "")
        print(f"  {n:30} {r['side']:8} {r['lo']:.2f}-{r['hi']:.3g} {r['band']:5}{win} "
              f"x{contracts_of(r)} on {series_label(r)}  named {str(r['named_at'])[:16]}"
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

        # the heartbeat says WHY a live trader is not trading: failed calls are counted
        # with their streak, and a cycle that raises still writes it - every key the
        # watchdog indexes, plus the exception
        import contextlib
        import io
        hb, st = Path(td) / "hb.json", {}

        def down(url, params=None):
            raise ConnectionError("exchange unreachable")

        def no_regime():
            raise RuntimeError("regime unreadable")

        with contextlib.redirect_stdout(io.StringIO()):
            for want in (1, 2):
                h = run_cycle(down, later, rp, lp, depth_fn=lambda t, sd, p: 50,
                              regime_fn=lambda: {"regime": "good", "mult": 1.0},
                              heartbeat=hb, state=st)
                assert (h["errors"], h["all_failed_streak"], h["cycle_error"]) == (1, want, None)
            try:
                run_cycle(down, later, rp, lp, depth_fn=lambda t, sd, p: 50,
                          regime_fn=no_regime, heartbeat=hb, state=st)
                raise AssertionError("a failing cycle must still raise")
            except RuntimeError:
                pass
        failed = json.loads(hb.read_text(encoding="utf-8"))
        assert failed["cycle_error"] == "RuntimeError: regime unreadable"
        assert failed["failed_cycles"] == 1 and list(failed)[:10] == list(h)[:10]
        assert (failed["rules"], failed["series"], failed["regime"]) == (1, 1, "good")

        # one malformed thing costs that one thing: a null market entry, a ledger row
        # with no ticker, a registry row with no band - the valid rule still trades, its
        # rows are written, and the cycle still ends in an error naming the unusable rule
        rp2, lp2, st2 = Path(td) / "rules2.jsonl", Path(td) / "ledger2.jsonl", {}
        register(probe_rules(["KXINX"])[:1], rp2, now - timedelta(days=1))
        _append(rp2, [{"t": "rule", "name": "NOBAND", "series": "KXINX", "side": "yes",
                       "lo": 0.1, "hi": 0.2, "named_at": _iso(now - timedelta(days=1))}])
        _append(lp2, [{"t": "open", "ts": _iso(now)}])
        markets.insert(0, None)
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                run_cycle(fake_get, now, rp2, lp2, depth_fn=lambda t, sd, p: 50,
                          regime_fn=lambda: {"regime": "good", "mult": 1.0},
                          heartbeat=None, state=st2)
                raise AssertionError("an unusable registry row must end the cycle in an error")
            except RegistryError as e:
                assert "NOBAND lacks band" in str(e), e
        assert [r["event"] for r in _load_jsonl(lp2) if r.get("t") == "open" and
                r.get("rule") == "FLB_FAV_KXINX"] == ["KXINX-E1", "KXINX-E2"]
        assert st2["last"]["market_errors"] == 1 and st2["last"]["opened"] == 2
        assert st2["last"]["bad_rules"] == {"NOBAND": "lacks band"}

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


def _log_path() -> Path:
    """The log `run` and `cycle` append to: logs/sleeve_paper.log (or SLEEVE_PAPER_LOG) for
    the real ledger; a file next to the ledger when this trader was pointed somewhere
    else. Like the lock: only the live trader writes the live log."""
    if os.environ.get("SLEEVE_PAPER_LOG") or LEDGER.parent == ROOT / "data":
        return LOG
    return LEDGER.parent / "sleeve_paper.log"


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
    # the paths are passed at call time (run_cycle's defaults were bound at import)
    paths = {"rules_path": RULES, "ledger": LEDGER, "heartbeat": HEARTBEAT}
    if cmd == "cycle":
        log = _log_path()
        try:
            s = run_cycle(log=log, **paths)
        except Exception as e:  # noqa: BLE001 - into the log, then the traceback as before
            _log_line(log, f"[warn] cycle failed: {type(e).__name__}: {e}")
            raise
        _log_line(log, cycle_line(s))
        print(s)
        return 0
    if cmd == "run":
        log = _log_path()
        _say(f"sleeve paper - every {CYCLE_S}s -> {LEDGER}. FORWARD-ONLY, no real orders.",
             log)
        state: dict = {}                 # the heartbeat's streaks live here between cycles
        while True:
            t0 = time.time()
            try:
                _say(cycle_line(run_cycle(state=state, log=log, **paths)), log)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001
                last = state.get("last") or {}       # what it recorded before it failed
                did = (f" [recorded first: opened={last['opened']} closed={last['closed']}]"
                       if last.get("opened") or last.get("closed") else "")
                _say(f"[warn] cycle failed ({state.get('failed_cycles', 1)} in a row): "
                     f"{type(e).__name__}: {e}{did}", log)
            time.sleep(max(1.0, CYCLE_S - (time.time() - t0)))
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
