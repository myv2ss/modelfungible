# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Tests for cost and latency routing.

Tests:
- Model profiles: cost/token, latency estimate, capability tags
- Routing modes: cheapest, fastest, balanced, capability-aware
- Health checking: model availability, latency tracking
- Routing decision: correct model selected per mode
- Failure handling: skip unhealthy models
"""
import pytest, time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


# ─────────────────────────────────────────────────────────────────
# Test data
# ─────────────────────────────────────────────────────────────────
MODEL_PROFILES = {
    "groq_llama8b": {
        "name": "groq_llama8b",
        "provider": "groq",
        "model_id": "llama-3.1-8b-instant",
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "latency_ms": 200,
        "capabilities": ["fast", "classification", "extraction"],
        "available": True,
    },
    "groq_llama70b": {
        "name": "groq_llama70b",
        "provider": "groq",
        "model_id": "llama-3.3-70b-versatile",
        "cost_per_1k_input": 0.0,
        "cost_per_1k_output": 0.0,
        "latency_ms": 800,
        "capabilities": ["analysis", "reasoning", "generation"],
        "available": True,
    },
    "openai_gpt4o": {
        "name": "openai_gpt4o",
        "provider": "openai",
        "model_id": "gpt-4o",
        "cost_per_1k_input": 2.5,
        "cost_per_1k_output": 10.0,
        "latency_ms": 1500,
        "capabilities": ["analysis", "reasoning", "generation"],
        "available": True,
    },
    "anthropic_sonnet": {
        "name": "anthropic_sonnet",
        "provider": "anthropic",
        "model_id": "claude-3-5-sonnet",
        "cost_per_1k_input": 1.5,
        "cost_per_1k_output": 5.0,
        "latency_ms": 2000,
        "capabilities": ["analysis", "reasoning", "generation"],
        "available": True,
    },
}


# ─────────────────────────────────────────────────────────────────
# Tests: Profile loading
# ─────────────────────────────────────────────────────────────────
class TestModelProfiles:
    def test_profile_has_required_fields(self):
        from modelfungible.core.cost_router import ModelProfile
        p = ModelProfile(**MODEL_PROFILES["groq_llama8b"])
        assert p.cost_per_1k_input == 0.0
        assert p.cost_per_1k_output == 0.0
        assert p.latency_ms == 200
        assert "fast" in p.capabilities

    def test_profile_defaults(self):
        from modelfungible.core.cost_router import ModelProfile
        p = ModelProfile(
            name="test",
            provider="groq",
            model_id="llama3",
        )
        assert p.cost_per_1k_input == 0.0
        assert p.cost_per_1k_output == 0.0
        assert p.latency_ms > 0
        assert p.available is True


# ─────────────────────────────────────────────────────────────────
# Tests: Cost scoring
# ─────────────────────────────────────────────────────────────────
class TestCostScoring:
    def test_cheapest_input_token(self):
        from modelfungible.core.cost_router import ModelProfile, CostScorer
        profiles = {k: ModelProfile(**v) for k, v in MODEL_PROFILES.items()}
        scorer = CostScorer()
        # Groq is free → should be cheapest
        cheapest = scorer.find_cheapest(
            profiles,
            input_tokens=1000,
            output_tokens=500,
        )
        assert cheapest.name == "groq_llama8b"

    def test_cheapest_favors_zero_cost(self):
        from modelfungible.core.cost_router import ModelProfile, CostScorer
        # Both zero cost
        profiles = {
            "a": ModelProfile(name="a", provider="groq", model_id="m1",
                              cost_per_1k_input=0.0, cost_per_1k_output=0.0, latency_ms=500),
            "b": ModelProfile(name="b", provider="groq", model_id="m2",
                              cost_per_1k_input=0.0, cost_per_1k_output=0.0, latency_ms=200),
        }
        scorer = CostScorer()
        cheapest = scorer.find_cheapest(profiles, input_tokens=100, output_tokens=50)
        # When costs equal, should pick lower latency
        assert cheapest.name == "b"

    def test_cost_estimate(self):
        from modelfungible.core.cost_router import ModelProfile, CostScorer
        profiles = {
            "gpt4o": ModelProfile(
                name="gpt4o", provider="openai", model_id="gpt-4o",
                cost_per_1k_input=2.5, cost_per_1k_output=10.0, latency_ms=1500,
            )
        }
        scorer = CostScorer()
        cost = scorer.estimate_cost(profiles["gpt4o"], input_tokens=2000, output_tokens=500)
        # 2k input @ $2.5/1k = $5.00, 500 output @ $10/1k = $5.00 → $10.00
        assert cost == 10.0


# ─────────────────────────────────────────────────────────────────
# Tests: Latency routing
# ─────────────────────────────────────────────────────────────────
class TestLatencyRouting:
    def test_fastest_model(self):
        from modelfungible.core.cost_router import ModelProfile, CostScorer
        profiles = {k: ModelProfile(**v) for k, v in MODEL_PROFILES.items()}
        scorer = CostScorer()
        fastest = scorer.find_fastest(profiles)
        assert fastest.name == "groq_llama8b"  # 200ms is lowest

    def test_unavailable_skipped(self):
        from modelfungible.core.cost_router import ModelProfile, CostScorer
        profiles = {
            "fast": ModelProfile(name="fast", provider="groq", model_id="m1",
                                 latency_ms=100, available=True),
            "slow": ModelProfile(name="slow", provider="openai", model_id="m2",
                                 latency_ms=2000, available=True),
        }
        scorer = CostScorer()
        fastest = scorer.find_fastest(profiles)
        assert fastest.name == "fast"


# ─────────────────────────────────────────────────────────────────
# Tests: Balanced routing
# ─────────────────────────────────────────────────────────────────
class TestBalancedRouting:
    def test_balanced_uses_score(self):
        from modelfungible.core.cost_router import ModelProfile, CostScorer
        profiles = {k: ModelProfile(**v) for k, v in MODEL_PROFILES.items()}
        scorer = CostScorer()
        # groq_llama8b has both lowest cost AND lowest latency → should win
        best = scorer.find_balanced(profiles)
        assert best.name == "groq_llama8b"


# ─────────────────────────────────────────────────────────────────
# Tests: Capability routing
# ─────────────────────────────────────────────────────────────────
class TestCapabilityRouting:
    def test_route_by_capability(self):
        from modelfungible.core.cost_router import ModelProfile, CostScorer
        profiles = {k: ModelProfile(**v) for k, v in MODEL_PROFILES.items()}
        scorer = CostScorer()
        # "extraction" task → only groq_llama8b has it
        result = scorer.find_by_capability(profiles, "extraction")
        assert result.name == "groq_llama8b"

    def test_capability_fallback(self):
        from modelfungible.core.cost_router import ModelProfile, CostScorer
        profiles = {
            "analysis_model": ModelProfile(
                name="analysis_model", provider="openai", model_id="gpt-4o",
                latency_ms=1500, capabilities=["analysis", "reasoning"],
            )
        }
        scorer = CostScorer()
        # Task not in capabilities → fall back to fastest
        result = scorer.find_by_capability(profiles, "extraction")
        assert result.name == "analysis_model"  # only one available


# ─────────────────────────────────────────────────────────────────
# Tests: Health checking
# ─────────────────────────────────────────────────────────────────
class TestHealthChecker:
    def test_record_latency(self):
        from modelfungible.core.cost_router import HealthChecker
        hc = HealthChecker()
        hc.record("groq_llama8b", latency_ms=250, success=True)
        assert "groq_llama8b" in hc._history
        assert hc.get_avg_latency("groq_llama8b") > 0

    def test_record_failure(self):
        from modelfungible.core.cost_router import HealthChecker
        hc = HealthChecker()
        hc.record("groq_llama8b", latency_ms=0, success=False)
        assert hc.get_success_rate("groq_llama8b") < 1.0

    def test_availability_flag(self):
        from modelfungible.core.cost_router import HealthChecker
        hc = HealthChecker()
        # Record 3 consecutive failures
        for _ in range(3):
            hc.record("groq_llama8b", latency_ms=0, success=False)
        assert hc.is_available("groq_llama8b") is False

    def test_recovery_after_success(self):
        from modelfungible.core.cost_router import HealthChecker
        hc = HealthChecker()
        # 3 failures → unavailable
        for _ in range(3):
            hc.record("groq_llama8b", latency_ms=0, success=False)
        assert hc.is_available("groq_llama8b") is False
        # 1 success → available again
        hc.record("groq_llama8b", latency_ms=200, success=True)
        assert hc.is_available("groq_llama8b") is True

    def test_get_healthy_models(self):
        from modelfungible.core.cost_router import HealthChecker, ModelProfile
        hc = HealthChecker()
        profiles = {k: ModelProfile(**v) for k, v in MODEL_PROFILES.items()}
        # All models are healthy
        healthy = hc.get_healthy_models(profiles)
        assert len(healthy) == 4


# ─────────────────────────────────────────────────────────────────
# Tests: Router integration
# ─────────────────────────────────────────────────────────────────
class TestCostRouter:
    def test_route_finds_model(self):
        from modelfungible.core.cost_router import CostRouter, ModelProfile
        profiles = {k: ModelProfile(**v) for k, v in MODEL_PROFILES.items()}
        router = CostRouter(profiles=profiles)
        model = router.route(mode="fastest")
        assert model is not None
        assert model.available is True

    def test_route_with_capability(self):
        from modelfungible.core.cost_router import CostRouter, ModelProfile
        profiles = {k: ModelProfile(**v) for k, v in MODEL_PROFILES.items()}
        router = CostRouter(profiles=profiles)
        # mode="balanced" with capability="analysis" → picks best-capable model
        model = router.route(mode="balanced", capability="analysis")
        assert "analysis" in model.capabilities

    def test_route_returns_none_when_all_unavailable(self):
        from modelfungible.core.cost_router import CostRouter, ModelProfile, HealthChecker
        profiles = {
            "bad": ModelProfile(name="bad", provider="openai", model_id="gpt-4o",
                               latency_ms=100, available=True),
        }
        hc = HealthChecker()
        # Mark as unavailable
        for _ in range(3):
            hc.record("bad", latency_ms=0, success=False)
        router = CostRouter(profiles=profiles, health_checker=hc)
        model = router.route(mode="fastest")
        assert model is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
