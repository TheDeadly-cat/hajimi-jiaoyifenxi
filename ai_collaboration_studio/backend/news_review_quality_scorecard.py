"""Aggregate explicitly supplied human ratings, never invent semantic scores."""
RUBRIC={'importance':2,'numbers_units_periods':3,'key_fact_coverage':2,'uncertainty_and_scope':2,'freshness':1}


def score_quality(cases, ratings):
    ids={case['id'] for case in cases}
    if len(ids)!=len(cases) or len({r['id'] for r in ratings})!=len(ratings) or any(r['id'] not in ids for r in ratings):
        raise ValueError('quality_case_identity_invalid')
    indexed={r['id']:r for r in ratings}
    totals=[]
    for case in cases:
        rating=indexed.get(case['id'])
        approved=case['reference_draft'].get('human_approved_at')
        reviewer=case['reference_draft'].get('human_reviewer')
        if not rating or not approved or not reviewer:
            totals.append({'id':case['id'],'score':None,'passed':None,'status':'human_signoff_or_rating_pending'})
            continue
        if not rating.get('reviewer') or not rating.get('reviewed_at'):
            raise ValueError('human_rating_attribution_missing')
        scores=rating['scores']
        if set(scores)!=set(RUBRIC) or any(type(scores[k]) is not int or not 0<=scores[k]<=maximum for k,maximum in RUBRIC.items()):
            raise ValueError('invalid_rubric_scores')
        critical=rating['critical_failures']
        if type(critical) is not list or any(type(c) is not str or not c.strip() for c in critical):
            raise ValueError('invalid_critical_failures')
        total=sum(scores.values())
        totals.append({'id':case['id'],'score':total,'passed':total>=9 and not critical,'status':'human_rated',
            'reviewer':rating['reviewer'],'reviewed_at':rating['reviewed_at'],'critical_failures':critical})
    complete=bool(totals) and all(r['score'] is not None for r in totals)
    return {'rubric':RUBRIC.copy(),'cases':totals,'mean_score':sum(r['score'] for r in totals)/len(totals) if complete else None,
        'quality_gate_passed':all(r['passed'] for r in totals) if complete else None,
        'rating_authenticity_independently_verified':False,'natural_event_acceptance':False,'release_approved':False}
