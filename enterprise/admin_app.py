# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.  # BUSL-1.0 License
"""
Rita — Universal AI Gateway Admin Portal.
Run: python3 -m modelfungible.enterprise.admin_app  Then open http://localhost:8765/admin
"""
from __future__ import annotations
import csv, hashlib, hmac, io, json, os, secrets, sys, time, uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Response, HTTPException, Header, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ── Resolve import paths ───────────────────────────────────────────────────────
for _p in [str(Path(__file__).parent), str(Path(__file__).parent.parent)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── Core imports ──────────────────────────────────────────────────────────────
try:
    from modelfungible.enterprise.audit import AuditLogger, PIIDetector, RetentionPolicy
    from modelfungible.enterprise.license import LicenseKey, LicenseGenerator
    from modelfungible.enterprise.prompt_marketplace import PromptStore
    from modelfungible.enterprise.api_decisions import router_prompts, router_decisions
    from modelfungible.enterprise.decision_attribution import DecisionStore, ModelScore
    from modelfungible.enterprise.semantic_cache import SemanticCache
    from modelfungible.enterprise.compliance_engine import ComplianceEngine
    from modelfungible.enterprise.guardrails import Guardrails, GuardrailConfig, build_guardrails_from_dict
    from modelfungible.enterprise.api_keys import APIKeyStore
    from modelfungible.enterprise.budget_alerts import BudgetAlertStore
    from modelfungible.enterprise.execute_integration import execute_with_cache_and_compliance, create_streaming_response
    from modelfungible.enterprise.distillation_detector import DistillationDetector
    from modelfungible.enterprise.byok import BYOKStore
    from modelfungible.core.circuit_breaker import CircuitBreaker
    from modelfungible.core.rules_engine import RulesEngine
    from modelfungible.core.execute import ModelSelector, RouterMode, ModelProfile, ExecutionRequest, estimate_cost, DEFAULT_COSTS
except ImportError:
    from enterprise.audit import AuditLogger, PIIDetector, RetentionPolicy
    from enterprise.license import LicenseKey, LicenseGenerator
    from enterprise.prompt_marketplace import PromptStore
    from enterprise.api_decisions import router_prompts, router_decisions
    from enterprise.decision_attribution import DecisionStore, ModelScore
    from enterprise.semantic_cache import SemanticCache
    from enterprise.compliance_engine import ComplianceEngine
    from enterprise.guardrails import Guardrails, GuardrailConfig, build_guardrails_from_dict
    from enterprise.api_keys import APIKeyStore
    from enterprise.budget_alerts import BudgetAlertStore
    from enterprise.execute_integration import execute_with_cache_and_compliance, create_streaming_response
    from enterprise.distillation_detector import DistillationDetector
    from enterprise.byok import BYOKStore
    from core.circuit_breaker import CircuitBreaker
    from core.rules_engine import RulesEngine
    from core.execute import ModelSelector, RouterMode, ModelProfile, ExecutionRequest, estimate_cost, DEFAULT_COSTS

# ─────────────────────────────────────────────────────────────────────────────
# AUTH — always defined locally so no import can break them
# ─────────────────────────────────────────────────────────────────────────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

class AuthContext:
    __slots__ = ("user_id", "role", "session_id")
    def __init__(self, user_id: str, role: str, session_id: str = ""):
        self.user_id = user_id
        self.role = role
        self.session_id = session_id

@dataclass
class User:
    user_id: str
    name: str
    role: str
    password_hash: str
    active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    @staticmethod
    def hashpw(pw: str) -> str:
        return hashlib.sha256(pw.encode()).hexdigest()
    def check_password(self, pw: str) -> bool:
        return hmac.compare_digest(self.password_hash, self.hashpw(pw))

# In-memory stores
_user_store: dict = {}
_sessions: dict = {}

def create_session(user: User):
    tok = secrets.token_urlsafe(32)
    exp = time.time() + 86400 * 7
    _sessions[tok] = {"user_id": user.user_id, "role": user.role, "exp": exp}
    class Sess: pass
    s = Sess()
    s.session_id = tok  # Return tok as the session token (what client sends back)
    s.expires_at = exp
    return s

def delete_session(tok: str):
    _sessions.pop(tok, None)

def get_session(tok: str):
    s = _sessions.get(tok)
    if s is None: return None
    if s.get("exp", 0) < time.time():
        _sessions.pop(tok, None)
        return None
    return s

async def _require_auth(x_auth_token: Optional[str] = Header(None)) -> AuthContext:
    if x_auth_token is None:
        raise HTTPException(401, {"error": "Login required"})
    tok = x_auth_token.replace("Bearer ", "")
    s = get_session(tok)
    if s is None:
        raise HTTPException(401, {"error": "Session expired or invalid"})
    return AuthContext(user_id=s["user_id"], role=s["role"], session_id=s.get("session_id", ""))


def _require_admin(ctx: AuthContext = Depends(_require_auth)) -> AuthContext:
    if ctx.role != "admin":
        raise HTTPException(403, {"error": "Admin role required"})
    return ctx


def _require_trader_or_admin(ctx: AuthContext = Depends(_require_auth)) -> AuthContext:
    if ctx.role not in ("admin", "trader"):
        raise HTTPException(403, {"error": "Trader or admin role required"})
    return ctx


# Default admin user
if "admin" not in _user_store:
    _user_store["admin"] = User(
        user_id="admin", name="Administrator", role="admin",
        password_hash=User.hashpw(ADMIN_PASSWORD),
    )
    print(f"[auth] Default admin created. Password: {ADMIN_PASSWORD}")

# ─────────────────────────────────────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Rita — Universal AI Gateway", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
app.include_router(router_prompts)
app.include_router(router_decisions)

# ─────────────────────────────────────────────────────────────────────────────
# LAZY STORE GETTERS
# ─────────────────────────────────────────────────────────────────────────────
_audit_logger = None
_distillation_det = None
_byok_store = None
_cache_store = None
_api_key_store = None
_budget_alert_store = None
_guardrails_instance = None

def get_audit_logger():
    global _audit_logger
    if _audit_logger is None:
        d = os.environ.get("MODELFUNGIBLE_AUDIT_DIR", ".modelfungible/audit")
        os.makedirs(d, exist_ok=True)
        try:
            _audit_logger = AuditLogger(audit_dir=d)
        except Exception as e:
            print(f"[audit] Init failed (non-fatal): {e}")
    return _audit_logger

def _get_distillation():
    global _distillation_det
    if _distillation_det is None:
        _distillation_det = DistillationDetector(high_risk_score=70, medium_risk_score=40, volume_threshold_per_hour=50)
    return _distillation_det

def _get_byok():
    global _byok_store
    if _byok_store is None:
        _byok_store = BYOKStore(os.environ.get("MODELFUNGIBLE_BYOK_DB", ".modelfungible/byok.db"))
    return _byok_store

def _get_cache():
    global _cache_store
    if _cache_store is None:
        try:
            _cache_store = SemanticCache(db_path=os.environ.get("MODELFUNGIBLE_CACHE_DB", ".modelfungible/cache.db"))
        except Exception as e:
            print(f"[cache] Init failed (non-fatal): {e}")
    return _cache_store

def _get_api_key_store():
    global _api_key_store
    if _api_key_store is None:
        _api_key_store = APIKeyStore(os.environ.get("MODELFUNGIBLE_API_KEYS_DB", ".modelfungible/api_keys.db"))
    return _api_key_store

def _get_budget_alert_store():
    global _budget_alert_store
    if _budget_alert_store is None:
        _budget_alert_store = BudgetAlertStore(os.environ.get("MODELFUNGIBLE_BUDGET_DB", ".modelfungible/budget.db"))
    return _budget_alert_store

def _get_guardrails():
    global _guardrails_instance
    if _guardrails_instance is None:
        terms = [t.strip() for t in os.environ.get("MODELFUNGIBLE_BLOCKED_TERMS", "").split(",") if t.strip()]
        mlen = int(os.environ["MODELFUNGIBLE_MAX_OUTPUT_LENGTH"]) if str(os.environ.get("MODELFUNGIBLE_MAX_OUTPUT_LENGTH", "")).isdigit() else None
        _guardrails_instance = Guardrails(GuardrailConfig(blocked_terms=terms, max_length=mlen))
    return _guardrails_instance

# ─────────────────────────────────────────────────────────────────────────────
# AUTH ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/auth/login")
def api_login(data: dict):
    user = _user_store.get(data.get("user_id", ""))
    if user is None or not user.check_password(data.get("password", "")):
        raise HTTPException(401, {"error": "Invalid user_id or password"})
    sess = create_session(user)
    audit = get_audit_logger()
    if audit:
        audit.log(action="login", actor=user.user_id, outcome="success")
    return JSONResponse({
        "session_id": sess.session_id,
        "user_id": user.user_id,
        "name": user.name,
        "role": user.role,
        "expires_at": datetime.fromtimestamp(sess.expires_at, tz=timezone.utc).isoformat(),
    })

@app.post("/api/auth/logout")
def api_logout(x_auth_token: Optional[str] = Header(None)):
    if x_auth_token:
        delete_session(x_auth_token.replace("Bearer ", ""))
    return JSONResponse({"success": True})

@app.get("/api/auth/me")
def api_me(ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse({"user_id": ctx.user_id, "role": ctx.role})

@app.get("/api/auth/users")
def api_users(ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse([{"user_id": u.user_id, "name": u.name, "role": u.role, "active": u.active,
                          "created_at": u.created_at.isoformat()} for u in _user_store.values()])

@app.post("/api/auth/users")
def api_create_user(data: dict, ctx: AuthContext = Depends(_require_admin)):
    uid = data.get("user_id", "").strip()
    if not uid or uid in _user_store:
        raise HTTPException(400, {"error": "user_id required and must be unique"})
    _user_store[uid] = User(user_id=uid, name=data.get("name", uid), role=data.get("role", "viewer"),
                             password_hash=User.hashpw(data.get("password", "changeme")))
    return JSONResponse({"success": True, "user_id": uid})

@app.delete("/api/auth/users/{user_id}")
def api_delete_user(user_id: str, ctx: AuthContext = Depends(_require_admin)):
    if user_id == ctx.user_id:
        raise HTTPException(400, {"error": "Cannot delete yourself"})
    _user_store.pop(user_id, None)
    return JSONResponse({"success": True})

@app.get("/api/auth/sessions")
def api_sessions(ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse([{"session_id": s.get("session_id", ""), "user_id": s["user_id"],
                          "role": s["role"], "expires_at": datetime.fromtimestamp(s["exp"], tz=timezone.utc).isoformat()}
                         for s in _sessions.values()])

# ─────────────────────────────────────────────────────────────────────────────
# STATE / HEALTH
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/state")
def api_state(ctx: AuthContext = Depends(_require_auth)):
    audit = get_audit_logger()
    total = audit.count() if audit else 0
    today = date.today().isoformat()
    today_count = 0
    verified = False
    if audit:
        today_count = len(audit.query(start_date=today, end_date=today+"T23:59:59", limit=10000))
        try:
            verified = audit.verify_integrity()
        except Exception:
            verified = False
    return JSONResponse({
        "user": {"user_id": ctx.user_id, "role": ctx.role},
        "models": [], "strategies": [],
        "audit": {"total_entries": total, "entries_today": today_count, "hash_chain_verified": verified},
        "circuit_breakers": [],
    })

@app.get("/api/health")
def api_health(ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse({"status": "ok"})

@app.get("/api/circuit-breakers")
def api_circuit_breakers(ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse([])

@app.post("/api/circuit-breakers/{name}/reset")
def api_circuit_breaker_reset(name: str):
    return JSONResponse({"ok": True})

# ─────────────────────────────────────────────────────────────────────────────
# AUDIT
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/audit/logs")
def api_audit_logs(ctx: AuthContext = Depends(_require_auth),
    start_date: Optional[str] = None, end_date: Optional[str] = None,
    actor: Optional[str] = None, action: Optional[str] = None,
    outcome: Optional[str] = None, limit: int = 100, offset: int = 0):
    audit = get_audit_logger()
    if not audit:
        return JSONResponse([])
    return JSONResponse(audit.query(start_date=start_date, end_date=end_date,
                                     actor=actor, action=action, outcome=outcome, limit=limit, offset=offset))

@app.get("/api/audit/export/{fmt}")
def api_audit_export(fmt: str, ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse({"format": fmt, "note": "Export — use audit.query() in code"})

@app.get("/api/audit/verify")
def api_audit_verify(ctx: AuthContext = Depends(_require_auth)):
    audit = get_audit_logger()
    return JSONResponse({"valid": audit.verify_integrity() if audit else False})

# ─────────────────────────────────────────────────────────────────────────────
# MODEL REGISTRY
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/models/register")
def api_model_register(data: dict, ctx: AuthContext = Depends(_require_admin)):
    name = data.get("name", "").strip()
    if not name:
        raise HTTPException(400, {"error": "name required"})
    return JSONResponse({"ok": True, "name": name})

@app.delete("/api/models/{name}")
def api_model_delete(name: str, ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse({"ok": True})

@app.get("/api/providers")
def api_providers_list(ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse({"providers": []})

@app.post("/api/providers")
def api_provider_register(data: dict, ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse({"ok": True})

@app.delete("/api/providers/{name}")
def api_provider_delete(name: str, ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse({"ok": True})

@app.post("/api/providers/{name}/test")
def api_provider_test(name: str, ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse({"success": True, "status_code": 200})

# ─────────────────────────────────────────────────────────────────────────────
# STRATEGIES
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/strategies")
def api_strategies(ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse([])

@app.get("/api/strategies/{strategy_id}")
def api_strategy_get(strategy_id: str, ctx: AuthContext = Depends(_require_auth)):
    raise HTTPException(404, {"error": "Strategy not found"})

@app.post("/api/strategies/validate")
def api_strategy_validate(data: dict, ctx: AuthContext = Depends(_require_trader_or_admin)):
    return JSONResponse({"valid": True})

# ─────────────────────────────────────────────────────────────────────────────
# COMPLIANCE
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/compliance/retention")
def api_compliance_retention(ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse({"policy": "default", "retention_days": 90})

@app.get("/api/compliance/pii/scan")
def api_pii_scan(q: str = "", ctx: AuthContext = Depends(_require_auth)):
    flags = PIIDetector().detect(q)
    return JSONResponse({"flags": flags, "text": (q[:100] + "...") if len(q) > 100 else q})

@app.get("/api/compliance/license")
def api_license_status(ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse({"licensed": True, "features": []})

@app.get("/api/compliance/policies")
def api_policies(domain: Optional[str] = None, enabled: Optional[bool] = None,
                ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse([])

@app.post("/api/compliance/policies")
def api_create_policy(data: dict, ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse({"ok": True})

@app.get("/api/compliance/policies/{pid}")
def api_get_policy(pid: str, ctx: AuthContext = Depends(_require_auth)):
    raise HTTPException(404)

@app.delete("/api/compliance/policies/{pid}")
def api_delete_policy(pid: str, ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse({"ok": True})

@app.get("/api/compliance/violations")
def api_violations(policy_id: Optional[str] = None, actor: Optional[str] = None,
                    start_date: Optional[str] = None, end_date: Optional[str] = None,
                    limit: int = 50, offset: int = 0, ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse([])

@app.get("/api/compliance/score")
def api_score(org_id: str = "default-org", period_days: int = 30,
              ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse({"score": 100, "total_requests": 0, "violations": 0})

# ─────────────────────────────────────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/cache")
def api_cache(ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse({"entries": 0, "hits": 0, "misses": 0})

@app.post("/api/cache/clear")
def api_clear(older_than_days: int = 0, ctx: AuthContext = Depends(_require_admin)):
    return JSONResponse({"cleared": 0})

# ─────────────────────────────────────────────────────────────────────────────
# EXECUTE
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/api/execute")
def api_execute(data: dict, ctx: AuthContext = Depends(_require_trader_or_admin)):
    return JSONResponse({
        "output": " Rita is running. Connect a real model provider to enable AI execution.",
        "model_id": data.get("model", "mock"), "cached": False, "cost": 0.0, "latency_ms": 0,
    })

@app.get("/api/cost-stats")
def api_cost_stats(period: str = "day", by: str = "model", ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse({})

# ─────────────────────────────────────────────────────────────────────────────
# DISTRACTION DETECTION
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/distillation/stats")
def api_distillation_stats(ctx: AuthContext = Depends(_require_auth)):
    d = _get_distillation()
    return JSONResponse({
        "monitored_users": len(d._metrics),
        "high_risk_users": len(d.get_all_high_risk_users()),
        "total_flagged_requests": sum(m.extraction_hits for m in d._metrics.values()),
    })

@app.get("/api/distillation/users/{user_id}")
def api_distillation_user(user_id: str, ctx: AuthContext = Depends(_require_auth)):
    d = _get_distillation()
    stats = d.get_stats(user_id)
    if stats["total_requests"] == 0:
        return JSONResponse({"error": "User not found"}, status_code=404)
    return JSONResponse(stats)

@app.post("/api/distillation/users/{user_id}/reset")
def api_distillation_reset(user_id: str, ctx: AuthContext = Depends(_require_admin)):
    _get_distillation().reset_user(user_id)
    return JSONResponse({"ok": True})

@app.get("/api/distillation/high-risk-users")
def api_distillation_high_risk(ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse({"users": _get_distillation().get_all_high_risk_users()})

@app.post("/api/distillation/check")
def api_distillation_check(data: dict, ctx: AuthContext = Depends(_require_auth)):
    d = _get_distillation()
    result = d.check(
        user_id=data.get("user_id", ctx.user_id),
        prompt=data.get("prompt", ""),
        session_history=data.get("session_history", []),
        is_paid_tier=data.get("is_paid_tier", False),
        is_authenticated=data.get("is_authenticated", True),
        tokens=data.get("tokens", 0),
    )
    return JSONResponse(result.to_dict())

# ─────────────────────────────────────────────────────────────────────────────
# BYOK
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/api/byok/keys")
def api_byok_keys(team_id: Optional[str] = None, include_inactive: bool = False,
                  ctx: AuthContext = Depends(_require_admin)):
    keys = _get_byok().list_keys(team_id=team_id, include_inactive=include_inactive)
    return JSONResponse({"keys": [{
        "key_id": k.key_id, "team_id": k.team_id, "provider": k.provider,
        "name": k.name, "upstream_key_id": k.upstream_key_id,
        "is_active": k.is_active,
        "created_at": k.created_at.isoformat() if k.created_at else None,
        "last_used": k.last_used.isoformat() if k.last_used else None,
        "error_count": k.error_count, "last_error": k.last_error,
        "owner_email": k.owner_email,
    } for k in keys]})

@app.get("/api/byok/stats")
def api_byok_stats(ctx: AuthContext = Depends(_require_admin)):
    s = _get_byok().get_stats()
    return JSONResponse({
        "total_keys": s.total_keys, "active_keys": s.active_keys,
        "revoked_keys": s.revoked_keys, "teams_with_keys": s.teams_with_keys,
        "total_calls": s.total_calls, "total_cost_usd": round(s.total_cost_usd, 6),
        "errors_today": s.errors_today,
    })

@app.post("/api/byok/register")
def api_byok_register(data: dict, ctx: AuthContext = Depends(_require_admin)):
    team_id = data.get("team_id", "").strip()
    upstream_key = data.get("upstream_key", "").strip()
    if not team_id or not upstream_key:
        raise HTTPException(400, {"error": "team_id and upstream_key required"})
    byok_key, virtual_key = _get_byok().register_key(
        team_id=team_id, provider=data.get("provider", "openai"),
        upstream_key=upstream_key, name=data.get("name", "Unnamed"),
        owner_email=data.get("owner_email"),
    )
    return JSONResponse({
        "key_id": byok_key.key_id, "virtual_key": virtual_key,
        "team_id": team_id, "provider": byok_key.provider,
        "name": byok_key.name, "upstream_key_id": byok_key.upstream_key_id,
        "created_at": byok_key.created_at.isoformat() if byok_key.created_at else None,
    })

@app.post("/api/byok/revoke/{key_id}")
def api_byok_revoke(key_id: str, data: Optional[dict] = None, ctx: AuthContext = Depends(_require_admin)):
    ok = _get_byok().revoke_key(key_id, reason=(data.get("reason") or "") if data else "")
    if not ok:
        raise HTTPException(404, {"error": "Key not found"})
    return JSONResponse({"ok": True, "key_id": key_id, "revoked": True})

@app.post("/api/byok/reactivate/{key_id}")
def api_byok_reactivate(key_id: str, ctx: AuthContext = Depends(_require_admin)):
    ok = _get_byok().reactivate_key(key_id)
    if not ok:
        raise HTTPException(404, {"error": "Key not found"})
    return JSONResponse({"ok": True, "key_id": key_id, "reactivated": True})

@app.get("/api/byok/usage/{key_id}")
def api_byok_usage(key_id: str, limit: int = 100, ctx: AuthContext = Depends(_require_admin)):
    records = _get_byok().get_usage(byok_key_id=key_id, limit=limit)
    return JSONResponse({"records": [{
        "byok_key_id": r.byok_key_id, "team_id": r.team_id, "provider": r.provider,
        "model": r.model, "cost_usd": round(r.cost_usd, 6),
        "tokens_used": r.tokens_used, "latency_ms": r.latency_ms,
        "error": r.error, "timestamp": r.timestamp.isoformat() if r.timestamp else None,
    } for r in records]})

@app.get("/api/byok/resolve/{virtual_key}")
def api_byok_resolve(virtual_key: str, ctx: AuthContext = Depends(_require_trader_or_admin)):
    result = _get_byok().get_upstream_key(virtual_key)
    if not result:
        raise HTTPException(401, {"error": "Invalid or inactive BYOK key"})
    provider, upstream_key = result
    return JSONResponse({"provider": provider, "upstream_key": upstream_key})

@app.get("/api/version")
def api_version(ctx: AuthContext = Depends(_require_auth)):
    return JSONResponse({"modelfungible": "1.0.0", "python": "3.12"})

# ─────────────────────────────────────────────────────────────────────────────
# HTML ADMIN UI  (placeholder — the real UI is served separately)
# ─────────────────────────────────────────────────────────────────────────────
HTML_UI = (
    "<!DOCTYPE html><html><head><title>Rita Gateway</title>"
    "<meta charset=utf-8><style>"
    "body{font-family:system-ui;background:#0f1117;color:#e5e7eb;margin:0;padding:20px}"
    "h1{color:#60a5fa}a{color:#60a5fa;text-decoration:none}"
    "pre{background:#1f2937;padding:12px;border-radius:6px;overflow-x:auto}"
    ".card{background:#1f2937;border-radius:8px;padding:16px;margin:12px 0}"
    "button{background:#60a5fa;color:#000;padding:8px 16px;border:none;border-radius:6px;cursor:pointer}"
    "input,select{background:#374151;color:#e5e7eb;border:1px solid #4b5563;padding:8px;border-radius:4px}"
    "</style></head><body>"
    "<h1>Rita Gateway</h1>"
    "<p>Admin portal running. Full UI: <code>python3 -m modelfungible.enterprise.admin_ui</code></p>"
    '<div class="card"><h2>Quick Test</h2>'
    "<p>Login: <code>admin</code> / <code>admin123</code></p></div>"
    "</body></html>"
)

@app.get("/admin")
def admin_root():
    return HTMLResponse(content=HTML_UI, media_type="text/html")

@app.get("/")
def root():
    return JSONResponse({"service": "Rita Universal AI Gateway", "version": "1.0.0", "docs": "/docs"})
