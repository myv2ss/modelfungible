# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
MiniMax Adapter — ModelFungible / Rita

Implements BaseAdapter for MiniMax's Moonshot-compatible API.
MiniMax offers competitive pricing with strong Chinese LLM capabilities.
Free tier available with abab6.5s models.

Usage:
    from modelfungible.adapters import MiniMaxAdapter
    adapter = MiniMaxAdapter(api_key="your-minimax-key")
    result = adapter.call("Explain quantum entanglement", model="abab6.5s-chat")
"""

from __future__ import annotations

from modelfungible.adapters.openai import OpenAIAdapter


class MiniMaxAdapter(OpenAIAdapter):
    """
    MiniMax API adapter (Moonshot-compatible endpoint).

    API docs: https://www.minimaxi.com/document
    Free tier: abab6.5s models available.

    Environment: MINIMAX_API_KEY
    """

    provider_name = "minimax"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 30,
        **kwargs,
    ):
        import os as _os
        super().__init__(
            api_key=api_key or _os.environ.get("MINIMAX_API_KEY", ""),
            base_url="https://api.minimax.chat/v1",
            timeout=timeout,
            **kwargs,
        )


__all__ = ["MiniMaxAdapter"]
