#!/usr/bin/env python3
# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
ModelFungible SDK — Usage Examples

Shows how to use ModelFungible as a drop-in replacement for OpenAI and Anthropic SDKs.

Setup:
    export MODELFUNGIBLE_BASE_URL="https://api.company.com"
    export MODELFUNGIBLE_API_KEY="YOUR_SESSION_TOKEN"

Then copy this file and adapt the examples.
"""

# =============================================================================
# Example 1: OpenAI-Compatible Client
# =============================================================================
# Replace: from openai import OpenAI
# With:    from modelfungible.sdk import ModelFungible
#
# Everything else stays the same!

from modelfungible.sdk import ModelFungible

# Initialize (same as OpenAI)
client = ModelFungible(
    base_url="http://localhost:8000",
    api_key="admin/changeme",   # ModelFungible session token
    # timeout=60,                # optional, default 60s
    # max_retries=3,           # optional, default 3
)

# All these calls look exactly like OpenAI:
response = client.chat.completions.create(
    model="claude-production",          # ModelFungible model name
    messages=[
        {"role": "system", "content": "You are a code reviewer."},
        {"role": "user", "content": "Review this function for bugs:\n\ndef foo(n):\n    return 1/n"},
    ],
    temperature=0.3,
    max_tokens=500,
)

# Response looks like OpenAI ChatCompletion
print(f"Model used:    {response.model}")         # claude-3.5-sonnet
print(f"Output:        {response.choices[0].message.content}")
print(f"Cost:          ${response.cost_usd:.6f}")  # ModelFungible extra
print(f"Latency:       {response.ms}ms")           # ModelFungible extra
print(f"Router mode:   {response.router_mode}")    # which mode was used
print(f"Total tokens:  {response.usage.total_tokens}")


# =============================================================================
# Example 2: Streaming Response
# =============================================================================
stream = client.chat.completions.create(
    model="claude-production",
    messages=[{"role": "user", "content": "Explain quantum entanglement in one sentence."}],
    stream=True,
    max_tokens=100,
)

for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.get("content"):
        print(delta["content"], end="", flush=True)
print()


# =============================================================================
# Example 3: Anthropic-Compatible Client
# =============================================================================
# Replace: from anthropic import Anthropic
# With:    from modelfungible.sdk import Anthropic

from modelfungible.sdk import Anthropic

ac = Anthropic(
    base_url="http://localhost:8000",
    api_key="admin/changeme",
)

response = ac.messages.create(
    model="claude-production",
    max_tokens=500,
    messages=[{"role": "user", "content": "Summarize the key findings of this research paper..."}],
    system="You are a research assistant.",
)

# Response looks like Anthropic Message
print(f"Output: {response.content[0].text}")
print(f"Model:  {response.model}")
print(f"Cost:   ${response.cost_usd:.6f}")
print(f"Latency: {response.ms}ms")


# =============================================================================
# Example 4: Smart Routing Modes
# =============================================================================
# Set the mode via ModelFungible gateway (passed to /api/execute)
# Modes: balanced (default), fastest, cheapest, capability

# Balanced (default): weighted score of speed + cost
balanced = client.chat.completions.create(
    model="balanced",   # special model name triggers balanced routing
    messages=[{"role": "user", "content": "Hello"}],
)
print(f"Balanced → {balanced.model_name} (${balanced.cost_usd:.6f}, {balanced.latency_ms}ms)")

# Cheapest: lowest cost model
cheap = client.chat.completions.create(
    model="cheapest",   # triggers cheapest routing
    messages=[{"role": "user", "content": "Hello"}],
)
print(f"Cheapest → {cheap.model_name} (${cheap.cost_usd:.6f})")

# Fastest: lowest latency model
fast = client.chat.completions.create(
    model="fastest",
    messages=[{"role": "user", "content": "Hello"}],
)
print(f"Fastest → {fast.model_name} ({fast.latency_ms}ms)")


# =============================================================================
# Example 5: Environment Variables
# =============================================================================
# Instead of passing base_url and api_key explicitly:
#   export MODELFUNGIBLE_BASE_URL="http://localhost:8000"
#   export MODELFUNGIBLE_API_KEY="admin/changeme"
#
# Then simply:
from modelfungible.sdk import ModelFungible as MF
client = MF()   # reads from environment
