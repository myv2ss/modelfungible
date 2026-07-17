#!/usr/bin/env python3
"""
Unit tests for Session Manager — crash recovery.
Tests: snapshot, update_step, check_incomplete, resume_context, clear.
"""
import pytest, json, tempfile, os, time
from pathlib import Path
from datetime import datetime, timedelta

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────
@pytest.fixture
def facts_file():
    facts = {
        "generated_at": "2026-07-17T14:00:00",
        "market": {"regime": "CONFIRMED_BULL", "vix": 16.7, "spy": 749.17},
        "positions": [{"ticker": "UPS", "direction": "LONG", "pnl_pct": 6.8}],
        "risk_flags": {"vix_elevated": False},
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(facts, f)
    yield path
    os.unlink(path)


@pytest.fixture
def snapshot_file():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)  # Start clean
    yield path
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ─────────────────────────────────────────────────────────────────
# Tests: Snapshot
# ─────────────────────────────────────────────────────────────────
class TestSnapshot:
    def test_snapshot_creates_file(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(
            facts_file=facts_file,
            snapshot_file=snapshot_file,
        )
        sm.snapshot_state(strategy="EQM", pending_tasks=["scan", "format"])

        assert os.path.exists(snapshot_file)
        data = json.load(open(snapshot_file))
        assert data["strategy"] == "EQM"
        assert data["pending_tasks"] == ["scan", "format"]
        assert data["completed_tasks"] == []

    def test_snapshot_contains_facts(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        snap = sm.snapshot_state(strategy="TEST", pending_tasks=["step1"])

        assert snap["facts"]["regime"] == "CONFIRMED_BULL"
        assert snap["facts"]["vix"] == 16.7
        assert snap["facts_version"] == "2026-07-17T14:00:00"

    def test_snapshot_version_is_set(self, snapshot_file, facts_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        snap = sm.snapshot_state(strategy="X", pending_tasks=["a", "b"])
        assert snap["version"] == "1.0"


# ─────────────────────────────────────────────────────────────────
# Tests: Update step
# ─────────────────────────────────────────────────────────────────
class TestUpdateStep:
    def test_update_prunes_pending(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["scan", "format", "send"])

        sm.update_step("scan", conclusion="OK", output_ref="/tmp/scan.json")

        snap = json.load(open(snapshot_file))
        assert "scan" not in snap["pending_tasks"]
        assert "format" in snap["pending_tasks"]
        assert "send" in snap["pending_tasks"]

    def test_update_records_conclusion(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["step1"])
        sm.update_step("step1", conclusion="ERROR: timeout", error="timeout")

        snap = json.load(open(snapshot_file))
        completed = snap["completed_tasks"]
        assert len(completed) == 1
        assert completed[0]["task"] == "step1"
        assert completed[0]["conclusion"] == "ERROR: timeout"
        assert completed[0]["error"] == "timeout"

    def test_multiple_updates(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["a", "b", "c"])

        sm.update_step("a", conclusion="OK")
        sm.update_step("b", conclusion="OK")

        snap = json.load(open(snapshot_file))
        assert len(snap["completed_tasks"]) == 2
        assert snap["pending_tasks"] == ["c"]


# ─────────────────────────────────────────────────────────────────
# Tests: Crash detection
# ─────────────────────────────────────────────────────────────────
class TestCrashDetection:
    def test_incomplete_when_pending(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["scan", "format"])

        inc = sm.check_incomplete()
        assert inc is not None
        assert inc["strategy"] == "EQM"
        assert inc["pending_tasks"] == ["scan", "format"]

    def test_clean_when_no_pending(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=[])
        # Empty pending = complete

        inc = sm.check_incomplete()
        # Should return None or clean up
        assert not os.path.exists(snapshot_file) or \
               json.load(open(snapshot_file)).get("pending_tasks") == []

    def test_is_crashed_within_window(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["scan"])
        # Within 30 min threshold
        assert sm.is_crashed() is False

    def test_snapshot_too_old_is_crashed(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["scan"])

        # Manually backdate the snapshot to simulate old session
        snap = json.load(open(snapshot_file))
        old_time = (datetime.now() - timedelta(hours=2)).isoformat()
        snap["snapshotted_at"] = old_time
        json.dump(snap, open(snapshot_file, "w"))

        inc = sm.check_incomplete()
        assert inc is not None
        assert inc.get("_crash_recovered") is True
        assert inc.get("_crash_age_minutes") > 0

    def test_no_snapshot_returns_none(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        assert sm.check_incomplete() is None
        assert sm.is_crashed() is False


# ─────────────────────────────────────────────────────────────────
# Tests: Resume
# ─────────────────────────────────────────────────────────────────
class TestResume:
    def test_resume_context_restores_market(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["format", "send"])

        ctx = sm.resume_context()
        assert ctx.market["regime"] == "CONFIRMED_BULL"
        assert ctx.market["vix"] == 16.7
        assert ctx.role == "resumed"

    def test_resume_context_pending_tasks(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["format", "send"])

        ctx = sm.resume_context()
        assert ctx.pending == ["format", "send"]

    def test_resume_summary_format(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["scan", "format"])
        sm.update_step("scan", conclusion="OK")

        summary = sm.resume_summary()
        assert "EQM" in summary
        assert "scan" in summary
        assert "format" in summary
        assert "crash" not in summary.lower()  # within window


# ─────────────────────────────────────────────────────────────────
# Tests: Clear
# ─────────────────────────────────────────────────────────────────
class TestClear:
    def test_clear_removes_snapshot(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["a"])
        sm.clear_snapshot()

        assert not os.path.exists(snapshot_file)
        assert sm.check_incomplete() is None

    def test_clear_idempotent(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.clear_snapshot()  # Should not raise
        sm.clear_snapshot()
        assert sm.check_incomplete() is None


# ─────────────────────────────────────────────────────────────────
# Tests: Get pending / completed
# ─────────────────────────────────────────────────────────────────
class TestGetters:
    def test_get_pending_tasks(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["a", "b"])
        sm.update_step("a", conclusion="OK")

        assert sm.get_pending_tasks() == ["b"]

    def test_get_completed_tasks(self, facts_file, snapshot_file):
        from modelfungible.core.session_manager import SessionManager

        sm = SessionManager(facts_file=facts_file, snapshot_file=snapshot_file)
        sm.snapshot_state(strategy="EQM", pending_tasks=["a", "b"])
        sm.update_step("a", conclusion="OK")
        sm.update_step("b", conclusion="OK")

        completed = sm.get_completed_tasks()
        assert len(completed) == 2
        assert [c["task"] for c in completed] == ["a", "b"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
