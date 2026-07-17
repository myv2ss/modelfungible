# ModelFungible Enterprise — Architecture Design

**Status:** Draft  
**Version:** 0.1.0  
**Date:** 2026-07-17

---

## 1. Vision

ModelFungible Enterprise is a **dual-mode product**:

1. **Self-Hosted (Enterprise):** Customers deploy ModelFungible inside their own infrastructure (on-prem, VPC, cloud). Pay a license key. Full data isolation.

2. **Managed API (SaaS):** Customers call `api.modelfungible.ai` with an API key. Pay per-call. Zero infrastructure required.

Both share the same core engine (`modelfungible/`), the same strategy rules, and the same concepts — so a strategy developed locally runs identically in the cloud.

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CLIENTS                                  │
│   Enterprise (self-hosted)        SaaS (API consumers)           │
│   Python SDK + Admin CLI          REST API + SDK                 │
└────────────────┬────────────────────────────────────────────────┘
                 │
        ┌────────▼────────────────────────────────────────┐
        │              MODELFUNGIBLE CORE                   │
        │                                                  │
        │  ┌─────────────┐   ┌──────────────────────────┐ │
        │  │ RulesEngine │   │   ContextBuilder        │ │
        │  │ (strategies │   │   (market + positions    │ │
        │  │  as JSON)   │   │    + risk + memory)     │ │
        │  └─────────────┘   └──────────────────────────┘ │
        │                                                  │
        │  ┌──────────────────────────────────────────┐  │
        │  │           ModelExecutor                   │  │
        │  │  fallback chains, error classification,  │  │
        │  │  output validation                       │  │
        │  └──────────────────────────────────────────┘  │
        │                                                  │
        │  ┌──────────────┐   ┌──────────────────────┐  │
        │  │ SessionMgr   │   │  LicenseManager      │  │
        │  │ (crash recov)│   │  (key validation)    │  │
        │  └──────────────┘   └──────────────────────┘  │
        └──────────────────────────────────────────────────┘
                 │                                    │
    ┌────────────▼────────┐           ┌──────────────▼──────────┐
    │   SELF-HOSTED        │           │   MANAGED SaaS API      │
    │   ENTERPRISE          │           │                         │
    │                       │           │  FastAPI server          │
    │  Enterprise adapters  │           │  (uvicorn, async)       │
    │  - Vertex AI          │           │                          │
    │  - SageMaker          │           │  API Key auth           │
    │  - Azure OpenAI       │           │  Per-key rate limits    │
    │  - Ollama (local)    │           │  Usage tracking          │
    │                       │           │  Stripe billing         │
    │  License key (offline│           │  Multi-tenant isolation  │
    │  validation)          │           │  Webhooks               │
    │                       │           │  Streaming (SSE)       │
    │  Admin CLI           │           │  Redis (cache, queue)   │
    │  Strategy UI (local) │           │  Postgres (metadata)    │
    │                       │           │                         │
    │                       │           │  SaaS adapters (same    │
    │                       │           │  interface, cloud creds) │
    └───────────────────────┘           └─────────────────────────┘
```

---

## 3. Directory Structure

```
modelfungible/
    # Core — shared by both
    adapters/           ← base + OpenAI + Anthropic + Groq
    core/               ← rules_engine, context_builder, executor, session_manager
    enterprise/         ← NEW: self-hosted specific
        adapters/       ← Vertex AI, SageMaker, Azure, Ollama
        license.py      ← License key validation (offline)
        admin_cli.py    ← Enterprise admin CLI
        ui/             ← Strategy authoring UI (streamlit or flask)
        sdk.py          ← Python SDK for self-hosted
    cloud/              ← NEW: managed API specific
        server/         ← FastAPI app
        api/            ← Routes: /v1/execute, /v1/strategies, /v1/keys, /v1/usage
        auth/           ← API key + OAuth2
        billing/        ← Stripe integration
        middleware/      ← Rate limiting, tenant isolation
        workers/        ← Background job processing
    tests/              ← 88 existing tests
    tests_enterprise/   ← NEW: enterprise adapter tests
    tests_cloud/        ← NEW: API integration tests
    pyproject.toml
    README.md
```

---

## 4. Phase 1: Self-Hosted Enterprise

### 4.1 License Key System

**How it works:**
```
Enterprise buys license → receives KEY-XXXX-XXXX-XXXX-XXXX
→ Installs ModelFungible → enters key → offline validation
→ Key contains: expiry, seats, features, signature
→ Validates locally (no phone home required for basic check)
→ Weekly optional ping to check revocation list
```

**Key format:** `MODEL-XXXX-XXXX-XXXX-XXXX`  
**Validation:** RSA-2048 signature of `{customer_id, expiry, features}`  
**Storage:** `~/.modelfungible/license.json`

**Implementation:**
- `enterprise/license.py` — LicenseKey class with `validate()`, `is_expired()`, `get_features()`
- `core/executor.py` — check license before every run
- `admin_cli.py` — `modelfungible-admin license install KEY`

### 4.2 Enterprise Adapters

| Adapter | Provider | Priority |
|---------|----------|----------|
| VertexAIAdapter | Google Vertex AI | P1 |
| SageMakerAdapter | AWS SageMaker | P1 |
| AzureAdapter | Azure OpenAI | P1 |
| OllamaAdapter | Ollama (local) | P1 |
| BedrockAdapter | AWS Bedrock | P2 |
| SagemakerEndpointAdapter | AWS SM endpoints | P2 |

**Adapter interface (same as base):**
```python
class VertexAIAdapter(BaseAdapter):
    provider_name = "vertexai"
    
    def _build_payload(self, prompt, **kwargs) -> dict:
        # Vertex AI uses specific payload format
        return {
            "instances": [{"prompt": prompt}],
            "parameters": {"temperature": kwargs.get("temperature", 0.1)},
        }
    
    def _parse_response(self, data) -> ParsedOutput:
        raw = json.dumps(data)
        content = data["predictions"][0]["content"]
        parsed = parse_json_output(content)
        return ParsedOutput(parsed, raw=raw)
```

### 4.3 Strategy Authoring UI

**Options:** Streamlit (fastest) vs Flask + React

**Core features:**
- Create / edit / delete strategies (JSON editor with validation)
- Backtest a strategy against historical data
- Live test against a single ticker
- Regime simulator (what-if: what if VIX was 30?)
- Strategy gallery (import/export JSON)

**Implementation:** Streamlit app in `enterprise/ui/`  
**Why Streamlit:** 200 lines vs 2000 for Flask, good enough for internal tools

### 4.4 Python SDK for Self-Hosted

```python
from modelfungible.enterprise import EnterpriseClient

client = EnterpriseClient(
    license_key="MODEL-XXXX-XXXX-XXXX-XXXX",
    endpoint="https://your-deployment.internal/v1",
)

# Same interface as core — but with enterprise adapters
result = client.execute(
    strategy="EQM",
    context=context,
    model="vertexai/claude-3-5-sonnet",
)
```

### 4.5 Admin CLI

```bash
# Install / validate license
modelfungible-admin license install MODEL-XXXX-XXXX-XXXX-XXXX
modelfungible-admin license status

# Manage strategies
modelfungible-admin strategy list
modelfungible-admin strategy validate my_strategy.json
modelfungible-admin strategy backtest --strategy EQM --start 2024-01-01

# Monitor
modelfungible-admin status
modelfungible-admin logs --tail 100
```

---

## 5. Phase 2: Managed SaaS API

### 5.1 API Design

```
Base URL: https://api.modelfungible.ai/v1

Authentication: Bearer API key (X-MF-Key header)

Endpoints:
  POST /execute          Execute a strategy with a model
  GET  /strategies       List available strategies
  GET  /strategies/{id}  Get strategy definition
  POST /strategies       Create a strategy
  
  # API Keys
  POST /keys             Create API key
  GET  /keys             List your keys
  DELETE /keys/{id}      Revoke key
  
  # Usage
  GET  /usage            Usage summary
  GET  /usage/daily      Daily breakdown
  
  # Webhooks
  POST /webhooks         Register webhook
  GET  /webhooks         List webhooks
  
  # Account
  GET  /account          Account info
  PUT  /account          Update account
```

### 5.2 Request/Response Shapes

**POST /execute**
```json
// Request
{
  "strategy": "EQM",
  "context": {
    "market": { "regime": "CONFIRMED_BULL" },
    "positions": []
  },
  "model": "groq/llama-3.3-70b-versatile",
  "temperature": 0.1,
  "max_tokens": 2000
}

// Response 200
{
  "request_id": "req_abc123",
  "result": {
    "ticker": "ADBE",
    "direction": "LONG",
    "size": 4500,
    "stop": 891.50,
    "target": 1034.00,
    "reason": "..."
  },
  "model": "groq/llama-3.3-70b-versatile",
  "latency_ms": 1340,
  "usage": { "prompt_tokens": 1200, "completion_tokens": 180 },
  "cached": false,
  "created_at": "2026-07-17T14:30:00Z"
}

// Response 429 (rate limit)
{
  "error": "rate_limit_exceeded",
  "message": "Rate limit reached. Upgrade plan or wait 60s.",
  "limit": 100,
  "remaining": 0,
  "reset_at": "2026-07-17T14:31:00Z"
}

// Response 402 (quota)
{
  "error": "quota_exceeded",
  "message": "Monthly quota exhausted. Upgrade plan."
}
```

### 5.3 Multi-Tenant Isolation

**Architecture:**
- Each tenant has a `tenant_id` (UUID)
- All DB queries scoped by `tenant_id`
- Redis keys namespaced: `tenant:{id}:...`
- ModelExecutor runs in isolated process per tenant (via worker)
- No shared mutable state between tenants

**Implementation:**
```python
# Every DB query includes tenant_id
async def get_tenant_usage(tenant_id: str, period: str) -> dict:
    result = await db.execute(
        "SELECT * FROM usage WHERE tenant_id = $1 AND period = $2",
        tenant_id, period
    )
    return result

# Middleware injects tenant_id
@app.middleware
async def tenant_scope(request: Request, call_next):
    key = request.headers.get("X-MF-Key")
    tenant = await auth.get_tenant(key)
    request.state.tenant_id = tenant.id
    response = await call_next(request)
    return response
```

### 5.4 Rate Limiting

| Plan | Requests/min | Requests/month | Price |
|------|-------------|---------------|-------|
| Free | 10 | 1,000 | $0 |
| Starter | 60 | 50,000 | $49/mo |
| Pro | 300 | 500,000 | $199/mo |
| Enterprise | 1000+ | Unlimited | Custom |

**Implementation:**
- Redis sliding window counter per tenant per endpoint
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` headers on every response
- 429 response when exceeded

### 5.5 Stripe Billing

**Subscriptions:**
- Free tier: no Stripe needed
- Paid tiers: Stripe Subscription (monthly/annual)
- API key created on successful subscription
- Webhook: `customer.subscription.updated` → update tenant plan
- Webhook: `customer.subscription.deleted` → downgrade to free

**Usage-based (optional):**
- Track API calls per tenant in `usage` table
- Stripe Metered Billing: report usage at end of billing period
- Alternative: fixed tiers (simpler)

### 5.6 Webhooks

```python
# Tenant registers webhook
POST /webhooks
{
  "url": "https://your-app.com/webhooks/modelfungible",
  "events": ["execution.completed", "execution.failed", "quota.warning"]
}

# When an execution completes
POST https://your-app.com/webhooks/modelfungible
{
  "event": "execution.completed",
  "request_id": "req_abc123",
  "tenant_id": "tenant_xyz",
  "result": { "ticker": "ADBE", ... },
  "timestamp": "2026-07-17T14:30:00Z"
}
```

### 5.7 Streaming (SSE)

```python
# For long-running executions
POST /execute/stream
{
  "strategy": "EQM",
  "context": {...},
  "model": "groq/llama-3.3-70b-versatile"
}

# Response: Server-Sent Events
event: start
data: {"request_id": "req_abc123"}

event: model_start
data: {"model": "groq/llama-3.3-70b-versatile"}

event: token
data: {"delta": "ADBE"}

event: complete
data: {"result": {"ticker": "ADBE", ...}, "latency_ms": 1340}
```

---

## 6. Shared Components

### 6.1 Core Engine (unchanged)

The `modelfungible/` core remains identical whether running:
- Locally (self-hosted enterprise)
- In a SaaS worker (managed API)

This is the key guarantee: **same strategy, same data, same model → same decision**.

### 6.2 Strategy Registry

Both self-hosted and SaaS share the same strategy JSON format:
```json
{
  "strategy_id": "EQM",
  "name": "Earnings Quality Momentum",
  "description": "...",
  "entry_trigger": "EQM_score >= 60 AND RevScore >= 100",
  "sizing": { ... },
  "exit": [...],
  "signal_output_schema": { ... }
}
```

Self-hosted: strategies stored in `/etc/modelfungible/strategies/`  
SaaS: strategies stored in Postgres per tenant

### 6.3 Usage Tracking (shared)

Both modes record execution logs:
```json
{
  "request_id": "req_abc123",
  "tenant_id": "tenant_xyz",
  "strategy": "EQM",
  "model": "groq/llama-3.3-70b-versatile",
  "latency_ms": 1340,
  "success": true,
  "input_tokens": 1200,
  "output_tokens": 180,
  "cost_cents": 0.24,
  "created_at": "2026-07-17T14:30:00Z"
}
```

Self-hosted: logged to local file + optional upstream  
SaaS: logged to Postgres + Redis

---

## 7. Implementation Phases

### Phase 1 (This Session): Self-Hosted Foundation
- [ ] `enterprise/license.py` — license key validation
- [ ] `enterprise/adapters/` — Vertex AI + Ollama adapters
- [ ] `enterprise/sdk.py` — Python SDK wrapper
- [ ] `enterprise/admin_cli.py` — admin CLI commands
- [ ] Enterprise tests
- [ ] Unit tests for all new components

### Phase 2: Self-Hosted UI
- [ ] Strategy Authoring UI (Streamlit)
- [ ] Backtester UI
- [ ] Dashboard (usage, license status)

### Phase 3: Managed SaaS API Foundation
- [ ] FastAPI server skeleton
- [ ] API key auth middleware
- [ ] `/execute` endpoint
- [ ] Tenant isolation middleware
- [ ] Redis rate limiting

### Phase 4: SaaS Billing
- [ ] Stripe integration
- [ ] Subscription webhooks
- [ ] Usage tracking
- [ ] Plan enforcement

### Phase 5: SaaS Advanced
- [ ] Webhooks
- [ ] Streaming (SSE)
- [ ] OAuth2 SSO (Google, GitHub)
- [ ] OpenAPI spec

---

## 8. Open Questions

1. **Database for SaaS:** PostgreSQL (Supabase?) or PlanetScale/Neon?
2. **Infra for SaaS:** Self-hosted (VPS) or cloud (Railway, Render, Fly.io)?
3. **Stripe billing:** Subscription tiers or usage-based or hybrid?
4. **Strategy storage in SaaS:** Tenant-owned or curated shared gallery?
5. **Self-hosted key validation:** Phone-home required or fully offline?
6. **LLM cost in SaaS:** Pass-through pricing or markup?

---

*Last updated: 2026-07-17*
