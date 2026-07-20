# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
"""Tests for new LLM provider adapters: MiniMax, Moonshot, GLM, Owen, CustomAdapter, ProviderRegistry."""
import sys
sys.path.insert(0, '.')

from unittest.mock import patch, MagicMock
import pytest


class TestMiniMaxAdapter:
    def test_instantiation(self):
        from modelfungible.adapters.minimax import MiniMaxAdapter
        a = MiniMaxAdapter(api_key="test-key")
        assert a.provider_name == "minimax"
        assert "minimax.chat" in a.base_url

    def test_uses_env_key(self):
        import os
        os.environ["MINIMAX_API_KEY"] = "env-key-xxx"
        from modelfungible.adapters.minimax import MiniMaxAdapter
        a = MiniMaxAdapter()
        assert a.api_key == "env-key-xxx"
        del os.environ["MINIMAX_API_KEY"]


class TestMoonshotAdapter:
    def test_instantiation(self):
        from modelfungible.adapters.moonshot import MoonshotAdapter
        a = MoonshotAdapter(api_key="test-key")
        assert a.provider_name == "moonshot"
        assert "moonshot.cn" in a.base_url

    def test_uses_kimi_env_key(self):
        import os
        # Clear MOONSHOT_API_KEY so KIMI_API_KEY is picked up
        _saved = os.environ.pop("MOONSHOT_API_KEY", None)
        os.environ["KIMI_API_KEY"] = "kimi-key-xxx"
        from modelfungible.adapters.moonshot import MoonshotAdapter
        a = MoonshotAdapter()
        assert a.api_key == "kimi-key-xxx"
        del os.environ["KIMI_API_KEY"]
        if _saved is not None:
            os.environ["MOONSHOT_API_KEY"] = _saved


class TestGLMAdapter:
    def test_instantiation(self):
        from modelfungible.adapters.glm import GLMAdapter
        a = GLMAdapter(api_key="test-key")
        assert a.provider_name == "glm"
        assert "bigmodel.cn" in a.base_url

    def test_uses_env_key(self):
        import os
        os.environ["ZHIPU_API_KEY"] = "zhipu-key-xxx"
        from modelfungible.adapters.glm import GLMAdapter
        a = GLMAdapter()
        assert a.api_key == "zhipu-key-xxx"
        del os.environ["ZHIPU_API_KEY"]


class TestOwenAdapter:
    def test_instantiation(self):
        from modelfungible.adapters.owen import OwenAdapter
        a = OwenAdapter(api_key="test-key", base_url="https://custom.owen.ai/v1")
        assert a.provider_name == "owen"
        assert a.base_url == "https://custom.owen.ai/v1"

    def test_custom_base_url(self):
        from modelfungible.adapters.owen import OwenAdapter
        a = OwenAdapter(api_key="key", base_url="https://my-llm.company.com/openai/v1")
        assert a.base_url == "https://my-llm.company.com/openai/v1"


class TestCustomAdapter:
    def test_local_ollama(self):
        from modelfungible.adapters.custom import CustomAdapter
        a = CustomAdapter(
            provider_name="ollama",
            base_url="http://localhost:11434/v1",
            api_key="not-needed",
        )
        assert a.provider_name == "ollama"
        assert a._default_model is None

    def test_enterprise_intranet(self):
        from modelfungible.adapters.custom import CustomAdapter
        a = CustomAdapter(
            provider_name="corp-gpt",
            base_url="https://llm.internal.corp.com/v1",
            api_key="corp-secret",
            default_model="corp-gpt-4",
        )
        assert a.provider_name == "corp-gpt"
        assert a._default_model == "corp-gpt-4"

    def test_system_prompt_respected(self):
        from modelfungible.adapters.custom import CustomAdapter
        a = CustomAdapter(provider_name="test", base_url="http://localhost:8000", supports_system_prompt=True)
        assert a._supports_system_prompt is True

    def test_system_prompt_disabled(self):
        from modelfungible.adapters.custom import CustomAdapter
        a = CustomAdapter(provider_name="test", base_url="http://localhost:8000", supports_system_prompt=False)
        assert a._supports_system_prompt is False


class TestProviderRegistry:
    def test_register_and_get(self):
        from modelfungible.adapters.custom import ProviderRegistry, CustomAdapter
        registry = ProviderRegistry()
        adapter = CustomAdapter(provider_name="test", base_url="http://localhost:8000")
        registry.register("my-test", adapter)
        retrieved = registry.get("my-test")
        assert retrieved is adapter

    def test_case_insensitive(self):
        from modelfungible.adapters.custom import ProviderRegistry, CustomAdapter
        registry = ProviderRegistry()
        adapter = CustomAdapter(provider_name="test", base_url="http://localhost:8000")
        registry.register("MyTest", adapter)
        assert registry.get("mytest") is adapter
        assert registry.get("MYTEST") is adapter

    def test_unknown_provider_raises(self):
        from modelfungible.adapters.custom import ProviderRegistry
        registry = ProviderRegistry()
        with pytest.raises(KeyError):
            registry.get("nonexistent")

    def test_list_providers(self):
        from modelfungible.adapters.custom import ProviderRegistry, CustomAdapter
        registry = ProviderRegistry()
        registry.register("a", CustomAdapter(provider_name="a", base_url="http://a.com"))
        registry.register("b", CustomAdapter(provider_name="b", base_url="http://b.com"))
        assert set(registry.list_providers()) == {"a", "b"}

    def test_unregister(self):
        from modelfungible.adapters.custom import ProviderRegistry, CustomAdapter
        registry = ProviderRegistry()
        adapter = CustomAdapter(provider_name="x", base_url="http://x.com")
        registry.register("x", adapter)
        registry.unregister("x")
        assert "x" not in registry.list_providers()

    def test_must_implement_call(self):
        from modelfungible.adapters.custom import ProviderRegistry
        registry = ProviderRegistry()
        with pytest.raises(ValueError, match="must implement call()"):
            registry.register("bad", object())

    def test_get_with_fallback_primary(self):
        from modelfungible.adapters.custom import ProviderRegistry, CustomAdapter
        registry = ProviderRegistry()
        a = CustomAdapter(provider_name="a", base_url="http://a.com")
        registry.register("a", a)
        result = registry.get_with_fallback("a")
        assert result is a

    def test_get_with_fallback_fails_to_second(self):
        from modelfungible.adapters.custom import ProviderRegistry, CustomAdapter
        registry = ProviderRegistry()
        a = CustomAdapter(provider_name="a", base_url="http://a.com")
        b = CustomAdapter(provider_name="b", base_url="http://b.com")
        registry.register("a", a)
        registry.register("b", b)
        result = registry.get_with_fallback("nonexistent", "a", "b")
        assert result is a

    def test_get_with_fallback_all_fail(self):
        from modelfungible.adapters.custom import ProviderRegistry
        registry = ProviderRegistry()
        with pytest.raises(KeyError):
            registry.get_with_fallback("x", "y", "z")


class TestProviderRegistryAutoRegistration:
    def test_groq_always_registered(self):
        from modelfungible.adapters.custom import get_default_registry
        registry = get_default_registry()
        assert "groq" in registry.list_providers()

    def test_get_default_registry_singleton(self):
        from modelfungible.adapters.custom import get_default_registry
        r1 = get_default_registry()
        r2 = get_default_registry()
        assert r1 is r2  # Same object


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
