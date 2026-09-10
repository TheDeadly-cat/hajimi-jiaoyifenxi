from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

import backend.source_monitoring.soak_plan as plan_module  # noqa: E402
from backend.source_monitoring.contracts import canonical_json, canonical_sha256  # noqa: E402
from backend.source_monitoring.soak_plan import (  # noqa: E402
    MAX_SOAK_PLAN_BYTES,
    SOAK_BASELINE_INVENTORY_FILENAME,
    SOAK_FINAL_INVENTORY_FILENAME,
    SOAK_LEDGER_FILENAME,
    SOAK_PLAN_FILENAME,
    SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS,
    SOURCE_MONITORING_SOAK_PLAN_VERSION,
    SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS,
    SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS,
    SourceMonitoringSoakPlanError,
    build_source_monitoring_soak_plan,
    load_source_monitoring_soak_plan,
    validate_source_monitoring_soak_plan,
    write_source_monitoring_soak_plan_exclusive,
)


CAMPAIGN_ID = "source_soak_campaign_" + "a" * 32
SESSION_ID = "source_soak_session_" + "b" * 32
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64


def adapter(
    adapter_key: str,
    *,
    config_version: str | None = None,
    state_version: int = 3,
    checkpoint_sha256: str = HASH_F,
) -> dict[str, object]:
    return {
        "adapter_key": adapter_key,
        "config_version": config_version or f"{adapter_key}_config_v1",
        "state_version": state_version,
        "checkpoint_sha256": checkpoint_sha256,
    }


def build_plan(
    *,
    enabled_adapters: object | None = None,
) -> dict[str, object]:
    return build_source_monitoring_soak_plan(
        campaign_id=CAMPAIGN_ID,
        session_id=SESSION_ID,
        settings_sha256=HASH_A,
        registry_sha256=HASH_B,
        code_identity_sha256=HASH_C,
        db_startup_identity_sha256=HASH_D,
        db_schema_sha256=HASH_E,
        baseline_run_count=17,
        baseline_run_inventory_sha256=HASH_F,
        enabled_adapters=(
            [adapter("sec_filings"), adapter("federal_reserve")]
            if enabled_adapters is None
            else enabled_adapters
        ),
    )


class SourceMonitoringSoakPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="ai-studio-soak-plan-test-"
        )
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def plan_path(self, parent: Path | None = None) -> Path:
        return (parent or self.root) / SOAK_PLAN_FILENAME

    def assert_plan_error(self, code: str, callback: object) -> None:
        if not callable(callback):
            self.fail("callback must be callable")
        with self.assertRaises(SourceMonitoringSoakPlanError) as captured:
            callback()
        self.assertEqual(captured.exception.code, code)

    def test_builder_sorts_and_seals_one_fixed_path_free_official_plan(self) -> None:
        inputs = [adapter("sec_filings"), adapter("federal_reserve")]
        plan = build_plan(enabled_adapters=inputs)

        self.assertEqual(plan["version"], SOURCE_MONITORING_SOAK_PLAN_VERSION)
        self.assertEqual(plan["mode"], "official")
        self.assertEqual(
            plan["required_duration_ns"],
            SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS,
        )
        self.assertEqual(
            plan["sample_interval_ns"],
            SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS,
        )
        self.assertEqual(
            plan["maximum_sample_gap_ns"],
            SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS,
        )
        self.assertEqual(
            [entry["adapter_key"] for entry in plan["enabled_adapters"]],
            ["federal_reserve", "sec_filings"],
        )
        self.assertEqual(plan["enabled_adapter_count"], 2)
        self.assertEqual(
            plan["enabled_adapter_keys_sha256"],
            canonical_sha256(["federal_reserve", "sec_filings"]),
        )
        self.assertEqual(
            plan["artifacts"],
            {
                "plan": SOAK_PLAN_FILENAME,
                "baseline_inventory": SOAK_BASELINE_INVENTORY_FILENAME,
                "ledger": SOAK_LEDGER_FILENAME,
                "final_inventory": SOAK_FINAL_INVENTORY_FILENAME,
            },
        )
        unsigned = copy.deepcopy(plan)
        unsigned.pop("preview_sha256")
        self.assertEqual(plan["preview_sha256"], canonical_sha256(unsigned))

        serialized = canonical_json(plan)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("database_path", serialized)
        self.assertNotIn("artifact_path", serialized)

        inputs[0]["adapter_key"] = "mutated"
        self.assertEqual(plan["enabled_adapters"][1]["adapter_key"], "sec_filings")

    def test_validate_returns_a_defensive_copy(self) -> None:
        plan = build_plan()
        validated = validate_source_monitoring_soak_plan(plan)
        self.assertEqual(validated, plan)
        self.assertIsNot(validated, plan)
        self.assertIsNot(validated["enabled_adapters"], plan["enabled_adapters"])
        validated["enabled_adapters"][0]["state_version"] = 99
        self.assertEqual(plan["enabled_adapters"][0]["state_version"], 3)

    def test_builder_rejects_empty_duplicate_or_noncanonical_adapters(self) -> None:
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTERS_INVALID",
            lambda: build_plan(enabled_adapters=[]),
        )
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTERS_INVALID",
            lambda: build_plan(
                enabled_adapters=[adapter("sec_filings"), adapter("sec_filings")]
            ),
        )
        for mutation in (
            {**adapter("sec_filings"), "adapter_key": "FUTU"},
            {**adapter("sec_filings"), "config_version": "bad/version"},
            {**adapter("sec_filings"), "state_version": True},
            {**adapter("sec_filings"), "state_version": 0},
            {**adapter("sec_filings"), "checkpoint_sha256": "A" * 64},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(SourceMonitoringSoakPlanError):
                    build_plan(enabled_adapters=[mutation])

    def test_validate_rejects_schema_identity_hash_and_integer_drift(self) -> None:
        mutations = (
            ("extra", 1),
            ("campaign_id", "source_soak_campaign_short"),
            ("settings_sha256", "A" * 64),
            ("baseline_run_count", True),
        )
        for field, value in mutations:
            candidate = build_plan()
            candidate[field] = value
            with self.subTest(field=field):
                with self.assertRaises(SourceMonitoringSoakPlanError):
                    validate_source_monitoring_soak_plan(candidate)

    def test_validate_rejects_any_policy_or_mode_override(self) -> None:
        for field, value in (
            ("mode", "futu"),
            ("required_duration_ns", SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS - 1),
            ("sample_interval_ns", SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS + 1),
            (
                "maximum_sample_gap_ns",
                SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS + 1,
            ),
        ):
            candidate = build_plan()
            candidate[field] = value
            with self.subTest(field=field):
                with self.assertRaises(SourceMonitoringSoakPlanError):
                    validate_source_monitoring_soak_plan(candidate)

    def test_validate_rejects_adapter_order_count_key_seal_and_preview_drift(self) -> None:
        candidate = build_plan()
        candidate["enabled_adapters"].reverse()
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTERS_INVALID",
            lambda: validate_source_monitoring_soak_plan(candidate),
        )

        candidate = build_plan()
        candidate["enabled_adapter_count"] = 1
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_ADAPTERS_INVALID",
            lambda: validate_source_monitoring_soak_plan(candidate),
        )

        candidate = build_plan()
        candidate["enabled_adapter_keys_sha256"] = HASH_A
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_SEAL_INVALID",
            lambda: validate_source_monitoring_soak_plan(candidate),
        )

        candidate = build_plan()
        candidate["baseline_run_count"] += 1
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_SEAL_INVALID",
            lambda: validate_source_monitoring_soak_plan(candidate),
        )

    def test_validate_rejects_artifact_name_or_shape_drift(self) -> None:
        candidate = build_plan()
        candidate["artifacts"]["ledger"] = "alternate.jsonl"
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_ARTIFACTS_INVALID",
            lambda: validate_source_monitoring_soak_plan(candidate),
        )

        candidate = build_plan()
        candidate["artifacts"]["database"] = "studio.sqlite3"
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_SCHEMA_INVALID",
            lambda: validate_source_monitoring_soak_plan(candidate),
        )

    def test_exclusive_write_is_exact_canonical_fsynced_and_loadable(self) -> None:
        plan = build_plan()
        path = self.plan_path()
        real_fsync = os.fsync
        fsync_calls: list[int] = []

        def tracked_fsync(descriptor: int) -> None:
            fsync_calls.append(descriptor)
            real_fsync(descriptor)

        with mock.patch.object(plan_module.os, "fsync", side_effect=tracked_fsync):
            receipt = write_source_monitoring_soak_plan_exclusive(path, plan)

        expected = canonical_json(plan).encode("utf-8")
        self.assertEqual(path.read_bytes(), expected)
        self.assertGreaterEqual(len(fsync_calls), 1)
        self.assertEqual(receipt["filename"], SOAK_PLAN_FILENAME)
        self.assertEqual(receipt["preview_sha256"], plan["preview_sha256"])
        self.assertEqual(receipt["bytes_written"], len(expected))
        self.assertEqual(load_source_monitoring_soak_plan(path), plan)

    def test_exclusive_write_never_overwrites_an_existing_plan(self) -> None:
        path = self.plan_path()
        original = b"preexisting"
        path.write_bytes(original)
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_EXISTS",
            lambda: write_source_monitoring_soak_plan_exclusive(path, build_plan()),
        )
        self.assertEqual(path.read_bytes(), original)

    def test_write_requires_fixed_filename_and_existing_directory(self) -> None:
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            lambda: write_source_monitoring_soak_plan_exclusive(
                self.root / "other.json",
                build_plan(),
            ),
        )
        missing_parent = self.root / "missing"
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            lambda: write_source_monitoring_soak_plan_exclusive(
                self.plan_path(missing_parent),
                build_plan(),
            ),
        )
        self.assertFalse(missing_parent.exists())

    def test_write_failure_is_bounded(self) -> None:
        with mock.patch.object(plan_module.os, "fsync", side_effect=OSError("disk")):
            self.assert_plan_error(
                "SOURCE_MONITORING_SOAK_PLAN_WRITE_FAILED",
                lambda: write_source_monitoring_soak_plan_exclusive(
                    self.plan_path(),
                    build_plan(),
                ),
            )

    def test_load_rejects_noncanonical_duplicate_and_oversized_json(self) -> None:
        path = self.plan_path()
        plan = build_plan()
        path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_FORMAT_INVALID",
            lambda: load_source_monitoring_soak_plan(path),
        )

        path.unlink()
        path.write_text('{"duplicate":1,"duplicate":2}', encoding="utf-8")
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_FORMAT_INVALID",
            lambda: load_source_monitoring_soak_plan(path),
        )

        path.unlink()
        path.write_bytes(b"x" * (MAX_SOAK_PLAN_BYTES + 1))
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_SIZE_INVALID",
            lambda: load_source_monitoring_soak_plan(path),
        )

    def test_load_rejects_canonical_content_with_a_broken_preview_seal(self) -> None:
        plan = build_plan()
        plan["baseline_run_count"] += 1
        self.plan_path().write_bytes(canonical_json(plan).encode("utf-8"))
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_SEAL_INVALID",
            lambda: load_source_monitoring_soak_plan(self.plan_path()),
        )

    def test_load_rejects_hard_link_alias(self) -> None:
        source_parent = self.root / "source"
        alias_parent = self.root / "alias"
        source_parent.mkdir()
        alias_parent.mkdir()
        source = self.plan_path(source_parent)
        alias = self.plan_path(alias_parent)
        write_source_monitoring_soak_plan_exclusive(source, build_plan())
        try:
            os.link(source, alias)
        except OSError as exc:
            self.skipTest(f"hard links unavailable: {exc}")
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            lambda: load_source_monitoring_soak_plan(alias),
        )

    def test_write_rejects_a_symlinked_parent(self) -> None:
        real_parent = self.root / "real"
        alias_parent = self.root / "alias"
        real_parent.mkdir()
        try:
            alias_parent.symlink_to(real_parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        self.assert_plan_error(
            "SOURCE_MONITORING_SOAK_PLAN_PATH_INVALID",
            lambda: write_source_monitoring_soak_plan_exclusive(
                self.plan_path(alias_parent),
                build_plan(),
            ),
        )
        self.assertFalse(self.plan_path(real_parent).exists())


if __name__ == "__main__":
    unittest.main()
