from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http_server
from backend.store import StudioStore


class NonLoopbackPeerHTTPServer(ThreadingHTTPServer):
    """Serve on loopback while presenting a remote peer to the handler."""

    def finish_request(self, request, client_address) -> None:
        remote_peer = ("203.0.113.9", client_address[1])
        self.RequestHandlerClass(request, remote_peer, self)


class LocalHttpSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_store = http_server.STORE
        http_server.STORE = StudioStore(Path(self.temp_dir.name) / "security.sqlite3")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        self.temp_dir.cleanup()

    def post_room(self, *, token: str = "", origin: str = "", content_type: str = "application/json", host: str = "") -> int:
        headers = {"Content-Type": content_type}
        if token:
            headers["X-AI-Studio-Token"] = token
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        request = Request(
            f"{self.base_url}/api/rooms",
            method="POST",
            headers=headers,
            data=json.dumps({"title": "安全测试房间", "objective": "验证本地写边界"}).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status
        except HTTPError as exc:
            code = exc.code
            exc.close()
            return code

    def test_mutations_require_json_same_origin_and_session_token(self) -> None:
        self.assertEqual(self.post_room(), 403)
        self.assertEqual(
            self.post_room(token=http_server.LOCAL_SESSION_TOKEN, content_type="text/plain"),
            415,
        )
        self.assertEqual(
            self.post_room(
                token=http_server.LOCAL_SESSION_TOKEN,
                origin="https://attacker.example",
            ),
            403,
        )
        self.assertEqual(
            self.post_room(token=http_server.LOCAL_SESSION_TOKEN, host="attacker.example"),
            403,
        )
        self.assertEqual(self.post_room(token=http_server.LOCAL_SESSION_TOKEN), 201)

    def test_rejected_post_bodies_are_drained_without_windows_connection_reset(
        self,
    ) -> None:
        cases = (
            {"token": ""},
            {
                "token": http_server.LOCAL_SESSION_TOKEN,
                "content_type": "text/plain",
            },
            {
                "token": http_server.LOCAL_SESSION_TOKEN,
                "origin": "https://attacker.example",
            },
            {
                "token": http_server.LOCAL_SESSION_TOKEN,
                "host": "attacker.example",
            },
        )
        expected = (403, 415, 403, 403)
        for iteration in range(20):
            with self.subTest(iteration=iteration):
                self.assertEqual(
                    tuple(self.post_room(**case) for case in cases),
                    expected,
                )

    def test_responses_block_iframe_embedding(self) -> None:
        with urlopen(f"{self.base_url}/api/health", timeout=3) as response:
            self.assertEqual(response.headers.get("X-Frame-Options"), "DENY")
            self.assertIn("frame-ancestors 'none'", response.headers.get("Content-Security-Policy") or "")
            self.assertEqual(response.headers.get("X-Content-Type-Options"), "nosniff")

    def test_execution_shaped_routes_fail_closed_before_dispatch(self) -> None:
        for path in ("/api/orders", "/api/placeOrder", "/api/accounts/demo/transfers"):
            with self.subTest(path=path):
                try:
                    urlopen(f"{self.base_url}{path}", timeout=3)
                except HTTPError as exc:
                    payload = json.loads(exc.read().decode("utf-8"))
                    self.assertEqual(exc.code, 403)
                    self.assertEqual(
                        payload.get("error_code"),
                        "EXECUTION_CAPABILITY_DISABLED",
                    )
                    exc.close()
                else:
                    self.fail(f"execution-shaped route was not blocked: {path}")

    def test_hashed_static_asset_names_do_not_enter_the_api_execution_gate(self) -> None:
        asset_dir = Path(self.temp_dir.name) / "dist" / "assets"
        asset_dir.mkdir(parents=True)
        asset = asset_dir / "RoundExecutionTraceDialog-test.css"
        asset.write_text(".round-trace-dialog { display: block; }", encoding="utf-8")

        with patch.object(http_server, "FRONTEND_DIST", asset_dir.parent):
            with urlopen(
                f"{self.base_url}/assets/{asset.name}",
                timeout=3,
            ) as response:
                body = response.read().decode("utf-8")

        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.headers.get_content_type(),
            "text/css",
        )
        self.assertIn("round-trace-dialog", body)

    def test_audit_trace_is_read_only_without_weakening_execution_route_gate(self) -> None:
        created = http_server.STORE.create_room(
            "轨迹安全测试房间",
            "验证只读审计轨迹不创建执行能力。",
        )
        room_id = str(created["room"]["id"])
        round_row = http_server.STORE.create_round(room_id, "验证只读审计轨迹")
        round_id = str(round_row["id"])

        with urlopen(
            f"{self.base_url}/api/rooms/{room_id}/rounds/{round_id}/audit-trace",
            timeout=3,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertTrue(payload.get("ok"))
        safety = (payload.get("trace") or {}).get("safety") or {}
        self.assertTrue(safety.get("read_only"))
        self.assertEqual(safety.get("provider_calls_performed"), 0)
        self.assertEqual(safety.get("execution_capability"), "none")
        self.assertFalse(safety.get("live_trading_allowed"))

        with self.assertRaises(HTTPError) as caught:
            urlopen(
                f"{self.base_url}/api/rooms/{room_id}/rounds/{round_id}/execution-trace",
                timeout=3,
            )
        blocked = caught.exception
        blocked_payload = json.loads(blocked.read().decode("utf-8"))
        blocked.close()
        self.assertEqual(blocked.code, 403)
        self.assertEqual(
            blocked_payload.get("error_code"),
            "EXECUTION_CAPABILITY_DISABLED",
        )

    def test_loopback_address_gate_handles_ipv4_ipv6_and_mapped_ipv4(self) -> None:
        for address in (
            "localhost",
            "127.0.0.1",
            "127.255.255.254",
            "::1",
            "[::1]",
            "::ffff:127.0.0.1",
            "::ffff:7f00:1",
        ):
            with self.subTest(address=address):
                self.assertTrue(http_server._is_loopback_address(address))

        for address in ("", "0.0.0.0", "::", "192.168.1.2", "203.0.113.9", "example.com"):
            with self.subTest(address=address):
                self.assertFalse(http_server._is_loopback_address(address))

    def test_run_server_rejects_non_loopback_bind_before_server_creation(self) -> None:
        owner = Mock(spec=http_server.DatabaseInstanceOwner)
        with patch.object(http_server, "ThreadingHTTPServer") as server_factory:
            with self.assertRaisesRegex(ValueError, "回环地址"):
                http_server.run_server(
                    host="0.0.0.0",
                    port=8770,
                    instance_owner=owner,
                )

        server_factory.assert_not_called()
        owner.assert_held_for.assert_not_called()

    def test_remote_peer_cannot_spoof_local_host_or_read_bootstrap_token(self) -> None:
        server = NonLoopbackPeerHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/bootstrap",
                headers={"Host": f"localhost:{server.server_port}"},
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=3)
            response = caught.exception
            body = response.read().decode("utf-8")
            response.close()

            self.assertEqual(response.code, 403)
            self.assertNotIn("session_token", body)
            self.assertNotIn(http_server.LOCAL_SESSION_TOKEN, body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
