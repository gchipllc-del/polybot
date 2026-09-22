#!/usr/bin/env python3
"""vol_replay - does the volatility model beat the Kalshi 15-min book? Conditional test.

WHY THIS EXISTS. edge_analysis screens the crypto venue for UNCONDITIONAL mispricing:
"at price P, does the market settle YES more often than P?" Weather taught us (the hard
way - see docs/WEATHER_CONDITIONAL.md) that a venue can pass every unconditional check
and still be beatable CONDITIONALLY: the edge appears only when an independent estimate
disagrees with the price, and averaged over all observations the disagreements cancel.

This script asks the conditional question for crypto, using the one independent estimate
this project already runs in production: lib/fair_value's zero-drift, fat-tailed
volatility model (binary_justify G1, EWMA sigma, Student-t nu=4). The paper trader has
used it since 2026-08-11 to rank strikes; every paper entry carries its `fv_edge` stamp.

  for every settled market in the stage0 log: price it from spot + trailing realized
  vol AS OF THAT MOMENT, compare to the book that existed at that moment, bet only
  where model and book disagree by more than a threshold, settle on Kalshi's result.

THREE GUARDS (inherited from forecast_replay, where each caught a real defect):

 1. NO LOOKAHEAD. Sigma at an entry is computed from spot observations at or before the
    entry timestamp, full stop. The selftest perturbs FUTURE spots and asserts entries
    are unchanged.
 2. THE MODEL MUST BEAT THE PRICE - PER BAND. Before any P&L: Brier of model
    probabilities vs Brier of the book's own mid, on the same settled contracts, out of
    sample. A cell is gated by ITS OWN band's duel, not the global average: the global
    Brier drowns a concentrated late-band edge under thousands of far-OTM contracts
    both sides price identically (the fixture proved it - a planted 8c late mispricing
    moved the global gate by 0.0003 while the model's own sigma noise cost 0.0007).
    Averaging away the conditional signal is the exact mistake this tool exists to
    avoid; the global gate is still printed as context. Bands are frozen; a band gate
    opens only on a REAL win - at least a 1% relative Brier improvement (a bare
    bm < bp flips on rounding luck) on at least 100 out-of-sample entries. The gate is
    a sanity precondition; the cell interval carries the significance.
 3. WALK-FORWARD, PER WINDOW, SEARCH-CORRECTED. The one calibration constant (a sigma
    scale k) is measured on the first half by date and frozen. A window is one 15-min
    event - all its strikes resolve on the SAME price move, so they are ONE bet. The
    grid is frozen at 4 bands x 4 thresholds and out-of-sample intervals are
    Bonferroni-corrected over the cells actually searched.

MEASURED DISCRIMINATION (fixture worlds; 'beatable' plants an 8c late-band mid
displacement, 'efficient' quotes the settlement-true probability plus spread):

    15-min windows   beatable world                     efficient world
    ~100             gates open, 0 HELD                 0 HELD
    ~200             1 HELD, planted bands only         0 HELD
    ~400             1 HELD, planted bands only         0 HELD
    ~600             3 HELD, planted bands only         0 HELD

At small samples a band gate can open on luck (measured: 1 band in ~half the runs
under ~300 windows) - which is why the gate is a precondition and the corrected cell
interval is the verdict; the efficient world never yielded a HELD at any size. CAVEAT:
the plant is large. A 1-2c real edge needs far more windows than this table suggests.
The live stage0 file already holds ~4,000 windows and grows ~100/day.

TWO COMMANDS, TWO EVIDENCE GRADES:
  replay   stage0 history re-priced with trailing vol. Honest but reconstructed.
  ledger   the FORWARD paper book, bucketed by the fv_edge stamped at entry time,
           before settlement. Nothing reconstructed - the strongest evidence we own.

  py scripts/vol_replay.py replay
  py scripts/vol_replay.py ledger
  py scripts/vol_replay.py selftest      # fixtures only, no data files, no network

Inputs : data/stage0_crypto.jsonl    (override: $env:STAGE0_LOG)
         data/paper_crypto15.jsonl   (override: $env:PAPER_CRYPTO_LEDGER)
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

# One shared definition of the honesty machinery - report and tests use the same code
# as the weather tool, so the two venues are judged by literally the same rules.
from forecast_replay import (MIN_CELL_WINDOWS, cell_verdict, mean_ci,  # noqa: E402
                             split_by_date, taker_fee, z_for)
from lib.binary_justify import fair_p_above  # noqa: E402  (the house model, unmodified)

S0_LOG = Path(os.environ.get("STAGE0_LOG") or (ROOT / "data" / "stage0_crypto.jsonl"))
LEDGER = Path(os.environ.get("PAPER_CRYPTO_LEDGER")
              or (ROOT / "data" / "paper_crypto15.jsonl"))

# ── frozen analysis grid (chosen before touching real data; never widened) ────
# <2min is split in two because the collector observes each market roughly once a
# minute: "1-2min" and "<1min" each hold about one observation per market, and the
# late-longshot thread (H1b/H5) lives somewhere in there.
BANDS = {
    ">10min": (10.0, 1e9),
    "2-10min": (2.0, 10.0),
    "1-2min": (1.0, 2.0),
    "<1min": (0.0, 1.0),
}
BAND_ORDER = list(BANDS)
THRESHOLDS = (0.03, 0.05, 0.08, 0.12)     # net edge (fair - ask - fee) required to bet
MAX_BOOK_WIDTH = 0.15   # yes_ask + no_ask - 1; placeholder books show ~0.98 here
EWMA_LAMBDA = 0.94      # house constant (binary_justify.ewma_sigma)
MIN_RETURNS = 30        # house constant (fair_value._MIN_RETURNS)
DT_LO_S, DT_HI_S = 30.0, 180.0   # a "1-minute" return must actually span ~1 minute;
                                 # collector-outage gaps must not enter the EWMA
NU = 4.0                # Student-t dof, same as production
# Sigma scale k: the ONE measured calibration. Grid is coarse on purpose - k is a
# measurement of model miscalibration, not a tunable edge parameter.
K_GRID = tuple(round(0.6 + 0.05 * i, 2) for i in range(21))          # 0.60 .. 1.60

# Frozen diagnostic slices for the live late-longshot thread (H1b/H5 territory:
# longshot side, late bands). These are HYPOTHESIS GENERATORS - anything found here
# gets a named_at rule and a forward paper record, never a size-up.
SLICE_BANDS = ("1-2min", "<1min")
SLICE_ASK_LO, SLICE_ASK_HI = 0.03, 0.20


# ── fast Student-t CDF (interpolation over binary_justify's own integrator) ───
# fair_p_above integrates the t pdf numerically per call (4000 steps). Fine live, too
# slow for ~10^6 replay evaluations. We tabulate THE SAME function once and
# interpolate; the selftest pins the interpolation to the direct integral.

_Z_LO, _Z_HI, _Z_STEP = -12.0, 12.0, 0.01
_t_table: list[float] | None = None


def _build_table() -> list[float]:
    global _t_table
    if _t_table is None:
        n = int(round((_Z_HI - _Z_LO) / _Z_STEP)) + 1
        # fair_p_above(spot=1, strike=e^{z*sigma_T}, ...) == t_sf(z*sqrt(nu/(nu-2)));
        # tabulate through the public function so the model can never drift from prod.
        _t_table = [fair_p_above(1.0, math.exp((_Z_LO + i * _Z_STEP) * 1.0), 1.0, 1.0,
                                 nu=NU) for i in range(n)]
    return _t_table


def p_above_fast(spot: float, strike: float, sigma_bar: float,
                 bars_left: float) -> float | None:
    """P(settle >= strike) - identical model to production, interpolated."""
    if spot is None or strike is None or sigma_bar is None:
        return None
    if spot <= 0 or strike <= 0 or sigma_bar <= 0 or bars_left <= 0:
        return 1.0 if (spot or 0) >= (strike or 0) else 0.0
    z = math.log(strike / spot) / (sigma_bar * math.sqrt(bars_left))
    if z <= _Z_LO:
        return 1.0
    if z >= _Z_HI:
        return 0.0
    tab = _build_table()
    x = (z - _Z_LO) / _Z_STEP
    # clamp: for z a few ulps under _Z_HI, x rounds to exactly len(tab)-1 and the
    # i+1 lookup would walk off the table (adversarial review, confirmed by repro)
    i = min(int(x), len(tab) - 2)
    frac = x - i
    return tab[i] * (1 - frac) + tab[i + 1] * frac


def brier(pairs: list[tuple[float, bool]]) -> float:
    if not pairs:
        return float("nan")
    return sum((p - (1.0 if w else 0.0)) ** 2 for p, w in pairs) / len(pairs)


# ── loading ───────────────────────────────────────────────────────────────────

def _epoch(iso: str) -> float | None:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def window_of(ticker: str) -> str:
    """SERIES-YYMMMDDHHMM-STRIKE -> the 15-min event. All strikes of one window resolve
    on the same price move: ONE bet."""
    parts = str(ticker or "").rsplit("-", 1)
    return parts[0] if len(parts) == 2 else str(ticker)


def load_stage0(path: Path) -> tuple[list[dict], dict, dict]:
    """-> (obs sorted by ts, settles {ticker: result}, spots {series: [(t, spot), ...]})

    Spots are deduped per timestamp (every strike in a cycle repeats the cycle's spot)
    and sorted, so the trailing-vol walk below is a single forward pass."""
    obs: list[dict] = []
    settles: dict[str, str] = {}
    spots: dict[str, dict[float, float]] = {}
    if not path.exists():
        return [], {}, {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("t")
            if t == "settle":
                if d.get("result") in ("yes", "no") and d.get("ticker"):
                    settles[d["ticker"]] = d["result"]
                continue
            if t != "obs":
                continue
            ts = _epoch(d.get("ts"))
            if ts is None:
                continue
            d["_ts"] = ts
            obs.append(d)
            sp, series = d.get("spot"), d.get("series")
            if sp is not None and series:
                try:
                    spots.setdefault(series, {})[ts] = float(sp)
                except (TypeError, ValueError):
                    pass
    obs.sort(key=lambda r: r["_ts"])
    spot_lists = {s: sorted(m.items()) for s, m in spots.items()}
    return obs, settles, spot_lists


class TrailingSigma:
    """EWMA sigma per series, fed ONLY spots with timestamp <= the query time.

    Same lambda and minimum-sample rule as production. Returns whose spacing falls
    outside [DT_LO_S, DT_HI_S] are skipped: a return across a collector outage is not a
    one-minute return, and feeding it to the EWMA poisons sigma for the next ~50 bars.
    """

    def __init__(self, spot_lists: dict[str, list[tuple[float, float]]]):
        self._spots = spot_lists
        self._idx = {s: 0 for s in spot_lists}
        self._var = {s: None for s in spot_lists}
        self._nret = {s: 0 for s in spot_lists}
        self._last = {s: None for s in spot_lists}   # (t, spot) last ingested

    def at(self, series: str, ts: float) -> float | None:
        pts = self._spots.get(series)
        if not pts:
            return None
        i = self._idx[series]
        while i < len(pts) and pts[i][0] <= ts:
            t, sp = pts[i]
            last = self._last[series]
            if last is not None and sp > 0 and last[1] > 0:
                dt = t - last[0]
                if DT_LO_S <= dt <= DT_HI_S:
                    r = math.log(sp / last[1])
                    v = self._var[series]
                    self._var[series] = (r * r if v is None
                                         else EWMA_LAMBDA * v + (1 - EWMA_LAMBDA) * r * r)
                    self._nret[series] += 1
            self._last[series] = (t, sp)
            i += 1
        self._idx[series] = i
        if self._nret[series] < MIN_RETURNS or not self._var[series]:
            return None
        s = math.sqrt(self._var[series])
        return s if s > 0 else None


def book_formed(ya, na) -> bool:
    """Both asks real, and the implied book width is not a placeholder shell."""
    if ya is None or na is None:
        return False
    try:
        ya, na = float(ya), float(na)
    except (TypeError, ValueError):
        return False
    return 0.0 < ya < 1.0 and 0.0 < na < 1.0 and (ya + na - 1.0) <= MAX_BOOK_WIDTH


def build_entries(obs: list[dict], settles: dict,
                  spot_lists: dict) -> list[dict]:
    """First formed observation per (settled ticker, band), priced with strictly
    trailing sigma. One row per entry; both sides evaluated at scoring time."""
    trail = TrailingSigma(spot_lists)
    out: list[dict] = []
    seen: set = set()
    for r in obs:                                   # already sorted by ts
        tk = r.get("ticker")
        result = settles.get(tk)
        if result is None:
            continue
        mins = r.get("mins_left")
        if mins is None or mins <= 0:
            continue
        band = next((b for b, (lo, hi) in BANDS.items() if lo <= mins < hi), None)
        if band is None or (tk, band) in seen:
            continue
        if not book_formed(r.get("yes_ask"), r.get("no_ask")):
            continue
        if r.get("spot") is None or r.get("strike") is None:
            continue
        sigma = trail.at(r.get("series"), r["_ts"])
        if sigma is None:
            continue
        seen.add((tk, band))
        out.append({
            "ts": r["_ts"],
            "date": datetime.fromtimestamp(r["_ts"], tz=timezone.utc).date().isoformat(),
            "series": r.get("series"), "ticker": tk, "window": window_of(tk),
            "band": band, "mins_left": float(mins),
            "spot": float(r["spot"]), "strike": float(r["strike"]),
            "sigma": sigma,
            "yes_ask": float(r["yes_ask"]), "no_ask": float(r["no_ask"]),
            "book": r.get("book"),
            "won_yes": result == "yes",
        })
    return out


# ── model application ─────────────────────────────────────────────────────────

def p_yes_of(e: dict, k: float) -> float | None:
    """The model's P(YES) for an entry, with sigma scaled by the frozen k."""
    return p_above_fast(e["spot"], e["strike"], e["sigma"] * k, e["mins_left"])


def fit_k(entries: list[dict]) -> tuple[float, float]:
    """The one measured calibration: pick the sigma scale k that minimizes the model's
    Brier on the FIRST half. Measured then frozen - the out half never votes."""
    best_k, best_b = 1.0, float("inf")
    for k in K_GRID:
        pairs = []
        for e in entries:
            p = p_yes_of(e, k)
            if p is not None:
                pairs.append((p, e["won_yes"]))
        b = brier(pairs)
        if not math.isnan(b) and b < best_b:
            best_k, best_b = k, b
    return best_k, best_b


GATE_MIN_N = 100    # a band gate decided on fewer out-of-sample entries is no gate


def gate(entries: list[dict], k: float) -> tuple[float, float, int]:
    """Brier of the model vs Brier of the book's own mid, same settled contracts."""
    ours, theirs = [], []
    for e in entries:
        p = p_yes_of(e, k)
        if p is None:
            continue
        mid = (e["yes_ask"] + (1.0 - e["no_ask"])) / 2.0
        ours.append((p, e["won_yes"]))
        theirs.append((mid, e["won_yes"]))
    return brier(ours), brier(theirs), len(ours)


GATE_REL_MARGIN = 0.01   # the model must be >= 1% better in Brier, not merely ahead


def band_gates(second: list[dict], k: float) -> dict[str, tuple[bool, float, float, int]]:
    """The gate each cell actually answers to: model-vs-mid on ITS band, out of sample.

    'Wins' must mean something: a bare bm < bp flips on rounding luck - the fixture's
    CLEAN band once 'won' by 0.3% relative, purely from cent-rounded quotes. Hence the
    relative margin. The gate stays a sanity precondition, deliberately not a second
    significance test: the cell interval below already carries the statistics
    (Bonferroni z ~ 3 on independent windows), and a fixture experiment showed that a
    window-clustered z>=2 gate test is so high-variance on binary outcomes that it
    blocks a planted 8-cent edge at 120 windows. Two significance hurdles in series is
    how real edges get filtered out while nothing extra is learned.

    {band: (ok, model_brier, price_brier, n_entries)}"""
    out = {}
    for band in BAND_ORDER:
        es = [e for e in second if e["band"] == band]
        bm, bp, n = gate(es, k)
        ok = (n >= GATE_MIN_N and not math.isnan(bp)
              and bm < bp * (1.0 - GATE_REL_MARGIN))
        out[band] = (ok, bm, bp, n)
    return out


def score(entries: list[dict], k: float, band: str, thresh: float) -> list[float]:
    """Per-WINDOW dollars for one (band, threshold) cell: among a window's qualifying
    strike-sides, take only the one the model calls most mispriced (the live
    MAX_PER_WINDOW=1 rule), enter at the ask, pay the real fee, settle on the result."""
    best: dict[str, tuple] = {}
    for e in entries:
        if e["band"] != band:
            continue
        p = p_yes_of(e, k)
        if p is None:
            continue
        for side, ask, win in (("yes", e["yes_ask"], e["won_yes"]),
                               ("no", e["no_ask"], not e["won_yes"])):
            if not (0 < ask < 1):
                continue
            pw = p if side == "yes" else 1.0 - p
            fee = taker_fee(ask)
            edge = pw - ask - fee
            if edge < thresh:
                continue
            pnl = (1.0 - ask - fee) if win else (-ask - fee)
            cur = best.get(e["window"])
            if cur is None or edge > cur[0]:
                best[e["window"]] = (edge, pnl)
    return [v[1] for v in best.values()]


def grid(first: list[dict], second: list[dict], k: float) -> tuple[list, float]:
    cells = []
    for band in BAND_ORDER:
        for th in THRESHOLDS:
            a, b = score(first, k, band, th), score(second, k, band, th)
            if not a and not b:
                continue
            cells.append((band, th, a, b))
    z = z_for(0.05 / max(1, len(cells)))
    rows = []
    for band, th, a, b in cells:
        ma, _, _ = mean_ci(a)
        mb, lob, hib = mean_ci(b, z=z)
        rows.append((band, th, len(a), ma, len(b), mb, lob, hib))
    return rows, z


# ── the frozen diagnostic slices (late-longshot forensics) ────────────────────

def liftable_depth(book, side: str, ask: float) -> float | None:
    """Contracts a TAKER can actually lift when buying `side` at `ask`.

    Kalshi book snapshots list resting YES bids under 'yes' and resting NO bids under
    'no'. Buying YES at `ask` fills against NO bids priced >= 100*(1-ask) - their
    owners are the ones selling YES at or below our price. Same-side levels are our
    COMPETITION, not our liquidity; counting them (which shadow_book._depth_at does -
    caught by adversarial review) labels unfillable books as deep. An absent side on a
    captured book is empty (0), not unknown (None) - Kalshi serves null for it."""
    if not isinstance(book, dict):
        return None
    opp = book.get("no" if side == "yes" else "yes")
    if opp is None:
        return 0.0
    if not isinstance(opp, list):
        return None
    need = (1.0 - ask) * 100.0 - 1e-6
    total = 0.0
    for lvl in opp:
        try:
            p_c, size = float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if p_c >= need:
            total += size
    return total


def _depth_state(e: dict, side: str, ask: float) -> str:
    d = liftable_depth(e.get("book"), side, ask)
    if d is None:
        return "no book"
    return "depth>=1" if d >= 1 else "empty book"


def slice_rows(entries: list[dict], k: float, cut: str) -> list[tuple]:
    """The frozen slice set over the live thread: late-band longshot entries, one per
    window - the FIRST qualifying touch, per the house entry convention (the earlier
    cheapest-ask pick selected the window's minimum in hindsight, a price no forward
    rule can have; caught by adversarial review). Split at the GLOBAL walk-forward
    `cut`, not each slice's own median. One Bonferroni family: every slice printed is
    a slice counted."""
    picked: dict[str, dict] = {}
    for e in entries:                       # entries arrive ts-sorted: first touch wins
        if e["band"] not in SLICE_BANDS or e["window"] in picked:
            continue
        ya, na = e["yes_ask"], e["no_ask"]
        side, ask = ("yes", ya) if ya < na else ("no", na)
        if not (SLICE_ASK_LO <= ask < SLICE_ASK_HI):
            continue
        p = p_yes_of(e, k)
        if p is None:
            continue
        pw = p if side == "yes" else 1.0 - p
        fee = taker_fee(ask)
        win = e["won_yes"] if side == "yes" else not e["won_yes"]
        picked[e["window"]] = {
            "date": e["date"], "ask": ask,
            "pnl": (1.0 - ask - fee) if win else (-ask - fee),
            "fv": pw - ask - fee,
            "depth": _depth_state(e, side, ask),
            "series": e["series"],
        }
    rows = list(picked.values())
    fam = [
        ("model: net fv edge >= +3c", [r for r in rows if r["fv"] >= 0.03]),
        ("model: net fv edge 0..3c", [r for r in rows if 0 <= r["fv"] < 0.03]),
        ("model: says overpriced", [r for r in rows if r["fv"] < 0]),
        ("book: resting depth >= 1", [r for r in rows if r["depth"] == "depth>=1"]),
        ("book: empty at our price", [r for r in rows if r["depth"] == "empty book"]),
        ("book: no depth captured", [r for r in rows if r["depth"] == "no book"]),
        ("series: KXBTC15M", [r for r in rows if r["series"] == "KXBTC15M"]),
        ("series: KXETH15M", [r for r in rows if r["series"] == "KXETH15M"]),
    ]
    z = z_for(0.05 / max(1, len(fam)))
    out = []
    for name, rs in fam:
        a = [r for r in rs if r["date"] <= cut]
        b = [r for r in rs if r["date"] > cut]
        ma, _, _ = mean_ci([r["pnl"] for r in a])
        mb, lob, hib = mean_ci([r["pnl"] for r in b], z=z)
        out.append((name, len(a), ma, len(b), mb, lob, hib))
    return out


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_replay() -> int:
    print("=" * 78)
    print("VOL REPLAY - does the volatility model beat the 15-min book? (conditional)")
    print("=" * 78)
    obs, settles, spot_lists = load_stage0(S0_LOG)
    if not obs:
        print(f"no stage0 log at {S0_LOG}.")
        return 1
    entries = build_entries(obs, settles, spot_lists)
    if not entries:
        print("no scorable entries (need settled markets + spot history + formed books).")
        return 1
    wins = {e["window"] for e in entries}
    dates = sorted({e["date"] for e in entries})
    print(f"{len(entries)} entries | {len(wins)} independent 15-min windows | "
          f"{dates[0]} .. {dates[-1]}")

    first, second, cut = split_by_date(entries)
    print(f"walk-forward cut at {cut}: {len({e['window'] for e in first})} windows in "
          f"sample, {len({e['window'] for e in second})} out")

    k, b_in = fit_k(first)
    print(f"sigma scale k = {k:.2f}  (measured on the first half, Brier {b_in:.4f}; "
          f"frozen for everything below)")
    if k in (K_GRID[0], K_GRID[-1]):
        print("  WARNING: k pinned at the grid edge - the model's sigma is off by more")
        print("  than the grid allows; treat every number below as suspect.")

    bm, bp, n = gate(second, k)
    print()
    print(f"GATE (context) - global out-of-sample Brier on {n} entries: "
          f"model {bm:.4f} vs price {bp:.4f}"
          f"  -> {'MODEL WINS' if bm < bp else 'PRICE WINS'}")
    gates = band_gates(second, k)
    print("  Cells answer to their OWN band's gate (a concentrated edge must not be")
    print("  averaged away under far-OTM contracts both sides price identically):")
    for band in BAND_ORDER:
        ok, gm, gp, gn = gates[band]
        why = ("MODEL WINS" if ok else
               f"too thin (n<{GATE_MIN_N})" if gn < GATE_MIN_N else
               "PRICE WINS" if gm >= gp else "model ahead, within noise - no gate")
        print(f"    {band:8} model {gm:.4f} vs price {gp:.4f}  n={gn:<6} -> {why}")

    rows, z = grid(first, second, k)
    print()
    print(f"  {'band':8} {'thr':>5} {'--- first half ---':>22}   "
          f"{'--- second half (out of sample) ---':>38}")
    print(f"  {'':8} {'':>5} {'wins':>6} {'$/window':>13}   "
          f"{'wins':>6} {'$/window':>13} {'CI (search-corr)':>20}  verdict")
    rows.sort(key=lambda r: -r[5])
    held = thin = flipped = 0
    for band, th, na, ma, nb2, mb, lob, hib in rows:
        verdict = cell_verdict(na, ma, nb2, mb, lob, gates[band][0])
        held += verdict.startswith("HELD")
        thin += verdict.startswith("persists")
        flipped += verdict.startswith("FLIPPED")
        ci = f"[{lob:+.3f},{hib:+.3f}]" if nb2 >= 2 else "-"
        print(f"  {band:8} {th:>5.2f} {na:>6} {ma:>+13.4f}   "
              f"{nb2:>6} {mb:>+13.4f} {ci:>20}  {verdict}")
    print()
    print(f"  intervals Bonferroni-corrected over the {len(rows)} cells searched "
          f"(z={z:.2f}).")
    print(f"  VERDICT: {held} HELD | {thin} persist-but-thin | {flipped} flipped")
    if not any(ok for ok, *_ in gates.values()):
        print("    Every band blocked at its gate - the conditional door is closed on")
        print("    this data, the same verdict shape that closed weather.")
    elif held:
        print("    The model beats the price AND a cell survives out of sample with a")
        print("    corrected interval clear of zero. Next step is a named_at rule and")
        print("    the forward paper book - NOT sizing, NOT live money.")

    print()
    print("-" * 78)
    print("LATE-LONGSHOT SLICES (H1b/H5 territory) - HYPOTHESIS GENERATION ONLY")
    print("  These slices are searched, so nothing here is a result. A clean slice")
    print("  earns exactly one thing: a pre-registered rule tested forward.")
    print(f"  {'slice':28} {'n_in':>5} {'$/win in':>9}  {'n_out':>5} {'$/win out':>9} "
          f"{'CI out (corr)':>18}")
    for name, na, ma, nb2, mb, lob, hib in slice_rows(entries, k, cut):
        ci = f"[{lob:+.3f},{hib:+.3f}]" if nb2 >= 2 else "-"
        print(f"  {name:28} {na:>5} {ma:>+9.3f}  {nb2:>5} {mb:>+9.3f} {ci:>18}")
    print()
    print("Caveats: entries are the collector's ~60s snapshots, not tick data; sigma is")
    print("EWMA over the collector's own spot samples; settlement is Kalshi's result.")
    print("=" * 78)
    return 0


# ── the forward ledger, conditioned on the stamps it already carries ──────────

FV_BINS = (("unstamped", None, None), ("fv < 0", -1e9, 0.0),
           ("fv 0-3c", 0.0, 0.03), ("fv 3-8c", 0.03, 0.08), ("fv >= 8c", 0.08, 1e9))


def ledger_rows(rows: list[dict]) -> dict:
    """Join opens to closes; keep per-trade fv_edge/depth stamps with the outcome."""
    opens = {(r.get("ticker"), r.get("rule")): r for r in rows if r.get("t") == "open"}
    out = []
    for r in rows:
        if r.get("t") != "close":
            continue
        o = opens.get((r.get("ticker"), r.get("rule"))) or {}
        out.append({"rule": r.get("rule"), "window": window_of(r.get("ticker")),
                    "pnl": float(r.get("pnl") or 0.0), "won": bool(r.get("won")),
                    "fv": o.get("fv_edge"), "depth": o.get("depth"),
                    "date": str(o.get("ts") or r.get("ts") or "")[:10]})
    return {"trades": out}


def _bin_of(fv) -> str:
    if fv is None:
        return "unstamped"
    for name, lo, hi in FV_BINS[1:]:
        if lo <= fv < hi:
            return name
    return "unstamped"


def _per_window(trades: list[dict]) -> list[tuple[str, float]]:
    """Collapse trades to one P&L per window (they are one bet), keyed for splitting."""
    agg: dict[str, list] = {}
    for t in trades:
        agg.setdefault(t["window"], []).append(t)
    return [(ts[0]["date"], sum(x["pnl"] for x in ts) / max(1, len(ts)))
            for ts in agg.values()]


def cmd_ledger() -> int:
    print("=" * 78)
    print("FORWARD LEDGER x MODEL STAMP - out-of-sample by construction")
    print("  Every fv_edge below was written BEFORE its market settled. If P&L rises")
    print("  with the stamped edge, the model sees real mispricing, forward.")
    print("=" * 78)
    if not LEDGER.exists():
        print(f"no paper ledger at {LEDGER}.")
        return 1
    rows = []
    for line in LEDGER.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    trades = ledger_rows(rows)["trades"]
    if not trades:
        print("no closed trades yet.")
        return 1
    print(f"{len(trades)} closed trades across "
          f"{len({t['window'] for t in trades})} windows")
    z = z_for(0.05 / len(FV_BINS))
    print()
    print("  NOTE: fv stamps are RAW model edges (pre-fee, from lib/fair_value); and")
    print("  one window often holds opposite-side rules whose stamps land in DIFFERENT")
    print("  bins with anti-correlated outcomes - so a monotone-looking column can be")
    print("  one lucky favorite streak wearing a trend costume. Trust the WITHIN-RULE")
    print("  table below for monotonicity; this headline table only sizes the bins.")
    print(f"  {'fv bin':12} {'windows':>8} {'WR':>7} {'$/window':>10} "
          f"{'CI (corr over bins)':>22}")
    for name, _, _ in FV_BINS:
        sub = [t for t in trades if _bin_of(t["fv"]) == name]
        if not sub:
            continue
        per_win = _per_window(sub)
        pnls = [p for _, p in per_win]
        m, lo, hi = mean_ci(pnls, z=z)
        wr = sum(1 for t in sub if t["won"]) / len(sub)
        ci = f"[{lo:+.3f},{hi:+.3f}]" if len(pnls) >= 2 else "-"
        print(f"  {name:12} {len(pnls):>8} {wr:>6.1%} {m:>+10.4f} {ci:>22}")
    print()
    print("  per rule x model view (does the stamp separate winners inside each rule?)")
    print(f"  {'rule':24} {'view':14} {'windows':>8} {'$/window':>10}")
    rules = sorted({t["rule"] for t in trades if t["rule"]})
    for rule in rules:
        for view, cond in (("model likes (fv>0)", lambda t: (t["fv"] or 0) > 0
                            and t["fv"] is not None),
                           ("model dislikes", lambda t: t["fv"] is not None
                            and t["fv"] <= 0),
                           ("unstamped", lambda t: t["fv"] is None)):
            sub = [t for t in trades if t["rule"] == rule and cond(t)]
            if len(sub) < 5:
                continue
            per_win = _per_window(sub)
            m, _, _ = mean_ci([p for _, p in per_win])
            print(f"  {rule:24} {view:14} {len(per_win):>8} {m:>+10.4f}")
    print()
    print("READ: the evidence is the WITHIN-RULE split - same rule, same side, does the")
    print("stamp separate winners? Monotone there = the model earning trust forward; flat")
    print("or inverted = the stamps are noise and the vol model adds nothing here.")
    print("Any pattern used to CHANGE a rule must be re-earned forward from its")
    print("named_at date - this table can nominate, never promote.")
    print("=" * 78)
    return 0


# ── selftest: two synthetic worlds, no data files, no network ─────────────────

def _lcg(seed: int):
    """Deterministic uniform(0,1) stream - keeps fixtures identical everywhere."""
    state = seed

    def rnd() -> float:
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        return ((state >> 11) & ((1 << 52) - 1)) / float(1 << 52)
    return rnd


def _gauss(rnd):
    u1 = max(rnd(), 1e-12)
    u2 = rnd()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2 * math.pi * u2)


def _true_p_above(cur: float, K: float, sig_min: float, mins_left: float) -> float:
    """The GENERATOR's own truth: paths are gaussian, so the Brier-optimal book is the
    gaussian probability - not the production t4 model. Quoting the t4 model here once
    made the 'efficient' fixture beatable for real (the calibrated model out-predicted
    a book that was itself miscalibrated against the gaussian settlements)."""
    z = math.log(K / cur) / (sig_min * math.sqrt(mins_left))
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def make_world(mode: str, n_windows: int, seed: int = 11):
    """Synthetic stage0 rows. 'efficient': the book quotes the settlement-true fair
    value plus a spread - unbeatable by construction. 'beatable': same book EXCEPT
    late (<2min) books carry an 8c mid displacement toward the favorite - a seller
    overhang on the longshot the model should catch, and only there."""
    rnd = _lcg(seed)
    rows = []
    sig_min = 0.0007                    # per-minute lognormal sigma, BTC-flavoured
    t0 = _epoch("2026-08-01T00:00:00+00:00")
    spot = 60000.0
    n_series_windows = n_windows // 2 + 1
    for series, base in (("KXBTC15M", 60000.0), ("KXETH15M", 2600.0)):
        spot = base
        t = t0
        for w in range(n_series_windows):
            # one 15-min window; walk the spot minute by minute
            path = [spot]
            for _ in range(15):
                path.append(path[-1] * math.exp(sig_min * _gauss(rnd)))
            close = path[-1]
            dcode = f"W{w:05d}"
            strikes = [round(spot * (1 + sig_min * z * 3.87), 2)
                       for z in (-1.5, -0.75, -0.25, 0.25, 0.75, 1.5)]
            for minute in range(15):
                mins_left = 15 - minute - 0.5
                cur = path[minute]
                ts_iso = datetime.fromtimestamp(t + 60 * minute, tz=timezone.utc
                                                ).isoformat()
                for si, K in enumerate(strikes):
                    p_true = _true_p_above(cur, K, sig_min, mins_left)
                    ya = min(0.99, max(0.01, p_true + 0.015))
                    na = min(0.99, max(0.01, (1 - p_true) + 0.015))
                    # The plant is a MID displacement, like the live hypothesis: late
                    # books overprice the favorite and underprice the longshot - two
                    # faces of one shift. It must CLEAR friction (spread + fee), or the
                    # fixture would be testing whether we can see untradeable edges.
                    if mode == "beatable" and mins_left < 2.0:
                        if ya <= na:            # yes is the longshot
                            ya = max(0.01, ya - 0.08)
                            na = min(0.99, na + 0.08)
                        else:
                            na = max(0.01, na - 0.08)
                            ya = min(0.99, ya + 0.08)
                    rows.append({"t": "obs", "ts": ts_iso, "series": series,
                                 "ticker": f"{series}-{dcode}-T{si}", "strike": K,
                                 "mins_left": round(mins_left, 2),
                                 "yes_ask": round(ya, 2), "no_ask": round(na, 2),
                                 "yes_bid": round(max(0.0, ya - 0.02), 2),
                                 "no_bid": round(max(0.0, na - 0.02), 2),
                                 "spot": round(cur, 2)})
            for si, K in enumerate(strikes):
                rows.append({"t": "settle", "ts": ts_iso, "series": series,
                             "ticker": f"{series}-{dcode}-T{si}",
                             "result": "yes" if close >= K else "no"})
            spot = close
            # hourly windows, so the fixture spans ~10 days and split_by_date (which
            # cuts on calendar DATES) lands near 50/50 - back-to-back windows once
            # squeezed the out-of-sample half to 50 windows and hid the planted edge
            t += 3600
    return rows


def _rows_to_parts(rows):
    """Round-trip synthetic rows through the real loader (serialization included)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "s0.jsonl"
        with open(p, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        return load_stage0(p)


def _run_world(rows) -> dict:
    obs, settles, spots = _rows_to_parts(rows)
    entries = build_entries(obs, settles, spots)
    first, second, cut = split_by_date(entries)
    k, _ = fit_k(first)
    bm, bp, n = gate(second, k)
    gates = band_gates(second, k)
    rows_, z = grid(first, second, k)
    verdicts = [cell_verdict(na, ma, nb, mb, lob, gates[band][0])
                for band, _, na, ma, nb, mb, lob, _ in rows_]
    held_cells = [(r[0], r[1]) for r, v in zip(rows_, verdicts)
                  if v.startswith("HELD")]
    return {"entries": entries, "k": k, "bm": bm, "bp": bp, "gates": gates,
            "held": held_cells, "verdicts": verdicts}


def _selftest() -> int:
    # interpolation table is pinned to the production integrator
    for z_ in (-3.0, -1.2, 0.0, 0.7, 2.5):
        direct = fair_p_above(1.0, math.exp(z_), 1.0, 1.0, nu=NU)
        fast = p_above_fast(1.0, math.exp(z_), 1.0, 1.0)
        assert abs(direct - fast) < 1e-4, (z_, direct, fast)

    # trailing sigma: never uses the future, skips outage gaps
    pts = {"S": [(float(i * 60), 100.0 * math.exp(0.001 * ((-1) ** i)))
                 for i in range(80)]}
    tr = TrailingSigma(pts)
    s_mid = tr.at("S", 40 * 60.0)
    assert s_mid is not None and s_mid > 0
    tr2 = TrailingSigma({"S": pts["S"][:41]})       # world with no future at all
    assert abs(tr2.at("S", 40 * 60.0) - s_mid) < 1e-12, "future data leaked into sigma"
    # outage guard: the price JUMPS 50% across a 2h gap. Without the dt filter that
    # one return poisons the EWMA ~4x; with it, sigma must stay near baseline.
    # (The first version of this fixture barely moved the price across the gap and
    # passed with the guard deleted - a vacuous test, caught by adversarial review.)
    calm = [(float(i * 60), 100.0 * (1 + 0.001 * (i % 2))) for i in range(40)]
    jump = calm + [(calm[-1][0] + 7200.0 + i * 60, 150.0 * (1 + 0.001 * (i % 2)))
                   for i in range(40)]
    s_jump = TrailingSigma({"S": jump}).at("S", 1e9)
    s_calm = TrailingSigma({"S": calm}).at("S", 1e9)
    assert s_jump is not None and s_jump < s_calm * 1.5, (s_jump, s_calm)

    # the docstring's promise, enforced: perturb every spot AFTER a cutoff by +5%
    # and the entries at or before the cutoff must be bit-identical
    w = make_world("efficient", 30, seed=13)
    obs_w, set_w, spots_w = _rows_to_parts(w)
    entries_a = build_entries(obs_w, set_w, spots_w)
    cutoff = sorted(e["ts"] for e in entries_a)[len(entries_a) // 2]
    spots_p = {s: [(t, sp * 1.05 if t > cutoff else sp) for t, sp in pts_]
               for s, pts_ in spots_w.items()}
    entries_b = build_entries(obs_w, set_w, spots_p)
    ea = [e for e in entries_a if e["ts"] <= cutoff]
    eb = [e for e in entries_b if e["ts"] <= cutoff]
    assert len(ea) == len(eb) and all(
        a["sigma"] == b["sigma"] and a["spot"] == b["spot"] for a, b in zip(ea, eb)), \
        "future spots leaked into past entries"

    # placeholder books are rejected, real ones pass
    assert not book_formed(0.99, 0.99)
    assert book_formed(0.07, 0.95)

    # window collapse: many strikes, one bet
    world = make_world("beatable", 40, seed=7)
    res = _run_world(world)
    per_band = {}
    for e in res["entries"]:
        per_band.setdefault(e["band"], set()).add(e["window"])
    for band in per_band:
        cell = score(res["entries"], res["k"], band, 0.03)
        assert len(cell) <= len(per_band[band]), "more bets than windows"

    # THE DISCRIMINATION TEST - the reason this tool can be trusted:
    eff = _run_world(make_world("efficient", 400, seed=5))
    assert not eff["held"], f"efficient world produced HELD cells: {eff['held']}"

    bt = _run_world(make_world("beatable", 400, seed=5))
    # the PLANTED bands' own gates must open; the clean >10min band's must not
    assert bt["gates"]["1-2min"][0] or bt["gates"]["<1min"][0], bt["gates"]
    assert not bt["gates"][">10min"][0], bt["gates"]
    assert bt["held"], "planted late-longshot edge not found"
    assert all(b in ("1-2min", "<1min") for b, _ in bt["held"]), \
        f"edge found outside the planted bands: {bt['held']}"

    # ledger binning
    lrows = [
        {"t": "open", "ticker": "KXBTC15M-W1-T1", "rule": "R", "fv_edge": 0.05,
         "depth": 3, "ts": "2026-08-11T00:00:00+00:00"},
        {"t": "close", "ticker": "KXBTC15M-W1-T1", "rule": "R", "won": True,
         "pnl": 0.90},
        {"t": "open", "ticker": "KXBTC15M-W2-T1", "rule": "R", "fv_edge": -0.02,
         "depth": 3, "ts": "2026-08-12T00:00:00+00:00"},
        {"t": "close", "ticker": "KXBTC15M-W2-T1", "rule": "R", "won": False,
         "pnl": -0.08},
    ]
    tr_ = ledger_rows(lrows)["trades"]
    assert _bin_of(tr_[0]["fv"]) == "fv 3-8c" and _bin_of(tr_[1]["fv"]) == "fv < 0"
    assert _bin_of(None) == "unstamped"
    pw = _per_window(tr_)
    assert len(pw) == 2

    print("selftest OK")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "replay":
        return cmd_replay()
    if cmd == "ledger":
        return cmd_ledger()
    if cmd == "selftest":
        return _selftest()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
