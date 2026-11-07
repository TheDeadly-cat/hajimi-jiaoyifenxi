"""Table revisions and historical lookup; all state is local, no generator calls."""
import copy
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ['AI_STUDIO_SKIP_LOCAL_ENV'] = '1'
from backend.news_article_evidence import extract_article
from backend.news_platform import NewsPlatform, analyze
from backend.news_platform_ai import NewsAnalysis
from backend import news_event_store as store

URL = 'https://investors.micron.com/news/press-release/2026/offline-sample/default.aspx'


def table_evidence(colspan=2, rowspan=1):
    raw = ('<article><p>Quarterly financial results, USD millions, unaudited and subject to stated conditions.</p>'
           f'<table><tr><th colspan="{colspan}" rowspan="{rowspan}">GAAP</th><th>Non-GAAP</th></tr>'
           '<tr><td>Quarter</td><td>Revenue</td><td>10</td></tr></table></article>').encode()
    return extract_article(raw, URL, 'ir_mu', now=1793232000)


class TableVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        guard = patch.object(socket.socket, 'connect', side_effect=AssertionError('network_forbidden'))
        guard.start(); self.addCleanup(guard.stop)
        self.now = 1793232000.0
        self.path = Path(self.temp.name) / 'news.sqlite'
        self.news = NewsPlatform(self.path, clock=lambda: self.now)
        self.addCleanup(self.news.stop)
        self.item_id = self.insert_item('table-test', URL)

    def insert_item(self, identity, url):
        with self.news._db() as db:
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (identity, 'ir_mu', identity, 'Offline financial sample', url, 'Saved local sample only.',
                        self.now, self.now, self.now,
                        json.dumps(analyze('Offline financial sample', 'Saved local sample only.', self.now, self.now, 'ir_mu'))))
        return identity

    def save(self, evidence):
        self.now += 1
        return self.news.save_article_evidence(self.item_id, copy.deepcopy(evidence))

    def stored(self, version_id):
        with self.news._db() as db:
            return dict(db.execute('SELECT * FROM news_article_versions WHERE id=?', (version_id,)).fetchone())

    def task_count(self):
        with self.news._db() as db:
            return db.execute('SELECT COUNT(*) FROM news_analysis_tasks').fetchone()[0]

    def saved_lookup(self, db, item):
        # Invoke only saved-result retrieval, without an analysis instance/client.
        context = SimpleNamespace(news=self.news, _version_fingerprint=NewsAnalysis._version_fingerprint)
        return NewsAnalysis._saved(context, db, item)

    def result_db(self):
        db = sqlite3.connect(':memory:'); db.row_factory = sqlite3.Row
        db.executescript('CREATE TABLE runs(fingerprint TEXT PRIMARY KEY,status TEXT,result TEXT);'
                         'CREATE TABLE reading_identity_aliases(alias TEXT PRIMARY KEY,target TEXT);')
        self.addCleanup(db.close)
        return db

    def test_colspan_and_rowspan_only_changes_append_versions_preserving_old_snapshot(self):
        a = self.save(table_evidence())
        original = self.stored(a['current_version_id'])
        before_body = a['evidence']['body_text']
        b = self.save(table_evidence(colspan=1))
        c = self.save(table_evidence(colspan=1, rowspan=2))
        self.assertEqual(before_body, b['evidence']['body_text'])
        self.assertEqual(before_body, c['evidence']['body_text'])
        self.assertEqual(len(self.news.event_versions(self.item_id)), 3)
        self.assertEqual(self.stored(a['current_version_id']), original)
        self.assertEqual(len({v['evidence_package_id'] for v in (a, b, c)}), 3)
        for item in (b, c):
            self.assertIn('table_structure', [d['field'] for d in item['event']['current_version']['changes']])
            self.assertEqual(len(self.news.equivalent_reading_versions(self.item_id, item['current_version_id'])), 1)

    def test_cosmetic_locators_versions_and_whitespace_are_unchanged(self):
        value = table_evidence(); first = self.save(value); count = self.task_count()
        value['extractor_version'] = 'later-extractor'
        value['fetched_at'] = '2026-10-30T00:00:00Z'
        value['body_text'] = '  ' + value['body_text'].replace(' ', '  ') + '  '
        for key in ('segments', 'tables'):
            for i, row in enumerate(value[key]):
                row['id'] = f'new-{i}'
                row['row_index'] = i + 100
                if row.get('kind') == 'table_row':
                    row['table_id'] = 'renumbered-table'
                    for cell in row['cells']:
                        cell['text'] = '  ' + cell['text'] + '  '
        second = self.save(value)
        self.assertEqual(second['current_version_id'], first['current_version_id'])
        self.assertEqual(self.task_count(), count)

    def test_pre_fix_text_hash_with_saved_cells_is_compared_from_snapshot(self):
        first = self.save(table_evidence())
        # Synthetic pre-fix row: the snapshot has cells but the old hash omitted them.
        item = first
        legacy = {'title': item['title'], 'excerpt': item['excerpt'], 'url': item['url'],
                  'body_text': store.normalized_text(item['evidence']['body_text']), 'content_status': 'full_text', 'attachments': []}
        with self.news._db() as db:
            db.execute('UPDATE news_article_versions SET content_hash=? WHERE id=?', (store.digest(legacy), first['current_version_id']))
        frozen = self.stored(first['current_version_id']); count = self.task_count()
        self.assertEqual(self.save(table_evidence())['current_version_id'], first['current_version_id'])
        self.assertEqual(self.stored(first['current_version_id']), frozen)
        self.assertEqual(self.task_count(), count)
        self.assertNotEqual(self.save(table_evidence(colspan=1))['current_version_id'], first['current_version_id'])

    def test_missing_legacy_structure_is_review_only_and_preserves_unknown(self):
        full = table_evidence(); legacy = copy.deepcopy(full)
        for key in ('segments', 'tables'):
            for row in legacy[key]:
                for field in ('cells', 'table_id', 'row_index'): row.pop(field, None)
        first = self.save(legacy)
        task = self.news.analysis_task_for_item(self.item_id)
        self.news.transition_analysis_task(task['id'], 'running', expected_status='pending')
        self.news.transition_analysis_task(task['id'], 'unknown', expected_status='running')
        original = self.stored(first['current_version_id'])
        second = self.save(full)
        current = self.news.analysis_task_for_item(self.item_id)
        self.assertEqual(current['status'], 'needs_review')
        self.assertEqual(current['reason'], 'table_structure_baseline_unverified')
        self.assertEqual(self.news.pending_analysis_tasks()['total'], 0)
        self.assertEqual(self.stored(first['current_version_id']), original)
        self.assertEqual(len(self.news.equivalent_reading_versions(self.item_id, second['current_version_id'])), 1)
        with self.news._db() as db:
            self.assertEqual(db.execute('SELECT status FROM news_analysis_tasks WHERE id=?', (task['id'],)).fetchone()[0], 'unknown')

    def test_same_source_bytes_reinterpretation_does_not_auto_requeue(self):
        full = table_evidence(); self.save(full)
        changed = table_evidence(colspan=1); changed['raw_content_hash'] = full['raw_content_hash']
        self.save(changed)
        self.assertEqual(self.news.analysis_task_for_item(self.item_id)['reason'], 'table_structure_reinterpreted_same_bytes')
        self.assertEqual(self.news.pending_analysis_tasks()['total'], 0)

    def test_attachment_table_structure_is_material_even_with_same_text(self):
        full = table_evidence()
        full['attachments'] = [{'url': URL+'/attachment.htm', 'status': 'full_text', 'read': True, 'evidence': table_evidence()}]
        first = self.save(full)
        full['attachments'][0]['evidence'] = table_evidence(rowspan=2)
        second = self.save(full)
        self.assertNotEqual(first['current_version_id'], second['current_version_id'])
        self.assertNotEqual(first['evidence_package_id'], second['evidence_package_id'])

    def test_conflicting_cached_table_view_requires_review(self):
        full = json.loads(json.dumps(table_evidence()))
        self.save(full)
        full['tables'][0]['cells'][0]['colspan'] = 4
        changed = self.save(full)
        self.assertEqual(self.news.analysis_task_for_item(self.item_id)['reason'], 'table_structure_conflicting_views')
        self.assertEqual(changed['event']['current_version']['structure_review'], 'table_structure_conflicting_views')

    def test_reversion_cannot_direct_hit_old_result_or_auto_pay_again(self):
        first = self.save(table_evidence()); self.save(table_evidence(colspan=1)); third = self.save(table_evidence())
        self.assertNotEqual(first['evidence_package_id'], third['evidence_package_id'])
        self.assertEqual(self.news.analysis_task_for_item(self.item_id)['reason'], 'previous_table_material_reappeared')
        db = self.result_db()
        db.execute('INSERT INTO runs VALUES(?,?,?)', (NewsAnalysis._version_fingerprint(first), 'completed', '{"summary":"Synthetic stored result"}'))
        self.assertIsNone(self.saved_lookup(db, third))
        self.assertEqual(self.saved_lookup(db, first)['status'], 'completed')

    def test_cross_structure_alias_is_ignored_and_history_survives_reopen(self):
        first = self.save(table_evidence()); second = self.save(table_evidence(rowspan=2))
        db = self.result_db(); old_key = NewsAnalysis._version_fingerprint(first); new_key = NewsAnalysis._version_fingerprint(second)
        db.execute('INSERT INTO runs VALUES(?,?,?)', (old_key, 'unknown', '{"summary":"Synthetic prior unknown"}'))
        db.execute('INSERT INTO reading_identity_aliases VALUES(?,?)', (new_key, old_key))
        self.assertIsNone(self.saved_lookup(db, second))
        self.news = NewsPlatform(self.path, clock=lambda:self.now)
        self.addCleanup(self.news.stop)
        self.assertIsNone(self.saved_lookup(db, self.news.get_item(self.item_id)))
        self.assertEqual(self.saved_lookup(db, first)['status'], 'unknown')
        self.assertEqual(len(self.news.event_versions(self.item_id)), 2)

    def test_provenance_only_version_recovers_saved_result_without_alias_cache(self):
        first = self.save(table_evidence())
        full = table_evidence(); full.update(origin_url='https://investors.micron.com/confirmed-origin', origin_confirmed=True)
        second = self.save(full)
        db = self.result_db()
        db.execute('INSERT INTO runs VALUES(?,?,?)', (NewsAnalysis._version_fingerprint(first), 'failed', '{"summary":"Synthetic saved failure"}'))
        self.assertEqual(self.saved_lookup(db, second)['status'], 'failed')
        self.assertEqual(len(self.news.equivalent_reading_versions(self.item_id, second['current_version_id'])), 2)

    def test_loss_of_structure_cannot_return_to_legacy_completed_package(self):
        full = table_evidence(); flat = copy.deepcopy(full)
        for key in ('segments', 'tables'):
            for row in flat[key]: row.pop('cells', None)
        first = self.save(flat); self.save(full); restored = self.save(flat)
        self.assertNotEqual(first['evidence_package_id'], restored['evidence_package_id'])
        self.assertEqual(self.news.analysis_task_for_item(self.item_id)['status'], 'needs_review')
        db = self.result_db()
        db.execute('INSERT INTO runs VALUES(?,?,?)', (NewsAnalysis._version_fingerprint(first), 'completed', '{}'))
        self.assertIsNone(self.saved_lookup(db, restored))

    def test_unread_unchanged_attachment_does_not_break_same_bytes_proof(self):
        full = table_evidence(); changed = table_evidence(colspan=1)
        attachment = {'url': URL+'/unread.pdf', 'status': 'not_read', 'read': False}
        full['attachments'] = [attachment]; changed['attachments'] = [attachment]
        changed['raw_content_hash'] = full['raw_content_hash']
        self.save(full); self.save(changed)
        self.assertEqual(self.news.analysis_task_for_item(self.item_id)['reason'], 'table_structure_reinterpreted_same_bytes')
        self.assertEqual(self.news.pending_analysis_tasks()['total'], 0)

    def test_unknown_legacy_structure_with_changed_separators_waits_for_review(self):
        full = table_evidence(); flat = copy.deepcopy(full)
        for key in ('segments', 'tables'):
            for row in flat[key]: row.pop('cells', None)
        flat.pop('raw_content_hash')
        flat['body_text'] = flat['body_text'].replace(' | ', ' ')
        self.save(flat); self.save(full)
        self.assertEqual(self.news.analysis_task_for_item(self.item_id)['reason'], 'table_structure_baseline_unverified')
        self.assertEqual(self.news.pending_analysis_tasks()['total'], 0)


if __name__ == '__main__':
    unittest.main()
