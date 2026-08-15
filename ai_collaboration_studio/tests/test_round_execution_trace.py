from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch


# Importing backend.store constructs its module-level default store. Keep that
# harmless import side effect inside a disposable directory so this test can
# never inspect or mutate the project's formal SQLite database.
_IMPORT_TEMP_DIR = tempfile.TemporaryDirectory(prefix="ai-studio-trace-import-")
_PREVIOUS_IMPORT_ENV = {
    key: os.environ.get(key)
    for key in (
        "AI_STUDIO_SKIP_LOCAL_ENV",
        "AI_STUDIO_RUNTIME_DIR",
        "AI_STUDIO_DATABASE_PATH",
    )
}
os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
os.environ["AI_STUDIO_RUNTIME_DIR"] = _IMPORT_TEMP_DIR.name
os.environ["AI_STUDIO_DATABASE_PATH"] = str(
    Path(_IMPORT_TEMP_DIR.name) / "import-only.sqlite3"
)

from backend import http_server  # noqa: E402
from backend.provider_call_ledger import ProviderCallLedger  # noqa: E402
from backend.store import (  # noqa: E402
    PROVIDER_OPERATION_BINDING_VERSION,
    StudioStore,
)
from backend.turn_contract import (  # noqa: E402
    CANDIDATE_RISK_REVIEW_VERSION,
    TURN_CONTRACT_VERSION,
)
from backend.turn_envelope import (  # noqa: E402
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
)
from backend.workflow_policy import default_workflow_policy  # noqa: E402

for _key, _value in _PREVIOUS_IMPORT_ENV.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


TRACE_VERSION = "round_execution_trace_v1"
CORRELATION_LIMITATION = "PROVIDER_OPERATION_CORRELATION_UNAVAILABLE"


def _approved_route(member: dict[str, Any]) -> dict[str, Any]:
    return {
        "member_id": str(member["id"]),
        "approved_member_version": int(member["version"]),
        "provider": str(member.get("provider") or "openai").strip().lower(),
        "model": str(member.get("model") or ""),
        "turn_output_mode": "prompt_json",
        "turn_envelope_version": TURN_ENVELOPE_VERSION,
        "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
    }


def _checkpoint_v9(
    members: list[dict[str, Any]],
    round_row: dict[str, Any],
    successful_member: dict[str, Any],
) -> dict[str, Any]:
    member_ids = [str(member["id"]) for member in members]
    successful_id = str(successful_member["id"])
    return {
        "version": 9,
        "member_ids": member_ids,
        "moderator_member_id": member_ids[0],
        "spoken_counts": {successful_id: 1},
        "spoken_stances": [
            str(successful_member.get("stance") or "neutral")
        ],
        "successful_member_ids": [successful_id],
        "failed_member_ids": [],
        "previous_name": str(successful_member.get("name") or "member"),
        "completed": 1,
        "failures": 0,
        "skipped": 0,
        "proposals_created": 0,
        "next_order": 2,
        "max_turns": max(1, len(member_ids)),
        "shared_context": "",
        "market_snapshot": None,
        "round_evidence_manifest": None,
        "skip_provider_ids": [],
        "workflow_policy": default_workflow_policy("open_collaboration"),
        "capability_pack_ids": [],
        "project_workspace": None,
        "turn_contract_version": TURN_CONTRACT_VERSION,
        "turn_contract_required": True,
        "candidate_risk_review_version": round_row.get(
            "candidate_risk_review_version"
        ),
        "candidate_risk_review_required": (
            round_row.get("candidate_risk_review_version")
            == CANDIDATE_RISK_REVIEW_VERSION
        ),
        "turn_envelope_version": TURN_ENVELOPE_VERSION,
        "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
        "turn_output_modes_by_member": {
            member_id: "prompt_json" for member_id in member_ids
        },
    }


def _valid_contract() -> dict[str, Any]:
    return {
        "version": TURN_CONTRACT_VERSION,
        "claims": [{
            "id": "trace_claim_1",
            "kind": "unknown",
            "text": "This offline trace fixture makes no market claim.",
            "as_of": "",
            "evidence": [],
        }],
        "responds_to": [],
        "candidate_updates": [],
        "risks": [],
        "next_actions": [{
            "id": "trace_action_1",
            "text": "Keep the result in research-only review.",
            "owner": "user",
            "state": "open",
            "due": "after_review",
            "evidence": [],
        }],
        "confidence": {
            "kind": "model_subjective",
            "value": None,
            "label": "unknown",
            "basis": "No Provider is called by this fixture.",
        },
        "confidence_is_not_win_rate": True,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def _minimal_artifact_content() -> dict[str, Any]:
    return {
        "summary": "Offline audit artifact; no Provider or market request was made.",
        "summary_evidence": [],
        "requirements": [],
        "risks": [],
        "conclusions": [],
        "disagreements": [],
        "unknowns": [],
        "actions": [],
        "decision": {
            "status": "undecided",
            "options": [],
            "preferred_option_id": "",
            "rationale": "",
            "evidence": [],
        },
    }


def _issue_codes(items: Any) -> set[str]:
    result: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            code = str(item.get("code") or "")
        else:
            code = str(item or "")
        if code:
            result.add(code.upper())
    return result


def _logical_database_sha256(path: Path) -> str:
    """Hash logical SQLite contents, ignoring WAL/SHM implementation files."""

    with closing(sqlite3.connect(path)) as connection:
        dump = "\n".join(connection.iterdump())
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


class RoundExecutionTraceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="round-trace-store-")
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "trace.sqlite3"
        self.store = StudioStore(self.db_path)
        self.sequence = 0

    def _create_legacy_round(self) -> dict[str, Any]:
        member = self.store.enabled_members("room_plan")[0]
        round_row = self.store.create_round(
            "room_plan",
            "Legacy message-only trace",
        )
        self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=str(member["id"]),
            sender_name=str(member["name"]),
            content="Historical unstructured message.",
            round_id=str(round_row["id"]),
        )
        self.store.complete_round(str(round_row["id"]), "COMPLETED")
        return self.store.get_round("room_plan", str(round_row["id"])) or round_row

    def _create_current_round(
        self,
        *,
        with_provider_ledger: bool,
        operation_binding: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if operation_binding and not with_provider_ledger:
            raise ValueError("operation binding requires the offline provider ledger")
        self.sequence += 1
        members = self.store.enabled_members("room_plan")
        for index, member in enumerate(members, start=1):
            if str(member.get("model") or "").strip():
                continue
            self.store.update_member(
                "room_plan",
                str(member["id"]),
                {
                    "provider": "deepseek",
                    "model": f"offline-trace-model-{index}",
                },
                expected_version=int(member["version"]),
            )
        members = self.store.enabled_members("room_plan")
        moderator = members[0]
        # Keep the fixture contract role-valid without inventing evidence: the
        # facilitator can emit the governance-only action below, while an
        # analysis role would require a grounded, dated claim.
        speaker = moderator
        round_row = self.store.create_formal_round(
            "room_plan",
            f"Current envelope trace {self.sequence}",
        )
        routes = [_approved_route(member) for member in members]
        route_by_member = {route["member_id"]: route for route in routes}
        ledger: ProviderCallLedger | None = None
        provider_attempt_ids: list[str] = []
        secret = f"sk-proj-trace-secret-{self.sequence}"
        shared_context, evidence_manifest = self.store.material_prompt_bundle(
            "room_plan"
        )
        evidence_manifest = self.store.finalize_round_evidence_manifest(
            evidence_manifest,
            shared_context=shared_context,
            market_snapshot=None,
        )

        initial_checkpoint = _checkpoint_v9(members, round_row, speaker)
        initial_checkpoint.update({
            "spoken_counts": {},
            "spoken_stances": [],
            "successful_member_ids": [],
            "previous_name": "user",
            "completed": 0,
            "next_order": 1,
            "shared_context": shared_context,
            "round_evidence_manifest": evidence_manifest,
        })
        self.store.save_round_checkpoint(
            "room_plan",
            str(round_row["id"]),
            initial_checkpoint,
        )

        if with_provider_ledger:
            ledger = ProviderCallLedger.create(
                self.store,
                "room_plan",
                scope="round",
                client_request_id=f"trace-run-{self.sequence}",
                plan_hash=hashlib.sha256(
                    f"trace-plan-{self.sequence}".encode("utf-8")
                ).hexdigest(),
                max_calls=3 if operation_binding else 8,
                member_routes={
                    "version": "provider_member_routes_v2",
                    "members": sorted(routes, key=lambda route: route["member_id"]),
                },
                operation_binding_version=(
                    PROVIDER_OPERATION_BINDING_VERSION
                    if operation_binding
                    else ""
                ),
            )
            ledger.bind_round(str(round_row["id"]))
            preflight = ledger.reserve(
                kind="preflight_probe",
                provider=str(route_by_member[str(moderator["id"])]["provider"]),
                model=str(route_by_member[str(moderator["id"])]["model"]),
                target_type="provider_route" if operation_binding else "",
                target_id=(
                    ledger.route_target_id(
                        str(route_by_member[str(moderator["id"])]["provider"]),
                        str(route_by_member[str(moderator["id"])]["model"]),
                    )
                    if operation_binding
                    else ""
                ),
            )
            provider_attempt_ids.append(str(preflight["id"]))
            ledger.finish(
                str(preflight["id"]),
                str(preflight["attempt_token"]),
                status="RESPONDED",
                elapsed_ms=2,
                usage={"input_tokens": 1, "output_tokens": 1},
            )

        director_attempt = self.store.begin_director_attempt(
            "room_plan",
            str(round_row["id"]),
            moderator_member_id=str(moderator["id"]),
            moderator_member_version=int(moderator["version"]),
            provider=str(route_by_member[str(moderator["id"])]["provider"]),
            model=str(route_by_member[str(moderator["id"])]["model"]),
            approved_route=(
                route_by_member[str(moderator["id"])]
                if with_provider_ledger
                else None
            ),
        )
        director_provider_attempt: dict[str, Any] | None = None
        if ledger is not None:
            director_provider_attempt = ledger.reserve(
                kind="round_director",
                provider=str(route_by_member[str(moderator["id"])]["provider"]),
                model=str(route_by_member[str(moderator["id"])]["model"]),
                member_id=str(moderator["id"]),
                member_version=int(moderator["version"]),
                target_type="director_attempt" if operation_binding else "",
                target_id=(
                    str(director_attempt["id"])
                    if operation_binding
                    else ""
                ),
            )
            provider_attempt_ids.append(str(director_provider_attempt["id"]))
            ledger.finish(
                str(director_provider_attempt["id"]),
                str(director_provider_attempt["attempt_token"]),
                status="RESPONDED",
                elapsed_ms=3,
                usage={"input_tokens": 4, "output_tokens": 2},
            )

        decision = self.store.add_director_decision(
            "room_plan",
            str(round_row["id"]),
            action="speak",
            member_id=str(speaker["id"]),
            member_name=str(speaker["name"]),
            reason="Offline fixture selected one frozen member.",
            source="provider" if with_provider_ledger else "rules_first",
            stage=str(speaker.get("workflow_stage") or "flexible"),
        )
        turn = self.store.begin_round_turn(
            "room_plan",
            str(round_row["id"]),
            1,
            speaker,
            director_decision_id=str(decision["id"]),
            approved_route=(
                route_by_member[str(speaker["id"])]
                if with_provider_ledger
                else None
            ),
        )
        self.store.finish_director_attempt(
            "room_plan",
            str(round_row["id"]),
            str(director_attempt["id"]),
            str(director_attempt["attempt_token"]),
            status="RESPONDED",
            response_summary={
                "content": f"PROVIDER_RESPONSE_BODY_{secret}",
                "request_prompt": f"PROVIDER_PROMPT_{secret}",
            },
            decision_summary={
                "action": "speak",
                "member_id": str(speaker["id"]),
            },
            selected_member_id=str(speaker["id"]),
            director_decision_id=str(decision["id"]),
            turn_order=1,
        )

        speaker_provider_attempt: dict[str, Any] | None = None
        if ledger is not None:
            speaker_provider_attempt = ledger.reserve(
                kind="round_speaker",
                provider=str(route_by_member[str(speaker["id"])]["provider"]),
                model=str(route_by_member[str(speaker["id"])]["model"]),
                member_id=str(speaker["id"]),
                member_version=int(speaker["version"]),
                target_type="round_turn" if operation_binding else "",
                target_id=str(turn["id"]) if operation_binding else "",
            )
            provider_attempt_ids.append(str(speaker_provider_attempt["id"]))
            ledger.finish(
                str(speaker_provider_attempt["id"]),
                str(speaker_provider_attempt["attempt_token"]),
                status="RESPONDED",
                elapsed_ms=5,
                usage={"input_tokens": 8, "output_tokens": 3},
            )

        final_checkpoint = _checkpoint_v9(members, round_row, speaker)
        final_checkpoint.update({
            "shared_context": shared_context,
            "round_evidence_manifest": evidence_manifest,
        })
        message = self.store.add_message(
            "room_plan",
            sender_type="ai",
            sender_id=str(speaker["id"]),
            sender_name=str(speaker["name"]),
            content=f"VISIBLE_MESSAGE_BODY_{secret}",
            round_id=str(round_row["id"]),
            round_turn_id=str(turn["id"]),
            round_turn_status="RESPONDED",
            round_checkpoint_state=final_checkpoint,
            turn_contract=_valid_contract(),
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_contract_qualified=True,
            turn_contract_issues=[],
        )
        self.store.complete_round(
            str(round_row["id"]),
            "COMPLETED",
        )
        if ledger is not None:
            ledger.close(status="COMPLETED")
        completed_round = self.store.get_round(
            "room_plan",
            str(round_row["id"]),
        )
        if completed_round is None:
            raise AssertionError("completed trace fixture round disappeared")
        return completed_round, {
            "run_id": ledger.run_id if ledger is not None else "",
            "provider_attempt_ids": provider_attempt_ids,
            "director_attempt_token": str(director_attempt["attempt_token"]),
            "provider_attempt_tokens": [
                str(item["attempt_token"])
                for item in (
                    director_provider_attempt,
                    speaker_provider_attempt,
                )
                if item is not None
            ],
            "secret": secret,
            "message_id": str(message["id"]),
            "director_decision_id": str(decision["id"]),
        }

    def test_legacy_message_only_history_is_honestly_partial(self) -> None:
        round_row = self._create_legacy_round()

        trace = self.store.round_execution_trace(
            "room_plan",
            str(round_row["id"]),
            limit=200,
            cursor="",
        )

        self.assertEqual(trace["version"], TRACE_VERSION)
        self.assertEqual(trace["room_id"], "room_plan")
        self.assertEqual(trace["round_id"], round_row["id"])
        self.assertEqual(trace["history"]["mode"], "legacy_message_only")
        self.assertEqual(trace["history"]["coverage"], "partial")
        self.assertEqual(trace["integrity"]["status"], "partial")
        self.assertTrue(trace["integrity"]["ok"])
        self.assertIsNot(trace["integrity"].get("round_ledger_verified"), True)
        self.assertIsNot(trace["integrity"].get("provider_ledger_verified"), True)
        self.assertTrue(trace["history"].get("limitations"))
        self.assertEqual(trace["safety"]["provider_calls_performed"], 0)
        self.assertTrue(trace["safety"]["read_only"])

    def test_current_formal_round_without_provider_ledger_is_partial(self) -> None:
        round_row, _fixture = self._create_current_round(with_provider_ledger=False)

        trace = self.store.round_execution_trace(
            "room_plan",
            str(round_row["id"]),
        )

        self.assertEqual(trace["history"]["mode"], "current_envelope")
        self.assertEqual(trace["history"]["coverage"], "partial")
        self.assertEqual(trace["integrity"]["status"], "partial")
        self.assertTrue(trace["integrity"]["ok"])
        self.assertTrue(trace["integrity"]["round_ledger_verified"])
        self.assertIsNot(trace["integrity"].get("provider_ledger_verified"), True)
        combined_codes = _issue_codes(trace["integrity"].get("issues")) | _issue_codes(
            trace["history"].get("limitations")
        )
        self.assertTrue(
            any("PROVIDER" in code and "UNAVAILABLE" in code for code in combined_codes),
            combined_codes,
        )

    def test_current_formal_ledgers_verify_but_missing_operation_links_stay_partial(
        self,
    ) -> None:
        round_row, _fixture = self._create_current_round(with_provider_ledger=True)

        trace = self.store.round_execution_trace(
            "room_plan",
            str(round_row["id"]),
        )

        self.assertEqual(trace["history"]["mode"], "current_envelope")
        self.assertEqual(trace["history"]["coverage"], "partial")
        self.assertTrue(trace["integrity"]["round_ledger_verified"])
        self.assertTrue(trace["integrity"]["provider_ledger_verified"])
        self.assertTrue(trace["integrity"]["ok"])
        self.assertEqual(trace["integrity"]["status"], "partial")
        combined_codes = _issue_codes(trace["integrity"].get("issues")) | _issue_codes(
            trace["history"].get("limitations")
        )
        self.assertIn(CORRELATION_LIMITATION, combined_codes)
        self.assertRegex(
            str(trace["integrity"]["trace_snapshot_sha256"]),
            r"^[0-9a-f]{64}$",
        )
        self.assertFalse(trace["integrity"]["snapshot_hash_persisted"])

    def test_current_operation_ids_are_uuid4_and_bound_trace_is_persisted(
        self,
    ) -> None:
        round_row, fixture = self._create_current_round(
            with_provider_ledger=True,
            operation_binding=True,
        )

        attempts = self.store.list_provider_call_attempts(fixture["run_id"])
        self.assertEqual(len(attempts), 3)
        operation_ids = [str(attempt["operation_id"]) for attempt in attempts]
        self.assertEqual(len(set(operation_ids)), len(operation_ids))
        for operation_id in operation_ids:
            parsed = uuid.UUID(operation_id)
            self.assertEqual(parsed.version, 4)
            self.assertEqual(str(parsed), operation_id)

        trace = self.store.round_execution_trace(
            "room_plan",
            str(round_row["id"]),
        )
        self.assertNotIn(
            CORRELATION_LIMITATION,
            _issue_codes(trace["history"].get("limitations")),
        )
        self.assertTrue(trace["integrity"]["provider_ledger_verified"])
        self.assertTrue(trace["integrity"]["trace_anchor_verified"])
        self.assertTrue(trace["integrity"]["snapshot_hash_persisted"])

    def test_operation_target_type_and_cross_round_target_fail_closed(self) -> None:
        members = self.store.enabled_members("room_plan")
        for index, member in enumerate(members, start=1):
            if str(member.get("model") or "").strip():
                continue
            self.store.update_member(
                "room_plan",
                str(member["id"]),
                {
                    "provider": "deepseek",
                    "model": f"offline-binding-model-{index}",
                },
                expected_version=int(member["version"]),
            )
        members = self.store.enabled_members("room_plan")
        moderator = members[0]
        routes = [_approved_route(member) for member in members]
        route = next(
            item for item in routes if item["member_id"] == str(moderator["id"])
        )
        own_round = self.store.create_formal_round(
            "room_plan",
            "Operation binding owner round",
        )
        foreign_round = self.store.create_formal_round(
            "room_plan",
            "Operation binding foreign round",
        )
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id=f"binding-fail-closed-{self.sequence}",
            plan_hash=hashlib.sha256(b"offline-binding-plan").hexdigest(),
            max_calls=2,
            member_routes={
                "version": "provider_member_routes_v2",
                "members": sorted(routes, key=lambda item: item["member_id"]),
            },
            operation_binding_version=PROVIDER_OPERATION_BINDING_VERSION,
        )
        ledger.bind_round(str(own_round["id"]))
        foreign_attempt = self.store.begin_director_attempt(
            "room_plan",
            str(foreign_round["id"]),
            moderator_member_id=str(moderator["id"]),
            moderator_member_version=int(moderator["version"]),
            provider=str(route["provider"]),
            model=str(route["model"]),
        )

        with self.assertRaisesRegex(ValueError, "kind and target type"):
            ledger.reserve(
                kind="round_director",
                provider=str(route["provider"]),
                model=str(route["model"]),
                member_id=str(moderator["id"]),
                member_version=int(moderator["version"]),
                target_type="round_turn",
                target_id="turn_wrong_type",
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            ledger.reserve(
                kind="round_director",
                provider=str(route["provider"]),
                model=str(route["model"]),
                member_id=str(moderator["id"]),
                member_version=int(moderator["version"]),
                target_type="director_attempt",
                target_id=str(foreign_attempt["id"]),
            )

        self.assertEqual(ledger.snapshot()["reserved_calls"], 0)
        self.assertEqual(ledger.attempts(), [])

    def test_director_decision_seal_detects_payload_tampering(self) -> None:
        round_row, fixture = self._create_current_round(
            with_provider_ledger=True,
            operation_binding=True,
        )
        before = self.store.round_execution_trace(
            "room_plan",
            str(round_row["id"]),
        )
        self.assertNotIn(
            "DIRECTOR_DECISION_SEAL_MISMATCH",
            _issue_codes(before["integrity"].get("issues")),
        )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE director_decisions SET reason=? WHERE id=?",
                ("tampered offline decision reason", fixture["director_decision_id"]),
            )

        after = self.store.round_execution_trace(
            "room_plan",
            str(round_row["id"]),
        )
        self.assertEqual(after["integrity"]["status"], "invalid")
        self.assertIn(
            "DIRECTOR_DECISION_SEAL_MISMATCH",
            _issue_codes(after["integrity"].get("issues")),
        )

    def test_legacy_unsealed_director_decision_is_partial_not_invalid(self) -> None:
        round_row, fixture = self._create_current_round(
            with_provider_ledger=True,
            operation_binding=True,
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE director_decisions SET decision_sha256='' WHERE id=?",
                (fixture["director_decision_id"],),
            )

        trace = self.store.round_execution_trace(
            "room_plan",
            str(round_row["id"]),
        )
        limitation_codes = _issue_codes(trace["history"].get("limitations"))
        self.assertEqual(trace["integrity"]["status"], "partial")
        self.assertTrue(trace["integrity"]["ok"])
        self.assertIn("DIRECTOR_DECISIONS_UNSEALED", limitation_codes)
        self.assertNotIn(
            "DIRECTOR_DECISION_SEAL_MISMATCH",
            _issue_codes(trace["integrity"].get("issues")),
        )

    def test_artifact_creation_appends_verified_trace_anchor(self) -> None:
        round_row, _fixture = self._create_current_round(
            with_provider_ledger=True,
            operation_binding=True,
        )
        before = self.store.round_execution_trace(
            "room_plan",
            str(round_row["id"]),
        )
        self.assertEqual(before["integrity"]["trace_anchor_sequence"], 1)
        self.assertTrue(before["integrity"]["snapshot_hash_persisted"])

        artifact = self.store.create_artifact(
            "room_plan",
            round_id=str(round_row["id"]),
            title="Offline trace anchor artifact",
            content=_minimal_artifact_content(),
            generation_source="offline_test",
            created_by="offline_test",
        )
        self.assertIsNotNone(artifact)

        after = self.store.round_execution_trace(
            "room_plan",
            str(round_row["id"]),
        )
        self.assertEqual(after["integrity"]["trace_anchor_sequence"], 2)
        self.assertTrue(after["integrity"]["trace_anchor_verified"])
        self.assertTrue(after["integrity"]["snapshot_hash_persisted"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                """SELECT sequence_no,previous_anchor_sha256,anchor_sha256
                     FROM round_trace_anchors WHERE round_id=?
                     ORDER BY sequence_no""",
                (str(round_row["id"]),),
            ).fetchall()
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertEqual(rows[1][1], rows[0][2])

    def test_trace_anchor_snapshot_chain_and_head_tampering_fail_closed(self) -> None:
        tamper_cases: tuple[
            tuple[str, Callable[[sqlite3.Connection, str], None], str],
            ...,
        ] = (
            (
                "snapshot",
                lambda connection, round_id: connection.execute(
                    """UPDATE round_trace_anchors SET snapshot_json='{}'
                         WHERE round_id=? AND sequence_no=1""",
                    (round_id,),
                ),
                "ROUND_TRACE_ANCHOR_CHAIN_INVALID",
            ),
            (
                "chain",
                lambda connection, round_id: connection.execute(
                    """UPDATE round_trace_anchors SET previous_anchor_sha256=?
                         WHERE round_id=? AND sequence_no=2""",
                    ("0" * 64, round_id),
                ),
                "ROUND_TRACE_ANCHOR_CHAIN_INVALID",
            ),
            (
                "head",
                lambda connection, round_id: connection.execute(
                    """UPDATE rounds SET trace_anchor_head_sha256=? WHERE id=?""",
                    ("0" * 64, round_id),
                ),
                "ROUND_TRACE_ANCHOR_HEAD_MISMATCH",
            ),
        )

        for name, tamper, expected_code in tamper_cases:
            with self.subTest(tamper=name):
                round_row, _fixture = self._create_current_round(
                    with_provider_ledger=True,
                    operation_binding=True,
                )
                self.store.create_artifact(
                    "room_plan",
                    round_id=str(round_row["id"]),
                    title=f"Offline anchor tamper fixture {name}",
                    content=_minimal_artifact_content(),
                    generation_source="offline_test",
                    created_by="offline_test",
                )
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    tamper(connection, str(round_row["id"]))

                trace = self.store.round_execution_trace(
                    "room_plan",
                    str(round_row["id"]),
                )
                self.assertEqual(trace["integrity"]["status"], "invalid")
                self.assertFalse(trace["integrity"]["snapshot_hash_persisted"])
                self.assertIn(
                    expected_code,
                    _issue_codes(trace["integrity"].get("issues")),
                )

    def test_audit_trace_get_handler_is_read_only_without_opening_a_socket(
        self,
    ) -> None:
        round_row, _fixture = self._create_current_round(
            with_provider_ledger=True,
            operation_binding=True,
        )
        before = _logical_database_sha256(self.db_path)
        responses: list[tuple[dict[str, Any], Any]] = []
        handler = object.__new__(http_server.StudioRequestHandler)
        handler.path = (
            f"/api/rooms/room_plan/rounds/{round_row['id']}/audit-trace"
        )
        handler._guard_request = lambda *args, **kwargs: True
        handler._send_json = (
            lambda payload, status=None: responses.append((payload, status))
        )

        with patch.object(http_server, "STORE", self.store):
            handler.do_GET()

        after = _logical_database_sha256(self.db_path)
        self.assertEqual(before, after)
        self.assertEqual(len(responses), 1)
        payload, _status = responses[0]
        self.assertTrue(payload.get("ok"))
        safety = (payload.get("trace") or {}).get("safety") or {}
        self.assertTrue(safety.get("read_only"))
        self.assertEqual(safety.get("provider_calls_performed"), 0)

    def test_repeated_reads_are_stable_redacted_and_do_not_write_sqlite(self) -> None:
        round_row, fixture = self._create_current_round(with_provider_ledger=True)
        before = _logical_database_sha256(self.db_path)

        first = self.store.round_execution_trace(
            "room_plan",
            str(round_row["id"]),
            limit=200,
            cursor="",
        )
        middle = _logical_database_sha256(self.db_path)
        second = StudioStore(self.db_path).round_execution_trace(
            "room_plan",
            str(round_row["id"]),
            limit=200,
            cursor="",
        )
        after = _logical_database_sha256(self.db_path)

        self.assertEqual(first, second)
        self.assertEqual(before, middle)
        self.assertEqual(middle, after)
        events = first["events"]
        self.assertGreater(len(events), 3)
        self.assertEqual(
            [event["ordinal"] for event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(
            len({event["event_id"] for event in events}),
            len(events),
        )

        encoded = json.dumps(first, ensure_ascii=False, sort_keys=True).lower()
        forbidden_values = [
            fixture["secret"],
            fixture["director_attempt_token"],
            *fixture["provider_attempt_tokens"],
            f"provider_response_body_{fixture['secret']}",
            f"provider_prompt_{fixture['secret']}",
            f"visible_message_body_{fixture['secret']}",
        ]
        for forbidden in forbidden_values:
            self.assertNotIn(str(forbidden).lower(), encoded)
        for forbidden_key in (
            "attempt_token",
            "api_key",
            "authorization_header",
            "request_prompt",
            "request_body",
            "response_body",
        ):
            self.assertNotIn(f'"{forbidden_key}"', encoded)

    def test_provider_usage_hash_sequence_and_run_count_tampering_are_invalid(
        self,
    ) -> None:
        tamper_cases: tuple[
            tuple[str, Callable[[sqlite3.Connection, dict[str, Any]], None], str],
            ...,
        ] = (
            (
                "usage_json",
                lambda connection, fixture: connection.execute(
                    "UPDATE provider_call_attempts SET usage_json=? WHERE id=?",
                    ('{"input_tokens":999}', fixture["provider_attempt_ids"][0]),
                ),
                "USAGE",
            ),
            (
                "usage_sha256",
                lambda connection, fixture: connection.execute(
                    "UPDATE provider_call_attempts SET usage_sha256=? WHERE id=?",
                    ("0" * 64, fixture["provider_attempt_ids"][0]),
                ),
                "HASH",
            ),
            (
                "sequence_no",
                lambda connection, fixture: connection.execute(
                    "UPDATE provider_call_attempts SET sequence_no=9 WHERE id=?",
                    (fixture["provider_attempt_ids"][0],),
                ),
                "SEQUENCE",
            ),
            (
                "run_counts",
                lambda connection, fixture: connection.execute(
                    "UPDATE provider_execution_runs SET reserved_calls=4 WHERE id=?",
                    (fixture["run_id"],),
                ),
                "COUNT",
            ),
        )

        for name, tamper, expected_fragment in tamper_cases:
            with self.subTest(tamper=name):
                round_row, fixture = self._create_current_round(
                    with_provider_ledger=True
                )
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    tamper(connection, fixture)

                trace = StudioStore(self.db_path).round_execution_trace(
                    "room_plan",
                    str(round_row["id"]),
                )

                self.assertEqual(trace["integrity"]["status"], "invalid")
                self.assertFalse(trace["integrity"]["ok"])
                self.assertFalse(trace["integrity"]["provider_ledger_verified"])
                codes = _issue_codes(trace["integrity"].get("issues"))
                self.assertTrue(
                    any(expected_fragment in code for code in codes),
                    (expected_fragment, codes),
                )


if __name__ == "__main__":
    unittest.main()
