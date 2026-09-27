"""Bounded local status probe for a separately prepared process-tree inspector.

No inspector is started here. The caller must supply the approved live native
tree check, including its registered descendants and passive listener check.
This module never opens SQLite, follows redirects, or contacts source/model URLs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import socket
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

ENDPOINTS = ('/api/monitoring/news-review/control', '/api/monitoring/health')
MAX_HEADER_BYTES = 16_384
MAX_BODY_BYTES = 262_144
STATUS_TIMEOUT_SECONDS = 15


class ProbeError(ValueError):
    """Only bounded error codes are exposed; raw responses stay in memory."""


def require(condition, code):
    if not condition:
        raise ProbeError(code)


def integer(value, *, positive=False):
    return type(value) is int and (0 < value if positive else 0 <= value) and value <= 2**63 - 1


def canonical(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def local_port(url):
    require(type(url) is str, 'host_url_invalid')
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ProbeError('host_url_invalid') from None
    require(type(port) is int and 1024 <= port <= 65535 and port not in (8770, 11111), 'host_port_invalid')
    require(url in (f'http://127.0.0.1:{port}', f'http://127.0.0.1:{port}/'), 'host_url_invalid')
    return port


def native_ticks(value):
    """Preserve the seventh fractional digit in native .NET UTC pin strings."""
    match = re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.(\d{1,7}))?(?:Z|[+-]\d{2}:\d{2})', value) if type(value) is str else None
    require(match is not None, 'process_creation_time_invalid')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
        delta = parsed - datetime(1, 1, 1, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        raise ProbeError('process_creation_time_invalid') from None
    fraction = match.group(1) or ''
    return (delta.days * 86400 + delta.seconds) * 10_000_000 + int(fraction.ljust(7, '0'))


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, 'status_duplicate_json_key')
        value[key] = item
    return value


def _reject_nonfinite(_value):
    raise ProbeError('status_nonfinite_json')


def get_status_json(port, path, *, monotonic=time.monotonic, wall_ms=None, stop_at_ms=None):
    """One GET, no proxy/DNS/redirect/retry, with a total 15-second deadline.

    The local server emits Content-Length. Ambiguous framing, compression and
    chunked responses are rejected rather than introducing an unbounded parser.
    Each blocking socket operation receives only the remaining total budget.
    """
    local_port(f'http://127.0.0.1:{port}')
    require(type(port) is int and path in ENDPOINTS, 'status_endpoint_invalid')
    require((wall_ms is None) == (stop_at_ms is None), 'status_deadline_invalid')
    if stop_at_ms is not None:
        require(integer(stop_at_ms, positive=True), 'status_deadline_invalid')
    deadline = monotonic() + STATUS_TIMEOUT_SECONDS

    def remaining():
        budget = deadline - monotonic()
        if wall_ms is not None:
            now = wall_ms()
            require(integer(now), 'status_clock_invalid')
            budget = min(budget, (stop_at_ms - now) / 1000)
        if budget <= 0:
            raise TimeoutError('STATUS_READ_TIMEOUT')
        return budget

    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        connection.settimeout(remaining())
        connection.connect(('127.0.0.1', port))
        connection.settimeout(remaining())
        request = (f'GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n'
                   'Accept: application/json\r\nConnection: close\r\n\r\n').encode('ascii')
        connection.sendall(request)
        data = bytearray()
        while b'\r\n\r\n' not in data:
            connection.settimeout(remaining())
            chunk = connection.recv(min(4096, MAX_HEADER_BYTES + 1 - len(data)))
            require(bool(chunk), 'status_truncated_headers')
            data.extend(chunk)
            marker = data.find(b'\r\n\r\n')
            require(marker <= MAX_HEADER_BYTES if marker >= 0 else len(data) <= MAX_HEADER_BYTES,
                    'status_headers_too_large')
        raw_headers, body = bytes(data).split(b'\r\n\r\n', 1)
        lines = raw_headers.split(b'\r\n')
        match = re.fullmatch(rb'HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?', lines[0])
        require(match is not None, 'status_http_invalid')
        status = int(match.group(1))
        require(status == 200, 'status_redirect_rejected' if 300 <= status < 400 else 'status_http_non_200')
        headers = {}
        for line in lines[1:]:
            require(b':' in line, 'status_headers_invalid')
            name, value = line.split(b':', 1)
            name = name.lower()
            require(re.fullmatch(rb'[a-z0-9-]+', name) is not None and name not in headers,
                    'status_headers_invalid')
            headers[name] = value.strip()
        require(b'transfer-encoding' not in headers and b'content-encoding' not in headers,
                'status_framing_unsupported')
        length = headers.get(b'content-length', b'')
        require(re.fullmatch(rb'[0-9]{1,6}', length) is not None, 'status_length_invalid')
        length = int(length)
        require(0 < length <= MAX_BODY_BYTES and len(body) <= length, 'status_body_size_invalid')
        require(headers.get(b'content-type', b'').split(b';')[0].strip().lower() == b'application/json',
                'status_content_type_invalid')
        data = bytearray(body)
        while len(data) < length:
            connection.settimeout(remaining())
            chunk = connection.recv(min(4096, length - len(data)))
            require(bool(chunk), 'status_truncated_body')
            data.extend(chunk)
        remaining()
        try:
            value = json.loads(bytes(data).decode('utf-8'), object_pairs_hook=_unique_json,
                parse_constant=_reject_nonfinite)
        except (UnicodeError, ValueError, RecursionError):
            raise ProbeError('status_json_invalid') from None
        remaining()
        require(type(value) is dict, 'status_json_invalid')
        return value
    except (OSError, TimeoutError) as exc:
        if isinstance(exc, TimeoutError):
            raise TimeoutError('STATUS_READ_TIMEOUT') from None
        raise ProbeError('status_transport_unconfirmed') from None
    finally:
        connection.close()


class NewsReviewStatusProbe:
    """Adapt a verified native tree inspector to MonitorReceiptWriter.

    A missing parent is reported separately while its registered owner/host is
    still alive. No absence or owner metadata is used to declare termination.
    The inspector must retain its full native pins across checks; this adapter
    does not reconstruct them, acquire ownership or authorize a new trial.
    """
    def __init__(self, target, *, inspect_tree, wall_ms, fetch=get_status_json):
        require(type(target) is dict and set(target) == {'identity', 'policy_id', 'activated_at_ms',
            'expires_at_ms', 'host_url', 'source_standards'}, 'monitor_target_invalid')
        identity = target['identity']
        require(type(identity) is dict and set(identity) == {'candidate_sha', 'activation_sha256', 'policy_sha256'},
                'monitor_identity_invalid')
        require(all(type(value) is str and re.fullmatch('[0-9a-f]{' + str(40 if key == 'candidate_sha' else 64) + '}', value)
                    for key, value in identity.items()), 'monitor_identity_invalid')
        require(type(target['policy_id']) is str and re.fullmatch('[A-Za-z0-9_.:-]{1,160}', target['policy_id']),
                'monitor_policy_invalid')
        start, end = target['activated_at_ms'], target['expires_at_ms']
        require(integer(start, positive=True) and integer(end, positive=True) and 0 < end-start <= 86_400_000,
                'monitor_window_invalid')
        local_port(target['host_url'])
        standards = target['source_standards']
        require(type(standards) is dict and set(standards) == {'sec_filings', 'company_ir'}, 'monitor_standards_invalid')
        for value in standards.values():
            require(type(value) is dict and set(value) == {'first_success_ms', 'maximum_gap_ms'}
                    and all(integer(v, positive=True) for v in value.values()), 'monitor_standards_invalid')
        self.target = copy.deepcopy(target)
        self.inspect_tree, self.wall_ms, self.fetch = inspect_tree, wall_ms, fetch
        self._registered_pins = set()
        self._host_pin = None
        self._last_health_capture = None
        self._last_source_success = {}

    def _time(self):
        now = self.wall_ms()
        require(integer(now) and self.target['activated_at_ms'] <= now < self.target['expires_at_ms'] + 255_000,
                'monitor_window_closed_or_unconfirmed')
        return now

    def _tree(self):
        self._time()
        tree = self.inspect_tree()
        identity = self.target['identity']
        require(type(tree) is dict and all(tree.get(key) == value for key, value in identity.items()),
                'process_tree_identity_mismatch')
        require(tree.get('safe_status_reads_allowed') is True
                and tree.get('known_identity_inspection_incomplete') is False
                and tree.get('owner_identity_pinned') is True and tree.get('owner_alive') is True,
                'process_tree_unconfirmed')
        require(tree.get('host_url') == self.target['host_url'], 'process_tree_host_mismatch')
        processes = tree.get('processes')
        require(type(processes) is list and 1 <= len(processes) <= 128, 'process_tree_unconfirmed')
        identities = set()
        for process in processes:
            require(type(process) is dict and integer(process.get('pid'), positive=True)
                    and type(process.get('start_utc')) is str and bool(process['start_utc'])
                    and type(process.get('alive')) is bool and process.get('pid_reused') is False,
                    'process_identity_unconfirmed')
            require('parent_pid' in process and (process['parent_pid'] is None or integer(process['parent_pid'])),
                    'process_identity_unconfirmed')
            pin = (process['pid'], native_ticks(process['start_utc']))
            require(pin not in identities, 'process_identity_unconfirmed')
            identities.add(pin)
        pins = tree.get('pins')
        require(type(pins) is list and len(pins) == len(processes)
                and all(type(p) is dict and integer(p.get('pid'), positive=True) for p in pins),
                'registered_processes_unconfirmed')
        registered = {(p.get('pid'), native_ticks(p.get('start_utc'))) for p in pins}
        require(registered == identities and self._registered_pins <= registered, 'registered_processes_unconfirmed')
        owner_pid, launcher_pid = tree.get('owner_pid'), tree.get('launcher_pid')
        require(integer(owner_pid, positive=True) and integer(launcher_pid, positive=True), 'process_tree_unconfirmed')
        owners = [p for p in processes if p['pid'] == owner_pid]
        launchers = [p for p in processes if p['pid'] == launcher_pid]
        require(len(owners) == len(launchers) == 1 and owners[0]['alive'], 'process_tree_unconfirmed')
        owner_pin = (owner_pid, native_ticks(owners[0]['start_utc']))
        require(self._host_pin is None or self._host_pin == owner_pin, 'host_generation_changed')
        port = local_port(self.target['host_url'])
        listeners = tree.get('listeners')
        require(type(listeners) is list and len(listeners) == 1 and type(listeners[0]) is dict
                and type(listeners[0].get('LocalPort')) is int and type(listeners[0].get('OwningProcess')) is int
                and listeners == [{'LocalAddress': '127.0.0.1', 'LocalPort': port, 'OwningProcess': owner_pid}],
                'listener_ownership_unconfirmed')
        self._time()
        self._registered_pins = registered
        self._host_pin = owner_pin
        return launchers[0]['alive']

    def __call__(self):
        launcher_alive = self._tree()
        target = self.target
        def read(path):
            return self.fetch(local_port(target['host_url']), path, wall_ms=self.wall_ms,
                              stop_at_ms=target['expires_at_ms'] + 255_000)
        envelope = read(ENDPOINTS[0])
        require(type(envelope) is dict and envelope.get('ok') is True, 'control_unconfirmed')
        control = envelope.get('news_review')
        require(type(control) is dict and control.get('version') == 'news_event_review_v1'
                and control.get('policy_sha256') == target['identity']['policy_sha256'], 'control_identity_mismatch')
        policy = control.get('policy')
        require(type(policy) is dict and policy.get('candidate_sha') == target['identity']['candidate_sha']
                and policy.get('policy_id') == target['policy_id']
                and policy.get('not_before_ms') == target['activated_at_ms']
                and policy.get('expires_at_ms') == target['expires_at_ms']
                and canonical(policy) == target['identity']['policy_sha256'], 'control_identity_mismatch')
        workers = control.get('workers_alive')
        require(type(workers) is dict and set(workers) == {'news-review-evidence', 'news-review-model'}
                and all(type(v) is bool for v in workers.values()), 'worker_status_unconfirmed')
        launcher_alive = self._tree() and launcher_alive
        envelope = read(ENDPOINTS[1])
        require(type(envelope) is dict and envelope.get('ok') is True, 'health_unconfirmed')
        health = envelope.get('source_monitoring_health')
        require(type(health) is dict and health.get('version') == 'source_monitoring_health_service_v4'
                and type(health.get('runtime_liveness_verified')) is bool, 'health_unconfirmed')
        adapters = health.get('adapters')
        require(type(adapters) is list and len(adapters) == 2 and all(type(v) is dict for v in adapters)
                and sorted(v.get('adapter_key', '') for v in adapters) == ['company_ir', 'sec_filings'], 'source_scope_mismatch')
        now, stale = self._time(), []
        captured = health.get('captured_at_ms')
        require(integer(captured) and 0 <= now - captured <= 30_000, 'health_timestamp_unconfirmed')
        require(self._last_health_capture is None or captured >= self._last_health_capture,
                'health_timestamp_regressed')
        for adapter in adapters:
            last, key = adapter.get('last_success_at_ms'), adapter['adapter_key']
            require(integer(last) and (last == 0 or target['activated_at_ms'] <= last <= captured),
                    'source_timestamp_unconfirmed')
            require(last >= self._last_source_success.get(key, 0), 'source_success_regressed')
            require(type(adapter.get('enabled')) is bool and type(adapter.get('runtime_liveness_verified')) is bool,
                    'source_status_unconfirmed')
            standard = target['source_standards'][key]
            late = (now - last > standard['maximum_gap_ms'] if last else
                    now - target['activated_at_ms'] > standard['first_success_ms'])
            if late or not adapter['enabled']:
                stale.append(key)
        launcher_alive = self._tree() and launcher_alive
        require(0 <= self._time() - captured <= 30_000, 'health_timestamp_unconfirmed')
        self._last_health_capture = captured
        self._last_source_success = {v['adapter_key']:v['last_success_at_ms'] for v in adapters}
        return {'identity': copy.deepcopy(target['identity']), 'process_identity_verified': True,
            'port_ownership_verified': True, 'health': {'host_alive': True,
                'workers_alive': all(workers.values()) and health['runtime_liveness_verified']
                    and all(v['runtime_liveness_verified'] for v in adapters),
                'source_stale_keys': sorted(stale), 'parent_launcher_alive': launcher_alive}}
