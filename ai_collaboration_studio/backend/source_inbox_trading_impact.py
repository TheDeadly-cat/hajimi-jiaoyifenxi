from __future__ import annotations

"""Immutable persistence for deterministic trading-impact projections.

The source item, source packet, and Source Inbox receipt remain the authority
for the imported evidence.  This module stores a separate, append-only
derivation that is bound to that immutable item.  It deliberately performs no
Provider, model, market, network, round, account, order, or execution work.

Transaction ownership stays with the caller.  In particular,
``insert_or_verify_trading_impact_projection`` never begins, commits, or rolls
back a transaction, so Source Inbox can make the import, item links, audit
events, and this sidecar one atomic unit.
"""

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any

from .source_inbox_contracts import project_source_item_fingerprint
from .source_monitoring.contracts import (
    FUTU_ANOMALY_SOURCE_CHANNEL,
    MAX_NATIVE_INTEGER,
    OFFICIAL_SOURCE_CHANNEL,
    OFFICIAL_SOURCE_CLASS,
    READONLY_MARKET_SOURCE_CLASS,
)
from .source_monitoring.trading_impact_rules import (
    TradingImpactProjection,
    TradingImpactRulesV1,
)


SOURCE_INBOX_TRADING_IMPACT_RECORD_VERSION = (
    "source_inbox_trading_impact_projection_record_v1"
)
SOURCE_INBOX_TRADING_IMPACT_RECEIPT_VERSION = (
    "source_inbox_trading_impact_receipt_v1"
)
SOURCE_INBOX_TRADING_IMPACT_MIGRATION_KEY = (
    "source_inbox_trading_impact_projection_v1"
)

TRADING_IMPACT_PROJECTION_VERSION = "trading_impact_projection_v1"
TRADING_IMPACT_RULESET_VERSION = "trading_impact_rules_v1"
TRADING_IMPACT_MAPPING_VERSION = "trading_impact_mapping_v1"
TRADING_IMPACT_PROJECTION_KEY_VERSION = "trading_impact_projection_key_v1"

TRADING_IMPACT_STATUS_MATCHED = "MATCHED"
TRADING_IMPACT_STATUS_NO_MATCH = "NO_MATCH"
MAX_TRADING_IMPACT_PROJECTION_BYTES = 16 * 1024
MAX_TRADING_IMPACT_RECEIPT_BYTES = 4 * 1024

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]{0,159}\Z")
_SOURCE_CLASS_BY_CHANNEL = {
    OFFICIAL_SOURCE_CHANNEL: OFFICIAL_SOURCE_CLASS,
    FUTU_ANOMALY_SOURCE_CHANNEL: READONLY_MARKET_SOURCE_CLASS,
}
_ENGINE_ACCOUNTING = {
    "scope": "trading_impact_engine_only",
    "model_calls_performed": 0,
    "provider_calls_performed": 0,
    "network_requests_performed": 0,
    "market_calls_performed": 0,
    "database_writes_performed": 0,
}
_RECEIPT_SAFETY = {
    **_ENGINE_ACCOUNTING,
    "formal_rounds_created": 0,
    "live_trading_allowed": False,
    "execution_capability": "none",
}


class SourceInboxTradingImpactError(ValueError):
    """A bounded persistence, integrity, or semantic-conflict failure."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class _DuplicateJSONKey(ValueError):
    pass


def _error(code: str, message: str) -> SourceInboxTradingImpactError:
    return SourceInboxTradingImpactError(message, code=code)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_JSON_INVALID",
            "trading-impact value is not canonical native JSON",
        ) from exc


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _load_canonical_object(value: Any, *, label: str) -> dict[str, Any]:
    if type(value) is not str:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            f"{label} must be canonical JSON text",
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise _DuplicateJSONKey(key)
            result[key] = item
        return result

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=unique_object,
            parse_constant=lambda _constant: (_ for _ in ()).throw(
                ValueError("non-finite JSON number")
            ),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError, _DuplicateJSONKey) as exc:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            f"{label} is not strict JSON",
        ) from exc
    if type(parsed) is not dict or _canonical_json(parsed) != value:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            f"{label} is not a canonical JSON object",
        )
    return parsed


def _row_dict(cursor: sqlite3.Cursor, row: Any) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return dict(row)
    names = [str(column[0]) for column in (cursor.description or ())]
    if type(row) not in {tuple, list} or len(names) != len(row):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "SQLite returned an invalid trading-impact row",
        )
    return dict(zip(names, row))


def _native_non_negative(value: Any, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_INPUT_INVALID",
            f"{field} must be a non-negative native signed 64-bit integer",
        )
    return value


def _bounded_token(value: Any, *, field: str) -> str:
    if type(value) is not str or not _TOKEN_RE.fullmatch(value):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_INPUT_INVALID",
            f"{field} must be a canonical lowercase token",
        )
    return value


def _identifier(value: Any, *, field: str, maximum: int = 200) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_INPUT_INVALID",
            f"{field} is not a canonical bounded identifier",
        )
    return value


def _sha256(value: Any, *, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_INPUT_INVALID",
            f"{field} must be a lowercase SHA-256 digest",
        )
    return value


def _normalize_projection(value: Any) -> dict[str, Any]:
    raw = value.to_dict() if type(value) is TradingImpactProjection else value
    try:
        projection = TradingImpactProjection.build(raw).to_dict()
    except Exception as exc:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact projection failed its immutable value contract",
        ) from exc
    if type(projection) is not dict:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact projection must normalize to a native object",
        )
    if len(_canonical_json(projection).encode("utf-8")) > MAX_TRADING_IMPACT_PROJECTION_BYTES:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact projection exceeds the 16 KiB persistence bound",
        )
    expected_fields = {
        "version",
        "ruleset_version",
        "ruleset_sha256",
        "mapping_version",
        "projection_key_version",
        "projection_key_sha256",
        "source_binding",
        "source_item_binding",
        "evaluation",
        "matched_rule_ids",
        "hypotheses",
        "verification_state",
        "interpretation_boundary",
        "accounting",
        "projection_sha256",
    }
    if set(projection) != expected_fields:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact projection fields do not match v1",
        )
    if (
        projection.get("version") != TRADING_IMPACT_PROJECTION_VERSION
        or projection.get("ruleset_version") != TRADING_IMPACT_RULESET_VERSION
        or projection.get("mapping_version") != TRADING_IMPACT_MAPPING_VERSION
        or projection.get("projection_key_version")
        != TRADING_IMPACT_PROJECTION_KEY_VERSION
        or projection.get("verification_state") != "external_unverified"
    ):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact projection version or verification state is invalid",
        )
    _sha256(projection.get("ruleset_sha256"), field="ruleset_sha256")
    _sha256(
        projection.get("projection_key_sha256"),
        field="projection_key_sha256",
    )
    supplied_projection_sha256 = _sha256(
        projection.get("projection_sha256"),
        field="projection_sha256",
    )
    projection_basis = {
        key: copy.deepcopy(item)
        for key, item in projection.items()
        if key != "projection_sha256"
    }
    if supplied_projection_sha256 != _canonical_sha256(projection_basis):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact projection SHA does not match its canonical content",
        )
    if projection.get("accounting") != _ENGINE_ACCOUNTING:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_CAPABILITY_FORBIDDEN",
            "trading-impact projection does not preserve the exact zero-call boundary",
        )
    if projection.get("interpretation_boundary") != {
        "directional_forecast": False,
        "causal_attribution": "none",
        "profitability_claim": False,
        "execution_authority": "none",
        "user_review_required": True,
    }:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_CAPABILITY_FORBIDDEN",
            "trading-impact projection interpretation boundary is invalid",
        )
    source_binding = projection.get("source_binding")
    if type(source_binding) is not dict or set(source_binding) != {
        "adapter_id",
        "source_class",
        "source_channel",
    }:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact source binding is invalid",
        )
    source_item_binding = projection.get("source_item_binding")
    if type(source_item_binding) is not dict or set(source_item_binding) != {
        "item_version",
        "external_item_id",
        "item_type",
        "item_sha256",
        "server_fingerprint_version",
        "server_fingerprint",
        "source_semantic_binding",
    }:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact source-item binding is invalid",
        )
    matched_rule_ids = projection.get("matched_rule_ids")
    hypotheses = projection.get("hypotheses")
    evaluation = projection.get("evaluation")
    if type(matched_rule_ids) is not list or type(hypotheses) is not list:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact matches and hypotheses must be native lists",
        )
    if evaluation == "no_match":
        if matched_rule_ids or hypotheses:
            raise _error(
                "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
                "no-match projection cannot contain matches or hypotheses",
            )
    elif evaluation == "matched":
        if not matched_rule_ids or not hypotheses:
            raise _error(
                "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
                "matched projection must contain matches and hypotheses",
            )
    else:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact evaluation must be matched or no_match",
        )
    if len(hypotheses) > 20:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
            "trading-impact hypothesis count exceeds the Source Inbox limit",
        )
    return copy.deepcopy(projection)


def _projection_binding(projection: dict[str, Any]) -> dict[str, Any]:
    hypothesis_sha256s: list[str] = []
    used_source_indexes: set[int] = set()
    for index, hypothesis in enumerate(projection["hypotheses"]):
        if type(hypothesis) is not dict:
            raise _error(
                "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
                f"hypotheses[{index}] must be a native object",
            )
        hypothesis_sha256s.append(
            _sha256(
                hypothesis.get("hypothesis_sha256"),
                field=f"hypotheses[{index}].hypothesis_sha256",
            )
        )
        impact_hypothesis = hypothesis.get("impact_hypothesis")
        source_indexes = (
            impact_hypothesis.get("source_indexes")
            if type(impact_hypothesis) is dict
            else None
        )
        if type(source_indexes) is not list:
            raise _error(
                "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
                f"hypotheses[{index}] source indexes are invalid",
            )
        for source_index in source_indexes:
            if type(source_index) is not int or source_index < 0:
                raise _error(
                    "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_INVALID",
                    f"hypotheses[{index}] contains an invalid source index",
                )
            used_source_indexes.add(source_index)
    return {
        "version": "trading_impact_projection_receipt_binding_v1",
        "projection_version": projection["version"],
        "projection_key_version": projection["projection_key_version"],
        "projection_key_sha256": projection["projection_key_sha256"],
        "projection_sha256": projection["projection_sha256"],
        "ruleset_version": projection["ruleset_version"],
        "ruleset_sha256": projection["ruleset_sha256"],
        "evaluation": projection["evaluation"],
        "matched_rule_ids": copy.deepcopy(projection["matched_rule_ids"]),
        "hypothesis_sha256s": hypothesis_sha256s,
        "hypothesis_count": len(hypothesis_sha256s),
        "used_source_indexes": sorted(used_source_indexes),
    }


def _normalized_source_item(
    source_item: Any,
    *,
    source_item_sha256: Any,
) -> tuple[dict[str, Any], str, str]:
    if type(source_item) is not dict:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_INPUT_INVALID",
            "source_item must be a native normalized object",
        )
    item = copy.deepcopy(source_item)
    item_sha256 = _sha256(source_item_sha256, field="source_item_sha256")
    if _canonical_sha256(item) != item_sha256:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_ITEM_CONFLICT",
            "source item SHA does not match its canonical content",
        )
    fingerprint_version = _identifier(
        item.get("server_fingerprint_version"),
        field="server_fingerprint_version",
        maximum=160,
    )
    fingerprint = _sha256(
        item.get("server_fingerprint"),
        field="server_fingerprint",
    )
    if project_source_item_fingerprint(item) != fingerprint:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_ITEM_CONFLICT",
            "source item fingerprint does not match its stable identity",
        )
    if item.get("external_claims_verification") != "external_unverified":
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_ITEM_CONFLICT",
            "source item must remain external_unverified",
        )
    return item, fingerprint_version, fingerprint


def _stored_item(
    connection: sqlite3.Connection,
    *,
    item_id: str,
) -> dict[str, Any]:
    cursor = connection.execute(
        """SELECT id,origin_import_id,server_fingerprint,item_sha256,item_json
             FROM source_inbox_items WHERE id=?""",
        (item_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_ITEM_NOT_FOUND",
            "source item does not exist",
        )
    data = _row_dict(cursor, row)
    item = _load_canonical_object(data.get("item_json"), label="source item")
    if (
        str(data.get("id") or "") != item_id
        or str(data.get("item_sha256") or "") != _canonical_sha256(item)
        or str(data.get("server_fingerprint") or "")
        != str(item.get("server_fingerprint") or "")
        or project_source_item_fingerprint(item)
        != str(item.get("server_fingerprint") or "")
    ):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "stored source item failed its SHA or fingerprint mirror",
        )
    return {**data, "item": item}


def _evaluation_binding(
    connection: sqlite3.Connection,
    *,
    evaluation_import_id: str,
    item_id: str,
) -> dict[str, Any]:
    cursor = connection.execute(
        """SELECT imports.id,imports.source_channel,imports.source_key,
                  imports.external_run_id,imports.import_key_sha256,
                  imports.normalized_packet_sha256,
                  link.position,link.disposition
             FROM source_inbox_imports imports
             JOIN source_inbox_import_items link ON link.import_id=imports.id
            WHERE imports.id=? AND link.item_id=?""",
        (evaluation_import_id, item_id),
    )
    row = cursor.fetchone()
    if row is None:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_IMPORT_BINDING_INVALID",
            "evaluation import is not linked to the source item",
        )
    data = _row_dict(cursor, row)
    channel = str(data.get("source_channel") or "")
    if channel not in _SOURCE_CLASS_BY_CHANNEL:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_IMPORT_BINDING_INVALID",
            "evaluation import is outside the sealed monitoring channels",
        )
    if str(data.get("disposition") or "") not in {"CREATED", "DUPLICATE"}:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "evaluation import link disposition is invalid",
        )
    position = data.get("position")
    if type(position) is not int or position < 0:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "evaluation import link position is invalid",
        )
    import_key_sha256 = _sha256(
        data.get("import_key_sha256"),
        field="evaluation_import.import_key_sha256",
    )
    normalized_packet_sha256 = _sha256(
        data.get("normalized_packet_sha256"),
        field="evaluation_import.normalized_packet_sha256",
    )
    return {
        "id": _identifier(data.get("id"), field="evaluation_import.id"),
        "source_channel": channel,
        "source_class": _SOURCE_CLASS_BY_CHANNEL[channel],
        "source_key": _identifier(
            data.get("source_key"),
            field="evaluation_import.source_key",
            maximum=160,
        ),
        "external_run_id": _identifier(
            data.get("external_run_id"),
            field="evaluation_import.external_run_id",
        ),
        "import_key_sha256": import_key_sha256,
        "normalized_packet_sha256": normalized_packet_sha256,
        "position": position,
        "disposition": str(data.get("disposition")),
    }


def _verify_projection_bindings(
    projection: dict[str, Any],
    *,
    source_item: dict[str, Any],
    source_item_sha256: str,
    fingerprint_version: str,
    fingerprint: str,
    evaluation: dict[str, Any],
) -> None:
    expected_source_binding = {
        "adapter_id": evaluation["source_key"],
        "source_class": evaluation["source_class"],
        "source_channel": evaluation["source_channel"],
    }
    if projection.get("source_binding") != expected_source_binding:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_IMPORT_BINDING_INVALID",
            "projection source binding does not match its evaluation import",
        )
    try:
        expected_semantic_binding = TradingImpactRulesV1.source_semantic_binding(
            source_item,
            item_sha256=source_item_sha256,
            adapter_id=evaluation["source_key"],
            source_class=evaluation["source_class"],
            source_channel=evaluation["source_channel"],
        )
    except Exception as exc:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_ITEM_CONFLICT",
            "immutable parent item cannot reproduce its sealed semantic binding",
        ) from exc
    expected_item_binding = {
        "item_version": str(source_item.get("version") or ""),
        "external_item_id": str(source_item.get("external_item_id") or ""),
        "item_type": str(source_item.get("item_type") or ""),
        "item_sha256": source_item_sha256,
        "server_fingerprint_version": fingerprint_version,
        "server_fingerprint": fingerprint,
        "source_semantic_binding": expected_semantic_binding,
    }
    if projection.get("source_item_binding") != expected_item_binding:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_ITEM_CONFLICT",
            "projection source-item binding does not match the immutable item",
        )


def _build_receipt(
    *,
    projection_id: str,
    evaluation: dict[str, Any],
    item_id: str,
    origin_import_id: str,
    source_item_sha256: str,
    fingerprint_version: str,
    fingerprint: str,
    projection: dict[str, Any],
    created_at_ms: int,
) -> dict[str, Any]:
    receipt = {
        "version": SOURCE_INBOX_TRADING_IMPACT_RECEIPT_VERSION,
        "projection_id": projection_id,
        "evaluation_import": copy.deepcopy(evaluation),
        "source_item": {
            "id": item_id,
            "origin_import_id": origin_import_id,
            "item_sha256": source_item_sha256,
            "server_fingerprint_version": fingerprint_version,
            "server_fingerprint": fingerprint,
        },
        "projection_binding": _projection_binding(projection),
        "first_created_at_ms": created_at_ms,
        "safety": copy.deepcopy(_RECEIPT_SAFETY),
    }
    receipt["receipt_sha256"] = _canonical_sha256(receipt)
    if len(_canonical_json(receipt).encode("utf-8")) > MAX_TRADING_IMPACT_RECEIPT_BYTES:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECEIPT_TOO_LARGE",
            "trading-impact receipt exceeds the 4 KiB persistence bound",
        )
    return receipt


def ensure_source_inbox_trading_impact_schema(
    connection: sqlite3.Connection,
    *,
    applied_at_ms: int,
) -> None:
    """Create the additive sidecar schema inside the controlled initializer."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    applied_at = _native_non_negative(applied_at_ms, field="applied_at_ms")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS source_inbox_trading_impact_projections (
            id TEXT PRIMARY KEY,
            record_version TEXT NOT NULL
                CHECK(record_version='source_inbox_trading_impact_projection_record_v1'),
            evaluation_import_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            source_adapter_id TEXT NOT NULL,
            source_class TEXT NOT NULL
                CHECK(source_class IN ('official_source','readonly_market')),
            source_channel TEXT NOT NULL
                CHECK(source_channel IN ('official_source_monitor','futu_anomaly_monitor')),
            source_item_sha256 TEXT NOT NULL CHECK(length(source_item_sha256)=64),
            server_fingerprint_version TEXT NOT NULL,
            server_fingerprint TEXT NOT NULL CHECK(length(server_fingerprint)=64),
            ruleset_version TEXT NOT NULL
                CHECK(length(ruleset_version) BETWEEN 1 AND 160),
            ruleset_sha256 TEXT NOT NULL CHECK(length(ruleset_sha256)=64),
            input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64),
            projection_key_sha256 TEXT NOT NULL CHECK(length(projection_key_sha256)=64),
            projection_json TEXT NOT NULL,
            projection_sha256 TEXT NOT NULL CHECK(length(projection_sha256)=64),
            receipt_json TEXT NOT NULL,
            receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64),
            status TEXT NOT NULL CHECK(status IN ('MATCHED','NO_MATCH')),
            matched_rule_count INTEGER NOT NULL CHECK(matched_rule_count>=0),
            hypothesis_count INTEGER NOT NULL CHECK(hypothesis_count BETWEEN 0 AND 20),
            provider_calls_performed INTEGER NOT NULL DEFAULT 0
                CHECK(provider_calls_performed=0),
            model_calls_performed INTEGER NOT NULL DEFAULT 0
                CHECK(model_calls_performed=0),
            market_calls_performed INTEGER NOT NULL DEFAULT 0
                CHECK(market_calls_performed=0),
            network_requests_performed INTEGER NOT NULL DEFAULT 0
                CHECK(network_requests_performed=0),
            database_writes_performed INTEGER NOT NULL DEFAULT 0
                CHECK(database_writes_performed=0),
            formal_rounds_created INTEGER NOT NULL DEFAULT 0
                CHECK(formal_rounds_created=0),
            live_trading_allowed INTEGER NOT NULL DEFAULT 0
                CHECK(live_trading_allowed=0),
            execution_capability TEXT NOT NULL DEFAULT 'none'
                CHECK(execution_capability='none'),
            created_at_ms INTEGER NOT NULL CHECK(created_at_ms>=0),
            CHECK(
                (status='NO_MATCH' AND matched_rule_count=0 AND hypothesis_count=0)
                OR
                (status='MATCHED' AND matched_rule_count>0 AND hypothesis_count>0)
            ),
            FOREIGN KEY(evaluation_import_id)
                REFERENCES source_inbox_imports(id) ON DELETE RESTRICT,
            FOREIGN KEY(item_id)
                REFERENCES source_inbox_items(id) ON DELETE RESTRICT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_source_inbox_trading_impact_item_ruleset
            ON source_inbox_trading_impact_projections(item_id,ruleset_version);
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_source_inbox_trading_impact_projection_key
            ON source_inbox_trading_impact_projections(projection_key_sha256);
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_source_inbox_trading_impact_receipt_sha256
            ON source_inbox_trading_impact_projections(receipt_sha256);
        CREATE INDEX IF NOT EXISTS idx_source_inbox_trading_impact_import
            ON source_inbox_trading_impact_projections(evaluation_import_id);
        CREATE INDEX IF NOT EXISTS idx_source_inbox_trading_impact_ruleset_status
            ON source_inbox_trading_impact_projections(
                ruleset_version,status,created_at_ms DESC,id DESC
            );

        CREATE TRIGGER IF NOT EXISTS trg_source_inbox_trading_impact_no_update
        BEFORE UPDATE ON source_inbox_trading_impact_projections
        BEGIN
            SELECT RAISE(ABORT,'source inbox trading-impact projections are immutable');
        END;

        CREATE TRIGGER IF NOT EXISTS trg_source_inbox_trading_impact_no_delete
        BEFORE DELETE ON source_inbox_trading_impact_projections
        BEGIN
            SELECT RAISE(ABORT,'source inbox trading-impact projections are immutable');
        END;
        """
    )
    connection.execute(
        """INSERT OR IGNORE INTO schema_migrations(key,applied_at)
           VALUES(?,?)""",
        (SOURCE_INBOX_TRADING_IMPACT_MIGRATION_KEY, applied_at),
    )


def _select_projection_rows(
    connection: sqlite3.Connection,
    *,
    item_id: str = "",
    ruleset_version: str = "",
    projection_key_sha256: str = "",
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if item_id:
        clauses.append("item_id=?")
        parameters.append(item_id)
    if ruleset_version:
        clauses.append("ruleset_version=?")
        parameters.append(ruleset_version)
    if projection_key_sha256:
        clauses.append("projection_key_sha256=?")
        parameters.append(projection_key_sha256)
    where = " AND ".join(clauses) if clauses else "1=1"
    cursor = connection.execute(
        f"""SELECT * FROM source_inbox_trading_impact_projections
             WHERE {where} ORDER BY ruleset_version,created_at_ms,id""",
        parameters,
    )
    return [_row_dict(cursor, row) for row in cursor.fetchall()]


def _verify_record(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    *,
    expected_item: dict[str, Any] | None = None,
    expected_item_sha256: str = "",
) -> dict[str, Any]:
    projection_id = _identifier(row.get("id"), field="projection.id")
    if row.get("record_version") != SOURCE_INBOX_TRADING_IMPACT_RECORD_VERSION:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "trading-impact record version is invalid",
        )
    item_id = _identifier(row.get("item_id"), field="projection.item_id")
    evaluation_import_id = _identifier(
        row.get("evaluation_import_id"),
        field="projection.evaluation_import_id",
    )
    stored_item = _stored_item(connection, item_id=item_id)
    source_item = stored_item["item"]
    source_item_sha256 = _sha256(
        row.get("source_item_sha256"),
        field="projection.source_item_sha256",
    )
    if (
        source_item_sha256 != str(stored_item.get("item_sha256") or "")
        or expected_item_sha256 and source_item_sha256 != expected_item_sha256
        or expected_item is not None and source_item != expected_item
    ):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "trading-impact record is not bound to the stored source item",
        )
    fingerprint_version = _identifier(
        row.get("server_fingerprint_version"),
        field="projection.server_fingerprint_version",
        maximum=160,
    )
    fingerprint = _sha256(
        row.get("server_fingerprint"),
        field="projection.server_fingerprint",
    )
    if (
        fingerprint_version
        != str(source_item.get("server_fingerprint_version") or "")
        or fingerprint != str(source_item.get("server_fingerprint") or "")
    ):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "trading-impact fingerprint mirror is invalid",
        )
    evaluation = _evaluation_binding(
        connection,
        evaluation_import_id=evaluation_import_id,
        item_id=item_id,
    )
    projection = _normalize_projection(
        _load_canonical_object(row.get("projection_json"), label="impact projection")
    )
    _verify_projection_bindings(
        projection,
        source_item=source_item,
        source_item_sha256=source_item_sha256,
        fingerprint_version=fingerprint_version,
        fingerprint=fingerprint,
        evaluation=evaluation,
    )
    status = (
        TRADING_IMPACT_STATUS_MATCHED
        if projection["evaluation"] == "matched"
        else TRADING_IMPACT_STATUS_NO_MATCH
    )
    matched_rule_count = len(projection["matched_rule_ids"])
    hypothesis_count = len(projection["hypotheses"])
    input_sha256 = _canonical_sha256({
        "version": "trading_impact_sidecar_input_v1",
        "source_binding": projection["source_binding"],
        "source_item_binding": projection["source_item_binding"],
    })
    projection_sha256 = str(projection["projection_sha256"])
    projection_key_sha256 = str(projection["projection_key_sha256"])
    ruleset_version = _bounded_token(
        row.get("ruleset_version"),
        field="projection.ruleset_version",
    )
    ruleset_sha256 = _sha256(
        row.get("ruleset_sha256"),
        field="projection.ruleset_sha256",
    )
    created_at_ms = _native_non_negative(
        row.get("created_at_ms"),
        field="projection.created_at_ms",
    )
    if (
        str(row.get("source_adapter_id") or "")
        != projection["source_binding"]["adapter_id"]
        or str(row.get("source_class") or "")
        != projection["source_binding"]["source_class"]
        or str(row.get("source_channel") or "")
        != projection["source_binding"]["source_channel"]
        or ruleset_version != projection["ruleset_version"]
        or ruleset_sha256 != projection["ruleset_sha256"]
        or str(row.get("input_sha256") or "") != input_sha256
        or str(row.get("projection_key_sha256") or "") != projection_key_sha256
        or str(row.get("projection_sha256") or "") != projection_sha256
        or str(row.get("status") or "") != status
        or type(row.get("matched_rule_count")) is not int
        or row.get("matched_rule_count") != matched_rule_count
        or type(row.get("hypothesis_count")) is not int
        or row.get("hypothesis_count") != hypothesis_count
    ):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "trading-impact row mirrors do not match its sealed projection",
        )
    for field in (
        "provider_calls_performed",
        "model_calls_performed",
        "market_calls_performed",
        "network_requests_performed",
        "database_writes_performed",
        "formal_rounds_created",
        "live_trading_allowed",
    ):
        if type(row.get(field)) is not int or row.get(field) != 0:
            raise _error(
                "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
                "trading-impact zero-capability column is invalid",
            )
    if row.get("execution_capability") != "none":
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "trading-impact execution capability is invalid",
        )
    expected_receipt = _build_receipt(
        projection_id=projection_id,
        evaluation=evaluation,
        item_id=item_id,
        origin_import_id=str(stored_item.get("origin_import_id") or ""),
        source_item_sha256=source_item_sha256,
        fingerprint_version=fingerprint_version,
        fingerprint=fingerprint,
        projection=projection,
        created_at_ms=created_at_ms,
    )
    receipt = _load_canonical_object(
        row.get("receipt_json"),
        label="impact receipt",
    )
    if (
        receipt != expected_receipt
        or str(row.get("receipt_sha256") or "")
        != expected_receipt["receipt_sha256"]
    ):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "trading-impact receipt failed its source or content binding",
        )
    return {
        "version": SOURCE_INBOX_TRADING_IMPACT_RECORD_VERSION,
        "id": projection_id,
        "evaluation_import_id": evaluation_import_id,
        "item_id": item_id,
        "source_item_sha256": source_item_sha256,
        "server_fingerprint_version": fingerprint_version,
        "server_fingerprint": fingerprint,
        "ruleset_version": ruleset_version,
        "ruleset_sha256": ruleset_sha256,
        "projection_key_sha256": projection_key_sha256,
        "projection_sha256": projection_sha256,
        "receipt_sha256": expected_receipt["receipt_sha256"],
        "status": status,
        "matched_rule_count": matched_rule_count,
        "hypothesis_count": hypothesis_count,
        "created_at_ms": created_at_ms,
        "projection": copy.deepcopy(projection),
        "receipt": copy.deepcopy(expected_receipt),
        "safety": copy.deepcopy(_RECEIPT_SAFETY),
    }


def build_trading_impact_receipt(
    *,
    projection_id: Any,
    evaluation_import: Any,
    item_id: Any,
    origin_import_id: Any,
    source_item_sha256: Any,
    server_fingerprint_version: Any,
    server_fingerprint: Any,
    projection: Any,
    first_created_at_ms: Any,
) -> dict[str, Any]:
    """Build a defensive receipt from already verified persistence inputs."""

    if type(evaluation_import) is not dict:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_INPUT_INVALID",
            "evaluation_import must be a verified native object",
        )
    normalized_projection = _normalize_projection(projection)
    return _build_receipt(
        projection_id=_identifier(projection_id, field="projection_id"),
        evaluation=copy.deepcopy(evaluation_import),
        item_id=_identifier(item_id, field="item_id"),
        origin_import_id=_identifier(origin_import_id, field="origin_import_id"),
        source_item_sha256=_sha256(
            source_item_sha256,
            field="source_item_sha256",
        ),
        fingerprint_version=_identifier(
            server_fingerprint_version,
            field="server_fingerprint_version",
            maximum=160,
        ),
        fingerprint=_sha256(
            server_fingerprint,
            field="server_fingerprint",
        ),
        projection=normalized_projection,
        created_at_ms=_native_non_negative(
            first_created_at_ms,
            field="first_created_at_ms",
        ),
    )


def insert_or_verify_trading_impact_projection(
    connection: sqlite3.Connection,
    *,
    evaluation_import_id: Any,
    item_id: Any,
    source_item: Any,
    source_item_sha256: Any,
    projection: Any,
    created_at_ms: Any,
) -> dict[str, Any]:
    """Insert one immutable sidecar or verify and reuse the exact first row.

    The caller must place this operation inside the Source Inbox import
    transaction.  Existing rows retain their original evaluation import and
    first-created time even when a later import reuses the same source item.
    """

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    clean_item_id = _identifier(item_id, field="item_id")
    clean_import_id = _identifier(
        evaluation_import_id,
        field="evaluation_import_id",
    )
    timestamp = _native_non_negative(created_at_ms, field="created_at_ms")
    item, fingerprint_version, fingerprint = _normalized_source_item(
        source_item,
        source_item_sha256=source_item_sha256,
    )
    item_sha256 = str(source_item_sha256)
    stored_item = _stored_item(connection, item_id=clean_item_id)
    if (
        stored_item["item"] != item
        or str(stored_item.get("item_sha256") or "") != item_sha256
        or str(stored_item.get("server_fingerprint") or "") != fingerprint
    ):
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_ITEM_CONFLICT",
            "supplied source item differs from the persisted immutable item",
        )
    evaluation = _evaluation_binding(
        connection,
        evaluation_import_id=clean_import_id,
        item_id=clean_item_id,
    )
    normalized_projection = _normalize_projection(projection)
    _verify_projection_bindings(
        normalized_projection,
        source_item=item,
        source_item_sha256=item_sha256,
        fingerprint_version=fingerprint_version,
        fingerprint=fingerprint,
        evaluation=evaluation,
    )
    ruleset_version = _bounded_token(
        normalized_projection["ruleset_version"],
        field="ruleset_version",
    )
    projection_key_sha256 = str(
        normalized_projection["projection_key_sha256"]
    )
    identity_rows = _select_projection_rows(
        connection,
        item_id=clean_item_id,
        ruleset_version=ruleset_version,
    )
    key_rows = _select_projection_rows(
        connection,
        projection_key_sha256=projection_key_sha256,
    )
    if len(identity_rows) > 1 or len(key_rows) > 1:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_RECORD_CORRUPT",
            "trading-impact uniqueness invariant is corrupt",
        )
    existing_rows = {
        str(row.get("id") or ""): row
        for row in (*identity_rows, *key_rows)
    }
    if existing_rows:
        if len(existing_rows) != 1:
            raise _error(
                "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_CONFLICT",
                "ruleset identity and projection key resolve to different rows",
            )
        existing = _verify_record(
            connection,
            next(iter(existing_rows.values())),
            expected_item=item,
            expected_item_sha256=item_sha256,
        )
        if existing["projection"] != normalized_projection:
            raise _error(
                "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_CONFLICT",
                "the same item/ruleset already has different deterministic semantics",
            )
        return {"disposition": "REUSED", "record": existing}

    projection_id = f"source_impact_{uuid.uuid4().hex}"
    status = (
        TRADING_IMPACT_STATUS_MATCHED
        if normalized_projection["evaluation"] == "matched"
        else TRADING_IMPACT_STATUS_NO_MATCH
    )
    matched_rule_count = len(normalized_projection["matched_rule_ids"])
    hypothesis_count = len(normalized_projection["hypotheses"])
    input_sha256 = _canonical_sha256({
        "version": "trading_impact_sidecar_input_v1",
        "source_binding": normalized_projection["source_binding"],
        "source_item_binding": normalized_projection["source_item_binding"],
    })
    receipt = _build_receipt(
        projection_id=projection_id,
        evaluation=evaluation,
        item_id=clean_item_id,
        origin_import_id=str(stored_item.get("origin_import_id") or ""),
        source_item_sha256=item_sha256,
        fingerprint_version=fingerprint_version,
        fingerprint=fingerprint,
        projection=normalized_projection,
        created_at_ms=timestamp,
    )
    try:
        connection.execute(
            """INSERT INTO source_inbox_trading_impact_projections(
                   id,record_version,evaluation_import_id,item_id,
                   source_adapter_id,source_class,source_channel,
                   source_item_sha256,server_fingerprint_version,server_fingerprint,
                   ruleset_version,ruleset_sha256,input_sha256,
                   projection_key_sha256,projection_json,projection_sha256,
                   receipt_json,receipt_sha256,status,matched_rule_count,
                   hypothesis_count,provider_calls_performed,model_calls_performed,
                   market_calls_performed,network_requests_performed,
                   database_writes_performed,formal_rounds_created,
                   live_trading_allowed,execution_capability,created_at_ms
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                projection_id,
                SOURCE_INBOX_TRADING_IMPACT_RECORD_VERSION,
                clean_import_id,
                clean_item_id,
                normalized_projection["source_binding"]["adapter_id"],
                normalized_projection["source_binding"]["source_class"],
                normalized_projection["source_binding"]["source_channel"],
                item_sha256,
                fingerprint_version,
                fingerprint,
                ruleset_version,
                normalized_projection["ruleset_sha256"],
                input_sha256,
                projection_key_sha256,
                _canonical_json(normalized_projection),
                normalized_projection["projection_sha256"],
                _canonical_json(receipt),
                receipt["receipt_sha256"],
                status,
                matched_rule_count,
                hypothesis_count,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
                "none",
                timestamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        # A caller using BEGIN IMMEDIATE should not race.  Still classify a
        # uniqueness collision explicitly instead of weakening first-write wins.
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PROJECTION_CONFLICT",
            "trading-impact sidecar insert violated an immutable constraint",
        ) from exc
    rows = _select_projection_rows(
        connection,
        item_id=clean_item_id,
        ruleset_version=ruleset_version,
    )
    if len(rows) != 1:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_PERSISTENCE_FAILED",
            "trading-impact sidecar was not readable after insertion",
        )
    verified = _verify_record(
        connection,
        rows[0],
        expected_item=item,
        expected_item_sha256=item_sha256,
    )
    return {"disposition": "CREATED", "record": verified}


def list_verified_trading_impact_projections(
    connection: sqlite3.Connection,
    *,
    item_id: Any,
    source_item: Any,
    source_item_sha256: Any,
) -> list[dict[str, Any]]:
    """Read and integrity-check stored sidecars without executing any rules."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    clean_item_id = _identifier(item_id, field="item_id")
    item, _fingerprint_version, _fingerprint = _normalized_source_item(
        source_item,
        source_item_sha256=source_item_sha256,
    )
    item_sha256 = str(source_item_sha256)
    stored_item = _stored_item(connection, item_id=clean_item_id)
    if stored_item["item"] != item or stored_item.get("item_sha256") != item_sha256:
        raise _error(
            "SOURCE_INBOX_TRADING_IMPACT_ITEM_CONFLICT",
            "readback source item differs from the persisted immutable item",
        )
    rows = _select_projection_rows(connection, item_id=clean_item_id)
    return [
        _verify_record(
            connection,
            row,
            expected_item=item,
            expected_item_sha256=item_sha256,
        )
        for row in rows
    ]


__all__ = [
    "MAX_TRADING_IMPACT_PROJECTION_BYTES",
    "MAX_TRADING_IMPACT_RECEIPT_BYTES",
    "SOURCE_INBOX_TRADING_IMPACT_MIGRATION_KEY",
    "SOURCE_INBOX_TRADING_IMPACT_RECEIPT_VERSION",
    "SOURCE_INBOX_TRADING_IMPACT_RECORD_VERSION",
    "SourceInboxTradingImpactError",
    "build_trading_impact_receipt",
    "ensure_source_inbox_trading_impact_schema",
    "insert_or_verify_trading_impact_projection",
    "list_verified_trading_impact_projections",
]
