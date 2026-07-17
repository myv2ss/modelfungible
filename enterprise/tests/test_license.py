# Copyright (c) 2026 Saabu / OpenClaw. All rights reserved.
# BUSL-1.0 License — see LICENSE file for details.

"""
Tests for license key system.

Tests:
- Key generation (server-side)
- Key validation (client-side, offline)
- Expiry checking
- Feature flags
- Tamper detection
- Storage (save/load)
"""
import pytest, json, tempfile, os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


SERVER_SECRET = "mf_license_server_secret_2026"

PAYLOAD = {
    "customer_id": "cust_abc123",
    "expiry": "2027-07-17",
    "seats": 10,
    "features": ["enterprise_adapters", "strategy_ui", "api"],
    "plan": "enterprise",
}


# ─────────────────────────────────────────────────────────────────
# Tests: Generation
# ─────────────────────────────────────────────────────────────────
class TestGeneration:
    def test_key_starts_with_MODEL(self):
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate(PAYLOAD, SERVER_SECRET)
        assert key.startswith("MODEL-"), f"Key doesn't start with MODEL-: {key[:20]}"

    def test_key_has_two_parts(self):
        """Key format: MODEL-{payload}.{sig}"""
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate(PAYLOAD, SERVER_SECRET)
        rest = key[len("MODEL-"):]
        parts = rest.rsplit(".", 1)
        assert len(parts) == 2, f"Key should have exactly one dot: {key}"

    def test_deterministic(self):
        """Same payload + secret → same key."""
        from modelfungible.enterprise.license import LicenseKey
        k1 = LicenseKey.generate(PAYLOAD, SERVER_SECRET)
        k2 = LicenseKey.generate(PAYLOAD, SERVER_SECRET)
        assert k1 == k2

    def test_different_payloads_different_keys(self):
        from modelfungible.enterprise.license import LicenseKey
        k1 = LicenseKey.generate({**PAYLOAD, "customer_id": "cust_1"}, SERVER_SECRET)
        k2 = LicenseKey.generate({**PAYLOAD, "customer_id": "cust_2"}, SERVER_SECRET)
        assert k1 != k2


# ─────────────────────────────────────────────────────────────────
# Tests: Validation
# ─────────────────────────────────────────────────────────────────
class TestValidation:
    def test_valid_key_is_valid(self):
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate(PAYLOAD, SERVER_SECRET)
        result = LicenseKey.validate(key, SERVER_SECRET)
        assert result["valid"] is True

    def test_valid_key_returns_all_fields(self):
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate(PAYLOAD, SERVER_SECRET)
        result = LicenseKey.validate(key, SERVER_SECRET)
        assert result["customer_id"] == "cust_abc123"
        assert result["expiry"] == "2027-07-17"
        assert result["seats"] == 10
        assert result["plan"] == "enterprise"
        assert "enterprise_adapters" in result["features"]

    def test_wrong_secret_fails(self):
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate(PAYLOAD, SERVER_SECRET)
        result = LicenseKey.validate(key, "wrong_secret")
        assert result["valid"] is False
        assert "signature" in result["error"].lower()

    def test_tampered_payload_fails(self):
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate(PAYLOAD, SERVER_SECRET)
        # Change one character in the payload part
        tampered = key[:-5] + ("X" if key[-5] != "X" else "Y")
        result = LicenseKey.validate(tampered, SERVER_SECRET)
        assert result["valid"] is False

    def test_malformed_key_fails(self):
        from modelfungible.enterprise.license import LicenseKey
        for bad in ["", "MODEL-", "MODEL-X", "random-key"]:
            result = LicenseKey.validate(bad, SERVER_SECRET)
            assert result["valid"] is False, f"Should fail for: {bad}"

    def test_empty_string_fails(self):
        from modelfungible.enterprise.license import LicenseKey
        result = LicenseKey.validate("", SERVER_SECRET)
        assert result["valid"] is False


# ─────────────────────────────────────────────────────────────────
# Tests: Expiry
# ─────────────────────────────────────────────────────────────────
class TestExpiry:
    def test_expired_key_fails(self):
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate({**PAYLOAD, "expiry": "2020-01-01"}, SERVER_SECRET)
        result = LicenseKey.validate(key, SERVER_SECRET)
        assert result["valid"] is False
        assert "expired" in result["error"].lower()

    def test_future_expiry_is_valid(self):
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate({**PAYLOAD, "expiry": "2030-12-31"}, SERVER_SECRET)
        result = LicenseKey.validate(key, SERVER_SECRET)
        assert result["valid"] is True

    def test_expires_today_is_valid(self):
        from datetime import date
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate({**PAYLOAD, "expiry": str(date.today())}, SERVER_SECRET)
        result = LicenseKey.validate(key, SERVER_SECRET)
        assert result["valid"] is True

    def test_expires_tomorrow_is_valid(self):
        from datetime import date, timedelta
        from modelfungible.enterprise.license import LicenseKey
        tomorrow = str(date.today() + timedelta(days=1))
        key = LicenseKey.generate({**PAYLOAD, "expiry": tomorrow}, SERVER_SECRET)
        result = LicenseKey.validate(key, SERVER_SECRET)
        assert result["valid"] is True


# ─────────────────────────────────────────────────────────────────
# Tests: Features
# ─────────────────────────────────────────────────────────────────
class TestFeatures:
    def test_check_feature_enabled(self):
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate(PAYLOAD, SERVER_SECRET)
        result = LicenseKey.validate(key, SERVER_SECRET)
        assert LicenseKey.check_feature(result, "enterprise_adapters") is True
        assert LicenseKey.check_feature(result, "strategy_ui") is True
        assert LicenseKey.check_feature(result, "nonexistent") is False

    def test_check_feature_on_invalid_license(self):
        from modelfungible.enterprise.license import LicenseKey
        bad = {"valid": False, "error": "bad key"}
        assert LicenseKey.check_feature(bad, "anything") is False


# ─────────────────────────────────────────────────────────────────
# Tests: Storage
# ─────────────────────────────────────────────────────────────────
class TestStorage:
    def test_save_and_load(self):
        from modelfungible.enterprise.license import LicenseKey
        key = LicenseKey.generate(PAYLOAD, SERVER_SECRET)

        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)

        try:
            LicenseKey.save_license(key, path)
            loaded = LicenseKey.load_license(path)
            assert loaded == key

            # Loaded key should also validate
            result = LicenseKey.validate(loaded, SERVER_SECRET)
            assert result["valid"] is True
        finally:
            os.unlink(path)

    def test_load_nonexistent_raises(self):
        from modelfungible.enterprise.license import LicenseKey
        with pytest.raises(FileNotFoundError):
            LicenseKey.load_license("/nonexistent/path/xyz.json")

    def test_default_path_has_modelfungible_dir(self):
        from modelfungible.enterprise.license import LicenseKey
        p = LicenseKey.default_path()
        assert ".modelfungible" in str(p)
        assert "license.json" in str(p)


# ─────────────────────────────────────────────────────────────────
# Tests: LicenseGenerator
# ─────────────────────────────────────────────────────────────────
class TestLicenseGenerator:
    def test_generator_produces_valid_key(self):
        from modelfungible.enterprise.license import LicenseGenerator, LicenseKey
        gen = LicenseGenerator(SERVER_SECRET)
        key = gen.generate(customer_id="cust_xyz", expiry="2027-12-31", seats=5)
        result = LicenseKey.validate(key, SERVER_SECRET)
        assert result["valid"] is True
        assert result["customer_id"] == "cust_xyz"
        assert result["seats"] == 5

    def test_generator_custom_features(self):
        from modelfungible.enterprise.license import LicenseGenerator, LicenseKey
        gen = LicenseGenerator(SERVER_SECRET)
        key = gen.generate(
            customer_id="cust_xyz",
            expiry="2027-12-31",
            seats=1,
            features=["api"],
            plan="starter",
        )
        result = LicenseKey.validate(key, SERVER_SECRET)
        assert result["features"] == ["api"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
