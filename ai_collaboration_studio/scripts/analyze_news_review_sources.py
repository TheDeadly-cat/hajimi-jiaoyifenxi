"""Analyze an exported safe poll summary, with no database or network access."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
from backend.news_review_source_analysis import analyze_source_runs


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--standards',help='Optional predeclared JSON map of adapter keys to maximum full-success gaps in ms')
    args=parser.parse_args()
    source=Path(args.input)
    if source.stat().st_size>2_097_152:
        raise ValueError('safe_summary_too_large')
    raw=source.read_bytes()
    value=json.loads(raw)
    standards=json.loads(Path(args.standards).read_text(encoding='utf-8')) if args.standards else {}
    result={'version':'news_review_source_analysis_report_v1','input_sha256':hashlib.sha256(raw).hexdigest(),
        'basis':value.get('basis','unverified_input'),'candidate_sha':value.get('candidate_sha'),
        'observed_until_ms':value['observed_until_ms'],
        'source_diagnostics':{key:analyze_source_runs(rows,observed_until_ms=value['observed_until_ms'],
            maximum_success_gap_ms=standards.get(key)) for key,rows in value['sources'].items()},
        'standards_are_approval':False,'live_acceptance_proven':False,'network_requests':0,'database_connections':0}
    with Path(args.output).open('x',encoding='utf-8') as f:
        json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'output':args.output,'sources':list(result['source_diagnostics']),'live_acceptance_proven':False}))

if __name__=='__main__':
    main()
