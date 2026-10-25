from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.telegram_intelligence.analyzer import (
    AnalysisResult,
    AnalysisUsage,
    Direction,
    FakeAnalyzer,
    RecommendedAction,
    VerificationStatus,
)
from backend.telegram_intelligence.bot import BotSendResult, FakeBot
from backend.telegram_intelligence.health import HealthState
from backend.telegram_intelligence.models import (
    DeletedMessageEvent,
    ForwardOrigin,
    SourceMessage,
)
from backend.telegram_intelligence.pipeline import PipelineConfig, PipelineService
from backend.telegram_intelligence.repository import TelegramRepository
from backend.telegram_intelligence.rules import RuleEngine
from backend.telegram_intelligence.scoring import PreScoreConfig, PreScoreEngine
from backend.structured_logging import emit_event


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def message(
    message_id: int,
    text: str,
    *,
    chat_id: int = -1001,
    edited: bool = False,
    when: datetime | None = None,
) -> SourceMessage:
    timestamp = when or datetime(2026, 8, 29, 3, 59, tzinfo=timezone.utc)
    return SourceMessage(
        source="telegram",
        chat_id=chat_id,
        chat_title=f"Source {chat_id}",
        chat_type="channel",
        message_id=message_id,
        sender_id=11,
        sender_name="Reporter",
        timestamp=timestamp,
        edit_timestamp=timestamp + timedelta(seconds=10) if edited else None,
        text=text,
        caption="",
        urls=(),
        entities=(),
        reply_to_message_id=None,
        forward_origin=None,
        media_type="",
        media_file_id="",
        telegram_link=f"https://t.me/source/{message_id}",
        raw_language="en",
        is_edited=edited,
        is_forwarded=False,
        ingested_at=timestamp + timedelta(seconds=20),
    )


def important_result() -> AnalysisResult:
    return AnalysisResult(
        event_type="company_announcement",
        category="AI",
        tickers=("NVDA",),
        entities=("NVIDIA",),
        summary="NVIDIA announced a new GPU platform.",
        sentiment="positive",
        direction=Direction.BULLISH,
        importance_score=100,
        confidence_score=100,
        novelty_score=100,
        verification_needed=False,
        verification_status=VerificationStatus.CONFIRMED,
        time_horizon="near_term",
        bull_case="Product demand increases.",
        bear_case="Execution may slip.",
        risk_flags=(),
        recommended_action=RecommendedAction.URGENT_ALERT,
        usage=AnalysisUsage(
            provider="openai",
            model="fixture-model",
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
            request_id="fixture-request",
            latency_ms=5,
            estimated_cost_usd=0.001,
            analyzed_at="2026-08-29T04:00:00Z",
            output_mode="json_schema",
        ),
    )


class FailingBot:
    destination_chat_id = "-100999"
    dry_run = False

    def __init__(self) -> None:
        self.calls = 0

    def send_alert(self, alert):
        self.calls += 1
        return BotSendResult(
            ok=False,
            status="FAILED",
            destination_chat_id=self.destination_chat_id,
            text="",
            error_code="fixture_failure",
        )


class BlockingAnalyzer:
    """Deterministic analyzer gate for edit/delete race regression tests."""

    def __init__(self, result: AnalysisResult) -> None:
        self.result = result
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def analyze(self, message_payload, context):
        self.calls.append((dict(message_payload), dict(context)))
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("fixture analyzer was not released")
        return self.result


class TelegramPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.clock = MutableClock()
        self.repository = TelegramRepository(
            Path(self.temp.name) / "telegram-pipeline.sqlite3",
            clock=self.clock,
        )
        self.rules = RuleEngine(
            {"AI": {"keywords": ["NVIDIA", "GPU"], "tickers": ["NVDA"]}},
            ticker_whitelist={"NVDA": ["NVIDIA"]},
        )
        self.full_pre_score = PreScoreEngine(
            PreScoreConfig(store_only_below=1, full_analysis_above=2)
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def policy(chat_id: int):
        return {
            "enabled": True,
            "name": f"policy-{chat_id}",
            "category": "market",
            "priority": "high",
            "trust_score": 95,
        }

    def service(
        self,
        *,
        analyzer=None,
        bot=None,
        pre_score=None,
        alerts_enabled=True,
        max_attempts=3,
    ) -> PipelineService:
        return PipelineService(
            self.repository,
            self.rules,
            analyzer=analyzer,
            bot=bot,
            policy_resolver=self.policy,
            pre_score=pre_score or self.full_pre_score,
            config=PipelineConfig(
                ai_enabled=analyzer is not None,
                alerts_enabled=alerts_enabled,
                provider="openai",
                model="fixture-model",
                max_attempts=max_attempts,
                base_backoff_seconds=0,
                max_backoff_seconds=0,
                alert_cooldown_seconds=300,
            ),
            clock=self.clock,
            event_emitter=lambda *args, **kwargs: None,
        )

    def test_ingestion_persists_and_enqueues_without_calling_ai_or_bot(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        bot = FakeBot(destination_chat_id="-100999", dry_run=True)
        service = self.service(analyzer=analyzer, bot=bot)

        accepted = service.message_sink(message(1, "NVIDIA announced a new GPU"))

        self.assertEqual(analyzer.calls, [])
        self.assertEqual(bot.sent, [])
        self.assertEqual(accepted["message"]["record_version"], 1)
        self.assertEqual(self.repository.job_counts()["PENDING"], 1)

        processed = service.process_once()
        self.assertEqual(processed["result"]["status"], "processed")
        self.assertEqual(len(analyzer.calls), 1)
        self.assertEqual(bot.sent, [])
        self.assertEqual(self.repository.job_counts()["PENDING"], 1)

        alerted = service.process_once()
        self.assertEqual(alerted["result"]["status"], "WOULD_SEND")
        self.assertEqual(len(bot.sent), 1)
        counts = self.repository.status_counts()
        self.assertEqual(counts["alerts"], {"WOULD_SEND": 1})
        self.assertEqual(counts["jobs"]["COMPLETED"], 2)

    def test_worker_pauses_existing_backlog_at_storage_warning(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        service = self.service(analyzer=analyzer, alerts_enabled=False)
        service.message_sink(message(2, "NVIDIA announced a new GPU"))
        usage = self.repository.storage_usage()
        self.repository.set_storage_limit(usage["database_bytes"] + 1)

        self.assertEqual(self.repository.storage_usage()["storage_status"], "warning")
        self.assertIsNone(service.process_once())
        self.assertEqual(analyzer.calls, [])
        self.assertEqual(self.repository.job_counts()["PENDING"], 1)

        self.repository.set_storage_limit(4 * 1024 * 1024 * 1024)
        self.assertIsNotNone(service.process_once())
        self.assertEqual(len(analyzer.calls), 1)

    def test_stop_reports_inflight_worker_until_external_call_resolves(self) -> None:
        analyzer = BlockingAnalyzer(important_result())
        service = self.service(analyzer=analyzer, alerts_enabled=False)
        service.message_sink(message(3, "NVIDIA announced a new GPU"))
        worker = service.start_worker()
        self.addCleanup(analyzer.release.set)
        self.addCleanup(lambda: service.stop(timeout=2.0))
        self.assertTrue(analyzer.started.wait(timeout=2.0))

        self.assertFalse(service.stop(timeout=0.01))
        self.assertTrue(worker.is_alive())
        analyzer.release.set()
        self.assertTrue(service.stop(timeout=2.0))
        self.assertFalse(worker.is_alive())

    def test_direct_sink_fails_closed_for_missing_or_disabled_policy(self) -> None:
        no_policy = PipelineService(
            self.repository,
            self.rules,
            analyzer=None,
            policy_resolver=lambda chat_id: None,
            config=PipelineConfig(ai_enabled=False, alerts_enabled=False),
            clock=self.clock,
        )
        with self.assertRaisesRegex(PermissionError, "not_whitelisted"):
            no_policy.message_sink(message(99, "NVIDIA GPU"))
        self.assertEqual(self.repository.status_counts()["messages"], 0)

        disabled = PipelineService(
            self.repository,
            self.rules,
            analyzer=None,
            policy_resolver=lambda chat_id: {"enabled": False},
            config=PipelineConfig(ai_enabled=False, alerts_enabled=False),
            clock=self.clock,
        )
        with self.assertRaisesRegex(PermissionError, "whitelist_disabled"):
            disabled.message_sink(message(100, "NVIDIA GPU"))
        self.assertEqual(self.repository.status_counts()["messages"], 0)

    def test_analyzer_failure_retries_to_dead_without_blocking_new_ingestion(self) -> None:
        analyzer = FakeAnalyzer(error=RuntimeError("provider unavailable"))
        service = self.service(
            analyzer=analyzer,
            bot=None,
            alerts_enabled=False,
            max_attempts=2,
        )
        service.message_sink(message(2, "NVIDIA GPU launch"))

        first = service.process_once()
        self.assertEqual(first["job"]["status"], "RETRY")
        service.message_sink(message(3, "NVIDIA GPU supply update"))
        self.assertEqual(self.repository.status_counts()["messages"], 2)

        service.drain(max_jobs=4)
        self.assertEqual(self.repository.job_counts()["DEAD"], 2)
        self.assertEqual(len(analyzer.calls), 4)

    def test_ai_failure_and_dead_letter_degrade_health(self) -> None:
        health = HealthState(wall_clock=self.clock, monotonic_clock=lambda: 0.0)
        health.set_states(
            tdlib_status="ready",
            authorization_state="authorizationStateReady",
            network_state="connectionStateReady",
            worker_status="ready",
            ai_status="ready",
            bot_status="disabled",
        )
        analyzer = FakeAnalyzer(error=RuntimeError("provider unavailable"))
        service = self.service(
            analyzer=analyzer,
            bot=None,
            alerts_enabled=False,
            max_attempts=1,
        )
        service.health = health
        self.assertEqual(health.snapshot()["status"], "ready")

        service.message_sink(message(201, "NVIDIA GPU launch"))
        failed = service.process_once(kinds=("process_message",))

        self.assertEqual(failed["job"]["status"], "DEAD")
        snapshot = health.snapshot()
        self.assertEqual(snapshot["ai_status"], "failed")
        self.assertEqual(snapshot["dead_letter_jobs"], 1)
        self.assertEqual(snapshot["status"], "degraded")

    def test_bot_failure_retries_and_does_not_reopen_completed_analysis(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        bot = FailingBot()
        service = self.service(analyzer=analyzer, bot=bot, max_attempts=2)
        service.message_sink(message(4, "NVIDIA announced a new GPU platform"))
        service.process_once()

        first_alert = service.process_once()
        second_alert = service.process_once()
        self.assertEqual(first_alert["job"]["status"], "RETRY")
        self.assertEqual(second_alert["job"]["status"], "DEAD")
        self.assertEqual(bot.calls, 2)
        self.assertEqual(len(analyzer.calls), 1)
        self.assertEqual(self.repository.job_counts()["COMPLETED"], 1)

    def test_bot_failure_and_dead_letter_degrade_health(self) -> None:
        health = HealthState(wall_clock=self.clock, monotonic_clock=lambda: 0.0)
        health.set_states(
            tdlib_status="ready",
            authorization_state="authorizationStateReady",
            network_state="connectionStateReady",
            worker_status="ready",
            ai_status="ready",
            bot_status="ready",
        )
        analyzer = FakeAnalyzer(important_result())
        bot = FailingBot()
        service = self.service(analyzer=analyzer, bot=bot, max_attempts=1)
        service.health = health
        service.message_sink(message(202, "NVIDIA announced a new GPU platform"))
        analyzed = service.process_once(kinds=("process_message",))
        self.assertEqual(analyzed["job"]["status"], "COMPLETED")
        self.assertEqual(health.snapshot()["status"], "ready")

        failed = service.process_once(kinds=("send_alert",))

        self.assertEqual(failed["job"]["status"], "DEAD")
        snapshot = health.snapshot()
        self.assertEqual(snapshot["bot_status"], "failed")
        self.assertEqual(snapshot["dead_letter_jobs"], 1)
        self.assertEqual(snapshot["status"], "degraded")

    def test_recorded_alert_is_not_resent_when_snapshot_write_retries(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        bot = FakeBot(destination_chat_id="-100999", dry_run=False)
        service = self.service(analyzer=analyzer, bot=bot, max_attempts=2)
        service.message_sink(message(39, "NVIDIA announced a new GPU platform"))
        service.process_once()
        original_set_state = self.repository.set_system_state
        failed = False

        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("fixture snapshot failure")
            return original_set_state(*args, **kwargs)

        self.repository.set_system_state = fail_once
        first = service.process_once()
        self.assertEqual(first["job"]["status"], "RETRY")
        self.assertEqual(len(bot.sent), 1)
        second = service.process_once()
        self.assertEqual(second["job"]["status"], "COMPLETED")
        self.assertEqual(second["result"]["status"], "ALREADY_RECORDED")
        self.assertEqual(len(bot.sent), 1)

    def test_exact_alert_idempotency_survives_a_newer_delivery_record(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        bot = FakeBot(destination_chat_id="-100999", dry_run=False)
        service = self.service(analyzer=analyzer, bot=bot, max_attempts=2)
        service.message_sink(message(390, "NVIDIA announced a new GPU platform"))
        service.process_once()
        original_set_state = self.repository.set_system_state
        failed = False

        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("fixture snapshot failure")
            return original_set_state(*args, **kwargs)

        self.repository.set_system_state = fail_once
        first = service.process_once()
        self.assertEqual(first["job"]["status"], "RETRY")
        event_id = first["job"]["payload"]["event_id"]
        self.clock.advance(1)
        self.repository.record_alert(
            event_id,
            alert_kind="alert",
            status="SENT",
            destination="-100999",
            message_hash="newer-hash",
            idempotency_key="alert:live:newer-fixture",
            at=self.clock(),
        )

        second = service.process_once()

        self.assertEqual(second["result"]["status"], "ALREADY_RECORDED")
        self.assertEqual(second["result"]["reason"], "matching_idempotency_record")
        self.assertEqual(len(bot.sent), 1)

    def test_delete_retracts_event_and_cancels_pending_alert(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        bot = FakeBot(destination_chat_id="-100999", dry_run=False)
        service = self.service(analyzer=analyzer, bot=bot)
        service.message_sink(message(391, "NVIDIA announced a new GPU platform"))
        processed = service.process_once()
        event_id = processed["result"]["event_id"]
        self.assertEqual(self.repository.job_counts()["PENDING"], 1)

        deleted = service.delete_sink(
            DeletedMessageEvent(
                source="telegram",
                chat_id=-1001,
                message_ids=(391,),
                is_permanent=True,
                from_cache=False,
                received_at=self.clock(),
            )
        )

        self.assertEqual(deleted["affected_event_ids"], (event_id,))
        self.assertEqual(deleted["cancelled_alert_jobs"], 1)
        event = self.repository.get_event(event_id)
        self.assertEqual(event["status"], "resolved")
        self.assertEqual(event["message_count"], 0)
        self.assertIsNone(service.process_once())
        self.assertEqual(bot.sent, [])

    def test_disabled_and_dry_run_modes_do_not_consume_live_alert_backlog(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        live_bot = FakeBot(destination_chat_id="-100999", dry_run=False)
        live = self.service(analyzer=analyzer, bot=live_bot)
        live.message_sink(message(392, "NVIDIA announced a new GPU platform"))
        live.process_once()
        self.assertEqual(self.repository.job_counts()["PENDING"], 1)

        disabled = self.service(
            analyzer=None,
            bot=None,
            alerts_enabled=False,
        )
        self.assertIsNone(disabled.process_once(kinds=("send_alert",)))
        dry = self.service(
            analyzer=None,
            bot=FakeBot(destination_chat_id="-100999", dry_run=True),
            alerts_enabled=True,
        )
        self.assertIsNone(dry.process_once(kinds=("send_alert",)))
        self.assertEqual(self.repository.job_counts()["PENDING"], 1)

        delivered = live.process_once(kinds=("send_alert",))
        self.assertEqual(delivered["result"]["status"], "SENT")
        self.assertEqual(len(live_bot.sent), 1)

    def test_pending_alerts_fail_closed_after_destination_expiry_or_policy_change(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        bot = FakeBot(destination_chat_id="-100999", dry_run=False)
        service = self.service(analyzer=analyzer, bot=bot)

        service.message_sink(message(601, "NVIDIA GPU launch alpha fixture"))
        service.process_once(kinds=("process_message",))
        bot.destination_chat_id = "-100998"
        destination_changed = service.process_once(kinds=("send_alert",))
        self.assertEqual(destination_changed["job"]["status"], "COMPLETED")
        self.assertEqual(
            destination_changed["result"]["reason"],
            "destination_changed_or_unbound",
        )
        self.assertEqual(bot.sent, [])

        service.message_sink(message(602, "NVIDIA GPU launch beta fixture"))
        service.process_once(kinds=("process_message",))
        self.clock.advance(901)
        expired = service.process_once(kinds=("send_alert",))
        self.assertEqual(expired["job"]["status"], "COMPLETED")
        self.assertEqual(expired["result"]["reason"], "alert_expired")
        self.assertEqual(bot.sent, [])

        service.message_sink(message(603, "NVIDIA GPU launch gamma fixture"))
        service.process_once(kinds=("process_message",))
        service.policy_resolver = lambda _chat_id: {"enabled": False}
        revoked = service.process_once(kinds=("send_alert",))
        self.assertEqual(revoked["job"]["status"], "COMPLETED")
        self.assertEqual(revoked["result"]["reason"], "source_policy_revoked")
        self.assertEqual(bot.sent, [])

        counts = self.repository.status_counts()
        self.assertEqual(counts["alerts"], {})
        self.assertEqual(counts["jobs"]["DEAD"], 0)

    def test_cached_cross_source_duplicate_is_scored_then_cooldown_suppresses(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        bot = FakeBot(destination_chat_id="-100999", dry_run=True)
        service = self.service(analyzer=analyzer, bot=bot)
        service.message_sink(message(43, "NVIDIA announced a new GPU platform"))
        service.process_once()
        service.process_once()
        self.assertEqual(len(bot.sent), 1)

        service.message_sink(
            message(
                44,
                "NVIDIA announced a new GPU platform",
                chat_id=-1002,
            )
        )
        duplicate = service.process_once()
        self.assertTrue(duplicate["result"]["cache_hit"])
        self.assertEqual(len(analyzer.calls), 1)
        suppressed = service.process_once()
        self.assertEqual(suppressed["result"]["status"], "SUPPRESSED")
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(
            self.repository.status_counts()["alerts"],
            {"SUPPRESSED": 1, "WOULD_SEND": 1},
        )

    def test_atomic_message_job_rolls_back_and_startup_recovers_legacy_orphan(self) -> None:
        with self.repository.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER fixture_reject_job BEFORE INSERT ON retry_jobs
                   BEGIN SELECT RAISE(ABORT, 'fixture job failure'); END"""
            )
        orphan = message(40, "NVIDIA GPU atomic fixture")
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.store_message_and_enqueue(
                orphan,
                kind="process_message",
                dedupe_key_prefix="message:telegram:-1001:40",
            )
        self.assertIsNone(self.repository.get_message("telegram", -1001, 40))
        with self.repository.transaction() as connection:
            connection.execute("DROP TRIGGER fixture_reject_job")

        stored = self.repository.store_message(orphan)
        self.assertEqual(self.repository.job_counts()["PENDING"], 0)
        recovered = self.service(analyzer=None, bot=None, alerts_enabled=False)
        self.assertIsNotNone(recovered)
        self.assertEqual(self.repository.job_counts()["PENDING"], 1)
        job = self.repository.claim_job("fixture", kinds=("process_message",))
        self.assertEqual(job["payload"]["message_row_id"], stored["id"])
        self.assertEqual(job["payload"]["record_version"], 1)

    def test_cache_and_provider_usage_survive_retry_without_double_charge(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        service = self.service(analyzer=analyzer, bot=None, alerts_enabled=False)
        original_save = self.repository.save_analysis_result
        failed = False

        def fail_once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("fixture crash after cache")
            return original_save(*args, **kwargs)

        self.repository.save_analysis_result = fail_once
        service.message_sink(message(41, "NVIDIA announced a new GPU"))
        first = service.process_once()
        self.assertEqual(first["job"]["status"], "RETRY")
        with self.repository.transaction(immediate=False) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_result_cache").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0], 1)

        second = service.process_once()
        self.assertEqual(second["job"]["status"], "COMPLETED")
        self.assertEqual(second["result"]["cache_hit"], True)
        self.assertEqual(len(analyzer.calls), 1)
        with self.repository.transaction(immediate=False) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0], 1)

    def test_default_structured_log_contains_metadata_not_message_body(self) -> None:
        stream = io.StringIO()

        def capture(event, **kwargs):
            return emit_event(event, stream=stream, **kwargs)

        service = PipelineService(
            self.repository,
            self.rules,
            analyzer=None,
            policy_resolver=self.policy,
            config=PipelineConfig(ai_enabled=False, alerts_enabled=False),
            clock=self.clock,
            event_emitter=capture,
        )
        secret_like_body = "NVIDIA GPU SECRET_DO_NOT_LOG_abcdefghijklmnopqrstuvwxyz1234567890"
        service.message_sink(message(42, secret_like_body))
        payload = json.loads(stream.getvalue().splitlines()[-1])
        self.assertEqual(payload["schema_version"], "studio_log_event_v1")
        self.assertEqual(payload["event"], "telegram_message_persisted")
        self.assertEqual(payload["fields"]["message_id"], 42)
        self.assertNotIn(secret_like_body, stream.getvalue())
        self.assertFalse({"body", "text", "token", "path"} & set(payload["fields"]))

    def test_default_lease_exceeds_provider_timeout_budget(self) -> None:
        self.assertGreaterEqual(PipelineConfig().lease_seconds, 180)

    def test_restart_replay_is_idempotent_and_edit_enqueues_one_new_version(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        bot = FakeBot(destination_chat_id="-100999", dry_run=False)
        first = self.service(analyzer=analyzer, bot=bot)
        original = message(5, "NVIDIA announced a new GPU")
        first.message_sink(original)
        first.drain(max_jobs=10)
        self.assertEqual(len(bot.sent), 1)

        restarted = self.service(analyzer=analyzer, bot=bot)
        replay = restarted.message_sink(original)
        self.assertTrue(replay["message"]["idempotent"])
        self.assertTrue(replay["job"]["idempotent"])
        self.assertEqual(restarted.drain(max_jobs=10), ())
        self.assertEqual(len(bot.sent), 1)

        edited = message(5, "NVIDIA announced an updated GPU roadmap", edited=True)
        changed = restarted.message_sink(edited)
        duplicate_edit = restarted.message_sink(edited)
        self.assertEqual(changed["message"]["record_version"], 2)
        self.assertFalse(changed["job"]["idempotent"])
        self.assertTrue(duplicate_edit["job"]["idempotent"])
        self.assertEqual(self.repository.job_counts()["PENDING"], 1)

    def test_material_edit_retracts_old_event_and_relinks_current_version(self) -> None:
        self.rules = RuleEngine(
            {
                "AI": {"keywords": ["NVIDIA", "GPU"], "tickers": ["NVDA"]},
                "CRYPTO": {"keywords": ["Bitcoin", "ETF"], "tickers": ["BTC"]},
            },
            ticker_whitelist={"NVDA": ["NVIDIA"], "BTC": ["Bitcoin"]},
        )
        analyzer = FakeAnalyzer(important_result())
        service = self.service(analyzer=analyzer, bot=None, alerts_enabled=False)
        service.message_sink(message(501, "NVIDIA announced a new GPU platform"))
        first = service.process_once(kinds=("process_message",))
        old_event_id = first["result"]["event_id"]
        row = self.repository.get_message("telegram", -1001, 501)
        self.assertIsNotNone(row)
        message_row_id = row["id"]

        service.message_sink(
            message(501, "Bitcoin ETF inflows increased sharply", edited=True)
        )
        second = service.process_once(kinds=("process_message",))
        new_event_id = second["result"]["event_id"]

        self.assertNotEqual(new_event_id, old_event_id)
        old_event = self.repository.get_event(old_event_id)
        new_event = self.repository.get_event(new_event_id)
        self.assertEqual(old_event["message_count"], 0)
        self.assertEqual(old_event["status"], "resolved")
        self.assertEqual(old_event["importance_score"], 0)
        self.assertEqual(old_event["confidence_score"], 0)
        self.assertEqual(new_event["message_count"], 1)
        with self.repository.transaction(immediate=False) as connection:
            memberships = connection.execute(
                """SELECT event_id,relation FROM event_messages
                   WHERE message_row_id=? ORDER BY event_id""",
                (message_row_id,),
            ).fetchall()
            analyses = connection.execute(
                """SELECT event_id,record_version FROM analysis_results
                   WHERE message_row_id=? ORDER BY record_version""",
                (message_row_id,),
            ).fetchall()
        self.assertEqual(
            {item["event_id"]: item["relation"] for item in memberships},
            {old_event_id: "retracted", new_event_id: "member"},
        )
        self.assertEqual(
            [(item["event_id"], item["record_version"]) for item in analyses],
            [(old_event_id, 1), (new_event_id, 2)],
        )

    def test_delete_while_analyzer_is_blocked_prevents_stale_analysis_and_alert(self) -> None:
        analyzer = BlockingAnalyzer(important_result())
        bot = FakeBot(destination_chat_id="-100999", dry_run=False)
        service = self.service(analyzer=analyzer, bot=bot)
        service.message_sink(message(502, "NVIDIA announced a new GPU platform"))
        outcome = {}

        def process_message_job() -> None:
            try:
                outcome["value"] = service.process_once(kinds=("process_message",))
            except BaseException as exc:  # pragma: no cover - surfaced below
                outcome["error"] = exc

        worker = threading.Thread(target=process_message_job, daemon=True)
        worker.start()
        self.assertTrue(analyzer.started.wait(timeout=3))
        try:
            service.delete_sink(
                DeletedMessageEvent(
                    source="telegram",
                    chat_id=-1001,
                    message_ids=(502,),
                    is_permanent=True,
                    from_cache=False,
                    received_at=self.clock(),
                )
            )
        finally:
            analyzer.release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", outcome)
        self.assertIn(
            outcome["value"]["result"]["status"],
            {"message_deleted", "stale_record_version"},
        )
        with self.repository.transaction(immediate=False) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM retry_jobs WHERE kind='send_alert'"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(bot.sent, [])

    def test_edit_while_analyzer_is_blocked_prevents_old_version_analysis_and_alert(self) -> None:
        analyzer = BlockingAnalyzer(important_result())
        bot = FakeBot(destination_chat_id="-100999", dry_run=False)
        service = self.service(analyzer=analyzer, bot=bot)
        service.message_sink(message(503, "NVIDIA announced a new GPU platform"))
        outcome = {}

        def process_message_job() -> None:
            try:
                outcome["value"] = service.process_once(kinds=("process_message",))
            except BaseException as exc:  # pragma: no cover - surfaced below
                outcome["error"] = exc

        worker = threading.Thread(target=process_message_job, daemon=True)
        worker.start()
        self.assertTrue(analyzer.started.wait(timeout=3))
        try:
            edited = service.message_sink(
                message(503, "NVIDIA cancelled the GPU platform", edited=True)
            )
        finally:
            analyzer.release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(edited["message"]["record_version"], 2)
        self.assertIn(
            outcome["value"]["result"]["status"],
            {"message_deleted", "stale_record_version"},
        )
        with self.repository.transaction(immediate=False) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM retry_jobs WHERE kind='send_alert'"
                ).fetchone()[0],
                0,
            )
        self.assertEqual(bot.sent, [])

    def test_alert_job_carries_record_version_and_rejects_changed_trigger(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        bot = FakeBot(destination_chat_id="-100999", dry_run=False)
        service = self.service(analyzer=analyzer, bot=bot)
        service.message_sink(message(504, "NVIDIA announced a new GPU platform"))
        processed = service.process_once(kinds=("process_message",))
        event_id = processed["result"]["event_id"]
        row = self.repository.get_message("telegram", -1001, 504)
        self.assertIsNotNone(row)
        with self.repository.transaction(immediate=False) as connection:
            alert_job = connection.execute(
                """SELECT payload_json FROM retry_jobs
                   WHERE kind='send_alert' AND status='PENDING'"""
            ).fetchone()
        self.assertIsNotNone(alert_job)
        alert_payload = json.loads(alert_job["payload_json"])
        self.assertEqual(alert_payload["record_version"], 1)

        service.message_sink(
            message(504, "NVIDIA cancelled the GPU platform", edited=True)
        )
        delivery = self.repository.event_delivery_state(
            event_id,
            message_row_id=row["id"],
            record_version=alert_payload["record_version"],
        )
        self.assertFalse(delivery["trigger_message_active"])

        self.repository.enqueue_job(
            "send_alert",
            alert_payload,
            dedupe_key="alert:live:stale-record-version-fixture",
        )
        attempted = service.process_once(kinds=("send_alert",))
        self.assertEqual(attempted["result"]["status"], "SUPPRESSED")
        self.assertEqual(bot.sent, [])
        with self.repository.transaction(immediate=False) as connection:
            delivered_count = connection.execute(
                "SELECT COUNT(*) FROM alerts WHERE status IN ('SENT','WOULD_SEND')"
            ).fetchone()[0]
        self.assertEqual(delivered_count, 0)

    def test_low_score_and_high_duplicate_skip_ai(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        low_service = self.service(
            analyzer=analyzer,
            bot=None,
            pre_score=PreScoreEngine(),
            alerts_enabled=False,
        )
        low_service.message_sink(message(6, "hello"))
        low = low_service.process_once()
        self.assertEqual(low["result"]["reason"], "store_only")
        self.assertEqual(analyzer.calls, [])

        high_service = self.service(analyzer=analyzer, bot=None, alerts_enabled=False)
        high_service.message_sink(message(7, "NVIDIA announced a new GPU"))
        first = high_service.process_once()
        self.assertEqual(first["result"]["status"], "processed")
        self.assertEqual(len(analyzer.calls), 1)

        high_service.message_sink(
            message(8, "NVIDIA announced a new GPU", chat_id=-1002)
        )
        duplicate = high_service.process_once()
        self.assertEqual(duplicate["result"]["status"], "processed")
        self.assertTrue(duplicate["result"]["cache_hit"])
        self.assertEqual(len(analyzer.calls), 1)

    def test_model_ticker_suggestions_are_audit_only_without_rule_evidence(self) -> None:
        model_result = replace(
            important_result(),
            tickers=("NVDA", "TSLA", "FAKE"),
        )
        analyzer = FakeAnalyzer(model_result)
        service = self.service(analyzer=analyzer, bot=None, alerts_enabled=False)

        service.message_sink(message(604, "NVIDIA announced a new GPU platform"))
        processed = service.process_once(kinds=("process_message",))
        event = self.repository.get_event(processed["result"]["event_id"])
        with self.repository.transaction(immediate=False) as connection:
            stored = connection.execute(
                "SELECT result_json FROM analysis_results ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        analysis = json.loads(stored["result_json"])

        self.assertEqual(event["tickers"], ["NVDA"])
        self.assertEqual(analysis["tickers"], ["NVDA"])
        self.assertEqual(
            analysis["model_suggested_tickers"],
            ["FAKE", "NVDA", "TSLA"],
        )
        self.assertIn("unapproved_model_ticker_suggestion", analysis["risk_flags"])

    def test_cache_identity_includes_forward_and_analysis_envelope(self) -> None:
        analyzer = FakeAnalyzer(important_result())
        service = self.service(analyzer=analyzer, bot=None, alerts_enabled=False)
        original = message(70, "NVIDIA announced a new GPU")
        forwarded = replace(
            message(71, "NVIDIA announced a new GPU", chat_id=-1002),
            forward_origin=ForwardOrigin(kind="channel", chat_id=-100900, message_id=1),
            is_forwarded=True,
        )
        service.message_sink(original)
        service.process_once()
        service.message_sink(forwarded)
        result = service.process_once()

        self.assertEqual(result["result"]["status"], "processed_without_ai")
        self.assertEqual(result["result"]["reason"], "high_duplicate")
        self.assertEqual(len(analyzer.calls), 1)


if __name__ == "__main__":
    unittest.main()
