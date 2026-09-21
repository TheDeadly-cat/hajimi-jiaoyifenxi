"""Native pipeline tests: real store/parser/provider protocol, synthetic transport."""
import copy
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from backend.document_evidence import DocumentEvidenceService, RECHECK_MS
from backend.instance_ownership import DatabaseInstanceOwner
from backend.material_ingest import FetchedResource
from backend.news_review_contracts import (DISABLED, ENDPOINT, MODEL, PROFILE, SOURCES, STRATEGY_SHA,
                                           VERSION, NewsReviewError, validate_policy)
from backend.news_review_service import NewsReviewService, item_review_projection
from backend.providers.registry import ProviderRegistry
from backend.source_inbox_service import SourceInboxService
from backend.store import StudioStore
from tests.test_document_evidence import SEC_HTML, NOW, import_event


class NewsReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="news-review-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"NewsReviewTrialSynthetic"/"data"/"studio.sqlite3"
        self.path.parent.mkdir(parents=True)
        self.store = StudioStore(self.path)
        self.owner = DatabaseInstanceOwner(self.path).acquire()
        self.addCleanup(self.owner.release)
        self.addCleanup(lambda: self.store.checkpoint_after_shutdown(instance_owner=self.owner))
        self.room = self.store.create_room("Native news fixture", "Offline only")["room"]["id"]
        self.now = NOW
        self.raw = SEC_HTML
        self.fetcher = Mock(side_effect=lambda source, **kw: FetchedResource(self.raw,"text/html",source["url"]))
        self.documents = DocumentEvidenceService(self.store,fetcher=self.fetcher,clock=lambda:self.now)
        self.providers = ProviderRegistry(disabled_provider_ids=DISABLED,api_keys={"doubao":"fixture-news-key"})
        self.candidate = self.enterContext(patch("backend.news_review_service.verify_candidate"))
        self.service = NewsReviewService(self.store,self.providers,instance_owner=self.owner,
                                         documents=self.documents,clock=lambda:self.now,monotonic_ms=lambda:self.now)
        self.policy = {"version":VERSION,"policy_id":"synthetic-news","candidate_sha":"a"*40,
                       "database_path":str(self.path),"room_id":self.room,"profile":PROFILE,"sources":SOURCES,
                       "poll_interval_ms":300_000,"initialization":"seed_only","strategy_sha256":STRATEGY_SHA,
                       "provider":"doubao","model":MODEL,"endpoint":ENDPOINT,"not_before_ms":self.now,
                       "expires_at_ms":self.now+86_400_000,"max_document_requests":6,"max_model_calls":3,
                       "max_request_bytes":65536,"max_output_tokens":1400,"max_total_tokens":200_000,
                       "spend_limit_cny":"3.00","rate_card":{"currency":"CNY","input_per_million":"3",
                       "output_per_million":"9","source_url":"https://www.volcengine.com/docs/82379/1544106",
                       "checked_at":"synthetic fixture, not an actual price"},"concurrency":1,
                       "resume_within_window":True,"stop_on_unknown":True}
        self.cancel = threading.Event()

    def approve(self):
        preview = self.service.preview(self.policy)
        return self.service.approve(self.policy,approved_policy_sha256=preview["policy_sha256"])

    def prepare(self, index=1):
        self.approve()
        item = import_event(self.store,index)
        self.service.discover(self.policy["policy_id"])
        job = self.service.request_document(self.policy["policy_id"],item)
        self.documents.run(job,cancel_event=self.cancel,deadline_monotonic_ms=int(time.monotonic()*1000)+12_000)
        return item, self.service.queue(self.policy["policy_id"],item)

    def payload(self, request):
        body = json.loads(request.data)
        source = json.loads(body["input"])
        paragraph = source["document"]["paragraphs"][1]
        result = {"version":VERSION,"assessment":"reviewed","summary":"原文陈述了季度收入；附件仍未读取。",
                  "importance":{"level":"high","reason":"涉及经营结果"},
                  "facts":[{"claim":"公告给出本季度收入","paragraph_id":paragraph["id"],"quote":paragraph["text"]}],
                  "inferences":[],"counterevidence":[],"open_questions":["附件是否包含其他重大限制？"],
                  "limitations":["仅审核已读取的主 HTML，未独立核验事实或附件。"]}
        return {"id":"synthetic-response","model":MODEL,"status":"completed",
                "output":[{"type":"message","content":[{"type":"output_text","text":json.dumps(result)}]}],
                "usage":{"input_tokens":100,"output_tokens":50}}

    def transport(self, request, **kwargs):
        self.assertEqual(request.full_url,ENDPOINT)
        self.assertEqual(kwargs,{"timeout":240})
        return io.BytesIO(json.dumps(self.payload(request)).encode())

    def run_one(self, effect=None):
        with patch("urllib.request.OpenerDirector.open",side_effect=effect or self.transport) as wire:
            self.service.run_one(self.policy["policy_id"])
        return wire

    def count(self, table):
        with closing(self.store._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM "+table).fetchone()[0]

    def view(self, item):
        return item_review_projection(self.store,item,clock=lambda:self.now)

    def test_native_end_to_end_and_replay_preserve_inbox_and_no_manual_session(self):
        item, job = self.prepare()
        before = SourceInboxService(self.store).get_item(item)
        self.assertEqual(self.view(item)["state"],"QUEUED")
        self.assertEqual(self.run_one().call_count,1)
        view = self.view(item)
        self.assertEqual(view["state"],"REVIEWED")
        self.assertEqual(view["claim_status"],"external_unverified")
        self.assertEqual(view["reviews"][0]["coverage"]["attachment_reading"],"not_read")
        receipt = view["reviews"][0]["receipt"]
        self.assertEqual(receipt["usage"],{"input_tokens":100,"output_tokens":50})
        self.assertFalse(receipt["supplier_bill_verified"])
        self.assertEqual(self.service.queue(self.policy["policy_id"],item),job)
        self.assertEqual(self.run_one().call_count,0)
        self.assertEqual(self.count("provider_call_attempts"),1)
        self.assertEqual(self.count("manual_chatgpt_sessions"),0)
        self.assertEqual(SourceInboxService(self.store).get_item(item),before)
        self.assertEqual(self.view(item),view)

    def test_no_backfill_and_no_fabricated_new_events(self):
        item = import_event(self.store)
        self.approve()
        self.service.discover(self.policy["policy_id"])
        self.assertEqual(self.service.snapshot(self.policy["policy_id"])["observation"],"no_new_event_observed")
        self.assertEqual(self.view(item)["state"],"UNREVIEWED")
        self.assertEqual(self.run_one().call_count,0)

    def test_real_body_revision_can_queue_but_html_decoration_cannot_charge_twice(self):
        item, first = self.prepare()
        self.run_one()
        for raw, expected in ((SEC_HTML+b"<!-- decoration -->",first),
                              (SEC_HTML.replace(b"$10 million",b"$11 million"),None)):
            self.now += RECHECK_MS+1
            self.raw = raw
            document = self.documents.request(item,session_id="fixture-manual",confirmation=True,refresh=True)
            self.documents.run(document["id"],cancel_event=self.cancel,deadline_monotonic_ms=int(time.monotonic()*1000)+12_000)
            job = self.service.queue(self.policy["policy_id"],item)
            if expected:
                self.assertEqual(job,expected)
                self.assertEqual(self.run_one().call_count,0)
            else:
                self.assertNotEqual(job,first)
                self.assertEqual(self.run_one().call_count,1)
        self.assertEqual(self.count("provider_call_attempts"),2)
        self.assertEqual(self.count("news_review_receipts"),2)
        self.assertEqual(len(self.view(item)["reviews"]),2)

    def test_missing_body_and_oversized_body_are_material_insufficient_without_calls(self):
        self.policy["max_request_bytes"] = 1024
        item, _ = self.prepare()
        self.assertEqual(self.view(item)["state"],"MATERIAL_INSUFFICIENT")
        self.assertEqual(self.run_one().call_count,0)
        self.assertEqual(self.count("provider_call_attempts"),0)

    def test_repaired_html_closure_can_be_reviewed_without_recharging_same_text(self):
        self.raw = SEC_HTML.replace(b"</body></html>",b"")
        item,job = self.prepare()
        self.assertIsNone(job)
        self.assertEqual(self.view(item)["state"],"MATERIAL_INSUFFICIENT")
        self.assertEqual(self.run_one().call_count,0)
        self.now += RECHECK_MS+1
        self.raw = SEC_HTML
        document = self.documents.request(item,session_id="fixture-manual",confirmation=True,refresh=True)
        self.documents.run(document["id"],cancel_event=self.cancel,deadline_monotonic_ms=int(time.monotonic()*1000)+12_000)
        self.assertIsNotNone(self.service.queue(self.policy["policy_id"],item))
        self.assertEqual(self.run_one().call_count,1)
        self.assertEqual(self.run_one().call_count,0)

    def test_failed_document_is_not_no_important_news(self):
        self.fetcher.side_effect = TimeoutError("synthetic")
        item, job = self.prepare()
        self.assertIsNone(job)
        self.assertEqual(self.view(item)["state"],"MATERIAL_INSUFFICIENT")
        self.assertTrue(self.view(item)["importance"]["review_required"])

    def test_timeout_is_unknown_and_never_retried_after_restart(self):
        item, _ = self.prepare()
        self.assertEqual(self.run_one(TimeoutError("synthetic")).call_count,1)
        self.assertEqual(self.view(item)["state"],"UNKNOWN")
        self.service.recover()
        self.service.queue(self.policy["policy_id"],item)
        self.assertEqual(self.run_one().call_count,0)
        self.assertEqual(self.count("provider_call_attempts"),1)
        self.assertEqual(self.service.snapshot(self.policy["policy_id"])["calls_reserved"],1)

    def test_invalid_quote_stops_paid_lane_and_retains_usage(self):
        item, _ = self.prepare()
        def invalid(request, **kw):
            payload = self.payload(request)
            value = json.loads(payload["output"][0]["content"][0]["text"])
            value["facts"][0]["quote"] = "unrelated invented quotation"
            payload["output"][0]["content"][0]["text"] = json.dumps(value)
            return io.BytesIO(json.dumps(payload).encode())
        self.run_one(invalid)
        self.assertEqual(self.view(item)["state"],"FAILED")
        self.assertEqual(self.view(item)["reviews"][0]["receipt"]["error_code"],"unsupported_quote")
        self.assertEqual(self.service.snapshot(self.policy["policy_id"])["paid_stop_reason"],"review_failure")

    def test_expiry_and_global_pause_rechecked_at_actual_send(self):
        item, _ = self.prepare()
        original = self.providers.get("doubao").generate_json
        def paused(**generation):
            self.service.pause(self.policy["policy_id"])
            return original(**generation)
        with patch.object(self.providers.get("doubao"),"generate_json",side_effect=paused):
            self.assertEqual(self.run_one().call_count,0)
        self.assertEqual(self.view(item)["state"],"FAILED")
        self.assertEqual(self.count("provider_call_attempts"),1)

    def test_clock_rollback_does_not_extend_paid_authority(self):
        self.prepare()
        self.now += 5000
        self.service.monotonic_ms = lambda: self.now+60_000
        with self.assertRaises(NewsReviewError), patch("urllib.request.OpenerDirector.open") as wire:
            self.service.run_one(self.policy["policy_id"])
        wire.assert_not_called()
        self.assertEqual(self.count("provider_call_attempts"),0)

    def test_document_pause_rechecked_even_if_different_runner_tries_it(self):
        self.approve()
        item = import_event(self.store)
        self.service.discover(self.policy["policy_id"])
        job = self.service.request_document(self.policy["policy_id"],item)
        self.service.pause(self.policy["policy_id"])
        self.documents.run(job,cancel_event=self.cancel,deadline_monotonic_ms=int(time.monotonic()*1000)+12_000)
        self.fetcher.assert_not_called()
        self.assertEqual(self.documents.view(item)["job"]["error_code"],"policy_not_active")

    def test_spend_budget_stops_model_but_collection_can_continue(self):
        self.policy["spend_limit_cny"] = "0.0001"
        self.prepare()
        self.assertEqual(self.run_one().call_count,0)
        self.assertEqual(self.count("provider_call_attempts"),0)
        second = import_event(self.store,2)
        self.service.discover(self.policy["policy_id"])
        self.assertTrue(self.service.request_document(self.policy["policy_id"],second))
        self.assertEqual(self.service.snapshot(self.policy["policy_id"])["events_observed"],2)

    def test_slow_model_does_not_hold_store_or_block_discovery_or_second_model(self):
        self.prepare()
        started, release = threading.Event(), threading.Event()
        errors = []
        def slow(request, **kw):
            started.set()
            if not release.wait(5):
                raise TimeoutError("fixture stalled")
            return self.transport(request,**kw)
        def work():
            try:
                self.service.run_one(self.policy["policy_id"])
            except Exception as exc:
                errors.append(exc)
        with patch("urllib.request.OpenerDirector.open",side_effect=slow) as wire:
            thread = threading.Thread(target=work)
            thread.start()
            try:
                self.assertTrue(started.wait(3))
                import_event(self.store,2)
                self.service.discover(self.policy["policy_id"])
                self.assertFalse(self.service.run_one(self.policy["policy_id"]))
                self.assertEqual(self.service.snapshot(self.policy["policy_id"])["events_observed"],2)
            finally:
                release.set()
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(wire.call_count,1)

    def test_interrupted_claim_is_unknown_and_policy_reapproval_does_not_refund(self):
        item, job = self.prepare()
        with patch("backend.provider_call_ledger.ProviderCallLedger.reserve",side_effect=RuntimeError("synthetic crash")):
            with self.assertRaises(RuntimeError):
                self.run_one()
        self.service.recover()
        self.approve()
        self.assertEqual(self.view(item)["state"],"UNKNOWN")
        self.assertEqual(self.run_one().call_count,0)
        self.assertEqual(self.service.snapshot(self.policy["policy_id"])["calls_reserved"],1)

    def test_closed_policy_and_hash_approval_cannot_enable_other_sources_or_models(self):
        for changes in ({"max_model_calls":True},{"concurrency":2},{"sources":["other"]},
                        {"provider":"openai"},{"model":"other"},{"api_key":"not-allowed"},
                        {"expires_at_ms":self.now+86_400_001},{"spend_limit_cny":"NaN"}):
            with self.subTest(changes=changes), self.assertRaises(NewsReviewError):
                validate_policy({**self.policy,**changes})
        with self.assertRaises(NewsReviewError):
            self.service.approve(self.policy,approved_policy_sha256="b"*64)
        self.assertEqual(self.count("provider_execution_runs"),0)

    def test_policy_and_evidence_are_immutable_and_expired_window_cannot_send(self):
        self.prepare()
        with closing(self.store._connect()) as db, self.assertRaises(Exception):
            db.execute("UPDATE news_review_jobs SET input_json='{}'")
        self.now = self.policy["expires_at_ms"]
        with self.assertRaises(NewsReviewError), patch("urllib.request.OpenerDirector.open") as wire:
            self.service.run_one(self.policy["policy_id"])
        wire.assert_not_called()
        self.assertEqual(self.count("provider_call_attempts"),0)

    def test_controller_drives_evidence_then_model_and_source_gate_rechecks_pause(self):
        from backend.news_review_controller import NewsReviewController
        from backend.source_poll_control import ensure_source_poll_active, SourcePollCancelled
        self.approve()
        item = import_event(self.store)
        controller = NewsReviewController(self.service,self.policy["policy_id"])
        controller.enrich_cycle()
        self.assertEqual(self.view(item)["state"],"QUEUED")
        with controller.source_authorization("sec_filings"):
            ensure_source_poll_active(cancel_event=None,deadline_monotonic_ms=0)
            controller.pause()
            with self.assertRaises(SourcePollCancelled):
                ensure_source_poll_active(cancel_event=None,deadline_monotonic_ms=0)
        # A context must not leak to other work after exit.
        ensure_source_poll_active(cancel_event=None,deadline_monotonic_ms=0)
        self.assertEqual(self.count("provider_call_attempts"),0)

    def test_partial_worker_start_failure_can_drain_without_joining_unstarted_thread(self):
        from backend.news_review_controller import NewsReviewController
        self.approve()
        controller = NewsReviewController(self.service,self.policy["policy_id"])
        self.addCleanup(controller.stop)
        original = threading.Thread.start
        started = []
        def start(thread):
            if started:
                raise RuntimeError("synthetic thread start failure")
            started.append(thread)
            original(thread)
        with patch.object(threading.Thread,"start",new=start),self.assertRaises(RuntimeError):
            controller.start(recover=False)
        self.assertTrue(controller.stop(timeout=2))
        self.assertFalse(any(t.is_alive() for t in controller.threads))
        self.assertEqual(self.count("provider_call_attempts"),0)

    def test_host_converts_news_stop_exception_to_owner_retention_signal(self):
        from backend import http_server
        from backend.news_review_controller import NewsReviewController
        self.approve()
        controller = NewsReviewController(self.service,self.policy["policy_id"])
        server = Mock(server_port=41731)
        with patch.object(http_server,"STORE",self.store),patch.object(http_server,"ThreadingHTTPServer",return_value=server), \
             patch.object(http_server,"DocumentEvidenceController") as documents, \
             patch.object(controller,"start"),patch.object(controller,"stop",side_effect=RuntimeError("synthetic join failure")):
            documents.return_value.stop.return_value = True
            with self.assertRaises(http_server.RuntimeShutdownIncomplete):
                http_server.run_server(instance_owner=self.owner,port=0,news_review_controller=controller)
        self.owner.assert_held_for(self.path)

    def test_report_binds_ledger_and_does_not_invent_24h_or_source_success(self):
        from backend.news_review_report import NewsReviewJournal, build_news_review_report
        item,_ = self.prepare()
        journal = NewsReviewJournal(self.service,self.policy["policy_id"])
        journal.record("session_started",runtime_status="starting")
        self.run_one()
        journal.record("heartbeat",runtime_status="running")
        journal.record("session_stopped",runtime_status="stopped")
        report = build_news_review_report(self.service,self.policy["policy_id"])
        self.assertEqual(report["duplicate_review_attempts"],0)
        self.assertEqual(len(report["call_ledger"]),1)
        self.assertTrue(report["event_end_to_end_recorded"])
        self.assertFalse(report["continuity"]["continuous_window_observed"])
        self.assertIsNone(report["source_checks"]["sec_filings"]["success_rate"])
        self.assertFalse(report["supplier_bill_verified"])
        self.assertEqual(report["body_coverage"]["main_body_located"],1)

    def test_native_source_grants_need_policy_and_preserve_operator_disablement(self):
        from backend.source_monitoring.runtime import build_source_monitoring_runtime
        from backend.source_monitoring.settings import SourceMonitoringSettings
        settings = SourceMonitoringSettings(enabled=True,auto_start=True,dry_run=False,source_profile=PROFILE)
        runtime = build_source_monitoring_runtime(self.store,settings)
        with patch("urllib.request.OpenerDirector.open") as wire:
            with self.assertRaises(NewsReviewError):
                self.service.prepare_sources(self.policy["policy_id"],runtime)
            self.approve()
            self.service.prepare_sources(self.policy["policy_id"],runtime)
            states = runtime.repository.list_states()
            self.assertEqual({s["adapter_key"] for s in states},{"sec_filings","company_ir"})
            self.assertTrue(all(s["enabled"] and s["checkpoint"] == {} for s in states))
            sec = next(s for s in states if s["adapter_key"] == "sec_filings")
            runtime.repository.set_enabled("sec_filings",config_version=sec["config_version"],enabled=False)
            self.service.prepare_sources(self.policy["policy_id"],runtime)
            self.assertFalse(runtime.repository.get_state("sec_filings")["enabled"])
            wire.assert_not_called()
        self.assertEqual(self.count("source_adapter_runs"),0)
        self.assertEqual(self.count("news_review_source_grants"),2)

    def test_synthetic_collector_seed_disconnect_restart_and_new_event_to_review(self):
        from backend.source_monitoring.registry import SourceAdapterRegistry
        from backend.source_monitoring.settings import SourceMonitoringSettings
        from backend.source_monitoring.state_repository import SourceMonitoringStateRepository
        from backend.source_monitoring.supervisor import SourceMonitoringSupervisor
        from backend.news_review_controller import NewsReviewController
        from backend.news_review_report import NewsReviewJournal,build_news_review_report
        from tests.test_source_monitoring_sec_baseline import MutableSecRecentFetcher,SecCompleteBaselineTests
        self.approve()
        transport = MutableSecRecentFetcher()
        adapter = SecCompleteBaselineTests.adapter(self,transport)
        repository = SourceMonitoringStateRepository(self.store,clock_ms=lambda:self.now)
        repository.set_enabled(adapter.adapter_key,config_version=adapter.config_version,enabled=True)
        registry = SourceAdapterRegistry((adapter,))
        def supervisor():
            return SourceMonitoringSupervisor(registry=registry,repository=repository,
                source_inbox=SourceInboxService(self.store,clock=lambda:self.now/1000),
                settings=SourceMonitoringSettings(enabled=True,dry_run=False),clock_ms=lambda:self.now,
                event_sink=lambda *_args,**_kwargs:None)
        monitor = supervisor()
        journal = NewsReviewJournal(self.service,self.policy["policy_id"])
        first = monitor.run_once(adapter.adapter_key)
        self.assertEqual(first["initialization"]["outcome"],"seeded")
        self.assertEqual(self.count("source_inbox_items"),0)
        journal.record("source_poll",source_run_id=first["run_id"])
        seeded = copy.deepcopy(repository.get_state(adapter.adapter_key)["checkpoint"])
        self.now += 300_001
        def disconnected():
            raise TimeoutError("synthetic disconnect")
        transport.after_snapshot = disconnected
        failed = monitor.run_once(adapter.adapter_key)
        self.assertIn(failed["status"],{"FAILED","DEGRADED"})
        self.assertEqual(repository.get_state(adapter.adapter_key)["checkpoint"],seeded)
        journal.record("source_poll",source_run_id=failed["run_id"])
        transport.records.append((14,"2026-09-05T09:00:00Z"))
        self.now += 300_001
        restored = supervisor().run_once(adapter.adapter_key)
        self.assertEqual(restored["status"],"SUCCEEDED")
        self.assertEqual(self.count("source_inbox_items"),1)
        journal.record("source_poll",source_run_id=restored["run_id"])
        controller = NewsReviewController(self.service,self.policy["policy_id"])
        controller.enrich_cycle()
        self.assertEqual(self.run_one().call_count,1)
        self.now += 300_001
        supervisor().run_once(adapter.adapter_key)
        controller.enrich_cycle()
        self.assertEqual(self.run_one().call_count,0)
        report = build_news_review_report(self.service,self.policy["policy_id"])
        self.assertEqual(report["source_checks"]["sec_filings"]["poll_runs_observed"],3)
        self.assertEqual(report["source_checks"]["sec_filings"]["success_rate"],2/3)
        self.assertEqual(report["duplicate_review_attempts"],0)
        self.assertTrue(report["event_end_to_end_recorded"])
        self.assertFalse(report["continuity"]["continuous_window_observed"])

    def test_native_schema_requires_explicit_upgrade_and_keeps_older_inbox_readable(self):
        from backend.database_migration import build_migration_manifest,assert_database_ready_for_startup,DatabaseMigrationRequired
        item = import_event(self.store)
        with closing(self.store._connect()) as db,db:
            for table in ("source_grants","observations","links","receipts","jobs","events","policies"):
                db.execute("DROP TABLE news_review_"+table)
            db.execute("DELETE FROM schema_migrations WHERE key='news_event_review_v1'")
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        manifest = build_migration_manifest(self.path)
        names = {change["name"] for change in manifest["changes"]["schema_changes"]}
        self.assertIn("news_review_jobs",names)
        with self.assertRaises(DatabaseMigrationRequired):
            assert_database_ready_for_startup(self.path)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
        self.assertEqual(SourceInboxService(self.store).get_item(item)["id"],item)

    def test_fresh_process_launcher_prepare_and_rejected_runs_are_zero_network(self):
        app = Path(__file__).resolve().parents[1]
        root = Path(self.temp.name)/"NewsReviewTrialLauncher"
        # Only Git identity is synthetic. The child uses the real entry point,
        # isolated environment, startup/migration gate and DB instance owner.
        program = """
import json,sys,subprocess,os
from contextlib import ExitStack
from unittest.mock import patch
from scripts.run_news_review_trial import main
original=subprocess.check_output
def git_identity(args,**kw):
    if args[-2:]==['rev-parse','HEAD']: return 'a'*40+'\\n'
    if args[-2:]==['status','--porcelain']: return ''
    return original(args,**kw)
try:
    with ExitStack() as stack:
        stack.enter_context(patch('subprocess.check_output',side_effect=git_identity))
        if os.environ.get('NEWS_FIXTURE_HOST')=='1':
            from pathlib import Path
            config=json.loads(Path(sys.argv[sys.argv.index('--config')+1]).read_text())
            os.environ['AI_STUDIO_DATABASE_PATH']=config['database_path']
            os.environ['AI_STUDIO_RUNTIME_DIR']=str(Path(config['database_path']).parent.parent/'runtime')
            os.environ['SEC_USER_AGENT']='Synthetic fixture@example.com'
            def host(**kwargs):
                from backend.store import STORE
                runtime=kwargs['runtime_factory'](STORE)
                assert {s['adapter_key'] for s in runtime.repository.list_states() if s['enabled']}=={'sec_filings','company_ir'}
                assert kwargs['news_review_shutdown_seconds']==255
                assert kwargs['news_review_controller'].service.owner is kwargs['instance_owner']
            stack.enter_context(patch('backend.http_server.run_server',side_effect=host))
            stack.enter_context(patch('scripts.pilot_password_dialog.ask_api_key',return_value='synthetic-credential'))
        result=main(sys.argv[1:])
except Exception as exc:
    print(json.dumps({'rejected':getattr(exc,'code',type(exc).__name__)}))
    raise SystemExit(2)
raise SystemExit(result)
"""
        def run(*args,synthetic_host=False):
            result = subprocess.run([sys.executable,"-X","utf8","-B","-c",program,*args],cwd=app,
                                    capture_output=True,text=True,timeout=30,
                                    env={**os.environ,"NEWS_FIXTURE_HOST":"1" if synthetic_host else "0"})
            return result,json.loads(result.stdout.strip().splitlines()[-1])
        result,created = run("--initialize","--root",str(root),"--candidate-sha","a"*40)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        p = {**self.policy,"database_path":created["database_path"],"room_id":created["room_id"]}
        config = root/"policy.json"
        config.write_text(json.dumps(p),encoding="utf-8")
        output = root/"preview.json"
        database = Path(created["database_path"])
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        self.assertEqual(created["database_sha256"],before)
        result,rejected = run("--initialize","--root",str(root),"--candidate-sha","a"*40)
        self.assertEqual(rejected,{"rejected":"trial_already_exists"})
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(),before)
        result,prepared = run("--prepare","--config",str(config),"--output",str(output))
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        result,rejected = run("--run","--config",str(config),"--sec-user-agent","Synthetic fixture@example.com")
        self.assertEqual(rejected,{"rejected":"approval_mismatch"})
        result,rejected = run("--run","--config",str(config),"--sec-user-agent","Synthetic fixture@example.com",
                              "--approve-policy-sha256",prepared["policy_sha256"])
        self.assertEqual(rejected,{"rejected":"policy_expired"})
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(),before)
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM provider_call_attempts").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM news_review_policies").fetchone()[0],0)
        # Exercise the approved orchestration without starting source/network
        # workers. The production owner, source activation and report paths run.
        now = int(time.time()*1000)
        p.update(not_before_ms=now-1000,expires_at_ms=now+60_000)
        config.write_text(json.dumps(p),encoding="utf-8")
        result,prepared = run("--prepare","--config",str(config),"--output",str(root/"preview-current.json"))
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        result,_ = run("--run","--config",str(config),"--sec-user-agent","Synthetic fixture@example.com",
                       "--approve-policy-sha256",prepared["policy_sha256"],"--password-dialog",
                       "--output",str(root/"run-result.json"),synthetic_host=True)
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        report = json.loads((root/"run-result.json").read_text(encoding="utf-8"))
        self.assertEqual(report["snapshot"]["observation"],"no_new_event_observed")
        self.assertEqual(report["snapshot"]["calls_reserved"],0)
        self.assertFalse(report["continuity"]["full_24h_window_elapsed"])
        self.assertEqual(report['stop_events'][0]['stop_type'], 'unexpected_host_return')
        self.assertFalse(report['outcome']['work_completed'])
        self.assertTrue(report['outcome']['cleanup_clean'])
        activation_root = Path(self.temp.name)/"NewsReviewTrialActivation"
        result,created = run("--initialize","--root",str(activation_root),"--candidate-sha","a"*40)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        activation_policy = {**p,"database_path":created["database_path"],"room_id":created["room_id"]}
        activation_config = activation_root/"template.json"
        activation_config.write_text(json.dumps(activation_policy),encoding="utf-8")
        result,proposal = run("--prepare-activation","--config",str(activation_config),"--duration-ms","3600000")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        activation_args = ("--activate","--config",str(activation_config),"--duration-ms","3600000",
                           "--approve-activation-sha256",proposal["activation_sha256"],"--password-dialog",
                           "--sec-user-agent","Synthetic fixture@example.com")
        result,_ = run(*activation_args,synthetic_host=True)
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        activated = json.loads((activation_root/"activation-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(activated["expires_at_ms"]-activated["not_before_ms"],3_600_000)
        self.assertEqual({k:v for k,v in activated.items() if k not in {"not_before_ms","expires_at_ms"}},
                         {k:v for k,v in activation_policy.items() if k not in {"not_before_ms","expires_at_ms"}})
        result,rejected = run(*activation_args,synthetic_host=True)
        self.assertEqual(rejected,{"rejected":"activation_already_reserved"})

    def test_native_http_read_and_pause_require_normal_host_token(self):
        from http.server import ThreadingHTTPServer
        from backend import http_server
        from backend.news_review_controller import NewsReviewController
        from tests.test_source_inbox_http import SourceInboxHttpTests
        item,_ = self.prepare()
        server = ThreadingHTTPServer(("127.0.0.1",0),http_server.StudioRequestHandler)
        server.ai_studio_instance_owner = self.owner
        server.ai_studio_news_review = NewsReviewController(self.service,self.policy["policy_id"])
        thread = threading.Thread(target=server.serve_forever)
        self.base_url = f"http://127.0.0.1:{server.server_port}"
        self.request_with_headers = SourceInboxHttpTests.request_with_headers.__get__(self)
        request = SourceInboxHttpTests.request.__get__(self)
        with patch.object(http_server,"STORE",self.store):
            thread.start()
            try:
                status,result = request(f"/api/monitoring/events/{item}/news-review")
                self.assertEqual(status,200)
                self.assertEqual(result["news_review"]["state"],"QUEUED")
                status,_ = request("/api/monitoring/news-review/control",method="POST",payload={"action":"pause"},include_token=False)
                self.assertEqual(status,403)
                status,_ = request("/api/monitoring/news-review/control",method="POST",payload={"action":"approve"})
                self.assertEqual(status,400)
                status,result = request("/api/monitoring/news-review/control",method="POST",payload={"action":"pause"})
                self.assertEqual(status,200)
                self.assertEqual(result["news_review"]["state"],"PAUSED")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
        self.assertEqual(self.count("provider_call_attempts"),0)
