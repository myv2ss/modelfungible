# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
License Key System — ModelFungible Enterprise

Offline-capable license validation using HMAC-SHA256.

Key format:
  MODEL-{payload_b64url}.{sig_b64url}

- payload_b64url: base64url(JSON payload), ~230 chars
- sig_b64url: base64url(HMAC-SHA256 of payload), ~43 chars

Total key length: ~280 chars (copy-paste friendly)
"""

import hmac
import hashlib
import json
import base64
import re
import os
from pathlib import Path
from datetime import date, datetime
from typing import Dict, Any, Optional


# ─────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────
KEY_PREFIX = "MODEL"
DEFAULT_LICENSE_PATH = Path.home() / ".modelfungible" / "license.json"


# ─────────────────────────────────────────────────────────────────
# Encoding helpers
# ─────────────────────────────────────────────────────────────────
def _b64url_encode(data: bytes) -> str:
    """Encode bytes to base64url string (no = padding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(encoded: str) -> bytes:
    """Decode base64url string (with or without = padding)."""
    pad = (4 - len(encoded) % 4) % 4
    return base64.urlsafe_b64decode(encoded + "=" * pad)


# ─────────────────────────────────────────────────────────────────
# LicenseKey
# ─────────────────────────────────────────────────────────────────
class LicenseKey:
    """
    Generate and validate license keys.

    Usage:

    # SERVER SIDE — generate a key for a customer:
    key = LicenseKey.generate(
        payload={"customer_id": "cust_123", "expiry": "2027-07-17",
                 "seats": 10, "features": ["enterprise_adapters"], "plan": "enterprise"},
        server_secret="my_secret"
    )
    # Send key to customer

    # CLIENT SIDE — install and validate a key:
    LicenseKey.save_license(key)  # saves to ~/.modelfungible/license.json
    result = LicenseKey.validate(key, server_secret="my_secret")
    # result["valid"] == True → licensed
    """

    # ─────────────────────────────
    # Generate
    # ─────────────────────────────
    @staticmethod
    def generate(payload: Dict[str, Any], server_secret: str) -> str:
        """Generate a license key from a payload dict."""
        normalized = {
            "customer_id": str(payload["customer_id"]),
            "expiry":      str(payload["expiry"]),
            "seats":       int(payload["seats"]),
            "features":    list(payload["features"]),
            "plan":        str(payload["plan"]),
        }

        payload_bytes = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload_b64 = _b64url_encode(payload_bytes)

        sig = hmac.new(
            server_secret.encode("utf-8"),
            payload_b64.encode("ascii"),
            hashlib.sha256,
        ).digest()
        sig_b64 = _b64url_encode(sig)

        return f"{KEY_PREFIX}-{payload_b64}.{sig_b64}"

    # ─────────────────────────────
    # Validate
    # ─────────────────────────────
    @staticmethod
    def validate(key: str, server_secret: str) -> Dict[str, Any]:
        """
        Validate a license key (offline, no phone-home).

        Returns:
          {"valid": True, "customer_id": ..., "expiry": ..., "seats": N,
           "features": [...], "plan": "..."}

          OR

          {"valid": False, "error": "..."}
        """
        if not key or not isinstance(key, str):
            return {"valid": False, "error": "Key is required"}

        # Parse format: MODEL-{payload}.{sig}
        if not key.startswith(KEY_PREFIX + "-"):
            return {"valid": False, "error": "Key must start with MODEL-"}

        rest = key[len(KEY_PREFIX) + 1:]  # strip "MODEL-"
        parts = rest.rsplit(".", 1)
        if len(parts) != 2:
            return {"valid": False, "error": "Key must contain exactly one '.' separator"}

        payload_b64, sig_b64 = parts

        # Verify signature
        try:
            sig_expected = hmac.new(
                server_secret.encode("utf-8"),
                payload_b64.encode("ascii"),
                hashlib.sha256,
            ).digest()
            sig_actual = _b64url_decode(sig_b64)
        except Exception:
            return {"valid": False, "error": "Invalid signature encoding"}

        if not hmac.compare_digest(sig_actual, sig_expected):
            return {"valid": False, "error": "Invalid license signature"}

        # Decode payload
        try:
            payload_bytes = _b64url_decode(payload_b64)
            payload = json.loads(payload_bytes.decode("utf-8"))
        except Exception as e:
            return {"valid": False, "error": f"Failed to decode license payload: {e}"}

        # Validate required fields
        for field in ["customer_id", "expiry", "seats", "features", "plan"]:
            if field not in payload:
                return {"valid": False, "error": f"Missing '{field}' in license"}

        # Check expiry
        try:
            expiry_date = datetime.strptime(str(payload["expiry"]), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return {"valid": False, "error": "Invalid expiry date in license"}

        if expiry_date < date.today():
            return {
                "valid": False,
                "error": f"License expired on {payload['expiry']}",
                "expired_on": payload["expiry"],
            }

        return {
            "valid": True,
            "customer_id": payload["customer_id"],
            "expiry":      payload["expiry"],
            "seats":       int(payload["seats"]),
            "features":    list(payload["features"]),
            "plan":        payload["plan"],
        }

    # ─────────────────────────────
    # Feature checks
    # ─────────────────────────────
    @staticmethod
    def check_feature(validated: Dict[str, Any], feature: str) -> bool:
        """Return True if feature is enabled in a validated license."""
        return (
            validated.get("valid", False)
            and feature in validated.get("features", [])
        )

    # ─────────────────────────────
    # Storage
    # ─────────────────────────────
    @staticmethod
    def save_license(key: str, path: Optional[str] = None) -> None:
        """Save license to a JSON file."""
        path = Path(path) if path else DEFAULT_LICENSE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump({"key": key, "saved_at": str(date.today())}, f, indent=2)

    @staticmethod
    def load_license(path: Optional[str] = None) -> str:
        """Load license from a JSON file."""
        path = Path(path) if path else DEFAULT_LICENSE_PATH
        with open(path) as f:
            return json.load(f)["key"]

    @staticmethod
    def default_path() -> Path:
        """Default license file path: ~/.modelfungible/license.json"""
        return DEFAULT_LICENSE_PATH


# ─────────────────────────────────────────────────────────────────
# Server-side key generation utilities
# ─────────────────────────────────────────────────────────────────
class LicenseGenerator:
    """
    Server-side utility to generate and manage license keys.
    In production this would run on your server, not the client.
    """

    def __init__(self, server_secret: str):
        self.server_secret = server_secret

    def generate(
        self,
        customer_id: str,
        expiry: str,
        seats: int = 10,
        features: Optional[list] = None,
        plan: str = "enterprise",
    ) -> str:
        """Generate a license key for a customer."""
        return LicenseKey.generate(
            payload={
                "customer_id": customer_id,
                "expiry": expiry,
                "seats": seats,
                "features": features or ["enterprise_adapters", "strategy_ui", "api"],
                "plan": plan,
            },
            server_secret=self.server_secret,
        )

    def revoke(self, key: str) -> Dict[str, Any]:
        """
        In a full system, revoked keys would be stored in a blocklist.
        This is a placeholder for future server-side integration.
        """
        return {"revoked": False, "reason": "Not implemented — add server-side blocklist"}


__all__ = ["LicenseKey", "LicenseGenerator"]
