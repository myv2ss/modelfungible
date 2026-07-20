# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Moonshot (Kimi) Adapter — ModelFungible / Rita

Implements BaseAdapter for Moonshot AI's Kimi API.
Moonshot offers long-context models (200K context) with strong reasoning.

Usage:
    from modelfungible.adapters import MoonshotAdapter
    adapter = MoonshotAdapter(api_key="your-moonshot-key")
    result = adapter.call("Explain quantum entanglement", model="moonshot-v1-8k")
"""

from __future__ import annotations

from modelfungible.adapters.openai import OpenAIAdapter


class MoonshotAdapter(OpenAIAdapter):
    """
    Moonshot AI (Kimi) API adapter (OpenAI-compatible endpoint).

    API docs: https://platform.moonshot.cn/docs
    Models: moonshot-v1-8k, moonshot-v1-32k, moonshot-v1-128k
    Free tier: Limited requests on free tier.

    Environment: MOONSHOT_API_KEY or KIMI_API_KEY
    """

    provider_name = "moonshot"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 30,
        **kwargs,
    ):
        import os as _os
        super().__init__(
            api_key=api_key or _os.environ.get("MOONSHOT_API_KEY") or _os.environ.get("KIMI_API_KEY", ""),
            base_url="https://api.moonshot.cn/v1",
            timeout=timeout,
            **kwargs,
        )


__all__ = ["MoonshotAdapter"]
