import copy
import hashlib
import json
import os
import unittest
from datetime import datetime

os.environ['AI_STUDIO_SKIP_LOCAL_ENV'] = '1'

from backend.news_market_context import resolve_saved_context, build_market_input, validate_output, project_result
from backend.store import StudioStore


def ms(value):
    return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)


ITEM = {'id':'news1', 'title':'Quarter revenue', 'excerpt':'Revenue was USD 10 million in Q4.',
        'published_at':'2026-10-29T15:50:00Z', 'version_seen_at':'2026-10-29T15:51:00Z',
        'discovered_at':'2026-10-29T15:51:00Z', 'symbols':['US.MU'],
        'url':'https://investors.micron.com/quarter', 'evidence': {'content_status':'excerpt_only','segments':[]}}
ASOF = '2026-10-29T16:05:00Z'


class FrozenStore(StudioStore):
    """In-memory saved records; actual existing manifest/hash validators run."""
    def __init__(self):
        self.material = {'id':'consensus1', 'room_id':'room1', 'version':1, 'active':True,
            'title':'Pre-event consensus', 'kind':'note', 'source_url':'https://example.invalid/consensus',
            'content':'Q4 revenue consensus USD 12 million.', 'changed_at':ms('2026-10-29T15:00:00Z'),
            'metadata':{'market_context':{'kind':'consensus','symbol':'US.MU','metric':'revenue','period':'FY2026-Q4',
                'unit':'million','currency':'USD','comparison_basis':'reported_revenue','value':12,'known_at':'2026-10-29T14:00:00Z','quote':'Q4 revenue consensus USD 12 million.'}}}
        self.actual = None
        self.snapshot = {'ok':True,'state':'ready','snapshot_id':'snapshot1','captured_at':'2026-10-29T16:00:00Z',
            'source':'futu_opend','source_errors':[],'missing_symbols':[],
            'execution_capability':'none','live_trading_allowed':False,'evidence':{'version':'storage_market_evidence_v6'},
            'rows':[{'symbol':symbol,'last':100 + index,'volume':1000,'market_time':'2026-10-29 11:59:30',
                     'updated_at':'2026-10-29T15:59:30Z','quality':'ready','age_seconds':30,'quote_is_live':True,
                     'freshness_basis':'live_20m_window'} for index,symbol in enumerate(('US.MU','US.SNDK','US.WDC','US.STX'))]}
        self.reseal()

    def reseal(self):
        materials = [self.material] + ([self.actual] if self.actual else [])
        blocks = [self._material_prompt_block_v1(row,len(row['content'])) for row in materials]
        block = '\n\n'.join(blocks)
        self.manifest = {'version':'round_evidence_manifest_v1','renderer':'material_prompt_v1','max_chars':14000,
            'materials':[{'id':row['id'],'version':1,'content_sha256':self._sha256_text(row['content']),
                'snapshot_sha256':self._material_snapshot_sha256(row),'body_chars':len(row['content']),
                'prompt_chars':len(piece),'truncated':False} for row,piece in zip(materials,blocks)],
            'quarantined_materials':[],'omitted_material_ids':[],
            'material_context_sha256':self._sha256_text(block),'context_sha256':self._sha256_text(block),
            'market_snapshot':self._market_snapshot_manifest_entry(self.snapshot),
            'execution_capability':'none','live_trading_allowed':False}
        self.checkpoint={'room_id':'room1','round_id':'round1','updated_at':ms('2026-10-29T16:01:00Z'),
            'protocol_integrity_ok':True,'state':{'shared_context':block,'round_evidence_manifest':self.manifest,'market_snapshot':self.snapshot}}

    def get_round_checkpoint(self, room_id, round_id):
        return copy.deepcopy(self.checkpoint)

    def get_material_version(self, room_id, material_id, version):
        if self.actual and (room_id,material_id,version)==('room1','actual1',1):
            return copy.deepcopy(self.actual)
        return copy.deepcopy(self.material) if (room_id,material_id,version)==('room1','consensus1',1) else None

    def add_actual(self):
        self.actual = copy.deepcopy(self.material)
        self.actual.update(id='actual1',title='Official results',source_url=ITEM['url'],
            content='Q4 revenue actual USD 10 million.',changed_at=ms('2026-10-29T15:55:00Z'))
        self.actual['metadata']['market_context'].update(kind='reported_actual',value=10,
            known_at='2026-10-29T15:50:00Z',quote=self.actual['content'],source_type='company_ir',event_url=ITEM['url'])
        self.reseal()


class MarketContextTests(unittest.TestCase):
    def resolve(self,store,**kwargs):
        return resolve_saved_context(store,ITEM,'room1','round1',[{'id':'consensus1','version':1}],kwargs.get('as_of',ASOF))

    def test_missing_context_remains_unknown_without_accessing_store(self):
        context=resolve_saved_context(None,ITEM,as_of=ASOF)
        material=build_market_input(ITEM,context,ASOF)
        self.assertFalse(material['has_consensus'])
        self.assertFalse(material['has_market'])
        self.assertEqual(validate_output(project_result(),material),project_result())

    def test_saved_material_and_shared_quote_use_existing_manifest_validation(self):
        store=FrozenStore()
        context=self.resolve(store)
        self.assertEqual(context['consensus'][0]['value'],12)
        self.assertEqual(context['market']['status'],'available')
        self.assertEqual(context['market']['rows'][0]['market_time'],'2026-10-29 11:59:30')
        self.assertEqual(len(context['market']['rows']),1)
        self.assertEqual(len(context['context_id']),64)

    def test_market_snapshot_tamper_is_rejected(self):
        store=FrozenStore(); store.snapshot['rows'][0]['last']=999
        with self.assertRaises(ValueError):
            self.resolve(store)

    def test_material_tamper_is_rejected(self):
        store=FrozenStore(); store.material['content']='changed after seal'
        with self.assertRaises(ValueError):
            self.resolve(store)

    def test_late_recorded_consensus_cannot_backfill_pre_event_expectation(self):
        store=FrozenStore(); store.material['changed_at']=ms('2026-10-29T16:00:00Z')
        context=self.resolve(store)
        self.assertEqual(context['consensus'],[])
        self.assertTrue(any('发布前' in row for row in context['missing']))

    def test_late_source_timestamp_and_unstructured_material_remain_unknown(self):
        store=FrozenStore(); store.material['metadata']['market_context']['known_at']='2026-10-29T16:00:00Z'; store.reseal()
        self.assertEqual(self.resolve(store)['consensus'],[])
        store=FrozenStore(); store.material['metadata']={}; store.reseal()
        self.assertEqual(self.resolve(store)['consensus'],[])

    def test_historical_replay_rejects_future_checkpoint_and_news_version(self):
        with self.assertRaisesRegex(ValueError,'checkpoint_not_known'):
            self.resolve(FrozenStore(),as_of='2026-10-29T15:55:00Z')
        item={**ITEM,'version_seen_at':'2026-10-29T18:00:00Z'}
        with self.assertRaisesRegex(ValueError,'news_version_not_known'):
            resolve_saved_context(None,item,as_of=ASOF)

    def test_snapshot_stale_at_requested_time_is_not_current(self):
        context=self.resolve(FrozenStore(),as_of='2026-10-29T17:00:00Z')
        self.assertEqual(context['market']['status'],'stale_or_unverified')
        self.assertFalse(any(row['type']=='market_snapshot' for row in context['evidence']))

    def test_unmanifested_version_and_protocol_failure_are_rejected(self):
        store=FrozenStore()
        with self.assertRaisesRegex(ValueError,'not_in_manifest'):
            resolve_saved_context(store,ITEM,'room1','round1',[{'id':'consensus1','version':2}],ASOF)
        store.checkpoint['protocol_integrity_ok']=False
        with self.assertRaisesRegex(ValueError,'checkpoint_unverified'):
            self.resolve(store)

    def test_output_citations_numbers_context_and_pricing_claims(self):
        context=resolve_saved_context(None,ITEM,as_of=ASOF)
        material=build_market_input(ITEM,context,ASOF)
        output=project_result()
        output['facts']=[{'claim':'季度收入为 USD 10 million。','evidence_id':'article:excerpt','quote':ITEM['excerpt']}]
        self.assertEqual(len(validate_output(output,material)['facts']),1)
        bad=copy.deepcopy(output);bad['facts'][0]['claim']='季度收入为 USD 100 million。'
        with self.assertRaisesRegex(ValueError,'numeric_assertion'):
            validate_output(bad,material)
        bad=copy.deepcopy(output);bad['facts'][0]['evidence_id']='invented'
        with self.assertRaisesRegex(ValueError,'citation_not_in_evidence'):
            validate_output(bad,material)
        bad=copy.deepcopy(output);bad['expectation_comparison']['status']='context_available'
        with self.assertRaises(ValueError):validate_output(bad,material)
        bad=copy.deepcopy(output);bad['facts'][0]['claim']='消息已充分定价。'
        with self.assertRaisesRegex(ValueError,'pricing_unproven'):validate_output(bad,material)

    def test_context_hash_cannot_be_mutated_before_input(self):
        context=self.resolve(FrozenStore());context['market']['rows'][0]['last']=123
        with self.assertRaisesRegex(ValueError,'context_changed'):
            build_market_input(ITEM,context,ASOF)

    def test_execution_time_rechecks_market_freshness(self):
        context=self.resolve(FrozenStore())
        self.assertEqual(context['market']['status'],'available')
        material=build_market_input(ITEM,context,'2026-10-29T17:00:00Z')
        self.assertFalse(material['has_market'])
        self.assertEqual(material['freshness']['market_asof'],'2026-10-29T16:00:00Z')
        self.assertEqual(material['freshness']['market_status'],'stale_at_execution')
        self.assertFalse(any(row['type']=='market_snapshot' for row in material['evidence']))

    def comparison_context(self,store):
        return resolve_saved_context(store,ITEM,'room1','round1',
            [{'id':'consensus1','version':1},{'id':'actual1','version':1}],ASOF)

    def test_actual_matching_comparison_is_computed_and_output_exact(self):
        store=FrozenStore();store.add_actual()
        context=self.comparison_context(store)
        self.assertEqual(len(context['comparisons']),1)
        comparison=context['comparisons'][0]
        self.assertEqual(comparison['delta'],'-2')
        self.assertEqual(comparison['relation'],'lower')
        material=build_market_input(ITEM,context,ASOF)
        sources={row['id']:row for row in material['evidence']}
        output=project_result()
        output['expectation_comparison']={'status':'evidence_supported','claim':comparison['claim'],
            'evidence':[{'evidence_id':key,'quote':sources[key]['text']} for key in (comparison['actual_evidence_id'],comparison['consensus_evidence_id'])]}
        self.assertEqual(validate_output(output,material)['expectation_comparison']['status'],'evidence_supported')
        bad=copy.deepcopy(output);bad['expectation_comparison']['claim']=comparison['claim'].replace('差额 -2','差额 2')
        with self.assertRaisesRegex(ValueError,'comparison_not_exact'):validate_output(bad,material)

    def test_comparison_requires_matching_unit_period_currency_and_basis(self):
        for field,value in [('unit','billion'),('period','FY2027-Q1'),('currency','EUR'),('comparison_basis','constant_currency')]:
            store=FrozenStore();store.add_actual();store.actual['metadata']['market_context'][field]=value;store.reseal()
            self.assertEqual(self.comparison_context(store)['comparisons'],[])

    def test_actual_must_be_same_event_official_and_known_by_cutoff(self):
        store=FrozenStore();store.add_actual();store.actual['source_url']='https://example.invalid/pretend-official';store.reseal()
        self.assertEqual(self.comparison_context(store)['reported_actual'],[])
        store=FrozenStore();store.add_actual();store.actual['metadata']['market_context']['event_url']=ITEM['url']+'/another-event';store.reseal()
        self.assertEqual(self.comparison_context(store)['reported_actual'],[])
        store=FrozenStore();store.add_actual();store.actual['metadata']['market_context']['known_at']='2026-10-29T17:00:00Z';store.reseal()
        self.assertEqual(self.comparison_context(store)['reported_actual'],[])

    def test_beat_claim_cannot_hide_in_fact_or_inferred_without_comparison(self):
        material=build_market_input(ITEM,resolve_saved_context(None,ITEM,as_of=ASOF),ASOF)
        for field in ('facts','inferred_impacts'):
            output=project_result()
            assertion={'claim':'本次收入超预期。','evidence_id':'article:excerpt','quote':ITEM['excerpt']}
            if field=='inferred_impacts':assertion.update(channel='收入',horizon='下一季度',condition='需要进一步核对。')
            output[field]=[assertion]
            with self.assertRaisesRegex(ValueError,'comparable_actual_unverified'):validate_output(output,material)

    def test_utf8_input_selection_keeps_whole_records_and_drops_incomplete_comparison(self):
        store=FrozenStore();store.add_actual()
        for row in (store.material,store.actual):
            row['content']=(row['content'] + '范围条件'*500)[:2000]
            row['metadata']['market_context']['quote']=row['content']
        store.reseal()
        context=self.comparison_context(store)
        self.assertEqual(len(context['comparisons']),1)
        material=build_market_input(ITEM,context,ASOF)
        self.assertLessEqual(len(json.dumps(material,ensure_ascii=False).encode()),8500)
        self.assertTrue(material['truncated'])
        self.assertGreater(material['omitted_evidence_count'],0)
        self.assertEqual(material['comparisons'],[])
        original={row['id']:row['text'] for row in context['evidence']}
        for row in material['evidence']:
            if row['id'] in original:self.assertEqual(row['text'],original[row['id']])

    def test_selected_room_requires_passed_saved_store(self):
        with self.assertRaisesRegex(ValueError,'saved_store_unavailable'):
            resolve_saved_context(None,ITEM,'room1','round1',[],ASOF)


if __name__=='__main__':
    unittest.main()
