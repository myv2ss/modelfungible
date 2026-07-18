# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Feature 9: Budget Alerts — webhook-based cost threshold notifications.
Fires when a team's daily or monthly spending crosses configured thresholds.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError

try:
    import sqlite3
except ImportError:
    sqlite3 = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BudgetAlert:
    alert_id: str
    org_id: str                     # team_id or "default-org"
    threshold_pct: float           # 0-100; e.g. 80 = fire at 80% of limit
    webhook_url: str
    enabled: bool = True
    alert_type: str = "daily"       # "daily" | "monthly"
    daily_limit: float = 0.0        # USD; 0 = disabled
    monthly_limit: float = 0.0
    last_triggered: Optional[datetime] = field(default=None)
    last_triggered_at_unix: Optional[int] = field(default=None)
    created_at: datetime = field(default_factory=_utc_now)
    secret: str = ""               # HMAC signing secret for webhook payload


@dataclass
class AlertEvent:
    alert_id: str
    org_id: str
    alert_type: str                 # "daily_budget_warning" | "monthly_budget_warning"
                                     # "daily_budget_exceeded" | "monthly_budget_exceeded"
    threshold_pct: float
    spent: float
    limit: float
    pct_used: float
    webhook_url: str
    fired_at: datetime = field(default_factory=_utc_now)
    signature: str = ""             # HMAC-SHA256 of payload


class BudgetAlertStore:
    """
    SQLite-backed budget alert configuration + in-process cooldown tracking.
    """

    def __init__(self, db_path: str = ".modelfungible/budget_alerts.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._init_db()
        # In-memory cooldown: alert_id → last_fired_unix
        self._cooldown: dict[str, int] = {}
        self._lock = threading.Lock()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS budget_alerts (
                    alert_id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    threshold_pct REAL NOT NULL DEFAULT 80,
                    webhook_url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    alert_type TEXT NOT NULL DEFAULT 'daily',
                    daily_limit REAL NOT NULL DEFAULT 0,
                    monthly_limit REAL NOT NULL DEFAULT 0,
                    last_triggered TEXT,
                    last_triggered_at_unix INTEGER,
                    created_at TEXT NOT NULL,
                    secret TEXT NOT NULL DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_events (
                    event_id TEXT PRIMARY KEY,
                    alert_id TEXT NOT NULL,
                    org_id TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    threshold_pct REAL NOT NULL,
                    spent REAL NOT NULL,
                    limit_amt REAL NOT NULL,
                    pct_used REAL NOT NULL,
                    webhook_url TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    fired_at TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'pending',
                    response_code INTEGER,
                    response_body TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_org
                    ON alert_events(org_id, fired_at)
            """)
            conn.commit()

    # ── Alert CRUD ─────────────────────────────────────────────────────────

    def create_alert(
        self, org_id: str, webhook_url: str,
        threshold_pct: float = 80.0,
        alert_type: str = "daily",
        daily_limit: float = 0.0,
        monthly_limit: float = 0.0,
        secret: str = "",
    ) -> BudgetAlert:
        alert = BudgetAlert(
            alert_id=uuid.uuid4().hex[:12],
            org_id=org_id,
            threshold_pct=threshold_pct,
            webhook_url=webhook_url,
            alert_type=alert_type,
            daily_limit=daily_limit,
            monthly_limit=monthly_limit,
            secret=secret,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO budget_alerts
                   (alert_id, org_id, threshold_pct, webhook_url, enabled, alert_type,
                    daily_limit, monthly_limit, created_at, secret)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (alert.alert_id, alert.org_id, alert.threshold_pct, alert.webhook_url,
                 1, alert.alert_type, alert.daily_limit, alert.monthly_limit,
                 alert.created_at.isoformat(), alert.secret)
            )
            conn.commit()
        return alert

    def get_alert(self, alert_id: str) -> Optional[BudgetAlert]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM budget_alerts WHERE alert_id = ?", (alert_id,)
            ).fetchone()
        if not row:
            return None
        return self._alert_from_row(row)

    def list_alerts(self, org_id: Optional[str] = None) -> list[BudgetAlert]:
        with sqlite3.connect(self.db_path) as conn:
            if org_id:
                rows = conn.execute(
                    "SELECT * FROM budget_alerts WHERE org_id = ? ORDER BY created_at DESC",
                    (org_id,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM budget_alerts ORDER BY created_at DESC"
                ).fetchall()
        return [self._alert_from_row(r) for r in rows]

    def update_alert(self, alert_id: str, **kwargs) -> Optional[BudgetAlert]:
        allowed = {"webhook_url", "threshold_pct", "enabled",
                   "alert_type", "daily_limit", "monthly_limit", "secret"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if "enabled" in updates:
            updates["enabled"] = int(updates["enabled"])
        if not updates:
            return self.get_alert(alert_id)
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [alert_id]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"UPDATE budget_alerts SET {set_clause} WHERE alert_id = ?", vals)
            conn.commit()
        return self.get_alert(alert_id)

    def delete_alert(self, alert_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "DELETE FROM budget_alerts WHERE alert_id = ?", (alert_id,)
            )
            conn.commit()
            return cur.rowcount > 0

    def _alert_from_row(self, row) -> BudgetAlert:
        cols = ["alert_id", "org_id", "threshold_pct", "webhook_url", "enabled",
                "alert_type", "daily_limit", "monthly_limit",
                "last_triggered", "last_triggered_at_unix", "created_at", "secret"]
        d = dict(zip(cols, row))
        return BudgetAlert(
            alert_id=d["alert_id"],
            org_id=d["org_id"],
            threshold_pct=d["threshold_pct"],
            webhook_url=d["webhook_url"],
            enabled=bool(d["enabled"]),
            alert_type=d["alert_type"],
            daily_limit=d["daily_limit"],
            monthly_limit=d["monthly_limit"],
            last_triggered=datetime.fromisoformat(d["last_triggered"]) if d["last_triggered"] else None,
            last_triggered_at_unix=d["last_triggered_at_unix"],
            created_at=datetime.fromisoformat(d["created_at"]),
            secret=d["secret"],
        )

    # ── Firing ─────────────────────────────────────────────────────────────

    def check_and_fire(
        self, org_id: str, spent_today: float, spent_month: float,
        daily_limit: float, monthly_limit: float,
    ) -> list[AlertEvent]:
        """
        Called after each model execution. Evaluates all enabled alerts for org_id.
        Returns list of AlertEvents that were fired (empty if none).
        Uses a per-alert cooldown of 1 hour to avoid spamming.
        """
        if daily_limit <= 0 and monthly_limit <= 0:
            return []

        fired = []
        now_unix = int(time.time())
        now = _utc_now()

        alerts = self.list_alerts(org_id=org_id)
        for alert in alerts:
            if not alert.enabled:
                continue

            # Determine what to check
            if alert.alert_type == "daily":
                limit = daily_limit or alert.daily_limit
                spent = spent_today
                alert_type = "daily_budget_warning"
                exceeded_type = "daily_budget_exceeded"
                exceeded_limit = limit
            elif alert.alert_type == "monthly":
                limit = monthly_limit or alert.monthly_limit
                spent = spent_month
                alert_type = "monthly_budget_warning"
                exceeded_type = "monthly_budget_exceeded"
                exceeded_limit = limit
            else:
                continue

            if limit <= 0:
                continue

            pct = (spent / limit) * 100

            # Cooldown check: 1 hour between fires of same alert
            with self._lock:
                last_fired = self._cooldown.get(alert.alert_id, 0)
                if now_unix - last_fired < 3600:
                    continue

            # Should we fire?
            if pct >= 100:
                fired_type = exceeded_type
            elif pct >= alert.threshold_pct:
                fired_type = alert_type
            else:
                continue

            # Build payload
            payload = {
                "event": fired_type,
                "alert_id": alert.alert_id,
                "org_id": org_id,
                "threshold_pct": alert.threshold_pct,
                "spent_usd": round(spent, 6),
                "limit_usd": round(limit, 4),
                "pct_used": round(pct, 2),
                "alert_type": alert.alert_type,
                "fired_at": now.isoformat(),
            }
            payload_json = json.dumps(payload, ensure_ascii=False)

            # HMAC signature
            sig = ""
            if alert.secret:
                sig = hmac.new(
                    alert.secret.encode(),
                    payload_json.encode(),
                    hashlib.sha256
                ).hexdigest()
                payload["signature"] = sig

            event = AlertEvent(
                alert_id=alert.alert_id,
                org_id=org_id,
                alert_type=fired_type,
                threshold_pct=alert.threshold_pct,
                spent=spent,
                limit=limit,
                pct_used=pct,
                webhook_url=alert.webhook_url,
                fired_at=now,
                signature=sig,
            )

            # Fire webhook (async fire-and-forget)
            self._send_webhook(event, payload, alert.alert_id)
            fired.append(event)

            # Update cooldown + last_triggered
            with self._lock:
                self._cooldown[alert.alert_id] = now_unix
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """UPDATE budget_alerts
                       SET last_triggered = ?, last_triggered_at_unix = ?
                       WHERE alert_id = ?""",
                    (now.isoformat(), now_unix, alert.alert_id)
                )
                conn.commit()

        return fired

    def _send_webhook(
        self, event: AlertEvent, payload: dict, alert_id_for_log: str,
        timeout_secs: int = 5,
    ):
        """Fire-and-forget webhook POST."""
        def _fire():
            body = json.dumps(payload, ensure_ascii=False).encode()
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "ModelFungible-BudgetAlert/1.0",
                "X-Alert-ID": event.alert_id,
            }
            if event.signature:
                headers["X-Signature-SHA256"] = event.signature

            try:
                req = Request(
                    event.webhook_url,
                    data=body,
                    headers=headers,
                    method="POST",
                )
                with urlopen(req, timeout=timeout_secs) as resp:
                    code = resp.status
                    body_out = resp.read(512).decode(errors="replace")
            except URLError as e:
                code = 0
                body_out = str(e.reason)[:512]
            except Exception as e:
                code = 0
                body_out = str(e)[:512]

            # Log outcome
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO alert_events
                       (event_id, alert_id, org_id, alert_type, threshold_pct,
                        spent, limit_amt, pct_used, webhook_url, signature,
                        fired_at, delivery_status, response_code, response_body)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (uuid.uuid4().hex[:16], alert_id_for_log, event.org_id,
                     event.alert_type, event.threshold_pct, event.spent,
                     event.limit, event.pct_used, event.webhook_url,
                     event.signature, event.fired_at.isoformat(),
                     "delivered" if code == 200 else "failed",
                     code, body_out)
                )
                conn.commit()

        t = threading.Thread(target=_fire, daemon=True)
        t.start()

    # ── History ────────────────────────────────────────────────────────────

    def get_events(
        self, org_id: Optional[str] = None,
        limit: int = 100, offset: int = 0,
    ) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            if org_id:
                rows = conn.execute(
                    """SELECT * FROM alert_events
                       WHERE org_id = ?
                       ORDER BY fired_at DESC LIMIT ? OFFSET ?""",
                    (org_id, limit, offset)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM alert_events
                       ORDER BY fired_at DESC LIMIT ? OFFSET ?""",
                    (limit, offset)
                ).fetchall()
        cols = ["event_id", "alert_id", "org_id", "alert_type", "threshold_pct",
                "spent", "limit_amt", "pct_used", "webhook_url", "signature",
                "fired_at", "delivery_status", "response_code", "response_body"]
        return [dict(zip(cols, r)) for r in rows]

    def get_alert_stats(self, alert_id: str) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """SELECT COUNT(*), SUM(CASE WHEN delivery_status='delivered' THEN 1 ELSE 0 END)
                   FROM alert_events WHERE alert_id = ?""",
                (alert_id,)
            ).fetchone()
        return {
            "alert_id": alert_id,
            "total_fired": row[0] or 0,
            "total_delivered": row[1] or 0,
        }
