"""Locally reviewed dependencies for one immutable saved official release.

This is a source-specific allowlist, not a heuristic for future Micron pages.
Neither caller-supplied profile metadata nor a claimed raw hash is sufficient.
"""
import hashlib
import json

PROFILE_ID = 'mu-fy2026-q3-reported-lines-v1'
SOURCE_URL = 'https://investors.micron.com/news/press-release/2026/Micron-Technology-Inc--Reports-Record-Results-for-the-Third-Quarter-of-Fiscal-2026/default.aspx'
RAW_SHA256 = 'ca82922d9a4ac60436ea272972ac0777850f2d643fffb12594cc9d9062faaa82'
BODY_SHA256 = '6cd98bac14427b10868d54373d62d2d10c15e8abe1e92527cb148e791ebfb7d4'
SEGMENTS_SHA256 = 'f9175f96be9b873dbb40d9ae1eab1042994a5aa60d8c49a45cac78fcfe0bea2a'

# Complete reported lines, with explicit source dependencies. No ratios, sums,
# implied ISO currency, or unselected non-GAAP/forecast values are introduced.
GROUPS = (
    ('gaap_net_income', 'quarterly_actual', [205], [2, 72, 186, 188, 189]),
    ('gaap_diluted_eps', 'quarterly_actual_per_share', [216], [2, 72, 186, 188, 189]),
    ('gaap_operating_cashflow', 'quarterly_actual', [224], [2, 72, 186, 219, 221, 222]),
    ('cash_and_equivalents', 'balance_as_of', [109], [104, 106, 108]),
    ('total_assets', 'balance_as_of', [122], [104, 106, 108]),
    ('closing_cash_including_restricted', 'nine_months_period_end_balance', [185], [146, 148]),
    ('operating_cashflow_nine_months', 'nine_months_actual', [162], [146, 148, 150, 151]),
)


def resolve(evidence, url):
    if (url != SOURCE_URL or evidence.get('url') != SOURCE_URL or evidence.get('raw_content_hash') != RAW_SHA256
            or evidence.get('extractor_version') != 'official-html-segments-v2'):
        return None
    body = evidence.get('body_text')
    if not isinstance(body, str) or hashlib.sha256(body.encode('utf-8')).hexdigest() != BODY_SHA256:
        return None
    rows = evidence.get('segments') or []
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if hashlib.sha256(encoded).hexdigest() != SEGMENTS_SHA256:
        return None
    by_number = {int(row['id'].rsplit('-', 1)[-1]): row['id'] for row in rows}
    result = []
    for name, scope, targets, dependencies in GROUPS:
        result.append({'name': name, 'scope': scope,
                       'target_ids': [by_number[number] for number in targets],
                       'dependency_ids': [by_number[number] for number in dependencies],
                       'member_ids': [by_number[number] for number in sorted(targets + dependencies)]})
    return {'id': PROFILE_ID, 'groups': result,
            'basis': 'offline_review_of_exact_saved_source_and_full_parsed_structure',
            'currency_symbol': '$', 'currency_iso': None,
            'scope_limit': 'Only selected reported lines; omitted details, non-GAAP adjustments and forecasts are not inferred.'}
