# ModelFungible Enterprise — User Guide

> **Who is this for?** This guide covers two user roles:
> - **Administrators** — IT/operations staff who deploy the system, manage users, and configure integrations
> - **Regular Users** — Traders, analysts, and operators who interact with the AI model system day-to-day

---

## Table of Contents

1. [What is ModelFungible?](#1-what-is-modelfungible)
2. [Quick Start (5 minutes)](#2-quick-start)
3. [Admin Guide](#3-admin-guide)
4. [Regular User Guide](#4-regular-user-guide)
5. [API Reference](#5-api-reference)
6. [Security & Compliance](#6-security--compliance)
7. [Troubleshooting](#7-troubleshooting)

---

## 1. What is ModelFungible?

ModelFungible is a universal AI execution layer that lets you run AI-powered workflows with any LLM provider — OpenAI, Anthropic, Groq, Ollama, Google Vertex AI, or your own models — without changing your application logic.

**Core concepts:**

| Concept | Description |
|---------|-------------|
| **Strategy** | A JSON file describing your workflow — entry triggers, rules, sizing, exit conditions |
| **Model** | An AI provider endpoint (GPT-4o, Claude, Llama, etc.) |
| **Executor** | Runs a strategy against a model — handles retries, circuit breaking, fallbacks |
| **Audit Log** | Every call is logged with actor, timestamp, outcome, and tamper-evident hash chain |
| **License** | BUSL-1.0 commercial license with seat management |

**Typical enterprise use cases:**
- Automated trading signal generation with multi-model validation
- Contract risk analysis with LLM evaluation
- Clinical note processing (HIPAA-compliant)
- Resume screening with audit trails for HR compliance

---

## 2. Quick Start

### Prerequisites

```bash
# Python 3.9+
python3 --version

# Install ModelFungible
pip install modelfungible[all]

# Or for core only (no FastAPI admin UI)
pip install modelfungible
```

### Run the Admin UI

```bash
# Start the web admin interface
python3 -m modelfungible.enterprise.admin_app

# Opens at http://localhost:8000/admin
```

**Default login:** `admin` / `changeme`

### Run a Strategy via Python

```python
from modelfungible.core import ModelExecutor, RulesEngine

# Load your strategy
engine = RulesEngine("examples/strategies/contract_risk.json")

# Set up a model (Groq free tier — no API key needed for basic use)
from modelfungible.adapters.groq import GroqAdapter
model = GroqAdapter(model_id="llama-3.1-8b-instant", api_key="")  # empty = free tier

# Run
executor = ModelExecutor()
result = executor.run(model=model, rules=engine, context={"contract_text": "..."})
print(result.output)
```

---

## 3. Admin Guide

> **Role required:** Administrator

### 3.1 First-Time Setup

**Step 1 — Change the admin password immediately**

After first login, create a strong password for the `admin` account:

```bash
# Via environment variable (recommended for automation)
export MODELFUNGIBLE_ADMIN_PASSWORD="YourSecurePassword123!"
```

Or use the Admin UI: Navigate to **Compliance** tab → User Management → update `admin` password.

**Step 2 — Configure your license**

```bash
# Install your license key
export MODELFUNGIBLE_LICENSE_KEY="MODEL-xxxxxxxxxxxx.your_signature_here"
export MODELFUNGIBLE_LICENSE_SECRET="your_secret_key"
```

Or upload via Admin UI: **Compliance** tab → License Status.

**Step 3 — Register your AI models**

Go to **Deployments** tab → **+ Add Model**:

| Field | Description | Example |
|-------|-------------|---------|
| Name | Internal identifier | `claude-production` |
| Provider | API provider | `anthropic`, `openai`, `groq`, `ollama` |
| Model ID | Model name on provider | `claude-3.5-sonnet`, `gpt-4o` |
| API Key | Provider API key | `sk-ant-...` |
| p50 Latency | Expected latency in ms | `500` |
| Capability | Use case tag | `precise`, `fast`, `code`, `vision` |

**Step 4 — Set up audit retention policy**

```bash
# Choose your regulation
export MODELFUNGIBLE_RETENTION_POLICY="gdpr"    # 30 days
export MODELFUNGIBLE_RETENTION_POLICY="hipaa"   # 6 years
export MODELFUNGIBLE_RETENTION_POLICY="finra"   # 6 years
export MODELFUNGIBLE_RETENTION_POLICY="sec"     # 5 years
```

### 3.2 User Management

**Create a new user:**

1. Log in as **admin**
2. Go to **Compliance** tab → **Users** section
3. Click **Add User**
4. Fill in: User ID, Name, Role, Password

**Roles explained:**

| Role | Description |
|------|-------------|
| `admin` | Full access — users, models, settings, all audit logs |
| `trader` | Can view dashboard, strategies, audit logs, run strategies |
| `viewer` | Read-only access — dashboard and audit logs only |

**Best practice:** Give each person their own account. Never share admin credentials.

**Remove a user:**

1. Go to **Compliance** → **Users**
2. Click **Delete** next to the user
3. Confirm — this immediately revokes their session

### 3.3 Circuit Breakers

Circuit breakers protect the system from cascading failures. If a model provider is slow or returning errors, the breaker trips and subsequent calls automatically fail-fast instead of waiting.

**When a breaker trips:**
- The model shows as **Circuit Open** in the Dashboard
- Calls to that model are rejected immediately (no API cost)
- Other models in the fallback chain are tried instead (if configured)

**Manually reset a breaker:**

1. Go to **Dashboard** → **Circuit Breakers**
2. Click **Reset** next to the affected model

**Tuning thresholds:**

Breakers are configured per-model when registered. Default: 5 failures within 60 seconds trips the breaker.

```python
# In code — override breaker settings
model = GroqAdapter(model_id="llama-3.1-8b-instant")
model.circuit_breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=120)
```

### 3.4 Audit Log Management

**View audit logs:**

1. Go to **Audit Logs** tab
2. Filter by date range, actor (user), action type, or outcome
3. Click **Export CSV** or **Export JSON** for compliance records

**Verify audit integrity:**

Click **Verify Chain** — this checks the SHA-256 hash chain for tampering. If the chain is valid, the audit log has not been modified since recording.

> ⚠️ **Audit logs are append-only.** Records cannot be edited or deleted. Retention policies enforce automatic cleanup after the configured period.

### 3.5 Deployment Configuration

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `MODELFUNGIBLE_ADMIN_PASSWORD` | `changeme` | Admin login password |
| `MODELFUNGIBLE_LICENSE_KEY` | — | License key for commercial use |
| `MODELFUNGIBLE_LICENSE_SECRET` | — | License validation secret |
| `MODELFUNGIBLE_RETENTION_POLICY` | `default` | `gdpr`, `hipaa`, `finra`, `sec`, `soc2`, `pci_dss`, `default` |
| `MODELFUNGIBLE_RETENTION_DAYS` | `90` | Override retention days (overrides policy default) |
| `MODELFUNGIBLE_AUDIT_DIR` | `/tmp/modelfungible_audit` | Directory for audit log files |
| `MODELFUNGIBLE_RULES_PATH` | `examples/strategies` | Path to strategy JSON files |
| `MODELFUNGIBLE_USERS` | — | JSON array of user accounts (see below) |

**Programmatic user configuration:**

```bash
export MODELFUNGIBLE_USERS='[
  {"user_id": "vika", "name": "Vikas Singhvi", "password": "SecurePass!@#", "role": "admin"},
  {"user_id": "analyst1", "name": "Analyst One", "password": "AnalystPass!@#", "role": "trader"},
  {"user_id": "viewer1", "name": "Viewer", "password": "ViewerPass!@#", "role": "viewer"}
]'
```

**Production deployment (recommended):**

```bash
# Use a reverse proxy (nginx/Caddy) in front of the admin app
# Enable HTTPS (required for production)
# Set a strong admin password before exposing to network

# Run as a service (systemd)
sudo tee /etc/systemd/system/modelfungible-admin.service > /dev/null <<EOF
[Unit]
Description=ModelFungible Enterprise Admin
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/modelfungible
ExecStart=/usr/bin/python3 -m modelfungible.enterprise.admin_app
Restart=on-failure
Environment=MODELFUNGIBLE_ADMIN_PASSWORD=YourSecurePassword
Environment=MODELFUNGIBLE_LICENSE_KEY=MODEL-xxxx.yyy

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable modelfungible-admin
sudo systemctl start modelfungible-admin
```

---

## 4. Regular User Guide

> **Role required:** `trader` or `viewer`

### 4.1 Logging In

1. Open the Admin UI URL provided by your administrator (e.g., `https://ai.yourcompany.com/admin`)
2. Enter your **User ID** and **Password**
3. Click **Sign In**
4. Your session lasts 12 hours before requiring re-login

> 💡 If you forget your password, contact your administrator to reset it.

### 4.2 Dashboard

The Dashboard is your home screen. It shows:

| Widget | What it shows |
|--------|---------------|
| **Total Entries** | All AI calls made through the system |
| **Today's Entries** | Calls made today |
| **Models** | Number of registered AI models and their health status |
| **Circuit Breakers** | Number of active breakers (high number may indicate issues) |
| **Model Health** | Live status of each registered model |
| **Recent Activity** | Last 10 audit log entries |

### 4.3 Running a Strategy

Strategies are pre-configured workflows. To run one:

1. Go to **Strategies** tab
2. Find your strategy (use the search box to filter)
3. Click the strategy name to view its configuration
4. Copy the JSON and use the API to execute:

```bash
curl -X POST http://localhost:8000/api/execute \
  -H "Content-Type: application/json" \
  -H "X-Auth-Token: YOUR_SESSION_TOKEN" \
  -d '{
    "strategy_id": "contract_risk",
    "model": "claude-production",
    "context": {
      "contract_text": "Party A agrees to deliver 1000 units by December 31..."
    }
  }'
```

Or in Python:

```python
from modelfungible.core import ModelExecutor, RulesEngine

engine = RulesEngine("examples/strategies/contract_risk.json")
executor = ModelExecutor()

result = executor.run(
    model=my_model,
    rules=engine,
    context={"contract_text": "Party A agrees to..."}
)

print(result.output)        # The model's response
print(result.metrics)       # Latency, tokens, cost
print(result.audit_entry_id) # For compliance records
```

### 4.4 Viewing Audit Logs

Your audit trail is automatically recorded for every action. To view your activity:

1. Go to **Audit Logs** tab
2. Filter by **Actor** = your user ID to see only your activity
3. Filter by date range for a specific time period
4. Click any entry to see full details (action, model used, outcome, latency)

> 💡 **Why is this important?** Audit logs provide proof of every AI decision for compliance, debugging, and accountability. You cannot edit or delete these records.

### 4.5 Validating a Custom Strategy

If your administrator has given you permission to author strategies:

1. Go to **Strategies** tab
2. Scroll to **Validate Custom Strategy**
3. Paste your strategy JSON in the editor
4. Click **Validate** — errors will be shown inline
5. If valid, share the JSON file with your administrator to deploy

**Strategy JSON structure:**

```json
{
  "strategy_id": "my_strategy",
  "name": "My Custom Strategy",
  "domain": "legal",
  "entry_trigger": {
    "type": "manual",
    "description": "Run when user submits a document for review"
  },
  "rules": {
    "instruction": "You are a legal document reviewer. Analyze the following...",
    "output_schema": {
      "risk_level": "string",
      "flags": "array",
      "summary": "string"
    }
  },
  "sizing": {
    "amount": 1000,
    "max_positions": 5
  }
}
```

---

## 5. API Reference

### Authentication

All API calls (except `/api/auth/login`) require the `X-Auth-Token` header:

```bash
curl -H "X-Auth-Token: YOUR_SESSION_TOKEN" \
  http://localhost:8000/api/state
```

To get a session token:

```bash
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"user_id": "analyst1", "password": "YourPassword"}'

# Response:
# {"session_id": "abc123...", "user_id": "analyst1", "name": "Analyst One", "role": "trader", "expires_at": "2026-07-18T06:00:00"}
```

### Key Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `POST` | `/api/auth/login` | None | Get session token |
| `POST` | `/api/auth/logout` | Any | Destroy session |
| `GET` | `/api/auth/me` | Any | Current user info |
| `GET` | `/api/state` | Any | System state, models, audit stats |
| `GET` | `/api/strategies` | Trader+ | List available strategies |
| `GET` | `/api/strategies/:id` | Trader+ | Get strategy JSON |
| `POST` | `/api/strategies/validate` | Trader+ | Validate strategy JSON |
| `GET` | `/api/audit/logs` | Any | Query audit logs |
| `GET` | `/api/audit/verify` | Admin | Verify hash chain integrity |
| `GET` | `/api/compliance/retention` | Any | Current retention policy |
| `GET` | `/api/compliance/license` | Admin | License status |
| `GET` | `/api/audit/export/json` | Admin | Export all logs as JSON |
| `GET` | `/api/audit/export/csv` | Admin | Export all logs as CSV |

### Query Parameters for `/api/audit/logs`

| Parameter | Type | Description |
|-----------|------|-------------|
| `start_date` | ISO date | Filter entries from this date |
| `end_date` | ISO date | Filter entries up to this date |
| `actor` | string | Filter by user ID |
| `action` | string | Filter by action type (e.g. `model_execute`, `strategy_run`) |
| `outcome` | string | `success`, `failure`, `error` |
| `limit` | int | Max entries to return (default 100, max 10000) |
| `offset` | int | Pagination offset |

Example:
```bash
curl "http://localhost:8000/api/audit/logs?actor=analyst1&outcome=success&limit=50" \
  -H "X-Auth-Token: YOUR_TOKEN"
```

---

## 6. Security & Compliance

### How Audit Logs Work

Every API call is logged to an append-only JSONL file with:
- Sequential entry ID
- SHA-256 hash of the previous entry (hash chain — tamper-evident)
- Actor (user ID), action, outcome, model used, latency
- Automatic PII detection and redaction before storage

**To verify integrity:**
```bash
# Via API
curl http://localhost:8000/api/audit/verify \
  -H "X-Auth-Token: YOUR_TOKEN"

# Response: {"verified": true}  or  {"verified": false, "error": "..."}
```

### Data Retention

| Regulation | Retention | Use Case |
|------------|-----------|----------|
| `gdpr` | 30 days | EU personal data |
| `hipaa` | 6 years | US healthcare |
| `finra` | 6 years | US broker-dealers |
| `sec` | 5 years | Investment advisors |
| `soc2` | 1 year | SOC 2 Type II |
| `pci_dss` | 1 year | Payment card data |
| `default` | 90 days | General use |

Records older than the retention period are automatically deleted.

### PII Detection

ModelFungible automatically detects and redacts PII before logging:

| Type | Example |
|------|---------|
| Email | `john@example.com` |
| Phone | `(555) 123-4567` |
| SSN | `123-45-6789` |
| Credit Card | `4111 1111 1111 1111` |
| IP Address | `192.168.1.1` |
| Passport | `P<USA<1234567<<` |

When PII is detected, the `pii_detected` flag is set to `true` in the audit entry, but the actual PII value is replaced with `[REDACTED]`.

---

## 7. Troubleshooting

### Can't log in

1.
**"Invalid user_id or password"**
- Check your User ID — it's case-sensitive
- Contact your administrator to reset your password
- Default admin: `admin` / `changeme` (change this immediately in production)

**"Session expired — please login again"**
- Your 12-hour session has expired
- Log out and log back in at the login screen
- If this happens repeatedly, check the server time (clock skew can cause early expiry)

---

### Circuit breaker keeps tripping

1. Go to **Dashboard** → check which model is affected
2. Check the model's health status (may be a provider outage)
3. Wait for the cooldown period (default: 60 seconds) and click **Reset**
4. If it keeps tripping, contact your administrator — the model's failure threshold may be too aggressive

---

### API returns 403 Forbidden

You don't have permission for that action:

| Your role | What you can't do |
|-----------|-------------------|
| `viewer` | Register models, manage users, reset breakers, validate strategies |
| `trader` | Manage users, register/delete models, reset breakers |
| `admin` | Everything |

Contact your administrator if you need elevated access.

---

### Audit log verify shows "tampered"

**Do not ignore this.**

The hash chain has been broken — some audit entries have been modified or deleted since recording. This is a serious compliance issue.

1. Do not delete or modify any audit files
2. Contact your security/compliance team immediately
3. Preserve the audit directory for investigation: `/tmp/modelfungible_audit` (or your configured path)

---

### Model registration fails

**"Model already registered"**
- A model with that name already exists
- Use a different name, or delete the existing one first

**"Connection failed"**
- Check the API key is correct
- Check the model ID matches the provider's expected format
- Check network connectivity to the model's API endpoint

---

### High latency or timeouts

1. Check **Dashboard** → model health — is the breaker open?
2. Check the provider's status page (OpenAI, Anthropic, Groq, etc.)
3. Try a different model — the fallback chain should handle this automatically
4. If latency is consistently high, lower the `p50_latency` value you registered to better reflect actual performance

---

### Installation issues

**"ModuleNotFoundError: No module named 'fastapi'"**
```bash
pip install modelfungible[all]   # includes FastAPI and uvicorn
```

**"Permission denied" when running as a service**
```bash
# Create a dedicated user (don't run as root)
sudo useradd -r -s /bin/false modelfungible
sudo chown -R modelfungible:modelfungible /opt/modelfungible
```

**Python version error**
ModelFungible requires Python 3.9 or later:
```bash
python3 --version  # must be 3.9+
```

---

### Getting Help

| Need | Where to go |
|------|-------------|
| Admin password reset | IT Administrator |
| New user account | IT Administrator |
| License issues | Your ModelFungible vendor |
| Bug report / feature request | Open an issue on GitHub |
| Strategy authoring help | Contact your administrator |

---

*ModelFungible Enterprise — BUSL-1.0 License*
*Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.*
