#!/usr/bin/env python3
"""diagnostics - the model-integrity layer: A/B/C/D self-checks over the REAL files.

Module selftests prove code paths work on fixtures. This file asks the other
question: is the DATA the system is accumulating, and the ledger it is writing,
internally consistent RIGHT NOW? Every check here is one a silent breakage could
fail while all processes look alive - the same philosophy as healthcheck, one layer
deeper (healthcheck runs these every watchdog cycle via check_model_integrity).

DESIGN RULE - RECENT EVIDENCE FAILS, HISTORY INFORMS. Both files are append-only and
never rotated, so a check that fails on anything EVER written fails forever after one
bad day - and a watchdog that is permanently red can no longer report a new outage.
The first version of this file did exactly that (one 30h collector gap in August
would have turned the watchdog red for the life of the file; caught by adversarial
review before it reached the host). Every file check therefore judges the last
RECENT_HOURS of the file's OWN timeline: a defect fails the watchdog on the day it
appears, then moves into the detail line as history - still printed by
`diagnostics.py run`, never again blinding the alarm. The same window stops a large
healthy history from diluting a fresh feed break below the bad-row threshold.

The four layers, and the bug class each one exists to catch:

  A. INPUT VALIDATION   asks missing (a renamed feed field - the orderbook_fp class),
                        prices outside [0,1], crossed books, non-positive spot/strike,
                        unparseable lines, contradictory settlements. Catches: a feed
                        change silently writing rows every loader skips - forever.
  B. CONSERVATION       double-entry on the paper ledger in EXACT integer units of
                        $0.0001 (Kalshi ticks in tenths of a cent - see the captured
                        orderbook_fp row with 0.9070 levels - so cents are not exact
                        enough and floats never were). Every close is recomputed from
                        its OWN OPEN's price, side, fee and contracts, and the close's
                        copies of price and side must match the open. Torn/unparseable
                        ledger lines fail structure: a lost open never settles.
  C. IDENTITY/INVERSION the pricing model must satisfy what it claims: one half at the
                        money (zero drift), monotone in strike, bounded, and invariant
                        under the sigma*sqrt(T) rescaling (sigma,4T) == (2*sigma,T).
                        Ledger closes must be internally consistent (won follows from
                        result vs the open's side) AND externally consistent (result
                        matches the collector's own settlement for that market).
  D. EDGE CASES         zero/None inputs return defined values, never raise; collection
                        gaps >= 24h surface on the day they close. (An outage STILL in
                        progress is healthcheck's [data] staleness check - 10 minutes.)

Every detector is also proven to fire: `selftest` plants one specific defect per
check and asserts exactly the right alarm goes off - a detector that has never been
seen to catch its target is decoration.

  py scripts/diagnostics.py            # run all checks against the live files
  py scripts/diagnostics.py selftest   # fixtures + planted corruptions, no data
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from lib.binary_justify import fair_p_above, kalshi_taker_fee  # noqa: E402
from shadow_book import liftable_depth  # noqa: E402

STAGE0 = Path(os.environ.get("STAGE0_LOG") or (ROOT / "data" / "stage0_crypto.jsonl"))
LEDGER = Path(os.environ.get("PAPER_CRYPTO_LEDGER")
              or (ROOT / "data" / "paper_crypto15.jsonl"))

RECENT_HOURS = 24.0          # the window that can turn the watchdog red
MAX_BAD_ROW_RATE = 0.01      # of RECENT stage0 rows - never diluted by old history
GAP_FAIL_S = 86400.0         # a collection hole this long is a failure, not a nap
U = 10000                    # exact unit: integer ten-thousandths of a dollar ($1e-4)


# ── helpers ───────────────────────────────────────────────────────────────────

def _num(v) -> float | None:
    """A finite number, or None. Strings, NaN and infinities are not prices."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _u(x) -> int | None:
    """Dollars -> exact integer units, refusing values off the $0.0001 grid."""
    v = _num(x)
    if v is None:
        return None
    v *= U
    r = round(v)
    return r if abs(v - r) < 1e-3 else None


def _ts(r: dict) -> float | None:
    t = r.get("ts")
    if not t:
        return None
    try:
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _recent_start(rows: list[dict]) -> int:
    """Index of the first row inside the recent window: the last RECENT_HOURS of the
    file's OWN timeline (its newest timestamp, not the wall clock - so an old file
    replayed later, or a collector that just restarted, is judged on its own last
    day). Rows without a parseable ts (torn lines) are positioned by file order. A
    file with no timestamps at all is entirely recent."""
    stamps: list[tuple[int, float]] = []
    for i, r in enumerate(rows):
        t = _ts(r)
        if t is not None:
            stamps.append((i, t))
    if not stamps:
        return 0
    cutoff = max(t for _, t in stamps) - RECENT_HOURS * 3600.0
    return next(i for i, t in stamps if t >= cutoff)


def _split(problems: list[tuple[int, str]], start: int):
    return ([p for p in problems if p[0] >= start], [p for p in problems if p[0] < start])


def _fmt(recent: list, hist: list, noun: str) -> str:
    """Actionable part FIRST: the watchdog log truncates, and a failure line that
    loses its offender to truncation sends whoever reads it the wrong way."""
    parts = []
    if recent:
        parts.append(f"{len(recent)} RECENT {noun} - first: {recent[0][1]}")
    if hist:
        parts.append(f"history: {len(hist)} older {noun} (first: {hist[0][1]})")
    return "; ".join(parts)


def _load(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                rows.append(d if isinstance(d, dict) else {"_unparseable": True})
            except json.JSONDecodeError:
                rows.append({"_unparseable": True})
    return rows


# ── A: input validation ───────────────────────────────────────────────────────

def _obs_problem(r: dict) -> str | None:
    ya, na = _num(r.get("yes_ask")), _num(r.get("no_ask"))
    if ya is None or na is None:
        return "ask missing/non-numeric - every loader silently skips this row"
    for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
        v = r.get(k)
        if v is None:
            continue
        x = _num(v)
        if x is None or not (0.0 <= x <= 1.0):
            return f"{k}={v!r} outside [0,1]"
    for k in ("spot", "strike"):
        v = r.get(k)
        if v is None:
            continue
        x = _num(v)
        if x is None or x <= 0:
            return f"{k}={v!r} not a positive number"
    if ya + na < 0.999:
        return f"crossed book (yes_ask {ya} + no_ask {na} < 1)"
    return None


def check_a1_obs_bounds(rows: list[dict]) -> tuple[bool, str]:
    """A: recent stage0 rows parse, carry both asks, and sit in bounds."""
    start = _recent_start(rows)
    problems: list[tuple[int, str]] = []
    n_recent = 0
    for i, r in enumerate(rows):
        if r.get("_unparseable"):
            n_recent += i >= start
            problems.append((i, f"line {i + 1} unparseable"))
            continue
        if r.get("t") != "obs":
            continue
        n_recent += i >= start
        why = _obs_problem(r)
        if why:
            problems.append((i, f"{r.get('ticker')}: {why}"))
    recent, hist = _split(problems, start)
    tail = _fmt(recent, hist, "bad rows")
    if n_recent == 0:
        return True, "no recent observations" + (f"; {tail}" if tail else "")
    rate = len(recent) / n_recent
    head = (f"{100 * rate:.2f}% of {n_recent} recent rows bad "
            f"(limit {100 * MAX_BAD_ROW_RATE:.0f}%)")
    return rate <= MAX_BAD_ROW_RATE, head + (f"; {tail}" if tail else "")


def check_a2_settle_consistency(rows: list[dict]) -> tuple[bool, str]:
    """A: settlements are yes/no and a market never settles both ways."""
    start = _recent_start(rows)
    seen: dict[str, str] = {}
    problems: list[tuple[int, str]] = []
    n = 0
    for i, r in enumerate(rows):
        if r.get("t") != "settle":
            continue
        n += 1
        res, tk = r.get("result"), r.get("ticker")
        if res not in ("yes", "no") or not tk:
            problems.append((i, f"{tk}: invalid result {res!r}"))
            continue
        if tk in seen and seen[tk] != res:
            problems.append((i, f"{tk} settled {seen[tk]} AND {res}"))
        seen[tk] = res
    recent, hist = _split(problems, start)
    tail = _fmt(recent, hist, "settle problems")
    return not recent, (f"{tail}; " if tail else "") + f"{n} settles"


# ── B: conservation ───────────────────────────────────────────────────────────

def check_b1_ledger_structure(rows: list[dict]) -> tuple[bool, str]:
    """B: double-entry structure - one open per (market, rule), every close after
    its open, no duplicate closes, and no torn lines (a lost open never settles)."""
    start = _recent_start(rows)
    opens: set = set()
    closes: set = set()
    problems: list[tuple[int, str]] = []
    for i, r in enumerate(rows):
        if r.get("_unparseable"):
            problems.append((i, f"line {i + 1} unparseable (torn write?)"))
            continue
        key = (r.get("ticker"), r.get("rule"))
        if r.get("t") == "open":
            if key in opens:
                problems.append((i, f"duplicate open {key[0]}/{key[1]}"))
            opens.add(key)
        elif r.get("t") == "close":
            if key not in opens:
                problems.append((i, f"orphan close {key[0]}/{key[1]}"))
            elif key in closes:
                problems.append((i, f"duplicate close {key[0]}/{key[1]}"))
            closes.add(key)
    recent, hist = _split(problems, start)
    tail = _fmt(recent, hist, "structure defects")
    return not recent, (f"{tail}; " if tail else "") + (
        f"{len(opens)} positions, {len(closes)} closed")


def check_b2_ledger_conservation(rows: list[dict]) -> tuple[bool, str]:
    """B: every close recomputes EXACTLY (integer $1e-4) from its OWN OPEN:
    win -> contracts*(1-price) - fee ; loss -> -contracts*price - fee, with the win
    decided by the close's result against the OPEN's side. The close's own price and
    side must match the open: a close rewritten to the other side with consistent
    P&L is exactly the corruption a close-only recomputation cannot see."""
    start = _recent_start(rows)
    opens: dict[tuple, dict] = {}
    problems: list[tuple[int, str]] = []
    n = s_stored = s_want = 0
    for i, r in enumerate(rows):
        key = (r.get("ticker"), r.get("rule"))
        if r.get("t") == "open":
            opens.setdefault(key, r)
            continue
        if r.get("t") != "close":
            continue
        o = opens.get(key)
        if o is None:
            continue                      # B1's finding, not B2's
        n += 1
        tag = f"{key[0]}/{key[1]}"
        o_side, o_price = o.get("side"), o.get("price")
        if r.get("side") != o_side or _u(r.get("price")) != _u(o_price):
            problems.append((i, f"{tag} close {r.get('side')}@{r.get('price')} "
                                f"!= open {o_side}@{o_price}"))
            continue
        price_u, fee_u, pnl_u = _u(o_price), _u(o.get("fee") or 0.0), _u(r.get("pnl"))
        if None in (price_u, fee_u, pnl_u):
            problems.append((i, f"{tag} off-grid value (pnl={r.get('pnl')!r})"))
            continue
        res = r.get("result")
        won = (res == o_side) if res in ("yes", "no") else bool(r.get("won"))
        c = int(_num(o.get("contracts")) or 1)
        want = (c * (U - price_u) - fee_u) if won else (-c * price_u - fee_u)
        s_stored += pnl_u
        s_want += want
        if want != pnl_u:
            problems.append((i, f"{tag} pnl {pnl_u / U:+.4f} != {want / U:+.4f} "
                                f"implied by its open"))
    recent, hist = _split(problems, start)
    tail = _fmt(recent, hist, "P&L defects")
    return not recent, (f"{tail}; " if tail else "") + (
        f"{n} closes, stored {s_stored / U:+.4f} vs recomputed {s_want / U:+.4f}")


# ── C: identity / inversion ───────────────────────────────────────────────────

def check_c1_model_identities() -> tuple[bool, str]:
    """C: the pricing model satisfies what it claims, verified by inversion."""
    probs = []
    # at the money, zero drift: one half, to the integrator's REAL accuracy
    # (measured ~2e-5 at z=0; its docstring's 1e-6 claim is optimistic)
    probs.append(("ATM == 0.5", abs(fair_p_above(100.0, 100.0, 0.01, 5.0) - 0.5) < 1e-4))
    grid = [fair_p_above(100.0, k, 0.005, 10.0) for k in (90, 95, 99, 100, 101, 105, 110)]
    probs.append(("monotone in strike", all(a >= b - 1e-12 for a, b in zip(grid, grid[1:]))))
    probs.append(("bounded [0,1]", all(0.0 <= p <= 1.0 for p in grid)))
    # inversion: sigma*sqrt(T) invariance - (sigma, 4T) must price like (2*sigma, T)
    a = fair_p_above(100.0, 103.0, 0.004, 12.0)
    b = fair_p_above(100.0, 103.0, 0.008, 3.0)
    probs.append(("sigma*sqrt(T) inversion", abs(a - b) < 1e-4))
    probs.append(("fee symmetry", all(kalshi_taker_fee(p) == kalshi_taker_fee(1 - p)
                                      for p in (0.05, 0.10, 0.30, 0.49))))
    probs.append(("fee bounds", all(0.0 <= kalshi_taker_fee(p) <= 0.02
                                    for p in (0.01, 0.25, 0.5, 0.75, 0.99))))
    failed = [name for name, ok in probs if not ok]
    return not failed, ("all identities hold" if not failed else "FAILED: " + ", ".join(failed))


def check_c2_ledger_field_identity(rows: list[dict],
                                   stage0_rows: list[dict]) -> tuple[bool, str]:
    """C: closes are internally AND externally consistent: result is yes/no, it
    matches the collector's own settlement for that market (when the collector has
    one), and won == (result == the OPEN's side)."""
    start = _recent_start(rows)
    by_tk: dict[str, set] = {}
    for r in stage0_rows:
        if r.get("t") == "settle" and r.get("result") in ("yes", "no"):
            by_tk.setdefault(r.get("ticker"), set()).add(r["result"])
    truth = {tk: next(iter(s)) for tk, s in by_tk.items() if len(s) == 1}  # A2 owns
    opens: dict[tuple, dict] = {}                                          # conflicts
    problems: list[tuple[int, str]] = []
    n = 0
    for i, r in enumerate(rows):
        key = (r.get("ticker"), r.get("rule"))
        if r.get("t") == "open":
            opens.setdefault(key, r)
            continue
        if r.get("t") != "close":
            continue
        n += 1
        tag = f"{key[0]}/{key[1]}"
        res = r.get("result")
        if res not in ("yes", "no"):
            problems.append((i, f"{tag} result {res!r} is not yes/no"))
            continue
        t = truth.get(key[0])
        if t is not None and t != res:
            problems.append((i, f"{tag} ledger says {res}, collector says {t}"))
            continue
        side = (opens.get(key) or r).get("side")
        if bool(r.get("won")) != (res == side):
            problems.append((i, f"{tag} won={r.get('won')} but result={res}, side={side}"))
    recent, hist = _split(problems, start)
    tail = _fmt(recent, hist, "inconsistent closes")
    return not recent, (f"{tail}; " if tail else "") + f"{n} closes checked"


# ── D: edge cases ─────────────────────────────────────────────────────────────

def check_d1_edge_cases() -> tuple[bool, str]:
    """D: degenerate inputs return defined values - never raise, never NaN."""
    try:
        cases = [
            fair_p_above(100.0, 90.0, 0.0, 5.0) == 1.0,     # zero vol, in the money
            fair_p_above(100.0, 110.0, 0.0, 5.0) == 0.0,    # zero vol, out
            fair_p_above(100.0, 90.0, 0.01, 0.0) == 1.0,    # expired
            fair_p_above(0.0, 90.0, 0.01, 5.0) in (0.0, 1.0),  # degenerate spot
            kalshi_taker_fee(0.0) == 0.0 and kalshi_taker_fee(1.0) == 0.0,
            liftable_depth(None, "yes", 0.5) is None,
            liftable_depth({}, "yes", 0.5) is None,
            liftable_depth({"no": "garbage"}, "yes", 0.5) is None,
            liftable_depth({"no": [["x", "y"], None, [0.9, 5]]}, "yes", 0.2) == 5.0,
            _u("garbage") is None and _u(float("nan")) is None,
        ]
        bad = sum(1 for c in cases if not c)
        return bad == 0, f"{len(cases)} degenerate inputs, {bad} misbehaved"
    except Exception as e:  # noqa: BLE001 - raising IS the failure being tested for
        return False, f"raised {type(e).__name__}: {e}"


def check_d2_collector_gaps(rows: list[dict]) -> tuple[bool, str]:
    """D: collection holes >= 24h fail on the day they CLOSE, then become history.
    (A hole still open has no closing row yet - healthcheck's staleness check owns
    that case and fires within 10 minutes.)"""
    start = _recent_start(rows)
    last: dict[str, float] = {}
    problems: list[tuple[int, str]] = []
    for i, r in enumerate(rows):
        if r.get("t") != "obs":
            continue
        s, t = r.get("series"), _ts(r)
        if t is None:
            continue
        if s in last and t - last[s] >= GAP_FAIL_S:
            end = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            problems.append((i, f"{s} {(t - last[s]) / 3600:.1f}h hole ending {end}Z"))
        last[s] = max(t, last.get(s, t))
    recent, hist = _split(problems, start)
    return not recent, _fmt(recent, hist, "collection holes >= 24h") or \
        "no collection hole >= 24h"


# ── runner ────────────────────────────────────────────────────────────────────

def run_all(stage0_rows: list[dict], ledger_rows: list[dict]) -> list[tuple[str, bool, str]]:
    return [
        ("A1 obs bounds", *check_a1_obs_bounds(stage0_rows)),
        ("A2 settle consistency", *check_a2_settle_consistency(stage0_rows)),
        ("B1 ledger structure", *check_b1_ledger_structure(ledger_rows)),
        ("B2 ledger conservation", *check_b2_ledger_conservation(ledger_rows)),
        ("C1 model identities", *check_c1_model_identities()),
        ("C2 ledger field identity", *check_c2_ledger_field_identity(ledger_rows,
                                                                     stage0_rows)),
        ("D1 edge cases", *check_d1_edge_cases()),
        ("D2 collector gaps", *check_d2_collector_gaps(stage0_rows)),
    ]


def run_diagnostics() -> int:
    print("=" * 74)
    print("MODEL-INTEGRITY DIAGNOSTICS - A/B/C/D self-checks over the live files")
    print("=" * 74)
    print(f"stage0: {STAGE0}")
    print(f"ledger: {LEDGER}")
    print(f"PASS/FAIL judges the last {RECENT_HOURS:.0f}h of each file; older defects are")
    print("listed as 'history' - visible here, but they never turn the watchdog red.")
    print()
    results = run_all(_load(STAGE0), _load(LEDGER))
    fails = 0
    for name, ok, detail in results:
        fails += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<24} {detail}")
    print("-" * 74)
    print(("ALL CHECKS PASS - the ledger conserves, the model inverts, the data is "
           "in bounds.") if fails == 0 else
          f"{fails} CHECK(S) FAILED - do not trust any report built on these files "
          f"until this is explained.")
    print("=" * 74)
    return 0 if fails == 0 else 1


# ── selftest: every detector must be seen to fire ─────────────────────────────

def _fixture() -> tuple[list[dict], list[dict]]:
    stage0 = [
        {"t": "obs", "ts": "2026-09-01T00:00:00+00:00", "series": "KXBTC15M",
         "ticker": "KXBTC15M-W1-T1", "strike": 64000.0, "mins_left": 5.0,
         "yes_bid": 0.05, "yes_ask": 0.07, "no_bid": 0.91, "no_ask": 0.95,
         "spot": 63000.0},
        {"t": "obs", "ts": "2026-09-01T00:01:00+00:00", "series": "KXBTC15M",
         "ticker": "KXBTC15M-W1-T1", "strike": 64000.0, "mins_left": 4.0,
         "yes_bid": 0.06, "yes_ask": 0.08, "no_bid": 0.90, "no_ask": 0.94,
         "spot": 63050.0},
        {"t": "settle", "ts": "2026-09-01T00:15:00+00:00", "series": "KXBTC15M",
         "ticker": "KXBTC15M-W1-T1", "result": "no"},
    ]
    ledger = [
        {"t": "open", "ticker": "KXBTC15M-W1-T1", "rule": "H2_far_strike_premium",
         "side": "yes", "price": 0.07, "contracts": 1, "fee": 0.01,
         "ts": "2026-09-01T00:00:30+00:00"},
        {"t": "close", "ticker": "KXBTC15M-W1-T1", "rule": "H2_far_strike_premium",
         "side": "yes", "price": 0.07, "result": "no", "won": False, "pnl": -0.08,
         "ts": "2026-09-01T00:15:30+00:00"},
        {"t": "open", "ticker": "KXBTC15M-W2-T1", "rule": "H1_settlement_lag",
         "side": "no", "price": 0.85, "contracts": 1, "fee": 0.01,
         "ts": "2026-09-01T00:16:00+00:00"},
        {"t": "close", "ticker": "KXBTC15M-W2-T1", "rule": "H1_settlement_lag",
         "side": "no", "price": 0.85, "result": "no", "won": True, "pnl": 0.14,
         "ts": "2026-09-01T00:31:00+00:00"},
    ]
    return stage0, ledger


def _failed(results) -> list[str]:
    return [name for name, ok, _ in results if not ok]


def _history_world() -> tuple[list[dict], list[dict]]:
    """Clean recent day preceded by a corrupted OLD pair and an OLD 48h hole: the
    watchdog must be green, and the history must still be reported."""
    s0 = [{"t": "obs", "ts": "2026-08-01T00:00:00+00:00", "series": "KXBTC15M",
           "ticker": "KXBTC15M-W0-T1", "yes_ask": 0.5, "no_ask": 0.52}]
    for h in range(0, 48):                          # resumes 48h later, then hourly
        s0.append({"t": "obs", "ts": f"2026-08-{3 + h // 24:02d}T{h % 24:02d}:00:00+00:00",
                   "series": "KXBTC15M", "ticker": "KXBTC15M-W0-T1",
                   "yes_ask": 0.5, "no_ask": 0.52})
    led = [
        {"t": "open", "ticker": "OLD-1", "rule": "R", "side": "yes", "price": 0.07,
         "contracts": 1, "fee": 0.01, "ts": "2026-08-01T00:00:00+00:00"},
        {"t": "close", "ticker": "OLD-1", "rule": "R", "side": "yes", "price": 0.07,
         "result": "no", "won": False, "pnl": -0.07,          # stolen cent, OLD
         "ts": "2026-08-01T00:15:00+00:00"},
        {"t": "open", "ticker": "NEW-1", "rule": "R", "side": "no", "price": 0.85,
         "contracts": 1, "fee": 0.01, "ts": "2026-08-04T23:00:00+00:00"},
        {"t": "close", "ticker": "NEW-1", "rule": "R", "side": "no", "price": 0.85,
         "result": "no", "won": True, "pnl": 0.14, "ts": "2026-08-04T23:15:00+00:00"},
    ]
    return s0, led


def _selftest() -> int:
    s0, led = _fixture()
    assert _failed(run_all(s0, led)) == [], run_all(s0, led)

    def fails(stage0, ledger):
        return _failed(run_all(stage0, ledger))

    def edit(rows, i, **kw):
        out = [dict(r) for r in rows]
        out[i].update(kw)
        return out

    # A1: out-of-bounds input, the null-ask feed break, and an all-garbage file
    assert fails(edit(s0, 0, spot=-5.0), led) == ["A1 obs bounds"]
    assert fails(edit(edit(s0, 0, yes_ask=None), 1, no_ask=None), led) == ["A1 obs bounds"]
    assert fails([{"_unparseable": True}] * 5, led) == ["A1 obs bounds"]
    # A2: a market settling both ways
    bad = s0 + [{"t": "settle", "ticker": "KXBTC15M-W1-T1", "result": "yes"}]
    assert fails(bad, led) == ["A2 settle consistency"]
    # B1: orphan close; torn (unparseable) ledger line
    ghost = {"t": "close", "ticker": "GHOST", "rule": "R", "side": "yes",
             "price": 0.5, "result": "yes", "won": True, "pnl": 0.48}
    assert fails(s0, led + [ghost]) == ["B1 ledger structure"]
    assert fails(s0, led + [{"_unparseable": True}]) == ["B1 ledger structure"]
    # B2: a stolen cent; an off-grid value; a close whose price drifted from its open
    assert fails(s0, edit(led, 1, pnl=-0.07)) == ["B2 ledger conservation"]
    assert fails(s0, edit(led, 1, pnl=-0.080000437)) == ["B2 ledger conservation"]
    assert fails(s0, edit(led, 3, price=0.80, pnl=0.19)) == ["B2 ledger conservation"]
    # B2+C2: a close rewritten to the OTHER side with self-consistent P&L (a -0.08
    # loss booked as +0.92) - two fields corrupted, two alarms, both correct
    flipped = edit(led, 1, side="no", won=True, pnl=0.92)
    assert set(fails(s0, flipped)) == {"B2 ledger conservation",
                                       "C2 ledger field identity"}
    # C2: won contradicting result; result not yes/no; result contradicting the
    # collector's own settlement (P&L kept self-consistent so ONLY C2 can see it)
    assert fails(s0, edit(led, 3, won=False)) == ["C2 ledger field identity"]
    assert fails(s0, edit(led, 1, result="YES")) == ["C2 ledger field identity"]
    assert fails(s0, edit(led, 1, result="yes", won=True, pnl=0.92)) == \
        ["C2 ledger field identity"]
    # D2: a 3-day hole that closes inside the recent window
    gap = s0[:2] + [dict(s0[1], ts="2026-09-04T00:01:00+00:00")] + s0[2:]
    assert fails(gap, led) == ["D2 collector gaps"]

    # RECENT FAILS, HISTORY INFORMS: old hole + old stolen cent -> green, but reported
    hs0, hled = _history_world()
    res = {name: (ok, detail) for name, ok, detail in run_all(hs0, hled)}
    assert all(ok for ok, _ in res.values()), res
    assert "history" in res["D2 collector gaps"][1], res["D2 collector gaps"]
    assert "history" in res["B2 ledger conservation"][1], res["B2 ledger conservation"]
    # ...and the same stolen cent made RECENT fails again
    hot = edit(hled, 3, pnl=0.13)
    assert fails(hs0, hot) == ["B2 ledger conservation"]

    # the offender leads the detail, so watchdog truncation cannot eat it
    detail = dict((n, d) for n, _, d in run_all(s0, edit(led, 1, pnl=-0.07)))
    assert "KXBTC15M-W1-T1" in detail["B2 ledger conservation"][:120]

    print("selftest OK")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "selftest":
        return _selftest()
    if cmd == "run":
        return run_diagnostics()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
