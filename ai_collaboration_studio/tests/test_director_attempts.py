from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from backend.store import StudioStore


class DirectorAttemptLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "director-attempts.sqlite3"
        self.store = StudioStore(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def start_round(self) -> tuple[dict[str, object], dict[str, object]]:
        moderator = self.store.enabled_members("room_plan")[0]
        round_row = self.store.create_round("room_plan", "Test the director attempt ledger")
        return moderator, round_row

    def begin(
        self,
        moderator: dict[str, object],
        round_row: dict[str, object],
        *,
        store: StudioStore | None = None,
    ) -> dict[str, object]:
        target = store or self.store
        return target.begin_director_attempt(
            "room_plan",
            str(round_row["id"]),
            moderator_member_id=str(moderator["id"]),
            moderator_member_version=int(moderator["version"]),
            provider=str(moderator["provider"]),
            model=str(moderator["model"]),
        )

    def finish(
        self,
        attempt: dict[str, object],
        round_row: dict[str, object],
        *,
        status: str,
        store: StudioStore | None = None,
        **fields: object,
    ) -> dict[str, object]:
        target = store or self.store
        return target.finish_director_attempt(
            "room_plan",
            str(round_row["id"]),
            str(attempt["id"]),
            str(attempt["attempt_token"]),
            status=status,
            **fields,
        )

    def test_migration_is_idempotent_and_self_heals_the_ledger_schema(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TABLE director_attempts")

        StudioStore(self.db_path)
        StudioStore(self.db_path)

        with closing(sqlite3.connect(self.db_path)) as connection:
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(director_attempts)"
                ).fetchall()
            }
            migration_count = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE key='director_attempts_v1'"
            ).fetchone()[0]
            indexes = {
                row[1]: row[2]
                for row in connection.execute(
                    "PRAGMA index_list(director_attempts)"
                ).fetchall()
            }

        self.assertTrue({
            "sequence_no",
            "moderator_member_id",
            "moderator_member_version",
            "provider",
            "model",
            "response_summary_sha256",
            "decision_summary_sha256",
            "selected_member_id",
            "director_decision_id",
            "turn_order",
            "started_at",
            "finished_at",
        }.issubset(columns))
        self.assertEqual(migration_count, 1)
        self.assertEqual(indexes["uq_director_attempts_round_started"], 1)

    def test_sequence_and_supported_terminal_states_are_persisted(self) -> None:
        moderator, round_row = self.start_round()
        first = self.begin(moderator, round_row)
        self.assertEqual(first["sequence_no"], 1)
        self.assertEqual(first["status"], "STARTED")
        self.assertEqual(first["moderator_member_id"], moderator["id"])
        self.assertEqual(first["moderator_member_version"], moderator["version"])
        self.assertEqual(first["provider"], moderator["provider"])
        self.assertEqual(first["model"], moderator["model"])
        with self.assertRaises(ValueError):
            self.begin(moderator, round_row)

        invalid = self.finish(
            first,
            round_row,
            status="INVALID",
            error_code="schema_invalid",
            response_summary={"shape": "invalid"},
        )
        self.assertEqual(invalid["status"], "INVALID")
        self.assertEqual(invalid["error_code"], "schema_invalid")
        self.assertRegex(str(invalid["response_summary_sha256"]), r"^[0-9a-f]{64}$")

        second = self.begin(moderator, round_row)
        self.assertEqual(second["sequence_no"], 2)
        selected = self.store.enabled_members("room_plan")[1]
        decision = self.store.add_director_decision(
            "room_plan",
            str(round_row["id"]),
            action="speak",
            member_id=str(selected["id"]),
            member_name=str(selected["name"]),
            source="provider",
        )
        self.store.begin_round_turn(
            "room_plan",
            str(round_row["id"]),
            4,
            selected,
            director_decision_id=str(decision["id"]),
        )
        responded = self.finish(
            second,
            round_row,
            status="RESPONDED",
            response_summary={"response": "accepted"},
            decision_summary={"action": "speak", "member_id": selected["id"]},
            selected_member_id=str(selected["id"]),
            director_decision_id=str(decision["id"]),
            turn_order=4,
        )
        self.assertEqual(responded["status"], "RESPONDED")
        self.assertEqual(responded["selected_member_id"], selected["id"])
        self.assertEqual(responded["director_decision_id"], decision["id"])
        self.assertEqual(responded["turn_order"], 4)
        self.assertGreaterEqual(int(responded["finished_at"]), int(responded["started_at"]))
        self.assertEqual(
            [item["status"] for item in self.store.list_director_attempts(
                "room_plan", round_id=str(round_row["id"])
            )],
            ["INVALID", "RESPONDED"],
        )

    def test_state_machine_rejects_wrong_old_and_reused_tokens(self) -> None:
        moderator, round_row = self.start_round()
        first = self.begin(moderator, round_row)
        with self.assertRaises(ValueError):
            self.store.finish_director_attempt(
                "room_plan",
                str(round_row["id"]),
                str(first["id"]),
                "stale-token",
                status="FAILED",
            )
        with self.assertRaises(ValueError):
            self.finish(first, round_row, status="STARTED")

        failed = self.finish(first, round_row, status="FAILED", error_code="timeout")
        self.assertEqual(failed["status"], "FAILED")
        with self.assertRaises(ValueError):
            self.finish(first, round_row, status="FAILED", error_code="retry")

        second = self.begin(moderator, round_row)
        with self.assertRaises(ValueError):
            self.store.finish_director_attempt(
                "room_plan",
                str(round_row["id"]),
                str(second["id"]),
                str(first["attempt_token"]),
                status="CANCELLED",
            )
        cancelled = self.finish(second, round_row, status="CANCELLED")
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertEqual(cancelled["error_code"], "director_call_cancelled")

    def test_two_store_instances_cannot_start_the_same_round_concurrently(self) -> None:
        moderator, round_row = self.start_round()
        second_store = StudioStore(self.db_path)
        barrier = threading.Barrier(2)
        lock = threading.Lock()
        attempts: list[dict[str, object]] = []
        errors: list[Exception] = []

        def worker(store: StudioStore) -> None:
            barrier.wait()
            try:
                result = self.begin(moderator, round_row, store=store)
            except Exception as exc:  # exercise the cross-connection transaction guard
                with lock:
                    errors.append(exc)
            else:
                with lock:
                    attempts.append(result)

        threads = [
            threading.Thread(target=worker, args=(self.store,)),
            threading.Thread(target=worker, args=(second_store,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        self.assertEqual(attempts[0]["sequence_no"], 1)
        self.finish(attempts[0], round_row, status="CANCELLED")
        follow_up = self.begin(moderator, round_row, store=second_store)
        self.assertEqual(follow_up["sequence_no"], 2)
        self.finish(follow_up, round_row, status="CANCELLED", store=second_store)

    def test_paused_round_recovery_cancels_abandoned_attempt_without_its_token(self) -> None:
        moderator, round_row = self.start_round()
        abandoned = self.begin(moderator, round_row)
        with self.assertRaises(ValueError):
            self.store.cancel_started_director_attempts_for_recovery(
                "room_plan", str(round_row["id"])
            )

        self.store.complete_round(str(round_row["id"]), "PAUSED")
        recovered = self.store.cancel_started_director_attempts_for_recovery(
            "room_plan", str(round_row["id"])
        )
        self.assertEqual(recovered, 1)
        persisted = self.store.list_director_attempts(
            "room_plan", round_id=str(round_row["id"])
        )
        self.assertEqual(persisted[0]["id"], abandoned["id"])
        self.assertEqual(persisted[0]["status"], "CANCELLED")
        self.assertEqual(persisted[0]["error_code"], "director_attempt_abandoned")

        self.store.complete_round(str(round_row["id"]), "RUNNING")
        replacement = self.begin(moderator, round_row)
        self.assertEqual(replacement["sequence_no"], 2)
        self.finish(replacement, round_row, status="CANCELLED")

    def test_explicit_paused_round_cancel_terminalizes_started_attempt(self) -> None:
        moderator, round_row = self.start_round()
        self.begin(moderator, round_row)
        self.store.complete_round(str(round_row["id"]), "PAUSED")

        cancelled_round = self.store.cancel_paused_round(
            "room_plan", str(round_row["id"])
        )
        attempts = self.store.list_director_attempts(
            "room_plan", round_id=str(round_row["id"])
        )

        self.assertEqual(cancelled_round["status"], "CANCELLED")
        self.assertEqual([attempt["status"] for attempt in attempts], ["CANCELLED"])
        self.assertEqual(attempts[0]["error_code"], "director_round_cancelled")
        self.assertGreaterEqual(
            int(attempts[0]["finished_at"]), int(attempts[0]["started_at"])
        )

    def test_sensitive_summaries_and_unstructured_errors_are_only_fingerprinted(self) -> None:
        member = self.store.enabled_members("room_plan")[0]
        moderator = self.store.update_member(
            "room_plan",
            str(member["id"]),
            {"provider": "deepseek", "model": "director-model"},
        )
        self.assertIsNotNone(moderator)
        round_row = self.store.create_round("room_plan", "Clean director audit fields")
        attempt = self.store.begin_director_attempt(
            "room_plan",
            str(round_row["id"]),
            moderator_member_id=str(moderator["id"]),
            moderator_member_version=int(moderator["version"]),
            provider="\tDEEPSEEK\n",
            model="  director-model\n",
        )
        secret = "sk-proj-never-persist-this"
        finished = self.finish(
            attempt,
            round_row,
            status="INVALID",
            error_code=f"upstream body: {secret}",
            response_summary={"raw": secret},
            decision_summary={"api_key": secret},
        )

        self.assertEqual(finished["provider"], "deepseek")
        self.assertEqual(finished["model"], "director-model")
        self.assertEqual(finished["error_code"], "invalid_error_code")
        self.assertRegex(str(finished["response_summary_sha256"]), r"^[0-9a-f]{64}$")
        self.assertRegex(str(finished["decision_summary_sha256"]), r"^[0-9a-f]{64}$")
        self.assertNotIn("attempt_token", finished)
        self.assertNotIn("attempt_token_sha256", finished)

        with closing(sqlite3.connect(self.db_path)) as connection:
            raw_row = connection.execute(
                "SELECT * FROM director_attempts WHERE id=?", (attempt["id"],)
            ).fetchone()
        serialized_row = repr(tuple(raw_row or ()))
        self.assertNotIn(secret, serialized_row)
        self.assertNotIn(str(attempt["attempt_token"]), serialized_row)

    def test_list_is_read_only_public_ordered_and_limited(self) -> None:
        moderator, round_row = self.start_round()
        for status in ("FAILED", "INVALID", "CANCELLED"):
            attempt = self.begin(moderator, round_row)
            self.finish(attempt, round_row, status=status)

        with closing(sqlite3.connect(self.db_path)) as connection:
            before = connection.execute(
                "SELECT COUNT(*),SUM(finished_at) FROM director_attempts"
            ).fetchone()
        listed = self.store.list_director_attempts(
            "room_plan", round_id=str(round_row["id"]), limit=2
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            after = connection.execute(
                "SELECT COUNT(*),SUM(finished_at) FROM director_attempts"
            ).fetchone()

        self.assertEqual(before, after)
        self.assertEqual([item["sequence_no"] for item in listed], [2, 3])
        self.assertEqual([item["status"] for item in listed], ["INVALID", "CANCELLED"])
        self.assertTrue(all("attempt_token" not in item for item in listed))
        self.assertTrue(all("attempt_token_sha256" not in item for item in listed))
        self.assertEqual(
            self.store.list_director_attempts(
                "room_research", round_id=str(round_row["id"])
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
