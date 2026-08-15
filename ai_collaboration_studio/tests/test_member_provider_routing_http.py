from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http_server
from backend.providers.registry import ProviderRegistry
from backend.store import StudioStore


class StatusOnlyProvider:
    def __init__(self, provider_id: str, *, configured: bool = False) -> None:
        self.provider_id = provider_id
        self.configured = configured
        self.status_calls = 0

    def status(self) -> dict[str, object]:
        self.status_calls += 1
        return {
            "id": self.provider_id,
            "name": f"Test {self.provider_id}",
            "configured": self.configured,
            "model": f"{self.provider_id}-default",
        }

    def probe(self, **_kwargs: object) -> object:
        raise AssertionError("member write validation must not probe providers")

    def generate(self, **_kwargs: object) -> object:
        raise AssertionError("member write validation must not call providers")


def valid_member_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "路由校验员",
        "identity": "验证成员模型路由写入边界",
        "responsibilities": "检查 Provider 标识，不执行模型请求。",
        "boundaries": "不得探测模型、调用模型或执行真实交易。",
        "provider": "deepseek",
        "model": "",
    }
    payload.update(overrides)
    return payload


class MemberProviderRoutingHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "member-provider-http.sqlite3")
        created = self.store.create_room(
            "成员 Provider 路由测试",
            "验证 HTTP 写入边界只接受已注册且未被策略禁用的 Provider。",
            template_id="open_collaboration",
        )
        self.room_id = str(created["room"]["id"])
        self.deepseek = StatusOnlyProvider("deepseek", configured=False)
        self.openai = StatusOnlyProvider("openai", configured=True)
        self.registry = ProviderRegistry(
            {
                "deepseek": self.deepseek,
                "openai": self.openai,
            },
            disabled_provider_ids={"openai"},
        )
        self.original_store = http_server.STORE
        self.original_providers = http_server.PROVIDERS
        http_server.STORE = self.store
        http_server.PROVIDERS = self.registry
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            http_server.StudioRequestHandler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        http_server.STORE = self.original_store
        http_server.PROVIDERS = self.original_providers
        self.temp_dir.cleanup()

    def request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object],
    ) -> tuple[int, dict[str, object]]:
        request = Request(
            f"{self.base_url}{path}",
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            finally:
                exc.close()

    def room_write_state(self) -> tuple[int, int, int]:
        with closing(sqlite3.connect(self.store.path)) as connection:
            row = connection.execute(
                """SELECT
                       (SELECT COUNT(*) FROM members WHERE room_id=?) AS members,
                       (SELECT COUNT(*) FROM member_versions WHERE room_id=?) AS versions,
                       (SELECT updated_at FROM rooms WHERE id=?) AS room_updated_at""",
                (self.room_id, self.room_id, self.room_id),
            ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    def member_write_state(self, member_id: str) -> tuple[object, ...]:
        with closing(sqlite3.connect(self.store.path)) as connection:
            member = connection.execute(
                """SELECT provider,model,identity,version,updated_at
                   FROM members WHERE room_id=? AND id=?""",
                (self.room_id, member_id),
            ).fetchone()
            versions = connection.execute(
                "SELECT COUNT(*) FROM member_versions WHERE room_id=? AND member_id=?",
                (self.room_id, member_id),
            ).fetchone()[0]
        return (*tuple(member), int(versions))

    def test_post_rejects_unknown_and_policy_disabled_without_writes(self) -> None:
        before = self.room_write_state()

        for provider, expected_error in (
            ("provider-not-installed", "未知 Provider"),
            ("openai", "策略禁用"),
        ):
            with self.subTest(provider=provider):
                status, body = self.request_json(
                    "POST",
                    f"/api/rooms/{self.room_id}/members",
                    valid_member_payload(provider=provider),
                )
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
                self.assertIn(expected_error, str(body["error"]))
                self.assertEqual(self.room_write_state(), before)

    def test_patch_rejects_changed_unknown_and_policy_disabled_without_versions(self) -> None:
        members = self.store.room_snapshot(self.room_id)["members"][:2]

        for member, provider, expected_error in (
            (members[0], "provider-not-installed", "未知 Provider"),
            (members[1], "openai", "策略禁用"),
        ):
            with self.subTest(provider=provider):
                before = self.member_write_state(str(member["id"]))
                status, body = self.request_json(
                    "PATCH",
                    f"/api/rooms/{self.room_id}/members/{member['id']}",
                    {
                        "expected_version": int(member["version"]),
                        "identity": "这次身份修改不得在非法路由旁路后落库",
                        "provider": provider,
                    },
                )
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
                self.assertIn(expected_error, str(body["error"]))
                self.assertEqual(self.member_write_state(str(member["id"])), before)

    def test_patch_allows_unchanged_historical_unknown_or_disabled_provider(self) -> None:
        members = self.store.room_snapshot(self.room_id)["members"][2:4]
        legacy_unknown = self.store.update_member(
            self.room_id,
            str(members[0]["id"]),
            {"provider": "legacy-provider", "model": "legacy-model"},
            expected_version=int(members[0]["version"]),
        )
        legacy_disabled = self.store.update_member(
            self.room_id,
            str(members[1]["id"]),
            {"provider": "openai", "model": "legacy-openai-model"},
            expected_version=int(members[1]["version"]),
        )

        status, body = self.request_json(
            "PATCH",
            f"/api/rooms/{self.room_id}/members/{legacy_unknown['id']}",
            {
                "expected_version": int(legacy_unknown["version"]),
                "identity": "历史未知 Provider 保持不变时仍可编辑身份",
                "provider": " LEGACY-PROVIDER ",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["member"]["provider"], "legacy-provider")

        status, body = self.request_json(
            "PATCH",
            f"/api/rooms/{self.room_id}/members/{legacy_disabled['id']}",
            {
                "expected_version": int(legacy_disabled["version"]),
                "identity": "历史禁用 Provider 未改动时仍可编辑身份",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["member"]["provider"], "openai")

    def test_deepseek_accepts_empty_or_custom_model_without_probe(self) -> None:
        status, body = self.request_json(
            "POST",
            f"/api/rooms/{self.room_id}/members",
            valid_member_payload(
                provider="DEEPSEEK",
                model="custom-model-not-claimed-by-registry",
            ),
        )
        self.assertEqual(status, 201)
        created = body["member"]
        self.assertEqual(created["provider"], "deepseek")
        self.assertEqual(created["model"], "custom-model-not-claimed-by-registry")

        legacy = self.store.update_member(
            self.room_id,
            str(created["id"]),
            {"provider": "legacy-provider", "model": "legacy-model"},
            expected_version=int(created["version"]),
        )
        status, body = self.request_json(
            "PATCH",
            f"/api/rooms/{self.room_id}/members/{legacy['id']}",
            {
                "expected_version": int(legacy["version"]),
                "provider": "deepseek",
                "model": "",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["member"]["provider"], "deepseek")
        self.assertEqual(body["member"]["model"], "")
        self.assertGreater(self.deepseek.status_calls, 0)


if __name__ == "__main__":
    unittest.main()
