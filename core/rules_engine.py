#!/usr/bin/env python3
"""
Strategy Rules Engine — ModelFungible Core

Loads, validates, and queries strategy rules as machine-readable JSON.
Strategy rules are the contract: any model following the same rules
produces the same decisions.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────
class StrategyValidationError(Exception):
    """Raised when a strategy is invalid or not found."""
    pass


# ─────────────────────────────────────────────────────────────────
# Regime size defaults (fallback when regime is unknown)
# ─────────────────────────────────────────────────────────────────
DEFAULT_REGIME = "NEUTRAL"


# ─────────────────────────────────────────────────────────────────
# RulesEngine
# ─────────────────────────────────────────────────────────────────
class RulesEngine:
    """
    Loads and queries strategy rules from a JSON file.

    Example:
        engine = RulesEngine("strategy_rules.json")
        engine.validate("EQM")
        sizing = engine.get_sizing("EQM", "CONFIRMED_BULL")
        exits  = engine.get_exit_rules("EQM")
        errors = engine.validate_output("EQM", {"ticker": "ADBE", ...})
    """

    REQUIRED_FIELDS = {
        "strategy_id", "name", "entry_trigger", "sizing"
    }

    REQUIRED_SIZING_FIELDS = {
        "amount", "max_positions"
    }

    def __init__(self, rules_path: str | Path):
        self.path = Path(rules_path)
        self._rules = self._load()

    # ── Loading ─────────────────────────────────────────────────

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(f"Rules file not found: {self.path}")
        except json.JSONDecodeError as e:
            raise StrategyValidationError(f"Invalid JSON in rules file: {e}")

        if not raw:
            raise StrategyValidationError("Rules file is empty")

        return raw

    # ── Query ──────────────────────────────────────────────────

    def list_strategies(self) -> list[str]:
        """Return list of strategy IDs (excluding _meta)."""
        return [k for k in self._rules.keys() if k != "_meta"]

    def get(self, strategy_id: str) -> dict:
        """Return raw strategy dict."""
        if strategy_id == "_meta" or strategy_id not in self._rules:
            raise StrategyValidationError(f"Strategy '{strategy_id}' not found")
        return self._rules[strategy_id]

    def get_raw(self) -> dict:
        """Return all rules (including _meta)."""
        return self._rules

    # ── Validation ──────────────────────────────────────────────

    def validate(self, strategy_id: str) -> list[str]:
        """
        Validate a strategy. Returns list of error messages (empty = valid).
        Raises StrategyValidationError for critical errors.
        """
        errors = []

        try:
            strategy = self.get(strategy_id)
        except StrategyValidationError:
            raise

        # Required top-level fields
        for field in self.REQUIRED_FIELDS:
            if field not in strategy:
                errors.append(f"Missing required field: '{field}'")

        # Sizing must have at least one regime
        sizing = strategy.get("sizing", {})
        if not sizing:
            errors.append("Missing required field: 'sizing'")
        elif not isinstance(sizing, dict):
            errors.append("'sizing' must be a dict")
        elif len(sizing) == 0:
            errors.append("'sizing' must have at least one regime entry")

        # Each regime must have required fields
        if isinstance(sizing, dict):
            for regime, config in sizing.items():
                if not isinstance(config, dict):
                    errors.append(f"Sizing regime '{regime}' must be a dict")
                    continue
                for field in self.REQUIRED_SIZING_FIELDS:
                    if field not in config:
                        errors.append(
                            f"Regime '{regime}' missing field: '{field}'"
                        )
                if "amount" in config and config["amount"] < 0:
                    errors.append(f"Regime '{regime}' amount cannot be negative")

        if errors:
            msg = f"Validation errors for '{strategy_id}': " + "; ".join(errors)
            raise StrategyValidationError(msg)

        return errors

    # ── Sizing ─────────────────────────────────────────────────

    def get_sizing(self, strategy_id: str, regime: str) -> dict:
        """
        Return sizing config for a given regime.
        Falls back to NEUTRAL if regime not found.
        """
        strategy = self.get(strategy_id)
        sizing = strategy.get("sizing", {})

        # Try exact match
        if regime in sizing:
            return sizing[regime]

        # Try case-insensitive
        for key in sizing:
            if key.upper() == regime.upper():
                return sizing[key]

        # Fall back to NEUTRAL or first available
        if DEFAULT_REGIME in sizing:
            return sizing[DEFAULT_REGIME]
        if len(sizing) > 0:
            return next(iter(sizing.values()))

        # Return a zero-sized default
        return {"amount": 0, "max_positions": 0}

    # ── Exit rules ─────────────────────────────────────────────

    def get_exit_rules(self, strategy_id: str) -> list[dict]:
        """Return the list of exit rules for a strategy."""
        strategy = self.get(strategy_id)
        return list(strategy.get("exit", []))

    def get_stop_loss(self, strategy_id: str) -> Optional[dict]:
        """Return the stop-loss exit rule if defined."""
        for rule in self.get_exit_rules(strategy_id):
            if rule.get("type") in ("stop_loss", "trailing_stop"):
                return rule
        # Fall back to top-level stop_loss_pct
        strategy = self.get(strategy_id)
        pct = strategy.get("stop_loss_pct")
        if pct:
            return {"type": "stop_loss", "pct": -abs(float(pct)) * 100}
        return None

    def get_target(self, strategy_id: str) -> Optional[dict]:
        """Return the target/gain exit rule if defined."""
        for rule in self.get_exit_rules(strategy_id):
            if rule.get("type") == "gain":
                return rule
        # Fall back to top-level target_gain_pct
        strategy = self.get(strategy_id)
        pct = strategy.get("target_gain_pct")
        if pct:
            return {"type": "gain", "gain_pct": float(pct) * 100}
        return None

    # ── Output schema ──────────────────────────────────────────

    def get_output_schema(self, strategy_id: str) -> dict:
        """Return the output schema for a strategy."""
        strategy = self.get(strategy_id)
        return dict(strategy.get("signal_output_schema", {}))

    def validate_output(self, strategy_id: str, output: dict) -> list[str]:
        """
        Validate a model's output against the strategy's output schema.
        Returns list of error messages (empty = valid).
        """
        errors = []
        schema = self.get_output_schema(strategy_id)

        if not schema:
            return errors  # No schema = no validation

        for field, expected_type in schema.items():
            if field not in output:
                errors.append(f"Missing required field: '{field}'")
                continue

            value = output[field]
            if expected_type == "number":
                if not isinstance(value, (int, float)):
                    errors.append(f"Field '{field}' must be a number, got {type(value).__name__}")
            elif expected_type == "string":
                if not isinstance(value, str):
                    errors.append(f"Field '{field}' must be a string, got {type(value).__name__}")
            elif expected_type == "boolean":
                if not isinstance(value, bool):
                    errors.append(f"Field '{field}' must be a boolean, got {type(value).__name__}")
            elif expected_type == "array":
                if not isinstance(value, list):
                    errors.append(f"Field '{field}' must be an array, got {type(value).__name__}")

        return errors
