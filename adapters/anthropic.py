# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.
# Commercial use requires a license. Unauthorized use is prohibited.

#!/usr/bin/env python3
"""
Anthropic Adapter — ModelFungible

Implements BaseAdapter for Anthropic's Claude API.
"""
from __future__ import annotations
import os
from typing import Any

import requests

from modelfungible.adapters.base import BaseAdapter, AdapterError, parse_json_output, ParsedOutput


class AnthropicAdapter(BaseAdapter):
    """
    Anthropic Claude adapter.

    Supports claude-3-5-sonnet, claude-3-opus, etc.
    """

    provider_name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: int = 30,
        **kwargs,
    ):
        super().__init__(api_key or os.environ.get("ANTHROPIC_API_KEY", ""), **kwargs)
        self.base_url = base_url.rstrip("/")
        self.timeout  = timeout
        self._session = None

    @property
    def session(self):
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "x-api-key":          self.api_key,
                "Content-Type":        "application/json",
                "anthropic-version":  "2023-06-01",
            })
        return self._session

    def call(
        self,
        prompt: str,
        model: str,
        system_prompt: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        **kwargs,
    ) -> dict:
        # Anthropic uses max_tokens differently (must be >= 1)
        max_tokens = max(max_tokens, 4)

        messages = [{"role": "user", "content": prompt}]
        body = {
            "model":       model,
            "messages":    messages,
            "temperature": temperature,
            "max_tokens":  max_tokens,
        }
        if system_prompt:
            body["system"] = system_prompt
        body.update({k: v for k, v in kwargs.items() if v is not None})

        try:
            r = self.session.post(
                f"{self.base_url}/messages",
                json=body,
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
            # Claude returns content as a list of blocks
            content = data.get("content", [])
            if content and isinstance(content, list):
                raw = content[0].get("text", "")
            else:
                raw = str(content)
            parsed = parse_json_output(raw)
            usage = self.get_usage(data)
            return ParsedOutput(parsed, raw=raw, usage=usage)

        # Error classification
        err_body = r.text[:200]
        if r.status_code == 401:
            self._raise_error("auth", f"Invalid API key: {err_body}")
        elif r.status_code == 403:
            self._raise_error("auth", f"Forbidden: {err_body}")
        elif r.status_code == 429:
            self._raise_error("rate_limit", f"Rate limited: {err_body}")
        elif r.status_code >= 500:
            self._raise_error("upstream_error", f"Upstream error {r.status_code}: {err_body}")
        elif r.status_code == 400:
            self._raise_error("invalid_request", f"Bad request: {err_body}")
        else:
            self._raise_error(f"http_{r.status_code}", f"API error {r.status_code}: {err_body}")

    def get_usage(self, raw_response: Any) -> dict | None:
        try:
            usage = raw_response.get("usage", {})
            return {
                "input_tokens":  usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
        except Exception:
            return None


__all__ = ["AnthropicAdapter"]
