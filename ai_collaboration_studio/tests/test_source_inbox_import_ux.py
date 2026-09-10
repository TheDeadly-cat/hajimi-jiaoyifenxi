from __future__ import annotations

import copy
import json
import unittest

from backend.source_inbox_contracts import (
    SourceInboxContractError,
    accept_source_import,
    canonical_sha256,
)
from backend.source_inbox_import_ux import (
    MANUAL_CHATGPT_SOURCE_CHANNEL,
    SOURCE_IMPORT_PREVIEW_VERSION,
    SOURCE_MONITORING_PROMPT_TEMPLATE_VERSION,
    build_source_monitoring_prompt_template,
)
from backend.source_inbox_service import SourceInboxError, SourceInboxService
from tests.test_source_inbox_contracts import RECEIVED_AT_MS, _packet


class _PoisonStore:
    @property
    def _lock(self):  # pragma: no cover - access itself is the failure
        raise AssertionError("preview must not acquire the store lock")

    def _connect(self):  # pragma: no cover - access itself is the failure
        raise AssertionError("preview must not open SQLite")


class SourceInboxImportPreviewTests(unittest.TestCase):
    def service(self) -> SourceInboxService:
        return SourceInboxService(
            _PoisonStore(),  # type: ignore[arg-type]
            clock=lambda: RECEIVED_AT_MS / 1_000,
        )

    def test_preview_reuses_exact_contract_and_never_opens_store(self) -> None:
        raw = json.dumps(_packet(), ensure_ascii=False)
        preview = self.service().preview_packet(raw)
        packet, receipt = accept_source_import(raw, received_at_ms=RECEIVED_AT_MS)

        self.assertEqual(preview["version"], SOURCE_IMPORT_PREVIEW_VERSION)
        self.assertTrue(preview["valid"])
        self.assertEqual(preview["packet"], packet)
        self.assertEqual(
            preview["candidate"]["normalized_packet_sha256"],
            receipt["normalized_packet_sha256"],
        )
        self.assertEqual(
            preview["candidate"]["source_payload_sha256"],
            receipt["source_payload_sha256"],
        )
        self.assertFalse(preview["store_disposition"]["evaluated"])
        self.assertEqual(
            preview["store_disposition"]["reason"],
            "preview_does_not_open_database",
        )
        self.assertEqual(preview["safety"]["database_reads_performed"], 0)
        self.assertEqual(preview["safety"]["database_writes_performed"], 0)
        self.assertFalse(preview["safety"]["import_performed"])
        self.assertTrue(preview["safety"]["revalidation_required"])
        sealed = dict(preview)
        preview_sha256 = sealed.pop("preview_sha256")
        self.assertEqual(preview_sha256, canonical_sha256(sealed))

    def test_preview_accepts_one_fence_but_preserves_contract_failures(self) -> None:
        raw = json.dumps(_packet(), ensure_ascii=False)
        fenced = self.service().preview_packet(f"```json\n{raw}\n```")
        plain = self.service().preview_packet(raw)
        self.assertEqual(fenced["packet"], plain["packet"])
        self.assertNotEqual(
            fenced["candidate"]["source_payload_sha256"],
            plain["candidate"]["source_payload_sha256"],
        )

        with self.assertRaises(SourceInboxContractError) as captured:
            self.service().preview_packet('{"version":"a","version":"b"}')
        self.assertEqual(captured.exception.code, "SOURCE_IMPORT_DUPLICATE_KEY")

    def test_preview_rejects_reserved_worker_channels_before_store_access(self) -> None:
        for channel in ("official_source_monitor", "futu_anomaly_monitor"):
            packet = _packet()
            packet["source_channel"] = channel
            packet["generation"]["channel"] = channel  # type: ignore[index]
            with self.subTest(channel=channel), self.assertRaises(SourceInboxError) as captured:
                self.service().preview_packet(json.dumps(packet))
            self.assertEqual(
                captured.exception.code,
                "SOURCE_INBOX_MONITORING_CHANNEL_UNAUTHORIZED",
            )
            self.assertEqual(captured.exception.status, 403)


class SourceMonitoringPromptTemplateTests(unittest.TestCase):
    def test_template_is_sealed_manual_only_and_intentionally_not_importable(self) -> None:
        template = build_source_monitoring_prompt_template()
        self.assertEqual(
            template["version"],
            SOURCE_MONITORING_PROMPT_TEMPLATE_VERSION,
        )
        self.assertEqual(
            template["default_source_channel"],
            MANUAL_CHATGPT_SOURCE_CHANNEL,
        )
        self.assertTrue(template["constraints"]["manual_copy_paste_only"])
        self.assertFalse(
            template["constraints"]["unmodified_template_is_importable"]
        )
        self.assertFalse(template["safety"]["chatgpt_page_controlled"])
        self.assertFalse(template["safety"]["chatgpt_automation_performed"])
        self.assertFalse(template["safety"]["external_task_created"])
        self.assertEqual(template["safety"]["execution_capability"], "none")
        self.assertIn("external_unverified", template["prompt"])
        self.assertIn("{{monitoring_scope}}", template["prompt"])
        self.assertIn("content_sha256", template["prompt"])
        sealed = copy.deepcopy(template)
        template_sha256 = sealed.pop("template_sha256")
        self.assertEqual(template_sha256, canonical_sha256(sealed))

        with self.assertRaises(SourceInboxContractError):
            accept_source_import(
                json.dumps(template["result_template"], ensure_ascii=False),
                received_at_ms=RECEIVED_AT_MS,
            )

    def test_template_generation_is_deterministic(self) -> None:
        self.assertEqual(
            build_source_monitoring_prompt_template(),
            build_source_monitoring_prompt_template(),
        )


if __name__ == "__main__":
    unittest.main()
