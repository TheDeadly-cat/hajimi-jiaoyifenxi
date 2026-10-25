"""News-only multi-provider behavior; all outputs and transports are synthetic."""
import copy
import io
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import news_analysis_providers as providers
from backend.news_platform_ai import NewsAnalysis, MODEL, MAX_CALLS, BUDGET_MICRO_CNY, INSTRUCTIONS
from backend.news_content_quality import TEXT_FORMAT, QUALITY_VERSION
from tests.test_news_platform_ai import FAKE_KEY, FakeNews, item, valid_response


def completed(model='deepseek-flash'):
    return {'model':model,'status':'completed','output':[{'type':'message','role':'assistant','status':'completed',
        'content':[{'type':'output_text','text':valid_response().content}]}]}


class NewsTransportTests(unittest.TestCase):
    def setUp(self):
        self.guard=patch.object(socket.socket,'connect',side_effect=AssertionError('offline_only'))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def send(self, provider='deepseek', model='deepseek-flash', payload=None, raw=None, endpoint=None):
        observations=[]
        body=providers.build_body(provider,model,'synthetic material',INSTRUCTIONS,TEXT_FORMAT)
        output=json.dumps(completed(model) if payload is None else payload).encode() if raw is None else raw
        class Response:
            status=200
            def __enter__(inner): return inner
            def __exit__(inner,*args): pass
            def geturl(inner): return endpoint or providers.ENDPOINTS[provider]
            def read(inner, maximum):
                observations.append(('read',maximum))
                return output
        def factory(*handlers):
            observations.append(('handlers',handlers))
            def open_request(request,timeout):
                observations.append(('request',request,timeout))
                return Response()
            return SimpleNamespace(open=open_request)
        with patch.object(providers.urllib.request,'build_opener',side_effect=factory), patch.dict('sys.modules',{'backend.config':None}):
            result=providers.generate_response(provider,model,FAKE_KEY,body)
        return result,observations,body

    def test_all_routes_use_fixed_single_post_strict_schema_and_no_thinking_tools(self):
        for option in providers.AVAILABLE_MODELS:
            with self.subTest(model=option['model']):
                result,observed,body=self.send(option['provider'],option['model'])
                self.assertTrue(result.ok)
                request_calls=[entry for entry in observed if entry[0]=='request']
                self.assertEqual(len(request_calls),1)
                _,request,timeout=request_calls[0]
                self.assertEqual(request.full_url,providers.ENDPOINTS[option['provider']])
                self.assertEqual(request.get_method(),'POST')
                self.assertEqual(timeout,45)
                self.assertEqual(request.data,providers.encode_body(body))
                self.assertEqual(body['text']['format'],TEXT_FORMAT)
                self.assertTrue(body['text']['format']['strict'])
                self.assertEqual(body['max_output_tokens'],900)
                self.assertIs(body['store'],False)
                self.assertNotIn('tools',body)
                self.assertEqual(body.get('thinking' if option['provider']=='doubao' else 'reasoning'),
                                 {'type':'disabled'} if option['provider']=='doubao' else {'effort':'none'})
                self.assertEqual([entry for entry in observed if entry[0]=='read'],[('read',2*1024*1024+1)])
                handlers=observed[0][1]
                self.assertEqual(len(handlers),1)
                self.assertIsNone(handlers[0].redirect_request(None,None,302,'redirect',{},'https://example.com'))

    def test_refusal_incomplete_missing_completion_tool_and_bad_json_fail_without_retry(self):
        variants=[]
        for status in (None,'incomplete','failed','cancelled'):
            value=completed();value['status']=status;variants.append(value)
        value=completed();value['incomplete_details']={'reason':'max_output_tokens'};variants.append(value)
        value=completed();value['output'][0]['content']=[{'type':'refusal','refusal':'synthetic'}];variants.append(value)
        value=completed();value['output']=[{'type':'function_call','name':'unrequested'}];variants.append(value)
        value=completed();value['model']='unsafe key phrase' + chr(10);variants.append(value)
        for payload in variants:
            with self.subTest(payload=payload):
                result,observed,_=self.send(payload=payload)
                self.assertFalse(result.ok)
                self.assertEqual(sum(entry[0]=='request' for entry in observed),1)
                self.assertNotIn(FAKE_KEY,repr(result))
        for raw in (b'not-json',b'x'*(providers.MAX_RESPONSE_BYTES+1)):
            self.assertFalse(self.send(raw=raw)[0].ok)
        self.assertFalse(self.send(endpoint='https://example.com/redirected')[0].ok)

    def test_rolling_model_is_observed_without_changing_requested_route(self):
        result,observed,body=self.send(payload=completed('deepseek-flash-20261025'))
        self.assertTrue(result.ok)
        self.assertEqual(result.requested_model,'deepseek-flash')
        self.assertEqual(result.reported_model,'deepseek-flash-20261025')
        self.assertEqual(body['model'],'deepseek-flash')
        unknown=self.send(payload=completed('another-provider-model'))[0]
        self.assertFalse(unknown.ok)
        self.assertEqual(unknown.error_code,'model_mismatch')
        self.assertIsNone(unknown.reported_model)
        self.assertEqual(unknown.requested_model,'deepseek-flash')

    def test_official_rolling_names_stay_in_requested_model_family(self):
        for requested,reported in (('deepseek-flash','DeepSeek-V4.1-Flash-1017'),
                                   ('deepseek-v4-pro','DeepSeek-V4-Pro-0813')):
            result,_,_=self.send(model=requested,payload=completed(reported))
            self.assertTrue(result.ok)
            self.assertEqual(result.reported_model,reported)
            self.assertEqual(result.requested_model,requested)
        for reported in ('deepseek-v4-pro','DeepSeek-V4-Pro-0813',MODEL,'another-provider-model'):
            result,observed,_=self.send(model='deepseek-flash',payload=completed(reported))
            self.assertFalse(result.ok)
            self.assertEqual(result.error_code,'model_mismatch')
            self.assertIsNone(result.reported_model)
            self.assertEqual(sum(entry[0]=='request' for entry in observed),1)

    def test_http_error_body_never_read_and_one_open_only(self):
        class ErrorBody(io.BytesIO):
            def read(self,*args): raise AssertionError('error body must not be read')
        stream=ErrorBody(b'secret error body')
        error=urllib.error.HTTPError(providers.ENDPOINTS['deepseek'],429,'synthetic',{},stream)
        opener=SimpleNamespace(open=unittest.mock.Mock(side_effect=error))
        body=providers.build_body('deepseek','deepseek-flash','fixture',INSTRUCTIONS,TEXT_FORMAT)
        with patch.object(providers.urllib.request,'build_opener',return_value=opener):
            result=providers.generate_response('deepseek','deepseek-flash',FAKE_KEY,body)
        self.assertFalse(result.ok)
        self.assertEqual(opener.open.call_count,1)
        self.assertTrue(stream.closed)
        self.assertNotIn('secret',repr(result))

    def test_exact_utf8_size_rate_ceil_and_peak_budget_bounds(self):
        synthetic={'x':'x'*15991}
        self.assertEqual(len(providers.encode_body(synthetic)),16000)
        for provider,model,expected in (('doubao',MODEL,124536),('deepseek','deepseek-flash',59568),('deepseek','deepseek-v4-pro',250220)):
            amount,basis=providers.reservation_for(provider,model,synthetic)
            self.assertEqual(amount,expected)
            self.assertEqual(basis['input_token_upper_estimate'],16256)
            self.assertEqual(basis['rate_version'],'2026-10-25_peak_budget_v1')
        body=providers.build_body('deepseek','deepseek-v4-pro','中文材料',INSTRUCTIONS,TEXT_FORMAT)
        _,basis=providers.reservation_for('deepseek','deepseek-v4-pro',body)
        self.assertEqual(basis['request_utf8_bytes'],len(providers.encode_body(body)))
        self.assertEqual(providers.CNY_PER_USD,10)

    def test_invalid_models_endpoints_tools_and_oversized_input_never_open(self):
        with patch.object(providers.urllib.request,'build_opener',side_effect=AssertionError('must not open')):
            for provider,model in (([],None),('other',MODEL),('deepseek',MODEL),('doubao','deepseek-flash'),('deepseek','arbitrary')):
                with self.assertRaises(ValueError): providers.select_route(provider,model)
            body=providers.build_body('deepseek','deepseek-flash','fixture',INSTRUCTIONS,TEXT_FORMAT)
            body['tools']=[{'type':'web_search'}]
            with self.assertRaises(ValueError): providers.generate_response('deepseek','deepseek-flash',FAKE_KEY,body)
            body=providers.build_body('deepseek','deepseek-flash','fixture',INSTRUCTIONS,TEXT_FORMAT)
            body['input']=[{'type':'input_image','image_url':'https://example.com'}]
            with self.assertRaises(ValueError): providers.generate_response('deepseek','deepseek-flash',FAKE_KEY,body)
            body=providers.build_body('deepseek','deepseek-flash','x'*20000,INSTRUCTIONS,TEXT_FORMAT)
            with self.assertRaises(ValueError): providers.generate_response('deepseek','deepseek-flash',FAKE_KEY,body)


class NewsProviderAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'analysis.db'
        self.news=FakeNews()
        self.calls=[]
        self.apps=[]
        self.guard=patch.object(socket.socket,'connect',side_effect=AssertionError('offline_only'))
        self.guard.start()
        self.app=self.new_app()

    def tearDown(self):
        for app in reversed(self.apps): self.assertTrue(app.stop(timeout=4))
        self.guard.stop();self.temp.cleanup()

    def new_app(self):
        app=NewsAnalysis(self.path,self.news)
        def generate(key,text):
            self.calls.append((app._provider,app._model))
            return SimpleNamespace(ok=True,provider=app._provider,model=app._model,
                requested_model=app._model,reported_model=app._model,content=valid_response().content)
        app._generate=generate
        self.apps.append(app)
        return app

    def configure(self, provider='deepseek', model='deepseek-flash'):
        return self.app.configure(FAKE_KEY,False,True,provider=provider,model=model)

    def test_configuration_defaults_allowlist_and_old_three_argument_api(self):
        status=self.app.configure(FAKE_KEY,False,True)
        self.assertEqual((status['provider'],status['model']),('doubao',MODEL))
        self.assertEqual(len(status['available_models']),3)
        self.assertEqual((status['max_calls'],status['budget_cny']),(36,3))
        self.assertEqual(status['budget_conversion_cny_per_usd'],10)
        self.assertNotIn(FAKE_KEY,json.dumps(status))
        for option in providers.AVAILABLE_MODELS:
            status=self.configure(option['provider'],option['model'])
            self.assertEqual((status['provider'],status['model']),(option['provider'],option['model']))
        with self.assertRaises(ValueError):self.configure('deepseek','bad-model')
        self.assertEqual(self.app.status()['model'],'deepseek-v4-pro')

    def test_switching_provider_returns_original_completed_result_without_recharge(self):
        self.configure();self.app._analyze('one')
        original=self.app.result(self.news.items['one'])
        with self.app._db() as db: before=[tuple(row) for row in db.execute('SELECT * FROM runs')]
        self.configure('doubao',MODEL);self.app._analyze('one')
        self.assertEqual(self.app.request('one'),original)
        self.configure('deepseek','deepseek-v4-pro');self.app._analyze('one')
        self.assertEqual(self.app.result(self.news.items['one']),original)
        with self.app._db() as db:self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM runs')],before)
        self.assertEqual(self.calls,[('deepseek','deepseek-flash')])

    def test_every_old_contract_and_terminal_state_prevents_cross_route_charge(self):
        original_rows=[]
        for contract,formula in (('v3',self.app._fingerprint),('v2',self.app._rss_v2_fingerprint),('legacy',self.app._legacy_fingerprint)):
            for state in ('completed','failed','pending','unknown'):
                identifier=contract+'-'+state;source=item(identifier);self.news.items[identifier]=source
                raw='{ "summary": "original saved output" }';fingerprint=formula(source)
                with self.app._db() as db:db.execute('INSERT INTO runs(fingerprint,item_id,created,reserved,status,result) VALUES(?,?,?,?,?,?)',(fingerprint,identifier,time.time(),123,state,raw))
                original_rows.append((fingerprint,state,raw))
                self.configure();self.app._analyze(identifier)
                self.assertEqual(self.app.request(identifier)['status'],state)
        with self.app._db() as db:
            for fingerprint,state,raw in original_rows:
                row=db.execute('SELECT status,result FROM runs WHERE fingerprint=?',(fingerprint,)).fetchone()
                self.assertEqual(tuple(row),(state,raw))
        self.assertEqual(self.calls,[])
        self.assertEqual(self.app.status()['calls_reserved'],12)

    def test_failed_and_unknown_new_route_results_are_shared_across_models(self):
        for state in ('failed','pending','unknown'):
            source=item(state);self.news.items[state]=source
            fp=self.app._route_fingerprint(source,'deepseek','deepseek-v4-pro')
            with self.app._db() as db:db.execute('INSERT INTO runs(fingerprint,item_id,created,reserved,status,result) VALUES(?,?,?,?,?,?)',(fp,state,time.time(),500,state,'{}'))
            self.configure('doubao',MODEL);self.app._analyze(state)
            self.assertEqual(self.app.request(state)['status'],state)
        self.assertEqual(self.calls,[])

    def test_mixed_old_versions_keep_v3_then_v2_then_legacy_read_precedence(self):
        source=self.news.items['one']
        with self.app._db() as db:
            for offset,formula,state,summary in (
                (-20,self.app._legacy_fingerprint,'failed','old legacy failure'),
                (-10,self.app._rss_v2_fingerprint,'completed','v2 saved result'),
                (0,self.app._fingerprint,'completed','v3 saved result')):
                db.execute('INSERT INTO runs(fingerprint,item_id,created,reserved,status,result) VALUES(?,?,?,?,?,?)',
                    (formula(source),'one',time.time()+offset,100,state,json.dumps({'summary':summary})))
        self.configure()
        self.assertEqual(self.app.result(source)['summary'],'v3 saved result')
        self.app._analyze('one')
        self.assertEqual(self.calls,[])

    def test_old_queued_or_dequeued_job_never_runs_with_new_key_or_route(self):
        self.configure();self.app.request('one')
        old_item,old_generation=self.app._queue.get_nowait()
        self.configure('doubao',MODEL)
        self.app._analyze(old_item,generation=old_generation)
        self.assertEqual(self.calls,[])
        self.assertTrue(self.app._queue.empty())
        self.app.request('one');self.app.clear()
        self.assertTrue(self.app._queue.empty())
        self.assertEqual(self.app.status()['calls_reserved'],0)

    def test_switch_during_material_read_cancels_old_analysis_admission(self):
        self.configure()
        original=self.news.get_item
        def switched(identifier):
            self.configure('doubao',MODEL)
            return original(identifier)
        self.news.get_item=switched
        self.app._analyze('one')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.app.status()['calls_reserved'],0)
        with self.assertRaisesRegex(ValueError,'configuration_changed'):self.app.request('one')

    def test_busy_switch_rejected_and_active_snapshot_survives_key_clear(self):
        self.configure()
        entered,release=threading.Event(),threading.Event()
        def generate(key,text):
            self.calls.append(('deepseek','deepseek-flash'));entered.set()
            if not release.wait(3):raise AssertionError('fixture timeout')
            return SimpleNamespace(ok=True,provider='deepseek',model='deepseek-flash',content=valid_response().content)
        self.app._generate=generate
        worker=threading.Thread(target=self.app._analyze,args=('one',));worker.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaisesRegex(ValueError,'in_progress'):self.configure('doubao',MODEL)
            self.app.clear()
            self.assertFalse(self.app.status()['configured'])
        finally:release.set();worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.app.result(self.news.items['one'])['provider'],'deepseek')
        self.assertEqual(len(self.calls),1)

    def test_two_instances_cross_route_reserve_same_material_only_once(self):
        second=self.new_app()
        self.configure()
        second.configure(FAKE_KEY,False,True,provider='doubao',model=MODEL)
        barrier=threading.Barrier(2)
        errors=[]
        def run(app):
            try:barrier.wait();app._analyze('one')
            except Exception as exc:errors.append(type(exc).__name__)
        workers=[threading.Thread(target=run,args=(app,)) for app in (self.app,second)]
        for worker in workers:worker.start()
        for worker in workers:worker.join(4);self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.app.status()['calls_reserved'],1)

    def test_shared_cap_atomic_for_two_instances_on_different_material(self):
        self.news.items['two']=item('two')
        second=self.new_app();self.configure();second.configure(FAKE_KEY,False,True)
        with self.app._db() as db:db.executemany('INSERT INTO runs(fingerprint,item_id,created,reserved,status) VALUES(?,?,?,?,?)',[(str(i),str(i),time.time(),1,'failed') for i in range(MAX_CALLS-1)])
        barrier=threading.Barrier(2)
        def run(app,identifier):barrier.wait();app._analyze(identifier)
        workers=[threading.Thread(target=run,args=(self.app,'one')),threading.Thread(target=run,args=(second,'two'))]
        for worker in workers:worker.start()
        for worker in workers:worker.join(4);self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.app.status()['calls_reserved'],MAX_CALLS)

    def test_reservation_metadata_persisted_key_absent_and_rates_shared_across_switch(self):
        self.configure('deepseek','deepseek-v4-pro');self.app._analyze('one')
        with self.app._db() as db:row=dict(db.execute('SELECT * FROM runs').fetchone())
        basis=json.loads(row['reservation_basis'])
        self.assertEqual(row['reserved'],basis['reserved_micro_cny'])
        self.assertEqual(row['provider'],'deepseek')
        self.assertEqual(row['model'],'deepseek-v4-pro')
        self.assertEqual(row['contract'],QUALITY_VERSION)
        self.assertEqual(row['rate_version'],'2026-10-25_peak_budget_v1')
        self.assertNotIn(FAKE_KEY,self.path.read_bytes().decode('utf-8',errors='ignore'))
        with self.app._db() as db:db.execute('UPDATE runs SET reserved=?',(BUDGET_MICRO_CNY-1,))
        self.news.items['two']=item('two');self.configure('doubao',MODEL);self.app._analyze('two')
        self.assertEqual(len(self.calls),1)

    def test_model_mismatch_keeps_reservation_and_never_saves_unknown_reported_identity(self):
        self.configure()
        def mismatched(key,text):
            self.calls.append(('deepseek','deepseek-flash'))
            return SimpleNamespace(ok=True,provider='deepseek',model='another-provider-model',content=valid_response().content)
        self.app._generate=mismatched
        self.app._analyze('one')
        result=self.app.result(self.news.items['one'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['error_code'],'model_mismatch')
        self.assertEqual(result['requested_model'],'deepseek-flash')
        self.assertIsNone(result['reported_model'])
        self.assertGreater(self.app.status()['reserved_cny'],0)
        self.configure('doubao',MODEL);self.app._analyze('one')
        self.assertEqual(len(self.calls),1)
        with self.app._db() as db:
            self.assertNotIn('another-provider-model',db.execute('SELECT result FROM runs').fetchone()[0])


if __name__=='__main__':unittest.main()
