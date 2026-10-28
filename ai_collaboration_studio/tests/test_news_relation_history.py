"""Offline storage checks for provenance-only identity and immutable annotations."""
import copy
import socket
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.news_platform import NewsPlatform,FeedResponse,parse_feed,SOURCES
from backend.news_event_actions import confirm_relation,confirm_withdrawal


BODY='An official synthetic statement with explicit scope and a reported operating condition.'


class RelationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.network=patch.object(socket.socket,'connect',side_effect=AssertionError('network_forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.now=1793220000.0
        self.payload=b''
        self.news=NewsPlatform(Path(self.directory.name)/'news.sqlite',transport=lambda *a:FeedResponse(200,self.payload),clock=lambda:self.now)
        self.target=self.add_item('target')
        self.origin_b=self.add_item('origin-b')
        self.origin_c=self.add_item('origin-c')
        self.body_version=self.news.save_article_evidence(self.target,self.body())['current_version_id']

    def add_item(self,guid,excerpt='An original synthetic release with a stated condition.'):
        self.payload=(f'<rss><channel><item><guid>{guid}</guid><title>Release {guid}</title>'
                      f'<link>https://www.federalreserve.gov/{guid}.htm</link><description>{excerpt}</description>'
                      '</item></channel></rss>').encode()
        with self.news._db() as db:
            source=db.execute("SELECT * FROM sources WHERE id='fed'").fetchone()
        self.news._fetch(source)
        return parse_feed(self.payload,SOURCES[0])[0]['id']

    def body(self,text=BODY,state='full_text'):
        return {'content_status':state,'body_text':text,'segments':[{'id':'p1','text':text,'kind':'paragraph'}],
                'attachments':[],'extractor_version':'test-v1'}

    def test_equivalence_follows_only_contiguous_same_material_predecessors(self):
        first=confirm_relation(self.news,self.target,self.origin_b)
        self.now+=1
        second=confirm_relation(self.news,self.target,self.origin_c)
        chain=self.news.equivalent_reading_versions(self.target,second['current_version_id'])
        self.assertEqual([value['id'] for value in chain],[second['current_version_id'],first['current_version_id'],self.body_version])
        self.assertEqual(len({value['content_hash'] for value in chain}),1)
        self.assertEqual(len({value['event_id'] for value in chain}),3)
        self.assertTrue(all(value['current_version_id']==value['id'] and value['evidence_package_id'] for value in chain))
        past=self.news.equivalent_reading_versions(self.target,first['current_version_id'])
        self.assertEqual([value['id'] for value in past],[first['current_version_id'],self.body_version])
        self.assertEqual(self.news.equivalent_reading_versions(self.origin_b,second['current_version_id']),[])

    def test_material_change_and_reversion_do_not_jump_over_changed_scope(self):
        confirm_relation(self.news,self.target,self.origin_b)
        partial=self.news.save_article_evidence(self.target,self.body(state='partial_text'))
        chain=self.news.equivalent_reading_versions(self.target,partial['current_version_id'])
        self.assertEqual(len(chain),1)
        restored=self.news.save_article_evidence(self.target,self.body())
        self.assertEqual([value['id'] for value in self.news.equivalent_reading_versions(self.target,restored['current_version_id'])],
                         [restored['current_version_id']])

    def test_scope_revision_inherits_confirmation_without_a_fake_new_user_action(self):
        confirmed=confirm_relation(self.news,self.target,self.origin_b)
        confirmation=copy.deepcopy(confirmed['evidence']['relation_confirmation'])
        revised=self.news.save_article_evidence(self.target,self.body(state='partial_text'))
        self.assertEqual(revised['evidence']['relation_confirmation'],confirmation)
        self.assertTrue(revised['evidence']['origin_confirmed'])
        self.assertEqual(revised['evidence']['origin_url'],confirmed['evidence']['origin_url'])
        self.assertEqual(len(revised['event']['relation_annotations']),1)

    def test_repeated_confirmation_records_new_annotation_without_content_or_task_version(self):
        first=confirm_relation(self.news,self.target,self.origin_b)
        with self.news._db() as db:
            tasks=db.execute('SELECT COUNT(*) FROM news_analysis_tasks').fetchone()[0]
            fetches=db.execute('SELECT COUNT(*) FROM news_article_fetches').fetchone()[0]
        original_version=self.news.get_item(self.origin_b)['current_version_id']
        self.now+=10
        self.add_item('origin-b','A corrected original statement with additional explicit conditions.')
        updated_original_version=self.news.get_item(self.origin_b)['current_version_id']
        self.assertNotEqual(original_version,updated_original_version)
        # Count other article's legitimate new task independently.
        with self.news._db() as db:
            tasks=db.execute('SELECT COUNT(*) FROM news_analysis_tasks').fetchone()[0]
        second=confirm_relation(self.news,self.target,self.origin_b)
        self.assertEqual(first['current_version_id'],second['current_version_id'])
        records=second['event']['relation_annotations']
        self.assertEqual(len(records),2)
        self.assertEqual(records[0]['confirmation']['original_version_id'],updated_original_version)
        self.assertEqual(records[1]['confirmation']['original_version_id'],original_version)
        with self.news._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM news_analysis_tasks').fetchone()[0],tasks)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM news_article_fetches').fetchone()[0],fetches)
            with self.assertRaisesRegex(sqlite3.IntegrityError,'annotation_immutable'):
                db.execute("UPDATE news_relation_annotations SET origin_url='changed'")
        reopened=NewsPlatform(self.news.path,clock=lambda:self.now)
        self.assertEqual(reopened.get_item(self.target)['event']['relation_annotations'],records)
        self.assertEqual(len(reopened.event_detail(self.target,as_of=self.now-1)['relation_annotations']),1)
        later=reopened.save_article_evidence(self.target,self.body(state='partial_text'))
        self.assertEqual(later['evidence']['relation_confirmation']['original_version_id'],updated_original_version)

    def test_annotation_failure_rolls_back_relation_and_content_commit(self):
        before=self.news.get_item(self.target)
        with self.news._db() as db:
            db.execute("CREATE TRIGGER fixture_annotation_failure BEFORE INSERT ON news_relation_annotations BEGIN SELECT RAISE(ABORT,'fixture_failed_annotation'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'fixture_failed_annotation'):
            confirm_relation(self.news,self.target,self.origin_b)
        after=self.news.get_item(self.target)
        self.assertEqual(after['current_version_id'],before['current_version_id'])
        self.assertEqual(after['event']['event_id'],before['event']['event_id'])
        self.assertEqual(after['event']['relation_annotations'],[])

    def test_relation_and_withdrawal_actions_do_not_inflate_body_read_metrics(self):
        quote='This official synthetic announcement was withdrawn by its publisher.'
        notice=self.add_item('withdrawal-notice',quote)
        before=self.news.overview()['quality']['body_reads']
        confirm_relation(self.news,self.target,self.origin_b)
        withdrawn=confirm_withdrawal(self.news,self.target,notice,quote)
        self.assertTrue(withdrawn['event']['retracted'])
        self.assertEqual(withdrawn['evidence']['withdrawal_evidence']['item_id'],notice)
        self.assertEqual(self.news.overview()['quality']['body_reads'],before)


if __name__=='__main__':
    unittest.main()
