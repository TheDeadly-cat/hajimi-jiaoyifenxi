"""Selection integrity and actual compact-bundle boundaries, using isolated fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

from backend import http_server
from backend.document_evidence import DocumentEvidenceService, RECHECK_MS
from backend.manual_chatgpt import ManualChatGPTError, ManualChatGPTService, evidence_scope_preview
from backend.material_ingest import FetchedResource
from backend.source_inbox_service import SourceInboxError, SourceInboxService
from backend.store import StudioStore
from tests.test_document_evidence import DocumentEvidenceFixture, import_event
from tests import test_source_inbox_http as http_helpers


class DocumentSelectionFixture(DocumentEvidenceFixture):
    def setUp(self):
        super().setUp()
        self.room_id = "room_market"
        self.run_job(self.request())
        self.version = self.service.view(self.item_id)["versions"][0]
        self.inbox = SourceInboxService(self.store)
        self.inbox.acknowledge(self.item_id, expected_state_version=1, acknowledgement=True)
        self.payload = {"document_version_id": self.version["id"],
                        "paragraph_ids": [self.version["paragraphs"][-1]["id"]],
                        "room_id": self.room_id, "expected_state_version": 2}
        self.fetcher.reset_mock()

    def preview(self, **changes):
        return self.service.preview_selection(self.item_id, **{**self.payload, **changes})

    def save(self, preview=None, **changes):
        preview = preview or self.preview(**changes)
        return self.service.save_selection(self.item_id, confirmation=True,
            preview_sha256=preview["preview_sha256"], **{**self.payload, **changes})

    def count(self, table):
        with closing(self.store._connect()) as db:
            return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

class DocumentSelectionTests(DocumentSelectionFixture):
    def test_selection_saved_and_actual_bundle_match_exactly_without_side_effects(self):
        from backend.providers.openai_provider import OpenAIProvider
        before = self.inbox.get_item(self.item_id)
        with patch.object(OpenAIProvider, "generate", side_effect=AssertionError("model forbidden")) as model:
            preview = self.preview()
            self.assertTrue(preview["can_save"])
            self.assertEqual(self.count("materials"), 0)
            material = self.save(preview)["material"]
            manual = ManualChatGPTService(self.store)
            scope = manual.preview_evidence(self.room_id)
            session = manual.create(self.room_id, objective="Inspect this explicitly selected evidence only.",
                                    expected_evidence_sha256=scope["evidence_sha256"])
            evidence = session["bundle"]["context"]["evidence_index"][0]
            self.assertEqual(evidence["excerpt"], preview["content"])
            self.assertEqual(material["content"], preview["package_excerpt"])
            self.assertEqual(material["source_url"], self.version["final_url"])
            self.assertEqual(evidence["source_url"], material["source_url"])
            self.assertEqual(material["metadata"]["document_selection"], preview["metadata"]["document_selection"])
            self.assertIn(self.payload["paragraph_ids"][0], session["task_prompt"])
            self.assertIn(self.version["body_text_sha256"], session["task_prompt"])
            self.assertEqual(self.inbox.get_item(self.item_id), before)
            for table in ("provider_call_attempts", "provider_execution_runs", "rounds", "source_inbox_attachments", "source_inbox_round_drafts"):
                self.assertEqual(self.count(table), 0)
            model.assert_not_called()
        self.fetcher.assert_not_called()

    def test_duplicate_concurrent_and_reordered_selection_creates_one_material_version(self):
        pair = [p["id"] for p in self.version["paragraphs"][:2]]
        self.assertEqual(self.preview(paragraph_ids=pair), self.preview(paragraph_ids=list(reversed(pair))))
        other = DocumentEvidenceService(StudioStore(self.store.path), clock=lambda: self.now)
        preview = self.preview()
        def save(service):
            return service.save_selection(self.item_id, confirmation=True, preview_sha256=preview["preview_sha256"], **self.payload)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save, [self.service, other]))
        self.assertEqual({r["material"]["id"] for r in results}, {preview["material_id"]})
        self.assertEqual(sum(r["idempotent_replay"] for r in results), 1)
        self.assertTrue(self.save(preview)["idempotent_replay"])
        self.assertEqual(self.count("materials"), 1)
        self.assertEqual(self.count("material_versions"), 1)

    def test_failure_after_material_insert_rolls_back_all_material_and_room_writes(self):
        before_room = self.store.room_snapshot(self.room_id)
        before_event = self.inbox.get_item(self.item_id)
        with patch.object(self.store, "_record_material_version", side_effect=sqlite3.OperationalError("fixture version failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.save()
        self.assertEqual(self.count("materials"), 0)
        self.assertEqual(self.count("material_versions"), 0)
        self.assertEqual(self.store.room_snapshot(self.room_id), before_room)
        self.assertEqual(self.inbox.get_item(self.item_id), before_event)
        self.assertEqual(self.service.view(self.item_id)["versions"][0], self.version)

    def test_unacknowledged_confirmation_stale_preview_and_invalid_room_are_rejected(self):
        for changes in ({"paragraph_ids": []}, {"paragraph_ids": ["missing"]},
                        {"paragraph_ids": self.payload["paragraph_ids"] * 2},
                        {"paragraph_ids": [self.version["id"] + ":p9999"]},
                        {"document_version_id": "missing"}, {"room_id": "missing"},
                        {"expected_state_version": True}, {"expected_state_version": 1}):
            with self.subTest(changes=changes), self.assertRaises(SourceInboxError):
                self.preview(**changes)
        preview = self.preview()
        with self.assertRaises(SourceInboxError):
            self.service.save_selection(self.item_id, confirmation=False, preview_sha256=preview["preview_sha256"], **self.payload)
        with self.assertRaises(SourceInboxError):
            self.service.save_selection(self.item_id, confirmation=True, preview_sha256="0" * 64, **self.payload)
        second = import_event(self.store, 2)
        with self.assertRaises(SourceInboxError):
            self.service.preview_selection(second, **{**self.payload, "expected_state_version": 1})
        self.assertEqual(self.count("materials"), 0)

    def test_unread_item_never_becomes_acknowledged_by_preview_or_save(self):
        second = import_event(self.store, 2)
        job = self.service.request(second, session_id="fixture", confirmation=True)
        self.run_job(job)
        version = self.service.view(second)["versions"][0]
        payload = {**self.payload, "document_version_id": version["id"], "paragraph_ids": [version["paragraphs"][-1]["id"]], "expected_state_version": 1}
        preview = self.service.preview_selection(second, **payload)
        self.assertFalse(preview["can_save"])
        with self.assertRaises(SourceInboxError) as caught:
            self.service.save_selection(second, confirmation=True, preview_sha256=preview["preview_sha256"], **payload)
        self.assertEqual(caught.exception.code, "SOURCE_INBOX_ACKNOWLEDGEMENT_REQUIRED")
        self.assertFalse(self.inbox.get_item(second)["acknowledged"])
        self.assertEqual(self.count("materials"), 0)

    def test_old_selected_version_stays_pinned_when_new_version_arrives(self):
        preview = self.preview()
        self.now += RECHECK_MS + 1
        self.fetcher.side_effect = lambda source, **kw: FetchedResource(b'<html><body><p>Item 1.01 ' + b'New revision only. ' * 20 + b'</p></body></html>', "text/html", source["url"])
        self.run_job(self.request(refresh=True))
        latest = self.service.view(self.item_id)["versions"][-1]
        self.assertNotEqual(latest["id"], self.version["id"])
        material = self.save(preview)["material"]
        self.assertEqual(material["metadata"]["document_selection"]["document_version_id"], self.version["id"])
        self.assertEqual(material["content"], preview["content"])
        with self.assertRaises(SourceInboxError):
            self.preview(paragraph_ids=[latest["paragraphs"][0]["id"]])

    def test_full_selection_over_budget_is_rejected_but_tail_risk_is_preserved(self):
        self.now += RECHECK_MS + 1
        self.fetcher.side_effect = lambda source, **kw: FetchedResource(('<html><body><p>Item 1.01 ' + 'General context. ' * 140 + '</p><p>TAIL RISK: the agreement may be cancelled without compensation.</p></body></html>').encode(), "text/html", source["url"])
        self.run_job(self.request(refresh=True))
        version = self.service.view(self.item_id)["versions"][-1]
        self.payload.update(document_version_id=version["id"], paragraph_ids=[p["id"] for p in version["paragraphs"]])
        preview = self.preview()
        self.assertFalse(preview["fits"])
        self.assertEqual(preview["package_characters"], 0)
        self.assertGreater(preview["packaged_characters"], 1600)
        with self.assertRaises(SourceInboxError):
            self.save(preview)
        self.payload["paragraph_ids"] = [version["paragraphs"][-1]["id"]]
        tail = self.preview()
        self.assertEqual(tail["omitted_paragraphs"], len(version["paragraphs"]) - 1)
        material = self.save(tail)["material"]
        session = ManualChatGPTService(self.store).create(self.room_id, objective="Evaluate the selected tail risk.")
        excerpt = session["bundle"]["context"]["evidence_index"][0]["excerpt"]
        self.assertEqual(excerpt, material["content"])
        self.assertIn("TAIL RISK", excerpt)
        self.assertIn("其余", excerpt)

    def test_empty_unlocated_body_cannot_become_material(self):
        self.now += RECHECK_MS + 1
        self.fetcher.side_effect = lambda source, **kw: FetchedResource(b'<html><head><title>Only a heading</title></head><body></body></html>', "text/html", source["url"])
        self.run_job(self.request(refresh=True))
        version = self.service.view(self.item_id)["versions"][-1]
        with self.assertRaises(SourceInboxError) as caught:
            self.preview(document_version_id=version["id"])
        self.assertEqual(caught.exception.code, "DOCUMENT_BODY_UNAVAILABLE")

    def test_new_freeze_refuses_1601_and_preview_changes_without_rewriting_old_bundle(self):
        material = self.store.add_material(self.room_id, {"title": "Legacy material", "content": "x" * 1600})
        service = ManualChatGPTService(self.store)
        preview = service.preview_evidence(self.room_id)
        old = service.create(self.room_id, objective="Old accepted snapshot", expected_evidence_sha256=preview["evidence_sha256"])
        with closing(self.store._connect()) as db:
            old_record = dict(db.execute("SELECT * FROM manual_chatgpt_sessions WHERE id=?", (old["id"],)).fetchone())
        self.store.update_material(self.room_id, material["id"], {"expected_version": 1, "content": "x" * 1601})
        current = service.preview_evidence(self.room_id)
        self.assertFalse(current["ready"])
        self.assertEqual(current["items"][0]["package_characters"], 0)
        with self.assertRaises(ManualChatGPTError):
            service.create(self.room_id, objective="Must not silently drop the last character")
        self.store.update_material(self.room_id, material["id"], {"expected_version": 2, "content": "changed"})
        with self.assertRaises(ManualChatGPTError) as caught:
            service.create(self.room_id, objective="Stale preview", expected_evidence_sha256=preview["evidence_sha256"])
        self.assertEqual(caught.exception.code, "MANUAL_CHATGPT_EVIDENCE_PREVIEW_STALE")
        with closing(self.store._connect()) as db:
            self.assertEqual(dict(db.execute("SELECT * FROM manual_chatgpt_sessions WHERE id=?", (old["id"],)).fetchone()), old_record)
        self.assertEqual(self.count("manual_chatgpt_sessions"), 1)
        reread = service.get(self.room_id, old["id"])
        self.assertTrue(reread["integrity"]["ok"])
        self.assertEqual(reread["bundle"], old["bundle"])

    def test_material_count_and_inactive_slot_boundaries_do_not_silently_omit(self):
        base = {"id": "test", "active": True, "title": "Test", "content": "safe"}
        self.assertTrue(evidence_scope_preview({"materials": [base] * 40})["ready"])
        self.assertFalse(evidence_scope_preview({"materials": [base] * 41})["ready"])
        self.assertFalse(evidence_scope_preview({"materials": [{**base, "active": False}] * 40 + [base]})["ready"])

    def test_generic_material_api_cannot_forge_or_rewrite_selection_provenance(self):
        preview = self.preview()
        with self.assertRaises(ValueError):
            self.store.add_material(self.room_id, preview)
        material = self.save(preview)["material"]
        with self.assertRaises(ValueError):
            self.store.update_material(self.room_id, material["id"], {"expected_version": 1, "content": "forged"})
        disabled = self.store.update_material(self.room_id, material["id"], {"expected_version": 1, "active": False})
        self.assertFalse(disabled["active"])
        self.assertFalse(self.save(preview)["material"]["active"])


class DocumentSelectionHttpTests(DocumentSelectionFixture):
    request_http = http_helpers.SourceInboxHttpTests.request
    request_with_headers = http_helpers.SourceInboxHttpTests.request_with_headers

    def test_http_requires_local_token_exact_payload_and_confirmation_without_network(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), http_server.StudioRequestHandler)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.server.ai_studio_instance_owner = Mock(spec=http_server.DatabaseInstanceOwner)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True); thread.start()
        try:
            with patch.object(http_server, "STORE", self.store):
                path = f"/api/monitoring/events/{self.item_id}/document/selection"
                self.assertEqual(self.request_http(path + "/preview", method="POST", payload=self.payload, include_token=False)[0], 403)
                self.assertEqual(self.request_http(path + "/preview", method="POST", payload={**self.payload, "content": "forged"})[0], 400)
                owner = self.server.ai_studio_instance_owner
                self.server.ai_studio_instance_owner = None
                self.assertEqual(self.request_http(path + "/preview", method="POST", payload=self.payload)[0], 503)
                self.server.ai_studio_instance_owner = owner
                status, result = self.request_http(path + "/preview", method="POST", payload=self.payload)
                self.assertEqual(status, 200)
                save = {**self.payload, "preview_sha256": result["selection"]["preview_sha256"], "confirmation": True}
                self.assertEqual(self.request_http(path, method="POST", payload={**save, "confirmation": False})[0], 409)
                with patch.object(self.store, "_record_material_version", side_effect=sqlite3.OperationalError("fixture save failure")):
                    status, failure = self.request_http(path, method="POST", payload=save)
                    self.assertEqual(status, 500)
                    self.assertEqual(failure["code"], "DOCUMENT_SELECTION_STORE_FAILED")
                self.assertEqual(self.count("materials"), 0)
                self.assertEqual(self.request_http(path, method="POST", payload=save)[0], 201)
                self.assertEqual(self.request_http(path, method="POST", payload=save)[0], 200)
                self.assertEqual(self.request_http(f"/api/rooms/{self.room_id}/chatgpt-collaborations/evidence-preview")[0], 200)
                self.fetcher.assert_not_called()
                self.assertEqual(self.count("materials"), 1)
                self.assertEqual(self.count("rounds"), 0)
        finally:
            self.server.shutdown(); self.server.server_close(); thread.join(3)
