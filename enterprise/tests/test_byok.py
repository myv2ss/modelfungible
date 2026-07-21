# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
"""Tests for BYOK — Bring Your Own Key."""
import sys
sys.path.insert(0, '.')

import os
import tempfile
import pytest
from enterprise.byok import (
    BYOKStore, BYOKKey, BYOKStats, Provider,
    _encrypt, _decrypt,
)
import sqlite3


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    s = BYOKStore(path)
    yield s
    os.unlink(path)


class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        key = "sk-test-abc123"
        enc = _encrypt(key)
        assert enc != key
        assert _decrypt(enc) == key

    def test_different_plaintexts_different_ciphertexts(self):
        e1 = _encrypt("key1")
        e2 = _encrypt("key2")
        assert e1 != e2


class TestBYOKLifecycle:
    def test_register_key_returns_byok_and_vkey(self, store):
        byok, vkey = store.register_key(
            team_id="team_abc",
            provider="openai",
            upstream_key="sk-abcdef1234567890",
            name="Test Key",
        )
        assert byok.key_id.startswith("ritabk_")
        assert vkey.startswith("ritabk_")
        assert byok.team_id == "team_abc"
        assert byok.provider == "openai"
        assert byok.is_active is True

    def test_vkey_equals_key_id(self, store):
        """Virtual key IS the key_id — shown once, used for all lookups."""
        byok, vkey = store.register_key(
            team_id="t1", provider="openai",
            upstream_key="sk-abc", name="k1",
        )
        assert byok.key_id == vkey
        assert vkey.startswith("ritabk_")

    def test_get_upstream_key_returns_real_key(self, store):
        byok, vkey = store.register_key(
            team_id="team_abc",
            provider="openai",
            upstream_key="sk-mysecretkey123",
            name="Real Key",
        )
        result = store.get_upstream_key(vkey)
        assert result is not None
        provider, upstream = result
        assert provider == "openai"
        assert upstream == "sk-mysecretkey123"

    def test_get_upstream_key_unknown_vkey_returns_none(self, store):
        assert store.get_upstream_key("ritabk_unknown") is None

    def test_get_upstream_key_revoked_returns_none(self, store):
        byok, vkey = store.register_key(
            team_id="t1", provider="openai",
            upstream_key="sk-test", name="k1",
        )
        store.revoke_key(byok.key_id)
        assert store.get_upstream_key(vkey) is None

    def test_revoke_key(self, store):
        byok, vkey = store.register_key(
            team_id="t1", provider="anthropic",
            upstream_key="sk-ant-test123", name="k1",
        )
        assert store.revoke_key(byok.key_id) is True
        assert store.get_key(byok.key_id).is_active is False

    def test_reactivate_key(self, store):
        byok, vkey = store.register_key(
            team_id="t1", provider="groq",
            upstream_key="gsk_test", name="k1",
        )
        store.revoke_key(byok.key_id)
        assert store.reactivate_key(byok.key_id) is True
        assert store.get_key(byok.key_id).is_active is True

    def test_list_keys_filters_by_team(self, store):
        store.register_key(team_id="team_a", provider="openai",
                           upstream_key="sk-a", name="Key A")
        store.register_key(team_id="team_b", provider="openai",
                           upstream_key="sk-b", name="Key B")
        store.register_key(team_id="team_a", provider="anthropic",
                           upstream_key="sk-ant-a", name="Key A2")
        keys_a = store.list_keys(team_id="team_a")
        assert len(keys_a) == 2
        assert all(k.team_id == "team_a" for k in keys_a)

    def test_list_keys_excludes_inactive_by_default(self, store):
        byok, _ = store.register_key(team_id="t1", provider="openai",
                                      upstream_key="sk-a", name="k1")
        store.revoke_key(byok.key_id)
        keys = store.list_keys(team_id="t1")
        assert len(keys) == 0
        keys_inc = store.list_keys(team_id="t1", include_inactive=True)
        assert len(keys_inc) == 1

    def test_upstream_key_id_extraction(self, store):
        byok, _ = store.register_key(
            team_id="t1", provider="openai",
            upstream_key="sk-1234567890abcdefghij", name="k1",
        )
        assert byok.upstream_key_id.startswith("sk-1234567890")
        assert "..." in byok.upstream_key_id

    def test_real_key_never_stored_plaintext(self, store):
        byok, vkey = store.register_key(
            team_id="t1", provider="openai",
            upstream_key="sk-super-secret-key-xyz", name="k1",
        )
        import sqlite3
        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT upstream_key FROM byok_keys WHERE key_id = ?", (vkey,)
            ).fetchone()
        encrypted = row[0]
        # Encrypted form is not the plaintext
        assert encrypted != "sk-super-secret-key-xyz"
        # But it can be decrypted back to the original

        assert _decrypt(encrypted) == "sk-super-secret-key-xyz"


class TestBYOKUsageTracking:
    def test_record_and_get_usage(self, store):
        byok, vkey = store.register_key(
            team_id="t1", provider="openai",
            upstream_key="sk-test", name="k1",
        )
        store.record_usage(
            byok_key_id=byok.key_id, team_id="t1",
            provider="openai", model="gpt-4o",
            cost_usd=0.02, tokens_used=500, latency_ms=300,
        )
        records = store.get_usage(byok_key_id=byok.key_id)
        assert len(records) == 1
        assert records[0].cost_usd == 0.02
        assert records[0].model == "gpt-4o"

    def test_record_error_increments_count(self, store):
        byok, _ = store.register_key(
            team_id="t1", provider="openai",
            upstream_key="sk-test", name="k1",
        )
        store.record_usage(
            byok_key_id=byok.key_id, team_id="t1",
            provider="openai", model="gpt-4o",
            cost_usd=0, error="Rate limit exceeded",
        )
        key = store.get_key(byok.key_id)
        assert key.error_count == 1
        assert "Rate limit" in key.last_error

    def test_get_stats(self, store):
        byok1, _ = store.register_key(
            team_id="t1", provider="openai",
            upstream_key="sk-a", name="k1",
        )
        byok2, _ = store.register_key(
            team_id="t2", provider="anthropic",
            upstream_key="sk-ant-b", name="k2",
        )
        store.record_usage(
            byok_key_id=byok1.key_id, team_id="t1",
            provider="openai", model="gpt-4o", cost_usd=0.05,
        )
        store.record_usage(
            byok_key_id=byok2.key_id, team_id="t2",
            provider="anthropic", model="claude-3-5-sonnet", cost_usd=0.10,
        )
        store.revoke_key(byok1.key_id)

        stats = store.get_stats()
        assert stats.total_keys == 2
        assert stats.active_keys == 1
        assert stats.revoked_keys == 1
        assert stats.teams_with_keys == 2
        assert stats.total_calls == 2
        assert abs(stats.total_cost_usd - 0.15) < 1e-9


class TestBYOKIsolation:
    def test_one_team_revoked_doesnt_affect_other(self, store):
        byok1, vkey1 = store.register_key(
            team_id="team_malicious", provider="openai",
            upstream_key="sk-malicious", name="bad",
        )
        byok2, vkey2 = store.register_key(
            team_id="team_good", provider="openai",
            upstream_key="sk-good", name="good",
        )
        store.revoke_key(byok1.key_id, reason="ToS violation")
        # Good team's key still works
        result = store.get_upstream_key(vkey2)
        assert result is not None
        # Bad team's key is blocked
        assert store.get_upstream_key(vkey1) is None

    def test_different_teams_isolated(self, store):
        _, vkey1 = store.register_key(team_id="t1", provider="openai",
                                      upstream_key="sk-t1", name="t1")
        _, vkey2 = store.register_key(team_id="t2", provider="openai",
                                      upstream_key="sk-t2", name="t2")
        # Each key returns its own team's upstream key
        _, key1 = store.get_upstream_key(vkey1)
        _, key2 = store.get_upstream_key(vkey2)
        assert key1 == "sk-t1"
        assert key2 == "sk-t2"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
