from __future__ import annotations

import json
import threading
import unittest
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import urlopen

from backend.telegram_intelligence.health import (
    HealthServer,
    HealthState,
    HealthStateError,
)


NOW = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)


class _TDLib:
    is_ready = True
    is_running = True
    authorization_state = "authorizationStateReady"
    connection_state = "connectionStateReady"
    api_hash = "must-never-appear"
    database_directory = "C:/private/session"


class TelegramHealthTests(unittest.TestCase):
    def test_snapshot_is_bounded_content_free_and_ready(self) -> None:
        monotonic = iter([100.0, 112.3456, 112.3456])
        state = HealthState(
            wall_clock=lambda: NOW,
            monotonic_clock=lambda: next(monotonic),
        )
        state.update_tdlib(_TDLib())
        state.set_states(
            ai_status="ready",
            bot_status="dry_run",
            worker_status="ready",
            storage_status="ready",
        )
        state.set_counts(
            queue_size=4,
            retry_jobs=2,
            dead_letter_jobs=1,
            database_bytes=1_000,
            database_limit_bytes=10_000,
        )
        state.record_message(NOW)
        state.record_processed()
        state.record_alert(NOW)
        snapshot = state.snapshot()
        self.assertEqual(snapshot["status"], "degraded")
        state.set_counts(dead_letter_jobs=0)
        self.assertEqual(state.snapshot()["status"], "ready")
        self.assertEqual(snapshot["messages_received"], 1)
        self.assertEqual(snapshot["messages_processed"], 1)
        self.assertEqual(snapshot["uptime_seconds"], 12.346)
        serialized = json.dumps(snapshot)
        self.assertNotIn("must-never-appear", serialized)
        self.assertNotIn("C:/private", serialized)
        self.assertNotIn("content", serialized)

    def test_state_rejects_negative_counts_unsafe_labels_and_naive_time(self) -> None:
        state = HealthState(wall_clock=lambda: NOW)
        with self.assertRaises(HealthStateError):
            state.set_counts(queue_size=-1)
        with self.assertRaises(HealthStateError):
            state.set_states(ai_status="ready secret=value")
        with self.assertRaises(HealthStateError):
            state.record_message(datetime(2026, 8, 29))
        with self.assertRaises(HealthStateError):
            state.set_counts(unknown=1)

    def test_counters_are_thread_safe(self) -> None:
        state = HealthState(wall_clock=lambda: NOW)
        threads = [
            threading.Thread(
                target=lambda: [state.record_processed() for _ in range(500)]
            )
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(state.snapshot()["messages_processed"], 2_000)

    def test_loopback_http_health_status_and_404(self) -> None:
        state = HealthState(wall_clock=lambda: NOW)
        state.update_tdlib(_TDLib())
        state.set_states(worker_status="ready", storage_status="ready")
        with HealthServer(state, host="127.0.0.1", port=0) as server:
            with urlopen(
                f"http://127.0.0.1:{server.port}/health",
                timeout=1,
            ) as response:
                health = json.loads(response.read())
            with urlopen(
                f"http://127.0.0.1:{server.port}/status",
                timeout=1,
            ) as response:
                status = json.loads(response.read())
            self.assertTrue(health["ok"])
            self.assertEqual(status["authorization_state"], "authorizationStateReady")
            with self.assertRaises(HTTPError) as captured:
                urlopen(f"http://127.0.0.1:{server.port}/other", timeout=1)
            self.assertEqual(captured.exception.code, 404)
            captured.exception.close()

    def test_storage_warning_and_full_degrade_readiness(self) -> None:
        state = HealthState(wall_clock=lambda: NOW)
        state.update_tdlib(_TDLib())
        state.set_states(worker_status="ready", storage_status="ready")
        state.set_counts(database_bytes=80, database_limit_bytes=100)
        self.assertEqual(state.snapshot()["status"], "ready")
        for storage_status in ("warning", "full"):
            with self.subTest(storage_status=storage_status):
                state.set_states(storage_status=storage_status)
                self.assertEqual(state.snapshot()["status"], "degraded")

    def test_non_loopback_bind_is_rejected_before_server_creation(self) -> None:
        with self.assertRaises(HealthStateError):
            HealthServer(HealthState(wall_clock=lambda: NOW), host="0.0.0.0")
        with self.assertRaises(HealthStateError):
            HealthServer(HealthState(wall_clock=lambda: NOW), host="example.com")


if __name__ == "__main__":
    unittest.main()
