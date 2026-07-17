# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
ModelFungible Enterprise — Self-hosted enterprise components.

Modules:
    license          — License key generation and validation
    adapters.ollama  — Ollama local model adapter
    adapters.vertexai — Google Vertex AI adapter
    admin_cli       — Admin CLI (modelfungible-admin)

Usage:
    from modelfungible.enterprise import LicenseKey, OllamaAdapter, VertexAIAdapter
"""

from modelfungible.enterprise.license import LicenseKey, LicenseGenerator
from modelfungible.enterprise.adapters.ollama import OllamaAdapter
from modelfungible.enterprise.adapters.vertexai import VertexAIAdapter

__all__ = [
    "LicenseKey",
    "LicenseGenerator",
    "OllamaAdapter",
    "VertexAIAdapter",
]
