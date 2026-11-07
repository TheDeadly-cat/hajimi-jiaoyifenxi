"""Offline contracts for retained versions, durable queue and bounded reads."""
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import patch
from xml.sax.saxutils import escape

from backend.news_platform import NewsPlatform, FeedResponse, parse_feed, SOURCES
from backend.news_platform_ai import NewsAnalysis, MODEL


def feed(excerpt="Revenue was $10 million.", guid="one", url="https://www.federalreserve.gov/release.htm"):
    return (f'<rss><channel><item><guid>{guid}</guid><title>Quarterly financial results</title>'
            f'<link>{escape(url)}</link><pubDate>Thu, 29 Oct 2026 01:00:00 GMT</pubDate>'
            f'<description>{escape(excerpt)}</description></item></channel></rss>').encode()


def evidence(text="Revenue was $12 million, subject to the stated scope.", **extra):
    return {"content_status":"full_text", "body_text":text,
            "segments":[{"id":"p1","text":text,"kind":"paragraph"}],
            "attachments":[],"extractor_version":"offline-test-v1",**extra}


class NewsEventStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/"news.sqlite"
        self.now = time.time()-1000
        self.body = feed()
        self.network = patch.object(socket.socket,"connect",side_effect=AssertionError("offline_only"))
        self.network.start()
        self.news = NewsPlatform(self.path,transport=lambda *args: FeedResponse(200,self.body),clock=lambda:self.now)

    def tearDown(self):
        self.news.stop()
        self.network.stop()
        self.directory.cleanup()

    def fetch(self, source="fed"):
        with self.news._db() as db:
            row=db.execute("SELECT * FROM sources WHERE id=?",(source,)).fetchone()
        self.news._fetch(row)
        return self.news.get_item(parse_feed(self.body,{'id':source,'url':row['url']})[0]['id'])

    def test_revision_preserves_old_text_and_first_discovery_and_snapshot(self):
        first=self.fetch()
        task=self.news.pending_analysis_tasks()["tasks"][0]
        self.now+=100
        self.body=feed("Corrected revenue was $11 million.")
        self.fetch()
        history=self.news.event_versions(first['id'])
        self.assertEqual([v['excerpt'] for v in history],["Corrected revenue was $11 million.","Revenue was $10 million."])
        self.assertEqual(history[0]['first_seen_at'],history[1]['first_seen_at'])
        self.assertNotEqual(history[0]['version_seen_at'],history[1]['version_seen_at'])
        self.assertEqual(history[0]['previous_version_id'],history[1]['id'])
        self.assertEqual(self.news.analysis_task_item(task['id'])['excerpt'],"Revenue was $10 million.")
        self.assertEqual(self.news.pending_analysis_tasks(after=self.now-1)['total'],1)
        self.assertEqual(self.news.event_detail(first['id'],as_of=self.now-50)['current_version']['excerpt'],"Revenue was $10 million.")

    def test_no_change_tracking_and_format_do_not_replace_full_body_or_queue(self):
        item=self.fetch()
        self.news.save_article_evidence(item['id'],evidence())
        before=self.news.event_versions(item['id'])
        count=self.news.pending_analysis_tasks()['total']
        self.now+=100
        self.body=feed(" Revenue   was $10 million. ",url="https://www.federalreserve.gov/release.htm?utm_source=test#top")
        self.fetch()
        self.assertEqual(self.news.event_versions(item['id']),before)
        self.assertEqual(self.news.pending_analysis_tasks()['total'],count)
        self.assertEqual(self.news.get_item(item['id'])['evidence']['content_status'],'full_text')
        self.assertEqual(self.news.get_item(item['id'])['excerpt'],'Revenue was $10 million.')
        self.assertEqual(len(self.news.event_detail(item['id'])['observations']),2)

    def test_more_than_100_tasks_are_persisted_pageable_and_terminal_stays_terminal(self):
        for i in range(111):
            self.body=feed(guid=f'item-{i}',url=f'https://www.federalreserve.gov/release-{i}.htm')
            self.fetch()
        page=self.news.pending_analysis_tasks(limit=100)
        tail=self.news.pending_analysis_tasks(limit=100,offset=100)
        self.assertEqual(page['total'],111)
        self.assertEqual(len(tail['tasks']),11)
        self.assertEqual(self.news.pending_article_tasks()['total'],111)
        self.assertEqual(len(self.news.pending_article_tasks(offset=100)['tasks']),11)
        task=page['tasks'][0]
        self.assertTrue(self.news.transition_analysis_task(task['id'],'running',expected_status='pending'))
        self.assertFalse(self.news.transition_analysis_task(task['id'],'running',expected_status='pending'))
        self.assertEqual(self.news.recover_analysis_tasks(),1)
        self.assertFalse(self.news.transition_analysis_task(task['id'],'running'))
        reopened=NewsPlatform(self.path,clock=lambda:self.now)
        self.assertEqual(reopened.overview()['analysis_queue']['unknown'],1)
        self.assertEqual(reopened.pending_analysis_tasks()['total'],110)

    def test_existing_database_migration_creates_no_paid_tasks(self):
        item=self.fetch()
        with self.news._db() as db:
            db.execute('DELETE FROM news_analysis_tasks')
            db.execute('DELETE FROM news_article_tasks')
            db.execute('DELETE FROM news_article_versions')
        self.now+=100
        migrated=NewsPlatform(self.path,clock=lambda:self.now)
        self.assertEqual(migrated.pending_analysis_tasks()['total'],0)
        version=migrated.event_versions(item['id'])[0]
        self.assertTrue(version['migrated_legacy'])
        self.assertEqual(version['time_provenance']['source_updated_at'],'unknown')
        self.assertIsNone(migrated.event_detail(item['id'],as_of=self.now-1))

    def test_same_topic_different_announcements_not_merged_and_confirmed_reprints_are(self):
        original=self.fetch()
        self.body=feed(guid='two',url='https://www.federalreserve.gov/release-two.htm')
        other=self.fetch()
        self.assertNotEqual(self.news.event_detail(original['id'])['event_id'],self.news.event_detail(other['id'])['event_id'])
        self.news.save_article_evidence(other['id'],evidence(origin_url=original['url'],origin_confirmed=True))
        result=self.news.event_detail(other['id'])
        self.assertEqual(result['event_id'],self.news.event_detail(original['id'])['event_id'])
        self.assertEqual(len(result['sources']),2)
        self.assertEqual(result['independent_evidence_count'],1)
        self.assertEqual(next(s for s in result['sources'] if s['item_id']==other['id'])['relation'],'reprint')

    def test_read_failure_does_not_destroy_prior_body_and_cooldown_blocks_second_url(self):
        item=self.fetch()
        self.news.save_article_evidence(item['id'],evidence())
        self.body=feed(guid='two',url='https://www.federalreserve.gov/two.htm')
        other=self.fetch()
        def limited(*args):
            raise urllib.error.HTTPError(args[0],429,'limited',{'Retry-After':'2000'},None)
        self.news.article_transport=limited
        result=self.news.read_article(item['id'])
        self.assertEqual(result['evidence']['content_status'],'full_text')
        self.assertEqual(result['event']['last_fetch_attempt']['content_status'],'access_restricted')
        with self.assertRaisesRegex(ValueError,'cooldown'):
            self.news.read_article(other['id'])
        self.assertEqual(self.news.overview()['sources'][1]['enabled'],True)
        with self.news._db() as db:
            self.assertGreaterEqual(db.execute("SELECT not_before FROM sources WHERE id='fed'").fetchone()[0],self.now+2000)

    def test_html_challenge_is_failed_evidence_with_persistent_source_wait(self):
        item=self.fetch()
        self.news.article_transport=lambda *args: FeedResponse(200,b'<html><title>Just a moment...</title><body>Checking your browser</body></html>',{'Content-Type':'text/html'})
        result=self.news.read_article(item['id'])
        self.assertEqual(result['event']['last_fetch_attempt']['content_status'],'access_restricted')
        with self.assertRaisesRegex(ValueError,'cooldown'):
            self.news.read_article(item['id'])

    def test_withdrawal_is_review_task_and_attachment_change_is_a_new_version(self):
        item=self.fetch()
        initial=evidence(attachments=[{'url':'https://www.federalreserve.gov/table.htm','status':'read','evidence':{'body_text':'Revenue 10'}}])
        self.news.save_article_evidence(item['id'],initial)
        initial['attachments'][0]['evidence']['body_text']='Revenue 11'
        self.now+=10
        self.news.save_article_evidence(item['id'],initial)
        self.assertEqual(len(self.news.event_versions(item['id'])),3)
        self.news.save_article_evidence(item['id'],{'content_status':'retracted','body_text':'','attachments':[],'retraction_basis':'official withdrawal notice'})
        task=self.news.analysis_task_for_item(item['id'])
        self.assertEqual(task['status'],'needs_review')
        self.assertEqual(task['reason'],'source_retracted')
        self.assertTrue(self.news.get_item(item['id'])['event']['requires_review'])
        self.news.save_article_evidence(item['id'],evidence('Later fetched text does not prove a publisher reversed its withdrawal.'))
        self.assertTrue(self.news.get_item(item['id'])['event']['retracted'])
        self.assertEqual(self.news.get_item(item['id'])['evidence']['content_status'],'retracted')

    def test_source_modified_time_is_not_publication_and_new_sources_are_opt_in(self):
        entries=parse_feed(b'<feed><entry><title>Official update</title><updated>2026-10-29T01:00:00Z</updated><summary>One fact.</summary></entry></feed>',SOURCES[0])
        self.assertIsNone(entries[0]['published'])
        self.assertIsNotNone(entries[0]['source_updated_at'])
        with self.news._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0],10)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources WHERE enabled=1').fetchone()[0],3)
        self.news.configure_source('ir_mu',True,poll_interval=120)
        self.assertTrue(next(s for s in self.news.overview()['sources'] if s['id']=='ir_mu')['enabled'])

    def test_source_time_only_and_reverted_observation_preserve_history_without_fee_task(self):
        original=self.fetch()
        baseline_tasks=self.news.pending_analysis_tasks()['total']
        self.now+=10
        self.body=feed().replace(b'</description>',b'</description><updated>2026-10-29T02:00:00Z</updated>')
        self.fetch()
        changed=self.news.event_detail(original['id'])
        self.assertEqual(changed['version_count'],1)
        self.assertEqual(len(changed['observations']),2)
        self.assertIsNotNone(changed['observations'][0]['source_updated_at'])
        self.now+=10
        self.body=feed()
        self.fetch()
        self.assertEqual(len(self.news.event_detail(original['id'])['observations']),3)
        self.assertEqual(self.news.pending_analysis_tasks()['total'],baseline_tasks)

    def test_old_article_revision_uses_new_task_and_fake_model_exact_snapshot(self):
        item=self.fetch()
        calls=[]
        def generate(key,text):
            calls.append(json.loads(text))
            return SimpleNamespace(ok=True,model=MODEL,content=json.dumps({'summary':'公告更正了营收。','key_points':[{'claim':'营收经更正','evidence_field':'excerpt','quote':'Corrected revenue was $11 million.'}],'uncertainties':['仅依据给定摘录。']},ensure_ascii=False))
        analysis=NewsAnalysis(Path(self.directory.name)/'ai.sqlite',self.news,generate=generate)
        analysis.configure('OFFLINE-TEST-NOT-A-REAL-KEY',True,True)
        self.now=analysis._configured_at+1
        self.body=feed('Corrected revenue was $11 million.')
        self.fetch()
        self.news.control(True)
        task=self.news.pending_analysis_tasks(after=analysis._configured_at)['tasks'][0]
        analysis._analyze(item['id'],automatic=True,task_id=task['id'])
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['source_text']['excerpt'],'Corrected revenue was $11 million.')
        self.assertEqual(self.news.analysis_task_for_item(item['id'])['status'],'completed')
        analysis._analyze(item['id'],automatic=True,task_id=task['id'])
        self.assertEqual(len(calls),1)
        analysis.stop()

    def test_quality_unknown_denominators_are_null_and_no_coverage_claim(self):
        quality=self.news.overview()['quality']
        self.assertIsNone(quality['body_reads']['full_text_rate'])
        self.assertIsNone(quality['latency']['publication_to_discovery']['p50_seconds'])
        self.assertTrue(all(value['value'] is None and value['unavailable_reason'] for value in quality['judged_metrics'].values()))
        self.assertFalse(quality['coverage']['continuous_coverage'])
        self.assertEqual(quality['coverage']['status'],'unknown')

    def test_quality_latency_uses_only_known_publication_and_known_results(self):
        for index,delay in enumerate((60,120)):
            published=datetime.fromtimestamp(self.now-delay,timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')
            self.body=feed(guid=f'metric-{index}',url=f'https://www.federalreserve.gov/metric-{index}.htm').replace(b'Thu, 29 Oct 2026 01:00:00 GMT',published.encode())
            item=self.fetch()
        self.body=feed(guid='unknown',url='https://www.federalreserve.gov/unknown.htm').replace(b'<pubDate>Thu, 29 Oct 2026 01:00:00 GMT</pubDate>',b'')
        self.fetch()
        self.now+=20
        self.news.save_article_evidence(item['id'],evidence())
        task=self.news.analysis_task_for_item(item['id'])
        self.news.transition_analysis_task(task['id'],'running',expected_status='pending')
        self.now+=30
        self.news.transition_analysis_task(task['id'],'completed',expected_status='running',result_ref='local-fake-result-reference')
        quality=self.news.overview()['quality']
        latency=quality['latency']
        self.assertEqual(latency['publication_to_discovery']['sample_count'],2)
        self.assertAlmostEqual(latency['publication_to_discovery']['p50_seconds'],90,delta=1)
        self.assertEqual(latency['excluded_discovery_samples']['missing_source_publication'],1)
        self.assertEqual(latency['evidence_to_analysis_result']['p50_seconds'],30)
        self.assertEqual(quality['body_reads']['full_text_rate'],1)
        fed=next(source for source in quality['source_checks'] if source['source_id']=='fed')
        self.assertEqual(fed['successful'],3)
        self.assertEqual(fed['success_rate_denominator'],3)

    def test_automatic_body_policy_is_explicit_and_processes_saved_queue_without_model(self):
        item=self.fetch()
        calls=[]
        def article(*args):
            calls.append(args[0])
            return FeedResponse(200,b'<html><article><p>Revenue was $12 million, subject to the stated scope.</p></article></html>',{'Content-Type':'text/html'})
        self.news.article_transport=article
        self.assertFalse(self.news._next_article_task())
        self.news.control(True)
        self.assertFalse(self.news._next_article_task())
        self.news.configure_article_reading(True)
        version=self.news.get_item(item['id'])['current_version_id']
        self.assertTrue(self.news.article_reading_blocks_analysis(version))
        self.assertTrue(self.news._next_article_task())
        self.assertEqual(len(calls),1)
        self.assertEqual(self.news.get_item(item['id'])['evidence']['content_status'],'full_text')
        self.assertEqual(self.news.overview()['article_reading']['queue']['completed'],1)
        self.assertFalse(self.news._next_article_task())
        self.assertEqual(len(calls),1)

    def test_slow_article_does_not_block_different_source_discovery(self):
        self.fetch()
        entered,released,fed=threading.Event(),threading.Event(),threading.Event()
        def article(*args):
            entered.set()
            if not released.wait(3):
                raise TimeoutError('test_read_not_released')
            return FeedResponse(200,b'<article><p>New official body evidence.</p></article>',{'Content-Type':'text/html'})
        self.news.article_transport=article
        self.news.control(True)
        self.news.configure_article_reading(True)
        reader=threading.Thread(target=self.news._next_article_task)
        reader.start()
        poller=None
        try:
            self.assertTrue(entered.wait(1))
            self.body=feed(guid='bls-item',url='https://www.bls.gov/news.release/test.htm')
            def discover():
                self.fetch('bls')
                fed.set()
            poller=threading.Thread(target=discover)
            poller.start()
            self.assertTrue(fed.wait(1), 'different source discovery was blocked by body read')
            self.assertFalse(released.is_set())
        finally:
            released.set()
            reader.join(3)
            if poller:
                poller.join(3)
        self.assertFalse(reader.is_alive())

    def test_interrupted_body_get_reschedules_but_preserves_unknown_attempt(self):
        self.fetch()
        task=self.news.pending_article_tasks()['tasks'][0]
        with self.news._db() as db:
            db.execute("UPDATE news_article_tasks SET status='running' WHERE id=?",(task['id'],))
            db.execute("INSERT INTO news_article_task_attempts(task_id,started,status) VALUES(?,?,'running')",(task['id'],self.now))
        self.assertEqual(self.news.recover_article_tasks(),1)
        self.assertEqual(self.news.pending_article_tasks()['total'],1)
        with self.news._db() as db:
            self.assertEqual(db.execute('SELECT status FROM news_article_task_attempts').fetchone()[0],'interrupted_unknown')
        self.news.start()
        self.assertTrue(self.news._article_thread.is_alive())
        self.assertTrue(self.news.stop(timeout=2))
        self.assertFalse(self.news._article_thread.is_alive())

    def test_sec_contact_is_explicit_persisted_and_not_echoed_in_status(self):
        with self.assertRaisesRegex(ValueError,'sec_contact_required'):
            self.news.configure_source('sec_mu',True)
        for invalid in ('', 'missing-contact', 'App contact@example.invalid\r\nHeader: injected', '工具 contact@example.invalid'):
            with self.assertRaisesRegex(ValueError,'invalid_sec_contact'):
                self.news.configure_sec_contact(invalid)
        contact='OfflineTest contact@example.invalid'
        status=self.news.configure_sec_contact(contact)
        self.assertTrue(status['sec_contact_configured'])
        self.assertNotIn(contact,json.dumps(status))
        self.news.configure_source('sec_mu',True)
        reopened=NewsPlatform(self.path,clock=lambda:self.now)
        self.assertTrue(reopened.overview()['sec_contact_configured'])
        self.assertEqual(reopened.sec_user_agent,contact)

    def test_scheduled_body_revisit_detects_body_only_revision_without_recharging_unchanged(self):
        item=self.fetch()
        text=['Revenue was $12 million, subject to the stated financial reporting scope.']
        self.news.article_transport=lambda *args:FeedResponse(200,('<article><p>'+text[0]+'</p></article>').encode(),{'Content-Type':'text/html'})
        self.news.control(True)
        self.news.configure_article_reading(True)
        self.assertTrue(self.news._next_article_task())
        first=self.news.get_item(item['id'])
        versions=len(self.news.event_versions(item['id']))
        tasks=self.news.overview()['analysis_queue']['total']
        self.now+=899
        self.assertFalse(self.news._next_article_task())
        self.now+=1
        self.assertTrue(self.news._next_article_task())
        self.assertEqual(len(self.news.event_versions(item['id'])),versions)
        self.assertEqual(self.news.overview()['analysis_queue']['total'],tasks)
        text[0]='Corrected revenue was $13 million, subject to the stated financial reporting scope.'
        self.now+=900
        self.assertTrue(self.news._next_article_task())
        revised=self.news.get_item(item['id'])
        self.assertNotEqual(first['current_version_id'],revised['current_version_id'])
        self.assertEqual(self.news.analysis_task_for_item(item['id'])['trigger'],'body_evidence')
        self.assertEqual(len(self.news.event_versions(item['id'])),versions+1)
        with self.news._db() as db:
            attempts=db.execute('SELECT version_id,result_version_id,status FROM news_article_task_attempts ORDER BY seq').fetchall()
        self.assertEqual(len(attempts),3)
        self.assertEqual(attempts[-1]['result_version_id'],revised['current_version_id'])


if __name__ == '__main__':
    unittest.main()
