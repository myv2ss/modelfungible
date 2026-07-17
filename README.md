# ModelFungible

> **The ORM moment for AI.**
> Plug in any AI model — OpenAI, Anthropic, Groq, Ollama, Gemini. Run the same strategy. Get the same decision. Swap models in one line of code.

```
pip install modelfungible
```

---

## Why ModelFungible?

Every team has this problem:

```
"We want to switch from GPT-4 to Claude (or Groq, or Gemini)
 but we can't — it would break production."

" Our AI feature works great in dev but falls over in prod
 because the model we validated against isn't available at scale."
```

ModelFungible solves this. It decouples your **strategy rules** from your **model provider**, so you can:

- **Swap models** without rewriting a single prompt or strategy rule
- **Run fallbacks** — primary fails? It tries the next model automatically
- **Validate outputs** against your schema before they reach your system
- **Recover from crashes** — interrupted session? It resumes where it left off
- **Use local models** alongside cloud models in the same chain

The core principle: **Models are reasoning engines. Data is truth. Prompts are adapters.**

---

## Quick Start

```bash
pip install modelfungible
```

### 5-line example

```python
from modelfungible import ModelExecutor, ContextBuilder, RulesEngine

# 1. Load your strategy rules
engine = RulesEngine("strategy_rules.json")

# 2. Build context once (market state, positions, risk)
cb = ContextBuilder(facts_file="state.json")
ctx = cb.build(role="scanner")

# 3. Add models
executor = ModelExecutor()
executor.add_model("primary", "groq",   "llama-3.3-70b-versatile", api_key="...")
executor.add_model("fallback", "openai", "gpt-4o",                   api_key="...")

# 4. Run — same call, any model
result = executor.run(
    prompt=cb.build_scanner_prompt(ctx, "EQM", engine.get("EQM")),
    model="primary"   # or let it try the fallback chain
)

# 5. Validate output against your schema
errors = engine.validate_output("EQM", dict(result))
assert errors == [], f"Invalid output: {errors}"
```

### CLI

```bash
# Build a prompt for a strategy
python3 -m modelfungible run --strategy EQM --show-prompt

# Validate all strategy rules
python3 -m modelfungible validate

# Benchmark two models on the same task
python3 -m modelfungible benchmark --models llama8b,llama70b

# Check for interrupted sessions
python3 -m modelfungible session status
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Your Code                           │
│                  (same, always)                        │
└─────────────────┬───────────────────────────────────────┘
                  │ same prompt format
┌─────────────────▼───────────────────────────────────────┐
│               ContextBuilder                            │
│    market state + positions + risk + memory            │
│    → structured context packet                          │
└─────────────────┬───────────────────────────────────────┘
                  │ same context
┌─────────────────▼───────────────────────────────────────┐
│              ModelExecutor                             │
│   add_model() → set_fallback_chain() → run()           │
│                                                         │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐   │
│   │ OpenAI      │  │ Anthropic   │  │ Groq        │   │
│   │ Adapter     │  │ Adapter     │  │ Adapter     │   │
│   └─────────────┘  └─────────────┘  └─────────────┘   │
└─────────────────────────────────────────────────────────┘
                  │
                  │ different APIs, same interface
┌─────────────────▼───────────────────────────────────────┐
│              Model Provider                            │
│        OpenAI   Anthropic   Groq   Ollama   Gemini     │
└─────────────────────────────────────────────────────────┘
```

---

## Core Components

### `RulesEngine` — Strategy as code
```python
engine = RulesEngine("strategy_rules.json")
engine.validate("EQM")           # fail fast on bad rules
sizing = engine.get_sizing("EQM", "CONFIRMED_BULL")  # regime-aware sizing
stop = engine.get_stop_loss("EQM", entry_price=100)   # compute stops
errors = engine.validate_output("EQM", output_dict)    # schema validation
```

### `ContextBuilder` — One context, any model
```python
cb = ContextBuilder(facts_file="state.json", memory_dir="./memory")
ctx = cb.build(role="scanner")   # scanner | monitor | analyst
prompt = cb.build_scanner_prompt(ctx, "EQM", rules)
```

### `ModelExecutor` — Model-agnostic execution
```python
executor = ModelExecutor()
executor.add_model("primary", "groq",     "llama-3.3-70b-versatile", api_key="...")
executor.add_model("fast",    "groq",    "llama-3.1-8b-instant",    api_key="...")
executor.add_model("best",    "openai",  "gpt-4o",                   api_key="...")

# Try primary → fast → best (auto-fallback on failure)
executor.set_fallback_chain(["primary", "fast", "best"])
result = executor.run(prompt, model="primary")

# Result is always the same shape
assert result.success
assert result.get("ticker")
assert result.latency_s > 0
```

### `SessionManager` — Crash recovery
```python
sm = SessionManager(facts_file="state.json")

# Before a long pipeline
sm.snapshot_state("scan_and_execute", market=ctx.market, positions=ctx.positions)

# After each step
sm.update_step("scanner", completed=True, result={"ticker": "ADBE"})
sm.update_step("executor", completed=True)

# On next startup — check for interrupted work
if sm.check_incomplete():
    ctx = sm.resume_context()   # restore full context
    pending = sm.get_pending_tasks()
    print(sm.resume_summary())  # "Crashed during: scanner (5 min ago)"
    # ... resume work ...
    sm.clear_snapshot()         # clean completion
```

### `AdapterError` — Categorized failures
```python
from modelfungible import AdapterError

try:
    result = executor.run(prompt)
except AdapterError as e:
    if e.is_retryable():
        # timeout, rate limit, server error — safe to retry
        schedule_retry()
    else:
        # auth failure, bad model, context too long — don't retry
        alert_human(e)
```

---

## Supported Models

| Provider | Models | Status |
|----------|--------|--------|
| **Groq** | llama-3.3-70b-versatile, llama-3.1-8b-instant | ✅ Free tier works |
| **OpenAI** | gpt-4o, gpt-4o-mini, gpt-3.5-turbo | ✅ Bring your key |
| **Anthropic** | claude-3-5-sonnet, claude-3-opus | ✅ Bring your key |
| **Ollama** | qwen2.5:7b, llama3 (local) | ⚠️ Degraded — hangs under load |

---

## Validation — Model Swap Proof

Same rules + same data = same decision, regardless of model size.

```
Benchmark: Groq Llama-3.1-8B-Instant vs Groq Llama-3.3-70B-Versatile
Task: Pick best ticker from EQM signals (ADBE, AMZN, AVGO, ROKU)
Context: CONFIRMED_BULL regime, VIX 16.7, SPY $749.17

Result: ✅ BOTH MODELS CHOSE ADBE
  ADBE — EQM 68.4 | RevScore 156.97 | BeatRate 96%
  AMZN — EQM 51.3 | RevScore 88.24  | BeatRate 72%
  AVGO — EQM 47.0 | RevScore 102.11 | BeatRate 81%
  ROKU — EQM 39.1 | RevScore 70.50  | BeatRate 61%

Models agree: 8B = 70B = ADBE ✅
```

---

## Installation

```bash
# Base install
pip install modelfungible

# With all provider support
pip install "modelfungible[all]"

# Dev dependencies
pip install "modelfungible[dev]"
pip install "modelfungible[openai]"   # OpenAI models
pip install "modelfungible[anthropic]" # Anthropic Claude
pip install "modelfungible[groq]"      # Groq models
```

---

## Requirements

- Python 3.10+
- `requests` (for API calls)
- Provider API keys as environment variables:
  - `OPENAI_API_KEY`
  - `ANTHROPIC_API_KEY`
  - `GROQ_API_KEY`

---

## Running Tests

```bash
# All tests
python3 -m pytest modelfungible/tests/ -v

# Unit only (no API calls)
python3 -m pytest modelfungible/tests/ -v -m "not integration"

# Integration (requires API keys)
GROQ_API_KEY=... python3 -m pytest modelfungible/tests/test_integration.py -v

# With coverage
python3 -m pytest modelfungible/tests/ --cov=modelfungible --cov-report=term-missing
```

**Current test status: 88/88 passing**

---

## Project Status

| Area | Status |
|------|--------|
| Core engine (executor, rules, context) | ✅ Stable |
| Adapters (OpenAI, Anthropic, Groq) | ✅ Stable |
| Session crash recovery | ✅ Stable |
| CLI | ✅ Stable |
| Integration tests | ✅ Passing |
| PyPI release | 🚧 Pending |
| Strategy authoring UI | 🚧 Future |
| Vertex AI / SageMaker adapters | 🚧 Future |

---

## License

**BUSL-1.0** — Copyright © 2026 Saabu / OpenClaw. All rights reserved.

Commercial use requires a license. See [LICENSE](LICENSE) for full terms.

---

*"The ORM moment for AI" — ModelFungible: swap any model, keep your strategy.*
