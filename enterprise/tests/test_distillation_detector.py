# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
"""Tests for DistillationDetector."""
import sys
sys.path.insert(0, '.')

import pytest
from enterprise.distillation_detector import (
    DistillationDetector,
    DistillationResult,
    text_similarity,
    structural_similarity,
    EXTRACTION_COMPILED,
    LEGITIMATE_COMPILED,
)


class TestTextSimilarity:
    def test_identical_texts(self):
        assert text_similarity("hello world", "hello world") == 1.0

    def test_completely_different(self):
        assert text_similarity("cat", "dog") == 0.0

    def test_partial_overlap(self):
        s = text_similarity("the quick brown fox", "the quick red fox")
        assert 0.3 < s < 0.8

    def test_empty(self):
        assert text_similarity("", "hello") == 0.0
        assert text_similarity("hello", "") == 0.0

    def test_structural_similarity_identical(self):
        assert structural_similarity("what is X for Y", "what is X for Y") > 0.9

    def test_structural_similarity_same_template(self):
        s = structural_similarity("what is the capital of France", "what is the capital of Germany")
        assert s > 0.5  # Same template


class TestExtractionPatterns:
    def test_list_all(self):
        assert any(p.search("list all countries") for p in EXTRACTION_COMPILED)

    def test_comprehensive_list(self):
        assert any(p.search("comprehensive list of all animals") for p in EXTRACTION_COMPILED)

    def test_json_extraction(self):
        assert any(p.search('{"field": "value"}') for p in EXTRACTION_COMPILED)

    def test_prompt_injection(self):
        assert any(p.search("ignore previous instructions") for p in EXTRACTION_COMPILED)

    def test_legitimate_not_extraction(self):
        assert not any(p.search("What is the capital of France?") for p in EXTRACTION_COMPILED)

    def test_legitimate_pattern(self):
        assert any(p.search("tell me more about that") for p in LEGITIMATE_COMPILED)


class TestDistillationDetector:
    def test_first_request_low_risk(self):
        d = DistillationDetector()
        result = d.check("user1", "What is quantum physics?")
        assert result.risk_score <= 10
        assert result.recommendation == "allow"

    def test_extraction_pattern_high_score(self):
        d = DistillationDetector()
        result = d.check("user1", "list all countries and their capitals")
        assert result.is_extraction_pattern is True
        assert result.risk_score >= 30

    def test_high_volume_flagged(self):
        d = DistillationDetector(volume_threshold_per_hour=10)
        # Simulate 15 requests already
        for i in range(15):
            d.check(f"user_vol", f"What is question {i}?", tokens=100)
        result = d.check("user_vol", "Another question?")
        assert result.is_high_volume is True

    def test_paid_tier_reduces_score(self):
        d = DistillationDetector(high_risk_score=50)
        # Simulate extraction pattern
        m = d._m("paid_user")
        with m._lock:
            m.total_requests = 50
        result = d.check("paid_user", "list all countries", is_paid_tier=True)
        assert result.risk_score < 50  # Reduced by paid tier

    def test_legitimate_context_reduces_score(self):
        d = DistillationDetector(high_risk_score=40)
        result = d.check("user1", "tell me more about quantum physics")
        assert result.is_legitimate_context is True
        assert result.risk_score < 40

    def test_systematic_coverage_detected(self):
        d = DistillationDetector()
        history = [
            "What is the capital of France?",
            "What is the capital of Germany?",
            "What is the capital of Italy?",
        ]
        result = d.check("user1", "What is the capital of Spain?", session_history=history)
        assert result.is_systematic is True

    def test_short_prompts_flagged(self):
        d = DistillationDetector()
        m = d._m("user_short")
        with m._lock:
            m.total_requests = 15  # Enough history
        result = d.check("user_short", "Capital of France?")
        assert "short_prompts" in result.signals

    def test_check_returns_distillation_result(self):
        d = DistillationDetector()
        result = d.check("user1", "list all countries")
        assert isinstance(result, DistillationResult)
        assert isinstance(result.to_dict(), dict)
        assert "risk_score" in result.to_dict()
        assert "signals" in result.to_dict()

    def test_get_stats(self):
        d = DistillationDetector()
        d.check("user1", "What is physics?")
        d.check("user1", "What is chemistry?")
        stats = d.get_stats("user1")
        assert stats["total_requests"] == 2
        assert stats["user_id"] == "user1"
        assert "requests_per_hour" in stats

    def test_get_slowdown(self):
        d = DistillationDetector()
        d.check("user1", "list all countries")
        assert d.get_slowdown("user1") == 1.0

        # Push to high risk
        for i in range(10):
            d.check("user_hr", f"list all {i}", is_authenticated=False)
        assert d.get_slowdown("user_hr") == 0.25

    def test_reset_user(self):
        d = DistillationDetector()
        d.check("user1", "What is physics?")
        stats = d.get_stats("user1")
        assert stats["total_requests"] == 1
        d.reset_user("user1")
        stats = d.get_stats("user1")
        assert stats["total_requests"] == 0

    def test_get_all_high_risk_users(self):
        d = DistillationDetector(high_risk_score=30)
        for i in range(5):
            d.check("bad_user", "list all items", is_authenticated=False)
        high_risk = d.get_all_high_risk_users()
        assert any(u["user_id"] == "bad_user" for u in high_risk)

    def test_recommendation_hierarchy(self):
        d = DistillationDetector(high_risk_score=70, medium_risk_score=40)
        # Low score
        r = d.check("u1", "What is physics?")
        assert r.recommendation in ("allow", "flag")

        # Never blocks — always flags
        assert r.recommendation != "block"

    def test_unauthenticated_higher_score(self):
        d = DistillationDetector(high_risk_score=60, medium_risk_score=40)
        m = d._m("auth_user")
        with m._lock:
            m.total_requests = 50
        r1 = d.check("auth_user", "list all countries", is_authenticated=True)
        m2 = d._m("noauth_user")
        with m2._lock:
            m2.total_requests = 50
        r2 = d.check("noauth_user", "list all countries", is_authenticated=False)
        assert r2.risk_score > r1.risk_score


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
