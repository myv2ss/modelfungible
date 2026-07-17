# Universal LLM Proxy — Specification

## Vision

ModelFungible as a universal LLM proxy: any application (IDE, chat surface, automation tool) makes a single HTTP call through ModelFungible to reach any AI model. Compliance, routing, and cost management are built-in — not bolted on.

Core principle: **Any app → ModelFungible → Any AI Model**

## Architecture

### Core Endpoint

```
POST /api/execute
Headers:
  X-Auth-Token: <session_token>
  Content-Type: application/json

Body:
{
  "prompt": "string",                    # required
  "system": "string",                    # optional, default provided
  "model": "string",                     # optional, auto-selected if absent
  "mode": "balanced|fastest|cheapest|capability",  # router mode
  "capability": "code|vision|fast|precise|any",     # for capability routing
  "max_cost_per_call": 0.05,           # optional, reject if exceeded
  "temperature": 0.7,                   # optional
  "max_tokens": 1024,                   # optional
  "metadata": {}                        # optional, stored in audit
}

Response:
{
  "output": "...",                      # model response text
  "model_id": "claude-3.5-sonnet",     # actual model used
  "latency_ms": 412,                   # round-trip ms
  "cost": 0.00123,                     # calculated cost in USD
  "router_mode": "cheapest",           # how model was selected
  "cached": false,                      # if a cached response was used
  "audit_entry_id": "entry_001",       # for compliance reference
}
```

## Model Selector

### Modes

**`fastest`** — Routes to model with lowest recent latency (p50 from health checks).

**`cheapest`** — Routes to model with lowest `cost_per_1k_tokens`. Falls back to next cheapest if primary fails.

**`balanced`** — Weighted score:
```
score = (latency_weight * normalized_speed) + (cost_weight * normalized_cost)
default: latency_weight=0.4, cost_weight=0.6
```

**`capability`** — Matches request capability tag to model capability:
- `code` → models tagged `code`
- `vision` → models tagged `vision`
- `fast` → models tagged `fast`
- `precise` → models tagged `precise`
Falls back to untagged models if none match.

### Health-Aware Fallback

If primary model fails (circuit breaker open, timeout, error), router automatically tries next best model. All attempts logged with `attempt_number` in audit.

### Cost Cap

If `max_cost_per_call` is set and estimated cost exceeds it, request is rejected before any API call is made. Returns `402 Payload Too Costly`.

## Cost Tracking

### Per-Model Cost Database

```json
{
  "openai/gpt-4o": {"input": 0.0025, "output": 0.01, "currency": "USD"},
  "anthropic/claude-3.5-sonnet": {"input": 0.003, "output": 0.015, "currency": "USD"},
  "groq/llama-3.1-8b-instant": {"input": 0.00005, "output": 0.00005, "currency": "USD"}
}
```

### Cost Calculation

```
cost = (input_tokens / 1000) * input_cost_per_1k + (output_tokens / 1000) * output_cost_per_1k
```

### Usage Records (audit/metering)

Every `/api/execute` call creates a usage record:
```json
{
  "timestamp": "2026-07-17T22:15:00Z",
  "org_id": "default-org",
  "user_id": "analyst1",
  "model_id": "claude-3.5-sonnet",
  "input_tokens": 320,
  "output_tokens": 180,
  "cost_usd": 0.00378,
  "latency_ms": 412,
  "router_mode": "balanced",
  "capability": "precise"
}
```

### Cost Limits (Future / Enterprise)

- Per-user daily cost limit → reject with 402
- Per-org monthly budget → reject with 402
- Alert thresholds (80% of budget → log warning)

## Compliance

### PII Scan

Before logging, the prompt is scanned for PII. If detected:
- PII is redacted from stored prompt
- `pii_detected: true` flag set in audit entry
- Original (redacted) prompt still logged

### Audit Entry

Every execute call logs:
```json
{
  "entry_id": "entry_001",
  "sequence": 1,
  "timestamp": "2026-07-17T22:15:00Z",
  "action": "model_execute",
  "actor": "analyst1",
  "org_id": "default-org",
  "model_id": "claude-3.5-sonnet",
  "outcome": "success",
  "pii_detected": false,
  "pii_redacted_fields": [],
  "metadata": {
    "router_mode": "balanced",
    "capability": "precise",
    "latency_ms": 412,
    "cost_usd": 0.00378,
    "input_tokens": 320,
    "output_tokens": 180,
    "cached": false
  },
  "hash": "sha256(previous + this entry)"
}
```

## SDK Interface

```python
from modelfungible import ModelFungible

# Initialize once
mf = ModelFungible(api_key="YOUR_TOKEN", base_url="https://api.company.com")

# Simple call
response = mf.execute(
    prompt="Review this function for bugs",
    system="You are a code reviewer.",
    capability="code",
    mode="balanced"
)

print(response.output)
print(f"Cost: ${response.cost:.6f} | Model: {response.model_id} | Latency: {response.latency_ms}ms")

# Explicit model
response = mf.execute(
    prompt="...",
    model="claude-production"  # specific model
)
```

## Registering Model Costs

```bash
POST /api/models/register
{
  "name": "claude-production",
  "provider": "anthropic",
  "model_id": "claude-3.5-sonnet",
  "api_key": "sk-ant-...",
  "latency_ms_p50": 500,
  "capability": "precise",
  "cost_input_per_1k": 0.003,
  "cost_output_per_1k": 0.015
}
```

## Response Codes

| Code | Meaning |
|------|---------|
| 200 | Success |
| 400 | Bad request (missing prompt) |
| 401 | Not authenticated |
| 402 | Cost limit exceeded (max_cost_per_call or budget) |
| 403 | Insufficient permissions |
| 422 | Validation error |
| 503 | All models failed (circuit breakers open) |
| 504 | Timeout after all retries |

## Model Health

Health scores are maintained per model based on recent calls:
- Success rate (last 100 calls)
- Average latency (last 100 calls)
- Circuit breaker state

Router uses health score to weight selections.

## Implementation Phases

### Phase 1 (This Build)
- [x] `POST /api/execute` endpoint
- [x] Smart model selector (4 modes)
- [x] Cost calculation on every call
- [x] PII scan before logging
- [x] Full audit entry
- [x] Circuit breaker integration
- [x] Cost limits (max_cost_per_call)
- [ ] SDK (`pip install modelfungible` → `mf.execute()`)

### Phase 2
- [ ] Cost dashboard in Admin UI (spend by model, user, day)
- [ ] Per-user / per-org cost limits
- [ ] Budget alerts
- [ ] Usage API: `GET /api/usage?period=day&by=model`

### Phase 3
- [ ] Response caching (semantic dedup)
- [ ] Prompt template library
- [ ] Streaming responses
