"""Real controller/service/parser/ledger; bounded threads and synthetic HTTP."""
import json
import threading
import time
import unittest
from contextlib import closing
from unittest.mock import patch

from backend.document_evidence import RECHECK_MS, current_document
from backend.execution_boundary import AuthorizedTextRequest
from backend.news_review_controller import NewsReviewController
from backend.news_review_report import NewsReviewJournal, build_news_review_report as build_report
from backend.news_review_stop import NewsReviewStop
from tests import test_news_review as fixtures


class NewsReviewEdgeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.NewsReviewTests()
        self.f.addCleanup = self.addCleanup
        self.f.setUp()

    def start_thread(self, action):
        errors = []
        def run():
            try:
                action()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        return thread, errors

    def join_thread(self, thread, errors):
        thread.join(10)
        self.assertFalse(thread.is_alive(), "synthetic worker failed to drain")
        if errors:
            raise errors[0]

    def stopper(self, controller):
        f = self.f
        return NewsReviewStop(f.service, f.policy, controller,
                              NewsReviewJournal(f.service, f.policy['policy_id']),
                              threading.Event(), f.path.parent.parent)

    def test_stop_after_preflight_blocks_http_even_while_stop_receipt_is_blocked(self):
        f = self.f
        item, job = f.prepare()
        controller = NewsReviewController(f.service, f.policy['policy_id'])
        stopper = self.stopper(controller)
        prepared, release_send = threading.Event(), threading.Event()
        writing, release_write = threading.Event(), threading.Event()
        def request(*args, **kwargs):
            bound = AuthorizedTextRequest(*args, **kwargs)
            check = bound.before_send
            def preflight():
                check()
                prepared.set()
                if not release_send.wait(10):
                    raise AssertionError("test did not release send preparation")
            bound.before_send = preflight
            return bound
        def blocked_write(*args):
            writing.set()
            if not release_write.wait(10):
                raise AssertionError("test did not release receipt")
            raise OSError("synthetic private sink detail")
        with patch('backend.news_review_service.AuthorizedTextRequest', side_effect=request), \
             patch('urllib.request.OpenerDirector.open', side_effect=f.transport) as wire, \
             patch('backend.news_review_stop.write_record', side_effect=blocked_write):
            worker, worker_errors = self.start_thread(controller.model_cycle)
            stopping = None
            try:
                self.assertTrue(prepared.wait(10))
                self.assertEqual(f.service.snapshot(f.policy['policy_id'])['calls_reserved'], 1)
                stopping, stop_errors = self.start_thread(lambda: stopper.request(
                    'runtime_failure', trigger='source_runtime', code='synthetic_fault'))
                self.assertTrue(writing.wait(10))
                self.assertTrue(controller.stop_event.is_set())
                self.assertTrue(stopper.event.is_set())
                release_send.set()
                self.join_thread(worker, worker_errors)
                wire.assert_not_called()
                f.owner.assert_held_for(f.path)
            finally:
                release_send.set()
                release_write.set()
                self.join_thread(worker, worker_errors)
                if stopping:
                    self.join_thread(stopping, stop_errors)
        review = f.view(item)['reviews'][0]
        self.assertEqual(review['state'], 'CANCELLED')
        self.assertEqual(review['error_code'], 'host_stopped_before_send')
        self.assertFalse(review['receipt']['http_attempted'])
        with closing(f.store._connect()) as db:
            row = db.execute('SELECT * FROM news_review_jobs WHERE id=?', (job,)).fetchone()
            self.assertEqual(row['http_attempted'], 0)
            self.assertTrue(row['attempt_id'])
            self.assertEqual(db.execute('SELECT status FROM provider_call_attempts').fetchone()[0], 'FAILED')
        self.assertEqual(f.count('news_review_content_claims'), 1)
        self.assertEqual(f.count('provider_call_attempts'), 1)
        self.assertEqual(f.service.snapshot(f.policy['policy_id'])['calls_reserved'], 1)
        self.assertEqual(f.run_one().call_count, 0)
        self.assertEqual(stopper.summary()['exit_code'], 2)
        self.assertEqual(stopper.summary()['persistence_errors'],
                         [{'sink':'file', 'exception_type':'OSError'}])
        self.assertNotIn('private sink detail', json.dumps(stopper.summary()))

    def test_stop_before_claim_consumes_no_reservation(self):
        f = self.f
        f.prepare()
        controller = NewsReviewController(f.service, f.policy['policy_id'])
        controller.request_stop()
        with patch('urllib.request.OpenerDirector.open') as wire:
            controller.model_cycle()
            self.assertFalse(f.service.run_one(f.policy['policy_id'], send_gate=controller.send_gate))
        wire.assert_not_called()
        self.assertEqual(f.count('provider_call_attempts'), 0)
        self.assertEqual(f.service.snapshot(f.policy['policy_id'])['calls_reserved'], 0)

    def test_real_host_stop_event_prevents_model_claim_before_host_drain(self):
        from backend import http_server
        from backend.source_monitoring.runtime import build_source_monitoring_runtime
        from backend.source_monitoring.settings import SourceMonitoringSettings
        f = self.f
        f.prepare()
        host_stop = threading.Event()
        controller = NewsReviewController(f.service, f.policy['policy_id'])
        def ready(server):
            self.assertGreater(server.server_port, 0)
            host_stop.set()
            # The server has not reached its finally/request_stop yet.
            self.assertFalse(controller.stop_event.is_set())
            controller.model_cycle()
        with patch.object(http_server, 'STORE', f.store), \
             patch('urllib.request.OpenerDirector.open', side_effect=f.transport) as wire:
            http_server.run_server(host='127.0.0.1', port=0, instance_owner=f.owner,
                runtime_factory=lambda store: build_source_monitoring_runtime(store, SourceMonitoringSettings()),
                news_review_controller=controller, stop_event=host_stop, ready_callback=ready)
        wire.assert_not_called()
        self.assertFalse(any(t.is_alive() for t in controller.threads))
        self.assertEqual(f.count('provider_call_attempts'), 0)
        f.owner.assert_held_for(f.path)

    def test_journal_stall_cannot_delay_host_and_worker_stop(self):
        f = self.f
        f.prepare()
        controller = NewsReviewController(f.service, f.policy['policy_id'])
        stopper = self.stopper(controller)
        writing, release = threading.Event(), threading.Event()
        def blocked(*args, **kwargs):
            writing.set()
            if not release.wait(10):
                raise AssertionError("test did not release journal")
            raise OSError('synthetic journal detail')
        with patch.object(stopper.journal, 'record', side_effect=blocked), \
             patch('urllib.request.OpenerDirector.open') as wire:
            worker, errors = self.start_thread(lambda: stopper.request('runtime_failure', trigger='test'))
            try:
                self.assertTrue(writing.wait(10))
                self.assertTrue(stopper.event.is_set())
                self.assertTrue(controller.stop_event.is_set())
                controller.model_cycle()
                wire.assert_not_called()
                f.owner.assert_held_for(f.path)
            finally:
                release.set()
                self.join_thread(worker, errors)
        self.assertEqual(stopper.summary()['persistence_errors'],
                         [{'sink':'journal', 'exception_type':'OSError'}])

    def sent_request(self, *, unknown):
        f = self.f
        item, _ = f.prepare()
        controller = NewsReviewController(f.service, f.policy['policy_id'])
        sent, release = threading.Event(), threading.Event()
        def transport(request, **kwargs):
            sent.set()
            if not release.wait(10):
                raise AssertionError('test did not release response')
            if unknown:
                raise TimeoutError('synthetic uncertain response')
            return f.transport(request, **kwargs)
        with patch('urllib.request.OpenerDirector.open', side_effect=transport) as wire:
            worker, errors = self.start_thread(controller.model_cycle)
            controller.threads = [worker]
            try:
                self.assertTrue(sent.wait(10))
                self.assertFalse(controller.stop(timeout=0))
                self.assertTrue(controller.stop_event.is_set())
                f.owner.assert_held_for(f.path)
            finally:
                release.set()
                self.join_thread(worker, errors)
            controller.model_cycle()
            self.assertEqual(wire.call_count, 1)
        self.assertEqual(f.view(item)['state'], 'UNKNOWN' if unknown else 'REVIEWED')
        self.assertTrue(f.view(item)['reviews'][0]['receipt']['http_attempted'])
        self.assertEqual(f.service.snapshot(f.policy['policy_id'])['calls_reserved'], 1)
        self.assertEqual(f.run_one().call_count, 0)

    def test_admitted_request_can_finish_after_stop(self):
        self.sent_request(unknown=False)

    def test_admitted_unknown_request_is_not_cancelled_or_replayed(self):
        self.sent_request(unknown=True)

    def refresh_document(self, item, raw):
        f = self.f
        f.now += RECHECK_MS + 1
        f.raw = raw
        job = f.documents.request(item, session_id='manual-synthetic', confirmation=True, refresh=True)
        f.documents.run(job['id'], cancel_event=f.cancel,
                        deadline_monotonic_ms=int(time.monotonic()*1000)+12000)
        return current_document(f.documents.view(item))

    def test_a_b_a_reuses_original_review_and_preserves_immutable_history(self):
        f = self.f
        item, job_a = f.prepare()
        f.run_one()
        doc_a = current_document(f.documents.view(item))
        receipt_a = f.view(item)['reviews'][0]['receipt']
        doc_b = self.refresh_document(item, fixtures.SEC_HTML.replace(b'$10 million', b'$11 million'))
        self.assertNotEqual(doc_b['id'], doc_a['id'])
        job_b = f.service.queue(f.policy['policy_id'], item)
        self.assertNotEqual(job_b, job_a)
        f.run_one()
        with closing(f.store._connect()) as db:
            immutable = [tuple(r) for r in db.execute('SELECT * FROM source_document_versions ORDER BY created_at,id')]
        restored = self.refresh_document(item, fixtures.SEC_HTML)
        self.assertEqual(restored, doc_a)
        self.assertEqual(f.documents.view(item)['current_observation']['completed_at'], f.now)
        self.assertEqual(f.service.queue(f.policy['policy_id'], item), job_a)
        self.assertEqual(f.run_one().call_count, 0)
        projected = f.view(item)
        self.assertEqual(projected['current_document_version_id'], doc_a['id'])
        self.assertEqual(projected['current_review_id'], job_a)
        self.assertEqual(projected['state'], 'REVIEWED')
        self.assertEqual(projected['reviews'][0]['receipt'], receipt_a)
        self.assertEqual([r['id'] for r in projected['reviews']], [job_a, job_b])
        with closing(f.store._connect()) as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM source_document_versions ORDER BY created_at,id')], immutable)
        self.assertEqual(f.count('provider_call_attempts'), 2)
        self.assertEqual(f.service.snapshot(f.policy['policy_id'])['calls_reserved'], 2)
        # A later unsuccessful refresh must not erase the last successful A.
        f.fetcher.side_effect = TimeoutError('synthetic read failure')
        self.refresh_document(item, fixtures.SEC_HTML)
        self.assertEqual(f.documents.view(item)['status'], 'failed')
        self.assertEqual(current_document(f.documents.view(item)), doc_a)
        self.assertEqual(f.view(item)['current_review_id'], job_a)

    def test_restored_body_controls_report_coverage_not_newest_blob(self):
        f = self.f
        item, job_a = f.prepare()
        f.run_one()
        doc_a = current_document(f.documents.view(item))
        partial = self.refresh_document(item, b'<html><body><p>Navigation only.</p></body></html>')
        self.assertFalse(partial['body_located'])
        self.assertIsNone(f.service.queue(f.policy['policy_id'], item))
        self.assertEqual(build_report(f.service, f.policy['policy_id'])['body_coverage'].get('main_body_located', 0), 0)
        self.refresh_document(item, fixtures.SEC_HTML)
        self.assertEqual(f.service.queue(f.policy['policy_id'], item), job_a)
        self.assertEqual(f.view(item)['current_document_version_id'], doc_a['id'])
        self.assertEqual(f.view(item)['state'], 'REVIEWED')
        self.assertEqual(build_report(f.service, f.policy['policy_id'])['body_coverage']['main_body_located'], 1)
        self.assertEqual(f.run_one().call_count, 0)
        self.assertEqual(f.count('provider_call_attempts'), 1)
