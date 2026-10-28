"""Offline saved evidence -> user market request -> shared ledger integration."""
import json
import os
import socket
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ['AI_STUDIO_SKIP_LOCAL_ENV'] = '1'

from backend.news_platform import NewsPlatform
from backend.news_platform_ai import NewsAnalysis, MODEL
from backend.news_feed_transport import FeedResponse
from backend.news_market_context import project_result
from backend.news_market_tasks import fingerprint
from backend import news_analysis_providers as providers
from tests.test_news_market_context import FrozenStore, ASOF, ITEM, ms


class MarketPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        network = patch.object(socket.socket, 'connect', side_effect=AssertionError('live_network_forbidden'))
        network.start(); self.addCleanup(network.stop)
        self.now = ms(ASOF) / 1000
        self.store = FrozenStore(); self.store.add_actual()
        self.news = NewsPlatform(Path(self.temp.name) / 'news.db', clock=lambda:self.now,
            transport=lambda *args:FeedResponse(200, (
                '<rss><channel><item><guid>market-pipeline</guid><title>Micron quarterly revenue</title>'
                '<link>'+ITEM['url']+'</link><pubDate>Thu, 29 Oct 2026 15:50:00 GMT</pubDate>'
                '<description>'+ITEM['excerpt']+'</description></item></channel></rss>').encode()))
        self.news.research_store = self.store
        self.news.configure_source('ir_mu',True)
        with self.news._db() as db:
            source = db.execute("SELECT * FROM sources WHERE id='ir_mu'").fetchone()
        self.news._fetch(source)
        self.item_id = self.news.list_items()['items'][0]['id']
        self.calls=[]
        def generate(key,text):
            material=json.loads(text);self.calls.append(material)
            if material['scope']=='market_impact_from_saved_context':
                comparison=material['comparisons'][0]
                evidence={row['id']:row for row in material['evidence']}
                result=project_result()
                result['direct_symbols']=['US.MU']
                result['expectation_comparison']={'status':'evidence_supported','claim':comparison['claim'],
                    'evidence':[{'evidence_id':key,'quote':evidence[key]['text']}
                                for key in (comparison['actual_evidence_id'],comparison['consensus_evidence_id'])]}
            else:
                result={'summary':'合成公告记录了季度收入。','key_points':[
                    {'claim':'合成公告中的季度说明。','evidence_field':'excerpt','quote':material['source_text']['excerpt']}],
                    'uncertainties':['只读给定合成材料，未核对完整公告。']}
            return SimpleNamespace(ok=True,model=MODEL,content=json.dumps(result,ensure_ascii=False))
        self.ai_path=Path(self.temp.name)/'ai.db'
        self.ai=NewsAnalysis(self.ai_path,self.news,generate=generate)
        self.addCleanup(self.ai.stop)
        self.ai.configure('PUBLIC_OFFLINE_FIXTURE_ONLY',False,True)
        self.bindings={'room_id':'room1','round_id':'round1','material_refs':[{'id':'consensus1','version':1},{'id':'actual1','version':1}]}

    def run_market(self,context):
        with patch('backend.news_platform_ai.datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime.fromisoformat(ASOF.replace('Z','+00:00'))
            self.ai._analyze(self.item_id,task_id=fingerprint(context['context_id']),market_context_id=context['context_id'])

    def test_saved_comparison_pipeline_wire_and_shared_ledger(self):
        context=self.ai.prepare_market_context(self.item_id,**self.bindings)
        self.assertNotEqual(context['context_id'],context['evidence_context_id'])
        self.assertEqual(len(context['comparisons']),1)
        self.assertEqual(self.ai.request_market(self.item_id,context['context_id'])['status'],'queued')
        sizes=[]
        original=providers.encode_body
        def encode(body):
            encoded=original(body);sizes.append(len(encoded));return encoded
        with patch.object(providers,'encode_body',side_effect=encode):
            self.run_market(context)
        result=self.ai.market_result(self.news.get_item(self.item_id))
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(result['expectation_comparison']['status'],'evidence_supported')
        self.assertEqual(len(self.calls),1)
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes),16000)
        self.assertEqual(self.ai.status()['calls_reserved'],1)
        self.ai._analyze(self.item_id,task_id=self.news.analysis_task_for_item(self.item_id)['id'])
        self.assertEqual(self.ai.status()['calls_reserved'],2)
        self.assertEqual(self.ai.result(self.news.get_item(self.item_id))['status'],'completed')
        self.ai.configure('PUBLIC_OFFLINE_FIXTURE_ONLY',False,True,provider='deepseek',model='deepseek-flash')
        self.assertEqual(self.ai.request_market(self.item_id,context['context_id'])['status'],'completed')
        self.run_market(context)
        self.assertEqual(self.ai.status()['calls_reserved'],2)
        self.assertEqual(len(self.calls),2)

    def test_equivalent_references_and_later_bind_reuse_exact_identity(self):
        first=self.ai.prepare_market_context(self.item_id,**self.bindings)
        self.now+=10
        second=self.ai.prepare_market_context(self.item_id,room_id='room1',round_id='round1',material_refs=[
            {'material_id':'actual1','version':1},{'material_id':'consensus1','version':1},
            {'id':'actual1','version':1}])
        self.assertEqual(first['context_id'],second['context_id'])
        self.assertEqual(first['evidence_context_id'],second['evidence_context_id'])
        with self.news._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM news_market_contexts').fetchone()[0],1)

    def test_interrupted_market_reservation_and_task_remain_unknown_after_reopen(self):
        context=self.ai.prepare_market_context(self.item_id,**self.bindings)
        self.ai.request_market(self.item_id,context['context_id'])
        key=fingerprint(context['context_id'])
        self.news.transition_analysis_task(key,'running',expected_status='pending')
        with self.ai._db() as db:
            db.execute("INSERT INTO runs(fingerprint,item_id,created,reserved,status) VALUES(?,?,?,?,?)",
                       (key,self.item_id,time.time(),1000,'pending'))
        reopened=NewsAnalysis(self.ai_path,self.news,generate=lambda *args:self.fail('unknown must not resend'))
        self.addCleanup(reopened.stop)
        reopened.configure('PUBLIC_OFFLINE_FIXTURE_ONLY',False,True)
        self.assertEqual(reopened.request_market(self.item_id,context['context_id'])['status'],'unknown')
        reopened._analyze(self.item_id,task_id=key,market_context_id=context['context_id'])
        self.assertEqual(reopened.market_result(self.news.get_item(self.item_id))['status'],'unknown')
        self.assertEqual(reopened.status()['calls_reserved'],1)
        self.assertEqual(self.calls,[])


if __name__=='__main__':
    unittest.main()
