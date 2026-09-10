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

from backend.source_inbox_contracts import SOURCE_IMPORT_PACKET_VERSION  # noqa: E402
from backend.source_inbox_service import SourceInboxError, SourceInboxService  # noqa: E402
from backend.source_inbox_trading_impact import (  # noqa: E402
    SourceInboxTradingImpactError,
)
from backend.source_monitoring.adapters.sec_filings import _filing_item  # noqa: E402
from backend.source_monitoring.trading_impact_rules import (  # noqa: E402
    TradingImpactRulesV1,
)
from backend.store import StudioStore  # noqa: E402


RECEIVED_AT_MS = 1_788_149_100_000


def _sec_item() -> dict[str, object]:
    return _filing_item(
        symbol="US.MU",
        cik="0000723125",
        company_name="Micron Technology, Inc.",
        filing={
            "accession_number": "0000723125-26-000001",
            "accepted_at": "2026-08-31T04:00:00Z",
            "description": "Current report metadata fixture.",
            "filing_date": "2026-08-31",
            "form": "8-K",
            "items": "2.02,9.01",
            "primary_document": "mu-20260831.htm",
        },
        event_time="2026-08-31T04:00:00Z",
        official_url=(
            "https://www.sec.gov/Archives/edgar/data/723125/"
            "000072312526000001/mu-20260831.htm"
        ),
    )


def _packet(run_id: str = "phase5-sec-run-1") -> dict[str, object]:
    return {
        "version": SOURCE_IMPORT_PACKET_VERSION,
        "source_channel": "official_source_monitor",
        "source_key": "sec_filings",
        "external_run_id": run_id,
        "checked_at": "2026-08-31T04:04:00Z",
        "cutoff_at": "2026-08-31T04:03:00Z",
        "meaningful_change": True,
        "items": [_sec_item()],
        "generation": {
            "channel": "official_source_monitor",
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


class SourceInboxTradingImpactIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-source-inbox-impact-integration-"
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

    @staticmethod
    def _raw(packet: dict[str, object]) -> str:
        return json.dumps(packet, ensure_ascii=False)

    def _import(
        self,
        packet: dict[str, object],
        *,
        rules: TradingImpactRulesV1 | None = None,
        actor: str = "source_monitoring_worker",
    ) -> dict[str, object]:
        return self.service.import_packet(
            self._raw(packet),
            actor=actor,
            impact_rules=rules,
        )

    def _counts(self) -> dict[str, int]:
        tables = (
            "source_inbox_imports",
            "source_inbox_items",
            "source_inbox_import_items",
            "source_inbox_state_events",
            "source_inbox_trading_impact_projections",
            "provider_execution_runs",
            "provider_call_attempts",
            "rounds",
            "source_inbox_round_drafts",
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            return {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in tables
            }

    def test_sidecar_is_atomic_and_parent_item_remains_byte_stable(self) -> None:
        packet = _packet()
        before = self._counts()
        result = self._import(packet, rules=self.rules)

        self.assertEqual(result["created_item_count"], 1)
        self.assertEqual(result["trading_impact_rules"]["evaluated_count"], 1)
        self.assertEqual(
            result["trading_impact_rules"]["created_projection_count"],
            1,
        )
        item_record = result["items"][0]
        self.assertEqual(item_record["item"]["impact_hypotheses"], [])
        self.assertEqual(len(item_record["impact_rule_projections"]), 1)
        projection = item_record["impact_rule_projections"][0]["projection"]
        self.assertEqual(projection["evaluation"], "matched")
        self.assertEqual(projection["hypotheses"][0]["impact_hypothesis"]["confidence"], 0.5)
        self.assertFalse(projection["interpretation_boundary"]["directional_forecast"])

        after = self._counts()
        self.assertEqual(after["source_inbox_imports"] - before["source_inbox_imports"], 1)
        self.assertEqual(after["source_inbox_items"] - before["source_inbox_items"], 1)
        self.assertEqual(
            after["source_inbox_trading_impact_projections"]
            - before["source_inbox_trading_impact_projections"],
            1,
        )
        for table in (
            "provider_execution_runs",
            "provider_call_attempts",
            "rounds",
            "source_inbox_round_drafts",
        ):
            self.assertEqual(after[table], before[table])

        with closing(sqlite3.connect(self.database_path)) as connection:
            stored_item_json, stored_item_sha = connection.execute(
                "SELECT item_json,item_sha256 FROM source_inbox_items"
            ).fetchone()
            stored_packet_json = connection.execute(
                "SELECT packet_json FROM source_inbox_imports"
            ).fetchone()[0]
        self.assertEqual(json.loads(stored_item_json)["impact_hypotheses"], [])
        self.assertEqual(stored_item_sha, item_record["item_sha256"])
        self.assertEqual(json.loads(stored_packet_json)["items"][0], item_record["item"])

    def test_exact_run_replay_reads_but_never_recomputes_or_backfills(self) -> None:
        packet = _packet()
        first = self._import(packet, rules=self.rules)
        with mock.patch.object(
            TradingImpactRulesV1,
            "project_item",
            side_effect=AssertionError("exact replay must not execute rules"),
        ):
            replay = self._import(copy.deepcopy(packet), rules=self.rules)

        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["import_id"], first["import_id"])
        accounting = replay["trading_impact_rules"]
        self.assertEqual(accounting["evaluated_count"], 0)
        self.assertEqual(accounting["created_projection_count"], 0)
        self.assertEqual(accounting["reused_projection_count"], 1)
        self.assertEqual(accounting["not_evaluated_count"], 0)
        self.assertEqual(self._counts()["source_inbox_trading_impact_projections"], 1)

    def test_new_run_duplicate_verifies_and_reuses_one_sidecar(self) -> None:
        first = self._import(_packet("phase5-sec-run-a"), rules=self.rules)
        second = self._import(_packet("phase5-sec-run-b"), rules=self.rules)

        self.assertFalse(first["idempotent_replay"])
        self.assertEqual(second["created_item_count"], 0)
        self.assertEqual(second["duplicate_item_count"], 1)
        self.assertEqual(
            second["trading_impact_rules"]["reused_projection_count"],
            1,
        )
        self.assertEqual(self._counts()["source_inbox_trading_impact_projections"], 1)
        self.assertEqual(len(second["items"][0]["impact_rule_projections"]), 1)

    def test_feature_cutover_never_backfills_an_exact_old_import(self) -> None:
        packet = _packet("phase5-cutover-old-run")
        disabled = self._import(packet, rules=None)
        self.assertEqual(disabled["items"][0]["impact_rule_projections"], [])

        exact_replay = self._import(copy.deepcopy(packet), rules=self.rules)
        self.assertTrue(exact_replay["idempotent_replay"])
        self.assertEqual(
            exact_replay["trading_impact_rules"]["not_evaluated_count"],
            1,
        )
        self.assertEqual(self._counts()["source_inbox_trading_impact_projections"], 0)

        future_run = self._import(
            _packet("phase5-cutover-future-run"),
            rules=self.rules,
        )
        self.assertEqual(
            future_run["trading_impact_rules"]["created_projection_count"],
            1,
        )
        self.assertEqual(self._counts()["source_inbox_trading_impact_projections"], 1)

    def test_sidecar_insert_failure_rolls_back_entire_new_import(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute(
                """CREATE TRIGGER fail_phase5_sidecar_insert
                   BEFORE INSERT ON source_inbox_trading_impact_projections
                   BEGIN
                     SELECT RAISE(ABORT,'simulated phase5 sidecar failure');
                   END"""
            )
        before = self._counts()

        with self.assertRaises(SourceInboxTradingImpactError):
            self._import(_packet("phase5-rollback-run"), rules=self.rules)

        self.assertEqual(self._counts(), before)

    def test_external_actor_cannot_supply_the_internal_rules_capability(self) -> None:
        before = self._counts()
        with self.assertRaises(SourceInboxError) as captured:
            self._import(
                _packet("phase5-unauthorized-run"),
                rules=self.rules,
                actor="local_user",
            )
        self.assertEqual(
            captured.exception.code,
            "SOURCE_INBOX_IMPACT_RULES_UNAUTHORIZED",
        )
        self.assertEqual(self._counts(), before)


if __name__ == "__main__":
    unittest.main()
