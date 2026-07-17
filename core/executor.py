#!/usr/bin/env python3
"""
Model Executor — ModelFungible

Universal model execution layer.
Routes to any model provider, handles fallback chains,
parses structured output, logs decisions.
"""
from __future__ import annotations
import time
from typing import Any

from modelfungible.adapters.base import BaseAdapter, AdapterError, parse_json_output
from modelfungible.adapters.openai import OpenAIAdapter
from modelfungible.adapters.anthropic import AnthropicAdapter
from modelfungible.adapters.groq import GroqAdapter


# ─────────────────────────────────────────────────────────────────
# ExecutionResult
# ─────────────────────────────────────────────────────────────────
class ExecutionResult(dict):
    """
    Result of a model execution.

    Acts as a dict (for schema validation access) but also carries
    execution metadata.

    Attributes:
        output:         parsed JSON output (dict)
        model_id:      model used for this execution
        latency_s:     time taken for the call
        raw:           raw text from model
        error:         error message if failed
        fallback_used: name of fallback model if primary failed
        all_failed:    True if all models in chain failed
    """

    def __init__(
        self,
        output: dict,
        model_id: str,
        latency_s: float = 0,
        raw: str = "",
        error: str | None = None,
        fallback_used: str | None = None,
        all_failed: bool = False,
    ):
        super().__init__(output)
        self.output        = output
        self.model_id      = model_id
        self.latency_s    = latency_s
        self.raw          = raw
        self._error       = error
        self._fallback_used = fallback_used
        self._all_failed  = all_failed

    def __repr__(self):
        if self._error:
            return "<ExecutionResult ERROR: {}>".format(self._error)
        ticker = str(self.get("ticker", ""))[:20]
        return "<ExecutionResult {}: {}>".format(self.model_id, ticker)

    @property
    def success(self) -> bool:
        return self._error is None and not self._all_failed

    @property
    def failed(self) -> bool:
        return not self.success


# ─────────────────────────────────────────────────────────────────
# ModelExecutor
# ─────────────────────────────────────────────────────────────────
class ModelExecutor:
    """
    Universal model executor with fallback chains.

    Example:
        executor = ModelExecutor()
        executor.add_model("primary",   "openai",    "gpt-4o")
        executor.add_model("fallback", "anthropic", "claude-3.5-sonnet")
        executor.set_fallback_chain(["primary", "fallback"])

        result = executor.run(
            prompt="Pick the best ticker from these signals...",
            context={"signals": [...]}
        )

        if result.success:
            print(result["ticker"], result["reason"])
    """

    # Provider name → adapter class
    _ADAPTERS = {
        "openai":    OpenAIAdapter,
        "anthropic": AnthropicAdapter,
        "groq":      GroqAdapter,
    }

    def __init__(self):
        self._adapters:    dict[str, BaseAdapter] = {}
        self._models:      dict[str, dict]        = {}   # name → {provider, model}
        self._chain:       list[str]              = []

        # Register default adapters
        for name, cls in self._ADAPTERS.items():
            try:
                self._adapters[name] = cls()
            except Exception:
                # Adapter init failed (e.g., no API key) — will be caught at call time
                pass

    # ── Model registration ─────────────────────────────────────

    def add_model(
        self,
        name: str,
        provider: str,
        model_id: str,
        api_key: str | None = None,
        **provider_kwargs,
    ):
        """
        Register a model with a name.

        Args:
            name:       friendly name (e.g. "primary", "fallback")
            provider:   provider name (openai, anthropic, groq)
            model_id:   provider-specific model ID (e.g. "gpt-4o")
            api_key:    optional API key override
        """
        if provider not in self._ADAPTERS:
            raise ValueError(
                f"Unknown provider '{provider}'. "
                f"Available: {list(self._ADAPTERS.keys())}"
            )

        # Instantiate adapter if not already
        if provider not in self._adapters:
            self._adapters[provider] = self._ADAPTERS[provider]()

        adapter = self._adapters[provider]
        if api_key:
            adapter.api_key = api_key

        self._models[name] = {
            "provider":    provider,
            "model_id":   model_id,
            "adapter":    adapter,
        }

    def set_fallback_chain(self, chain: list[str]):
        """
        Set the fallback chain — ordered list of model names to try.

        Args:
            chain: list of model names as registered with add_model()
        """
        self._chain = list(chain)

    # ── Execution ─────────────────────────────────────────────

    def run(
        self,
        prompt: str,
        model: str | None = None,
        system_prompt: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 500,
        context: dict | None = None,
        fallback_chain: list[str] | None = None,
        **kwargs,
    ) -> ExecutionResult:
        """
        Execute a model call with fallback support.

        Args:
            prompt:         user prompt
            model:          specific model name to use (bypasses chain)
            system_prompt:  optional system prompt
            temperature:    sampling temperature
            max_tokens:     max output tokens
            context:        optional context dict (for logging)
            fallback_chain: override default fallback chain
            **kwargs:       passed through to adapter

        Returns:
            ExecutionResult
        """
        chain = fallback_chain or self._chain

        # If specific model given, use it directly (no fallback)
        if model:
            return self._call_single(model, prompt, system_prompt,
                                     temperature, max_tokens, **kwargs)

        # Try chain in order
        fallback_used = None
        last_error    = None

        for model_name in chain:
            if model_name not in self._models:
                last_error = f"Model '{model_name}' not registered"
                continue

            try:
                result = self._call_single(
                    model_name, prompt, system_prompt,
                    temperature, max_tokens, **kwargs
                )
                if result.success:
                    return result
                last_error = result._error
            except AdapterError as e:
                last_error = str(e)
                if not e.is_retryable():
                    break
            except Exception as e:
                last_error = str(e)

            # This model failed, try next
            fallback_used = model_name

        # All failed
        return ExecutionResult(
            output={},
            model_id=chain[-1] if chain else model or "unknown",
            error=last_error,
            all_failed=True,
        )

    def _call_single(
        self,
        model_name: str,
        prompt: str,
        system_prompt: str | None,
        temperature: float,
        max_tokens: int,
        **kwargs,
    ) -> ExecutionResult:
        """Call a single model by registered name."""
        model_config = self._models[model_name]
        adapter      = model_config["adapter"]
        model_id    = model_config["model_id"]

        start = time.time()
        try:
            output = adapter.call(
                prompt=prompt,
                model=model_id,
                system_prompt=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                **kwargs,
            )
            latency = time.time() - start

            raw = getattr(output, "_raw", str(output))
            usage = getattr(output, "_usage", None)

            return ExecutionResult(
                output=output,
                model_id=model_id,
                latency_s=round(latency, 3),
                raw=raw,
            )

        except AdapterError as e:
            return ExecutionResult(
                output={},
                model_id=model_id,
                latency_s=time.time() - start,
                error=str(e),
                all_failed=(not e.is_retryable()),
            )

        except Exception as e:
            return ExecutionResult(
                output={},
                model_id=model_id,
                latency_s=time.time() - start,
                error=f"Unexpected error: {e}",
                all_failed=True,
            )

    # ── Utilities ──────────────────────────────────────────────

    def list_models(self) -> list[dict]:
        """Return list of registered models."""
        return [
            {"name": name, **cfg}
            for name, cfg in self._models.items()
        ]

    def model_info(self, name: str) -> dict | None:
        """Return config for a registered model."""
        return self._models.get(name)


# ─────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────
__all__ = ["ModelExecutor", "ExecutionResult"]
