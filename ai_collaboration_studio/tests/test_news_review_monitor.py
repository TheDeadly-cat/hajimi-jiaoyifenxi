import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from backend.news_review_monitor import MonitorReceiptWriter,evaluate_watchdog,source_freshness,notification_changes
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
        self.writer.run_schedule(probe=probe,stop_event=Stop(),expires_at_ms=self.wall+500,interval_ms=300)
        self.assertEqual(len(calls),1)
        self.assertEqual(self.mono,1500)
