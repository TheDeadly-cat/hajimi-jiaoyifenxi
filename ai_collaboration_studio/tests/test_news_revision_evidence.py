"""Saved-version comparisons with real isolated snapshots; no model or network."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.news_article_evidence import extract_article
from backend.news_platform import NewsPlatform, analyze
from backend.news_evidence_reading import build_input, locate_result, INSTRUCTIONS, TEXT_FORMAT
from backend.news_evidence_groups import encoded_size
from backend.news_revision_evidence import candidate, locate, UNKNOWN
from backend.news_content_quality import validate_output
from backend.news_market_context import resolve_saved_context, build_market_input, project_result, validate_output as validate_market
from backend.news_market_replay import historical_item
from backend import news_analysis_providers as providers

URL = 'https://investors.micron.com/synthetic-version-comparison'
NOW = 1793203200.0

def document(value, padding=''):
    return ('<article><p>Synthetic quarterly release, only for offline verification.</p>'
            '<table><tr><th colspan="2">Q1 amounts in USD millions; unaudited (1)</th></tr>'
            '<tr><td>Reported revenue</td><td>' + str(value) + '</td></tr></table>'
            '<p>(1) Excludes the specified discontinued activity.</p><p>' + padding + '</p></article>').encode()


class RevisionEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='news-revision-evidence-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = NOW
        for target, name in ((socket.socket, 'connect'), (socket, 'getaddrinfo'), (providers, 'generate_response')):
            guard = patch.object(target, name, side_effect=AssertionError('offline_only'))
            guard.start(); self.addCleanup(guard.stop)
        self.news = NewsPlatform(Path(temp.name) / 'news.sqlite', clock=lambda: self.now,
                                 transport=lambda *a: self.fail('no_source_fetch'))
        with self.news._db() as db:
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('revision', 'ir_mu', 'revision', 'Synthetic quarterly release', URL, '', self.now - 100, self.now, self.now,
                        json.dumps(analyze('Synthetic quarterly release', '', self.now - 100, self.now, 'ir_mu'))))

    def save(self, value, padding=''):
        self.now += 10
        return self.news.save_article_evidence('revision', extract_article(document(value, padding), URL, 'ir_mu', now=self.now))

    def pair(self):
        first = self.save(10)
        second = self.save(12)
        return first, second

    def test_immutable_predecessor_reaches_current_task_and_historical_item_without_future_backfill(self):
        first, second = self.pair()
        cutoff = second['version_seen_at']
        task = self.news.analysis_task_for_item('revision')
        frozen = self.news.analysis_task_item(task['id'])
        self.assertEqual(frozen['event']['previous_version']['id'], first['current_version_id'])
        third = self.save(99)
        historical = historical_item(self.news, 'revision', second['current_version_id'], cutoff)
        self.assertEqual(historical['event']['previous_version']['id'], first['current_version_id'])
        material = build_input(historical, checked_at=self.now)
        self.assertEqual(material['revision_comparison']['status'], 'available')
        self.assertNotIn('99', material['source_text']['revision'])
        self.assertNotEqual(third['current_version_id'], second['current_version_id'])
        self.assertEqual(self.news.analysis_task_item(task['id'])['event']['previous_version'], frozen['event']['previous_version'])

    def test_complete_table_pairs_keep_headers_footnotes_geometry_and_exact_version_locators(self):
        first, second = self.pair()
        original = copy.deepcopy(second)
        material = build_input(second, checked_at=self.now)
        comparison = material['revision_comparison']
        self.assertEqual(comparison['status'], 'available')
        self.assertEqual({row['role'] for row in comparison['selection']}, {'before', 'after'})
        for doc in comparison['documents']:
            rows = [row for row in comparison['selection'] if row['role'] == doc['role']]
            self.assertEqual(doc['required_segment_ids'], [row['id'] for row in rows])
            self.assertTrue(any(cell['colspan'] == 2 for row in rows for cell in row.get('cells', [])))
            text = '\n'.join(material['source_text']['revision'][row['start']:row['end']] for row in rows)
            self.assertIn('Q1 amounts in USD millions; unaudited (1)', text)
            self.assertIn('Excludes the specified discontinued activity.', text)
        quote = 'Reported revenue | 10'
        locator = locate({'quote': quote}, material)
        self.assertEqual(locator['article_version_id'], first['current_version_id'])
        self.assertEqual(locator['revision_role'], 'before')
        raw = json.dumps({'summary': '已保留旧版和新版原文以供对照。', 'key_points': [
            {'claim': '此处为旧版原句。', 'quote': quote, 'evidence_field': 'revision'}],
            'uncertainties': ['没有独立核实原文声明。']}, ensure_ascii=False)
        result = locate_result(validate_output(raw, material), material)
        self.assertEqual(result['evidence'][0]['locator'], locator)
        self.assertEqual(second, original)
        wire = providers.encode_body(providers.build_body('doubao', providers.DOUBAO_MODEL,
            json.dumps(material, ensure_ascii=False, sort_keys=True), INSTRUCTIONS, TEXT_FORMAT))
        self.assertLessEqual(len(wire), providers.MAX_REQUEST_BYTES)

    def test_low_budgets_never_retain_only_one_side_or_lose_source_dependencies(self):
        _, second = self.pair()
        saw_available = saw_omitted = False
        for cap in range(3000, 12001, 500):
            try:
                material = build_input(second, checked_at=self.now, max_package_bytes=cap)
            except ValueError as error:
                self.assertEqual(str(error), 'evidence_budget_smaller_than_metadata'); continue
            self.assertLessEqual(encoded_size(material), cap)
            comparison = material.get('revision_comparison') or {}
            if comparison.get('status') == 'available':
                saw_available = True
                self.assertEqual({row['role'] for row in comparison['selection']}, {'before', 'after'})
                self.assertIn('Reported revenue | 10', material['source_text']['revision'])
                self.assertIn('Reported revenue | 12', material['source_text']['revision'])
            else:
                saw_omitted = True
                self.assertNotIn('revision', material['source_text'])
                self.assertNotIn('selection', comparison)
        self.assertTrue(saw_available and saw_omitted)

    def test_missing_future_mismatched_or_partial_baseline_is_unavailable(self):
        _, second = self.pair()
        for kind in ('missing', 'wrong_id', 'future', 'different_item', 'partial', 'bad_body_hash', 'changed_current'):
            value = copy.deepcopy(second)
            previous = value['event']['previous_version']
            if kind == 'missing': value['event'].pop('previous_version')
            elif kind == 'wrong_id': previous['id'] = 'other'
            elif kind == 'future': previous['version_seen_at'] = '2035-01-01T00:00:00Z'
            elif kind == 'different_item': previous['item_id'] = 'other'
            elif kind == 'partial': previous['evidence']['content_status'] = 'partial_text'
            elif kind == 'bad_body_hash': previous['evidence']['content_hash'] = '0' * 64
            else: value['excerpt'] = 'not the frozen value'
            comparison = candidate(value, self.now)
            self.assertEqual(comparison['status'], 'unavailable', kind)
            self.assertNotIn('_text', comparison)

    def test_default_and_aware_datetime_checks_keep_the_existing_time_contract(self):
        _, second = self.pair()
        self.assertTrue(build_input(second)['source_text']['body'])
        material = build_input(second, checked_at=datetime.fromtimestamp(self.now, timezone.utc))
        self.assertEqual(material['revision_comparison']['status'], 'available')

    def test_repeated_or_cross_segment_quote_cannot_be_ambiguously_attributed(self):
        _, second = self.pair()
        material = build_input(second, checked_at=self.now)
        for quote in ('Excludes the specified discontinued activity.', '10\n(1)'):
            with self.assertRaisesRegex(ValueError, 'revision_quote_ambiguous_or_crosses_segments'):
                locate({'quote': quote}, material)

    def test_large_saved_pair_is_omitted_atomically_without_excerpt_fallback(self):
        self.save(10, 'Old condition. ' * 1500)
        second = self.save(12, 'New condition. ' * 1500)
        material = build_input(second, checked_at=self.now)
        self.assertEqual(material['revision_comparison']['status'], 'unavailable')
        self.assertEqual(material['revision_comparison']['reason'], 'comparison_budget')
        self.assertNotIn('revision', material['source_text'])
        self.assertFalse(material['source_text']['excerpt'])
        self.assertLessEqual(encoded_size(material), 12000)

    def test_market_revision_pair_is_atomic_and_new_information_must_cite_it(self):
        _, second = self.pair()
        context = resolve_saved_context(None, second, as_of=second['version_seen_at'])
        material = build_market_input(second, context, second['version_seen_at'])
        self.assertEqual(material['revision_comparison']['status'], 'available')
        pair = next(row for row in material['evidence'] if row['type'] == 'article_revision_comparison')
        self.assertLessEqual(encoded_size(material), 8500)
        output = project_result()
        output['new_information'] = [{'claim': '旧版与新版保留了可对照的原文。', 'evidence_id': pair['id'], 'quote': pair['text']}]
        self.assertEqual(validate_market(json.dumps(output, ensure_ascii=False), material)['revision_comparison']['status'], 'available')
        ordinary = next(row for row in material['evidence'] if row['type'] == 'article')
        output['new_information'][0].update(evidence_id=ordinary['id'], quote=ordinary['text'])
        with self.assertRaisesRegex(ValueError, 'market_revision_comparison_required'):
            validate_market(json.dumps(output, ensure_ascii=False), material)
        with patch('backend.news_market_context.MAX_MARKET_INPUT_BYTES', 3000):
            smaller = build_market_input(second, context, second['version_seen_at'])
        self.assertNotEqual(smaller['revision_comparison']['status'], 'available')
        self.assertFalse(any(row['type'] == 'article_revision_comparison' for row in smaller['evidence']))
        self.assertIn(UNKNOWN, smaller['missing'])

    def test_original_excerpt_revision_remains_excerpt_scope_and_saved_paid_identity_does_not_repeat(self):
        from backend.news_platform_ai import NewsAnalysis
        from backend.news_event_store import record_version
        def excerpt_version(text):
            self.now += 10
            with self.news._db() as db:
                db.execute('UPDATE items SET excerpt=?,updated=? WHERE id=?', (text, self.now, 'revision'))
                row = dict(db.execute('SELECT * FROM items WHERE id=?', ('revision',)).fetchone())
                record_version(db, row, self.now)
            return self.news.get_item('revision')
        calls = []
        def fixed(key, text):
            material = json.loads(text); calls.append(material)
            revision = (material.get('revision_comparison') or {}).get('status') == 'available'
            quote = 'Synthetic original forecast ten.' if revision else material['source_text']['excerpt']
            value = {'summary': '合成保存材料的说明。', 'key_points': [
                {'claim': '原句来自对应的保存版本。', 'evidence_field': 'revision' if revision else 'excerpt', 'quote': quote}],
                'uncertainties': ['仅为本地固定返回，不证明模型理解质量。']}
            return SimpleNamespace(ok=True, model=ai._model, content=json.dumps(value, ensure_ascii=False))
        ai = NewsAnalysis(self.root / 'analysis.sqlite', self.news, generate=fixed)
        self.addCleanup(ai.stop)
        self.news.ai = ai
        ai.configure('PUBLIC_SYNTHETIC_ONLY', False, True)
        first = excerpt_version('Synthetic original forecast ten.')
        ai.request('revision'); ai._analyze('revision', task_id=self.news.analysis_task_for_item('revision')['id'])
        second = excerpt_version('Synthetic revised forecast twelve.')
        task_id = self.news.analysis_task_for_item('revision')['id']
        ai.request('revision'); ai._analyze('revision', task_id=task_id)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]['scope']['kind'], 'rss_excerpt')
        self.assertEqual(calls[1]['revision_comparison']['status'], 'available')
        saved = ai.result(second)
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['evidence'][0]['locator']['article_version_id'], first['current_version_id'])
        ai.request('revision'); ai._analyze('revision', task_id=task_id)
        self.assertEqual(len(calls), 2)


if __name__ == '__main__':
    unittest.main()
