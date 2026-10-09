"""Prepare or explicitly run one independent read-only Windows observer.

Preparation does not start an observer or issue HTTP. Running needs the exact
prepared plan hash and a new output directory. No resume/restart mode exists.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from backend.news_review_monitor import MonitorReceiptWriter, publish_json_once
from backend.news_review_monitor_probe import NewsReviewStatusProbe, ProbeError, canonical, integer, native_ticks, require
from scripts.news_review_watchdog import native_creation_ticks

FILES = ('backend/news_review_monitor.py', 'backend/news_review_monitor_probe.py',
    'scripts/news_review_watchdog.py', 'scripts/run_news_review_observer.py',
    'scripts/inspect_news_review_process_tree.ps1', 'scripts/news_review_observer_wait.py')
RECORDS = ('preparation-manifest.json', 'user-approval.json', 'ci-verification.json',
    'launch-receipt.json', 'launcher-start.json', 'activation-receipt.json', 'activation-policy.json')


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def safe_path(value):
    require(type(value) is str and Path(value).is_absolute(), 'absolute_path_required')
    path = Path(value)
    for part in (path, *path.parents):
        require(not part.is_symlink() and not part.is_junction(), 'linked_path_rejected')
    return path.resolve()


def bounded_code(value, fallback='unclassified_error'):
    """Normalise an exception type name to a bounded, log-safe token.

    Mirrors ``backend/news_review_stop.py`` so a receipt never carries free
    text: only ``[A-Za-z][A-Za-z0-9_]{0,99}`` is retained, everything else
    collapses to the fallback.  ``str(exc)`` is never written.
    """
    return value if type(value) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,99}', value) else fallback


def publish_exit_receipt(directory, ended, *, fallback_version, ticks_provider=None,
                         observer_identity=None):
    """Publish the primary exit receipt, degrading to a fallback on failure.

    Returns ``'primary_published'``, ``'fallback_only_published'``,
    ``'fallback_present_content_unverified'``, ``'both_failed'`` or
    ``'receipt_directory_rejected:<bounded>'``.  It never raises: a publication
    failure is recorded as a bounded code, not propagated, so it cannot mask the
    original exception nor alter process teardown.

    Review fixes R-01..R-04 are enforced here rather than left to callers:

    * **R-01** production callers reuse their confirmed startup pin, avoiding a
      second native query during teardown. Standalone callers can query inside
      this protected path via ``ticks_provider``. A query that raises is recorded as
      ``identity_unavailable:<bounded>`` and cannot abort the caller's ``finally``
      block; a query that yields no exact creation time produces
      ``observer-exit-fallback-unidentified-<pid>.json`` -- deliberately *not*
      the exact-ticks name, and it never claims a pin-level match.
    * **R-02** the publication directory is re-validated with the existing
      ``safe_path`` immediately before writing.  On rejection nothing is written
      and ``receipt_directory_rejected:<bounded>`` is returned.
    * **R-03** every failure code is merged back into ``ended['failure_codes']``,
      so pre-existing report failures survive a fallback publication, and a
      primary+fallback double failure keeps both codes instead of leaving them in
      process memory only.
    * **R-04** a same-name collision is reported as
      ``fallback_present_content_unverified`` -- "the path exists, its content was
      not verified".  It is never called positive evidence, and no runtime
      validator or watchdog is added to check it.

    The fallback receipt remains **offline corroborating evidence only**: not read
    by the watchdog receipt chain, opening no alert, never marking the run as
    normally closed out.  The three always-false fields are re-asserted here so a
    degraded path can never become an acceptance shortcut, and
    ``publish_json_once`` still refuses to overwrite an existing file
    (``os.link``).  ``safe_path``'s symlink/junction checks are unchanged.
    """
    codes = [c for c in (ended.get('failure_codes') or [])]

    def commit(extra=()):
        merged = codes + [c for c in extra]
        ended['failure_codes'] = merged
        return merged

    # R-02: re-validate the directory at write time, not only at plan time.
    try:
        directory = safe_path(str(Path(directory)))
    except Exception as exc:
        code = 'receipt_directory_rejected:' + bounded_code(type(exc).__name__)
        commit([code])
        return code
    pid = os.getpid()
    ticks = None
    identity_basis = 'pid_only'
    if observer_identity is not None:
        if (type(observer_identity) is tuple and len(observer_identity) == 2
                and integer(observer_identity[0], positive=True)
                and observer_identity[0] == pid
                and integer(observer_identity[1], positive=True)):
            ticks = observer_identity[1]
            identity_basis = 'startup_pin'
        else:
            codes.append('startup_identity_unconfirmed')
        ended.update(pid=pid, process_start_utc_ticks=ticks)
        commit()
    try:
        publish_json_once(directory/'observer-exit.json', ended)
        commit()
        return 'primary_published'
    except Exception as exc:
        codes.append('exit_receipt_publish_failed:' + bounded_code(type(exc).__name__))
    # Production callers reuse the already-confirmed startup pin. Standalone
    # callers may use the protected query, but a malformed result never claims
    # a native identity and never replaces an explicitly rejected startup pin.
    if observer_identity is None:
        try:
            candidate_ticks = ticks_provider(pid) if callable(ticks_provider) else None
            if integer(candidate_ticks, positive=True):
                ticks = candidate_ticks
                identity_basis = 'native_creation_ticks'
            elif candidate_ticks is not None:
                codes.append('identity_unconfirmed')
        except Exception as exc:
            codes.append('identity_unavailable:' + bounded_code(type(exc).__name__))
    fallback_payload = dict(ended)
    fallback_payload['version'] = fallback_version
    fallback_payload['failure_codes'] = list(codes)
    fallback_payload['fallback_identity_basis'] = identity_basis
    fallback_payload['pid'] = pid
    fallback_payload['process_start_utc_ticks'] = ticks
    fallback_payload['final_acceptance_verified'] = False
    fallback_payload['monitored_processes_stopped_by_observer'] = False
    fallback_payload['database_opened'] = False
    name = (f'observer-exit-fallback-{pid}-{ticks}.json' if ticks is not None
            else f'observer-exit-fallback-unidentified-{pid}.json')
    try:
        publish_json_once(directory/name, fallback_payload)
        commit()
        return 'fallback_only_published'
    except FileExistsError:
        # R-04: the path exists; its content is NOT verified and it is NOT
        # treated as positive evidence.
        commit(['fallback_present_content_unverified'])
        return 'fallback_present_content_unverified'
    except Exception as exc:
        # R-03: keep both failure codes instead of losing them with the process.
        commit(['exit_receipt_fallback_publish_failed:' + bounded_code(type(exc).__name__)])
        return 'both_failed'


def read_json(path, limit=262144):
    return read_record(path, limit)[0]


def read_record(path, limit=262144):
    with Path(path).open('rb') as stream:
        data = stream.read(limit+1)
    require(len(data) <= limit, 'receipt_size_limit')
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'duplicate_receipt_field')
            result[key] = value
        return result
    def invalid(_value):
        raise ProbeError('nonfinite_receipt_value')
    return (json.loads(data.decode('utf-8-sig'), object_pairs_hook=pairs, parse_constant=invalid),
            hashlib.sha256(data).hexdigest())


def pin_key(pin):
    require(type(pin) is dict and set(pin) == {'pid', 'start_utc', 'parent_pid'}
            and integer(pin['pid'], positive=True)
            and (pin['parent_pid'] is None or integer(pin['parent_pid'])), 'process_pin_invalid')
    return pin['pid'], native_ticks(pin['start_utc'])


def verify_checkout(candidate):
    for args, expected in ((['rev-parse', 'HEAD'], candidate), (['status', '--porcelain'], '')):
        result = subprocess.run(['git', '-C', str(APP), *args], capture_output=True, timeout=15,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        require(result.returncode == 0 and result.stdout.decode('ascii').strip() == expected,
                'observer_candidate_unconfirmed')


def verify_preparation(root, identity, manifest, ci, *, approval=None, manifest_sha256=None):
    require(manifest.get('candidate_sha') == ci.get('candidate_sha') == identity['candidate_sha']
            and manifest.get('activation_sha256') == identity['activation_sha256'], 'trial_candidate_mismatch')
    if approval is not None:
        require(approval.get('approved') is True and approval.get('preparation_manifest_sha256') == manifest_sha256
                and all(approval.get(k) == identity[k] for k in ('candidate_sha','activation_sha256')),
                'trial_approval_unconfirmed')
    entries = manifest.get('files')
    require(type(entries) is list and 1 <= len(entries) <= 128, 'preparation_manifest_invalid')
    names = set()
    for entry in entries:
        name = entry.get('name') if type(entry) is dict else None
        require(type(name) is str and Path(name).name == name and name not in names
                and Path(name).suffix.lower() in {'.json', '.py', '.ps1', '.psm1', '.md'}
                and not re.search(r'key|secret|credential|password|raw|log', name, re.I), 'manifest_file_rejected')
        names.add(name)
        require(sha(safe_path(str(root/name))) == entry.get('sha256'), 'prepared_file_changed')
    runs = ci.get('workflow_runs')
    require(ci.get('evidence_reviewed') is True and type(runs) is list and len(runs) >= 2
            and all(type(v) is dict and v.get('head_sha') == identity['candidate_sha']
                    and v.get('status') == 'completed' and v.get('conclusion') == 'success'
                    and integer(v.get('id'), positive=True) for v in runs)
            and len({v['id'] for v in runs}) == len(runs), 'trial_ci_unconfirmed')


def verify_trial(request):
    require(type(request) is dict and set(request) == {'version', 'root', 'target', 'host_receipt_name',
        'launcher_pin', 'host_pin', 'pins'}, 'observer_request_invalid')
    require(request['version'] == 'news_review_observer_request_v1', 'observer_request_invalid')
    root = safe_path(request['root'])
    require(root.name.startswith('NewsReviewTrial') and not root.is_relative_to(APP.parent), 'isolated_trial_required')
    target = request['target']
    NewsReviewStatusProbe(target, inspect_tree=lambda:None, wall_ms=lambda:0)
    identity = target['identity']
    require(type(request['pins']) is list and 1 <= len(request['pins']) <= 128, 'process_pins_invalid')
    keys = [pin_key(pin) for pin in request['pins']]
    require(len(keys) == len(set(keys)) and pin_key(request['launcher_pin']) in keys
            and pin_key(request['host_pin']) in keys, 'process_pins_invalid')
    by_pid = {pin['pid']:pin for pin in request['pins']}
    require(len(by_pid) == len(keys), 'process_pins_invalid')
    for pin in request['pins']:
        seen, current = set(), pin
        while pin_key(current) != pin_key(request['launcher_pin']):
            require(current['pid'] not in seen and current['parent_pid'] in by_pid, 'process_ancestry_unconfirmed')
            seen.add(current['pid'])
            parent = by_pid[current['parent_pid']]
            require(native_ticks(parent['start_utc']) <= native_ticks(current['start_utc']), 'process_ancestry_unconfirmed')
            current = parent
    require(type(request['host_receipt_name']) is str
            and re.fullmatch(r'host-[A-Za-z0-9_-]+\.json', request['host_receipt_name']), 'host_receipt_invalid')
    require([p.name for p in root.glob('host-*.json')] == [request['host_receipt_name']], 'host_receipt_ambiguous')
    values, hashes = {}, {}
    for name in (*RECORDS, request['host_receipt_name']):
        path = safe_path(str(root/name))
        values[name], hashes[name] = read_record(path)
    approval = values['user-approval.json']
    manifest = values['preparation-manifest.json']
    ci = values['ci-verification.json']
    verify_preparation(root, identity, manifest, ci, approval=approval, manifest_sha256=hashes[RECORDS[0]])
    for value in (approval, manifest, ci, values['launch-receipt.json'], values['launcher-start.json']):
        require(value.get('candidate_sha') == identity['candidate_sha'], 'trial_candidate_mismatch')
    for value in (approval, manifest, values['launch-receipt.json'], values['launcher-start.json']):
        require(value.get('activation_sha256') == identity['activation_sha256'], 'trial_activation_mismatch')
    launch, start = values['launch-receipt.json'], values['launcher-start.json']
    launcher = request['launcher_pin']
    require(launch.get('process_id') == start.get('launcher_pid') == launcher['pid']
            and native_ticks(launch.get('process_start_utc')) == native_ticks(start.get('launcher_process_start_utc'))
                == native_ticks(launcher['start_utc']), 'launcher_receipt_mismatch')
    activation, policy = values['activation-receipt.json'], values['activation-policy.json']
    require(safe_path(policy.get('database_path')) == root/'data'/'studio.sqlite3', 'trial_database_identity_mismatch')
    proposal = activation.get('activation')
    require(type(proposal) is dict and canonical(proposal) == identity['activation_sha256']
            == activation.get('activation_sha256'), 'activation_receipt_mismatch')
    require(activation.get('policy_sha256') == identity['policy_sha256'] == canonical(policy), 'policy_receipt_mismatch')
    begin, end = target['activated_at_ms'], target['expires_at_ms']
    require(activation.get('activated_at') == policy.get('not_before_ms') == begin
            and activation.get('expires_at') == policy.get('expires_at_ms') == end
            and policy.get('candidate_sha') == identity['candidate_sha'] and policy.get('policy_id') == target['policy_id'],
            'activation_window_mismatch')
    template = proposal.get('policy_template', {})
    changes = proposal.get('permitted_changes')
    require(changes in (['not_before_ms', 'expires_at_ms'], ['not_before_ms', 'expires_at_ms', 'clock_activation'])
            and proposal.get('activations') == 1 and proposal.get('duration_ms') == end-begin
            and template.get('not_before_ms', -1) <= begin < template.get('expires_at_ms', -1), 'activation_resolution_invalid')
    require({k:v for k,v in policy.items() if k not in changes} == {k:v for k,v in template.items() if k not in changes},
            'activation_resolution_invalid')
    if 'clock_activation' in changes:
        reading = policy.get('clock_activation', {})
        require(reading.get('process_id') == request['host_pin']['pid']
                and reading.get('process_creation_utc_ticks') == native_ticks(request['host_pin']['start_utc']),
                'activation_host_pin_mismatch')
    host = values[request['host_receipt_name']]
    require(host.get('url') == target['host_url'] and host.get('policy_sha256') == identity['policy_sha256']
            and host.get('expires_at_ms') == end, 'host_receipt_mismatch')
    if 'process_id' in host or 'process_start_utc_ticks' in host:
        require(host.get('process_id') == request['host_pin']['pid']
                and type(host.get('process_start_utc_ticks')) is int
                and host['process_start_utc_ticks'] == native_ticks(request['host_pin']['start_utc']),
                'host_native_pin_mismatch')
    return root, hashes


def prepare(request, powershell):
    if type(request) is dict and request.get('version') == 'news_review_observer_wait_request_v1':
        from scripts.news_review_observer_wait import prepare_wait
        return prepare_wait(request, powershell, core=sys.modules[__name__])
    root, records = verify_trial(request)
    verify_checkout(request['target']['identity']['candidate_sha'])
    shell = safe_path(powershell)
    require(shell.name.lower() == 'pwsh.exe' and shell.is_file(), 'powershell7_required')
    plan = {'version': 'news_review_observer_plan_v1', 'request': copy.deepcopy(request),
        'receipt_sha256': records, 'monitor_files': {name:sha(APP/name) for name in FILES},
        'powershell': {'path':str(shell), 'sha256':sha(shell)},
        'python': {'path':str(Path(sys.executable).resolve()), 'sha256':sha(sys.executable)},
        'output_directory': str(root/'monitoring'), 'poll_interval_ms':300000,
        'inspection_timeout_seconds':45, 'status_timeout_seconds':15, 'maximum_silence_ms':360000,
        'drain_native_interval_ms':10000, 'drain_grace_ms':255000,
        'observer_activations':1, 'request_authorized':False}
    return {'plan':plan, 'plan_sha256':canonical(plan)}


def verify_plan(wrapped, approved_hash):
    require(type(wrapped) is dict and set(wrapped) == {'plan', 'plan_sha256'}, 'observer_plan_invalid')
    plan = wrapped['plan']
    require(wrapped['plan_sha256'] == canonical(plan) == approved_hash, 'observer_approval_mismatch')
    require(plan == prepare(plan['request'], plan['powershell']['path'])['plan'], 'observer_preparation_changed')
    require(os.name == 'nt', 'native_windows_required')
    now = time.time_ns()//1_000_000
    if plan['version'] == 'news_review_observer_wait_plan_v1':
        require(now < plan['activation_eligibility_until_ms'], 'observer_window_closed')
    else:
        target = plan['request']['target']
        require(target['activated_at_ms'] <= now < target['expires_at_ms']+255000, 'observer_window_closed')
    return plan


class NativeInspector:
    def __init__(self, plan, directory, *, run=subprocess.run):
        self.plan, self.directory, self.run = copy.deepcopy(plan), Path(directory), run
        self.pins = copy.deepcopy(plan['request']['pins'])
        self.sequence = 0
        self.last = None
        self.waiting = plan['request']['host_pin'] is None
        keys=[pin_key(pin) for pin in self.pins]
        require(1 <= len(keys) <= 128 and len(keys) == len(set(keys))
                and pin_key(plan['request']['launcher_pin']) in keys, 'native_launcher_pin_missing')
        if not self.waiting:
            require(pin_key(plan['request']['host_pin']) in keys, 'native_host_pin_missing')

    def bind_active(self, plan):
        prior, current = self.plan['request']['target']['identity'], plan['request']['target']['identity']
        require(self.waiting and prior['policy_sha256'] is None
                and all(prior[k] == current[k] for k in ('candidate_sha','activation_sha256'))
                and {pin_key(p) for p in self.pins} <= {pin_key(p) for p in plan['request']['pins']},
                'observer_activation_rebinding_forbidden')
        self.plan = copy.deepcopy(plan)
        self.pins = copy.deepcopy(plan['request']['pins'])
        self.waiting = False

    def __call__(self, *, deadline_monotonic_ms=None, expires_at_ms=None):
        plan, request = self.plan, self.plan['request']
        require((deadline_monotonic_ms is None) == (expires_at_ms is None)
                and (deadline_monotonic_ms is None or
                     integer(deadline_monotonic_ms,positive=True) and integer(expires_at_ms,positive=True)),
                'native_inspection_deadline_invalid')
        require(sha(plan['powershell']['path']) == plan['powershell']['sha256'], 'powershell_changed')
        for name, expected in plan['monitor_files'].items():
            require(sha(APP/name) == expected, 'monitor_source_changed')
        self.sequence += 1
        input_path = self.directory/f'process-tree-input-{self.sequence:06d}.json'
        output_path = self.directory/f'process-tree-check-{self.sequence:06d}.json'
        identity = request['target']['identity']
        publish_json_once(input_path, {'version':'news_review_native_inspection_input_v1',
            'identity':identity, 'host_url':request['target']['host_url'], 'launcher_pin':request['launcher_pin'],
            'host_pin':request['host_pin'], 'pins':self.pins})
        try:
            timeout = 45
            if deadline_monotonic_ms is not None:
                timeout = min(timeout,(deadline_monotonic_ms-time.monotonic_ns()//1_000_000)/1000,
                              (expires_at_ms-time.time_ns()//1_000_000)/1000)
                require(timeout > 0, 'observer_window_closed')
            result = self.run([plan['powershell']['path'], '-NoProfile', '-NonInteractive', '-File',
                str(APP/'scripts/inspect_news_review_process_tree.ps1'), '-InputPath', str(input_path)],
                capture_output=True, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            require(result.returncode == 0 and len(result.stdout) <= 65536, 'native_inspection_failed')
            tree = json.loads(result.stdout.decode('utf-8-sig'))
            expected_version = 'news_review_native_wait_inspection_v1' if self.waiting else 'news_review_native_inspection_v1'
            require(type(tree) is dict and tree.get('version') == expected_version
                    and all(tree.get(k) == v for k,v in identity.items()), 'native_inspection_identity_mismatch')
            require(type(tree.get('pins')) is list and len(tree['pins']) <= 128, 'native_inspection_pins_invalid')
            new_keys = [pin_key(pin) for pin in tree['pins']]
            require(len(set(new_keys)) == len(new_keys) and {pin_key(pin) for pin in self.pins} <= set(new_keys),
                    'native_inspection_lost_registered_pin')
            processes = tree.get('processes')
            require(type(processes) is list and len(processes) == len(new_keys)
                    and all(type(p) is dict and set(p) == {'pid','start_utc','parent_pid','alive','pid_reused'}
                            and type(p['alive']) is bool and type(p['pid_reused']) is bool for p in processes),
                    'native_inspection_processes_invalid')
            require({pin_key({k:p[k] for k in ('pid','parent_pid','start_utc')}) for p in processes} == set(new_keys)
                    and tree.get('owner_pid') == (None if self.waiting else request['host_pin']['pid'])
                    and tree.get('launcher_pid') == request['launcher_pin']['pid'], 'native_inspection_processes_invalid')
            require(set(tree) == {'version','checked_at_utc','candidate_sha','activation_sha256','policy_sha256',
                'launcher_pid','pins','processes','enumeration_consistent','known_identity_inspection_incomplete',
                'owner_pid','owner_identity_pinned','owner_alive','listeners','host_url','safe_status_reads_allowed',
                'ownership_lock_verified','terminal_status_inferred','database_opened','http_requests'},
                'native_inspection_shape_invalid')
            native_ticks(tree['checked_at_utc'])
            require(tree['host_url'] == request['target']['host_url']
                    and all(type(tree[k]) is bool for k in ('enumeration_consistent','known_identity_inspection_incomplete',
                        'owner_identity_pinned','owner_alive','safe_status_reads_allowed','ownership_lock_verified',
                        'terminal_status_inferred','database_opened'))
                    and tree['ownership_lock_verified'] is tree['terminal_status_inferred'] is tree['database_opened'] is False
                    and type(tree['http_requests']) is int and tree['http_requests'] == 0, 'native_inspection_shape_invalid')
            require(type(tree['listeners']) is list and len(tree['listeners']) <= 128, 'native_listener_shape_invalid')
            for listener in tree['listeners']:
                require(type(listener) is dict and set(listener) == {'LocalAddress','LocalPort','OwningProcess'}
                        and type(listener['LocalAddress']) is str and len(listener['LocalAddress']) <= 64
                        and integer(listener['LocalPort'], positive=True) and listener['LocalPort'] <= 65535
                        and integer(listener['OwningProcess']), 'native_listener_shape_invalid')
                ipaddress.ip_address(listener['LocalAddress'])
            if self.waiting:
                require(tree['host_url'] is None and not tree['listeners'] and tree['safe_status_reads_allowed'] is False
                        and tree['owner_identity_pinned'] is False and tree['owner_alive'] is False, 'waiting_inspection_invalid')
            # Retain descendants even when a later protocol/HTTP check fails.
            self.pins = tree['pins']
            self.last = tree
            publish_json_once(output_path, tree)
            require(os.getpid() not in {pin['pid'] for pin in self.pins}, 'observer_not_independent')
            return tree
        except subprocess.TimeoutExpired:
            publish_json_once(output_path, {'identity':identity, 'complete':False, 'code':'NATIVE_INSPECTION_TIMEOUT'})
            raise ProbeError('native_inspection_timeout') from None
        except Exception as exc:
            if not output_path.exists():
                publish_json_once(output_path, {'identity':identity, 'complete':False, 'code':'NATIVE_INSPECTION_UNCONFIRMED'})
            if isinstance(exc, ProbeError) and str(exc) == 'observer_not_independent':
                raise
            raise ProbeError('native_inspection_unconfirmed') from None


def terminal_tree(tree):
    return (type(tree) is dict and tree.get('known_identity_inspection_incomplete') is False
            and tree.get('enumeration_consistent') is True and tree.get('owner_identity_pinned') is True
            and type(tree.get('processes')) is list and bool(tree['processes'])
            and all(p.get('alive') is False and p.get('pid_reused') is False for p in tree['processes'])
            and tree.get('listeners') == [])


def run_observer(plan, plan_hash):
    plan = verify_plan({'plan':plan, 'plan_sha256':canonical(plan)}, plan_hash)
    if plan['version'] == 'news_review_observer_wait_plan_v1':
        from scripts.news_review_observer_wait import run_waiting
        return run_waiting(plan, plan_hash, core=sys.modules[__name__])
    directory = safe_path(plan['output_directory'])
    directory.mkdir(exist_ok=False)
    target = plan['request']['target']
    identity = target['identity']
    ticks = native_creation_ticks(os.getpid())
    require(integer(ticks, positive=True), 'observer_native_identity_unconfirmed')
    publish_json_once(directory/'observer-pin.json', {'version':'news_review_direct_observer_pin_v1',
        'identity':identity, 'plan_sha256':plan_hash, 'plan':plan,
        'pid':os.getpid(), 'process_start_utc_ticks':ticks, 'started_at_ms':time.time_ns()//1_000_000})
    return observe_active(plan, plan_hash, directory,
                          observer_identity=(os.getpid(), ticks))


def observe_active(plan, plan_hash, directory, *, inspector=None, observer_identity=None):
    """Internal continuation of the same already-approved observer process."""
    target = plan['request']['target']
    identity = target['identity']
    stop = threading.Event()
    ended = {'phase':'drain_deadline_unconfirmed_user_action_required', 'two_native_terminal_checks':False}
    wall = lambda:time.time_ns()//1_000_000
    mono = lambda:time.monotonic_ns()//1_000_000
    hard_end = target['expires_at_ms']+plan['drain_grace_ms']
    hard_mono = mono()+max(0,hard_end-wall())
    def inspect(*, draining=False):
        def native():
            if not draining:
                return inspector()
            require(wall() < hard_end and mono() < hard_mono, 'observer_window_closed')
            value = inspector(deadline_monotonic_ms=hard_mono,expires_at_ms=hard_end)
            require(wall() < hard_end and mono() < hard_mono, 'observer_window_closed')
            return value
        tree = native()
        if terminal_tree(tree):
            second = native()
            if terminal_tree(second):
                ended.update(phase='registered_tree_terminal', two_native_terminal_checks=True)
                stop.set()
            return second
        return tree
    fatal_codes = {'control_identity_mismatch','control_unconfirmed','source_scope_mismatch',
                   'host_generation_changed','process_tree_identity_mismatch','monitor_window_closed_or_unconfirmed',
                   'monitor_source_changed','powershell_changed','observer_not_independent'}
    def observe():
        try:
            return probe()
        except ProbeError as exc:
            if str(exc) in fatal_codes:
                ended.update(phase='inspection_failure_user_action_required', code=str(exc))
                stop.set()
            raise
    writer = None
    try:
        inspector = inspector or NativeInspector(plan, directory)
        probe = NewsReviewStatusProbe(target, inspect_tree=inspect, wall_ms=lambda:time.time_ns()//1_000_000)
        writer = MonitorReceiptWriter(directory, identity, wall_ms=lambda:time.time_ns()//1_000_000,
                                      monotonic_ms=lambda:time.monotonic_ns()//1_000_000)
        schedule_end = writer.run_schedule(probe=observe, stop_event=stop, expires_at_ms=target['expires_at_ms'],
                                          interval_ms=plan['poll_interval_ms'])
        hard_mono = min(hard_mono,schedule_end['deadline_monotonic_ms']+plan['drain_grace_ms'])
        # Ordinary GET cadence must not skip the shorter final drain window.
        # Native-only checks continue under the original absolute AND elapsed
        # hard bounds; no extra status/source/model requests are issued.
        if not stop.is_set() and wall() < hard_end and mono() < hard_mono:
            publish_json_once(directory/'observer-drain-start.json',{
                'version':'news_review_observer_drain_start_v2','identity':identity,'plan_sha256':plan_hash,
                'started_at_ms':wall(),'started_monotonic_ms':mono(),
                'original_expires_at_ms':target['expires_at_ms'],'schedule_end':schedule_end,
                'deadline_at_ms':hard_end,'deadline_monotonic_ms':hard_mono,'http_get_requests':0})
            sequence,previous = 0,''
            while not stop.is_set() and wall() < hard_end and mono() < hard_mono:
                started,started_mono = wall(),mono()
                tree,code = None,''
                try:
                    tree = inspect(draining=True)
                except ProbeError as exc:
                    code = str(exc)
                    if code in fatal_codes:
                        ended.update(phase='inspection_failure_user_action_required',code=code)
                        stop.set()
                sequence += 1
                receipt = {'version':'news_review_observer_drain_execution_v1','identity':identity,
                    'plan_sha256':plan_hash,'sequence':sequence,'previous_sha256':previous,
                    'started_at_ms':started,'completed_at_ms':wall(),'elapsed_monotonic_ms':mono()-started_mono,
                    'inspection_complete':bool(tree and tree.get('known_identity_inspection_incomplete') is False
                                               and tree.get('enumeration_consistent') is True),
                    'two_native_terminal_checks':ended['two_native_terminal_checks'],'error_code':code,
                    'http_get_requests':0}
                previous = canonical(receipt)
                publish_json_once(directory/f'observer-drain-execution-{sequence:06d}.json',
                                  {'receipt':receipt,'sha256':previous})
                if not stop.is_set():
                    delay = min(plan['drain_native_interval_ms'],hard_end-wall(),hard_mono-mono())
                    if delay > 0: stop.wait(delay/1000)
    except BaseException:
        ended.update(phase='observer_interrupted_or_failed', code='OBSERVER_INCOMPLETE')
        raise
    finally:
        failure_codes = []
        # Resolve the host report path, but never let a rejection abort the
        # whole receipt.  An unresolved path means "unknown", not "absent":
        # original_report_exists stays None so a degraded path cannot turn
        # "cannot confirm" into "confirmed missing" (the closeout checklist's
        # original error).  safe_path's symlink/junction checks are unchanged.
        try:
            report = safe_path(str(Path(plan['request']['root'])/'window-report.json'))
        except Exception as exc:
            report = None
            failure_codes.append('report_path_unresolved:' + bounded_code(type(exc).__name__))
        report_exists = None
        report_sha = None
        if report is not None:
            try:
                report_exists = report.is_file()
            except Exception as exc:
                failure_codes.append('report_existence_unconfirmed:' + bounded_code(type(exc).__name__))
            if report_exists:
                try:
                    report_sha = sha(report)
                except Exception as exc:
                    failure_codes.append('report_hash_unavailable:' + bounded_code(type(exc).__name__))
        ended.update(version='news_review_observer_exit_v1', identity=identity, plan_sha256=plan_hash,
            ended_at_ms=time.time_ns()//1_000_000, completed_checks=getattr(writer, 'sequence', None),
            original_report_exists=report_exists, final_acceptance_verified=False,
            original_report_sha256=report_sha,
            monitored_processes_stopped_by_observer=False, database_opened=False,
            failure_codes=failure_codes)
        # Phase refinement.  Only a *confirmed absent* report is the classic
        # "missing report" fault; an unknown report state is reported separately
        # and never collapsed into the confirmed-missing verdict.
        if ended['two_native_terminal_checks'] and report_exists is False:
            ended.update(phase='terminal_report_missing_user_action_required')
        elif ended['two_native_terminal_checks'] and report_exists is None:
            ended.update(phase='terminal_report_state_unconfirmed_user_action_required')
        # Reuse the same PID/ticks written in the startup pin. The provider is
        # needed only for standalone callers that did not supply a startup pin.
        publication = publish_exit_receipt(directory, ended,
            fallback_version='news_review_observer_exit_fallback_v1',
            ticks_provider=native_creation_ticks, observer_identity=observer_identity)
        ended['exit_receipt_publication'] = publication
    # The return code reflects whether closeout publication was *confirmed*,
    # which is orthogonal to the native terminal fact.  A native-terminal run
    # whose primary exit receipt was not published must NOT return 0, otherwise
    # the "closeout unconfirmed" signal is lost (report 8.3.3).  The native
    # terminal fact stays in two_native_terminal_checks / phase, unaltered.
    if ended['phase'] != 'registered_tree_terminal':
        return 2
    return 0 if ended.get('exit_receipt_publication') == 'primary_published' else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepare', action='store_true')
    action.add_argument('--run', action='store_true')
    parser.add_argument('--request')
    parser.add_argument('--powershell')
    parser.add_argument('--output')
    parser.add_argument('--plan')
    parser.add_argument('--approve-plan-sha256')
    args = parser.parse_args(argv)
    if args.prepare:
        require(bool(args.request and args.powershell and args.output) and not args.plan and not args.approve_plan_sha256,
                'observer_prepare_arguments_invalid')
        wrapped = prepare(read_json(safe_path(args.request)), args.powershell)
        publish_json_once(safe_path(args.output), wrapped)
        print(json.dumps({'plan_sha256':wrapped['plan_sha256'], 'request_authorized':False}))
        return 0
    require(bool(args.plan and args.approve_plan_sha256) and not any((args.request,args.powershell,args.output)),
            'observer_run_arguments_invalid')
    wrapped = read_json(safe_path(args.plan))
    plan = verify_plan(wrapped, args.approve_plan_sha256)
    return run_observer(plan, wrapped['plan_sha256'])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({'ok':False, 'code':'OBSERVER_INCOMPLETE', 'automatic_restart_allowed':False}))
        raise SystemExit(2)
