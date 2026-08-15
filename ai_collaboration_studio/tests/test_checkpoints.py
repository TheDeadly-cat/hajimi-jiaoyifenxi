from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from backend import http_server
from backend.orchestrator import DiscussionOrchestrator
from backend.provider_call_ledger import ProviderCallLedger
from backend.store import StudioStore
from tests.test_orchestrator import FakeProvider, FakeRegistry


class FakeEarningsPackMarketService:
    @staticmethod
    def earnings_packs(symbol: str, *, limit: int, force: bool):
        if symbol != "US.MU":
            raise ValueError("不在官方 IR 源白名单")
        return {
            "ok": True,
            "version": "official_earnings_pack_v1",
            "pack_count": 1,
            "requested_limit": limit,
            "forced": force,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def earnings_materials(symbol: str, *, limit: int, force: bool):
        if symbol != "US.MU":
            raise ValueError("不在官方业绩材料源白名单")
        return {
            "ok": True,
            "state": "ready",
            "requested_limit": limit,
            "forced": force,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }


class CheckpointHttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StudioStore(Path(self.temp_dir.name) / "studio.sqlite3")
        self.orchestrator = DiscussionOrchestrator(
            self.store,
            FakeRegistry(FakeProvider()),
            market_service=None,
        )
        self.original_store = http_server.STORE
        self.original_orchestrator = http_server.ORCHESTRATOR
        self.original_market = http_server.STORAGE_MARKET
        http_server.STORE = self.store
        http_server.ORCHESTRATOR = self.orchestrator
        http_server.STORAGE_MARKET = FakeEarningsPackMarketService()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        http_server.STORE = self.original_store
        http_server.ORCHESTRATOR = self.original_orchestrator
        http_server.STORAGE_MARKET = self.original_market
        self.temp_dir.cleanup()

    def test_resume_stream_route_uses_existing_round(self) -> None:
        members = self.store.enabled_members("room_plan")
        for member in members:
            self.store.update_member(
                "room_plan",
                member["id"],
                {"provider": "openai", "model": "fake-model"},
            )
        round_row = self.store.create_round("room_plan", "HTTP 检查点恢复")
        self.store.add_message(
            "room_plan",
            sender_type="user",
            sender_id="user",
            sender_name="我",
            content=round_row["objective"],
            round_id=round_row["id"],
        )
        self.store.save_round_checkpoint("room_plan", round_row["id"], {
            "member_ids": [member["id"] for member in members],
            "spoken_counts": {},
            "spoken_stances": [],
            "previous_name": "我",
            "completed": 0,
            "failures": 0,
            "skipped": 0,
            "proposals_created": 0,
            "next_order": 1,
            "max_turns": len(members) + 1,
            "skip_provider_ids": [],
            "shared_context": "frozen-context",
            "market_snapshot": None,
        })
        self.store.complete_round(round_row["id"], "PAUSED")
        ledger = ProviderCallLedger.create(
            self.store,
            "room_plan",
            scope="round",
            client_request_id=f"checkpoint-resume-{round_row['id']}",
            plan_hash="a" * 64,
            max_calls=100,
            skip_provider_ids=set(),
        )
        ledger.bind_round(round_row["id"])
        request = Request(
            f"{self.base_url}/api/rooms/room_plan/rounds/{round_row['id']}/resume/stream",
            data=json.dumps({}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            method="POST",
        )

        with urlopen(request, timeout=10) as response:
            events = [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line.strip()]

        self.assertEqual(response.status, 200)
        self.assertEqual(events[0]["type"], "round_resumed")
        self.assertEqual(events[0]["round"]["id"], round_row["id"])
        self.assertEqual(events[-1]["type"], "round_completed")
        final = self.store.get_round("room_plan", round_row["id"])
        self.assertEqual(final["status"], "COMPLETED")
        self.assertEqual(final["resume_count"], 1)

    def test_convergence_endpoint_and_bootstrap_expose_read_only_gate(self) -> None:
        with urlopen(f"{self.base_url}/api/rooms/room_storage/convergence", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        with urlopen(f"{self.base_url}/api/bootstrap?room=room_storage", timeout=5) as response:
            bootstrap = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["convergence"]["template_id"], "us_storage_committee")
        self.assertEqual(payload["convergence"]["execution_capability"], "none")
        self.assertFalse(payload["convergence"]["live_trading_allowed"])
        self.assertEqual(
            bootstrap["active"]["convergence"]["decision_status"],
            payload["convergence"]["decision_status"],
        )

    def test_user_decision_route_binds_confirmed_artifact_version_and_returns_convergence(self) -> None:
        message = self.store.room_snapshot("room_plan")["messages"][0]
        evidence = [{
            "type": "message",
            "id": message["id"],
            "evidence_role": "support",
            "verification_status": "source_checked",
        }]
        artifact = self.store.create_artifact(
            "room_plan",
            title="HTTP 用户最终决定",
            content={
                "summary": "比较后先采用可逆方案。",
                "summary_evidence": evidence,
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {
                            "id": "small",
                            "title": "小范围",
                            "description": "先验证关键假设并保留回退空间。",
                            "evidence": evidence,
                        },
                        {
                            "id": "full",
                            "title": "完整范围",
                            "description": "一次性覆盖完整研究范围。",
                            "evidence": evidence,
                        },
                    ],
                    "preferred_option_id": "small",
                    "rationale": "优先可逆方案。",
                    "evidence": evidence,
                },
            },
        )
        artifact = self.store.confirm_artifact(
            "room_plan",
            artifact["id"],
            expected_version=artifact["version"],
        )
        request = Request(
            f"{self.base_url}/api/rooms/room_plan/artifacts/{artifact['id']}/user-decision",
            data=json.dumps({
                "expected_version": artifact["version"],
                "action": "support",
                "rationale": "支持当前候选，但不授权任何执行动作。",
                "selected_option_id": "small",
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            method="POST",
        )

        with urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["user_decision"]["action"], "support")
        self.assertEqual(payload["user_decision"]["artifact_version"], artifact["version"])
        self.assertEqual(payload["user_decision"]["execution_capability"], "none")
        self.assertFalse(payload["user_decision"]["live_trading_allowed"])
        self.assertTrue(payload["artifact"]["user_decision"]["is_current"])
        self.assertEqual(payload["convergence"]["user_decision_gate"]["action"], "support")

        stale_request = Request(
            request.full_url,
            data=json.dumps({
                "expected_version": artifact["version"] - 1,
                "action": "hold",
                "rationale": "错误的旧版本。",
            }).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-AI-Studio-Token": http_server.LOCAL_SESSION_TOKEN,
            },
            method="POST",
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(stale_request, timeout=5)
        self.assertEqual(raised.exception.code, 409)
        raised.exception.close()

    def test_official_earnings_pack_endpoint_preserves_read_only_boundary(self) -> None:
        with urlopen(
            f"{self.base_url}/api/market/storage/earnings-packs?symbol=US.MU&limit=6&force=1",
            timeout=5,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        packs = payload["earnings_packs"]
        self.assertTrue(payload["ok"])
        self.assertEqual(packs["version"], "official_earnings_pack_v1")
        self.assertEqual(packs["requested_limit"], 6)
        self.assertTrue(packs["forced"])
        self.assertEqual(packs["execution_capability"], "none")
        self.assertFalse(packs["live_trading_allowed"])

    def test_official_earnings_material_endpoint_preserves_read_only_boundary(self) -> None:
        with urlopen(
            f"{self.base_url}/api/market/storage/earnings-materials?symbol=US.MU&limit=9&force=1",
            timeout=5,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))

        materials = payload["earnings_materials"]
        self.assertEqual(materials["requested_limit"], 9)
        self.assertTrue(materials["forced"])
        self.assertEqual(materials["execution_capability"], "none")
        self.assertFalse(materials["live_trading_allowed"])


if __name__ == "__main__":
    unittest.main()
