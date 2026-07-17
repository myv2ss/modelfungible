# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Vertex AI adapter — Google Cloud Vertex AI model inference.

Supports:
- Claude models via Vertex AI
- Gemini models via Vertex AI
- Any text model deployed on Vertex AI Endpoints

Authentication:
- API key: pass api_key=YOUR_API_KEY
- Service account: set GOOGLE_APPLICATION_CREDENTIALS env var

Usage:
    adapter = VertexAIAdapter(
        api_key="...",
        model_id="claude-3-5-sonnet",
        project="my-gcp-project",
        location="us-central1",
    )
    result = adapter.call("Pick AAPL or MSFT")
"""
import json
import requests
from modelfungible.adapters.base import BaseAdapter, parse_json_output, ParsedOutput, AdapterError


class VertexAIAdapter(BaseAdapter):
    """
    Google Cloud Vertex AI inference adapter.
    """

    provider_name = "vertexai"

    def __init__(
        self,
        api_key: str,
        model_id: str,
        project: str,
        location: str = "us-central1",
        **kwargs,
    ):
        """
        Args:
            api_key: Google API key or access token for service accounts.
            model_id: Vertex AI model ID (e.g. "claude-3-5-sonnet", "gemini-1.5-pro")
            project: GCP project ID
            location: GCP region (e.g. "us-central1", "europe-west4")
        """
        self.project = project
        self.location = location
        self.model_id = model_id
        super().__init__(api_key=api_key, model_id=model_id)
        publisher = self._get_publisher()
        self.base_url = (
            f"https://{location}-aiplatform.googleapis.com/v1"
            f"/projects/{project}/locations/{location}"
            f"/publishers/{publisher}/models/{model_id}:predict"
        )
        self.timeout = kwargs.get("timeout", 120)

    def _get_publisher(self) -> str:
        if self.model_id.startswith("claude"):
            return "anthropic"
        elif self.model_id.startswith("gemini"):
            return "google"
        elif self.model_id.startswith("meta"):
            return "meta"
        elif self.model_id.startswith("mistral"):
            return "mistralai"
        return "openai"

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
        Call Vertex AI prediction endpoint.

        Args:
            prompt:       user prompt
            model:        model ID (unused — uses self.model_id)
            system_prompt: optional system instruction
            temperature:  sampling temperature
            max_tokens:   max output tokens

        Returns:
            Parsed JSON dict from model output.
        """
        # Build message content
        content = prompt
        if system_prompt:
            content = f"System: {system_prompt}\n\nUser: {prompt}"

        instance = {"prompt": content}

        payload = {
            "instances": [instance],
            "parameters": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
                "topP": kwargs.get("top_p", 0.9),
                "topK": kwargs.get("top_k", 40),
            },
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(
                self.base_url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.Timeout:
            self._raise_error("timeout", f"Vertex AI request timed out after {self.timeout}s")
        except requests.exceptions.HTTPError as e:
            body = e.response.text[:200] if e.response else ""
            self._raise_error("server_error", f"Vertex AI HTTP error {e.response.status_code}: {body}")
        except requests.exceptions.ConnectionError:
            self._raise_error("server_error", "Cannot connect to Vertex AI. Check network and credentials.")

        # Parse response
        raw = json.dumps(data)
        try:
            predictions = data.get("predictions", [])
            if isinstance(predictions, list) and len(predictions) > 0:
                pred = predictions[0]
                if isinstance(pred, dict):
                    content = pred.get("content", pred.get("text", str(pred)))
                else:
                    content = str(pred)
            else:
                content = str(data.get("prediction", ""))
        except Exception:
            content = str(data)

        parsed = parse_json_output(content.strip())
        usage = {
            "prompt_tokens": data.get("metadata", {}).get("tokenCount", 0),
            "completion_tokens": data.get("metadata", {}).get("outputTokenCount", 0),
        }
        return ParsedOutput(parsed, raw=raw, usage=usage)


__all__ = ["VertexAIAdapter"]
