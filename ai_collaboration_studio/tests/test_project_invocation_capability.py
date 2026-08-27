from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import unittest

from backend.project_invocation import (
    PROJECT_CAPABILITY_AUDIENCE,
    PROJECT_CAPABILITY_VERSION,
    PROJECT_INVOCATION_ACTION_INTAKE,
    PROJECT_INVOCATION_ACTION_RESULT_READ,
    PROJECT_INVOCATION_ENVELOPE_VERSION,
    ProjectCapabilityAuthorizer,
    ProjectInvocationError,
    derive_project_invocation_room_id,
    normalize_project_invocation_envelope,
    project_invocation_request_sha256,
    project_invocation_retention_contract,
    project_invocation_semantics,
    project_invocation_semantics_sha256,
    seal_project_invocation_envelope,
)


SECRET = b"project-capability-test-secret-32-bytes-minimum"


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def signed_token(payload: dict[str, object], *, raw: bytes | None = None) -> str:
    encoded = b64url(raw if raw is not None else canonical_bytes(payload))
    signature = hmac.new(SECRET, encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{b64url(signature)}"


def token_payload(token: str) -> dict[str, object]:
    encoded = token.split(".", 1)[0]
    padding = "=" * ((4 - len(encoded) % 4) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + padding))


def unsealed_envelope(
    *,
    caller_id: str = "caller_alpha",
    project_id: str = "project_bazi",
    client_request_id: str = "request-00000001",
) -> dict[str, object]:
    room_id = derive_project_invocation_room_id(
        caller_id,
        project_id,
        client_request_id,
    )
    return {
        "version": PROJECT_INVOCATION_ENVELOPE_VERSION,
        "caller_id": caller_id,
        "project_id": project_id,
        "client_request_id": client_request_id,
        "room_id": room_id,
        "source": {
            "item_id": "source_item_001",
            "revision": "revision-7",
        },
        "workflow_kind": "research",
        "result_profile": "research_report_v1",
        "room_spec": {
            "title": "跨项目研究房间",
            "objective": "整理来源、约束、反证与待确认结论。",
            "domain": "project_research",
            "category": "项目研究",
            "template_id": "project_research",
            "capability_pack_ids": ["structured_project_research"],
        },
        "domain_context": {
            "schema_version": "bazi_context_v1",
            "schema_sha256": "1" * 64,
            "payload_sha256": "2" * 64,
        },
        "input_manifest": {
            "content_sha256": "3" * 64,
            "content_bytes": 4_096,
        },
        "data_handling": {
            "classification": "internal",
            "retention_policy": "project_default",
            "retention_days": None,
        },
        "budget": {
            "max_provider_calls": 4,
            "max_context_bytes": 100_000,
            "max_result_bytes": 200_000,
        },
        "user_confirmation": {
            "required": True,
            "boundary": "before_room_creation",
        },
        "safety": {
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        },
    }


class ProjectCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [2_000_000_000]
        self.authorizer = ProjectCapabilityAuthorizer(
            SECRET,
            clock=lambda: self.now[0],
        )
        self.envelope = seal_project_invocation_envelope(unsealed_envelope())

    def mint(self, **overrides: object) -> str:
        values: dict[str, object] = {
            "caller_id": self.envelope["caller_id"],
            "project_id": self.envelope["project_id"],
            "room_id": self.envelope["room_id"],
            "actions": [
                PROJECT_INVOCATION_ACTION_INTAKE,
                PROJECT_INVOCATION_ACTION_RESULT_READ,
            ],
            "client_request_id": self.envelope["client_request_id"],
            "request_sha256": self.envelope["request_sha256"],
            "ttl_seconds": 30,
        }
        values.update(overrides)
        return self.authorizer.mint(**values)

    def authorize(self, token: str, **overrides: object):
        values: dict[str, object] = {
            "caller_id": self.envelope["caller_id"],
            "project_id": self.envelope["project_id"],
            "room_id": self.envelope["room_id"],
            "action": PROJECT_INVOCATION_ACTION_INTAKE,
            "client_request_id": self.envelope["client_request_id"],
            "request_sha256": self.envelope["request_sha256"],
        }
        values.update(overrides)
        return self.authorizer.authorize(token, **values)

    def assert_unauthorized(self, token: object) -> None:
        with self.assertRaises(ProjectInvocationError) as raised:
            self.authorizer.authorize(token)
        self.assertEqual(raised.exception.code, "PROJECT_CAPABILITY_UNAUTHORIZED")
        self.assertEqual(raised.exception.status, 401)

    def test_capability_is_bound_to_every_claim_and_allowed_action(self) -> None:
        token = self.mint()
        claims = self.authorize(token)
        self.assertEqual(claims.audience, PROJECT_CAPABILITY_AUDIENCE)
        self.assertEqual(claims.caller_id, self.envelope["caller_id"])
        self.assertEqual(claims.project_id, self.envelope["project_id"])
        self.assertEqual(claims.room_id, self.envelope["room_id"])
        self.assertEqual(claims.client_request_id, self.envelope["client_request_id"])
        self.assertEqual(claims.request_sha256, self.envelope["request_sha256"])
        self.assertEqual(claims.issued_at, self.now[0])
        self.assertEqual(claims.expires_at, self.now[0] + 30)
        self.assertGreaterEqual(len(claims.token_id), 16)

        mismatches = {
            "caller_id": "caller_other",
            "project_id": "project_other",
            "room_id": "room_other",
            "client_request_id": "request-other-0001",
            "request_sha256": "f" * 64,
        }
        for field, value in mismatches.items():
            with self.subTest(field=field):
                self.assert_unauthorized_binding(token, **{field: value})

        intake_only = self.mint(actions=[PROJECT_INVOCATION_ACTION_INTAKE])
        with self.assertRaises(ProjectInvocationError) as denied:
            self.authorize(
                intake_only,
                action=PROJECT_INVOCATION_ACTION_RESULT_READ,
            )
        self.assertEqual(denied.exception.code, "PROJECT_CAPABILITY_ACTION_DENIED")
        self.assertEqual(denied.exception.status, 403)

    def assert_unauthorized_binding(self, token: str, **overrides: object) -> None:
        with self.assertRaises(ProjectInvocationError) as raised:
            self.authorize(token, **overrides)
        self.assertEqual(raised.exception.code, "PROJECT_CAPABILITY_UNAUTHORIZED")
        self.assertEqual(raised.exception.status, 401)

    def test_signature_ttl_future_issue_and_token_shape_fail_closed(self) -> None:
        token = self.mint()
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        self.assert_unauthorized(tampered)
        self.assert_unauthorized(token + ".extra")
        self.assert_unauthorized("é." + token.split(".", 1)[1])
        self.assert_unauthorized(b"not-native-text")

        self.now[0] += 30
        self.assert_unauthorized(token)

        payload = token_payload(self.mint())
        payload["iat"] = self.now[0] + 6
        payload["exp"] = self.now[0] + 30
        self.assert_unauthorized(signed_token(payload))
        payload["iat"] = self.now[0]
        payload["exp"] = self.now[0] + 901
        self.assert_unauthorized(signed_token(payload))

    def test_capability_claims_are_closed_exact_native_unique_and_canonical(self) -> None:
        baseline = token_payload(self.mint())
        cases: list[tuple[str, dict[str, object]]] = []

        missing = copy.deepcopy(baseline)
        missing.pop("aud")
        cases.append(("missing", missing))
        extra = copy.deepcopy(baseline)
        extra["scope"] = "all"
        cases.append(("extra", extra))
        wrong_audience = copy.deepcopy(baseline)
        wrong_audience["aud"] = "readonly-mcp"
        cases.append(("audience", wrong_audience))
        duplicate_actions = copy.deepcopy(baseline)
        duplicate_actions["actions"] = [
            PROJECT_INVOCATION_ACTION_INTAKE,
            PROJECT_INVOCATION_ACTION_INTAKE,
        ]
        cases.append(("duplicate-actions", duplicate_actions))
        unsorted_actions = copy.deepcopy(baseline)
        unsorted_actions["actions"] = [
            PROJECT_INVOCATION_ACTION_RESULT_READ,
            PROJECT_INVOCATION_ACTION_INTAKE,
        ]
        cases.append(("unsorted-actions", unsorted_actions))
        boolean_time = copy.deepcopy(baseline)
        boolean_time["iat"] = True
        cases.append(("bool-time", boolean_time))
        uppercase_hash = copy.deepcopy(baseline)
        uppercase_hash["request_sha256"] = str(baseline["request_sha256"]).upper()
        cases.append(("uppercase-hash", uppercase_hash))
        short_jti = copy.deepcopy(baseline)
        short_jti["jti"] = "short"
        cases.append(("short-jti", short_jti))

        for name, payload in cases:
            with self.subTest(name=name):
                self.assert_unauthorized(signed_token(payload))

        noncanonical_raw = json.dumps(
            baseline,
            ensure_ascii=False,
            sort_keys=False,
            indent=1,
        ).encode("utf-8")
        self.assertNotEqual(noncanonical_raw, canonical_bytes(baseline))
        self.assert_unauthorized(signed_token(baseline, raw=noncanonical_raw))

        canonical = canonical_bytes(baseline)
        duplicate_key_raw = canonical.replace(
            b'"aud":"ai_collaboration_studio.project_invocation_v1"',
            b'"aud":"ai_collaboration_studio.project_invocation_v1",'
            b'"aud":"ai_collaboration_studio.project_invocation_v1"',
            1,
        )
        self.assert_unauthorized(signed_token(baseline, raw=duplicate_key_raw))

        encoded, signature = self.mint().split(".", 1)
        self.assert_unauthorized(f"{encoded}=.{signature}")

    def test_mint_rejects_secret_subclasses_duplicate_actions_and_boolean_ttl(self) -> None:
        class HostileText(str):
            def encode(self, *_args, **_kwargs):  # pragma: no cover - must not run
                raise AssertionError("subclass-controlled encode was invoked")

        with self.assertRaises(ProjectInvocationError) as secret_error:
            ProjectCapabilityAuthorizer(HostileText("x" * 40))
        self.assertEqual(secret_error.exception.code, "PROJECT_CAPABILITY_SECRET_INVALID")

        with self.assertRaises(ProjectInvocationError):
            self.mint(actions=[PROJECT_INVOCATION_ACTION_INTAKE] * 2)
        with self.assertRaises(ProjectInvocationError):
            self.mint(ttl_seconds=True)
        with self.assertRaises(ProjectInvocationError):
            self.mint(caller_id=HostileText("caller_alpha"))


class ProjectInvocationEnvelopeTests(unittest.TestCase):
    def test_seal_normalize_hash_semantics_and_derived_room_are_stable(self) -> None:
        raw = unsealed_envelope()
        sealed = seal_project_invocation_envelope(raw)
        normalized = normalize_project_invocation_envelope(copy.deepcopy(sealed))
        self.assertEqual(normalized, sealed)
        self.assertEqual(
            project_invocation_request_sha256(raw),
            sealed["request_sha256"],
        )
        self.assertEqual(
            project_invocation_request_sha256(sealed),
            sealed["request_sha256"],
        )
        self.assertEqual(
            sealed["room_id"],
            derive_project_invocation_room_id(
                sealed["caller_id"],
                sealed["project_id"],
                sealed["client_request_id"],
            ),
        )
        self.assertRegex(str(sealed["room_id"]), r"^room_inv_[0-9a-f]{64}$")

        semantics = project_invocation_semantics(sealed)
        self.assertEqual(semantics["request_sha256"], sealed["request_sha256"])
        self.assertEqual(semantics["source"]["item_id"], sealed["source"]["item_id"])
        self.assertEqual(
            semantics["source"]["content_sha256"],
            sealed["input_manifest"]["content_sha256"],
        )
        serialized_semantics = json.dumps(semantics, ensure_ascii=False)
        self.assertNotIn(sealed["room_spec"]["title"], serialized_semantics)
        self.assertNotIn(sealed["room_spec"]["objective"], serialized_semantics)
        self.assertRegex(project_invocation_semantics_sha256(sealed), r"^[0-9a-f]{64}$")
        self.assertEqual(
            project_invocation_semantics_sha256(sealed),
            project_invocation_semantics_sha256(copy.deepcopy(sealed)),
        )

        other = unsealed_envelope(project_id="project_other")
        self.assertNotEqual(raw["room_id"], other["room_id"])

    def test_request_hash_excludes_only_itself_and_detects_semantic_drift(self) -> None:
        sealed = seal_project_invocation_envelope(unsealed_envelope())
        supplied_only = copy.deepcopy(sealed)
        supplied_only["request_sha256"] = "f" * 64
        self.assertEqual(
            project_invocation_request_sha256(supplied_only),
            sealed["request_sha256"],
        )
        with self.assertRaises(ProjectInvocationError) as mismatch:
            normalize_project_invocation_envelope(supplied_only)
        self.assertEqual(
            mismatch.exception.code,
            "PROJECT_INVOCATION_REQUEST_HASH_MISMATCH",
        )
        self.assertEqual(mismatch.exception.status, 409)

        drifted = copy.deepcopy(sealed)
        drifted["source"]["revision"] = "revision-8"
        with self.assertRaises(ProjectInvocationError):
            normalize_project_invocation_envelope(drifted)

    def test_room_contract_limits_match_host_creation_limits(self) -> None:
        too_many_packs = unsealed_envelope()
        too_many_packs["room_spec"]["capability_pack_ids"] = [
            f"pack_{index:02d}" for index in range(13)
        ]
        with self.assertRaises(ProjectInvocationError):
            seal_project_invocation_envelope(too_many_packs)

        long_category = unsealed_envelope()
        long_category["room_spec"]["category"] = "类" * 81
        with self.assertRaises(ProjectInvocationError):
            seal_project_invocation_envelope(long_category)

    def test_envelope_and_nested_contracts_are_closed_and_exact_native(self) -> None:
        sealed = seal_project_invocation_envelope(unsealed_envelope())
        mutations = []

        extra_root = copy.deepcopy(sealed)
        extra_root["capability"] = "forbidden"
        mutations.append(extra_root)
        missing_root = copy.deepcopy(sealed)
        missing_root.pop("budget")
        mutations.append(missing_root)
        extra_nested = copy.deepcopy(sealed)
        extra_nested["input_manifest"]["content"] = "raw private payload"
        mutations.append(extra_nested)
        float_budget = copy.deepcopy(sealed)
        float_budget["budget"]["max_provider_calls"] = 1.0
        mutations.append(float_budget)
        boolean_size = copy.deepcopy(sealed)
        boolean_size["input_manifest"]["content_bytes"] = True
        mutations.append(boolean_size)
        unsorted_packs = copy.deepcopy(sealed)
        unsorted_packs["room_spec"]["capability_pack_ids"] = ["z_pack", "a_pack"]
        mutations.append(unsorted_packs)
        execution = copy.deepcopy(sealed)
        execution["safety"]["execution_capability"] = "orders"
        mutations.append(execution)

        for index, value in enumerate(mutations):
            with self.subTest(index=index):
                with self.assertRaises(ProjectInvocationError):
                    normalize_project_invocation_envelope(value)

        class HostileText(str):
            pass

        hostile = copy.deepcopy(sealed)
        hostile["caller_id"] = HostileText("caller_alpha")
        with self.assertRaises(ProjectInvocationError):
            normalize_project_invocation_envelope(hostile)

    def test_room_workflow_confirmation_and_result_profile_bindings_fail_closed(self) -> None:
        raw = unsealed_envelope()
        wrong_room = copy.deepcopy(raw)
        wrong_room["room_id"] = "room_other"
        with self.assertRaises(ProjectInvocationError) as room_error:
            seal_project_invocation_envelope(wrong_room)
        self.assertEqual(
            room_error.exception.code,
            "PROJECT_INVOCATION_ROOM_BINDING_INVALID",
        )

        wrong_profile = copy.deepcopy(raw)
        wrong_profile["result_profile"] = "decision_v1"
        with self.assertRaises(ProjectInvocationError) as profile_error:
            seal_project_invocation_envelope(wrong_profile)
        self.assertEqual(
            profile_error.exception.code,
            "PROJECT_INVOCATION_RESULT_PROFILE_INVALID",
        )

        artifact = copy.deepcopy(raw)
        artifact["workflow_kind"] = "artifact_authoring"
        artifact["result_profile"] = "artifact_draft_v1"
        self.assertEqual(
            seal_project_invocation_envelope(artifact)["result_profile"],
            "artifact_draft_v1",
        )

        no_confirmation = copy.deepcopy(raw)
        no_confirmation["user_confirmation"]["required"] = False
        with self.assertRaises(ProjectInvocationError) as confirmation_error:
            seal_project_invocation_envelope(no_confirmation)
        self.assertEqual(
            confirmation_error.exception.code,
            "PROJECT_INVOCATION_USER_CONFIRMATION_REQUIRED",
        )

    def test_sensitive_data_rejects_default_or_long_retention(self) -> None:
        default_sensitive = unsealed_envelope()
        default_sensitive["data_handling"]["classification"] = "sensitive_personal"
        with self.assertRaises(ProjectInvocationError) as default_error:
            seal_project_invocation_envelope(default_sensitive)
        self.assertEqual(
            default_error.exception.code,
            "PROJECT_INVOCATION_SENSITIVE_RETENTION_REQUIRED",
        )

        long_sensitive = unsealed_envelope()
        long_sensitive["data_handling"] = {
            "classification": "sensitive_financial",
            "retention_policy": "bounded_days",
            "retention_days": 31,
        }
        with self.assertRaises(ProjectInvocationError) as long_error:
            seal_project_invocation_envelope(long_sensitive)
        self.assertEqual(
            long_error.exception.code,
            "PROJECT_INVOCATION_SENSITIVE_RETENTION_REQUIRED",
        )

        minimal_sensitive = unsealed_envelope()
        minimal_sensitive["data_handling"] = {
            "classification": "sensitive_personal",
            "retention_policy": "no_payload_retention",
            "retention_days": None,
        }
        sealed_minimal = seal_project_invocation_envelope(minimal_sensitive)
        self.assertEqual(
            project_invocation_retention_contract(sealed_minimal),
            {
                "classification": "sensitive_personal",
                "retention_policy": "no_payload_retention",
                "payload_retention_allowed": False,
                "max_retention_seconds": 0,
                "sensitive": True,
            },
        )

        bounded_sensitive = unsealed_envelope()
        bounded_sensitive["data_handling"] = {
            "classification": "sensitive_financial",
            "retention_policy": "bounded_days",
            "retention_days": 30,
        }
        sealed_bounded = seal_project_invocation_envelope(bounded_sensitive)
        self.assertEqual(
            project_invocation_retention_contract(sealed_bounded)[
                "max_retention_seconds"
            ],
            30 * 86_400,
        )


if __name__ == "__main__":
    unittest.main()
