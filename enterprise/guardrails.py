# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Feature 7: Guardrails — output filtering with blocked terms and max length.
Applies to both streaming and non-streaming model outputs.
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GuardrailConfig:
    blocked_terms: list[str] = field(default_factory=list)
    max_length: Optional[int] = None          # characters; None = no limit
    mask_char: str = "*"
    mask_replacement: str = "[FILTERED]"
    case_sensitive: bool = False


@dataclass
class GuardrailResult:
    passed: bool
    filtered_output: str
    terms_blocked: list[str] = field(default_factory=list)
    was_truncated: bool = False
    reason: str = ""


class Guardrails:
    """
    Applies output filtering: blocked term removal and max-length truncation.
    Thread-safe (stateless per call — no shared mutable state).
    """

    def __init__(self, config: Optional[GuardrailConfig] = None):
        self.config = config or GuardrailConfig()

    def apply(self, output: str) -> GuardrailResult:
        """
        Returns GuardrailResult with filtered_output and metadata.
        """
        if not output:
            return GuardrailResult(passed=True, filtered_output=output)

        filtered = output
        blocked: list[str] = []

        # 1. Blocked terms
        for term in self.config.blocked_terms:
            if not term or not term.strip():
                continue
            flags = 0 if self.config.case_sensitive else re.IGNORECASE
            pattern = re.escape(term)
            matches = re.findall(pattern, filtered, flags=flags)
            if matches:
                blocked.append(term)
                # Replace each occurrence with mask
                filtered = re.sub(pattern, self.config.mask_replacement, filtered, flags=flags)

        # 2. Max length truncation
        was_truncated = False
        reason = ""
        if self.config.max_length is not None and len(filtered) > self.config.max_length:
            filtered = filtered[:self.config.max_length]
            was_truncated = True
            reason = f"Truncated to {self.config.max_length} chars"

        if blocked:
            reason = f"Blocked terms: {', '.join(blocked)}" + (f"; {reason}" if reason else "")

        return GuardrailResult(
            passed=len(blocked) == 0 and not was_truncated,
            filtered_output=filtered,
            terms_blocked=blocked,
            was_truncated=was_truncated,
            reason=reason or "passed",
        )

    def check(self, output: str) -> bool:
        """Quick pass/fail check without building full result."""
        if not output:
            return True
        if self.config.max_length is not None and len(output) > self.config.max_length:
            return False
        for term in self.config.blocked_terms:
            if not term:
                continue
            flags = 0 if self.config.case_sensitive else re.IGNORECASE
            if re.search(re.escape(term), output, flags=flags):
                return False
        return True


def build_guardrails_from_dict(data: dict) -> Guardrails:
    """Build Guardrails from an execute request dict (output_filter field)."""
    of = data.get("output_filter")
    if not of:
        return Guardrails()
    cfg = GuardrailConfig(
        blocked_terms=of.get("blocked_terms", []),
        max_length=of.get("max_length"),
        mask_replacement=of.get("mask_replacement", "[FILTERED]"),
        case_sensitive=of.get("case_sensitive", False),
    )
    return Guardrails(cfg)
