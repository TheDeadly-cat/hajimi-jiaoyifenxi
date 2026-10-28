"""Offline integration: exact article evidence enters the existing budget ledger."""
import json
import socket
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.news_platform import NewsPlatform
from backend.news_feed_transport import FeedResponse
from backend.news_platform_ai import NewsAnalysis, MODEL
from backend import news_evidence_reading as reading
from backend.news_content_quality import validate_output


class EvidenceReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('live_network_forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.now = time.time() - 20
        self.excerpt = 'Original synthetic announcement with a stated condition.'
        def transport(*args):
            return FeedResponse(200, ('<rss><channel><item><guid>fixed</guid><title>Official test</title>'
                '<link>https://www.federalreserve.gov/newsevents/test.htm</link>'
                '<pubDate>Wed, 28 Oct 2026 00:00:00 GMT</pubDate><description>' + self.excerpt + '</description></item></channel></rss>').encode())
        self.news = NewsPlatform(Path(self.temp.name)/'news.db',transport=transport,clock=lambda:self.now)
        self.fetch()
        self.item_id = self.news.list_items()['items'][0]['id']
        self.calls = []
        def generate(key,text):
            self.calls.append(json.loads(text))
            material=self.calls[-1]
            field='body' if 'body' in material['source_text'] else 'excerpt'
            quote=material['source_text'][field].split('\n')[0][:80]
            value={'summary':'这是测试公告中的信息。','key_points':[{'claim':'公告中保留的说明。','evidence_field':field,'quote':quote}], 'uncertainties':['仅限本次已读材料。']}
            return SimpleNamespace(ok=True,model=MODEL,content=json.dumps(value,ensure_ascii=False))
        self.ai = NewsAnalysis(Path(self.temp.name)/'ai.db',self.news,generate=generate)
        self.addCleanup(self.ai.stop)

    def fetch(self):
        with self.news._db() as db:
            source=db.execute("SELECT * FROM sources WHERE id='fed'").fetchone()
        self.news._fetch(source)

    def evidence(self, text='A synthetic official statement with full conditional evidence.'):
        return {'content_status':'full_text','body_text':text,'content_hash':'body-1',
                'extraction_version':'fixture-v1','segments':[{'id':'p1','text':text,'kind':'paragraph'}],
                'attachments':[{'url':'https://www.federalreserve.gov/attachment.pdf','status':'not_read','read':False}]}

    def test_body_reading_is_located_and_repeat_across_models_is_spent(self):
        self.now=time.time()+1
        self.news.save_article_evidence(self.item_id,self.evidence())
        self.ai.configure('PUBLIC_FAKE_TEST_ONLY',False,True)
        self.ai._analyze(self.item_id,task_id=self.news.analysis_task_for_item(self.item_id)['id'])
        current=self.news.get_item(self.item_id)
        result=self.ai.result(current)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['evidence'][0]['locator']['segment_id'],'p1')
        self.assertTrue(result['scope']['full_text_read'])
        self.assertFalse(result['scope']['attachments'][0]['read'])
        self.ai.configure('PUBLIC_FAKE_TEST_ONLY',False,True,provider='deepseek',model='deepseek-flash')
        self.ai._analyze(self.item_id)
        self.assertEqual(len(self.calls),1)

    def test_old_article_revision_after_configuration_runs_exact_snapshot(self):
        old=self.news.get_item(self.item_id)
        self.ai.configure('PUBLIC_FAKE_TEST_ONLY',True,True)
        self.news.control(True)
        self.now=time.time()+1
        self.excerpt='An important corrected condition from the same older announcement.'
        self.fetch()
        task=self.news.analysis_task_for_item(self.item_id)
        self.ai._next_persistent_task(self.ai._configured_at,self.ai._generation)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['source_text']['excerpt'],self.excerpt)
        self.assertEqual(self.news.get_item(self.item_id)['discovered_at'],old['discovered_at'])
        self.assertEqual(self.news.analysis_task_for_item(self.item_id)['status'],'completed')

    def test_long_body_selection_and_quote_boundary(self):
        item=self.news.get_item(self.item_id)
        item['evidence']=self.evidence('正文条件和限制。'*5000)
        material=reading.build_input(item,text_budget=500)
        self.assertLessEqual(material['evidence_package']['body_utf8_bytes'],500)
        self.assertTrue(material['evidence_package']['truncated'])
        bad=json.dumps({'summary':'测试','key_points':[{'claim':'测试','evidence_field':'body','quote':'missing fabricated quote'}],'uncertainties':['局部材料']})
        with self.assertRaisesRegex(ValueError,'quote_not_in_source'):
            validate_output(bad,material)

    def test_interrupted_task_stays_unknown_without_resend(self):
        task=self.news.analysis_task_for_item(self.item_id)
        self.news.transition_analysis_task(task['id'],'running',expected_status='pending')
        self.news.recover_analysis_tasks()
        self.ai.configure('PUBLIC_FAKE_TEST_ONLY',False,True)
        result=self.ai.request(self.item_id)
        self.assertEqual(result['status'],'unknown')
        self.assertEqual(self.calls,[])

    def test_market_task_uses_shared_budget_and_same_binding_does_not_recharge(self):
        from backend.news_market_context import project_result
        from backend.news_market_tasks import fingerprint
        context=self.ai.prepare_market_context(self.item_id)
        self.ai.configure('PUBLIC_FAKE_TEST_ONLY',False,True)
        def generate(key,text):
            material=json.loads(text)
            self.calls.append(material)
            self.assertEqual(material['scope'],'market_impact_from_saved_context')
            self.assertFalse(material['has_consensus'])
            value=project_result()
            evidence=material['evidence'][0]
            value['facts']=[{'claim':'给定合成公告中的说明。','evidence_id':evidence['id'],'quote':evidence['text']}]
            return SimpleNamespace(ok=True,model=MODEL,content=json.dumps(value,ensure_ascii=False))
        self.ai._generate=generate
        self.assertEqual(self.ai.request_market(self.item_id,context['context_id'])['status'],'queued')
        self.ai._analyze(self.item_id,task_id=fingerprint(context['context_id']),market_context_id=context['context_id'])
        result=self.ai.market_result(self.news.get_item(self.item_id))
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(self.ai.status()['calls_reserved'],1)
        self.assertEqual(result['expectation_comparison']['status'],'unknown')
        self.now+=10
        rebound=self.ai.prepare_market_context(self.item_id)
        self.assertEqual(rebound['context_id'],context['context_id'])
        self.assertEqual(self.ai.request_market(self.item_id,rebound['context_id'])['status'],'completed')
        self.assertEqual(len(self.calls),1)

    def test_market_unknown_is_retained_and_too_long_task_does_not_starve_queue(self):
        from backend.news_market_tasks import fingerprint
        context=self.ai.prepare_market_context(self.item_id)
        self.ai.configure('PUBLIC_FAKE_TEST_ONLY',False,True)
        def uncertain(*args):
            self.calls.append('attempt')
            raise TimeoutError('synthetic uncertain response')
        self.ai._generate=uncertain
        self.ai.request_market(self.item_id,context['context_id'])
        self.ai._analyze(self.item_id,task_id=fingerprint(context['context_id']),market_context_id=context['context_id'])
        self.assertEqual(self.ai.market_result(self.news.get_item(self.item_id))['status'],'unknown')
        self.ai.request_market(self.item_id,context['context_id'])
        self.assertEqual(len(self.calls),1)
        self.now=time.time()-1
        self.excerpt='Long synthetic '+('明示条件 '*6000)
        self.fetch()
        task=self.news.analysis_task_for_item(self.item_id)
        self.ai._analyze(self.item_id,task_id=task['id'])
        self.assertEqual(self.news.analysis_task_for_item(self.item_id)['status'],'needs_review')
        self.assertEqual(self.news.analysis_task_for_item(self.item_id)['reason'],'request_size_limit_no_send')
        self.assertEqual(self.ai.status()['calls_reserved'],1)

    def test_relation_confirmation_keeps_prior_paid_reading_and_history(self):
        from backend.news_event_actions import confirm_relation
        self.news.ai=self.ai
        self.ai.configure('PUBLIC_FAKE_TEST_ONLY',False,True)
        self.ai._analyze(self.item_id,task_id=self.news.analysis_task_for_item(self.item_id)['id'])
        old=self.news.get_item(self.item_id)
        # A second saved original, discovered through the actual feed parser.
        self.news.transport=lambda *args:FeedResponse(200,b'<rss><channel><item><guid>other</guid><title>Original release</title><link>https://www.federalreserve.gov/original.htm</link><description>Original synthetic announcement with a stated condition.</description></item></channel></rss>')
        self.now=time.time()-1
        self.fetch()
        origin=next(row for row in self.news.list_items()['items'] if row['id']!=self.item_id)
        merged=confirm_relation(self.news,self.item_id,origin['id'])
        self.assertEqual(merged['event']['independent_evidence_count'],1)
        self.assertEqual(self.ai.result(merged)['status'],'completed')
        self.ai._analyze(self.item_id)
        self.assertEqual(len(self.calls),1)
        previous=next(v for v in self.news.event_versions(self.item_id) if v['id']==old['current_version_id'])
        self.assertEqual(self.ai.version_result(merged,previous)['status'],'completed')

    def test_withdrawal_requires_saved_notice_and_prevents_new_normal_summary(self):
        from backend.news_event_actions import confirm_withdrawal
        with self.assertRaises(ValueError):
            confirm_withdrawal(self.news,self.item_id,self.item_id,'This announcement was withdrawn.')
        self.now=time.time()-1
        self.excerpt='This synthetic announcement was withdrawn by the publisher.'
        self.fetch()
        value=confirm_withdrawal(self.news,self.item_id,self.item_id,self.excerpt)
        self.assertTrue(value['event']['retracted'])
        self.ai.configure('PUBLIC_FAKE_TEST_ONLY',False,True)
        self.assertEqual(self.ai.request(self.item_id)['status'],'needs_review')
        self.assertEqual(self.calls,[])


if __name__ == '__main__':
    unittest.main()
