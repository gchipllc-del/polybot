"""
Tests for the ensemble aggregation dispatcher (Wave B).

Adapted from Halawi et al. 2024 NeurIPS "Approaching Human-Level
Forecasting with Language Models" — they tested 5 aggregators on
N≥6 samples and found trimmed_mean optimal. Our `auto` mode routes
to trimmed_mean only when N≥5; below that it stays on the legacy
weighted geomean (with N=3 default polybot config).
"""

import pytest


class TestTrimmedMeanWeighted:
    def test_drops_top_and_bottom_one_each(self):
        from lib.forecaster import trimmed_mean_weighted
        # Outliers 0.05 and 0.95 dropped; mean of [0.50, 0.50, 0.50] = 0.50
        ests = {"a": 0.05, "b": 0.50, "c": 0.50, "d": 0.50, "e": 0.95}
        wts = {"a": 1.0, "b": 1.0, "c": 1.0, "d": 1.0, "e": 1.0}
        assert abs(trimmed_mean_weighted(ests, wts) - 0.50) < 1e-9

    def test_falls_back_to_weighted_mean_when_too_few_samples(self):
        from lib.forecaster import trimmed_mean_weighted
        # 2 samples — can't trim 1 from each end (would leave 0)
        ests = {"a": 0.20, "b": 0.80}
        wts = {"a": 1.0, "b": 3.0}
        # Falls back to weighted mean: (0.20*1 + 0.80*3) / 4 = 0.65
        assert abs(trimmed_mean_weighted(ests, wts) - 0.65) < 1e-9

    def test_respects_weights_in_middle_band(self):
        from lib.forecaster import trimmed_mean_weighted
        # Drop 0.10 and 0.90; weight middle: (0.30*1 + 0.50*3 + 0.70*1)/5
        ests = {"a": 0.10, "b": 0.30, "c": 0.50, "d": 0.70, "e": 0.90}
        wts = {"a": 1.0, "b": 1.0, "c": 3.0, "d": 1.0, "e": 1.0}
        expected = (0.30 + 0.50 * 3 + 0.70) / 5
        assert abs(trimmed_mean_weighted(ests, wts) - expected) < 1e-9

    def test_clamps_to_unit_range(self):
        from lib.forecaster import trimmed_mean_weighted
        # All extreme samples
        ests = {"a": 0.999, "b": 0.999, "c": 0.999, "d": 0.999, "e": 0.999}
        wts = {k: 1.0 for k in ests}
        result = trimmed_mean_weighted(ests, wts)
        assert 0.01 <= result <= 0.99


class TestAggregateSamplesDispatcher:
    def test_auto_routes_to_geomean_below_5(self):
        from lib.forecaster import aggregate_samples, geomean_log_odds
        ests = {"s0": 0.6, "s1": 0.5, "s2": 0.4}
        wts = {"s0": 1.0, "s1": 1.0, "s2": 1.0}
        # auto with N=3 should match weighted_geomean exactly
        a = aggregate_samples(ests, wts, method="auto")
        g = geomean_log_odds(ests, wts)
        assert abs(a - g) < 1e-9

    def test_auto_routes_to_trimmed_mean_at_5_or_more(self):
        from lib.forecaster import aggregate_samples, trimmed_mean_weighted
        ests = {f"s{i}": p for i, p in enumerate([0.1, 0.4, 0.5, 0.6, 0.9])}
        wts = {f"s{i}": 1.0 for i in range(5)}
        a = aggregate_samples(ests, wts, method="auto")
        t = trimmed_mean_weighted(ests, wts)
        assert abs(a - t) < 1e-9

    def test_explicit_method_overrides_auto(self):
        from lib.forecaster import aggregate_samples, geomean_log_odds
        # 6 samples — auto would pick trimmed_mean. Force weighted_geomean.
        ests = {f"s{i}": p for i, p in enumerate([0.1, 0.3, 0.5, 0.5, 0.7, 0.9])}
        wts = {f"s{i}": 1.0 for i in range(6)}
        forced = aggregate_samples(ests, wts, method="weighted_geomean")
        legacy = geomean_log_odds(ests, wts)
        assert abs(forced - legacy) < 1e-9

    def test_median_method(self):
        from lib.forecaster import aggregate_samples
        ests = {"a": 0.10, "b": 0.50, "c": 0.90}
        wts = {"a": 1.0, "b": 1.0, "c": 1.0}
        # Middle of [0.10, 0.50, 0.90] = 0.50
        assert aggregate_samples(ests, wts, method="median") == 0.50

    def test_mean_method(self):
        from lib.forecaster import aggregate_samples
        ests = {"a": 0.10, "b": 0.50, "c": 0.90}
        wts = {"a": 1.0, "b": 1.0, "c": 1.0}
        # 0.50 mean
        assert abs(aggregate_samples(ests, wts, method="mean") - 0.50) < 1e-9

    def test_unknown_method_raises(self):
        from lib.forecaster import aggregate_samples
        with pytest.raises(ValueError):
            aggregate_samples({"a": 0.5}, {"a": 1.0}, method="bogus")

    def test_empty_returns_neutral(self):
        from lib.forecaster import aggregate_samples
        assert aggregate_samples({}, {}, method="auto") == 0.50


class TestTrimmedMeanIsolatesOutliers:
    """The point of trimmed mean: one outlier shouldn't dominate."""

    def test_outlier_doesnt_skew_aggregate(self):
        from lib.forecaster import aggregate_samples
        # Five samples cluster at 0.5, one wild outlier at 0.95
        ests = {"a": 0.48, "b": 0.50, "c": 0.50, "d": 0.52, "e": 0.95}
        wts = {k: 1.0 for k in ests}
        trimmed = aggregate_samples(ests, wts, method="trimmed_mean")
        # Trimmed should be near 0.50 (drops 0.48 + 0.95)
        assert abs(trimmed - 0.504) < 0.05

        plain = aggregate_samples(ests, wts, method="mean")
        # Plain mean is pulled up by the outlier — much further from 0.50
        assert plain > trimmed
        assert abs(plain - 0.59) < 0.05  # (0.48+0.50+0.50+0.52+0.95)/5 = 0.59
