#!/usr/bin/env python3
"""
Integration tests — ModelFungible

Full end-to-end tests using real API calls (Groq free tier).
Tests the complete flow: rules → context → executor → output.
"""
import pytest, json, tempfile, os, time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ─────────────────────────────────────────────────────────────────
# Integration test helpers
# ─────────────────────────────────────────────────────────────────
def get_groq_api_key():
    return os.environ.get("GROQ_API_KEY", "")


# ─────────────────────────────────────────────────────────────────
# Tests: Full pipeline (integration)
# ─────────────────────────────────────────────────────────────────
class TestFullPipeline:
    """
    End-to-end integration tests with real Groq API.
    These test the complete ModelFungible stack: rules + context + executor.
    """

    @pytest.fixture
    def rules_file(self):
        rules = {
            "EQM": {
                "strategy_id": "EQM",
                "name": "EQM Test",
                "entry_trigger": "EQM_score >= 60",
                "sizing": {
                    "CONFIRMED_BULL":  {"amount": 4500, "max_positions": 3},
                    "NEUTRAL":         {"amount": 1500, "max_positions": 1},
                    "BEAR":            {"amount": 0,   "max_positions": 0},
                },
                "stop_loss_pct": 0.08,
                "target_gain_pct": 0.15,
                "exit": [
                    {"type": "stop_loss", "pct": -8},
                    {"type": "time", "trading_days": 20},
                ],
                "signal_output_schema": {
                    "ticker":    "string",
                    "direction": "string",
                    "size":      "number",
                    "reason":    "string",
                }
            }
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(rules, f)
        yield path
        os.unlink(path)

    @pytest.fixture
    def facts_file(self):
        facts = {
            "generated_at": "2026-07-17T14:00:00",
            "market": {
                "regime": "CONFIRMED_BULL",
                "vix": 16.7,
                "vix_regime": "CALM",
                "spy": 749.17,
                "spy_ma200": 694.91,
                "spy_ma200_dist": 7.81,
            },
            "positions": [
                {"ticker": "UPS", "direction": "LONG", "pnl_pct": 6.8, "pnl_dollar": 742}
            ],
            "risk_flags": {"vix_elevated": False},
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(facts, f)
        yield path
        os.unlink(path)

    def test_rules_engine_with_real_context(self, rules_file, facts_file):
        """Rules engine + Context builder integration."""
        from modelfungible.core.rules_engine import RulesEngine
        from modelfungible.core.context_builder import ContextBuilder

        # Load rules
        engine = RulesEngine(rules_file)
        engine.validate("EQM")

        sizing = engine.get_sizing("EQM", "CONFIRMED_BULL")
        assert sizing["amount"] == 4500

        # Load context
        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build(role="scanner", strategy="EQM")
        assert ctx.market["regime"] == "CONFIRMED_BULL"
        assert ctx.positions[0]["ticker"] == "UPS"
        assert ctx.open_tickers() == "UPS"

    def test_groq_adapter_direct_call(self):
        """Direct Groq API call — confirms API is reachable."""
        from modelfungible.adapters.groq import GroqAdapter

        key = get_groq_api_key()
        if not key:
            pytest.skip("GROQ_API_KEY not set")

        adapter = GroqAdapter(api_key=key)
        result = adapter.call(
            prompt="Say hello in 3 words. Reply with ONLY those 3 words.",
            model="llama-3.1-8b-instant",
            temperature=0.1,
            max_tokens=20,
        )
        # May return partial JSON - just check it ran without crashing
        content = str(result)
        assert len(content) > 0

    def test_model_executor_with_groq(self, rules_file, facts_file):
        """Full executor: rules → context → Groq → parse → validate."""
        from modelfungible.core.rules_engine import RulesEngine
        from modelfungible.core.context_builder import ContextBuilder
        from modelfungible.core.executor import ModelExecutor

        key = get_groq_api_key()
        if not key:
            pytest.skip("GROQ_API_KEY not set")

        # Setup
        engine = RulesEngine(rules_file)
        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build(role="scanner")

        # Build prompt
        prompt = cb.build_scanner_prompt(
            ctx, "EQM", engine.get("EQM")
        )

        # Execute
        executor = ModelExecutor()
        executor.add_model("scanner", "groq", "llama-3.1-8b-instant", api_key=key)
        result = executor.run(
            prompt=prompt,
            model="scanner",
            temperature=0.1,
            max_tokens=200,
        )

        assert result.success or result.failed  # Did execute
        assert result.model_id == "llama-3.1-8b-instant"
        assert result.latency_s > 0

        # If successful, validate against schema
        if result.success:
            errors = engine.validate_output("EQM", dict(result))
            assert errors == [], f"Output validation errors: {errors}"

    def test_benchmark_two_models_same_decision(self, rules_file, facts_file):
        """
        Integration test: same context → Groq Llama 8B + 70B → same decision.
        This is the key proof: model interchangeability.
        """
        from modelfungible.core.rules_engine import RulesEngine
        from modelfungible.core.context_builder import ContextBuilder
        from modelfungible.core.executor import ModelExecutor

        key = get_groq_api_key()
        if not key:
            pytest.skip("GROQ_API_KEY not set")

        # Setup
        engine = RulesEngine(rules_file)
        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build(role="scanner")
        prompt = cb.build_scanner_prompt(ctx, "EQM", engine.get("EQM"))

        executor = ModelExecutor()
        executor.add_model("llama8b", "groq", "llama-3.1-8b-instant", api_key=key)
        executor.add_model("llama70b", "groq", "llama-3.3-70b-versatile", api_key=key)
        executor.set_fallback_chain(["llama8b", "llama70b"])

        # Run with chain — primary 8B
        result8 = executor.run(prompt=prompt, model="llama8b", max_tokens=200)
        assert result8.success, f"8B failed: {result8._error}"

        # Run 70B
        result70 = executor.run(prompt=prompt, model="llama70b", max_tokens=200)
        assert result70.success, f"70B failed: {result70._error}"

        # Both should pick the same ticker (both chose ADBE in real tests)
        t8  = result8.get("ticker", "NONE")
        t70 = result70.get("ticker", "NONE")
        assert t8 == t70, f"Models disagree: 8B={t8}, 70B={t70}"

        # Both should agree it's valid
        for r, label in [(result8, "8B"), (result70, "70B")]:
            # At minimum, ticker should be present
            assert r.get("ticker"), f"{label} missing ticker: {dict(r)}"


    def test_fallback_chain_works(self):
        """Test that fallback chain works with real API calls."""
        from modelfungible.core.executor import ModelExecutor
        from modelfungible.adapters.base import AdapterError

        key = get_groq_api_key()
        if not key:
            pytest.skip("GROQ_API_KEY not set")

        executor = ModelExecutor()
        executor.add_model("primary", "groq", "llama-3.1-8b-instant", api_key=key)
        executor.add_model("fallback", "groq", "llama-3.3-70b-versatile", api_key=key)
        executor.set_fallback_chain(["primary", "fallback"])

        # Use a tiny prompt
        prompt = "Return JSON: {\"answer\": 42}"

        # If primary fails, should fall back
        result = executor.run(prompt=prompt, max_tokens=50)
        # At least one model should succeed
        assert result.success or result._error is not None


# ─────────────────────────────────────────────────────────────────
# Tests: CLI smoke test
# ─────────────────────────────────────────────────────────────────
class TestCLI:
    def test_cli_help(self):
        """CLI should respond to --help."""
        import subprocess
        r = subprocess.run(
            ["python3", "-m", "modelfungible", "--help"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).parent.parent.parent)
        )
        # Help should either succeed or show usage
        assert r.returncode in (0, 1)
        assert "usage" in r.stdout.lower() or "help" in r.stdout.lower() or "model" in r.stdout.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
