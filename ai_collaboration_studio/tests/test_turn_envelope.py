from __future__ import annotations

import json
import unittest

from backend.turn_contract import (
    CONFIDENCE_BOUNDARY,
    TURN_CONTRACT_VERSION,
    extract_turn_contract,
    validate_turn_contract_payload,
)
from backend.turn_envelope import (
    TURN_ENVELOPE_OUTPUT_MODES,
    TURN_ENVELOPE_SCHEMA,
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
    extract_turn_envelope,
    normalize_turn_envelope_member_modes,
    normalize_turn_envelope_mode,
    parse_speaker_output,
    turn_envelope_protocol,
)
from tests.turn_contract_fixture import (
    append_valid_turn_contract,
    append_xml_turn_contract,
    wrap_turn_envelope,
)


def base_contract() -> dict:
    return {
        "version": TURN_CONTRACT_VERSION,
        "claims": [{
            "id": "claim_1",
            "kind": "unknown",
            "text": "仍需核验一个问题。",
            "as_of": "",
            "evidence": [],
        }],
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


def envelope_text(
    contract: dict | None = None,
    *,
    visible_content: object = "这是可展示的群聊正文。",
    version: object = TURN_ENVELOPE_VERSION,
    extra: dict | None = None,
) -> str:
    payload = {
        "version": version,
        "turn_contract": contract if contract is not None else base_contract(),
        "visible_content": visible_content,
    }
    payload.update(extra or {})
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def issue_codes(result: dict) -> set[str]:
    return {
        str(issue.get("code") or "")
        for issue in result.get("issues") or []
        if isinstance(issue, dict)
    }


class TurnEnvelopeExtractionTests(unittest.TestCase):
    def test_valid_envelope_is_normalized_and_preserves_safety_boundaries(self) -> None:
        result = extract_turn_envelope(envelope_text())

        self.assertTrue(result["found"])
        self.assertTrue(result["qualified"], result["issues"])
        self.assertEqual(result["wire_format"], "json_envelope")
        self.assertEqual(result["turn_envelope_version"], TURN_ENVELOPE_VERSION)
        self.assertEqual(result["turn_contract_version"], TURN_CONTRACT_VERSION)
        self.assertEqual(result["visible_content"], "这是可展示的群聊正文。")
        self.assertEqual(result["contract"]["version"], TURN_CONTRACT_VERSION)
        self.assertTrue(result["confidence_is_not_win_rate"])
        self.assertEqual(result["confidence_boundary"], CONFIDENCE_BOUNDARY)
        self.assertEqual(result["execution_capability"], "none")
        self.assertFalse(result["live_trading_allowed"])
        self.assertFalse(result["can_autonomously_decide"])

    def test_envelope_requires_exact_root_fields_and_version(self) -> None:
        missing = json.loads(envelope_text())
        missing.pop("visible_content")
        missing_result = extract_turn_envelope(json.dumps(missing, ensure_ascii=False))
        unknown_result = extract_turn_envelope(envelope_text(extra={"debug": True}))
        version_result = extract_turn_envelope(envelope_text(version="turn_envelope_v2"))

        self.assertIn("TURN_ENVELOPE_FIELD_MISSING", issue_codes(missing_result))
        self.assertIn("TURN_ENVELOPE_FIELD_UNKNOWN", issue_codes(unknown_result))
        self.assertIn("TURN_ENVELOPE_VERSION_INVALID", issue_codes(version_result))
        self.assertFalse(missing_result["qualified"])
        self.assertFalse(unknown_result["qualified"])
        self.assertFalse(version_result["qualified"])

    def test_duplicate_keys_are_rejected_at_root_and_inside_contract(self) -> None:
        valid = envelope_text()
        duplicate_root = valid.replace(
            '"version":"turn_envelope_v1"',
            '"version":"turn_envelope_v1","version":"turn_envelope_v1"',
            1,
        )
        duplicate_contract = valid.replace(
            '"version":"turn_contract_v1"',
            '"version":"turn_contract_v1","version":"turn_contract_v1"',
            1,
        )

        for raw in (duplicate_root, duplicate_contract):
            with self.subTest(raw=raw[:80]):
                result = extract_turn_envelope(raw)
                self.assertFalse(result["qualified"])
                self.assertFalse(result["found"])
                self.assertIn("JSON_DUPLICATE_KEY", issue_codes(result))
                self.assertEqual(result["visible_content"], "")

    def test_invalid_json_non_object_and_non_finite_numbers_fail_closed(self) -> None:
        invalid = extract_turn_envelope("```json\n{}\n```")
        array_root = extract_turn_envelope("[]")
        non_finite = extract_turn_envelope(
            envelope_text().replace('"value":null', '"value":NaN')
        )

        self.assertIn("TURN_ENVELOPE_JSON_INVALID", issue_codes(invalid))
        self.assertIn("TURN_ENVELOPE_OBJECT_REQUIRED", issue_codes(array_root))
        self.assertIn("TURN_ENVELOPE_JSON_NON_FINITE", issue_codes(non_finite))
        self.assertEqual(invalid["visible_content"], "")
        self.assertEqual(array_root["visible_content"], "")
        self.assertEqual(non_finite["visible_content"], "")

    def test_visible_content_is_a_bounded_string_and_cannot_hide_xml_contract(self) -> None:
        not_string = extract_turn_envelope(envelope_text(visible_content=7))
        empty = extract_turn_envelope(envelope_text(visible_content=" \n\n "))
        too_long = extract_turn_envelope(envelope_text(visible_content="文" * 8001))
        control_character = extract_turn_envelope(envelope_text(
            visible_content="正文\u0000不可见",
        ))
        hidden_xml = extract_turn_envelope(envelope_text(
            visible_content="正文<turn_contract>{}</turn_contract>",
        ))

        self.assertIn("VISIBLE_CONTENT_STRING_REQUIRED", issue_codes(not_string))
        self.assertIn("VISIBLE_CONTENT_REQUIRED", issue_codes(empty))
        self.assertIn("VISIBLE_CONTENT_TOO_LONG", issue_codes(too_long))
        self.assertIn(
            "VISIBLE_CONTENT_CONTROL_CHARACTER_FORBIDDEN",
            issue_codes(control_character),
        )
        self.assertIn(
            "TURN_CONTRACT_TAG_FORBIDDEN_IN_VISIBLE_CONTENT",
            issue_codes(hidden_xml),
        )
        self.assertTrue(all(
            not result["qualified"]
            for result in (
                not_string,
                empty,
                too_long,
                control_character,
                hidden_xml,
            )
        ))

    def test_contract_requires_every_input_field_and_rejects_execution_fields(self) -> None:
        missing_contract = base_contract()
        missing_contract.pop("risks")
        missing = extract_turn_envelope(envelope_text(missing_contract))

        execution_contract = base_contract()
        execution_contract["claims"][0]["tool_call"] = "place_order"
        execution = extract_turn_envelope(envelope_text(execution_contract))

        self.assertIn("TURN_CONTRACT_FIELD_MISSING", issue_codes(missing))
        self.assertIn("EXECUTION_FIELD_FORBIDDEN", issue_codes(execution))
        self.assertFalse(missing["qualified"])
        self.assertFalse(execution["qualified"])
        self.assertEqual(execution["execution_capability"], "none")
        self.assertFalse(execution["live_trading_allowed"])

    def test_dynamic_reference_and_role_validation_is_shared_with_xml(self) -> None:
        contract = base_contract()
        contract["claims"] = [{
            "id": "grounded_fact",
            "kind": "fact",
            "text": "冻结资料支持该事实。",
            "as_of": "2026-08-03T00:00:00Z",
            "evidence": [{"type": "material", "id": "mat_1", "role": "support"}],
        }]
        member = {"workflow_stage": "analysis", "stance": "fundamental"}

        valid = extract_turn_envelope(
            envelope_text(contract),
            member=member,
            allowed_material_ids={"mat_1"},
        )
        invalid = extract_turn_envelope(
            envelope_text(contract),
            member=member,
            allowed_material_ids={"mat_other"},
        )

        self.assertTrue(valid["qualified"], valid["issues"])
        self.assertEqual(valid["role_profiles"], ["analysis"])
        self.assertFalse(invalid["qualified"])
        self.assertIn("REFERENCE_NOT_ALLOWED", issue_codes(invalid))
        self.assertIn("ANALYSIS_GROUNDED_CLAIM_REQUIRED", issue_codes(invalid))

    def test_oversized_and_non_string_envelopes_do_not_expose_payload_text(self) -> None:
        oversized = extract_turn_envelope("x" * 60001)
        not_string = extract_turn_envelope({"version": TURN_ENVELOPE_VERSION})

        self.assertIn("CONTENT_TOO_LONG", issue_codes(oversized))
        self.assertIn("TURN_ENVELOPE_STRING_REQUIRED", issue_codes(not_string))
        self.assertEqual(oversized["visible_content"], "")
        self.assertEqual(not_string["visible_content"], "")


class TurnEnvelopeCompatibilityTests(unittest.TestCase):
    def test_payload_validator_can_be_strict_without_changing_legacy_xml(self) -> None:
        partial = base_contract()
        partial.pop("risks")

        legacy = extract_turn_contract(append_xml_turn_contract("旧轮正文", partial))
        compatible = validate_turn_contract_payload(
            partial,
            require_all_input_fields=False,
        )
        strict = validate_turn_contract_payload(
            partial,
            require_all_input_fields=True,
        )

        self.assertTrue(legacy["qualified"], legacy["issues"])
        self.assertTrue(compatible["qualified"], compatible["issues"])
        self.assertFalse(strict["qualified"])
        self.assertIn("TURN_CONTRACT_FIELD_MISSING", issue_codes(strict))

    def test_payload_validator_rejects_non_string_mapping_keys_without_raising(self) -> None:
        payload = base_contract()
        payload["claims"][0][7] = "not a JSON field name"

        result = validate_turn_contract_payload(payload)

        self.assertFalse(result["qualified"])
        self.assertIn("JSON_KEY_STRING_REQUIRED", issue_codes(result))

    def test_dispatcher_never_autodetects_or_downgrades_protocols(self) -> None:
        contract = base_contract()
        envelope = envelope_text(contract)
        legacy_xml = append_xml_turn_contract("旧轮正文", contract)

        new_ok = parse_speaker_output(
            envelope,
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_envelope_version=TURN_ENVELOPE_VERSION,
        )
        new_rejects_xml = parse_speaker_output(
            legacy_xml,
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_envelope_version=TURN_ENVELOPE_VERSION,
        )
        old_ok = parse_speaker_output(
            legacy_xml,
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_envelope_version=None,
        )
        old_rejects_envelope = parse_speaker_output(
            envelope,
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_envelope_version=None,
        )

        self.assertTrue(new_ok["qualified"], new_ok["issues"])
        self.assertEqual(new_ok["wire_format"], "json_envelope")
        self.assertFalse(new_rejects_xml["qualified"])
        self.assertIn("TURN_ENVELOPE_JSON_INVALID", issue_codes(new_rejects_xml))
        self.assertTrue(old_ok["qualified"], old_ok["issues"])
        self.assertEqual(old_ok["wire_format"], "legacy_xml")
        self.assertFalse(old_rejects_envelope["qualified"])
        self.assertIn("TURN_CONTRACT_MISSING", issue_codes(old_rejects_envelope))

    def test_plain_and_unsupported_protocol_shapes_are_explicit(self) -> None:
        plain = parse_speaker_output(
            "历史普通正文",
            turn_contract_version=None,
            turn_envelope_version=None,
        )
        unsupported = parse_speaker_output(
            "{}",
            turn_contract_version=TURN_CONTRACT_VERSION,
            turn_envelope_version="turn_envelope_v999",
        )

        self.assertEqual(plain["wire_format"], "plain")
        self.assertEqual(plain["visible_content"], "历史普通正文")
        self.assertFalse(plain["contract_attempted"])
        self.assertEqual(plain["issues"], [])
        self.assertEqual(unsupported["wire_format"], "unsupported")
        self.assertIn("TURN_ENVELOPE_VERSION_UNSUPPORTED", issue_codes(unsupported))

    def test_fixture_auto_encodes_new_and_old_transports(self) -> None:
        input_text = (
            "本条发言合同允许引用的消息ID：无\n"
            "本轮此前正式 AI 消息ID：无\n"
        )
        new_output = append_valid_turn_contract(
            "新轮正文",
            instructions="必须输出 turn_envelope_v1",
            input_text=input_text,
        )
        old_output = append_valid_turn_contract(
            "旧轮正文",
            instructions="追加 <turn_contract>{JSON对象}</turn_contract>",
            input_text=input_text,
        )

        self.assertEqual(json.loads(new_output)["version"], TURN_ENVELOPE_VERSION)
        self.assertNotIn("<turn_contract>", new_output)
        self.assertIn("<turn_contract>", old_output)
        self.assertTrue(extract_turn_envelope(new_output)["qualified"])
        self.assertTrue(extract_turn_contract(old_output)["qualified"])


class TurnEnvelopeProtocolMetadataTests(unittest.TestCase):
    def test_schema_hash_is_stable_and_schema_is_strict(self) -> None:
        self.assertEqual(
            TURN_ENVELOPE_SCHEMA_SHA256,
            "2a98d43667d85b96c1fc6dfd179d7669e50c41c1d075ab9ce2e2656fc40212c7",
        )
        self.assertFalse(TURN_ENVELOPE_SCHEMA["additionalProperties"])
        self.assertEqual(
            TURN_ENVELOPE_SCHEMA["required"],
            ["version", "turn_contract", "visible_content"],
        )
        contract_schema = TURN_ENVELOPE_SCHEMA["properties"]["turn_contract"]
        self.assertFalse(contract_schema["additionalProperties"])
        self.assertEqual(set(contract_schema["required"]), {
            "version",
            "claims",
            "responds_to",
            "candidate_updates",
            "risks",
            "next_actions",
            "confidence",
        })

    def test_output_mode_and_member_mode_canonicalization_is_deterministic(self) -> None:
        self.assertEqual(TURN_ENVELOPE_OUTPUT_MODES, {
            "json_schema",
            "json_object",
            "prompt_json",
        })
        self.assertEqual(normalize_turn_envelope_mode(" JSON_Object "), "json_object")
        self.assertEqual(
            normalize_turn_envelope_member_modes({
                "member_b": "prompt_json",
                "member_a": "JSON_SCHEMA",
            }),
            {
                "member_a": "json_schema",
                "member_b": "prompt_json",
            },
        )
        with self.assertRaises(ValueError):
            normalize_turn_envelope_mode("xml")
        with self.assertRaises(ValueError):
            normalize_turn_envelope_member_modes({"": "json_object"})
        with self.assertRaises(ValueError):
            normalize_turn_envelope_member_modes({
                "member_a": "json_object",
                " member_a ": "prompt_json",
            })

    def test_protocol_marker_uses_current_schema_and_canonical_member_modes(self) -> None:
        protocol = turn_envelope_protocol({
            "member_b": "prompt_json",
            "member_a": "json_object",
        })

        self.assertEqual(protocol, {
            "version": TURN_ENVELOPE_VERSION,
            "schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
            "member_output_modes": {
                "member_a": "json_object",
                "member_b": "prompt_json",
            },
        })


if __name__ == "__main__":
    unittest.main()
