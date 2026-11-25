#!/usr/bin/env python3
"""hermes — bounded, evidence-gated strategy optimizer (the openclaw Hermes pattern,
subordinated to this project's validation discipline).

Openclaw's Hermes reviews trade history nightly and adjusts parameters within bounds.
That PATTERN is good; unsupervised it is also exactly the machine that produced
openclaw's revert churn and killed our old composite (min_conf 0.35 -> 0.65 -> BTC-only
-> dead). So this Hermes is caged:

  DORMANT GATE   refuses to propose ANYTHING until the dataset holds >= MIN_WINDOWS
                 independent 15-min windows (the measured discrimination threshold -
                 see edge_analysis.py: below ~600 windows a real edge and pure noise
                 are indistinguishable, so "tuning" is fitting the past).
  SEARCH SPACE   proposals are cells of the FROZEN grid only - (band x side x price
                 bucket), 60 cells total. No indicators, no stop-losses, no free knobs.
                 Small spaces cannot smuggle in a composite.
  TRAIN/HOLDOUT  a candidate must be +EV in the first 70% of history AND stay +EV in
                 the last 30% it never saw.
  SIGNIFICANCE   full-sample Wilson CI must clear breakeven at z=2.81 (p~0.005, chosen
                 because 60 cells are searched: expected false positives ~ 0.3).
  PROPOSE != APPLY  review only WRITES a proposal with its full evidence. A human runs
                 `apply` to activate it. Applied rules trade as v3 alongside the frozen
                 rules - the pre-registered set is never modified and remains the
                 permanent control group.
  ACCOUNTABILITY every applied rule is judged on its own FORWARD paper record
                 (independent windows, not trades); `review` flags rules whose forward
                 record has gone significantly negative for retirement.

  py scripts/hermes.py review              # gate check + proposals (safe, read-only)
  py scripts/hermes.py apply <name>        # activate a proposed rule (explicit)
  py scripts/hermes.py retire <name>       # deactivate an applied rule
  py scripts/hermes.py selftest
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import os
PROPOSALS = Path(os.environ.get("HERMES_PROPOSALS")
                 or (ROOT / "data" / "hermes_proposals.jsonl"))
OVERLAY = Path(os.environ.get("HERMES_OVERLAY")
               or (ROOT / "config" / "hermes_rules.json"))

MIN_WINDOWS = 600          # measured discrimination threshold (edge_analysis.py)
TRAIN_FRAC = 0.70
MIN_TRAIN_N = 50
MIN_HOLDOUT_N = 20
SIG_Z = 2.81               # p ~ 0.005; 60-cell search space -> ~0.3 expected false hits
MAX_ACTIVE = 2             # never more than this many Hermes rules live at once
RETIRE_Z = 1.96            # forward record significantly below breakeven -> flag


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_overlay() -> list[dict]:
    if not OVERLAY.exists():
        return []
    try:
        data = json.loads(OVERLAY.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_overlay(rules: list[dict]) -> None:
    OVERLAY.parent.mkdir(parents=True, exist_ok=True)
    OVERLAY.write_text(json.dumps(rules, indent=1), encoding="utf-8")


def _audit(kind: str, payload: dict) -> None:
    PROPOSALS.parent.mkdir(parents=True, exist_ok=True)
    with open(PROPOSALS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": kind, "ts": _now(), **payload},
                           separators=(",", ":")) + "\n")


# ── candidate search over the frozen grid ────────────────────────────────────

def _side_cells(events: list[dict]) -> dict:
    """(band, side, bucket) -> stats. Events come from edge_analysis.load_events."""
    from edge_analysis import taker_fee
    out: dict[tuple, dict] = {}
    for e in events:
        key = (e["band"], e["side"], e["bucket"])
        c = out.setdefault(key, {"n": 0, "wins": 0, "cost": 0.0, "fee": 0.0,
                                 "pnl": 0.0, "windows": set()})
        c["n"] += 1
        c["wins"] += 1 if e["won"] else 0
        c["cost"] += e["price"]
        f = taker_fee(e["price"])
        c["fee"] += f
        c["pnl"] += ((1.0 - e["price"]) if e["won"] else -e["price"]) - f
        c["windows"].add(e["window"])
    return out


def _bucket_range(bucket: str) -> tuple[float, float]:
    lo, hi = bucket.replace("c", "").split("-")
    return int(lo) / 100.0, int(hi) / 100.0


def _overlaps(band: str, side: str, lo: float, hi: float, rules: list[dict]) -> str | None:
    for r in rules:
        if r.get("band") == band and r.get("side") == side:
            if not (hi <= float(r["lo"]) or lo >= float(r["hi"])):
                return r.get("name")
    return None


def find_proposals(events: list[dict], existing_rules: list[dict],
                   active_overlay: list[dict]) -> tuple[list[dict], list[str]]:
    """Run the full gate chain. Returns (proposals, gate_log)."""
    from edge_analysis import wilson
    log: list[str] = []
    windows = {e["window"] for e in events}
    log.append(f"dataset: {len(events)} events, {len(windows)} independent windows")

    if len(windows) < MIN_WINDOWS:
        log.append(f"DORMANT: {len(windows)}/{MIN_WINDOWS} windows - below the measured "
                   f"discrimination threshold. Proposing now would fit noise.")
        return [], log

    mid = int(len(events) * TRAIN_FRAC)
    train, hold = events[:mid], events[mid:]
    tr, ho, full = _side_cells(train), _side_cells(hold), _side_cells(events)

    props = []
    for key, c in sorted(tr.items(), key=lambda kv: -(kv[1]["pnl"] / max(1, kv[1]["n"]))):
        band, side, bucket = key
        if c["n"] < MIN_TRAIN_N or c["pnl"] <= 0:
            continue
        h = ho.get(key)
        if not h or h["n"] < MIN_HOLDOUT_N:
            log.append(f"  {band} {side} {bucket}: train +EV but holdout too thin "
                       f"({0 if not h else h['n']}/{MIN_HOLDOUT_N})")
            continue
        if h["pnl"] <= 0:
            log.append(f"  {band} {side} {bucket}: train +EV but holdout NEGATIVE "
                       f"({h['pnl']/h['n']:+.3f}/bet) - noise, rejected")
            continue
        f = full[key]
        wr = f["wins"] / f["n"]
        be = (f["cost"] + f["fee"]) / f["n"]
        lo_ci, _ = wilson(wr, len(f["windows"]), z=SIG_Z)   # CI on WINDOWS, not trades
        if lo_ci <= be:
            log.append(f"  {band} {side} {bucket}: survives both halves but CI on "
                       f"{len(f['windows'])} windows does not clear breakeven "
                       f"({lo_ci:.3f} <= {be:.3f}) - keep collecting")
            continue
        blo, bhi = _bucket_range(bucket)
        clash = _overlaps(band, side, blo, bhi, existing_rules + active_overlay)
        if clash:
            log.append(f"  {band} {side} {bucket}: already covered by {clash}")
            continue
        name = f"HX_{band.replace('>','gt').replace('<','lt').replace('-','_')}_{side}_{bucket.replace('-','_')}"
        props.append({
            "name": name, "band": band, "side": side, "lo": blo, "hi": bhi, "v": 3,
            "thesis": f"Hermes: {side} in {band}/{bucket} passed train/holdout/CI gates",
            "evidence": {
                "train_n": c["n"], "train_ev": round(c["pnl"] / c["n"], 4),
                "holdout_n": h["n"], "holdout_ev": round(h["pnl"] / h["n"], 4),
                "full_n": f["n"], "full_windows": len(f["windows"]),
                "full_wr": round(wr, 4), "breakeven": round(be, 4),
                "ci_low_z281": round(lo_ci, 4),
            },
        })
        log.append(f"  {band} {side} {bucket}: PROPOSAL - train {c['pnl']/c['n']:+.3f}, "
                   f"holdout {h['pnl']/h['n']:+.3f}, CI low {lo_ci:.3f} > be {be:.3f}")
    return props, log


# ── forward accountability for applied rules ─────────────────────────────────

def review_active(active: list[dict]) -> list[str]:
    """Judge each applied rule on its own forward paper record; flag failures."""
    from edge_analysis import wilson
    notes = []
    try:
        import paper_trader as pt
        rep = pt.build_report(pt._load(pt.LEDGER))
    except Exception as e:  # noqa: BLE001
        return [f"paper ledger unavailable ({type(e).__name__}) - cannot judge"]
    for r in active:
        b = rep.get("by_rule", {}).get(r["name"])
        if not b or not b.get("n"):
            notes.append(f"{r['name']}: no forward trades yet")
            continue
        w = b.get("windows", 0)
        wr = b["wins"] / b["n"]
        _, hi_ci = wilson(wr, max(w, 1), z=RETIRE_Z)
        notes.append(f"{r['name']}: forward n={b['n']} windows={w} "
                     f"WR={wr*100:.0f}% pnl={b['pnl']:+.2f}")
        if w >= 50 and b["pnl"] < 0 and hi_ci < 0.5 + b["pnl"] / max(b["n"], 1):
            notes.append(f"  -> RETIREMENT FLAG: forward record significantly negative. "
                         f"Run: py scripts/hermes.py retire {r['name']}")
    return notes


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_review() -> int:
    import shadow_book as sb
    import stage0_collector as s0
    from edge_analysis import load_events
    events = load_events(s0.LOG)
    active = _load_overlay()

    print("=" * 74)
    print("HERMES - bounded optimizer (proposals require explicit `apply`)")
    print("=" * 74)
    props, log = find_proposals(events, sb.RULES, active)
    for line in log:
        print(line)
    print()
    if props:
        for p in props[: max(0, MAX_ACTIVE - len(active))]:
            _audit("proposal", p)
            print(f"PROPOSED: {p['name']}")
            print(f"  evidence: {json.dumps(p['evidence'])}")
            print(f"  activate with: py scripts/hermes.py apply {p['name']}")
        if len(active) + len(props) > MAX_ACTIVE:
            print(f"(capped: max {MAX_ACTIVE} active Hermes rules)")
    else:
        print("no proposals this run.")
    print()
    print("ACTIVE HERMES RULES (forward accountability):")
    if active:
        for line in review_active(active):
            print(f"  {line}")
    else:
        print("  none")
    print("=" * 74)
    return 0


def cmd_apply(name: str) -> int:
    active = _load_overlay()
    if any(r["name"] == name for r in active):
        print(f"{name} is already active.")
        return 1
    if len(active) >= MAX_ACTIVE:
        print(f"refused: {MAX_ACTIVE} Hermes rules already active. Retire one first.")
        return 1
    prop = None
    if PROPOSALS.exists():
        for line in PROPOSALS.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("t") == "proposal" and d.get("name") == name:
                prop = d
    if not prop:
        print(f"no proposal named {name} found in {PROPOSALS}. Run `review` first.")
        return 1
    rule = {k: prop[k] for k in ("name", "band", "side", "lo", "hi", "v", "thesis")}
    rule["applied_at"] = _now()
    rule["evidence"] = prop.get("evidence", {})
    active.append(rule)
    _save_overlay(active)
    _audit("apply", {"name": name})
    print(f"applied {name}. The paper trader picks it up next cycle (restart the task).")
    print("It trades as v3 alongside the frozen rules and is judged on its own forward "
          "record.")
    return 0


def cmd_retire(name: str) -> int:
    active = _load_overlay()
    keep = [r for r in active if r["name"] != name]
    if len(keep) == len(active):
        print(f"{name} is not active.")
        return 1
    _save_overlay(keep)
    _audit("retire", {"name": name})
    print(f"retired {name}. Its ledger history remains for the record.")
    return 0


def active_rules() -> list[dict]:
    """Consumed by paper_trader._rules(): currently active Hermes overlay rules."""
    return _load_overlay()


# ── selftest ─────────────────────────────────────────────────────────────────

def _selftest() -> int:
    import random
    import tempfile

    def mk_events(n_windows: int, edge: bool, seed: int) -> list[dict]:
        random.seed(seed)
        ev = []
        for wi in range(n_windows):
            w = f"W{wi:05d}"
            # favorite at 0.72: true 82% if edge else fair 72%
            p_true = 0.82 if edge else 0.72
            won = random.random() < p_true
            ev.append({"ts": f"{wi:07d}", "ticker": f"{w}-45", "window": w,
                       "band": "2-10min", "side": "yes", "bucket": "65-80c",
                       "price": 0.72, "won": won})
        return ev

    frozen = [{"name": "CONTROL", "band": ">10min", "side": "favorite",
               "lo": 0.35, "hi": 0.65}]

    # 1. dormant below the window threshold, even with a screaming edge
    props, log = find_proposals(mk_events(300, edge=True, seed=1), frozen, [])
    assert props == [] and any("DORMANT" in l for l in log), log

    # 2. real edge above threshold -> exactly one proposal with full evidence
    props, log = find_proposals(mk_events(900, edge=True, seed=2), frozen, [])
    assert len(props) == 1, (len(props), log)
    p = props[0]
    assert p["band"] == "2-10min" and p["side"] == "yes"
    assert (p["lo"], p["hi"]) == (0.65, 0.80)
    assert p["evidence"]["holdout_ev"] > 0
    assert p["v"] == 3

    # 3. fair pricing above threshold -> nothing proposed
    props, _ = find_proposals(mk_events(900, edge=False, seed=3), frozen, [])
    assert props == [], props

    # 4. overlap with an existing rule blocks the proposal
    blocker = [{"name": "H3", "band": "2-10min", "side": "yes", "lo": 0.65, "hi": 0.90}]
    props, log = find_proposals(mk_events(900, edge=True, seed=2), blocker, [])
    assert props == [] and any("covered by H3" in l for l in log), log

    # 5. overlay round-trip + paper_trader integration.
    # Patch the IMPORTED module instance, not this file-run-as-__main__: paper_trader
    # does `import hermes`, which is a separate module object from __main__ when this
    # selftest is executed as a script - patching only our own globals left the trader
    # reading the real overlay path (the exact bug this assertion first caught).
    import hermes as hmod
    with tempfile.TemporaryDirectory() as td:
        old_o, old_p = hmod.OVERLAY, hmod.PROPOSALS
        hmod.OVERLAY = Path(td) / "rules.json"
        hmod.PROPOSALS = Path(td) / "props.jsonl"
        try:
            rule = {"name": "HX_test", "band": "<2min", "side": "yes",
                    "lo": 0.80, "hi": 0.90, "v": 3, "thesis": "t"}
            hmod._save_overlay([rule])
            assert hmod.active_rules() == [rule]
            import paper_trader as pt
            merged, _, _ = pt._rules()
            assert any(r["name"] == "HX_test" for r in merged), \
                "paper_trader must merge Hermes overlay rules"
            frozen_names = {r["name"] for r in __import__("shadow_book").RULES}
            assert frozen_names <= {r["name"] for r in merged}
        finally:
            hmod.OVERLAY, hmod.PROPOSALS = old_o, old_p
    print("selftest OK")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "review"
    if cmd == "review":
        return cmd_review()
    if cmd == "apply" and len(sys.argv) > 2:
        return cmd_apply(sys.argv[2])
    if cmd == "retire" and len(sys.argv) > 2:
        return cmd_retire(sys.argv[2])
    if cmd == "selftest":
        return _selftest()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
