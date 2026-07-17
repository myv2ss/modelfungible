# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Tests for audit.py — Immutable audit trail with hash chain.

Tests:
- AuditLogger: append-only log, hash chain integrity
- PHI/PII detection: PIIDetector flags sensitive fields
- Compliance stamps: stamp as DRAFT/APPROVED/REJECTED
- Export: JSON + CSV tamper-evident export
- Retention policies: auto-purge after retention period
- Audit query: filter by date, user, action, outcome
"""
import pytest, json, time, tempfile, os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


class TestAuditLogIntegrity:
    def test_first_entry_has_no_previous_hash(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="test", actor="user1", outcome="success")
            entries = logger.entries()
            assert len(entries) == 1
            assert entries[0]["previous_hash"] == "0" * 64

    def test_second_entry_continues_chain(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="test1", actor="user1", outcome="success")
            logger.log(action="test2", actor="user2", outcome="success")
            entries = logger.entries()
            assert len(entries) == 2
            assert entries[1]["previous_hash"] == entries[0]["hash"]

    def test_hash_chain_integrity(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="step1", actor="user1", outcome="success")
            logger.log(action="step2", actor="user2", outcome="success")
            assert logger.verify_integrity() == True

    def test_tamper_detected(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="original", actor="user1", outcome="success")
            # Tamper with the log file
            log_path = Path(tmpdir) / "audit_log.jsonl"
            lines = log_path.read_text().splitlines()
            lines[0] = lines[0].replace('"original"', '"TAMPERED"')
            log_path.write_text("\n".join(lines))
            assert logger.verify_integrity() == False

    def test_prevents_duplicate_entry_ids(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="test", actor="user1", outcome="success")
            # Second log with same entry_id should be rejected
            result = logger.log(action="test", actor="user1", outcome="success",
                               entry_id=logger.entries()[0]["entry_id"])
            assert result == False  # duplicate rejected


class TestPHIDetection:
    def test_detects_email(self):
        from modelfungible.enterprise.audit import PIIDetector
        d = PIIDetector()
        flags = d.scan({"email": "john.doe@gmail.com", "name": "John"})
        assert any("email" in f for f in flags)

    def test_detects_phone(self):
        from modelfungible.enterprise.audit import PIIDetector
        d = PIIDetector()
        flags = d.scan({"phone": "+1-555-123-4567"})
        assert any("phone" in f for f in flags)

    def test_detects_ssn(self):
        from modelfungible.enterprise.audit import PIIDetector
        d = PIIDetector()
        flags = d.scan({"ssn": "123-45-6789"})
        assert any("ssn" in f for f in flags)

    def test_detects_credit_card(self):
        from modelfungible.enterprise.audit import PIIDetector
        d = PIIDetector()
        flags = d.scan({"card": "4532-1234-5678-9012"})
        assert any("credit_card" in f for f in flags)

    def test_nested_dict_detection(self):
        from modelfungible.enterprise.audit import PIIDetector
        d = PIIDetector()
        flags = d.scan({
            "patient": {
                "name": "John Doe",
                "ssn": "123-45-6789",
                "diagnosis": "flu"
            }
        })
        assert any("ssn" in f for f in flags)
        assert "diagnosis" not in flags  # not PHI by itself

    def test_redact_removes_pii(self):
        from modelfungible.enterprise.audit import PIIDetector
        d = PIIDetector()
        redacted = d.redact({
            "email": "john@example.com",
            "action": "approve_claim",
            "amount": 5000
        })
        assert "john@example.com" not in str(redacted.values())
        assert redacted["action"] == "approve_claim"


class TestComplianceStamps:
    def test_stamp_draft(self):
        from modelfungible.enterprise.audit import ComplianceStamper
        s = ComplianceStamper()
        stamped = s.stamp({"action": "approve_claim"}, "DRAFT")
        assert stamped["compliance_stamp"] == "DRAFT"
        assert stamped["stamp_reason"] == ""

    def test_stamp_with_reason(self):
        from modelfungible.enterprise.audit import ComplianceStamper
        s = ComplianceStamper()
        stamped = s.stamp({"action": "deny_claim"}, "REJECTED", reason="Insufficient documentation")
        assert stamped["compliance_stamp"] == "REJECTED"
        assert stamped["stamp_reason"] == "Insufficient documentation"

    def test_phi_stamped_hipaa(self):
        from modelfungible.enterprise.audit import ComplianceStamper
        s = ComplianceStamper()
        stamped = s.stamp({"patient_data": "..."}, "APPROVED", phi=True)
        assert stamped["compliance_stamp"] == "APPROVED"
        assert stamped["phi_compliant"] == True


class TestAuditQuery:
    def test_filter_by_actor(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="login", actor="alice", outcome="success")
            logger.log(action="view_record", actor="bob", outcome="success")
            logger.log(action="login", actor="alice", outcome="success")
            results = logger.query(actor="alice")
            assert len(results) == 2

    def test_filter_by_action(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="login", actor="alice", outcome="success")
            logger.log(action="view_record", actor="bob", outcome="success")
            logger.log(action="login", actor="alice", outcome="success")
            results = logger.query(action="login")
            assert len(results) == 2

    def test_filter_by_outcome(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="login", actor="alice", outcome="success")
            logger.log(action="login", actor="bob", outcome="failure")
            results = logger.query(outcome="failure")
            assert len(results) == 1
            assert results[0]["actor"] == "bob"

    def test_filter_by_date_range(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="test", actor="user1", outcome="success")
            results = logger.query(
                start_date="2020-01-01",
                end_date="2030-12-31"
            )
            assert len(results) == 1

    def test_pagination(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            for i in range(10):
                logger.log(action=f"step_{i}", actor="user1", outcome="success")
            page1 = logger.query(limit=3, offset=0)
            assert len(page1) == 3
            page2 = logger.query(limit=3, offset=3)
            assert len(page2) == 3
            assert page1[0]["action"] != page2[0]["action"]


class TestRetentionPolicy:
    def test_gdpr_retention(self):
        from modelfungible.enterprise.audit import RetentionPolicy
        policy = RetentionPolicy("gdpr")
        assert policy.max_age_days == 30
        assert policy.pii_only == True

    def test_hipaa_retention(self):
        from modelfungible.enterprise.audit import RetentionPolicy
        policy = RetentionPolicy("hipaa")
        assert policy.max_age_days == 2190  # 6 years

    def test_finra_retention(self):
        from modelfungible.enterprise.audit import RetentionPolicy
        policy = RetentionPolicy("finra")
        assert policy.max_age_days == 2190  # 6 years

    def test_soc2_retention(self):
        from modelfungible.enterprise.audit import RetentionPolicy
        policy = RetentionPolicy("soc2")
        assert policy.max_age_days == 365  # 1 year


class TestExport:
    def test_export_json(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="test", actor="user1", outcome="success")
            path = Path(tmpdir) / "export.json"
            logger.export_json(path)
            data = json.loads(path.read_text())
            assert len(data) == 1
            assert data[0]["action"] == "test"

    def test_export_csv(self):
        from modelfungible.enterprise.audit import AuditLogger
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = AuditLogger(tmpdir, partition_by="none")
            logger.log(action="test", actor="user1", outcome="success")
            path = Path(tmpdir) / "export.csv"
            logger.export_csv(path)
            content = path.read_text()
            assert "action,actor" in content
            assert "test,user1" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
