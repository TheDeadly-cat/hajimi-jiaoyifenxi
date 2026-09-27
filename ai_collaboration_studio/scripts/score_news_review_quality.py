"""Aggregate human ratings against an explicitly signed offline corpus copy."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.news_review_quality_scorecard import score_quality


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--approved-corpus')
    source.add_argument('--execution-plan')
    parser.add_argument('--ratings')
    parser.add_argument('--references')
    parser.add_argument('--receipts')
    parser.add_argument('--export-template',action='store_true')
    parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    if args.execution_plan:
        if (not args.references or not args.receipts
                or args.export_template == bool(args.ratings)):
            parser.error('execution evidence requires references, receipts and either a template export or ratings')
        return _bound(args,parser)
    if not args.ratings or args.references or args.receipts or args.export_template:
        parser.error('legacy corpus scoring requires ratings without execution options')
    corpus_raw=Path(args.approved_corpus).read_bytes()
    ratings_raw=Path(args.ratings).read_bytes()
    result=score_quality(json.loads(corpus_raw)['cases'],json.loads(ratings_raw)['ratings'])
    result.update(corpus_sha256=hashlib.sha256(corpus_raw).hexdigest(),ratings_sha256=hashlib.sha256(ratings_raw).hexdigest(),
        network_requests=0,provider_output_independently_verified=False,supplier_bill_verified=False)
    with Path(args.output).open('x',encoding='utf-8') as f:
        json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps({'output':args.output,'mean_score':result['mean_score'],'quality_gate_passed':result['quality_gate_passed']}))
    return 0


def _bound(args,parser):
    app=Path(__file__).resolve().parents[1]
    from backend.path_identity import first_reparse_component
    output=Path(args.output)
    if (not output.is_absolute() or not output.parent.is_dir() or output.exists()
            or output.resolve().is_relative_to(app.parent) or first_reparse_component(output) is not None):
        parser.error('output must be a new absolute file outside source without reparse components')
    os.environ['AI_STUDIO_SKIP_LOCAL_ENV']='1'
    for key in ('OPENAI_API_KEY','ARK_API_KEY','DOUBAO_API_KEY','DEEPSEEK_API_KEY','QWEN_API_KEY',
                'DASHSCOPE_API_KEY','GLM_API_KEY','ZHIPU_API_KEY','ZHIPUAI_API_KEY','BAILIAN_TOKEN_PLAN_API_KEY'):
        os.environ.pop(key,None)
    blocked=[]
    def deny(event,_args):
        if event.startswith('socket.') or event=='sqlite3.connect':
            blocked.append(event)
            raise RuntimeError('quality_scoring_runtime_access_forbidden')
    sys.addaudithook(deny)
    with tempfile.TemporaryDirectory(prefix='news-quality-score-') as temporary:
        os.environ['AI_STUDIO_RUNTIME_DIR']=temporary
        os.environ['AI_STUDIO_DATABASE_PATH']=str(Path(temporary)/'must-not-open.sqlite3')
        from backend.news_review_quality_evaluation import evaluation_template,evaluate_bound_ratings
        from backend.news_review_monitor import publish_json_once
        corpus=(app/'tests/fixtures/news_review_quality_v1.json').read_bytes()
        references=Path(args.references).read_bytes()
        plan=Path(args.execution_plan).read_bytes()
        inputs=(corpus,references,plan,Path(args.receipts))
        if args.export_template:
            value=evaluation_template(*inputs)
        else:
            value=evaluate_bound_ratings(*inputs,Path(args.ratings).read_bytes())
            value.update(grader_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                         network_requests=0,database_connections=0)
        if blocked:
            raise RuntimeError('quality_scoring_runtime_access_attempted')
        publish_json_once(output,value)
    print(json.dumps({'output':str(output),'scored_requests':value.get('scored_requests',0),
        'quality_gate_passed':value.get('quality_gate_passed'),
        'network_requests':0,'database_connections':0,'request_authorized':False}))
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        # A malformed supplied score must not echo model content or attribution.
        print(json.dumps({'ok':False,'code':'quality_scoring_failed','exception_type':type(exc).__name__}))
        raise SystemExit(1)
