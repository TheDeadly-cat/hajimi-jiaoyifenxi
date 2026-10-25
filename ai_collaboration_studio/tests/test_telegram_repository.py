from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.telegram_intelligence.events import EventClusterer, EventObservation
from backend.telegram_intelligence.models import DeletedMessageEvent, SourceMessage
from backend.telegram_intelligence.repository import (
    SCHEMA_VERSION,
    TelegramRepository,
    TelegramStorageCapacityError,
)


BASE_TIME = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def message(
    message_id: int = 1,
    *,
    chat_id: int = -1001,
    text: str = "NVDA launches a new Blackwell GPU",
    timestamp: datetime = BASE_TIME,
) -> SourceMessage:
    return SourceMessage(
        source="telegram",
        chat_id=chat_id,
        chat_title="Market Wire",
        chat_type="channel",
        message_id=message_id,
        sender_id=7,
        sender_name="Reporter",
        timestamp=timestamp,
        edit_timestamp=None,
        text=text,
        caption="",
        urls=("https://example.com/news?utm_source=tg",),
        entities=(),
        reply_to_message_id=None,
        forward_origin=None,
        media_type="",
        media_file_id="",
        telegram_link="https://t.me/example/1",
        raw_language="en",
        is_edited=False,
        is_forwarded=False,
        ingested_at=timestamp + timedelta(seconds=1),
    )


class TelegramRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="telegram-repository-")
        self.current_time = [BASE_TIME]
        self.repo = TelegramRepository(
            Path(self.temp.name) / "telegram-intelligence.sqlite3",
            clock=lambda: self.current_time[0],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema_is_standalone_and_enables_wal_and_foreign_keys(self) -> None:
        self.assertEqual(self.repo.schema_version, SCHEMA_VERSION)
        self.assertEqual(self.repo.get_system_state("schema_version"), SCHEMA_VERSION)
        with closing(sqlite3.connect(self.repo.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertTrue(
            {
                "telegram_chats",
                "telegram_messages",
                "events",
                "event_messages",
                "analysis_results",
                "alerts",
                "ai_usage",
                "retry_jobs",
                "system_state",
                "ai_result_cache",
            }.issubset(tables)
        )
        with self.assertRaisesRegex(ValueError, "formal Studio"):
            TelegramRepository(Path(self.temp.name) / "collaboration_studio.sqlite3")

    def test_storage_watermark_reports_sidecars_and_rejects_new_admission(self) -> None:
        base_usage = self.repo.storage_usage()
        self.assertEqual(base_usage["storage_status"], "ready")
        self.assertEqual(
            base_usage["database_bytes"],
            base_usage["database_file_bytes"]
            + base_usage["wal_file_bytes"]
            + base_usage["shm_file_bytes"],
        )

        self.repo.set_storage_limit(base_usage["database_bytes"] + 1)
        self.assertEqual(self.repo.storage_usage()["storage_status"], "warning")
        with self.assertRaisesRegex(
            TelegramStorageCapacityError,
            "capacity limit reached",
        ):
            self.repo.store_message(message())
        with self.assertRaises(TelegramStorageCapacityError):
            self.repo.store_message_and_enqueue(
                message(),
                kind="analyze_message",
                dedupe_key_prefix="capacity-test",
            )
        with self.assertRaises(TelegramStorageCapacityError):
            self.repo.enqueue_job("analyze_message", {"message_row_id": 1})

        self.repo.set_storage_limit(base_usage["database_bytes"])
        self.assertEqual(self.repo.storage_usage()["storage_status"], "full")

        with closing(sqlite3.connect(self.repo.path)) as connection:
            message_count = connection.execute(
                "SELECT COUNT(*) FROM telegram_messages"
            ).fetchone()[0]
            job_count = connection.execute(
                "SELECT COUNT(*) FROM retry_jobs"
            ).fetchone()[0]
        self.assertEqual(message_count, 0)
        self.assertEqual(job_count, 0)

    def test_message_insert_is_idempotent_and_edit_delete_are_auditable(self) -> None:
        self.repo.upsert_chat(-1001, title="Market Wire", priority="high", trust_score=90)
        original = message()
        first = self.repo.store_message(original)
        replay = self.repo.store_message(replace(original, ingested_at=original.ingested_at + timedelta(seconds=4)))
        self.assertTrue(first["created"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(first["id"], replay["id"])

        edited = replace(
            original,
            text="NVDA launches a revised Blackwell GPU platform",
            is_edited=True,
            edit_timestamp=BASE_TIME + timedelta(minutes=2),
            ingested_at=BASE_TIME + timedelta(minutes=2, seconds=1),
        )
        changed = self.repo.apply_edit(edited)
        self.assertTrue(changed["updated"])
        self.assertEqual(changed["record_version"], 2)
        self.assertEqual(len(self.repo.message_revisions(first["id"])), 1)

        deleted = self.repo.mark_deleted(
            DeletedMessageEvent(
                source="telegram",
                chat_id=-1001,
                message_ids=(1, 999),
                is_permanent=True,
                from_cache=False,
                received_at=BASE_TIME + timedelta(minutes=3),
            )
        )
        self.assertEqual(deleted["updated_count"], 1)
        self.assertEqual(deleted["missing_message_ids"], (999,))
        stored = self.repo.get_message("telegram", -1001, 1)
        self.assertIsNotNone(stored)
        self.assertTrue(stored["is_deleted"])
        self.assertIn("revised Blackwell", stored["text"])

    def test_persistent_job_lease_backoff_dead_letter_and_health_counts(self) -> None:
        queued = self.repo.enqueue_job(
            "analyze",
            {"message_row_id": 1},
            dedupe_key="message-1",
            max_attempts=2,
        )
        replay = self.repo.enqueue_job(
            "analyze",
            {"message_row_id": 1},
            dedupe_key="message-1",
            max_attempts=2,
        )
        self.assertFalse(queued["idempotent"])
        self.assertTrue(replay["idempotent"])
        claim = self.repo.claim_job("worker-a", lease_seconds=30)
        self.assertIsNotNone(claim)
        retry = self.repo.fail_job(
            claim["id"],
            claim["lease_token"],
            error_code="timeout",
            base_backoff_seconds=5,
            now=BASE_TIME,
        )
        self.assertEqual(retry["status"], "RETRY")
        self.assertIsNone(self.repo.claim_job("worker-a", now=BASE_TIME + timedelta(seconds=4)))
        second = self.repo.claim_job("worker-a", now=BASE_TIME + timedelta(seconds=5))
        self.assertIsNotNone(second)
        dead = self.repo.fail_job(
            second["id"],
            second["lease_token"],
            error_code="http_status",
            now=BASE_TIME + timedelta(seconds=5),
        )
        self.assertEqual(dead["status"], "DEAD")
        self.assertEqual(self.repo.job_counts()["DEAD"], 1)
        self.assertEqual(self.repo.status_counts()["jobs"]["DEAD"], 1)

    def test_events_alerts_cache_usage_and_bounded_read_helpers(self) -> None:
        first = self.repo.store_message(message())
        observation = EventObservation.from_message(
            message(),
            tickers=("NVDA",),
            keywords=("Blackwell", "GPU"),
            category="AI",
            trust_score=90,
            evidence_status="confirmed",
        )
        event = EventClusterer().create(observation)
        saved_event = self.repo.save_event(event)
        self.assertEqual(saved_event["primary_ticker"], "NVDA")
        self.assertTrue(self.repo.link_event_message(event.event_id, first["id"]))
        self.assertFalse(self.repo.link_event_message(event.event_id, first["id"]))

        alert = self.repo.record_alert(
            event.event_id,
            alert_kind="initial",
            status="SENT",
            message_hash="a" * 64,
            cooldown_seconds=300,
        )
        self.assertTrue(self.repo.alert_cooldown_active(event.event_id, at=BASE_TIME + timedelta(seconds=299)))
        self.assertFalse(self.repo.alert_cooldown_active(event.event_id, at=BASE_TIME + timedelta(seconds=301)))
        self.assertEqual(self.repo.latest_alert(event.event_id)["id"], alert["id"])

        self.repo.put_ai_cache(
            "analysis-v1-message-1",
            {"event_type": "product_launch", "importance_score": 80},
            provider="fake",
            model="fake-model",
            expires_at=BASE_TIME + timedelta(hours=1),
        )
        cached = self.repo.get_ai_cache("analysis-v1-message-1", at=BASE_TIME)
        self.assertEqual(cached["result"]["event_type"], "product_launch")
        self.assertEqual(cached["hit_count"], 1)
        self.assertIsNone(
            self.repo.get_ai_cache(
                "analysis-v1-message-1", at=BASE_TIME + timedelta(hours=2)
            )
        )
        analysis = self.repo.save_analysis_result(
            {"event_type": "product_launch"},
            provider="fake",
            model="fake-model",
            message_row_id=first["id"],
            event_id=event.event_id,
            importance_score=80,
            confidence_score=75,
        )
        self.assertEqual(analysis["result"]["event_type"], "product_launch")
        usage = self.repo.record_ai_usage(
            provider="fake",
            model="fake-model",
            input_tokens=100,
            output_tokens=20,
            estimated_cost=0.01,
            latency_ms=50,
            message_row_id=first["id"],
            event_id=event.event_id,
        )
        self.assertEqual(usage["input_tokens"], 100)

        self.assertEqual(
            self.repo.recent_messages(since=BASE_TIME - timedelta(seconds=1), limit=10)[0]["id"],
            first["id"],
        )
        self.assertEqual(
            self.repo.recent_messages(
                since=BASE_TIME - timedelta(seconds=1),
                limit=10,
                exclude_identity=message().identity_key,
            ),
            [],
        )
        self.assertEqual(self.repo.get_event(event.event_id)["event_id"], event.event_id)
        self.assertEqual(
            self.repo.list_events_recent(since=BASE_TIME - timedelta(seconds=1), limit=10)[0]["event_id"],
            event.event_id,
        )
        counts = self.repo.status_counts()
        self.assertEqual(counts["messages"], 1)
        self.assertEqual(counts["events"]["confirmed"], 1)
        self.assertEqual(counts["alerts"]["SENT"], 1)

    def test_analysis_results_persist_and_expose_message_record_version(self) -> None:
        original = message(21)
        first = self.repo.store_message(original)
        observation = EventObservation.from_message(
            original,
            tickers=("NVDA",),
            keywords=("Blackwell", "GPU"),
            category="AI",
            trust_score=90,
        )
        first_event = EventClusterer().create(observation)
        self.repo.save_event(first_event)
        self.repo.link_event_message(first_event.event_id, first["id"])
        first_analysis = self.repo.save_analysis_result(
            {"summary": "original"},
            provider="fake",
            model="fake-model",
            message_row_id=first["id"],
            record_version=first["record_version"],
            event_id=first_event.event_id,
        )

        edited_message = replace(
            original,
            text="NVDA cancels the revised Blackwell GPU platform",
            is_edited=True,
            edit_timestamp=BASE_TIME + timedelta(minutes=1),
            ingested_at=BASE_TIME + timedelta(minutes=1, seconds=1),
        )
        edited = self.repo.apply_edit(edited_message)
        second_observation = EventObservation.from_message(
            edited_message,
            tickers=("NVDA",),
            keywords=("cancels", "GPU"),
            category="AI",
            trust_score=90,
        )
        second_event = EventClusterer().create(second_observation)
        self.repo.save_event(second_event)
        self.repo.link_event_message(second_event.event_id, edited["id"])
        second_analysis = self.repo.save_analysis_result(
            {"summary": "edited"},
            provider="fake",
            model="fake-model",
            message_row_id=edited["id"],
            record_version=edited["record_version"],
            event_id=second_event.event_id,
        )

        self.assertEqual(first_analysis["record_version"], 1)
        self.assertEqual(second_analysis["record_version"], 2)
        with self.repo.transaction(immediate=False) as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(analysis_results)"
                ).fetchall()
            }
            versions = [
                row["record_version"]
                for row in connection.execute(
                    """SELECT record_version FROM analysis_results
                       WHERE message_row_id=? ORDER BY record_version""",
                    (first["id"],),
                ).fetchall()
            ]
        self.assertIn("record_version", columns)
        self.assertEqual(versions, [1, 2])

    def test_event_clustering_state_recovers_from_persisted_links_after_restart(self) -> None:
        self.repo.upsert_chat(-1001, title="Wire A", trust_score=65)
        self.repo.upsert_chat(-1002, title="Wire B", trust_score=92)
        first_message = message(1, chat_id=-1001, text="NVDA launches Blackwell GPU")
        second_message = message(
            2,
            chat_id=-1002,
            text="NVIDIA announces a Blackwell GPU launch",
            timestamp=BASE_TIME + timedelta(minutes=3),
        )
        first_row = self.repo.store_message(first_message)
        second_row = self.repo.store_message(second_message)
        clusterer = EventClusterer()
        first_observation = EventObservation.from_message(
            first_message,
            tickers=("NVDA",),
            keywords=("blackwell", "gpu"),
            category="AI",
            trust_score=65,
        )
        second_observation = EventObservation.from_message(
            second_message,
            tickers=("NVDA",),
            keywords=("blackwell", "gpu"),
            category="AI",
            trust_score=92,
        )
        event = clusterer.merge(clusterer.create(first_observation), second_observation)
        self.repo.save_event(event)
        self.repo.link_event_message(event.event_id, first_row["id"], similarity=1.0)
        self.repo.link_event_message(event.event_id, second_row["id"], similarity=0.9)

        restarted = TelegramRepository(self.repo.path, clock=lambda: self.current_time[0])
        recovered = restarted.load_event_states(
            since=BASE_TIME - timedelta(minutes=1),
            limit=10,
        )
        self.assertEqual(len(recovered), 1)
        state = recovered[0]
        self.assertEqual(state.event_id, event.event_id)
        self.assertEqual(state.first_seen_at, event.first_seen_at)
        self.assertEqual(state.last_seen_at, event.last_seen_at)
        self.assertEqual(state.message_keys, event.message_keys)
        self.assertEqual(set(state.source_keys), set(event.source_keys))
        self.assertEqual(state.message_count, 2)
        self.assertEqual(state.source_count, 2)
        self.assertEqual(state.highest_trust_score, 92)
        self.assertEqual(state.centroid_simhash, event.centroid_simhash)
        self.assertEqual(state.status, "developing")


if __name__ == "__main__":
    unittest.main()
