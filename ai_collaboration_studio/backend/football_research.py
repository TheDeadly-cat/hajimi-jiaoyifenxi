from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit


FOOTBALL_RESEARCH_CAPABILITY_PACK_ID = "football_research_readonly"
FOOTBALL_RESEARCH_CONTRACT_VERSION = "football_research_contract_v1"
FOOTBALL_RESEARCH_SCHEMA_VERSION = "football_research_contract_schema_v1"
FOOTBALL_PROBABILITY_STATE = "withheld_no_calibration"

FOOTBALL_EVIDENCE_CLASSES = frozenset({
    "official_fact",
    "media_report",
    "model_inference",
    "odds_proxy",
})

FIXED_FOOTBALL_RESEARCH_BOUNDARIES: dict[str, Any] = {
    "probability_state": FOOTBALL_PROBABILITY_STATE,
    "future_probability_available": False,
    "probability_metrics_visible": False,
    "odds_are_proxy_only": True,
    "execution_capability": "none",
    "betting_allowed": False,
    "live_betting_allowed": False,
    "automatic_betting_allowed": False,
    "wallet_connection_allowed": False,
    "order_placement_allowed": False,
    "can_autonomously_decide": False,
    "can_replace_user_decision": False,
    "user_final_decision_required": True,
}

_ROOT_FIELDS_WITHOUT_HASH = {
    "version",
    "capability_pack_id",
    "match_identity",
    "data_cutoff_utc",
    "teams",
    "odds_proxies",
    *FIXED_FOOTBALL_RESEARCH_BOUNDARIES,
}
_ROOT_FIELDS = {*_ROOT_FIELDS_WITHOUT_HASH, "contract_sha256"}

_MATCH_IDENTITY_FIELDS = {
    "competition_id",
    "competition",
    "season",
    "match_id",
    "kickoff_utc",
    "venue_id",
    "venue",
    "home_team_id",
    "home_team_name",
    "away_team_id",
    "away_team_name",
}
_TEAM_FIELDS = {
    "team_id",
    "team_name",
    "match_role",
    "schedule_context",
    "availability",
    "tactical_context",
    "recent_performance",
}
_SCHEDULE_FIELDS = {
    "fixture_history",
    "fixtures_last_7d",
    "fixtures_last_14d",
    "rest_hours_before_kickoff",
    "travel",
    "home_away_sequence",
}
_AVAILABILITY_FIELDS = {"lineup", "injuries", "suspensions"}
_RECENT_PERFORMANCE_FIELDS = {
    "fixture_ids",
    "results_sequence",
    "performance_notes",
}
_EVIDENCE_FIELD_KEYS = {
    "claim_id",
    "value",
    "evidence_class",
    "as_of_utc",
    "source",
}
_SOURCE_BASE_KEYS = {
    "source_id",
    "publisher",
    "source_uri",
    "source_sha256",
    "material_binding",
    "publication",
    "retrieved_at_utc",
}
_MATERIAL_BINDING_KEYS = {
    "material_id",
    "material_version",
    "content_sha256",
    "snapshot_sha256",
}
_PUBLICATION_KEYS = {"state", "published_at_utc", "observed_at_utc"}
_INFERENCE_KEYS = {
    "method_id",
    "method_version",
    "generated_at_utc",
    "upstream_claim_ids",
}
_FIXTURE_KEYS = {"match_id", "kickoff_utc", "venue", "role"}
_VENUE_KEYS = {"venue_id", "venue_name"}
_WINDOW_KEYS = {"window_start_utc", "window_end_utc", "fixture_ids", "count"}
_TRAVEL_KEYS = {"origin", "destination", "distance_km", "method"}
_MATCH_ROLE_REF_KEYS = {"match_id", "role"}
_RESULT_KEYS = {"match_id", "result"}
_PERFORMANCE_NOTE_KEYS = {"match_id", "note"}
_LINEUP_VALUE_KEYS = {"publication_state", "players"}
_LINEUP_PLAYER_KEYS = {"player_id", "player_name", "position", "selection_status"}
_AVAILABILITY_VALUE_KEYS = {"publication_state", "entries"}
_AVAILABILITY_ENTRY_KEYS = {"player_id", "player_name", "status", "detail"}
_ODDS_PROXY_VALUE_KEYS = {"market", "selection", "decimal_odds"}

_OFFICIAL_ONLY = frozenset({"official_fact"})
_SCHEDULE_EVIDENCE = frozenset({"official_fact", "model_inference"})
_TRAVEL_EVIDENCE = frozenset({
    "official_fact",
    "media_report",
    "model_inference",
})
_RESEARCH_EVIDENCE = _TRAVEL_EVIDENCE
_ODDS_EVIDENCE = frozenset({"odds_proxy"})

_UTC_PATTERN = re.compile(
    r"(?:19|20|21)[0-9]{2}-(?:0[1-9]|1[0-2])-"
    r"(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z"
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_MATERIAL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

_ALLOWED_PROBABILITY_KEYS = {
    "probability_state",
    "future_probability_available",
    "probability_metrics_visible",
}
_FORBIDDEN_KEY_FRAGMENTS = (
    "probability",
    "probabilities",
    "confidence",
    "brier",
    "logloss",
    "log_loss",
    "calibration",
    "win_rate",
    "winrate",
    "hit_rate",
    "hitrate",
    "likelihood",
)
_FORECAST_OUTCOME_PERCENT = re.compile(
    r"(?:\b(?:home|away|draw|win|winner|lose|loss)\b|主胜|客胜|平局|胜率)"
    r"[^\n.!?。！？]{0,40}?\b\d+(?:\.\d+)?\s*%",
    re.IGNORECASE,
)
_FORECAST_PERCENT_OUTCOME = re.compile(
    r"\b\d+(?:\.\d+)?\s*%[^\n.!?。！？]{0,40}?"
    r"(?:\b(?:home|away|draw|win|winner|lose|loss)\b|主胜|客胜|平局|胜率)",
    re.IGNORECASE,
)
_FORECAST_NUMBER_TERM = re.compile(
    r"(?:\b(?:probabilit(?:y|ies)|likelihood|confidence)\b|概率|胜率|置信)"
    r"[^\n.!?。！？]{0,30}?\b\d+(?:\.\d+)?\s*%?",
    re.IGNORECASE,
)
_METRIC_NUMBER = re.compile(
    r"(?:\b(?:brier|log[ _-]?loss|calibration)\b|布里尔|对数损失|校准)"
    r"[^\n.!?。！？]{0,30}?\b\d+(?:\.\d+)?",
    re.IGNORECASE,
)
_QUALITATIVE_FORECAST = re.compile(
    r"(?:\b(?:home|away|draw|win|winner)\b|主胜|客胜|平局)"
    r"[^\n.!?。！？]{0,30}"
    r"(?:\b(?:probabilit(?:y|ies)|likelihood|confidence)\b|概率|胜率|置信)",
    re.IGNORECASE,
)
_QUALITATIVE_CONFIDENCE = re.compile(
    r"(?:\b(?:model|forecast|prediction)\b[^\n.!?。！？]{0,30}\bconfidence\b|"
    r"\bconfidence\b[^\n.!?。！？]{0,20}\b(?:high|medium|low|strong|weak)\b|"
    r"模型[^\n。！？]{0,20}置信)",
    re.IGNORECASE,
)
_SAFE_WITHHOLDING = re.compile(
    r"(?:\b(?:no|not|without|withheld|unavailable|absent)\b|"
    r"not\s+(?:available|generated|calculated)|未生成|不提供|不可用|已隐藏|无校准)",
    re.IGNORECASE,
)


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON for hashing and persisted receipts."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


FOOTBALL_RESEARCH_OUTPUT_SCHEMA: dict[str, Any] = {
    "version": FOOTBALL_RESEARCH_SCHEMA_VERSION,
    "type": "object",
    "required": sorted(_ROOT_FIELDS),
    "fields": {
        "version": FOOTBALL_RESEARCH_CONTRACT_VERSION,
        "capability_pack_id": FOOTBALL_RESEARCH_CAPABILITY_PACK_ID,
        "match_identity": "football_match_identity_v1",
        "data_cutoff_utc": "canonical_utc_timestamp",
        "teams": "football_home_away_context_v1",
        "odds_proxies": "array<football_odds_proxy_field_v1>",
        "probability_state": FOOTBALL_PROBABILITY_STATE,
        "future_probability_available": "boolean:false",
        "probability_metrics_visible": "boolean:false",
        "odds_are_proxy_only": "boolean:true",
        "execution_capability": "none",
        "betting_allowed": "boolean:false",
        "live_betting_allowed": "boolean:false",
        "automatic_betting_allowed": "boolean:false",
        "wallet_connection_allowed": "boolean:false",
        "order_placement_allowed": "boolean:false",
        "can_autonomously_decide": "boolean:false",
        "can_replace_user_decision": "boolean:false",
        "user_final_decision_required": "boolean:true",
        "contract_sha256": "sha256",
    },
    "additional_properties": False,
}
FOOTBALL_RESEARCH_OUTPUT_SCHEMA_SHA256 = canonical_sha256(
    FOOTBALL_RESEARCH_OUTPUT_SCHEMA
)


def _closed_definition(required: set[str], **extra: Any) -> dict[str, Any]:
    return {
        "required": sorted(required),
        "additional_properties": False,
        **extra,
    }


# A serializable descriptor is exported for host registries. The executable
# validator below is authoritative and also checks cross-object references.
FOOTBALL_RESEARCH_CONTRACT_SCHEMA: dict[str, Any] = {
    "version": FOOTBALL_RESEARCH_SCHEMA_VERSION,
    "root": copy.deepcopy(FOOTBALL_RESEARCH_OUTPUT_SCHEMA),
    "definitions": {
        "football_match_identity_v1": _closed_definition(_MATCH_IDENTITY_FIELDS),
        "football_team_context_v1": _closed_definition(_TEAM_FIELDS),
        "football_schedule_context_v1": _closed_definition(_SCHEDULE_FIELDS),
        "football_availability_v1": _closed_definition(_AVAILABILITY_FIELDS),
        "football_recent_performance_v1": _closed_definition(
            _RECENT_PERFORMANCE_FIELDS
        ),
        "football_evidence_field_v1": _closed_definition(
            _EVIDENCE_FIELD_KEYS,
            evidence_class=sorted(FOOTBALL_EVIDENCE_CLASSES),
        ),
        "football_evidence_source_v1": _closed_definition(
            _SOURCE_BASE_KEYS,
            discriminator="evidence_class",
            model_inference_additional_required=["inference"],
        ),
        "football_material_binding_v1": _closed_definition(
            _MATERIAL_BINDING_KEYS
        ),
        "football_publication_v1": _closed_definition(
            _PUBLICATION_KEYS,
            states=["published", "not_published", "observed"],
        ),
        "football_model_inference_v1": _closed_definition(_INFERENCE_KEYS),
        "football_fixture_v1": _closed_definition(_FIXTURE_KEYS),
        "football_venue_v1": _closed_definition(_VENUE_KEYS),
        "football_density_window_v1": _closed_definition(_WINDOW_KEYS),
        "football_travel_v1": _closed_definition(_TRAVEL_KEYS),
        "football_match_role_reference_v1": _closed_definition(
            _MATCH_ROLE_REF_KEYS
        ),
        "football_result_reference_v1": _closed_definition(_RESULT_KEYS),
        "football_performance_note_v1": _closed_definition(
            _PERFORMANCE_NOTE_KEYS
        ),
        "football_lineup_value_v1": _closed_definition(_LINEUP_VALUE_KEYS),
        "football_lineup_player_v1": _closed_definition(_LINEUP_PLAYER_KEYS),
        "football_availability_value_v1": _closed_definition(
            _AVAILABILITY_VALUE_KEYS
        ),
        "football_availability_entry_v1": _closed_definition(
            _AVAILABILITY_ENTRY_KEYS
        ),
        "football_odds_proxy_value_v1": _closed_definition(
            _ODDS_PROXY_VALUE_KEYS
        ),
    },
}
FOOTBALL_RESEARCH_SCHEMA_SHA256 = canonical_sha256(
    FOOTBALL_RESEARCH_CONTRACT_SCHEMA
)


class FootballResearchContractError(ValueError):
    """Raised when a football read-only contract fails closed."""


def build_football_research_contract(payload: Any) -> dict[str, Any]:
    """Validate and hash one pre-kickoff, read-only football dossier."""

    if not isinstance(payload, dict):
        raise FootballResearchContractError("football research payload must be an object")
    contract = copy.deepcopy(payload)
    if "contract_sha256" in contract:
        raise FootballResearchContractError(
            "contract_sha256 is host generated and must not be supplied"
        )
    _install_fixed_field(contract, "version", FOOTBALL_RESEARCH_CONTRACT_VERSION)
    _install_fixed_field(
        contract,
        "capability_pack_id",
        FOOTBALL_RESEARCH_CAPABILITY_PACK_ID,
    )
    for field, expected in FIXED_FOOTBALL_RESEARCH_BOUNDARIES.items():
        _install_fixed_field(contract, field, expected)
    _validate_contract_body(contract)
    contract["contract_sha256"] = canonical_sha256(contract)
    return contract


def validate_football_research_contract(value: Any) -> dict[str, Any]:
    """Validate a fully sealed contract and return an isolated copy."""

    if not isinstance(value, dict):
        raise FootballResearchContractError("football research contract must be an object")
    contract = copy.deepcopy(value)
    _require_exact_keys(contract, _ROOT_FIELDS, "contract")
    stored_sha256 = contract.pop("contract_sha256")
    if not isinstance(stored_sha256, str) or not _SHA256_PATTERN.fullmatch(
        stored_sha256
    ):
        raise FootballResearchContractError(
            "contract.contract_sha256 must be lowercase sha256"
        )
    _validate_contract_body(contract)
    if canonical_sha256(contract) != stored_sha256:
        raise FootballResearchContractError("football research contract sha256 mismatch")
    contract["contract_sha256"] = stored_sha256
    return contract


def verify_football_research_contract(value: Any) -> dict[str, Any]:
    return validate_football_research_contract(value)


def _install_fixed_field(target: dict[str, Any], field: str, expected: Any) -> None:
    if field in target and (
        type(target[field]) is not type(expected) or target[field] != expected
    ):
        raise FootballResearchContractError(f"contract.{field} is fixed")
    target[field] = copy.deepcopy(expected)


def _validate_contract_body(contract: dict[str, Any]) -> None:
    _reject_forbidden_metric_keys(contract, "contract")
    _require_exact_keys(contract, _ROOT_FIELDS_WITHOUT_HASH, "contract")
    _require_fixed_value(
        contract.get("version"),
        FOOTBALL_RESEARCH_CONTRACT_VERSION,
        "contract.version",
    )
    _require_fixed_value(
        contract.get("capability_pack_id"),
        FOOTBALL_RESEARCH_CAPABILITY_PACK_ID,
        "contract.capability_pack_id",
    )
    for field, expected in FIXED_FOOTBALL_RESEARCH_BOUNDARIES.items():
        _require_fixed_value(contract.get(field), expected, f"contract.{field}")

    cutoff = _require_utc(contract.get("data_cutoff_utc"), "contract.data_cutoff_utc")
    context: dict[str, dict[str, Any]] = {}
    identity = _require_object(contract.get("match_identity"), "contract.match_identity")
    _require_exact_keys(identity, _MATCH_IDENTITY_FIELDS, "contract.match_identity")
    identity_values = _validate_match_identity(identity, cutoff, context)
    kickoff = _parse_utc_value(
        identity_values["kickoff_utc"],
        "contract.match_identity.kickoff_utc.value",
    )
    if cutoff >= kickoff:
        raise FootballResearchContractError(
            "contract.data_cutoff_utc must be before match kickoff_utc"
        )

    teams = _require_object(contract.get("teams"), "contract.teams")
    _require_exact_keys(teams, {"home", "away"}, "contract.teams")
    target = {
        "match_id": identity_values["match_id"],
        "kickoff": kickoff,
        "venue_id": identity_values["venue_id"],
        "venue_name": identity_values["venue"],
    }
    home = _validate_team(
        teams.get("home"),
        path="contract.teams.home",
        expected_role="home",
        cutoff=cutoff,
        target=target,
        context=context,
    )
    away = _validate_team(
        teams.get("away"),
        path="contract.teams.away",
        expected_role="away",
        cutoff=cutoff,
        target=target,
        context=context,
    )
    for prefix, team in (("home", home), ("away", away)):
        if identity_values[f"{prefix}_team_id"] != team["team_id"]:
            raise FootballResearchContractError(
                f"sealed {prefix} team_id does not match team context"
            )
        if identity_values[f"{prefix}_team_name"] != team["team_name"]:
            raise FootballResearchContractError(
                f"sealed {prefix} team_name does not match team context"
            )
    if home["team_id"] == away["team_id"]:
        raise FootballResearchContractError("home and away teams must be distinct")

    _validate_odds_proxies(contract.get("odds_proxies"), cutoff, context)
    _validate_inference_graph(context)


def _validate_match_identity(
    identity: dict[str, Any],
    cutoff: datetime,
    context: dict[str, dict[str, Any]],
) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in (
        "competition_id",
        "competition",
        "season",
        "match_id",
        "venue_id",
        "venue",
        "home_team_id",
        "home_team_name",
        "away_team_id",
        "away_team_name",
    ):
        evidence = _validate_evidence_field(
            identity.get(field),
            path=f"contract.match_identity.{field}",
            cutoff=cutoff,
            allowed_classes=_OFFICIAL_ONLY,
            value_validator=(
                _require_identifier
                if field == "competition_id"
                else lambda value, value_path, field=field: _require_text(
                    value,
                    value_path,
                    maximum=300 if field == "venue" else 160,
                )
            ),
            context=context,
        )
        values[field] = evidence["value"]

    kickoff = _validate_evidence_field(
        identity.get("kickoff_utc"),
        path="contract.match_identity.kickoff_utc",
        cutoff=cutoff,
        allowed_classes=_OFFICIAL_ONLY,
        value_validator=lambda value, value_path: (
            _require_utc(value, value_path),
            value,
        )[1],
        context=context,
    )
    values["kickoff_utc"] = kickoff["value"]
    return values


def _validate_team(
    raw: Any,
    *,
    path: str,
    expected_role: str,
    cutoff: datetime,
    target: dict[str, Any],
    context: dict[str, dict[str, Any]],
) -> dict[str, str]:
    team = _require_object(raw, path)
    _require_exact_keys(team, _TEAM_FIELDS, path)
    _require_fixed_value(team.get("match_role"), expected_role, f"{path}.match_role")

    identity_values: dict[str, str] = {}
    for field in ("team_id", "team_name"):
        evidence = _validate_evidence_field(
            team.get(field),
            path=f"{path}.{field}",
            cutoff=cutoff,
            allowed_classes=_OFFICIAL_ONLY,
            value_validator=lambda value, value_path: _require_text(
                value,
                value_path,
                maximum=160,
            ),
            context=context,
        )
        identity_values[field] = evidence["value"]

    fixture_ids = _validate_schedule(
        team.get("schedule_context"),
        path=f"{path}.schedule_context",
        cutoff=cutoff,
        target=target,
        context=context,
    )
    _validate_availability(
        team.get("availability"),
        path=f"{path}.availability",
        cutoff=cutoff,
        context=context,
    )
    _validate_evidence_field(
        team.get("tactical_context"),
        path=f"{path}.tactical_context",
        cutoff=cutoff,
        allowed_classes=_RESEARCH_EVIDENCE,
        value_validator=lambda value, value_path: _require_research_text_list(
            value,
            value_path,
            maximum_items=20,
            maximum_text=1000,
        ),
        context=context,
    )
    _validate_recent_performance(
        team.get("recent_performance"),
        path=f"{path}.recent_performance",
        cutoff=cutoff,
        fixture_ids=fixture_ids,
        context=context,
    )
    return identity_values


def _validate_schedule(
    raw: Any,
    *,
    path: str,
    cutoff: datetime,
    target: dict[str, Any],
    context: dict[str, dict[str, Any]],
) -> list[str]:
    schedule = _require_object(raw, path)
    _require_exact_keys(schedule, _SCHEDULE_FIELDS, path)
    history_field = _validate_evidence_field(
        schedule.get("fixture_history"),
        path=f"{path}.fixture_history",
        cutoff=cutoff,
        allowed_classes=_OFFICIAL_ONLY,
        value_validator=lambda value, value_path: _validate_fixture_history(
            value,
            value_path,
            cutoff=cutoff,
            target=target,
        ),
        context=context,
    )
    fixtures = history_field["value"]
    fixture_ids = [fixture["match_id"] for fixture in fixtures]
    fixture_by_id = {fixture["match_id"]: fixture for fixture in fixtures}

    for field, days in (("fixtures_last_7d", 7), ("fixtures_last_14d", 14)):
        window_field = _validate_evidence_field(
            schedule.get(field),
            path=f"{path}.{field}",
            cutoff=cutoff,
            allowed_classes=_SCHEDULE_EVIDENCE,
            value_validator=lambda value, value_path, days=days: _validate_density_window(
                value,
                value_path,
                cutoff=cutoff,
                days=days,
                fixtures=fixtures,
            ),
            context=context,
        )
        if target["match_id"] in window_field["value"]["fixture_ids"]:
            raise FootballResearchContractError(
                f"{path}.{field}.value must exclude the target match"
            )

    rest = _validate_evidence_field(
        schedule.get("rest_hours_before_kickoff"),
        path=f"{path}.rest_hours_before_kickoff",
        cutoff=cutoff,
        allowed_classes=_SCHEDULE_EVIDENCE,
        value_validator=lambda value, value_path: _require_number(
            value,
            value_path,
            minimum=0.0,
            maximum=2_000.0,
        ),
        context=context,
    )["value"]
    if not fixtures:
        raise FootballResearchContractError(f"{path}.fixture_history.value must not be empty")
    last_kickoff = _parse_utc_value(
        fixtures[-1]["kickoff_utc"],
        f"{path}.fixture_history.value[-1].kickoff_utc",
    )
    expected_rest = (target["kickoff"] - last_kickoff).total_seconds() / 3600.0
    if abs(float(rest) - expected_rest) > 1e-6:
        raise FootballResearchContractError(
            f"{path}.rest_hours_before_kickoff.value must equal the target-to-latest-fixture interval"
        )

    travel = _validate_evidence_field(
        schedule.get("travel"),
        path=f"{path}.travel",
        cutoff=cutoff,
        allowed_classes=_TRAVEL_EVIDENCE,
        value_validator=_validate_travel,
        context=context,
    )["value"]
    destination = travel["destination"]
    if (
        destination["venue_id"] != target["venue_id"]
        or destination["venue_name"] != target["venue_name"]
    ):
        raise FootballResearchContractError(
            f"{path}.travel.value.destination must equal the target venue"
        )

    sequence = _validate_evidence_field(
        schedule.get("home_away_sequence"),
        path=f"{path}.home_away_sequence",
        cutoff=cutoff,
        allowed_classes=_OFFICIAL_ONLY,
        value_validator=_validate_home_away_sequence,
        context=context,
    )["value"]
    expected_sequence = [
        {"match_id": fixture_id, "role": fixture_by_id[fixture_id]["role"]}
        for fixture_id in fixture_ids
    ]
    if sequence != expected_sequence:
        raise FootballResearchContractError(
            f"{path}.home_away_sequence.value must exactly reuse fixture_history IDs and roles"
        )
    return fixture_ids


def _validate_fixture_history(
    value: Any,
    path: str,
    *,
    cutoff: datetime,
    target: dict[str, Any],
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 40:
        raise FootballResearchContractError(f"{path} must contain 1 through 40 fixtures")
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_kickoff: datetime | None = None
    for index, raw_fixture in enumerate(value):
        fixture_path = f"{path}[{index}]"
        fixture = _require_object(raw_fixture, fixture_path)
        _require_exact_keys(fixture, _FIXTURE_KEYS, fixture_path)
        match_id = _require_text(fixture.get("match_id"), f"{fixture_path}.match_id", maximum=160)
        if match_id == target["match_id"]:
            raise FootballResearchContractError(f"{fixture_path}.match_id must exclude the target match")
        if match_id in seen:
            raise FootballResearchContractError(f"{path} match_id values must be unique")
        seen.add(match_id)
        kickoff = _require_utc(fixture.get("kickoff_utc"), f"{fixture_path}.kickoff_utc")
        if kickoff > cutoff or kickoff >= target["kickoff"]:
            raise FootballResearchContractError(f"{fixture_path}.kickoff_utc must be historical at the data cutoff")
        if previous_kickoff is not None and kickoff <= previous_kickoff:
            raise FootballResearchContractError(f"{path} must be in strictly increasing kickoff order")
        previous_kickoff = kickoff
        _validate_venue(fixture.get("venue"), f"{fixture_path}.venue")
        if fixture.get("role") not in {"home", "away", "neutral"}:
            raise FootballResearchContractError(f"{fixture_path}.role must be home, away, or neutral")
        clean.append(fixture)
    return clean


def _validate_density_window(
    value: Any,
    path: str,
    *,
    cutoff: datetime,
    days: int,
    fixtures: list[dict[str, Any]],
) -> dict[str, Any]:
    window = _require_object(value, path)
    _require_exact_keys(window, _WINDOW_KEYS, path)
    start = _require_utc(window.get("window_start_utc"), f"{path}.window_start_utc")
    end = _require_utc(window.get("window_end_utc"), f"{path}.window_end_utc")
    if end != cutoff or start != cutoff - timedelta(days=days):
        raise FootballResearchContractError(
            f"{path} must use the exact {days}-day interval ending at data_cutoff_utc"
        )
    ids = _validate_identifier_list(
        window.get("fixture_ids"),
        f"{path}.fixture_ids",
        maximum_items=40,
    )
    expected = [
        fixture["match_id"]
        for fixture in fixtures
        if start <= _parse_utc_value(fixture["kickoff_utc"], path) <= end
    ]
    if ids != expected:
        raise FootballResearchContractError(
            f"{path}.fixture_ids must exactly match fixture_history inside the window"
        )
    count = _require_integer(window.get("count"), f"{path}.count", minimum=0, maximum=40)
    if count != len(ids):
        raise FootballResearchContractError(f"{path}.count must equal fixture_ids length")
    return window


def _validate_venue(value: Any, path: str) -> dict[str, str]:
    venue = _require_object(value, path)
    _require_exact_keys(venue, _VENUE_KEYS, path)
    _require_text(venue.get("venue_id"), f"{path}.venue_id", maximum=160)
    _require_text(venue.get("venue_name"), f"{path}.venue_name", maximum=300)
    return venue


def _validate_travel(value: Any, path: str) -> dict[str, Any]:
    travel = _require_object(value, path)
    _require_exact_keys(travel, _TRAVEL_KEYS, path)
    _validate_venue(travel.get("origin"), f"{path}.origin")
    _validate_venue(travel.get("destination"), f"{path}.destination")
    distance = _require_number(
        travel.get("distance_km"),
        f"{path}.distance_km",
        minimum=0.0,
        maximum=50_000.0,
    )
    method = travel.get("method")
    if method not in {
        "geodesic_haversine",
        "route_estimate",
        "provider_reported",
        "not_applicable",
    }:
        raise FootballResearchContractError(
            f"{path}.method is not an allowed travel calculation method"
        )
    if method == "not_applicable" and float(distance) != 0.0:
        raise FootballResearchContractError(
            f"{path}.distance_km must be zero when method is not_applicable"
        )
    return travel


def _validate_home_away_sequence(value: Any, path: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 40:
        raise FootballResearchContractError(f"{path} must be an array with at most 40 items")
    clean: list[dict[str, str]] = []
    for index, raw_item in enumerate(value):
        item_path = f"{path}[{index}]"
        item = _require_object(raw_item, item_path)
        _require_exact_keys(item, _MATCH_ROLE_REF_KEYS, item_path)
        _require_text(item.get("match_id"), f"{item_path}.match_id", maximum=160)
        if item.get("role") not in {"home", "away", "neutral"}:
            raise FootballResearchContractError(f"{item_path}.role must be home, away, or neutral")
        clean.append(item)
    return clean


def _validate_availability(
    raw: Any,
    *,
    path: str,
    cutoff: datetime,
    context: dict[str, dict[str, Any]],
) -> None:
    availability = _require_object(raw, path)
    _require_exact_keys(availability, _AVAILABILITY_FIELDS, path)
    lineup = _validate_evidence_field(
        availability.get("lineup"),
        path=f"{path}.lineup",
        cutoff=cutoff,
        allowed_classes=_RESEARCH_EVIDENCE,
        value_validator=_validate_lineup_value,
        context=context,
    )
    injuries = _validate_evidence_field(
        availability.get("injuries"),
        path=f"{path}.injuries",
        cutoff=cutoff,
        allowed_classes=_RESEARCH_EVIDENCE,
        value_validator=lambda value, value_path: _validate_availability_value(
            value,
            value_path,
            allowed_statuses={"out", "doubtful", "questionable", "returning", "unknown"},
        ),
        context=context,
    )
    suspensions = _validate_evidence_field(
        availability.get("suspensions"),
        path=f"{path}.suspensions",
        cutoff=cutoff,
        allowed_classes=_RESEARCH_EVIDENCE,
        value_validator=lambda value, value_path: _validate_availability_value(
            value,
            value_path,
            allowed_statuses={"suspended", "pending", "appealed", "served", "unknown"},
        ),
        context=context,
    )
    for field_name, evidence in (
        ("lineup", lineup),
        ("injuries", injuries),
        ("suspensions", suspensions),
    ):
        expected = evidence["value"]["publication_state"]
        actual = evidence["source"]["publication"]["state"]
        if expected != actual:
            raise FootballResearchContractError(
                f"{path}.{field_name} value/source publication states must match"
            )


def _validate_lineup_value(value: Any, path: str) -> dict[str, Any]:
    lineup = _require_object(value, path)
    _require_exact_keys(lineup, _LINEUP_VALUE_KEYS, path)
    state = lineup.get("publication_state")
    if state not in {"published", "not_published"}:
        raise FootballResearchContractError(
            f"{path}.publication_state must be published or not_published"
        )
    players = lineup.get("players")
    if not isinstance(players, list) or len(players) > 100:
        raise FootballResearchContractError(f"{path}.players must contain at most 100 items")
    if state == "not_published" and players:
        raise FootballResearchContractError(f"{path}.players must be empty when not_published")
    seen: set[str] = set()
    for index, raw_player in enumerate(players):
        player_path = f"{path}.players[{index}]"
        player = _require_object(raw_player, player_path)
        _require_exact_keys(player, _LINEUP_PLAYER_KEYS, player_path)
        player_id = _require_text(player.get("player_id"), f"{player_path}.player_id", maximum=160)
        if player_id in seen:
            raise FootballResearchContractError(f"{path}.players player_id values must be unique")
        seen.add(player_id)
        _require_text(player.get("player_name"), f"{player_path}.player_name", maximum=200)
        _require_text(player.get("position"), f"{player_path}.position", maximum=80)
        if player.get("selection_status") not in {"starting", "substitute", "omitted"}:
            raise FootballResearchContractError(f"{player_path}.selection_status is invalid")
    return lineup


def _validate_availability_value(
    value: Any,
    path: str,
    *,
    allowed_statuses: set[str],
) -> dict[str, Any]:
    availability = _require_object(value, path)
    _require_exact_keys(availability, _AVAILABILITY_VALUE_KEYS, path)
    state = availability.get("publication_state")
    if state not in {"published", "not_published"}:
        raise FootballResearchContractError(
            f"{path}.publication_state must be published or not_published"
        )
    entries = availability.get("entries")
    if not isinstance(entries, list) or len(entries) > 100:
        raise FootballResearchContractError(f"{path}.entries must contain at most 100 items")
    if state == "not_published" and entries:
        raise FootballResearchContractError(f"{path}.entries must be empty when not_published")
    seen: set[str] = set()
    for index, raw_entry in enumerate(entries):
        entry_path = f"{path}.entries[{index}]"
        entry = _require_object(raw_entry, entry_path)
        _require_exact_keys(entry, _AVAILABILITY_ENTRY_KEYS, entry_path)
        player_id = _require_text(entry.get("player_id"), f"{entry_path}.player_id", maximum=160)
        if player_id in seen:
            raise FootballResearchContractError(f"{path}.entries player_id values must be unique")
        seen.add(player_id)
        _require_text(entry.get("player_name"), f"{entry_path}.player_name", maximum=200)
        if entry.get("status") not in allowed_statuses:
            raise FootballResearchContractError(f"{entry_path}.status is invalid")
        _require_research_text(entry.get("detail"), f"{entry_path}.detail", maximum=1000)
    return availability


def _validate_recent_performance(
    raw: Any,
    *,
    path: str,
    cutoff: datetime,
    fixture_ids: list[str],
    context: dict[str, dict[str, Any]],
) -> None:
    recent = _require_object(raw, path)
    _require_exact_keys(recent, _RECENT_PERFORMANCE_FIELDS, path)
    ids = _validate_evidence_field(
        recent.get("fixture_ids"),
        path=f"{path}.fixture_ids",
        cutoff=cutoff,
        allowed_classes=_OFFICIAL_ONLY,
        value_validator=lambda value, value_path: _validate_identifier_list(
            value,
            value_path,
            maximum_items=20,
        ),
        context=context,
    )["value"]
    if not ids or ids != fixture_ids[-len(ids):]:
        raise FootballResearchContractError(
            f"{path}.fixture_ids.value must be a non-empty recent suffix of fixture_history"
        )

    results = _validate_evidence_field(
        recent.get("results_sequence"),
        path=f"{path}.results_sequence",
        cutoff=cutoff,
        allowed_classes=_OFFICIAL_ONLY,
        value_validator=_validate_result_sequence,
        context=context,
    )["value"]
    if [item["match_id"] for item in results] != ids:
        raise FootballResearchContractError(
            f"{path}.results_sequence.value must exactly reuse fixture_ids"
        )

    notes = _validate_evidence_field(
        recent.get("performance_notes"),
        path=f"{path}.performance_notes",
        cutoff=cutoff,
        allowed_classes=_RESEARCH_EVIDENCE,
        value_validator=_validate_performance_notes,
        context=context,
    )["value"]
    if [item["match_id"] for item in notes] != ids:
        raise FootballResearchContractError(
            f"{path}.performance_notes.value must exactly reuse fixture_ids"
        )


def _validate_result_sequence(value: Any, path: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 20:
        raise FootballResearchContractError(f"{path} must be an array with at most 20 items")
    clean: list[dict[str, str]] = []
    for index, raw_item in enumerate(value):
        item_path = f"{path}[{index}]"
        item = _require_object(raw_item, item_path)
        _require_exact_keys(item, _RESULT_KEYS, item_path)
        _require_text(item.get("match_id"), f"{item_path}.match_id", maximum=160)
        if item.get("result") not in {"W", "D", "L"}:
            raise FootballResearchContractError(f"{item_path}.result must be W, D, or L")
        clean.append(item)
    return clean


def _validate_performance_notes(value: Any, path: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > 20:
        raise FootballResearchContractError(f"{path} must be an array with at most 20 items")
    clean: list[dict[str, str]] = []
    for index, raw_item in enumerate(value):
        item_path = f"{path}[{index}]"
        item = _require_object(raw_item, item_path)
        _require_exact_keys(item, _PERFORMANCE_NOTE_KEYS, item_path)
        _require_text(item.get("match_id"), f"{item_path}.match_id", maximum=160)
        _require_research_text(item.get("note"), f"{item_path}.note", maximum=1000)
        clean.append(item)
    return clean


def _validate_odds_proxies(
    value: Any,
    cutoff: datetime,
    context: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(value, list) or len(value) > 100:
        raise FootballResearchContractError(
            "contract.odds_proxies must be an array with at most 100 items"
        )
    seen: set[tuple[str, str]] = set()
    for index, raw_field in enumerate(value):
        path = f"contract.odds_proxies[{index}]"
        evidence = _validate_evidence_field(
            raw_field,
            path=path,
            cutoff=cutoff,
            allowed_classes=_ODDS_EVIDENCE,
            value_validator=_validate_odds_proxy_value,
            context=context,
        )
        key = (evidence["value"]["market"], evidence["value"]["selection"])
        if key in seen:
            raise FootballResearchContractError(
                "contract.odds_proxies market/selection pairs must be unique"
            )
        seen.add(key)


def _validate_odds_proxy_value(value: Any, path: str) -> dict[str, Any]:
    proxy = _require_object(value, path)
    _require_exact_keys(proxy, _ODDS_PROXY_VALUE_KEYS, path)
    _require_text(proxy.get("market"), f"{path}.market", maximum=160)
    _require_text(proxy.get("selection"), f"{path}.selection", maximum=160)
    _require_number(proxy.get("decimal_odds"), f"{path}.decimal_odds", minimum=1.000001, maximum=1_000_000.0)
    return proxy


def _validate_evidence_field(
    raw: Any,
    *,
    path: str,
    cutoff: datetime,
    allowed_classes: frozenset[str],
    value_validator: Callable[[Any, str], Any],
    context: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    field = _require_object(raw, path)
    _require_exact_keys(field, _EVIDENCE_FIELD_KEYS, path)
    claim_id = _require_identifier(field.get("claim_id"), f"{path}.claim_id")
    if claim_id in context:
        raise FootballResearchContractError(
            f"{path}.claim_id must be globally unique; duplicate {claim_id}"
        )
    evidence_class = field.get("evidence_class")
    if evidence_class not in FOOTBALL_EVIDENCE_CLASSES:
        raise FootballResearchContractError(
            f"{path}.evidence_class must be one of {sorted(FOOTBALL_EVIDENCE_CLASSES)}"
        )
    if evidence_class not in allowed_classes:
        raise FootballResearchContractError(
            f"{path}.evidence_class {evidence_class!r} is not allowed for this field"
        )
    as_of = _require_utc(field.get("as_of_utc"), f"{path}.as_of_utc")
    if as_of > cutoff:
        raise FootballResearchContractError(f"{path}.as_of_utc exceeds data cutoff")
    source = _validate_source(
        field.get("source"),
        path=f"{path}.source",
        evidence_class=evidence_class,
        cutoff=cutoff,
        as_of=as_of,
    )
    value_validator(field.get("value"), f"{path}.value")
    context[claim_id] = {
        "path": path,
        "as_of": as_of,
        "evidence_class": evidence_class,
        "generated_at": source.get("_generated_at"),
        "upstream_claim_ids": source.get("_upstream_claim_ids", []),
    }
    source.pop("_generated_at", None)
    source.pop("_upstream_claim_ids", None)
    return field


def _validate_source(
    raw: Any,
    *,
    path: str,
    evidence_class: str,
    cutoff: datetime,
    as_of: datetime,
) -> dict[str, Any]:
    source = _require_object(raw, path)
    expected_keys = set(_SOURCE_BASE_KEYS)
    if evidence_class == "model_inference":
        expected_keys.add("inference")
    _require_exact_keys(source, expected_keys, path)
    _require_identifier(source.get("source_id"), f"{path}.source_id")
    _require_text(source.get("publisher"), f"{path}.publisher", maximum=300)
    source_sha = _require_sha256(source.get("source_sha256"), f"{path}.source_sha256")
    binding = _validate_material_binding(source.get("material_binding"), f"{path}.material_binding")
    if source_sha != binding["content_sha256"]:
        raise FootballResearchContractError(
            f"{path}.source_sha256 must equal material_binding.content_sha256"
        )
    _validate_source_uri(
        source.get("source_uri"),
        f"{path}.source_uri",
        material_id=binding["material_id"],
        material_version=binding["material_version"],
    )
    retrieved = _require_utc(source.get("retrieved_at_utc"), f"{path}.retrieved_at_utc")
    if retrieved > cutoff:
        raise FootballResearchContractError(f"{path}.retrieved_at_utc exceeds data cutoff")
    publication = _validate_publication(
        source.get("publication"),
        path=f"{path}.publication",
        evidence_class=evidence_class,
        cutoff=cutoff,
        retrieved=retrieved,
        as_of=as_of,
    )

    if evidence_class == "model_inference":
        inference = _require_object(source.get("inference"), f"{path}.inference")
        _require_exact_keys(inference, _INFERENCE_KEYS, f"{path}.inference")
        _require_identifier(inference.get("method_id"), f"{path}.inference.method_id")
        _require_text(inference.get("method_version"), f"{path}.inference.method_version", maximum=80)
        generated = _require_utc(inference.get("generated_at_utc"), f"{path}.inference.generated_at_utc")
        if generated > cutoff or generated > retrieved or generated > as_of:
            raise FootballResearchContractError(
                f"{path}.inference.generated_at_utc must not exceed as_of, retrieval, or cutoff"
            )
        upstream = _validate_identifier_list(
            inference.get("upstream_claim_ids"),
            f"{path}.inference.upstream_claim_ids",
            maximum_items=100,
        )
        if not upstream:
            raise FootballResearchContractError(
                f"{path}.inference.upstream_claim_ids must not be empty"
            )
        source["_generated_at"] = generated
        source["_upstream_claim_ids"] = upstream
    elif "inference" in source:
        raise FootballResearchContractError(f"{path}.inference is model-inference only")

    # publication is accessed above to force the discriminator before the
    # evidence value can be accepted.
    assert publication is not None
    return source


def _validate_material_binding(value: Any, path: str) -> dict[str, Any]:
    binding = _require_object(value, path)
    _require_exact_keys(binding, _MATERIAL_BINDING_KEYS, path)
    material_id = binding.get("material_id")
    if not isinstance(material_id, str) or not _MATERIAL_ID_PATTERN.fullmatch(material_id):
        raise FootballResearchContractError(f"{path}.material_id is invalid")
    _require_integer(binding.get("material_version"), f"{path}.material_version", minimum=1, maximum=2_147_483_647)
    _require_sha256(binding.get("content_sha256"), f"{path}.content_sha256")
    _require_sha256(binding.get("snapshot_sha256"), f"{path}.snapshot_sha256")
    return binding


def _validate_publication(
    value: Any,
    *,
    path: str,
    evidence_class: str,
    cutoff: datetime,
    retrieved: datetime,
    as_of: datetime,
) -> dict[str, Any]:
    publication = _require_object(value, path)
    _require_exact_keys(publication, _PUBLICATION_KEYS, path)
    state = publication.get("state")
    published_raw = publication.get("published_at_utc")
    observed_raw = publication.get("observed_at_utc")
    if evidence_class == "odds_proxy":
        if state != "observed" or published_raw is not None:
            raise FootballResearchContractError(
                f"{path} odds_proxy must be observed with null published_at_utc"
            )
        observed = _require_utc(observed_raw, f"{path}.observed_at_utc")
        if observed > retrieved or observed > cutoff or observed > as_of:
            raise FootballResearchContractError(
                f"{path}.observed_at_utc must not exceed as_of, retrieval, or cutoff"
            )
        return publication

    if state == "published":
        published = _require_utc(published_raw, f"{path}.published_at_utc")
        if observed_raw is not None:
            raise FootballResearchContractError(f"{path}.observed_at_utc must be null when published")
        if published > retrieved or published > cutoff or published > as_of:
            raise FootballResearchContractError(
                f"{path}.published_at_utc must not exceed as_of, retrieval, or cutoff"
            )
    elif state == "not_published":
        if published_raw is not None or observed_raw is not None:
            raise FootballResearchContractError(
                f"{path} not_published timestamps must both be null"
            )
    else:
        raise FootballResearchContractError(
            f"{path}.state must be published or not_published for non-odds evidence"
        )
    if evidence_class == "model_inference" and state != "not_published":
        raise FootballResearchContractError(
            f"{path} model_inference must use not_published with null publication time"
        )
    return publication


def _validate_source_uri(
    value: Any,
    path: str,
    *,
    material_id: str,
    material_version: int,
) -> None:
    if not isinstance(value, str):
        raise FootballResearchContractError(f"{path} must be HTTPS or an exact material URN")
    expected_urn = f"urn:ai-studio:material:{material_id}:v{material_version}"
    if value == expected_urn:
        return
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise FootballResearchContractError(
            f"{path} must be HTTPS or exact {expected_urn}"
        )


def _validate_inference_graph(context: dict[str, dict[str, Any]]) -> None:
    for claim_id, claim in context.items():
        if claim["evidence_class"] != "model_inference":
            continue
        for upstream_id in claim["upstream_claim_ids"]:
            if upstream_id == claim_id:
                raise FootballResearchContractError(
                    f"{claim['path']} model inference must not reference itself"
                )
            upstream = context.get(upstream_id)
            if upstream is None:
                raise FootballResearchContractError(
                    f"{claim['path']} references missing upstream claim {upstream_id}"
                )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(claim_id: str) -> None:
        if claim_id in visiting:
            raise FootballResearchContractError("model inference upstream graph contains a cycle")
        if claim_id in visited:
            return
        visiting.add(claim_id)
        claim = context[claim_id]
        for upstream_id in claim["upstream_claim_ids"]:
            if context[upstream_id]["evidence_class"] == "model_inference":
                visit(upstream_id)
        visiting.remove(claim_id)
        visited.add(claim_id)

    for claim_id, claim in context.items():
        if claim["evidence_class"] == "model_inference":
            visit(claim_id)


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FootballResearchContractError(f"{path} must be an object")
    return value


def _require_exact_keys(value: dict[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise FootballResearchContractError(
            f"{path} is closed; missing={missing}, extra={extra}"
        )


def _require_fixed_value(actual: Any, expected: Any, path: str) -> None:
    if type(actual) is not type(expected) or actual != expected:
        raise FootballResearchContractError(f"{path} must be fixed to {expected!r}")


def _require_identifier(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise FootballResearchContractError(f"{path} is not a valid identifier")
    return value


def _require_sha256(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise FootballResearchContractError(f"{path} must be lowercase sha256")
    return value


def _require_text(value: Any, path: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise FootballResearchContractError(
            f"{path} must be non-empty text with at most {maximum} characters"
        )
    return value


def _require_research_text(value: Any, path: str, *, maximum: int) -> str:
    text = _require_text(value, path, maximum=maximum)
    for segment in re.split(r"[\n.!?。！？]+", text):
        if not segment.strip():
            continue
        safe = bool(_SAFE_WITHHOLDING.search(segment))
        if (
            _FORECAST_OUTCOME_PERCENT.search(segment)
            or _FORECAST_PERCENT_OUTCOME.search(segment)
            or _FORECAST_NUMBER_TERM.search(segment)
            or _METRIC_NUMBER.search(segment)
            or (_QUALITATIVE_FORECAST.search(segment) and not safe)
            or (_QUALITATIVE_CONFIDENCE.search(segment) and not safe)
        ):
            raise FootballResearchContractError(
                f"{path} must not contain a future probability or calibration metric"
            )
    return text


def _require_research_text_list(
    value: Any,
    path: str,
    *,
    maximum_items: int,
    maximum_text: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise FootballResearchContractError(
            f"{path} must be an array with at most {maximum_items} items"
        )
    return [
        _require_research_text(item, f"{path}[{index}]", maximum=maximum_text)
        for index, item in enumerate(value)
    ]


def _validate_identifier_list(
    value: Any,
    path: str,
    *,
    maximum_items: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise FootballResearchContractError(
            f"{path} must be an array with at most {maximum_items} items"
        )
    clean: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        identifier = _require_identifier(item, f"{path}[{index}]")
        if identifier in seen:
            raise FootballResearchContractError(f"{path} values must be unique")
        seen.add(identifier)
        clean.append(identifier)
    return clean


def _require_integer(
    value: Any,
    path: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise FootballResearchContractError(
            f"{path} must be an integer from {minimum} through {maximum}"
        )
    return value


def _require_number(
    value: Any,
    path: str,
    *,
    minimum: float,
    maximum: float,
) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise FootballResearchContractError(
            f"{path} must be a finite number from {minimum} through {maximum}"
        )
    return value


def _require_utc(value: Any, path: str) -> datetime:
    if not isinstance(value, str) or not _UTC_PATTERN.fullmatch(value):
        raise FootballResearchContractError(
            f"{path} must use canonical UTC YYYY-MM-DDTHH:MM:SSZ"
        )
    return _parse_utc_value(value, path)


def _parse_utc_value(value: str, path: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise FootballResearchContractError(f"{path} is not a real UTC timestamp") from exc


def _reject_forbidden_metric_keys(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise FootballResearchContractError(f"{path} keys must be strings")
            normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            if key not in _ALLOWED_PROBABILITY_KEYS and any(
                fragment in normalized for fragment in _FORBIDDEN_KEY_FRAGMENTS
            ):
                raise FootballResearchContractError(
                    f"{path}.{key} is a forbidden probability/confidence/calibration key"
                )
            _reject_forbidden_metric_keys(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_forbidden_metric_keys(nested, f"{path}[{index}]")
    elif isinstance(value, float) and not math.isfinite(value):
        raise FootballResearchContractError(f"{path} must be finite JSON data")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise FootballResearchContractError(f"{path} must contain JSON-compatible data")


__all__ = [
    "FIXED_FOOTBALL_RESEARCH_BOUNDARIES",
    "FOOTBALL_EVIDENCE_CLASSES",
    "FOOTBALL_PROBABILITY_STATE",
    "FOOTBALL_RESEARCH_CAPABILITY_PACK_ID",
    "FOOTBALL_RESEARCH_CONTRACT_SCHEMA",
    "FOOTBALL_RESEARCH_CONTRACT_VERSION",
    "FOOTBALL_RESEARCH_OUTPUT_SCHEMA",
    "FOOTBALL_RESEARCH_OUTPUT_SCHEMA_SHA256",
    "FOOTBALL_RESEARCH_SCHEMA_SHA256",
    "FOOTBALL_RESEARCH_SCHEMA_VERSION",
    "FootballResearchContractError",
    "build_football_research_contract",
    "canonical_json",
    "canonical_sha256",
    "validate_football_research_contract",
    "verify_football_research_contract",
]
