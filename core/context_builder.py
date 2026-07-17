#!/usr/bin/env python3
"""
Context Builder — ModelFungible

Aggregates all relevant context from shared state into a single packet.
One computation per cycle. Shared by all models.

Context = {
    market:    regime, VIX, SPY vs MA200, risk flags
    positions: open positions with live P&L
    risk_flags: active warnings
    sizing:    from facts
    facts_version: timestamp for staleness checks
}
"""
from __future__ import annotations
import json
from datetime import datetime, date
from pathlib import Path
from modelfungible.enterprise.audit import AuditLogger, PIIDetector
from typing import Optional


def load_json(path: str | Path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def load_text(path: str | Path, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


# ─────────────────────────────────────────────────────────────────
# ContextPacket
# ─────────────────────────────────────────────────────────────────
class ContextPacket:
    """A bundle of all context relevant to a strategy decision."""

    def __init__(self, **kwargs):
        self.role          = kwargs.get("role", "scanner")
        self.model         = kwargs.get("model", "auto")
        self.generated_at  = kwargs.get("generated_at", datetime.now().isoformat())
        self.market        = kwargs.get("market", {})      # regime, vix, spy, etc.
        self.positions     = kwargs.get("positions", [])
        self.risk_flags   = kwargs.get("risk_flags", {})
        self.sizing       = kwargs.get("sizing", {})
        self.pending       = kwargs.get("pending", [])      # tasks still to do
        self.strategy_rules = kwargs.get("strategy_rules", {})
        self.today_memory  = kwargs.get("today_memory", "")
        self.long_term_mem = kwargs.get("long_term_mem", "")
        self.facts_version = kwargs.get("facts_version", "")
        self._extra        = kwargs.get("_extra", {})

    # ── Formatting helpers ─────────────────────────────────────

    def market_summary(self) -> str:
        """Human-readable one-line market state."""
        m = self.market
        return (
            "Regime: {r} | VIX: {v} ({vreg}) | "
            "SPY: ${s} (MA200: ${m2}, {d:+.2f}%)".format(
                r=m.get("regime", "?"),
                v=m.get("vix", "?"),
                vreg=m.get("vix_regime", "?"),
                s=m.get("spy", "?"),
                m2=m.get("spy_ma200", "?"),
                d=m.get("spy_ma200_dist", 0),
            )
        )

    def positions_summary(self) -> str:
        """Human-readable list of open positions with P&L."""
        if not self.positions:
            return "  No open positions."
        lines = []
        for p in self.positions:
            pnl_pct = p.get("pnl_pct", 0)
            pnl_dol = p.get("pnl_dollar", 0)
            sgn = "+" if pnl_pct >= 0 else ""
            lines.append(
                "  {t} {d}: {s}{p:.1f}% (${n:+.0f}) @ ${c}".format(
                    t=p.get("ticker", "?"),
                    d=p.get("direction", "L")[0],
                    s=sgn, p=pnl_pct,
                    n=pnl_dol,
                    c=p.get("current", "?"),
                )
            )
        return "\n".join(lines)

    def open_tickers(self) -> str:
        """Comma-separated list of open tickers."""
        if not self.positions:
            return "None"
        return ", ".join(p["ticker"] for p in self.positions)

    def risk_summary(self) -> str:
        """Active risk flags as pipe-separated string."""
        active = [
            k for k, v in (self.risk_flags or {}).items()
            if v
        ]
        return " | ".join(active) if active else "None"

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return vars(self)


# ─────────────────────────────────────────────────────────────────
# ContextBuilder
# ─────────────────────────────────────────────────────────────────
class ContextBuilder:
    """
    Builds a ContextPacket from shared state files.

    Example:
        cb = ContextBuilder(
            facts_file="trading_desk_state.json",
            memory_dir="./memory"
        )
        ctx = cb.build(role="scanner", strategy="EQM")
        print(ctx.market_summary())
        print(ctx.open_tickers())
    """

    def __init__(
        self,
        facts_file: str | Path | None = None,
        memory_dir: str | Path | None = None,
        audit_logger: AuditLogger | None = None,
    ):
        self.facts_file = facts_file
        self.memory_dir  = Path(memory_dir) if memory_dir else None
        self._audit = audit_logger
        self._pii = PIIDetector() if audit_logger else None

    def set_audit_logger(self, logger: AuditLogger) -> None:
        """Attach an audit logger after construction."""
        self._audit = logger
        self._pii = PIIDetector()

    def _maybe_audit(self, action, outcome, context_summary=None, **extra):
        if self._audit is None:
            return
        try:
            pii_flags = list(self._pii.scan(context_summary)) if (self._pii and context_summary) else []
            safe_ctx = self._pii.redact(context_summary) if (self._pii and context_summary) else (context_summary or {})
            self._audit.log(action=action, actor="context_builder", outcome=outcome,
                            context=safe_ctx, pii_detected=pii_flags,
                            metadata={k: v for k, v in extra.items() if v})
        except Exception:
            pass

    def build(
        self,
        role: str = "scanner",
        model: str = "auto",
        strategy: str | None = None,
        org_id: str = "",
    ) -> ContextPacket:
        """
        Build and return a ContextPacket.

        Args:
            role:      what this context will be used for (scanner/monitor/analyst)
            model:     which model will consume this context
            strategy:  optional strategy ID (currently unused, for future routing)

        Returns:
            ContextPacket
        """
        facts = load_json(self.facts_file, {}) if self.facts_file else {}

        # Load today's memory
        today_str = date.today().isoformat()
        mem_today = ""
        mem_long  = ""
        if self.memory_dir:
            mem_today = load_text(
                self.memory_dir / f"{today_str}.md", ""
            )
            mem_long  = load_text(
                self.memory_dir / "MEMORY.md", ""
            )

        result = ContextPacket(
            role=role,
            model=model,
            generated_at=datetime.now().isoformat(),
            market=facts.get("market", {}),
            positions=facts.get("positions", []),
            risk_flags=facts.get("risk_flags", {}),
            sizing=facts.get("sizing", {}),
            pending=facts.get("pending", []),
            strategy_rules={},        # Filled by caller if strategy passed
            today_memory=mem_today,
            long_term_mem=mem_long,
            facts_version=facts.get("generated_at", ""),
        )
        safe_summary = dict(facts)
        if org_id:
            safe_summary["org_id"] = org_id
        self._maybe_audit(action="context_build", outcome="success",
                           context_summary=safe_summary, domain=role,
                           facts_file=str(self.facts_file) if self.facts_file else "")
        return result

    def build_scanner_prompt(
        self,
        packet: ContextPacket,
        strategy_name: str,
        strategy_rules: dict,
    ) -> str:
        """
        Build a formatted prompt from a context packet and strategy rules.

        Args:
            packet:          ContextPacket from build()
            strategy_name:   name of strategy (e.g. "EQM")
            strategy_rules:   strategy dict from RulesEngine

        Returns:
            Formatted prompt string
        """
        # Sizing table
        sizing_raw = strategy_rules.get("sizing", {})
        sizing_lines = []
        for regime, cfg in sizing_raw.items():
            if isinstance(cfg, dict):
                sizing_lines.append(
                    f"  {regime}: ${cfg.get('amount','?')} "
                    f"(max {cfg.get('max_positions','?')} positions)"
                )
            else:
                sizing_lines.append(f"  {regime}: {cfg}")
        sizing_txt = "\n".join(sizing_lines) if sizing_lines else "  See rules."

        # Stop/target
        exit_rules = strategy_rules.get("exit", [])
        stop_txt  = "See rules."
        target_txt = "See rules."
        for rule in exit_rules:
            if rule.get("type") in ("stop_loss", "trailing_stop"):
                stop_txt = f"{rule.get('pct', '?')}%"
            elif rule.get("type") == "gain":
                target_txt = f"+{rule.get('pct', rule.get('gain_pct','?'))}%"

        prompt = "\n".join([
            "You are a trading signal scanner. Output ONLY valid JSON.",
            "",
            f"## MARKET STATE (facts.json, {packet.facts_version[:19] if packet.facts_version else 'unknown'})",
            packet.market_summary(),
            "",
            "## OPEN POSITIONS -- DO NOT RE-SIGNAL THESE",
            packet.open_tickers(),
            packet.positions_summary(),
            "",
            "## STRATEGY: {}".format(strategy_name),
            "## RULES",
            "  Entry trigger: {}".format(
                strategy_rules.get("entry_trigger", "N/A")
            ),
            "  Sizing:",
            sizing_txt,
            "  Stop: {}%".format(stop_txt),
            "  Target: {}".format(target_txt),
            "",
            "## YOUR TASK",
            "1. Read the rules above",
            "2. Apply entry/exit/sizing rules mechanically",
            "3. Output ONLY valid JSON matching this schema:",
            json.dumps(strategy_rules.get("signal_output_schema", {}), indent=2),
            "",
            "## OUTPUT",
            "JSON only. No markdown. No explanation.",
            '{"ticker": ...',
        ])
        return prompt


# ─────────────────────────────────────────────────────────────────
# Convenience exports
# ─────────────────────────────────────────────────────────────────
__all__ = ["ContextBuilder", "ContextPacket", "load_json", "load_text"]
