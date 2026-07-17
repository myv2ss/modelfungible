#!/usr/bin/env python3
"""
Unit tests for Strategy Rules Engine.
Tests: validation, regime-based lookup, sizing, exit rules.
"""
import pytest, json, tempfile, os
from pathlib import Path

# Import the module we're testing
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from modelfungible.core.rules_engine import RulesEngine, StrategyValidationError


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────
@pytest.fixture
def valid_rules():
    return {
        "_meta": {"version": "1.0"},
        "EQM": {
            "strategy_id": "EQM",
            "version": "1.0.0",
            "name": "Earnings Quality Momentum",
            "entry_trigger": "EQM_score >= 60",
            "sizing": {
                "CONFIRMED_BULL":  {"amount": 4500, "max_positions": 3},
                "MODERATE_BULL":   {"amount": 2250, "max_positions": 2},
                "NEUTRAL":         {"amount": 1500, "max_positions": 1},
                "RECOVERY":        {"amount": 1000, "max_positions": 1},
                "BEAR":            {"amount": 0,    "max_positions": 0}
            },
            "stop_loss_pct": 0.08,
            "target_gain_pct": 0.15,
            "exit": [
                {"type": "stop_loss",     "pct": -8},
                {"type": "time",         "trading_days": 20},
                {"type": "trailing_stop", "pct": -5, "after_gain_pct": 10}
            ],
            "signal_output_schema": {
                "ticker":    "string",
                "direction": "string",
                "size":      "number",
                "stop":      "number",
                "target":    "number",
                "reason":    "string"
            }
        }
    }


@pytest.fixture
def rules_file(valid_rules):
    """Create a temp file with valid rules."""
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(valid_rules, f)
    yield path
    os.unlink(path)


# ─────────────────────────────────────────────────────────────────
# Tests: Loading
# ─────────────────────────────────────────────────────────────────
class TestRulesLoading:
    def test_load_from_file(self, rules_file, valid_rules):
        engine = RulesEngine(rules_file)
        assert engine.get("EQM") == valid_rules["EQM"]

    def test_load_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            RulesEngine("/nonexistent/path/rules.json")

    def test_load_empty_file_raises(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.write(fd, b"{}")
        os.close(fd)
        with pytest.raises(StrategyValidationError):
            RulesEngine(path)
        os.unlink(path)

    def test_load_invalid_json_raises(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.write(fd, b"not valid json")
        os.close(fd)
        with pytest.raises(StrategyValidationError):
            RulesEngine(path)
        os.unlink(path)

    def test_list_strategies(self, rules_file):
        engine = RulesEngine(rules_file)
        assert "EQM" in engine.list_strategies()
        assert "_meta" not in engine.list_strategies()


# ─────────────────────────────────────────────────────────────────
# Tests: Strategy validation
# ─────────────────────────────────────────────────────────────────
class TestStrategyValidation:
    def test_valid_strategy_passes(self, rules_file):
        engine = RulesEngine(rules_file)
        # Should not raise
        engine.validate("EQM")

    def test_unknown_strategy_raises(self, rules_file):
        engine = RulesEngine(rules_file)
        with pytest.raises(StrategyValidationError, match="not found"):
            engine.validate("UNKNOWN_STRATEGY")

    def test_missing_required_field_entry_trigger(self, valid_rules):
        del valid_rules["EQM"]["entry_trigger"]
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(valid_rules, f)
        engine = RulesEngine(path)
        with pytest.raises(StrategyValidationError, match="entry_trigger"):
            engine.validate("EQM")
        os.unlink(path)

    def test_missing_required_field_sizing(self, valid_rules):
        del valid_rules["EQM"]["sizing"]
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(valid_rules, f)
        engine = RulesEngine(path)
        with pytest.raises(StrategyValidationError, match="sizing"):
            engine.validate("EQM")
        os.unlink(path)

    def test_empty_sizing_dict_raises(self, valid_rules):
        valid_rules["EQM"]["sizing"] = {}
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(valid_rules, f)
        engine = RulesEngine(path)
        with pytest.raises(StrategyValidationError, match="sizing"):
            engine.validate("EQM")
        os.unlink(path)


# ─────────────────────────────────────────────────────────────────
# Tests: Regime-based sizing
# ─────────────────────────────────────────────────────────────────
class TestSizingLookup:
    def test_exact_regime_match(self, rules_file):
        engine = RulesEngine(rules_file)
        sizing = engine.get_sizing("EQM", "CONFIRMED_BULL")
        assert sizing["amount"] == 4500
        assert sizing["max_positions"] == 3

    def test_fallback_regime_NEUTRAL(self, rules_file):
        engine = RulesEngine(rules_file)
        sizing = engine.get_sizing("EQM", "NEUTRAL")
        assert sizing["amount"] == 1500
        assert sizing["max_positions"] == 1

    def test_zero_size_when_bear(self, rules_file):
        engine = RulesEngine(rules_file)
        sizing = engine.get_sizing("EQM", "BEAR")
        assert sizing["amount"] == 0
        assert sizing["max_positions"] == 0

    def test_unknown_regime_uses_default(self, rules_file):
        engine = RulesEngine(rules_file)
        sizing = engine.get_sizing("EQM", "RANDOM_UNKNOWN")
        # Should fall back to NEUTRAL sizing
        assert sizing["amount"] == 1500

    def test_bear_zero_position_blocks_new_entries(self, rules_file):
        engine = RulesEngine(rules_file)
        sizing = engine.get_sizing("EQM", "BEAR")
        assert sizing["max_positions"] == 0
        assert sizing["amount"] == 0


# ─────────────────────────────────────────────────────────────────
# Tests: Exit rules
# ─────────────────────────────────────────────────────────────────
class TestExitRules:
    def test_get_exit_rules(self, rules_file):
        engine = RulesEngine(rules_file)
        exits = engine.get_exit_rules("EQM")
        assert len(exits) == 3
        types = [e["type"] for e in exits]
        assert "stop_loss" in types
        assert "time" in types
        assert "trailing_stop" in types

    def test_get_stop_loss(self, rules_file):
        engine = RulesEngine(rules_file)
        stop = engine.get_stop_loss("EQM")
        assert stop is not None
        assert stop["pct"] == -8

    def test_get_target(self, rules_file):
        engine = RulesEngine(rules_file)
        target = engine.get_target("EQM")
        assert target is not None
        assert target["gain_pct"] == 15


# ─────────────────────────────────────────────────────────────────
# Tests: Output schema
# ─────────────────────────────────────────────────────────────────
class TestOutputSchema:
    def test_get_output_schema(self, rules_file):
        engine = RulesEngine(rules_file)
        schema = engine.get_output_schema("EQM")
        assert schema["ticker"] == "string"
        assert schema["direction"] == "string"
        assert schema["size"] == "number"

    def test_validate_valid_output(self, rules_file):
        engine = RulesEngine(rules_file)
        valid_output = {
            "ticker": "ADBE",
            "direction": "LONG",
            "size": 4500,
            "stop": 207.55,
            "target": 276.73,
            "reason": "Highest EQM score"
        }
        # Should not raise
        errors = engine.validate_output("EQM", valid_output)
        assert errors == []

    def test_validate_missing_required_field(self, rules_file):
        engine = RulesEngine(rules_file)
        invalid_output = {
            "ticker": "ADBE",
            # missing direction, size, etc.
        }
        errors = engine.validate_output("EQM", invalid_output)
        assert len(errors) > 0
        assert any("direction" in e for e in errors)

    def test_validate_wrong_type(self, rules_file):
        engine = RulesEngine(rules_file)
        invalid_output = {
            "ticker": "ADBE",
            "direction": "LONG",
            "size": "not_a_number",  # should be number
            "stop": 207.55,
            "target": 276.73,
            "reason": "Highest EQM"
        }
        errors = engine.validate_output("EQM", invalid_output)
        assert len(errors) > 0


# ─────────────────────────────────────────────────────────────────
# Tests: Full pipeline
# ─────────────────────────────────────────────────────────────────
class TestFullPipeline:
    def test_complete_strategy_flow(self, rules_file):
        engine = RulesEngine(rules_file)

        # Validate
        engine.validate("EQM")

        # Get sizing
        sizing = engine.get_sizing("EQM", "CONFIRMED_BULL")
        assert sizing["amount"] == 4500

        # Get exits
        exits = engine.get_exit_rules("EQM")
        assert len(exits) == 3

        # Validate output
        output = {
            "ticker": "ADBE", "direction": "LONG",
            "size": 4500, "stop": 207, "target": 276, "reason": "Test"
        }
        errors = engine.validate_output("EQM", output)
        assert errors == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
