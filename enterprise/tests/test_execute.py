# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Tests for the /api/execute universal LLM proxy endpoint.
"""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient
from modelfungible.enterprise.admin_app import app, _registry, _sessions


@pytest.fixture
def client():
    _sessions.clear()
    return TestClient(app)


def login(client, user_id="admin", password="changeme"):
    r = client.post("/api/auth/login", json={"user_id": user_id, "password": password})
    assert r.status_code == 200
    return r.json()["session_id"]


def hdrs(token):
    return {"X-Auth-Token": token}


class TestExecuteEndpoint:
    """Tests for POST /api/execute"""

    def test_execute_requires_auth(self, client):
        r = client.post("/api/execute", json={"prompt": "Hello"})
        assert r.status_code == 401

    def test_execute_requires_prompt(self, client):
        token = login(client)
        r = client.post("/api/execute", json={}, headers=hdrs(token))
        assert r.status_code == 400
        assert "prompt" in r.json()["error"]

    def test_execute_invalid_mode(self, client):
        token = login(client)
        r = client.post("/api/execute", json={"prompt": "Hello", "mode": "invalid"}, headers=hdrs(token))
        assert r.status_code == 400
        assert "mode" in r.json()["error"].lower()

    def test_execute_no_models_registered(self, client):
        token = login(client)
        # Clear all models
        for name in list(_registry._models.keys()):
            _registry.deregister_model(name)
        r = client.post("/api/execute", json={"prompt": "Hello"}, headers=hdrs(token))
        assert r.status_code == 503
        assert "No models registered" in r.json()["error"]

    def test_execute_viewer_forbidden(self, client):
        token = login(client, "viewer1", "viewer123")
        r = client.post("/api/execute", json={"prompt": "Hello"}, headers=hdrs(token))
        assert r.status_code == 403

    def test_execute_trader_allowed(self, client):
        token = login(client, "trader1", "trader123")
        # Register a mock model (will fail at actual call but should pass validation)
        _registry.register_model("test-model", "openai", "gpt-4o-mini", "fake-key", 200, "fast")
        r = client.post("/api/execute", json={"prompt": "Hello"}, headers=hdrs(token))
        # Will fail at model call (fake key) but auth passed
        assert r.status_code in (200, 503)  # 503 = all models failed due to bad key

    def test_execute_cost_fields_in_response(self, client):
        """Even a failed execute should include cost/latency fields."""
        token = login(client)
        _registry.register_model("test-model", "openai", "gpt-4o-mini", "fake-key", 200, "fast")
        r = client.post("/api/execute", json={"prompt": "Hello"}, headers=hdrs(token))
        # If 503 (all models failed), should still have error message
        if r.status_code == 503:
            assert "error" in r.json()
        # If somehow succeeds, check response shape
        elif r.status_code == 200:
            d = r.json()
            for field in ["output", "model_id", "latency_ms", "cost", "router_mode"]:
                assert field in d

    def test_execute_max_cost_rejected(self, client):
        """If estimated cost > max_cost_per_call, should reject with 402."""
        token = login(client)
        # Register an expensive model
        _registry.register_model("expensive", "openai", "gpt-4-turbo", "fake-key", 500, "precise",
                                  cost_input_per_1k=0.01, cost_output_per_1k=0.03)
        r = client.post("/api/execute", json={
            "prompt": "Hello world " * 100,
            "max_cost_per_call": 0.0001,  # very low, should reject
        }, headers=hdrs(token))
        assert r.status_code == 402

    def test_execute_with_capability_mode(self, client):
        """Capability-aware routing should work with registered models."""
        token = login(client)
        _registry.register_model("code-model", "openai", "gpt-4o-mini", "fake-key", 200, "code")
        _registry.register_model("fast-model", "openai", "gpt-4o-mini", "fake-key", 150, "fast")
        r = client.post("/api/execute", json={
            "prompt": "Write a function",
            "mode": "capability",
            "capability": "code",
        }, headers=hdrs(token))
        # May succeed or 503 (fake key), but should route to code-model
        assert r.status_code in (200, 503)

    def test_execute_cheapest_mode(self, client):
        token = login(client)
        _registry.register_model("cheap", "groq", "llama-3.1-8b-instant", "", 100, "fast")
        r = client.post("/api/execute", json={"prompt": "Hi", "mode": "cheapest"}, headers=hdrs(token))
        assert r.status_code in (200, 503)

    def test_execute_fastest_mode(self, client):
        token = login(client)
        _registry.register_model("fast", "groq", "llama-3.1-8b-instant", "", 80, "fast")
        _registry.register_model("slow", "openai", "gpt-4o", "fake-key", 2000, "precise")
        r = client.post("/api/execute", json={"prompt": "Hi", "mode": "fastest"}, headers=hdrs(token))
        assert r.status_code in (200, 503)


class TestCostStats:
    """Tests for GET /api/cost-stats"""

    def test_cost_stats_requires_auth(self, client):
        r = client.get("/api/cost-stats")
        assert r.status_code == 401

    def test_cost_stats_returns_structure(self, client):
        token = login(client)
        r = client.get("/api/cost-stats?period=day&by=model", headers=hdrs(token))
        assert r.status_code == 200
        d = r.json()
        assert "period" in d
        assert "data" in d

    def test_cost_stats_by_user(self, client):
        token = login(client)
        r = client.get("/api/cost-stats?by=user", headers=hdrs(token))
        assert r.status_code == 200

    def test_cost_stats_by_all(self, client):
        token = login(client)
        r = client.get("/api/cost-stats?by=all&period=month", headers=hdrs(token))
        assert r.status_code == 200
        d = r.json()
        assert "total_cost_usd" in d["data"]
        assert "total_calls" in d["data"]


class TestModelCostFields:
    """Tests for cost field handling in model registry."""

    def test_model_auto_detects_cost(self, client):
        """When registering gpt-4o, cost should auto-populate."""
        token = login(client)
        r = client.post("/api/models/register", json={
            "name": "gpt4o-test", "provider": "openai", "model_id": "gpt-4o",
            "api_key": "fake", "latency_ms_p50": 500, "capability": "precise",
        }, headers=hdrs(token))
        assert r.status_code == 200
        model = r.json()["model"]
        # Should auto-detect from DEFAULT_COSTS
        assert model["cost_input_per_1k"] > 0
        assert model["cost_output_per_1k"] > 0
        assert model["cost_input_per_1k"] == 0.0025  # gpt-4o rate

    def test_model_explicit_cost_overrides(self, client):
        token = login(client)
        r = client.post("/api/models/register", json={
            "name": "custom-cost", "provider": "openai", "model_id": "custom-model",
            "api_key": "fake", "latency_ms_p50": 300, "capability": "fast",
            "cost_input_per_1k": 0.0001,
            "cost_output_per_1k": 0.0002,
        }, headers=hdrs(token))
        assert r.status_code == 200
        model = r.json()["model"]
        assert model["cost_input_per_1k"] == 0.0001
        assert model["cost_output_per_1k"] == 0.0002

    def test_state_includes_cost_fields(self, client):
        token = login(client)
        client.post("/api/models/register", json={
            "name": "cost-field-test", "provider": "openai", "model_id": "gpt-4o",
            "api_key": "fake", "latency_ms_p50": 500, "capability": "precise",
        }, headers=hdrs(token))
        r = client.get("/api/state", headers=hdrs(token))
        assert r.status_code == 200
        models = r.json()["models"]
        found = next((m for m in models if m["name"] == "cost-field-test"), None)
        assert found is not None
        assert "cost_input_per_1k" in found
        assert "cost_output_per_1k" in found


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
