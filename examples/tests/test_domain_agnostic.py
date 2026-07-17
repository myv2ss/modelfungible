# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Tests for domain-agnostic example strategies.

Verifies that the example strategy files are valid JSON, parseable
by RulesEngine, and represent non-trading domains.
"""
import pytest, json, tempfile, os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


# ─────────────────────────────────────────────────────────────────
# Test data: example strategy files
# ─────────────────────────────────────────────────────────────────
EXAMPLES_DIR = Path(__file__).parent.parent.parent / "examples"


class TestExampleStrategies:
    """All example strategies should be valid and domain-agnostic."""

    def test_contract_risk_strategy_exists(self):
        path = EXAMPLES_DIR / "strategies" / "contract_risk.json"
        assert path.exists(), f"Missing: {path}"

    def test_contract_risk_is_valid_json(self):
        from modelfungible.core.rules_engine import RulesEngine
        path = EXAMPLES_DIR / "strategies" / "contract_risk.json"
        with open(path) as f:
            data = json.load(f)
        # File is wrapped: {"contract_risk": {...}} — strategy is in values
        assert "contract_risk" in data, "Expected top-level key: contract_risk"
        inner = data["contract_risk"]
        assert "strategy_id" in inner
        assert "entry_trigger" in inner
        assert "signal_output_schema" in inner

    def test_contract_risk_validates(self):
        from modelfungible.core.rules_engine import RulesEngine
        path = EXAMPLES_DIR / "strategies" / "contract_risk.json"
        engine = RulesEngine(str(path))
        errors = engine.validate("contract_risk")
        assert errors == [], f"Validation errors: {errors}"

    def test_contract_risk_has_no_trading_terms(self):
        path = EXAMPLES_DIR / "strategies" / "contract_risk.json"
        content = open(path).read().lower()
        trading_terms = ["vix", "spy", "qqq", "pnl", "ticker", "bull_regime",
                         "bear_regime", "long_position", "short_position"]
        found = [t for t in trading_terms if t in content]
        assert not found, f"Found trading terms: {found}"

    def test_clinical_notes_strategy_exists(self):
        path = EXAMPLES_DIR / "strategies" / "clinical_notes.json"
        assert path.exists()

    def test_clinical_notes_is_valid_json(self):
        from modelfungible.core.rules_engine import RulesEngine
        path = EXAMPLES_DIR / "strategies" / "clinical_notes.json"
        with open(path) as f:
            data = json.load(f)
        assert "clinical_notes" in data
        inner = data["clinical_notes"]
        assert "strategy_id" in inner
        assert "signal_output_schema" in inner

    def test_clinical_notes_validates(self):
        from modelfungible.core.rules_engine import RulesEngine
        path = EXAMPLES_DIR / "strategies" / "clinical_notes.json"
        engine = RulesEngine(str(path))
        errors = engine.validate("clinical_notes")
        assert errors == [], f"Validation errors: {errors}"

    def test_clinical_notes_has_no_trading_terms(self):
        path = EXAMPLES_DIR / "strategies" / "clinical_notes.json"
        content = open(path).read().lower()
        trading_terms = ["vix", "spy", "qqq", "pnl", "ticker", "bull_regime",
                         "bear_regime", "long_position", "short_position"]
        found = [t for t in trading_terms if t in content]
        assert not found, f"Found trading terms: {found}"

    def test_resume_screening_strategy_exists(self):
        path = EXAMPLES_DIR / "strategies" / "resume_screening.json"
        assert path.exists()

    def test_resume_screening_validates(self):
        from modelfungible.core.rules_engine import RulesEngine
        path = EXAMPLES_DIR / "strategies" / "resume_screening.json"
        engine = RulesEngine(str(path))
        errors = engine.validate("resume_screening")
        assert errors == [], f"Validation errors: {errors}"

    def test_resume_screening_has_no_trading_terms(self):
        path = EXAMPLES_DIR / "strategies" / "resume_screening.json"
        content = open(path).read().lower()
        trading_terms = ["vix", "spy", "qqq", "pnl", "ticker", "bull_regime",
                         "bear_regime", "long_position", "short_position"]
        found = [t for t in trading_terms if t in content]
        assert not found, f"Found trading terms: {found}"


class TestDomainFactsFiles:
    """Example facts/context files for non-trading domains."""

    def test_legal_context_exists(self):
        path = EXAMPLES_DIR / "facts" / "legal_context.json"
        assert path.exists()

    def test_legal_context_loads(self):
        from modelfungible.core.context_builder import ContextBuilder
        path = EXAMPLES_DIR / "facts" / "legal_context.json"
        cb = ContextBuilder(facts_file=str(path))
        ctx = cb.build(role="analyst")
        assert ctx.context is not None
        assert ctx.context != {}
        # Should NOT have trading market data
        assert ctx.market == {} or "regime" not in ctx.market

    def test_legal_context_contains_contracts(self):
        from modelfungible.core.context_builder import ContextBuilder
        path = EXAMPLES_DIR / "facts" / "legal_context.json"
        cb = ContextBuilder(facts_file=str(path))
        ctx = cb.build(role="analyst")
        assert "contracts" in ctx.context or "documents" in ctx.context

    def test_healthcare_context_exists(self):
        path = EXAMPLES_DIR / "facts" / "healthcare_context.json"
        assert path.exists()

    def test_healthcare_context_loads(self):
        from modelfungible.core.context_builder import ContextBuilder
        path = EXAMPLES_DIR / "facts" / "healthcare_context.json"
        cb = ContextBuilder(facts_file=str(path))
        ctx = cb.build(role="analyst")
        assert ctx.context is not None
        assert ctx.context != {}

    def test_healthcare_context_contains_patient_data(self):
        from modelfungible.core.context_builder import ContextBuilder
        path = EXAMPLES_DIR / "facts" / "healthcare_context.json"
        cb = ContextBuilder(facts_file=str(path))
        ctx = cb.build(role="analyst")
        keys = list(ctx.context.keys())
        assert len(keys) > 0


class TestContextBuilderDomainAgnostic:
    """ContextBuilder should work without any trading data."""

    def test_build_with_empty_context(self):
        from modelfungible.core.context_builder import ContextBuilder
        cb = ContextBuilder()
        ctx = cb.build(role="analyst")
        assert ctx.context == {}
        assert ctx.market == {}
        assert ctx.positions == []
        assert ctx.role == "analyst"

    def test_build_with_domain_data_override(self):
        from modelfungible.core.context_builder import ContextBuilder
        cb = ContextBuilder()
        ctx = cb.build(
            role="analyst",
            domain_data={
                "patient_id": "P12345",
                "diagnosis": "Type 2 Diabetes",
                "medications": ["Metformin 500mg"],
                "age": 58,
            }
        )
        assert ctx.context["patient_id"] == "P12345"
        assert ctx.context["diagnosis"] == "Type 2 Diabetes"
        assert ctx.context["age"] == 58

    def test_domain_context_in_prompt(self):
        from modelfungible.core.context_builder import ContextBuilder
        cb = ContextBuilder()
        ctx = cb.build(
            role="analyst",
            domain_data={"contract_id": "C-999", "risk_flags": ["missing_indemnity"]}
        )
        rules = {
            "name": "Contract Risk",
            "entry_trigger": "risk_score >= 0.7",
            "signal_output_schema": {"risk_score": "number"},
        }
        prompt = cb.build_prompt(ctx, "contract_risk", rules)
        assert "C-999" in prompt
        assert "missing_indemnity" in prompt
        # Should NOT contain trading terms
        assert "VIX" not in prompt
        assert "SPY" not in prompt
        assert "bull_regime" not in prompt

    def test_prompt_contains_only_domain_context(self):
        from modelfungible.core.context_builder import ContextBuilder
        cb = ContextBuilder()
        ctx = cb.build(
            role="analyst",
            domain_data={"resume_id": "R-001", "skills": ["Python", "AWS"]}
        )
        rules = {
            "name": "Resume Screening",
            "entry_trigger": "score >= 80",
            "signal_output_schema": {"score": "number"},
        }
        prompt = cb.build_prompt(ctx, "resume_screening", rules)
        assert "R-001" in prompt
        assert "Python" in prompt
        assert "AWS" in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
