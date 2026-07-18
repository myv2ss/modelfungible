# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Execute Integration — streaming + semantic cache + compliance + guardrails.
Powers /api/execute with:
  - API key quota + rate-limit checks
  - Semantic cache lookup (skip model call on hit)
  - Compliance pre-check (blocking)
  - Guardrail output filtering (blocked_terms, max_length)
  - Budget alert firing after each execution
  - Streaming SSE support
"""
from __future__ import annotations

import json as _json
import time as _time
import logging

try:
    from fastapi.responses import StreamingResponse
    from fastapi import HTTPException
except ImportError:
    StreamingResponse = None
    HTTPException = Exception

logger = logging.getLogger(__name__)


def execute_with_cache_and_compliance(
    data, ctx, registry,
    get_audit_logger_fn, get_decision_store_fn,
    get_cache_fn, get_compliance_fn, get_guardrails_fn,
    get_api_key_store_fn, get_budget_alert_store_fn,
    build_model_profiles_fn, get_adapter_fn,
    RouterMode, ModelSelector, ModelProfile, ExecutionRequest,
    estimate_cost, PIIDetector,
):
    """
    Non-streaming execute with: quota check → cache → compliance pre-check →
    model call → guardrail filter → budget alert → usage record → cache store.
    """
    prompt = data.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, {"error": "prompt is required"})

    system = data.get("system", "You are a helpful assistant.")
    explicit = data.get("model")
    mode_str = data.get("mode", "balanced")
    capability = data.get("capability", "any")
    max_cost = data.get("max_cost_per_call")
    temperature = float(data.get("temperature", 0.7))
    max_tokens = int(data.get("max_tokens", 1024))
    use_cache = data.get("use_cache", True)
    metadata = data.get("metadata", {})
    org_id = getattr(ctx, "org_id", "default-org")
    team_id = getattr(ctx, "team_id", org_id)

    try:
        router_mode = RouterMode(mode_str)
    except ValueError:
        raise HTTPException(400, {"error": f"Invalid mode: {mode_str}"})

    # ── 1. API Key quota / rate-limit check ───────────────────────────────
    api_key_store = get_api_key_store_fn()
    if api_key_store:
        qs = api_key_store.get_quota_status(team_id)
        if qs.is_exceeded:
            raise HTTPException(402, {
                "error": "Budget quota exceeded",
                "scope": qs.exceeded_scope,
                "spent_today": round(qs.spent_today, 4),
                "daily_limit": qs.daily_limit,
            })
        rl = api_key_store.check_rate_limit(team_id)
        if rl.is_limited:
            raise HTTPException(429, {
                "error": "Rate limit exceeded",
                "retry_after_secs": rl.retry_after_secs,
            })

    # ── 2. Compliance pre-check ──────────────────────────────────────────
    compliance = get_compliance_fn()
    if compliance:
        pol_results = compliance.evaluate_prompt(
            prompt=prompt, model_id=explicit or "auto",
            actor=getattr(ctx, "user_id", "anonymous"),
            org_id=org_id,
            department=metadata.get("department", ""),
        )
        for pr in pol_results:
            if not pr.passed and pr.action_taken == "block":
                audit = get_audit_logger_fn()
                if audit:
                    audit.log(action="policy_blocked", actor=getattr(ctx, "user_id", "anonymous"),
                              org_id=org_id, outcome="error",
                              metadata={"policy": pr.policy_name,
                                        "failed_conditions": pr.failed_conditions})
                raise HTTPException(422, {
                    "error": "Policy violation", "policy": pr.policy_name,
                    "failed_conditions": pr.failed_conditions, "details": pr.details,
                })

    # ── 3. Cache lookup ───────────────────────────────────────────────────
    cache = get_cache_fn()
    if use_cache and cache:
        hit = cache.get(prompt, system, explicit or "any")
        if hit:
            audit = get_audit_logger_fn()
            if audit:
                audit.log(action="model_execute", actor=getattr(ctx, "user_id", "anonymous"),
                          org_id=org_id, outcome="success",
                          metadata={"router_mode": "cache_hit",
                                    "model_id": hit.model_name,
                                    "cost_usd": 0.0, "latency_ms": 0,
                                    "cached": True, "team_id": team_id})
            _fire_budget_alerts(get_budget_alert_store_fn, team_id, 0)
            return {
                "output": hit.response, "model_id": hit.model_name,
                "cached": True, "cost": 0.0, "latency_ms": hit.latency_ms,
                "router_mode": "cache_hit", "model_name": hit.model_name,
                "provider": "", "capability": capability,
                "pii_detected": False, "attempt_number": 1,
                "audit_entry_id": "", "guardrail_passed": True,
            }

    # ── 4. Model selection ────────────────────────────────────────────────
    profiles = build_model_profiles_fn(registry)
    if not profiles:
        raise HTTPException(503, {"error": "No models registered"})

    selector = ModelSelector(profiles)
    req = ExecutionRequest(
        prompt=prompt, system=system, model=explicit,
        mode=router_mode, capability=capability,
        max_cost_per_call=max_cost, temperature=temperature, max_tokens=max_tokens,
    )

    if max_cost is not None:
        est_tokens = max(1, len(prompt) // 4) + max_tokens
        max_mc = max((m.cost_input_per_1k for m in profiles), default=0.001)
        est_cost = est_tokens / 1000 * max_mc
        if est_cost > max_cost:
            raise HTTPException(402, {
                "error": f"Estimated cost ${est_cost:.4f} > max_cost_per_call ${max_cost:.4f}"
            })

    selected = selector.select(req)
    if not selected:
        raise HTTPException(503, {"error": "No available model"})

    # ── 5. PII scan (prompt) ──────────────────────────────────────────────
    pii_detected = False
    pii_flags = []
    prompt_log = prompt
    system_log = system
    try:
        det = PIIDetector()
        scanned = det.scan({"p": prompt, "s": system})
        if scanned:
            pii_detected = True
            pii_flags = list(scanned.keys())
            for k, v in scanned.items():
                if isinstance(v, str):
                    prompt_log = prompt_log.replace(v, "[REDACTED]")
                    system_log = system_log.replace(v, "[REDACTED]")
    except Exception:
        pass

    # ── 6. Execute with fallback ──────────────────────────────────────────
    output_text = ""
    latency_ms = 0
    in_tok = max(1, len(prompt) // 4)
    out_tok = max_tokens // 2
    cost = 0.0
    success = False
    last_err = ""
    attempt = 1
    fallback = [selected] + selector.get_fallback_order(selected)
    tried = []
    decision_store = get_decision_store_fn()
    audit = get_audit_logger_fn()

    for candidate in fallback:
        if candidate.name in tried:
            continue
        tried.append(candidate.name)
        adapter, model_id = get_adapter_fn(registry, candidate.name)
        if not adapter:
            continue
        cb = registry._breakers.get(candidate.name)
        if cb and cb.state() == "OPEN":
            last_err = f"Circuit breaker open for {candidate.name}"
            continue
        t0 = _time.time()
        try:
            raw = adapter.call(prompt=prompt_log, model=model_id, system_prompt=system_log,
                             temperature=temperature, max_tokens=max_tokens)
            latency_ms = int((_time.time() - t0) * 1000)
            if isinstance(raw, dict):
                choices = raw.get("choices", [{}])
                output_text = choices[0].get("message", {}).get("content", "")
                usage = raw.get("usage", {})
                in_tok = usage.get("prompt_tokens", in_tok)
                out_tok = usage.get("completion_tokens", out_tok)
            else:
                output_text = str(raw)
                out_tok = max(out_tok, len(output_text) // 4)
            cost = estimate_cost(candidate, in_tok, out_tok)
            success = True
            if cb:
                cb.record(success=True)
            break
        except Exception as e:
            last_err = str(e)
            latency_ms = int((_time.time() - t0) * 1000)
            if cb:
                cb.record(success=False)

    if not success:
        if audit:
            audit.log(action="model_execute", actor=getattr(ctx, "user_id", "anonymous"),
                      org_id=org_id, outcome="error",
                      metadata={"router_mode": router_mode.value,
                                "capability": capability,
                                "models_tried": tried,
                                "last_error": last_err,
                                "pii_detected": pii_detected,
                                "team_id": team_id})
        raise HTTPException(503, {"error": f"All models failed. Last: {last_err}"})

    # ── 7. Guardrail output filtering ─────────────────────────────────────
    guardrails = get_guardrails_fn()
    guardrail_passed = True
    guardrail_reason = ""
    if guardrails:
        gr_result = guardrails.apply(output_text)
        output_text = gr_result.filtered_output
        guardrail_passed = gr_result.passed
        guardrail_reason = gr_result.reason
    else:
        gr_result = None

    # ── 8. Budget alert check ─────────────────────────────────────────────
    _fire_budget_alerts(get_budget_alert_store_fn, team_id, cost)

    # ── 9. Record usage (API key quota) ────────────────────────────────────
    if api_key_store:
        api_key_store.record_usage(team_id, cost)

    # ── 10. Audit log ─────────────────────────────────────────────────────
    entry_id = ""
    if audit:
        qs_after = api_key_store.get_quota_status(team_id) if api_key_store else None
        entry_id = audit.log(action="model_execute",
                             actor=getattr(ctx, "user_id", "anonymous"),
                             org_id=org_id, outcome="success",
                             metadata={"router_mode": router_mode.value,
                                       "capability": capability,
                                       "model_selected": selected.name,
                                       "model_id": model_id,
                                       "latency_ms": latency_ms,
                                       "cost_usd": cost,
                                       "input_tokens_est": in_tok,
                                       "output_tokens_est": out_tok,
                                       "pii_detected": pii_detected,
                                       "pii_flags": pii_flags,
                                       "attempt_number": attempt,
                                       "cached": False,
                                       "guardrail_passed": guardrail_passed,
                                       "guardrail_reason": guardrail_reason,
                                       "team_id": team_id,
                                       "quota_spent_today": qs_after.spent_today if qs_after else 0})

    # ── 11. Cache store ────────────────────────────────────────────────────
    if use_cache and cache:
        try:
            cache.store(prompt, system, selected.name, output_text,
                       latency_ms=latency_ms, cost_usd=cost,
                       input_tokens=in_tok, output_tokens=out_tok)
        except Exception:
            pass

    # ── 12. Decision attribution ───────────────────────────────────────────
    if decision_store:
        from modelfungible.enterprise.decision_attribution import ModelScore
        scores = []
        for cand in fallback:
            cb2 = registry._breakers.get(cand.name)
            failure = "circuit_breaker_open" if (cb2 and cb2.state() == "OPEN") else ""
            scores.append(ModelScore(
                model_name=cand.name, provider=cand.provider,
                model_id=cand.model_id, score=0.0,
                latency_ms=cand.latency_ms_p50,
                cost_score=(100/1000*cand.cost_input_per_1k)+(50/1000*cand.cost_output_per_1k),
                speed_score=cand.latency_ms_p50 / max(cand.latency_ms_p50, 1),
                capability_score=1.0 if cand.capability == capability else 0.0,
                final_score=0.0,
                was_selected=(cand.name == selected.name),
                was_tried=(cand.name in tried),
                failure_reason=failure,
            ))
        import uuid as _uuid
        decision_store.record(
            request_id=str(entry_id) if entry_id else _uuid.uuid4().hex[:12],
            actor=getattr(ctx, "user_id", "anonymous"),
            mode=router_mode.value,
            selected_model=selected.name,
            selected_provider=selected.provider,
            fallback_order=[m.name for m in fallback],
            scores=scores,
            request_summary=prompt[:100],
            capability=capability,
            explicit_model=explicit or "",
            piid_detected=pii_detected,
            total_latency_ms=latency_ms,
            total_cost_usd=cost,
            attempt_count=attempt,
        )

    return {
        "output": output_text,
        "model_id": model_id,
        "model_name": selected.name,
        "provider": selected.provider,
        "latency_ms": latency_ms,
        "cost": round(cost, 6),
        "router_mode": router_mode.value,
        "capability": capability,
        "pii_detected": pii_detected,
        "cached": False,
        "attempt_number": attempt,
        "audit_entry_id": str(entry_id) if entry_id else "",
        "guardrail_passed": guardrail_passed,
        "guardrail_reason": guardrail_reason,
    }


def _fire_budget_alerts(get_budget_alert_store_fn, team_id: str, cost: float):
    """Fire budget alerts if thresholds crossed. Safe to swallow errors."""
    try:
        store = get_budget_alert_store_fn()
        if not store:
            return
        qs = store.get_quota_status(team_id)
        if qs.daily_limit <= 0 and qs.monthly_limit <= 0:
            return
        store.check_and_fire(
            team_id,
            qs.spent_today + cost,
            qs.spent_month + cost,
            qs.daily_limit,
            qs.monthly_limit,
        )
    except Exception as e:
        logger.warning("Budget alert check failed: %s", e)


# ── Streaming ──────────────────────────────────────────────────────────────────

def create_streaming_response(
    data, ctx, registry,
    get_audit_logger_fn, get_cache_fn, get_compliance_fn, get_guardrails_fn,
    get_budget_alert_store_fn, get_api_key_store_fn,
    build_model_profiles_fn, get_adapter_fn,
    RouterMode, ModelSelector, ModelProfile, ExecutionRequest,
    estimate_cost, PIIDetector,
):
    """Streaming SSE response with guardrail filtering post-stream."""
    if StreamingResponse is None:
        raise HTTPException(503, {"error": "Streaming not available — FastAPI not installed"})

    prompt = data.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(400, {"error": "prompt is required"})

    system = data.get("system", "You are a helpful assistant.")
    explicit = data.get("model")
    mode_str = data.get("mode", "balanced")
    capability = data.get("capability", "any")
    temperature = float(data.get("temperature", 0.7))
    max_tokens = int(data.get("max_tokens", 1024))
    org_id = getattr(ctx, "org_id", "default-org")
    team_id = getattr(ctx, "team_id", org_id)

    try:
        router_mode = RouterMode(mode_str)
    except ValueError:
        raise HTTPException(400, {"error": f"Invalid mode: {mode_str}"})

    api_key_store = get_api_key_store_fn()
    if api_key_store:
        qs = api_key_store.get_quota_status(team_id)
        if qs.is_exceeded:
            raise HTTPException(402, {"error": "Budget quota exceeded", "scope": qs.exceeded_scope})
        rl = api_key_store.check_rate_limit(team_id)
        if rl.is_limited:
            raise HTTPException(429, {"error": "Rate limit exceeded",
                                       "retry_after_secs": rl.retry_after_secs})

    profiles = build_model_profiles_fn(registry)
    if not profiles:
        raise HTTPException(503, {"error": "No models registered"})

    selector = ModelSelector(profiles)
    selected = selector.select(ExecutionRequest(
        prompt=prompt, system=system, model=explicit,
        mode=router_mode, capability=capability,
        temperature=temperature, max_tokens=max_tokens,
    ))
    if not selected:
        raise HTTPException(503, {"error": "No available model"})

    adapter, model_id = get_adapter_fn(registry, selected.name)
    if not adapter:
        raise HTTPException(503, {"error": f"No adapter for {selected.name}"})

    def event_generator():
        t0 = _time.time()
        accumulated = ""
        guardrail_passed = True
        guardrail_reason = ""

        try:
            if hasattr(adapter, "stream"):
                for chunk in adapter.stream(prompt=prompt, model=model_id,
                                            system_prompt=system,
                                            temperature=temperature,
                                            max_tokens=max_tokens):
                    accumulated += chunk
                    yield f"data: {_json.dumps({'type': 'delta', 'delta': chunk})}\n\n"
            else:
                raw = adapter.call(prompt=prompt, model=model_id,
                                  system_prompt=system,
                                  temperature=temperature, max_tokens=max_tokens)
                content = ""
                if isinstance(raw, dict):
                    choices = raw.get("choices", [{}])
                    content = choices[0].get("message", {}).get("content", "")
                else:
                    content = str(raw)
                words = content.split(" ")
                for i, word in enumerate(words):
                    delta = word + (" " if i                    delta = word + (" " if i < len(words) - 1 else "")
                    accumulated += delta
                    yield f"data: {_json.dumps({'type': 'delta', 'delta': delta})}\n\n"
        except Exception as e:
            yield f"data: {_json.dumps({'type': 'error', 'error': str(e)})}\n\n"
            return

        latency_ms = int((_time.time() - t0) * 1000)
        in_tok = max(1, len(prompt) // 4)
        out_tok = max(1, len(accumulated) // 4)
        cost = estimate_cost(selected, in_tok, out_tok)

        # Guardrail filter
        guardrails = get_guardrails_fn()
        if guardrails:
            gr_result = guardrails.apply(accumulated)
            accumulated = gr_result.filtered_output
            guardrail_passed = gr_result.passed
            guardrail_reason = gr_result.reason

        # Budget alert + usage
        api_key_store = get_api_key_store_fn()
        _fire_budget_alerts(get_budget_alert_store_fn, team_id, cost)
        if api_key_store:
            api_key_store.record_usage(team_id, cost)

        final_event = {
            "type": "done", "content": accumulated,
            "model_id": model_id, "model_name": selected.name,
            "provider": selected.provider,
            "latency_ms": latency_ms, "cost": round(cost, 6),
            "router_mode": router_mode.value, "capability": capability,
            "input_tokens": in_tok, "output_tokens": out_tok,
            "guardrail_passed": guardrail_passed,
            "guardrail_reason": guardrail_reason,
        }
        yield f"data: {_json.dumps(final_event)}\n\n"
        yield "data: [DONE]\n\n"

        # Audit log
        audit = get_audit_logger_fn()
        if audit:
            audit.log(action="model_execute", actor=getattr(ctx, "user_id", "anonymous"),
                      org_id=org_id, outcome="success",
                      metadata={"router_mode": router_mode.value,
                                "capability": capability,
                                "model_selected": selected.name,
                                "model_id": model_id,
                                "latency_ms": latency_ms,
                                "cost_usd": cost,
                                "input_tokens_est": in_tok,
                                "output_tokens_est": out_tok,
                                "pii_detected": False,
                                "attempt_number": 1,
                                "cached": False,
                                "streaming": True,
                                "guardrail_passed": guardrail_passed,
                                "guardrail_reason": guardrail_reason,
                                "team_id": team_id})

        # Cache
        cache = get_cache_fn()
        if cache:
            try:
                cache.store(prompt, system, selected.name, accumulated,
                           latency_ms=latency_ms, cost_usd=cost,
                           input_tokens=in_tok, output_tokens=out_tok)
            except Exception:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
