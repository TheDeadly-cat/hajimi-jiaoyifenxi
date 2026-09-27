import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.news_review_monitor import MonitorReceiptWriter,evaluate_watchdog,source_freshness,notification_changes,publish_json_once
from scripts.news_review_watchdog import check, read_receipt_chain, native_creation_ticks


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='news-monitor-offline-')
        self.addCleanup(self.temp.cleanup)
        self.identity={'candidate_sha':'a'*40,'activation_sha256':'b'*64,'policy_sha256':'c'*64}
        self.wall=1000000
        self.mono=1000
        self.writer=MonitorReceiptWriter(self.temp.name,self.identity,wall_ms=lambda:self.wall,monotonic_ms=lambda:self.mono)

    def healthy(self):
        self.wall+=50
        self.mono+=50
        return {'identity':self.identity,'process_identity_verified':True,'port_ownership_verified':True,
            'health':{'host_alive':True,'workers_alive':True,'source_stale_keys':[]},'raw_body':'not saved'}

    def test_actual_execution_times_and_safe_payload_are_persisted(self):
        r=self.writer.execute(scheduled_at_ms=999000,probe=self.healthy)
        self.assertEqual((r['scheduled_at_ms'],r['started_at_ms'],r['completed_at_ms']),(999000,1000000,1000050))
        self.assertEqual(r['elapsed_monotonic_ms'],50)
        text=next(Path(self.temp.name).glob('*.json')).read_text()
        self.assertNotIn('not saved',text)
        self.assertFalse(evaluate_watchdog(r,identity=self.identity,now_ms=self.wall,observer_identity_alive=True)['alerts'])

    def test_alive_process_does_not_hide_missed_execution(self):
        r=self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
        result=evaluate_watchdog(r,identity=self.identity,now_ms=self.wall+360001,observer_identity_alive=True)
        self.assertIn('MONITOR_EXECUTION_OVERDUE',result['alerts'])
        self.assertEqual(result['automatic_actions'],[])

    def test_timeout_is_observation_uncertainty_not_model_failure_or_retry(self):
        good=self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
        def timeout():
            raise TimeoutError('sensitive text must not be persisted')
        r=self.writer.execute(scheduled_at_ms=self.wall,probe=timeout)
        self.assertEqual(r['last_successful_read_at_ms'],good['completed_at_ms'])
        self.assertEqual(r['error_code'],'STATUS_READ_TIMEOUT')
        self.assertFalse(r['model_task_outcome_inferred'])
        self.assertEqual(r['http_retries'],0)
        self.assertNotIn('sensitive',json.dumps(r))

    def test_identity_mismatch_and_process_exit_are_reported(self):
        r=self.writer.execute(scheduled_at_ms=self.wall,probe=lambda:{'identity':{}})
        result=evaluate_watchdog(r,identity=self.identity,now_ms=self.wall,observer_identity_alive=False)
        self.assertIn('OBSERVER_LIVENESS_UNCONFIRMED',result['alerts'])
        self.assertIn('MONITOR_READ_UNCONFIRMED',result['alerts'])

    def test_host_and_sources_are_separate_from_observer_liveness(self):
        def bad():
            value=self.healthy()
            value['health']={'host_alive':False,'workers_alive':False,'source_stale_keys':['company_ir']}
            return value
        r=self.writer.execute(scheduled_at_ms=self.wall,probe=bad)
        result=evaluate_watchdog(r,identity=self.identity,now_ms=self.wall,observer_identity_alive=True)
        self.assertEqual(set(result['alerts']),{'HOST_NOT_ALIVE','WORKER_NOT_ALIVE','SOURCE_FULL_SUCCESS_STALE'})

    def test_pending_is_not_full_success_and_does_not_request_early_retry(self):
        result=source_freshness([{'adapter_key':'company_ir','last_success_at_ms':1000,'state':'degraded',
            'next_due_at_ms':3000000,'last_error_code':'MICRON_IR_METADATA_REVALIDATION_PENDING'}],
            now_ms=2000000,maximum_gaps={'company_ir':1800000})
        self.assertEqual(result[0]['freshness'],'stale')
        self.assertTrue(result[0]['backoff_not_overridden'])
        self.assertFalse(result[0]['automatic_retry_requested'])

    def test_future_receipt_and_missing_receipt_are_unconfirmed(self):
        r=self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
        self.assertIn('MONITOR_CLOCK_UNCONFIRMED',evaluate_watchdog(r,identity=self.identity,now_ms=1,observer_identity_alive=True)['alerts'])
        self.assertIn('MONITOR_RECEIPT_IDENTITY_UNCONFIRMED',evaluate_watchdog(None,identity=self.identity,now_ms=self.wall,observer_identity_alive=True)['alerts'])

    def test_late_execution_is_not_hidden_by_fresh_completion(self):
        r=self.writer.execute(scheduled_at_ms=self.wall-360001,probe=self.healthy)
        verdict=evaluate_watchdog(r,identity=self.identity,now_ms=self.wall,observer_identity_alive=True)
        self.assertIn('MONITOR_SCHEDULE_DELAY_EXCEEDED',verdict['alerts'])

    def test_same_incident_is_quiet_and_recovery_notifies_without_actions(self):
        first=notification_changes([] ,{'alerts':['SOURCE_FULL_SUCCESS_STALE']})
        self.assertTrue(first['notify'])
        unchanged=notification_changes(first['active_alerts'],{'alerts':['SOURCE_FULL_SUCCESS_STALE']})
        self.assertFalse(unchanged['notify'])
        recovered=notification_changes(first['active_alerts'],{'alerts':[]})
        self.assertTrue(recovered['notify'])
        self.assertEqual(recovered['automatic_actions'],[])

    def test_failed_observation_does_not_claim_source_or_parent_recovery(self):
        def impaired():
            value=self.healthy()
            value['health']['source_stale_keys']=['company_ir']
            value['health']['parent_launcher_alive']=False
            return value
        pin={'identity':self.identity,'pid':123,'process_start_utc_ticks':638000000000000000}
        self.writer.execute(scheduled_at_ms=self.wall,probe=impaired)
        first=check(self.temp.name,pin,[],now_ms=self.wall,inspect=lambda _:pin['process_start_utc_ticks'])
        self.assertEqual(set(first['alerts']),{'SOURCE_FULL_SUCCESS_STALE','LAUNCHER_EXITED_CHILD_ALIVE'})
        def timeout():
            raise TimeoutError()
        self.writer.execute(scheduled_at_ms=self.wall,probe=timeout)
        uncertain=check(self.temp.name,pin,first['alerts'],now_ms=self.wall,inspect=lambda _:pin['process_start_utc_ticks'])
        self.assertEqual(uncertain['changes']['resolved'],[])
        self.assertTrue(set(first['alerts']) <= set(uncertain['alerts']))
        self.assertIn('MONITOR_READ_UNCONFIRMED',uncertain['alerts'])
        self.writer.execute(scheduled_at_ms=self.wall,probe=impaired)
        observed=check(self.temp.name,pin,uncertain['alerts'],now_ms=self.wall,inspect=lambda _:pin['process_start_utc_ticks'])
        self.assertEqual(observed['changes']['resolved'],['MONITOR_READ_UNCONFIRMED'])
        self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
        source_recovered=check(self.temp.name,pin,observed['alerts'],now_ms=self.wall,inspect=lambda _:pin['process_start_utc_ticks'])
        self.assertEqual(source_recovered['changes']['resolved'],['SOURCE_FULL_SUCCESS_STALE'])
        self.assertIn('LAUNCHER_EXITED_CHILD_ALIVE',source_recovered['alerts'])

    def test_independent_watchdog_checks_native_identity_and_chain(self):
        self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
        pin={'identity':self.identity,'pid':123,'process_start_utc_ticks':638000000000000000}
        good=check(self.temp.name,pin,[],now_ms=self.wall,inspect=lambda _:pin['process_start_utc_ticks'])
        self.assertFalse(good['changes']['notify'])
        self.assertTrue(good['receipt_chain_verified'])
        reused=check(self.temp.name,pin,[],now_ms=self.wall,inspect=lambda _:638000000000000001)
        self.assertIn('OBSERVER_IDENTITY_UNCONFIRMED',reused['alerts'])
        reads=iter([pin['process_start_utc_ticks'],None])
        race=check(self.temp.name,pin,[],now_ms=self.wall,inspect=lambda _:next(reads))
        self.assertIn('OBSERVER_IDENTITY_UNCONFIRMED',race['alerts'])

    def test_hung_probe_has_start_evidence_and_cannot_be_silently_restarted(self):
        def probe():
            start=json.loads(next(Path(self.temp.name).glob('monitor-start-*.json')).read_text())
            self.assertIsNone(start['completed_at_ms'])
            self.assertIsNone(read_receipt_chain(self.temp.name,self.identity))
            raise TimeoutError()
        value=self.writer.execute(scheduled_at_ms=self.wall,probe=probe)
        self.assertEqual(value['error_code'],'STATUS_READ_TIMEOUT')
        replacement=MonitorReceiptWriter(self.temp.name,self.identity,wall_ms=lambda:self.wall,monotonic_ms=lambda:self.mono)
        with self.assertRaises(FileExistsError):
            replacement.execute(scheduled_at_ms=self.wall,probe=self.healthy)

    def test_tampered_completion_is_unconfirmed_not_success(self):
        self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
        path=next(Path(self.temp.name).glob('monitor-execution-*.json'))
        value=json.loads(path.read_text())
        value['receipt']['completed_at_ms']+=1
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            read_receipt_chain(self.temp.name,self.identity)

    def test_pending_publication_is_invisible_and_cannot_hide_expired_previous_receipt(self):
        first=self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
        root=Path(self.temp.name)
        pin={'identity':self.identity,'pid':123,'process_start_utc_ticks':638000000000000000}
        ready,release=threading.Event(),threading.Event()
        original_link=os.link
        errors=[]
        def publish(source,target):
            if Path(target).name=='monitor-execution-000002.json':
                # Production writer has flushed and closed complete temp bytes.
                self.assertEqual(json.loads(Path(source).read_text(encoding='utf-8'))['receipt']['sequence'],2)
                ready.set()
                if not release.wait(5):
                    raise TimeoutError('test publication barrier')
            return original_link(source,target)
        def write():
            try:
                self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
            except BaseException as exc:
                errors.append(exc)
        with patch('backend.news_review_monitor.os.link',side_effect=publish):
            thread=threading.Thread(target=write)
            thread.start()
            try:
                self.assertTrue(ready.wait(5))
                self.assertFalse((root/'monitor-execution-000002.json').exists())
                fresh=check(root,pin,[],now_ms=self.wall,inspect=lambda _:pin['process_start_utc_ticks'])
                self.assertTrue(fresh['receipt_chain_verified'])
                self.assertFalse(fresh['changes']['notify'])
                stale=check(root,pin,[],now_ms=first['completed_at_ms']+360001,inspect=lambda _:pin['process_start_utc_ticks'])
                self.assertIn('MONITOR_EXECUTION_OVERDUE',stale['alerts'])
                self.assertNotIn('MONITOR_RECEIPT_IDENTITY_UNCONFIRMED',stale['alerts'])
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        completed=check(root,pin,fresh['alerts'],now_ms=self.wall,inspect=lambda _:pin['process_start_utc_ticks'])
        self.assertFalse(completed['changes']['notify'])
        self.assertEqual(read_receipt_chain(root,self.identity)['sequence'],2)
        self.assertEqual(list(root.glob('*.pending')),[])

    def test_atomic_publication_flushes_before_link_and_refuses_overwrite(self):
        path=Path(self.temp.name)/'evidence.json'
        original_fsync,original_link=os.fsync,os.link
        flushed=[]
        def sync(fd):
            original_fsync(fd)
            flushed.append(True)
        def link(source,target):
            self.assertTrue(flushed)
            self.assertEqual(json.loads(Path(source).read_text(encoding='utf-8')),{'stage':'complete'})
            original_link(source,target)
        with patch('backend.news_review_monitor.os.fsync',side_effect=sync),patch('backend.news_review_monitor.os.link',side_effect=link):
            publish_json_once(path,{'stage':'complete'})
        original=path.read_bytes()
        with self.assertRaises(FileExistsError):
            publish_json_once(path,{'stage':'replacement'})
        self.assertEqual(path.read_bytes(),original)
        self.assertEqual(list(Path(self.temp.name).glob('*.pending')),[])

    def test_corrupt_published_receipt_is_not_skipped_even_with_fresh_prior_success(self):
        self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
        (Path(self.temp.name)/'monitor-execution-000002.json').write_bytes(b'{')
        pin={'identity':self.identity,'pid':123,'process_start_utc_ticks':638000000000000000}
        verdict=check(self.temp.name,pin,[],now_ms=self.wall,inspect=lambda _:pin['process_start_utc_ticks'])
        self.assertIn('MONITOR_RECEIPT_IDENTITY_UNCONFIRMED',verdict['alerts'])
        self.assertFalse(verdict['receipt_chain_verified'])

    def test_failed_publication_leaves_start_evidence_and_cannot_restart_sequence(self):
        original=os.link
        def link(source,target):
            if Path(target).name.startswith('monitor-execution-'):
                raise OSError('fixture unsupported publication')
            original(source,target)
        with patch('backend.news_review_monitor.os.link',side_effect=link),self.assertRaises(OSError):
            self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
        self.assertEqual(self.writer.sequence,0)
        self.assertIsNone(read_receipt_chain(self.temp.name,self.identity))
        self.assertTrue((Path(self.temp.name)/'monitor-start-000001.json').exists())
        with self.assertRaises(FileExistsError):
            self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)

    @unittest.skipUnless(os.name=='nt','Native Windows identity check')
    def test_separate_watchdog_process_detects_missed_checks_despite_live_observer(self):
        self.writer.execute(scheduled_at_ms=self.wall,probe=self.healthy)
        ticks=native_creation_ticks(os.getpid())
        self.assertIsInstance(ticks,int)
        root=Path(self.temp.name)
        pin=root/'pin.json'
        pin.write_text(json.dumps({'identity':self.identity,'pid':os.getpid(),'process_start_utc_ticks':ticks}))
        output=root/'watchdog.json'
        command=[sys.executable,'-B',str(Path(__file__).resolve().parents[1]/'scripts/news_review_watchdog.py'),
            '--pin',str(pin),'--receipts',str(root),'--output',str(output)]
        result=subprocess.run(command,capture_output=True,timeout=15)
        self.assertEqual(result.returncode,2)
        verdict=json.loads(output.read_text())
        self.assertIn('MONITOR_EXECUTION_OVERDUE',verdict['alerts'])
        self.assertNotIn('OBSERVER_LIVENESS_UNCONFIRMED',verdict['alerts'])
        output2=root/'watchdog-quiet.json'
        repeat=subprocess.run(command[:-1]+[str(output2),'--previous',str(output)],capture_output=True,timeout=15)
        self.assertEqual(repeat.returncode,0)
        self.assertFalse(json.loads(output2.read_text())['changes']['notify'])

    def test_scheduler_does_not_catch_up_or_extend_on_wall_clock_rollback(self):
        outer=self
        class Stop:
            def is_set(self): return False
            def wait(self,seconds):
                outer.mono+=int(seconds*1000)
                return False
        calls=[]
        def probe():
            calls.append(self.wall)
            self.wall-=10000
            return self.healthy()
        ended=self.writer.run_schedule(probe=probe,stop_event=Stop(),expires_at_ms=self.wall+500,interval_ms=300)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.mono,1500)
        self.assertLess(ended['ended_at_ms'],ended['expires_at_ms'])
        self.assertEqual(ended['ended_monotonic_ms'],ended['deadline_monotonic_ms'])
        self.assertFalse(ended['stop_requested'])

    def test_scheduler_retains_stop_wait_signal_without_expiry_or_replay(self):
        class Stop:
            def is_set(self): return False
            def wait(self,seconds): return True
        ended=self.writer.run_schedule(probe=self.healthy,stop_event=Stop(),
                                       expires_at_ms=self.wall+500,interval_ms=300)
        self.assertEqual(self.writer.sequence,1)
        self.assertTrue(ended['stop_requested'])
        self.assertLess(ended['ended_at_ms'],ended['expires_at_ms'])
        self.assertLess(ended['ended_monotonic_ms'],ended['deadline_monotonic_ms'])
