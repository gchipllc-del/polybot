"""
Regression tests for the Superforecaster prompt structure (Wave C).

Locks in the 8-step protocol — protects against accidental step-renumbering,
missing the new Knowledge Expansion stage, or drifting numbering after a
future restructure. Doesn't test LLM behavior (that's a calibration metric,
not unit-testable); just guarantees the prompt the bot sends is well-formed.
"""

import pytest


class TestSuperforecasterPromptStructure:
    def test_all_8_steps_present_in_order(self):
        from lib.llm_analyst import SUPERFORECASTER_PROMPT
        expected_headings = [
            "## Step 1: Decompose the question",
            "## Step 2: Knowledge expansion",
            "## Step 3: Reference class forecasting",
            "## Step 4: Inside view",
            "## Step 5: Steelman the other side",
            "## Step 6: Check for common biases",
            "## Step 7: Probability estimate",
            "## Step 8: Meta-uncertainty",
        ]
        # Each heading must appear AND in the order listed.
        last_idx = -1
        for h in expected_headings:
            i = SUPERFORECASTER_PROMPT.find(h)
            assert i >= 0, f"missing step heading: {h}"
            assert i > last_idx, f"out-of-order heading: {h} appears before previous"
            last_idx = i

    def test_knowledge_expansion_has_no_fabrication_guard(self):
        """Wave C insight: explicit guardrail against hallucinated facts."""
        from lib.llm_analyst import SUPERFORECASTER_PROMPT
        # The Knowledge Expansion step needs an anti-fabrication clause.
        ke_section_start = SUPERFORECASTER_PROMPT.find("Knowledge expansion")
        ke_section_end = SUPERFORECASTER_PROMPT.find("## Step 3")
        assert ke_section_start >= 0
        ke_section = SUPERFORECASTER_PROMPT[ke_section_start:ke_section_end]
        # Look for the "don't make up facts" clause in some form
        has_anti_fab = (
            "make up" in ke_section.lower()
            or "fabricat" in ke_section.lower()
            or "uncertain" in ke_section.lower()
        )
        assert has_anti_fab, (
            "Knowledge expansion stage must guard against hallucinated facts; "
            "no anti-fabrication clause found"
        )

    def test_output_format_block_intact(self):
        """The aggregator parses PROBABILITY/CONFIDENCE/REFERENCE_CLASS/etc.
        Adding/renumbering steps must not break that contract."""
        from lib.llm_analyst import SUPERFORECASTER_PROMPT
        for required in (
            "PROBABILITY:",
            "CONFIDENCE:",
            "REFERENCE_CLASS:",
            "KEY_FACTORS:",
            "REVERSAL_TRIGGERS:",
        ):
            assert required in SUPERFORECASTER_PROMPT, (
                f"missing parser-required field: {required}"
            )

    def test_format_placeholders_unchanged(self):
        """Adding Wave C must not have broken any {placeholder} fields the
        builder fills in — corruption here would silently produce malformed
        prompts at runtime."""
        from lib.llm_analyst import SUPERFORECASTER_PROMPT
        for ph in (
            "{question}",
            "{description}",
            "{market_price",  # has format spec :.0%
            "{category}",
            "{resolution_date}",
            "{news_context}",
        ):
            assert ph in SUPERFORECASTER_PROMPT, f"missing placeholder: {ph}"

    def test_persona_prefix_references_correct_step_count(self):
        """Persona swarm (T1.2) prefix message must say '8-step', not '7-step',
        after Wave C — otherwise persona prompts contradict the main protocol."""
        from lib.llm_analyst import _persona_prefix
        prefix = _persona_prefix("analyst")
        assert "8-step" in prefix
        assert "7-step" not in prefix
