# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.
# Commercial use requires a license. Unauthorized use is prohibited.

#!/usr/bin/env python3
"""
Session Manager — ModelFungible Crash Recovery

Snapshots state before task pipelines.
If the agent crashes, the next agent resumes from where it left off.

Flow:
    snapshot_state()  → before pipeline
    update_step()    → after each task
    check_incomplete() → on next startup
    resume_context() → restore state
    clear_snapshot() → on clean completion
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ─────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────
SNAPSHOT_VERSION = "1.0"
CRASH_THRESHOLD_MINUTES = 30


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────
def load_json(path: Path | str, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}


def save_json(path: Path | str, data: dict):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ─────────────────────────────────────────────────────────────────
# SessionManager
# ─────────────────────────────────────────────────────────────────
class SessionManager:
    """
    Crash-recovery session manager.

    Example:
        sm = SessionManager()

        # Before pipeline
        sm.snapshot_state(strategy="EQM", pending_tasks=["scan", "format", "send"])

        # After each step
        sm.update_step("scan", conclusion="OK", output_ref="/tmp/scan.json")

        # On next startup
        if sm.check_incomplete():
            ctx = sm.resume_context()
            pending = sm.get_pending_tasks()
            print(sm.resume_summary())

        # On clean completion
        sm.clear_snapshot()
    """

    def __init__(
        self,
        facts_file: str | Path | None = None,
        snapshot_file: str | Path | None = None,
    ):
        self.facts_file   = Path(facts_file)   if facts_file   else None
        self.snapshot_file = Path(snapshot_file) if snapshot_file else None

    # ── Snapshot ────────────────────────────────────────────────

    def snapshot_state(
        self,
        strategy: str,
        pending_tasks: list[str],
        pipeline_name: str | None = None,
        extra: dict | None = None,
    ) -> dict:
        """
        Save a state snapshot before starting a task pipeline.

        Args:
            strategy:       strategy name (EQM, PEAD-3, etc.)
            pending_tasks:  ordered list of task IDs to complete
            pipeline_name:  optional human-readable pipeline name
            extra:         optional extra context dict

        Returns:
            The snapshot dict that was saved.
        """
        facts = load_json(self.facts_file, {}) if self.facts_file else {}

        snapshot = {
            "version": SNAPSHOT_VERSION,
            "strategy":     strategy,
            "pipeline":     pipeline_name or strategy,
            "snapshotted_at": datetime.now().isoformat(),

            # Market facts at time of snapshot
            "facts": {
                "generated_at":  facts.get("generated_at", ""),
                "regime":      facts.get("market", {}).get("regime", "UNKNOWN"),
                "vix":         facts.get("market", {}).get("vix", 0),
                "spy":         facts.get("market", {}).get("spy", 0),
                "risk_flags":  facts.get("risk_flags", {}),
            },
            "facts_version":   facts.get("generated_at", ""),

            # Task tracking
            "pending_tasks":    list(pending_tasks),
            "completed_tasks": [],

            # Extra
            "extra": dict(extra) if extra else {},
        }

        save_json(self.snapshot_file, snapshot)
        return snapshot

    def update_step(
        self,
        task_id: str,
        conclusion: str = "",
        output_ref: str = "",
        error: str | None = None,
    ):
        """
        Mark a task as completed. Prunes from pending, appends to completed.
        """
        snap = load_json(self.snapshot_file)
        if not snap:
            return

        completed = list(snap.get("completed_tasks", []))
        pending   = list(snap.get("pending_tasks", []))

        completed.append({
            "task":          task_id,
            "conclusion":    conclusion,
            "output_ref":    output_ref,
            "error":         error,
            "completed_at":  datetime.now().isoformat(),
        })
        if task_id in pending:
            pending.remove(task_id)

        snap["completed_tasks"] = completed
        snap["pending_tasks"]   = pending
        snap["last_step"]      = completed[-1]
        snap["updated_at"]     = datetime.now().isoformat()
        save_json(self.snapshot_file, snap)

    # ── Getters ─────────────────────────────────────────────────

    def get_pending_tasks(self) -> list[str]:
        """Ordered list of task IDs still to do."""
        snap = load_json(self.snapshot_file, {})
        return list(snap.get("pending_tasks", []))

    def get_completed_tasks(self) -> list[dict]:
        """List of completed task records."""
        snap = load_json(self.snapshot_file, {})
        return list(snap.get("completed_tasks", []))

    def clear_snapshot(self):
        """Remove snapshot on clean completion."""
        try:
            Path(self.snapshot_file).unlink(missing_ok=True)
        except Exception:
            pass

    # ── Crash detection ─────────────────────────────────────────

    def check_incomplete(self) -> Optional[dict]:
        """
        Check for an incomplete session to resume.

        Returns:
            Snapshot dict if found and incomplete,
            or None if no incomplete session.

        A session is considered "crashed" if it has pending tasks
        older than CRASH_THRESHOLD_MINUTES.
        """
        snap = load_json(self.snapshot_file, None)
        if not snap:
            return None

        # Clean completion: no pending tasks
        if not snap.get("pending_tasks"):
            self.clear_snapshot()
            return None

        # Check crash threshold
        try:
            snap_time    = datetime.fromisoformat(snap["snapshotted_at"])
            age_minutes  = (datetime.now() - snap_time).total_seconds() / 60
            if age_minutes > CRASH_THRESHOLD_MINUTES:
                snap["_crash_recovered"]   = True
                snap["_crash_age_minutes"] = round(age_minutes, 1)
                return snap
        except Exception:
            pass

        return snap  # Still in progress

    def is_crashed(self) -> bool:
        """True if snapshot has pending tasks older than crash threshold."""
        snap = load_json(self.snapshot_file, None)
        if not snap or not snap.get("pending_tasks"):
            return False
        try:
            snap_time   = datetime.fromisoformat(snap["snapshotted_at"])
            age_minutes = (datetime.now() - snap_time).total_seconds() / 60
            return age_minutes > CRASH_THRESHOLD_MINUTES
        except Exception:
            return False

    def crash_age_minutes(self) -> float:
        """Age of current snapshot in minutes. 0 if no snapshot."""
        snap = load_json(self.snapshot_file, None)
        if not snap:
            return 0.0
        try:
            snap_time = datetime.fromisoformat(snap["snapshotted_at"])
            return (datetime.now() - snap_time).total_seconds() / 60
        except Exception:
            return 0.0

    # ── Resume ─────────────────────────────────────────────────

    def resume_context(self) -> "ContextPacket":
        """
        Build a ContextPacket from the snapshot for crash recovery.

        Restores market state from snapshot facts to ensure the resumed
        agent sees the same market conditions as the crashed agent.
        """
        from modelfungible.core.context_builder import ContextPacket

        snap  = load_json(self.snapshot_file, {})
        facts = load_json(self.facts_file, {}) if self.facts_file else {}

        snap_facts_time = snap.get("facts_version", "")
        facts_time      = facts.get("generated_at", "")

        # Prefer snapshot facts (they were current when session started)
        if snap_facts_time:
            try:
                snap_dt   = datetime.fromisoformat(snap_facts_time)
                facts_dt  = datetime.fromisoformat(facts_time) if facts_time else None
                if facts_dt and snap_dt > facts_dt:
                    use_market = {
                        "regime": snap.get("facts", {}).get("regime", "UNKNOWN"),
                        "vix":    snap.get("facts", {}).get("vix", 0),
                        "spy":    snap.get("facts", {}).get("spy", 0),
                    }
                    use_risk_flags = snap.get("facts", {}).get("risk_flags", {})
                    use_facts_ver  = snap_facts_time
                else:
                    use_market      = facts.get("market", {})
                    use_risk_flags = facts.get("risk_flags", {})
                    use_facts_ver  = facts_time
            except Exception:
                use_market      = facts.get("market", {})
                use_risk_flags = facts.get("risk_flags", {})
                use_facts_ver  = facts_time
        else:
            use_market      = facts.get("market", {})
            use_risk_flags = facts.get("risk_flags", {})
            use_facts_ver  = facts_time

        return ContextPacket(
            role="resumed",
            model="auto",
            generated_at=datetime.now().isoformat(),
            market=use_market,
            positions=facts.get("positions", []),
            risk_flags=use_risk_flags,
            sizing=facts.get("sizing", {}),
            pending=snap.get("pending_tasks", []),
            strategy_rules={},
            facts_version=use_facts_ver,
        )

    def resume_summary(self) -> str:
        """
        Human-readable summary for crash recovery reporting.
        """
        snap = load_json(self.snapshot_file, {})
        if not snap:
            return "No incomplete session."

        strategy   = snap.get("strategy", "?")
        pending   = snap.get("pending_tasks", [])
        completed = [s["task"] for s in snap.get("completed_tasks", [])]
        crashed   = snap.get("_crash_recovered", False)
        age       = snap.get("_crash_age_minutes", 0)
        snap_time = snap.get("snapshotted_at", "?")
        pipeline  = snap.get("pipeline", "?")

        lines = [
            f"Pipeline:  {pipeline}",
            f"Strategy:  {strategy}",
            f"Snapshot:  {snap_time}",
        ]
        if crashed:
            lines.append(f"⚠️  Crashed {age:.1f} min ago — auto-recovering")
        lines.append(f"Done:     {', '.join(completed) if completed else 'none'}")
        lines.append(f"Pending:  {', '.join(pending)}")
        return " | ".join(lines)


# ─────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────
__all__ = ["SessionManager", "load_json", "save_json"]
