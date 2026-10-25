"""Reproduce the historical 20 checks and focused Retry-After regression offline."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

BASELINE_TESTS = (
    'tests.test_news_source_recovery.SourceRecoveryTests',
    'tests.test_news_transport_classification.FailureClassificationTests',
    'tests.test_news_cooldown.NewsCooldownTests.test_three_successes_and_full_recovery_wait_lower_only_one_level',
    'tests.test_news_cooldown.NewsCooldownTests.test_backoff_caps_local_delay_and_small_interval_scales',
    'tests.test_news_cooldown.NewsCooldownTests.test_429_write_failure_restores_server_deadline_without_refetching_completed_source',
    'tests.test_news_cooldown.NewsCooldownTests.test_uncommitted_successful_probe_keeps_pressure_and_waits_without_counting_success',
    'tests.test_news_cooldown.NewsCooldownTests.test_retry_after_http_date_is_persisted_as_a_deadline',
    'tests.test_news_cooldown.NewsCooldownTests.test_403_is_distinct_and_conservative_and_does_not_claim_origin',
    'tests.test_news_platform.NewsPlatformTests.test_repeated_fetch_deduplicates_and_preserves_read_state',
    'tests.test_news_feed_transport_diagnostics.TransportDiagnosticsTests.test_success_diagnostics_are_allowlisted_and_mock_route_is_unknown',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-only', action='store_true', help='Run precisely the historical 20 checks')
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_backend_tests_isolated import main as run_isolated
    tests = list(BASELINE_TESTS)
    if not args.baseline_only:
        tests.append('tests.test_news_retry_after_failure.RetryAfterFailureTests')
    return run_isolated([*tests, '--verbosity', '2'])


if __name__ == '__main__':
    raise SystemExit(main())
