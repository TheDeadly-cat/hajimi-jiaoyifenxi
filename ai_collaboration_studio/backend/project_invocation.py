"""Closed contracts for authenticated cross-project Studio invocations.

This module is deliberately independent from the HTTP server, Store, Provider
registry, and read-only MCP gateway.  It validates portable invocation metadata
and one short-lived bearer capability without granting any execution ability.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable


PROJECT_CAPABILITY_VERSION = "project_invocation_capability_v1"
PROJECT_CAPABILITY_AUDIENCE = "ai_collaboration_studio.project_invocation_v1"
PROJECT_INVOCATION_ENVELOPE_VERSION = "project_invocation_envelope_v1"
PROJECT_INVOCATION_SEMANTICS_VERSION = "project_invocation_semantics_v1"

PROJECT_INVOCATION_ACTION_INTAKE = "project_invocation.intake"
PROJECT_INVOCATION_ACTION_RESULT_READ = "project_invocation.result.read"
PROJECT_INVOCATION_INTAKE_PATH = "/api/integration/project-invocations"
PROJECT_INVOCATION_RESULT_PATH_TEMPLATE = (
    "/api/integration/project-invocations/{client_request_id}/result"
)
SUPPORTED_PROJECT_INVOCATION_ACTIONS = frozenset({
    PROJECT_INVOCATION_ACTION_INTAKE,
    PROJECT_INVOCATION_ACTION_RESULT_READ,
})

DEFAULT_PROJECT_CAPABILITY_TTL_SECONDS = 300
MAX_PROJECT_CAPABILITY_TTL_SECONDS = 900
MAX_PROJECT_CAPABILITY_TOKEN_BYTES = 8_192

SUPPORTED_WORKFLOW_RESULT_PROFILES = {
    "decision": "decision_v1",
    "research": "research_report_v1",
    "artifact_authoring": "artifact_draft_v1",
}
SUPPORTED_DATA_CLASSIFICATIONS = frozenset({
    "public",
    "internal",
    "confidential",
    "sensitive_personal",
    "sensitive_financial",
})
SUPPORTED_RETENTION_POLICIES = frozenset({
    "project_default",
    "no_payload_retention",
    "ephemeral_24h",
    "bounded_days",
})
SENSITIVE_DATA_CLASSIFICATIONS = frozenset({
    "sensitive_personal",
    "sensitive_financial",
})

_CAPABILITY_FIELDS = frozenset({
    "version",
    "aud",
    "caller_id",
    "project_id",
    "room_id",
    "actions",
    "client_request_id",
    "request_sha256",
    "iat",
    "exp",
    "jti",
})
_ENVELOPE_FIELDS = frozenset({
    "version",
    "caller_id",
    "project_id",
    "client_request_id",
    "request_sha256",
    "room_id",
    "source",
    "workflow_kind",
    "result_profile",
    "room_spec",
    "domain_context",
    "input_manifest",
    "data_handling",
    "budget",
    "user_confirmation",
    "safety",
})
_UNSEALED_ENVELOPE_FIELDS = _ENVELOPE_FIELDS - {"request_sha256"}
_SOURCE_FIELDS = frozenset({"item_id", "revision"})
_ROOM_SPEC_FIELDS = frozenset({
    "title",
    "objective",
    "domain",
    "category",
    "template_id",
    "capability_pack_ids",
})
_DOMAIN_CONTEXT_FIELDS = frozenset({
    "schema_version",
    "schema_sha256",
    "payload_sha256",
})
_INPUT_MANIFEST_FIELDS = frozenset({"content_sha256", "content_bytes"})
_DATA_HANDLING_FIELDS = frozenset({
    "classification",
    "retention_policy",
    "retention_days",
})
_BUDGET_FIELDS = frozenset({
    "max_provider_calls",
    "max_context_bytes",
    "max_result_bytes",
})
_USER_CONFIRMATION_FIELDS = frozenset({"required", "boundary"})
_SAFETY_FIELDS = frozenset({
    "execution_capability",
    "live_trading_allowed",
    "can_autonomously_decide",
})

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_SHORT_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
_SLUG = re.compile(r"[a-z][a-z0-9_-]{0,79}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_JTI = re.compile(r"[A-Za-z0-9_-]{16,80}")


class ProjectInvocationError(ValueError):
    """Typed, fail-closed project invocation contract error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _fail(code: str, message: str, *, status: int = 400) -> None:
    raise ProjectInvocationError(code, message, status=status)


def _require_exact_mapping(
    value: Any,
    fields: frozenset[str],
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("PROJECT_INVOCATION_REQUEST_INVALID", f"{label} must be an object.")
    actual = set(value)
    if actual != fields or any(type(key) is not str for key in value):
        _fail(
            "PROJECT_INVOCATION_REQUEST_INVALID",
            f"{label} fields are incomplete or unsupported.",
        )
    return value


def _require_text(
    value: Any,
    label: str,
    *,
    minimum: int = 1,
    maximum: int,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if type(value) is not str:
        _fail("PROJECT_INVOCATION_REQUEST_INVALID", f"{label} must be a string.")
    if (
        not minimum <= len(value) <= maximum
        or value != value.strip()
        or unicodedata.normalize("NFC", value) != value
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        _fail("PROJECT_INVOCATION_REQUEST_INVALID", f"{label} is invalid.")
    return value


def _require_identifier(value: Any, label: str, *, maximum: int = 160) -> str:
    pattern = _SHORT_IDENTIFIER if maximum <= 80 else _IDENTIFIER
    return _require_text(value, label, maximum=maximum, pattern=pattern)


def _require_sha256(value: Any, label: str) -> str:
    return _require_text(value, label, maximum=64, pattern=_SHA256)


def _require_integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("PROJECT_INVOCATION_REQUEST_INVALID", f"{label} is invalid.")
    return value


def _validate_exact_json(value: Any, *, path: str = "$") -> None:
    value_type = type(value)
    if value is None or value_type in {str, bool, int}:
        return
    if value_type is float:
        if not math.isfinite(value):
            _fail("PROJECT_INVOCATION_REQUEST_INVALID", f"{path} is non-finite.")
        _fail("PROJECT_INVOCATION_REQUEST_INVALID", f"{path} must not use floats.")
    if value_type is list:
        for index, item in enumerate(value):
            _validate_exact_json(item, path=f"{path}[{index}]")
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail(
                    "PROJECT_INVOCATION_REQUEST_INVALID",
                    f"{path} contains a non-string key.",
                )
            _validate_exact_json(item, path=f"{path}.{key}")
        return
    _fail(
        "PROJECT_INVOCATION_REQUEST_INVALID",
        f"{path} contains a non-native JSON value.",
    )


def _canonical_json_bytes(value: Any) -> bytes:
    _validate_exact_json(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _strict_json_loads(raw: bytes) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        for key, value in pairs:
            if key in parsed:
                raise ValueError("duplicate JSON key")
            parsed[key] = value
        return parsed

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_constant,
    )


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if type(value) is not str or not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid base64url")
    padding = "=" * ((4 - len(value) % 4) % 4)
    decoded = base64.urlsafe_b64decode(value + padding)
    if _base64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _capability_unauthorized() -> ProjectInvocationError:
    return ProjectInvocationError(
        "PROJECT_CAPABILITY_UNAUTHORIZED",
        "The project invocation capability is invalid or expired.",
        status=401,
    )


def _validate_actions(value: Any, *, capability: bool = False) -> list[str]:
    if type(value) is not list or not value:
        if capability:
            raise _capability_unauthorized()
        _fail("PROJECT_INVOCATION_REQUEST_INVALID", "actions must be a non-empty array.")
    if any(type(item) is not str for item in value):
        if capability:
            raise _capability_unauthorized()
        _fail("PROJECT_INVOCATION_REQUEST_INVALID", "actions are invalid.")
    if (
        len(value) != len(set(value))
        or value != sorted(value)
        or any(item not in SUPPORTED_PROJECT_INVOCATION_ACTIONS for item in value)
    ):
        if capability:
            raise _capability_unauthorized()
        _fail("PROJECT_INVOCATION_REQUEST_INVALID", "actions are invalid.")
    return list(value)


@dataclass(frozen=True)
class ProjectCapabilityClaims:
    audience: str
    caller_id: str
    project_id: str
    room_id: str
    actions: tuple[str, ...]
    client_request_id: str
    request_sha256: str
    issued_at: int
    expires_at: int
    token_id: str


class ProjectCapabilityAuthorizer:
    """Mint and verify an invocation-specific HMAC bearer capability."""

    def __init__(
        self,
        signing_secret: str | bytes,
        *,
        clock: Callable[[], float] = time.time,
        max_ttl_seconds: int = MAX_PROJECT_CAPABILITY_TTL_SECONDS,
    ) -> None:
        if type(signing_secret) is str:
            secret = signing_secret.encode("utf-8")
        elif type(signing_secret) is bytes:
            secret = signing_secret
        else:
            _fail(
                "PROJECT_CAPABILITY_SECRET_INVALID",
                "The project capability signing secret must be native text or bytes.",
                status=500,
            )
        if len(secret) < 32:
            _fail(
                "PROJECT_CAPABILITY_SECRET_INVALID",
                "The project capability signing secret must contain at least 32 bytes.",
                status=500,
            )
        if type(max_ttl_seconds) is not int or not 1 <= max_ttl_seconds <= 3_600:
            _fail(
                "PROJECT_CAPABILITY_TTL_INVALID",
                "The maximum project capability TTL is invalid.",
                status=500,
            )
        self._secret = secret
        self._clock = clock
        self.max_ttl_seconds = max_ttl_seconds

    def _now(self) -> int:
        value = self._clock()
        if type(value) not in {int, float} or isinstance(value, bool):
            _fail(
                "PROJECT_CAPABILITY_CLOCK_INVALID",
                "The project capability clock is invalid.",
                status=500,
            )
        if not math.isfinite(value) or value < 0:
            _fail(
                "PROJECT_CAPABILITY_CLOCK_INVALID",
                "The project capability clock is invalid.",
                status=500,
            )
        return int(value)

    def mint(
        self,
        *,
        caller_id: Any,
        project_id: Any,
        room_id: Any,
        actions: Any,
        client_request_id: Any,
        request_sha256: Any,
        ttl_seconds: int = DEFAULT_PROJECT_CAPABILITY_TTL_SECONDS,
    ) -> str:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= self.max_ttl_seconds:
            _fail(
                "PROJECT_CAPABILITY_TTL_INVALID",
                f"ttl_seconds must be between 1 and {self.max_ttl_seconds}.",
            )
        clean_actions = _validate_actions(actions)
        issued_at = self._now()
        payload = {
            "version": PROJECT_CAPABILITY_VERSION,
            "aud": PROJECT_CAPABILITY_AUDIENCE,
            "caller_id": _require_identifier(caller_id, "caller_id", maximum=80),
            "project_id": _require_identifier(project_id, "project_id", maximum=160),
            "room_id": _require_identifier(room_id, "room_id", maximum=80),
            "actions": clean_actions,
            "client_request_id": _require_identifier(
                client_request_id,
                "client_request_id",
                maximum=160,
            ),
            "request_sha256": _require_sha256(request_sha256, "request_sha256"),
            "iat": issued_at,
            "exp": issued_at + ttl_seconds,
            "jti": secrets.token_urlsafe(18),
        }
        encoded_payload = _base64url_encode(_canonical_json_bytes(payload))
        signature = hmac.new(
            self._secret,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded_payload}.{_base64url_encode(signature)}"

    def authorize(
        self,
        token: Any,
        *,
        caller_id: Any | None = None,
        project_id: Any | None = None,
        room_id: Any | None = None,
        action: Any | None = None,
        client_request_id: Any | None = None,
        request_sha256: Any | None = None,
    ) -> ProjectCapabilityClaims:
        unauthorized = _capability_unauthorized()
        if type(token) is not str:
            raise unauthorized
        try:
            token_bytes = token.encode("ascii")
        except UnicodeEncodeError:
            raise unauthorized from None
        if (
            not token
            or len(token_bytes) > MAX_PROJECT_CAPABILITY_TOKEN_BYTES
            or token.count(".") != 1
        ):
            raise unauthorized
        encoded_payload, encoded_signature = token.split(".", 1)
        try:
            supplied_signature = _base64url_decode(encoded_signature)
            if len(supplied_signature) != hashlib.sha256().digest_size:
                raise unauthorized
            expected_signature = hmac.new(
                self._secret,
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise unauthorized
            raw_payload = _base64url_decode(encoded_payload)
            payload = _strict_json_loads(raw_payload)
            if type(payload) is not dict or set(payload) != _CAPABILITY_FIELDS:
                raise unauthorized
            if any(type(key) is not str for key in payload):
                raise unauthorized
            _validate_exact_json(payload)
            if raw_payload != _canonical_json_bytes(payload):
                raise unauthorized
            if payload["version"] != PROJECT_CAPABILITY_VERSION:
                raise unauthorized
            if type(payload["aud"]) is not str or payload["aud"] != PROJECT_CAPABILITY_AUDIENCE:
                raise unauthorized
            payload_caller_id = _require_identifier(
                payload["caller_id"],
                "caller_id",
                maximum=80,
            )
            payload_project_id = _require_identifier(
                payload["project_id"],
                "project_id",
                maximum=160,
            )
            payload_room_id = _require_identifier(
                payload["room_id"],
                "room_id",
                maximum=80,
            )
            payload_actions = _validate_actions(payload["actions"], capability=True)
            payload_request_id = _require_identifier(
                payload["client_request_id"],
                "client_request_id",
                maximum=160,
            )
            payload_request_sha256 = _require_sha256(
                payload["request_sha256"],
                "request_sha256",
            )
            if type(payload["iat"]) is not int or type(payload["exp"]) is not int:
                raise unauthorized
            issued_at = payload["iat"]
            expires_at = payload["exp"]
            token_id = payload["jti"]
            if type(token_id) is not str or _JTI.fullmatch(token_id) is None:
                raise unauthorized
        except ProjectInvocationError:
            raise unauthorized from None
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise unauthorized from None

        now = self._now()
        if (
            issued_at < 0
            or issued_at > now + 5
            or expires_at <= now
            or expires_at <= issued_at
            or expires_at - issued_at > self.max_ttl_seconds
        ):
            raise unauthorized

        expected_values = (
            (caller_id, payload_caller_id, "caller_id", 80),
            (project_id, payload_project_id, "project_id", 160),
            (room_id, payload_room_id, "room_id", 80),
            (client_request_id, payload_request_id, "client_request_id", 160),
        )
        try:
            for expected, actual, label, maximum in expected_values:
                if expected is not None and _require_identifier(
                    expected,
                    label,
                    maximum=maximum,
                ) != actual:
                    raise unauthorized
            if request_sha256 is not None and _require_sha256(
                request_sha256,
                "request_sha256",
            ) != payload_request_sha256:
                raise unauthorized
        except ProjectInvocationError:
            raise unauthorized from None
        if action is not None:
            if type(action) is not str or action not in SUPPORTED_PROJECT_INVOCATION_ACTIONS:
                raise unauthorized
            if action not in payload_actions:
                raise ProjectInvocationError(
                    "PROJECT_CAPABILITY_ACTION_DENIED",
                    "The project invocation capability does not allow this action.",
                    status=403,
                )
        return ProjectCapabilityClaims(
            audience=PROJECT_CAPABILITY_AUDIENCE,
            caller_id=payload_caller_id,
            project_id=payload_project_id,
            room_id=payload_room_id,
            actions=tuple(payload_actions),
            client_request_id=payload_request_id,
            request_sha256=payload_request_sha256,
            issued_at=issued_at,
            expires_at=expires_at,
            token_id=token_id,
        )


def derive_project_invocation_room_id(
    caller_id: Any,
    project_id: Any,
    client_request_id: Any,
) -> str:
    binding = {
        "version": PROJECT_INVOCATION_ENVELOPE_VERSION,
        "caller_id": _require_identifier(caller_id, "caller_id", maximum=80),
        "project_id": _require_identifier(project_id, "project_id", maximum=160),
        "client_request_id": _require_identifier(
            client_request_id,
            "client_request_id",
            maximum=160,
        ),
    }
    return f"room_inv_{_canonical_sha256(binding)}"


def _normalize_string_list(
    value: Any,
    label: str,
    *,
    maximum_items: int,
) -> list[str]:
    if type(value) is not list or len(value) > maximum_items:
        _fail("PROJECT_INVOCATION_REQUEST_INVALID", f"{label} is invalid.")
    normalized = [
        _require_text(item, f"{label} item", maximum=80, pattern=_SLUG)
        for item in value
    ]
    if len(normalized) != len(set(normalized)) or normalized != sorted(normalized):
        _fail(
            "PROJECT_INVOCATION_REQUEST_INVALID",
            f"{label} must be sorted and unique.",
        )
    return normalized


def _normalize_envelope_without_request_hash(value: Any) -> dict[str, Any]:
    raw = _require_exact_mapping(
        value,
        _UNSEALED_ENVELOPE_FIELDS,
        "project invocation envelope",
    )
    if raw["version"] != PROJECT_INVOCATION_ENVELOPE_VERSION:
        _fail(
            "PROJECT_INVOCATION_SCHEMA_UNSUPPORTED",
            "The project invocation envelope version is unsupported.",
        )
    caller_id = _require_identifier(raw["caller_id"], "caller_id", maximum=80)
    project_id = _require_identifier(raw["project_id"], "project_id", maximum=160)
    client_request_id = _require_identifier(
        raw["client_request_id"],
        "client_request_id",
        maximum=160,
    )
    room_id = _require_identifier(raw["room_id"], "room_id", maximum=80)
    expected_room_id = derive_project_invocation_room_id(
        caller_id,
        project_id,
        client_request_id,
    )
    if room_id != expected_room_id:
        _fail(
            "PROJECT_INVOCATION_ROOM_BINDING_INVALID",
            "The project invocation room id does not match its source identity.",
        )

    source = _require_exact_mapping(raw["source"], _SOURCE_FIELDS, "source")
    normalized_source = {
        "item_id": _require_identifier(source["item_id"], "source.item_id", maximum=160),
        "revision": _require_text(source["revision"], "source.revision", maximum=160),
    }

    workflow_kind = _require_text(
        raw["workflow_kind"],
        "workflow_kind",
        maximum=40,
        pattern=_SLUG,
    )
    if workflow_kind not in SUPPORTED_WORKFLOW_RESULT_PROFILES:
        _fail(
            "PROJECT_INVOCATION_WORKFLOW_UNSUPPORTED",
            "The project invocation workflow kind is unsupported.",
        )
    result_profile = _require_text(
        raw["result_profile"],
        "result_profile",
        maximum=80,
        pattern=_SLUG,
    )
    if SUPPORTED_WORKFLOW_RESULT_PROFILES[workflow_kind] != result_profile:
        _fail(
            "PROJECT_INVOCATION_RESULT_PROFILE_INVALID",
            "The result profile is incompatible with the workflow kind.",
        )

    room_spec = _require_exact_mapping(
        raw["room_spec"],
        _ROOM_SPEC_FIELDS,
        "room_spec",
    )
    normalized_room_spec = {
        "title": _require_text(room_spec["title"], "room_spec.title", maximum=80),
        "objective": _require_text(
            room_spec["objective"],
            "room_spec.objective",
            maximum=2_000,
        ),
        "domain": _require_text(
            room_spec["domain"],
            "room_spec.domain",
            maximum=60,
            pattern=_SLUG,
        ),
        "category": _require_text(
            room_spec["category"],
            "room_spec.category",
            maximum=80,
        ),
        "template_id": _require_text(
            room_spec["template_id"],
            "room_spec.template_id",
            maximum=80,
            pattern=_SLUG,
        ),
        "capability_pack_ids": _normalize_string_list(
            room_spec["capability_pack_ids"],
            "room_spec.capability_pack_ids",
            maximum_items=12,
        ),
    }

    domain_context = _require_exact_mapping(
        raw["domain_context"],
        _DOMAIN_CONTEXT_FIELDS,
        "domain_context",
    )
    normalized_domain_context = {
        "schema_version": _require_text(
            domain_context["schema_version"],
            "domain_context.schema_version",
            maximum=80,
            pattern=_SLUG,
        ),
        "schema_sha256": _require_sha256(
            domain_context["schema_sha256"],
            "domain_context.schema_sha256",
        ),
        "payload_sha256": _require_sha256(
            domain_context["payload_sha256"],
            "domain_context.payload_sha256",
        ),
    }

    input_manifest = _require_exact_mapping(
        raw["input_manifest"],
        _INPUT_MANIFEST_FIELDS,
        "input_manifest",
    )
    normalized_input_manifest = {
        "content_sha256": _require_sha256(
            input_manifest["content_sha256"],
            "input_manifest.content_sha256",
        ),
        "content_bytes": _require_integer(
            input_manifest["content_bytes"],
            "input_manifest.content_bytes",
            minimum=0,
            maximum=10_000_000,
        ),
    }

    data_handling = _require_exact_mapping(
        raw["data_handling"],
        _DATA_HANDLING_FIELDS,
        "data_handling",
    )
    classification = _require_text(
        data_handling["classification"],
        "data_handling.classification",
        maximum=40,
        pattern=_SLUG,
    )
    retention_policy = _require_text(
        data_handling["retention_policy"],
        "data_handling.retention_policy",
        maximum=40,
        pattern=_SLUG,
    )
    if classification not in SUPPORTED_DATA_CLASSIFICATIONS:
        _fail(
            "PROJECT_INVOCATION_DATA_CLASSIFICATION_INVALID",
            "The project invocation data classification is unsupported.",
        )
    if retention_policy not in SUPPORTED_RETENTION_POLICIES:
        _fail(
            "PROJECT_INVOCATION_RETENTION_INVALID",
            "The project invocation retention policy is unsupported.",
        )
    retention_days = data_handling["retention_days"]
    if retention_policy == "bounded_days":
        retention_days = _require_integer(
            retention_days,
            "data_handling.retention_days",
            minimum=1,
            maximum=365,
        )
    elif retention_days is not None:
        _fail(
            "PROJECT_INVOCATION_RETENTION_INVALID",
            "retention_days is only valid for bounded retention.",
        )
    if classification in SENSITIVE_DATA_CLASSIFICATIONS:
        if retention_policy == "project_default":
            _fail(
                "PROJECT_INVOCATION_SENSITIVE_RETENTION_REQUIRED",
                "Sensitive project data requires an explicit minimal retention policy.",
            )
        if retention_policy == "bounded_days" and retention_days > 30:
            _fail(
                "PROJECT_INVOCATION_SENSITIVE_RETENTION_REQUIRED",
                "Sensitive project data may be retained for at most 30 days.",
            )
    normalized_data_handling = {
        "classification": classification,
        "retention_policy": retention_policy,
        "retention_days": retention_days,
    }

    budget = _require_exact_mapping(raw["budget"], _BUDGET_FIELDS, "budget")
    normalized_budget = {
        "max_provider_calls": _require_integer(
            budget["max_provider_calls"],
            "budget.max_provider_calls",
            minimum=0,
            maximum=100,
        ),
        "max_context_bytes": _require_integer(
            budget["max_context_bytes"],
            "budget.max_context_bytes",
            minimum=1,
            maximum=10_000_000,
        ),
        "max_result_bytes": _require_integer(
            budget["max_result_bytes"],
            "budget.max_result_bytes",
            minimum=1,
            maximum=10_000_000,
        ),
    }

    confirmation = _require_exact_mapping(
        raw["user_confirmation"],
        _USER_CONFIRMATION_FIELDS,
        "user_confirmation",
    )
    if type(confirmation["required"]) is not bool or confirmation["required"] is not True:
        _fail(
            "PROJECT_INVOCATION_USER_CONFIRMATION_REQUIRED",
            "Project invocation creation requires explicit user confirmation.",
        )
    if confirmation["boundary"] != "before_room_creation":
        _fail(
            "PROJECT_INVOCATION_USER_CONFIRMATION_REQUIRED",
            "The user confirmation boundary is invalid.",
        )
    normalized_confirmation = {
        "required": True,
        "boundary": "before_room_creation",
    }

    safety = _require_exact_mapping(raw["safety"], _SAFETY_FIELDS, "safety")
    if (
        type(safety["execution_capability"]) is not str
        or safety["execution_capability"] != "none"
        or type(safety["live_trading_allowed"]) is not bool
        or safety["live_trading_allowed"] is not False
        or type(safety["can_autonomously_decide"]) is not bool
        or safety["can_autonomously_decide"] is not False
    ):
        _fail(
            "PROJECT_INVOCATION_EXECUTION_FORBIDDEN",
            "Project invocations cannot grant execution or autonomous authority.",
            status=403,
        )
    normalized_safety = {
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }

    normalized = {
        "version": PROJECT_INVOCATION_ENVELOPE_VERSION,
        "caller_id": caller_id,
        "project_id": project_id,
        "client_request_id": client_request_id,
        "room_id": room_id,
        "source": normalized_source,
        "workflow_kind": workflow_kind,
        "result_profile": result_profile,
        "room_spec": normalized_room_spec,
        "domain_context": normalized_domain_context,
        "input_manifest": normalized_input_manifest,
        "data_handling": normalized_data_handling,
        "budget": normalized_budget,
        "user_confirmation": normalized_confirmation,
        "safety": normalized_safety,
    }
    _validate_exact_json(normalized)
    return normalized


def seal_project_invocation_envelope(value: Any) -> dict[str, Any]:
    """Normalize an unsealed envelope and add its deterministic request hash."""

    normalized = _normalize_envelope_without_request_hash(value)
    request_sha256 = _canonical_sha256(normalized)
    return {
        **normalized,
        "request_sha256": request_sha256,
    }


def normalize_project_invocation_envelope(value: Any) -> dict[str, Any]:
    """Validate a closed v1 envelope and its self-excluding request hash."""

    raw = _require_exact_mapping(
        value,
        _ENVELOPE_FIELDS,
        "project invocation envelope",
    )
    supplied_sha256 = _require_sha256(raw["request_sha256"], "request_sha256")
    unsealed = {key: raw[key] for key in raw if key != "request_sha256"}
    normalized = _normalize_envelope_without_request_hash(unsealed)
    expected_sha256 = _canonical_sha256(normalized)
    if not hmac.compare_digest(supplied_sha256, expected_sha256):
        _fail(
            "PROJECT_INVOCATION_REQUEST_HASH_MISMATCH",
            "The project invocation request hash does not match its envelope.",
            status=409,
        )
    return {
        **normalized,
        "request_sha256": expected_sha256,
    }


def project_invocation_request_sha256(value: Any) -> str:
    """Return the canonical request hash, excluding ``request_sha256`` itself."""

    if type(value) is not dict:
        _fail(
            "PROJECT_INVOCATION_REQUEST_INVALID",
            "project invocation envelope must be an object.",
        )
    if set(value) == _ENVELOPE_FIELDS:
        unsealed = {key: value[key] for key in value if key != "request_sha256"}
    elif set(value) == _UNSEALED_ENVELOPE_FIELDS:
        unsealed = dict(value)
    else:
        _fail(
            "PROJECT_INVOCATION_REQUEST_INVALID",
            "project invocation envelope fields are incomplete or unsupported.",
        )
    return _canonical_sha256(_normalize_envelope_without_request_hash(unsealed))


def project_invocation_semantics(value: Any) -> dict[str, Any]:
    """Return immutable semantics without retaining title/objective plaintext."""

    envelope = normalize_project_invocation_envelope(value)
    room_spec = envelope["room_spec"]
    input_manifest = envelope["input_manifest"]
    return {
        "version": PROJECT_INVOCATION_SEMANTICS_VERSION,
        "envelope_version": PROJECT_INVOCATION_ENVELOPE_VERSION,
        "caller_id": envelope["caller_id"],
        "project_id": envelope["project_id"],
        "client_request_id": envelope["client_request_id"],
        "request_sha256": envelope["request_sha256"],
        "room_id": envelope["room_id"],
        "source": {
            **envelope["source"],
            "content_sha256": input_manifest["content_sha256"],
        },
        "workflow_kind": envelope["workflow_kind"],
        "result_profile": envelope["result_profile"],
        "room_spec": {
            "title_sha256": hashlib.sha256(
                room_spec["title"].encode("utf-8")
            ).hexdigest(),
            "title_characters": len(room_spec["title"]),
            "objective_sha256": hashlib.sha256(
                room_spec["objective"].encode("utf-8")
            ).hexdigest(),
            "objective_characters": len(room_spec["objective"]),
            "domain": room_spec["domain"],
            "category": room_spec["category"],
            "template_id": room_spec["template_id"],
            "capability_pack_ids": list(room_spec["capability_pack_ids"]),
        },
        "domain_context": dict(envelope["domain_context"]),
        "input_manifest": dict(input_manifest),
        "data_handling": dict(envelope["data_handling"]),
        "budget": dict(envelope["budget"]),
        "user_confirmation": dict(envelope["user_confirmation"]),
        "safety": dict(envelope["safety"]),
    }


def project_invocation_semantics_sha256(value: Any) -> str:
    return _canonical_sha256(project_invocation_semantics(value))


def project_invocation_retention_contract(value: Any) -> dict[str, Any]:
    """Project one normalized policy into an enforceable retention decision."""

    envelope = normalize_project_invocation_envelope(value)
    handling = envelope["data_handling"]
    policy = handling["retention_policy"]
    if policy == "no_payload_retention":
        max_seconds: int | None = 0
        payload_retention_allowed = False
    elif policy == "ephemeral_24h":
        max_seconds = 86_400
        payload_retention_allowed = True
    elif policy == "bounded_days":
        max_seconds = int(handling["retention_days"]) * 86_400
        payload_retention_allowed = True
    else:
        max_seconds = None
        payload_retention_allowed = True
    return {
        "classification": handling["classification"],
        "retention_policy": policy,
        "payload_retention_allowed": payload_retention_allowed,
        "max_retention_seconds": max_seconds,
        "sensitive": handling["classification"] in SENSITIVE_DATA_CLASSIFICATIONS,
    }


# Compact aliases for later Store/HTTP integration without importing MCP names.
CapabilityAuthorizer = ProjectCapabilityAuthorizer
CapabilityClaims = ProjectCapabilityClaims
derive_room_id = derive_project_invocation_room_id


__all__ = [
    "CapabilityAuthorizer",
    "CapabilityClaims",
    "DEFAULT_PROJECT_CAPABILITY_TTL_SECONDS",
    "MAX_PROJECT_CAPABILITY_TTL_SECONDS",
    "PROJECT_CAPABILITY_AUDIENCE",
    "PROJECT_CAPABILITY_VERSION",
    "PROJECT_INVOCATION_ACTION_INTAKE",
    "PROJECT_INVOCATION_ACTION_RESULT_READ",
    "PROJECT_INVOCATION_INTAKE_PATH",
    "PROJECT_INVOCATION_RESULT_PATH_TEMPLATE",
    "PROJECT_INVOCATION_ENVELOPE_VERSION",
    "PROJECT_INVOCATION_SEMANTICS_VERSION",
    "ProjectCapabilityAuthorizer",
    "ProjectCapabilityClaims",
    "ProjectInvocationError",
    "SENSITIVE_DATA_CLASSIFICATIONS",
    "SUPPORTED_DATA_CLASSIFICATIONS",
    "SUPPORTED_PROJECT_INVOCATION_ACTIONS",
    "SUPPORTED_RETENTION_POLICIES",
    "SUPPORTED_WORKFLOW_RESULT_PROFILES",
    "derive_project_invocation_room_id",
    "derive_room_id",
    "normalize_project_invocation_envelope",
    "project_invocation_request_sha256",
    "project_invocation_retention_contract",
    "project_invocation_semantics",
    "project_invocation_semantics_sha256",
    "seal_project_invocation_envelope",
]
