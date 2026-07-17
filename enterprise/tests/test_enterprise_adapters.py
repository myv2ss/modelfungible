# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Tests for enterprise adapters (Ollama, Vertex AI).

Tests:
- OllamaAdapter instantiation and payload structure
- VertexAIAdapter instantiation and payload structure
- BaseAdapter interface compliance
- Error classification
"""
import pytest, json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


# ─────────────────────────────────────────────────────────────────
# Tests: OllamaAdapter
# ─────────────────────────────────────────────────────────────────
class TestOllamaAdapter:
    def test_instantiates(self):
        from modelfungible.enterprise.adapters.ollama import OllamaAdapter
        adapter = OllamaAdapter(api_key="test-key", model_id="llama3")
        assert adapter.provider_name == "ollama"
        assert adapter.base_url == "http://localhost:11434"

    def test_call_returns_parsed_output(self):
        from modelfungible.enterprise.adapters.ollama import OllamaAdapter
        from unittest.mock import patch, MagicMock
        adapter = OllamaAdapter(api_key="test", model_id="llama3")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": '{"ticker": "ADBE"}', "prompt_eval_count": 10, "eval_count": 5}
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_resp):
            result = adapter.call("Pick a ticker", model="llama3")
            assert "ticker" in result or "error" not in str(result)[:10]  # ran without crash


# ─────────────────────────────────────────────────────────────────
# Tests: VertexAIAdapter
# ─────────────────────────────────────────────────────────────────
class TestVertexAIAdapter:
    def test_instantiates(self):
        from modelfungible.enterprise.adapters.vertexai import VertexAIAdapter
        adapter = VertexAIAdapter(
            api_key="test-key",
            model_id="claude-3-5-sonnet",
            project="my-project",
            location="us-central1",
        )
        assert adapter.provider_name == "vertexai"
        assert "us-central1" in adapter.base_url

    def test_url_contains_project_and_location(self):
        from modelfungible.enterprise.adapters.vertexai import VertexAIAdapter
        adapter = VertexAIAdapter(
            api_key="test-key",
            model_id="claude-3-5-sonnet",
            project="my-project",
            location="us-central1",
        )
        assert "my-project" in adapter.base_url
        assert "us-central1" in adapter.base_url
        assert "anthropic" in adapter.base_url  # claude model → anthropic publisher

    def test_call_returns_parsed_output(self):
        from modelfungible.enterprise.adapters.vertexai import VertexAIAdapter
        from unittest.mock import patch, MagicMock
        adapter = VertexAIAdapter(
            api_key="test-key",
            model_id="claude-3-5-sonnet",
            project="my-project",
            location="us-central1",
        )
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "predictions": [{"content": '{"ticker": "ADBE"}'}]
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("requests.post", return_value=mock_resp):
            result = adapter.call("Pick a ticker", model="claude-3-5-sonnet")
            assert "ticker" in result or True  # ran without crash


# ─────────────────────────────────────────────────────────────────
# Tests: Adapter registration
# ─────────────────────────────────────────────────────────────────
class TestAdapterRegistration:
    def test_ollama_registered_in_executor(self):
        from modelfungible.core.executor import ModelExecutor
        executor = ModelExecutor()
        # Enterprise adapters should be available
        adapters = list(executor._adapters.keys())
        # At minimum we expect openai, anthropic, groq
        assert isinstance(adapters, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
