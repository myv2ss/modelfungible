# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

import pytest
from modelfungible.enterprise.guardrails import (
    Guardrails, GuardrailConfig, GuardrailResult, build_guardrails_from_dict
)


class TestGuardrailsApply:
    def test_empty_output_passes(self):
        g = Guardrails()
        r = g.apply("")
        assert r.passed and r.filtered_output == ""

    def test_clean_output_passes(self):
        g = Guardrails()
        r = g.apply("The weather is nice today.")
        assert r.passed
        assert r.filtered_output == "The weather is nice today."
        assert r.terms_blocked == []
        assert not r.was_truncated

    def test_blocked_term_replaced(self):
        g = Guardrails(GuardrailConfig(blocked_terms=["badword", "secret"]))
        r = g.apply("This contains badword and a secret.")
        assert not r.passed
        assert r.filtered_output == "This contains [FILTERED] and a [FILTERED]."
        assert "badword" in r.terms_blocked
        assert "secret" in r.terms_blocked

    def test_blocked_term_case_insensitive(self):
        g = Guardrails(GuardrailConfig(blocked_terms=["BADWORD"], case_sensitive=False))
        r = g.apply("This has badword in it.")
        assert not r.passed
        assert "[FILTERED]" in r.filtered_output

    def test_blocked_term_case_sensitive(self):
        g = Guardrails(GuardrailConfig(blocked_terms=["BADWORD"], case_sensitive=True))
        r = g.apply("This has badword in it.")
        assert r.passed  # lowercase doesn't match uppercase pattern

    def test_max_length_truncation(self):
        g = Guardrails(GuardrailConfig(max_length=20))
        r = g.apply("A" * 100)
        assert not r.passed
        assert r.was_truncated
        assert len(r.filtered_output) == 20
        assert r.reason == "Truncated to 20 chars"

    def test_max_length_exact_boundary(self):
        g = Guardrails(GuardrailConfig(max_length=50))
        text = "A" * 50
        r = g.apply(text)
        assert r.passed
        assert not r.was_truncated

    def test_combined_block_and_truncate(self):
        g = Guardrails(GuardrailConfig(blocked_terms=["secret"], max_length=30))
        r = g.apply("The secret code is 12345678901234567890")
        assert not r.passed
        assert "[FILTERED]" in r.filtered_output
        assert r.was_truncated

    def test_multiple_occurrences_same_term(self):
        g = Guardrails(GuardrailConfig(blocked_terms=["foo"]))
        r = g.apply("foo bar foo baz foo")
        assert r.filtered_output.count("[FILTERED]") == 3

    def test_empty_blocked_list_passes(self):
        g = Guardrails(GuardrailConfig(blocked_terms=[]))
        r = g.apply("anything goes here")
        assert r.passed

    def test_custom_mask_replacement(self):
        g = Guardrails(GuardrailConfig(blocked_terms=["secret"], mask_replacement="***REMOVED***"))
        r = g.apply("The secret is out.")
        assert "***REMOVED***" in r.filtered_output

    def test_truncation_preserves_words(self):
        """Truncation cuts at max_length, not at word boundary."""
        g = Guardrails(GuardrailConfig(max_length=15))
        r = g.apply("Hello world this is a long sentence")
        assert len(r.filtered_output) == 15


class TestGuardrailsCheck:
    def test_check_returns_bools(self):
        g = Guardrails(GuardrailConfig(blocked_terms=["bad"]))
        assert not g.check("this is bad")
        assert g.check("this is good")

    def test_check_empty(self):
        g = Guardrails()
        assert g.check("")


class TestBuildFromDict:
    def test_no_output_filter(self):
        g = build_guardrails_from_dict({})
        assert g.config.blocked_terms == []
        assert g.config.max_length is None

    def test_with_output_filter(self):
        data = {
            "output_filter": {
                "blocked_terms": ["foo", "bar"],
                "max_length": 500,
                "case_sensitive": True,
            }
        }
        g = build_guardrails_from_dict(data)
        assert g.config.blocked_terms == ["foo", "bar"]
        assert g.config.max_length == 500
        assert g.config.case_sensitive is True

    def test_partial_output_filter(self):
        g = build_guardrails_from_dict({"output_filter": {"max_length": 100}})
        assert g.config.blocked_terms == []
        assert g.config.max_length == 100
