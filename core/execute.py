# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Universal LLM Proxy — execute endpoint with smart routing, cost tracking, and compliance.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Literal
from enum import Enum


class RouterMode(Enum):
    FASTEST = "fastest"           # lowest latency model
    CHEAPEST = "cheapest"         # lowest cost per call
    BALANCED = "balanced"          # weighted latency + cost
    CAPABILITY = "capability"      # match capability tag to model


# Default cost table (USD per 1K tokens) — can be overridden per model registration
DEFAULT_COSTS: dict[str, dict[str, float]] = {
    "gpt-4o":             {"input": 0.0025,  "output": 0.010},
    "gpt-4o-mini":        {"input": 0.00015, "output": 0.0006},
    "gpt-4-turbo":        {"input": 0.010,   "output": 0.030},
    "claude-3-5-sonnet":  {"input": 0.003,   "output": 0.015},
    "claude-3-opus":      {"input": 0.015,   "output": 0.075},
    "claude-3-haiku":     {"input": 0.0008,  "output": 0.0008},
    "claude-3-sonnet":    {"input": 0.003,   "output": 0.015},
    "gemini-1.5-pro":     {"input": 0.00125, "output": 0.005},
    "gemini-1.5-flash":   {"input": 0.000075,"output": 0.0003},
    "llama-3.1-8b-instant": {"input": 0.00005,"output": 0.00005},
    "llama-3.1-70b-versatile": {"input": 0.00035,"output": 0.0007},
    "mixtral-8x7b":       {"input": 0.00024, "output": 0.00024},
    "default":            {"input": 0.001,   "output": 0.002},
}


@dataclass
class ModelProfile:
    """Full profile of a registered model."""
    name: str
    provider: str
    model_id: str
    api_key: str
    latency_ms_p50: int = 500          # expected latency in ms
    capability: str = "any"            # code | vision | fast | precise | any
    cost_input_per_1k: float = 0.001   # USD
    cost_output_per_1k: float = 0.002  # USD
    health_score: float = 1.0           # 0.0–1.0
    failure_count: int = 0
    last_success: float = 0.0          # timestamp
    is_available: bool = True


@dataclass
class ExecutionRequest:
    prompt: str
    system: str = "You are a helpful assistant."
    model: Optional[str] = None         # specific model name, or None for auto-select
    mode: RouterMode = RouterMode.BALANCED
    capability: str = "any"
    max_cost_per_call: Optional[float] = None
    temperature: float = 0.7
    max_tokens: int = 1024
    metadata: dict = field(default_factory=dict)


@dataclass
class ExecutionResult:
    output: str
    model_id: str
    latency_ms: int
    cost: float                        # USD
    router_mode: str
    cached: bool = False
    audit_entry_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    attempt_number: int = 1
    piid_detected: bool = False
    error: str = ""


class ModelSelector:
    """
    Smart model selector. Given a list of available ModelProfiles and an ExecutionRequest,
    picks the best model according to the router mode.
    """

    def __init__(self, models: list[ModelProfile]):
        self.models = [m for m in models if m.is_available]

    def select(self, req: ExecutionRequest) -> Optional[ModelProfile]:
        """Pick the best model for the request."""
        if req.model:
            # Explicit model requested — validate availability
            found = next((m for m in self.models if m.name == req.model), None)
            return found

        if not self.models:
            return None

        if req.mode == RouterMode.FASTEST:
            return self._fastest()
        elif req.mode == RouterMode.CHEAPEST:
            return self._cheapest()
        elif req.mode == RouterMode.CAPABILITY:
            return self._capability(req.capability)
        else:  # BALANCED
            return self._balanced()

    def _fastest(self) -> ModelProfile:
        candidates = sorted(self.models, key=lambda m: m.latency_ms_p50)
        # Filter out models that are unhealthy (failure_count > 5)
        for m in candidates:
            if m.failure_count < 5:
                return m
        return candidates[0] if candidates else self.models[0]

    def _cheapest(self) -> ModelProfile:
        def call_cost(m: ModelProfile) -> float:
            # Rough estimate: 100 input + 50 output tokens
            return (100/1000 * m.cost_input_per_1k) + (50/1000 * m.cost_output_per_1k)
        candidates = sorted(self.models, key=call_cost)
        for m in candidates:
            if m.failure_count < 5:
                return m
        return candidates[0] if candidates else self.models[0]

    def _capability(self, capability: str) -> ModelProfile:
        # Prefer models with matching capability tag
        tagged = [m for m in self.models
                  if m.capability == capability and m.failure_count < 5]
        if tagged:
            return min(tagged, key=lambda m: m.latency_ms_p50)
        # Fall back to any available
        available = [m for m in self.models if m.failure_count < 5]
        return available[0] if available else self.models[0]

    def _balanced(self) -> ModelProfile:
        """
        Weighted score: 40% latency, 60% cost.
        Normalize each to 0-1 range across available models.
        """
        if not self.models:
            return None
        available = [m for m in self.models if m.failure_count < 5]
        if not available:
            available = self.models

        # Normalization ranges
        min_lat = min(m.latency_ms_p50 for m in available)
        max_lat = max(m.latency_ms_p50 for m in available)
        lat_range = max(max_lat - min_lat, 1)

        def call_cost(m: ModelProfile) -> float:
            return (100/1000 * m.cost_input_per_1k) + (50/1000 * m.cost_output_per_1k)

        min_cost = min(call_cost(m) for m in available)
        max_cost = max(call_cost(m) for m in available)
        cost_range = max(max_cost - min_cost, 0.0001)

        best = None
        best_score = float("inf")
        for m in available:
            norm_lat = (m.latency_ms_p50 - min_lat) / lat_range
            norm_cost = (call_cost(m) - min_cost) / cost_range
            score = 0.4 * norm_lat + 0.6 * norm_cost
            if score < best_score:
                best_score = score
                best = m
        return best or available[0]

    def get_fallback_order(self, primary: ModelProfile) -> list[ModelProfile]:
        """Return remaining models in priority order, excluding primary."""
        others = [m for m in self.models if m.name != primary.name and m.failure_count < 5]
        # Sort by balanced score
        if self._balanced() in others:
            try:
                others.remove(self._balanced())
                others.insert(0, self._balanced())
            except ValueError:
                pass
        return others


def estimate_cost(model: ModelProfile, input_tokens: int, output_tokens: int) -> float:
    """Calculate USD cost for a call."""
    return (input_tokens / 1000 * model.cost_input_per_1k) + \
           (output_tokens / 1000 * model.cost_output_per_1k)


def estimate_tokens(prompt: str, system: str = "") -> int:
    """
    Rough token estimate: ~4 chars per token for English text.
    More accurate in production with tiktoken/octomark.
    """
    text = (system + " " + prompt) if system else prompt
    return max(1, len(text) // 4)
