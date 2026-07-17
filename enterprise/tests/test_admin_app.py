# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Tests for admin_app.py — FastAPI Admin Web UI.
"""
import pytest, tempfile, os, sys
from pathlib import Path

# Only run if FastAPI is available
pytest.importorskip("fastapi")
pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient
from modelfungible.enterprise.admin_app import app, InMemoryRegistry


class TestInMemoryRegistry:
    def test_register_model(self):
        r = InMemoryRegistry()
        r.register_model("claude", "anthropic", "claude-3.5-sonnet", "key123", 500, "precise")
        models = r.list_models()
        assert len(models) == 1
        assert models[0]["name"] == "claude"
        assert models[0]["model_id"] == "claude-3.5-sonnet"

    def test_deregister_model(self):
        r = InMemoryRegistry()
        r.register_model("claude", "anthropic", "claude-3.5-sonnet", "key123", 500, "precise")
        r.deregister_model("claude")
        assert len(r.list_models()) == 0

    def test_circuit_breaker_reset(self):
        from modelfungible.core.circuit_breaker import CircuitBreaker
        r = InMemoryRegistry()
        r._breakers["test"] = CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
        r._breakers["test"].record(success=False)  # trips to OPEN
        assert r._breakers["test"].state() == "OPEN"
        r.reset_breaker("test")
        assert r._breakers["test"].state() == "CLOSED"


class TestAdminAPI:
    @pytest.fixture
    def client(self):
        return TestClient(app)

    def test_state_empty(self, client):
        r = client.get("/api/state")
        assert r.status_code == 200
        data = r.json()
        assert "total_entries" in data
        assert "models" in data

    def test_health_empty(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {}

    def test_circuit_breakers_empty(self, client):
        r = client.get("/api/circuit-breakers")
        assert r.status_code == 200
        assert r.json() == []

    def test_register_model(self, client):
        resp = client.post("/api/models/register", json={
            "name": "claude-test",
            "provider": "anthropic",
            "model_id": "claude-3.5-sonnet",
            "api_key": "test-key",
            "latency_ms_p50": 500,
            "capability": "precise",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "claude-test"

    def test_register_duplicate(self, client):
        client.post("/api/models/register", json={
            "name": "dup", "provider": "openai", "model_id": "gpt-4o",
            "api_key": "k", "latency_ms_p50": 500, "capability": "fast",
        })
        resp2 = client.post("/api/models/register", json={
            "name": "dup", "provider": "openai", "model_id": "gpt-4o",
            "api_key": "k", "latency_ms_p50": 500, "capability": "fast",
        })
        assert resp2.status_code == 400

    def test_delete_model(self, client):
        client.post("/api/models/register", json={
            "name": "to-delete", "provider": "groq",
            "model_id": "llama-3.1-8b", "api_key": "k",
            "latency_ms_p50": 200, "capability": "fast",
        })
        resp = client.delete("/api/models/to-delete")
        assert resp.status_code == 200

    def test_delete_nonexistent(self, client):
        resp = client.delete("/api/models/does-not-exist")
        assert resp.status_code == 404

    def test_circuit_breaker_reset(self, client):
        client.post("/api/models/register", json={
            "name": "cb-test", "provider": "openai", "model_id": "gpt-4o",
            "api_key": "k", "latency_ms_p50": 500, "capability": "fast",
        })
        from modelfungible.core.circuit_breaker import CircuitBreaker
        app.state.registry._breakers["cb-test"] = CircuitBreaker(failure_threshold=1, cooldown_seconds=0)
        app.state.registry._breakers["cb-test"].record(success=False)
        assert app.state.registry._breakers["cb-test"].state() == "OPEN"
        resp = client.post("/api/circuit-breakers/cb-test/reset")
        assert resp.status_code == 200
        assert app.state.registry._breakers["cb-test"].state() == "CLOSED"

    def test_strategies_list(self, client):
        r = client.get("/api/strategies")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_audit_verify_empty(self, client):
        r = client.get("/api/audit/verify")
        assert r.status_code == 200
        data = r.json()
        assert "valid" in data

    def test_audit_logs_query(self, client):
        r = client.get("/api/audit/logs?limit=10")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_audit_logs_with_filters(self, client):
        r = client.get("/api/audit/logs?actor=gpt-4o&action=model_execute&outcome=success&limit=50")
        assert r.status_code == 200

    def test_compliance_retention(self, client):
        r = client.get("/api/compliance/retention")
        assert r.status_code == 200
        data = r.json()
        assert "gdpr" in data or "hipaa" in data

    def test_compliance_pii_scan(self, client):
        r = client.get("/api/compliance/pii/scan?q=")
        assert r.status_code == 200

    def test_version_endpoint(self, client):
        r = client.get("/api/version")
        assert r.status_code == 200
        data = r.json()
        assert "python" in data

    def test_admin_page(self, client):
        r = client.get("/admin")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_audit_export_json(self, client):
        r = client.get("/api/audit/export/json")
        # May redirect or return content
        assert r.status_code in (200, 404)

    def test_audit_export_csv(self, client):
        r = client.get("/api/audit/export/csv")
        assert r.status_code in (200, 404)

    def test_admin_ui_has_tabs(self, client):
        r = client.get("/admin")
        assert r.status_code == 200
        html = r.text
        assert 'id="tab-dashboard"' in html
        assert 'id="tab-deployments"' in html
        assert 'id="tab-strategies"' in html
        assert 'id="tab-audit"' in html
        assert 'id="tab-compliance"' in html
        assert "ModelFungible" in html

    def test_admin_ui_has_js(self, client):
        r = client.get("/admin")
        html = r.text
        assert "function showTab" in html
        assert "loadDashboard" in html
        assert "loadStrats" in html
        assert "loadAudit" in html


class TestAdminCLI:
    """Test the CLI entry point."""
    def test_cli_import(self):
        # Just verify the module can be imported (syntax check)
        from modelfungible.enterprise.admin_app import app, InMemoryRegistry
        assert app is not None
        assert InMemoryRegistry is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
