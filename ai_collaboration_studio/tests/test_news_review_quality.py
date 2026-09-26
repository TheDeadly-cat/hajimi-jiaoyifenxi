"""Fixed synthetic cases through native import, parser, queue, ledger and projection.

Scripted provider output exercises engineering contracts only. It cannot score
the model's understanding; the reference answers require human approval.
"""
import html
import io
import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.news_review_contracts import freshness, validate_result, VERSION
from backend.document_evidence import RECHECK_MS
from backend.source_inbox_service import SourceInboxService
from backend.source_monitoring.packet_builder import build_source_import_packet
from tests import test_news_review as fixtures
from tests.test_document_evidence import event

CORPUS_PATH = Path(__file__).parent/'fixtures/news_review_quality_v1.json'
CORPUS = json.loads(CORPUS_PATH.read_text(encoding='utf-8'))


class QualityCorpusReplay(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.NewsReviewTests()
        self.f.addCleanup = self.addCleanup
        self.f.setUp()

    def replay(self, case):
        f = self.f
        body = case['body']
        def markup(text):
            if case['body_mode']=='missing':
                return b'<html><body></body></html>'
            end = '' if case['body_mode']=='truncated' else '</body></html>'
            return ('<html><body><p>Item 8.01 Other Events</p><p>'+html.escape(text)+
                '</p><p>Synthetic offline filing for contract evaluation. This document does not describe an actual issuer event.</p>'+end).encode()
        f.raw = markup(body)
        f.approve()
        observed = event()
        published = f.now-case['age_hours']*3_600_000
        observed['published_at'] = datetime.fromtimestamp(published/1000, timezone.utc).isoformat()
        packet = build_source_import_packet(adapter_key='sec_filings', external_run_id=case['id'],
            captured_at_ms=f.now, observed_items=[observed])
        result = SourceInboxService(f.store, clock=lambda:f.now/1000).import_packet(json.dumps(packet), actor='source_monitoring_worker')
        item = result['items'][0]['id']
        f.service.discover(f.policy['policy_id'])
        document = f.service.request_document(f.policy['policy_id'], item)
        f.documents.run(document, cancel_event=f.cancel, deadline_monotonic_ms=int(time.monotonic()*1000)+12_000)
        first = f.service.queue(f.policy['policy_id'], item)
        def provider(request, **kwargs):
            native = json.loads(json.loads(request.data)['input'])['document']
            paragraph = native['paragraphs'][1]
            reference = case['reference_draft']
            scripted = {'version':VERSION,'assessment':reference['assessment'],
                'summary':reference['must_preserve'],
                'importance':{'level':reference['importance'],'reason':reference['must_preserve']},
                'facts':[{'claim':paragraph['text'],'quote':paragraph['text'],'paragraph_id':paragraph['id']}],
                'inferences':[],'counterevidence':[],'open_questions':[reference['must_preserve']],
                'limitations':['Synthetic fixture. Only supplied main HTML; attachments and external facts are unverified.']}
            response = {'id':'synthetic-quality-'+case['id'],'model':f.policy['model'],'status':'completed',
                'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(scripted)}]}],
                'usage':{'input_tokens':100,'output_tokens':50}}
            return io.BytesIO(json.dumps(response).encode())
        calls = f.run_one(provider).call_count
        expected_calls = 0 if case['body_mode']!='complete' else 1
        self.assertEqual(calls, expected_calls)
        self.assertEqual(f.view(item)['state'], 'MATERIAL_INSUFFICIENT' if case['reference_draft']['assessment']=='material_insufficient' else 'REVIEWED')
        if case['workflow'] in {'duplicate','revision','restore'}:
            states = [body] if case['workflow']=='duplicate' else [body.replace('USD 10 million','USD 11 million')]
            if case['workflow']=='restore':
                states.append(body)
            for text in states:
                f.now += RECHECK_MS+1
                f.raw = markup(text)
                doc = f.documents.request(item, session_id='offline-quality', confirmation=True, refresh=True)
                f.documents.run(doc['id'], cancel_event=f.cancel, deadline_monotonic_ms=int(time.monotonic()*1000)+12_000)
                queued = f.service.queue(f.policy['policy_id'], item)
                calls += f.run_one(provider).call_count
                if text==body:
                    self.assertEqual(queued, first)
                    self.assertEqual(f.view(item)['current_review_id'], first)
            expected_calls = 1 if case['workflow']=='duplicate' else 2
        self.assertEqual(calls, expected_calls)
        self.assertEqual(f.count('provider_call_attempts'), expected_calls)
        self.assertEqual(f.count('news_review_receipts'), expected_calls)
        self.assertEqual(f.service.snapshot(f.policy['policy_id'])['calls_reserved'], expected_calls)
        fresh = freshness({'item':observed,'created_at':published+case['age_hours']*3_600_000}, published+case['age_hours']*3_600_000)
        self.assertEqual(fresh['clock_anomaly'], case['age_hours']<0)
        if case['age_hours']>24:
            self.assertEqual(fresh['publication_age'], 'older_than_24h')


def _test(case):
    def run(self):
        self.replay(case)
    return run

for _case in CORPUS['cases']:
    setattr(QualityCorpusReplay, 'test_'+_case['id'], _test(_case))


class QualityBoundaryTests(unittest.TestCase):
    def test_fixed_unique_36_cases_with_unapproved_references(self):
        cases = CORPUS['cases']
        self.assertEqual(len(cases),36)
        self.assertEqual(len({c['id'] for c in cases}),36)
        for case in cases:
            self.assertTrue(case['synthetic'])
            self.assertIsNone(case['reference_draft']['human_approved_at'])

    def test_exact_quote_does_not_prove_semantic_correctness(self):
        paragraph = {'id':'p1','text':'Revenue was USD 10 million in Q2 2026.'}
        result = {'version':VERSION,'assessment':'reviewed','summary':'Incorrect synthetic claim',
            'importance':{'level':'high','reason':'results'},
            'facts':[{'claim':'Revenue was USD 10 billion in Q3 2026.', 'paragraph_id':'p1','quote':paragraph['text']}],
            'inferences':[],'counterevidence':[],'open_questions':[], 'limitations':['Offline semantic negative control.']}
        self.assertEqual(validate_result(json.dumps(result), {'paragraphs':[paragraph]}),result)
