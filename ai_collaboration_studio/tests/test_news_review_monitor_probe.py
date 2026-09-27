import copy
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

from backend.news_review_monitor import MonitorReceiptWriter, evaluate_watchdog, notification_changes
from backend.news_review_monitor_probe import (
    ENDPOINTS, NewsReviewStatusProbe, ProbeError, canonical, get_status_json, local_port, native_ticks,
)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000
        policy = {'candidate_sha': 'a'*40, 'policy_id': 'probe-fixture',
                  'not_before_ms': self.now-1000, 'expires_at_ms': self.now+86_399_000}
        identity = {'candidate_sha': policy['candidate_sha'], 'activation_sha256': 'b'*64,
                    'policy_sha256': canonical(policy)}
        self.target = {'identity': identity, 'policy_id': policy['policy_id'],
            'activated_at_ms': policy['not_before_ms'], 'expires_at_ms': policy['expires_at_ms'],
            'host_url': 'http://127.0.0.1:55029', 'source_standards': {
                'sec_filings': {'first_success_ms': 900_000, 'maximum_gap_ms': 900_000},
                'company_ir': {'first_success_ms': 1_800_000, 'maximum_gap_ms': 1_800_000}}}
        pins = [{'pid': 101, 'parent_pid': None, 'start_utc': '2026-09-27T00:00:00.1234567Z'},
                {'pid': 102, 'parent_pid': 101, 'start_utc': '2026-09-27T00:00:01.1234567Z'}]
        self.tree = {**identity, 'safe_status_reads_allowed': True,
            'known_identity_inspection_incomplete': False, 'owner_identity_pinned': True, 'owner_alive': True,
            'owner_pid': 102, 'launcher_pid': 101, 'host_url': self.target['host_url'],
            'pins': pins, 'processes': [{**p, 'alive': True, 'pid_reused': False} for p in pins],
            'listeners': [{'LocalAddress': '127.0.0.1', 'LocalPort': 55029, 'OwningProcess': 102}]}
        self.control = {'ok': True, 'news_review': {'version': 'news_event_review_v1', 'policy': policy,
            'policy_sha256': identity['policy_sha256'],
            'workers_alive': {'news-review-evidence': True, 'news-review-model': True}}}
        self.health = {'ok': True, 'source_monitoring_health': {
            'version': 'source_monitoring_health_service_v4', 'runtime_liveness_verified': True,
            'captured_at_ms': self.now, 'adapters': [{
                'adapter_key': key, 'enabled': True, 'runtime_liveness_verified': True,
                'last_success_at_ms': self.now-500,
            } for key in ('sec_filings', 'company_ir')]}}
        self.calls = []

    def fetch(self, port, path, **kwargs):
        self.calls.append((port, path))
        return copy.deepcopy(self.control if path == ENDPOINTS[0] else self.health)

    def probe(self, inspect_tree=None, fetch=None):
        return NewsReviewStatusProbe(self.target, inspect_tree=inspect_tree or (lambda:copy.deepcopy(self.tree)),
                                    wall_ms=lambda:self.now, fetch=fetch or self.fetch)

    def test_valid_read_is_exactly_two_gets_and_three_live_inspections(self):
        inspections = []
        def inspect():
            inspections.append(True)
            return copy.deepcopy(self.tree)
        result = self.probe(inspect)()
        self.assertEqual(self.calls, [(55029, path) for path in ENDPOINTS])
        self.assertEqual(len(inspections), 3)
        self.assertEqual(result['health'], {'host_alive': True, 'workers_alive': True,
            'source_stale_keys': [], 'parent_launcher_alive': True})
        self.assertNotIn('policy', result)

    def test_unverified_or_reused_process_and_wrong_listener_prevent_any_get(self):
        original = copy.deepcopy(self.tree)
        mutations = [lambda t:t.pop('known_identity_inspection_incomplete'),
            lambda t:t.update(safe_status_reads_allowed=False),
            lambda t:t.update(owner_identity_pinned=False),
            lambda t:t['processes'][1].update(pid_reused=True),
            lambda t:t['processes'][1].pop('start_utc'),
            lambda t:t['listeners'][0].update(LocalAddress='0.0.0.0'),
            lambda t:t['listeners'][0].update(OwningProcess=103),
            lambda t:t['pins'].pop()]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.tree = copy.deepcopy(original)
                mutate(self.tree)
                with self.assertRaises(ProbeError):
                    self.probe()()
                self.assertEqual(self.calls, [])

    def test_bad_control_identity_stops_before_health_get(self):
        self.control['news_review']['policy']['expires_at_ms'] += 1
        with self.assertRaisesRegex(ProbeError, 'control_identity_mismatch'):
            self.probe()()
        self.assertEqual(self.calls, [(55029, ENDPOINTS[0])])

    def test_port_replacement_between_gets_prevents_health_read(self):
        def fetch(port, path, **kwargs):
            result = self.fetch(port, path, **kwargs)
            self.tree['listeners'][0]['OwningProcess'] = 103
            return result
        with self.assertRaisesRegex(ProbeError, 'listener_ownership_unconfirmed'):
            self.probe(fetch=fetch)()
        self.assertEqual(self.calls, [(55029, ENDPOINTS[0])])

    def test_changed_native_tick_after_health_cannot_be_saved_as_success(self):
        def fetch(port, path, **kwargs):
            result = self.fetch(port, path, **kwargs)
            if path == ENDPOINTS[1]:
                for rows in ('pins', 'processes'):
                    self.tree[rows][1]['start_utc'] = '2026-09-27T00:00:01.1234568Z'
            return result
        with self.assertRaises(ProbeError):
            self.probe(fetch=fetch)()
        self.assertEqual(len(self.calls), 2)

    def test_registered_children_cannot_disappear_from_later_inspection(self):
        probe = self.probe()
        extra = {'pid': 103, 'parent_pid': 102, 'start_utc': '2026-09-27T00:00:02.1234567Z'}
        self.tree['pins'].append(extra)
        self.tree['processes'].append({**extra, 'alive': True, 'pid_reused': False})
        probe()
        self.tree['pins'].pop()
        self.tree['processes'].pop()
        with self.assertRaisesRegex(ProbeError, 'registered_processes_unconfirmed'):
            probe()
        self.assertEqual(len(self.calls), 2)

    def test_parent_exit_is_separate_incident_and_child_is_still_observed(self):
        import tempfile
        self.tree['processes'][0]['alive'] = False
        with tempfile.TemporaryDirectory(prefix='monitor-probe-fixture-') as root:
            writer = MonitorReceiptWriter(root, self.target['identity'], wall_ms=lambda:self.now, monotonic_ms=lambda:1)
            receipt = writer.execute(scheduled_at_ms=self.now, probe=self.probe())
            self.assertTrue(receipt['read_succeeded'])
            verdict = evaluate_watchdog(receipt, identity=self.target['identity'], now_ms=self.now, observer_identity_alive=True)
        self.assertEqual(verdict['alerts'], ['LAUNCHER_EXITED_CHILD_ALIVE'])
        self.assertFalse(notification_changes(verdict['alerts'], verdict)['notify'])
        self.assertEqual(len(self.calls), 2)

    def test_no_first_success_and_stale_full_success_are_independent_of_pending(self):
        self.now += 1_800_001
        health = self.health['source_monitoring_health']
        health['captured_at_ms'] = self.now
        health['adapters'][0]['last_success_at_ms'] = 0
        health['adapters'][1]['last_error_code'] = 'MICRON_IR_METADATA_REVALIDATION_PENDING'
        self.assertEqual(self.probe()()['health']['source_stale_keys'], ['company_ir', 'sec_filings'])
        health['adapters'][1]['last_error_code'] = 'HTTP_READ_TIMEOUT'
        self.assertEqual(self.probe()()['health']['source_stale_keys'], ['company_ir', 'sec_filings'])

    def test_initial_grace_and_actual_full_success_are_distinguished(self):
        for adapter in self.health['source_monitoring_health']['adapters']:
            adapter['last_success_at_ms'] = 0
        self.assertEqual(self.probe()()['health']['source_stale_keys'], [])
        self.now += 900_000
        self.health['source_monitoring_health']['captured_at_ms'] = self.now
        self.assertEqual(self.probe()()['health']['source_stale_keys'], ['sec_filings'])
        self.health['source_monitoring_health']['adapters'][0]['last_success_at_ms'] = self.now
        self.assertEqual(self.probe()()['health']['source_stale_keys'], [])

    def test_source_or_worker_heartbeat_is_not_source_success(self):
        self.health['source_monitoring_health']['runtime_liveness_verified'] = False
        self.assertFalse(self.probe()()['health']['workers_alive'])
        self.assertEqual(self.probe()()['health']['source_stale_keys'], [])

    def test_health_clock_or_full_success_regression_cannot_fake_recovery(self):
        probe = self.probe()
        probe()
        health = self.health['source_monitoring_health']
        health['captured_at_ms'] -= 1
        with self.assertRaisesRegex(ProbeError, 'health_timestamp_regressed'):
            probe()
        health['captured_at_ms'] += 1
        health['adapters'][1]['last_success_at_ms'] = 0
        with self.assertRaisesRegex(ProbeError, 'source_success_regressed'):
            probe()

    def test_expiry_plus_drain_and_future_health_fail_closed(self):
        self.health['source_monitoring_health']['captured_at_ms'] += 1
        with self.assertRaisesRegex(ProbeError, 'health_timestamp_unconfirmed'):
            self.probe()()
        self.calls.clear()
        self.now = self.target['expires_at_ms']+255_000
        with self.assertRaisesRegex(ProbeError, 'monitor_window_closed_or_unconfirmed'):
            self.probe()()
        self.assertEqual(self.calls, [])

    def test_native_creation_fraction_and_unsafe_urls(self):
        a = native_ticks('2026-09-27T00:00:01.1234567Z')
        self.assertEqual(native_ticks('2026-09-27T00:00:01.1234568Z'), a+1)
        self.assertEqual(native_ticks('2026-09-27T08:00:01.1234567+08:00'), a)
        for url in ('http://localhost:55029', 'https://127.0.0.1:55029', 'http://127.0.0.1:8770',
                    'http://127.0.0.1:11111', 'http://127.0.0.1:55029/path', 'http://127.0.0.1:55029?x=y',
                    'http://127.0.0.1:055029', 'http://user@127.0.0.1:55029'):
            with self.assertRaises(ProbeError):
                local_port(url)

    def test_actual_status_handlers_accept_probe_and_report_unstarted_workers(self):
        from http.server import ThreadingHTTPServer
        from backend import http_server
        from backend.news_review_controller import NewsReviewController
        from backend.source_monitoring.runtime import build_source_monitoring_runtime
        from backend.source_monitoring.settings import SourceMonitoringSettings
        from tests.test_news_review import NewsReviewTests
        fixture = NewsReviewTests()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.now = self.now = int(time.time()*1000)
        fixture.policy.update(not_before_ms=self.now, expires_at_ms=self.now+86_400_000)
        fixture.approve()
        controller = NewsReviewController(fixture.service, fixture.policy['policy_id'])
        controller.threads = [threading.Thread(name='news-review-'+lane) for lane in ('evidence', 'model')]
        settings = SourceMonitoringSettings(enabled=True, auto_start=True, dry_run=False,
            source_profile=fixture.policy['profile'], official_only=True, allow_readonly_market=False,
            initial_mode='seed_only', trading_impact_rules_enabled=False)
        runtime = build_source_monitoring_runtime(fixture.store, settings)
        # Construct the real profile but never start its source worker.
        fixture.service.prepare_sources(fixture.policy['policy_id'], runtime)
        server = ThreadingHTTPServer(('127.0.0.1', 0), http_server.StudioRequestHandler)
        server.ai_studio_instance_owner = fixture.owner
        server.ai_studio_news_review = controller
        server.ai_studio_source_monitoring_runtime = runtime
        port = server.server_port
        identity = {**self.target['identity'], 'policy_sha256': canonical(fixture.policy)}
        self.target.update(identity=identity, policy_id=fixture.policy['policy_id'],
            activated_at_ms=fixture.policy['not_before_ms'], expires_at_ms=fixture.policy['expires_at_ms'],
            host_url=f'http://127.0.0.1:{port}')
        self.tree.update(identity, host_url=self.target['host_url'])
        self.tree['listeners'][0]['LocalPort'] = port
        probe = NewsReviewStatusProbe(self.target, inspect_tree=lambda:copy.deepcopy(self.tree),
                                     wall_ms=lambda:int(time.time()*1000))
        worker = threading.Thread(target=server.serve_forever)
        with patch.object(http_server, 'STORE', fixture.store):
            worker.start()
            try:
                result = probe()
                self.assertTrue(result['health']['host_alive'])
                self.assertFalse(result['health']['workers_alive'])
                self.assertEqual(result['health']['source_stale_keys'], [])
            finally:
                server.shutdown()
                server.server_close()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(fixture.count('provider_call_attempts'), 0)


class TransportTests(unittest.TestCase):
    def wire(self, response):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(3)
        port = listener.getsockname()[1]
        requests, errors = [], []
        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(3)
                    requests.append(connection.recv(4096))
                    connection.sendall(response)
            except BaseException as exc:
                errors.append(type(exc).__name__)
            finally:
                listener.close()
        thread = threading.Thread(target=serve)
        thread.start()
        return port, thread, requests, errors

    def test_real_random_loopback_get_ignores_proxy_and_does_not_follow_redirect(self):
        cases = [(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 11\r\n\r\n{"ok":true}', None),
                 (b'HTTP/1.1 302 Found\r\nLocation: https://example.invalid/secret\r\nContent-Length: 0\r\n\r\n', 'status_redirect_rejected')]
        for response, failure in cases:
            port, thread, requests, errors = self.wire(response)
            try:
                with patch.dict('os.environ', {'HTTP_PROXY':'http://127.0.0.1:1', 'HTTPS_PROXY':'http://127.0.0.1:1', 'NO_PROXY':''}):
                    if failure:
                        with self.assertRaisesRegex(ProbeError, failure):
                            get_status_json(port, ENDPOINTS[0])
                    else:
                        self.assertEqual(get_status_json(port, ENDPOINTS[0]), {'ok': True})
            finally:
                thread.join(4)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(requests), 1)
            self.assertTrue(requests[0].startswith(b'GET '+ENDPOINTS[0].encode()+b' HTTP/1.1\r\n'))

    def test_ambiguous_framing_and_non_json_are_rejected(self):
        responses = [b'Content-Length: 2\r\nContent-Length: 2', b'Transfer-Encoding: chunked',
            b'Content-Length: 300000', b'Content-Length: 2\r\nContent-Encoding: gzip']
        for headers in responses:
            port, thread, _, _ = self.wire(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'+headers+b'\r\n\r\n{}')
            try:
                with self.assertRaises(ProbeError):
                    get_status_json(port, ENDPOINTS[0])
            finally:
                thread.join(4)
            self.assertFalse(thread.is_alive())

    def test_slow_headers_share_total_deadline_instead_of_resetting_per_chunk(self):
        clock = [0.0]
        class SlowSocket:
            def __init__(self):
                self.timeouts, self.reads, self.closed = [], 0, False
            def settimeout(self, value): self.timeouts.append(value)
            def connect(self, address): pass
            def sendall(self, value): pass
            def recv(self, size):
                clock[0] += 4.0
                self.reads += 1
                return b'x'
            def close(self): self.closed = True
        stream = SlowSocket()
        with patch('backend.news_review_monitor_probe.socket.socket', return_value=stream):
            with self.assertRaises(TimeoutError):
                get_status_json(55029, ENDPOINTS[0], monotonic=lambda:clock[0])
        self.assertEqual(stream.reads, 4)
        self.assertEqual(stream.timeouts[-4:], [15.0, 11.0, 7.0, 3.0])
        self.assertTrue(stream.closed)

    def test_missing_remaining_window_prevents_connect(self):
        with patch('backend.news_review_monitor_probe.socket.socket') as factory:
            with self.assertRaises(TimeoutError):
                get_status_json(55029, ENDPOINTS[0], wall_ms=lambda:1000, stop_at_ms=1000)
            factory.return_value.connect.assert_not_called()
            factory.return_value.close.assert_called_once()

    def test_slow_body_uses_budget_already_spent_reading_headers(self):
        clock = [0.0]
        class SlowBody:
            def __init__(self):
                self.timeouts, self.reads, self.closed = [], 0, False
            def settimeout(self, value): self.timeouts.append(value)
            def connect(self, address): pass
            def sendall(self, value): pass
            def recv(self, size):
                self.reads += 1
                clock[0] += 8.0
                if self.reads == 1:
                    return b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n'
                return b'{}'
            def close(self): self.closed = True
        stream = SlowBody()
        with patch('backend.news_review_monitor_probe.socket.socket', return_value=stream):
            with self.assertRaises(TimeoutError):
                get_status_json(55029, ENDPOINTS[0], monotonic=lambda:clock[0])
        self.assertEqual(stream.timeouts[-2:], [15.0, 7.0])
        self.assertTrue(stream.closed)


if __name__ == '__main__':
    unittest.main()
