"""Aggregate human ratings against an explicitly signed offline corpus copy."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.news_review_quality_scorecard import score_quality


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--approved-corpus',required=True)
    parser.add_argument('--ratings',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    corpus_raw=Path(args.approved_corpus).read_bytes()
    ratings_raw=Path(args.ratings).read_bytes()
    result=score_quality(json.loads(corpus_raw)['cases'],json.loads(ratings_raw)['ratings'])
    result.update(corpus_sha256=hashlib.sha256(corpus_raw).hexdigest(),ratings_sha256=hashlib.sha256(ratings_raw).hexdigest(),
        network_requests=0,provider_output_independently_verified=False,supplier_bill_verified=False)
    with Path(args.output).open('x',encoding='utf-8') as f:
        json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'output':args.output,'mean_score':result['mean_score'],'quality_gate_passed':result['quality_gate_passed']}))


if __name__=='__main__':
    main()
