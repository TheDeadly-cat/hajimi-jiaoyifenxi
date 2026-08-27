from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .decision_lineage import canonical_sha256
from .path_identity import first_reparse_component


TUNNEL_PREFLIGHT_EVIDENCE_VERSION = "secure_mcp_tunnel_preflight_evidence_v1"
TUNNEL_PREFLIGHT_REPORT_VERSION = "secure_mcp_tunnel_preflight_report_v1"
OPENAI_SECURE_MCP_TUNNEL_GUIDE = (
    "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels"
)
MAX_TUNNEL_PREFLIGHT_EVIDENCE_BYTES = 32 * 1024


class SecureMCPTunnelPreflightError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "TUNNEL_PREFLIGHT_EVIDENCE_INVALID",
    ) -> None:
        super().__init__(message)
        self.code = code


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


def _exact_object(
    value: Any,
    *,
    path: str,
    required: set[str],
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != required:
        raise SecureMCPTunnelPreflightError(
            f"{path} must contain exactly: {', '.join(sorted(required))}."
        )
    return value


def _boolean(value: Any, *, path: str) -> bool:
    if not isinstance(value, bool):
        raise SecureMCPTunnelPreflightError(f"{path} must be a boolean.")
    return value


def _normalized_evidence(raw: Any) -> dict[str, Any]:
    evidence = _exact_object(
        raw,
        path="$",
        required={
            "version",
            "tunnel_id_available",
            "runtime_api_key_available",
            "platform_permissions",
            "target_chatgpt_workspace_associated",
            "chatgpt_developer_mode_enabled",
            "outbound_https_available",
            "tunnel_client_available",
            "local_mcp_reachable",
            "doctor",
        },
    )
    if evidence.get("version") != TUNNEL_PREFLIGHT_EVIDENCE_VERSION:
        raise SecureMCPTunnelPreflightError("$.version is unsupported.")
    permissions = _exact_object(
        evidence.get("platform_permissions"),
        path="$.platform_permissions",
        required={"read", "use", "manage"},
    )
    doctor = _exact_object(
        evidence.get("doctor"),
        path="$.doctor",
        required={"executed", "healthy", "ready", "connected"},
    )
    normalized = {
        "version": TUNNEL_PREFLIGHT_EVIDENCE_VERSION,
        "tunnel_id_available": _boolean(
            evidence.get("tunnel_id_available"),
            path="$.tunnel_id_available",
        ),
        "runtime_api_key_available": _boolean(
            evidence.get("runtime_api_key_available"),
            path="$.runtime_api_key_available",
        ),
        "platform_permissions": {
            key: _boolean(permissions.get(key), path=f"$.platform_permissions.{key}")
            for key in ("read", "use", "manage")
        },
        "target_chatgpt_workspace_associated": _boolean(
            evidence.get("target_chatgpt_workspace_associated"),
            path="$.target_chatgpt_workspace_associated",
        ),
        "chatgpt_developer_mode_enabled": _boolean(
            evidence.get("chatgpt_developer_mode_enabled"),
            path="$.chatgpt_developer_mode_enabled",
        ),
        "outbound_https_available": _boolean(
            evidence.get("outbound_https_available"),
            path="$.outbound_https_available",
        ),
        "tunnel_client_available": _boolean(
            evidence.get("tunnel_client_available"),
            path="$.tunnel_client_available",
        ),
        "local_mcp_reachable": _boolean(
            evidence.get("local_mcp_reachable"),
            path="$.local_mcp_reachable",
        ),
        "doctor": {
            key: _boolean(doctor.get(key), path=f"$.doctor.{key}")
            for key in ("executed", "healthy", "ready", "connected")
        },
    }
    doctor_state = normalized["doctor"]
    if not doctor_state["executed"] and any(
        doctor_state[key] for key in ("healthy", "ready", "connected")
    ):
        raise SecureMCPTunnelPreflightError(
            "$.doctor cannot report runtime state when doctor was not executed."
        )
    if doctor_state["connected"] and not (
        doctor_state["healthy"] and doctor_state["ready"]
    ):
        raise SecureMCPTunnelPreflightError(
            "$.doctor.connected requires healthy and ready."
        )
    return normalized


def evaluate_secure_mcp_tunnel_preflight(raw: Any) -> dict[str, Any]:
    """Evaluate declared tunnel prerequisites without touching network or secrets."""

    evidence = _normalized_evidence(raw)
    permissions = evidence["platform_permissions"]
    doctor = evidence["doctor"]
    checks = [
        {
            "id": "platform_tunnels_read",
            "layer": "platform_organization_permission",
            "satisfied": permissions["read"],
            "required_for": "inspect_and_select_tunnel",
        },
        {
            "id": "platform_tunnels_use",
            "layer": "platform_organization_permission",
            "satisfied": permissions["use"],
            "required_for": "run_client_and_select_tunnel",
        },
        {
            "id": "tunnel_id_available",
            "layer": "platform_tunnel_identity",
            "satisfied": evidence["tunnel_id_available"],
            "required_for": "configure_tunnel_client",
        },
        {
            "id": "runtime_api_key_available",
            "layer": "runtime_credential",
            "satisfied": evidence["runtime_api_key_available"],
            "required_for": "authenticate_tunnel_client",
        },
        {
            "id": "target_chatgpt_workspace_associated",
            "layer": "workspace_association",
            "satisfied": evidence["target_chatgpt_workspace_associated"],
            "required_for": "discover_tunnel_in_chatgpt",
        },
        {
            "id": "chatgpt_developer_mode_enabled",
            "layer": "chatgpt_workspace_permission",
            "satisfied": evidence["chatgpt_developer_mode_enabled"],
            "required_for": "create_developer_mode_app",
        },
        {
            "id": "tunnel_client_available",
            "layer": "local_runtime",
            "satisfied": evidence["tunnel_client_available"],
            "required_for": "run_private_tunnel",
        },
        {
            "id": "outbound_https_available",
            "layer": "network",
            "satisfied": evidence["outbound_https_available"],
            "required_for": "reach_openai_tunnel_control_plane",
        },
        {
            "id": "local_mcp_reachable",
            "layer": "local_gateway",
            "satisfied": evidence["local_mcp_reachable"],
            "required_for": "forward_mcp_requests",
        },
    ]
    missing = [check["id"] for check in checks if not check["satisfied"]]
    ready_to_run_doctor = not missing
    doctor_verified = (
        ready_to_run_doctor
        and doctor["executed"]
        and doctor["healthy"]
        and doctor["ready"]
        and doctor["connected"]
    )
    if missing:
        next_step = {
            "id": "resolve_declared_prerequisites",
            "detail": "Resolve or verify each missing prerequisite before running tunnel-client.",
        }
    elif not doctor["executed"]:
        next_step = {
            "id": "run_tunnel_client_doctor",
            "detail": "Run tunnel-client doctor --profile <profile> --explain.",
        }
    elif not doctor_verified:
        next_step = {
            "id": "repair_tunnel_runtime",
            "detail": "Use doctor output to restore healthy, ready, and connected state.",
        }
    else:
        next_step = {
            "id": "test_chatgpt_developer_app",
            "detail": "Test the read-only tools from the explicitly authorized ChatGPT workspace.",
        }
    report: dict[str, Any] = {
        "version": TUNNEL_PREFLIGHT_REPORT_VERSION,
        "target_surface": "chatgpt_developer_mode_app",
        "assessment_basis": "user_declared_boolean_evidence",
        "evidence_version": evidence["version"],
        "evidence_sha256": canonical_sha256(evidence),
        "official_guidance": OPENAI_SECURE_MCP_TUNNEL_GUIDE,
        "checks": checks,
        "missing_or_unverified": missing,
        "tunnel_administration": {
            "manage_permission_declared": permissions["manage"],
            "required_to_create_or_edit_tunnel": True,
            "required_to_use_existing_tunnel": False,
        },
        "readiness": {
            "ready_to_run_doctor": ready_to_run_doctor,
            "runtime_doctor_executed": doctor["executed"],
            "runtime_healthy": doctor["healthy"],
            "runtime_ready": doctor["ready"],
            "runtime_connected": doctor["connected"],
            "runtime_connection_verified_by_declared_evidence": doctor_verified,
            "external_permission_truth_verified": False,
        },
        "next_step": next_step,
        "safety": {
            "secret_values_accepted": False,
            "secret_values_returned": False,
            "network_calls_performed": 0,
            "provider_calls_performed": 0,
            "market_calls_performed": 0,
            "database_reads_performed": 0,
            "database_writes_performed": 0,
            "local_mcp_server_started": False,
            "tunnel_client_started": False,
            "inbound_public_port_required_by_architecture": False,
        },
    }
    report["report_sha256"] = canonical_sha256(report)
    return report


def _read_evidence_file(path: str | Path) -> Any:
    requested = Path(path).expanduser()
    if first_reparse_component(requested) is not None:
        raise SecureMCPTunnelPreflightError(
            "The evidence path contains a reparse point.",
            code="TUNNEL_PREFLIGHT_PATH_UNSAFE",
        )
    clean_path = requested.resolve()
    if not clean_path.is_file():
        raise SecureMCPTunnelPreflightError(
            "The evidence file does not exist.",
            code="TUNNEL_PREFLIGHT_EVIDENCE_MISSING",
        )
    try:
        before = clean_path.stat()
        if before.st_size > MAX_TUNNEL_PREFLIGHT_EVIDENCE_BYTES:
            raise SecureMCPTunnelPreflightError(
                "The evidence file exceeds the size limit.",
                code="TUNNEL_PREFLIGHT_EVIDENCE_TOO_LARGE",
            )
        raw = clean_path.read_bytes()
        parsed = _strict_json_loads(raw)
        after = clean_path.stat()
    except SecureMCPTunnelPreflightError:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SecureMCPTunnelPreflightError(
            "The evidence file is not strict UTF-8 JSON.",
        ) from exc
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise SecureMCPTunnelPreflightError(
            "The evidence file changed while it was read.",
            code="TUNNEL_PREFLIGHT_EVIDENCE_CHANGED",
        )
    return parsed


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate declared Secure MCP Tunnel prerequisites without reading secrets, "
            "starting services, or making network calls."
        )
    )
    parser.add_argument("--evidence-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_cli().parse_args(argv)
    try:
        report = evaluate_secure_mcp_tunnel_preflight(
            _read_evidence_file(arguments.evidence_file)
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    except SecureMCPTunnelPreflightError as exc:
        print(
            json.dumps(
                {"ok": False, "code": exc.code, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OPENAI_SECURE_MCP_TUNNEL_GUIDE",
    "SecureMCPTunnelPreflightError",
    "TUNNEL_PREFLIGHT_EVIDENCE_VERSION",
    "TUNNEL_PREFLIGHT_REPORT_VERSION",
    "evaluate_secure_mcp_tunnel_preflight",
]
