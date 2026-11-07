"""Read-only preview routes preserve source versions and never fetch or analyze."""
import copy
import hashlib
import json
import socket
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.news_article_evidence import extract_article
from backend.news_evidence_preview import evidence_preview
from backend.news_platform import NewsPlatform, analyze
from backend.news_platform_routes import news_response

URL='https://investors.micron.com/offline-preview.htm'
NOW=1793232000.0
RAW=b'<article><p>Quarter 3, amounts in millions, unaudited reported values.</p><table><tr><th>Measure</th><th>Q3</th></tr><tr><td>Cash</td><td>10</td></tr></table></article>'


class EvidencePreviewTests(unittest.TestCase):
    def setUp(self):
        guard=patch.object(socket.socket,'connect',side_effect=AssertionError('network_forbidden'))
        guard.start();self.addCleanup(guard.stop)
        self.evidence=extract_article(RAW,URL,'ir_mu',now=NOW)
        self.item={'id':'saved-article','title':'Offline release','url':URL,'excerpt':'Unreviewed excerpt.',
                   'current_version_id':'version-1','evidence':self.evidence,'published_at':'2026-10-28T00:00:00Z',
                   'discovered_at':'2026-10-29T00:00:00Z','version_seen_at':'2026-10-29T00:00:00Z'}

    def test_preview_keeps_complete_groups_original_text_and_decoded_cells(self):
        before=copy.deepcopy(self.item);value=evidence_preview(self.item,checked_at=NOW)
        self.assertEqual(value['status'],'ready')
        self.assertTrue(value['preview_only']);self.assertFalse(value['model_called'])
        self.assertEqual(value['content_hash'],self.evidence['content_hash'])
        self.assertEqual(value['version_id'],'version-1')
        self.assertEqual('\n'.join(r['text'] for r in value['segments']),self.evidence['body_text'])
        selected={r['source_segment_id']:r for r in value['segments']}
        for row in self.evidence['tables']:self.assertEqual(selected[row['id']]['cells'],row['cells'])
        self.assertEqual(self.item,before)

    def test_missing_hash_mismatch_and_missing_version_fail_without_using_raw_text(self):
        for change,reason in [('missing','content_hash_missing'),('mismatch','content_hash_mismatch'),('version','version_required')]:
            item=copy.deepcopy(self.item)
            if change=='missing':item['evidence'].pop('content_hash')
            elif change=='mismatch':item['evidence']['body_text']+=' silently changed'
            else:item['current_version_id']=''
            value=evidence_preview(item,checked_at=NOW)
            self.assertEqual(value['status'],'unavailable');self.assertEqual(value['reason'],reason)
            self.assertEqual(value['segments'],[]);self.assertIsNone(value['material'])

    def test_oversized_unverified_table_context_has_an_empty_explicit_preview(self):
        raw=RAW.replace(b'Cash',b'Cash '+b'conditions '*3000)
        self.item['evidence']=extract_article(raw,URL,'ir_mu',now=NOW)
        value=evidence_preview(self.item,checked_at=NOW)
        self.assertEqual(value['status'],'no_selected_evidence')
        self.assertEqual(value['segments'],[])
        self.assertEqual(value['material']['source_text']['body'],'')
        self.assertEqual(value['material']['source_text']['excerpt'],'')
        self.assertGreater(value['material']['evidence_package']['omitted_table_groups'],0)

    def test_detail_and_history_routes_do_not_write_or_replace_old_preview_with_current(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        now=[NOW];news=NewsPlatform(Path(temp.name)/'news.sqlite',clock=lambda:now[0]);self.addCleanup(news.stop)
        with news._db() as db:
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('saved-article','ir_mu','offline-local-fixture','Offline release',URL,'',NOW-10,NOW,NOW,
                        json.dumps(analyze('Offline release','',NOW-10,NOW,'ir_mu'))))
        first=news.save_article_evidence('saved-article',self.evidence)
        now[0]+=1
        changed=extract_article(RAW.replace(b'>10<',b'>11<'),URL,'ir_mu',now=now[0])
        second=news.save_article_evidence('saved-article',changed)
        def snapshot():
            with news._db() as db:
                return {name:[tuple(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in
                        ('news_article_versions','news_analysis_tasks','news_article_fetches','news_article_observations','items')}
        before=snapshot()
        with patch.object(news,'read_article',side_effect=AssertionError('fetch_forbidden')),patch.object(news,'refresh',side_effect=AssertionError('refresh_forbidden')):
            detail,status=news_response(news,'/api/news/items/saved-article');self.assertEqual(status,200)
            self.assertEqual(detail['item']['evidence_preview']['version_id'],second['current_version_id'])
            history,status=news_response(news,'/api/news/items/saved-article/versions');self.assertEqual(status,200)
            versions={v['id']:v for v in history['versions']}
            old=versions[first['current_version_id']]['evidence_preview']
            current=versions[second['current_version_id']]['evidence_preview']
            self.assertEqual(old['version_id'],first['current_version_id'])
            self.assertIn('Cash | 10',old['material']['source_text']['body'])
            self.assertIn('Cash | 11',current['material']['source_text']['body'])
        self.assertEqual(snapshot(),before)

    def test_non_body_legacy_item_does_not_get_a_fabricated_preview(self):
        self.assertIsNone(evidence_preview({'id':'rss','excerpt':'Original RSS only'}))

    def test_paragraph_and_attachment_cache_must_match_saved_body(self):
        plain=extract_article(b'<article><p>Original conditions remain in the saved body.</p></article>',URL,'ir_mu',now=NOW)
        for attachment in (False, True):
            item=copy.deepcopy(self.item)
            if attachment:
                item['evidence']['attachments']=[{'read':True,'url':URL+'/attachment','evidence':plain}]
                document=item['evidence']['attachments'][0]['evidence']
            else:
                item['evidence']=copy.deepcopy(plain)
                document=item['evidence']
            document['segments'][0]['text']='Changed cached paragraph without the original conditions.'
            value=evidence_preview(item,checked_at=NOW)
            self.assertEqual(value['status'],'unavailable')
            self.assertEqual(value['reason'],'source_segment_mismatch')
            self.assertIsNone(value['material'])


if __name__=='__main__':unittest.main()
