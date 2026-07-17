# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Tests for admin_app.py — FastAPI Admin Web UI (multi-user auth).
"""
import pytest, os, sys
from pathlib import Path

pytest.importorskip("fastapi")
pytest.importorskip("fastapi.testclient")

from fastapi.testclient import TestClient
from modelfungible.enterprise import admin_app as app_module
from modelfungible.enterprise.admin_app import app, _user_store, _sessions, create_session, User


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def login(client: TestClient, user_id="admin", password=None) -> str:
    """Login and return the session token."""
    pwd = password or {"admin": "changeme", "trader1": "trader123", "viewer1": "viewer123"}.get(user_id, "x")
    r = client.post("/api/auth/login", json={"user_id": user_id, "password": pwd})
    assert r.status_code == 200, f"Login failed: {r.text()} — user:{user_id} pwd:{pwd}"
    return r.json()["session_id"]


def headers(token: str):
    return {"X-Auth-Token": token}


# ─── AUTH ENDPOINTS ────────────────────────────────────────────────────────────

class TestLogin:
    def test_login_valid(self, client):
        r = client.post("/api/auth/login", json={"user_id": "admin", "password": "changeme"})
        assert r.status_code == 200
        d = r.json()
        assert "session_id" in d
        assert d["user_id"] == "admin"
        assert d["role"] == "admin"

    def test_login_invalid_password(self, client):
        r = client.post("/api/auth/login", json={"user_id": "admin", "password": "wrong"})
        assert r.status_code == 401

    def test_login_invalid_user(self, client):
        r = client.post("/api/auth/login", json={"user_id": "nobody", "password": "x"})
        assert r.status_code == 401

    def test_login_missing_fields(self, client):
        r = client.post("/api/auth/login", json={"user_id": "admin"})
        assert r.status_code == 422  # validation error

    def test_logout(self, client):
        token = login(client)
        r = client.post("/api/auth/logout", headers=headers(token))
        assert r.status_code == 200

    def test_get_me(self, client):
        token = login(client)
        r = client.get("/api/auth/me", headers=headers(token))
        assert r.status_code == 200
        assert r.json()["user_id"] == "admin"

    def test_get_me_no_token(self, client):
        r = client.get("/api/auth/me")
        assert r.status_code == 401

    def test_get_me_bad_token(self, client):
        r = client.get("/api/auth/me", headers=headers("bad-token"))
        assert r.status_code == 401


class TestUserManagement:
    """Admin-only user management."""

    def test_list_users_admin(self, client):
        token = login(client, "admin")
        r = client.get("/api/auth/users", headers=headers(token))
        assert r.status_code == 200
        users = r.json()
        assert any(u["user_id"] == "admin" for u in users)
        assert any(u["user_id"] == "trader1" for u in users)

    def test_list_users_trader_forbidden(self, client):
        token = login(client, "trader1")
        r = client.get("/api/auth/users", headers=headers(token))
        assert r.status_code == 403

    def test_list_users_viewer_forbidden(self, client):
        token = login(client, "viewer1")
        r = client.get("/api/auth/users", headers=headers(token))
        assert r.status_code == 403

    def test_create_user_admin(self, client):
        token = login(client, "admin")
        r = client.post("/api/auth/users", json={"user_id": "newuser", "name": "New User", "role": "trader", "password": "secret123"}, headers=headers(token))
        assert r.status_code == 200
        # Verify can login with new user
        r2 = client.post("/api/auth/login", json={"user_id": "newuser", "password": "secret123"})
        assert r2.status_code == 200

    def test_create_user_trader_forbidden(self, client):
        token = login(client, "trader1")
        r = client.post("/api/auth/users", json={"user_id": "x", "password": "x"}, headers=headers(token))
        assert r.status_code == 403

    def test_delete_user_admin(self, client):
        token = login(client, "admin")
        # Create then delete
        client.post("/api/auth/users", json={"user_id": "tempuser", "password": "x"}, headers=headers(token))
        r = client.delete("/api/auth/users/tempuser", headers=headers(token))
        assert r.status_code == 200
        # Verify can't login
        r2 = client.post("/api/auth/login", json={"user_id": "tempuser", "password": "x"})
        assert r2.status_code == 401

    def test_delete_self_forbidden(self, client):
        token = login(client, "admin")
        r = client.delete("/api/auth/users/admin", headers=headers(token))
        assert r.status_code == 400

    def test_sessions_admin(self, client):
        token = login(client, "admin")
        r = client.get("/api/auth/sessions", headers=headers(token))
        assert r.status_code == 200
        assert isinstance(r.json(), list)


# ─── PROTECTED API ENDPOINTS ─────────────────────────────────────────────────

class TestAPIRequiresAuth:
    """All /api/* endpoints require valid auth."""

    def test_state_requires_auth(self, client):
        r = client.get("/api/state")
        assert r.status_code == 401

    def test_health_requires_auth(self, client):
        r = client.get("/api/health")
        assert r.status_code == 401

    def test_audit_requires_auth(self, client):
        r = client.get("/api/audit/logs")
        assert r.status_code == 401

    def test_strategies_requires_auth(self, client):
        r = client.get("/api/strategies")
        assert r.status_code == 401


class TestStateEndpoint:
    def test_state_with_auth(self, client):
        token = login(client)
        r = client.get("/api/state", headers=headers(token))
        assert r.status_code == 200
        d = r.json()
        assert "user" in d          # user info from auth context
        assert "models" in d
        assert d["user"]["user_id"] == "admin"

    def test_state_trader_role(self, client):
        token = login(client, "trader1")
        r = client.get("/api/state", headers=headers(token))
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "trader"


class TestModelManagement:
    def test_register_model_admin(self, client):
        token = login(client, "admin")
        r = client.post("/api/models/register", json={
            "name": "claude-test", "provider": "anthropic",
            "model_id": "claude-3.5-sonnet", "api_key": "key123",
            "latency_ms_p50": 500, "capability": "precise",
        }, headers=headers(token))
        assert r.status_code == 200

    def test_register_model_trader_forbidden(self, client):
        token = login(client, "trader1")
        r = client.post("/api/models/register", json={
            "name": "x", "provider": "y", "model_id": "z", "api_key": "k",
            "latency_ms_p50": 500, "capability": "fast",
        }, headers=headers(token))
        assert r.status_code == 403

    def test_delete_model_admin(self, client):
        token = login(client, "admin")
        client.post("/api/models/register", json={
            "name": "to-del", "provider": "x", "model_id": "y",
            "api_key": "k", "latency_ms_p50": 100, "capability": "fast",
        }, headers=headers(token))
        r = client.delete("/api/models/to-del", headers=headers(token))
        assert r.status_code == 200


class TestAuditLogs:
    def test_audit_logs_with_auth(self, client):
        token = login(client)
        r = client.get("/api/audit/logs?limit=10", headers=headers(token))
        assert r.status_code == 200
        assert "entries" in r.json()

    def test_audit_verify(self, client):
        token = login(client)
        r = client.get("/api/audit/verify", headers=headers(token))
        assert r.status_code == 200
        assert "verified" in r.json()


class TestCompliance:
    def test_retention_requires_admin_or_trader(self, client):
        token = login(client, "viewer1")  # viewer should still be able to read
        r = client.get("/api/compliance/retention", headers=headers(token))
        assert r.status_code == 200

    def test_license_requires_admin(self, client):
        token = login(client, "trader1")
        r = client.get("/api/compliance/license", headers=headers(token))
        assert r.status_code == 403


class TestCircuitBreakers:
    def test_circuit_breakers_requires_auth(self, client):
        r = client.get("/api/circuit-breakers")
        assert r.status_code == 401

    def test_circuit_breakers_with_auth(self, client):
        token = login(client)
        r = client.get("/api/circuit-breakers", headers=headers(token))
        assert r.status_code == 200

    def test_reset_breaker_requires_admin(self, client):
        token = login(client, "trader1")
        r = client.post("/api/circuit-breakers/test/reset", headers=headers(token))
        assert r.status_code == 403


class TestStrategies:
    def test_strategies_list(self, client):
        token = login(client)
        r = client.get("/api/strategies", headers=headers(token))
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_validate_strategy_requires_trader_or_admin(self, client):
        token = login(client, "viewer1")
        r = client.post("/api/strategies/validate", json={}, headers=headers(token))
        assert r.status_code == 403


class TestAdminUI:
    def test_admin_page_loads(self, client):
        r = client.get("/admin")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")

    def test_admin_ui_has_login_form(self, client):
        r = client.get("/admin")
        assert "login-form" in r.text
        assert "doLogin" in r.text

    def test_admin_ui_has_logout(self, client):
        r = client.get("/admin")
        assert "doLogout" in r.text

    def test_admin_ui_has_tabs(self, client):
        r = client.get("/admin")
        html = r.text
        assert 'id="tab-dashboard"' in html
        assert 'id="tab-deployments"' in html
        assert 'id="tab-strategies"' in html
        assert 'id="tab-audit"' in html
        assert 'id="tab-compliance"' in html


# ─── FIXTURES ─────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    # Reset sessions between tests
    _sessions.clear()
    # Ensure default users exist
    _user_store.clear()
    from modelfungible.enterprise.admin_app import _load_users
    _load_users()
    return TestClient(app)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
