#!/usr/bin/env python3
"""
Unit tests for Model Executor and Adapters.
Tests: output parsing, error classification, fallback chains,
       adapter registry, result object.
"""
import pytest, json, os
from unittest.mock import patch, MagicMock
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ─────────────────────────────────────────────────────────────────
# Tests: Output Parsing
# ─────────────────────────────────────────────────────────────────
class TestOutputParsing:
    def test_parse_clean_json(self):
        from modelfungible.adapters.base import parse_json_output
        raw = '{"ticker": "ADBE", "direction": "LONG", "size": 4500}'
        result = parse_json_output(raw)
        assert result["ticker"] == "ADBE"
        assert result["size"] == 4500

    def test_parse_with_markdown_code_block(self):
        from modelfungible.adapters.base import parse_json_output
        raw = '```json\n{"ticker": "ADBE"}\n```'
        result = parse_json_output(raw)
        assert result["ticker"] == "ADBE"

    def test_parse_with_leading_text(self):
        from modelfungible.adapters.base import parse_json_output
        raw = 'Based on the analysis, the best pick is {"ticker": "ADBE", "direction": "LONG"} trailing text'
        result = parse_json_output(raw)
        assert result["ticker"] == "ADBE"

    def test_parse_invalid_returns_error_dict(self):
        from modelfungible.adapters.base import parse_json_output
        raw = "This is not JSON at all"
        result = parse_json_output(raw)
        assert "error" in result or result == {}

    def test_parse_empty_returns_error(self):
        from modelfungible.adapters.base import parse_json_output
        result = parse_json_output("")
        assert "error" in result or result == {}


# ─────────────────────────────────────────────────────────────────
# Tests: Error Classification
# ─────────────────────────────────────────────────────────────────
class TestErrorClassification:
    def test_timeout_is_retryable(self):
        from modelfungible.adapters.base import AdapterError
        e = AdapterError("timeout", "Request timed out")
        assert e.is_retryable() is True

    def test_rate_limit_is_retryable(self):
        from modelfungible.adapters.base import AdapterError
        e = AdapterError("rate_limit", "Rate limited")
        assert e.is_retryable() is True

    def test_server_error_is_retryable(self):
        from modelfungible.adapters.base import AdapterError
        e = AdapterError("server_error", "HTTP 500")
        assert e.is_retryable() is True

    def test_auth_is_not_retryable(self):
        from modelfungible.adapters.base import AdapterError
        e = AdapterError("auth", "Invalid API key")
        assert e.is_retryable() is False

    def test_model_not_found_not_retryable(self):
        from modelfungible.adapters.base import AdapterError
        e = AdapterError("model_not_found", "Model not found")
        assert e.is_retryable() is False

    def test_context_length_not_retryable(self):
        from modelfungible.adapters.base import AdapterError
        e = AdapterError("context_length", "Context too long")
        assert e.is_retryable() is False


# ─────────────────────────────────────────────────────────────────
# Tests: ExecutionResult
# ─────────────────────────────────────────────────────────────────
class TestExecutionResult:
    def test_result_stores_output(self):
        from modelfungible.core.executor import ExecutionResult
        r = ExecutionResult(
            output={"ticker": "ADBE", "size": 4500},
            model_id="gpt-4o",
            latency_s=1.5,
            raw='{"ticker": "ADBE"}',
        )
        assert r["ticker"] == "ADBE"
        assert r.output["ticker"] == "ADBE"
        assert r.latency_s == 1.5
        assert r.model_id == "gpt-4o"

    def test_result_dict_access(self):
        from modelfungible.core.executor import ExecutionResult
        r = ExecutionResult(output={"key": "value"}, model_id="test")
        assert r["key"] == "value"
        assert r.get("missing", "default") == "default"

    def test_success_property(self):
        from modelfungible.core.executor import ExecutionResult
        ok = ExecutionResult(output={"a": 1}, model_id="m")
        assert ok.success is True
        assert ok.failed is False

    def test_failed_result(self):
        from modelfungible.core.executor import ExecutionResult
        bad = ExecutionResult(output={}, model_id="m", error="timeout")
        assert bad.success is False
        assert bad.failed is True

    def test_repr_on_success(self):
        from modelfungible.core.executor import ExecutionResult
        r = ExecutionResult(output={"ticker": "ADBE"}, model_id="gpt-4o")
        assert "ADBE" in repr(r)
        assert "ERROR" not in repr(r)


# ─────────────────────────────────────────────────────────────────
# Tests: Adapter Registry
# ─────────────────────────────────────────────────────────────────
class TestAdapterRegistry:
    def test_default_adapters_registered(self):
        from modelfungible.core.executor import ModelExecutor
        executor = ModelExecutor()
        # Default adapters should be registered
        assert "openai" in executor._adapters
        assert "anthropic" in executor._adapters
        assert "groq" in executor._adapters

    def test_add_model_requires_valid_provider(self):
        from modelfungible.core.executor import ModelExecutor
        executor = ModelExecutor()
        with pytest.raises(ValueError, match="Unknown provider"):
            executor.add_model("bad", "nonexistent_provider", "some-model")

    def test_add_and_list_model(self):
        from modelfungible.core.executor import ModelExecutor
        executor = ModelExecutor()
        executor.add_model("primary", "openai", "gpt-4o")
        models = executor.list_models()
        names = [m["name"] for m in models]
        assert "primary" in names

    def test_model_info(self):
        from modelfungible.core.executor import ModelExecutor
        executor = ModelExecutor()
        executor.add_model("test", "groq", "llama-3.3-70b")
        info = executor.model_info("test")
        assert info is not None
        assert info["model_id"] == "llama-3.3-70b"


# ─────────────────────────────────────────────────────────────────
# Tests: Fallback Chain (mock-based)
# ─────────────────────────────────────────────────────────────────
class TestFallbackChain:
    def test_chain_empty_calls_nothing(self):
        from modelfungible.core.executor import ModelExecutor
        executor = ModelExecutor()
        executor.add_model("primary", "openai", "gpt-4o")
        # No chain set — calling without model falls back gracefully
        executor.set_fallback_chain([])
        result = executor.run(prompt="hello")
        assert result._all_failed is True

    def test_unknown_model_in_chain_raises(self):
        from modelfungible.core.executor import ModelExecutor
        from modelfungible.adapters.base import AdapterError
        executor = ModelExecutor()
        executor.add_model("primary", "openai", "gpt-4o")
        executor.set_fallback_chain(["primary", "nonexistent"])
        # Unknown model in chain causes failure
        result = executor.run(prompt="hello")
        assert result._all_failed is True

    def test_explicit_model_bypasses_chain(self):
        from modelfungible.core.executor import ModelExecutor
        executor = ModelExecutor()
        executor.add_model("primary", "openai", "gpt-4o")
        executor.set_fallback_chain(["primary", "fallback"])
        # Calling with explicit model name should only use that model
        # (this will fail because the adapter has no real API key, but it should
        # not fall back to "fallback")
        result = executor.run(prompt="hello", model="primary")
        # Should get an error (no valid auth) but not fall back
        assert result.model_id == "gpt-4o"

    def test_result_contains_error_on_failure(self):
        from modelfungible.core.executor import ModelExecutor
        executor = ModelExecutor()
        executor.add_model("primary", "openai", "gpt-4o")
        executor.set_fallback_chain(["primary"])
        result = executor.run(prompt="hello")
        # Without valid API key this should fail with auth error
        assert result.failed is True


# ─────────────────────────────────────────────────────────────────
# Tests: Adapter Instantiation
# ─────────────────────────────────────────────────────────────────
class TestAdapters:
    def test_groq_adapter_instantiates(self):
        from modelfungible.adapters.groq import GroqAdapter
        adapter = GroqAdapter(api_key="test-key")
        assert adapter.provider_name == "groq"
        assert adapter.base_url == "https://api.groq.com/openai/v1"

    def test_openai_adapter_instantiates(self):
        from modelfungible.adapters.openai import OpenAIAdapter
        adapter = OpenAIAdapter(api_key="test-key")
        assert adapter.provider_name == "openai"
        assert "openai.com" in adapter.base_url

    def test_anthropic_adapter_instantiates(self):
        from modelfungible.adapters.anthropic import AnthropicAdapter
        adapter = AnthropicAdapter(api_key="test-key")
        assert adapter.provider_name == "anthropic"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
