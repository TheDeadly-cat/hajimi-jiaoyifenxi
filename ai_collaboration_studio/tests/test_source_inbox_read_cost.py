"""Bounded request-local validation with isolated synthetic database fixtures."""
import copy
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
from backend.source_inbox_service import SourceInboxError, SourceInboxService
from backend.store import StudioStore
from tests.test_source_inbox_contracts import RECEIVED_AT_MS, _packet


class SourceInboxReadCostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="inbox-read-cost-")
        self.addCleanup(temporary.cleanup)
        self.store = StudioStore(Path(temporary.name) / "synthetic.sqlite3")
        self.service = SourceInboxService(self.store, clock=lambda: RECEIVED_AT_MS / 1000)
        self.packet = _packet()
        example = self.packet["items"][0]
        self.packet["items"] = []
        for index in range(12):
            item = copy.deepcopy(example)
            item["external_item_id"] = f"read-cost-{index}"
            item["headline"] = f"Synthetic signal {index}"
            self.packet["items"].append(item)
        self.baseline = self.service.list_notifications()["cursor"]

    def import_items(self):
        return self.service.import_packet(json.dumps(self.packet, ensure_ascii=False))

    def test_import_and_idempotent_replay_validate_each_import_once(self):
        original = self.service._verify_import_record
        with patch.object(self.service, "_verify_import_record", wraps=original) as verified:
            result = self.import_items()
            self.assertEqual(verified.call_count, 1)
            self.assertEqual(len(result["items"]), 12)
        with patch.object(self.service, "_verify_import_record", wraps=original) as verified:
            replay = self.import_items()
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(verified.call_count, 1)

    def test_list_validates_shared_import_once_and_still_projects_every_item(self):
        self.import_items()
        with patch.object(self.service, "_verify_import_record",
                          wraps=self.service._verify_import_record) as verified:
            result = self.service.list_items(limit=12)
        self.assertEqual(verified.call_count, 1)
        self.assertEqual(len(result["items"]), 12)
        self.assertEqual(len({item["id"] for item in result["items"]}), 12)
        for item in result["items"]:
            self.assertEqual(item["external_claims_verification"], "external_unverified")
            self.assertEqual(item["safety"]["provider_calls_performed"], 0)
            self.assertEqual(item["safety"]["execution_capability"], "none")

    def test_notification_page_validates_shared_import_once(self):
        self.import_items()
        with patch.object(self.service, "_verify_import_record",
                          wraps=self.service._verify_import_record) as verified:
            feed = self.service.list_notifications(after=self.baseline, limit=12)
        self.assertEqual(verified.call_count, 1)
        self.assertEqual(len(feed["notifications"]), 12)
        self.assertFalse(feed["has_more"])
        self.assertFalse(feed["safety"]["live_trading_allowed"])

    def test_validation_is_not_cached_across_requests(self):
        self.import_items()
        with patch.object(self.service, "_verify_import_record",
                          wraps=self.service._verify_import_record) as verified:
            self.service.list_items(limit=12)
            self.service.list_items(limit=12)
        self.assertEqual(verified.call_count, 2)
        with patch.object(self.service, "_verify_import_record", side_effect=SourceInboxError(
                "Synthetic record corruption", code="SOURCE_INBOX_RECORD_CORRUPT", status=409)):
            with self.assertRaises(SourceInboxError) as caught:
                self.service.list_items(limit=12)
        self.assertEqual(caught.exception.code, "SOURCE_INBOX_RECORD_CORRUPT")

    def test_cached_projection_matches_uncached_integrity_projection(self):
        imported = self.import_items()
        actual = self.service.list_items(limit=12)["items"]
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            expected = [
                self.service._item_projection(connection,
                    self.service._select_item(connection, item["id"]), include_events=False)
                for item in actual
            ]
        self.assertEqual(actual, expected)
        self.assertEqual({item["id"] for item in actual},
                         {item["id"] for item in imported["items"]})


if __name__ == "__main__":
    unittest.main()
