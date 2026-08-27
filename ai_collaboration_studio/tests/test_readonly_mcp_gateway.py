from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.manual_chatgpt import ManualChatGPTService
from backend.readonly_mcp_gateway import (
    MCP_ENDPOINT_PATH,
    MCP_PROTOCOL_VERSION,
    CapabilityAuthorizer,
    ReadOnlyManualChatGPTDataSource,
    ReadonlyMCPApplication,
    ReadonlyMCPError,
    ReadonlyMCPGateway,
    build_http_server,
    mcp_tool_definitions,
)
from backend.store import StudioStore
from tests.test_manual_chatgpt import FakeReviewProvider, valid_result


class FakeReviewRegistry:
    def __init__(self, provider: FakeReviewProvider) -> None:
        self.provider = provider

    def get(self, provider_id: str) -> FakeReviewProvider:
        if provider_id != self.provider.provider_id:
            raise KeyError(provider_id)
        return self.provider


class ReadonlyMCPFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-readonly-mcp-",
            ignore_cleanup_errors=True,
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)
        created = self.store.create_room(
            "Read-only MCP room",
            "Test only the isolated read-only gateway.",
        )
        self.room_id = str(created["room"]["id"])
        material = self.store.add_material(self.room_id, {
            "title": "Bounded local evidence",
            "kind": "note",
            "content": (
                "Safe prefix. Bearer abcdefghijklmnop must be hidden. "
                "C:\\private\\studio.sqlite3 must be hidden. Safe suffix."
            ),
        })
        assert material is not None
        self.evidence_id = str(material["id"])
        service = ManualChatGPTService(self.store, review_rate_card={})
        self.session = service.create(
            self.room_id,
            objective="Review https://example.test/evidence?api_key=hidden within fixed bounds.",
            mode="standard",
        )
        self.round_id = str(self.session["round_id"])
        self.now = 1_900_000_000
        self.authorizer = CapabilityAuthorizer(
            "test-signing-secret-that-is-longer-than-thirty-two-bytes",
            clock=lambda: self.now,
        )
        self.token = self.authorizer.mint(
            self.room_id,
            self.round_id,
            ttl_seconds=300,
        )
        self.data_source = ReadOnlyManualChatGPTDataSource(self.database_path)
        self.gateway = ReadonlyMCPGateway(self.data_source, self.authorizer)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()


class CapabilityAuthorizerTests(unittest.TestCase):
    def test_token_is_bound_to_room_round_ttl_and_signature(self) -> None:
        now = [2_000_000_000]
        authorizer = CapabilityAuthorizer(
            b"0123456789abcdef0123456789abcdef",
            clock=lambda: now[0],
        )
        token = authorizer.mint("room_one", "round_one", ttl_seconds=10)
        claims = authorizer.authorize(
            token,
            room_id="room_one",
            round_id="round_one",
        )
        self.assertEqual(claims.expires_at, now[0] + 10)
        with self.assertRaises(ReadonlyMCPError) as wrong_room:
            authorizer.authorize(token, room_id="room_two", round_id="round_one")
        self.assertEqual(wrong_room.exception.code, "MCP_UNAUTHORIZED")
        tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
        with self.assertRaises(ReadonlyMCPError):
            authorizer.authorize(tampered)
        now[0] += 10
        with self.assertRaises(ReadonlyMCPError) as expired:
            authorizer.authorize(token)
        self.assertEqual(expired.exception.code, "MCP_UNAUTHORIZED")

    def test_secret_and_ttl_fail_closed(self) -> None:
        with self.assertRaises(ReadonlyMCPError) as short_secret:
            CapabilityAuthorizer("too-short")
        self.assertEqual(short_secret.exception.code, "MCP_SECRET_TOO_SHORT")
        authorizer = CapabilityAuthorizer(b"x" * 32)
        with self.assertRaises(ReadonlyMCPError) as invalid_ttl:
            authorizer.mint("room", "round", ttl_seconds=901)
        self.assertEqual(invalid_ttl.exception.code, "MCP_TTL_INVALID")


class ReadonlyMCPGatewayTests(ReadonlyMCPFixture):
    def test_four_tools_return_bounded_sanitized_projections(self) -> None:
        scope = {"room_id": self.room_id, "round_id": self.round_id}
        bundle = self.gateway.call_tool("get_room_bundle", scope, token=self.token)
        encoded_bundle = json.dumps(bundle, ensure_ascii=False)
        self.assertTrue(bundle["sanitized"])
        self.assertEqual(
            bundle["source_bundle_sha256"],
            self.session["bundle_sha256"],
        )
        self.assertNotIn("api_key=hidden", encoded_bundle)
        self.assertNotIn("abcdefghijklmnop", encoded_bundle)
        self.assertNotIn("C:\\private", encoded_bundle)
        self.assertIn("[REDACTED", encoded_bundle)

        evidence = self.gateway.call_tool(
            "get_evidence_chunk",
            scope | {"evidence_id": self.evidence_id, "offset": 0, "limit": 80},
            token=self.token,
        )
        self.assertLessEqual(evidence["returned_characters"], 80)
        self.assertTrue(evidence["source_limited_to_frozen_excerpt"])
        self.assertNotIn("abcdefghijklmnop", evidence["chunk"])
        self.assertNotIn("C:\\private", evidence["chunk"])

        status = self.gateway.call_tool("get_round_status", scope, token=self.token)
        self.assertEqual(status["version"], "readonly_mcp_projection_v2")
        self.assertEqual(status["state"], "BUNDLE_READY")
        self.assertTrue(status["integrity"]["ok"])
        self.assertEqual(status["independent_api_reviews"]["completed"], 0)
        self.assertEqual(status["independent_api_reviews"]["status"], "not_started")
        self.assertFalse(status["safety"]["sqlite_write_capability"])

        contract = self.gateway.call_tool("get_import_contract", scope, token=self.token)
        self.assertEqual(contract["import_location"], "host_application_only")
        self.assertTrue(contract["user_confirmation_required"])
        self.assertTrue(contract["contract"]["one_json_object_only"])
        self.assertTrue(contract["contract"]["duplicate_keys_rejected"])
        self.assertTrue(contract["contract"]["nonfinite_numbers_rejected"])
        self.assertEqual(contract["contract"], self.session["import_contract"])

        outputs = {item["name"]: item["outputSchema"] for item in mcp_tool_definitions()}
        self.assertIn("bundle", outputs["get_room_bundle"]["required"])
        self.assertIn("chunk", outputs["get_evidence_chunk"]["required"])
        self.assertIn("integrity", outputs["get_round_status"]["required"])
        self.assertIn("contract", outputs["get_import_contract"]["required"])
        self.assertTrue(all(schema["additionalProperties"] is False for schema in outputs.values()))

    def test_scope_unknown_inputs_and_chunk_limits_fail_closed(self) -> None:
        scope = {"room_id": self.room_id, "round_id": self.round_id}
        with self.assertRaises(ReadonlyMCPError) as cross_room:
            self.gateway.call_tool(
                "get_round_status",
                scope | {"room_id": "room_other"},
                token=self.token,
            )
        self.assertEqual(cross_room.exception.code, "MCP_UNAUTHORIZED")
        with self.assertRaises(ReadonlyMCPError) as extra:
            self.gateway.call_tool(
                "get_room_bundle",
                scope | {"database_path": "C:\\formal.sqlite3"},
                token=self.token,
            )
        self.assertEqual(extra.exception.code, "MCP_ARGUMENT_INVALID")
        with self.assertRaises(ReadonlyMCPError) as too_large:
            self.gateway.call_tool(
                "get_evidence_chunk",
                scope | {"evidence_id": self.evidence_id, "limit": 1_601},
                token=self.token,
            )
        self.assertEqual(too_large.exception.code, "MCP_ARGUMENT_INVALID")
        with self.assertRaises(ReadonlyMCPError) as boolean_limit:
            self.gateway.call_tool(
                "get_evidence_chunk",
                scope | {"evidence_id": self.evidence_id, "limit": True},
                token=self.token,
            )
        self.assertEqual(boolean_limit.exception.code, "MCP_ARGUMENT_INVALID")

    def test_sqlite_connection_is_query_only_and_database_bytes_do_not_change(self) -> None:
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        with closing(self.data_source._connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden_write(value TEXT)")
        self.data_source.load(self.room_id, self.round_id)
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(after, before)

    def test_database_path_replacement_fails_closed(self) -> None:
        original_path = self.database_path.with_suffix(".original.sqlite3")
        os.replace(self.database_path, original_path)
        shutil.copyfile(original_path, self.database_path)
        with self.assertRaises(ReadonlyMCPError) as caught:
            self.data_source.load(self.room_id, self.round_id)
        self.assertEqual(caught.exception.code, "MCP_DATABASE_PATH_UNSAFE")

    def test_tampered_event_chain_hides_all_round_content(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """UPDATE manual_chatgpt_events SET event_type='tampered'
                         WHERE session_id=? AND sequence_no=1""",
                    (self.session["id"],),
                )
        with self.assertRaises(ReadonlyMCPError) as caught:
            self.gateway.call_tool(
                "get_room_bundle",
                {"room_id": self.room_id, "round_id": self.round_id},
                token=self.token,
            )
        self.assertEqual(caught.exception.code, "MCP_INTEGRITY_FAILED")

    def test_completed_api_review_status_is_verified_without_exposing_review_body(self) -> None:
        provider = FakeReviewProvider()
        service = ManualChatGPTService(
            self.store,
            review_rate_card={},
            providers=FakeReviewRegistry(provider),
        )
        created = service.create(
            self.room_id,
            objective="Verify the completed review projection.",
            mode="standard",
        )
        waiting = service.dispatch(self.room_id, created["id"])
        imported = service.import_result(
            self.room_id,
            created["id"],
            json.dumps(valid_result(waiting), ensure_ascii=False),
        )
        reviewed = service.run_api_review(
            self.room_id,
            created["id"],
            provider_id=provider.provider_id,
            model="fake-review-v1",
            client_request_id="readonly-mcp-completed-review",
            expected_result_sha256=imported["result_sha256"],
        )
        self.assertEqual(reviewed["state"], "READY_FOR_DECISION")
        token = self.authorizer.mint(
            self.room_id,
            str(created["round_id"]),
            ttl_seconds=300,
        )
        status = self.gateway.call_tool(
            "get_round_status",
            {"room_id": self.room_id, "round_id": created["round_id"]},
            token=token,
        )
        reviews = status["independent_api_reviews"]
        self.assertEqual(reviews["planned"], 3)
        self.assertEqual(reviews["completed"], 3)
        self.assertEqual(reviews["status"], "completed")
        self.assertTrue(reviews["all_calls_are_distinct"])
        self.assertTrue(reviews["integrity_ok"])
        encoded = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("Independent fact_check completed", encoded)
        self.assertEqual(status["safety"]["provider_calls_performed"], 0)

        with closing(sqlite3.connect(self.database_path)) as connection:
            with connection:
                connection.execute(
                    """UPDATE manual_chatgpt_api_reviews
                           SET response_model='tampered-model'
                         WHERE session_id=? AND review_index=1""",
                    (created["id"],),
                )
        with self.assertRaises(ReadonlyMCPError) as caught:
            self.gateway.call_tool(
                "get_round_status",
                {"room_id": self.room_id, "round_id": created["round_id"]},
                token=token,
            )
        self.assertEqual(caught.exception.code, "MCP_INTEGRITY_FAILED")

    def test_pre_review_schema_remains_readable_and_reports_migration_required(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            with connection:
                connection.execute("DROP TABLE manual_chatgpt_decisions")
                connection.execute("DROP TABLE manual_chatgpt_api_reviews")
                connection.execute("DROP TABLE manual_chatgpt_review_runs")
        status = self.gateway.call_tool(
            "get_round_status",
            {"room_id": self.room_id, "round_id": self.round_id},
            token=self.token,
        )
        self.assertTrue(status["integrity"]["ok"])
        self.assertEqual(
            status["independent_api_reviews"]["status"],
            "migration_required",
        )
        self.assertEqual(status["independent_api_reviews"]["completed"], 0)


class ReadonlyMCPHTTPTests(ReadonlyMCPFixture):
    def setUp(self) -> None:
        super().setUp()
        application = ReadonlyMCPApplication(self.gateway, rate_limit_per_minute=30)
        self.server = build_http_server(application, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}{MCP_ENDPOINT_PATH}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def post(
        self,
        message: dict[str, object],
        *,
        token: str | None = None,
        origin: str | None = None,
        protocol_version: str | None = MCP_PROTOCOL_VERSION,
    ) -> tuple[int, dict[str, object]]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if origin is not None:
            headers["Origin"] = origin
        if protocol_version is not None:
            headers["MCP-Protocol-Version"] = protocol_version
        request = Request(
            self.url,
            data=json.dumps(message).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlopen(request, timeout=5) as response:
                body = response.read()
                return response.status, json.loads(body) if body else {}
        except HTTPError as exc:
            try:
                body = exc.read()
                return exc.code, json.loads(body) if body else {}
            finally:
                exc.close()

    def test_streamable_http_initialize_list_and_call(self) -> None:
        status, initialized = self.post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "isolated-test", "version": "1"},
            },
        }, token=self.token, protocol_version=None)
        self.assertEqual(status, 200)
        self.assertEqual(initialized["result"]["protocolVersion"], MCP_PROTOCOL_VERSION)
        self.assertEqual(initialized["result"]["capabilities"]["tools"]["listChanged"], False)

        status, listed = self.post({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }, token=self.token)
        self.assertEqual(status, 200)
        tools = listed["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            [
                "get_room_bundle",
                "get_evidence_chunk",
                "get_round_status",
                "get_import_contract",
            ],
        )
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])

        status, called = self.post({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "get_round_status",
                "arguments": {
                    "room_id": self.room_id,
                    "round_id": self.round_id,
                },
            },
        }, token=self.token)
        self.assertEqual(status, 200)
        self.assertFalse(called["result"]["isError"])
        self.assertEqual(called["result"]["structuredContent"]["state"], "BUNDLE_READY")
        self.assertEqual(len(called["result"]["content"]), 1)
        self.assertEqual(called["result"]["content"][0]["type"], "text")
        self.assertEqual(
            json.loads(called["result"]["content"][0]["text"]),
            called["result"]["structuredContent"],
        )
        self.assertEqual(
            called["result"]["content"][0]["text"],
            json.dumps(
                called["result"]["structuredContent"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )

    def test_transport_rejects_missing_auth_bad_origin_protocol_and_get(self) -> None:
        request_message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
        status, unauthorized = self.post(request_message)
        self.assertEqual(status, 401)
        self.assertEqual(unauthorized["error"]["data"]["code"], "MCP_UNAUTHORIZED")
        status, forbidden = self.post(
            request_message,
            token=self.token,
            origin="https://attacker.invalid",
        )
        self.assertEqual(status, 403)
        self.assertEqual(forbidden["error"]["data"]["code"], "MCP_ORIGIN_FORBIDDEN")
        status, unsupported = self.post(
            request_message,
            token=self.token,
            protocol_version="1900-01-01",
        )
        self.assertEqual(status, 400)
        self.assertEqual(unsupported["error"]["data"]["code"], "MCP_PROTOCOL_UNSUPPORTED")
        try:
            urlopen(Request(self.url, method="GET"), timeout=5)
        except HTTPError as exc:
            try:
                self.assertEqual(exc.code, 405)
            finally:
                exc.close()
        else:
            self.fail("GET must be rejected because this server emits no SSE stream.")

    def test_transport_rejects_duplicate_json_keys_and_invalid_initialize_shape(self) -> None:
        raw = (
            b'{"jsonrpc":"2.0","id":1,"method":"tools/list",'
            b'"params":{},"params":{"cursor":""}}'
        )
        request = Request(
            self.url,
            data=raw,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            },
            method="POST",
        )
        try:
            urlopen(request, timeout=5)
        except HTTPError as exc:
            try:
                self.assertEqual(exc.code, 400)
                payload = json.loads(exc.read())
                self.assertEqual(payload["error"]["code"], -32700)
            finally:
                exc.close()
        else:
            self.fail("Duplicate JSON keys must be rejected.")

        status, invalid = self.post({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"protocolVersion": MCP_PROTOCOL_VERSION},
        }, token=self.token, protocol_version=None)
        self.assertEqual(status, 400)
        self.assertEqual(invalid["error"]["code"], -32602)

    def test_protected_ports_and_non_loopback_bind_fail_before_start(self) -> None:
        application = ReadonlyMCPApplication(self.gateway)
        for port in (8770, 11111):
            with self.assertRaises(ReadonlyMCPError) as protected:
                build_http_server(application, host="127.0.0.1", port=port)
            self.assertEqual(protected.exception.code, "MCP_PORT_FORBIDDEN")
        with self.assertRaises(ReadonlyMCPError) as non_loopback:
            build_http_server(application, host="0.0.0.0", port=0)
        self.assertEqual(non_loopback.exception.code, "MCP_BIND_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
