# ModelFungible — Model-Agnostic AI Execution Layer
#
# Core principle: "Models are reasoning engines. Data is truth."
# Same strategy_rules.json + same facts.json → same decisions on any model.
#
# Usage:
#     from modelfungible import Executor, ContextBuilder, RulesEngine
#
#     rules = RulesEngine("strategy_rules.json")
#     ctx = ContextBuilder().build(role="scanner")
#     result = Executor().run(strategy="EQM", context=ctx, rules=rules)
