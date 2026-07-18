# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

import os
import tempfile
import time
import pytest
from datetime import datetime, timezone, timedelta

from modelfungible.enterprise.api_keys import (
    APIKeyStore, Team, APIKey, QuotaStatus, RateLimitStatus,
)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "api_keys.db")
        yield APIKeyStore(path)


class TestTeams:
    def test_create_and_get_team(self, store):
        t = store.create_team("Acme Corp", quota_daily=100, quota_monthly=2000, rate_limit=60)
        assert t.name == "Acme Corp"
        assert t.quota_daily == 100
        assert t.rate_limit == 60
        assert t.is_active is True

        t2 = store.get_team(t.team_id)
        assert t2 is not None
        assert t2.name == "Acme Corp"

    def test_get_nonexistent_team(self, store):
        assert store.get_team("nonexistent") is None

    def test_list_teams(self, store):
        store.create_team("Team A")
        store.create_team("Team B")
        teams = store.list_teams()
        assert len(teams) == 2

    def test_update_team(self, store):
        t = store.create_team("Old Name", quota_daily=50)
        store.update_team(t.team_id, name="New Name", quota_daily=75)
        t2 = store.get_team(t.team_id)
        assert t2.name == "New Name"
        assert t2.quota_daily == 75

    def test_deactivate_team(self, store):
        t = store.create_team("Active Team")
        store.update_team(t.team_id, is_active=False)
        assert store.get_team(t.team_id).is_active is False


class TestAPIKeys:
    def test_create_key(self, store):
        t = store.create_team("Test Team")
        ak, plaintext = store.create_key(t.team_id, "prod-key")
        assert ak.name == "prod-key"
        assert ak.team_id == t.team_id
        assert plaintext.startswith("mfkey_")

    def test_validate_key_success(self, store):
        t = store.create_team("Test Team")
        ak, plaintext = store.create_key(t.team_id, "dev-key", scopes=["execute", "read"])
        validated = store.validate_key(plaintext)
        assert validated is not None
        assert validated.key_id == ak.key_id
        assert validated.scopes == ["execute", "read"]

    def test_validate_key_wrong_key(self, store):
        t = store.create_team("Test Team")
        store.create_key(t.team_id, "key1")
        result = store.validate_key("mfkey_wrongkeyhere00000000000000000")
        assert result is None

    def test_validate_key_inactive(self, store):
        t = store.create_team("Test Team")
        ak, plaintext = store.create_key(t.team_id, "key1")
        store.revoke_key(ak.key_id)
        assert store.validate_key(plaintext) is None

    def test_validate_key_expired(self, store):
        t = store.create_team("Test Team")
        expired = datetime.now(timezone.utc) - timedelta(hours=1)
        ak, plaintext = store.create_key(t.team_id, "key1", expires_at=expired)
        assert store.validate_key(plaintext) is None

    def test_validate_key_not_mfkey_prefix(self, store):
        assert store.validate_key("sk-wrong-format") is None

    def test_revoke_key(self, store):
        t = store.create_team("Test Team")
        ak, plaintext = store.create_key(t.team_id, "key1")
        assert store.revoke_key(ak.key_id) is True
        assert store.revoke_key("nonexistent") is False
        assert store.validate_key(plaintext) is None

    def test_list_keys(self, store):
        t = store.create_team("Test Team")
        store.create_key(t.team_id, "key1")
        store.create_key(t.team_id, "key2")
        keys = store.list_keys(team_id=t.team_id)
        assert len(keys) == 2
        # All keys for all teams
        all_keys = store.list_keys()
        assert len(all_keys) == 2


class TestQuotaTracking:
    def test_record_and_quota_status(self, store):
        t = store.create_team("Quota Team", quota_daily=10.0, quota_monthly=100.0)
        store.record_usage(t.team_id, 3.50)
        store.record_usage(t.team_id, 2.25)
        qs = store.get_quota_status(t.team_id)
        assert qs.spent_today == 5.75
        assert qs.daily_pct == pytest.approx(57.5, abs=0.01)
        assert qs.is_exceeded is False

    def test_quota_exceeded_daily(self, store):
        t = store.create_team("Small Team", quota_daily=5.0, quota_monthly=100.0)
        store.record_usage(t.team_id, 3.0)
        store.record_usage(t.team_id, 3.0)
        qs = store.get_quota_status(t.team_id)
        assert qs.is_exceeded is True
        assert qs.exceeded_scope == "daily"

    def test_quota_unlimited(self, store):
        t = store.create_team("Unlimited Team", quota_daily=0, quota_monthly=0)
        store.record_usage(t.team_id, 999999.0)
        qs = store.get_quota_status(t.team_id)
        assert qs.is_exceeded is False
        assert qs.daily_pct == 0
        assert qs.monthly_pct == 0


class TestRateLimiting:
    def test_rate_limit_first_request(self, store):
        t = store.create_team("Rate Team", rate_limit=10)
        rs = store.check_rate_limit(t.team_id)
        assert rs.is_limited is False
        assert rs.requests_this_minute == 1
        assert rs.limit == 10

    def test_rate_limit_under_threshold(self, store):
        t = store.create_team("Rate Team", rate_limit=10)
        for _ in range(5):
            store.check_rate_limit(t.team_id)
        rs = store.check_rate_limit(t.team_id)
        assert rs.is_limited is False

    def test_rate_limit_exceeded(self, store):
        t = store.create_team("Rate Team", rate_limit=3)
        for _ in range(3):
            store.check_rate_limit(t.team_id)
        rs = store.check_rate_limit(t.team_id)
        assert rs.is_limited is True
        assert rs.retry_after_secs > 0

    def test_rate_limit_unlimited(self, store):
        t = store.create_team("No Limit Team", rate_limit=0)
        rs = store.check_rate_limit(t.team_id)
        assert rs.is_limited is False
        assert rs.limit == 0
