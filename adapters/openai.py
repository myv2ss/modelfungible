#!/usr/bin/env python3
"""
OpenAI Adapter — ModelFungible

Implements the BaseAdapter interface for OpenAI-compatible APIs
(includes OpenAI, Azure OpenAI, and any OpenAI-compatible server).
"""
from __future__ import annotations
import os
from typing import Any

import requests

from modelfungible.adapters.base import BaseAdapter, AdapterError, parse_json_output, ParsedOutput


class OpenAIAdapter(BaseAdapter):
    """
    OpenAI-compatible model adapter.

    Supports:
    - OpenAI models (gpt-4o, gpt-4o-mini, etc.)
    - Azure OpenAI (via base_url)
    - Any OpenAI-compatible API (Groq, Together, etc.)
    """

    provider_name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout: int = 30,
        **kwargs,
    ):
        super().__init__(api_key or os.environ.get("OPENAI_API_KEY", ""), **kwargs)
        self.base_url = base_url.rstrip("/")
        self.timeout  = timeout
        self._session = None

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type":  "application/json",
            })
        return self._session

    def call(
        self,
        prompt: str,
        model: str,
        system_prompt: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 500,
        **kwargs,
    ) -> dict:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model":       model,
            "messages":    messages,
            "temperature": temperature,
            "max_tokens":  max_tokens,
        }
        # Allow caller to override
        payload.update({k: v for k, v in kwargs.items() if v is not None})

        try:
            r = self.session.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=self.timeout,
            )
        except requests.exceptions.Timeout:
            self._raise_error("timeout", f"Request timed out after {self.timeout}s")
        except requests.exceptions.ConnectionError as e:
            self._raise_error("connection", f"Connection failed: {e}")
        except Exception as e:
            self._raise_error("request", f"Request error: {e}")

        if r.status_code == 200:
            data = r.json()
            raw = data["choices"][0]["message"]["content"]
            parsed = parse_json_output(raw)
            usage = self.get_usage(data)
            return ParsedOutput(parsed, raw=raw, usage=usage)

        # Error classification
        err_body = r.text[:200]
        if r.status_code == 401:
            self._raise_error("auth", f"Invalid API key: {err_body}")
        elif r.status_code == 403:
            self._raise_error("auth", f"Forbidden: {err_body}")
        elif r.status_code == 404:
            self._raise_error("model_not_found", f"Model not found: {model}")
        elif r.status_code == 429:
            self._raise_error("rate_limit", f"Rate limited: {err_body}")
        elif r.status_code == 500:
            self._raise_error("server_error", f"OpenAI server error: {err_body}")
        elif r.status_code >= 500:
            self._raise_error("upstream_error", f"Upstream error {r.status_code}: {err_body}")
        elif r.status_code == 400:
            self._raise_error("invalid_request", f"Bad request: {err_body}")
        else:
            self._raise_error(
                f"http_{r.status_code}",
                f"OpenAI API error {r.status_code}: {err_body}"
            )

    def get_usage(self, raw_response: Any) -> dict | None:
        try:
            usage = raw_response.get("usage", {})
            return {
                "prompt_tokens":     usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens":     usage.get("total_tokens", 0),
            }
        except Exception:
            return None


__all__ = ["OpenAIAdapter"]
