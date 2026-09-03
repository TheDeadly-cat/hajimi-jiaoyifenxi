from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.source_monitoring.contracts import canonical_json, canonical_sha256
from backend.source_monitoring.soak_db_inventory import (
    SOAK_DB_INVENTORY_VERSION,
    SOAK_DB_SCAN_ORDER,
)
from backend.source_monitoring.soak_evidence import (
    SOAK_EVENT_RUN_TERMINAL,
    SOAK_EVENT_RUNTIME_SAMPLE,
    SOAK_EVENT_SESSION_ENDED,
    SOAK_EVENT_SESSION_STARTED,
    SoakEvidenceWriter,
)
from backend.source_monitoring.soak_verifier import (
    MAX_SOAK_VERDICT_ISSUES,
    REQUIRED_SOAK_DURATION_NS,
    REQUIRED_SOAK_MAXIMUM_SAMPLE_GAP_NS,
    REQUIRED_SOAK_SAMPLE_INTERVAL_NS,
    verify_soak_evidence,
)


CAMPAIGN_ID = "source_soak_campaign_" + "1" * 32
SESSION_ID = "source_soak_session_" + "2" * 32
RUNTIME_ID = "source_monitor_runtime_" + "3" * 32
RUN_ID = "source_run_" + "4" * 32
SECOND_RUN_ID = "source_run_" + "5" * 32
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
HOUR_NS = 60 * 60 * 1_000_000_000
_MATCHING_BINDINGS = object()

INVENTORY_SAFETY = {
    "database_writes_performed": 0,
    "network_requests_performed": 0,
    "provider_calls_performed": 0,
    "market_calls_performed": 0,
    "execution_capability": "none",
    "live_trading_allowed": False,
}


def inventory(*runs: dict[str, str]) -> dict[str, object]:
    entries = sorted((dict(run) for run in runs), key=lambda item: item["run_id"])
    run_ids = [entry["run_id"] for entry in entries]
    columns = ["run_id", "status", "receipt_id"]
    value: dict[str, object] = {
        "version": SOAK_DB_INVENTORY_VERSION,
        "scan_order": SOAK_DB_SCAN_ORDER,
        "scan_page_size": 500,
        "scan_page_count": 1 if entries else 0,
        "run_row_columns": columns,
        "run_count": len(entries),
        "receipt_count": sum(1 for entry in entries if entry["receipt_id"]),
        "run_ids_sha256": canonical_sha256(run_ids),
        "runs_sha256": canonical_sha256(entries),
        "inventory_sha256": "",
        "runs": entries,
        "safety": dict(INVENTORY_SAFETY),
    }
    value["inventory_sha256"] = canonical_sha256(
        {
            "version": value["version"],
            "scan_order": value["scan_order"],
            "run_row_columns": value["run_row_columns"],
            "run_count": value["run_count"],
            "receipt_count": value["receipt_count"],
            "run_ids_sha256": value["run_ids_sha256"],
            "runs_sha256": value["runs_sha256"],
        }
    )
    return value


def run_entry(
    run_id: str = RUN_ID,
    *,
    status: str = "SUCCEEDED",
    row_sha256: str = HASH_A,
    receipt_id: str = "",
    receipt_sha256: str = "",
) -> dict[str, str]:
    return {
        "run_id": run_id,
        "status": status,
        "row_sha256": row_sha256,
        "receipt_id": receipt_id,
        "receipt_sha256": receipt_sha256,
    }


class SourceMonitoringSoakVerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ai-studio-soak-verifier-test-"
        )
        self.root = Path(self.temporary.name)
        self.baseline = inventory()
        self.final = inventory(run_entry())

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def path(self, name: str = "soak.jsonl") -> Path:
        return self.root / name

    def start_payload(
        self,
        *,
        required_duration_ns: int = REQUIRED_SOAK_DURATION_NS,
        sample_interval_ns: int = REQUIRED_SOAK_SAMPLE_INTERVAL_NS,
        maximum_sample_gap_ns: int = REQUIRED_SOAK_MAXIMUM_SAMPLE_GAP_NS,
        recovered_running_count: int = 0,
        baseline: dict[str, object] | None = None,
    ) -> dict[str, object]:
        selected = baseline or self.baseline
        return {
            "mode": "official",
            "preview_sha256": HASH_F,
            "required_duration_ns": required_duration_ns,
            "sample_interval_ns": sample_interval_ns,
            "maximum_sample_gap_ns": maximum_sample_gap_ns,
            "settings_sha256": HASH_A,
            "registry_sha256": HASH_B,
            "code_identity_sha256": HASH_C,
            "db_startup_identity_sha256": HASH_D,
            "db_schema_sha256": HASH_E,
            "baseline_run_count": selected["run_count"],
            "baseline_run_inventory_sha256": selected["inventory_sha256"],
            "recovered_running_count": recovered_running_count,
            "enabled_adapter_count": 1,
            "enabled_adapter_keys_sha256": canonical_sha256(["federal_reserve"]),
        }

    @staticmethod
    def sample_payload(*, live: bool = True) -> dict[str, object]:
        return {
            "runtime_status": "running" if live else "stalled",
            "thread_alive": True,
            "liveness_verified": live,
            "heartbeat_age_ms": 1 if live else 120_001,
            "active_adapter": "",
            "last_loop_at": 1_000,
        }

    @staticmethod
    def terminal_payload(
        *,
        run_id: str = RUN_ID,
        state_recorded: bool = True,
        row_sha256: str = HASH_A,
        receipt_sha256: str = "",
    ) -> dict[str, object]:
        return {
            "adapter_key": "federal_reserve",
            "config_version": "federal_reserve_config_v1_fixture",
            "run_id": run_id if state_recorded else "",
            "status": "SUCCEEDED",
            "state_recorded": state_recorded,
            "run_record_sha256": row_sha256 if state_recorded else "",
            "import_receipt_sha256": receipt_sha256,
            "counts": {
                "observed_count": 1,
                "accepted_count": 1,
                "duplicate_count": 0,
                "rejected_count": 0,
            },
            "error_code": "",
            "market_calls_performed": 0,
            "source_evidence_status": "not_evaluated",
        }

    def end_payload(
        self,
        *,
        elapsed_ns: int = REQUIRED_SOAK_DURATION_NS,
        reason: str = "duration_reached",
        stopped: bool = True,
        session_run_count: int = 1,
        final: dict[str, object] | None = None,
    ) -> dict[str, object]:
        selected = final or self.final
        return {
            "reason": reason,
            "elapsed_ns": elapsed_ns,
            "runtime_stopped_cleanly": stopped,
            "session_run_count": session_run_count,
            "final_run_inventory_sha256": selected["inventory_sha256"],
            "safety": {
                "provider_calls_performed": 0,
                "model_calls_performed": 0,
                "formal_rounds_created": 0,
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
        }

    def writer(self, name: str = "soak.jsonl") -> SoakEvidenceWriter:
        return SoakEvidenceWriter(
            self.path(name),
            campaign_id=CAMPAIGN_ID,
            session_id=SESSION_ID,
            runtime_id=RUNTIME_ID,
            _fsync=lambda _descriptor: None,
        )

    @staticmethod
    def expected_bindings() -> dict[str, str]:
        return {
            "settings_sha256": HASH_A,
            "registry_sha256": HASH_B,
            "code_identity_sha256": HASH_C,
            "db_startup_identity_sha256": HASH_D,
            "db_schema_sha256": HASH_E,
            "preview_sha256": HASH_F,
            "enabled_adapter_keys_sha256": canonical_sha256(["federal_reserve"]),
        }

    def verify(
        self,
        path: Path,
        *,
        baseline: dict[str, object] | None = None,
        final: dict[str, object] | None = None,
        expected_bindings: object = _MATCHING_BINDINGS,
        expected_enabled_adapter_keys: object = ("federal_reserve",),
    ) -> dict[str, object]:
        return verify_soak_evidence(
            path,
            baseline_inventory=baseline or self.baseline,
            final_inventory=final or self.final,
            expected_bindings=(
                self.expected_bindings()
                if expected_bindings is _MATCHING_BINDINGS
                else expected_bindings
            ),
            expected_enabled_adapter_keys=expected_enabled_adapter_keys,
        )

    def write_ledger(
        self,
        *,
        name: str = "soak.jsonl",
        start: dict[str, object] | None = None,
        samples: list[tuple[int, dict[str, object]]] | None = None,
        terminals: list[tuple[int, dict[str, object]]] | None = None,
        end: dict[str, object] | None = None,
        close_without_end: bool = False,
    ) -> Path:
        writer = self.writer(name)
        writer.append(
            SOAK_EVENT_SESSION_STARTED,
            wall_time_ms=1_000,
            monotonic_elapsed_ns=0,
            payload=start or self.start_payload(),
        )
        events: list[tuple[int, str, dict[str, object]]] = []
        selected_samples = (
            [
                (timestamp, self.sample_payload())
                for timestamp in range(
                    REQUIRED_SOAK_MAXIMUM_SAMPLE_GAP_NS,
                    REQUIRED_SOAK_DURATION_NS + 1,
                    REQUIRED_SOAK_MAXIMUM_SAMPLE_GAP_NS,
                )
            ]
            if samples is None
            else samples
        )
        for timestamp, payload in selected_samples:
            events.append((timestamp, SOAK_EVENT_RUNTIME_SAMPLE, payload))
        selected_terminals = [
            (10 * HOUR_NS, self.terminal_payload()),
        ] if terminals is None else terminals
        for timestamp, payload in selected_terminals:
            events.append((timestamp, SOAK_EVENT_RUN_TERMINAL, payload))
        events.sort(key=lambda item: item[0])
        for timestamp, event_type, payload in events:
            writer.append(
                event_type,
                wall_time_ms=1_000 + timestamp // 1_000_000,
                monotonic_elapsed_ns=timestamp,
                payload=payload,
            )
        if not close_without_end:
            end_payload = end or self.end_payload()
            writer.append(
                SOAK_EVENT_SESSION_ENDED,
                wall_time_ms=86_401_000,
                monotonic_elapsed_ns=end_payload["elapsed_ns"],
                payload=end_payload,
            )
        writer.close()
        return self.path(name)

    @staticmethod
    def issue_codes(verdict: dict[str, object]) -> set[str]:
        return {issue["code"] for issue in verdict["issues"]}  # type: ignore[index]

    def test_pass_verifies_continuity_and_database_but_never_source_acceptance(self) -> None:
        verdict = self.verify(self.write_ledger())

        self.assertEqual(verdict["overall_status"], "EVIDENCE_VERIFIED")
        self.assertEqual(verdict["continuity_verdict"], "PASS")
        self.assertEqual(verdict["production_binding_verdict"], "PASS")
        self.assertEqual(verdict["database_verdict"], "PASS")
        self.assertEqual(verdict["source_acceptance_verdict"], "NOT_EVALUATED")
        self.assertEqual(verdict["overall_acceptance"], "NOT_CLAIMED")
        self.assertEqual(verdict["issue_count"], 0)
        self.assertEqual(verdict["counts"]["runtime_sample_count"], 720)
        self.assertEqual(verdict["counts"]["run_terminal_count"], 1)
        self.assertEqual(
            verdict["timing"]["maximum_observed_sample_gap_ns"],
            REQUIRED_SOAK_MAXIMUM_SAMPLE_GAP_NS,
        )
        self.assertEqual(len(verdict["verdict_sha256"]), 64)
        self.assertEqual(
            set(verdict),
            {
                "version",
                "overall_status",
                "continuity_verdict",
                "production_binding_verdict",
                "database_verdict",
                "source_acceptance_verdict",
                "overall_acceptance",
                "identity",
                "counts",
                "timing",
                "bindings",
                "issue_count",
                "issues",
                "issues_truncated",
                "verdict_sha256",
                "safety",
            },
        )

    def test_expected_production_bindings_are_mandatory_and_exact(self) -> None:
        missing = self.verify(
            self.write_ledger(name="missing-bindings.jsonl"),
            expected_bindings=None,
        )
        self.assertEqual(missing["continuity_verdict"], "PASS")
        self.assertEqual(missing["database_verdict"], "PASS")
        self.assertEqual(missing["production_binding_verdict"], "FAIL")
        self.assertEqual(missing["overall_status"], "FAILED")
        self.assertIn("SOAK_EXPECTED_BINDINGS_MISSING", self.issue_codes(missing))

        mismatched_bindings = self.expected_bindings()
        mismatched_bindings["settings_sha256"] = HASH_F
        mismatch = self.verify(
            self.write_ledger(name="mismatched-bindings.jsonl"),
            expected_bindings=mismatched_bindings,
        )
        self.assertEqual(mismatch["continuity_verdict"], "PASS")
        self.assertEqual(mismatch["database_verdict"], "PASS")
        self.assertEqual(mismatch["production_binding_verdict"], "FAIL")
        self.assertEqual(mismatch["overall_status"], "FAILED")
        self.assertIn("SOAK_SETTINGS_BINDING_MISMATCH", self.issue_codes(mismatch))
        self.assertNotEqual(
            mismatch["bindings"]["expected_production_bindings_sha256"],
            mismatch["bindings"]["observed_production_bindings_sha256"],
        )

        invalid = self.verify(
            self.write_ledger(name="invalid-bindings.jsonl"),
            expected_bindings={"settings_sha256": HASH_A},
        )
        self.assertEqual(invalid["production_binding_verdict"], "FAIL")
        self.assertIn("SOAK_EXPECTED_BINDINGS_INVALID", self.issue_codes(invalid))

    def test_v1_timing_policy_rejects_looser_declared_interval_and_gap(self) -> None:
        path = self.write_ledger(
            name="loose-timing-policy.jsonl",
            start=self.start_payload(
                sample_interval_ns=REQUIRED_SOAK_SAMPLE_INTERVAL_NS + 1,
                maximum_sample_gap_ns=REQUIRED_SOAK_MAXIMUM_SAMPLE_GAP_NS + 1,
            ),
        )
        verdict = self.verify(path)
        codes = self.issue_codes(verdict)
        self.assertEqual(verdict["production_binding_verdict"], "PASS")
        self.assertEqual(verdict["database_verdict"], "PASS")
        self.assertEqual(verdict["continuity_verdict"], "FAIL")
        self.assertEqual(verdict["overall_status"], "FAILED")
        self.assertIn("SOAK_SAMPLE_INTERVAL_POLICY_INVALID", codes)
        self.assertIn("SOAK_MAXIMUM_SAMPLE_GAP_POLICY_INVALID", codes)

    def test_every_confirmed_adapter_requires_terminal_coverage(self) -> None:
        path = self.write_ledger(
            name="missing-adapter-coverage.jsonl",
            terminals=[],
            end=self.end_payload(final=self.baseline, session_run_count=0),
        )

        verdict = self.verify(path, final=self.baseline)

        self.assertEqual(verdict["continuity_verdict"], "PASS")
        self.assertEqual(verdict["database_verdict"], "PASS")
        self.assertEqual(verdict["production_binding_verdict"], "FAIL")
        self.assertEqual(verdict["counts"]["expected_adapter_count"], 1)
        self.assertEqual(verdict["counts"]["covered_adapter_count"], 0)
        self.assertIn(
            "SOAK_ENABLED_ADAPTER_NOT_COVERED",
            self.issue_codes(verdict),
        )

    def test_v1_verification_is_official_only(self) -> None:
        start = self.start_payload()
        start["mode"] = "futu"

        verdict = self.verify(
            self.write_ledger(name="futu-mode.jsonl", start=start)
        )

        self.assertEqual(verdict["production_binding_verdict"], "FAIL")
        self.assertEqual(verdict["overall_status"], "FAILED")
        self.assertIn("SOAK_MODE_POLICY_INVALID", self.issue_codes(verdict))

    def test_missing_end_is_incomplete_unsealed_and_skips_database_claim(self) -> None:
        path = self.write_ledger(
            name="unsealed.jsonl",
            samples=[(1, self.sample_payload())],
            terminals=[],
            close_without_end=True,
        )
        verdict = self.verify(path, final=self.baseline)
        self.assertEqual(verdict["overall_status"], "INCOMPLETE_UNSEALED")
        self.assertEqual(verdict["continuity_verdict"], "INCOMPLETE_UNSEALED")
        self.assertEqual(verdict["database_verdict"], "NOT_EVALUATED")
        self.assertIn("SOAK_SESSION_END_MISSING", self.issue_codes(verdict))
        self.assertEqual(verdict["overall_acceptance"], "NOT_CLAIMED")

    def test_structural_tamper_returns_bounded_invalid_ledger_verdict(self) -> None:
        path = self.write_ledger(name="tampered.jsonl")
        lines = path.read_bytes().splitlines(keepends=True)
        changed = json.loads(lines[1])
        changed["payload"]["heartbeat_age_ms"] = 99
        lines[1] = canonical_json(changed).encode("utf-8") + b"\n"
        path.write_bytes(b"".join(lines))

        verdict = self.verify(path)
        self.assertEqual(verdict["overall_status"], "INVALID_LEDGER")
        self.assertEqual(verdict["continuity_verdict"], "FAIL")
        self.assertEqual(verdict["database_verdict"], "NOT_EVALUATED")
        self.assertIn(
            "SOURCE_MONITORING_SOAK_CHAIN_INVALID",
            self.issue_codes(verdict),
        )

    def test_early_end_and_non_24h_policy_fail_continuity(self) -> None:
        elapsed = REQUIRED_SOAK_DURATION_NS - 1
        path = self.write_ledger(
            name="early.jsonl",
            start=self.start_payload(
                required_duration_ns=REQUIRED_SOAK_DURATION_NS - 1,
                maximum_sample_gap_ns=REQUIRED_SOAK_DURATION_NS - 1,
            ),
            samples=[(elapsed, self.sample_payload())],
            end=self.end_payload(elapsed_ns=elapsed),
        )
        verdict = self.verify(path)
        codes = self.issue_codes(verdict)
        self.assertEqual(verdict["continuity_verdict"], "FAIL")
        self.assertIn("SOAK_REQUIRED_DURATION_POLICY_INVALID", codes)
        self.assertIn("SOAK_DURATION_INCOMPLETE", codes)

    def test_sample_gap_and_non_live_sample_fail_continuity(self) -> None:
        path = self.write_ledger(
            name="gap-liveness.jsonl",
            start=self.start_payload(maximum_sample_gap_ns=HOUR_NS),
            samples=[(2 * HOUR_NS, self.sample_payload(live=False))],
            end=self.end_payload(),
        )
        verdict = self.verify(path)
        codes = self.issue_codes(verdict)
        self.assertEqual(verdict["continuity_verdict"], "FAIL")
        self.assertIn("SOAK_SAMPLE_GAP_EXCEEDED", codes)
        self.assertIn("SOAK_RUNTIME_SAMPLE_NOT_LIVE", codes)

    def test_missing_sample_recovery_and_unclean_end_are_rejected(self) -> None:
        path = self.write_ledger(
            name="policy-failures.jsonl",
            start=self.start_payload(recovered_running_count=1),
            samples=[],
            end=self.end_payload(reason="runtime_failed", stopped=False),
        )
        verdict = self.verify(path)
        codes = self.issue_codes(verdict)
        self.assertIn("SOAK_RECOVERED_RUNNING_ROWS_PRESENT", codes)
        self.assertIn("SOAK_RUNTIME_SAMPLE_MISSING", codes)
        self.assertIn("SOAK_END_REASON_INVALID", codes)
        self.assertIn("SOAK_RUNTIME_STOP_NOT_CLEAN", codes)

    def test_database_delta_extra_run_and_end_binding_mismatch_fail(self) -> None:
        mismatched_final = inventory(run_entry(), run_entry(SECOND_RUN_ID))
        path = self.write_ledger(
            name="db-extra.jsonl",
            end=self.end_payload(final=mismatched_final, session_run_count=2),
        )
        verdict = self.verify(path, final=mismatched_final)
        codes = self.issue_codes(verdict)
        self.assertEqual(verdict["database_verdict"], "FAIL")
        self.assertIn("SOAK_DB_SESSION_RUN_EXTRA", codes)
        self.assertIn("SOAK_SESSION_RUN_COUNT_MISMATCH", codes)

    def test_database_row_and_receipt_hashes_are_bound_to_terminal_record(self) -> None:
        final = inventory(
            run_entry(
                row_sha256=HASH_B,
                receipt_id="source_import_fixture",
                receipt_sha256=HASH_C,
            )
        )
        path = self.write_ledger(
            name="db-hash-mismatch.jsonl",
            end=self.end_payload(final=final),
        )
        verdict = self.verify(path, final=final)
        codes = self.issue_codes(verdict)
        self.assertEqual(verdict["database_verdict"], "FAIL")
        self.assertIn("SOAK_RUN_ROW_HASH_MISMATCH", codes)
        self.assertIn("SOAK_IMPORT_RECEIPT_HASH_MISMATCH", codes)

    def test_duplicate_and_unrecorded_terminal_declarations_fail_closed(self) -> None:
        duplicate = self.write_ledger(
            name="duplicate-run.jsonl",
            terminals=[
                (10 * HOUR_NS, self.terminal_payload()),
                (11 * HOUR_NS, self.terminal_payload()),
            ],
            end=self.end_payload(session_run_count=2),
        )
        duplicate_verdict = self.verify(duplicate)
        self.assertIn(
            "SOAK_TERMINAL_RUN_ID_DUPLICATE",
            self.issue_codes(duplicate_verdict),
        )
        self.assertEqual(duplicate_verdict["database_verdict"], "FAIL")

        unrecorded = self.write_ledger(
            name="unrecorded-run.jsonl",
            terminals=[
                (10 * HOUR_NS, self.terminal_payload(state_recorded=False)),
            ],
            end=self.end_payload(session_run_count=1, final=self.baseline),
        )
        unrecorded_verdict = self.verify(unrecorded, final=self.baseline)
        self.assertIn(
            "SOAK_TERMINAL_RUN_STATE_NOT_RECORDED",
            self.issue_codes(unrecorded_verdict),
        )
        self.assertEqual(unrecorded_verdict["database_verdict"], "FAIL")

    def test_issue_output_is_capped_without_losing_total(self) -> None:
        samples = [
            (index + 1, self.sample_payload(live=False))
            for index in range(MAX_SOAK_VERDICT_ISSUES + 6)
        ]
        path = self.write_ledger(
            name="many-issues.jsonl",
            start=self.start_payload(
                maximum_sample_gap_ns=REQUIRED_SOAK_DURATION_NS,
            ),
            samples=samples,
        )
        verdict = self.verify(path)
        self.assertGreater(verdict["issue_count"], MAX_SOAK_VERDICT_ISSUES)
        self.assertEqual(len(verdict["issues"]), MAX_SOAK_VERDICT_ISSUES)
        self.assertTrue(verdict["issues_truncated"])


if __name__ == "__main__":
    unittest.main()
