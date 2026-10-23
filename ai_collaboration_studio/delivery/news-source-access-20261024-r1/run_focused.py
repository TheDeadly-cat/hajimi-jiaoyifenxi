"""Opt-in offline validation of an exact, already-applied source candidate."""
import hashlib
import json
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

if len(sys.argv) != 2:
    raise SystemExit('usage: run_focused.py CANDIDATE_ROOT')
candidate = Path(sys.argv[1]).resolve(strict=True)
manifest = json.loads((Path(__file__).parent / 'manifest.json').read_text(encoding='utf-8'))
for relative, hashes in manifest['files'].items():
    if hashlib.sha256((candidate / relative).read_bytes()).hexdigest() != hashes['after_sha256']:
        raise SystemExit('candidate_hash_mismatch: ' + relative)
sys.path.insert(0, str(candidate))
names = [
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
]
with patch.object(socket.socket, 'connect', side_effect=AssertionError('offline_only')), \
     patch.object(socket.socket, 'connect_ex', side_effect=AssertionError('offline_only')), \
     patch.object(socket, 'getaddrinfo', side_effect=AssertionError('offline_only')):
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    if suite.countTestCases() != 20:
        raise SystemExit('unexpected_focused_test_count')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
