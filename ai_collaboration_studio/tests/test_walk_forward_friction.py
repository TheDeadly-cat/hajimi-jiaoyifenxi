from __future__ import annotations

import copy
import unittest

from backend.walk_forward_friction import (
    PAPER_FRICTION_MODEL_VERSION,
    PAPER_LIQUIDITY_PROXY_VERSION,
    STORAGE_FRICTION_SCENARIOS_VERSION,
    UNFILLABLE_POLICY,
    WALK_FORWARD_CONFIG_VERSION,
    WALK_FORWARD_ENGINE_VERSION,
    WALK_FORWARD_INPUT_SNAPSHOT_VERSION,
    WALK_FORWARD_RESULT_VERSION,
    PaperFrictionValidationError,
    apply_paper_friction,
    get_storage_friction_scenarios,
)


def position(
    side: str = "LONG",
    weight_pct: float = 10.0,
    symbol: str = "US.MU",
) -> list[dict]:
    return [
        {
            "symbol": symbol,
            "side": side,
            "weight_pct": weight_pct,
        }
    ]


def history(
    closes: tuple[float, ...] = (100.0, 100.0),
    *,
    dates: tuple[str, ...] | None = None,
    turnovers: tuple[float | None, ...] | None = None,
    volumes: tuple[float | None, ...] | None = None,
    symbol: str = "US.MU",
) -> dict[str, list[dict]]:
    if dates is None:
        dates = tuple(f"2026-01-{5 + index:02d}" for index in range(len(closes)))
    if turnovers is None:
        turnovers = tuple(1_000_000_000.0 for _ in closes)
    if volumes is None:
        volumes = tuple(10_000_000.0 for _ in closes)
    rows = []
    for session, close, turnover, volume in zip(
        dates,
        closes,
        turnovers,
        volumes,
    ):
        rows.append(
            {
                "date": session,
                "close": close,
                "turnover": turnover,
                "volume": volume,
            }
        )
    return {symbol: rows}


class StorageFrictionScenarioTests(unittest.TestCase):
    def test_versions_and_exact_server_owned_scenarios(self) -> None:
        self.assertEqual(WALK_FORWARD_ENGINE_VERSION, "walk_forward_engine_v3")
        self.assertEqual(WALK_FORWARD_CONFIG_VERSION, "walk_forward_config_v2")
        self.assertEqual(WALK_FORWARD_RESULT_VERSION, "walk_forward_result_v3")
        self.assertEqual(
            WALK_FORWARD_INPUT_SNAPSHOT_VERSION,
            "walk_forward_input_snapshot_v2",
        )
        self.assertEqual(PAPER_FRICTION_MODEL_VERSION, "paper_friction_model_v1")
        self.assertEqual(PAPER_LIQUIDITY_PROXY_VERSION, "paper_liquidity_proxy_v1")
        self.assertEqual(
            STORAGE_FRICTION_SCENARIOS_VERSION,
            "storage_friction_scenarios_v1",
        )
        self.assertEqual(UNFILLABLE_POLICY, "block_scenario_no_partial_fill")

        scenario_set = get_storage_friction_scenarios()
        self.assertEqual(
            scenario_set["version"],
            STORAGE_FRICTION_SCENARIOS_VERSION,
        )
        self.assertTrue(scenario_set["server_owned"])
        self.assertFalse(scenario_set["custom_overrides_allowed"])
        self.assertFalse(scenario_set["live_broker_rates"])
        self.assertEqual(
            scenario_set["scenarios"],
            [
                {
                    "scenario_id": "baseline",
                    "label": "基准摩擦",
                    "paper_reference_notional_usd": 1_000_000.0,
                    "commission_bps_per_side": 10.0,
                    "entry_slippage_bps": 5.0,
                    "exit_slippage_bps": 5.0,
                    "short_borrow_fee_bps_annual": 300.0,
                    "max_daily_turnover_participation_pct": 2.0,
                },
                {
                    "scenario_id": "stressed",
                    "label": "压力摩擦",
                    "paper_reference_notional_usd": 5_000_000.0,
                    "commission_bps_per_side": 15.0,
                    "entry_slippage_bps": 25.0,
                    "exit_slippage_bps": 25.0,
                    "short_borrow_fee_bps_annual": 1_500.0,
                    "max_daily_turnover_participation_pct": 1.0,
                },
                {
                    "scenario_id": "severe",
                    "label": "极端摩擦",
                    "paper_reference_notional_usd": 10_000_000.0,
                    "commission_bps_per_side": 25.0,
                    "entry_slippage_bps": 75.0,
                    "exit_slippage_bps": 75.0,
                    "short_borrow_fee_bps_annual": 3_000.0,
                    "max_daily_turnover_participation_pct": 0.25,
                },
            ],
        )

    def test_returned_scenarios_and_results_cannot_override_canonical_values(self) -> None:
        first = get_storage_friction_scenarios()
        first["scenarios"][0]["paper_reference_notional_usd"] = 1.0
        first["scenarios"].clear()
        second = get_storage_friction_scenarios()
        self.assertEqual(len(second["scenarios"]), 3)
        self.assertEqual(
            second["scenarios"][0]["paper_reference_notional_usd"],
            1_000_000.0,
        )

        result = apply_paper_friction(
            history(),
            position(),
            scenario_id="baseline",
        )
        result["scenario"]["commission_bps_per_side"] = 0
        another = apply_paper_friction(
            history(),
            position(),
            scenario_id="baseline",
        )
        self.assertEqual(another["scenario"]["commission_bps_per_side"], 10.0)

        with self.assertRaises(PaperFrictionValidationError):
            apply_paper_friction(
                history(),
                position(),
                scenario_id={"scenario_id": "baseline"},  # type: ignore[arg-type]
            )


class PaperLiquidityTests(unittest.TestCase):
    def test_positive_turnover_has_precedence_over_close_times_volume(self) -> None:
        result = apply_paper_friction(
            history(
                turnovers=(4_000_000.0, 4_000_000.0),
                volumes=(100_000_000.0, 100_000_000.0),
            ),
            position(weight_pct=10.0),
            scenario_id="baseline",
        )
        self.assertEqual(result["status"], "UNFILLABLE")
        self.assertEqual(
            result["liquidity_checks"][0]["proxy_basis"],
            "reported_turnover",
        )
        self.assertEqual(
            result["liquidity_checks"][0]["reason_code"],
            "CAPACITY_EXCEEDED",
        )

    def test_close_times_volume_fallback_accepts_exact_capacity_boundary(self) -> None:
        result = apply_paper_friction(
            history(
                turnovers=(0.0, None),
                volumes=(50_000.0, 50_000.0),
            ),
            position(weight_pct=10.0),
            scenario_id="baseline",
        )
        self.assertEqual(result["status"], "FILLABLE")
        self.assertEqual(len(result["liquidity_checks"]), 2)
        for check in result["liquidity_checks"]:
            self.assertEqual(check["proxy_basis"], "close_times_volume_proxy")
            self.assertEqual(check["required_notional_usd"], 100_000.0)
            self.assertEqual(check["capacity_usd"], 100_000.0)
            self.assertTrue(check["fillable"])

    def test_missing_liquidity_blocks_whole_scenario_and_hides_metrics(self) -> None:
        result = apply_paper_friction(
            history(
                turnovers=(None, None),
                volumes=(None, 0.0),
            ),
            position(),
            scenario_id="baseline",
        )
        self.assertEqual(result["status"], "UNFILLABLE")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason_code"], "UNFILLABLE")
        self.assertIsNone(result["equity_path"])
        self.assertIsNone(result["costs"])
        for key in (
            "gross_return_pct",
            "net_return_before_slippage_pct",
            "net_return_pct",
            "max_drawdown_pct",
            "positive",
        ):
            self.assertIsNone(result[key])
        self.assertTrue(
            all(
                check["reason_code"] == "LIQUIDITY_PROXY_UNAVAILABLE"
                for check in result["liquidity_checks"]
            )
        )
        self.assertFalse(result["partial_fills_allowed"])
        self.assertFalse(result["position_shrinking_allowed"])
        self.assertFalse(result["date_shifting_allowed"])

    def test_entry_and_exit_are_both_checked(self) -> None:
        result = apply_paper_friction(
            history(turnovers=(5_000_000.0, 4_999_000.0)),
            position(weight_pct=10.0),
            scenario_id="baseline",
        )
        self.assertEqual(result["status"], "UNFILLABLE")
        self.assertTrue(result["liquidity_checks"][0]["fillable"])
        self.assertFalse(result["liquidity_checks"][1]["fillable"])
        self.assertEqual(result["liquidity_checks"][1]["phase"], "exit")


class PaperFrictionAccountingTests(unittest.TestCase):
    def test_short_borrow_uses_weekend_calendar_days_and_average_notional(self) -> None:
        result = apply_paper_friction(
            history(
                closes=(100.0, 110.0),
                dates=("2026-01-09", "2026-01-12"),
            ),
            position(side="SHORT", weight_pct=100.0),
            scenario_id="baseline",
        )
        expected = 1_050_000.0 * 0.03 * 3 / 365
        self.assertEqual(result["status"], "FILLABLE")
        self.assertAlmostEqual(
            result["costs"]["short_borrow_cost_usd"],
            expected,
            places=8,
        )
        self.assertAlmostEqual(
            result["equity_path"][-1]["cumulative_short_borrow_cost_usd"],
            expected,
            places=8,
        )

    def test_rising_short_price_increases_borrow_cost(self) -> None:
        rising = apply_paper_friction(
            history(closes=(100.0, 110.0)),
            position(side="SHORT", weight_pct=100.0),
            scenario_id="baseline",
        )
        falling = apply_paper_friction(
            history(closes=(100.0, 90.0)),
            position(side="SHORT", weight_pct=100.0),
            scenario_id="baseline",
        )
        self.assertGreater(
            rising["costs"]["short_borrow_cost_usd"],
            falling["costs"]["short_borrow_cost_usd"],
        )

    def test_long_and_flat_positions_have_zero_short_borrow(self) -> None:
        long_result = apply_paper_friction(
            history(closes=(100.0, 110.0)),
            position(side="LONG", weight_pct=100.0),
            scenario_id="baseline",
        )
        flat_result = apply_paper_friction(
            history(closes=(100.0, 110.0)),
            position(side="FLAT", weight_pct=0.0),
            scenario_id="baseline",
        )
        self.assertEqual(long_result["costs"]["short_borrow_cost_usd"], 0.0)
        self.assertEqual(flat_result["costs"]["short_borrow_cost_usd"], 0.0)
        self.assertEqual(long_result["costs"]["short_borrow_cost_by_symbol_usd"], {})
        self.assertEqual(flat_result["costs"]["short_borrow_cost_by_symbol_usd"], {})

    def test_slippage_is_always_a_deduction(self) -> None:
        result = apply_paper_friction(
            history(closes=(100.0, 110.0)),
            position(side="LONG", weight_pct=50.0),
            scenario_id="baseline",
        )
        slippage_pct = (
            result["costs"]["slippage_cost_usd"]
            / result["scenario"]["paper_reference_notional_usd"]
            * 100
        )
        self.assertLess(
            result["net_return_pct"],
            result["net_return_before_slippage_pct"],
        )
        self.assertAlmostEqual(
            result["net_return_before_slippage_pct"] - result["net_return_pct"],
            slippage_pct,
            places=8,
        )

    def test_function_is_pure_and_marks_local_non_execution_scope(self) -> None:
        rows = history(closes=(100.0, 101.0, 102.0))
        positions = position(side="LONG", weight_pct=25.0)
        rows_before = copy.deepcopy(rows)
        positions_before = copy.deepcopy(positions)

        result = apply_paper_friction(
            rows,
            positions,
            scenario_id="baseline",
        )

        self.assertEqual(rows, rows_before)
        self.assertEqual(positions, positions_before)
        self.assertEqual(result["provider_calls_total"], 0)
        self.assertEqual(result["openai_calls"], 0)
        self.assertEqual(result["execution_capability"], "none")
        self.assertFalse(result["live_trading_allowed"])
        self.assertFalse(result["can_autonomously_decide"])
        self.assertFalse(result["out_of_sample_claim"])
        self.assertFalse(result["actual_execution_observed"])
        self.assertTrue(result["liquidity_model"]["capacity_is_proxy"])
        self.assertFalse(
            result["liquidity_model"]["actual_execution_observed"]
        )
        self.assertTrue(
            all(
                check["capacity_is_proxy"]
                and not check["actual_execution_observed"]
                for check in result["liquidity_checks"]
            )
        )


if __name__ == "__main__":
    unittest.main()
