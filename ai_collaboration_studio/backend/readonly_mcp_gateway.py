from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .decision_lineage import canonical_sha256
from .path_identity import first_reparse_component


MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-03-26", MCP_PROTOCOL_VERSION})
MCP_SERVER_NAME = "ai-collaboration-studio-readonly"
MCP_SERVER_VERSION = "0.2.0"
MCP_ENDPOINT_PATH = "/mcp"
MCP_CAPABILITY_VERSION = "studio_mcp_capability_v1"
MCP_GATEWAY_PROJECTION_VERSION = "readonly_mcp_projection_v2"
MANUAL_CHATGPT_EVENT_VERSION = "manual_chatgpt_event_v1"
MANUAL_CHATGPT_SESSION_VERSION = "manual_chatgpt_session_v1"
LEGACY_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION = "manual_chatgpt_import_contract_v1"
MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION = "manual_chatgpt_import_contract_v2"
SUPPORTED_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSIONS = frozenset({
    LEGACY_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION,
    MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION,
})
MANUAL_CHATGPT_RESULT_VERSION = "manual_chatgpt_result_v1"
MANUAL_CHATGPT_API_REVIEW_RECORD_VERSION = "manual_chatgpt_api_review_record_v1"
INDEPENDENCE_CLASSIFICATIONS = frozenset({
    "same_answer_multi_role_views",
    "same_model_independent_call",
    "different_provider_independent_opinion",
})

MAX_TOKEN_TTL_SECONDS = 900
DEFAULT_TOKEN_TTL_SECONDS = 300
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
MAX_BUNDLE_BYTES = 128 * 1024
MAX_IMPORT_CONTRACT_BYTES = 96 * 1024
MAX_EVIDENCE_CHUNK_CHARS = 1_600
DEFAULT_RATE_LIMIT_PER_MINUTE = 120
PROTECTED_PORTS = frozenset({8770, 11111})

_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9_-]{1,80}\Z")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/][^\r\n\"'<>|]+"
)
_UNC_PATH_RE = re.compile(r"\\\\[^\r\n\"'<>|]+")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_HTTP_URL_RE = re.compile(r"(?i)https?://[^\s\"'<>]+")
_HIGH_CONFIDENCE_SECRET_RE = re.compile(
    r"(?i)\b(?:sk|rk|pk|api|token)[-_][A-Za-z0-9_-]{16,}\b"
)
_HEADER_VALUE_RE = re.compile(
    r"(?i)\b(?:authorization|cookie|x-api-key|api[-_ ]?key)\s*[:=]\s*[^\s,;]+"
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])/(?:home|Users|var|tmp|etc|opt|root|mnt)/[^\r\n\"'<>|]+"
)
_SENSITIVE_KEYS = frozenset({
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "cookies",
    "database_path",
    "file_path",
    "headers",
    "header",
    "http_headers",
    "id_token",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token_signing_secret",
    "token",
    "signing_key",
    "access_token",
})


class ReadonlyMCPError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "MCP_GATEWAY_INVALID",
        http_status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _bounded_identifier(value: Any, label: str) -> str:
    candidate = str(value or "").strip()
    if not _IDENTIFIER_RE.fullmatch(candidate):
        raise ReadonlyMCPError(
            f"{label} is invalid.",
            code="MCP_ARGUMENT_INVALID",
        )
    return candidate


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strict_json_loads(raw: bytes) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
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
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_constant,
    )


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url")
    padding = "=" * ((4 - len(value) % 4) % 4)
    decoded = base64.urlsafe_b64decode(value + padding)
    if _base64url_encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _loads_object(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _loads_list(raw: Any) -> list[Any]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_list(value: Any) -> list[Any]:
    return copy.deepcopy(value) if isinstance(value, list) else []


def _contract_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _import_contract_version(bundle: Mapping[str, Any]) -> str:
    version = _contract_text(bundle.get("import_contract_version"), 80)
    if version not in SUPPORTED_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSIONS:
        raise ReadonlyMCPError(
            "Frozen bundle import contract version is unsupported.",
            code="MCP_INTEGRITY_FAILED",
            http_status=409,
        )
    return version


def _import_schema_template(bundle: Mapping[str, Any]) -> dict[str, Any]:
    context = bundle.get("context") if isinstance(bundle.get("context"), Mapping) else {}
    roles = _json_list(context.get("roles"))
    role_views = [{
        "role_id": _contract_text(role.get("role_id"), 80),
        "assessment": "",
        "evidence_refs": [],
        "uncertainty": "",
    } for role in roles if isinstance(role, Mapping)]
    budget = bundle.get("budget") if isinstance(bundle.get("budget"), Mapping) else {}
    contract_version = _import_contract_version(bundle)
    panel_calls = int(budget.get("chatgpt_panel_calls") or 0)
    default_panel_independence = (
        "same_model_independent_call"
        if (
            contract_version == MANUAL_CHATGPT_IMPORT_CONTRACT_VERSION
            and panel_calls > 1
        )
        else "same_answer_multi_role_views"
    )
    panels = []
    for index, panel_kind in enumerate(_json_list(budget.get("panel_kinds")), start=1):
        panels.append({
            "panel_id": f"panel_{index}",
            "panel_kind": panel_kind,
            "call_index": index,
            "declared_independence": default_panel_independence,
            "summary": "",
            "conclusion": "",
            "disagreements": [],
            "risks": [],
            "evidence_refs": [],
            "role_views": copy.deepcopy(role_views),
        })
    return {
        "version": MANUAL_CHATGPT_RESULT_VERSION,
        "room_id": _contract_text(bundle.get("room_id"), 80),
        "round_id": _contract_text(bundle.get("round_id"), 80),
        "bundle_sha256": _contract_text(bundle.get("bundle_sha256"), 64),
        "context_sha256": _contract_text(bundle.get("context_sha256"), 64),
        "declared_model": "",
        "panels": panels,
        "final_synthesis": {
            "summary": "",
            "decision_options": [{
                "option_id": "option_1",
                "title": "",
                "rationale": "",
                "evidence_refs": [],
                "risks": [],
            }],
            "recommended_option_id": "",
            "open_questions": [],
            "evidence_refs": [],
        },
    }


def _import_contract(bundle: Mapping[str, Any]) -> dict[str, Any]:
    budget = bundle.get("budget") if isinstance(bundle.get("budget"), Mapping) else {}
    return {
        "version": _import_contract_version(bundle),
        "result_version": MANUAL_CHATGPT_RESULT_VERSION,
        "one_json_object_only": True,
        "markdown_fence_tolerated": True,
        "duplicate_keys_rejected": True,
        "nonfinite_numbers_rejected": True,
        "missing_conclusions_may_be_inferred": False,
        "declared_model_is_trusted": False,
        "allowed_independence_classifications": sorted(INDEPENDENCE_CLASSIFICATIONS),
        "required_panel_kinds": _json_list(budget.get("panel_kinds")),
        "result_template": _import_schema_template(bundle),
    }


def _safe_projected_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "[REDACTED_URL]"
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return "[REDACTED_URL]"
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "[REDACTED_URL]"
    return urlunsplit((parsed.scheme.lower(), f"{hostname}{port}", parsed.path, "", ""))


def _sanitize_string(value: str) -> str:
    candidate = value
    candidate = _HTTP_URL_RE.sub(
        lambda match: _safe_projected_url(match.group(0)),
        candidate,
    )
    candidate = _BEARER_RE.sub("[REDACTED_BEARER]", candidate)
    candidate = _HEADER_VALUE_RE.sub("[REDACTED_HEADER]", candidate)
    candidate = _HIGH_CONFIDENCE_SECRET_RE.sub("[REDACTED_SECRET]", candidate)
    candidate = _WINDOWS_ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", candidate)
    candidate = _UNC_PATH_RE.sub("[REDACTED_PATH]", candidate)
    candidate = _POSIX_ABSOLUTE_PATH_RE.sub("[REDACTED_PATH]", candidate)
    candidate = re.sub(r"(?i)file://[^\s\"'<>]+", "[REDACTED_PATH]", candidate)
    return candidate


def sanitize_gateway_value(value: Any) -> Any:
    """Create a bounded disclosure projection, never a replacement source record."""

    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key.strip().lower() in _SENSITIVE_KEYS:
                continue
            projected[key] = sanitize_gateway_value(item)
        return projected
    if isinstance(value, list):
        return [sanitize_gateway_value(item) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_string(str(value))


@dataclass(frozen=True)
class CapabilityClaims:
    room_id: str
    round_id: str
    issued_at: int
    expires_at: int
    token_id: str


class CapabilityAuthorizer:
    def __init__(
        self,
        signing_secret: str | bytes,
        *,
        clock: Callable[[], float] = time.time,
        max_ttl_seconds: int = MAX_TOKEN_TTL_SECONDS,
    ) -> None:
        secret_bytes = (
            signing_secret.encode("utf-8")
            if isinstance(signing_secret, str)
            else bytes(signing_secret)
        )
        if len(secret_bytes) < 32:
            raise ReadonlyMCPError(
                "The MCP signing secret must contain at least 32 bytes.",
                code="MCP_SECRET_TOO_SHORT",
                http_status=500,
            )
        if not 1 <= int(max_ttl_seconds) <= 3_600:
            raise ReadonlyMCPError(
                "The MCP maximum token TTL is invalid.",
                code="MCP_TTL_INVALID",
                http_status=500,
            )
        self._secret = secret_bytes
        self._clock = clock
        self.max_ttl_seconds = int(max_ttl_seconds)

    def mint(
        self,
        room_id: Any,
        round_id: Any,
        *,
        ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    ) -> str:
        clean_room_id = _bounded_identifier(room_id, "room_id")
        clean_round_id = _bounded_identifier(round_id, "round_id")
        clean_ttl = int(ttl_seconds)
        if not 1 <= clean_ttl <= self.max_ttl_seconds:
            raise ReadonlyMCPError(
                f"ttl_seconds must be between 1 and {self.max_ttl_seconds}.",
                code="MCP_TTL_INVALID",
            )
        issued_at = int(self._clock())
        payload = {
            "version": MCP_CAPABILITY_VERSION,
            "room_id": clean_room_id,
            "round_id": clean_round_id,
            "iat": issued_at,
            "exp": issued_at + clean_ttl,
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
        room_id: Any | None = None,
        round_id: Any | None = None,
    ) -> CapabilityClaims:
        unauthorized = ReadonlyMCPError(
            "The MCP capability token is invalid or expired.",
            code="MCP_UNAUTHORIZED",
            http_status=401,
        )
        candidate = str(token or "").strip()
        if len(candidate) > 4_096 or candidate.count(".") != 1:
            raise unauthorized
        encoded_payload, encoded_signature = candidate.split(".", 1)
        try:
            supplied_signature = _base64url_decode(encoded_signature)
            expected_signature = hmac.new(
                self._secret,
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise unauthorized
            payload = json.loads(_base64url_decode(encoded_payload).decode("utf-8"))
            if not isinstance(payload, dict):
                raise unauthorized
            payload_room_id = _bounded_identifier(payload.get("room_id"), "room_id")
            payload_round_id = _bounded_identifier(payload.get("round_id"), "round_id")
            issued_at = int(payload.get("iat"))
            expires_at = int(payload.get("exp"))
            token_id = str(payload.get("jti") or "")
        except (ReadonlyMCPError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise unauthorized from None
        now = int(self._clock())
        if (
            payload.get("version") != MCP_CAPABILITY_VERSION
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", token_id)
            or issued_at > now + 5
            or expires_at <= now
            or expires_at <= issued_at
            or expires_at - issued_at > self.max_ttl_seconds
        ):
            raise unauthorized
        if room_id is not None and _bounded_identifier(room_id, "room_id") != payload_room_id:
            raise unauthorized
        if round_id is not None and _bounded_identifier(round_id, "round_id") != payload_round_id:
            raise unauthorized
        return CapabilityClaims(
            room_id=payload_room_id,
            round_id=payload_round_id,
            issued_at=issued_at,
            expires_at=expires_at,
            token_id=token_id,
        )


class ReadOnlyManualChatGPTDataSource:
    """Read frozen manual-ChatGPT records through an SQLite read-only URI."""

    def __init__(self, database_path: str | Path) -> None:
        requested = Path(database_path).expanduser()
        offending = first_reparse_component(requested)
        if offending is not None:
            raise ReadonlyMCPError(
                "The MCP database path contains a reparse point.",
                code="MCP_DATABASE_PATH_UNSAFE",
                http_status=500,
            )
        clean_path = requested.resolve()
        if not clean_path.is_file():
            raise ReadonlyMCPError(
                "The MCP database must be an existing regular file.",
                code="MCP_DATABASE_MISSING",
                http_status=500,
            )
        self._database_path = clean_path
        metadata = clean_path.stat()
        self._database_identity = (int(metadata.st_dev), int(metadata.st_ino))

    def _verify_path_identity(self) -> None:
        if first_reparse_component(self._database_path) is not None:
            raise ReadonlyMCPError(
                "The MCP database path identity changed.",
                code="MCP_DATABASE_PATH_UNSAFE",
                http_status=503,
            )
        try:
            metadata = self._database_path.stat()
        except OSError as exc:
            raise ReadonlyMCPError(
                "The MCP database path identity changed.",
                code="MCP_DATABASE_PATH_UNSAFE",
                http_status=503,
            ) from exc
        if (int(metadata.st_dev), int(metadata.st_ino)) != self._database_identity:
            raise ReadonlyMCPError(
                "The MCP database path identity changed.",
                code="MCP_DATABASE_PATH_UNSAFE",
                http_status=503,
            )

    def _connect(self) -> sqlite3.Connection:
        self._verify_path_identity()
        uri = f"{self._database_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            self._verify_path_identity()
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA busy_timeout=5000")
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _read_review_context(
        connection: sqlite3.Connection,
        *,
        session_id: str,
        room_id: str,
    ) -> dict[str, Any]:
        required_tables = {
            "manual_chatgpt_review_runs",
            "manual_chatgpt_api_reviews",
            "manual_chatgpt_decisions",
        }
        available_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)",
                tuple(sorted(required_tables)),
            ).fetchall()
        }
        if available_tables != required_tables:
            return {
                "available": False,
                "run": {},
                "reviews": [],
                "execution_run": {},
                "attempts": [],
                "decision": {},
            }

        run_row = connection.execute(
            "SELECT * FROM manual_chatgpt_review_runs WHERE session_id=? AND room_id=?",
            (session_id, room_id),
        ).fetchone()
        run = dict(run_row) if run_row else {}
        reviews = [
            dict(row)
            for row in connection.execute(
                """SELECT * FROM manual_chatgpt_api_reviews
                     WHERE session_id=? AND room_id=? ORDER BY review_index""",
                (session_id, room_id),
            ).fetchall()
        ]
        decision_row = connection.execute(
            "SELECT * FROM manual_chatgpt_decisions WHERE session_id=? AND room_id=?",
            (session_id, room_id),
        ).fetchone()
        decision = dict(decision_row) if decision_row else {}
        execution_run: dict[str, Any] = {}
        attempts: list[dict[str, Any]] = []
        if run:
            execution_run_id = str(run.get("provider_execution_run_id") or "")
            execution_row = connection.execute(
                "SELECT * FROM provider_execution_runs WHERE id=? AND room_id=?",
                (execution_run_id, room_id),
            ).fetchone()
            execution_run = dict(execution_row) if execution_row else {}
            attempts = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM provider_call_attempts
                         WHERE run_id=? ORDER BY sequence_no,id""",
                    (execution_run_id,),
                ).fetchall()
            ]
        return {
            "available": True,
            "run": run,
            "reviews": reviews,
            "execution_run": execution_run,
            "attempts": attempts,
            "decision": decision,
        }

    def load(self, room_id: Any, round_id: Any) -> dict[str, Any]:
        clean_room_id = _bounded_identifier(room_id, "room_id")
        clean_round_id = _bounded_identifier(round_id, "round_id")
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                row = connection.execute(
                    """SELECT * FROM manual_chatgpt_sessions
                         WHERE room_id=? AND round_id=?""",
                    (clean_room_id, clean_round_id),
                ).fetchone()
                if not row:
                    raise ReadonlyMCPError(
                        "The authorized collaboration round was not found.",
                        code="MCP_ROUND_NOT_FOUND",
                        http_status=404,
                    )
                events = connection.execute(
                    """SELECT * FROM manual_chatgpt_events
                         WHERE session_id=? AND room_id=? ORDER BY sequence_no""",
                    (str(row["id"]), clean_room_id),
                ).fetchall()
                review_context = self._read_review_context(
                    connection,
                    session_id=str(row["id"]),
                    room_id=clean_room_id,
                )
                return self._verified_projection(
                    dict(row),
                    [dict(item) for item in events],
                    review_context=review_context,
                )
        except sqlite3.Error as exc:
            raise ReadonlyMCPError(
                "The read-only collaboration snapshot is unavailable.",
                code="MCP_DATABASE_READ_FAILED",
                http_status=503,
            ) from exc

    @staticmethod
    def _verified_projection(
        data: Mapping[str, Any],
        event_rows: list[Mapping[str, Any]],
        *,
        review_context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        session_id = str(data.get("id") or "")
        room_id = str(data.get("room_id") or "")
        round_id = str(data.get("round_id") or "")
        bundle = _loads_object(data.get("bundle_json"))
        result = _loads_object(data.get("result_json"))
        issues = _loads_list(data.get("last_issues_json"))

        previous = ""
        event_chain_ok = True
        events: list[dict[str, Any]] = []
        for raw_event in event_rows:
            payload = _loads_object(raw_event.get("payload_json"))
            basis = {
                "version": MANUAL_CHATGPT_EVENT_VERSION,
                "session_id": session_id,
                "room_id": room_id,
                "sequence_no": int(raw_event.get("sequence_no") or 0),
                "from_state": str(raw_event.get("from_state") or ""),
                "to_state": str(raw_event.get("to_state") or ""),
                "event_type": str(raw_event.get("event_type") or ""),
                "payload": payload,
                "previous_event_sha256": str(
                    raw_event.get("previous_event_sha256") or ""
                ),
                "created_at": int(raw_event.get("created_at") or 0),
            }
            event_sha256 = str(raw_event.get("event_sha256") or "")
            if (
                basis["previous_event_sha256"] != previous
                or canonical_sha256(basis) != event_sha256
            ):
                event_chain_ok = False
            previous = event_sha256
            events.append({
                "sequence_no": basis["sequence_no"],
                "from_state": basis["from_state"],
                "to_state": basis["to_state"],
                "event_type": basis["event_type"],
                "created_at": basis["created_at"],
                "event_sha256": event_sha256,
            })

        bundle_basis = copy.deepcopy(bundle)
        stored_bundle_hash = str(data.get("bundle_sha256") or "")
        declared_bundle_hash = str(bundle_basis.pop("bundle_sha256", "") or "")
        context = bundle.get("context") if isinstance(bundle.get("context"), dict) else {}
        bundle_ok = bool(
            bundle_basis
            and declared_bundle_hash == stored_bundle_hash
            and canonical_sha256(bundle_basis) == stored_bundle_hash
            and canonical_sha256(context) == str(data.get("context_sha256") or "")
            and str(bundle.get("room_id") or "") == room_id
            and str(bundle.get("round_id") or "") == round_id
            and str(bundle.get("session_id") or "") == session_id
            and str(bundle.get("import_contract_version") or "")
            in SUPPORTED_MANUAL_CHATGPT_IMPORT_CONTRACT_VERSIONS
        )
        stored_result_hash = str(data.get("result_sha256") or "")
        result_ok = not stored_result_hash or canonical_sha256(result) == stored_result_hash
        event_chain_ok = bool(
            event_chain_ok
            and len(events) == int(data.get("event_sequence") or 0)
            and previous == str(data.get("event_head_sha256") or "")
        )
        state = str(data.get("state") or "")
        review_context = review_context or {
            "available": False,
            "run": {},
            "reviews": [],
            "execution_run": {},
            "attempts": [],
            "decision": {},
        }
        review_available = review_context.get("available") is True
        review_run = (
            dict(review_context.get("run") or {})
            if isinstance(review_context.get("run"), Mapping)
            else {}
        )
        review_rows = [
            dict(item)
            for item in review_context.get("reviews", [])
            if isinstance(item, Mapping)
        ]
        execution_run = (
            dict(review_context.get("execution_run") or {})
            if isinstance(review_context.get("execution_run"), Mapping)
            else {}
        )
        attempt_rows = [
            dict(item)
            for item in review_context.get("attempts", [])
            if isinstance(item, Mapping)
        ]
        decision_row = (
            dict(review_context.get("decision") or {})
            if isinstance(review_context.get("decision"), Mapping)
            else {}
        )
        review_integrity_ok = True
        decision_integrity_ok = True
        completed_reviews = 0
        expected_reviews = int(
            (
                bundle.get("budget")
                if isinstance(bundle.get("budget"), Mapping)
                else {}
            ).get("independent_api_reviews")
            or 0
        )
        review_status = "migration_required" if not review_available else "not_started"
        review_hashes: list[str] = []
        distinct_attempt_ids: set[str] = set()

        if not review_available:
            review_integrity_ok = state not in {"READY_FOR_DECISION", "FROZEN"}
            decision_integrity_ok = state not in {"READY_FOR_DECISION", "FROZEN"}
        elif review_run:
            plan = _loads_object(review_run.get("plan_json"))
            run_expected = int(review_run.get("expected_calls") or 0)
            run_completed = int(review_run.get("completed_calls") or 0)
            raw_status = str(review_run.get("status") or "")
            review_status = {
                "RUNNING": "running",
                "COMPLETED": "completed",
                "FAILED": "failed",
                "BUDGET_BLOCKED": "budget_blocked",
            }.get(raw_status, "invalid")
            review_integrity_ok = bool(
                plan
                and canonical_sha256(plan) == str(review_run.get("plan_sha256") or "")
                and str(plan.get("session_id") or "") == session_id
                and str(plan.get("room_id") or "") == room_id
                and str(plan.get("provider") or "") == str(review_run.get("provider") or "")
                and str(plan.get("model") or "") == str(review_run.get("requested_model") or "")
                and str(review_run.get("mode") or "") == str(data.get("mode") or "")
                and int(plan.get("expected_calls") or 0) == run_expected
                and run_expected == expected_reviews
                and 0 <= run_completed <= run_expected
                and review_status != "invalid"
                and execution_run
                and str(execution_run.get("room_id") or "") == room_id
                and str(execution_run.get("scope") or "") == "manual_chatgpt_review"
                and str(execution_run.get("client_request_id") or "")
                == str(review_run.get("client_request_id") or "")
                and str(execution_run.get("plan_hash") or "")
                == str(review_run.get("plan_sha256") or "")
                and int(execution_run.get("max_calls") or 0) == run_expected
            )
            raw_planned_reviews = (
                plan.get("reviews") if isinstance(plan.get("reviews"), list) else []
            )
            planned_reviews = {
                int(item.get("review_index") or 0): item
                for item in raw_planned_reviews
                if isinstance(item, Mapping)
            }
            attempts_by_id = {
                str(item.get("id") or ""): item
                for item in attempt_rows
                if str(item.get("id") or "")
            }
            response_models: set[str] = set()
            for review_row in review_rows:
                review_content = _loads_object(review_row.get("review_json"))
                review_index = int(review_row.get("review_index") or 0)
                attempt_id = str(review_row.get("provider_attempt_id") or "")
                attempt = attempts_by_id.get(attempt_id) or {}
                planned = planned_reviews.get(review_index) or {}
                record_basis = {
                    "version": MANUAL_CHATGPT_API_REVIEW_RECORD_VERSION,
                    "review_index": review_index,
                    "review_kind": str(review_row.get("review_kind") or ""),
                    "provider": str(review_row.get("provider") or ""),
                    "requested_model": str(review_row.get("requested_model") or ""),
                    "response_model": str(review_row.get("response_model") or ""),
                    "independence_classification": str(
                        review_row.get("independence_classification") or ""
                    ),
                    "provider_attempt_id": attempt_id,
                    "request_sha256": str(review_row.get("request_sha256") or ""),
                    "review": review_content,
                }
                row_ok = bool(
                    review_content
                    and canonical_sha256(record_basis)
                    == str(review_row.get("review_sha256") or "")
                    and str(review_content.get("review_kind") or "")
                    == str(review_row.get("review_kind") or "")
                    and attempt_id
                    and attempt_id not in distinct_attempt_ids
                    and attempt
                    and str(attempt.get("run_id") or "")
                    == str(review_run.get("provider_execution_run_id") or "")
                    and str(attempt.get("status") or "") == "RESPONDED"
                    and str(attempt.get("kind") or "") == "manual_chatgpt_review"
                    and str(attempt.get("provider") or "")
                    == str(review_row.get("provider") or "")
                    and str(planned.get("review_kind") or "")
                    == str(review_row.get("review_kind") or "")
                    and str(planned.get("request_sha256") or "")
                    == str(review_row.get("request_sha256") or "")
                )
                review_integrity_ok = review_integrity_ok and row_ok
                distinct_attempt_ids.add(attempt_id)
                response_model = str(review_row.get("response_model") or "")
                if response_model:
                    response_models.add(response_model)
                review_hashes.append(str(review_row.get("review_sha256") or ""))
            completed_reviews = len(review_rows)
            review_integrity_ok = bool(
                review_integrity_ok
                and completed_reviews == run_completed
                and len(response_models) <= 1
            )
            if raw_status == "COMPLETED":
                review_integrity_ok = bool(
                    review_integrity_ok
                    and completed_reviews == run_expected
                    and len(attempt_rows) == run_expected
                    and str(execution_run.get("status") or "") == "COMPLETED"
                )
        elif review_rows or decision_row or state in {"READY_FOR_DECISION", "FROZEN"}:
            review_integrity_ok = False

        if decision_row:
            decision_card = _loads_object(decision_row.get("decision_card_json"))
            confirmation = _loads_object(decision_row.get("confirmation_json"))
            decision_card_sha256 = str(decision_row.get("decision_card_sha256") or "")
            expected_reviews_sha256 = canonical_sha256(review_hashes)
            decision_integrity_ok = bool(
                review_run
                and str(decision_row.get("session_id") or "") == session_id
                and str(decision_row.get("room_id") or "") == room_id
                and str(decision_row.get("review_run_id") or "")
                == str(review_run.get("id") or "")
                and decision_card
                and canonical_sha256(decision_card) == decision_card_sha256
                and str(decision_row.get("result_sha256") or "") == stored_result_hash
                and str(decision_row.get("reviews_sha256") or "")
                == expected_reviews_sha256
                and str(decision_card.get("reviews_sha256") or "")
                == expected_reviews_sha256
            )
            confirmation_sha256 = str(decision_row.get("confirmation_sha256") or "")
            if confirmation_sha256:
                decision_integrity_ok = bool(
                    decision_integrity_ok
                    and confirmation
                    and canonical_sha256(confirmation) == confirmation_sha256
                    and str(confirmation.get("decision_card_sha256") or "")
                    == decision_card_sha256
                    and str(confirmation.get("selected_option_id") or "")
                    == str(decision_row.get("selected_option_id") or "")
                )
        if state in {"READY_FOR_DECISION", "FROZEN"}:
            decision_integrity_ok = bool(
                decision_integrity_ok
                and decision_row
                and review_status == "completed"
                and _loads_object(decision_row.get("decision_card_json")).get(
                    "ready_for_user_decision"
                ) is True
            )
        if state == "FROZEN":
            decision_integrity_ok = bool(
                decision_integrity_ok
                and str(decision_row.get("confirmation_sha256") or "")
                and _loads_object(decision_row.get("confirmation_json"))
            )

        integrity = {
            "ok": bool(
                bundle_ok
                and result_ok
                and event_chain_ok
                and review_integrity_ok
                and decision_integrity_ok
            ),
            "bundle_ok": bundle_ok,
            "result_ok": result_ok,
            "event_chain_ok": event_chain_ok,
            "api_review_ok": review_integrity_ok,
            "decision_ok": decision_integrity_ok,
        }
        if not integrity["ok"]:
            raise ReadonlyMCPError(
                "The collaboration round failed deterministic integrity checks.",
                code="MCP_INTEGRITY_FAILED",
                http_status=409,
            )
        return {
            "version": MANUAL_CHATGPT_SESSION_VERSION,
            "session_id": session_id,
            "room_id": room_id,
            "round_id": round_id,
            "mode": str(data.get("mode") or ""),
            "state": str(data.get("state") or ""),
            "bundle": bundle,
            "bundle_sha256": stored_bundle_hash,
            "context_sha256": str(data.get("context_sha256") or ""),
            "result_present": bool(stored_result_hash),
            "result": result,
            "result_sha256": stored_result_hash,
            "validation_issues": issues,
            "events": events,
            "api_review": {
                "available": review_available,
                "migration_required": not review_available,
                "planned": expected_reviews,
                "completed": completed_reviews,
                "status": review_status,
                "all_calls_are_distinct": bool(
                    completed_reviews == len(distinct_attempt_ids)
                ),
                "integrity_ok": review_integrity_ok,
            },
            "integrity": integrity,
            "created_at": int(data.get("created_at") or 0),
            "updated_at": int(data.get("updated_at") or 0),
        }


def _json_size(value: Any) -> int:
    return len(_canonical_json_bytes(value))


def _read_only_annotations(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def _scope_schema() -> dict[str, Any]:
    return {
        "room_id": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_-]{1,80}$",
            "description": "Room identifier bound to the bearer capability.",
        },
        "round_id": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_-]{1,80}$",
            "description": "Frozen manual-ChatGPT round bound to the bearer capability.",
        },
    }


def mcp_tool_definitions() -> list[dict[str, Any]]:
    scope = _scope_schema()
    base_output_properties = {
        "version": {"type": "string", "const": MCP_GATEWAY_PROJECTION_VERSION},
        "room_id": {"type": "string"},
        "round_id": {"type": "string"},
    }

    def output_schema(
        properties: Mapping[str, Any],
        required: list[str],
    ) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                **copy.deepcopy(base_output_properties),
                **copy.deepcopy(dict(properties)),
            },
            "required": ["version", "room_id", "round_id", *required],
            "additionalProperties": False,
        }

    bundle_output = output_schema({
        "source_bundle_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "projection_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "sanitized": {"type": "boolean", "const": True},
        "bundle": {"type": "object"},
    }, ["source_bundle_sha256", "projection_sha256", "sanitized", "bundle"])
    evidence_output = output_schema({
        "evidence_id": {"type": "string"},
        "evidence_version": {"type": "integer", "minimum": 1},
        "title": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0},
        "returned_characters": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_EVIDENCE_CHUNK_CHARS,
        },
        "next_offset": {"type": ["integer", "null"], "minimum": 0},
        "frozen_excerpt_characters": {
            "type": "integer",
            "minimum": 0,
            "maximum": MAX_EVIDENCE_CHUNK_CHARS,
        },
        "source_limited_to_frozen_excerpt": {"type": "boolean", "const": True},
        "chunk": {"type": "string", "maxLength": MAX_EVIDENCE_CHUNK_CHARS},
    }, [
        "evidence_id",
        "evidence_version",
        "title",
        "offset",
        "returned_characters",
        "next_offset",
        "frozen_excerpt_characters",
        "source_limited_to_frozen_excerpt",
        "chunk",
    ])
    status_output = output_schema({
        "session_id": {"type": "string"},
        "mode": {"type": "string", "enum": ["quick", "standard", "deep"]},
        "state": {"type": "string"},
        "bundle_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "context_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "result_sha256": {"type": "string", "pattern": "^(?:|[a-f0-9]{64})$"},
        "result_present": {"type": "boolean"},
        "integrity": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean", "const": True},
                "bundle_ok": {"type": "boolean", "const": True},
                "result_ok": {"type": "boolean", "const": True},
                "event_chain_ok": {"type": "boolean", "const": True},
                "api_review_ok": {"type": "boolean", "const": True},
                "decision_ok": {"type": "boolean", "const": True},
            },
            "required": [
                "ok",
                "bundle_ok",
                "result_ok",
                "event_chain_ok",
                "api_review_ok",
                "decision_ok",
            ],
            "additionalProperties": False,
        },
        "event_count": {"type": "integer", "minimum": 0},
        "last_event_type": {"type": "string"},
        "validation_issue_codes": {"type": "array", "items": {"type": "string"}},
        "deterministic_validation_passed": {"type": "boolean"},
        "chatgpt_panels": {
            "type": "object",
            "properties": {
                "planned": {"type": "integer", "minimum": 1, "maximum": 3},
                "imported": {"type": "integer", "minimum": 0, "maximum": 3},
            },
            "required": ["planned", "imported"],
            "additionalProperties": False,
        },
        "independent_api_reviews": {
            "type": "object",
            "properties": {
                "planned": {"type": "integer", "minimum": 2, "maximum": 4},
                "completed": {"type": "integer", "minimum": 0, "maximum": 4},
                "status": {
                    "type": "string",
                    "enum": [
                        "migration_required",
                        "not_started",
                        "running",
                        "completed",
                        "failed",
                        "budget_blocked",
                    ],
                },
                "all_calls_are_distinct": {"type": "boolean"},
                "integrity_ok": {"type": "boolean", "const": True},
            },
            "required": [
                "planned",
                "completed",
                "status",
                "all_calls_are_distinct",
                "integrity_ok",
            ],
            "additionalProperties": False,
        },
        "safety": {
            "type": "object",
            "properties": {
                "read_only": {"type": "boolean", "const": True},
                "sqlite_write_capability": {"type": "boolean", "const": False},
                "provider_calls_performed": {"type": "integer", "const": 0},
                "market_calls_performed": {"type": "integer", "const": 0},
                "import_capability": {"type": "string", "const": "host_only"},
                "user_final_decision_required": {"type": "boolean", "const": True},
            },
            "required": [
                "read_only",
                "sqlite_write_capability",
                "provider_calls_performed",
                "market_calls_performed",
                "import_capability",
                "user_final_decision_required",
            ],
            "additionalProperties": False,
        },
        "updated_at": {"type": "integer", "minimum": 0},
    }, [
        "session_id",
        "mode",
        "state",
        "bundle_sha256",
        "context_sha256",
        "result_sha256",
        "result_present",
        "integrity",
        "event_count",
        "last_event_type",
        "validation_issue_codes",
        "deterministic_validation_passed",
        "chatgpt_panels",
        "independent_api_reviews",
        "safety",
        "updated_at",
    ])
    contract_output = output_schema({
        "source_bundle_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "contract_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "import_location": {"type": "string", "const": "host_application_only"},
        "user_confirmation_required": {"type": "boolean", "const": True},
        "contract": {"type": "object"},
    }, [
        "source_bundle_sha256",
        "contract_sha256",
        "import_location",
        "user_confirmation_required",
        "contract",
    ])
    definitions = [
        {
            "name": "get_room_bundle",
            "title": "Get sanitized room bundle",
            "description": (
                "Read the bounded, sanitized projection of one authorized frozen "
                "manual-ChatGPT collaboration bundle. It never returns credentials, "
                "HTTP headers, local absolute paths, imported results, or write handles."
            ),
            "inputSchema": {
                "type": "object",
                "properties": copy.deepcopy(scope),
                "required": ["room_id", "round_id"],
                "additionalProperties": False,
            },
            "outputSchema": bundle_output,
            "annotations": _read_only_annotations("Get sanitized room bundle"),
        },
        {
            "name": "get_evidence_chunk",
            "title": "Get bounded evidence chunk",
            "description": (
                "Read a bounded character slice from evidence already frozen into the "
                "authorized room bundle. It cannot open files, URLs, providers, or markets."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    **copy.deepcopy(scope),
                    "evidence_id": {
                        "type": "string",
                        "pattern": "^[A-Za-z0-9_-]{1,80}$",
                    },
                    "offset": {"type": "integer", "minimum": 0, "maximum": 1_600},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 1_600},
                },
                "required": ["room_id", "round_id", "evidence_id"],
                "additionalProperties": False,
            },
            "outputSchema": evidence_output,
            "annotations": _read_only_annotations("Get bounded evidence chunk"),
        },
        {
            "name": "get_round_status",
            "title": "Get round status",
            "description": (
                "Read integrity, state, event, validation, and review-count status for "
                "the authorized frozen round. No result body or provider call is returned."
            ),
            "inputSchema": {
                "type": "object",
                "properties": copy.deepcopy(scope),
                "required": ["room_id", "round_id"],
                "additionalProperties": False,
            },
            "outputSchema": status_output,
            "annotations": _read_only_annotations("Get round status"),
        },
        {
            "name": "get_import_contract",
            "title": "Get import contract",
            "description": (
                "Read the deterministic single-JSON import contract for the authorized "
                "round. Import and confirmation remain in the host application."
            ),
            "inputSchema": {
                "type": "object",
                "properties": copy.deepcopy(scope),
                "required": ["room_id", "round_id"],
                "additionalProperties": False,
            },
            "outputSchema": contract_output,
            "annotations": _read_only_annotations("Get import contract"),
        },
    ]
    return definitions


class ReadonlyMCPGateway:
    def __init__(
        self,
        data_source: ReadOnlyManualChatGPTDataSource,
        authorizer: CapabilityAuthorizer,
    ) -> None:
        self.data_source = data_source
        self.authorizer = authorizer

    def _authorized_session(
        self,
        token: Any,
        arguments: Mapping[str, Any],
        *,
        allowed_arguments: set[str],
    ) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise ReadonlyMCPError(
                "Tool arguments must be an object.",
                code="MCP_ARGUMENT_INVALID",
            )
        unexpected = set(arguments) - allowed_arguments
        if unexpected:
            raise ReadonlyMCPError(
                f"Unexpected tool argument: {sorted(unexpected)[0]}",
                code="MCP_ARGUMENT_INVALID",
            )
        room_id = _bounded_identifier(arguments.get("room_id"), "room_id")
        round_id = _bounded_identifier(arguments.get("round_id"), "round_id")
        self.authorizer.authorize(token, room_id=room_id, round_id=round_id)
        return self.data_source.load(room_id, round_id)

    @staticmethod
    def _base(session: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "version": MCP_GATEWAY_PROJECTION_VERSION,
            "room_id": session["room_id"],
            "round_id": session["round_id"],
        }

    def call_tool(
        self,
        name: Any,
        arguments: Any,
        *,
        token: Any,
    ) -> dict[str, Any]:
        clean_name = str(name or "")
        handlers = {
            "get_room_bundle": self._get_room_bundle,
            "get_evidence_chunk": self._get_evidence_chunk,
            "get_round_status": self._get_round_status,
            "get_import_contract": self._get_import_contract,
        }
        handler = handlers.get(clean_name)
        if handler is None:
            raise ReadonlyMCPError(
                "Unknown MCP tool.",
                code="MCP_TOOL_NOT_FOUND",
                http_status=404,
            )
        return handler(arguments, token=token)

    def _get_room_bundle(self, arguments: Any, *, token: Any) -> dict[str, Any]:
        session = self._authorized_session(
            token,
            arguments,
            allowed_arguments={"room_id", "round_id"},
        )
        projected_bundle = sanitize_gateway_value(session["bundle"])
        if _json_size(projected_bundle) > MAX_BUNDLE_BYTES:
            raise ReadonlyMCPError(
                "The sanitized room bundle exceeds the gateway response limit.",
                code="MCP_RESPONSE_LIMIT",
                http_status=413,
            )
        return self._base(session) | {
            "source_bundle_sha256": session["bundle_sha256"],
            "projection_sha256": canonical_sha256(projected_bundle),
            "sanitized": True,
            "bundle": projected_bundle,
        }

    def _get_evidence_chunk(self, arguments: Any, *, token: Any) -> dict[str, Any]:
        session = self._authorized_session(
            token,
            arguments,
            allowed_arguments={"room_id", "round_id", "evidence_id", "offset", "limit"},
        )
        evidence_id = _bounded_identifier(arguments.get("evidence_id"), "evidence_id")
        raw_offset = arguments.get("offset", 0)
        raw_limit = arguments.get("limit", MAX_EVIDENCE_CHUNK_CHARS)
        if (
            isinstance(raw_offset, bool)
            or not isinstance(raw_offset, int)
            or isinstance(raw_limit, bool)
            or not isinstance(raw_limit, int)
        ):
            raise ReadonlyMCPError(
                "Evidence offset and limit must be integers.",
                code="MCP_ARGUMENT_INVALID",
            )
        offset = raw_offset
        limit = raw_limit
        if not 0 <= offset <= MAX_EVIDENCE_CHUNK_CHARS:
            raise ReadonlyMCPError(
                "Evidence offset is outside the frozen excerpt.",
                code="MCP_ARGUMENT_INVALID",
            )
        if not 1 <= limit <= MAX_EVIDENCE_CHUNK_CHARS:
            raise ReadonlyMCPError(
                "Evidence limit is outside the gateway bound.",
                code="MCP_ARGUMENT_INVALID",
            )
        context = session["bundle"].get("context")
        evidence_index = (
            context.get("evidence_index")
            if isinstance(context, Mapping) and isinstance(context.get("evidence_index"), list)
            else []
        )
        evidence = next(
            (
                item
                for item in evidence_index
                if isinstance(item, Mapping)
                and str(item.get("evidence_id") or "") == evidence_id
            ),
            None,
        )
        if evidence is None:
            raise ReadonlyMCPError(
                "Evidence is not present in the authorized frozen bundle.",
                code="MCP_EVIDENCE_NOT_FOUND",
                http_status=404,
            )
        excerpt = _sanitize_string(str(evidence.get("excerpt") or ""))
        chunk = excerpt[offset : offset + limit]
        return self._base(session) | {
            "evidence_id": evidence_id,
            "evidence_version": max(1, int(evidence.get("version") or 1)),
            "title": _sanitize_string(str(evidence.get("title") or "")),
            "offset": offset,
            "returned_characters": len(chunk),
            "next_offset": offset + len(chunk) if offset + len(chunk) < len(excerpt) else None,
            "frozen_excerpt_characters": len(excerpt),
            "source_limited_to_frozen_excerpt": True,
            "chunk": chunk,
        }

    def _get_round_status(self, arguments: Any, *, token: Any) -> dict[str, Any]:
        session = self._authorized_session(
            token,
            arguments,
            allowed_arguments={"room_id", "round_id"},
        )
        bundle = session["bundle"]
        budget = bundle.get("budget") if isinstance(bundle.get("budget"), Mapping) else {}
        result = session["result"] if isinstance(session.get("result"), Mapping) else {}
        panels = result.get("panels") if isinstance(result.get("panels"), list) else []
        state = str(session["state"])
        deterministic_validation_passed = state in {
            "API_REVIEW",
            "READY_FOR_DECISION",
            "FROZEN",
        }
        issue_codes = sorted({
            str(item.get("code") or "INVALID")
            for item in session["validation_issues"]
            if isinstance(item, Mapping)
        })
        return self._base(session) | {
            "session_id": session["session_id"],
            "mode": session["mode"],
            "state": state,
            "bundle_sha256": session["bundle_sha256"],
            "context_sha256": session["context_sha256"],
            "result_sha256": session["result_sha256"],
            "result_present": session["result_present"],
            "integrity": copy.deepcopy(session["integrity"]),
            "event_count": len(session["events"]),
            "last_event_type": session["events"][-1]["event_type"] if session["events"] else "",
            "validation_issue_codes": issue_codes,
            "deterministic_validation_passed": deterministic_validation_passed,
            "chatgpt_panels": {
                "planned": int(budget.get("chatgpt_panel_calls") or 0),
                "imported": len(panels),
            },
            "independent_api_reviews": {
                "planned": int(budget.get("independent_api_reviews") or 0),
                "completed": int(session["api_review"]["completed"]),
                "status": str(session["api_review"]["status"]),
                "all_calls_are_distinct": bool(
                    session["api_review"]["all_calls_are_distinct"]
                ),
                "integrity_ok": bool(session["api_review"]["integrity_ok"]),
            },
            "safety": {
                "read_only": True,
                "sqlite_write_capability": False,
                "provider_calls_performed": 0,
                "market_calls_performed": 0,
                "import_capability": "host_only",
                "user_final_decision_required": True,
            },
            "updated_at": session["updated_at"],
        }

    def _get_import_contract(self, arguments: Any, *, token: Any) -> dict[str, Any]:
        session = self._authorized_session(
            token,
            arguments,
            allowed_arguments={"room_id", "round_id"},
        )
        contract = sanitize_gateway_value(_import_contract(session["bundle"]))
        if _json_size(contract) > MAX_IMPORT_CONTRACT_BYTES:
            raise ReadonlyMCPError(
                "The import contract exceeds the gateway response limit.",
                code="MCP_RESPONSE_LIMIT",
                http_status=413,
            )
        return self._base(session) | {
            "source_bundle_sha256": session["bundle_sha256"],
            "contract_sha256": canonical_sha256(contract),
            "import_location": "host_application_only",
            "user_confirmation_required": True,
            "contract": contract,
        }


class _RateLimiter:
    def __init__(
        self,
        limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.limit = max(1, int(limit_per_minute))
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}

    def check(self, token_id: str) -> None:
        window = int(self._clock()) // 60
        with self._lock:
            previous_window, count = self._windows.get(token_id, (window, 0))
            if previous_window != window:
                previous_window, count = window, 0
            count += 1
            self._windows[token_id] = (previous_window, count)
            if len(self._windows) > 2_000:
                self._windows = {
                    key: value
                    for key, value in self._windows.items()
                    if value[0] >= window - 1
                }
            if count > self.limit:
                raise ReadonlyMCPError(
                    "The MCP capability rate limit was exceeded.",
                    code="MCP_RATE_LIMITED",
                    http_status=429,
                )


class ReadonlyMCPApplication:
    def __init__(
        self,
        gateway: ReadonlyMCPGateway,
        *,
        rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    ) -> None:
        self.gateway = gateway
        self.rate_limiter = _RateLimiter(rate_limit_per_minute)

    @staticmethod
    def _result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}

    @staticmethod
    def _error(
        request_id: Any,
        code: int,
        message: str,
        *,
        data: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        error: dict[str, Any] = {"code": code, "message": message}
        if data:
            error["data"] = dict(data)
        return {"jsonrpc": "2.0", "id": request_id, "error": error}

    @staticmethod
    def _tool_result(value: Mapping[str, Any]) -> dict[str, Any]:
        structured_content = dict(value)
        serialized_content = _canonical_json_bytes(structured_content).decode("utf-8")
        return {
            "content": [{"type": "text", "text": serialized_content}],
            "structuredContent": structured_content,
            "isError": False,
        }

    @staticmethod
    def _tool_error(error: ReadonlyMCPError) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": str(error)}],
            "isError": True,
        }

    def handle(self, message: Any, *, bearer_token: Any) -> tuple[int, dict[str, Any] | None]:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return 400, self._error(None, -32600, "Invalid Request")
        request_id = message.get("id")
        has_id = "id" in message
        if has_id and (
            isinstance(request_id, bool)
            or request_id is None
            or not isinstance(request_id, (str, int, float))
        ):
            return 400, self._error(None, -32600, "Invalid Request")
        method = message.get("method")
        if not isinstance(method, str):
            return 400, self._error(request_id if has_id else None, -32600, "Invalid Request")
        try:
            claims = self.gateway.authorizer.authorize(bearer_token)
            self.rate_limiter.check(claims.token_id)
        except ReadonlyMCPError as exc:
            return exc.http_status, self._error(
                request_id if has_id else None,
                -32001,
                "Unauthorized" if exc.http_status == 401 else str(exc),
                data={"code": exc.code},
            )

        params = message.get("params", {})
        if not isinstance(params, Mapping):
            return 400, self._error(request_id, -32602, "Invalid params")
        if method == "initialize":
            client_info = params.get("clientInfo")
            capabilities = params.get("capabilities")
            if (
                not has_id
                or not isinstance(client_info, Mapping)
                or not str(client_info.get("name") or "").strip()
                or not str(client_info.get("version") or "").strip()
                or not isinstance(capabilities, Mapping)
            ):
                return 400, self._error(request_id, -32602, "Invalid params")
            requested_version = str(params.get("protocolVersion") or "")
            negotiated = (
                requested_version
                if requested_version in MCP_SUPPORTED_PROTOCOL_VERSIONS
                else MCP_PROTOCOL_VERSION
            )
            return 200, self._result(request_id, {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": MCP_SERVER_NAME,
                    "title": "AI Collaboration Studio Read-only Gateway",
                    "version": MCP_SERVER_VERSION,
                },
                "instructions": (
                    "Read-only access to one bearer-authorized room and round. "
                    "Never infer write, import, provider, market, or execution capability."
                ),
            })
        if method == "notifications/initialized":
            if has_id or set(params):
                return 400, self._error(request_id if has_id else None, -32600, "Invalid Request")
            return 202, None
        if not has_id:
            return 202, None
        if method == "ping":
            return 200, self._result(request_id, {})
        if method == "tools/list":
            if set(params) - {"cursor"}:
                return 400, self._error(request_id, -32602, "Invalid params")
            if params.get("cursor") not in (None, ""):
                return 400, self._error(request_id, -32602, "Invalid cursor")
            return 200, self._result(request_id, {"tools": mcp_tool_definitions()})
        if method == "tools/call":
            if set(params) - {"name", "arguments"} or not isinstance(params.get("name"), str):
                return 400, self._error(request_id, -32602, "Invalid params")
            name = params.get("name")
            arguments = params.get("arguments", {})
            try:
                value = self.gateway.call_tool(name, arguments, token=bearer_token)
                result = self._tool_result(value)
            except ReadonlyMCPError as exc:
                if exc.code == "MCP_TOOL_NOT_FOUND":
                    return 404, self._error(
                        request_id,
                        -32601,
                        "Method not found",
                        data={"code": exc.code},
                    )
                result = self._tool_error(exc)
            response = self._result(request_id, result)
            if _json_size(response) > MAX_RESPONSE_BYTES:
                response = self._result(
                    request_id,
                    self._tool_error(ReadonlyMCPError(
                        "The MCP response exceeds the gateway limit.",
                        code="MCP_RESPONSE_LIMIT",
                        http_status=413,
                    )),
                )
            return 200, response
        return 404, self._error(request_id, -32601, "Method not found")


def _extract_bearer(value: Any) -> str:
    candidate = str(value or "")
    if not candidate.startswith("Bearer ") or candidate.count(" ") != 1:
        return ""
    return candidate[7:]


def _origin_allowed(value: Any, allowed_origins: set[str]) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return True
    if candidate in allowed_origins:
        return True
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def build_http_server(
    application: ReadonlyMCPApplication,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    allowed_origins: set[str] | None = None,
) -> ThreadingHTTPServer:
    clean_host = str(host or "").strip()
    if clean_host != "127.0.0.1":
        raise ReadonlyMCPError(
            "The read-only MCP gateway may bind only to 127.0.0.1.",
            code="MCP_BIND_FORBIDDEN",
            http_status=500,
        )
    clean_port = int(port)
    if clean_port < 0 or clean_port > 65_535 or clean_port in PROTECTED_PORTS:
        raise ReadonlyMCPError(
            "The requested MCP port is invalid or protected.",
            code="MCP_PORT_FORBIDDEN",
            http_status=500,
        )
    origins = set(allowed_origins or set())

    class Handler(BaseHTTPRequestHandler):
        server_version = "AIStudioReadonlyMCP/0.1"
        sys_version = ""

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _send_empty(self, status: int, *, allow: str = "") -> None:
            self.send_response(status)
            if allow:
                self.send_header("Allow", allow)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
            body = _canonical_json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _reject(self, status: int, message: str, code: str) -> None:
            self._send_json(status, ReadonlyMCPApplication._error(
                None,
                -32000,
                message,
                data={"code": code},
            ))

        def do_GET(self) -> None:  # noqa: N802
            if self.path != MCP_ENDPOINT_PATH:
                self._send_empty(404)
                return
            self._send_empty(405, allow="POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._send_empty(405, allow="POST")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != MCP_ENDPOINT_PATH:
                self._send_empty(404)
                return
            if not _origin_allowed(self.headers.get("Origin"), origins):
                self._reject(403, "Origin is not allowed.", "MCP_ORIGIN_FORBIDDEN")
                return
            accept = {
                part.split(";", 1)[0].strip().lower()
                for part in str(self.headers.get("Accept") or "").split(",")
            }
            if not {"application/json", "text/event-stream"}.issubset(accept):
                self._reject(406, "MCP Accept header is invalid.", "MCP_ACCEPT_INVALID")
                return
            content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            if content_type != "application/json":
                self._reject(415, "MCP requests must use application/json.", "MCP_CONTENT_TYPE_INVALID")
                return
            if self.headers.get("Transfer-Encoding"):
                self._reject(400, "Chunked MCP requests are not accepted.", "MCP_BODY_INVALID")
                return
            try:
                content_length = int(self.headers.get("Content-Length") or "-1")
            except ValueError:
                content_length = -1
            if not 0 <= content_length <= MAX_REQUEST_BYTES:
                self._reject(413, "MCP request body is too large.", "MCP_REQUEST_LIMIT")
                return
            try:
                message = _strict_json_loads(self.rfile.read(content_length))
            except (UnicodeError, ValueError, json.JSONDecodeError):
                self._send_json(400, ReadonlyMCPApplication._error(None, -32700, "Parse error"))
                return
            is_initialize = isinstance(message, Mapping) and message.get("method") == "initialize"
            protocol_header = str(self.headers.get("MCP-Protocol-Version") or "")
            if (
                not is_initialize
                and protocol_header
                and protocol_header not in MCP_SUPPORTED_PROTOCOL_VERSIONS
            ):
                self._reject(400, "Unsupported MCP protocol version.", "MCP_PROTOCOL_UNSUPPORTED")
                return
            token = _extract_bearer(self.headers.get("Authorization"))
            status, payload = application.handle(message, bearer_token=token)
            if payload is None:
                self._send_empty(status)
            else:
                self._send_json(status, payload)

    return ThreadingHTTPServer((clean_host, clean_port), Handler)


def _signing_secret_from_environment() -> str:
    return str(os.getenv("AI_STUDIO_MCP_TOKEN_SIGNING_SECRET") or "")


def _allowed_origins_from_environment() -> set[str]:
    return {
        item.strip()
        for item in str(os.getenv("AI_STUDIO_MCP_ALLOWED_ORIGINS") or "").split(",")
        if item.strip()
    }


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independent read-only MCP gateway for one frozen collaboration round."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    mint = subcommands.add_parser("mint-token")
    mint.add_argument("--database", type=Path, required=True)
    mint.add_argument("--room-id", required=True)
    mint.add_argument("--round-id", required=True)
    mint.add_argument("--ttl-seconds", type=int, default=DEFAULT_TOKEN_TTL_SECONDS)
    serve = subcommands.add_parser("serve")
    serve.add_argument("--database", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument(
        "--rate-limit-per-minute",
        type=int,
        default=DEFAULT_RATE_LIMIT_PER_MINUTE,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_cli().parse_args(argv)
    try:
        authorizer = CapabilityAuthorizer(_signing_secret_from_environment())
        data_source = ReadOnlyManualChatGPTDataSource(arguments.database)
        if arguments.command == "mint-token":
            data_source.load(arguments.room_id, arguments.round_id)
            print(authorizer.mint(
                arguments.room_id,
                arguments.round_id,
                ttl_seconds=arguments.ttl_seconds,
            ))
            return 0
        gateway = ReadonlyMCPGateway(data_source, authorizer)
        application = ReadonlyMCPApplication(
            gateway,
            rate_limit_per_minute=arguments.rate_limit_per_minute,
        )
        server = build_http_server(
            application,
            host=arguments.host,
            port=arguments.port,
            allowed_origins=_allowed_origins_from_environment(),
        )
        host, port = server.server_address[:2]
        print(
            json.dumps({
                "server": MCP_SERVER_NAME,
                "transport": "streamable_http_json_response",
                "endpoint": f"http://{host}:{port}{MCP_ENDPOINT_PATH}",
                "sqlite_access": "read_only",
                "provider_calls": False,
                "market_calls": False,
            }, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    except (ReadonlyMCPError, OSError) as exc:
        message = str(exc) if isinstance(exc, ReadonlyMCPError) else "The MCP gateway could not start."
        print(
            json.dumps({
                "ok": False,
                "code": getattr(exc, "code", "MCP_START_FAILED"),
                "message": message,
            }, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CapabilityAuthorizer",
    "MCP_ENDPOINT_PATH",
    "MCP_PROTOCOL_VERSION",
    "ReadOnlyManualChatGPTDataSource",
    "ReadonlyMCPApplication",
    "ReadonlyMCPError",
    "ReadonlyMCPGateway",
    "build_http_server",
    "mcp_tool_definitions",
    "sanitize_gateway_value",
]
