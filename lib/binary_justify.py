"""binary_justify — the YES/NO justification engine (the 2026-08-04 restart's foundation).

THE PRINCIPLE (learned the hard way — see the killed composite sleeve and the winning
weather sleeve): a bet is never justified by an indicator, a streak, or a vibe. A bet is
justified if and only if every gate below passes, and the full gate trace is logged so the
decision can be audited and calibrated after the fact:

  G1 MEASUREMENT  — we hold a real measurement (realized vol -> outcome distribution),
                    fresh and with enough sample. Not a prediction. (Weather analog: the
                    NWS nowcast.) 15-min direction is noise — fresh 196-bar check 2026-08-04:
                    P(up|up)=0.43, P(up|dn)=0.52, AC(1)<=0 — so nothing here predicts
                    direction; we only price the DISTRIBUTION around the current spot.
  G2 EDGE         — |p_fair - p_market| must exceed friction (fees+spread) plus a model-
                    error margin, on the specific side taken. Edge is computed net.
  G3 MECHANISM    — a named reason the counterparty is wrong (config `mechanism`). No
                    mechanism, no trade — "the market is off" is not a mechanism.
  G4 CALIBRATION  — the ledger of past p_fair-vs-outcome must not show the model is
                    miscalibrated (Brier vs base rate) once enough decisions accrue.
                    Until then the engine is paper/provisional by construction.
  G5 DECISIVE     — the strike must sit OUTSIDE the model's own blind zone. Weather
                    settlement diagnostic lesson: profit booked inside the measurement's
                    error band is variance, not skill. Require |S-K| >= decisive_z * sigma_T.

The engine is side-neutral: it evaluates BOTH sides of every market and returns BUY_YES /
BUY_NO / PASS with the losing gates named. Every evaluation (traded or not) is appended to
the justification ledger so calibration n accrues over ALL markets, not just trades.

Pure math + dataclasses; no network. Callers supply prices/candles.
"""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = Path(os.environ.get("JUSTIFY_LEDGER")
                   or (ROOT / "data" / "justify_ledger.jsonl"))


# ── measurement: realized vol → outcome distribution ─────────────────────────

def ewma_sigma(returns: list[float], lam: float = 0.94) -> float:
    """EWMA volatility per bar (RiskMetrics lambda). Newest return LAST."""
    if not returns:
        return 0.0
    v = returns[0] ** 2
    for r in returns[1:]:
        v = lam * v + (1.0 - lam) * r ** 2
    return math.sqrt(v)


def _norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _t_sf(z: float, nu: float) -> float:
    """Student-t survival function via numeric integration (no scipy on the host).
    Adequate for gate math (|err| < 1e-6 over the z-range we use)."""
    if z < 0:
        return 1.0 - _t_sf(-z, nu)
    # integrate pdf from z to z+20 (tail beyond is negligible at nu>=3)
    c = math.gamma((nu + 1) / 2) / (math.sqrt(nu * math.pi) * math.gamma(nu / 2))
    n_steps, hi = 4000, z + 20.0
    h = (hi - z) / n_steps
    s = 0.0
    for i in range(n_steps + 1):
        x = z + i * h
        w = 1.0 if 0 < i < n_steps else 0.5
        s += w * c * (1.0 + x * x / nu) ** (-(nu + 1) / 2)
    return s * h


def fair_p_above(spot: float, strike: float, sigma_bar: float, bars_to_expiry: float,
                 nu: float | None = 4.0) -> float:
    """P(price at expiry >= strike): zero-drift log-return model over the remaining
    horizon. nu=Student-t dof for crypto fat tails (None -> normal). sigma scaled so the
    t-distribution matches the measured variance (var of t = nu/(nu-2))."""
    if spot <= 0 or strike <= 0 or sigma_bar <= 0 or bars_to_expiry <= 0:
        return 1.0 if spot >= strike else 0.0
    sigma_T = sigma_bar * math.sqrt(bars_to_expiry)
    z = math.log(strike / spot) / sigma_T
    if nu is None:
        return _norm_sf(z)
    return _t_sf(z * math.sqrt(nu / (nu - 2.0)), nu)


# ── friction ─────────────────────────────────────────────────────────────────

def kalshi_taker_fee(price: float, contracts: int = 1) -> float:
    """Kalshi taker fee, ceil to the cent: ceil(0.07 * C * P * (1-P))."""
    if not (0 < price < 1):
        return 0.0
    return math.ceil(0.07 * contracts * price * (1.0 - price) * 100) / 100.0


def friction_per_contract(price: float, spread: float = 0.01) -> float:
    """All-in cost of taking one contract at `price`: fee + half-spread paid crossing."""
    return kalshi_taker_fee(price) + spread / 2.0


# ── the gates ────────────────────────────────────────────────────────────────

@dataclass
class Gate:
    name: str
    passed: bool
    value: float | str | None = None
    threshold: float | str | None = None
    note: str = ""


@dataclass
class Justification:
    """The full, auditable answer to `why is this YES / NO / PASS justified?`"""
    ts: float
    market_id: str
    spot: float
    strike: float
    minutes_left: float
    sigma_bar: float
    p_fair: float                       # P(YES side wins) under the measurement
    yes_price: float | None
    no_price: float | None
    verdict: str                        # BUY_YES | BUY_NO | PASS
    side_edge_net: float                # net edge of the verdict side (0 for PASS)
    gates: list[Gate] = field(default_factory=list)
    mechanism: str = ""

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, separators=(",", ":"))


DEFAULTS = {
    "min_bars": 40,             # G1: sample floor for the vol measurement
    "max_data_age_s": 120,      # G1: candles must be fresh
    "min_edge_net": 0.03,       # G2: net edge floor AFTER friction + error margin
    "error_margin": 0.03,       # G2: model-error haircut on the raw edge
    "spread": 0.01,             # G2: assumed cost of crossing
    "mechanism": "",            # G3: must be named to trade (e.g. "longshot_premium")
    "calib_min_n": 200,         # G4: decisions needed before calibration verdict binds
    "calib_max_brier_excess": 0.0,  # G4: model Brier must beat price-as-forecast Brier
    "decisive_z": 0.5,          # G5: |ln(K/S)| >= this many sigma_T (blind-zone floor)
    "nu": 4.0,                  # fat-tail dof for fair value
    # 2026-08-04 council fixes (Logician+Allocator, independently): a 15-min binary near
    # expiry is a live delta bet — one 30s spot move takes a far strike from 7c fair to
    # 40c fair, and a polling loop selling stale p_fair gets adversely selected. G6 voids
    # any decision whose spot has drifted more than this fraction of sigma_T since the
    # quoted prices were observed. G7 refuses to trade through scheduled jump events
    # (CPI/FOMC/liquidation regimes) — the caller wires the calendar.
    "max_stale_drift_z": 0.25,  # G6: |ln(spot_now/spot_at_quote)| / sigma_T ceiling
}


def justify(*, market_id: str, spot: float, strike: float, minutes_left: float,
            returns: list[float], bar_minutes: float, yes_price: float | None,
            no_price: float | None, data_age_s: float = 0.0,
            spot_at_quote: float | None = None, event_blackout: bool = False,
            calib_stats: dict | None = None, config: dict | None = None,
            now: float | None = None) -> Justification:
    """Evaluate one market, both sides. Pure function of its inputs.

    spot_at_quote: the spot when yes/no prices were observed — G6 voids the decision if
    spot has since drifted materially (the stale-quote/adverse-selection guard).
    event_blackout: True during scheduled jump windows (CPI/FOMC etc.) — G7 refuses."""
    cfg = {**DEFAULTS, **(config or {})}
    gates: list[Gate] = []

    sigma = ewma_sigma(returns)
    bars_T = minutes_left / bar_minutes if bar_minutes > 0 else 0.0
    p_fair = fair_p_above(spot, strike, sigma, bars_T, nu=cfg["nu"])

    # G1 measurement
    g1 = (len(returns) >= cfg["min_bars"] and sigma > 0
          and data_age_s <= cfg["max_data_age_s"] and bars_T > 0)
    gates.append(Gate("G1_measurement", g1,
                      value=f"n={len(returns)},age={data_age_s:.0f}s,sigma={sigma*1e4:.1f}bps",
                      threshold=f"n>={cfg['min_bars']},age<={cfg['max_data_age_s']}s"))

    # G5 decisive (computed early: it conditions both sides equally)
    sigma_T = sigma * math.sqrt(bars_T) if bars_T > 0 else 0.0
    dist_z = abs(math.log(strike / spot)) / sigma_T if sigma_T > 0 else 0.0
    g5 = dist_z >= cfg["decisive_z"]
    gates.append(Gate("G5_decisive", g5, value=round(dist_z, 3),
                      threshold=cfg["decisive_z"],
                      note="strike inside the measurement's blind zone" if not g5 else ""))

    # G2 edge — evaluate both sides net of friction + error margin
    def net_edge(p_model_side: float, price: float | None) -> float | None:
        if price is None or not (0 < price < 1):
            return None
        raw = p_model_side - price
        return raw - friction_per_contract(price, cfg["spread"]) - cfg["error_margin"]

    yes_net = net_edge(p_fair, yes_price)
    no_net = net_edge(1.0 - p_fair, no_price)
    best_side, best_net = "PASS", 0.0
    if yes_net is not None and yes_net >= cfg["min_edge_net"] and (no_net is None or yes_net >= no_net):
        best_side, best_net = "BUY_YES", yes_net
    elif no_net is not None and no_net >= cfg["min_edge_net"]:
        best_side, best_net = "BUY_NO", no_net
    g2 = best_side != "PASS"
    gates.append(Gate("G2_edge", g2,
                      value=f"yes_net={None if yes_net is None else round(yes_net,3)},"
                            f"no_net={None if no_net is None else round(no_net,3)}",
                      threshold=cfg["min_edge_net"]))

    # G3 mechanism — must be named in config to trade
    g3 = bool(cfg["mechanism"])
    gates.append(Gate("G3_mechanism", g3, value=cfg["mechanism"] or "(none)",
                      threshold="named", note="" if g3 else "no named counterparty error"))

    # G4 calibration — binds once enough ledger decisions have resolved
    cs = calib_stats or {}
    n_resolved = int(cs.get("n", 0))
    if n_resolved < cfg["calib_min_n"]:
        g4, note4 = True, f"provisional (n={n_resolved}<{cfg['calib_min_n']})"
    else:
        excess = float(cs.get("brier_model", 1.0)) - float(cs.get("brier_price", 1.0))
        g4 = excess <= cfg["calib_max_brier_excess"]
        note4 = f"brier_model-brier_price={excess:+.4f}"
    gates.append(Gate("G4_calibration", g4, value=n_resolved,
                      threshold=cfg["calib_min_n"], note=note4))

    # G6 staleness — the quoted prices must belong to the CURRENT spot
    if spot_at_quote is not None and spot_at_quote > 0 and sigma_T > 0:
        drift_z = abs(math.log(spot / spot_at_quote)) / sigma_T
        g6 = drift_z <= cfg["max_stale_drift_z"]
        gates.append(Gate("G6_staleness", g6, value=round(drift_z, 3),
                          threshold=cfg["max_stale_drift_z"],
                          note="" if g6 else "spot moved since quote — stale-fill risk"))
    else:
        gates.append(Gate("G6_staleness", True, value="untracked",
                          threshold=cfg["max_stale_drift_z"],
                          note="caller did not supply spot_at_quote"))

    # G7 event blackout — never sell tails through scheduled jumps
    gates.append(Gate("G7_event_blackout", not event_blackout,
                      value="blackout" if event_blackout else "clear", threshold="clear"))

    all_pass = all(g.passed for g in gates)
    verdict = best_side if (all_pass and g2) else "PASS"
    return Justification(
        ts=now if now is not None else time.time(),
        market_id=market_id, spot=spot, strike=strike, minutes_left=minutes_left,
        sigma_bar=sigma, p_fair=p_fair, yes_price=yes_price, no_price=no_price,
        verdict=verdict, side_edge_net=round(best_net, 4) if verdict != "PASS" else 0.0,
        gates=gates, mechanism=cfg["mechanism"],
    )


# ── the justification ledger (calibration accrues over ALL evaluations) ──────

def record(j: Justification, path: Path | None = None) -> None:
    p = path or LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(j.to_json() + "\n")


def resolve(market_id: str, ts: float, outcome_yes: bool,
            path: Path | None = None) -> None:
    """Append the settlement so calibration can join p_fair -> outcome."""
    p = path or LEDGER_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps({"resolve": True, "market_id": market_id, "ts": ts,
                            "outcome_yes": bool(outcome_yes)},
                           separators=(",", ":")) + "\n")


def calibration_stats(path: Path | None = None) -> dict:
    """Join evaluations to resolutions; Brier of the model vs Brier of the market price
    (price-as-forecast). The model must BEAT the price to earn size — that is the whole
    bar: if the market's own price forecasts outcomes better than we do, we have nothing."""
    p = path or LEDGER_PATH
    if not p.exists():
        return {"n": 0}
    evals: dict[str, dict] = {}
    outcomes: dict[str, bool] = {}
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("resolve"):
            outcomes[d["market_id"]] = bool(d["outcome_yes"])
        elif d.get("market_id"):
            evals.setdefault(d["market_id"], d)  # first evaluation per market
    bm = bp = n = 0
    for mid, d in evals.items():
        if mid not in outcomes:
            continue
        y = 1.0 if outcomes[mid] else 0.0
        pf = float(d.get("p_fair", 0.5))
        yp = d.get("yes_price")
        n += 1
        bm += (pf - y) ** 2
        bp += ((float(yp) if yp is not None else 0.5) - y) ** 2
    if n == 0:
        return {"n": 0}
    return {"n": n, "brier_model": bm / n, "brier_price": bp / n,
            "skill_vs_price": (bp - bm) / n}
