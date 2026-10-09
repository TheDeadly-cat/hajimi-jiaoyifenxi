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
        self.last_journal = journal
        journal.record('session_started', runtime_status='running')
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

    # ---- 工作包 A：历史报告不得因重报时间改变结论 ----

    def observe_session(self, statuses, *, stop=None, interval_ms=300_000):
        """Record polls plus an optional terminal stop inside ONE journal session.

        A poll is recorded AFTER it completes, so every run's completed_at_ms is
        <= its observation wall_ms (matching the real ordering).  Uses the fixture
        clock so the policy window and all observations share one timeline.
        Returns the wall-clock ms of the last observation (the stop, if any).
        """
        fixture = self.fixture
        start = fixture.now
        journal = NewsReviewJournal(fixture.service, fixture.policy['policy_id'])
        self.last_journal = journal
        journal.record('session_started', runtime_status='running')
        for index, status in enumerate(statuses):
            poll_at = start + index*interval_ms
            identifier = f'synthetic-session-{index}'
            self.runs[identifier] = run(identifier, status,
                                        start=poll_at, end=poll_at+1000)
            fixture.now = poll_at + 1000          # record after completion
            journal.record('source_poll', source_run_id=identifier)
        if stop is not None:
            fixture.now = start + len(statuses)*interval_ms
            journal.record('stop_requested', stop=dict(stop,
                window_reached=fixture.service.window_reached(fixture.policy),
                policy_id=fixture.policy['policy_id'], session_id=journal.session_id))
            journal.record('session_stopped', runtime_status='stopped')
        return fixture.now

    NORMAL_STOP = {'stop_type': 'window_elapsed', 'window_reached': True,
                   'cleanup_completed': True}
    FAULT_STOP = {'stop_type': 'fault_stop_requested', 'window_reached': False,
                  'cleanup_completed': True}

    def diagnostics_now(self):
        return self.report()['source_checks']['sec_filings']

    def test_A1_historical_timing_does_not_change_when_reported_a_day_later(self):
        # Work order A, acceptance 1: identical terminal evidence, reported at
        # closeout and again the next day, must yield identical historical
        # timing findings.
        self.observe_session(['SUCCEEDED']*288, stop=self.NORMAL_STOP)
        at_closeout = self.diagnostics_now()

        self.fixture.now += 86_400_000          # one day later, same evidence
        next_day = self.diagnostics_now()

        self.assertEqual(at_closeout['diagnostics']['source_timing_standard_evaluation'],
                         'within_observed_limits')
        self.assertEqual(next_day['diagnostics']['source_timing_standard_evaluation'],
                         at_closeout['diagnostics']['source_timing_standard_evaluation'],
                         'historical timing must not drift with report generation time')
        for key in ('first_full_success_delay_ms','maximum_interval_between_full_successes',
                    'full_success_age_ms','gap_standard_evaluation',
                    'initial_success_standard_evaluation','full_success_rate'):
            self.assertEqual(next_day['diagnostics'][key], at_closeout['diagnostics'][key],
                             f'{key} changed only because the report was regenerated')

    def test_A1b_report_generation_time_and_observation_end_are_separate_fields(self):
        # Work order A, requirement 2: report time, the actual observation window
        # and its basis, and the live age must be three distinct things.
        end = self.observe_session(['SUCCEEDED']*288, stop=self.NORMAL_STOP)
        self.fixture.now += 3_600_000
        metrics = self.diagnostics_now()

        self.assertEqual(metrics['report_generated_at_ms'], self.fixture.now)
        self.assertEqual(metrics['observation_end_ms'], end)
        self.assertEqual(metrics['observation_basis'], 'authorized_window_elapsed')
        self.assertEqual(metrics['diagnostics']['observed_until_ms'], end)
        # Live age keeps moving; historical timing does not.
        self.assertEqual(metrics['live_age_since_last_success_ms'],
                         self.fixture.now - metrics['diagnostics']['last_full_success_at_ms'])
        self.assertEqual(metrics['live_diagnostics_basis'], 'report_generation_time')

    def test_A2_early_fault_stop_does_not_count_remaining_authorization_as_coverage(self):
        # Work order A, requirements 3 and 4: an early fault end is recorded as an
        # early stop, and the unused authorization tail is NOT treated as observed.
        end = self.observe_session(['SUCCEEDED','SUCCEEDED'], stop=self.FAULT_STOP)
        self.fixture.now = self.fixture.policy['expires_at_ms'] + 1000   # long after
        metrics = self.diagnostics_now()

        self.assertEqual(metrics['observation_basis'], 'early_stop_recorded')
        self.assertEqual(metrics['observation_end_ms'], end)
        self.assertNotEqual(metrics['observation_end_ms'],
                            self.fixture.policy['expires_at_ms'],
                            'expires_at must never stand in for the actual run end')
        self.assertLess(metrics['observation_end_ms'], self.fixture.policy['expires_at_ms'])
        # The authorization tail after the early stop is not data coverage.
        self.assertLess(metrics['diagnostics']['observed_until_ms'],
                        self.fixture.policy['expires_at_ms'])

    def test_A3_missing_terminal_evidence_stays_unconfirmed_not_normal_end(self):
        # Work order A, requirement 4: without terminal evidence the report keeps
        # partial/unconfirmed observation.  The last heartbeat is NOT a terminal
        # state, and expires_at is NOT the time actually reached.
        last_poll = self.observe_session(['SUCCEEDED','SUCCEEDED'])   # no stop at all
        self.fixture.now = self.fixture.policy['expires_at_ms'] + 1000
        metrics = self.diagnostics_now()

        self.assertEqual(metrics['observation_basis'], 'terminal_evidence_missing')
        self.assertEqual(metrics['observation_end_ms'], last_poll)
        self.assertIs(metrics['terminal_evidence_verified'], False)
        self.assertNotEqual(metrics['observation_end_ms'],
                            self.fixture.policy['expires_at_ms'])

    def test_A4_live_window_may_report_current_age_and_says_so(self):
        # Work order A, acceptance 2: while the window is still running, the live
        # age legitimately moves -- but it must be labelled as the live basis and
        # must not be presented as a final historical finding.
        self.observe_session(['SUCCEEDED','SUCCEEDED'])               # still running
        live = self.diagnostics_now()
        self.assertEqual(live['observation_basis'], 'live_report_time')
        self.assertIs(live['terminal_evidence_verified'], False)
        first_evaluation = live['diagnostics']['source_timing_standard_evaluation']

        self.fixture.now += 3_600_000
        later = self.diagnostics_now()
        self.assertEqual(later['observation_basis'], 'live_report_time')
        self.assertGreater(later['live_age_since_last_success_ms'],
                           live['live_age_since_last_success_ms'])
        self.assertEqual(later['diagnostics']['source_timing_standard_evaluation'], 'exceeded')
        self.assertNotEqual(first_evaluation, 'exceeded')

    def test_A5_other_policy_live_state_does_not_pollute_historical_diagnostics(self):
        # Work order A, requirement 5: get_state() is global mutable source state.
        # A later policy's state must not alter this policy's historical timing,
        # and those fields must be labelled as live with their own timestamp.
        end = self.observe_session(['SUCCEEDED']*288, stop=self.NORMAL_STOP)
        self.fixture.now += 86_400_000
        stale = {'last_success_at_ms': self.fixture.now, 'last_error_code': '',
                 'consecutive_failures': 0, 'next_due_at_ms': self.fixture.now+1}
        with patch.object(SourceMonitoringStateRepository, 'get_state', return_value=dict(stale)):
            metrics = self.diagnostics_now()

        self.assertEqual(metrics['diagnostics']['source_timing_standard_evaluation'],
                         'within_observed_limits')
        self.assertEqual(metrics['diagnostics']['observed_until_ms'], end)
        self.assertNotEqual(metrics['diagnostics']['last_full_success_at_ms'],
                            stale['last_success_at_ms'])
        self.assertEqual(metrics['live_source_state_basis'],
                         'global_mutable_state_at_report_time_not_policy_scoped')
        self.assertEqual(metrics['live_source_state_observed_at_ms'], self.fixture.now)

    def test_A6_first_success_too_late_still_exceeds_and_none_stays_none(self):
        # Work order A, acceptance 5: the fix must not launder a genuine violation,
        # and a sample-free adapter must stay None rather than 0% or 100%.
        self.observe_session(['SUCCEEDED'], interval_ms=300_000)
        standards = source_timing_standards()
        standards['sec_filings'].update(first_success_ms=1, maximum_gap_ms=1)
        late = self.report(source_standards=standards)['source_checks']['sec_filings']
        self.assertEqual(late['diagnostics']['initial_success_standard_evaluation'], 'exceeded')
        self.assertEqual(late['diagnostics']['source_timing_standard_evaluation'], 'exceeded')

        empty = self.report()['source_checks']['company_ir']
        self.assertIsNone(empty['success_rate'])
        self.assertIsNone(empty['diagnostics']['full_success_rate'])

    def test_A7_evidence_after_the_terminal_point_is_reported_not_absorbed(self):
        # A poll that completed after the recorded terminal point contradicts the
        # bound window.  That must surface explicitly instead of being silently
        # widened or swallowed as a generic unconfirmed diagnostic.
        end = self.observe_session(['SUCCEEDED'], stop=self.FAULT_STOP)
        late_id = 'synthetic-late'
        self.runs[late_id] = run(late_id, 'SUCCEEDED', start=end+5000, end=end+6000)
        self.fixture.now = end + 6000
        self.last_journal.record('source_poll', source_run_id=late_id)
        metrics = self.diagnostics_now()
        self.assertIsNotNone(metrics.get('observation_end_conflict_ms'))
        self.assertEqual(metrics['diagnostics'].get('available'), False)
        self.assertEqual(metrics['diagnostics']['error_code'], 'SOURCE_DIAGNOSTIC_UNCONFIRMED')



    def test_stop_signal_is_not_a_verified_terminal(self):
        from backend.news_review_controller import NewsReviewController
        from backend.news_review_stop import NewsReviewStop
        f = self.fixture
        self.observe_session(['SUCCEEDED'])
        journal = NewsReviewJournal(f.service, f.policy['policy_id'])
        journal.record('session_started', runtime_status='running')
        controller = NewsReviewController(f.service, f.policy['policy_id'])
        termination = NewsReviewStop(f.service, f.policy, controller, journal,
                                     f.cancel, f.path.parent)
        termination.request('worker_failure', trigger='synthetic_worker',
                            runtime_status='running')
        f.now += 1000
        journal.record('heartbeat', runtime_status='draining')
        report = self.report()
        metrics = report['source_checks']['sec_filings']
        self.assertFalse(report['outcome']['cleanup_clean'])
        self.assertIs(metrics['terminal_evidence_verified'], False)
        self.assertEqual(metrics['observation_basis'], 'terminal_evidence_missing')
        self.assertEqual(metrics['observation_end_ms'], f.now)

    def test_new_active_session_does_not_inherit_previous_terminal(self):
        f = self.fixture
        old = NewsReviewJournal(f.service, f.policy['policy_id'])
        old.record('session_started', runtime_status='running')
        f.now += 1000
        old.record('session_stopped', runtime_status='stopped')
        f.now += 1000
        current = NewsReviewJournal(f.service, f.policy['policy_id'])
        current.record('session_started', runtime_status='running')
        f.now += 1000
        current.record('heartbeat', runtime_status='running')
        metrics = self.diagnostics_now()
        self.assertEqual(metrics['observation_basis'], 'live_report_time')
        self.assertIs(metrics['terminal_evidence_verified'], False)
        self.assertEqual(metrics['observation_end_ms'], f.now)

    def test_current_terminal_reason_is_scoped_to_current_session(self):
        f = self.fixture
        old = NewsReviewJournal(f.service, f.policy['policy_id'])
        old.record('session_started', runtime_status='running')
        old.record('stop_requested', stop=dict(self.FAULT_STOP,
            policy_id=f.policy['policy_id'], session_id=old.session_id))
        old.record('session_stopped', runtime_status='stopped')
        current = NewsReviewJournal(f.service, f.policy['policy_id'])
        current.record('session_started', runtime_status='running')
        f.now = f.policy['expires_at_ms']
        current.record('stop_requested', stop=dict(self.NORMAL_STOP,
            policy_id=f.policy['policy_id'], session_id=current.session_id))
        current.record('session_stopped', runtime_status='stopped')
        report = self.report()
        metrics = report['source_checks']['sec_filings']
        self.assertEqual(metrics['observation_basis'], 'authorized_window_elapsed')
        self.assertIs(metrics['terminal_evidence_verified'], True)
        # Session classification does not erase older faults from policy outcome.
        self.assertIs(report['outcome']['work_completed'], False)
        self.assertIs(report['outcome']['acceptance_passed'], False)


    def test_live_age_survives_a_conflicting_historical_endpoint(self):
        end = self.observe_session(['SUCCEEDED'], stop=self.FAULT_STOP)
        late = 'synthetic-late-live-age'
        self.runs[late] = run(late, 'SUCCEEDED', start=end+5000, end=end+6000)
        self.fixture.now = end + 6000
        self.last_journal.record('source_poll', source_run_id=late)
        self.fixture.now += 1000
        metrics = self.diagnostics_now()
        self.assertIs(metrics['terminal_evidence_verified'], False)
        self.assertFalse(metrics['diagnostics']['available'])
        self.assertEqual(metrics['live_age_since_last_success_ms'], 1000)
        self.assertEqual(metrics['observation_end_ms'], end)
        self.assertIsNotNone(metrics['observation_end_conflict_sequence'])

    def test_confirmed_stop_without_reason_preserves_unknown_classification(self):
        f = self.fixture
        journal = NewsReviewJournal(f.service, f.policy['policy_id'])
        journal.record('session_started', runtime_status='running')
        f.now += 1000
        journal.record('session_stopped', runtime_status='stopped')
        end = f.now
        f.now += 1000
        metrics = self.diagnostics_now()
        self.assertIs(metrics['terminal_evidence_verified'], True)
        self.assertEqual(metrics['observation_basis'], 'session_stopped_reason_unconfirmed')
        self.assertEqual(metrics['observation_end_ms'], end)
        self.assertIs(self.report()['outcome']['work_completed'], False)

    def test_expired_policy_without_observations_has_no_invented_endpoint(self):
        self.fixture.now = self.fixture.policy['expires_at_ms'] + 1000
        metrics = self.diagnostics_now()
        self.assertIsNone(metrics['observation_end_ms'])
        self.assertIsNone(metrics['observation_session_id'])
        self.assertEqual(metrics['observation_basis'], 'terminal_evidence_missing')
        self.assertIs(metrics['terminal_evidence_verified'], False)
        self.assertFalse(metrics['diagnostics']['available'])
        self.assertIsNone(metrics['live_age_since_last_success_ms'])

    def test_dual_clock_early_stop_keeps_its_original_observation_endpoint(self):
        from tests.test_news_review_runtime_clock import RuntimeClockTests
        clock = RuntimeClockTests()
        clock.addCleanup = self.addCleanup
        clock.setUp()
        f = clock.f
        f.approve()
        self.fixture = f
        journal = NewsReviewJournal(f.service, f.policy['policy_id'])
        journal.record('session_started', runtime_status='running')
        clock.advance(1000)
        journal.record('stop_requested', stop=dict(self.FAULT_STOP,
            policy_id=f.policy['policy_id'], session_id=journal.session_id))
        journal.record('session_stopped', runtime_status='stopped')
        end = f.now
        anchor = copy.deepcopy(f.policy['clock_activation'])
        clock.advance(86_400_000)
        report = self.report()
        metrics = report['source_checks']['sec_filings']
        self.assertEqual(metrics['observation_basis'], 'early_stop_recorded')
        self.assertEqual(metrics['observation_start_ms'], anchor['wall_ms'])
        self.assertEqual(metrics['observation_start_basis'], 'bound_clock_activation')
        self.assertEqual(metrics['observation_end_ms'], end)
        self.assertLess(end, f.policy['expires_at_ms'])
        self.assertEqual(f.policy['clock_activation'], anchor)
        self.assertIs(report['outcome']['acceptance_passed'], False)


    def test_session_start_provenance_does_not_hide_initial_delay(self):
        f = self.fixture
        f.now += 60_000
        started = f.now
        journal = NewsReviewJournal(f.service, f.policy['policy_id'])
        journal.record('session_started', runtime_status='running')
        identifier = 'synthetic-delayed-session'
        self.runs[identifier] = run(identifier, 'SUCCEEDED', start=started, end=started+1000)
        f.now += 1000
        journal.record('source_poll', source_run_id=identifier)
        metrics = self.diagnostics_now()
        self.assertEqual(metrics['observation_start_ms'], f.policy['not_before_ms'])
        self.assertEqual(metrics['observation_start_basis'], 'bound_policy_not_before')
        self.assertEqual(metrics['session_started_at_ms'], started)
        self.assertEqual(metrics['observation_first_sample_ms'], started)
        self.assertEqual(metrics['diagnostics']['first_full_success_delay_ms'], 61_000)

    def test_reporting_keeps_all_persisted_rows_unchanged(self):
        from contextlib import closing
        f = self.fixture
        self.observe_session(['SUCCEEDED', 'SUCCEEDED'], stop=self.FAULT_STOP)
        with closing(f.store._connect()) as db:
            before = tuple(db.iterdump())
        self.report()
        f.now += 86_400_000
        self.report()
        with closing(f.store._connect()) as db:
            after = tuple(db.iterdump())
        self.assertEqual(after, before)
        self.assertEqual(f.fetcher.call_count, 0)


if __name__ == '__main__':
    unittest.main()
