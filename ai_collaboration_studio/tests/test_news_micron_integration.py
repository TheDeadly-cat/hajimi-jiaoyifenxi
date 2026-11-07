"""Directory ingestion in isolated databases; all transports are in-memory."""
import copy
import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from backend.news_platform import NewsPlatform, analyze
from backend.news_sources import source_definition, discover
from backend.news_feed_transport import FeedResponse
from backend.ir_source_catalog import IR_FEEDS
from backend import news_event_store as store
from tests.test_news_micron_directory import page

NOW=1793240000.0
ROOT_URL='https://investors.micron.com/news/press-release/2026/'
def directory(count=2):
    return [{'category':'Press release','data':[{'year':2026,'documents':[
        {'text':f'Q{n}','link':ROOT_URL+f'Fiscal-2026-Q{n}/default.aspx'} for n in range(1,count+1)]}]}]


class MicronIntegrationTests(unittest.TestCase):
    def setUp(self):
        guard=patch.object(socket.socket,'connect',side_effect=AssertionError('network_forbidden'))
        guard.start();self.addCleanup(guard.stop)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'news.sqlite';self.now=NOW;self.calls=[]
        self.response=FeedResponse(200,page(directory()),{'ETag':'directory-v1'})
        self.news=self.make_news();self.addCleanup(self.news.stop)
        with self.news._db() as db:db.execute('UPDATE sources SET enabled=0')
        self.news.configure_source('ir_mu',True)

    def transport(self,url,headers,timeout,cap):
        self.calls.append((url,copy.deepcopy(headers)))
        self.assertEqual(url,source_definition('ir_mu')['url'])
        return self.response

    def make_news(self):return NewsPlatform(self.path,clock=lambda:self.now,transport=self.transport)
    def poll(self):
        with self.news._db() as db:row=dict(db.execute("SELECT * FROM sources WHERE id='ir_mu'").fetchone())
        self.now+=1
        return self.news._fetch(row)
    def counts(self):
        with self.news._db() as db:
            return {name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0] for name in
                    ('items','news_article_versions','news_article_tasks','news_analysis_tasks')}

    def test_first_directory_is_historical_provenance_without_body_or_analysis_queue(self):
        self.assertIn('/rss/',IR_FEEDS['US.MU']['url'])
        self.assertEqual(source_definition('ir_mu')['discovery_method'],'quarterly_directory')
        self.poll()
        self.assertEqual(self.counts(),{'items':2,'news_article_versions':2,'news_article_tasks':0,'news_analysis_tasks':0})
        self.assertEqual(len(self.calls),1)
        for item in self.news.list_items(source='ir_mu')['items']:
            self.assertEqual(item['discovery_kind'],'historical_backfill')
            self.assertFalse(item['automatic_reading_eligible']);self.assertIsNone(item['published_at'])
            self.assertTrue(item['reference_period'].startswith('FY2026-Q'))
            detail=self.news.get_item(item['id'])
            self.assertTrue(detail['discovery_evidence']['json_path'])
            self.assertEqual(detail['discovery_evidence']['directory_sha256'],hashlib.sha256(self.response.body).hexdigest())
        state=next(row for row in self.news.overview()['sources'] if row['id']=='ir_mu')
        self.assertEqual(state['last_check']['parsed_items'],2)

    def test_repeat_and_304_do_not_create_versions_or_restore_old_cache(self):
        self.poll();before=self.counts();self.poll()
        self.assertEqual(self.counts(),before)
        self.response=FeedResponse(304,headers={'ETag':'directory-v1'});self.poll()
        self.assertEqual(self.counts(),before)
        self.assertEqual(self.calls[-1][1],{'If-None-Match':'directory-v1'})
        state=next(row for row in self.news.overview()['sources'] if row['id']=='ir_mu')
        self.assertEqual(state['last_check']['outcome'],'not_modified')

    def test_later_new_link_is_new_discovery_with_unknown_publication(self):
        self.poll();self.response=FeedResponse(200,page(directory(3)),{'ETag':'directory-v2'});self.poll()
        rows=self.news.list_items(source='ir_mu')['items']
        new=next(row for row in rows if row['reference_period']=='FY2026-Q3')
        self.assertTrue(new['automatic_reading_eligible']);self.assertIsNone(new['published_at'])
        self.assertEqual(self.counts()['news_article_tasks'],1)
        self.assertEqual(self.counts()['news_analysis_tasks'],0)
        self.assertEqual(len(self.calls),2)  # No body fetched on discovery.

    def test_invalid_200_is_failure_and_keeps_last_good_cursor(self):
        self.poll()
        with self.news._db() as db:before=db.execute("SELECT cursor FROM news_source_coverage WHERE source_id='ir_mu'").fetchone()[0]
        self.response=FeedResponse(200,b'<html>verify you are human</html>',{'ETag':'bad'});self.poll()
        state=next(row for row in self.news.overview()['sources'] if row['id']=='ir_mu')
        self.assertEqual(state['status'],'error');self.assertEqual(state['last_check']['outcome'],'failed')
        self.assertEqual(state['last_check']['http_status'],200)
        with self.news._db() as db:
            self.assertEqual(db.execute("SELECT cursor FROM news_source_coverage WHERE source_id='ir_mu'").fetchone()[0],before)
            self.assertEqual(db.execute("SELECT etag FROM sources WHERE id='ir_mu'").fetchone()[0],'directory-v1')

    def test_valid_empty_directory_is_success_with_zero_parsed(self):
        self.response=FeedResponse(200,page([{'category':'Press release','data':[]}]))
        self.poll();state=next(row for row in self.news.overview()['sources'] if row['id']=='ir_mu')
        self.assertEqual(state['status'],'ok');self.assertEqual(state['last_check']['parsed_items'],0)
        self.assertEqual(state['last_check']['outcome'],'success')

    def test_article_publication_updates_visible_time_without_promoting_directory_title(self):
        from backend.news_article_evidence import extract_article
        from backend.news_evidence_reading import build_input
        self.poll();item=self.news.list_items(source='ir_mu')['items'][0]
        self.assertIsNone(item['published_at'])
        evidence=extract_article(b'<meta property="article:published_time" content="2026-06-24T15:01:00Z"><article><p>Saved source conditions remain attached.</p></article>',item['url'],'ir_mu',now=NOW)
        self.news.save_article_evidence(item['id'],evidence)
        current=self.news.get_item(item['id'])
        listed=next(row for row in self.news.list_items(source='ir_mu')['items'] if row['id']==item['id'])
        self.assertEqual(listed['published_at'],current['published_at'])
        self.assertEqual(current['published_at'],'2026-06-24T15:01:00Z')
        material=build_input(current,checked_at=NOW)
        self.assertEqual(material['metadata']['title_basis'],'directory_label_not_original_headline')
        self.assertTrue(any('不是已确认的原文标题' in warning for warning in material['reading_warnings']))
        with self.news._db() as db:
            task=db.execute('SELECT id FROM news_analysis_tasks WHERE item_id=?',(item['id'],)).fetchone()
        frozen=self.news.analysis_task_item(task['id'])
        self.assertEqual(frozen['discovery_evidence'],current['discovery_evidence'])
        self.assertEqual(build_input(frozen,checked_at=NOW)['metadata']['title_basis'],'directory_label_not_original_headline')
        frozen['discovery_evidence']['attachment_references']=[{'url':'https://unregistered.example/one.pdf','read':False}]
        scope=build_input(frozen,checked_at=NOW)['scope']
        self.assertEqual(scope['directory_attachment_references']['not_read'],1)
        self.assertEqual(scope['attachment_count'],0)

    def test_route_migration_preserves_old_failure_and_wait_and_requires_reenable(self):
        old_url=IR_FEEDS['US.MU']['url']
        with self.news._db() as db:
            db.execute("UPDATE sources SET url=?,enabled=1,status='error',error='saved RSS failure',failures=3,etag='old-rss-tag',last_modified='old-date',not_before=?,next_poll=?,manual_hold=1,refresh_queued=1 WHERE id='ir_mu'",(old_url,NOW+500,NOW+600))
            db.execute("INSERT INTO poll_runs(id,session_id,source_id,started,finished,outcome,http_status,error_code) VALUES('old-failed','old','ir_mu',?,?,'failed',404,'http_error')",(NOW-10,NOW-9))
        self.news.stop();self.news=self.make_news();self.addCleanup(self.news.stop)
        state=next(row for row in self.news.overview()['sources'] if row['id']=='ir_mu')
        self.assertFalse(state['enabled']);self.assertTrue(state['endpoint_change']['explicit_reenable_required'])
        self.assertEqual(state['last_check']['http_status'],404)
        with self.news._db() as db:
            row=db.execute("SELECT * FROM sources WHERE id='ir_mu'").fetchone()
            self.assertIsNone(row['etag']);self.assertIsNone(row['last_modified'])
            self.assertEqual((row['not_before'],row['next_poll'],row['failures'],row['manual_hold']),(NOW+500,NOW+600,3,1))
            old=json.loads(db.execute('SELECT previous_state FROM news_source_endpoint_changes').fetchone()[0])
            self.assertEqual(old['error'],'saved RSS failure');self.assertEqual(old['etag'],'old-rss-tag')
        self.news.configure_source('ir_mu',True)
        self.assertFalse(self.poll());self.assertEqual(self.calls,[])
        self.now=NOW+700;self.assertFalse(self.poll())  # Manual hold still applies.
        with self.news._db() as db:db.execute("UPDATE sources SET manual_hold=0 WHERE id='ir_mu'")
        self.assertTrue(self.poll());self.assertEqual(self.calls[0][1],{})
        with self.news._db() as db:self.assertEqual(db.execute("SELECT outcome FROM poll_runs WHERE id='old-failed'").fetchone()[0],'failed')

    def test_existing_guid_article_and_material_are_not_replaced_by_directory_labels(self):
        with self.news._db() as db:
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('guid-item','ir_mu','original-guid','Original RSS article',ROOT_URL+'Fiscal-2026-Q1/default.aspx?utm_source=old','Source excerpt with conditions',NOW-100,NOW-90,NOW-90,json.dumps(analyze('Original RSS article','Source excerpt with conditions',NOW-100,NOW-90,'ir_mu'))))
            row=dict(db.execute("SELECT * FROM items WHERE id='guid-item'").fetchone())
            version,_=store.record_version(db,row,NOW-90,metadata={'published_time_source':'rss:pubDate'},enqueue=False)
            snapshot=db.execute('SELECT snapshot FROM news_article_versions WHERE id=?',(version['id'],)).fetchone()[0]
        self.poll()
        with self.news._db() as db:
            row=db.execute("SELECT * FROM items WHERE id='guid-item'").fetchone()
            self.assertEqual(row['identity'],'original-guid');self.assertEqual(row['title'],'Original RSS article')
            self.assertEqual(row['excerpt'],'Source excerpt with conditions');self.assertEqual(row['published'],NOW-100)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM news_article_versions WHERE item_id='guid-item'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT snapshot FROM news_article_versions WHERE id=?',(version['id'],)).fetchone()[0],snapshot)
            observed=json.loads(db.execute("SELECT record FROM news_article_observations WHERE item_id='guid-item'").fetchone()[0])
            self.assertTrue(observed['discovery_evidence']['json_path'])
            self.assertEqual(observed['published_time_source'],'rss:pubDate')

    def test_over_100_legacy_pending_directory_rows_cannot_starve_an_eligible_task(self):
        with self.news._db() as db:
            for index in range(102):
                name=f'pending-{index:03d}'
                db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (name,'ir_mu',name,'Saved financial results',ROOT_URL+name,'',None,NOW-100+index,NOW-100+index,json.dumps(analyze('Saved financial results','',None,NOW,'ir_mu'))))
                item=dict(db.execute('SELECT * FROM items WHERE id=?',(name,)).fetchone())
                version,_=store.record_version(db,item,NOW-100+index,metadata={'symbols':['US.MU']})
                if index<101:store.observe(db,name,{'automatic_reading_eligible':False},NOW)
            db.execute("UPDATE settings SET value='true' WHERE key IN ('enabled','article_reading_enabled')")
        read=[]
        def saved_read(item_id,**kwargs):
            read.append(item_id)
            return {'current_version_id':version['id'],'event':{'last_fetch_attempt':{'content_status':'full_text'}}}
        with patch.object(self.news,'read_article',side_effect=saved_read):
            self.assertTrue(self.news._next_article_task())
        self.assertEqual(read,['pending-101']);self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
