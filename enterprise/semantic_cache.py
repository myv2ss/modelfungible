# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Semantic Cache — store and retrieve LLM responses using prompt hashing.
Skip model calls for identical/similar prompts and save cost + latency.
"""
from __future__ import annotations

import hashlib, json, os, sqlite3, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from dataclasses import dataclass


DB_PATH = os.environ.get("MODELFUNGIBLE_CACHE_DB",
                         str(Path(__file__).parent.parent / "data" / "semantic_cache.db"))


@dataclass
class CacheEntry:
    cache_key: str
    prompt_hash: str
    system_prompt: str
    model_name: str
    response: str
    latency_ms: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    created_at: str
    hit_count: int
    last_hit: str
    ttl_seconds: int
    metadata: dict


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_prompt(prompt: str, system: str = "", model: str = "") -> str:
    """Create a content-addressable hash of the prompt."""
    content = f"{model}:{system}:{prompt}".encode("utf-8")
    return hashlib.sha256(content).hexdigest()[:32]


def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.Connection(DB_PATH, autocommit=True)
    conn.execute("PRAGMA journal_mode=WAL")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS cache (
        cache_key TEXT PRIMARY KEY,
        prompt_hash TEXT NOT NULL,
        system_prompt TEXT DEFAULT '',
        model_name TEXT NOT NULL,
        response TEXT NOT NULL,
        latency_ms INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        hit_count INTEGER DEFAULT 0,
        last_hit TEXT DEFAULT '',
        ttl_seconds INTEGER DEFAULT 86400,
        metadata TEXT DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS idx_prompt_hash ON cache(prompt_hash);
    CREATE INDEX IF NOT EXISTS idx_model ON cache(model_name);
    CREATE INDEX IF NOT EXISTS idx_created ON cache(created_at);
    """)


class SemanticCache:
    """
    Hash-based prompt cache. Skips LLM calls for identical prompts.

    Usage:
        cache = SemanticCache()

        # Check cache before making LLM call
        hit = cache.get(prompt, system, model_name)
        if hit:
            print(f"Cache hit! Response: {hit.response}")
        else:
            # Call LLM...
            cache.store(prompt, system, model_name, response,
                       latency_ms=200, cost_usd=0.001)

        # Stats
        print(cache.stats())
    """

    def __init__(
        self,
        db_path: str = DB_PATH,
        default_ttl_seconds: int = 86400,  # 24 hours
        max_entries: int = 100000,
    ):
        self.db_path = db_path
        self.default_ttl = default_ttl_seconds
        self.max_entries = max_entries
        self._conn = sqlite3.Connection(db_path, autocommit=True)
        self._conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(self._conn)

    def get(
        self,
        prompt: str,
        system_prompt: str = "",
        model_name: str = "",
        exact_only: bool = False,
    ) -> Optional[CacheEntry]:
        """
        Look up a cached response.
        Returns CacheEntry if found and not expired, None otherwise.
        """
        h = _hash_prompt(prompt, system_prompt, model_name)
        now = _now()

        row = self._conn.execute("""
            SELECT * FROM cache WHERE prompt_hash=? AND model_name=?
        """, [h, model_name or "any"]).fetchone()

        if not row:
            return None

        entry = self._row_to_entry(row)

        # Check TTL
        created = datetime.fromisoformat(entry.created_at.replace("Z", "+00:00"))
        age_seconds = (datetime.now(timezone.utc) - created).total_seconds()
        if age_seconds > entry.ttl_seconds:
            # Expired — delete
            self._conn.execute("DELETE FROM cache WHERE cache_key=?", [entry.cache_key])
            return None

        # Update hit stats
        self._conn.execute("""
            UPDATE cache SET hit_count=hit_count+1, last_hit=? WHERE cache_key=?
        """, [_now(), entry.cache_key])

        return entry

    def store(
        self,
        prompt: str,
        system_prompt: str,
        model_name: str,
        response: str,
        latency_ms: int = 0,
        cost_usd: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        ttl_seconds: int = None,
        metadata: dict = None,
    ) -> CacheEntry:
        """
        Store a response in the cache.
        If an entry already exists for this prompt+model, it is updated.
        """
        h = _hash_prompt(prompt, system_prompt, model_name)
        key = f"cache_{h}"
        now = _now()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        meta = json.dumps(metadata or {})

        # Upsert
        self._conn.execute("""
            INSERT INTO cache (cache_key, prompt_hash, system_prompt, model_name, response,
                             latency_ms, cost_usd, input_tokens, output_tokens,
                             created_at, hit_count, last_hit, ttl_seconds, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                response=excluded.response,
                latency_ms=excluded.latency_ms,
                cost_usd=excluded.cost_usd,
                input_tokens=excluded.input_tokens,
                output_tokens=excluded.output_tokens,
                hit_count=cache.hit_count+1,
                last_hit=excluded.last_hit,
                ttl_seconds=excluded.ttl_seconds
        """, [
            key, h, system_prompt, model_name, response,
            latency_ms, cost_usd, input_tokens, output_tokens,
            now, now, ttl, meta,
        ])

        return CacheEntry(
            cache_key=key, prompt_hash=h, system_prompt=system_prompt,
            model_name=model_name, response=response,
            latency_ms=latency_ms, cost_usd=cost_usd,
            input_tokens=input_tokens, output_tokens=output_tokens,
            created_at=now, hit_count=1, last_hit=now,
            ttl_seconds=ttl, metadata=metadata or {},
        )

    def invalidate(self, prompt: str, system_prompt: str = "", model_name: str = "") -> bool:
        """Remove a specific entry from the cache."""
        h = _hash_prompt(prompt, system_prompt, model_name)
        cur = self._conn.execute(
            "SELECT cache_key FROM cache WHERE prompt_hash=?", [h]
        ).fetchone()
        if cur:
            self._conn.execute("DELETE FROM cache WHERE cache_key=?", [cur[0]])
            return True
        return False

    def clear(self, older_than_days: int = 0) -> int:
        """
        Clear expired entries. If older_than_days > 0, also clear entries
        older than that many days (regardless of TTL).
        """
        if older_than_days > 0:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM cache WHERE created_at < ?", [cutoff]
            ).fetchone()[0]
            self._conn.execute("DELETE FROM cache WHERE created_at < ?", [cutoff])
            return cur
        # Just expired TTL entries
        now = _now()
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM cache WHERE ttl_seconds > 0 AND datetime(created_at, '+' || ttl_seconds || ' seconds') < ?",
            [now]
        ).fetchone()[0]
        self._conn.execute(
            "DELETE FROM cache WHERE datetime(created_at, '+' || ttl_seconds || ' seconds') < ?",
            [now]
        )
        return cur

    def stats(self) -> dict:
        """Cache statistics."""
        total = self._conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0] or 0
        total_hits = self._conn.execute("SELECT SUM(hit_count) FROM cache").fetchone()[0] or 0
        total_cost_saved = self._conn.execute(
            "SELECT SUM(cost_usd * hit_count) FROM cache"
        ).fetchone()[0] or 0.0
        model_breakdown = self._conn.execute("""
            SELECT model_name, COUNT(*), SUM(hit_count), SUM(cost_usd * hit_count)
            FROM cache GROUP BY model_name ORDER BY SUM(hit_count) DESC
        """).fetchall()
        return {
            "total_entries": total,
            "total_hits": total_hits,
            "total_cost_saved_usd": round(total_cost_saved, 6),
            "hit_rate_percent": round(100 * total_hits / max(total_hits + total, 1), 1),
            "by_model": [
                {"model": r[0], "entries": r[1], "hits": r[2] or 0, "cost_saved": round(r[3] or 0, 6)}
                for r in model_breakdown
            ],
        }

    def _row_to_entry(self, row: sqlite3.Row) -> CacheEntry:
        return CacheEntry(
            cache_key=row[0], prompt_hash=row[1], system_prompt=row[2] or "",
            model_name=row[3], response=row[4],
            latency_ms=row[5], cost_usd=row[6],
            input_tokens=row[7], output_tokens=row[8],
            created_at=row[9], hit_count=row[10],
            last_hit=row[11] or "", ttl_seconds=row[12],
            metadata=json.loads(row[13] or "{}"),
        )
