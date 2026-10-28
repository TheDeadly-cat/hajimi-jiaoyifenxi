"""Real temporary StudioStore persistence, not a substitute metadata dictionary."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ['AI_STUDIO_SKIP_LOCAL_ENV'] = '1'

from backend.store import StudioStore
from backend.news_market_material import prepare_market_material_payload, clean_market_context_metadata
from backend.news_market_context import resolve_saved_context
from tests.test_news_market_context import ITEM, ASOF, ms


def payload(kind='consensus',value=12,known_at='2026-10-29T14:00:00Z'):
    options = dict(kind=kind,symbol='US.MU',metric='revenue',period='FY2026-Q4',unit='million',
        currency='USD',comparison_basis='reported_revenue',value=value,known_at=known_at,
        quote=f'FY2026-Q4 revenue USD {value} million.', source_url='https://example.invalid/consensus')
    if kind=='reported_actual':
        options.update(source_url=ITEM['url'],event_url=ITEM['url'],source_type='company_ir')
    return prepare_market_material_payload(**options)


class MarketMaterialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=StudioStore(Path(self.temp.name)/'studio.db')

    def test_actual_add_read_update_preserves_nested_metadata_and_real_saved_time(self):
        request=payload()
        request.update(created_at=1,updated_at=1,changed_at=1)
        with patch('backend.store.now_ms',return_value=ms('2026-10-29T15:00:00Z')):
            saved=self.store.add_material('room_plan',request)
        version=self.store.get_material_version('room_plan',saved['id'],1)
        self.assertEqual(version['metadata']['market_context'],request['metadata']['market_context'])
        self.assertEqual(version['changed_at'],ms('2026-10-29T15:00:00Z'))
        edited=payload(value=13)
        edited['expected_version']=1
        with patch('backend.store.now_ms',return_value=ms('2026-10-29T15:10:00Z')):
            updated=self.store.update_material('room_plan',saved['id'],edited)
        self.assertEqual(updated['version'],2)
        self.assertEqual(self.store.get_material_version('room_plan',saved['id'],1)['metadata']['market_context']['value'],12)
        self.assertEqual(self.store.get_material_version('room_plan',saved['id'],2)['metadata']['market_context']['value'],13)

    def test_strict_nested_schema_and_exact_content_are_enforced_at_store_boundary(self):
        for key,value in [('value',True),('value',float('inf')),('value','12'),('known_at','2026-10-29T14:00:00'),
                          ('symbol','BTC'),('kind','inference'),('metric','x'*81)]:
            request=payload();request['metadata']['market_context'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                self.store.add_material('room_plan',request)
        request=payload();request['metadata']['market_context']['changed_at']=1
        with self.assertRaisesRegex(ValueError,'metadata_fields_invalid'):
            self.store.add_material('room_plan',request)
        request=payload();request['content']='A different document without the stated quote.'
        with self.assertRaisesRegex(ValueError,'quote_not_in_content'):
            self.store.add_material('room_plan',request)
        request=payload();request['metadata']['market_context']['value']=99
        with self.assertRaisesRegex(ValueError,'value_not_in_quote'):
            self.store.add_material('room_plan',request)

    def freeze(self):
        with patch('backend.store.now_ms',return_value=ms('2026-10-29T16:01:00Z')):
            round_row=self.store.create_round('room_plan','Offline saved market evidence fixture')
            shared,manifest=self.store.material_prompt_bundle('room_plan')
            manifest=self.store.finalize_round_evidence_manifest(manifest,shared_context=shared,market_snapshot=None)
            self.store.save_round_checkpoint('room_plan',round_row['id'],{
                'member_ids':[],'spoken_counts':{},'spoken_stances':[], 'successful_member_ids':[],
                'failed_member_ids':[],'previous_name':'','completed':0,'failures':0,'skipped':0,
                'proposals_created':0,'next_order':1,'max_turns':1,'shared_context':shared,
                'market_snapshot':None,'round_evidence_manifest':manifest})
        return round_row['id']

    def test_real_saved_versions_checkpoint_and_resolver_support_comparison(self):
        with patch('backend.store.now_ms',return_value=ms('2026-10-29T15:00:00Z')):
            expected=self.store.add_material('room_plan',payload())
        with patch('backend.store.now_ms',return_value=ms('2026-10-29T15:55:00Z')):
            actual=self.store.add_material('room_plan',payload('reported_actual',10,'2026-10-29T15:50:00Z'))
        round_id=self.freeze()
        refs=[{'id':row['id'],'version':1} for row in (expected,actual)]
        context=resolve_saved_context(self.store,ITEM,'room_plan',round_id,refs,ASOF)
        self.assertEqual(len(context['comparisons']),1)
        self.assertEqual(context['comparisons'][0]['delta'],'-2')

    def test_late_entered_expected_time_cannot_backdate_actual_store_version(self):
        request=payload();request.update(created_at=ms('2026-10-29T14:00:00Z'),changed_at=ms('2026-10-29T14:00:00Z'))
        with patch('backend.store.now_ms',return_value=ms('2026-10-29T15:55:00Z')):
            saved=self.store.add_material('room_plan',request)
        round_id=self.freeze()
        context=resolve_saved_context(self.store,ITEM,'room_plan',round_id,[{'id':saved['id'],'version':1}],ASOF)
        self.assertEqual(context['consensus'],[])
        self.assertEqual(context['comparisons'],[])

    def test_future_publication_does_not_become_an_occurred_event(self):
        future={**ITEM,'published_at':'2026-10-30T00:00:00Z'}
        with self.assertRaisesRegex(ValueError,'publication_future'):
            resolve_saved_context(None,future,as_of=ASOF)


if __name__=='__main__':unittest.main()
