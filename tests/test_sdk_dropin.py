# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
"""Tests for sdk_dropin — true drop-in OpenAI + Anthropic SDK."""
import sys
sys.path.insert(0, 'core')

import importlib.util, json, time, os, httpx
from unittest.mock import patch, MagicMock

import pytest

# Load sdk_dropin without triggering package __init__ imports
spec = importlib.util.spec_from_file_location('sdk_dropin', 'core/sdk_dropin.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
OpenAI = mod.OpenAI
Anthropic = mod.Anthropic


class FakeSession:
    def __init__(self, payload_capture=None):
        self.captured = payload_capture or {}
        self._resp = {"output": "Test response", "input_tokens_est": 10, "output_tokens_est": 5, "model_name": "gpt-4o", "audit_entry_id": "test123"}
    def post(self, url, json, headers, timeout):
        self.captured['url'] = url
        self.captured['payload'] = json
        r = MagicMock()
        r.status_code = 200
        r.json = lambda: self._resp
        r.raise_for_status = lambda: None
        return r


class TestMultiTurnPreservation:
    """CRITICAL: Old sdk.py only sent last user message. New sdk_dropin preserves full conversation."""

    def test_full_conversation_preserved(self):
        client = mod._RitaHttpClient('http://localhost:8000', 'key', timeout=5.0)
        client._session = FakeSession()

        messages = [
            {'role': 'system', 'content': 'You are a lawyer.'},
            {'role': 'user', 'content': 'Can I sue for X?'},
            {'role': 'assistant', 'content': 'Yes, you can sue.'},
            {'role': 'user', 'content': 'What about Y?'},
        ]
        client.chat_completions_create(
            model='gpt-4o', messages=messages, temperature=0.7, max_tokens=100,
            top_p=None, stop=None, stream=False, tools=None, tool_choice=None,
            response_format=None, seed=None, presence_penalty=None, frequency_penalty=None,
        )

        payload = client._session.captured['payload']
        # System should be extracted separately
        assert payload['system'] == 'You are a lawyer.'
        # Prompt should contain FULL conversation (not just last user message)
        assert 'Can I sue for X?' in payload['prompt']
        assert 'Yes, you can sue.' in payload['prompt']
        assert 'What about Y?' in payload['prompt']
        assert '[USER]' in payload['prompt']
        assert '[ASSISTANT]' in payload['prompt']

    def test_old_bug_only_had_last_user_message(self):
        """This test documents the old bug for regression purposes."""
        client = mod._RitaHttpClient('http://localhost:8000', 'key', timeout=5.0)
        client._session = FakeSession()

        messages = [
            {'role': 'user', 'content': 'First question'},
            {'role': 'assistant', 'content': 'First answer'},
            {'role': 'user', 'content': 'Second question'},
        ]
        client.chat_completions_create(
            model='gpt-4o', messages=messages, temperature=0.7, max_tokens=100,
            top_p=None, stop=None, stream=False, tools=None, tool_choice=None,
            response_format=None, seed=None, presence_penalty=None, frequency_penalty=None,
        )

        payload = client._session.captured['payload']
        # OLD BUG: prompt would only contain "Second question"
        # FIXED: prompt contains full conversation
        assert 'Second question' in payload['prompt']
        assert 'First question' in payload['prompt']
        assert 'First answer' in payload['prompt']


class TestEmbeddings:
    """embeddings.create() was NotImplementedError in old sdk.py — now fully implemented."""

    def test_embeddings_returns_proper_structure(self):
        client = mod._RitaHttpClient('http://localhost:8000', 'key', timeout=5.0)
        client._session = MagicMock()
        client._session.post.side_effect = lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("no rita"))

        result = client.embeddings_create(input=['hello world'], model='text-embedding-3-small')

        assert 'data' in result
        assert 'embedding' in result['data'][0]
        assert len(result['data'][0]['embedding']) == 1536
        assert result['usage']['prompt_tokens'] > 0

    def test_embeddings_single_string(self):
        client = mod._RitaHttpClient('http://localhost:8000', 'key', timeout=5.0)
        client._session = MagicMock()
        client._session.post.side_effect = lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("no rita"))

        result = client.embeddings_create(input='single string', model='text-embedding-3-small')
        assert len(result['data']) == 1

    def test_no_notimplementederror(self):
        client = mod._RitaHttpClient('http://localhost:8000', 'key', timeout=5.0)
        client._session = MagicMock()
        client._session.post.side_effect = lambda *a, **kw: (_ for _ in ()).throw(httpx.ConnectError("no rita"))

        # Should not raise NotImplementedError (old bug)
        result = client.embeddings_create(input='test', model='text-embedding-3-small')
        assert result is not None


class TestOpenAIInterface:
    """Verify OpenAI class has identical interface to official openai.OpenAI."""

    def test_rita_mode_instantiation(self):
        client = OpenAI(api_key='ritakey_xxx', base_url='http://localhost:8000')
        assert client._is_rita is True
        assert client._api_key == 'ritakey_xxx'
        assert client._base_url == 'http://localhost:8000'

    def test_direct_mode_instantiation(self):
        # Should use real openai package (skipped if not installed)
        try:
            client = OpenAI(api_key='sk-xxx')
            assert client._is_rita is False
            assert client._real is not None
        except ImportError:
            pytest.skip("openai package not installed in test env")

    def test_chat_namespace_exists(self):
        client = OpenAI(api_key='ritakey_xxx', base_url='http://localhost:8000')
        assert hasattr(client, 'chat')
        assert hasattr(client.chat, 'create')

    def test_embeddings_namespace_exists(self):
        client = OpenAI(api_key='ritakey_xxx', base_url='http://localhost:8000')
        assert hasattr(client, 'embeddings')
        assert hasattr(client.embeddings, 'create')

    def test_models_namespace_exists(self):
        client = OpenAI(api_key='ritakey_xxx', base_url='http://localhost:8000')
        assert hasattr(client, 'models')


class TestAnthropicInterface:
    def test_rita_mode_instantiation(self):
        client = Anthropic(api_key='ritakey_xxx', base_url='http://localhost:8000')
        assert client._is_rita is True
        assert hasattr(client, 'messages')
        assert hasattr(client.messages, 'create')

    def test_messages_create_interface(self):
        import inspect
        client = Anthropic(api_key='ritakey_xxx', base_url='http://localhost:8000')
        sig = inspect.signature(client.messages.create)
        params = list(sig.parameters.keys())
        assert 'model' in params
        assert 'messages' in params
        assert 'max_tokens' in params
        assert 'system' in params
        assert 'temperature' in params


class TestChatCompletionsParams:
    """All OpenAI params should be accepted and passed through."""

    def test_all_params_passed_through(self):
        client = mod._RitaHttpClient('http://localhost:8000', 'key', timeout=5.0)
        client._session = FakeSession()

        client.chat_completions_create(
            model='gpt-4o',
            messages=[{'role': 'user', 'content': 'hi'}],
            temperature=0.5,
            max_tokens=50,
            top_p=0.9,
            stop=['END'],
            stream=False,
            tools=[{'type': 'function', 'function': {'name': 'test', 'parameters': {}}}],
            tool_choice='auto',
            response_format={'type': 'json_object'},
            seed=42,
            presence_penalty=0.5,
            frequency_penalty=0.5,
        )

        payload = client._session.captured['payload']
        assert payload['temperature'] == 0.5
        assert payload['max_tokens'] == 50
        assert payload['top_p'] == 0.9
        assert payload['stop'] == ['END']
        assert payload['tools'] is not None
        assert payload['tool_choice'] == 'auto'
        assert payload['response_format'] == {'type': 'json_object'}
        assert payload['seed'] == 42
        assert payload['presence_penalty'] == 0.5
        assert payload['frequency_penalty'] == 0.5


class TestOpenAIResponseFormat:
    """Response must be in official OpenAI API format (ChatCompletion)."""

    def test_response_has_required_fields(self):
        client = mod._RitaHttpClient('http://localhost:8000', 'key', timeout=5.0)
        client._session = FakeSession()

        result = client.chat_completions_create(
            model='gpt-4o', messages=[{'role': 'user', 'content': 'hi'}],
            temperature=0.7, max_tokens=10, top_p=None, stop=None, stream=False,
            tools=None, tool_choice=None, response_format=None, seed=None,
            presence_penalty=None, frequency_penalty=None,
        )

        assert 'id' in result
        assert result['id'].startswith('chatcmpl_')
        assert 'object' in result
        assert result['object'] == 'chat.completion'
        assert 'choices' in result
        assert len(result['choices']) == 1
        assert 'message' in result['choices'][0]
        assert result['choices'][0]['message']['role'] == 'assistant'
        assert 'content' in result['choices'][0]['message']
        assert 'usage' in result
        assert 'prompt_tokens' in result['usage']
        assert 'completion_tokens' in result['usage']
        assert 'total_tokens' in result['usage']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
