from __future__ import annotations

import copy
import math
import unittest

from backend.walk_forward import (
    CONFIG_VERSION_V1,
    CONFIG_VERSION_V2,
    ENGINE_VERSION,
    ENGINE_VERSION_V2,
    ENGINE_VERSION_V3,
    INPUT_SNAPSHOT_VERSION,
    INPUT_SNAPSHOT_VERSION_V1,
    INPUT_SNAPSHOT_VERSION_V2,
    RESULT_VERSION,
    RESULT_VERSION_V2,
    RESULT_VERSION_V3,
    WalkForwardValidationError,
    normalize_walk_forward_config,
    run_walk_forward_backtest,
)
from backend.walk_forward_friction import (
    PAPER_FRICTION_MODEL_VERSION,
    PAPER_LIQUIDITY_PROXY_VERSION,
    STORAGE_FRICTION_SCENARIOS_VERSION,
    UNFILLABLE_POLICY,
)
from tests.test_walk_forward import config as config_v1
from tests.test_walk_forward import history_set, plan


def config_v2(
    *,
    train_days: int = 20,
    test_days: int = 4,
    step_days: int = 4,
) -> dict:
    return {
        "version": CONFIG_VERSION_V2,
        "train_days": train_days,
        "test_days": test_days,
        "step_days": step_days,
        "price_adjustment": "QFQ",
        "friction_scenario_set": STORAGE_FRICTION_SCENARIOS_VERSION,
        "unfillable_policy": UNFILLABLE_POLICY,
    }


def with_market_fields(
    histories: dict[str, dict],
    *,
    turnover: float | None,
    volume: float | None = 1_000_000,
) -> dict[str, dict]:
    result = copy.deepcopy(histories)
    for history in result.values():
        for row in history["rows"]:
            close = float(row["close"])
            row.update(
                {
                    "open": close,
                    "high": close,
                    "low": close,
                    "volume": volume,
                    "turnover": turnover,
                }
            )
    return result


class WalkForwardV3Tests(unittest.TestCase):
    def test_config_versions_are_strict_and_current_aliases_stay_v2(self) -> None:
        old = config_v1(
            train_days=20,
            test_days=4,
            step_days=4,
            transaction_cost_bps=12.5,
        )
        self.assertEqual(normalize_walk_forward_config(old), old)
        new = config_v2()
        self.assertEqual(normalize_walk_forward_config(new), new)

        legacy_cost_in_v2 = dict(new, transaction_cost_bps=10)
        with self.assertRaisesRegex(
            WalkForwardValidationError,
            "transaction_cost_bps",
        ):
            normalize_walk_forward_config(legacy_cost_in_v2)

        custom_scenarios = dict(new, friction_scenario_set="custom")
        with self.assertRaisesRegex(
            WalkForwardValidationError,
            STORAGE_FRICTION_SCENARIOS_VERSION,
        ):
            normalize_walk_forward_config(custom_scenarios)

        self.assertEqual(ENGINE_VERSION, ENGINE_VERSION_V2)
        self.assertEqual(RESULT_VERSION, RESULT_VERSION_V2)
        self.assertEqual(INPUT_SNAPSHOT_VERSION, INPUT_SNAPSHOT_VERSION_V1)

    def test_v3_is_deterministic_and_projects_baseline_in_fixed_order(self) -> None:
        histories = with_market_fields(history_set(days=110), turnover=100_000_000)
        paper_plan = plan(
            histories,
            ("US.MU", "LONG", 100),
        )
        first = run_walk_forward_backtest(histories, paper_plan, config_v2())
        second = run_walk_forward_backtest(
            dict(reversed(list(histories.items()))),
            paper_plan,
            config_v2(),
        )

        self.assertEqual(first, second)
        self.assertEqual(first["version"], RESULT_VERSION_V3)
        self.assertEqual(first["engine_version"], ENGINE_VERSION_V3)
        self.assertEqual(
            first["input_snapshot_version"],
            INPUT_SNAPSHOT_VERSION_V2,
        )
        self.assertEqual(
            [item["scenario_id"] for item in first["scenario_results"]],
            ["baseline", "stressed", "severe"],
        )
        baseline, stressed, severe = first["scenario_results"]
        self.assertEqual(baseline["state"], "sufficient")
        self.assertFalse(baseline["blocked"])
        self.assertEqual(stressed["state"], "blocked")
        self.assertTrue(stressed["blocked"])
        self.assertEqual(severe["state"], "blocked")
        self.assertEqual(first["folds"], baseline["folds"])
        self.assertEqual(first["summary"], baseline["summary"])
        self.assertEqual(first["state"], baseline["state"])
        self.assertEqual(
            first["friction_model"]["version"],
            PAPER_FRICTION_MODEL_VERSION,
        )
        self.assertEqual(
            first["friction_model"]["liquidity_proxy_version"],
            PAPER_LIQUIDITY_PROXY_VERSION,
        )
        self.assertEqual(
            baseline["assumptions"]["paper_reference_notional_usd"],
            1_000_000,
        )
        self.assertEqual(
            stressed["assumptions"]["paper_reference_notional_usd"],
            5_000_000,
        )
        self.assertGreater(stressed["formal_unfillable_fold_count"], 0)
        self.assertIsNotNone(stressed["first_blocker"])
        self.assertGreater(stressed["capacity_gap_usd"], 0)
        self.assertEqual(
            stressed["first_blocker"]["reason_code"],
            "CAPACITY_EXCEEDED",
        )
        self.assertEqual(first["provider_calls_total"], 0)
        self.assertEqual(first["openai_calls"], 0)
        self.assertEqual(first["execution_capability"], "none")
        self.assertFalse(first["live_trading_allowed"])
        self.assertFalse(first["can_autonomously_decide"])
        self.assertFalse(first["out_of_sample_claim"])

    def test_blocked_scenario_hides_portfolio_metrics_but_not_benchmark(self) -> None:
        histories = with_market_fields(history_set(days=110), turnover=100_000_000)
        result = run_walk_forward_backtest(
            histories,
            plan(histories, ("US.MU", "LONG", 100)),
            config_v2(),
        )
        baseline, stressed, _severe = result["scenario_results"]
        summary = stressed["summary"]
        for field in (
            "mean_return_pct",
            "median_return_pct",
            "worst_return_pct",
            "max_drawdown_pct",
            "positive_fold_rate",
            "historical_positive_fold_ratio",
            "portfolio_cumulative_return_pct",
        ):
            self.assertIsNone(summary[field], field)
        self.assertIsNotNone(summary["benchmark_cumulative_return_pct"])
        blocked_fold = next(fold for fold in stressed["folds"] if fold["blocked"])
        self.assertIsNone(blocked_fold["net_return_pct"])
        self.assertIsNone(blocked_fold["max_drawdown_pct"])
        self.assertIsNone(blocked_fold["positive"])
        self.assertIsNotNone(blocked_fold["benchmark_equal_weight_return_pct"])
        self.assertEqual(
            blocked_fold["benchmark_equal_weight_return_pct"],
            baseline["folds"][blocked_fold["fold_index"] - 1][
                "benchmark_equal_weight_return_pct"
            ],
        )
        for fold in stressed["folds"]:
            self.assertTrue(fold["returns_hidden_due_to_scenario_block"])
            self.assertIsNone(fold["paper_equity_path"])
            for field in (
                "gross_return_pct",
                "net_return_before_slippage_pct",
                "net_return_pct",
                "max_drawdown_pct",
                "positive",
            ):
                self.assertIsNone(fold[field], field)

    def test_one_formal_capacity_failure_redacts_every_fold_return(self) -> None:
        histories = with_market_fields(
            history_set(days=110),
            turnover=1_000_000_000_000,
        )
        histories["US.MU"]["rows"][20]["turnover"] = 1
        result = run_walk_forward_backtest(
            histories,
            plan(histories, ("US.MU", "LONG", 100)),
            config_v2(),
        )
        baseline = result["scenario_results"][0]

        self.assertEqual(baseline["state"], "blocked")
        self.assertEqual(baseline["formal_unfillable_fold_count"], 1)
        self.assertTrue(any(not fold["blocked"] for fold in baseline["folds"]))
        for fold in baseline["folds"]:
            self.assertTrue(fold["returns_hidden_due_to_scenario_block"])
            self.assertIsNone(fold["net_return_pct"])
            self.assertIsNone(fold["max_drawdown_pct"])
            self.assertIsNone(fold["positive"])
        self.assertEqual(result["folds"], baseline["folds"])

    def test_flat_prices_charge_commission_and_slippage_without_benchmark_cost(self) -> None:
        histories = with_market_fields(
            history_set(days=110, daily_returns=(0, 0, 0, 0)),
            turnover=1_000_000_000_000,
        )
        result = run_walk_forward_backtest(
            histories,
            plan(histories, ("US.MU", "LONG", 100)),
            config_v2(),
        )
        fold = result["scenario_results"][0]["folds"][0]

        self.assertEqual(fold["gross_return_pct"], 0)
        self.assertEqual(fold["entry_cost_pct"], 0.15)
        self.assertEqual(fold["exit_cost_pct"], 0.15)
        self.assertEqual(fold["commission_cost_pct"], 0.2)
        self.assertEqual(fold["slippage_cost_pct"], 0.1)
        self.assertEqual(fold["short_borrow_cost_pct"], 0)
        self.assertEqual(fold["total_friction_cost_pct"], 0.3)
        self.assertEqual(fold["net_return_pct"], -0.3)
        self.assertEqual(fold["benchmark_equal_weight_return_pct"], 0)
        self.assertFalse(result["method"]["benchmark_friction_applied"])

    def test_short_borrow_uses_actual_weekend_calendar_days_in_v3(self) -> None:
        histories = with_market_fields(
            history_set(days=30, daily_returns=(0, 0, 0, 0)),
            turnover=1_000_000_000_000,
        )
        result = run_walk_forward_backtest(
            histories,
            plan(histories, ("US.MU", "SHORT", 100)),
            config_v2(train_days=4, test_days=1, step_days=1),
        )
        fold = result["scenario_results"][0]["folds"][0]

        self.assertEqual(fold["entry_price_date"], "2026-01-09")
        self.assertEqual(fold["exit_date"], "2026-01-12")
        expected_borrow = 1_000_000 * 0.03 * 3 / 365
        self.assertAlmostEqual(
            fold["friction_costs"]["short_borrow_cost_usd"],
            expected_borrow,
            places=8,
        )
        self.assertAlmostEqual(
            fold["short_borrow_cost_pct"],
            expected_borrow / 1_000_000 * 100,
            places=8,
        )

    def test_turnover_is_hash_bound_without_leaking_future_into_decision(self) -> None:
        histories = with_market_fields(history_set(days=110), turnover=1_000_000_000)
        paper_plan = plan(histories, ("US.MU", "LONG", 100))
        settings = config_v2()
        baseline = run_walk_forward_backtest(histories, paper_plan, settings)

        changed = copy.deepcopy(histories)
        changed["US.MU"]["rows"][21]["turnover"] *= 2
        modified = run_walk_forward_backtest(changed, paper_plan, settings)

        self.assertEqual(
            baseline["folds"][0]["decision_input_hash"],
            modified["folds"][0]["decision_input_hash"],
        )
        self.assertNotEqual(
            baseline["folds"][0]["input_hash"],
            modified["folds"][0]["input_hash"],
        )
        self.assertNotEqual(baseline["input_hash"], modified["input_hash"])

        far_future = copy.deepcopy(histories)
        far_future["US.MU"]["rows"][-1]["turnover"] *= 3
        future = run_walk_forward_backtest(far_future, paper_plan, settings)
        self.assertEqual(
            baseline["folds"][0]["decision_input_hash"],
            future["folds"][0]["decision_input_hash"],
        )
        self.assertEqual(
            baseline["folds"][0]["input_hash"],
            future["folds"][0]["input_hash"],
        )
        self.assertNotEqual(baseline["input_hash"], future["input_hash"])

    def test_v3_preserves_optional_ohlc_and_nonpositive_liquidity_semantics(self) -> None:
        histories = history_set(days=110)
        histories["US.MU"]["rows"][0]["volume"] = 0
        histories["US.MU"]["rows"][0]["turnover"] = -1
        result = run_walk_forward_backtest(
            histories,
            plan(histories, ("US.MU", "LONG", 100)),
            config_v2(),
        )
        self.assertEqual(result["scenario_results"][0]["state"], "blocked")
        blocker = result["scenario_results"][0]["first_blocker"]
        self.assertEqual(blocker["reason_code"], "LIQUIDITY_PROXY_UNAVAILABLE")

        bad_ohlc = with_market_fields(history_set(days=110), turnover=1_000_000_000)
        bad_ohlc["US.MU"]["rows"][0]["high"] = math.inf
        with self.assertRaisesRegex(WalkForwardValidationError, "high"):
            run_walk_forward_backtest(
                bad_ohlc,
                plan(bad_ohlc, ("US.MU", "LONG", 100)),
                config_v2(),
            )

    def test_v2_hashes_and_result_contract_remain_byte_for_byte_stable(self) -> None:
        histories = history_set(days=110)
        result = run_walk_forward_backtest(
            histories,
            plan(
                histories,
                ("US.MU", "LONG", 30),
                ("US.SNDK", "LONG", 20),
                ("US.WDC", "SHORT", 15),
                ("US.STX", "FLAT", 0),
            ),
            config_v1(train_days=20, test_days=4, step_days=4),
        )

        self.assertEqual(result["version"], RESULT_VERSION_V2)
        self.assertEqual(result["engine_version"], ENGINE_VERSION_V2)
        self.assertNotIn("scenario_results", result)
        self.assertEqual(
            result["input_hash"],
            "017d61d6a0d3f90e964a740a5bc87e64131c4da9ff980dfc3d2f4020fda86df1",
        )
        self.assertEqual(
            result["folds"][0]["decision_input_hash"],
            "a796668587c9344716c87acb5da48af02197a4f195e1f8b32f979c8b0b8ad233",
        )
        self.assertEqual(
            result["folds"][0]["input_hash"],
            "fbd060276490cc3df4555d7f887e2f5579724778b24f1e6f77a7224bd80572c2",
        )


if __name__ == "__main__":
    unittest.main()
