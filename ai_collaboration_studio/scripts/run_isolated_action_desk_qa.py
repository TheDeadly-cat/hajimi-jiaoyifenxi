from __future__ import annotations

"""Serve a disposable Action Desk browser-QA fixture.

This is a test-only harness, not an application launcher.  It creates a generic
room and an explicit SQLite database inside one system ``TemporaryDirectory``,
serves the current ``frontend/dist`` from an ephemeral loopback port, and hard
rejects port 8770.  Fixture writes finish before the HTTP server starts.

After startup, only the exact Action Desk transition route may perform local
business writes.  Every other POST/PATCH/PUT/DELETE request fails closed.
Provider calls, market/Futu reads, and outbound socket connections also fail
closed.  ``/__qa/status`` reports the allowed event/head/anchor changes and
verifies that every non-Action-Desk table remains byte-semantically unchanged.
"""

import hashlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import patch
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEY_ENV_NAMES = (
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "ARK_API_KEY",
    "GLM_API_KEY",
    "ZHIPUAI_API_KEY",
)
ACTION_STORAGE_TABLES = frozenset({
    "artifact_action_anchor_heads",
    "artifact_action_anchors",
    "artifact_action_events",
    "artifact_action_heads",
})
FIXED_ACTION_DESK_SAFETY = {
    "execution_capability": "none",
    "external_write": False,
    "can_autonomously_decide": False,
    "can_replace_user_decision": False,
    "user_final_decision_required": True,
}


def _configure_isolation(temp_root: Path) -> Path:
    database_path = temp_root / "action-desk-browser-qa.sqlite3"
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
        # Never read or echo an inherited credential; replace it before backend imports.
        os.environ[name] = ""
    if os.environ["AI_STUDIO_PORT"] == "8770":
        raise RuntimeError("isolated Action Desk QA must never configure port 8770")
    return database_path


def _reviewed_material(material_id: str) -> dict[str, str]:
    return {
        "type": "material",
        "id": material_id,
        "evidence_role": "support",
        "verification_status": "source_checked",
        "review_note": "Checked against the disposable local QA note.",
    }


def _artifact_content(material_id: str) -> dict[str, Any]:
    evidence = [_reviewed_material(material_id)]
    return {
        "summary": (
            "Two bounded follow-through actions remain subject to explicit user adoption."
        ),
        "summary_evidence": evidence,
        "requirements": [],
        "risks": [],
        "disagreements": [],
        "unknowns": [],
        "actions": [{
            "id": "action_prepare_acceptance_note",
            "text": "Prepare the isolated acceptance note for user review.",
            "owner": "Original owner",
            "due": "2026-08-20",
            "state": "open",
            "evidence": evidence,
        }, {
            "id": "action_schedule_user_review",
            "text": "Schedule a bounded user review of the acceptance note.",
            "owner": "",
            "due": "",
            "state": "open",
            "evidence": evidence,
        }],
    }


def _transition_request(
    source: dict[str, Any],
    *,
    request_id: str,
    transition: str,
    revision: int,
    owner: str,
    due: str,
    state: str,
    note: str,
) -> dict[str, Any]:
    return {
        "version": "artifact_action_transition_v1",
        "client_request_id": request_id,
        "artifact_id": str(source.get("artifact_id") or ""),
        "artifact_version": int(source.get("artifact_version") or 0),
        "action_id": str(source.get("action_id") or ""),
        "expected_action_snapshot_sha256": str(
            source.get("action_snapshot_sha256") or ""
        ),
        "expected_revision": revision,
        "transition": transition,
        "patch": {
            "owner": owner,
            "due": due,
            "state": state,
            "note": note,
        },
        "user_confirmed": True,
    }


def _assert_action_desk(
    desk: dict[str, Any],
    *,
    room_id: str,
    artifact_id: str,
    artifact_version: int,
) -> None:
    if (
        desk.get("version") != "artifact_action_desk_v1"
        or desk.get("room_id") != room_id
        or desk.get("integrity_ok") is not True
    ):
        raise RuntimeError("Action Desk fixture failed its top-level integrity contract")
    if any(desk.get(field) != expected for field, expected in FIXED_ACTION_DESK_SAFETY.items()):
        raise RuntimeError("Action Desk fixture drifted from its fixed safety boundary")
    candidates = desk.get("candidates") or []
    items = desk.get("items") or []
    if len(candidates) != 1 or len(items) != 1:
        raise RuntimeError("Action Desk fixture must expose one candidate and one item")
    candidate = candidates[0]
    item = items[0]
    if (
        candidate.get("version") != "artifact_action_candidate_v1"
        or candidate.get("artifact_id") != artifact_id
        or candidate.get("artifact_version") != artifact_version
        or candidate.get("action_id") != "action_schedule_user_review"
        or candidate.get("source_status") != "confirmed_exact"
    ):
        raise RuntimeError("Action Desk candidate lost its exact artifact source")
    if (
        item.get("version") != "artifact_action_item_v1"
        or item.get("artifact_id") != artifact_id
        or item.get("artifact_version") != artifact_version
        or item.get("action_id") != "action_prepare_acceptance_note"
        or item.get("revision") != 1
        or item.get("integrity_ok") is not True
        or item.get("source_status") != "confirmed_exact"
        or item.get("source_current") is not True
        or item.get("current_artifact_version") != artifact_version
    ):
        raise RuntimeError("pre-adopted Action Desk item lost its exact source lineage")
    counts = desk.get("counts") or {}
    if (
        counts.get("candidate_count") != 1
        or counts.get("item_count") != 1
        or counts.get("in_progress_count") != 1
    ):
        raise RuntimeError("Action Desk fixture counts are inconsistent")


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _json_storage_value(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return {"sqlite_blob_hex": bytes(value).hex()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return {"sqlite_value_repr": repr(value)}


def _database_snapshot_sha256(
    database_path: Path,
    *,
    excluded_tables: Iterable[str] = (),
) -> str:
    excluded = set(excluded_tables)
    payload: list[dict[str, Any]] = []
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute("BEGIN")
        table_names = [
            str(row[0])
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type='table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            ).fetchall()
            if str(row[0]) not in excluded
        ]
        for table_name in table_names:
            cursor = connection.execute(
                f"SELECT * FROM {_quote_identifier(table_name)}"
            )
            columns = [str(column[0]) for column in cursor.description or []]
            rows = [
                json.dumps(
                    [_json_storage_value(value) for value in row],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                for row in cursor.fetchall()
            ]
            rows.sort()
            payload.append({
                "table": table_name,
                "columns": columns,
                "rows": rows,
            })
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _action_storage_counts(database_path: Path) -> dict[str, int]:
    with closing(sqlite3.connect(database_path)) as connection, connection:
        return {
            "artifact_action_anchor_heads": int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifact_action_anchor_heads"
                ).fetchone()[0]
            ),
            "artifact_action_anchors": int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifact_action_anchors"
                ).fetchone()[0]
            ),
            "artifact_action_events": int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifact_action_events"
                ).fetchone()[0]
            ),
            "artifact_action_heads": int(
                connection.execute(
                    "SELECT COUNT(*) FROM artifact_action_heads"
                ).fetchone()[0]
            ),
        }


class FakeProviderRegistry:
    """Expose local display metadata while failing every callable Provider path."""

    disabled_provider_ids = frozenset({"openai"})

    def __init__(self) -> None:
        self.call_attempts = 0
        self.status_reads = 0

    def status(self) -> list[dict[str, Any]]:
        self.status_reads += 1
        return [{
            "id": "deepseek",
            "name": "Offline QA Provider",
            "configured": True,
            "policy_disabled": False,
            "model": "offline-action-desk-model",
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
    """Fail closed if market, Futu, or storage-readiness code is reached."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.access_attempts = 0

    def __getattr__(self, name: str) -> Any:
        self.access_attempts += 1
        raise AssertionError(f"{self.label} dependency must not be used: {name}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="ai-studio-action-desk-qa-") as raw_temp:
        temp_root = Path(raw_temp).resolve()
        database_path = _configure_isolation(temp_root).resolve()
        if database_path.parent != temp_root:
            raise RuntimeError("Action Desk QA database escaped its temporary runtime")
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

        # All backend imports stay below environment/key isolation.  The socket
        # guard remains active for the complete server lifetime.
        with patch.object(socket.socket, "connect", new=forbid_outbound_connect):
            from backend import http_server
            from backend.action_desk import ACTION_TRANSITION_REQUEST_VERSION
            from backend.store import StudioStore

            if ACTION_TRANSITION_REQUEST_VERSION != "artifact_action_transition_v1":
                raise RuntimeError("Action Desk transition contract version drifted")
            if not http_server.FRONTEND_DIST.joinpath("index.html").is_file():
                raise RuntimeError(
                    "frontend/dist is missing; build the frontend before browser QA"
                )

            store = StudioStore(database_path)
            room_snapshot = store.create_room(
                "Isolated Action Desk QA",
                "Turn confirmed conclusions into explicit user-owned follow-through.",
                domain="general",
                category="general",
                template_id="open_collaboration",
                capability_pack_ids=[],
            )
            room = room_snapshot.get("room") or {}
            room_id = str(room.get("id") or "")
            if not room_id:
                raise RuntimeError("failed to seed the disposable Action Desk room")

            material = store.add_material(room_id, {
                "title": "Local Action Desk QA evidence",
                "kind": "note",
                "content": "A bounded local source used only by this disposable fixture.",
            })
            material_id = str((material or {}).get("id") or "")
            if not material_id:
                raise RuntimeError("failed to seed local Action Desk evidence")
            draft = store.create_artifact(
                room_id,
                title="Confirmed Action Desk QA plan",
                content=_artifact_content(material_id),
                created_by="isolated_action_desk_qa",
            )
            confirmed = store.confirm_artifact(
                room_id,
                str(draft.get("id") or ""),
                expected_version=int(draft.get("version") or 0),
                confirmed_by="isolated_action_desk_qa",
            )
            if (
                not confirmed
                or confirmed.get("status") != "CONFIRMED"
                or int(confirmed.get("version") or 0) <= 0
            ):
                raise RuntimeError("failed to freeze an exact confirmed artifact version")
            artifact_id = str(confirmed.get("id") or "")
            artifact_version = int(confirmed.get("version") or 0)

            initial_candidates = {
                str(candidate.get("action_id") or ""): candidate
                for candidate in (store.action_desk(room_id).get("candidates") or [])
            }
            adopted_source = initial_candidates.get("action_prepare_acceptance_note")
            if not isinstance(adopted_source, dict):
                raise RuntimeError("pre-adopt Action Desk candidate is unavailable")
            pre_adopted, pre_adopt_created = store.transition_artifact_action(
                room_id,
                _transition_request(
                    adopted_source,
                    request_id="isolated_action_desk_seed_adopt_v1",
                    transition="adopt",
                    revision=0,
                    owner="QA owner",
                    due="2026-08-22",
                    state="in_progress",
                    note="Explicitly adopted before the isolated server starts.",
                ),
            )
            if not pre_adopt_created or pre_adopted.get("revision") != 1:
                raise RuntimeError("failed to pre-adopt the local Action Desk item")

            overview_room_snapshot = store.create_room(
                "Second Action Desk QA room",
                "A second local room used to verify the read-only workspace overview.",
                domain="general",
                category="general",
                template_id="open_collaboration",
                capability_pack_ids=[],
            )
            overview_room = overview_room_snapshot.get("room") or {}
            overview_room_id = str(overview_room.get("id") or "")
            if not overview_room_id:
                raise RuntimeError("failed to seed the second Action Desk QA room")
            overview_material = store.add_material(overview_room_id, {
                "title": "Second local Action Desk QA evidence",
                "kind": "note",
                "content": "A second bounded local source for overview navigation QA.",
            })
            overview_material_id = str((overview_material or {}).get("id") or "")
            if not overview_material_id:
                raise RuntimeError("failed to seed second-room Action Desk evidence")
            overview_draft = store.create_artifact(
                overview_room_id,
                title="Second confirmed Action Desk QA plan",
                content=_artifact_content(overview_material_id),
                created_by="isolated_action_desk_qa",
            )
            overview_confirmed = store.confirm_artifact(
                overview_room_id,
                str(overview_draft.get("id") or ""),
                expected_version=int(overview_draft.get("version") or 0),
                confirmed_by="isolated_action_desk_qa",
            )
            overview_candidates = {
                str(candidate.get("action_id") or ""): candidate
                for candidate in (
                    store.action_desk(overview_room_id).get("candidates") or []
                )
            }
            overview_source = overview_candidates.get("action_prepare_acceptance_note")
            if (
                not isinstance(overview_confirmed, dict)
                or overview_confirmed.get("status") != "CONFIRMED"
                or not isinstance(overview_source, dict)
            ):
                raise RuntimeError("second-room Action Desk source is unavailable")
            overview_item, overview_item_created = store.transition_artifact_action(
                overview_room_id,
                _transition_request(
                    overview_source,
                    request_id="isolated_action_desk_overview_seed_v1",
                    transition="adopt",
                    revision=0,
                    owner="Second QA owner",
                    due="2026-09-01",
                    state="blocked",
                    note="A second-room item for read-only overview filtering.",
                ),
            )
            if not overview_item_created or overview_item.get("revision") != 1:
                raise RuntimeError("failed to pre-adopt the second-room Action Desk item")

            initial_desk = store.action_desk(room_id)
            _assert_action_desk(
                initial_desk,
                room_id=room_id,
                artifact_id=artifact_id,
                artifact_version=artifact_version,
            )
            baseline_action_counts = _action_storage_counts(database_path)
            if baseline_action_counts != {
                "artifact_action_anchor_heads": 2,
                "artifact_action_anchors": 2,
                "artifact_action_events": 2,
                "artifact_action_heads": 2,
            }:
                raise RuntimeError("unexpected pre-server Action Desk storage counts")
            baseline_non_action_sha256 = _database_snapshot_sha256(
                database_path,
                excluded_tables=ACTION_STORAGE_TABLES,
            )

            fake_providers = FakeProviderRegistry()
            forbidden_market = ForbiddenReadSurface("market/Futu")
            forbidden_market_readiness = ForbiddenReadSurface("market readiness")

            original_bootstrap = store.bootstrap

            def qa_bootstrap(target_room_id: str = "") -> dict[str, Any]:
                return original_bootstrap(target_room_id or room_id)

            store.bootstrap = qa_bootstrap  # type: ignore[method-assign]
            http_server.STORE = store
            http_server.PROVIDERS = fake_providers
            http_server.ORCHESTRATOR.providers = fake_providers
            http_server.STORAGE_MARKET = forbidden_market
            http_server.STORAGE_READINESS = forbidden_market_readiness

            desk_path = f"/api/rooms/{room_id}/action-desk"
            transition_path = f"{desk_path}/transitions"
            overview_path = "/api/action-desk/overview"
            qa_lock = threading.Lock()
            qa_state: dict[str, Any] = {
                "temporary_database": True,
                "database_path_inside_temp": True,
                "formal_database_used": False,
                "formal_port_8770_used": False,
                "futu_or_opend_connected": False,
                "fixture_seed_writes_completed_before_server_start": True,
                "room_id": room_id,
                "overview_room_id": overview_room_id,
                "artifact_id": artifact_id,
                "artifact_version": artifact_version,
                "action_desk_path": desk_path,
                "action_transition_path": transition_path,
                "action_overview_path": overview_path,
                "action_desk_http_reads": 0,
                "action_overview_http_reads": 0,
                "action_transition_http_requests": 0,
                "blocked_post_start_write_attempts": 0,
                "initial_candidate_count": 1,
                "initial_item_count": 1,
                "initial_action_event_count": baseline_action_counts[
                    "artifact_action_events"
                ],
                "initial_action_head_count": baseline_action_counts[
                    "artifact_action_heads"
                ],
                "initial_action_anchor_count": baseline_action_counts[
                    "artifact_action_anchors"
                ],
                "initial_action_anchor_head_count": baseline_action_counts[
                    "artifact_action_anchor_heads"
                ],
                "allowed_post_start_write_scope": [
                    "artifact_action_anchor_heads",
                    "artifact_action_anchors",
                    "artifact_action_events",
                    "artifact_action_heads",
                ],
            }

            def current_status() -> dict[str, Any]:
                action_counts = _action_storage_counts(database_path)
                non_action_sha256 = _database_snapshot_sha256(
                    database_path,
                    excluded_tables=ACTION_STORAGE_TABLES,
                )
                with qa_lock, network_lock:
                    counters = dict(qa_state)
                    provider_calls = fake_providers.call_attempts
                    market_reads = (
                        forbidden_market.access_attempts
                        + forbidden_market_readiness.access_attempts
                    )
                    outbound_attempts = network_state["outbound_connect_attempts"]
                event_delta = (
                    action_counts["artifact_action_events"]
                    - baseline_action_counts["artifact_action_events"]
                )
                head_delta = (
                    action_counts["artifact_action_heads"]
                    - baseline_action_counts["artifact_action_heads"]
                )
                anchor_delta = (
                    action_counts["artifact_action_anchors"]
                    - baseline_action_counts["artifact_action_anchors"]
                )
                anchor_head_delta = (
                    action_counts["artifact_action_anchor_heads"]
                    - baseline_action_counts["artifact_action_anchor_heads"]
                )
                non_action_unchanged = (
                    non_action_sha256 == baseline_non_action_sha256
                )
                write_scope_verified = (
                    non_action_unchanged
                    and event_delta >= 0
                    and head_delta >= 0
                    and anchor_delta == event_delta
                    and anchor_head_delta == head_delta
                    and head_delta <= anchor_delta
                    and event_delta <= counters["action_transition_http_requests"]
                )
                return {
                    **counters,
                    "provider_calls": provider_calls,
                    "provider_status_reads": fake_providers.status_reads,
                    "market_reads": market_reads,
                    "outbound_connect_attempts": outbound_attempts,
                    "action_event_count": action_counts["artifact_action_events"],
                    "action_head_count": action_counts["artifact_action_heads"],
                    "action_anchor_count": action_counts[
                        "artifact_action_anchors"
                    ],
                    "action_anchor_head_count": action_counts[
                        "artifact_action_anchor_heads"
                    ],
                    "local_action_transition_writes": event_delta,
                    "local_action_head_creates": head_delta,
                    "local_action_anchor_writes": anchor_delta,
                    "local_action_anchor_head_creates": anchor_head_delta,
                    "non_action_storage_unchanged": non_action_unchanged,
                    "only_local_action_transition_writes_verified": (
                        write_scope_verified
                    ),
                    "zero_provider_market_outbound_verified": (
                        provider_calls == 0
                        and market_reads == 0
                        and outbound_attempts == 0
                    ),
                }

            class QaRequestHandler(http_server.StudioRequestHandler):
                def do_GET(self) -> None:
                    path = urlparse(self.path).path
                    if path == "/__qa/stop":
                        if not self._guard_request():
                            return
                        self._send_json({"ok": True, "stopping": True})
                        threading.Thread(
                            target=self.server.shutdown,
                            daemon=True,
                        ).start()
                        return
                    if path == "/__qa/status":
                        if not self._guard_request():
                            return
                        self._send_json({"ok": True, **current_status()})
                        return
                    if path == desk_path:
                        with qa_lock:
                            qa_state["action_desk_http_reads"] += 1
                    if path == overview_path:
                        with qa_lock:
                            qa_state["action_overview_http_reads"] += 1
                    super().do_GET()

                def _reject_post_start_write(self) -> None:
                    if not self._guard_request(mutating=True):
                        return
                    with qa_lock:
                        qa_state["blocked_post_start_write_attempts"] += 1
                    self._send_json(
                        {
                            "ok": False,
                            "error": (
                                "isolated Action Desk QA permits only local action "
                                "transitions after server start"
                            ),
                            "code": "ISOLATED_QA_WRITE_ROUTE_DISABLED",
                        },
                        HTTPStatus.METHOD_NOT_ALLOWED,
                    )

                def do_POST(self) -> None:
                    path = urlparse(self.path).path
                    if path == transition_path:
                        with qa_lock:
                            qa_state["action_transition_http_requests"] += 1
                        super().do_POST()
                        return
                    self._reject_post_start_write()

                def do_PATCH(self) -> None:
                    self._reject_post_start_write()

                def do_DELETE(self) -> None:
                    self._reject_post_start_write()

                def do_PUT(self) -> None:
                    self._reject_post_start_write()

            server = ThreadingHTTPServer(("127.0.0.1", 0), QaRequestHandler)
            # Join every request handler before TemporaryDirectory removes the
            # SQLite file.  A daemon stop-handler can otherwise outlive
            # ``serve_forever`` briefly on Windows and retain a database lock.
            server.daemon_threads = False
            server.block_on_close = True
            if server.server_port == 8770:
                server.server_close()
                raise RuntimeError("ephemeral Action Desk QA must never use port 8770")
            qa_state["port"] = server.server_port
            url = f"http://127.0.0.1:{server.server_port}/"
            ready_payload = {
                "ok": True,
                "url": url,
                "qa_status_url": f"{url}__qa/status",
                "qa_stop_url": f"{url}__qa/stop",
                "room_id": room_id,
                "overview_room_id": overview_room_id,
                "artifact_id": artifact_id,
                "artifact_version": artifact_version,
                "formal_port_8770_used": False,
                "temporary_database": True,
            }
            ready_file_name = os.environ.get("AI_STUDIO_QA_READY_FILE", "").strip()
            if ready_file_name:
                ready_path = Path(ready_file_name).resolve()
                temp_parent = Path(tempfile.gettempdir()).resolve()
                try:
                    ready_path.relative_to(temp_parent)
                except ValueError as exc:
                    raise RuntimeError(
                        "AI_STUDIO_QA_READY_FILE must stay inside the system temp directory"
                    ) from exc
                ready_path.write_text(
                    json.dumps(ready_payload, ensure_ascii=False),
                    encoding="utf-8",
                )
            print(json.dumps(ready_payload, ensure_ascii=False), flush=True)
            print(
                "Open the workspace action overview, verify two adopted items across "
                "two rooms, filter them, and return to either exact room Action Desk.",
                flush=True,
            )
            print("Press Ctrl+C to stop and delete the temporary database.", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()

            final_status = current_status()
            if final_status["provider_calls"]:
                raise RuntimeError("a Provider dependency was accessed during QA")
            if final_status["market_reads"]:
                raise RuntimeError("a market or Futu dependency was accessed during QA")
            if final_status["outbound_connect_attempts"]:
                raise RuntimeError("outbound network access was attempted during QA")
            if not final_status["only_local_action_transition_writes_verified"]:
                raise RuntimeError("a post-start write escaped the Action Desk tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
