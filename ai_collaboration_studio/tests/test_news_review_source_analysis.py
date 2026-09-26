import unittest

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
