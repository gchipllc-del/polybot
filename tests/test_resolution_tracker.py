"""
Regression tests for the 2026-04-30 polybot status display bug.

trade_history.json was being polluted with open-position entries (no `won`
field) that get_performance_summary() and dashboard_data.get_trade_history()
treated as losses. Result: 5 trades / 0% win rate / -$157.41 displayed when
the real record was 3 trades / 67% win rate / -$156.70.

Fix: filter to entries with `won` set to a bool. These tests lock the
filter so a future refactor can't reintroduce the bug.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_history(tmp_path, monkeypatch):
    """Point both modules at a per-test history file."""
    th = tmp_path / "trade_history.json"
    monkeypatch.setattr("lib.resolution_tracker.TRADE_HISTORY_PATH", th)
    monkeypatch.setattr("lib.dashboard_data.TRADE_HISTORY_PATH", th)
    return th


def _write(path, entries):
    path.write_text(json.dumps(entries))


class TestResolvedTradeFilter:
    def test_open_positions_excluded_from_win_rate(self, isolated_history):
        """A position appended to history while still open (no `won` field)
        must NOT count toward total_trades or win_rate."""
        from lib.resolution_tracker import get_performance_summary

        _write(isolated_history, [
            # Open position written mid-flight (the bug pattern)
            {"market_id": "M1", "status": "open", "side": "NO",
             "entry_price": 0.11, "quantity": 1431},
            # A real resolved loss
            {"market_id": "M1", "side": "NO", "outcome": "YES",
             "won": False, "net_profit": -157.41,
             "opened_at": "2026-04-17", "closed_at": "2026-04-23"},
        ])
        s = get_performance_summary()
        assert s["total_trades"] == 1
        assert s["wins"] == 0
        assert s["losses"] == 1
        assert s["total_pnl"] == -157.41

    def test_dashboard_get_trade_history_filters_opens(self, isolated_history):
        from lib.dashboard_data import get_trade_history

        _write(isolated_history, [
            {"status": "open", "side": "NO", "entry_price": 0.11},  # filtered out
            {"won": True, "net_profit": 0.42, "side": "NO",
             "opened_at": "2026-04-17", "closed_at": "2026-04-23",
             "question": "Iran war?"},
            {"won": False, "net_profit": -157.41, "side": "NO",
             "opened_at": "2026-04-17", "closed_at": "2026-04-23",
             "question": "Virginia"},
        ])
        h = get_trade_history()
        assert h["total_trades"] == 2
        assert h["win_rate"] == 0.5
        assert h["total_pnl"] == round(-157.41 + 0.42, 2)

    def test_two_wins_one_loss_67pct(self, isolated_history):
        """The exact post-backfill state: 1 loss + 2 wins → 67% win rate."""
        from lib.resolution_tracker import get_performance_summary

        _write(isolated_history, [
            {"won": False, "net_profit": -157.41},
            {"won": True,  "net_profit": 0.4179},
            {"won": True,  "net_profit": 0.2941},
        ])
        s = get_performance_summary()
        assert s["total_trades"] == 3
        assert s["wins"] == 2
        assert s["win_rate"] == round(2/3, 4)
        assert s["total_pnl"] == round(-157.41 + 0.4179 + 0.2941, 2)

    def test_daily_loss_pct_overrides_stale_dollar_floor(self):
        """Wave 2 polybot bug: dollar floor (-$10) was calibrated to $50
        bankroll and tripped at every active session once bankroll grew.
        Pct rule should now allow up to 10% of bankroll before tripping."""
        from lib.circuit_breaker import check_daily_loss, CircuitBreakerTripped

        settings = {"circuit_breakers": {
            "max_daily_loss": -50,
            "max_daily_loss_pct": -0.10,
        }}
        # Bankroll $739, daily P/L -$60. With dollar-only floor of -50, this
        # would trip; with pct=-0.10 (= -$73.90), -$60 stays under both rules.
        assert check_daily_loss(-60.0, bankroll=739.0, settings=settings) is True

        # Push past the pct ceiling — should trip.
        with pytest.raises(CircuitBreakerTripped):
            check_daily_loss(-80.0, bankroll=739.0, settings=settings)

    def test_daily_loss_dollar_floor_still_applies_at_low_bankroll(self):
        """If bankroll falls (or pct unset), the absolute dollar floor is
        the safety net that prevents runaway losses on a small book."""
        from lib.circuit_breaker import check_daily_loss, CircuitBreakerTripped

        settings = {"circuit_breakers": {
            "max_daily_loss": -50,
            "max_daily_loss_pct": -0.10,
        }}
        # $100 bankroll: pct rule = -$10. Dollar floor = -$50. Lenient = -$50.
        assert check_daily_loss(-30.0, bankroll=100.0, settings=settings) is True
        # Past the dollar floor.
        with pytest.raises(CircuitBreakerTripped):
            check_daily_loss(-60.0, bankroll=100.0, settings=settings)

    def test_won_must_be_bool_not_truthy(self, isolated_history):
        """`won: 1` (int) or `won: "true"` (str) should NOT count — only
        explicit booleans, since settle_position writes Python bool."""
        from lib.resolution_tracker import get_performance_summary

        _write(isolated_history, [
            {"won": 1, "net_profit": 0.5},        # not bool — filtered
            {"won": "true", "net_profit": 0.5},   # not bool — filtered
            {"won": True, "net_profit": 0.42},    # real
        ])
        s = get_performance_summary()
        assert s["total_trades"] == 1
        assert s["wins"] == 1
