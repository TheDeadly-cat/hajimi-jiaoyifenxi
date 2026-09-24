"""Actual launcher + HTTP host + runtime; isolated child, no real transports."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from backend.decision_lineage import canonical_sha256
from tests import test_news_review as fixtures


CHILD = r'''
import json, os, sys, subprocess, time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from scripts.run_news_review_trial import main
original_git = subprocess.check_output
def identity(args, **kw):
    if args[-2:] == ['rev-parse', 'HEAD']: return 'a'*40+'\n'
    if args[-2:] == ['status', '--porcelain']: return ''
    return original_git(args, **kw)
with ExitStack() as stack:
    stack.enter_context(patch('subprocess.check_output', side_effect=identity))
    if '--run' in sys.argv:
        config = json.loads(Path(sys.argv[sys.argv.index('--config')+1]).read_text())
        os.environ['AI_STUDIO_DATABASE_PATH'] = config['database_path']
        os.environ['AI_STUDIO_RUNTIME_DIR'] = str(Path(config['database_path']).parent.parent/'runtime')
        os.environ['SEC_USER_AGENT'] = 'Synthetic fixture@example.com'
        from backend.source_monitoring.runtime import build_source_monitoring_runtime
        from backend.news_review_report import NewsReviewJournal
        from backend.news_review_contracts import NewsReviewError
        scenario = os.environ['NEWS_RECOVERY_SCENARIO']
        def runtime_factory(*args, **kw):
            runtime = build_source_monitoring_runtime(*args, **kw)
            if scenario in {'runtime', 'checkpoint'}:
                def fail(*a, **k): raise RuntimeError('synthetic detail must not enter a receipt')
                runtime.scheduler.run_one_due = fail
            elif scenario == 'boundary':
                from backend.source_poll_control import ensure_source_poll_active
                def expire(*a, **kw):
                    while int(time.time()*1000) < config['expires_at_ms']:
                        time.sleep(0.01)
                    ensure_source_poll_active(cancel_event=kw['cancel_event'], deadline_monotonic_ms=kw['deadline_monotonic_ms'])
                    raise AssertionError('expired source authorization must not return')
                runtime.scheduler.run_one_due = expire
            else:
                # No source becomes due in this synthetic lifecycle fixture.
                runtime.scheduler.due_run_selections = lambda **kw: []
            return runtime
        stack.enter_context(patch('backend.source_monitoring.runtime.build_source_monitoring_runtime', side_effect=runtime_factory))
        if scenario == 'checkpoint':
            stack.enter_context(patch('backend.store.StudioStore.checkpoint_after_shutdown', side_effect=RuntimeError('synthetic checkpoint detail')))
        if scenario == 'observer':
            original_record = NewsReviewJournal.record
            def record(self, kind, **kw):
                if kind == 'heartbeat': raise NewsReviewError('synthetic_observer_fault')
                return original_record(self, kind, **kw)
            stack.enter_context(patch.object(NewsReviewJournal, 'record', record))
        stack.enter_context(patch('scripts.pilot_password_dialog.ask_api_key', return_value='synthetic-credential'))
    try:
        result = main(sys.argv[1:])
    except Exception as exc:
        print(json.dumps({'ok':False, 'code':getattr(exc,'code','news_trial_failed')}))
        result = 1
    raise SystemExit(result)
'''


class NewsReviewLauncherRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.NewsReviewTests()
        # Register on the executing case so cleanup exceptions reach its result.
        self.f.addCleanup = self.addCleanup
        self.f.setUp()

    def child(self, scenario, *args):
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', CHILD, *args],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=40,
            env={**os.environ, 'NEWS_RECOVERY_SCENARIO':scenario})
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result, json.loads(result.stdout.strip().splitlines()[-1])

    def test_real_launcher_distinguishes_runtime_observer_faults_and_planned_completion(self):
        for scenario, expected, expected_code in (
            ('runtime','runtime_failure','SOURCE_MONITORING_RUNTIME_FATAL'),
            ('observer','observer_failure','synthetic_observer_fault'),
            ('completed','window_elapsed',''),
            ('boundary','window_elapsed',''),
        ):
            with self.subTest(scenario=scenario):
                root = Path(self.f.temp.name)/('NewsReviewTrialHost'+scenario)
                init, created = self.child(scenario, '--initialize', '--root', str(root), '--candidate-sha', 'a'*40)
                self.assertEqual(init.returncode, 0, init.stdout+init.stderr)
                now = int(time.time()*1000)
                completed = scenario in {'completed', 'boundary'}
                policy = {**self.f.policy, 'database_path':created['database_path'], 'room_id':created['room_id'],
                          'resume_within_window':False, 'not_before_ms':now-1000,
                          'expires_at_ms':now+(8000 if completed else 30000)}
                config = root/'policy.json'
                config.write_text(json.dumps(policy), encoding='utf-8')
                output = root/'window-report.json'
                result, status = self.child(scenario, '--run', '--config', str(config),
                    '--approve-policy-sha256', canonical_sha256(policy), '--password-dialog',
                    '--sec-user-agent', 'Synthetic fixture@example.com', '--output', str(output))
                code = 0 if completed else 2
                self.assertEqual(result.returncode, code, result.stdout+result.stderr)
                self.assertEqual(status['ok'], code == 0)
                report = json.loads(output.read_text(encoding='utf-8'))
                stop = report['stop_events'][0]
                self.assertEqual(stop['stop_type'], expected)
                self.assertEqual(stop['error_code'], expected_code)
                self.assertEqual(stop['window_reached'], completed)
                self.assertEqual(report['outcome']['work_completed'], completed)
                self.assertTrue(report['outcome']['cleanup_clean'])
                self.assertFalse(report['outcome']['acceptance_passed'])
                self.assertFalse(report['event_end_to_end_recorded'])
                self.assertEqual(report['snapshot']['calls_reserved'], 0)
                hosts = list(root.glob('host-*.json'))
                self.assertEqual(len(hosts), 1)
                self.assertIn('http://127.0.0.1:', hosts[0].read_text())
                receipts = list(root.glob('stop-*.json'))
                self.assertGreaterEqual(len(receipts), 1)
                text = output.read_text()+''.join(p.read_text() for p in receipts)+result.stdout+result.stderr
                self.assertNotIn('synthetic-credential', text)
                self.assertNotIn('synthetic detail must not enter a receipt', text)

    def test_checkpoint_failure_has_no_success_report_and_preserves_stop_cause(self):
        root = Path(self.f.temp.name)/'NewsReviewTrialCheckpoint'
        init, created = self.child('checkpoint', '--initialize', '--root', str(root), '--candidate-sha', 'a'*40)
        self.assertEqual(init.returncode, 0, init.stdout+init.stderr)
        now = int(time.time()*1000)
        policy = {**self.f.policy, 'database_path':created['database_path'], 'room_id':created['room_id'],
                  'not_before_ms':now-1000, 'expires_at_ms':now+30000}
        config, output = root/'policy.json', root/'window-report.json'
        config.write_text(json.dumps(policy), encoding='utf-8')
        result, status = self.child('checkpoint', '--run', '--config', str(config),
            '--approve-policy-sha256', canonical_sha256(policy), '--password-dialog',
            '--sec-user-agent', 'Synthetic fixture@example.com', '--output', str(output))
        self.assertEqual(result.returncode, 1, result.stdout+result.stderr)
        self.assertFalse(status['ok'])
        self.assertFalse(output.exists())
        stops = [json.loads(p.read_text(encoding='utf-8')) for p in root.glob('stop-*.json')]
        self.assertTrue(any(s['stop_type'] == 'finalization_failure' and s['error_code'] == 'checkpoint_failed' for s in stops))
        self.assertNotIn('synthetic checkpoint detail', json.dumps(stops)+result.stdout+result.stderr)
