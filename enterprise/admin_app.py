# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
ModelFungible Enterprise — Admin Web UI (FastAPI)

Run with:
    python3 -m modelfungible.enterprise.admin_app

Then open http://localhost:8765/admin
"""

from __future__ import annotations

import csv, io, json, os, sys, uuid, secrets, hashlib, time
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Response, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

try:
    from modelfungible.enterprise.audit import AuditLogger, PIIDetector, RetentionPolicy
    from modelfungible.enterprise.license import LicenseKey
    from modelfungible.enterprise.prompt_marketplace import PromptStore
    from modelfungible.enterprise.decision_attribution import DecisionStore, ModelScore
    from modelfungible.enterprise.semantic_cache import SemanticCache
    from modelfungible.enterprise.compliance_engine import ComplianceEngine
    from modelfungible.enterprise.guardrails import Guardrails, GuardrailConfig, build_guardrails_from_dict
    from modelfungible.enterprise.api_keys import APIKeyStore
    from modelfungible.enterprise.budget_alerts import BudgetAlertStore
    from modelfungible.enterprise.execute_integration import execute_with_cache_and_compliance, create_streaming_response
    from modelfungible.core.circuit_breaker import CircuitBreaker
    from modelfungible.core.rules_engine import RulesEngine
    from modelfungible.core.execute import ModelSelector, RouterMode, ModelProfile, ExecutionRequest, estimate_cost, DEFAULT_COSTS
    from fastapi.responses import StreamingResponse, JSONResponse as _JR
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from enterprise.audit import AuditLogger, PIIDetector, RetentionPolicy
    from enterprise.license import LicenseKey
    from enterprise.prompt_marketplace import PromptStore
    from enterprise.decision_attribution import DecisionStore, ModelScore
    from enterprise.semantic_cache import SemanticCache
    from enterprise.compliance_engine import ComplianceEngine
    from enterprise.guardrails import Guardrails, GuardrailConfig, build_guardrails_from_dict
    from enterprise.distillation_detector import DistillationDetector
    from enterprise.distillation_detector import DistillationDetector
    from enterprise.api_keys import APIKeyStore
    from enterprise.budget_alerts import BudgetAlertStore
    from enterprise.execute_integration import execute_with_cache_and_compliance, create_streaming_response
    from core.circuit_breaker import CircuitBreaker
    from core.rules_engine import RulesEngine
    from core.execute import ModelSelector, RouterMode, ModelProfile, ExecutionRequest, estimate_cost, DEFAULT_COSTS
    from fastapi.responses import StreamingResponse, JSONResponse as _JR

app = FastAPI(title="ModelFungible Enterprise Admin", version="1.0.0")

# Register API routers
app.include_router(router_prompts)
app.include_router(router_decisions)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])



# ─── AUTH ENDPOINTS ───────────────────────────────────────────────────────────

@app.post("/api/auth/login")
def api_login(data: dict):
    """Login with user_id + password. Returns session token."""
    user = _user_store.get(data.get("user_id", ""))
    if user is None or not user.check_password(data.get("password", "")):
        raise HTTPException(401, {"error": "Invalid user_id or password"})
    sess = create_session(user)
    return JSONResponse({
        "session_id": sess.session_id,
        "user_id": user.user_id,
        "name": user.name,
        "role": user.role,
        "expires_at": datetime.fromtimestamp(sess.expires_at).isoformat(),
    })

@app.post("/api/auth/logout")
def api_logout(x_auth_token: Optional[str] = Header(None)):
    """Logout — destroy session."""
    if x_auth_token:
        tok = x_auth_token.replace("Bearer ", "")
        delete_session(tok)
    return JSONResponse({"success": True})

@app.get("/api/auth/me")
def api_me(ctx: AuthContext = require_auth()):
    """Get current user info."""
    user = _user_store.get(ctx.user_id)
    return JSONResponse({
        "user_id": ctx.user_id,
        "name": user.name if user else ctx.user_id,
        "role": ctx.role,
    })

@app.get("/api/auth/users")
def api_users(ctx: AuthContext = require_admin()):
    """List all users (admin only)."""
    return JSONResponse([{"user_id": u.user_id, "name": u.name, "role": u.role, "active": u.active}
                         for u in _user_store.values()])

@app.post("/api/auth/users")
def api_create_user(data: dict, ctx: AuthContext = require_admin()):
    """Create new user (admin only)."""
    uid = data.get("user_id", "").strip()
    if not uid or uid in _user_store:
        raise HTTPException(400, "user_id required and must be unique")
    _user_store[uid] = User(user_id=uid, name=data.get("name", uid),
                             role=data.get("role", "viewer"),
                             password_hash=User.hashpw(data.get("password", "changeme")))
    return JSONResponse({"success": True, "user_id": uid})

@app.delete("/api/auth/users/{user_id}")
def api_delete_user(user_id: str, ctx: AuthContext = require_admin()):
    if user_id == ctx.user_id:
        raise HTTPException(400, "Cannot delete yourself")
    if user_id not in _user_store:
        raise HTTPException(404, "User not found")
    del _user_store[user_id]
    return JSONResponse({"success": True})

@app.get("/api/auth/sessions")
def api_sessions(ctx: AuthContext = require_admin()):
    """List active sessions."""
    return JSONResponse([{"session_id": s.session_id, "user_id": s.user_id,
                          "role": s.role, "expires_at": datetime.fromtimestamp(s.expires_at).isoformat()}
                         for s in _sessions.values()])


@app.get("/api/state")
def api_state(ctx: AuthContext = require_auth()):
    audit = get_audit_logger()
    total = audit.count() if audit else 0
    today = date.today().isoformat()
    today_count = 0
    verified = False
    if audit:
        today_count = len(audit.query(start_date=today, end_date=today+"T23:59:59", limit=10000))
        verified = audit.verify_integrity()
    return JSONResponse({
        "user": {"user_id": ctx.user_id, "role": ctx.role},
        "models": _registry.list_models(),
        "strategies": _registry.list_strategies(RULES_PATH),
        "audit": {"total_entries": total, "entries_today": today_count, "hash_chain_verified": verified, "audit_dir": _audit_dir},
        "circuit_breakers": _registry.list_circuit_breakers(),
        "rules_path": RULES_PATH,
    })

@app.get("/api/health")
def api_health(ctx: AuthContext = require_auth()):
    return JSONResponse({"models": [{"name": m["name"], "provider": m["provider"], "latency_ms_p50": m["latency_ms_p50"], "health": m["health"], "circuit_state": m["circuit_state"]} for m in _registry.list_models()]})

@app.get("/api/circuit-breakers")
def api_circuit_breakers(ctx: AuthContext = require_auth()):
    return JSONResponse({"breakers": _registry.list_circuit_breakers()})

@app.post("/api/circuit-breakers/{name}/reset")
def api_circuit_breaker_reset(name: str):
    try:
        r = _registry.reset_breaker(name)
        return JSONResponse({"success": True, **r})
    except ValueError as e:
        raise HTTPException(404, str(e))

@app.get("/api/audit/logs")
def api_audit_logs(ctx: AuthContext = require_auth(), start_date: Optional[str] = None, end_date: Optional[str] = None, actor: Optional[str] = None, action: Optional[str] = None, outcome: Optional[str] = None, limit: int = 100, offset: int = 0):
    audit = get_audit_logger()
    if audit is None:
        return JSONResponse({"entries": [], "total": 0})
    entries = audit.query(start_date=start_date, end_date=end_date, actor=actor, action=action, outcome=outcome, limit=limit, offset=offset)
    return JSONResponse({"entries": entries, "total": audit.count(), "limit": limit, "offset": offset})

@app.get("/api/audit/export/{fmt}")
def api_audit_export(fmt: str, ctx: AuthContext = require_auth()):
    audit = get_audit_logger()
    if audit is None:
        raise HTTPException(503, "Audit logger unavailable")
    if fmt not in ("json", "csv"):
        raise HTTPException(400, "Format must be json or csv")
    entries = audit.entries()
    if fmt == "json":
        return Response(content=json.dumps(entries, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": "attachment; filename=audit_export.json"})
    buf = io.StringIO()
    if entries:
        flds = ["entry_id", "sequence", "timestamp", "action", "actor", "org_id", "strategy_id", "model_id", "outcome", "pii_detected", "compliance_stamp", "hash"]
        w = csv.DictWriter(buf, fieldnames=flds, extrasaction="ignore")
        w.writeheader()
        w.writerows(entries)
    return Response(content=buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=audit_export.csv"})

@app.get("/api/audit/verify")
def api_audit_verify(ctx: AuthContext = require_auth()):
    audit = get_audit_logger()
    if audit is None:
        return JSONResponse({"verified": False, "error": "Audit logger unavailable"})
    return JSONResponse({"verified": audit.verify_integrity()})

@app.post("/api/models/register")
def api_model_register(data: dict, ctx: AuthContext = require_admin()):
    for f in ["name", "provider", "model_id", "api_key", "latency_ms_p50", "capability"]:
        if f not in data:
            raise HTTPException(400, f"Missing field: {f}")
    try:
        model = _registry.register_model(
            name=data["name"], provider=data["provider"], model_id=data["model_id"],
            api_key=data["api_key"], latency_ms_p50=int(data["latency_ms_p50"]),
            capability=data["capability"],
            cost_input_per_1k=float(data.get("cost_input_per_1k", 0.001)),
            cost_output_per_1k=float(data.get("cost_output_per_1k", 0.002)),
        )
        return JSONResponse({"success": True, "model": model})
        return JSONResponse({"success": True, "model": model})
    except ValueError as e:
        raise HTTPException(409, str(e))

@app.delete("/api/models/{name}")
def api_model_delete(name: str, ctx: AuthContext = require_admin()):
    if _registry.deregister_model(name):
        return JSONResponse({"success": True})
    raise HTTPException(404, f"Model not found: {name}")

# Custom LLM Providers
_custom_providers: dict[str, dict] = {}

@app.get("/api/providers")
def api_providers_list(ctx: AuthContext = require_admin()):
    return JSONResponse({"providers": [{"name": n, "base_url": p["base_url"], "api_key": "***" if p.get("api_key") else "", "supports_system_prompt": p.get("supports_system_prompt", True), "default_model": p.get("default_model") or ""} for n, p in _custom_providers.items()]})

@app.post("/api/providers")
def api_provider_register(data: dict, ctx: AuthContext = require_admin()):
    for f in ["name", "base_url"]:
        if not str(data.get(f, "")).strip():
            raise HTTPException(400, f"Missing: {f}")
    name = str(data["name"]).strip().lower().replace(" ", "-")
    if not re.match(r"^[a-z0-9_-]+$", name):
        raise HTTPException(400, "Name: alphanumeric + dashes/underscores only")
    _custom_providers[name] = {"name": name, "base_url": str(data["base_url"]).strip().rstrip("/"), "api_key": data.get("api_key", ""), "supports_system_prompt": bool(data.get("supports_system_prompt", True)), "default_model": str(data.get("default_model", "")).strip() or None}
    return JSONResponse({"success": True, "provider": {"name": name, "base_url": _custom_providers[name]["base_url"], "supports_system_prompt": _custom_providers[name]["supports_system_prompt"], "default_model": _custom_providers[name]["default_model"] or ""}})

@app.delete("/api/providers/{name}")
def api_provider_delete(name: str, ctx: AuthContext = require_admin()):
    name = name.lower()
    if name not in _custom_providers:
        raise HTTPException(404, f"Provider not found: {name}")
    del _custom_providers[name]
    return JSONResponse({"success": True})

@app.post("/api/providers/{name}/test")
def api_provider_test(name: str, ctx: AuthContext = require_admin()):
    name = name.lower()
    if name not in _custom_providers:
        raise HTTPException(404, f"Provider not found: {name}")
    p = _custom_providers[name]
    try:
        import requests as _req
        r = _req.get(f"{p['base_url'].rstrip('/')}/models", headers={"Authorization": f"Bearer {p.get('api_key', '')}"}, timeout=10)
        return JSONResponse({"success": r.status_code < 400, "status_code": r.status_code, "message": "OK" if r.status_code < 400 else r.text[:200]})
    except Exception as e:
        return JSONResponse({"success": False, "status_code": 0, "message": str(e)})

@app.get("/api/strategies")
def api_strategies(ctx: AuthContext = require_auth()):
    return JSONResponse({"strategies": _registry.list_strategies(RULES_PATH)})

@app.get("/api/strategies/{strategy_id}")
def api_strategy_get(strategy_id: str, ctx: AuthContext = require_auth()):
    s = _registry.get_strategy(RULES_PATH, strategy_id)
    if s is None:
        raise HTTPException(404, f"Strategy not found: {strategy_id}")
    return JSONResponse(s)

@app.post("/api/strategies/validate")
def api_strategy_validate(data: dict, ctx: AuthContext = require_trader_or_admin()):
    return JSONResponse(_registry.validate_strategy_json(data))

@app.get("/api/compliance/retention")
def api_compliance_retention(ctx: AuthContext = require_auth()):
    policy = os.environ.get("MODELFUNGIBLE_RETENTION_POLICY", "default")
    days = int(os.environ.get("MODELFUNGIBLE_RETENTION_DAYS", "90"))
    return JSONResponse({"policy": policy, "max_age_days": days, "available_policies": RetentionPolicy.POLICIES})

@app.get("/api/compliance/pii/scan")
def api_pii_scan(q: str = "", ctx: AuthContext = require_auth()):
    flags = PIIDetector().scan({"sample": q})
    return JSONResponse({"detected": list(flags)})

@app.get("/api/compliance/license")
def api_license_status(ctx: AuthContext = require_admin()):
    try:
        key = LicenseKey.load_license()
    except FileNotFoundError:
        return JSONResponse({"licensed": False, "error": "No license installed"})
    except Exception as e:
        return JSONResponse({"licensed": False, "error": str(e)})
    result = LicenseKey.validate(key, os.environ.get("MODELFUNGIBLE_LICENSE_SECRET", ""))
    return JSONResponse({"licensed": result.get("valid", False), "customer_id": result.get("customer_id", "unknown"), "expiry": result.get("expiry", "unknown"), "seats": result.get("seats", 0), "features": result.get("features", []), "plan": result.get("plan", "unknown")})

@app.apiGet("/api/version")
def api_version(ctx: AuthContext = require_auth()):
    try:
        from modelfungible import __version__
    except Exception:
        __version__ = "unknown"
    return JSONResponse({"modelfungible": __version__, "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"})

# ─── UNIVERSAL LLM PROXY ───────────────────────────────────────────────────────

def _get_adapter(registry, model_name):
    m = registry._models.get(model_name)
    if not m:
        return None, None
    p = m["provider"].lower()
    key = m["api_key"]
    mid = m["model_id"]
    if p in ("openai", "openai-compatible", ""):
        from modelfungible.adapters.openai import OpenAIAdapter
        return OpenAIAdapter(api_key=key), mid
    elif p == "anthropic":
        from modelfungible.adapters.anthropic import AnthropicAdapter
        return AnthropicAdapter(api_key=key), mid
    elif p == "groq":
        from modelfungible.adapters.groq import GroqAdapter
        return GroqAdapter(api_key=key), mid
    elif p == "ollama":
        from modelfungible.enterprise.adapters.ollama import OllamaAdapter
        return OllamaAdapter(base_url=key or "http://localhost:11434"), mid
    elif p == "vertexai":
        from modelfungible.enterprise.adapters.vertexai import VertexAIAdapter
        return VertexAIAdapter(credentials_path=key or None), mid
    elif p == "minimax":
        return MiniMaxAdapter(api_key=key), mid
    elif p in ("moonshot", "kimi"):
        return MoonshotAdapter(api_key=key), mid
    elif p == "glm":
        return GLMAdapter(api_key=key), mid
    elif p == "owen":
        return OwenAdapter(api_key=key), mid
    elif p.startswith("custom:"):
        cname = p.split(":", 1)[1]
        if cname in _custom_providers:
            cp = _custom_providers[cname]
            return CustomAdapter(provider_name=cname, base_url=cp["base_url"], api_key=cp.get("api_key", ""), supports_system_prompt=cp.get("supports_system_prompt", True), default_model=cp.get("default_model")), mid
        return None, None
    else:
        from modelfungible.adapters.openai import OpenAIAdapter
        return OpenAIAdapter(api_key=key, base_url=p), mid



@app.post("/api/execute")
def api_execute(data: dict, ctx: AuthContext = require_trader_or_admin()):
    """
    Universal LLM proxy. Set stream=true for SSE streaming.
    Includes: semantic cache, compliance pre-check, PII redaction, cost tracking.
    """
    if data.get("stream", False):
        return create_streaming_response(
            data=data, ctx=ctx, registry=_registry,
            get_audit_logger_fn=get_audit_logger,
            get_cache_fn=get_cache, get_compliance_fn=get_compliance,
            get_guardrails_fn=_get_guardrails,
            get_api_key_store_fn=_get_api_key_store,
            get_budget_alert_store_fn=_get_budget_alert_store,
            get_distillation_fn=_get_distillation,
            build_model_profiles_fn=_build_model_profiles,
            get_adapter_fn=_get_adapter,
            RouterMode=RouterMode, ModelSelector=ModelSelector,
            ModelProfile=ModelProfile, ExecutionRequest=ExecutionRequest,
            estimate_cost=estimate_cost, PIIDetector=PIIDetector,
        )
    result = execute_with_cache_and_compliance(
        data=data, ctx=ctx, registry=_registry,
        get_audit_logger_fn=get_audit_logger,
        get_decision_store_fn=get_decision_store,
        get_cache_fn=get_cache, get_compliance_fn=get_compliance,
        get_guardrails_fn=_get_guardrails,
        get_api_key_store_fn=_get_api_key_store,
        get_budget_alert_store_fn=_get_budget_alert_store,
        get_distillation_fn=_get_distillation,
        build_model_profiles_fn=_build_model_profiles,
        get_adapter_fn=_get_adapter,
        RouterMode=RouterMode, ModelSelector=ModelSelector,
        ModelProfile=ModelProfile, ExecutionRequest=ExecutionRequest,
        estimate_cost=estimate_cost, PIIDetector=PIIDetector,
    )
    return _JR(content=json.dumps(result), media_type="application/json")


@app.get("/api/cost-stats")
def api_cost_stats(
    period: str = "day",
    by: str = "model",
    ctx: AuthContext = require_auth(),
):
    """Cost statistics by model or user."""
    from datetime import datetime, timedelta, timezone
    audit = get_audit_logger()
    if audit is None:
        return JSONResponse({"error": "Audit unavailable"}, status_code=503)

    now = datetime.now(timezone.utc)
    if period == "day":
        start = (now - timedelta(days=1)).isoformat()
    elif period == "week":
        start = (now - timedelta(weeks=1)).isoformat()
    else:
        start = (now - timedelta(days=30)).isoformat()

    entries = audit.query(start_date=start, action="model_execute", limit=10000)

    by_model = {}
    by_user = {}
    total_cost = 0.0
    for e in entries:
        m = e.get("metadata", {})
        if m.get("outcome") == "error":
            continue
        c = m.get("cost_usd", 0.0)
        total_cost += c
        mn = m.get("model_selected", "unknown")
        u = e.get("actor", "unknown")
        by_model[mn] = by_model.get(mn, 0.0) + c
        by_user[u] = by_user.get(u, 0.0) + c

    def fmt(items):
        return [{"key": k, "cost_usd": round(v, 4),
                 "pct": f"{100*v/max(total_cost,0.001):.1f}%"}
                for k, v in sorted(items, key=lambda x: -x[1])]

    if by == "model":
        data = fmt(by_model.items())
    elif by == "user":
        data = fmt(by_user.items())
    else:
        data = {"total_cost_usd": round(total_cost, 4), "total_calls": len(entries),
                "by_model": fmt(by_model.items()), "by_user": fmt(by_user.items())}

    return JSONResponse({"period": period, "data": data})


# ─── COMPLIANCE + CACHE ENDPOINTS ────────────────────────────────────────────────

@app.get("/api/compliance/policies")
def api_policies(domain: Optional[str]=None, enabled: Optional[bool]=None, ctx: AuthContext=require_auth()):
    c = get_compliance()
    if not c: return JSONResponse({"policies":[]})
    return JSONResponse({"policies":[{"policy_id":p.policy_id,"name":p.name,"domain":p.domain,"enabled":p.enabled,"priority":p.priority,"conditions":p.conditions,"actions":p.actions,"tags":p.tags,"created_by":p.created_by,"created_at":p.created_at} for p in c.list_policies(domain=domain,enabled=enabled)]})

@app.post("/api/compliance/policies")
def api_create_policy(data: dict, ctx: AuthContext=require_admin()):
    c = get_compliance()
    if not c: return JSONResponse({"error":"unavailable"},status_code=503)
    pol = c.create_policy(name=data["name"],conditions=data.get("conditions",[]),actions=data.get("actions",{"on_fail":"block","on_pass":"allow"}),created_by=ctx.user_id,description=data.get("description",""),domain=data.get("domain","general"),priority=int(data.get("priority",0)),tags=data.get("tags",[]))
    return JSONResponse({"policy_id":pol.policy_id})

@app.get("/api/compliance/policies/{pid}")
def api_get_policy(pid: str, ctx: AuthContext=require_auth()):
    c = get_compliance()
    if not c: return JSONResponse({})
    p = c.get_policy(pid)
    if not p: raise HTTPException(404)
    return JSONResponse({"policy_id":p.policy_id,"name":p.name,"conditions":p.conditions,"actions":p.actions,"enabled":p.enabled,"priority":p.priority})

@app.delete("/api/compliance/policies/{pid}")
def api_delete_policy(pid: str, ctx: AuthContext=require_admin()):
    c = get_compliance()
    if not c: return JSONResponse({"error":"unavailable"},status_code=503)
    c.delete_policy(pid)
    return JSONResponse({"deleted":True})

@app.get("/api/compliance/violations")
def api_violations(policy_id: Optional[str]=None, actor: Optional[str]=None, start_date: Optional[str]=None, end_date: Optional[str]=None, limit: int=50, offset: int=0, ctx: AuthContext=require_auth()):
    c = get_compliance()
    if not c: return JSONResponse({"violations":[]})
    return JSONResponse({"violations":c.get_violations(policy_id=policy_id,actor=actor,start_date=start_date,end_date=end_date,limit=limit,offset=offset)})

@app.get("/api/compliance/score")
def api_score(org_id: str="default-org", period_days: int=30, ctx: AuthContext=require_auth()):
    c = get_compliance()
    if not c: return JSONResponse({"error":"unavailable"})
    return JSONResponse(c.get_compliance_score(org_id=org_id,period_days=period_days))

@app.get("/api/cache/stats")
def api_cache(ctx: AuthContext=require_auth()):
    cache = get_cache()
    if not cache: return JSONResponse({"error":"Cache unavailable"})
    return JSONResponse(cache.stats())

@app.post("/api/cache/clear")
def api_clear(older_than_days: int=0, ctx: AuthContext=require_admin()):
    cache = get_cache()
    if not cache: return JSONResponse({"error":"Cache unavailable"})
    return JSONResponse({"cleared": cache.clear(older_than_days=older_than_days)})


# ─── DISTILLATION DETECTION ───────────────────────────────────────────────────

@app.get("/api/distillation/stats")
def api_distillation_stats(ctx: AuthContext=require_auth()):
    """Get overall distillation detection statistics."""
    d = _get_distillation()
    stats = {
        "monitored_users": len(d._metrics),
        "high_risk_users": len(d.get_all_high_risk_users()),
        "total_flagged_requests": sum(m.extraction_hits for m in d._metrics.values()),
    }
    return JSONResponse(stats)


@app.get("/api/distillation/users/{user_id}")
def api_distillation_user(user_id: str, ctx: AuthContext=require_auth()):
    """Get distillation stats for a specific user."""
    d = _get_distillation()
    stats = d.get_stats(user_id)
    if stats["total_requests"] == 0:
        return JSONResponse({"error": "User not found"}, status_code=404)
    return JSONResponse(stats)


@app.post("/api/distillation/users/{user_id}/reset")
def api_distillation_reset(user_id: str, ctx: AuthContext=require_admin()):
    """Reset distillation metrics for a user (admin only)."""
    d = _get_distillation()
    d.reset_user(user_id)
    return JSONResponse({"ok": True, "user_id": user_id})


@app.get("/api/distillation/high-risk-users")
def api_distillation_high_risk(ctx: AuthContext=require_auth()):
    """Get all high-risk users flagged for potential distillation."""
    d = _get_distillation()
    return JSONResponse({"users": d.get_all_high_risk_users()})


@app.post("/api/distillation/check")
def api_distillation_check(data: dict, ctx: AuthContext=require_auth()):
    """Manually check a prompt for distillation risk (for testing/admin review)."""
    d = _get_distillation()
    user_id = data.get("user_id", ctx.user_id)
    prompt = data.get("prompt", "")
    session_history = data.get("session_history", [])
    is_paid = data.get("is_paid_tier", False)
    is_authenticated = data.get("is_authenticated", True)
    tokens = data.get("tokens", 0)
    result = d.check(user_id, prompt, session_history=session_history,
                     is_paid_tier=is_paid, is_authenticated=is_authenticated, tokens=tokens)
    return JSONResponse(result.to_dict())


HTML_UI = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>ModelFungible Enterprise Admin</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0f1117;--card:#1a1f2e;--card2:#212738;--border:#2a3148;--accent:#4ade80;--accent2:#22c55e;--text:#e2e8f0;--text2:#94a3b8;--text3:#64748b;--red:#f87171;--yellow:#fbbf24;--blue:#60a5fa;--sidebar:#0d1117}
body{font-family:SegoeUI,system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.layout{display:flex;min-height:100vh}
.sidebar{width:220px;background:var(--sidebar);border-right:1px solid var(--border);display:flex;flex-direction:column;position:fixed;height:100vh;overflow-y:auto;z-index:100}
.sidebar-header{padding:20px 16px 16px;border-bottom:1px solid var(--border)}
.sidebar-header h1{font-size:15px;font-weight:700;color:var(--accent)}
.sidebar-header p{font-size:11px;color:var(--text3)}
.sidebar-nav{padding:12px 8px;flex:1}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;color:var(--text2);cursor:pointer;font-size:13px;transition:all .15s;user-select:none;margin-bottom:2px}
.nav-item:hover{background:var(--card);color:var(--text)}
.nav-item.active{background:#1a2332;color:var(--accent);font-weight:600}
.nav-item .icon{font-size:16px;width:20px;text-align:center;flex-shrink:0}
.sidebar-footer{padding:12px 16px;border-top:1px solid var(--border);font-size:11px;color:var(--text3)}
.main{flex:1;margin-left:220px;padding:28px 32px;max-width:1200px}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:28px;flex-wrap:wrap;gap:12px}
.topbar h2{font-size:22px;font-weight:700}
.badge{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;font-size:12px;font-weight:600}
.badge-green{background:rgba(74,222,128,.15);color:var(--accent)}
.badge-red{background:rgba(248,113,113,.15);color:var(--red)}
.badge-yellow{background:rgba(251,191,36,.15);color:var(--yellow)}
.badge-blue{background:rgba(96,165,250,.15);color:var(--blue)}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:20px}
.card h3{font-size:12px;color:var(--text2);text-transform:uppercase;letter-spacing:.5px;margin-bottom:14px;font-weight:600}
.card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}
.stat-card{background:var(--card2);border-radius:10px;padding:16px}
.stat-card .val{font-size:28px;font-weight:700;color:var(--accent)}
.stat-card .label{font-size:12px;color:var(--text2);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px 12px;color:var(--text3);font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
td{padding:10px 12px;border-bottom:1px solid rgba(42,49,72,.5);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.02)}
.mono{font-family:CascadiaCode,FiraCode,monospace;font-size:12px}
.form-row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.form-group{display:flex;flex-direction:column;gap:5px;flex:1;min-width:150px}
.form-group label{font-size:12px;color:var(--text2);font-weight:500}
input,select,textarea{background:var(--card2);border:1px solid var(--border);border-radius:8px;padding:8px 12px;color:var(--text);font-size:13px;outline:none;transition:border-color .15s;width:100%}
input:focus,select:focus,textarea:focus{border-color:var(--accent)}
textarea{resize:vertical;min-height:100px;font-family:inherit}
select{cursor:pointer}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer;border:none;transition:all .15s}
.btn-primary{background:var(--accent);color:#0f1117}
.btn-primary:hover{background:var(--accent2)}
.btn-danger{background:rgba(248,113,113,.15);color:var(--red)}
.btn-danger:hover{background:rgba(248,113,113,.25)}
.btn-ghost{background:transparent;color:var(--text2);border:1px solid var(--border)}
.btn-ghost:hover{background:var(--card2);color:var(--text)}
.btn-sm{padding:5px 10px;font-size:12px}
.tab{display:none}.tab.active{display:block}
.json-viewer{background:#0a0d14;border-radius:8px;padding:14px;overflow-x:auto;max-height:500px;overflow-y:auto}
.json-viewer pre{margin:0;font-family:CascadiaCode,FiraCode,monospace;font-size:12px;line-height:1.6;color:var(--text2)}
.json-key{color:#93c5fd}.json-str{color:#86efac}.json-num{color:#fcd34d}.json-bool{color:#c4b5fd}.json-null{color:#94a3b8}
.feed-item{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid rgba(42,49,72,.5);font-size:13px;align-items:baseline}
.feed-item:last-child{border-bottom:none}
.feed-time{color:var(--text3);font-size:11px;white-space:nowrap;min-width:140px}
.feed-action{font-weight:600;min-width:140px}
.feed-actor{color:var(--blue);flex:1}
.filter-bar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px;align-items:flex-end}
.filter-bar .form-group{min-width:130px;flex:0 1 auto;margin-bottom:0}
.alert{padding:12px 16px;border-radius:8px;font-size:13px;margin-bottom:14px}
.alert-success{background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.3);color:var(--accent)}
.alert-error{background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);color:var(--red)}
.alert-warning{background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.3);color:var(--yellow)}
.model-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.model-card{background:var(--card2);border-radius:10px;padding:16px;display:flex;flex-direction:column;gap:8px}
.model-card .name{font-size:14px;font-weight:700}
.model-card .meta{font-size:12px;color:var(--text2)}
.model-card .actions{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.strategy-list{display:flex;flex-direction:column;gap:4px;max-height:600px;overflow-y:auto}
.strategy-item{padding:10px 12px;border-radius:8px;cursor:pointer;font-size:13px;transition:all .15s}
.strategy-item:hover{background:var(--card2)}
.strategy-item.active{background:#1a2332;border-left:3px solid var(--accent)}
.pagination{display:flex;align-items:center;gap:12px;margin-top:16px}
.pagination .info{font-size:12px;color:var(--text2)}
.two-col{display:grid;grid-template-columns:260px 1fr;gap:20px;align-items:start}
.sbox,.ebox{padding:12px 16px;border-radius:8px;font-size:13px;margin-top:10px;display:none}
.sbox{background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.3);color:var(--accent)}
.ebox{background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);color:var(--red)}
.empty{color:var(--text3);font-size:13px;padding:20px;text-align:center}
.cb-badge{display:inline-flex;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:700}
.cb-closed{background:rgba(74,222,128,.15);color:var(--accent)}
.cb-open{background:rgba(248,113,113,.15);color:var(--red)}
.cb-half{background:rgba(251,191,36,.15);color:var(--yellow)}
.ret-display{background:var(--card2);border-radius:8px;padding:14px;font-size:13px}
.ret-display .row{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)}
.ret-display .row:last-child{border-bottom:none}
.ret-display .ival{color:var(--accent);font-weight:700}
@media(max-width:768px){.sidebar{width:60px}.sidebar-header h1,.sidebar-header p,.sidebar-footer,.nav-item span{display:none}.nav-item{justify-content:center;padding:12px}.main{margin-left:60px;padding:16px}.card-grid{grid-template-columns:1fr 1fr}.two-col{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="layout">
<nav class="sidebar">
  <div class="sidebar-header"><h1>🐙 ModelFungible</h1><p>Enterprise Admin</p></div>
  <div class="sidebar-nav">
    <div class="nav-item active" data-tab="dashboard" onclick="showTab('dashboard')"><span class="icon">📊</span><span>Dashboard</span></div>
    <div class="nav-item" data-tab="deployments" onclick="showTab('deployments')"><span class="icon">🚀</span><span>Deployments</span></div>
    <div class="nav-item" data-tab="strategies" onclick="showTab('strategies')"><span class="icon">⚙️</span><span>Strategies</span></div>
    <div class="nav-item" data-tab="execute" onclick="showTab('execute')"><span class="icon">▶</span><span>Execute</span></div>
    <div class="nav-item" data-tab="audit" onclick="showTab('audit')"><span class="icon">📋</span><span>Audit Logs</span></div>
    <div class="nav-item" data-tab="decisions" onclick="showTab('decisions')"><span class="icon">🧠</span><span>Decisions</span></div>
    <div class="nav-item" data-tab="prompts" onclick="showTab('prompts')"><span class="icon">📝</span><span>Prompts</span></div>
    <div class="nav-item" data-tab="compliance" onclick="showTab('compliance')"><span class="icon">🛡️</span><span>Compliance</span></div>
    <div class="nav-item" data-tab="guardrails" onclick="showTab('guardrails')"><span class="icon">🔍</span><span>Guardrails</span></div>
    <div class="nav-item" data-tab="distillation" onclick="showTab('distillation')"><span class="icon">🔬</span><span>Distillation</span></div>
    <div class="nav-item" data-tab="apikeys" onclick="showTab('apikeys')"><span class="icon">🔑</span><span>API Keys</span></div>
    <div class="nav-item" data-tab="budget" onclick="showTab('budget')"><span class="icon">💰</span><span>Budget</span></div>
    <div class="nav-item" data-tab="usage" onclick="showTab('usage')"><span class="icon">📈</span><span>Usage</span></div>
  </div>
  <div class="sidebar-footer">
    <div id="userInfo" style="display:none">
      <div id="userName" style="font-weight:600;color:var(--text2);font-size:11px;margin-bottom:2px"></div>
      <div id="userRole" style="font-size:10px;color:var(--text3);margin-bottom:6px;text-transform:uppercase"></div>
      <button class="btn btn-ghost btn-sm" onclick="doLogout()" style="width:100%;font-size:11px;padding:4px 8px">Sign Out</button>
    </div>
    <div id="verInfo">Loading...</div>
  </div>
</nav>
<main class="main">

<!-- DASHBOARD -->
<div class="tab active" id="tab-dashboard">
  <div class="topbar">
    <h2>System Dashboard</h2>
    <span class="badge badge-green" id="integrityBadge">● Checking...</span>
  </div>
  <div class="card-grid" style="margin-bottom:24px">
    <div class="stat-card"><div class="val" id="s-total">—</div><div class="label">Total Entries</div></div>
    <div class="stat-card"><div class="val" id="s-today">—</div><div class="label">Today</div></div>
    <div class="stat-card"><div class="val" id="s-models">—</div><div class="label">Models</div></div>
    <div class="stat-card"><div class="val" id="s-breakers">—</div><div class="label">Breakers</div></div>
  </div>
  <div class="card"><h3>Model Health</h3>
    <div class="model-grid" id="mHealth"></div>
    <div class="empty" id="noModels">No models registered. Go to Deployments.</div>
  </div>
  <div class="card"><h3>Circuit Breakers</h3><div id="cbTable"></div></div>
  <div class="card"><h3>Recent Activity</h3><div id="feed"></div></div>
</div>

<!-- DEPLOYMENTS -->
<div class="tab" id="tab-deployments">
  <div class="topbar"><h2>Model Deployments</h2><button class="btn btn-primary" onclick="showAddForm()">+ Add Model</button></div>
  <div class="card" id="addForm" style="display:none">
    <h3>Register Model</h3>
    <div class="sbox" id="addSuccess"></div>
    <div class="ebox" id="addErr"></div>
    <div class="form-row">
      <div class="form-group"><label>Name</label><input id="mName" placeholder="e.g. claude-primary"/></div>
      <div class="form-group"><label>Provider</label>
        <select id="mProv" onchange="onProviderChange()"><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option><option value="groq">Groq (free tier)</option><option value="minimax">MiniMax</option><option value="moonshot">Moonshot / Kimi</option><option value="glm">GLM / Zhipu AI</option><option value="owen">Owen</option><option value="ollama">Ollama (local)</option><option value="custom">Custom Provider...</option></select>
      </div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Model ID</label><input id="mModelId" placeholder="e.g. gpt-4o"/></div>
      <div class="form-group"><label>API Key</label><input id="mApiKey" type="password" placeholder="sk-..."/></div>
    </div>
    <div class="form-row">
      <div class="form-group" id="baseUrlField" style="display:none"><label>Base URL *</label><input id="mBaseUrl" placeholder="https://api.provider.com/v1"/></div>
      <div class="form-group"><label>p50 Latency (ms)</label><input id="mLat" type="number" value="500"/></div>
      <div class="form-group"><label>Capability</label>
        <select id="mCap"><option value="fast">Fast</option><option value="precise">Precise</option><option value="balanced">Balanced</option><option value="any">Any</option></select>
      </div>
    </div>
    <div style="display:flex;gap:8px;margin-top:4px">
      <button class="btn btn-primary" onclick="regModel()">Register</button>
      <button class="btn btn-ghost" onclick="hideAddForm()">Cancel</button>
    </div>
  </div>
  <div class="card"><h3>Registered Models</h3><div id="mTable"></div></div>
  <div class="card" style="border-left:3px solid #60a5fa">
    <h3>🧩 Custom Providers</h3>
    <p style="font-size:13px;color:var(--text2);margin-bottom:14px">Connect any LLM — local, intranet, or external. Name + base URL only.</p>
    <div id="provTable"></div>
    <button class="btn" style="background:#60a5fa;color:#fff;margin-top:12px" onclick="showAddProvForm()">+ Add Provider</button>
    <div id="provForm" style="display:none;margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
      <h4>New Custom Provider</h4>
      <div class="ebox" id="provErr"></div>
      <div class="sbox" id="provSuccess" style="display:none"></div>
      <div class="form-row" style="margin-top:10px">
        <div class="form-group"><label>Name</label><input id="pName" placeholder="my-ollama"/></div>
        <div class="form-group"><label>Base URL</label><input id="pBaseUrl" placeholder="http://localhost:11434/v1"/></div>
      </div>
      <div class="form-row">
        <div class="form-group"><label>API Key</label><input id="pApiKey" type="password" placeholder="empty for local models"/></div>
        <div class="form-group"><label>Default Model</label><input id="pModel" placeholder="llama-3.3-70b (optional)"/></div>
      </div>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn" style="background:#60a5fa;color:#fff" onclick="regProvider()">Add Provider</button>
        <button class="btn btn-ghost" onclick="hideAddProvForm()">Cancel</button>
      </div>
    </div>
  </div>
</div>

<!-- STRATEGIES -->
<div class="tab" id="tab-strategies">
  <div class="topbar"><h2>Strategy Library</h2></div>
  <div class="two-col">
    <div>
      <div class="card" style="padding:14px">
        <input id="strSearch" placeholder="Search strategies..." oninput="filterStrats()" style="margin-bottom:10px"/>
        <div class="strategy-list" id="strList"></div>
      </div>
    </div>
    <div>
      <div class="card"><h3 id="strTitle">Select a strategy</h3><div id="strDetail"></div></div>
      <div class="card"><h3>Validate Strategy JSON</h3>
        <textarea id="valJson" placeholder="{"strategy_id":"test","name":"Test","entry_trigger":"score > 80","sizing":{"NEUTRAL":{"amount":1000,"max_positions":3}}}"></textarea>
        <div style="margin-top:10px;display:flex;gap:8px">
          <button class="btn btn-primary" onclick="doValidate()">Validate</button>
          <button class="btn btn-ghost" onclick="loadIntoValidator()">Load Selected</button>
        </div>
        <div class="sbox" id="valOk"></div>
        <div class="ebox" id="valErr"></div>
      </div>
    </div>
  </div>
</div>
<div class="tab" id="tab-audit">
  <div class="topbar">
    <h2>Audit Logs</h2>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn btn-ghost btn-sm" onclick="dlJson()">Export JSON</button>
      <button class="btn btn-ghost btn-sm" onclick="dlCsv()">Export CSV</button>
      <button class="btn btn-ghost btn-sm" onclick="doVerify()">Verify Chain</button>
    </div>
  </div>
  <div id="verifyAlert"></div>
  <div class="card">
    <div class="filter-bar">
      <div class="form-group"><label>Start</label><input id="fStart" type="date"/></div>
      <div class="form-group"><label>End</label><input id="fEnd" type="date"/></div>
      <div class="form-group"><label>Actor</label><input id="fActor" placeholder="e.g. gpt-4o"/></div>
      <div class="form-group"><label>Action</label><input id="fAction" placeholder="e.g. model_execute"/></div>
      <div class="form-group"><label>Outcome</label><select id="fOutcome"><option value="">All</option><option value="success">Success</option><option value="failure">Failure</option><option value="error">Error</option></select></div>
      <button class="btn btn-primary" onclick="loadAudit(0)">Query</button>
    </div>
    <div id="aTable"></div>
    <div class="pagination">
      <button class="btn btn-ghost btn-sm" id="aPrev" onclick="aPrev()" disabled>Prev</button>
      <span class="info" id="aInfo"></span>
      <button class="btn btn-ghost btn-sm" id="aNext" onclick="aNext()" disabled>Next</button>
    </div>
  </div>
</div>
<div class="tab" id="tab-decisions">
  <div class="topbar"><h2>Decision Attribution</h2><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
    <input id="decSearch" placeholder="Search decisions..." style="padding:6px 10px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;width:200px"/>
    <select id="decMode" style="padding:6px 10px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px">
      <option value="">All modes</option><option value="fastest">Fastest</option><option value="cheapest">Cheapest</option><option value="balanced">Balanced</option><option value="capability">Capability</option>
    </select>
    <button class="btn btn-primary btn-sm" onclick="loadDecisions(0)">Search</button>
  </div></div>
  <div class="card"><h3>Model Selection History</h3>
    <div id="decStats" style="margin-bottom:12px;font-size:13px;color:var(--text2)"></div>
    <div id="decTable"></div>
    <div class="pagination">
      <button class="btn btn-ghost btn-sm" id="decPrev" onclick="decPrev()" disabled>Prev</button>
      <span class="info" id="decInfo"></span>
      <button class="btn btn-ghost btn-sm" id="decNext" onclick="decNext()" disabled>Next</button>
    </div>
  </div>
  <div class="card" id="decExplainCard" style="display:none"><h3>Decision Explanation</h3>
    <div id="decExplain" style="white-space:pre-wrap;font-size:13px;line-height:1.6"></div>
    <div style="margin-top:12px"><h4 style="font-size:12px;color:var(--text2);margin-bottom:8px">Candidate Scores</h4>
    <div id="decScores"></div></div>
  </div>
</div>

<div class="tab" id="tab-prompts">
  <div class="topbar"><h2>Prompt Marketplace</h2>
    <div style="display:flex;gap:8px">
      <select id="prDomain" style="padding:6px 10px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px">
        <option value="">All domains</option><option value="legal">Legal</option><option value="finance">Finance</option><option value="healthcare">Healthcare</option><option value="hr">HR</option><option value="coding">Coding</option><option value="general">General</option>
      </select>
      <input id="prSearch" placeholder="Search prompts..." style="padding:6px 10px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;width:180px"/>
      <button class="btn btn-primary btn-sm" onclick="loadPrompts(0)">Search</button>
      <button class="btn btn-ghost btn-sm" onclick="showNewPromptForm()">+ New Prompt</button>
    </div>
  </div>
  <div id="newPromptForm" class="card" style="display:none"><h3>New Prompt</h3>
    <div class="form-row"><div class="form-group"><label>Name</label><input id="prName" style="width:100%;padding:8px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px"/></div>
    <div class="form-group"><label>Domain</label><select id="prDomain2" style="width:100%;padding:8px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px"><option value="general">General</option><option value="legal">Legal</option><option value="finance">Finance</option><option value="healthcare">Healthcare</option><option value="coding">Coding</option><option value="hr">HR</option></select></div></div>
    <div class="form-group"><label>Description</label><input id="prDesc" placeholder="What does this prompt do?" style="width:100%;padding:8px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px"/></div>
    <div class="form-group"><label>System Prompt</label><textarea id="prSystem" placeholder="You are a..." style="width:100%;min-height:60px;padding:8px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px"></textarea></div>
    <div class="form-group"><label>Prompt Template (use {"{"}{"{variable_name}"}"})</label><textarea id="prText" placeholder="Review the following {{document_type}} for {{risk_type}} risks..." style="width:100%;min-height:100px;padding:8px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px"></textarea></div>
    <div class="form-group"><label>Tags (comma-separated)</label><input id="prTags" placeholder="legal,contract,review" style="width:100%;padding:8px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px"/></div>
    <button class="btn btn-primary" onclick="createPrompt()">Create Prompt</button>
  </div>
  <div id="prList"></div>
  <div class="pagination">
    <button class="btn btn-ghost btn-sm" id="prPrev" onclick="prPrev()" disabled>Prev</button>
    <span class="info" id="prInfo"></span>
    <button class="btn btn-ghost btn-sm" id="prNext" onclick="prNext()" disabled>Next</button>
  </div>
</div>

<div class="tab" id="tab-compliance">
  <div class="topbar"><h2>Compliance &amp; Settings</h2></div>
  <div class="card"><h3>Retention Policy</h3>
    <div class="form-row">
      <div class="form-group"><label>Regulation</label>
        <select id="retPolicy" onchange="updateRetDisplay()">
          <option value="gdpr">GDPR (EU) — 30 days</option>
          <option value="hipaa">HIPAA (US Healthcare) — 6 years</option>
          <option value="finra">FINRA (US Finance) — 6 years</option>
          <option value="sec">SEC (Investment Advisor) — 5 years</option>
          <option value="soc2">SOC 2 — 1 year</option>
          <option value="pci_dss">PCI-DSS — 1 year</option>
          <option value="default">Default — 90 days</option>
        </select>
      </div>
    </div>
    <div class="ret-display" id="retDisplay"></div>
  </div>
  <div class="card"><h3>PII Detection</h3>
    <div class="form-group" style="flex:2"><label>Sample Data (JSON)</label><textarea id="piiData" placeholder='{"email": "john@example.com", "ssn": "123-45-6789"}' style="min-height:80px"></textarea></div>
    <button class="btn btn-primary" onclick="testPii()" style="margin-top:10px">Detect PII</button>
    <div id="piiResults" style="margin-top:12px"></div>
  </div>
  <div class="card"><h3>License Status</h3><div id="licStatus">Loading...</div></div>
  <div class="card"><h3>System Info</h3><div id="sysInfo">Loading...</div></div>
</div>

<!-- Guardrails Tab -->
<div class="tab" id="tab-guardrails">
  <div class="topbar"><h2>Output Guardrails</h2></div>
  <div class="card">
    <h3>Test Guardrail Filter</h3>
    <textarea id="grTestOutput" placeholder="Paste model output to test..." style="width:100%;height:80px;margin-bottom:8px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px;font-family:monospace;font-size:12px"></textarea>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px">
      <input id="grBlockedTerms" placeholder="Blocked terms (comma-separated)" style="flex:1;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <input id="grMaxLen" type="number" placeholder="Max length" style="width:120px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
    </div>
    <button class="btn btn-primary" onclick="testGuardrail()">Test Filter</button>
    <div id="grResult" style="margin-top:12px;font-family:monospace;font-size:12px;white-space:pre-wrap;background:var(--input-bg);border:1px solid var(--border);border-radius:6px;padding:10px;min-height:60px;color:var(--text)"></div>
  </div>
  <div class="card">
    <h3>Guardrail Config</h3>
    <p style="color:var(--text3);font-size:13px">Add <code style="background:var(--card);padding:2px 6px;border-radius:4px">output_filter</code> to your execute request:</p>
    <pre style="background:var(--input-bg);border:1px solid var(--border);border-radius:6px;padding:12px;font-size:12px;overflow-x:auto;color:var(--text)">{
  "prompt": "...",
  "output_filter": {
    "blocked_terms": ["confidential", "secret", "ssn"],
    "max_length": 2000,
    "case_sensitive": false,
    "mask_replacement": "[FILTERED]"
  }
}</pre>
  </div>
</div>

<!-- Distillation Detection Tab -->
<div class="tab" id="tab-distillation">
  <div class="topbar"><h2>Distillation Detection</h2></div>
  <div class="card">
    <h3>Overview</h3>
    <div id="distillationStats" style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:8px">
      <div class="stat-box"><div class="stat-num" id="dist-monitored">-</div><div class="stat-label">Monitored Users</div></div>
      <div class="stat-box"><div class="stat-num" id="dist-high-risk">-</div><div class="stat-label">High Risk</div></div>
      <div class="stat-box"><div class="stat-num" id="dist-flagged">-</div><div class="stat-label">Flagged Requests</div></div>
    </div>
    <button class="btn btn-primary" onclick="loadDistillationStats()">Refresh</button>
  </div>
  <div class="card">
    <h3>Manual Check</h3>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
      <input id="dist-user" placeholder="User ID" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <input id="dist-tokens" type="number" placeholder="Tokens (optional)" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
    </div>
    <textarea id="dist-prompt" placeholder="Prompt to check..." style="width:100%;height:70px;margin-bottom:8px;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px;font-family:monospace;font-size:12px"></textarea>
    <div style="display:flex;gap:8px;margin-bottom:8px">
      <label style="display:flex;align-items:center;gap:4px;color:var(--text2);font-size:13px"><input id="dist-paid" type="checkbox"> Paid tier</label>
      <label style="display:flex;align-items:center;gap:4px;color:var(--text2);font-size:13px"><input id="dist-auth" type="checkbox" checked> Authenticated</label>
    </div>
    <button class="btn btn-primary" onclick="checkDistillation()">Check Prompt</button>
    <div id="dist-check-result" style="margin-top:12px;font-family:monospace;font-size:12px;white-space:pre-wrap;background:var(--input-bg);border:1px solid var(--border);border-radius:6px;padding:10px;min-height:60px;color:var(--text)"></div>
  </div>
  <div class="card">
    <h3>High-Risk Users</h3>
    <div id="dist-high-risk-list" style="color:var(--text2);font-size:13px">Loading...</div>
    <button class="btn btn-secondary" onclick="loadHighRiskUsers()" style="margin-top:8px">Refresh</button>
  </div>
  <div class="card">
    <h3>User Lookup</h3>
    <div style="display:flex;gap:8px;margin-bottom:8px">
      <input id="dist-lookup-user" placeholder="User ID" style="flex:1;background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <button class="btn btn-primary" onclick="lookupDistillationUser()">Lookup</button>
      <button class="btn btn-danger" onclick="resetDistillationUser()">Reset (Admin)</button>
    </div>
    <div id="dist-user-result" style="font-family:monospace;font-size:12px;white-space:pre-wrap;background:var(--input-bg);border:1px solid var(--border);border-radius:6px;padding:10px;min-height:80px;color:var(--text)"></div>
  </div>
</div>

<!-- API Keys Tab -->
<div class="tab" id="tab-apikeys">
  <div class="topbar"><h2>Per-Team API Keys</h2></div>
  <div class="card">
    <h3>Create Team</h3>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 120px;gap:8px;margin-bottom:8px">
      <input id="tName" placeholder="Team name" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <input id="tDaily" type="number" step="0.01" placeholder="Daily $ limit" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <input id="tMonthly" type="number" step="0.01" placeholder="Monthly $ limit" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <input id="tRate" type="number" placeholder="RPM (0=unlimited)" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
    </div>
    <button class="btn btn-primary" onclick="createTeam()">Create Team</button>
    <div id="tMsg" style="margin-top:8px;font-size:13px"></div>
  </div>
  <div class="card">
    <h3>Teams</h3>
    <div id="teamsList">Loading...</div>
  </div>
  <div class="card">
    <h3>Create API Key</h3>
    <div style="display:grid;grid-template-columns:200px 1fr 200px;gap:8px;margin-bottom:8px">
      <select id="akTeam" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px"><option value="">Select team...</option></select>
      <input id="akName" placeholder="Key name (e.g. prod-key)" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <input id="akSecret" placeholder="HMAC secret (optional)" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
    </div>
    <button class="btn btn-primary" onclick="createApiKey()">Generate Key</button>
    <div id="akResult" style="margin-top:10px"></div>
  </div>
  <div class="card">
    <h3>API Keys</h3>
    <div id="keysList">Loading...</div>
  </div>
</div>

<!-- Budget Alerts Tab -->
<div class="tab" id="tab-budget">
  <div class="topbar"><h2>Budget Alerts</h2></div>
  <div class="card">
    <h3>Create Alert</h3>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 200px;gap:8px;margin-bottom:8px">
      <select id="baTeam" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px"><option value="">Select team...</option></select>
      <input id="baUrl" placeholder="Webhook URL" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <input id="baThreshold" type="number" step="1" placeholder="Threshold % (e.g. 80)" value="80" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <select id="baType" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px"><option value="daily">Daily</option><option value="monthly">Monthly</option></select>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 200px;gap:8px;margin-bottom:8px">
      <input id="baDailyLimit" type="number" step="0.01" placeholder="Daily limit ($)" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <input id="baMonthlyLimit" type="number" step="0.01" placeholder="Monthly limit ($)" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
      <input id="baSecret" placeholder="HMAC secret (optional)" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px">
    </div>
    <button class="btn btn-primary" onclick="createBudgetAlert()">Create Alert</button>
    <div id="baMsg" style="margin-top:8px;font-size:13px"></div>
  </div>
  <div class="card">
    <h3>Active Alerts</h3>
    <div id="alertsList">Loading...</div>
  </div>
  <div class="card">
    <h3>Alert History</h3>
    <div id="alertEvents">Loading...</div>
  </div>
</div>

<!-- Usage Tab -->
<div class="tab" id="tab-usage">
  <div class="topbar"><h2>Usage &amp; Cost Dashboard</h2></div>
  <div class="card" style="margin-bottom:16px">
    <h3>Cost Summary</h3>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;text-align:center">
      <div><div style="font-size:24px;font-weight:700;color:var(--accent)" id="uToday">—</div><div style="font-size:11px;color:var(--text3);text-transform:uppercase">Today ($)</div></div>
      <div><div style="font-size:24px;font-weight:700;color:var(--accent)" id="uMonth">—</div><div style="font-size:11px;color:var(--text3);text-transform:uppercase">This Month ($)</div></div>
      <div><div style="font-size:24px;font-weight:700;color:var(--blue)" id="uTodayPct">—</div><div style="font-size:11px;color:var(--text3);text-transform:uppercase">Daily % Used</div></div>
      <div><div style="font-size:24px;font-weight:700;color:var(--blue)" id="uMonthPct">—</div><div style="font-size:11px;color:var(--text3);text-transform:uppercase">Monthly % Used</div></div>
    </div>
  </div>
  <div class="card" style="margin-bottom:16px">
    <h3>By Team</h3>
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">
      <select id="uTeamSel" onchange="loadTeamUsage()" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px"><option value="">All teams</option></select>
      <select id="uPeriod" onchange="loadTeamUsage()" style="background:var(--input-bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px"><option value="today">Today</option><option value="month">This Month</option></select>
    </div>
    <div id="usageTable">Loading...</div>
  </div>
  <div class="card">
    <h3>By Model</h3>
    <div id="modelCostTable">Loading...</div>
  </div>
</div>

</main>
</div>
</body>
</html>
<script>
var API="/api";
var SESSION_TOKEN=localStorage.getItem("mf_token")||null;
var CURRENT_USER=null;

function getToken(){return SESSION_TOKEN||localStorage.getItem("mf_token");}
function setToken(t){SESSION_TOKEN=t;localStorage.setItem("mf_token",t);}
function clearToken(){SESSION_TOKEN=null;localStorage.removeItem("mf_token");}

function hdrs(){return {"X-Auth-Token":getToken()||""};}

function esc(s){if(s==null)return"";return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}

async function apiGet(path){
  const r=await fetch(API+path,{headers:hdrs()});
  if(r.status===401){showLogin();throw new Error("unauthorized");}
  if(!r.ok)throw new Error(r.text());
  return r.json();
}
async function apiPost(path,body){
  const r=await fetch(API+path,{method:"POST",headers:{"Content-Type":"application/json",...hdrs()},body:JSON.stringify(body)});
  if(r.status===401){showLogin();throw new Error("unauthorized");}
  if(!r.ok)throw new Error(r.text());
  return r.json();
}

function showLogin(){
  document.getElementById("login-overlay").style.display="flex";
  document.getElementById("app-content").style.display="none";
}
function showApp(){
  document.getElementById("login-overlay").style.display="none";
  document.getElementById("app-content").style.display="block";
}

async function doLogin(e){
  e.preventDefault();
  var btn=document.getElementById("loginBtn");
  btn.disabled=true;btn.textContent="Signing in...";
  document.getElementById("loginError").style.display="none";
  try{
    var data=await apiPost("/auth/login",{user_id:document.getElementById("lUser").value,password:document.getElementById("lPass").value});
    setToken(data.session_id);
    CURRENT_USER={user_id:data.user_id,name:data.name,role:data.role};
    showApp();
    document.getElementById("userName").textContent=data.name;
    document.getElementById("userRole").textContent=data.role;
    document.getElementById("userInfo").style.display="block";
    document.getElementById("verInfo").style.display="none";
    loadDashboard();
  }catch(err){
    var errEl=document.getElementById("loginError");
    errEl.textContent="Login failed — check user_id and password";
    errEl.style.display="block";
  }finally{btn.disabled=false;btn.textContent="Sign In";}
}

async function doLogout(){
  try{await apiPost("/auth/logout",{});}catch(e){}
  clearToken();CURRENT_USER=null;
  showLogin();
}

async function checkAuth(){
  var t=getToken();
  if(!t){showLogin();return false;}
  try{
    var me=await apiGet("/auth/me");
    CURRENT_USER={user_id:me.user_id,name:me.name,role:me.role};
    showApp();
    document.getElementById("userName").textContent=me.name;
    document.getElementById("userRole").textContent=me.role;
    document.getElementById("userInfo").style.display="block";
    document.getElementById("verInfo").style.display="none";
    return true;
  }catch(e){clearToken();showLogin();return false;}
}

var allStrats=[];
var selStrat=null;
var aOffset=0;
var aLimit=50;
var aTotal=0;
function get(p){return fetch(API+p,{headers:hdrs()}).then(function(r){if(!r.ok)throw Error(r.text());return r.json();});}
function post_(p,b){return fetch(API+p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}).then(function(r){if(!r.ok)throw Error(r.text());return r.json();});}
function del__(p){return fetch(API+p,{method:"DELETE"}).then(function(r){if(!r.ok)throw Error(r.text());return r.json();});}
function esc(s){if(s==null)return"";return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}
function fmtTs(ts){if(!ts)return"";try{return new Date(ts).toLocaleString();}catch(e){return ts;}}
function hlJson(obj){var s=JSON.stringify(obj,null,2);return s.replace(/("([^"]*)"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?/g,function(m){if(/^"/.test(m)){return(/:$/.test(m)?'<span class="json-key">'+m+'</span>':'<span class="json-str">'+m+'</span>');}if(/true|false/.test(m))return'<span class="json-bool">'+m+'</span>';if(/null/.test(m))return'<span class="json-null">'+m+'</span>';return'<span class="json-num">'+m+'</span>';});}
function showTab(id){document.querySelectorAll(".tab").forEach(function(t){t.classList.remove("active");});document.querySelectorAll(".nav-item").forEach(function(n){n.classList.remove("active");});document.getElementById("tab-"+id).classList.add("active");document.querySelectorAll(".nav-item").forEach(function(n){if(n.dataset.tab===id)n.classList.add("active");});if(id==="dashboard")loadDashboard();else if(id==="strategies")loadStrats();
  else if(id==="decisions"){loadDecisions(0);loadDecStats();}
  else if(id==="prompts"){loadPrompts(0);}
  else if(id==="execute")initExecute();else if(id==="audit")loadAudit(0);else if(id==="compliance")loadCompliance();else if(id==="deployments")loadDeployments();else if(id==="guardrails"){}else if(id==="distillation"){loadDistillationStats();}else if(id==="apikeys"){loadTeams();loadApiKeys();}else if(id==="budget"){loadTeamsForBudget();loadAlerts();loadAlertEvents();}else if(id==="usage"){loadUsage();loadTeamsForUsage();}}
async function loadDashboard(){try{var s=await apiGet("/state");var b=await apiGet("/circuit-breakers");document.getElementById("s-total").textContent=s.total_entries||0;document.getElementById("s-today").textContent=s.entries_today||0;document.getElementById("s-models").textContent=s.models?s.models.length:0;document.getElementById("s-breakers").textContent=b.length;try{var v=await apiGet("/audit/verify");var ib=document.getElementById("integrityBadge");ib.className="badge "+(v.valid?"badge-green":"badge-red");ib.textContent=v.valid?"VERIFIED":"TAMPERED";}catch(e){}var mg=document.getElementById("mHealth");document.getElementById("noModels").style.display=(s.models&&s.models.length)?"none":"block";mg.innerHTML="";if(s.models){s.models.forEach(function(m){var n=esc(m.name).replace(/'/g,"\\'");mg.innerHTML+='<div class="model-card"><div class="name">'+esc(m.name)+'</div><div class="meta">'+esc(m.provider)+' / '+esc(m.model_id)+'</div><div class="meta">p50: '+(m.latency_ms_p50||"?")+'ms</div><div class="meta">'+esc(m.capability||"any")+'</div><div class="actions"><button class="btn btn-ghost btn-sm" onclick="testModel(\''+n+'\')">Test</button> <button class="btn btn-danger btn-sm" onclick="deleteModel(\''+n+'\')">Delete</button></div></div>';});}var ct=document.getElementById("cbTable");if(!b.length)ct.innerHTML='<div class="empty">No circuit breakers active.</div>';else{ct.innerHTML='<table><thead><tr><th>Name</th><th>State</th><th>Failures</th><th>Cooldown</th><th></th></tr></thead><tbody>'+b.map(function(x){var n=esc(x.name).replace(/'/g,"\\'");return'<tr><td class="mono">'+esc(x.name)+'</td><td><span class="cb-badge cb-'+(x.state||"CLOSED").toLowerCase().replace("-","")+'">'+esc(x.state||"CLOSED")+'</span></td><td>'+(x.failure_count||0)+'</td><td>'+(x.cooldown_seconds||60)+'s</td><td><button class="btn btn-ghost btn-sm" onclick="resetCb(\''+n+'\')">Reset</button></td></tr>';}).join("")+'</tbody></table>';}try{var logs=await get("/audit/logs?limit=10");var feed=document.getElementById("feed");feed.innerHTML=logs.length?logs.map(function(e){var cls=e.outcome==="success"?"badge-green":e.outcome==="failure"?"badge-red":"badge-yellow";return'<div class="feed-item"><span class="feed-time">'+fmtTs(e.timestamp)+'</span><span class="feed-action">'+esc(e.action)+'</span><span class="feed-actor">'+esc(e.actor||"")+'</span><span class="badge '+cls+'" style="font-size:11px">'+esc(e.outcome||"")+'</span></div>';}).join(""):'<div class="empty">No audit entries yet.</div>';}catch(e){document.getElementById("feed").innerHTML='<div class="empty">Could not load feed.</div>';}}catch(e){console.error(e);}apiGet("/api/version").then(function(v){document.getElementById("verInfo").textContent="v"+(v.modelfungible||"?")+" | Python "+(v.python||"?");}).catch(function(){document.getElementById("verInfo").textContent="ModelFungible Admin";});}
function onProviderChange(){document.getElementById("baseUrlField").style.display=document.getElementById("mProv").value==="custom"?"block":"none";}
function showAddProvForm(){document.getElementById("provForm").style.display="block";document.getElementById("provErr").style.display="none";document.getElementById("provSuccess").style.display="none";}
function hideAddProvForm(){document.getElementById("provForm").style.display="none";}
async function loadProviders(){try{var r=await apiGet("/providers");var t=document.getElementById("provTable");if(!r.providers||!r.providers.length){t.innerHTML='<div style="font-size:13px;color:var(--text3)">No custom providers yet. Add one below.</div>';return;}t.innerHTML='<table style="width:100%"><thead><tr style="text-align:left;color:var(--text2);font-size:11px;text-transform:uppercase"><th>Name</th><th>Base URL</th><th>Default Model</th><th>Sys</th><th></th></tr></thead><tbody>'+r.providers.map(function(p){var n=esc(p.name).replace(/'/g,"\\'");return'<tr style="border-top:1px solid var(--border)"><td class="mono" style="padding:6px 0">'+esc(p.name)+'</td><td style="padding:6px 0;color:var(--text2);max-width:140px;overflow:hidden;text-overflow:ellipsis">'+esc(p.base_url)+'</td><td style="padding:6px 0;color:var(--text2)">'+(p.default_model||"—")+'</td><td style="padding:6px 0">'+(p.supports_system_prompt?'<span style="color:var(--accent)">✓</span>':'<span style="color:var(--text3)">✗</span>')+'</td><td style="padding:6px 0;text-align:right"><button class="btn btn-sm" style="background:#60a5fa;color:#fff;margin-right:4px" onclick="testProvider(\''+n+'\')">Test</button><button class="btn btn-danger btn-sm" onclick="deleteProvider(\''+n+'\')">✕</button></td></tr>';}).join("")+'</tbody></table>';}catch(e){document.getElementById("provTable").innerHTML='<div class="ebox" style="font-size:13px">'+esc(e.message)+'</div>';}}
async function regProvider(){var name=document.getElementById("pName").value.trim();var baseUrl=document.getElementById("pBaseUrl").value.trim();if(!name||!baseUrl){document.getElementById("provErr").textContent="Name and Base URL required.";document.getElementById("provErr").style.display="block";return;}try{await apiPost("/providers",{name:name,base_url:baseUrl,api_key:document.getElementById("pApiKey").value,default_model:document.getElementById("pModel").value,supports_system_prompt:true});document.getElementById("provSuccess").textContent="Provider \""+name+"\" added successfully.";document.getElementById("provSuccess").style.display="block";document.getElementById("provErr").style.display="none";setTimeout(function(){hideAddProvForm();loadProviders();},1200);}catch(e){document.getElementById("provErr").textContent="Error: "+e.message;document.getElementById("provErr").style.display="block";}}
async function deleteProvider(name){if(!confirm("Delete provider \""+name+"\"?"))return;try{await apiDelete("/providers/"+name);loadProviders();}catch(e){alert("Error: "+e.message);}}
async function testProvider(name){try{var r=await apiPost("/providers/"+name+"/test",{});alert(r.success?"✓ Connected ("+r.status_code+")":"✗ Failed: "+r.message);}catch(e){alert("Error: "+e.message);}}
async function loadDeployments(){try{Promise.all([loadProviders()]);var s=await apiGet("/state");var t=document.getElementById("mTable");if(!s.models||!s.models.length){t.innerHTML='<div class="empty">No models. Click + Add Model.</div>';return;}t.innerHTML='<table><thead><tr><th>Name</th><th>Provider</th><th>Model ID</th><th>p50</th><th>Capability</th><th></th></tr></thead><tbody>'+s.models.map(function(m){var n=esc(m.name).replace(/'/g,"\\'");return'<tr><td class="mono">'+esc(m.name)+'</td><td>'+esc(m.provider)+'</td><td class="mono">'+esc(m.model_id)+'</td><td>'+(m.latency_ms_p50||"?")+'ms</td><td>'+esc(m.capability||"any")+'</td><td><button class="btn btn-danger btn-sm" onclick="deleteModel(\''+n+'\')">Delete</button></td></tr>';}).join("")+'</tbody></table>';}catch(e){document.getElementById("mTable").innerHTML='<div class="empty">'+esc(e.message)+'</div>';}}
function showAddForm(){document.getElementById("addForm").style.display="block";document.getElementById("addSuccess").style.display="none";document.getElementById("addErr").style.display="none";}
function hideAddForm(){document.getElementById("addForm").style.display="none";}
async function regModel(){var name=document.getElementById("mName").value.trim();var modelId=document.getElementById("mModelId").value.trim();if(!name||!modelId){var e=document.getElementById("addErr");e.textContent="Name and Model ID are required.";e.style.display="block";return;}try{var _prov=document.getElementById("mProv").value;if(_prov==="custom"){var _bu=document.getElementById("mBaseUrl").value.trim();if(!_bu){document.getElementById("addErr").textContent="Base URL required for custom.";document.getElementById("addErr").style.display="block";return;}_prov="custom:"+_bu;}await apiPost("/models/register",{name:name,provider:_prov,model_id:modelId,api_key:document.getElementById("mApiKey").value,latency_ms_p50:parseInt(document.getElementById("mLat").value)||500,capability:document.getElementById("mCap").value});var s=document.getElementById("addSuccess");s.textContent="Model registered successfully.";s.style.display="block";document.getElementById("addErr").style.display="none";setTimeout(function(){hideAddForm();loadDeployments();loadDashboard();},1000);}catch(e){var err=document.getElementById("addErr");err.textContent="Error: "+e.message;err.style.display="block";}}
async function deleteModel(name){if(!confirm("Delete model \""+name+"\"?"))return;try{await fetch(API+"/models/"+encodeURIComponent(name));loadDeployments();loadDashboard();}catch(e){alert("Error: "+e.message);}}
async function testModel(name){try{await apiPost("/models/"+encodeURIComponent(name)+"/test",{});alert("Test passed for "+name);}catch(e){alert("Test failed: "+e.message);}}
async function resetCb(name){try{await apiPost("/circuit-breakers/"+encodeURIComponent(name)+"/reset",{});loadDashboard();}catch(e){alert("Error: "+e.message);}}
async function loadStrats(){try{allStrats=await apiGet("/strategies");renderStratList("");}catch(e){document.getElementById("strList").innerHTML='<div class="empty">'+esc(e.message)+'</div>';}}
function filterStrats(){renderStratList(document.getElementById("strSearch").value);}
function renderStratList(q){var filtered=allStrats.filter(function(s){return s.toLowerCase().indexOf(q.toLowerCase())>=0;});document.getElementById("strList").innerHTML=filtered.map(function(s){var n=esc(s).replace(/'/g,"\\'");return'<div class="strategy-item" id="si-'+esc(s)+'" onclick="selectStrat(\''+n+'\')">'+esc(s)+'</div>';}).join("")||'<div class="empty">No strategies.</div>';if(selStrat){var el=document.getElementById("si-"+selStrat);if(el)el.classList.add("active");}}
async function selectStrat(id){selStrat=id;document.querySelectorAll(".strategy-item").forEach(function(el){el.classList.remove("active");});var el=document.getElementById("si-"+id);if(el)el.classList.add("active");document.getElementById("strTitle").textContent=id;try{var strat=await apiGet("/strategies/"+encodeURIComponent(id));document.getElementById("strDetail").innerHTML='<div class="json-viewer"><pre>'+hlJson(strat)+'</pre></div>';}catch(e){document.getElementById("strDetail").innerHTML='<div class="empty">'+esc(e.message)+'</div>';}}
function loadIntoValidator(){if(!selStrat)return;var detail=document.getElementById("strDetail").innerHTML;var pre=document.createElement("div");pre.innerHTML=detail;var text=pre.querySelector("pre");if(text){try{var obj=JSON.parse(text.textContent);document.getElementById("valJson").value=JSON.stringify(obj,null,2);}catch(e){}}}
async function doValidate(){var json=document.getElementById("valJson").value.trim();if(!json)return;try{var obj=JSON.parse(json);var result=await apiPost("/strategies/validate",obj);var ok=document.getElementById("valOk");var err=document.getElementById("valErr");if(result.valid){ok.textContent="Strategy is valid.";ok.style.display="block";err.style.display="none";}else{err.innerHTML="Validation errors:<br/>"+(result.errors||[]).map(function(e){return"&bull; "+esc(e);}).join("<br/>");err.style.display="block";ok.style.display="none";}}catch(e){var err=document.getElementById("valErr");err.textContent="Parse error: "+e.message;err.style.display="block";}}
async function loadAudit(offset){aOffset=offset;var params="limit="+aLimit+"&offset="+aOffset;var start=document.getElementById("fStart").value;var end=document.getElementById("fEnd").value;var actor=document.getElementById("fActor").value;var action=document.getElementById("fAction").value;var outcome=document.getElementById("fOutcome").value;if(start)params+="&start_date="+start;if(end)params+="&end_date="+end;if(actor)params+="&actor="+encodeURIComponent(actor);if(action)params+="&action="+encodeURIComponent(action);if(outcome)params+="&outcome="+encodeURIComponent(outcome);try{var data=await apiGet("/audit/logs?"+params);aTotal=data.length;var tbl=document.getElementById("aTable");if(!data.length){tbl.innerHTML='<div class="empty">No entries match your query.</div>';}else{tbl.innerHTML='<table><thead><tr><th>Time</th><th>Action</th><th>Actor</th><th>Model</th><th>Outcome</th><th>Org</th></tr></thead><tbody>'+data.map(function(e){return'<tr><td class="mono" style="white-space:nowrap">'+fmtTs(e.timestamp)+'</td><td>'+esc(e.action)+'</td><td class="mono">'+esc(e.actor||"")+'</td><td class="mono">'+esc(e.model_id||"")+'</td><td><span class="badge '+(e.outcome==="success"?"badge-green":e.outcome==="failure"?"badge-red":"badge-yellow")+'">'+esc(e.outcome||"")+'</span></td><td>'+esc(e.org_id||"")+'</td></tr>';}).join("")+'</tbody></table>';}document.getElementById("aInfo").textContent=(aOffset+1)+"-"+(aOffset+data.length)+" entries";document.getElementById("aPrev").disabled=aOffset===0;document.getElementById("aNext").disabled=data.length<aLimit;}catch(e){document.getElementById("aTable").innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>';}}
function aPrev(){if(aOffset>0){aOffset=Math.max(0,aOffset-aLimit);loadAudit(aOffset);}}
function aNext(){aOffset+=aLimit;loadAudit(aOffset);}
async function doVerify(){try{var v=await apiGet("/audit/verify");var alert=document.getElementById("verifyAlert");if(v.valid){alert.innerHTML='<div class="alert-success">Hash chain is intact &mdash; VERIFIED</div>';}else{alert.innerHTML='<div class="alert-error">Hash chain is BROKEN &mdash; tampering detected!</div>';}}catch(e){document.getElementById("verifyAlert").innerHTML='<div class="alert-error">Verify failed: '+esc(e.message)+'</div>';}}
function dlJson(){var start=document.getElementById("fStart").value;var end=document.getElementById("fEnd").value;var actor=document.getElementById("fActor").value;var action=document.getElementById("fAction").value;var outcome=document.getElementById("fOutcome").value;var params="limit=10000";if(start)params+="&start_date="+start;if(end)params+="&end_date="+end;if(actor)params+="&actor="+encodeURIComponent(actor);if(action)params+="&action="+encodeURIComponent(action);if(outcome)params+="&outcome="+encodeURIComponent(outcome);window.open(API+"/audit/export/json?"+params,"_blank");}
function dlCsv(){var start=document.getElementById("fStart").value;var end=document.getElementById("fEnd").value;var actor=document.getElementById("fActor").value;var action=document.getElementById("fAction").value;var outcome=document.getElementById("fOutcome").value;var params="limit=10000";if(start)params+="&start_date="+start;if(end)params+="&end_date="+end;if(actor)params+="&actor="+encodeURIComponent(actor);if(action)params+="&action="+encodeURIComponent(action);if(outcome)params+="&outcome="+encodeURIComponent(outcome);window.open(API+"/audit/export/csv?"+params,"_blank");}
async function loadCompliance(){updateRetDisplay();try{var lic=await apiGet("/compliance/license");var ls=document.getElementById("licStatus");if(lic.licensed){ls.innerHTML='<div class-badge-green";ls.innerHTML="<div><strong>Licensed to:</strong> "+esc(lic.customer_id||"?")+"</div><div><strong>Plan:</strong> "+esc(lic.plan||"?")+"</div><div><strong>Seats:</strong> "+(lic.seats||"?")+"</div><div><strong>Expiry:</strong> "+esc(lic.expiry||"?")+"</div>";}else{ls.innerHTML='<div class="alert-error">No license installed or invalid.</div>';}}catch(e){document.getElementById("licStatus").innerHTML='<div class="alert-error">Error loading license: '+esc(e.message)+'</div>';}try{var v=await apiGet("/api/version");document.getElementById("sysInfo").innerHTML='<div class="ret-display"><div class=row><span>ModelFungible</span><span class=ival>'+esc(v.modelfungible||"?")+'</span></div><div class=row><span>Python</span><span class=ival>'+esc(v.python||"?")+'</span></div></div>';}catch(e){document.getElementById("sysInfo").innerHTML='<div class="empty">Could not load system info.</div>';}}
var RET_POLICIES={"gdpr":{"days":30,"desc":"EU GDPR"},"hipaa":{"days":2190,"desc":"HIPAA (6yr)"},"finra":{"days":2190,"desc":"FINRA (6yr)"},"sec":{"days":1825,"desc":"SEC (5yr)"},"soc2":{"days":365,"desc":"SOC 2 (1yr)"},"pci_dss":{"days":365,"desc":"PCI-DSS (1yr)"},"default":{"days":90,"desc":"Default (90d)"}};
function updateRetDisplay(){var sel=document.getElementById("retPolicy").value;var pol=RET_POLICIES[sel]||RET_POLICIES["default"];document.getElementById("retDisplay").innerHTML='<div class=row><span>Regulation</span><span class=ival>'+esc(pol.desc)+'</span></div><div class=row><span>Retention</span><span class=ival>'+pol.days+" days ("+Math.round(pol.days/365*10)/10+" years)</span></div>";}
async function testPii(){var dataStr=document.getElementById("piiData").value.trim();if(!dataStr){document.getElementById("piiResults").innerHTML='<div class="alert-error">Please enter JSON data.</div>';return;}try{var data=JSON.parse(dataStr);var r=await get("/compliance/pii/scan?q="+encodeURIComponent(dataStr));var results=document.getElementById("piiResults");if(!r.flags||!r.flags.length){results.innerHTML='<div class="alert-success">No PII detected.</div>';}else{results.innerHTML='<div><strong>Detected:</strong> "+r.flags.map(function(f){return"<span class=cb-badge cb-closed style=\'background:rgba(96,165,250,0.15);color:var(--blue)\'>"+esc(f)+"</span>";}).join(" ")+"</div>";}}catch(e){document.getElementById("piiResults").innerHTML='<div class="alert-error">Error: '+esc(e.message)+'</div>';}}
async function doExecute(){
  var prompt=document.getElementById("exPrompt").value.trim();
  if(!prompt){document.getElementById("exOutput").textContent="Please enter a prompt.";return;}
  var btn=document.getElementById("exBtn");
  btn.disabled=true;btn.textContent="Running...";
  document.getElementById("exOutput").innerHTML='<span class="exec-loading">Waiting for model...</span>';
  try{
    var body={prompt:prompt,system:document.getElementById("exSystem").value.trim()||"You are a helpful assistant.",mode:document.getElementById("exMode").value,temperature:parseFloat(document.getElementById("exTemp").value)||0.7,max_tokens:parseInt(document.getElementById("exTokens").value)||1024};
    var model=document.getElementById("exModel").value.trim();
    if(model)body.model=model;
    var maxCost=parseFloat(document.getElementById("exMaxCost").value);
    if(maxCost>0)body.max_cost_per_call=maxCost;
    if(document.getElementById("exMode").value==="capability")body.capability=document.getElementById("exCap").value;
    var r=await apiPost("/execute",body);
    document.getElementById("exOutput").textContent=r.output||"(empty response)";
    document.getElementById("exCost").textContent="$"+r.cost.toFixed(4);
    document.getElementById("exLat").textContent=r.latency_ms+"ms";
    document.getElementById("exModelUsed").textContent=(r.model_name||r.model_id||"?").substring(0,16);
    var pii=document.getElementById("exPiiBadge");
    if(r.piidetected){pii.style.display="inline-flex";}else{pii.style.display="none";}
    var out=document.getElementById("exOutput");
    out.classList.remove("error");
  }catch(e){
    document.getElementById("exOutput").innerHTML="Error: "+esc(e.message||e);
    document.getElementById("exOutput").classList.add("error");
    document.getElementById("exCost").textContent="—";
    document.getElementById("exLat").textContent="—";
    document.getElementById("exModelUsed").textContent="—";
  }finally{btn.disabled=false;btn.textContent="Run Execute";}
}
function initExecute(){
  var mode=document.getElementById("exMode").value;
  document.getElementById("exCapabilityRow").style.display=(mode==="capability")?"block":"none";
}
document.getElementById("exMode").addEventListener("change",function(){
  document.getElementById("exCapabilityRow").style.display=(this.value==="capability")?"block":"none";
});
window.onload=function(){checkAuth();};

// ── Guardrails ───────────────────────────────────────────────────────────────
async function testGuardrail(){
  var output=document.getElementById("grTestOutput").value;
  var terms=document.getElementById("grBlockedTerms").value.split(",").map(function(t){return t.trim();}).filter(function(t){return t;});
  var maxLen=parseInt(document.getElementById("grMaxLen").value)||null;
  if(!output){document.getElementById("grResult").textContent="Paste output to test.";return;}
  try{
    var r=await post_("/cache/guardrail-test",{output:output,blocked_terms:terms,max_length:maxLen});
    var passed=r.passed?"✅ Passed":"❌ Filtered";
    var cls=r.passed?"color:#4ade80":"color:#f87171";
    document.getElementById("grResult").innerHTML='<span style="'+cls+';font-weight:600">'+passed+"</span>\n\n"+esc(r.filtered_output)+(r.terms_blocked&&r.terms_blocked.length?'\n\n<span style="color:#f97316">Blocked: '+r.terms_blocked.join(", ")+"</span>":"")+(r.was_truncated?'\n<span style="color:#f97316">Truncated to '+maxLen+' chars</span>':"");
  }catch(e){document.getElementById("grResult").textContent="Error: "+e.message;}
}

// ── API Keys ─────────────────────────────────────────────────────────────────
async function loadTeams(){
  try{var teams=await get("/api-keys/teams");var sel=document.getElementById("akTeam");sel.innerHTML='<option value="">Select team...</option>'+teams.map(function(t){return'<option value="'+esc(t.team_id)+'">'+esc(t.name)+'</option>';}).join("");
  document.getElementById("teamsList").innerHTML=!teams.length?'<div class="empty">No teams yet.</div>':'<table><thead><tr><th>Name</th><th>Daily $</th><th>Monthly $</th><th>RPM</th><th>Status</th></tr></thead><tbody>'+teams.map(function(t){var active=t.is_active?'<span class="cb-badge cb-closed">Active</span>':'<span class="cb-badge cb-open">Inactive</span>';return'<tr><td>'+esc(t.name)+'</td><td>'+(t.quota_daily||"unlimited")+'</td><td>'+(t.quota_monthly||"unlimited")+'</td><td>'+(t.rate_limit||"unlimited")+'</td><td>'+active+'</td></tr>';}).join("")+"</tbody></table>";}catch(e){document.getElementById("teamsList").innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>';}
}
async function createTeam(){
  var name=document.getElementById("tName").value.trim();
  var daily=parseFloat(document.getElementById("tDaily").value)||0;
  var monthly=parseFloat(document.getElementById("tMonthly").value)||0;
  var rate=parseInt(document.getElementById("tRate").value)||0;
  if(!name){document.getElementById("tMsg").innerHTML='<span style="color:#f87171">Name required.</span>';return;}
  try{var t=await post_("/api-keys/teams",{name:name,quota_daily:daily,quota_monthly:monthly,rate_limit:rate});
  document.getElementById("tMsg").innerHTML='<span style="color:#4ade80">Team created: '+esc(t.team_id)+'</span>';
  document.getElementById("tName").value="";loadTeams();loadTeamsForBudget();loadTeamsForUsage();
  }catch(e){document.getElementById("tMsg").innerHTML='<span style="color:#f87171">'+esc(e.message)+'</span>';}
}
async function loadApiKeys(){
  try{var keys=await get("/api-keys/keys");document.getElementById("keysList").innerHTML=!keys.length?'<div class="empty">No API keys yet.</div>':'<table><thead><tr><th>Name</th><th>Key ID</th><th>Team</th><th>Scopes</th><th>Created</th><th>Last Used</th><th>Status</th><th></th></tr></thead><tbody>'+keys.map(function(k){var active=k.is_active?'<span class="cb-badge cb-closed">Active</span>':'<span class="cb-badge cb-open">Revoked</span>';return'<tr><td>'+esc(k.name)+'</td><td class="mono">'+esc(k.key_id)+'</td><td>'+esc(k.team_id||"")+'</td><td>'+(k.scopes||[]).join(", ")+'</td><td>'+fmtTs(k.created_at)+'</td><td>'+fmtTs(k.last_used||"Never")+'</td><td>'+active+'</td><td><button class="btn btn-danger btn-sm" onclick="revokeKey(\''+esc(k.key_id).replace(/'/g,"\\'")+'\')">Revoke</button></td></tr>';}).join("")+"</tbody></table>";}catch(e){document.getElementById("keysList").innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>';}
}
async function createApiKey(){
  var teamId=document.getElementById("akTeam").value;
  var name=document.getElementById("akName").value.trim();
  var secret=document.getElementById("akSecret").value.trim();
  if(!teamId||!name){document.getElementById("akResult").innerHTML='<span style="color:#f87171">Team and name required.</span>';return;}
  try{var r=await post_("/api-keys/keys",{team_id:teamId,name:name,secret:secret||undefined});
  document.getElementById("akResult").innerHTML='<div style="background:var(--input-bg);border:1px solid var(--border);border-radius:6px;padding:12px;margin-top:8px"><div style="color:#f97316;font-weight:600;margin-bottom:4px">⚠️ Save this key now — it will not be shown again!</div><div class="mono" style="font-size:12px;word-break:break-all;color:var(--accent)">'+esc(r.plaintext_key)+'</div><div style="font-size:11px;color:var(--text3);margin-top:4px">Key ID: '+esc(r.key.key_id)+'</div></div>';
  loadApiKeys();}catch(e){document.getElementById("akResult").innerHTML='<span style="color:#f87171">'+esc(e.message)+'</span>';}
}
async function revokeKey(keyId){if(!confirm("Revoke key "+keyId+"?"))return;try{await del__("/api-keys/keys/"+encodeURIComponent(keyId));loadApiKeys();}catch(e){alert("Error: "+e.message);}}

// ── Budget Alerts ─────────────────────────────────────────────────────────────
async function loadTeamsForBudget(){
  try{var teams=await get("/api-keys/teams");var sel=document.getElementById("baTeam");sel.innerHTML='<option value="">Select team...</option>'+teams.map(function(t){return'<option value="'+esc(t.team_id)+'">'+esc(t.name)+'</option>';}).join("");}catch(e){}
}
async function createBudgetAlert(){
  var teamId=document.getElementById("baTeam").value;
  var url=document.getElementById("baUrl").value.trim();
  var threshold=parseFloat(document.getElementById("baThreshold").value)||80;
  var type=document.getElementById("baType").value;
  var dailyLimit=parseFloat(document.getElementById("baDailyLimit").value)||0;
  var monthlyLimit=parseFloat(document.getElementById("baMonthlyLimit").value)||0;
  var secret=document.getElementById("baSecret").value.trim();
  if(!teamId||!url){document.getElementById("baMsg").innerHTML='<span style="color:#f87171">Team and webhook URL required.</span>';return;}
  try{var a=await post_("/budget-alerts/alerts",{org_id:teamId,webhook_url:url,threshold_pct:threshold,alert_type:type,daily_limit:dailyLimit,monthly_limit:monthlyLimit,secret:secret||undefined});
  document.getElementById("baMsg").innerHTML='<span style="color:#4ade80">Alert created: '+esc(a.alert_id)+'</span>';
  document.getElementById("baUrl").value="";loadAlerts();}catch(e){document.getElementById("baMsg").innerHTML='<span style="color:#f87171">'+esc(e.message)+'</span>';}
}
async function loadAlerts(){
  try{var alerts=await get("/budget-alerts/alerts");document.getElementById("alertsList").innerHTML=!alerts.length?'<div class="empty">No alerts configured.</div>':'<table><thead><tr><th>Alert ID</th><th>Org</th><th>Type</th><th>Threshold</th><th>Daily Limit</th><th>Monthly Limit</th><th>Last Fired</th><th>Status</th><th></th></tr></thead><tbody>'+alerts.map(function(a){var en=a.enabled?'<span class="cb-badge cb-closed">Active</span>':'<span class="cb-badge cb-open">Disabled</span>';return'<tr><td class="mono">'+esc(a.alert_id)+'</td><td>'+esc(a.org_id)+'</td><td>'+esc(a.alert_type)+'</td><td>'+a.threshold_pct+'%</td><td>'+(a.daily_limit||"none")+'</td><td>'+(a.monthly_limit||"none")+'</td><td>'+fmtTs(a.last_triggered||"Never")+'</td><td>'+en+'</td><td><button class="btn btn-ghost btn-sm" onclick="toggleAlert(\''+esc(a.alert_id).replace(/'/g,"\\'")+'\','+!a.enabled+')">'+(a.enabled?"Disable":"Enable")+'</button> <button class="btn btn-danger btn-sm" onclick="deleteAlert(\''+esc(a.alert_id).replace(/'/g,"\\'")+'\')">Delete</button></td></tr>';}).join("")+"</tbody></table>";}catch(e){document.getElementById("alertsList").innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>';}
}
async function loadAlertEvents(){
  try{var events=await get("/budget-alerts/events?limit=50");document.getElementById("alertEvents").innerHTML=!events.length?'<div class="empty">No events yet.</div>':'<table><thead><tr><th>Fired At</th><th>Alert ID</th><th>Org</th><th>Type</th><th>Spent</th><th>Limit</th><th>% Used</th><th>Delivery</th></tr></thead><tbody>'+events.map(function(e){var cls=e.delivery_status==="delivered"?"badge-green":e.delivery_status==="failed"?"badge-red":"badge-yellow";return'<tr><td style="white-space:nowrap">'+fmtTs(e.fired_at)+'</td><td class="mono">'+esc(e.alert_id)+'</td><td>'+esc(e.org_id)+'</td><td>'+esc(e.alert_type)+'</td><td>$'+parseFloat(e.spent).toFixed(4)+'</td><td>$'+parseFloat(e.limit_amt).toFixed(2)+'</td><td>'+parseFloat(e.pct_used).toFixed(1)+'%</td><td><span class="badge '+cls+'">'+esc(e.delivery_status||"")+'</span></td></tr>';}).join("")+"</tbody></table>";}catch(e){document.getElementById("alertEvents").innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>';}
}
async function toggleAlert(alertId,enable){try{await post_("/budget-alerts/alerts/"+encodeURIComponent(alertId),{enabled:enable});loadAlerts();}catch(e){alert("Error: "+e.message);}}
async function deleteAlert(alertId){if(!confirm("Delete alert "+alertId+"?"))return;try{await del__("/budget-alerts/alerts/"+encodeURIComponent(alertId));loadAlerts();}catch(e){alert("Error: "+e.message);}}

// ── Usage Dashboard ──────────────────────────────────────────────────────────
async function loadTeamsForUsage(){
  try{var teams=await get("/api-keys/teams");var sel=document.getElementById("uTeamSel");sel.innerHTML='<option value="">All teams</option>'+teams.map(function(t){return'<option value="'+esc(t.team_id)+'">'+esc(t.name)+'</option>';}).join("");}catch(e){}
}
async function loadUsage(){
  var teamId=document.getElementById("uTeamSel").value;
  var period=document.getElementById("uPeriod").value;
  var endpoint="/cost-stats"+(teamId?"?org_id="+encodeURIComponent(teamId):"");
  try{
    var stats=await apiGet(endpoint);
    var today=stats.today_total||0;var month=stats.month_total||0;
    var dailyLimit=stats.daily_limit||0;var monthlyLimit=stats.monthly_limit||0;
    document.getElementById("uToday").textContent="$"+today.toFixed(4);
    document.getElementById("uMonth").textContent="$"+month.toFixed(4);
    document.getElementById("uTodayPct").textContent=dailyLimit>0?(today/dailyLimit*100).toFixed(1)+"%":"—";
    document.getElementById("uMonthPct").textContent=monthlyLimit>0?(month/monthlyLimit*100).toFixed(1)+"%":"—";
    var byModel=stats.by_model||[];
    document.getElementById("modelCostTable").innerHTML=!byModel.length?'<div class="empty">No usage data.</div>':'<table><thead><tr><th>Model</th><th>Calls</th><th>Total Cost</th><th>Avg Cost/Call</th></tr></thead><tbody>'+byModel.map(function(m){return'<tr><td class="mono">'+esc(m.model)+'</td><td>'+m.calls+'</td><td>$'+parseFloat(m.cost).toFixed(4)+'</td><td>$'+(m.calls>0?(parseFloat(m.cost)/m.calls).toFixed(4):"0")+'</td></tr>';}).join("")+"</tbody></table>";
    document.getElementById("usageTable").innerHTML='<div class="empty">Select a team and period above.</div>';
  }catch(e){document.getElementById("modelCostTable").innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>';}
}
async function loadTeamUsage(){
  var teamId=document.getElementById("uTeamSel").value;
  if(!teamId){document.getElementById("usageTable").innerHTML='<div class="empty">Select a team.</div>';return;}
  try{var qs=await get("/api-keys/quota/"+encodeURIComponent(teamId));var period=document.getElementById("uPeriod").value;
  var spent=period==="today"?qs.spent_today:qs.spent_month;var limit=period==="today"?qs.daily_limit:qs.monthly_limit;
  var pct=limit>0?(spent/limit*100).toFixed(2)+"%":"unlimited";
  document.getElementById("usageTable").innerHTML='<table><thead><tr><th>Team</th><th>Spent ('+esc(period)+')</th><th>Limit</th><th>% Used</th><th>Exceeded</th></tr></thead><tbody><tr><td>'+esc(qs.team_id)+'</td><td>$'+parseFloat(spent).toFixed(4)+'</td><td>'+(limit>0?"$"+limit:"unlimited")+'</td><td>'+pct+'</td><td>'+(qs.is_exceeded?'<span class="cb-badge cb-open">YES</span>':'<span class="cb-badge cb-closed">No</span>')+'</td></tr></tbody></table>';}catch(e){document.getElementById("usageTable").innerHTML='<div class="empty">Error: '+esc(e.message)+'</div>';}
}

window.onload=function(){checkAuth();};

async function loadDistillationStats(){try{var r=await apiGet("/distillation/stats");document.getElementById("dist-monitored").textContent=r.monitored_users||0;document.getElementById("dist-high-risk").textContent=r.high_risk_users||0;document.getElementById("dist-flagged").textContent=r.total_flagged_requests||0;loadHighRiskUsers();}catch(e){console.error(e);}}
async function loadHighRiskUsers(){try{var r=await apiGet("/distillation/high-risk-users");var el=document.getElementById("dist-high-risk-list");if(!r.users||!r.users.length){el.innerHTML='<div class="empty">No high-risk users detected.</div>';return;}el.innerHTML='<table style="width:100%;font-size:13px"><thead><tr style="text-align:left;color:var(--text2);font-size:11px;text-transform:uppercase"><th>User ID</th><th>Risk Score</th><th>Requests</th><th>Signals</th><th>Recommendation</th><th>Slowdown</th></tr></thead><tbody>'+r.users.map(function(u){var score=Math.round(u.risk_score||0);var cls=score>=70?"var(--red)":score>=40?"var(--yellow)":"var(--accent)";return'<tr style="border-top:1px solid var(--border)"><td style="padding:6px 0" class="mono">'+esc(u.user_id||"")+'</td><td style="padding:6px 0;color:'+cls+';font-weight:600">'+score+'</td><td style="padding:6px 0">'+(u.total_requests||0)+'</td><td style="padding:6px 0;color:var(--text2)">'+((u.signals||[]).join(", ")||"—")+'</td><td style="padding:6px 0"><span class="cb-badge '+(u.recommendation==="allow"?"cb-closed":u.recommendation==="flag"?"cb-halfopen":u.recommendation==="slowdown"?"cb-open":"cb-closed")+'">'+esc(u.recommendation||"")+'</span></td><td style="padding:6px 0">'+((u.slowdown_multiplier||1)>=1?"none":"<span style=\'color:var(--yellow)\'>"+Math.round((1/u.slowdown_multiplier))+"x</span>")+'</td></tr>';}).join("")+'</tbody></table>';}catch(e){document.getElementById("dist-high-risk-list").innerHTML='<div class="alert-error">Error: '+esc(e.message)+'</div>';}}
async function checkDistillation(){var userId=document.getElementById("dist-user").value.trim()||"test-user";var prompt=document.getElementById("dist-prompt").value.trim();if(!prompt){document.getElementById("dist-check-result").innerHTML='<div class="alert-error">Enter a prompt to check.</div>';return;}try{var r=await apiPost("/distillation/check",{user_id:userId,prompt:prompt,tokens:parseInt(document.getElementById("dist-tokens").value)||0,is_paid_tier:document.getElementById("dist-paid").checked,is_authenticated:document.getElementById("dist-auth").checked});var score=Math.round(r.risk_score||0);var cls=score>=70?"var(--red)":score>=40?"var(--yellow)":"var(--accent)";var out='<div style="margin-bottom:8px">Risk Score: <span style="color:'+cls+';font-size:18px;font-weight:700">'+score+'</span> / 100 &nbsp; <span class="cb-badge '+(r.recommendation==="allow"?"cb-closed":r.recommendation==="flag"?"cb-halfopen":r.recommendation==="slowdown"?"cb-open":"cb-closed")+'">'+esc(r.recommendation||"")+'</span> &nbsp; Slowdown: <strong>'+r.slowdown_multiplier+'x</strong></div>';out+='<div style="margin-bottom:8px">Confidence: <strong>'+Math.round((r.confidence||0)*100)+'%</strong></div>';if(r.signals&&r.signals.length)out+='<div style="margin-bottom:8px">Signals: '+r.signals.map(function(s){return'<span style="background:rgba(248,113,113,0.15);color:var(--red);padding:2px 6px;border-radius:4px;margin-right:4px;font-size:12px">'+esc(s)+'</span>';}).join("")+'</div>';else out+='<div style="margin-bottom:8px;color:var(--text2)">No signals triggered.</div>';out+='<div style="margin-top:8px;font-size:11px;color:var(--text3)">Extraction: '+(r.is_extraction_pattern?"<span style=\'color:var(--red)\'>Y</span>":"<span style=\'color:var(--text3)\'>N</span>")+' &nbsp; High Vol: '+(r.is_high_volume?"<span style=\'color:var(--red)\'>Y</span>":"<span style=\'color:var(--text3)\'>N</span>")+' &nbsp; Systematic: '+(r.is_systematic?"<span style=\'color:var(--red)\'>Y</span>":"<span style=\'color:var(--text3)\'>N</span>")+' &nbsp; Legitimate: '+(r.is_legitimate_context?"<span style=\'color:var(--accent)\'>Y</span>":"<span style=\'color:var(--text3)\'>N</span>")+'</div>';document.getElementById("dist-check-result").innerHTML=out;}catch(e){document.getElementById("dist-check-result").innerHTML='<div class="alert-error">Error: '+esc(e.message)+'</div>';}}
async function lookupDistillationUser(){var uid=document.getElementById("dist-lookup-user").value.trim();if(!uid){document.getElementById("dist-user-result").innerHTML='<div class="alert-error">Enter a user ID.</div>';return;}try{var r=await apiGet("/distillation/users/"+encodeURIComponent(uid));var score=Math.round(r.risk_score||0);var cls=score>=70?"var(--red)":score>=40?"var(--yellow)":"var(--accent)";document.getElementById("dist-user-result").innerHTML='<div style="margin-bottom:8px">Risk: <span style="color:'+cls+';font-weight:700">'+score+'</span>/100 &nbsp; Rec: <strong>'+esc(r.recommendation||"")+'</strong></div><div style="font-size:12px;color:var(--text2);display:grid;grid-template-columns:1fr 1fr;gap:4px"><div>Requests: <strong>'+r.total_requests+'</strong></div><div>Rate: <strong>'+r.requests_per_hour+'</strong>/hr</div><div>Unique ratio: <strong>'+r.unique_ratio+'</strong></div><div>Similarity: <strong>'+r.recent_similarity+'</strong></div><div>Extraction hits: <strong>'+r.extraction_hits+'</strong></div><div>Legitimate hits: <strong>'+r.legitimate_hits+'</strong></div></div>';}catch(e){document.getElementById("dist-user-result").innerHTML='<div class="alert-error">'+esc(e.message)+'</div>';}}
async function resetDistillationUser(){var uid=document.getElementById("dist-lookup-user").value.trim();if(!uid)return;if(!confirm("Reset metrics for user \""+uid+"\"?"))return;try{await apiPost("/distillation/users/"+encodeURIComponent(uid)+"/reset",{});document.getElementById("dist-user-result").innerHTML='<div class="alert-success">Metrics reset for '+esc(uid)+'</div>';loadDistillationStats();}catch(e){document.getElementById("dist-user-result").innerHTML='<div class="alert-error">'+esc(e.message)+'</div>';}}

</script>
</body>
</html>
"""

# ─── Shared Store Instances ─────────────────────────────────────────────────────
_api_key_store = None
_budget_alert_store = None
_guardrails_instance = None
_distillation_detector = None


def _get_api_key_store():
    global _api_key_store
    if _api_key_store is None:
        try:
            db_path = os.environ.get("MODELFUNGIBLE_API_KEYS_DB", ".modelfungible/api_keys.db")
            _api_key_store = APIKeyStore(db_path)
        except Exception as e:
            print(f"[api_keys] Init failed: {e}")
    return _api_key_store


def _get_budget_alert_store():
    global _budget_alert_store
    if _budget_alert_store is None:
        try:
            db_path = os.environ.get("MODELFUNGIBLE_BUDGET_DB", ".modelfungible/budget_alerts.db")
            _budget_alert_store = BudgetAlertStore(db_path)
        except Exception as e:
            print(f"[budget_alerts] Init failed: {e}")
    return _budget_alert_store


def _get_guardrails():
    """Returns the global Guardrails instance (configured via env or default)."""
    global _guardrails_instance
    if _guardrails_instance is None:
        # Load from env: BLOCKED_TERMS=term1,term2;MAX_OUTPUT_LENGTH=2000
        terms_str = os.environ.get("MODELFUNGIBLE_BLOCKED_TERMS", "")
        blocked_terms = [t.strip() for t in terms_str.split(",") if t.strip()]
        max_len_str = os.environ.get("MODELFUNGIBLE_MAX_OUTPUT_LENGTH", "")
        max_len = int(max_len_str) if max_len_str.isdigit() else None
        cfg = GuardrailConfig(blocked_terms=blocked_terms, max_length=max_len)
        _guardrails_instance = Guardrails(cfg)
    return _guardrails_instance


def _get_distillation():
    """Returns the global DistillationDetector instance."""
    global _distillation_detector
    if _distillation_detector is None:
        _distillation_detector = DistillationDetector(
            high_risk_score=70,
            medium_risk_score=40,
            volume_threshold_per_hour=50,
        )
    return _distillation_detector


def _get_guardrails_from_request(data: dict) -> Guardrails:
    """Build per-request guardrails from output_filter in request data."""
    return build_guardrails_from_dict(data)


@app.get("/admin", include_in_schema=False)
async def admin_ui():
    return HTMLResponse(content=HTML_UI, media_type="text/html")


# ─── Guardrail Test Endpoint ───────────────────────────────────────────────────
@app.post("/api/cache/guardrail-test")
def api_guardrail_test(data: dict, ctx: AuthContext = require_auth()):
    """Test guardrail filtering on arbitrary output text."""
    output = data.get("output", "")
    blocked = data.get("blocked_terms", [])
    max_len = data.get("max_length")
    cfg = GuardrailConfig(blocked_terms=blocked, max_length=max_len)
    g = Guardrails(cfg)
    r = g.apply(output)
    return JSONResponse({
        "passed": r.passed,
        "filtered_output": r.filtered_output,
        "terms_blocked": r.terms_blocked,
        "was_truncated": r.was_truncated,
        "reason": r.reason,
    })


# ─── API Keys Endpoints ────────────────────────────────────────────────────────
@app.get("/api-keys/teams")
def api_list_teams(ctx: AuthContext = require_admin()):
    store = _get_api_key_store()
    if store is None:
        return JSONResponse({"error": "API key store unavailable"}, status_code=503)
    teams = store.list_teams()
    return JSONResponse([{
        "team_id": t.team_id, "name": t.name,
        "quota_daily": t.quota_daily, "quota_monthly": t.quota_monthly,
        "rate_limit": t.rate_limit, "is_active": t.is_active,
        "created_at": t.created_at.isoformat(),
    } for t in teams])


@app.post("/api-keys/teams")
def api_create_team(data: dict, ctx: AuthContext = require_admin()):
    store = _get_api_key_store()
    if store is None:
        return JSONResponse({"error": "API key store unavailable"}, status_code=503)
    team = store.create_team(
        name=data.get("name", "").strip(),
        quota_daily=float(data.get("quota_daily", 0)),
        quota_monthly=float(data.get("quota_monthly", 0)),
        rate_limit=int(data.get("rate_limit", 0)),
    )
    return JSONResponse({
        "team_id": team.team_id, "name": team.name,
        "quota_daily": team.quota_daily, "quota_monthly": team.quota_monthly,
        "rate_limit": team.rate_limit, "is_active": team.is_active,
        "created_at": team.created_at.isoformat(),
    })


@app.get("/api-keys/keys")
def api_list_keys(ctx: AuthContext = require_admin()):
    store = _get_api_key_store()
    if store is None:
        return JSONResponse({"error": "API key store unavailable"}, status_code=503)
    keys = store.list_keys()
    return JSONResponse([{
        "key_id": k.key_id, "team_id": k.team_id, "name": k.name,
        "scopes": k.scopes, "is_active": k.is_active,
        "created_at": k.created_at.isoformat(),
        "last_used": k.last_used.isoformat() if k.last_used else None,
        "expires_at": k.expires_at.isoformat() if k.expires_at else None,
    } for k in keys])


@app.post("/api-keys/keys")
def api_create_key(data: dict, ctx: AuthContext = require_admin()):
    """Create an API key. Returns plaintext key ONLY once."""
    store = _get_api_key_store()
    if store is None:
        return JSONResponse({"error": "API key store unavailable"}, status_code=503)
    team_id = data.get("team_id", "").strip()
    name = data.get("name", "").strip()
    secret = data.get("secret", "")
    if not team_id or not name:
        raise HTTPException(400, "team_id and name are required")
    try:
        ak, plaintext = store.create_key(team_id, name, secret=secret or None)
        return JSONResponse({
            "key": {
                "key_id": ak.key_id, "team_id": ak.team_id,
                "name": ak.name, "scopes": ak.scopes,
                "created_at": ak.created_at.isoformat(),
            },
            "plaintext_key": plaintext,
        })
    except Exception as e:
        raise HTTPException(400, str(e))


@app.delete("/api-keys/keys/{key_id}")
def api_revoke_key(key_id: str, ctx: AuthContext = require_admin()):
    store = _get_api_key_store()
    if store is None:
        return JSONResponse({"error": "API key store unavailable"}, status_code=503)
    ok = store.revoke_key(key_id)
    return JSONResponse({"revoked": ok})


@app.get("/api-keys/quota/{team_id}")
def api_quota_status(team_id: str, ctx: AuthContext = require_admin()):
    store = _get_api_key_store()
    if store is None:
        return JSONResponse({"error": "API key store unavailable"}, status_code=503)
    qs = store.get_quota_status(team_id)
    return JSONResponse({
        "team_id": qs.team_id,
        "spent_today": qs.spent_today,
        "spent_month": qs.spent_month,
        "daily_limit": qs.daily_limit,
        "monthly_limit": qs.monthly_limit,
        "daily_pct": qs.daily_pct,
        "monthly_pct": qs.monthly_pct,
        "is_exceeded": qs.is_exceeded,
        "exceeded_scope": qs.exceeded_scope,
    })


# ─── Budget Alerts Endpoints ───────────────────────────────────────────────────
@app.get("/budget-alerts/alerts")
def api_list_alerts(org_id: Optional[str] = None, ctx: AuthContext = require_admin()):
    store = _get_budget_alert_store()
    if store is None:
        return JSONResponse({"error": "Budget alert store unavailable"}, status_code=503)
    alerts = store.list_alerts(org_id=org_id)
    return JSONResponse([{
        "alert_id": a.alert_id,
        "org_id": a.org_id,
        "threshold_pct": a.threshold_pct,
        "webhook_url": a.webhook_url,
        "alert_type": a.alert_type,
        "daily_limit": a.daily_limit,
        "monthly_limit": a.monthly_limit,
        "enabled": a.enabled,
        "last_triggered": a.last_triggered.isoformat() if a.last_triggered else None,
        "created_at": a.created_at.isoformat(),
    } for a in alerts])


@app.post("/budget-alerts/alerts")
def api_create_alert(data: dict, ctx: AuthContext = require_admin()):
    store = _get_budget_alert_store()
    if store is None:
        return JSONResponse({"error": "Budget alert store unavailable"}, status_code=503)
    alert = store.create_alert(
        org_id=data.get("org_id", "default-org"),
        webhook_url=data.get("webhook_url", ""),
        threshold_pct=float(data.get("threshold_pct", 80)),
        alert_type=data.get("alert_type", "daily"),
        daily_limit=float(data.get("daily_limit", 0)),
        monthly_limit=float(data.get("monthly_limit", 0)),
        secret=data.get("secret", ""),
    )
    return JSONResponse({
        "alert_id": alert.alert_id,
        "org_id": alert.org_id,
        "threshold_pct": alert.threshold_pct,
        "webhook_url": alert.webhook_url,
        "alert_type": alert.alert_type,
        "enabled": alert.enabled,
    })


@app.post("/budget-alerts/alerts/{alert_id}")
def api_update_alert(alert_id: str, data: dict, ctx: AuthContext = require_admin()):
    store = _get_budget_alert_store()
    if store is None:
        return JSONResponse({"error": "Budget alert store unavailable"}, status_code=503)
    updated = store.update_alert(alert_id, **data)
    if updated is None:
        raise HTTPException(404, "Alert not found")
    return JSONResponse({"alert_id": updated.alert_id, "enabled": updated.enabled})


@app.delete("/budget-alerts/alerts/{alert_id}")
def api_delete_alert(alert_id: str, ctx: AuthContext = require_admin()):
    store = _get_budget_alert_store()
    if store is None:
        return JSONResponse({"error": "Budget alert store unavailable"}, status_code=503)
    ok = store.delete_alert(alert_id)
    return JSONResponse({"deleted": ok})


@app.get("/budget-alerts/events")
def api_alert_events(
    org_id: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    ctx: AuthContext = require_admin(),
):
    store = _get_budget_alert_store()
    if store is None:
        return JSONResponse({"error": "Budget alert store unavailable"}, status_code=503)
    events = store.get_events(org_id=org_id, limit=limit, offset=offset)
    return JSONResponse(events)


@app.get("/budget-alerts/stats/{alert_id}")
def api_alert_stats(alert_id: str, ctx: AuthContext = require_admin()):
    store = _get_budget_alert_store()
    if store is None:
        return JSONResponse({"error": "Budget alert store unavailable"}, status_code=503)
    return JSONResponse(store.get_alert_stats(alert_id))


# ─── Usage Dashboard (cost-stats enrichment) ───────────────────────────────────
@app.get("/api/cost-stats")
def api_cost_stats(
    period: str = "day",
    by: str = "model",
    org_id: Optional[str] = None,
    ctx: AuthContext = require_auth(),
):
    """Cost statistics — enriched with quota info if API key store available."""
    from datetime import datetime, timedelta, timezone
    audit = get_audit_logger()
    if audit is None:
        return JSONResponse({"error": "Audit unavailable"}, status_code=503)

    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=1)).isoformat()
    entries = audit.query(start_date=start, action="model_execute", limit=100000)

    # Aggregate cost
    total_cost = 0.0
    model_costs = {}
    for e in entries:
        m = (e.get("model_id") or "unknown")
        c = float(e.get("cost_usd") or 0)
        total_cost += c
        model_costs[m] = model_costs.get(m, 0.0) + c

    today_total = total_cost

    # Monthly
    month_start = (now - timedelta(days=30)).isoformat()
    month_entries = audit.query(start_date=month_start, action="model_execute", limit=100000)
    month_total = sum(float(e.get("cost_usd", 0)) for e in month_entries)

    # Quota status if available
    daily_limit = 0.0
    monthly_limit = 0.0
    if org_id:
        ks = _get_api_key_store()
        if ks:
            qs = ks.get_quota_status(org_id)
            daily_limit = qs.daily_limit
            monthly_limit = qs.monthly_limit

    by_model = [{"model": m, "cost": round(c, 6), "calls": sum(1 for e in entries if e.get("model_id") == m)}
                 for m, c in sorted(model_costs.items(), key=lambda x: -x[1])]

    return JSONResponse({
        "today_total": round(today_total, 6),
        "month_total": round(month_total, 6),
        "daily_limit": daily_limit,
        "monthly_limit": monthly_limit,
        "by_model": by_model,
        "period": period,
    })
