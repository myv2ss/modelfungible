# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Owen Adapter — ModelFungible / Rita

Implements BaseAdapter for Owen (provider-agnostic OpenAI-compatible API).
Supports any OpenAI-compatible endpoint — configure base_url at runtime.

Usage:
    from modelfungible.adapters import OwenAdapter
    adapter = OwenAdapter(
        api_key="your-key",
        base_url="https://api.owen.ai/v1"   # Owen's endpoint
    )
    result = adapter.call("Explain quantum entanglement", model="owen-chat")
"""

from __future__ import annotations

from modelfungible.adapters.openai import OpenAIAdapter


class OwenAdapter(OpenAIAdapter):
    """
    Owen API adapter — generic OpenAI-compatible endpoint.

    Owen is a flexible adapter that works with any OpenAI-compatible API.
    Configure base_url to point to your specific provider.

    If Owen is a specific hosted service, replace base_url accordingly.
    Default: https://api.owen.ai/v1

    Environment: OWEN_API_KEY
    """

    provider_name = "owen"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.owen.ai/v1",
        timeout: int = 30,
        **kwargs,
    ):
        import os as _os
        super().__init__(
            api_key=api_key or _os.environ.get("OWEN_API_KEY", ""),
            base_url=base_url,
            timeout=timeout,
            **kwargs,
        )


__all__ = ["OwenAdapter"]
