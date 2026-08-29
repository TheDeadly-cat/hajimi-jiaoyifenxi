from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime, timezone

from backend.source_inbox_contracts import (
    EXTERNAL_UNVERIFIED,
    MAX_SOURCE_IMPORT_BYTES,
    MAX_SOURCE_IMPORT_DEPTH,
    MAX_SOURCE_ITEMS,
    MAX_SOURCES_PER_ITEM,
    PROJECT_SOURCE_ITEM_VERSION,
    SOURCE_IMPORT_PACKET_VERSION,
    SOURCE_IMPORT_RECEIPT_VERSION,
    SOURCE_IMPORT_STATUSES,
    SOURCE_STATUS_AWAITING_USER,
    SOURCE_STATUS_DUPLICATE,
    SourceInboxContractError,
    accept_source_import,
    build_source_import_receipt,
    canonicalize_source_url,
    normalize_source_import_packet,
    parse_source_import_json,
    project_source_item_fingerprint,
)


RECEIVED_AT_MS = int(
    datetime(2026, 8, 28, 13, 5, tzinfo=timezone.utc).timestamp() * 1_000
)


def _source(index: int = 0) -> dict[str, object]:
    return {
        "url": f"https://github.com/TheDeadly-cat/hajimi-jiaoyifenxi/actions/runs/{100 + index}",
        "publisher": "GitHub",
        "source_type": "official_platform",
        "published_at": "2026-08-28T12:56:00Z",
        "content_sha256": f"{index + 1:064x}",
    }


def _item(index: int = 0) -> dict[str, object]:
    return {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": f"github-run-{100 + index}",
        "item_type": "ci_run_failure",
        "severity": "high",
        "occurred_at": "2026-08-28T12:55:00Z",
        "published_at": "2026-08-28T12:56:00Z",
        "entities": [
            {
                "kind": "repository",
                "id": "TheDeadly-cat/hajimi-jiaoyifenxi",
                "label": "hajimi-jiaoyifenxi",
            },
            {
                "kind": "workflow",
                "id": "isolated-validation",
                "label": "Isolated validation",
            },
        ],
        "headline": f"Isolated validation failed in run {100 + index}",
        "summary": "The workflow reported a failing unit-test step.",
        "facts": [
            {
                "claim": "The workflow conclusion is failure.",
                "source_indexes": [0],
            }
        ],
        "sources": [_source(index)],
        "impact_hypotheses": [
            {
                "statement": "The current revision may not satisfy its isolated checks.",
                "affected_area": "release readiness",
                "time_horizon": "before next publication",
                "source_indexes": [0],
                "confidence": 0.72,
            }
        ],
        "unknowns": ["The exact failing assertion has not been imported."],
        "confidence": 0.93,
        "recommended_route": "open_round_draft",
        "extensions": {
            "github_v1": {
                "repository": "TheDeadly-cat/hajimi-jiaoyifenxi",
                "workflow": "isolated-validation",
                "run_status": "failure",
            }
        },
    }


def _packet() -> dict[str, object]:
    return {
        "version": SOURCE_IMPORT_PACKET_VERSION,
        "source_channel": "chatgpt_scheduled_task",
        "source_key": "github_ci_watch",
        "external_run_id": "2026-08-28T13:00:00Z-github-ci-watch",
        "checked_at": "2026-08-28T13:03:00Z",
        "cutoff_at": "2026-08-28T13:00:00Z",
        "meaningful_change": True,
        "items": [_item()],
        "generation": {
            "channel": "chatgpt_scheduled_task",
            "model": "",
            "cost": {
                "status": "unavailable",
                "amount": None,
                "currency": "",
                "usage_source": "subscription_unavailable",
            },
            "correlated_output": True,
        },
    }


class SourceInboxParsingTests(unittest.TestCase):
    def assert_contract_code(self, expected: str, callback) -> None:
        with self.assertRaises(SourceInboxContractError) as captured:
            callback()
        self.assertEqual(captured.exception.code, expected)
        self.assertTrue(captured.exception.issues)

    def test_accepts_exact_object_and_one_json_fence(self) -> None:
        raw = json.dumps(_packet(), ensure_ascii=False)
        parsed = parse_source_import_json(raw)
        fenced = parse_source_import_json(f"```json\n{raw}\n```")
        self.assertEqual(parsed, fenced)

    def test_rejects_duplicate_keys_nonfinite_numbers_and_extra_json(self) -> None:
        cases = [
            ('{"version":"a","version":"b"}', "SOURCE_IMPORT_DUPLICATE_KEY"),
            ('{"value":NaN}', "SOURCE_IMPORT_NONFINITE_NUMBER"),
            ('{} {}', "SOURCE_IMPORT_JSON_INVALID"),
        ]
        for raw, code in cases:
            with self.subTest(code=code):
                self.assert_contract_code(code, lambda raw=raw: parse_source_import_json(raw))

    def test_rejects_invalid_utf8_and_utf8_byte_overflow(self) -> None:
        self.assert_contract_code(
            "SOURCE_IMPORT_UTF8_INVALID",
            lambda: parse_source_import_json(b"\xff"),
        )
        oversized = "猫" * (MAX_SOURCE_IMPORT_BYTES // 3 + 1)
        self.assert_contract_code(
            "SOURCE_IMPORT_TOO_LARGE",
            lambda: parse_source_import_json(oversized),
        )

    def test_rejects_explicit_depth_overflow(self) -> None:
        value: object = "leaf"
        for _ in range(MAX_SOURCE_IMPORT_DEPTH + 2):
            value = {"nested": value}
        self.assert_contract_code(
            "SOURCE_IMPORT_DEPTH_INVALID",
            lambda: parse_source_import_json(json.dumps(value)),
        )


class SourceURLContractTests(unittest.TestCase):
    def assert_url_code(self, raw: str, code: str) -> None:
        with self.assertRaises(SourceInboxContractError) as captured:
            canonicalize_source_url(raw)
        self.assertEqual(captured.exception.code, code)

    def test_canonicalizes_public_url_without_network(self) -> None:
        self.assertEqual(
            canonicalize_source_url(
                "HTTPS://GitHub.com:443/TheDeadly-cat/hajimi-jiaoyifenxi?q=猫#logs"
            ),
            "https://github.com/TheDeadly-cat/hajimi-jiaoyifenxi?q=%E7%8C%AB",
        )

    def test_rejects_userinfo_non_http_private_hosts_and_abnormal_ports(self) -> None:
        cases = [
            ("https://user:secret@github.com/a", "SOURCE_URL_USERINFO_FORBIDDEN"),
            ("ftp://github.com/a", "SOURCE_URL_SCHEME_FORBIDDEN"),
            ("http://127.0.0.1/a", "SOURCE_URL_PRIVATE_HOST_FORBIDDEN"),
            ("http://10.0.0.7/a", "SOURCE_URL_PRIVATE_HOST_FORBIDDEN"),
            ("http://169.254.1.1/a", "SOURCE_URL_PRIVATE_HOST_FORBIDDEN"),
            ("http://[::1]/a", "SOURCE_URL_PRIVATE_HOST_FORBIDDEN"),
            ("http://service.internal/a", "SOURCE_URL_PRIVATE_HOST_FORBIDDEN"),
            ("http://2130706433/a", "SOURCE_URL_HOST_INVALID"),
            ("https://github.com:8443/a", "SOURCE_URL_PORT_FORBIDDEN"),
        ]
        for raw, code in cases:
            with self.subTest(raw=raw):
                self.assert_url_code(raw, code)

    def test_rejects_sensitive_query_keys_but_preserves_benign_query(self) -> None:
        sensitive_urls = [
            "https://example.com/report?access_token=synthetic",
            "https://example.com/report?api_key=synthetic",
            "https://example.com/report?X-Amz-Credential=synthetic",
            "https://example.com/report?%61ccess_token=synthetic",
            "https://example.com/report?%2561ccess_token=synthetic",
            "https://example.com/report?%25252574%2525256f%2525256b%25252565%2525256e=synthetic",
            "https://example.com/report?api_t%D0%BEken=synthetic",
            "https://example.com/report?safe=1;access_token=synthetic",
        ]
        for raw in sensitive_urls:
            with self.subTest(raw=raw):
                self.assert_url_code(raw, "SOURCE_URL_SENSITIVE_QUERY_FORBIDDEN")
        self.assertEqual(
            canonicalize_source_url("https://example.com/report?page=1&monkey=capuchin"),
            "https://example.com/report?page=1&monkey=capuchin",
        )


class SourceInboxNormalizationTests(unittest.TestCase):
    def assert_contract_code(self, expected: str, packet: dict[str, object]) -> None:
        with self.assertRaises(SourceInboxContractError) as captured:
            normalize_source_import_packet(packet, received_at_ms=RECEIVED_AT_MS)
        self.assertEqual(captured.exception.code, expected)

    def test_normalizes_domain_neutral_item_and_marks_external_claims(self) -> None:
        packet = _packet()
        packet["items"][0]["sources"][0]["url"] = (  # type: ignore[index]
            "HTTPS://GitHub.com:443/TheDeadly-cat/hajimi-jiaoyifenxi/actions/runs/100#log"
        )
        normalized = normalize_source_import_packet(
            packet,
            received_at_ms=RECEIVED_AT_MS,
        )
        item = normalized["items"][0]
        self.assertEqual(item["version"], PROJECT_SOURCE_ITEM_VERSION)
        self.assertRegex(item["server_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertEqual(item["external_claims_verification"], EXTERNAL_UNVERIFIED)
        self.assertIn("impact_hypotheses", item)
        self.assertIn("extensions", item)
        self.assertNotIn("bullish_implications", item)
        self.assertEqual(
            normalized["external_claims_verification"]["model"],
            EXTERNAL_UNVERIFIED,
        )
        self.assertEqual(
            normalized["external_claims_verification"]["cost"],
            EXTERNAL_UNVERIFIED,
        )
        self.assertEqual(normalized["safety"]["network_requests_performed"], 0)
        self.assertEqual(normalized["safety"]["execution_capability"], "none")

    def test_rejects_unknown_fields_at_core_levels(self) -> None:
        cases: list[dict[str, object]] = []
        root = _packet()
        root["fingerprint"] = "caller-owned"
        cases.append(root)
        item = _packet()
        item["items"][0]["bullish_implications"] = []  # type: ignore[index]
        cases.append(item)
        source = _packet()
        source["items"][0]["sources"][0]["trust_me"] = True  # type: ignore[index]
        cases.append(source)
        for packet in cases:
            with self.subTest(keys=set(packet)):
                self.assert_contract_code("SOURCE_IMPORT_FIELD_UNKNOWN", packet)

    def test_rejects_execution_fields_even_inside_extensions(self) -> None:
        for field in ("order_id", "tool_choice", "walletAddress", "shell_command"):
            packet = _packet()
            packet["items"][0]["extensions"]["github_v1"][field] = "forbidden"  # type: ignore[index]
            with self.subTest(field=field):
                self.assert_contract_code(
                    "SOURCE_IMPORT_EXECUTION_FIELD_FORBIDDEN",
                    packet,
                )

    def test_rejects_sensitive_fields_inside_extensions(self) -> None:
        for field in (
            "api_token",
            "api_tоken",
            "client_secret",
            "authorization",
            "session_cookie",
            "private_key",
            "x_amz_signature",
        ):
            packet = _packet()
            packet["items"][0]["extensions"]["github_v1"]["nested"] = {  # type: ignore[index]
                field: "synthetic-secret"
            }
            with self.subTest(field=field):
                self.assert_contract_code(
                    "SOURCE_IMPORT_SENSITIVE_FIELD_FORBIDDEN",
                    packet,
                )

        benign = _packet()
        benign["items"][0]["extensions"]["github_v1"]["monkey"] = "capuchin"  # type: ignore[index]
        benign["items"][0]["extensions"]["github_v1"]["status_code"] = 200  # type: ignore[index]
        normalized = normalize_source_import_packet(benign, received_at_ms=RECEIVED_AT_MS)
        self.assertEqual(
            normalized["items"][0]["extensions"]["github_v1"]["monkey"],
            "capuchin",
        )
        self.assertEqual(
            normalized["items"][0]["extensions"]["github_v1"]["status_code"],
            200,
        )

    def test_rejects_item_and_source_limits(self) -> None:
        too_many_items = _packet()
        too_many_items["items"] = [_item(index) for index in range(MAX_SOURCE_ITEMS + 1)]
        self.assert_contract_code("SOURCE_IMPORT_ITEM_LIMIT", too_many_items)

        too_many_sources = _packet()
        too_many_sources["items"][0]["sources"] = [  # type: ignore[index]
            _source(index) for index in range(MAX_SOURCES_PER_ITEM + 1)
        ]
        self.assert_contract_code("SOURCE_IMPORT_SOURCE_LIMIT", too_many_sources)

    def test_rejects_bad_source_reference_duplicate_source_and_duplicate_item(self) -> None:
        bad_reference = _packet()
        bad_reference["items"][0]["facts"][0]["source_indexes"] = [1]  # type: ignore[index]
        self.assert_contract_code("SOURCE_IMPORT_SOURCE_REFERENCE_INVALID", bad_reference)

        duplicate_source = _packet()
        duplicate_source["items"][0]["sources"] = [_source(), _source()]  # type: ignore[index]
        self.assert_contract_code("SOURCE_IMPORT_SOURCE_DUPLICATE", duplicate_source)

        duplicate_item = _packet()
        duplicate_item["items"] = [_item(), _item()]
        self.assert_contract_code("SOURCE_IMPORT_ITEM_DUPLICATE", duplicate_item)

    def test_rejects_time_conflicts_meaning_conflicts_and_bool_confidence(self) -> None:
        future = _packet()
        future["cutoff_at"] = "2026-08-28T13:06:00Z"
        future["checked_at"] = "2026-08-28T13:06:00Z"
        self.assert_contract_code("SOURCE_IMPORT_TIME_FUTURE", future)

        no_change_with_items = _packet()
        no_change_with_items["meaningful_change"] = False
        self.assert_contract_code("SOURCE_IMPORT_MEANING_CONFLICT", no_change_with_items)

        boolean_confidence = _packet()
        boolean_confidence["items"][0]["confidence"] = True  # type: ignore[index]
        self.assert_contract_code("SOURCE_IMPORT_NUMBER_INVALID", boolean_confidence)

        channel_mismatch = _packet()
        channel_mismatch["generation"]["channel"] = "github_app"  # type: ignore[index]
        self.assert_contract_code(
            "SOURCE_IMPORT_CHANNEL_BINDING_INVALID",
            channel_mismatch,
        )

    def test_rejects_invalid_declared_cost(self) -> None:
        packet = _packet()
        packet["generation"]["cost"] = {  # type: ignore[index]
            "status": "declared",
            "amount": 0.01,
            "currency": "USD",
            "usage_source": "external_report",
        }
        self.assert_contract_code("SOURCE_IMPORT_COST_INVALID", packet)

    def test_fingerprint_ignores_external_id_and_summary_but_not_source_identity(self) -> None:
        normalized = normalize_source_import_packet(_packet(), received_at_ms=RECEIVED_AT_MS)
        first = normalized["items"][0]
        variant = copy.deepcopy(first)
        variant["external_item_id"] = "different-external-id"
        variant["summary"] = "Different external wording."
        self.assertEqual(
            project_source_item_fingerprint(first),
            project_source_item_fingerprint(variant),
        )
        variant["sources"][0]["url"] = "https://github.com/example/other/actions/runs/100"
        self.assertNotEqual(
            project_source_item_fingerprint(first),
            project_source_item_fingerprint(variant),
        )


class SourceImportReceiptTests(unittest.TestCase):
    def test_accept_builds_deterministic_receipt_and_import_key(self) -> None:
        raw = json.dumps(_packet(), ensure_ascii=False, sort_keys=True)
        packet, receipt = accept_source_import(raw, received_at_ms=RECEIVED_AT_MS)
        second_packet, second_receipt = accept_source_import(
            raw,
            received_at_ms=RECEIVED_AT_MS,
        )
        self.assertEqual(packet, second_packet)
        self.assertEqual(receipt, second_receipt)
        self.assertEqual(receipt["version"], SOURCE_IMPORT_RECEIPT_VERSION)
        self.assertEqual(receipt["status"], SOURCE_STATUS_AWAITING_USER)
        self.assertEqual(receipt["item_count"], 1)
        self.assertEqual(receipt["source_count"], 1)
        self.assertEqual(receipt["external_claims_verification"], EXTERNAL_UNVERIFIED)
        self.assertRegex(receipt["receipt_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(receipt["safety"]["database_writes_performed"], 0)

    def test_receipt_supports_all_declared_states_but_rejects_unknown_state(self) -> None:
        raw = json.dumps(_packet())
        packet, receipt = accept_source_import(raw, received_at_ms=RECEIVED_AT_MS)
        for status in SOURCE_IMPORT_STATUSES:
            with self.subTest(status=status):
                rebuilt = build_source_import_receipt(
                    packet,
                    received_at_ms=RECEIVED_AT_MS,
                    source_payload_bytes=receipt["source_payload_bytes"],
                    source_payload_sha256=receipt["source_payload_sha256"],
                    status=status,
                )
                self.assertEqual(rebuilt["status"], status)
        with self.assertRaises(SourceInboxContractError) as captured:
            build_source_import_receipt(
                packet,
                received_at_ms=RECEIVED_AT_MS,
                source_payload_bytes=receipt["source_payload_bytes"],
                source_payload_sha256=receipt["source_payload_sha256"],
                status="READY",
            )
        self.assertEqual(captured.exception.code, "SOURCE_IMPORT_STATUS_INVALID")

    def test_duplicate_status_is_available_for_store_level_replay_detection(self) -> None:
        raw = json.dumps(_packet())
        packet, receipt = accept_source_import(raw, received_at_ms=RECEIVED_AT_MS)
        duplicate = build_source_import_receipt(
            packet,
            received_at_ms=RECEIVED_AT_MS,
            source_payload_bytes=receipt["source_payload_bytes"],
            source_payload_sha256=receipt["source_payload_sha256"],
            status=SOURCE_STATUS_DUPLICATE,
        )
        self.assertEqual(duplicate["status"], SOURCE_STATUS_DUPLICATE)
        self.assertEqual(duplicate["import_key_sha256"], receipt["import_key_sha256"])

    def test_receipt_rejects_tampered_server_fingerprint(self) -> None:
        raw = json.dumps(_packet())
        packet, receipt = accept_source_import(raw, received_at_ms=RECEIVED_AT_MS)
        packet["items"][0]["server_fingerprint"] = "f" * 64
        with self.assertRaises(SourceInboxContractError) as captured:
            build_source_import_receipt(
                packet,
                received_at_ms=RECEIVED_AT_MS,
                source_payload_bytes=receipt["source_payload_bytes"],
                source_payload_sha256=receipt["source_payload_sha256"],
            )
        self.assertEqual(
            captured.exception.code,
            "SOURCE_IMPORT_RECEIPT_PACKET_INVALID",
        )


if __name__ == "__main__":
    unittest.main()
