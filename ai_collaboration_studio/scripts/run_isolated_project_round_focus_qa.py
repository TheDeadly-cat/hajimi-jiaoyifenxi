from __future__ import annotations

"""Serve disposable P27 project-round-focus browser fixtures.

This is a test-only harness, not an application launcher.  Each invocation
creates one isolated scenario in a system ``TemporaryDirectory`` and binds an
ephemeral loopback port.  Select ``exact``, ``bootstrap``, or
``inactive-record`` with ``--scenario``; no scenario ever touches the formal
runtime, SQLite database, Provider ledger, market data, Futu/OpenD, or port
8770.

Fixture initialization is completed before the server starts.  Afterwards all
business-write methods fail closed.  The only POST exception is the read-only
round-launch-plan projection needed to display (but not confirm) the v5 launch
dialog.  ``/__qa/status`` exposes zero-use counters.
"""

import argparse
import json
import os
import re
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
SCENARIOS = ("exact", "bootstrap", "inactive-record")
PACK_IDS = [
    "structured_project_research",
    "project_readiness_review",
    "project_round_focus",
]
KEY_ENV_NAMES = (
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ARK_API_KEY",
    "GLM_API_KEY",
    "ZHIPUAI_API_KEY",
)
FOCUS_TARGETS = {
    "blocker": ["critical_review"],
    "evidence": ["evidence_review"],
    "structural": ["evidence_review", "decision_synthesis"],
}
ZERO_BUDGET_FIELDS = (
    "provider_calls_performed",
    "market_reads_performed",
    "adapter_business_writes_performed",
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
    "member_assignment_produced": False,
    "workflow_mutation_performed": False,
    "user_final_decision_required": True,
    "host_lineage_write_required": True,
}


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one isolated P27 browser-QA scenario.",
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIOS,
        default="exact",
        help=(
            "exact shows the confirmed-artifact card and v5 dialog; bootstrap "
            "shows the no-artifact card and v5 dialog; inactive-record shows a "
            "frozen paused-round record after its contribution is disabled"
        ),
    )
    return parser.parse_args()


def _configure_isolation(temp_root: Path, scenario: str) -> Path:
    database_path = temp_root / f"project-round-focus-{scenario}-qa.sqlite3"
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    os.environ["AI_STUDIO_RUNTIME_DIR"] = str(temp_root)
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(database_path)
    os.environ["AI_STUDIO_HOST"] = "127.0.0.1"
    os.environ["AI_STUDIO_PORT"] = "0"
    os.environ["AI_STUDIO_DEFAULT_PROVIDER"] = "deepseek"
    os.environ["AI_STUDIO_DISABLED_PROVIDERS"] = "openai"
    os.environ["FUTU_HOST"] = "127.0.0.1"
    os.environ["FUTU_PORT"] = "1"
    os.environ["SEC_USER_AGENT"] = ""
    for name in KEY_ENV_NAMES:
        # Assign empty values without reading or echoing inherited credentials.
        os.environ[name] = ""
    if os.environ["AI_STUDIO_PORT"] == "8770":
        raise RuntimeError("isolated QA must never configure formal port 8770")
    return database_path


def _reviewed_material(
    material_id: str,
    *,
    role: str = "support",
    status: str = "source_checked",
    note: str = "Reviewed against the frozen local P27 QA material.",
) -> dict[str, str]:
    return {
        "type": "material",
        "id": material_id,
        "evidence_role": role,
        "verification_status": status,
        "review_note": note,
    }


def _artifact_content(material_id: str) -> dict[str, Any]:
    support = _reviewed_material(material_id)
    disputed = _reviewed_material(
        material_id,
        role="counter",
        status="disputed",
        note=(
            "The frozen note supports the capacity concern but does not prove "
            "that the proposed mitigation is sufficient."
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


def _authorization(preview: dict[str, Any]) -> dict[str, Any]:
    binding = preview.get("artifact_binding") or {}
    if binding.get("status") == "none":
        authorization_binding: dict[str, Any] = {"status": "none"}
    else:
        authorization_binding = {
            "status": "exact",
            "artifact_id": str(binding.get("artifact_id") or ""),
            "artifact_version": int(binding.get("artifact_version") or 0),
        }
    return {
        "version": "project_round_focus_authorization_v1",
        "artifact_binding": authorization_binding,
        "preview_sha256": str(preview.get("preview_sha256") or ""),
        "user_confirmed": True,
    }


def _round_context_authorization_set(
    authorization: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": "round_context_authorization_set_v1",
        "contexts": [{
            "version": "round_context_authorization_entry_v1",
            "owner_pack_id": "project_round_focus",
            "port_id": "core.round.context/v1",
            "request": dict(authorization),
        }],
    }


def _assert_focus_projection(
    value: dict[str, Any],
    *,
    scenario: str,
    room_id: str,
    round_id: str = "",
) -> None:
    is_record = bool(round_id)
    expected_version = (
        "project_round_focus_record_v1"
        if is_record
        else "project_round_focus_preview_v1"
    )
    if value.get("version") != expected_version:
        raise RuntimeError("unexpected project-round-focus projection version")
    if value.get("room_id") != room_id:
        raise RuntimeError("project-round-focus room binding drifted")
    if is_record:
        if value.get("round_id") != round_id:
            raise RuntimeError("project-round-focus round binding drifted")
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z",
            str(value.get("frozen_at") or ""),
        ):
            raise RuntimeError("project-round-focus frozen_at is not UTC ISO milliseconds")
        if value.get("runtime_available") is not False:
            raise RuntimeError("inactive frozen record must expose runtime_available=false")
    if value.get("integrity_ok") is not True or value.get("metrics_visible") is not True:
        raise RuntimeError("project-round-focus fixture integrity is not verified")
    if any(value.get(field) != 0 for field in ZERO_BUDGET_FIELDS):
        raise RuntimeError("project-round-focus fixture violated a zero-use budget")
    if any(
        value.get(field) != expected
        for field, expected in READONLY_SAFETY_FIELDS.items()
    ):
        raise RuntimeError("project-round-focus fixture violated its safety contract")
    for field in (
        "plugin_registry_snapshot_sha256",
        "input_seal_sha256",
        "preview_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(value.get(field) or "")):
            raise RuntimeError(f"project-round-focus {field} is not sealed")
    resolution = value.get("resolution") or {}
    contribution = resolution.get("contribution") or {}
    adapter = resolution.get("adapter") or {}
    port = resolution.get("port") or {}
    if (
        resolution.get("version") != "project_round_focus_resolution_v1"
        or contribution.get("contribution_id")
        != "project_round_focus.room_inspector/v1"
        or contribution.get("component_key") != "project_round_focus"
        or adapter.get("adapter_id") != "project_round_focus"
        or adapter.get("contract_version") != "domain_adapter_contract_v2"
        or port.get("port_id") != "core.round.context/v1"
        or port.get("provider_call_budget") != 0
        or port.get("market_read_budget") != 0
        or port.get("business_write_budget") != 0
        or port.get("failure_policy") != "fail_closed"
    ):
        raise RuntimeError("project-round-focus exact resolution drifted")
    binding = value.get("artifact_binding") or {}
    counts = value.get("counts") or {}
    focus_items = value.get("focus_items") or []
    if scenario == "bootstrap":
        if (
            binding.get("status") != "none"
            or value.get("state") != "bootstrap"
            or focus_items != []
            or any(int(counts.get(field) or 0) != 0 for field in (
                "structural_gap_count",
                "blocker_count",
                "evidence_gap_count",
                "focus_item_count",
            ))
        ):
            raise RuntimeError("bootstrap focus fabricated an artifact gap")
        return
    if binding.get("status") != "exact":
        raise RuntimeError("exact focus fixture did not bind a confirmed artifact")
    categories = {str(item.get("category") or "") for item in focus_items}
    if categories != set(FOCUS_TARGETS):
        raise RuntimeError("exact focus fixture must expose all three gap categories")
    for sequence_no, item in enumerate(focus_items, start=1):
        category = str(item.get("category") or "")
        if (
            item.get("sequence_no") != sequence_no
            or item.get("target_capabilities") != FOCUS_TARGETS.get(category)
        ):
            raise RuntimeError("project-round-focus item ordering or targets drifted")


def _assert_v5_plan(
    plan: dict[str, Any],
    authorization_set: dict[str, Any],
    *,
    registry_sha256: str,
) -> None:
    if plan.get("version") != "round_launch_plan_v5":
        raise RuntimeError("focus fixture did not build round_launch_plan_v5")
    if "project_round_focus_authorization" in plan:
        raise RuntimeError("v5 plan exposed the retired project-only authorization")
    if plan.get("round_context_authorizations") != authorization_set:
        raise RuntimeError("v5 plan changed the exact round-context authorization")
    if (plan.get("room") or {}).get("plugin_registry_snapshot_sha256") != registry_sha256:
        raise RuntimeError("v5 plan registry binding drifted")
    if plan.get("ready_for_authorization") is not True:
        raise RuntimeError(f"v5 launch plan is blocked: {plan.get('blockers')!r}")
    safety = plan.get("safety") or {}
    if (
        safety.get("execution_capability") != "none"
        or safety.get("live_trading_allowed") is not False
        or safety.get("user_confirmation_required") is not True
    ):
        raise RuntimeError("v5 launch plan safety fields drifted")


class FakeProviderRegistry:
    """Expose local route metadata while failing every callable Provider path."""

    disabled_provider_ids = frozenset({"openai"})

    def __init__(self) -> None:
        self.call_attempts = 0
        self.status_reads = 0

    def status(self) -> list[dict[str, Any]]:
        self.status_reads += 1
        return [{
            "id": "deepseek",
            "name": "Offline QA DeepSeek",
            "configured": True,
            "policy_disabled": False,
            "model": "offline-project-focus-model",
            "output_capabilities": {
                "version": "provider_output_capabilities_v1",
                "modes": ["json_object"],
                "preferred_mode": "json_object",
                "declared": True,
            },
        }, {
            "id": "openai",
            "name": "OpenAI (disabled in isolated QA)",
            "configured": False,
            "policy_disabled": True,
            "model": "",
            "output_capabilities": {
                "version": "provider_output_capabilities_v1",
                "modes": ["prompt_json"],
                "preferred_mode": "prompt_json",
                "declared": True,
            },
        }]

    def __getattr__(self, name: str) -> Any:
        self.call_attempts += 1
        raise AssertionError(f"Provider dependency must not be used: {name}")


class ForbiddenReadSurface:
    """Fail closed if a market, Futu, or readiness surface is accessed."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.access_attempts = 0

    def __getattr__(self, name: str) -> Any:
        self.access_attempts += 1
        raise AssertionError(f"{self.label} dependency must not be used: {name}")


def _disable_focus_contribution(store: Any) -> None:
    from backend.plugin_lifecycle import (
        PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION,
        PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION,
    )

    lifecycle = store.plugin_lifecycle_view(include_history=True)
    states = [
        row
        for row in lifecycle.get("targets") or []
        if row.get("kind") == "ui_contribution"
        and row.get("id") == "project_round_focus.room_inspector/v1"
    ]
    if len(states) != 1:
        raise RuntimeError("P27 lifecycle target is not unique")
    state = states[0]
    target = {
        "kind": state["kind"],
        "id": state["id"],
        "version": state["version"],
        "sha256": state["target_sha256"],
    }
    preview = store.preview_plugin_lifecycle({
        "version": PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION,
        "target": target,
        "action": "disable",
        "expected_head_sequence": state["head_sequence"],
        "expected_head_sha256": state["head_sha256"],
        "replacement": None,
    })
    transition, created = store.transition_plugin_lifecycle({
        "version": PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION,
        "client_request_id": "p27-isolated-inactive-record-disable-v1",
        "target": preview["target"],
        "action": preview["action"],
        "expected_head_sequence": preview["expected_head_sequence"],
        "expected_head_sha256": preview["expected_head_sha256"],
        "replacement": preview.get("replacement"),
        "impact_preview_sha256": preview["preview_sha256"],
        "reason": "P27 isolated browser QA preserves a frozen record after disable",
        "user_confirmed_history_preserved": True,
        "user_confirmed_no_automatic_migration": True,
    })
    if not created or (transition.get("target") or {}).get("runtime_state") != "disabled":
        raise RuntimeError("failed to disable the P27 contribution in the fixture")


def main() -> int:
    scenario = str(_arguments().scenario)
    with tempfile.TemporaryDirectory(
        prefix=f"ai-studio-p27-{scenario}-qa-",
    ) as raw_temp:
        temp_root = Path(raw_temp).resolve()
        database_path = _configure_isolation(temp_root, scenario).resolve()
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

        # Backend imports remain below the isolation setup.  The connect guard
        # covers both import time and the full QA-server lifetime.
        with patch.object(socket.socket, "connect", new=forbid_outbound_connect):
            from backend import http_server
            from backend.project_round_focus import (
                ProjectRoundFocusService,
                normalize_project_round_focus_authorization,
                validate_project_round_focus_preview,
            )
            from backend.round_launch_plan import RoundLaunchPlanService
            from backend.store import StudioStore

            if not http_server.FRONTEND_DIST.joinpath("index.html").is_file():
                raise RuntimeError(
                    "frontend/dist is missing; build the frontend before browser QA"
                )

            store = StudioStore(database_path)
            room_objective = (
                "Continue the current project objective without inventing artifact gaps."
                if scenario == "bootstrap"
                else "Repair the exact confirmed project's structural, evidence, and blocker gaps."
            )
            room_snapshot = store.create_room(
                f"P27 isolated project round focus QA ({scenario})",
                room_objective,
                domain="project_research",
                category="project_research",
                template_id="open_collaboration",
                capability_pack_ids=PACK_IDS,
            )
            room = room_snapshot.get("room") or {}
            room_id = str(room.get("id") or "")
            if not room_id:
                raise RuntimeError("failed to seed the disposable P27 room")

            artifact_id = ""
            artifact_version = 0
            if scenario != "bootstrap":
                material = store.add_material(room_id, {
                    "title": "Frozen local P27 focus evidence",
                    "kind": "note",
                    "content": (
                        "The prototype is bounded to two people and remains reversible."
                    ),
                })
                material_id = str((material or {}).get("id") or "")
                if not material_id:
                    raise RuntimeError("failed to seed local P27 evidence")
                draft = store.create_artifact(
                    room_id,
                    title="P27 exact confirmed project focus",
                    content=_artifact_content(material_id),
                    created_by="isolated_project_round_focus_qa",
                )
                if not draft:
                    raise RuntimeError("failed to seed the disposable P27 artifact")
                confirmed = store.confirm_artifact(
                    room_id,
                    str(draft["id"]),
                    expected_version=int(draft["version"]),
                    confirmed_by="isolated_project_round_focus_qa",
                )
                if not confirmed or confirmed.get("status") != "CONFIRMED":
                    raise RuntimeError("failed to confirm the disposable P27 artifact")
                artifact_id = str(confirmed["id"])
                artifact_version = int(confirmed["version"])

            fake_providers = FakeProviderRegistry()
            forbidden_market = ForbiddenReadSurface("market")
            forbidden_market_readiness = ForbiddenReadSurface("market readiness")
            focus_service = ProjectRoundFocusService(store)
            initial_preview = focus_service.preview(room_id)
            validate_project_round_focus_preview(initial_preview)
            _assert_focus_projection(
                initial_preview,
                scenario="bootstrap" if scenario == "bootstrap" else "exact",
                room_id=room_id,
            )
            authorization = normalize_project_round_focus_authorization(
                _authorization(initial_preview)
            )
            authorization_set = _round_context_authorization_set(authorization)
            current_room = (store.room_snapshot(room_id) or {}).get("room") or {}
            registry_sha256 = str(
                current_room.get("plugin_registry_snapshot_sha256") or ""
            )
            initial_plan = RoundLaunchPlanService(store, fake_providers).build(
                room_id,
                str(initial_preview.get("suggested_objective") or room_objective),
                {"openai"},
                round_context_authorizations=authorization_set,
            )
            _assert_v5_plan(
                initial_plan,
                authorization_set,
                registry_sha256=registry_sha256,
            )
            if fake_providers.call_attempts:
                raise RuntimeError("fixture setup attempted a Provider call")

            round_id = ""
            initial_record: dict[str, Any] | None = None
            if scenario == "inactive-record":
                prepared = focus_service.prepare_authorized(room_id, authorization)
                lifecycle = current_room.get("plugin_lifecycle_current") or {}
                round_row = store.create_formal_round(
                    room_id,
                    str(initial_preview["suggested_objective"]),
                    expected_settings_version=int(current_room["settings_version"]),
                    expected_plugin_registry_snapshot_sha256=registry_sha256,
                    expected_plugin_lifecycle_head_set_sha256=str(
                        lifecycle.get("current_head_set_sha256") or ""
                    ),
                    project_round_focus_prepared=prepared,
                )
                round_id = str(round_row.get("id") or "")
                if not round_id:
                    raise RuntimeError("failed to seed the frozen P27 round")
                store.complete_round(round_id, "PAUSED")
                _disable_focus_contribution(store)
                initial_record = store.get_round_project_focus(room_id, round_id)
                if not isinstance(initial_record, dict):
                    raise RuntimeError("frozen P27 record is unavailable")
                _assert_focus_projection(
                    initial_record,
                    scenario="inactive-record",
                    room_id=room_id,
                    round_id=round_id,
                )

            original_bootstrap = store.bootstrap

            def qa_bootstrap(target_room_id: str = "") -> dict[str, Any]:
                return original_bootstrap(target_room_id or room_id)

            store.bootstrap = qa_bootstrap  # type: ignore[method-assign]
            http_server.STORE = store
            http_server.PROVIDERS = fake_providers
            http_server.ORCHESTRATOR.providers = fake_providers
            http_server.STORAGE_MARKET = forbidden_market
            http_server.STORAGE_READINESS = forbidden_market_readiness

            preview_path = f"/api/rooms/{room_id}/project-round-focus"
            record_path = (
                f"/api/rooms/{room_id}/rounds/{round_id}/project-round-focus"
                if round_id
                else ""
            )
            plan_path = f"/api/rooms/{room_id}/round-launch-plan"
            qa_lock = threading.Lock()
            qa_state: dict[str, Any] = {
                "scenario": scenario,
                "temporary_database": True,
                "database_path_inside_temp": True,
                "formal_database_used": False,
                "formal_port_8770_used": False,
                "futu_or_opend_connected": False,
                "room_id": room_id,
                "artifact_id": artifact_id,
                "artifact_version": artifact_version,
                "round_id": round_id,
                "focus_preview_path": preview_path,
                "focus_record_path": record_path,
                "round_launch_plan_path": plan_path,
                "focus_preview_http_requests": 0,
                "focus_record_http_requests": 0,
                "round_launch_plan_requests": 0,
                "business_write_attempts": 0,
                "fixture_seed_writes_completed_before_server_start": True,
                "exact_focus_verified": scenario in {"exact", "inactive-record"},
                "bootstrap_none_verified": scenario == "bootstrap",
                "inactive_frozen_record_verified": scenario == "inactive-record",
                "v5_round_context_plan_verified": True,
            }

            class QaRequestHandler(http_server.StudioRequestHandler):
                def do_GET(self) -> None:
                    path = urlparse(self.path).path
                    if path == "/__qa/status":
                        if not self._guard_request():
                            return
                        with qa_lock, network_lock:
                            counters = {
                                "provider_calls": fake_providers.call_attempts,
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
                        status["focus_zero_budget_verified"] = all(
                            initial_preview.get(field) == 0
                            for field in ZERO_BUDGET_FIELDS
                        )
                        self._send_json({"ok": True, **status})
                        return
                    if path == preview_path:
                        with qa_lock:
                            qa_state["focus_preview_http_requests"] += 1
                    if record_path and path == record_path:
                        with qa_lock:
                            qa_state["focus_record_http_requests"] += 1
                    super().do_GET()

                def _reject_business_write(self) -> None:
                    if not self._guard_request(mutating=True):
                        return
                    with qa_lock:
                        qa_state["business_write_attempts"] += 1
                    self._send_json(
                        {
                            "ok": False,
                            "error": "isolated P27 QA disables post-start business writes",
                            "code": "ISOLATED_QA_BUSINESS_WRITES_DISABLED",
                        },
                        HTTPStatus.METHOD_NOT_ALLOWED,
                    )

                def do_POST(self) -> None:
                    path = urlparse(self.path).path
                    if path == plan_path:
                        with qa_lock:
                            qa_state["round_launch_plan_requests"] += 1
                        super().do_POST()
                        return
                    self._reject_business_write()

                def do_PATCH(self) -> None:
                    self._reject_business_write()

                def do_DELETE(self) -> None:
                    self._reject_business_write()

                def do_PUT(self) -> None:
                    self._reject_business_write()

            server = ThreadingHTTPServer(("127.0.0.1", 0), QaRequestHandler)
            server.daemon_threads = True
            if server.server_port == 8770:
                server.server_close()
                raise RuntimeError("ephemeral QA server must never use formal port 8770")
            qa_state["port"] = server.server_port
            url = f"http://127.0.0.1:{server.server_port}/"
            print(
                json.dumps({
                    "ok": True,
                    "scenario": scenario,
                    "url": url,
                    "qa_status_url": f"{url}__qa/status",
                    "room_id": room_id,
                    "artifact_id": artifact_id,
                    "artifact_version": artifact_version,
                    "round_id": round_id,
                    "formal_port_8770_used": False,
                    "temporary_database": True,
                }, ensure_ascii=False),
                flush=True,
            )
            if scenario == "inactive-record":
                print(
                    "Open room information and verify the paused round's frozen focus "
                    "record remains visible and read-only while runtime availability is off."
                )
            else:
                print(
                    "Open room information, verify the focus card, choose Fill next-round "
                    "objective, then Start one round to inspect the v5 confirmation dialog. "
                    "Do not confirm the dialog."
                )
            print("Press Ctrl+C to stop and delete the temporary database.")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()

            if fake_providers.call_attempts:
                raise RuntimeError("a Provider dependency was accessed during QA")
            if forbidden_market.access_attempts:
                raise RuntimeError("a market dependency was accessed during QA")
            if forbidden_market_readiness.access_attempts:
                raise RuntimeError("market readiness was accessed during QA")
            if qa_state["business_write_attempts"]:
                raise RuntimeError("a post-start business write was attempted during QA")
            if network_state["outbound_connect_attempts"]:
                raise RuntimeError("outbound network access was attempted during QA")
            if initial_record is not None:
                _assert_focus_projection(
                    initial_record,
                    scenario="inactive-record",
                    room_id=room_id,
                    round_id=round_id,
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
