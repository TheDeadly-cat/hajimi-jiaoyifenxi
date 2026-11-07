"""Offline focus, one-route admission and bounded recovery against real queues."""
import json
import socket
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import news_analysis_policy as policy
from backend import news_analysis_providers as providers
from backend import news_event_store as events
from backend import news_platform_ai as ai_module
from backend.news_platform import NewsPlatform,analyze
from backend.news_platform_ai import NewsAnalysis,MAX_CALLS,BUDGET_MICRO_CNY
from backend.news_market_context import project_result


class NewsFocusRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='news-focus-offline-')
        self.addCleanup(self.temp.cleanup)
        self.now=datetime(2026,11,7,tzinfo=timezone.utc).timestamp()
        self.monotonic=1000
        self.calls=[]
        for target,name in ((socket.socket,'connect'),(socket,'create_connection'),(socket,'getaddrinfo'),
                            (urllib.request.OpenerDirector,'open'),(providers,'generate_response'),(threading.Thread,'start')):
            guard=patch.object(target,name,side_effect=AssertionError('external_execution_forbidden'))
            guard.start();self.addCleanup(guard.stop)
        clock=patch.object(ai_module,'time',SimpleNamespace(time=lambda:self.now,monotonic=lambda:self.monotonic))
        clock.start();self.addCleanup(clock.stop)
        self.news=NewsPlatform(Path(self.temp.name)/'news.db',clock=lambda:self.now,
            transport=lambda *args: self.fail('source_request_forbidden'))
        self.ai=NewsAnalysis(Path(self.temp.name)/'analysis.db',self.news,generate=self.generate)
        self.addCleanup(self.ai.stop)
        self.news.control(True)

    def generate(self,key,text):
        material=json.loads(text);self.calls.append(material)
        if material.get('scope')=='market_impact_from_saved_context':
            value=project_result()
        else:
            value={'summary':'离线合成消息说明。','key_points':[{'claim':'材料中的原始说明。',
                'evidence_field':'excerpt','quote':material['source_text']['excerpt']}],
                'uncertainties':['仅为合成测试，未读取真实来源或调用真实模型。']}
        return SimpleNamespace(ok=True,provider=self.ai._provider,model=self.ai._model,
            requested_model=self.ai._model,reported_model=self.ai._model,content=json.dumps(value,ensure_ascii=False))

    def advance(self,seconds=1):
        self.now+=seconds;self.monotonic+=seconds

    def configure(self,automatic=False):
        return self.ai.configure('SYNTHETIC_FOCUS_KEY_NEVER_VALID',automatic,True,provider='deepseek',model='deepseek-flash')

    def put(self,identifier,title='Inflation report',excerpt='The synthetic report states a measured change.',source='fed',historical=False):
        self.advance()
        published=self.now-172800 if historical else self.now
        with self.news._db() as db:
            existing=db.execute('SELECT discovered FROM items WHERE id=?',(identifier,)).fetchone()
            discovered=existing[0] if existing else self.now
            row=(identifier,source,identifier,title,'https://www.federalreserve.gov/'+identifier+'.htm',excerpt,published,discovered,self.now,
                 json.dumps(analyze(title,excerpt,published,self.now,source)))
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(id) DO UPDATE SET title=excluded.title,excerpt=excluded.excerpt,published=excluded.published,updated=excluded.updated,analysis=excluded.analysis',row)
            item=dict(db.execute('SELECT * FROM items WHERE id=?',(identifier,)).fetchone())
            events.record_version(db,item,self.now,metadata={'symbols':['US.MU'] if source=='ir_mu' else []})
        return self.news.get_item(identifier)

    def auto_once(self):
        self.ai._next_persistent_task(self.ai._configured_at,self.ai._generation)

    def dispatch(self):
        while not self.ai._queue.empty():
            item,generation,*extra=self.ai._queue.get_nowait()
            self.ai._analyze(item,generation=generation,task_id=extra[0] if extra else None,
                market_context_id=extra[1] if len(extra)>1 else None,recovery_token=extra[2] if len(extra)>2 else None)

    def test_focus_catalog_and_four_dimensions_do_not_enable_sources(self):
        before=[(s['id'],s['enabled']) for s in self.news.overview()['sources']]
        result=self.news.configure_focus({'all_registered':False,'companies':['micron'],'symbols':['mu'],
            'industries':['storage'],'macro_topics':['inflation'],'automatic_scope':'market'})
        self.assertEqual(result['focus']['symbols'],['US.MU'])
        self.assertEqual([s['id'] for s in result['catalog']['companies']],['micron','sandisk','western_digital','seagate'])
        self.assertEqual(before,[(s['id'],s['enabled']) for s in self.news.overview()['sources']])
        item=self.put('company','Micron earnings report',source='ir_mu')
        decision=item['analysis_policy']
        self.assertEqual((decision['relevance'],decision['novelty'],decision['event_type']),('direct','new_event','earnings'))
        self.assertEqual(decision['evidence']['status'],'excerpt_only')
        self.assertTrue(decision['automatic_eligible'])
        with self.assertRaisesRegex(ValueError,'choice'):
            self.news.configure_focus({'industries':['unregistered-sector']})

    def test_mode_reuses_key_without_extending_authorization_or_resetting_budget(self):
        self.configure();self.put('one');self.ai.request('one');self.dispatch()
        before=(self.ai._key,self.ai._configured_at,self.ai._expires_at,self.ai._deadline,self.ai._generation)
        used=self.ai.status()['reserved_cny'];self.advance(90)
        enabled=self.ai.set_mode(True,True)
        self.assertTrue(enabled['automatic'])
        self.assertEqual(before,(self.ai._key,self.ai._configured_at,self.ai._expires_at,self.ai._deadline,self.ai._generation))
        self.assertEqual(enabled['reserved_cny'],used)
        self.ai.set_mode(False,False)
        self.assertTrue(self.ai.status()['configured'])
        self.assertEqual(self.ai.status()['pause_reason'],'manual_mode')
        self.assertEqual(self.ai.status()['provider'],'deepseek')
        self.advance(86401)
        with self.assertRaisesRegex(ValueError,'session_required'):self.ai.set_mode(True,True)

    def test_automatic_admission_keeps_ordinary_unrelated_duplicate_and_backfill_free(self):
        self.news.configure_focus({'all_registered':False,'macro_topics':['inflation']})
        self.configure(True)
        self.put('ordinary','Meeting schedule')
        self.put('unrelated','Micron earnings report',source='ir_mu')
        self.put('history',historical=True)
        self.put('duplicate');self.put('duplicate','Inflation report revised headline')
        self.put('important')
        for _ in range(8):self.auto_once()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.ai.status()['calls_reserved'],1)
        self.assertEqual(self.news.analysis_task_for_item('important')['status'],'completed')
        self.assertEqual(self.news.get_item('duplicate')['analysis_policy']['novelty'],'duplicate_confirmation')
        self.assertEqual(self.ai.status()['pending']['held'],4)

    def test_substantive_revision_is_processed_before_new_events(self):
        self.configure(True)
        self.put('revised');self.put('new')
        self.put('revised',excerpt='The synthetic report corrects the measured change.')
        self.auto_once()
        self.assertIn('corrects',self.calls[0]['source_text']['excerpt'])
        self.assertEqual(self.news.get_item('revised')['analysis_policy']['novelty'],'material_revision')

    def test_automatic_market_uses_one_main_call_and_no_fact_call(self):
        self.news.configure_focus({'automatic_scope':'market'})
        self.configure(True);self.put('market')
        self.auto_once();self.auto_once()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['scope'],'market_impact_from_saved_context')
        result=self.ai.market_result(self.news.get_item('market'))
        self.assertEqual(result['status'],'completed',result)
        self.assertEqual(self.ai.status()['calls_reserved'],1)
        self.assertIsNone(self.ai.result(self.news.get_item('market')))
        self.assertEqual(self.ai.status()['pending']['total'],0)

    def test_preview_is_free_and_confirmation_is_single_use_and_bounded(self):
        for index in range(5):self.put('old'+str(index))
        self.configure(True);self.auto_once()
        self.assertEqual(self.calls,[])
        preview=self.ai.preview_backlog(2,.4,'fact_reading')
        self.assertEqual(preview['count'],2)
        self.assertEqual(self.ai.status()['calls_reserved'],0)
        self.assertTrue(self.ai._queue.empty())
        confirmed=self.ai.confirm_backlog(preview['token'])
        self.assertEqual(confirmed['count'],2)
        with self.assertRaisesRegex(ValueError,'expired_or_changed'):self.ai.confirm_backlog(preview['token'])
        self.dispatch();self.auto_once()
        self.assertEqual(len(self.calls),2)
        self.assertLessEqual(self.ai.status()['reserved_cny'],preview['max_cost_cny'])
        self.assertEqual(self.news.pending_analysis_tasks()['total'],3)

    def test_preview_excludes_unknown_and_failed_paid_identities(self):
        self.configure()
        for state in ('unknown','failed'):
            item=self.put(state)
            with self.ai._db() as db:
                db.execute('INSERT INTO runs(fingerprint,item_id,created,reserved,status,result) VALUES(?,?,?,?,?,?)',
                    (self.ai._version_fingerprint(item),item['id'],self.now,123,state,json.dumps({'error':'preserve-'+state})))
        self.put('unspent')
        preview=self.ai.preview_backlog(10,3,'fact_reading')
        self.assertEqual([x['item_id'] for x in preview['tasks']],['unspent'])
        self.ai.confirm_backlog(preview['token']);self.dispatch()
        self.assertEqual(len(self.calls),1)
        for state in ('unknown','failed'):
            self.assertEqual(self.ai.result(self.news.get_item(state))['error'],'preserve-'+state)
            self.assertEqual(self.ai.status()['pending'][state],1)

    def test_focus_or_version_changes_invalidate_preview_without_reservation(self):
        self.configure();self.put('old')
        preview=self.ai.preview_backlog(1,.3,'fact_reading')
        self.news.configure_focus({'automatic_scope':'market'})
        with self.assertRaisesRegex(ValueError,'expired_or_changed'):self.ai.confirm_backlog(preview['token'])
        preview=self.ai.preview_backlog(1,.3,'fact_reading')
        self.put('old',excerpt='Corrected synthetic material replaces the old value.')
        with self.assertRaisesRegex(ValueError,'evidence_changed'):self.ai.confirm_backlog(preview['token'])
        self.assertEqual(self.calls,[])
        self.assertEqual(self.ai.status()['calls_reserved'],0)

    def test_confirmed_queue_is_revoked_by_focus_change_and_expiration(self):
        self.configure();self.put('one')
        preview=self.ai.preview_backlog(1,.3,'fact_reading');self.ai.confirm_backlog(preview['token'])
        self.news.configure_focus({'automatic_scope':'market'});self.dispatch()
        self.assertEqual(self.calls,[])
        preview=self.ai.preview_backlog(1,.3,'fact_reading');self.ai.confirm_backlog(preview['token'])
        self.advance(301);self.dispatch()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.ai.status()['calls_reserved'],0)

    def test_new_session_invalidates_preview_and_budget_is_shared(self):
        self.configure();self.put('one')
        preview=self.ai.preview_backlog(1,.3,'fact_reading')
        self.configure()
        with self.assertRaisesRegex(ValueError,'expired_or_changed'):self.ai.confirm_backlog(preview['token'])
        preview=self.ai.preview_backlog(1,.3,'fact_reading')
        with self.ai._db() as db:
            for index in range(MAX_CALLS):
                db.execute('INSERT INTO runs(fingerprint,item_id,created,reserved,status) VALUES(?,?,?,?,?)',
                    ('other'+str(index),'other',self.now,10,'unknown'))
        with self.assertRaisesRegex(ValueError,'budget_changed'):self.ai.confirm_backlog(preview['token'])
        self.assertEqual(self.calls,[])
        self.assertEqual(self.ai.status()['remaining_calls'],0)
        self.assertEqual(MAX_CALLS,36);self.assertEqual(BUDGET_MICRO_CNY,3000000)

    def test_default_recommendation_and_legacy_explicit_configuration_remain_distinct(self):
        self.assertEqual((self.ai.status()['provider'],self.ai.status()['model']),('deepseek','deepseek-flash'))
        self.ai.configure('SYNTHETIC_LEGACY_KEY',False,True)
        route=(self.ai.status()['provider'],self.ai.status()['model'])
        self.assertEqual(route,('doubao',providers.DOUBAO_MODEL))
        self.ai.set_mode(True,True)
        self.assertEqual((self.ai.status()['provider'],self.ai.status()['model']),route)

    def test_oversized_table_group_is_held_without_spending_or_preview_entry(self):
        from backend.news_article_evidence import extract_article
        self.configure(True);item=self.put('table')
        rows=''.join('<tr><td>Long synthetic financial caption '+str(n)+' '+('context '*40)+'</td><td>42</td></tr>' for n in range(50))
        document='<html><body><main><h1>Inflation report</h1><table><tr><th>Period Q3</th><th>USD millions</th></tr>'+rows+'</table></main></body></html>'
        evidence=extract_article(document.encode(),item['url'],source_id='fed',now=self.now)
        self.news.save_article_evidence(item['id'],evidence)
        self.auto_once()
        current=self.news.get_item(item['id'])
        self.assertFalse(current['analysis_policy']['automatic_eligible'])
        self.assertFalse(current['analysis_policy']['evidence']['selected_complete_groups'])
        self.assertEqual(self.ai.preview_backlog(1,.3)['count'],0)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.ai.status()['calls_reserved'],0)

    def test_market_recovery_uses_only_confirmed_count_and_shared_budget(self):
        self.put('old-market');self.advance();self.configure()
        preview=self.ai.preview_backlog(1,.3,'market_impact')
        self.assertEqual(preview['count'],1)
        self.assertEqual(self.calls,[])
        self.ai.confirm_backlog(preview['token'])
        pending=self.ai.status()['pending']
        self.assertEqual(pending['raw_pending'],2)
        self.assertEqual(pending['total'],1)
        self.assertEqual(pending['explicit_market'],1)
        self.assertEqual(pending['eligible']+pending['backlog'],0)
        self.dispatch()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0]['scope'],'market_impact_from_saved_context')
        self.assertEqual(self.ai.status()['calls_reserved'],1)
        self.assertLessEqual(self.ai.status()['reserved_cny'],preview['estimated_cost_cny'])
        self.assertEqual(self.ai.preview_backlog(1,.3,'fact_reading')['count'],0)

    def test_invalid_bounds_and_clear_do_not_extend_or_recover_authorization(self):
        self.configure();self.put('old')
        for limit,cost in ((0,.3),(11,.3),(True,.3),(1,True),(1,float('nan')),(1,3.1)):
            with self.assertRaisesRegex(ValueError,'invalid_backlog_bounds'):self.ai.preview_backlog(limit,cost)
        self.ai.clear()
        self.assertEqual(self.ai.status()['pause_reason'],'authorization_required')
        with self.assertRaisesRegex(ValueError,'session_required'):self.ai.preview_backlog(1,.3)
        self.assertEqual(self.calls,[])

    def test_preview_cannot_survive_focus_round_trip_or_wall_clock_rollback(self):
        self.configure();self.put('old')
        preview=self.ai.preview_backlog(1,.3)
        original=self.news.focus()['focus']
        self.news.configure_focus({'automatic_scope':'market'})
        self.news.configure_focus(original)
        with self.assertRaisesRegex(ValueError,'expired_or_changed'):self.ai.confirm_backlog(preview['token'])
        preview=self.ai.preview_backlog(1,.3)
        self.monotonic+=301;self.now-=100
        with self.assertRaisesRegex(ValueError,'expired_or_changed'):self.ai.confirm_backlog(preview['token'])
        self.assertEqual(self.ai.status()['calls_reserved'],0)

    def test_registered_quarterly_provenance_classifies_earnings_without_title_keywords(self):
        import copy
        from backend.news_feed_transport import FeedResponse
        from backend.news_sources import discover,source_definition
        from tests.test_news_micron_directory import page
        source=source_definition('ir_mu')
        raw=page([{'category':'Press release','data':[{'year':2026,'documents':[{
            'text':'Q3','link':'https://investors.micron.com/news/press-release/2026/Fiscal-2026-Q3/default.aspx'}]}]}])
        response=discover(source,transport=lambda *args:FeedResponse(200,raw),now=self.now,
            history_state={'schema':'micron_directory_state_v1','directory_url':source['url'],'baseline_identities':[]})
        entry=response['entries'][0]
        item={**entry,'source_id':'ir_mu','published_at':None,'event':{'current_version':entry}}
        original=copy.deepcopy(item)
        result=policy.classify(item)
        self.assertNotIn('earnings',item['title'].lower())
        self.assertNotIn('financial results',item['title'].lower())
        self.assertEqual(result['event_type'],'earnings')
        self.assertEqual(result['event_type_basis'],'registered_quarterly_directory')
        self.assertEqual(result['importance'],'important')
        self.assertEqual(result['novelty'],'new_event')
        self.assertTrue(result['evidence']['time_unknown'])
        self.assertFalse(result['automatic_eligible'])  # A label alone cannot spend.
        self.assertIn('evidence_missing',result['reasons'])
        self.assertEqual(item,original)
        historical=copy.deepcopy(item)
        historical['event']['current_version'].update(discovery_kind='historical_backfill',automatic_reading_eligible=False)
        self.assertIn('historical_backfill',policy.classify(historical)['reasons'])
        for key,value in (('schema','unverified'),('json_path',''),('directory_sha256',''),
                          ('observed_url','https://unregistered.example/release')):
            invalid=copy.deepcopy(item)
            invalid['discovery_evidence'][key]=value
            self.assertNotEqual(policy.classify(invalid)['event_type'],'earnings')
