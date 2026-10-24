"""Pure route contracts: no database, server, provider or network is opened."""
from __future__ import annotations

import unittest
from unittest.mock import Mock

from backend.news_platform_routes import news_response


class NewsPlatformRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = Mock()
        self.runtime.ai = Mock()
        self.runtime.ai.status.return_value = {"configured": False, "max_calls": 36}
        self.runtime.ai.result.return_value = {"status": "completed", "summary": "合成测试摘要"}
        self.runtime.overview.return_value = {"enabled": True, "unread_count": 3}
        self.runtime.list_items.return_value = {"items": [], "total": 0}
        self.runtime.get_item.return_value = {"id": "news-123", "title": "合成测试消息"}
        self.runtime.mark_read.return_value = {"id": "news-123", "read": True}

    def test_unavailable_runtime_has_safe_service_error(self) -> None:
        body, status = news_response(None, "/api/news/overview")
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])

    def test_list_filters_and_pagination_reach_runtime_exactly(self) -> None:
        query = {"q": ["利率"], "source": ["fed"], "category": ["macro"],
                 "unread": ["1"], "importance": ["high"], "limit": ["25"], "offset": ["50"]}
        body, status = news_response(self.runtime, "/api/news/items", query=query)
        self.assertEqual((status, body["ok"]), (200, True))
        self.runtime.list_items.assert_called_once_with(query="利率", source="fed", category="macro",
                                                       unread_only=True, importance="high", limit=25, offset=50)

    def test_bad_queries_never_call_runtime(self) -> None:
        invalid_queries = ({"q": ["one", "two"]}, {"limit": ["0"]}, {"limit": ["101"]},
                           {"offset": ["-1"]}, {"offset": ["1000001"]}, {"limit": ["NaN"]},
                           {"q": ["x" * 201]}, {"unread": ["true"]}, {"unexpected": ["x"]})
        for query in invalid_queries:
            with self.subTest(query=query):
                body, status = news_response(self.runtime, "/api/news/items", query=query)
                self.assertEqual(status, 400)
                self.assertFalse(body["ok"])
        self.runtime.list_items.assert_not_called()
        body, status = news_response(self.runtime, "/api/news/overview", query={"q": ["x"]})
        self.assertEqual(status, 400)
        self.runtime.overview.assert_not_called()

    def test_boolean_mutations_are_exact_and_reject_extra_fields(self) -> None:
        for payload in ({"enabled": 1}, {"enabled": "true"}, {}, {"enabled": True, "extra": 1}, None, []):
            with self.subTest(payload=payload):
                self.assertEqual(news_response(self.runtime, "/api/news/control", payload=payload, method="POST")[1], 400)
        self.runtime.control.assert_not_called()
        body, status = news_response(self.runtime, "/api/news/control", payload={"enabled": False}, method="POST")
        self.assertEqual(status, 200)
        self.runtime.control.assert_called_once_with(False)
        self.assertIn("ai", body["overview"])
        for payload in ({"read": 1}, {"read": "true"}, {"read": True, "extra": 1}):
            self.assertEqual(news_response(self.runtime, "/api/news/items/news-123/read", payload=payload, method="POST")[1], 400)
        self.runtime.mark_read.assert_not_called()

    def test_refresh_clear_and_analysis_reject_payload_and_query_extras(self) -> None:
        for path in ("/api/news/refresh", "/api/news/ai/clear", "/api/news/items/news-123/analyze"):
            with self.subTest(path=path):
                self.assertEqual(news_response(self.runtime, path, payload={"extra": 1}, method="POST")[1], 400)
                self.assertEqual(news_response(self.runtime, path, payload={}, query={"extra": ["1"]}, method="POST")[1], 400)
        self.runtime.refresh.assert_not_called()
        self.runtime.ai.clear.assert_not_called()
        self.runtime.ai.request.assert_not_called()
        self.runtime.ai.request.return_value = {"status": "queued"}
        self.assertEqual(news_response(self.runtime, "/api/news/items/news-123/analyze", payload={}, method="POST"),
                         ({"ok": True, "ai_analysis": {"status": "queued"}}, 202))
        self.runtime.ai.request.assert_called_once_with("news-123")

    def test_item_and_read_responses_keep_analysis_and_unknown_item_is_404(self) -> None:
        body, status = news_response(self.runtime, "/api/news/items/news-123")
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["ai_analysis"]["status"], "completed")
        body, status = news_response(self.runtime, "/api/news/items/news-123/read", payload={"read": True}, method="POST")
        self.assertEqual(status, 200)
        self.runtime.mark_read.assert_called_once_with("news-123", read=True)
        self.runtime.get_item.return_value = None
        self.assertEqual(news_response(self.runtime, "/api/news/items/missing")[1], 404)
        self.runtime.get_item.reset_mock()
        for path in ("/api/news/items/../private", "/api/news/items/" + "a" * 129, "/api/news/items/x%2fy"):
            self.assertEqual(news_response(self.runtime, path)[1], 404)
        self.runtime.get_item.assert_not_called()

    def test_session_configuration_is_closed_and_errors_do_not_echo_key(self) -> None:
        synthetic = "synthetic-key-for-test"
        for payload in ({"api_key": synthetic}, {"api_key": synthetic, "automatic": False, "acknowledge_budget": True, "extra": True}):
            self.assertEqual(news_response(self.runtime, "/api/news/ai/session", payload=payload, method="POST")[1], 400)
        self.runtime.ai.configure.assert_not_called()
        self.runtime.ai.configure.return_value = {"configured": True}
        body, status = news_response(self.runtime, "/api/news/ai/session",
            payload={"api_key": synthetic, "automatic": False, "acknowledge_budget": True}, method="POST")
        self.assertEqual((body, status), ({"ok": True, "ai": {"configured": True}}, 200))
        self.runtime.ai.configure.assert_called_once_with(api_key=synthetic, automatic=False, acknowledge_budget=True)
        self.runtime.ai.configure.side_effect = ValueError(synthetic)
        body, status = news_response(self.runtime, "/api/news/ai/session",
            payload={"api_key": synthetic, "automatic": False, "acknowledge_budget": True}, method="POST")
        self.assertEqual(status, 400)
        self.assertNotIn(synthetic, str(body))

    def test_operational_failure_returns_safe_503(self) -> None:
        self.runtime.list_items.side_effect = RuntimeError("private diagnostic not for browser")
        body, status = news_response(self.runtime, "/api/news/items")
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.assertNotIn("private diagnostic", str(body))

    def test_explicit_analysis_route_is_forwarded_without_rewriting_key_or_route(self):
        payload = {"api_key": "synthetic-deepseek-key", "automatic": False,
                   "acknowledge_budget": True, "provider": "deepseek", "model": "deepseek-flash"}
        self.runtime.ai.configure.return_value = {"configured": True, "provider": "deepseek", "model": "deepseek-flash"}
        body, status = news_response(self.runtime, "/api/news/ai/session", payload=payload, method="POST")
        self.assertEqual(status, 200)
        self.runtime.ai.configure.assert_called_once_with(**payload)
        self.assertEqual(body["ai"]["provider"], "deepseek")
        self.assertNotIn(payload["api_key"], str(body))

    def test_analysis_route_payload_remains_closed_and_model_needs_provider(self):
        base = {"api_key": "synthetic-key", "automatic": False, "acknowledge_budget": True}
        extras = ({"model": "deepseek-flash"}, {"provider": None}, {"provider": ["deepseek"]},
                  {"provider": "deepseek", "model": None},
                  {"provider": "deepseek", "model": "deepseek-flash", "base_url": "https://example.com"},
                  {"provider": "deepseek", "retry": True})
        for extra in extras:
            with self.subTest(extra=extra):
                body, status = news_response(self.runtime, "/api/news/ai/session", payload={**base, **extra}, method="POST")
                self.assertEqual(status, 400)
                self.assertNotIn(base["api_key"], str(body))
        self.runtime.ai.configure.assert_not_called()

    def test_material_quality_is_available_without_ai_and_does_not_mutate_saved_rows(self):
        original = {"id": "news-123", "title": "Board approves a conditional expansion", "excerpt": "", "published_at": None, "discovered_at": "2026-10-19T12:00:00Z"}
        self.runtime.ai = None
        self.runtime.get_item.return_value = original
        self.runtime.list_items.return_value = {"items": [original], "total": 1}
        detail, code = news_response(self.runtime, "/api/news/items/news-123")
        self.assertEqual(code, 200)
        self.assertEqual(detail["item"]["material_quality"]["status"], "title_only")
        self.assertFalse(detail["item"]["material_quality"]["can_analyze"])
        self.assertNotIn("material_quality", original)
        listing, code = news_response(self.runtime, "/api/news/items")
        self.assertEqual(code, 200)
        self.assertFalse(listing["items"][0]["material_quality"]["scope"]["full_text_read"])
        self.assertNotIn("material_quality", original)
        self.runtime.refresh.assert_not_called()

    def test_future_publication_is_not_promoted_to_recent_confirmed_news(self):
        self.runtime.ai = None
        self.runtime.get_item.return_value = {"id": "news-123", "title": "Future effective appointment", "excerpt": "The appointment will take effect on 1 January 2100.", "published_at": "2100-01-01T00:00:00Z", "discovered_at": "2026-10-19T12:00:00Z"}
        body, code = news_response(self.runtime, "/api/news/items/news-123")
        self.assertEqual(code, 200)
        quality = body["item"]["material_quality"]
        self.assertEqual(quality["status"], "clock_anomaly")
        self.assertEqual(quality["freshness"]["publication_age"], "unconfirmed")
        self.assertIsNone(quality["freshness"]["age_seconds"])
        self.assertEqual(body["item"]["published_at"], "2100-01-01T00:00:00Z")

    def test_absent_ai_keeps_reading_available_and_rejects_ai_operations(self) -> None:
        self.runtime.ai = None
        body, status = news_response(self.runtime, "/api/news/overview")
        self.assertEqual(status, 200)
        self.assertNotIn("ai", body["overview"])
        self.assertEqual(news_response(self.runtime, "/api/news/ai")[1], 404)
        self.assertEqual(news_response(self.runtime, "/api/news/ai/session", payload={}, method="POST")[1], 404)


if __name__ == "__main__":
    unittest.main()
