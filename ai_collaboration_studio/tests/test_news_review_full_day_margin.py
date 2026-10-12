"""Opt-in future full-day margin; synthetic clocks/owned fixtures only."""
import copy
import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import news_review_contracts as contracts
from backend import news_review_clock_policy_review as clocks
from backend.decision_lineage import canonical_sha256
from backend.news_review_monitor_probe import NewsReviewStatusProbe, ProbeError, ENDPOINTS, canonical
from backend.news_review_report import NewsReviewJournal, build_news_review_report
from backend.news_review_runtime_clock import BoundRuntimeClock
from backend.news_review_service import NewsReviewService
from backend.news_review_controller import NewsReviewController
from backend.news_review_stop import NewsReviewStop
from scripts import run_news_review_observer as observer
from scripts import news_review_observer_wait as waiting
from scripts import run_news_review_trial as trial
from tests import test_news_review as fixtures
from tests import test_news_review_monitor_probe as probe_fixtures
from tests import test_news_review_observer_wait as wait_fixtures


POLICY = 'news_event_review_full_day_margin_v2'
WINDOW = 'news_review_full_day_margin_reference_v2'
RUNTIME = 'news_review_runtime_clock_full_day_margin_v2'
ACTIVATION = 'news_review_full_day_margin_activation_v2'
TARGET = 86_400_000
MAXIMUM = 86_700_000
CLOCK_POLICY = {**contracts.DUAL_CLOCK_POLICY,
    'version': 'dual_deadline_windows_full_day_margin_v2',
    'maximum_authorized_duration_ms': MAXIMUM, 'required_elapsed_lower_ms': TARGET}


def policy(version=POLICY, duration=MAXIMUM):
    start = 1790000000000
    result = {'version': version, 'policy_id': 'margin-offline', 'candidate_sha': 'a'*40,
        'database_path': 'C:/synthetic/NewsReviewTrialMargin/data/studio.sqlite3', 'room_id': 'room_margin',
        'profile': contracts.PROFILE, 'sources': copy.deepcopy(contracts.SOURCES), 'poll_interval_ms': 300000,
        'initialization': 'seed_only', 'strategy_sha256': contracts.STRATEGY_SHA,
        'provider': 'doubao', 'model': contracts.MODEL, 'endpoint': contracts.ENDPOINT,
        'not_before_ms': start, 'expires_at_ms': start+duration,
        'max_document_requests': 16, 'max_model_calls': 8, 'max_request_bytes': 32768,
        'max_output_tokens': 1400, 'max_total_tokens': 275392, 'spend_limit_cny': '2.00',
        'rate_card': {'currency': 'CNY', 'input_per_million': '3', 'output_per_million': '9',
                     'source_url': 'https://www.volcengine.com/docs/82379/1544106',
                     'checked_at': 'offline fixture; not independently verified'},
        'concurrency': 1, 'resume_within_window': False, 'stop_on_unknown': True}
    if version != contracts.VERSION:
        result.update(clock_policy=copy.deepcopy(CLOCK_POLICY if version == POLICY else contracts.DUAL_CLOCK_POLICY),
                      clock_activation=None)
    return result


class MarginContractTests(unittest.TestCase):
    def assert_code(self, expected, action):
        with self.assertRaises(contracts.NewsReviewError) as raised:
            action()
        self.assertEqual(raised.exception.code, expected)

    def test_legacy_policy_and_dual_v1_keep_exact_hard_24h(self):
        for version in (contracts.VERSION, contracts.DUAL_POLICY_VERSION):
            self.assertEqual(contracts.validate_policy(policy(version, TARGET), allow_clock_template=True)['version'], version)
            self.assert_code('invalid_window', lambda: contracts.validate_policy(
                policy(version, TARGET+1), allow_clock_template=True))

    def test_new_version_allows_only_its_explicit_24h05m_bound(self):
        admitted = contracts.validate_policy(policy(), allow_clock_template=True)
        self.assertEqual(admitted['clock_policy'], CLOCK_POLICY)
        self.assertTrue(contracts.is_dual_policy(admitted))
        self.assertEqual(contracts.FULL_DAY_POLICY_VERSION, POLICY)
        self.assert_code('invalid_window', lambda: contracts.validate_policy(
            policy(duration=MAXIMUM+1), allow_clock_template=True))

    def test_margin_budget_does_not_grant_extra_calls_bytes_tokens_or_money(self):
        for field, value in (('max_model_calls',9),('max_document_requests',17),('max_request_bytes',32769),
                             ('max_output_tokens',1401),('max_total_tokens',275393),('spend_limit_cny','2.01')):
            with self.subTest(field=field):
                self.assert_code('full_day_budget_exceeded', lambda: contracts.validate_policy(
                    {**policy(),field:value},allow_clock_template=True))

    def test_v1_cannot_borrow_new_clock_version_and_new_version_cannot_borrow_v1(self):
        for p in ({**policy(contracts.DUAL_POLICY_VERSION,TARGET),'clock_policy':CLOCK_POLICY},
                  {**policy(),'clock_policy':contracts.DUAL_CLOCK_POLICY},
                  {**policy(),'clock_policy':{**CLOCK_POLICY,'required_elapsed_lower_ms':TARGET-1}}):
            self.assert_code('clock_policy_mismatch',lambda:contracts.validate_policy(p,allow_clock_template=True))

    def test_new_version_retains_single_activation_resume_and_unknown_gates(self):
        self.assert_code('clock_activation_required',lambda:contracts.validate_policy(policy()))
        self.assert_code('dual_clock_resume_forbidden',lambda:contracts.validate_policy(
            {**policy(),'resume_within_window':True},allow_clock_template=True))
        self.assert_code('invalid_stop_policy',lambda:contracts.validate_policy(
            {**policy(),'stop_on_unknown':False},allow_clock_template=True))

    def test_unknown_or_boolean_versions_and_extra_fields_are_not_opt_in(self):
        for p in ({**policy(),'version':True},{**policy(),'version':POLICY+'-unknown'},
                  {**policy(),'maximum_authorized_duration_ms':MAXIMUM}):
            with self.subTest(p=p),self.assertRaises(contracts.NewsReviewError):
                contracts.validate_policy(p,allow_clock_template=True)

    def test_new_duration_rejects_nonpositive_boolean_and_float_endpoints(self):
        for end in (True,0,1790000000000,1790000000000+MAXIMUM+0.5):
            self.assert_code('invalid_window',lambda:contracts.validate_policy(
                {**policy(),'expires_at_ms':end},allow_clock_template=True))
        self.assertEqual(contracts.validate_policy(policy(duration=TARGET+1),allow_clock_template=True)
                         ['expires_at_ms']-policy()['not_before_ms'],TARGET+1)

    def test_cli_duration_limit_is_chosen_from_config_before_database_open(self):
        with tempfile.TemporaryDirectory(prefix='margin-cli-rejection-') as temporary:
            root=Path(temporary)
            for version,duration,code in ((contracts.DUAL_POLICY_VERSION,TARGET+1,'invalid_duration'),
                                          (POLICY,MAXIMUM+1,'invalid_duration'),
                                          (POLICY,MAXIMUM,'trial_not_initialized')):
                p=policy(version,duration=3600000)
                p['database_path']=str(root/'NewsReviewTrialOfflineMargin'/'data'/'studio.sqlite3')
                config=root/(version+'.json')
                config.write_text(json.dumps(p),encoding='utf-8')
                self.assert_code(code,lambda:trial.main(
                    ['--prepare-activation','--config',str(config),'--duration-ms',str(duration)]))
                self.assertFalse(Path(p['database_path']).exists())

    def test_default_boundwindow_is_v1_and_rejects_one_ms_extra(self):
        reading=clocks.Reading(1000,10000,10001,101,123)
        self.assertTrue(clocks.BoundWindow('a'*64,1000,1000+TARGET,reading).valid())
        self.assertFalse(clocks.BoundWindow('a'*64,1000,1000+TARGET+1,reading).valid())

    def test_new_boundwindow_requires_exact_version_and_preserves_sampling_limit(self):
        reading=clocks.Reading(1000,10000,10001,101,123)
        window=clocks.BoundWindow('a'*64,1000,1000+MAXIMUM,reading,version=WINDOW)
        self.assertTrue(window.valid())
        self.assertFalse(dataclasses.replace(window,expires_at_ms=window.expires_at_ms+1).valid())
        self.assertFalse(dataclasses.replace(window,version=True).valid())
        self.assertFalse(dataclasses.replace(window,version=WINDOW+'-unknown').valid())
        self.assertFalse(dataclasses.replace(window,activation_reading=dataclasses.replace(reading,
            monotonic_after_ms=10051)).valid())

    def test_margin_guard_still_only_shortens_and_first_expiry_is_latched(self):
        reading=clocks.Reading(1000,10000,10000,101,123)
        current=[reading]
        guard=clocks.DualDeadlineReference(clocks.BoundWindow('a'*64,1000,1000+MAXIMUM,reading,version=WINDOW),
                                           lambda:current[0])
        current[0]=clocks.Reading(11500,20000,20000,101,123)
        first=guard.check()
        self.assertEqual(first['effective_elapsed_deadline_ms'],10000+MAXIMUM-500)
        current[0]=clocks.Reading(21200,30000,30000,101,123)
        self.assertEqual(guard.check()['effective_elapsed_deadline_ms'],first['effective_elapsed_deadline_ms'])
        current[0]=clocks.Reading(1000+MAXIMUM,10000+MAXIMUM-500,10000+MAXIMUM-500,101,123)
        final=guard.check()
        self.assertEqual(final['stop_reason'],'absolute_deadline_reached')
        current[0]=reading
        self.assertEqual(guard.check(),final)
        self.assertFalse(final['deadline_extended'])
        self.assertFalse(final['request_authorized'])
        self.assertEqual(final['version'],WINDOW)

    def test_new_runtime_clock_carries_its_explicit_bound_identity(self):
        p=policy()
        p['clock_activation']=vars(clocks.Reading(p['not_before_ms'],10000,10001,101,123,
                                                10000001000,10000001000))
        current=clocks.Reading(p['not_before_ms']+1000,11000,11001,101,123,11000001000,11000001000)
        runtime=BoundRuntimeClock(p,sample=lambda:current)
        self.assertTrue(runtime.check()['within_proposed_time_bounds'])
        snapshot=runtime.snapshot()
        self.assertEqual(snapshot['version'],RUNTIME)
        self.assertEqual(snapshot['clock_policy'],CLOCK_POLICY['version'])
        self.assertEqual(snapshot['reference']['version'],WINDOW)
        self.assertEqual(snapshot['policy_sha256'],canonical_sha256(p))


class MarginRuntimeReportTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.NewsReviewTests()
        self.f.addCleanup=self.addCleanup
        self.f.setUp()
        self.mono=100000
        self.f.policy.update(version=POLICY,clock_policy=copy.deepcopy(CLOCK_POLICY),
            max_request_bytes=32768,spend_limit_cny='2.00',resume_within_window=False,
            expires_at_ms=self.f.now+MAXIMUM,clock_activation=vars(self.sample()))
        self.f.service=self.new_service()

    def sample(self):
        return clocks.Reading(self.f.now,self.mono,self.mono+1,12345,638000000000000001,
                             self.mono*1000000+1000,self.mono*1000000+1000)

    def new_service(self):
        f=self.f
        return NewsReviewService(f.store,f.providers,instance_owner=f.owner,documents=f.documents,
                                clock=lambda:f.now,monotonic_ms=lambda:self.mono,clock_sampler=self.sample)

    def report_at(self, elapsed, *, correction=0):
        f=self.f
        f.approve()
        anchor=f.now
        journal=NewsReviewJournal(f.service,f.policy['policy_id'])
        journal.record('session_started')
        remaining=elapsed
        while remaining:
            step=min(29000,remaining)
            self.mono+=step
            f.now+=step
            remaining-=step
            f.service.check_clock(allow_expiry=True)
        if correction:
            f.now+=correction
            f.service.check_clock(allow_expiry=True)
        journal.record('heartbeat')
        return build_news_review_report(f.service,f.policy['policy_id'])

    def test_large_gradual_shortening_is_still_below24h_and_late_report_cannot_fill_it(self):
        f=self.f
        f.approve()
        journal=NewsReviewJournal(f.service,f.policy['policy_id'])
        journal.record('session_started')
        while True:
            self.mono+=29000
            f.now+=29101
            verdict=f.service._dual_clock.check(allow_expiry=True)
            if not verdict['within_proposed_time_bounds']:break
        journal.record('window_closed')
        first=build_news_review_report(f.service,f.policy['policy_id'])
        self.assertFalse(first['authorization_time']['actual_full_24h_proven'])
        self.assertLess(first['authorization_time']['authorized_elapsed_lower_ms'],TARGET)
        f.now+=TARGET
        later=build_news_review_report(f.service,f.policy['policy_id'])
        self.assertEqual(later['authorization_time'],first['authorization_time'])
        self.assertFalse(later['continuity']['full_24h_window_elapsed'])

    def test_report_lower_one_ms_short_fails_even_if_upper_is24h(self):
        report=self.report_at(TARGET)
        self.assertEqual(report['authorization_time']['authorized_elapsed_lower_ms'],TARGET-1)
        self.assertEqual(report['authorization_time']['authorized_elapsed_upper_ms'],TARGET+1)
        self.assertFalse(report['authorization_time']['actual_full_24h_proven'])
        self.assertFalse(report['continuity']['full_24h_window_elapsed'])

    def test_report_exact_lower24h_passes_only_elapsed_metric_not_full_acceptance(self):
        report=self.report_at(TARGET+1)
        self.assertEqual(report['authorization_time']['authorized_elapsed_lower_ms'],TARGET)
        self.assertTrue(report['authorization_time']['actual_full_24h_proven'])
        self.assertTrue(report['continuity']['full_24h_window_elapsed'])
        self.assertFalse(report['continuity']['continuous_window_observed'])
        self.assertFalse(report['outcome']['acceptance_passed'])
        self.assertEqual(self.f.count('provider_call_attempts'),0)

    def test_24h05m_with5521ms_gradual_calibration_has_margin_not_repaired_history(self):
        f=self.f
        f.approve()
        journal=NewsReviewJournal(f.service,f.policy['policy_id'])
        journal.record('session_started')
        elapsed=0
        offset=0
        while elapsed < MAXIMUM:
            step=min(29000,MAXIMUM-elapsed)
            elapsed+=step
            self.mono+=step
            correction=min(2,5521-offset)
            offset+=correction
            f.now+=step+correction
            verdict=f.service._dual_clock.check(allow_expiry=True)
            if not verdict['within_proposed_time_bounds']:
                break
        journal.record('window_closed')
        report=build_news_review_report(f.service,f.policy['policy_id'])
        self.assertEqual(offset,5521)
        self.assertTrue(report['authorization_time']['actual_full_24h_proven'])
        self.assertEqual(f.service._dual_clock.reference.snapshot()['effective_elapsed_deadline_ms'],
                         100000+MAXIMUM-5521)
        self.assertFalse(report['outcome']['acceptance_passed'])

    def test_early_identity_failure_cannot_be_promoted_by_late_report(self):
        f=self.f
        f.approve()
        journal=NewsReviewJournal(f.service,f.policy['policy_id'])
        journal.record('session_started')
        bad=dataclasses.replace(self.sample(),process_creation_utc_ticks=638000000000000002)
        f.service._dual_clock.reference._sample=lambda:bad
        with self.assertRaises(contracts.NewsReviewError):f.service.check_clock()
        f.now+=MAXIMUM+1000
        report=build_news_review_report(f.service,f.policy['policy_id'])
        self.assertFalse(report['authorization_time']['actual_full_24h_proven'])
        self.assertFalse(report['authorization_time']['authorized_window_end_observed'])
        self.assertEqual(report['authorization_time']['stop_reason'],'process_identity_changed')

    def test_new_version_cannot_recover_or_resume_existing_owner(self):
        f=self.f
        f.approve()
        for action in (f.service.recover,lambda:self.new_service().approve(f.policy,
                approved_policy_sha256=canonical_sha256(f.policy)),lambda:f.service.resume(f.policy['policy_id'],
                approved_policy_sha256=canonical_sha256(f.policy))):
            with self.assertRaises(contracts.NewsReviewError):action()
        self.assertEqual(f.count('provider_call_attempts'),0)


class MarginProbeTests(unittest.TestCase):
    def setUp(self):
        self.f=probe_fixtures.ProbeTests()
        self.f.addCleanup=self.addCleanup
        self.f.setUp()
        p=self.f.control['news_review']['policy']
        p.update(version=POLICY,clock_policy=copy.deepcopy(CLOCK_POLICY),expires_at_ms=p['not_before_ms']+MAXIMUM)
        identity=self.f.target['identity']
        identity['policy_sha256']=canonical(p)
        self.f.control['news_review']['policy_sha256']=identity['policy_sha256']
        self.f.tree['policy_sha256']=identity['policy_sha256']
        self.f.target.update(expires_at_ms=p['expires_at_ms'],window_contract_version=POLICY)

    def test_explicit_margin_target_supports_24h05m_after_native_checks(self):
        result=self.f.probe()()
        self.assertTrue(result['health']['host_alive'])
        self.assertEqual(self.f.calls,[(55029,path) for path in ENDPOINTS])

    def test_legacy_or_unknown_target_does_not_widen_window(self):
        for value in (None,True,contracts.DUAL_POLICY_VERSION,POLICY+'-unknown'):
            with self.subTest(value=value),self.assertRaises(ProbeError):
                NewsReviewStatusProbe({**self.f.target,'window_contract_version':value},
                                     inspect_tree=lambda:None,wall_ms=lambda:0)
        with self.assertRaises(ProbeError):
            NewsReviewStatusProbe({k:v for k,v in self.f.target.items() if k!='window_contract_version'},
                                 inspect_tree=lambda:None,wall_ms=lambda:0)

    def test_self_claimed_margin_target_must_match_readback_policy_and_clock(self):
        for mutate in (lambda p:p.update(version=contracts.DUAL_POLICY_VERSION),
                       lambda p:p.update(clock_policy=contracts.DUAL_CLOCK_POLICY)):
            self.f.calls.clear()
            p=copy.deepcopy(self.f.control['news_review']['policy'])
            mutate(p)
            digest=canonical(p)
            self.f.control['news_review'].update(policy=p,policy_sha256=digest)
            self.f.target['identity']['policy_sha256']=digest
            self.f.tree['policy_sha256']=digest
            with self.assertRaisesRegex(ProbeError,'monitor_window_contract_mismatch'):
                self.f.probe()()
            self.assertEqual(self.f.calls,[(55029,ENDPOINTS[0])])

    def test_margin_still_refuses_unverified_native_tree_before_any_get(self):
        self.f.tree['safe_status_reads_allowed']=False
        with self.assertRaises(ProbeError):self.f.probe()()
        self.assertEqual(self.f.calls,[])

    def test_boolean_clock_contract_number_cannot_borrow_integer_identity(self):
        p=self.f.control['news_review']['policy']
        p['clock_policy']['activations']=True
        digest=canonical(p)
        self.f.control['news_review']['policy_sha256']=digest
        self.f.target['identity']['policy_sha256']=digest
        self.f.tree['policy_sha256']=digest
        with self.assertRaisesRegex(ProbeError,'monitor_window_contract_mismatch'):
            self.f.probe()()
        self.assertEqual(self.f.calls,[(55029,ENDPOINTS[0])])


class MarginWaitingTests(unittest.TestCase):
    def setUp(self):
        self.f=wait_fixtures.WaitingObserverTests()
        self.f.addCleanup=self.addCleanup
        self.f.setUp()

    def margin_proposal(self, duration=MAXIMUM):
        f=self.f
        proposal=observer.read_json(f.root/'activation-proposal.json')
        template=policy(duration=3600000)
        old=proposal['activation']['policy_template']
        template.update(policy_id=old['policy_id'],candidate_sha=old['candidate_sha'],database_path=old['database_path'],
                        not_before_ms=old['not_before_ms'],expires_at_ms=old['expires_at_ms'])
        proposal['activation'].update(version=ACTIVATION,policy_template=template,duration_ms=duration,
                                     permitted_changes=['not_before_ms','expires_at_ms','clock_activation'])
        proposal['activation_sha256']=canonical(proposal['activation'])
        wait_fixtures.write(f.root/'activation-proposal.json',proposal)
        manifest=observer.read_json(f.root/'preparation-manifest.json')
        manifest['activation_sha256']=proposal['activation_sha256']
        for row in manifest['files']:
            if row['name']=='activation-proposal.json':row['sha256']=observer.sha(f.root/'activation-proposal.json')
        wait_fixtures.write(f.root/'preparation-manifest.json',manifest)
        f.request['activation_sha256']=proposal['activation_sha256']
        return proposal

    def test_wait_prepare_binds_v2_duration_without_start_or_http(self):
        self.margin_proposal()
        with patch.object(observer,'verify_checkout'),patch('socket.socket') as wire:
            result=observer.prepare(self.f.request,str(self.f.f.shell),poll_interval_ms=150000)
        self.assertEqual(result['plan']['activation']['duration_ms'],MAXIMUM)
        self.assertEqual(result['plan']['activation']['version'],ACTIVATION)
        self.assertEqual(result['plan']['poll_interval_ms'],150000)
        self.assertEqual(result['plan']['maximum_silence_ms'],360000)
        self.assertFalse(result['plan']['request_authorized'])
        self.assertFalse((self.f.root/'activation-receipt.json').exists())
        wire.assert_not_called()

    def test_wait_rejects_one_ms_above_margin_and_v1_activation_borrow(self):
        self.margin_proposal(MAXIMUM+1)
        with patch.object(observer,'verify_checkout'),self.assertRaises(ProbeError):
            observer.prepare(self.f.request,str(self.f.f.shell))
        proposal=self.margin_proposal()
        proposal['activation']['version']='news_review_dual_clock_activation_v1'
        proposal['activation_sha256']=canonical(proposal['activation'])
        wait_fixtures.write(self.f.root/'activation-proposal.json',proposal)
        # Independent proposal identity is kept coherent so version is the rejection.
        manifest=observer.read_json(self.f.root/'preparation-manifest.json')
        manifest['activation_sha256']=proposal['activation_sha256']
        for row in manifest['files']:
            if row['name']=='activation-proposal.json':row['sha256']=observer.sha(self.f.root/'activation-proposal.json')
        wait_fixtures.write(self.f.root/'preparation-manifest.json',manifest)
        self.f.request['activation_sha256']=proposal['activation_sha256']
        with patch.object(observer,'verify_checkout'),self.assertRaisesRegex(ProbeError,'activation_proposal_mismatch'):
            observer.prepare(self.f.request,str(self.f.f.shell))

    def test_wait_to_active_handoff_retains_new_version_and_exact_cadence(self):
        f=self.f
        proposal=self.margin_proposal()
        activation=f.active['activation-receipt.json']
        begin=activation['activated_at']
        template=proposal['activation']['policy_template']
        host_pin=f.f.request['host_pin']
        reading=clocks.Reading(begin,10000,10001,host_pin['pid'],
                              observer.native_ticks(host_pin['start_utc']),10000001000,10000001000)
        active_policy={**template,'not_before_ms':begin,'expires_at_ms':begin+MAXIMUM,
                       'clock_activation':vars(reading)}
        identity={**f.f.request['target']['identity'],'activation_sha256':proposal['activation_sha256'],
                  'policy_sha256':canonical(active_policy)}
        f.active['activation-policy.json']=active_policy
        activation.update(activation=proposal['activation'],activation_sha256=identity['activation_sha256'],
            policy_sha256=identity['policy_sha256'],expires_at=begin+MAXIMUM,
            activation_monotonic_before_ns=reading.monotonic_before_ns,
            activation_monotonic_after_ns=reading.monotonic_after_ns)
        f.active['host-fixture.json'].update(policy_sha256=identity['policy_sha256'],expires_at_ms=begin+MAXIMUM)
        f.f.request['target']['identity']=identity
        for name in ('launch-receipt.json','launcher-start.json','user-approval.json'):
            value=observer.read_json(f.root/name)
            value.update(identity)
            if name=='user-approval.json':value['preparation_manifest_sha256']=observer.sha(f.root/'preparation-manifest.json')
            wait_fixtures.write(f.root/name,value)
        with patch.object(observer,'verify_checkout'):
            f.wrapped=observer.prepare(f.request,str(f.f.shell),poll_interval_ms=150000)
        session=f.session()
        with patch.object(observer,'NativeInspector',f.inspector_class()),patch.object(observer,'verify_checkout'), \
             patch('socket.socket') as wire:
            self.assertEqual(session.step()['phase'],'waiting_local_input')
            f.activate();f.wall+=30000;f.mono+=30000
            resolved=session.step()['resolved_plan']
        self.assertEqual(resolved['request']['target']['window_contract_version'],POLICY)
        self.assertEqual(resolved['request']['target']['expires_at_ms'],begin+MAXIMUM)
        self.assertEqual(resolved['poll_interval_ms'],150000)
        self.assertEqual(resolved['maximum_silence_ms'],360000)
        self.assertFalse(resolved['request_authorized'])
        wire.assert_not_called()


if __name__=='__main__':unittest.main()
