import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from backend.news_micron_directory import (
    MICRON_DIRECTORY_URL, MAX_DIRECTORY_BYTES, MAX_JSON_CHARS,
    MicronDirectoryError, parse_micron_directory,
)


def directory_data():
    result = []
    for category in ('Earnings presentation', 'Prepared remarks', 'Press release', 'Form 10-Q / 10K'):
        yearly = []
        for year in range(2026, 2018, -1):
            documents = []
            for quarter in range(1, 5):
                if category == 'Press release':
                    link = f'https://investors.micron.com/news/press-release/{year}/Fiscal-{year}-Q{quarter}/default.aspx'
                    if year == 2026 and quarter == 4:
                        link = link.replace('https:', 'http:')
                else:
                    link = f'//s25.q4cdn.com/621799436/files/{year}/q{quarter}/sample.pdf'
                    if category == 'Form 10-Q / 10K' and year == 2026 and quarter == 4:
                        link = 'https://d18rn0p25nwr6d.cloudfront.net/CIK-0000723125/sample.pdf'
                documents.append({'text': f'Q{quarter}', 'link': link})
            yearly.append({'year': year, 'documents': documents})
        result.append({'category': category, 'data': yearly, 'tag': 'half-width'})
    return result


def page(data=None, *, value=None, before='', after=''):
    value = json.dumps(data if data is not None else directory_data()) if value is None else value
    return (f'<!doctype html><html><script>{before}\n'
            f"$tudio('#financial-table').q4FinancialTable({{documentCategoryData: {value}, modulePadding: true}});"
            f'{after}</script></html>').encode('utf-8')


class MicronDirectoryTests(unittest.TestCase):
    def assert_error(self, raw, code, **kwargs):
        with self.assertRaises(MicronDirectoryError) as caught:
            parse_micron_directory(raw, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_saved_layout_counts_provenance_unknown_times_and_unread_references(self):
        raw = page()
        with patch('urllib.request.urlopen', side_effect=AssertionError('network_forbidden')), \
                patch('builtins.eval', side_effect=AssertionError('script_execution_forbidden')):
            result = parse_micron_directory(raw, observed_at='2026-10-29T08:22:30+08:00')
        self.assertEqual(result['counts'], {'announcements_seen': 32, 'announcements_accepted': 31,
            'announcements_rejected': 1, 'attachment_references': 96, 'attachment_reads': 0})
        self.assertEqual(result['directory_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result['observed_at'], '2026-10-29T00:22:30Z')
        q3 = next(row for row in result['announcements'] if row['reference_period'] == 'FY2026-Q3')
        self.assertEqual(q3['url_json_path'], 'documentCategoryData[2].data[0].documents[2].link')
        self.assertEqual(q3['source_label'], 'Q3')
        self.assertEqual(q3['title_basis'], 'directory_label')
        self.assertEqual(q3['identity'], q3['url'])
        for row in result['announcements'] + result['rejected_announcements'] + result['attachments']:
            self.assertIsNone(row['published'])
            self.assertIsNone(row['source_updated_at'])
            self.assertEqual(row['published_time_source'], 'unknown_directory_listing')
            self.assertEqual(row['directory_sha256'], result['directory_sha256'])
        blocked = result['rejected_announcements'][0]
        self.assertEqual(blocked['reference_period'], 'FY2026-Q4')
        self.assertEqual(blocked['reason'], 'insecure_scheme')
        self.assertTrue(blocked['url'].startswith('http://'))
        self.assertEqual(blocked['observed_url'], blocked['url'])
        self.assertTrue(all(row['read'] is False and row['download_attempted'] is False for row in result['attachments']))
        self.assertTrue(all(row['status'] == 'not_read' and row['allowed'] is False for row in result['attachments']))
        self.assertTrue(all(row['reason'] == 'host_not_registered' for row in result['attachments']))
        self.assertFalse(result['coverage']['continuous_coverage'])
        self.assertIsNone(result['coverage']['latest_publication'])
        self.assertFalse(result['coverage']['latest_announcement_verified'])
        self.assertEqual(result['coverage']['years'], list(range(2019, 2027)))

    def test_announcement_url_boundary_rejects_without_upgrading_or_following(self):
        cases = {
            'http://investors.micron.com/news/press-release/2026/release/default.aspx': 'insecure_scheme',
            'https://investors.micron.com.evil.example/news/press-release/2026/release/default.aspx': 'host_not_registered',
            'https://127.0.0.1/news/press-release/2026/release/default.aspx': 'host_not_registered',
            'https://user:password@investors.micron.com/news/press-release/2026/release/default.aspx': 'link_credentials_forbidden',
            'https://investors.micron.com:444/news/press-release/2026/release/default.aspx': 'link_port_forbidden',
            'javascript:alert(1)': 'invalid_link_scheme',
            'data:text/html,example': 'invalid_link_scheme',
            'file:///etc/passwd': 'invalid_link_scheme',
            'https://investors.micron.com/news/press-release/2026/%252e%252e/private': 'link_path_forbidden',
            'https://investors.micron.com/news/press-release/2026/%0arelease': 'link_path_forbidden',
            'https://investors.micron.com/private/admin': 'announcement_path_unreviewed',
            'https://investors.micron.com/news/press-release/2026/release?redirect=https://example.invalid': 'announcement_path_unreviewed',
            'https://investors.micron.com\\@example.invalid/release': 'invalid_link',
            ' https://investors.micron.com/news/press-release/2026/release': 'invalid_link',
        }
        for link, expected in cases.items():
            with self.subTest(link=link):
                data = [{'category': 'Press release', 'data': [{'year': 2026, 'documents': [{'text': 'Q3', 'link': link}]}]}]
                out = parse_micron_directory(page(data))
                self.assertEqual(out['announcements'], [])
                self.assertEqual(out['rejected_announcements'][0]['reason'], expected)

    def test_relative_reference_preserves_observed_link_and_resolves_only_fixed_origin(self):
        data = [{'category': 'Press release', 'data': [{'year': 2026, 'documents': [
            {'text': 'Q3', 'link': '/news/press-release/2026/release/default.aspx#section'}]}]}]
        row = parse_micron_directory(page(data))['announcements'][0]
        self.assertEqual(row['observed_url'], '/news/press-release/2026/release/default.aspx#section')
        self.assertEqual(row['url'], 'https://investors.micron.com/news/press-release/2026/release/default.aspx')

    def test_directory_is_not_arbitrary_url_input_and_has_hard_byte_limit(self):
        raw = page()
        self.assert_error(raw, 'directory_url_not_allowed', directory_url='https://example.invalid/hub')
        self.assert_error(raw, 'directory_url_not_allowed', directory_url=MICRON_DIRECTORY_URL + '?year=2026')
        self.assert_error(raw, 'directory_too_large', max_bytes=len(raw) - 1)
        self.assertEqual(parse_micron_directory(raw, max_bytes=len(raw))['directory_bytes'], len(raw))
        self.assert_error(b'x' * (MAX_DIRECTORY_BYTES + 1), 'directory_too_large', max_bytes=MAX_DIRECTORY_BYTES * 10)
        self.assert_error(raw, 'directory_input_invalid', max_bytes=0)
        self.assert_error(raw.decode(), 'directory_input_invalid')
        self.assert_error(b'\xff', 'directory_encoding_invalid')

    def test_data_must_be_one_literal_json_value_without_executable_tail(self):
        for expression in ('fetch("/new").then(x=>x)', 'JSON.parse("[]")', '(()=>[])()', '[function(){return 1}]', '[] + fetch("/new")', '[] .map(x=>x)'):
            with self.subTest(expression=expression), self.assertRaises(MicronDirectoryError):
                parse_micron_directory(page(value=expression))
        self.assert_error(page(value='[NaN]'), 'directory_json_invalid')
        self.assert_error(page(value='[{"category":"Press release","category":"other","data":[]}]'), 'directory_duplicate_json_key')
        self.assert_error(page(value='[' * 13 + ']' * 13), 'directory_json_too_deep')
        self.assert_error(page(value='["' + 'x' * MAX_JSON_CHARS + '"]'), 'directory_json_too_large')
        self.assert_error(page(after=';const duplicate={documentCategoryData: []};'), 'directory_data_ambiguous')
        self.assert_error(b'<script>const x={documentCategoryData: []', 'directory_html_invalid')

    def test_strings_comments_and_external_script_references_are_not_data(self):
        before = 'const sample="documentCategoryData: []"; // documentCategoryData: []\n/* documentCategoryData: [] */'
        self.assertEqual(parse_micron_directory(page(before=before))['counts']['announcements_accepted'], 31)
        self.assert_error(b'<script src="/remote.js">documentCategoryData: []</script>', 'directory_data_missing')
        self.assert_error(b'<p>documentCategoryData: []</p>', 'directory_data_missing')
        self.assert_error(b'<script>/* incomplete documentCategoryData</script>', 'directory_script_malformed')
        self.assert_error(b'<script>const x=/documentCategoryData: [{"category":"Press release","data":[]}],/;</script>', 'directory_data_missing')
        self.assert_error(b'<script type="text/template">widget({documentCategoryData: [{"category":"Press release","data":[]}], x:true});</script>', 'directory_data_missing')

    def test_schema_and_duplicate_periods_fail_closed(self):
        data = directory_data()
        cases = []
        duplicate = copy.deepcopy(data); duplicate.append(copy.deepcopy(data[0])); cases.append(duplicate)
        duplicate = copy.deepcopy(data); duplicate[2]['data'].append(copy.deepcopy(duplicate[2]['data'][0])); cases.append(duplicate)
        bad = copy.deepcopy(data); bad[2]['data'][0]['documents'][1]['text'] = 'Q1'; cases.append(bad)
        bad = copy.deepcopy(data); bad[2]['data'][0]['documents'][1]['link'] = bad[2]['data'][0]['documents'][0]['link']; cases.append(bad)
        bad = copy.deepcopy(data); bad[2]['data'][0]['year'] = True; cases.append(bad)
        bad = copy.deepcopy(data); bad[2]['data'][0]['documents'][0]['published'] = '2026-01-01'; cases.append(bad)
        bad = copy.deepcopy(data); bad[2]['category'] = []; cases.append(bad)
        bad = copy.deepcopy(data); bad[2]['data'][0]['documents'][0]['link'] = 'x' * 2049; cases.append(bad)
        for value in cases:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(MicronDirectoryError):
                parse_micron_directory(page(value))
        self.assert_error(page(data[:2]), 'directory_press_releases_missing')

    def test_observation_is_optional_not_a_publication_time_and_never_backfilled(self):
        out = parse_micron_directory(page())
        self.assertIsNone(out['observed_at'])
        for value in ('2026-10-29T08:00:00', True, float('nan'), 'not-a-date'):
            with self.subTest(value=value):
                self.assert_error(page(), 'directory_observation_time_invalid', observed_at=value)
        out = parse_micron_directory(page(), observed_at=0)
        self.assertEqual(out['observed_at'], '1970-01-01T00:00:00Z')
        self.assertTrue(all(row['published'] is None for row in out['announcements']))

    def test_explicit_empty_directory_is_a_snapshot_not_an_invented_announcement(self):
        out = parse_micron_directory(page([{'category': 'Press release', 'data': []}]))
        self.assertEqual(out['announcements'], [])
        self.assertEqual(out['rejected_announcements'], [])
        self.assertEqual(out['attachments'], [])
        self.assertEqual(out['counts']['announcements_seen'], 0)
        self.assertEqual(out['coverage']['listed_periods'], [])
        self.assertIsNone(out['coverage']['latest_publication'])


if __name__ == '__main__':
    unittest.main()
