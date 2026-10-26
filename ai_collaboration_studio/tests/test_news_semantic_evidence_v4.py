"""Scoped v4 regressions: synthetic positive/negative/unknown pairs, no I/O."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import socket
import sqlite3
import unittest
from unittest.mock import patch

from backend.news_content_quality import (
    QUALITY_VERSION, RSS_V3_QUALITY_VERSION, RSS_V3_INSTRUCTIONS, RSS_V3_TEXT_FORMAT,
    SemanticReviewRequired, build_input, validate_output,
)

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def item(excerpt="Consumer Price Index (CPI): +0.6% in Sep 2026.", **changes):
    value = {"title": "Synthetic statistical release", "excerpt": excerpt, "source_name": "Synthetic official fixture",
             "url": "https://example.invalid/synthetic", "published_at": "2026-10-20T12:00:00Z",
             "discovered_at": "2026-10-20T12:05:00Z"}
    value.update(changes)
    return value


def output(source, assertion="CPI为+0.6%。", uncertainty=None):
    return {"summary": assertion, "key_points": [{"claim": assertion, "evidence_field": "excerpt", "quote": source["excerpt"]}],
            "uncertainties": uncertainty or ["仅阅读RSS摘录，尚未核对全文。"]}


def validate(source, result):
    return validate_output(json.dumps(result, ensure_ascii=False), build_input(source, checked_at=NOW))


class SemanticEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("network_forbidden"))
        self.database = patch.object(sqlite3, "connect", side_effect=AssertionError("database_forbidden"))
        self.dns = patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns_forbidden"))
        self.network.start(); self.database.start(); self.dns.start()

    def tearDown(self):
        self.network.stop(); self.database.stop(); self.dns.stop()

    def review(self, source, value, code, status=None):
        before = copy.deepcopy(value)
        with self.assertRaises(SemanticReviewRequired) as caught:
            validate(source, value)
        matches = [entry for entry in caught.exception.issues if entry["code"] == code]
        self.assertTrue(matches, caught.exception.issues)
        if status:
            self.assertTrue(any(entry["status"] == status for entry in matches), matches)
        self.assertEqual(value, before)
        self.assertEqual(caught.exception.candidate_result["summary"], value["summary"])
        return caught.exception

    def test_exact_v3_contract_values_frozen(self):
        self.assertEqual(RSS_V3_QUALITY_VERSION, "rss_excerpt_reading_v3")
        self.assertEqual(hashlib.sha256(RSS_V3_INSTRUCTIONS.encode()).hexdigest(), "a4b46556bb85cd0eca71b717e8c4f9fb33bfae51d7cf82beb28ecf0f10965078")
        self.assertEqual(hashlib.sha256(json.dumps(RSS_V3_TEXT_FORMAT, sort_keys=True).encode()).hexdigest(), "f91950d191a12c4472962fbd9704ab917627a4a4f8d0db68efbd6e26bd17ecb7")
        self.assertEqual(QUALITY_VERSION, "rss_excerpt_reading_v4")

    def test_explicit_comparison_basis_positive_and_opposite_negative(self):
        for original, claim, wrong in (("year-over-year", "同比", "环比"), ("month-over-month", "环比", "同比")):
            source = item("Consumer Price Index (CPI): +0.6% " + original + " in Sep 2026.")
            good = validate(source, output(source, "2026年9月CPI" + claim + "上涨0.6%。"))
            self.assertEqual(good["semantic_review"]["status"], "scoped_supported")
            self.assertFalse(good["semantic_review"]["general_truth_verified"])
            self.review(source, output(source, "CPI" + wrong + "上涨0.6%。"), "statistical_comparison_basis", "contradicted")

    def test_missing_basis_is_unknown_not_inferred_from_percentage(self):
        source = item()
        self.review(source, output(source, "CPI环比上涨0.6%。"), "statistical_comparison_basis", "unknown")
        self.review(source, output(source, "CPI同比上涨0.6%。"), "statistical_comparison_basis", "unknown")
        value = validate(source, output(source, "CPI为+0.6%。", ["仅阅读RSS摘录，未核对全文；同比和环比口径未知。 "]))
        self.assertFalse(value["semantic_review"]["issues"])

    def test_two_explicit_bases_cannot_swap_numbers(self):
        source = item("CPI: 3.0% year-over-year, 0.6% month-over-month in Sep 2026.")
        for claim in ("2026年9月CPI同比上涨3.0%。", "2026年9月CPI环比上涨0.6%。", "CPI同比上涨3.0%，环比上涨0.6%。"):
            self.assertFalse(validate(source, output(source, claim))["semantic_review"]["issues"], claim)
        self.review(source, output(source, "CPI环比上涨3.0%。"), "statistical_comparison_basis", "contradicted")
        self.review(source, output(source, "CPI同比上涨0.6%。"), "statistical_comparison_basis", "contradicted")

    def test_repeated_metric_periods_cannot_be_cross_paired(self):
        source = item("CPI: 0.5% month-over-month in Aug 2026. CPI: 0.6% month-over-month in Sep 2026.")
        self.assertFalse(validate(source, output(source, "2026年9月CPI环比上涨0.6%。"))["semantic_review"]["issues"])
        self.review(source, output(source, "2026年8月CPI环比上涨0.6%。"), "statistical_period", "contradicted")

    def test_separate_sentence_period_and_respectively_do_not_steal_values(self):
        source = item("U.S. Import Price Index: +1.1% in Sep 2026. U.S. Export Price Index: +2.2% in Sep 2026. Employment Cost Index: +0.9% in 2nd Qtr of 2026. Productivity: +1.4% in 2nd Qtr of 2026.")
        claim = "2026年9月进口和出口价格指数分别为+1.1%、+2.2%。2026年二季度就业成本指数为+0.9%、生产率为+1.4%。公告已超过24小时。"
        self.assertFalse(validate(source, output(source, claim))["semantic_review"]["issues"])
        self.assertFalse(validate(source, output(source, "进口价格指数为+1.1%，出口价格指数为+2.2%；2026年第二季度就业成本指数为+0.9%，生产率为+1.4%。"))["semantic_review"]["issues"])
        self.review(source, output(source, "进口和出口价格指数分别为+2.2%、+1.1%。"), "statistical_value_unit", "contradicted")

    def test_source_supported_basis_does_not_excuse_conflicting_uncertainty(self):
        source = item("CPI: 0.6% month-over-month in Sep 2026.")
        self.review(source, output(source, "CPI环比上涨0.6%。", ["仅阅读RSS摘录。", "CPI口径未知。"]), "assertion_uncertainty_conflict", "contradicted")
        self.assertFalse(validate(source, output(source, "CPI环比上涨0.6%。", ["仅阅读RSS摘录。", "其他指标的口径未知。", "PPI口径未给出。 "]))["semantic_review"]["issues"])

    def test_unrelated_unknown_reason_cannot_hide_basis_assertion(self):
        source = item()
        self.review(source, output(source, "具体原因未知，CPI环比上涨0.6%。"), "statistical_comparison_basis", "unknown")
        self.review(source, output(source, "CPI同比上涨0.6%。", ["原文没有注明比较口径。"]), "assertion_uncertainty_conflict", "contradicted")

    def test_explicit_money_tuple_currency_scale_and_period(self):
        source = item("Synthetic issuer revenue was USD 10 million in Q2 2026.")
        self.assertFalse(validate(source, output(source, "合成发行人2026年第二季度收入为1000万美元。"))["semantic_review"]["issues"])
        for claim in ("合成发行人2026年第二季度收入为10亿元。", "合成发行人2026年第二季度收入为1000万欧元。", "合成发行人2026年第二季度收入为100万美元。"):
            self.review(source, output(source, claim), "monetary_value_currency", "contradicted")
        self.review(source, output(source, "合成发行人2026年第三季度收入为1000万美元。"), "monetary_period", "contradicted")
        missing = item("Synthetic issuer discusses revenue without stating an amount.")
        self.review(missing, output(missing, "合成发行人收入为1000万美元。"), "monetary_value_currency", "unknown")

    def test_basis_must_belong_to_metric_and_own_point_quote(self):
        source = item("Consumer Price Index (CPI): +0.6% in Sep 2026. Producer Price Index (PPI): +1.1% year-over-year in Sep 2026.")
        self.review(source, output(source, "CPI同比上涨0.6%。"), "statistical_comparison_basis", "unknown")
        explicit = item("Consumer Price Index (CPI): +0.6% year-over-year in Sep 2026.")
        result = output(explicit, "CPI同比上涨0.6%。")
        result["key_points"][0]["quote"] = "Consumer Price Index (CPI): +0.6%"
        exc = self.review(explicit, result, "statistical_comparison_basis", "unknown")
        self.assertTrue(any(issue["field"] == "key_points.0.claim" for issue in exc.issues))

    def test_uncertainty_cannot_cancel_positive_summary_assertion(self):
        source = item()
        result = output(source, "CPI环比上涨0.6%。", ["仅阅读RSS摘录。", "摘录未说明比较基准，不能确认同比或环比。"])
        exc = self.review(source, result, "assertion_uncertainty_conflict", "contradicted")
        self.assertEqual(exc.candidate_result["uncertainties"], result["uncertainties"])

    def test_numbers_units_period_are_bound_to_recognized_metric(self):
        source = item()
        for claim, code in (("CPI上涨6%。", "statistical_value_unit"), ("CPI上涨0.6个百分点。", "statistical_value_unit"),
                            ("2026年8月CPI为0.6%。", "statistical_period")):
            self.review(source, output(source, claim), code, "contradicted")
        payroll = item("Payroll Employment: +29,000 in Sep 2026.")
        self.assertFalse(validate(payroll, output(payroll, "非农就业人数增加2.9万。"))["semantic_review"]["issues"])
        self.review(payroll, output(payroll, "非农就业人数增加29,000美元。"), "statistical_value_unit", "contradicted")

    def test_action_count_presupposition_inside_uncertainty(self):
        source = item("Synthetic Board announces termination of enforcement actions with Synthetic Alder and Synthetic Birch.")
        value = output(source, "公告终止涉及两家机构的执法行动。", ["只读RSS摘录；不清楚两项行动是否完全解除监管。"])
        self.review(source, value, "enforcement_action_count", "unknown")
        explicit = item("Synthetic Board announces termination of two enforcement actions with Synthetic Alder and Synthetic Birch.")
        self.assertFalse(validate(explicit, output(explicit, "公告终止涉及两家机构的两项行动。"))["semantic_review"]["issues"])

    def test_explicit_against_not_made_unknown_but_other_legal_limits_remain(self):
        source = item("Synthetic Board announces an enforcement action against Synthetic Alder.")
        good = output(source, "公告宣布一项针对合成机构的执法行动。", ["仅阅读RSS摘录，未核对全文；未知罚款金额和违法责任认定。"])
        self.assertFalse(validate(source, good)["semantic_review"]["issues"])
        bad = output(source, "公告宣布一项涉及合成机构的执法行动。", ["仅表示原文关联，不据此确认处罚方向或法律责任。"])
        self.review(source, bad, "explicit_relationship_direction_obscured", "contradicted")

    def test_with_direction_still_requires_review_and_retains_error_contract(self):
        source = item("Synthetic Board announces enforcement actions with Synthetic Alder.")
        exc = self.review(source, output(source, "公告宣布针对合成机构的执法行动。"), "analysis_unsupported_relationship_direction", "unknown")
        self.assertIn("unsupported_relationship_direction", str(exc))

    def test_old_publication_correct_direction_and_backwards_word(self):
        source = item()
        good = output(source, "公告标注发布时间早于本次检查，已超过24小时。")
        result = validate(source, good)
        self.assertEqual(result["deterministic_time"]["publication_age_seconds"], 6 * 86400)
        self.review(source, output(source, "公告标注发布时间晚于本次检查24小时以上。"), "publication_check_direction", "contradicted")

    def test_old_newly_discovered_and_old_discovery_differ(self):
        fresh_discovery = item(discovered_at="2026-10-26T11:50:00Z")
        value = output(fresh_discovery, "公告已超过24小时；该公告是新发现，不代表新事件。")
        result = validate(fresh_discovery, value)
        self.assertEqual(result["deterministic_time"]["discovery_age_seconds"], 600)
        self.review(item(), output(item(), value["summary"]), "discovery_recency_claim", "contradicted")
        self.assertFalse(validate(item(), output(item(), "新发现不代表新事件；当前只读RSS摘录。"))["semantic_review"]["issues"])
        self.assertFalse(validate(item(), output(item(), "该公告并非本次新发现，也并非最近24小时内发布。"))["semantic_review"]["issues"])
        self.review(fresh_discovery, output(fresh_discovery, "该公告并非本次新发现。"), "discovery_recency_claim", "contradicted")

    def test_revision_and_future_and_unknown_time_remain_separate(self):
        revision = item(published_at="2026-10-25T10:00:00Z", discovered_at="2026-10-20T10:00:00Z", updated_at="2026-10-25T10:05:00Z")
        result = validate(revision, output(revision))
        self.assertTrue(result["deterministic_time"]["revision_after_discovery"])
        self.assertEqual(result["freshness"]["publish_to_discovery_seconds"], None)
        future = item(published_at="2026-10-27T12:00:00Z", discovered_at="2026-10-26T11:00:00Z")
        self.review(future, output(future, "公告已发布。"), "publication_not_confirmed", "unknown")
        safe = validate(future, output(future, "来源时间异常，发布未确认。"))
        self.assertTrue(safe["deterministic_time"]["publication_is_future"])
        missing = item(published_at=None, discovered_at=None)
        self.review(missing, output(missing, "该公告是新发现。"), "discovery_recency_claim", "unknown")

    def test_unhandled_semantics_not_labeled_proven(self):
        source = item("Synthetic issuer announces a conditional agreement.")
        result = validate(source, output(source, "合成发行人宣布附条件协议。"))
        self.assertEqual(result["semantic_review"]["status"], "unassessed")
        self.assertTrue(result["semantic_review_required"])
        self.assertFalse(result["semantic_review"]["general_truth_verified"])


if __name__ == "__main__":
    unittest.main()
