# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Audit Logging, Compliance & Reporting — ModelFungible Enterprise

Provides immutable audit trails with hash-chain integrity for regulated
industries: HIPAA (healthcare), GDPR (EU data), FINRA/SEC (finance),
SOC 2, PCI-DSS.
"""
from __future__ import annotations
import hashlib, json, re, uuid, csv, io, copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


# ─── PII / PHI Detection ──────────────────────────────────────────
class PIIDetector:
    PATTERNS = {
        "email": re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"),
        "phone": re.compile(r"(\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}"),
        "ssn": re.compile(r"\d{3}-\d{2}-\d{4}"),
        "credit_card": re.compile(r"\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}"),
        "ip_address": re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
        "passport": re.compile(r"[A-Z]{1,2}\d{6,9}"),
        "drivers_license": re.compile(r"[A-Z]\d{5,8}|\d{5,8}[A-Z]"),
        "patient_id": re.compile(r"(patient|mrn|medical_record)[_-]?id[:\s]*[\"']?\w+", re.IGNORECASE),
    }
    PHI_KEYWORDS = {
        "diagnosis", "treatment", "medication", "prescription", "lab_result",
        "blood_pressure", "condition", "symptom", "patient_name", "physician",
        "hospital", "admission_date", "discharge_date", "medical_record", "health_plan",
    }

    def scan(self, data: dict) -> set[str]:
        flags: set[str] = set()
        self._scan(data, "", flags)
        return flags

    def _scan(self, obj, key, flags):
        if isinstance(obj, dict):
            for k, v in obj.items(): self._scan(v, k, flags)
        elif isinstance(obj, list):
            for v in obj: self._scan(v, key, flags)
        elif isinstance(obj, str):
            for ptype, pat in self.PATTERNS.items():
                if pat.search(obj.strip()):
                    flags.add(f"{key}.{ptype}" if key else ptype)
            kl = key.lower()
            if any(kw in kl for kw in self.PHI_KEYWORDS):
                flags.add(f"{key}.phi_keyword")

    def redact(self, data: dict, repl="[REDACTED]") -> dict:
        def _r(obj):
            if isinstance(obj, dict): return {k: _r(v) for k, v in obj.items()}
            if isinstance(obj, list): return [_r(v) for v in obj]
            if isinstance(obj, str):
                s = obj
                for pat in self.PATTERNS.values(): s = pat.sub(repl, s)
                return s
            return obj
        return _r(data)


# ─── Compliance Stamping ─────────────────────────────────────────
class ComplianceStamper:
    VALID_STAMPS = {"DRAFT", "APPROVED", "REJECTED", "ESCALATED", "ANONYMIZED"}

    def stamp(self, output: dict, status: str, reason="", phi=False,
              gdpr_consent=False, custom_fields=None) -> dict:
        if status not in self.VALID_STAMPS:
            raise ValueError(f"Invalid stamp: {status}")
        stamped = dict(output)
        stamped["compliance_stamp"] = status
        stamped["stamp_reason"] = reason
        stamped["stamp_timestamp"] = datetime.now(timezone.utc).isoformat()
        stamped["phi_compliant"] = phi
        stamped["gdpr_consent"] = gdpr_consent
        if custom_fields: stamped["compliance_custom"] = custom_fields
        return stamped


# ─── Retention Policies ───────────────────────────────────────────
class RetentionPolicy:
    POLICIES = {
        "gdpr":    {"days": 30,   "pii_only": True,  "desc": "EU GDPR"},
        "hipaa":   {"days": 2190, "pii_only": False, "desc": "US HIPAA — 6 years"},
        "finra":   {"days": 2190, "pii_only": False, "desc": "FINRA — 6 years"},
        "sec":     {"days": 1825, "pii_only": False, "desc": "SEC — 5 years"},
        "soc2":    {"days": 365,  "pii_only": False, "desc": "SOC 2 — 1 year"},
        "pci_dss": {"days": 365,  "pii_only": True,  "desc": "PCI-DSS — 1 year"},
        "default": {"days": 90,   "pii_only": False, "desc": "Default"},
    }

    def __init__(self, regulation="default"):
        if regulation not in self.POLICIES:
            raise ValueError(f"Unknown regulation: {regulation}")
        cfg = self.POLICIES[regulation]
        self.max_age_days: int = cfg["days"]
        self.pii_only: bool = cfg["pii_only"]
        self.description: str = cfg["desc"]

    def is_stale(self, entry: dict) -> bool:
        try:
            ts = entry.get("timestamp", "")
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return False
        return dt < (datetime.now(timezone.utc) - timedelta(days=self.max_age_days))

    def stale_entries(self, entries: list[dict]) -> list[dict]:
        return [e for e in entries if self.is_stale(e)]


# ─── Hash computation ────────────────────────────────────────────
def _entry_hash(entry: dict, prev: str) -> str:
    ctx = entry.get("context", {})
    ctx_str = json.dumps(ctx, sort_keys=True, ensure_ascii=True)
    payload = {
        "id": entry.get("entry_id", ""),
        "seq": entry.get("sequence", 0),
        "prev": prev,
        "ts": entry.get("timestamp", ""),
        "action": entry.get("action", ""),
        "actor": entry.get("actor", ""),
        "org": entry.get("org_id", ""),
        "outcome": entry.get("outcome", ""),
        "model": entry.get("model_id", ""),
        "ctx_hash": hashlib.sha256(ctx_str.encode()).hexdigest()[:32],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


# ─── Immutable Audit Logger ──────────────────────────────────────
class AuditLogger:
    """
    Append-only audit log with SHA-256 hash chain.

    Each entry's hash includes the previous entry's hash, creating a
    tamper-evident chain (similar to blockchain, single-writer).

    Log format: JSONL (one JSON dict per line).

    Usage:
        logger = AuditLogger("/var/log/modelfungible/audit")
        logger.log(action="execute_strategy", actor="agent_001",
                   org_id="acme_corp", outcome="success",
                   model_id="claude-3-5-sonnet",
                   context={"strategy": "EQM", "signal": "ADBE"},
                   pii_detected=["email"])
        assert logger.verify_integrity()  # True = chain intact
        results = logger.query(actor="agent_001", limit=100)
        logger.export_json("/export/audit_q1.json")
        logger.export_csv("/export/audit_q1.csv")
    """

    GENESIS = "0" * 64

    def __init__(self, log_dir: str | Path, partition_by: str = "day"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.partition_by = partition_by
        self._seen_ids: set = set()
        for e in self.entries():
            if "entry_id" in e:
                self._seen_ids.add(e["entry_id"])

    def _log_path(self, ts: Optional[str] = None) -> Path:
        if self.partition_by == "day" and ts:
            return self.log_dir / f"audit_{ts[:10]}.jsonl"
        return self.log_dir / "audit_log.jsonl"

    def log(
        self,
        action: str,
        actor: str,
        outcome: str,
        org_id: str = "",
        strategy_id: str = "",
        model_id: str = "",
        context: Optional[dict] = None,
        pii_detected: Optional[list] = None,
        compliance_stamp: str = "",
        stamp_reason: str = "",
        metadata: Optional[dict] = None,
        entry_id: Optional[str] = None,
    ) -> bool:
        """
        Append an immutable entry. Returns True if appended,
        False if rejected (duplicate entry_id).
        """
        all_entries = self.entries()
        prev_hash = all_entries[-1]["hash"] if all_entries else self.GENESIS
        seq = len(all_entries) + 1

        eid = entry_id or str(uuid.uuid4())
        if eid in self._seen_ids:
            return False
        self._seen_ids.add(eid)

        ts = datetime.now(timezone.utc).isoformat()
        entry = {
            "entry_id": eid,
            "sequence": seq,
            "timestamp": ts,
            "previous_hash": prev_hash,
            "action": action,
            "actor": actor,
            "org_id": org_id,
            "strategy_id": strategy_id,
            "model_id": model_id,
            "outcome": outcome,
            "context": context or {},
            "pii_detected": pii_detected or [],
            "compliance_stamp": compliance_stamp,
            "stamp_reason": stamp_reason,
            "metadata": metadata or {},
        }
        entry["hash"] = _entry_hash(entry, prev_hash)
        with open(self._log_path(ts), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True

    def entries(self) -> list[dict]:
        if self.partition_by == "day":
            paths = sorted(self.log_dir.glob("audit_*.jsonl"))
        else:
            paths = [self.log_dir / "audit_log.jsonl"]
        entries = []
        for p in paths:
            if not p.exists(): continue
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try: entries.append(json.loads(line))
                    except json.JSONDecodeError: continue
        return sorted(entries, key=lambda e: e.get("sequence", 0))

    def verify_integrity(self) -> bool:
        """Verify hash chain. Returns False if tampered."""
        all_entries = self.entries()
        for i, e in enumerate(all_entries):
            prev = all_entries[i - 1]["hash"] if i > 0 else self.GENESIS
            if e["previous_hash"] != prev: return False
            if e["hash"] != _entry_hash(e, prev): return False
        return True

    def query(
        self,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        outcome: Optional[str] = None,
        org_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Filter entries (AND-combined). Returns fresh list."""
        r = self.entries()
        if actor:   r = [e for e in r if e.get("actor") == actor]
        if action: r = [e for e in r if e.get("action") == action]
        if outcome: r = [e for e in r if e.get("outcome") == outcome]
        if org_id: r = [e for e in r if e.get("org_id") == org_id]
        if strategy_id: r = [e for e in r if e.get("strategy_id") == strategy_id]
        def _aware(dt_str: str):
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        if start_date:
            sd = _aware(start_date)
            r = [e for e in r if _aware(e.get("timestamp","").replace("Z","+00:00")) >= sd]
        if end_date:
            ed = _aware(end_date)
            r = [e for e in r if _aware(e.get("timestamp","").replace("Z","+00:00")) <= ed]
        return r[offset:offset + limit]

    def count(self) -> int:
        return len(self.entries())

    def export_json(self, path: str | Path) -> None:
        with open(Path(path), "w", encoding="utf-8") as f:
            json.dump(self.entries(), f, ensure_ascii=False, indent=2)

    def export_csv(self, path: str | Path) -> None:
        entries = self.entries()
        if not entries:
            Path(path).write_text("", encoding="utf-8")
            return
        fields = ["entry_id","sequence","timestamp","action","actor","org_id",
                  "strategy_id","model_id","outcome","pii_detected",
                  "compliance_stamp","stamp_reason","hash","previous_hash"]
        with open(Path(path), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(entries)
