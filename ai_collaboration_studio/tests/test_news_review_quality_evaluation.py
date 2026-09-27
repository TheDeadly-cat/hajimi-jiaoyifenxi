"""Synthetic output/terminal fixtures validate binding, never actual human quality."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from backend.news_review_monitor import publish_json_once
from backend.decision_lineage import canonical_sha256
from backend.news_review_quality_evaluation import evaluation_template,evaluate_bound_ratings
from backend.news_review_quality_scorecard import RUBRIC
from tests import test_news_review_quality_executor as fixtures


class BoundQualityEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.QualityExecutorTests()
        self.f.addCleanup=self.addCleanup
        self.f.setUp()
        self.plan_raw=json.dumps(self.f.f.plan).encode()
        self.inputs=(fixtures.fixtures.CORPUS_RAW,self.f.f.references,self.plan_raw,self.f.executor.root)

    def execute(self,effect=None):
        with patch('urllib.request.OpenerDirector.open',side_effect=effect or self.f.transport):
            report=self.f.executor.run()
        # Deliberately a fixture declaration. The offline grader must not claim
        # it independently checked process termination or opened the ledger.
        publish_json_once(self.f.executor.root.parent/'launcher-exit.json',{
            'version':'news_quality_launcher_exit_v1','plan_sha256':self.f.f.plan['plan_sha256'],
            'exit_code':0 if report['execution_completed'] else 2,'worker_drained':True,
            'checkpoint_completed':True,'model_quality_verified':False})

    def rate(self,template):
        value=copy.deepcopy(template)
        for row in value['ratings']:
            row.update(reviewer='synthetic-human-declaration',reviewed_at='2026-09-27T12:00:00+00:00',
                scores=RUBRIC.copy(),critical_failures=[])
        return value

    def evaluate(self,value):
        return evaluate_bound_ratings(*self.inputs,json.dumps(value).encode())

    def test_empty_template_binds_all_unique_outputs_without_inventing_scores(self):
        self.execute()
        template=evaluation_template(*self.inputs)
        self.assertEqual(len(template['ratings']),36)
        self.assertEqual(len({r['id'] for r in template['ratings']}),36)
        self.assertEqual(len({r['case_id'] for r in template['ratings']}),34)
        self.assertEqual(len(template['excluded_cases']),2)
        self.assertTrue(all(r['scores'] is None and r['reviewer'] is None for r in template['ratings']))
        for case in ('Q24_revision','Q25_restore'):
            rows=[r for r in template['ratings'] if r['case_id']==case]
            self.assertEqual(len(rows),2)
            self.assertEqual(len({r['document_sha256'] for r in rows}),2)
        result=self.evaluate(template)
        self.assertEqual(result['scored_requests'],0)
        self.assertIsNone(result['quality_gate_passed'])
        self.assertFalse(result['provider_output_independently_verified'])
        self.assertFalse(result['runtime_terminal_independently_verified'])
        self.assertFalse(result['ledger_independently_rechecked'])

    def test_declared_human_scores_keep_machine_criteria_and_no_release_authority(self):
        self.execute()
        ratings=self.rate(evaluation_template(*self.inputs))
        result=self.evaluate(ratings)
        self.assertEqual(result['mean_score'],10)
        self.assertTrue(result['human_rating_gate_passed'])
        self.assertTrue(result['quality_gate_passed'])
        self.assertFalse(result['rating_authenticity_independently_verified'])
        self.assertFalse(result['request_authorized'])
        self.assertFalse(result['release_approved'])
        ratings['ratings'][0]['critical_failures']=['wrong_number_unit_or_period']
        self.assertFalse(self.evaluate(ratings)['quality_gate_passed'])

    def test_request_swap_and_changed_reference_cannot_inherit_existing_scores(self):
        self.execute()
        ratings=self.rate(evaluation_template(*self.inputs))
        ratings['ratings'][0]['response_content_sha256']=ratings['ratings'][1]['response_content_sha256']
        with self.assertRaisesRegex(ValueError,'output_binding_changed'):
            self.evaluate(ratings)
        references=json.loads(self.f.f.references)
        references['cases'][0]['reference']['must_preserve']+=' changed'
        with self.assertRaisesRegex(ValueError,'quality_plan_changed'):
            evaluation_template(self.inputs[0],json.dumps(references).encode(),self.plan_raw,self.f.executor.root)

    def test_changed_output_and_missing_terminal_receipt_refuse_grading(self):
        self.execute()
        template=evaluation_template(*self.inputs)
        path=next(p for p in self.f.executor.root.glob('execution-*.json') if p.name!='execution-summary.json')
        record=json.loads(path.read_text(encoding='utf-8'))
        record['content']='changed content'
        path.write_text(json.dumps(record),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'execution_hash_invalid'):
            self.evaluate(template)
        # A different directory with a copied summary has no terminal receipt.
        other=Path(self.f.f.temp.name)/'evidence-without-terminal'
        other.mkdir()
        (other/'execution-summary.json').write_bytes((self.f.executor.root/'execution-summary.json').read_bytes())
        with self.assertRaisesRegex(ValueError,'evidence_path_invalid'):
            evaluation_template(*self.inputs[:3],other)

    def test_unknown_and_unattempted_requests_have_no_rateable_result(self):
        self.execute(TimeoutError('synthetic failure'))
        template=evaluation_template(*self.inputs)
        self.assertFalse(template['execution_completed'])
        self.assertEqual(sum(r['rating_eligible'] for r in template['ratings']),0)
        self.assertEqual(sum(r['status']=='UNKNOWN' for r in template['ratings']),1)
        self.assertIsNone(self.evaluate(template)['quality_gate_passed'])
        with self.assertRaisesRegex(ValueError,'without_verified_output'):
            self.evaluate(self.rate(template))

    def test_full_human_points_cannot_override_invalid_model_contract(self):
        def malformed(request,**kwargs):
            value=self.f.payload(request,**kwargs)
            value['output'][0]['content'][0]['text']='{"wrong_contract":true}'
            return io.BytesIO(json.dumps(value).encode())
        self.execute(malformed)
        result=self.evaluate(self.rate(evaluation_template(*self.inputs)))
        self.assertTrue(result['human_rating_gate_passed'])
        self.assertFalse(result['quality_gate_passed'])

    def test_resealed_changed_output_cannot_inherit_an_old_rating_packet(self):
        self.execute()
        ratings=self.rate(evaluation_template(*self.inputs))
        path=next(p for p in self.f.executor.root.glob('execution-*.json') if p.name!='execution-summary.json')
        record=json.loads(path.read_text(encoding='utf-8'))
        content=json.loads(record['content'])
        content['summary']='A different valid output after the rating template was frozen.'
        record['content']=json.dumps(content)
        record['response_content_sha256']=hashlib.sha256(record['content'].encode()).hexdigest()
        record.pop('execution_sha256')
        record['execution_sha256']=canonical_sha256(record)
        path.write_text(json.dumps(record),encoding='utf-8')
        outcome_path=self.f.executor.root/(record['provider_attempt_id']+'.json')
        outcome=json.loads(outcome_path.read_text(encoding='utf-8'))
        outcome['response_sha256']=record['response_content_sha256']
        outcome.pop('receipt_sha256')
        outcome['receipt_sha256']=canonical_sha256(outcome)
        outcome_path.write_text(json.dumps(outcome),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'rating_evidence_changed'):
            self.evaluate(ratings)

    def test_missing_attribution_duplicate_request_and_bad_scores_are_rejected(self):
        self.execute()
        ratings=self.rate(evaluation_template(*self.inputs))
        for field,value in (('reviewer',''),('reviewed_at','2026-09-27T12:00:00'),
                            ('scores',{**RUBRIC,'importance':True})):
            changed=copy.deepcopy(ratings)
            changed['ratings'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):
                self.evaluate(changed)
        ratings['ratings'][1]=copy.deepcopy(ratings['ratings'][0])
        with self.assertRaisesRegex(ValueError,'rating_identity_invalid'):
            self.evaluate(ratings)

    def test_cli_is_file_only_preserves_pending_state_and_refuses_overwrite(self):
        self.execute()
        root=self.f.executor.root.parent
        plan,reference,ratings,result=(root/name for name in ('plan.json','reference.json','ratings.json','score.json'))
        plan.write_bytes(self.plan_raw)
        reference.write_bytes(self.f.f.references)
        app=Path(__file__).resolve().parents[1]
        common=[sys.executable,'-X','utf8','-B','scripts/score_news_review_quality.py',
            '--execution-plan',str(plan),'--references',str(reference),'--receipts',str(self.f.executor.root)]
        exported=subprocess.run(common+['--export-template','--output',str(ratings)],cwd=app,
            capture_output=True,text=True,timeout=20,env=os.environ.copy())
        self.assertEqual(exported.returncode,0,exported.stdout+exported.stderr)
        first=ratings.read_bytes()
        audit=json.loads(exported.stdout.strip().splitlines()[-1])
        self.assertEqual(audit['database_connections'],0)
        self.assertEqual(audit['network_requests'],0)
        scored=subprocess.run(common+['--ratings',str(ratings),'--output',str(result)],cwd=app,
            capture_output=True,text=True,timeout=20,env=os.environ.copy())
        self.assertEqual(scored.returncode,0,scored.stdout+scored.stderr)
        self.assertIsNone(json.loads(result.read_text(encoding='utf-8'))['quality_gate_passed'])
        repeat=subprocess.run(common+['--export-template','--output',str(ratings)],cwd=app,
            capture_output=True,text=True,timeout=20,env=os.environ.copy())
        self.assertNotEqual(repeat.returncode,0)
        self.assertEqual(ratings.read_bytes(),first)


if __name__=='__main__':
    unittest.main()
