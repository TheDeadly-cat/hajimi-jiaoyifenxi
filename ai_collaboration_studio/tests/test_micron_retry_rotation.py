"""Bounded old-metadata recovery with no external network or article-body reads."""
import unittest

from backend.market.micron_ir_json import MicronIrJsonClient
from tests import test_micron_ir_json as fixtures
from tests.test_micron_ir_json import NOW, HOST, head, metadata, record


class MicronRetryRotationTests(unittest.TestCase):
    def setup_client(self):
        fetch = fixtures.MicronIrIncrementalTests.fixture()
        client = MicronIrJsonClient(fetch_bytes=fetch, clock=lambda: NOW)
        client.read_recent()
        return fetch, client

    def fail_first_identity(self, fetch, client):
        row = fetch.rows[0]
        url = HOST + row['LinkToDetailPage']
        good = fetch.heads[url]
        fetch.heads[url] = head(metadata(row, datePublished='invalid'))
        result = client.read_recent(require_complete=False)
        self.assertFalse(result['complete'])
        self.assertNotIn(1, {r['q4_press_release_id'] for r in result['releases']})
        return url, good

    def test_transient_failed_identity_recovers_on_next_poll_with_same_four_head_budget(self):
        fetch, client = self.setup_client()
        url, good = self.fail_first_identity(fetch, client)
        fetch.heads[url] = good
        before = len(fetch.calls)
        result = client.read_recent(require_complete=False)
        self.assertTrue(result['complete'])
        self.assertIn(1, result['metadata_progress']['requested_ids'])
        self.assertEqual(len(fetch.calls) - before, 5)  # One list + four heads.
        self.assertEqual(len(result['releases']), 30)

    def test_persistent_failure_stays_withheld_while_other_identities_keep_rotating(self):
        fetch, client = self.setup_client()
        self.fail_first_identity(fetch, client)
        seen = set()
        for _ in range(10):
            before = len(fetch.calls)
            result = client.read_recent(require_complete=False)
            self.assertFalse(result['complete'])
            self.assertNotIn(1, {r['q4_press_release_id'] for r in result['releases']})
            self.assertEqual(len(fetch.calls) - before, 5)
            requested = result['metadata_progress']['requested_ids']
            self.assertEqual(len(requested), 4)
            self.assertEqual(len(set(requested)), 4)
            seen.update(requested)
        self.assertEqual(seen, set(range(1, 31)))

    def test_new_identity_precedes_old_retry_and_failed_evidence_is_not_reused(self):
        fetch, client = self.setup_client()
        self.fail_first_identity(fetch, client)
        new = record(31)
        new['LinkToDetailPage'] = '/news/press-release/2026/new-retry-31/default.aspx'
        new_url = HOST + new['LinkToDetailPage']
        fetch.rows = fetch.rows[:-1] + [new]
        fetch.heads[new_url] = head(metadata(new))
        before = len(fetch.calls)
        result = client.read_recent(require_complete=False)
        self.assertEqual(fetch.calls[before + 1][0], new_url)
        self.assertIn(1, result['metadata_progress']['requested_ids'])
        self.assertIn(31, {r['q4_press_release_id'] for r in result['releases']})
        self.assertNotIn(1, {r['q4_press_release_id'] for r in result['releases']})
        self.assertEqual(len(fetch.calls) - before, 6)
        self.assertFalse(result['complete'])

    def test_multiple_failed_identities_all_get_a_bounded_retry_without_starving_others(self):
        fetch, client = self.setup_client()
        for row in fetch.rows[:4]:
            fetch.heads[HOST + row['LinkToDetailPage']] = head(metadata(row, datePublished='invalid'))
        self.assertFalse(client.read_recent(require_complete=False)['complete'])
        retried = set()
        for _ in range(4):
            result = client.read_recent(require_complete=False)
            requested = set(result['metadata_progress']['requested_ids'])
            retried.update(requested & {1, 2, 3, 4})
            self.assertEqual(len(requested), 4)
            self.assertTrue(requested - {1, 2, 3, 4})
        self.assertEqual(retried, {1, 2, 3, 4})


if __name__ == '__main__':
    unittest.main()
