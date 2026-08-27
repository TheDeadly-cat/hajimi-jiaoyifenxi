from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from backend import http_server
from backend.collaboration_result import verify_collaboration_result
from backend.project_invocation import (
    PROJECT_INVOCATION_ACTION_INTAKE,
    PROJECT_INVOCATION_ACTION_RESULT_READ,
    ProjectCapabilityAuthorizer,
    seal_project_invocation_envelope,
)
from backend.store import StudioStore
from tests.test_project_invocation_capability import unsealed_envelope


SECRET = "project-http-capability-secret-at-least-32-bytes"


class ProjectInvocationHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-project-invocation-http-"
        )
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        self.original_store = http_server.STORE
        self.original_secret = http_server.PROJECT_CAPABILITY_SIGNING_SECRET
        http_server.STORE = self.store
        http_server.PROJECT_CAPABILITY_SIGNING_SECRET = SECRET
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.envelope = seal_project_invocation_envelope(unsealed_envelope())

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        http_server.PROJECT_CAPABILITY_SIGNING_SECRET = self.original_secret
        self.temp_dir.cleanup()

    def mint(
        self,
        envelope: dict[str, object] | None = None,
        *,
        actions: list[str] | None = None,
    ) -> str:
        value = envelope or self.envelope
        return ProjectCapabilityAuthorizer(SECRET).mint(
            caller_id=value["caller_id"],
            project_id=value["project_id"],
            room_id=value["room_id"],
            actions=actions or [PROJECT_INVOCATION_ACTION_INTAKE],
            client_request_id=value["client_request_id"],
            request_sha256=value["request_sha256"],
            ttl_seconds=120,
        )

    def post(
        self,
        payload: dict[str, object],
        *,
        bearer: str | None = None,
        ui_token: str | None = None,
        path: str = http_server.PROJECT_INVOCATION_INTAKE_PATH,
        origin: str | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict[str, object]]:
        headers = {"Content-Type": content_type}
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        if ui_token is not None:
            headers["X-AI-Studio-Token"] = ui_token
        if origin is not None:
            headers["Origin"] = origin
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def get_result(
        self,
        client_request_id: str,
        *,
        bearer: str | None = None,
        ui_token: str | None = None,
        query: str = "",
    ) -> tuple[int, dict[str, object]]:
        headers = {}
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        if ui_token is not None:
            headers["X-AI-Studio-Token"] = ui_token
        request = Request(
            f"{self.base_url}/api/integration/project-invocations/"
            f"{client_request_id}/result{query}",
            headers=headers,
            method="GET",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def test_scoped_intake_creates_once_then_replays_same_room(self) -> None:
        token = self.mint()
        first_status, first = self.post(self.envelope, bearer=token)
        second_status, second = self.post(self.envelope, bearer=token)

        self.assertEqual(first_status, 201)
        self.assertTrue(first["created"])
        self.assertEqual(second_status, 200)
        self.assertFalse(second["created"])
        self.assertEqual(first["invocation"], second["invocation"])
        self.assertEqual(
            first["invocation"]["room_binding"]["room_id"],
            self.envelope["room_id"],
        )

    def test_scoped_result_read_returns_portable_pending_result_without_provider(self) -> None:
        token = self.mint(actions=[
            PROJECT_INVOCATION_ACTION_INTAKE,
            PROJECT_INVOCATION_ACTION_RESULT_READ,
        ])
        created_status, _ = self.post(self.envelope, bearer=token)
        self.assertEqual(created_status, 201)

        with patch.object(
            http_server.PROVIDERS,
            "status",
            side_effect=AssertionError("result read must not inspect providers"),
        ):
            status, payload = self.get_result(
                str(self.envelope["client_request_id"]),
                bearer=token,
            )

        self.assertEqual(status, 200)
        result = payload["result"]
        self.assertEqual(
            verify_collaboration_result(
                result,
                expected_envelope=self.envelope,
            ),
            result,
        )
        self.assertEqual(result["user_boundary"]["status"], "pending")
        self.assertEqual(result["independent_review"]["status"], "not_run")
        self.assertEqual(result["safety"]["execution_capability"], "none")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(self.envelope["room_spec"]["title"], serialized)
        self.assertNotIn(self.envelope["room_spec"]["objective"], serialized)

    def test_bootstrap_ui_token_never_authorizes_integration_plane(self) -> None:
        status, payload = self.post(
            self.envelope,
            ui_token=http_server.LOCAL_SESSION_TOKEN,
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error_code"], "PROJECT_CAPABILITY_UNAUTHORIZED")

        both_status, both = self.post(
            self.envelope,
            bearer=self.mint(),
            ui_token=http_server.LOCAL_SESSION_TOKEN,
        )
        self.assertEqual(both_status, 401)
        self.assertEqual(both["error_code"], "PROJECT_CAPABILITY_UNAUTHORIZED")
        self.assertIsNone(
            self.store.get_project_invocation(
                caller_id=str(self.envelope["caller_id"]),
                project_id=str(self.envelope["project_id"]),
                client_request_id=str(self.envelope["client_request_id"]),
            )
        )
        result_status, result_payload = self.get_result(
            str(self.envelope["client_request_id"]),
            ui_token=http_server.LOCAL_SESSION_TOKEN,
        )
        self.assertEqual(result_status, 401)
        self.assertEqual(
            result_payload["error_code"],
            "PROJECT_CAPABILITY_UNAUTHORIZED",
        )

    def test_claim_drift_and_action_escalation_fail_closed(self) -> None:
        drifted_raw = unsealed_envelope()
        drifted_raw["input_manifest"]["content_bytes"] += 1
        drifted = seal_project_invocation_envelope(drifted_raw)
        status, payload = self.post(drifted, bearer=self.mint(self.envelope))
        self.assertEqual(status, 401)
        self.assertEqual(payload["error_code"], "PROJECT_CAPABILITY_UNAUTHORIZED")

        read_only = self.mint(
            actions=[PROJECT_INVOCATION_ACTION_RESULT_READ]
        )
        denied_status, denied = self.post(self.envelope, bearer=read_only)
        self.assertEqual(denied_status, 403)
        self.assertEqual(
            denied["error_code"],
            "PROJECT_CAPABILITY_ACTION_DENIED",
        )

    def test_unknown_room_template_cannot_silently_fall_back(self) -> None:
        raw = unsealed_envelope(
            project_id="project_unknown_template",
            client_request_id="request-unknown-template-0001",
        )
        raw["room_spec"]["template_id"] = "unregistered_template"
        envelope = seal_project_invocation_envelope(raw)
        status, payload = self.post(
            envelope,
            bearer=self.mint(envelope),
        )

        self.assertEqual(status, 400)
        self.assertEqual(
            payload["error_code"],
            "PROJECT_INVOCATION_ROOM_CONTRACT_UNSUPPORTED",
        )
        self.assertIsNone(
            self.store.get_project_invocation(
                caller_id=str(envelope["caller_id"]),
                project_id=str(envelope["project_id"]),
                client_request_id=str(envelope["client_request_id"]),
            )
        )

    def test_query_cross_origin_and_missing_secret_are_rejected_without_write(self) -> None:
        token = self.mint()
        query_status, query = self.post(
            self.envelope,
            bearer=token,
            path=http_server.PROJECT_INVOCATION_INTAKE_PATH + "?alias=1",
        )
        self.assertEqual(query_status, 400)
        self.assertEqual(
            query["error_code"],
            "PROJECT_INVOCATION_QUERY_UNSUPPORTED",
        )

        origin_status, _ = self.post(
            self.envelope,
            bearer=token,
            origin="https://attacker.example",
        )
        self.assertEqual(origin_status, 403)

        http_server.PROJECT_CAPABILITY_SIGNING_SECRET = ""
        unavailable_status, unavailable = self.post(
            self.envelope,
            bearer=token,
        )
        self.assertEqual(unavailable_status, 503)
        self.assertEqual(
            unavailable["error_code"],
            "PROJECT_CAPABILITY_UNAVAILABLE",
        )
        self.assertIsNone(
            self.store.get_project_invocation(
                caller_id=str(self.envelope["caller_id"]),
                project_id=str(self.envelope["project_id"]),
                client_request_id=str(self.envelope["client_request_id"]),
            )
        )

    def test_result_identity_query_not_found_and_budget_fail_closed(self) -> None:
        result_only = self.mint(actions=[PROJECT_INVOCATION_ACTION_RESULT_READ])
        missing_status, missing = self.get_result(
            str(self.envelope["client_request_id"]),
            bearer=result_only,
        )
        self.assertEqual(missing_status, 404)
        self.assertEqual(missing["error_code"], "PROJECT_INVOCATION_NOT_FOUND")

        query_status, query = self.get_result(
            str(self.envelope["client_request_id"]),
            bearer=result_only,
            query="?verbose=1",
        )
        self.assertEqual(query_status, 400)
        self.assertEqual(
            query["error_code"],
            "PROJECT_INVOCATION_QUERY_UNSUPPORTED",
        )

        mismatch_status, mismatch = self.get_result(
            "request-other-0001",
            bearer=result_only,
        )
        self.assertEqual(mismatch_status, 401)
        self.assertEqual(
            mismatch["error_code"],
            "PROJECT_CAPABILITY_UNAUTHORIZED",
        )

        small_raw = unsealed_envelope(
            project_id="project_budget",
            client_request_id="request-budget-0001",
        )
        small_raw["budget"]["max_result_bytes"] = 1
        small = seal_project_invocation_envelope(small_raw)
        small_token = self.mint(
            small,
            actions=[
                PROJECT_INVOCATION_ACTION_INTAKE,
                PROJECT_INVOCATION_ACTION_RESULT_READ,
            ],
        )
        create_status, _ = self.post(small, bearer=small_token)
        self.assertEqual(create_status, 201)
        budget_status, budget = self.get_result(
            str(small["client_request_id"]),
            bearer=small_token,
        )
        self.assertEqual(budget_status, 413)
        self.assertEqual(
            budget["error_code"],
            "PROJECT_INVOCATION_RESULT_BUDGET_EXCEEDED",
        )

    def test_time_bounded_result_read_expires_with_no_provider_access(self) -> None:
        raw = unsealed_envelope(
            project_id="project_expiring_result",
            client_request_id="request-expiring-result-0001",
        )
        raw["data_handling"] = {
            "classification": "confidential",
            "retention_policy": "ephemeral_24h",
            "retention_days": None,
        }
        envelope = seal_project_invocation_envelope(raw)
        token = self.mint(
            envelope,
            actions=[
                PROJECT_INVOCATION_ACTION_INTAKE,
                PROJECT_INVOCATION_ACTION_RESULT_READ,
            ],
        )
        created_at = 1_000_000_000
        with patch("backend.store.now_ms", return_value=created_at):
            create_status, created = self.post(envelope, bearer=token)
        self.assertEqual(create_status, 201)
        self.assertFalse(
            created["invocation"]["retention"]["room_payload_persisted"]
        )

        with (
            patch(
                "backend.store.now_ms",
                return_value=created_at + 86_400_000,
            ),
            patch.object(
                http_server.PROVIDERS,
                "status",
                side_effect=AssertionError("expired result must not inspect providers"),
            ),
        ):
            status, payload = self.get_result(
                str(envelope["client_request_id"]),
                bearer=token,
            )
        self.assertEqual(status, 410)
        self.assertEqual(
            payload["error_code"],
            "PROJECT_INVOCATION_RETENTION_EXPIRED",
        )


if __name__ == "__main__":
    unittest.main()
