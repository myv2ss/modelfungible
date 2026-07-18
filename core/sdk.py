# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
ModelFungible SDK — OpenAI and Anthropic compatible drop-in replacement.

Usage (OpenAI compatible):
    from modelfungible.sdk import ModelFungible
    client = ModelFungible(
        base_url="https://api.company.com",
        api_key="YOUR_SESSION_TOKEN"   # or Bearer token
    )
    # Drop-in: same interface as openai.OpenAI
    response = client.chat.completions.create(
        model="claude-production",      # gateway model name
        messages=[{"role": "user", "content": "Hello"}]
    )
    print(response.choices[0].message.content)

Usage (Anthropic compatible):
    from modelfungible.sdk import Anthropic
    client = Anthropic(
        base_url="https://api.company.com",   # ModelFungible gateway
        api_key="YOUR_SESSION_TOKEN"
    )
    # Drop-in: same interface as anthropic.Anthropic
    response = client.messages.create(
        model="claude-production",
        max_tokens=1024,
        messages=[{"role": "user", "content": "Hello"}]
    )
"""
from __future__ import annotations

import os, json, time
from typing import Optional, Any, Union, Literal, Iterator
from dataclasses import dataclass, field
from urllib.parse import urljoin

import requests

# ─── Base Client ────────────────────────────────────────────────────────────────

@dataclass
class ModelFungibleConfig:
    base_url: str
    api_key: str
    timeout: int = 60
    max_retries: int = 3
    default_headers: dict = field(default_factory=dict)

    def headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        h.update(self.default_headers)
        return h


class BaseModelFungibleClient:
    """Shared HTTP logic for both OpenAI and Anthropic compatible clients."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 60, max_retries: int = 3, **kwargs):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        # Allow override of default headers via kwargs
        for k, v in kwargs.items():
            self._session.headers[k] = v

    def _post(self, path: str, payload: dict, stream: bool = False) -> requests.Response:
        url = f"{self.base_url}{path}"
        for attempt in range(self.max_retries):
            try:
                r = self._session.post(
                    url, json=payload, timeout=self.timeout, stream=stream,
                    headers={"Accept": "text/event-stream"} if stream else self._session.headers
                )
                if r.status_code == 503 and attempt < self.max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                return r
            except (requests.ConnectionError, requests.Timeout):
                if attempt < self.max_retries - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise
        return r

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.base_url}{path}"
        r = self._session.get(url, params=params or {}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()


# ─── OpenAI Compatible ─────────────────────────────────────────────────────────

class ModelFungible(BaseModelFungibleClient):
    """
    OpenAI SDK-compatible client using ModelFungible gateway.

    Drop-in replacement for:
        from openai import OpenAI
        client = OpenAI(api_key="...")

    Usage:
        from modelfungible.sdk import ModelFungible
        client = ModelFungible(
            base_url="https://api.company.com",
            api_key="SESSION_TOKEN"
        )
        # Same interface as OpenAI
        response = client.chat.completions.create(
            model="claude-production",
            messages=[...],
            temperature=0.7,
            max_tokens=1024,
            stream=False,
        )
    """

    def __init__(self, **kwargs):
        base_url = kwargs.pop("base_url", os.environ.get("MODELFUNGIBLE_BASE_URL", "http://localhost:8000"))
        api_key = kwargs.pop("api_key", os.environ.get("MODELFUNGIBLE_API_KEY", ""))
        timeout = kwargs.pop("timeout", 60)
        max_retries = kwargs.pop("max_retries", 3)
        super().__init__(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=max_retries, **kwargs)
        self.chat = _ChatCompletions(self)

    @property
    def models(self):
        return _Models(self)

    @property
    def embeddings(self):
        return _Embeddings(self)


class _ChatCompletions:
    """ Implements client.chat.completions namespace. """
    def __init__(self, client: ModelFungible):
        self._client = client

    def create(
        self,
        model: str,
        messages: list[dict],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
        stop: Optional[list[str]] = None,
        stream: bool = False,
        stream_options: Optional[dict] = None,
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[Union[str, dict]] = None,
        response_format: Optional[dict] = None,
        **kwargs,
    ) -> Union[_ChatCompletion, Iterator[_ChatCompletionChunk]]:
        # Map OpenAI params → ModelFungible execute params
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        prompt = next((m["content"] for m in messages if m.get("role") == "user"), "")

        payload = {
            "model": model,
            "prompt": prompt,
            "system": system,
            "temperature": temperature if temperature is not None else 0.7,
            "max_tokens": max_tokens or 1024,
        }
        if stop:
            payload["stop"] = stop
        if top_p:
            payload["top_p"] = top_p
        if response_format:
            payload["response_format"] = response_format
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        # Pass through extra kwargs
        for k, v in kwargs.items():
            if k not in payload and v is not None:
                payload[k] = v

        if stream:
            return self._stream_create(model, payload)
        else:
            return self._sync_create(model, payload)

    def _sync_create(self, model: str, payload: dict) -> _ChatCompletion:
        r = self._client._post("/api/execute", payload, stream=False)
        if r.status_code == 402:
            raise ValueError(f"Cost limit exceeded: {r.json()}")
        if r.status_code == 503:
            raise ValueError(f"All models failed: {r.json().get('error', 'unknown')}")
        r.raise_for_status()
        data = r.json()

        # Map ModelFungible response → OpenAI ChatCompletion shape
        return _ChatCompletion(
            id=f"mf_{data.get('audit_entry_id', 'unknown')}",
            choices=[_Choice(
                index=0,
                message=_Message(role="assistant", content=data.get("output", "")),
                finish_reason="stop",
            )],
            model=data.get("model_id", model),
            usage=_Usage(
                prompt_tokens=data.get("input_tokens_est", 0),
                completion_tokens=data.get("output_tokens_est", 0),
                total_tokens=data.get("input_tokens_est", 0) + data.get("output_tokens_est", 0),
            ),
            ms=data.get("latency_ms", 0),
            cost_usd=data.get("cost", 0.0),
            router_mode=data.get("router_mode", "unknown"),
            model_name=data.get("model_name", ""),
        )

    def _stream_create(self, model: str, payload: dict) -> Iterator[_ChatCompletionChunk]:
        """Streaming via SSE."""
        import sseclient
        r = self._client._post("/api/execute", {**payload, "stream": True}, stream=True)
        r.raise_for_status()
        client = sseclient.SSEClient(r)
        accumulated = ""
        for event in client.events():
            if event.data == "[DONE]":
                break
            try:
                chunk = json.loads(event.data)
                text = chunk.get("delta", "")
                accumulated += text
                yield _ChatCompletionChunk(
                    id=f"mf_chunk",
                    choices=[_StreamChoice(index=0, delta={"content": text}, finish_reason=None)],
                )
            except json.JSONDecodeError:
                continue
        # Final chunk
        yield _ChatCompletionChunk(
            id="mf_done",
            choices=[_StreamChoice(index=0, delta={}, finish_reason="stop")],
        )


class _Models:
    def __init__(self, client: ModelFungible):
        self._client = client

    def list(self) -> dict:
        return self._client._get("/api/state")


class _Embeddings:
    def __init__(self, client: ModelFungible):
        self._client = client

    def create(self, input: Union[str, list], model: str = "text-embedding-3-small", **kwargs) -> dict:
        # embeddings not yet implemented — placeholder
        raise NotImplementedError("Embeddings via ModelFungible gateway not yet implemented")


# ─── OpenAI Response Objects ───────────────────────────────────────────────────

@dataclass
class _Message:
    role: str
    content: str

@dataclass
class _Choice:
    index: int
    message: _Message
    finish_reason: str

@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

@dataclass
class _StreamChoice:
    index: int
    delta: dict
    finish_reason: Optional[str]

@dataclass
class _ChatCompletionChunk:
    id: str
    choices: list[_StreamChoice]

@dataclass
class _ChatCompletion:
    id: str
    choices: list[_Choice]
    model: str
    usage: Optional[_Usage] = None
    ms: int = 0
    cost_usd: float = 0.0
    router_mode: str = ""
    model_name: str = ""

    def __repr__(self):
        return f"<ChatCompletion model={self.model_name} cost=${self.cost_usd:.6f} latency={self.ms}ms>"


# ─── Anthropic Compatible ──────────────────────────────────────────────────────

class Anthropic(BaseModelFungibleClient):
    """
    Anthropic SDK-compatible client using ModelFungible gateway.

    Drop-in replacement for:
        from anthropic import Anthropic
        client = Anthropic(api_key="...")

    Usage:
        from modelfungible.sdk import Anthropic
        client = Anthropic(
            base_url="https://api.company.com",
            api_key="SESSION_TOKEN"
        )
        # Same interface as Anthropic SDK
        response = client.messages.create(
            model="claude-production",
            max_tokens=1024,
            messages=[{"role": "user", "content": "Hello"}]
        )
        print(response.content[0].text)
    """

    def __init__(self, **kwargs):
        base_url = kwargs.pop("base_url", os.environ.get("MODELFUNGIBLE_BASE_URL", "http://localhost:8000"))
        api_key = kwargs.pop("api_key", os.environ.get("MODELFUNGIBLE_API_KEY", ""))
        timeout = kwargs.pop("timeout", 60)
        max_retries = kwargs.pop("max_retries", 3)
        super().__init__(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=max_retries, **kwargs)
        self.messages = _AnthropicMessages(self)


class _AnthropicMessages:
    """ Implements client.messages namespace. """
    def __init__(self, client: Anthropic):
        self._client = client

    def create(
        self,
        model: str,
        max_tokens: int = 1024,
        messages: Optional[list[dict]] = None,
        system: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        stop_sequences: Optional[list[str]] = None,
        stream: bool = False,
        **kwargs,
    ) -> Union[_AnthropicMessage, Iterator[_AnthropicMessageStream]]:
        # Extract user message and system from Anthropic message format
        user_content = ""
        for m in (messages or []):
            if m.get("role") == "user":
                user_content = m.get("content", "")
                break

        payload = {
            "model": model,
            "prompt": user_content,
            "system": system or "",
            "max_tokens": max_tokens,
            "temperature": temperature if temperature is not None else 1.0,  # Anthropic default is 1.0
        }
        if stop_sequences:
            payload["stop"] = stop_sequences
        if top_p:
            payload["top_p"] = top_p

        for k, v in kwargs.items():
            if v is not None:
                payload[k] = v

        if stream:
            return self._stream_create(payload)
        else:
            return self._sync_create(model, payload)

    def _sync_create(self, model: str, payload: dict) -> _AnthropicMessage:
        r = self._client._post("/api/execute", payload, stream=False)
        if r.status_code == 402:
            raise ValueError(f"Cost limit exceeded: {r.json()}")
        if r.status_code == 503:
            raise ValueError(f"All models failed: {r.json().get('error', 'unknown')}")
        r.raise_for_status()
        data = r.json()

        return _AnthropicMessage(
            id=f"mf_{data.get('audit_entry_id', 'unknown')}",
            type="message",
            role="assistant",
            content=[_AnthropicContent(type="text", text=data.get("output", ""))],
            model=data.get("model_id", model),
            usage=_AnthropicUsage(
                input_tokens=data.get("input_tokens_est", 0),
                output_tokens=data.get("output_tokens_est", 0),
            ),
            ms=data.get("latency_ms", 0),
            cost_usd=data.get("cost", 0.0),
            stop_reason="end_turn",
            stop_sequence=None,
        )

    def _stream_create(self, payload: dict) -> Iterator[_AnthropicMessageStream]:
        r = self._client._post("/api/execute", {**payload, "stream": True}, stream=True)
        r.raise_for_status()
        import sseclient
        client = sseclient.SSEClient(r)
        accumulated = ""
        for event in client.events():
            if event.data == "[DONE]":
                break
            try:
                chunk = json.loads(event.data)
                delta = chunk.get("delta", "")
                accumulated += delta
                yield _AnthropicMessageStream(type="content_block_delta", index=0, delta={"type": "text_delta", "text": delta})
            except json.JSONDecodeError:
                continue
        yield _AnthropicMessageStream(type="message_delta", index=0, delta={}, usage=_AnthropicUsage(0, 0))


@dataclass
class _AnthropicContent:
    type: str
    text: str

@dataclass
class _AnthropicUsage:
    input_tokens: int
    output_tokens: int

@dataclass
class _AnthropicMessage:
    id: str
    type: str
    role: str
    content: list[_AnthropicContent]
    model: str
    usage: _AnthropicUsage
    ms: int = 0
    cost_usd: float = 0.0
    stop_reason: str = "end_turn"
    stop_sequence: Optional[str] = None

    def __repr__(self):
        return f"<AnthropicMessage model={self.model} cost=${self.cost_usd:.6f} latency={self.ms}ms>"

@dataclass
class _AnthropicMessageStream:
    type: str
    index: int
    delta: dict
    usage: Optional[_AnthropicUsage] = None



def is_modelfungible_error(r: requests.Response) -> bool:
    """Check if response is a ModelFungible error."""
    return r.status_code >= 400


class SSEClient:
    """Simple SSE client for streaming responses."""
    def __init__(self, response: requests.Response):
        self.response = response
        self._buffer = ""

    def _read_line(self) -> str:
        while True:
            idx = self._buffer.find("\n")
            if idx != -1:
                line = self._buffer[:idx].strip()
                self._buffer = self._buffer[idx+1:]
                if line.startswith("data: "):
                    return line[6:]
                elif line == "":
                    continue
                return line
            chunk = self.response.raw.read(1024, decode_content=True)
            if not chunk:
                return ""
            self._buffer += chunk

    def events(self):
        while True:
            line = self._read_line()
            if line is None or line == "":
                break
            yield type("Event", (), {"data": line})()

