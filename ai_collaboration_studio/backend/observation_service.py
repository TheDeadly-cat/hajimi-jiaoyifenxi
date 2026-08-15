from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .market.futu_readonly import (
    STORAGE_SYMBOLS,
    validate_readonly_daily_history,
    validate_storage_quote_snapshot,
)
from .market.storage_service import STORAGE_MARKET, StorageResearchMarketService
from .store import OBSERVATION_MEASUREMENT_METHOD, STORE, StudioStore


US_EASTERN = ZoneInfo("America/New_York")
OBSERVATION_FORWARD_WINDOW_CALENDAR_DAYS = 180
OBSERVATION_FORWARD_HISTORY_LIMIT = 256


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=US_EASTERN)
    return parsed.astimezone(timezone.utc)


class ObservationService:
    """User-confirmed, read-only market observations with delayed empirical scoring."""

    def __init__(
        self,
        store: StudioStore = STORE,
        market_service: StorageResearchMarketService = STORAGE_MARKET,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.market_service = market_service
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create(self, room_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        observation = self.store.create_observation(room_id, {**payload, "created_by": "user"})
        if not observation:
            raise ValueError("房间不存在")
        return observation

    def bind_decision_lineage(
        self,
        room_id: str,
        observation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = self.store.bind_observation_decision_lineage(
            room_id,
            observation_id,
            user_decision_id=str(payload.get("user_decision_id") or ""),
            source_portfolio_id=str(payload.get("source_portfolio_id") or ""),
            source_portfolio_version=payload.get("source_portfolio_version") or 0,
            derivation_note=str(payload.get("derivation_note") or ""),
            bound_by="user",
        )
        if not result:
            raise LookupError("模拟观察不存在")
        return result

    def confirm(self, room_id: str, observation_id: str) -> dict[str, Any]:
        observation = self.store.get_observation(room_id, observation_id)
        if not observation:
            raise ValueError("模拟观察不存在")
        if observation["status"] != "PROPOSED":
            return observation
        snapshot = self.market_service.snapshot(force=True)
        baseline = self._baseline_from_snapshot(observation, snapshot)
        confirmed = self.store.confirm_observation(room_id, observation_id, baseline)
        if not confirmed:
            raise ValueError("模拟观察不存在")
        return confirmed

    def reconcile(self, room_id: str) -> dict[str, Any]:
        observations = self.store.list_observations(room_id)
        pending = [row for row in observations if row["status"] == "PENDING_BASELINE" and row["user_confirmed"]]
        if pending:
            snapshot = self.market_service.snapshot(force=True)
            for observation in pending:
                baseline = self._baseline_from_snapshot(observation, snapshot)
                if baseline:
                    self.store.set_observation_baseline(room_id, observation["id"], baseline)

        now = self._clock().astimezone(timezone.utc)
        open_observations = [
            row for row in self.store.list_observations(room_id)
            if row["status"] == "OPEN" and row["user_confirmed"]
        ]
        for observation in open_observations:
            self._try_resolve(room_id, observation, now)
        return {
            "observations": self.store.list_observations(room_id),
            "reflections": self.store.list_reflections(room_id),
            "scorecard": self.store.observation_scorecard(room_id),
        }

    @staticmethod
    def _baseline_from_snapshot(observation: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
        if not validate_storage_quote_snapshot(snapshot)["ready"]:
            return None
        target = next(
            (row for row in snapshot.get("rows") or [] if row.get("symbol") == observation.get("symbol")),
            None,
        )
        if not target:
            return None
        price = _finite_positive(target.get("last"))
        market_time = str(
            target.get("updated_at")
            or target.get("market_time")
            or snapshot.get("captured_at")
            or ""
        ).strip()
        if price is None or _parse_time(market_time) is None:
            return None
        peer_baselines: list[dict[str, Any]] = []
        for row in snapshot.get("rows") or []:
            symbol = str(row.get("symbol") or "").upper()
            if symbol == observation.get("symbol") or symbol not in STORAGE_SYMBOLS:
                continue
            peer_price = _finite_positive(row.get("last"))
            peer_time = str(row.get("updated_at") or row.get("market_time") or "").strip()
            if peer_price is None or _parse_time(peer_time) is None:
                continue
            peer_baselines.append({"symbol": symbol, "price": peer_price, "market_time": peer_time})
        return {
            "price": price,
            "time": market_time,
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "benchmark": {
                "version": "storage_peer_benchmark_v1",
                "definition": "目标股票之外可用白名单同行的等权收益",
                "snapshot_id": str(snapshot.get("snapshot_id") or ""),
                "captured_at": str(snapshot.get("captured_at") or ""),
                "peers": peer_baselines,
                "minimum_peers": 2,
            },
        }

    def _try_resolve(self, room_id: str, observation: dict[str, Any], now: datetime) -> None:
        baseline_time = _parse_time(observation.get("baseline_time"))
        if baseline_time is None or _finite_positive(observation.get("baseline_price")) is None:
            return
        baseline_market_date = baseline_time.astimezone(US_EASTERN).date()
        end_date = min(
            now.astimezone(US_EASTERN).date(),
            baseline_market_date + timedelta(
                days=OBSERVATION_FORWARD_WINDOW_CALENDAR_DAYS
            ),
        )
        if end_date <= baseline_market_date:
            return
        try:
            history = self.market_service.history(
                observation["symbol"],
                start=baseline_market_date.isoformat(),
                end=end_date.isoformat(),
                limit=OBSERVATION_FORWARD_HISTORY_LIMIT,
            )
        except Exception:
            return
        history_contract = validate_readonly_daily_history(
            history,
            expected_symbol=str(observation["symbol"]),
            expected_start=baseline_market_date.isoformat(),
            expected_end=end_date.isoformat(),
        )
        if history_contract.get("ready") is not True:
            return
        baseline_row: tuple[datetime, dict[str, Any]] | None = None
        candidates: list[tuple[datetime, dict[str, Any]]] = []
        for row in history.get("rows") or []:
            row_time = _parse_time(row.get("time") or row.get("market_time"))
            close = _finite_positive(row.get("close"))
            if row_time is None or close is None:
                continue
            row_date = row_time.astimezone(US_EASTERN).date()
            if row_time > now:
                continue
            if row_date == baseline_market_date:
                baseline_row = (row_time, row)
            elif row_date > baseline_market_date:
                candidates.append((row_time, row))
        if baseline_row is None:
            return
        candidates.sort(key=lambda item: item[0])
        horizon = int(observation["horizon_days"])
        if len(candidates) < horizon:
            return
        scoring_baseline_time, scoring_baseline_row = baseline_row
        scoring_baseline_price = float(scoring_baseline_row["close"])
        outcome_time, outcome_row = candidates[horizon - 1]
        outcome_price = float(outcome_row["close"])
        return_pct = (outcome_price / scoring_baseline_price - 1) * 100
        threshold = float(observation.get("threshold_pct") or 0)
        direction = observation["direction"]
        if direction == "UP":
            hit = return_pct >= threshold
        elif direction == "DOWN":
            hit = return_pct <= -threshold
        else:
            hit = abs(return_pct) < threshold
        benchmark_result, relative_return_pct, relative_hit = self._benchmark_outcome(
            observation,
            outcome_time,
            return_pct,
            now,
        )
        self.store.resolve_observation(
            room_id,
            observation["id"],
            outcome_price=outcome_price,
            outcome_time=str(outcome_row.get("market_time") or outcome_time.isoformat()),
            return_pct=return_pct,
            hit=hit,
            note=(
                f"按第 {horizon} 个后续交易日收盘验证；来源=futu_opend；"
                f"同行基准={benchmark_result.get('state', 'unavailable')}"
            ),
            benchmark_result=benchmark_result,
            relative_return_pct=relative_return_pct,
            relative_hit=relative_hit,
            measurement_method=OBSERVATION_MEASUREMENT_METHOD,
            scoring_baseline_price=scoring_baseline_price,
            scoring_baseline_time=str(
                scoring_baseline_row.get("market_time")
                or scoring_baseline_time.isoformat()
            ),
        )

    def _benchmark_outcome(
        self,
        observation: dict[str, Any],
        target_outcome_time: datetime,
        target_return_pct: float,
        now: datetime,
    ) -> tuple[dict[str, Any], float | None, bool | None]:
        baseline = observation.get("benchmark_baseline") if isinstance(observation.get("benchmark_baseline"), dict) else {}
        peer_baselines = baseline.get("peers") if isinstance(baseline.get("peers"), list) else []
        minimum_peers = max(2, int(baseline.get("minimum_peers") or 2))
        target_date = target_outcome_time.astimezone(US_EASTERN).date()
        result: dict[str, Any] = {
            "version": "storage_peer_benchmark_v1",
            "definition": "目标股票之外可用白名单同行的等权收益",
            "state": "unavailable",
            "target_symbol": observation.get("symbol"),
            "target_outcome_time": target_outcome_time.isoformat(),
            "minimum_peers": minimum_peers,
            "peers": [],
            "source_errors": [],
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "measurement_method": OBSERVATION_MEASUREMENT_METHOD,
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        for peer in peer_baselines[:3]:
            if not isinstance(peer, dict):
                continue
            symbol = str(peer.get("symbol") or "").upper()
            baseline_time = _parse_time(peer.get("market_time"))
            if (
                symbol not in STORAGE_SYMBOLS
                or _finite_positive(peer.get("price")) is None
                or baseline_time is None
            ):
                continue
            baseline_date = baseline_time.astimezone(US_EASTERN).date()
            try:
                history = self.market_service.history(
                    symbol,
                    start=baseline_date.isoformat(),
                    end=target_date.isoformat(),
                    limit=OBSERVATION_FORWARD_HISTORY_LIMIT,
                )
            except Exception as exc:
                result["source_errors"].append({"symbol": symbol, "code": "PEER_HISTORY_ERROR", "message": str(exc)[:240]})
                continue
            history_contract = validate_readonly_daily_history(
                history,
                expected_symbol=symbol,
                expected_start=baseline_date.isoformat(),
                expected_end=target_date.isoformat(),
            )
            if history_contract.get("ready") is not True:
                result["source_errors"].append({
                    "symbol": symbol,
                    "code": "PEER_HISTORY_CONTRACT_INVALID",
                    "message": "同行日线未通过只读 Futu 1d/QFQ 历史契约",
                    "issues": list(history_contract.get("issues") or []),
                })
                continue
            baseline_row = None
            outcome_row = None
            for row in history.get("rows") or []:
                row_time = _parse_time(row.get("time") or row.get("market_time"))
                close = _finite_positive(row.get("close"))
                if row_time is None or close is None or row_time > now:
                    continue
                row_date = row_time.astimezone(US_EASTERN).date()
                if row_date == baseline_date:
                    baseline_row = (row_time, close)
                if row_date == target_date:
                    outcome_row = (row_time, close)
            if not baseline_row or not outcome_row:
                result["source_errors"].append({
                    "symbol": symbol,
                    "code": "PEER_MEASUREMENT_WINDOW_MISSING",
                    "message": (
                        f"同行缺少基准日 {baseline_date.isoformat()} 或"
                        f"到期日 {target_date.isoformat()} 的同口径 QFQ 收盘价"
                    ),
                })
                continue
            peer_baseline_time, peer_baseline_price = baseline_row
            peer_time, peer_close = outcome_row
            peer_return = (peer_close / peer_baseline_price - 1) * 100
            result["peers"].append({
                "symbol": symbol,
                "baseline_price": peer_baseline_price,
                "baseline_time": peer_baseline_time.isoformat(),
                "outcome_price": peer_close,
                "outcome_time": peer_time.isoformat(),
                "return_pct": round(peer_return, 8),
            })

        if len(result["peers"]) < minimum_peers:
            result["peer_count"] = len(result["peers"])
            return result, None, None
        peer_return_pct = sum(float(peer["return_pct"]) for peer in result["peers"]) / len(result["peers"])
        relative_return_pct = target_return_pct - peer_return_pct
        threshold = float(observation.get("threshold_pct") or 0)
        direction = observation.get("direction")
        if direction == "UP":
            relative_hit = relative_return_pct >= threshold
        elif direction == "DOWN":
            relative_hit = relative_return_pct <= -threshold
        else:
            relative_hit = abs(relative_return_pct) < threshold
        result.update({
            "state": "ready" if len(result["peers"]) == 3 else "limited",
            "peer_count": len(result["peers"]),
            "peer_equal_weight_return_pct": round(peer_return_pct, 8),
            "target_return_pct": round(target_return_pct, 8),
            "relative_return_pct": round(relative_return_pct, 8),
            "relative_hit": relative_hit,
        })
        return result, relative_return_pct, relative_hit


OBSERVATIONS = ObservationService()
