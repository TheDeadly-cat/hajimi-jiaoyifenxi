"""Offline directory-discovery provenance and automatic-work admission."""
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.news_platform import NewsPlatform
from backend import news_event_store as store


class DirectoryVersionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        guard=patch.object(socket.socket,'connect',side_effect=AssertionError('offline_test_only'))
        guard.start()
        self.addCleanup(guard.stop)
        self.now=1793220000.0
        self.news=NewsPlatform(Path(self.directory.name)/'news.sqlite',clock=lambda:self.now)

    def metadata(self,eligible=False):
        return {'discovery_kind':'directory_change' if eligible else 'historical_backfill',
                'automatic_reading_eligible':eligible,'published_time_source':'unknown_directory_listing',
                'reference_period':'FY2026 Q3','symbols':['US.MU'],
                'discovery_evidence':{'schema':'mu_directory_discovery_v1',
                    'directory_url':'https://investors.micron.com/quarterly-results',
                    'directory_sha256':'a'*64,'json_path':'$.results[0].release',
                    'reference_period':'FY2026 Q3','title_basis':'directory_link_text',
                    'attachment_references':[{'url':'https://investors.micron.com/attachment.pdf','read':False,'status':'not_read'}]}}

    def save_discovery(self,item_id,metadata,excerpt=''):
        entry={'id':item_id,'source_id':'ir_mu','identity':item_id,
               'title':'Micron quarterly announcement '+item_id,
               'url':'https://investors.micron.com/release-'+item_id+'.htm',
               'excerpt':excerpt,'published':None,'discovered':self.now,'updated':self.now,
               'analysis':json.dumps({'category':'company','importance':'attention','summary':excerpt,'keywords':[],'limitations':[]})}
        with self.news._db() as db:
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       tuple(entry[key] for key in ('id','source_id','identity','title','url','excerpt','published','discovered','updated','analysis')))
            store.observe(db,item_id,{**entry,**metadata},self.now)
            result,_=store.record_version(db,entry,self.now,metadata=metadata)
        return result

    def body(self,text='A saved official article with full context and a clearly stated operating condition.'):
        return {'content_status':'full_text','body_text':text,'segments':[{'id':'p1','kind':'paragraph','text':text}],
                'attachments':[],'extractor_version':'fixture-v1'}

    def test_initial_directory_history_is_persistent_but_does_not_queue_automatic_body(self):
        metadata=self.metadata()
        version=self.save_discovery('historical',metadata)
        self.assertFalse(store.automatic_reading_eligible(version))
        self.assertEqual(version['discovery_kind'],'historical_backfill')
        self.assertIsNone(version['published_at'])
        self.assertEqual(version['time_provenance']['published_at'],'unknown_directory_listing')
        self.assertEqual(version['reference_period'],'FY2026 Q3')
        self.assertEqual(version['discovery_evidence'],metadata['discovery_evidence'])
        self.assertEqual(self.news.pending_article_tasks()['total'],0)
        self.assertEqual(self.news.pending_analysis_tasks()['total'],0)
        observed=self.news.event_detail('historical')['observations'][0]
        self.assertFalse(observed['automatic_reading_eligible'])
        self.assertEqual(observed['discovery_evidence'],metadata['discovery_evidence'])
        self.assertFalse(observed['discovery_evidence']['attachment_references'][0]['read'])

    def test_manual_body_and_later_body_revision_keep_historical_admission_and_discovery_proof(self):
        metadata=self.metadata()
        original=self.save_discovery('manual',metadata)
        for text in ('Saved body with full original conditions, scope and appropriate contextual details.',
                     'Corrected body with full original conditions, scope and a different operating amount.'):
            self.now+=10
            item=self.news.save_article_evidence('manual',self.body(text))
            current=item['event']['current_version']
            self.assertFalse(store.automatic_reading_eligible(current))
            self.assertEqual(current['discovery_evidence'],metadata['discovery_evidence'])
            self.assertEqual(current['reference_period'],'FY2026 Q3')
            self.assertEqual(current['discovery_kind'],'historical_backfill')
            self.assertEqual(self.news.analysis_task_for_item('manual')['trigger'],'historical_backfill')
            self.assertEqual(self.news.pending_article_tasks()['total'],0)
        reopened=NewsPlatform(self.news.path,clock=lambda:self.now)
        self.assertEqual(reopened.event_versions('manual')[-1],original)
        self.assertEqual(reopened.event_versions('manual')[0]['discovery_evidence'],metadata['discovery_evidence'])

    def test_later_directory_change_can_queue_body_without_inventing_publication(self):
        version=self.save_discovery('new-link',self.metadata(True))
        self.assertTrue(store.automatic_reading_eligible(version))
        self.assertEqual(version['discovery_kind'],'directory_change')
        self.assertIsNone(version['published_at'])
        self.assertEqual(self.news.pending_article_tasks()['total'],1)
        self.news.save_article_evidence('new-link',self.body())
        self.assertTrue(store.automatic_reading_eligible(self.news.event_versions('new-link')[0]))
        self.assertEqual(self.news.analysis_task_for_item('new-link')['trigger'],'body_evidence')

    def test_rss_observation_hash_is_unchanged_when_optional_directory_fields_absent(self):
        self.save_discovery('rss',{})
        entry={'identity':'rss','title':'RSS title','url':'https://investors.micron.com/rss-item.htm',
               'excerpt':'Existing RSS text','published':None,'source_updated_at':None,
               'published_time_source':'unknown','symbols':['US.MU'],'filing_date':None}
        with self.news._db() as db:
            store.observe(db,'rss',entry,self.now)
            before=db.execute("SELECT record_hash,record FROM news_article_observations WHERE item_id='rss' ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertEqual(before['record_hash'],store.digest(entry))
        self.assertNotIn('automatic_reading_eligible',json.loads(before['record']))
        self.assertTrue(store.automatic_reading_eligible({}))
        self.assertFalse(store.automatic_reading_eligible({'automatic_reading_eligible':False}))

    def test_new_directory_admission_does_not_rewrite_consumed_task_or_old_snapshot(self):
        old=self.save_discovery('retained',self.metadata(True),'An existing RSS observation with a stated scope.')
        task=self.news.analysis_task_for_item('retained')
        self.news.transition_analysis_task(task['id'],'unknown',expected_status='pending',result_ref='retained-uncertain-attempt')
        with self.news._db() as db:
            row=dict(db.execute("SELECT * FROM items WHERE id='retained'").fetchone())
            row['title']='Historical directory name observed after endpoint migration'
            result,_=store.record_version(db,row,self.now+1,metadata=self.metadata(False))
        self.assertFalse(result['automatic_reading_eligible'])
        self.assertEqual(self.news.event_versions('retained')[-1],old)
        with self.news._db() as db:
            retained=db.execute('SELECT status,result_ref FROM news_analysis_tasks WHERE id=?',(task['id'],)).fetchone()
        self.assertEqual(retained['status'],'unknown')
        self.assertEqual(retained['result_ref'],'retained-uncertain-attempt')


if __name__=='__main__':
    unittest.main()
