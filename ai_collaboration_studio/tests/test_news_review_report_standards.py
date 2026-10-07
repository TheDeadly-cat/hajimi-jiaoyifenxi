"""Source-report rate definitions and explicit diagnostic standards."""
import copy
import unittest
from unittest.mock import Mock, patch

from backend.decision_lineage import canonical_sha256
from backend.news_review_report import NewsReviewJournal, build_news_review_report
from backend.news_review_source_analysis import analyze_source_runs, source_timing_standards
from backend.source_monitoring.state_repository import SourceMonitoringStateRepository


def run(identifier, status, *, start=10, end=20):
    return {'run_id': identifier, 'adapter_key': 'sec_filings', 'status': status,
            'started_at_ms': start, 'completed_at_ms': 0 if status == 'RUNNING' else end,
            'observed_count': 0, 'accepted_count': 0, 'duplicate_count': 0, 'error_code': ''}


class SourceDiagnosticStandardTests(unittest.TestCase):
    def test_defaults_are_explicit_and_cannot_be_mutated_through_a_returned_copy(self):
        standards = source_timing_standards()
        self.assertEqual(standards['sec_filings'], {'first_success_ms': 900000, 'maximum_gap_ms': 900000})
        self.assertEqual(standards['company_ir'], {'first_success_ms': 1800000, 'maximum_gap_ms': 1800000})
        standards['sec_filings']['maximum_gap_ms'] = 1
        self.assertEqual(source_timing_standards()['sec_filings']['maximum_gap_ms'], 900000)

    def test_caller_supplied_values_are_validated_and_copied(self):
        values = source_timing_standards()
        values['sec_filings']['first_success_ms'] = 100
        result = source_timing_standards(values)
        self.assertEqual(result['sec_filings']['first_success_ms'], 100)
        values['sec_filings']['first_success_ms'] = 200
        self.assertEqual(result['sec_filings']['first_success_ms'], 100)

    def test_invalid_standards_fail_before_reading_the_service(self):
        for invalid in ({}, {'sec_filings': {}}, {'sec_filings': {}, 'company_ir': {}, 'extra': {}}):
            with self.subTest(value=invalid):
                service = Mock()
                with self.assertRaises(ValueError):
                    build_news_review_report(service, 'invalid', source_standards=invalid)
                service.snapshot.assert_not_called()
        for limit in (True, 0, -1, 1.5, '100', 2**63):
            with self.subTest(limit=limit):
                standards = source_timing_standards()
                standards['sec_filings']['first_success_ms'] = limit
                with self.assertRaises(ValueError):
                    source_timing_standards(standards)

    def test_running_is_excluded_and_abandoned_or_dry_run_never_becomes_full_success(self):
        rows = [run('success','SUCCEEDED'),run('abandoned','ABANDONED'),
                run('dry','DRY_RUN'),run('running','RUNNING')]
        result = analyze_source_runs(rows, observed_until_ms=100, observed_since_ms=0,
                                     maximum_initial_success_delay_ms=100, maximum_success_gap_ms=100)
        self.assertEqual(result['completed_poll_denominator'],3)
        self.assertEqual(result['in_flight_excluded'],1)
        self.assertEqual(result['full_success_rate'],1/3)
        self.assertIs(result['acceptance_passed'],False)


class SourceReportIntegrationTests(unittest.TestCase):
    def setUp(self):
        from tests import test_news_review as fixtures
        fixture = fixtures.NewsReviewTests()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.approve()
        self.fixture = fixture
        self.runs = {}
        self.enterContext(patch.object(SourceMonitoringStateRepository, 'get_run',
                                      side_effect=lambda identifier: copy.deepcopy(self.runs[identifier])))
        self.enterContext(patch.object(SourceMonitoringStateRepository, 'get_state', return_value=None))

    def observations(self, statuses):
        fixture = self.fixture
        start = fixture.now
        fixture.now += 1000
        journal = NewsReviewJournal(fixture.service, fixture.policy['policy_id'])
        for index, status in enumerate(statuses):
            identifier = f'synthetic-source-{index}'
            self.runs[identifier] = run(identifier, status, start=start+10+index, end=start+20+index)
            journal.record('source_poll',source_run_id=identifier)

    def report(self, **options):
        fixture = self.fixture
        return build_news_review_report(fixture.service,fixture.policy['policy_id'], **options)

    def test_primary_rate_and_diagnostic_rate_share_completed_poll_denominator(self):
        self.observations(['SUCCEEDED','RUNNING'])
        report = self.report()
        metrics = report['source_checks']['sec_filings']
        self.assertEqual(metrics['poll_runs_observed'],2)
        self.assertEqual(metrics['completed_poll_runs_observed'],1)
        self.assertEqual(metrics['in_flight_excluded'],1)
        self.assertEqual(metrics['success_rate'],1.0)
        self.assertEqual(metrics['success_rate'],metrics['diagnostics']['full_success_rate'])
        self.assertEqual(metrics['success_rate_basis'],metrics['diagnostics']['denominator_basis'])
        self.assertEqual(metrics['diagnostics']['observed_since_ms'],self.fixture.policy['not_before_ms'])
        self.assertEqual(metrics['diagnostics']['source_timing_standard_evaluation'],'within_observed_limits')
        self.assertIs(report['outcome']['acceptance_passed'],False)
        self.assertIs(report['source_timing_standards']['acceptance_authority'],False)

    def test_only_running_samples_do_not_invent_zero_or_success(self):
        self.observations(['RUNNING'])
        metrics = self.report()['source_checks']['sec_filings']
        self.assertIsNone(metrics['success_rate'])
        self.assertIsNone(metrics['diagnostics']['full_success_rate'])
        self.assertEqual(metrics['completed_poll_runs_observed'],0)
        self.assertEqual(metrics['diagnostics']['source_timing_standard_evaluation'],'incomplete_evidence_or_standards')

    def test_default_standards_are_identified_without_claiming_monitor_approval(self):
        report = self.report()
        standards = report['source_timing_standards']
        self.assertEqual(standards['basis'],'profile_diagnostic_defaults')
        self.assertEqual(standards['sha256'],canonical_sha256(standards['standards']))
        self.assertIs(standards['monitoring_approval_verified'],False)
        for adapter, gap in (('sec_filings',900000),('company_ir',1800000)):
            self.assertEqual(report['source_checks'][adapter]['diagnostics']['gap_standard_ms'],gap)
            self.assertEqual(report['source_checks'][adapter]['diagnostics']['initial_success_standard_ms'],gap)
        self.assertEqual(self.fixture.fetcher.call_count,0)

    def test_explicit_plan_standards_override_defaults_and_can_show_a_real_gap(self):
        self.observations(['SUCCEEDED'])
        standards = source_timing_standards()
        for standard in standards.values():
            standard.update(first_success_ms=100,maximum_gap_ms=100)
        report = self.report(source_standards=standards)
        self.assertEqual(report['source_timing_standards']['basis'],'caller_supplied')
        self.assertEqual(report['source_timing_standards']['sha256'],canonical_sha256(standards))
        self.assertEqual(report['source_checks']['sec_filings']['diagnostics']['gap_standard_evaluation'],'exceeded')
        self.assertIs(report['outcome']['acceptance_passed'],False)


if __name__ == '__main__':
    unittest.main()
