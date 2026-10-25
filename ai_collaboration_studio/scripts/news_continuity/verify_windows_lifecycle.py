"""Two bounded Windows lifecycle checks using only disposable offline fixtures.

No HTTP, database, provider, or product process is accessed. The console event
is sent only after a private test console contains exactly the test guardian
and its emitter. Every fixture process has a natural time limit; none is killed.
Run explicitly with --output-root pointing to a new offline_fixture directory.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()
PYTHON = Path(sys.executable).with_name('python.exe')
POWERSHELL = Path(r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe')
INTERRUPTED = 0xC000013A


class FixtureError(RuntimeError):
    def __init__(self, label):
        self.label = label
        self.winerror = ctypes.get_last_error()
        super().__init__(label)


def require(value, label):
    if not value:
        raise FixtureError(label)


def safe_failure(error, stage):
    return {'failure_type': type(error).__name__, 'stage': stage,
            'failure_label': error.label if isinstance(error, FixtureError) else 'fixture_submode_failed',
            'winerror': error.winerror if isinstance(error, FixtureError) else ctypes.get_last_error()}


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def publish(path, value):
    with Path(path).open('xb') as stream:
        stream.write(json.dumps(value, sort_keys=True, ensure_ascii=True).encode('ascii') + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def await_file(path, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).is_file():
            try:
                return read(path)
            except (ValueError, OSError):
                pass
        time.sleep(.05)
    raise RuntimeError('fixture_receipt_timeout:' + Path(path).name)


class Native:
    def __init__(self):
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k.GetCurrentProcess.restype = wintypes.HANDLE
        self.k.GetProcessId.argtypes = [wintypes.HANDLE]
        self.k.GetProcessId.restype = wintypes.DWORD
        self.k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        self.k.GetProcessTimes.restype = wintypes.BOOL
        self.k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k.OpenProcess.restype = wintypes.HANDLE
        self.k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.k.WaitForSingleObject.restype = wintypes.DWORD
        self.k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.k.GetExitCodeProcess.restype = wintypes.BOOL
        self.k.AttachConsole.argtypes = [wintypes.DWORD]
        self.k.AttachConsole.restype = wintypes.BOOL
        self.k.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        self.k.GetConsoleProcessList.restype = wintypes.DWORD
        self.k.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.k.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
        self.k.FreeConsole.argtypes = []
        self.k.FreeConsole.restype = wintypes.BOOL

    def snapshot(self, handle):
        values = [wintypes.FILETIME() for _ in range(4)]
        require(self.k.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)), 'fixture_process_times')
        result = {'pid': int(self.k.GetProcessId(handle)),
                  'creation_filetime_ticks': str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)}
        wait = self.k.WaitForSingleObject(handle, 0)
        require(wait in (0, 258), 'fixture_process_wait')
        result['state'] = 'exited' if wait == 0 else 'alive'
        if wait == 0:
            code = wintypes.DWORD()
            require(self.k.GetExitCodeProcess(handle, ctypes.byref(code)), 'fixture_exit_code')
            result['exit_code'] = int(code.value)
        return result

    def bound(self, identity):
        handle = self.k.OpenProcess(0x100000 | 0x1000, False, int(identity['pid']))
        require(handle, 'fixture_open_process')
        observed = self.snapshot(handle)
        if (observed['pid'], observed['creation_filetime_ticks']) != (
                identity['pid'], str(identity['creation_filetime_ticks'])):
            self.k.CloseHandle(handle)
            raise RuntimeError('fixture_creation_identity_mismatch')
        return handle

    def console(self):
        members = (wintypes.DWORD * 16)()
        count = int(self.k.GetConsoleProcessList(members, 16))
        require(count <= 16, 'unexpected_console_members')
        return sorted(int(members[i]) for i in range(count))


def spawn(mode, folder, extra=(), console=False):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0  # SW_HIDE, including the private CREATE_NEW_CONSOLE.
    return subprocess.Popen([str(PYTHON), '-I', '-S', '-B', '-X', 'utf8', str(SELF),
                             '--mode', mode, '--output-root', str(folder), *map(str, extra)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, startupinfo=startup,
                            creationflags=subprocess.CREATE_NEW_CONSOLE if console else subprocess.CREATE_NO_WINDOW)


def legacy_guardian(folder):
    native = Native()
    child = spawn('sleep_child', folder)
    publish(folder / 'legacy-ready.json', {'guardian': native.snapshot(native.k.GetCurrentProcess()),
                                         'child': native.snapshot(child._handle),
                                         'console_members': native.console()})
    try:
        native.k.WaitForSingleObject(child._handle, 20000)
    finally:
        publish(folder / 'legacy-finally.json', {'finally_ran': True})


def emitter(folder):
    native = Native()
    stage = 'inspect_initial_console'
    handle = None
    attached = False
    try:
        initial_members = native.console()
        publish(folder / 'emitter-console-before.json', {'members': initial_members,
                'emitter_pid': os.getpid(), 'creation_flags': 'CREATE_NO_WINDOW'})
        require(initial_members in ([], [os.getpid()]), 'emitter_initial_console_contains_other_process')
        if initial_members == [os.getpid()]:
            stage = 'release_emitter_private_console'
            require(native.k.FreeConsole(), 'emitter_private_console_release_failed')
            require(native.console() == [], 'emitter_private_console_still_attached')
            publish(folder / 'emitter-console-released.json', {'released_own_private_console': True,
                    'previous_members': initial_members, 'remaining_members': []})
        stage = 'open_bound_guardian'
        expected = read(folder / 'legacy-ready.json')['guardian']
        handle = native.bound(expected)
        require(native.snapshot(handle)['state'] == 'alive', 'fixture_guardian_not_alive')
        stage = 'attach_private_console'
        require(native.k.AttachConsole(expected['pid']), 'private_console_attach_failed')
        attached = True
        stage = 'install_emitter_handler'
        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        handler = handler_type(lambda event: True)
        native.k.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
        native.k.SetConsoleCtrlHandler.restype = wintypes.BOOL
        require(native.k.SetConsoleCtrlHandler(handler, True), 'emitter_handler_failed')
        stage = 'verify_private_console_members'
        members = native.console()
        require(members == sorted([expected['pid'], os.getpid()]), 'refusing_nonprivate_console_event')
        require(native.snapshot(handle)['state'] == 'alive', 'fixture_guardian_exited_before_event')
        publish(folder / 'console-event-scope.json', {'event': 'CTRL_BREAK_EVENT',
                'members': members, 'expected_guardian': expected, 'emitter_pid': os.getpid(),
                'only_known_fixture_members': True, 'user_console_attached': False})
        stage = 'emit_private_console_break'
        require(native.k.GenerateConsoleCtrlEvent(1, 0), 'private_console_event_failed')
        time.sleep(.3)
    except BaseException as error:
        publish(folder / 'emitter-failure.json', safe_failure(error, stage))
        raise
    finally:
        if attached:
            native.k.FreeConsole()
        if handle:
            native.k.CloseHandle(handle)


def fake_observer(folder):
    """Schema-shaped offline input; never a real sample or 24-hour observation."""
    config = read(folder / 'fixture-host.json')
    native = Native()
    own = native.snapshot(native.k.GetCurrentProcess())
    spec = importlib.util.spec_from_file_location('fixture_observer_helper', folder / 'observer.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    log = helper.ReceiptLog(folder / 'offline_fixture-run')
    initial = {'observed_utc': now(), 'started_monotonic_seconds': time.monotonic(),
               'observer_identity': {'pid': own['pid'], 'creation_filetime_ticks': int(own['creation_filetime_ticks'])},
               'binding': config['binding'], 'interval_seconds': 150, 'target_seconds': 86400,
               'maximum_seconds': 86700, 'simulation': False}
    # The exact schema intentionally exercises the normal attach guards. The
    # isolated directory and fixture manifest identify this as offline evidence;
    # no sample or terminal is produced and completion must remain false.
    log.append('started', initial)
    helper_raw = helper.canonical({**initial, 'initial_record_sha256': log.previous}) + b'\n'
    with (log.output / 'started.json').open('xb') as stream:
        stream.write(helper_raw)
        stream.flush()
        os.fsync(stream.fileno())
    publish(folder / 'fixture-observer-ready.json', own)
    time.sleep(25)
    os._exit(0)


def powershell(script, arguments, timeout=30):
    # A wrapper catches errors inside the invoked script and writes only safe
    # identifiers. Never inspect stderr or persist exception messages/arguments.
    token = 'invoke-' + script.stem + '-' + uuid.uuid4().hex[:8]
    wrapper = script.parent / (token + '.ps1')
    argument_file = script.parent / (token + '-arguments.json')
    failure_file = script.parent / (token + '-failure.json')
    require(len(arguments) % 2 == 0 and all(str(arguments[i]).startswith('-')
            for i in range(0, len(arguments), 2)), 'fixture_powershell_named_arguments_required')
    publish(argument_file, {str(arguments[i])[1:]: str(arguments[i + 1])
                           for i in range(0, len(arguments), 2)})
    with wrapper.open('x', encoding='utf-8') as stream:
        stream.write('''param([string]$TargetScript,[string]$ArgumentFile,[string]$FailureFile)
$ErrorActionPreference='Stop'
$stage='read_fixture_arguments'
try {
    $values=Get-Content -LiteralPath $ArgumentFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $named=@{}
    foreach($property in $values.PSObject.Properties){$named[$property.Name]=[string]$property.Value}
    $stage='invoke_fixture_script'
    & $TargetScript @named
} catch {
    $errorId=($_.FullyQualifiedErrorId -split ',')[0]
    if($errorId -notmatch '^[A-Za-z0-9_.-]{1,120}$'){$errorId='unclassified_powershell_error'}
    $receipt=@{stage=$stage;error_id=$errorId;exception_type=$_.Exception.GetType().FullName;
        hresult=$_.Exception.HResult;line=$_.InvocationInfo.ScriptLineNumber}
    $bytes=[Text.Encoding]::UTF8.GetBytes(($receipt | ConvertTo-Json -Compress))
    $s=[IO.File]::Open($FailureFile,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::Read)
    try{$s.Write($bytes,0,$bytes.Length);$s.Flush($true)}finally{$s.Dispose()}
    exit 3
}
''')
    result = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                             '-File', str(wrapper), '-TargetScript', str(script),
                             '-ArgumentFile', str(argument_file), '-FailureFile', str(failure_file)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=timeout)
    publish(script.parent / (token + '-result.json'), {'returncode': result.returncode,
            'stage': 'fixture_script_returned', 'safe_failure_receipt': failure_file.name if failure_file.exists() else None})
    require(result.returncode == 0, 'fixture_powershell_failed')
    return result.stdout


def cleanup_task(folder, name):
    script = folder / 'cleanup-fixture-task.ps1'
    script.write_text('''param([string]$TaskName,[string]$PythonPath,[string]$ScriptPath,[string]$ConfigPath)
$ErrorActionPreference='Stop'
$s=New-Object -ComObject Schedule.Service
$s.Connect()
$f=$s.GetFolder('\\')
$t=$f.GetTask($TaskName)
$expected='-I -S -B -X utf8 "'+$ScriptPath+'" --config "'+$ConfigPath+'"'
if($t.Name -ne $TaskName -or $t.Definition.Actions.Count -ne 1){throw 'Task identity mismatch'}
$a=$t.Definition.Actions.Item(1)
if($a.Path -ne $PythonPath -or $a.Arguments -ne $expected){throw 'Task action mismatch'}
$deadline=[DateTime]::UtcNow.AddSeconds(10)
while($t.State -eq 4 -and [DateTime]::UtcNow -lt $deadline){Start-Sleep -Milliseconds 200; $t=$f.GetTask($TaskName)}
if($t.State -eq 4){throw 'Task still running; retained without stopping'}
$state=$t.State
$f.DeleteTask($TaskName,0)
@{deleted=$true;name=$TaskName;state_before_delete=$state;action_matched=$true}|ConvertTo-Json
''', encoding='utf-8')
    raw = powershell(script, ['-TaskName', name, '-PythonPath', PYTHON.with_name('pythonw.exe'),
                     '-ScriptPath', folder / 'guardian.py', '-ConfigPath', folder / 'fixture-config.json'])
    result = json.loads(raw.decode('utf-8-sig'))
    publish(folder / 'task-cleanup.json', result)
    return result


def check_legacy(folder):
    folder.mkdir()
    native = Native()
    guardian = spawn('legacy_guardian', folder, console=True)
    child_handle = None
    try:
        ready = await_file(folder / 'legacy-ready.json')
        require(ready['console_members'] == [guardian.pid], 'legacy_console_not_private')
        require(ready['guardian']['pid'] == guardian.pid, 'legacy_guardian_identity')
        child_handle = native.bound(ready['child'])
        emit = spawn('emitter', folder)
        require(emit.wait(timeout=8) == 0, 'fixture_event_emitter_failed')
        code = guardian.wait(timeout=5) & 0xffffffff
        survived = native.snapshot(child_handle)
        require(code == INTERRUPTED, 'legacy_exit_did_not_reproduce_ctrl_break')
        require(not (folder / 'legacy-finally.json').exists(), 'legacy_finally_unexpectedly_ran')
        require(survived['state'] == 'alive', 'console_free_child_did_not_survive')
        require(native.k.WaitForSingleObject(child_handle, 20000) == 0, 'fixture_child_did_not_self_expire')
        return {'scenario': 'private_console_break_reproduces_unguarded_exit', 'passed': True,
                'guardian_exit_code': code, 'guardian_exit_hex': hex(code), 'finally_receipt_present': False,
                'console_free_child_survived_event': True, 'child_natural_exit': native.snapshot(child_handle),
                'event_scope_receipt': str(folder / 'console-event-scope.json')}
    finally:
        if child_handle:
            native.k.CloseHandle(child_handle)
        # No termination fallback: both owned fixtures are bounded naturally.
        try:
            guardian.wait(timeout=22)
        except subprocess.TimeoutExpired:
            pass


def check_scheduled(folder):
    folder.mkdir()
    hashes = {}
    for name in ('guardian.py', 'observer.py', 'register_task.ps1'):
        shutil.copyfile(HERE / name, folder / name)
        hashes[name] = digest(folder / name)
        require(hashes[name] == digest(HERE / name), 'fixture_copy_hash_mismatch')
    native = Native()
    host = native.snapshot(native.k.GetCurrentProcess())
    binding = {'pid': host['pid'], 'creation_filetime_ticks': int(host['creation_filetime_ticks']),
               'backend_source_sha256': 'a' * 64, 'source_ids': ['fed', 'bls', 'seagate'],
               'origin': 'http://127.0.0.1:8770', 'process_handle_rebind_allowed': False}
    publish(folder / 'fixture-host.json', {'binding': binding, 'offline_fixture': True,
                'no_http_requests': True, 'host_is_test_runner': True, 'fake_observer_lifetime_seconds': 25})
    child = spawn('fake_observer', folder)
    name = 'NewsObserver-offline-fixture-' + uuid.uuid4().hex[:12]
    registration_started = False
    cleanup = None
    try:
        expected = await_file(folder / 'fixture-observer-ready.json')
        run = folder / 'offline_fixture-run'
        started_hash, prefix_hash = digest(run / 'started.json'), digest(run / 'observations.jsonl')
        config = {'output_name': 'offline_fixture-attachment', 'run_directory': str(run),
                  'observer_script': str(folder / 'observer.py'), 'observer_sha256': hashes['observer.py'],
                  'started_sha256': started_hash, 'observer_pid': expected['pid'],
                  'observer_creation_ticks': int(expected['creation_filetime_ticks']),
                  'host_pid': host['pid'], 'host_creation_ticks': int(host['creation_filetime_ticks']),
                  'backend_sha256': 'a' * 64, 'prior_guardian_exit_utc': now()}
        publish(folder / 'fixture-config.json', config)
        registration_started = True
        powershell(folder / 'register_task.ps1', ['-TaskName', name, '-ScriptPath', folder / 'guardian.py',
                   '-ScriptSha256', hashes['guardian.py'], '-ConfigPath', folder / 'fixture-config.json',
                   '-ConfigSha256', digest(folder / 'fixture-config.json'),
                   '-ReceiptDirectory', folder / 'task-registration', '-PythonPath', PYTHON])
        lifecycle = folder / 'lifecycle' / config['output_name']
        startup = await_file(lifecycle / 'startup.json')
        attached = await_file(lifecycle / 'attached.json')
        require(startup['console']['no_console_confirmed'] is True, 'scheduled_guardian_owns_console')
        require(attached['observer_identity'] == {'pid': child.pid,
                'creation_filetime_ticks': expected['creation_filetime_ticks']}, 'attached_observer_identity_mismatch')
        require(attached['observer_created'] is False and attached['observer_restarted'] is False,
                'guardian_did_not_attach_only')
        require(digest(run / 'started.json') == started_hash and digest(run / 'observations.jsonl') == prefix_hash,
                'fixture_evidence_changed_during_attach')
        require(child.wait(timeout=30) == 0, 'fake_observer_natural_exit_nonzero')
        exited = await_file(lifecycle / 'exited.json')
        terminal = await_file(lifecycle / 'supervisor-terminal.json')
        require(exited['process']['identity_match'] is True and type(exited['process']['exit_code']) is int
                and exited['process']['exit_code'] == 0 and int(exited['process']['exit_filetime_ticks']) > 0,
                'native_exit_evidence_missing')
        require(terminal['normal_completion'] is False and terminal['reason'] == 'observer_terminal_missing',
                'missing_terminal_classified_as_completion')
        require(terminal['observer_terminated'] is False and terminal['observer_evidence_modified'] is False,
                'guardian_mutation_flag')
        require(not (run / 'terminal.json').exists() and digest(run / 'started.json') == started_hash
                and digest(run / 'observations.jsonl') == prefix_hash, 'fixture_evidence_changed_at_closeout')
        cleanup = cleanup_task(folder, name)
        return {'scenario': 'scheduled_pythonw_attaches_and_records_missing_terminal', 'passed': True,
                'source_hashes': hashes, 'guardian_no_console': startup['console'],
                'observer_identity': attached['observer_identity'], 'native_exit': exited['process'],
                'closeout_reason': terminal['reason'], 'normal_completion': False,
                'started_and_prefix_preserved': True, 'observer_created_or_signaled_by_guardian': False,
                'task_xml': str(folder / 'task-registration' / 'registered-task.xml'), 'task_cleanup': cleanup}
    finally:
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        if registration_started and cleanup is None:
            try:
                cleanup_task(folder, name)
            except Exception:
                # Preserve a running/unconfirmed task; never stop it to tidy up.
                publish(folder / 'cleanup-not-completed.json', {'task_name': name,
                        'task_retained': True, 'reason': 'completion_or_action_not_confirmed'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-root', required=True)
    parser.add_argument('--mode', choices=['verify', 'legacy_guardian', 'sleep_child', 'emitter', 'fake_observer'], default='verify')
    parser.add_argument('--scenario', choices=['legacy', 'scheduled', 'both'], default='both')
    args = parser.parse_args()
    require(os.name == 'nt', 'windows_required')
    folder = Path(args.output_root).resolve()
    require('offline_fixture' in folder.name or any('offline_fixture' in p for p in folder.parts), 'offline_fixture_path_required')
    if args.mode != 'verify':
        try:
            if args.mode == 'sleep_child':
                time.sleep(15)
            else:
                {'legacy_guardian': legacy_guardian, 'emitter': emitter,
                 'fake_observer': fake_observer}[args.mode](folder)
            return 0
        except BaseException as error:
            path = folder / (args.mode + '-submode-failure.json')
            if not path.exists():
                publish(path, safe_failure(error, args.mode))
            return 2
    folder.mkdir(parents=True, exist_ok=False)
    report = {'version': 'windows_observer_lifecycle_two_scenarios_v1', 'offline_fixture': True,
              'started_utc': now(), 'script_sha256': digest(SELF), 'scenarios': [], 'passed': False,
              'requested_scenario': args.scenario,
              'http_calls': 0, 'paid_calls': 0, 'CI_run': False, 'product_processes_accessed': False}
    try:
        if args.scenario in ('legacy', 'both'):
            report['scenarios'].append(check_legacy(folder / 'legacy-console'))
        if args.scenario in ('scheduled', 'both'):
            report['scenarios'].append(check_scheduled(folder / 'scheduled-windowless'))
        report['passed'] = True
    except Exception as error:
        report['failure_type'] = type(error).__name__
        report['failure_code'] = error.label if isinstance(error, FixtureError) else 'fixture_execution_failed'
    finally:
        report['finished_utc'] = now()
        publish(folder / 'result.json', report)
    print(json.dumps({'passed': report['passed'], 'scenarios_completed': len(report['scenarios']),
                      'receipt': str(folder / 'result.json')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
