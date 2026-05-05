"""
Tests for lib/news_classifier — Wave A polybot upgrade.

Adapted from brodyautomates/polymarket-pipeline's classification-not-
probability pattern. Covers parsing tolerance, materiality thresholding,
and the prior-shift math. All tests use injected fake LLM completers —
no real API calls.
"""

import json
import pytest


# ── Parser ───────────────────────────────────────────────────────────


class TestParseResponse:
    def test_clean_json_response(self):
        from lib.news_classifier import _parse_response
        text = json.dumps({
            "direction": "MORE_LIKELY_YES",
            "materiality": 0.7,
            "reasoning": "Fed signaled hold",
        })
        c = _parse_response(text)
        assert c.direction == "MORE_LIKELY_YES"
        assert c.materiality == 0.7
        assert "Fed" in c.reasoning

    def test_json_in_markdown_fence(self):
        from lib.news_classifier import _parse_response
        text = '''Here is my analysis:
```json
{"direction": "MORE_LIKELY_NO", "materiality": 0.4, "reasoning": "weak signal"}
```
'''
        c = _parse_response(text)
        assert c.direction == "MORE_LIKELY_NO"
        assert c.materiality == 0.4

    def test_materiality_clamped_to_unit_range(self):
        from lib.news_classifier import _parse_response
        text = json.dumps({
            "direction": "MORE_LIKELY_YES",
            "materiality": 2.5,
            "reasoning": "x",
        })
        c = _parse_response(text)
        assert c.materiality == 1.0

    def test_invalid_direction_raises(self):
        from lib.news_classifier import _parse_response
        text = json.dumps({"direction": "MAYBE", "materiality": 0.5, "reasoning": ""})
        with pytest.raises(ValueError):
            _parse_response(text)

    def test_no_json_raises(self):
        from lib.news_classifier import _parse_response
        with pytest.raises(ValueError):
            _parse_response("just some prose, no json here")


# ── Probability translation ──────────────────────────────────────────


class TestClassificationToProbability:
    def _classification(self, direction, materiality):
        from lib.news_classifier import NewsClassification
        return NewsClassification(direction=direction, materiality=materiality,
                                  reasoning="")

    def test_below_threshold_returns_prior(self):
        from lib.news_classifier import classification_to_probability
        c = self._classification("MORE_LIKELY_YES", 0.2)
        # default threshold 0.3 → no shift
        assert classification_to_probability(c, prior=0.5) == 0.5

    def test_not_relevant_returns_prior(self):
        from lib.news_classifier import classification_to_probability
        c = self._classification("NOT_RELEVANT", 0.9)
        assert classification_to_probability(c, prior=0.4) == 0.4

    def test_more_likely_yes_pushes_up(self):
        from lib.news_classifier import classification_to_probability
        c = self._classification("MORE_LIKELY_YES", 0.5)
        # prior 0.5 + (1-0.5) * 0.5 = 0.75
        assert abs(classification_to_probability(c, prior=0.5) - 0.75) < 1e-9

    def test_more_likely_no_pushes_down(self):
        from lib.news_classifier import classification_to_probability
        c = self._classification("MORE_LIKELY_NO", 0.5)
        # prior 0.5 * (1 - 0.5) = 0.25
        assert abs(classification_to_probability(c, prior=0.5) - 0.25) < 1e-9

    def test_decisive_yes_clamped_below_one(self):
        from lib.news_classifier import classification_to_probability
        c = self._classification("MORE_LIKELY_YES", 1.0)
        # Would be 1.0 mathematically; clamped to 0.99 to keep Bayesian sane
        assert classification_to_probability(c, prior=0.5) == 0.99

    def test_decisive_no_clamped_above_zero(self):
        from lib.news_classifier import classification_to_probability
        c = self._classification("MORE_LIKELY_NO", 1.0)
        # Would be 0.0 mathematically; clamped to 0.01
        assert classification_to_probability(c, prior=0.5) == 0.01

    def test_custom_threshold(self):
        from lib.news_classifier import classification_to_probability
        c = self._classification("MORE_LIKELY_YES", 0.4)
        # Default threshold 0.3 → applies
        assert classification_to_probability(c, prior=0.5) > 0.5
        # Stricter threshold 0.5 → suppresses
        assert classification_to_probability(c, prior=0.5,
                                             materiality_threshold=0.5) == 0.5


# ── End-to-end with mocked completer ─────────────────────────────────


class TestClassifyNewsImpact:
    def test_classifies_via_injected_completer(self):
        from lib.news_classifier import classify_news_impact
        fake_response = json.dumps({
            "direction": "MORE_LIKELY_YES",
            "materiality": 0.6,
            "reasoning": "policy signals favor outcome",
        })
        result = classify_news_impact(
            question="Will the Fed pause rates in May?",
            articles=[{"headline": "Powell signals patience"}],
            current_yes_price=0.62,
            complete_fn=lambda p: (fake_response, "fake_provider"),
        )
        assert result is not None
        assert result.direction == "MORE_LIKELY_YES"
        assert result.materiality == 0.6
        assert result.provider == "fake_provider"

    def test_returns_none_on_no_articles(self):
        from lib.news_classifier import classify_news_impact
        result = classify_news_impact(
            question="?", articles=[],
            complete_fn=lambda p: ("", "fake"),
        )
        assert result is None

    def test_returns_none_on_provider_error(self):
        from lib.news_classifier import classify_news_impact
        def boom(prompt):
            raise RuntimeError("provider down")
        result = classify_news_impact(
            question="?", articles=[{"headline": "x"}],
            complete_fn=boom,
        )
        assert result is None

    def test_returns_none_on_unparseable_response(self):
        from lib.news_classifier import classify_news_impact
        result = classify_news_impact(
            question="?", articles=[{"headline": "x"}],
            complete_fn=lambda p: ("not json at all", "fake"),
        )
        assert result is None


# ── Integration with forecaster ──────────────────────────────────────


class TestForecasterIntegration:
    def test_news_impact_estimate_shifts_probability(self):
        """When news_impact_estimate is passed, it joins the Bayesian chain."""
        from lib.forecaster import estimate_probability
        from lib.market_client import MarketInfo

        market = MarketInfo(
            market_id="x", platform="manifold",
            question="?", description="", category="politics",
            status="open",
            yes_price=0.5, no_price=0.5,
            volume_24h=100_000, total_volume=500_000,
            resolution_date="2026-12-31", resolution_source="manifold_auto",
        )

        # No news_impact: forecast comes from base_rate alone
        fc_neutral = estimate_probability(market)

        # With strong YES news_impact: forecast pushed up
        fc_yes = estimate_probability(market, news_impact_estimate=0.85)
        assert fc_yes.probability > fc_neutral.probability

        # With strong NO news_impact: forecast pushed down
        fc_no = estimate_probability(market, news_impact_estimate=0.15)
        assert fc_no.probability < fc_neutral.probability

        # Chain logs the news_impact step
        assert any(s.get("step") == "news_impact_update" for s in fc_yes.bayesian_chain)
