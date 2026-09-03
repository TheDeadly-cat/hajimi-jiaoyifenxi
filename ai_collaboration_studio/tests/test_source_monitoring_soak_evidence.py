from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.source_monitoring.contracts import canonical_json, canonical_sha256
from backend.source_monitoring.soak_evidence import (
    MAX_SOAK_RECORD_BYTES,
    SOAK_EVENT_RUN_TERMINAL,
    SOAK_EVENT_RUNTIME_SAMPLE,
    SOAK_EVENT_SESSION_ENDED,
    SOAK_EVENT_SESSION_STARTED,
    SourceMonitoringSoakEvidenceError,
    SoakEvidenceWriter,
    load_soak_evidence,
    validate_soak_evidence,
)


CAMPAIGN_ID = "source_soak_campaign_" + "a" * 32
SESSION_ID = "source_soak_session_" + "b" * 32
RUNTIME_ID = "source_monitor_runtime_" + "c" * 32
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def start_payload() -> dict[str, object]:
    return {
        "mode": "official",
        "preview_sha256": HASH_A,
        "required_duration_ns": 86_400_000_000_000,
        "sample_interval_ns": 5_000_000_000,
        "maximum_sample_gap_ns": 15_000_000_000,
        "settings_sha256": HASH_A,
        "registry_sha256": HASH_B,
        "code_identity_sha256": HASH_C,
        "db_startup_identity_sha256": HASH_D,
        "db_schema_sha256": HASH_E,
        "baseline_run_count": 7,
        "baseline_run_inventory_sha256": HASH_F,
        "recovered_running_count": 0,
        "enabled_adapter_count": 1,
        "enabled_adapter_keys_sha256": HASH_B,
    }


def sample_payload() -> dict[str, object]:
    return {
        "runtime_status": "running",
        "thread_alive": True,
        "liveness_verified": True,
        "heartbeat_age_ms": 3,
        "active_adapter": "",
        "last_loop_at": 1_000,
    }


def run_payload(*, state_recorded: bool = True) -> dict[str, object]:
    return {
        "adapter_key": "federal_reserve",
        "config_version": "federal_reserve_config_v1_fixture",
        "run_id": "source_run_" + "d" * 32 if state_recorded else "",
        "status": "SUCCEEDED",
        "state_recorded": state_recorded,
        "run_record_sha256": HASH_A if state_recorded else "",
        "import_receipt_sha256": "",
        "counts": {
            "observed_count": 2,
            "accepted_count": 1,
            "duplicate_count": 1,
            "rejected_count": 0,
        },
        "error_code": "",
        "market_calls_performed": 0,
        "source_evidence_status": "not_evaluated",
    }


def end_payload(*, elapsed_ns: int = 86_400_000_000_000) -> dict[str, object]:
    return {
        "reason": "duration_reached",
        "elapsed_ns": elapsed_ns,
        "runtime_stopped_cleanly": True,
        "session_run_count": 1,
        "final_run_inventory_sha256": HASH_B,
        "safety": {
            "provider_calls_performed": 0,
            "model_calls_performed": 0,
            "formal_rounds_created": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
        },
    }


class SourceMonitoringSoakEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ai-studio-soak-evidence-test-"
        )
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def path(self, name: str = "soak.jsonl") -> Path:
        return self.root / name

    def writer(self, path: Path | None = None, **kwargs: object) -> SoakEvidenceWriter:
        return SoakEvidenceWriter(
            path or self.path(),
            campaign_id=CAMPAIGN_ID,
            session_id=SESSION_ID,
            runtime_id=RUNTIME_ID,
            **kwargs,
        )

    @staticmethod
    def append_complete_ledger(writer: SoakEvidenceWriter) -> None:
        writer.append(
            SOAK_EVENT_SESSION_STARTED,
            wall_time_ms=1_000,
            monotonic_elapsed_ns=0,
            payload=start_payload(),
        )
        writer.append(
            SOAK_EVENT_RUNTIME_SAMPLE,
            wall_time_ms=1_005,
            monotonic_elapsed_ns=5_000_000_000,
            payload=sample_payload(),
        )
        writer.append(
            SOAK_EVENT_RUN_TERMINAL,
            wall_time_ms=1_010,
            monotonic_elapsed_ns=10_000_000_000,
            payload=run_payload(),
        )
        writer.append(
            SOAK_EVENT_SESSION_ENDED,
            wall_time_ms=87_401_000,
            monotonic_elapsed_ns=86_400_000_000_000,
            payload=end_payload(),
        )

    def complete_bytes(self) -> bytes:
        source = self.path("source.jsonl")
        writer = self.writer(source)
        self.append_complete_ledger(writer)
        writer.close()
        return source.read_bytes()

    def write_mutation(self, name: str, value: bytes) -> Path:
        target = self.path(name)
        target.write_bytes(value)
        return target

    def assert_invalid(self, path: Path, *codes: str) -> None:
        with self.assertRaises(SourceMonitoringSoakEvidenceError) as captured:
            validate_soak_evidence(path)
        if codes:
            self.assertIn(captured.exception.code, codes)

    def test_writer_creates_one_fsynced_chain_and_stream_validator_is_bounded(self) -> None:
        fsync_calls: list[int] = []
        real_fsync = os.fsync

        def tracked_fsync(descriptor: int) -> None:
            fsync_calls.append(descriptor)
            real_fsync(descriptor)

        path = self.path()
        writer = self.writer(path, _fsync=tracked_fsync)
        self.append_complete_ledger(writer)
        self.assertTrue(writer.terminal)
        self.assertEqual(writer.record_count, 4)
        self.assertEqual(len(writer.last_record_sha256), 64)

        with self.assertRaises(SourceMonitoringSoakEvidenceError) as terminal:
            writer.append(
                SOAK_EVENT_RUNTIME_SAMPLE,
                wall_time_ms=87_401_001,
                monotonic_elapsed_ns=86_400_000_000_001,
                payload=sample_payload(),
            )
        self.assertEqual(
            terminal.exception.code,
            "SOURCE_MONITORING_SOAK_WRITER_TERMINAL",
        )
        writer.close()
        self.assertTrue(writer.closed)
        self.assertGreaterEqual(len(fsync_calls), 5)

        with self.assertRaises(SourceMonitoringSoakEvidenceError) as closed:
            writer.append(
                SOAK_EVENT_RUNTIME_SAMPLE,
                wall_time_ms=87_401_002,
                monotonic_elapsed_ns=86_400_000_000_002,
                payload=sample_payload(),
            )
        self.assertEqual(
            closed.exception.code,
            "SOURCE_MONITORING_SOAK_WRITER_CLOSED",
        )

        sequences: list[int] = []
        summary = validate_soak_evidence(
            path,
            on_record=lambda record: sequences.append(record["sequence_no"]),
        )
        self.assertEqual(sequences, [1, 2, 3, 4])
        self.assertTrue(summary["terminal"])
        self.assertEqual(summary["record_count"], 4)
        self.assertEqual(summary["source_acceptance_verdict"], "NOT_EVALUATED")
        self.assertEqual(summary["overall_acceptance"], "NOT_CLAIMED")
        self.assertEqual(len(load_soak_evidence(path)), 4)
        with self.assertRaises(SourceMonitoringSoakEvidenceError):
            load_soak_evidence(path, maximum_records=3)

        with self.assertRaises(SourceMonitoringSoakEvidenceError) as exists:
            self.writer(path)
        self.assertEqual(
            exists.exception.code,
            "SOURCE_MONITORING_SOAK_LEDGER_EXISTS",
        )

    def test_writer_holds_an_os_exclusive_lock_for_its_lifetime(self) -> None:
        path = self.path()
        writer = self.writer(path)
        second = os.open(path, os.O_RDWR | int(getattr(os, "O_BINARY", 0) or 0))
        acquired = False
        try:
            os.lseek(second, 0, os.SEEK_SET)
            with self.assertRaises(OSError):
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(second, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(second, 0, os.SEEK_SET)
                    msvcrt.locking(second, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(second, fcntl.LOCK_UN)
            os.close(second)
            writer.close()

    def test_structurally_valid_unsealed_ledger_is_never_reported_terminal(self) -> None:
        path = self.path()
        writer = self.writer(path)
        writer.append(
            SOAK_EVENT_SESSION_STARTED,
            wall_time_ms=1_000,
            monotonic_elapsed_ns=0,
            payload=start_payload(),
        )
        writer.close()

        summary = validate_soak_evidence(path)
        self.assertFalse(summary["terminal"])
        self.assertEqual(summary["last_event_type"], SOAK_EVENT_SESSION_STARTED)
        self.assertEqual(summary["overall_acceptance"], "NOT_CLAIMED")

    def test_writer_rejects_non_start_first_record_and_closed_payload_fields(self) -> None:
        wrong_first = self.writer(self.path("wrong-first.jsonl"))
        with self.assertRaises(SourceMonitoringSoakEvidenceError) as transition:
            wrong_first.append(
                SOAK_EVENT_RUNTIME_SAMPLE,
                wall_time_ms=1,
                monotonic_elapsed_ns=0,
                payload=sample_payload(),
            )
        self.assertEqual(
            transition.exception.code,
            "SOURCE_MONITORING_SOAK_TRANSITION_INVALID",
        )
        self.assertTrue(wrong_first.failed)
        self.assertTrue(wrong_first.closed)

        extra = start_payload()
        extra["unexpected"] = True
        wrong_payload = self.writer(self.path("wrong-payload.jsonl"))
        with self.assertRaises(SourceMonitoringSoakEvidenceError) as schema:
            wrong_payload.append(
                SOAK_EVENT_SESSION_STARTED,
                wall_time_ms=1,
                monotonic_elapsed_ns=0,
                payload=extra,
            )
        self.assertEqual(
            schema.exception.code,
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
        )

        bad_safety = end_payload()
        bad_safety["safety"] = {
            **bad_safety["safety"],  # type: ignore[arg-type]
            "live_trading_allowed": 0,
        }
        writer = self.writer(self.path("bad-safety.jsonl"))
        writer.append(
            SOAK_EVENT_SESSION_STARTED,
            wall_time_ms=1,
            monotonic_elapsed_ns=0,
            payload=start_payload(),
        )
        with self.assertRaises(SourceMonitoringSoakEvidenceError):
            writer.append(
                SOAK_EVENT_SESSION_ENDED,
                wall_time_ms=2,
                monotonic_elapsed_ns=86_400_000_000_000,
                payload=bad_safety,
            )

    def test_writer_rejects_monotonic_rollback_and_identity_aliases(self) -> None:
        with self.assertRaises(SourceMonitoringSoakEvidenceError):
            SoakEvidenceWriter(
                self.path(),
                campaign_id="bad",
                session_id=SESSION_ID,
                runtime_id=RUNTIME_ID,
            )

        writer = self.writer(self.path("monotonic.jsonl"))
        writer.append(
            SOAK_EVENT_SESSION_STARTED,
            wall_time_ms=100,
            monotonic_elapsed_ns=0,
            payload=start_payload(),
        )
        writer.append(
            SOAK_EVENT_RUNTIME_SAMPLE,
            wall_time_ms=101,
            monotonic_elapsed_ns=10,
            payload=sample_payload(),
        )
        with self.assertRaises(SourceMonitoringSoakEvidenceError) as rollback:
            writer.append(
                SOAK_EVENT_RUNTIME_SAMPLE,
                wall_time_ms=102,
                monotonic_elapsed_ns=9,
                payload=sample_payload(),
            )
        self.assertEqual(
            rollback.exception.code,
            "SOURCE_MONITORING_SOAK_MONOTONIC_INVALID",
        )

    def test_stream_detects_delete_reorder_duplicate_truncation_and_drift(self) -> None:
        raw = self.complete_bytes()
        lines = raw.splitlines(keepends=True)

        cases: dict[str, bytes] = {
            "deleted.jsonl": b"".join((lines[0], lines[2], lines[3])),
            "reordered.jsonl": b"".join((lines[0], lines[2], lines[1], lines[3])),
            "duplicated.jsonl": b"".join((lines[0], lines[1], lines[1], lines[2], lines[3])),
            "truncated.jsonl": raw[:-1],
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                self.assert_invalid(self.write_mutation(name, value))

        changed = json.loads(lines[1])
        changed["payload"]["heartbeat_age_ms"] = 99
        changed_line = canonical_json(changed).encode("utf-8") + b"\n"
        self.assert_invalid(
            self.write_mutation(
                "content-drift.jsonl",
                b"".join((lines[0], changed_line, lines[2], lines[3])),
            ),
            "SOURCE_MONITORING_SOAK_CHAIN_INVALID",
        )

    def test_stream_detects_same_size_in_place_mutation_behind_read_cursor(self) -> None:
        original = self.complete_bytes()
        lines = original.splitlines(keepends=True)
        path = self.write_mutation("same-size-race.jsonl", original)
        mutated = False

        def mutate_after_first_record(record: dict[str, object]) -> None:
            nonlocal mutated
            if mutated or record["sequence_no"] != 1:
                return
            with path.open("r+b", buffering=0) as stream:
                raw = stream.read()
                changed = raw.replace(b'"mode":"official"', b'"mode":"officiax"', 1)
                self.assertEqual(len(changed), len(raw))
                self.assertNotEqual(changed, raw)
                stream.seek(0)
                stream.write(changed)
                stream.flush()
                os.fsync(stream.fileno())
            mutated = True

        with self.assertRaises(SourceMonitoringSoakEvidenceError) as captured:
            validate_soak_evidence(path, on_record=mutate_after_first_record)

        self.assertTrue(mutated)
        self.assertEqual(
            captured.exception.code,
            "SOURCE_MONITORING_SOAK_FILE_CHANGED",
        )

        hash_drift = json.loads(lines[1])
        hash_drift["record_sha256"] = HASH_F
        hash_line = canonical_json(hash_drift).encode("utf-8") + b"\n"
        self.assert_invalid(
            self.write_mutation(
                "hash-drift.jsonl",
                b"".join((lines[0], hash_line, lines[2], lines[3])),
            ),
            "SOURCE_MONITORING_SOAK_CHAIN_INVALID",
        )

        unknown_field = json.loads(lines[0])
        unknown_field["unexpected"] = 1
        self.assert_invalid(
            self.write_mutation(
                "unknown-field.jsonl",
                canonical_json(unknown_field).encode("utf-8") + b"\n",
            ),
            "SOURCE_MONITORING_SOAK_SCHEMA_INVALID",
        )

        runtime_drift = json.loads(lines[1])
        runtime_drift["runtime_id"] = "source_monitor_runtime_" + "e" * 32
        unsigned = dict(runtime_drift)
        unsigned.pop("record_sha256")
        runtime_drift["record_sha256"] = canonical_sha256(unsigned)
        self.assert_invalid(
            self.write_mutation(
                "runtime-drift.jsonl",
                b"".join(
                    (
                        lines[0],
                        canonical_json(runtime_drift).encode("utf-8") + b"\n",
                    )
                ),
            ),
            "SOURCE_MONITORING_SOAK_IDENTITY_INVALID",
        )

    def test_stream_rejects_duplicate_json_keys_and_oversized_records(self) -> None:
        raw = self.complete_bytes()
        first = raw.splitlines(keepends=True)[0]
        duplicate = first.replace(
            b'{"campaign_id"',
            b'{"campaign_id":"source_soak_campaign_' + b"a" * 32 + b'","campaign_id"',
            1,
        )
        self.assert_invalid(
            self.write_mutation("duplicate-key.jsonl", duplicate),
            "SOURCE_MONITORING_SOAK_FORMAT_INVALID",
        )
        self.assert_invalid(
            self.write_mutation(
                "oversized.jsonl",
                b"{" + b"x" * MAX_SOAK_RECORD_BYTES + b"}\n",
            ),
            "SOURCE_MONITORING_SOAK_LIMIT_EXCEEDED",
        )

    def test_external_length_change_permanently_poisons_writer(self) -> None:
        path = self.path()
        writer = self.writer(path)
        writer.append(
            SOAK_EVENT_SESSION_STARTED,
            wall_time_ms=1,
            monotonic_elapsed_ns=0,
            payload=start_payload(),
        )
        with path.open("ab") as external:
            external.write(b"unexpected")
            external.flush()
            os.fsync(external.fileno())
        with self.assertRaises(SourceMonitoringSoakEvidenceError) as changed:
            writer.append(
                SOAK_EVENT_RUNTIME_SAMPLE,
                wall_time_ms=2,
                monotonic_elapsed_ns=1,
                payload=sample_payload(),
            )
        self.assertEqual(
            changed.exception.code,
            "SOURCE_MONITORING_SOAK_FILE_CHANGED",
        )
        self.assertTrue(writer.failed)
        self.assertTrue(writer.closed)

    def test_path_replacement_identity_is_rechecked_before_every_append(self) -> None:
        path = self.path()
        replacement = self.path("replacement.jsonl")
        replacement.write_bytes(b"")
        replacement_metadata = replacement.lstat()
        writer = self.writer(path)
        writer.append(
            SOAK_EVENT_SESSION_STARTED,
            wall_time_ms=1,
            monotonic_elapsed_ns=0,
            payload=start_payload(),
        )
        ledger_path = writer.path
        real_lstat = Path.lstat

        def replaced_lstat(path_value: Path) -> os.stat_result:
            if path_value == ledger_path:
                return replacement_metadata
            return real_lstat(path_value)

        with mock.patch.object(Path, "lstat", new=replaced_lstat):
            with self.assertRaises(SourceMonitoringSoakEvidenceError) as changed:
                writer.append(
                    SOAK_EVENT_RUNTIME_SAMPLE,
                    wall_time_ms=2,
                    monotonic_elapsed_ns=1,
                    payload=sample_payload(),
                )
        self.assertEqual(
            changed.exception.code,
            "SOURCE_MONITORING_SOAK_FILE_CHANGED",
        )
        self.assertTrue(writer.failed)

    def test_write_and_fsync_failure_close_writer_without_allowing_resume(self) -> None:
        def fail_write(_stream: object, _data: object) -> int:
            raise OSError("fixture write failure")

        write_path = self.path("write-failure.jsonl")
        writer = self.writer(write_path, _write=fail_write)
        with self.assertRaises(SourceMonitoringSoakEvidenceError) as write_error:
            writer.append(
                SOAK_EVENT_SESSION_STARTED,
                wall_time_ms=1,
                monotonic_elapsed_ns=0,
                payload=start_payload(),
            )
        self.assertEqual(
            write_error.exception.code,
            "SOURCE_MONITORING_SOAK_WRITE_FAILED",
        )
        self.assertTrue(writer.failed)
        self.assertTrue(writer.closed)
        with self.assertRaises(SourceMonitoringSoakEvidenceError) as existing:
            self.writer(write_path)
        self.assertEqual(
            existing.exception.code,
            "SOURCE_MONITORING_SOAK_LEDGER_EXISTS",
        )

        def fail_fsync(_descriptor: int) -> None:
            raise OSError("fixture fsync failure")

        fsync_path = self.path("fsync-failure.jsonl")
        fsync_writer = self.writer(fsync_path, _fsync=fail_fsync)
        with self.assertRaises(SourceMonitoringSoakEvidenceError) as fsync_error:
            fsync_writer.append(
                SOAK_EVENT_SESSION_STARTED,
                wall_time_ms=1,
                monotonic_elapsed_ns=0,
                payload=start_payload(),
            )
        self.assertEqual(
            fsync_error.exception.code,
            "SOURCE_MONITORING_SOAK_WRITE_FAILED",
        )
        self.assertTrue(fsync_writer.failed)
        self.assertTrue(fsync_writer.closed)


if __name__ == "__main__":
    unittest.main()
