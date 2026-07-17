#!/usr/bin/env python3
"""
Groq Adapter — ModelFungible

Implements BaseAdapter for Groq's OpenAI-compatible API.
Groq provides free tier with Llama and Qwen models — excellent for benchmarking.
"""
from __future__ import annotations
import os

from modelfungible.adapters.openai import OpenAIAdapter


class GroqAdapter(OpenAIAdapter):
    """
    Groq API adapter (OpenAI-compatible endpoint).

    Uses the same interface as OpenAIAdapter but with Groq's endpoint.
    Free tier available with llama-3.1-8b-instant and llama-3.3-70b-versatile.
    """

    provider_name = "groq"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 45,
        **kwargs,
    ):
        super().__init__(
            api_key=api_key or os.environ.get("GROQ_API_KEY", ""),
            base_url="https://api.groq.com/openai/v1",
            timeout=timeout,
            **kwargs,
        )


__all__ = ["GroqAdapter"]
