"""Offline, temporary-file checks of the actual notification commit functions.

No Windows observer lifecycle, HTTP request or notification sender is invoked.
"""
import copy
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

TOOLS=Path(__file__).resolve().parents[1]

def load(name):
    spec=importlib.util.spec_from_file_location('notification_test_'+name,TOOLS/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

checker=load('check_readonly')
guardian=load('guardian')


class CompletionNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.out=Path(self.temp.name)
        self.binding={'started_sha256':'a'*64,
                      'identities':{'observer':{'pid':123,'creation_filetime_ticks':'134370000000000001'},
                                    'guardian':{'pid':456,'creation_filetime_ticks':'134370000000000002'}},
                      'notification_baseline':{'active_alerts':[]}}
        self.terminal={'log_records':3,'final_record_sha256':'b'*64,
                       'terminal':{'observed_utc':'2026-10-24T02:00:00Z','reason':'observation_duration_reached'}}
        self.serial=0
        self.serial_lock=threading.Lock()
        self.guards=[patch.object(socket.socket,name,side_effect=AssertionError('network_forbidden'))
                     for name in ('connect','connect_ex')]
        self.guards.append(patch.object(socket,'getaddrinfo',side_effect=AssertionError('network_forbidden')))
        for guard in self.guards:guard.start()

    def tearDown(self):
        for guard in reversed(self.guards):guard.stop()
        self.temp.cleanup()

    def publish(self,*,binding=None,terminal=None,complete=True,other=False,out=None):
        out=out or self.out
        binding=binding or self.binding
        terminal=terminal or self.terminal
        with self.serial_lock:
            self.serial+=1;serial=self.serial
        tag='20261024T030000'+f'{serial:06d}'+'Z'
        with checker.notification_state_lock(out,guardian,timeout=3):
            path=out/'notification-state.json'
            saved=json.loads(path.read_text()) if path.exists() else {}
            run_id=checker.notification_run_identity(binding)
            prior=checker.notification_prior(saved,binding,run_id)
            completed=checker.completion_identity(run_id,terminal) if complete else None
            receipt={'normal_completion':complete,'terminal':terminal if complete else None,
                     'checked_utc':'2026-10-24T03:00:00Z'}
            updated={**prior,'last_receipt':tag+'-receipt.json','last_log_sequence':2,
                     'last_log_record_sha256':'b'*64}
            return checker.persist_heartbeat(out,guardian,tag,receipt,updated,run_id,completed,other)

    def state(self,out=None):
        return json.loads(((out or self.out)/'notification-state.json').read_text())

    def test_repeated_completion_is_once_for_missing_false_and_unbound_legacy_true(self):
        for index,legacy in enumerate(({}, {'completion_reported':False}, {'completion_reported':True})):
            with self.subTest(legacy=legacy):
                out=self.out/str(index);out.mkdir()
                (out/'notification-state.json').write_text(json.dumps(legacy))
                results=[self.publish(out=out) for _ in range(3)]
                self.assertEqual([r['notification_required'] for r in results],[True,False,False])
                self.assertEqual(len(list(out.glob('completion-*-generated.json'))),1)
                generated=self.state(out)['completion_notification']
                self.assertEqual(generated['generation_status'],'generated')
                self.assertEqual(generated['delivery_status'],'unknown')
                self.assertNotEqual(generated['receipt'],self.state(out)['last_receipt'])
                self.assertEqual(checker.digest(out/generated['receipt']),generated['receipt_sha256'])
                self.assertTrue(json.loads((out/generated['receipt']).read_text())['completion_notification_required'])
                self.assertNotIn('completion_reported',self.state(out))
                self.assertTrue(all(r['completion_delivery_status']=='unknown' for r in results))

    def test_new_run_and_reused_pid_get_new_identity_without_old_log_anchor(self):
        self.assertTrue(self.publish()['notification_required'])
        previous=self.state()
        for field,value in [('started_sha256','c'*64),('creation_filetime_ticks','134370000000099999')]:
            binding=copy.deepcopy(self.binding)
            if field=='started_sha256':binding[field]=value
            else:binding['identities']['observer'][field]=value
            new_id=checker.notification_run_identity(binding)
            prior=checker.notification_prior(previous,binding,new_id)
            self.assertNotIn('completion_notification',prior)
            self.assertNotIn('last_log_sequence',prior)
            self.assertTrue(self.publish(binding=binding)['notification_required'])
        self.assertEqual(len(list(self.out.glob('completion-*-generated.json'))),3)

    def test_guardian_replacement_does_not_change_original_run_completion(self):
        self.publish()
        binding=copy.deepcopy(self.binding)
        binding['identities']['guardian']={'pid':789,'creation_filetime_ticks':'134370999999999999'}
        self.assertFalse(self.publish(binding=binding)['notification_required'])
        self.assertEqual(len(list(self.out.glob('completion-*-generated.json'))),1)

    def test_incomplete_result_never_generates_completion_and_other_alerts_remain_visible(self):
        pending=self.publish(complete=False)
        self.assertFalse(pending['notification_required'])
        self.assertFalse(list(self.out.glob('completion-*-generated.json')))
        self.assertTrue(self.publish()['notification_required'])
        repeated=self.publish(other=True)
        self.assertTrue(repeated['notification_required'])
        self.assertFalse(repeated['completion_notification_required'])

    def test_receipt_write_failure_cannot_mark_completion_generated(self):
        initial=b'{"completion_reported":false}'
        state=self.out/'notification-state.json';state.write_bytes(initial)
        write=guardian.write_new
        def fail(path,value):
            if path.name.endswith('-receipt.json'):
                path.write_bytes(b'{"partial":')
                raise OSError('injected_receipt_write_failure')
            write(path,value)
        with patch.object(guardian,'write_new',side_effect=fail),self.assertRaises(OSError):self.publish()
        self.assertEqual(state.read_bytes(),initial)
        self.assertFalse(list(self.out.glob('completion-*-generated.json')))
        self.assertTrue(self.publish()['notification_required'])

    def test_generation_write_failure_does_not_mark_or_claim_delivery(self):
        write=guardian.write_new
        def fail(path,value):
            if path.name.endswith('-completion.tmp'):
                path.write_text(json.dumps(value))
                raise OSError('injected_generation_write_failure')
            write(path,value)
        with patch.object(guardian,'write_new',side_effect=fail),self.assertRaises(OSError):self.publish()
        self.assertFalse((self.out/'notification-state.json').exists())
        self.assertFalse(list(self.out.glob('completion-*-generated.json')))
        self.assertTrue(list(self.out.glob('*-completion.tmp')))
        self.assertTrue(self.publish()['notification_required'])

    def test_generation_fsync_and_rename_failures_never_publish_a_credential(self):
        for stage in ('fsync','rename'):
            with self.subTest(stage=stage):
                out=self.out/stage;out.mkdir()
                # Establish the lock byte before injecting fsync failure.
                with checker.notification_state_lock(out,guardian):pass
                calls=[];fsync=guardian.os.fsync;rename=checker.os.rename
                def fail_fsync(fd):
                    calls.append(fd)
                    if stage=='fsync' and len(calls)==2:raise OSError('injected_generation_fsync_failure')
                    fsync(fd)
                def fail_rename(source,target):
                    if stage=='rename':raise OSError('injected_generation_rename_failure')
                    rename(source,target)
                with patch.object(guardian.os,'fsync',side_effect=fail_fsync),patch.object(checker.os,'rename',side_effect=fail_rename),self.assertRaises(OSError):
                    self.publish(out=out)
                self.assertFalse((out/'notification-state.json').exists())
                self.assertFalse(list(out.glob('completion-*-generated.json')))
                self.assertTrue(list(out.glob('*-completion.tmp')))
                self.assertTrue(self.publish(out=out)['notification_required'])

    def test_each_state_write_failure_retains_generation_without_regeneration(self):
        for stage in ('snapshot','temporary','replace'):
            with self.subTest(stage=stage):
                out=self.out/stage;out.mkdir()
                initial=b'{"completion_reported":false}'
                state=out/'notification-state.json';state.write_bytes(initial)
                write=guardian.write_new;replace=checker.os.replace
                def fail_write(path,value):
                    if stage=='snapshot' and path.name.endswith('-notification-state.json'):raise OSError('injected_state_failure')
                    if stage=='temporary' and path.name.endswith('-state.tmp'):raise OSError('injected_temp_failure')
                    write(path,value)
                def fail_replace(source,dest):
                    if stage=='replace':raise OSError('injected_replace_failure')
                    replace(source,dest)
                with patch.object(guardian,'write_new',side_effect=fail_write),patch.object(checker.os,'replace',side_effect=fail_replace),self.assertRaises(OSError):
                    self.publish(out=out)
                self.assertEqual(state.read_bytes(),initial)
                generated=json.loads(next(out.glob('completion-*-generated.json')).read_text())
                self.assertEqual(generated['delivery_status'],'unknown')
                self.assertEqual(checker.digest(out/generated['receipt']),generated['receipt_sha256'])
                self.assertFalse(self.publish(out=out)['notification_required'])
                self.assertEqual(len(list(out.glob('completion-*-generated.json'))),1)

    def test_generation_credential_requires_bound_and_unchanged_completion_receipt(self):
        for corruption in ('missing_receipt','modified_receipt','wrong_run','wrong_completion','path_escape','missing_credential'):
            with self.subTest(corruption=corruption):
                out=self.out/corruption;out.mkdir();self.publish(out=out)
                marker=next(out.glob('completion-*-generated.json'))
                generated=json.loads(marker.read_text());receipt=out/generated['receipt']
                if corruption=='missing_receipt':receipt.unlink()
                elif corruption=='modified_receipt':receipt.write_bytes(receipt.read_bytes()+b' ')
                elif corruption=='missing_credential':marker.unlink()
                else:
                    generated[{'wrong_run':'notification_run_identity','wrong_completion':'completion_identity','path_escape':'receipt'}[corruption]]='../../outside.json' if corruption=='path_escape' else '0'*64
                    marker.write_text(json.dumps(generated))
                with self.assertRaises((ValueError,OSError)):self.publish(out=out)

    def test_concurrent_checks_generate_one_completion_identity(self):
        start=threading.Barrier(2)
        def one():
            start.wait(timeout=3)
            return self.publish()['notification_required']
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(one) for _ in range(2)]
            results=[future.result(timeout=5) for future in futures]
        self.assertEqual(sorted(results),[False,True])
        self.assertEqual(len(list(self.out.glob('completion-*-generated.json'))),1)
        self.assertEqual(self.state()['completion_notification']['delivery_status'],'unknown')


if __name__=='__main__':unittest.main()
