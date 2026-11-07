"""Offline regressions for an IR page wrapped in an ASP.NET layout form."""
import json
import os
import socket
import unittest
from unittest.mock import patch

os.environ['AI_STUDIO_SKIP_LOCAL_ENV'] = '1'
from backend.news_article_evidence import extract_article, MAX_JSON_LD_CHARS

URL = 'https://investors.micron.com/news/press-release/2026/sample/default.aspx'
TEXT = 'Quarterly results are unaudited and subject to the conditions in this announcement.'


def article_metadata(**overrides):
    return {'@type': 'NewsArticle', 'mainEntityOfPage': {'@id': URL},
            'datePublished': '2026-06-24T15:01:00Z',
            'dateModified': '2026-06-24T16:02:00+00:00', **overrides}


def page(body=None, metadata=None, head=''):
    script = '' if metadata is None else '<script type="application/ld+json">' + json.dumps(metadata) + '</script>'
    return ('<html><head>' + head + script + '</head><body><div class="PageNewsPressrelease"><form id="fmForm1">'
            '<nav>Navigation excluded</nav><main id="main-content"><p>Outside body excluded</p>'
            '<div class="evergreen-news-body">' + (body or '<p>' + TEXT + '</p>') +
            '</div></main></form></div></body></html>').encode()


class SavedLayoutTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('offline_only'))
        guard.start()
        self.addCleanup(guard.stop)

    def extract(self, raw):
        return extract_article(raw, URL, 'ir_mu', now=1793232000)

    def test_layout_form_allows_body_but_hides_interactive_and_script_content(self):
        body = ('<p>' + TEXT + '</p><form role="search"><p>Secret search prompt</p></form>'
                '<form><article>Nested interactive form excluded</article></form>'
                '<textarea>Entered text excluded</textarea><select><option>Option excluded</option></select>'
                '<button>Button excluded</button><p hidden>Hidden excluded</p><p aria-hidden="true">Aria excluded</p>'
                '<script>Script excluded</script><p>End of the official announcement.</p>')
        value = self.extract(page(body))
        self.assertEqual(value['content_status'], 'full_text')
        self.assertIn(TEXT, value['body_text'])
        self.assertIn('End of the official announcement.', value['body_text'])
        self.assertNotIn('excluded', value['body_text'])
        self.assertNotIn('Secret', value['body_text'])
        self.assertEqual(value['extractor_version'], 'official-html-segments-v2')

    def test_search_or_login_form_without_article_is_not_body(self):
        for raw in [b'<form><p>Please login to read this announcement. This is a login page.</p></form>',
                    b'<form role="search"><article>Search results are not an official financial announcement.</article></form>',
                    b'<form hidden><article>Hidden content is not an official financial announcement.</article></form>']:
            with self.subTest(raw=raw):
                self.assertEqual(self.extract(raw)['content_status'], 'fetch_failed')

    def test_unclosed_or_invalidly_decoded_body_stays_partial(self):
        for raw in [page()[:page().index(b'</div>')], page().replace(b'</div>', b''), page().replace(TEXT.encode(), TEXT.encode()+b'\xff')]:
            with self.subTest(raw=raw[:50]):
                self.assertEqual(self.extract(raw)['content_status'], 'partial_text')

    def test_json_ld_dates_require_matching_article_and_keep_provenance(self):
        data = {'@graph': [{'@type': 'Organization', 'datePublished': '2000-01-01T00:00:00Z'},
                           article_metadata(mainEntityOfPage={'@id': URL+'/other'}), article_metadata()]}
        value = self.extract(page(metadata=data))
        self.assertEqual(value['published_at'], '2026-06-24T15:01:00Z')
        self.assertEqual(value['source_updated_at'], '2026-06-24T16:02:00Z')
        self.assertNotEqual(value['fetched_at'], value['published_at'])
        origin = value['source_time_evidence']['published_at']['sources'][0]
        self.assertEqual(origin['article_url'], URL)
        self.assertTrue(origin['path'].endswith('.@graph[2].datePublished'))

    def test_other_article_or_timezone_free_dates_are_unknown(self):
        for data in [article_metadata(mainEntityOfPage={'@id': URL+'/other'}),
                     article_metadata(datePublished='2026-06-24', dateModified='2026-06-24T15:01:00')]:
            value = self.extract(page(metadata=data))
            self.assertIsNone(value['published_at'])
            self.assertIsNone(value['source_updated_at'])

    def test_conflicting_article_nodes_or_meta_do_not_choose_a_time(self):
        for data, head in [([article_metadata(), article_metadata(datePublished='2026-06-25T15:01:00Z')], ''),
                           (article_metadata(), '<meta property="article:published_time" content="2026-06-25T15:01:00Z">')]:
            value = self.extract(page(metadata=data, head=head))
            self.assertIsNone(value['published_at'])
            self.assertEqual(value['source_time_evidence']['published_at']['status'], 'conflict')

    def test_invalid_or_oversize_json_is_inert_and_cannot_supply_dates(self):
        for data in ['{invalid json}', json.dumps(article_metadata(description='x' * MAX_JSON_LD_CHARS))]:
            value = self.extract(page(head='<script type="application/ld+json">'+data+'</script>'))
            self.assertEqual(value['content_status'], 'full_text')
            self.assertIsNone(value['published_at'])
            self.assertNotIn('invalid json', value['body_text'])

    def test_financial_table_preserves_groups_periods_units_cells_and_blank_rows(self):
        body = '<p>' + TEXT + '''</p><table>
<tr><td colspan="7">Quarterly Financial Results</td></tr>
<tr><th rowspan="2">USD millions</th><th colspan="3">GAAP</th><th colspan="3">Non-GAAP</th></tr>
<tr><th>Q3</th><th>Q2</th><th>Prior Q3</th><th>Q3</th><th>Q2</th><th>Prior Q3</th></tr>
<tr><td></td><td colspan="6"></td></tr>
<tr><td><p>Revenue</p><p>Unaudited</p></td><td>$</td><td>41,456</td><td>23,860</td><td>9,301</td><td>84.6</td><td>%</td></tr>
</table><p>Cash flows and footnotes remain part of the announcement.</p>'''
        value = self.extract(page(body))
        rows = value['tables']
        self.assertEqual(len(rows), 5)
        self.assertEqual({r['table_id'] for r in rows}, {'table-1'})
        self.assertEqual([r['row_index'] for r in rows], list(range(1, 6)))
        self.assertEqual(rows[1]['cells'][0]['rowspan'], 2)
        self.assertEqual(rows[1]['cells'][1]['colspan'], 3)
        self.assertEqual(rows[4]['cells'][0]['text'], 'Revenue Unaudited')
        self.assertEqual([c['text'] for c in rows[4]['cells'][1:]], ['$', '41,456', '23,860', '9,301', '84.6', '%'])
        self.assertEqual(value['content_status'], 'full_text')

    def test_invalid_span_is_partial_and_cannot_crash_the_reader(self):
        value = self.extract(page('<p>' + TEXT + '</p><table><tr><td colspan>Revenue</td><td>10</td></tr></table>'))
        self.assertEqual(value['content_status'], 'partial_text')
        self.assertIn('Revenue', value['body_text'])

    def test_hidden_nested_table_cannot_close_visible_cells_or_rows(self):
        body = ('<p>' + TEXT + '</p><table><tr><td>Visible before<div hidden>'
                '<table><tr><td>Hidden only</td></tr></table></div> visible after</td><td>100</td></tr></table>')
        value = self.extract(page(body))
        self.assertEqual(value['content_status'], 'full_text')
        self.assertEqual(len(value['tables']), 1)
        self.assertEqual([c['text'] for c in value['tables'][0]['cells']], ['Visible before visible after', '100'])
        self.assertNotIn('Hidden only', value['body_text'])

    def test_unclosed_table_does_not_claim_full_text(self):
        body = '<p>' + TEXT + '</p><table><tr><td>Revenue USD million</td><td>100</td></tr>'
        self.assertEqual(self.extract(page(body))['content_status'], 'partial_text')

    def test_conflicting_identity_fields_do_not_confirm_article_dates(self):
        value = self.extract(page(metadata=article_metadata(url=URL + '/other')))
        self.assertIsNone(value['published_at'])
        self.assertEqual(value['source_time_evidence']['published_at']['status'], 'identity_conflict')
        same_page = self.extract(page(metadata=article_metadata(url=URL, **{'@id': URL + '#article'})))
        self.assertEqual(same_page['published_at'], '2026-06-24T15:01:00Z')


if __name__ == '__main__':
    unittest.main()
