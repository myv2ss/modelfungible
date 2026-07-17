# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.
# Commercial use requires a license. Unauthorized use is prohibited.

"""
ModelFungible — Universal AI Model Execution Layer

Core principle: "Models are reasoning engines. Data is truth. Prompts are adapters."
Same strategy + same context → same decisions on any model.

Usage:
    from modelfungible import ModelExecutor, ContextBuilder, RulesEngine

    rules = RulesEngine("strategy_rules.json")
    ctx = ContextBuilder().build(role="analyst")
    executor = ModelExecutor()
    executor.add_model("fast", "groq", "llama-3.1-8b-instant", api_key="...")
    result = executor.run(prompt=cb.build_prompt(ctx, "strategy_id", rules), model="fast")
"""

from modelfungible.core.rules_engine import RulesEngine, StrategyValidationError
from modelfungible.core.context_builder import ContextBuilder, ContextPacket
from modelfungible.core.executor import ModelExecutor, ExecutionResult
from modelfungible.core.cost_router import (
    CostRouter,
    ModelProfile,
    HealthChecker,
    GROQ_FREE_PROFILES,
)
from modelfungible.adapters.base import AdapterError, ParsedOutput
from modelfungible.enterprise.license import LicenseKey, LicenseGenerator
from modelfungible.core.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    RetryWithBackoff,
    RetryExhausted,
    is_retryable_error,
    call_with_protection,
)

__all__ = [
    # Core
    "ModelExecutor",
    "ExecutionResult",
    "ContextBuilder",
    "ContextPacket",
    "RulesEngine",
    "StrategyValidationError",
    # Cost routing
    "CostRouter",
    "ModelProfile",
    "HealthChecker",
    "GROQ_FREE_PROFILES",
    # Adapters
    "AdapterError",
    "ParsedOutput",
    # Enterprise
    "LicenseKey",
    "LicenseGenerator",
]

__version__ = "0.1.0"
