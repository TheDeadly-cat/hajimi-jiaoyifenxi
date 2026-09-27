import unittest
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from backend.news_review_source_analysis import analyze_source_runs,error_stage


class SourceAnalysisTests(unittest.TestCase):
    def row(self,n,status,code='',observed=0):
        return {'run_id':str(n),'started_at_ms':n*300000,'completed_at_ms':n*300000+1000 if status!='RUNNING' else 0,
            'status':status,'error_code':code,'observed_count':observed,'accepted_count':0,'duplicate_count':observed}

    def test_partial_polls_and_pending_do_not_advance_full_success(self):
        rows=[self.row(1,'SUCCEEDED',observed=30),self.row(2,'DEGRADED','MICRON_IR_METADATA_REQUEST_FAILED',26),
              self.row(3,'DEGRADED','MICRON_IR_METADATA_REVALIDATION_PENDING',26),self.row(4,'RUNNING')]
        result=analyze_source_runs(rows,observed_until_ms=1500000,maximum_success_gap_ms=900000)
        self.assertEqual(result['full_success_rate'],1/3)
        self.assertEqual(result['in_flight_excluded'],1)
        self.assertEqual(result['last_full_success_at_ms'],301000)
        self.assertEqual(result['degraded_polls_with_validated_rows'],2)
        self.assertEqual(result['gap_standard_evaluation'],'exceeded')
        self.assertIsNone(result['actual_http_requests'])

    def test_recovery_preserves_long_historical_gap(self):
        rows=[self.row(1,'SUCCEEDED'),self.row(2,'FAILED','IR_FEED_ERROR'),self.row(15,'SUCCEEDED')]
        result=analyze_source_runs(rows,observed_until_ms=4501000,maximum_success_gap_ms=900000)
        self.assertEqual(result['full_success_age_ms'],0)
        self.assertEqual(result['maximum_interval_between_full_successes']['gap_ms'],4200000)
        self.assertEqual(result['gap_standard_evaluation'],'exceeded')
        self.assertFalse(result['acceptance_passed'])

    def test_no_samples_do_not_mean_zero_latency_or_perfect_success(self):
        result=analyze_source_runs([],observed_until_ms=1000,maximum_success_gap_ms=900000)
        self.assertIsNone(result['full_success_rate'])
        self.assertIsNone(result['full_success_age_ms'])
        self.assertEqual(result['gap_standard_evaluation'],'no_full_success_sample')

    def test_error_labels_do_not_invent_http_root_cause(self):
        self.assertEqual(error_stage('MICRON_IR_METADATA_REQUEST_FAILED'),'metadata_transport_unclassified')
        self.assertEqual(error_stage('IR_FEED_ERROR'),'list_or_transport_unclassified')
        self.assertEqual(error_stage('MICRON_IR_METADATA_CACHE_EXPIRED'),'cache_age_or_integrity')

    def test_duplicate_run_and_future_completion_are_rejected(self):
        row=self.row(1,'SUCCEEDED')
        with self.assertRaises(ValueError):
            analyze_source_runs([row,row],observed_until_ms=600000)
        with self.assertRaises(ValueError):
            analyze_source_runs([row],observed_until_ms=300000)

    def test_first_success_two_hours_late_is_exceeded_even_immediately_after_recovery(self):
        rows=[self.row(0,'FAILED','IR_FEED_ERROR'),self.row(24,'SUCCEEDED')]
        result=analyze_source_runs(rows,observed_since_ms=0,observed_until_ms=7202000,
            maximum_success_gap_ms=1800000,maximum_initial_success_delay_ms=1800000)
        self.assertEqual(result['first_full_success_delay_ms'],7201000)
        self.assertEqual(result['full_success_age_ms'],1000)
        self.assertEqual(result['full_success_interval_count'],0)
        self.assertEqual(result['initial_success_standard_evaluation'],'exceeded')
        self.assertEqual(result['source_timing_standard_evaluation'],'exceeded')
        self.assertFalse(result['acceptance_passed'])

    def test_never_successful_is_waiting_then_overdue_without_invented_first_delay(self):
        for now,expected in ((1800000,'awaiting_first_full_success'),(1800001,'exceeded')):
            result=analyze_source_runs([self.row(0,'FAILED')],observed_since_ms=0,observed_until_ms=now,
                maximum_success_gap_ms=1800000,maximum_initial_success_delay_ms=1800000)
            self.assertIsNone(result['first_full_success_delay_ms'])
            self.assertEqual(result['waiting_for_first_full_success_ms'],now)
            self.assertEqual(result['initial_success_standard_evaluation'],expected)

    def test_start_is_not_inferred_from_earliest_poll_or_first_success(self):
        result=analyze_source_runs([self.row(24,'SUCCEEDED')],observed_until_ms=7202000,
            maximum_success_gap_ms=1800000,maximum_initial_success_delay_ms=1800000)
        self.assertIsNone(result['observed_since_ms'])
        self.assertIsNone(result['first_full_success_delay_ms'])
        self.assertEqual(result['initial_success_standard_evaluation'],'start_time_unverified')
        self.assertEqual(result['source_timing_standard_evaluation'],'incomplete_evidence_or_standards')

    def test_initial_boundary_and_adjacent_gap_are_evaluated_separately(self):
        result=analyze_source_runs([self.row(1,'SUCCEEDED'),self.row(15,'SUCCEEDED')],
            observed_since_ms=1000,observed_until_ms=4501000,
            maximum_initial_success_delay_ms=300000,maximum_success_gap_ms=900000)
        self.assertEqual(result['initial_success_standard_evaluation'],'within_observed_limits')
        self.assertEqual(result['maximum_interval_between_full_successes']['gap_ms'],4200000)
        self.assertEqual(result['source_timing_standard_evaluation'],'exceeded')
        self.assertEqual(result['full_success_age_ms'],0)

    def test_invalid_start_and_runs_before_start_are_rejected(self):
        for start in (-1,True,600001,301000):
            with self.subTest(start=start),self.assertRaises(ValueError):
                analyze_source_runs([self.row(1,'SUCCEEDED')],observed_since_ms=start,observed_until_ms=600000)

    def test_cli_preserves_start_provenance_and_rejects_missing_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'source.json';standards=root/'standards.json';output=root/'output.json'
            payload={'observed_until_ms':7202000,'sources':{'company_ir':[self.row(24,'SUCCEEDED')]},
                'observation_starts':{'company_ir':{'observed_since_ms':0,'basis':'activation_receipt','evidence_sha256':'a'*64}}}
            source.write_text(json.dumps(payload),encoding='utf-8')
            standards.write_text(json.dumps({'company_ir':{'maximum_success_gap_ms':1800000,
                'maximum_initial_success_delay_ms':1800000}}),encoding='utf-8')
            command=[sys.executable,str(Path(__file__).resolve().parents[1]/'scripts/analyze_news_review_sources.py'),
                '--input',str(source),'--standards',str(standards),'--output',str(output)]
            run=subprocess.run(command,capture_output=True,timeout=15)
            self.assertEqual(run.returncode,0,run.stderr)
            report=json.loads(output.read_text(encoding='utf-8'))
            result=report['source_diagnostics']['company_ir']
            self.assertEqual(result['initial_success_standard_evaluation'],'exceeded')
            self.assertEqual(result['observation_start_evidence'],payload['observation_starts']['company_ir'])
            self.assertFalse(report['standards_are_approval'])
            self.assertFalse(result['start_evidence_reference_independently_verified'])
            payload['observation_starts']['company_ir'].pop('evidence_sha256')
            source.write_text(json.dumps(payload),encoding='utf-8')
            bad=subprocess.run(command[:-1]+[str(root/'bad.json')],capture_output=True,timeout=15)
            self.assertNotEqual(bad.returncode,0)
            self.assertFalse((root/'bad.json').exists())
