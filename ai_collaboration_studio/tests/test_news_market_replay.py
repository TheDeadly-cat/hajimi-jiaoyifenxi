"""Historical market preparation with real isolated news and StudioStore data.

No NewsAnalysis instance, model/provider invocation, source fetch, or worker is
allowed. Saved quotes and materials below are explicitly synthetic fixtures.
"""
from contextlib import ExitStack, closing
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch
from types import SimpleNamespace

os.environ['AI_STUDIO_SKIP_LOCAL_ENV'] = '1'

from backend.store import StudioStore
from backend.news_platform import NewsPlatform, analyze
from backend.news_platform_routes import news_response
from backend.news_platform_ai import NewsAnalysis, MODEL
from backend import news_analysis_providers as providers
from backend import news_market_tasks as tasks
from backend import news_event_store as event_store
from backend.news_market_context import build_market_input, project_result
from backend.news_market_material import prepare_market_material_payload


URL = 'https://investors.micron.com/offline-historical-quarter'
PUB = '2026-10-29T15:50:00Z'
V1_SEEN = '2026-10-29T15:51:00Z'
CUTOFF = '2026-10-29T16:05:00Z'
V2_SEEN = '2026-10-29T16:10:00Z'
NOW = '2026-10-29T17:00:00Z'


def seconds(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def ms(value):
    return int(seconds(value) * 1000)


def material_payload(kind='consensus', value=12, known_at='2026-10-29T14:00:00Z'):
    kwargs = dict(kind=kind, symbol='US.MU', metric='revenue', period='FY2026-Q4', unit='million',
                  currency='USD', comparison_basis='reported_revenue', value=value, known_at=known_at,
                  quote=f'FY2026-Q4 revenue USD {value} million.', source_url='https://example.invalid/consensus')
    if kind == 'reported_actual':
        kwargs.update(source_url=URL, event_url=URL, source_type='company_ir')
    return prepare_market_material_payload(**kwargs)


def saved_quote(captured='2026-10-29T16:00:00Z'):
    updated = (datetime.fromisoformat(captured.replace('Z', '+00:00')) - timedelta(seconds=30)).isoformat()
    return {'ok': True, 'state': 'ready', 'snapshot_id': 'offline-snapshot-' + hashlib.sha256(captured.encode()).hexdigest()[:12],
            'captured_at': captured, 'source': 'futu_opend', 'source_errors': [], 'missing_symbols': [],
            'execution_capability': 'none', 'live_trading_allowed': False,
            'evidence': {'version': 'storage_market_evidence_v6'},
            'rows': [{'symbol': symbol, 'last': 100 + index, 'volume': 1000,
                      'market_time': '2026-10-29 11:59:30', 'updated_at': updated,
                      'quality': 'ready', 'age_seconds': 30, 'quote_is_live': True,
                      'freshness_basis': 'live_20m_window'} for index, symbol in enumerate(('US.MU', 'US.SNDK', 'US.WDC', 'US.STX'))]}


class MarketReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='news-market-replay-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        self.attempts = {'network': 0, 'model_constructor': 0, 'provider': 0, 'worker': 0, 'outside_database': 0}

        def deny(kind):
            def fail(*args, **kwargs):
                self.attempts[kind] += 1
                raise AssertionError(kind + '_forbidden')
            return fail

        for target, name in ((socket.socket, 'connect'), (socket.socket, 'connect_ex'), (socket.socket, 'sendto'),
                             (socket, 'create_connection'), (socket, 'getaddrinfo'),
                             (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
            self.guards.enter_context(patch.object(target, name, side_effect=deny('network')))
        self.guards.enter_context(patch.object(NewsAnalysis, '__init__', side_effect=deny('model_constructor')))
        self.guards.enter_context(patch.object(providers, 'generate_response', side_effect=deny('provider')))
        self.guards.enter_context(patch.object(threading.Thread, 'start', side_effect=deny('worker')))
        real_connect = sqlite3.connect

        def isolated_connect(database, *args, **kwargs):
            if kwargs.get('uri') or (str(database) != ':memory:' and not Path(database).resolve().is_relative_to(self.root)):
                return deny('outside_database')()
            return real_connect(database, *args, **kwargs)

        self.guards.enter_context(patch.object(sqlite3, 'connect', side_effect=isolated_connect))
        self.store = StudioStore(self.root / 'studio.sqlite')
        self.now = seconds(V1_SEEN)
        self.news = NewsPlatform(self.root / 'news.sqlite', clock=lambda: self.now,
                                 transport=deny('network'), article_transport=deny('network'))
        self.addCleanup(self.news.stop)
        self.news.research_store = self.store
        self.item_id = 'offline-market-replay'
        with self.news._db() as db:
            db.execute('INSERT INTO items(id,source_id,identity,title,url,excerpt,published,discovered,updated,analysis) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.item_id, 'ir_mu', self.item_id, 'Offline synthetic quarterly results', URL, '', seconds(PUB), self.now, self.now,
                 json.dumps(analyze('Offline synthetic quarterly results', '', seconds(PUB), self.now, 'ir_mu'))))
        self.v1 = self.save_body('Offline fixture: FY2026-Q4 reported revenue was USD 10 million.')
        self.now = seconds(V2_SEEN)
        self.v2 = self.save_body('Offline corrected fixture: FY2026-Q4 reported revenue was USD 11 million.')
        self.now = seconds(NOW)

    def tearDown(self):
        self.assertTrue(all(value == 0 for value in self.attempts.values()), self.attempts)
        self.assertIsNone(getattr(self.news, 'ai', None))
        self.assertIsNone(self.news._thread)
        self.assertIsNone(self.news._article_thread)

    def save_body(self, text):
        return self.news.save_article_evidence(self.item_id, {'content_status': 'full_text', 'body_text': text,
            'segments': [{'id': 'p1', 'kind': 'paragraph', 'text': text}], 'attachments': [],
            'content_hash': hashlib.sha256(text.encode()).hexdigest(), 'published_at': PUB,
            'extractor_version': 'offline-replay-fixture-v1', 'symbols': ['US.MU']})

    def add_material(self, *, at='2026-10-29T15:00:00Z', **kwargs):
        with patch('backend.store.now_ms', return_value=ms(at)):
            return self.store.add_material('room_plan', material_payload(**kwargs))

    def freeze(self, at, *, snapshot=None, checkpoint_at=None):
        with patch('backend.store.now_ms', return_value=ms(at)):
            round_row = self.store.create_round('room_plan', 'Offline historical replay fixture')
            shared, manifest = self.store.material_prompt_bundle('room_plan')
            manifest = self.store.finalize_round_evidence_manifest(manifest, shared_context=shared, market_snapshot=snapshot)
        with patch('backend.store.now_ms', return_value=ms(checkpoint_at or at)):
            self.store.save_round_checkpoint('room_plan', round_row['id'], {
                'member_ids': [], 'spoken_counts': {}, 'spoken_stances': [], 'successful_member_ids': [],
                'failed_member_ids': [], 'previous_name': '', 'completed': 0, 'failures': 0, 'skipped': 0,
                'proposals_created': 0, 'next_order': 1, 'max_turns': 1, 'shared_context': shared,
                'market_snapshot': snapshot, 'round_evidence_manifest': manifest})
        return round_row['id']

    def request(self, *, version=None, as_of=CUTOFF, room=None, round_id=None, refs=()):
        return news_response(self.news, f'/api/news/items/{self.item_id}/market/context', method='POST', payload={
            'room_id': room, 'round_id': round_id, 'material_refs': list(refs),
            'version_id': version or self.v1['current_version_id'], 'as_of': as_of})

    def options(self, *, cutoff=CUTOFF, limit=50, offset=0):
        return news_response(self.news, '/api/news/market/options', query={'room_id': ['room_plan'],
            'as_of': [cutoff], 'limit': [str(limit)], 'offset': [str(offset)]})

    def news_state(self):
        with self.news._db() as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: [tuple(row) for row in db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')] for name in names}

    def studio_state(self):
        with closing(self.store._connect()) as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: [tuple(row) for row in db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')] for name in names}

    def offline_consumer(self):
        """Run real request/consumer methods with a local ledger and fixed callback.

        No NewsAnalysis object/constructor, provider, credentials, or worker is
        created. The callback reports unknowns and merely records frozen input.
        """
        ledger = self.root / 'fixed-callback-ledger.sqlite'
        with closing(sqlite3.connect(ledger)) as db:
            db.execute('CREATE TABLE runs(fingerprint TEXT PRIMARY KEY,item_id TEXT,created REAL,reserved INTEGER,status TEXT,result TEXT DEFAULT "{}",provider TEXT,model TEXT,contract TEXT,rate_version TEXT,reservation_basis TEXT)')
        calls = []

        def fixed_callback(key, text):
            calls.append(json.loads(text))
            return SimpleNamespace(ok=True, model=MODEL, content=json.dumps(project_result()))

        facade = SimpleNamespace(path=ledger, news=self.news, _lock=threading.RLock(), _generation=0,
            _configured=lambda: True, _queue=queue.Queue(), _stop=threading.Event(), _provider='doubao',
            _model=MODEL, _automatic=False, _key='', _generate=fixed_callback, _active=False)
        for name in ('_db', 'market_result', 'request_market'):
            setattr(facade, name, getattr(NewsAnalysis, name).__get__(facade))
        def consume(*args, **kwargs):
            with patch('backend.news_platform_ai.datetime', wraps=datetime) as clock:
                clock.now.return_value = datetime.fromtimestamp(self.now, timezone.utc)
                return NewsAnalysis._analyze_once(facade, *args, **kwargs)
        facade._analyze_once = consume
        return facade, calls

    def test_pending_historical_market_survives_new_version_and_consumes_frozen_input_once(self):
        response, code = self.request()
        self.assertEqual(code, 200)
        historical = tasks.get(self.news, self.item_id, response['market_context']['context_id'])
        historical_task = tasks.task(self.news, historical)
        current = tasks.prepare(self.news, self.store, self.item_id)
        current_task = tasks.task(self.news, tasks.get(self.news, self.item_id, current['context_id']))
        fact_task = self.news.analysis_task_for_item(self.item_id)['id']
        self.now += 60
        self.save_body('Offline v3 newer correction is outside the historical cutoff.')
        with self.news._db() as db:
            statuses = {row['id']: row['status'] for row in db.execute('SELECT id,status FROM news_analysis_tasks')}
        self.assertEqual(statuses[historical_task], 'pending')
        self.assertEqual(statuses[current_task], 'skipped')
        self.assertEqual(statuses[fact_task], 'skipped')
        self.assertTrue(tasks.belongs_to_replay(self.news, historical, self.item_id))
        facade, calls = self.offline_consumer()
        self.assertEqual(facade.request_market(self.item_id, historical['id'], acknowledge_historical=True)['status'], 'queued')
        facade._analyze_once(self.item_id, task_id=historical_task, market_context_id=historical['id'])
        self.assertEqual(facade.market_result(historical['item'], historical['id'])['status'], 'completed')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['as_of'], CUTOFF)
        self.assertIn('revenue was USD 10 million', json.dumps(calls[0]))
        self.assertNotIn('Offline v3', json.dumps(calls[0]))
        facade._model = 'deepseek-flash'
        facade._provider = 'deepseek'
        self.assertEqual(facade.request_market(self.item_id, historical['id'], acknowledge_historical=True)['status'], 'completed')
        facade._analyze_once(self.item_id, task_id=historical_task, market_context_id=historical['id'])
        self.assertEqual(len(calls), 1)
        with facade._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)

    def test_claim_rechecks_current_context_after_request_version_race(self):
        current = tasks.prepare(self.news, self.store, self.item_id)
        saved = tasks.get(self.news, self.item_id, current['context_id'])
        facade, calls = self.offline_consumer()
        # The article changes after the requester checked its binding, before
        # the pending row is inserted. Claim must still refuse the stale row.
        self.now += 60
        self.save_body('Offline newer value created before the pending task insert.')
        task_id = tasks.task(self.news, saved)
        with self.assertRaisesRegex(ValueError, 'market_context_missing_or_version_changed'):
            facade.request_market(self.item_id, saved['id'])
        facade._analyze_once(self.item_id, task_id=task_id, market_context_id=saved['id'])
        with self.news._db() as db:
            state = dict(db.execute('SELECT * FROM news_analysis_tasks WHERE id=?', (task_id,)).fetchone())
        self.assertEqual(state['status'], 'skipped')
        self.assertEqual(state['reason'], 'context_or_version_no_longer_valid')
        self.assertEqual(calls, [])
        with facade._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 0)

    def test_invalid_saved_historical_bindings_cannot_use_trigger_to_survive_or_claim(self):
        for index, mode in enumerate(('hash', 'cutoff', 'version', 'task_binding')):
            with self.subTest(mode=mode):
                response, code = self.request(as_of=f'2026-10-29T16:0{index}:00Z')
                self.assertEqual(code, 200)
                saved = tasks.get(self.news, self.item_id, response['market_context']['context_id'])
                task_id = tasks.task(self.news, saved)
                changed = copy.deepcopy(saved['context'])
                if mode == 'hash':
                    changed['context_id'] = 'invalid'
                elif mode == 'cutoff':
                    changed['as_of'] = '2026-10-29T16:11:00Z'
                elif mode == 'version':
                    changed['article_version_id'] = self.v2['current_version_id']
                if mode not in ('hash', 'task_binding'):
                    changed['context_id'] = event_store.digest({key: value for key, value in changed.items() if key != 'context_id'})
                with self.news._db() as db:
                    db.execute('UPDATE news_market_contexts SET context=? WHERE id=?', (json.dumps(changed), saved['id']))
                    db.execute("UPDATE news_analysis_tasks SET trigger='historical_replay' WHERE id=?", (task_id,))
                    if mode == 'task_binding':
                        db.execute('UPDATE news_analysis_tasks SET version_id=? WHERE id=?', (self.v2['current_version_id'], task_id))
                if mode != 'task_binding':
                    self.assertFalse(tasks.belongs_to_replay(self.news, tasks.get(self.news, self.item_id, saved['id']), self.item_id))
                self.assertFalse(self.news.transition_analysis_task(task_id, 'running', expected_status='pending'))
                with self.news._db() as db:
                    self.assertEqual(db.execute('SELECT status FROM news_analysis_tasks WHERE id=?', (task_id,)).fetchone()[0], 'skipped')

    def test_legacy_database_without_context_table_and_terminal_tasks_remain_safe(self):
        with self.news._db() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='news_market_contexts'").fetchone())
            db.execute('''INSERT INTO news_analysis_tasks(id,item_id,event_id,version_id,evidence_package_id,task_type,
                trigger,version_seen_at,priority,status,updated_at)
                SELECT 'fake-replay',item_id,event_id,version_id,'absent-context','market_impact',
                'historical_replay',version_seen_at,priority,'pending',updated_at
                FROM news_analysis_tasks WHERE version_id=? LIMIT 1''', (self.v2['current_version_id'],))
        fact = self.news.analysis_task_for_item(self.item_id)
        self.assertTrue(self.news.transition_analysis_task(fact['id'], 'running', expected_status='pending'))
        self.assertTrue(self.news.transition_analysis_task(fact['id'], 'unknown', expected_status='running', reason='offline_interruption'))
        before = self.news_state()['news_analysis_tasks']
        self.now += 60
        self.save_body('Offline newer version in legacy news-only database.')
        with self.news._db() as db:
            self.assertEqual(db.execute("SELECT status FROM news_analysis_tasks WHERE id='fake-replay'").fetchone()[0], 'skipped')
            old = db.execute('SELECT * FROM news_analysis_tasks WHERE id=?', (fact['id'],)).fetchone()
            self.assertEqual(tuple(old), next(row for row in before if row[0] == fact['id']))
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='news_market_contexts'").fetchone())
        self.assertFalse(self.news.transition_analysis_task(fact['id'], 'running'))

    def test_news_only_prepare_without_ai_freezes_v1_and_default_get_excludes_replays(self):
        response, code = self.request()
        self.assertEqual(code, 200, response)
        context = response['market_context']
        self.assertEqual(context['mode'], 'historical_replay')
        self.assertEqual(context['article_version_id'], self.v1['current_version_id'])
        self.assertEqual(context['as_of'], CUTOFF)
        self.assertIsNone(response['market_impact'])
        self.assertIsNone(context['room_id'])
        saved = tasks.get(self.news, self.item_id, context['context_id'])
        self.assertEqual(saved['item']['evidence']['body_text'], self.v1['evidence']['body_text'])
        self.assertNotEqual(saved['item']['evidence']['body_text'], self.v2['evidence']['body_text'])
        self.assertTrue(tasks.belongs_to_replay(self.news, saved, self.item_id))
        self.assertIsNone(tasks.get(self.news, self.item_id))
        current = tasks.prepare(self.news, self.store, self.item_id)
        self.assertNotEqual(current['context_id'], context['context_id'])
        self.assertEqual(tasks.get(self.news, self.item_id)['id'], current['context_id'])
        self.assertEqual(tasks.get(self.news, self.item_id)['version_id'], self.v2['current_version_id'])
        again, code = self.request(as_of='2026-10-29T16:06:00Z')
        self.assertEqual(code, 200)
        self.assertEqual(tasks.get(self.news, self.item_id)['id'], current['context_id'])

    def test_bad_cutoffs_missing_pairs_and_versions_not_current_at_cutoff_never_fallback(self):
        before = self.news_state()
        cases = [
            {'as_of': '2026-10-29T18:00:00Z'},
            {'as_of': '2026-10-29T16:05:00'},
            {'as_of': 'invalid'},
            {'as_of': '2026-10-29T15:49:00Z'},
            {'version': self.v2['current_version_id']},
            {'version': self.v1['current_version_id'], 'as_of': '2026-10-29T16:11:00Z'},
            {'version': 'missing-version'},
            {'room': 'room_plan', 'round_id': None},
            {'room': None, 'round_id': 'some-round'},
        ]
        for values in cases:
            with self.subTest(values=values):
                response, code = self.request(**values)
                self.assertEqual(code, 400, response)
                self.assertFalse(response['ok'])
        base = {'room_id': None, 'round_id': None, 'material_refs': [],
                'version_id': self.v1['current_version_id'], 'as_of': CUTOFF}
        for missing in ('version_id', 'as_of'):
            payload = {key: value for key, value in base.items() if key != missing}
            response, code = news_response(self.news, f'/api/news/items/{self.item_id}/market/context', method='POST', payload=payload)
            self.assertEqual(code, 400, response)
        self.assertEqual(self.news_state(), before)

    def test_options_page_uses_older_rounds_and_material_versions_not_latest(self):
        material = self.add_material()
        first = self.freeze('2026-10-29T15:56:00Z')
        second = self.freeze('2026-10-29T16:00:00Z')
        update = material_payload(value=99)
        update['expected_version'] = 1
        with patch('backend.store.now_ms', return_value=ms('2026-10-29T16:12:00Z')):
            self.store.update_material('room_plan', material['id'], update)
        latest = self.freeze('2026-10-29T16:15:00Z')
        self.assertEqual(self.store.room_snapshot('room_plan')['latest_round']['id'], latest)
        with patch.object(self.store, 'room_snapshot', side_effect=AssertionError('historical_options_must_not_use_latest')):
            page1, status1 = self.options(limit=1)
            page2, status2 = self.options(limit=1, offset=1)
            page3, status3 = self.options(limit=1, offset=2)
        self.assertEqual([status1, status2, status3], [200, 200, 200])
        self.assertEqual([row['id'] for row in page1['rounds']], [second])
        self.assertEqual([row['id'] for row in page2['rounds']], [first])
        self.assertEqual(page3['rounds'], [])
        self.assertEqual(page1['total'], 2)
        self.assertEqual(page1['next_offset'], 1)
        self.assertFalse(page2['has_more'])
        self.assertEqual(page1['rounds'][0]['materials'], [{'id': material['id'], 'version': 1, 'title': material['title']}])

    def test_old_material_values_quotes_and_identity_are_frozen_and_canonical_asof_deduplicates(self):
        expected = self.add_material()
        actual = self.add_material(at='2026-10-29T15:55:00Z', kind='reported_actual', value=10, known_at=PUB)
        quote = saved_quote()
        round_id = self.freeze('2026-10-29T16:01:00Z', snapshot=quote)
        old_expected = self.store.get_material_version('room_plan', expected['id'], 1)
        for row, kind, value, when in ((expected, 'consensus', 99, '2026-10-29T16:12:00Z'),
                                      (actual, 'reported_actual', 77, '2026-10-29T16:13:00Z')):
            request = material_payload(kind, value, PUB if kind == 'reported_actual' else '2026-10-29T14:00:00Z')
            request['expected_version'] = 1
            with patch('backend.store.now_ms', return_value=ms(when)):
                self.store.update_material('room_plan', row['id'], request)
        refs = [{'material_id': row['id'], 'version': 1} for row in (expected, actual)]
        response, code = self.request(room='room_plan', round_id=round_id, refs=[refs[1], refs[0], refs[1]])
        self.assertEqual(code, 200, response)
        context = response['market_context']
        self.assertEqual(context['material_refs'], sorted(refs, key=lambda ref: (ref['material_id'], ref['version'])))
        self.assertEqual(context['consensus'][0]['value'], 12)
        self.assertEqual(context['reported_actual'][0]['value'], 10)
        self.assertEqual(context['comparisons'][0]['delta'], '-2')
        self.assertEqual(self.store.get_material_version('room_plan', expected['id'], 1), old_expected)
        self.assertEqual(context['market']['captured_at'], quote['captured_at'])
        self.assertEqual(context['market']['rows'][0]['market_time'], quote['rows'][0]['market_time'])
        same, code = self.request(room='room_plan', round_id=round_id, refs=list(reversed(refs)), as_of='2026-10-30T00:05:00+08:00')
        self.assertEqual(code, 200, same)
        self.assertEqual(same['market_context']['context_id'], context['context_id'])
        later, code = self.request(room='room_plan', round_id=round_id, refs=refs, as_of='2026-10-29T16:06:00Z')
        self.assertEqual(code, 200, later)
        self.assertNotEqual(later['market_context']['context_id'], context['context_id'])
        saved = tasks.get(self.news, self.item_id, context['context_id'])
        material = build_market_input(saved['item'], saved['context'], NOW)
        self.assertEqual(material['mode'], 'historical_replay')
        self.assertTrue(material['has_market'])
        self.assertEqual(material['freshness']['scope'], 'historical_cutoff_not_live_market')

    def test_historical_input_refuses_current_or_unidentified_article(self):
        response, code = self.request()
        self.assertEqual(code, 200, response)
        saved = tasks.get(self.news, self.item_id, response['market_context']['context_id'])
        valid = build_market_input(saved['item'], saved['context'], NOW)
        self.assertEqual(valid['article_version_id'], self.v1['current_version_id'])
        for item in (self.news.get_item(self.item_id), {**saved['item'], 'current_version_id': None}):
            with self.assertRaisesRegex(ValueError, 'market_replay_article_version_mismatch'):
                build_market_input(item, saved['context'], NOW)

    def test_future_checkpoint_and_future_material_version_cannot_bind(self):
        material = self.add_material()
        old_round = self.freeze('2026-10-29T16:01:00Z')
        future_checkpoint = self.freeze('2026-10-29T16:02:00Z', checkpoint_at='2026-10-29T16:07:00Z')
        edit = material_payload(value=99)
        edit['expected_version'] = 1
        with patch('backend.store.now_ms', return_value=ms('2026-10-29T16:08:00Z')):
            self.store.update_material('room_plan', material['id'], edit)
        for round_id, version in ((future_checkpoint, 1), (old_round, 2)):
            response, code = self.request(room='room_plan', round_id=round_id,
                                           refs=[{'material_id': material['id'], 'version': version}])
            self.assertEqual(code, 400, response)
        options, code = self.options()
        self.assertEqual(code, 200, options)
        self.assertEqual([row['id'] for row in options['rounds']], [old_round])
        self.assertEqual(options['rounds'][0]['materials'][0]['version'], 1)

    def test_future_saved_market_snapshot_is_excluded_from_options_and_prepare(self):
        # An intentionally inconsistent saved clock tests the read-time guard;
        # the real Store seals the data, but the quote was captured after cutoff.
        rejected = []
        for captured in ('2026-10-29T16:08:00Z', '2026-10-29T16:03:00Z'):
            bad_round = self.freeze('2026-10-29T16:02:00Z', snapshot=saved_quote(captured))
            response, code = self.request(room='room_plan', round_id=bad_round)
            self.assertEqual(code, 400, response)
            rejected.append(bad_round)
        options, code = self.options()
        self.assertEqual(code, 200, options)
        self.assertFalse(set(rejected) & {row['id'] for row in options['rounds']})

    def test_material_before_cutoff_but_after_checkpoint_is_not_known_to_that_round(self):
        # Explicit clock-anomaly fixture: the material exists by cutoff, but
        # cannot have been part of the earlier checkpoint's known evidence.
        material = self.add_material(at='2026-10-29T16:03:00Z')
        round_id = self.freeze('2026-10-29T16:02:00Z')
        options, code = self.options()
        self.assertEqual(code, 200, options)
        choice = next(row for row in options['rounds'] if row['id'] == round_id)
        self.assertEqual(choice['materials'], [])
        refs = [{'material_id': material['id'], 'version': 1}]
        response, code = self.request(room='room_plan', round_id=round_id, refs=refs)
        self.assertEqual(code, 400, response)
        with self.assertRaisesRegex(ValueError, 'market_material_not_known_at_checkpoint'):
            tasks.prepare(self.news, self.store, self.item_id, room_id='room_plan', round_id=round_id,
                          material_refs=refs, version_id=self.v1['current_version_id'], as_of=CUTOFF)

    def test_explicit_saved_context_and_paginated_replay_gets_are_read_only(self):
        news_before, studio_before = self.news_state(), self.studio_state()
        empty, code = news_response(self.news, f'/api/news/items/{self.item_id}/market/replays')
        self.assertEqual(code, 200)
        self.assertEqual(empty['contexts'], [])
        self.assertEqual(self.news_state(), news_before)
        self.assertEqual(self.studio_state(), studio_before)
        first, code = self.request()
        self.assertEqual(code, 200, first)
        second, code = self.request(as_of='2026-10-29T16:06:00Z')
        self.assertEqual(code, 200, second)
        frozen_news, frozen_studio = self.news_state(), self.studio_state()
        saved, code = news_response(self.news, f'/api/news/items/{self.item_id}/market/context',
            query={'context_id': [first['market_context']['context_id']]})
        self.assertEqual(code, 200, saved)
        self.assertEqual(saved['market_context'], first['market_context'])
        page, code = news_response(self.news, f'/api/news/items/{self.item_id}/market/replays',
            query={'version_id': [self.v1['current_version_id']], 'limit': ['1'], 'offset': ['0']})
        self.assertEqual(code, 200, page)
        self.assertEqual(page['total'], 2)
        self.assertEqual(len(page['contexts']), 1)
        self.assertEqual(page['contexts'][0]['article_version_id'], self.v1['current_version_id'])
        empty, code = news_response(self.news, f'/api/news/items/{self.item_id}/market/replays',
            query={'version_id': [self.v2['current_version_id']]})
        self.assertEqual(code, 200)
        self.assertEqual(empty['contexts'], [])
        for item_id, context_id, expected in ((self.item_id, 'missing-context', 404),
                ('another-item', first['market_context']['context_id'], 404)):
            response, code = news_response(self.news, f'/api/news/items/{item_id}/market/context', query={'context_id': [context_id]})
            self.assertEqual(code, expected, response)
        response, code = news_response(self.news, f'/api/news/items/{self.item_id}/market/context')
        self.assertEqual(code, 400, response)
        self.assertEqual(self.news_state(), frozen_news)
        self.assertEqual(self.studio_state(), frozen_studio)


if __name__ == '__main__':
    unittest.main()
