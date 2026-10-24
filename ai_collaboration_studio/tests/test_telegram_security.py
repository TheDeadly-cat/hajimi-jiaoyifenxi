from __future__ import annotations

import unittest

from backend.structured_logging import REDACTED, sanitize_fields


class TelegramSecretRedactionTests(unittest.TestCase):
    def test_redacts_all_telegram_authentication_fields(self) -> None:
        sanitized = sanitize_fields(
            {
                "telegram_api_hash": "synthetic-api-hash",
                "telegram_phone": "+10000000000",
                "verification_code": "12345",
                "authentication_code": "67890",
                "two_factor_password": "synthetic-password",
                "tdlib_database_encryption_key": "synthetic-encryption-key",
                "safe_status": "authorizationStateWaitCode",
            }
        )

        self.assertEqual(sanitized["telegram_api_hash"], REDACTED)
        self.assertEqual(sanitized["telegram_phone"], REDACTED)
        self.assertEqual(sanitized["verification_code"], REDACTED)
        self.assertEqual(sanitized["authentication_code"], REDACTED)
        self.assertEqual(sanitized["two_factor_password"], REDACTED)
        self.assertEqual(sanitized["tdlib_database_encryption_key"], REDACTED)
        self.assertEqual(sanitized["safe_status"], "authorizationStateWaitCode")


if __name__ == "__main__":
    unittest.main()
