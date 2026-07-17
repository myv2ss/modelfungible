# Contributing to ModelFungible

Thank you for your interest in contributing!

## Development Setup

```bash
# Clone the repo
git clone https://github.com/myv2ss/modelfungible
cd modelfungible

# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run tests
python3 -m pytest modelfungible/tests/ -v

# Run with coverage
python3 -m pytest modelfungible/tests/ --cov=modelfungible --cov-report=term-missing
```

## Adding a New Adapter

1. Create `modelfungible/adapters/<provider>.py`
2. Inherit from `BaseAdapter`
3. Implement `def _build_payload(self, prompt, **kwargs) -> dict` and `def _parse_response(self, data) -> dict`
4. Add tests in `tests/test_model_executor.py` (look for `TestAdapters`)
5. Add to `ModelExecutor._default_adapters()` in `core/executor.py`

```python
from modelfungible.adapters.base import BaseAdapter, AdapterError, ParsedOutput

class MyAdapter(BaseAdapter):
    provider_name = "myprovider"

    def _build_payload(self, prompt, temperature=0.1, max_tokens=2000, **kwargs):
        return {
            "model": self.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

    def _parse_response(self, data) -> ParsedOutput:
        raw = json.dumps(data)
        content = data["choices"][0]["message"]["content"]
        parsed = parse_json_output(content)   # from base
        return ParsedOutput(parsed, raw=raw)
```

## Adding a New Strategy Rule

1. Add the strategy block to `strategy_rules.json`
2. Run `python3 -m modelfungible validate` to check for schema errors
3. Add test cases in `tests/test_rules_engine.py`

## Code Style

- Python 3.10+ features (type hints, structural pattern matching)
- Docstrings: Google style
- Error messages: human-readable, actionable

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(executor): add streaming support
fix(adapter): correct header for Anthropic API
docs(readme): add quick start section
test(integration): add real Groq benchmark
```

## Pull Request Process

1. Fork the repo and create a feature branch
2. Add tests for any new behavior
3. Ensure all 88 tests pass (`pytest tests/`)
4. Update CHANGELOG.md if adding a notable feature
5. Open a PR with a clear description

## Reporting Bugs

Please include:
- Python version
- ModelFungible version (`python3 -m modelfungible --version`)
- Provider (OpenAI / Anthropic / Groq)
- Full error traceback
- Minimal reproduction case

## suggesting Features

Open an issue with:
- Problem you're solving
- Proposed solution
- Alternative solutions considered
