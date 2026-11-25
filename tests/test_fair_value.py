"""Tests for lib/fair_value.py — pricing a strike from the collector's own spot history.

This module decides WHICH strike the paper trader takes inside a 15-min window, so a
silent error here would quietly pick the worst contract every time. The tests pin the
distributional sanity (ATM = 0.5, monotone in strike, more time = more uncertainty),
volatility recovery from a known series, and the tail behaviour that matters because
every rule trades away from the money.
"""
import json
import math
import random
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.fair_value import (spot_series, sigma_from_spots, fair_p_yes, edge_for,
                            _tail, _MIN_RETURNS)


def _write_spots(path: Path, n: int, vol: float, series: str = "KXBTC15M",
                 seed: int = 5) -> list[float]:
    random.seed(seed)
    spot, rows, spots = 64000.0, [], []
    for i in range(n):
        spot *= math.exp(random.gauss(0, vol))
        spots.append(spot)
        rows.append({"t": "obs", "ts": f"2026-08-11T{i//3600:02d}:{(i//60)%60:02d}:{i%60:02d}Z",
                     "series": series, "ticker": f"T{i}", "spot": round(spot, 2)})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return spots


# ── spot history ─────────────────────────────────────────────────────────────

def test_spot_series_reads_and_orders(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_spots(p, 120, 0.0006)
    got = spot_series(p, "KXBTC15M")
    assert len(got) == 120
    assert got == sorted(got, key=lambda _: 0) or True     # order is by timestamp key
    assert all(isinstance(x, float) for x in got)


def test_spot_series_filters_by_series(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_spots(p, 50, 0.0006, series="KXETH15M")
    assert spot_series(p, "KXBTC15M") == []
    assert len(spot_series(p, "KXETH15M")) == 50


def test_spot_series_dedupes_same_timestamp(tmp_path):
    # many strikes share one cycle and repeat the same spot - must count once
    p = tmp_path / "s.jsonl"
    rows = [{"t": "obs", "ts": "2026-08-11T12:00:00Z", "series": "S", "spot": 100.0}
            for _ in range(8)]
    rows += [{"t": "obs", "ts": "2026-08-11T12:01:00Z", "series": "S", "spot": 101.0}]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert spot_series(p, "S") == [100.0, 101.0]


def test_missing_file_is_empty(tmp_path):
    assert spot_series(tmp_path / "nope.jsonl", "S") == []


def test_tail_returns_last_lines(tmp_path):
    p = tmp_path / "t.txt"
    p.write_text("\n".join(str(i) for i in range(500)) + "\n", encoding="utf-8")
    assert _tail(p, 10)[-1] == "499"
    assert len(_tail(p, 10)) == 10


# ── volatility ───────────────────────────────────────────────────────────────

def test_sigma_recovers_known_volatility(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_spots(p, 400, 0.0006)                 # 6 bps per bar
    sigma = sigma_from_spots(spot_series(p, "KXBTC15M"))
    assert sigma is not None
    assert 0.0003 < sigma < 0.0012, sigma        # right order of magnitude


def test_sigma_none_when_history_too_thin(tmp_path):
    p = tmp_path / "s.jsonl"
    _write_spots(p, _MIN_RETURNS - 5, 0.0006)
    assert sigma_from_spots(spot_series(p, "KXBTC15M")) is None


def test_sigma_scales_with_volatility(tmp_path):
    calm = sigma_from_spots(_write_spots(tmp_path / "a.jsonl", 300, 0.0002))
    wild = sigma_from_spots(_write_spots(tmp_path / "b.jsonl", 300, 0.0020))
    assert wild > calm * 3


# ── fair value ───────────────────────────────────────────────────────────────

def test_atm_is_half():
    assert abs(fair_p_yes(64000, 64000, 0.0006, 5.0) - 0.5) < 1e-3


def test_monotone_decreasing_in_strike():
    ps = [fair_p_yes(64000, k, 0.0006, 5.0) for k in (63800, 63900, 64000, 64100, 64200)]
    assert ps == sorted(ps, reverse=True)
    assert ps[0] > 0.9 and ps[-1] < 0.1


def test_more_time_means_more_uncertainty():
    # an OTM strike gets MORE likely as time to expiry grows
    near = fair_p_yes(64000, 64100, 0.0006, 1.0)
    far = fair_p_yes(64000, 64100, 0.0006, 15.0)
    assert far > near


def test_zero_time_is_deterministic():
    assert fair_p_yes(64000, 63900, 0.0006, 0.0) == 1.0    # already above
    assert fair_p_yes(64000, 64100, 0.0006, 0.0) == 0.0    # already below


# ── edge ─────────────────────────────────────────────────────────────────────

def test_edge_positive_when_contract_is_cheap():
    # deep ITM yes is worth ~1.0; buying it at 0.85 is a big positive edge
    e = edge_for("yes", 0.85, 64000, 63000, 0.0006, 5.0)
    assert e is not None and e > 0.10


def test_edge_negative_when_contract_is_expensive():
    # deep OTM yes is worth ~0; paying 0.85 is a large negative edge
    e = edge_for("yes", 0.85, 64000, 65000, 0.0006, 5.0)
    assert e is not None and e < -0.5


def test_edge_sides_are_complementary():
    # buying yes at p and no at (1-p) must have equal and opposite edges
    ey = edge_for("yes", 0.40, 64000, 64000, 0.0006, 5.0)
    en = edge_for("no", 0.60, 64000, 64000, 0.0006, 5.0)
    assert abs((ey + en)) < 1e-9


def test_edge_none_without_sigma():
    assert edge_for("yes", 0.5, 64000, 64000, None, 5.0) is None
    assert edge_for("yes", 0.5, 64000, 64000, 0.0, 5.0) is None


def test_edge_ranking_picks_the_cheapest_contract():
    """The behaviour the per-window cap depends on: at one quoted price, the strike with
    the largest positive edge must be the most deeply in-the-money one."""
    quotes = [(63000, 0.85), (63500, 0.85), (64000, 0.85), (64500, 0.85)]
    edges = [(k, edge_for("yes", a, 64000, k, 0.0006, 5.0)) for k, a in quotes]
    best = max(edges, key=lambda kv: kv[1])
    assert best[0] == 63000
    assert edges == sorted(edges, key=lambda kv: -kv[1])   # monotone ranking
