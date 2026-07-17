# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Ollama adapter — local model inference.

Connects to a locally running Ollama server (http://localhost:11434).
Supports any Ollama model: llama3, qwen2.5, mistral, etc.

Usage:
    adapter = OllamaAdapter(api_key="ignored", model_id="llama3")
    result = adapter.call("Pick AAPL or MSFT")
"""
import json
import requests
from modelfungible.adapters.base import BaseAdapter, parse_json_output, ParsedOutput, AdapterError


class OllamaAdapter(BaseAdapter):
    """
    Ollama local inference adapter.

    Ollama runs locally, so API key is not used but kept for interface compliance.
    The server must be running: `ollama serve`
    """

    provider_name = "ollama"

    def __init__(self, api_key: str = "localhost", model_id: str = "llama3", **kwargs):
        """
        Args:
            api_key: Not used (Ollama has no auth by default). Pass any string.
            model_id: Ollama model name (e.g. "llama3", "qwen2.5:7b")
        """
        super().__init__(api_key=api_key, model_id=model_id)
        self.model_id = model_id
        self.base_url = kwargs.get("base_url", "http://localhost:11434")
        self.timeout = kwargs.get("timeout", 120)

    def call(
        self,
        prompt: str,
        model: str,
        system_prompt=None,
        temperature: float = 0.1,
        max_tokens: int = 2000,
        **kwargs,
    ) -> dict:
        """
        Call Ollama /api/generate.
        
        Args:
            prompt:       user prompt
            model:        model ID (unused — uses self.model_id)
            system_prompt: system prompt (prepended to prompt)
            temperature:  sampling temperature
            max_tokens:   max output tokens
            
        Returns:
            Parsed JSON dict from model output.
        """
        # Build full prompt
        full_prompt = prompt
        if system_prompt:
            full_prompt = f"{system_prompt}\n\n{prompt}"

        payload = {
            "model": self.model_id,
            "prompt": full_prompt,
            "stream": False,
            "temperature": temperature,
            "options": {
                "num_predict": max_tokens,
                "top_p": kwargs.get("top_p", 0.9),
            },
        }

        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            self._raise_error("timeout", f"Ollama request timed out after {self.timeout}s")
        except requests.exceptions.ConnectionError:
            self._raise_error("server_error", "Cannot connect to Ollama. Is `ollama serve` running?")
        except requests.exceptions.HTTPError as e:
            self._raise_error("server_error", f"Ollama HTTP error: {e}")

        # Parse response
        raw = json.dumps(data)
        content = data.get("response", "")
        parsed = parse_json_output(content.strip())
        usage = {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
        }
        return ParsedOutput(parsed, raw=raw, usage=usage)


__all__ = ["OllamaAdapter"]
