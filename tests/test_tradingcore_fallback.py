"""Tests for the vendored tradingcore fallback (vendor/tradingcore).

The real tradingcore is a sibling repo present only on the original dev machine; on a
fresh clone lib/__init__.py falls back to this shim. What the shim must satisfy is
DEFINED BY THIS REPO'S CALL SITES - not by what seems textbook-tidy.

LESSON PINNED HERE: the first version of these tests checked the API the shim had
invented (kelly_bet_size(p, q, bankroll=...), ensemble_dampener(n_sources), ...), so
they passed while the real callers crashed or silently miscomputed: 7 of 17 call sites
worked. Comparing against an independently rebuilt core exposed it. Every test below
replays a REAL call site and names it, so the shim and its callers can no longer drift.
"""
import json
import math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import lib  # noqa: F401,E402  (bootstraps the fallback onto sys.path)
import pytest  # noqa: E402
import tradingcore.audit as au  # noqa: E402
import tradingcore.calibration as cal  # noqa: E402
from tradingcore.kelly import (ensemble_dampener, expected_value, fee_per_contract,  # noqa: E402
                               fractional_kelly, kelly_bet_size,
                               kelly_bet_size_slippage_aware, kelly_fraction,
                               min_edge_for_trade)


@pytest.fixture(autouse=True)
def _isolated_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(au, "_LOG", tmp_path / "audit.jsonl")
    monkeypatch.setattr(cal, "_LOG", tmp_path / "forecasts.jsonl")


# ── kelly: every real call site ───────────────────────────────────────────────

def test_forecaster_559_expected_value_with_fee_rate():
    """expected_value(trade_prob, trade_market_prob, fee_rate). The old shim read
    fee_rate as the PAYOUT and returned -0.488 for a +10c edge."""
    ev = expected_value(0.60, 0.50, 0.07)
    assert abs(ev - (0.10 - 0.07 * 0.5 * 0.5)) < 1e-12        # 10c edge minus 1.75c fee
    assert abs(expected_value(0.5, 0.5)) < 1e-12              # fair price, no fee: zero


def test_forecaster_344_risk_agent_171_min_edge_is_the_breakeven():
    """min_edge_for_trade(market_prob, fee_rate): exactly the edge at which EV is zero."""
    for q in (0.1, 0.5, 0.9):
        m = min_edge_for_trade(q, 0.07)
        assert abs(expected_value(q + m, q, 0.07)) < 1e-12
    assert min_edge_for_trade(0.5, 0.0) == 0.0


def test_fee_is_kalshi_shaped_not_a_percent_of_price():
    """fee_rate defaults to 0.07 (forecaster.py:771) - Kalshi's coefficient. A 90c
    favorite pays 0.63c, not 6.3c."""
    assert abs(fee_per_contract(0.90, 0.07) - 0.0063) < 1e-12
    assert fee_per_contract(0.50, 0.07) > fee_per_contract(0.90, 0.07)


def test_forecaster_558_fractional_kelly_positional():
    assert abs(fractional_kelly(0.6, 0.5) - 0.25 * 0.2) < 1e-12


def test_kalshi_weather_paper_129_kelly_fraction_textbook():
    # binary paying 1 at price q: f* = (p - q) / (1 - q)
    assert abs(kelly_fraction(0.6, 0.5) - 0.2) < 1e-12
    assert kelly_fraction(0.5, 0.5) == 0.0 and kelly_fraction(0.3, 0.5) == 0.0


def test_backtest_265_kelly_bet_size_bankroll_first_with_cap():
    """kelly_bet_size(bankroll, p, q, fraction=, max_per_market_pct=) - bankroll FIRST.
    The old shim took bankroll third and crashed on max_per_market_pct."""
    assert abs(kelly_bet_size(1000.0, 0.6, 0.5, fraction=1.0, max_per_market_pct=1.0)
               - 200.0) < 1e-9
    assert abs(kelly_bet_size(1000.0, 0.6, 0.5, fraction=0.25, max_per_market_pct=0.05)
               - 50.0) < 1e-9
    assert kelly_bet_size(1000.0, 0.6, 0.5, fraction=1.0, max_per_market_pct=0.05) == 50.0


@pytest.mark.parametrize("site,kwargs", [
    ("backtest.py:251", dict(bankroll=1000.0, our_prob=0.6, market_prob=0.5,
                             volume_24h=50_000, market_type="CLOB", fraction=0.25,
                             max_per_market_pct=0.05)),
    ("market_scanner.py:534", dict(bankroll=1000.0, our_prob=0.6, market_prob=0.5,
                                   volume_24h=50_000, market_type="CPMM")),
    ("backtest.py:1026", dict(bankroll=50.0, our_prob=0.6, market_prob=0.5,
                              volume_24h=10_000, market_type="CLOB")),
])
def test_slippage_aware_returns_the_dict_callers_index(site, kwargs):
    r = kelly_bet_size_slippage_aware(**kwargs)
    for k in ("bet_usd", "naive_kelly_usd", "effective_price", "slippage_pct",
              "edge_post_slip"):
        assert k in r, (site, k)
    assert r["converged"] is True                     # backtest.py:1045 asserts this
    assert 0 <= r["bet_usd"] <= r["naive_kelly_usd"] <= kwargs["bankroll"]
    assert r["effective_price"] >= kwargs["market_prob"]


def test_slippage_shrinks_size_and_thin_markets_more():
    deep = kelly_bet_size_slippage_aware(bankroll=10_000, our_prob=0.6, market_prob=0.5,
                                         volume_24h=10_000_000, max_per_market_pct=1.0)
    thin = kelly_bet_size_slippage_aware(bankroll=10_000, our_prob=0.6, market_prob=0.5,
                                         volume_24h=2_000, max_per_market_pct=1.0)
    assert thin["bet_usd"] < deep["bet_usd"] <= deep["naive_kelly_usd"]
    # unknown liquidity (volume_24h=0): assume the worst-case 25c impact. A 10c edge
    # cannot survive that, so the fixed point is NO TRADE - never a guessed fill price
    unknown = kelly_bet_size_slippage_aware(bankroll=1000, our_prob=0.6, market_prob=0.5,
                                            volume_24h=0)
    assert unknown["bet_usd"] == 0.0 and unknown["converged"] is True
    # an edge big enough to survive the worst case still sizes, AT the worst-case price
    huge = kelly_bet_size_slippage_aware(bankroll=1000, our_prob=0.99, market_prob=0.5,
                                         volume_24h=0)
    assert huge["bet_usd"] > 0 and abs(huge["effective_price"] - 0.75) < 1e-9


def test_market_scanner_572_ensemble_dampener_spread_thresholds():
    kw = dict(mild_threshold=0.10, strong_threshold=0.20, floor=0.5)
    assert ensemble_dampener(spread=0.05, **kw) == 1.0
    assert abs(ensemble_dampener(spread=0.15, **kw) - 0.75) < 1e-12
    assert ensemble_dampener(spread=0.30, **kw) == 0.5


def test_probabilities_out_of_range_raise():
    with pytest.raises(ValueError):
        kelly_fraction(1.2, 0.5)
    with pytest.raises(ValueError):
        expected_value(0.5, float("nan"))


# ── calibration: every real call site ─────────────────────────────────────────

def test_backtest_859_scores_the_list_it_is_given():
    """brier_score(our_forecasts) must score THAT list - not the stored file (the
    rebuilt core compared in 2026-11 scored the file, making our Brier == crowd Brier)."""
    cal.record_forecast(market_id="M", our_probability=0.5, outcome=True)   # decoy on disk
    recs = [{"our_probability": 0.9, "outcome": True},
            {"our_probability": 0.1, "outcome": False}]
    assert abs(cal.brier_score(recs) - 0.01) < 1e-12
    assert abs(cal.log_loss([{"forecast": 0.9, "outcome": True}]) + math.log(0.9)) < 1e-9


def test_no_data_returns_the_callers_negative_sentinel():
    """forecaster.py:323 `if bs < 0: benefit of doubt`; dashboard_data.py:150/297
    `if bs >= 0`. nan fails BOTH tests and silently read as 'worst calibration'."""
    assert cal.brier_score() == -1.0 and cal.log_loss() == -1.0
    assert cal.brier_score([]) == -1.0


def test_brier_is_mean_squared_error():
    assert cal.brier_score([{"forecast": 1.0, "outcome": True},
                            {"forecast": 0.0, "outcome": False}]) == 0.0
    assert cal.brier_score([{"forecast": 0.0, "outcome": True}]) == 1.0
    assert abs(cal.brier_score([{"forecast": 0.5, "outcome": True},
                                {"forecast": 0.5, "outcome": False}]) - 0.25) < 1e-12


def test_resolution_tracker_201_then_dashboards_read_it_back():
    """record_forecast with the tracker's keywords, then every dashboard reader."""
    for p, o in ((0.95, True), (0.92, True), (0.05, False), (0.08, False)):
        cal.record_forecast(market_id="M", platform="kalshi", question="q?",
                            our_probability=p, market_probability=0.5, side="YES",
                            sources={"llm": p, "base_rate": 0.5}, outcome=o)
    assert 0 <= cal.brier_score() < 0.01
    curve = cal.calibration_curve()                   # dashboard_terminal.py:179
    for bucket, d in curve.items():
        assert {"predicted_mean", "actual_rate", "gap", "count"} <= set(d)
    assert curve["0.9-1.0"]["count"] == 2 and curve["0.9-1.0"]["actual_rate"] == 1.0
    sa = cal.source_accuracy()                        # templates/dashboard.html
    assert sa["llm"]["count"] == 4 and sa["llm"]["brier"] < sa["base_rate"]["brier"]
    assert abs(sa["base_rate"]["brier"] - 0.25) < 1e-12


def test_no_arg_calls_never_raise():
    for fn in (cal.brier_score, cal.log_loss, cal.calibration_curve, cal.source_accuracy):
        fn()
    cal.print_calibration_report()                    # main.py calibration command


# ── audit: writers and readers ────────────────────────────────────────────────

def test_audit_roundtrip_with_dashboard_fields():
    au.log_event("unit", "thing_happened", {"market_id": "M1", "edge": 0.1}, result="ok")
    ev = au.get_recent_events(5)                      # dashboard_data.py:340 positional
    assert ev[0]["action"] == "thing_happened" and ev[0]["component"] == "unit"
    assert ev[0]["details"]["market_id"] == "M1" and ev[0]["timestamp"]
    assert au.get_recent_events(5, source="unit") and not au.get_recent_events(5, source="x")


def test_audit_redacts_credentials_at_any_depth():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
    au.log_event("auth", "login", {"api_key": "sk-live-123", "nested": {"Token": "t0k"},
                                   "blob": pem, "market_id": "M"},
                 authorization="Bearer xyz")
    raw = au._LOG.read_text()
    for secret in ("sk-live-123", "t0k", "MIIabc", "Bearer xyz"):
        assert secret not in raw, secret
    assert json.loads(raw)["details"]["market_id"] == "M"   # ordinary fields survive


def test_audit_never_raises_on_unwritable_path(monkeypatch):
    monkeypatch.setattr(au, "_LOG", Path("/nonexistent-root/x/y.jsonl"))
    au.log_event("unit", "should_not_raise")


def test_audit_reads_pre_rewrite_records():
    au._LOG.write_text(json.dumps({"ts": "2026-09-01T00:00:00+00:00", "source": "old",
                                   "event": "e", "data": {"k": 1}, "result": ""}) + "\n")
    ev = au.get_recent_events(1)
    assert ev[0]["action"] == "e" and ev[0]["details"] == {"k": 1} and ev[0]["timestamp"]
