# ModelFungible Examples

Domain-agnostic strategy examples. These are NOT trading strategies.

## Strategies

| Strategy | Domain | Description |
|----------|--------|-------------|
| `contract_risk.json` | Legal | Score contracts for legal risk flags |
| `clinical_notes.json` | Healthcare | Review clinical notes, suggest CPT codes |
| `resume_screening.json` | HR | Score resumes against job requirements |

## Facts (Context Data)

| File | Domain | Description |
|------|--------|-------------|
| `facts/legal_context.json` | Legal | MSA contracts for review |
| `facts/healthcare_context.json` | Healthcare | Clinical notes batch |

## Usage

```python
from modelfungible import ContextBuilder, RulesEngine, ModelExecutor

# Load a non-trading strategy
engine = RulesEngine("examples/strategies/contract_risk.json")

# Build legal context
cb = ContextBuilder(facts_file="examples/facts/legal_context.json")
ctx = cb.build(role="analyst")

# Execute with any model
executor = ModelExecutor()
executor.add_model("fast", "groq", "llama-3.3-70b-versatile", api_key="...")
result = executor.run(
    prompt=cb.build_prompt(ctx, "contract_risk", engine.get("contract_risk")),
    model="fast"
)

# Validate output
errors = engine.validate_output("contract_risk", dict(result))
assert errors == [], f"Invalid: {errors}"
```
