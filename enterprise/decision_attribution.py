# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Decision Attribution — log every model selection decision with full routing reasoning.

Every /api/execute call gets a decision record:
- Why was this model selected?
- What other models were considered?
- What were their scores?
- What was the routing context?

This turns the gateway into an explainable AI layer — critical for regulated industries.
"""
from __future__ import annotations

import json, os, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field, asdict


DB_PATH = os.environ.get("MODELFUNGIBLE_DECISIONS_DB",
                          str(Path(__file__).parent.parent / "data" / "decisions.db"))


@dataclass
class ModelScore:
    """Score breakdown for a single model candidate."""
    model_name: str
    provider: str
    model_id: str
    score: float
    latency_ms: int
    cost_score: float
    speed_score: float
    capability_score: float
    final_score: float
    was_selected: bool
    was_tried: bool
    failure_reason: str = ""


@dataclass
class RoutingContext:
    """The full routing decision context."""
    request_id: str
    timestamp: str
    actor: str
    org_id: str
    mode: str                    # fastest | cheapest | balanced | capability
    capability: str
    explicit_model: str          # was a specific model requested?
    candidate_count: int
    selected_model: str
    selected_provider: str
    fallback_order: list[str]
    scores: list[ModelScore]
    request_summary: str         # first 100 chars of prompt
    piid_detected: bool
    total_latency_ms: int
    total_cost_usd: float
    attempt_count: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.Connection(DB_PATH, autocommit=True)
    conn.execute("PRAGMA journal_mode=WAL")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS decisions (
        request_id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        org_id TEXT DEFAULT 'default-org',
        mode TEXT NOT NULL,
        capability TEXT DEFAULT 'any',
        explicit_model TEXT DEFAULT '',
        candidate_count INTEGER DEFAULT 0,
        selected_model TEXT NOT NULL,
        selected_provider TEXT DEFAULT '',
        fallback_order TEXT DEFAULT '[]',
        request_summary TEXT DEFAULT '',
        piid_detected INTEGER DEFAULT 0,
        total_latency_ms INTEGER DEFAULT 0,
        total_cost_usd REAL DEFAULT 0.0,
        attempt_count INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS decision_scores (
        score_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL,
        model_name TEXT NOT NULL,
        provider TEXT DEFAULT '',
        model_id TEXT DEFAULT '',
        score REAL DEFAULT 0.0,
        latency_ms INTEGER DEFAULT 0,
        cost_score REAL DEFAULT 0.0,
        speed_score REAL DEFAULT 0.0,
        capability_score REAL DEFAULT 0.0,
        final_score REAL DEFAULT 0.0,
        was_selected INTEGER DEFAULT 0,
        was_tried INTEGER DEFAULT 0,
        failure_reason TEXT DEFAULT '',
        FOREIGN KEY (request_id) REFERENCES decisions(request_id)
    );

    CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp);
    CREATE INDEX IF NOT EXISTS idx_decisions_actor ON decisions(actor);
    CREATE INDEX IF NOT EXISTS idx_decisions_mode ON decisions(mode);
    CREATE INDEX IF NOT EXISTS idx_scores_request ON decision_scores(request_id);
    CREATE INDEX IF NOT EXISTS idx_scores_model ON decision_scores(model_name);
    """)


class DecisionStore:
    """Store and query routing decisions."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.Connection(db_path, autocommit=True)
        self._conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(self._conn)

    def record(
        self,
        request_id: str,
        actor: str,
        mode: str,
        selected_model: str,
        selected_provider: str,
        fallback_order: list[str],
        scores: list[ModelScore],
        request_summary: str = "",
        capability: str = "any",
        explicit_model: str = "",
        piid_detected: bool = False,
        total_latency_ms: int = 0,
        total_cost_usd: float = 0.0,
        attempt_count: int = 1,
        org_id: str = "default-org",
    ) -> str:
        """Record a routing decision."""
        now = _now()
        self._conn.execute("""
            INSERT INTO decisions
            (request_id, timestamp, actor, org_id, mode, capability, explicit_model,
             candidate_count, selected_model, selected_provider, fallback_order,
             request_summary, piid_detected, total_latency_ms, total_cost_usd, attempt_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            request_id, now, actor, org_id, mode, capability, explicit_model,
            len(scores), selected_model, selected_provider,
            json.dumps(fallback_order), request_summary[:100],
            1 if piid_detected else 0,
            total_latency_ms, total_cost_usd, attempt_count,
        ])

        for s in scores:
            sid = f"score_{uuid.uuid4().hex[:12]}"
            self._conn.execute("""
                INSERT INTO decision_scores
                (score_id, request_id, model_name, provider, model_id, score, latency_ms,
                 cost_score, speed_score, capability_score, final_score,
                 was_selected, was_tried, failure_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                sid, request_id, s.model_name, s.provider, s.model_id,
                s.score, s.latency_ms, s.cost_score, s.speed_score,
                s.capability_score, s.final_score,
                1 if s.was_selected else 0,
                1 if s.was_tried else 0,
                s.failure_reason,
            ])
        return request_id

    def get(self, request_id: str) -> Optional[RoutingContext]:
        row = self._conn.execute(
            "SELECT * FROM decisions WHERE request_id=?", [request_id]
        ).fetchone()
        if not row:
            return None
        scores = self._get_scores(request_id)
        return self._row_to_context(row, scores)

    def _get_scores(self, request_id: str) -> list[ModelScore]:
        rows = self._conn.execute(
            "SELECT * FROM decision_scores WHERE request_id=?", [request_id]
        ).fetchall()
        return [self._row_to_score(r) for r in rows]

    def _row_to_context(self, row: sqlite3.Row, scores: list[ModelScore]) -> RoutingContext:
        return RoutingContext(
            request_id=row[0], timestamp=row[1], actor=row[2], org_id=row[3],
            mode=row[4], capability=row[5], explicit_model=row[6],
            candidate_count=row[7], selected_model=row[8], selected_provider=row[9],
            fallback_order=json.loads(row[10]),
            request_summary=row[11], piid_detected=bool(row[12]),
            total_latency_ms=row[13], total_cost_usd=row[14], attempt_count=row[15],
            scores=scores,
        )

    def _row_to_score(self, row: sqlite3.Row) -> ModelScore:
        return ModelScore(
            score_id=row[0], request_id=row[1], model_name=row[2],
            provider=row[3], model_id=row[4], score=row[5],
            latency_ms=row[6], cost_score=row[7], speed_score=row[8],
            capability_score=row[9], final_score=row[10],
            was_selected=bool(row[11]), was_tried=bool(row[12]),
            failure_reason=row[13],
        )

    def explain(self, request_id: str) -> Optional[dict]:
        """Return a human-readable explanation of why a model was selected."""
        ctx = self.get(request_id)
        if not ctx:
            return None

        lines = [
            f"**Model Selected:** {ctx.selected_model} ({ctx.selected_provider})",
            f"**Routing Mode:** {ctx.mode}",
            f"**Why it was chosen:**",
        ]

        if ctx.explicit_model:
            lines.append(f"- You explicitly requested: {ctx.explicit_model}")
        else:
            if ctx.mode == "fastest":
                best = max(ctx.scores, key=lambda s: s.speed_score)
                lines.append(f"- Fastest model: {best.model_name} at {best.latency_ms}ms")
                lines.append(f"- {ctx.selected_model} selected (2nd fastest but better overall score)")
            elif ctx.mode == "cheapest":
                best = min(ctx.scores, key=lambda s: s.cost_score)
                lines.append(f"- {ctx.selected_model} has the lowest cost among {ctx.candidate_count} candidates")
            elif ctx.mode == "balanced":
                winner = next((s for s in ctx.scores if s.was_selected), None)
                if winner:
                    lines.append(f"- Balanced score: {winner.final_score:.3f}")
                    lines.append(f"  Speed: {winner.speed_score:.2f}, Cost: {winner.cost_score:.2f}, Capability: {winner.capability_score:.2f}")
            elif ctx.mode == "capability":
                lines.append(f"- Closest capability match for '{ctx.capability}'")

        lines.append(f"**Candidates considered:** {', '.join(s.model_name for s in ctx.scores)}")
        if ctx.fallback_order:
            lines.append(f"**Fallback order:** {' → '.join(ctx.fallback_order)}")
        if ctx.attempt_count > 1:
            lines.append(f"**Retries:** {ctx.attempt_count} attempts (primary model failed)")

        return {
            "request_id": ctx.request_id,
            "timestamp": ctx.timestamp,
            "selected_model": ctx.selected_model,
            "selected_provider": ctx.selected_provider,
            "explanation": "\n".join(lines),
            "scores": [
                {
                    "model": s.model_name,
                    "provider": s.provider,
                    "final_score": round(s.final_score, 3),
                    "latency_ms": s.latency_ms,
                    "cost_score": round(s.cost_score, 3),
                    "speed_score": round(s.speed_score, 3),
                    "capability_score": round(s.capability_score, 3),
                    "was_selected": s.was_selected,
                    "was_tried": s.was_tried,
                    "failure_reason": s.failure_reason,
                }
                for s in sorted(ctx.scores, key=lambda x: x.final_score, reverse=True)
            ],
            "context": {
                "mode": ctx.mode,
                "capability": ctx.capability,
                "explicit_model": ctx.explicit_model,
                "candidate_count": ctx.candidate_count,
                "total_latency_ms": ctx.total_latency_ms,
                "total_cost_usd": round(ctx.total_cost_usd, 6),
                "attempt_count": ctx.attempt_count,
                "pii_detected": ctx.piid_detected,
            }
        }

    def query(
        self,
        actor: str = None,
        model: str = None,
        mode: str = None,
        start_date: str = None,
        end_date: str = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RoutingContext]:
        query = "SELECT * FROM decisions WHERE 1=1"
        params = []
        if actor:
            query += " AND actor=?"
            params.append(actor)
        if model:
            query += " AND selected_model=?"
            params.append(model)
        if mode:
            query += " AND mode=?"
            params.append(mode)
        if start_date:
            query += " AND timestamp>=?"
            params.append(start_date)
        if end_date:
            query += " AND timestamp<=?"
            params.append(end_date)
        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_context(row, self._get_scores(row[0])) for row in rows]

    def similar(
        self,
        request_summary: str,
        model: str = None,
        limit: int = 5,
    ) -> list[RoutingContext]:
        """Find similar decisions based on request content similarity (keyword match)."""
        keywords = request_summary.lower().split()[:5]
        if not keywords:
            return []
        conditions = " OR ".join(["request_summary LIKE ?" for _ in keywords])
        params = [f"%{kw}%" for kw in keywords]
        if model:
            query = f"SELECT * FROM decisions WHERE selected_model=? AND ({conditions}) ORDER BY timestamp DESC LIMIT ?"
            params = [model] + params + [limit]
        else:
            query = f"SELECT * FROM decisions WHERE {conditions} ORDER BY timestamp DESC LIMIT ?"
            params = params + [limit]
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_context(row, self._get_scores(row[0])) for row in rows]

    def model_stats(self, model_name: str = None) -> dict:
        """Get routing statistics for a model or all models."""
        where = f"WHERE selected_model='{model_name}'" if model_name else ""
        rows = self._conn.execute(f"""
            SELECT
                selected_model,
                COUNT(*) as call_count,
                AVG(total_latency_ms) as avg_latency,
                AVG(total_cost_usd) as avg_cost,
                MAX(total_latency_ms) as max_latency,
                SUM(CASE WHEN attempt_count > 1 THEN 1 ELSE 0 END) as retry_count
            FROM decisions
            {where}
            GROUP BY selected_model
            ORDER BY call_count DESC
        """).fetchall()
        return {
            "model": model_name or "all",
            "stats": [
                {
                    "model": r[0],
                    "call_count": r[1],
                    "avg_latency_ms": round(r[2] or 0, 1),
                    "avg_cost_usd": round(r[3] or 0, 6),
                    "max_latency_ms": r[4] or 0,
                    "retry_count": r[5] or 0,
                }
                for r in rows
            ]
        }
