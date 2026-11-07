"""Portable dependency-closure fixtures; real MU binding is replayed offline separately."""
import copy
import socket
import unittest
from unittest.mock import patch

from backend.news_article_evidence import extract_article, evidence_package
from backend.news_evidence_reading import build_input, locate_result
from backend.news_evidence_groups import encoded_size, decoded_cells
from backend import news_verified_table_groups as profiles
from backend import news_market_context as market

URL='https://investors.micron.com/fixture.htm'
NOW=1793232000
ASOF='2026-10-29T12:00:00Z'
HTML=b'''<article><p>The following reported balances are unaudited.</p>
<table><tr><th colspan="3">Amounts in millions; source symbol $</th></tr>
<tr><td>Reported measure (1)</td><td colspan="2">As of May 28, 2026</td></tr>
<tr><td>Cash</td><td>$</td><td>10</td></tr>
<tr><td>Assets</td><td>$</td><td>25</td></tr></table>
<p>(1) Values exclude the stated discontinued activity.</p><p>Other context not reviewed for small-group use.</p></article>'''


def fixture():
    value={'id':'fixture','title':'Local fixture','url':URL,'source_name':'Offline fixture','excerpt':'Unreviewed excerpt.',
        'published_at':'2026-10-28T00:00:00Z','discovered_at':'2026-10-29T00:00:00Z',
        'version_seen_at':'2026-10-29T00:00:00Z','current_version_id':'fixed-version',
        'evidence':extract_article(HTML,URL,'ir_mu',now=NOW)}
    rows=value['evidence']['segments']
    dependencies=[rows[i]['id'] for i in (0,1,2,5)]
    groups=[]
    for name,index in [('cash',3),('assets',4)]:
        target=rows[index]['id']
        groups.append({'name':name,'scope':'balance_as_of','target_ids':[target],
                       'dependency_ids':dependencies,'member_ids':dependencies+[target]})
    return value,{'id':'trusted-local-fixture','basis':'unit_test_only','currency_symbol':'$','currency_iso':None,
                  'scope_limit':'Only selected fixture lines.', 'groups':groups}


class VerifiedContextTests(unittest.TestCase):
    def setUp(self):
        guard=patch.object(socket.socket,'connect',side_effect=AssertionError('offline_only'))
        guard.start();self.addCleanup(guard.stop)

    def reading(self,value,**kwargs):
        return build_input(value,checked_at=NOW,text_budget=6500,**kwargs)

    def assert_closed(self,package,rows):
        selected={r['id'] for r in rows}
        covered=set()
        for group in package['groups']:
            required=set(group['required_segment_ids'])
            self.assertTrue(required.issubset(selected))
            self.assertTrue(set(group.get('targets',[])).issubset(required))
            self.assertTrue(set(group.get('dependencies',[])).issubset(required))
            covered.update(required)
        self.assertEqual(covered,selected)

    def test_shared_dependencies_are_deduplicated_with_lossless_cells_and_locations(self):
        value,profile=fixture();before=copy.deepcopy(value)
        with patch.object(profiles,'resolve',return_value=profile):result=self.reading(value)
        package=result['evidence_package'];rows=package['selection'];by_id={r['source_segment_id']:r for r in rows}
        self.assertEqual(len(package['groups']),2)
        self.assertEqual(len(rows),6)
        self.assertEqual(len({r['id'] for r in rows}),6)
        self.assert_closed(package,rows)
        for original in value['evidence']['tables']:
            self.assertEqual(decoded_cells(by_id[original['id']]),original['cells'])
            self.assertEqual(by_id[original['id']]['row_index'],original['row_index'])
        self.assertNotIn('Other context',result['source_text']['body'])
        self.assertEqual(package['omitted_group_reasons'],{'dependencies_unverified':1})
        self.assertEqual(package['omitted_segments'],1)
        self.assertIsNone(package['verified_profiles'][0]['currency_iso'])
        self.assertEqual(value,before)
        located=locate_result({'evidence':[{'evidence_field':'body','quote':'Amounts in millions; source symbol $'}]},result)
        self.assertEqual(len(located['evidence'][0]['locator']['group_ids']),2)
        self.assertEqual(located['evidence'][0]['locator']['url'],URL)

    def test_budget_sweep_never_treats_shared_headers_as_a_completed_second_group(self):
        value,profile=fixture()
        with patch.object(profiles,'resolve',return_value=profile):
            for cap in range(1800,10001,137):
                try:result=self.reading(value,max_package_bytes=cap)
                except ValueError as error:
                    self.assertIn('smaller_than_metadata',str(error));continue
                self.assertLessEqual(encoded_size(result),cap)
                self.assert_closed(result['evidence_package'],result['evidence_package']['selection'])
                for group in result['evidence_package']['groups']:
                    self.assertTrue(group['targets'])

    def test_generic_and_market_second_selector_preserve_each_dependency_closure(self):
        value,profile=fixture()
        with patch.object(profiles,'resolve',return_value=profile):
            generic=evidence_package(value,max_bytes=3500)
            self.assertTrue(generic['groups']);self.assert_closed(generic,generic['segments'])
            context=market.resolve_saved_context(None,value,as_of=ASOF)
            for cap in (2500,3500,5000,8500):
                with patch.object(market,'MAX_MARKET_INPUT_BYTES',cap):
                    result=market.build_market_input(value,context,ASOF)
                selected={row['id'] for row in result['evidence']}
                covered=set()
                for group in result['selected_article_groups']:
                    self.assertTrue(set(group['required_evidence_ids']).issubset(selected))
                    covered.update(group['required_evidence_ids'])
                self.assertEqual(covered,{r['id'] for r in result['evidence'] if r['type']=='article'})
                self.assertLessEqual(encoded_size(result),cap)
                for row in result['evidence']:
                    if row['type']=='article':self.assertEqual(row['url'],URL)

    def test_missing_dependency_cannot_be_accepted_as_a_small_group(self):
        value,profile=fixture()
        profile['groups'][0]['dependency_ids'].append('missing-footnote')
        with patch.object(profiles,'resolve',return_value=profile):result=self.reading(value)
        self.assertFalse(result['evidence_package']['verified_profiles'])
        self.assertTrue(all(not g.get('profile_id') for g in result['evidence_package']['groups']))

    def test_profile_claim_in_source_data_is_not_authorization(self):
        value,_=fixture();evidence=value['evidence']
        evidence.update(raw_content_hash=profiles.RAW_SHA256,content_hash=profiles.BODY_SHA256,
                        verified_profile=profiles.PROFILE_ID,url=profiles.SOURCE_URL)
        self.assertIsNone(profiles.resolve(evidence,profiles.SOURCE_URL))

    def test_read_attachments_keep_separate_document_and_segment_namespaces(self):
        value,profile=fixture();evidence=copy.deepcopy(value['evidence'])
        value['evidence']['attachments']=[{'url':URL+'/one','read':True,'evidence':evidence},
                                         {'url':URL+'/unread','read':False,'evidence':evidence}]
        with patch.object(profiles,'resolve',return_value=profile):
            result=build_input(value,checked_at=NOW,text_budget=100000,max_package_bytes=100000)
        package=result['evidence_package'];rows=package['selection']
        self.assertEqual(len(rows),12);self.assertEqual(len({r['id'] for r in rows}),12)
        self.assertEqual(len({g['document_id'] for g in package['groups']}),2)
        self.assertEqual({g['url'] for g in package['groups']},{URL,URL+'/one'})
        self.assert_closed(package,rows)


if __name__=='__main__':unittest.main()
