"""Offline regressions for startup-pin reuse and initialization closeout."""
import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import news_review_observer_wait as waiting
from scripts import run_news_review_observer as observer


class StartupIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='observer-startup-offline-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.ticks = 638000000000000000
        self.published = []

    def primary_fails(self, path, payload):
        if Path(path).name == 'observer-exit.json':
            raise OSError('synthetic primary failure')
        self.published.append((Path(path), copy.deepcopy(payload)))

    def test_startup_pin_survives_unavailable_teardown_identity_query(self):
        query = Mock(side_effect=OSError('query unavailable'))
        with patch.object(observer, 'publish_json_once', side_effect=self.primary_fails):
            result = observer.publish_exit_receipt(self.directory, {'phase': 'failed'},
                fallback_version='offline_test', ticks_provider=query,
                observer_identity=(os.getpid(), self.ticks))
        query.assert_not_called()
        self.assertEqual(result, 'fallback_only_published')
        path, payload = self.published[0]
        self.assertEqual(path.name, f'observer-exit-fallback-{os.getpid()}-{self.ticks}.json')
        self.assertEqual(payload['pid'], os.getpid())
        self.assertEqual(payload['process_start_utc_ticks'], self.ticks)
        self.assertEqual(payload['fallback_identity_basis'], 'startup_pin')
        for name in ('final_acceptance_verified', 'monitored_processes_stopped_by_observer', 'database_opened'):
            self.assertIs(payload[name], False)

    def test_primary_receipt_carries_the_same_startup_pin(self):
        query = Mock(side_effect=AssertionError('no query expected'))
        with patch.object(observer, 'publish_json_once',
                          side_effect=lambda path, payload: self.published.append((path, copy.deepcopy(payload)))):
            result = observer.publish_exit_receipt(self.directory, {'phase': 'failed'},
                fallback_version='offline_test', ticks_provider=query,
                observer_identity=(os.getpid(), self.ticks))
        query.assert_not_called()
        self.assertEqual(result, 'primary_published')
        self.assertEqual(self.published[0][1]['process_start_utc_ticks'], self.ticks)

    def test_invalid_startup_pin_is_not_repaired_by_a_fresh_query(self):
        invalid = ((os.getpid() + 1, self.ticks), (os.getpid(), True),
                   (os.getpid(), str(self.ticks)), [os.getpid(), self.ticks])
        for identity in invalid:
            with self.subTest(identity=identity):
                self.published.clear()
                query = Mock(return_value=self.ticks)
                with patch.object(observer, 'publish_json_once', side_effect=self.primary_fails):
                    observer.publish_exit_receipt(self.directory, {'phase': 'failed'},
                        fallback_version='offline_test', ticks_provider=query, observer_identity=identity)
                query.assert_not_called()
                path, payload = self.published[0]
                self.assertEqual(path.name, f'observer-exit-fallback-unidentified-{os.getpid()}.json')
                self.assertEqual(payload['fallback_identity_basis'], 'pid_only')
                self.assertIsNone(payload['process_start_utc_ticks'])
                self.assertIn('startup_identity_unconfirmed', payload['failure_codes'])

    def test_malformed_native_query_results_never_claim_exact_ticks(self):
        for ticks in (True, 1.5, '123', 0, -1, 2**63):
            with self.subTest(ticks=ticks):
                self.published.clear()
                with patch.object(observer, 'publish_json_once', side_effect=self.primary_fails):
                    observer.publish_exit_receipt(self.directory, {'phase': 'failed'},
                        fallback_version='offline_test', ticks_provider=lambda _pid: ticks)
                self.assertEqual(self.published[0][1]['fallback_identity_basis'], 'pid_only')
                self.assertIsNone(self.published[0][1]['process_start_utc_ticks'])

    def test_active_initialization_failures_still_publish_without_masking_exception(self):
        identity = {'candidate_sha': 'a'*40, 'activation_sha256': 'b'*64, 'policy_sha256': 'c'*64}
        plan = {'request': {'root': str(self.directory),
                           'target': {'identity': identity, 'expires_at_ms': int(time.time()*1000)+10000}},
                'drain_grace_ms': 1000}
        for constructor in ('NativeInspector', 'NewsReviewStatusProbe', 'MonitorReceiptWriter'):
            with self.subTest(constructor=constructor):
                self.published.clear()
                original = RuntimeError('synthetic initialization failure')
                constructors = {name: Mock() for name in
                                ('NativeInspector', 'NewsReviewStatusProbe', 'MonitorReceiptWriter')}
                constructors[constructor].side_effect = original
                with patch.multiple(observer, **constructors), \
                        patch.object(observer, 'publish_json_once', side_effect=self.primary_fails):
                    with self.assertRaises(RuntimeError) as raised:
                        observer.observe_active(plan, 'd'*64, self.directory,
                                                observer_identity=(os.getpid(), self.ticks))
                self.assertIs(raised.exception, original)
                payload = self.published[0][1]
                self.assertEqual(payload['phase'], 'observer_interrupted_or_failed')
                self.assertIsNone(payload['completed_checks'])
                self.assertEqual(payload['identity'], identity)
                self.assertEqual(payload['process_start_utc_ticks'], self.ticks)

    def test_waiting_initialization_failure_reuses_pin_and_preserves_exception(self):
        directory = self.directory/'waiting'
        plan = {'output_directory': str(directory),
                'request': {'candidate_sha': 'a'*40, 'activation_sha256': 'b'*64},
                'activation_eligibility_until_ms': int(time.time()*1000)+10000,
                'activation': {'duration_ms': 10000}}
        startup = []
        query = Mock(side_effect=[self.ticks, OSError('teardown query unavailable')])
        original = RuntimeError('synthetic waiting initialization failure')
        with patch.object(observer, 'native_creation_ticks', query), \
                patch.object(waiting, 'WaitingSession', side_effect=original), \
                patch.object(waiting, 'publish_json_once',
                             side_effect=lambda path, payload: startup.append(copy.deepcopy(payload))), \
                patch.object(observer, 'publish_json_once', side_effect=self.primary_fails):
            with self.assertRaises(RuntimeError) as raised:
                waiting.run_waiting(plan, 'd'*64, core=observer)
        self.assertIs(raised.exception, original)
        query.assert_called_once_with(os.getpid())
        pin = startup[0]
        payload = self.published[0][1]
        self.assertEqual(payload['pid'], pin['pid'])
        self.assertEqual(payload['process_start_utc_ticks'], pin['process_start_utc_ticks'])
        self.assertEqual(payload['fallback_identity_basis'], 'startup_pin')
        self.assertIsNone(payload['identity']['policy_sha256'])
        self.assertEqual(payload['completed_wait_checks'], 0)
        self.assertEqual(payload['code'], 'OBSERVER_WAIT_INCOMPLETE')


if __name__ == '__main__':
    unittest.main()
