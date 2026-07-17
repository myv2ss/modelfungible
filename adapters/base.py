#!/usr/bin/env python3
"""
Base Adapter — ModelFungible

Defines the adapter interface and shared utilities:
- AdapterError with retry classification
- parse_json_output() with think-block stripping
- BaseAdapter abstract class
"""
from __future__ import annotations
import json
import re
from abc import ABC, abstractmethod
from typing import Any


# ─────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────
class AdapterError(Exception):
    """
    Categorized error from a model adapter.

    Attributes:
        kind:        error category (timeout, auth, rate_limit, server_error, etc.)
        message:     human-readable description
        retryable:  whether retrying with same model would help
    """

    RETRYABLE_KINDS = {"timeout", "rate_limit", "server_error", "upstream_error"}
    NON_RETRYABLE_KINDS = {"auth", "invalid_request", "model_not_found", "context_length"}

    def __init__(self, kind: str, message: str):
        self.kind    = kind
        self.message = message
        super().__init__(f"[{kind}] {message}")

    def is_retryable(self) -> bool:
        return self.kind in self.RETRYABLE_KINDS


# ─────────────────────────────────────────────────────────────────
# Parsed output wrapper
# ─────────────────────────────────────────────────────────────────
class ParsedOutput(dict):
    """
    A dict that also carries raw text and usage metadata.
    Created by BaseAdapter.call() to return both parsed JSON and metadata.
    Acts as a dict for schema validation but carries execution metadata.
    """

    def __init__(self, data: dict, raw: str = "", usage: dict | None = None):
        super().__init__(data)
        self._raw   = raw
        self._usage = usage or {}

    def __repr__(self):
        return f"<ParsedOutput: {super().__repr__()[:50]}>"


# ─────────────────────────────────────────────────────────────────
# JSON parsing
# ─────────────────────────────────────────────────────────────────
def parse_json_output(raw: str) -> dict:
    """
    Extract a JSON object from raw model output.

    Handles:
    - Clean JSON: {"ticker": "ADBE"}
    - Markdown code blocks: ```json\n{...}\n```
    - Think/reasoning blocks: <think>\n...\n</think>{actual json}
    - Trailing text: {"ticker": "ADBE"}\n\nMore text
    - Partial text before: Some explanation {"ticker": "ADBE"} more text

    Returns:
        dict on success, {"error": "parse_failed"} on complete failure.
    """
    if not raw or not raw.strip():
        return {"error": "empty_output"}

    text = raw.strip()

    # Remove think/reasoning blocks (provider-specific markers)
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)    # OpenAI/Anthropic think
    text = re.sub(r"<\|reserved_\d{4,}\|>[\s\S]*?<\|reserved_\d{4,}\|>", "", text)  # Groq think
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE)  # case-insensitive
    text = text.strip()

    # Try markdown code blocks
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    # Find first { and last }
    start = text.find("{")
    end   = text.rfind("}") + 1

    if start >= 0 and end > start:
        json_str = text[start:end]
        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            # Try to fix common issues: trailing commas, single quotes
            try:
                # Fix trailing commas
                json_str = re.sub(r",(\s*[}\]])", r"\1", json_str)
                return json.loads(json_str)
            except json.JSONDecodeError:
                return {"error": "parse_failed", "raw": raw[:200]}

    return {"error": "no_json_found", "raw": raw[:200]}


# ─────────────────────────────────────────────────────────────────
# BaseAdapter
# ─────────────────────────────────────────────────────────────────
class BaseAdapter(ABC):
    """
    Abstract base class for model provider adapters.

    Implementations must provide:
        call(prompt, model, **kwargs) → dict (parsed JSON)
        provider_name                  → str

    Optional:
        get_usage(response)           → dict
        supportsstreaming()           → bool
    """

    provider_name: str = "base"

    def __init__(self, api_key: str | None = None, **provider_kwargs):
        self.api_key = api_key
        self._kwargs = provider_kwargs

    @abstractmethod
    def call(
        self,
        prompt: str,
        model: str,
        system_prompt: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 500,
        **kwargs,
    ) -> dict:
        """
        Call the model and return parsed JSON output.

        Args:
            prompt:       the user prompt
            model:        model identifier string
            system_prompt: optional system prompt
            temperature:  sampling temperature
            max_tokens:   max output tokens

        Returns:
            Parsed JSON dict from the model.

        Raises:
            AdapterError on failure.
        """
        ...

    def supports_streaming(self) -> bool:
        """Whether this adapter supports streaming responses."""
        return False

    def get_usage(self, raw_response: Any) -> dict | None:
        """Extract token usage from raw provider response."""
        return None

    def _raise_error(self, kind: str, message: str):
        raise AdapterError(kind, message)


# ─────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────
__all__ = [
    "AdapterError",
    "BaseAdapter",
    "parse_json_output",
]
