"""Ten synthetic saved citations; no media integration, fetch, or model execution."""
from contextlib import ExitStack, contextmanager
import copy
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from backend.news_platform import NewsPlatform, analyze
from backend.news_platform_routes import news_response
from backend.news_platform_ai import NewsAnalysis
from backend import news_analysis_providers as providers
from backend.news_sources import source_registry, source_definition


TITLE = 'OFFLINE SYNTHETIC FIXTURE: Micron operating update'
BODY = ('Synthetic fixture, not a real publication. Micron FY2026-Q3 earnings: '
        'reported revenue was 10 million fixture dollars, subject to the stated scope.')


class SavedReprintFixture:
    """Only fixture rows in one caller-supplied isolated database are added."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.now = 1793220000.0
        self.news = NewsPlatform(self.directory / 'news.sqlite', clock=lambda: self.now,
                                 transport=self.no_fetch, article_transport=self.no_fetch)

    @staticmethod
    def no_fetch(*args, **kwargs):
        raise AssertionError('synthetic_fixture_never_fetches')

    def rows(self, table):
        with self.news._db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM "' + table.replace('"', '""') + '" ORDER BY rowid')]

    def snapshot(self):
        return {name: self.rows(name) for name in ('sources', 'items', 'news_article_versions',
                'news_relation_annotations', 'news_article_fetches', 'news_analysis_tasks', 'news_article_tasks')}

    def body(self, text):
        return {'content_status': 'full_text', 'body_text': text,
                'segments': [{'id': 'p1', 'text': text, 'kind': 'paragraph'}], 'attachments': [],
                'content_hash': hashlib.sha256(text.encode()).hexdigest(),
                'extractor_version': 'synthetic-offline-fixture-v1', 'fixture_only': True, 'symbols': ['US.MU']}

    def add(self, key, publisher, text=BODY, period='FY2026-Q3', event='earnings'):
        item_id = 'fixture-' + key
        source_id = f'offline-fixture-publisher-{publisher:02d}'
        url = f'https://publisher-{publisher:02d}.example.invalid/{period}/{event}/{key}'
        self.now += 1
        with self.news._db() as db:
            db.execute('INSERT OR IGNORE INTO sources(id,name,url,enabled) VALUES(?,?,?,0)',
                       (source_id, f'SYNTHETIC OFFLINE publisher {publisher:02d}',
                        f'https://publisher-{publisher:02d}.example.invalid/fixture-no-feed'))
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (item_id, source_id, item_id, TITLE, url, '', self.now - 10, self.now, self.now,
                        json.dumps(analyze(TITLE, '', self.now - 10, self.now, source_id))))
        return self.news.save_article_evidence(item_id, self.body(text))

    def ten(self):
        return [self.add('original' if index == 0 else f'reprint-{index:02d}', index + 1) for index in range(10)]

    def confirm(self, item_id, original_id):
        self.now += 1
        value, status = news_response(self.news, '/api/news/items/' + item_id + '/event-relation', method='POST',
                                     payload={'origin_item_id': original_id, 'acknowledge': True})
        if status != 200:
            raise AssertionError(('confirmation_route_failed', status, value))
        return value['item']

    def link_all(self, items):
        for item in items[1:]:
            self.confirm(item['id'], items[0]['id'])

    def detail(self, item_id):
        value, status = news_response(self.news, '/api/news/items/' + item_id)
        if status != 200:
            raise AssertionError(('detail_route_failed', status, value))
        return value['item']

    def close(self):
        self.news.stop(timeout=0)


class ReadOnlySavedResults:
    """Use product saved-result lookup without constructing a model controller."""
    _version_fingerprint = staticmethod(NewsAnalysis._version_fingerprint)
    _input = staticmethod(NewsAnalysis._input)
    _legacy_fingerprint = staticmethod(NewsAnalysis._legacy_fingerprint)
    _rss_v2_fingerprint = staticmethod(NewsAnalysis._rss_v2_fingerprint)
    _rss_v3_fingerprint = staticmethod(NewsAnalysis._rss_v3_fingerprint)
    _rss_v3_route_fingerprint = staticmethod(NewsAnalysis._rss_v3_route_fingerprint)
    _route_fingerprint = staticmethod(NewsAnalysis._route_fingerprint)
    _fingerprint = NewsAnalysis._fingerprint

    def __init__(self, news, ledger):
        self.news, self.ledger = news, ledger

    @contextmanager
    def _db(self):
        yield self.ledger

    def _saved(self, db, item):
        return NewsAnalysis._saved(self, db, item)

    def result(self, item):
        return NewsAnalysis.result(self, item)

    def version_result(self, current, version):
        return NewsAnalysis.version_result(self, current, version)

    def retain_equivalent_reading(self, before, after):
        return NewsAnalysis.retain_equivalent_reading(self, before, after)


class TenSourceReprintTests(unittest.TestCase):
    # The external acceptance runner may set this to a fresh output directory.
    # Normal repository tests leave no artifacts outside their temporary folder.
    artifact_root = None

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='offline-ten-source-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.attempts = {'network': 0, 'model_constructor': 0, 'provider': 0, 'thread_start': 0, 'database_outside_fixture': 0}
        self.notes = {}

        def deny(kind):
            def blocked(*args, **kwargs):
                self.attempts[kind] += 1
                raise AssertionError(kind + '_forbidden')
            return blocked

        for target, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'), (socket.socket, 'sendto'),
                             (socket, 'getaddrinfo'), (socket, 'create_connection'),
                             (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            self.guards.enter_context(patch.object(target, name, side_effect=deny('network')))
        self.guards.enter_context(patch.object(NewsAnalysis, '__init__', side_effect=deny('model_constructor')))
        self.guards.enter_context(patch.object(providers, 'generate_response', side_effect=deny('provider')))
        self.guards.enter_context(patch.object(threading.Thread, 'start', side_effect=deny('thread_start')))
        real_connect = sqlite3.connect

        def isolated_connect(path, *args, **kwargs):
            if kwargs.get('uri') or not Path(path).resolve().is_relative_to(self.root):
                return deny('database_outside_fixture')()
            return real_connect(path, *args, **kwargs)

        self.guards.enter_context(patch.object(sqlite3, 'connect', side_effect=isolated_connect))
        self.registry_before = source_registry()
        self.fixture = SavedReprintFixture(self.root)
        self.news = self.fixture.news
        self.addCleanup(self.fixture.close)
        self.ledger = sqlite3.connect(self.root / 'synthetic-saved-results.sqlite')
        self.ledger.row_factory = sqlite3.Row
        self.ledger.executescript('CREATE TABLE runs(fingerprint TEXT PRIMARY KEY,status TEXT,result TEXT);'
                                 'CREATE TABLE reading_identity_aliases(alias TEXT PRIMARY KEY,target TEXT);')
        self.addCleanup(self.ledger.close)
        self.addCleanup(self.export_artifacts)

    def export_artifacts(self):
        if self.artifact_root is None:
            return
        directory = Path(self.artifact_root) / self._testMethodName
        directory.mkdir(exist_ok=False)
        def save(name, value):
            with (directory / name).open('x', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
        save('fixture-state.json', self.fixture.snapshot())
        save('checkpoints.json', self.notes)
        save('guards.json', self.attempts)
        save('saved-results.json', [dict(row) for row in self.ledger.execute('SELECT * FROM runs ORDER BY fingerprint')])
        save('scope.json', {'synthetic_only': True, 'real_media_sources_connected': 0,
                           'source_registry_unchanged': source_registry() == self.registry_before,
                           'model_calls': 0, 'model_instances': 0,
                           'scope': 'Synthetic saved citations and seeded saved results; explicit confirmation remains a human assertion.'})
        self.ledger.commit()
        for filename in ('news.sqlite', 'synthetic-saved-results.sqlite'):
            shutil.copyfile(self.root / filename, directory / filename)

    def tearDown(self):
        self.assertEqual(source_registry(), self.registry_before)
        self.assertTrue(all(count == 0 for count in self.attempts.values()), self.attempts)

    def immutable_versions(self):
        return {row['id']: row for row in self.fixture.rows('news_article_versions')}

    def seed_result(self, item, status):
        key = NewsAnalysis._version_fingerprint(item)
        result = {'fixture_only': True, 'analysis_performed': False, 'article_version_id': item['current_version_id'],
                  'summary': 'Seeded synthetic saved result; no model was called.'}
        self.ledger.execute('INSERT INTO runs VALUES(?,?,?)', (key, status, json.dumps(result, sort_keys=True)))
        self.ledger.commit()
        task = self.news.analysis_task_for_item(item['id'])
        self.assertTrue(self.news.transition_analysis_task(task['id'], status, expected_status='pending',
                                                          result_ref=key, reason='synthetic_saved_result_fixture'))
        return dict(next(row for row in self.fixture.rows('news_analysis_tasks') if row['id'] == task['id']))

    def test_ten_saved_outlets_confirm_one_basis_without_losing_citations_or_old_versions(self):
        items = self.fixture.ten()
        original = items[0]
        initial = self.immutable_versions()
        cutoff = self.fixture.now
        self.assertEqual(len({item['event']['event_id'] for item in items}), 10)
        self.fixture.link_all(items)
        detail = self.fixture.detail(original['id'])
        event = detail['event']
        self.assertEqual(event['independent_evidence_count'], 1)
        self.assertEqual({row['item_id'] for row in event['sources']}, {item['id'] for item in items})
        self.assertEqual(len({row['source_id'] for row in event['sources']}), 10)
        self.assertEqual(len({row['url'] for row in event['sources']}), 10)
        self.assertTrue(all(row['source_name'].startswith('SYNTHETIC OFFLINE') for row in event['sources']))
        self.assertTrue(all(source_definition(row['source_id']) is None for row in event['sources']))
        self.assertEqual(sum(row['relation'] == 'reprint' for row in event['sources']), 9)
        current = self.immutable_versions()
        self.assertEqual({key: current[key] for key in initial}, initial)
        self.assertEqual(len(current), 19)
        tasks = self.fixture.rows('news_analysis_tasks')
        self.assertEqual(len(tasks), 10)
        self.assertEqual(sum(row['status'] == 'pending' for row in tasks), 1)
        self.assertEqual(sum(row['status'] == 'skipped' for row in tasks), 9)
        for item in items:
            self.assertEqual(self.fixture.detail(item['id'])['event']['independent_evidence_count'], 1)
            old = self.news.event_detail(item['id'], as_of=cutoff)
            self.assertEqual(len(old['sources']), 1)
        reopened = NewsPlatform(self.news.path, clock=lambda: self.fixture.now,
                                transport=SavedReprintFixture.no_fetch, article_transport=SavedReprintFixture.no_fetch)
        self.addCleanup(reopened.stop)
        self.assertEqual(len(reopened.event_detail(original['id'])['sources']), 10)
        self.assertEqual(reopened.event_detail(original['id'])['independent_evidence_count'], 1)
        self.notes.update(initial_versions=list(initial.values()), confirmed_detail=detail,
                          after_reopen=reopened.event_detail(original['id']))

    def test_repeated_confirmation_records_audit_without_new_versions_tasks_or_body_fetches(self):
        items = self.fixture.ten()
        self.fixture.link_all(items)
        tables = ('news_article_versions', 'news_analysis_tasks', 'news_article_tasks', 'news_article_fetches')
        before = {name: self.fixture.rows(name) for name in tables}
        version_ids = {item['id']: self.news.get_item(item['id'])['current_version_id'] for item in items}
        self.fixture.link_all(items)
        self.assertEqual({name: self.fixture.rows(name) for name in tables}, before)
        self.assertEqual({item['id']: self.news.get_item(item['id'])['current_version_id'] for item in items}, version_ids)
        self.assertEqual(len(self.fixture.rows('news_relation_annotations')), 18)
        for item in items[1:]:
            annotations = self.news.event_detail(item['id'])['relation_annotations']
            self.assertEqual(len(annotations), 2)
            self.assertTrue(all(row['confirmation']['independently_verified'] is False for row in annotations))
        self.notes.update(version_ids_unchanged=version_ids, repeated_confirmation_detail=self.fixture.detail(items[-1]['id']))

    def test_same_company_same_title_different_period_and_event_do_not_automatically_merge(self):
        items = self.fixture.ten()
        self.fixture.link_all(items)
        previous_period = self.fixture.add('different-period', 1,
            'Synthetic fixture only. Micron FY2026-Q2 earnings: reported revenue was 10 million fixture dollars.', period='FY2026-Q2')
        different_event = self.fixture.add('different-event', 1,
            'Synthetic fixture only. Micron FY2026-Q3 factory proposal: planned investment is conditional, not completed.', event='factory-proposal')
        self.assertEqual(previous_period['title'], different_event['title'])
        ids = {self.news.event_detail(item['id'])['event_id'] for item in (items[0], previous_period, different_event)}
        self.assertEqual(len(ids), 3)
        for item in (previous_period, different_event):
            detail = self.fixture.detail(item['id'])['event']
            self.assertEqual(detail['independent_evidence_count'], 1)
            self.assertEqual([row['item_id'] for row in detail['sources']], [item['id']])
        before = self.fixture.snapshot()
        response, status = news_response(self.news, '/api/news/items/' + previous_period['id'] + '/event-relation',
            method='POST', payload={'origin_item_id': items[0]['id'], 'acknowledge': False})
        self.assertEqual(status, 400)
        self.assertFalse(response['ok'])
        self.assertEqual(self.fixture.snapshot(), before)
        self.notes.update(original_event=self.fixture.detail(items[0]['id'])['event'],
                          different_period=self.fixture.detail(previous_period['id']),
                          different_event=self.fixture.detail(different_event['id']))

    def test_correction_and_withdrawal_preserve_old_confirmations_and_seeded_saved_results(self):
        items = self.fixture.ten()
        original, reprint = items[:2]
        completed_task = self.seed_result(original, 'completed')
        unknown_task = self.seed_result(reprint, 'unknown')
        old_results = [dict(row) for row in self.ledger.execute('SELECT * FROM runs ORDER BY fingerprint')]
        initial_versions = self.immutable_versions()
        self.fixture.link_all(items)
        confirmed = self.news.get_item(reprint['id'])
        old_annotation = copy.deepcopy(confirmed['event']['relation_annotations'][0])
        self.fixture.now += 1
        corrected = self.news.save_article_evidence(original['id'], self.fixture.body(
            'Synthetic correction only. Micron FY2026-Q3 reported revenue is 11 million fixture dollars; the prior 10 was corrected.'))
        self.assertNotEqual(corrected['current_version_id'], original['current_version_id'])
        self.assertTrue(corrected['event']['requires_review'])
        copy_before_repeat = self.news.get_item(reprint['id'])
        task_count = len(self.fixture.rows('news_analysis_tasks'))
        repeated = self.fixture.confirm(reprint['id'], original['id'])
        self.assertEqual(repeated['current_version_id'], copy_before_repeat['current_version_id'])
        self.assertEqual(len(self.fixture.rows('news_analysis_tasks')), task_count)
        self.assertEqual(repeated['event']['relation_annotations'][-1], old_annotation)
        self.assertEqual(repeated['event']['latest_relation_annotation']['confirmation']['original_version_id'], corrected['current_version_id'])
        corrected_at = self.fixture.now
        notice_text = 'Synthetic publisher notice: this FY2026-Q3 announcement was withdrawn by its publisher.'
        wrong_notice = self.fixture.add('wrong-publisher-notice', 12, notice_text, event='withdrawal')
        before_reject = self.immutable_versions()
        response, code = news_response(self.news, '/api/news/items/' + original['id'] + '/withdrawal', method='POST',
            payload={'evidence_item_id': wrong_notice['id'], 'quote': notice_text, 'acknowledge': True})
        self.assertEqual(code, 400)
        self.assertEqual(self.immutable_versions(), before_reject)
        notice = self.fixture.add('publisher-withdrawal-notice', 1, notice_text, event='withdrawal')
        self.fixture.now += 1
        response, code = news_response(self.news, '/api/news/items/' + original['id'] + '/withdrawal', method='POST',
            payload={'evidence_item_id': notice['id'], 'quote': notice_text, 'acknowledge': True})
        self.assertEqual(code, 200)
        withdrawn = response['item']
        self.assertEqual(withdrawn['evidence']['content_status'], 'retracted')
        self.assertTrue(withdrawn['event']['requires_review'])
        self.assertFalse(self.news.event_detail(original['id'], as_of=corrected_at)['retracted'])
        for item in items:
            detail = self.fixture.detail(item['id'])['event']
            self.assertTrue(detail['retracted'])
            self.assertEqual(detail['independent_evidence_count'], 1)
            self.assertEqual(len(detail['sources']), 10)
        current_rows = self.immutable_versions()
        self.assertEqual({key: current_rows[key] for key in initial_versions}, initial_versions)
        tasks = {row['id']: row for row in self.fixture.rows('news_analysis_tasks')}
        self.assertEqual(tasks[completed_task['id']], completed_task)
        self.assertEqual(tasks[unknown_task['id']], unknown_task)
        self.assertEqual([dict(row) for row in self.ledger.execute('SELECT * FROM runs ORDER BY fingerprint')], old_results)
        self.news.ai = ReadOnlySavedResults(self.news, self.ledger)
        history, code = news_response(self.news, '/api/news/items/' + original['id'] + '/versions')
        self.assertEqual(code, 200)
        by_id = {row['id']: row for row in history['versions']}
        self.assertEqual(by_id[original['current_version_id']]['ai_analysis']['status'], 'completed')
        self.assertTrue(by_id[original['current_version_id']]['ai_analysis']['fixture_only'])
        self.assertIsNone(by_id[corrected['current_version_id']]['ai_analysis'])
        self.assertEqual(self.fixture.detail(original['id'])['ai_analysis']['status'], 'needs_review')
        copy_history, code = news_response(self.news, '/api/news/items/' + reprint['id'] + '/versions')
        self.assertEqual(code, 200)
        old_copy = next(row for row in copy_history['versions'] if row['id'] == reprint['current_version_id'])
        self.assertEqual(old_copy['ai_analysis']['status'], 'unknown')
        self.notes.update(old_annotation=old_annotation, corrected=corrected, withdrawn=withdrawn,
                          original_history_with_saved_results=history, reprint_history_with_saved_results=copy_history,
                          terminal_tasks_preserved=[completed_task, unknown_task])


if __name__ == '__main__':
    unittest.main()
