from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.decision_lineage import (
    artifact_binding_payload,
    calculate_event_sha256,
    canonical_sha256,
    normalize_resource_snapshot,
)
from backend.paper_portfolio import default_paper_portfolio_plan
from backend.paper_portfolio_service import (
    DEFAULT_WALK_FORWARD_CONFIG,
    PaperPortfolioService,
)
from backend.store import StudioStore
from tests.test_walk_forward_integration import FakeWalkForwardMarket


ROOM_ID = "room_storage"
RESOURCE_TYPE = "simulation.paper_portfolio"


def audited_message_evidence(message_id: str) -> dict[str, object]:
    return {
        "type": "message",
        "id": message_id,
        "evidence_role": "support",
        "verification_status": "source_checked",
        "review_note": "",
    }


def valid_plan(name: str = "Decision-linked paper portfolio") -> dict[str, object]:
    plan = default_paper_portfolio_plan()
    plan["name"] = name
    for position, side, weight in zip(
        plan["positions"],
        ("LONG", "LONG", "SHORT", "FLAT"),
        (25, 20, 10, 0),
    ):
        position["side"] = side
        position["weight_pct"] = weight
        if side != "FLAT":
            position["thesis"] = "Paper-only hypothesis derived from the selected candidate."
            position["invalidation"] = "Return to the user when the stated condition fails."
    return plan


def passing_evaluation() -> dict[str, object]:
    return {
        "version": "paper_portfolio_risk_v1",
        "state": "ready",
        "evaluated_at": "2026-08-01T00:00:00Z",
        "input_fingerprint": "fixture-only",
        "exposures": {
            "gross_exposure_pct": 55,
            "net_exposure_pct": 35,
        },
        "metrics": {
            "annualized_volatility_pct": 12.5,
            "max_drawdown_pct": 8.0,
        },
        "stress_results": [],
        "risk_gate": {
            "status": "PASS",
            "ready": True,
            "blockers": [],
        },
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


class DecisionLineageContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "studio.sqlite3"
        self.store = StudioStore(self.db_path)
        self.message = self.store.add_message(
            ROOM_ID,
            sender_type="user",
            sender_name="User",
            content="Use a reversible paper-only allocation and preserve the stop conditions.",
        )
        self.artifact_counter = 0

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_confirmed_candidate(self) -> dict[str, object]:
        self.artifact_counter += 1
        evidence = [audited_message_evidence(self.message["id"])]
        artifact = self.store.create_artifact(
            ROOM_ID,
            title=f"Candidate decision {self.artifact_counter}",
            content={
                "summary": "Compare two paper-only alternatives before the user decides.",
                "summary_evidence": evidence,
                "requirements": [],
                "risks": [],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {
                            "id": "paper_small",
                            "title": "Small paper allocation",
                            "description": "Use limited paper weights with explicit invalidation.",
                            "benefits": ["Reversible"],
                            "risks": ["Limited coverage"],
                            "evidence": evidence,
                        },
                        {
                            "id": "paper_broad",
                            "title": "Broad paper allocation",
                            "description": "Use broader paper weights for comparison only.",
                            "benefits": ["Broader coverage"],
                            "risks": ["Higher research complexity"],
                            "evidence": evidence,
                        },
                    ],
                    "preferred_option_id": "paper_small",
                    "rationale": "The smaller paper allocation is easier to invalidate and review.",
                    "evidence": evidence,
                },
            },
            created_by="user",
        )
        self.assertIsNotNone(artifact)
        confirmed = self.store.confirm_artifact(
            ROOM_ID,
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="user",
        )
        self.assertIsNotNone(confirmed)
        return confirmed

    def create_decision(self, action: str = "support") -> tuple[dict[str, object], dict[str, object]]:
        artifact = self.create_confirmed_candidate()
        decision = self.store.create_artifact_user_decision(
            ROOM_ID,
            artifact["id"],
            expected_version=artifact["version"],
            action=action,
            rationale=f"User recorded the {action} outcome for this exact candidate version.",
            **({"selected_option_id": "paper_small"} if action == "support" else {}),
        )
        self.assertIsNotNone(decision)
        return artifact, decision

    def create_linked_portfolio(
        self,
        decision: dict[str, object],
        *,
        name: str = "Linked paper portfolio",
        evaluation: dict[str, object] | None = None,
    ) -> dict[str, object]:
        portfolio = self.store.create_paper_portfolio(
            ROOM_ID,
            valid_plan(name),
            evaluation or passing_evaluation(),
            created_by="user",
            user_decision_id=decision["id"],
            derivation_note="Implements the exact user-supported preferred candidate.",
        )
        self.assertIsNotNone(portfolio)
        return portfolio

    def package_for(self, room_id: str, decision_id: str) -> dict[str, object]:
        return next(
            package
            for package in self.store.list_decision_packages(room_id)
            if package["package_id"] == decision_id
        )

    def assert_no_new_portfolio(self, operation) -> None:
        before = [item["id"] for item in self.store.list_paper_portfolios(ROOM_ID)]
        with self.assertRaises(ValueError):
            operation()
        after = [item["id"] for item in self.store.list_paper_portfolios(ROOM_ID)]
        self.assertEqual(after, before)

    def test_legacy_unreviewed_confirmed_decision_cannot_derive_new_lineage_resource(self) -> None:
        artifact = self.store.create_artifact(
            ROOM_ID,
            title="Legacy confirmation without evidence review",
            content={
                "summary": "A legacy version was marked confirmed before the current gate existed.",
                "summary_evidence": [{"type": "message", "id": self.message["id"]}],
                "requirements": [],
                "risks": [],
                "conclusions": [],
                "disagreements": [],
                "unknowns": [],
                "actions": [],
                "decision": {
                    "status": "candidate",
                    "options": [
                        {
                            "id": "legacy_small",
                            "title": "Legacy small plan",
                            "description": "A paper-only candidate whose evidence was never reviewed.",
                            "benefits": ["Reversible"],
                            "risks": ["Unreviewed evidence"],
                            "evidence": [{"type": "message", "id": self.message["id"]}],
                        },
                        {
                            "id": "legacy_broad",
                            "title": "Legacy broad plan",
                            "description": "A comparison candidate with the same evidence defect.",
                            "benefits": ["Comparison"],
                            "risks": ["Unreviewed evidence"],
                            "evidence": [{"type": "message", "id": self.message["id"]}],
                        },
                    ],
                    "preferred_option_id": "legacy_small",
                    "rationale": "Legacy preference retained only for migration testing.",
                    "evidence": [{"type": "message", "id": self.message["id"]}],
                },
            },
            created_by="legacy_import",
        )
        decision_id = "user_decision_legacy_unreviewed"
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                """UPDATE artifacts
                   SET status='CONFIRMED',confirmed_by='legacy_user',confirmed_at=updated_at
                   WHERE room_id=? AND id=?""",
                (ROOM_ID, artifact["id"]),
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE room_id=? AND id=?",
                (ROOM_ID, artifact["id"]),
            ).fetchone()
            snapshot = dict(row)
            snapshot["content"] = json.loads(snapshot.pop("content_json"))
            connection.execute(
                """UPDATE artifact_versions SET snapshot_json=?
                   WHERE room_id=? AND artifact_id=? AND version=?""",
                (
                    json.dumps(snapshot, ensure_ascii=False),
                    ROOM_ID,
                    artifact["id"],
                    artifact["version"],
                ),
            )
            connection.execute(
                """INSERT INTO artifact_user_decisions(
                       id,room_id,artifact_id,artifact_version,action,rationale,
                       preferred_option_id,artifact_snapshot_sha256,created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    ROOM_ID,
                    artifact["id"],
                    artifact["version"],
                    "support",
                    "Legacy support record imported without the current evidence gate.",
                    "legacy_small",
                    canonical_sha256(artifact_binding_payload(snapshot)),
                    "legacy_user",
                    int(row["updated_at"] or 0) + 1,
                ),
            )

        before_portfolios = self.store.list_paper_portfolios(ROOM_ID)
        with self.assertRaisesRegex(ValueError, "用户决定来源快照完整性校验失败"):
            self.store.create_paper_portfolio(
                ROOM_ID,
                valid_plan("Blocked legacy-derived portfolio"),
                passing_evaluation(),
                created_by="user",
                user_decision_id=decision_id,
                derivation_note="Must be rejected before a new lineage resource is committed.",
            )
        self.assertEqual(self.store.list_paper_portfolios(ROOM_ID), before_portfolios)
        package = self.package_for(ROOM_ID, decision_id)
        self.assertFalse(package["anchor"]["integrity_ok"])
        self.assertIn(
            "CURRENT_ARTIFACT_CONFIRMATION_INVALID",
            package["anchor"]["integrity_issues"],
        )
        self.assertEqual(package["lineage"], [])

    def test_current_support_creates_implements_event_and_active_safe_package(self) -> None:
        artifact, decision = self.create_decision("support")
        portfolio = self.create_linked_portfolio(decision)

        package = self.package_for(ROOM_ID, decision["id"])
        self.assertEqual(package["state"], "active")
        self.assertTrue(package["integrity_ok"])
        self.assertEqual(package["anchor"]["artifact_id"], artifact["id"])
        self.assertEqual(package["anchor"]["artifact_version"], artifact["version"])
        self.assertEqual(package["anchor"]["preferred_option_id"], "paper_small")
        self.assertEqual(len(package["lineage"]), 1)

        event = package["lineage"][0]
        self.assertEqual(event["sequence_no"], 1)
        self.assertEqual(event["relation_type"], "implements")
        self.assertEqual(event["resource_type"], RESOURCE_TYPE)
        self.assertEqual(event["resource_id"], portfolio["id"])
        self.assertEqual(event["resource_revision"], "1")
        self.assertEqual(event["resource_snapshot"]["status"], "DRAFT")
        self.assertEqual(len(event["resource_snapshot_sha256"]), 64)
        self.assertEqual(len(event["event_sha256"]), 64)

        for record in (package, event):
            self.assertEqual(record["execution_capability"], "none")
            self.assertFalse(record["live_trading_allowed"])
            self.assertFalse(record["can_autonomously_decide"])

        snapshot = self.store.room_snapshot(ROOM_ID)
        self.assertEqual(snapshot["decision_packages"], self.store.list_decision_packages(ROOM_ID))

    def test_hold_and_return_reject_linked_creation_without_partial_portfolio(self) -> None:
        for action in ("hold", "return"):
            with self.subTest(action=action):
                _artifact, decision = self.create_decision(action)
                self.assert_no_new_portfolio(
                    lambda: self.create_linked_portfolio(
                        decision,
                        name=f"Must not persist for {action}",
                    )
                )
                package = self.package_for(ROOM_ID, decision["id"])
                self.assertEqual(package["state"], "non_actionable")
                self.assertEqual(package["lineage"], [])

    def test_superseded_same_version_decision_rejects_creation_without_partial_portfolio(self) -> None:
        artifact, supported = self.create_decision("support")
        held = self.store.create_artifact_user_decision(
            ROOM_ID,
            artifact["id"],
            expected_version=artifact["version"],
            action="hold",
            rationale="A newer same-version decision supersedes the earlier support.",
        )
        self.assertIsNotNone(held)

        self.assert_no_new_portfolio(lambda: self.create_linked_portfolio(supported))
        self.assertEqual(self.package_for(ROOM_ID, supported["id"])["state"], "stale")
        self.assertEqual(self.package_for(ROOM_ID, held["id"])["state"], "non_actionable")

    def test_artifact_revision_rejects_old_decision_without_partial_portfolio(self) -> None:
        artifact, supported = self.create_decision("support")
        revised_content = copy.deepcopy(artifact["content"])
        revised_content["summary"] = "The artifact changed and requires a new confirmation and decision."
        revised = self.store.update_artifact(
            ROOM_ID,
            artifact["id"],
            {
                "expected_version": artifact["version"],
                "title": artifact["title"],
                "content": revised_content,
            },
        )
        self.assertEqual(revised["status"], "DRAFT")

        self.assert_no_new_portfolio(lambda: self.create_linked_portfolio(supported))
        self.assertEqual(self.package_for(ROOM_ID, supported["id"])["state"], "stale")

    def test_cross_room_decision_is_rejected_and_transaction_rolls_back(self) -> None:
        _artifact, supported = self.create_decision("support")
        other_room = self.store.create_room(
            "Other storage room",
            "Prove that lineage never crosses room boundaries.",
            template_id="us_storage_committee",
        )
        other_room_id = other_room["room"]["id"]
        before = self.store.list_paper_portfolios(other_room_id)

        with self.assertRaises(ValueError):
            self.store.create_paper_portfolio(
                other_room_id,
                valid_plan("Cross-room portfolio"),
                passing_evaluation(),
                created_by="user",
                user_decision_id=supported["id"],
                derivation_note="This must fail because the decision belongs to another room.",
            )

        self.assertEqual(self.store.list_paper_portfolios(other_room_id), before)
        self.assertEqual(self.store.list_decision_packages(other_room_id), [])

    def test_unlinked_legacy_portfolio_is_preserved_but_excluded_from_package(self) -> None:
        legacy = self.store.create_paper_portfolio(
            ROOM_ID,
            valid_plan("Legacy unlinked portfolio"),
            passing_evaluation(),
            created_by="user",
        )
        _artifact, supported = self.create_decision("support")
        linked = self.create_linked_portfolio(supported)

        portfolio_ids = {item["id"] for item in self.store.list_paper_portfolios(ROOM_ID)}
        self.assertIn(legacy["id"], portfolio_ids)
        self.assertIn(linked["id"], portfolio_ids)
        package_resource_ids = {
            event["resource_id"]
            for event in self.package_for(ROOM_ID, supported["id"])["lineage"]
        }
        self.assertIn(linked["id"], package_resource_ids)
        self.assertNotIn(legacy["id"], package_resource_ids)

    def test_update_and_confirm_append_revises_and_confirms_with_confirmed_snapshot(self) -> None:
        _artifact, supported = self.create_decision("support")
        portfolio = self.create_linked_portfolio(supported)
        revised_plan = valid_plan("Revised linked portfolio")
        revised_plan["positions"][0]["weight_pct"] = 20

        revised = self.store.update_paper_portfolio(
            ROOM_ID,
            portfolio["id"],
            revised_plan,
            passing_evaluation(),
            expected_version=portfolio["version"],
        )
        confirmed = self.store.confirm_paper_portfolio(
            ROOM_ID,
            portfolio["id"],
            expected_version=revised["version"],
            confirmed_by="user",
        )

        self.assertEqual(confirmed["status"], "CONFIRMED")
        package = self.package_for(ROOM_ID, supported["id"])
        events = package["lineage"]
        self.assertEqual(
            [event["relation_type"] for event in events],
            ["implements", "revises", "confirms"],
        )
        self.assertEqual([event["sequence_no"] for event in events], [1, 2, 3])
        self.assertEqual(events[1]["resource_revision"], str(revised["version"]))
        self.assertEqual(events[1]["resource_snapshot"]["status"], "DRAFT")
        self.assertEqual(events[2]["resource_revision"], str(confirmed["version"]))
        self.assertEqual(events[2]["resource_snapshot"]["status"], "CONFIRMED")
        self.assertTrue(all(event["integrity_ok"] for event in events))
        self.assertTrue(package["integrity_ok"])

    def test_forward_observation_branches_from_exact_confirmed_portfolio(self) -> None:
        artifact, supported = self.create_decision("support")
        portfolio = self.create_linked_portfolio(supported)
        confirmed = self.store.confirm_paper_portfolio(
            ROOM_ID,
            portfolio["id"],
            expected_version=portfolio["version"],
            confirmed_by="user",
        )

        observation = self.store.create_observation(ROOM_ID, {
            "symbol": "MU",
            "direction": "UP",
            "horizon_days": 5,
            "threshold_pct": 2,
            "thesis": "未来五个交易日验证用户支持方案中的可证伪条件。",
            "counter_case": "需求或定价快速转弱。",
            "evidence": {},
            "created_by": "user",
            "user_decision_id": supported["id"],
            "source_portfolio_id": confirmed["id"],
            "source_portfolio_version": confirmed["version"],
            "derivation_note": "从精确确认的模拟组合建立前向观察。",
        })

        self.assertEqual(observation["artifact_id"], artifact["id"])
        self.assertEqual(observation["artifact_version"], artifact["version"])
        package = self.package_for(ROOM_ID, supported["id"])
        event = package["lineage"][-1]
        self.assertEqual(event["relation_type"], "tests")
        self.assertEqual(event["resource_type"], "validation.forward_observation")
        self.assertEqual(event["resource_id"], observation["id"])
        self.assertEqual(event["resource_snapshot"]["source_portfolio_id"], confirmed["id"])
        self.assertEqual(
            event["resource_snapshot"]["source_portfolio_version"],
            confirmed["version"],
        )

        pending = self.store.confirm_observation(ROOM_ID, observation["id"], {})
        self.assertEqual(pending["status"], "PENDING_BASELINE")
        event = self.package_for(ROOM_ID, supported["id"])["lineage"][-1]
        self.assertEqual(event["relation_type"], "confirms")
        self.assertEqual(event["resource_state"], "PENDING_BASELINE")

        opened = self.store.set_observation_baseline(
            ROOM_ID,
            observation["id"],
            {
                "price": 100,
                "time": "2026-08-01 16:00:00",
                "snapshot_id": "fixture-baseline",
                "benchmark": {},
            },
        )
        self.assertEqual(opened["status"], "OPEN")
        event = self.package_for(ROOM_ID, supported["id"])["lineage"][-1]
        self.assertEqual(event["relation_type"], "revises")
        self.assertEqual(event["resource_state"], "OPEN")

        resolved = self.store.resolve_observation(
            ROOM_ID,
            observation["id"],
            outcome_price=104,
            outcome_time="2026-08-08 16:00:00",
            return_pct=4,
            measurement_method="qfq_close_to_close_v2",
            scoring_baseline_price=100,
            scoring_baseline_time="2026-08-01 16:00:00",
            hit=True,
            note="fixture outcome",
        )
        self.assertEqual(resolved["status"], "RESOLVED")
        package = self.package_for(ROOM_ID, supported["id"])
        event = package["lineage"][-1]
        self.assertEqual(event["relation_type"], "records_outcome")
        self.assertEqual(event["resource_state"], "RESOLVED")
        self.assertTrue(event["resource_snapshot"]["hit"])
        self.assertTrue(package["integrity_ok"])
        self.assertTrue(all(item["integrity_ok"] for item in package["lineage"]))

    def test_existing_observation_can_finish_after_support_decision_becomes_stale(self) -> None:
        artifact, supported = self.create_decision("support")
        portfolio = self.create_linked_portfolio(supported)
        confirmed = self.store.confirm_paper_portfolio(
            ROOM_ID,
            portfolio["id"],
            expected_version=portfolio["version"],
            confirmed_by="user",
        )
        observation_payload = {
            "symbol": "MU",
            "direction": "UP",
            "horizon_days": 5,
            "threshold_pct": 2,
            "thesis": "验证已经建立的历史观察能否完整收尾。",
            "counter_case": "需求或定价快速转弱。",
            "evidence": {},
            "created_by": "user",
            "user_decision_id": supported["id"],
            "source_portfolio_id": confirmed["id"],
            "source_portfolio_version": confirmed["version"],
            "derivation_note": "从当前用户支持决定建立可证伪观察。",
        }
        observation = self.store.create_observation(ROOM_ID, observation_payload)
        held = self.store.create_artifact_user_decision(
            ROOM_ID,
            artifact["id"],
            expected_version=artifact["version"],
            action="hold",
            rationale="A later user decision makes the earlier support package historical.",
        )
        self.assertIsNotNone(held)
        self.assertEqual(self.package_for(ROOM_ID, supported["id"])["state"], "stale")

        confirmed_observation = self.store.confirm_observation(
            ROOM_ID,
            observation["id"],
            {
                "price": 100,
                "time": "2026-08-01 16:00:00",
                "snapshot_id": "historical-fixture",
                "benchmark": {},
            },
        )
        self.assertEqual(confirmed_observation["status"], "OPEN")
        resolved = self.store.resolve_observation(
            ROOM_ID,
            observation["id"],
            outcome_price=97,
            outcome_time="2026-08-08 16:00:00",
            return_pct=-3,
            measurement_method="qfq_close_to_close_v2",
            scoring_baseline_price=100,
            scoring_baseline_time="2026-08-01 16:00:00",
            hit=False,
            note="historical fixture outcome",
        )
        self.assertEqual(resolved["status"], "RESOLVED")
        package = self.package_for(ROOM_ID, supported["id"])
        self.assertEqual(package["state"], "stale")
        self.assertEqual(
            [event["relation_type"] for event in package["lineage"][-2:]],
            ["confirms", "records_outcome"],
        )
        self.assertTrue(package["integrity_ok"])

        before_ids = {item["id"] for item in self.store.list_observations(ROOM_ID)}
        with self.assertRaisesRegex(ValueError, "已经过期"):
            self.store.create_observation(ROOM_ID, observation_payload)
        after_ids = {item["id"] for item in self.store.list_observations(ROOM_ID)}
        self.assertEqual(after_ids, before_ids)

    def test_missing_tail_tests_event_blocks_observation_confirmation_and_rolls_back(self) -> None:
        _artifact, supported = self.create_decision("support")
        portfolio = self.create_linked_portfolio(supported)
        confirmed_portfolio = self.store.confirm_paper_portfolio(
            ROOM_ID,
            portfolio["id"],
            expected_version=portfolio["version"],
            confirmed_by="user",
        )
        observation = self.store.create_observation(ROOM_ID, {
            "symbol": "MU",
            "direction": "UP",
            "horizon_days": 5,
            "threshold_pct": 2,
            "thesis": "A linked observation must never silently become legacy-unlinked.",
            "counter_case": "The validation condition fails.",
            "evidence": {},
            "created_by": "user",
            "user_decision_id": supported["id"],
            "source_portfolio_id": confirmed_portfolio["id"],
            "source_portfolio_version": confirmed_portfolio["version"],
            "derivation_note": "Create a forward observation with an auditable source.",
        })
        package = self.package_for(ROOM_ID, supported["id"])
        tests_event = package["lineage"][-1]
        self.assertEqual(tests_event["relation_type"], "tests")
        self.assertEqual(tests_event["resource_id"], observation["id"])

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "DELETE FROM decision_lineage_events WHERE id=?",
                (tests_event["id"],),
            )

        with self.assertRaises(ValueError):
            self.store.confirm_observation(ROOM_ID, observation["id"], {})
        unchanged = self.store.get_observation(ROOM_ID, observation["id"])
        self.assertEqual(unchanged["status"], "PROPOSED")
        self.assertFalse(unchanged["user_confirmed"])
        self.assertFalse(any(
            event["resource_id"] == observation["id"]
            for event in self.package_for(ROOM_ID, supported["id"])["lineage"]
        ))

    def test_stale_exception_rejects_non_lifecycle_relation_for_existing_observation(self) -> None:
        artifact, supported = self.create_decision("support")
        portfolio = self.create_linked_portfolio(supported)
        confirmed_portfolio = self.store.confirm_paper_portfolio(
            ROOM_ID,
            portfolio["id"],
            expected_version=portfolio["version"],
            confirmed_by="user",
        )
        observation = self.store.create_observation(ROOM_ID, {
            "symbol": "MU",
            "direction": "UP",
            "horizon_days": 5,
            "threshold_pct": 2,
            "thesis": "Only lifecycle completion may continue after the source decision is stale.",
            "counter_case": "The validation condition fails.",
            "evidence": {},
            "created_by": "user",
            "user_decision_id": supported["id"],
            "source_portfolio_id": confirmed_portfolio["id"],
            "source_portfolio_version": confirmed_portfolio["version"],
            "derivation_note": "Create a forward observation before the decision becomes stale.",
        })
        self.store.create_artifact_user_decision(
            ROOM_ID,
            artifact["id"],
            expected_version=artifact["version"],
            action="hold",
            rationale="Make the earlier support decision historical.",
        )
        source_event = self.package_for(ROOM_ID, supported["id"])["lineage"][-1]
        snapshot = {
            **observation,
            "source_portfolio_id": confirmed_portfolio["id"],
            "source_portfolio_version": confirmed_portfolio["version"],
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        }

        with self.assertRaises(ValueError):
            with self.store._lock, closing(self.store._connect()) as connection, connection:
                self.store._append_decision_lineage_event(
                    connection,
                    ROOM_ID,
                    supported["id"],
                    relation_type="tests",
                    resource_type=source_event["resource_type"],
                    resource_id=observation["id"],
                    resource_revision="illegal-repeat-test",
                    resource_state="PROPOSED",
                    resource_snapshot=snapshot,
                    relation_note="A stale exception must not create another tests relation.",
                    created_by="user",
                    created_at=source_event["created_at"] + 1,
                    allow_stale_existing_resource=True,
                )

    def test_stale_exception_rejects_resource_with_non_tests_first_event(self) -> None:
        artifact, supported = self.create_decision("support")
        observation = self.store.create_observation(ROOM_ID, {
            "symbol": "MU",
            "direction": "UP",
            "horizon_days": 5,
            "threshold_pct": 2,
            "thesis": "Fixture observation for a semantically invalid lineage root.",
            "counter_case": "The fixture is rejected.",
            "evidence": {},
            "created_by": "user",
        })
        snapshot = normalize_resource_snapshot({
            **observation,
            "source_portfolio_id": "",
            "source_portfolio_version": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
        })
        event = {
            "id": "lineage_invalid_observation_root",
            "room_id": ROOM_ID,
            "user_decision_id": supported["id"],
            "sequence_no": 1,
            "relation_type": "confirms",
            "resource_type": "validation.forward_observation",
            "resource_id": observation["id"],
            "resource_revision": "invalid-root",
            "resource_state": "PROPOSED",
            "relation_note": "Hash-valid fixture with an invalid first relation.",
            "resource_snapshot_json": json.dumps(
                snapshot,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "resource_snapshot_sha256": canonical_sha256(snapshot),
            "previous_event_sha256": "",
            "event_sha256": "",
            "created_by": "fixture",
            "created_at": 1,
        }
        event["event_sha256"] = calculate_event_sha256(event)
        with closing(self.store._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO decision_lineage_events(
                    id,room_id,user_decision_id,sequence_no,relation_type,
                    resource_type,resource_id,resource_revision,resource_state,
                    relation_note,resource_snapshot_json,resource_snapshot_sha256,
                    previous_event_sha256,event_sha256,created_by,created_at
                ) VALUES(
                    :id,:room_id,:user_decision_id,:sequence_no,:relation_type,
                    :resource_type,:resource_id,:resource_revision,:resource_state,
                    :relation_note,:resource_snapshot_json,:resource_snapshot_sha256,
                    :previous_event_sha256,:event_sha256,:created_by,:created_at
                )""",
                event,
            )
        self.store.create_artifact_user_decision(
            ROOM_ID,
            artifact["id"],
            expected_version=artifact["version"],
            action="hold",
            rationale="Make the malformed support package stale.",
        )

        with self.assertRaises(ValueError):
            with self.store._lock, closing(self.store._connect()) as connection, connection:
                self.store._append_decision_lineage_event(
                    connection,
                    ROOM_ID,
                    supported["id"],
                    relation_type="records_outcome",
                    resource_type="validation.forward_observation",
                    resource_id=observation["id"],
                    resource_revision="resolved:fixture",
                    resource_state="RESOLVED",
                    resource_snapshot={**snapshot, "status": "RESOLVED"},
                    relation_note="An invalid lineage root must not unlock stale lifecycle writes.",
                    created_by="system",
                    created_at=2,
                    allow_stale_existing_resource=True,
                )

    def test_in_place_confirmed_portfolio_drift_blocks_new_observation(self) -> None:
        _artifact, supported = self.create_decision("support")
        portfolio = self.create_linked_portfolio(supported)
        confirmed = self.store.confirm_paper_portfolio(
            ROOM_ID,
            portfolio["id"],
            expected_version=portfolio["version"],
            confirmed_by="user",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE paper_portfolios SET name=? WHERE room_id=? AND id=?",
                ("Tampered without a version change", ROOM_ID, confirmed["id"]),
            )
        before_ids = {item["id"] for item in self.store.list_observations(ROOM_ID)}

        with self.assertRaises(ValueError):
            self.store.create_observation(ROOM_ID, {
                "symbol": "MU",
                "direction": "UP",
                "horizon_days": 5,
                "threshold_pct": 2,
                "thesis": "Do not branch from a portfolio that drifted after confirmation.",
                "counter_case": "The stored confirmation snapshot no longer matches.",
                "evidence": {},
                "created_by": "user",
                "user_decision_id": supported["id"],
                "source_portfolio_id": confirmed["id"],
                "source_portfolio_version": confirmed["version"],
                "derivation_note": "This request must fail closed on snapshot drift.",
            })
        self.assertEqual(
            {item["id"] for item in self.store.list_observations(ROOM_ID)},
            before_ids,
        )

    def test_missing_tail_implements_event_breaks_head_and_blocks_portfolio_update(self) -> None:
        _artifact, supported = self.create_decision("support")
        portfolio = self.create_linked_portfolio(supported)
        package = self.package_for(ROOM_ID, supported["id"])
        implements_event = package["lineage"][-1]
        self.assertEqual(implements_event["relation_type"], "implements")
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "DELETE FROM decision_lineage_events WHERE id=?",
                (implements_event["id"],),
            )

        truncated = self.package_for(ROOM_ID, supported["id"])
        self.assertEqual(truncated["state"], "chain_broken")
        self.assertFalse(truncated["integrity_ok"])
        with self.assertRaises(ValueError):
            self.store.update_paper_portfolio(
                ROOM_ID,
                portfolio["id"],
                valid_plan("Must not silently become unlinked"),
                passing_evaluation(),
                expected_version=portfolio["version"],
            )
        self.assertEqual(
            self.store.get_paper_portfolio(ROOM_ID, portfolio["id"])["version"],
            portfolio["version"],
        )

    def test_v3_rebuild_failure_rolls_back_v2_table_data_and_marker(self) -> None:
        _artifact, supported = self.create_decision("support")
        self.create_linked_portfolio(supported)
        v2_relations = (
            "implements",
            "revises",
            "confirms",
            "tests",
            "evaluates",
        )
        with closing(self.store._connect()) as connection, connection:
            connection.execute("DROP TABLE decision_lineage_resources")
            connection.execute("DROP TABLE decision_lineage_heads")
            connection.execute(
                "DELETE FROM schema_migrations WHERE key IN (?,?)",
                (
                    "decision_lineage_outcome_relation_v3",
                    "decision_lineage_registry_v4",
                ),
            )
            StudioStore._rebuild_decision_lineage_events_table(
                connection,
                temporary_table="decision_lineage_events_v2",
                allowed_relations=v2_relations,
            )

        def read_v2_state() -> tuple[str, list[tuple[object, ...]], set[str]]:
            with closing(self.store._connect()) as connection:
                table_sql = str(connection.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='decision_lineage_events'"
                ).fetchone()["sql"])
                rows = [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM decision_lineage_events ORDER BY id"
                    ).fetchall()
                ]
                migrations = {
                    str(row["key"])
                    for row in connection.execute(
                        "SELECT key FROM schema_migrations"
                    ).fetchall()
                }
                return table_sql, rows, migrations

        v2_sql, v2_rows, v2_migrations = read_v2_state()
        self.assertNotIn("'records_outcome'", v2_sql)
        self.assertTrue(v2_rows)
        self.assertNotIn("decision_lineage_outcome_relation_v3", v2_migrations)

        original_rebuild = StudioStore._rebuild_decision_lineage_events_table

        def rebuild_then_fail(
            connection: sqlite3.Connection,
            *,
            temporary_table: str,
            allowed_relations: tuple[str, ...],
        ) -> None:
            original_rebuild(
                connection,
                temporary_table=temporary_table,
                allowed_relations=allowed_relations,
            )
            raise RuntimeError("injected failure after v3 table rebuild")

        with patch.object(
            StudioStore,
            "_rebuild_decision_lineage_events_table",
            new=staticmethod(rebuild_then_fail),
        ):
            with closing(self.store._connect()) as connection:
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    self.store._apply_decision_lineage_outcome_relation_migration(
                        connection
                    )

        failed_sql, failed_rows, failed_migrations = read_v2_state()
        self.assertEqual(failed_sql, v2_sql)
        self.assertEqual(failed_rows, v2_rows)
        self.assertNotIn("decision_lineage_outcome_relation_v3", failed_migrations)
        with closing(self.store._connect()) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='decision_lineage_events_v3'"
            ).fetchone())

        with closing(self.store._connect()) as connection, connection:
            self.store._apply_decision_lineage_outcome_relation_migration(connection)

        migrated_sql, migrated_rows, migrated_migrations = read_v2_state()
        self.assertIn("'records_outcome'", migrated_sql)
        self.assertEqual(migrated_rows, v2_rows)
        self.assertIn("decision_lineage_outcome_relation_v3", migrated_migrations)
        with closing(self.store._connect()) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='decision_lineage_events_v3'"
            ).fetchone())

    def test_v4_registry_migration_backfills_exact_resources_and_head(self) -> None:
        _artifact, supported = self.create_decision("support")
        portfolio = self.create_linked_portfolio(supported)
        confirmed_portfolio = self.store.confirm_paper_portfolio(
            ROOM_ID,
            portfolio["id"],
            expected_version=portfolio["version"],
            confirmed_by="user",
        )
        observation = self.store.create_observation(ROOM_ID, {
            "symbol": "MU",
            "direction": "UP",
            "horizon_days": 5,
            "threshold_pct": 2,
            "thesis": "Registry recovery must preserve the exact derivation chain.",
            "counter_case": "The recovered resource root or chain head is incorrect.",
            "evidence": {},
            "created_by": "user",
            "user_decision_id": supported["id"],
            "source_portfolio_id": confirmed_portfolio["id"],
            "source_portfolio_version": confirmed_portfolio["version"],
            "derivation_note": "Create the observation before rebuilding registry state.",
        })
        original_package = self.package_for(ROOM_ID, supported["id"])
        self.assertEqual(
            [event["relation_type"] for event in original_package["lineage"]],
            ["implements", "confirms", "tests"],
        )
        implements_event, _portfolio_confirm_event, tests_event = (
            original_package["lineage"]
        )

        with closing(self.store._connect()) as connection, connection:
            connection.execute("DROP TABLE decision_lineage_resources")
            connection.execute("DROP TABLE decision_lineage_heads")
            connection.execute(
                "DELETE FROM schema_migrations WHERE key=?",
                ("decision_lineage_registry_v4",),
            )
            self.store._apply_decision_lineage_registry_migration(connection)

        with closing(self.store._connect()) as connection:
            resource_rows = connection.execute(
                """SELECT room_id,resource_type,resource_id,user_decision_id,
                          creation_event_id,created_at
                   FROM decision_lineage_resources
                   WHERE room_id=? AND user_decision_id=?
                   ORDER BY resource_type,resource_id""",
                (ROOM_ID, supported["id"]),
            ).fetchall()
            resources = {
                (str(row["resource_type"]), str(row["resource_id"])): dict(row)
                for row in resource_rows
            }
            self.assertEqual(
                set(resources),
                {
                    (implements_event["resource_type"], portfolio["id"]),
                    (tests_event["resource_type"], observation["id"]),
                },
            )
            portfolio_registration = resources[
                (implements_event["resource_type"], portfolio["id"])
            ]
            self.assertEqual(
                portfolio_registration["creation_event_id"],
                implements_event["id"],
            )
            self.assertEqual(
                portfolio_registration["created_at"],
                implements_event["created_at"],
            )
            observation_registration = resources[
                (tests_event["resource_type"], observation["id"])
            ]
            self.assertEqual(
                observation_registration["creation_event_id"],
                tests_event["id"],
            )
            self.assertEqual(
                observation_registration["created_at"],
                tests_event["created_at"],
            )
            head = connection.execute(
                """SELECT user_decision_id,room_id,head_sequence,head_sha256
                   FROM decision_lineage_heads WHERE user_decision_id=?""",
                (supported["id"],),
            ).fetchone()
            self.assertIsNotNone(head)
            self.assertEqual(head["room_id"], ROOM_ID)
            self.assertEqual(head["head_sequence"], tests_event["sequence_no"])
            self.assertEqual(head["head_sha256"], tests_event["event_sha256"])
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM schema_migrations WHERE key=?",
                ("decision_lineage_registry_v4",),
            ).fetchone())

        recovered_package = self.package_for(ROOM_ID, supported["id"])
        self.assertTrue(recovered_package["integrity_ok"])
        self.assertEqual(recovered_package["state"], "active")
        confirmed_observation = self.store.confirm_observation(
            ROOM_ID,
            observation["id"],
            {},
        )
        self.assertEqual(confirmed_observation["status"], "PENDING_BASELINE")
        extended_package = self.package_for(ROOM_ID, supported["id"])
        appended_event = extended_package["lineage"][-1]
        self.assertEqual(appended_event["relation_type"], "confirms")
        self.assertEqual(appended_event["resource_id"], observation["id"])
        self.assertTrue(extended_package["integrity_ok"])
        with closing(self.store._connect()) as connection:
            updated_head = connection.execute(
                """SELECT head_sequence,head_sha256 FROM decision_lineage_heads
                   WHERE user_decision_id=?""",
                (supported["id"],),
            ).fetchone()
            self.assertEqual(updated_head["head_sequence"], appended_event["sequence_no"])
            self.assertEqual(updated_head["head_sha256"], appended_event["event_sha256"])

    def test_walk_forward_is_parallel_evaluation_of_exact_confirmed_portfolio(self) -> None:
        _artifact, supported = self.create_decision("support")
        portfolio = self.create_linked_portfolio(supported)
        with self.assertRaisesRegex(ValueError, "用户已确认"):
            PaperPortfolioService(
                self.store,
                FakeWalkForwardMarket(),
            ).walk_forward(
                ROOM_ID,
                portfolio["id"],
                DEFAULT_WALK_FORWARD_CONFIG,
                expected_portfolio_version=portfolio["version"],
            )
        self.assertEqual(
            self.store.list_paper_portfolio_walk_forward_runs(
                ROOM_ID,
                portfolio["id"],
            ),
            [],
        )

        confirmed = self.store.confirm_paper_portfolio(
            ROOM_ID,
            portfolio["id"],
            expected_version=portfolio["version"],
            confirmed_by="user",
        )
        run = PaperPortfolioService(
            self.store,
            FakeWalkForwardMarket(),
        ).walk_forward(
            ROOM_ID,
            portfolio["id"],
            DEFAULT_WALK_FORWARD_CONFIG,
            expected_portfolio_version=confirmed["version"],
        )

        package = self.package_for(ROOM_ID, supported["id"])
        event = package["lineage"][-1]
        self.assertEqual(event["relation_type"], "evaluates")
        self.assertEqual(event["resource_type"], "validation.walk_forward")
        self.assertEqual(event["resource_id"], run["id"])
        self.assertEqual(
            event["resource_snapshot"]["portfolio_version"],
            confirmed["version"],
        )

    def test_tampered_event_snapshot_marks_package_chain_broken(self) -> None:
        _artifact, supported = self.create_decision("support")
        self.create_linked_portfolio(supported)
        event = self.package_for(ROOM_ID, supported["id"])["lineage"][0]

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE decision_lineage_events SET resource_snapshot_json=? WHERE id=?",
                ('{"tampered":true}', event["id"]),
            )

        package = self.package_for(ROOM_ID, supported["id"])
        self.assertEqual(package["state"], "chain_broken")
        self.assertFalse(package["integrity_ok"])
        self.assertTrue(
            any("RESOURCE_SNAPSHOT_HASH_MISMATCH" in issue for issue in package["chain_issues"])
        )

    def test_tampered_event_hash_marks_package_chain_broken(self) -> None:
        _artifact, supported = self.create_decision("support")
        self.create_linked_portfolio(supported)
        event = self.package_for(ROOM_ID, supported["id"])["lineage"][0]

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE decision_lineage_events SET resource_snapshot_sha256=? WHERE id=?",
                ("0" * 64, event["id"]),
            )

        package = self.package_for(ROOM_ID, supported["id"])
        self.assertEqual(package["state"], "chain_broken")
        self.assertFalse(package["integrity_ok"])
        self.assertTrue(any("EVENT_HASH_MISMATCH" in issue for issue in package["chain_issues"]))

    def test_sensitive_lineage_snapshot_field_is_rejected_and_transaction_rolls_back(self) -> None:
        _artifact, supported = self.create_decision("support")
        unsafe_evaluation = passing_evaluation()
        unsafe_evaluation["api_key"] = "forbidden-test-marker"

        self.assert_no_new_portfolio(
            lambda: self.create_linked_portfolio(
                supported,
                name="Must roll back unsafe snapshot",
                evaluation=unsafe_evaluation,
            )
        )
        self.assertEqual(self.package_for(ROOM_ID, supported["id"])["lineage"], [])


if __name__ == "__main__":
    unittest.main()
