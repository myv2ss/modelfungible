# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Chat POC — Proof-of-concept chat application powered by ModelFungible AIP Gateway.
Demonstrates: streaming, model switching, cost tracking, guardrails, and fallback chains.

Run standalone (no gateway needed):
    cp .env.example .env   # edit GROQ_API_KEY
    python3 app.py

Or with the full ModelFungible gateway running on :8765:
    python3 app.py   # will use gateway automatically

Then open http://localhost:8766
"""
from __future__ import annotations

import os, time, json, threading
from flask import Flask, render_template, request, Response, stream_with_context, jsonify, session
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

# ── Config ────────────────────────────────────────────────────────────────────
MF_MODE       = os.environ.get("MF_MODE", "balanced")
MF_GUARDRAIL  = os.environ.get("MF_GUARDRAIL", "")
MF_MAX_LEN    = int(os.environ.get("MF_MAX_LEN", "4000"))
MF_BASE_URL   = os.environ.get("MF_BASE_URL", "")   # empty = use Groq directly
GROQ_API_KEY  = os.environ.get("GROQ_API_KEY", "")
DIRECT_MODE   = bool(GROQ_API_KEY and not MF_BASE_URL)

_cost_today = 0.0
_cost_lock   = threading.Lock()

# ── Guardrails ────────────────────────────────────────────────────────────────

def _make_guardrails():
    if not MF_GUARDRAIL and MF_MAX_LEN > 0:
        return None
    from modelfungible.enterprise.guardrails import Guardrails, GuardrailConfig
    terms = [t.strip() for t in MF_GUARDRAIL.split(",") if t.strip()]
    cfg = GuardrailConfig(
        blocked_terms=terms,
        max_length=MF_MAX_LEN if MF_MAX_LEN > 0 else None,
    )
    return Guardrails(cfg)

def _apply_guardrail(text, g):
    if not g:
        return text, True, "no_guardrails"
    r = g.apply(text)
    return r.filtered_output, r.passed, r.reason

# ── Groq direct client (used when no gateway) ────────────────────────────────

def _groq_stream(messages, model, temperature, max_tokens, api_key):
    """Yield SSE tokens directly from Groq API."""
    import urllib.request, urllib.error
    payload = {
        "model": model.replace("groq/", ""),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        for line in resp:
            line = line.decode().strip()
            if not line or not line.startswith("data: "):
                continue
            data = line[6:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = (
                    chunk.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content", "")
                )
                if delta:
                    yield delta
            except Exception:
                pass

def _groq_cost(model, text):
    """Rough cost estimate for Groq."""
    out_toks = max(1, len(text) // 4)
    # Groq free tier = $0; fall through silently
    return 0.0

# ── Chat History ───────────────────────────────────────────────────────────────

def get_history():
    if "history" not in session:
        session["history"] = []
    return session["history"]

def add_msg(role, content, model=None, cost=None, latency_ms=None,
            guardrail_passed=True, guardrail_reason=None):
    msg = {"role": role, "content": content}
    if model: msg["model"] = model
    if cost is not None: msg["cost_usd"] = round(cost, 6)
    if latency_ms is not None: msg["latency_ms"] = latency_ms
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
    default_model = os.environ.get("MF_MODEL", "groq/llama-3.3-70b-versatile")
    return render_template("index.html", model=default_model, mode=MF_MODE)

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
        "direct": DIRECT_MODE,
    })

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}
    user_msg = (data.get("message") or "").strip()
    if not user_msg:
        return jsonify({"error": "message is required"}), 400

    explicit_model = request.args.get("model") or os.environ.get("MF_MODEL", "groq/llama-3.3-70b-versatile")
    guardrails = _make_guardrails()

    add_msg("user", user_msg)

    def generate():
        t0 = time.time()
        accumulated = ""
        model_used = explicit_model
        cost_usd = 0.0
        latency_ms = 0
        guardrail_passed = True
        guardrail_reason = ""

        try:
            messages = [{"role": m["role"], "content": m["content"]}
                        for m in get_history()[:-1]]
            messages.append({"role": "user", "content": user_msg})

            if DIRECT_MODE or not MF_BASE_URL:
                # ── Direct Groq (no gateway needed) ────────────────────────
                for delta in _groq_stream(
                    messages=messages,
                    model=explicit_model,
                    temperature=0.7,
                    max_tokens=1024,
                    api_key=GROQ_API_KEY,
                ):
                    accumulated += delta
                    yield f"data: {delta}\n\n".encode()

            else:
                # ── Via ModelFungible gateway ──────────────────────────────
                from modelfungible.core.sdk import ModelFungible
                client = ModelFungible(
                    base_url=MF_BASE_URL.rstrip("/"),
                    api_key=os.environ.get("MF_API_KEY", "dev-key"),
                )
                kwargs = {
                    "messages": messages,
                    "stream": True,
                    "temperature": 0.7,
                    "max_tokens": 1024,
                }
                if explicit_model:
                    kwargs["model"] = explicit_model

                stream = client.chat.completions.create(**kwargs)
                for chunk in stream:
                    delta = (
                        chunk.choices and
                        chunk.choices[0].delta and
                        chunk.choices[0].delta.content
                    )
                    if delta:
                        accumulated += delta
                        yield f"data: {delta}\n\n".encode()

                    # Capture extra fields from gateway
                    if hasattr(chunk, "_mf_meta"):
                        meta = chunk._mf_meta
                        cost_usd   = meta.get("cost_usd", 0)
                        model_used = meta.get("model_name", model_used)
                        guardrail_passed = meta.get("guardrail_passed", True)
                        guardrail_reason = meta.get("guardrail_reason", "")

            latency_ms = int((time.time() - t0) * 1000)

            # Guardrail filter on accumulated output
            if guardrails and guardrail_passed:
                accumulated, guardrail_passed, guardrail_reason = _apply_guardrail(
                    accumulated, guardrails
                )

        except Exception as e:
            yield f"data: [ERROR] {str(e)}\n\n".encode()
            add_msg("assistant", f"Error: {str(e)}",
                    model=model_used, cost=0, latency_ms=int((time.time()-t0)*1000))
            return

        # Cost estimate for direct Groq (free tier = $0)
        if cost_usd == 0 and accumulated:
            cost_usd = _groq_cost(model_used, accumulated)

        add_msg(
            "assistant", accumulated,
            model=model_used, cost=cost_usd, latency_ms=latency_ms,
            guardrail_passed=guardrail_passed, guardrail_reason=guardrail_reason,
        )

        meta = {
            "model": model_used,
            "cost": round(cost_usd, 6),
            "latency_ms": latency_ms,
            "guardrail_passed": guardrail_passed,
            "guardrail_reason": guardrail_reason,
            "total_cost_today": round(_cost_today, 6),
        }
        yield f"data: [META] {json.dumps(meta)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Accel-Buffering": "no",
        },
    )

if __name__ == "__main__":
    print("=" * 60)
    print("  ModelFungible Chat POC")
    print("  Open http://localhost:8766")
    print("=" * 60)
    print(f"  Mode:         {'Direct Groq (no gateway)' if DIRECT_MODE else 'Via ModelFungible Gateway'}")
    print(f"  Gateway URL:  {MF_BASE_URL or '(direct Groq)'}")
    print(f"  Model:        {os.environ.get('MF_MODEL', 'groq/llama-3.3-70b-versatile')}")
    print(f"  Guardrail:    {MF_GUARDRAIL or '(none)'}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=8766, debug=False, threaded=True)
