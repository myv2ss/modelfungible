# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Custom / Generic Adapter — ModelFungible / Rita

Plug in ANY LLM provider in seconds — no code changes needed.
Supports:
  - Local models (Ollama, LM Studio, LocalAI, ollama)
  - Enterprise intranet models
  - Any OpenAI-compatible external API
  - Anthropic-compatible APIs (via /v1/messages endpoint)
  - vLLM, TGI, SGLang serving endpoints

Usage:
    from modelfungible.adapters import CustomAdapter

    # Local Ollama
    adapter = CustomAdapter(
        provider_name="ollama",
        base_url="http://localhost:11434/v1",
        api_key="not-needed",
    )

    # Enterprise intranet model
    adapter = CustomAdapter(
        provider_name="intranet-gpt",
        base_url="https://llm.internal.corp.com/v1",
        api_key="corp-key-xxx",
    )

    # Any other OpenAI-compatible provider
    adapter = CustomAdapter(
        provider_name="my-provider",
        base_url="https://api.my-provider.com/v1",
        api_key="key-xxx",
    )

    # With system prompt support
    adapter = CustomAdapter(
        provider_name="my-provider",
        base_url="https://api.my-provider.com/v1",
        api_key="key-xxx",
        supports_system_prompt=True,
    )
"""

from __future__ import annotations
from typing import Any

from modelfungible.adapters.openai import OpenAIAdapter


class CustomAdapter(OpenAIAdapter):
    """
    Generic adapter for any OpenAI-compatible LLM provider.

    This is the "bring your own model" adapter — works with anything that
    speaks the OpenAI chat completions API protocol.

    For non-OpenAI-compatible providers (Anthropic /v1/messages, etc.),
    subclass BaseAdapter and implement the call() method.
    """

    def __init__(
        self,
        provider_name: str,
        base_url: str,
        api_key: str | None = None,
        timeout: int = 60,
        supports_system_prompt: bool = True,
        default_model: str | None = None,
        **kwargs,
    ):
        """
        Args:
            provider_name:     Identifier for this provider (used in logs, routing)
            base_url:          Full base URL of the provider's OpenAI-compatible endpoint
            api_key:           API key (or None for local models with no auth)
            timeout:           Request timeout in seconds
            supports_system_prompt: Whether the provider supports system messages
            default_model:     Model to use if none specified in call()
        """
        import os as _os
        actual_key = api_key or _os.environ.get(f"{provider_name.upper()}_API_KEY", "not-needed")
        super().__init__(
            api_key=actual_key,
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            **kwargs,
        )
        self._provider_name = provider_name
        self._supports_system_prompt = supports_system_prompt
        self._default_model = default_model

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def call(
        self,
        prompt: str,
        model: str | None = None,
        system_prompt: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 500,
        **kwargs,
    ) -> dict:
        # Use default model if none specified
        actual_model = model or self._default_model or "default"
        # Skip system prompt if provider doesn't support it
        effective_system = system_prompt if self._supports_system_prompt else None
        return super().call(
            prompt=prompt,
            model=actual_model,
            system_prompt=effective_system,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Provider Registry
# Self-service provider registration — add any provider at runtime in 2 lines
# ─────────────────────────────────────────────────────────────────────────────

class ProviderRegistry:
    """
    Self-service provider registry. Add new LLM providers without touching core code.

    Usage:
        from modelfungible.adapters import ProviderRegistry, CustomAdapter

        registry = ProviderRegistry()

        # Add a custom provider
        registry.register("my_intranet", CustomAdapter(
            provider_name="my_intranet",
            base_url="https://llm.corp.com/v1",
            api_key="corp-secret",
        ))

        # Get a provider
        adapter = registry.get("my_intranet")
        result = adapter.call("Hello", model="corp-gpt-4")

        # List all registered providers
        print(registry.list_providers())
    """

    def __init__(self):
        self._providers: dict[str, Any] = {}

    def register(self, name: str, adapter: Any) -> None:
        """Register a provider adapter under a human-readable name."""
        if not hasattr(adapter, "call"):
            raise ValueError(f"Adapter for '{name}' must implement call()")
        self._providers[name.lower()] = adapter
        print(f"[ProviderRegistry] Registered: {name}")

    def get(self, name: str) -> Any:
        """Get a registered provider by name."""
        name = name.lower()
        if name not in self._providers:
            available = ", ".join(self._providers.keys()) or "none"
            raise KeyError(f"Unknown provider '{name}'. Available: {available}")
        return self._providers[name]

    def list_providers(self) -> list[str]:
        """List all registered provider names."""
        return list(self._providers.keys())

    def unregister(self, name: str) -> None:
        """Remove a provider from the registry."""
        del self._providers[name.lower()]

    def get_with_fallback(self, primary: str, *fallbacks: str) -> Any:
        """
        Get primary provider, falling back to each fallback in order.
        Useful when a provider fails — automatically try the next.
        """
        for name in (primary, *fallbacks):
            try:
                return self.get(name)
            except KeyError:
                continue
        raise KeyError(f"All providers failed: {primary}, {fallbacks}")


# Global default registry with all built-in providers pre-registered
_default_registry: ProviderRegistry | None = None


def get_default_registry() -> ProviderRegistry:
    """Get the global default provider registry."""
    global _default_registry
    if _default_registry is None:
        _default_registry = ProviderRegistry()
        _register_builtin_providers(_default_registry)
    return _default_registry


def _register_builtin_providers(registry: ProviderRegistry) -> None:
    """Register all built-in providers on first use."""
    import os as _os

    # MiniMax
    if _os.environ.get("MINIMAX_API_KEY"):
        from modelfungible.adapters.minimax import MiniMaxAdapter
        registry.register("minimax", MiniMaxAdapter())

    # Moonshot / Kimi
    if _os.environ.get("MOONSHOT_API_KEY") or _os.environ.get("KIMI_API_KEY"):
        from modelfungible.adapters.moonshot import MoonshotAdapter
        registry.register("moonshot", MoonshotAdapter())
        registry.register("kimi", MoonshotAdapter())

    # GLM / Zhipu
    if _os.environ.get("ZHIPU_API_KEY"):
        from modelfungible.adapters.glm import GLMAdapter
        registry.register("glm", GLMAdapter())

    # Owen (generic — user must configure base_url)
    if _os.environ.get("OWEN_API_KEY"):
        from modelfungible.adapters.owen import OwenAdapter
        registry.register("owen", OwenAdapter())

    # Groq (always available)
    from modelfungible.adapters.groq import GroqAdapter
    registry.register("groq", GroqAdapter())

    # OpenAI
    if _os.environ.get("OPENAI_API_KEY"):
        from modelfungible.adapters.openai import OpenAIAdapter
        registry.register("openai", OpenAIAdapter())

    # Anthropic
    if _os.environ.get("ANTHROPIC_API_KEY"):
        from modelfungible.adapters.anthropic import AnthropicAdapter
        registry.register("anthropic", AnthropicAdapter())

    # Ollama (local)
    try:
        from modelfungible.enterprise.adapters.ollama import OllamaAdapter
        registry.register("ollama", OllamaAdapter())
    except Exception:
        pass


__all__ = [
    "CustomAdapter",
    "ProviderRegistry",
    "get_default_registry",
]
