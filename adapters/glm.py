# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
GLM (Zhipu AI) Adapter — ModelFungible / Rita

Implements BaseAdapter for Zhipu AI's GLM API.
GLM offers strong Chinese LLM capabilities and multilingual support.

Usage:
    from modelfungible.adapters import GLMAdapter
    adapter = GLMAdapter(api_key="your-glm-key")
    result = adapter.call("Explain quantum entanglement", model="glm-4")
"""

from __future__ import annotations

from modelfungible.adapters.openai import OpenAIAdapter


class GLMAdapter(OpenAIAdapter):
    """
    Zhipu AI (GLM) API adapter (OpenAI-compatible endpoint).

    API docs: https://open.bigmodel.cn/dev/api
    Models: glm-4, glm-4-flash, glm-4-plus, glm-3-turbo
    Free tier: glm-4-flash has generous free tier.

    Environment: ZHIPU_API_KEY
    """

    provider_name = "glm"

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 30,
        **kwargs,
    ):
        import os as _os
        super().__init__(
            api_key=api_key or _os.environ.get("ZHIPU_API_KEY", ""),
            base_url="https://open.bigmodel.cn/api/paas/v4",
            timeout=timeout,
            **kwargs,
        )


__all__ = ["GLMAdapter"]
