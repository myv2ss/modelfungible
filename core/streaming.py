# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Streaming Response Handler — SSE (Server-Sent Events) for LLM responses.

Usage:
    from modelfungible.core.streaming import stream_execute

    async def execute_stream(request: ExecuteRequest):
        async def generator():
            async for event in stream_execute(request, model_adapter):
                yield f"data: {json.dumps(event)}\n\n"
        return StreamingResponse(generator(), media_type="text/event-stream")
"""
from __future__ import annotations

import json, time
from typing import AsyncGenerator, Optional, Callable, Any
from dataclasses import dataclass


@dataclass
class StreamEvent:
    """A single streaming event."""
    type: str              # delta | done | error | metadata | usage
    delta: str = ""        # text delta (for type=delta)
    content: str = ""       # full content (for type=done)
    model_id: str = ""
    provider: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str = ""
    error: str = ""
    cached: bool = False
    router_mode: str = ""
    metadata: dict = None

    def to_json(self) -> str:
        d = {"type": self.type}
        if self.delta:
            d["delta"] = self.delta
        if self.content:
            d["content"] = self.content
        if self.model_id:
            d["model_id"] = self.model_id
        if self.provider:
            d["provider"] = self.provider
        if self.latency_ms:
            d["latency_ms"] = self.latency_ms
        if self.cost_usd:
            d["cost_usd"] = self.cost_usd
        if self.input_tokens:
            d["input_tokens"] = self.input_tokens
        if self.output_tokens:
            d["output_tokens"] = self.output_tokens
        if self.finish_reason:
            d["finish_reason"] = self.finish_reason
        if self.error:
            d["error"] = self.error
        if self.cached:
            d["cached"] = True
        if self.router_mode:
            d["router_mode"] = self.router_mode
        if self.metadata:
            d["metadata"] = self.metadata
        return json.dumps(d)


async def stream_execute(
    prompt: str,
    system_prompt: str,
    model_adapter: Any,
    model_id: str,
    temperature: float = 0.7,
    max_tokens: int = 1024,
    timeout: int = 120,
    on_complete: Optional[Callable[[StreamEvent], None]] = None,
) -> AsyncGenerator[StreamEvent, None]:
    """
    Execute a streaming LLM call and yield events as they arrive.

    Yields StreamEvent objects:
    - metadata: first event with model info
    - delta: text chunks as they arrive
    - done: final event with full content + usage stats
    - error: if something goes wrong

    Usage:
        async for event in stream_execute(prompt, system, adapter, model_id):
            if event.type == "delta":
                print(event.delta, end="", flush=True)
            elif event.type == "done":
                print(f"\\nCost: ${event.cost_usd:.6f}")
    """
    t0 = time.time()
    accumulated = ""
    input_tokens_est = max(1, len(prompt) // 4)
    output_tokens_est = 0
    error_occurred = False

    # Emit metadata event first
    yield StreamEvent(type="metadata", model_id=model_id, router_mode="direct")

    try:
        # The adapter should have a stream() method
        if hasattr(model_adapter, "stream"):
            # Native streaming support
            async for chunk in model_adapter.stream(
                prompt=prompt, model=model_id, system_prompt=system_prompt,
                temperature=temperature, max_tokens=max_tokens,
            ):
                accumulated += chunk
                yield StreamEvent(type="delta", delta=chunk)
        else:
            # Fallback: simulate streaming by calling regular method and yielding chunks
            raw = model_adapter.call(
                prompt=prompt, model=model_id,
                system_prompt=system_prompt,
                temperature=temperature, max_tokens=max_tokens,
            )
            # Extract content from response
            content = ""
            if isinstance(raw, dict):
                choices = raw.get("choices", [{}])
                content = choices[0].get("message", {}).get("content", "")
                usage = raw.get("usage", {})
                input_tokens_est = usage.get("prompt_tokens", input_tokens_est)
                output_tokens_est = usage.get("completion_tokens", max_tokens // 2)
            else:
                content = str(raw)

            # Yield in small chunks to simulate streaming
            words = content.split(" ")
            for i, word in enumerate(words):
                delta = word + (" " if i < len(words) - 1 else "")
                accumulated += delta
                yield StreamEvent(type="delta", delta=delta)
                # Small yield to not block
                await _async_sleep(0.005)

    except Exception as e:
        error_occurred = True
        yield StreamEvent(type="error", error=str(e))
        return

    latency_ms = int((time.time() - t0) * 1000)
    output_tokens_est = max(output_tokens_est, len(accumulated) // 4)

    # Get cost estimate
    cost_usd = 0.0
    if hasattr(model_adapter, "estimate_cost"):
        cost_usd = model_adapter.estimate_cost(input_tokens_est, output_tokens_est)
    elif hasattr(model_adapter, "cost_per_1k"):
        cost_usd = (input_tokens_est / 1000 + output_tokens_est / 1000) * model_adapter.cost_per_1k

    final_event = StreamEvent(
        type="done",
        content=accumulated,
        model_id=model_id,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        input_tokens=input_tokens_est,
        output_tokens=output_tokens_est,
        finish_reason="stop",
        cached=False,
    )
    yield final_event

    if on_complete:
        on_complete(final_event)


async def _async_sleep(seconds: float):
    """Minimal async sleep without importing asyncio."""
    import asyncio
    await asyncio.sleep(seconds)


# ─── SSE Helpers ───────────────────────────────────────────────────────────────

def sse_formatter(event: StreamEvent) -> str:
    """Format a StreamEvent as an SSE data frame."""
    return f"data: {event.to_json()}\n\n"


def sse_done() -> str:
    """Send SSE stream termination."""
    return "data: [DONE]\n\n"


class SSEConsumer:
    """
    Client-side SSE consumer. Use to iterate over SSE responses.

    Usage:
        async for event in SSEConsumer(response_object).events():
            if event.type == "delta":
                print(event.delta, end="")
            elif event.type == "done":
                print(f"\\nTotal: ${event.cost_usd:.6f}")
    """

    def __init__(self, response):
        self.response = response

    async def events(self) -> AsyncGenerator[StreamEvent, None]:
        import asyncio
        buffer = ""
        async for chunk in self.response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                if frame.startswith("data: "):
                    data = frame[6:]
                    if data == "[DONE]":
                        return
                    try:
                        d = json.loads(data)
                        yield StreamEvent(
                            type=d.get("type", "delta"),
                            delta=d.get("delta", ""),
                            content=d.get("content", ""),
                            model_id=d.get("model_id", ""),
                            latency_ms=d.get("latency_ms", 0),
                            cost_usd=d.get("cost_usd", 0.0),
                            input_tokens=d.get("input_tokens", 0),
                            output_tokens=d.get("output_tokens", 0),
                            finish_reason=d.get("finish_reason", ""),
                            error=d.get("error", ""),
                            cached=d.get("cached", False),
                        )
                    except json.JSONDecodeError:
                        continue
