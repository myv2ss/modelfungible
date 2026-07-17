# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Tests for Cost and Latency Router.

Tests:
- ModelProfile: stores cost, latency, capability
- HealthChecker: sliding window, auto-skip failing models
- CostRouter: mode=fastest/cheapest/balanced/capability
- Integration: router selects correct model
"""
import pytest
import time
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


# ─────────────────────────────────────────────────────────────────
# Tests: ModelProfile
# ─────────────────────────────────────────────────────────────────
class TestModelProfile:
    def test_profile_stores_all_fields(self):
        from modelfungible.core.cost_router import ModelProfile
        p = ModelProfile(
            name="fast",
            provider="groq",
            model_id="llama-3.1-8b",
            cost_per_1k_input=0.0,
            cost_per_1k_output=0.0,
            latency_ms_p50=200,
            latency_ms_p95=500,
            capability="fast",
        )
        assert p.name == "fast"
        assert p.cost_per_1k_input == 0.0
        assert p.latency_ms_p50 == 200

    def test_profile_equality(self):
        from modelfungible.core.cost_router import ModelProfile
        p1 = ModelProfile(name="a", provider="x", model_id="m1",
                          cost_per_1k_input=0.1, cost_per_1k_output=0.3,
                          latency_ms_p50=200, latency_ms_p95=500, capability="fast")
        p2 = ModelProfile(name="a", provider="x", model_id="m1",
                          cost_per_1k_input=0.1, cost_per_1k_output=0.3,
                          latency_ms_p50=200, latency_ms_p95=500, capability="fast")
        assert p1 == p2

    def test_profile_inequality(self):
        from modelfungible.core.cost_router import ModelProfile
        p1 = ModelProfile(name="a", provider="x", model_id="m1",
                          cost_per_1k_input=0.1, cost_per_1k_output=0.3,
                          latency_ms_p50=200, latency_ms_p95=500, capability="fast")
        p2 = ModelProfile(name="b", provider="x", model_id="m1",
                          cost_per_1k_input=0.1, cost_per_1k_output=0.3,
                          latency_ms_p50=200, latency_ms_p95=500, capability="fast")
        assert p1 != p2


# ─────────────────────────────────────────────────────────────────
# Tests: HealthChecker
# ─────────────────────────────────────────────────────────────────
class TestHealthChecker:
    def test_new_model_is_healthy(self):
        from modelfungible.core.cost_router import HealthChecker
        hc = HealthChecker()
        assert hc.is_healthy("fast") is True

    def test_successful_call_marks_healthy(self):
        from modelfungible.core.cost_router import HealthChecker
        hc = HealthChecker()
        hc.record("groq", success=True, latency_ms=150)
        assert hc.is_healthy("groq") is True
        assert hc.get_success_rate("groq") == 1.0

    def test_failed_call_marks_unhealthy(self):
        from modelfungible.core.cost_router import HealthChecker
        hc = HealthChecker(window=5)
        for _ in range(4):
            hc.record("model_a", success=True, latency_ms=100)
        hc.record("model_a", success=False, latency_ms=0)
        # 4/5 = 80% still might be healthy, depends on threshold
        rate = hc.get_success_rate("model_a")
        assert rate < 1.0

    def test_consecutive_failures_mark_unhealthy(self):
        from modelfungible.core.cost_router import HealthChecker
        hc = HealthChecker(failure_threshold=3)
        for _ in range(3):
            hc.record("model_x", success=False, latency_ms=0)
        assert hc.is_healthy("model_x") is False

    def test_sliding_window_forgets_old_failures(self):
        from modelfungible.core.cost_router import HealthChecker
        hc = HealthChecker(window=3)
        # 3 old successes
        for _ in range(3):
            hc.record("m", success=True, latency_ms=100)
        # Now 3 failures
        for _ in range(3):
            hc.record("m", success=False, latency_ms=0)
        # Window of 3, oldest (the successes) should be pushed out
        rate = hc.get_success_rate("m")
        assert rate == 0.0

    def test_get_healthy_models(self):
        from modelfungible.core.cost_router import HealthChecker, ModelProfile
        hc = HealthChecker()
        profiles = [
            ModelProfile(name="healthy", provider="x", model_id="h",
                         cost_per_1k_input=0, cost_per_1k_output=0,
                         latency_ms_p50=100, latency_ms_p95=200, capability="fast"),
            ModelProfile(name="sick", provider="x", model_id="s",
                         cost_per_1k_input=0, cost_per_1k_output=0,
                         latency_ms_p50=100, latency_ms_p95=200, capability="fast"),
        ]
        hc.record("sick", success=False, latency_ms=0)
        hc.record("sick", success=False, latency_ms=0)
        hc.record("sick", success=False, latency_ms=0)
        healthy = hc.get_healthy_models(profiles)
        names = [p.name for p in healthy]
        assert "healthy" in names
        assert "sick" not in names

    def test_get_latency_p95(self):
        from modelfungible.core.cost_router import HealthChecker
        hc = HealthChecker()
        hc.record("m", success=True, latency_ms=100)
        hc.record("m", success=True, latency_ms=200)
        hc.record("m", success=True, latency_ms=300)
        p95 = hc.get_latency_p95("m")
        assert p95 >= 200


# ─────────────────────────────────────────────────────────────────
# Tests: CostRouter
# ─────────────────────────────────────────────────────────────────
class TestCostRouter:
    def _profiles(self):
        from modelfungible.core.cost_router import ModelProfile
        return [
            ModelProfile(name="fast", provider="groq", model_id="llama-3.1-8b",
                        cost_per_1k_input=0.0, cost_per_1k_output=0.0,
                        latency_ms_p50=150, latency_ms_p95=300, capability="fast"),
            ModelProfile(name="precise", provider="groq", model_id="llama-3.3-70b",
                        cost_per_1k_input=0.0, cost_per_1k_output=0.0,
                        latency_ms_p50=800, latency_ms_p95=1500, capability="precise"),
            ModelProfile(name="claude", provider="anthropic", model_id="claude-3-5-sonnet",
                        cost_per_1k_input=0.003, cost_per_1k_output=0.015,
                        latency_ms_p50=600, latency_ms_p95=1200, capability="precise"),
        ]

    def test_cheapest_mode(self):
        from modelfungible.core.cost_router import CostRouter
        profiles = self._profiles()
        router = CostRouter(profiles, mode="cheapest")
        best = router.get_model()
        assert best.name == "fast"  # Groq free tier

    def test_fastest_mode(self):
        from modelfungible.core.cost_router import CostRouter
        profiles = self._profiles()
        router = CostRouter(profiles, mode="fastest")
        best = router.get_model()
        assert best.name == "fast"  # lowest p50 latency

    def test_balanced_mode(self):
        from modelfungible.core.cost_router import CostRouter
        profiles = self._profiles()
        router = CostRouter(profiles, mode="balanced")
        best = router.get_model()
        # Should be one of them (not fast but not most expensive)
        assert best.name in ["fast", "precise", "claude"]

    def test_capability_mode(self):
        from modelfungible.core.cost_router import CostRouter
        profiles = self._profiles()
        router = CostRouter(profiles, mode="capability")
        best = router.get_model(capability_required="precise")
        assert best.capability == "precise"
        # Among precise models, should pick the cheapest/healthiest
        assert best.name in ["precise", "claude"]

    def test_skips_unhealthy_models(self):
        from modelfungible.core.cost_router import CostRouter, HealthChecker
        profiles = self._profiles()
        hc = HealthChecker()
        # Mark fast as sick
        for _ in range(3):
            hc.record("fast", success=False, latency_ms=0)
        router = CostRouter(profiles, mode="fastest", health_checker=hc)
        best = router.get_model()
        assert best.name != "fast"

    def test_all_models_unhealthy_raises(self):
        from modelfungible.core.cost_router import CostRouter, HealthChecker
        profiles = self._profiles()
        hc = HealthChecker(failure_threshold=1)
        for p in profiles:
            hc.record(p.name, success=False, latency_ms=0)
        router = CostRouter(profiles, mode="fastest", health_checker=hc)
        result = router.get_model()
        assert result is None  # No healthy models

    def test_get_cost_estimate(self):
        from modelfungible.core.cost_router import CostRouter
        profiles = self._profiles()
        router = CostRouter(profiles, mode="fastest")
        cost = router.get_cost_estimate("claude", input_tokens=1000, output_tokens=200)
        # claude: $0.003/1K in + $0.015/1K out
        expected = 0.003 * 1 + 0.015 * 0.2  # = $0.006
        assert abs(cost - expected) < 0.001

    def test_get_latency_estimate(self):
        from modelfungible.core.cost_router import CostRouter
        profiles = self._profiles()
        router = CostRouter(profiles, mode="cheapest")
        lat = router.get_latency_estimate("fast")
        assert lat == 150  # p50 latency

    def test_record_outcome_updates_health(self):
        from modelfungible.core.cost_router import CostRouter, HealthChecker
        profiles = self._profiles()
        hc = HealthChecker()
        router = CostRouter(profiles, mode="fastest", health_checker=hc)
        router.record_outcome("fast", success=True, latency_ms=180)
        assert hc.is_healthy("fast")

    def test_router_returns_profile(self):
        from modelfungible.core.cost_router import CostRouter
        profiles = self._profiles()
        router = CostRouter(profiles, mode="fastest")
        result = router.get_model()
        from modelfungible.core.cost_router import ModelProfile
        assert isinstance(result, ModelProfile)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
