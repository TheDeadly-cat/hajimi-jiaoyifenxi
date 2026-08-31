from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import re
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .action_desk import ActionDeskError, ActionDeskService
from .artifact_service import ARTIFACTS
from .candidate_comparison import (
    CandidateComparisonError,
    CandidateComparisonService,
)
from .candidate_experiment import (
    CandidateExperimentError,
    CandidateExperimentService,
)
from .candidate_simulation_contract import CandidateSimulationContractError
from .config import (
    DEFAULT_PROVIDER,
    FRONTEND_DIST,
    HOST,
    PORT,
    PROJECT_CAPABILITY_SIGNING_SECRET,
)
from .discussion_audit import (
    DiscussionAuditConflict,
    project_discussion_audit,
)
from .execution_boundary import ExecutionBoundaryViolation, ensure_safe_api_path
from .football_research_service import FootballResearchError, FootballResearchService
from .integration_manifest import (
    PLUGIN_REGISTRY_CATALOG_V3_PATH,
    STUDIO_INTEGRATION_MANIFEST_PATH,
    build_studio_integration_manifest,
)
from .instance_ownership import DatabaseInstanceOwner
from .material_ingest import MATERIAL_INGEST
from .manual_chatgpt import ManualChatGPTError, ManualChatGPTService
from .market.storage_service import STORAGE_MARKET
from .market.readiness import STORAGE_READINESS
from .observation_service import OBSERVATIONS
from .orchestrator import ORCHESTRATOR
from .paper_portfolio_service import PaperPortfolioService
from .plugin_lifecycle import PluginLifecycleError
from .plugin_registry import plugin_registry_catalog_v3
from .project_readiness import ProjectReadinessError, ProjectReadinessService
from .project_invocation import (
    PROJECT_INVOCATION_ACTION_INTAKE,
    PROJECT_INVOCATION_ACTION_RESULT_READ,
    PROJECT_INVOCATION_INTAKE_PATH,
    ProjectCapabilityAuthorizer,
    ProjectCapabilityClaims,
    ProjectInvocationError,
    normalize_project_invocation_envelope,
    project_invocation_semantics,
)
from .project_integration_service import (
    ProjectIntegrationError,
    ProjectIntegrationService,
)
from .project_round_focus import ProjectRoundFocusError, ProjectRoundFocusService
from .provider_call_ledger import ProviderCallLedger
from .provider_preflight import ProviderPreflightService
from .providers.registry import PROVIDERS
from .round_launch_plan import RoundLaunchPlanService
from .round_contexts import RoundContextError
from .stock_research_service import StockResearchError, StockResearchService
from .source_inbox_contracts import MAX_SOURCE_IMPORT_BYTES, SourceInboxContractError
from .source_inbox_import_ux import build_source_monitoring_prompt_template
from .source_inbox_service import SourceInboxError, SourceInboxService
from .source_monitoring.health_service import (
    SourceMonitoringHealthService,
    SourceMonitoringHealthServiceError,
)
from .source_monitoring.operations import (
    SourceMonitoringOperationsError,
    SourceMonitoringRetentionService,
)
from .source_monitoring.state_repository import SourceMonitoringStateError
from .storage_sample_acceptance import StorageSampleAcceptance
from .structured_logging import (
    classify_request_target,
    emit_event,
    safe_http_method,
)
from .store import (
    PROVIDER_OPERATION_BINDING_VERSION,
    STORE,
    MessageRoutingConflict,
    ProjectInvocationStoreError,
    RoundExecutionTraceConflict,
)
from .turn_envelope import (
    TURN_ENVELOPE_SCHEMA_SHA256,
    TURN_ENVELOPE_VERSION,
    normalize_turn_envelope_mode,
)
from .user_decision import (
    USER_DECISION_REQUEST_FIELDS,
    USER_DECISION_SELECTION_FIELDS,
)
from .walk_forward import WalkForwardFeasibilityError


# `content` is itself JSON text inside an outer JSON request string.  Allow a
# bounded 3x escape envelope (including ensure_ascii clients) plus fixed framing;
# the inner contract remains authoritative at exactly 256 KiB UTF-8.
_SOURCE_INBOX_HTTP_ENVELOPE_MAX_BYTES = (MAX_SOURCE_IMPORT_BYTES * 3) + 16_384
LOCAL_SESSION_TOKEN = secrets.token_urlsafe(32)
LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}
SERVICE_ID = "ai_collaboration_studio"
SERVICE_NAME = "AI 共创室"
SERVICE_VERSION = "0.1.0"
HOST_API_CONTRACT_VERSION = "host_delivery_v1"
HOST_READINESS_SCHEMA_VERSION = "host_readiness_v1"
HOST_VERSION_SCHEMA_VERSION = "host_version_v2"


def _is_source_inbox_path(path: str) -> bool:
    return bool(
        path in {
            "/api/monitoring/health",
            "/api/monitoring/inbox",
            "/api/monitoring/imports/chatgpt",
            "/api/monitoring/imports/chatgpt/preview",
            "/api/monitoring/imports/chatgpt/prompt-template",
            "/api/monitoring/notifications",
            "/api/monitoring/retention/attest",
            "/api/monitoring/retention/preview",
        }
        or re.fullmatch(r"/api/monitoring/events/[^/]+(?:/(?:acknowledge|attach|round-draft))?", path)
    )


def _backend_build_identity_at_startup() -> dict[str, Any]:
    """Freeze a path-free digest of the Python host source loaded at startup."""

    project_root = Path(__file__).resolve().parents[1]
    candidates = [project_root / "server.py"]
    candidates.extend((project_root / "backend").rglob("*.py"))
    relative_paths = sorted(
        path.relative_to(project_root).as_posix() for path in candidates
    )
    digest = hashlib.sha256()
    try:
        for relative_path in relative_paths:
            body = (project_root / relative_path).read_bytes()
            file_sha256 = hashlib.sha256(body).hexdigest()
            digest.update(relative_path.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file_sha256.encode("ascii"))
            digest.update(b"\n")
    except OSError:
        return {
            "available": False,
            "source_file_count": 0,
            "source_sha256": "",
        }
    return {
        "available": True,
        "source_file_count": len(relative_paths),
        "source_sha256": digest.hexdigest(),
    }


BACKEND_BUILD_IDENTITY_AT_STARTUP = _backend_build_identity_at_startup()


def frontend_build_identity() -> dict[str, Any]:
    """Return a bounded identity for the production frontend entrypoint."""

    index_path = FRONTEND_DIST / "index.html"
    try:
        body = index_path.read_bytes()
    except OSError:
        return {
            "available": False,
            "index_bytes": 0,
            "index_sha256": "",
        }
    return {
        "available": True,
        "index_bytes": len(body),
        "index_sha256": hashlib.sha256(body).hexdigest(),
    }


def host_version_payload() -> dict[str, Any]:
    """Describe the local host contract without reading providers or secrets."""

    return {
        "ok": True,
        "schema_version": HOST_VERSION_SCHEMA_VERSION,
        "service": {
            "id": SERVICE_ID,
            "name": SERVICE_NAME,
            "version": SERVICE_VERSION,
        },
        "api": {
            "contract_version": HOST_API_CONTRACT_VERSION,
            "readiness_schema_version": HOST_READINESS_SCHEMA_VERSION,
            "version_schema_version": HOST_VERSION_SCHEMA_VERSION,
        },
        "backend_build": dict(BACKEND_BUILD_IDENTITY_AT_STARTUP),
        "frontend_build": frontend_build_identity(),
    }


def _is_loopback_address(value: Any) -> bool:
    """Return whether an explicit bind or peer address is local-only.

    ``IPv6Address.is_loopback`` does not classify IPv4-mapped loopback
    addresses as loopback, so inspect the mapped IPv4 address separately.
    Hostnames other than the exact local alias are intentionally rejected to
    avoid turning this security decision into a DNS lookup.
    """

    raw = str(value or "").strip()
    if raw.lower() == "localhost":
        return True
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    raw = raw.split("%", 1)[0]
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def storage_sample_acceptance(
    room_id: str,
    *,
    snapshot: dict[str, Any],
    convergence_state: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate the storage sample against the current request-scoped store."""

    return StorageSampleAcceptance(
        STORE,
        ORCHESTRATOR.convergence,
    ).evaluate(
        room_id,
        snapshot=snapshot,
        convergence_state=convergence_state,
    )


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def project_capability_claims_sha256(claims: ProjectCapabilityClaims) -> str:
    """Seal verified claim semantics without retaining the bearer token."""

    projection = {
        "version": "project_capability_claims_digest_v1",
        "audience": claims.audience,
        "caller_id": claims.caller_id,
        "project_id": claims.project_id,
        "room_id": claims.room_id,
        "actions": list(claims.actions),
        "client_request_id": claims.client_request_id,
        "request_sha256": claims.request_sha256,
        "issued_at": claims.issued_at,
        "expires_at": claims.expires_at,
        "token_id": claims.token_id,
    }
    return hashlib.sha256(
        json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _member_provider_id(value: Any) -> str:
    """Normalize a member provider exactly as the store write path does."""

    return str(value or DEFAULT_PROVIDER).strip().lower()[:40]


def _validate_member_provider_assignment(provider_id: str) -> None:
    """Fail closed on unknown or policy-disabled provider assignments.

    Registry status is local metadata.  Deliberately do not call preflight,
    probe, generate, or infer whether a custom model exists here; real
    reachability and model access remain the round preflight's responsibility.
    """

    statuses = {
        _member_provider_id(status.get("id")): status
        for status in PROVIDERS.status()
        if isinstance(status, dict) and str(status.get("id") or "").strip()
    }
    status = statuses.get(provider_id)
    if status is None:
        raise ValueError(f"未知 Provider：{provider_id}")
    if status.get("policy_disabled") is True:
        raise ValueError(f"Provider 已被服务端策略禁用：{provider_id}")


class StudioRequestHandler(BaseHTTPRequestHandler):
    server_version = f"AICollaborationStudio/{SERVICE_VERSION}"
    _formal_execution_locks_guard = threading.Lock()
    _formal_execution_locks: dict[str, threading.Lock] = {}

    @classmethod
    def _formal_execution_lock(cls, room_id: str) -> threading.Lock:
        clean_room_id = str(room_id or "").strip()
        with cls._formal_execution_locks_guard:
            return cls._formal_execution_locks.setdefault(
                clean_room_id,
                threading.Lock(),
            )

    def _acquire_formal_execution(self, room_id: str) -> threading.Lock | None:
        lock = self._formal_execution_lock(room_id)
        if lock.acquire(blocking=False):
            return lock
        self._send_json(
            {
                "ok": False,
                "error_code": "ROUND_EXECUTION_BUSY",
                "error": "Another formal round or artifact operation is active for this room.",
            },
            HTTPStatus.CONFLICT,
        )
        return None

    def _require_plugin_action(self, room_id: str, action_id: str) -> bool:
        try:
            STORE.require_room_plugin_action(room_id, action_id)
        except LookupError as exc:
            self._send_json(
                {"ok": False, "error": str(exc)},
                HTTPStatus.NOT_FOUND,
            )
            return False
        except PluginLifecycleError as exc:
            self._send_json(
                {"ok": False, "error": str(exc), "code": exc.code},
                HTTPStatus(exc.status),
            )
            return False
        return True

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        try:
            status = int(getattr(code, "value", code))
        except (TypeError, ValueError):
            status = 0
        fields: dict[str, Any] = {
            "method": safe_http_method(getattr(self, "command", "")),
            "path_class": classify_request_target(getattr(self, "path", "")),
            "status": status if 100 <= status <= 599 else 0,
        }
        try:
            response_bytes = int(size)
        except (TypeError, ValueError):
            response_bytes = -1
        if response_bytes >= 0:
            fields["response_bytes"] = response_bytes
        emit_event("http_request_completed", fields=fields)

    def log_error(self, format: str, *args: Any) -> None:
        emit_event(
            "http_handler_error",
            severity="error",
            fields={
                "method": safe_http_method(getattr(self, "command", "")),
                "path_class": classify_request_target(getattr(self, "path", "")),
            },
        )

    def log_message(self, format: str, *args: Any) -> None:
        # Never format BaseHTTPRequestHandler messages because request lines,
        # headers, and exception text can contain caller-controlled material.
        return

    def _host_readiness_payload(self) -> dict[str, Any]:
        startup_ready = bool(
            getattr(self.server, "ai_studio_startup_ready", False)
        )
        database_ready = False
        if startup_ready:
            try:
                database_ready = Path(STORE.path).is_file()
            except (OSError, RuntimeError, TypeError, ValueError):
                database_ready = False
        frontend = frontend_build_identity()
        ready = bool(
            startup_ready
            and database_ready
            and frontend["available"]
        )
        return {
            "ok": ready,
            "ready": ready,
            "status": "ready" if ready else "not_ready",
            "schema_version": HOST_READINESS_SCHEMA_VERSION,
            "service": {
                "id": SERVICE_ID,
                "name": SERVICE_NAME,
                "version": SERVICE_VERSION,
            },
            "checks": {
                "startup_gate": {"ready": startup_ready},
                "database": {"ready": database_ready},
                "frontend_build": {
                    "ready": bool(frontend["available"]),
                    "index_bytes": int(frontend["index_bytes"]),
                    "index_sha256": str(frontend["index_sha256"]),
                },
            },
        }

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        if not self._guard_request(require_same_origin=True):
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-AI-Studio-Token")
        if _is_source_inbox_path(urlparse(self.path).path):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        project_result_match = re.fullmatch(
            r"/api/integration/project-invocations/([^/]+)/result",
            parsed.path,
        )
        if project_result_match:
            self._handle_project_invocation_result(
                parsed,
                project_result_match.group(1),
            )
            return
        if not self._guard_request():
            return
        query = parse_qs(parsed.query)
        if parsed.path in {
            "/api/readiness",
            "/api/version",
            STUDIO_INTEGRATION_MANIFEST_PATH,
        } and parsed.query:
            self._send_json(
                {
                    "ok": False,
                    "error": "Host delivery endpoints do not accept query parameters.",
                    "error_code": "HOST_ENDPOINT_QUERY_UNSUPPORTED",
                },
                HTTPStatus.BAD_REQUEST,
                cache_control="no-store",
            )
            return
        if parsed.path == "/api/readiness":
            payload = self._host_readiness_payload()
            self._send_json(
                payload,
                HTTPStatus.OK if payload["ready"] else HTTPStatus.SERVICE_UNAVAILABLE,
                cache_control="no-store",
            )
            return
        if parsed.path == "/api/version":
            self._send_json(
                host_version_payload(),
                cache_control="no-store",
            )
            return
        if parsed.path == STUDIO_INTEGRATION_MANIFEST_PATH:
            self._send_json(
                build_studio_integration_manifest(
                    service_id=SERVICE_ID,
                    service_name=SERVICE_NAME,
                    service_version=SERVICE_VERSION,
                    host_api_contract_version=HOST_API_CONTRACT_VERSION,
                ),
                cache_control="no-store",
            )
            return
        if parsed.path == PLUGIN_REGISTRY_CATALOG_V3_PATH:
            if parsed.query:
                self._send_json(
                    {
                        "ok": False,
                        "error": "Plugin registry catalog does not accept query parameters.",
                        "error_code": "PLUGIN_REGISTRY_QUERY_UNSUPPORTED",
                    },
                    HTTPStatus.BAD_REQUEST,
                    cache_control="no-store",
                )
                return
            self._send_json(
                {"ok": True, "catalog": plugin_registry_catalog_v3()},
                cache_control="no-store",
            )
            return
        if parsed.path == "/api/health":
            self._send_json({
                "ok": True,
                "service": SERVICE_NAME,
                "providers": PROVIDERS.status(),
            })
            return
        if parsed.path == "/api/bootstrap":
            payload = STORE.bootstrap((query.get("room") or [""])[0])
            if payload.get("active"):
                active = payload["active"]
                convergence = ORCHESTRATOR.convergence.evaluate(
                    str((active.get("room") or {}).get("id") or ""),
                    snapshot=active,
                )
                active["convergence"] = convergence
                active["storage_sample_acceptance"] = storage_sample_acceptance(
                    str((active.get("room") or {}).get("id") or ""),
                    snapshot=active,
                    convergence_state=convergence,
                )
            self._send_json({
                **payload,
                "providers": PROVIDERS.status(),
                "session_token": LOCAL_SESSION_TOKEN,
                "ok": True,
            })
            return
        if parsed.path == "/api/plugin-registry/lifecycle":
            self._send_json({
                "ok": True,
                "plugin_lifecycle": STORE.plugin_lifecycle_view(
                    include_history=True,
                ),
            })
            return
        if parsed.path == "/api/action-desk/overview":
            if parsed.query:
                self._send_json(
                    {
                        "ok": False,
                        "error": "Action Desk overview v1 does not accept query parameters.",
                        "code": "ACTION_DESK_OVERVIEW_QUERY_UNSUPPORTED",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                overview = ActionDeskService(STORE).overview()
            except ActionDeskError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "action_desk_overview": overview})
            return
        if parsed.path == "/api/monitoring/imports/chatgpt/prompt-template":
            if parsed.query:
                self._send_json(
                    {
                        "ok": False,
                        "error": "GPT 监控提示词模板不接受查询参数。",
                        "code": "SOURCE_INBOX_PROMPT_TEMPLATE_QUERY_UNSUPPORTED",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "source_monitoring_prompt_template": (
                        build_source_monitoring_prompt_template()
                    ),
                }
            )
            return
        if parsed.path == "/api/monitoring/health":
            if parsed.query:
                self._send_json(
                    {
                        "ok": False,
                        "error": "Source Monitoring health does not accept query parameters.",
                        "code": "SOURCE_MONITORING_HEALTH_QUERY_UNSUPPORTED",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                health = SourceMonitoringHealthService(STORE).snapshot()
            except (SourceMonitoringHealthServiceError, SourceMonitoringStateError) as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": getattr(
                            exc,
                            "code",
                            "SOURCE_MONITORING_HEALTH_UNAVAILABLE",
                        ),
                    },
                    HTTPStatus(getattr(exc, "status", 409)),
                )
                return
            self._send_json({"ok": True, "source_monitoring_health": health})
            return
        if parsed.path == "/api/monitoring/retention/preview":
            if parsed.query:
                self._send_json(
                    {
                        "ok": False,
                        "error": "Source Monitoring retention preview does not accept query parameters.",
                        "code": "SOURCE_MONITORING_RETENTION_QUERY_UNSUPPORTED",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                preview = SourceMonitoringRetentionService(STORE).preview()
            except SourceMonitoringOperationsError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({
                "ok": True,
                "source_monitoring_retention_preview": preview,
            })
            return
        if parsed.path == "/api/monitoring/notifications":
            notification_query = parse_qs(parsed.query, keep_blank_values=True)
            if (
                set(notification_query) - {"after", "limit"}
                or any(len(values) != 1 for values in notification_query.values())
            ):
                self._send_json(
                    {
                        "ok": False,
                        "error": "通知流只接受 after 和 limit。",
                        "code": "SOURCE_INBOX_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                limit = int((notification_query.get("limit") or ["50"])[0])
                notifications = SourceInboxService(STORE).list_notifications(
                    after=(
                        notification_query["after"][0]
                        if "after" in notification_query
                        else None
                    ),
                    limit=limit,
                )
            except (TypeError, ValueError, SourceInboxError) as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": getattr(exc, "code", "SOURCE_INBOX_REQUEST_INVALID"),
                    },
                    HTTPStatus(getattr(exc, "status", 400)),
                )
                return
            self._send_json({"ok": True, "source_notifications": notifications})
            return
        if parsed.path == "/api/monitoring/inbox":
            source_query = parse_qs(parsed.query, keep_blank_values=True)
            if (
                set(source_query) - {"state", "q", "source", "unread", "limit"}
                or any(len(values) != 1 for values in source_query.values())
            ):
                self._send_json(
                    {
                        "ok": False,
                        "error": "来源收件箱只接受 state、q、source、unread 和 limit。",
                        "code": "SOURCE_INBOX_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                limit = int((source_query.get("limit") or ["100"])[0])
                inbox = SourceInboxService(STORE).list_items(
                    state=(source_query.get("state") or [""])[0],
                    query=(source_query.get("q") or [""])[0],
                    source=(
                        source_query["source"][0]
                        if "source" in source_query
                        else None
                    ),
                    unread=(
                        source_query["unread"][0]
                        if "unread" in source_query
                        else None
                    ),
                    limit=limit,
                )
            except (TypeError, ValueError, SourceInboxError) as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": getattr(exc, "code", "SOURCE_INBOX_REQUEST_INVALID"),
                    },
                    HTTPStatus(getattr(exc, "status", 400)),
                )
                return
            self._send_json({"ok": True, "source_inbox": inbox})
            return
        source_inbox_item_match = re.fullmatch(
            r"/api/monitoring/events/([^/]+)",
            parsed.path,
        )
        if source_inbox_item_match:
            if parsed.query:
                self._send_json(
                    {
                        "ok": False,
                        "error": "来源事件详情不接受查询参数。",
                        "code": "SOURCE_INBOX_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                item = SourceInboxService(STORE).get_item(
                    source_inbox_item_match.group(1)
                )
            except SourceInboxError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            if item is None:
                self._send_json(
                    {
                        "ok": False,
                        "error": "来源事件不存在。",
                        "code": "SOURCE_INBOX_NOT_FOUND",
                    },
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"ok": True, "source_item": item})
            return
        manual_chatgpt_latest_match = re.fullmatch(
            r"/api/rooms/([^/]+)/chatgpt-collaborations/latest",
            parsed.path,
        )
        if manual_chatgpt_latest_match:
            try:
                session = ManualChatGPTService(STORE).latest(
                    manual_chatgpt_latest_match.group(1)
                )
            except ManualChatGPTError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "manual_chatgpt": session})
            return
        manual_chatgpt_list_match = re.fullmatch(
            r"/api/rooms/([^/]+)/chatgpt-collaborations",
            parsed.path,
        )
        if manual_chatgpt_list_match:
            if set(query) - {"limit"}:
                self._send_json(
                    {
                        "ok": False,
                        "error": "ChatGPT 协作任务列表只接受 limit。",
                        "code": "MANUAL_CHATGPT_LIST_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                limit = int((query.get("limit") or ["30"])[0])
                sessions = ManualChatGPTService(STORE).list(
                    manual_chatgpt_list_match.group(1),
                    limit=limit,
                )
            except (TypeError, ValueError, ManualChatGPTError) as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": getattr(
                            exc,
                            "code",
                            "MANUAL_CHATGPT_LIST_REQUEST_INVALID",
                        ),
                    },
                    HTTPStatus(getattr(exc, "status", 400)),
                )
                return
            self._send_json({"ok": True, "manual_chatgpt_sessions": sessions})
            return
        manual_chatgpt_record_match = re.fullmatch(
            r"/api/rooms/([^/]+)/chatgpt-collaborations/([^/]+)",
            parsed.path,
        )
        if manual_chatgpt_record_match:
            room_id, session_id = manual_chatgpt_record_match.groups()
            try:
                session = ManualChatGPTService(STORE).get(room_id, session_id)
            except ManualChatGPTError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            if session is None:
                self._send_json(
                    {"ok": False, "error": "ChatGPT 协作任务不存在。"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"ok": True, "manual_chatgpt": session})
            return
        action_desk_continuations_match = re.fullmatch(
            r"/api/rooms/([^/]+)/action-desk/continuations",
            parsed.path,
        )
        if action_desk_continuations_match:
            try:
                continuations = ActionDeskService(STORE).continuations(
                    action_desk_continuations_match.group(1),
                )
            except ActionDeskError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "continuations": continuations})
            return
        action_desk_match = re.fullmatch(
            r"/api/rooms/([^/]+)/action-desk",
            parsed.path,
        )
        if action_desk_match:
            try:
                desk = ActionDeskService(STORE).get(action_desk_match.group(1))
            except ActionDeskError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "action_desk": desk})
            return
        member_version_match = re.fullmatch(
            r"/api/rooms/([^/]+)/members/([^/]+)/versions/(\d+)",
            parsed.path,
        )
        if member_version_match:
            room_id, member_id, version = member_version_match.groups()
            try:
                payload = STORE.get_member_version_record(
                    room_id,
                    member_id,
                    int(version),
                )
            except ValueError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "error_code": "MEMBER_VERSION_CORRUPT",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if payload is None:
                self._send_json({"ok": False, "error": "成员身份版本不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, **payload})
            return
        member_versions_match = re.fullmatch(
            r"/api/rooms/([^/]+)/members/([^/]+)/versions",
            parsed.path,
        )
        if member_versions_match:
            try:
                limit = int((query.get("limit") or ["30"])[0])
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "limit 必须是整数"}, HTTPStatus.BAD_REQUEST)
                return
            payload = STORE.list_member_versions(
                member_versions_match.group(1),
                member_versions_match.group(2),
                limit=limit,
            )
            if payload is None:
                self._send_json({"ok": False, "error": "成员不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, **payload})
            return
        project_round_focus_record_match = re.fullmatch(
            r"/api/rooms/([^/]+)/rounds/([^/]+)/project-round-focus",
            parsed.path,
        )
        if project_round_focus_record_match:
            room_id, round_id = project_round_focus_record_match.groups()
            try:
                record = STORE.get_round_project_focus(room_id, round_id)
            except ProjectRoundFocusError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            if record is None:
                self._send_json(
                    {
                        "ok": False,
                        "error": "This round has no frozen project round-focus context.",
                        "code": "PROJECT_ROUND_FOCUS_NOT_BOUND",
                    },
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"ok": True, "project_round_focus": record})
            return
        project_round_focus_preview_match = re.fullmatch(
            r"/api/rooms/([^/]+)/project-round-focus",
            parsed.path,
        )
        if project_round_focus_preview_match:
            try:
                preview = ProjectRoundFocusService(STORE).preview(
                    project_round_focus_preview_match.group(1)
                )
            except ProjectRoundFocusError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "project_round_focus": preview})
            return
        project_readiness_match = re.fullmatch(
            r"/api/rooms/([^/]+)/artifacts/([^/]+)/versions/(\d+)/project-readiness",
            parsed.path,
        )
        if project_readiness_match:
            room_id, artifact_id, version = project_readiness_match.groups()
            try:
                projection = ProjectReadinessService(STORE).inspect(
                    room_id,
                    artifact_id,
                    int(version),
                )
            except ProjectReadinessError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "projection": projection})
            return
        artifact_version_match = re.fullmatch(
            r"/api/rooms/([^/]+)/artifacts/([^/]+)/versions/(\d+)",
            parsed.path,
        )
        if artifact_version_match:
            room_id, artifact_id, version = artifact_version_match.groups()
            try:
                payload = STORE.get_artifact_version(
                    room_id,
                    artifact_id,
                    int(version),
                )
            except ValueError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "error_code": "ARTIFACT_VERSION_CORRUPT",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if payload is None:
                self._send_json({"ok": False, "error": "会议产物版本不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, **payload})
            return
        artifact_versions_match = re.fullmatch(
            r"/api/rooms/([^/]+)/artifacts/([^/]+)/versions",
            parsed.path,
        )
        if artifact_versions_match:
            try:
                limit = int((query.get("limit") or ["30"])[0])
            except (TypeError, ValueError):
                self._send_json({"ok": False, "error": "limit 必须是整数"}, HTTPStatus.BAD_REQUEST)
                return
            payload = STORE.list_artifact_versions(
                artifact_versions_match.group(1),
                artifact_versions_match.group(2),
                limit=limit,
            )
            if payload is None:
                self._send_json({"ok": False, "error": "会议产物不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, **payload})
            return
        artifact_evidence_graph_match = re.fullmatch(
            r"/api/rooms/([^/]+)/artifacts/([^/]+)/evidence-graph",
            parsed.path,
        )
        if artifact_evidence_graph_match:
            room_id, artifact_id = artifact_evidence_graph_match.groups()
            try:
                graph = STORE.artifact_evidence_graph(room_id, artifact_id)
            except ValueError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "error_code": "ARTIFACT_EVIDENCE_GRAPH_INVALID",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if graph is None:
                self._send_json(
                    {"ok": False, "error": "Artifact not found"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"ok": True, **graph})
            return
        artifact_source_detail_match = re.fullmatch(
            r"/api/rooms/([^/]+)/artifacts/([^/]+)/evidence-sources/([^/]+)/([^/]+)",
            parsed.path,
        )
        if artifact_source_detail_match:
            room_id, artifact_id, source_type, source_id = (
                artifact_source_detail_match.groups()
            )
            try:
                detail = STORE.artifact_evidence_source_detail(
                    room_id,
                    artifact_id,
                    source_type,
                    source_id,
                )
            except LookupError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            if detail is None:
                self._send_json({"ok": False, "error": "会议产物不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, **detail})
            return
        artifact_sources_match = re.fullmatch(
            r"/api/rooms/([^/]+)/artifacts/([^/]+)/evidence-sources",
            parsed.path,
        )
        if artifact_sources_match:
            room_id, artifact_id = artifact_sources_match.groups()
            try:
                evidence_sources = STORE.artifact_evidence_sources(room_id, artifact_id)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            if evidence_sources is None:
                self._send_json({"ok": False, "error": "会议产物不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, **evidence_sources})
            return
        if parsed.path == "/api/market/futu/status":
            self._send_json({"ok": True, "status": STORAGE_MARKET.status()})
            return
        if parsed.path == "/api/market/storage/readiness":
            force = (query.get("force") or [""])[0].lower() in {"1", "true", "yes"}
            room_id = str((query.get("room") or [""])[0] or "").strip()
            room_snapshot = STORE.room_snapshot(room_id) if room_id else None
            if room_id and room_snapshot is None:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            readiness = (
                STORAGE_READINESS.inspect(force=force, room_snapshot=room_snapshot)
                if room_snapshot is not None
                else STORAGE_READINESS.inspect(force=force)
            )
            self._send_json({"ok": True, "readiness": readiness})
            return
        if parsed.path == "/api/market/storage/snapshot":
            force = (query.get("force") or [""])[0].lower() in {"1", "true", "yes"}
            self._send_json({"ok": True, "snapshot": STORAGE_MARKET.snapshot(force=force)})
            return
        if parsed.path == "/api/market/storage/history":
            symbol = (query.get("symbol") or [""])[0]
            start = (query.get("start") or [None])[0]
            end = (query.get("end") or [None])[0]
            try:
                limit = int((query.get("limit") or ["120"])[0])
                history = STORAGE_MARKET.history(symbol, start=start, end=end, limit=limit)
            except (TypeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "history": history})
            return
        if parsed.path == "/api/market/storage/financials":
            symbol = (query.get("symbol") or [""])[0]
            statement_type = (query.get("statement") or ["main_index"])[0]
            try:
                limit = int((query.get("limit") or ["4"])[0])
                financials = STORAGE_MARKET.financials(
                    symbol,
                    statement_type=statement_type,
                    limit=limit,
                )
            except (TypeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "financials": financials})
            return
        if parsed.path == "/api/market/storage/filings":
            symbol = (query.get("symbol") or [""])[0]
            raw_forms = (query.get("forms") or [""])[0]
            forms = [item.strip() for item in raw_forms.split(",") if item.strip()] or None
            force = (query.get("force") or [""])[0].lower() in {"1", "true", "yes"}
            try:
                limit = int((query.get("limit") or ["8"])[0])
                filings = STORAGE_MARKET.filings(
                    symbol,
                    forms=forms,
                    limit=limit,
                    force=force,
                )
            except (TypeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "filings": filings})
            return
        if parsed.path == "/api/market/storage/revenue-breakdown":
            symbol = (query.get("symbol") or [""])[0]
            try:
                breakdown = STORAGE_MARKET.revenue_breakdown(symbol)
            except (TypeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "revenue_breakdown": breakdown})
            return
        if parsed.path == "/api/market/storage/ir-releases":
            symbol = (query.get("symbol") or [""])[0]
            force = (query.get("force") or [""])[0].lower() in {"1", "true", "yes"}
            try:
                limit = int((query.get("limit") or ["8"])[0])
                releases = STORAGE_MARKET.ir_releases(symbol, limit=limit, force=force)
            except (TypeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "ir_releases": releases})
            return
        if parsed.path == "/api/market/storage/earnings-packs":
            symbol = (query.get("symbol") or [""])[0]
            force = (query.get("force") or [""])[0].lower() in {"1", "true", "yes"}
            try:
                limit = int((query.get("limit") or ["12"])[0])
                earnings_packs = STORAGE_MARKET.earnings_packs(symbol, limit=limit, force=force)
            except (TypeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "earnings_packs": earnings_packs})
            return
        if parsed.path == "/api/market/storage/earnings-materials":
            symbol = (query.get("symbol") or [""])[0]
            force = (query.get("force") or [""])[0].lower() in {"1", "true", "yes"}
            try:
                limit = int((query.get("limit") or ["24"])[0])
                earnings_materials = STORAGE_MARKET.earnings_materials(
                    symbol,
                    limit=limit,
                    force=force,
                )
            except (TypeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "earnings_materials": earnings_materials})
            return
        if parsed.path == "/api/market/storage/industry-proxies":
            force = (query.get("force") or [""])[0].lower() in {"1", "true", "yes"}
            self._send_json({"ok": True, "industry_proxies": STORAGE_MARKET.industry_proxies(force=force)})
            return
        candidate_experiment_match = re.fullmatch(
            r"/api/rooms/([^/]+)/candidate-experiments/([^/]+)",
            parsed.path,
        )
        if candidate_experiment_match:
            room_id, cohort_id = candidate_experiment_match.groups()
            try:
                experiment = CandidateExperimentService(
                    STORE,
                    STORAGE_MARKET,
                ).get(room_id, cohort_id)
            except CandidateExperimentError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "experiment": experiment})
            return
        paper_portfolio_walk_forward_match = re.fullmatch(
            r"/api/rooms/([^/]+)/paper-portfolios/([^/]+)/walk-forward",
            parsed.path,
        )
        if paper_portfolio_walk_forward_match:
            room_id, portfolio_id = paper_portfolio_walk_forward_match.groups()
            portfolio = STORE.get_paper_portfolio(room_id, portfolio_id)
            if not portfolio:
                self._send_json({"ok": False, "error": "模拟组合不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "ok": True,
                "portfolio": portfolio,
                "walk_forward_runs": STORE.list_paper_portfolio_walk_forward_runs(
                    room_id,
                    portfolio_id,
                ),
            })
            return
        paper_portfolio_versions_match = re.fullmatch(
            r"/api/rooms/([^/]+)/paper-portfolios/([^/]+)/versions",
            parsed.path,
        )
        if paper_portfolio_versions_match:
            room_id, portfolio_id = paper_portfolio_versions_match.groups()
            portfolio = STORE.get_paper_portfolio(room_id, portfolio_id)
            if not portfolio:
                self._send_json({"ok": False, "error": "模拟组合不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "ok": True,
                "portfolio": portfolio,
                "versions": STORE.list_paper_portfolio_versions(room_id, portfolio_id),
            })
            return
        paper_portfolios_match = re.fullmatch(
            r"/api/rooms/([^/]+)/paper-portfolios",
            parsed.path,
        )
        if paper_portfolios_match:
            room_id = paper_portfolios_match.group(1)
            room = STORE.room_snapshot(room_id)
            if not room:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "ok": True,
                "paper_portfolios": room.get("paper_portfolios") or [],
            })
            return
        decision_packages_match = re.fullmatch(
            r"/api/rooms/([^/]+)/decision-packages",
            parsed.path,
        )
        if decision_packages_match:
            room_id = decision_packages_match.group(1)
            room = STORE.room_snapshot(room_id)
            if not room:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "ok": True,
                "decision_packages": room.get("decision_packages") or [],
            })
            return
        observations_match = re.fullmatch(r"/api/rooms/([^/]+)/observations", parsed.path)
        if observations_match:
            room = STORE.room_snapshot(observations_match.group(1))
            if not room:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "ok": True,
                "observations": room.get("observations") or [],
                "reflections": room.get("reflections") or [],
                "scorecard": room.get("observation_scorecard") or {},
            })
            return
        material_version_match = re.fullmatch(r"/api/rooms/([^/]+)/materials/([^/]+)/versions/(\d+)", parsed.path)
        if material_version_match:
            material = STORE.get_material_version(
                material_version_match.group(1),
                material_version_match.group(2),
                int(material_version_match.group(3)),
            )
            if not material:
                self._send_json({"ok": False, "error": "资料版本不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "material": material})
            return
        material_versions_match = re.fullmatch(r"/api/rooms/([^/]+)/materials/([^/]+)/versions", parsed.path)
        if material_versions_match:
            material = STORE.get_material(material_versions_match.group(1), material_versions_match.group(2))
            if not material:
                self._send_json({"ok": False, "error": "资料不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "ok": True,
                "versions": STORE.list_material_versions(material_versions_match.group(1), material_versions_match.group(2)),
            })
            return
        discussion_audit_match = re.fullmatch(
            r"/api/rooms/([^/]+)/rounds/([^/]+)/discussion-audit",
            parsed.path,
        )
        if discussion_audit_match:
            room_id, round_id = discussion_audit_match.groups()
            try:
                trace = STORE.round_execution_trace(
                    room_id,
                    round_id,
                    limit=500,
                    cursor="",
                )
            except RoundExecutionTraceConflict:
                self._send_json(
                    {
                        "ok": False,
                        "error": "discussion audit inputs failed integrity checks",
                        "error_code": "DISCUSSION_AUDIT_CONFLICT",
                        "conflict_code": "ROUND_EXECUTION_TRACE_CONFLICT",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            except (TypeError, ValueError):
                self._send_json(
                    {
                        "ok": False,
                        "error": "discussion audit inputs failed integrity checks",
                        "error_code": "DISCUSSION_AUDIT_CONFLICT",
                        "conflict_code": "ROUND_EXECUTION_TRACE_READ_CONFLICT",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if trace is None:
                self._send_json(
                    {"ok": False, "error": "房间或讨论轮次不存在"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            try:
                contract_bundle = STORE.round_turn_contract_bundle(
                    room_id,
                    round_id,
                )
                discussion_audit = project_discussion_audit(
                    trace,
                    contract_bundle,
                    expected_room_id=room_id,
                    expected_round_id=round_id,
                )
            except DiscussionAuditConflict as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "error_code": "DISCUSSION_AUDIT_CONFLICT",
                        "conflict_code": exc.code,
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            except (TypeError, ValueError):
                self._send_json(
                    {
                        "ok": False,
                        "error": "discussion audit inputs failed integrity checks",
                        "error_code": "DISCUSSION_AUDIT_CONFLICT",
                        "conflict_code": "TURN_CONTRACT_BUNDLE_CONFLICT",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            self._send_json({
                "ok": True,
                "discussion_audit": discussion_audit,
            })
            return
        execution_trace_match = re.fullmatch(
            r"/api/rooms/([^/]+)/rounds/([^/]+)/audit-trace",
            parsed.path,
        )
        if execution_trace_match:
            room_id, round_id = execution_trace_match.groups()
            try:
                limit = int((query.get("limit") or ["200"])[0])
                trace = STORE.round_execution_trace(
                    room_id,
                    round_id,
                    limit=limit,
                    cursor=(query.get("cursor") or [""])[0],
                )
            except RoundExecutionTraceConflict as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "error_code": "ROUND_EXECUTION_TRACE_CONFLICT",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            except (TypeError, ValueError) as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if trace is None:
                self._send_json(
                    {"ok": False, "error": "房间或讨论轮次不存在"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"ok": True, "trace": trace})
            return
        message_history_match = re.fullmatch(r"/api/rooms/([^/]+)/messages", parsed.path)
        if message_history_match:
            try:
                limit = int((query.get("limit") or ["30"])[0])
                result = STORE.message_history(
                    message_history_match.group(1),
                    limit=limit,
                    before=(query.get("before") or [""])[0],
                    query=(query.get("q") or [""])[0],
                )
            except (TypeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if result is None:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, **result})
            return
        convergence_match = re.fullmatch(r"/api/rooms/([^/]+)/convergence", parsed.path)
        if convergence_match:
            room_id = convergence_match.group(1)
            room = STORE.room_snapshot(room_id)
            if not room:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            convergence = ORCHESTRATOR.convergence.evaluate(room_id, snapshot=room)
            self._send_json({
                "ok": True,
                "convergence": convergence,
                "storage_sample_acceptance": storage_sample_acceptance(
                    room_id,
                    snapshot=room,
                    convergence_state=convergence,
                ),
            })
            return
        acceptance_match = re.fullmatch(
            r"/api/rooms/([^/]+)/storage-sample-acceptance",
            parsed.path,
        )
        if acceptance_match:
            room_id = acceptance_match.group(1)
            room = STORE.room_snapshot(room_id)
            if not room:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            convergence = ORCHESTRATOR.convergence.evaluate(room_id, snapshot=room)
            self._send_json({
                "ok": True,
                "storage_sample_acceptance": storage_sample_acceptance(
                    room_id,
                    snapshot=room,
                    convergence_state=convergence,
                ),
            })
            return
        room_version_match = re.fullmatch(r"/api/rooms/([^/]+)/versions/(\d+)", parsed.path)
        if room_version_match:
            try:
                result = STORE.get_room_version_record(
                    room_version_match.group(1),
                    int(room_version_match.group(2)),
                )
            except ValueError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "error_code": "ROOM_VERSION_CORRUPT",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if not result:
                self._send_json({"ok": False, "error": "房间设置版本不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, **result})
            return
        room_versions_match = re.fullmatch(r"/api/rooms/([^/]+)/versions", parsed.path)
        if room_versions_match:
            try:
                limit = int((query.get("limit") or ["30"])[0])
                result = STORE.list_room_versions(room_versions_match.group(1), limit=limit)
            except (TypeError, ValueError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if not result:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, **result})
            return
        room_plugin_registry_match = re.fullmatch(
            r"/api/rooms/([^/]+)/plugin-registry",
            parsed.path,
        )
        if room_plugin_registry_match:
            result = STORE.room_plugin_registry(room_plugin_registry_match.group(1))
            if not result:
                self._send_json(
                    {"ok": False, "error": "房间不存在"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"ok": True, "plugin_registry": result})
            return
        room_match = re.fullmatch(r"/api/rooms/([^/]+)", parsed.path)
        if room_match:
            room = STORE.room_snapshot(room_match.group(1))
            if not room:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            convergence = ORCHESTRATOR.convergence.evaluate(room_match.group(1), snapshot=room)
            room["convergence"] = convergence
            room["storage_sample_acceptance"] = storage_sample_acceptance(
                room_match.group(1),
                snapshot=room,
                convergence_state=convergence,
            )
            self._send_json({"ok": True, **room, "providers": PROVIDERS.status()})
            return
        if parsed.path == "/api" or parsed.path.startswith("/api/"):
            self._send_json(
                {
                    "ok": False,
                    "error": "API endpoint not found.",
                    "error_code": "API_NOT_FOUND",
                },
                HTTPStatus.NOT_FOUND,
            )
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == PROJECT_INVOCATION_INTAKE_PATH:
            self._handle_project_invocation_intake(parsed)
            return
        football_research_match = re.fullmatch(
            r"/api/rooms/([^/]+)/football-research/inspect",
            parsed.path,
        )
        stock_research_match = re.fullmatch(
            r"/api/rooms/([^/]+)/stock-research/inspect",
            parsed.path,
        )
        readonly_research_match = football_research_match or stock_research_match
        if not self._guard_request(
            mutating=readonly_research_match is None,
            require_same_origin=readonly_research_match is not None,
        ):
            return
        is_file_import = bool(re.fullmatch(r"/api/rooms/[^/]+/materials/import-file", parsed.path))
        is_manual_chatgpt_import = bool(re.fullmatch(
            r"/api/rooms/[^/]+/chatgpt-collaborations/[^/]+/imports",
            parsed.path,
        ))
        is_source_inbox_import = parsed.path == "/api/monitoring/imports/chatgpt"
        is_source_inbox_import_preview = (
            parsed.path == "/api/monitoring/imports/chatgpt/preview"
        )
        is_monitoring_retention_attest = (
            parsed.path == "/api/monitoring/retention/attest"
        )
        is_source_inbox_request = bool(
            is_source_inbox_import
            or is_source_inbox_import_preview
            or is_monitoring_retention_attest
            or re.fullmatch(
                r"/api/monitoring/events/[^/]+/(?:acknowledge|attach|round-draft)",
                parsed.path,
            )
        )
        carries_round_context = bool(re.fullmatch(
            r"/api/rooms/[^/]+/(?:round-launch-plan|providers/preflight|rounds/stream)",
            parsed.path,
        ))
        payload = self._read_json(
            max_bytes=(
                3_000_000
                if is_file_import
                else 1_000_000
                if readonly_research_match is not None or carries_round_context
                else 256_000
                if is_manual_chatgpt_import
                else _SOURCE_INBOX_HTTP_ENVELOPE_MAX_BYTES
                if is_source_inbox_import or is_source_inbox_import_preview
                else 128_000
            ),
            strict=is_source_inbox_request,
        )
        if payload is None:
            return
        if is_source_inbox_import or is_source_inbox_import_preview:
            if set(payload) != {"content"} or type(payload.get("content")) is not str:
                self._send_json(
                    {
                        "ok": False,
                        "error": (
                            "来源预览请求必须只包含字符串 content。"
                            if is_source_inbox_import_preview
                            else "来源导入请求必须只包含字符串 content。"
                        ),
                        "code": "SOURCE_INBOX_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                source_inbox_service = SourceInboxService(STORE)
                result = (
                    source_inbox_service.preview_packet(payload["content"])
                    if is_source_inbox_import_preview
                    else source_inbox_service.import_packet(payload["content"])
                )
            except SourceInboxContractError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": exc.code,
                        "issues": [item.as_dict() for item in exc.issues],
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            except SourceInboxError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            if is_source_inbox_import_preview:
                self._send_json(
                    {"ok": True, "source_import_preview": result},
                    HTTPStatus.OK,
                )
                return
            self._send_json(
                {"ok": True, "source_import": result},
                HTTPStatus.OK if result["idempotent_replay"] else HTTPStatus.CREATED,
            )
            return
        if is_monitoring_retention_attest:
            if parsed.query or set(payload) != {"preview", "confirmation"}:
                self._send_json(
                    {
                        "ok": False,
                        "error": "Retention attestation requires only preview and confirmation.",
                        "code": "SOURCE_MONITORING_RETENTION_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                result = SourceMonitoringRetentionService(STORE).attest(
                    payload.get("preview"),
                    confirmation=payload.get("confirmation"),
                )
            except SourceMonitoringOperationsError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "source_monitoring_retention_attestation": result,
                },
                (
                    HTTPStatus.OK
                    if result["idempotent_replay"]
                    else HTTPStatus.CREATED
                ),
            )
            return
        source_inbox_ack_match = re.fullmatch(
            r"/api/monitoring/events/([^/]+)/acknowledge",
            parsed.path,
        )
        if source_inbox_ack_match:
            if set(payload) != {"expected_state_version", "acknowledgement"}:
                self._send_json(
                    {
                        "ok": False,
                        "error": "已阅确认字段不完整或包含额外字段。",
                        "code": "SOURCE_INBOX_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                item = SourceInboxService(STORE).acknowledge(
                    source_inbox_ack_match.group(1),
                    expected_state_version=payload.get("expected_state_version"),
                    acknowledgement=payload.get("acknowledgement"),
                )
            except SourceInboxError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "source_item": item})
            return
        source_inbox_attach_match = re.fullmatch(
            r"/api/monitoring/events/([^/]+)/attach",
            parsed.path,
        )
        if source_inbox_attach_match:
            if set(payload) != {"room_id", "expected_state_version"}:
                self._send_json(
                    {
                        "ok": False,
                        "error": "来源附加请求字段不完整或包含额外字段。",
                        "code": "SOURCE_INBOX_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                result = SourceInboxService(STORE).attach_to_room(
                    source_inbox_attach_match.group(1),
                    room_id=payload.get("room_id"),
                    expected_state_version=payload.get("expected_state_version"),
                )
            except SourceInboxError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json(
                {"ok": True, **result},
                HTTPStatus.OK if result["idempotent_replay"] else HTTPStatus.CREATED,
            )
            return
        source_inbox_draft_match = re.fullmatch(
            r"/api/monitoring/events/([^/]+)/round-draft",
            parsed.path,
        )
        if source_inbox_draft_match:
            if not set(payload).issubset({"room_id", "expected_state_version", "objective"}) or (
                set(payload) - {"objective"}
                != {"room_id", "expected_state_version"}
            ) or ("objective" in payload and type(payload.get("objective")) is not str):
                self._send_json(
                    {
                        "ok": False,
                        "error": "轮次草稿请求字段不完整或包含额外字段。",
                        "code": "SOURCE_INBOX_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                result = SourceInboxService(STORE).create_round_draft(
                    source_inbox_draft_match.group(1),
                    room_id=payload.get("room_id"),
                    expected_state_version=payload.get("expected_state_version"),
                    objective=payload.get("objective", ""),
                )
            except SourceInboxError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json(
                {"ok": True, **result},
                HTTPStatus.OK if result["idempotent_replay"] else HTTPStatus.CREATED,
            )
            return
        if football_research_match:
            if set(payload) != {"payload"}:
                self._send_json(
                    {
                        "ok": False,
                        "error": "足球研究检查请求必须只包含 payload。",
                        "code": "FOOTBALL_RESEARCH_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                view_model = FootballResearchService(STORE).inspect(
                    football_research_match.group(1),
                    payload.get("payload"),
                )
            except FootballResearchError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "football_research": view_model})
            return
        if stock_research_match:
            if set(payload) != {"payload"}:
                self._send_json(
                    {
                        "ok": False,
                        "error": "Stock research inspection accepts only payload.",
                        "code": "STOCK_RESEARCH_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                view_model = StockResearchService(STORE).inspect(
                    stock_research_match.group(1),
                    payload.get("payload"),
                )
            except StockResearchError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "stock_research": view_model})
            return
        manual_chatgpt_create_match = re.fullmatch(
            r"/api/rooms/([^/]+)/chatgpt-collaborations",
            parsed.path,
        )
        if manual_chatgpt_create_match:
            if not set(payload).issubset({"objective", "mode"}):
                self._send_json(
                    {
                        "ok": False,
                        "error": "ChatGPT 协作创建请求只接受 objective 和 mode。",
                        "code": "MANUAL_CHATGPT_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                session = ManualChatGPTService(STORE).create(
                    manual_chatgpt_create_match.group(1),
                    objective=payload.get("objective"),
                    mode=payload.get("mode", "standard"),
                )
            except LookupError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.NOT_FOUND,
                )
                return
            except ManualChatGPTError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": exc.code,
                        "issues": [item.as_dict() for item in exc.issues],
                    },
                    HTTPStatus(exc.status),
                )
                return
            self._send_json(
                {"ok": True, "manual_chatgpt": session},
                HTTPStatus.CREATED,
            )
            return
        manual_chatgpt_dispatch_match = re.fullmatch(
            r"/api/rooms/([^/]+)/chatgpt-collaborations/([^/]+)/dispatch",
            parsed.path,
        )
        if manual_chatgpt_dispatch_match:
            if payload:
                self._send_json(
                    {
                        "ok": False,
                        "error": "复制/打开确认请求不接受额外字段。",
                        "code": "MANUAL_CHATGPT_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            room_id, session_id = manual_chatgpt_dispatch_match.groups()
            try:
                session = ManualChatGPTService(STORE).dispatch(room_id, session_id)
            except LookupError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.NOT_FOUND,
                )
                return
            except ManualChatGPTError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "manual_chatgpt": session})
            return
        manual_chatgpt_import_match = re.fullmatch(
            r"/api/rooms/([^/]+)/chatgpt-collaborations/([^/]+)/imports",
            parsed.path,
        )
        if manual_chatgpt_import_match:
            if set(payload) != {"content"} or not isinstance(payload.get("content"), str):
                self._send_json(
                    {
                        "ok": False,
                        "error": "ChatGPT 导入请求必须只包含字符串 content。",
                        "code": "MANUAL_CHATGPT_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            room_id, session_id = manual_chatgpt_import_match.groups()
            try:
                session = ManualChatGPTService(STORE).import_result(
                    room_id,
                    session_id,
                    payload["content"],
                )
            except LookupError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.NOT_FOUND,
                )
                return
            except ManualChatGPTError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": exc.code,
                        "issues": [item.as_dict() for item in exc.issues],
                    },
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({
                "ok": True,
                "accepted": session.get("state") == "API_REVIEW",
                "manual_chatgpt": session,
            })
            return
        manual_chatgpt_review_match = re.fullmatch(
            r"/api/rooms/([^/]+)/chatgpt-collaborations/([^/]+)/api-reviews",
            parsed.path,
        )
        if manual_chatgpt_review_match:
            allowed_fields = {
                "provider", "model", "client_request_id", "expected_result_sha256",
            }
            if (
                set(payload) != allowed_fields
                or not all(isinstance(payload.get(field), str) for field in allowed_fields)
            ):
                self._send_json(
                    {
                        "ok": False,
                        "error": "独立 API 审查请求字段不完整或类型无效。",
                        "code": "MANUAL_CHATGPT_REVIEW_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            room_id, session_id = manual_chatgpt_review_match.groups()
            try:
                session = ManualChatGPTService(
                    STORE,
                    providers=PROVIDERS,
                ).run_api_review(
                    room_id,
                    session_id,
                    provider_id=payload["provider"],
                    model=payload["model"],
                    client_request_id=payload["client_request_id"],
                    expected_result_sha256=payload["expected_result_sha256"],
                )
            except LookupError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.NOT_FOUND,
                )
                return
            except ManualChatGPTError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": exc.code,
                        "issues": [item.as_dict() for item in exc.issues],
                    },
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "manual_chatgpt": session})
            return
        manual_chatgpt_review_recovery_match = re.fullmatch(
            r"/api/rooms/([^/]+)/chatgpt-collaborations/([^/]+)/api-reviews/recover",
            parsed.path,
        )
        if manual_chatgpt_review_recovery_match:
            allowed_fields = {"expected_result_sha256", "acknowledgement"}
            if (
                set(payload) != allowed_fields
                or not all(
                    type(payload.get(field)) is str for field in allowed_fields
                )
            ):
                self._send_json(
                    {
                        "ok": False,
                        "error": "审查恢复请求字段不完整或类型无效。",
                        "code": "MANUAL_CHATGPT_REVIEW_RECOVERY_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            room_id, session_id = manual_chatgpt_review_recovery_match.groups()
            try:
                session = ManualChatGPTService(STORE).recover_api_review(
                    room_id,
                    session_id,
                    expected_result_sha256=payload["expected_result_sha256"],
                    acknowledgement=payload["acknowledgement"],
                )
            except LookupError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.NOT_FOUND,
                )
                return
            except ManualChatGPTError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "manual_chatgpt": session})
            return
        manual_chatgpt_freeze_match = re.fullmatch(
            r"/api/rooms/([^/]+)/chatgpt-collaborations/([^/]+)/freeze",
            parsed.path,
        )
        if manual_chatgpt_freeze_match:
            allowed_fields = {
                "expected_result_sha256",
                "decision_card_sha256",
                "selected_option_id",
                "acknowledgement",
            }
            if (
                set(payload) != allowed_fields
                or not all(isinstance(payload.get(field), str) for field in allowed_fields)
            ):
                self._send_json(
                    {
                        "ok": False,
                        "error": "用户冻结请求字段不完整或类型无效。",
                        "code": "MANUAL_CHATGPT_FREEZE_REQUEST_INVALID",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            room_id, session_id = manual_chatgpt_freeze_match.groups()
            try:
                session = ManualChatGPTService(STORE).freeze_decision(
                    room_id,
                    session_id,
                    expected_result_sha256=payload["expected_result_sha256"],
                    decision_card_sha256=payload["decision_card_sha256"],
                    selected_option_id=payload["selected_option_id"],
                    acknowledgement=payload["acknowledgement"],
                )
            except LookupError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.NOT_FOUND,
                )
                return
            except ManualChatGPTError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": exc.code,
                        "issues": [item.as_dict() for item in exc.issues],
                    },
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "manual_chatgpt": session})
            return
        if parsed.path == "/api/plugin-registry/lifecycle-events/preview":
            try:
                preview = STORE.preview_plugin_lifecycle(payload)
            except PluginLifecycleError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json({"ok": True, "preview": preview})
            return
        if parsed.path == "/api/plugin-registry/lifecycle-events":
            try:
                result, created = STORE.transition_plugin_lifecycle(payload)
            except PluginLifecycleError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json(
                {"ok": True, "transition": result, "idempotent_replay": not created},
                HTTPStatus.CREATED if created else HTTPStatus.OK,
            )
            return
        action_desk_transition_match = re.fullmatch(
            r"/api/rooms/([^/]+)/action-desk/transitions",
            parsed.path,
        )
        if action_desk_transition_match:
            try:
                transition, created = ActionDeskService(STORE).transition(
                    action_desk_transition_match.group(1),
                    payload,
                )
            except ActionDeskError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "transition": transition,
                    "idempotent_replay": not created,
                },
                HTTPStatus.CREATED if created else HTTPStatus.OK,
            )
            return
        action_desk_continuation_match = re.fullmatch(
            r"/api/rooms/([^/]+)/action-desk/continuations",
            parsed.path,
        )
        if action_desk_continuation_match:
            try:
                continuation, created = ActionDeskService(STORE).continue_action(
                    action_desk_continuation_match.group(1),
                    payload,
                )
            except ActionDeskError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            self._send_json(
                {
                    "ok": True,
                    "continuation": continuation,
                    "idempotent_replay": not created,
                },
                HTTPStatus.CREATED if created else HTTPStatus.OK,
            )
            return
        if parsed.path == "/api/rooms":
            try:
                room = STORE.create_room(
                    str(payload.get("title") or ""),
                    str(payload.get("objective") or ""),
                    domain=str(payload.get("domain") or ""),
                    category=str(payload.get("category") or ""),
                    template_id=str(payload.get("template_id") or payload.get("domain") or "open_collaboration"),
                    workflow_policy=payload.get("workflow_policy") if "workflow_policy" in payload else None,
                    capability_pack_ids=(
                        payload.get("capability_pack_ids")
                        if "capability_pack_ids" in payload
                        else None
                    ),
                    stock_room_scope=(
                        payload.get("stock_room_scope")
                        if "stock_room_scope" in payload
                        else None
                    ),
                )
            except PluginLifecycleError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, **room}, HTTPStatus.CREATED)
            return
        round_launch_plan_match = re.fullmatch(
            r"/api/rooms/([^/]+)/round-launch-plan",
            parsed.path,
        )
        if round_launch_plan_match:
            if "member_ids" in payload:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_MEMBER_IDS_NOT_ALLOWED",
                        "error": "Round launch plans always freeze every enabled room member.",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                skip_provider_ids = self._skip_provider_ids(payload)
                plan = RoundLaunchPlanService(
                    STORE,
                    self._round_provider_registry(),
                ).build(
                    round_launch_plan_match.group(1),
                    str(payload.get("objective") or payload.get("content") or ""),
                    skip_provider_ids,
                    payload.get("project_round_focus_authorization"),
                    round_context_authorizations=payload.get(
                        "round_context_authorizations"
                    ),
                )
            except (
                ProjectRoundFocusError,
                RoundContextError,
                FootballResearchError,
                StockResearchError,
            ) as exc:
                self._send_json(
                    {"ok": False, "error_code": exc.code, "error": str(exc)},
                    HTTPStatus(exc.status),
                )
                return
            except Exception:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_LAUNCH_PLAN_INVALID",
                        "error": "The round launch plan could not be built from this request.",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json({"ok": True, "plan": plan})
            return
        provider_preflight_match = re.fullmatch(
            r"/api/rooms/([^/]+)/providers/preflight",
            parsed.path,
        )
        if provider_preflight_match:
            raw_member_ids = payload.get("member_ids")
            if raw_member_ids is not None and not isinstance(raw_member_ids, list):
                self._send_json(
                    {"ok": False, "error": "member_ids 必须是数组。"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                skip_provider_ids = self._skip_provider_ids(payload)
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                plan = RoundLaunchPlanService(
                    STORE,
                    self._round_provider_registry(),
                ).build(
                    provider_preflight_match.group(1),
                    str(payload.get("objective") or "Local configuration check"),
                    skip_provider_ids,
                    configuration_only=True,
                )
                preflight = self._local_configuration_preflight(
                    plan,
                    member_ids=(
                        [str(member_id or "").strip() for member_id in raw_member_ids[:100]]
                        if isinstance(raw_member_ids, list)
                        else None
                    ),
                )
            except Exception:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "LOCAL_PROVIDER_CONFIGURATION_INVALID",
                        "error": "The local Provider configuration could not be checked safely.",
                    },
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"ok": True, "preflight": preflight})
            return
        restore_member_match = re.fullmatch(
            r"/api/rooms/([^/]+)/members/([^/]+)/restore",
            parsed.path,
        )
        if restore_member_match:
            if "expected_version" not in payload:
                self._send_json(
                    {"ok": False, "error": "恢复成员必须提供 expected_version"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                member = STORE.restore_member(
                    restore_member_match.group(1),
                    restore_member_match.group(2),
                    expected_version=int(payload.get("expected_version") or 0),
                )
            except ValueError as exc:
                status = HTTPStatus.CONFLICT if "版本" in str(exc) or "当前轮次" in str(exc) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            if not member:
                self._send_json({"ok": False, "error": "成员不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "member": member})
            return
        add_member_match = re.fullmatch(r"/api/rooms/([^/]+)/members", parsed.path)
        if add_member_match:
            try:
                _validate_member_provider_assignment(
                    _member_provider_id(payload.get("provider")),
                )
                member = STORE.add_member(add_member_match.group(1), payload)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if not member:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "member": member}, HTTPStatus.CREATED)
            return
        add_paper_portfolio_match = re.fullmatch(
            r"/api/rooms/([^/]+)/paper-portfolios",
            parsed.path,
        )
        candidate_experiment_match = re.fullmatch(
            r"/api/rooms/([^/]+)/candidate-experiments",
            parsed.path,
        )
        if candidate_experiment_match:
            try:
                experiment = CandidateExperimentService(
                    STORE,
                    STORAGE_MARKET,
                ).run(candidate_experiment_match.group(1), payload)
            except CandidateExperimentError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
                return
            status = (
                HTTPStatus.OK
                if experiment.get("idempotent_replay") is True
                else HTTPStatus.CREATED
            )
            self._send_json(
                {"ok": True, "experiment": experiment},
                status,
            )
            return
        candidate_comparison_match = re.fullmatch(
            r"/api/rooms/([^/]+)/candidate-comparisons/preview",
            parsed.path,
        )
        if candidate_comparison_match:
            try:
                comparison = CandidateComparisonService(STORE).preview(
                    candidate_comparison_match.group(1),
                    payload,
                )
            except LookupError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.NOT_FOUND,
                )
                return
            except CandidateComparisonError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._send_json({"ok": True, "comparison": comparison})
            return
        if add_paper_portfolio_match:
            if not self._require_plugin_action(
                add_paper_portfolio_match.group(1),
                "paper_portfolio.manage",
            ):
                return
            try:
                portfolio = PaperPortfolioService(STORE, STORAGE_MARKET).create(
                    add_paper_portfolio_match.group(1),
                    payload,
                    created_by="user",
                )
            except CandidateSimulationContractError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                message = str(exc)
                status = (
                    HTTPStatus.CONFLICT
                    if any(marker in message for marker in (
                        "用户决定已经过期",
                        "只有当前支持候选",
                        "用户决定来源快照完整性",
                        "决策谱系哈希链",
                        "决策谱系绑定的用户决定不存在",
                    ))
                    else HTTPStatus.BAD_REQUEST
                )
                self._send_json({"ok": False, "error": message}, status)
                return
            self._send_json({"ok": True, "portfolio": portfolio}, HTTPStatus.CREATED)
            return
        walk_forward_paper_portfolio_match = re.fullmatch(
            r"/api/rooms/([^/]+)/paper-portfolios/([^/]+)/walk-forward",
            parsed.path,
        )
        if walk_forward_paper_portfolio_match:
            room_id, portfolio_id = walk_forward_paper_portfolio_match.groups()
            if not self._require_plugin_action(
                room_id,
                "paper_portfolio.run_walk_forward",
            ):
                return
            config = dict(payload)
            expected_portfolio_version = config.pop(
                "expected_portfolio_version",
                None,
            )
            try:
                walk_forward_run = PaperPortfolioService(
                    STORE,
                    STORAGE_MARKET,
                ).walk_forward(
                    room_id,
                    portfolio_id,
                    config,
                    expected_portfolio_version=expected_portfolio_version,
                    created_by="user",
                )
            except LookupError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except WalkForwardFeasibilityError as exc:
                self._send_json(
                    {
                        "ok": False,
                        "error": str(exc),
                        "code": exc.diagnostic["reason_code"],
                        "diagnostic": exc.diagnostic,
                    },
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )
                return
            except CandidateSimulationContractError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                status = (
                    HTTPStatus.CONFLICT
                    if "版本已变化" in str(exc)
                    else HTTPStatus.BAD_REQUEST
                )
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            self._send_json(
                {"ok": True, "walk_forward_run": walk_forward_run},
                HTTPStatus.CREATED,
            )
            return
        confirm_paper_portfolio_match = re.fullmatch(
            r"/api/rooms/([^/]+)/paper-portfolios/([^/]+)/confirm",
            parsed.path,
        )
        if confirm_paper_portfolio_match:
            room_id, portfolio_id = confirm_paper_portfolio_match.groups()
            if not self._require_plugin_action(room_id, "paper_portfolio.manage"):
                return
            try:
                portfolio = PaperPortfolioService(STORE, STORAGE_MARKET).confirm(
                    room_id,
                    portfolio_id,
                    expected_version=int(payload.get("expected_version") or 0),
                    confirmed_by="user",
                )
            except LookupError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except CandidateSimulationContractError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            self._send_json({"ok": True, "portfolio": portfolio})
            return
        evaluate_paper_portfolio_match = re.fullmatch(
            r"/api/rooms/([^/]+)/paper-portfolios/([^/]+)/evaluate",
            parsed.path,
        )
        if evaluate_paper_portfolio_match:
            room_id, portfolio_id = evaluate_paper_portfolio_match.groups()
            if not self._require_plugin_action(room_id, "paper_portfolio.manage"):
                return
            try:
                portfolio = PaperPortfolioService(STORE, STORAGE_MARKET).reevaluate(
                    room_id,
                    portfolio_id,
                    expected_version=int(payload.get("expected_version") or 0),
                )
            except LookupError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except CandidateSimulationContractError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                status = HTTPStatus.CONFLICT if "版本" in str(exc) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            self._send_json({"ok": True, "portfolio": portfolio})
            return
        add_observation_match = re.fullmatch(r"/api/rooms/([^/]+)/observations", parsed.path)
        if add_observation_match:
            if not self._require_plugin_action(
                add_observation_match.group(1),
                "observation.manage",
            ):
                return
            try:
                observation = OBSERVATIONS.create(add_observation_match.group(1), payload)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, "observation": observation}, HTTPStatus.CREATED)
            return
        bind_observation_lineage_match = re.fullmatch(
            r"/api/rooms/([^/]+)/observations/([^/]+)/decision-lineage",
            parsed.path,
        )
        if bind_observation_lineage_match:
            room_id, observation_id = bind_observation_lineage_match.groups()
            if not self._require_plugin_action(room_id, "decision_lineage.manage"):
                return
            try:
                result = OBSERVATIONS.bind_decision_lineage(
                    room_id,
                    observation_id,
                    payload,
                )
            except LookupError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.NOT_FOUND,
                )
                return
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.CONFLICT,
                )
                return
            self._send_json({
                "ok": True,
                "observation": result["observation"],
                "scorecard": STORE.observation_scorecard(room_id),
            })
            return
        confirm_observation_match = re.fullmatch(r"/api/rooms/([^/]+)/observations/([^/]+)/confirm", parsed.path)
        if confirm_observation_match:
            if not self._require_plugin_action(
                confirm_observation_match.group(1),
                "observation.manage",
            ):
                return
            try:
                observation = OBSERVATIONS.confirm(
                    confirm_observation_match.group(1),
                    confirm_observation_match.group(2),
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({
                "ok": True,
                "observation": observation,
                "scorecard": STORE.observation_scorecard(confirm_observation_match.group(1)),
            })
            return
        reconcile_observations_match = re.fullmatch(r"/api/rooms/([^/]+)/observations/reconcile", parsed.path)
        if reconcile_observations_match:
            if not self._require_plugin_action(
                reconcile_observations_match.group(1),
                "observation.manage",
            ):
                return
            result = OBSERVATIONS.reconcile(reconcile_observations_match.group(1))
            self._send_json({"ok": True, **result})
            return
        confirm_reflection_match = re.fullmatch(
            r"/api/rooms/([^/]+)/observations/([^/]+)/reflection/confirm",
            parsed.path,
        )
        if confirm_reflection_match:
            if not self._require_plugin_action(
                confirm_reflection_match.group(1),
                "observation.manage",
            ):
                return
            try:
                reflection = STORE.confirm_reflection(
                    confirm_reflection_match.group(1),
                    confirm_reflection_match.group(2),
                    expected_version=int(payload.get("expected_version") or 0),
                    confirmed_by="user",
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            if not reflection:
                self._send_json({"ok": False, "error": "反思记录不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "reflection": reflection})
            return
        import_file_match = re.fullmatch(r"/api/rooms/([^/]+)/materials/import-file", parsed.path)
        if import_file_match:
            try:
                material = MATERIAL_INGEST.import_file(import_file_match.group(1), payload)
            except PluginLifecycleError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                status = HTTPStatus.CONFLICT if "版本已变化" in str(exc) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            official_attestation = material.pop("_official_attestation", None)
            response: dict[str, Any] = {"ok": True, "material": material}
            if official_attestation:
                response["official_attestation"] = official_attestation
            self._send_json(response, HTTPStatus.CREATED)
            return
        confirm_official_attestation_match = re.fullmatch(
            r"/api/rooms/([^/]+)/materials/([^/]+)/official-attestation/confirm",
            parsed.path,
        )
        if confirm_official_attestation_match:
            room_id, material_id = confirm_official_attestation_match.groups()
            if payload.get("user_confirmed") is not True:
                self._send_json({"ok": False, "error": "必须由用户显式确认精确官方文件副本"}, HTTPStatus.BAD_REQUEST)
                return
            if STORE.get_material(room_id, material_id) is None:
                self._send_json({"ok": False, "error": "房间或资料不存在"}, HTTPStatus.NOT_FOUND)
                return
            try:
                official_attestation = STORE.confirm_material_official_attestation(
                    room_id,
                    material_id,
                    payload,
                    confirmed_by="user",
                )
            except PluginLifecycleError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            material = STORE.get_material(room_id, material_id)
            if material is None:
                self._send_json({"ok": False, "error": "资料不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({
                "ok": True,
                "material": material,
                "official_attestation": official_attestation,
            })
            return
        fetch_url_match = re.fullmatch(r"/api/rooms/([^/]+)/materials/fetch-url", parsed.path)
        if fetch_url_match:
            try:
                material = MATERIAL_INGEST.fetch_url(fetch_url_match.group(1), payload)
            except ValueError as exc:
                status = HTTPStatus.CONFLICT if "版本已变化" in str(exc) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            self._send_json({"ok": True, "material": material}, HTTPStatus.CREATED)
            return
        freeze_official_match = re.fullmatch(r"/api/rooms/([^/]+)/materials/freeze-official-evidence", parsed.path)
        if freeze_official_match:
            room_id = freeze_official_match.group(1)
            if not self._require_plugin_action(
                room_id,
                "market.storage.freeze_official_evidence",
            ):
                return
            if not STORE.room_snapshot(room_id):
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            try:
                material_payload = STORAGE_MARKET.official_evidence_material_payload(
                    evidence_kind=str(payload.get("evidence_kind") or ""),
                    symbol=str(payload.get("symbol") or ""),
                    official_url=str(payload.get("official_url") or ""),
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            metadata = material_payload.get("metadata") or {}
            existing = next((
                material
                for material in STORE.list_materials(room_id)
                if (material.get("metadata") or {}).get("official_evidence_kind") == metadata.get("official_evidence_kind")
                and (material.get("metadata") or {}).get("official_evidence_id") == metadata.get("official_evidence_id")
            ), None)
            if existing:
                self._send_json({"ok": True, "material": existing, "created": False})
                return
            try:
                material = STORE.add_material(
                    room_id,
                    material_payload,
                    required_plugin_action=(
                        "market.storage.freeze_official_evidence"
                    ),
                )
            except PluginLifecycleError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            if not material:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "material": material, "created": True}, HTTPStatus.CREATED)
            return
        add_material_match = re.fullmatch(r"/api/rooms/([^/]+)/materials", parsed.path)
        if add_material_match:
            try:
                material = STORE.add_material(add_material_match.group(1), payload)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if not material:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "material": material}, HTTPStatus.CREATED)
            return
        generate_artifact_match = re.fullmatch(r"/api/rooms/([^/]+)/artifacts/generate", parsed.path)
        if generate_artifact_match:
            room_id = generate_artifact_match.group(1)
            execution_lock = self._acquire_formal_execution(room_id)
            if execution_lock is None:
                return
            try:
                self._generate_artifact(room_id, payload)
            finally:
                execution_lock.release()
            return
        artifact_user_decision_match = re.fullmatch(
            r"/api/rooms/([^/]+)/artifacts/([^/]+)/user-decision",
            parsed.path,
        )
        if artifact_user_decision_match:
            room_id, artifact_id = artifact_user_decision_match.groups()
            try:
                unknown_fields = sorted(set(payload) - USER_DECISION_REQUEST_FIELDS)
                if unknown_fields:
                    raise ValueError(
                        "最终决定请求包含未允许字段："
                        + "、".join(unknown_fields[:12])
                    )
                decision_kwargs = {
                    "expected_version": payload.get("expected_version"),
                    "action": payload.get("action"),
                    "rationale": payload.get("rationale"),
                    "created_by": "user",
                }
                decision_kwargs.update({
                    field: payload[field]
                    for field in USER_DECISION_SELECTION_FIELDS
                    if field in payload
                })
                user_decision = STORE.create_artifact_user_decision(
                    room_id,
                    artifact_id,
                    **decision_kwargs,
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            if not user_decision:
                self._send_json({"ok": False, "error": "产物不存在"}, HTTPStatus.NOT_FOUND)
                return
            snapshot = STORE.room_snapshot(room_id)
            artifact = next(
                (
                    item
                    for item in (snapshot or {}).get("artifacts") or []
                    if str(item.get("id") or "") == artifact_id
                ),
                None,
            )
            if not snapshot or not artifact:
                self._send_json({"ok": False, "error": "产物不存在"}, HTTPStatus.NOT_FOUND)
                return
            convergence = ORCHESTRATOR.convergence.evaluate(
                room_id,
                snapshot=snapshot,
            )
            self._send_json({
                "ok": True,
                "artifact": artifact,
                "user_decision": user_decision,
                "convergence": convergence,
                "storage_sample_acceptance": storage_sample_acceptance(
                    room_id,
                    snapshot=snapshot,
                    convergence_state=convergence,
                ),
            })
            return
        confirm_artifact_match = re.fullmatch(r"/api/rooms/([^/]+)/artifacts/([^/]+)/confirm", parsed.path)
        if confirm_artifact_match:
            try:
                artifact = STORE.confirm_artifact(
                    confirm_artifact_match.group(1),
                    confirm_artifact_match.group(2),
                    expected_version=int(payload.get("expected_version") or 0),
                    confirmed_by="user",
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            if not artifact:
                self._send_json({"ok": False, "error": "产物不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "artifact": artifact})
            return
        reorder_match = re.fullmatch(r"/api/rooms/([^/]+)/members/reorder", parsed.path)
        if reorder_match:
            member_ids = payload.get("member_ids")
            expected_member_ids = payload.get("expected_member_ids")
            if not isinstance(member_ids, list) or not isinstance(expected_member_ids, list):
                self._send_json({"ok": False, "error": "成员顺序格式无效"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                members = STORE.reorder_members(
                    reorder_match.group(1),
                    [str(member_id) for member_id in member_ids],
                    expected_member_ids=[str(member_id) for member_id in expected_member_ids],
                )
            except ValueError as exc:
                status = HTTPStatus.CONFLICT if "已变化" in str(exc) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            self._send_json({"ok": True, "members": members})
            return
        resume_chat_match = re.fullmatch(
            r"/api/rooms/([^/]+)/chat-requests/([^/]+)/resume/stream",
            parsed.path,
        )
        if resume_chat_match:
            self._resume_chat_request(*resume_chat_match.groups())
            return
        message_stream_match = re.fullmatch(r"/api/rooms/([^/]+)/messages/stream", parsed.path)
        if message_stream_match:
            self._stream_user_message(message_stream_match.group(1), payload)
            return
        message_match = re.fullmatch(r"/api/rooms/([^/]+)/messages", parsed.path)
        if message_match:
            content = str(payload.get("content") or "").strip()
            if not content:
                self._send_json({"ok": False, "error": "消息不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            if payload.get("mentions"):
                self._send_json(
                    {"ok": False, "error": "结构化点名请使用消息流接口"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                result = STORE.create_user_message_request(
                    message_match.group(1),
                    content=content,
                    mentions=[],
                    expected_round_id=str(payload.get("expected_round_id") or ""),
                    client_message_id=str(payload.get("client_message_id") or ""),
                )
            except MessageRoutingConflict as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            except LookupError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"ok": True, **result}, HTTPStatus.CREATED)
            return
        round_match = re.fullmatch(r"/api/rooms/([^/]+)/rounds/stream", parsed.path)
        if round_match:
            if "member_ids" in payload:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_MEMBER_IDS_NOT_ALLOWED",
                        "error": "Authorized rounds always use the frozen enabled-member set.",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                skip_provider_ids = self._skip_provider_ids(payload)
                client_round_request_id, plan_hash, max_provider_calls = (
                    self._round_authorization_fields(payload)
                )
            except Exception:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_AUTHORIZATION_REQUIRED",
                        "error": (
                            "client_round_request_id, plan_hash, and a 1..100 "
                            "max_provider_calls value are required."
                        ),
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._stream_round(
                round_match.group(1),
                str(payload.get("objective") or payload.get("content") or ""),
                None,
                skip_provider_ids=skip_provider_ids,
                client_round_request_id=client_round_request_id,
                plan_hash=plan_hash,
                max_provider_calls=max_provider_calls,
                project_round_focus_authorization=payload.get(
                    "project_round_focus_authorization"
                ),
                round_context_authorizations=payload.get(
                    "round_context_authorizations"
                ),
            )
            return
        pause_round_match = re.fullmatch(
            r"/api/rooms/([^/]+)/rounds/([^/]+)/pause",
            parsed.path,
        )
        if pause_round_match:
            room_id, round_id = pause_round_match.groups()
            try:
                round_row = STORE.request_round_pause(room_id, round_id)
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.CONFLICT,
                )
                return
            if not round_row:
                self._send_json(
                    {"ok": False, "error": "讨论轮次不存在"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            accepted = bool(
                str(round_row.get("status") or "").upper() == "RUNNING"
                and round_row.get("pause_requested")
            )
            self._send_json(
                {
                    "ok": True,
                    "accepted": accepted,
                    "round": round_row,
                },
                HTTPStatus.ACCEPTED if accepted else HTTPStatus.OK,
            )
            return
        cancel_round_match = re.fullmatch(
            r"/api/rooms/([^/]+)/rounds/([^/]+)/cancel",
            parsed.path,
        )
        if cancel_round_match:
            room_id, round_id = cancel_round_match.groups()
            try:
                round_row = STORE.cancel_paused_round(room_id, round_id)
            except ValueError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc)},
                    HTTPStatus.CONFLICT,
                )
                return
            if not round_row:
                self._send_json(
                    {"ok": False, "error": "讨论轮次不存在"},
                    HTTPStatus.NOT_FOUND,
                )
                return
            self._send_json({"ok": True, "round": round_row})
            return
        resume_round_match = re.fullmatch(r"/api/rooms/([^/]+)/rounds/([^/]+)/resume/stream", parsed.path)
        if resume_round_match:
            forbidden_resume_fields = {
                "client_round_request_id",
                "plan_hash",
                "max_provider_calls",
                "member_ids",
                "skip_providers",
                "project_round_focus_authorization",
                "round_context_authorizations",
            }
            if forbidden_resume_fields.intersection(payload):
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_RESUME_AUTHORIZATION_OVERRIDE_FORBIDDEN",
                        "error": "A paused round must reuse its exact persisted authorization.",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            self._stream_round(
                resume_round_match.group(1),
                "",
                None,
                resume_round_id=resume_round_match.group(2),
            )
            return
        self._send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        if not self._guard_request(mutating=True):
            return
        parsed = urlparse(self.path)
        payload = self._read_json()
        if payload is None:
            return
        member_match = re.fullmatch(r"/api/rooms/([^/]+)/members/([^/]+)", parsed.path)
        if member_match:
            if "expected_version" not in payload:
                self._send_json(
                    {"ok": False, "error": "归档成员必须提供 expected_version"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                member = STORE.archive_member(
                    member_match.group(1),
                    member_match.group(2),
                    expected_version=int(payload.get("expected_version") or 0),
                )
            except ValueError as exc:
                status = HTTPStatus.CONFLICT if "版本" in str(exc) or "当前轮次" in str(exc) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            if not member:
                self._send_json({"ok": False, "error": "成员不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "member": member})
            return
        self._send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:
        if not self._guard_request(mutating=True):
            return
        parsed = urlparse(self.path)
        payload = self._read_json()
        if payload is None:
            return
        paper_portfolio_match = re.fullmatch(
            r"/api/rooms/([^/]+)/paper-portfolios/([^/]+)",
            parsed.path,
        )
        if paper_portfolio_match:
            room_id, portfolio_id = paper_portfolio_match.groups()
            if not self._require_plugin_action(room_id, "paper_portfolio.manage"):
                return
            allowed_fields = {
                "name",
                "positions",
                "budgets",
                "stress_scenarios",
                "expected_version",
                "candidate_simulation_confirmation",
            }
            unknown_fields = set(payload) - allowed_fields
            if unknown_fields:
                self._send_json(
                    {
                        "ok": False,
                        "error": "模拟组合更新包含未知字段："
                        + "、".join(sorted(unknown_fields)),
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                portfolio = PaperPortfolioService(STORE, STORAGE_MARKET).update(
                    room_id,
                    portfolio_id,
                    {
                        "name": payload.get("name"),
                        "positions": payload.get("positions"),
                        "budgets": payload.get("budgets"),
                        "stress_scenarios": payload.get("stress_scenarios"),
                        **({
                            "candidate_simulation_confirmation": payload.get(
                                "candidate_simulation_confirmation"
                            ),
                        } if "candidate_simulation_confirmation" in payload else {}),
                    },
                    expected_version=int(payload.get("expected_version") or 0),
                )
            except LookupError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except CandidateSimulationContractError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                status = HTTPStatus.CONFLICT if "版本" in str(exc) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            self._send_json({"ok": True, "portfolio": portfolio})
            return
        room_match = re.fullmatch(r"/api/rooms/([^/]+)", parsed.path)
        if room_match:
            if (
                "expected_settings_version" not in payload
                and "expected_version" not in payload
                and "expected_updated_at" not in payload
            ):
                self._send_json(
                    {"ok": False, "error": "保存房间设置必须提供 expected_settings_version"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                room = STORE.update_room(room_match.group(1), payload)
            except PluginLifecycleError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                status = HTTPStatus.CONFLICT if "已被其他操作更新" in str(exc) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            if not room:
                self._send_json({"ok": False, "error": "房间不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "room": room})
            return
        member_match = re.fullmatch(r"/api/rooms/([^/]+)/members/([^/]+)", parsed.path)
        if member_match:
            if "expected_version" not in payload:
                self._send_json(
                    {"ok": False, "error": "保存成员身份必须提供 expected_version"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            room_id, member_id = member_match.groups()
            current_member = STORE.get_member(room_id, member_id)
            if not current_member:
                self._send_json({"ok": False, "error": "成员不存在。"}, HTTPStatus.NOT_FOUND)
                return
            if "provider" in payload:
                requested_provider = _member_provider_id(payload.get("provider"))
                current_provider = _member_provider_id(current_member.get("provider"))
                if requested_provider != current_provider:
                    try:
                        _validate_member_provider_assignment(requested_provider)
                    except ValueError as exc:
                        self._send_json(
                            {"ok": False, "error": str(exc)},
                            HTTPStatus.BAD_REQUEST,
                        )
                        return
            try:
                member = STORE.update_member(
                    room_id,
                    member_id,
                    payload,
                    expected_version=int(payload.get("expected_version") or 0),
                )
            except ValueError as exc:
                status = HTTPStatus.CONFLICT if "版本" in str(exc) or "已归档" in str(exc) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            if not member:
                self._send_json({"ok": False, "error": "成员不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "member": member})
            return
        material_match = re.fullmatch(r"/api/rooms/([^/]+)/materials/([^/]+)", parsed.path)
        if material_match:
            if "expected_version" not in payload:
                self._send_json(
                    {"ok": False, "error": "保存资料必须提供 expected_version"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                material = STORE.update_material(
                    material_match.group(1),
                    material_match.group(2),
                    payload,
                )
            except ValueError as exc:
                status = HTTPStatus.CONFLICT if "版本已变化" in str(exc) else HTTPStatus.BAD_REQUEST
                self._send_json({"ok": False, "error": str(exc)}, status)
                return
            if not material:
                self._send_json({"ok": False, "error": "资料不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "material": material})
            return
        artifact_match = re.fullmatch(r"/api/rooms/([^/]+)/artifacts/([^/]+)", parsed.path)
        if artifact_match:
            if "expected_version" not in payload:
                self._send_json(
                    {"ok": False, "error": "保存产物必须提供 expected_version"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                artifact = STORE.update_artifact(
                    artifact_match.group(1),
                    artifact_match.group(2),
                    payload,
                )
            except PluginLifecycleError as exc:
                self._send_json(
                    {"ok": False, "error": str(exc), "code": exc.code},
                    HTTPStatus(exc.status),
                )
                return
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            if not artifact:
                self._send_json({"ok": False, "error": "产物不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "artifact": artifact})
            return
        reflection_match = re.fullmatch(
            r"/api/rooms/([^/]+)/observations/([^/]+)/reflection",
            parsed.path,
        )
        if reflection_match:
            if not self._require_plugin_action(
                reflection_match.group(1),
                "observation.manage",
            ):
                return
            try:
                reflection = STORE.update_reflection(
                    reflection_match.group(1),
                    reflection_match.group(2),
                    payload,
                )
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
                return
            if not reflection:
                self._send_json({"ok": False, "error": "反思记录不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"ok": True, "reflection": reflection})
            return
        self._send_json({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def _stream_user_message(self, room_id: str, payload: dict[str, Any]) -> None:
        try:
            skip_provider_ids = self._skip_provider_ids(payload)
            result = STORE.create_user_message_request(
                room_id,
                content=str(payload.get("content") or ""),
                mentions=payload.get("mentions"),
                expected_round_id=str(payload.get("expected_round_id") or ""),
                client_message_id=str(payload.get("client_message_id") or ""),
                skip_provider_ids=skip_provider_ids,
            )
        except MessageRoutingConflict as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.CONFLICT)
            return
        except LookupError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
            return
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        routing = result["routing"]
        initial_event = {
            "type": "user_message",
            "message": result["message"],
            "routing": routing,
            "idempotent_replay": bool(result.get("idempotent_replay")),
        }
        try:
            self.wfile.write(json_bytes(initial_event) + b"\n")
            self.wfile.flush()
            mode = str(routing.get("mode") or "stored_only")
            if mode in {"idle_targeted", "idle_auto"}:
                for event in ORCHESTRATOR.run_idle_chat_request(
                    room_id,
                    str(routing.get("request_id") or ""),
                    skip_provider_ids=skip_provider_ids,
                ):
                    self.wfile.write(json_bytes(event) + b"\n")
                    self.wfile.flush()
            elif mode == "round_interjection":
                self.wfile.write(json_bytes({
                    "type": "interjection_queued",
                    "request_id": str(routing.get("request_id") or ""),
                    "round_id": str(routing.get("round_id") or ""),
                    "target_member_ids": list(routing.get("target_member_ids") or []),
                    "message": "插话已进入当前轮次，将在下一安全调度边界处理。",
                }) + b"\n")
                self.wfile.flush()
            else:
                self.wfile.write(json_bytes({
                    "type": "message_stored",
                    "message_id": str(result["message"].get("id") or ""),
                    "status": str(routing.get("status") or "COMPLETED"),
                    "error_code": str(routing.get("error_code") or ""),
                    "notice": str(routing.get("message") or ""),
                }) + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except Exception:
            try:
                self.wfile.write(json_bytes({
                    "type": "error",
                    "code": "CHAT_REQUEST_FAILED",
                    "error": "群聊消息处理失败，请刷新房间查看持久化状态。",
                }) + b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

    def _resume_chat_request(self, room_id: str, request_id: str) -> None:
        STORE.recover_expired_chat_targets(room_id=room_id, request_id=request_id)
        request = STORE.get_chat_request(room_id, request_id)
        if not request:
            self._send_json({"ok": False, "error": "点名请求不存在"}, HTTPStatus.NOT_FOUND)
            return
        if str(request.get("kind") or "") != "idle_mention":
            self._send_json(
                {"ok": False, "error": "轮次插话请恢复对应讨论轮次"},
                HTTPStatus.CONFLICT,
            )
            return
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(json_bytes({
                "type": "chat_request_resumed",
                "request_id": request_id,
                "source_message_id": str((request.get("source_message") or {}).get("id") or ""),
            }) + b"\n")
            self.wfile.flush()
            for event in ORCHESTRATOR.run_idle_chat_request(
                room_id,
                request_id,
                skip_provider_ids=set(request.get("skip_provider_ids") or []),
            ):
                self.wfile.write(json_bytes(event) + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except Exception:
            try:
                self.wfile.write(json_bytes({
                    "type": "error",
                    "code": "CHAT_REQUEST_RESUME_FAILED",
                    "error": "点名请求恢复失败，请刷新房间查看持久化状态。",
                }) + b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

    def _generate_artifact(
        self,
        room_id: str,
        payload: dict[str, Any],
    ) -> None:
        round_id = str(payload.get("round_id") or "").strip()
        try:
            ledger: ProviderCallLedger | None = None
            frozen_synthesizer_route: dict[str, Any] | None = None
            if round_id:
                generation_key = STORE.artifact_generation_key(
                    room_id,
                    round_id,
                    "meeting_minutes",
                )
                existing_artifact = STORE.get_artifact_by_generation_key(
                    room_id,
                    generation_key,
                )
                if existing_artifact:
                    self._send_json({
                        "ok": True,
                        "artifact": {
                            **existing_artifact,
                            "idempotent_replay": True,
                        },
                        "created": False,
                    })
                    return
                try:
                    artifact_round = STORE.get_round(room_id, round_id)
                    if (
                        not artifact_round
                        or str(artifact_round.get("status") or "").upper()
                        in {"RUNNING", "CANCELLED"}
                    ):
                        raise ValueError("round is not eligible for artifact generation")
                    ledger = ProviderCallLedger.resume_for_round(
                        STORE,
                        room_id,
                        round_id,
                        scope="round",
                    )
                    provider_execution = ledger.snapshot()
                    if (
                        str(provider_execution.get("room_id") or "") != room_id
                        or str(provider_execution.get("round_id") or "") != round_id
                        or str(provider_execution.get("scope") or "") != "round"
                    ):
                        raise ValueError("round ledger identity mismatch")
                    persisted_max_calls = provider_execution.get("max_calls")
                    if (
                        isinstance(persisted_max_calls, bool)
                        or not isinstance(persisted_max_calls, int)
                        or persisted_max_calls < 1
                        or persisted_max_calls > 100
                    ):
                        raise ValueError("round ledger limit is invalid")
                    persisted_plan_hash = str(
                        provider_execution.get("plan_hash") or ""
                    ).strip().lower()
                    if not re.fullmatch(r"[0-9a-f]{64}", persisted_plan_hash):
                        raise ValueError("round ledger plan hash is invalid")
                    if any(
                        str(attempt.get("status") or "").upper() == "STARTED"
                        for attempt in ledger.attempts()
                    ):
                        self._send_json(
                            {
                                "ok": False,
                                "error_code": "ARTIFACT_ROUND_LEDGER_BUSY",
                                "error": "The round still has an unfinished Provider call; resume recovery first.",
                            },
                            HTTPStatus.CONFLICT,
                        )
                        return
                    if provider_execution.get("artifact_route_integrity_ok") is not True:
                        raise ValueError("round artifact route seal is missing")
                    frozen_synthesizer_route = dict(
                        provider_execution.get("artifact_route") or {}
                    )
                    if not frozen_synthesizer_route:
                        raise ValueError("round artifact route is missing")
                except Exception:
                    self._send_json(
                        {
                            "ok": False,
                            "error_code": "ARTIFACT_ROUND_LEDGER_REQUIRED",
                            "error": "This round has no reusable Provider-call authorization.",
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                skip_provider_ids = self._ledger_skip_provider_ids(
                    provider_execution
                )
                if "skip_providers" in payload:
                    requested_skip_ids = self._skip_provider_ids(payload)
                    if requested_skip_ids != skip_provider_ids:
                        self._send_json(
                            {
                                "ok": False,
                                "error_code": "ARTIFACT_SKIP_POLICY_CONFLICT",
                                "error": "Artifact generation must reuse the round skip policy.",
                            },
                            HTTPStatus.CONFLICT,
                        )
                        return
                requested_synthesizer_id = str(
                    payload.get("synthesizer_member_id") or ""
                ).strip()
                if (
                    requested_synthesizer_id
                    and requested_synthesizer_id
                    != str(frozen_synthesizer_route.get("member_id") or "")
                ):
                    self._send_json(
                        {
                            "ok": False,
                            "error_code": "ARTIFACT_ROUTE_OVERRIDE_FORBIDDEN",
                            "error": "Artifact generation must reuse the route approved for this round.",
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
            else:
                skip_provider_ids = self._skip_provider_ids(payload)
                statuses = self._artifact_provider_statuses()
                configured_provider_exists = any(
                    status.get("configured") is True for status in statuses
                )
                if configured_provider_exists or not statuses:
                    self._send_json(
                        {
                            "ok": False,
                            "error_code": "ARTIFACT_ROUND_AUTHORIZATION_REQUIRED",
                            "error": "Choose an authorized round before using a configured Provider.",
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                # No configured Provider is available. Force every known route
                # into the local template-fallback path.
                skip_provider_ids.update(
                    str(status.get("id") or "").strip().lower()
                    for status in statuses
                    if str(status.get("id") or "").strip()
                )
            artifact = ARTIFACTS.generate_minutes(
                room_id,
                round_id,
                str(payload.get("synthesizer_member_id") or ""),
                skip_provider_ids=skip_provider_ids,
                ledger=ledger,
                frozen_synthesizer_route=frozen_synthesizer_route,
            )
        except Exception:
            self._send_json(
                {
                    "ok": False,
                    "error_code": "ARTIFACT_GENERATION_FAILED",
                    "error": "Artifact generation could not be completed safely.",
                },
                HTTPStatus.BAD_REQUEST,
            )
            return
        created = artifact.get("idempotent_replay") is not True
        self._send_json(
            {"ok": True, "artifact": artifact, "created": created},
            HTTPStatus.CREATED if created else HTTPStatus.OK,
        )

    def _stream_round(
        self,
        room_id: str,
        objective: str,
        member_ids: list[str] | None,
        *,
        resume_round_id: str = "",
        skip_provider_ids: set[str] | None = None,
        client_round_request_id: str = "",
        plan_hash: str = "",
        max_provider_calls: int = 0,
        project_round_focus_authorization: Any = None,
        round_context_authorizations: Any = None,
    ) -> None:
        execution_lock = self._acquire_formal_execution(room_id)
        if execution_lock is None:
            return
        try:
            self._stream_round_locked(
                room_id,
                objective,
                member_ids,
                resume_round_id=resume_round_id,
                skip_provider_ids=skip_provider_ids,
                client_round_request_id=client_round_request_id,
                plan_hash=plan_hash,
                max_provider_calls=max_provider_calls,
                project_round_focus_authorization=(
                    project_round_focus_authorization
                ),
                round_context_authorizations=round_context_authorizations,
            )
        finally:
            execution_lock.release()

    def _stream_round_locked(
        self,
        room_id: str,
        objective: str,
        member_ids: list[str] | None,
        *,
        resume_round_id: str = "",
        skip_provider_ids: set[str] | None = None,
        client_round_request_id: str = "",
        plan_hash: str = "",
        max_provider_calls: int = 0,
        project_round_focus_authorization: Any = None,
        round_context_authorizations: Any = None,
    ) -> None:
        skip_provider_ids = {
            str(item or "").strip().lower()
            for item in (skip_provider_ids or set())
            if str(item or "").strip()
        }
        provider_call_ledger: ProviderCallLedger | None = None
        round_authorization: dict[str, Any] | None = None
        round_kind_call_limits: dict[str, int] = {}
        recommended_director_calls = 0
        if resume_round_id:
            try:
                provider_call_ledger = ProviderCallLedger.resume_for_round(
                    STORE,
                    room_id,
                    resume_round_id,
                    scope="round",
                )
                provider_execution = provider_call_ledger.snapshot()
                round_kind_call_limits = dict(
                    provider_execution.get("kind_call_limits") or {}
                )
                recommended_director_calls = int(
                    round_kind_call_limits.get("round_director") or 0
                )
                if (
                    str(provider_execution.get("room_id") or "") != room_id
                    or str(provider_execution.get("round_id") or "")
                    != resume_round_id
                    or str(provider_execution.get("scope") or "") != "round"
                ):
                    raise ValueError("round ledger identity mismatch")
                persisted_max_calls = provider_execution.get("max_calls")
                if (
                    isinstance(persisted_max_calls, bool)
                    or not isinstance(persisted_max_calls, int)
                    or persisted_max_calls < 1
                    or persisted_max_calls > 100
                ):
                    raise ValueError("round ledger limit is invalid")
                persisted_plan_hash = str(
                    provider_execution.get("plan_hash") or ""
                ).strip().lower()
                if not re.fullmatch(r"[0-9a-f]{64}", persisted_plan_hash):
                    raise ValueError("round ledger plan hash is invalid")
                persisted_round = STORE.get_round(room_id, resume_round_id)
                if (
                    not persisted_round
                    or str(persisted_round.get("status") or "").upper()
                    != "PAUSED"
                ):
                    raise ValueError("only a paused round can reuse its ledger")
                provider_call_ledger.abandon_started(
                    error_code="provider_call_abandoned_before_resume"
                )
                provider_execution = provider_call_ledger.snapshot()
                skip_provider_ids = self._ledger_skip_provider_ids(
                    provider_execution
                )
            except Exception:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_PROVIDER_LEDGER_REQUIRED",
                        "error": (
                            "This paused round has no exact reusable Provider-call "
                            "authorization. Legacy rounds remain paused."
                        ),
                    },
                    HTTPStatus.CONFLICT,
                )
                return
        else:
            try:
                launch_plan_service = RoundLaunchPlanService(
                    STORE,
                    self._round_provider_registry(),
                )
                launch_plan = launch_plan_service.build(
                    room_id,
                    objective,
                    skip_provider_ids,
                    project_round_focus_authorization,
                    round_context_authorizations=round_context_authorizations,
                )
            except (
                ProjectRoundFocusError,
                RoundContextError,
                FootballResearchError,
                StockResearchError,
            ) as exc:
                self._send_json(
                    {"ok": False, "error_code": exc.code, "error": str(exc)},
                    HTTPStatus(exc.status),
                )
                return
            except Exception:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_LAUNCH_PLAN_INVALID",
                        "error": "The round launch plan could not be rebuilt safely.",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            confirmed_plan_hash = str(plan_hash or "").strip().lower()
            if not secrets.compare_digest(
                str(launch_plan.get("plan_hash") or ""),
                confirmed_plan_hash,
            ):
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_LAUNCH_PLAN_DRIFT",
                        "error": "Room or Provider settings changed after plan confirmation.",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if launch_plan.get("ready_for_authorization") is not True:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_LAUNCH_PLAN_BLOCKED",
                        "error": "The confirmed launch plan is not ready for authorization.",
                        "blockers": list(launch_plan.get("blockers") or []),
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            try:
                round_authorization = launch_plan_service.validate_authorization(
                    confirmed_plan_hash,
                    max_provider_calls,
                )
            except Exception:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_AUTHORIZATION_INVALID",
                        "error": "The Provider-call authorization must be between 1 and 100.",
                    },
                    HTTPStatus.BAD_REQUEST,
                )
                return
            objective = str(launch_plan.get("objective") or "")
        prefetched_market_snapshot: dict[str, Any] | None = None
        resume_checkpoint: dict[str, Any] | None = None
        resume_failed_member_ids: set[str] = set()
        try:
            room_snapshot = STORE.room_snapshot(room_id)
        except Exception:
            self._send_json(
                {
                    "ok": False,
                    "error_code": "ROUND_ROOM_SNAPSHOT_FAILED",
                    "error": "The room snapshot could not be read safely.",
                },
                HTTPStatus.CONFLICT,
            )
            return
        if not isinstance(room_snapshot, dict) or not room_snapshot:
            self._send_json(
                {
                    "ok": False,
                    "error_code": "ROUND_ROOM_SNAPSHOT_FAILED",
                    "error": "The room snapshot could not be read safely.",
                },
                HTTPStatus.CONFLICT,
            )
            return
        if resume_round_id:
            try:
                resume_checkpoint = STORE.get_round_checkpoint(
                    room_id,
                    resume_round_id,
                )
            except Exception:
                resume_checkpoint = None
            if not resume_checkpoint:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_CHECKPOINT_REQUIRED",
                        "error": "The paused round has no verifiable checkpoint.",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if resume_checkpoint:
                checkpoint_state = resume_checkpoint.get("state") or {}
                checkpoint_skip_ids = checkpoint_state.get("skip_provider_ids", [])
                if not isinstance(checkpoint_skip_ids, list):
                    self.send_response(HTTPStatus.OK)
                    self._cors_headers()
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-transform")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    self.wfile.write(json_bytes({
                        "type": "error",
                        "code": "ROUND_CHECKPOINT_INVALID",
                        "error": "本轮检查点的 Provider 禁用策略无效，轮次保持暂停。",
                    }) + b"\n")
                    self.wfile.flush()
                    return
                checkpoint_skip_provider_ids = {
                    str(item or "").strip().lower()
                    for item in checkpoint_skip_ids
                    if str(item or "").strip()
                }
                if checkpoint_skip_provider_ids != skip_provider_ids:
                    self._send_json(
                        {
                            "ok": False,
                            "error_code": "ROUND_LEDGER_SKIP_POLICY_MISMATCH",
                            "error": (
                                "The paused checkpoint does not match its persisted "
                                "Provider skip policy."
                            ),
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                raw_member_ids = checkpoint_state.get("member_ids")
                try:
                    resume_failed_member_ids = (
                        ORCHESTRATOR.checkpoint_failed_member_ids(
                            checkpoint_state,
                            raw_member_ids,
                        )
                    )
                except Exception:
                    self.send_response(HTTPStatus.OK)
                    self._cors_headers()
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-transform")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    self.wfile.write(json_bytes({
                        "type": "error",
                        "code": "ROUND_CHECKPOINT_INVALID",
                        "error": "The paused checkpoint could not be restored safely.",
                    }) + b"\n")
                    self.wfile.flush()
                    return
                try:
                    workflow_preflight = (
                        ORCHESTRATOR.convergence.workflow_configuration_preflight(
                            room_snapshot or {},
                            workflow_policy=checkpoint_state.get("workflow_policy"),
                        )
                    )
                    if not isinstance(workflow_preflight, dict):
                        raise TypeError("workflow preflight result is invalid")
                except Exception:
                    self._send_json(
                        {
                            "ok": False,
                            "error_code": "ROUND_WORKFLOW_PREFLIGHT_ERROR",
                            "error": "Workflow preflight could not be completed safely.",
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                if not workflow_preflight.get("ready"):
                    self.send_response(HTTPStatus.OK)
                    self._cors_headers()
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-transform")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    self.wfile.write(json_bytes({
                        "type": "error",
                        "code": "ROUND_WORKFLOW_PREFLIGHT_FAILED",
                        "error": "本轮讨论配置已无法满足冻结流程，轮次保持暂停。",
                        "preflight": self._safe_gate_preflight(workflow_preflight),
                    }) + b"\n")
                    self.wfile.flush()
                    return
            if room_snapshot and resume_checkpoint:
                try:
                    market_preflight, _ = ORCHESTRATOR.preflight_frozen_market(
                        room_id,
                        resume_round_id,
                        snapshot=room_snapshot,
                        checkpoint=resume_checkpoint,
                    )
                    if not isinstance(market_preflight, dict):
                        raise TypeError("market preflight result is invalid")
                except Exception:
                    self._send_json(
                        {
                            "ok": False,
                            "error_code": "ROUND_MARKET_PREFLIGHT_ERROR",
                            "error": "Market preflight could not be completed safely.",
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                if (
                    market_preflight.get("applicable")
                    and not market_preflight.get("ready")
                ):
                    self.send_response(HTTPStatus.OK)
                    self._cors_headers()
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-transform")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    self.wfile.write(json_bytes({
                        "type": "error",
                        "code": "ROUND_MARKET_PREFLIGHT_FAILED",
                        "error": "本轮冻结行情不满足恢复条件，讨论轮次保持暂停。",
                        "preflight": self._safe_gate_preflight(market_preflight),
                    }) + b"\n")
                    self.wfile.flush()
                    return
        else:
            latest_round = (
                (room_snapshot or {}).get("pending_round")
                or (room_snapshot or {}).get("latest_round")
                or {}
            )
            if str(latest_round.get("status") or "").upper() == "PAUSED":
                self._send_json(
                    {
                        "ok": False,
                        "error": "当前有暂停轮次，请先继续该轮，不能直接开始新轮",
                        "error_code": "PAUSED_ROUND_PENDING",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if room_snapshot:
                try:
                    workflow_preflight = (
                        ORCHESTRATOR.convergence.workflow_configuration_preflight(
                            room_snapshot,
                            workflow_policy=(room_snapshot.get("room") or {}).get(
                                "workflow_policy"
                            ),
                        )
                    )
                    if not isinstance(workflow_preflight, dict):
                        raise TypeError("workflow preflight result is invalid")
                except Exception:
                    self._send_json(
                        {
                            "ok": False,
                            "error_code": "ROUND_WORKFLOW_PREFLIGHT_ERROR",
                            "error": "Workflow preflight could not be completed safely.",
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                if not workflow_preflight.get("ready"):
                    self.send_response(HTTPStatus.OK)
                    self._cors_headers()
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-transform")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    self.wfile.write(json_bytes({
                        "type": "error",
                        "code": "ROUND_WORKFLOW_PREFLIGHT_FAILED",
                        "error": "会前讨论配置检查未通过，讨论轮次尚未启动。",
                        "preflight": self._safe_gate_preflight(workflow_preflight),
                    }) + b"\n")
                    self.wfile.flush()
                    return
                try:
                    market_preflight, prefetched_market_snapshot = (
                        ORCHESTRATOR.preflight_market(
                            room_id,
                            snapshot=room_snapshot,
                        )
                    )
                    if not isinstance(market_preflight, dict):
                        raise TypeError("market preflight result is invalid")
                except Exception:
                    self._send_json(
                        {
                            "ok": False,
                            "error_code": "ROUND_MARKET_PREFLIGHT_ERROR",
                            "error": "Market preflight could not be completed safely.",
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                if (
                    market_preflight.get("applicable")
                    and not market_preflight.get("ready")
                ):
                    self.send_response(HTTPStatus.OK)
                    self._cors_headers()
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-transform")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    self.wfile.write(json_bytes({
                        "type": "error",
                        "code": "ROUND_MARKET_PREFLIGHT_FAILED",
                        "error": "会前行情检查未通过，讨论轮次尚未启动。",
                        "preflight": self._safe_gate_preflight(market_preflight),
                    }) + b"\n")
                    self.wfile.flush()
                    return

        artifact_route: dict[str, Any] | None = None
        member_routes: dict[str, Any] | None = None
        if not resume_round_id:
            try:
                launch_plan = launch_plan_service.build(
                    room_id,
                    objective,
                    skip_provider_ids,
                    project_round_focus_authorization,
                    round_context_authorizations=round_context_authorizations,
                )
                if not secrets.compare_digest(
                    str(launch_plan.get("plan_hash") or ""),
                    confirmed_plan_hash,
                ):
                    raise ValueError("launch plan drift")
                artifact_route = self._launch_plan_artifact_route(launch_plan)
                member_routes = self._launch_plan_member_routes(launch_plan)
                recommended_director_calls = int(
                    (launch_plan.get("calls") or {}).get(
                        "recommended_director_calls"
                    )
                    or 0
                )
                round_kind_call_limits = {
                    "round_director": recommended_director_calls,
                }
            except Exception:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_LAUNCH_PLAN_DRIFT",
                        "error": "Room or Provider settings changed after plan confirmation.",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            try:
                provider_call_ledger = ProviderCallLedger.create(
                    STORE,
                    room_id,
                    scope="round",
                    client_request_id=client_round_request_id,
                    plan_hash=str(plan_hash or "").strip().lower(),
                    max_calls=max_provider_calls,
                    skip_provider_ids=skip_provider_ids,
                    artifact_route=artifact_route,
                    member_routes=member_routes,
                    kind_call_limits=round_kind_call_limits,
                    operation_binding_version=PROVIDER_OPERATION_BINDING_VERSION,
                )
                provider_execution = provider_call_ledger.snapshot()
                if str(provider_execution.get("round_id") or ""):
                    self._send_json(
                        {
                            "ok": False,
                            "error_code": "ROUND_REQUEST_ALREADY_BOUND",
                            "error": (
                                "This client_round_request_id is already bound to a "
                                "round; use that round's resume endpoint if it is paused."
                            ),
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
                provider_call_ledger.abandon_started(
                    error_code="provider_call_abandoned_before_round"
                )
            except Exception:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_REQUEST_ID_CONFLICT",
                        "error": (
                            "client_round_request_id conflicts with a persisted "
                            "round authorization."
                        ),
                    },
                    HTTPStatus.CONFLICT,
                )
                return

        preflight_member_ids = member_ids
        should_preflight = True
        if resume_round_id:
            checkpoint = resume_checkpoint or STORE.get_round_checkpoint(
                room_id,
                resume_round_id,
            )
            if not checkpoint:
                should_preflight = False
            else:
                checkpoint_state = checkpoint.get("state") or {}
                raw_member_ids = checkpoint_state.get("member_ids") or []
                preflight_member_ids = (
                    [
                        str(member_id)
                        for member_id in raw_member_ids
                        if str(member_id) not in resume_failed_member_ids
                    ]
                    if isinstance(raw_member_ids, list)
                    else []
                )
                if not preflight_member_ids:
                    should_preflight = False
        if should_preflight:
            actual_member_ids = (
                [str(member_id or "").strip() for member_id in preflight_member_ids]
                if preflight_member_ids
                else None
            )
            try:
                preflight_service = ProviderPreflightService(
                    STORE,
                    ORCHESTRATOR.providers,
                )
                if resume_round_id:
                    preflight = preflight_service.check_resume_round(
                        room_id,
                        checkpoint_state=checkpoint_state,
                        member_ids=actual_member_ids or [],
                        skip_provider_ids=skip_provider_ids,
                        ledger=provider_call_ledger,
                    )
                else:
                    preflight = preflight_service.check_launch_plan(
                        room_id,
                        launch_plan=launch_plan,
                        skip_provider_ids=skip_provider_ids,
                        ledger=provider_call_ledger,
                    )
            except Exception:
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_PROVIDER_PREFLIGHT_ERROR",
                        "error": "Provider preflight could not be completed safely.",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if not isinstance(preflight, dict):
                self._send_json(
                    {
                        "ok": False,
                        "error_code": "ROUND_PROVIDER_PREFLIGHT_ERROR",
                        "error": "Provider preflight could not be completed safely.",
                    },
                    HTTPStatus.CONFLICT,
                )
                return
            if preflight is not None and not preflight.get("ready"):
                self.send_response(HTTPStatus.OK)
                self._cors_headers()
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(json_bytes({
                    "type": "error",
                    "code": "ROUND_PROVIDER_PREFLIGHT_FAILED",
                    "error": "会前模型检查未通过，讨论轮次尚未启动。",
                    "preflight": self._safe_provider_preflight(preflight),
                }) + b"\n")
                self.wfile.flush()
                return
        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            if round_authorization is not None:
                warning = round_authorization.get("warning")
                self.wfile.write(json_bytes({
                    "type": "round_authorization",
                    "plan_hash": str(round_authorization.get("plan_hash") or ""),
                    "max_provider_calls": int(
                        round_authorization.get("max_provider_calls") or 0
                    ),
                    "recommended_provider_calls": int(
                        round_authorization.get("recommended_provider_calls") or 0
                    ),
                    "kind_call_limits": dict(round_kind_call_limits),
                    "recommended_director_calls": recommended_director_calls,
                    "sufficient": round_authorization.get("sufficient") is True,
                    "warning_code": (
                        self._safe_error_code(
                            warning.get("code"),
                            "ROUND_AUTHORIZATION_WARNING",
                        )
                        if isinstance(warning, dict)
                        else ""
                    ),
                }) + b"\n")
                self.wfile.flush()
            for event in ORCHESTRATOR.run_round(
                room_id,
                objective,
                member_ids,
                resume_round_id=resume_round_id,
                prefetched_market_snapshot=prefetched_market_snapshot,
                skip_provider_ids=skip_provider_ids,
                provider_call_ledger=provider_call_ledger,
                expected_launch_plan_hash=(
                    confirmed_plan_hash if not resume_round_id else ""
                ),
                project_round_focus_authorization=(
                    project_round_focus_authorization
                ),
                round_context_authorizations=round_context_authorizations,
            ):
                self.wfile.write(json_bytes(self._safe_round_stream_event(event)) + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            try:
                self.wfile.write(json_bytes({
                    "type": "error",
                    "code": "ROUND_STREAM_FAILED",
                    "error": "The round stream stopped safely; refresh to inspect persisted state.",
                }) + b"\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    @staticmethod
    def _round_provider_registry() -> Any:
        return getattr(ORCHESTRATOR, "providers", PROVIDERS)

    @staticmethod
    def _local_configuration_preflight(
        plan: dict[str, Any],
        *,
        member_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Project local route metadata without probing or writing a ledger."""

        members = [
            dict(member)
            for member in (plan.get("members") or [])
            if isinstance(member, dict)
        ]
        known_member_ids = {str(member.get("id") or "") for member in members}
        requested_ids = (
            list(dict.fromkeys(member_id for member_id in member_ids if member_id))
            if member_ids is not None
            else [str(member.get("id") or "") for member in members]
        )
        requested_set = set(requested_ids)
        selected_members = [
            member
            for member in members
            if str(member.get("id") or "") in requested_set
        ]
        requested_issues = [
            {
                "id": member_id,
                "name": "",
                "available": False,
                "is_moderator": False,
                "provider": "",
                "model": "",
                "error_code": "MEMBER_NOT_FOUND",
                "message": "The requested member is not enabled in this room.",
            }
            for member_id in requested_ids
            if member_id not in known_member_ids
        ]
        routes = [
            dict(route)
            for route in (plan.get("preflight_routes") or [])
            if isinstance(route, dict)
        ]
        route_by_key = {
            (
                str(route.get("provider") or "").strip().lower(),
                str(route.get("model") or "").strip(),
            ): route
            for route in routes
        }

        def route_result(route: dict[str, Any]) -> tuple[bool, str, str]:
            if route.get("policy_disabled") is True:
                return False, "PROVIDER_POLICY_DISABLED", "This Provider is disabled by policy."
            if route.get("skipped") is True:
                return False, "PROVIDER_SKIPPED", "This Provider is skipped by policy."
            if route.get("known") is not True:
                return False, "PROVIDER_UNKNOWN", "This Provider is not registered locally."
            if route.get("configured") is not True:
                return False, "PROVIDER_NOT_CONFIGURED", "This Provider is not configured locally."
            return (
                True,
                "LOCAL_CONFIGURATION_READY",
                "Local configuration is ready; connectivity is checked only after round confirmation.",
            )

        provider_checks: list[dict[str, Any]] = []
        for route in routes:
            route_member_ids = [
                str(item or "")
                for item in (route.get("member_ids") or [])
                if str(item or "") in requested_set
            ]
            if not route_member_ids:
                continue
            ready, error_code, message = route_result(route)
            assigned_members = [
                member
                for member in selected_members
                if str(member.get("id") or "") in set(route_member_ids)
            ]
            provider_checks.append({
                "provider": str(route.get("provider") or "").strip().lower(),
                "model": str(route.get("model") or "").strip(),
                "configured": route.get("configured") is True,
                "policy_disabled": route.get("policy_disabled") is True,
                "reachable": None,
                "model_access": None,
                "ready": ready,
                "error_code": error_code,
                "message": message,
                "member_count": len(assigned_members),
                "member_ids": [str(member.get("id") or "") for member in assigned_members],
                "member_names": [str(member.get("name") or "") for member in assigned_members],
                "external_call_count": 0,
            })

        moderator = dict(plan.get("moderator") or {})
        moderator_id = str(moderator.get("id") or "")
        member_states: list[dict[str, Any]] = []
        for member in selected_members:
            route = route_by_key.get((
                str(member.get("provider") or "").strip().lower(),
                str(member.get("model") or "").strip(),
            ), {})
            ready, error_code, message = route_result(route)
            member_states.append({
                "id": str(member.get("id") or ""),
                "name": str(member.get("name") or ""),
                "available": ready,
                "is_moderator": str(member.get("id") or "") == moderator_id,
                "provider": str(member.get("provider") or "").strip().lower(),
                "model": str(member.get("model") or "").strip(),
                "error_code": error_code,
                "message": message,
            })
        moderator_state = next(
            (member for member in member_states if member["is_moderator"]),
            {
                "id": moderator_id,
                "name": str(moderator.get("name") or ""),
                "available": False,
                "is_moderator": True,
                "provider": str(moderator.get("provider") or "").strip().lower(),
                "model": str(moderator.get("model") or "").strip(),
                "error_code": "MODERATOR_NOT_SELECTED",
                "message": "The moderator is not included in this local check.",
            },
        )
        unavailable_members = [
            *requested_issues,
            *(member for member in member_states if not member["available"]),
        ]
        ready = bool(
            member_states
            and not requested_issues
            and moderator_state.get("available") is True
            and not unavailable_members
        )
        return {
            "room_id": str((plan.get("room") or {}).get("id") or ""),
            "context": "local_configuration",
            "verification_scope": "local_configuration_only",
            "external_call_count": 0,
            "ready": ready,
            "member_count": len(member_states),
            "provider_check_count": len(provider_checks),
            "provider_checks": provider_checks,
            "members": member_states,
            "moderator": moderator_state,
            "unavailable_members": unavailable_members,
            "blocking": {
                "moderator_unavailable": not bool(moderator_state.get("available")),
                "unavailable_member_count": len(unavailable_members),
            },
        }

    @staticmethod
    def _launch_plan_artifact_route(plan: dict[str, Any]) -> dict[str, Any]:
        """Select exactly the artifact route projected into the confirmed plan."""

        routes = [
            route
            for route in (plan.get("preflight_routes") or [])
            if isinstance(route, dict) and route.get("callable") is True
        ]
        callable_keys = {
            (
                str(route.get("provider") or "").strip().lower(),
                str(route.get("model") or "").strip(),
            )
            for route in routes
        }
        members = [
            member
            for member in (plan.get("members") or [])
            if isinstance(member, dict)
            and (
                str(member.get("provider") or "").strip().lower(),
                str(member.get("model") or "").strip(),
            ) in callable_keys
        ]
        selected = next(
            (
                member
                for member in members
                if str(member.get("stage") or "").strip().lower() == "decision"
            ),
            None,
        )
        if selected is None:
            moderator_id = str((plan.get("moderator") or {}).get("id") or "")
            selected = next(
                (member for member in members if str(member.get("id") or "") == moderator_id),
                None,
            )
        if selected is None:
            raise ValueError("the confirmed plan has no artifact route")
        route = {
            "member_id": str(selected.get("id") or ""),
            "member_version": int(selected.get("version") or 0),
            "provider": str(selected.get("provider") or "").strip().lower(),
            "model": str(selected.get("model") or "").strip(),
        }
        if (
            not route["member_id"]
            or route["member_version"] < 1
            or not route["provider"]
            or not route["model"]
        ):
            raise ValueError("the confirmed artifact route is incomplete")
        return route

    @staticmethod
    def _launch_plan_member_routes(plan: dict[str, Any]) -> dict[str, Any]:
        """Project every confirmed member onto the sealed Provider route manifest."""

        raw_members = plan.get("members")
        if not isinstance(raw_members, list) or not raw_members:
            raise ValueError("the confirmed plan has no member routes")
        raw_preflight_routes = plan.get("preflight_routes")
        if not isinstance(raw_preflight_routes, list):
            raise ValueError("the confirmed plan has no output routes")
        output_modes_by_route: dict[tuple[str, str], str] = {}
        for raw_route in raw_preflight_routes:
            if not isinstance(raw_route, dict):
                raise ValueError("the confirmed output route is invalid")
            route_key = (
                str(raw_route.get("provider") or "").strip().lower(),
                str(raw_route.get("model") or "").strip(),
            )
            if route_key in output_modes_by_route:
                raise ValueError("the confirmed output route is duplicated")
            output_modes_by_route[route_key] = normalize_turn_envelope_mode(
                raw_route.get("turn_output_mode")
            )
        routes: list[dict[str, Any]] = []
        seen_member_ids: set[str] = set()
        for member in raw_members:
            if not isinstance(member, dict):
                raise ValueError("the confirmed member route is invalid")
            member_id = str(member.get("id") or "").strip()
            provider_id = str(member.get("provider") or "").strip().lower()
            model_id = str(member.get("model") or "").strip()
            try:
                member_version = int(member.get("version") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("the confirmed member version is invalid") from exc
            if (
                not member_id
                or member_id in seen_member_ids
                or member_version < 1
                or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", provider_id)
                or not model_id
                or len(model_id) > 160
            ):
                raise ValueError("the confirmed member route is incomplete")
            route_key = (provider_id, model_id)
            if route_key not in output_modes_by_route:
                raise ValueError("the confirmed member output route is incomplete")
            seen_member_ids.add(member_id)
            routes.append({
                "member_id": member_id,
                "approved_member_version": member_version,
                "provider": provider_id,
                "model": model_id,
                "turn_output_mode": output_modes_by_route[route_key],
                "turn_envelope_version": TURN_ENVELOPE_VERSION,
                "turn_envelope_schema_sha256": TURN_ENVELOPE_SCHEMA_SHA256,
            })
        routes.sort(key=lambda item: item["member_id"])
        return {
            "version": "provider_member_routes_v2",
            "members": routes,
        }

    @staticmethod
    def _round_authorization_fields(
        payload: dict[str, Any],
    ) -> tuple[str, str, int]:
        raw_request_id = payload.get("client_round_request_id")
        if not isinstance(raw_request_id, str):
            raise ValueError("client_round_request_id is required")
        request_id = raw_request_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}", request_id):
            raise ValueError("client_round_request_id is invalid")
        raw_plan_hash = payload.get("plan_hash")
        if not isinstance(raw_plan_hash, str):
            raise ValueError("plan_hash is required")
        plan_hash = raw_plan_hash.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", plan_hash):
            raise ValueError("plan_hash is invalid")
        max_calls = payload.get("max_provider_calls")
        if (
            isinstance(max_calls, bool)
            or not isinstance(max_calls, int)
            or max_calls < 1
            or max_calls > 100
        ):
            raise ValueError("max_provider_calls is invalid")
        return request_id, plan_hash, max_calls

    @staticmethod
    def _ledger_skip_provider_ids(provider_execution: dict[str, Any]) -> set[str]:
        skip_policy = provider_execution.get("skip_policy")
        if not isinstance(skip_policy, dict):
            raise ValueError("provider execution skip policy is invalid")
        raw_provider_ids = skip_policy.get("provider_ids")
        if not isinstance(raw_provider_ids, list):
            raise ValueError("provider execution skip policy is invalid")
        clean: set[str] = set()
        for raw_provider_id in raw_provider_ids:
            provider_id = str(raw_provider_id or "").strip().lower()
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", provider_id):
                raise ValueError("provider execution skip policy is invalid")
            clean.add(provider_id)
        return clean

    @classmethod
    def _artifact_provider_statuses(cls) -> list[dict[str, Any]]:
        registry = getattr(ARTIFACTS, "providers", None) or cls._round_provider_registry()
        try:
            statuses = registry.status()
        except Exception:
            return []
        return [
            {
                "id": str(status.get("id") or "").strip().lower(),
                "configured": status.get("configured") is True,
                "policy_disabled": status.get("policy_disabled") is True,
            }
            for status in statuses
            if isinstance(status, dict)
            and re.fullmatch(
                r"[a-z][a-z0-9_-]{0,39}",
                str(status.get("id") or "").strip().lower(),
            )
        ]

    @staticmethod
    def _safe_error_code(value: Any, fallback: str) -> str:
        code = str(value or "").strip().upper()
        allowed_prefixes = (
            "ROUND_",
            "PROVIDER_",
            "WORKFLOW_",
            "FUTU_",
            "MEMBER_",
            "MODERATOR_",
            "CHECKPOINT_",
            "DIRECTOR_",
            "ARTIFACT_",
        )
        allowed_exact = {
            "BELOW_RECOMMENDED_PROVIDER_CALLS",
            "NOT_CONFIGURED",
            "PROBE_FAILED",
            "INVALID_RESPONSE",
            "MODEL_ACCESS_DENIED",
        }
        if (
            re.fullmatch(r"[A-Z0-9][A-Z0-9_.-]{0,79}", code)
            and (code.startswith(allowed_prefixes) or code in allowed_exact)
        ):
            return code
        return fallback

    @staticmethod
    def _safe_nonnegative_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _safe_gate_preflight(cls, value: Any) -> dict[str, Any]:
        preflight = value if isinstance(value, dict) else {}
        safe: dict[str, Any] = {
            key: preflight[key]
            for key in ("applicable", "ready", "live_trading_allowed")
            if isinstance(preflight.get(key), bool)
        }
        state = str(preflight.get("state") or "").strip().lower()
        if state in {"ready", "offline", "degraded", "unavailable", "error"}:
            safe["state"] = state
        snapshot_origin = str(
            preflight.get("snapshot_origin") or ""
        ).strip().lower()
        if snapshot_origin in {
            "frozen_checkpoint",
            "fresh_capture",
            "prefetched",
            "none",
        }:
            safe["snapshot_origin"] = snapshot_origin
        if preflight.get("execution_capability") == "none":
            safe["execution_capability"] = "none"
        capture_error = preflight.get("capture_error")
        if isinstance(capture_error, dict):
            safe["capture_error"] = {
                "code": cls._safe_error_code(
                    capture_error.get("code"),
                    "PREFLIGHT_ERROR",
                )
            }
        blockers: list[dict[str, Any]] = []
        for blocker in preflight.get("blockers") or []:
            if not isinstance(blocker, dict):
                continue
            blockers.append({
                "code": cls._safe_error_code(
                    blocker.get("code"),
                    "PREFLIGHT_BLOCKED",
                )
            })
        if blockers:
            safe["blockers"] = blockers
        return safe

    @classmethod
    def _safe_provider_preflight(cls, value: Any) -> dict[str, Any]:
        preflight = value if isinstance(value, dict) else {}

        def safe_assignment(item: Any) -> dict[str, Any]:
            assignment = item if isinstance(item, dict) else {}
            provider = str(assignment.get("provider") or "").strip().lower()
            model = str(assignment.get("model") or "").strip()
            raw_error_code = str(assignment.get("error_code") or "").strip()
            return {
                "id": (
                    str(assignment.get("id") or "")[:80]
                    if re.fullmatch(
                        r"[A-Za-z0-9_-]{1,80}",
                        str(assignment.get("id") or ""),
                    )
                    else ""
                ),
                "available": assignment.get("available") is True,
                "is_moderator": assignment.get("is_moderator") is True,
                "provider": (
                    provider
                    if re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", provider)
                    else ""
                ),
                "model": (
                    model
                    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", model)
                    else ""
                ),
                "error_code": (
                    cls._safe_error_code(
                        raw_error_code,
                        "PROVIDER_PREFLIGHT_FAILED",
                    )
                    if raw_error_code
                    else ""
                ),
            }

        provider_checks: list[dict[str, Any]] = []
        for raw_check in preflight.get("provider_checks") or []:
            if not isinstance(raw_check, dict):
                continue
            provider = str(raw_check.get("provider") or "").strip().lower()
            model = str(raw_check.get("model") or "").strip()
            raw_error_code = str(raw_check.get("error_code") or "").strip()
            provider_checks.append({
                "provider": (
                    provider
                    if re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", provider)
                    else ""
                ),
                "model": (
                    model
                    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", model)
                    else ""
                ),
                "configured": raw_check.get("configured") is True,
                "reachable": raw_check.get("reachable") is True,
                "model_access": raw_check.get("model_access") is True,
                "cached": raw_check.get("cached") is True,
                "error_code": (
                    cls._safe_error_code(
                        raw_error_code,
                        "PROVIDER_PREFLIGHT_FAILED",
                    )
                    if raw_error_code
                    else ""
                ),
                "member_count": cls._safe_nonnegative_int(
                    raw_check.get("member_count")
                ),
            })
        return {
            "ready": preflight.get("ready") is True,
            "member_count": cls._safe_nonnegative_int(
                preflight.get("member_count")
            ),
            "provider_check_count": cls._safe_nonnegative_int(
                preflight.get("provider_check_count") or len(provider_checks)
            ),
            "provider_checks": provider_checks,
            "members": [
                safe_assignment(item) for item in preflight.get("members") or []
            ],
            "unavailable_members": [
                safe_assignment(item)
                for item in preflight.get("unavailable_members") or []
            ],
            "moderator": safe_assignment(preflight.get("moderator")),
        }

    @classmethod
    def _safe_round_stream_event(cls, event: Any) -> dict[str, Any]:
        if not isinstance(event, dict):
            return {
                "type": "error",
                "code": "ROUND_STREAM_EVENT_INVALID",
                "error": "The round returned an invalid stream event.",
            }
        if str(event.get("type") or "") != "error":
            return event
        safe_event = {
            "type": "error",
            "code": cls._safe_error_code(
                event.get("code"),
                "ROUND_EXECUTION_FAILED",
            ),
            "error": "Round execution stopped safely; inspect persisted room state.",
        }
        if re.fullmatch(r"[a-z0-9_-]{1,80}", str(event.get("stage") or "")):
            safe_event["stage"] = str(event.get("stage"))
        return safe_event

    @staticmethod
    def _skip_provider_ids(payload: dict[str, Any]) -> set[str]:
        raw_skip_providers = payload.get("skip_providers", ["openai"])
        if not isinstance(raw_skip_providers, list):
            raise ValueError("skip_providers 必须是数组。")
        if len(raw_skip_providers) > 20:
            raise ValueError("skip_providers 最多包含 20 项。")
        skip_provider_ids: set[str] = set()
        for raw_provider_id in raw_skip_providers:
            provider_id = str(raw_provider_id or "").strip().lower()
            if not provider_id:
                continue
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", provider_id):
                raise ValueError("skip_providers 包含无效的 Provider 标识。")
            skip_provider_ids.add(provider_id)
        execution_registry = getattr(ORCHESTRATOR, "providers", PROVIDERS)
        policy_disabled = getattr(
            execution_registry,
            "disabled_provider_ids",
            frozenset(),
        )
        skip_provider_ids.update(
            str(provider_id or "").strip().lower()
            for provider_id in policy_disabled
            if str(provider_id or "").strip()
        )
        try:
            local_statuses = execution_registry.status()
        except Exception:
            local_statuses = []
        skip_provider_ids.update(
            str(status.get("id") or "").strip().lower()
            for status in local_statuses
            if isinstance(status, dict)
            and status.get("policy_disabled") is True
            and re.fullmatch(
                r"[a-z][a-z0-9_-]{0,39}",
                str(status.get("id") or "").strip().lower(),
            )
        )
        return skip_provider_ids

    def _read_json(
        self,
        *,
        max_bytes: int = 128_000,
        strict: bool = False,
    ) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > max_bytes:
            self._send_json({"ok": False, "error": "请求内容为空或过大"}, HTTPStatus.BAD_REQUEST)
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            if strict:
                def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                    output: dict[str, Any] = {}
                    for key, value in pairs:
                        if key in output:
                            raise ValueError("duplicate JSON key")
                        output[key] = value
                    return output

                def reject_constant(_value: str) -> None:
                    raise ValueError("non-finite JSON number")

                payload = json.loads(
                    raw,
                    object_pairs_hook=unique_object,
                    parse_constant=reject_constant,
                )
            else:
                payload = json.loads(raw)
        except Exception:
            self._send_json({"ok": False, "error": "JSON 格式无效"}, HTTPStatus.BAD_REQUEST)
            return None
        if type(payload) is not dict:
            self._send_json({"ok": False, "error": "请求必须是 JSON 对象"}, HTTPStatus.BAD_REQUEST)
            return None
        return payload

    @staticmethod
    def _local_hostname(value: str) -> str:
        parsed = urlparse(value if "://" in value else f"//{value}")
        return str(parsed.hostname or "").lower()

    def _origin_matches_host(self, origin: str) -> bool:
        if not origin:
            return True
        parsed = urlparse(origin)
        if parsed.scheme not in {"http", "https"} or self._local_hostname(origin) not in LOCAL_HOSTNAMES:
            return False
        return parsed.netloc.lower() == str(self.headers.get("Host") or "").lower()

    def _discard_rejected_request_body(self, *, max_bytes: int = 128_000) -> None:
        """Boundedly drain a rejected local request before closing its socket.

        On Windows, closing a TCP socket while a POST body remains unread can
        emit an RST and discard the 4xx response already written by the
        handler.  Only an exact, bounded Content-Length is drained; chunked,
        malformed, or oversized bodies remain fail-closed without unbounded
        reads.  Rejected requests never reuse the connection.
        """

        raw_length = str(self.headers.get("Content-Length") or "").strip()
        transfer_encoding = str(
            self.headers.get("Transfer-Encoding") or ""
        ).strip()
        if not raw_length and not transfer_encoding:
            return
        self.close_connection = True
        if transfer_encoding:
            return
        try:
            length = int(raw_length)
        except ValueError:
            return
        if length <= 0 or length > max_bytes:
            return
        previous_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(0.25)
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 64_000))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            return
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                pass

    def _reject_guarded_request(
        self,
        payload: dict[str, Any],
        status: HTTPStatus,
    ) -> bool:
        self._discard_rejected_request_body()
        self._send_json(payload, status)
        return False

    def _project_capability_transport(
        self,
        *,
        action: str,
        require_json: bool,
    ) -> tuple[
        ProjectCapabilityAuthorizer,
        str,
        ProjectCapabilityClaims,
    ] | None:
        """Authenticate the dedicated integration plane without UI-token fallback."""

        if not self._guard_request(require_same_origin=True):
            return None
        if require_json:
            content_type = str(self.headers.get("Content-Type") or "").split(
                ";", 1
            )[0].strip().lower()
            if content_type != "application/json":
                self._reject_guarded_request(
                    {
                        "ok": False,
                        "error": "Project invocation writes require application/json.",
                        "error_code": "PROJECT_INVOCATION_CONTENT_TYPE_INVALID",
                    },
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
                return None

        # A browser bootstrap token is deliberately invalid on this plane. Reject
        # ambiguous dual credentials even when the bearer itself would verify.
        if self.headers.get_all("X-AI-Studio-Token", []):
            self._reject_guarded_request(
                {
                    "ok": False,
                    "error": "The project capability credential is missing or invalid.",
                    "error_code": "PROJECT_CAPABILITY_UNAUTHORIZED",
                },
                HTTPStatus.UNAUTHORIZED,
            )
            return None

        authorizations = self.headers.get_all("Authorization", [])
        if len(authorizations) != 1:
            self._reject_guarded_request(
                {
                    "ok": False,
                    "error": "The project capability credential is missing or invalid.",
                    "error_code": "PROJECT_CAPABILITY_UNAUTHORIZED",
                },
                HTTPStatus.UNAUTHORIZED,
            )
            return None
        authorization = authorizations[0]
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)", authorization)
        if not match:
            self._reject_guarded_request(
                {
                    "ok": False,
                    "error": "The project capability credential is missing or invalid.",
                    "error_code": "PROJECT_CAPABILITY_UNAUTHORIZED",
                },
                HTTPStatus.UNAUTHORIZED,
            )
            return None
        try:
            authorizer = ProjectCapabilityAuthorizer(
                PROJECT_CAPABILITY_SIGNING_SECRET
            )
        except ProjectInvocationError:
            self._reject_guarded_request(
                {
                    "ok": False,
                    "error": "Project invocation capability verification is unavailable.",
                    "error_code": "PROJECT_CAPABILITY_UNAVAILABLE",
                },
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return None
        token = match.group(1)
        try:
            claims = authorizer.authorize(token, action=action)
        except ProjectInvocationError as exc:
            self._reject_guarded_request(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                },
                HTTPStatus(exc.status),
            )
            return None
        return authorizer, token, claims

    def _handle_project_invocation_intake(self, parsed: Any) -> None:
        authenticated = self._project_capability_transport(
            action=PROJECT_INVOCATION_ACTION_INTAKE,
            require_json=True,
        )
        if authenticated is None:
            return
        authorizer, token, _ = authenticated
        if parsed.query or parsed.params or parsed.fragment:
            self._reject_guarded_request(
                {
                    "ok": False,
                    "error": "Project invocation intake does not accept URL parameters.",
                    "error_code": "PROJECT_INVOCATION_QUERY_UNSUPPORTED",
                },
                HTTPStatus.BAD_REQUEST,
            )
            return
        payload = self._read_json(max_bytes=256_000)
        if payload is None:
            return
        try:
            envelope = normalize_project_invocation_envelope(payload)
            claims = authorizer.authorize(
                token,
                caller_id=envelope["caller_id"],
                project_id=envelope["project_id"],
                room_id=envelope["room_id"],
                action=PROJECT_INVOCATION_ACTION_INTAKE,
                client_request_id=envelope["client_request_id"],
                request_sha256=envelope["request_sha256"],
            )

            def reauthorize() -> None:
                authorizer.authorize(
                    token,
                    caller_id=envelope["caller_id"],
                    project_id=envelope["project_id"],
                    room_id=envelope["room_id"],
                    action=PROJECT_INVOCATION_ACTION_INTAKE,
                    client_request_id=envelope["client_request_id"],
                    request_sha256=envelope["request_sha256"],
                )

            invocation, created = STORE.create_project_invocation(
                envelope,
                request_semantics=project_invocation_semantics(envelope),
                authorization={
                    "authorization_sha256": project_capability_claims_sha256(
                        claims
                    ),
                    "jti": claims.token_id,
                    "expires_at": claims.expires_at,
                },
                reauthorize=reauthorize,
            )
        except ProjectInvocationError as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                },
                HTTPStatus(exc.status),
                cache_control="no-store",
            )
            return
        except ProjectInvocationStoreError as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                },
                HTTPStatus(exc.status),
                cache_control="no-store",
            )
            return
        self._send_json(
            {
                "ok": True,
                "created": created,
                "invocation": invocation,
            },
            HTTPStatus.CREATED if created else HTTPStatus.OK,
            cache_control="no-store",
        )

    def _handle_project_invocation_result(
        self,
        parsed: Any,
        client_request_id: str,
    ) -> None:
        authenticated = self._project_capability_transport(
            action=PROJECT_INVOCATION_ACTION_RESULT_READ,
            require_json=False,
        )
        if authenticated is None:
            return
        authorizer, token, _ = authenticated
        if parsed.query or parsed.params or parsed.fragment:
            self._send_json(
                {
                    "ok": False,
                    "error": "Project invocation result reads do not accept URL parameters.",
                    "error_code": "PROJECT_INVOCATION_QUERY_UNSUPPORTED",
                },
                HTTPStatus.BAD_REQUEST,
                cache_control="no-store",
            )
            return
        try:
            claims = authorizer.authorize(
                token,
                action=PROJECT_INVOCATION_ACTION_RESULT_READ,
                client_request_id=client_request_id,
            )
            details = STORE.get_project_invocation_details(
                caller_id=claims.caller_id,
                project_id=claims.project_id,
                client_request_id=claims.client_request_id,
            )
            if details is None:
                self._send_json(
                    {
                        "ok": False,
                        "error": "Project invocation was not found.",
                        "error_code": "PROJECT_INVOCATION_NOT_FOUND",
                    },
                    HTTPStatus.NOT_FOUND,
                    cache_control="no-store",
                )
                return
            invocation = details["invocation"]
            semantics = details["request_semantics"]
            if (
                invocation.get("caller_id") != claims.caller_id
                or invocation.get("project_id") != claims.project_id
                or invocation.get("client_request_id")
                != claims.client_request_id
                or invocation.get("request_sha256") != claims.request_sha256
                or (invocation.get("room_binding") or {}).get("room_id")
                != claims.room_id
            ):
                raise ProjectInvocationStoreError(
                    "项目调用结果授权与持久化身份不一致。",
                    code="PROJECT_INVOCATION_INTEGRITY_FAILED",
                )

            room_binding = invocation.get("room_binding") or {}
            room_id = str(room_binding.get("room_id") or "")
            room_version = STORE.get_room_version_record(
                room_id,
                int(room_binding.get("settings_version") or 0),
            )
            if room_version is None:
                raise ProjectInvocationStoreError(
                    "项目调用创建时的房间版本不可用。",
                    code="PROJECT_INVOCATION_INTEGRITY_FAILED",
                )

            manual_session = ManualChatGPTService(STORE).latest(room_id)
            artifact_version = None
            artifacts = STORE.list_artifacts(room_id)
            if artifacts:
                latest_artifact = artifacts[0]
                artifact_version = STORE.get_artifact_version(
                    room_id,
                    str(latest_artifact.get("id") or ""),
                    int(latest_artifact.get("version") or 0),
                )
            result = ProjectIntegrationService.project_result(
                semantics,
                studio_snapshot=room_version,
                manual_session=manual_session,
                artifact=artifact_version,
            )
            result_bytes = len(json_bytes(result))
            result_budget = int(
                (semantics.get("budget") or {}).get("max_result_bytes") or 0
            )
            if result_budget <= 0 or result_bytes > result_budget:
                self._send_json(
                    {
                        "ok": False,
                        "error": "The portable result exceeds the invocation result budget.",
                        "error_code": "PROJECT_INVOCATION_RESULT_BUDGET_EXCEEDED",
                        "result_bytes": result_bytes,
                        "max_result_bytes": result_budget,
                    },
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    cache_control="no-store",
                )
                return
            # Close the read/expiry race immediately before returning data.
            authorizer.authorize(
                token,
                caller_id=claims.caller_id,
                project_id=claims.project_id,
                room_id=claims.room_id,
                action=PROJECT_INVOCATION_ACTION_RESULT_READ,
                client_request_id=claims.client_request_id,
                request_sha256=claims.request_sha256,
            )
        except ProjectInvocationError as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                },
                HTTPStatus(exc.status),
                cache_control="no-store",
            )
            return
        except ProjectInvocationStoreError as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": exc.code,
                },
                HTTPStatus(exc.status),
                cache_control="no-store",
            )
            return
        except (ManualChatGPTError, ProjectIntegrationError, ValueError) as exc:
            error_code = str(
                getattr(exc, "code", "PROJECT_INVOCATION_RESULT_INVALID")
            )
            error_status = int(getattr(exc, "status", HTTPStatus.CONFLICT))
            self._send_json(
                {
                    "ok": False,
                    "error": str(exc),
                    "error_code": error_code,
                },
                HTTPStatus(error_status),
                cache_control="no-store",
            )
            return
        self._send_json(
            {
                "ok": True,
                "result": result,
            },
            cache_control="no-store",
        )

    def _guard_request(self, *, mutating: bool = False, require_same_origin: bool = False) -> bool:
        client_host = (
            self.client_address[0]
            if isinstance(self.client_address, tuple) and self.client_address
            else ""
        )
        if not _is_loopback_address(client_host):
            return self._reject_guarded_request(
                {"ok": False, "error": "仅允许本机访问"},
                HTTPStatus.FORBIDDEN,
            )
        request_path = urlparse(self.path).path
        if request_path == "/api" or request_path.startswith("/api/"):
            try:
                ensure_safe_api_path(request_path)
            except ExecutionBoundaryViolation as exc:
                return self._reject_guarded_request(
                    {
                        "ok": False,
                        "error": str(exc),
                        "error_code": "EXECUTION_CAPABILITY_DISABLED",
                    },
                    HTTPStatus.FORBIDDEN,
                )
        host = str(self.headers.get("Host") or "")
        if self._local_hostname(host) not in LOCAL_HOSTNAMES:
            return self._reject_guarded_request(
                {"ok": False, "error": "仅允许本机访问"},
                HTTPStatus.FORBIDDEN,
            )
        origin = str(self.headers.get("Origin") or "")
        if (require_same_origin or mutating) and origin and not self._origin_matches_host(origin):
            return self._reject_guarded_request(
                {"ok": False, "error": "请求来源不受信任"},
                HTTPStatus.FORBIDDEN,
            )
        if not mutating:
            return True
        content_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return self._reject_guarded_request(
                {"ok": False, "error": "写入请求必须使用 application/json"},
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
        supplied_token = str(self.headers.get("X-AI-Studio-Token") or "")
        if not supplied_token or not secrets.compare_digest(supplied_token, LOCAL_SESSION_TOKEN):
            return self._reject_guarded_request(
                {"ok": False, "error": "本机会话校验失败，请刷新页面后重试"},
                HTTPStatus.FORBIDDEN,
            )
        return True

    def _send_json(
        self,
        payload: dict[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
        *,
        cache_control: str | None = None,
    ) -> None:
        if cache_control is None and _is_source_inbox_path(urlparse(self.path).path):
            cache_control = "no-store"
        body = json_bytes(payload)
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Browsers legitimately cancel slow read-only requests while switching
            # rooms or reloading; do not turn that client lifecycle into a server error.
            return

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin and self._origin_matches_host(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _serve_static(self, request_path: str) -> None:
        if not FRONTEND_DIST.is_dir():
            self._send_json({"ok": False, "error": "前端尚未构建，请先运行 npm.cmd run build"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        relative = request_path.lstrip("/") or "index.html"
        candidate = (FRONTEND_DIST / relative).resolve()
        if FRONTEND_DIST.resolve() not in candidate.parents and candidate != FRONTEND_DIST.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            candidate = FRONTEND_DIST / "index.html"
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return


def run_server(
    host: str = HOST,
    port: int = PORT,
    *,
    instance_owner: DatabaseInstanceOwner,
) -> None:
    if not _is_loopback_address(host):
        raise ValueError("AI 共创室只能监听回环地址")
    configured_store_path = getattr(STORE, "configured_path", None)
    if configured_store_path is not None:
        # The default store is lazy. Reject a mismatched owner before resolving
        # STORE.path so an invalid direct caller cannot run migrations first.
        instance_owner.assert_held_for(configured_store_path)
    store_path = STORE.path
    instance_owner.assert_held_for(store_path)
    server = ThreadingHTTPServer((host, port), StudioRequestHandler)
    server.ai_studio_startup_ready = False
    started = False
    try:
        recovery = STORE.recover_orphaned_work(instance_owner=instance_owner)
        server.ai_studio_startup_ready = True
        started = True
        emit_event(
            "server_started",
            fields={
                "bind_scope": "loopback",
                "port": int(server.server_port),
            },
        )
        recovery_counts = {
            "recovered_chat_targets": int(
                recovery.get("recovered_chat_targets", 0) or 0
            ),
            "paused_rounds": int(recovery.get("paused_rounds", 0) or 0),
            "cancelled_rounds": int(recovery.get("cancelled_rounds", 0) or 0),
        }
        if any(recovery_counts.values()):
            emit_event("server_state_recovered", fields=recovery_counts)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            emit_event("server_interrupt_received")
    finally:
        server.server_close()
        emit_event("server_stopped", fields={"started": started})
