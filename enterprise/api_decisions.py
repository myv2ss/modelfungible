# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Decision Attribution + Prompt Marketplace API endpoints.
Import into admin_app.py via: from enterprise.api_markets import router_markets
"""
from __future__ import annotations

import json, os, sys
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Header, Depends
from fastapi.responses import JSONResponse

# ── Imports with fallback ──────────────────────────────────────────────────────
try:
    from modelfungible.enterprise.audit import AuditLogger, PIIDetector
    from modelfungible.enterprise.prompt_marketplace import PromptStore
    from modelfungible.enterprise.decision_attribution import DecisionStore, ModelScore
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from enterprise.audit import AuditLogger, PIIDetector
    from enterprise.prompt_marketplace import PromptStore
    from enterprise.decision_attribution import DecisionStore, ModelScore

# ─── Stores ────────────────────────────────────────────────────────────────────
_prompt_store = None
_decision_store = None
_audit_logger = None


def _init_stores():
    global _prompt_store, _decision_store, _audit_logger
    if _prompt_store is None:
        try:
            _prompt_store = PromptStore()
        except Exception as e:
            print(f"[prompts] Init failed: {e}")
    if _decision_store is None:
        try:
            _decision_store = DecisionStore()
        except Exception as e:
            print(f"[decisions] Init failed: {e}")


def _get_prompt_store():
    _init_stores()
    return _prompt_store


def _get_decision_store():
    _init_stores()
    return _decision_store


# ─── Auth dependency (must match admin_app.py signature) ───────────────────────
class AuthContext:
    def __init__(self, user_id: str, role: str, session_id: str = ""):
        self.user_id = user_id
        self.role = role
        self.session_id = session_id


def _require_auth(x_auth_token: Optional[str] = Header(None)) -> AuthContext:
    from fastapi import HTTPException
    if x_auth_token is None:
        raise HTTPException(401, {"error": "Login required"})
    # Import from admin_app to share session state
    try:
        from modelfungible.enterprise.admin_app import get_session
        tok = x_auth_token.replace("Bearer ", "")
        s = get_session(tok) if 'get_session' in dir() else None
        if s is None:
            raise HTTPException(401, {"error": "Session expired"})
        return AuthContext(user_id=s.user_id, role=s.role, session_id=s.session_id)
    except (ImportError, AttributeError):
        raise HTTPException(401, {"error": "Auth unavailable"})


def _require_admin(ctx: AuthContext = None) -> AuthContext:
    from fastapi import HTTPException
    if ctx is None or ctx.role != "admin":
        raise HTTPException(403, {"error": "Admin role required"})
    return ctx


def _require_trader_or_admin(ctx: AuthContext = None) -> AuthContext:
    from fastapi import HTTPException
    if ctx is None or ctx.role not in ("admin", "trader"):
        raise HTTPException(403, {"error": "Trader or admin role required"})
    return ctx


# ─── Decision Attribution Endpoints ────────────────────────────────────────────
router_decisions = APIRouter(prefix="/api/decisions", tags=["decisions"])


@router_decisions.get("")
def decisions_list(
    actor: Optional[str] = None,
    model: Optional[str] = None,
    mode: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    ctx: AuthContext = Depends(_require_auth),
):
    store = _get_decision_store()
    if store is None:
        return JSONResponse({"decisions": [], "error": "Decision store unavailable"})
    decisions = store.query(actor=actor, model=model, mode=mode,
                            start_date=start_date, end_date=end_date,
                            limit=limit, offset=offset)
    return JSONResponse({
        "decisions": [
            {"request_id": d.request_id, "timestamp": d.timestamp, "actor": d.actor,
             "selected_model": d.selected_model, "selected_provider": d.selected_provider,
             "mode": d.mode, "capability": d.capability,
             "total_latency_ms": d.total_latency_ms,
             "total_cost_usd": round(d.total_cost_usd, 6),
             "candidate_count": d.candidate_count, "attempt_count": d.attempt_count,
             "pii_detected": d.piid_detected, "request_summary": d.request_summary}
            for d in decisions
        ], "limit": limit, "offset": offset,
    })


@router_decisions.get("/stats")
def decisions_stats(
    model: Optional[str] = None,
    ctx: AuthContext = Depends(_require_auth),
):
    store = _get_decision_store()
    if store is None:
        return JSONResponse({"error": "Decision store unavailable"})
    return JSONResponse(store.model_stats(model))


@router_decisions.get("/{request_id}/explain")
def decision_explain(request_id: str, ctx: AuthContext = Depends(_require_auth)):
    store = _get_decision_store()
    if store is None:
        return JSONResponse({"error": "Decision store unavailable"})
    result = store.explain(request_id)
    if result is None:
        raise HTTPException(404, "Decision not found")
    return JSONResponse(result)


@router_decisions.get("/similar")
def decisions_similar(
    q: str, model: Optional[str] = None, limit: int = 5,
    ctx: AuthContext = Depends(_require_auth),
):
    store = _get_decision_store()
    if store is None:
        return JSONResponse({"decisions": []})
    decisions = store.similar(q, model=model, limit=limit)
    return JSONResponse({
        "query": q,
        "decisions": [
            {"request_id": d.request_id, "timestamp": d.timestamp,
             "selected_model": d.selected_model, "selected_provider": d.selected_provider,
             "mode": d.mode, "total_latency_ms": d.total_latency_ms,
             "total_cost_usd": round(d.total_cost_usd, 6),
             "request_summary": d.request_summary}
            for d in decisions
        ]
    })


# ─── Prompt Marketplace Endpoints ──────────────────────────────────────────────
router_prompts = APIRouter(prefix="/api/prompts", tags=["prompts"])


@router_prompts.get("")
def prompts_list(
    domain: Optional[str] = None, status: Optional[str] = None,
    tags: Optional[str] = None, search: Optional[str] = None,
    limit: int = 50, offset: int = 0,
    ctx: AuthContext = Depends(_require_auth),
):
    store = _get_prompt_store()
    if store is None:
        return JSONResponse({"prompts": [], "error": "Prompt store unavailable"})
    tag_list = tags.split(",") if tags else None
    prompts = store.list_prompts(domain=domain, status=status, tags=tag_list,
                                  search=search, limit=limit, offset=offset)
    return JSONResponse({
        "prompts": [
            {"prompt_id": p.prompt_id, "name": p.name, "domain": p.domain,
             "description": p.description, "created_by": p.created_by,
             "created_at": p.created_at, "status": p.status,
             "call_count": p.call_count,
             "avg_cost_per_call": round(p.avg_cost_per_call, 6),
             "avg_latency_ms": round(p.avg_latency_ms, 1),
             "error_rate": round(p.error_rate, 3),
             "like_count": p.like_count,
             "tags": (p.versions[0].tags if p.versions else []),
             "variables": (p.versions[0].variables if p.versions else []),
             "version_count": len(p.versions)}
            for p in prompts
        ], "limit": limit, "offset": offset,
    })


@router_prompts.get("/{prompt_id}")
def prompt_get(prompt_id: str, ctx: AuthContext = Depends(_require_auth)):
    store = _get_prompt_store()
    if store is None:
        return JSONResponse({"error": "Prompt store unavailable"})
    p = store.get(prompt_id)
    if p is None:
        raise HTTPException(404, "Prompt not found")
    return JSONResponse({
        "prompt_id": p.prompt_id, "name": p.name, "domain": p.domain,
        "description": p.description, "created_by": p.created_by,
        "created_at": p.created_at, "status": p.status,
        "call_count": p.call_count,
        "avg_cost_per_call": round(p.avg_cost_per_call, 6),
        "avg_latency_ms": round(p.avg_latency_ms, 1),
        "like_count": p.like_count,
        "versions": [
            {"version_id": v.version_id, "version_num": v.version_num,
             "name": v.name, "description": v.description,
             "prompt_text": v.prompt_text, "system_prompt": v.system_prompt,
             "variables": v.variables, "use_cases": v.use_cases,
             "tags": v.tags, "created_by": v.created_by,
             "created_at": v.created_at, "is_active": v.is_active}
            for v in p.versions
        ]
    })


@router_prompts.post("")
def prompt_create(
    data: dict,
    ctx: AuthContext = Depends(_require_trader_or_admin),
):
    store = _get_prompt_store()
    if store is None:
        return JSONResponse({"error": "Prompt store unavailable"}, status_code=503)
    p = store.create_prompt(name=data["name"], created_by=ctx.user_id,
                             domain=data.get("domain", "general"),
                             description=data.get("description", ""))
    if data.get("prompt_text"):
        store.add_version(prompt_id=p.prompt_id, version_num=1,
                          name=data.get("name", p.name),
                          prompt_text=data["prompt_text"],
                          system_prompt=data.get("system_prompt", ""),
                          description=data.get("description", ""),
                          use_cases=data.get("use_cases", []),
                          tags=data.get("tags", []),
                          created_by=ctx.user_id)
    return JSONResponse({"prompt_id": p.prompt_id, "created": True})


@router_prompts.post("/{prompt_id}/versions")
def prompt_add_version(
    prompt_id: str, data: dict,
    ctx: AuthContext = Depends(_require_trader_or_admin),
):
    store = _get_prompt_store()
    if store is None:
        return JSONResponse({"error": "Prompt store unavailable"}, status_code=503)
    p = store.get(prompt_id)
    if p is None:
        raise HTTPException(404, "Prompt not found")
    vn = (p.versions[0].version_num + 1) if p.versions else 1
    v = store.add_version(prompt_id=prompt_id, version_num=vn,
                           name=data.get("name", p.name),
                           prompt_text=data["prompt_text"],
                           system_prompt=data.get("system_prompt", ""),
                           description=data.get("description", ""),
                           use_cases=data.get("use_cases", []),
                           tags=data.get("tags", []),
                           created_by=ctx.user_id)
    return JSONResponse({"version_id": v.version_id, "version_num": vn})


@router_prompts.post("/{prompt_id}/publish")
def prompt_publish(prompt_id: str, ctx: AuthContext = Depends(_require_admin)):
    store = _get_prompt_store()
    if store is None:
        return JSONResponse({"error": "Prompt store unavailable"}, status_code=503)
    store.publish(prompt_id)
    return JSONResponse({"prompt_id": prompt_id, "status": "published"})


@router_prompts.post("/{prompt_id}/archive")
def prompt_archive(prompt_id: str, ctx: AuthContext = Depends(_require_admin)):
    store = _get_prompt_store()
    if store is None:
        return JSONResponse({"error": "Prompt store unavailable"}, status_code=503)
    store.archive(prompt_id)
    return JSONResponse({"prompt_id": prompt_id, "status": "archived"})


@router_prompts.post("/{prompt_id}/rate")
def prompt_rate(prompt_id: str, data: dict, ctx: AuthContext = Depends(_require_auth)):
    store = _get_prompt_store()
    if store is None:
        return JSONResponse({"error": "Prompt store unavailable"}, status_code=503)
    store.rate(prompt_id, ctx.user_id, int(data.get("rating", 3)))
    return JSONResponse({"prompt_id": prompt_id, "rating": data.get("rating", 3)})


@router_prompts.delete("/{prompt_id}")
def prompt_delete(prompt_id: str, ctx: AuthContext = Depends(_require_admin)):
    store = _get_prompt_store()
    if store is None:
        return JSONResponse({"error": "Prompt store unavailable"}, status_code=503)
    store.delete(prompt_id)
    return JSONResponse({"prompt_id": prompt_id, "deleted": True})
