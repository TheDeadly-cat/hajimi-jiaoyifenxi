"""Actual local entry point in isolated children with synthetic identity/key/HTTP."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from backend.news_review_quality_preparation import signoff_template

APP=Path(__file__).resolve().parents[1]
CHILD=r'''
import io,json,subprocess,sys
from unittest.mock import patch
from scripts.run_news_review_quality import main
original=subprocess.check_output
calls={'key_requests':0,'http_calls':0}
def identity(args,**kwargs):
    if args[-2:]==['rev-parse','HEAD']: return 'a'*40+'\n'
    if args[-2:]==['status','--porcelain']: return ''
    return original(args,**kwargs)
def key(plan,directory):
    config=plan['config']
    assert config['provider']=='doubao' and config['max_output_tokens']==1400
    assert config['expires_at_ms']>0 and config['rate_card']['currency']=='CNY'
    assert plan['max_calls']==36 and plan['plan_sha256']
    calls['key_requests']+=1
    return 'synthetic-private-fixture-key'
def transport(request,**kwargs):
    calls['http_calls']+=1
    assert kwargs=={'timeout':240}
    assert b'synthetic-test-reviewer' not in request.data
    body=json.loads(request.data)
    evidence=json.loads(body['input'])
    p=evidence['document']['paragraphs'][1]
    result={'version':'news_event_review_v1','assessment':'reviewed','summary':'Synthetic only.',
        'importance':{'level':'normal','reason':'Synthetic fixture'},
        'facts':[{'claim':p['text'],'paragraph_id':p['id'],'quote':p['text']}],
        'inferences':[],'counterevidence':[],'open_questions':[],
        'limitations':['Synthetic, independently unverified.']}
    payload={'model':body['model'],'status':'completed','output':[{'type':'message','content':[
        {'type':'output_text','text':json.dumps(result)}]}],
        'usage':{'input_tokens':100,'output_tokens':50,'total_tokens':150}}
    return io.BytesIO(json.dumps(payload).encode())
with patch('subprocess.check_output',side_effect=identity), \
     patch('scripts.pilot_password_dialog.ask_api_key',side_effect=key), \
     patch('urllib.request.OpenerDirector.open',side_effect=transport):
    try:
        result=main(sys.argv[1:])
        calls['exit_code']=result
    except Exception as exc:
        result=1
        calls['code']=str(exc) if type(exc) is ValueError else type(exc).__name__
print(json.dumps(calls))
raise SystemExit(result)
'''


class QualityLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='quality-launcher-test-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'NewsQualityBenchmarkSynthetic'
        self.corpus=(APP/'tests/fixtures/news_review_quality_v1.json').read_bytes()
        self.references=Path(self.temp.name)/'references.json'

    def child(self,*args):
        result=subprocess.run([sys.executable,'-X','utf8','-B','-c',CHILD,*map(str,args)],
            cwd=APP,capture_output=True,text=True,timeout=40,env=os.environ.copy())
        self.assertTrue(result.stdout.strip(),result.stderr)
        self.assertNotIn('synthetic-private-fixture-key',result.stdout+result.stderr)
        return result,json.loads(result.stdout.strip().splitlines()[-1])

    def prepare(self,signed=True):
        result,_=self.child('--initialize','--root',self.root,'--candidate-sha','a'*40)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        initialized=json.loads((self.root/'initialized.json').read_text(encoding='utf-8'))
        reference=signoff_template(self.corpus)
        if signed:
            for row in reference['cases']:
                row.update(approved=True,reviewer='synthetic-test-reviewer',reviewed_at='2026-09-27T00:00:00+00:00')
        self.references.write_text(json.dumps(reference),encoding='utf-8')
        now=int(time.time()*1000)
        self.plan_file=self.root/'plan.json'
        return self.child('--prepare','--database',initialized['database_path'],'--room-id',initialized['room_id'],
            '--candidate-sha','a'*40,'--references',self.references,'--not-before-ms',now-1000,
            '--expires-at-ms',now+60000,'--output',self.plan_file)

    def test_full_entrypoint_requires_new_exact_approval_and_refuses_second_key_window(self):
        result,_=self.prepare()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        plan=json.loads(self.plan_file.read_text(encoding='utf-8'))
        args=('--run','--plan',self.plan_file,'--references',self.references,'--password-dialog')
        result,status=self.child(*args,'--approve-plan-sha256','b'*64,'--acknowledge-human-references')
        self.assertEqual(status['code'],'quality_approval_mismatch')
        self.assertEqual(status['key_requests'],0)
        result,status=self.child(*args,'--approve-plan-sha256',plan['plan_sha256'])
        self.assertEqual(status['code'],'quality_human_acknowledgement_missing')
        self.assertEqual(status['key_requests'],0)
        approved=(*args,'--approve-plan-sha256',plan['plan_sha256'],'--acknowledge-human-references')
        result,status=self.child(*approved)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(status['key_requests'],1)
        self.assertEqual(status['http_calls'],36)
        report=json.loads((self.root/'quality-batch-receipts'/'execution-summary.json').read_text(encoding='utf-8'))
        self.assertTrue(report['all_requests_responded'])
        self.assertFalse(report['human_quality_verified'])
        terminal=json.loads((self.root/'launcher-exit.json').read_text(encoding='utf-8'))
        self.assertTrue(terminal['checkpoint_completed'])
        result,status=self.child(*approved)
        self.assertEqual(status['code'],'quality_activation_already_claimed')
        self.assertEqual(status['key_requests'],0)
        self.assertEqual(status['http_calls'],0)

    def test_unsigned_references_cannot_be_prepared_as_an_executable_plan(self):
        result,status=self.prepare(signed=False)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(status['code'],'reference_signoff_incomplete')
        self.assertEqual(status['key_requests'],0)
        self.assertFalse(self.plan_file.exists())

    def test_wrong_candidate_does_not_initialize_a_database(self):
        result,status=self.child('--initialize','--root',self.root,'--candidate-sha','b'*40)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(status['code'],'quality_candidate_not_exact_clean')
        self.assertFalse(self.root.exists())


if __name__=='__main__':
    unittest.main()
