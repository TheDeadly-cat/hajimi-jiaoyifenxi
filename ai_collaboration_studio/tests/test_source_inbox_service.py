from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.source_inbox_service import SourceInboxError, SourceInboxService  # noqa: E402
from backend.store import StudioStore  # noqa: E402
from tests.test_source_inbox_contracts import RECEIVED_AT_MS, _packet  # noqa: E402


class SourceInboxServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-source-inbox-")
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        self.room = self.store.create_room(
            "CI 复核房间",
            "复核公开 GitHub/CI 事件，不自动启动任何轮次。",
            capability_pack_ids=[],
        )["room"]
        self.service = SourceInboxService(
            self.store,
            clock=lambda: RECEIVED_AT_MS / 1000,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def raw_packet(packet=None) -> str:
        return json.dumps(packet or _packet(), ensure_ascii=False)

    @staticmethod
    def packet_variant(
        suffix: str,
        *,
        source_channel: str = "chatgpt_scheduled_task",
        source_key: str = "github_ci_watch",
    ) -> dict:
        packet = copy.deepcopy(_packet())
        packet["source_channel"] = source_channel
        packet["source_key"] = source_key
        packet["external_run_id"] = f"source-run-{suffix}"
        packet["generation"]["channel"] = source_channel
        packet["generation"]["correlated_output"] = (
            source_channel == "chatgpt_scheduled_task"
        )
        packet["items"][0]["external_item_id"] = f"event-{suffix}"
        packet["items"][0]["headline"] = f"Source event {suffix}"
        return packet

    def test_import_is_persisted_idempotent_and_drift_conflicts(self) -> None:
        first = self.service.import_packet(self.raw_packet())
        replay = self.service.import_packet(
            json.dumps(_packet(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(first["import_id"], replay["import_id"])
        self.assertEqual(first["created_item_count"], 1)
        self.assertEqual(first["duplicate_item_count"], 0)
        self.assertEqual(first["items"][0]["state"], "AWAITING_USER")
        self.assertEqual(
            first["items"][0]["external_claims_verification"],
            "external_unverified",
        )
        self.assertEqual(first["items"][0]["safety"]["provider_calls_performed"], 0)

        listing = self.service.list_items(state="AWAITING_USER")
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["counts"], {"AWAITING_USER": 1})

        drift = _packet()
        drift["items"][0]["summary"] = "Same run identifier, different semantic content."
        with self.assertRaises(SourceInboxError) as captured:
            self.service.import_packet(self.raw_packet(drift))
        self.assertEqual(captured.exception.code, "SOURCE_IMPORT_KEY_CONFLICT")

        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_imports").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_items").fetchone()[0],
                1,
            )

    def test_list_source_unread_counts_and_tiers_are_additive_and_limit_independent(self) -> None:
        manual = self.service.import_packet(
            self.raw_packet(self.packet_variant("manual"))
        )["items"][0]
        official_packet = self.packet_variant(
            "official",
            source_channel="official_source_monitor",
            source_key="sec_filings",
        )
        official = self.service.import_packet(
            self.raw_packet(official_packet),
            actor="source_monitoring_worker",
        )["items"][0]
        readonly_packet = self.packet_variant(
            "readonly-market",
            source_channel="futu_anomaly_monitor",
            source_key="futu_anomaly_signals",
        )
        self.service.import_packet(
            self.raw_packet(readonly_packet),
            actor="source_monitoring_worker",
        )

        listing = self.service.list_items(limit=1)
        self.assertEqual(listing["version"], "source_inbox_list_v1")
        self.assertEqual(len(listing["items"]), 1)
        self.assertEqual(listing["total_count"], 3)
        self.assertEqual(listing["unread_count"], 3)
        self.assertEqual(listing["matched_count"], 3)
        self.assertEqual(listing["counts"], {"AWAITING_USER": 3})
        self.assertEqual(
            {
                facet["source"]: (
                    facet["source_tier"],
                    facet["count"],
                    facet["unread_count"],
                )
                for facet in listing["source_facets"]
            },
            {
                "chatgpt_scheduled_task:github_ci_watch": (
                    "external_manual",
                    1,
                    1,
                ),
                "official_source_monitor:sec_filings": (
                    "official_source",
                    1,
                    1,
                ),
                "futu_anomaly_monitor:futu_anomaly_signals": (
                    "readonly_market",
                    1,
                    1,
                ),
            },
        )

        official_listing = self.service.list_items(
            source="official_source_monitor:sec_filings",
            unread="true",
        )
        self.assertEqual(official_listing["matched_count"], 1)
        self.assertEqual(official_listing["items"][0]["id"], official["id"])
        self.assertEqual(official_listing["items"][0]["source_tier"], "official_source")
        self.assertEqual(
            official_listing["items"][0]["external_claims_verification"],
            "external_unverified",
        )
        no_match = self.service.list_items(
            state="AWAITING_USER",
            query="does-not-exist",
            source="official_source_monitor:sec_filings",
            unread="true",
            limit=1,
        )
        self.assertEqual(no_match["matched_count"], 0)
        self.assertEqual(no_match["items"], [])
        self.assertEqual(no_match["total_count"], 3)
        self.assertEqual(len(no_match["source_facets"]), 3)

        acknowledged = self.service.acknowledge(
            manual["id"],
            expected_state_version=manual["state_version"],
            acknowledgement=True,
        )
        self.assertEqual(acknowledged["state"], "AWAITING_USER")
        acknowledged_listing = self.service.list_items(unread="false")
        self.assertEqual(acknowledged_listing["matched_count"], 1)
        self.assertEqual(acknowledged_listing["items"][0]["id"], manual["id"])
        self.assertEqual(acknowledged_listing["unread_count"], 2)
        self.assertEqual(acknowledged_listing["counts"], {"AWAITING_USER": 3})

        for kwargs in (
            {"source": "official_source_monitor"},
            {"source": "Official:sec_filings"},
            {"source": "official_source_monitor:sec filings"},
            {"source": "official_source_monitor:sec:filings"},
            {"source": "official_source_monitor:SecFilings"},
            {"source": " official_source_monitor:sec_filings"},
            {"source": "official_source_monitor:sec_filings "},
            {"source": ""},
            {"unread": "TRUE"},
            {"unread": ""},
            {"unread": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(SourceInboxError):
                self.service.list_items(**kwargs)

    def test_notification_feed_baselines_pages_without_duplicates_and_tracks_unread(self) -> None:
        baseline = self.service.list_notifications(limit=1)
        self.assertTrue(baseline["baseline"])
        self.assertEqual(baseline["notifications"], [])
        self.assertEqual(baseline["unread_count"], 0)

        first = self.service.import_packet(
            self.raw_packet(self.packet_variant("notification-a"))
        )["items"][0]
        second = self.service.import_packet(
            self.raw_packet(self.packet_variant("notification-b"))
        )["items"][0]

        page_one = self.service.list_notifications(
            after=baseline["cursor"],
            limit=1,
        )
        self.assertFalse(page_one["baseline"])
        self.assertTrue(page_one["has_more"])
        self.assertEqual(len(page_one["notifications"]), 1)
        page_two = self.service.list_notifications(
            after=page_one["cursor"],
            limit=1,
        )
        self.assertFalse(page_two["has_more"])
        self.assertEqual(len(page_two["notifications"]), 1)
        delivered_ids = {
            page_one["notifications"][0]["id"],
            page_two["notifications"][0]["id"],
        }
        self.assertEqual(delivered_ids, {first["id"], second["id"]})
        self.assertTrue(all(
            notification["external_claims_verification"] == "external_unverified"
            and notification["safety"]["execution_authorization"] is False
            for notification in [
                *page_one["notifications"],
                *page_two["notifications"],
            ]
        ))

        empty = self.service.list_notifications(after=page_two["cursor"], limit=1)
        self.assertEqual(empty["notifications"], [])
        self.assertEqual(empty["cursor"], page_two["cursor"])
        self.assertEqual(empty["unread_count"], 2)

        self.service.acknowledge(
            first["id"],
            expected_state_version=first["state_version"],
            acknowledgement=True,
        )
        after_ack = self.service.list_notifications(after=empty["cursor"], limit=1)
        self.assertEqual(after_ack["notifications"], [])
        self.assertEqual(after_ack["unread_count"], 1)
        detail = self.service.get_item(first["id"])
        assert detail is not None
        self.assertEqual(detail["state"], "AWAITING_USER")

        tampered_cursor = page_two["cursor"][:-1] + (
            "A" if page_two["cursor"][-1] != "A" else "B"
        )
        with self.assertRaises(SourceInboxError) as cursor_error:
            self.service.list_notifications(after=tampered_cursor)
        self.assertEqual(cursor_error.exception.code, "SOURCE_INBOX_CURSOR_INVALID")

    def test_notification_baseline_never_replays_existing_history(self) -> None:
        self.service.import_packet(
            self.raw_packet(self.packet_variant("historical-before-baseline"))
        )
        baseline = self.service.list_notifications()
        self.assertTrue(baseline["baseline"])
        self.assertEqual(baseline["notifications"], [])
        self.assertEqual(baseline["unread_count"], 1)
        replay = self.service.list_notifications(after=baseline["cursor"])
        self.assertEqual(replay["notifications"], [])
        self.assertEqual(replay["cursor"], baseline["cursor"])

    def test_notification_cursor_uses_insertion_order_for_same_millisecond_ids(self) -> None:
        uuid_values = [
            SimpleNamespace(hex=value * 32)
            for value in ("1", "f", "2", "3", "4", "5", "0", "6", "7", "8")
        ]
        with patch(
            "backend.source_inbox_service.uuid.uuid4",
            side_effect=uuid_values,
        ):
            first = self.service.import_packet(
                self.raw_packet(self.packet_variant("same-ms-a"))
            )["items"][0]
            baseline = self.service.list_notifications()
            second = self.service.import_packet(
                self.raw_packet(self.packet_variant("same-ms-b"))
            )["items"][0]

        self.assertEqual(first["created_at"], second["created_at"])
        self.assertLess(second["id"], first["id"])
        delivered = self.service.list_notifications(after=baseline["cursor"])
        self.assertEqual(
            [notification["id"] for notification in delivered["notifications"]],
            [second["id"]],
        )
        self.assertFalse(delivered["has_more"])
        replay = self.service.list_notifications(after=delivered["cursor"])
        self.assertEqual(replay["notifications"], [])

    def test_manual_import_cannot_claim_reserved_monitoring_channels(self) -> None:
        for source_channel, source_key in (
            ("official_source_monitor", "sec_filings"),
            ("futu_anomaly_monitor", "futu_anomaly_signals"),
        ):
            with self.subTest(source_channel=source_channel):
                packet = copy.deepcopy(_packet())
                packet["source_channel"] = source_channel
                packet["source_key"] = source_key
                packet["external_run_id"] = f"manual-forgery-{source_key}"
                packet["generation"]["channel"] = source_channel
                packet["generation"]["correlated_output"] = False
                with self.assertRaises(SourceInboxError) as captured:
                    self.service.import_packet(self.raw_packet(packet))
                self.assertEqual(
                    captured.exception.code,
                    "SOURCE_INBOX_MONITORING_CHANNEL_UNAUTHORIZED",
                )
                self.assertEqual(captured.exception.status, 403)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_imports").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_items").fetchone()[0],
                0,
            )

    def test_import_first_write_is_final_and_fingerprint_collision_rolls_back(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER source_import_must_be_final
                   BEFORE INSERT ON source_inbox_imports
                   WHEN NEW.receipt_json='{}'
                     OR NEW.receipt_sha256=printf('%064d',0)
                     OR NEW.status='RECEIVED'
                   BEGIN
                     SELECT RAISE(ABORT,'source import placeholder forbidden');
                   END"""
            )
        self.service.import_packet(self.raw_packet())
        collision = copy.deepcopy(_packet())
        collision["external_run_id"] = "2026-08-28T13:06:00Z-github-ci-watch"
        collision["items"][0]["summary"] = "Same identity, changed full item content."
        with self.assertRaises(SourceInboxError) as captured:
            self.service.import_packet(self.raw_packet(collision))
        self.assertEqual(captured.exception.code, "SOURCE_IMPORT_FINGERPRINT_CONFLICT")
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_imports").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_import_items").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_items").fetchone()[0], 1)

    def test_duplicate_fingerprint_across_new_run_is_linked_not_recreated(self) -> None:
        self.service.import_packet(self.raw_packet())
        second_packet = copy.deepcopy(_packet())
        second_packet["external_run_id"] = "2026-08-28T13:04:00Z-github-ci-watch"
        result = self.service.import_packet(self.raw_packet(second_packet))

        self.assertEqual(result["status"], "DUPLICATE")
        self.assertEqual(result["created_item_count"], 0)
        self.assertEqual(result["duplicate_item_count"], 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_items").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_import_items").fetchone()[0],
                2,
            )

    def test_acknowledge_attach_and_round_draft_remain_user_owned_and_nonexecuting(self) -> None:
        imported = self.service.import_packet(self.raw_packet())
        item = imported["items"][0]

        acknowledged = self.service.acknowledge(
            item["id"],
            expected_state_version=item["state_version"],
            acknowledgement=True,
        )
        self.assertTrue(acknowledged["acknowledged"])
        self.assertEqual(acknowledged["state"], "AWAITING_USER")
        ack_event = acknowledged["events"][-1]
        self.assertFalse(ack_event["payload"]["fact_confirmation"])
        self.assertFalse(ack_event["payload"]["execution_authorization"])

        attached = self.service.attach_to_room(
            item["id"],
            room_id=self.room["id"],
            expected_state_version=acknowledged["state_version"],
        )
        attached_item = attached["item"]
        self.assertEqual(attached_item["state"], "ATTACHED")
        self.assertEqual(attached["attachment"]["room_id"], self.room["id"])
        material = self.store.get_material(
            self.room["id"],
            attached["attachment"]["material_id"],
        )
        self.assertIsNotNone(material)
        self.assertIn("外部声明尚未核验", material["content"])

        drafted = self.service.create_round_draft(
            item["id"],
            room_id=self.room["id"],
            expected_state_version=attached_item["state_version"],
        )
        draft = drafted["round_draft"]
        self.assertEqual(drafted["item"]["state"], "ROUND_DRAFTED")
        self.assertFalse(draft["formal_round_created"])
        self.assertEqual(draft["provider_calls_performed"], 0)
        self.assertEqual(draft["market_calls_performed"], 0)
        self.assertTrue(draft["user_confirmation_required_to_launch"])
        self.assertRegex(draft["draft_sha256"], r"^[0-9a-f]{64}$")

        with closing(sqlite3.connect(self.db_path)) as connection:
            round_count = connection.execute(
                "SELECT COUNT(*) FROM rounds WHERE room_id=?",
                (self.room["id"],),
            ).fetchone()[0]
            draft_flags = connection.execute(
                """SELECT formal_round_created,provider_calls_performed,
                          market_calls_performed
                   FROM source_inbox_round_drafts"""
            ).fetchone()
        self.assertEqual(round_count, 0)
        self.assertEqual(tuple(draft_flags), (0, 0, 0))

    def test_attachment_and_draft_fail_closed_before_required_user_steps(self) -> None:
        item = self.service.import_packet(self.raw_packet())["items"][0]
        with self.assertRaises(SourceInboxError) as attach_error:
            self.service.attach_to_room(
                item["id"],
                room_id=self.room["id"],
                expected_state_version=item["state_version"],
            )
        self.assertEqual(
            attach_error.exception.code,
            "SOURCE_INBOX_ACKNOWLEDGEMENT_REQUIRED",
        )

        acknowledged = self.service.acknowledge(
            item["id"],
            expected_state_version=item["state_version"],
            acknowledgement=True,
        )
        with self.assertRaises(SourceInboxError) as draft_error:
            self.service.create_round_draft(
                item["id"],
                room_id=self.room["id"],
                expected_state_version=acknowledged["state_version"],
            )
        self.assertEqual(draft_error.exception.code, "SOURCE_INBOX_ATTACHMENT_REQUIRED")

        with self.assertRaises(SourceInboxError) as stale_error:
            self.service.acknowledge(
                item["id"],
                expected_state_version=item["state_version"],
                acknowledgement=True,
            )
        self.assertEqual(stale_error.exception.code, "SOURCE_INBOX_STATE_CONFLICT")

        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_attachments").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM source_inbox_round_drafts").fetchone()[0],
                0,
            )

    def test_event_chain_tamper_is_detected(self) -> None:
        item = self.service.import_packet(self.raw_packet())["items"][0]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE source_inbox_state_events SET payload_json='{}'
                   WHERE item_id=? AND sequence_no=2""",
                (item["id"],),
            )
        with self.assertRaises(SourceInboxError) as captured:
            self.service.get_item(item["id"])
        self.assertEqual(captured.exception.code, "SOURCE_INBOX_RECORD_CORRUPT")

    def test_mutations_reject_receipt_item_and_ack_mirror_tamper_before_writes(self) -> None:
        cases = (
            (
                "receipt",
                "UPDATE source_inbox_imports SET receipt_json='{}'",
                1,
            ),
            (
                "item mirror",
                "UPDATE source_inbox_items SET summary='tampered mirror'",
                1,
            ),
            (
                "ack mirror",
                """UPDATE source_inbox_items
                   SET acknowledged_by='local_user',acknowledged_at=1,state_version=2""",
                2,
            ),
        )
        for label, statement, expected_version in cases:
            with self.subTest(label=label):
                temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-source-tamper-")
                try:
                    store = StudioStore(Path(temp_dir.name) / "studio.sqlite3")
                    room = store.create_room("tamper", "tamper", capability_pack_ids=[])["room"]
                    service = SourceInboxService(store, clock=lambda: RECEIVED_AT_MS / 1000)
                    item = service.import_packet(self.raw_packet())["items"][0]
                    with closing(sqlite3.connect(store.path)) as connection, connection:
                        connection.execute(statement)
                    with self.assertRaises(SourceInboxError) as captured:
                        service.attach_to_room(
                            item["id"],
                            room_id=room["id"],
                            expected_state_version=expected_version,
                        )
                    self.assertEqual(captured.exception.code, "SOURCE_INBOX_RECORD_CORRUPT")
                    with closing(sqlite3.connect(store.path)) as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0], 0)
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_attachments").fetchone()[0], 0)
                finally:
                    temp_dir.cleanup()

    def test_attachment_and_room_snapshot_tamper_block_draft_creation(self) -> None:
        for label, statement, expected_code in (
            (
                "attachment hash",
                "UPDATE source_inbox_attachments SET attachment_sha256='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'",
                "SOURCE_INBOX_RECORD_CORRUPT",
            ),
            (
                "room snapshot seal",
                "UPDATE room_versions SET snapshot_sha256='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'",
                "SOURCE_INBOX_ROOM_SNAPSHOT_INVALID",
            ),
        ):
            with self.subTest(label=label):
                temp_dir = tempfile.TemporaryDirectory(prefix="ai-studio-source-binding-")
                try:
                    store = StudioStore(Path(temp_dir.name) / "studio.sqlite3")
                    room = store.create_room("binding", "binding", capability_pack_ids=[])["room"]
                    service = SourceInboxService(store, clock=lambda: RECEIVED_AT_MS / 1000)
                    item = service.import_packet(self.raw_packet())["items"][0]
                    ack = service.acknowledge(
                        item["id"],
                        expected_state_version=item["state_version"],
                        acknowledgement=True,
                    )
                    attached = service.attach_to_room(
                        item["id"],
                        room_id=room["id"],
                        expected_state_version=ack["state_version"],
                    )
                    with closing(sqlite3.connect(store.path)) as connection, connection:
                        connection.execute(statement)
                    with self.assertRaises(SourceInboxError) as captured:
                        service.create_round_draft(
                            item["id"],
                            room_id=room["id"],
                            expected_state_version=attached["item"]["state_version"],
                        )
                    self.assertEqual(captured.exception.code, expected_code)
                    with closing(sqlite3.connect(store.path)) as connection:
                        self.assertEqual(
                            connection.execute("SELECT COUNT(*) FROM source_inbox_round_drafts").fetchone()[0],
                            0,
                        )
                finally:
                    temp_dir.cleanup()

    def test_round_draft_replay_binds_objective_and_original_state_version(self) -> None:
        item = self.service.import_packet(self.raw_packet())["items"][0]
        ack = self.service.acknowledge(
            item["id"],
            expected_state_version=item["state_version"],
            acknowledgement=True,
        )
        attached = self.service.attach_to_room(
            item["id"],
            room_id=self.room["id"],
            expected_state_version=ack["state_version"],
        )
        request_version = attached["item"]["state_version"]
        first = self.service.create_round_draft(
            item["id"],
            room_id=self.room["id"],
            expected_state_version=request_version,
            objective="核对 CI 失败的事实与反证。",
        )
        replay = self.service.create_round_draft(
            item["id"],
            room_id=self.room["id"],
            expected_state_version=request_version,
            objective="核对 CI 失败的事实与反证。",
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["round_draft"]["id"], first["round_draft"]["id"])
        self.assertEqual(replay["round_draft"]["request_state_version"], request_version)
        for objective, state_version in (
            ("改为核对其他目标。", request_version),
            ("核对 CI 失败的事实与反证。", request_version + 1),
        ):
            with self.subTest(objective=objective, state_version=state_version):
                with self.assertRaises(SourceInboxError) as captured:
                    self.service.create_round_draft(
                        item["id"],
                        room_id=self.room["id"],
                        expected_state_version=state_version,
                        objective=objective,
                    )
                self.assertEqual(captured.exception.code, "SOURCE_INBOX_DRAFT_CONFLICT")
        with self.assertRaises(SourceInboxError) as captured:
            self.service.create_round_draft(
                item["id"],
                room_id=self.room["id"],
                expected_state_version=request_version,
                objective={"not": "a string"},
            )
        self.assertEqual(captured.exception.code, "SOURCE_INBOX_REQUEST_INVALID")
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_inbox_round_drafts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
