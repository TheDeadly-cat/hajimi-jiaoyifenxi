from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .decision_lineage import canonical_sha256
from .candidate_simulation_contract import (
    CandidateSimulationContractError,
    build_candidate_simulation_contract,
    verify_candidate_simulation_contract,
)
from .market.futu_readonly import STORAGE_SYMBOLS
from .paper_portfolio import evaluate_paper_portfolio, normalize_paper_portfolio_plan
from .store import StudioStore
from .walk_forward import (
    CONFIG_VERSION_V2,
    CONFIG_VERSION_V3,
    INPUT_SNAPSHOT_VERSION_V2,
    INPUT_SNAPSHOT_VERSION_V3,
    PLAN_VERSION as WALK_FORWARD_PLAN_VERSION,
    PLAN_VERSION_V2 as STRATEGY_WALK_FORWARD_PLAN_VERSION,
    RULE_ID as DEFAULT_STRATEGY_RULE_ID,
    build_strategy_rule_contract,
    normalize_walk_forward_config,
    normalize_walk_forward_plan,
    run_walk_forward_backtest,
)
from .walk_forward_friction import (
    PAPER_FRICTION_MODEL_VERSION,
    PAPER_LIQUIDITY_PROXY_VERSION,
    STORAGE_FRICTION_SCENARIOS_VERSION,
    UNFILLABLE_POLICY,
    get_storage_friction_scenarios,
)


DEFAULT_WALK_FORWARD_CONFIG = {
    "version": CONFIG_VERSION_V2,
    "train_days": 99,
    "test_days": 20,
    "step_days": 20,
    "price_adjustment": "QFQ",
    "friction_scenario_set": STORAGE_FRICTION_SCENARIOS_VERSION,
    "unfillable_policy": UNFILLABLE_POLICY,
}
DEFAULT_STRATEGY_WALK_FORWARD_CONFIG = {
    "version": CONFIG_VERSION_V3,
    "train_days": 99,
    "test_days": 20,
    "step_days": 20,
    "price_adjustment": "QFQ",
    "friction_scenario_set": STORAGE_FRICTION_SCENARIOS_VERSION,
    "unfillable_policy": UNFILLABLE_POLICY,
    "strategy_rule_id": DEFAULT_STRATEGY_RULE_ID,
}
WALK_FORWARD_HISTORY_LIMIT = 500
WALK_FORWARD_HISTORY_LOOKBACK_CALENDAR_DAYS = 1460


class PaperPortfolioService:
    """Versioned, research-only paper allocations with deterministic risk checks."""

    def __init__(self, store: StudioStore, market_service: Any) -> None:
        self.store = store
        self.market_service = market_service

    def _histories(self, plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
        active_symbols = {
            position["symbol"]
            for position in plan["positions"]
            if position["side"] != "FLAT" and position["weight_pct"] > 0
        }
        histories: dict[str, dict[str, Any]] = {}
        for symbol in STORAGE_SYMBOLS:
            if symbol not in active_symbols:
                continue
            try:
                history = self.market_service.history(symbol, limit=260)
            except Exception as exc:
                history = {
                    "ok": False,
                    "symbol": symbol,
                    "rows": [],
                    "source_errors": [{
                        "source": "futu_opend",
                        "code": "PORTFOLIO_HISTORY_ERROR",
                        "message": str(exc)[:300],
                    }],
                }
            histories[symbol] = history if isinstance(history, dict) else {
                "ok": False,
                "symbol": symbol,
                "rows": [],
            }
        return histories

    def evaluate(self, plan_value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        plan = normalize_paper_portfolio_plan(plan_value)
        evaluation = self._evaluate_normalized(plan)
        return plan, evaluation

    def _evaluate_normalized(self, plan: dict[str, Any]) -> dict[str, Any]:
        return evaluate_paper_portfolio(plan, self._histories(plan))

    def _candidate_contract_preflight(
        self,
        room_id: str,
        plan: dict[str, Any],
        confirmation_value: Any,
        *,
        user_decision_id: str = "",
        portfolio_id: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        context = self.store.candidate_simulation_context(
            room_id,
            user_decision_id=user_decision_id,
            portfolio_id=portfolio_id,
        )
        if context.get("strict_required") is not True:
            if confirmation_value not in (None, "", {}):
                raise CandidateSimulationContractError(
                    "CANDIDATE_SIMULATION_SOURCE_UNAVAILABLE",
                    "当前决定没有正式候选快照，不能提交已验证模拟映射。",
                    status=422,
                )
            return {}, context

        if confirmation_value not in (None, "", {}):
            contract = build_candidate_simulation_contract(
                context.get("anchor") or {},
                plan,
                confirmation_value,
            )
            return contract, context

        existing_contract = context.get("contract") or {}
        if existing_contract:
            try:
                contract = verify_candidate_simulation_contract(
                    existing_contract,
                    context.get("anchor") or {},
                    plan,
                )
            except CandidateSimulationContractError as exc:
                raise CandidateSimulationContractError(
                    "CANDIDATE_SIMULATION_CONFIRMATION_REQUIRED",
                    "组合映射已变化，必须由用户重新确认精确候选规格。",
                    status=400,
                ) from exc
            return contract, context

        raise CandidateSimulationContractError(
            "CANDIDATE_SIMULATION_CONFIRMATION_REQUIRED",
            "建立正式候选模拟组合前必须由用户确认精确映射。",
            status=400,
        )

    @staticmethod
    def _walk_forward_plan(
        portfolio: dict[str, Any],
        *,
        evaluation_as_of_date: str,
        data_snapshot_cutoff: str,
    ) -> dict[str, Any]:
        return normalize_walk_forward_plan({
            "version": WALK_FORWARD_PLAN_VERSION,
            "portfolio_id": portfolio.get("id"),
            "portfolio_version": portfolio.get("version"),
            "strategy_created_at": portfolio.get("created_at"),
            "mode": "retroactive_fixed_plan_replay",
            "strategy_provenance": "current_plan_retroactive",
            "out_of_sample_claim": False,
            "evaluation_as_of_date": evaluation_as_of_date,
            "data_snapshot_cutoff": data_snapshot_cutoff,
            "name": portfolio.get("name") or "",
            "positions": [
                {
                    "symbol": position.get("symbol"),
                    "side": position.get("side"),
                    "weight_pct": position.get("weight_pct"),
                    "thesis": position.get("thesis") or "",
                    "invalidation": position.get("invalidation") or "",
                }
                for position in portfolio.get("positions") or []
            ],
        })

    @staticmethod
    def _strategy_walk_forward_plan(
        portfolio: dict[str, Any],
        *,
        evaluation_as_of_date: str,
        data_snapshot_cutoff: str,
        decision_state: dict[str, Any],
    ) -> dict[str, Any]:
        decision_binding = decision_state["decision_binding"]
        return normalize_walk_forward_plan({
            "version": STRATEGY_WALK_FORWARD_PLAN_VERSION,
            "portfolio_id": portfolio.get("id"),
            "portfolio_version": portfolio.get("version"),
            "strategy_created_at": portfolio.get("created_at"),
            "mode": "fold_train_only_next_session_test_replay",
            "strategy_provenance": "server_whitelisted_fold_trained_rule",
            "out_of_sample_claim": False,
            "future_performance_claim": False,
            "retrospective_dataset": True,
            "source_user_decision_id": decision_binding["user_decision_id"],
            "decision_anchor_sha256": decision_state[
                "decision_anchor_sha256"
            ],
            "source_decision_head_sequence": decision_binding[
                "source_lineage_head_sequence"
            ],
            "source_decision_head_sha256": decision_binding[
                "source_lineage_head_sha256"
            ],
            "evaluation_as_of_date": evaluation_as_of_date,
            "data_snapshot_cutoff": data_snapshot_cutoff,
            "name": portfolio.get("name") or "",
            "positions": [
                {
                    "symbol": position.get("symbol"),
                    "side": position.get("side"),
                    "weight_pct": position.get("weight_pct"),
                    "thesis": position.get("thesis") or "",
                    "invalidation": position.get("invalidation") or "",
                }
                for position in portfolio.get("positions") or []
            ],
        })

    def _walk_forward_histories(self) -> dict[str, dict[str, Any]]:
        # Futu's request_history_kline defaults to a much shorter implicit
        # range when start/end are omitted.  Ask for an explicit bounded range
        # and still retain at most the adapter's latest 500 completed rows.
        # The adapter removes the current US session, so an Asia-local end date
        # one day ahead cannot leak an unfinished bar into the frozen input.
        request_end = date.today()
        request_start = request_end - timedelta(
            days=WALK_FORWARD_HISTORY_LOOKBACK_CALENDAR_DAYS
        )
        try:
            batch = self.market_service.history_batch(
                STORAGE_SYMBOLS,
                start=request_start.isoformat(),
                end=request_end.isoformat(),
                limit=WALK_FORWARD_HISTORY_LIMIT,
            )
        except Exception as exc:
            raise ValueError("四只标的的 Futu QFQ 日线批量读取失败") from exc
        if not isinstance(batch, dict):
            raise ValueError("Futu QFQ 日线批量响应无效")
        if batch.get("source") != "futu_opend":
            raise ValueError("历史数据不是 Futu 只读来源")
        if batch.get("interval") != "1d" or batch.get("price_adjustment") != "QFQ":
            raise ValueError("历史数据不是 Futu QFQ 日线")
        if batch.get("execution_capability") != "none":
            raise ValueError("Futu 历史数据源不能获得订单执行能力")
        if batch.get("live_trading_allowed") is not False:
            raise ValueError("Futu 历史数据源不能打开真实交易")
        if batch.get("ok") is not True or batch.get("source_errors"):
            raise ValueError("四只标的的 Futu QFQ 日线尚未就绪")
        raw_histories = batch.get("histories")
        if not isinstance(raw_histories, dict):
            raise ValueError("Futu QFQ 日线批量结果缺少 histories")
        histories: dict[str, dict[str, Any]] = {}
        for symbol in STORAGE_SYMBOLS:
            history = raw_histories.get(symbol)
            if not isinstance(history, dict):
                raise ValueError(f"{symbol} 的 Futu QFQ 日线响应无效")
            if history.get("source") != "futu_opend":
                raise ValueError(f"{symbol} 的历史数据不是 Futu 只读来源")
            if history.get("interval") != "1d":
                raise ValueError(f"{symbol} 的历史数据不是已完成日线")
            if history.get("price_adjustment") != "QFQ":
                raise ValueError(f"{symbol} 的历史数据不是 QFQ 日线")
            if history.get("execution_capability") != "none":
                raise ValueError("Futu 历史数据源不能获得订单执行能力")
            if history.get("live_trading_allowed") is not False:
                raise ValueError("Futu 历史数据源不能打开真实交易")
            if history.get("ok") is not True or history.get("source_errors"):
                raise ValueError(f"{symbol} 的 Futu QFQ 日线尚未就绪")
            for required_field in (
                "captured_at",
                "as_of_date",
                "last_completed_session",
                "actual_start",
                "actual_end",
            ):
                if not str(history.get(required_field) or "").strip():
                    raise ValueError(f"{symbol} 的 Futu 日线缺少 {required_field}")
            try:
                as_of_date = date.fromisoformat(str(history["as_of_date"]))
                last_completed = date.fromisoformat(
                    str(history["last_completed_session"])
                )
            except ValueError as exc:
                raise ValueError(f"{symbol} 的 Futu 日线日期元数据无效") from exc
            if last_completed >= as_of_date:
                raise ValueError(f"{symbol} 的 Futu 日线包含未完成的当前日期")
            rows = history.get("rows")
            if not isinstance(rows, list) or not rows:
                raise ValueError(f"{symbol} 的 Futu QFQ 日线为空")
            if str(rows[-1].get("market_time") or "")[:10] != str(
                history["last_completed_session"]
            ):
                raise ValueError(f"{symbol} 的最后完成交易日与日线不一致")
            histories[symbol] = history
        return self._align_walk_forward_histories(histories)

    @staticmethod
    def _align_walk_forward_histories(
        histories: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        calendars: dict[str, list[str]] = {}
        for symbol in STORAGE_SYMBOLS:
            rows = histories[symbol]["rows"]
            dates: list[str] = []
            for index, row in enumerate(rows):
                date_key = str(row.get("market_time") or "")[:10]
                try:
                    date.fromisoformat(date_key)
                except ValueError as exc:
                    raise ValueError(
                        f"{symbol} 第 {index + 1} 行交易日期无效"
                    ) from exc
                if dates and date_key <= dates[-1]:
                    raise ValueError(f"{symbol} 的 Futu 日线日期必须严格递增")
                dates.append(date_key)
            calendars[symbol] = dates

        common_start = max(dates[0] for dates in calendars.values())
        aligned_calendars = {
            symbol: [date_key for date_key in dates if date_key >= common_start]
            for symbol, dates in calendars.items()
        }
        reference = aligned_calendars[STORAGE_SYMBOLS[0]]
        if not reference:
            raise ValueError("四只标的没有共同的 Futu QFQ 日线")
        for symbol in STORAGE_SYMBOLS[1:]:
            if aligned_calendars[symbol] != reference:
                raise ValueError(
                    "四只标的从共同起点后的 Futu 日线存在缺口，不能静默取交集"
                )

        aligned: dict[str, dict[str, Any]] = {}
        for symbol in STORAGE_SYMBOLS:
            original = histories[symbol]
            trimmed_rows = [
                dict(row)
                for row in original["rows"]
                if str(row.get("market_time") or "")[:10] >= common_start
            ]
            aligned[symbol] = {
                **original,
                "rows": trimmed_rows,
                "source_actual_start": original["actual_start"],
                "source_actual_end": original["actual_end"],
                "alignment_start": common_start,
                "alignment_dropped_leading_rows": (
                    len(original["rows"]) - len(trimmed_rows)
                ),
                "actual_start": common_start,
                "actual_end": reference[-1],
            }
        return aligned

    @staticmethod
    def _walk_forward_input_snapshot(
        room_id: str,
        portfolio: dict[str, Any],
        plan: dict[str, Any],
        config: dict[str, Any],
        histories: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        as_of_dates = {str(history["as_of_date"]) for history in histories.values()}
        cutoffs = {
            str(history["last_completed_session"])
            for history in histories.values()
        }
        calendars = [
            [
                str(row.get("market_time") or "")[:10]
                for row in history["rows"]
            ]
            for history in histories.values()
        ]
        if len(as_of_dates) != 1:
            raise ValueError("四只标的的 Futu 评估日期不一致")
        if len(cutoffs) != 1:
            raise ValueError("四只标的的最后完成交易日不一致")
        if any(calendar != calendars[0] for calendar in calendars[1:]):
            raise ValueError("四只标的的 Futu 日线交易日未完整对齐")
        captured_at = max(
            str(history["captured_at"])
            for history in histories.values()
        )
        evaluation_as_of_date = next(iter(as_of_dates))
        data_snapshot_cutoff = next(iter(cutoffs))
        scenario_set = get_storage_friction_scenarios()
        try:
            frozen_inputs = json.loads(
                json.dumps(
                    {
                        "plan": plan,
                        "config": config,
                        "histories": histories,
                        "portfolio_snapshot": portfolio,
                        "friction_scenario_set": scenario_set,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Futu 冻结输入不能序列化") from exc
        return {
            "version": INPUT_SNAPSHOT_VERSION_V2,
            "room_id": room_id,
            "portfolio_id": portfolio["id"],
            "portfolio_version": int(portfolio["version"]),
            "mode": "retroactive_fixed_plan_replay",
            "strategy_provenance": "current_plan_retroactive",
            "out_of_sample_claim": False,
            "portfolio_snapshot": frozen_inputs["portfolio_snapshot"],
            "plan": frozen_inputs["plan"],
            "config": frozen_inputs["config"],
            "histories": frozen_inputs["histories"],
            "manifest": {
                "source": "futu_qfq_daily_history",
                "interval": "1d",
                "price_adjustment": "QFQ",
                "captured_at": captured_at,
                "evaluation_as_of_date": evaluation_as_of_date,
                "data_snapshot_cutoff": data_snapshot_cutoff,
                "required_symbols": list(STORAGE_SYMBOLS),
                "covered_symbols": list(histories),
                "common_trading_days": len(calendars[0]),
                "actual_start": calendars[0][0],
                "actual_end": calendars[0][-1],
                "assumptions": {
                    "friction_scenario_set": frozen_inputs[
                        "friction_scenario_set"
                    ],
                    "unfillable_policy": UNFILLABLE_POLICY,
                    "friction_model_version": PAPER_FRICTION_MODEL_VERSION,
                    "liquidity_proxy_version": PAPER_LIQUIDITY_PROXY_VERSION,
                    "paper_research_only": True,
                    "server_owned": True,
                    "custom_overrides_allowed": False,
                    "partial_fills_allowed": False,
                    "position_shrinking_allowed": False,
                    "date_shifting_allowed": False,
                    "live_broker_rates": False,
                    "actual_execution_observed": False,
                },
                "provider_calls_total": 0,
                "openai_calls": 0,
                "execution_capability": "none",
                "live_trading_allowed": False,
                "can_autonomously_decide": False,
                "out_of_sample_claim": False,
                "actual_execution_observed": False,
            },
            "provider_calls_total": 0,
            "openai_calls": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
            "actual_execution_observed": False,
        }

    @staticmethod
    def _strategy_walk_forward_input_snapshot(
        room_id: str,
        portfolio: dict[str, Any],
        plan: dict[str, Any],
        config: dict[str, Any],
        histories: dict[str, dict[str, Any]],
        decision_state: dict[str, Any],
    ) -> dict[str, Any]:
        as_of_dates = {str(history["as_of_date"]) for history in histories.values()}
        cutoffs = {
            str(history["last_completed_session"])
            for history in histories.values()
        }
        calendars = [
            [
                str(row.get("market_time") or "")[:10]
                for row in history["rows"]
            ]
            for history in histories.values()
        ]
        if len(as_of_dates) != 1 or len(cutoffs) != 1:
            raise ValueError("the four Futu histories must share one snapshot cutoff")
        if any(calendar != calendars[0] for calendar in calendars[1:]):
            raise ValueError("the four Futu histories must use one aligned calendar")
        decision_binding = decision_state.get("decision_binding")
        decision_anchor_sha256 = str(
            decision_state.get("decision_anchor_sha256") or ""
        )
        if (
            not isinstance(decision_binding, dict)
            or canonical_sha256(decision_binding) != decision_anchor_sha256
        ):
            raise ValueError("walk-forward decision binding is not canonical")
        strategy_contract = config.get("strategy_rule_contract")
        if not isinstance(strategy_contract, dict):
            raise ValueError("walk-forward v4 strategy contract is missing")
        strategy_contract_sha256 = canonical_sha256(strategy_contract)
        captured_at = max(
            str(history["captured_at"])
            for history in histories.values()
        )
        evaluation_as_of_date = next(iter(as_of_dates))
        data_snapshot_cutoff = next(iter(cutoffs))
        scenario_set = get_storage_friction_scenarios()
        frozen_inputs = json.loads(json.dumps({
            "portfolio_snapshot": portfolio,
            "plan": plan,
            "config": config,
            "histories": histories,
            "strategy_rule_contract": strategy_contract,
            "decision_binding": decision_binding,
            "friction_scenario_set": scenario_set,
        }, ensure_ascii=False, allow_nan=False))
        return {
            "version": INPUT_SNAPSHOT_VERSION_V3,
            "room_id": room_id,
            "portfolio_id": portfolio["id"],
            "portfolio_version": int(portfolio["version"]),
            "mode": "fold_train_only_next_session_test_replay",
            "strategy_provenance": "server_whitelisted_fold_trained_rule",
            "out_of_sample_claim": False,
            "future_performance_claim": False,
            "retrospective_dataset": True,
            "portfolio_snapshot": frozen_inputs["portfolio_snapshot"],
            "plan": frozen_inputs["plan"],
            "config": frozen_inputs["config"],
            "histories": frozen_inputs["histories"],
            "strategy_rule_contract": frozen_inputs["strategy_rule_contract"],
            "strategy_contract_sha256": strategy_contract_sha256,
            "decision_binding": frozen_inputs["decision_binding"],
            "decision_anchor_sha256": decision_anchor_sha256,
            "manifest": {
                "source": "futu_qfq_daily_history",
                "interval": "1d",
                "price_adjustment": "QFQ",
                "captured_at": captured_at,
                "evaluation_as_of_date": evaluation_as_of_date,
                "data_snapshot_cutoff": data_snapshot_cutoff,
                "required_symbols": list(STORAGE_SYMBOLS),
                "covered_symbols": list(histories),
                "common_trading_days": len(calendars[0]),
                "actual_start": calendars[0][0],
                "actual_end": calendars[0][-1],
                "test_data_excluded_from_fold_fit": True,
                "prospective_test_protocol": True,
                "retrospective_dataset": True,
                "future_performance_claim": False,
                "assumptions": {
                    "friction_scenario_set": frozen_inputs[
                        "friction_scenario_set"
                    ],
                    "unfillable_policy": UNFILLABLE_POLICY,
                    "friction_model_version": PAPER_FRICTION_MODEL_VERSION,
                    "liquidity_proxy_version": PAPER_LIQUIDITY_PROXY_VERSION,
                    "paper_research_only": True,
                    "server_owned": True,
                    "custom_overrides_allowed": False,
                    "partial_fills_allowed": False,
                    "position_shrinking_allowed": False,
                    "date_shifting_allowed": False,
                    "live_broker_rates": False,
                    "actual_execution_observed": False,
                    "test_data_excluded_from_fold_fit": True,
                },
                "provider_calls_total": 0,
                "openai_calls": 0,
                "execution_capability": "none",
                "live_trading_allowed": False,
                "can_autonomously_decide": False,
                "out_of_sample_claim": False,
                "actual_execution_observed": False,
            },
            "provider_calls_total": 0,
            "openai_calls": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "can_autonomously_decide": False,
            "actual_execution_observed": False,
        }

    def walk_forward(
        self,
        room_id: str,
        portfolio_id: str,
        config_value: Any,
        *,
        expected_portfolio_version: int,
        created_by: str = "user",
    ) -> dict[str, Any]:
        if (
            isinstance(expected_portfolio_version, bool)
            or not isinstance(expected_portfolio_version, int)
            or expected_portfolio_version < 1
        ):
            raise ValueError("expected_portfolio_version 必须是正整数")
        current = self.store.get_paper_portfolio(room_id, portfolio_id)
        if not current:
            raise LookupError("模拟组合不存在")
        if int(current["version"]) != expected_portfolio_version:
            raise ValueError("模拟组合版本已变化，请重新载入后再运行 walk-forward")
        persisted_portfolio = self.store.get_paper_portfolio_snapshot(
            room_id,
            portfolio_id,
        )
        if not persisted_portfolio:
            raise LookupError("模拟组合不存在")

        current_plan = normalize_paper_portfolio_plan({
            "name": current.get("name") or "",
            "positions": current.get("positions") or [],
            "budgets": current.get("budgets") or {},
            "stress_scenarios": current.get("stress_scenarios") or [],
        })
        candidate_context = self.store.candidate_simulation_context(
            room_id,
            portfolio_id=portfolio_id,
        )
        candidate_contract: dict[str, Any] = {}
        if candidate_context.get("strict_required") is True:
            candidate_contract = verify_candidate_simulation_contract(
                candidate_context.get("contract") or {},
                candidate_context.get("anchor") or {},
                current_plan,
            )

        if not isinstance(config_value, dict):
            raise ValueError("walk-forward 配置必须是对象")
        raw_config = dict(config_value)
        strategy_mode = raw_config.get("version") == CONFIG_VERSION_V3
        if candidate_contract and strategy_mode:
            raise CandidateSimulationContractError(
                "CANDIDATE_SIMULATION_WALK_FORWARD_RULE_MISMATCH",
                "横截面排名规则会更换标的，不能声称实现所选单标的候选。",
                status=422,
            )
        if candidate_contract and raw_config.get("version") != CONFIG_VERSION_V2:
            raise CandidateSimulationContractError(
                "CANDIDATE_SIMULATION_WALK_FORWARD_RULE_MISMATCH",
                "已验证候选只能使用固定候选方向历史回放。",
                status=422,
            )
        request_defaults = (
            DEFAULT_STRATEGY_WALK_FORWARD_CONFIG
            if strategy_mode
            else DEFAULT_WALK_FORWARD_CONFIG
        )
        unknown_config_fields = set(raw_config) - set(request_defaults)
        if unknown_config_fields:
            raise ValueError(
                "config 包含未知字段："
                f"{', '.join(sorted(unknown_config_fields))}"
            )
        decision_state: dict[str, Any] | None = None
        if strategy_mode:
            requested = {
                **DEFAULT_STRATEGY_WALK_FORWARD_CONFIG,
                **raw_config,
            }
            strategy_rule_id = requested.pop("strategy_rule_id")
            decision_state = (
                self.store.capture_paper_portfolio_walk_forward_decision_binding(
                    room_id,
                    portfolio_id,
                    expected_portfolio_version=expected_portfolio_version,
                )
            )
            config = normalize_walk_forward_config({
                **requested,
                "strategy_rule_contract": build_strategy_rule_contract(
                    persisted_portfolio,
                    strategy_rule_id,
                ),
            })
        else:
            config = normalize_walk_forward_config({
                **DEFAULT_WALK_FORWARD_CONFIG,
                **raw_config,
            })
            if candidate_contract:
                evaluation_rule = candidate_contract.get("evaluation") or {}
                required_horizon = int(
                    evaluation_rule.get("horizon_days") or 0
                )
                if (
                    int(config.get("test_days") or 0) != required_horizon
                    or int(config.get("step_days") or 0) != required_horizon
                ):
                    raise CandidateSimulationContractError(
                        "CANDIDATE_SIMULATION_HORIZON_MISMATCH",
                        "测试窗口与步进必须等于候选合同的交易日期限。",
                        status=422,
                    )
            self.store.validate_paper_portfolio_walk_forward_eligibility(
                room_id,
                portfolio_id,
                expected_portfolio_version=expected_portfolio_version,
            )
        histories = self._walk_forward_histories()
        as_of_dates = {
            str(history["as_of_date"])
            for history in histories.values()
        }
        cutoffs = {
            str(history["last_completed_session"])
            for history in histories.values()
        }
        if len(as_of_dates) != 1 or len(cutoffs) != 1:
            raise ValueError("四只标的的 Futu 日线快照时点不一致")
        if strategy_mode:
            plan = self._strategy_walk_forward_plan(
                persisted_portfolio,
                evaluation_as_of_date=next(iter(as_of_dates)),
                data_snapshot_cutoff=next(iter(cutoffs)),
                decision_state=decision_state or {},
            )
            input_snapshot = self._strategy_walk_forward_input_snapshot(
                room_id,
                persisted_portfolio,
                plan,
                config,
                histories,
                decision_state or {},
            )
        else:
            plan = self._walk_forward_plan(
                persisted_portfolio,
                evaluation_as_of_date=next(iter(as_of_dates)),
                data_snapshot_cutoff=next(iter(cutoffs)),
            )
            input_snapshot = self._walk_forward_input_snapshot(
                room_id,
                persisted_portfolio,
                plan,
                config,
                histories,
            )
        result = run_walk_forward_backtest(
            histories,
            plan,
            config,
        )
        run = self.store.create_paper_portfolio_walk_forward_run(
            room_id,
            portfolio_id,
            result,
            input_snapshot,
            expected_portfolio_version=expected_portfolio_version,
            created_by=created_by,
        )
        if not run:
            raise LookupError("模拟组合不存在")
        return run

    def create(
        self,
        room_id: str,
        plan_value: Any,
        *,
        created_by: str = "user",
    ) -> dict[str, Any]:
        if not isinstance(plan_value, dict):
            raise ValueError("模拟组合必须是 JSON 对象")
        raw_plan = dict(plan_value)
        user_decision_id = str(raw_plan.pop("user_decision_id", "") or "").strip()
        derivation_note = str(raw_plan.pop("derivation_note", "") or "").strip()
        confirmation = raw_plan.pop("candidate_simulation_confirmation", None)
        plan = normalize_paper_portfolio_plan(raw_plan)
        candidate_contract, _context = self._candidate_contract_preflight(
            room_id,
            plan,
            confirmation,
            user_decision_id=user_decision_id,
        )
        evaluation = self._evaluate_normalized(plan)
        portfolio = self.store.create_paper_portfolio(
            room_id,
            plan,
            evaluation,
            created_by=created_by,
            user_decision_id=user_decision_id,
            derivation_note=derivation_note,
            candidate_simulation_contract_value=candidate_contract,
        )
        if not portfolio:
            raise ValueError("房间不存在")
        return portfolio

    def update(
        self,
        room_id: str,
        portfolio_id: str,
        plan_value: Any,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        if not isinstance(plan_value, dict):
            raise ValueError("模拟组合必须是 JSON 对象")
        raw_plan = dict(plan_value)
        confirmation = raw_plan.pop("candidate_simulation_confirmation", None)
        plan = normalize_paper_portfolio_plan(raw_plan)
        candidate_contract, context = self._candidate_contract_preflight(
            room_id,
            plan,
            confirmation,
            portfolio_id=portfolio_id,
        )
        context_portfolio = context.get("portfolio") or {}
        if int(context_portfolio.get("version") or 0) != int(expected_version):
            raise ValueError("模拟组合版本已变化，请重新载入后再保存")
        evaluation = self._evaluate_normalized(plan)
        portfolio = self.store.update_paper_portfolio(
            room_id,
            portfolio_id,
            plan,
            evaluation,
            expected_version=expected_version,
            candidate_simulation_contract_value=candidate_contract,
        )
        if not portfolio:
            raise LookupError("模拟组合不存在")
        return portfolio

    def reevaluate(
        self,
        room_id: str,
        portfolio_id: str,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        current = self.store.get_paper_portfolio(room_id, portfolio_id)
        if not current:
            raise LookupError("模拟组合不存在")
        return self.update(
            room_id,
            portfolio_id,
            {
                "name": current["name"],
                "positions": current["positions"],
                "budgets": current["budgets"],
                "stress_scenarios": current["stress_scenarios"],
            },
            expected_version=expected_version,
        )

    def confirm(
        self,
        room_id: str,
        portfolio_id: str,
        *,
        expected_version: int,
        confirmed_by: str = "user",
    ) -> dict[str, Any]:
        portfolio = self.store.confirm_paper_portfolio(
            room_id,
            portfolio_id,
            expected_version=expected_version,
            confirmed_by=confirmed_by,
        )
        if not portfolio:
            raise LookupError("模拟组合不存在")
        return portfolio
