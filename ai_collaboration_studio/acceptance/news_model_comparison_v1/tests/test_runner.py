"""Focused synthetic-only ledger, binding, transport and redaction checks."""
from datetime import datetime, timezone, timedelta
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

SOURCE = Path(__file__).resolve().parents[1] / "runner.py"
spec = importlib.util.spec_from_file_location("comparison_runner_tested", SOURCE)
r = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = r
spec.loader.exec_module(r)


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="news-comparison-offline-")
        self.root = Path(self.temp.name)
        self.samples = self.root / "samples.json"
        r.write_new(self.samples, [{"id": "synthetic_%d" % i, "source_id": "fed", "source_name": "Synthetic Fed fixture",
                   "url": "https://www.federalreserve.gov/synthetic/%d" % i, "title": "Synthetic event %d" % i,
                   "excerpt": "Synthetic official announcement number %d for offline testing only." % i,
                   "published_at": "2026-01-01T00:00:00Z", "discovered_at": "2026-01-02T00:00:00Z"} for i in range(5)])
        self.prep = r.prepare(str(self.samples), r.sha(self.samples.read_bytes()), str(self.root / "run"), "2026-01-03T00:00:00Z")
        self.plan_path = self.root / "run/plan.json"
        self.run, self.plan, self.requests, self.provider, self.quality = r.inspect_plan(str(self.plan_path), self.prep["plan_sha256"])
        self.socket_patch = patch.object(socket.socket, "connect", side_effect=AssertionError("real_network_forbidden"))
        self.socket_patch.start()
        self.dns_patch = patch.object(socket, "getaddrinfo", side_effect=AssertionError("real_dns_forbidden"))
        self.dns_patch.start()

    def tearDown(self):
        self.socket_patch.stop()
        self.dns_patch.stop()
        self.temp.cleanup()

    def authorization(self):
        now = datetime.now(timezone.utc)
        return {"schema": "news_model_comparison_authorization_v1", "approved": True,
                "plan_sha256": self.prep["plan_sha256"], "grant_id": self.plan["grant_id"],
                "max_calls": 10, "max_reserved_micro_cny": 1_000_000,
                "approved_at": (now-timedelta(seconds=1)).isoformat(), "expires_at": (now+timedelta(minutes=30)).isoformat(),
                "approval_reference": "Synthetic test only; no live grant."}

    def payload(self, request=None, text=None):
        request = request or self.requests[0]
        output = text or json.dumps({"summary": "合成公告，供离线验证。", "key_points": [{"claim": "这是合成公告。", "evidence_field": "excerpt", "quote": request["material"]["source_text"]["excerpt"]}], "uncertainties": ["只读订阅摘录，未核对全文。"]}, ensure_ascii=False)
        return {"status": "completed", "model": request["model"], "error": None,
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": output}]}],
                "usage": {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150,
                          "input_tokens_details": {"cached_tokens": 10}, "output_tokens_details": {"reasoning_tokens": 0}}}

    def fake_factory(self, raw, calls):
        endpoint = self.provider.ENDPOINTS[self.requests[0]["provider"]]
        class Response:
            status = 200
            def geturl(self): return endpoint
            def read(self, size): return raw[:size]
            def close(self): pass
        class Opener:
            def open(self, request, timeout):
                calls.append((request.full_url, timeout))
                return Response()
        return lambda *_: Opener()

    def run_fake(self, raw, key="SYNTHETIC_SECRET_NEVER_REAL"):
        ledger = r.Ledger(self.run / "comparison.sqlite3")
        calls = []
        with patch.object(r, "_REAL_BUILD_OPENER", self.fake_factory(raw, calls)):
            receipt = r.run_one(self.run, self.requests[0], self.provider, self.quality, key, ledger, {"allowed": False})
        return receipt, calls, ledger

    def test_fixed_pairs_and_same_material(self):
        self.assertEqual(len(self.requests), 10)
        self.assertLessEqual(self.plan["total_reserved_micro_cny"], 1_000_000)
        for i in range(0, 10, 2):
            self.assertEqual(self.requests[i]["body"]["input"], self.requests[i+1]["body"]["input"])
            self.assertEqual(self.requests[i]["body"]["instructions"], self.requests[i+1]["body"]["instructions"])

    def test_frozen_sample_and_code_tamper(self):
        frozen = self.run / "frozen-code/news_content_quality.py"
        frozen.write_bytes(frozen.read_bytes() + b"\n")
        with self.assertRaises(r.Refused): r.inspect_plan(str(self.plan_path), self.prep["plan_sha256"])

    def test_authorization_scope_and_expiry(self):
        auth = self.authorization()
        r.check_authorization(auth, self.plan, self.prep["plan_sha256"])
        auth["max_calls"] = 11
        with self.assertRaises(r.Refused): r.check_authorization(auth, self.plan, self.prep["plan_sha256"])
        auth = self.authorization()
        auth["expires_at"] = (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        with self.assertRaises(r.Refused): r.check_authorization(auth, self.plan, self.prep["plan_sha256"])

    def test_reservation_unique_cross_model_independent(self):
        ledger = r.Ledger(self.run / "comparison.sqlite3")
        ledger.reserve(self.requests[0])
        ledger.reserve(self.requests[1])
        with self.assertRaises(r.Refused): ledger.reserve(self.requests[0])
        with ledger.connect() as db:
            count, amount = db.execute("SELECT COUNT(*),SUM(reserved) FROM attempts").fetchone()
        self.assertEqual(count, 2)
        self.assertEqual(amount, sum(x["reserved_micro_cny"] for x in self.requests[:2]))

    def test_budget_and_call_limits(self):
        ledger = r.Ledger(self.run / "comparison.sqlite3")
        oversized = dict(self.requests[0], reserved_micro_cny=1_000_001)
        with self.assertRaises(r.Refused): ledger.reserve(oversized)
        for request in self.requests: ledger.reserve(request)
        extra = dict(self.requests[0], slot=11, sample_id="eleventh")
        with self.assertRaises(r.Refused): ledger.reserve(extra)

    def test_real_adapter_single_transport_safe_usage(self):
        receipt, calls, ledger = self.run_fake(json.dumps(self.payload()).encode())
        self.assertEqual(len(calls), 1)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["http_status"], 200)
        self.assertEqual(receipt["usage"]["total_tokens"], 150)
        self.assertTrue((self.run / "evidence/01-spent.json").exists())
        with self.assertRaises(r.Refused):
            r.run_one(self.run, self.requests[0], self.provider, self.quality, "SYNTHETIC_SECRET_NEVER_REAL", ledger, {"allowed": False})

    def test_malformed_output_is_spent_without_retry(self):
        receipt, calls, ledger = self.run_fake(json.dumps(self.payload(text="malformed { text")).encode())
        self.assertEqual(receipt["status"], "quality_rejected")
        self.assertEqual(len(calls), 1)
        with ledger.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0], 1)

    def test_key_echo_not_persisted(self):
        key = "SYNTHETIC_SECRET_NEVER_REAL"
        receipt, calls, _ = self.run_fake(json.dumps(self.payload(text=key)).encode(), key)
        self.assertEqual(receipt["status"], "unknown")
        self.assertIsNone(receipt["output_text"])
        for p in self.run.rglob("*"):
            if p.is_file(): self.assertNotIn(key.encode(), p.read_bytes())

    def test_json_escaped_other_batch_key_not_persisted(self):
        other_key = "SECOND_SYNTHETIC_SECRET"
        payload = self.payload()
        decoded = json.loads(payload["output"][0]["content"][0]["text"])
        decoded["summary"] = other_key
        encoded_key = "".join("\\u%04x" % ord(char) for char in other_key)
        payload["output"][0]["content"][0]["text"] = json.dumps(decoded).replace(other_key, encoded_key)
        ledger = r.Ledger(self.run / "comparison.sqlite3")
        calls = []
        with patch.object(r, "_REAL_BUILD_OPENER", self.fake_factory(json.dumps(payload).encode(), calls)):
            receipt = r.run_one(self.run, self.requests[0], self.provider, self.quality,
                               "FIRST_SYNTHETIC_SECRET", ledger, {"allowed": False},
                               ("FIRST_SYNTHETIC_SECRET", other_key))
        self.assertNotEqual(receipt["status"], "completed")
        self.assertIsNone(receipt["output_text"])
        self.assertNotIn("validated_result", receipt)
        self.assertEqual(len(calls), 1)
        for p in self.run.rglob("*"):
            if p.is_file():
                self.assertNotIn(other_key.encode(), p.read_bytes())
                self.assertNotIn(encoded_key.encode(), p.read_bytes())

    def test_numeric_http_error_no_error_body(self):
        calls = []
        class Opener:
            def open(inner, request, timeout):
                calls.append(1)
                raise urllib.error.HTTPError(request.full_url, 401, "SECRET_ERROR_TEXT", {}, io.BytesIO(b"SECRET_ERROR_BODY"))
        ledger = r.Ledger(self.run / "comparison.sqlite3")
        with patch.object(r, "_REAL_BUILD_OPENER", lambda *_: Opener()):
            receipt = r.run_one(self.run, self.requests[0], self.provider, self.quality, "SYNTHETIC_SECRET_NEVER_REAL", ledger, {"allowed": False})
        self.assertEqual(receipt["http_status"], 401)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(len(calls), 1)
        for p in self.run.rglob("*"):
            if p.is_file(): self.assertNotIn(b"SECRET_ERROR", p.read_bytes())

    def test_started_marker_denies_execute_replay_before_prompt(self):
        auth = self.authorization()
        authpath = self.run / "auth.json"
        r.write_new(authpath, auth)
        r.write_new(self.run / "started.json", {"synthetic": True})
        with self.assertRaises(r.Refused):
            r.execute(str(self.plan_path), self.prep["plan_sha256"], str(authpath), r.sha(authpath.read_bytes()), True)

    def test_foreign_database_guard(self):
        # Capture the installed audit hook and invoke only that callback; never
        # install process-global guards into the test framework itself.
        hooks = []
        with patch.object(sys, "addaudithook", hooks.append): r.install_guard(self.run, {"allowed": False})
        with self.assertRaises(r.Refused): hooks[0]("sqlite3.connect", (str(self.root / "production.sqlite3"),))
        with self.assertRaises(r.Refused): hooks[0]("socket.connect", ())
        with self.assertRaises(r.Refused): hooks[0]("open", (str(self.root / "outside.txt"), "w", 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
