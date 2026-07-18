# ModelFungible Chat POC — AI Gateway Demo

A proof-of-concept chat application demonstrating the **ModelFungible AIP Gateway** in action. Shows streaming, model switching, cost tracking, guardrails, and fallback chains — all wired through a single SDK interface.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Your Browser                          │
│                  (chat_poc/templates/)                   │
└─────────────────────┬───────────────────────────────────┘
                      │ HTTP + SSE
┌─────────────────────▼───────────────────────────────────┐
│                  Flask :8766                             │
│            examples/chat_poc/app.py                      │
│                                                         │
│   Uses modelfungible.core.sdk.ModelFungible              │
│   (OpenAI-compatible drop-in client)                     │
└─────────────────────┬───────────────────────────────────┘
                      │ /chat (OpenAI-compatible API)
┌─────────────────────▼───────────────────────────────────┐
│         ModelFungible Admin Gateway :8765                │
│              enterprise/admin_app.py                     │
│                                                         │
│  ┌─────────────┐  ┌──────────┐  ┌────────────────┐     │
│  │ ModelRouter │→ │ Semantic │ → │  Compliance    │ → │
│  │ (mode-aware)│  │  Cache   │  │  Pre-check    │     │
│  └─────────────┘  └──────────┘  └────────────────┘     │
│        ↓                 ↓              ↓              │
│  ┌──────────────────────────────────────────────┐       │
│  │ CircuitBreaker + RetryWithBackoff + Fallback│       │
│  └──────────────────────────────────────────────┘       │
│                          ↓                              │
│  ┌────────────┐  ┌────────────┐  ┌────────────┐        │
│  │  Groq API  │  │ OpenAI API │  │Anthropic API│       │
│  └────────────┘  └────────────┘  └────────────┘        │
│                                                         │
│  Post-execution: Guardrails → Budget Alerts → Audit      │
└─────────────────────────────────────────────────────────┘
```

---

## Quick Start

**Prerequisites:** Python 3.10+, a Groq API key (free at [console.groq.com](https://console.groq.com))

```bash
cd examples/chat_poc

# 1. Set up environment
cp .env.example .env
# Edit .env — add your GROQ_API_KEY

# 2. Install dependencies
pip install flask python-dotenv

# 3. Run the chat app
python3 app.py

# 4. Open http://localhost:8766
```

**With the full gateway running:**
```bash
# Terminal 1 — start the gateway
python3 -m modelfungible.enterprise.admin_app

# Terminal 2 — start the chat app
cd examples/chat_poc
python3 app.py
# Open http://localhost:8766
```

---

## What It Demonstrates

### 🔀 Model Switching
Pick any model from the dropdown. The gateway routes the request to that model — **zero code changes in the chat app**. The `ModelFungible` client is a drop-in OpenAI replacement:

```python
from modelfungible.core.sdk import ModelFungible

client = ModelFungible(base_url="http://localhost:8765/api", api_key="dev-key")

# Same interface as OpenAI — but with gateway superpowers
stream = client.chat.completions.create(
    messages=[{"role": "user", "content": "Hello"}],
    model="groq/llama-3.3-70b-versatile",
    stream=True,
)
```

### 💰 Cost Tracking
Every response shows:
- **Cost per message** (from the gateway's audit log)
- **Running total** for the session
- **Latency** (time to first token + total)

### 🛡️ Guardrails
Set blocked terms via `MF_GUARDRAIL=secret,confidential` in `.env`. The gateway:
1. Applies them **before** the response reaches the browser
2. Shows a 🛡️ warning badge if content was filtered
3. Logs the guardrail event to the audit trail

### 🔄 Fallback Chains
If the primary model fails (rate limit, timeout), the gateway automatically tries the next model in the chain — without the chat app knowing.

Configure in the gateway's Admin UI → Deployments tab.

### ⚡ Streaming
Server-Sent Events (SSE) streaming — tokens appear word-by-word as the model generates them, using the standard OpenAI streaming interface.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MF_BASE_URL` | `http://localhost:8765/api` | Gateway URL. Empty = use Groq directly |
| `MF_API_KEY` | `dev-key` | Gateway auth key |
| `MF_MODEL` | `groq/llama-3.3-70b-versatile` | Default model |
| `MF_MODE` | `balanced` | Router mode: fastest / cheapest / balanced |
| `MF_GUARDRAIL` | _(none)_ | Comma-separated blocked terms |
| `MF_MAX_LEN` | `4000` | Max output chars (0 = unlimited) |
| `GROQ_API_KEY` | — | Required if MF_BASE_URL is empty |

---

## File Structure

```
examples/chat_poc/
├── app.py              ← Flask app (gateway client + SSE chat)
├── templates/
│   └── index.html      ← Dark-themed chat UI
├── .env.example        ← Environment config template
└── README.md           ← This file
```

---

## Extending

**Add a new model:** Register it in the Admin UI → Deployments tab. The chat app will immediately see it in the dropdown.

**Custom guardrail terms:** Update `MF_GUARDRAIL` in `.env` or configure per-request in `app.py`'s `output_filter`.

**Change routing mode:** Set `MF_MODE` to `fastest`, `cheapest`, or `balanced`. The gateway re-ranks models on every request.
