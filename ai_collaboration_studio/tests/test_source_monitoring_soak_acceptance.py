from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.source_monitoring.contracts import canonical_sha256
from backend.source_monitoring import soak_acceptance as acceptance_module
from backend.source_monitoring.soak_acceptance import (
    MAX_SOAK_OPERATIONAL_ACCEPTANCE_ISSUES,
    REQUIRED_OFFICIAL_ADAPTER_KEYS,
    SOURCE_MONITORING_SOAK_OPERATIONAL_ACCEPTANCE_VERSION,
    _verify_official_source_operational_acceptance_paths,
    verify_official_source_operational_acceptance,
)
from backend.source_monitoring.soak_db_inventory import (
    SOAK_DB_INVENTORY_VERSION,
    SOAK_DB_SCAN_ORDER,
    load_soak_db_inventory,
    write_soak_db_inventory_exclusive,
)
from backend.source_monitoring.soak_evidence import (
    SOAK_EVENT_RUN_TERMINAL,
    SOAK_EVENT_RUNTIME_SAMPLE,
    SOAK_EVENT_SESSION_ENDED,
    SOAK_EVENT_SESSION_STARTED,
    SoakEvidenceWriter,
)
from backend.source_monitoring.soak_plan import (
    SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS,
    SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS,
    SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS,
    build_source_monitoring_soak_plan,
    load_source_monitoring_soak_plan,
    write_source_monitoring_soak_plan_exclusive,
)
from backend.source_monitoring.soak_verifier import (
    _verify_soak_evidence_with_observer,
    verify_soak_evidence,
)


CAMPAIGN_ID = "source_soak_campaign_" + "1" * 32
SESSION_ID = "source_soak_session_" + "2" * 32
RUNTIME_ID = "source_monitor_runtime_" + "3" * 32
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HOUR_NS = 60 * 60 * 1_000_000_000

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


class SourceMonitoringSoakAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ai-studio-soak-acceptance-test-"
        )
        self.root = Path(self.temporary.name)
        self.bundle_index = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _config_version(adapter_key: str) -> str:
        return f"{adapter_key}_config_v1_fixture"

    @staticmethod
    def _run_id(adapter_key: str, index: int) -> str:
        token = canonical_sha256({"adapter_key": adapter_key, "index": index})[:32]
        return "source_run_" + token

    @staticmethod
    def _run_entry(run: dict[str, object]) -> dict[str, str]:
        return {
            "run_id": run["run_id"],
            "status": run["status"],
            "row_sha256": run["row_sha256"],
            "receipt_id": run["receipt_id"],
            "receipt_sha256": run["receipt_sha256"],
        }

    @staticmethod
    def _sample_payload() -> dict[str, object]:
        return {
            "runtime_status": "running",
            "thread_alive": True,
            "liveness_verified": True,
            "heartbeat_age_ms": 1,
            "active_adapter": "",
            "last_loop_at": 1_000,
        }

    @staticmethod
    def _end_payload(final: dict[str, object], run_count: int) -> dict[str, object]:
        return {
            "reason": "duration_reached",
            "elapsed_ns": SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS,
            "runtime_stopped_cleanly": True,
            "session_run_count": run_count,
            "final_run_inventory_sha256": final["inventory_sha256"],
            "safety": {
                "provider_calls_performed": 0,
                "model_calls_performed": 0,
                "formal_rounds_created": 0,
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
        }

    def build_bundle(
        self,
        *,
        plan_keys: tuple[str, ...] = REQUIRED_OFFICIAL_ADAPTER_KEYS,
        run_keys: tuple[str, ...] | None = None,
        status_overrides: dict[str, str] | None = None,
        rejected_overrides: dict[str, int] | None = None,
        error_overrides: dict[str, str] | None = None,
        config_overrides: dict[str, str] | None = None,
        market_overrides: dict[str, int | None] | None = None,
        missing_receipt_keys: frozenset[str] = frozenset(),
        include_end: bool = True,
    ) -> Path:
        self.bundle_index += 1
        bundle = self.root / f"bundle-{self.bundle_index}"
        bundle.mkdir()
        baseline = inventory()
        write_soak_db_inventory_exclusive(
            inventory=baseline,
            artifact_path=bundle / "baseline-inventory.json",
        )
        plan = build_source_monitoring_soak_plan(
            campaign_id=CAMPAIGN_ID,
            session_id=SESSION_ID,
            settings_sha256=HASH_A,
            registry_sha256=HASH_B,
            code_identity_sha256=HASH_C,
            db_startup_identity_sha256=HASH_D,
            db_schema_sha256=HASH_E,
            baseline_run_count=baseline["run_count"],
            baseline_run_inventory_sha256=baseline["inventory_sha256"],
            enabled_adapters=[
                {
                    "adapter_key": key,
                    "config_version": self._config_version(key),
                    "state_version": 1,
                    "checkpoint_sha256": canonical_sha256({"adapter_key": key}),
                }
                for key in plan_keys
            ],
        )
        write_source_monitoring_soak_plan_exclusive(
            artifact_path=bundle / "plan.json",
            plan=plan,
        )

        selected_run_keys = plan_keys if run_keys is None else run_keys
        status_overrides = status_overrides or {}
        rejected_overrides = rejected_overrides or {}
        error_overrides = error_overrides or {}
        config_overrides = config_overrides or {}
        market_overrides = market_overrides or {}
        runs: list[dict[str, object]] = []
        for index, adapter_key in enumerate(selected_run_keys, start=1):
            status = status_overrides.get(adapter_key, "SUCCEEDED")
            rejected = rejected_overrides.get(adapter_key, 0)
            run_id = self._run_id(adapter_key, index)
            accepted_count = 0 if rejected else 1
            receipt_sha256 = (
                ""
                if accepted_count == 0 or adapter_key in missing_receipt_keys
                else canonical_sha256({"run_id": run_id, "kind": "receipt"})
            )
            runs.append(
                {
                    "adapter_key": adapter_key,
                    "config_version": config_overrides.get(
                        adapter_key,
                        self._config_version(adapter_key),
                    ),
                    "run_id": run_id,
                    "status": status,
                    "row_sha256": canonical_sha256(
                        {"adapter_key": adapter_key, "run_id": run_id, "status": status}
                    ),
                    "rejected_count": rejected,
                    "accepted_count": accepted_count,
                    "receipt_id": (
                        "" if receipt_sha256 == "" else f"source_import_{index}"
                    ),
                    "receipt_sha256": receipt_sha256,
                    "error_code": error_overrides.get(adapter_key, ""),
                    "market_calls_performed": market_overrides.get(adapter_key, 0),
                }
            )
        final = inventory(*(self._run_entry(run) for run in runs))
        write_soak_db_inventory_exclusive(
            inventory=final,
            artifact_path=bundle / "final-inventory.json",
        )

        writer = SoakEvidenceWriter(
            bundle / "ledger.jsonl",
            campaign_id=CAMPAIGN_ID,
            session_id=SESSION_ID,
            runtime_id=RUNTIME_ID,
            _fsync=lambda _descriptor: None,
        )
        writer.append(
            SOAK_EVENT_SESSION_STARTED,
            wall_time_ms=1_000,
            monotonic_elapsed_ns=0,
            payload={
                "mode": "official",
                "preview_sha256": plan["preview_sha256"],
                "required_duration_ns": plan["required_duration_ns"],
                "sample_interval_ns": plan["sample_interval_ns"],
                "maximum_sample_gap_ns": plan["maximum_sample_gap_ns"],
                "settings_sha256": plan["settings_sha256"],
                "registry_sha256": plan["registry_sha256"],
                "code_identity_sha256": plan["code_identity_sha256"],
                "db_startup_identity_sha256": plan["db_startup_identity_sha256"],
                "db_schema_sha256": plan["db_schema_sha256"],
                "baseline_run_count": baseline["run_count"],
                "baseline_run_inventory_sha256": baseline["inventory_sha256"],
                "recovered_running_count": 0,
                "enabled_adapter_count": plan["enabled_adapter_count"],
                "enabled_adapter_keys_sha256": plan[
                    "enabled_adapter_keys_sha256"
                ],
            },
        )
        events: list[tuple[int, str, dict[str, object]]] = []
        for timestamp in range(
            SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS,
            SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS + 1,
            SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS,
        ):
            events.append(
                (timestamp, SOAK_EVENT_RUNTIME_SAMPLE, self._sample_payload())
            )
        for index, run in enumerate(runs, start=1):
            rejected = run["rejected_count"]
            events.append(
                (
                    HOUR_NS + index,
                    SOAK_EVENT_RUN_TERMINAL,
                    {
                        "adapter_key": run["adapter_key"],
                        "config_version": run["config_version"],
                        "run_id": run["run_id"],
                        "status": run["status"],
                        "state_recorded": True,
                        "run_record_sha256": run["row_sha256"],
                        "import_receipt_sha256": run["receipt_sha256"],
                        "counts": {
                            "observed_count": max(1, rejected),
                            "accepted_count": run["accepted_count"],
                            "duplicate_count": 0,
                            "rejected_count": rejected,
                        },
                        "error_code": run["error_code"],
                        "market_calls_performed": run["market_calls_performed"],
                        "source_evidence_status": "not_evaluated",
                    },
                )
            )
        events.sort(key=lambda event: event[0])
        for timestamp, event_type, payload in events:
            writer.append(
                event_type,
                wall_time_ms=1_000 + timestamp // 1_000_000,
                monotonic_elapsed_ns=timestamp,
                payload=payload,
            )
        if include_end:
            writer.append(
                SOAK_EVENT_SESSION_ENDED,
                wall_time_ms=86_401_000,
                monotonic_elapsed_ns=SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS,
                payload=self._end_payload(final, len(runs)),
            )
        writer.close()
        return bundle

    @staticmethod
    def issue_codes(verdict: dict[str, object]) -> set[str]:
        return {issue["code"] for issue in verdict["issues"]}  # type: ignore[index]

    def test_exact_six_source_bundle_passes_operationally_only(self) -> None:
        bundle = self.build_bundle()

        with mock.patch("socket.socket", side_effect=AssertionError("network")), mock.patch(
            "sqlite3.connect", side_effect=AssertionError("database")
        ):
            verdict = verify_official_source_operational_acceptance(bundle)

        self.assertEqual(
            verdict["version"],
            SOURCE_MONITORING_SOAK_OPERATIONAL_ACCEPTANCE_VERSION,
        )
        self.assertEqual(
            set(verdict),
            {
                "version",
                "overall_status",
                "source_acceptance_verdict",
                "overall_acceptance",
                "operational_only",
                "content_truth_attested",
                "independent_network_witness",
                "required_adapter_keys",
                "per_adapter_counts",
                "v1_evidence_verdict",
                "issue_count",
                "issues",
                "issues_truncated",
                "verdict_sha256",
                "safety",
            },
        )
        self.assertEqual(
            verdict["overall_status"], "OPERATIONAL_SOURCE_PATH_ACCEPTED"
        )
        self.assertEqual(verdict["source_acceptance_verdict"], "PASS")
        self.assertEqual(verdict["overall_acceptance"], "NOT_CLAIMED")
        self.assertIs(verdict["operational_only"], True)
        self.assertIs(verdict["content_truth_attested"], False)
        self.assertIs(verdict["independent_network_witness"], False)
        self.assertEqual(
            verdict["required_adapter_keys"],
            list(REQUIRED_OFFICIAL_ADAPTER_KEYS),
        )
        self.assertEqual(verdict["issue_count"], 0)
        self.assertEqual(len(verdict["verdict_sha256"]), 64)
        self.assertEqual(
            verdict["v1_evidence_verdict"]["source_acceptance_verdict"],  # type: ignore[index]
            "NOT_EVALUATED",
        )
        self.assertEqual(
            verdict["v1_evidence_verdict"]["overall_acceptance"],  # type: ignore[index]
            "NOT_CLAIMED",
        )
        for adapter_key in REQUIRED_OFFICIAL_ADAPTER_KEYS:
            counts = verdict["per_adapter_counts"][adapter_key]  # type: ignore[index]
            self.assertEqual(counts["terminal_run_count"], 1)
            self.assertEqual(counts["succeeded_run_count"], 1)
            self.assertEqual(
                sum(
                    value
                    for key, value in counts.items()
                    if key not in {"terminal_run_count", "succeeded_run_count"}
                ),
                0,
            )
        self.assertEqual(
            verdict["safety"]["database_discovery_performed"],  # type: ignore[index]
            False,
        )

    def test_v1_public_output_remains_not_evaluated(self) -> None:
        bundle = self.build_bundle()
        acceptance = verify_official_source_operational_acceptance(bundle)
        plan = load_source_monitoring_soak_plan(bundle / "plan.json")
        v1 = verify_soak_evidence(
            bundle / "ledger.jsonl",
            baseline_inventory=load_soak_db_inventory(
                bundle / "baseline-inventory.json"
            ),
            final_inventory=load_soak_db_inventory(bundle / "final-inventory.json"),
            expected_bindings={
                field: plan[field]
                for field in (
                    "settings_sha256",
                    "registry_sha256",
                    "code_identity_sha256",
                    "db_startup_identity_sha256",
                    "db_schema_sha256",
                    "preview_sha256",
                    "enabled_adapter_keys_sha256",
                )
            },
            expected_enabled_adapter_keys=REQUIRED_OFFICIAL_ADAPTER_KEYS,
        )

        self.assertEqual(acceptance["source_acceptance_verdict"], "PASS")
        self.assertEqual(v1["source_acceptance_verdict"], "NOT_EVALUATED")
        self.assertEqual(v1["overall_acceptance"], "NOT_CLAIMED")
        self.assertEqual(
            acceptance["v1_evidence_verdict"]["verdict_sha256"],  # type: ignore[index]
            v1["verdict_sha256"],
        )

    def test_v1_and_operational_checks_share_one_validated_stream(self) -> None:
        bundle = self.build_bundle()
        from backend.source_monitoring.soak_evidence import validate_soak_evidence

        calls = 0

        def one_stream(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return validate_soak_evidence(*args, **kwargs)

        with mock.patch(
            "backend.source_monitoring.soak_verifier.validate_soak_evidence",
            side_effect=one_stream,
        ):
            verdict = verify_official_source_operational_acceptance(bundle)

        self.assertEqual(verdict["source_acceptance_verdict"], "PASS")
        self.assertEqual(calls, 1)
        self.assertFalse(hasattr(acceptance_module, "validate_soak_evidence"))

    def test_private_explicit_path_seam_consumes_only_supplied_artifacts(self) -> None:
        bundle = self.build_bundle()

        verdict = _verify_official_source_operational_acceptance_paths(
            plan_path=bundle / "plan.json",
            baseline_inventory_path=bundle / "baseline-inventory.json",
            ledger_path=bundle / "ledger.jsonl",
            final_inventory_path=bundle / "final-inventory.json",
        )

        self.assertEqual(verdict["source_acceptance_verdict"], "PASS")

    def test_missing_or_extra_plan_adapter_fails_closed(self) -> None:
        cases = {
            "missing": REQUIRED_OFFICIAL_ADAPTER_KEYS[:-1],
            "extra": tuple(sorted((*REQUIRED_OFFICIAL_ADAPTER_KEYS, "unexpected_source"))),
        }
        for label, plan_keys in cases.items():
            with self.subTest(label=label):
                verdict = verify_official_source_operational_acceptance(
                    self.build_bundle(plan_keys=plan_keys)
                )
                self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
                self.assertEqual(verdict["overall_acceptance"], "NOT_CLAIMED")
                self.assertIn(
                    "SOURCE_MONITORING_SOAK_V1_EVIDENCE_NOT_VERIFIED",
                    self.issue_codes(verdict),
                )
                expected = (
                    "SOURCE_MONITORING_SOAK_REQUIRED_ADAPTER_MISSING"
                    if label == "missing"
                    else "SOURCE_MONITORING_SOAK_UNEXPECTED_ADAPTER_CONFIGURED"
                )
                self.assertIn(expected, self.issue_codes(verdict))

    def test_required_adapter_without_terminal_run_fails(self) -> None:
        run_keys = REQUIRED_OFFICIAL_ADAPTER_KEYS[:-1]

        verdict = verify_official_source_operational_acceptance(
            self.build_bundle(run_keys=run_keys)
        )

        self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
        self.assertEqual(verdict["overall_acceptance"], "NOT_CLAIMED")
        self.assertIn(
            "SOURCE_MONITORING_SOAK_V1_EVIDENCE_NOT_VERIFIED",
            self.issue_codes(verdict),
        )

    def test_every_non_succeeded_terminal_status_fails(self) -> None:
        selected = REQUIRED_OFFICIAL_ADAPTER_KEYS[0]
        for status in ("DEGRADED", "FAILED", "DRY_RUN", "ABANDONED"):
            with self.subTest(status=status):
                verdict = verify_official_source_operational_acceptance(
                    self.build_bundle(status_overrides={selected: status})
                )
                self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
                codes = self.issue_codes(verdict)
                if status == "ABANDONED":
                    self.assertIn(
                        "SOURCE_MONITORING_SOAK_V1_EVIDENCE_NOT_VERIFIED", codes
                    )
                else:
                    self.assertIn(
                        "SOURCE_MONITORING_SOAK_DISALLOWED_RUN_STATUS", codes
                    )

    def test_succeeded_run_with_rejection_or_source_error_fails(self) -> None:
        selected = REQUIRED_OFFICIAL_ADAPTER_KEYS[0]
        cases = (
            (
                {"rejected_overrides": {selected: 1}},
                "SOURCE_MONITORING_SOAK_REJECTED_SOURCE_ITEMS",
            ),
            (
                {"error_overrides": {selected: "SOURCE_RESPONSE_INVALID"}},
                "SOURCE_MONITORING_SOAK_SOURCE_ERROR_OBSERVED",
            ),
        )
        for overrides, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                verdict = verify_official_source_operational_acceptance(
                    self.build_bundle(**overrides)
                )
                self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
                self.assertIn(expected_code, self.issue_codes(verdict))

    def test_accepted_items_without_bound_receipt_fail(self) -> None:
        selected = REQUIRED_OFFICIAL_ADAPTER_KEYS[0]

        verdict = verify_official_source_operational_acceptance(
            self.build_bundle(missing_receipt_keys=frozenset({selected}))
        )

        self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
        self.assertIn(
            "SOURCE_MONITORING_SOAK_ACCEPTED_ITEMS_RECEIPT_MISSING",
            self.issue_codes(verdict),
        )
        counts = verdict["per_adapter_counts"][selected]  # type: ignore[index]
        self.assertEqual(counts["missing_import_receipt_run_count"], 1)

    def test_issue_projection_is_bounded_without_losing_total(self) -> None:
        selected = REQUIRED_OFFICIAL_ADAPTER_KEYS[0]
        repeated = (selected,) * (MAX_SOAK_OPERATIONAL_ACCEPTANCE_ISSUES + 6)
        remaining = REQUIRED_OFFICIAL_ADAPTER_KEYS[1:]

        verdict = verify_official_source_operational_acceptance(
            self.build_bundle(
                run_keys=repeated + remaining,
                status_overrides={selected: "DEGRADED"},
            )
        )

        self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
        self.assertGreater(
            verdict["issue_count"],  # type: ignore[arg-type]
            MAX_SOAK_OPERATIONAL_ACCEPTANCE_ISSUES,
        )
        self.assertEqual(
            len(verdict["issues"]),  # type: ignore[arg-type]
            MAX_SOAK_OPERATIONAL_ACCEPTANCE_ISSUES,
        )
        self.assertIs(verdict["issues_truncated"], True)

    def test_config_drift_or_market_activity_fails(self) -> None:
        selected = REQUIRED_OFFICIAL_ADAPTER_KEYS[0]
        cases = (
            (
                {"config_overrides": {selected: "different_config_v1"}},
                "SOURCE_MONITORING_SOAK_CONFIG_VERSION_MISMATCH",
            ),
            (
                {"market_overrides": {selected: 1}},
                "SOURCE_MONITORING_SOAK_MARKET_ACTIVITY_OBSERVED",
            ),
            (
                {"market_overrides": {selected: None}},
                "SOURCE_MONITORING_SOAK_MARKET_ACTIVITY_OBSERVED",
            ),
        )
        for overrides, expected_code in cases:
            with self.subTest(expected_code=expected_code, overrides=overrides):
                verdict = verify_official_source_operational_acceptance(
                    self.build_bundle(**overrides)
                )
                self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
                self.assertIn(expected_code, self.issue_codes(verdict))

    def test_incomplete_ledger_cannot_pass_the_shared_v1_stream(self) -> None:
        bundle = self.build_bundle(include_end=False)
        from backend.source_monitoring.soak_evidence import validate_soak_evidence

        calls = 0

        def one_stream(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return validate_soak_evidence(*args, **kwargs)

        with mock.patch(
            "backend.source_monitoring.soak_verifier.validate_soak_evidence",
            side_effect=one_stream,
        ):
            verdict = verify_official_source_operational_acceptance(bundle)

        self.assertEqual(calls, 1)
        self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
        self.assertEqual(
            verdict["v1_evidence_verdict"]["overall_status"],  # type: ignore[index]
            "INCOMPLETE_UNSEALED",
        )
        self.assertIn(
            "SOURCE_MONITORING_SOAK_V1_EVIDENCE_NOT_VERIFIED",
            self.issue_codes(verdict),
        )

    def test_hash_chain_tampering_fails_at_v1(self) -> None:
        bundle = self.build_bundle()
        ledger = bundle / "ledger.jsonl"
        raw = ledger.read_bytes()
        self.assertIn(b'"status":"SUCCEEDED"', raw)
        ledger.write_bytes(raw.replace(b'"status":"SUCCEEDED"', b'"status":"DEGRADED"', 1))

        verdict = verify_official_source_operational_acceptance(bundle)

        self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
        self.assertIn(
            "SOURCE_MONITORING_SOAK_V1_EVIDENCE_NOT_VERIFIED",
            self.issue_codes(verdict),
        )

    def test_bundle_rejects_extra_database_without_opening_it(self) -> None:
        bundle = self.build_bundle()
        database = bundle / "formal.sqlite3"
        database.write_bytes(b"must-not-open")
        before = database.read_bytes()

        with mock.patch("sqlite3.connect", side_effect=AssertionError("database opened")):
            verdict = verify_official_source_operational_acceptance(bundle)

        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
        self.assertIn(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_BUNDLE_CONTENTS_INVALID",
            self.issue_codes(verdict),
        )
        self.assertEqual(
            verdict["v1_evidence_verdict"]["overall_status"],  # type: ignore[index]
            "NOT_EVALUATED",
        )

    def test_non_native_path_type_is_rejected_without_coercion(self) -> None:
        bundle = self.build_bundle()

        class TrickyPath(str):
            pass

        verdict = verify_official_source_operational_acceptance(TrickyPath(str(bundle)))

        self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
        self.assertIn(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
            self.issue_codes(verdict),
        )

    def test_unc_and_device_paths_are_rejected_before_filesystem_access(self) -> None:
        forbidden_paths = (
            r"\\server\share\bundle",
            r"//server/share/bundle",
            r"\\?\UNC\server\share\bundle",
            r"\\.\GLOBALROOT\Device\Mup\server\share",
            r"\??\UNC\server\share\bundle",
            r"\Device\Mup\server\share\bundle",
        )
        for forbidden_path in forbidden_paths:
            with (
                self.subTest(forbidden_path=forbidden_path),
                mock.patch.object(
                    Path,
                    "expanduser",
                    side_effect=AssertionError("path expansion reached"),
                ),
                mock.patch.object(
                    acceptance_module,
                    "first_reparse_component",
                    side_effect=AssertionError("reparse inspection reached"),
                ),
                mock.patch.object(
                    acceptance_module,
                    "_windows_drive_type",
                    side_effect=AssertionError("drive classification reached"),
                ),
                mock.patch.object(
                    acceptance_module.os,
                    "scandir",
                    side_effect=AssertionError("directory inspection reached"),
                ),
            ):
                verdict = verify_official_source_operational_acceptance(
                    forbidden_path
                )

            self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
            self.assertIn(
                "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
                self.issue_codes(verdict),
            )
            self.assertEqual(verdict["safety"]["network_requests_performed"], 0)

    @unittest.skipUnless(os.name == "nt", "mapped-drive classification is Windows-only")
    def test_remote_mapped_drive_is_rejected_before_path_dereference(self) -> None:
        with (
            mock.patch.object(
                acceptance_module,
                "_windows_drive_type",
                return_value=4,
            ) as drive_type,
            mock.patch.object(
                Path,
                "expanduser",
                side_effect=AssertionError("path expansion reached"),
            ),
            mock.patch.object(
                acceptance_module,
                "first_reparse_component",
                side_effect=AssertionError("reparse inspection reached"),
            ),
            mock.patch.object(
                acceptance_module.os,
                "scandir",
                side_effect=AssertionError("directory inspection reached"),
            ),
        ):
            verdict = verify_official_source_operational_acceptance(
                r"Z:\remote-bundle"
            )

        drive_type.assert_called_once_with("Z:")
        self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
        self.assertIn(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
            self.issue_codes(verdict),
        )
        self.assertEqual(verdict["safety"]["network_requests_performed"], 0)

    def test_symlinked_bundle_is_rejected_when_supported(self) -> None:
        bundle = self.build_bundle()
        alias = self.root / "bundle-alias"
        try:
            os.symlink(bundle, alias, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are unavailable")

        verdict = verify_official_source_operational_acceptance(alias)

        self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
        self.assertIn(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_PATH_INVALID",
            self.issue_codes(verdict),
        )

    def test_ledger_replacement_between_v1_and_v2_is_rejected(self) -> None:
        bundle = self.build_bundle()
        real_v1 = _verify_soak_evidence_with_observer

        def replace_after_v1(*args: object, **kwargs: object) -> dict[str, object]:
            verdict = real_v1(*args, **kwargs)
            ledger = Path(args[0])
            replacement = ledger.with_name("replacement.jsonl")
            replacement.write_bytes(ledger.read_bytes())
            os.replace(replacement, ledger)
            return verdict

        with mock.patch(
            "backend.source_monitoring.soak_acceptance._verify_soak_evidence_with_observer",
            side_effect=replace_after_v1,
        ):
            verdict = _verify_official_source_operational_acceptance_paths(
                plan_path=bundle / "plan.json",
                baseline_inventory_path=bundle / "baseline-inventory.json",
                ledger_path=bundle / "ledger.jsonl",
                final_inventory_path=bundle / "final-inventory.json",
            )

        self.assertEqual(verdict["source_acceptance_verdict"], "FAIL")
        self.assertIn(
            "SOURCE_MONITORING_SOAK_OPERATIONAL_ARTIFACT_CHANGED",
            self.issue_codes(verdict),
        )


if __name__ == "__main__":
    unittest.main()
