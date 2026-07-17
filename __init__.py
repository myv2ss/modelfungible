# ModelFungible — Model-Agnostic AI Execution Layer
#
# Core principle: "Models are reasoning engines. Data is truth."
# Same strategy_rules.json + same facts.json → same decisions on any model.
#
# Usage:
#     from modelfungible import ModelExecutor, ContextBuilder, RulesEngine
#
#     rules = RulesEngine("strategy_rules.json")
#     ctx = ContextBuilder().build(role="scanner")
#     result = ModelExecutor().run(strategy="EQM", context=ctx, rules=rules)
#
# Cost & latency routing:
#     from modelfungible import CostRouter, ModelProfile, HealthChecker
#
#     router = CostRouter(profiles={
#         "fast": ModelProfile(name="fast", provider="groq", model_id="llama-3.1-8b", latency_ms=200),
#         "precise": ModelProfile(name="precise", provider="anthropic", model_id="claude-3-5-sonnet", latency_ms=2000),
#     })
#     model = router.route(mode="fastest")
#     router.record(model.name, latency_ms=180, success=True)
