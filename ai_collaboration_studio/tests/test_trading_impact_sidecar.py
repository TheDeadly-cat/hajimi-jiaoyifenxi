from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

import backend.source_inbox_trading_impact as sidecar  # noqa: E402
from backend.source_inbox_contracts import canonical_sha256  # noqa: E402
from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_inbox_trading_impact import (  # noqa: E402
    MAX_TRADING_IMPACT_PROJECTION_BYTES,
    MAX_TRADING_IMPACT_RECEIPT_BYTES,
    SOURCE_INBOX_TRADING_IMPACT_MIGRATION_KEY,
    SourceInboxTradingImpactError,
    ensure_source_inbox_trading_impact_schema,
    insert_or_verify_trading_impact_projection,
    list_verified_trading_impact_projections,
)
from backend.source_monitoring.adapters.company_ir import _release_item  # noqa: E402
from backend.source_monitoring.contracts import (  # noqa: E402
    OFFICIAL_SOURCE_CHANNEL,
    OFFICIAL_SOURCE_CLASS,
)
from backend.source_monitoring.trading_impact_rules import (  # noqa: E402
    TradingImpactProjection,
    TradingImpactRulesV1,
)
from backend.store import StudioStore  # noqa: E402


RECEIVED_AT_MS = 1_800_000_000_000


class _UncheckedProjection:
    """Test-only value used to emulate a future same-version implementation drift."""

    def __init__(self, value: dict[str, object]) -> None:
        self._value = copy.deepcopy(value)

    def to_dict(self) -> dict[str, object]:
        return copy.deepcopy(self._value)


def _raw_ir_item(*, event_type: str, identity_digit: str) -> dict[str, object]:
    identity_sha = identity_digit * 64
    projection_sha = chr(ord(identity_digit) + 1) * 64
    return _release_item(
        symbol="US.MU",
        publisher="Micron Technology",
        feed_url="https://investors.micron.com/rss/news-releases.xml",
        release={
            "title": f"Micron fixture {event_type}",
            "summary": "A deterministic local company IR fixture.",
            "event_type": event_type,
            "fiscal_period": "Q4 FY2026" if event_type != "other" else "",
        },
        official_url=(
            "https://investors.micron.com/news-releases/"
            f"news-release-details/{event_type}-{identity_digit}"
        ),
        published_at="2026-08-28T12:00:00Z",
        guid=f"fixture-{event_type}-{identity_digit}",
        identity_kind="guid",
        identity_value=f"fixture-{event_type}-{identity_digit}",
        identity_sha=identity_sha,
        projection_sha=projection_sha,
        previous_projection_sha="",
    )


def _packet(
    *,
    external_run_id: str,
    event_type: str,
    identity_digit: str,
) -> dict[str, object]:
    return {
        "version": "source_import_packet_v1",
        "source_channel": OFFICIAL_SOURCE_CHANNEL,
        "source_key": "company_ir",
        "external_run_id": external_run_id,
        "checked_at": "2026-08-28T13:00:00Z",
        "cutoff_at": "2026-08-28T13:00:00Z",
        "meaningful_change": True,
        "items": [
            _raw_ir_item(
                event_type=event_type,
                identity_digit=identity_digit,
            )
        ],
        "generation": {
            "channel": OFFICIAL_SOURCE_CHANNEL,
            "model": "",
            "cost": {
                "status": "unavailable",
                "amount": None,
                "currency": "",
                "usage_source": "not_applicable",
            },
            "correlated_output": False,
        },
    }


class TradingImpactSidecarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-trading-impact-sidecar-"
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.database_path)
        self.service = SourceInboxService(
            self.store,
            clock=lambda: RECEIVED_AT_MS / 1_000,
        )
        self.rules = TradingImpactRulesV1()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _import(
        self,
        *,
        external_run_id: str,
        event_type: str = "earnings_release",
        identity_digit: str = "1",
    ) -> tuple[dict[str, object], dict[str, object]]:
        result = self.service.import_packet(
            json.dumps(
                _packet(
                    external_run_id=external_run_id,
                    event_type=event_type,
                    identity_digit=identity_digit,
                ),
                ensure_ascii=False,
            ),
            actor="source_monitoring_worker",
        )
        record = result["items"][0]
        return result, record

    def _projection(self, record: dict[str, object]):
        return self.rules.project_item(
            record["item"],
            item_sha256=record["item_sha256"],
            adapter_id="company_ir",
            source_class=OFFICIAL_SOURCE_CLASS,
            source_channel=OFFICIAL_SOURCE_CHANNEL,
        )

    def _persist(
        self,
        import_result: dict[str, object],
        item_record: dict[str, object],
        *,
        created_at_ms: int = RECEIVED_AT_MS,
        projection=None,
    ) -> dict[str, object]:
        candidate = projection or self._projection(item_record)
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            return insert_or_verify_trading_impact_projection(
                connection,
                evaluation_import_id=import_result["import_id"],
                item_id=item_record["id"],
                source_item=item_record["item"],
                source_item_sha256=item_record["item_sha256"],
                projection=candidate,
                created_at_ms=created_at_ms,
            )

    def _read(self, item_record: dict[str, object]) -> list[dict[str, object]]:
        with closing(self.store._connect()) as connection:
            connection.execute("BEGIN")
            return list_verified_trading_impact_projections(
                connection,
                item_id=item_record["id"],
                source_item=item_record["item"],
                source_item_sha256=item_record["item_sha256"],
            )

    def test_schema_marker_indexes_zero_checks_and_immutable_triggers(self) -> None:
        first, item = self._import(external_run_id="impact-schema-run")
        created = self._persist(first, item)
        self.assertEqual(created["disposition"], "CREATED")

        with closing(self.store._connect()) as connection:
            marker_before = connection.execute(
                "SELECT applied_at FROM schema_migrations WHERE key=?",
                (SOURCE_INBOX_TRADING_IMPACT_MIGRATION_KEY,),
            ).fetchone()
            self.assertIsNotNone(marker_before)
            with connection:
                ensure_source_inbox_trading_impact_schema(
                    connection,
                    applied_at_ms=1,
                )
            marker_after = connection.execute(
                "SELECT applied_at FROM schema_migrations WHERE key=?",
                (SOURCE_INBOX_TRADING_IMPACT_MIGRATION_KEY,),
            ).fetchone()
            self.assertEqual(marker_after, marker_before)

            object_rows = connection.execute(
                """SELECT type,name,sql FROM sqlite_master
                    WHERE tbl_name='source_inbox_trading_impact_projections'
                       OR name LIKE '%source_inbox_trading_impact%'
                    ORDER BY type,name"""
            ).fetchall()
            names = {str(row[1]) for row in object_rows}
            self.assertTrue({
                "uq_source_inbox_trading_impact_item_ruleset",
                "uq_source_inbox_trading_impact_projection_key",
                "uq_source_inbox_trading_impact_receipt_sha256",
                "idx_source_inbox_trading_impact_import",
                "idx_source_inbox_trading_impact_ruleset_status",
                "trg_source_inbox_trading_impact_no_update",
                "trg_source_inbox_trading_impact_no_delete",
            }.issubset(names))
            table_sql = next(
                str(row[2]) for row in object_rows
                if row[0] == "table"
                and row[1] == "source_inbox_trading_impact_projections"
            ).replace(" ", "").replace("\n", "")
            for zero_check in (
                "CHECK(provider_calls_performed=0)",
                "CHECK(model_calls_performed=0)",
                "CHECK(market_calls_performed=0)",
                "CHECK(network_requests_performed=0)",
                "CHECK(database_writes_performed=0)",
                "CHECK(formal_rounds_created=0)",
                "CHECK(live_trading_allowed=0)",
                "CHECK(execution_capability='none')",
            ):
                self.assertIn(zero_check, table_sql)
            foreign_keys = {
                (str(row[2]), str(row[3]), str(row[4]), str(row[6]))
                for row in connection.execute(
                    "PRAGMA foreign_key_list(source_inbox_trading_impact_projections)"
                ).fetchall()
            }
            self.assertIn(
                ("source_inbox_imports", "evaluation_import_id", "id", "RESTRICT"),
                foreign_keys,
            )
            self.assertIn(
                ("source_inbox_items", "item_id", "id", "RESTRICT"),
                foreign_keys,
            )

            projection_id = created["record"]["id"]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """UPDATE source_inbox_trading_impact_projections
                          SET created_at_ms=created_at_ms+1 WHERE id=?""",
                    (projection_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM source_inbox_trading_impact_projections WHERE id=?",
                    (projection_id,),
                )
            connection.rollback()

    def test_helper_obeys_caller_transaction_and_rollback_is_atomic(self) -> None:
        imported, item = self._import(external_run_id="impact-atomic-run")
        projection = self._projection(item)
        with self.store._lock, closing(self.store._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            created = insert_or_verify_trading_impact_projection(
                connection,
                evaluation_import_id=imported["import_id"],
                item_id=item["id"],
                source_item=item["item"],
                source_item_sha256=item["item_sha256"],
                projection=projection,
                created_at_ms=RECEIVED_AT_MS,
            )
            self.assertEqual(created["disposition"], "CREATED")
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM source_inbox_trading_impact_projections"
                ).fetchone()[0],
                1,
            )
            connection.rollback()
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM source_inbox_trading_impact_projections"
                ).fetchone()[0],
                0,
            )

    def test_create_new_run_reuse_and_same_version_drift_conflict(self) -> None:
        first, item = self._import(external_run_id="impact-first-run")
        candidate = self._projection(item)
        created = self._persist(first, item, projection=candidate)
        self.assertEqual(created["disposition"], "CREATED")
        first_record = created["record"]

        second, duplicate_item = self._import(external_run_id="impact-second-run")
        self.assertEqual(second["duplicate_item_count"], 1)
        reused = self._persist(
            second,
            duplicate_item,
            created_at_ms=RECEIVED_AT_MS + 60_000,
            projection=self._projection(duplicate_item),
        )
        self.assertEqual(reused["disposition"], "REUSED")
        self.assertEqual(reused["record"], first_record)
        self.assertEqual(
            reused["record"]["evaluation_import_id"],
            first["import_id"],
        )
        self.assertEqual(reused["record"]["created_at_ms"], RECEIVED_AT_MS)

        drift = candidate.to_dict()
        drift["hypotheses"][0]["impact_hypothesis"]["statement"] += " Drift."
        drift_basis = {
            key: value for key, value in drift.items()
            if key != "projection_sha256"
        }
        drift["projection_sha256"] = canonical_sha256(drift_basis)
        with mock.patch.object(
            sidecar.TradingImpactProjection,
            "build",
            side_effect=lambda value: _UncheckedProjection(value),
        ):
            with self.assertRaises(SourceInboxTradingImpactError) as captured:
                self._persist(
                    second,
                    duplicate_item,
                    projection=drift,
                )
        self.assertEqual(
            captured.exception.code,
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_CONFLICT",
        )
        self.assertEqual(len(self._read(item)), 1)

    def test_parent_semantic_binding_rejects_coherent_hash_valid_forgery(self) -> None:
        imported, item = self._import(external_run_id="impact-semantic-parent")
        forged = self._projection(item).to_dict()
        forged["source_item_binding"]["source_semantic_binding"]["symbol"] = (
            "US.WDC"
        )
        hypothesis = forged["hypotheses"][0]
        hypothesis["impact_hypothesis"]["affected_area"] = "security:US.WDC"
        hypothesis["impact_hypothesis"]["statement"] = (
            "The admitted company IR RSS projection classifies earnings_release for "
            "US.WDC; issuer assumptions may require review, but company self-reporting "
            "alone does not establish market direction or magnitude."
        )
        hypothesis["affected_area_binding"] = {
            "kind": "security",
            "id": "US.WDC",
            "security_ids": ["US.WDC"],
        }
        hypothesis["hypothesis_sha256"] = canonical_sha256({
            key: value
            for key, value in hypothesis.items()
            if key != "hypothesis_sha256"
        })
        forged["projection_sha256"] = canonical_sha256({
            key: value
            for key, value in forged.items()
            if key != "projection_sha256"
        })
        self.assertEqual(
            TradingImpactProjection.build(forged).to_dict(),
            forged,
        )
        with self.assertRaises(SourceInboxTradingImpactError) as captured:
            self._persist(
                imported,
                item,
                projection=TradingImpactProjection.build(forged),
            )
        self.assertEqual(
            captured.exception.code,
            "SOURCE_INBOX_TRADING_IMPACT_ITEM_CONFLICT",
        )
        self.assertEqual(len(self._read(item)), 0)

    def test_no_match_is_persisted_and_receipt_is_bounded_projection_binding(self) -> None:
        imported, item = self._import(
            external_run_id="impact-no-match-run",
            event_type="other",
            identity_digit="3",
        )
        projection = self._projection(item)
        self.assertEqual(projection.to_dict()["evaluation"], "no_match")
        created = self._persist(imported, item, projection=projection)
        record = created["record"]
        self.assertEqual(record["status"], "NO_MATCH")
        self.assertEqual(record["matched_rule_count"], 0)
        self.assertEqual(record["hypothesis_count"], 0)
        self.assertNotIn("projection", record["receipt"])
        binding = record["receipt"]["projection_binding"]
        self.assertEqual(binding["evaluation"], "no_match")
        self.assertEqual(binding["hypothesis_sha256s"], [])
        self.assertEqual(binding["used_source_indexes"], [])
        self.assertLessEqual(
            len(json.dumps(record["projection"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")),
            MAX_TRADING_IMPACT_PROJECTION_BYTES,
        )
        self.assertLessEqual(
            len(json.dumps(record["receipt"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")),
            MAX_TRADING_IMPACT_RECEIPT_BYTES,
        )

    def test_readback_detects_receipt_and_evaluation_source_tamper(self) -> None:
        imported, item = self._import(external_run_id="impact-tamper-run")
        created = self._persist(imported, item)
        with mock.patch.object(
            TradingImpactRulesV1,
            "project_item",
            side_effect=AssertionError("readback must not execute current rules"),
        ):
            self.assertEqual(len(self._read(item)), 1)

        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute(
                "DROP TRIGGER trg_source_inbox_trading_impact_no_update"
            )
            connection.execute(
                """UPDATE source_inbox_trading_impact_projections
                      SET receipt_json='{}' WHERE id=?""",
                (created["record"]["id"],),
            )
        with self.assertRaises(SourceInboxTradingImpactError) as receipt_error:
            self._read(item)
        self.assertEqual(
            receipt_error.exception.code,
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
        )

    def test_readback_validates_original_evaluation_import_source_binding(self) -> None:
        imported, item = self._import(external_run_id="impact-source-bind-run")
        self._persist(imported, item)
        with self.store._lock, closing(self.store._connect()) as connection, connection:
            connection.execute(
                "UPDATE source_inbox_imports SET source_key='company_ir_drift' WHERE id=?",
                (imported["import_id"],),
            )
        with self.assertRaises(SourceInboxTradingImpactError) as captured:
            self._read(item)
        self.assertEqual(
            captured.exception.code,
            "SOURCE_INBOX_TRADING_IMPACT_IMPORT_BINDING_INVALID",
        )


if __name__ == "__main__":
    unittest.main()
