"""Cross-startup/authorization regressions; real host, isolated DB, fake I/O."""
import copy
import json
import sqlite3
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import patch

from backend.news_review_service import NewsReviewService
from backend.news_review_controller import NewsReviewController
from tests import test_news_review as fixtures


class NewsReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.NewsReviewTests()
        # Register on the executing case so cleanup exceptions reach its result.
        self.f.addCleanup = self.addCleanup
        self.f.setUp()

    def restart_service(self):
        f = self.f
        f.service = NewsReviewService(f.store, f.providers, instance_owner=f.owner,
            documents=f.documents, clock=lambda: f.now, monotonic_ms=lambda: f.now)
        return f.service

    def host(self, service, *, on_ready=None):
        from backend import http_server
        from backend.source_monitoring.runtime import build_source_monitoring_runtime
        from backend.source_monitoring.settings import SourceMonitoringSettings
        f = self.f
        stop = threading.Event()
        controller = NewsReviewController(service, f.policy['policy_id'])
        def factory(store):
            self.assertEqual(service.snapshot(f.policy['policy_id'])['state'], 'ACTIVE')
            # Real runtime lifecycle, disabled sources. No transport is needed
            # for the empty inbox startup regressions.
            return build_source_monitoring_runtime(store, SourceMonitoringSettings())
        def ready(server):
            self.assertGreater(server.server_port, 0)
            if on_ready:
                on_ready(controller)
            stop.set()
        with patch.object(http_server, 'STORE', f.store):
            http_server.run_server(host='127.0.0.1', port=0, instance_owner=f.owner,
                runtime_factory=factory, news_review_controller=controller,
                stop_event=stop, ready_callback=ready)
        self.assertFalse(any(t.is_alive() for t in controller.threads))
        f.owner.assert_held_for(f.path)

    def test_first_approval_without_automatic_resume_survives_real_host(self):
        self.f.policy['resume_within_window'] = False
        self.f.approve()
        self.host(self.f.service)

    def test_explicit_resume_without_automatic_resume_survives_real_host(self):
        f = self.f
        f.policy['resume_within_window'] = False
        approved = f.approve()
        service = self.restart_service()
        service.recover()
        self.assertEqual(service.snapshot(f.policy['policy_id'])['state'], 'PAUSED')
        service.resume(f.policy['policy_id'], approved_policy_sha256=approved['policy_sha256'])
        self.host(service)

    def test_waiting_document_survives_both_recovery_owners_without_new_reservation(self):
        f = self.f
        f.approve()
        item = fixtures.import_event(f.store)
        f.service.discover(f.policy['policy_id'])
        job = f.service.request_document(f.policy['policy_id'], item)
        service = self.restart_service()
        def ready(controller):
            self.assertEqual(f.documents.view(item)['status'], 'waiting')
            controller.enrich_cycle()
        self.host(service, on_ready=ready)
        self.assertEqual(f.fetcher.call_count, 1)
        self.assertTrue(f.documents.view(item)['versions'])
        self.assertEqual(service.snapshot(f.policy['policy_id'])['documents_reserved'], 1)
        with closing(f.store._connect()) as db:
            self.assertEqual(db.execute('SELECT document_job_id FROM news_review_events').fetchone()[0], job)
        self.assertEqual(f.count('provider_call_attempts'), 0)

    def next_policy_item(self):
        from backend.source_monitoring.packet_builder import build_source_import_packet
        from backend.source_inbox_service import SourceInboxService
        from tests.test_document_evidence import event
        f = self.f
        old_policy = copy.deepcopy(f.policy)
        f.now = old_policy['expires_at_ms'] + 1
        f.policy.update(policy_id='synthetic-next', not_before_ms=f.now, expires_at_ms=f.now+86400000)
        f.approve()
        updated = event()
        updated['headline'] += ' (metadata updated)'
        packet = build_source_import_packet(adapter_key='sec_filings', external_run_id='metadata-update',
            captured_at_ms=f.now, observed_items=[updated])
        new_item = SourceInboxService(f.store, clock=lambda:f.now/1000).import_packet(
            json.dumps(packet), actor='source_monitoring_worker')['items'][0]['id']
        f.service.discover(f.policy['policy_id'])
        doc = f.service.request_document(f.policy['policy_id'], new_item)
        f.documents.run(doc, cancel_event=f.cancel, deadline_monotonic_ms=int(time.monotonic()*1000)+12000)
        return new_item

    def test_expired_unsent_job_continues_under_new_authorization(self):
        f = self.f
        item, old_job = f.prepare()
        old_policy = copy.deepcopy(f.policy)
        new_item = self.next_policy_item()
        self.assertNotEqual(item, new_item)
        new_job = f.service.queue(f.policy['policy_id'], new_item)
        self.assertNotEqual(new_job, old_job)
        self.assertEqual(f.run_one().call_count, 1)
        self.assertEqual(f.run_one().call_count, 0)
        self.assertEqual(f.service.snapshot(old_policy['policy_id'])['calls_reserved'], 0)
        self.assertEqual(f.service.snapshot(f.policy['policy_id'])['calls_reserved'], 1)
        with closing(f.store._connect()) as db:
            old = db.execute('SELECT * FROM news_review_jobs WHERE id=?', (old_job,)).fetchone()
            self.assertEqual(old['policy_id'], old_policy['policy_id'])
            self.assertEqual(old['status'], 'CANCELLED')
            self.assertEqual(old['started_at'], 0)
        self.assertEqual(f.view(new_item)['state'], 'REVIEWED')

    def test_unknown_old_content_cannot_consume_a_new_authorization(self):
        f = self.f
        _, old_job = f.prepare()
        self.assertEqual(f.run_one(TimeoutError('synthetic')).call_count, 1)
        new_item = self.next_policy_item()
        self.assertEqual(f.service.queue(f.policy['policy_id'], new_item), old_job)
        with patch('urllib.request.OpenerDirector.open') as wire:
            self.host(self.restart_service(), on_ready=lambda c:c.model_cycle())
            wire.assert_not_called()
        self.assertEqual(f.count('provider_call_attempts'), 1)
        self.assertEqual(f.service.snapshot(f.policy['policy_id'])['calls_reserved'], 0)
        self.assertEqual(f.view(new_item)['state'], 'UNKNOWN')

    def test_unknown_result_survives_real_host_restart_without_send(self):
        f = self.f
        item, job = f.prepare()
        self.assertEqual(f.run_one(TimeoutError('synthetic unknown')).call_count, 1)
        service = self.restart_service()
        with patch('urllib.request.OpenerDirector.open') as wire:
            self.host(service, on_ready=lambda c:c.model_cycle())
            wire.assert_not_called()
        self.assertEqual(f.view(item)['state'], 'UNKNOWN')
        self.assertEqual(f.count('provider_call_attempts'), 1)
        self.assertEqual(service.snapshot(f.policy['policy_id'])['calls_reserved'], 1)

    def test_expired_waiting_and_interrupted_fetching_are_explicitly_cancelled(self):
        f = self.f
        f.approve()
        waiting_item = fixtures.import_event(f.store, 1)
        fetching_item = fixtures.import_event(f.store, 2)
        f.service.discover(f.policy['policy_id'])
        waiting = f.service.request_document(f.policy['policy_id'], waiting_item)
        fetching = f.service.request_document(f.policy['policy_id'], fetching_item)
        with closing(f.store._connect()) as db, db:
            db.execute("UPDATE source_document_jobs SET status='fetching' WHERE id=?", (fetching,))
        f.now = f.policy['expires_at_ms']
        self.restart_service().recover()
        f.documents.recover()
        for item, code in ((waiting_item,'DOCUMENT_AUTHORIZATION_EXPIRED'), (fetching_item,'DOCUMENT_INTERRUPTED')):
            self.assertEqual(f.documents.view(item)['status'], 'cancelled')
            self.assertEqual(f.documents.view(item)['job']['error_code'], code)
            self.assertEqual(f.view(item)['state'], 'DOCUMENT_CANCELLED')
        f.fetcher.assert_not_called()
        self.assertEqual(f.service.snapshot(f.policy['policy_id'])['documents_reserved'], 2)

    def test_upgrade_preserves_historical_receipts_and_claims_cannot_be_deleted(self):
        from backend.news_review_schema import ensure_news_review_schema
        f = self.f
        item, job = f.prepare()
        f.run_one()
        with closing(f.store._connect()) as db:
            original = tuple(db.execute('SELECT * FROM news_review_jobs WHERE id=?', (job,)).fetchone())
            receipt = tuple(db.execute('SELECT * FROM news_review_receipts WHERE job_id=?', (job,)).fetchone())
            db.execute('DROP TABLE news_review_content_claims')  # pre-upgrade absence, disposable fixture only
            ensure_news_review_schema(db, applied_at_ms=f.now)
            self.assertEqual(tuple(db.execute('SELECT * FROM news_review_jobs WHERE id=?', (job,)).fetchone()), original)
            self.assertEqual(tuple(db.execute('SELECT * FROM news_review_receipts WHERE job_id=?', (job,)).fetchone()), receipt)
            self.assertEqual(db.execute('SELECT job_id FROM news_review_content_claims').fetchone()[0], job)
            for sql in ('DELETE FROM news_review_jobs', 'DELETE FROM news_review_receipts',
                        'DELETE FROM news_review_content_claims', "UPDATE news_review_content_claims SET job_id='changed'"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)

    def test_observer_journal_failure_still_leaves_a_bounded_stop_receipt(self):
        from types import SimpleNamespace
        from backend.news_review_report import NewsReviewJournal
        from backend.news_review_stop import NewsReviewStop
        f = self.f
        f.approve()
        journal = NewsReviewJournal(f.service, f.policy['policy_id'])
        controller = NewsReviewController(f.service, f.policy['policy_id'])
        stop = threading.Event()
        termination = NewsReviewStop(f.service, f.policy, controller, journal, stop, f.path.parent.parent)
        runtime = SimpleNamespace(snapshot=lambda:{'status':'running'})
        with patch.object(journal, 'record', side_effect=sqlite3.OperationalError('sensitive detail omitted')):
            termination.watch({'runtime':runtime, 'ready':True}, interval_seconds=0.001)
        self.assertTrue(stop.is_set())
        summary = termination.summary()
        self.assertEqual(summary['exit_code'], 2)
        self.assertEqual(summary['persistence_errors'], [{'sink':'journal','exception_type':'OperationalError'}])
        receipt = next(f.path.parent.parent.glob('stop-*.json')).read_text(encoding='utf-8')
        self.assertNotIn('sensitive detail', receipt)
        self.assertEqual(json.loads(receipt)['stop_type'], 'observer_failure')
        self.assertEqual(json.loads(receipt)['exception_type'], 'OperationalError')
