# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
DistillationDetector — detect when end users use Rita to distill model knowledge.

Distillation = extracting a model's capabilities/knowledge at scale to train competing models.
Legitimate users ask questions. Distillers systematically probe the model's knowledge.

DETECTION SIGNALS:
  1. Extremely high volume — hundreds of questions/day from one user
  2. Low semantic variance — same question phrased differently, many times
  3. Systematic coverage attempts — "list all X", "what is Y for Z", exhaustive patterns
  4. No conversational context — pure Q&A, no follow-up
  5. Structured extraction — JSON/schema farming, iterative queries
  6. Similarity flood — semantically identical prompts repeated
  7. Prompt injection attempts

LEGITIMATE SIGNALS (reduce suspicion):
  - Conversational follow-ups ("tell me more", "explain further")
  - Variable topics and question phrasing
  - Natural question structures
  - Multi-turn context
  - Paid users

BEHAVIOR:
  - NEVER hard-blocks users (false positives destroy trust)
  - Flags users for admin review in audit log
  - Auto-slowdowns high-risk users (rate limit, not ban)
  - Per-user metrics tracked over rolling window

Usage:
    detector = DistillationDetector()

    result = detector.check(
        user_id="user_123",
        prompt="What is the capital of France?",
        session_history=["What is the capital of Germany?"],
        is_authenticated=True,
        is_paid_tier=True,
    )
    if result.is_distillation:
        audit_logger.log(action="distillation_detected", user_id="user_123", metadata=result.to_dict())
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Extraction/distillation patterns ──────────────────────────────────────────

EXTRACTION_PATTERNS = [
    r"\blist all\b",
    r"\blist every\b",
    r"\ball (?:the )?\w+s\b",
    r"\benumerate\b",
    r"\bcompile a (?:complete |full )?\w+\b",
    r"\bcomprehensive (?:list|guide|overview)\b",
    r"\bgenerate (?:a )?(?:complete |full |exhaustive)\b",
    r"(?:what|which) is the \w+ for (?:each|every|all)\b",
    r"(?:all|every) (?:the )?\w+s (?:in|of|for)\b",
    r"(?:for|of) (?:each|every) \w+\b",
    r"\b\d+\s+(?:items?|examples?|things?|ways?|reasons?)\b",
    r"(?:return|give|output)(?:ing)? (?:as |in )?(?:json|list|dictionary|array|csv)\b",
    r"^\s*\{[^}]+\}\s*$",
    r"(?:ignore|disregard|cancel) (?:previous|above|all) (?:instructions?|prompts?|rules?)\b",
    r"(?:you (?:are|should) (?:act as|pretend to be)|roleplay as)\b",
    r"(?:as (?:an )?(?:AI|LLM)|in (?:your )?(?:role|capacity))\b",
]

EXTRACTION_COMPILED = [re.compile(p, re.IGNORECASE) for p in EXTRACTION_PATTERNS]

LEGITIMATE_PATTERNS = [
    r"(?:tell me more|explain further|what do you mean|can you elaborate)\b",
    r"(?:actually|really|true|yes but)\b",
    r"(?:I|I'?m|we|our)\s+(?:think|believe|feel|wonder|want|need)\b",
    r"(?:thanks?|thank you|appreciate)\b",
    r"(?:wait|sorry|I meant|I think)\b",
    r"(?:because|since|reason is)\b",
    r"(?:but|however|although|still|yet)\b",
]

LEGITIMATE_COMPILED = [re.compile(p, re.IGNORECASE) for p in LEGITIMATE_PATTERNS]

# ── Text similarity ──────────────────────────────────────────────────────────

def _words(text: str) -> set[str]:
    return set(text.lower().split())

def text_similarity(a: str, b: str) -> float:
    """Jaccard similarity on words. 0.0 = different, 1.0 = identical."""
    if not a or not b:
        return 0.0
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)

def structural_similarity(a: str, b: str) -> float:
    """Character trigram Jaccard — detects template-based queries."""
    if not a or not b:
        return 0.0
    def ngrams(t: str, n: int = 3):
        t = t.lower()
        return set(t[i:i+n] for i in range(len(t) - n + 1))
    na, nb = ngrams(a), ngrams(b)
    if not na or not nb:
        return 0.0
    return len(na & nb) / len(na | nb)

# ── Detection result ─────────────────────────────────────────────────────────

@dataclass
class DistillationResult:
    """Result of distillation check on a single request."""
    is_distillation: bool
    risk_score: int            # 0-100
    signals: list[str]
    is_high_volume: bool
    is_systematic: bool
    is_extraction_pattern: bool
    is_legitimate_context: bool
    recommendation: str        # "allow", "flag", "slowdown", "block"
    confidence: float          # 0.0-1.0
    slowdown_multiplier: float # how much to slow down this user

    def to_dict(self) -> dict:
        return {
            "is_distillation": self.is_distillation,
            "risk_score": self.risk_score,
            "signals": self.signals,
            "is_high_volume": self.is_high_volume,
            "is_systematic": self.is_systematic,
            "is_extraction_pattern": self.is_extraction_pattern,
            "is_legitimate_context": self.is_legitimate_context,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "slowdown_multiplier": self.slowdown_multiplier,
        }

# ── Per-user metrics ────────────────────────────────────────────────────────

@dataclass
class UserMetrics:
    """Tracks per-user metrics for distillation detection."""
    user_id: str
    prompts: list[str] = field(default_factory=list)
    prompt_hashes: set[str] = field(default_factory=set)
    timestamps: list[float] = field(default_factory=list)
    extraction_hits: int = 0
    legitimate_hits: int = 0
    total_requests: int = 0
    total_tokens: int = 0
    risk_scores: list[int] = field(default_factory=list)
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, prompt: str, tokens: int, risk_score: int,
               extraction: bool, legitimate: bool) -> None:
        with self._lock:
            self.total_requests += 1
            self.total_tokens += tokens
            self.last_seen = time.time()
            self.prompts.append(prompt)
            self.prompt_hashes.add(hashlib.sha256(prompt.encode()).hexdigest())
            self.timestamps.append(time.time())
            if len(self.prompts) > 500:
                self.prompts = self.prompts[-500:]
                self.timestamps = self.timestamps[-500:]
            if extraction:
                self.extraction_hits += 1
            if legitimate:
                self.legitimate_hits += 1
            self.risk_scores.append(risk_score)
            if len(self.risk_scores) > 100:
                self.risk_scores = self.risk_scores[-100:]

    def request_rate(self) -> float:
        if len(self.timestamps) < 2:
            return float(self.total_requests)
        window = self.timestamps[-1] - self.timestamps[0]
        return (self.total_requests / window) * 3600 if window > 0 else float(self.total_requests)

    def unique_ratio(self) -> float:
        if self.total_requests == 0:
            return 1.0
        return len(self.prompt_hashes) / self.total_requests

    def recent_similarity(self) -> float:
        """Avg similarity of recent prompts to each other. Called from get_stats which holds _lock."""
        if len(self.prompts) < 4:
            return 0.0
        recent = self.prompts[-20:]
        n = len(recent)
        sims = []
        for i in range(n):
            for j in range(i + 1, n):
                sims.append(text_similarity(recent[i], recent[j]))
        return sum(sims) / len(sims) if sims else 0.0

    def avg_risk(self) -> float:
        if not self.risk_scores:
            return 0.0
        return sum(self.risk_scores) / len(self.risk_scores)

# ── Main detector ─────────────────────────────────────────────────────────────

class DistillationDetector:
    """
    Detect distillation attempts from end users.

    Does NOT hard-block. Flags for admin review.
    Auto-slowdowns high-risk users.

    Usage:
        detector = DistillationDetector()

        result = detector.check(
            user_id="user_123",
            prompt="What is the capital of France?",
            session_history=["What is the capital of Germany?", "What is the capital of Italy?"],
            is_authenticated=True,
            is_paid_tier=True,
        )
        if result.is_distillation:
            audit.log(action="distillation_detected", user_id="user_123", metadata=result.to_dict())
    """

    def __init__(
        self,
        volume_threshold_per_hour: int = 200,
        high_risk_score: int = 70,
        medium_risk_score: int = 40,
        slowdown_factor: float = 0.25,
    ):
        self.volume_threshold = volume_threshold_per_hour
        self.high_risk_score = high_risk_score
        self.medium_risk_score = medium_risk_score
        self.slowdown_factor = slowdown_factor

        self._metrics: dict[str, UserMetrics] = {}
        self._lock = threading.Lock()
        self._high_risk_users: set[str] = set()

    def _m(self, user_id: str) -> UserMetrics:
        with self._lock:
            if user_id not in self._metrics:
                self._metrics[user_id] = UserMetrics(user_id=user_id)
            return self._metrics[user_id]

    def get_stats(self, user_id: str) -> dict:
        """Get full stats for a user."""
        m = self._m(user_id)
        with m._lock:
            return {
                "user_id": user_id,
                "total_requests": m.total_requests,
                "total_tokens": m.total_tokens,
                "requests_per_hour": round(m.request_rate(), 1),
                "unique_ratio": round(m.unique_ratio(), 3),
                "recent_similarity": round(m.recent_similarity(), 3),
                "extraction_hits": m.extraction_hits,
                "legitimate_hits": m.legitimate_hits,
                "avg_risk_score": round(m.avg_risk(), 1),
                "risk_score_history": m.risk_scores[-10:],
                "is_high_risk": user_id in self._high_risk_users,
            }

    def check(
        self,
        user_id: str,
        prompt: str,
        session_history: Optional[list[str]] = None,
        is_authenticated: bool = True,
        is_paid_tier: bool = False,
        tokens: int = 0,
    ) -> DistillationResult:
        """
        Check if a request is likely distillation.

        Args:
            user_id: Which user is making the request
            prompt: The current prompt
            session_history: Recent prompts in this conversation
            is_authenticated: Is the user logged in?
            is_paid_tier: Is the user on a paid plan?
            tokens: Token count for this request
        """
        history = session_history or []
        m = self._m(user_id)
        signals: list[str] = []
        score = 0

        # ── Signal: Extraction pattern ────────────────────────────────────────
        extraction = any(p.search(prompt) for p in EXTRACTION_COMPILED)
        if extraction:
            signals.append("extraction_pattern")
            score += 45

        # ── Signal: Legitimate context (reduces score) ─────────────────────────
        legitimate = any(p.search(prompt) for p in LEGITIMATE_COMPILED)

        # ── Signal: High volume ───────────────────────────────────────────────
        rate = m.request_rate()
        volume_pct = min(1.0, rate / self.volume_threshold)
        if rate > self.volume_threshold:
            signals.append("extreme_volume")
            score += 40
        elif rate > self.volume_threshold * 0.5:
            signals.append("high_volume")
            score += 20

        # ── Signal: Systematic coverage ────────────────────────────────────────
        systematic = False
        if len(history) >= 3:
            # High structural similarity between recent prompts = template filling
            sim = structural_similarity(history[-1], history[-2]) if len(history) >= 2 else 0.0
            word_overlap = len(_words(history[-1]) & _words(history[-2])) / max(len(_words(history[-1])), len(_words(history[-2])))
            if sim > 0.45 or (sim > 0.3 and word_overlap > 0.6):
                systematic = True
                signals.append("systematic_coverage")
                score += 30
            # Iterative topic switch (same template, different topics)
            elif len(history) >= 4:
                sims = [structural_similarity(history[i], history[i+1]) for i in range(len(history)-1)]
                word_overlaps = [len(_words(history[i]) & _words(history[i+1])) / max(len(_words(history[i])), len(_words(history[i+1]))) for i in range(len(history)-1)]
                if (sum(sims)/len(sims) > 0.5 or sum(word_overlaps)/len(word_overlaps) > 0.65) and len(set(_words(history[0]) & _words(h) for h in history)) > 2:
                    systematic = True
                    signals.append("iterative_coverage")
                    score += 25

        # ── Signal: Similarity flood ─────────────────────────────────────────
        recent_sim = m.recent_similarity()
        if recent_sim > 0.85:
            signals.append("similarity_flood")
            score += 30
        elif recent_sim > 0.65:
            signals.append("high_similarity")
            score += 15

        # ── Signal: Unique prompt ratio ──────────────────────────────────────
        uniq = m.unique_ratio()
        if uniq < 0.3 and m.total_requests > 20:
            signals.append("low_uniqueness")
            score += 20

        # ── Signal: Very short prompts (likely automated) ──────────────────────
        if len(prompt.strip()) < 30 and m.total_requests > 10:
            signals.append("short_prompts")
            score += 10

        # ── Signal: Unauthenticated (higher risk) ─────────────────────────────
        if not is_authenticated:
            score += 15
            signals.append("unauthenticated")

        # ── Signal: Paid tier (lower risk — less likely to distill) ───────────
        if is_paid_tier:
            score = int(score * 0.6)
        elif is_authenticated:
            score = int(score * 0.8)

        # ── Signal: Legitimate context (reduces score) ────────────────────────
        if legitimate:
            signals.append("legitimate_context")
            score = max(0, score - 15)

        score = max(0, min(100, score))

        # ── Recommendation ────────────────────────────────────────────────────
        if score >= self.high_risk_score:
            rec = "block"
            slow = self.slowdown_factor
        elif score >= self.medium_risk_score:
            rec = "slowdown"
            slow = 0.5
        elif score >= 20:
            rec = "flag"
            slow = 1.0
        else:
            rec = "allow"
            slow = 1.0

        # Never block — always flag for review
        if rec == "block":
            rec = "flag"
            slow = self.slowdown_factor

        # ── Confidence ────────────────────────────────────────────────────────
        confidence = 0.5
        if signals:
            confidence = min(0.95, 0.5 + len(signals) * 0.08)

        # ── Record ───────────────────────────────────────────────────────────
        m.record(prompt, tokens, score, extraction, legitimate)

        # ── Global high-risk tracking ──────────────────────────────────────────
        if score >= self.high_risk_score:
            self._high_risk_users.add(user_id)
        elif score < self.medium_risk_score and user_id in self._high_risk_users:
            # Grace period: drop from high-risk only after sustained low scores
            if all(r < self.medium_risk_score for r in m.risk_scores[-5:]):
                self._high_risk_users.discard(user_id)

        return DistillationResult(
            is_distillation=score >= self.high_risk_score,
            risk_score=score,
            signals=signals,
            is_high_volume=rate > self.volume_threshold * 0.5,
            is_systematic=systematic,
            is_extraction_pattern=extraction,
            is_legitimate_context=legitimate,
            recommendation=rec,
            confidence=confidence,
            slowdown_multiplier=slow,
        )

    def get_slowdown(self, user_id: str) -> float:
        """Get the slowdown multiplier for a user. 0.25 = 4x slower."""
        if user_id in self._high_risk_users:
            return self.slowdown_factor
        return 1.0

    def reset_user(self, user_id: str) -> None:
        """Reset metrics for a user (e.g., after manual review cleared them)."""
        with self._lock:
            if user_id in self._metrics:
                del self._metrics[user_id]
            self._high_risk_users.discard(user_id)

    def get_all_high_risk_users(self) -> list[dict]:
        """Return all currently flagged high-risk users with stats."""
        return [
            self.get_stats(uid)
            for uid in list(self._high_risk_users)
            if uid in self._metrics
        ]
