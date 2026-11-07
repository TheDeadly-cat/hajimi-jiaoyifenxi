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

from backend.news_platform import NewsPlatform, analyze
from backend import news_event_store
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
            transport=lambda *args:self.fail('Saved market pipeline must not discover sources'))
        self.news.research_store = self.store
        self.news.configure_source('ir_mu',True)
        self.item_id='market-pipeline'
        with self.news._db() as db:
            published=ms('2026-10-29T15:50:00Z')/1000
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.item_id,'ir_mu',self.item_id,'Micron quarterly revenue',ITEM['url'],ITEM['excerpt'],published,self.now,self.now,
                 json.dumps(analyze('Micron quarterly revenue',ITEM['excerpt'],published,self.now,'ir_mu'))))
            item=dict(db.execute('SELECT * FROM items WHERE id=?',(self.item_id,)).fetchone())
            news_event_store.record_version(db,item,self.now,metadata={'symbols':['US.MU'],'published_time_source':'saved_fixture'})
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

    def test_explicit_historical_request_keeps_cutoff_freshness_and_shared_spent_identity(self):
        from backend.news_article_evidence import extract_article
        from backend.news_market_context import build_market_input
        from backend.news_platform_routes import news_response
        first=self.news.get_item(self.item_id)
        self.now+=3600
        later=extract_article(b'<article><p>Later corrected value 99 is not known at the historical cutoff.</p></article>',ITEM['url'],'ir_mu',now=self.now)
        self.news.save_article_evidence(self.item_id,later)
        history=self.ai.prepare_market_context(self.item_id,**self.bindings,version_id=first['current_version_id'],as_of=ASOF)
        self.assertEqual(history['mode'],'historical_replay')
        with self.assertRaisesRegex(ValueError,'historical_market_confirmation_required'):
            self.ai.request_market(self.item_id,history['context_id'])
        self.assertEqual(self.ai.status()['calls_reserved'],0)
        self.assertEqual(self.ai.request_market(self.item_id,history['context_id'],acknowledge_historical=True)['status'],'queued')
        with patch('backend.news_platform_ai.datetime',wraps=datetime) as clock:
            clock.now.return_value=datetime.fromtimestamp(self.now,timezone.utc)
            self.ai._analyze(self.item_id,task_id=fingerprint(history['context_id']),market_context_id=history['context_id'])
        result=self.ai.market_result(first,history['context_id'])
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(len(self.calls),1)
        sent=self.calls[0]
        self.assertEqual(sent['mode'],'historical_replay')
        self.assertEqual(sent['as_of'],ASOF);self.assertTrue(sent['has_market'])
        self.assertEqual(sent['freshness']['evaluated_at'],ASOF)
        self.assertNotIn('Later corrected value 99',json.dumps(sent))
        self.assertIsNone(self.ai.market_context(self.news.get_item(self.item_id)))
        self.assertEqual(self.ai.request_market(self.item_id,history['context_id'],acknowledge_historical=True)['status'],'completed')
        self.assertEqual(self.ai.status()['calls_reserved'],1)
        self.news.ai=self.ai
        body,status=news_response(self.news,f'/api/news/items/{self.item_id}/market/context',method='POST',
            payload={**self.bindings,'material_refs':[{'material_id':ref['id'],'version':ref['version']} for ref in self.bindings['material_refs']],
                     'version_id':first['current_version_id'],'as_of':ASOF})
        self.assertEqual(status,200);self.assertEqual(body['market_impact']['status'],'completed')
        self.assertEqual(len(self.calls),1)


if __name__=='__main__':
    unittest.main()
