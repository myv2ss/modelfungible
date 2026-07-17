# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Cost and Latency Router — ModelFungible Core

Automatically selects the best model based on:
- Cost efficiency (cheapest tokens)
- Latency (fastest response)
- Capability match (model is suited for the task)
- Health (skip failing models)

Usage:
    router = CostRouter(profiles={
        "fast": ModelProfile(name="fast", provider="groq", model_id="llama-3.1-8b", latency_ms=200),
        "precise": ModelProfile(name="precise", provider="anthropic", model_id="claude-3-5-sonnet", latency_ms=2000),
    })

    # Route by speed
    model = router.route(mode="fastest")

    # Route by cost
    model = router.route(mode="cheapest", input_tokens=1000, output_tokens=500)

    # Route by capability
    model = router.route(mode="fastest", capability="analysis")

    # Record real latency after a call
    router.health.record("fast", latency_ms=180, success=True)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import time


# ─────────────────────────────────────────────────────────────────
# ModelProfile
# ─────────────────────────────────────────────────────────────────
@dataclass
class ModelProfile:
    """
    Describes a model's cost, latency, and capability profile.

    Args:
        name:             Unique identifier for this model config
        provider:         "openai" | "anthropic" | "groq" | "vertexai" | "ollama"
        model_id:         Provider's model identifier
        cost_per_1k_input:  Cost per 1,000 input tokens (USD)
        cost_per_1k_output: Cost per 1,000 output tokens (USD)
        latency_ms:        Estimated round-trip latency (ms)
        capabilities:      Tags describing what this model does well
                          e.g. ["fast", "classification", "reasoning", "extraction"]
        available:        Whether the model is currently reachable
    """

    name: str
    provider: str
    model_id: str
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    latency_ms: int = 1000
    capabilities: list[str] = field(default_factory=list)
    available: bool = True

    def estimated_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate total cost for a call with given token counts."""
        return (input_tokens / 1000.0) * self.cost_per_1k_input + \
               (output_tokens / 1000.0) * self.cost_per_1k_output


# ─────────────────────────────────────────────────────────────────
# HealthChecker
# ─────────────────────────────────────────────────────────────────
class HealthChecker:
    """
    Tracks model health: latency history and success/failure rate.

    Uses a sliding window of recent calls to determine if a model
    should be skipped (too many failures) or is recovering.
    """

    def __init__(self, failure_threshold: int = 3, window_size: int = 10):
        """
        Args:
            failure_threshold: Consecutive failures before marking unavailable
            window_size: Number of recent calls to track per model
        """
        self.failure_threshold = failure_threshold
        self.window_size = window_size
        self._history: dict[str, list[tuple[float, bool]]] = {}  # name → [(latency_ms, success)]

    def record(self, model_name: str, latency_ms: float, success: bool) -> None:
        """Record a single call result."""
        if model_name not in self._history:
            self._history[model_name] = []
        self._history[model_name].append((latency_ms, success))
        # Keep only window_size recent entries
        if len(self._history[model_name]) > self.window_size:
            self._history[model_name] = self._history[model_name][-self.window_size:]

    def is_available(self, model_name: str) -> bool:
        """Return True if model has no recent consecutive failures."""
        if model_name not in self._history:
            return True  # No history → assume available
        history = self._history[model_name]
        if len(history) < self.failure_threshold:
            return True
        # Check last N calls
        recent = history[-self.failure_threshold:]
        if all(not success for _, success in recent):
            return False
        return True

    def get_avg_latency(self, model_name: str) -> float:
        """Return average latency for successful calls."""
        if model_name not in self._history:
            return 0.0
        successes = [ms for ms, ok in self._history[model_name] if ok and ms > 0]
        return sum(successes) / len(successes) if successes else 0.0

    def get_success_rate(self, model_name: str) -> float:
        """Return fraction of successful calls in the window."""
        if model_name not in self._history:
            return 1.0
        history = self._history[model_name]
        if not history:
            return 1.0
        return sum(1 for _, ok in history if ok) / len(history)

    def get_healthy_models(self, profiles: dict[str, ModelProfile]) -> dict[str, ModelProfile]:
        """Filter a profiles dict to only include available models."""
        return {n: p for n, p in profiles.items() if self.is_available(n)}


# ─────────────────────────────────────────────────────────────────
# CostScorer
# ─────────────────────────────────────────────────────────────────
class CostScorer:
    """
    Scores and ranks models by various criteria.
    """

    def find_cheapest(
        self,
        profiles: dict[str, ModelProfile],
        input_tokens: int = 1000,
        output_tokens: int = 500,
    ) -> Optional[ModelProfile]:
        """Find the lowest-cost model (by estimated total cost)."""
        if not profiles:
            return None
        scored = [(p.estimated_cost(input_tokens, output_tokens), p) for p in profiles.values()]
        scored.sort(key=lambda x: (x[0], x[1].latency_ms))  # cost primary, latency tiebreak
        return scored[0][1]

    def find_fastest(self, profiles: dict[str, ModelProfile]) -> Optional[ModelProfile]:
        """Find the model with lowest estimated latency."""
        if not profiles:
            return None
        available = [p for p in profiles.values() if p.available]
        if not available:
            return None
        return min(available, key=lambda p: p.latency_ms)

    def find_balanced(self, profiles: dict[str, ModelProfile]) -> Optional[ModelProfile]:
        """
        Find the best balance of cost and latency.
        Normalizes both to 0-1 scores and picks the model with the best combined score.
        """
        if not profiles:
            return None
        available = [p for p in profiles.values() if p.available]
        if not available:
            return None

        # Normalize cost (lower = better)
        costs = [p.cost_per_1k_input + p.cost_per_1k_output for p in available]
        latencies = [p.latency_ms for p in available]
        max_cost = max(costs) if max(costs) > 0 else 1
        max_lat = max(latencies) if max(latencies) > 0 else 1

        best = None
        best_score = float("inf")
        for p in available:
            cost_score = (p.cost_per_1k_input + p.cost_per_1k_output) / max_cost
            lat_score = p.latency_ms / max_lat
            # Equal weight: cost and latency both matter
            combined = cost_score + lat_score
            if combined < best_score:
                best_score = combined
                best = p
        return best

    def find_by_capability(
        self,
        profiles: dict[str, ModelProfile],
        capability: str,
    ) -> Optional[ModelProfile]:
        """
        Find the best model that supports a given capability.
        Falls back to the fastest available model if no capability match.
        """
        if not profiles:
            return None
        available = [p for p in profiles.values() if p.available]
        if not available:
            return None

        capable = [p for p in available if capability in p.capabilities]
        if not capable:
            # Fall back to fastest
            return min(available, key=lambda p: p.latency_ms)
        # Pick cheapest among capable
        return min(capable, key=lambda p: p.cost_per_1k_input + p.cost_per_1k_output)

    def estimate_cost(
        self,
        profile: ModelProfile,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Estimate cost for a single call."""
        return profile.estimated_cost(input_tokens, output_tokens)


# ─────────────────────────────────────────────────────────────────
# CostRouter
# ─────────────────────────────────────────────────────────────────
class CostRouter:
    """
    Main routing interface. Combines profiles + health + scoring.

    Usage:
        router = CostRouter(profiles={
            "fast": ModelProfile(name="fast", provider="groq", model_id="llama-3.1-8b", latency_ms=200),
            "precise": ModelProfile(name="precise", provider="anthropic", model_id="claude-3-5-sonnet", latency_ms=2000),
        })

        # Route
        model = router.route(mode="fastest")
        model = router.route(mode="cheapest", input_tokens=1000, output_tokens=500)
        model = router.route(mode="balanced", capability="analysis")

        # After call completes
        router.record(model.name, latency_ms=180, success=True)
    """

    def __init__(
        self,
        profiles: Optional[dict[str, ModelProfile]] = None,
        health_checker: Optional[HealthChecker] = None,
        scorer: Optional[CostScorer] = None,
    ):
        self.profiles = profiles or {}
        self.health = health_checker or HealthChecker()
        self.scorer = scorer or CostScorer()

    def add_model(self, name: str, profile: ModelProfile) -> None:
        """Register a model profile."""
        self.profiles[name] = profile

    def route(
        self,
        mode: str = "balanced",
        input_tokens: int = 1000,
        output_tokens: int = 500,
        capability: Optional[str] = None,
    ) -> Optional[ModelProfile]:
        """
        Route to the best model for the given criteria.

        Args:
            mode:          "fastest" | "cheapest" | "balanced" | "capability"
            input_tokens:  Estimated input token count (for cost mode)
            output_tokens: Estimated output token count (for cost mode)
            capability:    Required capability (for capability mode)

        Returns:
            Selected ModelProfile, or None if no healthy models available.
        """
        # Filter to healthy models
        candidates = self.health.get_healthy_models(self.profiles)
        if not candidates:
            return None

        # If capability specified, filter to capable models first (then apply mode)
        if capability:
            capable = [p for p in candidates.values()
                       if capability in p.capabilities]
            if capable:
                candidates = {p.name: p for p in capable}

        if mode == "fastest":
            return self.scorer.find_fastest(candidates)

        elif mode == "cheapest":
            return self.scorer.find_cheapest(candidates, input_tokens, output_tokens)

        elif mode == "balanced":
            return self.scorer.find_balanced(candidates)

        elif mode == "capability":
            if not capability or not candidates:
                return self.scorer.find_fastest(candidates)
            return self.scorer.find_by_capability(candidates, capability)

        else:
            # Default: balanced
            return self.scorer.find_balanced(candidates)

    def record(self, model_name: str, latency_ms: float, success: bool) -> None:
        """Record a call result for health tracking."""
        self.health.record(model_name, latency_ms, success)

    def get_model_info(self, model_name: str) -> Optional[ModelProfile]:
        """Return profile for a specific model."""
        return self.profiles.get(model_name)

    def set_unavailable(self, model_name: str) -> None:
        """Manually mark a model as unavailable."""
        if model_name in self.profiles:
            self.profiles[model_name].available = False

    def set_available(self, model_name: str) -> None:
        """Manually mark a model as available (after recovery)."""
        if model_name in self.profiles:
            self.profiles[model_name].available = True


__all__ = ["CostRouter", "CostScorer", "HealthChecker", "ModelProfile"]
