"""Beginner control contracts keep configuration, previews and sending separate."""
import unittest
from unittest.mock import Mock

from backend.news_platform_routes import news_response


class BeginnerRouteTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime.startup = None
        self.ai = self.runtime.ai
        self.focus = {'all_registered': False, 'companies': ['micron'], 'symbols': [],
                      'industries': [], 'macro_topics': [], 'automatic_scope': 'market'}
        self.runtime.focus.return_value = {'focus': self.focus, 'catalog': {}}
        self.runtime.configure_focus.return_value = {'focus': self.focus, 'catalog': {}}
        self.ai.set_mode.return_value = {'configured': True, 'automatic': True, 'expires_at': 100}
        self.ai.preview_backlog.return_value = {'token': 'x' * 32, 'count': 2, 'confirmation_required': True}
        self.ai.confirm_backlog.return_value = {'status': 'queued', 'count': 2, 'task_ids': ['a', 'b']}
        self.ai.prepare_market_context.return_value = {'context_id': 'news-only'}
        self.ai.market_result.return_value = None

    def post(self, path, payload, **kwargs):
        return news_response(self.runtime, path, payload=payload, method='POST', **kwargs)

    def test_focus_save_is_distinct_from_starting_sources_or_ai(self):
        result, status = news_response(self.runtime, '/api/news/focus')
        self.assertEqual((status, result['focus']), (200, self.focus))
        result, status = self.post('/api/news/focus', self.focus)
        self.assertEqual((status, result['focus']), (200, self.focus))
        self.runtime.configure_focus.assert_called_once_with(self.focus)
        self.runtime.control.assert_not_called()
        self.runtime.configure_source.assert_not_called()
        self.runtime.refresh.assert_not_called()
        self.ai.configure.assert_not_called()
        for payload in ({}, {**self.focus, 'reset_cooldown': True}):
            self.assertEqual(self.post('/api/news/focus', payload)[1], 400)
        self.assertEqual(news_response(self.runtime, '/api/news/focus', query={'key': ['hidden']})[1], 400)

    def test_mode_switch_cannot_replace_key_or_reconfigure_session(self):
        payload = {'automatic': True, 'acknowledge_budget': True}
        result, status = self.post('/api/news/ai/mode', payload)
        self.assertEqual((status, result['ai']['expires_at']), (200, 100))
        self.ai.set_mode.assert_called_once_with(**payload)
        self.ai.configure.assert_not_called()
        self.ai.clear.assert_not_called()
        for invalid in ({}, {**payload, 'api_key': 'untrusted'}, {**payload, 'automatic': 1},
                        {**payload, 'acknowledge_budget': 'yes'}, {**payload, 'expires_at': 999}):
            self.assertEqual(self.post('/api/news/ai/mode', invalid)[1], 400)
        self.assertEqual(self.ai.set_mode.call_count, 1)

    def test_bounded_backlog_preview_is_not_sending(self):
        payload = {'limit': 2, 'max_cost_cny': .25, 'task_type': 'market_impact'}
        result, status = self.post('/api/news/ai/backlog/preview', payload)
        self.assertEqual((status, result['count']), (200, 2))
        self.ai.preview_backlog.assert_called_once_with(**payload)
        self.ai.confirm_backlog.assert_not_called()
        self.ai.request.assert_not_called()
        self.ai.request_market.assert_not_called()
        for invalid in ({**payload, 'limit': True}, {**payload, 'limit': 11},
                        {**payload, 'max_cost_cny': 3.1}, {**payload, 'max_cost_cny': float('nan')},
                        {**payload, 'task_type': 'all'}, {**payload, 'api_key': 'unexpected'}):
            self.assertEqual(self.post('/api/news/ai/backlog/preview', invalid)[1], 400)
        self.assertEqual(self.ai.preview_backlog.call_count, 1)

    def test_backlog_confirm_only_passes_preview_token(self):
        token = 'x' * 32
        result, status = self.post('/api/news/ai/backlog/confirm', {'token': token})
        self.assertEqual((status, result['count']), (202, 2))
        self.ai.confirm_backlog.assert_called_once_with(token)
        for invalid in ({}, {'token': 'bad'}, {'token': token, 'task_ids': ['forged']}, {'token': 12}):
            self.assertEqual(self.post('/api/news/ai/backlog/confirm', invalid)[1], 400)
        self.assertEqual(self.ai.confirm_backlog.call_count, 1)

    def test_current_news_can_be_prepared_without_room_round_or_model(self):
        payload = {'room_id': None, 'round_id': None, 'material_refs': []}
        result, status = self.post('/api/news/items/news-1/market/context', payload)
        self.assertEqual((status, result['market_context']['context_id']), (200, 'news-only'))
        self.ai.prepare_market_context.assert_called_once_with('news-1', **payload)
        self.ai.request_market.assert_not_called()
        self.runtime.research_store.room_snapshot.assert_not_called()

    def test_news_only_and_bound_round_cannot_be_mixed_or_select_floating_materials(self):
        base = {'room_id': None, 'round_id': None, 'material_refs': []}
        for historical in ({}, {'version_id': 'version-1', 'as_of': '2026-11-07T00:00:00Z'}):
            for invalid in ({**base, 'room_id': 'room-1'}, {**base, 'round_id': 'round-1'},
                            {**base, 'material_refs': [{'material_id': 'mat-1', 'version': 1}]}):
                self.assertEqual(self.post('/api/news/items/news-1/market/context', {**invalid, **historical})[1], 400)
        self.ai.prepare_market_context.assert_not_called()
        self.ai.request_market.assert_not_called()

    def test_mutation_query_extras_are_rejected_before_runtime(self):
        for path, payload in [('/api/news/focus', self.focus),
                              ('/api/news/ai/mode', {'automatic': False, 'acknowledge_budget': False}),
                              ('/api/news/ai/backlog/confirm', {'token': 'x' * 32})]:
            self.assertEqual(self.post(path, payload, query={'bypass': ['1']})[1], 400)
        self.runtime.configure_focus.assert_not_called()
        self.ai.set_mode.assert_not_called()
        self.ai.confirm_backlog.assert_not_called()


if __name__ == '__main__':
    unittest.main()
