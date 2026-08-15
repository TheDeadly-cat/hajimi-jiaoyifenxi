from __future__ import annotations

import json
import unittest

from backend.turn_contract import (
    TURN_CONTRACT_VERSION,
    candidate_risk_review_protocol_required,
    extract_turn_contract,
    validate_stored_turn_contract,
)


MESSAGE_IDS = {"msg_analysis", "msg_plan", "msg_bull", "msg_risk"}
MATERIAL_IDS = {"mat_market", "mat_filing"}
MARKET_SNAPSHOT_ID = "frozen-market-round-a"


def contract_message(payload: dict, visible: str = "这是可展示的专业发言。") -> str:
    return (
        f"{visible}\n"
        f"<turn_contract>{json.dumps(payload, ensure_ascii=False)}</turn_contract>"
    )


def base_payload(**updates) -> dict:
    payload = {
        "version": TURN_CONTRACT_VERSION,
        "claims": [],
        "responds_to": [],
        "candidate_updates": [],
        "risks": [],
        "next_actions": [],
        "confidence": {
            "kind": "model_subjective",
            "value": None,
            "label": "unknown",
            "basis": "",
        },
    }
    payload.update(updates)
    return payload


def evidence(source_type: str, source_id: str, role: str = "support") -> dict:
    return {"type": source_type, "id": source_id, "role": role}


def claim(
    claim_id: str = "claim_1",
    *,
    kind: str = "fact",
    as_of: str = "2026-08-01T09:30:00-04:00",
    refs: list[dict] | None = None,
) -> dict:
    return {
        "id": claim_id,
        "kind": kind,
        "text": "统一截面的市场数据可用于本轮比较。",
        "as_of": as_of,
        "evidence": refs if refs is not None else [evidence("material", "mat_market")],
    }


def response(message_id: str = "msg_analysis", relation: str = "challenges") -> dict:
    return {
        "type": "message",
        "id": message_id,
        "relation": relation,
        "reason": "该结论仍缺少失效条件。",
    }


def candidate(
    candidate_id: str,
    action: str,
    *,
    invalidation: str = "价格结构或基本面假设失效。",
    refs: list[dict] | None = None,
) -> dict:
    return {
        "id": candidate_id,
        "title": f"候选 {candidate_id}",
        "action": action,
        "symbol": "US.MU",
        "direction": "UP",
        "horizon_days": 20,
        "thesis": "仅建立可验证的模拟观察。",
        "invalidation": invalidation,
        "evidence": refs if refs is not None else [evidence("message", "msg_analysis")],
    }


def risk(refs: list[dict] | None = None) -> dict:
    return {
        "id": "risk_1",
        "text": "财报跳空可能超过纸面风险预算。",
        "severity": "high",
        "status": "open",
        "trigger": "财报前隐含波动率异常上升。",
        "mitigation": "保持模拟仓位并等待用户复核。",
        "blocking": True,
        "evidence": refs if refs is not None else [evidence("message", "msg_plan", "counter")],
    }


def next_action() -> dict:
    return {
        "id": "action_1",
        "text": "核对冻结数据时间并补充失效条件。",
        "owner": "数据质量官",
        "state": "open",
        "due": "",
        "evidence": [evidence("material", "mat_market", "context")],
    }


def parse(
    payload: dict,
    member: dict | None = None,
    *,
    allowed_market_snapshot_id: str = "",
    prior_ai_message_ids: set[str] | None = None,
) -> dict:
    return extract_turn_contract(
        contract_message(payload),
        member=member,
        allowed_message_ids=MESSAGE_IDS,
        allowed_material_ids=MATERIAL_IDS,
        allowed_market_snapshot_id=allowed_market_snapshot_id,
        prior_ai_message_ids=prior_ai_message_ids,
    )


class CandidateRiskReviewProtocolTests(unittest.TestCase):
    def test_protocol_requires_explicit_risk_stage_and_sealed_risk_member(self) -> None:
        general_policy = {
            "minimum_stage_coverage": {"analysis": 1, "decision": 1},
            "required_coverage": [],
        }
        sealed_risk_member = {
            "workflow_stage": "risk",
            "stance": "risk",
            "capabilities": ["risk_review"],
        }
        self.assertFalse(candidate_risk_review_protocol_required(
            general_policy,
            [sealed_risk_member],
        ))

        explicit_risk_policy = {
            "minimum_stage_coverage": {"analysis": 1, "risk": 1, "decision": 1},
            "required_coverage": [],
        }
        self.assertTrue(candidate_risk_review_protocol_required(
            explicit_risk_policy,
            [sealed_risk_member],
        ))
        self.assertFalse(candidate_risk_review_protocol_required(
            explicit_risk_policy,
            [{
                "workflow_stage": "analysis",
                "stance": "analyst",
                "capabilities": ["evidence_review"],
            }],
        ))


def issue_codes(result: dict) -> set[str]:
    return {str(item.get("code") or "") for item in result["issues"]}


class TurnContractExtractionTests(unittest.TestCase):
    def test_analysis_contract_is_hidden_normalized_and_qualified(self) -> None:
        payload = base_payload(
            claims=[claim()],
            confidence={
                "kind": "model_subjective",
                "value": 63,
                "label": "medium",
                "basis": "证据完整但样本仍有限。",
            },
        )

        result = parse(
            payload,
            {"workflow_stage": "analysis", "stance": "fundamental", "capabilities": []},
        )

        self.assertTrue(result["found"])
        self.assertTrue(result["qualified"])
        self.assertEqual(result["visible_content"], "这是可展示的专业发言。")
        self.assertNotIn("turn_contract", result["visible_content"])
        self.assertEqual(result["role_profiles"], ["analysis"])
        self.assertEqual(result["contract"]["claims"][0]["evidence"], [
            {"type": "material", "id": "mat_market", "role": "support"},
        ])
        self.assertEqual(result["contract"]["confidence"]["value"], 63.0)
        self.assertTrue(result["confidence_is_not_win_rate"])
        self.assertEqual(result["execution_capability"], "none")
        self.assertFalse(result["live_trading_allowed"])
        self.assertFalse(result["can_autonomously_decide"])

    def test_missing_malformed_and_multiple_blocks_fail_closed(self) -> None:
        missing = extract_turn_contract("只有普通正文。")
        self.assertFalse(missing["qualified"])
        self.assertIn("TURN_CONTRACT_MISSING", issue_codes(missing))

        malformed_text = "可见正文\n<turn_contract>{\"version\":\"turn_contract_v1\"}内部内容"
        malformed = extract_turn_contract(malformed_text)
        self.assertFalse(malformed["qualified"])
        self.assertIn("TURN_CONTRACT_TAG_MALFORMED", issue_codes(malformed))
        self.assertEqual(malformed["visible_content"], "可见正文")
        self.assertNotIn("内部内容", malformed["visible_content"])

        payload = base_payload(next_actions=[next_action()])
        block = f"<turn_contract>{json.dumps(payload, ensure_ascii=False)}</turn_contract>"
        multiple = extract_turn_contract(
            f"正文\n{block}\n{block}",
            allowed_material_ids=MATERIAL_IDS,
        )
        self.assertFalse(multiple["qualified"])
        self.assertIn("TURN_CONTRACT_MULTIPLE", issue_codes(multiple))
        self.assertEqual(multiple["visible_content"], "正文")

    def test_invalid_json_and_duplicate_keys_fail_closed(self) -> None:
        invalid = extract_turn_contract("正文<turn_contract>{invalid}</turn_contract>")
        self.assertIn("TURN_CONTRACT_JSON_INVALID", issue_codes(invalid))
        self.assertIsNone(invalid["contract"])

        duplicate = extract_turn_contract(
            '正文<turn_contract>{"version":"turn_contract_v1","version":"turn_contract_v1"}</turn_contract>'
        )
        self.assertIn("JSON_DUPLICATE_KEY", issue_codes(duplicate))
        self.assertIsNone(duplicate["contract"])

    def test_reference_allowlist_is_enforced_and_untrusted_reference_is_removed(self) -> None:
        payload = base_payload(
            claims=[claim(refs=[evidence("material", "mat_not_allowed")])],
        )

        result = parse(payload, {"workflow_stage": "analysis"})

        self.assertFalse(result["qualified"])
        self.assertIn("REFERENCE_NOT_ALLOWED", issue_codes(result))
        self.assertEqual(result["contract"]["claims"][0]["evidence"], [])
        self.assertIn("ANALYSIS_GROUNDED_CLAIM_REQUIRED", issue_codes(result))

    def test_prior_ai_response_graph_is_optional_for_legacy_and_first_turn_calls(self) -> None:
        payload = base_payload(claims=[claim()])
        member = {"workflow_stage": "analysis"}

        legacy = parse(payload, member)
        first_turn = parse(payload, member, prior_ai_message_ids=set())

        self.assertTrue(legacy["qualified"], legacy["issues"])
        self.assertTrue(first_turn["qualified"], first_turn["issues"])

    def test_later_turn_requires_response_to_frozen_prior_ai_prefix(self) -> None:
        member = {"workflow_stage": "analysis"}
        missing = parse(
            base_payload(claims=[claim()]),
            member,
            prior_ai_message_ids={"msg_analysis"},
        )
        accepted = parse(
            base_payload(
                claims=[claim()],
                responds_to=[response("msg_analysis", "supports")],
            ),
            member,
            prior_ai_message_ids={"msg_analysis"},
        )

        self.assertFalse(missing["qualified"])
        self.assertIn("PRIOR_AI_RESPONSE_REQUIRED", issue_codes(missing))
        self.assertTrue(accepted["qualified"], accepted["issues"])

    def test_response_graph_rejects_allowed_non_ai_target_and_invalid_relation(self) -> None:
        member = {"workflow_stage": "analysis"}
        wrong_target = parse(
            base_payload(
                claims=[claim()],
                responds_to=[response("msg_plan", "qualifies")],
            ),
            member,
            prior_ai_message_ids={"msg_analysis"},
        )
        invalid_relation = parse(
            base_payload(
                claims=[claim()],
                responds_to=[response("msg_analysis", "agrees")],
            ),
            member,
            prior_ai_message_ids={"msg_analysis"},
        )

        self.assertFalse(wrong_target["qualified"])
        self.assertIn("RESPONSE_TARGET_NOT_PRIOR_AI", issue_codes(wrong_target))
        self.assertIn("PRIOR_AI_RESPONSE_REQUIRED", issue_codes(wrong_target))
        self.assertFalse(invalid_relation["qualified"])
        self.assertIn("RESPONSE_RELATION_INVALID", issue_codes(invalid_relation))
        self.assertIn("PRIOR_AI_RESPONSE_REQUIRED", issue_codes(invalid_relation))

    def test_stored_contract_revalidates_the_same_frozen_response_prefix(self) -> None:
        member = {"workflow_stage": "analysis"}
        extracted = parse(
            base_payload(
                claims=[claim()],
                responds_to=[response("msg_analysis", "questions")],
            ),
            member,
            prior_ai_message_ids={"msg_analysis"},
        )
        self.assertTrue(extracted["qualified"], extracted["issues"])

        accepted = validate_stored_turn_contract(
            extracted["contract"],
            member=member,
            allowed_message_ids=MESSAGE_IDS,
            allowed_material_ids=MATERIAL_IDS,
            prior_ai_message_ids={"msg_analysis"},
        )
        changed_prefix = validate_stored_turn_contract(
            extracted["contract"],
            member=member,
            allowed_message_ids=MESSAGE_IDS,
            allowed_material_ids=MATERIAL_IDS,
            prior_ai_message_ids={"msg_plan"},
        )

        self.assertTrue(accepted["qualified"], accepted["issues"])
        self.assertFalse(changed_prefix["qualified"])
        self.assertIn("RESPONSE_TARGET_NOT_PRIOR_AI", issue_codes(changed_prefix))
        self.assertIn("PRIOR_AI_RESPONSE_REQUIRED", issue_codes(changed_prefix))

    def test_round_market_snapshot_reference_is_exact_unique_and_server_bound(self) -> None:
        payload = base_payload(claims=[claim(refs=[
            evidence("round_market_snapshot", MARKET_SNAPSHOT_ID),
        ])])
        member = {"workflow_stage": "analysis", "stance": "technical"}

        accepted = parse(
            payload,
            member,
            allowed_market_snapshot_id=MARKET_SNAPSHOT_ID,
        )

        self.assertTrue(accepted["qualified"], accepted["issues"])
        self.assertEqual(
            accepted["contract"]["claims"][0]["evidence"],
            [{
                "type": "round_market_snapshot",
                "id": MARKET_SNAPSHOT_ID,
                "role": "support",
            }],
        )
        revalidated = validate_stored_turn_contract(
            accepted["contract"],
            member=member,
            allowed_market_snapshot_id=MARKET_SNAPSHOT_ID,
        )
        self.assertTrue(revalidated["qualified"], revalidated["issues"])

        missing = parse(payload, member)
        self.assertFalse(missing["qualified"])
        self.assertIn("REFERENCE_NOT_ALLOWED", issue_codes(missing))

        cross_round = parse(
            payload,
            member,
            allowed_market_snapshot_id="frozen-market-round-b",
        )
        self.assertFalse(cross_round["qualified"])
        self.assertIn("REFERENCE_NOT_ALLOWED", issue_codes(cross_round))
        persisted_cross_round = validate_stored_turn_contract(
            accepted["contract"],
            member=member,
            allowed_market_snapshot_id="frozen-market-round-b",
        )
        self.assertFalse(persisted_cross_round["qualified"])
        self.assertIn("REFERENCE_NOT_ALLOWED", issue_codes(persisted_cross_round))

        forged_identity = json.loads(json.dumps(payload, ensure_ascii=False))
        forged_identity["claims"][0]["evidence"][0].update({
            "source_revision": "forged-revision",
            "source_snapshot_sha256": "0" * 64,
        })
        forged = parse(
            forged_identity,
            member,
            allowed_market_snapshot_id=MARKET_SNAPSHOT_ID,
        )
        self.assertFalse(forged["qualified"])
        self.assertIn("UNKNOWN_FIELD", issue_codes(forged))

    def test_unknown_execution_and_win_rate_fields_are_rejected(self) -> None:
        payload = base_payload(
            next_actions=[next_action()],
            order={"symbol": "US.MU"},
            confidence={
                "kind": "model_subjective",
                "value": 70,
                "label": "high",
                "basis": "仅为主观判断。",
                "win_rate": 70,
            },
        )

        result = parse(payload)

        self.assertFalse(result["qualified"])
        self.assertIn("EXECUTION_FIELD_FORBIDDEN", issue_codes(result))
        self.assertIn("UNKNOWN_FIELD", issue_codes(result))
        self.assertTrue(result["confidence_is_not_win_rate"])

    def test_bounds_enums_and_duplicate_claim_ids_are_strict(self) -> None:
        claims = [claim("same_claim", kind="guess") for _ in range(13)]
        claims[0]["text"] = "X" * 801
        payload = base_payload(claims=claims)

        result = parse(payload, {"workflow_stage": "analysis"})
        codes = issue_codes(result)

        self.assertFalse(result["qualified"])
        self.assertIn("ARRAY_LIMIT_EXCEEDED", codes)
        self.assertIn("CLAIM_KIND_INVALID", codes)
        self.assertIn("TEXT_TOO_LONG", codes)
        self.assertIn("CLAIM_ID_DUPLICATE", codes)
        self.assertEqual(len(result["contract"]["claims"]), 12)

    def test_duplicate_candidate_ids_and_numeric_strings_fail_strict_schema(self) -> None:
        payload = base_payload(
            candidate_updates=[
                candidate("candidate_same", "select"),
                candidate("candidate_same", "reject"),
            ],
            risks=[risk()],
            confidence={
                "kind": "model_subjective",
                "value": "70",
                "label": "high",
                "basis": "字符串形式不应被静默转换。",
            },
        )

        result = parse(payload, {"workflow_stage": "decision"})

        self.assertFalse(result["qualified"])
        self.assertIn("CANDIDATE_ID_DUPLICATE", issue_codes(result))
        self.assertIn("CONFIDENCE_VALUE_INVALID", issue_codes(result))
        self.assertIsNone(result["contract"]["confidence"]["value"])

    def test_persisted_contract_is_revalidated_against_frozen_prefix(self) -> None:
        member = {
            "workflow_stage": "decision",
            "stance": "portfolio_manager",
            "capabilities": ["decision_synthesis"],
        }
        extracted = parse(
            base_payload(
                candidate_updates=[
                    candidate("candidate_a", "select"),
                    candidate("candidate_b", "reject"),
                ],
                risks=[risk()],
            ),
            member,
        )
        self.assertTrue(extracted["qualified"], extracted["issues"])

        valid = validate_stored_turn_contract(
            extracted["contract"],
            member=member,
            allowed_message_ids=MESSAGE_IDS,
            allowed_material_ids=MATERIAL_IDS,
        )
        self.assertTrue(valid["qualified"], valid["issues"])

        tampered = json.loads(json.dumps(extracted["contract"], ensure_ascii=False))
        tampered["candidate_updates"][0]["evidence"][0]["id"] = "msg_future"
        invalid = validate_stored_turn_contract(
            tampered,
            member=member,
            allowed_message_ids=MESSAGE_IDS,
            allowed_material_ids=MATERIAL_IDS,
        )
        self.assertFalse(invalid["qualified"])
        self.assertIn("REFERENCE_NOT_ALLOWED", issue_codes(invalid))

    def test_stored_json_duplicate_keys_fail_closed(self) -> None:
        invalid = validate_stored_turn_contract(
            '{"version":"turn_contract_v1","version":"turn_contract_v1"}'
        )
        self.assertFalse(invalid["qualified"])
        self.assertIn("JSON_DUPLICATE_KEY", issue_codes(invalid))


class TurnContractRoleQualificationTests(unittest.TestCase):
    def test_each_supported_role_accepts_a_minimum_professional_contract(self) -> None:
        cases = {
            "analysis": (
                {"workflow_stage": "analysis", "stance": "data_guardian", "capabilities": ["data_quality_review"]},
                base_payload(claims=[claim()]),
            ),
            "debate": (
                {"workflow_stage": "debate", "stance": "bear", "capabilities": ["bear_case"]},
                base_payload(
                    responds_to=[response("msg_bull", "challenges")],
                    candidate_updates=[candidate("candidate_bear", "challenge")],
                ),
            ),
            "risk": (
                {"workflow_stage": "risk", "stance": "risk", "capabilities": ["risk_review"]},
                base_payload(responds_to=[response("msg_plan", "qualifies")], risks=[risk()]),
            ),
            "plan": (
                {"workflow_stage": "plan", "stance": "paper_trader", "capabilities": ["simulation_planning"]},
                base_payload(candidate_updates=[candidate("candidate_plan", "propose")], next_actions=[next_action()]),
            ),
            "decision": (
                {"workflow_stage": "decision", "stance": "portfolio_manager", "capabilities": ["decision_synthesis"]},
                base_payload(
                    candidate_updates=[
                        candidate("candidate_a", "select"),
                        candidate("candidate_b", "reject"),
                    ],
                    risks=[risk()],
                ),
            ),
            "facilitate": (
                {"workflow_stage": "facilitate", "stance": "facilitator", "capabilities": ["facilitation"]},
                base_payload(
                    claims=[claim("objective_gap", kind="unknown", as_of="", refs=[])],
                    next_actions=[next_action()],
                ),
            ),
        }

        for expected_profile, (member, payload) in cases.items():
            with self.subTest(role=expected_profile):
                result = parse(payload, member)
                self.assertTrue(result["qualified"], result["issues"])
                self.assertEqual(result["role_profiles"], [expected_profile])

    def test_role_specific_empty_or_shallow_contracts_fail_closed(self) -> None:
        cases = {
            "analysis": (
                {"workflow_stage": "analysis"},
                base_payload(claims=[claim(kind="unknown", as_of="", refs=[])]),
                "ANALYSIS_GROUNDED_CLAIM_REQUIRED",
            ),
            "debate": (
                {"workflow_stage": "debate"},
                base_payload(claims=[claim()]),
                "DEBATE_TARGET_REQUIRED",
            ),
            "risk": (
                {"workflow_stage": "risk"},
                base_payload(risks=[risk()]),
                "RISK_TARGET_REQUIRED",
            ),
            "plan": (
                {"workflow_stage": "plan"},
                base_payload(candidate_updates=[candidate("candidate_plan", "propose", invalidation="")]),
                "PLAN_CANDIDATE_REQUIRED",
            ),
            "decision": (
                {"workflow_stage": "decision"},
                base_payload(candidate_updates=[candidate("candidate_a", "select")]),
                "DECISION_COMPARISON_REQUIRED",
            ),
            "facilitate": (
                {"workflow_stage": "facilitate"},
                base_payload(next_actions=[]),
                "FACILITATOR_FRAMING_REQUIRED",
            ),
        }

        for role, (member, payload, expected_code) in cases.items():
            with self.subTest(role=role):
                result = parse(payload, member)
                self.assertFalse(result["qualified"])
                self.assertIn(expected_code, issue_codes(result))

    def test_stance_and_capability_profiles_are_enforced_even_without_stage(self) -> None:
        result = parse(
            base_payload(responds_to=[response("msg_plan")], risks=[risk()]),
            {"workflow_stage": "flexible", "stance": "risk", "capabilities": ["risk_review"]},
        )

        self.assertTrue(result["qualified"], result["issues"])
        self.assertEqual(result["role_profiles"], ["risk"])

    def test_conflicting_role_signals_must_satisfy_every_declared_profile(self) -> None:
        result = parse(
            base_payload(claims=[claim()]),
            {"workflow_stage": "analysis", "stance": "risk", "capabilities": []},
        )

        self.assertFalse(result["qualified"])
        self.assertEqual(result["role_profiles"], ["analysis", "risk"])
        self.assertIn("RISK_TARGET_REQUIRED", issue_codes(result))

    def test_subjective_confidence_requires_basis_and_never_becomes_win_rate(self) -> None:
        payload = base_payload(
            next_actions=[next_action()],
            confidence={
                "kind": "model_subjective",
                "value": 82,
                "label": "high",
                "basis": "",
            },
        )

        result = parse(payload)

        self.assertFalse(result["qualified"])
        self.assertIn("CONFIDENCE_BASIS_REQUIRED", issue_codes(result))
        self.assertTrue(result["contract"]["confidence_is_not_win_rate"])
        self.assertNotIn("win_rate", result["contract"]["confidence"])

    def test_decision_select_and_defer_are_strictly_exclusive(self) -> None:
        member = {"workflow_stage": "decision"}
        invalid_cases = [
            [
                candidate("candidate_a", "select"),
                candidate("candidate_b", "defer"),
            ],
            [
                candidate("candidate_a", "select"),
                candidate("candidate_b", "select"),
                candidate("candidate_c", "defer"),
            ],
        ]
        for updates in invalid_cases:
            with self.subTest(actions=[item["action"] for item in updates]):
                result = parse(base_payload(candidate_updates=updates, risks=[risk()]), member)
                self.assertFalse(result["qualified"])
                self.assertIn("DECISION_SELECTION_REQUIRED", issue_codes(result))


if __name__ == "__main__":
    unittest.main()
