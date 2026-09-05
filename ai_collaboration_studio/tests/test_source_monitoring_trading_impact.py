from __future__ import annotations

import copy
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.market.sec_edgar import (  # noqa: E402
    SEC_MONITOR_SYMBOLS,
    SEC_TICKERS_URL,
    SecEdgarAdapter,
)
from backend.market.ir_releases import OfficialIrReleaseAdapter  # noqa: E402
from backend.source_inbox_contracts import canonical_sha256  # noqa: E402
from backend.source_inbox_service import SourceInboxService  # noqa: E402
from backend.source_monitoring.adapters.company_ir import (  # noqa: E402
    CompanyIrSourceAdapter,
)
from backend.source_monitoring.adapters.futu_anomaly import (  # noqa: E402
    FutuAnomalySourceAdapter,
)
from backend.source_monitoring.adapters.sec_filings import (  # noqa: E402
    SecFilingsSourceAdapter,
)
from backend.source_monitoring.registry import SourceAdapterRegistry  # noqa: E402
from backend.source_monitoring.scheduler import BackoffPolicy  # noqa: E402
from backend.source_monitoring.settings import SourceMonitoringSettings  # noqa: E402
from backend.source_monitoring.state_repository import (  # noqa: E402
    RUN_STATUS_ABANDONED,
    RUN_STATUS_SUCCEEDED,
    SourceMonitoringStateRepository,
)
from backend.source_monitoring.supervisor import (  # noqa: E402
    SourceMonitoringSupervisor,
    SourceMonitoringSupervisorError,
)
from backend.source_monitoring.trading_impact_rules import (  # noqa: E402
    TradingImpactRulesV1,
)
from backend.store import StudioStore  # noqa: E402


FIXED_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
FIXED_NOW_MS = int(FIXED_NOW.timestamp() * 1_000)
FUTU_OBSERVED_AT_MS = 1_788_184_800_000
FUTU_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "futu_anomaly"
    / "live_anomaly_snapshot.json"
)
RULESET_SHA256 = "28ee013d4841ff7d1f955204ae1f9fa8007b544c2a13f0bf7f5e7ee705b93603"


class SecFixtureFetcher:
    def __call__(self, url: str, _user_agent: str) -> dict:
        if url == SEC_TICKERS_URL:
            return {
                "0": {
                    "cik_str": 1_045_810,
                    "ticker": "NVDA",
                    "title": "NVIDIA Corporation",
                }
            }
        return {
            "cik": "0001045810",
            "name": "NVIDIA Corporation",
            "filings": {
                "recent": {
                    "accessionNumber": ["0001045810-26-000001"],
                    "form": ["8-K"],
                    "filingDate": ["2026-08-30"],
                    "reportDate": ["2026-08-30"],
                    "acceptanceDateTime": ["2026-08-30T20:00:00Z"],
                    "primaryDocument": ["valid.htm"],
                    "primaryDocDescription": ["Current report"],
                    "items": ["2.02,9.01"],
                }
            },
        }


class FakeQuoteClient:
    def __init__(self, *responses: dict[str, object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], bool]] = []

    def quote_batch(self, symbols, *, force=False):
        self.calls.append((tuple(symbols), force))
        selected = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return copy.deepcopy(selected)


class OtherIrFixtureFetcher:
    def __call__(self, _url: str, _allowed_hosts: set[str]) -> bytes:
        return b"""<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>Micron publishes a corporate governance update</title>
            <guid>mu-governance-update-1</guid>
            <link>https://investors.micron.com/news/governance-update</link>
            <pubDate>Sun, 30 Aug 2026 20:00:00 +0000</pubDate>
            <description>Official board and governance information.</description>
          </item>
        </channel></rss>"""


class SourceMonitoringTradingImpactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="ai-studio-monitoring-trading-impact-"
        )
        self.database_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.clock = [FIXED_NOW_MS]
        self.store = StudioStore(self.database_path)
        self.repository = SourceMonitoringStateRepository(
            self.store,
            clock_ms=lambda: self.clock[0],
        )
        self.inbox = SourceInboxService(
            self.store,
            clock=lambda: self.clock[0] / 1_000,
        )
        self.backoff = BackoffPolicy(
            initial_delay_ms=30_000,
            maximum_delay_ms=120_000,
            jitter_ratio=0,
            random_source=lambda: 0.5,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def settings(*, dry_run: bool, official_only: bool = True) -> SourceMonitoringSettings:
        return SourceMonitoringSettings(
            enabled=True,
            auto_start=False,
            official_only=official_only,
            allow_readonly_market=not official_only,
            trading_impact_rules_enabled=True,
            dry_run=dry_run,
            max_items_per_run=50,
            initial_mode=("from_time" if official_only else "seed_only"),
            from_time=("1970-01-01T00:00:00Z" if official_only else ""),
        )

    @staticmethod
    def disabled_settings() -> SourceMonitoringSettings:
        return SourceMonitoringSettings(
            enabled=True,
            auto_start=False,
            official_only=True,
            allow_readonly_market=False,
            trading_impact_rules_enabled=False,
            dry_run=True,
            max_items_per_run=50,
            initial_mode="from_time",
            from_time="1970-01-01T00:00:00Z",
        )

    @staticmethod
    def sec_adapter() -> SecFilingsSourceAdapter:
        return SecFilingsSourceAdapter(
            adapter=SecEdgarAdapter(
                user_agent="AI Studio monitor@example.com",
                fetch_json=SecFixtureFetcher(),
                clock=lambda: FIXED_NOW,
                allowed_symbols=SEC_MONITOR_SYMBOLS,
            ),
            allowed_symbols=["US.NVDA"],
            allowed_forms=["8-K"],
        )

    @staticmethod
    def futu_adapter() -> FutuAnomalySourceAdapter:
        snapshot = json.loads(FUTU_FIXTURE.read_text(encoding="utf-8"))
        return FutuAnomalySourceAdapter(
            market_adapter=FakeQuoteClient(snapshot), clock_ms=lambda: FUTU_OBSERVED_AT_MS
        )

    @staticmethod
    def company_ir_other_adapter() -> CompanyIrSourceAdapter:
        return CompanyIrSourceAdapter(
            adapter=OfficialIrReleaseAdapter(
                source_format="rss",
                fetch_bytes=OtherIrFixtureFetcher(),
                clock=lambda: FIXED_NOW,
            ),
            symbols=["US.MU"],
            per_symbol_limit=1,
            force=True,
        )

    def supervisor(
        self,
        adapter,
        *,
        dry_run: bool,
        official_only: bool = True,
        impact_rules=...,
        after_import_hook=None,
    ) -> SourceMonitoringSupervisor:
        rules = TradingImpactRulesV1() if impact_rules is ... else impact_rules
        return SourceMonitoringSupervisor(
            registry=SourceAdapterRegistry((adapter,), official_only=official_only),
            repository=self.repository,
            source_inbox=self.inbox,
            settings=self.settings(dry_run=dry_run, official_only=official_only),
            backoff_policy=self.backoff,
            clock_ms=lambda: self.clock[0],
            after_import_hook=after_import_hook,
            impact_rules=rules,
        )

    def enable(self, adapter) -> None:
        self.repository.set_enabled(
            adapter.adapter_key,
            config_version=adapter.config_version,
            enabled=True,
        )

    def mark_legacy_initialized(self, adapter) -> None:
        started = self.repository.start_run(
            adapter.adapter_key,
            config_version=adapter.config_version,
            dry_run=False,
        )["run"]
        self.repository.complete_run(
            started["run_id"],
            next_checkpoint={},
            status=RUN_STATUS_SUCCEEDED,
            observed_count=0,
            accepted_count=0,
            duplicate_count=0,
            rejected_count=0,
            next_due_at_ms=self.clock[0] + 60_000,
        )

    def side_effect_counts(self) -> tuple[int, int, int, int, int, int]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "source_inbox_imports",
                    "source_inbox_items",
                    "provider_execution_runs",
                    "provider_call_attempts",
                    "rounds",
                    "source_inbox_round_drafts",
                )
            )

    def impact_projection_count(self) -> int:
        with closing(sqlite3.connect(self.database_path)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM source_inbox_trading_impact_projections"
            ).fetchone()[0]

    def persisted_import_contract(self) -> tuple[dict, dict]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT packet_json,receipt_json FROM source_inbox_imports"
            ).fetchone()
        self.assertIsNotNone(row)
        return json.loads(row[0]), json.loads(row[1])

    def assert_engine_safety(self, accounting: dict[str, object]) -> None:
        self.assertEqual(set(accounting), {
            "version",
            "enabled",
            "scope",
            "ruleset_version",
            "ruleset_sha256",
            "evaluated_count",
            "matched_count",
            "no_match_count",
            "created_projection_count",
            "reused_projection_count",
            "not_evaluated_count",
            "safety",
        })
        self.assertEqual(accounting["version"], "trading_impact_import_accounting_v1")
        self.assertIs(accounting["enabled"], True)
        self.assertEqual(accounting["scope"], "impact_engine_only")
        self.assertEqual(accounting["ruleset_version"], "trading_impact_rules_v1")
        self.assertEqual(accounting["ruleset_sha256"], RULESET_SHA256)
        self.assertEqual(accounting["not_evaluated_count"], 0)
        self.assertEqual(accounting["safety"], {
            "execution_capability": "none",
            "live_trading_allowed": False,
            "provider_calls_performed": 0,
            "model_calls_performed": 0,
            "network_requests_performed": 0,
            "market_calls_performed": 0,
            "formal_rounds_created": 0,
        })

    def test_constructor_requires_exact_engine_and_matching_feature_flag(self) -> None:
        with self.assertRaises(TypeError):
            class DerivedRules(TradingImpactRulesV1):
                pass

        sealed_rules = TradingImpactRulesV1()
        with self.assertRaises(AttributeError):
            sealed_rules.project_item = object()

        adapter = self.sec_adapter()
        registry = SourceAdapterRegistry((adapter,), official_only=True)
        common = {
            "registry": registry,
            "repository": self.repository,
            "source_inbox": self.inbox,
            "backoff_policy": self.backoff,
            "clock_ms": lambda: self.clock[0],
        }
        with self.assertRaises(SourceMonitoringSupervisorError) as missing:
            SourceMonitoringSupervisor(
                **common,
                settings=self.settings(dry_run=True),
            )
        self.assertEqual(missing.exception.code, "SOURCE_MONITORING_IMPACT_RULES_INVALID")

        for supplied in (object(),):
            with self.subTest(supplied_type=type(supplied).__name__), self.assertRaises(
                SourceMonitoringSupervisorError
            ) as wrong:
                SourceMonitoringSupervisor(
                    **common,
                    settings=self.settings(dry_run=True),
                    impact_rules=supplied,
                )
            self.assertEqual(
                wrong.exception.code,
                "SOURCE_MONITORING_IMPACT_RULES_INVALID",
            )

        with self.assertRaises(SourceMonitoringSupervisorError) as disabled:
            SourceMonitoringSupervisor(
                **common,
                settings=self.disabled_settings(),
                impact_rules=TradingImpactRulesV1(),
            )
        self.assertEqual(disabled.exception.code, "SOURCE_MONITORING_IMPACT_RULES_DISABLED")

    def test_accounting_validation_distinguishes_live_and_exact_replay(self) -> None:
        supervisor = self.supervisor(self.sec_adapter(), dry_run=False)
        new_import_with_unevaluated_creation = supervisor._empty_impact_accounting()
        new_import_with_unevaluated_creation.update({
            "matched_count": 1,
            "created_projection_count": 1,
        })

        with self.assertRaises(SourceMonitoringSupervisorError) as invalid_new:
            supervisor._validate_impact_accounting(
                new_import_with_unevaluated_creation,
                idempotent_replay=False,
            )
        self.assertEqual(
            invalid_new.exception.code,
            "SOURCE_MONITORING_IMPACT_ACCOUNTING_INVALID",
        )

        exact_replay = supervisor._empty_impact_accounting()
        exact_replay.update({
            "matched_count": 1,
            "reused_projection_count": 1,
        })
        validated = supervisor._validate_impact_accounting(
            exact_replay,
            idempotent_replay=True,
        )
        self.assertEqual(validated["evaluated_count"], 0)
        self.assertEqual(validated["reused_projection_count"], 1)

        with self.assertRaises(SourceMonitoringSupervisorError):
            supervisor._validate_impact_accounting(
                exact_replay,
                idempotent_replay=True,
                expected_item_count=2,
            )

        with self.assertRaises(SourceMonitoringSupervisorError) as invalid_boolean:
            supervisor._validate_impact_accounting(
                exact_replay,
                idempotent_replay=1,
            )
        self.assertEqual(
            invalid_boolean.exception.code,
            "SOURCE_MONITORING_IMPACT_ACCOUNTING_INVALID",
        )

    def test_dry_run_sec_match_is_pure_and_does_not_advance_checkpoint(self) -> None:
        adapter = self.sec_adapter()
        rules = TradingImpactRulesV1()
        supervisor = self.supervisor(
            adapter,
            dry_run=True,
            impact_rules=rules,
        )
        self.enable(adapter)
        state_before = self.repository.get_state(adapter.adapter_key)
        counts_before = self.side_effect_counts()

        result = supervisor.run_once(adapter.adapter_key)

        accounting = result["trading_impact_rules"]
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["state"], state_before)
        self.assertEqual(result["state"]["checkpoint"], {})
        self.assertEqual(
            self.repository.get_state(adapter.adapter_key),
            state_before,
        )
        self.assertEqual(self.side_effect_counts(), counts_before)
        self.assertEqual(accounting["evaluated_count"], 1)
        self.assertEqual(accounting["matched_count"], 1)
        self.assertEqual(accounting["no_match_count"], 0)
        self.assertEqual(accounting["created_projection_count"], 0)
        self.assertEqual(accounting["reused_projection_count"], 0)
        self.assert_engine_safety(accounting)
        self.assertEqual(result["safety"]["market_calls_performed"], 0)
        self.assertEqual(result["safety"]["provider_calls_performed"], 0)
        self.assertEqual(result["safety"]["formal_rounds_created"], 0)
        self.assertEqual(self.impact_projection_count(), 0)

    def test_dry_run_no_match_is_counted_without_fabricating_a_hypothesis(self) -> None:
        adapter = self.company_ir_other_adapter()
        supervisor = self.supervisor(adapter, dry_run=True)
        self.enable(adapter)
        state_before = self.repository.get_state(adapter.adapter_key)

        result = supervisor.run_once(adapter.adapter_key)

        accounting = result["trading_impact_rules"]
        self.assertEqual(accounting["evaluated_count"], 1)
        self.assertEqual(accounting["matched_count"], 0)
        self.assertEqual(accounting["no_match_count"], 1)
        self.assert_engine_safety(accounting)
        self.assertEqual(self.side_effect_counts(), (0, 0, 0, 0, 0, 0))
        self.assertEqual(self.impact_projection_count(), 0)
        self.assertEqual(self.repository.get_state(adapter.adapter_key), state_before)

    def test_dry_run_futu_keeps_upstream_market_call_separate_from_engine_zero(self) -> None:
        self.clock[0] = FUTU_OBSERVED_AT_MS
        adapter = self.futu_adapter()
        supervisor = self.supervisor(
            adapter,
            dry_run=True,
            official_only=False,
        )
        self.enable(adapter)
        self.mark_legacy_initialized(adapter)
        state_before = self.repository.get_state(adapter.adapter_key)

        result = supervisor.run_once(adapter.adapter_key)

        accounting = result["trading_impact_rules"]
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["safety"]["market_calls_performed"], 1)
        self.assertEqual(result["safety"]["market_calls_accounting"], "exact")
        self.assertEqual(accounting["evaluated_count"], 1)
        self.assertEqual(accounting["matched_count"], 1)
        self.assertEqual(accounting["safety"]["market_calls_performed"], 0)
        self.assert_engine_safety(accounting)
        self.assertEqual(self.side_effect_counts(), (0, 0, 0, 0, 0, 0))
        self.assertEqual(self.impact_projection_count(), 0)
        self.assertEqual(self.repository.get_state(adapter.adapter_key), state_before)

    def test_live_sec_import_returns_created_projection_accounting(self) -> None:
        adapter = self.sec_adapter()
        rules = TradingImpactRulesV1()

        def mutate_import_result(_run_id: str, import_result: dict) -> None:
            import_result["trading_impact_rules"]["safety"][
                "market_calls_performed"
            ] = 1

        supervisor = self.supervisor(
            adapter,
            dry_run=False,
            impact_rules=rules,
            after_import_hook=mutate_import_result,
        )
        self.enable(adapter)
        counts_before = self.side_effect_counts()

        result = supervisor.run_once(adapter.adapter_key)

        accounting = result["trading_impact_rules"]
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["import"]["created_item_count"], 1)
        self.assertEqual(accounting["evaluated_count"], 1)
        self.assertEqual(accounting["matched_count"], 1)
        self.assertEqual(accounting["created_projection_count"], 1)
        self.assertEqual(accounting["reused_projection_count"], 0)
        self.assert_engine_safety(accounting)
        counts_after = self.side_effect_counts()
        self.assertEqual(counts_after[:2], (1, 1))
        self.assertEqual(counts_after[2:], counts_before[2:])
        self.assertEqual(self.impact_projection_count(), 1)
        stored = self.inbox.list_items()["items"][0]
        self.assertEqual(stored["item"]["impact_hypotheses"], [])
        self.assertEqual(len(stored["impact_rule_projections"]), 1)
        semantic = stored["impact_rule_projections"][0]["projection"][
            "source_item_binding"
        ]["source_semantic_binding"]
        self.assertEqual(semantic["adapter_id"], "sec_filings")
        self.assertEqual(semantic["rule_id"], "sec_current_filing_review_v1")
        self.assertEqual(
            semantic["symbol"],
            next(
                entity["id"]
                for entity in stored["item"]["entities"]
                if entity["kind"] == "security"
            ),
        )
        self.assertEqual(semantic["source_index"], 0)
        projection = stored["impact_rule_projections"][0]["projection"]
        self.assertEqual(projection["evaluation"], "matched")
        self.assertEqual(projection["source_binding"], {
            "adapter_id": "sec_filings",
            "source_class": "official_source",
            "source_channel": "official_source_monitor",
        })
        self.assertEqual(
            projection["hypotheses"][0]["impact_hypothesis"]["source_indexes"],
            [0],
        )
        packet, receipt = self.persisted_import_contract()
        self.assertNotIn("trading_impact_rules", packet)
        self.assertNotIn("trading_impact_rules", receipt)
        self.assertEqual(packet["items"][0]["impact_hypotheses"], [])
        self.assertEqual(receipt["normalized_packet_sha256"], canonical_sha256(packet))
        receipt_sha256 = receipt.pop("receipt_sha256")
        self.assertEqual(receipt_sha256, canonical_sha256(receipt))

    def test_live_zero_candidate_poll_returns_stable_enabled_accounting(self) -> None:
        adapter = self.sec_adapter()
        supervisor = self.supervisor(adapter, dry_run=False)
        self.enable(adapter)
        first = supervisor.run_once(adapter.adapter_key)
        counts_after_first = self.side_effect_counts()

        second = supervisor.run_once(adapter.adapter_key)

        accounting = second["trading_impact_rules"]
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(second["status"], "SUCCEEDED")
        self.assertIsNone(second["import"])
        self.assertEqual(accounting["evaluated_count"], 0)
        self.assertEqual(accounting["matched_count"], 0)
        self.assertEqual(accounting["no_match_count"], 0)
        self.assertEqual(accounting["created_projection_count"], 0)
        self.assertEqual(accounting["reused_projection_count"], 0)
        self.assertEqual(accounting["not_evaluated_count"], 0)
        self.assert_engine_safety(accounting)
        self.assertEqual(self.side_effect_counts(), counts_after_first)

    def test_live_crash_replay_reuses_one_projection_and_preserves_side_effects(self) -> None:
        adapter = self.sec_adapter()

        def crash_after_import(_run_id: str, _result: dict) -> None:
            raise SystemExit("fixture crash after Source Inbox import")

        crashing = self.supervisor(
            adapter,
            dry_run=False,
            after_import_hook=crash_after_import,
        )
        self.enable(adapter)
        side_effects_before = self.side_effect_counts()[2:]
        with self.assertRaises(SystemExit):
            crashing.run_once(adapter.adapter_key)
        self.assertEqual(self.repository.get_state(adapter.adapter_key)["checkpoint"], {})
        self.assertEqual(self.side_effect_counts()[:2], (1, 1))

        self.clock[0] += 60_000
        restarted = self.supervisor(adapter, dry_run=False)
        replay = restarted.run_once(adapter.adapter_key)

        self.assertIn("trading_impact_rules", replay, replay)
        accounting = replay["trading_impact_rules"]
        self.assertEqual(replay["status"], "SUCCEEDED")
        self.assertEqual(replay["import"]["created_item_count"], 0)
        self.assertEqual(replay["import"]["duplicate_item_count"], 1)
        self.assertEqual(accounting["created_projection_count"], 0)
        self.assertEqual(accounting["reused_projection_count"], 1)
        self.assertEqual(accounting["not_evaluated_count"], 0)
        self.assertNotEqual(replay["state"]["checkpoint"], {})
        self.assertIn(
            RUN_STATUS_ABANDONED,
            {
                run["status"]
                for run in self.repository.list_runs(adapter_key=adapter.adapter_key)
            },
        )
        self.assertEqual(self.side_effect_counts()[:2], (2, 1))
        self.assertEqual(self.side_effect_counts()[2:], side_effects_before)
        self.assertEqual(self.impact_projection_count(), 1)


if __name__ == "__main__":
    unittest.main()
