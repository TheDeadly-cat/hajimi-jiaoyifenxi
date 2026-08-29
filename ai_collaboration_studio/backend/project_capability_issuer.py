"""Offline operator issuer for scoped project invocation capabilities.

This module deliberately reuses :mod:`backend.project_invocation` for the
envelope and bearer token contracts.  It does not expose an HTTP endpoint,
open SQLite, start a service, or contact a Provider or market source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .path_identity import first_reparse_component
from .project_invocation import (
    DEFAULT_PROJECT_CAPABILITY_TTL_SECONDS,
    MAX_PROJECT_CAPABILITY_TTL_SECONDS,
    PROJECT_CAPABILITY_AUDIENCE,
    PROJECT_CAPABILITY_VERSION,
    PROJECT_INVOCATION_ACTION_INTAKE,
    PROJECT_INVOCATION_ACTION_RESULT_READ,
    SUPPORTED_PROJECT_INVOCATION_ACTIONS,
    ProjectCapabilityAuthorizer,
    ProjectInvocationError,
    derive_project_invocation_room_id,
    normalize_project_invocation_envelope,
)


PROJECT_CAPABILITY_ISSUER_POLICY_VERSION = (
    "project_capability_issuer_policy_v1"
)
PROJECT_CAPABILITY_ISSUER_INSPECTION_VERSION = (
    "project_capability_issuer_inspection_v1"
)
PROJECT_CAPABILITY_ISSUER_PLAN_VERSION = "project_capability_issuer_plan_v1"
PROJECT_CAPABILITY_ISSUANCE_RECEIPT_VERSION = (
    "project_capability_issuance_receipt_v1"
)
PROJECT_CAPABILITY_ISSUANCE_OUTPUT_VERSION = (
    "project_capability_issuance_output_v1"
)
PROJECT_CAPABILITY_ISSUANCE_ACKNOWLEDGEMENT = (
    "APPROVE_PROJECT_INVOCATION_CAPABILITY"
)
PROJECT_CAPABILITY_SIGNING_SECRET_ENV = (
    "AI_STUDIO_PROJECT_CAPABILITY_SIGNING_SECRET"
)

MAX_ISSUER_ENVELOPE_BYTES = 256_000
MAX_ISSUER_POLICY_BYTES = 128_000
MAX_ISSUER_PROJECTS = 256
MAX_SECRET_INPUT_BYTES = 4_096
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_POLICY_FIELDS = frozenset({"version", "projects"})
_PROJECT_POLICY_FIELDS = frozenset({
    "caller_id",
    "project_id",
    "enabled",
    "allowed_actions",
    "max_ttl_seconds",
})
_ACTION_OPERATION = {
    PROJECT_INVOCATION_ACTION_INTAKE: "intake",
    PROJECT_INVOCATION_ACTION_RESULT_READ: "result_read",
}


class ProjectCapabilityIssuerError(ValueError):
    """A local issuer input or policy failed closed."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(code: str, message: str) -> None:
    raise ProjectCapabilityIssuerError(message, code=code)


def _strict_json_loads(raw: bytes) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )


def _validate_exact_json(value: Any, *, path: str = "$") -> None:
    value_type = type(value)
    if value is None or value_type in {str, bool, int}:
        return
    if value_type is float:
        if math.isfinite(value):
            _fail("PROJECT_CAPABILITY_ISSUER_JSON_INVALID", f"{path} must not use floats.")
        _fail("PROJECT_CAPABILITY_ISSUER_JSON_INVALID", f"{path} is non-finite.")
    if value_type is list:
        for index, item in enumerate(value):
            _validate_exact_json(item, path=f"{path}[{index}]")
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail(
                    "PROJECT_CAPABILITY_ISSUER_JSON_INVALID",
                    f"{path} contains a non-string key.",
                )
            _validate_exact_json(item, path=f"{path}.{key}")
        return
    _fail(
        "PROJECT_CAPABILITY_ISSUER_JSON_INVALID",
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


def _native_text(value: Any, *, path: str, maximum: int = 160) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or value != value.strip()
    ):
        _fail("PROJECT_CAPABILITY_ISSUER_POLICY_INVALID", f"{path} is invalid.")
    return value


def _native_integer(
    value: Any,
    *,
    path: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("PROJECT_CAPABILITY_ISSUER_POLICY_INVALID", f"{path} is invalid.")
    return value


def normalize_project_capability_issuer_policy(value: Any) -> dict[str, Any]:
    """Validate one closed, deterministic local project registration policy."""

    _validate_exact_json(value)
    if type(value) is not dict or set(value) != _POLICY_FIELDS:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_POLICY_INVALID",
            "The issuer policy fields are incomplete or unsupported.",
        )
    if value.get("version") != PROJECT_CAPABILITY_ISSUER_POLICY_VERSION:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_POLICY_INVALID",
            "The issuer policy version is unsupported.",
        )
    projects = value.get("projects")
    if type(projects) is not list or not 1 <= len(projects) <= MAX_ISSUER_PROJECTS:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_POLICY_INVALID",
            "The issuer policy must contain a bounded non-empty project list.",
        )

    normalized_projects: list[dict[str, Any]] = []
    identities: list[tuple[str, str]] = []
    for index, item in enumerate(projects):
        path = f"$.projects[{index}]"
        if type(item) is not dict or set(item) != _PROJECT_POLICY_FIELDS:
            _fail(
                "PROJECT_CAPABILITY_ISSUER_POLICY_INVALID",
                f"{path} fields are incomplete or unsupported.",
            )
        caller_id = _native_text(
            item.get("caller_id"),
            path=f"{path}.caller_id",
            maximum=80,
        )
        project_id = _native_text(
            item.get("project_id"),
            path=f"{path}.project_id",
            maximum=160,
        )
        try:
            # Reuse the invocation contract's exact identifier validation
            # instead of maintaining a second caller/project grammar here.
            derive_project_invocation_room_id(
                caller_id,
                project_id,
                "issuer-policy-validation",
            )
        except ProjectInvocationError:
            _fail(
                "PROJECT_CAPABILITY_ISSUER_POLICY_INVALID",
                f"{path} caller/project identity is invalid.",
            )
        if type(item.get("enabled")) is not bool:
            _fail(
                "PROJECT_CAPABILITY_ISSUER_POLICY_INVALID",
                f"{path}.enabled must be a boolean.",
            )
        actions = item.get("allowed_actions")
        if (
            type(actions) is not list
            or not actions
            or any(type(action) is not str for action in actions)
            or actions != sorted(actions)
            or len(actions) != len(set(actions))
            or any(action not in SUPPORTED_PROJECT_INVOCATION_ACTIONS for action in actions)
        ):
            _fail(
                "PROJECT_CAPABILITY_ISSUER_POLICY_INVALID",
                f"{path}.allowed_actions must be sorted, unique, and supported.",
            )
        max_ttl_seconds = _native_integer(
            item.get("max_ttl_seconds"),
            path=f"{path}.max_ttl_seconds",
            minimum=1,
            maximum=MAX_PROJECT_CAPABILITY_TTL_SECONDS,
        )
        identities.append((caller_id, project_id))
        normalized_projects.append({
            "caller_id": caller_id,
            "project_id": project_id,
            "enabled": item["enabled"],
            "allowed_actions": list(actions),
            "max_ttl_seconds": max_ttl_seconds,
        })

    if identities != sorted(identities) or len(identities) != len(set(identities)):
        _fail(
            "PROJECT_CAPABILITY_ISSUER_POLICY_INVALID",
            "Issuer project registrations must be sorted and unique.",
        )
    return {
        "version": PROJECT_CAPABILITY_ISSUER_POLICY_VERSION,
        "projects": normalized_projects,
    }


def _registered_project(
    envelope: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    identity = (envelope["caller_id"], envelope["project_id"])
    for project in policy["projects"]:
        if (project["caller_id"], project["project_id"]) != identity:
            continue
        if project["enabled"] is not True:
            _fail(
                "PROJECT_CAPABILITY_ISSUER_PROJECT_DISABLED",
                "The requested project registration is disabled.",
            )
        return project
    _fail(
        "PROJECT_CAPABILITY_ISSUER_PROJECT_UNKNOWN",
        "The envelope caller and project are not registered in the issuer policy.",
    )


def inspect_project_capability_request(
    envelope_value: Any,
    policy_value: Any,
) -> dict[str, Any]:
    """Inspect a sealed envelope against local policy without reading a secret."""

    envelope = normalize_project_invocation_envelope(envelope_value)
    policy = normalize_project_capability_issuer_policy(policy_value)
    project = _registered_project(envelope, policy)
    report = {
        "version": PROJECT_CAPABILITY_ISSUER_INSPECTION_VERSION,
        "status": "validated",
        "envelope": {
            "version": envelope["version"],
            "caller_id": envelope["caller_id"],
            "project_id": envelope["project_id"],
            "client_request_id": envelope["client_request_id"],
            "request_sha256": envelope["request_sha256"],
            "room_id": envelope["room_id"],
            "source_item_id": envelope["source"]["item_id"],
            "source_revision": envelope["source"]["revision"],
            "workflow_kind": envelope["workflow_kind"],
            "result_profile": envelope["result_profile"],
            "data_classification": envelope["data_handling"]["classification"],
            "retention_policy": envelope["data_handling"]["retention_policy"],
            "max_provider_calls": envelope["budget"]["max_provider_calls"],
            "user_confirmation_required": envelope["user_confirmation"]["required"],
            "user_confirmation_boundary": envelope["user_confirmation"]["boundary"],
            "execution_capability": envelope["safety"]["execution_capability"],
            "live_trading_allowed": envelope["safety"]["live_trading_allowed"],
            "can_autonomously_decide": envelope["safety"]["can_autonomously_decide"],
        },
        "registration": {
            "enabled": True,
            "allowed_actions": list(project["allowed_actions"]),
            "max_ttl_seconds": project["max_ttl_seconds"],
        },
        "policy_sha256": _canonical_sha256(policy),
        "secret_read": False,
        "token_minted": False,
        "network_calls_performed": 0,
        "database_reads_performed": 0,
        "database_writes_performed": 0,
        "provider_calls_performed": 0,
        "market_calls_performed": 0,
    }
    report["inspection_sha256"] = _canonical_sha256(report)
    return report


def plan_project_capability_issuance(
    envelope_value: Any,
    policy_value: Any,
    *,
    action: Any,
    ttl_seconds: Any = DEFAULT_PROJECT_CAPABILITY_TTL_SECONDS,
) -> dict[str, Any]:
    """Build a secret-free issuance plan for exactly one existing action."""

    inspection = inspect_project_capability_request(envelope_value, policy_value)
    if type(action) is not str or action not in SUPPORTED_PROJECT_INVOCATION_ACTIONS:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_ACTION_UNKNOWN",
            "The requested project invocation action is unsupported.",
        )
    if action not in inspection["registration"]["allowed_actions"]:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_ACTION_DENIED",
            "The requested action is not allowed for this registered project.",
        )
    if (
        type(ttl_seconds) is not int
        or not 1 <= ttl_seconds <= inspection["registration"]["max_ttl_seconds"]
    ):
        _fail(
            "PROJECT_CAPABILITY_ISSUER_TTL_DENIED",
            "The requested TTL exceeds the registered project limit.",
        )
    envelope = inspection["envelope"]
    plan = {
        "version": PROJECT_CAPABILITY_ISSUER_PLAN_VERSION,
        "status": "ready_to_mint",
        "capability_version": PROJECT_CAPABILITY_VERSION,
        "audience": PROJECT_CAPABILITY_AUDIENCE,
        "operation": _ACTION_OPERATION[action],
        "action": action,
        "ttl_seconds": ttl_seconds,
        "caller_id": envelope["caller_id"],
        "project_id": envelope["project_id"],
        "room_id": envelope["room_id"],
        "client_request_id": envelope["client_request_id"],
        "request_sha256": envelope["request_sha256"],
        "policy_sha256": inspection["policy_sha256"],
        "scope": {
            "single_action": True,
            "invocation_bound": True,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        },
        "replay_semantics": {
            "unique_jti_per_mint": True,
            "single_use_consumption_enforced_by_host": False,
            "intake_replay_is_idempotent": action == PROJECT_INVOCATION_ACTION_INTAKE,
            "result_read_may_repeat_until_expiry": (
                action == PROJECT_INVOCATION_ACTION_RESULT_READ
            ),
        },
        "secret_read": False,
        "token_minted": False,
        "network_calls_performed": 0,
        "database_reads_performed": 0,
        "database_writes_performed": 0,
        "provider_calls_performed": 0,
        "market_calls_performed": 0,
    }
    plan["plan_sha256"] = _canonical_sha256(plan)
    return plan


def issue_project_capability(
    envelope_value: Any,
    policy_value: Any,
    *,
    action: Any,
    ttl_seconds: Any,
    signing_secret: str | bytes,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Mint one invocation-bound, single-action capability and safe receipt."""

    plan = plan_project_capability_issuance(
        envelope_value,
        policy_value,
        action=action,
        ttl_seconds=ttl_seconds,
    )
    authorizer = ProjectCapabilityAuthorizer(
        signing_secret,
        clock=clock,
        max_ttl_seconds=MAX_PROJECT_CAPABILITY_TTL_SECONDS,
    )
    token = authorizer.mint(
        caller_id=plan["caller_id"],
        project_id=plan["project_id"],
        room_id=plan["room_id"],
        actions=[plan["action"]],
        client_request_id=plan["client_request_id"],
        request_sha256=plan["request_sha256"],
        ttl_seconds=plan["ttl_seconds"],
    )
    claims = authorizer.authorize(
        token,
        caller_id=plan["caller_id"],
        project_id=plan["project_id"],
        room_id=plan["room_id"],
        action=plan["action"],
        client_request_id=plan["client_request_id"],
        request_sha256=plan["request_sha256"],
    )
    receipt = {
        "version": PROJECT_CAPABILITY_ISSUANCE_RECEIPT_VERSION,
        "status": "issued",
        "capability_version": PROJECT_CAPABILITY_VERSION,
        "audience": PROJECT_CAPABILITY_AUDIENCE,
        "operation": plan["operation"],
        "action": plan["action"],
        "ttl_seconds": plan["ttl_seconds"],
        "caller_id": claims.caller_id,
        "project_id": claims.project_id,
        "room_id": claims.room_id,
        "client_request_id": claims.client_request_id,
        "request_sha256": claims.request_sha256,
        "policy_sha256": plan["policy_sha256"],
        "plan_sha256": plan["plan_sha256"],
        "issued_at": claims.issued_at,
        "expires_at": claims.expires_at,
        "token_id_sha256": hashlib.sha256(
            claims.token_id.encode("ascii")
        ).hexdigest(),
        "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "scope": dict(plan["scope"]),
        "replay_semantics": dict(plan["replay_semantics"]),
        "signing_secret_in_receipt": False,
        "bearer_token_in_receipt": False,
        "network_calls_performed": 0,
        "database_reads_performed": 0,
        "database_writes_performed": 0,
        "provider_calls_performed": 0,
        "market_calls_performed": 0,
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    return {
        "version": PROJECT_CAPABILITY_ISSUANCE_OUTPUT_VERSION,
        "token": token,
        "receipt": receipt,
    }


def _read_json_file(path: str | Path, *, maximum_bytes: int, label: str) -> Any:
    requested = Path(path).expanduser()
    if first_reparse_component(requested) is not None:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_PATH_UNSAFE",
            f"The {label} path contains a reparse point.",
        )
    try:
        clean_path = requested.resolve()
        if not clean_path.is_file():
            _fail(
                "PROJECT_CAPABILITY_ISSUER_INPUT_MISSING",
                f"The {label} file does not exist.",
            )
        before = clean_path.stat()
        if before.st_size > maximum_bytes:
            _fail(
                "PROJECT_CAPABILITY_ISSUER_INPUT_TOO_LARGE",
                f"The {label} file exceeds the size limit.",
            )
        raw = clean_path.read_bytes()
        parsed = _strict_json_loads(raw)
        after = clean_path.stat()
    except ProjectCapabilityIssuerError:
        raise
    except (
        OSError,
        UnicodeError,
        ValueError,
        RecursionError,
        json.JSONDecodeError,
    ) as exc:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_JSON_INVALID",
            f"The {label} file is not strict UTF-8 JSON.",
        )
        raise AssertionError("unreachable") from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        _fail(
            "PROJECT_CAPABILITY_ISSUER_INPUT_CHANGED",
            f"The {label} file changed while it was read.",
        )
    return parsed


def _secret_from_environment() -> str:
    value = os.environ.get(PROJECT_CAPABILITY_SIGNING_SECRET_ENV, "")
    if type(value) is not str or not value:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_SECRET_MISSING",
            f"{PROJECT_CAPABILITY_SIGNING_SECRET_ENV} is not set.",
        )
    return value


def _secret_from_stdin() -> str:
    stream = sys.stdin.buffer
    if stream.isatty():
        _fail(
            "PROJECT_CAPABILITY_ISSUER_SECRET_INPUT_INVALID",
            "Secret stdin must be redirected from an operator-controlled source.",
        )
    raw = stream.readline(MAX_SECRET_INPUT_BYTES + 1)
    if len(raw) > MAX_SECRET_INPUT_BYTES or not raw.endswith((b"\n", b"\r")):
        _fail(
            "PROJECT_CAPABILITY_ISSUER_SECRET_INPUT_INVALID",
            "The stdin signing secret must be one bounded line.",
        )
    if stream.read(1):
        _fail(
            "PROJECT_CAPABILITY_ISSUER_SECRET_INPUT_INVALID",
            "The stdin signing secret must contain exactly one line.",
        )
    try:
        value = raw.rstrip(b"\r\n").decode("utf-8")
    except UnicodeError:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_SECRET_INPUT_INVALID",
            "The stdin signing secret must be UTF-8 text.",
        )
    if not value:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_SECRET_MISSING",
            "The stdin signing secret is empty.",
        )
    return value


def _write_json_exclusive(path: str | Path, payload: Any) -> None:
    requested = Path(path).expanduser()
    if first_reparse_component(requested) is not None:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_OUTPUT_UNSAFE",
            "The output path contains a reparse point.",
        )
    output = requested.resolve(strict=False)
    try:
        output.relative_to(PROJECT_ROOT)
    except ValueError:
        pass
    else:
        _fail(
            "PROJECT_CAPABILITY_ISSUER_OUTPUT_UNSAFE",
            "Capability output must remain outside the project source tree.",
        )
    if not output.parent.is_dir():
        _fail(
            "PROJECT_CAPABILITY_ISSUER_OUTPUT_UNSAFE",
            "The output parent must be an existing directory.",
        )
    if output.exists():
        _fail(
            "PROJECT_CAPABILITY_ISSUER_OUTPUT_EXISTS",
            "The output file already exists.",
        )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ) + "\n"
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            descriptor = None
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                output.unlink()
            except OSError:
                pass
        raise


def _emit(payload: Any, output: Path | None) -> None:
    if output is None:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return
    _write_json_exclusive(output, payload)


def _add_common_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--envelope", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="Create one new JSON output outside the project source tree.",
    )


def _add_issuance_inputs(parser: argparse.ArgumentParser) -> None:
    _add_common_inputs(parser)
    parser.add_argument("--action", required=True)
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=DEFAULT_PROJECT_CAPABILITY_TTL_SECONDS,
    )


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect or mint one offline, invocation-bound project capability. "
            "No HTTP endpoint, database, Provider, or market access is used."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser("inspect")
    _add_common_inputs(inspect_parser)
    dry_run_parser = commands.add_parser("dry-run")
    _add_issuance_inputs(dry_run_parser)
    mint_parser = commands.add_parser("mint")
    _add_issuance_inputs(mint_parser)
    mint_parser.add_argument(
        "--acknowledgement",
        required=True,
        help=(
            "Exact operator acknowledgement: "
            f"{PROJECT_CAPABILITY_ISSUANCE_ACKNOWLEDGEMENT}"
        ),
    )
    mint_parser.add_argument(
        "--secret-stdin",
        action="store_true",
        help=(
            "Read exactly one secret line from stdin instead of "
            f"{PROJECT_CAPABILITY_SIGNING_SECRET_ENV}."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_cli().parse_args(argv)
    try:
        envelope = _read_json_file(
            arguments.envelope,
            maximum_bytes=MAX_ISSUER_ENVELOPE_BYTES,
            label="envelope",
        )
        policy = _read_json_file(
            arguments.policy,
            maximum_bytes=MAX_ISSUER_POLICY_BYTES,
            label="policy",
        )
        if arguments.command == "inspect":
            payload = inspect_project_capability_request(envelope, policy)
        elif arguments.command == "dry-run":
            payload = plan_project_capability_issuance(
                envelope,
                policy,
                action=arguments.action,
                ttl_seconds=arguments.ttl_seconds,
            )
        else:
            if (
                type(arguments.acknowledgement) is not str
                or arguments.acknowledgement
                != PROJECT_CAPABILITY_ISSUANCE_ACKNOWLEDGEMENT
            ):
                _fail(
                    "PROJECT_CAPABILITY_ISSUER_ACKNOWLEDGEMENT_REQUIRED",
                    "The exact operator acknowledgement is required.",
                )
            signing_secret = (
                _secret_from_stdin()
                if arguments.secret_stdin
                else _secret_from_environment()
            )
            payload = issue_project_capability(
                envelope,
                policy,
                action=arguments.action,
                ttl_seconds=arguments.ttl_seconds,
                signing_secret=signing_secret,
            )
        _emit(payload, arguments.output)
        return 0
    except (ProjectCapabilityIssuerError, ProjectInvocationError) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": str(getattr(exc, "code", "PROJECT_CAPABILITY_ISSUER_FAILED")),
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (OSError, RecursionError):
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "PROJECT_CAPABILITY_ISSUER_IO_FAILED",
                    "message": "The issuer could not safely read or create the requested file.",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_ISSUER_ENVELOPE_BYTES",
    "MAX_ISSUER_POLICY_BYTES",
    "PROJECT_CAPABILITY_ISSUANCE_ACKNOWLEDGEMENT",
    "PROJECT_CAPABILITY_ISSUANCE_OUTPUT_VERSION",
    "PROJECT_CAPABILITY_ISSUANCE_RECEIPT_VERSION",
    "PROJECT_CAPABILITY_ISSUER_INSPECTION_VERSION",
    "PROJECT_CAPABILITY_ISSUER_PLAN_VERSION",
    "PROJECT_CAPABILITY_ISSUER_POLICY_VERSION",
    "ProjectCapabilityIssuerError",
    "inspect_project_capability_request",
    "issue_project_capability",
    "normalize_project_capability_issuer_policy",
    "plan_project_capability_issuance",
]
