# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Chat POC — Proof-of-concept chat application powered by ModelFungible AIP Gateway.
Demonstrates: streaming, model switching, cost tracking, guardrails, and fallback chains.

Run:
    cp .env.example .env   # edit with your API keys
    python3 app.py

Then open http://localhost:8766
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

from flask import (
    Flask, render_template, request, Response,
    stream_with_context, jsonify, session,
)
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
app.config["JSON_AS_ASCII"] = False

# ── ModelFungible Setup ─────────────────────────────────────────────────────────
# The magic: swap models without changing a single line of chat logic.
# The gateway handles routing, fallback, cost, and audit behind the scenes.

MF_MODE       = os.environ.get("MF_MODE", "balanced")   # fastest | cheapest | balanced
MF_GUARDRAIL = os.environ.get("MF_GUARDRAIL", "")       # comma-separated blocked terms
MF_MAX_LEN    = int(os.environ.get("MF_MAX_LEN", "4000"))  # max output chars

_cost_today  = 0.0
_cost_lock   = __import__("threading").Lock()


def _build_client():
    """Build the drop-in OpenAI-compatible client pointing at our gateway."""
    from modelfungible.core.sdk import ModelFungible

    # Point at our local gateway if it's running, otherwise fall back to direct Groq
    base_url = os.environ.get("MF_BASE_URL", "http://localhost:8765/api")

    return ModelFungible(
        base_url=base_url,
        api_key=os.environ.get("MF_API_KEY", "dev-key"),
    )


def _build_guardrails():
    from modelfungible.enterprise.guardrails import Guardrails, GuardrailConfig
    terms = [t.strip() for t in MF_GUARDRAIL.split(",") if t.strip()]
    if not terms and MF_MAX_LEN >= 0:
        return None
    cfg = GuardrailConfig(
        blocked_terms=terms,
        max_length=MF_MAX_LEN if MF_MAX_LEN > 0 else None,
    )
    return Guardrails(cfg)


def _apply_guardrail(text: str, guardrails) -> tuple[str, bool, str]:
    """Apply guardrails. Returns (filtered_text, passed, reason)."""
    if not guardrails:
        return text, True, "no_guardrails"
    r = guardrails.apply(text)
    return r.filtered_output, r.passed, r.reason


# ── Chat History (in-memory per session) ──────────────────────────────────────

def get_history():
    if "history" not in session:
        session["history"] = []
    return session["history"]


def add_to_history(role: str, content: str, model: str = None,
                   cost: float = None, latency_ms: int = None,
                   guardrail_passed: bool = True, guardrail_reason: str = None):
    msg = {"role": role, "content": content}
    if model:
        msg["model"] = model
    if cost is not None:
        msg["cost_usd"] = round(cost, 6)
    if latency_ms is not None:
        msg["latency_ms"] = latency_ms
    if not guardrail_passed:
        msg["guardrail_flagged"] = True
        msg["guardrail_reason"] = guardrail_reason
    hist = get_history()
    hist.append(msg)
    session["history"] = hist
    if role == "assistant" and cost is not None:
        with _cost_lock:
            global _cost_today
            _cost_today += cost


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    if "history" not in session:
        session["history"] = []
    return render_template("index.html",
                           model=os.environ.get("MF_MODEL", "groq/llama-3.3-70b-versatile"),
                           mode=MF_MODE)


@app.route("/reset", methods=["POST"])
def reset_chat():
    session["history"] = []
    session.modified = True
    return "", 204


@app.route("/stats", methods=["GET"])
def stats():
    return jsonify({
        "cost_today": round(_cost_today, 6),
        "mode": MF_MODE,
        "model": os.environ.get("MF_MODEL", "groq/llama-3.3-70b-versatile"),
    })


@app.route("/chat", methods=["POST"])
def chat():
    """
    Streaming chat endpoint using the ModelFungible OpenAI-compatible SDK.
    Switch models by passing ?model=xxx or changing MF_MODEL env var.
    """
    data = request.get_json() or {}
    user_msg = (data.get("message") or "").strip()
    if not user_msg:
        return jsonify({"error": "message is required"}), 400

    explicit_model = request.args.get("model") or os.environ.get("MF_MODEL", "")
    guardrails = _build_guardrails()

    add_to_history("user", user_msg)

    def generate():
        client = _build_client()

        # Build messages
        messages = [{"role": m["role"], "content": m["content"]} for m in get_history()[:-1]]
        messages.append({"role": "user", "content": user_msg})

        # Build request kwargs
        kwargs = {
            "messages": messages,
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 1024,
        }
        if explicit_model:
            kwargs["model"] = explicit_model

        # Guardrail output filter
        output_filter = {}
        if guardrails:
            output_filter["blocked_terms"] = MF_GUARDRAIL.split(",") if MF_GUARDRAIL else []
            if MF_MAX_LEN > 0:
                output_filter["max_length"] = MF_MAX_LEN
            kwargs["output_filter"] = output_filter

        t0 = time.time()
        accumulated = ""
        model_used = explicit_model or "auto"
        cost_usd = 0.0
        latency_ms = 0
        guardrail_passed = True
        guardrail_reason = ""

        try:
            # The SDK call — same interface as OpenAI, but with gateway superpowers
            stream = client.chat.completions.create(**kwargs)

            for chunk in stream:
                delta = chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content
                if delta:
                    accumulated += delta
                    # SSE: send delta immediately to browser
                    yield f"data: {delta}\n\n".encode()

            latency_ms = int((time.time() - t0) * 1000)

            # Extra fields on the response object (not standard OpenAI)
            if hasattr(stream, "_mf_meta"):
                meta = stream._mf_meta
                cost_usd    = meta.get("cost_usd", 0)
                model_used  = meta.get("model_name", model_used)
                guardrail_passed = meta.get("guardrail_passed", True)
                guardrail_reason = meta.get("guardrail_reason", "")

            # Apply guardrails server-side as a fallback
            if guardrails and guardrail_passed:
                filtered, guardrail_passed, guardrail_reason = _apply_guardrail(accumulated, guardrails)
                if not guardrail_passed:
                    accumulated = filtered

        except Exception as e:
            yield f"data: [ERROR] {str(e)}\n\n".encode()
            add_to_history("assistant", f"Error: {str(e)}",
                           model=model_used, cost=0, latency_ms=int((time.time()-t0)*1000))
            return

        # Cost from response or estimate
        if cost_usd == 0 and accumulated:
            # rough estimate if not populated
            toks = len(accumulated) // 4
            cost_usd = toks / 1000 * 0.0007  # roughest estimate

        add_to_history(
            "assistant", accumulated,
            model=model_used, cost=cost_usd, latency_ms=latency_ms,
            guardrail_passed=guardrail_passed, guardrail_reason=guardrail_reason,
        )

        # Send metadata as final SSE event
        meta = {
            "model": model_used,
            "cost": round(cost_usd, 6),
            "latency_ms": latency_ms,
            "guardrail_passed": guardrail_passed,
            "guardrail_reason": guardrail_reason,
            "total_cost_today": round(_cost_today, 6),
        }
        import json as _json
        yield f"data: [META] {_json.dumps(meta)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    resp = Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
    return resp


if __name__ == "__main__":
    print("=" * 60)
    print("  ModelFungible Chat POC")
    print("  Open http://localhost:8766")
    print("  Mode:", MF_MODE)
    print("  Model:", os.environ.get("MF_MODEL", "groq/llama-3.3-70b-versatile"))
    print("  Guardrail terms:", MF_GUARDRAIL or "(none)")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8766, debug=False, threaded=True)
