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
                "max_calls": 10, "max_reserved_micro_cny": self.plan["max_reserved_micro_cny"],
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

    def test_frozen_boundary_is_bound_and_not_loaded_from_ambient_backend(self):
        frozen = self.run / "frozen-code/execution_boundary.py"
        self.assertEqual(self.plan["frozen_code_sha256"]["execution_boundary.py"], r.sha(frozen.read_bytes()))
        function = self.provider.build_text_provider_request
        self.assertEqual(Path(sys.modules[function.__module__].__file__).resolve(), frozen.resolve())
        self.assertEqual(sys.modules[self.provider.__package__].__path__, [])
        frozen.write_bytes(frozen.read_bytes() + b"\n# tampered\n")
        with self.assertRaisesRegex(r.Refused, "code_hash_mismatch"):
            r.inspect_plan(str(self.plan_path), self.prep["plan_sha256"])

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

    def test_exact_batch_cap_binds_plan_template_and_all_slots(self):
        cap = self.plan["total_reserved_micro_cny"]
        output = self.root / "exact-cap"
        prepared = r.prepare(str(self.samples), r.sha(self.samples.read_bytes()), str(output),
                             "2026-01-03T00:00:00Z", budget_micro_cny=cap)
        _, plan, requests, *_ = r.inspect_plan(str(output / "plan.json"), prepared["plan_sha256"])
        self.assertEqual(plan["max_reserved_micro_cny"], cap)
        self.assertEqual(r.read_json(output / "authorization.template.json")["max_reserved_micro_cny"], cap)
        self.assertEqual([x["body_sha256"] for x in requests], [x["body_sha256"] for x in self.requests])
        ledger = r.Ledger(output / "comparison.sqlite3", budget_micro_cny=cap)
        for request in requests:
            ledger.reserve(request)
        with ledger.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*),SUM(reserved) FROM attempts").fetchone(), (10, cap))

    def test_prepare_and_inspect_reject_invalid_or_insufficient_caps(self):
        for index, cap in enumerate((True, False, None, 0, -1, 452154.0, "452154", 1_000_001)):
            with self.subTest(cap=cap), self.assertRaises(r.Refused):
                r.prepare(str(self.samples), r.sha(self.samples.read_bytes()), str(self.root / f"invalid-{index}"),
                          "2026-01-03T00:00:00Z", budget_micro_cny=cap)
        insufficient = self.plan["total_reserved_micro_cny"] - 1
        with self.assertRaisesRegex(r.Refused, "batch_budget_exceeded"):
            r.prepare(str(self.samples), r.sha(self.samples.read_bytes()), str(self.root / "too-low"),
                      "2026-01-03T00:00:00Z", budget_micro_cny=insufficient)
        for cap in (True, 452154.0, insufficient):
            modified = dict(self.plan, max_reserved_micro_cny=cap)
            self.plan_path.write_bytes(r.canonical(modified) + b"\n")
            with self.subTest(inspect_cap=cap), self.assertRaises(r.Refused):
                r.inspect_plan(str(self.plan_path), r.sha(self.plan_path.read_bytes()))

    def test_authorization_requires_exact_integer_cap_and_sufficient_total(self):
        plan = dict(self.plan, max_reserved_micro_cny=452154)
        self.assertLessEqual(plan["total_reserved_micro_cny"], 452154)
        auth = dict(self.authorization(), max_reserved_micro_cny=452154)
        r.check_authorization(auth, plan, self.prep["plan_sha256"])
        for cap in (1_000_000, 452153, 452155, True, False, None, 0, -1, 452154.0, "452154"):
            with self.subTest(cap=cap), self.assertRaises(r.Refused):
                r.check_authorization(dict(auth, max_reserved_micro_cny=cap), plan, self.prep["plan_sha256"])
        for total in (452155, 0, -1, True, 452154.0):
            with self.subTest(total=total), self.assertRaises(r.Refused):
                r.check_authorization(auth, dict(plan, total_reserved_micro_cny=total), self.prep["plan_sha256"])

    def test_ledger_exact_cap_rejects_invalid_amounts_and_never_refunds_failure(self):
        cap = 452154
        ledger = r.Ledger(self.run / "comparison.sqlite3", budget_micro_cny=cap)
        for amount in (None, True, False, 0, -1, 1.0, "1", cap + 1):
            with self.subTest(amount=amount), self.assertRaises(r.Refused):
                ledger.reserve(dict(self.requests[0], reserved_micro_cny=amount))
        request = dict(self.requests[0], reserved_micro_cny=cap)
        ledger.reserve(request)
        extra = dict(self.requests[1], reserved_micro_cny=1)
        for status in ("reserved_unknown", "unknown", "failed"):
            if status != "reserved_unknown":
                ledger.finish(request, {"status": status})
            with self.subTest(status=status), self.assertRaisesRegex(r.Refused, "ledger_limit"):
                ledger.reserve(extra)
            with ledger.connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*),SUM(reserved) FROM attempts").fetchone(), (1, cap))

    def test_execute_records_exact_cap_and_refuses_replay_after_all_fake_slots(self):
        cap = self.plan["total_reserved_micro_cny"]
        output = self.root / "execute-exact-cap"
        prepared = r.prepare(str(self.samples), r.sha(self.samples.read_bytes()), str(output),
                             "2026-01-03T00:00:00Z", budget_micro_cny=cap)
        _, plan, requests, *_ = r.inspect_plan(str(output / "plan.json"), prepared["plan_sha256"])
        auth = dict(self.authorization(), grant_id=plan["grant_id"], plan_sha256=prepared["plan_sha256"],
                    max_reserved_micro_cny=cap)
        auth_path = output / "authorization.json"
        r.write_new(auth_path, auth)
        prompt_caps, opens = [], []
        def ask_keys(_expires, *, max_reserved_micro_cny):
            prompt_caps.append(max_reserved_micro_cny)
            return {"doubao": "FIRST_SYNTHETIC_KEY", "deepseek": "SECOND_SYNTHETIC_KEY"}
        credentials = SimpleNamespace(ask_keys=ask_keys, PromptCancelled=type("Cancelled", (Exception,), {}),
            run_with_progress=lambda work: work(threading.Event(), lambda _: None))
        load_module = r.module
        def load(p, name):
            return credentials if name == "comparison_credentials" else load_module(p, name)
        by_hash = {request["body_sha256"]: request for request in requests}
        outer = self
        class Opener:
            def open(self, request, timeout):
                opens.append((request.full_url, timeout))
                frozen_request = by_hash[r.sha(request.data)]
                raw = json.dumps(outer.payload(frozen_request)).encode()
                return SimpleNamespace(status=200, geturl=lambda: request.full_url,
                                       read=lambda size: raw[:size], close=lambda: None)
        with patch.object(r, "module", load), patch.object(r, "install_guard"), patch.object(
            r, "_REAL_BUILD_OPENER", lambda *_: Opener()
        ):
            terminal = r.execute(str(output / "plan.json"), prepared["plan_sha256"],
                                 str(auth_path), r.sha(auth_path.read_bytes()), True)
            with self.assertRaisesRegex(r.Refused, "plan_already_started_no_replay"):
                r.execute(str(output / "plan.json"), prepared["plan_sha256"],
                          str(auth_path), r.sha(auth_path.read_bytes()), True)
        self.assertEqual(prompt_caps, [cap])
        self.assertEqual(len(opens), 10)
        self.assertTrue(all(timeout == 45 for _, timeout in opens))
        self.assertEqual(terminal["status"], "completed_all_slots")
        self.assertEqual(terminal["recorded_reserved_micro_cny"], cap)
        self.assertEqual(terminal["max_reserved_micro_cny"], cap)
        self.assertEqual(r.read_json(output / "started.json")["max_reserved_micro_cny"], cap)

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
