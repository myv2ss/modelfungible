# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Feature 8: Per-team API keys — team-scoped access, quotas, and rate limiting.
Powers multi-tenant SaaS with per-team API key auth and usage quotas.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    import sqlite3
except ImportError:
    sqlite3 = None


def _aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Team:
    team_id: str
    name: str
    quota_daily: float          # USD / day; 0 = unlimited
    quota_monthly: float        # USD / month; 0 = unlimited
    rate_limit: int             # req/min; 0 = unlimited
    is_active: bool = True
    created_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)
        if self.metadata is None:
            self.metadata = {}

    @property
    def team_id_short(self) -> str:
        return self.team_id[:8]


@dataclass
class APIKey:
    key_id: str                 # public identifier (shown once on creation)
    team_id: str
    key_hash: str               # stored hash; key itself never stored
    name: str                   # human label: "prod-key", "dev-key", etc.
    scopes: list = field(default_factory=list)  # execute, admin, read
    is_active: bool = True
    created_at: Optional[datetime] = None
    last_used: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)
        if self.scopes is None:
            self.scopes = ["execute"]


@dataclass
class QuotaStatus:
    team_id: str
    spent_today: float
    spent_month: float
    daily_limit: float
    monthly_limit: float
    daily_pct: float
    monthly_pct: float
    is_exceeded: bool
    exceeded_scope: Optional[str] = None   # "daily" | "monthly" | None


@dataclass
class RateLimitStatus:
    team_id: str
    requests_this_minute: int
    limit: int
    is_limited: bool
    retry_after_secs: int = 0


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class APIKeyStore:
    """
    SQLite-backed API key store with team management, quotas, and rate limiting.
    """

    def __init__(self, db_path: str = ".modelfungible/api_keys.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS teams (
                    team_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    quota_daily REAL NOT NULL DEFAULT 0,
                    quota_monthly REAL NOT NULL DEFAULT 0,
                    rate_limit INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    name TEXT NOT NULL,
                    scopes TEXT NOT NULL DEFAULT '["execute"]',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_used TEXT,
                    expires_at TEXT,
                    FOREIGN KEY (team_id) REFERENCES teams(team_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    team_id TEXT NOT NULL,
                    cost_usd REAL NOT NULL,
                    recorded_at TEXT NOT NULL,
                    period_date TEXT NOT NULL   -- YYYY-MM-DD for daily, YYYY-MM for monthly
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_usage_team_date
                    ON usage_log(team_id, period_date)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rate_log (
                    team_id TEXT PRIMARY KEY,
                    window_start_unix INTEGER NOT NULL,
                    request_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.commit()

    # ── Teams ────────────────────────────────────────────────────────────────

    def create_team(self, name: str, quota_daily: float = 0,
                    quota_monthly: float = 0, rate_limit: int = 0,
                    metadata: Optional[dict] = None) -> Team:
        team = Team(
            team_id=uuid.uuid4().hex[:16],
            name=name,
            quota_daily=quota_daily,
            quota_monthly=quota_monthly,
            rate_limit=rate_limit,
            metadata=metadata or {},
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO teams (team_id, name, quota_daily, quota_monthly,
                                     rate_limit, is_active, created_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (team.team_id, team.name, team.quota_daily, team.quota_monthly,
                 team.rate_limit, 1, team.created_at.isoformat(),
                 _json_d(team.metadata))
            )
            conn.commit()
        return team

    def get_team(self, team_id: str) -> Optional[Team]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM teams WHERE team_id = ?", (team_id,)
            ).fetchone()
        if not row:
            return None
        return self._team_from_row(row)

    def list_teams(self) -> list[Team]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM teams ORDER BY created_at DESC").fetchall()
        return [self._team_from_row(r) for r in rows]

    def update_team(self, team_id: str, **kwargs) -> Optional[Team]:
        allowed = {"name", "quota_daily", "quota_monthly", "rate_limit", "is_active"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get_team(team_id)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        if "is_active" in updates:
            updates["is_active"] = int(updates["is_active"])
        vals = list(updates.values()) + [team_id]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"UPDATE teams SET {set_clause} WHERE team_id = ?", vals)
            conn.commit()
        return self.get_team(team_id)

    def _team_from_row(self, row) -> Team:
        cols = ["team_id", "name", "quota_daily", "quota_monthly",
                "rate_limit", "is_active", "created_at", "metadata"]
        d = dict(zip(cols, row))
        return Team(
            team_id=d["team_id"],
            name=d["name"],
            quota_daily=d["quota_daily"],
            quota_monthly=d["quota_monthly"],
            rate_limit=d["rate_limit"],
            is_active=bool(d["is_active"]),
            created_at=_aware(datetime.fromisoformat(d["created_at"])),
            metadata=_json_dict(d["metadata"]),
        )

    # ── API Keys ────────────────────────────────────────────────────────────

    def create_key(self, team_id: str, name: str,
                   scopes: Optional[list[str]] = None,
                   expires_at: Optional[datetime] = None) -> tuple[APIKey, str]:
        """Returns (APIKey object, plaintext_key). Plaintext key is shown ONLY once."""
        plaintext = f"mfkey_{secrets.token_urlsafe(32)}"
        key_id = secrets.token_urlsafe(8)
        key_hash = _hash_key(plaintext)
        scopes = scopes or ["execute"]
        ak = APIKey(
            key_id=key_id,
            team_id=team_id,
            key_hash=key_hash,
            name=name,
            scopes=scopes,
            expires_at=expires_at,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO api_keys
                   (key_id, team_id, key_hash, name, scopes, is_active, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ak.key_id, ak.team_id, ak.key_hash, ak.name,
                 _json_d(ak.scopes), 1, ak.created_at.isoformat(),
                 ak.expires_at.isoformat() if ak.expires_at else None)
            )
            conn.commit()
        return ak, plaintext

    def validate_key(self, plaintext_key: str) -> Optional[APIKey]:
        """
        Validates a plaintext API key. Returns APIKey if valid, None if not.
        Checks: key exists, is active, not expired, team is active.
        Updates last_used timestamp.
        """
        if not plaintext_key or not plaintext_key.startswith("mfkey_"):
            return None
        key_hash = _hash_key(plaintext_key)
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT k.*, t.is_active as team_active, t.quota_daily,
                          t.quota_monthly, t.rate_limit
                   FROM api_keys k JOIN teams t ON k.team_id = t.team_id
                   WHERE k.key_hash = ?""",
                (key_hash,)
            ).fetchone()
        if not row:
            return None

        cols = ["key_id", "team_id", "key_hash", "name", "scopes",
                "is_active", "created_at", "last_used", "expires_at",
                "team_active", "quota_daily", "quota_monthly", "rate_limit"]
        d = dict(zip(cols, row))

        if not d["is_active"] or not d["team_active"]:
            return None
        expires = d["expires_at"]
        if expires and _aware(datetime.fromisoformat(expires)) < _utc_now():
            return None

        # Update last_used
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE api_keys SET last_used = ? WHERE key_id = ?",
                (_utc_now().isoformat(), d["key_id"])
            )
            conn.commit()

        return APIKey(
            key_id=d["key_id"], team_id=d["team_id"], key_hash=d["key_hash"],
            name=d["name"],
            scopes=_json_list(d["scopes"]),
            is_active=bool(d["is_active"]),
            created_at=_aware(datetime.fromisoformat(d["created_at"])),
            last_used=_aware(datetime.fromisoformat(d["last_used"])) if d["last_used"] else None,
            expires_at=_aware(datetime.fromisoformat(expires)) if expires else None,
        )

    def revoke_key(self, key_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE api_keys SET is_active = 0 WHERE key_id = ?",
                (key_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def list_keys(self, team_id: Optional[str] = None) -> list[APIKey]:
        with sqlite3.connect(self.db_path) as conn:
            if team_id:
                rows = conn.execute(
                    "SELECT * FROM api_keys WHERE team_id = ? ORDER BY created_at DESC",
                    (team_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM api_keys ORDER BY created_at DESC"
                ).fetchall()
        return [self._key_from_row(r) for r in rows]

    def _key_from_row(self, row) -> APIKey:
        cols = ["key_id", "team_id", "key_hash", "name", "scopes",
                "is_active", "created_at", "last_used", "expires_at"]
        d = dict(zip(cols, row))
        return APIKey(
            key_id=d["key_id"], team_id=d["team_id"], key_hash=d["key_hash"],
            name=d["name"],
            scopes=_json_list(d["scopes"]),
            is_active=bool(d["is_active"]),
            created_at=_aware(datetime.fromisoformat(d["created_at"])),
            last_used=_aware(datetime.fromisoformat(d["last_used"])) if d["last_used"] else None,
            expires_at=_aware(datetime.fromisoformat(d["expires_at"])) if d["expires_at"] else None,
        )

    # ── Usage tracking ───────────────────────────────────────────────────────

    def record_usage(self, team_id: str, cost_usd: float):
        """Record a cost (USD) for a team."""
        now = _utc_now()
        period_date = now.strftime("%Y-%m-%d")          # daily
        month_date = now.strftime("%Y-%m")              # monthly
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO usage_log (team_id, cost_usd, recorded_at, period_date) VALUES (?, ?, ?, ?)",
                (team_id, cost_usd, now.isoformat(), period_date)
            )
            conn.commit()

    def get_quota_status(self, team_id: str) -> QuotaStatus:
        """Returns current spending vs quotas for a team."""
        team = self.get_team(team_id)
        if not team:
            return QuotaStatus(team_id=team_id, spent_today=0, spent_month=0,
                               daily_limit=0, monthly_limit=0,
                               daily_pct=0, monthly_pct=0, is_exceeded=True)

        now = _utc_now()
        today = now.strftime("%Y-%m-%d")
        month = now.strftime("%Y-%m")

        with sqlite3.connect(self.db_path) as conn:
            row_d = conn.execute(
                """SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log
                   WHERE team_id = ? AND period_date = ?""",
                (team_id, today)
            ).fetchone()

            row_m = conn.execute(
                """SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log
                   WHERE team_id = ? AND period_date LIKE ? || '%'""",
                (team_id, month[:7])
            ).fetchone()

        spent_today = row_d[0] or 0.0
        spent_month = row_m[0] or 0.0
        daily_pct = (spent_today / team.quota_daily * 100) if team.quota_daily > 0 else 0
        monthly_pct = (spent_month / team.quota_monthly * 100) if team.quota_monthly > 0 else 0

        exceeded = False
        scope = None
        if team.quota_daily > 0 and spent_today >= team.quota_daily:
            exceeded = True
            scope = "daily"
        elif team.quota_monthly > 0 and spent_month >= team.quota_monthly:
            exceeded = True
            scope = "monthly"

        return QuotaStatus(
            team_id=team_id, spent_today=spent_today, spent_month=spent_month,
            daily_limit=team.quota_daily, monthly_limit=team.quota_monthly,
            daily_pct=daily_pct, monthly_pct=monthly_pct,
            is_exceeded=exceeded, exceeded_scope=scope,
        )

    def check_rate_limit(self, team_id: str) -> RateLimitStatus:
        """Check and update rate limit counter. Returns status."""
        team = self.get_team(team_id)
        if not team or team.rate_limit <= 0:
            return RateLimitStatus(team_id=team_id, requests_this_minute=0,
                                   limit=0, is_limited=False)

        now_unix = int(time.time())
        window = now_unix // 60  # 1-minute windows

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT window_start_unix, request_count FROM rate_log WHERE team_id = ?",
                (team_id,)
            ).fetchone()

        if row and row[0] == window:
            count = row[1] + 1
        else:
            count = 1

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO rate_log
                   (team_id, window_start_unix, request_count)
                   VALUES (?, ?, ?)""",
                (team_id, window, count)
            )
            conn.commit()

        is_limited = count > team.rate_limit
        retry_after = max(0, 60 - (now_unix % 60)) if is_limited else 0

        return RateLimitStatus(
            team_id=team_id, requests_this_minute=count,
            limit=team.rate_limit, is_limited=is_limited,
            retry_after_secs=retry_after,
        )


def _json_d(d: dict) -> str:
    import json
    return json.dumps(d)


def _json_dict(s: str) -> dict:
    import json
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _json_list(s: str) -> list:
    import json
    try:
        v = json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []
