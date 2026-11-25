"""command_center - the one page that answers: what is running, what is it learning,
and has anything earned real money yet? (http://127.0.0.1:5155)

It reads the same files everything else writes - nothing here trades, fetches markets,
or changes state:
  data/paper_crypto15.jsonl     crypto 15-min forward paper ledger
  data/paper_sleeves.jsonl      multi-sleeve forward paper ledger (+ data/sleeve_rules.jsonl)
  data/sleeve_paper_heartbeat.json  the sleeve trader's last cycle
  data/paper_crypto_heartbeat.json  the crypto trader's last cycle (when it writes one)
  data/news_regime.jsonl        the news agent's hourly regime
  data/sleeves/screen.json      sleeve scanner nominations
  logs/healthcheck.log          the watchdog's last verdict

LIVE, NOT JUST SETTLED: a rule's record moves only when a bet settles, so beside it the
page shows the paper positions still open per rule and when each trader last showed
signs of life - a rule with bets open and none settled yet is working, not idle.

ONE STANDARD FOR EVERY RULE. All rules - crypto and sleeves - are scored in one family:
per independent window/event, against the variance a fair price would produce, with
Benjamini-Hochberg across every rule at once (the same machinery as sleeve_paper and
sleeve_scan). Retired sleeve rules are not shown, but their records stay in the family,
and `sleeve_paper.py report` corrects over this same family (crypto_family). Crypto
rules are judged ONLY on fill-checked (v4 gated) trades: before 2026-09-25 the fill gate
was a silent no-op, and the slice evidence says the late-longshot record lived in quotes
nobody could fill. The all-era record is still shown.

Design follows the dataviz method: the headline is a number (rules that earned an edge),
color is reserved for identity (two series) and status (good/bad, always with an icon
and a label), every chart has a table view, and every label is inserted as text.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
CRYPTO_LEDGER = Path(os.environ.get("PAPER_CRYPTO_LEDGER") or (DATA / "paper_crypto15.jsonl"))
HEALTH_LOG = Path(os.environ.get("POLYBOT_HEALTH_LOG") or (ROOT / "logs" / "healthcheck.log"))
SCREEN = Path(os.environ.get("SLEEVE_DIR") or (DATA / "sleeves")) / "screen.json"
MIN_EVENTS = 100
_CACHE: dict = {"t": 0.0, "payload": None}


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


def _ts(iso) -> float | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _why(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:200]}"


def _downsample(points: list, n: int = 40) -> list:
    if len(points) <= n:
        return points
    step = (len(points) - 1) / (n - 1)
    return [points[round(i * step)] for i in range(n)]


# ── trades from both ledgers, one shape ───────────────────────────────────────

def crypto_trades(rows: list[dict]) -> list[dict]:
    from vol_replay import era_of, window_of
    opens = {(r.get("ticker"), r.get("rule")): r for r in rows if r.get("t") == "open"}
    out = []
    for r in rows:
        if r.get("t") != "close":
            continue
        o = opens.get((r.get("ticker"), r.get("rule"))) or {}
        n = max(1, int(o.get("contracts") or 1))
        pnl = float(r.get("pnl") or 0.0)
        out.append({"rule": r.get("rule"), "venue": "crypto", "event": window_of(r.get("ticker")),
                    "pnl": pnl, "pnl_pc": pnl / n,
                    "fee_pc": float(o["fee"]) / n if o.get("fee") is not None else None,
                    "won": bool(r.get("won")),
                    "ask": float(r.get("price") or o.get("price") or 0.5),
                    "ts": _ts(r.get("ts")) or _ts(o.get("ts")) or 0.0,
                    "era": era_of({"v": o.get("v"), "depth": o.get("depth")})})
    return out


def sleeve_trades(rows: list[dict] | None = None) -> tuple[list[dict], dict, list[dict]]:
    """Trades the sleeve report counts, by the sleeve report's own code (trades_of: after
    named_at, per contract), labelled for the table - plus the RETIRED rules' records
    (sleeve_paper.retired_stats): not shown, but they stay in the family correction.
    rows: the sleeve ledger, when the caller has read it already (read it once a build)."""
    try:
        import sleeve_paper as sp
    except Exception:  # noqa: BLE001
        return [], {}, []
    rules = sp.active_rules(sp.RULES)          # explicit: the default arg binds at def time
    rows = _load_jsonl(sp.LEDGER) if rows is None else rows
    out = []
    for name, ts in sp.trades_of(rows, rules).items():
        label = sp.series_label(rules[name])
        out += [dict(t, venue="sleeves", series=label,
                     era="v4 gated" if t["depth"] is not None else "v4 ungated") for t in ts]
    return out, rules, sp.retired_stats(rows, sp.RULES)


def _series_count(srules: dict) -> int:
    try:
        import sleeve_paper as sp
        return len({x for r in srules.values() for x in sp.series_of(r)})
    except Exception:  # noqa: BLE001
        return 0


# ── live activity: bets still open, and is each trader still cycling ──────────

# The sleeve trader stamps a heartbeat every cycle (2 min); the watchdog FAILs it past 10.
SLEEVE_LATE_MIN = 10.0
# A heartbeat a minute old only says the trader is ALIVE. It also says whether the cycle
# worked: `cycle_error` (the cycle raised) or `all_failed_streak` (cycles in a row in which
# EVERY market-list call failed). This many of those in a row is failing, not a blip.
ALL_FAILED_CYCLES = 3
# The crypto trader writes a heartbeat too (after every completed 60 s cycle) once it
# runs the code that has one; five missed cycles is late.
CRYPTO_HEARTBEAT = Path(os.environ.get("PAPER_CRYPTO_HEARTBEAT")
                        or (DATA / "paper_crypto_heartbeat.json"))
CRYPTO_LATE_MIN = 5.0
# Without a heartbeat (a trader started before it existed keeps running old code after a
# pull until it is restarted) its newest ledger row stands in - and a row is written only
# when a bet opens or settles. At its concurrency cap a healthy trader goes ~11-15 min
# from its last entry in a 15-min window to the next settlement, so a 10-min bar would
# flag it every window. 20 = a whole window with neither, plus settlement lag.
CRYPTO_QUIET_MIN = 20.0


def _open_by_rule(rows: list[dict], open_positions) -> dict[str, int]:
    """rule -> paper positions entered and not yet settled, by the trader's OWN
    open_positions: an open row with no close for the same (ticker, rule)."""
    out: dict[str, int] = {}
    for _, rule in open_positions([r for r in rows if r.get("ticker") and r.get("rule")]):
        out[rule] = out.get(rule, 0) + 1
    return out


def _freshness(venue: str, name: str, what: str, t: float | None, limit_min: float,
               now: float, late: str, missing: str) -> dict:
    out = {"venue": venue, "name": name, "what": what, "ts": None, "age_min": None,
           "limit_min": limit_min, "status": "missing", "note": missing}
    if t is None:
        return out
    age = max(0.0, (now - t) / 60.0)
    return dict(out, ts=datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds"),
                age_min=round(age, 1), status="good" if age < limit_min else "warning",
                note=None if age < limit_min else late)


def _heartbeat(path: Path) -> tuple[dict | None, bool]:
    """(heartbeat, the file exists). None = not there, or not a readable JSON object."""
    if not path.exists():
        return None, False
    try:
        d = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None, True
    return (d if isinstance(d, dict) else None), True


def _count(d: dict, key: str) -> int:
    """A heartbeat counter. A key that is missing or is not a count reads 0: a trader
    that still runs the code from before the key existed is UNKNOWN on it, not at fault."""
    v = d.get(key)
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _clip(text, n: int) -> str:
    text = str(text)
    return text if len(text) <= n else text[:n].rstrip() + "..."


def _overdue_note(hb: dict, past: str) -> str | None:
    """Open positions the trader itself reports as stranded past their close."""
    od = hb.get("overdue")
    if not isinstance(od, dict) or not _count(od, "n"):
        return None
    note = f"{_plural(od['n'], 'open position')} overdue - {past} with no result"
    if od.get("oldest"):
        hours = od.get("oldest_h")
        note += f" (oldest {_clip(od['oldest'], 40)}" + (
            f", {hours:.0f} h)" if isinstance(hours, (int, float)) else ")")
    return note


def _with_notes(chip: dict, failing: list[str], watch: list[str]) -> dict:
    """A chip built from a heartbeat, plus what that heartbeat says about the cycle
    itself: `failing` (why it is not working) and `watch` (worth a look). Every status
    is a word in the note as well as an icon on the page."""
    parts = [chip["note"]] if chip["note"] else []          # "late - ...", when it is
    if failing:
        parts.append("failing - " + "; ".join(failing))
    status = "failing" if failing else ("warning" if parts or watch else "good")
    return dict(chip, status=status, note=" · ".join(parts + watch) or None)


def _sleeve_trader(now: float) -> dict:
    import sleeve_paper as sp
    hb, exists = _heartbeat(sp.HEARTBEAT)        # read at call time: tests and env move it
    t = _ts(hb.get("ts")) if hb else None
    chip = _freshness("sleeves", "Sleeve paper trader", "last cycle", t, SLEEVE_LATE_MIN, now,
                      "late - it cycles every 2 min",
                      "heartbeat unreadable" if exists else "no heartbeat yet - it has never cycled")
    if t is None:
        return chip
    failing = []
    if hb.get("cycle_error"):
        n = _count(hb, "failed_cycles")
        failing.append(f"its last cycle raised {_clip(hb['cycle_error'], 160)}"
                       + (f" ({n} cycles in a row)" if n > 1 else ""))
    streak = _count(hb, "all_failed_streak")
    if streak >= ALL_FAILED_CYCLES:
        failing.append(f"every market list call has failed for {streak} cycles in a row")
    overdue = _overdue_note(hb, "more than a day past their close")
    return _with_notes(chip, failing, [overdue] if overdue else [])


def _crypto_trader(crypto_rows: list[dict] | None, now: float) -> dict:
    hb, _ = _heartbeat(CRYPTO_HEARTBEAT)
    t = _ts(hb.get("ts")) if hb else None
    if t is None:           # no heartbeat to read: unknown - the ledger stands in, as before
        newest = next((x for x in (_ts(r.get("ts")) for r in reversed(crypto_rows or []))
                       if x is not None), None)      # append-only: the last row is newest
        return _freshness("crypto", "Crypto paper trader", "newest ledger row", newest,
                          CRYPTO_QUIET_MIN, now,
                          "quiet - a row is written only when a bet opens or settles",
                          "ledger could not be read" if crypto_rows is None
                          else "no ledger rows yet")
    chip = _freshness("crypto", "Crypto paper trader", "last cycle", t, CRYPTO_LATE_MIN, now,
                      "late - it cycles every minute", "")
    watch = []
    failed = _count(hb, "errors")
    if failed:
        watch.append(f"{_plural(failed, 'request')} failed in its last cycle"
                     + (f" (last: {_clip(hb['last_error'], 120)})" if hb.get("last_error") else ""))
    overdue = _overdue_note(hb, "more than an hour past their close")
    return _with_notes(chip, [], watch + ([overdue] if overdue else []))


def trader_freshness(crypto_rows: list[dict] | None, now: float | None = None) -> list[dict]:
    """Is each paper trader working - not merely alive? Both from their heartbeats: when
    the last cycle ran, and what it says about itself. A sleeve trader whose cycle raises,
    or whose every market-list call has failed for ALL_FAILED_CYCLES cycles, is 'failing'
    however fresh its heartbeat. The crypto trader is read from its heartbeat when it has
    one, else from its newest ledger row. A missing file or key is unknown, never a fault.
    crypto_rows is None when the crypto ledger could not be read - not 'no rows yet'."""
    now = time.time() if now is None else now
    return [_sleeve_trader(now), _crypto_trader(crypto_rows, now)]


# ── the one scoring family ────────────────────────────────────────────────────

_STATS_MEMO: dict[str, dict] = {}
_STATS_MEMO_MAX = 512


def _money(trades: list[dict]) -> dict:
    """What a record actually paid and cost: the net alone hides the shape - a 97-99c
    favorite wins $0.10-0.30 at a time and loses ~$9.80 when it loses."""
    return {"won": round(sum(t["pnl"] for t in trades if t["pnl"] > 0), 2),
            "lost": round(-sum(t["pnl"] for t in trades if t["pnl"] < 0), 2),
            "losses": sum(1 for t in trades if not t["won"])}


def _record_key(trades: list[dict]) -> str:
    """A fingerprint of everything forward_stats reads. A rule's record only changes when
    one of its trades settles, so the exact tails are reused across dashboard rebuilds."""
    import hashlib
    h = hashlib.sha1()
    for t in trades:
        h.update(repr((str(t["event"]), bool(t["won"]), float(t["ask"]), t.get("fee_pc"),
                       t.get("pnl_pc"), float(t["pnl"]))).encode())
    return f"{len(trades)}:{h.hexdigest()}"


def _stats(trades: list[dict]) -> dict:
    """sleeve_paper.forward_stats, memoized on the record. Exact Poisson-binomial tails
    cost up to ~0.15 s each on long records and the page rebuilds every poll; without
    this a 40-rule ledger took 17 s per refresh."""
    import sleeve_paper as sp
    key = _record_key(trades)
    hit = _STATS_MEMO.get(key)
    if hit is None:
        hit = sp.forward_stats(trades)
        if len(_STATS_MEMO) >= _STATS_MEMO_MAX:
            _STATS_MEMO.pop(next(iter(_STATS_MEMO)))          # oldest first
        _STATS_MEMO[key] = hit
    return dict(hit)


def _decision_basis(ts: list[dict], venue: str) -> list[dict]:
    """The trades a verdict is decided on: a crypto rule's fill-checked (v4 gated) trades
    only; a sleeve rule's every trade after named_at."""
    return [t for t in ts if t["era"] == "v4 gated"] if venue == "crypto" else ts


def crypto_family(path: Path | None = None) -> list[dict]:
    """The crypto rules' members of the one forward family: each rule's decision stats,
    built exactly as score_rules builds them. The sleeve report corrects over these too,
    so both surfaces judge a sleeve rule against the same family."""
    by_rule: dict[str, list] = {}
    for t in crypto_trades(_load_jsonl(path or CRYPTO_LEDGER)):
        by_rule.setdefault(t["rule"], []).append(t)
    return [_stats(_decision_basis(ts, "crypto")) for _, ts in sorted(by_rule.items())]


def score_rules(trades: list[dict], sleeve_rules=None, extra_stats=(),
                open_now: dict | None = None) -> list[dict]:
    """Every rule, one family. Crypto rules are judged on fill-checked (v4 gated) trades
    only; sleeve rules on everything after named_at. BH across all testable rules - the
    rules shown plus extra_stats (the retired sleeve rules' records, never shown).

    sleeve_rules: the active sleeve rules, name -> registration (a bare set of names also
    works). Each is shown from registration on, LABELLED FROM ITS REGISTRATION: a rule
    with nothing settled has no trade to take a label from. open_now: (venue, rule) ->
    paper positions not settled yet; a crypto rule whose first bets are all still open is
    listed too (nothing settled, nothing tested: n = 0, so the family is unchanged)."""
    import sleeve_paper as sp
    regs = sleeve_rules if isinstance(sleeve_rules, dict) else dict.fromkeys(sleeve_rules or ())
    open_now = open_now or {}
    by_rule: dict[str, list] = {}
    venue_of: dict[str, str] = {}
    for t in trades:
        by_rule.setdefault(t["rule"], []).append(t)
        venue_of.setdefault(t["rule"], t["venue"])
    for name in regs:
        by_rule.setdefault(name, [])
        venue_of.setdefault(name, "sleeves")
    for venue, name in open_now:
        if venue == "crypto":                    # sleeve rules are listed by registration
            by_rule.setdefault(name, [])
            venue_of.setdefault(name, "crypto")
    rows = []
    for name, ts in by_rule.items():
        venue = venue_of[name]
        basis = _decision_basis(ts, venue)
        s_all = _stats(ts)
        s_dec = dict(s_all) if basis is ts else _stats(basis)   # sleeves: the same list
        cum, acc = [], 0.0
        for t in sorted(ts, key=lambda x: x["ts"]):
            acc += t["pnl"]
            cum.append(round(acc, 4))
        reg = regs.get(name)
        rows.append({"rule": name, "venue": venue,
                     "series": sp.series_label(reg) if reg else
                     next((t.get("series") for t in ts if t.get("series")), None),
                     "open": open_now.get((venue, name), 0),
                     "all": s_all, "decision": s_dec, "money": _money(basis),
                     "spark": _downsample(cum)})
    keep = sp.survivors([r["decision"] for r in rows] + list(extra_stats))
    for i, r in enumerate(rows):                  # rows come first: index i is rows[i]
        d = r["decision"]
        basis = "fill-checked" if r["venue"] == "crypto" else "after registration"
        cat = sp.classify(d, i in keep)
        r["verdict"], r["status"] = {
            "collecting": (f"collecting · {d['n']}/{MIN_EVENTS} {basis}", "neutral"),
            "edge": ("EDGE · survives the family correction", "good"),
            "promising": (f"promising · needs {MIN_EVENTS} events", "warning"),
            "negative": ("negative · significant", "critical"),
        }.get(cat, ("no edge yet", "neutral"))
    order = {"good": 0, "warning": 1, "neutral": 2, "critical": 3}
    rows.sort(key=lambda r: (order[r["status"]], -r["decision"]["n"], r["rule"]))
    return rows


def equity_series(trades: list[dict]) -> dict:
    out = {}
    for venue in ("crypto", "sleeves"):
        by_day: dict[str, float] = {}
        for t in trades:
            if t["venue"] == venue and t["ts"]:
                d = datetime.fromtimestamp(t["ts"], tz=timezone.utc).date().isoformat()
                by_day[d] = by_day.get(d, 0.0) + t["pnl"]
        acc, pts = 0.0, []
        for d in sorted(by_day):
            acc += by_day[d]
            pts.append([d, round(acc, 2)])
        out[venue] = pts
    return out


# ── the other panels ──────────────────────────────────────────────────────────

def regime_panel(days: int = 14) -> dict:
    try:
        from lib import news_regime as nr
    except Exception:  # noqa: BLE001
        return {"current": {"regime": "unknown", "why": "news agent unavailable"}, "cells": []}
    cur = nr.current_regime()
    cutoff = time.time() - days * 86400
    cells: dict[tuple, str] = {}
    for r in nr.load_history():
        t = _ts(r.get("ts"))
        if t is None or t < cutoff:
            continue
        utc = datetime.fromtimestamp(t, tz=timezone.utc)
        et = utc + timedelta(hours=nr.et_offset_hours(utc))
        cells[(et.date().isoformat(), et.hour)] = r.get("regime", "unknown")
    return {"current": {"regime": cur["regime"], "mult": cur["mult"], "why": cur["why"],
                        "ts": cur["ts"]},
            "cells": [{"d": d, "h": h, "r": v} for (d, h), v in sorted(cells.items())]}


# healthcheck.py --log writes "<ts> healthy|UNHEALTHY fails=N warns=N", then one indented
# "    [STATUS] section: message" line per check that did not pass (STATUS is FAIL, WARN or
# a padded "OK  ", which --log skips). It writes them in CHECK order, so a warning from an
# early check (auth, tasks) sat above the failure that made the run unhealthy.
_HEALTH_RANK = {"FAIL": 0, "WARN": 1, "OK": 3}       # an untagged note sits at 2


def _health_tag(line: str) -> str:
    return line[1:line.index("]")].strip() if line.startswith("[") and "]" in line else ""


def health_panel() -> dict:
    """The watchdog's last verdict with its reasons first: FAIL lines, then WARN, then any
    other note, OK last - sorted before the 20-line cap, so a failure is never cut."""
    out = {"state": "unknown", "ts": None, "age_min": None, "lines": [], "fails": 0,
           "warns": 0, "failing": []}
    if not HEALTH_LOG.exists():
        return dict(out, lines=["the watchdog has not run yet"])
    try:
        lines = HEALTH_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    except OSError:
        return dict(out, lines=["watchdog log unreadable"])
    head = max((i for i, l in enumerate(lines) if l and not l[0].isspace()), default=None)
    if head is None:
        return out
    parts = lines[head].split()
    state = "healthy" if len(parts) > 1 and parts[1] == "healthy" else "unhealthy"
    detail = [l.strip() for l in lines[head + 1:] if l.startswith(" ")]
    detail.sort(key=lambda l: _HEALTH_RANK.get(_health_tag(l), 2))   # stable within a tag
    fails = [l for l in detail if _health_tag(l) == "FAIL"]
    age = (time.time() - (_ts(parts[0]) or 0)) / 60.0
    if age > 30:
        state = "stale"
        detail.insert(0, f"last watchdog run was {age:.0f} min ago (it runs every 15)")
    return dict(out, state=state, ts=parts[0], age_min=round(age, 1), lines=detail[:20],
                fails=len(fails), warns=sum(_health_tag(l) == "WARN" for l in detail),
                failing=list(dict.fromkeys(l.split("]", 1)[1].split(":", 1)[0].strip()[:24]
                                           for l in fails)))


_NOMINATION_KEYS = ("series", "band", "side", "bucket", "n_out", "mean_out", "p_out",
                    "mean_out_traded", "verdict")


def _read_nominations() -> tuple[list[dict], str | None]:
    """(nominations, note). screen.json is the least important input on the page and is
    written by a hand-run tool, so it is read for whatever it holds: a file that is not
    the scanner's shape gives no nominations, an entry that is not an object is skipped,
    and the note says what was wrong - a damaged file must not read as 'none nominated'
    without a word. A missing file is no note: the scanner has simply not run."""
    p = SCREEN
    if not p.exists():
        return [], None
    try:
        # errors="replace" + ValueError: one undecodable byte used to raise out of here
        # and replace the WHOLE payload with an error - every panel for the least
        # important input. A damaged file is "no nominations", like a truncated one.
        d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as e:
        return [], f"screen.json could not be read - {_why(e)}"
    if not isinstance(d, dict):
        return [], (f"screen.json is not the scanner's output (a {type(d).__name__} where "
                    f"an object belongs) - run the screen again")
    noms = d.get("nominated")
    if noms is None:
        return [], None
    if not isinstance(noms, list):
        return [], (f"screen.json is not the scanner's output ('nominated' is a "
                    f"{type(noms).__name__}, not a list) - run the screen again")
    rows = [{k: c.get(k) for k in _NOMINATION_KEYS} for c in noms if isinstance(c, dict)]
    skipped = len(noms) - len(rows)
    return rows[:30], (f"{skipped} of {len(noms)} entries in screen.json are not "
                       f"nominations - skipped" if skipped else None)


def nominations_panel() -> list[dict]:
    return _read_nominations()[0]


# ── one panel, one guard ──────────────────────────────────────────────────────
#
# One unreadable input used to fail the whole payload: a ledger another program held
# open (a sharing violation on Windows), a screen.json that was not the scanner's shape,
# or one malformed ledger row replaced every panel with a single error line - the page
# went blank exactly when something was wrong. Each panel is now built under its own
# guard, and a panel that failed is named in `errors` (the page shows each as a chip).
#
# The scored panels - rules, headline numbers, equity - are ONE block: every rule is
# judged in one family across both ledgers, so a table scored without one of them would
# show verdicts the full family does not give. It is built whole or not at all. When it
# cannot be built the page keeps the LAST GOOD block, marked with its time (scored_at);
# before any good build the three are null - never zeros, which would read "0 of 0
# rules have earned an edge", unknown shown as a number.

_GOOD: dict = {"scored": None, "at": None}     # the last scored block that built, and when


def _guarded(errors: list, panel: str, build, fallback):
    """build(), or - when it raises - `fallback` (called with the exception if callable)
    and an entry in errors. One bad input costs its own panel, never the page."""
    try:
        return build()
    except Exception as e:  # noqa: BLE001
        errors.append({"panel": panel, "error": f"could not be read - {_why(e)}"})
        return fallback(e) if callable(fallback) else fallback


def _scored(crows: list[dict]) -> dict:
    """The scored block: the rules, the headline numbers and the equity series."""
    import paper_trader as pt
    import sleeve_paper as sp
    srows = _load_jsonl(sp.LEDGER)                      # each ledger is read once a build
    ct = crypto_trades(crows)
    st, srules, retired = sleeve_trades(srows)
    trades = ct + st
    c_open = _open_by_rule(crows, pt.open_positions)
    s_open = _open_by_rule(srows, sp.open_positions)
    open_now = {("crypto", r): n for r, n in c_open.items()}
    open_now.update({("sleeves", r): n for r, n in s_open.items() if r in srules})
    rules = score_rules(trades, srules, retired, open_now)
    n_crypto = sum(c_open.values())
    n_sleeves = sum(n for (venue, _), n in open_now.items() if venue == "sleeves")
    return {
        "kpi": {"rules": len(rules), "edge": sum(r["status"] == "good" for r in rules),
                "net": round(sum(t["pnl"] for t in trades), 2),
                **{k: v for k, v in _money(trades).items() if k in ("won", "lost")},
                "net_crypto": round(sum(t["pnl"] for t in ct), 2),
                "net_sleeves": round(sum(t["pnl"] for t in st), 2),
                "windows": len({t["event"] for t in ct}),
                "events": len({t["event"] for t in st}),
                "sleeves": _series_count(srules),
                # the rules shown; a retired rule's bets still settle, into its record
                "open": n_crypto + n_sleeves, "open_crypto": n_crypto,
                "open_sleeves": n_sleeves, "open_retired": sum(s_open.values()) - n_sleeves},
        "rules": rules, "equity": equity_series(trades)}


def _regime_unread(e: BaseException) -> dict:
    return {"current": {"regime": "unknown", "why": f"the regime could not be read ({_why(e)})"},
            "cells": [], "unread": True}


def _health_unread(e: BaseException) -> dict:
    return {"state": "unknown", "ts": None, "age_min": None, "fails": 0, "warns": 0,
            "failing": [], "lines": [f"the watchdog verdict could not be read ({_why(e)})"]}


def build_payload() -> dict:
    now = time.time()
    if _CACHE["payload"] is not None and now - _CACHE["t"] < 30:
        return _CACHE["payload"]
    generated = datetime.now(timezone.utc).isoformat()
    errors: list[dict] = []
    read: dict = {}                    # the crypto ledger: the trader chips read it too

    def scored() -> dict:
        read["crypto"] = _load_jsonl(CRYPTO_LEDGER)
        return _scored(read["crypto"])

    def nominations() -> list[dict]:
        rows, note = _read_nominations()
        if note:
            errors.append({"panel": "nominations", "error": note})
        return rows

    block, scored_at = _guarded(errors, "rules", scored, None), generated
    if block is not None:
        _GOOD.update(scored=block, at=generated)
    else:
        last = dict(_GOOD)             # one read: its block and its time belong together
        block, scored_at = last["scored"] or dict.fromkeys(("kpi", "rules", "equity")), last["at"]
    payload = {
        "generated": generated,
        "live_money": "OFF",
        "kpi": block["kpi"], "rules": block["rules"], "equity": block["equity"],
        # when the three above were scored. Equal to `generated` on a good build; older
        # = the last good values, kept because this build could not score (see errors);
        # None = nothing has been scored yet, and the three are null.
        "scored_at": scored_at,
        "traders": _guarded(errors, "traders",
                            lambda: trader_freshness(read.get("crypto"), now), []),
        "regime": _guarded(errors, "regime", regime_panel, _regime_unread),
        "health": _guarded(errors, "health", health_panel, _health_unread),
        "nominations": _guarded(errors, "nominations", nominations, []),
        "errors": errors,
    }
    _CACHE.update(t=now, payload=payload)
    return payload


# ── server ────────────────────────────────────────────────────────────────────

def make_app():
    from flask import Flask, jsonify, Response
    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(_PAGE, mimetype="text/html")

    @app.route("/api/command")
    def api():
        try:
            return jsonify(build_payload())
        except Exception as e:  # noqa: BLE001 - a panel error must not 500 the page
            return jsonify({"error": f"{type(e).__name__}: {str(e)[:200]}"}), 200
    return app


def run_dashboard(port: int = 5155):
    import socket
    host = os.environ.get("POLYBOT_DASH_HOST", "127.0.0.1")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError as e:
        s.close()
        raise RuntimeError(f"Port {port} is already in use ({e}).")
    s.close()
    print(f"Command center -> http://localhost:{port}")
    make_app().run(host=host, port=port, debug=False, use_reloader=False)


# ── the page ──────────────────────────────────────────────────────────────────

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polybot Command Center</title>
<style>
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s2: #eb6834; --deemph: #c3c2b7;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --good-text: #006300; --neutral-cell: #e1e0d9;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --s1: #3987e5; --s2: #d95926; --deemph: #52514e; --good-text: #0ca30c;
    --neutral-cell: #383835;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --s1: #3987e5; --s2: #d95926; --deemph: #52514e; --good-text: #0ca30c;
  --neutral-cell: #383835;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1200px; margin: 0 auto; padding: 20px 16px 48px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 16px; }
h1 { font-size: 20px; font-weight: 600; margin: 0; }
.sub { color: var(--muted); font-size: 12px; }
.pill { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--ring);
        border-radius: 999px; padding: 2px 10px; font-size: 12px; color: var(--ink-2);
        background: var(--surface); }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.filters { display: flex; gap: 8px; margin: 16px 0; flex-wrap: wrap; }
.seg { display: inline-flex; border: 1px solid var(--ring); border-radius: 8px; overflow: hidden; }
.seg button { border: 0; background: var(--surface); color: var(--ink-2); padding: 6px 12px;
              font: inherit; cursor: pointer; }
.seg button[aria-pressed="true"] { background: var(--ink); color: var(--surface); }
.grid { display: grid; gap: 12px; }
.kpis { grid-template-columns: minmax(0, 1.6fr) repeat(5, minmax(0, 1fr)); }
@media (max-width: 1100px) { .kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 900px) { .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
                            .kpis .hero, .kpis .wide { grid-column: 1 / -1; } }
.traders { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.chip { display: inline-flex; align-items: flex-start; gap: 6px; max-width: 100%;
        border: 1px solid var(--ring); border-radius: 8px; padding: 6px 10px;
        background: var(--surface); font-size: 12px; color: var(--ink-2); }
.chip strong, .health strong { color: var(--ink); font-weight: 600; }
.chip span { min-width: 0; overflow-wrap: anywhere; }
.notices { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.notices:empty { display: none; }
.note a, .note a:visited { color: var(--ink-2); }
.opencount { color: var(--ink-2); font-weight: 600; }
.reason { color: var(--ink); font-size: 12px; margin-top: 6px; overflow-wrap: anywhere;
          display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
          overflow: hidden; }
.two { grid-template-columns: 1fr 1fr; align-items: start; }
@media (max-width: 900px) { .two { grid-template-columns: 1fr; } }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px;
        padding: 16px; min-width: 0; }
.card h2 { font-size: 13px; font-weight: 600; margin: 0 0 4px; }
.card .cap { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
.label { color: var(--ink-2); font-size: 12px; }
.value { font-size: 24px; font-weight: 600; margin-top: 4px; }
.hero .value { font-size: 56px; line-height: 1.05; }
.value .status { font-size: 20px; }
#eq-chart, #rg-chart { overflow-x: auto; }
.hero .of { font-size: 20px; color: var(--ink-2); font-weight: 500; }
.note { color: var(--ink-2); font-size: 12px; margin-top: 6px; }
.status { display: inline-flex; align-items: center; gap: 6px; font-weight: 600; }
.icon { width: 16px; height: 16px; flex: none; }
.cardhead { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
.linkbtn { border: 0; background: none; color: var(--ink-2); font: inherit; font-size: 12px;
           text-decoration: underline; cursor: pointer; padding: 0; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th { text-align: left; font-weight: 500; color: var(--muted); font-size: 12px;
     border-bottom: 1px solid var(--grid); padding: 6px 8px; white-space: nowrap; }
td { border-bottom: 1px solid var(--grid); padding: 6px 8px; vertical-align: middle;
     white-space: nowrap; }
td.num, th.num { text-align: right; }
.rule { font-weight: 500; }
.venue { color: var(--muted); font-size: 12px; }
.legend { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: var(--ink-2);
          margin-bottom: 8px; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.key { width: 14px; height: 2px; display: inline-block; border-radius: 1px; }
.sw { width: 10px; height: 10px; display: inline-block; border-radius: 2px; }
.tip { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
       border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 12px;
       box-shadow: 0 4px 16px rgba(0,0,0,.12); display: none; z-index: 10; min-width: 140px; }
.tip .row { display: flex; align-items: center; gap: 8px; justify-content: space-between; }
.tip .v { font-weight: 600; }
.tip .n { color: var(--ink-2); }
.loading { opacity: .55; transition: opacity .2s; }
.empty { color: var(--muted); font-size: 13px; padding: 12px 0; }
svg text { fill: var(--muted); font-size: 11px; }
.health li { margin: 4px 0; display: flex; gap: 8px; align-items: flex-start; }
.health li span { min-width: 0; overflow-wrap: anywhere; }
.health ul { list-style: none; padding: 0; margin: 0; font-size: 12px; color: var(--ink-2); }
.health details { margin-top: 8px; font-size: 12px; color: var(--ink-2); }
.health summary { cursor: pointer; color: var(--muted); }
section { margin-top: 12px; }
#health { scroll-margin-top: 12px; }
</style>
</head>
<body>
<main id="app">
  <header>
    <h1>Polybot command center</h1>
    <span class="pill" id="live"></span>
    <span class="sub" id="gen"></span>
  </header>

  <div class="filters" role="group" aria-label="Venue filter">
    <div class="seg" id="venue">
      <button aria-pressed="true" data-v="all">All rules</button>
      <button aria-pressed="false" data-v="crypto">Crypto 15-min</button>
      <button aria-pressed="false" data-v="sleeves">Sleeves</button>
    </div>
  </div>

  <div class="notices" id="notices" role="group" aria-label="Panels that could not be read"></div>
  <div class="grid kpis" id="kpis"></div>
  <div class="traders" id="traders" role="group" aria-label="Paper traders, live"></div>

  <section class="card" id="equity-card">
    <div class="cardhead"><h2>Paper P&amp;L, cumulative</h2>
      <button class="linkbtn" id="eq-toggle">Show table</button></div>
    <p class="cap">Forward paper only - every trade stamped before its market settled.</p>
    <div class="legend" id="eq-legend"></div>
    <div id="eq-chart"></div>
    <div id="eq-table" hidden class="scroll"></div>
  </section>

  <section class="card">
    <div class="cardhead"><h2>Every rule, one standard</h2></div>
    <p class="cap">Judged per independent window/event and per contract, by the exact odds
      of the record with no edge (a favorite that hasn't lost yet is not proof), with one
      false-discovery correction across all rules. Crypto rules count only
      fill-checked trades (the fill gate was a no-op before Sep 25). EDGE needs
      100 events and survival of the correction. A rule pooling several series counts
      each series' event as its own bet, so it assumes those series settle
      independently.</p>
    <div class="scroll"><table id="rules"></table></div>
  </section>

  <div class="grid two">
    <section class="card">
      <div class="cardhead"><h2>News regime, last 14 days</h2>
        <button class="linkbtn" id="rg-toggle">Show table</button></div>
      <p class="cap" id="rg-cap"></p>
      <div class="legend" id="rg-legend"></div>
      <div id="rg-chart"></div>
      <div id="rg-table" hidden class="scroll"></div>
    </section>
    <section class="card health" id="health">
      <h2>System health</h2>
      <p class="cap" id="hl-cap"></p>
      <ul id="hl"></ul>
      <details id="hl-ok" hidden><summary id="hl-ok-sum"></summary><ul id="hl-ok-list"></ul></details>
    </section>
  </div>

  <section class="card">
    <h2>Sleeve scanner nominations</h2>
    <p class="cap">Candidates only - each must be registered and earn its keep on forward paper.</p>
    <div class="scroll"><table id="noms"></table></div>
  </section>
</main>
<div class="tip" id="tip" role="tooltip"></div>

<script>
"use strict";
const SVGNS = "http://www.w3.org/2000/svg";
const STATUS = {
  good:     {label: "Good",      color: "var(--good)"},
  warning:  {label: "Watch",     color: "var(--warning)"},
  critical: {label: "Negative",  color: "var(--critical)"},
  neutral:  {label: "Collecting", color: "var(--muted)"},
};
const REGIME = {
  good:     {label: "Good day",     color: "var(--good)",    icon: "check"},
  cautious: {label: "Cautious day", color: "var(--warning)", icon: "alert"},
  unknown:  {label: "Unknown",      color: "var(--neutral-cell)", icon: "dash"},
};
let DATA = null, VENUE = "all";

function el(tag, attrs, text) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}
function svg(tag, attrs) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  return e;
}
function icon(kind, color) {
  const s = svg("svg", {viewBox: "0 0 16 16", class: "icon", "aria-hidden": "true"});
  s.appendChild(svg("circle", {cx: 8, cy: 8, r: 7, fill: color}));
  const ink = kind === "alert" ? "#0b0b0b" : "#ffffff";
  const d = {check: "M4.5 8.2l2.2 2.2 4.8-4.8", alert: "M8 4.2v4.6M8 11.2v.4",
             cross: "M5.2 5.2l5.6 5.6M10.8 5.2l-5.6 5.6", dash: "M5 8h6"}[kind];
  s.appendChild(svg("path", {d, stroke: ink, "stroke-width": 1.8, fill: "none",
                             "stroke-linecap": "round", "stroke-linejoin": "round"}));
  return s;
}
const usd = (v) => (v < 0 ? "-$" : "+$") + Math.abs(v).toFixed(2);
const dollars = (v) => "$" + Math.abs(v || 0).toFixed(2);
const cents = (v) => (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "¢";
const compact = (n) => n >= 10000 ? (n / 1000).toFixed(1) + "K" : n.toLocaleString();
const ago = (m) => m === null || m === undefined ? "at an unknown time"
  : m < 1 ? "under a minute ago" : m < 120 ? `${Math.round(m)} min ago`
  : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

// ── tooltip ──
const tip = document.getElementById("tip");
function showTip(x, y, rows, title) {
  tip.replaceChildren();
  if (title) tip.appendChild(el("div", {class: "n"}, title));
  for (const r of rows) {
    const row = el("div", {class: "row"});
    const left = el("span", {style: "display:inline-flex;align-items:center;gap:6px"});
    if (r.color) left.appendChild(el("span", {class: "key", style: `background:${r.color}`}));
    left.appendChild(el("span", {class: "n"}, r.name));
    row.appendChild(el("span", {class: "v"}, r.value));
    row.appendChild(left);
    tip.appendChild(row);
  }
  tip.style.display = "block";
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = Math.min(window.innerWidth - w - 8, x + 14) + "px";
  tip.style.top = Math.max(8, y - h - 10) + "px";
}
function hideTip() { tip.style.display = "none"; }

// ── KPI row ──
function tile(label, value, note, opts) {
  const c = el("div", {class: "card" + (opts && opts.hero ? " hero" : "")
                                      + (opts && opts.wide ? " wide" : "")});
  c.appendChild(el("div", {class: "label"}, label));
  const v = el("div", {class: "value"});
  if (opts && opts.node) v.appendChild(opts.node); else v.textContent = value;
  if (opts && opts.of) v.appendChild(el("span", {class: "of"}, opts.of));
  c.appendChild(v);
  if (note) c.appendChild(el("div", {class: "note"}, note));
  return c;
}
// ── a panel that could not be read says so, in words - never an empty panel in silence ──
const PANEL = {rules: "Rules, P&L and open bets", traders: "Paper traders", regime: "News regime",
               health: "System health", nominations: "Scanner nominations"};
function renderNotices() {
  const box = document.getElementById("notices"); box.replaceChildren();
  for (const e of DATA.errors || []) {
    const chip = el("div", {class: "chip"}), txt = el("span");
    chip.appendChild(icon("alert", "var(--warning)"));
    txt.appendChild(el("strong", {}, PANEL[e.panel] || e.panel));
    let said = " · " + e.error;
    if (e.panel === "rules")           // the scored block: the last good one, or nothing yet
      said += DATA.scored_at ? ` · showing the last good values, scored ${DATA.scored_at.slice(11, 19)}Z`
                             : " · nothing has been scored yet";
    txt.appendChild(document.createTextNode(said));
    chip.appendChild(txt);
    box.appendChild(chip);
  }
}
function unknownTile(label, note, opts) {       // unknown is a word and an icon, never a zero
  const s = el("span", {class: "status"});
  s.appendChild(icon("dash", "var(--neutral-cell)"));
  s.appendChild(document.createTextNode("Unknown"));
  return tile(label, "", note, {...(opts || {}), node: s});
}
function renderKpis() {
  const k = DATA.kpi, box = document.getElementById("kpis");
  box.replaceChildren();
  if (!k) {                     // nothing scored yet: "0 of 0" would be unknown shown as a number
    box.appendChild(unknownTile("Rules that have earned an edge",
      "The ledgers could not be read, so nothing is scored - see the notice above. "
      + "Live money stays off.", {hero: true}));
    for (const label of ["Paper P&L, all rules", "Evidence collected", "Open bets"])
      box.appendChild(unknownTile(label, "not read yet"));
  } else {
    box.appendChild(tile("Rules that have earned an edge", String(k.edge),
      k.edge ? "Survived 100+ events and the family correction - still paper until you decide."
             : "None yet. That is the honest number, and the reason live money stays off.",
      {hero: true, of: ` of ${k.rules}`}));
    box.appendChild(tile("Paper P&L, all rules", usd(k.net),
      `crypto ${usd(k.net_crypto)} · sleeves ${usd(k.net_sleeves)} · won ${dollars(k.won)}, `
      + `lost ${dollars(k.lost)}`));
    box.appendChild(tile("Evidence collected", compact(k.windows + k.events),
      `${compact(k.windows)} crypto windows · ${compact(k.events)} sleeve events`));
    box.appendChild(tile("Open bets", String(k.open),
      `crypto ${k.open_crypto} · sleeves ${k.open_sleeves}`
      + (k.open_retired ? ` (+${k.open_retired} on retired rules)` : "") + " - not settled yet"));
  }
  const rg = DATA.regime.current, meta = REGIME[rg.regime] || REGIME.unknown;
  const st = el("span", {class: "status"});
  st.appendChild(icon(meta.icon, meta.color));
  st.appendChild(document.createTextNode(meta.label));
  box.appendChild(tile("News regime now", "", `size ×${rg.mult ?? 0.5}`, {node: st}));
  // the verdict AND its reason: the first failing check is the first thing the tile says
  const hl = DATA.health, ok = hl.state === "healthy";
  const hs = el("span", {class: "status"});
  hs.appendChild(icon(ok ? "check" : (hl.state === "unknown" ? "dash" : "cross"),
                      ok ? "var(--good)" : (hl.state === "unknown" ? "var(--neutral-cell)"
                                                                    : "var(--critical)")));
  hs.appendChild(document.createTextNode(ok ? "Healthy" : hl.state === "stale" ? "Stale"
                                         : hl.state === "unknown" ? "Unknown" : "Unhealthy"));
  const ht = tile("System health", "", null, {node: hs, wide: true});
  const fail1 = hl.lines.find((l) => l.startsWith("[FAIL]")), stale = hl.state === "stale";
  const reason = hl.state === "unknown" ? (hl.lines[0] || "no watchdog verdict yet")
    : stale ? `the watchdog last ran ${ago(hl.age_min)} - it runs every 15 min`
    : fail1 ? fail1.replace(/^\[FAIL\]\s*/, "")
    : hl.state === "unhealthy" ? "unhealthy, but no failing check was logged - see logs/healthcheck.log"
    : hl.warns ? "nothing failing" : "every check passed";
  const rn = el("div", {class: "reason"}, reason);
  rn.title = reason;
  ht.appendChild(rn);
  const facts = [];
  if (hl.fails && (stale || hl.fails > 1)) facts.push(`${hl.fails} failing: ${hl.failing.join(", ")}`);
  if (hl.warns) facts.push(plural(hl.warns, "warning"));
  if (hl.ts && !stale) facts.push(`watchdog ${hl.ts.slice(11, 16)}Z, ${ago(hl.age_min)}`);
  const hn = el("div", {class: "note"}, facts.join(" · "));
  if (hl.lines.length && hl.state !== "unknown") {
    if (facts.length) hn.appendChild(document.createTextNode(" · "));
    hn.appendChild(el("a", {href: "#health"}, "details"));
  }
  ht.appendChild(hn);
  box.appendChild(ht);
}

// ── is each paper trader still working? (live - not the watchdog's 15-min verdict) ──
const FRESH = {good: ["check", "var(--good)"], warning: ["alert", "var(--warning)"],
               missing: ["alert", "var(--warning)"], failing: ["cross", "var(--critical)"]};
function renderTraders() {
  const box = document.getElementById("traders"); box.replaceChildren();
  for (const t of DATA.traders || []) {
    const [kind, color] = FRESH[t.status] || ["dash", "var(--neutral-cell)"];
    const chip = el("div", {class: "chip"}), txt = el("span");
    chip.appendChild(icon(kind, color));
    txt.appendChild(el("strong", {}, t.name));
    txt.appendChild(document.createTextNode(" · " + (t.status === "missing" ? t.note
      : `${t.what} ${ago(t.age_min)}` + (t.note ? ` · ${t.note}` : ""))));
    chip.appendChild(txt);
    box.appendChild(chip);
  }
}

// ── equity chart: 2 series, one axis, crosshair tooltip ──
const SERIES = [{key: "crypto", name: "Crypto 15-min", color: "var(--s1)"},
                {key: "sleeves", name: "Sleeves", color: "var(--s2)"}];
function niceTicks(lo, hi, n) {
  const span = hi - lo || 1, raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
  // ticks must ENCLOSE the data: floor the bottom AND ceil the top (rounding the top down
  // let the line run above the plot - caught by rendering it and looking)
  const out = [], top = Math.ceil(hi / step) * step;
  for (let v = Math.floor(lo / step) * step; v <= top + 1e-9; v += step) out.push(+v.toFixed(10));
  return out;
}
function renderEquity() {
  const box = document.getElementById("eq-chart"), legend = document.getElementById("eq-legend");
  box.replaceChildren(); legend.replaceChildren();
  if (!DATA.equity) {
    box.appendChild(el("div", {class: "empty"}, "Paper P&L could not be read - see the notice at the top of the page."));
    document.getElementById("eq-table").replaceChildren();
    return;
  }
  const series = SERIES.filter(s => VENUE === "all" || VENUE === s.key)
                       .map(s => ({...s, pts: DATA.equity[s.key] || []}))
                       .filter(s => s.pts.length);
  if (!series.length) { box.appendChild(el("div", {class: "empty"}, "No closed paper trades yet.")); return; }
  if (series.length > 1) for (const s of series) {
    const sp = el("span"); sp.appendChild(el("span", {class: "key", style: `background:${s.color}`}));
    sp.appendChild(document.createTextNode(s.name)); legend.appendChild(sp);
  }
  const days = [...new Set(series.flatMap(s => s.pts.map(p => p[0])))].sort();
  const val = (s, d) => { let v = null; for (const p of s.pts) { if (p[0] <= d) v = p[1]; else break; } return v; };
  const all = series.flatMap(s => s.pts.map(p => p[1])).concat([0]);
  const ticks = niceTicks(Math.min(...all), Math.max(...all), 4);
  // wide: room for "Crypto 15-min -$808.09" so no end label is ever clipped; narrow
  // (phones): end dots only - the legend, crosshair tooltip and table view carry the rest
  const W = Math.max(300, box.clientWidth || 800), narrow = W < 560;
  const H = 240, L = 56, R = narrow ? 16 : 168, T = 10, B = 26;
  const x = (i) => L + (days.length < 2 ? 0 : i * (W - L - R) / (days.length - 1));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const y = (v) => T + (H - T - B) * (1 - (v - y0) / ((y1 - y0) || 1));
  const s = svg("svg", {width: W, height: H, role: "img",
                        "aria-label": "Cumulative paper P&L by day"});
  for (const t of ticks) {
    s.appendChild(svg("line", {x1: L, x2: W - R, y1: y(t), y2: y(t),
      stroke: t === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1}));
    const lab = svg("text", {x: L - 8, y: y(t) + 4, "text-anchor": "end"});
    lab.textContent = (t < 0 ? "-$" : "$") + Math.abs(t).toLocaleString();
    s.appendChild(lab);
  }
  for (const i of [0, Math.floor((days.length - 1) / 2), days.length - 1]) {
    const lab = svg("text", {x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : i === days.length - 1 ? "end" : "middle"});
    lab.textContent = days[i].slice(5); s.appendChild(lab);
  }
  const ends = [];
  for (const sr of series) {
    const d = days.map((day, i) => { const v = val(sr, day); return v === null ? null : [x(i), y(v)]; })
                  .filter(Boolean).map((p, j) => (j ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    s.appendChild(svg("path", {d, fill: "none", stroke: sr.color, "stroke-width": 2,
                               "stroke-linejoin": "round", "stroke-linecap": "round"}));
    // cumulative P&L carries forward on days with no closes, so the series ends at the
    // RIGHT EDGE with its latest value - never mid-chart on top of its own line
    const lastV = val(sr, days[days.length - 1]);
    ends.push({sr, xy: [x(days.length - 1), y(lastV)], v: lastV});
  }
  ends.sort((a, b) => a.xy[1] - b.xy[1]);
  let prevY = -1e9;
  for (const e of ends) {
    s.appendChild(svg("circle", {cx: e.xy[0], cy: e.xy[1], r: 4, fill: e.sr.color,
                                 stroke: "var(--surface)", "stroke-width": 2}));
    if (narrow) continue;
    const ly = Math.max(e.xy[1] + 4, prevY + 14); prevY = ly;
    if (Math.abs(ly - e.xy[1] - 4) > 2)
      s.appendChild(svg("line", {x1: e.xy[0] + 6, x2: e.xy[0] + 10, y1: e.xy[1], y2: ly - 4,
                                 stroke: "var(--axis)", "stroke-width": 1}));
    const t = svg("text", {x: e.xy[0] + 12, y: ly});
    t.style.fill = "var(--ink-2)"; t.textContent = `${e.sr.name} ${usd(e.v)}`;
    s.appendChild(t);
  }
  const cross = svg("line", {y1: T, y2: H - B, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden"});
  s.appendChild(cross);
  const hit = svg("rect", {x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent", tabindex: 0});
  const at = (cx) => Math.max(0, Math.min(days.length - 1, Math.round((cx - L) / ((W - L - R) / Math.max(1, days.length - 1)))));
  function show(i, px, py) {
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    showTip(px, py, series.map(sr => ({name: sr.name, color: sr.color,
      value: val(sr, days[i]) === null ? "-" : usd(val(sr, days[i]))})), days[i]);
  }
  hit.addEventListener("pointermove", (ev) => { const r = s.getBoundingClientRect(); show(at(ev.clientX - r.left), ev.clientX, ev.clientY); });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  hit.addEventListener("focus", () => { const r = s.getBoundingClientRect(); show(days.length - 1, r.left + x(days.length - 1), r.top + 20); });
  hit.addEventListener("blur", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });
  s.appendChild(hit);
  box.appendChild(s);
  const tb = el("table"), head = el("tr");
  head.appendChild(el("th", {}, "Day"));
  for (const sr of series) head.appendChild(el("th", {class: "num"}, sr.name));
  tb.appendChild(head);
  for (const d of days.slice().reverse()) {
    const tr = el("tr"); tr.appendChild(el("td", {}, d));
    for (const sr of series) { const v = val(sr, d); tr.appendChild(el("td", {class: "num"}, v === null ? "-" : usd(v))); }
    tb.appendChild(tr);
  }
  document.getElementById("eq-table").replaceChildren(tb);
}

// ── scoreboard ──
function spark(points) {
  const W = 96, H = 24, s = svg("svg", {width: W, height: H, "aria-hidden": "true"});
  if (!points || points.length < 2) return s;
  const lo = Math.min(0, ...points), hi = Math.max(0, ...points), span = (hi - lo) || 1;
  const px = (i) => 2 + i * (W - 8) / (points.length - 1), py = (v) => 2 + (H - 4) * (1 - (v - lo) / span);
  s.appendChild(svg("line", {x1: 2, x2: W - 6, y1: py(0), y2: py(0), stroke: "var(--grid)", "stroke-width": 1}));
  s.appendChild(svg("path", {d: points.map((v, i) => (i ? "L" : "M") + px(i).toFixed(1) + " " + py(v).toFixed(1)).join(" "),
                             fill: "none", stroke: "var(--deemph)", "stroke-width": 1.5}));
  s.appendChild(svg("circle", {cx: px(points.length - 1), cy: py(points[points.length - 1]), r: 3,
                               fill: "var(--s1)", stroke: "var(--surface)", "stroke-width": 1.5}));
  return s;
}
function ciBar(st, dom) {
  const W = 120, H = 16, s = svg("svg", {width: W, height: H, "aria-hidden": "true"});
  if (st.n < 2) return s;
  const x = (v) => 4 + (W - 8) * (Math.max(-dom, Math.min(dom, v)) + dom) / (2 * dom);
  s.appendChild(svg("line", {x1: x(0), x2: x(0), y1: 1, y2: H - 1, stroke: "var(--axis)", "stroke-width": 1}));
  s.appendChild(svg("line", {x1: x(st.lo), x2: x(st.hi), y1: H / 2, y2: H / 2, stroke: "var(--muted)",
                             "stroke-width": 2, "stroke-linecap": "round"}));
  s.appendChild(svg("circle", {cx: x(st.mean), cy: H / 2, r: 4, fill: "var(--s1)",
                               stroke: "var(--surface)", "stroke-width": 2}));
  return s;
}
function renderRules() {
  const t = document.getElementById("rules"); t.replaceChildren();
  if (!DATA.rules) {            // not read: one cell that wraps, no header row to scroll past
    const tr = el("tr");
    tr.appendChild(el("td", {class: "empty", style: "white-space:normal"},
      "The rules could not be read, so none is scored - see the notice at the top of the page."));
    t.appendChild(tr); return;
  }
  const rows = DATA.rules.filter(r => VENUE === "all" || r.venue === VENUE);
  const head = el("tr");
  for (const [h, cls] of [["Rule", ""], ["Verdict", ""], ["Events", "num"], ["Win rate", "num"],
                          ["Edge/contract", "num"], ["95% interval", ""], ["Net $", "num"],
                          ["Trend", ""], ["All eras", "num"]]) head.appendChild(el("th", {class: cls}, h));
  t.appendChild(head);
  if (!rows.length) { const tr = el("tr"); const td = el("td", {colspan: 9, class: "empty"},
    "No rules here yet - register sleeve probes or wait for the first closed trades."); tr.appendChild(td); t.appendChild(tr); return; }
  const dom = Math.max(0.05, ...rows.filter(r => r.decision.n >= 2).flatMap(r => [Math.abs(r.decision.lo), Math.abs(r.decision.hi)]).filter(isFinite).map(v => Math.min(v, 1)));
  for (const r of rows) {
    const d = r.decision, a = r.all, tr = el("tr");
    const name = el("td"); name.appendChild(el("div", {class: "rule"}, r.rule));
    const sub = el("div", {class: "venue"}, r.venue === "crypto" ? "crypto 15-min" : (r.series || "sleeve"));
    if (r.open) {         // bets placed and not settled yet: working, not idle
      sub.appendChild(document.createTextNode(" · "));
      sub.appendChild(el("span", {class: "opencount"}, `${r.open} open`));
    }
    name.appendChild(sub);
    tr.appendChild(name);
    const vd = el("td"), vs = el("span", {class: "status", style: "font-weight:500"});
    const meta = STATUS[r.status];
    vs.appendChild(icon({good: "check", warning: "alert", critical: "cross", neutral: "dash"}[r.status],
                        r.status === "neutral" ? "var(--neutral-cell)" : meta.color));
    vs.appendChild(document.createTextNode(r.verdict)); vd.appendChild(vs); tr.appendChild(vd);
    tr.appendChild(el("td", {class: "num"}, String(d.n)));
    tr.appendChild(el("td", {class: "num"}, d.wr === null ? "-" : (d.wr * 100).toFixed(0) + "%"));
    tr.appendChild(el("td", {class: "num"}, d.n ? cents(d.mean) : "-"));
    const ci = el("td"); ci.appendChild(ciBar(d, dom));
    if (d.n >= 2) ci.title = `${cents(d.lo)} to ${cents(d.hi)} per contract per event`;
    tr.appendChild(ci);
    const net = el("td", {class: "num"}, usd(d.net));
    const m = r.money;
    if (m && (m.won || m.lost)) {                  // the shape the net hides, stacked narrow
      net.appendChild(el("div", {class: "sub"}, `won ${dollars(m.won)}`));
      const lost = el("div", {class: "sub"}, `lost ${dollars(m.lost)}`);
      lost.title = `${m.losses} losing bet${m.losses === 1 ? "" : "s"}`;   // the table has no room
      net.appendChild(lost);
    }
    tr.appendChild(net);
    const sp = el("td"); sp.appendChild(spark(r.spark)); tr.appendChild(sp);
    tr.appendChild(el("td", {class: "num"}, a.n ? `${cents(a.mean)} · ${a.n}` : "-"));
    t.appendChild(tr);
  }
}

// ── regime strip ──
function renderRegime() {
  const rg = DATA.regime, cur = rg.current;
  document.getElementById("rg-cap").textContent = cur.why ? `Now: ${cur.why}` : "";
  const legend = document.getElementById("rg-legend"); legend.replaceChildren();
  for (const k of ["good", "cautious", "unknown"]) {
    const sp = el("span"); sp.appendChild(icon(REGIME[k].icon, REGIME[k].color));
    sp.appendChild(document.createTextNode(REGIME[k].label)); legend.appendChild(sp);
  }
  const box = document.getElementById("rg-chart"); box.replaceChildren();
  if (!rg.cells.length) { box.appendChild(el("div", {class: "empty"},
    rg.unread ? "The news regime could not be read." : "The news agent has not run yet.")); return; }
  const days = [...new Set(rg.cells.map(c => c.d))].sort().slice(-14);
  const gap = 2, L = 44, T = 4, avail = (box.clientWidth || 380) - L;
  const cell = Math.max(6, Math.min(12, Math.floor(avail / 24) - gap));   // fits a phone
  const W = L + 24 * (cell + gap), H = T + days.length * (cell + gap) + 16;
  const s = svg("svg", {width: W, height: H, role: "img", "aria-label": "Hourly news regime, last 14 days"});
  const map = new Map(rg.cells.map(c => [c.d + "|" + c.h, c.r]));
  days.forEach((d, i) => {
    const lab = svg("text", {x: L - 6, y: T + i * (cell + gap) + cell - 2, "text-anchor": "end"});
    lab.textContent = d.slice(5); s.appendChild(lab);
    for (let h = 0; h < 24; h++) {
      const r = map.get(d + "|" + h);
      const rect = svg("rect", {x: L + h * (cell + gap), y: T + i * (cell + gap), width: cell, height: cell, rx: 2,
                                fill: r ? REGIME[r].color : "transparent",
                                stroke: r ? "none" : "var(--grid)", "stroke-width": 1});
      if (r) {
        rect.addEventListener("pointermove", (ev) => showTip(ev.clientX, ev.clientY,
          [{name: `${String(h).padStart(2, "0")}:00 ET`, value: REGIME[r].label}], d));
        rect.addEventListener("pointerleave", hideTip);
      }
      s.appendChild(rect);
    }
  });
  for (const h of [0, 6, 12, 18]) {
    const lab = svg("text", {x: L + h * (cell + gap), y: H - 2}); lab.textContent = String(h).padStart(2, "0"); s.appendChild(lab);
  }
  box.appendChild(s);
  const tb = el("table"), hd = el("tr");
  for (const h of ["Day (ET)", "Good hours", "Cautious hours", "Unknown hours"]) hd.appendChild(el("th", {}, h));
  tb.appendChild(hd);
  for (const d of days.slice().reverse()) {
    const tr = el("tr"); tr.appendChild(el("td", {}, d));
    for (const k of ["good", "cautious", "unknown"])
      tr.appendChild(el("td", {class: "num"}, String(rg.cells.filter(c => c.d === d && c.r === k).length)));
    tb.appendChild(tr);
  }
  document.getElementById("rg-table").replaceChildren(tb);
}

// ── health + nominations ──
// the payload lists FAIL lines first, then WARN, then notes (the stale warning), OK last
const CHECK = {FAIL: ["cross", "var(--critical)", "Fail"], WARN: ["alert", "var(--warning)", "Warn"],
               OK: ["check", "var(--good)", "OK"], "": ["alert", "var(--warning)", ""]};
function checkItem(text, tag) {
  const [kind, color, word] = CHECK[tag], li = el("li"), s = el("span");
  li.appendChild(icon(kind, color));
  if (word) s.appendChild(el("strong", {}, word + " "));
  s.appendChild(document.createTextNode(text));
  li.appendChild(s);
  return li;
}
function renderHealth() {
  const h = DATA.health;
  document.getElementById("hl-cap").textContent = h.ts
    ? `Watchdog verdict at ${h.ts.slice(0, 16)}Z (${ago(h.age_min)}); it runs every 15 min.` : "";
  const ul = document.getElementById("hl"), okList = document.getElementById("hl-ok-list");
  ul.replaceChildren(); okList.replaceChildren();
  for (const line of h.lines) {
    const m = line.match(/^\[(FAIL|WARN|OK)\s*\]\s*/), tag = m ? m[1] : "";
    (tag === "OK" ? okList : ul).appendChild(checkItem(m ? line.slice(m[0].length) : line, tag));
  }
  if (!h.lines.length && h.state === "healthy") ul.appendChild(checkItem("Every check passed.", "OK"));
  const passing = okList.children.length, det = document.getElementById("hl-ok");
  det.hidden = !passing;
  document.getElementById("hl-ok-sum").textContent = plural(passing, "passing check");
}
function renderNoms() {
  const t = document.getElementById("noms"); t.replaceChildren();
  const hd = el("tr");
  for (const [h, c] of [["Sleeve", ""], ["Band", ""], ["Side", ""], ["Price", ""], ["Events out", "num"],
                        ["$/event out", "num"], ["Trade-confirmed", "num"], ["Verdict", ""]]) hd.appendChild(el("th", {class: c}, h));
  t.appendChild(hd);
  if (!DATA.nominations.length) {
    const tr = el("tr"); tr.appendChild(el("td", {colspan: 8, class: "empty"},
      "No nominations - run the sleeve scanner (discover, backfill, screen)."));
    t.appendChild(tr); return;
  }
  for (const n of DATA.nominations) {
    const tr = el("tr");
    for (const v of [n.series, n.band, n.side, n.bucket]) tr.appendChild(el("td", {}, String(v)));
    tr.appendChild(el("td", {class: "num"}, String(n.n_out)));
    tr.appendChild(el("td", {class: "num"}, cents(n.mean_out)));
    tr.appendChild(el("td", {class: "num"}, n.mean_out_traded === null ? "none" : cents(n.mean_out_traded)));
    const vd = el("td"), clean = n.verdict === "NOMINATED", s = el("span", {class: "status", style: "font-weight:500"});
    s.appendChild(icon(clean ? "check" : "alert", clean ? "var(--good)" : "var(--warning)"));
    s.appendChild(document.createTextNode(clean ? "Clean - register it" : "Phantom risk - not traded"));
    vd.appendChild(s); tr.appendChild(vd); t.appendChild(tr);
  }
}

function render() {
  if (!DATA) return;
  const live = document.getElementById("live"); live.replaceChildren();
  live.appendChild(icon("dash", "var(--neutral-cell)"));
  live.appendChild(document.createTextNode(`Live money ${DATA.live_money} - paper only`));
  document.getElementById("gen").textContent = `updated ${DATA.generated.slice(11, 19)}Z · refreshes every minute`;
  renderNotices(); renderKpis(); renderTraders(); renderEquity(); renderRules(); renderRegime();
  renderHealth(); renderNoms();
}
async function load() {
  const app = document.getElementById("app");
  app.classList.add("loading");                       // hold the frame; no skeleton flash
  try {
    const r = await fetch("/api/command", {cache: "no-store"});
    const d = await r.json();
    if (d.error) throw new Error(d.error);
    DATA = d; render();
  } catch (e) {
    document.getElementById("gen").textContent = `refresh failed (${String(e).slice(0, 80)}) - showing last data`;
  } finally { app.classList.remove("loading"); }
}
document.querySelectorAll("#venue button").forEach(b => b.addEventListener("click", () => {
  VENUE = b.dataset.v;
  document.querySelectorAll("#venue button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  render();
}));
for (const [btn, chart, table] of [["eq-toggle", "eq-chart", "eq-table"], ["rg-toggle", "rg-chart", "rg-table"]]) {
  document.getElementById(btn).addEventListener("click", (ev) => {
    const t = document.getElementById(table), c = document.getElementById(chart), showing = t.hidden;
    t.hidden = !showing; c.hidden = showing; ev.target.textContent = showing ? "Show chart" : "Show table";
  });
}
window.addEventListener("resize", () => { clearTimeout(window._rz); window._rz = setTimeout(render, 150); });
load(); setInterval(load, 60000);
</script>
</body>
</html>
"""
