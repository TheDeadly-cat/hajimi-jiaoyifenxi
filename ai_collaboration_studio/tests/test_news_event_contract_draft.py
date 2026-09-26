import copy
import unittest
from backend.news_event_contract_draft import MockEventAdapter,DraftEventHistory,validate_event


class DraftContractTests(unittest.TestCase):
    def event(self, **changes):
        value={'version':'news_event_contract_draft_v1','market':'TEST-US','publisher':'fixture-sec',
            'native_id':'0001','revision_id':'v1','previous_revision_id':None,'published_at_ms':1000,
            'observed_at_ms':2000,'content_sha256':'a'*64,'status':'published','synthetic':True}
        return value|changes

    def test_duplicate_across_polls_and_market_namespaces(self):
        history=DraftEventHistory()
        first=history.observe(self.event())
        duplicate=history.observe(self.event(observed_at_ms=3000))
        other=history.observe(self.event(market='TEST-HK'))
        self.assertTrue(duplicate['duplicate'])
        self.assertNotEqual(first['event_key'],other['event_key'])
        self.assertFalse(first['new_paid_request_authorized'])

    def test_revision_restore_reuses_content_key_and_retains_history(self):
        history=DraftEventHistory()
        a=history.observe(self.event())
        b=history.observe(self.event(revision_id='v2',previous_revision_id='v1',content_sha256='b'*64))
        restored=history.observe(self.event(revision_id='v3',previous_revision_id='v2'))
        self.assertNotEqual(a['current']['review_content_key'],b['current']['review_content_key'])
        self.assertEqual(a['current']['review_content_key'],restored['current']['review_content_key'])
        self.assertEqual(len(history.history[a['event_key']]),3)

    def test_retraction_preserves_prior_review_and_does_not_authorize_model(self):
        history=DraftEventHistory()
        original=history.observe(self.event())
        r=history.observe(self.event(revision_id='v2',previous_revision_id='v1',status='retracted',content_sha256=None))
        self.assertIsNone(r['current']['review_content_key'])
        self.assertEqual(len(history.history[original['event_key']]),2)
        self.assertFalse(r['new_paid_request_authorized'])

    def test_missing_revision_and_identity_collision_rejected(self):
        history=DraftEventHistory()
        history.observe(self.event())
        for e in (self.event(content_sha256='b'*64),self.event(revision_id='v3',previous_revision_id='v2')):
            with self.assertRaises(ValueError): history.observe(e)

    def test_real_event_rejected_and_mock_is_copy_isolated(self):
        with self.assertRaises(ValueError): validate_event(self.event(synthetic=False))
        adapter=MockEventAdapter([self.event()])
        adapter.poll()[0]['market']='modified'
        self.assertEqual(adapter.poll()[0]['market'],'TEST-US')

    def test_future_publication_is_unconfirmed_and_negative_time_rejected(self):
        result=DraftEventHistory().observe(self.event(published_at_ms=3000))
        self.assertTrue(result['current']['publication_clock_unconfirmed'])
        with self.assertRaises(ValueError): validate_event(self.event(observed_at_ms=-1))
