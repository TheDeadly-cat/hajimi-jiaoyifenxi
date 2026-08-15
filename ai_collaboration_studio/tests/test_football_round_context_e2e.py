from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.football_research_service import FootballResearchService
from backend.orchestrator import DiscussionOrchestrator
from backend.round_contexts import (
    FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_VERSION,
    FOOTBALL_ROUND_CONTEXT_REQUEST_VERSION,
    ROUND_CONTEXT_AUTHORIZATION_SET_VERSION,
    build_round_context_authorization_set,
    prompt_sections,
    round_context_authorization_entry,
)
from backend.round_launch_plan import (
    ROUND_LAUNCH_PLAN_VERSION_V5,
    RoundLaunchPlanService,
)
from backend.store import StudioStore
from tests.test_football_research import payload as football_payload


FOOTBALL_PACK_ID = "football_research_readonly"
FOOTBALL_PORT_ID = "core.football.match_context/v1"


def _iter_bound_sources(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        source = value.get("source")
        if isinstance(source, dict) and isinstance(
            source.get("material_binding"), dict
        ):
            yield source
        for child in value.values():
            yield from _iter_bound_sources(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_bound_sources(child)


class _NoCallProvider:
    provider_id = "deepseek"

    def __init__(self) -> None:
        self.generate_calls = 0

    def generate(self, *_args: Any, **_kwargs: Any) -> None:
        self.generate_calls += 1
        raise AssertionError("football round setup must not call a Provider")


class _LocalStatusOnlyRegistry:
    disabled_provider_ids = frozenset()

    def __init__(self) -> None:
        self.provider = _NoCallProvider()
        self.status_calls = 0
        self.get_calls = 0
        self.preflight_calls = 0

    def status(self) -> list[dict[str, Any]]:
        self.status_calls += 1
        return [{
            "id": "deepseek",
            "name": "Isolated DeepSeek fixture",
            "model": "deepseek-offline-test",
            "configured": True,
            "policy_disabled": False,
        }]

    def get(self, _provider_id: str) -> _NoCallProvider:
        self.get_calls += 1
        return self.provider

    def preflight(self, *_args: Any, **_kwargs: Any) -> None:
        self.preflight_calls += 1
        raise AssertionError("football round setup must not probe a Provider")


class FootballRoundContextE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-football-round-e2e-",
            ignore_cleanup_errors=True,
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_room(self) -> dict[str, Any]:
        created = self.store.create_room(
            "Football formal round",
            "Discuss one exact pre-kickoff football evidence snapshot.",
            template_id="football_research",
            capability_pack_ids=[
                FOOTBALL_PACK_ID,
                "structured_turn_contract_v1",
            ],
        )
        room_id = str(created["room"]["id"])
        for member in created.get("members") or []:
            if member.get("enabled") is True:
                updated = self.store.update_member(
                    room_id,
                    str(member["id"]),
                    {
                        "provider": "deepseek",
                        "model": "deepseek-offline-test",
                    },
                    expected_version=int(member["version"]),
                )
                self.assertIsNotNone(updated)
        snapshot = self.store.room_snapshot(room_id)
        self.assertIsNotNone(snapshot)
        room = (snapshot or {})["room"]
        self.assertTrue(room["plugin_registry_integrity_ok"])
        self.assertIn(FOOTBALL_PACK_ID, room["active_capability_pack_ids"])
        return room

    def _material_bound_payload(
        self,
        room_id: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        value = deepcopy(football_payload())
        expected_bindings: list[dict[str, Any]] = []
        for index, source in enumerate(_iter_bound_sources(value), start=1):
            content = f"Exact football formal-round evidence {index}."
            content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
            material = self.store.add_material(room_id, {
                "title": f"Football E2E evidence {index}",
                "kind": "note",
                "content": content,
                "metadata": {
                    "content_sha256": content_sha256,
                    "extraction_method": "manual",
                },
            })
            self.assertIsNotNone(material)
            binding = {
                "material_id": str(material["id"]),
                "material_version": int(material["version"]),
                "content_sha256": content_sha256,
                "snapshot_sha256": str(material["source_snapshot_sha256"]),
            }
            self.assertEqual(binding["material_version"], 1)
            self.assertRegex(binding["content_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(binding["snapshot_sha256"], r"^[0-9a-f]{64}$")
            source["source_uri"] = (
                f"urn:ai-studio:material:{material['id']}:v{material['version']}"
            )
            source["source_sha256"] = content_sha256
            source["material_binding"] = deepcopy(binding)
            expected_bindings.append(binding)
        self.assertGreater(len(expected_bindings), 0)
        return value, expected_bindings

    @staticmethod
    def _authorization_set(
        payload: dict[str, Any],
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        authorization = {
            "version": FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_VERSION,
            "owner_pack_id": FOOTBALL_PACK_ID,
            "port_id": FOOTBALL_PORT_ID,
            "contract_sha256": preview["contract_sha256"],
            "data_cutoff_utc": preview["data_cutoff_utc"],
            "match_id": preview["contract"]["match_identity"]["match_id"][
                "value"
            ],
            "user_confirmed": True,
        }
        request = {
            "version": FOOTBALL_ROUND_CONTEXT_REQUEST_VERSION,
            "payload": deepcopy(payload),
            "authorization": authorization,
        }
        return build_round_context_authorization_set([
            round_context_authorization_entry(
                FOOTBALL_PACK_ID,
                FOOTBALL_PORT_ID,
                request,
            )
        ])

    @staticmethod
    def _first_event(
        orchestrator: DiscussionOrchestrator,
        room_id: str,
        *,
        authorizations: Any = None,
    ) -> dict[str, Any]:
        stream = orchestrator.run_round(
            room_id,
            "Inspect the exact football evidence boundary.",
            round_context_authorizations=authorizations,
        )
        try:
            return next(stream)
        finally:
            stream.close()

    def _round_count(self, room_id: str) -> int:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM rounds WHERE room_id=?",
                (room_id,),
            ).fetchone()[0])

    def _corrupt_material_version_content(
        self,
        binding: dict[str, Any],
    ) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                "DROP TRIGGER IF EXISTS trg_material_versions_no_update"
            )
            row = connection.execute(
                """SELECT id,snapshot_json FROM material_versions
                    WHERE material_id=? AND version=?""",
                (
                    binding["material_id"],
                    int(binding["material_version"]),
                ),
            ).fetchone()
            self.assertIsNotNone(row)
            snapshot = json.loads(str(row[1]))
            snapshot["content"] = f"{snapshot.get('content', '')} DRIFTED"
            connection.execute(
                "UPDATE material_versions SET snapshot_json=? WHERE id=?",
                (json.dumps(snapshot, ensure_ascii=False), row[0]),
            )

    def test_v5_formal_round_atomically_freezes_and_resumes_sealed_football_context(
        self,
    ) -> None:
        room = self._create_room()
        room_id = str(room["id"])
        payload, expected_bindings = self._material_bound_payload(room_id)
        preview = FootballResearchService(self.store).inspect(room_id, payload)
        authorizations = self._authorization_set(payload, preview)
        self.assertEqual(
            authorizations["version"], ROUND_CONTEXT_AUTHORIZATION_SET_VERSION
        )

        registry = _LocalStatusOnlyRegistry()
        plan_service = RoundLaunchPlanService(self.store, registry)
        inspection_counts = {"readonly": 0, "transaction": 0}
        transaction_states: list[bool] = []
        post_inspection_sql: list[list[str]] = []
        network_calls: list[tuple[Any, ...]] = []
        original_inspect = FootballResearchService.inspect
        original_transaction_inspect = FootballResearchService.inspect_from_connection

        def counted_inspect(
            service: FootballResearchService,
            inspected_room_id: str,
            inspected_payload: Any,
        ) -> dict[str, Any]:
            inspection_counts["readonly"] += 1
            return original_inspect(service, inspected_room_id, inspected_payload)

        def counted_transaction_inspect(
            service: FootballResearchService,
            connection: sqlite3.Connection,
            inspected_room_id: str,
            inspected_payload: Any,
        ) -> dict[str, Any]:
            inspection_counts["transaction"] += 1
            transaction_states.append(connection.in_transaction)
            result = original_transaction_inspect(
                service,
                connection,
                inspected_room_id,
                inspected_payload,
            )
            # Keep a per-call trace only after the read-only inspection.  The
            # formal-freeze call's bucket must subsequently receive the INSERT
            # for the sealed context on this exact same connection.
            trace_bucket: list[str] = []
            post_inspection_sql.append(trace_bucket)
            connection.set_trace_callback(
                lambda statement, bucket=trace_bucket: bucket.append(statement)
            )
            return result

        def forbidden_network(*args: Any, **_kwargs: Any) -> None:
            network_calls.append(args)
            raise AssertionError("football formal-round E2E forbids network access")

        objective = "Use only the exact sealed football research context."
        with (
            patch.object(FootballResearchService, "inspect", new=counted_inspect),
            patch.object(
                FootballResearchService,
                "inspect_from_connection",
                new=counted_transaction_inspect,
            ),
            patch("socket.create_connection", new=forbidden_network),
        ):
            launch_plan = plan_service.build(
                room_id,
                objective,
                round_context_authorizations=authorizations,
            )
            self.assertEqual(launch_plan["version"], ROUND_LAUNCH_PLAN_VERSION_V5)
            self.assertEqual(
                launch_plan["round_context_authorizations"], authorizations
            )
            self.assertTrue(launch_plan["ready_for_authorization"])

            orchestrator = DiscussionOrchestrator(
                self.store,
                registry,
                market_service=None,
            )
            stream = orchestrator.run_round(
                room_id,
                objective,
                expected_launch_plan_hash=launch_plan["plan_hash"],
                round_context_authorizations=authorizations,
            )
            started: dict[str, Any] | None = None
            try:
                for event in stream:
                    if event.get("type") == "error":
                        self.fail(f"formal football round failed: {event}")
                    if event.get("type") == "round_started":
                        started = event
                        break
            finally:
                stream.close()

            self.assertIsNotNone(started)
            round_id = str((started or {})["round"]["id"])
            self.assertEqual(
                self.store.get_round(room_id, round_id)["status"], "PAUSED"
            )
            # The launch plan, launch recheck, and formal freeze all use an
            # explicit Store transaction.  Exactly one post-inspection SQL
            # trace then inserts the sealed context, proving revalidation and
            # freeze shared the caller-owned formal-round connection.
            self.assertGreaterEqual(inspection_counts["transaction"], 4)
            self.assertTrue(all(transaction_states))
            freezing_traces = [
                statements
                for statements in post_inspection_sql
                if any(
                    "INSERT INTO ROUND_DOMAIN_CONTEXTS" in statement.upper()
                    for statement in statements
                )
            ]
            self.assertEqual(len(freezing_traces), 1)
            self.assertTrue(any(
                "INSERT INTO ROUNDS" in statement.upper()
                for statement in freezing_traces[0]
            ))
            self.assertEqual(inspection_counts["readonly"], 0)

            frozen = self.store.get_round_contexts(room_id, round_id)
            self.assertTrue(frozen["integrity_ok"])
            self.assertEqual(frozen["round_domain_context_count"], 1)
            self.assertRegex(
                frozen["round_domain_contexts_sha256"], r"^[0-9a-f]{64}$"
            )
            context = frozen["contexts"][0]
            self.assertEqual(context["owner_pack_id"], FOOTBALL_PACK_ID)
            self.assertEqual(context["port_id"], FOOTBALL_PORT_ID)
            self.assertEqual(
                context["preview"]["contract_sha256"],
                preview["contract_sha256"],
            )
            self.assertFalse(context["preview"]["future_probability_available"])
            sealed_sections = prompt_sections(frozen)
            self.assertEqual(len(sealed_sections), 1)
            self.assertEqual(
                sealed_sections[0]["payload"]["view_model"], context["preview"]
            )

            checkpoint = self.store.get_round_checkpoint(room_id, round_id)
            self.assertIsNotNone(checkpoint)
            shared_context = str((checkpoint or {})["state"]["shared_context"])
            self.assertIn("[Frozen round context 1]", shared_context)
            self.assertIn("football_round_context_prompt_payload_v1", shared_context)
            self.assertIn(preview["contract_sha256"], shared_context)
            self.assertIn(FOOTBALL_PACK_ID, shared_context)

            with closing(sqlite3.connect(self.database_path)) as connection:
                row = connection.execute(
                    """SELECT input_seal_json,provider_calls_performed,
                              market_reads_performed,adapter_business_writes_performed
                         FROM round_domain_contexts
                        WHERE room_id=? AND round_id=?""",
                    (room_id, round_id),
                ).fetchone()
            self.assertIsNotNone(row)
            input_seal = json.loads(str(row[0]))
            self.assertEqual(
                input_seal["material_bindings"],
                sorted(
                    expected_bindings,
                    key=lambda item: (
                        item["material_id"], item["material_version"]
                    ),
                ),
            )
            self.assertEqual(tuple(row[1:]), (0, 0, 0))

            counts_before_resume = dict(inspection_counts)
            resume_stream = orchestrator.run_round(
                room_id,
                "",
                resume_round_id=round_id,
            )
            resumed: dict[str, Any] | None = None
            try:
                for event in resume_stream:
                    if event.get("type") == "error":
                        self.fail(f"sealed football round did not resume: {event}")
                    if event.get("type") == "round_resumed":
                        resumed = event
                        break
            finally:
                resume_stream.close()
            self.assertIsNotNone(resumed)
            self.assertEqual(inspection_counts, counts_before_resume)

        # Resume may resolve frozen local provider handles while rebuilding its
        # checkpoint route map.  No handle is invoked before this test closes
        # the stream at round_resumed.
        self.assertEqual(registry.preflight_calls, 0)
        self.assertEqual(registry.provider.generate_calls, 0)
        self.assertEqual(network_calls, [])

    def test_missing_authorization_and_registry_drift_stop_before_provider_calls(
        self,
    ) -> None:
        missing_room = self._create_room()
        missing_registry = _LocalStatusOnlyRegistry()
        missing_event = self._first_event(
            DiscussionOrchestrator(
                self.store,
                missing_registry,
                market_service=None,
            ),
            str(missing_room["id"]),
        )
        self.assertEqual(
            missing_event["code"], "ROUND_CONTEXT_AUTHORIZATION_REQUIRED"
        )
        self.assertEqual(self._round_count(str(missing_room["id"])), 0)
        self.assertEqual(missing_registry.get_calls, 0)
        self.assertEqual(missing_registry.provider.generate_calls, 0)

        registry_room = self._create_room()
        registry_room_id = str(registry_room["id"])
        registry_payload, _bindings = self._material_bound_payload(registry_room_id)
        registry_preview = FootballResearchService(self.store).inspect(
            registry_room_id,
            registry_payload,
        )
        registry_authorizations = self._authorization_set(
            registry_payload,
            registry_preview,
        )
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                "UPDATE rooms SET plugin_registry_snapshot_json='{}' WHERE id=?",
                (registry_room_id,),
            )
        corrupted_snapshot = self.store.room_snapshot(registry_room_id)
        self.assertIsNotNone(corrupted_snapshot)
        self.assertFalse(
            (corrupted_snapshot or {})["room"]["plugin_registry_integrity_ok"]
        )
        corrupt_registry = _LocalStatusOnlyRegistry()
        registry_event = self._first_event(
            DiscussionOrchestrator(
                self.store,
                corrupt_registry,
                market_service=None,
            ),
            registry_room_id,
            authorizations=registry_authorizations,
        )
        self.assertEqual(registry_event["code"], "ROUND_PLUGIN_REGISTRY_INVALID")
        self.assertEqual(self._round_count(registry_room_id), 0)
        self.assertEqual(corrupt_registry.get_calls, 0)
        self.assertEqual(corrupt_registry.provider.generate_calls, 0)

    def test_material_drift_between_precheck_and_transaction_freeze_fails_closed(
        self,
    ) -> None:
        room = self._create_room()
        room_id = str(room["id"])
        payload, bindings = self._material_bound_payload(room_id)
        preview = FootballResearchService(self.store).inspect(room_id, payload)
        authorizations = self._authorization_set(payload, preview)
        registry = _LocalStatusOnlyRegistry()
        transaction_calls = 0
        create_calls = 0

        original_transaction_inspect = FootballResearchService.inspect_from_connection

        def counted_transaction_inspect(
            service: FootballResearchService,
            connection: sqlite3.Connection,
            inspected_room_id: str,
            inspected_payload: Any,
        ) -> dict[str, Any]:
            nonlocal transaction_calls
            transaction_calls += 1
            self.assertTrue(connection.in_transaction)
            result = original_transaction_inspect(
                service,
                connection,
                inspected_room_id,
                inspected_payload,
            )
            return result

        original_create_formal_round = self.store.create_formal_round

        def create_after_material_drift(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal create_calls
            create_calls += 1
            # Authorization and the general evidence manifest have both passed;
            # drift immediately before Store begins the atomic formal transaction.
            self._corrupt_material_version_content(bindings[0])
            return original_create_formal_round(*args, **kwargs)

        network_calls: list[tuple[Any, ...]] = []

        def forbidden_network(*args: Any, **_kwargs: Any) -> None:
            network_calls.append(args)
            raise AssertionError("material drift E2E forbids network access")

        with (
            patch.object(
                FootballResearchService,
                "inspect_from_connection",
                new=counted_transaction_inspect,
            ),
            patch.object(
                self.store,
                "create_formal_round",
                new=create_after_material_drift,
            ),
            patch("socket.create_connection", new=forbidden_network),
        ):
            events = list(DiscussionOrchestrator(
                self.store,
                registry,
                market_service=None,
            ).run_round(
                room_id,
                "Reject drift before a formal football round is written.",
                round_context_authorizations=authorizations,
            ))

        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0]["code"], "FOOTBALL_RESEARCH_MATERIAL_CONTENT_DRIFT"
        )
        self.assertEqual(transaction_calls, 2)
        self.assertEqual(create_calls, 1)
        self.assertEqual(self._round_count(room_id), 0)
        self.assertEqual(registry.status_calls, 0)
        self.assertEqual(registry.get_calls, 0)
        self.assertEqual(registry.preflight_calls, 0)
        self.assertEqual(registry.provider.generate_calls, 0)
        self.assertEqual(network_calls, [])


if __name__ == "__main__":
    unittest.main()
