"""Offline launcher/observer contracts; no product, model, source, or real DB starts."""
import copy
import ctypes
import hashlib
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.news_review_monitor_probe import ProbeError
from scripts import launch_news_review_isolated as launcher
from scripts import run_news_review_observer as core
from tests import test_news_review_observer_wait as waiting_fixtures


def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


class LauncherObserverContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = waiting_fixtures.WaitingObserverTests()
        self.fixture.addCleanup = self.addCleanup
        self.fixture.setUp()
        self.root = self.fixture.root
        self.patch = patch.object(launcher, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def publish_boundaries(self, naming=launcher.boundary_receipt_name):
        for role in launcher.ROLES:
            launcher.publish(naming(role), {'synthetic_native_boundary': True, 'role': role})

    def waiting_step(self):
        session = self.fixture.session()
        self.fixture.activate()
        with patch.object(core, 'NativeInspector', self.fixture.inspector_class()), patch.object(core, 'verify_checkout'):
            return session.step()

    def test_real_waiting_and_active_contracts_accept_all_four_new_boundary_receipts(self):
        self.publish_boundaries()
        result = self.waiting_step()
        self.assertEqual(result['phase'], 'activation_receipts_bound')
        self.assertEqual(result['resolved_plan']['request']['host_receipt_name'], 'host-fixture.json')
        self.assertEqual([p.name for p in self.root.glob('host-*.json')], ['host-fixture.json'])
        # WaitingSession called the real active-layer prepare(), so both glob
        # contracts were exercised, rather than checking only the filename helper.
        with patch.object(core, 'verify_checkout'):
            active = core.prepare(result['resolved_plan']['request'], str(self.fixture.f.shell))
        self.assertEqual(active['plan']['request'], result['resolved_plan']['request'])

    def test_original_launcher_naming_reproduces_waiting_failure(self):
        self.publish_boundaries(lambda role: role + '-job-boundary.json')
        with self.assertRaisesRegex(ProbeError, 'host_receipt_ambiguous'):
            self.waiting_step()

    def test_original_launcher_naming_also_reproduces_active_failure(self):
        self.fixture.activate()
        self.publish_boundaries(lambda role: role + '-job-boundary.json')
        with patch.object(core, 'verify_checkout'), self.assertRaisesRegex(ProbeError, 'host_receipt_ambiguous'):
            core.prepare(self.fixture.f.request, str(self.fixture.f.shell))

    def test_two_real_host_receipts_still_fail_waiting(self):
        self.publish_boundaries()
        write(self.root / 'host-second.json', {'synthetic': 'second native host'})
        with self.assertRaisesRegex(ProbeError, 'host_receipt_ambiguous'):
            self.waiting_step()

    def test_two_real_host_receipts_still_fail_active(self):
        self.fixture.activate()
        self.publish_boundaries()
        shutil.copyfile(self.root / 'host-fixture.json', self.root / 'host-second.json')
        with patch.object(core, 'verify_checkout'), self.assertRaisesRegex(ProbeError, 'host_receipt_ambiguous'):
            core.prepare(self.fixture.f.request, str(self.fixture.f.shell))

    def test_boundary_publish_is_exclusive_and_retains_first_bytes(self):
        name = launcher.boundary_receipt_name('host')
        launcher.publish(name, {'synthetic': 1})
        original = (self.root / name).read_bytes()
        with self.assertRaises(FileExistsError):
            launcher.publish(name, {'synthetic': 2})
        self.assertEqual((self.root / name).read_bytes(), original)
        with self.assertRaisesRegex(launcher.BoundaryFault, 'boundary_role_invalid'):
            launcher.boundary_receipt_name('../host')


class LauncherPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='isolated-launch-preflight-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'NewsReviewTrialSyntheticLaunch'
        self.root.mkdir()
        self.tool = Path(temporary.name) / 'synthetic_recorder.py'
        self.tool.write_bytes(b'# synthetic bytes, never executed\n')
        self.tool_sha = hashlib.sha256(self.tool.read_bytes()).hexdigest()
        for name in launcher.LAUNCH_FILES:
            shutil.copyfile(Path(launcher.__file__).parent / name, self.root / name)
        self.source_sha = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in launcher.LAUNCH_FILES}
        for name, value in {'ROOT': self.root, 'WORKSPACE': Path(temporary.name) / 'candidate',
                            'APP': Path(temporary.name) / 'candidate' / 'ai_collaboration_studio',
                            'TOOL': self.tool, 'TOOL_SHA': self.tool_sha}.items():
            change = patch.object(launcher, name, value)
            change.start()
            self.addCleanup(change.stop)
        self.candidate, self.activation = 'a' * 40, 'b' * 64
        self.scope = {'candidate_sha': self.candidate, 'activation_sha256': self.activation,
                      'duration_ms': 900000, 'max_model_calls': 2, 'spend_limit_cny': '0.50'}
        now = time.time_ns() // 1000000
        self.policy = {'candidate_sha': self.candidate, 'max_model_calls': 2, 'spend_limit_cny': '0.50',
                       'sources': ['sec_filings:US.NVDA:8-K', 'company_ir:US.MU:recent-30'],
                       'provider': 'doubao', 'model': 'doubao-seed-2-1-pro-260915',
                       'endpoint': 'https://ark.cn-beijing.volces.com/api/v3/responses',
                       'max_document_requests': 16, 'max_request_bytes': 32768, 'max_output_tokens': 1400,
                       'concurrency': 1, 'stop_on_unknown': True, 'resume_within_window': False,
                       'not_before_ms': now - 5000, 'expires_at_ms': now + 3600000}
        self.step_names = ['Deterministic offline security baseline', 'Bootstrap locked dependencies and production frontend',
                           'Guarded frontend regression', 'Required historical reader compatibility matrix',
                           'Full isolated backend regression', 'Clean-source install, test, build, and startup smoke',
                           'Isolated install, upgrade, and rollback drill', 'Generate offline dependency inventory',
                           'Upload non-secret delivery receipts']
        def ci(head):
            return {'candidate_sha': head, 'evidence_reviewed': True, 'workflow_runs': [
                {'id': i, 'head_sha': head, 'status': 'completed', 'conclusion': 'success',
                 'required_steps': [{'name': name, 'status': 'completed', 'conclusion': 'success'} for name in self.step_names]}
                for i in (101, 102)]}
        self.runtime_ci = ci(self.candidate)
        self.recorder_ci = dict(ci(launcher.RECORDER_CANDIDATE), tool_source_sha256=self.tool_sha)
        self.launcher_ci = dict(ci('c' * 40), source_files=self.source_sha)
        self.approval = {'approved': True, 'expected_session_id': 1, 'candidate_sha': self.candidate,
                         'activation_sha256': self.activation}
        write(self.root / 'window-sec-contact-authorization.json', {'user_agent': 'Synthetic contact@example.invalid'})
        write(self.root / 'observer-plan.json', {'plan_sha256': 'd' * 64})
        write(self.root / 'activation-proposal.json', {'synthetic': 'never activated'})
        write(self.root / 'isolated-launch-bindings.json', {'version': 'news_review_isolated_launch_bindings_v1',
              'candidate_workspace': str(launcher.WORKSPACE), 'recorder_script': str(self.tool)})
        self.seal()

    def seal(self, *, omit=()):
        for name, value in [('window-execution-scope.json', self.scope), ('window-policy-template.json', self.policy),
                            ('ci-verification.json', self.runtime_ci), ('recorder-ci-verification.json', self.recorder_ci),
                            ('launcher-ci-verification.json', self.launcher_ci)]:
            write(self.root / name, value)
        names = [p.name for p in self.root.iterdir() if p.is_file() and p.name not in ('preparation-manifest.json', 'user-approval.json', *omit)]
        manifest = {'candidate_sha': self.candidate, 'activation_sha256': self.activation,
                    'files': [{'name': name, 'sha256': hashlib.sha256((self.root / name).read_bytes()).hexdigest()} for name in sorted(names)]}
        write(self.root / 'preparation-manifest.json', manifest)
        self.approval['preparation_manifest_sha256'] = hashlib.sha256((self.root / 'preparation-manifest.json').read_bytes()).hexdigest()
        write(self.root / 'user-approval.json', self.approval)

    def preflight(self):
        with patch.object(launcher.subprocess, 'Popen') as spawn, patch('socket.socket') as network:
            result = launcher.preflight({'session_id': 1})
            spawn.assert_not_called()
            network.assert_not_called()
            return result

    def test_sealed_short_preflight_reads_no_keys_starts_no_process_or_network(self):
        self.assertEqual(self.preflight()[-1], 900000)
        self.assertFalse((self.root / 'monitoring').exists())

    def test_sealed_new_24h_mode_has_its_own_eight_call_two_yuan_caps(self):
        self.scope.update(duration_ms=86400000, max_model_calls=8, spend_limit_cny='2.00')
        self.policy.update(max_model_calls=8, spend_limit_cny='2.00')
        self.seal()
        self.assertEqual(self.preflight()[-1], 86400000)

    def test_unapproved_duration_rejected(self):
        for duration in (True, '900000', 900001, 172800000):
            with self.subTest(duration=duration):
                self.scope['duration_ms'] = duration
                self.seal()
                with self.assertRaisesRegex(launcher.BoundaryFault, 'unapproved_window_mode'):
                    self.preflight()

    def test_caps_route_resume_and_unknown_policy_cannot_be_relaxed(self):
        original = copy.deepcopy(self.policy)
        for field, value in [('max_model_calls', 3), ('spend_limit_cny', '0.51'), ('max_document_requests', 17),
                             ('max_request_bytes', 32769), ('max_output_tokens', 1401), ('concurrency', 2),
                             ('provider', 'qwen'), ('model', 'another-model'), ('endpoint', 'https://example.invalid'),
                             ('resume_within_window', True), ('stop_on_unknown', False)]:
            with self.subTest(field=field):
                self.policy = dict(original, **{field: value})
                self.seal()
                with self.assertRaisesRegex(RuntimeError, 'isolated_scope_unconfirmed'):
                    self.preflight()

    def test_modified_file_not_silently_resealed(self):
        (self.root / 'window-policy-template.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, 'prepared_file_changed'):
            self.preflight()

    def test_unsealed_launcher_bindings_rejected_even_with_valid_other_files(self):
        self.seal(omit=('isolated-launch-bindings.json',))
        with self.assertRaisesRegex(launcher.BoundaryFault, 'launcher_materials_not_sealed'):
            self.preflight()

    def test_incomplete_runtime_recorder_or_launcher_ci_is_not_full_pass(self):
        for receipt, code in [(self.runtime_ci, 'runtime_ci_steps_incomplete'),
                              (self.recorder_ci, 'recorder_ci_steps_incomplete'),
                              (self.launcher_ci, 'launcher_ci_steps_incomplete')]:
            with self.subTest(code=code):
                step = receipt['workflow_runs'][0]['required_steps'].pop()
                self.seal()
                with self.assertRaisesRegex(launcher.BoundaryFault, code):
                    self.preflight()
                receipt['workflow_runs'][0]['required_steps'].append(step)

    def test_launcher_sha_cannot_borrow_other_candidates_green_ci(self):
        self.launcher_ci['workflow_runs'][1]['head_sha'] = 'e' * 40
        self.seal()
        with self.assertRaisesRegex(launcher.BoundaryFault, 'launcher_ci_steps_incomplete'):
            self.preflight()

    def test_launcher_source_binding_is_checked_independently_of_manifest(self):
        (self.root / launcher.LAUNCH_FILES[0]).write_bytes(b'# different launcher\n')
        self.seal()
        with self.assertRaisesRegex(launcher.BoundaryFault, 'launcher_source_changed'):
            self.preflight()

    def test_prior_activation_monitor_output_or_old_host_namespace_cannot_resume(self):
        for name in ('activation-receipt.json', 'monitoring', 'independent-exit', 'window-report.json', 'host-job-boundary.json'):
            with self.subTest(name=name):
                target = self.root / name
                target.write_bytes(b'{}')
                with self.assertRaisesRegex(RuntimeError, 'isolated_run_already_claimed|unclaimed_host_namespace_required'):
                    self.preflight()
                target.unlink()  # Only this synthetic temporary fixture.

    def test_session_change_and_expired_eligibility_rejected(self):
        self.approval['expected_session_id'] = 2
        self.seal()
        with self.assertRaisesRegex(launcher.BoundaryFault, 'approved_session_changed'):
            self.preflight()
        self.approval['expected_session_id'] = 1
        self.policy['expires_at_ms'] = self.policy['not_before_ms'] + 1
        self.seal()
        with self.assertRaisesRegex(RuntimeError, 'isolated_activation_eligibility_expired'):
            self.preflight()


class NativeBoundaryLogicTests(unittest.TestCase):
    def native(self, *, ticks=639272086895292896, wait=258, in_job=0, session=1, job_query=True):
        closed = []
        def job_query_function(handle, job, output):
            output._obj.value = in_job
            return job_query
        def session_function(pid, output):
            output._obj.value = session
            return True
        dll = SimpleNamespace(WaitForSingleObject=lambda handle, timeout: wait,
                              IsProcessInJob=job_query_function, ProcessIdToSessionId=session_function,
                              CloseHandle=lambda handle: closed.append(handle))
        native = SimpleNamespace(open=lambda pid: pid + 1000, times=lambda handle: (ticks, 0, 0, 0), dll=dll)
        return native, closed

    def test_live_identity_any_job_and_session_are_independent_requirements(self):
        pin = {'pid': 123, 'process_start_utc_ticks': '639272086895292896'}
        native, closed = self.native()
        result = launcher.process_boundary(native, pin, expected_session=1)
        self.assertTrue(result['outside_all_jobs'])
        self.assertEqual(result['session_id'], 1)
        self.assertEqual(closed, [1123])
        for arguments, code in [({'ticks': 639272086895292897}, 'boundary_identity_mismatch'),
                                ({'wait': 0}, 'boundary_process_not_live'),
                                ({'in_job': 1}, 'boundary_process_still_in_job'),
                                ({'session': 2}, 'boundary_session_mismatch'),
                                ({'job_query': False}, 'boundary_job_query_unconfirmed')]:
            with self.subTest(code=code):
                native, closed = self.native(**arguments)
                with self.assertRaisesRegex(launcher.BoundaryFault, code):
                    launcher.process_boundary(native, pin, expected_session=1)
                self.assertEqual(closed, [1123])

    def test_utc_pin_keeps_all_seven_fraction_digits_without_float(self):
        self.assertEqual(launcher.utc_pin('639272086895292896'), '2026-10-10T05:58:09.5292896Z')


if __name__ == '__main__':
    unittest.main()
