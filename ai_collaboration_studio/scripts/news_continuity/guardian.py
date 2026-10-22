"""Attach-only, console-free supervision of one existing observation.

This tool never creates or signals an observer, alters its window, contacts HTTP,
or writes its evidence. It owns only a fresh lifecycle receipt directory.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time

HERE = Path(__file__).resolve().parent
FIELDS = {'output_name', 'run_directory', 'observer_script', 'observer_sha256',
          'started_sha256', 'observer_pid', 'observer_creation_ticks', 'host_pid',
          'host_creation_ticks', 'backend_sha256', 'prior_guardian_exit_utc'}
TARGET, INTERVAL, MAXIMUM = 86400, 150, 86700
SOURCES = ['fed', 'bls', 'seagate']


class SupervisorError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise SupervisorError(code)


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def no_alias(path):
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode) and
                not getattr(info, 'st_file_attributes', 0) & 0x400,
                'path_alias_refused')


def object_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate_json_key')
        result[key] = value
    return result


def safe_read(path, maximum=1048576):
    no_alias(path)
    info = Path(path).lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
            info.st_size <= maximum, 'receipt_file_invalid')
    raw = Path(path).read_bytes()
    require(len(raw) <= maximum, 'receipt_file_invalid')
    return raw


def read_json(path, maximum=1048576):
    return json.loads(safe_read(path, maximum).decode('utf-8'),
                      object_pairs_hook=object_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(
                          SupervisorError('non_finite_json')))


def write_new(path, value):
    """Publish one durable receipt without replacing any existing evidence."""
    no_alias(path)
    with Path(path).open('xb') as stream:
        stream.write(canonical(value) + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def valid_integer(value, maximum):
    return type(value) is int and 1 <= value <= maximum


def local_absolute(path):
    require(isinstance(path, str) and Path(path).is_absolute() and
            not Path(path).drive.startswith('\\\\'), 'local_absolute_path_required')
    no_alias(path)
    return Path(path)


def utc_ticks(value):
    require(isinstance(value, str) and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?Z', value), 'utc_timestamp_invalid')
    whole, _, fraction = value[:-1].partition('.')
    parsed = datetime.strptime(whole, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
    delta = parsed - datetime(1601, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10000000 + int(fraction.ljust(7, '0') or '0')


def load_config(path):
    path = Path(os.path.abspath(path))
    require(path.parent == HERE and path.suffix == '.json', 'config_location_invalid')
    raw = safe_read(path, 16384)
    config = json.loads(raw.decode('utf-8'), object_pairs_hook=object_pairs)
    require(isinstance(config, dict) and set(config) == FIELDS, 'config_fields_invalid')
    name = config['output_name']
    require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', name)
            and not name.endswith('.') and name.lower().split('.')[0] not in
            {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))},
            'output_name_invalid')
    for key in ('host_pid', 'observer_pid'):
        require(valid_integer(config[key], 0xffffffff), 'configured_pid_invalid')
    for key in ('host_creation_ticks', 'observer_creation_ticks'):
        require(valid_integer(config[key], 0xffffffffffffffff), 'configured_creation_invalid')
    for key in ('backend_sha256', 'observer_sha256', 'started_sha256'):
        require(isinstance(config[key], str) and re.fullmatch(r'[a-f0-9]{64}', config[key]),
                'configured_hash_invalid')
    for key in ('run_directory', 'observer_script'):
        local_absolute(config[key])
    utc_ticks(config['prior_guardian_exit_utc'])
    return config, hashlib.sha256(raw).hexdigest()


def filetime_utc(ticks):
    require(type(ticks) is int and 0 < ticks <= 0xffffffffffffffff, 'filetime_invalid')
    seconds, fraction = divmod(ticks, 10000000)
    value = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return value.strftime('%Y-%m-%dT%H:%M:%S') + f'.{fraction:07d}Z'


class NativeProcess:
    """Read-only metadata from an already-held process handle."""
    def __init__(self):
        require(os.name == 'nt', 'windows_required')
        from ctypes import wintypes
        self.w = wintypes
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        self.kernel.GetConsoleProcessList.restype = wintypes.DWORD
        self.kernel.GetConsoleWindow.restype = wintypes.HWND
        self.kernel.GetProcessId.argtypes = [wintypes.HANDLE]
        self.kernel.GetProcessId.restype = wintypes.DWORD
        self.kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetExitCodeProcess.restype = wintypes.BOOL

    def snapshot(self, handle):
        pid = int(self.kernel.GetProcessId(handle))
        require(pid > 0, 'process_id_unavailable')
        values = [self.w.FILETIME() for _ in range(4)]
        require(self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)),
                'process_times_unavailable')
        ticks = [(value.dwHighDateTime << 32) | value.dwLowDateTime for value in values]
        waited = int(self.kernel.WaitForSingleObject(handle, 0))
        require(waited in (0, 258), 'process_wait_unavailable')
        result = {'pid': pid, 'creation_filetime_ticks': str(ticks[0]),
                  'state': 'exited' if waited == 0 else 'alive'}
        if waited == 0:
            code = self.w.DWORD()
            require(self.kernel.GetExitCodeProcess(handle, ctypes.byref(code)),
                    'process_exit_code_unavailable')
            # Refresh times after observing the signaled handle to avoid a race
            # where the first metadata read happened just before process exit.
            require(self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)),
                    'process_times_unavailable')
            exit_ticks = (values[1].dwHighDateTime << 32) | values[1].dwLowDateTime
            require(exit_ticks > 0, 'process_exit_time_unavailable')
            result.update(exit_code=int(code.value), exit_filetime_ticks=str(exit_ticks),
                          exited_utc=filetime_utc(exit_ticks))
        return result

    def own_identity(self):
        return identity(self.snapshot(self.kernel.GetCurrentProcess()))

    def console_evidence(self):
        processes = (self.w.DWORD * 16)()
        ctypes.set_last_error(0)
        count = int(self.kernel.GetConsoleProcessList(processes, len(processes)))
        error = ctypes.get_last_error()
        window = int(self.kernel.GetConsoleWindow() or 0)
        return {'console_process_count': count, 'console_process_list_error': error,
                'console_window_present': window != 0,
                'no_console_confirmed': count == 0 and error == 6 and window == 0}

    def open_bound_alive(self, expected):
        handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, int(expected['pid']))
        require(bool(handle), 'bound_process_handle_unavailable')
        try:
            snapshot = collect_exit(handle, expected, self)
            require(snapshot.get('identity_match') is True and snapshot.get('state') == 'alive',
                    'bound_process_not_confirmed_alive')
            return handle, snapshot
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def wait(self, handle, milliseconds=1000):
        result = int(self.kernel.WaitForSingleObject(handle, milliseconds))
        require(result in (0, 258), 'process_wait_unavailable')
        return result == 0

    def close(self, handle):
        if handle:
            self.kernel.CloseHandle(handle)


def identity(value):
    return {'pid': int(value['pid']), 'creation_filetime_ticks': str(value['creation_filetime_ticks'])}


def collect_exit(handle, expected_identity, native=None):
    """Read a retained handle, checking full identity before interpreting exit.

    The injectable native.snapshot(handle) seam supports offline unit tests.
    The retained handle is never rebound and no process is signaled.
    """
    native = native or NativeProcess()
    snapshot = native.snapshot(handle)
    expected = identity(expected_identity)
    observed = identity(snapshot)
    if expected != observed:
        return {'state': 'identity_changed', 'identity_match': False,
                'expected_identity': expected, 'observed_identity': observed}
    return {**snapshot, 'identity_match': True}


def safe_error(error):
    if isinstance(error, SupervisorError) and re.fullmatch(r'[a-z0-9_]{1,80}', error.code):
        return error.code
    if isinstance(error, KeyboardInterrupt):
        return 'supervisor_interrupted'
    if isinstance(error, SystemExit):
        return 'supervisor_system_exit'
    return 'supervisor_internal_error'


def closeout(run_path, expected_identity, exit_evidence, verify_fn, expected_binding=None):
    """Read child evidence and classify completion; never write to child files."""
    result = {'status': 'incomplete', 'normal_completion': False,
              'observed_for_at_least_24h': False, 'continuous_24h_passed': False}
    if exit_evidence.get('state') != 'exited' or exit_evidence.get('identity_match') is not True:
        return {**result, 'reason': 'child_exit_identity_unconfirmed'}
    if identity(exit_evidence) != identity(expected_identity):
        return {**result, 'reason': 'child_exit_identity_unconfirmed'}
    run_path = Path(run_path)
    if not (run_path / 'terminal.json').is_file():
        return {**result, 'reason': 'observer_terminal_missing',
                'exit_code': exit_evidence.get('exit_code')}
    try:
        verification = verify_fn(run_path)
        require(isinstance(verification, dict) and verification.get('verified') is True,
                'observer_verifier_rejected')
        started_bytes = safe_read(run_path / 'started.json')
        started_sha = hashlib.sha256(started_bytes).hexdigest()
        require(verification.get('started_sha256') == started_sha, 'started_hash_unbound')
        started = read_json(run_path / 'started.json')
        expected_started_identity = {'pid': int(expected_identity['pid']),
                                     'creation_filetime_ticks': int(expected_identity['creation_filetime_ticks'])}
        observed_identity = started.get('observer_identity')
        require(isinstance(observed_identity, dict) and
                set(observed_identity) == {'pid', 'creation_filetime_ticks'} and
                type(observed_identity['pid']) is int and
                type(observed_identity['creation_filetime_ticks']) is int and
                observed_identity == expected_started_identity, 'started_child_identity_mismatch')
        require(started.get('simulation') is False and
                started.get('target_seconds') == TARGET and
                started.get('interval_seconds') == INTERVAL and
                started.get('maximum_seconds') == MAXIMUM, 'started_window_invalid')
        if expected_binding is not None:
            require(started.get('binding') == expected_binding, 'started_host_binding_mismatch')
        receipt = read_json(run_path / 'terminal.json')
        terminal = receipt['terminal']
        require(terminal.get('binding') == started.get('binding') and
                terminal.get('simulation') is False, 'terminal_window_binding_mismatch')
        elapsed = terminal.get('elapsed_monotonic_seconds')
        require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0,
                'terminal_elapsed_invalid')
        result.update(verification={key: verification.get(key) for key in
                      ('verified', 'records', 'final_record_sha256', 'started_sha256')},
                      terminal_reason=terminal.get('reason'), elapsed_monotonic_seconds=elapsed,
                      exit_code=exit_evidence.get('exit_code'))
        if type(exit_evidence.get('exit_code')) is not int or exit_evidence['exit_code'] != 0:
            return {**result, 'reason': 'observer_nonzero_exit'}
        if terminal.get('reason') != 'observation_duration_reached' or elapsed < TARGET:
            return {**result, 'reason': 'observer_window_incomplete'}
        require(terminal.get('observed_for_at_least_24h') is True, 'terminal_duration_flag_mismatch')
        return {**result, 'status': 'completed', 'normal_completion': True,
                'observed_for_at_least_24h': True, 'reason': 'verified_duration_reached'}
    except BaseException as error:
        return {**result, 'reason': 'observer_evidence_verification_failed',
                'error_code': safe_error(error)}


def require_pythonw():
    executable = Path(sys.executable).resolve()
    base = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
    companion = base.with_name('pythonw.exe')
    require(executable.name.lower() == 'pythonw.exe' and executable == companion and
            companion.with_name('python.exe').is_file(), 'pythonw_companion_required')
    no_alias(executable)
    return {'executable_name': executable.name,
            'companion_pythonw_confirmed': True, 'python_version': sys.version.split()[0]}


def load_observer(path, expected_sha):
    path = local_absolute(path)
    raw = safe_read(path, 2097152)
    require(hashlib.sha256(raw).hexdigest() == expected_sha, 'observer_script_hash_mismatch')
    spec = importlib.util.spec_from_file_location('attached_observer_helper', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    require(all(callable(getattr(module, name, None)) for name in ('verify', 'sha', 'no_alias')),
            'observer_verify_missing')
    require(hashlib.sha256(safe_read(path, 2097152)).hexdigest() == expected_sha,
            'observer_script_changed_during_import')
    return module


def expected_binding(config):
    return {'pid': config['host_pid'], 'creation_filetime_ticks': config['host_creation_ticks'],
            'backend_source_sha256': config['backend_sha256'], 'source_ids': SOURCES,
            'origin': 'http://127.0.0.1:8770', 'process_handle_rebind_allowed': False}


def prefix_snapshot(config, helper):
    """Verify a committed prefix while its sole writer may append another line."""
    run = local_absolute(config['run_directory'])
    require(run.is_dir(), 'run_directory_missing')
    started_raw = safe_read(run / 'started.json')
    require(hashlib.sha256(started_raw).hexdigest() == config['started_sha256'], 'started_hash_mismatch')
    started = json.loads(started_raw, object_pairs_hook=object_pairs)
    expected_observer = {'pid': config['observer_pid'],
                         'creation_filetime_ticks': config['observer_creation_ticks']}
    actual_observer = started.get('observer_identity')
    require(isinstance(actual_observer, dict) and set(actual_observer) == set(expected_observer)
            and all(type(value) is int for value in actual_observer.values())
            and actual_observer == expected_observer, 'started_observer_identity_mismatch')
    require(started.get('binding') == expected_binding(config), 'started_host_binding_mismatch')
    require(started.get('simulation') is False and
            (started.get('interval_seconds'), started.get('target_seconds'), started.get('maximum_seconds'))
            == (INTERVAL, TARGET, MAXIMUM), 'started_window_invalid')
    helper.no_alias(run / 'observations.jsonl')
    raw = safe_read(run / 'observations.jsonl', 100 * 1024 * 1024)
    committed = raw[:raw.rfind(b'\n') + 1]
    require(bool(committed), 'committed_log_prefix_missing')
    previous, sequence = '0' * 64, 0
    first = None
    starts, terminals, samples = 0, 0, 0
    sample_elapsed = -1
    last_sample_utc = None
    for line in committed.splitlines():
        require(bool(line) and len(line) <= 262144 and sequence < 100000, 'log_bounds_invalid')
        row = json.loads(line, object_pairs_hook=object_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(SupervisorError('non_finite_json')))
        digest = row.pop('record_sha256')
        require(row.get('version') == 'news_continuity_observation_v1' and
                type(row.get('sequence')) is int and row['sequence'] == sequence and
                row.get('previous_sha256') == previous and helper.sha(row) == digest,
                'log_chain_mismatch')
        require(terminals == 0, 'records_after_terminal')
        if first is None:
            first = {**row, 'record_sha256': digest}
        starts += row.get('kind') == 'started'
        terminals += row.get('kind') == 'terminal'
        if row.get('kind') == 'sample':
            elapsed = row['data']['elapsed_seconds']
            require(type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= sample_elapsed,
                    'sample_monotonic_order_invalid')
            sample_elapsed = elapsed
            last_sample_utc = row['data'].get('observed_utc')
            samples += 1
        previous, sequence = digest, sequence + 1
    require(first['kind'] == 'started' and starts == 1 and
            started.get('initial_record_sha256') == first['record_sha256'] and
            {key: value for key, value in started.items() if key != 'initial_record_sha256'} == first['data'],
            'started_record_binding_mismatch')
    result = {'started_sha256': config['started_sha256'], 'records': sequence,
              'prefix_bytes': len(committed), 'prefix_sha256': hashlib.sha256(committed).hexdigest(),
              'final_record_sha256': previous, 'ignored_inflight_tail_bytes': len(raw) - len(committed),
              'sample_count': samples, 'last_sample_elapsed_seconds': sample_elapsed if samples else None,
              'last_sample_utc': last_sample_utc, 'terminal_record_present': terminals == 1}
    return result, committed, started


def supervise(config, config_sha):
    lifecycle = HERE / 'lifecycle' / config['output_name']
    no_alias(lifecycle)
    lifecycle.parent.mkdir(exist_ok=True)
    lifecycle.mkdir(exist_ok=False)
    native = None
    observer_handle = host_handle = None
    guardian_identity = None
    observer_identity = {'pid': config['observer_pid'],
                         'creation_filetime_ticks': str(config['observer_creation_ticks'])}
    host_identity = {'pid': config['host_pid'],
                     'creation_filetime_ticks': str(config['host_creation_ticks'])}
    supervision_gap = None
    attached_utc = None
    attached_prefix = None
    started_monotonic = time.monotonic()
    exit_written = False
    terminal_written = False
    terminal = {'status': 'incomplete', 'normal_completion': False,
                'observed_for_at_least_24h': False, 'continuous_24h_passed': False,
                'reason': 'supervisor_setup_failed'}
    try:
        interpreter = require_pythonw()
        native = NativeProcess()
        guardian_identity = native.own_identity()
        console = native.console_evidence()
        write_new(lifecycle / 'startup.json', {
            'version': 'news_observer_attach_startup_v2', 'observed_utc': utcnow(),
            'guardian_identity': guardian_identity, 'config_sha256': config_sha,
            'interpreter': interpreter, 'console': console, 'observer_identity': observer_identity,
            'host_identity': host_identity, 'observer_sha256': config['observer_sha256'],
            'started_sha256': config['started_sha256'], 'mode': 'attach_only',
            'creates_or_signals_processes': False, 'continuous_supervision': False})
        require(console['no_console_confirmed'], 'console_attachment_refused')
        observer_handle, observer_before = native.open_bound_alive(observer_identity)
        host_handle, host_before = native.open_bound_alive(host_identity)
        helper = load_observer(config['observer_script'], config['observer_sha256'])
        before, before_raw, started = prefix_snapshot(config, helper)
        after, after_raw, _ = prefix_snapshot(config, helper)
        require(len(after_raw) >= len(before_raw) and after_raw[:len(before_raw)] == before_raw,
                'log_prefix_changed_during_attachment')
        attached_prefix = after_raw
        observer_after = collect_exit(observer_handle, observer_identity, native)
        host_after = collect_exit(host_handle, host_identity, native)
        require(all(value.get('identity_match') is True and value.get('state') == 'alive'
                    for value in (observer_after, host_after)), 'process_changed_during_attachment')
        attached_utc = utcnow()
        gap_ticks = utc_ticks(attached_utc) - utc_ticks(config['prior_guardian_exit_utc'])
        require(gap_ticks >= 0, 'attachment_before_prior_exit')
        supervision_gap = {'started_utc': config['prior_guardian_exit_utc'], 'ended_utc': attached_utc,
                           'elapsed_seconds': gap_ticks / 10000000,
                           'meaning': 'independent_supervision_gap_not_observer_downtime'}
        write_new(lifecycle / 'attached.json', {
            'version': 'news_observer_attach_receipt_v2', 'observed_utc': attached_utc,
            'guardian_identity': guardian_identity, 'observer_identity': observer_identity,
            'host_identity': host_identity, 'observer_before': observer_before, 'observer_after': observer_after,
            'host_before': host_before, 'host_after': host_after, 'binding': expected_binding(config),
            'started_sha256': config['started_sha256'], 'observation_started_utc': started['observed_utc'],
            'target_seconds': TARGET, 'interval_seconds': INTERVAL, 'window_start_unchanged': True,
            'log_prefix_before': before, 'log_prefix_after': after, 'native_handles_retained': True,
            'supervision_gap': supervision_gap, 'continuous_supervision': False,
            'observer_created': False, 'observer_restarted': False, 'observer_evidence_modified': False})
        while not native.wait(observer_handle, 1000):
            pass
        exited = collect_exit(observer_handle, observer_identity, native)
        write_new(lifecycle / 'exited.json', {
            'version': 'news_observer_attach_exit_v2', 'observed_utc': utcnow(),
            'guardian_identity': guardian_identity, 'observer_identity': observer_identity,
            'process': exited, 'host_state': collect_exit(host_handle, host_identity, native)})
        exit_written = True
        require(hashlib.sha256(safe_read(config['observer_script'], 2097152)).hexdigest() == config['observer_sha256'],
                'observer_script_changed_before_verification')
        require(hashlib.sha256(safe_read(Path(config['run_directory']) / 'started.json')).hexdigest() == config['started_sha256'],
                'started_hash_changed_before_verification')
        final_log = safe_read(Path(config['run_directory']) / 'observations.jsonl', 100 * 1024 * 1024)
        require(final_log[:len(attached_prefix)] == attached_prefix, 'attached_prefix_changed_before_closeout')
        terminal = closeout(config['run_directory'], observer_identity, exited, helper.verify, expected_binding(config))
        terminal['attached_prefix_preserved'] = True
        require(terminal.get('verification', {}).get('started_sha256') in (None, config['started_sha256']),
                'closeout_started_hash_mismatch')
    except BaseException as error:
        terminal = {'status': 'incomplete', 'normal_completion': False,
                    'observed_for_at_least_24h': False, 'continuous_24h_passed': False,
                    'reason': 'supervisor_exception', 'error_code': safe_error(error)}
        if observer_handle and native is not None:
            try:
                current = collect_exit(observer_handle, observer_identity, native)
                terminal['observer_state_at_supervisor_failure'] = current
                if current.get('state') == 'exited' and not exit_written:
                    write_new(lifecycle / 'exited.json', {
                        'version': 'news_observer_attach_exit_v2', 'observed_utc': utcnow(),
                        'guardian_identity': guardian_identity, 'observer_identity': observer_identity,
                        'process': current})
                    exit_written = True
            except BaseException:
                terminal['observer_state_at_supervisor_failure'] = {'state': 'unknown'}
    finally:
        try:
            terminal.update(version='news_observer_attach_terminal_v2', observed_utc=utcnow(),
                            guardian_identity=guardian_identity, observer_identity=observer_identity,
                            host_identity=host_identity, config_sha256=config_sha,
                            started_sha256=config['started_sha256'], attached_utc=attached_utc,
                            supervisor_elapsed_monotonic_seconds=max(0, time.monotonic() - started_monotonic),
                            supervision_gap=supervision_gap, continuous_supervision=False,
                            observer_created=False, observer_restarted=False, observer_terminated=False,
                            observer_evidence_modified=False,
                            limitations=['Supervision has a recorded gap; the original observation window is unchanged.',
                                         'Duration completion is not continuous stability or content acceptance.',
                                         'Native supervisor termination or storage failure can prevent this receipt.'])
            write_new(lifecycle / 'supervisor-terminal.json', terminal)
            terminal_written = True
        except BaseException:
            terminal_written = False
        finally:
            if native is not None:
                native.close(observer_handle)
                native.close(host_handle)
    return 0 if terminal_written and terminal.get('normal_completion') is True else 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config, config_sha = load_config(args.config)
    return supervise(config, config_sha)


if __name__ == '__main__':
    try:
        code = main()
    except BaseException:
        code = 3
    raise SystemExit(code)

