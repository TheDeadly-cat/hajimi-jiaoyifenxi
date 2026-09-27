"""One temp database per test; no model requests or real approvals occur."""
import json
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from backend.instance_ownership import DatabaseInstanceOwner
from backend.news_review_quality_ledger import QualityBatchLedger, build_batch_plan
from backend.news_review_quality_preparation import signoff_template
from backend.provider_call_ledger import ProviderCallBudgetExceeded, ProviderCallLedger
from backend.store import StudioStore

CORPUS_RAW = (Path(__file__).parent / "fixtures/news_review_quality_v1.json").read_bytes()
CANDIDATE = "a" * 40


class QualityBatchLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quality-ledger-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "NewsQualityBenchmarkSynthetic" / "data" / "studio.sqlite3"
        self.owner = DatabaseInstanceOwner(self.path).acquire()
        self.addCleanup(self.owner.release)
        self.store = StudioStore(self.path)
        self.addCleanup(lambda: self.store.checkpoint_after_shutdown(instance_owner=self.owner))
        self.room = self.store.create_room("Synthetic quality ledger", "No model calls")["room"]["id"]
        reference = signoff_template(CORPUS_RAW)
        for row in reference["cases"]:
            row.update(approved=True, reviewer="synthetic-test-reviewer", reviewed_at="2026-09-27T00:00:00+00:00")
        self.references = json.dumps(reference).encode("utf-8")
        self.now, self.mono = 1790463867701, 10000
        self.config = dict(corpus_raw=CORPUS_RAW, reference_raw=self.references, candidate_sha=CANDIDATE,
                           room_id=self.room, not_before_ms=self.now, expires_at_ms=self.now + 10000)
        self.plan = build_batch_plan(database_path=self.path, **self.config)
        self.identity = self.enterContext(patch("backend.news_review_quality_ledger.verify_candidate"))

    def start(self, **overrides):
        params = dict(approved_plan_sha256=self.plan["plan_sha256"],
                      human_reference_review_acknowledged=True, wall_ms=lambda: self.now, monotonic_ms=lambda: self.mono)
        params.update(overrides)
        return QualityBatchLedger(self.store, self.owner, **self.config, **params)

    def request_id(self, index=0):
        return self.plan["prepared"]["requests"][index]["id"]

    def responded(self, batch, attempt):
        return batch.finish(attempt["id"], status="RESPONDED", http_attempted=True,
                            response_sha256="b" * 64, usage={"input_tokens": 100, "output_tokens": 50})

    def test_whole_batch_has_one_permanent_ledger_and_no_37th_slot(self):
        batch = self.start()
        for request in self.plan["prepared"]["requests"]:
            attempt = batch.reserve(request["id"])
            batch.before_send(attempt["id"])
            batch.final_admission_check(attempt["id"])
            self.responded(batch, attempt)
        state = batch.snapshot()
        self.assertEqual(state["calls_reserved"], 36)
        self.assertEqual(state["tokens_reserved"], self.plan["prepared"]["reservation"]["total_tokens"])
        self.assertEqual(state["cost_reserved_cny"], self.plan["prepared"]["reservation"]["estimated_cny"])
        self.assertTrue(state["closed"])
        with self.assertRaisesRegex(ValueError, "batch_completed"):
            batch.reserve(self.request_id())
        foreign = ProviderCallLedger.create(self.store, self.room, scope="synthetic_second_run",
            client_request_id="synthetic", plan={"synthetic": True}, max_calls=36)
        with self.assertRaises(ProviderCallBudgetExceeded):
            foreign.reserve(kind="synthetic", provider="doubao", database_max_calls=36)

    def test_duplicate_request_and_unplanned_request_cannot_claim(self):
        batch = self.start()
        attempt = batch.reserve(self.request_id())
        self.responded(batch, attempt)
        with self.assertRaisesRegex(ValueError, "already_claimed"):
            batch.reserve(self.request_id())
        with self.assertRaisesRegex(ValueError, "not_in_plan"):
            batch.reserve("invented")
        self.assertEqual(batch.snapshot()["calls_reserved"], 1)

    def test_concurrent_reservations_allow_one_inflight_attempt(self):
        batch = self.start()
        def reserve():
            try:
                return batch.reserve(self.request_id())
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: reserve(), range(2)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(batch.snapshot()["calls_reserved"], 1)

    def test_unknown_retains_reservation_and_stops_remaining_batch(self):
        batch = self.start()
        attempt = batch.reserve(self.request_id())
        receipt = batch.finish(attempt["id"], status="UNKNOWN", http_attempted=True)
        self.assertEqual(receipt["status"], "UNKNOWN")
        state = batch.snapshot()
        self.assertEqual(state["calls_reserved"], 1)
        self.assertTrue(state["closed"])
        with self.assertRaisesRegex(ValueError, "quality_unknown"):
            batch.reserve(self.request_id(1))
        with self.assertRaisesRegex(ValueError, "not_fresh"):
            self.start()

    def test_oversize_reported_usage_becomes_unknown_not_refunded(self):
        batch = self.start()
        attempt = batch.reserve(self.request_id())
        receipt = batch.finish(attempt["id"], status="RESPONDED", http_attempted=True,
            response_sha256="b"*64, usage={"input_tokens": 100, "output_tokens": 1401})
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(batch.snapshot()["calls_reserved"], 1)

    def test_pre_send_failure_keeps_reservation_without_claiming_http(self):
        batch = self.start()
        attempt = batch.reserve(self.request_id())
        receipt = batch.finish(attempt["id"], status="FAILED", http_attempted=False)
        self.assertFalse(receipt["http_attempted"])
        self.assertEqual(batch.snapshot()["calls_reserved"], 1)
        self.assertTrue(batch.snapshot()["closed"])

    def test_expiry_after_preflight_fails_at_final_admission_and_is_latched(self):
        batch = self.start()
        attempt = batch.reserve(self.request_id())
        batch.before_send(attempt["id"])
        self.now += 10000
        self.mono += 10000
        with self.assertRaisesRegex(ValueError, "window_expired"):
            batch.final_admission_check(attempt["id"])
        self.now -= 10000
        self.mono -= 10000
        with self.assertRaisesRegex(ValueError, "window_expired"):
            batch.final_admission_check(attempt["id"])
        self.assertEqual(batch.snapshot()["calls_reserved"], 1)

    def test_clock_failure_is_latched_even_if_wall_clock_restored(self):
        batch = self.start()
        self.now += 2001
        with self.assertRaisesRegex(ValueError, "clock_changed"):
            batch.reserve(self.request_id())
        self.now -= 2001
        with self.assertRaisesRegex(ValueError, "clock_changed"):
            batch.reserve(self.request_id())
        self.assertEqual(batch.snapshot()["calls_reserved"], 0)

    def test_late_start_gets_only_remaining_window_even_with_small_wall_drift(self):
        self.now += 2000
        self.mono += 2000
        batch = self.start()
        attempt = batch.reserve(self.request_id())
        batch.before_send(attempt["id"])
        self.now += 7999
        self.mono += 8000
        with self.assertRaisesRegex(ValueError, "duration_expired"):
            batch.final_admission_check(attempt["id"])
        self.assertEqual(batch.snapshot()["calls_reserved"], 1)

    def test_interrupted_empty_claim_is_not_resumed(self):
        with patch("backend.news_review_quality_ledger.ProviderCallLedger.create", side_effect=OSError("synthetic crash")):
            with self.assertRaises(OSError):
                self.start()
        with self.assertRaises(FileExistsError):
            self.start()

    def test_receipt_write_failure_stops_and_keeps_claim(self):
        batch = self.start()
        attempt = batch.reserve(self.request_id())
        with patch("backend.news_review_quality_ledger.publish_json_once", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.responded(batch, attempt)
        self.assertEqual(batch.snapshot()["stop_code"], "quality_outcome_persistence_failed")
        self.assertEqual(batch.snapshot()["calls_reserved"], 1)
        with self.assertRaises(ValueError):
            batch.reserve(self.request_id(1))

    def test_missing_approval_or_reference_acknowledgement_has_no_start_receipt(self):
        for changes in ({"approved_plan_sha256": ""}, {"human_reference_review_acknowledged": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.start(**changes)
        self.assertFalse((self.path.parent.parent / "quality-batch-receipts").exists())

    def test_candidate_change_and_wrong_attempt_are_rejected(self):
        batch = self.start()
        with self.assertRaisesRegex(ValueError, "not_inflight"):
            batch.final_admission_check("invented")
        self.identity.side_effect = ValueError("synthetic candidate changed")
        with self.assertRaisesRegex(ValueError, "candidate changed"):
            batch.reserve(self.request_id())
        self.assertEqual(batch.snapshot()["calls_reserved"], 0)

    def test_caller_cannot_mutate_the_frozen_plan(self):
        batch = self.start()
        public = batch.plan
        public["expires_at_ms"] += 86400000
        public["prepared"]["requests"][0]["id"] = "invented"
        self.assertEqual(batch.plan, self.plan)
        with self.assertRaisesRegex(ValueError, "not_in_plan"):
            batch.reserve("invented")

    def test_wrong_database_or_unsigned_references_cannot_build_batch_plan(self):
        for changes in ({"database_path": self.path.parent / "other.sqlite3"},
                        {"expires_at_ms": self.now + 10_800_001},
                        {"reference_raw": json.dumps(signoff_template(CORPUS_RAW)).encode()},
                        {"reference_raw": None}):
            with self.subTest(changes=list(changes)), self.assertRaises(ValueError):
                build_batch_plan(**(dict(self.config, database_path=self.path) | changes))

    def test_false_response_without_send_is_rejected_and_claim_retained(self):
        batch = self.start()
        attempt = batch.reserve(self.request_id())
        with self.assertRaisesRegex(ValueError, "response_without_send"):
            batch.finish(attempt["id"], status="RESPONDED", http_attempted=False,
                         response_sha256="b" * 64, usage={"input_tokens": 100, "output_tokens": 50})
        self.assertEqual(batch.snapshot()["inflight_attempt_id"], attempt["id"])
        self.assertEqual(batch.snapshot()["calls_reserved"], 1)

    def test_late_last_response_cannot_replace_an_existing_stop_reason(self):
        batch = self.start()
        for request in self.plan["prepared"]["requests"][:-1]:
            self.responded(batch, batch.reserve(request["id"]))
        attempt = batch.reserve(self.request_id(35))
        batch.before_send(attempt["id"])
        self.now += 10000
        self.mono += 10000
        with self.assertRaisesRegex(ValueError, "window_expired"):
            batch.admission_check()
        self.responded(batch, attempt)
        self.assertEqual(batch.snapshot()["stop_code"], "quality_window_expired")
        self.assertEqual(batch.snapshot()["calls_reserved"], 36)
        self.assertEqual(batch.snapshot()["observed_stop_codes"], ["quality_window_expired", "quality_batch_completed"])

    def test_late_unknown_cannot_replace_the_first_clock_failure(self):
        batch = self.start()
        attempt = batch.reserve(self.request_id())
        self.now += 2001
        with self.assertRaisesRegex(ValueError, "clock_changed"):
            batch.admission_check()
        receipt = batch.finish(attempt["id"], status="UNKNOWN", http_attempted=True)
        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(batch.snapshot()["stop_code"], "quality_clock_changed")
        self.assertEqual(batch.snapshot()["observed_stop_codes"], ["quality_clock_changed", "quality_unknown"])

    def test_persistence_failure_is_retained_without_replacing_first_stop(self):
        batch = self.start()
        attempt = batch.reserve(self.request_id())
        self.now += 2001
        with self.assertRaises(ValueError):
            batch.admission_check()
        with patch("backend.news_review_quality_ledger.publish_json_once", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.responded(batch, attempt)
        state = batch.snapshot()
        self.assertEqual(state["stop_code"], "quality_clock_changed")
        self.assertIn("quality_outcome_persistence_failed", state["observed_stop_codes"])
        self.assertEqual(state["calls_reserved"], 1)


if __name__ == "__main__":
    unittest.main()
