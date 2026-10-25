"""Pure RSS reading contract checks; no network, model or database access."""
import copy
from datetime import datetime, timezone
import json
import socket
import sqlite3
import unittest
from unittest.mock import patch

from backend.news_content_quality import (
    QUALITY_VERSION, build_input, inspect_material, insufficient_result, validate_output,
)


NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


def material_item(**changes):
    value = {'title': 'Issuer signs USD 40 million agreement subject to regulatory approval',
             'excerpt': 'The issuer signed an agreement for USD 40 million, subject to regulatory approval.',
             'source_name': 'Issuer IR', 'url': 'https://example.com/release',
             'published_at': '2026-10-20T10:00:00Z', 'discovered_at': '2026-10-20T10:05:00Z'}
    value.update(changes)
    return value


def output_for(item, field='excerpt', quote=None):
    return {'summary': '公司签署4000万美元协议，仍需监管批准。',
            'key_points': [{'claim': '已签约，仍附监管批准条件。', 'evidence_field': field,
                            'quote': item[field] if quote is None else quote}],
            'uncertainties': ['仅阅读订阅摘录，尚未核对全文或交割情况。']}


class NewsContentQualityTests(unittest.TestCase):
    def test_inspection_is_free_preserves_source_and_exposes_reading_boundaries(self):
        item = material_item()
        original = copy.deepcopy(item)
        inspected = inspect_material(item, NOW)
        self.assertEqual(inspected['status'], 'sufficient_excerpt')
        self.assertTrue(inspected['can_analyze'])
        self.assertFalse(inspected['scope']['full_text_read'])
        self.assertEqual(inspected['freshness']['age_seconds'], 7200)
        self.assertEqual(inspected['freshness']['publish_to_discovery_seconds'], 300)
        self.assertEqual(inspected['checked_at'], NOW.isoformat())
        self.assertTrue(any('不证明' in warning for warning in inspected['warnings']))
        request = build_input(item, checked_at=NOW)
        request['source_text']['title'] = 'changed local copy'
        self.assertEqual(item, original)

    def test_empty_or_whole_link_label_is_insufficient_but_mixed_business_text_is_not(self):
        for excerpt in ('', '  ', 'Read more', '  READ   MORE  ', '查看全文', 'https://example.com/full'):
            with self.subTest(excerpt=excerpt):
                item = material_item(excerpt=excerpt)
                inspection = inspect_material(item, NOW)
                self.assertEqual(inspection['status'], 'title_only')
                self.assertFalse(inspection['can_analyze'])
                result = insufficient_result(item, checked_at=NOW)
                self.assertEqual(result['status'], 'material_insufficient')
                self.assertFalse(result['analysis_performed'])
                self.assertEqual(result['evidence'], [])
        mixed = material_item(excerpt='Read more. Revenue was USD 12 million.')
        self.assertTrue(inspect_material(mixed, NOW)['can_analyze'])
        self.assertIsNone(insufficient_result(mixed, checked_at=NOW))

    def test_future_publication_overrides_recent_age_even_after_later_read(self):
        for published, discovered in (('2026-10-21T12:00:00Z', '2026-10-20T12:00:00Z'),
                                       ('2026-10-20T11:00:00Z', '2026-10-20T10:00:00Z')):
            with self.subTest(published=published):
                inspected = inspect_material(material_item(published_at=published, discovered_at=discovered), NOW)
                self.assertEqual(inspected['status'], 'clock_anomaly')
                self.assertTrue(inspected['can_analyze'])
                self.assertTrue(inspected['freshness']['clock_anomaly'])
                self.assertEqual(inspected['freshness']['publication_age'], 'unconfirmed')
                self.assertIsNone(inspected['freshness']['age_seconds'])
                self.assertLess(inspected['freshness']['publish_to_discovery_seconds'], 0)

    def test_old_publication_new_discovery_and_unknown_timezone_are_distinct(self):
        old = inspect_material(material_item(published_at='2026-10-18T12:00:00Z'), NOW)
        self.assertEqual(old['freshness']['publication_age'], 'older_than_24h')
        self.assertEqual(old['freshness']['age_seconds'], 172800)
        for stamp in (None, '', 'not a date', '2026-10-20T10:00:00'):
            with self.subTest(stamp=stamp):
                value = inspect_material(material_item(published_at=stamp), NOW)
                self.assertEqual(value['freshness']['publication_age'], 'unconfirmed')
                self.assertIsNone(value['freshness']['age_seconds'])
        with self.assertRaisesRegex(ValueError, 'check_time'):
            inspect_material(material_item(), datetime(2026, 10, 20, 12))

    def test_real_business_title_and_excerpt_each_support_visible_quote_projection(self):
        item = material_item()
        source = build_input(item, checked_at=NOW)
        for field in ('title', 'excerpt'):
            with self.subTest(field=field):
                value = output_for(item, field)
                original = copy.deepcopy(value)
                result = validate_output(json.dumps(value), source)
                self.assertEqual(result['key_points'], [value['key_points'][0]['claim']])
                self.assertEqual(result['evidence'], value['key_points'])
                self.assertEqual(result['quality_version'], QUALITY_VERSION)
                self.assertTrue(result['semantic_review_required'])
                self.assertFalse(result['scope']['full_text_read'])
                self.assertEqual(value, original)

    def test_wrong_field_changed_quote_and_metadata_cannot_supply_evidence(self):
        item = material_item()
        source = build_input(item, checked_at=NOW)
        for field, quote in (('source_name', item['source_name']), ('url', item['url']),
                             ('published_at', item['published_at']), ('excerpt', 'USD 41 million'),
                             ('title', item['excerpt'])):
            with self.subTest(field=field, quote=quote):
                value = output_for(item)
                value['key_points'][0].update(evidence_field=field, quote=quote)
                with self.assertRaises(ValueError):
                    validate_output(json.dumps(value), source)

    def test_exact_pure_label_is_rejected_without_banning_mixed_business_paragraph(self):
        item = material_item(excerpt='Read more. Revenue was USD 12 million in Q2 2026.')
        source = build_input(item, checked_at=NOW)
        with self.assertRaisesRegex(ValueError, 'context_only'):
            validate_output(json.dumps(output_for(item, quote='Read more.')), source)
        business = output_for(item, quote='Revenue was USD 12 million in Q2 2026.')
        self.assertEqual(validate_output(json.dumps(business), source)['evidence'][0]['quote'],
                         'Revenue was USD 12 million in Q2 2026.')

    def test_source_matching_does_not_claim_to_verify_semantic_meaning(self):
        item = material_item()
        value = output_for(item)
        value['key_points'][0]['claim'] = '这句话的含义仍需复核，引用匹配不是语义证明。'
        result = validate_output(json.dumps(value), build_input(item, checked_at=NOW))
        self.assertTrue(result['semantic_review_required'])
        self.assertIn('不证明', result['limitations'])
        self.assertNotIn('quality_gate_passed', result)

    def test_duplicate_keys_legacy_shape_empty_points_and_empty_uncertainty_are_rejected(self):
        item = material_item()
        source = build_input(item, checked_at=NOW)
        duplicate = '{"summary":"first","summary":"second","key_points":[],"uncertainties":[]}'
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            validate_output(duplicate, source)
        for changes in ({'key_points': ['unbound old point']}, {'key_points': []},
                        {'uncertainties': []}, {'uncertainties': ['   ']},
                        {'summary': 'x' * 501}):
            with self.subTest(changes=changes):
                value = output_for(item)
                value.update(changes)
                with self.assertRaises(ValueError):
                    validate_output(json.dumps(value), source)

    def test_rolling_entry_later_version_uses_local_update_not_first_discovery(self):
        item = material_item(published_at='2026-10-19T14:17:10Z',
                             discovered_at='2026-10-15T16:21:32.392901Z',
                             updated_at='2026-10-19T14:30:59.933802Z')
        inspected = inspect_material(item, NOW)
        fresh = inspected['freshness']
        self.assertEqual(inspected['status'], 'sufficient_excerpt')
        self.assertEqual(inspected['label'], '订阅条目有更新，全文未核对')
        self.assertTrue(fresh['revision_after_discovery'])
        self.assertFalse(fresh['clock_anomaly'])
        self.assertEqual(fresh['publication_age'], 'within_24h')
        self.assertEqual(fresh['age_seconds'], 78170)
        self.assertEqual(fresh['version_seen_at'], '2026-10-19T14:30:59.933802+00:00')
        self.assertAlmostEqual(fresh['publish_to_version_seen_seconds'], 829.933802, places=6)
        self.assertIsNone(fresh['publish_to_discovery_seconds'])
        self.assertTrue(any('首次发现属于更早版本' in warning for warning in inspected['warnings']))

    def test_real_future_publication_cannot_be_explained_away_by_update_metadata(self):
        for updated in ('2026-10-20T11:30:00Z', '2026-10-21T13:00:00Z'):
            with self.subTest(updated=updated):
                inspected = inspect_material(material_item(published_at='2026-10-21T12:00:00Z',
                    discovered_at='2026-10-20T10:00:00Z', updated_at=updated), NOW)
                fresh = inspected['freshness']
                self.assertEqual(inspected['status'], 'clock_anomaly')
                self.assertFalse(fresh['revision_after_discovery'])
                self.assertTrue(fresh['clock_anomaly'])
                self.assertEqual(fresh['publication_age'], 'unconfirmed')
                self.assertIsNone(fresh['age_seconds'])
                self.assertIsNone(fresh['version_seen_at'])
                self.assertIsNone(fresh['publish_to_version_seen_seconds'])

    def test_missing_invalid_future_or_misordered_update_does_not_clear_negative_ordering(self):
        for updated in (None, '', 'bad timestamp', '2026-10-20T11:30:00',
                        '2026-10-20T13:00:00Z', '2026-10-20T10:00:00Z', '2026-10-20T10:30:00Z'):
            with self.subTest(updated=updated):
                inspected = inspect_material(material_item(published_at='2026-10-20T11:00:00Z',
                    discovered_at='2026-10-20T10:00:00Z', updated_at=updated), NOW)
                self.assertFalse(inspected['freshness']['revision_after_discovery'])
                self.assertTrue(inspected['freshness']['clock_anomaly'])
                self.assertIsNone(inspected['freshness']['publish_to_version_seen_seconds'])

    def test_revision_inspection_and_projection_are_pure_and_preserve_original_material(self):
        item = material_item(published_at='2026-10-20T11:00:00Z',
            discovered_at='2026-10-20T10:00:00Z', updated_at='2026-10-20T11:00:00Z')
        original = copy.deepcopy(item)
        with patch('builtins.open', side_effect=AssertionError('no file IO')) as files, \
             patch.object(socket.socket, 'connect', side_effect=AssertionError('no network')) as network, \
             patch.object(sqlite3, 'connect', side_effect=AssertionError('no database')) as database:
            inspection = inspect_material(item, NOW)
            request = build_input(item, checked_at=NOW)
        files.assert_not_called()
        network.assert_not_called()
        database.assert_not_called()
        self.assertTrue(inspection['freshness']['revision_after_discovery'])
        self.assertEqual(inspection['freshness']['publish_to_version_seen_seconds'], 0)
        self.assertEqual(request['metadata']['updated_at'], original['updated_at'])
        request['metadata']['updated_at'] = 'caller changed copy'
        self.assertEqual(item, original)


if __name__ == '__main__':
    unittest.main()
