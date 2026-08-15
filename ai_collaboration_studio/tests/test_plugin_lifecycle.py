from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend import http_server  # noqa: E402
from backend.candidate_experiment import (  # noqa: E402
    CandidateExperimentError,
    CandidateExperimentService,
)
from backend.capability_packs import capability_pack_prompt  # noqa: E402
from backend.decision_lineage import canonical_sha256  # noqa: E402
from backend.domain_adapters import (  # noqa: E402
    DEFAULT_DOMAIN_ADAPTERS,
    DomainAdapterError,
)
from backend.plugin_lifecycle import (  # noqa: E402
    PLUGIN_LIFECYCLE_EVENT_VERSION,
    PLUGIN_LIFECYCLE_EVENT_VERSION_V1,
    PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION,
    PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION,
    PluginLifecycleError,
    apply_lifecycle_action,
    available_lifecycle_actions,
    plugin_lifecycle_targets,
    snapshot_target_refs,
    validate_lifecycle_resolution,
)
from backend.orchestrator import DiscussionOrchestrator  # noqa: E402
from backend.storage_sample_acceptance import StorageSampleAcceptance  # noqa: E402
from backend.store import StudioStore  # noqa: E402
from backend.turn_contract import (  # noqa: E402
    CANDIDATE_RISK_REVIEW_VERSION,
    TURN_CONTRACT_VERSION,
)
from backend.turn_envelope import (  # noqa: E402
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
)


class _ExecuteFaultConnection:
    """Delegate SQLite while failing at one deterministic transaction boundary."""

    def __init__(self, connection: sqlite3.Connection, fail_prefix: str) -> None:
        self._connection = connection
        self._fail_prefix = fail_prefix

    def execute(self, sql, parameters=()):
        normalized = " ".join(str(sql).split()).upper()
        if normalized.startswith(self._fail_prefix):
            raise RuntimeError("injected lifecycle head update failure")
        return self._connection.execute(sql, parameters)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def close(self) -> None:
        self._connection.close()

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _NoMarketCalls:
    def __init__(self) -> None:
        self.call_count = 0

    def __getattr__(self, name):
        def fail_if_called(*_args, **_kwargs):
            self.call_count += 1
            raise AssertionError(f"market boundary was called: {name}")

        return fail_if_called


class _NoCallProvider:
    provider_id = "deepseek"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, **_kwargs):
        self.calls += 1
        raise AssertionError("provider boundary must not be called")


class _NoCallProviderRegistry:
    def __init__(self, provider: _NoCallProvider) -> None:
        self.provider = provider

    def get(self, _provider_id: str) -> _NoCallProvider:
        return self.provider


class PluginLifecycleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-p25-plugin-lifecycle-"
        )
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        self._request_counter = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _state(
        self,
        kind: str,
        target_id: str,
        *,
        store: StudioStore | None = None,
        include_history: bool = True,
    ) -> dict:
        lifecycle = (store or self.store).plugin_lifecycle_view(
            include_history=include_history
        )
        return next(
            row
            for row in lifecycle["targets"]
            if row["kind"] == kind and row["id"] == target_id
        )

    @staticmethod
    def _target_ref(state: dict) -> dict[str, str]:
        return {
            "kind": state["kind"],
            "id": state["id"],
            "version": state["version"],
            "sha256": state["target_sha256"],
        }

    def _preview(
        self,
        state: dict,
        action: str,
        *,
        store: StudioStore | None = None,
        replacement: dict | None = None,
    ) -> dict:
        active_store = store or self.store
        if (
            state.get("kind") == "capability_pack"
            and state.get("id") in {
                "structured_project_research",
                "project_readiness_review",
            }
            and action in {"disable", "quarantine", "deprecate", "tombstone"}
        ):
            dependant_ids = (
                [
                    "stock_research_readonly",
                    "project_round_focus",
                    "project_readiness_review",
                ]
                if state.get("id") == "structured_project_research"
                else ["project_round_focus"]
            )
            for dependant_id in dependant_ids:
                dependant = self._state(
                    "capability_pack",
                    dependant_id,
                    store=active_store,
                )
                if dependant.get("runtime_state") != "ready":
                    continue
                dependant_action = "quarantine" if action == "quarantine" else (
                    "deprecate" if action in {"deprecate", "tombstone"} else "disable"
                )
                dependant_preview = active_store.preview_plugin_lifecycle({
                    "version": PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION,
                    "target": self._target_ref(dependant),
                    "action": dependant_action,
                    "expected_head_sequence": dependant["head_sequence"],
                    "expected_head_sha256": dependant["head_sha256"],
                    "replacement": None,
                })
                active_store.transition_plugin_lifecycle(
                    self._transition_request(
                        dependant_preview,
                        reason="P27 test fixture disables explicit dependants first",
                    )
                )
        return active_store.preview_plugin_lifecycle({
            "version": PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION,
            "target": self._target_ref(state),
            "action": action,
            "expected_head_sequence": state["head_sequence"],
            "expected_head_sha256": state["head_sha256"],
            "replacement": replacement,
        })

    def _transition_request(
        self,
        preview: dict,
        *,
        client_request_id: str | None = None,
        reason: str = "P25 isolated lifecycle transition test",
    ) -> dict:
        if client_request_id is None:
            self._request_counter += 1
            client_request_id = f"p25-lifecycle-request-{self._request_counter:04d}"
        return {
            "version": PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION,
            "client_request_id": client_request_id,
            "target": copy.deepcopy(preview["target"]),
            "action": preview["action"],
            "expected_head_sequence": preview["expected_head_sequence"],
            "expected_head_sha256": preview["expected_head_sha256"],
            "replacement": copy.deepcopy(preview.get("replacement")),
            "impact_preview_sha256": preview["preview_sha256"],
            "reason": reason,
            "user_confirmed_history_preserved": True,
            "user_confirmed_no_automatic_migration": True,
        }

    def _transition(
        self,
        kind: str,
        target_id: str,
        action: str,
        *,
        store: StudioStore | None = None,
        reason: str = "P25 isolated lifecycle transition test",
    ) -> tuple[dict, bool, dict]:
        active_store = store or self.store
        state = self._state(kind, target_id, store=active_store)
        preview = self._preview(state, action, store=active_store)
        request = self._transition_request(preview, reason=reason)
        result, created = active_store.transition_plugin_lifecycle(request)
        return result, created, request

    def test_dependency_target_must_be_inactivated_before_its_base_pack(self) -> None:
        state = self._state("capability_pack", "structured_project_research")
        with self.assertRaises(PluginLifecycleError) as caught:
            self.store.preview_plugin_lifecycle({
                "version": PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION,
                "target": self._target_ref(state),
                "action": "disable",
                "expected_head_sequence": state["head_sequence"],
                "expected_head_sha256": state["head_sha256"],
                "replacement": None,
            })
        self.assertEqual(
            caught.exception.code,
            "PLUGIN_LIFECYCLE_REVERSE_DEPENDENCY_BLOCKED",
        )

    def _create_room(self, pack_ids: list[str]) -> dict:
        return self.store.create_room(
            "P25 lifecycle room",
            "Freeze lifecycle resolution without provider or market access",
            capability_pack_ids=pack_ids,
        )["room"]

    def _save_frozen_checkpoint(self, room: dict, round_row: dict) -> dict:
        members = self.store.enabled_members(room["id"])
        member_ids = [str(member["id"]) for member in members]
        moderator = members[0]
        shared_context, manifest = self.store.material_prompt_bundle(room["id"])
        frozen_manifest = self.store.finalize_round_evidence_manifest(
            manifest,
            shared_context=shared_context,
            market_snapshot=None,
        )
        return self.store.save_round_checkpoint(
            room["id"],
            round_row["id"],
            {
                "version": 9,
                "member_ids": member_ids,
                "moderator_member_id": member_ids[0],
                "discussion_mode": room["discussion_mode"],
                "domain": room["domain"],
                "moderator_member_version": int(moderator["version"]),
                "moderator_provider": str(moderator["provider"]),
                "moderator_model": str(moderator.get("model") or ""),
                "spoken_counts": {},
                "spoken_stances": [],
                "successful_member_ids": [],
                "failed_member_ids": [],
                "previous_name": "host",
                "completed": 0,
                "failures": 0,
                "skipped": 0,
                "proposals_created": 0,
                "next_order": 1,
                "max_turns": max(1, len(member_ids)),
                "shared_context": shared_context,
                "market_snapshot": None,
                "round_evidence_manifest": frozen_manifest,
                "skip_provider_ids": [],
                "workflow_policy": room["workflow_policy"],
                "capability_pack_ids": room["capability_pack_ids"],
                "plugin_registry_snapshot": round_row["plugin_registry_snapshot"],
                "project_workspace": None,
                "turn_contract_version": TURN_CONTRACT_VERSION,
                "turn_contract_required": True,
                "candidate_risk_review_version": round_row.get(
                    "candidate_risk_review_version"
                ),
                "candidate_risk_review_required": round_row.get(
                    "candidate_risk_review_version"
                )
                == CANDIDATE_RISK_REVIEW_VERSION,
                "turn_envelope_version": TURN_ENVELOPE_VERSION,
                "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
                "turn_output_modes_by_member": {
                    member_id: "json_schema" for member_id in member_ids
                },
            },
        )

    @staticmethod
    def _artifact_content() -> dict:
        return {
            "summary": "P25 lifecycle history remains readable.",
            "conclusions": [],
            "disagreements": [],
            "unknowns": [],
            "actions": [],
            "decision": {"status": "undecided", "options": []},
        }

    @staticmethod
    def _targets_with_same_id_ui_upgrade() -> tuple[list[dict], dict]:
        targets = plugin_lifecycle_targets()
        source = next(
            row
            for row in targets
            if row["kind"] == "ui_contribution"
            and row["id"] == "project_research.artifact_workspace/v1"
        )
        upgraded_snapshot = copy.deepcopy(source["snapshot"])
        upgraded_snapshot.pop("contract_sha256", None)
        upgraded_snapshot["contribution_version"] = "1.1.0"
        upgraded_sha256 = canonical_sha256(upgraded_snapshot)
        upgraded_snapshot["contract_sha256"] = upgraded_sha256
        upgraded = {
            **copy.deepcopy(source),
            "version": "1.1.0",
            "sha256": upgraded_sha256,
            "label": f"{source['label']} isolated v1.1 fixture",
            "snapshot": upgraded_snapshot,
        }
        return [*targets, upgraded], upgraded

    @staticmethod
    def _rewrite_latest_event_replacement(
        path: Path,
        event_id: str,
        replacement: dict | None,
    ) -> str:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            event = dict(connection.execute(
                "SELECT * FROM plugin_lifecycle_events WHERE id=?",
                (event_id,),
            ).fetchone())
            event["replacement_json"] = json.dumps(
                replacement or {},
                ensure_ascii=False,
            )
            event_sha256 = canonical_sha256(
                StudioStore._plugin_lifecycle_event_payload(event)
            )
            connection.execute(
                "DROP TRIGGER IF EXISTS trg_plugin_lifecycle_events_no_update"
            )
            connection.execute(
                """UPDATE plugin_lifecycle_events
                   SET replacement_json=?,event_sha256=? WHERE id=?""",
                (event["replacement_json"], event_sha256, event_id),
            )
            head = dict(connection.execute(
                """SELECT * FROM plugin_lifecycle_heads
                   WHERE target_kind=? AND target_id=? AND target_version=?""",
                (
                    event["target_kind"],
                    event["target_id"],
                    event["target_version"],
                ),
            ).fetchone())
            if str(head["head_event_id"] or "") != event_id:
                raise AssertionError("fixture event must be the latest target event")
            head_payload = StudioStore._plugin_lifecycle_head_payload(
                target_kind=head["target_kind"],
                target_id=head["target_id"],
                target_version=head["target_version"],
                target_sha256=head["target_sha256"],
                head_sequence=head["head_sequence"],
                head_event_id=head["head_event_id"],
                head_event_sha256=event_sha256,
                catalog_state=head["catalog_state"],
                activation_state=head["activation_state"],
                resume_activation_state=head["resume_activation_state"],
                updated_at=head["updated_at"],
            )
            connection.execute(
                """UPDATE plugin_lifecycle_heads
                   SET head_event_sha256=?,head_sha256=?
                   WHERE target_kind=? AND target_id=? AND target_version=?""",
                (
                    event_sha256,
                    canonical_sha256(head_payload),
                    event["target_kind"],
                    event["target_id"],
                    event["target_version"],
                ),
            )
        return event_sha256

    def test_new_database_and_reopen_keep_one_verified_builtin_baseline(self) -> None:
        first = self.store.plugin_lifecycle_view(include_history=True)
        expected_targets = plugin_lifecycle_targets()

        self.assertTrue(first["integrity_ok"])
        self.assertEqual(len(first["targets"]), len(expected_targets))
        self.assertTrue(all(row["integrity_ok"] for row in first["targets"]))
        self.assertTrue(all(row["runtime_state"] == "ready" for row in first["targets"]))
        self.assertTrue(all(row["catalog_state"] == "active" for row in first["targets"]))
        self.assertTrue(all(row["activation_state"] == "enabled" for row in first["targets"]))
        self.assertTrue(all(row["head_sequence"] == 0 for row in first["targets"]))
        self.assertTrue(all(row["history"] == [] for row in first["targets"]))

        with closing(sqlite3.connect(self.db_path)) as connection:
            target_count = connection.execute(
                "SELECT COUNT(*) FROM plugin_lifecycle_targets"
            ).fetchone()[0]
            head_count = connection.execute(
                "SELECT COUNT(*) FROM plugin_lifecycle_heads"
            ).fetchone()[0]
            event_count = connection.execute(
                "SELECT COUNT(*) FROM plugin_lifecycle_events"
            ).fetchone()[0]
            marker_count = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations "
                "WHERE key='plugin_lifecycle_ledger_v1'"
            ).fetchone()[0]
            unsealed_rooms = connection.execute(
                "SELECT COUNT(*) FROM rooms "
                "WHERE plugin_lifecycle_resolution_sha256=''"
            ).fetchone()[0]
        self.assertEqual(target_count, len(expected_targets))
        self.assertEqual(head_count, len(expected_targets))
        self.assertEqual(event_count, 0)
        self.assertEqual(marker_count, 1)
        self.assertEqual(unsealed_rooms, 0)

        reopened = StudioStore(self.db_path)
        second = reopened.plugin_lifecycle_view(include_history=True)
        self.assertEqual(second["view_sha256"], first["view_sha256"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM plugin_lifecycle_targets"
                ).fetchone()[0],
                len(expected_targets),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM plugin_lifecycle_events"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE key='plugin_lifecycle_ledger_v1'"
                ).fetchone()[0],
                1,
            )

    def test_state_transition_matrix_and_public_action_projection(self) -> None:
        initial = {
            "catalog_state": "active",
            "activation_state": "enabled",
            "resume_activation_state": "enabled",
        }
        disabled = {
            "catalog_state": "active",
            "activation_state": "disabled",
            "resume_activation_state": "disabled",
        }
        quarantined_enabled = {
            "catalog_state": "active",
            "activation_state": "quarantined",
            "resume_activation_state": "enabled",
        }
        deprecated = {
            "catalog_state": "deprecated",
            "activation_state": "enabled",
            "resume_activation_state": "enabled",
        }
        tombstoned = {
            "catalog_state": "tombstoned",
            "activation_state": "disabled",
            "resume_activation_state": "disabled",
        }
        valid = [
            (initial, "disable", disabled),
            (initial, "quarantine", quarantined_enabled),
            (initial, "deprecate", deprecated),
            (initial, "tombstone", tombstoned),
            (disabled, "enable", initial),
            (
                disabled,
                "quarantine",
                {
                    "catalog_state": "active",
                    "activation_state": "quarantined",
                    "resume_activation_state": "disabled",
                },
            ),
            (quarantined_enabled, "clear_quarantine", initial),
            (deprecated, "reinstate", initial),
        ]
        for state, action, expected in valid:
            with self.subTest(state=state, action=action):
                self.assertEqual(apply_lifecycle_action(state, action), expected)

        invalid = [
            (initial, "enable"),
            (initial, "clear_quarantine"),
            (initial, "reinstate"),
            (disabled, "disable"),
            (quarantined_enabled, "quarantine"),
            (quarantined_enabled, "enable"),
            (deprecated, "deprecate"),
        ]
        invalid.extend((tombstoned, action) for action in (
            "disable",
            "enable",
            "quarantine",
            "clear_quarantine",
            "deprecate",
            "reinstate",
            "tombstone",
        ))
        for state, action in invalid:
            with self.subTest(invalid_state=state, invalid_action=action):
                with self.assertRaises(PluginLifecycleError):
                    apply_lifecycle_action(state, action)

        self.assertEqual(
            available_lifecycle_actions(initial, system_managed=False),
            ["disable", "quarantine", "deprecate", "tombstone"],
        )
        self.assertEqual(
            available_lifecycle_actions(disabled, system_managed=False),
            ["enable", "quarantine", "deprecate", "tombstone"],
        )
        self.assertEqual(
            available_lifecycle_actions(quarantined_enabled, system_managed=False),
            ["clear_quarantine", "deprecate", "tombstone"],
        )
        self.assertEqual(
            available_lifecycle_actions(deprecated, system_managed=False),
            ["disable", "quarantine", "reinstate", "tombstone"],
        )
        self.assertEqual(
            available_lifecycle_actions(tombstoned, system_managed=False),
            [],
        )
        self.assertEqual(
            available_lifecycle_actions(initial, system_managed=True),
            [],
        )

        target_kind = "ui_contribution"
        target_id = "project_research.artifact_workspace/v1"
        persisted_actions = [
            ("deprecate", "deprecated", "enabled"),
            ("reinstate", "active", "enabled"),
            ("disable", "active", "disabled"),
            ("enable", "active", "enabled"),
            ("quarantine", "active", "quarantined"),
            ("clear_quarantine", "active", "enabled"),
            ("tombstone", "tombstoned", "disabled"),
        ]
        for sequence, (action, catalog_state, activation_state) in enumerate(
            persisted_actions,
            start=1,
        ):
            with self.subTest(persisted_action=action):
                _result, created, _request = self._transition(
                    target_kind,
                    target_id,
                    action,
                )
                self.assertTrue(created)
                persisted = self._state(target_kind, target_id)
                self.assertEqual(persisted["head_sequence"], sequence)
                self.assertEqual(persisted["catalog_state"], catalog_state)
                self.assertEqual(persisted["activation_state"], activation_state)
        terminal = self._state(target_kind, target_id)
        self.assertEqual(
            [row["action"] for row in terminal["history"]],
            [row[0] for row in persisted_actions],
        )
        self.assertEqual(
            [row["sequence_no"] for row in terminal["history"]],
            list(range(1, len(persisted_actions) + 1)),
        )
        self.assertFalse(terminal["runtime_available"])
        self.assertEqual(terminal["available_actions"], [])
        with self.assertRaises(PluginLifecycleError) as caught:
            self._preview(terminal, "enable")
        self.assertEqual(
            caught.exception.code,
            "PLUGIN_LIFECYCLE_TOMBSTONE_TERMINAL",
        )

    def test_preview_seal_idempotency_semantic_conflict_and_stale_head(self) -> None:
        state = self._state("capability_pack", "structured_project_research")
        preview = self._preview(state, "disable")
        sealed_preview = copy.deepcopy(preview)
        stored_preview_sha256 = sealed_preview.pop("preview_sha256")
        self.assertEqual(canonical_sha256(sealed_preview), stored_preview_sha256)

        request = self._transition_request(
            preview,
            client_request_id="p25-preview-seal-0001",
        )
        tampered = {**request, "impact_preview_sha256": "0" * 64}
        with self.assertRaises(PluginLifecycleError) as caught:
            self.store.transition_plugin_lifecycle(tampered)
        self.assertEqual(caught.exception.code, "PLUGIN_LIFECYCLE_PREVIEW_CONFLICT")
        self.assertEqual(
            self._state("capability_pack", "structured_project_research")[
                "head_sequence"
            ],
            0,
        )

        first, created = self.store.transition_plugin_lifecycle(request)
        replay, replay_created = self.store.transition_plugin_lifecycle(request)
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replay["event"]["id"], first["event"]["id"])

        changed_semantics = {**request, "reason": "P25 changed semantic reason"}
        with self.assertRaises(PluginLifecycleError) as caught:
            self.store.transition_plugin_lifecycle(changed_semantics)
        self.assertEqual(
            caught.exception.code,
            "PLUGIN_LIFECYCLE_IDEMPOTENCY_CONFLICT",
        )

        disabled = self._state("capability_pack", "structured_project_research")
        stale_enable_preview = self._preview(disabled, "enable")
        quarantine_preview = self._preview(disabled, "quarantine")
        self.store.transition_plugin_lifecycle(
            self._transition_request(
                quarantine_preview,
                client_request_id="p25-stale-head-winner-0001",
            )
        )
        stale_request = self._transition_request(
            stale_enable_preview,
            client_request_id="p25-stale-head-loser-0001",
        )
        with self.assertRaises(PluginLifecycleError) as caught:
            self.store.transition_plugin_lifecycle(stale_request)
        self.assertEqual(caught.exception.code, "PLUGIN_LIFECYCLE_HEAD_CONFLICT")
        final_state = self._state("capability_pack", "structured_project_research")
        self.assertEqual(final_state["head_sequence"], 2)
        self.assertEqual(final_state["activation_state"], "quarantined")

    def test_historical_idempotent_replay_keeps_the_exact_original_response(self) -> None:
        state = self._state("capability_pack", "structured_project_research")
        disable_preview = self._preview(state, "disable")
        disable_request = self._transition_request(
            disable_preview,
            client_request_id="p25-historical-replay-a-0001",
            reason="P25 historical replay event A",
        )
        first_response, first_created = self.store.transition_plugin_lifecycle(
            disable_request
        )
        self.assertTrue(first_created)

        disabled = self._state("capability_pack", "structured_project_research")
        enable_preview = self._preview(disabled, "enable")
        enable_request = self._transition_request(
            enable_preview,
            client_request_id="p25-historical-replay-b-0001",
            reason="P25 later event B",
        )
        _enabled_response, enabled_created = self.store.transition_plugin_lifecycle(
            enable_request
        )
        self.assertTrue(enabled_created)
        self.assertEqual(
            self._state("capability_pack", "structured_project_research")[
                "activation_state"
            ],
            "enabled",
        )

        replay_response, replay_created = self.store.transition_plugin_lifecycle(
            disable_request
        )
        self.assertFalse(replay_created)
        self.assertEqual(replay_response, first_response)

    def test_system_managed_target_cannot_be_previewed_or_mutated(self) -> None:
        lifecycle = self.store.plugin_lifecycle_view(include_history=True)
        system_target = next(row for row in lifecycle["targets"] if row["system_managed"])
        with self.assertRaises(PluginLifecycleError) as caught:
            self._preview(system_target, "disable")
        self.assertEqual(
            caught.exception.code,
            "PLUGIN_LIFECYCLE_CORE_TARGET_IMMUTABLE",
        )
        self.assertEqual(
            self._state(system_target["kind"], system_target["id"])["head_sequence"],
            0,
        )

    def test_ui_replacement_requires_same_stable_id_and_compatible_contract(self) -> None:
        source = self._state(
            "ui_contribution",
            "project_research.artifact_workspace/v1",
        )
        cross_slot = self._state(
            "ui_contribution",
            "core.capability_pack_settings/v1",
        )
        with self.assertRaises(PluginLifecycleError) as caught:
            self._preview(
                source,
                "deprecate",
                replacement=self._target_ref(cross_slot),
            )
        self.assertEqual(
            caught.exception.code,
            "PLUGIN_LIFECYCLE_REPLACEMENT_INCOMPATIBLE",
        )
        self.assertEqual(caught.exception.status, 409)

        same_slot_different_id = self._state(
            "ui_contribution",
            "storage_research.artifact_workspace/v1",
        )
        with self.assertRaises(PluginLifecycleError) as caught:
            self._preview(
                source,
                "deprecate",
                replacement=self._target_ref(same_slot_different_id),
            )
        self.assertEqual(
            caught.exception.code,
            "PLUGIN_LIFECYCLE_REPLACEMENT_INCOMPATIBLE",
        )
        self.assertEqual(caught.exception.status, 409)

        fixture_targets, upgraded = self._targets_with_same_id_ui_upgrade()
        fixture_path = Path(self.temp_dir.name) / "same-id-replacement.sqlite3"
        with patch(
            "backend.store.plugin_lifecycle_targets",
            return_value=fixture_targets,
        ):
            fixture_store = StudioStore(fixture_path)
            lifecycle = fixture_store.plugin_lifecycle_view(include_history=True)
            fixture_source = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == "ui_contribution"
                and row["id"] == upgraded["id"]
                and row["version"] == "1.0.0"
            )
            fixture_replacement = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == "ui_contribution"
                and row["id"] == upgraded["id"]
                and row["version"] == upgraded["version"]
            )
            preview = self._preview(
                fixture_source,
                "deprecate",
                store=fixture_store,
                replacement=self._target_ref(fixture_replacement),
            )
            self.assertEqual(
                preview["replacement"],
                self._target_ref(fixture_replacement),
            )
            self.assertEqual(preview["result"]["catalog_state"], "deprecated")
            self.assertFalse(preview["result"]["new_bindings_allowed"])
            self.assertFalse(preview["impact"]["automatic_replacement_performed"])
            sealed = copy.deepcopy(preview)
            preview_sha256 = sealed.pop("preview_sha256")
            self.assertEqual(canonical_sha256(sealed), preview_sha256)

    def test_replacement_projection_tracks_unavailability_without_auto_migration(self) -> None:
        fixture_targets, upgraded = self._targets_with_same_id_ui_upgrade()
        fixture_path = Path(self.temp_dir.name) / "replacement-status.sqlite3"
        with patch(
            "backend.store.plugin_lifecycle_targets",
            return_value=fixture_targets,
        ):
            fixture_store = StudioStore(fixture_path)
            room = fixture_store.create_room(
                "P25 replacement projection room",
                "Replacement remains informational and never migrates the room",
                capability_pack_ids=["structured_project_research"],
            )["room"]
            original_registry = copy.deepcopy(room["plugin_registry_snapshot"])
            original_settings_version = room["settings_version"]
            lifecycle = fixture_store.plugin_lifecycle_view(include_history=True)
            source = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == "ui_contribution"
                and row["id"] == upgraded["id"]
                and row["version"] == "1.0.0"
            )
            replacement = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == "ui_contribution"
                and row["id"] == upgraded["id"]
                and row["version"] == upgraded["version"]
            )
            replacement_ref = self._target_ref(replacement)
            preview = self._preview(
                source,
                "deprecate",
                store=fixture_store,
                replacement=replacement_ref,
            )
            request = self._transition_request(
                preview,
                client_request_id="p25-replacement-source-0001",
                reason="P25 records a compatible replacement without migrating",
            )
            _result, created = fixture_store.transition_plugin_lifecycle(request)
            self.assertTrue(created)

            replacement_after_source = next(
                row
                for row in fixture_store.plugin_lifecycle_view(
                    include_history=True
                )["targets"]
                if row["kind"] == "ui_contribution"
                and row["id"] == upgraded["id"]
                and row["version"] == upgraded["version"]
            )
            disable_preview = self._preview(
                replacement_after_source,
                "disable",
                store=fixture_store,
            )
            fixture_store.transition_plugin_lifecycle(
                self._transition_request(
                    disable_preview,
                    client_request_id="p25-replacement-target-disable-0001",
                    reason="P25 later disables the declared replacement",
                )
            )
            lifecycle_after_disable = fixture_store.plugin_lifecycle_view(
                include_history=True
            )
            source_after = next(
                row
                for row in lifecycle_after_disable["targets"]
                if row["kind"] == "ui_contribution"
                and row["id"] == upgraded["id"]
                and row["version"] == "1.0.0"
            )
            disabled_replacement = next(
                row
                for row in lifecycle_after_disable["targets"]
                if row["kind"] == "ui_contribution"
                and row["id"] == upgraded["id"]
                and row["version"] == upgraded["version"]
            )
            self.assertTrue(lifecycle_after_disable["integrity_ok"])
            self.assertTrue(source_after["integrity_ok"])
            self.assertTrue(disabled_replacement["integrity_ok"])
            self.assertFalse(disabled_replacement["runtime_available"])
            self.assertEqual(source_after["replacement"], replacement_ref)
            self.assertIn("replacement_status", source_after)
            replacement_status = source_after["replacement_status"]
            self.assertTrue(replacement_status["declared"])
            self.assertTrue(replacement_status["integrity_ok"])
            self.assertEqual(
                replacement_status["current_runtime_state"],
                "disabled",
            )
            self.assertFalse(replacement_status["current_runtime_available"])
            self.assertFalse(replacement_status["automatic_migration_performed"])

            room_after = fixture_store.room_snapshot(room["id"])["room"]
            self.assertEqual(
                room_after["settings_version"],
                original_settings_version,
            )
            self.assertEqual(
                room_after["capability_pack_ids"],
                ["structured_project_research"],
            )
            self.assertEqual(
                room_after["plugin_registry_snapshot"],
                original_registry,
            )
            contribution_ids = {
                row["contribution_id"]
                for row in room_after["plugin_registry_snapshot"]["ui_contributions"]
            }
            self.assertEqual(
                contribution_ids,
                {
                    "core.capability_pack_settings/v1",
                    "project_research.artifact_workspace/v1",
                },
            )

    def test_rehashed_forged_replacement_history_fails_closed(self) -> None:
        fixture_targets, upgraded = self._targets_with_same_id_ui_upgrade()
        source_target = next(
            row
            for row in fixture_targets
            if row["kind"] == "ui_contribution"
            and row["id"] == upgraded["id"]
            and row["version"] != upgraded["version"]
        )
        cross_target = next(
            row
            for row in fixture_targets
            if row["kind"] == "ui_contribution"
            and row["id"] != source_target["id"]
            and row["system_managed"] is False
        )
        valid_replacement = {
            "kind": upgraded["kind"],
            "id": upgraded["id"],
            "version": upgraded["version"],
            "sha256": upgraded["sha256"],
        }
        forged_cases = (
            (
                "missing_exact_target",
                "deprecate",
                {
                    "kind": source_target["kind"],
                    "id": source_target["id"],
                    "version": "9.9.9",
                    "sha256": "f" * 64,
                },
            ),
            (
                "wrong_exact_hash",
                "deprecate",
                {**valid_replacement, "sha256": "f" * 64},
            ),
            (
                "cross_stable_id",
                "deprecate",
                {
                    "kind": cross_target["kind"],
                    "id": cross_target["id"],
                    "version": cross_target["version"],
                    "sha256": cross_target["sha256"],
                },
            ),
            (
                "replacement_on_disallowed_action",
                "disable",
                valid_replacement,
            ),
        )
        for case_name, action, forged_replacement in forged_cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory(
                prefix=f"ai-studio-p25-forged-replacement-{case_name}-"
            ) as fixture_dir, patch(
                "backend.store.plugin_lifecycle_targets",
                return_value=fixture_targets,
            ):
                fixture_path = Path(fixture_dir) / "studio.sqlite3"
                fixture_store = StudioStore(fixture_path)
                source_state = next(
                    row
                    for row in fixture_store.plugin_lifecycle_view(
                        include_history=True
                    )["targets"]
                    if row["kind"] == source_target["kind"]
                    and row["id"] == source_target["id"]
                    and row["version"] == source_target["version"]
                )
                preview = self._preview(
                    source_state,
                    action,
                    store=fixture_store,
                )
                request = self._transition_request(
                    preview,
                    client_request_id=f"p25-forged-replacement-{case_name}-0001",
                    reason=f"P25 creates the base event for {case_name}",
                )
                result, created = fixture_store.transition_plugin_lifecycle(
                    request
                )
                self.assertTrue(created)
                self._rewrite_latest_event_replacement(
                    fixture_path,
                    result["event"]["id"],
                    forged_replacement,
                )
                with closing(sqlite3.connect(fixture_path)) as connection:
                    event_count_before = connection.execute(
                        "SELECT COUNT(*) FROM plugin_lifecycle_events"
                    ).fetchone()[0]

                lifecycle = fixture_store.plugin_lifecycle_view(
                    include_history=True
                )
                forged_source = next(
                    row
                    for row in lifecycle["targets"]
                    if row["kind"] == source_target["kind"]
                    and row["id"] == source_target["id"]
                    and row["version"] == source_target["version"]
                )
                self.assertFalse(lifecycle["integrity_ok"])
                self.assertFalse(forged_source["integrity_ok"])
                self.assertFalse(forged_source["runtime_available"])
                self.assertFalse(forged_source["new_bindings_allowed"])
                self.assertEqual(
                    forged_source["runtime_state"],
                    "lifecycle_integrity_failed",
                )
                self.assertEqual(forged_source["available_actions"], [])
                self.assertEqual(forged_source["history"], [])
                with self.assertRaises(PluginLifecycleError) as caught:
                    self._preview(
                        result["target"],
                        "tombstone",
                        store=fixture_store,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "PLUGIN_LIFECYCLE_INTEGRITY_FAILED",
                )
                with closing(sqlite3.connect(fixture_path)) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM plugin_lifecycle_events"
                        ).fetchone()[0],
                        event_count_before,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM provider_call_attempts"
                        ).fetchone()[0],
                        0,
                    )

    def test_rehashed_effective_replacement_cycle_fails_all_participants_closed(self) -> None:
        fixture_targets, upgraded = self._targets_with_same_id_ui_upgrade()
        fixture_path = Path(self.temp_dir.name) / "replacement-cycle.sqlite3"
        with patch(
            "backend.store.plugin_lifecycle_targets",
            return_value=fixture_targets,
        ):
            fixture_store = StudioStore(fixture_path)
            lifecycle = fixture_store.plugin_lifecycle_view(include_history=True)
            source = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == "ui_contribution"
                and row["id"] == upgraded["id"]
                and row["version"] != upgraded["version"]
            )
            replacement = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == "ui_contribution"
                and row["id"] == upgraded["id"]
                and row["version"] == upgraded["version"]
            )
            source_preview = self._preview(
                source,
                "deprecate",
                store=fixture_store,
                replacement=self._target_ref(replacement),
            )
            fixture_store.transition_plugin_lifecycle(
                self._transition_request(
                    source_preview,
                    client_request_id="p25-replacement-cycle-source-0001",
                    reason="P25 creates the legal first replacement edge",
                )
            )
            replacement_current = next(
                row
                for row in fixture_store.plugin_lifecycle_view(
                    include_history=True
                )["targets"]
                if row["kind"] == replacement["kind"]
                and row["id"] == replacement["id"]
                and row["version"] == replacement["version"]
            )
            replacement_preview = self._preview(
                replacement_current,
                "deprecate",
                store=fixture_store,
            )
            replacement_request = self._transition_request(
                replacement_preview,
                client_request_id="p25-replacement-cycle-target-0001",
                reason="P25 creates the base second edge event",
            )
            replacement_result, replacement_created = (
                fixture_store.transition_plugin_lifecycle(
                    replacement_request
                )
            )
            self.assertTrue(replacement_created)
            self._rewrite_latest_event_replacement(
                fixture_path,
                replacement_result["event"]["id"],
                self._target_ref(source),
            )

            cycled = fixture_store.plugin_lifecycle_view(include_history=True)
            participants = [
                row
                for row in cycled["targets"]
                if row["kind"] == source["kind"]
                and row["id"] == source["id"]
                and row["version"] in {source["version"], replacement["version"]}
            ]
            self.assertEqual(len(participants), 2)
            self.assertFalse(cycled["integrity_ok"])
            for participant in participants:
                self.assertFalse(participant["integrity_ok"])
                self.assertFalse(participant["runtime_available"])
                self.assertFalse(participant["new_bindings_allowed"])
                self.assertEqual(
                    participant["runtime_state"],
                    "lifecycle_integrity_failed",
                )
                self.assertEqual(participant["available_actions"], [])
                self.assertEqual(participant["history"], [])
            with closing(sqlite3.connect(fixture_path)) as connection:
                event_count_before_replay = connection.execute(
                    "SELECT COUNT(*) FROM plugin_lifecycle_events"
                ).fetchone()[0]
                replacement_head_before_replay = connection.execute(
                    """SELECT head_sequence,head_event_id,head_event_sha256,
                              head_sha256,catalog_state,activation_state,
                              resume_activation_state,updated_at
                       FROM plugin_lifecycle_heads
                       WHERE target_kind=? AND target_id=? AND target_version=?""",
                    (
                        replacement["kind"],
                        replacement["id"],
                        replacement["version"],
                    ),
                ).fetchone()
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM provider_call_attempts"
                    ).fetchone()[0],
                    0,
                )
            with self.assertRaises(PluginLifecycleError) as replay_caught:
                fixture_store.transition_plugin_lifecycle(replacement_request)
            self.assertEqual(
                replay_caught.exception.code,
                "PLUGIN_LIFECYCLE_INTEGRITY_FAILED",
            )
            self.assertEqual(replay_caught.exception.status, 409)
            with closing(sqlite3.connect(fixture_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM plugin_lifecycle_events"
                    ).fetchone()[0],
                    event_count_before_replay,
                )
                self.assertEqual(
                    connection.execute(
                        """SELECT head_sequence,head_event_id,head_event_sha256,
                                  head_sha256,catalog_state,activation_state,
                                  resume_activation_state,updated_at
                           FROM plugin_lifecycle_heads
                           WHERE target_kind=? AND target_id=? AND target_version=?""",
                        (
                            replacement["kind"],
                            replacement["id"],
                            replacement["version"],
                        ),
                    ).fetchone(),
                    replacement_head_before_replay,
                )

    def test_rehashed_replacement_body_cannot_drift_from_sealed_request_semantics(self) -> None:
        fixture_targets, replacement_b = self._targets_with_same_id_ui_upgrade()
        replacement_c_snapshot = copy.deepcopy(replacement_b["snapshot"])
        replacement_c_snapshot.pop("contract_sha256", None)
        replacement_c_snapshot["contribution_version"] = "1.2.0"
        replacement_c_sha256 = canonical_sha256(replacement_c_snapshot)
        replacement_c_snapshot["contract_sha256"] = replacement_c_sha256
        replacement_c = {
            **copy.deepcopy(replacement_b),
            "version": "1.2.0",
            "sha256": replacement_c_sha256,
            "label": f"{replacement_b['label']} isolated v1.2 fixture",
            "snapshot": replacement_c_snapshot,
        }
        fixture_targets = [*fixture_targets, replacement_c]
        source_target = next(
            row
            for row in fixture_targets
            if row["kind"] == replacement_b["kind"]
            and row["id"] == replacement_b["id"]
            and row["version"] not in {
                replacement_b["version"],
                replacement_c["version"],
            }
        )
        fixture_path = Path(self.temp_dir.name) / "replacement-semantics-drift.sqlite3"
        with patch(
            "backend.store.plugin_lifecycle_targets",
            return_value=fixture_targets,
        ):
            fixture_store = StudioStore(fixture_path)
            lifecycle = fixture_store.plugin_lifecycle_view(include_history=True)
            source = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == source_target["kind"]
                and row["id"] == source_target["id"]
                and row["version"] == source_target["version"]
            )
            replacement_b_state = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == replacement_b["kind"]
                and row["id"] == replacement_b["id"]
                and row["version"] == replacement_b["version"]
            )
            preview = self._preview(
                source,
                "deprecate",
                store=fixture_store,
                replacement=self._target_ref(replacement_b_state),
            )
            original_request = self._transition_request(
                preview,
                client_request_id="p25-replacement-semantics-a-to-b-0001",
                reason="P25 seals the exact A to B replacement request",
            )
            original_result, original_created = (
                fixture_store.transition_plugin_lifecycle(original_request)
            )
            self.assertTrue(original_created)
            self.assertEqual(
                original_result["event"]["replacement"],
                self._target_ref(replacement_b_state),
            )
            self._rewrite_latest_event_replacement(
                fixture_path,
                original_result["event"]["id"],
                {
                    "kind": replacement_c["kind"],
                    "id": replacement_c["id"],
                    "version": replacement_c["version"],
                    "sha256": replacement_c["sha256"],
                },
            )
            with closing(sqlite3.connect(fixture_path)) as connection:
                event_count_before_replay = connection.execute(
                    "SELECT COUNT(*) FROM plugin_lifecycle_events"
                ).fetchone()[0]
                head_before_replay = connection.execute(
                    """SELECT head_sequence,head_event_id,head_event_sha256,
                              head_sha256,catalog_state,activation_state,
                              resume_activation_state,updated_at
                       FROM plugin_lifecycle_heads
                       WHERE target_kind=? AND target_id=? AND target_version=?""",
                    (
                        source["kind"],
                        source["id"],
                        source["version"],
                    ),
                ).fetchone()

            drifted = fixture_store.plugin_lifecycle_view(include_history=True)
            drifted_source = next(
                row
                for row in drifted["targets"]
                if row["kind"] == source["kind"]
                and row["id"] == source["id"]
                and row["version"] == source["version"]
            )
            self.assertFalse(drifted["integrity_ok"])
            self.assertFalse(drifted_source["integrity_ok"])
            self.assertEqual(
                drifted_source["runtime_state"],
                "lifecycle_integrity_failed",
            )
            self.assertEqual(drifted_source["history"], [])
            with self.assertRaises(PluginLifecycleError) as replay_caught:
                fixture_store.transition_plugin_lifecycle(original_request)
            self.assertEqual(
                replay_caught.exception.code,
                "PLUGIN_LIFECYCLE_INTEGRITY_FAILED",
            )
            self.assertEqual(replay_caught.exception.status, 409)
            with closing(sqlite3.connect(fixture_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM plugin_lifecycle_events"
                    ).fetchone()[0],
                    event_count_before_replay,
                )
                self.assertEqual(
                    connection.execute(
                        """SELECT head_sequence,head_event_id,head_event_sha256,
                                  head_sha256,catalog_state,activation_state,
                                  resume_activation_state,updated_at
                           FROM plugin_lifecycle_heads
                           WHERE target_kind=? AND target_id=? AND target_version=?""",
                        (
                            source["kind"],
                            source["id"],
                            source["version"],
                        ),
                    ).fetchone(),
                    head_before_replay,
                )

    def test_v2_request_semantics_seal_missing_or_tampered_fails_closed(self) -> None:
        for case_name in ("missing", "tampered"):
            with self.subTest(case=case_name), tempfile.TemporaryDirectory(
                prefix=f"ai-studio-p25-v2-semantics-{case_name}-"
            ) as fixture_dir:
                fixture_path = Path(fixture_dir) / "studio.sqlite3"
                fixture_store = StudioStore(fixture_path)
                state = next(
                    row
                    for row in fixture_store.plugin_lifecycle_view(
                        include_history=True
                    )["targets"]
                    if row["kind"] == "capability_pack"
                    and row["id"] == "structured_project_research"
                )
                preview = self._preview(
                    state,
                    "disable",
                    store=fixture_store,
                )
                request = self._transition_request(
                    preview,
                    client_request_id=f"p25-v2-semantics-{case_name}-0001",
                    reason=f"P25 v2 semantics seal {case_name} fixture",
                )
                result, created = fixture_store.transition_plugin_lifecycle(
                    request
                )
                self.assertTrue(created)
                event_id = result["event"]["id"]
                with closing(sqlite3.connect(fixture_path)) as connection, connection:
                    event = connection.execute(
                        """SELECT request_semantics_json,request_semantics_sha256
                           FROM plugin_lifecycle_events WHERE id=?""",
                        (event_id,),
                    ).fetchone()
                    self.assertIsInstance(event[0], str)
                    sealed_semantics = json.loads(event[0])
                    self.assertEqual(
                        canonical_sha256(sealed_semantics),
                        event[1],
                    )
                    connection.execute(
                        "DROP TRIGGER trg_plugin_lifecycle_events_no_update"
                    )
                    if case_name == "missing":
                        connection.execute(
                            """UPDATE plugin_lifecycle_events
                               SET request_semantics_json=NULL WHERE id=?""",
                            (event_id,),
                        )
                    else:
                        tampered_semantics = copy.deepcopy(sealed_semantics)
                        tampered_semantics["reason"] = (
                            "P25 tampered request semantics that do not match the seal"
                        )
                        connection.execute(
                            """UPDATE plugin_lifecycle_events
                               SET request_semantics_json=? WHERE id=?""",
                            (
                                json.dumps(
                                    tampered_semantics,
                                    ensure_ascii=False,
                                ),
                                event_id,
                            ),
                        )
                    event_count_before_replay = connection.execute(
                        "SELECT COUNT(*) FROM plugin_lifecycle_events"
                    ).fetchone()[0]

                lifecycle = fixture_store.plugin_lifecycle_view(
                    include_history=True
                )
                tampered_target = next(
                    row
                    for row in lifecycle["targets"]
                    if row["kind"] == state["kind"]
                    and row["id"] == state["id"]
                    and row["version"] == state["version"]
                )
                self.assertFalse(lifecycle["integrity_ok"])
                self.assertFalse(tampered_target["integrity_ok"])
                self.assertEqual(
                    tampered_target["runtime_state"],
                    "lifecycle_integrity_failed",
                )
                self.assertEqual(tampered_target["history"], [])
                with self.assertRaises(PluginLifecycleError) as replay_caught:
                    fixture_store.transition_plugin_lifecycle(request)
                self.assertEqual(
                    replay_caught.exception.code,
                    "PLUGIN_LIFECYCLE_INTEGRITY_FAILED",
                )
                self.assertEqual(replay_caught.exception.status, 409)
                with closing(sqlite3.connect(fixture_path)) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM plugin_lifecycle_events"
                        ).fetchone()[0],
                        event_count_before_replay,
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM provider_call_attempts"
                        ).fetchone()[0],
                        0,
                    )

    def test_begin_immediate_concurrency_and_fault_injection_leave_no_partial_event(self) -> None:
        state = self._state("capability_pack", "structured_project_research")
        preview = self._preview(state, "disable")
        first_request = self._transition_request(
            preview,
            client_request_id="p25-concurrent-writer-0001",
            reason="P25 concurrent writer one",
        )
        second_request = self._transition_request(
            preview,
            client_request_id="p25-concurrent-writer-0002",
            reason="P25 concurrent writer two",
        )
        second_store = StudioStore(self.db_path)
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, object]] = []
        outcome_lock = threading.Lock()

        def worker(store: StudioStore, request: dict) -> None:
            barrier.wait(timeout=5)
            try:
                result, created = store.transition_plugin_lifecycle(request)
            except Exception as exc:  # inspected below with exact typed assertions
                outcome = ("error", exc)
            else:
                outcome = ("created", (result, created))
            with outcome_lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=worker, args=(self.store, first_request)),
            threading.Thread(target=worker, args=(second_store, second_request)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

        self.assertEqual([kind for kind, _ in outcomes].count("created"), 1)
        self.assertEqual([kind for kind, _ in outcomes].count("error"), 1)
        successful = next(payload for kind, payload in outcomes if kind == "created")
        self.assertTrue(successful[1])
        failure = next(payload for kind, payload in outcomes if kind == "error")
        self.assertIsInstance(failure, PluginLifecycleError)
        self.assertEqual(failure.code, "PLUGIN_LIFECYCLE_HEAD_CONFLICT")
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM plugin_lifecycle_events "
                    "WHERE target_kind='capability_pack' "
                    "AND target_id='structured_project_research'"
                ).fetchone()[0],
                1,
            )

        fault_state = self._state(
            "ui_contribution",
            "project_research.artifact_workspace/v1",
        )
        fault_preview = self._preview(fault_state, "disable")
        fault_request = self._transition_request(
            fault_preview,
            client_request_id="p25-fault-injection-0001",
        )
        original_connect = self.store._connect

        def fault_connect():
            return _ExecuteFaultConnection(
                original_connect(),
                "UPDATE PLUGIN_LIFECYCLE_HEADS SET",
            )

        with patch.object(self.store, "_connect", side_effect=fault_connect):
            with self.assertRaisesRegex(RuntimeError, "injected lifecycle head"):
                self.store.transition_plugin_lifecycle(fault_request)

        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM plugin_lifecycle_events "
                    "WHERE client_request_id='p25-fault-injection-0001'"
                ).fetchone()[0],
                0,
            )
        fault_after = self._state(
            "ui_contribution",
            "project_research.artifact_workspace/v1",
        )
        self.assertEqual(fault_after["head_sequence"], fault_state["head_sequence"])
        self.assertEqual(fault_after["head_sha256"], fault_state["head_sha256"])
        self.assertTrue(fault_after["integrity_ok"])

    def test_event_and_head_tamper_fail_closed_for_target_and_catalog(self) -> None:
        result, created, _request = self._transition(
            "capability_pack",
            "structured_project_research",
            "disable",
        )
        self.assertTrue(created)
        event_id = result["event"]["id"]
        head_target = self._state(
            "ui_contribution",
            "project_research.artifact_workspace/v1",
        )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_plugin_lifecycle_events_no_update")
            connection.execute(
                "UPDATE plugin_lifecycle_events SET reason=? WHERE id=?",
                ("tampered lifecycle reason", event_id),
            )
            connection.execute(
                "UPDATE plugin_lifecycle_heads SET head_sha256=? "
                "WHERE target_kind=? AND target_id=? AND target_version=?",
                (
                    "f" * 64,
                    head_target["kind"],
                    head_target["id"],
                    head_target["version"],
                ),
            )

        lifecycle = self.store.plugin_lifecycle_view(include_history=True)
        tampered_event_target = next(
            row
            for row in lifecycle["targets"]
            if row["kind"] == "capability_pack"
            and row["id"] == "structured_project_research"
        )
        tampered_head_target = next(
            row
            for row in lifecycle["targets"]
            if row["kind"] == head_target["kind"]
            and row["id"] == head_target["id"]
        )
        self.assertFalse(lifecycle["integrity_ok"])
        for target in (tampered_event_target, tampered_head_target):
            self.assertFalse(target["integrity_ok"])
            self.assertFalse(target["runtime_available"])
            self.assertFalse(target["new_bindings_allowed"])
            self.assertEqual(target["runtime_state"], "lifecycle_integrity_failed")
            self.assertEqual(target["available_actions"], [])
            self.assertEqual(target["history"], [])

    def test_room_round_and_artifact_keep_the_exact_frozen_resolution(self) -> None:
        room = self._create_room(["structured_project_research"])
        room_resolution = copy.deepcopy(room["plugin_lifecycle_resolution"])
        verified_room_resolution = validate_lifecycle_resolution(
            room_resolution,
            room["plugin_registry_snapshot"],
        )
        self.assertEqual(
            verified_room_resolution["resolution_sha256"],
            room["plugin_lifecycle_resolution_sha256"],
        )
        self.assertEqual(
            [
                (row["kind"], row["id"], row["version"], row["sha256"])
                for row in verified_room_resolution["targets"]
            ],
            [
                (row["kind"], row["id"], row["version"], row["sha256"])
                for row in snapshot_target_refs(room["plugin_registry_snapshot"])
            ],
        )

        lifecycle_current = room["plugin_lifecycle_current"]
        round_row = self.store.create_formal_round(
            room["id"],
            "P25 exact lifecycle freeze",
            expected_settings_version=room["settings_version"],
            expected_plugin_registry_snapshot_sha256=room[
                "plugin_registry_snapshot_sha256"
            ],
            expected_plugin_lifecycle_head_set_sha256=lifecycle_current[
                "current_head_set_sha256"
            ],
        )
        self._save_frozen_checkpoint(room, round_row)
        frozen_round_resolution = copy.deepcopy(
            round_row["plugin_lifecycle_resolution"]
        )
        self.assertEqual(
            frozen_round_resolution["lifecycle_head_set_sha256"],
            lifecycle_current["current_head_set_sha256"],
        )
        artifact = self.store.create_artifact(
            room["id"],
            round_id=round_row["id"],
            title="P25 round-bound lifecycle artifact",
            content=self._artifact_content(),
        )
        self.assertIsNotNone(artifact)

        self._transition(
            "capability_pack",
            "structured_project_research",
            "disable",
        )
        room_after = self.store.room_snapshot(room["id"])["room"]
        round_after = self.store.get_round(room["id"], round_row["id"])
        self.assertEqual(room_after["plugin_lifecycle_resolution"], room_resolution)
        self.assertTrue(room_after["plugin_lifecycle_resolution_integrity_ok"])
        self.assertFalse(room_after["plugin_lifecycle_current"]["runtime_available"])
        self.assertEqual(
            round_after["plugin_lifecycle_resolution"],
            frozen_round_resolution,
        )
        self.assertTrue(round_after["plugin_lifecycle_resolution_integrity_ok"])

        historical_artifact = self.store.get_artifact(
            room["id"],
            artifact["id"],
        )
        context = historical_artifact["plugin_registry_context"]
        self.assertEqual(
            context["lifecycle_resolution"],
            frozen_round_resolution,
        )
        self.assertTrue(context["lifecycle_resolution_integrity_ok"])
        self.assertFalse(context["runtime_available"])
        self.assertFalse(context["lifecycle_current"]["runtime_available"])
        with self.assertRaises(PluginLifecycleError):
            self.store.create_artifact(
                room["id"],
                round_id=round_row["id"],
                title="P25 blocked post-disable artifact",
                content=self._artifact_content(),
            )

        with self.assertRaises(PluginLifecycleError) as caught:
            self.store.create_formal_round(
                room["id"],
                "P25 disabled target must block a new formal round",
                expected_settings_version=room_after["settings_version"],
                expected_plugin_registry_snapshot_sha256=room_after[
                    "plugin_registry_snapshot_sha256"
                ],
            )
        self.assertEqual(caught.exception.code, "PLUGIN_LIFECYCLE_TARGET_UNAVAILABLE")

    def test_disabled_pack_stays_filtered_in_metadata_and_noop_room_responses(self) -> None:
        room = self._create_room(["structured_project_research"])
        self.assertIn("research.project.evidence_map", room["capabilities"])
        self._transition(
            "capability_pack",
            "structured_project_research",
            "disable",
        )

        metadata_updated = self.store.update_room(room["id"], {
            "expected_settings_version": room["settings_version"],
            "title": "P25 lifecycle-filtered metadata response",
        })
        self.assertIsNotNone(metadata_updated)
        self.assertIn("plugin_lifecycle_current", metadata_updated)
        self.assertFalse(
            metadata_updated["plugin_lifecycle_current"]["runtime_available"]
        )
        self.assertNotIn(
            "structured_project_research",
            metadata_updated["active_capability_pack_ids"],
        )
        self.assertIn(
            "structured_project_research",
            metadata_updated["inactive_capability_pack_ids"],
        )
        self.assertNotIn(
            "research.project.evidence_map",
            metadata_updated["capabilities"],
        )

        no_op = self.store.update_room(room["id"], {
            "expected_settings_version": metadata_updated["settings_version"],
            "title": metadata_updated["title"],
        })
        self.assertIsNotNone(no_op)
        self.assertIn("plugin_lifecycle_current", no_op)
        self.assertFalse(no_op["plugin_lifecycle_current"]["runtime_available"])
        self.assertNotIn(
            "research.project.evidence_map",
            no_op["capabilities"],
        )

    def test_disabled_and_quarantined_block_actions_and_adapter_but_not_user_decision(self) -> None:
        room = self._create_room(["storage_research_readonly"])
        material = self.store.add_material(room["id"], {
            "title": "P25 user-owned decision evidence",
            "kind": "note",
            "content": "The user retains the final decision after lifecycle changes.",
        })
        artifact = self.store.create_artifact(
            room["id"],
            title="P25 user decision remains core-owned",
            content={
                **self._artifact_content(),
                "summary_evidence": [{
                    "type": "material",
                    "id": material["id"],
                    "evidence_role": "support",
                    "verification_status": "source_checked",
                    "review_note": "P25 isolated local evidence",
                }],
            },
        )
        self.assertIsNotNone(artifact)
        confirmed = self.store.confirm_artifact(
            room["id"],
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )
        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertEqual(len(DEFAULT_DOMAIN_ADAPTERS.active_for_room(room)), 1)
        self.store.require_room_plugin_action(
            room["id"],
            "candidate_experiment.run_historical",
        )

        self._transition("domain_adapter", "storage_research", "disable")
        disabled_room = self.store.room_snapshot(room["id"])["room"]
        self.assertFalse(disabled_room["plugin_lifecycle_current"]["runtime_available"])
        self.assertNotIn(
            "candidate_experiment.run_historical",
            disabled_room["plugin_lifecycle_current"]["available_action_ids"],
        )
        with self.assertRaises(PluginLifecycleError) as caught:
            self.store.require_room_plugin_action(
                room["id"],
                "candidate_experiment.run_historical",
            )
        self.assertEqual(caught.exception.code, "PLUGIN_LIFECYCLE_ACTION_UNAVAILABLE")
        with self.assertRaises(DomainAdapterError):
            DEFAULT_DOMAIN_ADAPTERS.active_for_room(disabled_room)

        no_market = _NoMarketCalls()
        experiment = CandidateExperimentService(self.store, no_market)
        with self.assertRaises(CandidateExperimentError) as caught:
            experiment.run(room["id"], {})
        self.assertEqual(
            caught.exception.code,
            "CANDIDATE_EXPERIMENT_PLUGIN_UNAVAILABLE",
        )
        self.assertEqual(no_market.call_count, 0)

        decision = self.store.create_artifact_user_decision(
            room["id"],
            confirmed["id"],
            expected_version=confirmed["version"],
            action="hold",
            rationale="Lifecycle state does not replace the user's final decision.",
        )
        self.assertIsNotNone(decision)
        self.assertEqual(decision["action"], "hold")
        self.assertEqual(decision["execution_capability"], "none")
        self.assertFalse(decision["live_trading_allowed"])
        self.assertFalse(decision["can_autonomously_decide"])

        self._transition("domain_adapter", "storage_research", "enable")
        self._transition("domain_adapter", "storage_research", "quarantine")
        quarantined_room = self.store.room_snapshot(room["id"])["room"]
        self.assertFalse(quarantined_room["plugin_lifecycle_current"]["runtime_available"])
        with self.assertRaises(PluginLifecycleError):
            self.store.require_room_plugin_action(
                room["id"],
                "candidate_experiment.run_historical",
            )
        with self.assertRaises(DomainAdapterError):
            DEFAULT_DOMAIN_ADAPTERS.active_for_room(quarantined_room)
        self.assertEqual(no_market.call_count, 0)

        with closing(sqlite3.connect(self.db_path)) as connection:
            provider_calls = connection.execute(
                "SELECT COUNT(*) FROM provider_call_attempts"
            ).fetchone()[0]
        self.assertEqual(provider_calls, 0)
        self.assertTrue(
            quarantined_room["plugin_lifecycle_current"][
                "user_final_decision_unaffected"
            ]
        )

    def test_disabled_project_pack_is_absent_from_direct_and_formal_prompts(self) -> None:
        room = self._create_room(["structured_project_research"])
        member = self.store.enabled_members(room["id"])[0]
        enabled_prompt = capability_pack_prompt(["structured_project_research"])
        unique_pack_rule = "领域能力协议【结构化项目研究协议】"
        self.assertIn(unique_pack_rule, enabled_prompt)

        self._transition(
            "capability_pack",
            "structured_project_research",
            "disable",
        )
        disabled_room = self.store.room_snapshot(room["id"])["room"]
        self.assertEqual(disabled_room["active_capability_pack_ids"], [])
        self.assertIn(
            "structured_project_research",
            disabled_room["inactive_capability_pack_ids"],
        )
        self.assertNotIn(
            "research.project.evidence_map",
            disabled_room["capabilities"],
        )

        provider = _NoCallProvider()
        market = _NoMarketCalls()
        orchestrator = DiscussionOrchestrator(
            self.store,
            _NoCallProviderRegistry(provider),
            market_service=market,
        )
        with patch(
            "backend.orchestrator.capability_pack_prompt",
            wraps=capability_pack_prompt,
        ) as prompt_builder:
            direct_prompt = orchestrator._instructions(
                disabled_room,
                member,
                "user",
                direct_mention=True,
            )
            formal_prompt = orchestrator._instructions(
                disabled_room,
                member,
                "user",
                direct_mention=False,
                turn_contract_required=True,
            )

        self.assertEqual(prompt_builder.call_count, 2)
        for call in prompt_builder.call_args_list:
            self.assertEqual(
                list(call.args[0]),
                disabled_room["active_capability_pack_ids"],
            )
        self.assertNotIn(unique_pack_rule, direct_prompt)
        self.assertNotIn(unique_pack_rule, formal_prompt)
        self.assertNotIn(enabled_prompt, direct_prompt)
        self.assertNotIn(enabled_prompt, formal_prompt)
        self.assertEqual(provider.calls, 0)
        self.assertEqual(market.call_count, 0)

    def test_new_and_paused_round_lifecycle_gates_precede_provider_and_market(self) -> None:
        new_room = self._create_room(["structured_project_research"])
        self._transition(
            "capability_pack",
            "structured_project_research",
            "disable",
        )
        provider = _NoCallProvider()
        market = _NoMarketCalls()
        orchestrator = DiscussionOrchestrator(
            self.store,
            _NoCallProviderRegistry(provider),
            market_service=market,
        )

        new_round_events = list(orchestrator.run_round(
            new_room["id"],
            "P25 disabled pack must stop before any external boundary",
        ))
        self.assertEqual(len(new_round_events), 1)
        self.assertEqual(
            new_round_events[0]["code"],
            "ROUND_PLUGIN_LIFECYCLE_UNAVAILABLE",
        )

        paused_room = self._create_room(["storage_research_readonly"])
        paused_round = self.store.create_formal_round(
            paused_room["id"],
            "P25 frozen storage round",
        )
        saved_checkpoint = self._save_frozen_checkpoint(paused_room, paused_round)
        self.store.request_round_pause(paused_room["id"], paused_round["id"])
        self.assertTrue(self.store.pause_round_at_checkpoint(
            paused_room["id"],
            paused_round["id"],
            saved_checkpoint["state"],
        ))
        self._transition(
            "capability_pack",
            "storage_research_readonly",
            "quarantine",
        )

        with self.assertRaises(PluginLifecycleError) as caught:
            self.store.resume_round(paused_room["id"], paused_round["id"])
        self.assertEqual(
            caught.exception.code,
            "PLUGIN_LIFECYCLE_ACTION_UNAVAILABLE",
        )
        self.assertEqual(
            self.store.get_round(paused_room["id"], paused_round["id"])["status"],
            "PAUSED",
        )

        resume_events = list(orchestrator.run_round(
            paused_room["id"],
            "",
            resume_round_id=paused_round["id"],
        ))
        self.assertEqual(len(resume_events), 1)
        self.assertEqual(
            resume_events[0]["code"],
            "ROUND_PLUGIN_LIFECYCLE_UNAVAILABLE",
        )
        self.assertEqual(
            self.store.get_round(paused_room["id"], paused_round["id"])["status"],
            "PAUSED",
        )
        self.assertEqual(provider.calls, 0)
        self.assertEqual(market.call_count, 0)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_execution_runs"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_call_attempts"
                ).fetchone()[0],
                0,
            )

    def test_paused_round_uses_frozen_pack_ids_after_room_pack_change(self) -> None:
        room = self._create_room(["structured_project_research"])
        round_row = self.store.create_formal_round(
            room["id"],
            "P25 resume exact frozen pack identity",
        )
        saved_checkpoint = self._save_frozen_checkpoint(room, round_row)
        self.store.request_round_pause(room["id"], round_row["id"])
        self.assertTrue(self.store.pause_round_at_checkpoint(
            room["id"],
            round_row["id"],
            saved_checkpoint["state"],
        ))
        changed_room = self.store.update_room(room["id"], {
            "capability_pack_ids": ["storage_research_readonly"],
            "expected_settings_version": room["settings_version"],
        })
        self.assertEqual(
            changed_room["active_capability_pack_ids"],
            ["storage_research_readonly"],
        )

        frozen_runtime = self.store.require_round_plugin_runtime(
            room["id"],
            round_row["id"],
        )
        self.assertEqual(
            frozen_runtime["active_capability_pack_ids"],
            ["structured_project_research"],
        )

        provider = _NoCallProvider()
        market = _NoMarketCalls()
        orchestrator = DiscussionOrchestrator(
            self.store,
            _NoCallProviderRegistry(provider),
            market_service=market,
        )
        observed_rooms: list[dict] = []

        def stop_after_frozen_room(snapshot, *, workflow_policy):
            del workflow_policy
            observed_rooms.append(copy.deepcopy(snapshot["room"]))
            return {"ready": False, "issues": ["test_stop_before_runtime"]}

        with patch.object(
            orchestrator.convergence,
            "workflow_configuration_preflight",
            side_effect=stop_after_frozen_room,
        ):
            events = list(orchestrator.run_round(
                room["id"],
                "",
                resume_round_id=round_row["id"],
            ))

        self.assertEqual(events[0]["code"], "ROUND_WORKFLOW_PREFLIGHT_FAILED")
        self.assertEqual(len(observed_rooms), 1)
        self.assertEqual(
            observed_rooms[0]["capability_pack_ids"],
            ["structured_project_research"],
        )
        self.assertEqual(
            observed_rooms[0]["active_capability_pack_ids"],
            ["structured_project_research"],
        )
        self.assertIn(
            "research.project.evidence_map",
            observed_rooms[0]["capabilities"],
        )
        self.assertNotIn(
            "market.storage.readonly",
            observed_rooms[0]["capabilities"],
        )
        self.assertEqual(
            self.store.get_round(room["id"], round_row["id"])["status"],
            "PAUSED",
        )
        self.assertEqual(provider.calls, 0)
        self.assertEqual(market.call_count, 0)

    def test_event_v2_seals_unavailable_host_implementation_and_replays_exactly(self) -> None:
        current_targets, upgraded = self._targets_with_same_id_ui_upgrade()
        source = next(
            row
            for row in current_targets
            if row["kind"] == "ui_contribution"
            and row["id"] == "project_research.artifact_workspace/v1"
            and row["version"] != upgraded["version"]
        )
        host_upgrade_targets = [
            row
            for row in current_targets
            if not (
                row["kind"] == source["kind"]
                and row["id"] == source["id"]
                and row["version"] == source["version"]
            )
        ]
        with patch(
            "backend.store.plugin_lifecycle_targets",
            return_value=host_upgrade_targets,
        ):
            upgraded_store = StudioStore(self.db_path)
            lifecycle = upgraded_store.plugin_lifecycle_view(include_history=True)
            old_state = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == source["kind"]
                and row["id"] == source["id"]
                and row["version"] == source["version"]
            )
            new_state = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == upgraded["kind"]
                and row["id"] == upgraded["id"]
                and row["version"] == upgraded["version"]
            )
            self.assertTrue(old_state["integrity_ok"])
            self.assertFalse(old_state["implementation_available"])
            self.assertEqual(old_state["runtime_state"], "implementation_unavailable")
            self.assertTrue(new_state["implementation_available"])
            self.assertTrue(new_state["runtime_available"])
            replacement = self._target_ref(new_state)

            deprecated_preview = self._preview(
                old_state,
                "deprecate",
                store=upgraded_store,
                replacement=replacement,
            )
            deprecated_request = self._transition_request(
                deprecated_preview,
                client_request_id="p25-event-v2-deprecate-unavailable-0001",
                reason="P25 old exact implementation is unavailable after host upgrade",
            )
            deprecated_first, deprecated_created = (
                upgraded_store.transition_plugin_lifecycle(deprecated_request)
            )
            self.assertTrue(deprecated_created)
            self.assertEqual(
                deprecated_first["event"]["version"],
                PLUGIN_LIFECYCLE_EVENT_VERSION,
            )
            self.assertFalse(
                deprecated_first["event"]["implementation_available_at_event"]
            )
            self.assertFalse(
                deprecated_first["target"]["implementation_available"]
            )

            current_old = next(
                row
                for row in upgraded_store.plugin_lifecycle_view(
                    include_history=True
                )["targets"]
                if row["kind"] == source["kind"]
                and row["id"] == source["id"]
                and row["version"] == source["version"]
            )
            tombstone_preview = self._preview(
                current_old,
                "tombstone",
                store=upgraded_store,
                replacement=replacement,
            )
            tombstone_request = self._transition_request(
                tombstone_preview,
                client_request_id="p25-event-v2-tombstone-unavailable-0001",
                reason="P25 tombstones the unavailable exact implementation",
            )
            tombstone_first, tombstone_created = (
                upgraded_store.transition_plugin_lifecycle(tombstone_request)
            )
            self.assertTrue(tombstone_created)
            self.assertEqual(
                tombstone_first["event"]["version"],
                PLUGIN_LIFECYCLE_EVENT_VERSION,
            )
            self.assertFalse(
                tombstone_first["event"]["implementation_available_at_event"]
            )

            deprecated_replay, deprecated_replay_created = (
                upgraded_store.transition_plugin_lifecycle(deprecated_request)
            )
            tombstone_replay, tombstone_replay_created = (
                upgraded_store.transition_plugin_lifecycle(tombstone_request)
            )
            self.assertFalse(deprecated_replay_created)
            self.assertFalse(tombstone_replay_created)
            self.assertEqual(deprecated_replay, deprecated_first)
            self.assertEqual(tombstone_replay, tombstone_first)

            terminal_old = next(
                row
                for row in upgraded_store.plugin_lifecycle_view(
                    include_history=True
                )["targets"]
                if row["kind"] == source["kind"]
                and row["id"] == source["id"]
                and row["version"] == source["version"]
            )
            for action in ("enable", "clear_quarantine", "reinstate"):
                with self.subTest(action=action):
                    with self.assertRaises(PluginLifecycleError) as caught:
                        self._preview(
                            terminal_old,
                            action,
                            store=upgraded_store,
                        )
                    self.assertEqual(
                        caught.exception.code,
                        "PLUGIN_LIFECYCLE_TARGET_UNAVAILABLE",
                    )
                    self.assertEqual(caught.exception.status, 409)

        deprecated_after_host_recovery, deprecated_after_created = (
            upgraded_store.transition_plugin_lifecycle(deprecated_request)
        )
        tombstone_after_host_recovery, tombstone_after_created = (
            upgraded_store.transition_plugin_lifecycle(tombstone_request)
        )
        self.assertFalse(deprecated_after_created)
        self.assertFalse(tombstone_after_created)
        self.assertEqual(deprecated_after_host_recovery, deprecated_first)
        self.assertEqual(tombstone_after_host_recovery, tombstone_first)
        self.assertFalse(
            deprecated_after_host_recovery["event"][
                "implementation_available_at_event"
            ]
        )
        self.assertFalse(
            deprecated_after_host_recovery["target"]["implementation_available"]
        )

    def test_reopen_preserves_legacy_v1_event_hash_and_integrity(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="ai-studio-p25-legacy-v1-schema-"
        ) as fixture_dir:
            fixture_path = Path(fixture_dir) / "studio.sqlite3"
            StudioStore(fixture_path)
            target = next(
                row
                for row in plugin_lifecycle_targets()
                if row["kind"] == "capability_pack"
                and row["id"] == "structured_project_research"
            )
            event_id = "plugin_lifecycle_event_legacy_golden_v1"
            reason = "P25 independent legacy v1 golden event"
            client_request_id = "p25-legacy-v1-golden-event-0001"
            request_semantics_sha256 = canonical_sha256({
                "fixture": "independent_legacy_v1",
                "action": "disable",
            })

            with closing(sqlite3.connect(fixture_path)) as connection, connection:
                connection.row_factory = sqlite3.Row
                head = dict(connection.execute(
                    """SELECT * FROM plugin_lifecycle_heads
                       WHERE target_kind=? AND target_id=? AND target_version=?""",
                    (target["kind"], target["id"], target["version"]),
                ).fetchone())
                created_at = int(head["updated_at"]) + 1
                golden_payload = {
                    "version": PLUGIN_LIFECYCLE_EVENT_VERSION_V1,
                    "id": event_id,
                    "target_kind": target["kind"],
                    "target_id": target["id"],
                    "target_version": target["version"],
                    "target_sha256": target["sha256"],
                    "sequence_no": 1,
                    "action": "disable",
                    "previous_event_id": "",
                    "previous_event_sha256": "",
                    "catalog_state": "active",
                    "activation_state": "disabled",
                    "resume_activation_state": "disabled",
                    "replacement": None,
                    "reason": reason,
                    "client_request_id": client_request_id,
                    "request_semantics_sha256": request_semantics_sha256,
                    "created_at": created_at,
                }
                legacy_event_sha256 = canonical_sha256(golden_payload)
                connection.executescript(
                    """
                    DROP TRIGGER IF EXISTS trg_plugin_lifecycle_events_no_update;
                    DROP TRIGGER IF EXISTS trg_plugin_lifecycle_events_no_delete;
                    DROP TABLE plugin_lifecycle_events;
                    CREATE TABLE plugin_lifecycle_events (
                        id TEXT PRIMARY KEY,
                        target_kind TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        target_version TEXT NOT NULL,
                        target_sha256 TEXT NOT NULL CHECK(length(target_sha256)=64),
                        sequence_no INTEGER NOT NULL CHECK(sequence_no>0),
                        action TEXT NOT NULL CHECK(action IN (
                            'disable','enable','quarantine','clear_quarantine',
                            'deprecate','reinstate','tombstone'
                        )),
                        previous_event_id TEXT NOT NULL DEFAULT '',
                        previous_event_sha256 TEXT NOT NULL DEFAULT '',
                        catalog_state TEXT NOT NULL
                            CHECK(catalog_state IN ('active','deprecated','tombstoned')),
                        activation_state TEXT NOT NULL
                            CHECK(activation_state IN ('enabled','disabled','quarantined')),
                        resume_activation_state TEXT NOT NULL
                            CHECK(resume_activation_state IN ('enabled','disabled')),
                        replacement_json TEXT NOT NULL DEFAULT '{}',
                        reason TEXT NOT NULL,
                        client_request_id TEXT NOT NULL UNIQUE,
                        request_semantics_sha256 TEXT NOT NULL
                            CHECK(length(request_semantics_sha256)=64),
                        created_at INTEGER NOT NULL,
                        event_sha256 TEXT NOT NULL UNIQUE CHECK(length(event_sha256)=64),
                        UNIQUE(target_kind,target_id,target_version,sequence_no),
                        FOREIGN KEY(target_kind,target_id,target_version)
                            REFERENCES plugin_lifecycle_targets(
                                target_kind,target_id,target_version
                            )
                    );
                    CREATE INDEX idx_plugin_lifecycle_events_target_sequence
                        ON plugin_lifecycle_events(
                            target_kind,target_id,target_version,sequence_no
                        );
                    """
                )
                connection.execute(
                    """INSERT INTO plugin_lifecycle_events(
                           id,target_kind,target_id,target_version,target_sha256,
                           sequence_no,action,previous_event_id,previous_event_sha256,
                           catalog_state,activation_state,resume_activation_state,
                           replacement_json,reason,client_request_id,
                           request_semantics_sha256,created_at,event_sha256
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event_id,
                        target["kind"],
                        target["id"],
                        target["version"],
                        target["sha256"],
                        1,
                        "disable",
                        "",
                        "",
                        "active",
                        "disabled",
                        "disabled",
                        "{}",
                        reason,
                        client_request_id,
                        request_semantics_sha256,
                        created_at,
                        legacy_event_sha256,
                    ),
                )
                head_payload = StudioStore._plugin_lifecycle_head_payload(
                    target_kind=target["kind"],
                    target_id=target["id"],
                    target_version=target["version"],
                    target_sha256=target["sha256"],
                    head_sequence=1,
                    head_event_id=event_id,
                    head_event_sha256=legacy_event_sha256,
                    catalog_state="active",
                    activation_state="disabled",
                    resume_activation_state="disabled",
                    updated_at=created_at,
                )
                connection.execute(
                    """UPDATE plugin_lifecycle_heads SET
                           head_sequence=1,head_event_id=?,head_event_sha256=?,
                           catalog_state='active',activation_state='disabled',
                           resume_activation_state='disabled',updated_at=?,head_sha256=?
                       WHERE target_kind=? AND target_id=? AND target_version=?""",
                    (
                        event_id,
                        legacy_event_sha256,
                        created_at,
                        canonical_sha256(head_payload),
                        target["kind"],
                        target["id"],
                        target["version"],
                    ),
                )
                connection.execute(
                    "DELETE FROM schema_migrations WHERE key='plugin_lifecycle_ledger_v1'"
                )
                old_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(plugin_lifecycle_events)"
                    ).fetchall()
                }
                self.assertNotIn("event_version", old_columns)
                self.assertNotIn("implementation_available_at_event", old_columns)
                self.assertNotIn("request_semantics_json", old_columns)

            migrated = StudioStore(fixture_path)
            with closing(sqlite3.connect(fixture_path)) as connection:
                migrated_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(plugin_lifecycle_events)"
                    ).fetchall()
                }
                persisted = connection.execute(
                    """SELECT event_version,implementation_available_at_event,
                              request_semantics_json,event_sha256
                       FROM plugin_lifecycle_events WHERE id=?""",
                    (event_id,),
                ).fetchone()
            self.assertIn("event_version", migrated_columns)
            self.assertIn("implementation_available_at_event", migrated_columns)
            self.assertIn("request_semantics_json", migrated_columns)
            self.assertEqual(persisted[0], PLUGIN_LIFECYCLE_EVENT_VERSION_V1)
            self.assertIsNone(persisted[1])
            self.assertIsNone(persisted[2])
            self.assertEqual(persisted[3], legacy_event_sha256)

            lifecycle = migrated.plugin_lifecycle_view(include_history=True)
            self.assertTrue(lifecycle["integrity_ok"])
            state = next(
                row
                for row in lifecycle["targets"]
                if row["kind"] == target["kind"]
                and row["id"] == target["id"]
                and row["version"] == target["version"]
            )
            self.assertTrue(state["integrity_ok"])
            self.assertEqual(state["current_event_sha256"], legacy_event_sha256)
            self.assertEqual(
                state["history"][0]["version"],
                PLUGIN_LIFECYCLE_EVENT_VERSION_V1,
            )
            self.assertEqual(
                state["history"][0]["event_sha256"],
                legacy_event_sha256,
            )
            self.assertTrue(
                state["history"][0]["implementation_available_at_event"]
            )

            reopened = StudioStore(fixture_path)
            reopened_state = next(
                row
                for row in reopened.plugin_lifecycle_view(
                    include_history=True
                )["targets"]
                if row["kind"] == target["kind"]
                and row["id"] == target["id"]
                and row["version"] == target["version"]
            )
            self.assertTrue(reopened_state["integrity_ok"])
            self.assertEqual(
                reopened_state["current_event_sha256"],
                legacy_event_sha256,
            )

            with closing(sqlite3.connect(fixture_path)) as connection, connection:
                connection.execute(
                    "DROP TRIGGER trg_plugin_lifecycle_events_no_update"
                )
                connection.execute(
                    """UPDATE plugin_lifecycle_events
                       SET implementation_available_at_event=1 WHERE id=?""",
                    (event_id,),
                )
            tampered = reopened.plugin_lifecycle_view(include_history=True)
            tampered_state = next(
                row
                for row in tampered["targets"]
                if row["kind"] == target["kind"]
                and row["id"] == target["id"]
                and row["version"] == target["version"]
            )
            self.assertFalse(tampered["integrity_ok"])
            self.assertFalse(tampered_state["integrity_ok"])
            self.assertEqual(
                tampered_state["runtime_state"],
                "lifecycle_integrity_failed",
            )
            self.assertEqual(tampered_state["history"], [])

    def test_v2_recovery_event_with_false_implementation_flag_fails_closed_even_rehashed(self) -> None:
        current_targets, upgraded = self._targets_with_same_id_ui_upgrade()
        source = next(
            row
            for row in current_targets
            if row["kind"] == "ui_contribution"
            and row["id"] == "project_research.artifact_workspace/v1"
            and row["version"] != upgraded["version"]
        )
        host_upgrade_targets = [
            row
            for row in current_targets
            if not (
                row["kind"] == source["kind"]
                and row["id"] == source["id"]
                and row["version"] == source["version"]
            )
        ]
        for precursor, recovery in (
            ("disable", "enable"),
            ("quarantine", "clear_quarantine"),
            ("deprecate", "reinstate"),
        ):
            with self.subTest(recovery=recovery), tempfile.TemporaryDirectory(
                prefix=f"ai-studio-p25-v2-{recovery}-"
            ) as fixture_dir:
                fixture_path = Path(fixture_dir) / "studio.sqlite3"
                fixture_store = StudioStore(fixture_path)
                self._transition(
                    source["kind"],
                    source["id"],
                    precursor,
                    store=fixture_store,
                )
                recovery_result, recovery_created, _request = self._transition(
                    source["kind"],
                    source["id"],
                    recovery,
                    store=fixture_store,
                )
                self.assertTrue(recovery_created)
                event_id = recovery_result["event"]["id"]

                with closing(sqlite3.connect(fixture_path)) as connection, connection:
                    connection.row_factory = sqlite3.Row
                    event = dict(connection.execute(
                        "SELECT * FROM plugin_lifecycle_events WHERE id=?",
                        (event_id,),
                    ).fetchone())
                    self.assertEqual(
                        event["event_version"],
                        PLUGIN_LIFECYCLE_EVENT_VERSION,
                    )
                    self.assertEqual(event["implementation_available_at_event"], 1)
                    event["implementation_available_at_event"] = 0
                    rehashed_event_sha256 = canonical_sha256(
                        StudioStore._plugin_lifecycle_event_payload(event)
                    )
                    connection.execute(
                        "DROP TRIGGER trg_plugin_lifecycle_events_no_update"
                    )
                    connection.execute(
                        """UPDATE plugin_lifecycle_events
                           SET implementation_available_at_event=0,event_sha256=?
                           WHERE id=?""",
                        (rehashed_event_sha256, event_id),
                    )
                    head = dict(connection.execute(
                        """SELECT * FROM plugin_lifecycle_heads
                           WHERE target_kind=? AND target_id=? AND target_version=?""",
                        (
                            source["kind"],
                            source["id"],
                            source["version"],
                        ),
                    ).fetchone())
                    self.assertEqual(head["head_event_id"], event_id)
                    head_payload = StudioStore._plugin_lifecycle_head_payload(
                        target_kind=head["target_kind"],
                        target_id=head["target_id"],
                        target_version=head["target_version"],
                        target_sha256=head["target_sha256"],
                        head_sequence=head["head_sequence"],
                        head_event_id=head["head_event_id"],
                        head_event_sha256=rehashed_event_sha256,
                        catalog_state=head["catalog_state"],
                        activation_state=head["activation_state"],
                        resume_activation_state=head["resume_activation_state"],
                        updated_at=head["updated_at"],
                    )
                    connection.execute(
                        """UPDATE plugin_lifecycle_heads
                           SET head_event_sha256=?,head_sha256=?
                           WHERE target_kind=? AND target_id=? AND target_version=?""",
                        (
                            rehashed_event_sha256,
                            canonical_sha256(head_payload),
                            source["kind"],
                            source["id"],
                            source["version"],
                        ),
                    )

                with patch(
                    "backend.store.plugin_lifecycle_targets",
                    return_value=host_upgrade_targets,
                ):
                    reopened = StudioStore(fixture_path)
                    lifecycle = reopened.plugin_lifecycle_view(
                        include_history=True
                    )
                old_state = next(
                    row
                    for row in lifecycle["targets"]
                    if row["kind"] == source["kind"]
                    and row["id"] == source["id"]
                    and row["version"] == source["version"]
                )
                self.assertFalse(lifecycle["integrity_ok"])
                self.assertFalse(old_state["integrity_ok"])
                self.assertFalse(old_state["runtime_available"])
                self.assertFalse(old_state["new_bindings_allowed"])
                self.assertEqual(
                    old_state["runtime_state"],
                    "lifecycle_integrity_failed",
                )
                self.assertEqual(old_state["available_actions"], [])
                self.assertEqual(old_state["history"], [])

    def test_unrelated_integrity_failure_blocks_new_preview_and_commit_but_not_replay(self) -> None:
        replay_state = self._state(
            "capability_pack",
            "structured_project_research",
        )
        replay_preview = self._preview(replay_state, "disable")
        replay_request = self._transition_request(
            replay_preview,
            client_request_id="p25-closure-existing-replay-0001",
            reason="P25 historical replay survives unrelated corruption",
        )
        replay_first, replay_created = self.store.transition_plugin_lifecycle(
            replay_request
        )
        self.assertTrue(replay_created)

        healthy_state = self._state(
            "capability_pack",
            "storage_research_readonly",
        )
        healthy_preview = self._preview(healthy_state, "disable")
        pending_request = self._transition_request(
            healthy_preview,
            client_request_id="p25-closure-new-commit-0001",
            reason="P25 new commit must see whole catalog integrity",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            event_count_before = connection.execute(
                "SELECT COUNT(*) FROM plugin_lifecycle_events"
            ).fetchone()[0]
            healthy_head_before = connection.execute(
                """SELECT head_sequence,head_event_id,head_event_sha256,head_sha256
                   FROM plugin_lifecycle_heads
                   WHERE target_kind=? AND target_id=? AND target_version=?""",
                (
                    healthy_state["kind"],
                    healthy_state["id"],
                    healthy_state["version"],
                ),
            ).fetchone()

        unrelated = self._state(
            "ui_contribution",
            "project_research.artifact_workspace/v1",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE plugin_lifecycle_heads SET head_sha256=?
                   WHERE target_kind=? AND target_id=? AND target_version=?""",
                (
                    "0" * 64,
                    unrelated["kind"],
                    unrelated["id"],
                    unrelated["version"],
                ),
            )

        with self.assertRaises(PluginLifecycleError) as preview_caught:
            self._preview(healthy_state, "disable")
        self.assertEqual(
            preview_caught.exception.code,
            "PLUGIN_LIFECYCLE_INTEGRITY_FAILED",
        )
        self.assertEqual(preview_caught.exception.status, 409)
        with self.assertRaises(PluginLifecycleError) as commit_caught:
            self.store.transition_plugin_lifecycle(pending_request)
        self.assertEqual(
            commit_caught.exception.code,
            "PLUGIN_LIFECYCLE_INTEGRITY_FAILED",
        )
        self.assertEqual(commit_caught.exception.status, 409)

        replay_after, replay_after_created = (
            self.store.transition_plugin_lifecycle(replay_request)
        )
        self.assertFalse(replay_after_created)
        self.assertEqual(replay_after, replay_first)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM plugin_lifecycle_events"
                ).fetchone()[0],
                event_count_before,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT head_sequence,head_event_id,head_event_sha256,head_sha256
                       FROM plugin_lifecycle_heads
                       WHERE target_kind=? AND target_id=? AND target_version=?""",
                    (
                        healthy_state["kind"],
                        healthy_state["id"],
                        healthy_state["version"],
                    ),
                ).fetchone(),
                healthy_head_before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_call_attempts"
                ).fetchone()[0],
                0,
            )

    def test_unrelated_catalog_failure_blocks_optional_pack_room_create_with_zero_writes(self) -> None:
        unrelated = self._state(
            "ui_contribution",
            "project_research.artifact_workspace/v1",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            room_count_before = connection.execute(
                "SELECT COUNT(*) FROM rooms"
            ).fetchone()[0]
            room_version_count_before = connection.execute(
                "SELECT COUNT(*) FROM room_versions"
            ).fetchone()[0]
            connection.execute(
                """UPDATE plugin_lifecycle_heads SET head_sha256=?
                   WHERE target_kind=? AND target_id=? AND target_version=?""",
                (
                    "0" * 64,
                    unrelated["kind"],
                    unrelated["id"],
                    unrelated["version"],
                ),
            )

        with self.assertRaises(PluginLifecycleError) as caught:
            self.store.create_room(
                "P25 blocked catalog binding room",
                "Healthy optional pack still requires the complete catalog gate",
                capability_pack_ids=["storage_research_readonly"],
            )
        self.assertEqual(
            caught.exception.code,
            "PLUGIN_LIFECYCLE_INTEGRITY_FAILED",
        )
        self.assertEqual(caught.exception.status, 409)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM rooms").fetchone()[0],
                room_count_before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM room_versions"
                ).fetchone()[0],
                room_version_count_before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_call_attempts"
                ).fetchone()[0],
                0,
            )

    def test_catalog_failure_blocks_pack_add_without_version_write_but_allows_removal(self) -> None:
        room = self._create_room(["structured_project_research"])
        selected_target = self._state(
            "ui_contribution",
            "project_research.artifact_workspace/v1",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            room_row_before = connection.execute(
                """SELECT title,objective,template_id,capability_packs_json,
                          plugin_registry_snapshot_json,
                          plugin_lifecycle_resolution_json,
                          plugin_lifecycle_resolution_sha256,
                          settings_version,updated_at
                   FROM rooms WHERE id=?""",
                (room["id"],),
            ).fetchone()
            room_version_count_before = connection.execute(
                "SELECT COUNT(*) FROM room_versions WHERE room_id=?",
                (room["id"],),
            ).fetchone()[0]
            connection.execute(
                """UPDATE plugin_lifecycle_heads SET head_sha256=?
                   WHERE target_kind=? AND target_id=? AND target_version=?""",
                (
                    "0" * 64,
                    selected_target["kind"],
                    selected_target["id"],
                    selected_target["version"],
                ),
            )

        with self.assertRaises(PluginLifecycleError) as caught:
            self.store.update_room(room["id"], {
                "capability_pack_ids": [
                    "structured_project_research",
                    "storage_research_readonly",
                ],
                "expected_settings_version": room["settings_version"],
            })
        self.assertEqual(
            caught.exception.code,
            "PLUGIN_LIFECYCLE_INTEGRITY_FAILED",
        )
        self.assertEqual(caught.exception.status, 409)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT title,objective,template_id,capability_packs_json,
                              plugin_registry_snapshot_json,
                              plugin_lifecycle_resolution_json,
                              plugin_lifecycle_resolution_sha256,
                              settings_version,updated_at
                       FROM rooms WHERE id=?""",
                    (room["id"],),
                ).fetchone(),
                room_row_before,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM room_versions WHERE room_id=?",
                    (room["id"],),
                ).fetchone()[0],
                room_version_count_before,
            )

        recovered = self.store.update_room(room["id"], {
            "capability_pack_ids": [],
            "expected_settings_version": room["settings_version"],
        })
        self.assertEqual(recovered["capability_pack_ids"], [])
        self.assertEqual(recovered["active_capability_pack_ids"], [])
        self.assertNotIn(
            "structured_project_research",
            recovered["inactive_capability_pack_ids"],
        )
        self.assertEqual(
            recovered["settings_version"],
            room["settings_version"] + 1,
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM room_versions WHERE room_id=?",
                    (room["id"],),
                ).fetchone()[0],
                room_version_count_before + 1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_call_attempts"
                ).fetchone()[0],
                0,
            )

    def test_disabled_storage_pack_is_not_applicable_to_sample_acceptance(self) -> None:
        room = self._create_room(["storage_research_readonly"])
        self.assertIn("market.storage.readonly", room["capabilities"])
        self._transition(
            "capability_pack",
            "storage_research_readonly",
            "disable",
        )
        snapshot = self.store.room_snapshot(room["id"])
        disabled_room = snapshot["room"]
        self.assertIn(
            "storage_research_readonly",
            disabled_room["capability_pack_ids"],
        )
        self.assertNotIn(
            "storage_research_readonly",
            disabled_room["active_capability_pack_ids"],
        )
        self.assertNotIn("market.storage.readonly", disabled_room["capabilities"])

        acceptance = StorageSampleAcceptance(self.store).evaluate(
            room["id"],
            snapshot=snapshot,
            convergence_state={},
        )
        self.assertFalse(acceptance["applicable"])
        self.assertEqual(acceptance["state"], "not_applicable")
        self.assertFalse(acceptance["acceptance_ready"])
        self.assertEqual(acceptance["provider_calls"], 0)
        self.assertEqual(acceptance["market_calls"], 0)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_call_attempts"
                ).fetchone()[0],
                0,
            )


class PluginLifecycleHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-p25-plugin-lifecycle-http-"
        )
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        self.original_store = http_server.STORE
        http_server.STORE = self.store
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def _json_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict | None = None,
    ) -> tuple[int, dict]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if payload is not None:
            headers = {
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            }
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_http_preview_create_replay_and_typed_conflict(self) -> None:
        get_status, lifecycle_response = self._json_request(
            "/api/plugin-registry/lifecycle"
        )
        self.assertEqual(get_status, 200)
        lifecycle = lifecycle_response["plugin_lifecycle"]
        self.assertTrue(lifecycle["integrity_ok"])
        target = next(
            row
            for row in lifecycle["targets"]
            if row["kind"] == "capability_pack"
            and row["id"] == "project_round_focus"
        )
        target_ref = {
            "kind": target["kind"],
            "id": target["id"],
            "version": target["version"],
            "sha256": target["target_sha256"],
        }
        preview_request = {
            "version": PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION,
            "target": target_ref,
            "action": "disable",
            "expected_head_sequence": target["head_sequence"],
            "expected_head_sha256": target["head_sha256"],
            "replacement": None,
        }
        preview_status, preview_response = self._json_request(
            "/api/plugin-registry/lifecycle-events/preview",
            method="POST",
            payload=preview_request,
        )
        self.assertEqual(preview_status, 200)
        preview = preview_response["preview"]
        transition_request = {
            "version": PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION,
            "client_request_id": "p25-http-transition-0001",
            "target": target_ref,
            "action": "disable",
            "expected_head_sequence": target["head_sequence"],
            "expected_head_sha256": target["head_sha256"],
            "replacement": None,
            "impact_preview_sha256": preview["preview_sha256"],
            "reason": "P25 isolated HTTP lifecycle transition",
            "user_confirmed_history_preserved": True,
            "user_confirmed_no_automatic_migration": True,
        }
        created_status, created_response = self._json_request(
            "/api/plugin-registry/lifecycle-events",
            method="POST",
            payload=transition_request,
        )
        replay_status, replay_response = self._json_request(
            "/api/plugin-registry/lifecycle-events",
            method="POST",
            payload=transition_request,
        )
        self.assertEqual(created_status, 201)
        self.assertFalse(created_response["idempotent_replay"])
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay_response["idempotent_replay"])
        self.assertEqual(
            replay_response["transition"]["event"]["id"],
            created_response["transition"]["event"]["id"],
        )
        changed = {
            **transition_request,
            "reason": "P25 changed HTTP lifecycle semantics",
        }
        conflict_status, conflict_response = self._json_request(
            "/api/plugin-registry/lifecycle-events",
            method="POST",
            payload=changed,
        )
        self.assertEqual(conflict_status, 409)
        self.assertEqual(
            conflict_response["code"],
            "PLUGIN_LIFECYCLE_IDEMPOTENCY_CONFLICT",
        )
        invalid_status, invalid_response = self._json_request(
            "/api/plugin-registry/lifecycle-events/preview",
            method="POST",
            payload={**preview_request, "unexpected": True},
        )
        self.assertEqual(invalid_status, 400)
        self.assertEqual(
            invalid_response["code"],
            "PLUGIN_LIFECYCLE_REQUEST_INVALID",
        )

        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM provider_call_attempts"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
