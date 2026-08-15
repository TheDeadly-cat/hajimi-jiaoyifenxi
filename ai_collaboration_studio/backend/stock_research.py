from __future__ import annotations

import copy
import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from .decision_lineage import canonical_json, canonical_sha256


STOCK_RESEARCH_CAPABILITY_PACK_ID = "stock_research_readonly"
STOCK_RESEARCH_CONTRACT_VERSION = "stock_research_contract_v1"
STOCK_RESEARCH_SCHEMA_VERSION = "stock_research_contract_schema_v1"
STOCK_ROOM_SCOPE_VERSION = "stock_room_scope_v1"
MAX_STOCK_ROOM_SYMBOLS = 64

STOCK_EVIDENCE_CLASSES = frozenset({
    "official_fact",
    "media_report",
    "model_inference",
    "market_proxy",
})
STOCK_PREFLIGHT_SOURCE_TYPES = (
    "futu",
    "sec",
    "investor_relations",
    "price_adjustment",
    "corporate_actions",
)
STOCK_PREFLIGHT_STATES = frozenset({"ready", "unavailable"})

FIXED_STOCK_RESEARCH_BOUNDARIES: dict[str, Any] = {
    "execution_capability": "none",
    "live_trading_allowed": False,
    "order_placement_allowed": False,
    "wallet_connection_allowed": False,
    "automatic_trading_allowed": False,
    "can_autonomously_decide": False,
    "can_replace_user_decision": False,
    "user_final_decision_required": True,
}

_ROOT_WITHOUT_HASH = {
    "version",
    "capability_pack_id",
    "stock_room_scope",
    "data_cutoff_utc",
    "symbols",
    "research_ready",
    *FIXED_STOCK_RESEARCH_BOUNDARIES,
}
_ROOT_FIELDS = {*_ROOT_WITHOUT_HASH, "contract_sha256"}
_SCOPE_FIELDS = {"version", "symbols"}
_SYMBOL_FIELDS = {
    "symbol",
    "issuer_name",
    "exchange",
    "currency",
    "preflight",
    "evidence",
}
_PREFLIGHT_FIELDS = set(STOCK_PREFLIGHT_SOURCE_TYPES)
_PREFLIGHT_ENTRY_FIELDS = {
    "version",
    "source_type",
    "status",
    "as_of_utc",
    "reason",
    "source",
}
_EVIDENCE_FIELDS = {
    "claim_id",
    "symbol",
    "claim",
    "evidence_class",
    "as_of_utc",
    "source",
    "inference",
}
_SOURCE_FIELDS = {
    "source_id",
    "publisher",
    "source_uri",
    "source_sha256",
    "material_binding",
    "published_at_utc",
    "retrieved_at_utc",
}
_MATERIAL_FIELDS = {
    "material_id",
    "material_version",
    "content_sha256",
    "snapshot_sha256",
}
_INFERENCE_FIELDS = {
    "method_id",
    "method_version",
    "generated_at_utc",
    "upstream_claim_ids",
}

_INSTRUMENT_PATTERN = re.compile(
    r"[A-Z][A-Z0-9]{1,7}:[A-Z0-9][A-Z0-9.-]{0,31}"
)
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MATERIAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_UTC_PATTERN = re.compile(
    r"(?:19|20|21)[0-9]{2}-(?:0[1-9]|1[0-2])-"
    r"(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z"
)


FIXED_STOCK_RESEARCH_BOUNDARY_FIELDS = tuple(
    FIXED_STOCK_RESEARCH_BOUNDARIES.keys()
)

STOCK_RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "version": STOCK_RESEARCH_SCHEMA_VERSION,
    "type": "object",
    "required": sorted(_ROOT_FIELDS),
    "fields": {
        "version": STOCK_RESEARCH_CONTRACT_VERSION,
        "capability_pack_id": STOCK_RESEARCH_CAPABILITY_PACK_ID,
        "stock_room_scope": STOCK_ROOM_SCOPE_VERSION,
        "data_cutoff_utc": "canonical_utc_timestamp",
        "symbols": "array<stock_symbol_research_v1>",
        "research_ready": "boolean",
        "execution_capability": "none",
        "live_trading_allowed": "boolean:false",
        "order_placement_allowed": "boolean:false",
        "wallet_connection_allowed": "boolean:false",
        "automatic_trading_allowed": "boolean:false",
        "can_autonomously_decide": "boolean:false",
        "can_replace_user_decision": "boolean:false",
        "user_final_decision_required": "boolean:true",
        "contract_sha256": "sha256",
    },
    "additional_properties": False,
}
STOCK_RESEARCH_OUTPUT_SCHEMA_SHA256 = canonical_sha256(STOCK_RESEARCH_OUTPUT_SCHEMA)


def _closed_definition(required: set[str], **extra: Any) -> dict[str, Any]:
    return {
        "required": sorted(required),
        "additional_properties": False,
        **extra,
    }


STOCK_RESEARCH_CONTRACT_SCHEMA: dict[str, Any] = {
    "version": STOCK_RESEARCH_SCHEMA_VERSION,
    "root": _closed_definition(_ROOT_FIELDS),
    "stock_room_scope": _closed_definition(_SCOPE_FIELDS),
    "symbol": _closed_definition(_SYMBOL_FIELDS),
    "preflight": _closed_definition(_PREFLIGHT_FIELDS),
    "preflight_entry": _closed_definition(_PREFLIGHT_ENTRY_FIELDS),
    "evidence": _closed_definition(_EVIDENCE_FIELDS),
    "source": _closed_definition(_SOURCE_FIELDS),
    "material_binding": _closed_definition(_MATERIAL_FIELDS),
    "inference": _closed_definition(_INFERENCE_FIELDS),
    "evidence_classes": sorted(STOCK_EVIDENCE_CLASSES),
    "preflight_source_types": list(STOCK_PREFLIGHT_SOURCE_TYPES),
    "preflight_states": sorted(STOCK_PREFLIGHT_STATES),
    "fixed_boundaries": copy.deepcopy(FIXED_STOCK_RESEARCH_BOUNDARIES),
}
STOCK_RESEARCH_SCHEMA_SHA256 = canonical_sha256(STOCK_RESEARCH_CONTRACT_SCHEMA)


class StockResearchContractError(ValueError):
    """Raised when a stock read-only seal or room scope fails closed."""


def normalize_stock_symbols(value: Any, *, path: str = "symbols") -> list[str]:
    """Return the canonical, sorted MARKET:TICKER instrument-id set."""

    if not isinstance(value, list):
        raise StockResearchContractError(f"{path} must be an array")
    if len(value) > MAX_STOCK_ROOM_SYMBOLS:
        raise StockResearchContractError(
            f"{path} must contain at most {MAX_STOCK_ROOM_SYMBOLS} symbols"
        )
    normalized: list[str] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, str):
            raise StockResearchContractError(f"{path}[{index}] must be a string")
        instrument_id = raw.strip().upper()
        if not _INSTRUMENT_PATTERN.fullmatch(instrument_id):
            raise StockResearchContractError(
                f"{path}[{index}] must be a canonical MARKET:TICKER instrument_id"
            )
        normalized.append(instrument_id)
    if len(set(normalized)) != len(normalized):
        raise StockResearchContractError(f"{path} must contain unique symbols")
    return sorted(normalized)


def normalize_stock_room_scope(
    value: Any,
    *,
    require_nonempty: bool = True,
) -> dict[str, Any]:
    """Normalize one closed, versioned room stock-pool envelope."""

    if not isinstance(value, dict):
        raise StockResearchContractError("stock_room_scope must be an object")
    _require_exact_keys(value, _SCOPE_FIELDS, "stock_room_scope")
    if value.get("version") != STOCK_ROOM_SCOPE_VERSION:
        raise StockResearchContractError(
            f"stock_room_scope.version must be {STOCK_ROOM_SCOPE_VERSION}"
        )
    symbols = normalize_stock_symbols(
        value.get("symbols"),
        path="stock_room_scope.symbols",
    )
    if require_nonempty and not symbols:
        raise StockResearchContractError("stock_room_scope.symbols must not be empty")
    return {"version": STOCK_ROOM_SCOPE_VERSION, "symbols": symbols}


def validate_stock_room_scope(room: Any) -> dict[str, Any]:
    """Validate the persisted room scope when the stock pack is selected."""

    if not isinstance(room, dict):
        raise StockResearchContractError("room must be an object")
    pack_ids = room.get("capability_pack_ids")
    if not isinstance(pack_ids, list) or any(not isinstance(item, str) for item in pack_ids):
        raise StockResearchContractError("room.capability_pack_ids must be an array of strings")
    selected = STOCK_RESEARCH_CAPABILITY_PACK_ID in pack_ids
    raw_scope = room.get("stock_room_scope")
    if not selected and raw_scope in (None, {}):
        return {"version": STOCK_ROOM_SCOPE_VERSION, "symbols": []}
    normalized = normalize_stock_room_scope(
        raw_scope,
        require_nonempty=selected,
    )
    if raw_scope != normalized:
        raise StockResearchContractError("room.stock_room_scope is not canonical")
    return normalized


def build_stock_research_contract(payload: Any) -> dict[str, Any]:
    """Validate and hash one offline, material-bound stock research seal."""

    if not isinstance(payload, dict):
        raise StockResearchContractError("stock research payload must be an object")
    contract = copy.deepcopy(payload)
    if "contract_sha256" in contract:
        raise StockResearchContractError(
            "contract_sha256 is host generated and must not be supplied"
        )
    _install_fixed(contract, "version", STOCK_RESEARCH_CONTRACT_VERSION)
    _install_fixed(
        contract,
        "capability_pack_id",
        STOCK_RESEARCH_CAPABILITY_PACK_ID,
    )
    for field, expected in FIXED_STOCK_RESEARCH_BOUNDARIES.items():
        _install_fixed(contract, field, expected)
    _validate_contract_body(contract)
    contract["contract_sha256"] = canonical_sha256(contract)
    return contract


def validate_stock_research_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StockResearchContractError("stock research contract must be an object")
    contract = copy.deepcopy(value)
    _require_exact_keys(contract, _ROOT_FIELDS, "contract")
    stored_hash = contract.pop("contract_sha256")
    _require_sha256(stored_hash, "contract.contract_sha256")
    _validate_contract_body(contract)
    if canonical_sha256(contract) != stored_hash:
        raise StockResearchContractError("stock research contract sha256 mismatch")
    contract["contract_sha256"] = stored_hash
    return contract


def verify_stock_research_contract(value: Any) -> dict[str, Any]:
    return validate_stock_research_contract(value)


def _install_fixed(target: dict[str, Any], field: str, expected: Any) -> None:
    if field in target and (
        type(target[field]) is not type(expected) or target[field] != expected
    ):
        raise StockResearchContractError(f"contract.{field} is fixed")
    target[field] = copy.deepcopy(expected)


def _validate_contract_body(contract: dict[str, Any]) -> None:
    _reject_non_json(contract, "contract")
    _require_exact_keys(contract, _ROOT_WITHOUT_HASH, "contract")
    if contract.get("version") != STOCK_RESEARCH_CONTRACT_VERSION:
        raise StockResearchContractError("contract.version is fixed")
    if contract.get("capability_pack_id") != STOCK_RESEARCH_CAPABILITY_PACK_ID:
        raise StockResearchContractError("contract.capability_pack_id is fixed")
    for field, expected in FIXED_STOCK_RESEARCH_BOUNDARIES.items():
        if type(contract.get(field)) is not type(expected) or contract.get(field) != expected:
            raise StockResearchContractError(f"contract.{field} is fixed")

    cutoff = _require_utc(contract.get("data_cutoff_utc"), "contract.data_cutoff_utc")
    scope = normalize_stock_room_scope(contract.get("stock_room_scope"))
    if contract.get("stock_room_scope") != scope:
        raise StockResearchContractError("contract.stock_room_scope is not canonical")

    rows = contract.get("symbols")
    if not isinstance(rows, list) or not rows:
        raise StockResearchContractError("contract.symbols must be a non-empty array")
    validated: list[dict[str, Any]] = []
    all_claims: dict[str, dict[str, Any]] = {}
    all_ready = True
    for index, value in enumerate(rows):
        row, row_ready, claims = _validate_symbol(
            value,
            f"contract.symbols[{index}]",
            cutoff,
        )
        validated.append(row)
        all_ready = all_ready and row_ready
        for claim_id, claim in claims.items():
            if claim_id in all_claims:
                raise StockResearchContractError("evidence claim_id must be globally unique")
            all_claims[claim_id] = claim

    symbols = [row["symbol"] for row in validated]
    if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
        raise StockResearchContractError("contract.symbols must be unique and sorted by symbol")
    if symbols != scope["symbols"]:
        raise StockResearchContractError(
            "contract.symbols must exactly match stock_room_scope.symbols"
        )
    if type(contract.get("research_ready")) is not bool:
        raise StockResearchContractError("contract.research_ready must be boolean")
    if contract["research_ready"] is not all_ready:
        raise StockResearchContractError(
            "contract.research_ready must equal all five preflight states"
        )
    _validate_inference_graph(all_claims)


def _validate_symbol(
    value: Any,
    path: str,
    cutoff: datetime,
) -> tuple[dict[str, Any], bool, dict[str, dict[str, Any]]]:
    row = _require_object(value, path)
    _require_exact_keys(row, _SYMBOL_FIELDS, path)
    symbol = _require_instrument_id(row.get("symbol"), f"{path}.symbol")
    exchange, _ticker = symbol.split(":", 1)
    exchange_value = _require_text(
        row.get("exchange"),
        f"{path}.exchange",
        maximum=16,
    )
    if exchange_value != exchange:
        raise StockResearchContractError(f"{path}.exchange must match symbol market prefix")
    _require_text(row.get("issuer_name"), f"{path}.issuer_name", maximum=180)
    currency = _require_text(row.get("currency"), f"{path}.currency", maximum=8)
    if currency != currency.upper() or not re.fullmatch(r"[A-Z]{3,8}", currency):
        raise StockResearchContractError(f"{path}.currency must be uppercase")

    preflight = _require_object(row.get("preflight"), f"{path}.preflight")
    _require_exact_keys(preflight, _PREFLIGHT_FIELDS, f"{path}.preflight")
    ready = True
    for source_type in STOCK_PREFLIGHT_SOURCE_TYPES:
        state = _validate_preflight_entry(
            preflight.get(source_type),
            f"{path}.preflight.{source_type}",
            source_type,
            cutoff,
        )
        ready = ready and state == "ready"

    evidence = row.get("evidence")
    if not isinstance(evidence, list):
        raise StockResearchContractError(f"{path}.evidence must be an array")
    claims: dict[str, dict[str, Any]] = {}
    for index, raw_claim in enumerate(evidence):
        claim = _validate_evidence(
            raw_claim,
            f"{path}.evidence[{index}]",
            symbol,
            cutoff,
        )
        claim_id = claim["claim_id"]
        if claim_id in claims:
            raise StockResearchContractError(f"{path}.evidence claim_id must be unique")
        claims[claim_id] = claim
    return row, ready, claims


def _validate_preflight_entry(
    value: Any,
    path: str,
    expected_type: str,
    cutoff: datetime,
) -> str:
    entry = _require_object(value, path)
    _require_exact_keys(entry, _PREFLIGHT_ENTRY_FIELDS, path)
    if entry.get("version") != "stock_source_preflight_v1":
        raise StockResearchContractError(f"{path}.version is fixed")
    if entry.get("source_type") != expected_type:
        raise StockResearchContractError(f"{path}.source_type must be {expected_type}")
    status = entry.get("status")
    if status not in STOCK_PREFLIGHT_STATES:
        raise StockResearchContractError(f"{path}.status must be ready or unavailable")
    _require_at_or_before(entry.get("as_of_utc"), f"{path}.as_of_utc", cutoff)
    reason = entry.get("reason")
    if not isinstance(reason, str) or len(reason.strip()) > 500:
        raise StockResearchContractError(f"{path}.reason must be a string")
    source = entry.get("source")
    if status == "ready":
        if reason.strip():
            raise StockResearchContractError(f"{path}.reason must be empty when ready")
        _validate_source(source, f"{path}.source", cutoff)
    else:
        if not reason.strip():
            raise StockResearchContractError(f"{path}.reason is required when unavailable")
        if source is not None:
            _validate_source(source, f"{path}.source", cutoff)
    return str(status)


def _validate_evidence(
    value: Any,
    path: str,
    symbol: str,
    cutoff: datetime,
) -> dict[str, Any]:
    claim = _require_object(value, path)
    _require_exact_keys(claim, _EVIDENCE_FIELDS, path)
    _require_identifier(claim.get("claim_id"), f"{path}.claim_id")
    if claim.get("symbol") != symbol:
        raise StockResearchContractError(f"{path}.symbol must match its stock row")
    _require_text(claim.get("claim"), f"{path}.claim", maximum=4000)
    evidence_class = claim.get("evidence_class")
    if evidence_class not in STOCK_EVIDENCE_CLASSES:
        raise StockResearchContractError(
            f"{path}.evidence_class must be one of {sorted(STOCK_EVIDENCE_CLASSES)}"
        )
    _require_at_or_before(claim.get("as_of_utc"), f"{path}.as_of_utc", cutoff)
    _validate_source(claim.get("source"), f"{path}.source", cutoff)
    inference = claim.get("inference")
    if evidence_class == "model_inference":
        detail = _require_object(inference, f"{path}.inference")
        _require_exact_keys(detail, _INFERENCE_FIELDS, f"{path}.inference")
        _require_identifier(detail.get("method_id"), f"{path}.inference.method_id")
        _require_text(detail.get("method_version"), f"{path}.inference.method_version", maximum=80)
        _require_at_or_before(
            detail.get("generated_at_utc"),
            f"{path}.inference.generated_at_utc",
            cutoff,
        )
        upstream = detail.get("upstream_claim_ids")
        if not isinstance(upstream, list) or not upstream:
            raise StockResearchContractError(
                f"{path}.inference.upstream_claim_ids must be non-empty"
            )
        for index, claim_id in enumerate(upstream):
            _require_identifier(
                claim_id,
                f"{path}.inference.upstream_claim_ids[{index}]",
            )
        if len(set(upstream)) != len(upstream):
            raise StockResearchContractError(
                f"{path}.inference.upstream_claim_ids must be unique"
            )
    elif inference is not None:
        raise StockResearchContractError(
            f"{path}.inference is only allowed for model_inference"
        )
    return claim


def _validate_source(value: Any, path: str, cutoff: datetime) -> dict[str, Any]:
    source = _require_object(value, path)
    _require_exact_keys(source, _SOURCE_FIELDS, path)
    _require_identifier(source.get("source_id"), f"{path}.source_id")
    _require_text(source.get("publisher"), f"{path}.publisher", maximum=180)
    uri = _require_text(source.get("source_uri"), f"{path}.source_uri", maximum=2048)
    parsed = urlsplit(uri)
    if not (
        (parsed.scheme == "https" and bool(parsed.netloc))
        or (parsed.scheme == "urn" and bool(parsed.path))
    ):
        raise StockResearchContractError(f"{path}.source_uri must use HTTPS or URN")
    source_sha256 = _require_sha256(
        source.get("source_sha256"),
        f"{path}.source_sha256",
    )
    material = _require_object(source.get("material_binding"), f"{path}.material_binding")
    _require_exact_keys(material, _MATERIAL_FIELDS, f"{path}.material_binding")
    material_id = material.get("material_id")
    if not isinstance(material_id, str) or not _MATERIAL_ID_PATTERN.fullmatch(material_id):
        raise StockResearchContractError(f"{path}.material_binding.material_id is invalid")
    material_version = _require_integer(
        material.get("material_version"),
        f"{path}.material_binding.material_version",
        minimum=1,
        maximum=2_147_483_647,
    )
    content_sha256 = _require_sha256(
        material.get("content_sha256"),
        f"{path}.material_binding.content_sha256",
    )
    _require_sha256(
        material.get("snapshot_sha256"),
        f"{path}.material_binding.snapshot_sha256",
    )
    if source_sha256 != content_sha256:
        raise StockResearchContractError(
            f"{path}.source_sha256 must equal material_binding.content_sha256"
        )
    if parsed.scheme == "urn":
        expected_urn = (
            f"urn:ai-studio:material:{material_id}:v{material_version}"
        )
        if uri != expected_urn:
            raise StockResearchContractError(
                f"{path}.source_uri must exactly bind the material version"
            )
    published = source.get("published_at_utc")
    if published is not None:
        _require_at_or_before(published, f"{path}.published_at_utc", cutoff)
    _require_at_or_before(source.get("retrieved_at_utc"), f"{path}.retrieved_at_utc", cutoff)
    return source


def _validate_inference_graph(claims: dict[str, dict[str, Any]]) -> None:
    graph: dict[str, list[str]] = {}
    for claim_id, claim in claims.items():
        detail = claim.get("inference")
        if not isinstance(detail, dict):
            continue
        upstream = list(detail.get("upstream_claim_ids") or [])
        if claim_id in upstream:
            raise StockResearchContractError("model inference cannot reference itself")
        for upstream_id in upstream:
            if upstream_id not in claims:
                raise StockResearchContractError(
                    f"model inference {claim_id} references missing upstream claim {upstream_id}"
                )
        graph[claim_id] = upstream

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(claim_id: str) -> None:
        if claim_id in visiting:
            raise StockResearchContractError("model inference graph contains a cycle")
        if claim_id in visited:
            return
        visiting.add(claim_id)
        for upstream_id in graph.get(claim_id, []):
            visit(upstream_id)
        visiting.remove(claim_id)
        visited.add(claim_id)

    for claim_id in graph:
        visit(claim_id)


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StockResearchContractError(f"{path} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise StockResearchContractError(
            f"{path} must be closed; missing={missing}, unexpected={unexpected}"
        )


def _require_identifier(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise StockResearchContractError(f"{path} must be an identifier")
    return value


def _require_instrument_id(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _INSTRUMENT_PATTERN.fullmatch(value):
        raise StockResearchContractError(f"{path} must be a canonical MARKET:TICKER instrument_id")
    return value


def _require_sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise StockResearchContractError(f"{path} must be lowercase sha256")
    return value


def _require_text(value: Any, path: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise StockResearchContractError(f"{path} must be a string")
    clean = value.strip()
    if not clean or len(clean) > maximum or clean != value:
        raise StockResearchContractError(f"{path} must be non-empty canonical text")
    return clean


def _require_integer(
    value: Any,
    path: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise StockResearchContractError(f"{path} must be an integer")
    if value < minimum or value > maximum:
        raise StockResearchContractError(
            f"{path} must be between {minimum} and {maximum}"
        )
    return value


def _require_utc(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not _UTC_PATTERN.fullmatch(value):
        raise StockResearchContractError(f"{path} must be canonical UTC with second precision")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise StockResearchContractError(f"{path} must be a real UTC timestamp") from exc
    return parsed


def _require_at_or_before(value: Any, path: str, cutoff: datetime) -> datetime:
    parsed = _require_utc(value, path)
    if parsed > cutoff:
        raise StockResearchContractError(f"{path} must not be after data cutoff")
    return parsed


def _reject_non_json(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise StockResearchContractError(f"{path} keys must be strings")
            _reject_non_json(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_non_json(nested, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise StockResearchContractError(f"{path} must contain finite JSON numbers")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise StockResearchContractError(f"{path} must contain JSON-compatible data")


__all__ = [
    "FIXED_STOCK_RESEARCH_BOUNDARIES",
    "FIXED_STOCK_RESEARCH_BOUNDARY_FIELDS",
    "MAX_STOCK_ROOM_SYMBOLS",
    "STOCK_EVIDENCE_CLASSES",
    "STOCK_PREFLIGHT_SOURCE_TYPES",
    "STOCK_PREFLIGHT_STATES",
    "STOCK_RESEARCH_CAPABILITY_PACK_ID",
    "STOCK_RESEARCH_CONTRACT_SCHEMA",
    "STOCK_RESEARCH_CONTRACT_VERSION",
    "STOCK_RESEARCH_OUTPUT_SCHEMA",
    "STOCK_RESEARCH_OUTPUT_SCHEMA_SHA256",
    "STOCK_RESEARCH_SCHEMA_SHA256",
    "STOCK_RESEARCH_SCHEMA_VERSION",
    "STOCK_ROOM_SCOPE_VERSION",
    "StockResearchContractError",
    "build_stock_research_contract",
    "canonical_json",
    "canonical_sha256",
    "normalize_stock_room_scope",
    "normalize_stock_symbols",
    "validate_stock_research_contract",
    "validate_stock_room_scope",
    "verify_stock_research_contract",
]
