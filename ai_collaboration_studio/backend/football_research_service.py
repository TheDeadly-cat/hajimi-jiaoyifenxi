from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterator, TYPE_CHECKING

from .domain_adapters import (
    DEFAULT_DOMAIN_ADAPTERS,
    DomainAdapterError,
    DomainAdapterRegistry,
)
from .football_research import (
    FOOTBALL_PROBABILITY_STATE,
    FOOTBALL_RESEARCH_CAPABILITY_PACK_ID,
    FootballResearchContractError,
    build_football_research_contract,
    canonical_sha256,
    validate_football_research_contract,
)
from .plugin_lifecycle import PluginLifecycleError
from .plugin_registry import HOST_UI_VIEW_MODEL_SCHEMAS

if TYPE_CHECKING:  # pragma: no cover
    from .store import StudioStore


FOOTBALL_RESEARCH_ACTION_ID = "football_research.inspect"
FOOTBALL_RESEARCH_ADAPTER_ID = "football_research"
FOOTBALL_RESEARCH_CONTRIBUTION_ID = "football_research.room_inspector/v1"
FOOTBALL_RESEARCH_PORT_ID = "core.football.match_context/v1"
FOOTBALL_RESEARCH_VIEW_MODEL_VERSION = "football_research_view_model_v1"

_VIEW_MODEL_KEYS = {
    "version",
    "integrity_ok",
    "metrics_visible",
    "room_id",
    "contract",
    "contract_sha256",
    "data_cutoff_utc",
    "probability_state",
    "future_probability_available",
    "probability_metrics_visible",
    "odds_are_proxy_only",
    "provider_calls_performed",
    "market_reads_performed",
    "business_writes_performed",
    "execution_capability",
    "live_trading_allowed",
    "betting_allowed",
    "automatic_betting_allowed",
    "wallet_connection_allowed",
    "order_placement_allowed",
    "can_autonomously_decide",
    "can_replace_user_decision",
    "user_final_decision_required",
}


class FootballResearchError(ValueError):
    """A typed, fail-closed football material inspection error."""

    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class FootballResearchService:
    """Read one material-bound football dossier from one SQLite snapshot.

    This service deliberately has no provider, market, wallet, execution, or
    persistence dependency. Its SQLite handle is opened with
    ``mode=ro&immutable=1`` and ``query_only`` and is used for both the room
    lifecycle check and every
    exact material-version verification.
    """

    def __init__(
        self,
        store: "StudioStore",
        domain_adapters: DomainAdapterRegistry = DEFAULT_DOMAIN_ADAPTERS,
    ) -> None:
        self.store = store
        self.domain_adapters = domain_adapters

    @staticmethod
    def _error(
        message: str,
        *,
        code: str,
        status: int = 409,
    ) -> FootballResearchError:
        return FootballResearchError(message, code=code, status=status)

    def _readonly_connection(self) -> sqlite3.Connection:
        path = Path(self.store.path).expanduser().resolve()
        if not path.is_file():
            raise self._error(
                "足球研究数据库不可用。",
                code="FOOTBALL_RESEARCH_DATABASE_UNAVAILABLE",
                status=503,
            )
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro&immutable=1",
                uri=True,
                timeout=20,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            query_only = connection.execute("PRAGMA query_only").fetchone()
            if not query_only or int(query_only[0]) != 1:
                connection.close()
                raise self._error(
                    "足球研究只读数据库保护未生效。",
                    code="FOOTBALL_RESEARCH_READONLY_GUARD_FAILED",
                    status=503,
                )
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN")
            return connection
        except FootballResearchError:
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise self._error(
                "足球研究数据库只读快照无法打开。",
                code="FOOTBALL_RESEARCH_DATABASE_UNAVAILABLE",
                status=503,
            ) from exc

    @staticmethod
    def _iter_material_bindings(value: Any) -> Iterator[dict[str, Any]]:
        if isinstance(value, dict):
            source = value.get("source")
            if isinstance(source, dict) and isinstance(
                source.get("material_binding"), dict
            ):
                yield source["material_binding"]
            for child in value.values():
                yield from FootballResearchService._iter_material_bindings(child)
        elif isinstance(value, list):
            for child in value:
                yield from FootballResearchService._iter_material_bindings(child)

    @staticmethod
    def _single_registry_binding(room: dict[str, Any]) -> dict[str, Any]:
        if (
            FOOTBALL_RESEARCH_CAPABILITY_PACK_ID
            not in (room.get("active_capability_pack_ids") or [])
            or room.get("plugin_registry_integrity_ok") is not True
        ):
            raise FootballResearchError(
                "当前房间没有可验证的足球只读能力包。",
                code="FOOTBALL_RESEARCH_ACTION_UNAVAILABLE",
                status=409,
            )
        snapshot = room.get("plugin_registry_snapshot")
        if not isinstance(snapshot, dict):
            raise FootballResearchError(
                "足球研究插件 registry 封印无效。",
                code="FOOTBALL_RESEARCH_BINDING_INVALID",
            )
        contributions = [
            row
            for row in snapshot.get("ui_contributions") or []
            if isinstance(row, dict)
            and str(row.get("contribution_id") or "")
            == FOOTBALL_RESEARCH_CONTRIBUTION_ID
        ]
        adapters = [
            row
            for row in snapshot.get("domain_adapters") or []
            if isinstance(row, dict)
            and str(row.get("adapter_id") or "") == FOOTBALL_RESEARCH_ADAPTER_ID
        ]
        resolutions = [
            row
            for row in snapshot.get("port_resolutions") or []
            if isinstance(row, dict)
            and str(row.get("owner_pack_id") or "")
            == FOOTBALL_RESEARCH_CAPABILITY_PACK_ID
            and str(row.get("port_id") or "") == FOOTBALL_RESEARCH_PORT_ID
            and row.get("requirement") == "required"
            and row.get("cardinality") == "one"
        ]
        if len(contributions) != 1 or len(adapters) != 1 or len(resolutions) != 1:
            raise FootballResearchError(
                "足球研究插件没有唯一的 UI、adapter 与端口绑定。",
                code="FOOTBALL_RESEARCH_BINDING_INVALID",
            )
        contribution = contributions[0]
        adapter = adapters[0]
        resolved = resolutions[0].get("resolved")
        if not isinstance(resolved, list) or len(resolved) != 1:
            raise FootballResearchError(
                "足球研究端口没有唯一精确实现。",
                code="FOOTBALL_RESEARCH_BINDING_INVALID",
            )
        port = resolved[0]
        source_port = contribution.get("source_port_resolution")
        view_model = contribution.get("view_model")
        expected_view_schema = HOST_UI_VIEW_MODEL_SCHEMAS.get(
            FOOTBALL_RESEARCH_VIEW_MODEL_VERSION
        )
        if (
            not isinstance(port, dict)
            or not isinstance(source_port, dict)
            or not isinstance(view_model, dict)
            or not isinstance(expected_view_schema, dict)
            or str(contribution.get("component_key") or "")
            != "football_research_inspector"
            or str(adapter.get("adapter_id") or "")
            != str(port.get("adapter_id") or "")
            or str(adapter.get("adapter_version") or "")
            != str(port.get("adapter_version") or "")
            or str(adapter.get("contract_sha256") or "")
            != str(port.get("adapter_contract_sha256") or "")
            or str(port.get("port_id") or "") != FOOTBALL_RESEARCH_PORT_ID
            or any(
                type(port.get(field)) is not int or port.get(field) != 0
                for field in (
                    "provider_call_budget",
                    "market_read_budget",
                    "business_write_budget",
                )
            )
            or str(port.get("failure_policy") or "") != "fail_closed"
            or str(source_port.get("owner_pack_id") or "")
            != FOOTBALL_RESEARCH_CAPABILITY_PACK_ID
            or any(
                str(source_port.get(field) or "") != str(port.get(field) or "")
                for field in (
                    "port_id",
                    "port_version",
                    "port_contract_sha256",
                    "output_schema_version",
                    "output_schema_sha256",
                )
            )
            or str(view_model.get("schema_version") or "")
            != FOOTBALL_RESEARCH_VIEW_MODEL_VERSION
            or str(view_model.get("schema_sha256") or "")
            != canonical_sha256(expected_view_schema)
            or set(expected_view_schema.get("required") or []) != _VIEW_MODEL_KEYS
            or set(expected_view_schema.get("fields") or {}) != _VIEW_MODEL_KEYS
            or expected_view_schema.get("additional_properties") is not False
        ):
            raise FootballResearchError(
                "足球研究的当前 registry 绑定与闭合视图合同不一致。",
                code="FOOTBALL_RESEARCH_BINDING_INVALID",
            )
        return port

    def _verify_material_bindings(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        contract: dict[str, Any],
    ) -> None:
        bindings = list(self._iter_material_bindings(contract))
        if not bindings:
            raise self._error(
                "足球研究合同没有材料绑定。",
                code="FOOTBALL_RESEARCH_MATERIAL_BINDING_MISSING",
            )
        for binding in bindings:
            material_id = str(binding.get("material_id") or "")
            material_version = int(binding.get("material_version") or 0)
            rows = connection.execute(
                """SELECT id,material_id,room_id,version,snapshot_json,changed_at
                     FROM material_versions
                    WHERE room_id=? AND material_id=? AND version=?""",
                (room_id, material_id, material_version),
            ).fetchall()
            if not rows:
                raise self._error(
                    f"精确材料版本不存在：{material_id} v{material_version}。",
                    code="FOOTBALL_RESEARCH_MATERIAL_VERSION_NOT_FOUND",
                    status=404,
                )
            if len(rows) != 1:
                raise self._error(
                    f"精确材料版本身份不唯一：{material_id} v{material_version}。",
                    code="FOOTBALL_RESEARCH_MATERIAL_VERSION_AMBIGUOUS",
                )
            try:
                raw_snapshot = json.loads(str(rows[0]["snapshot_json"] or ""))
                if not isinstance(raw_snapshot, dict):
                    raise ValueError("snapshot must be an object")
                snapshot = self.store._material_dict(raw_snapshot)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise self._error(
                    f"精确材料版本快照不可验证：{material_id} v{material_version}。",
                    code="FOOTBALL_RESEARCH_MATERIAL_SNAPSHOT_INVALID",
                ) from exc
            if (
                str(snapshot.get("id") or "") != material_id
                or str(snapshot.get("room_id") or "") != room_id
                or int(snapshot.get("version") or 0) != material_version
                or str(rows[0]["material_id"] or "") != material_id
                or str(rows[0]["room_id"] or "") != room_id
                or int(rows[0]["version"] or 0) != material_version
            ):
                raise self._error(
                    f"精确材料版本身份与快照不一致：{material_id} v{material_version}。",
                    code="FOOTBALL_RESEARCH_MATERIAL_SNAPSHOT_INVALID",
                )
            actual_content_sha256 = hashlib.sha256(
                str(snapshot.get("content") or "").encode("utf-8")
            ).hexdigest()
            if actual_content_sha256 != str(binding.get("content_sha256") or ""):
                raise self._error(
                    f"材料内容哈希漂移：{material_id} v{material_version}。",
                    code="FOOTBALL_RESEARCH_MATERIAL_CONTENT_DRIFT",
                )
            actual_snapshot_sha256 = self.store._material_snapshot_sha256(snapshot)
            if actual_snapshot_sha256 != str(binding.get("snapshot_sha256") or ""):
                raise self._error(
                    f"材料快照哈希漂移：{material_id} v{material_version}。",
                    code="FOOTBALL_RESEARCH_MATERIAL_SNAPSHOT_DRIFT",
                )

    @staticmethod
    def _closed_view_model(room_id: str, contract: dict[str, Any]) -> dict[str, Any]:
        view_model = {
            "version": FOOTBALL_RESEARCH_VIEW_MODEL_VERSION,
            "integrity_ok": True,
            "metrics_visible": False,
            "room_id": room_id,
            "contract": copy.deepcopy(contract),
            "contract_sha256": str(contract["contract_sha256"]),
            "data_cutoff_utc": str(contract["data_cutoff_utc"]),
            "probability_state": FOOTBALL_PROBABILITY_STATE,
            "future_probability_available": False,
            "probability_metrics_visible": False,
            "odds_are_proxy_only": True,
            "provider_calls_performed": 0,
            "market_reads_performed": 0,
            "business_writes_performed": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "betting_allowed": False,
            "automatic_betting_allowed": False,
            "wallet_connection_allowed": False,
            "order_placement_allowed": False,
            "can_autonomously_decide": False,
            "can_replace_user_decision": False,
            "user_final_decision_required": True,
        }
        if set(view_model) != _VIEW_MODEL_KEYS:
            raise FootballResearchError(
                "足球研究视图模型不是闭合合同。",
                code="FOOTBALL_RESEARCH_OUTPUT_INVALID",
            )
        return view_model

    def _validated_request(
        self,
        room_id: str,
        payload: Any,
    ) -> tuple[str, dict[str, Any]]:
        clean_room_id = str(room_id or "").strip()
        if not clean_room_id or len(clean_room_id) > 80:
            raise self._error(
                "足球研究房间标识无效。",
                code="FOOTBALL_RESEARCH_ROOM_ID_INVALID",
                status=400,
            )
        try:
            contract = (
                validate_football_research_contract(payload)
                if isinstance(payload, dict) and "contract_sha256" in payload
                else build_football_research_contract(payload)
            )
        except FootballResearchContractError as exc:
            raise self._error(
                "足球研究合同无效。",
                code="FOOTBALL_RESEARCH_CONTRACT_INVALID",
                status=400,
            ) from exc
        return clean_room_id, contract

    def _inspect_snapshot(
        self,
        connection: sqlite3.Connection,
        clean_room_id: str,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            room = self.store._require_room_plugin_action_connection(
                connection,
                clean_room_id,
                FOOTBALL_RESEARCH_ACTION_ID,
            )
        except LookupError as exc:
            raise self._error(
                "足球研究房间不存在。",
                code="FOOTBALL_RESEARCH_ROOM_NOT_FOUND",
                status=404,
            ) from exc
        except PluginLifecycleError as exc:
            raise self._error(
                "足球研究动作在当前插件生命周期下不可用。",
                code="FOOTBALL_RESEARCH_ACTION_UNAVAILABLE",
            ) from exc

        port_resolution = self._single_registry_binding(room)
        try:
            adapter = self.domain_adapters.require_port_resolution(
                port_resolution
            )
            project = getattr(adapter, "project_football_match_context", None)
            if not callable(project):
                raise DomainAdapterError("football projection port is missing")
            projected_contract = project(contract=copy.deepcopy(contract))
        except (DomainAdapterError, TypeError, ValueError) as exc:
            raise self._error(
                "足球研究 adapter 精确实现不可用或输出无效。",
                code="FOOTBALL_RESEARCH_IMPLEMENTATION_UNAVAILABLE",
            ) from exc

        self._verify_material_bindings(
            connection,
            clean_room_id,
            projected_contract,
        )
        return self._closed_view_model(clean_room_id, projected_contract)

    def inspect_from_connection(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        payload: Any,
    ) -> dict[str, Any]:
        """Revalidate inside a caller-owned SQLite transaction snapshot.

        The supplied connection is never begun, committed, rolled back, closed,
        or reconfigured.  Formal round context code can therefore reuse the
        exact Store transaction that freezes its other inputs.
        """

        if not isinstance(connection, sqlite3.Connection):
            raise self._error(
                "足球研究需要 Store SQLite 连接。",
                code="FOOTBALL_RESEARCH_CONNECTION_INVALID",
                status=400,
            )
        if not connection.in_transaction:
            raise self._error(
                "足球研究必须复用已开始的 SQLite 事务快照。",
                code="FOOTBALL_RESEARCH_SNAPSHOT_REQUIRED",
                status=409,
            )
        clean_room_id, contract = self._validated_request(room_id, payload)
        changes_before = int(connection.total_changes)
        try:
            result = self._inspect_snapshot(
                connection,
                clean_room_id,
                contract,
            )
        except FootballResearchError:
            raise
        except sqlite3.Error as exc:
            raise self._error(
                "足球研究 SQLite 快照无法验证。",
                code="FOOTBALL_RESEARCH_DATABASE_SNAPSHOT_INVALID",
                status=409,
            ) from exc
        if int(connection.total_changes) != changes_before:
            raise self._error(
                "足球研究只读检查检测到数据库写入。",
                code="FOOTBALL_RESEARCH_WRITE_GUARD_FAILED",
                status=500,
            )
        return result

    def inspect(self, room_id: str, payload: Any) -> dict[str, Any]:
        clean_room_id, contract = self._validated_request(room_id, payload)
        with closing(self._readonly_connection()) as connection:
            changes_before = int(connection.total_changes)
            try:
                result = self._inspect_snapshot(
                    connection,
                    clean_room_id,
                    contract,
                )
            except FootballResearchError:
                raise
            except sqlite3.Error as exc:
                raise self._error(
                    "足球研究 SQLite 只读快照无法验证。",
                    code="FOOTBALL_RESEARCH_DATABASE_SNAPSHOT_INVALID",
                    status=409,
                ) from exc
            if int(connection.total_changes) != changes_before:
                raise self._error(
                    "足球研究只读检查检测到数据库写入。",
                    code="FOOTBALL_RESEARCH_WRITE_GUARD_FAILED",
                    status=500,
                )
            return result


__all__ = [
    "FOOTBALL_RESEARCH_ACTION_ID",
    "FOOTBALL_RESEARCH_ADAPTER_ID",
    "FOOTBALL_RESEARCH_CONTRIBUTION_ID",
    "FOOTBALL_RESEARCH_PORT_ID",
    "FOOTBALL_RESEARCH_VIEW_MODEL_VERSION",
    "FootballResearchError",
    "FootballResearchService",
]
