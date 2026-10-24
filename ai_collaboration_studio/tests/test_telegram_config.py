from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.telegram_intelligence.config import (
    MAX_CONFIG_BYTES,
    ChatConfigError,
    ChatPolicy,
    TelegramCollectorConfig,
    load_chats_config,
    parse_chats_config,
)


VALID_YAML = """\
chats:
  - chat_id: -1001234567890
    enabled: true
    name: "Official # Market"
    category: market
    priority: high
    trust_score: 95
  - chat_id: -1009876543210
    enabled: false
    name: Rumors
    category: crypto
    priority: low
    trust_score: 25
"""


class TelegramConfigTests(unittest.TestCase):
    def test_yaml_loads_strict_policies_and_defaults_to_deny(self) -> None:
        config = parse_chats_config(VALID_YAML)
        self.assertEqual(len(config.chats), 2)
        self.assertTrue(config.is_allowed(-1001234567890))
        self.assertFalse(config.is_allowed(-1009876543210))
        self.assertFalse(config.is_allowed(-1000000000000))
        self.assertEqual(config.policy_for(-1001234567890).trust_score, 95)
        self.assertEqual(config.policy_for(-1001234567890).name, "Official # Market")

    def test_omitted_enabled_is_deny_and_other_defaults_are_bounded(self) -> None:
        config = parse_chats_config("chats:\n  - chat_id: -1001\n")
        policy = config.policy_for(-1001)
        self.assertFalse(policy.enabled)
        self.assertEqual(policy.category, "general")
        self.assertEqual(policy.priority, "medium")
        self.assertEqual(policy.trust_score, 50)

    def test_json_is_accepted(self) -> None:
        raw = json.dumps(
            {
                "chats": [
                    {
                        "chat_id": -1002,
                        "enabled": True,
                        "name": "Desk",
                        "category": "market",
                        "priority": "MEDIUM",
                        "trust_score": 80,
                    }
                ]
            }
        )
        config = parse_chats_config(raw)
        self.assertEqual(config.chats[0].priority, "medium")

    def test_duplicate_keys_ids_and_unknown_fields_fail_closed(self) -> None:
        cases = [
            '{"chats":[],"chats":[]}',
            (
                "chats:\n"
                "  - chat_id: -1001\n"
                "    enabled: true\n"
                "    enabled: false\n"
            ),
            (
                "chats:\n"
                "  - chat_id: -1001\n"
                "  - chat_id: -1001\n"
            ),
            "chats:\n  - chat_id: -1001\n    token: secret\n",
        ]
        for raw in cases:
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(ChatConfigError):
                    parse_chats_config(raw)

    def test_types_trust_and_priority_are_strict(self) -> None:
        bad_records = [
            {"chat_id": True},
            {"chat_id": 0},
            {"chat_id": -1, "enabled": "true"},
            {"chat_id": -1, "trust_score": -1},
            {"chat_id": -1, "trust_score": 101},
            {"chat_id": -1, "trust_score": True},
            {"chat_id": -1, "priority": "urgent"},
        ]
        for record in bad_records:
            with self.subTest(record=record):
                with self.assertRaises(ChatConfigError):
                    parse_chats_config(json.dumps({"chats": [record]}))

    def test_unsupported_yaml_features_tabs_and_oversize_are_rejected(self) -> None:
        cases = [
            "chats:\n\t- chat_id: -1\n",
            "chats:\n  - chat_id: &anchor -1\n",
            "chats:\n  - chat_id: [1]\n",
            "other:\n  - chat_id: -1\n",
            "chats:\n    chat_id: -1\n",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ChatConfigError):
                    parse_chats_config(raw)
        with self.assertRaises(ChatConfigError):
            parse_chats_config(b"x" * (MAX_CONFIG_BYTES + 1))

    def test_load_file_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chats.yaml"
            path.write_text(VALID_YAML, encoding="utf-8")
            self.assertTrue(load_chats_config(path).is_allowed(-1001234567890))
            with self.assertRaises(ChatConfigError):
                load_chats_config(Path(directory) / "missing.yaml")

    def test_dataclass_rejects_duplicate_policies(self) -> None:
        policy = ChatPolicy(chat_id=-1)
        with self.assertRaises(ChatConfigError):
            TelegramCollectorConfig((policy, policy))


if __name__ == "__main__":
    unittest.main()
