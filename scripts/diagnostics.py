#!/usr/bin/env python3
"""diagnostics - the model-integrity layer: A/B/C/D self-checks over the REAL files.

Module selftests prove code paths work on fixtures. This file asks the other
question: is the DATA the system is accumulating, and the ledger it is writing,
internally consistent RIGHT NOW? Every check here is one a silent breakage could
fail while all processes look alive - the same philosophy as healthcheck, one layer
deeper (healthcheck runs these every watchdog cycle via check_model_integrity).

The four layers, and the bug class each one exists to catch:

  A. INPUT VALIDATION   prices outside (0,1), crossed books, negative spot/strike,
                        contradictory settlements. Catches: a feed change silently
                        writing garbage that loaders "handle" by skipping - forever.
  B. CONSERVATION       double-entry on the paper ledger in EXACT integer units of
                        $0.0001 (Kalshi ticks in tenths of a cent now - see the
                        captured orderbook_fp row with 0.9070 levels - so cents are
                        not exact enough and floats never were). Every close must
                        recompute to the cent-exact P&L implied by its own open, and
                        the sum must equal what the report calls equity. Catches:
                        pnl drift, double entries, orphan closes, fee mismatches.
  C. IDENTITY/INVERSION the pricing model must satisfy what it claims: exactly 0.5
                        at the money (zero drift), monotone in strike, bounded, and
                        invariant under the sigma*sqrt(T) rescaling (sigma,4T) ==
                        (2*sigma,T); stored ledger fields must agree with each other
                        (won == (result == side)). Catches: a "small refactor" that
                        bends the distribution or the settlement logic.
  D. EDGE CASES         zero/None inputs must return defined values, never raise;
                        collector outage gaps are surfaced, not discovered in a
                        backtest three weeks later.

Anything here can also be corrupted on purpose: `selftest` plants one specific
defect per layer and asserts that exactly the right check fails - a detector that
has never seen its target fire is not a detector.

  py scripts/diagnostics.py            # run all checks against the live files
  py scripts/diagnostics.py selftest   # fixtures + planted corruptions, no data
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from lib.binary_justify import fair_p_above, kalshi_taker_fee  # noqa: E402
from shadow_book import liftable_depth  # noqa: E402

STAGE0 = Path(os.environ.get("STAGE0_LOG") or (ROOT / "data" / "stage0_crypto.jsonl"))
LEDGER = Path(os.environ.get("PAPER_CRYPTO_LEDGER")
              or (ROOT / "data" / "paper_crypto15.jsonl"))

# A malformed-row RATE above this fails input validation. The loaders skip bad rows
# by design; this is the alarm that "skipping" has quietly become the common case.
MAX_BAD_ROW_RATE = 0.01
U = 10000                    # exact unit: integer ten-thousandths of a dollar ($1e-4)


def _u(x) -> int | None:
    """Dollars -> exact integer units, refusing values off the $0.0001 grid."""
    if x is None:
        return None
    v = float(x) * U
    r = round(v)
    return r if abs(v - r) < 1e-3 else None


def _load(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"_unparseable": True})
    return rows


# ── the checks (each returns (ok, detail)) ────────────────────────────────────

def check_a1_obs_bounds(rows: list[dict]) -> tuple[bool, str]:
    """A: every observed price in (0,1), books not crossed, spot/strike positive."""
    n = bad = crossed = 0
    for r in rows:
        if r.get("_unparseable"):
            bad += 1
            continue
        if r.get("t") != "obs":
            continue
        n += 1
        ok_row = True
        for k in ("yes_bid", "yes_ask", "no_bid", "no_ask"):
            v = r.get(k)
            if v is not None and not (0.0 <= float(v) <= 1.0):
                ok_row = False
        for k in ("spot", "strike"):
            v = r.get(k)
            if v is not None and float(v) <= 0:
                ok_row = False
        ya, na = r.get("yes_ask"), r.get("no_ask")
        if ya is not None and na is not None and float(ya) + float(na) < 0.999:
            crossed += 1        # a crossed book is free money and thus data error
        if not ok_row:
            bad += 1
    if n == 0:
        return True, "no observations (nothing to validate)"
    rate = (bad + crossed) / max(1, n)
    return (rate <= MAX_BAD_ROW_RATE,
            f"{n} obs, {bad} malformed, {crossed} crossed books "
            f"({100 * rate:.2f}% bad, limit {100 * MAX_BAD_ROW_RATE:.0f}%)")


def check_a2_settle_consistency(rows: list[dict]) -> tuple[bool, str]:
    """A: settlements are yes/no and never contradict themselves."""
    seen: dict[str, str] = {}
    n = invalid = contradict = 0
    for r in rows:
        if r.get("t") != "settle":
            continue
        n += 1
        res, tk = r.get("result"), r.get("ticker")
        if res not in ("yes", "no") or not tk:
            invalid += 1
            continue
        if tk in seen and seen[tk] != res:
            contradict += 1     # the same market settling both ways is impossible
        seen[tk] = res
    return (invalid == 0 and contradict == 0,
            f"{n} settles, {invalid} invalid, {contradict} contradictory")


def check_b1_ledger_structure(rows: list[dict]) -> tuple[bool, str]:
    """B: double-entry structure - one open per (market, rule), no orphan closes."""
    opens: dict[tuple, int] = {}
    closes: dict[tuple, int] = {}
    for r in rows:
        key = (r.get("ticker"), r.get("rule"))
        if r.get("t") == "open":
            opens[key] = opens.get(key, 0) + 1
        elif r.get("t") == "close":
            closes[key] = closes.get(key, 0) + 1
    dup_open = sum(1 for v in opens.values() if v > 1)
    dup_close = sum(1 for v in closes.values() if v > 1)
    orphan = sum(1 for k in closes if k not in opens)
    return (dup_open == 0 and dup_close == 0 and orphan == 0,
            f"{len(opens)} positions, {dup_open} dup opens, "
            f"{dup_close} dup closes, {orphan} orphan closes")


def check_b2_ledger_conservation(rows: list[dict]) -> tuple[bool, str]:
    """B: every close's P&L recomputes EXACTLY (integer $1e-4) from its own open:
    win -> contracts*(1-price) - fee ; loss -> -contracts*price - fee. The sum of
    stored P&L must equal the sum of recomputed P&L to the last unit."""
    opens = {(r.get("ticker"), r.get("rule")): r for r in rows if r.get("t") == "open"}
    n = mismatch = ungrid = 0
    sum_stored = sum_recomputed = 0
    for r in rows:
        if r.get("t") != "close":
            continue
        o = opens.get((r.get("ticker"), r.get("rule")))
        if o is None:
            continue                      # b1's problem, not b2's
        n += 1
        price_u, fee_u, pnl_u = _u(r.get("price")), _u(o.get("fee") or 0.0), _u(r.get("pnl"))
        if None in (price_u, fee_u, pnl_u):
            ungrid += 1
            continue
        c = int(o.get("contracts") or 1)
        want = (c * (U - price_u) - fee_u) if r.get("won") else (-c * price_u - fee_u)
        sum_stored += pnl_u
        sum_recomputed += want
        if want != pnl_u:
            mismatch += 1
    ok = mismatch == 0 and ungrid == 0 and sum_stored == sum_recomputed
    return ok, (f"{n} closes, {mismatch} P&L mismatches, {ungrid} off-grid values, "
                f"stored {sum_stored / U:+.4f} vs recomputed {sum_recomputed / U:+.4f}")


def check_c1_model_identities() -> tuple[bool, str]:
    """C: the pricing model satisfies what it claims, verified by inversion."""
    probs = []
    # at the money, zero drift: one half, to the integrator's REAL accuracy
    # (measured ~2e-5 at z=0; its docstring's 1e-6 claim is optimistic)
    p_atm = fair_p_above(100.0, 100.0, 0.01, 5.0)
    probs.append(("ATM == 0.5", abs(p_atm - 0.5) < 1e-4))
    # monotone decreasing in strike, bounded in [0, 1]
    grid = [fair_p_above(100.0, k, 0.005, 10.0) for k in (90, 95, 99, 100, 101, 105, 110)]
    probs.append(("monotone in strike", all(a >= b - 1e-12 for a, b in zip(grid, grid[1:]))))
    probs.append(("bounded [0,1]", all(0.0 <= p <= 1.0 for p in grid)))
    # inversion: sigma*sqrt(T) invariance - (sigma, 4T) must price like (2*sigma, T)
    a = fair_p_above(100.0, 103.0, 0.004, 12.0)
    b = fair_p_above(100.0, 103.0, 0.008, 3.0)
    probs.append(("sigma*sqrt(T) inversion", abs(a - b) < 1e-4))
    # fee: symmetric in p vs 1-p, and never exceeds the 2c cap implied by ceil
    fee_sym = all(kalshi_taker_fee(p) == kalshi_taker_fee(1 - p)
                  for p in (0.05, 0.10, 0.30, 0.49))
    probs.append(("fee symmetry", fee_sym))
    probs.append(("fee bounds", all(0.0 <= kalshi_taker_fee(p) <= 0.02
                                    for p in (0.01, 0.25, 0.5, 0.75, 0.99))))
    failed = [name for name, ok in probs if not ok]
    return not failed, ("all identities hold" if not failed else "FAILED: " + ", ".join(failed))


def check_c2_ledger_field_identity(rows: list[dict]) -> tuple[bool, str]:
    """C: stored fields must agree with each other: won == (result == side)."""
    n = bad = 0
    for r in rows:
        if r.get("t") != "close" or r.get("result") not in ("yes", "no"):
            continue
        n += 1
        if bool(r.get("won")) != (r.get("result") == r.get("side")):
            bad += 1
    return bad == 0, f"{n} closes, {bad} won/result/side contradictions"


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
        ]
        bad = sum(1 for c in cases if not c)
        return bad == 0, f"{len(cases)} degenerate inputs, {bad} misbehaved"
    except Exception as e:  # noqa: BLE001 - raising IS the failure being tested for
        return False, f"raised {type(e).__name__}: {e}"


def check_d2_collector_gaps(rows: list[dict]) -> tuple[bool, str]:
    """D: surface the largest collection outage per series (informational unless a
    gap says the collector missed more than a day without anyone noticing)."""
    last: dict[str, float] = {}
    worst: dict[str, float] = {}
    for r in rows:
        if r.get("t") != "obs":
            continue
        s, ts = r.get("series"), r.get("ts")
        try:
            t = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if s in last:
            worst[s] = max(worst.get(s, 0.0), t - last[s])
        last[s] = max(t, last.get(s, t))
    if not worst:
        return True, "no gap data"
    msg = ", ".join(f"{s}: {g / 3600:.1f}h" for s, g in sorted(worst.items()))
    return max(worst.values()) < 86400, f"largest collection gaps - {msg}"


# ── runner ────────────────────────────────────────────────────────────────────

def run_all(stage0_rows: list[dict], ledger_rows: list[dict]) -> list[tuple[str, bool, str]]:
    return [
        ("A1 obs bounds", *check_a1_obs_bounds(stage0_rows)),
        ("A2 settle consistency", *check_a2_settle_consistency(stage0_rows)),
        ("B1 ledger structure", *check_b1_ledger_structure(ledger_rows)),
        ("B2 ledger conservation", *check_b2_ledger_conservation(ledger_rows)),
        ("C1 model identities", *check_c1_model_identities()),
        ("C2 ledger field identity", *check_c2_ledger_field_identity(ledger_rows)),
        ("D1 edge cases", *check_d1_edge_cases()),
        ("D2 collector gaps", *check_d2_collector_gaps(stage0_rows)),
    ]


def run_diagnostics() -> int:
    print("=" * 74)
    print("MODEL-INTEGRITY DIAGNOSTICS - A/B/C/D self-checks over the live files")
    print("=" * 74)
    print(f"stage0: {STAGE0}")
    print(f"ledger: {LEDGER}")
    print()
    results = run_all(_load(STAGE0), _load(LEDGER))
    fails = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        fails += 0 if ok else 1
        print(f"  [{status}] {name:<24} {detail}")
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
         "side": "yes", "price": 0.07, "result": "no", "won": False, "pnl": -0.08},
        {"t": "open", "ticker": "KXBTC15M-W2-T1", "rule": "H1_settlement_lag",
         "side": "no", "price": 0.85, "contracts": 1, "fee": 0.01,
         "ts": "2026-09-01T00:16:00+00:00"},
        {"t": "close", "ticker": "KXBTC15M-W2-T1", "rule": "H1_settlement_lag",
         "side": "no", "price": 0.85, "result": "no", "won": True, "pnl": 0.14},
    ]
    return stage0, ledger


def _one_fail(results, expect: str) -> None:
    failed = [name for name, ok, _ in results if not ok]
    assert failed == [expect], f"expected only [{expect}] to fail, got {failed}"


def _selftest() -> int:
    s0, led = _fixture()
    clean = run_all(s0, led)
    assert all(ok for _, ok, _ in clean), [r for r in clean if not r[1]]

    # A1: a negative spot must trip input validation (1 bad row of 2 >> 1% limit)
    bad = [dict(s0[0], spot=-5.0)] + s0[1:]
    _one_fail(run_all(bad, led), "A1 obs bounds")

    # A2: the same market settling both ways is impossible
    bad = s0 + [{"t": "settle", "ticker": "KXBTC15M-W1-T1", "result": "yes"}]
    _one_fail(run_all(bad, led), "A2 settle consistency")

    # B1: a close with no matching open is an orphan
    bad_led = led + [{"t": "close", "ticker": "GHOST", "rule": "H1_settlement_lag",
                      "side": "yes", "price": 0.5, "result": "yes", "won": True,
                      "pnl": 0.48}]
    _one_fail(run_all(s0, bad_led), "B1 ledger structure")

    # B2: one stolen cent must not survive conservation
    bad_led = [dict(r) for r in led]
    bad_led[1]["pnl"] = -0.07                     # true value is -0.08
    _one_fail(run_all(s0, bad_led), "B2 ledger conservation")

    # C2: won contradicting result+side must be caught
    bad_led = [dict(r) for r in led]
    bad_led[3]["won"] = False                     # but side=no and result=no
    res = run_all(s0, bad_led)
    failed = {name for name, ok, _ in res if not ok}
    # flipping `won` also flips the recomputed P&L, so conservation fails WITH it -
    # both alarms firing on one corruption is correct, not double counting
    assert failed == {"B2 ledger conservation", "C2 ledger field identity"}, failed

    # the exact-grid guard: a price that is not on the $0.0001 grid is flagged
    bad_led = [dict(r) for r in led]
    bad_led[1]["pnl"] = -0.080000437
    _one_fail(run_all(s0, bad_led), "B2 ledger conservation")

    # D2: a two-day hole in collection must surface
    gap = s0[:2] + [dict(s0[1], ts="2026-09-04T00:01:00+00:00")] + s0[2:]
    _one_fail(run_all(gap, led), "D2 collector gaps")

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
