# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
BYOK — Bring Your Own Key.

Allows teams/departments to register their own LLM provider API keys.
Rita maps each external key to a virtual BYOK key. If a team's key is
revoked by the provider or violates ToS, only that team's key is affected —
the proxy and all other teams remain operational.

Key concept: A "virtual key" is Rita's handle. The "upstream key" is the
real provider API key the team registered. Rita stores the upstream key
encrypted at rest and decrypts it only at request time.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

try:
    from cryptography.fernet import Fernet
    HAS_FERNET = True
except ImportError:
    HAS_FERNET = False


# ── Enums ────────────────────────────────────────────────────────────────────

class Provider(Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GROQ = "groq"
    OLLAMA = "ollama"          # localhost / self-hosted
    VERTEXAI = "vertexai"      # Google Cloud Vertex AI
    BEDROCK = "bedrock"         # AWS Bedrock
    AZURE = "azure"             # Azure OpenAI
    OTHER = "other"


# ── Dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class BYOKKey:
    """
    A BYOK virtual key — maps a Rita key_id to a team's real upstream provider key.

    The real provider key is stored encrypted. It is NEVER returned after creation.
    Only the key_id (shown once on registration) allows using the key.
    """
    key_id: str                 # public virtual identifier (ritabk_xxx)
    team_id: str
    provider: str               # openai | anthropic | groq | etc.
    name: str                   # human label: "Marketing OpenAI", "Dev Anthropic"
    upstream_key_id: str        # team's actual key identifier (sk-... or key_...)
                                 # stored as-is (not the secret, just the key ID for display)
    is_active: bool = True
    created_at: Optional[datetime] = None
    last_used: Optional[datetime] = None
    last_error: Optional[str] = None
    error_count: int = 0
    owner_email: Optional[str] = None    # who registered it (for compliance)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)
        if self.metadata is None:
            self.metadata = {}


@dataclass
class BYOKStats:
    total_keys: int
    active_keys: int
    revoked_keys: int
    teams_with_keys: int
    total_calls: int
    total_cost_usd: float
    errors_today: int


@dataclass
class UsageRecord:
    byok_key_id: str
    team_id: str
    provider: str
    model: str
    cost_usd: float
    tokens_used: int
    latency_ms: int
    error: Optional[str]
    timestamp: datetime


# ── Encryption helpers ────────────────────────────────────────────────────────

def _get_encryption_key() -> bytes:
    """
    Derive a Fernet key from MODELFUNGIBLE_BYOK_KEY env var.
    If not set, falls back to a per-install key stored in the DB (weaker but functional).
    """
    env_key = os.environ.get("MODELFUNGIBLE_BYOK_KEY", "")
    if env_key:
        # Use raw key if it's exactly 32 url-safe base64 bytes
        try:
            return env_key.encode("utf-8")
        except Exception:
            pass
    # Fallback: derive from a machine-specific secret
    machine_id = f"{os.environ.get('HOSTNAME', 'local')}-{os.getcwd()}"
    raw = hashlib.sha256(machine_id.encode()).digest()
    return base64.urlsafe_b64encode(raw)


def _encrypt(plaintext: str) -> str:
    if not HAS_FERNET:
        # XOR obfuscation (not real encryption — for dev only; set MODELFUNGIBLE_BYOK_KEY in prod)
        key = _get_encryption_key()
        klen = len(key)
        encrypted = bytes(a ^ b for a, b in zip(plaintext.encode(), (key * (len(plaintext) // klen + 1))[:len(plaintext)]))
        return base64.b64encode(encrypted).decode()
    f = Fernet(_get_encryption_key())
    return f.encrypt(plaintext.encode()).decode()


def _decrypt(ciphertext: str) -> str:
    if not HAS_FERNET:
        key = _get_encryption_key()
        klen = len(key)
        encrypted = base64.b64decode(ciphertext.encode())
        decrypted = bytes(a ^ b for a, b in zip(encrypted, (key * (len(encrypted) // klen + 1))[:len(encrypted)]))
        return decrypted.decode()
    f = Fernet(_get_encryption_key())
    return f.decrypt(ciphertext.encode()).decode()


# ── Main Store ───────────────────────────────────────────────────────────────

class BYOKStore:
    """
    SQLite-backed BYOK store. Manages team-owned provider API keys.

    Usage:
        store = BYOKStore(".modelfungible/byok.db")

        # Register a team's OpenAI key
        byok_key, plaintext_vkey = store.register_key(
            team_id="team_abc",
            provider="openai",
            upstream_key="sk-...",
            name="Marketing Dept",
            owner_email="marketing@acme.com",
        )
        # Give plaintext_vkey to the team — shown ONLY once

        # At request time — look up real key
        real_key = store.get_upstream_key("ritabk_xxx")
        # → returns the decrypted upstream API key

        # If provider revokes / team violates ToS — revoke just this key
        store.revoke_key("ritabk_xxx")
    """

    LATEST_SCHEMA = 1

    def __init__(self, db_path: str = ".modelfungible/byok.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS byok_keys (
                    key_id         TEXT PRIMARY KEY,   -- ritabk_xxx
                    team_id        TEXT NOT NULL,
                    provider       TEXT NOT NULL,
                    name           TEXT NOT NULL,
                    upstream_key   TEXT NOT NULL,       -- encrypted real API key
                    upstream_key_id TEXT NOT NULL,      -- key ID for display (sk-...-xxxx)
                    is_active      INTEGER NOT NULL DEFAULT 1,
                    created_at     TEXT NOT NULL,
                    last_used      TEXT,
                    last_error     TEXT,
                    error_count    INTEGER NOT NULL DEFAULT 0,
                    owner_email    TEXT,
                    metadata       TEXT NOT NULL DEFAULT '{}'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS byok_usage (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    byok_key_id    TEXT NOT NULL,
                    team_id        TEXT NOT NULL,
                    provider       TEXT NOT NULL,
                    model          TEXT NOT NULL,
                    cost_usd       REAL NOT NULL,
                    tokens_used    INTEGER NOT NULL DEFAULT 0,
                    latency_ms     INTEGER NOT NULL DEFAULT 0,
                    error          TEXT,
                    timestamp      TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_byok_team
                    ON byok_keys(team_id)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_byok_usage_key
                    ON byok_usage(byok_key_id, timestamp)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key    TEXT PRIMARY KEY,
                    value  TEXT NOT NULL
                )
            """)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', ?)",
                (str(self.LATEST_SCHEMA),)
            )
            conn.commit()

    # ── Key lifecycle ───────────────────────────────────────────────────────

    def register_key(
        self,
        team_id: str,
        provider: str,
        upstream_key: str,
        name: str,
        owner_email: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> tuple[BYOKKey, str]:
        """
        Register a team's real provider API key.

        Returns (BYOKKey object, plaintext_virtual_key).
        The plaintext virtual key is shown ONLY once — team uses it to make calls.

        The real upstream_key is encrypted at rest.
        """
        # Generate virtual key — this is what the team uses in Rita.
        # key_id IS the virtual key (shown once on registration).
        key_id = f"ritabk_{secrets.token_urlsafe(16)}"

        # Extract the key's public ID for display (not the secret)
        upstream_key_id = self._extract_key_id(provider, upstream_key)

        # Encrypt the real key before storing
        encrypted_upstream = _encrypt(upstream_key)

        byok = BYOKKey(
            key_id=key_id,
            team_id=team_id,
            provider=provider,
            name=name,
            upstream_key_id=upstream_key_id,
            owner_email=owner_email,
            metadata=metadata or {},
        )

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO byok_keys
                   (key_id, team_id, provider, name, upstream_key, upstream_key_id,
                    is_active, created_at, error_count, owner_email, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    byok.key_id, byok.team_id, byok.provider, byok.name,
                    encrypted_upstream, upstream_key_id,
                    1, byok.created_at.isoformat(),
                    0, byok.owner_email, json.dumps(byok.metadata),
                )
            )
            conn.commit()

        return byok, key_id  # key_id shown once = team's virtual key

    def get_upstream_key(self, virtual_key: str) -> Optional[tuple[str, str]]:
        """
        Look up the real upstream provider key for a virtual BYOK key.

        Returns (provider, upstream_key) if valid and active, else None.

        Also updates last_used timestamp and records the access.
        """
        if not virtual_key or not virtual_key.startswith("ritabk_"):
            return None

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT key_id, team_id, provider, upstream_key, is_active FROM byok_keys WHERE key_id = ?",
                (virtual_key,)
            ).fetchone()

        if not row:
            return None

        key_id, team_id, provider, encrypted_key, is_active = row

        if not is_active:
            return None

        try:
            upstream_key = _decrypt(encrypted_key)
        except Exception:
            # Key exists but couldn't be decrypted — key was registered with
            # different encryption. Mark as error.
            self._record_error(virtual_key, "Key decryption failed — encryption key may have rotated")
            return None

        # Update last_used
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE byok_keys SET last_used = ? WHERE key_id = ?",
                (datetime.now(timezone.utc).isoformat(), key_id)
            )
            conn.commit()

        return provider, upstream_key

    def revoke_key(self, key_id: str, reason: str = "") -> bool:
        """
        Revoke a BYOK key. Does NOT delete the record — marks it inactive.
        This preserves audit history.
        """
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE byok_keys SET is_active = 0 WHERE key_id = ?",
                (key_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def reactivate_key(self, key_id: str) -> bool:
        """Reactivate a previously revoked BYOK key."""
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "UPDATE byok_keys SET is_active = 1, last_error = NULL WHERE key_id = ?",
                (key_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def list_keys(self, team_id: Optional[str] = None,
                  include_inactive: bool = False) -> list[BYOKKey]:
        """List BYOK keys for a team (or all teams if team_id is None)."""
        with sqlite3.connect(self.db_path) as conn:
            if team_id:
                query = "SELECT * FROM byok_keys WHERE team_id = ?"
                params: tuple = (team_id,)
                if not include_inactive:
                    query += " AND is_active = 1"
                query += " ORDER BY created_at DESC"
                rows = conn.execute(query, params).fetchall()
            else:
                query = "SELECT * FROM byok_keys"
                if not include_inactive:
                    query += " WHERE is_active = 1"
                query += " ORDER BY created_at DESC"
                rows = conn.execute(query).fetchall()

        return [self._from_row(r) for r in rows]

    def get_key(self, key_id: str) -> Optional[BYOKKey]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM byok_keys WHERE key_id = ?", (key_id,)
            ).fetchone()
        if not row:
            return None
        return self._from_row(row)

    # ── Usage tracking ─────────────────────────────────────────────────────

    def record_usage(
        self,
        byok_key_id: str,
        team_id: str,
        provider: str,
        model: str,
        cost_usd: float,
        tokens_used: int = 0,
        latency_ms: int = 0,
        error: Optional[str] = None,
    ):
        """Record a BYOK API call for attribution and billing."""
        if error:
            self._record_error(byok_key_id, error)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO byok_usage
                   (byok_key_id, team_id, provider, model, cost_usd,
                    tokens_used, latency_ms, error, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    byok_key_id, team_id, provider, model,
                    cost_usd, tokens_used, latency_ms, error,
                    datetime.now(timezone.utc).isoformat(),
                )
            )
            conn.commit()

    def get_stats(self, team_id: Optional[str] = None) -> BYOKStats:
        """Get BYOK usage statistics."""
        with sqlite3.connect(self.db_path) as conn:
            if team_id:
                where = "WHERE team_id = ?"
                params: tuple = (team_id,)
            else:
                where = ""
                params = ()

            total = conn.execute(
                f"SELECT COUNT(*), SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) FROM byok_keys {where}",
                params
            ).fetchone()
            total_keys = total[0] or 0
            active_keys = total[1] or 0

            revoked = conn.execute(
                f"SELECT COUNT(*) FROM byok_keys {where.replace('WHERE', 'WHERE is_active=0 AND') if where else 'WHERE is_active=0'}",
                params
            ).fetchone()[0] or 0

            if team_id:
                teams_q = "SELECT COUNT(DISTINCT team_id) FROM byok_keys WHERE team_id = ?"
                teams_p = (team_id,)
            else:
                teams_q = "SELECT COUNT(DISTINCT team_id) FROM byok_keys"
                teams_p = ()
            teams = conn.execute(teams_q, teams_p).fetchone()[0] or 0

            usage = conn.execute(
                f"""SELECT COUNT(*), COALESCE(SUM(cost_usd), 0),
                            COALESCE(SUM(CASE WHEN date(timestamp) = date('now') AND error IS NOT NULL THEN 1 ELSE 0 END), 0)
                     FROM byok_usage {where}""",
                params
            ).fetchone()

            return BYOKStats(
                total_keys=total_keys,
                active_keys=active_keys,
                revoked_keys=revoked,
                teams_with_keys=teams,
                total_calls=usage[0] or 0,
                total_cost_usd=float(usage[1] or 0),
                errors_today=int(usage[2] or 0),
            )

    def get_usage(
        self,
        byok_key_id: Optional[str] = None,
        team_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[UsageRecord]:
        """Get recent usage records."""
        conditions = []
        params: list = []
        if byok_key_id:
            conditions.append("byok_key_id = ?")
            params.append(byok_key_id)
        if team_id:
            conditions.append("team_id = ?")
            params.append(team_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"""SELECT byok_key_id, team_id, provider, model, cost_usd,
                           tokens_used, latency_ms, error, timestamp
                    FROM byok_usage {where}
                    ORDER BY timestamp DESC LIMIT ?""",
                [*params, limit]
            ).fetchall()
        return [
            UsageRecord(
                byok_key_id=r[0], team_id=r[1], provider=r[2], model=r[3],
                cost_usd=float(r[4]), tokens_used=r[5], latency_ms=r[6],
                error=r[7], timestamp=datetime.fromisoformat(r[8]),
            )
            for r in rows
        ]

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _extract_key_id(self, provider: str, key: str) -> str:
        """Extract the public key identifier from a provider key for display."""
        if provider == "openai" and key.startswith("sk-"):
            return key[:18] + "..." if len(key) > 18 else key
        elif provider == "anthropic" and key.startswith("sk-ant-"):
            return key[:14] + "..." if len(key) > 14 else key
        elif provider == "groq":
            return key[:12] + "..." if len(key) > 12 else key
        elif provider == "azure":
            # Azure keys don't have a prefix
            return key[:12] + "..." if len(key) > 12 else key
        # Generic: hash it to a stable short ID
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _record_error(self, key_id: str, error: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE byok_keys
                   SET error_count = error_count + 1,
                       last_error = ?
                   WHERE key_id = ?""",
                (error[:500], key_id)
            )
            conn.commit()

    def _from_row(self, row) -> BYOKKey:
        cols = [
            "key_id", "team_id", "provider", "name", "upstream_key",
            "upstream_key_id", "is_active", "created_at", "last_used",
            "last_error", "error_count", "owner_email", "metadata",
        ]
        d = dict(zip(cols, row))
        from datetime import timezone as tz
        def aware(ts_str):
            if not ts_str:
                return None
            return datetime.fromisoformat(ts_str).replace(tzinfo=tz.utc)
        return BYOKKey(
            key_id=d["key_id"], team_id=d["team_id"], provider=d["provider"],
            name=d["name"], upstream_key_id=d["upstream_key_id"],
            is_active=bool(d["is_active"]),
            created_at=aware(d["created_at"]),
            last_used=aware(d["last_used"]),
            last_error=d["last_error"],
            error_count=d["error_count"] or 0,
            owner_email=d["owner_email"],
            metadata=json.loads(d["metadata"]) if d["metadata"] else {},
        )
