"""Bounded Windows-only synthetic rehearsal of the existing monitor/watchdog.

Runs a real observer timer, separate watchdog processes and a file receiver.
No HTTP, database, model, trial activation, Task Scheduler install or user-channel
notification is performed. The six-minute missed-completion period is real time.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from backend.news_review_monitor import MonitorReceiptWriter, publish_json_once
from scripts.news_review_watchdog import native_creation_ticks, read_receipt_chain

IDENTITY = {'candidate_sha': 'a' * 40, 'activation_sha256': 'b' * 64, 'policy_sha256': 'c' * 64}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def now():
    return time.time_ns() // 1_000_000


def wait_for(predicate, seconds=10):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.1)
    raise TimeoutError('rehearsal_step_timeout')


def observer(root):
    end = time.monotonic() + 600
    writer = MonitorReceiptWriter(root / 'receipts', IDENTITY, wall_ms=now,
                                  monotonic_ms=lambda: time.monotonic_ns() // 1_000_000)
    publish_json_once(root / 'observer-pin.json', {'identity': IDENTITY, 'pid': os.getpid(),
        'process_start_utc_ticks': native_creation_ticks(os.getpid())})

    class Stop:
        def is_set(self):
            return (root / 'stop-observer').exists() or time.monotonic() >= end

        def wait(self, seconds):
            until = time.monotonic() + seconds
            while not self.is_set() and time.monotonic() < until:
                time.sleep(min(.1, max(0, until - time.monotonic())))
            return self.is_set()

    stop = Stop()

    def probe():
        # Synthetic health is intentionally local. This does NOT verify a real
        # application identity, port owner, source or two approved status GETs.
        while (root / 'pause-completion').exists() and not stop.is_set():
            time.sleep(.1)
        if stop.is_set():
            raise TimeoutError('rehearsal_stop')
        return {'identity': IDENTITY, 'process_identity_verified': True,
                'port_ownership_verified': True, 'health': {'host_alive': True,
                'workers_alive': True, 'source_stale_keys': ['company_ir'] if (root / 'source-stale').exists() else []}}

    writer.run_schedule(probe=probe, stop_event=stop, expires_at_ms=now() + 590000, interval_ms=1000)
    publish_json_once(root / 'observer-exit.json', {'pid': os.getpid(), 'ended_at_ms': now(),
        'completed_receipts': writer.sequence, 'synthetic_only': True})


def receiver(root):
    end = time.monotonic() + 600
    acknowledged = set()
    publish_json_once(root / 'receiver-pin.json', {'pid': os.getpid(),
        'process_start_utc_ticks': native_creation_ticks(os.getpid())})
    while not (root / 'stop-receiver').exists() and time.monotonic() < end:
        for path in sorted((root / 'notifications').glob('*.json')):
            if path.name in acknowledged:
                continue
            value = read(path)
            if value['identity'] != IDENTITY:
                raise ValueError('receiver_identity_mismatch')
            publish_json_once(root / 'acknowledgements' / path.name, {
                'notification_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'received_at_ms': now(), 'receiver_pid': os.getpid(),
                'opened': value['changes']['opened'], 'resolved': value['changes']['resolved'],
                'test_receiver_only': True, 'human_receipt_confirmed': False})
            acknowledged.add(path.name)
        time.sleep(.1)
    publish_json_once(root / 'receiver-exit.json', {'pid': os.getpid(), 'ended_at_ms': now(),
        'acknowledgements': len(acknowledged)})


def rehearse(root):
    root.mkdir(parents=True, exist_ok=False)
    for name in ('receipts', 'watchdog', 'notifications', 'acknowledgements'):
        (root / name).mkdir()
    checks = []
    previous = None
    children = []
    started = now()
    flags = subprocess.CREATE_NO_WINDOW

    def checkpoint(phase):
        nonlocal previous
        path = root / 'watchdog' / f'{len(checks)+1:04d}.json'
        command = [sys.executable, '-B', str(APP / 'scripts/news_review_watchdog.py'),
                   '--pin', str(root / 'observer-pin.json'), '--receipts', str(root / 'receipts'),
                   '--output', str(path)]
        if previous:
            command += ['--previous', str(previous)]
        result = subprocess.run(command, capture_output=True, timeout=15, creationflags=flags)
        if result.returncode not in (0, 2):
            raise RuntimeError('watchdog_cli_failed')
        value = read(path)
        if value['changes']['notify'] != (result.returncode == 2):
            raise ValueError('notification_exit_mismatch')
        if value['changes']['notify']:
            notification = root / 'notifications' / path.name
            publish_json_once(notification, {'identity': IDENTITY, 'phase': phase,
                'changes': value['changes'], 'watchdog_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
            acknowledgement = root / 'acknowledgements' / path.name
            wait_for(acknowledgement.exists)
            if read(acknowledgement)['notification_sha256'] != hashlib.sha256(notification.read_bytes()).hexdigest():
                raise ValueError('receiver_ack_mismatch')
        checks.append({'phase': phase, 'at_ms': now(), 'watchdog_receipt': path.name,
                       'alerts': value['alerts'], 'changes': value['changes']})
        previous = path
        publish_json_once(root / f'progress-{len(checks):04d}.json', checks[-1])
        return value

    try:
        for role in ('observer', 'receiver'):
            children.append(subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                '--role', role, '--output-dir', str(root)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags))
        wait_for(lambda: (root / 'observer-pin.json').exists() and (root / 'receiver-pin.json').exists())
        wait_for(lambda: (root / 'receipts/monitor-execution-000002.json').exists())
        assert not checkpoint('healthy')['changes']['notify']
        (root / 'source-stale').touch(exist_ok=False)
        wait_for(lambda: bool(read_receipt_chain(root / 'receipts', IDENTITY)['health']['source_stale_keys']))
        assert checkpoint('source_stale')['changes']['opened'] == ['SOURCE_FULL_SUCCESS_STALE']
        assert not checkpoint('same_source_incident')['changes']['notify']
        (root / 'source-stale').unlink()
        wait_for(lambda: not read_receipt_chain(root / 'receipts', IDENTITY)['health']['source_stale_keys'])
        assert checkpoint('source_recovered')['changes']['resolved'] == ['SOURCE_FULL_SUCCESS_STALE']
        (root / 'pause-completion').touch(exist_ok=False)
        wait_for(lambda: len(list((root / 'receipts').glob('monitor-start-*.json'))) >
                        len(list((root / 'receipts').glob('monitor-execution-*.json'))))
        previous_completed = read_receipt_chain(root / 'receipts', IDENTITY)['completed_at_ms']
        silence_start = time.monotonic()
        assert not checkpoint('pending_publication_fresh_previous')['changes']['notify']
        while now() - previous_completed <= 360500 or time.monotonic() - silence_start <= 361:
            time.sleep(30)
            checkpoint('intentional_missed_completion')
        overdue = checkpoint('missed_completion_confirmed')
        assert 'MONITOR_EXECUTION_OVERDUE' in overdue['alerts']
        assert 'OBSERVER_LIVENESS_UNCONFIRMED' not in overdue['alerts']
        assert 'MONITOR_RECEIPT_IDENTITY_UNCONFIRMED' not in overdue['alerts']
        assert not checkpoint('same_missed_completion')['changes']['notify']
        (root / 'pause-completion').unlink()
        wait_for(lambda: read_receipt_chain(root / 'receipts', IDENTITY)['completed_at_ms'] > previous_completed)
        recovered = checkpoint('completion_recovered')
        assert not recovered['alerts'] and recovered['changes']['notify']
        assert not checkpoint('healthy_again')['changes']['notify']
        result = {'version': 'news_review_windows_rehearsal_v1', 'started_at_ms': started,
            'completed_at_ms': now(), 'passed': True, 'checks': checks,
            'real_silence_ms': int((time.monotonic()-silence_start)*1000),
            'notification_count': len(list((root / 'notifications').glob('*.json'))),
            'acknowledgement_count': len(list((root / 'acknowledgements').glob('*.json'))),
            'synthetic_probe_only': True, 'file_receiver_verified': True,
            'human_notification_channel_verified': False, 'production_probe_deployed': False,
            'windows_task_scheduler_installed': False, 'network_requests': 0, 'database_connections': 0,
            'live_trial_started': False, 'system_configuration_changed': False}
    finally:
        (root / 'stop-observer').touch(exist_ok=True)
        (root / 'stop-receiver').touch(exist_ok=True)
        (root / 'pause-completion').unlink(missing_ok=True)
        for child in children:
            child.wait(timeout=20)
    result['child_exit_codes'] = [child.returncode for child in children]
    assert result['child_exit_codes'] == [0, 0]
    assert result['notification_count'] == result['acknowledgement_count'] == 4
    publish_json_once(root / 'rehearsal-report.json', result)
    print(json.dumps({'output': str(root), 'passed': True, 'notification_count': 4,
                      'human_notification_channel_verified': False}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--role', choices=('observer', 'receiver'))
    args = parser.parse_args()
    if os.name != 'nt' or not Path(args.output_dir).is_absolute():
        raise ValueError('native_windows_and_absolute_output_required')
    if args.role:
        {'observer': observer, 'receiver': receiver}[args.role](Path(args.output_dir))
    else:
        rehearse(Path(args.output_dir))
