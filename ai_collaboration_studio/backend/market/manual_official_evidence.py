from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterable


MANUAL_OFFICIAL_EVIDENCE_VERSION = "manual_official_evidence_v1"
MANUAL_SUBSTITUTION_STATE = "ready_with_manual_substitution"
ELIGIBLE_ACCESS_ERROR_CODES = frozenset({
    "EARNINGS_MATERIAL_ACCESS_TIMEOUT",
    "EARNINGS_MATERIAL_ACCESS_ERROR",
})
COMPLETE_CATALOG_HUB_ERROR_CODES = frozenset({
    "EARNINGS_MATERIAL_HUB_TIMEOUT",
    "EARNINGS_MATERIAL_HUB_ANTIBOT",
    "EARNINGS_MATERIAL_HUB_ERROR",
})
EFFECTIVELY_RESOLVABLE_ERROR_CODES = (
    ELIGIBLE_ACCESS_ERROR_CODES | COMPLETE_CATALOG_HUB_ERROR_CODES
)

_OVERLAY_KEYS = frozenset({
    "manual_official_evidence",
    "upstream_source_errors",
    "unresolved_source_errors",
    "source_issue_resolutions",
})

LIVE_FETCHABLE_COVERAGE_VERSION = "curated_live_fetchable_coverage_v1"
LIVE_FETCHABLE_COVERAGE_KEYS = frozenset({
    "version",
    "candidate_sha256",
    "symbol",
    "official_url",
    "fiscal_period",
    "material_kind",
    "verified_at",
    "valid_until",
    "source_type",
    "source_tier",
    "claim_status",
    "discovery_method",
    "access_state",
    "access_checked_at",
    "access_status_code",
    "execution_capability",
    "live_trading_allowed",
    "live_evidence_sha256",
})


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _error_key(error: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(error.get("code") or "").strip().upper(),
        str(error.get("symbol") or "").strip().upper(),
        str(error.get("official_url") or "").strip(),
    )


def _resolution_key(resolution: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(resolution.get("original_error_code") or resolution.get("code") or "").strip().upper(),
        str(resolution.get("symbol") or "").strip().upper(),
        str(resolution.get("official_url") or "").strip(),
    )


def _valid_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return bool(re.fullmatch(r"[0-9a-f]{64}", text) and text != "0" * 64)


def _freeze_live_fetchable_coverage(
    material: Any,
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    """Freeze one exact, currently fetchable curated adapter result.

    This deliberately accepts neither arbitrary official-looking links nor
    user-attested overlay rows.  It is only called for materials emitted in
    the current ``official_earnings_materials`` upstream payload.
    """

    if not isinstance(material, dict):
        return None
    status_code = material.get("access_status_code")
    if (
        isinstance(status_code, bool)
        or not isinstance(status_code, int)
        or status_code < 200
        or status_code >= 300
        or not str(material.get("access_checked_at") or "").strip()
        or str(material.get("symbol") or "").strip().upper()
            != str(candidate.get("symbol") or "").strip().upper()
        or str(material.get("official_url") or "")
            != str(candidate.get("official_url") or "")
        or str(material.get("fiscal_period") or "")
            != str(candidate.get("fiscal_period") or "")
        or str(material.get("material_kind") or "")
            != str(candidate.get("material_kind") or "")
        or str(material.get("verified_at") or "")
            != str(candidate.get("verified_at") or "")
        or str(material.get("valid_until") or "")
            != str(candidate.get("valid_until") or "")
        or material.get("source_type") != "company_ir"
        or material.get("source_tier") != "primary"
        or material.get("claim_status") != "company_statement"
        or material.get("discovery_method") != "curated_verified"
        or material.get("access_state") != "fetchable"
        or material.get("execution_capability") != "none"
        or material.get("live_trading_allowed") is not False
    ):
        return None
    frozen = {
        "version": LIVE_FETCHABLE_COVERAGE_VERSION,
        "candidate_sha256": str(candidate.get("candidate_sha256") or ""),
        "symbol": str(candidate.get("symbol") or ""),
        "official_url": str(candidate.get("official_url") or ""),
        "fiscal_period": str(candidate.get("fiscal_period") or ""),
        "material_kind": str(candidate.get("material_kind") or ""),
        "verified_at": str(candidate.get("verified_at") or ""),
        "valid_until": str(candidate.get("valid_until") or ""),
        "source_type": "company_ir",
        "source_tier": "primary",
        "claim_status": "company_statement",
        "discovery_method": "curated_verified",
        "access_state": "fetchable",
        "access_checked_at": str(material.get("access_checked_at") or ""),
        "access_status_code": status_code,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }
    frozen["live_evidence_sha256"] = _canonical_sha256(frozen)
    return frozen


def _validate_frozen_live_fetchable_coverage(
    frozen: Any,
    candidate: dict[str, Any],
) -> bool:
    if not isinstance(frozen, dict) or set(frozen) != LIVE_FETCHABLE_COVERAGE_KEYS:
        return False
    live_evidence_sha256 = str(frozen.get("live_evidence_sha256") or "")
    unsigned = copy.deepcopy(frozen)
    unsigned.pop("live_evidence_sha256", None)
    status_code = frozen.get("access_status_code")
    return bool(
        _valid_sha256(live_evidence_sha256)
        and _canonical_sha256(unsigned) == live_evidence_sha256
        and frozen.get("version") == LIVE_FETCHABLE_COVERAGE_VERSION
        and str(frozen.get("candidate_sha256") or "")
            == str(candidate.get("candidate_sha256") or "")
        and str(frozen.get("symbol") or "").strip().upper()
            == str(candidate.get("symbol") or "").strip().upper()
        and str(frozen.get("official_url") or "")
            == str(candidate.get("official_url") or "")
        and str(frozen.get("fiscal_period") or "")
            == str(candidate.get("fiscal_period") or "")
        and str(frozen.get("material_kind") or "")
            == str(candidate.get("material_kind") or "")
        and str(frozen.get("verified_at") or "")
            == str(candidate.get("verified_at") or "")
        and str(frozen.get("valid_until") or "")
            == str(candidate.get("valid_until") or "")
        and frozen.get("source_type") == "company_ir"
        and frozen.get("source_tier") == "primary"
        and frozen.get("claim_status") == "company_statement"
        and frozen.get("discovery_method") == "curated_verified"
        and frozen.get("access_state") == "fetchable"
        and bool(str(frozen.get("access_checked_at") or "").strip())
        and not isinstance(status_code, bool)
        and isinstance(status_code, int)
        and 200 <= status_code < 300
        and frozen.get("execution_capability") == "none"
        and frozen.get("live_trading_allowed") is False
    )


def trusted_manual_substitution_claimed(value: Any) -> bool:
    """Inspect only contract layers that are allowed to claim manual readiness."""

    if not isinstance(value, dict):
        return False
    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else value
    candidates = [value, evidence]
    if isinstance(evidence, dict):
        candidates.extend(
            evidence.get(key)
            for key in ("official_earnings_materials", "official_earnings_packs")
            if isinstance(evidence.get(key), dict)
        )
    return any(
        str(candidate.get("state") or "").strip().lower() == MANUAL_SUBSTITUTION_STATE
        for candidate in candidates
        if isinstance(candidate, dict)
    )


def validate_manual_official_evidence(
    envelope: Any,
    *,
    expected_room_id: str = "",
) -> bool:
    """Validate the frozen overlay before any gate trusts its resolutions."""

    if not isinstance(envelope, dict):
        return False
    if envelope.get("version") != MANUAL_OFFICIAL_EVIDENCE_VERSION:
        return False
    if envelope.get("execution_capability") != "none" or envelope.get("live_trading_allowed") is not False:
        return False
    room_id = str(envelope.get("room_id") or "").strip()
    expected_room_id = str(expected_room_id or "").strip()
    if not room_id or (expected_room_id and room_id != expected_room_id):
        return False
    overlay_sha256 = str(envelope.get("overlay_sha256") or "").strip().lower()
    unsigned = copy.deepcopy(envelope)
    unsigned.pop("overlay_sha256", None)
    if not _valid_sha256(overlay_sha256) or _canonical_sha256(unsigned) != overlay_sha256:
        return False

    confirmed = envelope.get("confirmed_attestations")
    resolutions = envelope.get("source_issue_resolutions")
    upstream_errors = envelope.get("upstream_source_errors")
    unresolved_errors = envelope.get("unresolved_source_errors")
    invalid_confirmed = envelope.get("invalid_confirmed_attestations")
    superseded_confirmed = envelope.get("superseded_confirmed_attestations")
    if not all(isinstance(value, list) for value in (
        confirmed,
        resolutions,
        upstream_errors,
        unresolved_errors,
        invalid_confirmed,
        superseded_confirmed,
    )):
        return False
    if invalid_confirmed:
        return False
    if any(not isinstance(item, dict) for collection in (
        confirmed,
        resolutions,
        upstream_errors,
        unresolved_errors,
        superseded_confirmed,
    ) for item in collection):
        return False
    if any(
        str(item.get("room_id") or "").strip() != room_id
        or str(item.get("status") or "").upper() != "CONFIRMED"
        or item.get("current_version") is not False
        for item in superseded_confirmed
    ):
        return False

    from .earnings_materials import curated_candidate_sha256

    confirmed_by_id: dict[str, dict[str, Any]] = {}
    for attestation in confirmed:
        attestation_id = str(attestation.get("id") or "").strip()
        material_id = str(attestation.get("material_id") or "").strip()
        try:
            material_version = int(attestation.get("material_version") or 0)
        except (TypeError, ValueError):
            return False
        codes = {
            str(code or "").strip().upper()
            for code in attestation.get("original_error_codes") or []
            if str(code or "").strip()
        }
        if (
            not attestation_id
            or attestation_id in confirmed_by_id
            or not material_id
            or material_version <= 0
            or str(attestation.get("room_id") or "").strip() != room_id
            or str(attestation.get("status") or "").upper() != "CONFIRMED"
            or attestation.get("execution_capability") != "none"
            or attestation.get("live_trading_allowed") is not False
            or not codes
            or not codes.issubset(ELIGIBLE_ACCESS_ERROR_CODES)
            or any(not _valid_sha256(attestation.get(key)) for key in (
                "candidate_sha256",
                "source_sha256",
                "content_sha256",
                "material_snapshot_sha256",
            ))
        ):
            return False
        candidate = attestation.get("candidate_snapshot")
        if not isinstance(candidate, dict) or any((
            curated_candidate_sha256(candidate) != str(attestation.get("candidate_sha256") or ""),
            str(candidate.get("candidate_sha256") or "") != str(attestation.get("candidate_sha256") or ""),
            str(candidate.get("version") or "") != "curated_official_material_candidate_v1",
            str(candidate.get("symbol") or "").strip().upper()
                != str(attestation.get("symbol") or "").strip().upper(),
            str(candidate.get("official_url") or "") != str(attestation.get("official_url") or ""),
            str(candidate.get("fiscal_period") or "") != str(attestation.get("fiscal_period") or ""),
            str(candidate.get("material_kind") or "") != str(attestation.get("material_kind") or ""),
            str(candidate.get("source_type") or "") != "company_ir",
            str(candidate.get("source_tier") or "") != "primary",
        )):
            return False
        confirmed_by_id[attestation_id] = attestation

    upstream_keys = {_error_key(error) for error in upstream_errors}
    resolution_keys: set[tuple[str, str, str]] = set()
    for resolution in resolutions:
        key = _resolution_key(resolution)
        code, symbol, official_url = key
        resolution_version = str(resolution.get("version") or "")
        if resolution_version == "manual_complete_catalog_hub_resolution_v1":
            matrix = resolution.get("candidate_attestation_matrix")
            disposition = str(resolution.get("disposition") or "")
            if (
                disposition not in {
                    "covered_by_complete_live_or_confirmed_catalog",
                    # Preserve validation of already-frozen all-confirmed v1
                    # envelopes generated before mixed coverage was supported.
                    "covered_by_complete_confirmed_catalog_copies",
                }
                or str(resolution.get("room_id") or "").strip() != room_id
                or resolution.get("execution_capability") != "none"
                or resolution.get("live_trading_allowed") is not False
                or code not in COMPLETE_CATALOG_HUB_ERROR_CODES
                or not symbol
                or key not in upstream_keys
                or key in resolution_keys
                or not isinstance(matrix, list)
                or not matrix
                or int(resolution.get("catalog_candidate_count") or 0) != len(matrix)
                or any(not isinstance(item, dict) for item in matrix)
            ):
                return False
            candidate_hashes: set[str] = set()
            candidate_urls: set[str] = set()
            for item in matrix:
                candidate = item.get("candidate_snapshot")
                candidate_sha256 = str(item.get("candidate_sha256") or "")
                candidate_url = str(item.get("official_url") or "")
                if (
                    not isinstance(candidate, dict)
                    or not _valid_sha256(candidate_sha256)
                    or curated_candidate_sha256(candidate) != candidate_sha256
                    or str(candidate.get("candidate_sha256") or "") != candidate_sha256
                    or str(candidate.get("version") or "")
                        != "curated_official_material_candidate_v1"
                    or str(candidate.get("symbol") or "").strip().upper() != symbol
                    or str(candidate.get("official_url") or "") != candidate_url
                    or str(candidate.get("source_type") or "") != "company_ir"
                    or str(candidate.get("source_tier") or "") != "primary"
                    or candidate_sha256 in candidate_hashes
                    or candidate_url in candidate_urls
                ):
                    return False
                coverage_kind = str(item.get("coverage_kind") or "")
                if (
                    not coverage_kind
                    and disposition == "covered_by_complete_confirmed_catalog_copies"
                ):
                    coverage_kind = "confirmed_copy"
                if coverage_kind == "confirmed_copy":
                    attestation = confirmed_by_id.get(str(item.get("attestation_id") or ""))
                    if (
                        attestation is None
                        or str(attestation.get("candidate_sha256") or "") != candidate_sha256
                        or attestation.get("candidate_snapshot") != candidate
                        or str(attestation.get("official_url") or "") != candidate_url
                        or str(attestation.get("symbol") or "").strip().upper() != symbol
                        or any(
                            str(attestation.get(hash_key) or "")
                            != str(item.get(hash_key) or "")
                            for hash_key in (
                                "source_sha256",
                                "content_sha256",
                                "material_snapshot_sha256",
                            )
                        )
                    ):
                        return False
                elif coverage_kind == "live_fetchable":
                    if (
                        disposition != "covered_by_complete_live_or_confirmed_catalog"
                        or item.get("attestation_id")
                        or any(item.get(hash_key) for hash_key in (
                            "source_sha256",
                            "content_sha256",
                            "material_snapshot_sha256",
                        ))
                        or not _validate_frozen_live_fetchable_coverage(
                            item.get("live_evidence_snapshot"),
                            candidate,
                        )
                    ):
                        return False
                else:
                    return False
                candidate_hashes.add(candidate_sha256)
                candidate_urls.add(candidate_url)
            resolution_keys.add(key)
            continue

        attestation = confirmed_by_id.get(str(resolution.get("attestation_id") or ""))
        try:
            material_version = int(resolution.get("material_version") or 0)
        except (TypeError, ValueError):
            return False
        if (
            resolution_version != "manual_source_issue_resolution_v1"
            or resolution.get("disposition") != "covered_by_confirmed_exact_official_copy"
            or str(resolution.get("room_id") or "").strip() != room_id
            or resolution.get("execution_capability") != "none"
            or resolution.get("live_trading_allowed") is not False
            or code not in ELIGIBLE_ACCESS_ERROR_CODES
            or not symbol
            or not official_url
            or key not in upstream_keys
            or key in resolution_keys
            or attestation is None
            or str(attestation.get("symbol") or "").strip().upper() != symbol
            or str(attestation.get("official_url") or "").strip() != official_url
            or code not in {
                str(item or "").strip().upper()
                for item in attestation.get("original_error_codes") or []
            }
            or str(attestation.get("material_id") or "") != str(resolution.get("material_id") or "")
            or int(attestation.get("material_version") or 0) != material_version
            or any(str(attestation.get(hash_key) or "") != str(resolution.get(hash_key) or "") for hash_key in (
                "source_sha256",
                "content_sha256",
                "material_snapshot_sha256",
            ))
        ):
            return False
        resolution_keys.add(key)

    expected_unresolved = upstream_keys - resolution_keys
    if {_error_key(error) for error in unresolved_errors} != expected_unresolved:
        return False
    state = str(envelope.get("state") or "")
    if state == "confirmed" and not confirmed:
        return False
    if state == "staged_only" and (confirmed or int(envelope.get("staged_count") or 0) <= 0):
        return False
    if state == "none" and (confirmed or resolutions or int(envelope.get("staged_count") or 0) != 0):
        return False
    if state not in {"confirmed", "staged_only", "none"}:
        return False
    return True


def _dedupe_dicts(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        fingerprint = _canonical_sha256(row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        result.append(copy.deepcopy(row))
    return result


def collect_source_errors(value: Any) -> list[dict[str, Any]]:
    """Return preserved source errors while ignoring overlay audit copies."""

    errors: list[dict[str, Any]] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            raw_errors = item.get("source_errors")
            if isinstance(raw_errors, list):
                errors.extend(copy.deepcopy(error) for error in raw_errors if isinstance(error, dict))
            for key, child in item.items():
                if key == "source_errors" or key in _OVERLAY_KEYS:
                    continue
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return _dedupe_dicts(errors)


def effective_source_errors(
    value: Any,
    manual_envelope: dict[str, Any] | None = None,
    *,
    expected_room_id: str = "",
) -> list[dict[str, Any]]:
    """Return unresolved errors without mutating or hiding the upstream record.

    A resolution is intentionally narrow: the error code, symbol and exact
    official URL must all match a confirmed server-side attestation.
    """

    envelope = manual_envelope
    if envelope is None and isinstance(value, dict):
        candidate = value.get("manual_official_evidence")
        envelope = candidate if isinstance(candidate, dict) else None
    resolution_keys = {
        _resolution_key(resolution)
        for resolution in (envelope or {}).get("source_issue_resolutions") or []
        if isinstance(resolution, dict)
    } if validate_manual_official_evidence(
        envelope,
        expected_room_id=expected_room_id,
    ) else set()
    return [
        error
        for error in collect_source_errors(value)
        if not (
            _error_key(error)[0] in EFFECTIVELY_RESOLVABLE_ERROR_CODES
            and _error_key(error) in resolution_keys
        )
    ]


def effective_source_error_codes(
    value: Any,
    manual_envelope: dict[str, Any] | None = None,
    *,
    expected_room_id: str = "",
) -> list[str]:
    return sorted({
        str(error.get("code") or "").strip().upper()
        for error in effective_source_errors(
            value,
            manual_envelope,
            expected_room_id=expected_room_id,
        )
        if str(error.get("code") or "").strip()
    })


def _attestation_public_record(attestation: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "room_id",
        "material_id",
        "material_version",
        "symbol",
        "fiscal_period",
        "material_kind",
        "official_url",
        "candidate_sha256",
        "candidate_snapshot",
        "source_sha256",
        "content_sha256",
        "material_snapshot_sha256",
        "status",
        "confirmed_by",
        "confirmed_at",
        "original_error_codes",
    )
    record = {key: copy.deepcopy(attestation.get(key)) for key in keys}
    record.update({
        "evidence_kind": "earnings_material",
        "execution_capability": "none",
        "live_trading_allowed": False,
    })
    return record


def _attested_material(attestation: dict[str, Any]) -> dict[str, Any]:
    return {
        "room_id": str(attestation.get("room_id") or ""),
        "symbol": str(attestation.get("symbol") or ""),
        "fiscal_period": str(attestation.get("fiscal_period") or ""),
        "material_kind": str(attestation.get("material_kind") or ""),
        "official_url": str(attestation.get("official_url") or ""),
        "discovery_method": "user_attested_exact_official_copy",
        "access_state": "local_user_confirmed_copy",
        "claim_status": "user_attested_official_copy",
        "source_type": "company_ir",
        "source_tier": "primary_user_attested",
        "attestation_id": str(attestation.get("id") or ""),
        "material_id": str(attestation.get("material_id") or ""),
        "material_version": int(attestation.get("material_version") or 0),
        "source_sha256": str(attestation.get("source_sha256") or ""),
        "content_sha256": str(attestation.get("content_sha256") or ""),
        "material_snapshot_sha256": str(attestation.get("material_snapshot_sha256") or ""),
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def _append_attested_material(
    materials_payload: dict[str, Any],
    attestation: dict[str, Any],
) -> None:
    symbol = str(attestation.get("symbol") or "")
    rows = materials_payload.setdefault("rows", [])
    row = next(
        (candidate for candidate in rows if str(candidate.get("symbol") or "") == symbol),
        None,
    )
    if row is None:
        row = {
            "symbol": symbol,
            "quality": "limited",
            "materials": [],
            "source_errors": [],
            "source_warnings": [],
        }
        rows.append(row)
    materials = row.setdefault("materials", [])
    identity = (
        str(attestation.get("official_url") or ""),
        str(attestation.get("fiscal_period") or ""),
        str(attestation.get("material_kind") or ""),
    )
    if not any(
        (
            str(item.get("official_url") or ""),
            str(item.get("fiscal_period") or ""),
            str(item.get("material_kind") or ""),
        ) == identity
        for item in materials
        if isinstance(item, dict)
    ):
        materials.append(_attested_material(attestation))
    row["material_count"] = len(materials)
    row["usable_material_count"] = len(materials)
    row["manual_attestation_ids"] = sorted({
        str(item.get("attestation_id") or "")
        for item in materials
        if isinstance(item, dict) and item.get("attestation_id")
    })


def _append_attested_pack_material(
    packs_payload: dict[str, Any],
    attestation: dict[str, Any],
) -> None:
    symbol = str(attestation.get("symbol") or "")
    period = str(attestation.get("fiscal_period") or "")
    kind = str(attestation.get("material_kind") or "")
    url = str(attestation.get("official_url") or "")
    for row in packs_payload.get("rows") or []:
        if str(row.get("symbol") or "") != symbol:
            continue
        for pack in row.get("packs") or []:
            if str(pack.get("fiscal_period") or "") != period:
                continue
            direct_materials = pack.setdefault("direct_materials", [])
            if not any(
                str(item.get("official_url") or "") == url
                for item in direct_materials
                if isinstance(item, dict)
            ):
                direct_materials.append(_attested_material(attestation))
            field_name = {
                "earnings_presentation": "presentation_url",
                "prepared_remarks": "prepared_remarks_url",
                "supplemental_financial_information": "supplemental_url",
                "earnings_release": "earnings_release_material_url",
                "corrected_transcript": "transcript_url",
                "earnings_transcript": "transcript_url",
            }.get(kind)
            if field_name:
                pack[field_name] = url
            if kind == "earnings_presentation":
                pack["presentation_discovery_status"] = "user_attested_exact_official_copy"
            pack["manual_attestation_ids"] = sorted({
                str(item.get("attestation_id") or "")
                for item in direct_materials
                if isinstance(item, dict) and item.get("attestation_id")
            })


def _complete_current_catalog_matrix(
    symbol: str,
    confirmed: list[dict[str, Any]],
    source_materials_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Cover every current candidate by a live result or a confirmed copy."""

    from .earnings_materials import (
        CURATED_OFFICIAL_MATERIALS,
        curated_official_material_candidate,
    )

    normalized_symbol = str(symbol or "").strip().upper()
    raw_candidates = CURATED_OFFICIAL_MATERIALS.get(normalized_symbol) or []
    if not raw_candidates:
        return []
    matrix: list[dict[str, Any]] = []
    for raw_candidate in raw_candidates:
        candidate = curated_official_material_candidate(
            normalized_symbol,
            str(raw_candidate.get("official_url") or ""),
        )
        if not candidate:
            return []
        live_evidence_snapshot = next((
            frozen
            for row in source_materials_payload.get("rows") or []
            if isinstance(row, dict)
            and str(row.get("symbol") or "").strip().upper() == normalized_symbol
            for material in row.get("materials") or []
            if (
                frozen := _freeze_live_fetchable_coverage(material, candidate)
            ) is not None
        ), None)
        if live_evidence_snapshot is not None:
            matrix.append({
                "coverage_kind": "live_fetchable",
                "candidate_sha256": str(candidate.get("candidate_sha256") or ""),
                "candidate_snapshot": copy.deepcopy(candidate),
                "official_url": str(candidate.get("official_url") or ""),
                "live_evidence_snapshot": live_evidence_snapshot,
            })
            continue
        attestation = next((
            item
            for item in confirmed
            if str(item.get("symbol") or "").strip().upper() == normalized_symbol
            and str(item.get("official_url") or "") == str(candidate.get("official_url") or "")
            and str(item.get("candidate_sha256") or "")
                == str(candidate.get("candidate_sha256") or "")
            and item.get("candidate_snapshot") == candidate
        ), None)
        if attestation is None:
            return []
        matrix.append({
            "coverage_kind": "confirmed_copy",
            "candidate_sha256": str(candidate.get("candidate_sha256") or ""),
            "candidate_snapshot": copy.deepcopy(candidate),
            "official_url": str(candidate.get("official_url") or ""),
            "attestation_id": str(attestation.get("id") or ""),
            "source_sha256": str(attestation.get("source_sha256") or ""),
            "content_sha256": str(attestation.get("content_sha256") or ""),
            "material_snapshot_sha256": str(
                attestation.get("material_snapshot_sha256") or ""
            ),
        })
    return matrix


def _independent_structure_ready(snapshot: dict[str, Any]) -> bool:
    symbols = {str(symbol or "") for symbol in snapshot.get("symbols") or [] if str(symbol or "")}
    coverage = snapshot.get("coverage") if isinstance(snapshot.get("coverage"), dict) else {}
    return bool(symbols) and all((
        set(coverage.get("official_filings") or []) == symbols,
        set(coverage.get("company_ir_releases") or []) == symbols,
        set(coverage.get("official_earnings_packs") or []) == symbols,
        coverage.get("industry_supply_demand") is True,
    ))


def apply_attested_earnings_overlay(
    snapshot: dict[str, Any],
    room_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    """Overlay confirmed room-scoped copies onto a captured read-only snapshot.

    The upstream errors remain byte-for-byte represented in ``source_errors``.
    Only the effective gate view changes, and only for exact catalog access
    failures covered by a current, integrity-valid, explicitly confirmed copy.
    """

    if not isinstance(snapshot, dict):
        return {}
    evidence = snapshot.get("evidence") if isinstance(snapshot.get("evidence"), dict) else None
    if evidence is None:
        return snapshot
    # A prefetched/frozen snapshot already carries the exact room-scoped audit
    # overlay that was used to build its launch plan. Do not silently rebase it.
    if isinstance(evidence.get("manual_official_evidence"), dict):
        return snapshot

    room_payload = (
        room_snapshot.get("room")
        if isinstance(room_snapshot, dict) and isinstance(room_snapshot.get("room"), dict)
        else {}
    )
    room_id = str(room_payload.get("id") or "").strip()
    if not room_id:
        return snapshot

    attestations = [
        item
        for item in (room_snapshot or {}).get("official_attestations") or []
        if isinstance(item, dict)
        and str(item.get("room_id") or "").strip() == room_id
    ]
    raw_confirmed = [
        item
        for item in attestations
        if str(item.get("status") or "").upper() == "CONFIRMED"
    ]
    if not raw_confirmed:
        return snapshot
    current_confirmed = [
        item
        for item in raw_confirmed
        if item.get("current_version") is True
    ]
    confirmed = [
        item
        for item in current_confirmed
        if item.get("confirmation_ready") is True
        and item.get("integrity_ready") is True
    ]
    superseded_confirmed = [
        {
            "id": str(item.get("id") or ""),
            "room_id": room_id,
            "material_id": str(item.get("material_id") or ""),
            "material_version": int(item.get("material_version") or 0),
            "status": "CONFIRMED",
            "state": str(item.get("state") or "stale_version"),
            "current_version": False,
            "integrity_issues": copy.deepcopy(item.get("integrity_issues") or []),
        }
        for item in raw_confirmed
        if item.get("current_version") is not True
    ]
    invalid_confirmed = [
        {
            "id": str(item.get("id") or ""),
            "room_id": room_id,
            "material_id": str(item.get("material_id") or ""),
            "state": str(item.get("state") or "integrity_failed"),
            "integrity_issues": copy.deepcopy(item.get("integrity_issues") or []),
        }
        for item in current_confirmed
        if item not in confirmed
    ]

    source_materials_payload = (
        evidence.get("official_earnings_materials")
        if isinstance(evidence.get("official_earnings_materials"), dict)
        else {}
    )
    source_packs_payload = (
        evidence.get("official_earnings_packs")
        if isinstance(evidence.get("official_earnings_packs"), dict)
        else {}
    )
    upstream_errors = _dedupe_dicts([
        *collect_source_errors(source_materials_payload),
        *collect_source_errors(source_packs_payload),
    ])
    usable_confirmed = [
        attestation
        for attestation in confirmed
        if any(
            _error_key(error)[0] in ELIGIBLE_ACCESS_ERROR_CODES
            and _error_key(error)[1] == str(attestation.get("symbol") or "").strip().upper()
            and _error_key(error)[2] == str(attestation.get("official_url") or "").strip()
            and _error_key(error)[0] in {
                str(code or "").strip().upper()
                for code in attestation.get("original_error_codes") or []
            }
            for error in upstream_errors
        )
    ]
    hub_resolution_specs: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    usable_attestation_ids = {
        str(item.get("id") or "") for item in usable_confirmed
    }
    for error in upstream_errors:
        code, symbol, _official_url = _error_key(error)
        if code not in COMPLETE_CATALOG_HUB_ERROR_CODES or not symbol:
            continue
        matrix = _complete_current_catalog_matrix(
            symbol,
            confirmed,
            source_materials_payload,
        )
        if not matrix:
            continue
        hub_resolution_specs.append((error, matrix))
        usable_attestation_ids.update(
            str(item.get("attestation_id") or "")
            for item in matrix
            if item.get("coverage_kind") == "confirmed_copy"
        )
    usable_confirmed = [
        item
        for item in confirmed
        if str(item.get("id") or "") in usable_attestation_ids
    ]
    if not usable_confirmed and not invalid_confirmed:
        return snapshot

    result = copy.deepcopy(snapshot)
    evidence = result["evidence"]
    materials_payload = (
        evidence.get("official_earnings_materials")
        if isinstance(evidence.get("official_earnings_materials"), dict)
        else {}
    )
    packs_payload = (
        evidence.get("official_earnings_packs")
        if isinstance(evidence.get("official_earnings_packs"), dict)
        else {}
    )
    evidence["official_earnings_materials"] = materials_payload
    evidence["official_earnings_packs"] = packs_payload
    for attestation in usable_confirmed:
        _append_attested_material(materials_payload, attestation)
        _append_attested_pack_material(packs_payload, attestation)

    resolutions: list[dict[str, Any]] = []
    for error in upstream_errors:
        code, symbol, official_url = _error_key(error)
        if code not in ELIGIBLE_ACCESS_ERROR_CODES or not symbol or not official_url:
            continue
        attestation = next((
            item
            for item in usable_confirmed
            if str(item.get("symbol") or "").strip().upper() == symbol
            and str(item.get("official_url") or "").strip() == official_url
            and code in {
                str(bound_code or "").strip().upper()
                for bound_code in item.get("original_error_codes") or []
            }
        ), None)
        if attestation is None:
            continue
        resolutions.append({
            "version": "manual_source_issue_resolution_v1",
            "room_id": room_id,
            "disposition": "covered_by_confirmed_exact_official_copy",
            "original_error_code": code,
            "symbol": symbol,
            "official_url": official_url,
            "attestation_id": str(attestation.get("id") or ""),
            "material_id": str(attestation.get("material_id") or ""),
            "material_version": int(attestation.get("material_version") or 0),
            "source_sha256": str(attestation.get("source_sha256") or ""),
            "content_sha256": str(attestation.get("content_sha256") or ""),
            "material_snapshot_sha256": str(attestation.get("material_snapshot_sha256") or ""),
            "execution_capability": "none",
            "live_trading_allowed": False,
        })
    for error, matrix in hub_resolution_specs:
        code, symbol, official_url = _error_key(error)
        resolutions.append({
            "version": "manual_complete_catalog_hub_resolution_v1",
            "room_id": room_id,
            "disposition": "covered_by_complete_live_or_confirmed_catalog",
            "original_error_code": code,
            "symbol": symbol,
            "official_url": official_url,
            "catalog_candidate_count": len(matrix),
            "candidate_attestation_matrix": copy.deepcopy(matrix),
            "execution_capability": "none",
            "live_trading_allowed": False,
        })
    resolutions = _dedupe_dicts(resolutions)
    resolved_keys = {_resolution_key(item) for item in resolutions}
    unresolved_errors = [
        error
        for error in upstream_errors
        if _error_key(error) not in resolved_keys
    ]

    envelope = {
        "version": MANUAL_OFFICIAL_EVIDENCE_VERSION,
        "room_id": room_id,
        "state": "confirmed",
        "confirmed_attestations": [_attestation_public_record(item) for item in usable_confirmed],
        "staged_count": sum(
            1 for item in attestations if str(item.get("status") or "").upper() == "STAGED"
        ),
        "invalid_confirmed_attestations": invalid_confirmed,
        "superseded_confirmed_attestations": superseded_confirmed,
        "source_issue_resolutions": resolutions,
        "upstream_source_errors": upstream_errors,
        "unresolved_source_errors": unresolved_errors,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }
    envelope["overlay_sha256"] = _canonical_sha256(envelope)
    evidence["manual_official_evidence"] = envelope

    hub_resolutions = [
        item
        for item in resolutions
        if item.get("version") == "manual_complete_catalog_hub_resolution_v1"
    ]
    if hub_resolutions:
        warning_rows = packs_payload.setdefault("source_warnings", [])
        for resolution in hub_resolutions:
            warning = {
                "source": "official_company_ir_materials",
                "symbol": str(resolution.get("symbol") or ""),
                "code": "EARNINGS_MATERIAL_HUB_ERROR_COVERED_BY_COMPLETE_CATALOG",
                "message": "官方材料中心访问错误保留审计；当前目录全部候选已有同房间确认副本。",
            }
            if warning not in warning_rows:
                warning_rows.append(warning)

    if resolutions and not unresolved_errors:
        materials_payload["state"] = MANUAL_SUBSTITUTION_STATE
        packs_payload["state"] = MANUAL_SUBSTITUTION_STATE
        for row in materials_payload.get("rows") or []:
            if row.get("manual_attestation_ids"):
                row["quality"] = MANUAL_SUBSTITUTION_STATE

    all_effective_errors = effective_source_errors(
        evidence,
        envelope,
        expected_room_id=room_id,
    )
    if resolutions and not all_effective_errors:
        if evidence.get("structure_ready") is True:
            evidence["state"] = MANUAL_SUBSTITUTION_STATE
        elif _independent_structure_ready(result):
            result["state"] = MANUAL_SUBSTITUTION_STATE
    return result


__all__ = [
    "ELIGIBLE_ACCESS_ERROR_CODES",
    "MANUAL_OFFICIAL_EVIDENCE_VERSION",
    "MANUAL_SUBSTITUTION_STATE",
    "apply_attested_earnings_overlay",
    "collect_source_errors",
    "effective_source_error_codes",
    "effective_source_errors",
    "trusted_manual_substitution_claimed",
    "validate_manual_official_evidence",
]
