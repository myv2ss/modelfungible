# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

import os
import tempfile
import time
import pytest
from unittest.mock import patch, MagicMock
from modelfungible.enterprise.budget_alerts import BudgetAlertStore, BudgetAlert, AlertEvent


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "alerts.db")
        yield BudgetAlertStore(path)


class TestAlertCRUD:
    def test_create_alert(self, store):
        a = store.create_alert(
            org_id="team-abc",
            webhook_url="https://example.com/webhook",
            threshold_pct=80.0,
            alert_type="daily",
            daily_limit=100.0,
        )
        assert a.alert_id is not None
        assert a.org_id == "team-abc"
        assert a.threshold_pct == 80.0
        assert a.enabled is True
        assert a.webhook_url == "https://example.com/webhook"

    def test_get_alert(self, store):
        a = store.create_alert("team-1", "https://hook.com/h", threshold_pct=50)
        a2 = store.get_alert(a.alert_id)
        assert a2 is not None
        assert a2.threshold_pct == 50

    def test_get_nonexistent(self, store):
        assert store.get_alert("nonexistent") is None

    def test_list_alerts(self, store):
        store.create_alert("team-1", "http://h1.com", daily_limit=10)
        store.create_alert("team-2", "http://h2.com", daily_limit=20)
        alerts = store.list_alerts()
        assert len(alerts) == 2

    def test_list_alerts_filtered(self, store):
        store.create_alert("team-1", "http://h1.com", daily_limit=10)
        store.create_alert("team-2", "http://h2.com", daily_limit=20)
        alerts = store.list_alerts(org_id="team-1")
        assert len(alerts) == 1
        assert alerts[0].org_id == "team-1"

    def test_update_alert(self, store):
        a = store.create_alert("team-1", "http://old.com", threshold_pct=50, daily_limit=10)
        store.update_alert(a.alert_id, threshold_pct=75, daily_limit=20)
        a2 = store.get_alert(a.alert_id)
        assert a2.threshold_pct == 75
        assert a2.daily_limit == 20

    def test_delete_alert(self, store):
        a = store.create_alert("team-1", "http://h.com")
        assert store.delete_alert(a.alert_id) is True
        assert store.delete_alert("nonexistent") is False

    def test_secret_is_stored(self, store):
        a = store.create_alert("team-1", "http://h.com", secret="my-secret-123")
        a2 = store.get_alert(a.alert_id)
        assert a2.secret == "my-secret-123"


class TestCheckAndFire:
    def test_no_alert_no_spend(self, store):
        fired = store.check_and_fire("team-1", 0, 0, 0, 0)
        assert fired == []

    def test_warning_threshold_hit(self, store):
        """80% of daily $100 = $80 spent → should fire at 80% threshold."""
        a = store.create_alert(
            "team-1", "http://hook.com/h",
            threshold_pct=80.0, alert_type="daily", daily_limit=100.0,
        )
        # Patch _send_webhook to avoid network
        with patch.object(store, "_send_webhook") as mock_fire:
            fired = store.check_and_fire("team-1", 80.0, 0, 100.0, 0)
            assert len(fired) == 1
            assert fired[0].alert_type == "daily_budget_warning"
            assert fired[0].pct_used == 80.0

    def test_exceeded_threshold(self, store):
        """$110 spent on $100 limit → exceeded type."""
        a = store.create_alert(
            "team-1", "http://hook.com/h",
            threshold_pct=80.0, alert_type="daily", daily_limit=100.0,
        )
        with patch.object(store, "_send_webhook") as mock_fire:
            fired = store.check_and_fire("team-1", 110.0, 0, 100.0, 0)
            assert len(fired) == 1
            assert fired[0].alert_type == "daily_budget_exceeded"

    def test_under_threshold_no_fire(self, store):
        a = store.create_alert(
            "team-1", "http://hook.com/h",
            threshold_pct=80.0, alert_type="daily", daily_limit=100.0,
        )
        with patch.object(store, "_send_webhook"):
            fired = store.check_and_fire("team-1", 50.0, 0, 100.0, 0)
            assert fired == []

    def test_monthly_alert_type(self, store):
        a = store.create_alert(
            "team-1", "http://hook.com/h",
            threshold_pct=80.0, alert_type="monthly", monthly_limit=1000.0,
        )
        with patch.object(store, "_send_webhook"):
            fired = store.check_and_fire("team-1", 0, 810.0, 0, 1000.0)
            assert len(fired) == 1
            assert fired[0].alert_type == "monthly_budget_warning"

    def test_disabled_alert_no_fire(self, store):
        a = store.create_alert(
            "team-1", "http://hook.com/h",
            threshold_pct=50.0, daily_limit=100.0,
        )
        store.update_alert(a.alert_id, enabled=False)
        with patch.object(store, "_send_webhook"):
            fired = store.check_and_fire("team-1", 90.0, 0, 100.0, 0)
            assert fired == []

    def test_hmac_signature(self, store):
        a = store.create_alert(
            "team-1", "http://hook.com/h",
            secret="shared-secret",
            threshold_pct=50.0, alert_type="daily", daily_limit=100.0,
        )
        with patch.object(store, "_send_webhook"):
            fired = store.check_and_fire("team-1", 60.0, 0, 100.0, 0)
            assert len(fired) == 1
            assert fired[0].signature != ""

    def test_cooldown_no_refire(self, store):
        """Within 1 hour, same alert should not refire."""
        a = store.create_alert(
            "team-1", "http://hook.com/h",
            threshold_pct=50.0, alert_type="daily", daily_limit=100.0,
        )
        with patch.object(store, "_send_webhook") as mock_fire:
            fired1 = store.check_and_fire("team-1", 60.0, 0, 100.0, 0)
            assert len(fired1) == 1
            fired2 = store.check_and_fire("team-1", 65.0, 0, 100.0, 0)
            assert len(fired2) == 0  # still in cooldown
            mock_fire.assert_called_once()


class TestAlertHistory:
    def test_events_logged(self, store):
        import threading
        a = store.create_alert(
            "team-1", "http://hook.com/h",
            threshold_pct=50.0, daily_limit=100.0,
        )
        # Patch Thread so the fire-and-forget actually completes before we query
        orig_start = threading.Thread.start
        def patched_start(self):
            orig_start(self)
            self.join()
        with patch.object(threading.Thread, "start", patched_start):
            fired = store.check_and_fire("team-1", 60.0, 0, 100.0, 0)
        assert len(fired) == 1
        events = store.get_events(org_id="team-1")
        assert len(events) >= 1
        assert events[0]["alert_type"] == "daily_budget_warning"

    def test_get_alert_stats(self, store):
        import threading
        a = store.create_alert("team-1", "http://hook.com/h", daily_limit=100)
        orig_start = threading.Thread.start
        def patched_start(self):
            orig_start(self)
            self.join()
        # 85 > 80 threshold → fires
        with patch.object(threading.Thread, "start", patched_start):
            fired = store.check_and_fire("team-1", 85.0, 0, 100.0, 0)
        assert len(fired) == 1
        stats = store.get_alert_stats(a.alert_id)
        assert stats["total_fired"] >= 1
