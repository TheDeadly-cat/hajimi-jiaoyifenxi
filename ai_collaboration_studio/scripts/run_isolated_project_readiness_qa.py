from __future__ import annotations

"""Serve a disposable P26 project-readiness browser-QA fixture.

This is a test-only harness, not an application launcher.  It configures all
isolation variables before importing the backend, creates an explicit SQLite
database inside a ``TemporaryDirectory``, freezes a confirmed project artifact
and its evidence-review event, and asks Windows for an ephemeral loopback port.

Provider, market, Futu/OpenD, and post-start business-write access all fail
closed.  ``/__qa/status`` exposes the corresponding zero-use counters.  The
temporary database is deleted when the process exits.
"""

import json
import os
import socket
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOM_TITLE = "P26 isolated project readiness QA"
PROJECT_READINESS_PACK_IDS = [
    "structured_project_research",
    "project_readiness_review",
]
KEY_ENV_NAMES = (
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ARK_API_KEY",
    "GLM_API_KEY",
    "ZHIPUAI_API_KEY",
)
ZERO_BUDGET_FIELDS = (
    "provider_calls_performed",
    "market_reads_performed",
    "business_writes_performed",
)
READONLY_SAFETY_FIELDS = {
    "execution_capability": "none",
    "live_trading_allowed": False,
    "can_autonomously_decide": False,
    "can_replace_user_decision": False,
    "arbitrary_code_loading_allowed": False,
    "ranking_produced": False,
    "winner_claim": False,
    "approval_produced": False,
    "user_final_decision_required": True,
}


def _configure_isolation(temp_root: Path) -> Path:
    database_path = temp_root / "project-readiness-browser-qa.sqlite3"
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    os.environ["AI_STUDIO_RUNTIME_DIR"] = str(temp_root)
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(database_path)
    os.environ["AI_STUDIO_HOST"] = "127.0.0.1"
    os.environ["AI_STUDIO_PORT"] = "0"
    os.environ["FUTU_HOST"] = "127.0.0.1"
    os.environ["FUTU_PORT"] = "1"
    os.environ["SEC_USER_AGENT"] = ""
    for name in KEY_ENV_NAMES:
        # Assign an empty value without reading or echoing an inherited secret.
        os.environ[name] = ""
    if os.environ["AI_STUDIO_PORT"] == "8770":
        raise RuntimeError("isolated QA must never configure formal port 8770")
    return database_path


def _reviewed_material(
    material_id: str,
    *,
    role: str = "support",
    status: str = "source_checked",
    note: str = "Reviewed against the frozen local QA material.",
) -> dict[str, str]:
    return {
        "type": "material",
        "id": material_id,
        "evidence_role": role,
        "verification_status": status,
        "review_note": note,
    }


def _fixture_content(material_id: str) -> dict[str, Any]:
    support = _reviewed_material(material_id)
    disputed = _reviewed_material(
        material_id,
        role="counter",
        status="disputed",
        note=(
            "The frozen note supports the capacity concern but does not yet "
            "establish that the proposed mitigation is sufficient."
        ),
    )
    return {
        "summary": (
            "A bounded local prototype is documented, while readiness gaps "
            "remain explicitly unresolved for user review."
        ),
        "summary_evidence": [support],
        "requirements": [{
            "id": "requirement_acceptance_definition",
            "text": "Define measurable acceptance criteria for the first prototype.",
            "status": "pending",
            "owner": "",
            "acceptance_criteria": "",
            "evidence": [support],
        }],
        "risks": [{
            "id": "risk_capacity_blocker",
            "text": "The current team may not cover the full prototype scope.",
            "probability": "high",
            "impact": "high",
            "blocking": True,
            "trigger": "The first-week estimate exceeds ten development days.",
            "mitigation": "",
            "owner": "project owner",
            "status": "open",
            "evidence": [disputed],
        }],
        "conclusions": [],
        "disagreements": [],
        "unknowns": [],
        "actions": [],
        "decision": {
            "status": "undecided",
            "options": [],
            "preferred_option_id": "",
            "rationale": "",
            "evidence": [],
        },
    }


def _assert_projection_is_qa_ready(projection: dict[str, Any]) -> None:
    if projection.get("version") != "project_readiness_projection_v1":
        raise RuntimeError("unexpected project-readiness projection version")
    if projection.get("integrity_ok") is not True:
        raise RuntimeError("project-readiness fixture integrity is not verified")
    if projection.get("metrics_visible") is not True:
        raise RuntimeError("project-readiness fixture metrics are unexpectedly hidden")
    if any(projection.get(field) != 0 for field in ZERO_BUDGET_FIELDS):
        raise RuntimeError("project-readiness fixture violated a zero-use budget")
    if any(
        projection.get(field) != expected
        for field, expected in READONLY_SAFETY_FIELDS.items()
    ):
        raise RuntimeError("project-readiness fixture violated its safety contract")
    if not projection.get("structural_gaps"):
        raise RuntimeError("project-readiness fixture needs a visible structural gap")
    if not projection.get("blockers"):
        raise RuntimeError("project-readiness fixture needs a visible blocker")
    if not projection.get("evidence_gaps"):
        raise RuntimeError("project-readiness fixture needs a visible evidence gap")


def _assert_frozen_readiness_binding(plugin_context: dict[str, Any]) -> None:
    snapshot = plugin_context.get("snapshot") or {}
    expected_requirement = {
        "port_id": "core.artifact.projection/v1",
        "requirement": "required",
        "cardinality": "one",
        "version_range": ">=1.0.0 <2.0.0",
    }
    contributions = [
        row
        for row in snapshot.get("ui_contributions") or []
        if row.get("contribution_id")
        == "project_readiness.artifact_workspace/v1"
    ]
    if len(contributions) != 1:
        raise RuntimeError("the frozen readiness contribution is not unique")
    contribution = contributions[0]
    source_port = contribution.get("source_port") or {}
    view_model = contribution.get("view_model") or {}
    frozen_resolution = contribution.get("source_port_resolution") or {}
    if (
        contribution.get("contract_version") != "ui_contribution_contract_v2"
        or contribution.get("component_key") != "project_readiness_review"
        or source_port.get("owner_pack_id") != "project_readiness_review"
        or source_port.get("requirement") != "required"
        or source_port.get("cardinality") != "one"
        or view_model.get("schema_version")
        != "project_readiness_view_model_v1"
        or frozen_resolution.get("owner_pack_id")
        != source_port.get("owner_pack_id")
        or frozen_resolution.get("port_id") != source_port.get("port_id")
    ):
        raise RuntimeError("the frozen readiness UI binding is not exact v2")
    matching_resolutions = [
        row
        for row in snapshot.get("port_resolutions") or []
        if row.get("owner_pack_id") == source_port.get("owner_pack_id")
        and row.get("port_id") == source_port.get("port_id")
    ]
    if len(matching_resolutions) != 1:
        raise RuntimeError("the frozen readiness source port is not unique")
    resolution = matching_resolutions[0]
    if (
        set(resolution) != {*expected_requirement, "owner_pack_id", "resolved"}
        or any(
            resolution.get(field) != expected
            for field, expected in expected_requirement.items()
        )
    ):
        raise RuntimeError("the frozen readiness port requirement drifted")
    packs = [
        row
        for row in snapshot.get("capability_packs") or []
        if row.get("id") == source_port.get("owner_pack_id")
    ]
    requirements = (
        packs[0].get("domain_adapter_port_requirements") or []
        if len(packs) == 1
        else []
    )
    if len(requirements) != 1 or requirements[0] != expected_requirement:
        raise RuntimeError("the frozen readiness pack requirement drifted")
    resolved = resolution.get("resolved") or []
    if len(resolved) != 1:
        raise RuntimeError("the frozen readiness source port is unresolved")
    binding = resolved[0]
    if (
        binding.get("handler_method") != "project_artifact"
        or binding.get("provider_call_budget") != 0
        or binding.get("market_read_budget") != 0
        or binding.get("business_write_budget") != 0
        or binding.get("failure_policy") != "fail_closed"
    ):
        raise RuntimeError("the frozen readiness resolved port policy drifted")
    for frozen_field, binding_field in (
        ("port_version", "port_version"),
        ("port_contract_sha256", "port_contract_sha256"),
        ("output_schema_version", "output_schema_version"),
        ("output_schema_sha256", "output_schema_sha256"),
    ):
        if frozen_resolution.get(frozen_field) != binding.get(binding_field):
            raise RuntimeError("the frozen readiness source resolution drifted")


class ForbiddenProviders:
    """Provider metadata may render, but every callable Provider path is blocked."""

    def __init__(self) -> None:
        self.call_attempts = 0

    @staticmethod
    def status() -> list[dict[str, Any]]:
        return []

    def __getattr__(self, name: str) -> Any:
        self.call_attempts += 1
        raise AssertionError(f"Provider dependency must not be used: {name}")


class ForbiddenReadSurface:
    """Fail closed if a market or market-readiness surface is accessed."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.access_attempts = 0

    def __getattr__(self, name: str) -> Any:
        self.access_attempts += 1
        raise AssertionError(f"{self.label} dependency must not be used: {name}")


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="ai-studio-p26-project-readiness-qa-"
    ) as raw_temp:
        temp_root = Path(raw_temp).resolve()
        database_path = _configure_isolation(temp_root).resolve()
        if database_path.parent != temp_root:
            raise RuntimeError("QA database escaped its temporary runtime")
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))

        network_state = {"outbound_connect_attempts": 0}
        network_lock = threading.Lock()

        def forbid_outbound_connect(
            _socket: socket.socket,
            address: Any,
        ) -> None:
            with network_lock:
                network_state["outbound_connect_attempts"] += 1
            raise AssertionError(f"outbound network access is forbidden: {address!r}")

        # The outbound connect guard covers backend import and the full QA server
        # lifetime.  Binding/accepting the loopback server does not use connect().
        with patch.object(socket.socket, "connect", new=forbid_outbound_connect):
            # Backend imports must remain below _configure_isolation above.
            from backend import http_server
            from backend.project_readiness import ProjectReadinessService
            from backend.store import StudioStore

            if not http_server.FRONTEND_DIST.joinpath("index.html").is_file():
                raise RuntimeError(
                    "frontend/dist is missing; build the frontend before browser QA"
                )

            store = StudioStore(database_path)
            room_snapshot = store.create_room(
                ROOM_TITLE,
                (
                    "Visual QA for a frozen, read-only project readiness "
                    "projection without Provider, market, or business writes."
                ),
                domain="project_research",
                category="project_research",
                template_id="open_collaboration",
                capability_pack_ids=PROJECT_READINESS_PACK_IDS,
            )
            room_id = str((room_snapshot.get("room") or {}).get("id") or "")
            if not room_id:
                raise RuntimeError("failed to seed the disposable QA room")

            material = store.add_material(room_id, {
                "title": "Frozen local readiness evidence",
                "kind": "note",
                "content": (
                    "The first prototype is intentionally bounded to two people "
                    "and must remain reversible."
                ),
            })
            material_id = str((material or {}).get("id") or "")
            if not material_id:
                raise RuntimeError("failed to seed local QA evidence")

            draft = store.create_artifact(
                room_id,
                title="P26 exact confirmed readiness review",
                content=_fixture_content(material_id),
                created_by="isolated_project_readiness_qa",
            )
            if not draft:
                raise RuntimeError("failed to seed the disposable QA artifact")
            confirmed = store.confirm_artifact(
                room_id,
                str(draft["id"]),
                expected_version=int(draft["version"]),
                confirmed_by="isolated_project_readiness_qa",
            )
            if not confirmed or confirmed.get("status") != "CONFIRMED":
                raise RuntimeError("failed to confirm the disposable QA artifact")
            if (
                (confirmed.get("evidence_review") or {}).get("confirmation_ready")
                is not True
            ):
                raise RuntimeError("the exact artifact evidence review is not ready")
            plugin_context = confirmed.get("plugin_registry_context") or {}
            if (
                plugin_context.get("status") != "ready"
                or plugin_context.get("integrity_ok") is not True
                or plugin_context.get("runtime_available") is not True
                or (plugin_context.get("snapshot") or {}).get("version")
                != "plugin_registry_snapshot_v2"
            ):
                raise RuntimeError("the exact artifact registry v2 context is not ready")
            _assert_frozen_readiness_binding(plugin_context)

            artifact_id = str(confirmed["id"])
            artifact_version = int(confirmed["version"])
            initial_projection = ProjectReadinessService(store).inspect(
                room_id,
                artifact_id,
                artifact_version,
            )
            _assert_projection_is_qa_ready(initial_projection)

            # Make the isolated room deterministic on the root page even though a
            # fresh StudioStore also contains standard sample rooms.
            original_bootstrap = store.bootstrap

            def qa_bootstrap(target_room_id: str = "") -> dict[str, Any]:
                return original_bootstrap(target_room_id or room_id)

            store.bootstrap = qa_bootstrap  # type: ignore[method-assign]

            forbidden_providers = ForbiddenProviders()
            forbidden_market = ForbiddenReadSurface("market")
            forbidden_market_readiness = ForbiddenReadSurface("market readiness")
            http_server.STORE = store
            http_server.PROVIDERS = forbidden_providers
            http_server.STORAGE_MARKET = forbidden_market
            http_server.STORAGE_READINESS = forbidden_market_readiness

            qa_lock = threading.Lock()
            qa_state: dict[str, Any] = {
                "temporary_database": True,
                "database_path_inside_temp": True,
                "formal_database_used": False,
                "formal_port_8770_used": False,
                "futu_or_opend_connected": False,
                "room_id": room_id,
                "artifact_id": artifact_id,
                "artifact_version": artifact_version,
                "project_readiness_path": (
                    f"/api/rooms/{room_id}/artifacts/{artifact_id}/versions/"
                    f"{artifact_version}/project-readiness"
                ),
                "readiness_http_requests": 0,
                "readiness_service_attempts": 0,
                "readiness_service_successes": 0,
                "business_write_attempts": 0,
                "fixture_seed_writes_completed_before_server_start": True,
            }

            original_inspect = ProjectReadinessService.inspect

            def audited_inspect(
                service: ProjectReadinessService,
                target_room_id: str,
                target_artifact_id: str,
                target_artifact_version: int,
            ) -> dict[str, Any]:
                with qa_lock:
                    qa_state["readiness_service_attempts"] += 1
                projection = original_inspect(
                    service,
                    target_room_id,
                    target_artifact_id,
                    target_artifact_version,
                )
                _assert_projection_is_qa_ready(projection)
                with qa_lock:
                    qa_state["readiness_service_successes"] += 1
                return projection

            class QaRequestHandler(http_server.StudioRequestHandler):
                def do_GET(self) -> None:
                    path = urlparse(self.path).path
                    if path == "/__qa/status":
                        if not self._guard_request():
                            return
                        with qa_lock, network_lock:
                            counters = {
                                "provider_calls": forbidden_providers.call_attempts,
                                "market_reads": (
                                    forbidden_market.access_attempts
                                    + forbidden_market_readiness.access_attempts
                                ),
                                "business_writes": qa_state[
                                    "business_write_attempts"
                                ],
                                "outbound_connect_attempts": network_state[
                                    "outbound_connect_attempts"
                                ],
                            }
                            status = {**qa_state, **counters}
                        status["zero_external_and_business_use_verified"] = all(
                            value == 0 for value in counters.values()
                        )
                        status["projection_zero_budget_verified"] = all(
                            initial_projection.get(field) == 0
                            for field in ZERO_BUDGET_FIELDS
                        )
                        self._send_json({"ok": True, **status})
                        return
                    if path == qa_state["project_readiness_path"]:
                        with qa_lock:
                            qa_state["readiness_http_requests"] += 1
                    super().do_GET()

                def _reject_business_write(self) -> None:
                    if not self._guard_request(mutating=True):
                        return
                    with qa_lock:
                        qa_state["business_write_attempts"] += 1
                    self._send_json(
                        {
                            "ok": False,
                            "error": "isolated project-readiness QA is read-only",
                            "code": "ISOLATED_QA_BUSINESS_WRITES_DISABLED",
                        },
                        HTTPStatus.METHOD_NOT_ALLOWED,
                    )

                def do_POST(self) -> None:
                    self._reject_business_write()

                def do_PATCH(self) -> None:
                    self._reject_business_write()

                def do_DELETE(self) -> None:
                    self._reject_business_write()

                def do_PUT(self) -> None:
                    self._reject_business_write()

            with patch.object(
                ProjectReadinessService,
                "inspect",
                new=audited_inspect,
            ):
                server = ThreadingHTTPServer(("127.0.0.1", 0), QaRequestHandler)
                server.daemon_threads = True
                if server.server_port == 8770:
                    server.server_close()
                    raise RuntimeError(
                        "ephemeral QA server must never use formal port 8770"
                    )
                qa_state["port"] = server.server_port
                url = f"http://127.0.0.1:{server.server_port}/"
                print(json.dumps({
                    "ok": True,
                    "url": url,
                    "qa_status_url": f"{url}__qa/status",
                    "room_id": room_id,
                    "artifact_id": artifact_id,
                    "artifact_version": artifact_version,
                    "formal_port_8770_used": False,
                    "temporary_database": True,
                }, ensure_ascii=False))
                print(
                    "Open the confirmed artifact to inspect project readiness. "
                    "Press Ctrl+C to stop and delete the temporary database."
                )
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    server.server_close()

            if forbidden_providers.call_attempts:
                raise RuntimeError("a Provider dependency was accessed during QA")
            if forbidden_market.access_attempts:
                raise RuntimeError("a market dependency was accessed during QA")
            if forbidden_market_readiness.access_attempts:
                raise RuntimeError("market readiness was accessed during QA")
            if qa_state["business_write_attempts"]:
                raise RuntimeError("a post-start business write was attempted during QA")
            if network_state["outbound_connect_attempts"]:
                raise RuntimeError("outbound network access was attempted during QA")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
