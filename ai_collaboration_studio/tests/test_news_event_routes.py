"""Closed HTTP contracts for event evidence; fixtures do not open a network."""
import unittest
from unittest.mock import Mock, patch

from backend.news_platform_routes import news_response


class EventRouteTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime.ai = None
        self.runtime.startup = None
        self.item = {"id": "article-1", "title": "Synthetic official announcement", "excerpt": "Synthetic source excerpt"}
        self.runtime.get_item.return_value = self.item
        self.runtime.read_article.return_value = self.item
        self.runtime.event_versions.return_value = [{"id": "version-1"}]
        self.runtime.overview.return_value = {"sources": []}

    def test_history_is_read_only_and_unknown_article_is_404(self):
        body, status = news_response(self.runtime, "/api/news/items/article-1/versions")
        self.assertEqual(status, 200)
        self.assertEqual(body["versions"], [{"id": "version-1"}])
        self.runtime.read_article.assert_not_called()
        self.runtime.get_item.return_value = None
        self.assertEqual(news_response(self.runtime, "/api/news/items/article-1/versions")[1], 404)

    def test_body_fetch_requires_exact_boolean_scope_and_never_accepts_client_url(self):
        path = "/api/news/items/article-1/evidence"
        for payload in ({}, {"include_attachments": 1}, {"include_attachments": False, "url": "http://127.0.0.1/private"}):
            self.assertEqual(news_response(self.runtime, path, method="POST", payload=payload)[1], 400)
        self.runtime.read_article.assert_not_called()
        body, status = news_response(self.runtime, path, method="POST", payload={"include_attachments": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["item"]["id"], "article-1")
        self.runtime.read_article.assert_called_once_with("article-1", include_attachments=True)

    def test_source_control_is_explicit_and_does_not_refresh(self):
        path = "/api/news/sources/sec_mu/control"
        for payload in ({}, {"enabled": "true"}, {"enabled": True, "reset_cooldown": True}):
            self.assertEqual(news_response(self.runtime, path, method="POST", payload=payload)[1], 400)
        self.runtime.configure_source.assert_not_called()
        self.assertEqual(news_response(self.runtime, path, method="POST", payload={"enabled": True})[1], 200)
        self.runtime.configure_source.assert_called_once_with("sec_mu", True)
        self.runtime.refresh.assert_not_called()

    def test_fetch_failure_is_safe_and_does_not_echo_raw_exception(self):
        self.runtime.read_article.side_effect = RuntimeError("private source transport state")
        body, status = news_response(self.runtime, "/api/news/items/article-1/evidence", method="POST", payload={"include_attachments": False})
        self.assertEqual(status, 503)
        self.assertNotIn("private", str(body))

    def test_market_binding_never_requests_model_and_rejects_floating_versions(self):
        self.runtime.ai = Mock()
        self.runtime.ai.prepare_market_context.return_value = {"context_id": "ctx-1"}
        payload = {"room_id": "room-1", "round_id": "round-1", "material_refs": [{"material_id": "material-1", "version": 2}]}
        path = "/api/news/items/article-1/market/context"
        body, status = news_response(self.runtime, path, method="POST", payload=payload)
        self.assertEqual(status, 200)
        self.assertEqual(body["market_context"], {"context_id": "ctx-1"})
        self.runtime.ai.request_market.assert_not_called()
        for version in (True, "latest", 0):
            bad = {**payload, "material_refs": [{"material_id": "material-1", "version": version}]}
            self.assertEqual(news_response(self.runtime, path, method="POST", payload=bad)[1], 400)
        self.runtime.ai.request_market.return_value = {"status": "queued"}
        self.assertEqual(news_response(self.runtime, "/api/news/items/article-1/market/analyze", method="POST", payload={"context_id": "ctx-1"})[1], 202)
        self.runtime.ai.request_market.assert_called_once_with("article-1", "ctx-1")

    def test_market_options_only_exposes_frozen_material_titles_and_versions(self):
        store = self.runtime.research_store = Mock()
        store.room_snapshot.return_value = {"latest_round": {"id": "round-1", "created_at": 1000}}
        store.get_round_checkpoint.return_value = {"protocol_integrity_ok": True, "room_id": "room-1", "round_id": "round-1", "state": {"round_evidence_manifest": {"materials": [{"id": "material-1", "version": 2}]}, "private_context": "MUST_NOT_LEAK"}}
        store.get_material_version.return_value = {"title": "Saved consensus", "content": "MUST_NOT_LEAK"}
        body, status = news_response(self.runtime, "/api/news/market/options", query={"room_id": ["room-1"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["rounds"][0]["materials"], [{"id": "material-1", "version": 2, "title": "Saved consensus"}])
        self.assertNotIn("MUST_NOT_LEAK", str(body))

    def test_automatic_body_reading_requires_exact_boolean_and_does_not_configure_ai(self):
        path = "/api/news/article-reading"
        for payload in ({}, {"enabled": 1}, {"enabled": True, "reset_limits": True}):
            self.assertEqual(news_response(self.runtime, path, method="POST", payload=payload)[1], 400)
        self.runtime.configure_article_reading.assert_not_called()
        self.assertEqual(news_response(self.runtime, path, method="POST", payload={"enabled": True})[1], 200)
        self.runtime.configure_article_reading.assert_called_once_with(True)

    def test_sec_contact_is_explicit_and_not_echoed(self):
        self.runtime.overview.return_value = {"sec_contact_configured": True}
        body, status = news_response(self.runtime, "/api/news/sec-contact", method="POST", payload={"user_agent": "Synthetic App contact@example.test"})
        self.assertEqual(status, 200)
        self.assertTrue(body["overview"]["sec_contact_configured"])
        self.assertNotIn("contact@example.test", str(body))
        self.runtime.configure_sec_contact.assert_called_once_with("Synthetic App contact@example.test")

    def test_event_annotations_require_acknowledgment_and_only_saved_message_identifiers(self):
        with patch("backend.news_event_actions.confirm_relation", return_value=self.item) as relation, patch("backend.news_event_actions.confirm_withdrawal", return_value=self.item) as withdrawal:
            path = "/api/news/items/article-1/event-relation"
            for payload in ({"origin_item_id": "origin-1"}, {"origin_item_id": "origin-1", "acknowledge": 1}, {"origin_item_id": "https://example.test", "acknowledge": True}):
                self.assertEqual(news_response(self.runtime, path, method="POST", payload=payload)[1], 400)
            relation.assert_not_called()
            self.assertEqual(news_response(self.runtime, path, method="POST", payload={"origin_item_id": "origin-1", "acknowledge": True})[1], 200)
            relation.assert_called_once_with(self.runtime, "article-1", "origin-1")
            path = "/api/news/items/article-1/withdrawal"
            self.assertEqual(news_response(self.runtime, path, method="POST", payload={"evidence_item_id": "origin-1", "quote": "The notice was withdrawn.", "acknowledge": True})[1], 200)
            withdrawal.assert_called_once_with(self.runtime, "article-1", "origin-1", "The notice was withdrawn.")


if __name__ == "__main__":
    unittest.main()
