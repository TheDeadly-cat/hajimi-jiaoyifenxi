from __future__ import annotations

import copy
import re
from typing import Any, Mapping, Protocol

from .decision_lineage import canonical_sha256


ACTION_DESK_VERSION = "artifact_action_desk_v1"
ACTION_DESK_OVERVIEW_VERSION = "artifact_action_desk_overview_v1"
ACTION_DESK_ROOM_SUMMARY_VERSION = "artifact_action_room_summary_v1"
ACTION_DESK_OVERVIEW_MAX_ROOMS = 500
ACTION_DESK_OVERVIEW_MAX_ITEMS_PER_ROOM = 500
ACTION_DESK_OVERVIEW_MAX_ITEMS = 2000
ACTION_DESK_CANDIDATE_VERSION = "artifact_action_candidate_v1"
ACTION_DESK_ITEM_VERSION = "artifact_action_item_v1"
ACTION_SOURCE_VERSION = "artifact_action_source_v1"
ACTION_EVENT_VERSION = "artifact_action_event_v1"
ACTION_HEAD_VERSION = "artifact_action_head_v1"
ACTION_ANCHOR_VERSION = "artifact_action_anchor_v1"
ACTION_ANCHOR_HEAD_VERSION = "artifact_action_anchor_head_v1"
ACTION_TRANSITION_REQUEST_VERSION = "artifact_action_transition_v1"
ACTION_CONTINUATION_VERSION = "artifact_action_continuation_v1"
ACTION_CONTINUATION_ITEM_VERSION = "artifact_action_continuation_item_v1"
ACTION_CONTINUATION_RESULT_VERSION = "artifact_action_continuation_result_v1"
ACTION_CONTINUATION_SOURCE_VERSION = "artifact_action_continuation_source_v1"
ACTION_CONTINUATION_EVENT_VERSION = "artifact_action_continuation_event_v1"
ACTION_CONTINUATION_HEAD_VERSION = "artifact_action_continuation_head_v1"
ACTION_CONTINUATION_ANCHOR_VERSION = "artifact_action_continuation_anchor_v1"
ACTION_CONTINUATION_ANCHOR_HEAD_VERSION = "artifact_action_continuation_anchor_head_v1"

ACTION_STATES = frozenset({"open", "in_progress", "blocked", "done", "cancelled"})
SOURCE_ACTION_STATES = frozenset({"open", "in_progress", "blocked", "done"})
ACTION_PATCH_FIELDS = frozenset({"owner", "due", "state", "note"})
ACTION_TRANSITION_FIELDS = frozenset({
    "version",
    "client_request_id",
    "artifact_id",
    "artifact_version",
    "action_id",
    "expected_action_snapshot_sha256",
    "expected_revision",
    "transition",
    "patch",
    "user_confirmed",
})
ACTION_CONTINUATION_FIELDS = frozenset({
    "version",
    "client_request_id",
    "source_artifact_id",
    "source_artifact_version",
    "source_action_id",
    "source_action_snapshot_sha256",
    "source_expected_revision",
    "target_artifact_id",
    "target_artifact_version",
    "target_action_id",
    "target_action_snapshot_sha256",
    "reason",
    "user_confirmed",
})

FIXED_ACTION_DESK_SAFETY = {
    "execution_capability": "none",
    "external_write": False,
    "can_autonomously_decide": False,
    "can_replace_user_decision": False,
    "user_final_decision_required": True,
}

FIXED_ACTION_DESK_OVERVIEW_SAFETY = {
    **FIXED_ACTION_DESK_SAFETY,
    "ranking_produced": False,
    "winner_claim": False,
}

ACTION_DESK_COUNT_FIELDS = (
    "candidate_count",
    "item_count",
    "open_count",
    "in_progress_count",
    "blocked_count",
    "done_count",
    "cancelled_count",
)

ACTION_DESK_ITEM_FIELDS = (
    "version",
    "artifact_id",
    "artifact_version",
    "artifact_title",
    "action_id",
    "action_snapshot_sha256",
    "text",
    "owner",
    "due",
    "state",
    "evidence_count",
    "source_status",
    "revision",
    "note",
    "latest_event_id",
    "latest_event_sha256",
    "adopted_at",
    "updated_at",
    "source_current",
    "current_artifact_version",
    "integrity_ok",
)

ACTION_DESK_FIELDS = (
    "version",
    "room_id",
    "integrity_ok",
    "candidates",
    "items",
    "counts",
    "issues",
    *FIXED_ACTION_DESK_SAFETY,
)

ACTION_DESK_OVERVIEW_COUNT_FIELDS = (
    "room_count",
    "healthy_room_count",
    "failed_room_count",
    *ACTION_DESK_COUNT_FIELDS,
)

_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,120}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_SQLITE_INT64_MAX = 2**63 - 1


class ActionDeskError(ValueError):
    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class _ActionDeskStore(Protocol):
    def action_desk(self, room_id: str) -> dict[str, Any]: ...

    def transition_artifact_action(
        self,
        room_id: str,
        request: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]: ...

    def action_desk_overview(self) -> dict[str, Any]: ...

    def action_desk_continuations(self, room_id: str) -> dict[str, Any]: ...

    def transition_artifact_action_continuation(
        self,
        room_id: str,
        request: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]: ...


class ActionDeskService:
    def __init__(self, store: _ActionDeskStore) -> None:
        self.store = store

    def get(self, room_id: str) -> dict[str, Any]:
        return self.store.action_desk(room_id)

    def transition(
        self,
        room_id: str,
        request: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        return self.store.transition_artifact_action(room_id, request)

    def overview(self) -> dict[str, Any]:
        return self.store.action_desk_overview()

    def continuations(self, room_id: str) -> dict[str, Any]:
        return self.store.action_desk_continuations(room_id)

    def continue_action(
        self,
        room_id: str,
        request: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        return self.store.transition_artifact_action_continuation(room_id, request)


def empty_action_desk_counts() -> dict[str, int]:
    return {field: 0 for field in ACTION_DESK_COUNT_FIELDS}


def failed_action_desk_room_summary(
    *,
    room_id: str,
    room_title: str,
) -> dict[str, Any]:
    """Return a closed, non-leaking projection for an untrusted room desk."""

    clean_room_title = str(room_title or "").strip()
    if (
        not clean_room_title
        or len(clean_room_title) > 500
        or any(ord(character) < 32 for character in clean_room_title)
    ):
        clean_room_title = "Unavailable room"
    return {
        "version": ACTION_DESK_ROOM_SUMMARY_VERSION,
        "room_id": str(room_id or ""),
        "room_title": clean_room_title,
        "integrity_ok": False,
        "items": [],
        "counts": empty_action_desk_counts(),
        "issues": [{
            "code": "ACTION_DESK_ROOM_INTEGRITY_FAILED",
            "message": "This room's Action Desk failed integrity verification and was hidden.",
        }],
    }


def verified_action_desk_room_summary(
    *,
    room_id: str,
    room_title: str,
    desk: Mapping[str, Any],
) -> dict[str, Any]:
    """Project one fully verified room desk without inheriting future fields."""

    clean_room_id = str(room_id or "").strip()
    clean_room_title = str(room_title or "").strip()
    if (
        not _ID_PATTERN.fullmatch(clean_room_id)
        or not clean_room_title
        or len(clean_room_title) > 500
        or any(ord(character) < 32 for character in clean_room_title)
        or not isinstance(desk, Mapping)
        or set(desk) != set(ACTION_DESK_FIELDS)
        or desk.get("version") != ACTION_DESK_VERSION
        or str(desk.get("room_id") or "") != clean_room_id
        or desk.get("integrity_ok") is not True
        or any(desk.get(key) != value for key, value in FIXED_ACTION_DESK_SAFETY.items())
        or not isinstance(desk.get("candidates"), list)
        or not isinstance(desk.get("items"), list)
        or not isinstance(desk.get("counts"), Mapping)
        or set(desk.get("counts") or {}) != set(ACTION_DESK_COUNT_FIELDS)
        or not isinstance(desk.get("issues"), list)
        or desk.get("issues")
    ):
        raise ValueError("Action Desk room projection is not fully verified")

    raw_counts = desk["counts"]
    counts: dict[str, int] = {}
    for field in ACTION_DESK_COUNT_FIELDS:
        value = raw_counts.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Action Desk room count is invalid")
        counts[field] = value

    if counts["candidate_count"] != len(desk["candidates"]):
        raise ValueError("Action Desk candidate count does not match its projection")

    items: list[dict[str, Any]] = []
    identities: set[tuple[str, int, str]] = set()
    state_counts = {state: 0 for state in ACTION_STATES}
    for raw_item in desk["items"]:
        if not isinstance(raw_item, Mapping) or set(raw_item) != set(ACTION_DESK_ITEM_FIELDS):
            raise ValueError("Action Desk item shape is invalid")
        item = {field: copy.deepcopy(raw_item.get(field)) for field in ACTION_DESK_ITEM_FIELDS}
        artifact_id = str(item.get("artifact_id") or "")
        action_id = str(item.get("action_id") or "")
        artifact_version = item.get("artifact_version")
        revision = item.get("revision")
        adopted_at = item.get("adopted_at")
        updated_at = item.get("updated_at")
        current_artifact_version = item.get("current_artifact_version")
        evidence_count = item.get("evidence_count")
        if (
            item.get("version") != ACTION_DESK_ITEM_VERSION
            or item.get("integrity_ok") is not True
            or item.get("source_status") != "confirmed_exact"
            or not _ID_PATTERN.fullmatch(artifact_id)
            or not _ID_PATTERN.fullmatch(action_id)
            or not is_sha256(item.get("action_snapshot_sha256"))
            or not is_sha256(item.get("latest_event_sha256"))
            or not _ID_PATTERN.fullmatch(str(item.get("latest_event_id") or ""))
            or item.get("state") not in ACTION_STATES
            or isinstance(artifact_version, bool)
            or not isinstance(artifact_version, int)
            or artifact_version < 1
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
            or isinstance(adopted_at, bool)
            or not isinstance(adopted_at, int)
            or adopted_at < 1
            or isinstance(updated_at, bool)
            or not isinstance(updated_at, int)
            or updated_at < adopted_at
            or isinstance(current_artifact_version, bool)
            or not isinstance(current_artifact_version, int)
            or current_artifact_version < 1
            or isinstance(evidence_count, bool)
            or not isinstance(evidence_count, int)
            or not 0 <= evidence_count <= 20
            or not isinstance(item.get("source_current"), bool)
            or not isinstance(item.get("artifact_title"), str)
            or not 0 < len(item["artifact_title"]) <= 500
            or not isinstance(item.get("text"), str)
            or not 0 < len(item["text"]) <= 3000
            or not isinstance(item.get("owner"), str)
            or len(item["owner"]) > 120
            or not isinstance(item.get("due"), str)
            or len(item["due"]) > 80
            or not isinstance(item.get("note"), str)
            or len(item["note"]) > 4000
        ):
            raise ValueError("Action Desk item projection is invalid")
        identity = (artifact_id, artifact_version, action_id)
        if identity in identities:
            raise ValueError("Action Desk item identity is duplicated")
        identities.add(identity)
        state_counts[str(item["state"])] += 1
        items.append(item)

    items.sort(key=lambda item: (
        -int(item["updated_at"]),
        clean_room_id,
        str(item["artifact_id"]),
        int(item["artifact_version"]),
        str(item["action_id"]),
    ))
    if (
        counts["item_count"] != len(items)
        or any(
            counts[f"{state}_count"] != state_counts[state]
            for state in ACTION_STATES
        )
    ):
        raise ValueError("Action Desk item counts do not match verified rows")

    return {
        "version": ACTION_DESK_ROOM_SUMMARY_VERSION,
        "room_id": clean_room_id,
        "room_title": clean_room_title,
        "integrity_ok": True,
        "items": items,
        "counts": counts,
        "issues": [],
    }


def is_sha256(value: Any) -> bool:
    return bool(_SHA256_PATTERN.fullmatch(str(value or "").strip().lower()))


def normalize_transition_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ActionDeskError(
            "Action Desk transition request must be an object.",
            code="ACTION_DESK_REQUEST_INVALID",
            status=400,
        )
    unknown_fields = sorted(set(request) - ACTION_TRANSITION_FIELDS)
    missing_fields = sorted(ACTION_TRANSITION_FIELDS - set(request))
    if unknown_fields or missing_fields:
        raise ActionDeskError(
            "Action Desk transition request fields do not match the v1 contract.",
            code="ACTION_DESK_REQUEST_INVALID",
            status=400,
        )
    if request.get("version") != ACTION_TRANSITION_REQUEST_VERSION:
        raise ActionDeskError(
            "Action Desk transition request version is unsupported.",
            code="ACTION_DESK_REQUEST_VERSION_UNSUPPORTED",
            status=400,
        )
    client_request_id = str(request.get("client_request_id") or "").strip()
    artifact_id = str(request.get("artifact_id") or "").strip()
    action_id = str(request.get("action_id") or "").strip()
    if not _ID_PATTERN.fullmatch(client_request_id):
        raise ActionDeskError(
            "client_request_id is invalid.",
            code="ACTION_DESK_REQUEST_INVALID",
            status=400,
        )
    if not _ID_PATTERN.fullmatch(artifact_id) or not _ID_PATTERN.fullmatch(action_id):
        raise ActionDeskError(
            "Artifact or action identity is invalid.",
            code="ACTION_DESK_SOURCE_INVALID",
            status=400,
        )
    if isinstance(request.get("artifact_version"), bool) or isinstance(
        request.get("expected_revision"), bool
    ):
        raise ActionDeskError(
            "Artifact version and expected revision must be integers.",
            code="ACTION_DESK_REQUEST_INVALID",
            status=400,
        )
    try:
        artifact_version = int(request.get("artifact_version"))
        expected_revision = int(request.get("expected_revision"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ActionDeskError(
            "Artifact version and expected revision must be integers.",
            code="ACTION_DESK_REQUEST_INVALID",
            status=400,
        ) from exc
    if not 1 <= artifact_version <= _SQLITE_INT64_MAX or not 0 <= expected_revision <= _SQLITE_INT64_MAX:
        raise ActionDeskError(
            "Artifact version or expected revision is outside the supported range.",
            code="ACTION_DESK_REQUEST_INVALID",
            status=400,
        )
    expected_sha256 = str(request.get("expected_action_snapshot_sha256") or "").strip().lower()
    if not is_sha256(expected_sha256):
        raise ActionDeskError(
            "Expected action snapshot seal is invalid.",
            code="ACTION_DESK_SOURCE_INVALID",
            status=400,
        )
    transition = str(request.get("transition") or "").strip().lower()
    if transition not in {"adopt", "update"}:
        raise ActionDeskError(
            "Action Desk transition must be adopt or update.",
            code="ACTION_DESK_TRANSITION_INVALID",
            status=400,
        )
    if request.get("user_confirmed") is not True:
        raise ActionDeskError(
            "Action Desk transitions require explicit user confirmation.",
            code="ACTION_DESK_USER_CONFIRMATION_REQUIRED",
            status=400,
        )
    raw_patch = request.get("patch")
    if not isinstance(raw_patch, Mapping):
        raise ActionDeskError(
            "Action Desk patch must be an object.",
            code="ACTION_DESK_PATCH_INVALID",
            status=400,
        )
    unknown_patch_fields = sorted(set(raw_patch) - ACTION_PATCH_FIELDS)
    if unknown_patch_fields:
        raise ActionDeskError(
            "Action Desk patch contains fields that cannot be changed.",
            code="ACTION_DESK_PATCH_INVALID",
            status=400,
        )
    if transition == "adopt" and expected_revision != 0:
        raise ActionDeskError(
            "Adopt requires expected_revision=0.",
            code="ACTION_DESK_TRANSITION_INVALID",
            status=409,
        )
    if transition == "update" and (expected_revision < 1 or not raw_patch):
        raise ActionDeskError(
            "Update requires a positive expected revision and a non-empty patch.",
            code="ACTION_DESK_TRANSITION_INVALID",
            status=409,
        )
    patch: dict[str, Any] = {}
    for field in sorted(raw_patch):
        value = raw_patch[field]
        if field == "state":
            state = str(value or "").strip().lower()
            if state not in ACTION_STATES:
                raise ActionDeskError(
                    "Action Desk state is invalid.",
                    code="ACTION_DESK_PATCH_INVALID",
                    status=400,
                )
            patch[field] = state
            continue
        if not isinstance(value, str):
            raise ActionDeskError(
                f"Action Desk {field} must be a string.",
                code="ACTION_DESK_PATCH_INVALID",
                status=400,
            )
        clean_value = value.strip()
        limit = {"owner": 120, "due": 80, "note": 4000}[field]
        if len(clean_value) > limit:
            raise ActionDeskError(
                f"Action Desk {field} is too long.",
                code="ACTION_DESK_PATCH_INVALID",
                status=400,
            )
        patch[field] = clean_value
    return {
        "version": ACTION_TRANSITION_REQUEST_VERSION,
        "client_request_id": client_request_id,
        "artifact_id": artifact_id,
        "artifact_version": artifact_version,
        "action_id": action_id,
        "expected_action_snapshot_sha256": expected_sha256,
        "expected_revision": expected_revision,
        "transition": transition,
        "patch": patch,
        "user_confirmed": True,
    }


def transition_semantics(room_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": ACTION_TRANSITION_REQUEST_VERSION,
        "room_id": str(room_id or "").strip(),
        **copy.deepcopy(dict(request)),
    }


def normalize_continuation_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(request, Mapping):
        raise ActionDeskError(
            "Action Desk continuation request must be an object.",
            code="ACTION_CONTINUATION_REQUEST_INVALID",
            status=400,
        )
    unknown_fields = sorted(set(request) - ACTION_CONTINUATION_FIELDS)
    missing_fields = sorted(ACTION_CONTINUATION_FIELDS - set(request))
    if unknown_fields or missing_fields:
        raise ActionDeskError(
            "Action Desk continuation request fields do not match the v1 contract.",
            code="ACTION_CONTINUATION_REQUEST_INVALID",
            status=400,
        )
    if request.get("version") != ACTION_CONTINUATION_VERSION:
        raise ActionDeskError(
            "Action Desk continuation request version is unsupported.",
            code="ACTION_CONTINUATION_VERSION_UNSUPPORTED",
            status=400,
        )
    text_fields = (
        "client_request_id",
        "source_artifact_id",
        "source_action_id",
        "target_artifact_id",
        "target_action_id",
    )
    clean_texts = {
        field: str(request.get(field) or "").strip()
        for field in text_fields
    }
    if not _ID_PATTERN.fullmatch(clean_texts["client_request_id"]):
        raise ActionDeskError(
            "client_request_id is invalid.",
            code="ACTION_CONTINUATION_REQUEST_INVALID",
            status=400,
        )
    for field in text_fields[1:]:
        if not _ID_PATTERN.fullmatch(clean_texts[field]):
            raise ActionDeskError(
                "Action Desk continuation identity is invalid.",
                code="ACTION_CONTINUATION_SOURCE_INVALID",
                status=400,
            )
    integer_fields = (
        "source_artifact_version",
        "source_expected_revision",
        "target_artifact_version",
    )
    clean_integers: dict[str, int] = {}
    for field in integer_fields:
        if isinstance(request.get(field), bool):
            raise ActionDeskError(
                "Action Desk continuation versions and revision must be integers.",
                code="ACTION_CONTINUATION_REQUEST_INVALID",
                status=400,
            )
        try:
            value = int(request.get(field))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ActionDeskError(
                "Action Desk continuation versions and revision must be integers.",
                code="ACTION_CONTINUATION_REQUEST_INVALID",
                status=400,
            ) from exc
        if field == "source_expected_revision":
            valid = 1 <= value <= _SQLITE_INT64_MAX
        else:
            valid = 1 <= value <= _SQLITE_INT64_MAX
        if not valid:
            raise ActionDeskError(
                "Action Desk continuation version or revision is outside the supported range.",
                code="ACTION_CONTINUATION_REQUEST_INVALID",
                status=400,
            )
        clean_integers[field] = value
    hash_fields = (
        "source_action_snapshot_sha256",
        "target_action_snapshot_sha256",
    )
    clean_hashes = {
        field: str(request.get(field) or "").strip().lower()
        for field in hash_fields
    }
    if any(not is_sha256(value) for value in clean_hashes.values()):
        raise ActionDeskError(
            "Action Desk continuation source seal is invalid.",
            code="ACTION_CONTINUATION_SOURCE_INVALID",
            status=400,
        )
    reason = request.get("reason")
    if not isinstance(reason, str):
        raise ActionDeskError(
            "Action Desk continuation reason must be a string.",
            code="ACTION_CONTINUATION_REQUEST_INVALID",
            status=400,
        )
    reason = reason.strip()
    if len(reason) > 4000 or any(ord(character) < 32 for character in reason):
        raise ActionDeskError(
            "Action Desk continuation reason is invalid.",
            code="ACTION_CONTINUATION_REQUEST_INVALID",
            status=400,
        )
    if request.get("user_confirmed") is not True:
        raise ActionDeskError(
            "Action Desk continuation requires explicit user confirmation.",
            code="ACTION_CONTINUATION_USER_CONFIRMATION_REQUIRED",
            status=400,
        )
    return {
        "version": ACTION_CONTINUATION_VERSION,
        "client_request_id": clean_texts["client_request_id"],
        "source_artifact_id": clean_texts["source_artifact_id"],
        "source_artifact_version": clean_integers["source_artifact_version"],
        "source_action_id": clean_texts["source_action_id"],
        "source_action_snapshot_sha256": clean_hashes["source_action_snapshot_sha256"],
        "source_expected_revision": clean_integers["source_expected_revision"],
        "target_artifact_id": clean_texts["target_artifact_id"],
        "target_artifact_version": clean_integers["target_artifact_version"],
        "target_action_id": clean_texts["target_action_id"],
        "target_action_snapshot_sha256": clean_hashes["target_action_snapshot_sha256"],
        "reason": reason,
        "user_confirmed": True,
    }


def continuation_semantics(room_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": ACTION_CONTINUATION_VERSION,
        "room_id": str(room_id or "").strip(),
        **copy.deepcopy(dict(request)),
    }


def continuation_event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(event.get(field))
        for field in (
            "event_version",
            "id",
            "room_id",
            "source_artifact_id",
            "source_artifact_version",
            "source_action_id",
            "source_action_snapshot_sha256",
            "source_revision",
            "target_artifact_id",
            "target_artifact_version",
            "target_action_id",
            "target_action_snapshot_sha256",
            "client_request_id",
            "request_semantics",
            "request_semantics_sha256",
            "previous_event_sha256",
            "reason",
            "created_at",
        )
    }


def continuation_head_payload(head: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(head.get(field))
        for field in (
            "head_version",
            "room_id",
            "source_artifact_id",
            "source_artifact_version",
            "source_action_id",
            "source_action_snapshot_sha256",
            "source_revision",
            "target_artifact_id",
            "target_artifact_version",
            "target_action_id",
            "target_action_snapshot_sha256",
            "head_event_sha256",
            "created_at",
            "updated_at",
        )
    }


def continuation_anchor_payload(anchor: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(anchor.get(field))
        for field in (
            "anchor_version",
            "id",
            "room_id",
            "source_artifact_id",
            "source_artifact_version",
            "source_action_id",
            "source_action_snapshot_sha256",
            "source_revision",
            "target_artifact_id",
            "target_artifact_version",
            "target_action_id",
            "target_action_snapshot_sha256",
            "client_request_id",
            "request_semantics_sha256",
            "event_sha256",
            "head_sha256",
            "created_at",
        )
    }


def continuation_anchor_head_payload(head: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(head.get(field))
        for field in (
            "head_version",
            "room_id",
            "source_artifact_id",
            "source_artifact_version",
            "source_action_id",
            "source_action_snapshot_sha256",
            "source_revision",
            "head_target_artifact_id",
            "head_target_artifact_version",
            "head_target_action_id",
            "head_target_action_snapshot_sha256",
            "head_event_sha256",
            "head_anchor_sha256",
            "created_at",
            "updated_at",
        )
    }


def public_continuation_relation(
    source: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    relation_id: str,
    source_revision: int,
    created_at: int,
    reason: str,
    integrity_ok: bool = True,
) -> dict[str, Any]:
    def candidate(value: Mapping[str, Any]) -> dict[str, Any]:
        public = public_candidate(value)
        if not integrity_ok:
            return {
                key: ("" if isinstance(public.get(key), str) else 0)
                for key in public
            }
        return public
    return {
        "version": ACTION_CONTINUATION_ITEM_VERSION,
        "relation_id": str(relation_id or "") if integrity_ok else "",
        "source": candidate(source),
        "target": candidate(target),
        "source_revision": int(source_revision) if integrity_ok else 0,
        "created_at": int(created_at) if integrity_ok else 0,
        "reason": str(reason or "") if integrity_ok else "",
        "integrity_ok": bool(integrity_ok),
    }


def build_action_source(
    *,
    room_id: str,
    artifact_id: str,
    artifact_version: int,
    artifact_title: str,
    artifact_snapshot_sha256: str,
    action: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(action, Mapping):
        raise ValueError("artifact action is not an object")
    action_id = str(action.get("id") or "").strip()
    text = str(action.get("text") or "").strip()
    owner = str(action.get("owner") or "").strip()
    due = str(action.get("due") or "").strip()
    state = str(action.get("state") or "open").strip().lower()
    evidence = action.get("evidence")
    if (
        not _ID_PATTERN.fullmatch(action_id)
        or not text
        or len(text) > 3000
        or len(owner) > 120
        or len(due) > 80
        or state not in SOURCE_ACTION_STATES
        or not isinstance(evidence, list)
        or len(evidence) > 20
        or any(not isinstance(item, Mapping) for item in evidence)
    ):
        raise ValueError("artifact action does not match the persisted action contract")
    return {
        "version": ACTION_SOURCE_VERSION,
        "room_id": str(room_id or ""),
        "artifact_id": str(artifact_id or ""),
        "artifact_version": int(artifact_version),
        "artifact_title": str(artifact_title or "")[:500],
        "artifact_snapshot_sha256": str(artifact_snapshot_sha256 or "").lower(),
        "action": {
            "id": action_id,
            "text": text,
            "owner": owner,
            "due": due,
            "state": state,
            "evidence": copy.deepcopy(list(evidence)),
        },
    }


def action_snapshot_sha256(source: Mapping[str, Any]) -> str:
    return canonical_sha256(copy.deepcopy(dict(source)))


def public_candidate(source: Mapping[str, Any]) -> dict[str, Any]:
    action = source.get("action") if isinstance(source.get("action"), Mapping) else {}
    evidence = action.get("evidence") if isinstance(action.get("evidence"), list) else []
    return {
        "version": ACTION_DESK_CANDIDATE_VERSION,
        "artifact_id": str(source.get("artifact_id") or ""),
        "artifact_version": int(source.get("artifact_version") or 0),
        "artifact_title": str(source.get("artifact_title") or ""),
        "action_id": str(action.get("id") or ""),
        "action_snapshot_sha256": action_snapshot_sha256(source),
        "text": str(action.get("text") or ""),
        "owner": str(action.get("owner") or ""),
        "due": str(action.get("due") or ""),
        "state": str(action.get("state") or "open"),
        "evidence_count": len(evidence),
        "source_status": "confirmed_exact",
    }


def initial_item_state(source: Mapping[str, Any]) -> dict[str, Any]:
    action = source.get("action") if isinstance(source.get("action"), Mapping) else {}
    return {
        "owner": str(action.get("owner") or ""),
        "due": str(action.get("due") or ""),
        "state": str(action.get("state") or "open"),
        "note": "",
    }


def apply_item_patch(state: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "owner": str(state.get("owner") or ""),
        "due": str(state.get("due") or ""),
        "state": str(state.get("state") or "open"),
        "note": str(state.get("note") or ""),
    }
    result.update(copy.deepcopy(dict(patch)))
    return result


def action_event_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(event.get(field))
        for field in (
            "event_version",
            "id",
            "room_id",
            "artifact_id",
            "artifact_version",
            "action_id",
            "action_snapshot_sha256",
            "sequence_no",
            "revision",
            "transition",
            "patch",
            "item_snapshot",
            "item_snapshot_sha256",
            "client_request_id",
            "request_semantics",
            "request_semantics_sha256",
            "previous_event_sha256",
            "created_at",
        )
    }


def action_head_payload(head: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(head.get(field))
        for field in (
            "head_version",
            "room_id",
            "artifact_id",
            "artifact_version",
            "action_id",
            "action_snapshot_sha256",
            "revision",
            "sequence_no",
            "event_count",
            "head_event_sha256",
            "created_at",
            "updated_at",
        )
    }


def action_anchor_payload(anchor: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(anchor.get(field))
        for field in (
            "anchor_version",
            "id",
            "room_id",
            "artifact_id",
            "artifact_version",
            "action_id",
            "action_snapshot_sha256",
            "sequence_no",
            "revision",
            "event_count",
            "client_request_id",
            "request_semantics_sha256",
            "event_sha256",
            "action_head_sha256",
            "previous_anchor_sha256",
            "created_at",
        )
    }


def action_anchor_head_payload(head: Mapping[str, Any]) -> dict[str, Any]:
    return {
        field: copy.deepcopy(head.get(field))
        for field in (
            "head_version",
            "room_id",
            "artifact_id",
            "artifact_version",
            "action_id",
            "action_snapshot_sha256",
            "anchor_count",
            "head_sequence",
            "head_revision",
            "head_anchor_sha256",
            "head_event_sha256",
            "head_action_sha256",
            "created_at",
            "updated_at",
        )
    }


def public_item(
    source: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    revision: int,
    latest_event_id: str,
    latest_event_sha256: str,
    adopted_at: int,
    updated_at: int,
    source_current: bool,
    current_artifact_version: int,
    integrity_ok: bool,
) -> dict[str, Any]:
    action = source.get("action") if isinstance(source.get("action"), Mapping) else {}
    evidence = action.get("evidence") if isinstance(action.get("evidence"), list) else []
    if not integrity_ok:
        action = {}
        evidence = []
        state = {}
    return {
        "version": ACTION_DESK_ITEM_VERSION,
        "artifact_id": str(source.get("artifact_id") or ""),
        "artifact_version": int(source.get("artifact_version") or 0),
        "artifact_title": str(source.get("artifact_title") or "") if integrity_ok else "",
        "action_id": str(action.get("id") or "") if integrity_ok else "",
        "action_snapshot_sha256": action_snapshot_sha256(source) if integrity_ok else "",
        "text": str(action.get("text") or ""),
        "owner": str(state.get("owner") or ""),
        "due": str(state.get("due") or ""),
        "state": str(state.get("state") or ""),
        "evidence_count": len(evidence),
        "source_status": "confirmed_exact" if integrity_ok else "integrity_failed",
        "revision": int(revision) if integrity_ok else 0,
        "note": str(state.get("note") or ""),
        "latest_event_id": str(latest_event_id or "") if integrity_ok else "",
        "latest_event_sha256": str(latest_event_sha256 or "") if integrity_ok else "",
        "adopted_at": int(adopted_at) if integrity_ok else 0,
        "updated_at": int(updated_at) if integrity_ok else 0,
        "source_current": bool(source_current) if integrity_ok else False,
        "current_artifact_version": int(current_artifact_version) if integrity_ok else 0,
        "integrity_ok": bool(integrity_ok),
    }


def redacted_item(identity: Mapping[str, Any]) -> dict[str, Any]:
    source = {
        "artifact_id": str(identity.get("artifact_id") or ""),
        "artifact_version": max(0, _safe_int(identity.get("artifact_version"))),
        "artifact_title": "",
        "action": {},
    }
    return public_item(
        source,
        {},
        revision=0,
        latest_event_id="",
        latest_event_sha256="",
        adopted_at=0,
        updated_at=0,
        source_current=False,
        current_artifact_version=0,
        integrity_ok=False,
    )


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
