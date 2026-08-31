"""Pure, deterministic Phase 5 trading-impact sidecar projection.

The engine consumes an already-normalized ``project_source_item_v1`` and
returns an immutable sidecar value.  It never mutates the parent item and has
no clock, database, network, Provider, market, model, or random dependency.

The word "impact" in this module means a bounded research-review hypothesis.
It never means a directional forecast, causal attribution, profitability
claim, execution permission, or trading instruction.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..source_inbox_contracts import (
    EXTERNAL_UNVERIFIED,
    MAX_SOURCE_IMPORT_BYTES,
    PROJECT_SOURCE_ITEM_VERSION,
    SOURCE_ITEM_FINGERPRINT_VERSION,
    canonical_sha256,
    project_source_item_fingerprint,
)


TRADING_IMPACT_RULESET_VERSION = "trading_impact_rules_v1"
TRADING_IMPACT_MAPPING_VERSION = "trading_impact_mapping_v1"
TRADING_IMPACT_PROJECTION_KEY_VERSION = "trading_impact_projection_key_v1"
TRADING_IMPACT_PROJECTION_VERSION = "trading_impact_projection_v1"
TRADING_IMPACT_HYPOTHESIS_VERSION = "trading_impact_hypothesis_v1"
TRADING_IMPACT_SOURCE_SEMANTICS_VERSION = "trading_impact_source_semantics_v1"

MAX_TRADING_IMPACT_MANIFEST_BYTES = 32 * 1024
MAX_TRADING_IMPACT_PROJECTION_BYTES = 16 * 1024
MAX_TRADING_IMPACT_JSON_DEPTH = 12
MAX_TRADING_IMPACT_HYPOTHESES = 3
MAX_TRADING_IMPACT_MATCHED_RULES = 1
MAX_TRADING_IMPACT_STATEMENT_CHARS = 500

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_SLUG_RE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
_RFC3339_UTC_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z"
)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")

_DIRECT_SECURITY_IDS = (
    "US.MU",
    "US.SNDK",
    "US.WDC",
    "US.STX",
    "US.NVDA",
    "US.MRVL",
    "US.AMD",
)
_STORAGE_SECURITY_IDS = _DIRECT_SECURITY_IDS[:4]
_SECTOR_SECURITY_MAP = (
    ("dram", ("US.MU",)),
    ("nand", ("US.MU", "US.SNDK")),
    ("hdd", ("US.WDC", "US.STX")),
)

_SOURCE_BINDINGS: dict[str, dict[str, str]] = {
    "sec_filings": {
        "source_class": "official_source",
        "source_channel": "official_source_monitor",
        "item_type": "sec_filing",
        "extension": "sec_v1",
    },
    "company_ir": {
        "source_class": "official_source",
        "source_channel": "official_source_monitor",
        "item_type": "company_ir_release",
        "extension": "company_ir_v1",
    },
    "federal_reserve": {
        "source_class": "official_source",
        "source_channel": "official_source_monitor",
        "item_type": "official_macro_release",
        "extension": "macro_official_v1",
    },
    "bls_releases": {
        "source_class": "official_source",
        "source_channel": "official_source_monitor",
        "item_type": "official_macro_release",
        "extension": "macro_official_v1",
    },
    "treasury_releases": {
        "source_class": "official_source",
        "source_channel": "official_source_monitor",
        "item_type": "official_macro_release",
        "extension": "macro_official_v1",
    },
    "official_macro_calendar": {
        "source_class": "official_source",
        "source_channel": "official_source_monitor",
        "item_type": "official_macro_schedule",
        "extension": "macro_official_v1",
    },
    "futu_anomaly_signals": {
        "source_class": "readonly_market",
        "source_channel": "futu_anomaly_monitor",
        "item_type": "market_anomaly_signal",
        "extension": "futu_anomaly_v1",
    },
}

_SEC_PERIODIC_FORMS = ("10-K", "10-Q", "20-F", "40-F")
_SEC_CURRENT_FORMS = ("8-K", "6-K")
_IR_EVENT_TYPES = (
    "earnings_schedule",
    "earnings_release",
    "earnings_material",
    "other",
)
_MACRO_AUTHORITY_FAMILIES = (
    ("federal_reserve", ("monetary_policy", "fomc_meeting")),
    ("bls", ("consumer_price_index", "employment_situation")),
    ("treasury", ("debt_to_penny",)),
)
_MACRO_OCCURRENCE_PRECISION = {
    "official_schedule_time": "timestamp",
    "official_date_anchor_not_exact_time": "date_anchor",
    "official_release_time": "timestamp",
    "official_reference_period_anchor_not_release_time": "reference_period_anchor",
}

_FUTU_RULE_SPECS = {
    "price_up_5pct": {
        "metric": "change_rate",
        "entry_threshold": "5",
        "exit_threshold": "4",
        "signal_direction": "up_observation",
        "unit": "percent",
    },
    "price_down_5pct": {
        "metric": "change_rate",
        "entry_threshold": "-5",
        "exit_threshold": "-4",
        "signal_direction": "down_observation",
        "unit": "percent",
    },
    "amplitude_8pct": {
        "metric": "amplitude",
        "entry_threshold": "8",
        "exit_threshold": "6",
        "signal_direction": "none",
        "unit": "percent",
    },
    "volume_ratio_3x": {
        "metric": "volume_ratio",
        "entry_threshold": "3",
        "exit_threshold": "2.5",
        "signal_direction": "none",
        "unit": "ratio",
    },
}

_RULE_ORDER = (
    "sec_periodic_filing_review_v1",
    "sec_current_filing_review_v1",
    "ir_revision_review_v1",
    "ir_earnings_schedule_review_v1",
    "ir_earnings_disclosure_review_v1",
    "macro_schedule_revision_review_v1",
    "macro_schedule_review_v1",
    "macro_release_revision_review_v1",
    "macro_release_review_v1",
    "futu_price_up_condition_review_v1",
    "futu_price_down_condition_review_v1",
    "futu_range_condition_review_v1",
    "futu_market_activity_condition_review_v1",
)

_RULE_SPECS: dict[str, dict[str, Any]] = {
    "sec_periodic_filing_review_v1": {
        "adapter_ids": ["sec_filings"],
        "predicate": {"form_in": list(_SEC_PERIODIC_FORMS)},
        "area_kind": "security",
        "mechanism": "issuer_periodic_disclosure_review",
        "horizon": "reporting_cycle",
        "source_type": "regulatory_filing",
        "statement_template": (
            "The admitted SEC {form} metadata for {symbol} may require review of "
            "existing issuer assumptions; the form alone does not imply market "
            "direction or magnitude."
        ),
    },
    "sec_current_filing_review_v1": {
        "adapter_ids": ["sec_filings"],
        "predicate": {"form_in": list(_SEC_CURRENT_FORMS)},
        "area_kind": "security",
        "mechanism": "issuer_current_disclosure_review",
        "horizon": "reporting_window",
        "source_type": "regulatory_filing",
        "statement_template": (
            "The admitted SEC {form} metadata for {symbol} may require review of "
            "current issuer-event assumptions; the form alone does not imply "
            "market direction or magnitude."
        ),
    },
    "ir_revision_review_v1": {
        "adapter_ids": ["company_ir"],
        "predicate": {"is_revision": True},
        "area_kind": "security",
        "mechanism": "issuer_revision_recheck",
        "horizon": "revision_review",
        "source_type": "company_ir_rss_projection",
        "statement_template": (
            "The admitted company IR RSS projection marks a revision for {symbol}; "
            "prior issuer assumptions may require recheck, but the revision does "
            "not establish market direction or magnitude."
        ),
    },
    "ir_earnings_schedule_review_v1": {
        "adapter_ids": ["company_ir"],
        "predicate": {"is_revision": False, "event_type": "earnings_schedule"},
        "area_kind": "security",
        "mechanism": "issuer_scheduled_disclosure_review",
        "horizon": "event_date_unknown",
        "source_type": "company_ir_rss_projection",
        "statement_template": (
            "The admitted company IR RSS projection classifies an earnings schedule "
            "for {symbol}; it may define a future review window, but the event date "
            "is unavailable here and no outcome or market direction is inferred."
        ),
    },
    "ir_earnings_disclosure_review_v1": {
        "adapter_ids": ["company_ir"],
        "predicate": {
            "is_revision": False,
            "event_type_in": ["earnings_release", "earnings_material"],
        },
        "area_kind": "security",
        "mechanism": "issuer_disclosure_review",
        "horizon": "reporting_cycle",
        "source_type": "company_ir_rss_projection",
        "statement_template": (
            "The admitted company IR RSS projection classifies {event_type} for "
            "{symbol}; issuer assumptions may require review, but company "
            "self-reporting alone does not establish market direction or magnitude."
        ),
    },
    "macro_schedule_revision_review_v1": {
        "adapter_ids": ["official_macro_calendar"],
        "predicate": {"subject_phase": "schedule", "event_state": "revised"},
        "area_kind": "sector",
        "mechanism": "scheduled_macro_revision_recheck",
        "horizon": "revision_review",
        "source_type": "official_macro_calendar_projection",
        "statement_template": (
            "The admitted {authority} {family} schedule revision may require "
            "rechecking the timing of {sector} research assumptions; it does not "
            "claim that a release or market effect occurred."
        ),
    },
    "macro_schedule_review_v1": {
        "adapter_ids": ["official_macro_calendar"],
        "predicate": {"subject_phase": "schedule", "event_state": "scheduled"},
        "area_kind": "sector",
        "mechanism": "scheduled_macro_event_review",
        "horizon": "scheduled_event",
        "source_type": "official_macro_calendar_projection",
        "statement_template": (
            "The admitted {authority} {family} schedule may define a review window "
            "for {sector} research assumptions; it does not claim that a release or "
            "market effect occurred."
        ),
    },
    "macro_release_revision_review_v1": {
        "adapter_ids": ["federal_reserve", "bls_releases", "treasury_releases"],
        "predicate": {"subject_phase": "release", "event_state": "revised"},
        "area_kind": "sector",
        "mechanism": "prior_macro_assumption_recheck",
        "horizon": "revision_review",
        "source_type": "official_macro_release_projection",
        "statement_template": (
            "The admitted {authority} {family} revision may require rechecking prior "
            "macro assumptions used for {sector} research; no sector or security "
            "direction is inferred."
        ),
    },
    "macro_release_review_v1": {
        "adapter_ids": ["federal_reserve", "bls_releases", "treasury_releases"],
        "predicate": {"subject_phase": "release", "event_state": "released"},
        "area_kind": "sector",
        "mechanism": "shared_macro_assumption_review",
        "horizon": "macro_release_window",
        "source_type": "official_macro_release_projection",
        "statement_template": (
            "The admitted {authority} {family} release may require review of shared "
            "macro assumptions used for {sector} research; no sector or security "
            "direction is inferred."
        ),
    },
    "futu_price_up_condition_review_v1": {
        "adapter_ids": ["futu_anomaly_signals"],
        "predicate": {"futu_rule_id": "price_up_5pct"},
        "area_kind": "security",
        "mechanism": "observed_price_condition_review",
        "horizon": "us_eastern_session_date",
        "source_type": "readonly_market_signal",
        "statement_template": (
            "The sealed Futu rule records an observed same-session price increase "
            "condition for {symbol}; current market-state assumptions may require "
            "review, but continuation or reversal is not forecast."
        ),
    },
    "futu_price_down_condition_review_v1": {
        "adapter_ids": ["futu_anomaly_signals"],
        "predicate": {"futu_rule_id": "price_down_5pct"},
        "area_kind": "security",
        "mechanism": "observed_price_condition_review",
        "horizon": "us_eastern_session_date",
        "source_type": "readonly_market_signal",
        "statement_template": (
            "The sealed Futu rule records an observed same-session price decrease "
            "condition for {symbol}; current market-state assumptions may require "
            "review, but continuation or reversal is not forecast."
        ),
    },
    "futu_range_condition_review_v1": {
        "adapter_ids": ["futu_anomaly_signals"],
        "predicate": {"futu_rule_id": "amplitude_8pct"},
        "area_kind": "security",
        "mechanism": "observed_range_condition_review",
        "horizon": "us_eastern_session_date",
        "source_type": "readonly_market_signal",
        "statement_template": (
            "The sealed Futu rule records an observed same-session intraday range "
            "condition for {symbol}; current market-state assumptions may require "
            "review, but no future price move is forecast."
        ),
    },
    "futu_market_activity_condition_review_v1": {
        "adapter_ids": ["futu_anomaly_signals"],
        "predicate": {"futu_rule_id": "volume_ratio_3x"},
        "area_kind": "security",
        "mechanism": "observed_market_activity_review",
        "horizon": "us_eastern_session_date",
        "source_type": "readonly_market_signal",
        "statement_template": (
            "The sealed Futu rule records an observed same-session market activity "
            "condition for {symbol}; current market-state assumptions may require "
            "review, but no cause or future price move is inferred."
        ),
    },
}

_CONFIDENCE_BASIS = {
    "semantics": "deterministic_rule_coverage",
    "matched_checks": ["source_binding", "rule_and_area_mapping"],
    "missing_checks": ["independent_corroboration", "counterevidence_review"],
    "numerator": 2,
    "denominator": 4,
    "score": 0.5,
    "outcome_probability": False,
}
_COUNTEREVIDENCE = {
    "status": "unknown",
    "statement": (
        "No independent or contrary evidence is present in the admitted source "
        "item; counterevidence remains unknown."
    ),
    "source_indexes": [],
}
_INTERPRETATION_BOUNDARY = {
    "directional_forecast": False,
    "causal_attribution": "none",
    "profitability_claim": False,
    "execution_authority": "none",
    "user_review_required": True,
}
_ACCOUNTING = {
    "scope": "trading_impact_engine_only",
    "model_calls_performed": 0,
    "provider_calls_performed": 0,
    "network_requests_performed": 0,
    "market_calls_performed": 0,
    "database_writes_performed": 0,
}

_MANIFEST = {
    "version": TRADING_IMPACT_RULESET_VERSION,
    "mapping_version": TRADING_IMPACT_MAPPING_VERSION,
    "projection_key_version": TRADING_IMPACT_PROJECTION_KEY_VERSION,
    "projection_version": TRADING_IMPACT_PROJECTION_VERSION,
    "hypothesis_version": TRADING_IMPACT_HYPOTHESIS_VERSION,
    "source_semantics_version": TRADING_IMPACT_SOURCE_SEMANTICS_VERSION,
    "source_bindings": [
        {"adapter_id": adapter_id, **binding}
        for adapter_id, binding in _SOURCE_BINDINGS.items()
    ],
    "direct_security_ids": list(_DIRECT_SECURITY_IDS),
    "sector_security_map": [
        {"sector_id": sector_id, "security_ids": list(security_ids)}
        for sector_id, security_ids in _SECTOR_SECURITY_MAP
    ],
    "macro_authority_families": [
        {"authority": authority, "families": list(families)}
        for authority, families in _MACRO_AUTHORITY_FAMILIES
    ],
    "rule_order": list(_RULE_ORDER),
    "rules": [
        {"rule_id": rule_id, **_RULE_SPECS[rule_id]}
        for rule_id in _RULE_ORDER
    ],
    "confidence_policy": dict(_CONFIDENCE_BASIS),
    "counterevidence_policy": dict(_COUNTEREVIDENCE),
    "limits": {
        "max_input_item_bytes": MAX_SOURCE_IMPORT_BYTES,
        "max_manifest_bytes": MAX_TRADING_IMPACT_MANIFEST_BYTES,
        "max_projection_bytes": MAX_TRADING_IMPACT_PROJECTION_BYTES,
        "max_json_depth": MAX_TRADING_IMPACT_JSON_DEPTH,
        "max_matched_rules": MAX_TRADING_IMPACT_MATCHED_RULES,
        "max_hypotheses": MAX_TRADING_IMPACT_HYPOTHESES,
        "max_statement_chars": MAX_TRADING_IMPACT_STATEMENT_CHARS,
        "source_indexes_per_hypothesis": 1,
    },
    "interpretation_boundary": dict(_INTERPRETATION_BOUNDARY),
    "accounting": dict(_ACCOUNTING),
}

# Golden hash of the complete canonical manifest above.  A semantic change must
# use a new ruleset version; changing this value under v1 is not permitted.
TRADING_IMPACT_RULESET_SHA256 = (
    "28ee013d4841ff7d1f955204ae1f9fa8007b544c2a13f0bf7f5e7ee705b93603"
)
if canonical_sha256(_MANIFEST) != TRADING_IMPACT_RULESET_SHA256:
    raise RuntimeError("trading_impact_rules_v1 manifest drift")


class TradingImpactRulesError(ValueError):
    """Machine-readable deterministic impact-rule contract failure."""

    def __init__(self, code: str, message: str | None = None) -> None:
        if type(code) is not str or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code):
            raise ValueError("TradingImpactRulesError code must be a canonical error token")
        self.code = code
        self.message = message or code
        super().__init__(self.message)


def _error(code: str, message: str) -> TradingImpactRulesError:
    return TradingImpactRulesError(code, message)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_size(value: Any) -> int:
    return len(_canonical_json(value).encode("utf-8"))


def _validate_native_json(value: Any, *, field: str, depth: int = 0) -> None:
    if depth > MAX_TRADING_IMPACT_JSON_DEPTH:
        raise _error("TRADING_IMPACT_DEPTH_INVALID", f"{field} exceeds JSON depth bound")
    if type(value) is dict:
        for key, child in value.items():
            if type(key) is not str:
                raise _error("TRADING_IMPACT_TYPE_INVALID", f"{field} has a non-string key")
            _validate_native_json(child, field=f"{field}.{key}", depth=depth + 1)
        return
    if type(value) is list:
        for index, child in enumerate(value):
            _validate_native_json(child, field=f"{field}[{index}]", depth=depth + 1)
        return
    if value is None or type(value) in {str, int, bool}:
        return
    if type(value) is float and math.isfinite(value):
        return
    raise _error("TRADING_IMPACT_TYPE_INVALID", f"{field} is not strict finite JSON")


def _exact_dict(value: Any, fields: frozenset[str], *, field: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise _error("TRADING_IMPACT_TYPE_INVALID", f"{field} must be an object")
    if set(value) != fields:
        raise _error("TRADING_IMPACT_FIELDS_INVALID", f"{field} fields do not match v1")
    return value


def _text(
    value: Any,
    *,
    field: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        raise _error("TRADING_IMPACT_TEXT_INVALID", f"{field} must be a native string")
    if value != unicodedata.normalize("NFC", value) or value != value.strip():
        raise _error("TRADING_IMPACT_TEXT_NONCANONICAL", f"{field} is not canonical text")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise _error("TRADING_IMPACT_TEXT_INVALID", f"{field} contains a control character")
    if not value and not allow_empty:
        raise _error("TRADING_IMPACT_TEXT_INVALID", f"{field} must not be empty")
    if len(value) > maximum:
        raise _error("TRADING_IMPACT_TEXT_TOO_LONG", f"{field} exceeds {maximum} characters")
    return value


def _slug(value: Any, *, field: str) -> str:
    clean = _text(value, field=field, maximum=80)
    if not _SLUG_RE.fullmatch(clean):
        raise _error("TRADING_IMPACT_ENUM_INVALID", f"{field} is not a canonical slug")
    return clean


def _hash(value: Any, *, field: str) -> str:
    clean = _text(value, field=field, maximum=64)
    if not _HASH_RE.fullmatch(clean):
        raise _error("TRADING_IMPACT_HASH_INVALID", f"{field} must be lowercase SHA-256")
    return clean


def _rfc3339(value: Any, *, field: str, allow_empty: bool = False) -> str:
    clean = _text(value, field=field, maximum=40, allow_empty=allow_empty)
    if not clean and allow_empty:
        return clean
    if not _RFC3339_UTC_RE.fullmatch(clean):
        raise _error("TRADING_IMPACT_TIME_INVALID", f"{field} must be canonical UTC RFC3339")
    try:
        datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error("TRADING_IMPACT_TIME_INVALID", f"{field} is not a real timestamp") from exc
    return clean


def _native_int(value: Any, *, field: str, minimum: int = 0, maximum: int = (1 << 63) - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise _error("TRADING_IMPACT_INTEGER_INVALID", f"{field} is outside its native integer bound")
    return value


def _source_indexes(value: Any, *, source_count: int, field: str) -> list[int]:
    if type(value) is not list or not value or len(value) != 1:
        raise _error("TRADING_IMPACT_SOURCE_INDEX_INVALID", f"{field} must contain exactly one index")
    index = value[0]
    if type(index) is not int or not 0 <= index < source_count:
        raise _error("TRADING_IMPACT_SOURCE_INDEX_INVALID", f"{field} index is outside the parent source table")
    return [index]


_PARENT_ITEM_FIELDS = frozenset({
    "version",
    "external_item_id",
    "item_type",
    "severity",
    "occurred_at",
    "published_at",
    "entities",
    "headline",
    "summary",
    "facts",
    "sources",
    "impact_hypotheses",
    "unknowns",
    "confidence",
    "recommended_route",
    "extensions",
    "external_claims_verification",
    "server_fingerprint_version",
    "server_fingerprint",
})
_ENTITY_FIELDS = frozenset({"kind", "id", "label"})
_FACT_FIELDS = frozenset({"claim", "source_indexes"})
_SOURCE_FIELDS = frozenset({"url", "publisher", "source_type", "published_at", "content_sha256"})


def _validate_parent_item(value: Any, *, item_sha256: Any) -> dict[str, Any]:
    _validate_native_json(value, field="item")
    item = _exact_dict(value, _PARENT_ITEM_FIELDS, field="item")
    if _canonical_size(item) > MAX_SOURCE_IMPORT_BYTES:
        raise _error("TRADING_IMPACT_INPUT_TOO_LARGE", "item exceeds the source-import byte bound")
    if item.get("version") != PROJECT_SOURCE_ITEM_VERSION:
        raise _error("TRADING_IMPACT_ITEM_VERSION_INVALID", "parent item version is unsupported")
    _text(item.get("external_item_id"), field="item.external_item_id", maximum=200, allow_empty=True)
    _slug(item.get("item_type"), field="item.item_type")
    if item.get("severity") != "info" or item.get("recommended_route") != "notify_only":
        raise _error("TRADING_IMPACT_PARENT_BOUNDARY_INVALID", "monitoring parent must remain info/notify_only")
    _rfc3339(item.get("occurred_at"), field="item.occurred_at")
    _rfc3339(item.get("published_at"), field="item.published_at", allow_empty=True)
    _text(item.get("headline"), field="item.headline", maximum=500)
    _text(item.get("summary"), field="item.summary", maximum=8_000)
    if type(item.get("confidence")) is not float or item["confidence"] != 1.0:
        raise _error("TRADING_IMPACT_PARENT_BOUNDARY_INVALID", "monitoring parent confidence must remain 1.0")
    if item.get("impact_hypotheses") != []:
        raise _error("TRADING_IMPACT_PARENT_HYPOTHESES_PRESENT", "parent impact_hypotheses must remain empty")
    if item.get("external_claims_verification") != EXTERNAL_UNVERIFIED:
        raise _error("TRADING_IMPACT_PARENT_TRUST_INVALID", "parent trust marker must remain external_unverified")
    if item.get("server_fingerprint_version") != SOURCE_ITEM_FINGERPRINT_VERSION:
        raise _error("TRADING_IMPACT_FINGERPRINT_VERSION_INVALID", "parent fingerprint version is unsupported")
    stored_fingerprint = _hash(item.get("server_fingerprint"), field="item.server_fingerprint")
    if stored_fingerprint != project_source_item_fingerprint(item):
        raise _error("TRADING_IMPACT_FINGERPRINT_INVALID", "parent fingerprint does not recompute")
    clean_item_sha = _hash(item_sha256, field="item_sha256")
    if clean_item_sha != canonical_sha256(item):
        raise _error("TRADING_IMPACT_ITEM_HASH_INVALID", "parent item hash does not recompute")

    entities = item.get("entities")
    if type(entities) is not list or not 1 <= len(entities) <= 50:
        raise _error("TRADING_IMPACT_ENTITY_INVALID", "parent entities are outside bounds")
    seen_entities: set[tuple[str, str]] = set()
    for index, value_entity in enumerate(entities):
        entity = _exact_dict(value_entity, _ENTITY_FIELDS, field=f"item.entities[{index}]")
        kind = _slug(entity.get("kind"), field=f"item.entities[{index}].kind")
        entity_id = _text(entity.get("id"), field=f"item.entities[{index}].id", maximum=200)
        _text(entity.get("label"), field=f"item.entities[{index}].label", maximum=240)
        if (kind, entity_id) in seen_entities:
            raise _error("TRADING_IMPACT_ENTITY_INVALID", "parent entity identity is duplicated")
        seen_entities.add((kind, entity_id))

    sources = item.get("sources")
    if type(sources) is not list or not 1 <= len(sources) <= 12:
        raise _error("TRADING_IMPACT_SOURCE_INVALID", "parent sources are outside bounds")
    for index, value_source in enumerate(sources):
        source = _exact_dict(value_source, _SOURCE_FIELDS, field=f"item.sources[{index}]")
        _text(source.get("url"), field=f"item.sources[{index}].url", maximum=2_000)
        _text(source.get("publisher"), field=f"item.sources[{index}].publisher", maximum=200)
        _slug(source.get("source_type"), field=f"item.sources[{index}].source_type")
        _rfc3339(source.get("published_at"), field=f"item.sources[{index}].published_at", allow_empty=True)
        content_hash = source.get("content_sha256")
        if content_hash != "":
            _hash(content_hash, field=f"item.sources[{index}].content_sha256")

    facts = item.get("facts")
    if type(facts) is not list or not 1 <= len(facts) <= 50:
        raise _error("TRADING_IMPACT_FACT_INVALID", "parent facts are outside bounds")
    for index, value_fact in enumerate(facts):
        fact = _exact_dict(value_fact, _FACT_FIELDS, field=f"item.facts[{index}]")
        _text(fact.get("claim"), field=f"item.facts[{index}].claim", maximum=4_000)
        indexes = fact.get("source_indexes")
        if type(indexes) is not list or not indexes or len(indexes) > 12:
            raise _error("TRADING_IMPACT_FACT_INVALID", "parent fact source indexes are invalid")
        if any(type(index_value) is not int or not 0 <= index_value < len(sources) for index_value in indexes):
            raise _error("TRADING_IMPACT_FACT_INVALID", "parent fact source index is outside bounds")
        if len(set(indexes)) != len(indexes):
            raise _error("TRADING_IMPACT_FACT_INVALID", "parent fact source index is duplicated")

    unknowns = item.get("unknowns")
    if type(unknowns) is not list or len(unknowns) > 30:
        raise _error("TRADING_IMPACT_UNKNOWN_INVALID", "parent unknowns are outside bounds")
    for index, unknown in enumerate(unknowns):
        _text(unknown, field=f"item.unknowns[{index}]", maximum=2_000)
    if type(item.get("extensions")) is not dict:
        raise _error("TRADING_IMPACT_EXTENSION_INVALID", "parent extensions must be an object")
    return item


def _security_entity(item: dict[str, Any], *, allowed: tuple[str, ...]) -> str:
    securities = [
        entity["id"]
        for entity in item["entities"]
        if entity["kind"] == "security"
    ]
    if len(securities) != 1 or securities[0] not in allowed:
        raise _error("TRADING_IMPACT_SECURITY_INVALID", "parent must bind one admitted security")
    return securities[0]


def _unique_source_index(
    item: dict[str, Any],
    *,
    source_type: str,
    required_hash: str | None = None,
) -> int:
    indexes = [
        index
        for index, source in enumerate(item["sources"])
        if source["source_type"] == source_type
    ]
    if len(indexes) != 1:
        raise _error("TRADING_IMPACT_SOURCE_BINDING_INVALID", "evidence source_type is not unique")
    index = indexes[0]
    if required_hash is not None and item["sources"][index]["content_sha256"] != required_hash:
        raise _error("TRADING_IMPACT_SOURCE_BINDING_INVALID", "evidence projection hash does not match extension")
    return index


_SEC_EXTENSION_FIELDS = frozenset({
    "accession_number",
    "accepted_at",
    "cik",
    "discovered_at_ms",
    "filing_date",
    "form",
    "items",
    "primary_document",
    "submissions_metadata_only",
    "symbol",
})
_IR_EXTENSION_FIELDS = frozenset({
    "event_type",
    "fiscal_period",
    "guid",
    "identity_kind",
    "identity_value",
    "identity_sha256",
    "is_revision",
    "previous_rss_projection_sha256",
    "rss_hash_semantics",
    "rss_projection_sha256",
    "rss_projection_version",
})
_MACRO_EXTENSION_FIELDS = frozenset({
    "authority",
    "data",
    "event_state",
    "family",
    "identity_sha256",
    "identity_version",
    "official_id",
    "official_revision",
    "occurrence_at",
    "occurrence_basis",
    "previous_projection_sha256",
    "projection_hash_semantics",
    "projection_sha256",
    "projection_version",
    "reference_period",
    "released_at",
    "revision_target",
    "scheduled_at",
    "status_basis",
    "subject_phase",
})
_FUTU_EXTENSION_FIELDS = frozenset({
    "causal_attribution",
    "content_hash_semantics",
    "entry_threshold",
    "exit_threshold",
    "metric",
    "news_attribution_performed",
    "projection_version",
    "rule_id",
    "signal_direction",
    "signal_only",
    "symbol",
    "unit",
    "us_eastern_market_date",
})


def _extension(item: dict[str, Any], key: str, fields: frozenset[str]) -> dict[str, Any]:
    if set(item["extensions"]) != {key}:
        raise _error("TRADING_IMPACT_EXTENSION_INVALID", "parent extension namespace is not exact")
    return _exact_dict(item["extensions"][key], fields, field=f"item.extensions.{key}")


def _sec_context(item: dict[str, Any]) -> dict[str, Any]:
    extension = _extension(item, "sec_v1", _SEC_EXTENSION_FIELDS)
    symbol = _security_entity(item, allowed=_DIRECT_SECURITY_IDS)
    if extension.get("symbol") != symbol or extension.get("submissions_metadata_only") is not True:
        raise _error("TRADING_IMPACT_SEC_INVALID", "SEC extension symbol/metadata boundary is invalid")
    form = extension.get("form")
    if type(form) is not str or form not in _SEC_PERIODIC_FORMS + _SEC_CURRENT_FORMS:
        raise _error("TRADING_IMPACT_SEC_INVALID", "SEC form is outside the sealed set")
    _native_int(extension.get("discovered_at_ms"), field="sec_v1.discovered_at_ms")
    accession = _text(
        extension.get("accession_number"),
        field="sec_v1.accession_number",
        maximum=240,
    )
    cik = _text(extension.get("cik"), field="sec_v1.cik", maximum=240)
    accepted_at = _rfc3339(
        extension.get("accepted_at"),
        field="sec_v1.accepted_at",
        allow_empty=True,
    )
    filing_date = _text(
        extension.get("filing_date"),
        field="sec_v1.filing_date",
        maximum=10,
    )
    if _DATE_RE.fullmatch(filing_date) is None:
        raise _error("TRADING_IMPACT_SEC_INVALID", "SEC filing_date is invalid")
    try:
        datetime.strptime(filing_date, "%Y-%m-%d")
    except ValueError as exc:
        raise _error("TRADING_IMPACT_SEC_INVALID", "SEC filing_date is not a real date") from exc
    _text(
        extension.get("primary_document"),
        field="sec_v1.primary_document",
        maximum=240,
        allow_empty=True,
    )
    if accepted_at:
        if accepted_at != item["occurred_at"]:
            raise _error(
                "TRADING_IMPACT_SEC_INVALID",
                "SEC accepted_at does not match the parent occurrence anchor",
            )
        time_semantics = "sec_acceptance_time"
        time_precision = "timestamp"
    else:
        expected_anchor = f"{filing_date}T00:00:00Z"
        if item["occurred_at"] != expected_anchor:
            raise _error(
                "TRADING_IMPACT_SEC_INVALID",
                "SEC date-only occurrence does not match the filing-date anchor",
            )
        time_semantics = "sec_filing_date_anchor_not_exact_time"
        time_precision = "date_anchor"
    if item["external_item_id"] != accession:
        raise _error("TRADING_IMPACT_SEC_INVALID", "SEC external identity does not match accession")
    issuers = [entity["id"] for entity in item["entities"] if entity["kind"] == "issuer"]
    if issuers != [cik]:
        raise _error("TRADING_IMPACT_SEC_INVALID", "SEC issuer entity does not match CIK")
    raw_items = extension.get("items")
    if type(raw_items) is not list or len(raw_items) > 40 or any(type(value) is not str for value in raw_items):
        raise _error("TRADING_IMPACT_SEC_INVALID", "SEC form items are invalid")
    source_index = _unique_source_index(item, source_type="regulatory_filing")
    if len(item["sources"]) != 1 or source_index != 0 or item["sources"][0]["content_sha256"] != "":
        raise _error("TRADING_IMPACT_SEC_INVALID", "SEC source must remain metadata-only at index zero")
    rule_id = (
        "sec_periodic_filing_review_v1"
        if form in _SEC_PERIODIC_FORMS
        else "sec_current_filing_review_v1"
    )
    return {
        "rule_id": rule_id,
        "symbol": symbol,
        "form": form,
        "source_index": source_index,
        "time_semantics": time_semantics,
        "time_precision": time_precision,
    }


def _ir_context(item: dict[str, Any]) -> dict[str, Any]:
    extension = _extension(item, "company_ir_v1", _IR_EXTENSION_FIELDS)
    symbol = _security_entity(item, allowed=_STORAGE_SECURITY_IDS)
    event_type = extension.get("event_type")
    if type(event_type) is not str or event_type not in _IR_EVENT_TYPES:
        raise _error("TRADING_IMPACT_IR_INVALID", "IR event_type is outside the sealed set")
    if type(extension.get("is_revision")) is not bool:
        raise _error("TRADING_IMPACT_IR_INVALID", "IR revision flag must be native boolean")
    projection_hash = _hash(extension.get("rss_projection_sha256"), field="company_ir_v1.rss_projection_sha256")
    if extension.get("rss_projection_version") != "company_ir_rss_projection_v1" or extension.get("rss_hash_semantics") != "normalized_rss_item_not_web_page_body":
        raise _error("TRADING_IMPACT_IR_INVALID", "IR projection semantics/version is invalid")
    identity_kind = extension.get("identity_kind")
    if identity_kind not in {"guid", "url"}:
        raise _error("TRADING_IMPACT_IR_INVALID", "IR identity kind is outside the sealed set")
    for field in (
        "fiscal_period",
        "guid",
        "identity_value",
    ):
        _text(extension.get(field), field=f"company_ir_v1.{field}", maximum=1_000, allow_empty=True)
    identity_sha = _hash(extension.get("identity_sha256"), field="company_ir_v1.identity_sha256")
    previous_projection = extension.get("previous_rss_projection_sha256")
    if previous_projection != "":
        _hash(previous_projection, field="company_ir_v1.previous_rss_projection_sha256")
    if extension["is_revision"] != bool(previous_projection):
        raise _error("TRADING_IMPACT_IR_INVALID", "IR revision flag/history binding is invalid")
    if item["external_item_id"] != f"ir-{identity_sha}":
        raise _error("TRADING_IMPACT_IR_INVALID", "IR external identity does not match identity hash")
    source_index = _unique_source_index(
        item,
        source_type="company_ir_rss_projection",
        required_hash=projection_hash,
    )
    if len(item["sources"]) != 2 or source_index != 1 or item["sources"][0]["source_type"] != "company_ir" or item["sources"][0]["content_sha256"] != "":
        raise _error("TRADING_IMPACT_IR_INVALID", "IR page/projection source order is invalid")
    if extension["is_revision"]:
        rule_id: str | None = "ir_revision_review_v1"
    elif event_type == "earnings_schedule":
        rule_id = "ir_earnings_schedule_review_v1"
    elif event_type in {"earnings_release", "earnings_material"}:
        rule_id = "ir_earnings_disclosure_review_v1"
    else:
        rule_id = None
    return {
        "rule_id": rule_id,
        "symbol": symbol,
        "event_type": event_type,
        "is_revision": extension["is_revision"],
        "source_index": source_index,
    }


def _macro_family_allowed(authority: str, family: str) -> bool:
    return any(
        authority == allowed_authority and family in families
        for allowed_authority, families in _MACRO_AUTHORITY_FAMILIES
    )


def _macro_context(item: dict[str, Any], adapter_id: str) -> dict[str, Any]:
    extension = _extension(item, "macro_official_v1", _MACRO_EXTENSION_FIELDS)
    authority = extension.get("authority")
    family = extension.get("family")
    phase = extension.get("subject_phase")
    state = extension.get("event_state")
    if type(authority) is not str or type(family) is not str or not _macro_family_allowed(authority, family):
        raise _error("TRADING_IMPACT_MACRO_INVALID", "macro authority/family pair is outside the sealed set")
    expected_authority = {
        "federal_reserve": "federal_reserve",
        "bls_releases": "bls",
        "treasury_releases": "treasury",
    }.get(adapter_id)
    if expected_authority is not None and authority != expected_authority:
        raise _error("TRADING_IMPACT_MACRO_INVALID", "macro adapter authority binding is invalid")
    if adapter_id == "official_macro_calendar":
        if phase != "schedule" or state not in {"scheduled", "revised"}:
            raise _error("TRADING_IMPACT_MACRO_INVALID", "macro calendar phase/state is invalid")
        source_type = "official_macro_calendar_projection"
        rule_id = (
            "macro_schedule_revision_review_v1"
            if state == "revised"
            else "macro_schedule_review_v1"
        )
    else:
        if phase != "release" or state not in {"released", "revised"}:
            raise _error("TRADING_IMPACT_MACRO_INVALID", "macro release phase/state is invalid")
        source_type = "official_macro_release_projection"
        rule_id = (
            "macro_release_revision_review_v1"
            if state == "revised"
            else "macro_release_review_v1"
        )
    if extension.get("identity_version") != "official_macro_lifecycle_v1" or extension.get("projection_version") != "official_macro_projection_v1":
        raise _error("TRADING_IMPACT_MACRO_INVALID", "macro identity/projection version is invalid")
    projection_hash = _hash(extension.get("projection_sha256"), field="macro_official_v1.projection_sha256")
    identity_sha = _hash(extension.get("identity_sha256"), field="macro_official_v1.identity_sha256")
    if item["external_item_id"] != f"macro-{identity_sha}":
        raise _error("TRADING_IMPACT_MACRO_INVALID", "macro external identity does not match identity hash")
    previous_projection = extension.get("previous_projection_sha256")
    if previous_projection != "":
        _hash(previous_projection, field="macro_official_v1.previous_projection_sha256")
    occurrence = _rfc3339(extension.get("occurrence_at"), field="macro_official_v1.occurrence_at")
    if occurrence != item["occurred_at"]:
        raise _error("TRADING_IMPACT_MACRO_INVALID", "macro occurrence does not match parent")
    occurrence_basis = extension.get("occurrence_basis")
    if occurrence_basis not in _MACRO_OCCURRENCE_PRECISION:
        raise _error("TRADING_IMPACT_MACRO_INVALID", "macro occurrence basis is outside the sealed set")
    if type(extension.get("official_revision")) is not bool or type(extension.get("data")) is not dict:
        raise _error("TRADING_IMPACT_MACRO_INVALID", "macro data/revision representation is invalid")
    source_index = _unique_source_index(item, source_type=source_type, required_hash=projection_hash)
    if source_index != len(item["sources"]) - 1 or len(item["sources"]) not in {1, 2}:
        raise _error("TRADING_IMPACT_MACRO_INVALID", "macro projection source must be the final source")
    institutions = [entity["id"] for entity in item["entities"] if entity["kind"] == "institution"]
    if institutions != [authority]:
        raise _error("TRADING_IMPACT_MACRO_INVALID", "macro institution entity does not match authority")
    return {
        "rule_id": rule_id,
        "authority": authority,
        "family": family,
        "subject_phase": phase,
        "event_state": state,
        "source_index": source_index,
        "occurrence_basis": occurrence_basis,
    }


def _futu_context(item: dict[str, Any]) -> dict[str, Any]:
    extension = _extension(item, "futu_anomaly_v1", _FUTU_EXTENSION_FIELDS)
    symbol = _security_entity(item, allowed=_STORAGE_SECURITY_IDS)
    if extension.get("symbol") != symbol:
        raise _error("TRADING_IMPACT_FUTU_INVALID", "Futu extension symbol does not match entity")
    upstream_rule_id = extension.get("rule_id")
    if type(upstream_rule_id) is not str or upstream_rule_id not in _FUTU_RULE_SPECS:
        raise _error("TRADING_IMPACT_FUTU_INVALID", "Futu upstream rule is outside the sealed set")
    expected = _FUTU_RULE_SPECS[upstream_rule_id]
    for field, expected_value in expected.items():
        if extension.get(field) != expected_value:
            raise _error("TRADING_IMPACT_FUTU_INVALID", f"Futu {field} drifted from the sealed policy")
    if (
        extension.get("causal_attribution") != "none"
        or extension.get("news_attribution_performed") is not False
        or extension.get("signal_only") is not True
        or extension.get("projection_version") != "futu_anomaly_projection_v1"
        or extension.get("content_hash_semantics") != "stable_session_rule_signal_semantics_not_web_body"
    ):
        raise _error("TRADING_IMPACT_FUTU_INVALID", "Futu no-attribution boundary is invalid")
    session_date = extension.get("us_eastern_market_date")
    if type(session_date) is not str or not _DATE_RE.fullmatch(session_date):
        raise _error("TRADING_IMPACT_FUTU_INVALID", "Futu market date is invalid")
    identity_sha = canonical_sha256({
        "version": "futu_anomaly_identity_v1",
        "symbol": symbol,
        "session_date": session_date,
        "rule_id": upstream_rule_id,
    })
    if item["external_item_id"] != f"futu-anomaly-{identity_sha}":
        raise _error("TRADING_IMPACT_FUTU_INVALID", "Futu external identity does not match sealed episode")
    if item["occurred_at"][:10] != session_date:
        raise _error("TRADING_IMPACT_FUTU_INVALID", "Futu occurrence anchor does not match market date")
    source_index = _unique_source_index(item, source_type="readonly_market_signal")
    if len(item["sources"]) != 1 or source_index != 0 or not item["sources"][0]["content_sha256"]:
        raise _error("TRADING_IMPACT_FUTU_INVALID", "Futu evidence source is invalid")
    rule_id = {
        "price_up_5pct": "futu_price_up_condition_review_v1",
        "price_down_5pct": "futu_price_down_condition_review_v1",
        "amplitude_8pct": "futu_range_condition_review_v1",
        "volume_ratio_3x": "futu_market_activity_condition_review_v1",
    }[upstream_rule_id]
    return {
        "rule_id": rule_id,
        "symbol": symbol,
        "source_index": source_index,
        "upstream_rule_id": upstream_rule_id,
        "session_date": session_date,
    }


def _source_context(item: dict[str, Any], *, adapter_id: str) -> dict[str, Any]:
    if adapter_id == "sec_filings":
        return _sec_context(item)
    if adapter_id == "company_ir":
        return _ir_context(item)
    if adapter_id in {"federal_reserve", "bls_releases", "treasury_releases", "official_macro_calendar"}:
        return _macro_context(item, adapter_id)
    if adapter_id == "futu_anomaly_signals":
        return _futu_context(item)
    raise _error("TRADING_IMPACT_SOURCE_BINDING_INVALID", "adapter is outside the sealed set")


def _statement(rule_id: str, context: dict[str, Any], *, sector: str | None = None) -> str:
    values = dict(context)
    if sector is not None:
        values["sector"] = sector.upper()
    return _RULE_SPECS[rule_id]["statement_template"].format(**values)


def _time_semantics(rule_id: str, context: dict[str, Any]) -> tuple[str, str]:
    if rule_id.startswith("sec_"):
        return context["time_semantics"], context["time_precision"]
    if rule_id == "ir_earnings_schedule_review_v1":
        return "schedule_announcement_publication_time", "event_date_unknown"
    if rule_id.startswith("ir_"):
        return "source_publication_time", "timestamp"
    if rule_id.startswith("macro_"):
        semantics = context["occurrence_basis"]
        return semantics, _MACRO_OCCURRENCE_PRECISION[semantics]
    return "us_eastern_session_identity_anchor_not_tick_time", "date_anchor"


def _source_semantic_binding(
    *,
    adapter_id: str,
    item: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    rule_id = context["rule_id"] or ""
    if rule_id:
        time_semantics, time_precision = _time_semantics(rule_id, context)
    else:
        time_semantics, time_precision = "source_publication_time", "timestamp"
    return {
        "version": TRADING_IMPACT_SOURCE_SEMANTICS_VERSION,
        "adapter_id": adapter_id,
        "rule_id": rule_id,
        "source_index": context["source_index"],
        "symbol": context.get("symbol", ""),
        "form": context.get("form", ""),
        "event_type": context.get("event_type", ""),
        "revision_state": (
            "revised" if context.get("is_revision") is True else "original"
            if adapter_id == "company_ir"
            else "not_applicable"
        ),
        "authority": context.get("authority", ""),
        "family": context.get("family", ""),
        "subject_phase": context.get("subject_phase", ""),
        "event_state": context.get("event_state", ""),
        "occurrence_basis": context.get("occurrence_basis", ""),
        "upstream_rule_id": context.get("upstream_rule_id", ""),
        "session_date": context.get("session_date", ""),
        "anchor_at": item["occurred_at"],
        "anchor_semantics": time_semantics,
        "precision": time_precision,
    }


def _time_dimension(rule_id: str, item: dict[str, Any], context: dict[str, Any]) -> dict[str, str]:
    horizon = _RULE_SPECS[rule_id]["horizon"]
    semantics, precision = _time_semantics(rule_id, context)
    return {
        "horizon_id": horizon,
        "anchor_at": item["occurred_at"],
        "anchor_semantics": semantics,
        "precision": precision,
    }


def _hypothesis(
    *,
    rule_id: str,
    item: dict[str, Any],
    context: dict[str, Any],
    area_kind: str,
    area_id: str,
    security_ids: tuple[str, ...],
) -> dict[str, Any]:
    affected_area = f"{area_kind}:{area_id}"
    impact_hypothesis = {
        "statement": _statement(
            rule_id,
            context,
            sector=area_id if area_kind == "sector" else None,
        ),
        "affected_area": affected_area,
        "time_horizon": _RULE_SPECS[rule_id]["horizon"],
        "source_indexes": [context["source_index"]],
        "confidence": 0.5,
    }
    basis = {
        "version": TRADING_IMPACT_HYPOTHESIS_VERSION,
        "rule_id": rule_id,
        "impact_hypothesis": impact_hypothesis,
        "affected_area_binding": {
            "kind": area_kind,
            "id": area_id,
            "security_ids": list(security_ids),
        },
        "transmission_mechanism": _RULE_SPECS[rule_id]["mechanism"],
        "time_dimension": _time_dimension(rule_id, item, context),
        "confidence_basis": json.loads(_canonical_json(_CONFIDENCE_BASIS)),
        "counterevidence": json.loads(_canonical_json(_COUNTEREVIDENCE)),
    }
    return {
        **basis,
        "hypothesis_sha256": canonical_sha256(basis),
    }


def _expected_statement(
    rule_id: str,
    semantic: dict[str, Any],
    *,
    area_id: str,
) -> str:
    values = dict(semantic)
    if _RULE_SPECS[rule_id]["area_kind"] == "sector":
        values["sector"] = area_id.upper()
    return _RULE_SPECS[rule_id]["statement_template"].format(**values)


_PROJECTION_FIELDS = frozenset({
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
})
_SOURCE_BINDING_FIELDS = frozenset({"adapter_id", "source_class", "source_channel"})
_SOURCE_ITEM_BINDING_FIELDS = frozenset({
    "item_version",
    "external_item_id",
    "item_type",
    "item_sha256",
    "server_fingerprint_version",
    "server_fingerprint",
    "source_semantic_binding",
})
_SOURCE_SEMANTIC_BINDING_FIELDS = frozenset({
    "version",
    "adapter_id",
    "rule_id",
    "source_index",
    "symbol",
    "form",
    "event_type",
    "revision_state",
    "authority",
    "family",
    "subject_phase",
    "event_state",
    "occurrence_basis",
    "upstream_rule_id",
    "session_date",
    "anchor_at",
    "anchor_semantics",
    "precision",
})
_HYPOTHESIS_FIELDS = frozenset({
    "version",
    "hypothesis_sha256",
    "rule_id",
    "impact_hypothesis",
    "affected_area_binding",
    "transmission_mechanism",
    "time_dimension",
    "confidence_basis",
    "counterevidence",
})
_IMPACT_HYPOTHESIS_FIELDS = frozenset({
    "statement",
    "affected_area",
    "time_horizon",
    "source_indexes",
    "confidence",
})
_AREA_FIELDS = frozenset({"kind", "id", "security_ids"})
_TIME_FIELDS = frozenset({"horizon_id", "anchor_at", "anchor_semantics", "precision"})
_CONFIDENCE_FIELDS = frozenset(_CONFIDENCE_BASIS)
_COUNTEREVIDENCE_FIELDS = frozenset(_COUNTEREVIDENCE)
_INTERPRETATION_FIELDS = frozenset(_INTERPRETATION_BOUNDARY)
_ACCOUNTING_FIELDS = frozenset(_ACCOUNTING)


def _semantic_expected_rule(
    adapter_id: str,
    semantic: dict[str, Any],
) -> str:
    if adapter_id == "sec_filings":
        form = semantic["form"]
        if form in _SEC_PERIODIC_FORMS:
            return "sec_periodic_filing_review_v1"
        if form in _SEC_CURRENT_FORMS:
            return "sec_current_filing_review_v1"
        raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "SEC form is outside the sealed set")
    if adapter_id == "company_ir":
        if semantic["revision_state"] == "revised":
            return "ir_revision_review_v1"
        event_type = semantic["event_type"]
        if event_type == "earnings_schedule":
            return "ir_earnings_schedule_review_v1"
        if event_type in {"earnings_release", "earnings_material"}:
            return "ir_earnings_disclosure_review_v1"
        if event_type == "other":
            return ""
        raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "IR event_type is outside the sealed set")
    if adapter_id == "official_macro_calendar":
        if semantic["subject_phase"] != "schedule" or semantic["event_state"] not in {"scheduled", "revised"}:
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "macro schedule phase/state is invalid")
        return (
            "macro_schedule_revision_review_v1"
            if semantic["event_state"] == "revised"
            else "macro_schedule_review_v1"
        )
    if adapter_id in {"federal_reserve", "bls_releases", "treasury_releases"}:
        if semantic["subject_phase"] != "release" or semantic["event_state"] not in {"released", "revised"}:
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "macro release phase/state is invalid")
        return (
            "macro_release_revision_review_v1"
            if semantic["event_state"] == "revised"
            else "macro_release_review_v1"
        )
    if adapter_id == "futu_anomaly_signals":
        rule_id = {
            "price_up_5pct": "futu_price_up_condition_review_v1",
            "price_down_5pct": "futu_price_down_condition_review_v1",
            "amplitude_8pct": "futu_range_condition_review_v1",
            "volume_ratio_3x": "futu_market_activity_condition_review_v1",
        }.get(semantic["upstream_rule_id"])
        if rule_id is None:
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "Futu upstream rule is outside the sealed set")
        return rule_id
    raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "semantic adapter is outside the sealed set")


def _validate_source_semantic_binding(
    value: Any,
    *,
    adapter_id: str,
) -> dict[str, Any]:
    semantic = _exact_dict(
        value,
        _SOURCE_SEMANTIC_BINDING_FIELDS,
        field="projection.source_item_binding.source_semantic_binding",
    )
    if (
        semantic.get("version") != TRADING_IMPACT_SOURCE_SEMANTICS_VERSION
        or semantic.get("adapter_id") != adapter_id
    ):
        raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "source semantic version/adapter binding is invalid")
    for field in (
        "rule_id",
        "symbol",
        "form",
        "event_type",
        "revision_state",
        "authority",
        "family",
        "subject_phase",
        "event_state",
        "occurrence_basis",
        "upstream_rule_id",
        "session_date",
        "anchor_semantics",
        "precision",
    ):
        _text(
            semantic.get(field),
            field=f"projection.source_item_binding.source_semantic_binding.{field}",
            maximum=80,
            allow_empty=True,
        )
    _rfc3339(
        semantic.get("anchor_at"),
        field="projection.source_item_binding.source_semantic_binding.anchor_at",
    )
    source_index = _native_int(
        semantic.get("source_index"),
        field="projection.source_item_binding.source_semantic_binding.source_index",
        maximum=1,
    )

    empty_by_adapter = {
        "sec_filings": (
            "event_type", "authority", "family", "subject_phase", "event_state",
            "occurrence_basis", "upstream_rule_id", "session_date",
        ),
        "company_ir": (
            "form", "authority", "family", "subject_phase", "event_state",
            "occurrence_basis", "upstream_rule_id", "session_date",
        ),
        "official_macro_calendar": (
            "symbol", "form", "event_type", "upstream_rule_id", "session_date",
        ),
        "federal_reserve": (
            "symbol", "form", "event_type", "upstream_rule_id", "session_date",
        ),
        "bls_releases": (
            "symbol", "form", "event_type", "upstream_rule_id", "session_date",
        ),
        "treasury_releases": (
            "symbol", "form", "event_type", "upstream_rule_id", "session_date",
        ),
        "futu_anomaly_signals": (
            "form", "event_type", "authority", "family", "subject_phase",
            "event_state", "occurrence_basis",
        ),
    }[adapter_id]
    if any(semantic[field] != "" for field in empty_by_adapter):
        raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "source semantic fields cross adapter boundaries")

    if adapter_id in {"sec_filings", "futu_anomaly_signals"} and source_index != 0:
        raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "direct source index is invalid")
    if adapter_id == "company_ir" and source_index != 1:
        raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "IR source index is invalid")
    if adapter_id in {"sec_filings", "company_ir", "futu_anomaly_signals"}:
        allowed = _DIRECT_SECURITY_IDS if adapter_id == "sec_filings" else _STORAGE_SECURITY_IDS
        if semantic["symbol"] not in allowed:
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "direct security binding is invalid")
    if adapter_id == "sec_filings":
        if semantic["revision_state"] != "not_applicable":
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "SEC revision state is invalid")
        valid_time = (
            (
                semantic["anchor_semantics"] == "sec_acceptance_time"
                and semantic["precision"] == "timestamp"
            )
            or (
                semantic["anchor_semantics"] == "sec_filing_date_anchor_not_exact_time"
                and semantic["precision"] == "date_anchor"
                and semantic["anchor_at"].endswith("T00:00:00Z")
            )
        )
    elif adapter_id == "company_ir":
        if semantic["revision_state"] not in {"original", "revised"}:
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "IR revision state is invalid")
        valid_time = (
            semantic["anchor_semantics"]
            == (
                "schedule_announcement_publication_time"
                if semantic["rule_id"] == "ir_earnings_schedule_review_v1"
                else "source_publication_time"
            )
            and semantic["precision"]
            == (
                "event_date_unknown"
                if semantic["rule_id"] == "ir_earnings_schedule_review_v1"
                else "timestamp"
            )
        )
    elif adapter_id in {"federal_reserve", "bls_releases", "treasury_releases", "official_macro_calendar"}:
        if semantic["revision_state"] != "not_applicable":
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "macro revision state is invalid")
        if not _macro_family_allowed(semantic["authority"], semantic["family"]):
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "macro authority/family binding is invalid")
        expected_authority = {
            "federal_reserve": "federal_reserve",
            "bls_releases": "bls",
            "treasury_releases": "treasury",
        }.get(adapter_id)
        if expected_authority is not None and semantic["authority"] != expected_authority:
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "macro adapter authority binding is invalid")
        valid_time = (
            semantic["occurrence_basis"] in _MACRO_OCCURRENCE_PRECISION
            and semantic["anchor_semantics"] == semantic["occurrence_basis"]
            and semantic["precision"]
            == _MACRO_OCCURRENCE_PRECISION[semantic["occurrence_basis"]]
        )
    else:
        if semantic["revision_state"] != "not_applicable":
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "Futu revision state is invalid")
        if _DATE_RE.fullmatch(semantic["session_date"]) is None or semantic["anchor_at"][:10] != semantic["session_date"]:
            raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "Futu session anchor is invalid")
        valid_time = (
            semantic["anchor_semantics"]
            == "us_eastern_session_identity_anchor_not_tick_time"
            and semantic["precision"] == "date_anchor"
        )
    expected_rule = _semantic_expected_rule(adapter_id, semantic)
    if semantic["rule_id"] != expected_rule or not valid_time:
        raise _error("TRADING_IMPACT_SEMANTIC_BINDING_INVALID", "rule/time semantics do not match the bound source fields")
    return semantic


def _projection_key_basis(projection: dict[str, Any]) -> dict[str, Any]:
    item_binding = projection["source_item_binding"]
    return {
        "version": TRADING_IMPACT_PROJECTION_KEY_VERSION,
        "ruleset_version": TRADING_IMPACT_RULESET_VERSION,
        "ruleset_sha256": projection["ruleset_sha256"],
        "mapping_version": TRADING_IMPACT_MAPPING_VERSION,
        "source_binding": projection["source_binding"],
        "source_item_sha256": item_binding["item_sha256"],
        "server_fingerprint_version": item_binding["server_fingerprint_version"],
        "server_fingerprint": item_binding["server_fingerprint"],
    }


def _validate_projection(value: Any) -> dict[str, Any]:
    _validate_native_json(value, field="projection")
    projection = _exact_dict(value, _PROJECTION_FIELDS, field="projection")
    if _canonical_size(projection) > MAX_TRADING_IMPACT_PROJECTION_BYTES:
        raise _error("TRADING_IMPACT_PROJECTION_TOO_LARGE", "projection exceeds its byte bound")
    if (
        projection.get("version") != TRADING_IMPACT_PROJECTION_VERSION
        or projection.get("ruleset_version") != TRADING_IMPACT_RULESET_VERSION
        or projection.get("ruleset_sha256") != TRADING_IMPACT_RULESET_SHA256
        or projection.get("mapping_version") != TRADING_IMPACT_MAPPING_VERSION
        or projection.get("projection_key_version") != TRADING_IMPACT_PROJECTION_KEY_VERSION
    ):
        raise _error("TRADING_IMPACT_PROJECTION_VERSION_INVALID", "projection version identity is invalid")
    source_binding = _exact_dict(projection.get("source_binding"), _SOURCE_BINDING_FIELDS, field="projection.source_binding")
    adapter_id = _slug(source_binding.get("adapter_id"), field="projection.source_binding.adapter_id")
    expected_binding = _SOURCE_BINDINGS.get(adapter_id)
    if expected_binding is None or source_binding.get("source_class") != expected_binding["source_class"] or source_binding.get("source_channel") != expected_binding["source_channel"]:
        raise _error("TRADING_IMPACT_SOURCE_BINDING_INVALID", "projection source binding is invalid")
    item_binding = _exact_dict(projection.get("source_item_binding"), _SOURCE_ITEM_BINDING_FIELDS, field="projection.source_item_binding")
    if item_binding.get("item_version") != PROJECT_SOURCE_ITEM_VERSION or item_binding.get("item_type") != expected_binding["item_type"]:
        raise _error("TRADING_IMPACT_ITEM_BINDING_INVALID", "projection parent item binding is invalid")
    _text(item_binding.get("external_item_id"), field="projection.source_item_binding.external_item_id", maximum=200, allow_empty=True)
    _hash(item_binding.get("item_sha256"), field="projection.source_item_binding.item_sha256")
    if item_binding.get("server_fingerprint_version") != SOURCE_ITEM_FINGERPRINT_VERSION:
        raise _error("TRADING_IMPACT_ITEM_BINDING_INVALID", "projection parent fingerprint version is invalid")
    _hash(item_binding.get("server_fingerprint"), field="projection.source_item_binding.server_fingerprint")
    semantic = _validate_source_semantic_binding(
        item_binding.get("source_semantic_binding"),
        adapter_id=adapter_id,
    )
    expected_key = canonical_sha256(_projection_key_basis(projection))
    if _hash(projection.get("projection_key_sha256"), field="projection.projection_key_sha256") != expected_key:
        raise _error("TRADING_IMPACT_PROJECTION_KEY_INVALID", "projection key does not recompute")

    evaluation = projection.get("evaluation")
    if evaluation not in {"matched", "no_match"}:
        raise _error("TRADING_IMPACT_EVALUATION_INVALID", "projection evaluation is invalid")
    rule_ids = projection.get("matched_rule_ids")
    hypotheses = projection.get("hypotheses")
    if type(rule_ids) is not list or type(hypotheses) is not list:
        raise _error("TRADING_IMPACT_PROJECTION_TYPE_INVALID", "projection rules/hypotheses must be arrays")
    if evaluation == "no_match":
        if adapter_id != "company_ir" or semantic["rule_id"] != "" or rule_ids or hypotheses:
            raise _error("TRADING_IMPACT_NO_MATCH_INVALID", "only unmatched company IR may have a no-match projection")
    else:
        if len(rule_ids) != 1 or len(hypotheses) not in {1, 3}:
            raise _error("TRADING_IMPACT_MATCH_INVALID", "matched projection cardinality is invalid")
        if rule_ids != [semantic["rule_id"]] or semantic["rule_id"] not in _RULE_SPECS:
            raise _error("TRADING_IMPACT_RULE_INVALID", "matched rule does not match the parent semantic binding")
        rule_id = rule_ids[0]
        if adapter_id not in _RULE_SPECS[rule_id]["adapter_ids"]:
            raise _error("TRADING_IMPACT_RULE_INVALID", "matched rule does not belong to source adapter")
        expected_count = 3 if _RULE_SPECS[rule_id]["area_kind"] == "sector" else 1
        if len(hypotheses) != expected_count:
            raise _error("TRADING_IMPACT_MATCH_INVALID", "matched hypothesis count is invalid")
        seen_hashes: set[str] = set()
        area_ids: list[str] = []
        for index, hypothesis_value in enumerate(hypotheses):
            hypothesis = _exact_dict(hypothesis_value, _HYPOTHESIS_FIELDS, field=f"projection.hypotheses[{index}]")
            if hypothesis.get("version") != TRADING_IMPACT_HYPOTHESIS_VERSION or hypothesis.get("rule_id") != rule_id:
                raise _error("TRADING_IMPACT_HYPOTHESIS_INVALID", "hypothesis version/rule binding is invalid")
            supplied_hypothesis_hash = _hash(hypothesis.get("hypothesis_sha256"), field=f"projection.hypotheses[{index}].hypothesis_sha256")
            hypothesis_basis = {key: value for key, value in hypothesis.items() if key != "hypothesis_sha256"}
            if supplied_hypothesis_hash != canonical_sha256(hypothesis_basis) or supplied_hypothesis_hash in seen_hashes:
                raise _error("TRADING_IMPACT_HYPOTHESIS_HASH_INVALID", "hypothesis hash is invalid or duplicated")
            seen_hashes.add(supplied_hypothesis_hash)
            impact = _exact_dict(hypothesis.get("impact_hypothesis"), _IMPACT_HYPOTHESIS_FIELDS, field=f"projection.hypotheses[{index}].impact_hypothesis")
            statement = _text(impact.get("statement"), field=f"projection.hypotheses[{index}].statement", maximum=MAX_TRADING_IMPACT_STATEMENT_CHARS)
            area = _exact_dict(hypothesis.get("affected_area_binding"), _AREA_FIELDS, field=f"projection.hypotheses[{index}].affected_area_binding")
            area_kind = area.get("kind")
            area_id = area.get("id")
            if area_kind != _RULE_SPECS[rule_id]["area_kind"] or type(area_id) is not str:
                raise _error("TRADING_IMPACT_AREA_INVALID", "hypothesis affected area kind/id is invalid")
            security_ids = area.get("security_ids")
            if type(security_ids) is not list or not security_ids or len(security_ids) > 2 or any(type(symbol) is not str for symbol in security_ids):
                raise _error("TRADING_IMPACT_AREA_INVALID", "hypothesis security mapping is invalid")
            if area_kind == "security":
                if area_id != semantic["symbol"] or security_ids != [semantic["symbol"]]:
                    raise _error("TRADING_IMPACT_AREA_INVALID", "direct-security area binding is invalid")
            else:
                expected_sectors = dict(_SECTOR_SECURITY_MAP)
                if area_id not in expected_sectors or security_ids != list(expected_sectors[area_id]):
                    raise _error("TRADING_IMPACT_AREA_INVALID", "sector area binding is invalid")
            area_ids.append(area_id)
            if impact.get("affected_area") != f"{area_kind}:{area_id}" or impact.get("time_horizon") != _RULE_SPECS[rule_id]["horizon"]:
                raise _error("TRADING_IMPACT_HYPOTHESIS_INVALID", "compatible impact hypothesis fields drifted")
            indexes = impact.get("source_indexes")
            if indexes != [semantic["source_index"]]:
                raise _error("TRADING_IMPACT_SOURCE_INDEX_INVALID", "hypothesis evidence index does not match the parent")
            if type(impact.get("confidence")) is not float or impact.get("confidence") != 0.5:
                raise _error("TRADING_IMPACT_CONFIDENCE_INVALID", "hypothesis confidence must be exactly 0.5")
            if statement != _expected_statement(rule_id, semantic, area_id=area_id):
                raise _error("TRADING_IMPACT_STATEMENT_INVALID", "hypothesis statement does not match the parent semantic binding")
            if hypothesis.get("transmission_mechanism") != _RULE_SPECS[rule_id]["mechanism"]:
                raise _error("TRADING_IMPACT_MECHANISM_INVALID", "hypothesis mechanism is invalid")
            time_dimension = _exact_dict(hypothesis.get("time_dimension"), _TIME_FIELDS, field=f"projection.hypotheses[{index}].time_dimension")
            expected_time = {
                "horizon_id": impact.get("time_horizon"),
                "anchor_at": semantic["anchor_at"],
                "anchor_semantics": semantic["anchor_semantics"],
                "precision": semantic["precision"],
            }
            if time_dimension != expected_time:
                raise _error("TRADING_IMPACT_TIME_INVALID", "time dimension does not match the parent semantic binding")
            confidence = _exact_dict(hypothesis.get("confidence_basis"), _CONFIDENCE_FIELDS, field=f"projection.hypotheses[{index}].confidence_basis")
            if confidence != _CONFIDENCE_BASIS:
                raise _error("TRADING_IMPACT_CONFIDENCE_INVALID", "confidence semantics are invalid")
            counterevidence = _exact_dict(hypothesis.get("counterevidence"), _COUNTEREVIDENCE_FIELDS, field=f"projection.hypotheses[{index}].counterevidence")
            if counterevidence != _COUNTEREVIDENCE:
                raise _error("TRADING_IMPACT_COUNTEREVIDENCE_INVALID", "counterevidence must remain unknown")
        if expected_count == 3 and area_ids != [sector_id for sector_id, _security_ids in _SECTOR_SECURITY_MAP]:
            raise _error("TRADING_IMPACT_AREA_INVALID", "macro sector order is invalid")

    if projection.get("verification_state") != EXTERNAL_UNVERIFIED:
        raise _error("TRADING_IMPACT_TRUST_INVALID", "projection trust marker is invalid")
    interpretation = _exact_dict(projection.get("interpretation_boundary"), _INTERPRETATION_FIELDS, field="projection.interpretation_boundary")
    if interpretation != _INTERPRETATION_BOUNDARY:
        raise _error("TRADING_IMPACT_BOUNDARY_INVALID", "projection interpretation boundary is invalid")
    accounting = _exact_dict(projection.get("accounting"), _ACCOUNTING_FIELDS, field="projection.accounting")
    if accounting != _ACCOUNTING:
        raise _error("TRADING_IMPACT_ACCOUNTING_INVALID", "projection accounting is invalid")
    supplied_projection_hash = _hash(projection.get("projection_sha256"), field="projection.projection_sha256")
    projection_basis = {key: child for key, child in projection.items() if key != "projection_sha256"}
    if supplied_projection_hash != canonical_sha256(projection_basis):
        raise _error("TRADING_IMPACT_PROJECTION_HASH_INVALID", "projection hash does not recompute")
    return json.loads(_canonical_json(projection))


@dataclass(frozen=True, slots=True)
class TradingImpactProjection:
    """Deeply immutable canonical projection backed by a JSON string."""

    _canonical_value: str

    @classmethod
    def build(cls, value: Any) -> "TradingImpactProjection":
        normalized = _validate_projection(value)
        return cls(_canonical_json(normalized))

    def to_dict(self) -> dict[str, Any]:
        value = json.loads(self._canonical_value)
        if type(value) is not dict:  # pragma: no cover - constructor invariant
            raise RuntimeError("canonical trading-impact projection is not an object")
        return value


class TradingImpactRulesV1:
    """Closed seven-adapter, thirteen-rule deterministic impact engine."""

    __slots__ = ()

    ruleset_version = TRADING_IMPACT_RULESET_VERSION
    ruleset_sha256 = TRADING_IMPACT_RULESET_SHA256

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("TradingImpactRulesV1 is sealed and cannot be subclassed")

    @classmethod
    def manifest(cls) -> dict[str, Any]:
        if _canonical_size(_MANIFEST) > MAX_TRADING_IMPACT_MANIFEST_BYTES:
            raise _error("TRADING_IMPACT_MANIFEST_TOO_LARGE", "ruleset manifest exceeds its bound")
        if canonical_sha256(_MANIFEST) != TRADING_IMPACT_RULESET_SHA256:
            raise _error("TRADING_IMPACT_RULESET_DRIFT", "ruleset manifest hash drifted")
        return json.loads(_canonical_json(_MANIFEST))

    @classmethod
    def source_semantic_binding(
        cls,
        item: Any,
        *,
        item_sha256: Any,
        adapter_id: Any,
        source_class: Any,
        source_channel: Any,
    ) -> dict[str, Any]:
        parent = _validate_parent_item(item, item_sha256=item_sha256)
        clean_adapter = _slug(adapter_id, field="adapter_id")
        binding = _SOURCE_BINDINGS.get(clean_adapter)
        if binding is None:
            raise _error("TRADING_IMPACT_SOURCE_BINDING_INVALID", "adapter is outside the sealed set")
        if type(source_class) is not str or type(source_channel) is not str:
            raise _error("TRADING_IMPACT_SOURCE_BINDING_INVALID", "source class/channel must be native strings")
        if (
            source_class != binding["source_class"]
            or source_channel != binding["source_channel"]
            or parent["item_type"] != binding["item_type"]
        ):
            raise _error("TRADING_IMPACT_SOURCE_BINDING_INVALID", "adapter/class/channel/item binding is invalid")
        context = _source_context(parent, adapter_id=clean_adapter)
        return json.loads(
            _canonical_json(
                _source_semantic_binding(
                    adapter_id=clean_adapter,
                    item=parent,
                    context=context,
                )
            )
        )

    @classmethod
    def project_item(
        cls,
        item: Any,
        *,
        item_sha256: Any,
        adapter_id: Any,
        source_class: Any,
        source_channel: Any,
    ) -> TradingImpactProjection:
        parent = _validate_parent_item(item, item_sha256=item_sha256)
        clean_adapter = _slug(adapter_id, field="adapter_id")
        binding = _SOURCE_BINDINGS.get(clean_adapter)
        if binding is None:
            raise _error("TRADING_IMPACT_SOURCE_BINDING_INVALID", "adapter is outside the sealed set")
        if type(source_class) is not str or type(source_channel) is not str:
            raise _error("TRADING_IMPACT_SOURCE_BINDING_INVALID", "source class/channel must be native strings")
        if source_class != binding["source_class"] or source_channel != binding["source_channel"] or parent["item_type"] != binding["item_type"]:
            raise _error("TRADING_IMPACT_SOURCE_BINDING_INVALID", "adapter/class/channel/item binding is invalid")
        context = _source_context(parent, adapter_id=clean_adapter)
        rule_id = context["rule_id"]
        if rule_id is not None and clean_adapter not in _RULE_SPECS[rule_id]["adapter_ids"]:
            raise _error("TRADING_IMPACT_RULE_INVALID", "selected rule does not belong to adapter")

        hypotheses: list[dict[str, Any]] = []
        matched_rule_ids: list[str] = []
        if rule_id is not None:
            matched_rule_ids.append(rule_id)
            if _RULE_SPECS[rule_id]["area_kind"] == "security":
                hypotheses.append(_hypothesis(
                    rule_id=rule_id,
                    item=parent,
                    context=context,
                    area_kind="security",
                    area_id=context["symbol"],
                    security_ids=(context["symbol"],),
                ))
            else:
                for sector_id, security_ids in _SECTOR_SECURITY_MAP:
                    hypotheses.append(_hypothesis(
                        rule_id=rule_id,
                        item=parent,
                        context=context,
                        area_kind="sector",
                        area_id=sector_id,
                        security_ids=security_ids,
                    ))
        if len(matched_rule_ids) > MAX_TRADING_IMPACT_MATCHED_RULES or len(hypotheses) > MAX_TRADING_IMPACT_HYPOTHESES:
            raise _error("TRADING_IMPACT_CAPACITY_EXCEEDED", "projection exceeds sealed rule/hypothesis bounds")

        source_binding = {
            "adapter_id": clean_adapter,
            "source_class": source_class,
            "source_channel": source_channel,
        }
        source_item_binding = {
            "item_version": parent["version"],
            "external_item_id": parent["external_item_id"],
            "item_type": parent["item_type"],
            "item_sha256": item_sha256,
            "server_fingerprint_version": parent["server_fingerprint_version"],
            "server_fingerprint": parent["server_fingerprint"],
            "source_semantic_binding": _source_semantic_binding(
                adapter_id=clean_adapter,
                item=parent,
                context=context,
            ),
        }
        key_basis = {
            "version": TRADING_IMPACT_PROJECTION_KEY_VERSION,
            "ruleset_version": TRADING_IMPACT_RULESET_VERSION,
            "ruleset_sha256": TRADING_IMPACT_RULESET_SHA256,
            "mapping_version": TRADING_IMPACT_MAPPING_VERSION,
            "source_binding": source_binding,
            "source_item_sha256": item_sha256,
            "server_fingerprint_version": parent["server_fingerprint_version"],
            "server_fingerprint": parent["server_fingerprint"],
        }
        projection_basis = {
            "version": TRADING_IMPACT_PROJECTION_VERSION,
            "ruleset_version": TRADING_IMPACT_RULESET_VERSION,
            "ruleset_sha256": TRADING_IMPACT_RULESET_SHA256,
            "mapping_version": TRADING_IMPACT_MAPPING_VERSION,
            "projection_key_version": TRADING_IMPACT_PROJECTION_KEY_VERSION,
            "projection_key_sha256": canonical_sha256(key_basis),
            "source_binding": source_binding,
            "source_item_binding": source_item_binding,
            "evaluation": "matched" if matched_rule_ids else "no_match",
            "matched_rule_ids": matched_rule_ids,
            "hypotheses": hypotheses,
            "verification_state": EXTERNAL_UNVERIFIED,
            "interpretation_boundary": json.loads(_canonical_json(_INTERPRETATION_BOUNDARY)),
            "accounting": json.loads(_canonical_json(_ACCOUNTING)),
        }
        projection = {
            **projection_basis,
            "projection_sha256": canonical_sha256(projection_basis),
        }
        return TradingImpactProjection.build(projection)


__all__ = [
    "MAX_TRADING_IMPACT_HYPOTHESES",
    "TRADING_IMPACT_HYPOTHESIS_VERSION",
    "TRADING_IMPACT_MAPPING_VERSION",
    "TRADING_IMPACT_PROJECTION_KEY_VERSION",
    "TRADING_IMPACT_PROJECTION_VERSION",
    "TRADING_IMPACT_RULESET_VERSION",
    "TRADING_IMPACT_RULESET_SHA256",
    "TRADING_IMPACT_SOURCE_SEMANTICS_VERSION",
    "TradingImpactProjection",
    "TradingImpactRulesError",
    "TradingImpactRulesV1",
]
