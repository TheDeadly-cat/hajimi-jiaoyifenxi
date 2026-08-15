from __future__ import annotations

import copy
import json
import re
import uuid
from contextlib import closing
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, TYPE_CHECKING

from .decision_lineage import canonical_sha256
from .project_round_focus import (
    PROJECT_ROUND_FOCUS_PACK_ID,
    PROJECT_ROUND_FOCUS_PORT_ID,
    ProjectRoundFocusError,
)

if TYPE_CHECKING:  # pragma: no cover
    import sqlite3

    from .store import StudioStore


ROUND_CONTEXT_PREPARED_SET_VERSION = "round_context_prepared_set_v1"
ROUND_CONTEXT_PREPARED_ENTRY_VERSION = "round_context_prepared_v1"
ROUND_CONTEXT_AUTHORIZATION_SET_VERSION = "round_context_authorization_set_v1"
ROUND_CONTEXT_AUTHORIZATION_ENTRY_VERSION = "round_context_authorization_entry_v1"
ROUND_CONTEXT_PROMPT_SECTION_VERSION = "round_context_prompt_section_v1"
ROUND_CONTEXT_RECORD_SET_VERSION = "round_context_record_set_v1"
ROUND_CONTEXT_RECORD_VERSION = "round_context_record_v1"
ROUND_CONTEXT_PROVIDER_REGISTRY_VERSION = "round_context_provider_registry_v1"
ROUND_CONTEXT_ANCHOR_VERSION = "round_domain_context_anchor_v1"
MAX_ROUND_CONTEXTS = 64

FOOTBALL_ROUND_CONTEXT_PREPARED_VERSION = "football_round_context_prepared_v1"
FOOTBALL_ROUND_CONTEXT_REQUEST_VERSION = "football_round_context_request_v1"
FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_VERSION = (
    "football_round_context_authorization_v1"
)
FOOTBALL_ROUND_CONTEXT_INPUT_SEAL_VERSION = "football_round_context_input_seal_v1"

STOCK_ROUND_CONTEXT_PREPARED_VERSION = "stock_round_context_prepared_v1"
STOCK_ROUND_CONTEXT_REQUEST_VERSION = "stock_round_context_request_v1"
STOCK_ROUND_CONTEXT_AUTHORIZATION_VERSION = "stock_round_context_authorization_v1"
STOCK_ROUND_CONTEXT_INPUT_SEAL_VERSION = "stock_round_context_input_seal_v1"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_KEY_SEPARATOR = "\u001f"


class RoundContextError(ValueError):
    """A typed, fail-closed formal-round context error."""

    def __init__(self, message: str, *, code: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _provider_error(exc: Exception, *, fallback_code: str) -> RoundContextError:
    if isinstance(exc, RoundContextError):
        return RoundContextError(
            str(exc),
            code=exc.code,
            status=exc.status,
        )
    typed_errors: tuple[type[BaseException], ...] = (ProjectRoundFocusError,)
    try:
        *_prefix, football_error_class, _service, _validator = _football_imports()
        typed_errors = (*typed_errors, football_error_class)
    except ImportError:
        pass
    try:
        (
            *_stock_prefix,
            stock_contract_error_class,
            stock_service_error_class,
            _stock_service,
            _stock_validator,
            _stock_scope_validator,
        ) = _stock_imports()
        typed_errors = (
            *typed_errors,
            stock_contract_error_class,
            stock_service_error_class,
        )
    except ImportError:
        pass
    if isinstance(exc, typed_errors):
        return RoundContextError(
            str(exc),
            code=str(getattr(exc, "code", fallback_code) or fallback_code),
            status=int(getattr(exc, "status", 409) or 409),
        )
    return RoundContextError(
        "Round-context provider failed closed.",
        code=fallback_code,
        status=409,
    )


class RoundContextProvider(Protocol):
    owner_pack_id: str
    port_id: str

    def prepare_authorized(
        self,
        store: "StudioStore",
        room_id: str,
        request: Any,
    ) -> Any: ...

    def prompt_section(self, value: Mapping[str, Any]) -> dict[str, Any]: ...

    def prepare_row(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        prepared: Any,
    ) -> dict[str, Any]: ...

    def verify_row(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        context_row: Mapping[str, Any],
    ) -> dict[str, Any]: ...


def _provider_key(owner_pack_id: Any, port_id: Any) -> tuple[str, str]:
    owner = str(owner_pack_id or "").strip()
    port = str(port_id or "").strip()
    if (
        not owner
        or not port
        or len(owner) > 160
        or len(port) > 200
        or _PROVIDER_KEY_SEPARATOR in owner
        or _PROVIDER_KEY_SEPARATOR in port
    ):
        raise RoundContextError(
            "Round-context provider identity is invalid.",
            code="ROUND_CONTEXT_PROVIDER_ID_INVALID",
            status=400,
        )
    return owner, port


def round_context_binding_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "context_version",
        "room_id",
        "round_id",
        "owner_pack_id",
        "adapter_id",
        "adapter_version",
        "adapter_contract_sha256",
        "port_id",
        "port_version",
        "port_contract_sha256",
        "source_status",
        "source_artifact_id",
        "source_artifact_version",
        "artifact_snapshot_sha256",
        "evidence_review_event_sha256",
        "evidence_relations_sha256",
        "plugin_registry_snapshot_sha256",
        "plugin_lifecycle_resolution_sha256",
        "authorization_sha256",
        "input_seal_sha256",
        "preview_sha256",
        "output_sha256",
        "provider_calls_performed",
        "market_reads_performed",
        "adapter_business_writes_performed",
        "created_at",
    )
    return {field: row.get(field) for field in fields}


def round_context_anchor_sha256(binding_sha256s: Iterable[Any]) -> str:
    normalized = sorted(str(value or "").strip().lower() for value in binding_sha256s)
    if any(not _SHA256_PATTERN.fullmatch(value) for value in normalized):
        raise RoundContextError(
            "Round-context binding hash is invalid.",
            code="ROUND_CONTEXT_BINDING_INVALID",
        )
    if not normalized:
        return ""
    return canonical_sha256({
        "version": ROUND_CONTEXT_ANCHOR_VERSION,
        "binding_sha256s": normalized,
    })


def round_context_prepared_entry(
    owner_pack_id: str,
    port_id: str,
    prepared: Any,
) -> dict[str, Any]:
    owner, port = _provider_key(owner_pack_id, port_id)
    return {
        "version": ROUND_CONTEXT_PREPARED_ENTRY_VERSION,
        "owner_pack_id": owner,
        "port_id": port,
        "prepared": copy.deepcopy(prepared),
    }


def round_context_authorization_entry(
    owner_pack_id: str,
    port_id: str,
    request: Any,
) -> dict[str, Any]:
    owner, port = _provider_key(owner_pack_id, port_id)
    return {
        "version": ROUND_CONTEXT_AUTHORIZATION_ENTRY_VERSION,
        "owner_pack_id": owner,
        "port_id": port,
        "request": copy.deepcopy(request),
    }


def build_round_context_authorization_set(
    entries: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    clean_entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in entries:
        if not isinstance(raw, Mapping) or set(raw) != {
            "version",
            "owner_pack_id",
            "port_id",
            "request",
        } or raw.get("version") != ROUND_CONTEXT_AUTHORIZATION_ENTRY_VERSION:
            raise RoundContextError(
                "Round-context authorization entry has an invalid closed shape.",
                code="ROUND_CONTEXT_AUTHORIZATION_INVALID",
                status=400,
            )
        key = _provider_key(raw.get("owner_pack_id"), raw.get("port_id"))
        if key in seen:
            raise RoundContextError(
                "Round-context authorization set contains a duplicate provider.",
                code="ROUND_CONTEXT_AUTHORIZATION_DUPLICATE",
                status=400,
            )
        seen.add(key)
        clean_entries.append(round_context_authorization_entry(
            *key,
            raw.get("request"),
        ))
    if len(clean_entries) > MAX_ROUND_CONTEXTS:
        raise RoundContextError(
            "Round-context authorization set exceeds the supported capacity.",
            code="ROUND_CONTEXT_CAPACITY_EXCEEDED",
            status=400,
        )
    clean_entries.sort(key=lambda row: (row["owner_pack_id"], row["port_id"]))
    return {
        "version": ROUND_CONTEXT_AUTHORIZATION_SET_VERSION,
        "contexts": clean_entries,
    }


def normalize_round_context_authorizations(
    value: Any,
) -> dict[tuple[str, str], Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"version", "contexts"}
        or value.get("version") != ROUND_CONTEXT_AUTHORIZATION_SET_VERSION
        or not isinstance(value.get("contexts"), list)
    ):
        raise RoundContextError(
            "Round-context authorization set has an invalid closed shape.",
            code="ROUND_CONTEXT_AUTHORIZATION_INVALID",
            status=400,
        )
    normalized = build_round_context_authorization_set(value["contexts"])
    return {
        (str(entry["owner_pack_id"]), str(entry["port_id"])): copy.deepcopy(
            entry["request"]
        )
        for entry in normalized["contexts"]
    }


def coerce_round_context_authorization_set(
    value: Any,
    *,
    legacy_project_round_focus_authorization: Any = None,
) -> dict[str, Any]:
    """Normalize the generic set or map the one legacy project boundary."""

    if value is not None and legacy_project_round_focus_authorization is not None:
        raise RoundContextError(
            "Generic and legacy round-context authorizations cannot be combined.",
            code="ROUND_CONTEXT_AUTHORIZATION_AMBIGUOUS",
            status=400,
        )
    if value is None:
        entries = (
            []
            if legacy_project_round_focus_authorization is None
            else [round_context_authorization_entry(
                PROJECT_ROUND_FOCUS_PACK_ID,
                PROJECT_ROUND_FOCUS_PORT_ID,
                legacy_project_round_focus_authorization,
            )]
        )
        return build_round_context_authorization_set(entries)
    normalized = normalize_round_context_authorizations(value)
    return build_round_context_authorization_set(
        round_context_authorization_entry(*key, request)
        for key, request in normalized.items()
    )


def build_round_context_prepared(
    entries: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    clean_entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in entries:
        if not isinstance(raw, Mapping) or set(raw) != {
            "version",
            "owner_pack_id",
            "port_id",
            "prepared",
        } or raw.get("version") != ROUND_CONTEXT_PREPARED_ENTRY_VERSION:
            raise RoundContextError(
                "Round-context prepared entry has an invalid closed shape.",
                code="ROUND_CONTEXT_PREPARED_INVALID",
                status=400,
            )
        key = _provider_key(raw.get("owner_pack_id"), raw.get("port_id"))
        if key in seen:
            raise RoundContextError(
                "Round-context prepared entries contain a duplicate provider.",
                code="ROUND_CONTEXT_PREPARED_DUPLICATE",
                status=400,
            )
        seen.add(key)
        clean_entries.append(round_context_prepared_entry(*key, raw.get("prepared")))
    if len(clean_entries) > MAX_ROUND_CONTEXTS:
        raise RoundContextError(
            "Round-context prepared set exceeds the supported capacity.",
            code="ROUND_CONTEXT_CAPACITY_EXCEEDED",
            status=400,
        )
    clean_entries.sort(key=lambda row: (row["owner_pack_id"], row["port_id"]))
    return {
        "version": ROUND_CONTEXT_PREPARED_SET_VERSION,
        "contexts": clean_entries,
    }


def normalize_round_context_prepared(
    value: Any,
    *,
    legacy_project_round_focus_prepared: Any = None,
) -> dict[tuple[str, str], Any]:
    entries: list[Mapping[str, Any]] = []
    if value is not None:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"version", "contexts"}
            or value.get("version") != ROUND_CONTEXT_PREPARED_SET_VERSION
            or not isinstance(value.get("contexts"), list)
        ):
            raise RoundContextError(
                "Round-context prepared set has an invalid closed shape.",
                code="ROUND_CONTEXT_PREPARED_INVALID",
                status=400,
            )
        entries.extend(value["contexts"])
    if legacy_project_round_focus_prepared is not None:
        entries.append(round_context_prepared_entry(
            PROJECT_ROUND_FOCUS_PACK_ID,
            PROJECT_ROUND_FOCUS_PORT_ID,
            legacy_project_round_focus_prepared,
        ))
    normalized = build_round_context_prepared(entries)
    return {
        (str(entry["owner_pack_id"]), str(entry["port_id"])): copy.deepcopy(
            entry["prepared"]
        )
        for entry in normalized["contexts"]
    }


def _snapshot_provider_resolution(
    snapshot: Mapping[str, Any],
    key: tuple[str, str],
) -> dict[str, Any]:
    matches = [
        row
        for row in snapshot.get("port_resolutions", [])
        if isinstance(row, dict)
        and str(row.get("owner_pack_id") or "") == key[0]
        and str(row.get("port_id") or "") == key[1]
    ]
    if len(matches) != 1:
        raise RoundContextError(
            "The frozen plugin registry has no unique round-context port binding.",
            code="ROUND_CONTEXT_PROVIDER_BINDING_INVALID",
        )
    resolution = matches[0]
    resolved = resolution.get("resolved")
    if (
        resolution.get("requirement") != "required"
        or resolution.get("cardinality") != "one"
        or not isinstance(resolved, list)
        or len(resolved) != 1
        or not isinstance(resolved[0], dict)
    ):
        raise RoundContextError(
            "The frozen round-context port must resolve one required implementation.",
            code="ROUND_CONTEXT_PROVIDER_BINDING_INVALID",
        )
    implementation = copy.deepcopy(resolved[0])
    if (
        str(implementation.get("port_id") or "") != key[1]
        or not _SHA256_PATTERN.fullmatch(
            str(implementation.get("adapter_contract_sha256") or "")
        )
        or not _SHA256_PATTERN.fullmatch(
            str(implementation.get("port_contract_sha256") or "")
        )
    ):
        raise RoundContextError(
            "The frozen round-context implementation binding is invalid.",
            code="ROUND_CONTEXT_PROVIDER_BINDING_INVALID",
        )
    return implementation


class RoundContextProviderRegistry:
    def __init__(
        self,
        providers: Iterable[RoundContextProvider] = (),
        *,
        version: str = ROUND_CONTEXT_PROVIDER_REGISTRY_VERSION,
    ) -> None:
        if version != ROUND_CONTEXT_PROVIDER_REGISTRY_VERSION:
            raise RoundContextError(
                "Round-context provider registry version is unsupported.",
                code="ROUND_CONTEXT_PROVIDER_REGISTRY_VERSION_UNSUPPORTED",
            )
        self.version = version
        self._providers: dict[tuple[str, str], RoundContextProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: RoundContextProvider) -> None:
        key = _provider_key(provider.owner_pack_id, provider.port_id)
        if key in self._providers:
            raise RoundContextError(
                "Round-context provider is already registered.",
                code="ROUND_CONTEXT_PROVIDER_DUPLICATE",
            )
        self._providers[key] = provider

    def provider_keys(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._providers))

    def selected_provider_keys(
        self,
        snapshot: Mapping[str, Any],
    ) -> tuple[tuple[str, str], ...]:
        if not isinstance(snapshot, Mapping):
            raise RoundContextError(
                "Frozen plugin registry is unavailable for round contexts.",
                code="ROUND_CONTEXT_REGISTRY_INVALID",
            )
        if not snapshot:
            return ()
        selected = snapshot.get("selected_capability_pack_ids")
        packs = snapshot.get("capability_packs")
        if not isinstance(selected, list) or not isinstance(packs, list):
            raise RoundContextError(
                "Frozen plugin registry selection is invalid.",
                code="ROUND_CONTEXT_REGISTRY_INVALID",
            )
        selected_ids = {
            str(value or "")
            for value in selected
            if isinstance(value, str) and str(value or "")
        }
        frozen_pack_ids = {
            str(row.get("id") or "")
            for row in packs
            if isinstance(row, dict) and str(row.get("id") or "")
        }
        if not selected_ids.issubset(frozen_pack_ids):
            raise RoundContextError(
                "Frozen plugin registry pack selection is inconsistent.",
                code="ROUND_CONTEXT_REGISTRY_INVALID",
            )
        frozen_resolution_keys = {
            (
                str(row.get("owner_pack_id") or ""),
                str(row.get("port_id") or ""),
            )
            for row in snapshot.get("port_resolutions", [])
            if isinstance(row, dict)
        }
        expected = tuple(
            key
            for key in sorted(self._providers)
            if key[0] in selected_ids and key in frozen_resolution_keys
        )
        if len(expected) > MAX_ROUND_CONTEXTS:
            raise RoundContextError(
                "Selected round-context providers exceed the supported capacity.",
                code="ROUND_CONTEXT_CAPACITY_EXCEEDED",
            )
        for key in expected:
            _snapshot_provider_resolution(snapshot, key)
        return expected

    def prepare_authorized_set(
        self,
        store: "StudioStore",
        room_id: str,
        authorization_set: Any,
    ) -> dict[str, Any]:
        snapshot_record = store.room_snapshot(room_id)
        room = (
            snapshot_record.get("room")
            if isinstance(snapshot_record, dict)
            else None
        )
        if not isinstance(room, dict):
            raise RoundContextError(
                "Round-context room does not exist.",
                code="ROUND_CONTEXT_ROOM_NOT_FOUND",
                status=404,
            )
        snapshot = room.get("plugin_registry_snapshot")
        if (
            room.get("plugin_registry_integrity_ok") is not True
            or not isinstance(snapshot, dict)
        ):
            raise RoundContextError(
                "Room plugin registry cannot authorize round contexts.",
                code="ROUND_CONTEXT_REGISTRY_INVALID",
            )
        expected = set(self.selected_provider_keys(snapshot))
        requests = normalize_round_context_authorizations(authorization_set)
        supplied = set(requests)
        if supplied != expected:
            missing = sorted(expected - supplied)
            extra = sorted(supplied - expected)
            raise RoundContextError(
                "Round-context authorization set does not exactly match selected "
                f"providers (missing={missing!r}, extra={extra!r}).",
                code=(
                    "ROUND_CONTEXT_AUTHORIZATION_REQUIRED"
                    if missing
                    else "ROUND_CONTEXT_AUTHORIZATION_NOT_APPLICABLE"
                ),
                status=400,
            )
        entries: list[dict[str, Any]] = []
        for key in sorted(expected):
            try:
                provider_prepared = self._providers[key].prepare_authorized(
                    store,
                    room_id,
                    requests[key],
                )
            except Exception as exc:
                raise _provider_error(
                    exc,
                    fallback_code="ROUND_CONTEXT_PROVIDER_PREPARE_FAILED",
                ) from exc
            entries.append(round_context_prepared_entry(
                *key,
                provider_prepared,
            ))
        return build_round_context_prepared(entries)

    def prompt_sections(self, prepared_set: Any) -> list[dict[str, Any]]:
        if (
            isinstance(prepared_set, Mapping)
            and prepared_set.get("version") == ROUND_CONTEXT_RECORD_SET_VERSION
        ):
            if (
                set(prepared_set) != {
                    "version",
                    "integrity_ok",
                    "room_id",
                    "round_id",
                    "round_domain_context_count",
                    "round_domain_contexts_sha256",
                    "contexts",
                }
                or prepared_set.get("integrity_ok") is not True
                or not isinstance(prepared_set.get("contexts"), list)
            ):
                raise RoundContextError(
                    "Frozen round-context record set is not verified.",
                    code="ROUND_CONTEXT_INTEGRITY_FAILED",
                )
            prepared: dict[tuple[str, str], Any] = {}
            for record in prepared_set["contexts"]:
                if (
                    not isinstance(record, Mapping)
                    or set(record) != {
                        "version",
                        "context_version",
                        "owner_pack_id",
                        "port_id",
                        "binding_sha256",
                        "preview_sha256",
                        "preview",
                    }
                    or record.get("version") != ROUND_CONTEXT_RECORD_VERSION
                    or not isinstance(record.get("preview"), dict)
                ):
                    raise RoundContextError(
                        "Frozen round-context record has an invalid closed shape.",
                        code="ROUND_CONTEXT_INTEGRITY_FAILED",
                    )
                key = _provider_key(
                    record.get("owner_pack_id"), record.get("port_id")
                )
                if key in prepared:
                    raise RoundContextError(
                        "Frozen round-context records contain a duplicate provider.",
                        code="ROUND_CONTEXT_INTEGRITY_FAILED",
                    )
                prepared[key] = {"preview": copy.deepcopy(record["preview"])}
            if len(prepared) != int(
                prepared_set.get("round_domain_context_count") or 0
            ):
                raise RoundContextError(
                    "Frozen round-context record count is inconsistent.",
                    code="ROUND_CONTEXT_INTEGRITY_FAILED",
                )
            if round_context_anchor_sha256(
                record["binding_sha256"]
                for record in prepared_set["contexts"]
            ) != str(prepared_set.get("round_domain_contexts_sha256") or ""):
                raise RoundContextError(
                    "Frozen round-context record anchor is inconsistent.",
                    code="ROUND_CONTEXT_INTEGRITY_FAILED",
                )
        else:
            prepared = normalize_round_context_prepared(prepared_set)
        sections: list[dict[str, Any]] = []
        for key in sorted(prepared):
            provider = self._providers.get(key)
            if provider is None:
                raise RoundContextError(
                    "Prepared round context has no registered prompt projector.",
                    code="ROUND_CONTEXT_PROVIDER_UNREGISTERED",
                )
            try:
                section = provider.prompt_section(prepared[key])
            except Exception as exc:
                raise _provider_error(
                    exc,
                    fallback_code="ROUND_CONTEXT_PROVIDER_PROMPT_FAILED",
                ) from exc
            expected_keys = {
                "version",
                "owner_pack_id",
                "port_id",
                "title",
                "payload",
                "payload_sha256",
            }
            if (
                not isinstance(section, dict)
                or set(section) != expected_keys
                or section.get("version") != ROUND_CONTEXT_PROMPT_SECTION_VERSION
                or _provider_key(
                    section.get("owner_pack_id"), section.get("port_id")
                ) != key
                or canonical_sha256(section.get("payload"))
                != str(section.get("payload_sha256") or "")
            ):
                raise RoundContextError(
                    "Round-context provider returned an invalid prompt section.",
                    code="ROUND_CONTEXT_PROVIDER_OUTPUT_INVALID",
                )
            sections.append(copy.deepcopy(section))
        return sections

    def prepare_rows(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        prepared: Any,
        *,
        legacy_project_round_focus_prepared: Any = None,
    ) -> list[dict[str, Any]]:
        expected = self.selected_provider_keys(snapshot)
        supplied = normalize_round_context_prepared(
            prepared,
            legacy_project_round_focus_prepared=(
                legacy_project_round_focus_prepared
            ),
        )
        supplied_keys = set(supplied)
        expected_keys = set(expected)
        if supplied_keys != expected_keys:
            missing = sorted(expected_keys - supplied_keys)
            extra = sorted(supplied_keys - expected_keys)
            code = (
                "ROUND_CONTEXT_AUTHORIZATION_REQUIRED"
                if missing
                else "ROUND_CONTEXT_AUTHORIZATION_NOT_APPLICABLE"
            )
            raise RoundContextError(
                "Round-context authorizations do not exactly match selected providers "
                f"(missing={missing!r}, extra={extra!r}).",
                code=code,
                status=400,
            )
        rows: list[dict[str, Any]] = []
        for key in expected:
            try:
                row = self._providers[key].prepare_row(
                    store,
                    connection,
                    round_row,
                    supplied[key],
                )
            except Exception as exc:
                raise _provider_error(
                    exc,
                    fallback_code="ROUND_CONTEXT_PROVIDER_PREPARE_FAILED",
                ) from exc
            rows.append(row)
        actual_keys = [
            _provider_key(row.get("owner_pack_id"), row.get("port_id"))
            for row in rows
        ]
        if actual_keys != list(expected):
            raise RoundContextError(
                "Round-context provider returned a mismatched identity.",
                code="ROUND_CONTEXT_PROVIDER_OUTPUT_INVALID",
            )
        return rows

    def verify_row(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        context_row: Mapping[str, Any],
    ) -> dict[str, Any]:
        key = _provider_key(
            context_row.get("owner_pack_id"),
            context_row.get("port_id"),
        )
        provider = self._providers.get(key)
        if provider is None:
            raise RoundContextError(
                "Frozen round context has no registered verifier.",
                code="ROUND_CONTEXT_PROVIDER_UNREGISTERED",
            )
        try:
            return provider.verify_row(
                store,
                connection,
                dict(round_row),
                dict(context_row),
            )
        except Exception as exc:
            raise _provider_error(
                exc,
                fallback_code="ROUND_CONTEXT_PROVIDER_VERIFY_FAILED",
            ) from exc

    def verify_rows(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        context_rows: Iterable[Mapping[str, Any]],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        frozen_round = dict(round_row)
        try:
            snapshot = json.loads(
                str(frozen_round.get("plugin_registry_snapshot_json") or "{}")
            )
        except json.JSONDecodeError as exc:
            raise RoundContextError(
                "Frozen round plugin registry JSON is invalid.",
                code="ROUND_CONTEXT_REGISTRY_INVALID",
            ) from exc
        expected = set(self.selected_provider_keys(snapshot))
        rows = [dict(row) for row in context_rows]
        if len(rows) > MAX_ROUND_CONTEXTS:
            raise RoundContextError(
                "Frozen round contexts exceed the supported capacity.",
                code="ROUND_CONTEXT_CAPACITY_EXCEEDED",
            )
        actual_keys = [
            _provider_key(row.get("owner_pack_id"), row.get("port_id"))
            for row in rows
        ]
        if len(set(actual_keys)) != len(actual_keys) or set(actual_keys) != expected:
            raise RoundContextError(
                "Frozen round-context identities do not match selected providers.",
                code="ROUND_CONTEXT_PROVIDER_SET_MISMATCH",
            )
        verified: dict[tuple[str, str], dict[str, Any]] = {}
        bindings: list[str] = []
        for key, row in sorted(zip(actual_keys, rows), key=lambda item: item[0]):
            record = self.verify_row(
                store,
                connection,
                frozen_round,
                row,
            )
            verified[key] = record
            bindings.append(str(record.get("binding_sha256") or ""))
        expected_anchor = round_context_anchor_sha256(bindings)
        if (
            type(frozen_round.get("round_domain_context_count")) is not int
            or int(frozen_round.get("round_domain_context_count") or 0)
            != len(rows)
            or str(frozen_round.get("round_domain_contexts_sha256") or "")
            != expected_anchor
        ):
            raise RoundContextError(
                "Frozen round-context aggregate anchor does not match.",
                code="ROUND_CONTEXT_ANCHOR_MISMATCH",
            )
        return verified


@dataclass(frozen=True)
class ProjectRoundFocusContextProvider:
    owner_pack_id: str = PROJECT_ROUND_FOCUS_PACK_ID
    port_id: str = PROJECT_ROUND_FOCUS_PORT_ID

    def prepare_authorized(
        self,
        store: "StudioStore",
        room_id: str,
        request: Any,
    ) -> Any:
        from .project_round_focus import ProjectRoundFocusService

        return ProjectRoundFocusService(store).prepare_authorized(room_id, request)

    def prompt_section(self, value: Mapping[str, Any]) -> dict[str, Any]:
        from .project_round_focus import ProjectRoundFocusService

        preview = value.get("preview")
        if not isinstance(preview, dict):
            raise RoundContextError(
                "Project round-focus prepared payload has no preview.",
                code="ROUND_CONTEXT_PROVIDER_OUTPUT_INVALID",
            )
        payload = {
            "version": "project_round_focus_prompt_payload_v1",
            "preview": copy.deepcopy(preview),
            "legacy_workspace": (
                ProjectRoundFocusService.legacy_workspace_from_preview(preview)
            ),
        }
        return {
            "version": ROUND_CONTEXT_PROMPT_SECTION_VERSION,
            "owner_pack_id": self.owner_pack_id,
            "port_id": self.port_id,
            "title": "Frozen project next-round focus",
            "payload": payload,
            "payload_sha256": canonical_sha256(payload),
        }

    def prepare_row(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        prepared: Any,
    ) -> dict[str, Any]:
        return store._prepare_project_round_focus_context_connection(
            connection,
            str(round_row.get("room_id") or ""),
            str(round_row.get("id") or ""),
            prepared,
            plugin_registry_snapshot_sha256=str(
                round_row.get("plugin_registry_snapshot_sha256") or ""
            ),
            plugin_lifecycle_resolution_sha256=str(
                round_row.get("plugin_lifecycle_resolution_sha256") or ""
            ),
            created_at=int(round_row.get("created_at") or 0),
        )

    def verify_row(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        context_row: Mapping[str, Any],
    ) -> dict[str, Any]:
        return store._verify_project_round_focus_context_connection(
            connection,
            round_row,
            context_row,
        )


def _football_imports() -> tuple[Any, ...]:
    from .football_research import (
        FOOTBALL_RESEARCH_CAPABILITY_PACK_ID,
        validate_football_research_contract,
    )
    from .football_research_service import (
        FOOTBALL_RESEARCH_ACTION_ID,
        FOOTBALL_RESEARCH_PORT_ID,
        FootballResearchError,
        FootballResearchService,
    )

    return (
        FOOTBALL_RESEARCH_CAPABILITY_PACK_ID,
        FOOTBALL_RESEARCH_PORT_ID,
        FOOTBALL_RESEARCH_ACTION_ID,
        FootballResearchError,
        FootballResearchService,
        validate_football_research_contract,
    )


def _football_material_bindings(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    *_, service_class, _validator = _football_imports()
    by_identity: dict[tuple[str, int], dict[str, Any]] = {}
    for binding in service_class._iter_material_bindings(contract):
        clean = {
            "material_id": str(binding.get("material_id") or ""),
            "material_version": int(binding.get("material_version") or 0),
            "content_sha256": str(binding.get("content_sha256") or ""),
            "snapshot_sha256": str(binding.get("snapshot_sha256") or ""),
        }
        identity = (clean["material_id"], clean["material_version"])
        existing = by_identity.get(identity)
        if existing is not None and existing != clean:
            raise RoundContextError(
                "Football material identity has conflicting bindings.",
                code="FOOTBALL_ROUND_CONTEXT_MATERIAL_CONFLICT",
            )
        by_identity[identity] = clean
    if not by_identity:
        raise RoundContextError(
            "Football round context has no exact material bindings.",
            code="FOOTBALL_ROUND_CONTEXT_MATERIAL_MISSING",
        )
    return [by_identity[key] for key in sorted(by_identity)]


def _normalize_football_authorization(value: Any) -> dict[str, Any]:
    (
        football_pack_id,
        football_port_id,
        _action_id,
        _error_class,
        _service_class,
        _validator,
    ) = _football_imports()
    keys = {
        "version",
        "owner_pack_id",
        "port_id",
        "contract_sha256",
        "data_cutoff_utc",
        "match_id",
        "user_confirmed",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value.get("version") != FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_VERSION
        or value.get("owner_pack_id") != football_pack_id
        or value.get("port_id") != football_port_id
        or not _SHA256_PATTERN.fullmatch(str(value.get("contract_sha256") or ""))
        or not isinstance(value.get("data_cutoff_utc"), str)
        or not str(value.get("data_cutoff_utc") or "")
        or len(str(value.get("data_cutoff_utc") or "")) > 40
        or not isinstance(value.get("match_id"), str)
        or not str(value.get("match_id") or "")
        or len(str(value.get("match_id") or "")) > 200
        or value.get("user_confirmed") is not True
    ):
        raise RoundContextError(
            "Football round-context authorization is invalid.",
            code="FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_INVALID",
            status=400,
        )
    return copy.deepcopy(value)


def _football_match_id(contract: Mapping[str, Any]) -> str:
    identity = contract.get("match_identity")
    match_field = identity.get("match_id") if isinstance(identity, dict) else None
    return str(match_field.get("value") or "") if isinstance(match_field, dict) else ""


def _football_input_seal(
    *,
    room_id: str,
    preview: Mapping[str, Any],
    plugin_registry_snapshot_sha256: str,
    plugin_lifecycle_head_set_sha256: str,
) -> dict[str, Any]:
    contract = preview.get("contract")
    if not isinstance(contract, dict):
        raise RoundContextError(
            "Football round-context preview has no closed contract.",
            code="FOOTBALL_ROUND_CONTEXT_PREPARED_INVALID",
        )
    return {
        "version": FOOTBALL_ROUND_CONTEXT_INPUT_SEAL_VERSION,
        "room_id": str(room_id),
        "contract_sha256": str(preview.get("contract_sha256") or ""),
        "material_bindings": _football_material_bindings(contract),
        "plugin_registry_snapshot_sha256": str(
            plugin_registry_snapshot_sha256 or ""
        ),
        "plugin_lifecycle_head_set_sha256": str(
            plugin_lifecycle_head_set_sha256 or ""
        ),
    }


def _football_prepared_payload(
    *,
    room_id: str,
    preview: Mapping[str, Any],
    authorization: Any,
    plugin_registry_snapshot_sha256: str,
    plugin_lifecycle_head_set_sha256: str,
) -> dict[str, Any]:
    clean_authorization = _normalize_football_authorization(authorization)
    contract = preview.get("contract")
    if not isinstance(contract, dict):
        raise RoundContextError(
            "Football round-context preview is invalid.",
            code="FOOTBALL_ROUND_CONTEXT_PREPARED_INVALID",
        )
    if any((
        str(preview.get("contract_sha256") or "")
        != clean_authorization["contract_sha256"],
        str(preview.get("data_cutoff_utc") or "")
        != clean_authorization["data_cutoff_utc"],
        _football_match_id(contract) != clean_authorization["match_id"],
    )):
        raise RoundContextError(
            "Football research preview changed after explicit authorization.",
            code="FOOTBALL_ROUND_CONTEXT_PREVIEW_DRIFT",
        )
    input_seal = _football_input_seal(
        room_id=room_id,
        preview=preview,
        plugin_registry_snapshot_sha256=plugin_registry_snapshot_sha256,
        plugin_lifecycle_head_set_sha256=plugin_lifecycle_head_set_sha256,
    )
    preview_copy = copy.deepcopy(dict(preview))
    preview_sha256 = canonical_sha256(preview_copy)
    return {
        "version": FOOTBALL_ROUND_CONTEXT_PREPARED_VERSION,
        "authorization": clean_authorization,
        "input_seal": input_seal,
        "input_seal_sha256": canonical_sha256(input_seal),
        "preview": preview_copy,
        "preview_sha256": preview_sha256,
        "output_sha256": preview_sha256,
    }


def prepare_football_round_context(
    store: "StudioStore",
    room_id: str,
    payload: Any,
    authorization: Any,
) -> dict[str, Any]:
    """Prepare one explicitly authorized football entry in one read snapshot."""

    (
        football_pack_id,
        football_port_id,
        football_action_id,
        football_error_class,
        service_class,
        _validator,
    ) = _football_imports()
    service = service_class(store)
    inspect_from_connection = getattr(service, "inspect_from_connection", None)
    if not callable(inspect_from_connection):
        raise RoundContextError(
            "Football service does not expose same-snapshot inspection.",
            code="FOOTBALL_ROUND_CONTEXT_IMPLEMENTATION_UNAVAILABLE",
            status=503,
        )
    try:
        with closing(store._connect()) as connection:
            connection.execute("BEGIN")
            preview = inspect_from_connection(connection, room_id, payload)
            room = store._require_room_plugin_action_connection(
                connection,
                room_id,
                football_action_id,
            )
            lifecycle = room.get("plugin_lifecycle_current") or {}
            prepared = _football_prepared_payload(
                room_id=room_id,
                preview=preview,
                authorization=authorization,
                plugin_registry_snapshot_sha256=str(
                    room.get("plugin_registry_snapshot_sha256") or ""
                ),
                plugin_lifecycle_head_set_sha256=str(
                    lifecycle.get("current_head_set_sha256") or ""
                ),
            )
    except Exception as exc:
        raise _provider_error(
            exc,
            fallback_code="ROUND_CONTEXT_PROVIDER_PREPARE_FAILED",
        ) from exc
    return round_context_prepared_entry(
        football_pack_id,
        football_port_id,
        prepared,
    )


@dataclass(frozen=True)
class FootballResearchContextProvider:
    owner_pack_id: str
    port_id: str

    @classmethod
    def create(cls) -> "FootballResearchContextProvider":
        football_pack_id, football_port_id, *_ = _football_imports()
        return cls(football_pack_id, football_port_id)

    def prepare_authorized(
        self,
        store: "StudioStore",
        room_id: str,
        request: Any,
    ) -> Any:
        if (
            not isinstance(request, dict)
            or set(request) != {"version", "payload", "authorization"}
            or request.get("version") != FOOTBALL_ROUND_CONTEXT_REQUEST_VERSION
        ):
            raise RoundContextError(
                "Football round-context request has an invalid closed shape.",
                code="FOOTBALL_ROUND_CONTEXT_REQUEST_INVALID",
                status=400,
            )
        entry = prepare_football_round_context(
            store,
            room_id,
            request.get("payload"),
            request.get("authorization"),
        )
        return copy.deepcopy(entry["prepared"])

    def prompt_section(self, value: Mapping[str, Any]) -> dict[str, Any]:
        preview = value.get("preview")
        if not isinstance(preview, dict):
            raise RoundContextError(
                "Football round-context prepared payload has no preview.",
                code="ROUND_CONTEXT_PROVIDER_OUTPUT_INVALID",
            )
        (
            _pack_id,
            _port_id,
            _action_id,
            _error_class,
            service_class,
            validate_contract,
        ) = _football_imports()
        try:
            contract = validate_contract(preview.get("contract"))
            expected_preview = service_class._closed_view_model(
                str(preview.get("room_id") or ""),
                contract,
            )
        except Exception as exc:
            raise RoundContextError(
                "Football prompt context is invalid.",
                code="ROUND_CONTEXT_PROVIDER_OUTPUT_INVALID",
            ) from exc
        if preview != expected_preview:
            raise RoundContextError(
                "Football prompt context seals do not match.",
                code="ROUND_CONTEXT_PROVIDER_OUTPUT_INVALID",
            )
        payload = {
            "version": "football_round_context_prompt_payload_v1",
            "view_model": copy.deepcopy(preview),
        }
        return {
            "version": ROUND_CONTEXT_PROMPT_SECTION_VERSION,
            "owner_pack_id": self.owner_pack_id,
            "port_id": self.port_id,
            "title": "Frozen football research context",
            "payload": payload,
            "payload_sha256": canonical_sha256(payload),
        }

    def prepare_row(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        prepared: Any,
    ) -> dict[str, Any]:
        (
            _pack_id,
            _port_id,
            _action_id,
            football_error_class,
            service_class,
            validate_contract,
        ) = _football_imports()
        if (
            not isinstance(prepared, dict)
            or set(prepared) != {
                "version",
                "authorization",
                "input_seal",
                "input_seal_sha256",
                "preview",
                "preview_sha256",
                "output_sha256",
            }
            or prepared.get("version") != FOOTBALL_ROUND_CONTEXT_PREPARED_VERSION
        ):
            raise RoundContextError(
                "Football round-context prepared payload has an invalid closed shape.",
                code="FOOTBALL_ROUND_CONTEXT_PREPARED_INVALID",
                status=400,
            )
        authorization = _normalize_football_authorization(
            prepared.get("authorization")
        )
        preview = prepared.get("preview")
        input_seal = prepared.get("input_seal")
        if not isinstance(preview, dict) or not isinstance(input_seal, dict):
            raise RoundContextError(
                "Football round-context seals are invalid.",
                code="FOOTBALL_ROUND_CONTEXT_PREPARED_INVALID",
            )
        try:
            contract = validate_contract(preview.get("contract"))
        except Exception as exc:
            raise RoundContextError(
                "Football round-context contract failed validation.",
                code="FOOTBALL_ROUND_CONTEXT_PREPARED_INVALID",
            ) from exc
        inspect_from_connection = getattr(
            service_class(store), "inspect_from_connection", None
        )
        if not callable(inspect_from_connection):
            raise RoundContextError(
                "Football service does not expose same-snapshot inspection.",
                code="FOOTBALL_ROUND_CONTEXT_IMPLEMENTATION_UNAVAILABLE",
                status=503,
            )
        try:
            expected_preview = inspect_from_connection(
                connection,
                str(round_row.get("room_id") or ""),
                contract,
            )
        except football_error_class:
            raise
        try:
            snapshot = json.loads(
                str(round_row.get("plugin_registry_snapshot_json") or "{}")
            )
            lifecycle_resolution = json.loads(
                str(round_row.get("plugin_lifecycle_resolution_json") or "{}")
            )
        except json.JSONDecodeError as exc:
            raise RoundContextError(
                "Frozen plugin bindings are not valid JSON.",
                code="FOOTBALL_ROUND_CONTEXT_BINDING_INVALID",
            ) from exc
        if not isinstance(snapshot, dict) or not isinstance(
            lifecycle_resolution, dict
        ):
            raise RoundContextError(
                "Frozen plugin bindings are invalid.",
                code="FOOTBALL_ROUND_CONTEXT_BINDING_INVALID",
            )
        expected_input_seal = _football_input_seal(
            room_id=str(round_row.get("room_id") or ""),
            preview=expected_preview,
            plugin_registry_snapshot_sha256=str(
                round_row.get("plugin_registry_snapshot_sha256") or ""
            ),
            plugin_lifecycle_head_set_sha256=str(
                lifecycle_resolution.get("lifecycle_head_set_sha256") or ""
            ),
        )
        preview_sha256 = canonical_sha256(expected_preview)
        if any((
            preview != expected_preview,
            input_seal != expected_input_seal,
            str(prepared.get("input_seal_sha256") or "")
            != canonical_sha256(expected_input_seal),
            str(prepared.get("preview_sha256") or "") != preview_sha256,
            str(prepared.get("output_sha256") or "") != preview_sha256,
            authorization["contract_sha256"]
            != str(expected_preview.get("contract_sha256") or ""),
            authorization["data_cutoff_utc"]
            != str(expected_preview.get("data_cutoff_utc") or ""),
            authorization["match_id"]
            != _football_match_id(expected_preview.get("contract") or {}),
        )):
            raise RoundContextError(
                "Football round-context source or seals drifted before creation.",
                code="FOOTBALL_ROUND_CONTEXT_SOURCE_DRIFT",
            )
        implementation = _snapshot_provider_resolution(
            snapshot,
            (self.owner_pack_id, self.port_id),
        )
        context_version = "round_domain_context_v1"
        row = {
            "id": f"round_context_{uuid.uuid4().hex}",
            "room_id": str(round_row.get("room_id") or ""),
            "round_id": str(round_row.get("id") or ""),
            "context_version": context_version,
            "owner_pack_id": self.owner_pack_id,
            "adapter_id": str(implementation.get("adapter_id") or ""),
            "adapter_version": str(implementation.get("adapter_version") or ""),
            "adapter_contract_sha256": str(
                implementation.get("adapter_contract_sha256") or ""
            ),
            "port_id": self.port_id,
            "port_version": str(implementation.get("port_version") or ""),
            "port_contract_sha256": str(
                implementation.get("port_contract_sha256") or ""
            ),
            "source_status": "none",
            "source_artifact_id": "",
            "source_artifact_version": 0,
            "artifact_snapshot_sha256": "",
            "evidence_review_event_sha256": "",
            "evidence_relations_sha256": "",
            "plugin_registry_snapshot_sha256": str(
                round_row.get("plugin_registry_snapshot_sha256") or ""
            ),
            "plugin_lifecycle_resolution_sha256": str(
                round_row.get("plugin_lifecycle_resolution_sha256") or ""
            ),
            "authorization_json": json.dumps(authorization, ensure_ascii=False),
            "authorization_sha256": canonical_sha256(authorization),
            "input_seal_json": json.dumps(expected_input_seal, ensure_ascii=False),
            "input_seal_sha256": canonical_sha256(expected_input_seal),
            "preview_json": json.dumps(expected_preview, ensure_ascii=False),
            "preview_sha256": preview_sha256,
            "output_sha256": preview_sha256,
            "provider_calls_performed": 0,
            "market_reads_performed": 0,
            "adapter_business_writes_performed": 0,
            "created_at": int(round_row.get("created_at") or 0),
        }
        row["binding_sha256"] = canonical_sha256(
            round_context_binding_payload(row)
        )
        return row

    def verify_row(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        context_row: Mapping[str, Any],
    ) -> dict[str, Any]:
        (
            _pack_id,
            _port_id,
            _action_id,
            _football_error_class,
            service_class,
            validate_contract,
        ) = _football_imports()
        row = dict(context_row)
        try:
            authorization = _normalize_football_authorization(
                json.loads(str(row.get("authorization_json") or "{}"))
            )
            input_seal = json.loads(str(row.get("input_seal_json") or "{}"))
            preview = json.loads(str(row.get("preview_json") or "{}"))
            contract = validate_contract(preview.get("contract"))
            snapshot = json.loads(
                str(round_row.get("plugin_registry_snapshot_json") or "{}")
            )
            lifecycle = json.loads(
                str(round_row.get("plugin_lifecycle_resolution_json") or "{}")
            )
        except Exception as exc:
            raise RoundContextError(
                "Frozen football round-context JSON failed validation.",
                code="FOOTBALL_ROUND_CONTEXT_INTEGRITY_FAILED",
            ) from exc
        expected_preview = service_class._closed_view_model(
            str(row.get("room_id") or ""),
            contract,
        )
        service_class(store)._verify_material_bindings(
            connection,
            str(row.get("room_id") or ""),
            contract,
        )
        implementation = _snapshot_provider_resolution(
            snapshot,
            (self.owner_pack_id, self.port_id),
        )
        expected_input_seal = _football_input_seal(
            room_id=str(row.get("room_id") or ""),
            preview=expected_preview,
            plugin_registry_snapshot_sha256=str(
                round_row.get("plugin_registry_snapshot_sha256") or ""
            ),
            plugin_lifecycle_head_set_sha256=str(
                lifecycle.get("lifecycle_head_set_sha256") or ""
            ),
        )
        input_sha256 = canonical_sha256(expected_input_seal)
        preview_sha256 = canonical_sha256(expected_preview)
        authorization_sha256 = canonical_sha256(authorization)
        binding_sha256 = canonical_sha256(round_context_binding_payload(row))
        if any((
            row.get("context_version") != "round_domain_context_v1",
            row.get("owner_pack_id") != self.owner_pack_id,
            row.get("port_id") != self.port_id,
            row.get("room_id") != round_row.get("room_id"),
            row.get("round_id") != round_row.get("id"),
            int(row.get("created_at") or 0)
            != int(round_row.get("created_at") or 0),
            row.get("adapter_id") != implementation.get("adapter_id"),
            row.get("adapter_version") != implementation.get("adapter_version"),
            row.get("adapter_contract_sha256")
            != implementation.get("adapter_contract_sha256"),
            row.get("port_version") != implementation.get("port_version"),
            row.get("port_contract_sha256")
            != implementation.get("port_contract_sha256"),
            row.get("plugin_registry_snapshot_sha256")
            != round_row.get("plugin_registry_snapshot_sha256"),
            row.get("plugin_lifecycle_resolution_sha256")
            != round_row.get("plugin_lifecycle_resolution_sha256"),
            row.get("authorization_sha256") != authorization_sha256,
            row.get("input_seal_sha256") != input_sha256,
            row.get("preview_sha256") != preview_sha256,
            row.get("output_sha256") != preview_sha256,
            row.get("binding_sha256") != binding_sha256,
            input_seal != expected_input_seal,
            preview != expected_preview,
            authorization["contract_sha256"]
            != str(expected_preview.get("contract_sha256") or ""),
            authorization["data_cutoff_utc"]
            != str(expected_preview.get("data_cutoff_utc") or ""),
            authorization["match_id"]
            != _football_match_id(expected_preview.get("contract") or {}),
            row.get("source_status") != "none",
            bool(str(row.get("source_artifact_id") or "")),
            int(row.get("source_artifact_version") or 0) != 0,
            bool(str(row.get("artifact_snapshot_sha256") or "")),
            bool(str(row.get("evidence_review_event_sha256") or "")),
            bool(str(row.get("evidence_relations_sha256") or "")),
            int(row.get("provider_calls_performed") or 0) != 0,
            int(row.get("market_reads_performed") or 0) != 0,
            int(row.get("adapter_business_writes_performed") or 0) != 0,
        )):
            raise RoundContextError(
                "Frozen football round-context seals do not match.",
                code="FOOTBALL_ROUND_CONTEXT_INTEGRITY_FAILED",
            )
        return {
            "authorization": authorization,
            "input_seal": input_seal,
            "preview": preview,
            "binding_sha256": binding_sha256,
        }


def _stock_imports() -> tuple[Any, ...]:
    from .stock_research import (
        STOCK_RESEARCH_CAPABILITY_PACK_ID,
        StockResearchContractError,
        validate_stock_research_contract,
        validate_stock_room_scope,
    )
    from .stock_research_service import (
        STOCK_RESEARCH_ACTION_ID,
        STOCK_RESEARCH_PORT_ID,
        StockResearchError,
        StockResearchService,
    )

    return (
        STOCK_RESEARCH_CAPABILITY_PACK_ID,
        STOCK_RESEARCH_PORT_ID,
        STOCK_RESEARCH_ACTION_ID,
        StockResearchContractError,
        StockResearchError,
        StockResearchService,
        validate_stock_research_contract,
        validate_stock_room_scope,
    )


def _stock_material_bindings(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_identity: dict[tuple[str, int], dict[str, Any]] = {}

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            binding = value.get("material_binding")
            if isinstance(binding, Mapping):
                clean = {
                    "material_id": str(binding.get("material_id") or ""),
                    "material_version": int(binding.get("material_version") or 0),
                    "content_sha256": str(binding.get("content_sha256") or ""),
                    "snapshot_sha256": str(binding.get("snapshot_sha256") or ""),
                }
                identity = (clean["material_id"], clean["material_version"])
                existing = by_identity.get(identity)
                if existing is not None and existing != clean:
                    raise RoundContextError(
                        "Stock material identity has conflicting bindings.",
                        code="STOCK_ROUND_CONTEXT_MATERIAL_CONFLICT",
                    )
                by_identity[identity] = clean
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(contract)
    if not by_identity:
        raise RoundContextError(
            "Stock round context has no exact material bindings.",
            code="STOCK_ROUND_CONTEXT_MATERIAL_MISSING",
        )
    return [by_identity[key] for key in sorted(by_identity)]


def _normalize_stock_authorization(value: Any) -> dict[str, Any]:
    (
        stock_pack_id,
        stock_port_id,
        _action_id,
        _contract_error,
        _service_error,
        _service,
        _validator,
        _scope_validator,
    ) = _stock_imports()
    keys = {
        "version",
        "owner_pack_id",
        "port_id",
        "contract_sha256",
        "stock_room_scope_sha256",
        "data_cutoff_utc",
        "user_confirmed",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value.get("version") != STOCK_ROUND_CONTEXT_AUTHORIZATION_VERSION
        or value.get("owner_pack_id") != stock_pack_id
        or value.get("port_id") != stock_port_id
        or not _SHA256_PATTERN.fullmatch(str(value.get("contract_sha256") or ""))
        or not _SHA256_PATTERN.fullmatch(
            str(value.get("stock_room_scope_sha256") or "")
        )
        or not isinstance(value.get("data_cutoff_utc"), str)
        or not str(value.get("data_cutoff_utc") or "")
        or len(str(value.get("data_cutoff_utc") or "")) > 40
        or value.get("user_confirmed") is not True
    ):
        raise RoundContextError(
            "Stock round-context authorization is invalid.",
            code="STOCK_ROUND_CONTEXT_AUTHORIZATION_INVALID",
            status=400,
        )
    return copy.deepcopy(value)


def _stock_scope_from_room(room: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    *_, validate_scope = _stock_imports()
    try:
        scope = validate_scope(dict(room))
    except Exception as exc:
        raise RoundContextError(
            "Stock room scope failed validation.",
            code="STOCK_ROUND_CONTEXT_SCOPE_INVALID",
        ) from exc
    scope_sha256 = canonical_sha256(scope)
    if (
        room.get("stock_room_scope_integrity_ok") is not True
        or str(room.get("stock_room_scope_sha256") or "") != scope_sha256
    ):
        raise RoundContextError(
            "Stock room scope seal does not match.",
            code="STOCK_ROUND_CONTEXT_SCOPE_INVALID",
        )
    return scope, scope_sha256


def _stock_input_seal(
    *,
    room_id: str,
    preview: Mapping[str, Any],
    stock_room_scope: Mapping[str, Any],
    stock_room_scope_sha256: str,
    plugin_registry_snapshot_sha256: str,
    plugin_lifecycle_head_set_sha256: str,
) -> dict[str, Any]:
    contract = preview.get("contract")
    if not isinstance(contract, dict):
        raise RoundContextError(
            "Stock round-context preview has no closed contract.",
            code="STOCK_ROUND_CONTEXT_PREPARED_INVALID",
        )
    return {
        "version": STOCK_ROUND_CONTEXT_INPUT_SEAL_VERSION,
        "room_id": str(room_id),
        "contract_sha256": str(preview.get("contract_sha256") or ""),
        "stock_room_scope": copy.deepcopy(dict(stock_room_scope)),
        "stock_room_scope_sha256": str(stock_room_scope_sha256 or ""),
        "material_bindings": _stock_material_bindings(contract),
        "plugin_registry_snapshot_sha256": str(
            plugin_registry_snapshot_sha256 or ""
        ),
        "plugin_lifecycle_head_set_sha256": str(
            plugin_lifecycle_head_set_sha256 or ""
        ),
    }


def _stock_prepared_payload(
    *,
    room_id: str,
    preview: Mapping[str, Any],
    authorization: Any,
    stock_room_scope: Mapping[str, Any],
    stock_room_scope_sha256: str,
    plugin_registry_snapshot_sha256: str,
    plugin_lifecycle_head_set_sha256: str,
) -> dict[str, Any]:
    clean_authorization = _normalize_stock_authorization(authorization)
    contract = preview.get("contract")
    if not isinstance(contract, dict):
        raise RoundContextError(
            "Stock round-context preview is invalid.",
            code="STOCK_ROUND_CONTEXT_PREPARED_INVALID",
        )
    if any((
        str(preview.get("contract_sha256") or "")
        != clean_authorization["contract_sha256"],
        str(preview.get("data_cutoff_utc") or "")
        != clean_authorization["data_cutoff_utc"],
        stock_room_scope_sha256
        != clean_authorization["stock_room_scope_sha256"],
        contract.get("stock_room_scope") != dict(stock_room_scope),
    )):
        raise RoundContextError(
            "Stock research preview changed after explicit authorization.",
            code="STOCK_ROUND_CONTEXT_PREVIEW_DRIFT",
        )
    input_seal = _stock_input_seal(
        room_id=room_id,
        preview=preview,
        stock_room_scope=stock_room_scope,
        stock_room_scope_sha256=stock_room_scope_sha256,
        plugin_registry_snapshot_sha256=plugin_registry_snapshot_sha256,
        plugin_lifecycle_head_set_sha256=plugin_lifecycle_head_set_sha256,
    )
    preview_copy = copy.deepcopy(dict(preview))
    preview_sha256 = canonical_sha256(preview_copy)
    return {
        "version": STOCK_ROUND_CONTEXT_PREPARED_VERSION,
        "authorization": clean_authorization,
        "input_seal": input_seal,
        "input_seal_sha256": canonical_sha256(input_seal),
        "preview": preview_copy,
        "preview_sha256": preview_sha256,
        "output_sha256": preview_sha256,
    }


def prepare_stock_round_context(
    store: "StudioStore",
    room_id: str,
    payload: Any,
    authorization: Any,
) -> dict[str, Any]:
    """Prepare one explicitly authorized stock entry in one read snapshot."""

    (
        stock_pack_id,
        stock_port_id,
        stock_action_id,
        _contract_error,
        _service_error,
        service_class,
        _validator,
        _scope_validator,
    ) = _stock_imports()
    inspect_from_connection = getattr(
        service_class(store), "inspect_from_connection", None
    )
    if not callable(inspect_from_connection):
        raise RoundContextError(
            "Stock service does not expose same-snapshot inspection.",
            code="STOCK_ROUND_CONTEXT_IMPLEMENTATION_UNAVAILABLE",
            status=503,
        )
    try:
        with closing(store._connect()) as connection:
            connection.execute("BEGIN")
            preview = inspect_from_connection(connection, room_id, payload)
            room = store._require_room_plugin_action_connection(
                connection,
                room_id,
                stock_action_id,
            )
            stock_room_scope, stock_room_scope_sha256 = _stock_scope_from_room(
                room
            )
            lifecycle = room.get("plugin_lifecycle_current") or {}
            prepared = _stock_prepared_payload(
                room_id=room_id,
                preview=preview,
                authorization=authorization,
                stock_room_scope=stock_room_scope,
                stock_room_scope_sha256=stock_room_scope_sha256,
                plugin_registry_snapshot_sha256=str(
                    room.get("plugin_registry_snapshot_sha256") or ""
                ),
                plugin_lifecycle_head_set_sha256=str(
                    lifecycle.get("current_head_set_sha256") or ""
                ),
            )
    except Exception as exc:
        raise _provider_error(
            exc,
            fallback_code="ROUND_CONTEXT_PROVIDER_PREPARE_FAILED",
        ) from exc
    return round_context_prepared_entry(
        stock_pack_id,
        stock_port_id,
        prepared,
    )


@dataclass(frozen=True)
class StockResearchContextProvider:
    owner_pack_id: str
    port_id: str

    @classmethod
    def create(cls) -> "StockResearchContextProvider":
        stock_pack_id, stock_port_id, *_ = _stock_imports()
        return cls(stock_pack_id, stock_port_id)

    def prepare_authorized(
        self,
        store: "StudioStore",
        room_id: str,
        request: Any,
    ) -> Any:
        if (
            not isinstance(request, dict)
            or set(request) != {"version", "payload", "authorization"}
            or request.get("version") != STOCK_ROUND_CONTEXT_REQUEST_VERSION
        ):
            raise RoundContextError(
                "Stock round-context request has an invalid closed shape.",
                code="STOCK_ROUND_CONTEXT_REQUEST_INVALID",
                status=400,
            )
        entry = prepare_stock_round_context(
            store,
            room_id,
            request.get("payload"),
            request.get("authorization"),
        )
        return copy.deepcopy(entry["prepared"])

    def prompt_section(self, value: Mapping[str, Any]) -> dict[str, Any]:
        preview = value.get("preview")
        if not isinstance(preview, dict):
            raise RoundContextError(
                "Stock round-context prepared payload has no preview.",
                code="ROUND_CONTEXT_PROVIDER_OUTPUT_INVALID",
            )
        (
            _pack_id,
            _port_id,
            _action_id,
            _contract_error,
            _service_error,
            service_class,
            validate_contract,
            _validate_scope,
        ) = _stock_imports()
        try:
            contract = validate_contract(preview.get("contract"))
            expected_preview = service_class._closed_view_model(
                str(preview.get("room_id") or ""),
                contract,
            )
        except Exception as exc:
            raise RoundContextError(
                "Stock prompt context is invalid.",
                code="ROUND_CONTEXT_PROVIDER_OUTPUT_INVALID",
            ) from exc
        if preview != expected_preview:
            raise RoundContextError(
                "Stock prompt context seals do not match.",
                code="ROUND_CONTEXT_PROVIDER_OUTPUT_INVALID",
            )
        payload = {
            "version": "stock_round_context_prompt_payload_v1",
            "view_model": copy.deepcopy(preview),
        }
        return {
            "version": ROUND_CONTEXT_PROMPT_SECTION_VERSION,
            "owner_pack_id": self.owner_pack_id,
            "port_id": self.port_id,
            "title": "Frozen stock research context",
            "payload": payload,
            "payload_sha256": canonical_sha256(payload),
        }

    def prepare_row(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        prepared: Any,
    ) -> dict[str, Any]:
        (
            _pack_id,
            _port_id,
            stock_action_id,
            _contract_error,
            service_error_class,
            service_class,
            validate_contract,
            _validate_scope,
        ) = _stock_imports()
        if (
            not isinstance(prepared, dict)
            or set(prepared) != {
                "version",
                "authorization",
                "input_seal",
                "input_seal_sha256",
                "preview",
                "preview_sha256",
                "output_sha256",
            }
            or prepared.get("version") != STOCK_ROUND_CONTEXT_PREPARED_VERSION
        ):
            raise RoundContextError(
                "Stock round-context prepared payload has an invalid closed shape.",
                code="STOCK_ROUND_CONTEXT_PREPARED_INVALID",
                status=400,
            )
        authorization = _normalize_stock_authorization(
            prepared.get("authorization")
        )
        preview = prepared.get("preview")
        input_seal = prepared.get("input_seal")
        if not isinstance(preview, dict) or not isinstance(input_seal, dict):
            raise RoundContextError(
                "Stock round-context seals are invalid.",
                code="STOCK_ROUND_CONTEXT_PREPARED_INVALID",
            )
        try:
            contract = validate_contract(preview.get("contract"))
        except Exception as exc:
            raise RoundContextError(
                "Stock round-context contract failed validation.",
                code="STOCK_ROUND_CONTEXT_PREPARED_INVALID",
            ) from exc
        inspect_from_connection = getattr(
            service_class(store), "inspect_from_connection", None
        )
        if not callable(inspect_from_connection):
            raise RoundContextError(
                "Stock service does not expose same-snapshot inspection.",
                code="STOCK_ROUND_CONTEXT_IMPLEMENTATION_UNAVAILABLE",
                status=503,
            )
        try:
            expected_preview = inspect_from_connection(
                connection,
                str(round_row.get("room_id") or ""),
                contract,
            )
            current_room = store._require_room_plugin_action_connection(
                connection,
                str(round_row.get("room_id") or ""),
                stock_action_id,
            )
            stock_room_scope, stock_room_scope_sha256 = (
                _stock_scope_from_room(current_room)
            )
        except service_error_class:
            raise
        try:
            snapshot = json.loads(
                str(round_row.get("plugin_registry_snapshot_json") or "{}")
            )
            lifecycle_resolution = json.loads(
                str(round_row.get("plugin_lifecycle_resolution_json") or "{}")
            )
        except json.JSONDecodeError as exc:
            raise RoundContextError(
                "Frozen plugin bindings are not valid JSON.",
                code="STOCK_ROUND_CONTEXT_BINDING_INVALID",
            ) from exc
        if not isinstance(snapshot, dict) or not isinstance(
            lifecycle_resolution, dict
        ):
            raise RoundContextError(
                "Frozen plugin bindings are invalid.",
                code="STOCK_ROUND_CONTEXT_BINDING_INVALID",
            )
        expected_input_seal = _stock_input_seal(
            room_id=str(round_row.get("room_id") or ""),
            preview=expected_preview,
            stock_room_scope=stock_room_scope,
            stock_room_scope_sha256=stock_room_scope_sha256,
            plugin_registry_snapshot_sha256=str(
                round_row.get("plugin_registry_snapshot_sha256") or ""
            ),
            plugin_lifecycle_head_set_sha256=str(
                lifecycle_resolution.get("lifecycle_head_set_sha256") or ""
            ),
        )
        preview_sha256 = canonical_sha256(expected_preview)
        if any((
            preview != expected_preview,
            input_seal != expected_input_seal,
            str(prepared.get("input_seal_sha256") or "")
            != canonical_sha256(expected_input_seal),
            str(prepared.get("preview_sha256") or "") != preview_sha256,
            str(prepared.get("output_sha256") or "") != preview_sha256,
            authorization["contract_sha256"]
            != str(expected_preview.get("contract_sha256") or ""),
            authorization["data_cutoff_utc"]
            != str(expected_preview.get("data_cutoff_utc") or ""),
            authorization["stock_room_scope_sha256"]
            != stock_room_scope_sha256,
            contract.get("stock_room_scope") != stock_room_scope,
        )):
            raise RoundContextError(
                "Stock round-context source or seals drifted before creation.",
                code="STOCK_ROUND_CONTEXT_SOURCE_DRIFT",
            )
        implementation = _snapshot_provider_resolution(
            snapshot,
            (self.owner_pack_id, self.port_id),
        )
        context_version = "round_domain_context_v1"
        row = {
            "id": f"round_context_{uuid.uuid4().hex}",
            "room_id": str(round_row.get("room_id") or ""),
            "round_id": str(round_row.get("id") or ""),
            "context_version": context_version,
            "owner_pack_id": self.owner_pack_id,
            "adapter_id": str(implementation.get("adapter_id") or ""),
            "adapter_version": str(implementation.get("adapter_version") or ""),
            "adapter_contract_sha256": str(
                implementation.get("adapter_contract_sha256") or ""
            ),
            "port_id": self.port_id,
            "port_version": str(implementation.get("port_version") or ""),
            "port_contract_sha256": str(
                implementation.get("port_contract_sha256") or ""
            ),
            "source_status": "none",
            "source_artifact_id": "",
            "source_artifact_version": 0,
            "artifact_snapshot_sha256": "",
            "evidence_review_event_sha256": "",
            "evidence_relations_sha256": "",
            "plugin_registry_snapshot_sha256": str(
                round_row.get("plugin_registry_snapshot_sha256") or ""
            ),
            "plugin_lifecycle_resolution_sha256": str(
                round_row.get("plugin_lifecycle_resolution_sha256") or ""
            ),
            "authorization_json": json.dumps(authorization, ensure_ascii=False),
            "authorization_sha256": canonical_sha256(authorization),
            "input_seal_json": json.dumps(expected_input_seal, ensure_ascii=False),
            "input_seal_sha256": canonical_sha256(expected_input_seal),
            "preview_json": json.dumps(expected_preview, ensure_ascii=False),
            "preview_sha256": preview_sha256,
            "output_sha256": preview_sha256,
            "provider_calls_performed": 0,
            "market_reads_performed": 0,
            "adapter_business_writes_performed": 0,
            "created_at": int(round_row.get("created_at") or 0),
        }
        row["binding_sha256"] = canonical_sha256(
            round_context_binding_payload(row)
        )
        return row

    def verify_row(
        self,
        store: "StudioStore",
        connection: "sqlite3.Connection",
        round_row: Mapping[str, Any],
        context_row: Mapping[str, Any],
    ) -> dict[str, Any]:
        (
            _pack_id,
            _port_id,
            _action_id,
            _contract_error,
            _service_error,
            service_class,
            validate_contract,
            _validate_scope,
        ) = _stock_imports()
        row = dict(context_row)
        try:
            authorization = _normalize_stock_authorization(
                json.loads(str(row.get("authorization_json") or "{}"))
            )
            input_seal = json.loads(str(row.get("input_seal_json") or "{}"))
            preview = json.loads(str(row.get("preview_json") or "{}"))
            contract = validate_contract(preview.get("contract"))
            snapshot = json.loads(
                str(round_row.get("plugin_registry_snapshot_json") or "{}")
            )
            lifecycle = json.loads(
                str(round_row.get("plugin_lifecycle_resolution_json") or "{}")
            )
        except Exception as exc:
            raise RoundContextError(
                "Frozen stock round-context JSON failed validation.",
                code="STOCK_ROUND_CONTEXT_INTEGRITY_FAILED",
            ) from exc
        expected_preview = service_class._closed_view_model(
            str(row.get("room_id") or ""),
            contract,
        )
        service_class(store)._verify_material_bindings(
            connection,
            str(row.get("room_id") or ""),
            contract,
        )
        stock_room_scope = contract.get("stock_room_scope")
        if not isinstance(stock_room_scope, dict):
            raise RoundContextError(
                "Frozen stock room scope is invalid.",
                code="STOCK_ROUND_CONTEXT_INTEGRITY_FAILED",
            )
        stock_room_scope_sha256 = canonical_sha256(stock_room_scope)
        implementation = _snapshot_provider_resolution(
            snapshot,
            (self.owner_pack_id, self.port_id),
        )
        expected_input_seal = _stock_input_seal(
            room_id=str(row.get("room_id") or ""),
            preview=expected_preview,
            stock_room_scope=stock_room_scope,
            stock_room_scope_sha256=stock_room_scope_sha256,
            plugin_registry_snapshot_sha256=str(
                round_row.get("plugin_registry_snapshot_sha256") or ""
            ),
            plugin_lifecycle_head_set_sha256=str(
                lifecycle.get("lifecycle_head_set_sha256") or ""
            ),
        )
        input_sha256 = canonical_sha256(expected_input_seal)
        preview_sha256 = canonical_sha256(expected_preview)
        authorization_sha256 = canonical_sha256(authorization)
        binding_sha256 = canonical_sha256(round_context_binding_payload(row))
        if any((
            row.get("context_version") != "round_domain_context_v1",
            row.get("owner_pack_id") != self.owner_pack_id,
            row.get("port_id") != self.port_id,
            row.get("room_id") != round_row.get("room_id"),
            row.get("round_id") != round_row.get("id"),
            int(row.get("created_at") or 0)
            != int(round_row.get("created_at") or 0),
            row.get("adapter_id") != implementation.get("adapter_id"),
            row.get("adapter_version") != implementation.get("adapter_version"),
            row.get("adapter_contract_sha256")
            != implementation.get("adapter_contract_sha256"),
            row.get("port_version") != implementation.get("port_version"),
            row.get("port_contract_sha256")
            != implementation.get("port_contract_sha256"),
            row.get("plugin_registry_snapshot_sha256")
            != round_row.get("plugin_registry_snapshot_sha256"),
            row.get("plugin_lifecycle_resolution_sha256")
            != round_row.get("plugin_lifecycle_resolution_sha256"),
            row.get("authorization_sha256") != authorization_sha256,
            row.get("input_seal_sha256") != input_sha256,
            row.get("preview_sha256") != preview_sha256,
            row.get("output_sha256") != preview_sha256,
            row.get("binding_sha256") != binding_sha256,
            input_seal != expected_input_seal,
            preview != expected_preview,
            authorization["contract_sha256"]
            != str(expected_preview.get("contract_sha256") or ""),
            authorization["data_cutoff_utc"]
            != str(expected_preview.get("data_cutoff_utc") or ""),
            authorization["stock_room_scope_sha256"]
            != stock_room_scope_sha256,
            row.get("source_status") != "none",
            bool(str(row.get("source_artifact_id") or "")),
            int(row.get("source_artifact_version") or 0) != 0,
            bool(str(row.get("artifact_snapshot_sha256") or "")),
            bool(str(row.get("evidence_review_event_sha256") or "")),
            bool(str(row.get("evidence_relations_sha256") or "")),
            int(row.get("provider_calls_performed") or 0) != 0,
            int(row.get("market_reads_performed") or 0) != 0,
            int(row.get("adapter_business_writes_performed") or 0) != 0,
        )):
            raise RoundContextError(
                "Frozen stock round-context seals do not match.",
                code="STOCK_ROUND_CONTEXT_INTEGRITY_FAILED",
            )
        return {
            "authorization": authorization,
            "input_seal": input_seal,
            "preview": preview,
            "binding_sha256": binding_sha256,
        }


_providers: list[RoundContextProvider] = [ProjectRoundFocusContextProvider()]
try:
    _providers.append(FootballResearchContextProvider.create())
except ImportError:  # pragma: no cover - optional pack registration boundary
    pass
try:
    _providers.append(StockResearchContextProvider.create())
except ImportError:  # pragma: no cover - optional pack registration boundary
    pass

DEFAULT_ROUND_CONTEXT_PROVIDERS = RoundContextProviderRegistry(_providers)


def prepare_authorized_set(
    store: "StudioStore",
    room_id: str,
    authorization_set: Any,
) -> dict[str, Any]:
    return DEFAULT_ROUND_CONTEXT_PROVIDERS.prepare_authorized_set(
        store,
        room_id,
        authorization_set,
    )


def prompt_sections(prepared_set: Any) -> list[dict[str, Any]]:
    return DEFAULT_ROUND_CONTEXT_PROVIDERS.prompt_sections(prepared_set)


__all__ = [
    "DEFAULT_ROUND_CONTEXT_PROVIDERS",
    "FOOTBALL_ROUND_CONTEXT_AUTHORIZATION_VERSION",
    "FOOTBALL_ROUND_CONTEXT_INPUT_SEAL_VERSION",
    "FOOTBALL_ROUND_CONTEXT_PREPARED_VERSION",
    "FOOTBALL_ROUND_CONTEXT_REQUEST_VERSION",
    "MAX_ROUND_CONTEXTS",
    "ROUND_CONTEXT_ANCHOR_VERSION",
    "ROUND_CONTEXT_AUTHORIZATION_ENTRY_VERSION",
    "ROUND_CONTEXT_AUTHORIZATION_SET_VERSION",
    "ROUND_CONTEXT_PREPARED_ENTRY_VERSION",
    "ROUND_CONTEXT_PREPARED_SET_VERSION",
    "ROUND_CONTEXT_PROVIDER_REGISTRY_VERSION",
    "ROUND_CONTEXT_PROMPT_SECTION_VERSION",
    "ROUND_CONTEXT_RECORD_SET_VERSION",
    "ROUND_CONTEXT_RECORD_VERSION",
    "STOCK_ROUND_CONTEXT_AUTHORIZATION_VERSION",
    "STOCK_ROUND_CONTEXT_INPUT_SEAL_VERSION",
    "STOCK_ROUND_CONTEXT_PREPARED_VERSION",
    "STOCK_ROUND_CONTEXT_REQUEST_VERSION",
    "FootballResearchContextProvider",
    "ProjectRoundFocusContextProvider",
    "StockResearchContextProvider",
    "RoundContextError",
    "RoundContextProvider",
    "RoundContextProviderRegistry",
    "build_round_context_authorization_set",
    "build_round_context_prepared",
    "coerce_round_context_authorization_set",
    "normalize_round_context_authorizations",
    "normalize_round_context_prepared",
    "prepare_authorized_set",
    "prepare_football_round_context",
    "prepare_stock_round_context",
    "prompt_sections",
    "round_context_anchor_sha256",
    "round_context_binding_payload",
    "round_context_authorization_entry",
    "round_context_prepared_entry",
]
