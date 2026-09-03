from __future__ import annotations

import inspect
import io
import json
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backend.source_monitoring.contracts import canonical_sha256
from backend.source_monitoring.soak_plan import build_source_monitoring_soak_plan
from backend.source_monitoring_soak_cli import main
import backend.source_monitoring_soak_cli as soak_cli


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


class _Settings:
    enabled = True
    auto_start = True
    official_only = True
    allow_readonly_market = False
    dry_run = False
    trading_impact_rules_enabled = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "auto_start": True,
            "official_only": True,
            "allow_readonly_market": False,
            "dry_run": False,
            "trading_impact_rules_enabled": False,
            "max_items_per_run": 10,
            "initial_mode": "seed_only",
            "catch_up_max_items": 0,
            "initial_preview_sha256": "",
            "from_time": "",
        }


class _Registry:
    official_only = True
    adapter_keys = ("federal_reserve", "treasury_releases")

    def __init__(self) -> None:
        self._metadata = {
            key: SimpleNamespace(
                adapter_key=key,
                config_version=f"{key}_v1",
                official_source=True,
                max_market_calls_per_poll=0,
                execution_capability="none",
                live_trading_allowed=False,
            )
            for key in self.adapter_keys
        }

    def metadata_for(self, key: str) -> Any:
        return self._metadata[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "official_only": True,
            "adapter_count": len(self.adapter_keys),
            "adapters": [
                {
                    "adapter_key": key,
                    "config_version": self._metadata[key].config_version,
                    "official_source": True,
                    "max_market_calls_per_poll": 0,
                    "execution_capability": "none",
                    "live_trading_allowed": False,
                }
                for key in self.adapter_keys
            ],
        }


class _Repository:
    def __init__(self, *, checkpoint: str = SHA_A) -> None:
        self.checkpoint = checkpoint

    def get_state(self, key: str) -> dict[str, Any]:
        return {
            "adapter_key": key,
            "enabled": True,
            "config_version": f"{key}_v1",
            "state_version": 3,
            "checkpoint_sha256": self.checkpoint,
        }


class _Owner:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.held = True

    def assert_held_for(self, _path: Any) -> None:
        self.events.append("owner.assert")
        if not self.held:
            raise AssertionError("owner is not held")

    def release(self) -> None:
        self.events.append("owner.release")
        self.held = False


class _Runtime:
    def __init__(
        self,
        settings: _Settings,
        registry: _Registry,
        repository: _Repository,
        events: list[str],
        *,
        initially_alive: bool = False,
        wait_result: bool = True,
    ) -> None:
        self.settings = settings
        self.scheduler = SimpleNamespace(
            registry=registry,
            supervisor=SimpleNamespace(repository=repository),
        )
        self.events = events
        self.alive = initially_alive
        self.wait_result = wait_result

    def snapshot(self) -> dict[str, Any]:
        self.events.append("runtime.snapshot")
        return {"thread_alive": self.alive}

    def stop(self) -> bool:
        self.events.append("runtime.stop")
        return not self.alive

    def wait_until_stopped(self) -> bool:
        self.events.append("runtime.wait")
        if self.wait_result:
            self.alive = False
        return self.wait_result


def _inventory(digest: str = SHA_B) -> dict[str, Any]:
    return {"run_count": 0, "inventory_sha256": digest, "runs": []}


def _verdict(plan: dict[str, Any], *, status: str = "EVIDENCE_VERIFIED") -> dict[str, Any]:
    return {
        "version": "source_monitoring_soak_verdict_v1",
        "overall_status": status,
        "continuity_verdict": "PASS",
        "production_binding_verdict": "PASS",
        "database_verdict": "PASS",
        "source_acceptance_verdict": "NOT_EVALUATED",
        "overall_acceptance": "NOT_CLAIMED",
        "identity": {
            "campaign_id": plan["campaign_id"],
            "session_id": plan["session_id"],
            "runtime_id": "source_monitor_runtime_" + "1" * 32,
        },
        "counts": {
            "ledger_record_count": 3,
            "runtime_sample_count": 1,
            "run_terminal_count": 2,
            "unique_terminal_run_count": 2,
            "expected_adapter_count": 2,
            "covered_adapter_count": 2,
        },
        "timing": {
            "required_duration_ns": 86_400_000_000_000,
            "declared_sample_interval_ns": 5_000_000_000,
            "declared_maximum_sample_gap_ns": 120_000_000_000,
            "observed_elapsed_ns": 86_400_000_000_000,
            "maximum_observed_sample_gap_ns": 5_000_000_000,
        },
        "bindings": {
            "ledger_terminal": True,
            "last_record_sha256": SHA_A,
            "baseline_inventory_sha256": SHA_B,
            "final_inventory_sha256": SHA_C,
            "database_delta_verdict_sha256": SHA_D,
            "expected_production_bindings_sha256": SHA_A,
            "observed_production_bindings_sha256": SHA_A,
        },
        "issue_count": 0,
        "verdict_sha256": SHA_D,
        "issues": [],
        "issues_truncated": False,
        "safety": dict(soak_cli._VERIFIER_SAFETY),
    }


class _Harness:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.settings = _Settings()
        self.registry = _Registry()
        self.repository = _Repository()
        self.baseline = _inventory()
        self.final = _inventory(SHA_C)
        self.files: dict[str, Any] = {}
        self.last_plan: dict[str, Any] | None = None
        self.owner: _Owner | None = None
        self.runtime: _Runtime | None = None
        self.runner_kwargs: dict[str, Any] | None = None
        self.signal_seen = False
        self.wait_result = True
        self.initially_alive = False

    def owner_acquirer(self, _path: Any) -> _Owner:
        self.events.append("owner.acquire")
        self.owner = _Owner(self.events)
        return self.owner

    def readiness(self, _path: Any) -> dict[str, Any]:
        self.events.append("readiness")
        return {"startup_identity": {"main": SHA_A}, "schema_sha256": SHA_D}

    def inventory_builder(self, _path: Any) -> dict[str, Any]:
        self.events.append("inventory.build")
        return self.baseline

    def inventory_writer(self, *, inventory: Any, artifact_path: Path) -> dict[str, Any]:
        self.events.append("inventory.write." + artifact_path.name)
        self.files[artifact_path.name] = inventory
        artifact_path.write_text("inventory", encoding="utf-8")
        return {"inventory_sha256": inventory["inventory_sha256"]}

    def inventory_loader(self, path: Path) -> dict[str, Any]:
        self.events.append("inventory.load." + path.name)
        return self.files[path.name]

    def plan_writer(self, *, artifact_path: Path, plan: Any) -> dict[str, Any]:
        self.events.append("plan.write")
        self.files[artifact_path.name] = plan
        self.last_plan = plan
        artifact_path.write_text("plan", encoding="utf-8")
        return {"preview_sha256": plan["preview_sha256"]}

    def plan_loader(self, path: Path) -> dict[str, Any]:
        self.events.append("plan.load")
        return self.files[path.name]

    def runtime_builder(self, _store: Any, settings: Any, _observer: Any) -> _Runtime:
        self.events.append("runtime.build")
        self.runtime = _Runtime(
            settings,
            self.registry,
            self.repository,
            self.events,
            initially_alive=self.initially_alive,
            wait_result=self.wait_result,
        )
        return self.runtime

    def runner_factory(self, **kwargs: Any) -> Any:
        self.events.append("runner.build")
        self.runner_kwargs = kwargs
        harness = self

        class Runner:
            def run(self) -> dict[str, Any]:
                harness.events.append("runner.run")
                if harness.signal_seen:
                    handler = signal.getsignal(signal.SIGINT)
                    handler(signal.SIGINT, None)
                    if kwargs["stop_event"].is_set() is not True:
                        raise AssertionError("signal did not set stop_event")
                kwargs["baseline_inventory_sink"](harness.baseline)
                Path(kwargs["ledger_path"]).write_text("ledger", encoding="utf-8")
                kwargs["final_inventory_sink"](harness.final)
                return {"end_reason": "duration_reached"}

        return Runner()

    def verifier(self, _ledger: Path, **kwargs: Any) -> dict[str, Any]:
        self.events.append("verify")
        if set(kwargs["expected_bindings"]) != set(soak_cli._PRODUCTION_BINDING_FIELDS):
            raise AssertionError("missing production binding")
        expected_keys = tuple(
            row["adapter_key"] for row in self.last_plan["enabled_adapters"]
        )
        if kwargs["expected_enabled_adapter_keys"] != expected_keys:
            raise AssertionError("adapter coverage keys drifted")
        return _verdict(self.last_plan)

    def dependencies(self) -> soak_cli._SoakCliDependencies:
        return soak_cli._SoakCliDependencies(
            database_path_loader=lambda: Path("configured.sqlite3"),
            owner_acquirer=self.owner_acquirer,
            readiness_checker=self.readiness,
            store_opener=lambda _path: object(),
            settings_loader=lambda: self.settings,
            registry_builder=lambda _settings: self.registry,
            repository_builder=lambda _store: self.repository,
            inventory_builder=self.inventory_builder,
            inventory_writer=self.inventory_writer,
            inventory_loader=self.inventory_loader,
            plan_builder=build_source_monitoring_soak_plan,
            plan_writer=self.plan_writer,
            plan_loader=self.plan_loader,
            observer_factory=lambda _path: SimpleNamespace(await_activation=lambda: None),
            runtime_builder=self.runtime_builder,
            runner_factory=self.runner_factory,
            verifier=self.verifier,
            canonical_sha256=canonical_sha256,
            code_identity_builder=lambda: SHA_C,
            id_factory=lambda kind: f"source_soak_{kind}_" + ("1" if kind == "campaign" else "2") * 32,
        )


def _invoke(arguments: list[str], deps: soak_cli._SoakCliDependencies) -> tuple[int, dict[str, Any], str]:
    output = io.StringIO()
    code = soak_cli._main_with_dependencies(
        arguments,
        output=output,
        dependencies=deps,
    )
    raw = output.getvalue()
    return code, json.loads(raw), raw


class SourceMonitoringSoakCliTests(unittest.TestCase):
    def test_public_main_has_no_dependency_or_duration_seam(self) -> None:
        self.assertEqual(list(inspect.signature(main).parameters), ["argv", "output"])
        self.assertEqual(soak_cli.__all__, ["main"])

    def test_help_and_unknown_duration_never_resolve_database(self) -> None:
        calls: list[str] = []
        deps = soak_cli._SoakCliDependencies(
            database_path_loader=lambda: calls.append("database")
        )
        help_code, help_payload, _ = _invoke(["--help"], deps)
        self.assertEqual(help_code, 0)
        self.assertEqual(help_payload["required_duration_hours"], 24)
        code, payload, _ = _invoke(
            ["preview", "--bundle", "ignored", "--mode", "official", "--hours", "1"],
            deps,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "SOURCE_MONITORING_SOAK_ARGUMENT_INVALID")
        self.assertEqual(calls, [])

    def test_wrong_confirmation_fails_before_bundle_or_owner(self) -> None:
        calls: list[str] = []
        deps = soak_cli._SoakCliDependencies(
            database_path_loader=lambda: calls.append("database")
        )
        code, payload, _ = _invoke(
            [
                "start",
                "--bundle",
                "does-not-exist",
                "--confirm",
                "wrong",
                "--preview-sha256",
                SHA_A,
            ],
            deps,
        )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "SOURCE_MONITORING_SOAK_CONFIRMATION_REQUIRED")
        self.assertEqual(calls, [])

    def test_preview_is_owner_scoped_read_only_and_writes_fixed_two_artifacts(self) -> None:
        harness = _Harness()
        with tempfile.TemporaryDirectory() as directory:
            code, payload, raw = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"],
                harness.dependencies(),
            )
            self.assertEqual(code, 0, raw)
            self.assertTrue(payload["ok"])
            self.assertEqual(set(Path(directory).iterdir()), {
                Path(directory) / "plan.json",
                Path(directory) / "baseline-inventory.json",
            })
        self.assertLess(harness.events.index("owner.acquire"), harness.events.index("readiness"))
        self.assertLess(harness.events.index("inventory.build"), harness.events.index("inventory.write.baseline-inventory.json"))
        self.assertLess(harness.events.index("inventory.write.baseline-inventory.json"), harness.events.index("plan.write"))
        self.assertNotIn("runtime.build", harness.events)
        self.assertNotIn("runner.run", harness.events)
        self.assertEqual(harness.events[-1], "owner.release")

    def test_preview_rejects_running_baseline(self) -> None:
        harness = _Harness()
        harness.baseline = {
            "run_count": 1,
            "inventory_sha256": SHA_B,
            "runs": [{"status": "RUNNING"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            code, payload, _ = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"],
                harness.dependencies(),
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["error_code"], "SOURCE_MONITORING_SOAK_RUNNING_ROWS_PRESENT")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_owner_conflict_is_bounded_and_does_not_leak_path(self) -> None:
        secret_path = "C:/private/formal.sqlite3"

        def conflict(_path: Any) -> Any:
            raise soak_cli._CliFailure("SOURCE_MONITORING_INSTANCE_ACTIVE", exit_code=3)

        deps = soak_cli._SoakCliDependencies(
            database_path_loader=lambda: secret_path,
            owner_acquirer=conflict,
        )
        with tempfile.TemporaryDirectory() as directory:
            code, payload, raw = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"], deps
            )
        self.assertEqual(code, 3)
        self.assertEqual(payload["error_code"], "SOURCE_MONITORING_INSTANCE_ACTIVE")
        self.assertNotIn(secret_path, raw)

    def test_migration_required_releases_owner_before_return(self) -> None:
        harness = _Harness()

        def migration(_path: Any) -> Any:
            raise soak_cli._CliFailure("SOURCE_MONITORING_DATABASE_MIGRATION_REQUIRED")

        deps = harness.dependencies()
        deps = soak_cli._SoakCliDependencies(
            **{
                field: getattr(deps, field)
                for field in deps.__dataclass_fields__
                if field != "readiness_checker"
            },
            readiness_checker=migration,
        )
        with tempfile.TemporaryDirectory() as directory:
            code, payload, _ = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"], deps
            )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "SOURCE_MONITORING_DATABASE_MIGRATION_REQUIRED")
        self.assertEqual(harness.events, ["owner.acquire", "owner.release"])

    def test_start_loads_plan_and_baseline_before_database_resolution(self) -> None:
        calls: list[str] = []

        def fail_plan(_path: Path) -> Any:
            calls.append("plan")
            raise soak_cli._CliFailure("SOURCE_MONITORING_SOAK_PLAN_INVALID")

        deps = soak_cli._SoakCliDependencies(
            plan_loader=fail_plan,
            database_path_loader=lambda: calls.append("database"),
        )
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "plan.json").write_text("x", encoding="utf-8")
            Path(directory, "baseline-inventory.json").write_text("x", encoding="utf-8")
            code, _, _ = _invoke(
                [
                    "start",
                    "--bundle",
                    directory,
                    "--confirm",
                    soak_cli.SOURCE_MONITORING_SOAK_CONFIRMATION,
                    "--preview-sha256",
                    SHA_A,
                ],
                deps,
            )
        self.assertEqual(code, 2)
        self.assertEqual(calls, ["plan"])

    def test_start_rejects_baseline_drift_before_runtime(self) -> None:
        harness = _Harness()
        deps = harness.dependencies()
        with tempfile.TemporaryDirectory() as directory:
            preview_code, preview, _ = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"], deps
            )
            self.assertEqual(preview_code, 0)
            harness.baseline = _inventory(SHA_A)
            code, payload, _ = _invoke(
                [
                    "start",
                    "--bundle",
                    directory,
                    "--confirm",
                    soak_cli.SOURCE_MONITORING_SOAK_CONFIRMATION,
                    "--preview-sha256",
                    preview["preview_sha256"],
                ],
                deps,
            )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "SOURCE_MONITORING_SOAK_BASELINE_DRIFT")
        self.assertNotIn("runtime.build", harness.events)

    def test_start_rejects_plan_binding_drift_before_runtime(self) -> None:
        harness = _Harness()
        preview_deps = harness.dependencies()
        with tempfile.TemporaryDirectory() as directory:
            _, preview, _ = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"],
                preview_deps,
            )
            start_deps = soak_cli._SoakCliDependencies(
                **{
                    field: getattr(preview_deps, field)
                    for field in preview_deps.__dataclass_fields__
                    if field != "code_identity_builder"
                },
                code_identity_builder=lambda: SHA_D,
            )
            code, payload, _ = _invoke(
                [
                    "start",
                    "--bundle",
                    directory,
                    "--confirm",
                    soak_cli.SOURCE_MONITORING_SOAK_CONFIRMATION,
                    "--preview-sha256",
                    preview["preview_sha256"],
                ],
                start_deps,
            )
        self.assertEqual(code, 2)
        self.assertEqual(payload["error_code"], "SOURCE_MONITORING_SOAK_PLAN_DRIFT")
        self.assertNotIn("runtime.build", harness.events)

    def test_start_passes_full_descriptors_owner_and_no_timing_override(self) -> None:
        harness = _Harness()
        deps = harness.dependencies()
        with tempfile.TemporaryDirectory() as directory:
            _, preview, _ = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"], deps
            )
            code, payload, raw = _invoke(
                [
                    "start",
                    "--bundle",
                    directory,
                    "--confirm",
                    soak_cli.SOURCE_MONITORING_SOAK_CONFIRMATION,
                    "--preview-sha256",
                    preview["preview_sha256"],
                ],
                deps,
            )
        self.assertEqual(code, 0, raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            harness.runner_kwargs["expected_enabled_adapters"],
            tuple(harness.last_plan["enabled_adapters"]),
        )
        self.assertIs(harness.runner_kwargs["database_owner"], harness.owner)
        self.assertTrue(callable(harness.runner_kwargs["code_identity_checker"]))
        self.assertEqual(harness.runner_kwargs["code_identity_checker"](), SHA_C)
        self.assertFalse(any(key.startswith("_") for key in harness.runner_kwargs))
        snapshot_index = max(
            index
            for index, event in enumerate(harness.events)
            if event == "runtime.snapshot"
        )
        release_index = max(
            index
            for index, event in enumerate(harness.events)
            if event == "owner.release"
        )
        self.assertLess(snapshot_index, release_index)

    def test_signal_handler_sets_only_runner_stop_event(self) -> None:
        harness = _Harness()
        harness.signal_seen = True
        deps = harness.dependencies()
        with tempfile.TemporaryDirectory() as directory:
            _, preview, _ = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"], deps
            )
            code, _, _ = _invoke(
                [
                    "start",
                    "--bundle",
                    directory,
                    "--confirm",
                    soak_cli.SOURCE_MONITORING_SOAK_CONFIRMATION,
                    "--preview-sha256",
                    preview["preview_sha256"],
                ],
                deps,
            )
        self.assertEqual(code, 0)
        self.assertTrue(harness.runner_kwargs["stop_event"].is_set())

    def test_stop_timeout_waits_for_quiescence_before_owner_release(self) -> None:
        harness = _Harness()
        harness.initially_alive = True
        deps = harness.dependencies()
        with tempfile.TemporaryDirectory() as directory:
            _, preview, _ = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"], deps
            )
            code, _, _ = _invoke(
                [
                    "start",
                    "--bundle",
                    directory,
                    "--confirm",
                    soak_cli.SOURCE_MONITORING_SOAK_CONFIRMATION,
                    "--preview-sha256",
                    preview["preview_sha256"],
                ],
                deps,
            )
        self.assertEqual(code, 0)
        wait_index = max(index for index, event in enumerate(harness.events) if event == "runtime.wait")
        release_index = max(index for index, event in enumerate(harness.events) if event == "owner.release")
        self.assertLess(wait_index, release_index)

    def test_nonquiescent_runtime_owner_is_not_released(self) -> None:
        harness = _Harness()
        harness.initially_alive = True
        harness.wait_result = False
        deps = harness.dependencies()
        before = len(soak_cli._RETAINED_LIVE_OWNERS)
        try:
            with tempfile.TemporaryDirectory() as directory:
                _, preview, _ = _invoke(
                    ["preview", "--bundle", directory, "--mode", "official"], deps
                )
                code, payload, _ = _invoke(
                    [
                        "start",
                        "--bundle",
                        directory,
                        "--confirm",
                        soak_cli.SOURCE_MONITORING_SOAK_CONFIRMATION,
                        "--preview-sha256",
                        preview["preview_sha256"],
                    ],
                    deps,
                )
            self.assertEqual(code, 1)
            self.assertEqual(payload["error_code"], "SOURCE_MONITORING_SOAK_RUNTIME_NOT_QUIESCENT")
            self.assertTrue(harness.owner.held)
            self.assertEqual(len(soak_cli._RETAINED_LIVE_OWNERS), before + 1)
        finally:
            if len(soak_cli._RETAINED_LIVE_OWNERS) > before:
                owner, _runtime = soak_cli._RETAINED_LIVE_OWNERS.pop()
                owner.release()

    def test_verify_reads_bundle_only_and_passes_all_expected_bindings(self) -> None:
        harness = _Harness()
        deps = harness.dependencies()
        with tempfile.TemporaryDirectory() as directory:
            _, _, _ = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"], deps
            )
            Path(directory, "ledger.jsonl").write_text("ledger", encoding="utf-8")
            harness.files["final-inventory.json"] = harness.final
            Path(directory, "final-inventory.json").write_text("final", encoding="utf-8")
            calls_before = list(harness.events)
            verify_deps = soak_cli._SoakCliDependencies(
                plan_loader=harness.plan_loader,
                inventory_loader=harness.inventory_loader,
                verifier=harness.verifier,
                database_path_loader=lambda: (_ for _ in ()).throw(
                    AssertionError("verify resolved database")
                ),
                runtime_builder=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("verify built runtime")
                ),
            )
            code, payload, raw = _invoke(
                ["verify", "--bundle", directory], verify_deps
            )
        self.assertEqual(code, 0, raw)
        self.assertTrue(payload["ok"])
        new_events = harness.events[len(calls_before):]
        self.assertEqual(
            new_events,
            [
                "plan.load",
                "inventory.load.baseline-inventory.json",
                "inventory.load.final-inventory.json",
                "verify",
            ],
        )
        self.assertEqual(payload["safety"]["database_reads_performed"], 0)

    def test_outputs_are_bounded_and_never_include_paths_or_inventories(self) -> None:
        harness = _Harness()
        secret = "C:/secret/formal.sqlite3"
        deps = harness.dependencies()
        deps = soak_cli._SoakCliDependencies(
            **{
                field: getattr(deps, field)
                for field in deps.__dataclass_fields__
                if field != "database_path_loader"
            },
            database_path_loader=lambda: secret,
        )
        with tempfile.TemporaryDirectory() as directory:
            code, payload, raw = _invoke(
                ["preview", "--bundle", directory, "--mode", "official"], deps
            )
        self.assertEqual(code, 0, raw)
        self.assertLessEqual(len(raw.encode("utf-8")), soak_cli._MAX_OUTPUT_BYTES)
        self.assertNotIn(secret, raw)
        self.assertNotIn("runs", payload)


if __name__ == "__main__":
    unittest.main()
