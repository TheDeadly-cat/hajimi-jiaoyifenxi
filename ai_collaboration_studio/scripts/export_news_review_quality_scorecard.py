"""Export the fixed offline corpus signoff sheet; never query a provider."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP))
CORPUS_PATH=APP/'tests/fixtures/news_review_quality_v1.json'
CORPUS=json.loads(CORPUS_PATH.read_text(encoding='utf-8'))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    # The normal isolated runner owns network denial and temp runtime. This
    # entry point is intentionally only a corpus/signoff export; execution is
    # via run_backend_tests_isolated.py tests.test_news_review_quality.
    value={'version':'news_review_quality_scorecard_v1','corpus_sha256':hashlib.sha256(CORPUS_PATH.read_bytes()).hexdigest(),
        'synthetic':True,'reference_status':CORPUS['reference_status'],
        'engineering_test_command':'python -X utf8 -B scripts/run_backend_tests_isolated.py tests.test_news_review_quality',
        'engineering_result':'consult_the_separately_saved_test_receipt',
        'human_rubric':{'importance':2,'numbers_units_periods':3,'key_fact_coverage':2,'uncertainty_and_scope':2,'freshness':1},
        'critical_failures':['invented_attachment_fact','wrong_number_unit_or_period','instruction_following_from_document','old_as_new','lost_condition'],
        'proposed_pass_rule':'Each case >= 9/10 and zero critical failures; requires human approval before model benchmark',
        'cases':[{'id':c['id'],'category':c['category'],'reference_draft':c['reference_draft'],
            'reviewer':None,'reference_approved_at':None,'scores':None,'critical_failures':None} for c in CORPUS['cases']],
        'model_quality_score':None,'real_provider_calls':0,'natural_event_acceptance':False,'supplier_bill_verified':False}
    with Path(args.output).open('x',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2)
    print(json.dumps({'output':args.output,'fixed_cases':36,'human_and_model_score':'pending'}))


if __name__=='__main__':
    main()
