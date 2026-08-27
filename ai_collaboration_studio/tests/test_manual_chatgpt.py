from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.manual_chatgpt import (
    LEGACY_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION,
    MANUAL_CHATGPT_API_REVIEW_VERSION,
    MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
    MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION,
    MANUAL_CHATGPT_RESULT_VERSION,
    MANUAL_CHATGPT_REVIEW_ORPHAN_AGE_MS,
    MANUAL_CHATGPT_REVIEW_RECOVERY_ACKNOWLEDGEMENT,
    ManualChatGPTError,
    ManualChatGPTService,
    estimate_text_tokens,
    import_contract,
    parse_single_json_object,
    task_prompt,
    validate_import_result,
)
from backend import http_server
from backend.decision_lineage import canonical_sha256
from backend.providers.base import ProviderResponse
from backend.providers.registry import ProviderRegistry
from backend.store import StudioStore


def valid_result(session: dict[str, object]) -> dict[str, object]:
    template = copy.deepcopy(session["import_contract"]["result_template"])
    template["declared_model"] = "user-declared-chatgpt-model"
    for panel in template["panels"]:
        panel["summary"] = f"{panel['panel_kind']} summary"
        panel["conclusion"] = f"{panel['panel_kind']} conclusion"
        panel["disagreements"] = ["One bounded disagreement."]
        panel["risks"] = ["One bounded risk."]
        for role in panel["role_views"]:
            role["assessment"] = f"Assessment from {role['role_id']} perspective."
            role["uncertainty"] = "Evidence remains bounded."
    final = template["final_synthesis"]
    final["summary"] = "Final synthesis for user review."
    final["decision_options"][0]["title"] = "Proceed with the bounded option"
    final["decision_options"][0]["rationale"] = "It preserves the frozen evidence boundary."
    final["recommended_option_id"] = "option_1"
    final["open_questions"] = ["Independent API review is still required."]
    return template


class FakeReviewProvider:
    provider_id = "fake-review"

    def __init__(self, *, invalid_at: int = 0, blocking_kind: str = "") -> None:
        self.invalid_at = invalid_at
        self.blocking_kind = blocking_kind
        self.calls: list[dict[str, str]] = []

    def status(self) -> dict[str, object]:
        return {
            "id": self.provider_id,
            "name": "Fake Review",
            "configured": True,
            "model": "fake-review-v1",
        }

    def probe(self, *, model: str = "") -> object:
        raise AssertionError("API review must not spend extra probe calls")

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        request = json.loads(input_text)
        self.calls.append({
            "review_kind": request["review_kind"],
            "model": model,
            "instructions": instructions,
        })
        if self.invalid_at == len(self.calls):
            return ProviderResponse(
                ok=True,
                content='{"invalid":true}',
                provider=self.provider_id,
                model=model,
            )
        blocking = request["review_kind"] == self.blocking_kind
        review = {
            "version": MANUAL_CHATGPT_API_REVIEW_VERSION,
            "review_kind": request["review_kind"],
            "verdict": "block" if blocking else "pass",
            "summary": f"Independent {request['review_kind']} completed.",
            "findings": ([{
                "severity": "blocking",
                "claim": "A blocking test finding.",
                "rationale": "The test requires a user-visible block.",
                "evidence_refs": [],
            }] if blocking else []),
            "open_questions": [],
        }
        return ProviderResponse(
            ok=True,
            content=json.dumps(review),
            provider=self.provider_id,
            model=model,
            usage={"input_tokens": 100, "output_tokens": 20},
        )


class ContextMutatingReviewProvider(FakeReviewProvider):
    def __init__(self, mutation: object, *, mutate_at_call: int) -> None:
        super().__init__()
        self.mutation = mutation
        self.mutate_at_call = mutate_at_call

    def generate(self, *, instructions: str, input_text: str, model: str = "") -> ProviderResponse:
        response = super().generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
        )
        if len(self.calls) == self.mutate_at_call:
            assert callable(self.mutation)
            self.mutation()
        return response


class ManualChatGPTContractTests(unittest.TestCase):
    def test_parser_tolerates_one_fence_but_rejects_multiple_objects(self) -> None:
        parsed = parse_single_json_object('```json\n{"value": 1}\n```')
        self.assertEqual(parsed, {"value": 1})
        with self.assertRaises(ManualChatGPTError) as caught:
            parse_single_json_object('{"value": 1}\n{"value": 2}')
        self.assertEqual(caught.exception.code, "MANUAL_CHATGPT_IMPORT_JSON_INVALID")
        self.assertEqual(caught.exception.issues[0].path, "$[line=2,column=1]")

    def test_parser_rejects_duplicate_keys_at_exact_nested_paths(self) -> None:
        cases = (
            ('{"version":1,"version":2}', "$.version"),
            ('{"panels":[{"summary":"one","summary":"two"}]}', "$.panels[0].summary"),
            ('{"outer":{"dotted.key":1,"dotted.key":2}}', '$.outer["dotted.key"]'),
        )
        for raw, expected_path in cases:
            with self.subTest(expected_path=expected_path):
                with self.assertRaises(ManualChatGPTError) as caught:
                    parse_single_json_object(raw)
                self.assertEqual(
                    caught.exception.code,
                    "MANUAL_CHATGPT_IMPORT_DUPLICATE_KEY",
                )
                self.assertEqual(caught.exception.issues[0].code, "DUPLICATE_KEY")
                self.assertEqual(caught.exception.issues[0].path, expected_path)

    def test_parser_rejects_nonstandard_nonfinite_numbers(self) -> None:
        for raw in ('{"value":NaN}', '{"value":Infinity}', '{"value":-Infinity}'):
            with self.subTest(raw=raw):
                with self.assertRaises(ManualChatGPTError) as caught:
                    parse_single_json_object(raw)
                self.assertEqual(
                    caught.exception.code,
                    "MANUAL_CHATGPT_IMPORT_NONFINITE_NUMBER",
                )
                self.assertEqual(caught.exception.issues[0].code, "NONFINITE_NUMBER")

    def test_validator_reports_exact_paths_without_filling_conclusions(self) -> None:
        bundle = {
            "room_id": "room_one",
            "round_id": "round_one",
            "bundle_sha256": "a" * 64,
            "context_sha256": "b" * 64,
            "budget": {"panel_kinds": ["synthesis"]},
            "context": {
                "roles": [{"role_id": "role_one"}],
                "evidence_index": [{"evidence_id": "evidence_one"}],
            },
        }
        malformed = {
            "version": MANUAL_CHATGPT_RESULT_VERSION,
            "room_id": "room_one",
            "round_id": "round_one",
            "bundle_sha256": "a" * 64,
            "context_sha256": "b" * 64,
            "declared_model": "claimed-model",
            "panels": [{
                "panel_id": "panel_1",
                "panel_kind": "synthesis",
                "call_index": 1,
                "declared_independence": "same_answer_multi_role_views",
                "summary": "summary",
                "conclusion": "",
                "disagreements": [],
                "risks": [],
                "evidence_refs": ["missing-evidence"],
                "role_views": [{
                    "role_id": "role_one",
                    "assessment": "assessment",
                    "evidence_refs": [],
                    "uncertainty": "uncertainty",
                }],
            }],
            "final_synthesis": {
                "summary": "summary",
                "decision_options": [{
                    "option_id": "option_1",
                    "title": "title",
                    "rationale": "rationale",
                    "evidence_refs": [],
                    "risks": [],
                }],
                "recommended_option_id": "option_1",
                "open_questions": [],
                "evidence_refs": [],
            },
        }
        normalized, issues = validate_import_result(malformed, bundle)
        self.assertIsNone(normalized)
        issue_paths = {issue.path for issue in issues}
        self.assertIn("$.panels[0].conclusion", issue_paths)
        self.assertIn("$.panels[0].evidence_refs[0]", issue_paths)


class ManualChatGPTServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-manual-chatgpt-",
            ignore_cleanup_errors=True,
        )
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        created = self.store.create_room(
            "Manual ChatGPT room",
            "Keep the collaboration local and research-only.",
        )
        self.room_id = created["room"]["id"]
        self.store.add_material(self.room_id, {
            "title": "Frozen local evidence",
            "kind": "note",
            "content": "Evidence content for a deterministic bundle.",
        })
        self.service = ManualChatGPTService(self.store, review_rate_card={})

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def imported_session(self, mode: str = "standard") -> dict[str, object]:
        session = self.service.create(
            self.room_id,
            objective=f"Exercise the {mode} independent review path.",
            mode=mode,
        )
        waiting = self.service.dispatch(self.room_id, session["id"])
        return self.service.import_result(
            self.room_id,
            session["id"],
            json.dumps(valid_result(waiting), ensure_ascii=False),
        )

    def test_create_dispatch_and_valid_import_reach_api_review(self) -> None:
        session = self.service.create(
            self.room_id,
            objective="Evaluate the bounded research question.",
            mode="standard",
        )
        self.assertEqual(session["state"], "BUNDLE_READY")
        self.assertTrue(session["integrity"]["ok"])
        self.assertEqual(session["bundle"]["budget"]["chatgpt_panel_calls"], 2)
        self.assertEqual(session["bundle"]["budget"]["independent_api_reviews"], 3)
        planning = session["bundle"]["planning"]
        encoded_context = json.dumps(
            session["bundle"]["context"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.assertEqual(planning["version"], "manual_chatgpt_planning_v1")
        self.assertEqual(
            planning["context_size"]["characters"],
            len(encoded_context),
        )
        self.assertEqual(
            planning["context_size"]["utf8_bytes"],
            len(encoded_context.encode("utf-8")),
        )
        self.assertEqual(
            planning["context_size"]["estimated_tokens"],
            estimate_text_tokens(encoded_context),
        )
        self.assertEqual(planning["estimated_api_cost"]["status"], "unavailable")
        self.assertIsNone(planning["estimated_api_cost"]["amount_usd"])
        self.assertFalse(
            planning["estimated_api_cost"]["manual_chatgpt_cost_included"]
        )
        self.assertNotIn(str(self.db_path), session["task_prompt"])
        self.assertNotIn("provider", json.dumps(session["bundle"]).lower())

        waiting = self.service.dispatch(self.room_id, session["id"])
        self.assertEqual(waiting["state"], "WAITING_FOR_CHATGPT")
        result = valid_result(waiting)
        imported = self.service.import_result(
            self.room_id,
            waiting["id"],
            f"```json\n{json.dumps(result, ensure_ascii=False)}\n```",
        )
        self.assertEqual(imported["state"], "API_REVIEW")
        self.assertTrue(imported["integrity"]["ok"])
        self.assertFalse(imported["declared_model_trusted"])
        self.assertFalse(imported["result"]["declared_model_trusted"])
        self.assertFalse(imported["result"]["role_views_are_independent_opinions"])
        self.assertEqual(
            [event["to_state"] for event in imported["events"]],
            [
                "DRAFT",
                "BUNDLE_READY",
                "WAITING_FOR_CHATGPT",
                "RESULT_IMPORTED",
                "VALIDATING",
                "API_REVIEW",
            ],
        )
        self.assertTrue(imported["next_step"]["actionable"])
        self.assertEqual(imported["next_step"]["id"], "run_api_review")
        self.assertEqual(imported["safety"]["provider_calls_performed"], 0)

    def test_session_list_and_explicit_zero_call_orphan_recovery(self) -> None:
        imported = self.imported_session("standard")
        plan = {
            "version": "manual_chatgpt_review_plan_v1",
            "session_id": imported["id"],
            "room_id": self.room_id,
            "result_sha256": imported["result_sha256"],
            "provider": "fake-review",
            "model": "fake-review-v1",
            "expected_calls": 3,
            "reviews": [],
        }
        plan_sha256 = canonical_sha256(plan)
        ledger = self.store.create_provider_execution_run(
            self.room_id,
            scope="manual_chatgpt_review",
            client_request_id="orphaned-review-zero-call",
            plan_hash=plan_sha256,
            max_calls=3,
        )
        stale_at = int(time.time() * 1000) - MANUAL_CHATGPT_REVIEW_ORPHAN_AGE_MS - 1
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """INSERT INTO manual_chatgpt_review_runs(
                       id,session_id,room_id,provider_execution_run_id,
                       client_request_id,provider,requested_model,mode,status,
                       expected_calls,completed_calls,plan_json,plan_sha256,
                       error_code,created_at,updated_at,completed_at
                   ) VALUES(?,?,?,?,?,?,?,?, 'RUNNING',3,0,?,?, '',?,?,0)""",
                (
                    "mcgrv_orphan_zero_call",
                    imported["id"],
                    self.room_id,
                    ledger["id"],
                    "orphaned-review-zero-call",
                    "fake-review",
                    "fake-review-v1",
                    "standard",
                    json.dumps(plan, sort_keys=True, separators=(",", ":")),
                    plan_sha256,
                    stale_at,
                    stale_at,
                ),
            )

        listed = self.service.list(self.room_id)
        self.assertEqual(listed[0]["id"], imported["id"])
        self.assertTrue(listed[0]["review_recovery"]["eligible"])
        self.assertEqual(
            listed[0]["review_recovery"]["reason_code"],
            "ORPHANED_ZERO_CALL_REVIEW",
        )

        recovered = self.service.recover_api_review(
            self.room_id,
            imported["id"],
            expected_result_sha256=imported["result_sha256"],
            acknowledgement=MANUAL_CHATGPT_REVIEW_RECOVERY_ACKNOWLEDGEMENT,
        )
        self.assertEqual(recovered["state"], "API_REVIEW")
        self.assertTrue(recovered["integrity"]["ok"])
        self.assertEqual(recovered["api_review"]["status"], "NOT_STARTED")
        self.assertEqual(recovered["review_recovery"]["recovery_count"], 1)
        self.assertFalse(recovered["review_recovery"]["eligible"])
        self.assertEqual(
            recovered["events"][-1]["event_type"],
            "api_review_reauthorized",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM manual_chatgpt_review_runs WHERE session_id=?",
                    (imported["id"],),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM provider_execution_runs WHERE id=?",
                    (ledger["id"],),
                ).fetchone()[0],
                "ABANDONED",
            )

    def test_standard_mode_carries_all_twelve_roles_through_two_panels_and_three_reviews(self) -> None:
        storage_room = self.store.create_room(
            "Twelve-role Manual ChatGPT room",
            "Prove that formal room roles remain analysis views, not provider calls.",
            template_id="us_storage_committee",
        )
        room_id = storage_room["room"]["id"]
        enabled_members = [
            member
            for member in storage_room["members"]
            if member["enabled"] is True and not member["archived"]
        ]
        self.assertEqual(len(enabled_members), 12)

        session = self.service.create(
            room_id,
            objective="Evaluate one bounded storage research decision.",
            mode="standard",
        )
        self.assertEqual(session["bundle"]["budget"]["chatgpt_panel_calls"], 2)
        self.assertEqual(session["bundle"]["budget"]["independent_api_reviews"], 3)
        role_ids = [
            role["role_id"] for role in session["bundle"]["context"]["roles"]
        ]
        self.assertEqual(len(role_ids), 12)
        self.assertEqual(len(set(role_ids)), 12)
        template_panels = session["import_contract"]["result_template"]["panels"]
        self.assertEqual(len(template_panels), 2)
        for panel in template_panels:
            self.assertEqual(
                [view["role_id"] for view in panel["role_views"]],
                role_ids,
            )

        waiting = self.service.dispatch(room_id, session["id"])
        imported = self.service.import_result(
            room_id,
            session["id"],
            json.dumps(valid_result(waiting), ensure_ascii=False),
        )
        self.assertEqual(imported["state"], "API_REVIEW")
        self.assertFalse(imported["result"]["role_views_are_independent_opinions"])
        for panel in imported["result"]["panels"]:
            self.assertEqual(
                [view["role_id"] for view in panel["role_views"]],
                role_ids,
            )

        provider = FakeReviewProvider()
        review_service = ManualChatGPTService(
            self.store,
            review_rate_card={},
            providers=ProviderRegistry({provider.provider_id: provider}),
        )
        reviewed = review_service.run_api_review(
            room_id,
            session["id"],
            provider_id=provider.provider_id,
            model="fake-review-v1",
            client_request_id="review-twelve-role-standard",
            expected_result_sha256=imported["result_sha256"],
        )
        self.assertEqual(reviewed["state"], "READY_FOR_DECISION")
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(reviewed["api_review"]["completed_calls"], 3)
        self.assertTrue(reviewed["api_review"]["all_calls_are_distinct"])

        frozen = review_service.freeze_decision(
            room_id,
            session["id"],
            expected_result_sha256=reviewed["result_sha256"],
            decision_card_sha256=reviewed["decision_card_sha256"],
            selected_option_id="option_1",
            acknowledgement=MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
        )
        self.assertEqual(frozen["state"], "FROZEN")
        self.assertEqual(len(provider.calls), 3)

    def test_chatgpt_panel_budget_is_an_explicit_multi_turn_single_import_protocol(self) -> None:
        for mode, expected_panels in (("quick", 1), ("standard", 2), ("deep", 3)):
            with self.subTest(mode=mode):
                session = self.service.create(
                    self.room_id,
                    objective=f"Exercise the {mode} ChatGPT turn protocol.",
                    mode=mode,
                )
                prompt = session["task_prompt"]
                self.assertIn(
                    f"恰好 {expected_panels} 次分别发送的回复",
                    prompt,
                )
                self.assertIn("不要在第一条回复中一次性生成全部 Panel", prompt)
                self.assertIn(
                    f"第 {expected_panels}/{expected_panels} 次回复",
                    prompt,
                )
                self.assertIn("本次回复只输出该 JSON 对象", prompt)
                self.assertIn("导入本身不能证明真实模型来源或调用独立性", prompt)
                self.assertEqual(
                    prompt.count("不要输出最终 JSON"),
                    expected_panels - 1,
                )
                expected_declaration = (
                    "same_model_independent_call"
                    if expected_panels > 1
                    else "same_answer_multi_role_views"
                )
                self.assertEqual(
                    {
                        panel["declared_independence"]
                        for panel in session["import_contract"]["result_template"]["panels"]
                    },
                    {expected_declaration},
                )
                self.assertEqual(
                    session["import_contract"]["version"],
                    MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION,
                )
                self.assertTrue(session["import_contract"]["duplicate_keys_rejected"])
                self.assertTrue(session["import_contract"]["nonfinite_numbers_rejected"])

    def test_legacy_import_contract_keeps_its_original_single_answer_semantics(self) -> None:
        current = self.service.create(
            self.room_id,
            objective="Verify legacy contract compatibility.",
            mode="standard",
        )
        legacy_bundle = copy.deepcopy(current["bundle"])
        legacy_bundle["import_contract_version"] = (
            LEGACY_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION
        )
        legacy_contract = import_contract(legacy_bundle)
        self.assertEqual(
            legacy_contract["version"],
            LEGACY_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION,
        )
        self.assertEqual(
            {
                panel["declared_independence"]
                for panel in legacy_contract["result_template"]["panels"]
            },
            {"same_answer_multi_role_views"},
        )
        legacy_prompt = task_prompt(legacy_bundle)
        self.assertIn("旧版 v1 导入契约不声明多回合 ChatGPT 协议", legacy_prompt)
        self.assertNotIn("恰好 2 次分别发送的回复", legacy_prompt)

    def test_review_modes_spend_exact_distinct_calls_then_user_freezes(self) -> None:
        for mode, expected_calls in (("quick", 2), ("standard", 3), ("deep", 4)):
            with self.subTest(mode=mode):
                imported = self.imported_session(mode)
                provider = FakeReviewProvider()
                service = ManualChatGPTService(
                    self.store,
                    review_rate_card={},
                    providers=ProviderRegistry({provider.provider_id: provider}),
                )
                reviewed = service.run_api_review(
                    self.room_id,
                    imported["id"],
                    provider_id=provider.provider_id,
                    model="fake-review-v1",
                    client_request_id=f"review-{mode}",
                    expected_result_sha256=imported["result_sha256"],
                )
                self.assertEqual(reviewed["state"], "READY_FOR_DECISION")
                self.assertEqual(len(provider.calls), expected_calls)
                self.assertEqual(
                    reviewed["safety"]["provider_calls_performed"],
                    expected_calls,
                )
                self.assertEqual(
                    reviewed["api_review"]["completed_calls"],
                    expected_calls,
                )
                self.assertTrue(reviewed["api_review"]["all_calls_are_distinct"])
                self.assertTrue(reviewed["decision_card"]["ready_for_user_decision"])
                self.assertFalse(reviewed["confirmation"])
                replayed_review = service.run_api_review(
                    self.room_id,
                    imported["id"],
                    provider_id=provider.provider_id,
                    model="fake-review-v1",
                    client_request_id=f"review-{mode}",
                    expected_result_sha256=imported["result_sha256"],
                )
                self.assertEqual(replayed_review["state"], "READY_FOR_DECISION")
                self.assertEqual(len(provider.calls), expected_calls)

                frozen = service.freeze_decision(
                    self.room_id,
                    imported["id"],
                    expected_result_sha256=reviewed["result_sha256"],
                    decision_card_sha256=reviewed["decision_card_sha256"],
                    selected_option_id="option_1",
                    acknowledgement=MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
                )
                self.assertEqual(frozen["state"], "FROZEN")
                self.assertEqual(
                    frozen["confirmation"]["selected_option_id"],
                    "option_1",
                )
                self.assertEqual(len(provider.calls), expected_calls)
                replayed_freeze = service.freeze_decision(
                    self.room_id,
                    imported["id"],
                    expected_result_sha256=reviewed["result_sha256"],
                    decision_card_sha256=reviewed["decision_card_sha256"],
                    selected_option_id="option_1",
                    acknowledgement=MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
                )
                self.assertEqual(replayed_freeze["state"], "FROZEN")
                self.assertEqual(len(provider.calls), expected_calls)

    def test_invalid_independent_review_fails_closed_without_refund(self) -> None:
        imported = self.imported_session("standard")
        provider = FakeReviewProvider(invalid_at=2)
        service = ManualChatGPTService(
            self.store,
            review_rate_card={},
            providers=ProviderRegistry({provider.provider_id: provider}),
        )
        failed = service.run_api_review(
            self.room_id,
            imported["id"],
            provider_id=provider.provider_id,
            model="fake-review-v1",
            client_request_id="review-invalid",
            expected_result_sha256=imported["result_sha256"],
        )
        self.assertEqual(failed["state"], "NEEDS_USER_ACTION")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(failed["safety"]["provider_calls_performed"], 2)
        self.assertEqual(failed["decision_card"], {})
        self.assertEqual(failed["validation_issues"][0]["path"], "$.api_review")

    def test_blocking_review_never_becomes_ready_or_freezable(self) -> None:
        imported = self.imported_session("quick")
        provider = FakeReviewProvider(blocking_kind="risk_review")
        service = ManualChatGPTService(
            self.store,
            review_rate_card={},
            providers=ProviderRegistry({provider.provider_id: provider}),
        )
        blocked = service.run_api_review(
            self.room_id,
            imported["id"],
            provider_id=provider.provider_id,
            model="fake-review-v1",
            client_request_id="review-blocked",
            expected_result_sha256=imported["result_sha256"],
        )
        self.assertEqual(blocked["state"], "NEEDS_USER_ACTION")
        self.assertFalse(blocked["decision_card"]["ready_for_user_decision"])
        self.assertEqual(len(blocked["decision_card"]["blocking_findings"]), 1)
        with self.assertRaises(ManualChatGPTError) as caught:
            service.freeze_decision(
                self.room_id,
                imported["id"],
                expected_result_sha256=blocked["result_sha256"],
                decision_card_sha256=blocked["decision_card_sha256"],
                selected_option_id="option_1",
                acknowledgement=MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
            )
        self.assertEqual(caught.exception.code, "MANUAL_CHATGPT_STATE_CONFLICT")

    def test_freeze_requires_exact_decision_card_hash(self) -> None:
        imported = self.imported_session("quick")
        provider = FakeReviewProvider()
        service = ManualChatGPTService(
            self.store,
            review_rate_card={},
            providers=ProviderRegistry({provider.provider_id: provider}),
        )
        reviewed = service.run_api_review(
            self.room_id,
            imported["id"],
            provider_id=provider.provider_id,
            model="fake-review-v1",
            client_request_id="review-hash-gate",
            expected_result_sha256=imported["result_sha256"],
        )
        with self.assertRaises(ManualChatGPTError) as caught:
            service.freeze_decision(
                self.room_id,
                imported["id"],
                expected_result_sha256=reviewed["result_sha256"],
                decision_card_sha256="0" * 64,
                selected_option_id="option_1",
                acknowledgement=MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
            )
        self.assertEqual(caught.exception.code, "MANUAL_CHATGPT_FREEZE_INPUT_STALE")

    def test_review_record_metadata_tamper_hides_the_decision(self) -> None:
        imported = self.imported_session("quick")
        provider = FakeReviewProvider()
        service = ManualChatGPTService(
            self.store,
            review_rate_card={},
            providers=ProviderRegistry({provider.provider_id: provider}),
        )
        reviewed = service.run_api_review(
            self.room_id,
            imported["id"],
            provider_id=provider.provider_id,
            model="fake-review-v1",
            client_request_id="review-tamper",
            expected_result_sha256=imported["result_sha256"],
        )
        self.assertEqual(reviewed["state"], "READY_FOR_DECISION")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE manual_chatgpt_api_reviews
                      SET response_model='tampered-model'
                    WHERE session_id=? AND review_index=1""",
                (imported["id"],),
            )
        hidden = service.get(self.room_id, imported["id"])
        self.assertFalse(hidden["integrity"]["ok"])
        self.assertFalse(hidden["integrity"]["api_reviews_ok"])
        self.assertEqual(hidden["decision_card"], {})
        self.assertEqual(hidden["state"], "IMPORT_REJECTED")

    def test_existing_schema_without_review_tables_is_readable_but_never_auto_migrated(self) -> None:
        imported = self.imported_session("quick")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TABLE manual_chatgpt_decisions")
            connection.execute("DROP TABLE manual_chatgpt_api_reviews")
            connection.execute("DROP TABLE manual_chatgpt_review_runs")
        existing_store = StudioStore._open_existing_schema(self.db_path)
        provider = FakeReviewProvider()
        service = ManualChatGPTService(
            existing_store,
            review_rate_card={},
            providers=ProviderRegistry({provider.provider_id: provider}),
        )
        readable = service.get(self.room_id, imported["id"])
        self.assertTrue(readable["integrity"]["ok"])
        self.assertEqual(readable["state"], "API_REVIEW")
        self.assertTrue(readable["api_review"]["migration_required"])
        with self.assertRaises(ManualChatGPTError) as caught:
            service.run_api_review(
                self.room_id,
                imported["id"],
                provider_id=provider.provider_id,
                model="fake-review-v1",
                client_request_id="review-migration-required",
                expected_result_sha256=imported["result_sha256"],
            )
        self.assertEqual(caught.exception.code, "MANUAL_CHATGPT_MIGRATION_REQUIRED")
        self.assertEqual(provider.calls, [])
        with closing(sqlite3.connect(self.db_path)) as connection:
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertNotIn("manual_chatgpt_review_runs", names)

    def test_explicit_rate_card_produces_a_frozen_nonzero_api_review_estimate(self) -> None:
        service = ManualChatGPTService(self.store, review_rate_card={
            "label": "test-review-rate-v1",
            "input_usd_per_million_tokens": "2.5",
            "output_usd_per_million_tokens": "10",
        })
        session = service.create(
            self.room_id,
            objective="Estimate the bounded review workload without making a call.",
            mode="deep",
        )
        planning = session["bundle"]["planning"]
        cost = planning["estimated_api_cost"]
        workload = planning["workload"]
        self.assertEqual(cost["status"], "estimated")
        self.assertEqual(cost["rate_card_label"], "test-review-rate-v1")
        self.assertGreater(float(cost["amount_usd"]), 0)
        self.assertEqual(workload["independent_api_review_calls"], 4)
        self.assertEqual(workload["api_review_output_token_budget"], 8_000)
        self.assertTrue(cost["not_a_bill"])
        self.assertFalse(cost["manual_chatgpt_cost_included"])
        self.assertEqual(session["safety"]["provider_calls_performed"], 0)

    def test_invalid_import_stores_only_issues_and_can_be_repaired(self) -> None:
        session = self.service.create(
            self.room_id,
            objective="Reject incomplete conclusions.",
            mode="quick",
        )
        waiting = self.service.dispatch(self.room_id, session["id"])
        rejected = self.service.import_result(
            self.room_id,
            waiting["id"],
            '{"untrusted_secret":"must-not-persist"}',
        )
        self.assertEqual(rejected["state"], "IMPORT_REJECTED")
        self.assertTrue(rejected["validation_issues"])
        self.assertTrue(rejected["repair_prompt"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT result_json,last_issues_json FROM manual_chatgpt_sessions WHERE id=?",
                (session["id"],),
            ).fetchone()
        self.assertEqual(row[0], "{}")
        self.assertNotIn("must-not-persist", row[1])

        repaired = self.service.import_result(
            self.room_id,
            session["id"],
            json.dumps(valid_result(rejected), ensure_ascii=False),
        )
        self.assertEqual(repaired["state"], "API_REVIEW")

    def test_duplicate_key_import_stores_only_the_exact_issue_and_can_be_repaired(self) -> None:
        session = self.service.create(
            self.room_id,
            objective="Reject ambiguous duplicate conclusions.",
            mode="quick",
        )
        waiting = self.service.dispatch(self.room_id, session["id"])
        valid = json.dumps(valid_result(waiting), ensure_ascii=False)
        ambiguous = valid.replace(
            '"summary": "synthesis summary"',
            '"summary": "duplicate-value-must-not-persist", "summary": "synthesis summary"',
            1,
        )
        self.assertNotEqual(ambiguous, valid)

        rejected = self.service.import_result(
            self.room_id,
            waiting["id"],
            ambiguous,
        )
        self.assertEqual(rejected["state"], "IMPORT_REJECTED")
        self.assertEqual(rejected["validation_issues"], [{
            "path": "$.panels[0].summary",
            "code": "DUPLICATE_KEY",
            "message": "同一路径只能出现一次。",
        }])
        self.assertIn("$.panels[0].summary", rejected["repair_prompt"])
        self.assertEqual(rejected["result"], {})
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT result_json,last_issues_json FROM manual_chatgpt_sessions WHERE id=?",
                (session["id"],),
            ).fetchone()
        self.assertEqual(row[0], "{}")
        self.assertNotIn("duplicate-value-must-not-persist", row[1])

        repaired = self.service.import_result(
            self.room_id,
            session["id"],
            valid,
        )
        self.assertEqual(repaired["state"], "API_REVIEW")

    def test_context_drift_blocks_import_and_requires_new_bundle(self) -> None:
        session = self.service.create(
            self.room_id,
            objective="Detect evidence drift.",
            mode="quick",
        )
        waiting = self.service.dispatch(self.room_id, session["id"])
        self.store.add_material(self.room_id, {
            "title": "Later evidence",
            "kind": "note",
            "content": "This appeared after the bundle was frozen.",
        })
        stale = self.service.import_result(
            self.room_id,
            waiting["id"],
            json.dumps(valid_result(waiting), ensure_ascii=False),
        )
        self.assertEqual(stale["state"], "CONTEXT_STALE")
        self.assertEqual(stale["validation_issues"][0]["path"], "$.context_sha256")
        self.assertEqual(stale["result"], {})

    def test_context_drift_after_import_blocks_review_before_provider_calls(self) -> None:
        imported = self.imported_session("quick")
        self.store.add_material(self.room_id, {
            "title": "Evidence added after import",
            "kind": "note",
            "content": "This must invalidate the frozen review context.",
        })
        provider = FakeReviewProvider()
        service = ManualChatGPTService(
            self.store,
            review_rate_card={},
            providers=ProviderRegistry({provider.provider_id: provider}),
        )

        stale = service.run_api_review(
            self.room_id,
            imported["id"],
            provider_id=provider.provider_id,
            model="fake-review-v1",
            client_request_id="review-context-stale-before-call",
            expected_result_sha256=imported["result_sha256"],
        )

        self.assertEqual(stale["state"], "CONTEXT_STALE")
        self.assertEqual(stale["validation_issues"][0]["code"], "CONTEXT_STALE")
        self.assertEqual(provider.calls, [])
        self.assertEqual(stale["decision_card"], {})
        with closing(sqlite3.connect(self.db_path)) as connection:
            execution_runs = connection.execute(
                "SELECT COUNT(*) FROM provider_execution_runs WHERE client_request_id=?",
                ("review-context-stale-before-call",),
            ).fetchone()[0]
            review_runs = connection.execute(
                "SELECT COUNT(*) FROM manual_chatgpt_review_runs WHERE session_id=?",
                (imported["id"],),
            ).fetchone()[0]
        self.assertEqual(execution_runs, 0)
        self.assertEqual(review_runs, 0)

    def test_context_drift_during_review_prevents_decision_card_publish(self) -> None:
        imported = self.imported_session("quick")

        def mutate_context() -> None:
            self.store.add_material(self.room_id, {
                "title": "Evidence added during review",
                "kind": "note",
                "content": "The completed reviews must not publish a stale decision card.",
            })

        provider = ContextMutatingReviewProvider(
            mutate_context,
            mutate_at_call=2,
        )
        service = ManualChatGPTService(
            self.store,
            review_rate_card={},
            providers=ProviderRegistry({provider.provider_id: provider}),
        )

        stale = service.run_api_review(
            self.room_id,
            imported["id"],
            provider_id=provider.provider_id,
            model="fake-review-v1",
            client_request_id="review-context-stale-before-publish",
            expected_result_sha256=imported["result_sha256"],
        )

        self.assertEqual(stale["state"], "CONTEXT_STALE")
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(stale["decision_card"], {})
        self.assertEqual(stale["api_review"]["status"], "FAILED")
        with closing(sqlite3.connect(self.db_path)) as connection:
            decision_count = connection.execute(
                "SELECT COUNT(*) FROM manual_chatgpt_decisions WHERE session_id=?",
                (imported["id"],),
            ).fetchone()[0]
            review_run = connection.execute(
                """SELECT status,error_code FROM manual_chatgpt_review_runs
                     WHERE session_id=?""",
                (imported["id"],),
            ).fetchone()
        self.assertEqual(decision_count, 0)
        self.assertEqual(review_run, ("FAILED", "MANUAL_CHATGPT_CONTEXT_STALE"))

    def test_context_drift_after_review_prevents_user_decision_freeze(self) -> None:
        imported = self.imported_session("quick")
        provider = FakeReviewProvider()
        service = ManualChatGPTService(
            self.store,
            review_rate_card={},
            providers=ProviderRegistry({provider.provider_id: provider}),
        )
        reviewed = service.run_api_review(
            self.room_id,
            imported["id"],
            provider_id=provider.provider_id,
            model="fake-review-v1",
            client_request_id="review-before-freeze-context-stale",
            expected_result_sha256=imported["result_sha256"],
        )
        self.assertEqual(reviewed["state"], "READY_FOR_DECISION")
        self.store.add_material(self.room_id, {
            "title": "Evidence added before freeze",
            "kind": "note",
            "content": "The prior decision card is now stale.",
        })

        stale = service.freeze_decision(
            self.room_id,
            imported["id"],
            expected_result_sha256=reviewed["result_sha256"],
            decision_card_sha256=reviewed["decision_card_sha256"],
            selected_option_id="option_1",
            acknowledgement=MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
        )

        self.assertEqual(stale["state"], "CONTEXT_STALE")
        self.assertEqual(stale["confirmation"], {})
        self.assertEqual(stale["frozen_at"], 0)
        with closing(sqlite3.connect(self.db_path)) as connection:
            frozen = connection.execute(
                """SELECT selected_option_id,confirmation_sha256,frozen_at
                     FROM manual_chatgpt_decisions WHERE session_id=?""",
                (imported["id"],),
            ).fetchone()
        self.assertEqual(frozen, ("", "", 0))

    def test_event_tamper_hides_bundle_and_result(self) -> None:
        session = self.service.create(
            self.room_id,
            objective="Keep the audit chain fail-closed.",
            mode="quick",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE manual_chatgpt_events SET event_type='tampered' WHERE session_id=? AND sequence_no=1",
                (session["id"],),
            )
        hidden = self.service.get(self.room_id, session["id"])
        self.assertFalse(hidden["integrity"]["ok"])
        self.assertEqual(hidden["bundle"], {})
        self.assertEqual(hidden["task_prompt"], "")
        self.assertEqual(hidden["state"], "IMPORT_REJECTED")


class ManualChatGPTHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-manual-chatgpt-http-",
            ignore_cleanup_errors=True,
        )
        self.original_store = http_server.STORE
        self.original_providers = http_server.PROVIDERS
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        http_server.STORE = self.store
        self.review_provider = FakeReviewProvider()
        http_server.PROVIDERS = ProviderRegistry({
            self.review_provider.provider_id: self.review_provider,
        })
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.room_id = self.store.create_room(
            "Manual ChatGPT HTTP",
            "Exercise the local HTTP contract.",
        )["room"]["id"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_server.STORE = self.original_store
        http_server.PROVIDERS = self.original_providers
        self.temp_dir.cleanup()

    def post(self, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            method="POST",
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_create_latest_dispatch_and_rejected_import_routes(self) -> None:
        base_path = f"/api/rooms/{self.room_id}/chatgpt-collaborations"
        status, created = self.post(base_path, {
            "objective": "Exercise one fenced collaboration result.",
            "mode": "quick",
        })
        self.assertEqual(status, 201)
        session = created["manual_chatgpt"]
        self.assertEqual(session["state"], "BUNDLE_READY")

        with urlopen(self.base_url + base_path + "/latest", timeout=5) as response:
            latest = json.loads(response.read())
        self.assertEqual(latest["manual_chatgpt"]["id"], session["id"])

        status, dispatched = self.post(
            base_path + f"/{session['id']}/dispatch",
            {},
        )
        self.assertEqual(status, 200)
        self.assertEqual(dispatched["manual_chatgpt"]["state"], "WAITING_FOR_CHATGPT")

        status, rejected = self.post(
            base_path + f"/{session['id']}/imports",
            {"content": '{"missing":"contract"}'},
        )
        self.assertEqual(status, 200)
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["manual_chatgpt"]["state"], "IMPORT_REJECTED")

    def test_http_review_and_freeze_require_explicit_bound_payloads(self) -> None:
        base_path = f"/api/rooms/{self.room_id}/chatgpt-collaborations"
        _, created = self.post(base_path, {
            "objective": "Exercise the complete guarded HTTP path.",
            "mode": "quick",
        })
        session = created["manual_chatgpt"]
        _, dispatched = self.post(base_path + f"/{session['id']}/dispatch", {})
        waiting = dispatched["manual_chatgpt"]
        _, imported_response = self.post(
            base_path + f"/{session['id']}/imports",
            {"content": json.dumps(valid_result(waiting), ensure_ascii=False)},
        )
        imported = imported_response["manual_chatgpt"]
        status, reviewed_response = self.post(
            base_path + f"/{session['id']}/api-reviews",
            {
                "provider": self.review_provider.provider_id,
                "model": "fake-review-v1",
                "client_request_id": "http-review-complete",
                "expected_result_sha256": imported["result_sha256"],
            },
        )
        self.assertEqual(status, 200)
        reviewed = reviewed_response["manual_chatgpt"]
        self.assertEqual(reviewed["state"], "READY_FOR_DECISION")
        self.assertEqual(len(self.review_provider.calls), 2)

        status, frozen_response = self.post(
            base_path + f"/{session['id']}/freeze",
            {
                "expected_result_sha256": reviewed["result_sha256"],
                "decision_card_sha256": reviewed["decision_card_sha256"],
                "selected_option_id": "option_1",
                "acknowledgement": MANUAL_CHATGPT_FREEZE_ACKNOWLEDGEMENT,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(frozen_response["manual_chatgpt"]["state"], "FROZEN")
        self.assertEqual(len(self.review_provider.calls), 2)


if __name__ == "__main__":
    unittest.main()
