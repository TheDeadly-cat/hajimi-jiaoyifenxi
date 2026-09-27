"""Offline human-rating bindings; files only, no execution or approval authority."""
from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re

from .decision_lineage import canonical_sha256
from .news_review_contracts import MODEL, NewsReviewError, validate_result
from .news_review_quality_ledger import build_batch_plan
from .news_review_quality_preparation import _signoffs, require, strict_json
from .news_review_quality_scorecard import RUBRIC, score_quality
from .path_identity import first_reparse_component

VERSION = 'news_quality_bound_ratings_v1'
_EDITABLE = {'reviewer','reviewed_at','scores','critical_failures'}


def _read(path, limit=2100000):
    require(first_reparse_component(path) is None and path.is_file(), 'quality_evidence_path_invalid')
    with path.open('rb') as source:
        raw = source.read(limit+1)
    require(len(raw) <= limit, 'quality_evidence_too_large')
    # Execution envelopes may add a small header to the bounded visible text.
    def unique(pairs):
        value = {}
        for key,item in pairs:
            require(key not in value, 'quality_evidence_duplicate_key')
            value[key] = item
        return value
    value = json.loads(raw,object_pairs_hook=unique,
                       parse_constant=lambda _:require(False,'quality_evidence_nonfinite'))
    return value,hashlib.sha256(raw).hexdigest()


def evaluation_template(corpus_raw, reference_raw, plan_raw, receipt_directory):
    """Freeze existing output identities, never manufacture ratings or receipts.

    Ledger correlation is the drained host's reported audit, not a new SQLite
    query or an independent proof that a remote supplier processed the request.
    """
    supplied = strict_json(plan_raw)
    plan = build_batch_plan(corpus_raw,reference_raw,candidate_sha=supplied['candidate_sha'],
        database_path=supplied['database_path'],room_id=supplied['room_id'],
        not_before_ms=supplied['not_before_ms'],expires_at_ms=supplied['expires_at_ms'])
    require(canonical_sha256(plan)==canonical_sha256(supplied),'quality_plan_changed')
    references,_ = _signoffs(corpus_raw,reference_raw)
    reference_map = {entry['case_id']:entry for entry in references['cases']}
    root = Path(receipt_directory)
    require(root.is_absolute() and root.is_dir() and first_reparse_component(root) is None,
            'quality_evidence_directory_invalid')
    summary,summary_sha = _read(root/'execution-summary.json',1000000)
    terminal,terminal_sha = _read(root.parent/'launcher-exit.json',16000)
    require(summary['version']=='news_quality_execution_summary_v1'
        and terminal['version']=='news_quality_launcher_exit_v1'
        and summary['plan_sha256']==terminal['plan_sha256']==plan['plan_sha256']
        and summary['cleanup_clean'] is True and summary['owner_release_allowed'] is True
        and terminal['worker_drained'] is True and terminal['checkpoint_completed'] is True,
        'quality_terminal_receipts_unconfirmed')
    require(type(summary['execution_completed']) is bool and type(summary['all_requests_responded']) is bool,
            'quality_summary_invalid')
    require(type(terminal['exit_code']) is int
        and terminal['exit_code']==(0 if summary['execution_completed'] else 2),'quality_terminal_disagrees')
    requests = {r['id']:r for r in plan['prepared']['requests']}
    observed,manifest,attempt_ids = {},[],set()
    files = sorted(p for p in root.glob('execution-*.json') if p.name!='execution-summary.json')
    require(len(files)<=len(requests),'quality_extra_execution_records')
    for path in files:
        record,file_sha = _read(path)
        require(record['version']=='news_quality_execution_v1','quality_execution_version_invalid')
        digest = record.pop('execution_sha256')
        require(digest==canonical_sha256(record),'quality_execution_hash_invalid')
        request_id,attempt_id = record['request_id'],record['provider_attempt_id']
        require(request_id in requests and request_id not in observed
            and type(attempt_id) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',attempt_id)
            and attempt_id not in attempt_ids and path.name=='execution-'+attempt_id+'.json',
            'quality_execution_identity_invalid')
        request = requests[request_id]
        outcome,outcome_sha = _read(root/(attempt_id+'.json'),16000)
        outcome_digest = outcome.pop('receipt_sha256')
        require(outcome_digest==canonical_sha256(outcome),'quality_outcome_hash_invalid')
        for value in (record,outcome):
            require(value['plan_sha256']==plan['plan_sha256'] and value['request_id']==request_id
                and value['provider_attempt_id']==attempt_id and value['request_sha256']==request['request_sha256']
                and value['document_sha256']==request['document_sha256'],'quality_output_binding_changed')
        require(record['candidate_sha']==plan['candidate_sha'] and outcome['version']=='news_quality_outcome_v1'
            and record['status'] in {'RESPONDED','FAILED','CANCELLED','UNKNOWN'}
            and record['status']==outcome['status'] and record['usage']==outcome['usage']
            and type(record['http_attempted']) is bool and type(outcome['http_attempted']) is bool
            and record['http_attempted']==outcome['http_attempted']
            and record['response_content_sha256']==outcome['response_sha256'], 'quality_outcome_disagrees')
        require(type(record['response_retained']) is bool and type(record['content']) is str
            and type(record['protocol_completed']) is bool and type(record['output_contract_valid']) is bool,
            'quality_output_flags_invalid')
        if record['response_retained']:
            require(hashlib.sha256(record['content'].encode('utf-8')).hexdigest()==record['response_content_sha256'],
                    'quality_output_content_changed')
        else:
            require(record['content']=='','quality_unretained_content_invalid')
        if record['status']=='RESPONDED':
            require(record['http_attempted'] and record['response_retained'] and record['protocol_completed']
                and record['reported_model']==MODEL and record['response_status']=='completed'
                and record['refused'] is False and record['incomplete'] is False,'quality_response_unconfirmed')
            usage = record['usage']
            require(type(usage) is dict and set(usage)=={'input_tokens','output_tokens'}
                and type(usage['input_tokens']) is int and 0<=usage['input_tokens']<=request['reserved_input_tokens']
                and type(usage['output_tokens']) is int and 0<=usage['output_tokens']<=request['reserved_output_tokens'],
                'quality_usage_unconfirmed')
        contract_valid = False
        if record['protocol_completed']:
            document = json.loads(json.loads(request['request_utf8'])['input'])['document']
            try:
                validate_result(record['content'],document)
                contract_valid = True
            except (NewsReviewError,TypeError,ValueError):
                pass
        require(record['output_contract_valid']==contract_valid,'quality_contract_check_disagrees')
        observed[request_id] = {'provider_attempt_id':attempt_id,'execution_sha256':digest,
            'response_content_sha256':record['response_content_sha256'],'status':record['status'],
            'output_contract_valid':contract_valid,'rating_eligible':record['status']=='RESPONDED'}
        attempt_ids.add(attempt_id)
        manifest.append({'file':path.name,'sha256':file_sha,'outcome_file':attempt_id+'.json','outcome_sha256':outcome_sha})
    audit,batch = summary['evidence_audit'],summary['batch']
    responded = sum(v['status']=='RESPONDED' for v in observed.values())
    valid = sum(v['output_contract_valid'] for v in observed.values())
    require(batch['plan_sha256']==plan['plan_sha256'] and type(batch['calls_reserved']) is int
        and batch['calls_reserved']==len(observed)==audit['ledger_attempts']
        and audit['complete'] is True and audit['verified_responded']==responded
        and audit['output_contract_valid']==valid and not audit['missing_attempt_evidence']
        and not audit['invalid_attempt_evidence'] and not audit['unexpected_evidence'],
        'quality_host_audit_incomplete_or_disagrees')
    require(summary['all_requests_responded']==(responded==len(requests)),'quality_response_count_disagrees')
    if summary['execution_completed']:
        require(responded==len(requests) and batch['stop_code']=='quality_batch_completed'
            and batch['observed_stop_codes']==['quality_batch_completed'] and not summary['worker_failure_code'],
            'quality_completion_disagrees')
    rows = []
    for request in requests.values():
        binding = observed.get(request['id'],{'provider_attempt_id':None,'execution_sha256':None,
            'response_content_sha256':None,'status':'no_verifiable_output','output_contract_valid':None,'rating_eligible':False})
        rows.append({'id':request['id'],'case_id':request['case_id'],
            'case_sha256':reference_map[request['case_id']]['case_sha256'],
            'request_sha256':request['request_sha256'],'document_sha256':request['document_sha256'],**binding,
            'reviewer':None,'reviewed_at':None,'scores':None,'critical_failures':None})
    return {'version':VERSION,'plan_sha256':plan['plan_sha256'],'candidate_sha':plan['candidate_sha'],
        'corpus_sha256':plan['prepared']['corpus_sha256'],'reference_sha256':hashlib.sha256(reference_raw).hexdigest(),
        'execution_summary_sha256':summary_sha,'launcher_exit_sha256':terminal_sha,
        'execution_files_sha256':canonical_sha256(manifest),'execution_completed':summary['execution_completed'],
        'rubric':RUBRIC.copy(),'pass_rule':references['pass_rule'],'ratings':rows,
        'excluded_cases':[c for c in plan['prepared']['cases'] if all(o['action']=='do_not_send' for o in c['observations'])],
        'ledger_correlation_basis':'host_reported_drained_audit','ledger_independently_rechecked':False,
        'runtime_terminal_independently_verified':False,'request_authorized':False}


def evaluate_bound_ratings(corpus_raw, reference_raw, plan_raw, receipt_directory, ratings_raw):
    expected = evaluation_template(corpus_raw,reference_raw,plan_raw,receipt_directory)
    supplied = strict_json(ratings_raw)
    require(type(supplied) is dict and set(supplied)==set(expected),'quality_rating_fields_invalid')
    require(canonical_sha256({k:v for k,v in supplied.items() if k!='ratings'})
        ==canonical_sha256({k:v for k,v in expected.items() if k!='ratings'}),'quality_rating_evidence_changed')
    references,_ = _signoffs(corpus_raw,reference_raw)
    reference_map = {r['case_id']:r for r in references['cases']}
    expected_rows = {r['id']:r for r in expected['ratings']}
    require(type(supplied['ratings']) is list and len(supplied['ratings'])==len(expected_rows),'quality_rating_inventory_invalid')
    cases,ratings,seen = [],[],set()
    for rating in supplied['ratings']:
        require(type(rating) is dict and rating.get('id') in expected_rows and rating['id'] not in seen,
                'quality_rating_identity_invalid')
        wanted = expected_rows[rating['id']]
        require(set(rating)==set(wanted) and canonical_sha256({k:v for k,v in rating.items() if k not in _EDITABLE})
            ==canonical_sha256({k:v for k,v in wanted.items() if k not in _EDITABLE}),'quality_rating_output_binding_changed')
        seen.add(rating['id'])
        reference = reference_map[rating['case_id']]
        cases.append({'id':rating['id'],'reference_draft':{**copy.deepcopy(reference['reference']),
            'human_reviewer':reference['reviewer'],'human_approved_at':reference['reviewed_at']}})
        if all(rating[k] is None for k in _EDITABLE):
            continue
        require(rating['rating_eligible'],'quality_rating_without_verified_output')
        require(type(rating['reviewer']) is str and 0<len(rating['reviewer'].strip())<=200
            and type(rating['reviewed_at']) is str,'quality_human_attribution_missing')
        try:
            when = datetime.fromisoformat(rating['reviewed_at'].replace('Z','+00:00'))
        except ValueError as exc:
            raise ValueError('quality_rating_time_invalid') from exc
        require(when.tzinfo is not None,'quality_rating_time_invalid')
        require(type(rating['scores']) is dict and type(rating['critical_failures']) is list
            and len(rating['critical_failures'])<=20 and all(type(v) is str and 0<len(v.strip())<=2000
                for v in rating['critical_failures']),'quality_rating_content_invalid')
        ratings.append({k:copy.deepcopy(rating[k]) for k in ('id','reviewer','reviewed_at','scores','critical_failures')})
    scored = score_quality(cases,ratings)
    human_gate = scored['quality_gate_passed']
    return {**scored,'version':'news_quality_bound_evaluation_v1',
        'plan_sha256':expected['plan_sha256'],'ratings_sha256':hashlib.sha256(ratings_raw).hexdigest(),
        'execution_files_sha256':expected['execution_files_sha256'],'scored_requests':len(ratings),
        'expected_requests':len(expected_rows),'logical_cases':36,'model_eligible_cases':34,
        'execution_completed':expected['execution_completed'],'human_rating_gate_passed':human_gate,
        'quality_gate_passed':(bool(human_gate and expected['execution_completed']
            and all(r['output_contract_valid'] is True for r in expected_rows.values())) if human_gate is not None else None),
        'acceptance_scope':'declared_human_ratings_on_bound_synthetic_outputs',
        'provider_output_independently_verified':False,'runtime_terminal_independently_verified':False,
        'ledger_independently_rechecked':False,'supplier_bill_verified':False,'request_authorized':False}
