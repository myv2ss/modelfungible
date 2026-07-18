# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Prompt Marketplace — store, version, approve, share, and analyze prompts.
Built on top of the existing AuditLogger's SQLite storage.
"""
from __future__ import annotations

import json, os, sqlite3, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Literal
from dataclasses import dataclass, field, asdict


# ─── Data Model ────────────────────────────────────────────────────────────────

@dataclass
class PromptVersion:
    version_id: str
    prompt_id: str
    version_num: int
    name: str
    description: str
    prompt_text: str
    system_prompt: str
    variables: list[str]          # list of {{variable}} names
    use_cases: list[str]
    tags: list[str]
    created_by: str
    created_at: str
    is_active: bool = False


@dataclass
class Prompt:
    prompt_id: str
    name: str
    domain: str                  # legal, finance, healthcare, hr, coding, general
    description: str
    created_by: str
    created_at: str
    status: str = "draft"        # draft | published | archived
    current_version_id: str = ""
    versions: list[PromptVersion] = field(default_factory=list)
    like_count: int = 0
    call_count: int = 0
    avg_cost_per_call: float = 0.0
    avg_latency_ms: float = 0.0
    error_rate: float = 0.0


# ─── Storage ───────────────────────────────────────────────────────────────────

DB_PATH = os.environ.get("MODELFUNGIBLE_PROMPTS_DB",
                         str(Path(__file__).parent.parent / "data" / "prompt_marketplace.db"))


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.Connection(DB_PATH, autocommit=True)
    conn.execute("PRAGMA journal_mode=WAL")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS prompts (
        prompt_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        domain TEXT DEFAULT 'general',
        description TEXT DEFAULT '',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT DEFAULT 'draft',
        current_version_id TEXT DEFAULT '',
        like_count INTEGER DEFAULT 0,
        call_count INTEGER DEFAULT 0,
        avg_cost_per_call REAL DEFAULT 0.0,
        avg_latency_ms REAL DEFAULT 0.0,
        error_rate REAL DEFAULT 0.0
    );

    CREATE TABLE IF NOT EXISTS prompt_versions (
        version_id TEXT PRIMARY KEY,
        prompt_id TEXT NOT NULL,
        version_num INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        prompt_text TEXT NOT NULL,
        system_prompt TEXT DEFAULT '',
        variables TEXT DEFAULT '[]',
        use_cases TEXT DEFAULT '[]',
        tags TEXT DEFAULT '[]',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        is_active INTEGER DEFAULT 0,
        FOREIGN KEY (prompt_id) REFERENCES prompts(prompt_id)
    );

    CREATE TABLE IF NOT EXISTS prompt_ratings (
        rating_id TEXT PRIMARY KEY,
        prompt_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        rating INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(prompt_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS prompt_approvals (
        approval_id TEXT PRIMARY KEY,
        prompt_id TEXT NOT NULL,
        version_id TEXT NOT NULL,
        approved_by TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_prompts_domain ON prompts(domain);
    CREATE INDEX IF NOT EXISTS idx_prompts_status ON prompts(status);
    CREATE INDEX IF NOT EXISTS idx_versions_prompt ON prompt_versions(prompt_id);
    """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── CRUD ──────────────────────────────────────────────────────────────────────

class PromptStore:
    """Store and manage prompts."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.Connection(db_path, autocommit=True)
        self._conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(self._conn)

    def create_prompt(
        self,
        name: str,
        created_by: str,
        domain: str = "general",
        description: str = "",
    ) -> Prompt:
        pid = f"prompt_{uuid.uuid4().hex[:12]}"
        now = _now()
        self._conn.execute("""
            INSERT INTO prompts (prompt_id, name, domain, description, created_by, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'draft')
        """, [pid, name, domain, description, created_by, now])
        return Prompt(
            prompt_id=pid, name=name, domain=domain,
            description=description, created_by=created_by, created_at=now,
        )

    def add_version(
        self,
        prompt_id: str,
        version_num: int,
        name: str,
        prompt_text: str,
        created_by: str,
        description: str = "",
        system_prompt: str = "",
        use_cases: list[str] = None,
        tags: list[str] = None,
    ) -> PromptVersion:
        vid = f"v_{uuid.uuid4().hex[:12]}"
        now = _now()

        # Extract variables from prompt text: {{variable_name}}
        import re
        vars_ = re.findall(r'\{\{(\w+)\}\}', prompt_text + " " + (system_prompt or ""))

        self._conn.execute("""
            INSERT INTO prompt_versions
            (version_id, prompt_id, version_num, name, description, prompt_text, system_prompt,
             variables, use_cases, tags, created_by, created_at, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """, [
            vid, prompt_id, version_num, name, description, prompt_text, system_prompt,
            json.dumps(vars_),
            json.dumps(use_cases or []),
            json.dumps(tags or []),
            created_by, now,
        ])

        return PromptVersion(
            version_id=vid, prompt_id=prompt_id, version_num=version_num,
            name=name, description=description, prompt_text=prompt_text,
            system_prompt=system_prompt, variables=vars_,
            use_cases=use_cases or [], tags=tags or [],
            created_by=created_by, created_at=now,
        )

    def publish(self, prompt_id: str) -> None:
        now = _now()
        self._conn.execute("UPDATE prompts SET status='published', current_version_id=? WHERE prompt_id=?",
                          [now, prompt_id])

    def archive(self, prompt_id: str) -> None:
        self._conn.execute("UPDATE prompts SET status='archived' WHERE prompt_id=?", [prompt_id])

    def rate(self, prompt_id: str, user_id: str, rating: int) -> None:
        rid = f"rate_{uuid.uuid4().hex[:12]}"
        now = _now()
        self._conn.execute("""
            INSERT INTO prompt_ratings (rating_id, prompt_id, user_id, rating, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(prompt_id, user_id) DO UPDATE SET rating=excluded.rating
        """, [rid, prompt_id, user_id, rating, now])

    def record_call(self, prompt_id: str, cost_usd: float, latency_ms: int, success: bool) -> None:
        """Record that a prompt was used. Updates analytics."""
        self._conn.execute("""
            UPDATE prompts SET
                call_count = call_count + 1,
                avg_cost_per_call = CASE
                    WHEN call_count = 0 THEN ?
                    ELSE (avg_cost_per_call * call_count + ?) / (call_count + 1)
                END,
                avg_latency_ms = CASE
                    WHEN call_count = 0 THEN ?
                    ELSE (avg_latency_ms * call_count + ?) / (call_count + 1)
                END,
                error_rate = CASE
                    WHEN call_count = 0 THEN ?
                    ELSE (error_rate * call_count + ?) / (call_count + 1)
                END
            WHERE prompt_id = ?
        """, [
            cost_usd, cost_usd,
            latency_ms, latency_ms,
            0.0 if success else 1.0,
            0.0 if success else 1.0,
            prompt_id,
        ])

    def get(self, prompt_id: str) -> Optional[Prompt]:
        row = self._conn.execute(
            "SELECT * FROM prompts WHERE prompt_id=?", [prompt_id]
        ).fetchone()
        if not row:
            return None
        versions = self._get_versions(prompt_id)
        return self._row_to_prompt(row, versions)

    def list_prompts(
        self,
        domain: str = None,
        status: str = None,
        tags: list[str] = None,
        search: str = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Prompt]:
        query = "SELECT * FROM prompts WHERE 1=1"
        params = []
        if domain:
            query += " AND domain=?"
            params.append(domain)
        if status:
            query += " AND status=?"
            params.append(status)
        if search:
            query += " AND (name LIKE ? OR description LIKE ?)"
            params.extend([f"%{search}%", f"%{search}%"])
        query += " ORDER BY call_count DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = self._conn.execute(query, params).fetchall()
        prompts = []
        for row in rows:
            pid = row[0]
            versions = self._get_versions(pid)
            p = self._row_to_prompt(row, versions)
            if tags:
                if not any(t in p.versions[0].tags if p.versions else [] for t in tags):
                    continue
            prompts.append(p)
        return prompts

    def _get_versions(self, prompt_id: str) -> list[PromptVersion]:
        rows = self._conn.execute(
            "SELECT * FROM prompt_versions WHERE prompt_id=? ORDER BY version_num DESC",
            [prompt_id]
        ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def _row_to_prompt(self, row: sqlite3.Row, versions: list[PromptVersion]) -> Prompt:
        return Prompt(
            prompt_id=row[0], name=row[1], domain=row[2],
            description=row[3] or "", created_by=row[4], created_at=row[5],
            status=row[6], current_version_id=row[7] or "",
            like_count=row[8] or 0, call_count=row[9] or 0,
            avg_cost_per_call=row[10] or 0.0, avg_latency_ms=row[11] or 0.0,
            error_rate=row[12] or 0.0,
            versions=versions,
        )

    def _row_to_version(self, row: sqlite3.Row) -> PromptVersion:
        return PromptVersion(
            version_id=row[0], prompt_id=row[1], version_num=row[2],
            name=row[3], description=row[4] or "", prompt_text=row[5],
            system_prompt=row[6] or "",
            variables=json.loads(row[7] or "[]"),
            use_cases=json.loads(row[8] or "[]"),
            tags=json.loads(row[9] or "[]"),
            created_by=row[10], created_at=row[11],
            is_active=bool(row[12]),
        )

    def delete(self, prompt_id: str) -> bool:
        self._conn.execute("DELETE FROM prompt_versions WHERE prompt_id=?", [prompt_id])
        self._conn.execute("DELETE FROM prompts WHERE prompt_id=?", [prompt_id])
        return True
