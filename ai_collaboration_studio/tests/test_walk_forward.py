from __future__ import annotations

import copy
import unittest
from datetime import date, datetime, timedelta, timezone

from backend.walk_forward import (
    CONFIG_VERSION,
    CONFIG_VERSION_V1,
    CONFIG_VERSION_V2,
    ENGINE_VERSION,
    ENGINE_VERSION_V1,
    ENGINE_VERSION_V2,
    ENGINE_VERSION_V3,
    INPUT_SNAPSHOT_VERSION,
    INPUT_SNAPSHOT_VERSION_V1,
    INPUT_SNAPSHOT_VERSION_V2,
    INSUFFICIENT_WINDOWS_REASON,
    MINIMUM_INDEPENDENT_FOLDS,
    PLAN_VERSION,
    RESULT_VERSION,
    RESULT_VERSION_V1,
    RESULT_VERSION_V2,
    RESULT_VERSION_V3,
    WalkForwardFeasibilityError,
    WalkForwardValidationError,
    calculate_walk_forward_feasibility,
    run_walk_forward_backtest,
)


SYMBOLS = ("US.MU", "US.SNDK", "US.WDC", "US.STX")


def trading_dates(days: int, *, start: date = date(2026, 1, 5)) -> list[date]:
    result: list[date] = []
    current = start
    while len(result) < days:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


def history_payload(
    symbol: str,
    dates: list[date],
    closes: list[float],
    *,
    captured_at: str | None = None,
) -> dict:
    if len(dates) != len(closes):
        raise AssertionError("dates and closes must align")
    as_of = dates[-1] + timedelta(days=1)
    captured = captured_at or datetime.combine(
        as_of,
        datetime.min.time(),
        tzinfo=timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    return {
        "ok": True,
        "source": "futu_opend",
        "interval": "1d",
        "price_adjustment": "QFQ",
        "captured_at": captured,
        "as_of_date": as_of.isoformat(),
        "last_completed_session": dates[-1].isoformat(),
        "actual_start": dates[0].isoformat(),
        "actual_end": dates[-1].isoformat(),
        "symbol": symbol,
        "rows": [
            {
                "symbol": symbol,
                "market_time": f"{session.isoformat()} 16:00:00",
                "close": round(close, 8),
            }
            for session, close in zip(dates, closes)
        ],
        "source_errors": [],
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def history_set(
    *,
    days: int = 40,
    daily_returns: tuple[float, float, float, float] = (0.003, 0.002, -0.001, 0.001),
) -> dict[str, dict]:
    dates = trading_dates(days)
    result: dict[str, dict] = {}
    for symbol, daily_return in zip(SYMBOLS, daily_returns):
        close = 100.0
        closes: list[float] = []
        for index in range(days):
            if index:
                close *= 1 + daily_return
            closes.append(close)
        result[symbol] = history_payload(symbol, dates, closes)
    return result


def plan(
    histories: dict[str, dict],
    *positions: tuple[str, str, float],
    **overrides,
) -> dict:
    first = histories[SYMBOLS[0]]
    payload = {
        "version": PLAN_VERSION,
        "portfolio_id": "portfolio_walk_forward_fixture",
        "portfolio_version": 3,
        "strategy_created_at": 1_750_000_000_000,
        "mode": "retroactive_fixed_plan_replay",
        "strategy_provenance": "current_plan_retroactive",
        "out_of_sample_claim": False,
        "evaluation_as_of_date": first["as_of_date"],
        "data_snapshot_cutoff": first["last_completed_session"],
        "name": "版本化固定纸面方案",
        "positions": [
            {
                "symbol": symbol,
                "side": side,
                "weight_pct": weight,
                "thesis": "仅用于固定方案历史滚动回放",
                "invalidation": "结果失效时退回用户复核",
            }
            for symbol, side, weight in positions
        ],
    }
    payload.update(overrides)
    return payload


def config(
    *,
    train_days: int = 8,
    test_days: int = 2,
    step_days: int = 2,
    transaction_cost_bps: float = 0,
) -> dict:
    return {
        "version": CONFIG_VERSION,
        "train_days": train_days,
        "test_days": test_days,
        "step_days": step_days,
        "transaction_cost_bps": transaction_cost_bps,
        "price_adjustment": "QFQ",
    }


class WalkForwardTests(unittest.TestCase):
    def test_version_constants_keep_current_runtime_on_v2(self) -> None:
        self.assertEqual(ENGINE_VERSION_V1, "walk_forward_engine_v1")
        self.assertEqual(ENGINE_VERSION_V2, "walk_forward_engine_v2")
        self.assertEqual(ENGINE_VERSION_V3, "walk_forward_engine_v3")
        self.assertEqual(CONFIG_VERSION_V1, "walk_forward_config_v1")
        self.assertEqual(CONFIG_VERSION_V2, "walk_forward_config_v2")
        self.assertEqual(RESULT_VERSION_V1, "walk_forward_result_v1")
        self.assertEqual(RESULT_VERSION_V2, "walk_forward_result_v2")
        self.assertEqual(RESULT_VERSION_V3, "walk_forward_result_v3")
        self.assertEqual(
            INPUT_SNAPSHOT_VERSION_V1,
            "walk_forward_input_snapshot_v1",
        )
        self.assertEqual(
            INPUT_SNAPSHOT_VERSION_V2,
            "walk_forward_input_snapshot_v2",
        )
        self.assertEqual(ENGINE_VERSION, ENGINE_VERSION_V2)
        self.assertEqual(CONFIG_VERSION, CONFIG_VERSION_V1)
        self.assertEqual(RESULT_VERSION, RESULT_VERSION_V2)
        self.assertEqual(INPUT_SNAPSHOT_VERSION, INPUT_SNAPSHOT_VERSION_V1)

    def test_result_is_deterministic_retroactive_historical_only_and_safe(self) -> None:
        histories = history_set(days=110)
        positions = (
            ("US.MU", "LONG", 30),
            ("US.SNDK", "LONG", 20),
            ("US.WDC", "SHORT", 15),
            ("US.STX", "FLAT", 0),
        )
        first = run_walk_forward_backtest(
            histories,
            plan(histories, *positions),
            config(train_days=20, test_days=4, step_days=4),
        )
        reversed_histories = dict(reversed(list(histories.items())))
        second = run_walk_forward_backtest(
            reversed_histories,
            plan(histories, *reversed(positions)),
            config(train_days=20, test_days=4, step_days=4),
        )

        self.assertEqual(first, second)
        self.assertEqual(first["state"], "sufficient")
        self.assertEqual(first["scope"], "historical_only")
        self.assertTrue(first["historical_only"])
        self.assertEqual(first["evaluation_mode"], "retroactive_fixed_plan_replay")
        self.assertEqual(first["strategy_provenance"], "current_plan_retroactive")
        self.assertFalse(first["out_of_sample_claim"])
        self.assertEqual(first["summary"]["fold_count"], 22)
        self.assertEqual(first["summary"]["non_overlapping_test_fold_count"], 22)
        self.assertEqual(
            first["summary"]["minimum_non_overlapping_test_folds"],
            MINIMUM_INDEPENDENT_FOLDS,
        )
        self.assertEqual(first["summary"]["status"], "sufficient")
        self.assertNotIn("win_rate", first["summary"])
        self.assertEqual(first["provider_calls_total"], 0)
        self.assertEqual(first["openai_calls"], 0)
        self.assertEqual(first["execution_capability"], "none")
        self.assertFalse(first["live_trading_allowed"])
        self.assertEqual(len(first["input_hash"]), 64)
        self.assertTrue(all(len(fold["input_hash"]) == 64 for fold in first["folds"]))

    def test_future_prices_do_not_change_earlier_decision_hash(self) -> None:
        histories = history_set(days=80)
        paper_plan = plan(
            histories,
            ("US.MU", "LONG", 60),
            ("US.WDC", "SHORT", 20),
        )
        settings = config(train_days=8, test_days=3, step_days=3)
        baseline = run_walk_forward_backtest(histories, paper_plan, settings)

        far_future = copy.deepcopy(histories)
        far_future["US.MU"]["rows"][-1]["close"] *= 4
        far_future_result = run_walk_forward_backtest(
            far_future,
            paper_plan,
            settings,
        )
        self.assertEqual(baseline["folds"][0], far_future_result["folds"][0])
        self.assertNotEqual(baseline["input_hash"], far_future_result["input_hash"])

        first_return_day = copy.deepcopy(histories)
        first_return_day["US.MU"]["rows"][9]["close"] *= 1.5
        execution_changed = run_walk_forward_backtest(
            first_return_day,
            paper_plan,
            settings,
        )
        self.assertEqual(
            baseline["folds"][0]["decision_input_hash"],
            execution_changed["folds"][0]["decision_input_hash"],
        )
        self.assertNotEqual(
            baseline["folds"][0]["input_hash"],
            execution_changed["folds"][0]["input_hash"],
        )
        self.assertNotEqual(
            baseline["folds"][0]["max_drawdown_pct"],
            execution_changed["folds"][0]["max_drawdown_pct"],
        )

    def test_entry_is_next_session_after_friday_decision_cutoff(self) -> None:
        histories = history_set(days=46, daily_returns=(0, 0, 0, 0))
        result = run_walk_forward_backtest(
            histories,
            plan(histories, ("US.MU", "LONG", 100)),
            config(train_days=5, test_days=2, step_days=2),
        )
        fold = result["folds"][0]

        self.assertEqual(fold["decision_cutoff"], "2026-01-09")
        self.assertEqual(fold["execution_start"], "2026-01-12")
        self.assertEqual(fold["entry_price_date"], "2026-01-12")
        self.assertEqual(fold["first_return_date"], "2026-01-13")
        self.assertLess(fold["decision_cutoff"], fold["execution_start"])

    def test_fixed_transaction_cost_is_charged_on_entry_and_exit(self) -> None:
        histories = history_set(days=44, daily_returns=(0, 0, 0, 0))
        result = run_walk_forward_backtest(
            histories,
            plan(histories, ("US.MU", "LONG", 100)),
            config(
                train_days=3,
                test_days=2,
                step_days=2,
                transaction_cost_bps=10,
            ),
        )

        self.assertEqual(result["folds"][0]["entry_cost_pct"], 0.1)
        self.assertEqual(result["folds"][0]["exit_cost_pct"], 0.1)
        self.assertEqual(result["folds"][0]["transaction_cost_pct"], 0.2)
        self.assertEqual(result["folds"][0]["net_return_pct"], -0.2)
        self.assertEqual(result["folds"][0]["benchmark_equal_weight_return_pct"], 0)

    def test_fixed_initial_notional_does_not_hide_daily_rebalancing(self) -> None:
        dates = trading_dates(44)
        histories = {
            "US.MU": history_payload(
                "US.MU",
                dates,
                [100, 100, 100, 100, 200, 100] + [100] * 38,
            ),
            "US.SNDK": history_payload(
                "US.SNDK",
                dates,
                [100, 100, 100, 100, 50, 100] + [100] * 38,
            ),
            "US.WDC": history_payload("US.WDC", dates, [100] * 44),
            "US.STX": history_payload("US.STX", dates, [100] * 44),
        }
        result = run_walk_forward_backtest(
            histories,
            plan(
                histories,
                ("US.MU", "LONG", 50),
                ("US.SNDK", "LONG", 50),
            ),
            config(train_days=3, test_days=2, step_days=2),
        )

        self.assertEqual(result["folds"][0]["net_return_pct"], 0)
        self.assertEqual(
            result["folds"][0]["holding_rule"],
            "fixed_initial_notional_buy_and_hold",
        )
        self.assertEqual(result["folds"][0]["rebalancing"], "none_within_fold")

    def test_short_weight_uses_fixed_initial_notional(self) -> None:
        dates = trading_dates(44)
        histories = {
            symbol: history_payload(
                symbol,
                dates,
                (
                    [100, 100, 100, 100, 90, 81] + [81] * 38
                    if symbol == "US.MU"
                    else [100] * 44
                ),
            )
            for symbol in SYMBOLS
        }
        result = run_walk_forward_backtest(
            histories,
            plan(histories, ("US.MU", "SHORT", 100)),
            config(train_days=3, test_days=2, step_days=2),
        )
        fold = result["folds"][0]

        self.assertEqual(fold["net_return_pct"], 19)
        self.assertEqual(fold["benchmark_equal_weight_return_pct"], -4.75)
        self.assertTrue(fold["positive"])
        self.assertFalse(result["method"]["short_borrow_fee_included"])

    def test_overlapping_tests_count_only_non_overlapping_windows(self) -> None:
        histories = history_set(days=70)
        result = run_walk_forward_backtest(
            histories,
            plan(histories, ("US.MU", "LONG", 100)),
            config(train_days=3, test_days=3, step_days=1),
        )

        self.assertEqual(result["summary"]["fold_count"], 64)
        self.assertEqual(result["summary"]["non_overlapping_test_fold_count"], 22)
        self.assertEqual(result["summary"]["status"], "sufficient")
        self.assertEqual(
            [
                fold["non_overlapping_test_window"]
                for fold in result["folds"][:7]
            ],
            [True, False, False, True, False, False, True],
        )
        self.assertIn("not_statistical_independence", result["summary"]["basis"])

    def test_preflight_exactly_gates_1_5_and_20_day_windows(self) -> None:
        for horizon, minimum_rows in ((1, 120), (5, 200), (20, 500)):
            settings = config(
                train_days=99,
                test_days=horizon,
                step_days=horizon,
            )
            with self.subTest(horizon=horizon, state="one_row_short"):
                diagnostic = calculate_walk_forward_feasibility(
                    minimum_rows - 1,
                    settings,
                )
                self.assertEqual(diagnostic["status"], "blocked")
                self.assertEqual(
                    diagnostic["reason_code"],
                    INSUFFICIENT_WINDOWS_REASON,
                )
                self.assertEqual(
                    diagnostic["maximum_non_overlapping_test_fold_count"],
                    19,
                )
                self.assertEqual(diagnostic["minimum_common_trading_days"], minimum_rows)
                self.assertEqual(diagnostic["history_row_shortfall"], 1)
                self.assertEqual(
                    diagnostic["test_window_return_observations"],
                    horizon,
                )
                self.assertEqual(diagnostic["test_window_price_rows"], horizon + 1)
                self.assertFalse(diagnostic["window_shortening_allowed"])
                self.assertFalse(diagnostic["synthetic_padding_allowed"])

                histories = history_set(days=minimum_rows - 1)
                with self.assertRaises(WalkForwardFeasibilityError) as raised:
                    run_walk_forward_backtest(
                        histories,
                        plan(histories, ("US.MU", "LONG", 100)),
                        settings,
                    )
                self.assertEqual(raised.exception.diagnostic, diagnostic)

            with self.subTest(horizon=horizon, state="exact_boundary"):
                histories = history_set(days=minimum_rows)
                result = run_walk_forward_backtest(
                    histories,
                    plan(histories, ("US.MU", "LONG", 100)),
                    settings,
                )
                self.assertEqual(result["state"], "sufficient")
                self.assertEqual(result["summary"]["fold_count"], 20)
                self.assertEqual(
                    result["summary"]["non_overlapping_test_fold_count"],
                    20,
                )
                self.assertEqual(result["feasibility"]["status"], "ready")
                self.assertEqual(result["feasibility"]["history_row_shortfall"], 0)

    def test_preflight_counts_match_scheduler_for_non_divisible_steps(self) -> None:
        cases = (
            (70, 3, 3, 1),
            (100, 10, 5, 2),
            (80, 8, 7, 3),
            (500, 99, 20, 20),
            (500, 50, 20, 37),
        )
        for rows, train_days, test_days, step_days in cases:
            with self.subTest(
                rows=rows,
                train_days=train_days,
                test_days=test_days,
                step_days=step_days,
            ):
                settings = config(
                    train_days=train_days,
                    test_days=test_days,
                    step_days=step_days,
                )
                diagnostic = calculate_walk_forward_feasibility(rows, settings)
                starts: list[int] = []
                start = train_days
                while start + test_days < rows:
                    starts.append(start)
                    start += step_days
                selected: list[int] = []
                last_end: int | None = None
                for start in starts:
                    if last_end is None or start >= last_end:
                        selected.append(start)
                        last_end = start + test_days

                self.assertEqual(
                    diagnostic["maximum_candidate_fold_count"],
                    len(starts),
                )
                self.assertEqual(
                    diagnostic["maximum_non_overlapping_test_fold_count"],
                    len(selected),
                )
                minimum_rows = diagnostic["minimum_common_trading_days"]
                self.assertEqual(
                    calculate_walk_forward_feasibility(
                        minimum_rows,
                        settings,
                    )["maximum_non_overlapping_test_fold_count"],
                    MINIMUM_INDEPENDENT_FOLDS,
                )
                self.assertLess(
                    calculate_walk_forward_feasibility(
                        minimum_rows - 1,
                        settings,
                    )["maximum_non_overlapping_test_fold_count"],
                    MINIMUM_INDEPENDENT_FOLDS,
                )

    def test_history_integrity_and_four_symbol_coverage_fail_closed(self) -> None:
        valid = history_set(days=12)
        invalid_cases: list[tuple[str, dict[str, dict]]] = []

        missing_close = copy.deepcopy(valid)
        del missing_close["US.MU"]["rows"][2]["close"]
        invalid_cases.append(("缺少 close", missing_close))

        missing_date = copy.deepcopy(valid)
        del missing_date["US.MU"]["rows"][2]["market_time"]
        invalid_cases.append(("缺少有效交易日期", missing_date))

        duplicate_date = copy.deepcopy(valid)
        duplicate_date["US.MU"]["rows"][3]["market_time"] = duplicate_date["US.MU"]["rows"][2]["market_time"]
        invalid_cases.append(("重复交易日期", duplicate_date))

        non_increasing = copy.deepcopy(valid)
        non_increasing["US.MU"]["rows"][2], non_increasing["US.MU"]["rows"][3] = (
            non_increasing["US.MU"]["rows"][3],
            non_increasing["US.MU"]["rows"][2],
        )
        invalid_cases.append(("严格递增", non_increasing))

        missing_calendar_row = copy.deepcopy(valid)
        del missing_calendar_row["US.WDC"]["rows"][4]
        invalid_cases.append(("不完整对齐", missing_calendar_row))

        missing_symbol = copy.deepcopy(valid)
        del missing_symbol["US.STX"]
        invalid_cases.append(("完整覆盖", missing_symbol))

        wrong_source = copy.deepcopy(valid)
        wrong_source["US.MU"]["source"] = "client_upload"
        invalid_cases.append(("source 必须是 futu_opend", wrong_source))

        future_row = copy.deepcopy(valid)
        future_row["US.MU"]["last_completed_session"] = future_row["US.MU"]["rows"][-2]["market_time"][:10]
        invalid_cases.append(("超过 last_completed_session", future_row))

        paper_plan = plan(valid, ("US.MU", "LONG", 100))
        for expected_message, histories in invalid_cases:
            with self.subTest(expected_message=expected_message):
                with self.assertRaisesRegex(WalkForwardValidationError, expected_message):
                    run_walk_forward_backtest(histories, paper_plan, config())

    def test_plan_provenance_and_version_contract_fail_closed(self) -> None:
        histories = history_set(days=12)
        valid_plan = plan(histories, ("US.MU", "LONG", 100))

        with self.assertRaisesRegex(WalkForwardValidationError, "仅支持"):
            run_walk_forward_backtest(
                histories,
                plan(histories, ("US.AAPL", "LONG", 100)),
                config(),
            )

        unversioned = dict(valid_plan)
        del unversioned["version"]
        with self.assertRaisesRegex(WalkForwardValidationError, "plan.version"):
            run_walk_forward_backtest(histories, unversioned, config())

        false_oos_claim = dict(valid_plan)
        false_oos_claim["out_of_sample_claim"] = True
        with self.assertRaisesRegex(WalkForwardValidationError, "必须为 false"):
            run_walk_forward_backtest(histories, false_oos_claim, config())

        non_qfq = config()
        non_qfq["price_adjustment"] = "NONE"
        with self.assertRaisesRegex(WalkForwardValidationError, "QFQ"):
            run_walk_forward_backtest(histories, valid_plan, non_qfq)


if __name__ == "__main__":
    unittest.main()
