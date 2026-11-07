"""No provider calls: complete table context survives both package selectors."""
import copy
import json
import socket
import unittest
from unittest.mock import patch

from backend.news_article_evidence import extract_article, evidence_package
from backend.news_evidence_reading import build_input, locate_result
from backend.news_evidence_groups import encoded_size
from backend import news_market_context as market

URL = 'https://investors.micron.com/q.htm'
NOW = 1793232000
ASOF = '2026-10-29T12:00:00Z'
HTML = b'''<article><p>FY2026 Q3 results, reported in USD millions, with the conditions below.</p>
<table><tr><th colspan="2">GAAP Q3 2026</th><th>Non-GAAP Q3 2026</th></tr>
<tr><td>Revenue (1)</td><td>10</td><td>11</td></tr></table>
<p>Additional explanation separates the first table from its continued data.</p>
<table><tr><td colspan="2">Continued</td></tr><tr><td>Expense</td><td>3</td></tr></table>
<p>(1) The revenue excludes the stated discontinued activity; the preceding units also apply to the continued table.</p></article>'''
SMALL = b'<article><p>Q3 2026, USD million, unaudited. See the complete table.</p><table><tr><th>Revenue</th><th>Q3</th></tr><tr><td>Actual</td><td>10</td></tr></table></article>'


def item(raw=HTML):
    return {'id':'offline', 'title':'Quarterly results', 'url':URL, 'source_name':'Micron',
            'excerpt':'Excerpt must not replace an omitted table.', 'symbols':['US.MU'],
            'published_at':'2026-10-28T00:00:00Z','discovered_at':'2026-10-29T00:00:00Z',
            'version_seen_at':'2026-10-29T00:00:00Z','current_version_id':'immutable-version',
            'evidence':extract_article(raw,URL,'ir_mu',now=NOW)}


class EvidenceGroupTests(unittest.TestCase):
    def setUp(self):
        guard=patch.object(socket.socket,'connect',side_effect=AssertionError('network_forbidden'))
        guard.start();self.addCleanup(guard.stop)

    def reading(self, value, **kwargs):
        return build_input(value,checked_at=NOW,**kwargs)

    def test_complete_document_retains_remote_footnotes_units_periods_and_cell_geometry(self):
        value=item(); before=copy.deepcopy(value)
        result=self.reading(value,text_budget=100000,max_package_bytes=100000)
        self.assertEqual(result['source_text']['body'],value['evidence']['body_text'])
        self.assertIn('USD millions',result['source_text']['body'])
        self.assertIn('excludes the stated discontinued activity',result['source_text']['body'])
        package=result['evidence_package'];self.assertEqual(len(package['groups']),1)
        self.assertEqual(package['groups'][0]['kind'],'table_context')
        self.assertEqual(len(package['selection']),len(value['evidence']['segments']))
        selected={row['source_segment_id']:row for row in package['selection']}
        for row in value['evidence']['tables']:
            self.assertEqual(selected[row['id']]['cells'],row['cells'])
            self.assertEqual(selected[row['id']]['table_id'],row['table_id'])
        self.assertEqual(value,before)

    def test_full_serialized_budget_edge_is_all_or_none(self):
        value=item();full=self.reading(value,text_budget=100000,max_package_bytes=100000)
        size=encoded_size(full)
        exact=self.reading(value,text_budget=100000,max_package_bytes=size)
        short=self.reading(value,text_budget=100000,max_package_bytes=size-1)
        self.assertEqual(exact,full)
        self.assertEqual(short['source_text']['body'],'')
        self.assertEqual(short['evidence_package']['selection'],[])
        self.assertEqual(short['evidence_package']['omitted_table_groups'],1)
        self.assertLessEqual(encoded_size(short),size-1)

    def test_text_budget_edge_never_keeps_just_headers_or_just_values(self):
        value=item();size=len(value['evidence']['body_text'].encode('utf-8'))
        self.assertEqual(self.reading(value,text_budget=size,max_package_bytes=100000)['source_text']['body'],value['evidence']['body_text'])
        for cap in (0,1,size-1):
            result=self.reading(value,text_budget=cap,max_package_bytes=100000)
            self.assertEqual(result['evidence_package']['selection'],[])
            self.assertEqual(result['evidence_package']['omitted_group_reasons'],{'budget':1})

    def test_generic_package_has_the_same_atomicity_and_json_cap(self):
        value=item(SMALL);full=evidence_package(value,max_bytes=12000)
        self.assertTrue(full['segments'])
        size=encoded_size(full)
        self.assertEqual(evidence_package(value,max_bytes=size),full)
        omitted=evidence_package(value,max_bytes=size-1)
        self.assertEqual(omitted['segments'],[])
        self.assertEqual(omitted['omitted_table_groups'],1)
        self.assertLessEqual(encoded_size(omitted),size-1)
        with self.assertRaisesRegex(ValueError,'smaller_than_metadata'):evidence_package(value,max_bytes=1)

    def test_flat_legacy_missing_rows_partial_and_conflicting_tables_are_omitted(self):
        variants=[]
        flat=item()
        for key in ('segments','tables'):
            for row in flat['evidence'][key]:row.pop('cells',None)
        variants.append(flat)
        no_rows=item();no_rows['evidence']['segments']=[];variants.append(no_rows)
        partial=item();partial['evidence']['truncated']=True;variants.append(partial)
        missing_context=item();missing_context['evidence']['segments'].pop(0);variants.append(missing_context)
        conflicting=json.loads(json.dumps(item()));conflicting['evidence']['tables'][0]['cells'][0]['colspan']=8;variants.append(conflicting)
        missing_id=item();missing_id['evidence']['segments'][0].pop('id');variants.append(missing_id)
        no_cells=item();no_cells['evidence']['tables'][0]['cells']=[];variants.append(no_cells)
        wrong_cells=item();wrong_cells['evidence']['tables'][0]['cells'][0]['text']='Wrong header';variants.append(wrong_cells)
        for value in variants:
            result=self.reading(value,text_budget=100000,max_package_bytes=100000)
            self.assertEqual(result['source_text']['body'],'')
            self.assertEqual(result['evidence_package']['omitted_group_reasons'],{'table_context_unverified':1})

    def test_attachment_table_names_and_urls_do_not_merge_or_include_unread_material(self):
        value=item(SMALL);attachment=copy.deepcopy(value['evidence'])
        value['evidence']['attachments']=[{'url':URL+'/one.htm','read':True,'evidence':attachment},
            {'url':URL+'/two.htm','read':True,'evidence':attachment},
            {'url':URL+'/unread.htm','read':False,'evidence':attachment}]
        result=self.reading(value,text_budget=100000,max_package_bytes=100000)
        selection=result['evidence_package']['selection'];groups=result['evidence_package']['groups']
        self.assertEqual(len(groups),3)
        self.assertEqual(len({g['document_id'] for g in groups}),3)
        self.assertEqual({s['url'] for s in selection},{URL,URL+'/one.htm',URL+'/two.htm'})
        self.assertEqual(len({s['id'] for s in selection}),len(selection))

    def test_quote_locator_remains_bound_to_the_original_segment_and_group(self):
        value=item();result=self.reading(value,text_budget=100000,max_package_bytes=100000)
        quote='Revenue (1) | 10 | 11'
        located=locate_result({'evidence':[{'evidence_field':'body','quote':quote}]},result)
        locator=located['evidence'][0]['locator']
        original=next(r for r in value['evidence']['segments'] if r['id']==locator['source_segment_id'])
        self.assertEqual(original['text'][locator['start']:locator['end']],quote)
        self.assertEqual(locator['group_id'],result['evidence_package']['groups'][0]['id'])

    def test_market_second_budget_does_not_split_groups_or_refill_rows(self):
        value=item(SMALL);context=market.resolve_saved_context(None,value,as_of=ASOF)
        full=market.build_market_input(value,context,ASOF)
        self.assertTrue(full['selected_article_groups'])
        size=encoded_size(full)
        with patch.object(market,'MAX_MARKET_INPUT_BYTES',size-1):
            short=market.build_market_input(value,context,ASOF)
        self.assertFalse(any(r['type']=='article' for r in short['evidence']))
        self.assertTrue(short['omitted_article_groups'])
        self.assertLessEqual(encoded_size(short),size-1)

    def test_market_does_not_replace_omitted_financial_group_with_excerpt(self):
        value=item(HTML.replace(b'Continued',b'Continued '+b'explanation '*1000))
        context=market.resolve_saved_context(None,value,as_of=ASOF)
        result=market.build_market_input(value,context,ASOF)
        self.assertFalse(any(r['type'] in {'article','article_excerpt'} for r in result['evidence']))
        self.assertEqual(result['source_table_groups_omitted'],1)

    def test_plain_text_paragraph_is_not_cut_mid_condition(self):
        value=item(b'<article><p>'+b'Condition applies. '*100+b'</p></article>')
        result=self.reading(value,text_budget=100)
        self.assertEqual(result['source_text']['body'],'')
        self.assertTrue(result['evidence_package']['truncated'])

    def test_omitted_table_cannot_reenter_through_a_flattened_rss_excerpt(self):
        value=item();value['excerpt']='Revenue (1) | 10 | 11'
        result=self.reading(value,text_budget=1)
        self.assertEqual(result['source_text']['body'],'')
        self.assertEqual(result['source_text']['excerpt'],'')
        self.assertFalse(result['scope']['has_excerpt'])
        self.assertTrue(result['evidence_package']['excerpt_omitted_for_table_context'])


if __name__=='__main__':unittest.main()
