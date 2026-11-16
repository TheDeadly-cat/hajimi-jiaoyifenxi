"""Current-news-only explanation, fixed local callbacks and isolated databases."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.news_platform import NewsPlatform, analyze
from backend.news_platform_ai import NewsAnalysis
from backend import news_platform_ai as analysis_module
from backend import news_analysis_providers as providers
from backend.news_article_evidence import extract_article
from backend.news_market_context import (
    LEGACY_QUALITY_VERSION, QUALITY_VERSION, UNKNOWN_EXPECTATION, UNKNOWN_MARKET,
    build_market_input, project_result, resolve_saved_context, validate_output,
)
from backend import news_market_tasks as tasks
from backend.news_platform_routes import news_response

NOW = 1794045600.0
TEXT = 'Synthetic quarterly revenue was USD 10 million, subject to final approval.'
URL = 'https://investors.micron.com/synthetic-current-news-only'


def fixed_market_output(material):
    """A deterministic v2 UI fixture, never a claim of model-quality acceptance."""
    output = project_result()
    source = next(row for row in material['evidence'] if row['type'] in {'article', 'article_excerpt'})
    output['facts'] = [{'claim': '给定合成资料保留了可核对的原文。', 'evidence_id': source['id'], 'quote': source['text'][:500]}]
    output['unknowns'] = ['固定本地测试返回，不代表真实模型判断；尚缺可核验的预期及对应时点行情。']
    output['research_actions'] = [
        {'kind': 'verify', 'action': '核对官方原文的发布时间和适用条件。'},
        {'kind': 'collect', 'action': '需要比较时，补充发布前已保存的同口径预期。'},
        {'kind': 'observe', 'action': '观察后续更正或补充披露。'},
    ]
    output['judgment_change_conditions'] = ['若官方更正改变原文或适用条件，需要重新核对当前解释。']
    return output


class CurrentNewsMarketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='news-direct-market-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = NOW
        self.calls = []
        for target, name in ((socket.socket, 'connect'), (socket, 'getaddrinfo'), (providers, 'generate_response')):
            guard = patch.object(target, name, side_effect=AssertionError('offline_fixed_callback_only'))
            guard.start(); self.addCleanup(guard.stop)
        guard = patch.object(analysis_module, 'time', SimpleNamespace(time=lambda: self.now, monotonic=time.monotonic))
        guard.start(); self.addCleanup(guard.stop)
        guard = patch.object(analysis_module, 'datetime', wraps=datetime)
        self.date = guard.start(); self.addCleanup(guard.stop)
        self.date.now.side_effect = lambda tz=None: datetime.fromtimestamp(self.now, tz)
        self.news = NewsPlatform(self.root / 'news.sqlite', clock=lambda: self.now,
                                 transport=lambda *a: self.fail('source request'))
        self.addCleanup(self.news.stop)
        self.ai = NewsAnalysis(self.root / 'analysis.sqlite', self.news, generate=self.fixed)
        self.addCleanup(self.ai.stop)
        self.news.ai = self.ai
        self.ai.configure('PUBLIC_OFFLINE_FIXTURE_ONLY', False, True)
        self.item = self.save()

    def save(self, excerpt=TEXT, item_id='direct-news', *, url=None):
        with self.news._db() as db:
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (item_id, 'ir_mu', item_id, 'Synthetic Micron results', url or URL + '/' + item_id, excerpt,
                        self.now - 900, self.now - 10, self.now,
                        json.dumps(analyze('Synthetic Micron results', excerpt, self.now - 900, self.now, 'ir_mu'))))
        return self.news.save_article_evidence(item_id, {'content_status': 'excerpt_only', 'body_text': '', 'segments': [], 'attachments': []})

    def fixed(self, key, text):
        material = json.loads(text); self.calls.append(material)
        output = fixed_market_output(material)
        return SimpleNamespace(ok=True, model=self.ai._model, content=json.dumps(output, ensure_ascii=False))

    def prepare(self):
        return tasks.prepare(self.news, None, self.item['id'], room_id=None, round_id=None, material_refs=[])

    def material(self):
        saved = tasks.get(self.news, self.item['id'], self.prepare()['context_id'])
        return build_market_input(saved['item'], saved['context'], datetime.fromtimestamp(self.now, timezone.utc).isoformat())

    def test_prepare_needs_no_research_store_or_model_and_keeps_missing_context_explicit(self):
        context = self.prepare()
        self.assertEqual(context['mode'], 'current_news_only')
        self.assertIsNone(context['room_id']); self.assertIsNone(context['round_id'])
        self.assertEqual(context['article_version_id'], self.item['current_version_id'])
        self.assertIn(UNKNOWN_EXPECTATION, context['missing']); self.assertIn(UNKNOWN_MARKET, context['missing'])
        self.assertEqual(self.ai.status()['calls_reserved'], 0); self.assertEqual(self.calls, [])
        self.assertIsNone(self.news._thread)

    def test_current_null_context_route_prepares_without_room_and_rejects_partial_bindings(self):
        endpoint = '/api/news/items/direct-news/market/context'
        response, code = news_response(self.news, endpoint, method='POST', payload={'room_id': None, 'round_id': None, 'material_refs': []})
        self.assertEqual(code, 200, response)
        self.assertEqual(response['market_context']['mode'], 'current_news_only')
        for body in ({'room_id': 'room', 'round_id': None, 'material_refs': []},
                     {'room_id': None, 'round_id': 'round', 'material_refs': []},
                     {'room_id': None, 'round_id': None, 'material_refs': [{'material_id': 'material', 'version': 1}]}):
            with self.subTest(body=body):
                self.assertEqual(news_response(self.news, endpoint, method='POST', payload=body)[1], 400)
        self.assertEqual(self.ai.status()['calls_reserved'], 0)

    def test_core_rejects_partial_bindings_without_touching_research_store(self):
        for room, round_id, refs in [('room', None, []), (None, 'round', []), ('', '', []), (None, None, [{'id': 'm', 'version': 1}])]:
            with self.subTest(room=room, round=round_id), self.assertRaises(ValueError):
                tasks.prepare(self.news, None, self.item['id'], room_id=room, round_id=round_id, material_refs=refs)

    def test_six_questions_are_distinct_and_cannot_invent_expectations_or_reaction(self):
        material = self.material(); output = project_result()
        self.assertEqual(material['quality_version'], QUALITY_VERSION)
        self.assertFalse(material['has_consensus']); self.assertFalse(material['has_market'])
        self.assertEqual(validate_output(output, material), output)
        for field, status in [('expectation_comparison', 'context_available'), ('market_reaction', 'observed_snapshot')]:
            invalid = copy.deepcopy(output); invalid[field]['status'] = status
            with self.assertRaises(ValueError): validate_output(invalid, material)
        invalid = copy.deepcopy(output); invalid.pop('research_actions')
        with self.assertRaisesRegex(ValueError, 'shape'): validate_output(invalid, material)
        invalid = copy.deepcopy(output); invalid['research_actions'][0]['kind'] = 'trade'
        with self.assertRaisesRegex(ValueError, 'research_action'): validate_output(invalid, material)
        invalid = copy.deepcopy(output); invalid['judgment_change_conditions'] = [invalid['research_actions'][0]['action']]
        with self.assertRaisesRegex(ValueError, 'repeats_action'): validate_output(invalid, material)
        source = material['evidence'][0]
        invalid = copy.deepcopy(output); invalid['new_information'] = [{'claim': '发生了新的变化。', 'evidence_id': source['id'], 'quote': source['text']}]
        with self.assertRaisesRegex(ValueError, 'revision_comparison_required'): validate_output(invalid, material)

    def test_explicit_legacy_validation_and_saved_projection_do_not_fabricate_new_answers(self):
        material = self.material()
        legacy = project_result(quality_version=LEGACY_QUALITY_VERSION)
        self.assertEqual(validate_output(legacy, {**material, 'quality_version': LEGACY_QUALITY_VERSION}), legacy)
        with self.assertRaisesRegex(ValueError, 'shape'): validate_output(legacy, material)
        self.assertEqual(project_result(legacy), legacy)
        self.assertNotIn('research_actions', project_result(legacy))

    def test_direct_request_uses_one_callback_and_same_evidence_reopen_does_not_recharge(self):
        context = self.prepare()
        self.assertEqual(self.ai.request_market(self.item['id'], context['context_id'])['status'], 'queued')
        self.ai._analyze(self.item['id'], task_id=tasks.fingerprint(context['context_id']), market_context_id=context['context_id'])
        result = self.ai.market_result(self.item, context['context_id'])
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(len(self.calls), 1); self.assertEqual(self.ai.status()['calls_reserved'], 1)
        self.assertIn('research_actions', result); self.assertIn('judgment_change_conditions', result)
        self.assertEqual(result['expectation_comparison']['status'], 'unknown')
        self.assertEqual(result['market_reaction']['status'], 'unknown')
        self.now += 100
        again = self.prepare()
        self.assertEqual(again['context_id'], context['context_id'])
        self.assertEqual(self.ai.request_market(self.item['id'], again['context_id'])['status'], 'completed')
        self.assertEqual(len(self.calls), 1); self.assertEqual(self.ai.status()['calls_reserved'], 1)

    def test_legacy_completed_identity_is_returned_without_configuration_or_recharge(self):
        context = self.prepare()
        legacy = project_result(quality_version=LEGACY_QUALITY_VERSION)
        with self.ai._db() as db:
            db.execute('INSERT INTO runs(fingerprint,item_id,created,reserved,status,result) VALUES(?,?,?,?,?,?)',
                       (tasks.fingerprint(context['context_id']), self.item['id'], self.now, 1000, 'completed', json.dumps(legacy)))
        self.ai.clear()
        result = self.ai.request_market(self.item['id'], context['context_id'])
        self.assertEqual(result['status'], 'completed'); self.assertNotIn('research_actions', result)
        self.assertEqual(self.calls, []); self.assertEqual(self.ai.status()['calls_reserved'], 1)

    def test_title_only_stops_before_spending_and_revision_requires_new_explicit_scope(self):
        empty = self.save(excerpt='', item_id='empty-news')
        context = tasks.prepare(self.news, None, empty['id'])
        self.ai.request_market(empty['id'], context['context_id'])
        self.ai._analyze(empty['id'], task_id=tasks.fingerprint(context['context_id']), market_context_id=context['context_id'])
        result = self.ai.market_result(empty, context['context_id'])
        self.assertTrue(result['material_insufficient']); self.assertFalse(result['analysis_performed'])
        self.assertEqual(self.calls, []); self.assertEqual(self.ai.status()['calls_reserved'], 0)
        old = self.prepare(); self.now += 100
        correction = b'<article><p>New synthetic official correction. The reported quarterly amount is revised and remains subject to the explicitly stated final approval condition.</p></article>'
        changed = self.news.save_article_evidence(self.item['id'], extract_article(correction, URL, 'ir_mu', now=self.now))
        self.assertNotEqual(changed['current_version_id'], self.item['current_version_id'])
        with self.assertRaisesRegex(ValueError, 'version_changed'):
            self.ai.request_market(self.item['id'], old['context_id'])
        self.assertNotEqual(self.prepare()['context_id'], old['context_id'])

    def test_completed_current_explanation_reopens_from_history_after_revision_and_restart(self):
        context = self.prepare()
        context_id = context['context_id']
        self.ai.request_market(self.item['id'], context_id)
        self.ai._analyze(self.item['id'], task_id=tasks.fingerprint(context_id), market_context_id=context_id)
        original_result = self.ai.market_result(self.item, context_id)
        self.assertEqual(original_result['status'], 'completed')
        self.assertEqual(len(self.calls), 1)
        original_saved = tasks.get(self.news, self.item['id'], context_id)

        self.now += 100
        changed = self.news.save_article_evidence(self.item['id'], extract_article(
            b'<article><p>Corrected synthetic quarterly revenue was USD 9 million, subject to final approval. This is a material correction to the original synthetic announcement.</p></article>',
            URL, 'ir_mu', now=self.now))
        self.assertNotEqual(changed['current_version_id'], self.item['current_version_id'])
        self.assertIsNone(self.ai.market_context(changed))
        self.assertEqual(tasks.replay_list(self.news, self.item['id'])['total'], 0,
                         'ordinary saved results must not be relabelled as historical replays')

        self.ai.stop(); self.news.stop()
        reopened = NewsPlatform(self.root / 'news.sqlite', clock=lambda: self.now,
                                transport=lambda *a: self.fail('source request while reopening'))
        reopened_ai = NewsAnalysis(self.root / 'analysis.sqlite', reopened,
                                   generate=lambda *a: self.fail('model request while reopening'))
        self.addCleanup(reopened.stop); self.addCleanup(reopened_ai.stop)
        reopened.ai = reopened_ai
        self.assertFalse(reopened_ai.status()['configured'])
        reservations = reopened_ai.status()['calls_reserved']
        with reopened._db() as db:
            tasks_before = [tuple(row) for row in db.execute('SELECT * FROM news_analysis_tasks ORDER BY id')]

        listed, code = news_response(reopened, '/api/news/items/direct-news/market/history')
        self.assertEqual(code, 200, listed)
        self.assertEqual(listed['total'], 1)
        record = listed['contexts'][0]
        self.assertEqual(record['context_id'], context_id)
        self.assertEqual(record['article_version_id'], self.item['current_version_id'])
        self.assertEqual(record['mode'], 'current_news_only')
        self.assertFalse(record['evidence_matches_current'])
        self.assertTrue(record['requires_review'])
        self.assertEqual(record['current_article_version_id'], changed['current_version_id'])
        self.assertEqual(record['created_at'], datetime.fromtimestamp(NOW, timezone.utc).isoformat())

        saved, code = news_response(reopened, '/api/news/items/direct-news/market/context',
                                   query={'context_id': [context_id]})
        self.assertEqual(code, 200, saved)
        self.assertEqual(saved['saved_record'], record)
        self.assertEqual(saved['market_context'], context)
        self.assertEqual(saved['market_impact'], original_result)
        self.assertEqual(tasks.get(reopened, self.item['id'], context_id), original_saved)
        self.assertEqual(reopened_ai.status()['calls_reserved'], reservations)
        with reopened._db() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM news_analysis_tasks ORDER BY id')], tasks_before)
        self.assertEqual(len(self.calls), 1)

    def test_saved_history_lists_current_legacy_and_replay_scopes_without_changing_them(self):
        context = self.prepare()
        saved = tasks.get(self.news, self.item['id'], context['context_id'])
        legacy = dict(saved['context'])
        legacy.pop('mode'); legacy.pop('article_version_id')
        with self.news._db() as db:
            db.execute('INSERT INTO news_market_contexts VALUES(?,?,?,?,?,?)',
                       ('legacy-context', self.item['id'], self.item['current_version_id'], self.now - 30,
                        json.dumps(legacy), json.dumps(saved['item'])))
        self.now += 10
        replay = tasks.prepare(self.news, None, self.item['id'],
                               version_id=self.item['current_version_id'],
                               as_of=datetime.fromtimestamp(NOW, timezone.utc).isoformat())
        before, code = news_response(self.news, '/api/news/items/direct-news/market/history',
                                    query={'limit': ['2'], 'offset': ['0']})
        self.assertEqual(code, 200, before)
        self.assertEqual((before['total'], before['limit'], before['offset']), (3, 2, 0))
        self.assertEqual([row['context_id'] for row in before['contexts']], [replay['context_id'], context['context_id']])
        self.assertEqual([row['mode'] for row in before['contexts']], ['historical_replay', 'current_news_only'])
        self.assertTrue(all(row['evidence_matches_current'] for row in before['contexts']))
        tail, code = news_response(self.news, '/api/news/items/direct-news/market/history',
                                  query={'limit': ['2'], 'offset': ['2'], 'version_id': [self.item['current_version_id']]})
        self.assertEqual(code, 200, tail)
        self.assertEqual(tail['contexts'][0]['mode'], 'current')
        self.assertEqual(tail['contexts'][0]['context_id'], 'legacy-context')
        self.assertNotIn('mode', tasks.get(self.news, self.item['id'], 'legacy-context')['context'])
        self.assertEqual(tasks.replay_list(self.news, self.item['id'])['total'], 1)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ai.status()['calls_reserved'], 0)

    def test_saved_history_is_item_scoped_and_version_filtered(self):
        first = self.prepare()
        self.now += 100
        changed = self.news.save_article_evidence(self.item['id'], extract_article(
            b'<article><p>A materially revised synthetic filing changes both the previously reported amount and its conditions. This is a saved offline fixture, not a real market release.</p></article>',
            URL, 'ir_mu', now=self.now))
        latest = self.prepare()
        page, code = news_response(self.news, '/api/news/items/direct-news/market/history',
                                  query={'version_id': [self.item['current_version_id']]})
        self.assertEqual(code, 200, page)
        self.assertEqual([row['context_id'] for row in page['contexts']], [first['context_id']])
        current, code = news_response(self.news, '/api/news/items/direct-news/market/history',
                                     query={'version_id': [changed['current_version_id']]})
        self.assertEqual(code, 200, current)
        self.assertEqual([row['context_id'] for row in current['contexts']], [latest['context_id']])

        other = self.save('A different synthetic company disclosed an unrelated acquisition.', 'unrelated-news')
        empty, code = news_response(self.news, '/api/news/items/unrelated-news/market/history')
        self.assertEqual((code, empty['total']), (200, 0))
        rejected, code = news_response(self.news, '/api/news/items/unrelated-news/market/context',
                                       query={'context_id': [first['context_id']]})
        self.assertEqual(code, 404, rejected)
        absent, code = news_response(self.news, '/api/news/items/missing-news/market/history')
        self.assertEqual(code, 404, absent)
        self.assertEqual(self.calls, [])

    def test_saved_history_validates_queries_and_does_not_require_model_configuration(self):
        context = self.prepare()
        self.ai.clear()
        endpoint = '/api/news/items/direct-news/market/history'
        for query in ({'limit': ['0']}, {'limit': ['101']}, {'offset': ['-1']},
                      {'offset': ['1000001']}, {'limit': ['x']}, {'limit': ['1', '2']},
                      {'version_id': ['']}, {'version_id': ['bad/value']}, {'unknown': ['1']}):
            with self.subTest(query=query):
                self.assertEqual(news_response(self.news, endpoint, query=query)[1], 400)
        page, code = news_response(self.news, endpoint)
        self.assertEqual((code, page['total']), (200, 1))
        self.assertEqual(page['contexts'][0]['context_id'], context['context_id'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ai.status()['calls_reserved'], 0)
        self.assertFalse(self.ai.status()['configured'])

    def test_shared_current_result_remains_readable_from_matching_old_version_after_copy_changes(self):
        original = self.save(item_id='shared-original', url=URL + '/shared?utm_source=original')
        copied = self.save(item_id='shared-copy', url=URL + '/shared?utm_source=copy')
        context = tasks.prepare(self.news, None, original['id'])
        reused = tasks.prepare(self.news, None, copied['id'])
        self.assertEqual(reused['context_id'], context['context_id'])
        self.ai.request_market(original['id'], context['context_id'])
        self.ai._analyze(original['id'], task_id=tasks.fingerprint(context['context_id']),
                         market_context_id=context['context_id'])
        original_result = self.ai.market_result(original, context['context_id'])
        self.assertEqual(original_result['status'], 'completed')
        before, code = news_response(self.news, '/api/news/items/shared-copy/market/history')
        self.assertEqual((code, before['total']), (200, 1), before)
        self.assertTrue(before['contexts'][0]['is_shared'])
        self.assertTrue(before['contexts'][0]['evidence_matches_current'])

        self.now += 100
        updated = self.news.save_article_evidence(copied['id'], extract_article(
            b'<article><p>The copied synthetic release now reports a materially revised amount and additional conditions. It is no longer the same evidence as the original saved explanation.</p></article>',
            URL + '/shared?utm_source=copy', 'ir_mu', now=self.now))
        self.assertNotEqual(updated['current_version_id'], copied['current_version_id'])
        self.assertIsNone(self.ai.market_context(updated))
        self.ai.clear()
        callbacks, reservations = len(self.calls), self.ai.status()['calls_reserved']
        for query in ({}, {'version_id': [copied['current_version_id']]}):
            listing, code = news_response(self.news, '/api/news/items/shared-copy/market/history', query=query)
            self.assertEqual((code, listing['total']), (200, 1), listing)
            record = listing['contexts'][0]
            self.assertEqual(record['context_id'], context['context_id'])
            self.assertEqual(record['article_version_id'], copied['current_version_id'])
            self.assertEqual(record['source_item_id'], original['id'])
            self.assertEqual(record['source_article_version_id'], original['current_version_id'])
            self.assertFalse(record['evidence_matches_current'])
            self.assertTrue(record['requires_review'])
            read, code = news_response(self.news, '/api/news/items/shared-copy/market/context',
                                      query={'context_id': [context['context_id']],
                                             'version_id': [record['article_version_id']]})
            self.assertEqual(code, 200, read)
            self.assertEqual(read['saved_record'], record)
            self.assertEqual(read['market_context'], context)
            self.assertEqual(read['market_impact'], original_result)
        legacy_read, code = news_response(self.news, '/api/news/items/shared-copy/market/context',
                                         query={'context_id': [context['context_id']]})
        self.assertEqual((code, legacy_read['market_impact']), (200, original_result))
        with self.assertRaisesRegex(ValueError, 'version_changed'):
            self.ai.request_market(copied['id'], context['context_id'])
        current_page, code = news_response(self.news, '/api/news/items/shared-copy/market/history',
                                          query={'version_id': [updated['current_version_id']]})
        self.assertEqual((code, current_page['total']), (200, 0))
        self.assertEqual(len(self.calls), callbacks)
        self.assertEqual(self.ai.status()['calls_reserved'], reservations)

    def test_shared_history_requires_original_version_event_and_evidence_and_never_shares_replays(self):
        original = self.save(item_id='shared-original', url=URL + '/shared?utm_source=original')
        copied = self.save(item_id='shared-copy', url=URL + '/shared?utm_source=copy')
        context = tasks.prepare(self.news, None, original['id'])
        replay = tasks.prepare(self.news, None, original['id'], version_id=original['current_version_id'],
                               as_of=datetime.fromtimestamp(self.now, timezone.utc).isoformat())
        listing, code = news_response(self.news, '/api/news/items/shared-copy/market/history')
        self.assertEqual((code, listing['total']), (200, 1), listing)
        self.assertEqual(listing['contexts'][0]['context_id'], context['context_id'])
        rejected, code = news_response(self.news, '/api/news/items/shared-copy/market/context',
                                       query={'context_id': [replay['context_id']]})
        self.assertEqual(code, 404, rejected)

        different_evidence = self.save('Different conditions and a different amount in the same synthetic release.',
                                       'different-evidence', url=URL + '/shared?utm_source=different')
        different_event = self.save(item_id='different-event', url=URL + '/another-original')
        self.assertEqual(different_evidence['event']['event_id'], original['event']['event_id'])
        self.assertNotEqual(different_evidence['evidence_package_id'], original['evidence_package_id'])
        self.assertNotEqual(different_event['event']['event_id'], original['event']['event_id'])
        for unrelated in (different_evidence, different_event):
            path = '/api/news/items/' + unrelated['id']
            empty, code = news_response(self.news, path + '/market/history')
            self.assertEqual((code, empty['total']), (200, 0), empty)
            rejected, code = news_response(self.news, path + '/market/context',
                                           query={'context_id': [context['context_id']]})
            self.assertEqual(code, 404, rejected)
        for version_id in (original['current_version_id'], different_event['current_version_id']):
            rejected, code = news_response(self.news, '/api/news/items/shared-copy/market/context',
                                           query={'context_id': [context['context_id']], 'version_id': [version_id]})
            self.assertEqual(code, 404, rejected)

        saved = tasks.get(self.news, original['id'], context['context_id'])
        forged = copy.deepcopy(saved['item'])
        forged['event']['event_id'] = different_event['event']['event_id']
        with self.news._db() as db:
            db.execute('INSERT INTO news_market_contexts VALUES(?,?,?,?,?,?)',
                       ('forged-scope', original['id'], original['current_version_id'], self.now,
                        json.dumps(saved['context']), json.dumps(forged)))
        rejected, code = news_response(self.news, '/api/news/items/shared-copy/market/context',
                                       query={'context_id': ['forged-scope']})
        self.assertEqual(code, 404, rejected)
        final, code = news_response(self.news, '/api/news/items/shared-copy/market/history')
        self.assertEqual((code, final['total']), (200, 1), final)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ai.status()['calls_reserved'], 0)

    def test_queued_shared_analysis_cannot_send_after_requesting_article_changes(self):
        original = self.save(item_id='queued-original', url=URL + '/shared?utm_source=original')
        copied = self.save(item_id='queued-copy', url=URL + '/shared?utm_source=copy')
        context = tasks.prepare(self.news, None, original['id'])
        self.assertEqual(tasks.prepare(self.news, None, copied['id'])['context_id'], context['context_id'])
        self.assertEqual(self.ai.request_market(copied['id'], context['context_id'])['status'], 'queued')
        self.now += 100
        self.news.save_article_evidence(copied['id'], extract_article(
            b'<article><p>The requesting copied release now has materially changed data and qualifications. A queued ordinary explanation must not send the superseded scope.</p></article>',
            URL + '/shared?utm_source=copy', 'ir_mu', now=self.now))
        self.ai._analyze(copied['id'], task_id=tasks.fingerprint(context['context_id']),
                         market_context_id=context['context_id'])
        self.assertEqual(len(self.calls), 0)
        self.assertEqual(self.ai.status()['calls_reserved'], 0)
        with self.assertRaisesRegex(ValueError, 'version_changed'):
            self.ai.request_market(copied['id'], context['context_id'])
        readable, code = news_response(self.news, '/api/news/items/queued-copy/market/context',
                                      query={'context_id': [context['context_id']],
                                             'version_id': [copied['current_version_id']]})
        self.assertEqual(code, 200, readable)
        self.assertTrue(readable['saved_record']['requires_review'])
        self.assertFalse(readable['market_impact']['analysis_performed'])
        self.assertEqual(self.ai.status()['calls_reserved'], 0)


if __name__ == '__main__':
    unittest.main()
