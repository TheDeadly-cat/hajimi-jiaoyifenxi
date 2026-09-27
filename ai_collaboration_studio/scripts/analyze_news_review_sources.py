"""Analyze an exported safe poll summary, with no database or network access."""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from backend.news_review_source_analysis import analyze_source_runs


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--standards',help='Adapter -> gap ms (legacy), or object with maximum_success_gap_ms and maximum_initial_success_delay_ms; this is not approval')
    args=parser.parse_args()
    source=Path(args.input)
    if source.stat().st_size>2_097_152:
        raise ValueError('safe_summary_too_large')
    raw=source.read_bytes()
    value=json.loads(raw)
    standards=json.loads(Path(args.standards).read_text(encoding='utf-8')) if args.standards else {}
    starts=value.get('observation_starts',{})
    if type(starts) is not dict or not set(starts)<=set(value['sources']):
        raise ValueError('invalid_observation_starts')
    diagnostics={}
    for key,rows in value['sources'].items():
        start=starts.get(key)
        if start is not None and (type(start) is not dict or set(start)!={'observed_since_ms','basis','evidence_sha256'}
                or type(start['basis']) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,119}',start['basis'])
                or type(start['evidence_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}',start['evidence_sha256'])):
            raise ValueError('observation_start_provenance_missing_or_invalid')
        standard=standards.get(key)
        if type(standard) is dict:
            if set(standard)-{'maximum_success_gap_ms','maximum_initial_success_delay_ms'}:
                raise ValueError('unknown_timing_standard')
            bounds=standard
        else:
            bounds={'maximum_success_gap_ms':standard}
        diagnostics[key]=analyze_source_runs(rows,observed_until_ms=value['observed_until_ms'],
            observed_since_ms=start['observed_since_ms'] if start else None,**bounds)
        diagnostics[key]['observation_start_evidence']=start
        diagnostics[key]['start_evidence_reference_independently_verified']=False
    result={'version':'news_review_source_analysis_report_v2','input_sha256':hashlib.sha256(raw).hexdigest(),
        'basis':value.get('basis','unverified_input'),'candidate_sha':value.get('candidate_sha'),
        'observed_until_ms':value['observed_until_ms'],
        'source_diagnostics':diagnostics,
        'standards_are_approval':False,'live_acceptance_proven':False,'network_requests':0,'database_connections':0}
    with Path(args.output).open('x',encoding='utf-8') as f:
        json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'output':args.output,'sources':list(result['source_diagnostics']),'live_acceptance_proven':False}))

if __name__=='__main__':
    main()
