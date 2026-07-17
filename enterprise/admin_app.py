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
    from modelfungible.core.circuit_breaker import CircuitBreaker
    from modelfungible.core.rules_engine import RulesEngine
    from modelfungible.core.execute import ModelSelector, RouterMode, ModelProfile, ExecutionRequest, estimate_cost, DEFAULT_COSTS
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from enterprise.audit import AuditLogger, PIIDetector, RetentionPolicy
    from enterprise.license import LicenseKey
    from core.circuit_breaker import CircuitBreaker
    from core.rules_engine import RulesEngine
    from core.execute import ModelSelector, RouterMode, ModelProfile, ExecutionRequest, estimate_cost, DEFAULT_COSTS


# ─── MULTI-USER AUTH ──────────────────────────────────────────────────────────

@dataclass
class User:
    user_id: str
    name: str
    role: str          # "admin" | "trader" | "viewer"
    password_hash: str
    active: bool = True

    @staticmethod
    def hashpw(password: str) -> str:
        return hashlib.sha256(password.encode()).hexdigest()

    def check_password(self, password: str) -> bool:
        return self.active and secrets.compare_digest(self.password_hash, self.hashpw(password))


_DEFAULT_USERS = [
    User(user_id="admin", name="Administrator", role="admin",
         password_hash=User.hashpw(os.environ.get("MODELFUNGIBLE_ADMIN_PASSWORD", "changeme"))),
    User(user_id="trader1", name="Trader One", role="trader",
         password_hash=User.hashpw("trader123")),
    User(user_id="viewer1", name="Viewer", role="viewer",
         password_hash=User.hashpw("viewer123")),
]
_user_store: dict[str, User] = {}

def _load_users():
    global _user_store
    _user_store = {u.user_id: u for u in _DEFAULT_USERS}
    env_users = os.environ.get("MODELFUNGIBLE_USERS", "")
    if env_users:
        try:
            for u in json.loads(env_users):
                _user_store[u["user_id"]] = User(
                    user_id=u["user_id"], name=u["name"],
                    role=u.get("role", "viewer"),
                    password_hash=User.hashpw(u["password"])
                )
        except Exception as e:
            print(f"[auth] MODELFUNGIBLE_USERS parse error: {e}")
_load_users()

@dataclass
class Session:
    session_id: str
    user_id: str
    role: str
    created_at: float
    expires_at: float

_sessions: dict[str, Session] = {}
SESSION_TTL_HOURS = 12

def create_session(user: User) -> Session:
    sid = secrets.token_urlsafe(32)
    now = time.time()
    return _sessions.setdefault(sid, Session(
        session_id=sid, user_id=user.user_id, role=user.role,
        created_at=now, expires_at=now + SESSION_TTL_HOURS * 3600))

def get_session(sid: str) -> Optional[Session]:
    s = _sessions.get(sid)
    if s and time.time() <= s.expires_at:
        return s
    _sessions.pop(sid, None)
    return None

def delete_session(sid: str):
    _sessions.pop(sid, None)

class AuthContext:
    def __init__(self, user_id: str, role: str, session_id: str):
        self.user_id = user_id; self.role = role; self.session_id = session_id
    def is_admin(self): return self.role == "admin"
    def is_trader_or_admin(self): return self.role in ("admin", "trader")

def require_auth(x_auth_token: Optional[str] = Header(None)) -> AuthContext:
    if x_auth_token is None:
        raise HTTPException(401, {"error": "Login required"})
    tok = x_auth_token.replace("Bearer ", "")
    s = get_session(tok)
    if s is None:
        raise HTTPException(401, {"error": "Session expired — please login again"})
    return AuthContext(user_id=s.user_id, role=s.role, session_id=s.session_id)

def require_admin(ctx: AuthContext = None) -> AuthContext:
    if ctx is None or ctx.role != "admin":
        raise HTTPException(403, {"error": "Admin role required"})
    return ctx

def require_trader_or_admin(ctx: AuthContext = None) -> AuthContext:
    if ctx is None or ctx.role not in ("admin", "trader"):
        raise HTTPException(403, {"error": "Trader or admin role required"})
    return ctx

def audit_log(audit, action: str, ctx: AuthContext, outcome: str = "success", **kw):
    if audit:
        audit.log(action=action, actor=ctx.user_id, org_id="default-org",
                  outcome=outcome, metadata=kw)


class InMemoryRegistry:
    def __init__(self):
        self._models = {}
        self._breakers = {}
        self._engines = {}

    def register_model(self, name, provider, model_id, api_key, latency_ms_p50, capability,
                        cost_input_per_1k=0.001, cost_output_per_1k=0.002):
        if name in self._models:
            raise ValueError(f"Model already registered: {name}")
        # Auto-detect cost from DEFAULT_COSTS if model_id matches
        for cost_key, costs in DEFAULT_COSTS.items():
            if cost_key in model_id.lower() or model_id.lower() in cost_key:
                cost_input_per_1k = costs["input"]
                cost_output_per_1k = costs["output"]
                break
        self._models[name] = {
            "name": name, "provider": provider, "model_id": model_id,
            "api_key": api_key, "latency_ms_p50": latency_ms_p50,
            "capability": capability,
            "cost_input_per_1k": cost_input_per_1k,
            "cost_output_per_1k": cost_output_per_1k,
        }
        self._breakers[name] = CircuitBreaker(failure_threshold=5, cooldown_seconds=60)
        return self._models[name]

    def deregister_model(self, name):
        if name not in self._models:
            return False
        del self._models[name]
        if name in self._breakers:
            del self._breakers[name]
        return True

    def list_models(self):
        out = []
        for name, m in self._models.items():
            cb = self._breakers.get(name)
            state = cb.state() if cb else "CLOSED"
            health = "healthy" if state == "CLOSED" else ("degraded" if state == "HALF-OPEN" else "circuit_open")
            out.append({**m, "health": health, "circuit_state": state,
                         "cost_input_per_1k": m.get("cost_input_per_1k", 0.001),
                         "cost_output_per_1k": m.get("cost_output_per_1k", 0.002)})
        return out

    def list_circuit_breakers(self):
        return [{"model_name": n, "state": self._breakers[n].state(), "failure_count": self._breakers[n]._failure_count}
                for n in self._models]

    def reset_breaker(self, name):
        if name not in self._breakers:
            raise ValueError(f"No breaker for: {name}")
        self._breakers[name].reset()
        return {"model_name": name, "state": "CLOSED"}

    def get_engine(self, rules_path):
        rules_path = os.path.expanduser(rules_path)
        if rules_path not in self._engines:
            self._engines[rules_path] = RulesEngine(rules_path)
        return self._engines[rules_path]

    def list_strategies(self, rules_path):
        try:
            return self.get_engine(rules_path).list_strategies()
        except Exception:
            return []

    def get_strategy(self, rules_path, sid):
        try:
            engine = self.get_engine(rules_path)
            raw = engine._rules.get(sid)
            return {"strategy_id": sid, **raw} if raw else None
        except Exception:
            return None

    def validate_strategy_json(self, data):
        errors = []
        for f in ["strategy_id", "name", "entry_trigger", "sizing"]:
            if f not in data:
                errors.append(f"Missing: {f}")
        s = data.get("sizing", {})
        if not isinstance(s, dict):
            errors.append("sizing must be an object")
        else:
            for f in ["amount", "max_positions"]:
                if f not in s:
                    errors.append(f"sizing.{f} required")
        return {"valid": len(errors) == 0, "errors": errors}


_registry = InMemoryRegistry()
RULES_PATH = os.environ.get("MODELFUNGIBLE_RULES_PATH",
    str(Path(__file__).parent.parent / "examples" / "strategies"))
_audit_dir = os.environ.get("MODELFUNGIBLE_AUDIT_DIR", "/tmp/modelfungible_audit")
_audit_logger = None


def _build_model_profiles(registry):
    profiles = []
    for name, m in registry._models.items():
        cb = registry._breakers.get(name)
        state = cb.state() if cb else "CLOSED"
        profiles.append(ModelProfile(
            name=m["name"], provider=m["provider"], model_id=m["model_id"],
            api_key=m["api_key"], latency_ms_p50=m.get("latency_ms_p50", 500),
            capability=m.get("capability", "any"),
            cost_input_per_1k=m.get("cost_input_per_1k", 0.001),
            cost_output_per_1k=m.get("cost_output_per_1k", 0.002),
            failure_count=cb._failure_count if cb else 0,
            is_available=state != "OPEN",
        ))
    return profiles

def get_audit_logger():
    global _audit_logger
    if _audit_logger is None:
        try:
            _audit_logger = AuditLogger(_audit_dir)
        except Exception:
            _audit_logger = None
    return _audit_logger

app = FastAPI(title="ModelFungible Enterprise Admin", version="1.0.0")
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
    else:
        from modelfungible.adapters.openai import OpenAIAdapter
        return OpenAIAdapter(api_key=key, base_url=p), mid


@app.post("/api/execute")
def api_execute(data: dict, ctx: AuthContext = require_trader_or_admin()):
    """
    Universal LLM proxy: POST /api/execute
    {
      "prompt": "string",          # required
      "system": "string",           # optional, default provided
      "model": "claude-production", # optional — auto-select if absent
      "mode": "balanced",           # fastest|cheapest|balanced|capability
      "capability": "precise",     # code|vision|fast|precise|any
      "max_cost_per_call": 0.05,  # optional — reject if estimated cost exceeds
      "temperature": 0.7,         # optional
      "max_tokens": 1024           # optional
    }
    """
    prompt = data.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, {"error": "prompt is required"})

    system = data.get("system", "You are a helpful assistant.")
    explicit = data.get("model")
    mode_str = data.get("mode", "balanced")
    capability = data.get("capability", "any")
    max_cost = data.get("max_cost_per_call")
    temperature = float(data.get("temperature", 0.7))
    max_tokens = int(data.get("max_tokens", 1024))

    try:
        router_mode = RouterMode(mode_str)
    except ValueError:
        raise HTTPException(400, {"error": f"Invalid mode: {mode_str}. Use: fastest|cheapest|balanced|capability"})

    profiles = _build_model_profiles(_registry)
    if not profiles:
        raise HTTPException(503, {"error": "No models registered. Add a model in the Deployments tab."})

    selector = ModelSelector(profiles)
    req = ExecutionRequest(
        prompt=prompt, system=system, model=explicit,
        mode=router_mode, capability=capability,
        max_cost_per_call=max_cost, temperature=temperature, max_tokens=max_tokens,
    )

    # Pre-check cost cap
    if max_cost is not None:
        est_tokens = max(1, len(prompt) // 4) + max_tokens
        max_model_cost = max((m.cost_input_per_1k for m in profiles), default=0.001)
        est_cost = est_tokens / 1000 * max_model_cost
        if est_cost > max_cost:
            raise HTTPException(402, {"error": f"Estimated cost ${est_cost:.4f} > max_cost_per_call ${max_cost:.4f}"})

    selected = selector.select(req)
    if not selected:
        raise HTTPException(503, {"error": "No available model"})

    # PII scan + redact
    pii_detected = False
    pii_flags = []
    prompt_log = prompt
    system_log = system
    try:
        det = PIIDetector()
        scanned = det.scan({"p": prompt, "s": system})
        if scanned:
            pii_detected = True
            pii_flags = list(scanned.keys())
            for k, v in scanned.items():
                if isinstance(v, str):
                    prompt_log = prompt_log.replace(v, "[REDACTED]")
                    system_log = system_log.replace(v, "[REDACTED]")
    except Exception:
        pass

    # Execute with fallback
    output_text = ""
    latency_ms = 0
    in_tok = max(1, len(prompt) // 4)
    out_tok = max_tokens // 2
    cost = 0.0
    success = False
    last_err = ""
    attempt = 1

    fallback = [selected] + selector.get_fallback_order(selected)
    tried = []

    for candidate in fallback:
        if candidate.name in tried:
            continue
        tried.append(candidate.name)
        adapter, model_id = _get_adapter(_registry, candidate.name)
        if not adapter:
            continue
        cb = _registry._breakers.get(candidate.name)
        if cb and cb.state() == "OPEN":
            last_err = f"Circuit breaker open for {candidate.name}"
            continue
        t0 = time.time()
        try:
            raw = adapter.call(prompt=prompt_log, model=model_id, system_prompt=system_log,
                               temperature=temperature, max_tokens=max_tokens)
            latency_ms = int((time.time() - t0) * 1000)
            if isinstance(raw, dict):
                choices = raw.get("choices", [{}])
                output_text = choices[0].get("message", {}).get("content", "")
                usage = raw.get("usage", {})
                in_tok = usage.get("prompt_tokens", in_tok)
                out_tok = usage.get("completion_tokens", out_tok)
            else:
                output_text = str(raw)
                out_tok = max(out_tok, len(output_text) // 4)
            cost = estimate_cost(candidate, in_tok, out_tok)
            success = True
            if cb:
                cb.record(success=True)
            break
        except Exception as e:
            last_err = str(e)
            latency_ms = int((time.time() - t0) * 1000)
            if cb:
                cb.record(success=False)

    audit = get_audit_logger()
    entry_id = ""

    if not success:
        if audit:
            entry_id = audit.log(action="model_execute", actor=ctx.user_id,
                                  org_id="default-org", outcome="error",
                                  metadata={"router_mode": router_mode.value,
                                            "capability": capability,
                                            "models_tried": tried,
                                            "last_error": last_err,
                                            "pii_detected": pii_detected})
        raise HTTPException(503, {"error": f"All models failed. Last: {last_err}"})

    if audit:
        entry_id = audit.log(action="model_execute", actor=ctx.user_id,
                              org_id="default-org", outcome="success",
                              metadata={"router_mode": router_mode.value,
                                        "capability": capability,
                                        "model_selected": selected.name,
                                        "model_id": model_id,
                                        "latency_ms": latency_ms,
                                        "cost_usd": cost,
                                        "input_tokens_est": in_tok,
                                        "output_tokens_est": out_tok,
                                        "pii_detected": pii_detected,
                                        "pii_flags": pii_flags,
                                        "attempt_number": attempt})

    return JSONResponse({
        "output": output_text,
        "model_id": model_id,
        "model_name": selected.name,
        "provider": selected.provider,
        "latency_ms": latency_ms,
        "cost": round(cost, 6),
        "router_mode": router_mode.value,
        "capability": capability,
        "pii_detected": pii_detected,
        "attempt_number": attempt,
        "audit_entry_id": str(entry_id) if entry_id else "",
    })


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
    <div class="nav-item" data-tab="compliance" onclick="showTab('compliance')"><span class="icon">🛡️</span><span>Compliance</span></div>
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
        <select id="mProv"><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option><option value="groq">Groq</option><option value="ollama">Ollama (local)</option></select>
      </div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Model ID</label><input id="mModelId" placeholder="e.g. gpt-4o"/></div>
      <div class="form-group"><label>API Key</label><input id="mApiKey" type="password" placeholder="sk-..."/></div>
    </div>
    <div class="form-row">
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
  else if(id==="execute")initExecute();else if(id==="audit")loadAudit(0);else if(id==="compliance")loadCompliance();else if(id==="deployments")loadDeployments();}
async function loadDashboard(){try{var s=await apiGet("/state");var b=await apiGet("/circuit-breakers");document.getElementById("s-total").textContent=s.total_entries||0;document.getElementById("s-today").textContent=s.entries_today||0;document.getElementById("s-models").textContent=s.models?s.models.length:0;document.getElementById("s-breakers").textContent=b.length;try{var v=await apiGet("/audit/verify");var ib=document.getElementById("integrityBadge");ib.className="badge "+(v.valid?"badge-green":"badge-red");ib.textContent=v.valid?"VERIFIED":"TAMPERED";}catch(e){}var mg=document.getElementById("mHealth");document.getElementById("noModels").style.display=(s.models&&s.models.length)?"none":"block";mg.innerHTML="";if(s.models){s.models.forEach(function(m){var n=esc(m.name).replace(/'/g,"\\'");mg.innerHTML+='<div class="model-card"><div class="name">'+esc(m.name)+'</div><div class="meta">'+esc(m.provider)+' / '+esc(m.model_id)+'</div><div class="meta">p50: '+(m.latency_ms_p50||"?")+'ms</div><div class="meta">'+esc(m.capability||"any")+'</div><div class="actions"><button class="btn btn-ghost btn-sm" onclick="testModel(\''+n+'\')">Test</button> <button class="btn btn-danger btn-sm" onclick="deleteModel(\''+n+'\')">Delete</button></div></div>';});}var ct=document.getElementById("cbTable");if(!b.length)ct.innerHTML='<div class="empty">No circuit breakers active.</div>';else{ct.innerHTML='<table><thead><tr><th>Name</th><th>State</th><th>Failures</th><th>Cooldown</th><th></th></tr></thead><tbody>'+b.map(function(x){var n=esc(x.name).replace(/'/g,"\\'");return'<tr><td class="mono">'+esc(x.name)+'</td><td><span class="cb-badge cb-'+(x.state||"CLOSED").toLowerCase().replace("-","")+'">'+esc(x.state||"CLOSED")+'</span></td><td>'+(x.failure_count||0)+'</td><td>'+(x.cooldown_seconds||60)+'s</td><td><button class="btn btn-ghost btn-sm" onclick="resetCb(\''+n+'\')">Reset</button></td></tr>';}).join("")+'</tbody></table>';}try{var logs=await get("/audit/logs?limit=10");var feed=document.getElementById("feed");feed.innerHTML=logs.length?logs.map(function(e){var cls=e.outcome==="success"?"badge-green":e.outcome==="failure"?"badge-red":"badge-yellow";return'<div class="feed-item"><span class="feed-time">'+fmtTs(e.timestamp)+'</span><span class="feed-action">'+esc(e.action)+'</span><span class="feed-actor">'+esc(e.actor||"")+'</span><span class="badge '+cls+'" style="font-size:11px">'+esc(e.outcome||"")+'</span></div>';}).join(""):'<div class="empty">No audit entries yet.</div>';}catch(e){document.getElementById("feed").innerHTML='<div class="empty">Could not load feed.</div>';}}catch(e){console.error(e);}apiGet("/api/version").then(function(v){document.getElementById("verInfo").textContent="v"+(v.modelfungible||"?")+" | Python "+(v.python||"?");}).catch(function(){document.getElementById("verInfo").textContent="ModelFungible Admin";});}
async function loadDeployments(){try{var s=await apiGet("/state");var t=document.getElementById("mTable");if(!s.models||!s.models.length){t.innerHTML='<div class="empty">No models. Click + Add Model.</div>';return;}t.innerHTML='<table><thead><tr><th>Name</th><th>Provider</th><th>Model ID</th><th>p50</th><th>Capability</th><th></th></tr></thead><tbody>'+s.models.map(function(m){var n=esc(m.name).replace(/'/g,"\\'");return'<tr><td class="mono">'+esc(m.name)+'</td><td>'+esc(m.provider)+'</td><td class="mono">'+esc(m.model_id)+'</td><td>'+(m.latency_ms_p50||"?")+'ms</td><td>'+esc(m.capability||"any")+'</td><td><button class="btn btn-danger btn-sm" onclick="deleteModel(\''+n+'\')">Delete</button></td></tr>';}).join("")+'</tbody></table>';}catch(e){document.getElementById("mTable").innerHTML='<div class="empty">'+esc(e.message)+'</div>';}}
function showAddForm(){document.getElementById("addForm").style.display="block";document.getElementById("addSuccess").style.display="none";document.getElementById("addErr").style.display="none";}
function hideAddForm(){document.getElementById("addForm").style.display="none";}
async function regModel(){var name=document.getElementById("mName").value.trim();var modelId=document.getElementById("mModelId").value.trim();if(!name||!modelId){var e=document.getElementById("addErr");e.textContent="Name and Model ID are required.";e.style.display="block";return;}try{await apiPost("/models/register",{name:name,provider:document.getElementById("mProv").value,model_id:modelId,api_key:document.getElementById("mApiKey").value,latency_ms_p50:parseInt(document.getElementById("mLat").value)||500,capability:document.getElementById("mCap").value});var s=document.getElementById("addSuccess");s.textContent="Model registered successfully.";s.style.display="block";document.getElementById("addErr").style.display="none";setTimeout(function(){hideAddForm();loadDeployments();loadDashboard();},1000);}catch(e){var err=document.getElementById("addErr");err.textContent="Error: "+e.message;err.style.display="block";}}
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
</script>
</body>
</html>
"""
@app.get("/admin", include_in_schema=False)
async def admin_ui():
    return HTMLResponse(content=HTML_UI, media_type="text/html")



@app.get("/admin", include_in_schema=False)
async def admin_ui():
    return HTMLResponse(content=HTML_UI, media_type="text/html")
