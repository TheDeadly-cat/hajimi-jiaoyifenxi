from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.decision_lineage import artifact_binding_payload, canonical_sha256
from backend.paper_portfolio_service import PaperPortfolioService
from backend.store import (
    WALK_FORWARD_DECISION_BINDING_VERSION_V1,
    StudioStore,
)
from backend.user_decision import USER_DECISION_VERSION_V1
from backend.walk_forward import CONFIG_VERSION_V3, RULE_ID
from tests.test_walk_forward_integration import (
    FakeWalkForwardMarket,
    evaluation,
    paper_plan,
)


ROOM_ID = "room_storage"


class LegacyWalkForwardDecisionBindingTests(unittest.TestCase):
    def _create_legacy_actionable_portfolio(
        self,
        store: StudioStore,
    ) -> tuple[dict[str, object], str]:
        message = store.add_message(
            ROOM_ID,
            sender_type="user",
            sender_name="User",
            content="Confirm a reversible legacy paper-only storage plan.",
        )
        evidence = [{
            "type": "message",
            "id": message["id"],
            "evidence_role": "support",
            "verification_status": "source_checked",
            "review_note": "",
        }]
        artifact = store.create_artifact(
            ROOM_ID,
            title="Legacy walk-forward candidate",
            content={
                "summary": "Retain an already-confirmed v1 decision for replay.",
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
                            "description": "Use bounded paper weights only.",
                            "benefits": ["Reversible"],
                            "risks": ["Historical proxy"],
                            "evidence": evidence,
                        },
                        {
                            "id": "paper_flat",
                            "title": "Remain flat",
                            "description": "Keep all paper weights at zero.",
                            "benefits": ["No exposure"],
                            "risks": ["No comparison"],
                            "evidence": evidence,
                        },
                    ],
                    "preferred_option_id": "paper_small",
                    "rationale": "The bounded paper plan is easier to invalidate.",
                    "evidence": evidence,
                },
            },
            created_by="legacy_import",
        )
        confirmed = store.confirm_artifact(
            ROOM_ID,
            artifact["id"],
            expected_version=artifact["version"],
            confirmed_by="legacy_user",
        )
        decision_id = "user_decision_legacy_walk_forward_v1"
        with closing(store._connect()) as connection, connection:
            version_row = connection.execute(
                """SELECT snapshot_json FROM artifact_versions
                   WHERE room_id=? AND artifact_id=? AND version=?""",
                (ROOM_ID, confirmed["id"], confirmed["version"]),
            ).fetchone()
            self.assertIsNotNone(version_row)
            snapshot = json.loads(version_row["snapshot_json"])
            connection.execute(
                """INSERT INTO artifact_user_decisions(
                       id,room_id,artifact_id,artifact_version,action,rationale,
                       preferred_option_id,decision_version,
                       artifact_snapshot_sha256,created_by,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    ROOM_ID,
                    confirmed["id"],
                    confirmed["version"],
                    "support",
                    "Preserve the historical AI-preferred paper candidate.",
                    "paper_small",
                    USER_DECISION_VERSION_V1,
                    canonical_sha256(artifact_binding_payload(snapshot)),
                    "legacy_user",
                    int(confirmed["updated_at"]) + 1,
                ),
            )

        portfolio = store.create_paper_portfolio(
            ROOM_ID,
            paper_plan(),
            evaluation(),
            created_by="legacy_user",
            user_decision_id=decision_id,
            derivation_note="Bind the imported v1 support decision to paper research.",
        )
        confirmed_portfolio = store.confirm_paper_portfolio(
            ROOM_ID,
            portfolio["id"],
            expected_version=portfolio["version"],
            confirmed_by="legacy_user",
        )
        return confirmed_portfolio, decision_id

    @staticmethod
    def _stored_decision_binding(
        store: StudioStore,
        run_id: str,
    ) -> dict[str, object]:
        with closing(store._connect()) as connection:
            row = connection.execute(
                """SELECT input_snapshot_json
                   FROM paper_portfolio_walk_forward_runs WHERE id=?""",
                (run_id,),
            ).fetchone()
        snapshot = json.loads(row["input_snapshot_json"])
        return snapshot["decision_binding"]

    def test_legacy_v1_capture_write_and_list_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = StudioStore(Path(temp_dir) / "legacy-walk-forward.sqlite3")
            portfolio, decision_id = self._create_legacy_actionable_portfolio(store)

            captured = store.capture_paper_portfolio_walk_forward_decision_binding(
                ROOM_ID,
                portfolio["id"],
                expected_portfolio_version=portfolio["version"],
            )
            capture_binding = captured["decision_binding"]
            self.assertEqual(
                capture_binding["version"],
                WALK_FORWARD_DECISION_BINDING_VERSION_V1,
            )
            self.assertEqual(capture_binding["user_decision_id"], decision_id)
            self.assertEqual(capture_binding["preferred_option_id"], "paper_small")
            self.assertEqual(capture_binding["selected_option"]["id"], "paper_small")
            self.assertNotIn("decision_version", capture_binding)
            self.assertNotIn("selected_option_id", capture_binding)

            run = PaperPortfolioService(
                store,
                FakeWalkForwardMarket(),
            ).walk_forward(
                ROOM_ID,
                portfolio["id"],
                {
                    "version": CONFIG_VERSION_V3,
                    "strategy_rule_id": RULE_ID,
                },
                expected_portfolio_version=portfolio["version"],
            )
            written_binding = self._stored_decision_binding(store, run["id"])
            self.assertEqual(written_binding, capture_binding)
            self.assertEqual(
                written_binding["version"],
                WALK_FORWARD_DECISION_BINDING_VERSION_V1,
            )
            self.assertEqual(written_binding["preferred_option_id"], "paper_small")
            self.assertEqual(written_binding["selected_option"]["id"], "paper_small")
            self.assertTrue(run["decision_binding_verified"])
            self.assertTrue(run["lineage_binding_verified"])
            self.assertTrue(run["fully_verified"])

            listed = store.list_paper_portfolio_walk_forward_runs(
                ROOM_ID,
                portfolio["id"],
            )
            self.assertEqual([item["id"] for item in listed], [run["id"]])
            self.assertTrue(listed[0]["decision_binding_verified"])
            self.assertTrue(listed[0]["lineage_binding_verified"])
            self.assertTrue(listed[0]["fully_verified"])
            listed_binding = self._stored_decision_binding(store, listed[0]["id"])
            self.assertEqual(listed_binding, capture_binding)
            self.assertEqual(
                listed_binding["version"],
                WALK_FORWARD_DECISION_BINDING_VERSION_V1,
            )
            self.assertEqual(listed_binding["preferred_option_id"], "paper_small")
            self.assertEqual(listed_binding["selected_option"]["id"], "paper_small")


if __name__ == "__main__":
    unittest.main()
