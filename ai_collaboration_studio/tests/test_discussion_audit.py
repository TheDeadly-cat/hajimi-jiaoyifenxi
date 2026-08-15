from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import patch


_IMPORT_TEMP_DIR = tempfile.TemporaryDirectory(prefix="discussion-audit-import-")
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
from backend.discussion_audit import (  # noqa: E402
    DISCUSSION_AUDIT_VERSION,
    DiscussionAuditConflict,
    project_discussion_audit,
)
from backend.store import StudioStore  # noqa: E402

for _key, _value in _PREVIOUS_IMPORT_ENV.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


ROOM_ID = "room_plan"
ROUND_ID = "round_discussion_audit_fixture"
TRACE_HASH = "a" * 64
SECRET_SENTINEL = "sk-proj-discussion-audit-must-not-leak-123456789"


def _event(
    event_type: str,
    number: int,
    *,
    refs: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ordinal": number,
        "event_id": f"trace_event_{number:024d}",
        "type": event_type,
        "occurred_at": number,
        "finished_at": number,
        "source": {
            "table": "fixture",
            "id": f"source_{number}",
            "sequence_no": number,
        },
        "actor": {
            "kind": "system",
            "id": "",
            "version": 0,
            "name": "",
            "provider": "",
            "model": "",
        },
        "status": "verified",
        "refs": refs or {},
        "payload": payload or {},
        "integrity": {"status": "verified", "ok": True, "issues": []},
    }


def _formal_messages() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for turn_order in (1, 2):
        prior_id = "msg_1" if turn_order == 2 else ""
        result.append({
            "id": f"msg_{turn_order}",
            "round_id": ROUND_ID,
            "round_turn_id": f"turn_{turn_order}",
            "turn_order": turn_order,
            "sender_type": "ai",
            "sender_id": f"member_{turn_order}",
            "sender_name": f"Member {turn_order}",
            "reply_to_message_id": prior_id,
            "member_version": 1,
            "turn_contract": {
                "responds_to": ([{"id": prior_id}] if prior_id else []),
                "candidate_updates": [],
            },
            "turn_contract_qualified": True,
            "turn_contract_integrity_ok": True,
            "member_snapshot_integrity_ok": True,
        })
    return result


def _source_pair(
    *,
    candidate_count: int = 2,
    fallback: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    messages = _formal_messages()
    events: list[dict[str, Any]] = []
    event_number = 1
    for message in messages:
        refs = {
            "round_turn_id": message["round_turn_id"],
            "message_id": message["id"],
        }
        events.append(_event("round_turn_reserved", event_number, refs=refs))
        event_number += 1
        events.append(_event("round_turn_terminal", event_number, refs=refs))
        event_number += 1
        events.append(_event("message_persisted", event_number, refs=refs))
        event_number += 1

    source = "director_circuit_breaker" if fallback else "ai"
    authority = "safety_fallback" if fallback else "moderator_model"
    model_used = not fallback
    events.append(_event(
        "director_decision_recorded",
        event_number,
        refs={"director_decision_id": "director_1", "round_turn_id": "turn_1"},
        payload={
            "action": "speak",
            "member_id": "member_1",
            "member_name": "Member 1",
            "reason": SECRET_SENTINEL,
            "source": source,
            "stage": "analysis",
            "moderator_context": {
                "version": "director_moderator_context_v1",
                "decision_authority": authority,
                "model_used": model_used,
                "discussion_mode": "dynamic",
                "member_id": "moderator_1",
                "member_name": "Moderator",
                "identity": SECRET_SENTINEL,
                "member_version": 1,
                "provider": "fixture-provider",
                "model": SECRET_SENTINEL,
                "scheduling_context": {
                    "version": "director_scheduling_context_v1",
                    "eligible_member_ids": ["member_1", "member_2"],
                    "gap_codes": ["fundamental_gap", "risk_gap"],
                    "candidate_contributions": [{
                        "member_id": "member_1",
                        "contribution_count": 1,
                        "gap_codes": ["fundamental_gap"],
                    }],
                    "selected_gap_codes": ["fundamental_gap"],
                    "global_remaining_calls": 20,
                    "director_remaining_calls": 4,
                    "minimum_remaining_visible_speaker_calls": 1,
                    "remaining_visible_plan_feasible": True,
                },
            },
        },
    ))

    candidates = [
        {
            "id": f"option_{index}",
            "origin_message_id": "msg_1",
            "latest_message_id": "msg_1",
            "revision": 1,
        }
        for index in range(1, candidate_count + 1)
    ]
    comparison_ready = candidate_count >= 2
    candidate_projection = {
        "qualified_message_count": len(messages),
        "source_message_ids": [message["id"] for message in messages],
        "candidate_lineage": {
            "ready": comparison_ready,
            "status": "ready" if comparison_ready else "blocked",
            "decision_message_id": "msg_2",
            "referenced_candidate_ids": [item["id"] for item in candidates],
            "candidates": candidates,
            "issues": ([] if comparison_ready else [{
                "code": "CANDIDATE_LINEAGE_COMPARISON_INSUFFICIENT",
                "message": SECRET_SENTINEL,
            }]),
        },
        "candidate_risk_reviews": {
            "required": False,
            "ready": True,
            "status": "not_required",
            "target_candidate_count": candidate_count,
            "reviewed_candidate_count": 0,
            "review_count": 0,
            "issues": [],
        },
        "decision": {
            "status": "candidate" if comparison_ready else "undecided",
            "preferred_option_id": "option_1" if comparison_ready else "",
            "rationale": SECRET_SENTINEL,
        },
    }
    trace = {
        "version": "round_execution_trace_v1",
        "trace_hash": TRACE_HASH,
        "room_id": ROOM_ID,
        "round_id": ROUND_ID,
        "history": {"mode": "current_envelope", "coverage": "full", "limitations": []},
        "integrity": {"status": "verified", "ok": True, "issues": []},
        "summary": {"event_count": len(events), "formal_turn_count": len(messages)},
        "events": events,
        "candidate_projection": candidate_projection,
        "page": {
            "limit": 500,
            "cursor": 0,
            "next_cursor": None,
            "has_more": False,
            "total": len(events),
        },
        "safety": {
            "read_only": True,
            "provider_calls_performed": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        },
    }
    bundle = {
        "applicable": True,
        "valid": True,
        "round_status": "COMPLETED",
        "turn_contract_version": "turn_contract_v1",
        "candidate_risk_review_required": False,
        "messages": messages,
        "issues": [],
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }
    return trace, bundle


def _legacy_source_pair() -> tuple[dict[str, Any], dict[str, Any]]:
    events = [_event("round_started", 1)]
    trace = {
        "version": "round_execution_trace_v1",
        "trace_hash": "b" * 64,
        "room_id": ROOM_ID,
        "round_id": ROUND_ID,
        "history": {
            "mode": "legacy_message_only",
            "coverage": "partial",
            "limitations": ["LEGACY_MESSAGE_ONLY", "PROVIDER_LEDGER_UNAVAILABLE"],
        },
        "integrity": {"status": "partial", "ok": True, "issues": []},
        "summary": {"event_count": len(events), "formal_turn_count": 0},
        "events": events,
        "candidate_projection": None,
        "page": {
            "limit": 500,
            "cursor": 0,
            "next_cursor": None,
            "has_more": False,
            "total": len(events),
        },
        "safety": {
            "read_only": True,
            "provider_calls_performed": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        },
    }
    bundle = {
        "applicable": False,
        "valid": True,
        "round_status": "COMPLETED",
        "messages": [],
        "issues": [],
    }
    return trace, bundle


def _logical_database_sha256(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        dump = "\n".join(connection.iterdump())
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()


class DiscussionAuditProjectionTests(unittest.TestCase):
    def test_dynamic_structure_and_response_edge_do_not_claim_semantic_causality(
        self,
    ) -> None:
        trace, bundle = _source_pair(candidate_count=2)

        first = project_discussion_audit(
            trace,
            bundle,
            expected_room_id=ROOM_ID,
            expected_round_id=ROUND_ID,
        )
        second = project_discussion_audit(
            copy.deepcopy(trace),
            copy.deepcopy(bundle),
            expected_room_id=ROOM_ID,
            expected_round_id=ROUND_ID,
        )

        self.assertEqual(first, second)
        self.assertEqual(first["version"], DISCUSSION_AUDIT_VERSION)
        self.assertRegex(first["audit_hash"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(first["audit_hash"], TRACE_HASH)
        self.assertEqual(first["structural"]["dynamic_status"], "verified")
        self.assertEqual(first["structural"]["dynamic_selection_count"], 1)
        self.assertEqual(first["structural"]["response_edge_count"], 1)
        edge = first["structural"]["response_edges"][0]
        self.assertTrue(edge["structurally_verified"])
        self.assertEqual(edge["semantic_causality_status"], "unknown")
        self.assertEqual(first["semantic_causality"]["status"], "unknown")
        self.assertFalse(first["semantic_causality"]["proven"])
        self.assertNotIn(SECRET_SENTINEL, json.dumps(first, ensure_ascii=False))

    def test_candidate_checkpoint_distinguishes_one_from_two_candidates(self) -> None:
        one_trace, one_bundle = _source_pair(candidate_count=1)
        one = project_discussion_audit(
            one_trace,
            one_bundle,
            expected_room_id=ROOM_ID,
            expected_round_id=ROUND_ID,
        )
        self.assertEqual(one["candidate_checkpoint"]["candidate_count"], 1)
        self.assertFalse(one["candidate_checkpoint"]["ready"])
        self.assertIn(
            "CANDIDATE_GENERATION_INSUFFICIENT",
            {item["code"] for item in one["findings"]},
        )

        two_trace, two_bundle = _source_pair(candidate_count=2)
        two = project_discussion_audit(
            two_trace,
            two_bundle,
            expected_room_id=ROOM_ID,
            expected_round_id=ROUND_ID,
        )
        self.assertEqual(two["candidate_checkpoint"]["candidate_count"], 2)
        self.assertTrue(two["candidate_checkpoint"]["ready"])
        self.assertNotIn(
            "CANDIDATE_GENERATION_INSUFFICIENT",
            {item["code"] for item in two["findings"]},
        )

    def test_fallback_is_explicit_without_becoming_a_semantic_claim(self) -> None:
        trace, bundle = _source_pair(candidate_count=2, fallback=True)
        audit = project_discussion_audit(
            trace,
            bundle,
            expected_room_id=ROOM_ID,
            expected_round_id=ROUND_ID,
        )

        self.assertEqual(audit["structural"]["fallback_count"], 1)
        self.assertTrue(audit["structural"]["selections"][0]["fallback"])
        self.assertIn("FALLBACK_USED", {item["code"] for item in audit["findings"]})
        self.assertEqual(audit["semantic_causality"]["status"], "unknown")

    def test_tampered_or_divergent_sources_fail_closed(self) -> None:
        cases: list[tuple[str, dict[str, Any], dict[str, Any], str]] = []

        trace, bundle = _source_pair()
        trace["integrity"] = {"status": "invalid", "ok": False, "issues": []}
        cases.append(("trace", trace, bundle, "TRACE_INTEGRITY_INVALID"))

        trace, bundle = _source_pair()
        bundle["valid"] = False
        cases.append(("bundle", trace, bundle, "TURN_CONTRACT_BUNDLE_INVALID"))

        trace, bundle = _source_pair()
        trace["candidate_projection"]["source_message_ids"] = ["msg_other"]
        cases.append(("diverged", trace, bundle, "TRACE_BUNDLE_DIVERGED"))

        trace, bundle = _source_pair()
        trace["page"]["has_more"] = True
        cases.append(("truncated", trace, bundle, "TRACE_PAGE_INCOMPLETE"))

        for name, case_trace, case_bundle, expected_code in cases:
            with self.subTest(name=name), self.assertRaises(DiscussionAuditConflict) as caught:
                project_discussion_audit(
                    case_trace,
                    case_bundle,
                    expected_room_id=ROOM_ID,
                    expected_round_id=ROUND_ID,
                )
            self.assertEqual(caught.exception.code, expected_code)

    def test_legacy_is_partial_and_scope_mismatch_fails_closed(self) -> None:
        trace, bundle = _legacy_source_pair()
        audit = project_discussion_audit(
            trace,
            bundle,
            expected_room_id=ROOM_ID,
            expected_round_id=ROUND_ID,
        )
        self.assertEqual(audit["coverage"]["history_mode"], "legacy_message_only")
        self.assertEqual(audit["structural"]["dynamic_status"], "legacy_unknown")
        self.assertFalse(audit["candidate_checkpoint"]["applicable"])
        self.assertIn(
            "LEGACY_HISTORY_PARTIAL",
            {item["code"] for item in audit["findings"]},
        )

        with self.assertRaises(DiscussionAuditConflict) as caught:
            project_discussion_audit(
                trace,
                bundle,
                expected_room_id="room_other",
                expected_round_id=ROUND_ID,
            )
        self.assertEqual(caught.exception.code, "TRACE_SCOPE_MISMATCH")


class DiscussionAuditHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="discussion-audit-http-")
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "audit.sqlite3"
        self.store = StudioStore(self.db_path)

    def _legacy_round(self) -> dict[str, Any]:
        member = self.store.enabled_members(ROOM_ID)[0]
        round_row = self.store.create_round(ROOM_ID, "Legacy discussion audit")
        self.store.add_message(
            ROOM_ID,
            sender_type="ai",
            sender_id=str(member["id"]),
            sender_name=str(member["name"]),
            content="Offline legacy message.",
            round_id=str(round_row["id"]),
        )
        self.store.complete_round(str(round_row["id"]), "COMPLETED")
        return self.store.get_round(ROOM_ID, str(round_row["id"])) or round_row

    def _request(self, path: str) -> tuple[dict[str, Any], Any]:
        responses: list[tuple[dict[str, Any], Any]] = []
        handler = object.__new__(http_server.StudioRequestHandler)
        handler.path = path
        handler._guard_request = lambda *args, **kwargs: True
        handler._send_json = (
            lambda payload, status=None: responses.append((payload, status))
        )
        with patch.object(http_server, "STORE", self.store):
            handler.do_GET()
        self.assertEqual(len(responses), 1)
        return responses[0]

    def test_get_handler_uses_both_verified_reads_and_does_not_write(self) -> None:
        round_row = self._legacy_round()
        round_id = str(round_row["id"])
        before = _logical_database_sha256(self.db_path)
        trace_before = StudioStore(self.db_path).round_execution_trace(
            ROOM_ID,
            round_id,
            limit=500,
            cursor="",
        )

        with (
            patch.object(
                self.store,
                "round_execution_trace",
                wraps=self.store.round_execution_trace,
            ) as trace_read,
            patch.object(
                self.store,
                "round_turn_contract_bundle",
                wraps=self.store.round_turn_contract_bundle,
            ) as bundle_read,
        ):
            payload, status = self._request(
                f"/api/rooms/{ROOM_ID}/rounds/{round_id}/discussion-audit"
            )
        self.assertEqual(trace_read.call_count, 1)
        self.assertEqual(bundle_read.call_count, 1)

        after = _logical_database_sha256(self.db_path)
        trace_after = StudioStore(self.db_path).round_execution_trace(
            ROOM_ID,
            round_id,
            limit=500,
            cursor="",
        )
        self.assertEqual(before, after)
        self.assertEqual(trace_before["trace_hash"], trace_after["trace_hash"])
        self.assertIsNone(status)
        self.assertTrue(payload["ok"])
        audit = payload["discussion_audit"]
        self.assertEqual(audit["version"], DISCUSSION_AUDIT_VERSION)
        self.assertTrue(audit["source"]["turn_contract_valid"])
        self.assertFalse(audit["source"]["turn_contract_applicable"])
        self.assertTrue(audit["safety"]["read_only"])
        self.assertEqual(audit["safety"]["database_writes_performed"], 0)
        self.assertEqual(audit["safety"]["provider_calls_performed"], 0)
        self.assertEqual(audit["safety"]["market_data_calls_performed"], 0)

    def test_get_handler_scopes_room_and_round(self) -> None:
        round_row = self._legacy_round()

        payload, status = self._request(
            f"/api/rooms/room_other/rounds/{round_row['id']}/discussion-audit"
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(status, HTTPStatus.NOT_FOUND)

    def test_get_handler_maps_tampered_trace_to_redacted_conflict(self) -> None:
        trace, bundle = _source_pair()
        trace["integrity"] = {
            "status": "invalid",
            "ok": False,
            "issues": [{"code": "TAMPERED", "detail": SECRET_SENTINEL}],
        }
        responses: list[tuple[dict[str, Any], Any]] = []
        handler = object.__new__(http_server.StudioRequestHandler)
        handler.path = (
            f"/api/rooms/{ROOM_ID}/rounds/{ROUND_ID}/discussion-audit"
        )
        handler._guard_request = lambda *args, **kwargs: True
        handler._send_json = (
            lambda payload, status=None: responses.append((payload, status))
        )

        with (
            patch.object(self.store, "round_execution_trace", return_value=trace),
            patch.object(self.store, "round_turn_contract_bundle", return_value=bundle),
            patch.object(http_server, "STORE", self.store),
        ):
            handler.do_GET()

        self.assertEqual(len(responses), 1)
        payload, status = responses[0]
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(payload["error_code"], "DISCUSSION_AUDIT_CONFLICT")
        self.assertEqual(payload["conflict_code"], "TRACE_INTEGRITY_INVALID")
        self.assertNotIn(SECRET_SENTINEL, json.dumps(payload, ensure_ascii=False))

    def test_get_handler_maps_bundle_value_error_to_redacted_conflict(self) -> None:
        trace, _bundle = _source_pair()
        responses: list[tuple[dict[str, Any], Any]] = []
        handler = object.__new__(http_server.StudioRequestHandler)
        handler.path = (
            f"/api/rooms/{ROOM_ID}/rounds/{ROUND_ID}/discussion-audit"
        )
        handler._guard_request = lambda *args, **kwargs: True
        handler._send_json = (
            lambda payload, status=None: responses.append((payload, status))
        )

        with (
            patch.object(self.store, "round_execution_trace", return_value=trace),
            patch.object(
                self.store,
                "round_turn_contract_bundle",
                side_effect=ValueError(SECRET_SENTINEL),
            ),
            patch.object(http_server, "STORE", self.store),
        ):
            handler.do_GET()

        self.assertEqual(len(responses), 1)
        payload, status = responses[0]
        self.assertEqual(status, HTTPStatus.CONFLICT)
        self.assertEqual(payload["error_code"], "DISCUSSION_AUDIT_CONFLICT")
        self.assertEqual(
            payload["conflict_code"],
            "TURN_CONTRACT_BUNDLE_CONFLICT",
        )
        self.assertNotIn(SECRET_SENTINEL, json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
