# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
ContextBuilder — ModelFungible Core

Builds a structured context packet from any structured data source.
Domain-agnostic — works with legal, healthcare, finance, HR, or any structured data.
"""
from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def load_json(path, default=None):
    if path is None:
        return default if default is not None else {}
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return default if default is not None else {}


def load_text(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return default


class ContextPacket:
    """
    A structured context bundle for any domain.

    Fields (all optional):
        role:          "scanner" | "monitor" | "analyst" | "custom"
        model:         model hint
        generated_at:  ISO timestamp
        context:       **DOMAIN-AGNOSTIC** — any structured dict
        market:        optional trading market data (backward compat)
        positions:      optional positions (backward compat)
        risk_flags:     optional risk flags (backward compat)
        sizing:         domain-specific sizing
        pending:        tasks still to do in session
        strategy_rules: relevant strategy rule definitions
        today_memory:   today's session notes
        long_term_mem:  long-term memory
        facts_version:  version timestamp of facts for staleness detection
    """

    def __init__(self, **kwargs):
        self.role = kwargs.get("role", "analyst")
        self.model = kwargs.get("model", "auto")
        self.generated_at = kwargs.get("generated_at") or datetime.now().isoformat()
        self.context = kwargs.get("context", {})
        self.market = kwargs.get("market", {})
        self.positions = kwargs.get("positions", [])
        self.risk_flags = kwargs.get("risk_flags", {})
        self.sizing = kwargs.get("sizing", {})
        self.pending = kwargs.get("pending", [])
        self.strategy_rules = kwargs.get("strategy_rules", {})
        self.today_memory = kwargs.get("today_memory", "")
        self.long_term_mem = kwargs.get("long_term_mem", "")
        # facts_version falls back to generated_at for backward compat
        fv = kwargs.get("facts_version")
        ga = kwargs.get("generated_at")
        self.facts_version = fv if fv is not None else (ga if ga else "")

    def context_summary(self) -> str:
        if self.context:
            keys = list(self.context.keys())[:5]
            return "Context keys: " + ", ".join(keys)
        if self.market:
            m = self.market
            reg = str(m.get("regime", "?"))
            vix = str(m.get("vix", "?"))
            spy = str(m.get("spy", "?"))
            return "Regime: " + reg + " | VIX: " + vix + " | SPY: $" + spy
        return "No context data"

    def open_tickers(self) -> str:
        if not self.positions:
            return "None"
        return ", ".join(str(p.get("ticker", "?")) for p in self.positions)

    def positions_summary(self) -> str:
        if not self.positions:
            return "No open positions."
        parts = []
        for p in self.positions:
            ticker = str(p.get("ticker", "?"))
            direction = str(p.get("direction", "?"))
            pnl_pct = p.get("pnl_pct", 0)
            pnl_dollar = p.get("pnl_dollar", 0)
            entry = p.get("entry", 0)
            current = p.get("current", 0)
            pct_sign = "+" if pnl_pct >= 0 else ""
            dol_sign = "+" if pnl_dollar >= 0 else ""
            parts.append(
                ticker + ": " + direction + " | Entry: $" +
                str(round(entry, 2)) + " -> $" + str(round(current, 2)) +
                " | P&L: " + pct_sign + str(round(pnl_pct, 1)) + "% (" +
                dol_sign + "$" + str(round(pnl_dollar)) + ")"
            )
        return "\n".join(parts)

    def risk_summary(self) -> str:
        if not self.risk_flags:
            return "No active risk flags."
        active = [k for k, v in self.risk_flags.items() if v]
        if not active:
            return "None"
        flags_str = ", ".join("[" + str(f) + "]" for f in active)
        return "Active risk flags: " + flags_str

    def market_summary(self) -> str:
        m = self.market
        if not m:
            return "No market data"
        reg = str(m.get("regime", "?"))
        vix = str(m.get("vix", "?"))
        vix_reg = str(m.get("vix_regime", "?"))
        spy = str(m.get("spy", "?"))
        ma200 = str(m.get("spy_ma200", "?"))
        dist = round(m.get("spy_ma200_dist", 0), 2)
        dist_sign = "+" if dist >= 0 else ""
        return (
            "Regime: " + reg +
            " | VIX: " + vix + " (" + vix_reg + ")" +
            " | SPY: $" + spy + " (MA200: $" + ma200 + ", " + dist_sign + str(dist) + "%)"
        )

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "model": self.model,
            "generated_at": self.generated_at,
            "context": self.context,
            "market": self.market,
            "positions": self.positions,
            "risk_flags": self.risk_flags,
            "sizing": self.sizing,
            "pending": self.pending,
            "facts_version": self.facts_version,
        }


class ContextBuilder:
    """
    Builds ContextPacket from any structured data source.

    Usage:
        cb = ContextBuilder(facts_file="my_context.json")
        ctx = cb.build(role="analyst")

        cb = ContextBuilder()
        ctx = cb.build(role="analyst", domain_data={"contracts": [...], "jurisdiction": "NY"})
        prompt = cb.build_prompt(ctx, "my_strategy", rules)
    """

    def __init__(self, facts_file: Optional[str] = None, memory_dir: Optional[str] = None):
        self.facts_file = facts_file
        self.memory_dir = Path(memory_dir) if memory_dir else None
        self._facts = load_json(facts_file, {})

    def build(
        self,
        role: str = "analyst",
        domain_data: Optional[dict] = None,
        **extra,
    ) -> ContextPacket:
        context_data = domain_data if domain_data is not None else self._facts.get("context", {})
        return ContextPacket(
            role=role,
            model=self._facts.get("model", "auto"),
            generated_at=self._facts.get("generated_at", datetime.now().isoformat()),
            context=context_data,
            market=self._facts.get("market", {}),
            positions=self._facts.get("positions", []),
            risk_flags=self._facts.get("risk_flags", {}),
            sizing=self._facts.get("sizing", {}),
            pending=self._facts.get("pending", []),
            strategy_rules=self._facts.get("strategy_rules", {}),
            today_memory=self._load_today_memory(),
            long_term_mem=self._facts.get("long_term_memory", ""),
            facts_version=self._facts.get("facts_version") or self._facts.get("generated_at") or "",
        )

    def _load_today_memory(self) -> str:
        if not self.memory_dir:
            return ""
        today = datetime.now().strftime("%Y-%m-%d")
        path = self.memory_dir / (today + ".md")
        return load_text(path)

    def build_prompt(
        self,
        ctx: ContextPacket,
        strategy_id: str,
        strategy_rules: dict,
    ) -> str:
        role_instr = self._role_instruction(ctx.role)
        strat_block = self._format_strategy(strategy_id, strategy_rules)
        ctx_block = self._format_context(ctx)
        schema_block = self._format_output_schema(
            strategy_rules.get("signal_output_schema", {})
        )
        parts = [
            "You are a " + ctx.role + " using ModelFungible.",
            "",
            role_instr,
            "",
            strat_block,
            "",
            ctx_block,
            "",
            schema_block,
            "",
            "Respond ONLY with valid JSON matching the schema above. No extra text.",
        ]
        return "\n".join(parts)

    def build_scanner_prompt(self, ctx, strategy_id, strategy_rules) -> str:
        return self.build_prompt(ctx, strategy_id, strategy_rules)

    def build_monitor_prompt(self, ctx, strategy_id, strategy_rules) -> str:
        return self.build_prompt(ctx, strategy_id, strategy_rules)

    def _role_instruction(self, role: str) -> str:
        mapping = {
            "scanner": "You scan structured data and identify actionable items based on the strategy rules.",
            "monitor": "You continuously monitor data and flag changes that match strategy triggers.",
            "analyst": "You analyze structured data and produce decisions based on the strategy rules.",
            "custom": "You follow the strategy rules provided to analyze context and produce a decision.",
        }
        return mapping.get(role, "You are a " + role + " following the strategy rules provided.")

    def _format_strategy(self, strategy_id: str, rules: dict) -> str:
        lines = [
            "## Strategy: " + rules.get("name", strategy_id),
            rules.get("description", ""),
            "",
            "### Entry Trigger",
            "`" + rules.get("entry_trigger", "none") + "`",
            "",
            "### Sizing",
        ]
        sizing = rules.get("sizing", {})
        if sizing:
            for regime, config in sizing.items():
                lines.append("  [" + regime + "]: " + str(config))
        else:
            lines.append("  (no sizing rules)")
        exits = rules.get("exit", [])
        if exits:
            lines.append("")
            lines.append("### Exit Rules")
            for ex in exits:
                lines.append("  - " + str(ex))
        return "\n".join(lines)

    def _format_context(self, ctx: ContextPacket) -> str:
        lines = ["## Context Data"]

        if ctx.context:
            lines.append("### Domain Data")
            ctx_str = json.dumps(ctx.context, indent=2, default=str)
            lines.append(ctx_str[:2000])
            lines.append("")

        if ctx.market:
            lines.append("### Market State")
            m = ctx.market
            lines.append("- Regime: " + str(m.get("regime", "?")))
            lines.append("- VIX: " + str(m.get("vix", "?")) + " (" +
                         str(m.get("vix_regime", "?")) + ")")
            spy = m.get("spy")
            if spy:
                ma200 = str(m.get("spy_ma200", "?"))
                dist = round(m.get("spy_ma200_dist", 0), 2)
                dist_sign = "+" if dist >= 0 else ""
                lines.append("- SPY: $" + str(spy) + " (MA200: $" + ma200 +
                             ", " + dist_sign + str(dist) + "%)")
            lines.append("")

        if ctx.positions:
            lines.append("### Open Positions")
            for p in ctx.positions:
                ticker = str(p.get("ticker", "?"))
                direction = str(p.get("direction", "?"))
                pnl_pct = p.get("pnl_pct", 0)
                pnl_sign = "+" if pnl_pct >= 0 else ""
                lines.append("- " + ticker + ": " + direction +
                             " | P&L: " + pnl_sign + str(pnl_pct) + "%")
            lines.append("")

        if ctx.risk_flags:
            lines.append("### Risk Flags")
            for flag, active in ctx.risk_flags.items():
                if active:
                    lines.append("- [RISK] " + str(flag))
            lines.append("")

        if ctx.today_memory:
            lines.append("### Today's Notes\n" + ctx.today_memory[:500])

        if ctx.long_term_mem:
            lines.append("\n### Long-Term Memory\n" + ctx.long_term_mem[:500])

        return "\n".join(lines)

    def _format_output_schema(self, schema: dict) -> str:
        if not schema:
            return "## Output Schema\n{ /* your output schema here */ }"
        lines = ["## Output Schema (required)"]
        for field, type_desc in schema.items():
            lines.append("  " + str(field) + ": " + str(type_desc))
        return "\n".join(lines)


__all__ = ["ContextBuilder", "ContextPacket"]
