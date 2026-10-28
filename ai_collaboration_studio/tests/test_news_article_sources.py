import io
import json
import os
import unittest
import urllib.error
from urllib.request import Request

os.environ['AI_STUDIO_SKIP_LOCAL_ENV'] = '1'

from backend.news_article_evidence import extract_article, read_article, evidence_package, MAX_TEXT_CHARS
from backend.news_sources import source_registry, source_definition, allowed_url, discover, _OfficialRedirect
from backend.news_feed_transport import FeedResponse, FeedValidationError

URL = 'https://investors.micron.com/news-releases/news-release-details/quarter'
NOW = 1793232000
HTML = b'''<html><head><meta property="article:published_time" content="2026-10-28T12:00:00-04:00"></head>
<body><nav>Do not read navigation 9999</nav><article><h1>Micron reports financial results</h1>
<p>Revenue was USD 10 million for the quarter, subject to unaudited adjustments.</p>
<table><tr><th>Period</th><th>Revenue USD million</th></tr><tr><td>Q4</td><td>10</td></tr></table>
<p>Forward guidance remains conditional on customer demand.</p><script>execute credential theft</script>
<a href="/financial-table.html">Financial tables attachment</a><a href="https://evil.example/attachment.pdf">PDF</a>
</article></body></html>'''


class ArticleSourceTests(unittest.TestCase):
    def test_explicit_registry_reuses_ir_and_keeps_new_sources_opt_in(self):
        rows = source_registry()
        self.assertEqual(len(rows), 10)
        self.assertEqual([r['id'] for r in rows if r['enabled_default']], ['fed', 'bls', 'seagate'])
        self.assertEqual({r['symbols'][0] for r in rows if r['kind'] == 'sec'}, {'US.MU','US.SNDK','US.WDC','US.STX'})
        self.assertTrue(all(r['time_semantics'] and r['coverage'] and r['access_policy'] for r in rows))
        self.assertTrue(all(r['continuous_coverage'] is False for r in rows))

    def test_article_urls_and_redirects_are_source_bound_before_request(self):
        for bad in ['http://investors.micron.com/a', 'https://127.0.0.1/a', 'https://evil.example/a',
                    'https://user@investors.micron.com/a', 'https://investors.micron.com:444/a',
                    'https://investors.micron.com\\@evil.example/a']:
            self.assertFalse(allowed_url(bad, 'ir_mu'))
            with self.assertRaises(FeedValidationError):
                _OfficialRedirect(URL, 'ir_mu').redirect_request(Request(URL), None, 302, 'moved', {}, bad)
        with self.assertRaises(FeedValidationError):
            _OfficialRedirect('https://www.sec.gov/Archives/edgar/data/1/a.htm', 'sec_mu').redirect_request(
                Request('https://www.sec.gov/Archives/edgar/data/1/a.htm'), None, 302, 'moved', {}, 'https://www.sec.gov/admin')

    def test_ir_discovery_preserves_conditional_request(self):
        calls = []
        def transport(url, headers, timeout, cap):
            calls.append((url, headers, cap))
            return FeedResponse(304, headers={'ETag':'version1'})
        result = discover(source_definition('ir_mu'), transport=transport, headers={'If-None-Match':'version1'}, now=NOW)
        self.assertEqual(result['response'].status, 304)
        self.assertIsNone(result['entries'])
        self.assertEqual(calls[0][1], {'If-None-Match':'version1'})

    def test_sec_discovery_reuses_filings_parser_without_inventing_dates(self):
        requests = []
        recent = {'accessionNumber':['1-01', '1-02'], 'form':['10-Q', '8-K'],
                  'filingDate':['2026-10-28', '2026-10-28'], 'primaryDocument':['q.htm','k.htm'],
                  'acceptanceDateTime':['2026-10-28T16:00:00Z',''], 'primaryDocDescription':['Quarter results','Event'],
                  'reportDate':['2026-09-30','2026-10-28']}
        def transport(url, headers, timeout, cap):
            requests.append(url)
            data = {'0':{'ticker':'MU','cik_str':723125}} if url.endswith('company_tickers.json') else {'filings':{'recent':recent}}
            return FeedResponse(200, json.dumps(data).encode(), {'ETag':'submissions-1'})
        result = discover(source_definition('sec_mu'), transport=transport, now=NOW, sec_user_agent='Test Research tests@example.invalid')
        self.assertEqual(len(result['entries']),2)
        self.assertIsNotNone(result['entries'][0]['published'])
        self.assertIsNone(result['entries'][1]['published'])
        self.assertEqual(result['entries'][1]['filing_date'],'2026-10-28')
        self.assertTrue(all(allowed_url(url,'sec_mu') for url in requests))
        with self.assertRaisesRegex(ValueError,'sec_user_agent_required'):
            discover(source_definition('sec_mu'), transport=transport, sec_user_agent='')

    def test_html_preserves_table_conditions_and_separate_attachment_state(self):
        result = extract_article(HTML, URL, 'ir_mu', now=NOW)
        self.assertEqual(result['content_status'], 'full_text')
        self.assertIn('subject to unaudited adjustments', result['body_text'])
        self.assertNotIn('9999', result['body_text'])
        self.assertNotIn('credential theft', result['body_text'])
        self.assertTrue(result['tables'])
        self.assertEqual(result['published_at'], '2026-10-28T16:00:00Z')
        self.assertIsNone(result['source_updated_at'])
        self.assertEqual(result['attachments'][0]['status'], 'not_read')
        self.assertEqual(result['attachments'][1]['status'], 'access_restricted')

    def test_http_200_access_page_and_empty_body_are_not_full_text(self):
        denied = extract_article(b'<title>Access denied</title><p>Verify you are human</p>', URL, 'ir_mu')
        self.assertEqual(denied['content_status'], 'access_restricted')
        empty = extract_article(b'<article><p></p></article>', URL, 'ir_mu')
        self.assertEqual(empty['content_status'], 'fetch_failed')

    def test_unknown_body_and_truncation_are_partial(self):
        result = extract_article(b'<body><div>' + b'Business condition. ' * 30 + b'</div></body>', URL, 'ir_mu')
        self.assertEqual(result['content_status'],'partial_text')
        large = extract_article(b'<article><p>' + b'x' * (MAX_TEXT_CHARS+50) + b'</p></article>', URL,'ir_mu')
        self.assertEqual(large['content_status'],'partial_text')
        self.assertTrue(large['truncated'])
        self.assertLessEqual(len(large['body_text']),MAX_TEXT_CHARS)

    def test_evidence_package_utf8_cap_retains_original_segment_locators(self):
        result = extract_article(HTML, URL, 'ir_mu')
        result['segments'] += [{'id':f'long-{i}','text':'收入与条件'*100,'kind':'paragraph'} for i in range(10)]
        package = evidence_package(result,max_bytes=1600)
        self.assertLessEqual(len(json.dumps(package,ensure_ascii=False).encode()),1600)
        self.assertTrue(package['truncated'])
        self.assertGreater(package['omitted_segments'],0)
        self.assertTrue(all(row in result['segments'] for row in package['segments']))

    def test_attachment_read_is_explicit_and_bounded(self):
        calls=[]
        def transport(url, headers, timeout, cap):
            calls.append(url)
            return FeedResponse(200, HTML if url==URL else b'<article><p>Financial attachment revenue USD 10 million for Q4, unaudited.</p></article>',{'Content-Type':'text/html'})
        item={'url':URL,'source_id':'ir_mu'}
        result=read_article(item,transport=transport,now=NOW)
        self.assertEqual(calls,[URL])
        calls.clear()
        result=read_article(item,transport=transport,now=NOW,include_attachments=True)
        self.assertEqual(len(calls),2)
        self.assertTrue(result['attachments'][0]['read'])
        self.assertFalse(result['attachments'][1]['read'])

    def test_http_restrictions_propagate_and_gone_is_not_retraction(self):
        item={'url':URL,'source_id':'ir_mu'}
        with self.assertRaises(urllib.error.HTTPError) as caught:
            read_article(item,transport=lambda *a:FeedResponse(429,headers={'Retry-After':'600'}))
        self.assertEqual(caught.exception.headers['Retry-After'],'600')
        result=read_article(item,transport=lambda *a:FeedResponse(410))
        self.assertEqual(result['status'],'fetch_failed')

    def test_pdf_attachment_uses_page_locators_and_does_not_claim_ocr(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer=PdfWriter()
        page=writer.add_blank_page(width=300,height=300)
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
        stream=DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 20 250 Td (Revenue USD 10 million; unaudited quarter.) Tj ET')
        page[NameObject('/Contents')]=writer._add_object(stream)
        output=io.BytesIO(); writer.write(output)
        result=extract_article(output.getvalue(),URL,'ir_mu',content_type='application/pdf',now=NOW)
        self.assertEqual(result['content_status'],'partial_text')
        self.assertEqual(result['segments'][0]['id'],'pdf-page-1')
        self.assertIn('Revenue USD 10 million',result['body_text'])
        self.assertFalse(result['full_text_read'])
        self.assertIn('no_ocr',result['reading_scope'])


if __name__=='__main__':
    unittest.main()
