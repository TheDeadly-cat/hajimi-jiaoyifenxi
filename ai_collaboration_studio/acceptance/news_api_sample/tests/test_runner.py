"""Focused local-only tests; no genuine key or external transport is used."""
import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT))
from acceptance.news_api_sample import runner as r

FAKE_KEY = "SYNTHETIC_NEVER_VALID_KEY"
ITEM = {"id": "synthetic-one", "source_id": "fed", "source_name": "Synthetic fixture",
        "url": "https://www.federalreserve.gov/synthetic-test-only.htm",
        "title": "Synthetic local acceptance fixture", "excerpt": "The synthetic board ended its order.",
        "published_at": "2026-01-01T00:00:00Z", "discovered_at": "2026-01-01T00:02:00Z",
        "updated_at": "2026-01-01T00:02:00Z"}


def date(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class Response(io.BytesIO):
    status = 200


class AcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapter = r.provider_module()
        assert not hasattr(sys.modules["backend.config"], "__file__")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample = self.root / "synthetic.json"
        r.exclusive_json(self.sample, ITEM)
        self.sample_sha = r.digest(self.sample.read_bytes())
        self.prepared = r.prepare(self.sample, self.sample_sha, self.root / "run")
        self.plan_path = Path(self.prepared["plan"])
        self.plan_sha = self.prepared["plan_sha256"]
        self.plan, self.item = r.inspect_plan(self.plan_path, self.plan_sha)
        self.auth = {"schema": "news_single_rss_authorization_v1", "approved": True,
                     "plan_sha256": self.plan_sha, "grant_id": self.plan["grant_id"], "max_calls": 1,
                     "max_reserved_micro_cny": r.LIMIT, "approved_at": date(time.time() - 1),
                     "expires_at": date(time.time() + 120), "approval_reference": "SYNTHETIC OFFLINE ONLY"}
        self.reset = patch.object(r, "_PROCESS_TRANSPORT_SPENT", False)
        self.reset.start()
        original_connect, original_dns = socket.socket.connect, socket.getaddrinfo

        def local_connect(connection, address):
            if address[0] != "127.0.0.1" or address[1] == 8770:
                raise AssertionError("external_or_production_network_forbidden")
            return original_connect(connection, address)

        def local_dns(host, *args, **kwargs):
            if host != "127.0.0.1":
                raise AssertionError("external_dns_forbidden")
            return original_dns(host, *args, **kwargs)

        self.net = patch.object(socket.socket, "connect", local_connect)
        self.dns = patch.object(socket, "getaddrinfo", local_dns)
        self.net.start()
        self.dns.start()
        self.calls = []

    def tearDown(self):
        self.net.stop()
        self.dns.stop()
        self.reset.stop()
        self.temp.cleanup()

    def opener(self, request, *, timeout):
        self.calls.append(request.data)
        self.assertEqual(timeout, 45)
        self.assertEqual(request.full_url, r.ENDPOINT)
        self.assertTrue((self.plan_path.parent / "spent.json").is_file())
        body = json.loads(request.data)
        self.assertEqual(body["model"], r.MODEL)
        self.assertEqual(body["max_output_tokens"], 900)
        result = {"summary": "合成资料称董事会结束该命令。", "key_points": [{"claim": "命令已结束。",
                  "evidence_field": "excerpt", "quote": ITEM["excerpt"]}],
                  "uncertainties": ["这里只阅读合成 RSS 摘录，未核对全文。"]}
        return Response(json.dumps({"status": "completed", "model": r.MODEL,
                                   "output_text": json.dumps(result, ensure_ascii=False),
                                   "usage": {"input_tokens": 100, "output_tokens": 50}}).encode())

    def test_prepare_is_unapproved_and_changed_sample_refused(self):
        template = r.read_json(self.plan_path.parent / "authorization.template.json")
        self.assertFalse(template["approved"])
        self.assertFalse((self.plan_path.parent / "analysis.sqlite3").exists())
        changed = copy.deepcopy(ITEM)
        changed["updated_at"] = "2026-01-02T00:00:00Z"
        self.sample.write_bytes(r.canonical(changed))
        with self.assertRaises(r.Refused):
            r.inspect_plan(self.plan_path, self.plan_sha)
        self.assertEqual(self.calls, [])

    def test_authorization_defaults_expiry_and_hash_are_fail_closed(self):
        path = self.root / "authorization.json"
        r.exclusive_json(path, self.auth)
        actual = r.digest(path.read_bytes())
        self.assertTrue(r.authorize(self.plan, self.plan_sha, path, actual)["approved"])
        with self.assertRaises(r.Refused):
            r.authorize(self.plan, self.plan_sha, path, "0" * 64)
        expired = {**self.auth, "approved_at": date(time.time() - 20), "expires_at": date(time.time() - 10)}
        other = self.root / "expired.json"
        r.exclusive_json(other, expired)
        with self.assertRaises(r.Refused):
            r.authorize(self.plan, self.plan_sha, other, r.digest(other.read_bytes()))

    def test_real_http_worker_provider_parser_persistence_with_fake_opener(self):
        result = r.run_http(self.plan, self.item, self.auth, FAKE_KEY, self.adapter, offline_opener=self.opener)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["model_post_attempts"], 1)
        self.assertEqual(result["ai"]["calls_reserved"], 1)
        self.assertEqual(result["source_snapshot_sha256"], self.plan["snapshot_sha256"])
        self.assertFalse(result["real_provider"])
        self.assertTrue(result["result"]["semantic_review_required"])
        self.assertEqual(result["result"], r.read_json(self.plan_path.parent / "terminal.json")["result"])
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(r.Refused):
            r.OneShot(self.plan, self.item, self.auth)
        # Ordinary cached result remains visible without constructing a generator.
        runtime = r.SnapshotNews(self.item)
        runtime.ai = r.NewsAnalysis(self.plan_path.parent / "analysis.sqlite3", runtime,
                                   generate=lambda *_: self.fail("must_not_resend"))
        cached, status = r.news_response(runtime, "/api/news/items/" + ITEM["id"])
        self.assertEqual(status, 200)
        self.assertEqual(cached["item"]["ai_analysis"]["status"], "completed")
        self.assertEqual(runtime.ai.request(ITEM["id"])["status"], "completed")
        for path in self.plan_path.parent.iterdir():
            self.assertNotIn(FAKE_KEY.encode(), path.read_bytes())

    def test_timeout_is_spent_unknown_remote_and_no_retry(self):
        def timeout(*args, **kwargs):
            self.calls.append("fake-timeout")
            raise TimeoutError("SYNTHETIC")
        result = r.run_http(self.plan, self.item, self.auth, FAKE_KEY, self.adapter, offline_opener=timeout)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["provider"]["remote_outcome"], "UNKNOWN")
        self.assertEqual(result["model_post_attempts"], 1)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(r.Refused):
            r.OneShot(self.plan, self.item, self.auth)

    def test_durable_receipt_failure_prevents_transport(self):
        original = r.exclusive_json
        def fail_spent(path, value):
            if Path(path).name == "spent.json":
                # Like a crash during write: presence is irreversible, content incomplete.
                Path(path).write_bytes(b"{")
                raise OSError("synthetic_fsync_failure")
            return original(path, value)
        with patch.object(r, "exclusive_json", fail_spent):
            result = r.run_http(self.plan, self.item, self.auth, FAKE_KEY, self.adapter, offline_opener=self.opener)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["model_post_attempts"], 0)
        self.assertEqual(self.calls, [])
        with self.assertRaises(r.Refused):
            r.OneShot(self.plan, self.item, self.auth)

    def test_http_denies_auto_and_wrong_item_concurrent_duplicates_call_once(self):
        guard = r.OneShot(self.plan, self.item, self.auth)
        runtime = r.SnapshotNews(self.item)
        runtime.ai = r.NewsAnalysis(self.plan_path.parent / "analysis.sqlite3", runtime, generate=guard.generate)
        guard.ai, guard.original_open = runtime.ai, self.opener
        self.adapter.open_provider_request = guard.open
        server, token = r.server_for(runtime)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        runtime.ai.start()
        path = "/api/news/items/" + ITEM["id"]
        try:
            status, _ = r.request_local(server, token, "POST", "/api/news/ai/session", {
                "api_key": FAKE_KEY, "automatic": True, "acknowledge_budget": True})
            self.assertEqual(status, 400)
            status, _ = r.request_local(server, token, "POST", "/api/news/items/wrong/analyze", {})
            self.assertEqual(status, 403)
            status, _ = r.request_local(server, token, "POST", "/api/news/ai/session", {
                "api_key": FAKE_KEY, "automatic": False, "acknowledge_budget": True})
            self.assertEqual(status, 200)
            attempts = [threading.Thread(target=r.request_local, args=(server, token, "POST", path + "/analyze", {})) for _ in range(4)]
            for attempt in attempts:
                attempt.start()
            for attempt in attempts:
                attempt.join()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and (runtime.ai.result(self.item) or {}).get("status") not in {"completed", "failed"}:
                time.sleep(.01)
            self.assertEqual(runtime.ai.result(self.item)["status"], "completed")
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(runtime.ai.status()["calls_reserved"], 1)
            with self.assertRaises(r.Refused):
                guard.open(type("Request", (), {"full_url": r.ENDPOINT, "data": guard.expected_wire,
                            "get_method": lambda self: "POST"})(), timeout=45)
        finally:
            runtime.ai.stop(timeout=4)
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
