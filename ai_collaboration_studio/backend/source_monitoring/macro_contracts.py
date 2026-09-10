"""Closed lifecycle projection shared by official macro source adapters.

The source-specific market clients only fetch and parse fixed official endpoints.
This module owns the stable identity, scheduled/released/revised distinction,
checkpoint shape, and Source Inbox projection.  It performs no network, storage,
provider, model, or execution work.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..source_inbox_contracts import (
    PROJECT_SOURCE_ITEM_VERSION,
    SourceInboxContractError,
    canonicalize_source_url,
)
from .contracts import (
    MAX_SOURCE_ERRORS_PER_POLL,
    SourceMonitoringContractError,
    SourcePollError,
    canonical_json,
    canonical_sha256,
    normalize_checkpoint,
)


MACRO_LIFECYCLE_VERSION = "official_macro_lifecycle_v1"
MACRO_PROJECTION_VERSION = "official_macro_projection_v1"
MACRO_CHECKPOINT_ENTRY_LIMIT = 120

MACRO_EVENT_STATES = frozenset({"scheduled", "released", "revised"})
MACRO_SUBJECT_PHASES = frozenset({"schedule", "release"})
MACRO_AUTHORITIES = frozenset({"federal_reserve", "bls", "treasury"})

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SLUG_RE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
_DATA_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,79}\Z")
_BLS_REFERENCE_PERIOD_RE = re.compile(r"((?:19|20)[0-9]{2})-M(0[1-9]|1[0-2])\Z")
_DATE_REFERENCE_PERIOD_RE = re.compile(
    r"((?:19|20)[0-9]{2})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])\Z"
)
_MACRO_RECORD_FIELDS = frozenset({
    "authority",
    "data",
    "family",
    "official_id",
    "official_revision",
    "official_url",
    "reference_period",
    "released_at",
    "scheduled_at",
    "source_url",
    "summary",
    "title",
})
_FORBIDDEN_DATA_FIELD_WORDS = frozenset({
    "account",
    "accounts",
    "bet",
    "bets",
    "brokerage",
    "command",
    "commands",
    "execute",
    "execution",
    "function",
    "functions",
    "mcp",
    "order",
    "orders",
    "payment",
    "payments",
    "shell",
    "tool",
    "tools",
    "trade",
    "trades",
    "transfer",
    "transfers",
    "wallet",
    "wallets",
    "withdraw",
    "withdrawals",
})
_SENSITIVE_DATA_FIELD_WORDS = frozenset({
    "auth",
    "authorization",
    "cookie",
    "credential",
    "jwt",
    "key",
    "password",
    "secret",
    "session",
    "sig",
    "signature",
    "token",
})
_FORBIDDEN_COMPACT_DATA_FIELDS = frozenset({
    "functioncall",
    "paralleltoolcalls",
    "toolchoice",
})
_SENSITIVE_COMPACT_DATA_FIELDS = frozenset({
    "accesstoken",
    "apikey",
    "authtoken",
    "clientsecret",
    "encryptionkey",
    "privatekey",
    "refreshtoken",
    "signingkey",
    "xamzalgorithm",
    "xamzcredential",
    "xamzdate",
    "xamzsecuritytoken",
    "xamzsignature",
})

_AUTHORITY_METADATA = {
    "federal_reserve": {
        "label": "Federal Reserve Board",
        "publisher": "Board of Governors of the Federal Reserve System",
        "hosts": frozenset({"www.federalreserve.gov"}),
    },
    "bls": {
        "label": "U.S. Bureau of Labor Statistics",
        "publisher": "U.S. Bureau of Labor Statistics",
        "hosts": frozenset({"www.bls.gov", "api.bls.gov"}),
    },
    "treasury": {
        "label": "U.S. Department of the Treasury",
        "publisher": "U.S. Department of the Treasury",
        "hosts": frozenset({
            "api.fiscaldata.treasury.gov",
            "fiscaldata.treasury.gov",
            "home.treasury.gov",
        }),
    },
}


def _error(code: str, message: str) -> SourceMonitoringContractError:
    return SourceMonitoringContractError(code, message)


def _native_text(
    value: Any,
    *,
    field: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        raise _error("OFFICIAL_MACRO_RECORD_INVALID", f"{field} must be a native string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _error(
            "OFFICIAL_MACRO_RECORD_INVALID",
            f"{field} contains a control character",
        )
    try:
        normalized = unicodedata.normalize("NFC", value)
        normalized.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise _error(
            "OFFICIAL_MACRO_RECORD_INVALID",
            f"{field} is not valid UTF-8 text",
        ) from exc
    clean = " ".join(normalized.split())
    if (
        (not clean and not allow_empty)
        or len(clean) > maximum
    ):
        raise _error(
            "OFFICIAL_MACRO_RECORD_INVALID",
            f"{field} is empty, oversized, or contains a control character",
        )
    return clean


def _slug(value: Any, *, field: str) -> str:
    clean = _native_text(value, field=field, maximum=80)
    if not _SLUG_RE.fullmatch(clean):
        raise _error("OFFICIAL_MACRO_RECORD_INVALID", f"{field} must be a canonical slug")
    return clean


def _rfc3339(
    value: Any,
    *,
    field: str,
    allow_empty: bool,
) -> tuple[str, datetime | None]:
    clean = _native_text(
        value,
        field=field,
        maximum=40,
        allow_empty=allow_empty,
    )
    if not clean:
        return "", None
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error("OFFICIAL_MACRO_TIME_INVALID", f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise _error("OFFICIAL_MACRO_TIME_INVALID", f"{field} must include an offset")
    utc = parsed.astimezone(timezone.utc)
    base = utc.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f".{utc.microsecond:06d}".rstrip("0") if utc.microsecond else ""
    return f"{base}{fraction}Z", utc


def _official_url(value: Any, *, authority: str, field: str) -> str:
    raw = _native_text(value, field=field, maximum=2_000)
    try:
        clean = canonicalize_source_url(raw, path=f"$.{field}")
        parsed = urlsplit(clean)
        port = parsed.port
    except (SourceInboxContractError, ValueError) as exc:
        raise _error("OFFICIAL_MACRO_URL_INVALID", f"{field} has an invalid URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _AUTHORITY_METADATA[authority]["hosts"]
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise _error(
            "OFFICIAL_MACRO_URL_INVALID",
            f"{field} is outside the fixed authority HTTPS boundary",
        )
    return clean


def _data_projection(value: Any) -> dict[str, str]:
    if type(value) is not dict or len(value) > 32:
        raise _error(
            "OFFICIAL_MACRO_RECORD_INVALID",
            "data must be a bounded native mapping",
        )
    keys: list[str] = []
    for key in value:
        if type(key) is not str or not _DATA_KEY_RE.fullmatch(key):
            raise _error(
                "OFFICIAL_MACRO_RECORD_INVALID",
                "data contains a noncanonical key",
            )
        words = frozenset(part for part in key.lower().split("_") if part)
        compact = re.sub(r"[^a-z0-9]", "", key.lower())
        if (
            words & (_FORBIDDEN_DATA_FIELD_WORDS | _SENSITIVE_DATA_FIELD_WORDS)
            or compact in (
                _FORBIDDEN_COMPACT_DATA_FIELDS
                | _SENSITIVE_COMPACT_DATA_FIELDS
            )
        ):
            raise _error(
                "OFFICIAL_MACRO_RECORD_INVALID",
                "data contains a forbidden execution or sensitive field",
            )
        keys.append(key)
    clean: dict[str, str] = {}
    for key in sorted(keys):
        clean[key] = _native_text(
            value[key],
            field=f"data.{key}",
            maximum=1_000,
            allow_empty=True,
        )
    if len(canonical_json(clean).encode("utf-8")) > 4_096:
        raise _error(
            "OFFICIAL_MACRO_RECORD_INVALID",
            "data exceeds the sealed UTF-8 projection limit",
        )
    return clean


def _release_reference_anchor(authority: str, reference_period: str) -> str:
    if authority == "bls":
        match = _BLS_REFERENCE_PERIOD_RE.fullmatch(reference_period)
        if match:
            return f"{match.group(1)}-{match.group(2)}-01T00:00:00Z"
    if authority == "treasury":
        match = _DATE_REFERENCE_PERIOD_RE.fullmatch(reference_period)
        if match:
            try:
                parsed = datetime.strptime(reference_period, "%Y-%m-%d")
            except ValueError:
                return ""
            return parsed.strftime("%Y-%m-%dT00:00:00Z")
    return ""


def normalize_macro_checkpoint(
    value: Any,
    *,
    checkpoint_version: str,
    maximum_entries: int = MACRO_CHECKPOINT_ENTRY_LIMIT,
) -> tuple[dict[str, Any], list[str], dict[str, dict[str, str]]]:
    """Validate an exact current-window projection checkpoint."""

    checkpoint = normalize_checkpoint(value)
    if checkpoint == {}:
        return checkpoint, [], {}
    if set(checkpoint) != {"version", "entries"}:
        raise _error(
            "OFFICIAL_MACRO_CHECKPOINT_INVALID",
            "macro checkpoint fields do not match v1",
        )
    if checkpoint.get("version") != checkpoint_version:
        raise _error(
            "OFFICIAL_MACRO_CHECKPOINT_INVALID",
            "macro checkpoint version is unsupported",
        )
    entries = checkpoint.get("entries")
    if type(entries) is not list or len(entries) > maximum_entries:
        raise _error(
            "OFFICIAL_MACRO_CHECKPOINT_INVALID",
            "macro checkpoint entries exceed the sealed bound",
        )
    order: list[str] = []
    projections: dict[str, dict[str, str]] = {}
    for entry in entries:
        if type(entry) is not dict or set(entry) != {
            "data_sha256",
            "document_sha256",
            "identity_sha256",
            "projection_sha256",
            "schedule_sha256",
        }:
            raise _error(
                "OFFICIAL_MACRO_CHECKPOINT_INVALID",
                "macro checkpoint entry fields are invalid",
            )
        identity_sha = entry.get("identity_sha256")
        projection_sha = entry.get("projection_sha256")
        data_sha = entry.get("data_sha256")
        document_sha = entry.get("document_sha256")
        schedule_sha = entry.get("schedule_sha256")
        if (
            type(identity_sha) is not str
            or not _SHA256_RE.fullmatch(identity_sha)
            or type(projection_sha) is not str
            or not _SHA256_RE.fullmatch(projection_sha)
            or type(data_sha) is not str
            or not _SHA256_RE.fullmatch(data_sha)
            or type(document_sha) is not str
            or not _SHA256_RE.fullmatch(document_sha)
            or type(schedule_sha) is not str
            or not _SHA256_RE.fullmatch(schedule_sha)
            or identity_sha in projections
        ):
            raise _error(
                "OFFICIAL_MACRO_CHECKPOINT_INVALID",
                "macro checkpoint entry identity or projection is invalid",
        )
        order.append(identity_sha)
        projections[identity_sha] = {
            "data_sha256": data_sha,
            "document_sha256": document_sha,
            "projection_sha256": projection_sha,
            "schedule_sha256": schedule_sha,
        }
    return checkpoint, order, projections


def normalize_macro_source_errors(
    value: Any,
    *,
    fallback_scope: str,
) -> tuple[SourcePollError, ...]:
    if value is None:
        return ()
    if type(value) is not list:
        return (SourcePollError.build(
            "OFFICIAL_MACRO_SOURCE_ERROR",
            "official macro client returned an invalid error collection",
            fallback_scope,
        ),)
    if len(value) > MAX_SOURCE_ERRORS_PER_POLL:
        return (SourcePollError.build(
            "OFFICIAL_MACRO_SOURCE_ERRORS_EXCEEDED",
            "official macro client returned too many source errors",
            fallback_scope,
        ),)
    errors: list[SourcePollError] = []
    malformed = False
    for raw in value:
        if type(raw) is not dict or set(raw) != {"code", "message", "scope"}:
            malformed = True
            continue
        code = raw.get("code")
        message = raw.get("message")
        scope = raw.get("scope")
        if (
            type(code) is not str
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code)
            or type(message) is not str
            or not message.strip()
            or type(scope) is not str
            or not scope.strip()
        ):
            malformed = True
            continue
        errors.append(SourcePollError.build(code, message[:1_000], scope[:160]))
    if malformed:
        return (SourcePollError.build(
            "OFFICIAL_MACRO_SOURCE_ERROR_INVALID",
            "official macro client returned a malformed source error",
            fallback_scope,
        ),)
    return tuple(errors)


def normalize_macro_record(
    value: Any,
    *,
    allowed_authorities: frozenset[str],
    subject_phase: str,
    observed_at: datetime,
) -> dict[str, Any]:
    """Normalize one parsed official record without coercing identity fields."""

    if type(value) is not dict:
        raise _error("OFFICIAL_MACRO_RECORD_INVALID", "macro record must be an object")
    if set(value) != _MACRO_RECORD_FIELDS:
        raise _error(
            "OFFICIAL_MACRO_RECORD_INVALID",
            "macro record fields do not match the closed v1 schema",
        )
    if subject_phase not in MACRO_SUBJECT_PHASES:
        raise _error("OFFICIAL_MACRO_PHASE_INVALID", "macro subject phase is unsupported")
    authority = _slug(value.get("authority"), field="authority")
    if authority not in MACRO_AUTHORITIES or authority not in allowed_authorities:
        raise _error(
            "OFFICIAL_MACRO_AUTHORITY_INVALID",
            "macro record authority is outside the adapter boundary",
        )
    family = _slug(value.get("family"), field="family")
    reference_period = _native_text(
        value.get("reference_period"),
        field="reference_period",
        maximum=160,
        allow_empty=True,
    )
    official_id = _native_text(value.get("official_id"), field="official_id", maximum=1_000)
    title = _native_text(value.get("title"), field="title", maximum=500)
    summary = _native_text(
        value.get("summary"),
        field="summary",
        maximum=8_000,
        allow_empty=True,
    )
    official_url = _official_url(
        value.get("official_url"),
        authority=authority,
        field="official_url",
    )
    source_url = _official_url(
        value.get("source_url"),
        authority=authority,
        field="source_url",
    )
    scheduled_at, _scheduled = _rfc3339(
        value.get("scheduled_at", ""),
        field="scheduled_at",
        allow_empty=True,
    )
    released_at, released = _rfc3339(
        value.get("released_at", ""),
        field="released_at",
        allow_empty=True,
    )
    data = _data_projection(value.get("data"))
    if released is not None and released > observed_at:
        raise _error(
            "OFFICIAL_MACRO_RELEASE_TIME_FUTURE",
            "released_at cannot be later than the observation time",
        )
    if subject_phase == "schedule" and not scheduled_at:
        if set(data) != {
            "scheduled_date_end",
            "scheduled_date_start",
            "time_precision",
        } or data.get("time_precision") != "date":
            raise _error(
                "OFFICIAL_MACRO_SCHEDULE_TIME_MISSING",
                "date-only schedules require exact bounded date projection fields",
            )
        try:
            start_date = datetime.strptime(
                data["scheduled_date_start"], "%Y-%m-%d"
            ).date()
            end_date = datetime.strptime(
                data["scheduled_date_end"], "%Y-%m-%d"
            ).date()
        except ValueError as exc:
            raise _error(
                "OFFICIAL_MACRO_SCHEDULE_TIME_INVALID",
                "date-only schedule fields must be valid ISO dates",
            ) from exc
        if end_date < start_date:
            raise _error(
                "OFFICIAL_MACRO_SCHEDULE_TIME_INVALID",
                "date-only schedule end cannot precede its start",
            )
    if subject_phase == "release" and scheduled_at:
        raise _error(
            "OFFICIAL_MACRO_RELEASE_RECORD_INVALID",
            "release records must not carry calendar schedule claims",
        )
    if subject_phase == "schedule" and released_at:
        raise _error(
            "OFFICIAL_MACRO_SCHEDULE_RECORD_INVALID",
            "schedule records must not carry release-time claims",
        )
    if subject_phase == "schedule":
        occurrence_at = (
            scheduled_at
            or f"{data['scheduled_date_start']}T00:00:00Z"
        )
        occurrence_basis = (
            "official_schedule_time"
            if scheduled_at
            else "official_date_anchor_not_exact_time"
        )
    else:
        occurrence_at = released_at or _release_reference_anchor(
            authority,
            reference_period,
        )
        if not occurrence_at:
            raise _error(
                "OFFICIAL_MACRO_OCCURRENCE_TIME_MISSING",
                "release records require an official release time or fixed reference anchor",
            )
        occurrence_basis = (
            "official_release_time"
            if released_at
            else "official_reference_period_anchor_not_release_time"
        )
        _occurrence_text, occurrence = _rfc3339(
            occurrence_at,
            field="occurrence_at",
            allow_empty=False,
        )
        if occurrence is not None and occurrence > observed_at:
            raise _error(
                "OFFICIAL_MACRO_OCCURRENCE_TIME_FUTURE",
                "release occurrence anchor cannot be later than observation time",
            )
    official_revision = value.get("official_revision", False)
    if type(official_revision) is not bool:
        raise _error(
            "OFFICIAL_MACRO_RECORD_INVALID",
            "official_revision must be a native boolean",
        )
    identity_sha = canonical_sha256({
        "version": MACRO_LIFECYCLE_VERSION,
        "authority": authority,
        "family": family,
        "reference_period": reference_period,
        "official_id": official_id,
        "subject_phase": subject_phase,
    })
    projection_basis = {
        "version": MACRO_PROJECTION_VERSION,
        "authority": authority,
        "family": family,
        "reference_period": reference_period,
        "official_id": official_id,
        "title": title,
        "summary": summary,
        "official_url": official_url,
        "source_url": source_url,
        "scheduled_at": scheduled_at,
        "released_at": released_at,
        "official_revision": official_revision,
        "occurrence_at": occurrence_at,
        "occurrence_basis": occurrence_basis,
        "data": data,
        "subject_phase": subject_phase,
    }
    data_sha = canonical_sha256({
        "version": MACRO_PROJECTION_VERSION,
        "data": data,
    })
    schedule_sha = canonical_sha256({
        "version": MACRO_PROJECTION_VERSION,
        "scheduled_at": scheduled_at,
        "schedule_data": data if subject_phase == "schedule" else {},
    })
    document_sha = canonical_sha256({
        "version": MACRO_PROJECTION_VERSION,
        "official_revision": official_revision,
        "official_url": official_url,
        "released_at": released_at,
        "source_url": source_url,
        "summary": summary,
        "title": title,
    })
    return {
        **projection_basis,
        "data_sha256": data_sha,
        "document_sha256": document_sha,
        "identity_sha256": identity_sha,
        "projection_sha256": canonical_sha256(projection_basis),
        "schedule_sha256": schedule_sha,
    }


def _macro_item(
    record: dict[str, Any],
    *,
    observed_at: datetime,
    event_state: str,
    previous_projection_sha256: str,
    revision_target: str,
) -> dict[str, Any]:
    if event_state not in MACRO_EVENT_STATES:
        raise _error("OFFICIAL_MACRO_STATE_INVALID", "macro event state is unsupported")
    authority = record["authority"]
    metadata = _AUTHORITY_METADATA[authority]
    subject_phase = record["subject_phase"]
    projection_sha = record["projection_sha256"]
    occurrence = record["occurrence_at"]
    source_published_at = record["released_at"] if subject_phase == "release" else ""
    source_type = (
        "official_macro_calendar_projection"
        if subject_phase == "schedule"
        else "official_macro_release_projection"
    )
    source_rows: list[dict[str, Any]] = []
    if record["official_url"] != record["source_url"]:
        source_rows.append({
            "url": record["official_url"],
            "publisher": metadata["publisher"],
            "source_type": "official_release_page",
            "published_at": source_published_at,
            "content_sha256": "",
        })
    source_rows.append({
        "url": record["source_url"],
        "publisher": metadata["publisher"],
        "source_type": source_type,
        "published_at": source_published_at,
        "content_sha256": projection_sha,
    })
    state_label = {
        "scheduled": "scheduled",
        "released": "released",
        "revised": "revised",
    }[event_state]
    if revision_target not in {"", "schedule_time", "data", "document"}:
        raise _error(
            "OFFICIAL_MACRO_REVISION_TARGET_INVALID",
            "macro revision target is unsupported",
        )
    fact = (
        f"{metadata['publisher']} records this {record['family']} item as {state_label}."
    )
    status_basis = (
        (
            "official_schedule_projection"
            if record["scheduled_at"]
            else "official_schedule_date_projection"
        )
        if subject_phase == "schedule"
        else (
            "official_released_at"
            if record["released_at"]
            else "official_data_observed"
        )
    )
    return {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": f"macro-{record['identity_sha256']}",
        "item_type": (
            "official_macro_schedule"
            if subject_phase == "schedule"
            else "official_macro_release"
        ),
        "severity": "info",
        "occurred_at": occurrence,
        "published_at": source_published_at,
        "entities": [{
            "kind": "institution",
            "id": authority,
            "label": metadata["label"],
        }],
        "headline": record["title"],
        "summary": record["summary"] or fact,
        "facts": [{"claim": fact, "source_indexes": list(range(len(source_rows)))}],
        "sources": source_rows,
        "impact_hypotheses": [],
        "unknowns": [
            (
                "The official calendar may change; this item does not claim that a release occurred."
                if subject_phase == "schedule"
                else "No market impact or trading implication is inferred from this official observation."
            )
        ],
        "confidence": 1.0,
        "recommended_route": "notify_only",
        "extensions": {
            "macro_official_v1": {
                "authority": authority,
                "data": record["data"],
                "event_state": event_state,
                "family": record["family"],
                "identity_sha256": record["identity_sha256"],
                "identity_version": MACRO_LIFECYCLE_VERSION,
                "official_id": record["official_id"],
                "official_revision": record["official_revision"],
                "occurrence_at": occurrence,
                "occurrence_basis": record["occurrence_basis"],
                "previous_projection_sha256": previous_projection_sha256,
                "projection_hash_semantics": "normalized_official_projection_not_web_body",
                "projection_sha256": projection_sha,
                "projection_version": MACRO_PROJECTION_VERSION,
                "reference_period": record["reference_period"],
                "released_at": record["released_at"],
                "revision_target": revision_target if event_state == "revised" else "",
                "scheduled_at": record["scheduled_at"],
                "status_basis": status_basis,
                "subject_phase": subject_phase,
            }
        },
    }


def project_macro_records(
    rows: Any,
    *,
    started_checkpoint: dict[str, Any],
    previous_projections: dict[str, dict[str, str]],
    checkpoint_version: str,
    allowed_authorities: frozenset[str],
    subject_phase: str,
    observed_at: datetime,
    candidate_limit: int,
    checkpoint_entry_limit: int = MACRO_CHECKPOINT_ENTRY_LIMIT,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], int, int, tuple[SourcePollError, ...]]:
    """Project one complete parsed window, atomically failing on ambiguity."""

    if type(rows) is not list:
        error = SourcePollError.build(
            "OFFICIAL_MACRO_ROWS_INVALID",
            "official macro client returned a non-list row collection",
            "official_macro",
        )
        return started_checkpoint, (), 0, 0, (error,)
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    rejected_count = 0
    try:
        for raw in rows:
            record = normalize_macro_record(
                raw,
                allowed_authorities=allowed_authorities,
                subject_phase=subject_phase,
                observed_at=observed_at,
            )
            identity_sha = record["identity_sha256"]
            if identity_sha not in grouped:
                order.append(identity_sha)
                grouped[identity_sha] = []
            grouped[identity_sha].append(record)
    except SourceMonitoringContractError as exc:
        error = SourcePollError.build(
            "OFFICIAL_MACRO_RECORD_REJECTED",
            str(exc)[:1_000],
            "official_macro",
        )
        return started_checkpoint, (), 0, rejected_count + 1, (error,)

    if len(grouped) > checkpoint_entry_limit:
        error = SourcePollError.build(
            "OFFICIAL_MACRO_CHECKPOINT_CAPACITY_EXCEEDED",
            (
                f"official macro poll returned {len(grouped)} unique identities; "
                f"the checkpoint capacity is {checkpoint_entry_limit}"
            ),
            "official_macro",
        )
        return started_checkpoint, (), 0, len(grouped), (error,)

    conflicts = [
        identity
        for identity, records in grouped.items()
        if len({record["projection_sha256"] for record in records}) != 1
    ]
    if conflicts:
        error = SourcePollError.build(
            "OFFICIAL_MACRO_IDENTITY_CONFLICT",
            "one official macro identity produced conflicting projections",
            "official_macro",
        )
        return started_checkpoint, (), 0, sum(len(grouped[key]) for key in conflicts), (error,)

    order = sorted(grouped)

    changed: list[tuple[dict[str, Any], str, str, str]] = []
    duplicate_count = 0
    next_entries: list[dict[str, str]] = []
    for identity_sha in order:
        group = grouped[identity_sha]
        record = group[0]
        duplicate_count += len(group) - 1
        projection_sha = record["projection_sha256"]
        previous = previous_projections.get(identity_sha, {})
        previous_sha = previous.get("projection_sha256", "")
        next_entries.append({
            "data_sha256": record["data_sha256"],
            "document_sha256": record["document_sha256"],
            "identity_sha256": identity_sha,
            "projection_sha256": projection_sha,
            "schedule_sha256": record["schedule_sha256"],
        })
        if previous_sha == projection_sha:
            duplicate_count += 1
            continue
        event_state = (
            "revised"
            if previous_sha or record["official_revision"]
            else ("scheduled" if subject_phase == "schedule" else "released")
        )
        if event_state != "revised":
            revision_target = ""
        elif previous_sha:
            if (
                subject_phase == "schedule"
                and previous.get("schedule_sha256") != record["schedule_sha256"]
            ):
                revision_target = "schedule_time"
            elif (
                subject_phase == "release"
                and previous.get("data_sha256") != record["data_sha256"]
            ):
                revision_target = "data"
            else:
                revision_target = "document"
        else:
            revision_target = (
                "schedule_time"
                if subject_phase == "schedule"
                else ("data" if record["data"] else "document")
            )
        changed.append((record, event_state, previous_sha, revision_target))

    if len(changed) > candidate_limit:
        error = SourcePollError.build(
            "OFFICIAL_MACRO_CANDIDATE_CAPACITY_EXCEEDED",
            (
                f"official macro poll produced {len(changed)} changed identities; "
                f"the sealed candidate limit is {candidate_limit}"
            ),
            "official_macro",
        )
        return started_checkpoint, (), duplicate_count, len(changed), (error,)

    items = tuple(
        _macro_item(
            record,
            observed_at=observed_at,
            event_state=event_state,
            previous_projection_sha256=previous_sha,
            revision_target=revision_target,
        )
        for record, event_state, previous_sha, revision_target in changed
    )
    next_checkpoint = {
        "version": checkpoint_version,
        "entries": next_entries,
    }
    return next_checkpoint, items, duplicate_count, rejected_count, ()


__all__ = [
    "MACRO_AUTHORITIES",
    "MACRO_CHECKPOINT_ENTRY_LIMIT",
    "MACRO_EVENT_STATES",
    "MACRO_LIFECYCLE_VERSION",
    "MACRO_PROJECTION_VERSION",
    "MACRO_SUBJECT_PHASES",
    "normalize_macro_checkpoint",
    "normalize_macro_record",
    "normalize_macro_source_errors",
    "project_macro_records",
]
