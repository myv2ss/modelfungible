# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.
# Commercial use requires a license. Unauthorized use is prohibited.

#!/usr/bin/env python3
"""
ModelFungible CLI — Command-line interface

Usage:
    python3 -m modelfungible run --strategy EQM --model groq-llama8b
    python3 -m modelfungible benchmark --strategies EQM PEAD-3
    python3 -m modelfungible validate --rules strategy_rules.json
    python3 -m modelfungible session status
    python3 -m modelfungible session clear
"""
import argparse, json, sys, os
from pathlib import Path

# Add package to path
sys.path.insert(0, str(Path(__file__).parent))

from modelfungible.core.rules_engine import RulesEngine, StrategyValidationError
from modelfungible.core.context_builder import ContextBuilder
from modelfungible.core.session_manager import SessionManager
from modelfungible.core.executor import ModelExecutor


# ─────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────
def get_default_paths():
    td = Path(os.environ.get("TD", "/root/.openclaw/workspace/trading-desk"))
    return {
        "rules":  td / "data" / "strategy_rules.json",
        "facts":  td / "data" / "trading_desk_state.json",
        "memory": td.parent / "memory",
    }


# ─────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────
def cmd_run(args):
    paths = get_default_paths()

    # Load rules
    try:
        engine = RulesEngine(args.rules or paths["rules"])
        print(f"Loaded {len(engine.list_strategies())} strategies from {args.rules}")
    except Exception as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        return 1

    # Load context
    cb = ContextBuilder(
        facts_file=args.facts or paths["facts"],
        memory_dir=args.memory or paths["memory"],
    )
    ctx = cb.build(role=args.role)

    print(f"Context: {ctx.market_summary()}")
    print(f"Positions: {len(ctx.positions)} open")

    if args.strategy:
        try:
            rules = engine.get(args.strategy)
            prompt = cb.build_scanner_prompt(ctx, args.strategy, rules)
            print(f"\nPrompt built for strategy: {args.strategy}")
            if args.show_prompt:
                print("\n" + "=" * 60)
                print(prompt)
                print("=" * 60)
        except StrategyValidationError as e:
            print(f"Strategy error: {e}", file=sys.stderr)
            return 1

    return 0


def cmd_validate(args):
    try:
        engine = RulesEngine(args.rules)
        strategies = args.strategies or engine.list_strategies()

        all_ok = True
        for s in strategies:
            try:
                errors = engine.validate(s)
                if not errors:
                    print(f"  ✅ {s}")
                else:
                    print(f"  ❌ {s}: {errors}")
                    all_ok = False
            except StrategyValidationError as e:
                print(f"  ❌ {s}: {e}")
                all_ok = False

        return 0 if all_ok else 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_session(args):
    sm = SessionManager(
        facts_file=args.facts or get_default_paths()["facts"],
    )

    if args.action == "status":
        inc = sm.check_incomplete()
        if inc:
            print("INCOMPLETE SESSION:")
            print(sm.resume_summary())
        else:
            print("No incomplete sessions.")

    elif args.action == "clear":
        sm.clear_snapshot()
        print("Snapshot cleared.")

    elif args.action == "list":
        inc = sm.check_incomplete()
        if inc:
            print("Pending:", sm.get_pending_tasks())
            print("Completed:", [c["task"] for c in sm.get_completed_tasks()])
        else:
            print("No session.")

    return 0


def cmd_benchmark(args):
    """Run model benchmark on a strategy using Groq."""
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        print("GROQ_API_KEY not set. Benchmark requires Groq free tier.", file=sys.stderr)
        return 1

    from modelfungible.core.executor import ModelExecutor

    executor = ModelExecutor()
    executor.add_model("llama8b", "groq", "llama-3.1-8b-instant", api_key=key)
    executor.add_model("llama70b", "groq", "llama-3.3-70b-versatile", api_key=key)

    # Simple benchmark prompt
    prompt = args.prompt or "Pick one: AAPL or MSFT. Return JSON: {\"ticker\": \"TICKER\"}"
    print(f"Benchmarking: {args.models}")
    print(f"Prompt: {prompt[:80]}...")
    print()

    results = {}
    for model_name in args.models.split(","):
        model_name = model_name.strip()
        print(f"  Running {model_name}...", end=" ", flush=True)
        result = executor.run(prompt=prompt, model=model_name, max_tokens=100)
        ticker = result.get("ticker", "ERROR")
        print(f"→ {ticker} ({result.latency_s:.2f}s)")
        results[model_name] = {"ticker": ticker, "latency": result.latency_s}

    agree = len(set(r.get("ticker") for r in results.values())) == 1
    print()
    print(f"Agreement: {'✅ YES' if agree else '❌ NO'}")
    return 0


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="ModelFungible CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    p_run = sub.add_parser("run", help="Build context and prompt for a strategy")
    p_run.add_argument("--strategy", help="Strategy ID (e.g. EQM)")
    p_run.add_argument("--role", default="scanner", help="Role (scanner/monitor/analyst)")
    p_run.add_argument("--rules", help="Path to strategy_rules.json")
    p_run.add_argument("--facts", help="Path to facts.json")
    p_run.add_argument("--memory", help="Path to memory directory")
    p_run.add_argument("--show-prompt", action="store_true", help="Print the full prompt")
    p_run.set_defaults(func=cmd_run)

    # validate
    p_val = sub.add_parser("validate", help="Validate strategy rules")
    p_val.add_argument("--rules", help="Path to strategy_rules.json")
    p_val.add_argument("--strategies", nargs="+", help="Specific strategies to validate")
    p_val.set_defaults(func=cmd_validate)

    # session
    p_sess = sub.add_parser("session", help="Manage sessions")
    p_sess.add_argument("action", choices=["status", "clear", "list"])
    p_sess.add_argument("--facts", help="Path to facts.json")
    p_sess.set_defaults(func=cmd_session)

    # benchmark
    p_bench = sub.add_parser("benchmark", help="Benchmark models on a task")
    p_bench.add_argument("--models", default="llama8b,llama70b",
                         help="Comma-separated model names")
    p_bench.add_argument("--prompt", help="Prompt to benchmark with")
    p_bench.set_defaults(func=cmd_benchmark)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
