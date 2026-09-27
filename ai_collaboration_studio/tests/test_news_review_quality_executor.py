"""Real provider protocol and send gate with synthetic HTTP and signed fixtures."""
import io
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.execution_boundary import AuthorizedTextRequest
from backend.news_review_contracts import ENDPOINT, MODEL, VERSION
from backend.news_review_quality_executor import QualityBatchExecutor, QualityShutdownIncomplete
from backend.providers.doubao_provider import DoubaoProvider
from tests import test_news_review_quality_ledger as fixtures


class QualityExecutorTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.QualityBatchLedgerTests()
        self.f.addCleanup = self.addCleanup
        self.f.setUp()
        self.batch = self.f.start()
        self.provider = DoubaoProvider(api_key='synthetic-private-fixture-key',base_url=ENDPOINT.removesuffix('/responses'))
        self.executor = QualityBatchExecutor(self.batch,self.provider)

    def payload(self, request, **kwargs):
        self.assertEqual(kwargs,{'timeout':240})
        self.assertEqual(request.full_url,ENDPOINT)
        p = json.loads(json.loads(request.data)['input'])['document']['paragraphs'][1]
        result = {'version':VERSION,'assessment':'reviewed','summary':'Synthetic statement only.',
            'importance':{'level':'normal','reason':'Synthetic fixture'},
            'facts':[{'claim':p['text'],'paragraph_id':p['id'],'quote':p['text']}],
            'inferences':[],'counterevidence':[],'open_questions':[],
            'limitations':['Synthetic source, unverified facts.']}
        return {'model':MODEL,'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(result)}]}],
            'usage':{'input_tokens':100,'output_tokens':50,'total_tokens':150}}

    def transport(self,request,**kwargs):
        return io.BytesIO(json.dumps(self.payload(request,**kwargs)).encode())

    def records(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in self.executor.root.glob('execution-*.json')
                if p.name != 'execution-summary.json']

    def test_full_batch_is_sent_once_retained_and_correlated_but_never_human_scored(self):
        copy = self.executor.plan
        copy['prepared']['requests'].clear()
        with patch('urllib.request.OpenerDirector.open',side_effect=self.transport) as wire:
            report = self.executor.run()
        self.assertEqual(wire.call_count,36)
        self.assertTrue(report['cleanup_clean'])
        self.assertTrue(report['all_requests_responded'])
        self.assertTrue(report['execution_completed'])
        self.assertTrue(report['evidence_audit']['complete'])
        self.assertEqual(report['evidence_audit']['verified_responded'],36)
        self.assertEqual(report['evidence_audit']['output_contract_valid'],36)
        self.assertFalse(report['human_quality_verified'])
        self.assertEqual(len(self.records()),36)
        self.assertTrue(all(r['http_attempted'] and r['response_retained'] for r in self.records()))
        with self.assertRaisesRegex(ValueError,'already_started'):
            self.executor.run()
        with self.assertRaisesRegex(ValueError,'fresh_batch'):
            QualityBatchExecutor(self.batch,self.provider)

    def test_model_contract_failure_is_recorded_as_quality_evidence_not_a_transport_failure(self):
        def malformed(request,**kwargs):
            payload = self.payload(request,**kwargs)
            payload['output'][0]['content'][0]['text']='{"invented_contract":true}'
            return io.BytesIO(json.dumps(payload).encode())
        with patch('urllib.request.OpenerDirector.open',side_effect=malformed):
            report=self.executor.run()
        self.assertTrue(report['all_requests_responded'])
        self.assertEqual(report['evidence_audit']['output_contract_valid'],0)
        self.assertFalse(report['human_quality_verified'])
        self.assertTrue(all(r['output_contract_error'] for r in self.records()))

    def test_admitted_timeout_remains_unknown_no_retry_and_complete_missing_usage_evidence(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=TimeoutError('private supplier detail')) as wire:
            report=self.executor.run()
        self.assertEqual(wire.call_count,1)
        self.assertEqual(report['batch']['stop_code'],'quality_unknown')
        self.assertEqual(report['evidence_audit']['unknown'],1)
        self.assertTrue(report['evidence_audit']['complete'])
        self.assertEqual(report['batch']['calls_reserved'],1)
        self.assertNotIn('private supplier detail',json.dumps(self.records()))

    def test_late_last_response_cannot_turn_an_operator_stop_into_success(self):
        calls=0
        def late(request,**kwargs):
            nonlocal calls
            calls+=1
            if calls==36:
                self.executor.request_stop('quality_operator_interrupted')
            return self.transport(request,**kwargs)
        with patch('urllib.request.OpenerDirector.open',side_effect=late):
            report=self.executor.run()
        self.assertEqual(calls,36)
        self.assertTrue(report['all_requests_responded'])
        self.assertFalse(report['execution_completed'])
        self.assertEqual(report['batch']['stop_code'],'quality_operator_interrupted')

    def test_expiry_after_preflight_keeps_reservation_but_performs_no_http(self):
        def request(*args,**kwargs):
            bound=AuthorizedTextRequest(*args,**kwargs)
            original=bound.before_send
            def preflight():
                original()
                self.f.now+=10000
                self.f.mono+=10000
            bound.before_send=preflight
            return bound
        with patch('backend.news_review_quality_executor.AuthorizedTextRequest',side_effect=request), \
             patch('urllib.request.OpenerDirector.open') as wire:
            report=self.executor.run()
        wire.assert_not_called()
        self.assertEqual(report['batch']['calls_reserved'],1)
        self.assertEqual(report['batch']['stop_code'],'quality_window_expired')
        self.assertFalse(self.records()[0]['http_attempted'])

    def test_late_last_response_observes_expiry_even_before_next_watchdog_sample(self):
        calls=0
        def late(request,**kwargs):
            nonlocal calls
            calls+=1
            if calls==36:
                self.f.now+=10001
                self.f.mono+=10001
            return self.transport(request,**kwargs)
        with patch('urllib.request.OpenerDirector.open',side_effect=late):
            report=self.executor.run()
        self.assertEqual(calls,36)
        self.assertTrue(report['all_requests_responded'])
        self.assertFalse(report['execution_completed'])
        self.assertEqual(report['batch']['stop_code'],'quality_window_expired')

    def test_changed_retained_text_is_rejected_by_final_evidence_correlation(self):
        with patch('urllib.request.OpenerDirector.open',side_effect=self.transport):
            self.executor._run_one(self.f.request_id())
        self.assertTrue(self.executor._audit()['complete'])
        path=next(self.executor.root.glob('execution-*.json'))
        record=json.loads(path.read_text(encoding='utf-8'))
        record['content']='changed after receipt'
        path.write_text(json.dumps(record),encoding='utf-8')
        audit=self.executor._audit()
        self.assertFalse(audit['complete'])
        self.assertEqual(len(audit['invalid_attempt_evidence']),1)

    def test_stop_cancels_gate_before_any_blocked_persistence(self):
        attempted=threading.Event()
        release=threading.Event()
        def request(*args,**kwargs):
            bound=AuthorizedTextRequest(*args,**kwargs)
            original=bound.before_send
            def preflight():
                original()
                attempted.set()
                if not release.wait(10):
                    raise AssertionError('fixture not released')
            bound.before_send=preflight
            return bound
        with patch('backend.news_review_quality_executor.AuthorizedTextRequest',side_effect=request), \
             patch('urllib.request.OpenerDirector.open') as wire:
            worker=threading.Thread(target=lambda:self.executor._run_one(self.f.request_id()))
            worker.start()
            try:
                self.assertTrue(attempted.wait(10))
                self.executor.request_stop('quality_operator_interrupted')
                self.assertTrue(self.executor.send_gate.cancelled)
            finally:
                release.set()
                worker.join(10)
            self.assertFalse(worker.is_alive())
        wire.assert_not_called()
        self.assertEqual(self.records()[0]['status'],'CANCELLED')
        self.assertEqual(self.batch.snapshot()['calls_reserved'],1)

    def test_credential_echo_is_not_written_to_output_or_summary(self):
        def echo(request,**kwargs):
            value=self.payload(request,**kwargs)
            value['output'][0]['content'][0]['text']=self.provider._api_key
            return io.BytesIO(json.dumps(value).encode())
        with patch('urllib.request.OpenerDirector.open',side_effect=echo) as wire:
            report=self.executor.run()
        self.assertEqual(wire.call_count,1)
        self.assertEqual(report['batch']['stop_code'],'quality_credential_echo')
        self.assertFalse(self.records()[0]['response_retained'])
        for path in self.executor.root.glob('*.json'):
            self.assertNotIn(self.provider._api_key,path.read_text(encoding='utf-8'))

    def test_response_disk_failure_stops_and_does_not_complete_the_claim(self):
        original=self.executor._publish
        def publish(name,value):
            if name.startswith('execution-') and name != 'execution-summary.json':
                raise OSError('private disk detail')
            return original(name,value)
        with patch.object(self.executor,'_publish',side_effect=publish), \
             patch('urllib.request.OpenerDirector.open',side_effect=self.transport) as wire:
            report=self.executor.run()
        self.assertEqual(wire.call_count,1)
        self.assertEqual(report['batch']['stop_code'],'quality_response_persistence_failed')
        self.assertFalse(report['evidence_audit']['complete'])
        self.assertEqual(len(report['evidence_audit']['missing_attempt_evidence']),1)
        self.assertTrue(report['batch']['inflight_attempt_id'])
        self.assertFalse(report['all_requests_responded'])

    def test_timeout_retains_owner_while_worker_lives_and_produces_no_final_summary(self):
        started,release=threading.Event(),threading.Event()
        clock=[0.0]
        def stuck(request,**kwargs):
            started.set()
            clock[0]=300.0
            if not release.wait(10):
                raise AssertionError('fixture not released')
            return self.transport(request,**kwargs)
        try:
            with patch('backend.news_review_quality_executor.time',SimpleNamespace(monotonic=lambda:clock[0])), \
                 patch('urllib.request.OpenerDirector.open',side_effect=stuck), \
                 self.assertRaises(QualityShutdownIncomplete):
                self.executor.run()
            self.assertTrue(started.is_set())
            self.assertTrue(self.executor._worker.is_alive())
            self.f.owner.assert_held_for(self.f.path)
            self.assertFalse((self.executor.root/'execution-summary.json').exists())
            value=json.loads((self.executor.root/'drain-incomplete.json').read_text(encoding='utf-8'))
            self.assertFalse(value['owner_release_allowed'])
        finally:
            release.set()
            if self.executor._worker:
                self.executor._worker.join(10)
        self.assertFalse(self.executor._worker.is_alive())
        self.assertEqual(self.batch.admission_state()['stop_code'],'quality_request_timeout')


if __name__ == '__main__':
    unittest.main()
