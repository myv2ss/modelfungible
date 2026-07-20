# Adapters package — ModelFungible / Rita
# Each adapter implements the BaseAdapter interface for a different provider.

from modelfungible.adapters.base import BaseAdapter, AdapterError, parse_json_output, ParsedOutput
from modelfungible.adapters.openai import OpenAIAdapter
from modelfungible.adapters.anthropic import AnthropicAdapter
from modelfungible.adapters.groq import GroqAdapter
from modelfungible.adapters.minimax import MiniMaxAdapter
from modelfungible.adapters.moonshot import MoonshotAdapter
from modelfungible.adapters.glm import GLMAdapter
from modelfungible.adapters.owen import OwenAdapter
from modelfungible.adapters.custom import CustomAdapter, ProviderRegistry, get_default_registry

__all__ = [
    # Core
    "BaseAdapter",
    "AdapterError",
    "parse_json_output",
    "ParsedOutput",
    # Providers
    "OpenAIAdapter",
    "AnthropicAdapter",
    "GroqAdapter",
    "MiniMaxAdapter",
    "MoonshotAdapter",
    "GLMAdapter",
    "OwenAdapter",
    # Self-service
    "CustomAdapter",
    "ProviderRegistry",
    "get_default_registry",
]
