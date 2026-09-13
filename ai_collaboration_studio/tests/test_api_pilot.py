from __future__ import annotations

import copy
import io
import json
import urllib.error
from unittest.mock import patch

from backend.api_pilot import ControlledAPIPilot, PilotError, RESPONSE_VERSION, SCOPE
from backend.instance_ownership import DatabaseInstanceOwner
from backend.manual_chatgpt import ManualChatGPTService
from backend.providers.registry import ProviderRegistry
from backend.store import StudioStore
from tests.test_document_selection import DocumentSelectionFixture


class ControlledPilotTests(DocumentSelectionFixture):
    def setUp(self):
        super().setUp()
        self.material = self.save()["material"]
        self.session = ManualChatGPTService(self.store).create(self.room_id, objective="Synthetic pilot fixture only.", mode="quick")
        self.owner = DatabaseInstanceOwner(self.store.path).acquire()
        self.registry = ProviderRegistry(disabled_provider_ids={"openai", "doubao", "glm"}, api_keys={"deepseek": "fixture-key"})
        self.pilot = ControlledAPIPilot(self.store, self.registry, instance_owner=self.owner, clock=lambda: self.now)
        self.config = {"version": "api_controlled_pilot_v1", "pilot_id": "fixture-pilot", "candidate_sha": "a" * 40,
                       "database_path": str(self.store.path), "room_id": self.room_id, "session_id": self.session["id"],
                       "provider": "deepseek", "model": "fixture-model", "accepted_response_models": ["fixture-model"],
                       "max_output_tokens": 900, "max_request_bytes": 65_536,
                       "not_before_ms": self.now - 1000, "expires_at_ms": self.now + 60_000,
                       "rate_card": {"currency": "CNY", "input_per_million": "1", "output_per_million": "2",
                                     "source_url": "https://api-docs.deepseek.com/quick_start/pricing", "checked_at": "synthetic fixture price, not a live quote"},
                       "spend_plan_limit": "10"}

    def tearDown(self):
        if self.owner.held:
            self.store.checkpoint_after_shutdown(instance_owner=self.owner)
            self.owner.release()
        super().tearDown()

    def answer(self):
        return {"version": RESPONSE_VERSION,
                "facts": [{"claim": "Synthetic fixture fact.", "evidence_id": self.material["id"], "quote": self.material["content"][-30:]}],
                "inferences": [], "unknowns": ["Only selected fixture evidence is available."],
                "sector_impact_supported": False, "scope": SCOPE}

    def provider_payload(self, **changes):
        return {"id": "fixture-response", "model": "fixture-model",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.answer())}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}, **changes}

    def run_with_payload(self, payload):
        plan = self.pilot.prepare(self.config)
        with patch("urllib.request.OpenerDirector.open", return_value=io.BytesIO(json.dumps(payload).encode())) as transport:
            result = self.pilot.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        return plan, result, transport

    def test_preparation_and_missing_approval_do_not_reserve_or_call(self):
        with patch("urllib.request.OpenerDirector.open") as transport, patch("urllib.request.urlopen") as ordinary:
            plan = self.pilot.prepare(self.config)
            with self.assertRaises(PilotError):
                self.pilot.run(self.config)
        transport.assert_not_called(); ordinary.assert_not_called()
        self.assertEqual(plan["max_calls"], 1)
        self.assertEqual(plan["additional_calls_authorized"], 0)
        self.assertEqual(self.count("provider_call_attempts"), 0)
        self.assertEqual(self.count("provider_execution_runs"), 0)
        self.fetcher.assert_not_called()

    def test_exact_wire_request_result_usage_and_restart_replay(self):
        plan, result, transport = self.run_with_payload(self.provider_payload())
        self.assertEqual(result["response"]["status"], "RESPONDED")
        self.assertEqual(result["response"]["usage"], {"input_tokens": 100, "output_tokens": 50})
        self.assertEqual(result["response"]["actual_model"], "fixture-model")
        self.assertEqual(result["response"]["response_id"], "fixture-response")
        self.assertFalse(result["response"]["supplier_bill_verified"])
        request = transport.call_args.args[0]
        self.assertEqual(json.loads(request.data), plan["http_body"])
        self.assertEqual(request.full_url, plan["endpoint"])
        self.assertEqual(transport.call_args.kwargs["timeout"], plan["timeout_seconds"])
        self.assertEqual(result["run"]["reserved_calls"], 1)
        self.assertEqual(transport.call_count, 1)
        self.store.checkpoint_after_shutdown(instance_owner=self.owner)
        self.owner.release()
        self.owner = DatabaseInstanceOwner(self.store.path).acquire()
        restarted_store = StudioStore._open_existing_schema(self.store.path)
        restarted = ControlledAPIPilot(restarted_store, self.registry, instance_owner=self.owner, clock=lambda: self.now)
        with patch("urllib.request.OpenerDirector.open") as again:
            replay = restarted.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        again.assert_not_called()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["response"], result["response"])
        self.assertEqual(self.count("materials"), 1)
        self.assertEqual(self.count("rounds"), 0)
        self.assertEqual(ManualChatGPTService(self.store).get(self.room_id, self.session["id"])["state"], "BUNDLE_READY")

    def test_changed_budget_expired_window_and_oversize_input_stop_before_call(self):
        plan = self.pilot.prepare(self.config)
        with patch("urllib.request.OpenerDirector.open") as transport:
            changed = {**self.config, "max_output_tokens": 901}
            with self.assertRaises(PilotError):
                self.pilot.run(changed, approved_plan_sha256=plan["plan_sha256"])
            for changes in [{"max_request_bytes": 1}, {"spend_plan_limit": "0.000000001"}, {"max_output_tokens": True}]:
                with self.assertRaises(PilotError):
                    self.pilot.prepare({**self.config, **changes})
            expired = {**self.config, "not_before_ms": self.now - 1000, "expires_at_ms": self.now - 1}
            expired_plan = self.pilot.prepare(expired)
            with self.assertRaises(PilotError):
                self.pilot.run(expired, approved_plan_sha256=expired_plan["plan_sha256"])
        transport.assert_not_called()
        self.assertEqual(self.count("provider_call_attempts"), 0)

    def test_official_endpoint_and_production_registry_policies_cannot_be_bypassed(self):
        with patch("urllib.request.OpenerDirector.open") as transport:
            unrestricted = ControlledAPIPilot(self.store, ProviderRegistry(), instance_owner=self.owner)
            with self.assertRaises(PilotError):
                unrestricted.prepare(self.config)
            injected = ProviderRegistry({"deepseek": self.registry.get("deepseek")}, disabled_provider_ids={"openai", "doubao", "glm"})
            with self.assertRaises(PilotError):
                ControlledAPIPilot(self.store, injected, instance_owner=self.owner).prepare(self.config)
            openai_config = {**self.config, "provider": "openai"}
            openai_registry = ProviderRegistry(disabled_provider_ids={"deepseek", "doubao", "glm"}, api_keys={"openai": "fixture-key"})
            with self.assertRaises(PilotError):
                ControlledAPIPilot(self.store, openai_registry, instance_owner=self.owner).prepare(openai_config)
            with patch.object(self.registry.get("deepseek"), "_base_url", "https://unapproved.example"):
                with self.assertRaises(PilotError):
                    self.pilot.prepare(self.config)
        transport.assert_not_called()

    def test_401_429_and_timeout_are_permanent_attempts_without_retry(self):
        for i, status in enumerate([401, 429, "timeout"]):
            self.config["pilot_id"] = f"failure-{i}"
            plan = self.pilot.prepare(self.config)
            error = TimeoutError() if status == "timeout" else urllib.error.HTTPError(plan["endpoint"], status, "fixture", {}, io.BytesIO(b'{}'))
            with patch("urllib.request.OpenerDirector.open", side_effect=error) as transport:
                failed = self.pilot.run(self.config, approved_plan_sha256=plan["plan_sha256"])
                replay = self.pilot.run(self.config, approved_plan_sha256=plan["plan_sha256"])
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(failed["response"]["status"], "FAILED")
            self.assertEqual(failed["response"]["usage_status"], "unknown")
            self.assertIsNone(failed["response"]["cost_estimate"])
            self.assertEqual(failed["run"]["reserved_calls"], 1)
            self.assertTrue(replay["replayed"])

    def test_missing_usage_is_unknown_and_missing_actual_model_is_invalid(self):
        plan, result, _ = self.run_with_payload(self.provider_payload(usage=None))
        self.assertEqual(result["response"]["status"], "RESPONDED")
        self.assertEqual(result["response"]["usage_status"], "unknown")
        self.assertIsNone(result["response"]["cost_estimate"])
        self.config["pilot_id"] = "missing-model"
        payload = self.provider_payload(); payload.pop("model")
        _, invalid, _ = self.run_with_payload(payload)
        self.assertEqual(invalid["response"]["status"], "INVALID")
        self.assertEqual(invalid["response"]["error_code"], "model_identity_mismatch")

    def test_invalid_json_nonexistent_citation_and_partial_reply_fail_without_repair(self):
        answer = self.answer(); answer["facts"][0]["evidence_id"] = "not-present"
        for i, (content, finish) in enumerate([("{broken", "stop"), (json.dumps(answer), "stop"), (json.dumps(self.answer()), "length")]):
            self.config["pilot_id"] = f"invalid-{i}"
            payload = self.provider_payload(choices=[{"finish_reason": finish, "message": {"content": content}}])
            _, result, transport = self.run_with_payload(payload)
            self.assertNotEqual(result["response"]["status"], "RESPONDED")
            self.assertEqual(transport.call_count, 1)
            self.assertEqual(result["run"]["reserved_calls"], 1)

    def test_wire_drift_is_rejected_before_transport(self):
        from backend.execution_boundary import text_generation_body
        plan = self.pilot.prepare(self.config)
        def drift(**kwargs):
            body = text_generation_body(**kwargs); body["max_tokens"] = 3200; return body
        with patch("backend.providers.compatible_chat_provider.text_generation_body", side_effect=drift), patch("urllib.request.OpenerDirector.open") as transport:
            result = self.pilot.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        transport.assert_not_called()
        self.assertFalse(result["response"]["http_attempted"])
        self.assertEqual(result["run"]["reserved_calls"], 1)

    def test_lost_response_record_never_authorizes_another_attempt(self):
        plan = self.pilot.prepare(self.config)
        from backend.api_pilot import write_record
        def fail_response(path, value):
            if path.name == "response.json":
                raise OSError("fixture storage failure")
            return write_record(path, value)
        with patch("backend.api_pilot.write_record", side_effect=fail_response), patch("urllib.request.OpenerDirector.open", return_value=io.BytesIO(json.dumps(self.provider_payload()).encode())) as transport:
            with self.assertRaises(OSError):
                self.pilot.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        self.assertEqual(transport.call_count, 1)
        with patch("urllib.request.OpenerDirector.open") as again:
            replay = self.pilot.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        again.assert_not_called()
        self.assertTrue(replay["outcome_unknown"])
        self.assertEqual(replay["attempts"][0]["status"], "STARTED")

    def test_redirect_is_refused_before_credentials_can_reach_another_endpoint(self):
        import email.message
        import urllib.request
        import urllib.response
        from backend.execution_boundary import _NoProviderRedirect
        seen = []
        class RedirectingFixture(urllib.request.HTTPSHandler):
            def https_open(self, request):
                seen.append(request.full_url)
                headers = email.message.Message()
                headers["Location"] = "https://unapproved.example/collect"
                response = urllib.response.addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
                return response
        opener = urllib.request.build_opener(_NoProviderRedirect(), RedirectingFixture())
        plan = self.pilot.prepare(self.config)
        with patch("urllib.request.build_opener", return_value=opener):
            result = self.pilot.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        self.assertEqual(seen, [plan["endpoint"]])
        self.assertNotEqual(result["response"]["status"], "RESPONDED")
        self.assertEqual(result["run"]["reserved_calls"], 1)

    def test_one_authorization_cannot_open_a_second_http_request(self):
        import hashlib
        from backend.execution_boundary import AuthorizedTextRequest, ExecutionBoundaryViolation, authorized_text_request, build_text_provider_request, open_text_provider_request
        plan = self.pilot.prepare(self.config)
        request = build_text_provider_request("https://api.deepseek.com", "chat_completions", plan["http_body"], headers={})
        policy = AuthorizedTextRequest(plan["endpoint"], hashlib.sha256(request.data).hexdigest(), self.config["max_request_bytes"], plan["timeout_seconds"])
        with patch("urllib.request.OpenerDirector.open", return_value=io.BytesIO(b"{}")) as transport, authorized_text_request(policy):
            open_text_provider_request(request, timeout=plan["timeout_seconds"]).close()
            with self.assertRaises(ExecutionBoundaryViolation):
                open_text_provider_request(request, timeout=plan["timeout_seconds"])
        self.assertEqual(transport.call_count, 1)

    def test_reported_output_above_approved_limit_is_not_accepted(self):
        _, result, _ = self.run_with_payload(self.provider_payload(usage={"prompt_tokens": 100, "completion_tokens": 901}))
        self.assertEqual(result["response"]["status"], "INVALID")
        self.assertEqual(result["response"]["error_code"], "reported_usage_exceeds_plan")
        self.assertEqual(result["response"]["usage"]["output_tokens"], 901)

    def test_echoed_credential_is_not_written_into_any_response_record(self):
        payload = self.provider_payload(id="fixture-key", choices=[{"finish_reason": "stop", "message": {"content": "fixture-key"}}])
        _, result, _ = self.run_with_payload(payload)
        self.assertEqual(result["response"]["error_code"], "credential_echo_redacted")
        self.assertNotIn("fixture-key", json.dumps(result))
        for path in (self.store.path.parent / "pilot-receipts").rglob("*.json"):
            self.assertNotIn("fixture-key", path.read_text(encoding="utf-8"))

    def test_original_version_is_immutable_and_forged_material_metadata_is_rejected(self):
        from contextlib import closing
        import sqlite3
        with closing(self.store._connect()) as connection, connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE source_document_versions SET record_sha256=? WHERE id=?", ("0" * 64, self.version["id"]))
        corrupted = copy.deepcopy(self.material)
        corrupted["metadata"]["document_selection"]["body_text_sha256"] = "0" * 64
        with patch.object(self.store, "get_material", return_value=corrupted), patch("urllib.request.OpenerDirector.open") as transport:
            with self.assertRaises(PilotError):
                self.pilot.prepare(self.config)
        transport.assert_not_called()
