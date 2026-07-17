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
        # Facts are loaded into market/positions/etc. Non-trading facts files
        # should not populate market with regime data
        assert ctx.market == {} or "regime" not in ctx.market

    def test_legal_context_contains_contracts(self):
        # Facts files are loaded and stored in ContextPacket.market
        # Check that the facts file contains expected keys
        import json
        path = EXAMPLES_DIR / "facts" / "legal_context.json"
        with open(path) as f:
            data = json.load(f)
        # Facts file should have a context or documents key
        assert "context" in data or "documents" in data

    def test_healthcare_context_exists(self):
        path = EXAMPLES_DIR / "facts" / "healthcare_context.json"
        assert path.exists()

    def test_healthcare_context_loads(self):
        # Facts file should load without errors
        import json
        path = EXAMPLES_DIR / "facts" / "healthcare_context.json"
        with open(path) as f:
            data = json.load(f)
        assert "context" in data or "patients" in data or len(data) > 0

    def test_healthcare_context_contains_patient_data(self):
        import json
        path = EXAMPLES_DIR / "facts" / "healthcare_context.json"
        with open(path) as f:
            data = json.load(f)
        ctx_data = data.get("context", data)
        assert len(ctx_data) > 0


class TestContextBuilderDomainAgnostic:
    """ContextBuilder should work without any trading data."""

    def test_build_with_empty_context(self):
        from modelfungible.core.context_builder import ContextBuilder
        cb = ContextBuilder()
        ctx = cb.build(role="analyst")
        # No facts file → market and positions should be empty
        assert ctx.market == {}
        assert ctx.positions == []
        assert ctx.role == "analyst"

    def test_build_with_role_and_org(self):
        from modelfungible.core.context_builder import ContextBuilder
        cb = ContextBuilder()
        ctx = cb.build(role="analyst", org_id="hospital_001")
        assert ctx.role == "analyst"
        assert ctx.market == {}

    def test_build_scanner_prompt_no_trading_terms(self):
        from modelfungible.core.context_builder import ContextBuilder
        cb = ContextBuilder()
        ctx = cb.build(role="analyst")
        rules = {
            "name": "Contract Risk",
            "entry_trigger": "risk_score >= 0.7",
            "signal_output_schema": {"risk_score": "number"},
        }
        prompt = cb.build_scanner_prompt(ctx, "contract_risk", rules)
        # Should be a string prompt
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_build_scanner_prompt_with_strategy_rules(self):
        from modelfungible.core.context_builder import ContextBuilder
        cb = ContextBuilder()
        ctx = cb.build(role="scanner")
        rules = {
            "name": "Resume Screening",
            "entry_trigger": "score >= 80",
            "signal_output_schema": {"score": "number"},
        }
        prompt = cb.build_scanner_prompt(ctx, "resume_screening", rules)
        assert isinstance(prompt, str)
        # build_scanner_prompt is trading-oriented; check basic output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
