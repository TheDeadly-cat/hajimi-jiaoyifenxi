from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import threading
import subprocess
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.controlled_manual_review import ControlledManualReview, ControlledReviewError, DISABLED, MODEL, VERSION
from backend.instance_ownership import DatabaseInstanceOwner
from backend.manual_chatgpt import ManualChatGPTService, ManualChatGPTError
from backend.providers.registry import ProviderRegistry
from backend.store import StudioStore
from tests.test_manual_chatgpt import valid_result


class ControlledManualReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-studio-controlled-review-")
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "ChatGPTTrialSynthetic" / "data" / "studio.sqlite3"
        self.database.parent.mkdir(parents=True)
        self.store = StudioStore(self.database)
        self.room = self.store.create_room("Synthetic review", "Offline fixture only")["room"]["id"]
        self.store.add_material(self.room, {"title": "Synthetic evidence", "kind": "note", "content": "Frozen synthetic material."})
        service = ManualChatGPTService(self.store, review_rate_card={})
        session = service.create(self.room, objective="Synthetic standard review only", mode="standard")
        waiting = service.dispatch(self.room, session["id"])
        self.session = service.import_result(self.room, session["id"], json.dumps(valid_result(waiting)))
        self.owner = DatabaseInstanceOwner(self.database).acquire()
        self.addCleanup(self.owner.release)
        self.addCleanup(lambda: self.store.checkpoint_after_shutdown(instance_owner=self.owner))
        self.candidate = self.enterContext(patch("backend.controlled_manual_review.verify_candidate"))
        self.registry = ProviderRegistry(disabled_provider_ids=DISABLED, api_keys={"doubao": "fixture-review-key"})
        self.now = int(time.time() * 1000)
        self.control = self.controller()
        self.config = {"version": VERSION, "pilot_id": "synthetic-review", "candidate_sha": "a" * 40,
                       "database_path": str(self.database), "room_id": self.room, "session_id": self.session["id"],
                       "expected_result_sha256": self.session["result_sha256"], "provider": "doubao", "model": MODEL,
                       "max_output_tokens": 1400, "max_request_bytes": 65536,
                       "not_before_ms": self.now - 1000, "expires_at_ms": self.now + 60_000,
                       "rate_card": {"currency": "CNY", "input_per_million": "6", "output_per_million": "30",
                                     "source_url": "https://docs.volcengine.com/docs/ark/model-pricing", "checked_at": "2026-09-20"},
                       "spend_plan_limit": "0.60"}

    def controller(self):
        return ControlledManualReview(self.store, self.registry, instance_owner=self.owner, clock=lambda: self.now)

    def count(self, table):
        with closing(self.store._connect()) as connection:
            return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def payload(self, request):
        body = json.loads(request.data)
        source = json.loads(body["input"])
        review = {"version": "manual_chatgpt_api_review_v1", "review_kind": source["review_kind"],
                  "verdict": "concern", "summary": "Synthetic remaining uncertainty", "findings": [],
                  "open_questions": ["Synthetic unresolved question"]}
        return {"id": "synthetic-response", "model": MODEL, "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(review)}]}],
                "usage": {"input_tokens": 100, "output_tokens": 50}}

    def transport(self, request, **kwargs):
        self.assertEqual(kwargs, {"timeout": 240})
        return io.BytesIO(json.dumps(self.payload(request)).encode())

    def execute(self, effect=None):
        plan = self.control.prepare(self.config)
        with patch("urllib.request.OpenerDirector.open", side_effect=effect or self.transport) as wire:
            result = self.control.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        return plan, result, wire

    def test_prepare_and_unapproved_execute_are_zero_calls_and_zero_reservations(self):
        with patch("urllib.request.OpenerDirector.open") as wire:
            plan = self.control.prepare(self.config)
            self.assertEqual(plan["max_calls"], 3)
            self.assertEqual(plan["total_output_token_limit"], 4200)
            with self.assertRaises(ControlledReviewError):
                self.control.run(self.config)
        wire.assert_not_called()
        self.assertEqual(self.count("manual_chatgpt_review_runs"), 0)
        self.assertEqual(self.count("provider_call_attempts"), 0)

    def test_exact_three_wire_requests_persist_reviews_and_replay_without_calls(self):
        plan, result, wire = self.execute()
        self.assertEqual(result["session"]["state"], "READY_FOR_DECISION")
        self.assertEqual(result["session"]["api_review"]["completed_calls"], 3)
        self.assertEqual(wire.call_count, 3)
        self.assertEqual(self.count("provider_call_attempts"), 3)
        self.assertEqual(result["session"]["confirmation"], {})
        self.assertTrue(result["session"]["decision_card"]["user_confirmation_required"])
        with closing(self.store._connect()) as connection:
            decision = connection.execute("SELECT selected_option_id,frozen_at FROM manual_chatgpt_decisions").fetchone()
        self.assertEqual(tuple(decision), ("", 0))
        for call, expected in zip(wire.call_args_list, plan["requests"], strict=True):
            request = call.args[0]
            self.assertEqual(hashlib.sha256(request.data).hexdigest(), expected["http_body_sha256"])
            self.assertEqual(json.loads(request.data), expected["http_body"])
            self.assertEqual(request.full_url, plan["endpoint"])
        self.assertFalse(result["supplier_bill_verified"])
        self.assertEqual(len(result["receipts"]), 3)
        with patch("urllib.request.OpenerDirector.open") as replay:
            repeated = self.controller().run(self.config, approved_plan_sha256=plan["plan_sha256"])
        replay.assert_not_called()
        self.assertEqual(repeated["new_http_attempts"], 0)
        self.assertEqual(repeated["receipts"], result["receipts"])

    def test_changed_approval_inputs_fail_before_reserving(self):
        plan = self.control.prepare(self.config)
        for key, value in (("spend_plan_limit", "0.61"), ("expires_at_ms", self.now + 59_999),
                           ("model", "other-model"), ("expected_result_sha256", "b" * 64),
                           ("candidate_sha", "b" * 40), ("max_output_tokens", 1600)):
            with self.subTest(key=key), patch("urllib.request.OpenerDirector.open") as wire:
                changed = {**self.config, key: value}
                with self.assertRaises(ControlledReviewError):
                    self.control.run(changed, approved_plan_sha256=plan["plan_sha256"])
                wire.assert_not_called()
        self.assertEqual(self.count("provider_call_attempts"), 0)

    def test_expired_or_underfunded_plan_has_zero_reservations(self):
        plan = self.control.prepare(self.config)
        self.now = self.config["expires_at_ms"]
        with patch("urllib.request.OpenerDirector.open") as wire:
            with self.assertRaises(ControlledReviewError):
                self.control.run(self.config, approved_plan_sha256=plan["plan_sha256"])
            with self.assertRaises(ControlledReviewError):
                self.control.prepare({**self.config, "spend_plan_limit": "0.0001"})
        wire.assert_not_called()
        self.assertEqual(self.count("provider_call_attempts"), 0)

    def test_actual_http_body_drift_is_rejected_before_transport(self):
        from backend.execution_boundary import text_generation_body
        plan = self.control.prepare(self.config)
        def changed(**kwargs):
            return {**text_generation_body(**kwargs), "max_output_tokens": 1600}
        with patch("backend.providers.doubao_provider.text_generation_body", side_effect=changed), patch("urllib.request.OpenerDirector.open") as wire:
            result = self.control.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        wire.assert_not_called()
        self.assertEqual(self.count("provider_call_attempts"), 1)
        self.assertEqual(result["receipts"][0]["status"], "FAILED")
        self.assertFalse(result["receipts"][0]["http_attempted"])

    def test_expiry_between_reviews_stops_the_next_http_call(self):
        def expire(request, **kwargs):
            response = self.transport(request, **kwargs)
            self.now = self.config["expires_at_ms"]
            return response
        _, result, wire = self.execute(expire)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(self.count("manual_chatgpt_api_reviews"), 1)
        self.assertEqual(self.count("provider_call_attempts"), 2)
        self.assertFalse(result["receipts"][-1]["http_attempted"])

    def test_source_change_during_pre_send_check_cannot_spend(self):
        plan = self.control.prepare(self.config)
        # prepare, run-window, wrapper precheck, actual HTTP pre-send check.
        self.candidate.side_effect = [None, None, None, ControlledReviewError("changed source")]
        with patch("urllib.request.OpenerDirector.open") as wire:
            result = self.control.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        wire.assert_not_called()
        self.assertEqual(result["receipts"][0]["status"], "FAILED")

    def test_remaining_spend_is_checked_before_the_next_request(self):
        def costly(request, **kwargs):
            payload = self.payload(request)
            payload["usage"]["input_tokens"] = 95000
            return io.BytesIO(json.dumps(payload).encode())
        _, result, wire = self.execute(costly)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(result["receipts"][0]["status"], "RESPONDED")
        self.assertFalse(result["receipts"][-1]["http_attempted"])

    def test_timeout_stops_without_retry_or_remaining_reviews(self):
        plan, result, wire = self.execute(TimeoutError("synthetic timeout"))
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(self.count("manual_chatgpt_api_reviews"), 0)
        with patch("urllib.request.OpenerDirector.open") as again:
            self.controller().run(self.config, approved_plan_sha256=plan["plan_sha256"])
        again.assert_not_called()
        self.assertEqual(len(result["receipts"]), 1)

    def test_truncated_response_stops_remaining_reviews(self):
        def incomplete(request, **kwargs):
            payload = self.payload(request)
            payload.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
            return io.BytesIO(json.dumps(payload).encode())
        _, result, wire = self.execute(incomplete)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(self.count("manual_chatgpt_api_reviews"), 0)
        self.assertEqual(result["receipts"][0]["status"], "FAILED")

    def test_invalid_review_contract_stops_remaining_reviews(self):
        def invalid(request, **kwargs):
            payload = self.payload(request)
            payload["output"][0]["content"][0]["text"] = '{"invalid":true}'
            return io.BytesIO(json.dumps(payload).encode())
        _, result, wire = self.execute(invalid)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(self.count("manual_chatgpt_api_reviews"), 0)
        self.assertEqual(result["receipts"][0]["status"], "FAILED")

    def test_wrong_returned_model_stops_remaining_reviews(self):
        def wrong_model(request, **kwargs):
            payload = self.payload(request)
            payload["model"] = "unapproved-model"
            return io.BytesIO(json.dumps(payload).encode())
        _, result, wire = self.execute(wrong_model)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(result["receipts"][0]["error_code"], "model_identity_mismatch")

    def test_unknown_usage_remains_unknown_and_stops(self):
        def missing_usage(request, **kwargs):
            payload = self.payload(request)
            payload.pop("usage")
            return io.BytesIO(json.dumps(payload).encode())
        _, result, wire = self.execute(missing_usage)
        self.assertEqual(wire.call_count, 1)
        self.assertIsNone(result["receipts"][0]["cost_estimate_cny"])
        self.assertEqual(result["receipts"][0]["error_code"], "usage_unknown")

    def test_process_loss_keeps_started_attempt_and_cannot_replay(self):
        plan = self.control.prepare(self.config)
        with patch("urllib.request.OpenerDirector.open", side_effect=SystemExit("synthetic process loss")) as wire:
            with self.assertRaises(SystemExit):
                self.control.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        self.assertEqual(wire.call_count, 1)
        with patch("urllib.request.OpenerDirector.open") as again:
            with self.assertRaises(ManualChatGPTError):
                self.controller().run(self.config, approved_plan_sha256=plan["plan_sha256"])
        again.assert_not_called()
        self.assertEqual(self.count("provider_call_attempts"), 1)

    def test_parallel_duplicate_is_rejected_without_second_batch(self):
        plan = self.control.prepare(self.config)
        entered, release = threading.Event(), threading.Event()
        answers, errors = [], []
        def blocked(request, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.transport(request, **kwargs)
        def first():
            try:
                answers.append(self.control.run(self.config, approved_plan_sha256=plan["plan_sha256"]))
            except BaseException as exc:
                errors.append(exc)
        with patch("urllib.request.OpenerDirector.open", side_effect=blocked) as wire:
            worker = threading.Thread(target=first)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ManualChatGPTError):
                    self.controller().run(self.config, approved_plan_sha256=plan["plan_sha256"])
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(wire.call_count, 3)
        self.assertEqual(answers[0]["session"]["state"], "READY_FOR_DECISION")

    def test_credential_echo_is_not_written_to_receipts(self):
        def echo(request, **kwargs):
            payload = self.payload(request)
            payload["output"][0]["content"][0]["text"] = 'fixture-review-key'
            return io.BytesIO(json.dumps(payload).encode())
        _, result, wire = self.execute(echo)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(result["receipts"][0]["error_code"], "credential_echo_redacted")
        for path in (self.database.parent / "controlled-review-receipts").rglob("*.json"):
            self.assertNotIn("fixture-review-key", path.read_text(encoding="utf-8"))

    def test_expiry_during_source_check_is_rechecked_at_send(self):
        plan = self.control.prepare(self.config)
        checks = []
        def source_check(_sha):
            checks.append(True)
            if len(checks) == 4:
                self.now = self.config["expires_at_ms"]
        self.candidate.side_effect = source_check
        with patch("urllib.request.OpenerDirector.open") as wire:
            result = self.control.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        wire.assert_not_called()
        self.assertFalse(result["receipts"][0]["http_attempted"])

    def test_endpoint_changed_after_request_construction_is_rejected(self):
        from backend.execution_boundary import build_text_provider_request
        plan = self.control.prepare(self.config)
        def changed(*args, **kwargs):
            request = build_text_provider_request(*args, **kwargs)
            request.full_url = "https://unapproved.invalid/responses"
            return request
        with patch("backend.providers.doubao_provider.build_text_provider_request", side_effect=changed), patch("urllib.request.OpenerDirector.open") as wire:
            result = self.control.run(self.config, approved_plan_sha256=plan["plan_sha256"])
        wire.assert_not_called()
        self.assertFalse(result["receipts"][0]["http_attempted"])

    def test_reported_output_over_budget_stops_remaining_calls(self):
        def excess(request, **kwargs):
            payload = self.payload(request)
            payload["usage"]["output_tokens"] = 1401
            return io.BytesIO(json.dumps(payload).encode())
        _, result, wire = self.execute(excess)
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(result["receipts"][0]["error_code"], "reported_usage_exceeds_plan")

    def test_missing_receipt_on_replay_cannot_restart_or_claim_success(self):
        plan, _, _ = self.execute()
        path = self.database.parent / "controlled-review-receipts" / self.session["id"] / plan["plan_sha256"] / "response-2.json"
        path.unlink()
        with patch("urllib.request.OpenerDirector.open") as wire:
            with self.assertRaisesRegex(ControlledReviewError, "回执缺失"):
                self.controller().run(self.config, approved_plan_sha256=plan["plan_sha256"])
        wire.assert_not_called()
        self.assertEqual(self.count("provider_call_attempts"), 3)

    def test_cli_prepare_and_execute_end_to_end_use_owned_synthetic_database(self):
        config_path = self.database.parent.parent / "synthetic-config.json"
        preview_path = self.database.parent.parent / "preview.json"
        result_path = self.database.parent.parent / "result.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.store.checkpoint_after_shutdown(instance_owner=self.owner)
        self.owner.release()
        program = '''
import io, json, os, sys
from pathlib import Path
from unittest.mock import patch
from scripts.run_controlled_manual_review import main
config_path, preview_path, result_path = sys.argv[1:]
config=json.loads(Path(config_path).read_text(encoding='utf-8'))
os.environ['AI_STUDIO_DATABASE_PATH']=config['database_path']
os.environ['AI_STUDIO_RUNTIME_DIR']=str(Path(config['database_path']).parent.parent / 'runtime')
calls = []
def transport(request, **kwargs):
    calls.append(json.loads(request.data))
    source = json.loads(calls[-1]['input'])
    review = dict(version='manual_chatgpt_api_review_v1', review_kind=source['review_kind'], verdict='concern', summary='Synthetic only', findings=[], open_questions=['Synthetic question'])
    payload = dict(id='synthetic', model=calls[-1]['model'], status='completed', output=[dict(type='message', content=[dict(type='output_text', text=json.dumps(review))])], usage=dict(input_tokens=100,output_tokens=50))
    return io.BytesIO(json.dumps(payload).encode())
with patch('backend.controlled_manual_review.verify_candidate'), patch('urllib.request.OpenerDirector.open', side_effect=transport):
    assert main(['--config',config_path,'--prepare','--output',preview_path]) == 0
    assert calls == []
'''
        # Prepare and execute in different processes, as the operator does;
        # each must independently acquire ownership and pass startup checks.
        execute = '''
import io, json, os, sys
from pathlib import Path
from unittest.mock import patch
from scripts.run_controlled_manual_review import main
config_path, preview_path, result_path = sys.argv[1:]
config=json.loads(Path(config_path).read_text(encoding='utf-8'))
os.environ['AI_STUDIO_DATABASE_PATH']=config['database_path']
os.environ['AI_STUDIO_RUNTIME_DIR']=str(Path(config['database_path']).parent.parent / 'runtime')
calls=[]
def transport(request, **kwargs):
    body=json.loads(request.data); calls.append(body)
    source=json.loads(body['input'])
    review=dict(version='manual_chatgpt_api_review_v1',review_kind=source['review_kind'],verdict='concern',summary='Synthetic only',findings=[],open_questions=['Synthetic question'])
    return io.BytesIO(json.dumps(dict(id='synthetic',model=body['model'],status='completed',output=[dict(type='message',content=[dict(type='output_text',text=json.dumps(review))])],usage=dict(input_tokens=100,output_tokens=50))).encode())
plan=json.loads(Path(preview_path).read_text(encoding='utf-8'))
with patch('backend.controlled_manual_review.verify_candidate'), patch('scripts.pilot_password_dialog.ask_api_key',return_value='fixture-review-key') as password, patch('urllib.request.OpenerDirector.open',side_effect=transport):
    assert main(['--config',config_path,'--execute','--approve-plan-sha256',plan['plan_sha256'],'--password-dialog','--output',result_path]) == 0
    assert len(calls)==3
    assert password.call_count==1
    assert [row['http_body'] for row in plan['requests']]==calls
'''
        try:
            for source in (program, execute):
                process = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", source,
                                          str(config_path), str(preview_path), str(result_path)],
                                         capture_output=True, text=True, timeout=30)
                self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
                self.assertNotIn("fixture-review-key", process.stdout + process.stderr)
        finally:
            self.owner.acquire()
        result = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual(result["session"]["state"], "READY_FOR_DECISION")
        self.assertEqual(result["new_http_attempts"], 3)
        self.assertEqual(result["session"]["confirmation"], {})
