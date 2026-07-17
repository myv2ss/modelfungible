#!/usr/bin/env python3
"""
Unit tests for Context Builder.
Tests: market state aggregation, position tracking, risk flags,
       session recovery context, pending tasks.
"""
import pytest, json, tempfile, os
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────
@pytest.fixture
def facts_file():
    """Temp trading_desk_state.json with full market context."""
    facts = {
        "generated_at": "2026-07-17T14:00:00",
        "market": {
            "regime": "CONFIRMED_BULL",
            "vix": 16.7,
            "vix_regime": "CALM",
            "spy": 749.17,
            "spy_ma200": 694.91,
            "spy_ma200_dist": 7.81,
            "qqq": 480.22,
            "qqq_ma200": 430.00,
            "qqq_ma200_dist": 11.68,
        },
        "positions": [
            {
                "ticker": "UPS",
                "direction": "LONG",
                "entry": 109.76,
                "current": 117.18,
                "pnl_dollar": 742,
                "pnl_pct": 6.8,
                "stop": 93.0,
                "target": 185.0,
            }
        ],
        "risk_flags": {
            "vix_elevated": False,
            "bear_regime": False,
            "spy_below_ma200": False,
        },
        "sizing": {
            "CONFIRMED_BULL": {"amount": 4500, "max_positions": 3},
        }
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(facts, f)
    yield path
    os.unlink(path)


@pytest.fixture
def memory_today():
    """Temp today's memory file."""
    content = "# 2026-07-17\n\nTesting context builder.\n"
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    yield path
    os.unlink(path)


@pytest.fixture
def memory_long():
    """Temp long-term memory file."""
    content = "# MEMORY.md\n\nKey facts.\n"
    fd, path = tempfile.mkstemp(suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    yield path
    os.unlink(path)


# ─────────────────────────────────────────────────────────────────
# Tests: Market state
# ─────────────────────────────────────────────────────────────────
class TestMarketState:
    def test_regime_extracted(self, facts_file, memory_today, memory_long):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(
            facts_file=facts_file,
            memory_dir=Path(tempfile.gettempdir()),
        )
        ctx = cb.build()
        assert ctx.market["regime"] == "CONFIRMED_BULL"

    def test_vix_extracted(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build()
        assert ctx.market["vix"] == 16.7
        assert ctx.market["vix_regime"] == "CALM"

    def test_spy_extracted(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build()
        assert ctx.market["spy"] == 749.17
        assert ctx.market["spy_ma200_dist"] == 7.81

    def test_market_summary_format(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build()
        summary = ctx.market_summary()
        assert "CONFIRMED_BULL" in summary
        assert "16.7" in summary
        assert "749.17" in summary


# ─────────────────────────────────────────────────────────────────
# Tests: Positions
# ─────────────────────────────────────────────────────────────────
class TestPositions:
    def test_positions_loaded(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build()
        assert len(ctx.positions) == 1
        assert ctx.positions[0]["ticker"] == "UPS"

    def test_open_tickers(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build()
        assert ctx.open_tickers() == "UPS"

    def test_positions_summary_includes_pnl(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build()
        summary = ctx.positions_summary()
        assert "UPS" in summary
        assert "+6.8%" in summary

    def test_empty_positions(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        # Inject empty positions
        facts = json.load(open(facts_file))
        facts["positions"] = []
        fd, path = tempfile.mkstemp(suffix=".json")
        json.dump(facts, open(path, "w"))

        cb = ContextBuilder(facts_file=path)
        ctx = cb.build()
        assert len(ctx.positions) == 0
        assert ctx.open_tickers() == "None"
        assert "No open positions" in ctx.positions_summary()
        os.unlink(path)


# ─────────────────────────────────────────────────────────────────
# Tests: Risk flags
# ─────────────────────────────────────────────────────────────────
class TestRiskFlags:
    def test_risk_flags_loaded(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build()
        assert ctx.risk_flags["vix_elevated"] is False
        assert ctx.risk_flags["bear_regime"] is False

    def test_risk_summary_none_when_clear(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build()
        assert ctx.risk_summary() == "None"

    def test_risk_summary_active_flag(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        facts = json.load(open(facts_file))
        facts["risk_flags"]["vix_elevated"] = True
        fd, path = tempfile.mkstemp(suffix=".json")
        json.dump(facts, open(path, "w"))

        cb = ContextBuilder(facts_file=path)
        ctx = cb.build()
        assert "vix_elevated" in ctx.risk_summary()
        os.unlink(path)


# ─────────────────────────────────────────────────────────────────
# Tests: Facts version
# ─────────────────────────────────────────────────────────────────
class TestFactsVersion:
    def test_facts_version_extracted(self, facts_file):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(facts_file=facts_file)
        ctx = cb.build()
        assert ctx.facts_version == "2026-07-17T14:00:00"


# ─────────────────────────────────────────────────────────────────
# Tests: Missing/empty facts file
# ─────────────────────────────────────────────────────────────────
class TestEdgeCases:
    def test_missing_facts_file_returns_defaults(self):
        from modelfungible.core.context_builder import ContextBuilder

        cb = ContextBuilder(facts_file="/nonexistent/file.json")
        ctx = cb.build()
        assert ctx.market == {}
        assert ctx.positions == []
        assert ctx.facts_version == ""

    def test_partial_facts_file(self):
        """Facts file with only some fields."""
        facts = {"market": {"regime": "BEAR"}}
        fd, path = tempfile.mkstemp(suffix=".json")
        json.dump(facts, open(path, "w"))

        from modelfungible.core.context_builder import ContextBuilder
        cb = ContextBuilder(facts_file=path)
        ctx = cb.build()
        assert ctx.market["regime"] == "BEAR"
        os.unlink(path)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
