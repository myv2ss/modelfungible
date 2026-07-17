# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Cost and Latency Router — ModelFungible Core

Automatically selects the best model for a given task based on:
- Latency (fastest)
- Cost (cheapest)
- Balanced (latency + cost)
- Capability (task-specific routing)

Also tracks model health and auto-skips failing models.

Usage:
    from modelfungible.core.cost_router import CostRouter, ModelProfile

    profiles = [
        ModelProfile(name="fast", provider="groq", model_id="llama-3.1-8b",
                    cost_per_1k_input=0.0, cost_per_1k_output=0.0,
                    latency_ms_p50=150, latency_ms_p95=300, capability="fast"),
        ModelProfile(name="precise", provider="anthropic", model_id="claude-3-5-sonnet",
                    cost_per_1k_input=0.003, cost_per_1k_output=0.015,
                    latency_ms_p50=600, latency_ms_p95=1200, capability="precise"),
    ]

    router = CostRouter(profiles, mode="balanced")
    best = router.get_model()  # returns ModelProfile
    router.record_outcome(best.name, success=True, latency_ms=180)
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional
from collections import deque


# ─────────────────────────────────────────────────────────────────
# ModelProfile
# ─────────────────────────────────────────────────────────────────
@dataclass
class ModelProfile:
    """
    Static profile of a model's cost and latency characteristics.

    Args:
        name:             unique identifier for this model in the router
        provider:         "openai" | "anthropic" | "groq" | "vertexai" | "ollama"
        model_id:         provider's model identifier (e.g. "gpt-4o")
        cost_per_1k_input:  cost per 1,000 input tokens (USD)
        cost_per_1k_output: cost per 1,000 output tokens (USD)
        latency_ms_p50:   median latency in milliseconds
        latency_ms_p95:   95th percentile latency in milliseconds
        capability:       "fast" | "precise" | "coder" | "reasoner" | "any"
    """
    name: str
    provider: str
    model_id: str
    cost_per_1k_input: float
    cost_per_1k_output: float
    latency_ms_p50: int
    latency_ms_p95: int
    capability: str = "any"

    def __eq__(self, other):
        if not isinstance(other, ModelProfile):
            return False
        return (
            self.name == other.name
            and self.provider == other.provider
            and self.model_id == other.model_id
        )

    def __hash__(self):
        return hash((self.name, self.provider, self.model_id))

    def estimated_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate total cost for a call with given token counts."""
        in_cost = (input_tokens / 1000) * self.cost_per_1k_input
        out_cost = (output_tokens / 1000) * self.cost_per_1k_output
        return in_cost + out_cost


# ─────────────────────────────────────────────────────────────────
# HealthChecker
# ─────────────────────────────────────────────────────────────────
class HealthChecker:
    """
    Tracks model health via a sliding window of success/failure records.

    A model is "healthy" if its success rate in the window exceeds the threshold.
    Failed models are auto-skipped by CostRouter.
    """

    def __init__(
        self,
        window: int = 10,
        success_rate_threshold: float = 0.5,
        failure_threshold: int = 3,
    ):
        """
        Args:
            window:                  number of recent calls to track per model
            success_rate_threshold:  fraction of successes required (0.0-1.0)
            failure_threshold:       consecutive failures that mark a model unhealthy
        """
        self.window = window
        self.success_rate_threshold = success_rate_threshold
        self.failure_threshold = failure_threshold
        # {model_name: deque of (success: bool, latency_ms: float, timestamp: float)}
        self._records: dict[str, deque] = {}

    def record(self, model_name: str, success: bool, latency_ms: float) -> None:
        """Record a call outcome for a model."""
        if model_name not in self._records:
            self._records[model_name] = deque(maxlen=self.window)
        self._records[model_name].append((success, latency_ms, time.time()))

    def is_healthy(self, model_name: str) -> bool:
        """Return True if model is healthy (passes success rate + consecutive failure check)."""
        if model_name not in self._records:
            return True  # New models are healthy by default

        records = self._records[model_name]
        if not records:
            return True

        # Check consecutive failures first
        consecutive = 0
        for success, _, _ in reversed(records):
            if not success:
                consecutive += 1
            else:
                break
        if consecutive >= self.failure_threshold:
            return False

        # Check overall success rate
        n = len(records)
        successes = sum(1 for s, _, _ in records if s)
        return (successes / n) >= self.success_rate_threshold

    def get_success_rate(self, model_name: str) -> float:
        """Return success rate as a fraction (0.0-1.0). Returns 1.0 for unknown models."""
        if model_name not in self._records or not self._records[model_name]:
            return 1.0
        records = self._records[model_name]
        successes = sum(1 for s, _, _ in records if s)
        return successes / len(records)

    def get_latency_p95(self, model_name: str) -> Optional[float]:
        """Return 95th percentile latency from recorded calls. None if no data."""
        if model_name not in self._records or not self._records[model_name]:
            return None
        records = self._records[model_name]
        latencies = sorted(lat for _, lat, _ in records if lat > 0)
        if not latencies:
            return 0.0
        idx = int(len(latencies) * 0.95)
        idx = min(idx, len(latencies) - 1)
        return latencies[idx]

    def get_healthy_models(self, profiles: list[ModelProfile]) -> list[ModelProfile]:
        """Return only models that are currently healthy."""
        return [p for p in profiles if self.is_healthy(p.name)]

    def reset(self, model_name: str) -> None:
        """Clear health history for a model."""
        if model_name in self._records:
            del self._records[model_name]


# ─────────────────────────────────────────────────────────────────
# CostRouter
# ─────────────────────────────────────────────────────────────────
class CostRouter:
    """
    Automatically selects the best model based on mode and health.

    Modes:
        fastest:    lowest p50 latency (best for real-time)
        cheapest:   lowest cost per call (best for batch)
        balanced:  equal weight of latency + cost (default)
        capability: pick best model for a specific capability

    Usage:
        router = CostRouter(profiles, mode="balanced")
        best = router.get_model(capability_required="precise")
        router.record_outcome(best.name, success=True, latency_ms=180)
    """

    def __init__(
        self,
        profiles: list[ModelProfile],
        mode: str = "balanced",
        health_checker: Optional[HealthChecker] = None,
    ):
        """
        Args:
            profiles:          list of ModelProfile for available models
            mode:              "fastest" | "cheapest" | "balanced" | "capability"
            health_checker:    optional HealthChecker for auto-skipping failing models
        """
        self.profiles = {p.name: p for p in profiles}
        self.mode = mode
        self.health = health_checker or HealthChecker()

    # ── Selection ────────────────────────────────────────────────

    def get_model(
        self,
        capability_required: Optional[str] = None,
    ) -> Optional[ModelProfile]:
        """
        Select the best model based on current mode and health.

        Args:
            capability_required: filter to models with this capability
                               (only used in "capability" mode)

        Returns:
            ModelProfile, or None if no healthy models available.
        """
        candidates = self._filter_candidates(capability_required)
        if not candidates:
            return None

        if self.mode == "fastest":
            return self._select_fastest(candidates)
        elif self.mode == "cheapest":
            return self._select_cheapest(candidates)
        elif self.mode == "capability":
            return self._select_capability(candidates, capability_required)
        else:  # balanced (default)
            return self._select_balanced(candidates)

    def get_cost_estimate(
        self,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
    ) -> Optional[float]:
        """Return estimated cost in USD for a call on a named model."""
        profile = self.profiles.get(model_name)
        if not profile:
            return None
        return profile.estimated_cost(input_tokens, output_tokens)

    def get_latency_estimate(self, model_name: str) -> Optional[int]:
        """Return p50 latency estimate in ms for a named model."""
        profile = self.profiles.get(model_name)
        if not profile:
            return None
        return profile.latency_ms_p50

    def record_outcome(
        self,
        model_name: str,
        success: bool,
        latency_ms: float,
    ) -> None:
        """Record a call outcome — updates health history."""
        self.health.record(model_name, success, latency_ms)

    # ── Private selection helpers ─────────────────────────────────

    def _filter_candidates(
        self,
        capability: Optional[str],
    ) -> list[ModelProfile]:
        """Return healthy models matching the required capability."""
        all_profiles = list(self.profiles.values())
        healthy = self.health.get_healthy_models(all_profiles)
        if not capability or capability == "any":
            return healthy
        return [p for p in healthy if p.capability == capability or p.capability == "any"]

    def _select_fastest(self, candidates: list[ModelProfile]) -> ModelProfile:
        return min(candidates, key=lambda p: p.latency_ms_p50)

    def _select_cheapest(self, candidates: list[ModelProfile]) -> ModelProfile:
        # Use p50 estimate: 1000 input + 200 output tokens as standard
        def cost_key(p):
            return p.estimated_cost(1000, 200)
        return min(candidates, key=cost_key)

    def _select_capability(
        self,
        candidates: list[ModelProfile],
        required: Optional[str],
    ) -> ModelProfile:
        # Among capability-matched models, pick cheapest
        return self._select_cheapest(candidates)

    def _select_balanced(self, candidates: list[ModelProfile]) -> ModelProfile:
        """
        Score each model: normalized_latency (0-1) + normalized_cost (0-1), lower = better.
        """
        if len(candidates) == 1:
            return candidates[0]

        # Normalize latency (lowest = 0, highest = 1)
        latencies = [p.latency_ms_p50 for p in candidates]
        min_lat, max_lat = min(latencies), max(latencies)
        lat_range = max_lat - min_lat or 1

        # Normalize cost (lowest = 0, highest = 1)
        costs = [p.estimated_cost(1000, 200) for p in candidates]
        min_cost, max_cost = min(costs), max(costs)
        cost_range = max_cost - min_cost or 1

        best = None
        best_score = float("inf")
        for p in candidates:
            norm_lat = (p.latency_ms_p50 - min_lat) / lat_range
            norm_cost = (p.estimated_cost(1000, 200) - min_cost) / cost_range
            score = norm_lat + norm_cost  # equal weight
            if score < best_score:
                best_score = score
                best = p
        return best


# ─────────────────────────────────────────────────────────────────
# Built-in model profiles (Groq free tier)
# ─────────────────────────────────────────────────────────────────
GROQ_FREE_PROFILES = [
    ModelProfile(
        name="groq_llama_8b",
        provider="groq",
        model_id="llama-3.1-8b-instant",
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        latency_ms_p50=150,
        latency_ms_p95=300,
        capability="fast",
    ),
    ModelProfile(
        name="groq_llama_70b",
        provider="groq",
        model_id="llama-3.3-70b-versatile",
        cost_per_1k_input=0.0,
        cost_per_1k_output=0.0,
        latency_ms_p50=800,
        latency_ms_p95=1500,
        capability="precise",
    ),
]


__all__ = ["ModelProfile", "HealthChecker", "CostRouter", "GROQ_FREE_PROFILES"]
