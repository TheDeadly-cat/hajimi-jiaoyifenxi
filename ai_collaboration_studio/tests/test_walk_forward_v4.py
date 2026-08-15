from __future__ import annotations

import copy
import unittest

from backend.walk_forward import (
    CONFIG_VERSION_V2,
    CONFIG_VERSION_V3,
    ENGINE_VERSION,
    ENGINE_VERSION_V2,
    ENGINE_VERSION_V3,
    ENGINE_VERSION_V4,
    INPUT_SNAPSHOT_VERSION,
    INPUT_SNAPSHOT_VERSION_V1,
    INPUT_SNAPSHOT_VERSION_V3,
    PLAN_VERSION_V2,
    RESULT_VERSION,
    RESULT_VERSION_V2,
    RESULT_VERSION_V3,
    RESULT_VERSION_V4,
    RULE_ID,
    STRATEGY_RULE_CONTRACT_VERSION,
    WalkForwardValidationError,
    build_strategy_rule_contract,
    normalize_walk_forward_config,
    normalize_walk_forward_plan,
    run_walk_forward_backtest,
)
from backend.walk_forward_friction import (
    STORAGE_FRICTION_SCENARIOS_VERSION,
    UNFILLABLE_POLICY,
)
from tests.test_walk_forward import history_set, plan as legacy_plan
from tests.test_walk_forward_v3 import config_v2, with_market_fields


def plan_v2(
    histories: dict[str, dict],
    *positions: tuple[str, str, float],
    **overrides,
) -> dict:
    result = legacy_plan(histories, *positions)
    result.update(
        {
            "version": PLAN_VERSION_V2,
            "mode": "fold_train_only_next_session_test_replay",
            "strategy_provenance": "server_whitelisted_fold_trained_rule",
            "out_of_sample_claim": False,
            "future_performance_claim": False,
            "retrospective_dataset": True,
            "source_user_decision_id": "decision_walk_forward_v4_fixture",
            "decision_anchor_sha256": "a" * 64,
            "source_decision_head_sequence": 7,
            "source_decision_head_sha256": "b" * 64,
        }
    )
    result.update(overrides)
    return result


def config_v3(
    paper_plan: dict,
    *,
    train_days: int = 20,
    test_days: int = 4,
    step_days: int = 4,
) -> dict:
    return {
        "version": CONFIG_VERSION_V3,
        "train_days": train_days,
        "test_days": test_days,
        "step_days": step_days,
        "price_adjustment": "QFQ",
        "friction_scenario_set": STORAGE_FRICTION_SCENARIOS_VERSION,
        "unfillable_policy": UNFILLABLE_POLICY,
        "strategy_rule_contract": build_strategy_rule_contract(
            paper_plan,
            RULE_ID,
        ),
    }


class WalkForwardV4Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.histories = with_market_fields(
            history_set(days=110),
            turnover=1_000_000_000_000,
        )
        self.plan = plan_v2(
            self.histories,
            ("US.MU", "LONG", 60),
            ("US.WDC", "SHORT", 40),
        )
        self.config = config_v3(self.plan)

    def test_contract_is_strict_server_whitelisted_and_source_derived(self) -> None:
        contract = build_strategy_rule_contract(self.plan, RULE_ID)
        self.assertEqual(
            set(contract),
            {
                "version",
                "rule_id",
                "universe",
                "signal",
                "fit_scope",
                "ranking",
                "long_count",
                "short_count",
                "long_budget_pct",
                "short_budget_pct",
                "weighting",
                "rebalance",
                "execution_lag_trading_days",
                "test_data_excluded_from_fit",
                "contract_selected_before_full_history",
                "out_of_sample_claim",
                "future_performance_claim",
                "partial_fills_allowed",
                "position_shrinking_allowed",
                "date_shifting_allowed",
                "source_portfolio_id",
                "source_portfolio_version",
                "source_positions_sha256",
            },
        )
        self.assertEqual(contract["version"], STRATEGY_RULE_CONTRACT_VERSION)
        self.assertEqual(contract["rule_id"], RULE_ID)
        self.assertEqual(contract["long_count"], 1)
        self.assertEqual(contract["short_count"], 1)
        self.assertEqual(contract["long_budget_pct"], 60)
        self.assertEqual(contract["short_budget_pct"], 40)
        self.assertEqual(len(contract["source_positions_sha256"]), 64)
        normalized_plan = normalize_walk_forward_plan(self.plan)
        self.assertEqual(normalized_plan["version"], PLAN_VERSION_V2)
        self.assertEqual(len(normalized_plan["positions"]), 4)
        self.assertEqual(normalize_walk_forward_config(self.config), self.config)

        with self.assertRaisesRegex(WalkForwardValidationError, RULE_ID):
            build_strategy_rule_contract(self.plan, "client_custom_rule")
        unknown = copy.deepcopy(self.config)
        unknown["strategy_rule_contract"]["lookahead"] = True
        with self.assertRaisesRegex(WalkForwardValidationError, "unknown fields"):
            normalize_walk_forward_config(unknown)
        tampered = copy.deepcopy(self.config)
        tampered["strategy_rule_contract"]["source_positions_sha256"] = "0" * 64
        with self.assertRaisesRegex(WalkForwardValidationError, "does not match"):
            run_walk_forward_backtest(self.histories, self.plan, tampered)

        bad_anchor = dict(self.plan, decision_anchor_sha256="A" * 64)
        with self.assertRaisesRegex(WalkForwardValidationError, "lowercase SHA-256"):
            normalize_walk_forward_plan(bad_anchor)

    def test_deterministic_fold_fit_ranking_and_equal_side_budgets(self) -> None:
        first = run_walk_forward_backtest(
            self.histories,
            self.plan,
            self.config,
        )
        reversed_plan = dict(
            self.plan,
            positions=list(reversed(self.plan["positions"])),
        )
        second = run_walk_forward_backtest(
            dict(reversed(list(self.histories.items()))),
            reversed_plan,
            config_v3(reversed_plan),
        )
        self.assertEqual(first, second)
        self.assertEqual(first["version"], RESULT_VERSION_V4)
        self.assertEqual(first["engine_version"], ENGINE_VERSION_V4)
        self.assertEqual(
            first["input_snapshot_version"],
            INPUT_SNAPSHOT_VERSION_V3,
        )
        self.assertNotEqual(
            first["strategy_contract_sha256"],
            first["config"]["strategy_rule_contract"][
                "source_positions_sha256"
            ],
        )

        decision = first["folds"][0]["strategy_decision"]
        self.assertEqual(
            decision["ranking"],
            ["US.MU", "US.SNDK", "US.STX", "US.WDC"],
        )
        self.assertEqual(
            decision["selected_positions"],
            [
                {"symbol": "US.MU", "side": "LONG", "weight_pct": 60.0},
                {"symbol": "US.WDC", "side": "SHORT", "weight_pct": 40.0},
            ],
        )
        self.assertEqual(decision["weights_pct"]["US.MU"], 60)
        self.assertEqual(decision["weights_pct"]["US.WDC"], -40)
        self.assertTrue(decision["test_data_excluded_from_fit"])
        self.assertFalse(decision["source_positions_directly_replayed"])

    def test_test_rows_do_not_change_earlier_fit_but_training_rows_do(self) -> None:
        original = run_walk_forward_backtest(
            self.histories,
            self.plan,
            self.config,
        )["folds"][0]

        changed_test = copy.deepcopy(self.histories)
        changed_test["US.MU"]["rows"][21]["close"] *= 1.4
        test_result = run_walk_forward_backtest(
            changed_test,
            self.plan,
            self.config,
        )["folds"][0]
        self.assertEqual(
            original["decision_input_hash"],
            test_result["decision_input_hash"],
        )
        self.assertEqual(
            original["strategy_decision"],
            test_result["strategy_decision"],
        )
        self.assertNotEqual(original["input_hash"], test_result["input_hash"])

        changed_train = copy.deepcopy(self.histories)
        changed_train["US.MU"]["rows"][10]["close"] *= 1.4
        train_result = run_walk_forward_backtest(
            changed_train,
            self.plan,
            self.config,
        )["folds"][0]
        self.assertNotEqual(
            original["decision_input_hash"],
            train_result["decision_input_hash"],
        )

    def test_next_session_boundary_and_scenario_hash_separation(self) -> None:
        result = run_walk_forward_backtest(
            self.histories,
            self.plan,
            self.config,
        )
        baseline, stressed, severe = result["scenario_results"]
        self.assertEqual(
            [item["scenario_id"] for item in result["scenario_results"]],
            ["baseline", "stressed", "severe"],
        )
        baseline_fold = baseline["folds"][0]
        self.assertEqual(baseline_fold["train_end"], "2026-01-30")
        self.assertEqual(baseline_fold["test_start"], "2026-02-02")
        self.assertEqual(baseline_fold["entry_price_date"], "2026-02-02")
        self.assertEqual(baseline_fold["first_return_date"], "2026-02-03")
        self.assertTrue(baseline_fold["test_start_is_next_trading_session"])

        folds = [item["folds"][0] for item in (baseline, stressed, severe)]
        self.assertEqual(len({fold["decision_input_hash"] for fold in folds}), 1)
        self.assertEqual(len({fold["strategy_decision_sha256"] for fold in folds}), 1)
        self.assertEqual(len({fold["input_hash"] for fold in folds}), 3)
        self.assertEqual(
            [fold["scenario_id"] for fold in folds],
            ["baseline", "stressed", "severe"],
        )

    def test_honest_claims_zero_model_zero_execution_and_anchor_binding(self) -> None:
        result = run_walk_forward_backtest(
            self.histories,
            self.plan,
            self.config,
        )
        self.assertFalse(result["out_of_sample"])
        self.assertFalse(result["out_of_sample_claim"])
        self.assertFalse(result["future_performance_claim"])
        self.assertTrue(result["retrospective_dataset"])
        self.assertTrue(result["prospective_test_protocol"])
        self.assertTrue(result["test_data_excluded_from_fold_fit"])
        self.assertEqual(result["model_calls_total"], 0)
        self.assertEqual(result["provider_calls_total"], 0)
        self.assertEqual(result["openai_calls"], 0)
        self.assertEqual(result["execution_capability"], "none")
        self.assertFalse(result["live_trading_allowed"])
        self.assertFalse(result["actual_execution"])
        self.assertFalse(result["method"]["source_positions_directly_replayed"])
        self.assertFalse(result["friction_model"]["partial_fills_allowed"])
        self.assertNotIn("win_rate", result["summary"])
        self.assertIn("not out-of-sample", result["interpretation"])
        self.assertEqual(
            result["source_user_decision_id"],
            self.plan["source_user_decision_id"],
        )
        self.assertEqual(
            result["source_decision_head_sequence"],
            self.plan["source_decision_head_sequence"],
        )

        changed_anchor_plan = dict(self.plan, decision_anchor_sha256="c" * 64)
        changed = run_walk_forward_backtest(
            self.histories,
            changed_anchor_plan,
            config_v3(changed_anchor_plan),
        )
        self.assertNotEqual(result["input_hash"], changed["input_hash"])
        self.assertNotEqual(
            result["folds"][0]["decision_input_hash"],
            changed["folds"][0]["decision_input_hash"],
        )
        self.assertEqual(
            result["folds"][0]["strategy_decision"]["fit_input_hash"],
            changed["folds"][0]["strategy_decision"]["fit_input_hash"],
        )

    def test_current_aliases_and_v3_dispatch_remain_unchanged(self) -> None:
        self.assertEqual(ENGINE_VERSION, ENGINE_VERSION_V2)
        self.assertEqual(RESULT_VERSION, RESULT_VERSION_V2)
        self.assertEqual(INPUT_SNAPSHOT_VERSION, INPUT_SNAPSHOT_VERSION_V1)

        histories = with_market_fields(
            history_set(days=110),
            turnover=100_000_000,
        )
        old_plan = legacy_plan(histories, ("US.MU", "LONG", 100))
        old_result = run_walk_forward_backtest(
            histories,
            old_plan,
            config_v2(),
        )
        self.assertEqual(CONFIG_VERSION_V2, old_result["config"]["version"])
        self.assertEqual(old_result["version"], RESULT_VERSION_V3)
        self.assertEqual(old_result["engine_version"], ENGINE_VERSION_V3)
        self.assertNotIn("strategy_rule_contract", old_result)


if __name__ == "__main__":
    unittest.main()
