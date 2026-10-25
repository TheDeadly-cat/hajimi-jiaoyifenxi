"""Frozen pre-v4 identities and immutable spent results; synthetic/offline only."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.news_analysis_providers import AVAILABLE_MODELS, DOUBAO_MODEL
from backend.news_content_quality import RSS_V3_INSTRUCTIONS, RSS_V3_TEXT_FORMAT, QUALITY_VERSION
from backend.news_platform_ai import NewsAnalysis, MAX_CALLS

KEY = 'SYNTHETIC_NEVER_REAL_SECRET'


def sample(identity='one'):
    return {'id':identity, 'title':'Issuer Q2 results',
            'excerpt':'Revenue was USD 10 million in Q2 2026.',
            'source_name':'Synthetic issuer', 'url':'https://example.com/release',
            'published_at':'2026-10-19T10:00:00Z','discovered_at':'2026-10-19T10:05:00Z'}


class News:
    def __init__(self): self.items = {'one':sample()}
    def get_item(self, key): return copy.deepcopy(self.items.get(key))
    def overview(self): return {'enabled':True}
    def list_items(self, limit=100): return {'items':list(self.items.values())[:limit]}


class V3HistoryCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='news-v3-history-offline-')
        self.path = Path(self.temp.name)/'independent-analysis.sqlite3'
        self.news = News()
        self.calls = []
        self.network = patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden'))
        self.dns = patch.object(socket, 'getaddrinfo', side_effect=AssertionError('dns_forbidden'))
        self.network.start(); self.dns.start()
        def generate(*args):
            self.calls.append(1)
            raise AssertionError('old_result_must_not_call_provider')
        self.app = NewsAnalysis(self.path, self.news, generate=generate)

    def tearDown(self):
        self.app.stop(timeout=1)
        self.network.stop(); self.dns.stop()
        self.temp.cleanup()

    def insert(self, fingerprint, item, status='completed', raw=None, reserved=1234):
        raw = raw or ' { "summary" : "旧结果原样保留", "quality_version" : "rss_excerpt_reading_v3" } '
        row = (fingerprint,item['id'],time.time(),reserved,status,raw)
        with self.app._db() as db:
            db.execute('INSERT INTO runs(fingerprint,item_id,created,reserved,status,result) VALUES(?,?,?,?,?,?)',row)
        return row

    def test_v3_hashes_extracted_from_d7251bb_before_quality_upgrade(self):
        # Constants independently extracted from immutable PR36 d7251bb, not
        # regenerated from the implementation under test during this test.
        self.assertEqual(hashlib.sha256(RSS_V3_INSTRUCTIONS.encode()).hexdigest(),
                         'a4b46556bb85cd0eca71b717e8c4f9fb33bfae51d7cf82beb28ecf0f10965078')
        self.assertEqual(hashlib.sha256(json.dumps(RSS_V3_TEXT_FORMAT,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),
                         'f91950d191a12c4472962fbd9704ab917627a4a4f8d0db68efbd6e26bd17ecb7')
        self.assertEqual(self.app._rss_v3_fingerprint(sample()),
                         '1298a80b40cd776dddfbe791b0d5122dace6f1f03c814ea10356af95f5284527')
        expected = {
            ('doubao',DOUBAO_MODEL):'101add1339c15a97e2902b9c1b63c2c35ce0b12fc0dfc75891c0edcdaf7c94dc',
            ('deepseek','deepseek-flash'):'69075c20303488bad22034fdad2cb668fb4a7c2e551229cb7c2895f33495b0be',
            ('deepseek','deepseek-v4-pro'):'f6f7cbf64acc4e9871eafe27d6a429789898ec190cb76788195f81572036dfa9',
        }
        for (provider,model), digest in expected.items():
            self.assertEqual(self.app._rss_v3_route_fingerprint(sample(),provider,model),digest)
            self.assertNotEqual(self.app._route_fingerprint(sample(),provider,model),digest)
        self.assertEqual(QUALITY_VERSION,'rss_excerpt_reading_v4')

    def test_every_v3_route_and_status_stays_spent_across_all_selections(self):
        original = []
        factories = [self.app._rss_v3_fingerprint]
        factories += [lambda item, p=option['provider'], m=option['model']: self.app._rss_v3_route_fingerprint(item,p,m)
                      for option in AVAILABLE_MODELS]
        for route, factory in enumerate(factories):
            for status in ('completed','failed','pending','unknown'):
                item = sample('%d-%s' % (route,status))
                item['url'] += '/'+item['id']
                self.news.items[item['id']] = item
                original.append(self.insert(factory(item),item,status))
        for option in AVAILABLE_MODELS:
            self.app.configure(KEY,True,True,provider=option['provider'],model=option['model'])
            for row in original:
                item = self.news.items[row[1]]
                saved = self.app.result(item)
                self.assertEqual(saved['status'],row[4])
                self.assertEqual(saved['quality_version'],'rss_excerpt_reading_v3')
                self.assertIn('未经过v4',saved['legacy_notice'])
                self.assertTrue(saved['semantic_review_required'])
                self.assertEqual(self.app.request(item['id'])['status'],row[4])
                self.app._analyze(item['id']); self.app._analyze(item['id'],automatic=True)
            self.assertEqual(self.app.status()['calls_reserved'],16)
            self.assertEqual(self.app.status()['reserved_cny'],round(16*1234/1_000_000,6))
        with self.app._db() as db:
            after = [tuple(db.execute('SELECT fingerprint,item_id,created,reserved,status,result FROM runs WHERE fingerprint=?',(row[0],)).fetchone()) for row in original]
        self.assertEqual(original,after)
        self.assertEqual(self.calls,[])
        self.assertTrue(self.app._queue.empty())

    def test_read_precedence_keeps_v3_then_v2_then_legacy_then_old_routes(self):
        item = sample()
        priority = [self.app._rss_v3_fingerprint(item),self.app._rss_v2_fingerprint(item),self.app._legacy_fingerprint(item)]
        priority += [self.app._rss_v3_route_fingerprint(item,o['provider'],o['model']) for o in AVAILABLE_MODELS]
        priority += [self.app._fingerprint(item)]
        priority += [self.app._route_fingerprint(item,o['provider'],o['model']) for o in AVAILABLE_MODELS]
        for index, fingerprint in enumerate(priority):
            self.insert(fingerprint,item,raw=json.dumps({'summary':'saved-%d'%index}))
        for index,fingerprint in enumerate(priority):
            for option in AVAILABLE_MODELS:
                self.app.configure(KEY,False,True,provider=option['provider'],model=option['model'])
                self.assertEqual(self.app.result(item)['summary'],'saved-%d'%index)
            # Test fixture removal only, to expose the next precedence entry.
            with self.app._db() as db:db.execute('DELETE FROM runs WHERE fingerprint=?',(fingerprint,))
        self.assertEqual(self.calls,[])

    def test_v2_and_legacy_all_states_remain_spent_on_every_new_route(self):
        original=[]
        for version,factory in (('rss_excerpt_reading_v2',self.app._rss_v2_fingerprint),
                                ('legacy_unchecked',self.app._legacy_fingerprint)):
            for status in ('completed','failed','pending','unknown'):
                item=sample(version+'-'+status);item['url']+='/'+item['id']
                self.news.items[item['id']]=item
                raw=json.dumps({'summary':'旧结果内容不重写','quality_version':version},ensure_ascii=False,indent=2)
                original.append((version,self.insert(factory(item),item,status,raw=raw)))
        for option in AVAILABLE_MODELS:
            self.app.configure(KEY,True,True,provider=option['provider'],model=option['model'])
            for version,row in original:
                item=self.news.items[row[1]]
                saved=self.app.request(item['id'])
                self.assertEqual(saved['status'],row[4])
                self.assertEqual(saved['quality_version'],version)
                self.app._analyze(item['id'],automatic=True)
            self.assertEqual(self.app.status()['calls_reserved'],8)
            self.assertEqual(self.app.status()['reserved_cny'],0.009872)
        with self.app._db() as db:
            after=[tuple(db.execute('SELECT fingerprint,item_id,created,reserved,status,result FROM runs WHERE fingerprint=?',(row[0],)).fetchone()) for _,row in original]
        self.assertEqual(after,[row for _,row in original])
        self.assertEqual(self.calls,[])
        self.assertTrue(self.app._queue.empty())

    def test_old_pending_restart_remains_unknown_and_charged_with_existing_transition(self):
        item = sample()
        old = self.insert(self.app._rss_v3_route_fingerprint(item,'deepseek','deepseek-flash'),item,'pending')
        self.app = NewsAnalysis(self.path,self.news,generate=lambda *_:self.fail('pending_restart_must_not_retry'))
        for option in AVAILABLE_MODELS:
            self.app.configure(KEY,False,True,provider=option['provider'],model=option['model'])
            self.assertEqual(self.app.result(item)['status'],'unknown')
            self.app._analyze(item['id'])
            self.assertEqual(self.app.status()['reserved_cny'],0.001234)
        with self.app._db() as db:
            row = tuple(db.execute('SELECT fingerprint,item_id,created,reserved,status,result FROM runs').fetchone())
        self.assertEqual(row[:4],old[:4])
        self.assertEqual(row[4],'unknown')
        # Existing constructor behavior deliberately replaces pending JSON with
        # interruption notice; do not claim this transition preserves its bytes.
        self.assertNotEqual(row[5],old[5])
        self.assertIn('不自动重试',json.loads(row[5])['error'])

    def test_shared_budget_includes_historical_rows_without_version_reset(self):
        for index in range(MAX_CALLS):
            item = sample('spent-%d'%index);item['url'] += '/'+item['id']
            self.insert(self.app._rss_v3_route_fingerprint(item,'deepseek','deepseek-flash'),item,'failed')
        for option in AVAILABLE_MODELS:
            self.app.configure(KEY,False,True,provider=option['provider'],model=option['model'])
            self.app._analyze('one')
            self.assertEqual(self.app.status()['calls_reserved'],MAX_CALLS)
            self.assertIn('额度已用完',self.app.status()['error'])
        self.assertEqual(self.calls,[])

    def test_old_v3_attempt_remains_spent_after_rolling_budget_ages_out(self):
        item=sample()
        row=self.insert(self.app._rss_v3_route_fingerprint(item,'deepseek','deepseek-flash'),item,'unknown')
        with self.app._db() as db:
            db.execute('UPDATE runs SET created=? WHERE fingerprint=?',(time.time()-172800,row[0]))
        for option in AVAILABLE_MODELS:
            self.app.configure(KEY,True,True,provider=option['provider'],model=option['model'])
            self.assertEqual(self.app.status()['calls_reserved'],0)
            self.assertEqual(self.app.request('one')['status'],'unknown')
            self.app._analyze('one',automatic=True)
            self.assertEqual(self.app.status()['calls_reserved'],0)
        self.assertEqual(self.calls,[])

    def test_validator_unknown_keeps_exact_raw_output_and_no_repeat(self):
        raw = '{ "summary": "保留原始空格", "key_points": [], "uncertainties": ["仅摘录"] }'
        self.app._generate = lambda *_:SimpleNamespace(ok=True,model=DOUBAO_MODEL,content=raw)
        self.app.configure(KEY,False,True)
        with patch('backend.news_platform_ai.validate_output',side_effect=RuntimeError('private exception detail')):
            self.app._analyze('one')
        saved = self.app.result(sample())
        self.assertEqual(saved['status'],'needs_review')
        self.assertEqual(saved['error_code'],'validation_unconfirmed')
        self.assertEqual(saved['raw_model_output'],raw)
        self.assertEqual(saved['raw_model_output_sha256'],hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotIn('private exception',json.dumps(saved))
        self.app._generate = lambda *_:self.fail('review_record_must_not_repeat')
        self.app.configure(KEY,False,True,provider='deepseek',model='deepseek-flash')
        self.app._analyze('one')
        self.assertEqual(self.app.request('one')['status'],'needs_review')
        self.assertEqual(self.app.status()['calls_reserved'],1)

    def test_semantic_rejection_preserves_original_text_not_rewritten_claim(self):
        item = sample();item['title']='CPI release';item['excerpt']='Consumer Price Index (CPI): +0.6% in Sep 2026.'
        self.news.items['one']=item
        value={'summary':'2026年9月CPI同比上涨0.6%。','key_points':[{'claim':'CPI同比上涨0.6%。','evidence_field':'excerpt','quote':item['excerpt']}],
               'uncertainties':['仅阅读RSS摘录，未核对全文；原文没有注明比较口径。']}
        raw=json.dumps(value,ensure_ascii=False,indent=2)
        self.app._generate=lambda *_:SimpleNamespace(ok=True,model=DOUBAO_MODEL,content=raw)
        self.app.configure(KEY,False,True);self.app._analyze('one')
        saved=self.app.result(item)
        self.assertEqual(saved['status'],'needs_review')
        self.assertEqual(saved['error_code'],'semantic_review_required')
        self.assertEqual(saved['raw_model_output'],raw)
        self.assertTrue(saved['semantic_review']['issues'])
        self.assertNotIn('summary',saved)
        self.assertEqual(saved['candidate_result']['summary'],value['summary'])
        self.app._generate=lambda *_:self.fail('rejected_semantics_must_not_repeat')
        self.app.configure(KEY,False,True,provider='deepseek',model='deepseek-v4-pro')
        self.app._analyze('one')
        self.assertEqual(self.app.status()['calls_reserved'],1)

    def test_escaped_credential_is_not_preserved_even_on_validation_failure(self):
        escaped=''.join('\\u%04x'%ord(c) for c in KEY)
        raw='{"summary":"'+escaped+'","key_points":[],"uncertainties":["仅摘录"]}'
        self.app._generate=lambda *_:SimpleNamespace(ok=True,model=DOUBAO_MODEL,content=raw)
        self.app.configure(KEY,False,True);self.app._analyze('one')
        saved=self.app.result(sample())
        self.assertEqual(saved['status'],'failed')
        self.assertEqual(saved['raw_output_retention_reason'],'credential_echo_rejected')
        self.assertNotIn('raw_model_output',saved)
        self.assertNotIn(KEY.encode(),self.path.read_bytes())
        self.assertNotIn(escaped.encode(),self.path.read_bytes())


if __name__=='__main__':unittest.main(verbosity=2)
