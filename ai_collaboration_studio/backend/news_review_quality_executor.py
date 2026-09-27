"""Execute one approved synthetic batch through the existing provider boundary.

No CLI or credential acquisition lives here. The caller retains database
ownership until run() confirms its non-daemon worker has drained.
"""
from __future__ import annotations

import hashlib
import copy
import json
import re
import threading
import time
from decimal import Decimal
from pathlib import Path

from .decision_lineage import canonical_sha256
from .execution_boundary import (AuthorizedTextRequest, TextRequestSendGate, authorized_text_request,
                                 build_text_provider_request, text_generation_body)
from .news_review_contracts import ENDPOINT, MODEL, NewsReviewError, validate_result
from .news_review_monitor import publish_json_once
from .news_review_quality_ledger import QualityBatchLedger
from .news_review_quality_preparation import INPUT_RATE, OUTPUT_RATE, MAX_BYTES, MAX_OUTPUT, require
from .path_identity import first_reparse_component
from .provider_call_ledger import normalized_token_usage
from .providers.doubao_provider import DoubaoProvider
from .providers.base import normalize_provider_error_code


class QualityShutdownIncomplete(RuntimeError):
    code = 'quality_shutdown_incomplete'


def _quality_code(exc, fallback='quality_worker_failed'):
    # Only our bounded local ValueError codes may be retained, never provider
    # exception text, headers, credentials or arbitrary traceback content.
    value = str(exc) if type(exc) is ValueError else ''
    return value if re.fullmatch(r'quality_[a-z_]{1,70}', value) else fallback


class QualityBatchExecutor:
    def __init__(self, batch, provider):
        require(type(batch) is QualityBatchLedger, 'quality_batch_binding_required')
        require(type(provider) is DoubaoProvider and provider._base_url+'/responses' == ENDPOINT
                and provider.status()['configured'], 'quality_provider_binding_required')
        self.batch, self.provider = batch, provider
        state = batch.snapshot()
        require(not state['closed'] and state['calls_reserved'] == 0 and not state['inflight_attempt_id'],
                'quality_executor_requires_fresh_batch')
        self._plan = batch.plan
        self._requests = {r['id']: r for r in self._plan['prepared']['requests']}
        self.root = Path(self._plan['database_path']).parent.parent/'quality-batch-receipts'
        self.send_gate = TextRequestSendGate()
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._started = False
        self._request_started = None
        self._worker = None
        self._done = threading.Event()
        self._failure = ''

    @property
    def plan(self):
        return copy.deepcopy(self._plan)

    def request_stop(self, code):
        self.send_gate.cancel()  # Before any ledger, file, or owner operation.
        self.batch.stop(code)

    def _generation(self, request):
        payload = json.loads(request['request_utf8'])
        generation = {"instructions":payload['instructions'], "input_text":payload['input'],
                      "model":MODEL, "max_output_tokens":MAX_OUTPUT}
        body = text_generation_body(api='responses', **generation, json_output=True, thinking_disabled=True)
        raw = build_text_provider_request(ENDPOINT.removesuffix('/responses'),'responses',body,headers={}).data
        require(raw.decode('utf-8') == request['request_utf8']
                and len(raw) == request['request_bytes'] <= MAX_BYTES
                and hashlib.sha256(raw).hexdigest() == request['request_sha256'], 'quality_request_changed')
        return generation, json.loads(payload['input'])['document']

    def _publish(self, name, value):
        self.batch.owner.assert_held_for(self.batch.store.path)
        require(first_reparse_component(self.root) is None, 'quality_receipt_path_changed')
        publish_json_once(self.root/name,value)

    def _run_one(self, request_id):
        require(self._run_lock.acquire(blocking=False), 'quality_executor_inflight')
        try:
            if self.send_gate.cancelled:
                return None
            require(request_id in self._requests, 'quality_request_not_in_plan')
            request = self._requests[request_id]
            generation, document = self._generation(request)
            attempt = self.batch.reserve(request_id)
            with self._state_lock:
                self._request_started = time.monotonic()
            admission_error = ''
            def before_send():
                nonlocal admission_error
                try:
                    self.batch.before_send(attempt['id'])
                except Exception as exc:
                    admission_error = _quality_code(exc)
                    raise
            def final_check():
                nonlocal admission_error
                try:
                    self.batch.final_admission_check(attempt['id'])
                except Exception as exc:
                    admission_error = _quality_code(exc)
                    raise
            bound = AuthorizedTextRequest(ENDPOINT,request['request_sha256'],MAX_BYTES,240,
                before_send=before_send,send_gate=self.send_gate,admission_check=final_check)
            response = None
            try:
                with authorized_text_request(bound):
                    response = self.provider.generate_json(**generation)
            except BaseException:
                # An admitted request remains potentially billable, even on
                # an interrupted or malformed transport. Never retry it.
                pass
            content, content_hash, usage = '', '', {}
            reported_model, response_status, provider_error = '', '', ''
            refused, incomplete = False, False
            retained, protocol_ok, contract_ok = False, False, False
            code, contract_code = admission_error, ''
            if response is not None:
                value = response.reported_model
                if (type(value) is str and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',value)
                        and self.provider._api_key not in value):
                    reported_model = value
                response_status = response.response_status if response.response_status in {
                    'completed','incomplete','failed','cancelled','in_progress','queued'} else 'unconfirmed'
                refused, incomplete = bool(response.refused), bool(response.incomplete_reason)
                provider_error = normalize_provider_error_code(response.error_code) if response.error_code else ''
                usage = normalized_token_usage(response.usage)
                known = (type(usage) is dict and not any(k.startswith('usage_') for k in usage)
                    and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ('input_tokens','output_tokens'))
                    and usage['input_tokens'] <= request['reserved_input_tokens']
                    and usage['output_tokens'] <= request['reserved_output_tokens'])
                usage = {k:usage[k] for k in ('input_tokens','output_tokens')} if known else {}
                text = response.content
                if type(text) is str and len(text.encode('utf-8')) <= 2000000:
                    content_hash = hashlib.sha256(text.encode('utf-8')).hexdigest()
                    if self.provider._api_key in text:
                        code = 'quality_credential_echo'
                        self.request_stop(code)
                    else:
                        content, retained = text, True
                else:
                    code = 'quality_response_invalid'
                protocol_ok = bool(bound.attempted and retained and response.ok
                    and response.provider == 'doubao' and response.reported_model == MODEL
                    and response.response_status == 'completed' and not response.refused
                    and not response.incomplete_reason)
                if protocol_ok:
                    try:
                        validate_result(content,document)
                        contract_ok = True
                    except NewsReviewError as exc:
                        contract_code = exc.code
                    except Exception:
                        contract_code = 'invalid_model_output'
            if not bound.attempted:
                status = 'CANCELLED' if self.send_gate.cancelled else 'FAILED'
                code = code or 'quality_not_sent'
            elif not usage:
                status, code = 'UNKNOWN', code or 'quality_usage_unknown'
            elif protocol_ok:
                status = 'RESPONDED'
            else:
                status, code = 'FAILED', code or 'quality_response_not_complete'
            # A fast final response can race the watchdog's next sample.
            # Observe expiry before finish() may declare the batch completed;
            # still retain the already admitted response without any replay.
            if not self.batch.admission_state()['closed']:
                try:
                    self.batch.admission_check()
                except Exception:
                    self.send_gate.cancel()
            record = {'version':'news_quality_execution_v1','plan_sha256':self._plan['plan_sha256'],
                'candidate_sha':self._plan['candidate_sha'],'request_id':request_id,
                'request_sha256':request['request_sha256'],'document_sha256':request['document_sha256'],
                'provider_attempt_id':attempt['id'],'status':status,'http_attempted':bound.attempted,
                'admission_basis':'authorized_text_request_final_gate','response_content_sha256':content_hash,
                'response_retained':retained,'content':content,'usage':usage,'error_code':code,
                'protocol_completed':protocol_ok,'output_contract_valid':contract_ok,
                'reported_model':reported_model,'response_status':response_status,
                'refused':refused,'incomplete':incomplete,'provider_error_code':provider_error,
                'output_contract_error':contract_code,'human_quality_verified':False,
                'response_scope':'provider_visible_text','raw_http_envelope_retained':False,
                'external_facts_verified':False,'supplier_bill_verified':False}
            record['execution_sha256'] = canonical_sha256(record)
            try:
                self._publish('execution-'+attempt['id']+'.json',record)
            except BaseException:
                self.request_stop('quality_response_persistence_failed')
                raise
            outcome = self.batch.finish(attempt['id'],status=status,http_attempted=bound.attempted,
                response_sha256=content_hash,usage=usage)
            require(outcome['status'] == status, 'quality_outcome_disagrees')
            return {k:record[k] for k in ('request_id','provider_attempt_id','status','http_attempted',
                'execution_sha256','output_contract_valid','human_quality_verified')}
        finally:
            with self._state_lock:
                self._request_started = None
            self._run_lock.release()

    def _work(self):
        try:
            for request_id in self._requests:
                if self.send_gate.cancelled or self.batch.admission_state()['closed']:
                    break
                self._run_one(request_id)
        except BaseException as exc:
            self._failure = _quality_code(exc)
            self.request_stop('quality_worker_failed')
        finally:
            self._done.set()

    def run(self):
        """Run once; never release ownership or fabricate a report on timeout."""
        require(not self._started, 'quality_executor_already_started')
        self._started = True
        require(self.batch.snapshot()['calls_reserved'] == 0, 'quality_executor_requires_fresh_batch')
        self._publish('executor-start.json',{'version':'news_quality_executor_start_v1',
            'plan_sha256':self._plan['plan_sha256'],'phase':'claimed_no_resume'})
        self._worker = threading.Thread(target=self._work,name='news-quality-model',daemon=False)
        self._worker.start()
        drain_deadline = None
        while True:
            try:
                if self._done.wait(.1):
                    break
                if not self.batch.admission_state()['closed']:
                    try:
                        self.batch.admission_check()
                    except Exception:
                        self.send_gate.cancel()
                with self._state_lock:
                    started = self._request_started
                now = time.monotonic()
                if started is not None and now-started >= 240:
                    self.request_stop('quality_request_timeout')
                if self.send_gate.cancelled or self.batch.admission_state()['closed']:
                    self.send_gate.cancel()
                    if drain_deadline is None:
                        drain_deadline = min(now+255,started+255) if started is not None else now+255
                    if now >= drain_deadline:
                        self.request_stop('quality_shutdown_incomplete')
                        self._publish('drain-incomplete.json',{'version':'news_quality_drain_v1',
                            'plan_sha256':self._plan['plan_sha256'],'cleanup_clean':False,
                            'worker_alive':self._worker.is_alive(),'owner_release_allowed':False})
                        raise QualityShutdownIncomplete('quality_shutdown_incomplete')
            except KeyboardInterrupt:
                self.request_stop('quality_operator_interrupted')
        self._worker.join()
        state = self.batch.snapshot()
        audit = self._audit()
        result = {'version':'news_quality_execution_summary_v1','plan_sha256':self._plan['plan_sha256'],
            'cleanup_clean':True,'owner_release_allowed':True,'worker_failure_code':self._failure,
            'batch':state,'evidence_audit':audit,
            'all_requests_responded':audit['verified_responded'] == len(self._requests) and audit['complete'],
            'human_quality_verified':False,'natural_event_acceptance':False,'supplier_bill_verified':False}
        result['execution_completed'] = bool(result['all_requests_responded'] and not self._failure
            and state['stop_code']=='quality_batch_completed' and state['observed_stop_codes']==['quality_batch_completed'])
        self._publish('execution-summary.json',result)
        return result

    def _audit(self):
        """Correlate the drained worker's immutable files with permanent claims."""
        attempts = self.batch._ledger.attempts()
        missing, invalid = [], []
        responded, contract_valid, unknown = 0, 0, 0
        input_tokens, output_tokens, unknown_usage = 0, 0, 0
        expected_files = set()
        for attempt in attempts:
            name = 'execution-'+attempt['id']+'.json'
            expected_files.add(name)
            path = self.root/name
            outcome_path = self.root/(attempt['id']+'.json')
            if not path.is_file() or not outcome_path.is_file():
                missing.append(attempt['id'])
                continue
            try:
                require(first_reparse_component(path) is None and first_reparse_component(outcome_path) is None,
                        'quality_evidence_path_changed')
                require(path.stat().st_size <= 2100000 and outcome_path.stat().st_size <= 16000,
                        'quality_evidence_size_invalid')
                record = json.loads(path.read_bytes())
                outcome = json.loads(outcome_path.read_bytes())
                request = self._requests[attempt['operation_target_id']]
                require(attempt['operation_target_type']=='quality_request' and attempt['provider']=='doubao'
                        and attempt['model']==MODEL,'quality_ledger_binding_invalid')
                digest = record.pop('execution_sha256')
                receipt_digest = outcome.pop('receipt_sha256')
                require(digest == canonical_sha256(record) and receipt_digest == canonical_sha256(outcome),
                        'quality_evidence_hash_invalid')
                for value in (record,outcome):
                    require(value['plan_sha256'] == self._plan['plan_sha256']
                        and value['provider_attempt_id'] == attempt['id'] and value['request_id'] == request['id']
                        and value['request_sha256'] == request['request_sha256']
                        and value['document_sha256'] == request['document_sha256'], 'quality_evidence_binding_invalid')
                require(record['status'] == outcome['status'] and record['usage'] == outcome['usage']
                        and record['http_attempted'] == outcome['http_attempted']
                        and record['response_content_sha256'] == outcome['response_sha256']
                        and attempt['status'] == ('ABANDONED' if record['status']=='UNKNOWN' else record['status']),
                        'quality_evidence_outcome_invalid')
                if record['response_retained']:
                    require(hashlib.sha256(record['content'].encode('utf-8')).hexdigest() == record['response_content_sha256'],
                            'quality_retained_content_changed')
                responded += int(record['status']=='RESPONDED')
                unknown += int(record['status']=='UNKNOWN')
                contract_valid += int(record['output_contract_valid'])
                input_tokens += record['usage'].get('input_tokens',0)
                output_tokens += record['usage'].get('output_tokens',0)
                unknown_usage += int(record['http_attempted'] and not record['usage'])
            except (ValueError,KeyError,TypeError,OSError):
                invalid.append(attempt['id'])
        unexpected = sorted(p.name for p in self.root.glob('execution-*.json')
                            if p.name not in expected_files and p.name != 'execution-summary.json')
        return {'complete':not (missing or invalid or unexpected),'ledger_attempts':len(attempts),
            'verified_responded':responded,'output_contract_valid':contract_valid,'unknown':unknown,
            'reported_input_tokens':input_tokens,'reported_output_tokens':output_tokens,
            'admitted_attempts_with_unknown_usage':unknown_usage,
            'known_usage_estimated_cny':str((input_tokens*INPUT_RATE+output_tokens*OUTPUT_RATE)/Decimal(1000000)),
            'supplier_bill_verified':False,
            'missing_attempt_evidence':missing,'invalid_attempt_evidence':invalid,'unexpected_evidence':unexpected,
            'human_quality_verified':False}
