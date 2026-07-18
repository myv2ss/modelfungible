# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Compliance Policy Engine — define, enforce, and audit policy rules.

Policies are JSON objects with:
- conditions: list of conditions (all must pass)
- actions: pass | block | redact | flag

Condition types:
  - "prompt_no_pii": prompt must not contain PII
  - "output_no_pii": output must not contain PII
  - "output_max_length": max characters in output
  - "output_contains_blocked": output must not contain blocked terms
  - "prompt_injects_sql": detect prompt injection patterns
  - "output_safe": no harmful content markers
  - "model_approved": model must be in approved list
  - "department_allowed": department can use this model
"""
from __future__ import annotations

import json, os, sqlite3, uuid, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Literal
from dataclasses import dataclass, field


DB_PATH = os.environ.get("MODELFUNGIBLE_COMPLIANCE_DB",
                         str(Path(__file__).parent.parent / "data" / "compliance.db"))


# ─── Policy Model ───────────────────────────────────────────────────────────────

@dataclass
class Policy:
    policy_id: str
    name: str
    description: str
    domain: str              # legal | finance | healthcare | hr | coding | general
    enabled: bool
    priority: int             # higher = checked first
    conditions: list[dict]   # list of condition objects
    actions: dict            # e.g. {"on_fail": "block", "on_pass": "allow"}
    created_by: str
    created_at: str
    updated_at: str
    tags: list[str] = field(default_factory=list)


@dataclass
class PolicyResult:
    policy_id: str
    policy_name: str
    passed: bool
    failed_conditions: list[str]
    action_taken: str
    details: str = ""


# ─── Storage ───────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.Connection(DB_PATH, autocommit=True)
    conn.execute("PRAGMA journal_mode=WAL")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS policies (
        policy_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        domain TEXT DEFAULT 'general',
        enabled INTEGER DEFAULT 1,
        priority INTEGER DEFAULT 0,
        conditions TEXT DEFAULT '[]',
        actions TEXT DEFAULT '{}',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        tags TEXT DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS policy_violations (
        violation_id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL,
        request_id TEXT DEFAULT '',
        model_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        org_id TEXT DEFAULT 'default-org',
        prompt_preview TEXT DEFAULT '',
        output_preview TEXT DEFAULT '',
        failed_conditions TEXT DEFAULT '[]',
        action_taken TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (policy_id) REFERENCES policies(policy_id)
    );
    CREATE INDEX IF NOT EXISTS idx_violations_policy ON policy_violations(policy_id);
    CREATE INDEX IF NOT EXISTS idx_violations_actor ON policy_violations(actor);
    CREATE INDEX IF NOT EXISTS idx_violations_created ON policy_violations(created_at);
    CREATE INDEX IF NOT EXISTS idx_policies_domain ON policies(domain);
    CREATE INDEX IF NOT EXISTS idx_policies_enabled ON policies(enabled);
    """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Policy Evaluator ───────────────────────────────────────────────────────────

class ComplianceEngine:
    """
    Evaluates prompts and outputs against defined policies.

    Usage:
        engine = ComplianceEngine()
        results = engine.evaluate(
            prompt="User query here",
            output="Model response here",
            model_id="claude-3.5-sonnet",
            actor="analyst1",
            department="legal"
        )
        # If any result.action_taken == "block", reject the request
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._conn = sqlite3.Connection(db_path, autocommit=True)
        self._conn.execute("PRAGMA journal_mode=WAL")
        _init_schema(self._conn)

    def evaluate(
        self,
        prompt: str,
        output: str = "",
        model_id: str = "",
        actor: str = "",
        org_id: str = "default-org",
        request_id: str = "",
        department: str = "",
        metadata: dict = None,
    ) -> list[PolicyResult]:
        """
        Evaluate prompt (pre-execution) and output (post-execution).
        Returns list of PolicyResults in priority order.
        """
        policies = self._get_policies()
        results = []
        for pol in policies:
            result = self._evaluate_policy(pol, prompt, output, model_id, actor, org_id, department, metadata)
            results.append(result)
            if result.passed:
                continue
            # Log violation
            self._log_violation(result, request_id, model_id, actor, org_id, prompt, output)
        return results

    def evaluate_prompt(
        self,
        prompt: str,
        model_id: str = "",
        actor: str = "",
        org_id: str = "default-org",
        department: str = "",
        metadata: dict = None,
    ) -> list[PolicyResult]:
        """Pre-execution check — only evaluates prompt-related conditions."""
        return self.evaluate(prompt=prompt, model_id=model_id, actor=actor,
                           org_id=org_id, department=department, metadata=metadata)

    def evaluate_output(
        self,
        output: str,
        model_id: str = "",
        actor: str = "",
        request_id: str = "",
        metadata: dict = None,
    ) -> list[PolicyResult]:
        """Post-execution check — only evaluates output-related conditions."""
        policies = self._get_policies()
        results = []
        for pol in policies:
            output_conditions = [c for c in pol.conditions if self._is_output_condition(c)]
            if not output_conditions:
                continue
            pol_copy = Policy(
                policy_id=pol.policy_id, name=pol.name, description=pol.description,
                domain=pol.domain, enabled=pol.enabled, priority=pol.priority,
                conditions=output_conditions, actions=pol.actions,
                created_by=pol.created_by, created_at=pol.created_at,
                updated_at=pol.updated_at, tags=pol.tags,
            )
            result = self._evaluate_policy(pol_copy, "", output, model_id, actor, "default-org", "", metadata)
            results.append(result)
            if not result.passed:
                self._log_violation(result, request_id, model_id, actor, "default-org", "", output)
        return results

    def _is_output_condition(self, cond: dict) -> bool:
        t = cond.get("type", "")
        return t in ("output_no_pii", "output_max_length", "output_contains_blocked",
                     "output_safe", "output_matches_pattern")

    def _evaluate_policy(
        self, pol: Policy, prompt: str, output: str, model_id: str,
        actor: str, org_id: str, department: str, metadata: dict
    ) -> PolicyResult:
        if not pol.enabled:
            return PolicyResult(policy_id=pol.policy_id, policy_name=pol.name,
                               passed=True, failed_conditions=[], action_taken="skipped")

        failed = []
        details = []

        for cond in pol.conditions:
            ok, detail = self._check_condition(cond, prompt, output, model_id, department)
            if not ok:
                failed.append(cond.get("type", "unknown"))
                details.append(detail)

        passed = len(failed) == 0
        action = pol.actions.get("on_fail", "block") if not passed else pol.actions.get("on_pass", "allow")

        return PolicyResult(
            policy_id=pol.policy_id, policy_name=pol.name,
            passed=passed, failed_conditions=failed,
            action_taken=action, details="; ".join(details) if details else "All checks passed"
        )

    def _check_condition(self, cond: dict, prompt: str, output: str, model_id: str, department: str) -> tuple[bool, str]:
        ctype = cond.get("type", "")
        params = cond.get("params", {})

        if ctype == "prompt_no_pii":
            from modelfungible.enterprise.audit import PIIDetector
            d = PIIDetector()
            result = d.scan({"prompt": prompt})
            if result:
                flags = list(result.keys())
                return False, f"PII detected in prompt: {', '.join(flags)}"
            return True, "No PII in prompt"

        if ctype == "output_no_pii":
            from modelfungible.enterprise.audit import PIIDetector
            d = PIIDetector()
            result = d.scan({"output": output})
            if result:
                flags = list(result.keys())
                return False, f"PII detected in output: {', '.join(flags)}"
            return True, "No PII in output"

        if ctype == "output_max_length":
            max_len = params.get("max_chars", 5000)
            if len(output) > max_len:
                return False, f"Output {len(output)} chars exceeds max {max_len}"
            return True, f"Output {len(output)} chars within limit"

        if ctype == "output_contains_blocked":
            blocked = params.get("terms", [])
            for term in blocked:
                if term.lower() in output.lower():
                    return False, f"Blocked term found: '{term}'"
            return True, "No blocked terms in output"

        if ctype == "prompt_injects_sql":
            # Detect common prompt injection patterns
            patterns = [
                r"ignore\s+(previous|all|above)\s+(instructions?|rules?|constraints?)",
                r"forget\s+(previous|all|your)\s+(instructions?|rules?)",
                r"(system|prompt)\s*:\s*you\s+are\s+a\s+different",
                r"---\s*system\s*message",
                r"\{\{\s*.*\}\}\s*.*roleplay",
                r"sql\s*injection|s' OR '1'='1|UNION\s+SELECT",
            ]
            for pat in patterns:
                if re.search(pat, prompt, re.IGNORECASE):
                    return False, f"Prompt injection pattern detected: {pat}"
            return True, "No prompt injection patterns"

        if ctype == "output_safe":
            harmful = ["exec(", "os.system", "subprocess(", "__import__", "eval("]
            for h in harmful:
                if h in output:
                    return False, f"Potentially harmful code detected: {h}"
            return True, "Output appears safe"

        if ctype == "model_approved":
            approved = params.get("models", [])
            if approved and model_id not in approved:
                return False, f"Model {model_id} not in approved list: {approved}"
            return True, f"Model {model_id} is approved"

        if ctype == "department_allowed":
            allowed = params.get("departments", [])
            if allowed and department not in allowed:
                return False, f"Department '{department}' not allowed for this policy"
            return True, f"Department '{department}' is allowed"

        if ctype == "output_matches_pattern":
            pattern = params.get("regex", "")
            if pattern:
                if not re.search(pattern, output):
                    return False, f"Output does not match required pattern: {pattern}"
            return True, "Output matches required pattern"

        if ctype == "cost_under_limit":
            max_cost = params.get("max_usd", 1.0)
            cost = params.get("actual_cost", 0)
            if cost > max_cost:
                return False, f"Cost ${cost:.4f} exceeds limit ${max_cost}"
            return True, f"Cost ${cost:.4f} within limit"

        # Unknown condition type — skip
        return True, f"Unknown condition type: {ctype}"

    def _log_violation(
        self, result: PolicyResult, request_id: str, model_id: str,
        actor: str, org_id: str, prompt: str, output: str
    ):
        vid = f"viol_{uuid.uuid4().hex[:12]}"
        self._conn.execute("""
            INSERT INTO policy_violations
            (violation_id, policy_id, request_id, model_id, actor, org_id,
             prompt_preview, output_preview, failed_conditions, action_taken, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            vid, result.policy_id, request_id, model_id, actor, org_id,
            prompt[:200], output[:200],
            json.dumps(result.failed_conditions),
            result.action_taken, _now(),
        ])

    def _get_policies(self) -> list[Policy]:
        rows = self._conn.execute(
            "SELECT * FROM policies ORDER BY priority DESC, name ASC"
        ).fetchall()
        policies = []
        for row in rows:
            if not row[4]:  # enabled == 0
                continue
            policies.append(Policy(
                policy_id=row[0], name=row[1], description=row[2] or "",
                domain=row[3], enabled=bool(row[4]), priority=row[5],
                conditions=json.loads(row[6] or "[]"),
                actions=json.loads(row[7] or "{}"),
                created_by=row[8], created_at=row[9], updated_at=row[10],
                tags=json.loads(row[11] or "[]"),
            ))
        return policies

    # ─── CRUD ────────────────────────────────────────────────────────────────

    def create_policy(
        self, name: str, conditions: list[dict], actions: dict,
        created_by: str, description: str = "", domain: str = "general",
        priority: int = 0, tags: list[str] = None,
    ) -> Policy:
        pid = f"pol_{uuid.uuid4().hex[:12]}"
        now = _now()
        self._conn.execute("""
            INSERT INTO policies (policy_id, name, description, domain, enabled, priority,
                                 conditions, actions, created_by, created_at, updated_at, tags)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """, [
            pid, name, description, domain, priority,
            json.dumps(conditions), json.dumps(actions),
            created_by, now, now, json.dumps(tags or []),
        ])
        return Policy(policy_id=pid, name=name, description=description,
                      domain=domain, enabled=True, priority=priority,
                      conditions=conditions, actions=actions,
                      created_by=created_by, created_at=now, updated_at=now,
                      tags=tags or [])

    def update_policy(self, policy_id: str, **kwargs) -> Optional[Policy]:
        sets, vals = [], []
        for k, v in kwargs.items():
            if k == "conditions":
                sets.append("conditions=?"); vals.append(json.dumps(v))
            elif k == "actions":
                sets.append("actions=?"); vals.append(json.dumps(v))
            elif k == "tags":
                sets.append("tags=?"); vals.append(json.dumps(v))
            elif k == "name":
                sets.append("name=?"); vals.append(v)
            elif k == "description":
                sets.append("description=?"); vals.append(v)
            elif k == "enabled":
                sets.append("enabled=?"); vals.append(1 if v else 0)
            elif k == "priority":
                sets.append("priority=?"); vals.append(v)
            elif k == "domain":
                sets.append("domain=?"); vals.append(v)
        if not sets:
            return self.get_policy(policy_id)
        sets.append("updated_at=?"); vals.append(_now())
        vals.append(policy_id)
        self._conn.execute(f"UPDATE policies SET {', '.join(sets)} WHERE policy_id=?", vals)
        return self.get_policy(policy_id)

    def get_policy(self, policy_id: str) -> Optional[Policy]:
        row = self._conn.execute(
            "SELECT * FROM policies WHERE policy_id=?", [policy_id]
        ).fetchone()
        if not row:
            return None
        return Policy(
            policy_id=row[0], name=row[1], description=row[2] or "",
            domain=row[3], enabled=bool(row[4]), priority=row[5],
            conditions=json.loads(row[6] or "[]"),
            actions=json.loads(row[7] or "{}"),
            created_by=row[8], created_at=row[9], updated_at=row[10],
            tags=json.loads(row[11] or "[]"),
        )


    def list_policies(self, domain: str = None, enabled: bool = None) -> list:
        query = "SELECT * FROM policies WHERE 1=1"
        params = []
        if domain:
            query += " AND domain=?"
            params.append(domain)
        if enabled is not None:
            query += " AND enabled=?"
            params.append(1 if enabled else 0)
        query += " ORDER BY priority DESC, name ASC"
        rows = self._conn.execute(query, params).fetchall()
        return [self.get_policy(row[0]) for row in rows]

    def delete_policy(self, policy_id: str) -> bool:
        self._conn.execute("DELETE FROM policy_violations WHERE policy_id=?", [policy_id])
        self._conn.execute("DELETE FROM policies WHERE policy_id=?", [policy_id])
        return True

    def get_violations(self, policy_id: str = None, actor: str = None,
                       start_date: str = None, end_date: str = None,
                       limit: int = 100, offset: int = 0) -> list:
        query = "SELECT * FROM policy_violations WHERE 1=1"
        par = []
        if policy_id:
            query += " AND policy_id=?"
            par.append(policy_id)
        if actor:
            query += " AND actor=?"
            par.append(actor)
        if start_date:
            query += " AND created_at>=?"
            par.append(start_date)
        if end_date:
            query += " AND created_at<=?"
            par.append(end_date)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        par.extend([limit, offset])
        rows = self._conn.execute(query, par).fetchall()
        return [dict(violation_id=r[0], policy_id=r[1], request_id=r[2],
                     model_id=r[3], actor=r[4], org_id=r[5],
                     prompt_preview=r[6], output_preview=r[7],
                     failed_conditions=json.loads(r[8] or "[]"),
                     action_taken=r[9], created_at=r[10])
                for r in rows]

    def get_compliance_score(self, org_id: str = "default-org", period_days: int = 30) -> dict:
        from datetime import timedelta
        start = (datetime.now(timezone.utc) - timedelta(days=period_days)).isoformat()
        total = self._conn.execute(
            "SELECT COUNT(*) FROM policy_violations WHERE org_id=? AND created_at>=?",
            [org_id, start]).fetchone()[0] or 0
        blocked = self._conn.execute(
            "SELECT COUNT(*) FROM policy_violations WHERE org_id=? AND created_at>=? AND action_taken=?",
            [org_id, start, "block"]).fetchone()[0] or 0
        score = max(0.0, 1.0 - (blocked / max(total, 1)))
        bd = self._conn.execute(
            "SELECT p.name, COUNT(v.violation_id) as vc, "
            "SUM(CASE WHEN v.action_taken=? THEN 1 ELSE 0 END) as bc "
            "FROM policies p "
            "LEFT JOIN policy_violations v ON p.policy_id=v.policy_id "
            "AND v.org_id=? AND v.created_at>=? "
            "GROUP BY p.policy_id ORDER BY vc DESC",
            ["block", org_id, start]).fetchall()
        return {
            "org_id": org_id,
            "period_days": period_days,
            "compliance_score": round(score, 3),
            "total_violations": total,
            "blocked_requests": blocked,
            "policy_breakdown": [{"policy": r[0], "violations": r[1] or 0, "blocked": r[2] or 0}
                                  for r in bd]
        }
