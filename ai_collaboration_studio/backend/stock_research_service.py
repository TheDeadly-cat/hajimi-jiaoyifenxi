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
from .plugin_lifecycle import PluginLifecycleError
from .plugin_registry import (
    HOST_UI_VIEW_MODEL_SCHEMAS,
    plugin_registry_catalog,
)
from .stock_research import (
    STOCK_PREFLIGHT_SOURCE_TYPES,
    STOCK_RESEARCH_CAPABILITY_PACK_ID,
    StockResearchContractError,
    build_stock_research_contract,
    canonical_sha256,
    validate_stock_research_contract,
    validate_stock_room_scope,
)

if TYPE_CHECKING:  # pragma: no cover
    from .store import StudioStore


STOCK_RESEARCH_ACTION_ID = "stock_research.inspect"
STOCK_RESEARCH_ADAPTER_ID = "stock_research"
STOCK_RESEARCH_CONTRIBUTION_ID = "stock_research.room_inspector/v1"
STOCK_RESEARCH_PORT_ID = "core.market.readonly_context/v1"
STOCK_RESEARCH_VIEW_MODEL_VERSION = "stock_research_view_model_v1"
STOCK_SYMBOL_PREFLIGHT_VIEW_VERSION = "stock_symbol_preflight_view_v1"

_PREFLIGHT_SUMMARY_KEYS = {"status", "as_of_utc", "reason"}
_SYMBOL_PREFLIGHT_KEYS = {
    "version",
    "symbol",
    "research_ready",
    *STOCK_PREFLIGHT_SOURCE_TYPES,
}
_VIEW_MODEL_KEYS = {
    "version",
    "integrity_ok",
    "metrics_visible",
    "room_id",
    "stock_room_scope",
    "contract",
    "contract_sha256",
    "data_cutoff_utc",
    "research_ready",
    "symbol_preflights",
    "provider_calls_performed",
    "market_reads_performed",
    "business_writes_performed",
    "execution_capability",
    "live_trading_allowed",
    "order_placement_allowed",
    "wallet_connection_allowed",
    "automatic_trading_allowed",
    "can_autonomously_decide",
    "can_replace_user_decision",
    "user_final_decision_required",
}


class StockResearchError(ValueError):
    """A typed, fail-closed stock scope and material inspection error."""

    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class StockResearchService:
    """Inspect one room-scoped stock seal without any external data access.

    The owned connection is SQLite ``mode=ro&immutable=1`` plus ``query_only``
    and one explicit read transaction. The caller-owned API requires an already-open
    transaction so a future formal-round freezer can revalidate and persist in
    one Store snapshot without this service committing, rolling back, or
    reconfiguring its connection.
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
    ) -> StockResearchError:
        return StockResearchError(message, code=code, status=status)

    def _readonly_connection(self) -> sqlite3.Connection:
        path = Path(self.store.path).expanduser().resolve()
        if not path.is_file():
            raise self._error(
                "Stock research database is unavailable.",
                code="STOCK_RESEARCH_DATABASE_UNAVAILABLE",
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
                    "Stock research SQLite query-only guard did not activate.",
                    code="STOCK_RESEARCH_READONLY_GUARD_FAILED",
                    status=503,
                )
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN")
            return connection
        except StockResearchError:
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise self._error(
                "Stock research read-only SQLite snapshot could not open.",
                code="STOCK_RESEARCH_DATABASE_UNAVAILABLE",
                status=503,
            ) from exc

    @staticmethod
    def _iter_sources(value: Any) -> Iterator[dict[str, Any]]:
        if isinstance(value, dict):
            source = value.get("source")
            if isinstance(source, dict) and isinstance(
                source.get("material_binding"), dict
            ):
                yield source
            for child in value.values():
                yield from StockResearchService._iter_sources(child)
        elif isinstance(value, list):
            for child in value:
                yield from StockResearchService._iter_sources(child)

    @staticmethod
    def _iter_material_bindings(value: Any) -> Iterator[dict[str, Any]]:
        for source in StockResearchService._iter_sources(value):
            binding = source.get("material_binding")
            if isinstance(binding, dict):
                yield binding

    @staticmethod
    def _single_registry_binding(room: dict[str, Any]) -> dict[str, Any]:
        if (
            STOCK_RESEARCH_CAPABILITY_PACK_ID
            not in (room.get("active_capability_pack_ids") or [])
            or room.get("plugin_registry_integrity_ok") is not True
        ):
            raise StockResearchError(
                "The room has no verified active stock research pack.",
                code="STOCK_RESEARCH_ACTION_UNAVAILABLE",
            )
        current = room.get("plugin_lifecycle_current")
        if (
            not isinstance(current, dict)
            or current.get("integrity_ok") is not True
            or STOCK_RESEARCH_ACTION_ID
            not in (current.get("available_action_ids") or [])
        ):
            raise StockResearchError(
                "The stock research action is unavailable.",
                code="STOCK_RESEARCH_ACTION_UNAVAILABLE",
            )
        snapshot = room.get("plugin_registry_snapshot")
        if not isinstance(snapshot, dict):
            raise StockResearchError(
                "The stock research registry snapshot is invalid.",
                code="STOCK_RESEARCH_BINDING_INVALID",
            )

        packs = [
            row
            for row in snapshot.get("capability_packs") or []
            if isinstance(row, dict)
            and str(row.get("id") or "") == STOCK_RESEARCH_CAPABILITY_PACK_ID
        ]
        contributions = [
            row
            for row in snapshot.get("ui_contributions") or []
            if isinstance(row, dict)
            and str(row.get("contribution_id") or "")
            == STOCK_RESEARCH_CONTRIBUTION_ID
        ]
        adapters = [
            row
            for row in snapshot.get("domain_adapters") or []
            if isinstance(row, dict)
            and str(row.get("adapter_id") or "") == STOCK_RESEARCH_ADAPTER_ID
        ]
        resolutions = [
            row
            for row in snapshot.get("port_resolutions") or []
            if isinstance(row, dict)
            and str(row.get("owner_pack_id") or "")
            == STOCK_RESEARCH_CAPABILITY_PACK_ID
            and str(row.get("port_id") or "") == STOCK_RESEARCH_PORT_ID
            and row.get("requirement") == "required"
            and row.get("cardinality") == "one"
        ]
        if not all(len(rows) == 1 for rows in (
            packs,
            contributions,
            adapters,
            resolutions,
        )):
            raise StockResearchError(
                "Stock research has no unique pack, UI, adapter, and port binding.",
                code="STOCK_RESEARCH_BINDING_INVALID",
            )
        resolved = resolutions[0].get("resolved")
        if not isinstance(resolved, list) or len(resolved) != 1:
            raise StockResearchError(
                "Stock research port has no unique exact implementation.",
                code="STOCK_RESEARCH_BINDING_INVALID",
            )

        pack = packs[0]
        contribution = contributions[0]
        adapter = adapters[0]
        port = resolved[0]
        source_port = contribution.get("source_port_resolution")
        view_model = contribution.get("view_model")
        expected_view_schema = HOST_UI_VIEW_MODEL_SCHEMAS.get(
            STOCK_RESEARCH_VIEW_MODEL_VERSION
        )
        try:
            catalog = plugin_registry_catalog()
        except Exception as exc:
            raise StockResearchError(
                "The current stock research registry catalog is invalid.",
                code="STOCK_RESEARCH_BINDING_INVALID",
            ) from exc
        catalog_pack = next((
            row
            for row in catalog.get("capability_packs") or []
            if row.get("id") == STOCK_RESEARCH_CAPABILITY_PACK_ID
        ), None)
        catalog_adapter = next((
            row
            for row in catalog.get("domain_adapters") or []
            if row.get("adapter_id") == STOCK_RESEARCH_ADAPTER_ID
        ), None)
        catalog_port = next((
            row
            for row in catalog.get("domain_adapter_ports") or []
            if row.get("port_id") == STOCK_RESEARCH_PORT_ID
        ), None)
        catalog_contribution = next((
            row
            for row in catalog.get("ui_contributions") or []
            if row.get("contribution_id") == STOCK_RESEARCH_CONTRIBUTION_ID
        ), None)
        if any(value is None for value in (
            catalog_pack,
            catalog_adapter,
            catalog_port,
            catalog_contribution,
        )):
            raise StockResearchError(
                "The current stock research registry contracts are incomplete.",
                code="STOCK_RESEARCH_BINDING_INVALID",
            )

        if (
            not isinstance(port, dict)
            or not isinstance(source_port, dict)
            or not isinstance(view_model, dict)
            or not isinstance(expected_view_schema, dict)
            or pack.get("domain_adapter_ids") != [STOCK_RESEARCH_ADAPTER_ID]
            or pack.get("ui_contribution_ids")
            != [STOCK_RESEARCH_CONTRIBUTION_ID]
            or str(pack.get("manifest_sha256") or "")
            != str(catalog_pack.get("manifest_sha256") or "")
            or str(contribution.get("component_key") or "")
            != "stock_research_inspector"
            or str(contribution.get("contract_sha256") or "")
            != str(catalog_contribution.get("contract_sha256") or "")
            or catalog_contribution.get("allowed_actions")
            != [STOCK_RESEARCH_ACTION_ID]
            or str(catalog_contribution.get("pack_id") or "")
            != STOCK_RESEARCH_CAPABILITY_PACK_ID
            or str(adapter.get("adapter_id") or "")
            != str(port.get("adapter_id") or "")
            or str(adapter.get("adapter_version") or "")
            != str(port.get("adapter_version") or "")
            or str(adapter.get("contract_sha256") or "")
            != str(port.get("adapter_contract_sha256") or "")
            or str(adapter.get("contract_sha256") or "")
            != str(catalog_adapter.get("contract_sha256") or "")
            or str(port.get("port_id") or "") != STOCK_RESEARCH_PORT_ID
            or str(port.get("handler_method") or "")
            != "project_market_readonly_context"
            or str(port.get("port_contract_sha256") or "")
            != str(catalog_port.get("contract_sha256") or "")
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
            != STOCK_RESEARCH_CAPABILITY_PACK_ID
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
            != STOCK_RESEARCH_VIEW_MODEL_VERSION
            or str(view_model.get("schema_sha256") or "")
            != canonical_sha256(expected_view_schema)
            or set(expected_view_schema.get("required") or [])
            != _VIEW_MODEL_KEYS
            or set(expected_view_schema.get("fields") or {})
            != _VIEW_MODEL_KEYS
            or expected_view_schema.get("additional_properties") is not False
        ):
            raise StockResearchError(
                "The stock pack/action/UI/adapter/port binding does not match.",
                code="STOCK_RESEARCH_BINDING_INVALID",
            )
        return copy.deepcopy(port)

    def _verify_material_bindings(
        self,
        connection: sqlite3.Connection,
        room_id: str,
        contract: dict[str, Any],
    ) -> None:
        sources = list(self._iter_sources(contract))
        if not sources:
            raise self._error(
                "The stock research contract has no material-bound sources.",
                code="STOCK_RESEARCH_MATERIAL_BINDING_MISSING",
            )
        for source in sources:
            binding = source.get("material_binding") or {}
            material_id = str(binding.get("material_id") or "")
            raw_version = binding.get("material_version")
            if (
                type(raw_version) is not int
                or raw_version < 1
                or raw_version > 2_147_483_647
            ):
                raise self._error(
                    "Stock material version must be an exact positive integer.",
                    code="STOCK_RESEARCH_MATERIAL_VERSION_INVALID",
                    status=400,
                )
            material_version = raw_version
            if str(source.get("source_sha256") or "") != str(
                binding.get("content_sha256") or ""
            ):
                raise self._error(
                    "Stock source and material content hashes do not match.",
                    code="STOCK_RESEARCH_MATERIAL_CONTENT_DRIFT",
                )
            rows = connection.execute(
                """SELECT id,material_id,room_id,version,snapshot_json,changed_at
                     FROM material_versions
                    WHERE room_id=? AND material_id=? AND version=?""",
                (room_id, material_id, material_version),
            ).fetchall()
            if not rows:
                raise self._error(
                    f"Exact stock material does not exist: {material_id} "
                    f"v{material_version}.",
                    code="STOCK_RESEARCH_MATERIAL_VERSION_NOT_FOUND",
                    status=404,
                )
            if len(rows) != 1:
                raise self._error(
                    f"Exact stock material identity is ambiguous: {material_id} "
                    f"v{material_version}.",
                    code="STOCK_RESEARCH_MATERIAL_VERSION_AMBIGUOUS",
                )
            try:
                raw_snapshot = json.loads(str(rows[0]["snapshot_json"] or ""))
                if not isinstance(raw_snapshot, dict):
                    raise ValueError("snapshot must be an object")
                snapshot = self.store._material_dict(raw_snapshot)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise self._error(
                    f"Exact stock material snapshot is invalid: {material_id} "
                    f"v{material_version}.",
                    code="STOCK_RESEARCH_MATERIAL_SNAPSHOT_INVALID",
                ) from exc
            if (
                str(snapshot.get("id") or "") != material_id
                or str(snapshot.get("room_id") or "") != room_id
                or type(snapshot.get("version")) is not int
                or snapshot.get("version") != material_version
                or str(rows[0]["material_id"] or "") != material_id
                or str(rows[0]["room_id"] or "") != room_id
                or type(rows[0]["version"]) is not int
                or rows[0]["version"] != material_version
            ):
                raise self._error(
                    f"Stock material identity and snapshot differ: {material_id} "
                    f"v{material_version}.",
                    code="STOCK_RESEARCH_MATERIAL_SNAPSHOT_INVALID",
                )
            actual_content_sha256 = hashlib.sha256(
                str(snapshot.get("content") or "").encode("utf-8")
            ).hexdigest()
            if actual_content_sha256 != str(
                binding.get("content_sha256") or ""
            ):
                raise self._error(
                    f"Stock material content hash drifted: {material_id} "
                    f"v{material_version}.",
                    code="STOCK_RESEARCH_MATERIAL_CONTENT_DRIFT",
                )
            actual_snapshot_sha256 = self.store._material_snapshot_sha256(snapshot)
            if actual_snapshot_sha256 != str(
                binding.get("snapshot_sha256") or ""
            ):
                raise self._error(
                    f"Stock material snapshot hash drifted: {material_id} "
                    f"v{material_version}.",
                    code="STOCK_RESEARCH_MATERIAL_SNAPSHOT_DRIFT",
                )

    @staticmethod
    def _symbol_preflight_views(
        contract: dict[str, Any],
    ) -> list[dict[str, Any]]:
        views: list[dict[str, Any]] = []
        for stock in contract.get("symbols") or []:
            preflight = stock.get("preflight") if isinstance(stock, dict) else None
            if not isinstance(preflight, dict):
                raise StockResearchError(
                    "Stock preflight projection is invalid.",
                    code="STOCK_RESEARCH_OUTPUT_INVALID",
                    status=500,
                )
            view: dict[str, Any] = {
                "version": STOCK_SYMBOL_PREFLIGHT_VIEW_VERSION,
                "symbol": str(stock.get("symbol") or ""),
            }
            ready = True
            for source_type in STOCK_PREFLIGHT_SOURCE_TYPES:
                source = preflight.get(source_type)
                if not isinstance(source, dict):
                    raise StockResearchError(
                        "Stock preflight source projection is invalid.",
                        code="STOCK_RESEARCH_OUTPUT_INVALID",
                        status=500,
                    )
                summary = {
                    "status": str(source.get("status") or ""),
                    "as_of_utc": str(source.get("as_of_utc") or ""),
                    "reason": str(source.get("reason") or ""),
                }
                if set(summary) != _PREFLIGHT_SUMMARY_KEYS:
                    raise StockResearchError(
                        "Stock preflight summary is not closed.",
                        code="STOCK_RESEARCH_OUTPUT_INVALID",
                        status=500,
                    )
                ready = ready and summary["status"] == "ready"
                view[source_type] = summary
            view["research_ready"] = ready
            if set(view) != _SYMBOL_PREFLIGHT_KEYS:
                raise StockResearchError(
                    "Stock symbol preflight view is not closed.",
                    code="STOCK_RESEARCH_OUTPUT_INVALID",
                    status=500,
                )
            views.append(view)
        if [row["symbol"] for row in views] != sorted(
            row["symbol"] for row in views
        ):
            raise StockResearchError(
                "Stock symbol preflight views are not canonical.",
                code="STOCK_RESEARCH_OUTPUT_INVALID",
                status=500,
            )
        return views

    @classmethod
    def _closed_view_model(
        cls,
        room_id: str,
        contract: dict[str, Any],
    ) -> dict[str, Any]:
        symbol_preflights = cls._symbol_preflight_views(contract)
        research_ready = bool(
            symbol_preflights
            and all(row["research_ready"] for row in symbol_preflights)
        )
        if research_ready is not contract.get("research_ready"):
            raise StockResearchError(
                "Stock preflight projection disagrees with the sealed contract.",
                code="STOCK_RESEARCH_OUTPUT_INVALID",
                status=500,
            )
        view_model = {
            "version": STOCK_RESEARCH_VIEW_MODEL_VERSION,
            "integrity_ok": True,
            "metrics_visible": True,
            "room_id": room_id,
            "stock_room_scope": copy.deepcopy(contract["stock_room_scope"]),
            "contract": copy.deepcopy(contract),
            "contract_sha256": str(contract["contract_sha256"]),
            "data_cutoff_utc": str(contract["data_cutoff_utc"]),
            "research_ready": research_ready,
            "symbol_preflights": symbol_preflights,
            "provider_calls_performed": 0,
            "market_reads_performed": 0,
            "business_writes_performed": 0,
            "execution_capability": "none",
            "live_trading_allowed": False,
            "order_placement_allowed": False,
            "wallet_connection_allowed": False,
            "automatic_trading_allowed": False,
            "can_autonomously_decide": False,
            "can_replace_user_decision": False,
            "user_final_decision_required": True,
        }
        if set(view_model) != _VIEW_MODEL_KEYS:
            raise StockResearchError(
                "Stock research view model is not closed.",
                code="STOCK_RESEARCH_OUTPUT_INVALID",
                status=500,
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
                "Stock research room id is invalid.",
                code="STOCK_RESEARCH_ROOM_ID_INVALID",
                status=400,
            )
        try:
            contract = (
                validate_stock_research_contract(payload)
                if isinstance(payload, dict) and "contract_sha256" in payload
                else build_stock_research_contract(payload)
            )
        except StockResearchContractError as exc:
            raise self._error(
                "Stock research contract is invalid.",
                code="STOCK_RESEARCH_CONTRACT_INVALID",
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
                STOCK_RESEARCH_ACTION_ID,
            )
        except LookupError as exc:
            raise self._error(
                "Stock research room does not exist.",
                code="STOCK_RESEARCH_ROOM_NOT_FOUND",
                status=404,
            ) from exc
        except PluginLifecycleError as exc:
            raise self._error(
                "Stock research action is unavailable in this room.",
                code="STOCK_RESEARCH_ACTION_UNAVAILABLE",
            ) from exc

        try:
            room_scope = validate_stock_room_scope(room)
        except StockResearchContractError as exc:
            raise self._error(
                "The room stock pool seal is invalid.",
                code="STOCK_RESEARCH_SCOPE_INVALID",
            ) from exc
        if (
            room.get("stock_room_scope_integrity_ok") is not True
            or str(room.get("stock_room_scope_sha256") or "")
            != canonical_sha256(room_scope)
            or room_scope != contract.get("stock_room_scope")
            or room_scope.get("symbols")
            != [str(row.get("symbol") or "") for row in contract["symbols"]]
        ):
            raise self._error(
                "The contract does not exactly match the room stock pool.",
                code="STOCK_RESEARCH_SCOPE_MISMATCH",
                status=400,
            )

        port_resolution = self._single_registry_binding(room)
        try:
            adapter = self.domain_adapters.require_port_resolution(
                port_resolution
            )
            project = getattr(adapter, "project_market_readonly_context", None)
            if not callable(project):
                raise DomainAdapterError("stock projection port is missing")
            projected_contract = project(contract=copy.deepcopy(contract))
        except (DomainAdapterError, TypeError, ValueError) as exc:
            raise self._error(
                "The exact stock research adapter implementation is unavailable.",
                code="STOCK_RESEARCH_IMPLEMENTATION_UNAVAILABLE",
            ) from exc
        if projected_contract != contract:
            raise self._error(
                "The stock research adapter changed the sealed contract.",
                code="STOCK_RESEARCH_IMPLEMENTATION_UNAVAILABLE",
            )

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
        """Inspect inside an already-open caller-owned SQLite transaction."""

        if not isinstance(connection, sqlite3.Connection):
            raise self._error(
                "Stock research requires a Store SQLite connection.",
                code="STOCK_RESEARCH_CONNECTION_INVALID",
                status=400,
            )
        if not connection.in_transaction:
            raise self._error(
                "Stock research requires an active caller transaction snapshot.",
                code="STOCK_RESEARCH_SNAPSHOT_REQUIRED",
            )
        clean_room_id, contract = self._validated_request(room_id, payload)
        changes_before = int(connection.total_changes)
        try:
            result = self._inspect_snapshot(
                connection,
                clean_room_id,
                contract,
            )
        except StockResearchError:
            raise
        except sqlite3.Error as exc:
            raise self._error(
                "Stock research SQLite snapshot could not be verified.",
                code="STOCK_RESEARCH_DATABASE_SNAPSHOT_INVALID",
            ) from exc
        if int(connection.total_changes) != changes_before:
            raise self._error(
                "Stock research detected a database write.",
                code="STOCK_RESEARCH_WRITE_GUARD_FAILED",
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
            except StockResearchError:
                raise
            except sqlite3.Error as exc:
                raise self._error(
                    "Stock research read-only SQLite snapshot could not be verified.",
                    code="STOCK_RESEARCH_DATABASE_SNAPSHOT_INVALID",
                ) from exc
            if int(connection.total_changes) != changes_before:
                raise self._error(
                    "Stock research detected a database write.",
                    code="STOCK_RESEARCH_WRITE_GUARD_FAILED",
                    status=500,
                )
            return result


__all__ = [
    "STOCK_RESEARCH_ACTION_ID",
    "STOCK_RESEARCH_ADAPTER_ID",
    "STOCK_RESEARCH_CONTRIBUTION_ID",
    "STOCK_RESEARCH_PORT_ID",
    "STOCK_RESEARCH_VIEW_MODEL_VERSION",
    "STOCK_SYMBOL_PREFLIGHT_VIEW_VERSION",
    "StockResearchError",
    "StockResearchService",
]
