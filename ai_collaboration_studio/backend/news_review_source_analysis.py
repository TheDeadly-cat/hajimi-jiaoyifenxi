"""Pure safe-metadata diagnostics, distinct from source capture or acceptance."""
from collections import Counter


def error_stage(code):
    if code in {'SEC_SUBMISSIONS_ERROR','SEC_TICKER_MAP_ERROR','IR_FEED_ERROR'}:
        return 'list_or_transport_unclassified'
    if code == 'MICRON_IR_METADATA_REQUEST_FAILED':
        return 'metadata_transport_unclassified'
    if code in {'MICRON_IR_REVALIDATION_TIMEOUT','MICRON_IR_METADATA_REVALIDATION_PENDING',
                'MICRON_IR_METADATA_RETRY_PENDING','MICRON_IR_REVALIDATION_DEFERRED'}:
        return 'metadata_revalidation'
    if code == 'MICRON_IR_METADATA_CACHE_EXPIRED':
        return 'cache_age_or_integrity'
    if code in {'MICRON_IR_TIME_METADATA_INVALID','MICRON_IR_JSON_INVALID','MICRON_IR_CACHE_INVALID'}:
        return 'metadata_parse_or_integrity'
    if code == 'SOURCE_MONITORING_POLL_DEADLINE_EXCEEDED':
        return 'whole_poll_deadline'
    return 'unclassified'


def analyze_source_runs(runs, *, observed_until_ms, maximum_success_gap_ms=None):
    if type(observed_until_ms) is not int or observed_until_ms < 0:
        raise ValueError('invalid_observation_time')
    if maximum_success_gap_ms is not None and (type(maximum_success_gap_ms) is not int or maximum_success_gap_ms <= 0):
        raise ValueError('invalid_gap_standard')
    ordered=sorted(runs,key=lambda r:(r['started_at_ms'],r['run_id']))
    if len({r['run_id'] for r in ordered}) != len(ordered):
        raise ValueError('duplicate_run_id')
    completed=[]
    for r in ordered:
        if r['status'] not in {'SUCCEEDED','DEGRADED','FAILED','CANCELLED','RUNNING'}:
            raise ValueError('unknown_run_status')
        if any(type(r[k]) is not int or r[k]<0 for k in ('started_at_ms','completed_at_ms','observed_count','accepted_count','duplicate_count')):
            raise ValueError('invalid_run_counters')
        if r['status']=='RUNNING':
            continue
        if not r['started_at_ms']<=r['completed_at_ms']<=observed_until_ms:
            raise ValueError('run_outside_observation')
        completed.append(r)
    success=sorted(r['completed_at_ms'] for r in completed if r['status']=='SUCCEEDED')
    gaps=[{'from_ms':a,'to_ms':b,'gap_ms':b-a} for a,b in zip(success,success[1:])]
    last_success=success[-1] if success else None
    errors=Counter(r['error_code'] for r in completed if r['error_code'])
    statuses=Counter(r['status'] for r in completed)
    longest=max(gaps,key=lambda r:r['gap_ms'],default=None)
    age=observed_until_ms-last_success if last_success is not None else None
    return {'version':'news_review_source_diagnostics_v1','polls_observed':len(ordered),
        'completed_poll_denominator':len(completed),'in_flight_excluded':len(ordered)-len(completed),
        'statuses':dict(statuses),'full_successes':len(success),
        'full_success_rate':len(success)/len(completed) if completed else None,
        'denominator_basis':'completed_adapter_polls_not_http_requests_or_announcement_capture',
        'last_full_success_at_ms':last_success,'full_success_age_ms':age,
        'maximum_interval_between_full_successes':longest,
        'full_success_interval_count':len(gaps),
        'errors_by_code':dict(errors),'errors_by_stage':{stage:sum(n for code,n in errors.items() if error_stage(code)==stage) for stage in sorted({error_stage(c) for c in errors})},
        'degraded_polls_with_validated_rows':sum(r['status']=='DEGRADED' and r['observed_count']>0 for r in completed),
        'gap_standard_ms':maximum_success_gap_ms,
        'gap_standard_evaluation':('not_configured' if maximum_success_gap_ms is None else 'no_full_success_sample' if age is None
            else 'exceeded' if age>maximum_success_gap_ms or longest and longest['gap_ms']>maximum_success_gap_ms else 'within_observed_limits'),
        'actual_http_requests':None,'capture_completeness_verified':False,'acceptance_passed':False}
