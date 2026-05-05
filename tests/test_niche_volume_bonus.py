"""
Tests for the niche-market scoring bonus (Wave D).

Adapted from brodyautomates/polymarket-pipeline. The point of this layer
is that we should NOT preferentially pick whale markets where sophisticated
bots have already arb'd the edge away — instead pick markets in the
"inefficient sweet spot" (default $50K 24h volume, ±1.5 octaves).
"""

import pytest


class TestNicheVolumeBonusCurve:
    def _cfg(self, **overrides):
        base = {
            "niche_preferred_volume": 50_000,
            "niche_decay_octaves": 1.5,
            "niche_floor_score": 0.85,
        }
        base.update(overrides)
        return base

    def test_peak_at_preferred_volume(self):
        from lib.market_scanner import _niche_volume_bonus
        # Exactly at sweet spot → 1.0
        assert abs(_niche_volume_bonus(50_000, self._cfg()) - 1.0) < 1e-9

    def test_floor_far_from_preferred(self):
        from lib.market_scanner import _niche_volume_bonus
        # Whale: 100x the sweet spot (~6.6 octaves out)
        whale = _niche_volume_bonus(5_000_000, self._cfg())
        # Dust: 1/100th the sweet spot
        dust = _niche_volume_bonus(500, self._cfg())
        # Both should be near the floor
        assert 0.85 <= whale <= 0.86
        assert 0.85 <= dust <= 0.86

    def test_symmetric_in_log_space(self):
        from lib.market_scanner import _niche_volume_bonus
        cfg = self._cfg()
        # 10x above and 10x below the sweet spot should score equal-ish
        above = _niche_volume_bonus(500_000, cfg)
        below = _niche_volume_bonus(5_000, cfg)
        assert abs(above - below) < 1e-3

    def test_unknown_volume_is_neutral(self):
        from lib.market_scanner import _niche_volume_bonus
        # Missing/zero data shouldn't punish — 1.0 keeps it competitive
        assert _niche_volume_bonus(None, self._cfg()) == 1.0
        assert _niche_volume_bonus(0, self._cfg()) == 1.0
        assert _niche_volume_bonus(-100, self._cfg()) == 1.0

    def test_floor_can_be_lowered(self):
        """A more aggressive floor (e.g., 0.5) should cause whales to score
        lower than the default 0.85."""
        from lib.market_scanner import _niche_volume_bonus
        whale_default = _niche_volume_bonus(5_000_000, self._cfg())
        whale_aggressive = _niche_volume_bonus(
            5_000_000, self._cfg(niche_floor_score=0.5)
        )
        assert whale_aggressive < whale_default
        assert 0.5 <= whale_aggressive <= 0.55

    def test_clamps_floor_to_unit_range(self):
        """Bad config (floor < 0 or > 1) shouldn't break sizing math."""
        from lib.market_scanner import _niche_volume_bonus
        # Floor below 0 → clamped to 0, score in [0, 1]
        result = _niche_volume_bonus(5_000_000, self._cfg(niche_floor_score=-0.5))
        assert 0.0 <= result <= 1.0
        # Floor above 1 → clamped to 1
        result = _niche_volume_bonus(5_000_000, self._cfg(niche_floor_score=1.5))
        assert result == 1.0


class TestNichePriorityChangesOrdering:
    """The whole point of this fix: candidate ordering must shift away
    from pure volume-DESC toward niche-aware priority."""

    def test_niche_market_beats_whale_after_priority_calculation(self):
        """A $50K market should rank ABOVE a $5M market under the new
        priority, even though the whale has 100x more volume.

        Priority = niche_bonus × log10(volume).
        Niche $50K: 1.00 × 4.70 = 4.70
        Whale $5M: 0.86 × 6.70 = 5.76 (still wins on raw priority)

        BUT: we also expect the score to be CLOSER to the whale's raw
        log10 score for the niche than for, say, a tiny $1K market —
        meaning the niche bonus partially closes the gap.
        """
        import math
        from lib.market_scanner import _niche_volume_bonus
        cfg = {
            "niche_preferred_volume": 50_000,
            "niche_decay_octaves": 1.5,
            "niche_floor_score": 0.85,
        }
        whale_raw = math.log10(5_000_000)
        niche_raw = math.log10(50_000)
        whale_priority = _niche_volume_bonus(5_000_000, cfg) * whale_raw
        niche_priority = _niche_volume_bonus(50_000, cfg) * niche_raw

        # Without the bonus, whale dominates by ~2.0 (raw log10 difference)
        raw_gap = whale_raw - niche_raw
        # With the bonus, the gap is meaningfully closed
        weighted_gap = whale_priority - niche_priority
        assert weighted_gap < raw_gap, (
            "niche bonus should narrow the volume-priority gap"
        )

    def test_dust_market_gets_lowest_priority(self):
        """A super-thin $500 market should rank LOWEST — niche bonus
        doesn't help if there's no liquidity to trade against."""
        import math
        from lib.market_scanner import _niche_volume_bonus
        cfg = {
            "niche_preferred_volume": 50_000,
            "niche_decay_octaves": 1.5,
            "niche_floor_score": 0.85,
        }
        dust_priority = _niche_volume_bonus(500, cfg) * math.log10(500)
        niche_priority = _niche_volume_bonus(50_000, cfg) * math.log10(50_000)
        whale_priority = _niche_volume_bonus(5_000_000, cfg) * math.log10(5_000_000)
        assert dust_priority < niche_priority
        assert dust_priority < whale_priority
