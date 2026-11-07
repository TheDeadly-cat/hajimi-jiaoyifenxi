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

    def save(self, excerpt=TEXT, item_id='direct-news'):
        with self.news._db() as db:
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (item_id, 'ir_mu', item_id, 'Synthetic Micron results', URL + '/' + item_id, excerpt,
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


if __name__ == '__main__':
    unittest.main()
