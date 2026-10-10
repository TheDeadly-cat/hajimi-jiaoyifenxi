"""Offline request/signoff boundaries, not actual provider quality evidence."""
import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.decision_lineage import canonical_sha256
from backend.news_review_contracts import ENDPOINT, MODEL, STRATEGY_SHA, NewsReviewError, validate_result
from backend.news_review_quality_preparation import (
    QUALITY_INSTRUCTIONS, QUALITY_STRATEGY_SHA, prepare, signoff_template, strict_json,
)

CORPUS_RAW = (Path(__file__).parent / "fixtures/news_review_quality_v1.json").read_bytes()
CANDIDATE = "a" * 40


def raw(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class QualityPreparationTests(unittest.TestCase):
    def draft(self, references=None, **changes):
        return prepare(CORPUS_RAW, candidate_sha=CANDIDATE,
                       reference_raw=raw(references) if references is not None else None, **changes)

    def signed_fixture(self):
        references = signoff_template(CORPUS_RAW)
        for entry in references["cases"]:
            entry.update(approved=True, reviewer="synthetic-test-reviewer",
                         reviewed_at="2026-09-27T00:00:00+00:00")
        return references

    def test_whole_request_hash_route_and_budget_are_bound(self):
        value = self.draft()
        self.assertEqual((value["case_count"], value["request_count"]), (36, 36))
        for entry in value["requests"]:
            body = entry["request_utf8"].encode("utf-8")
            self.assertEqual(hashlib.sha256(body).hexdigest(), entry["request_sha256"])
            self.assertEqual(len(body), entry["request_bytes"])
            self.assertLessEqual(len(body), 4096)
            payload = json.loads(body)
            self.assertEqual((payload["model"], payload["instructions"], payload["max_output_tokens"]),
                             (MODEL, QUALITY_INSTRUCTIONS, 1400))
            self.assertEqual(entry["reserved_input_tokens"], len(body) + 256)
            self.assertEqual(json.loads(payload["input"])["document"]["origin"], "synthetic_fixture")
        self.assertEqual(value["endpoint"], ENDPOINT)
        self.assertEqual(value["strategy_sha256"], QUALITY_STRATEGY_SHA)
        self.assertNotEqual(value["strategy_sha256"], STRATEGY_SHA)
        self.assertLessEqual(value["reservation"]["total_tokens"], 207072)
        self.assertFalse(value["reservation"]["prices_independently_verified"])
        self.assertFalse(value["candidate_verified_by_cli"])

    def test_model_input_contains_complete_original_body_not_reference(self):
        value = self.draft()
        corpus = {case["id"]: case for case in json.loads(CORPUS_RAW)["cases"]}
        for entry in value["requests"]:
            evidence = json.loads(json.loads(entry["request_utf8"])["input"])
            text = "\n".join(p["text"] for p in evidence["document"]["paragraphs"])
            original = corpus[entry["case_id"]]["body"]
            self.assertTrue(original in text or original.replace("USD 10 million", "USD 11 million") in text)
            self.assertNotIn("reference_draft", evidence)

    def test_missing_and_truncated_cases_have_no_paid_request(self):
        value = self.draft()
        cases = {case["case_id"]: case for case in value["cases"]}
        self.assertEqual(sum(any(o["action"] == "send_once" for o in c["observations"]) for c in cases.values()), 34)
        for case_id in ("Q28_missing_body", "Q29_truncated_body"):
            self.assertEqual([o["action"] for o in cases[case_id]["observations"]], ["do_not_send"])
            self.assertFalse(any(r["case_id"] == case_id for r in value["requests"]))

    def test_quote_text_cannot_rescue_a_mistyped_paragraph_identity(self):
        request = self.draft()["requests"][0]
        document = json.loads(json.loads(request["request_utf8"])["input"])["document"]
        paragraph = document["paragraphs"][1]
        result = {
            "version": "news_event_review_v1", "assessment": "reviewed", "summary": "合成正文已读。",
            "importance": {"level": "high", "reason": "材料内事项需要关注。"},
            "facts": [{"claim": "给定原文包含该表述。", "paragraph_id": paragraph["id"], "quote": paragraph["text"]}],
            "inferences": [], "counterevidence": [], "open_questions": [], "limitations": ["仅合成正文，不核验外部事实。"],
        }
        validate_result(json.dumps(result, ensure_ascii=False), document)
        original = copy.deepcopy(result)
        # This is a synthetic typo, not a copied private provider response.
        result["facts"][0]["paragraph_id"] = paragraph["id"].replace(":p0002", "abc:p0002")
        self.assertNotEqual(result["facts"][0]["paragraph_id"], paragraph["id"])
        self.assertEqual(result["facts"][0]["quote"], original["facts"][0]["quote"])
        self.assertIn(result["facts"][0]["quote"], paragraph["text"])
        with self.assertRaises(NewsReviewError) as error:
            validate_result(json.dumps(result, ensure_ascii=False), document)
        self.assertEqual(error.exception.code, "unsupported_quote")

    def test_duplicate_revision_restore_preserve_version_reuse(self):
        cases = {case["case_id"]: case["observations"] for case in self.draft()["cases"]}
        self.assertEqual([o["action"] for o in cases["Q23_duplicate"]], ["send_once", "reuse"])
        self.assertEqual(cases["Q23_duplicate"][0]["request_id"], cases["Q23_duplicate"][1]["request_id"])
        self.assertEqual([o["action"] for o in cases["Q24_revision"]], ["send_once", "send_once"])
        restored = cases["Q25_restore"]
        self.assertEqual([o["action"] for o in restored], ["send_once", "send_once", "reuse"])
        self.assertEqual(restored[0]["request_id"], restored[2]["request_id"])
        self.assertNotEqual(restored[0]["request_id"], restored[1]["request_id"])

    def test_declared_signatures_never_authorize_execution(self):
        signed = self.draft(self.signed_fixture())
        unsigned = self.draft()
        self.assertEqual(signed["references"]["declared_approved_cases"], 36)
        self.assertEqual(unsigned["references"]["declared_approved_cases"], 0)
        self.assertEqual(signed["requests"], unsigned["requests"])
        for value in (signed, unsigned):
            for field in ("request_authorized", "executor_verified", "natural_event_acceptance", "model_quality_verified"):
                self.assertFalse(value[field])
            self.assertFalse(value["references"]["human_identity_independently_verified"])

    def test_changed_reference_changes_binding_but_never_model_input(self):
        references = self.signed_fixture()
        first = self.draft(references)
        references["cases"][0]["reference"]["must_preserve"] = "SECRET_GOLD_MARKER_NOT_A_MODEL_INPUT"
        second = self.draft(references)
        self.assertNotEqual(first["references"]["file_sha256"], second["references"]["file_sha256"])
        self.assertEqual(first["requests"], second["requests"])
        self.assertNotIn("SECRET_GOLD_MARKER", json.dumps(second["requests"]))
        self.assertNotEqual(canonical_sha256(first), canonical_sha256(second))

    def test_changed_classification_references_never_supply_model_answers(self):
        references = self.signed_fixture()
        first = self.draft(references)
        for entry in references["cases"]:
            entry["reference"]["importance"] = "normal"
            entry["reference"]["assessment"] = "material_insufficient"
            entry["reference"]["supported_facts"] = ["GOLD_CLASSIFICATION_MUST_NOT_ENTER_REQUEST"]
        second = self.draft(references)
        self.assertNotEqual(first["references"]["file_sha256"], second["references"]["file_sha256"])
        self.assertEqual(first["requests"], second["requests"])
        self.assertNotIn("GOLD_CLASSIFICATION", json.dumps(second["requests"]))
        self.assertFalse(second["request_authorized"])

    def test_missing_duplicate_unknown_or_rebound_reference_rejected(self):
        for change in (lambda r: r["cases"].pop(),
                       lambda r: r["cases"].__setitem__(1, copy.deepcopy(r["cases"][0])),
                       lambda r: r["cases"][0].update(case_id="invented"),
                       lambda r: r["cases"][0].update(case_sha256="0" * 64),
                       lambda r: r.update(corpus_sha256="0" * 64),
                       lambda r: r.update(intended_use="live_source_collection")):
            with self.subTest(change=change):
                value = self.signed_fixture()
                change(value)
                with self.assertRaises(ValueError):
                    self.draft(value)

    def test_signature_requires_attribution_and_timezone(self):
        for update in ({"reviewer": None}, {"reviewed_at": "not-a-date"},
                       {"reviewed_at": "2026-09-27T00:00:00"}, {"approved": 1},
                       {"approved": False}):
            with self.subTest(update=update):
                value = self.signed_fixture()
                value["cases"][0].update(update)
                with self.assertRaises(ValueError):
                    self.draft(value)

    def test_fixed_corpus_cannot_be_silently_edited_or_shortened(self):
        value = json.loads(CORPUS_RAW)
        value["cases"].pop()
        with self.assertRaisesRegex(ValueError, "fixed_corpus_changed"):
            prepare(raw(value), candidate_sha=CANDIDATE)
        with self.assertRaisesRegex(ValueError, "fixed_corpus_changed"):
            prepare(CORPUS_RAW + b" ", candidate_sha=CANDIDATE)

    def test_small_byte_or_total_budget_fails_without_truncation(self):
        for constant, limit in (("MAX_BYTES", 1024), ("MAX_TOKENS", 1024), ("MAX_COST", 0)):
            with self.subTest(constant=constant), patch("backend.news_review_quality_preparation." + constant, limit):
                with self.assertRaisesRegex(ValueError, "budget_exceeded"):
                    self.draft()

    def test_deterministic_and_candidate_bound(self):
        first = self.draft()
        self.assertEqual(first, self.draft())
        second = prepare(CORPUS_RAW, candidate_sha="b" * 40)
        self.assertNotEqual(canonical_sha256(first), canonical_sha256(second))
        self.assertEqual(first["requests"], second["requests"])

    def test_duplicate_keys_and_nonfinite_json_fail(self):
        for value in (b'{"cases": [], "cases": []}', b'{"value":NaN}'):
            with self.assertRaises(ValueError):
                strict_json(value)


if __name__ == "__main__":
    unittest.main()
