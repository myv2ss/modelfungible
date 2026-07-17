# Changelog

All notable changes to ModelFungible will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**License:** BUSL-1.0 — Commercial use requires a license. See LICENSE for full terms.

## [0.1.0] — 2026-07-17

### Added

- **ModelFungible core package** — model-agnostic AI execution layer
- `RulesEngine` — load, validate, and execute strategy rules with regime-aware sizing
- `ContextBuilder` — builds structured context packets from market state, positions, risk flags, and memory
- `ModelExecutor` — model-agnostic executor with fallback chains and error classification
- `SessionManager` — crash recovery with snapshot/update/resume/clear workflow
- `ParsedOutput` — dict subclass carrying raw text and usage metadata
- `AdapterError` — categorized errors (timeout, rate_limit, auth, model_not_found, context_length, server_error)
- `parse_json_output` — extract JSON from raw model output with think-block stripping
- CLI with `run`, `validate`, `benchmark`, and `session` commands

### Adapters

- `OpenAIAdapter` — OpenAI API (any OpenAI-compatible endpoint)
- `AnthropicAdapter` — Anthropic Claude API
- `GroqAdapter` — Groq API (inherits OpenAI, free tier works)

### Tests

- 88/88 tests passing (82 unit + 6 integration)
- Integration tests validate real Groq API calls
- Benchmark tests confirm model interchangeability (8B = 70B on identical inputs)

### Files

```
modelfungible/
├── adapters/
│   ├── base.py          # BaseAdapter, AdapterError, ParsedOutput, parse_json_output
│   ├── openai.py        # OpenAI adapter
│   ├── anthropic.py     # Anthropic adapter
│   └── groq.py          # Groq adapter
├── core/
│   ├── rules_engine.py  # Strategy rules: load/validate/size/exit/schema
│   ├── context_builder.py # Context: market/positions/risk/memory
│   ├── executor.py      # ModelExecutor + ExecutionResult + fallback chains
│   └── session_manager.py # Crash recovery: snapshot/update/resume/clear
├── tests/
│   ├── test_rules_engine.py
│   ├── test_context_builder.py
│   ├── test_session_manager.py
│   ├── test_model_executor.py
│   └── test_integration.py
├── cli.py
├── pyproject.toml
├── README.md
└── LICENSE
```

---

## [0.0.0] — 2026-07-16

### Added

- Initial design — `MODEL_AGNOSTIC_ARCHITECTURE.md` research document
- Proof of concept: Phase 3 benchmark validating model swap equivalence
