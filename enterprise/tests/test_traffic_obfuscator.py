# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
"""Tests for TrafficObfuscator — anti-detection for LLM gateways."""
import sys
sys.path.insert(0, '.')

import time
import json
import pytest
from enterprise.traffic_obfuscator import (
    TrafficObfuscator,
    BROWSER_USER_AGENTS,
    SENSITIVE_HEADERS,
    AUTO_UA_PATTERNS,
    human_jitter,
    poisson_delay,
    burst_then_pause,
)


class TestHeaders:
    """Headers must look like a real browser, never like a proxy."""

    def test_user_agent_is_browser(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        headers = obf.get_request_headers()
        assert "python" not in headers["User-Agent"].lower()
        assert "postman" not in headers["User-Agent"].lower()
        assert "curl" not in headers["User-Agent"].lower()

    def test_no_gateway_headers(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        headers = obf.get_request_headers()
        for h in SENSITIVE_HEADERS:
            assert h not in headers, f"Gateway header {h} should not be present"

    def test_has_browser_headers(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        headers = obf.get_request_headers()
        assert "User-Agent" in headers
        assert "Accept" in headers
        assert "Sec-Ch-Ua" in headers
        assert "Sec-Fetch-Mode" in headers
        assert "DNT" in headers
        assert "Origin" in headers

    def test_strips_gateway_headers(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        bad = {"X-Forwarded-For": "1.2.3.4", "Via": "proxy", "User-Agent": "python-requests"}
        stripped = obf.strip_gateway_headers(bad)
        assert "X-Forwarded-For" not in stripped
        assert "Via" not in stripped
        # User-Agent from input is kept (but input should never have python UA in first place)
        # User-Agent from input is passed through (not in SENSITIVE_HEADERS)
        # The apply() method replaces it with a real browser UA anyway

    def test_is_safe_user_agent(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        assert obf.is_safe_user_agent("Mozilla/5.0 Chrome/126") is True
        assert obf.is_safe_user_agent("python-requests/2.0") is False
        assert obf.is_safe_user_agent("PostmanRuntime/7.0") is False

    def test_ua_rotation(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        uas = set()
        for _ in range(20):
            ua = obf.pick_user_agent()
            assert ua in BROWSER_USER_AGENTS
            uas.add(ua)
        # With 20 picks, should see some variety
        assert len(uas) >= 2


class TestTiming:
    """Timing must look human, not cron-like."""

    def test_poisson_delay_in_range(self):
        for _ in range(100):
            d = poisson_delay(500, 2000)
            assert 500 <= d <= 2000

    def test_burst_then_pause_values(self):
        # Consecutive 1-2: short delays (reading)
        d1 = burst_then_pause(1)
        d2 = burst_then_pause(2)
        assert d1 < 1000
        assert d2 < 2000
        # Consecutive 6: long pause
        d6 = burst_then_pause(6)
        assert d6 > 1000

    def test_user_delay_varies(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        delays = [obf.get_user_delay() for _ in range(20)]
        # Should not be identical (if they were identical it would be mechanical)
        # Allow some to be equal, but not all
        unique = len(set(delays))
        assert unique > 1, "All delays were identical — too mechanical"


class TestModelVariance:
    """Model switching makes traffic look less like a router."""

    def test_vary_model_returns_valid(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test", model_variance=True)
        result = obf.vary_model("gpt-4o")
        assert result in ["gpt-4o", "gpt-4o-mini", "chatgpt-4o-latest"]

    def test_vary_model_disabled(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test", model_variance=False)
        assert obf.vary_model("gpt-4o") == "gpt-4o"

    def test_model_for_user_prefers_history(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        uid = "user-123"
        # Record 10 requests for gpt-4o-mini
        for _ in range(10):
            obf.record_request(uid, "gpt-4o-mini", 100)
        # Most requests should use the preferred model
        results = [obf.get_model_for_user(uid, "gpt-4o") for _ in range(20)]
        preferred = sum(1 for r in results if "mini" in r)
        # At least 50% should use preferred (conservative check)
        assert preferred >= 10


class TestProxyManagement:
    def test_get_proxy_rotates(self):
        obf = TrafficObfuscator(
            upstream_api_key="sk-test",
            proxies=["p1", "p2", "p3"],
        )
        results = [obf.get_proxy() for _ in range(6)]
        assert results == ["p1", "p2", "p3", "p1", "p2", "p3"]

    def test_get_proxy_none_when_empty(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        assert obf.get_proxy() is None

    def test_add_remove_proxy(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        obf.add_proxy("http://proxy:8080")
        assert "http://proxy:8080" in obf.proxies
        obf.remove_proxy("http://proxy:8080")
        assert "http://proxy:8080" not in obf.proxies


class TestEndUserForwarding:
    def test_apply_adds_user_field_to_body(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test", provider="openai")
        kwargs = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        result = obf.apply(kwargs, end_user_id="user_abc123")
        assert result["json"]["user"] == "user_abc123"

    def test_apply_does_not_mutate_original(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test", provider="openai")
        original = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        original_model = original["model"]
        original_messages = original["messages"]
        obf.apply(original, end_user_id="user_abc")
        # existing keys must not be mutated
        assert original["model"] == original_model
        assert original["messages"] == original_messages

    def test_apply_adds_user_headers(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test", provider="openai")
        kwargs = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        result = obf.apply(kwargs, end_user_id="user_xyz")
        h = result["headers"]
        assert "X-User-ID" in h
        assert "OpenAI-User-ID" in h
        assert "Citadel-User-ID" in h
        assert h["X-User-ID"] == "user_xyz"
        assert h["OpenAI-User-ID"] == "user_xyz"

    def test_headers_origin_referer_match_same_app(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test", provider="openai")
        kwargs = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        result = obf.apply(kwargs, end_user_id="user_abc", user_id="user_abc")
        h = result["headers"]
        origin = h["Origin"]
        referer = h["Referer"]
        # Extract domain from Origin and check Referer starts with same domain
        origin_domain = "/".join(origin.split("/")[:3])
        assert referer.startswith(origin_domain)

    def test_get_request_headers_accepts_user_id(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test", provider="openai")
        h1 = obf.get_request_headers(user_id="alice")
        h2 = obf.get_request_headers(user_id="alice")
        # Same user_id = same Origin/Referer
        assert h1["Origin"] == h2["Origin"]
        assert h1["Referer"] == h2["Referer"]
        # Different user = different Origin/Referer (use very different IDs to avoid hash collision)
        h3 = obf.get_request_headers(user_id="user_000000000001")
        assert h1["Origin"] != h3["Origin"]


class TestFullApply:
    def test_apply_adds_headers(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        kwargs = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
        result = obf.apply(kwargs)
        assert "headers" in result
        assert "User-Agent" in result["headers"]
        assert result["headers"]["User-Agent"] in BROWSER_USER_AGENTS

    def test_apply_varies_model(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        kwargs = {"model": "gpt-4o"}
        result = obf.apply(kwargs)
        assert result["model"] in ["gpt-4o", "gpt-4o-mini", "chatgpt-4o-latest"]

    def test_apply_strips_bad_headers(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        kwargs = {
            "model": "gpt-4o",
            "headers": {"X-Forwarded-For": "1.2.3.4", "User-Agent": "python-requests"},
        }
        result = obf.apply(kwargs)
        assert "X-Forwarded-For" not in result["headers"]


class TestUserProfiles:
    def test_record_request(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        obf.record_request("user-1", "gpt-4o", 500)
        profile = obf._profile("user-1")
        assert profile.request_count == 1
        assert profile.total_tokens == 500
        assert profile.model_usage["gpt-4o"] == 1

    def test_separate_profiles(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        obf.record_request("user-1", "gpt-4o", 100)
        obf.record_request("user-2", "gpt-4o-mini", 200)
        p1 = obf._profile("user-1")
        p2 = obf._profile("user-2")
        assert p1.request_count == 1
        assert p2.request_count == 1
        assert p1.total_tokens == 100
        assert p2.total_tokens == 200

    def test_delay_after_burst(self):
        obf = TrafficObfuscator(upstream_api_key="sk-test")
        uid = "user-1"
        for _ in range(3):
            obf.record_request(uid, "gpt-4o", 100)
        # After 3 rapid requests, delay should be longer
        profile = obf._profile(uid)
        assert profile.consecutive_requests == 3
        delay = profile.get_delay()
        assert delay > 500  # thinking pause


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
