#!/usr/bin/env python3
# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
ModelFungible Enterprise — Admin CLI

Usage:
    modelfungible-admin license install MODEL-XXXX-...
    modelfungible-admin license status
    modelfungible-admin license generate --customer cust_xyz --expiry 2027-12-31
    modelfungible-admin model list
    modelfungible-admin strategy list
    modelfungible-admin strategy validate my_strategy.json
    modelfungible-admin status
"""
import argparse
import json
import sys
from pathlib import Path

# Allow running as module or script
try:
    from modelfungible.enterprise.license import LicenseKey, LicenseGenerator
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from enterprise.license import LicenseKey, LicenseGenerator


# ─────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────
LICENSE_SECRET = os.environ.get("MODELFUNGIBLE_LICENSE_SECRET", "")


# ─────────────────────────────────────────────────────────────────
# Commands: License
# ─────────────────────────────────────────────────────────────────
def cmd_license_install(args):
    key = args.key
    if not key:
        print("Error: --key is required", file=sys.stderr)
        return 1

    result = LicenseKey.validate(key, LICENSE_SECRET)
    if not result["valid"]:
        print(f"❌ Invalid license: {result['error']}", file=sys.stderr)
        return 1

    # Save
    path = Path(args.path) if args.path else LicenseKey.default_path()
    LicenseKey.save_license(key, str(path))
    print(f"✅ License installed: {result['plan']}")
    print(f"   Customer: {result['customer_id']}")
    print(f"   Expires: {result['expiry']}")
    print(f"   Seats:   {result['seats']}")
    print(f"   Features: {', '.join(result['features'])}")
    print(f"   Saved to: {path}")
    return 0


def cmd_license_status(args):
    try:
        path = Path(args.path) if args.path else LicenseKey.default_path()
        key = LicenseKey.load_license(str(path))
    except FileNotFoundError:
        print("No license found. Run: modelfungible-admin license install <KEY>")
        return 1
    except Exception as e:
        print(f"Error loading license: {e}", file=sys.stderr)
        return 1

    result = LicenseKey.validate(key, LICENSE_SECRET)
    if result["valid"]:
        print(f"✅ Licensed: {result['plan']}")
        print(f"   Customer:  {result['customer_id']}")
        print(f"   Expires:  {result['expiry']}")
        print(f"   Seats:    {result['seats']}")
        print(f"   Features: {', '.join(result['features'])}")
    else:
        print(f"❌ {result['error']}", file=sys.stderr)
        return 1
    return 0


def cmd_license_generate(args):
    if not LICENSE_SECRET:
        print("Error: MODELFUNGIBLE_LICENSE_SECRET env var not set", file=sys.stderr)
        print("Cannot generate keys without the server secret.", file=sys.stderr)
        return 1

    gen = LicenseGenerator(LICENSE_SECRET)
    key = gen.generate(
        customer_id=args.customer,
        expiry=args.expiry,
        seats=args.seats,
        features=args.features.split(",") if args.features else None,
        plan=args.plan,
    )
    print(key)
    return 0


# ─────────────────────────────────────────────────────────────────
# Commands: Models
# ─────────────────────────────────────────────────────────────────
def cmd_model_list(args):
    from modelfungible.core.executor import ModelExecutor

    executor = ModelExecutor()
    models = executor.list_models()
    if not models:
        print("No models registered.")
        return 0

    print(f"{'Name':<20} {'Provider':<15} {'Model ID'}")
    print("-" * 60)
    for m in models:
        print(f"{m['name']:<20} {m['provider']:<15} {m['model_id']}")
    return 0


# ─────────────────────────────────────────────────────────────────
# Commands: Strategy
# ─────────────────────────────────────────────────────────────────
def cmd_strategy_list(args):
    from modelfungible.core.rules_engine import RulesEngine

    rules_path = args.rules or "/root/.openclaw/workspace/trading-desk/data/strategy_rules.json"
    if not Path(rules_path).exists():
        print(f"Strategy rules not found: {rules_path}", file=sys.stderr)
        return 1

    engine = RulesEngine(rules_path)
    strategies = engine.list_strategies()
    print(f"{len(strategies)} strategies:")
    for s in strategies:
        print(f"  - {s}")
    return 0


def cmd_strategy_validate(args):
    from modelfungible.core.rules_engine import RulesEngine, StrategyValidationError

    rules_path = args.rules or "/root/.openclaw/workspace/trading-desk/data/strategy_rules.json"
    if not Path(rules_path).exists():
        print(f"Strategy rules not found: {rules_path}", file=sys.stderr)
        return 1

    engine = RulesEngine(rules_path)
    errors = engine.validate(args.strategy)
    if errors:
        print(f"❌ {args.strategy}: {errors}")
        return 1
    else:
        print(f"✅ {args.strategy}: valid")
        sizing = engine.get_sizing(args.strategy, "CONFIRMED_BULL")
        print(f"   Sizing (BULL): ${sizing.get('amount', 'N/A')}")
        return 0


# ─────────────────────────────────────────────────────────────────
# Commands: Status
# ─────────────────────────────────────────────────────────────────
def cmd_status(args):
    from datetime import datetime

    print("ModelFungible Enterprise — Status")
    print(f"Time: {datetime.utcnow().isoformat()}Z")
    print()

    # License
    try:
        key = LicenseKey.load_license()
        result = LicenseKey.validate(key, LICENSE_SECRET)
        if result["valid"]:
            print(f"✅ License: {result['plan']} ({result['customer_id']}) — {result['expiry']}")
        else:
            print(f"❌ License: {result['error']}")
    except FileNotFoundError:
        print("❌ License: not installed")
    except Exception as e:
        print(f"❌ License: error — {e}")

    # Config
    config_path = Path.home() / ".modelfungible"
    print(f"\nConfig dir: {config_path}")
    if config_path.exists():
        for f in config_path.iterdir():
            print(f"  {f.name}")
    return 0


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        prog="modelfungible-admin",
        description="ModelFungible Enterprise Admin CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # license
    p_lic = sub.add_parser("license", help="License management")
    lic_sub = p_lic.add_subparsers(dest="subcommand", required=True)

    install = lic_sub.add_parser("install", help="Install a license key")
    install.add_argument("key", help="License key (MODEL-...)")
    install.add_argument("--path", help="Path to save license file")
    install.set_defaults(func=cmd_license_install)

    status = lic_sub.add_parser("status", help="Show license status")
    status.add_argument("--path", help="Path to license file")
    status.set_defaults(func=cmd_license_status)

    gen = lic_sub.add_parser("generate", help="Generate a license key (server-side)")
    gen.add_argument("--customer", required=True, help="Customer ID")
    gen.add_argument("--expiry", required=True, help="Expiry date (YYYY-MM-DD)")
    gen.add_argument("--seats", type=int, default=10, help="Number of seats")
    gen.add_argument("--features", default="", help="Comma-separated features")
    gen.add_argument("--plan", default="enterprise", help="Plan name")
    gen.set_defaults(func=cmd_license_generate)

    # model
    p_model = sub.add_parser("model", help="Model management")
    model_sub = p_model.add_subparsers(dest="subcommand", required=True)
    list_models = model_sub.add_parser("list", help="List registered models")
    list_models.set_defaults(func=cmd_model_list)

    # strategy
    p_strat = sub.add_parser("strategy", help="Strategy management")
    strat_sub = p_strat.add_subparsers(dest="subcommand", required=True)
    list_strat = strat_sub.add_parser("list", help="List strategies")
    list_strat.add_argument("--rules", help="Path to strategy rules JSON")
    list_strat.set_defaults(func=cmd_strategy_list)

    validate_strat = strat_sub.add_parser("validate", help="Validate a strategy")
    validate_strat.add_argument("strategy", help="Strategy ID")
    validate_strat.add_argument("--rules", help="Path to strategy rules JSON")
    validate_strat.set_defaults(func=cmd_strategy_validate)

    # status
    p_stat = sub.add_parser("status", help="Show system status")
    p_stat.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    import os
    sys.exit(main())
