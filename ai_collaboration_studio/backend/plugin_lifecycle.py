from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from .decision_lineage import canonical_sha256
from .plugin_registry import FIXED_PLUGIN_SAFETY, plugin_registry_catalog


PLUGIN_LIFECYCLE_CATALOG_VERSION = "plugin_lifecycle_catalog_v1"
PLUGIN_LIFECYCLE_EVENT_VERSION_V1 = "plugin_lifecycle_event_v1"
PLUGIN_LIFECYCLE_EVENT_VERSION = "plugin_lifecycle_event_v2"
PLUGIN_LIFECYCLE_EVENT_VERSIONS = {
    PLUGIN_LIFECYCLE_EVENT_VERSION_V1,
    PLUGIN_LIFECYCLE_EVENT_VERSION,
}
PLUGIN_LIFECYCLE_HEAD_VERSION = "plugin_lifecycle_head_v1"
PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION = "plugin_lifecycle_preview_request_v1"
PLUGIN_LIFECYCLE_PREVIEW_VERSION_V1 = "plugin_lifecycle_impact_preview_v1"
PLUGIN_LIFECYCLE_PREVIEW_VERSION = "plugin_lifecycle_impact_preview_v2"
PLUGIN_LIFECYCLE_PREVIEW_VERSIONS = {
    PLUGIN_LIFECYCLE_PREVIEW_VERSION_V1,
    PLUGIN_LIFECYCLE_PREVIEW_VERSION,
}
PLUGIN_LIFECYCLE_RESOLUTION_VERSION = "plugin_lifecycle_resolution_v1"
PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION = "plugin_lifecycle_transition_request_v1"
PLUGIN_LIFECYCLE_TARGET_VERSION = "plugin_lifecycle_target_v1"

TARGET_KINDS = {"capability_pack", "domain_adapter", "ui_contribution"}
CATALOG_STATES = {"active", "deprecated", "tombstoned"}
ACTIVATION_STATES = {"enabled", "disabled", "quarantined"}
LIFECYCLE_ACTIONS = {
    "disable",
    "enable",
    "quarantine",
    "clear_quarantine",
    "deprecate",
    "reinstate",
    "tombstone",
}
LIFECYCLE_ACTIONS_REQUIRING_IMPLEMENTATION = {
    "enable",
    "clear_quarantine",
    "reinstate",
}

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
TARGET_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}")
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
CLIENT_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}")
LIFECYCLE_RESOLUTION_BINDING_SOURCES = {
    "p25_room_migration",
    "room_create",
    "room_settings_update",
    "round_create",
    "room_runtime_projection",
}
LIFECYCLE_RESOLUTION_POLICY = {
    "exact_target_version_and_hash": True,
    "all_targets_must_be_ready": True,
    "silent_replacement_allowed": False,
    "historical_snapshot_mutation_allowed": False,
}


class PluginLifecycleError(ValueError):
    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _fail(message: str, code: str, *, status: int = 400) -> None:
    raise PluginLifecycleError(message, code=code, status=status)


def _strict_object(value: Any, *, allowed: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{field} 必须是对象。", "PLUGIN_LIFECYCLE_REQUEST_INVALID")
    unknown = set(value) - allowed
    if unknown:
        _fail(
            f"{field} 包含未知字段：{', '.join(sorted(unknown))}。",
            "PLUGIN_LIFECYCLE_REQUEST_INVALID",
        )
    return value


def _target_ref(value: Any, *, field: str = "target") -> dict[str, str]:
    raw = _strict_object(
        value,
        allowed={"kind", "id", "version", "sha256"},
        field=field,
    )
    kind = str(raw.get("kind") or "").strip().lower()
    target_id = str(raw.get("id") or "").strip()
    version = str(raw.get("version") or "").strip()
    sha256 = str(raw.get("sha256") or "").strip().lower()
    if kind not in TARGET_KINDS:
        _fail(f"{field}.kind 不受支持。", "PLUGIN_LIFECYCLE_TARGET_INVALID")
    if not TARGET_ID_PATTERN.fullmatch(target_id):
        _fail(f"{field}.id 无效。", "PLUGIN_LIFECYCLE_TARGET_INVALID")
    if not VERSION_PATTERN.fullmatch(version):
        _fail(f"{field}.version 必须是完整语义版本。", "PLUGIN_LIFECYCLE_TARGET_INVALID")
    if not SHA256_PATTERN.fullmatch(sha256):
        _fail(f"{field}.sha256 无效。", "PLUGIN_LIFECYCLE_TARGET_INVALID")
    return {"kind": kind, "id": target_id, "version": version, "sha256": sha256}


def normalize_lifecycle_target_ref(
    value: Any,
    *,
    field: str = "target",
) -> dict[str, str]:
    """Normalize one exact lifecycle target identity at a trust boundary."""

    return _target_ref(value, field=field)


def _head_expectation(raw: dict[str, Any]) -> tuple[int, str]:
    try:
        sequence = int(raw.get("expected_head_sequence"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise PluginLifecycleError(
            "expected_head_sequence 必须是非负整数。",
            code="PLUGIN_LIFECYCLE_HEAD_INVALID",
        ) from exc
    sha256 = str(raw.get("expected_head_sha256") or "").strip().lower()
    if sequence < 0 or sequence > 2**63 - 1 or not SHA256_PATTERN.fullmatch(sha256):
        _fail("生命周期 head 期望值无效。", "PLUGIN_LIFECYCLE_HEAD_INVALID")
    return sequence, sha256


def _action(value: Any) -> str:
    action = str(value or "").strip().lower()
    if action not in LIFECYCLE_ACTIONS:
        _fail("生命周期动作不受支持。", "PLUGIN_LIFECYCLE_ACTION_INVALID")
    return action


def normalize_preview_request(value: Any) -> dict[str, Any]:
    raw = _strict_object(
        value,
        allowed={
            "version",
            "target",
            "action",
            "expected_head_sequence",
            "expected_head_sha256",
            "replacement",
        },
        field="request",
    )
    if raw.get("version") != PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION:
        _fail("生命周期预览请求版本不受支持。", "PLUGIN_LIFECYCLE_REQUEST_VERSION_UNSUPPORTED")
    sequence, head_sha256 = _head_expectation(raw)
    replacement = raw.get("replacement")
    return {
        "version": PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION,
        "target": _target_ref(raw.get("target")),
        "action": _action(raw.get("action")),
        "expected_head_sequence": sequence,
        "expected_head_sha256": head_sha256,
        "replacement": (
            _target_ref(replacement, field="replacement")
            if replacement is not None
            else None
        ),
    }


def normalize_transition_request(value: Any) -> dict[str, Any]:
    raw = _strict_object(
        value,
        allowed={
            "version",
            "client_request_id",
            "target",
            "action",
            "expected_head_sequence",
            "expected_head_sha256",
            "replacement",
            "impact_preview_sha256",
            "reason",
            "user_confirmed_history_preserved",
            "user_confirmed_no_automatic_migration",
        },
        field="request",
    )
    if raw.get("version") != PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION:
        _fail("生命周期变更请求版本不受支持。", "PLUGIN_LIFECYCLE_REQUEST_VERSION_UNSUPPORTED")
    client_request_id = str(raw.get("client_request_id") or "").strip()
    if not CLIENT_REQUEST_ID_PATTERN.fullmatch(client_request_id):
        _fail("client_request_id 无效。", "PLUGIN_LIFECYCLE_CLIENT_REQUEST_ID_INVALID")
    sequence, head_sha256 = _head_expectation(raw)
    preview_sha256 = str(raw.get("impact_preview_sha256") or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(preview_sha256):
        _fail("影响预览封印无效。", "PLUGIN_LIFECYCLE_PREVIEW_SEAL_INVALID")
    reason = " ".join(str(raw.get("reason") or "").split())
    if len(reason) < 4 or len(reason) > 500:
        _fail("生命周期变更原因必须为 4–500 个字符。", "PLUGIN_LIFECYCLE_REASON_INVALID")
    if raw.get("user_confirmed_history_preserved") is not True:
        _fail("必须确认历史记录只读保留。", "PLUGIN_LIFECYCLE_CONFIRMATION_REQUIRED")
    if raw.get("user_confirmed_no_automatic_migration") is not True:
        _fail("必须确认不会自动迁移或替换能力包。", "PLUGIN_LIFECYCLE_CONFIRMATION_REQUIRED")
    replacement = raw.get("replacement")
    return {
        "version": PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION,
        "client_request_id": client_request_id,
        "target": _target_ref(raw.get("target")),
        "action": _action(raw.get("action")),
        "expected_head_sequence": sequence,
        "expected_head_sha256": head_sha256,
        "replacement": (
            _target_ref(replacement, field="replacement")
            if replacement is not None
            else None
        ),
        "impact_preview_sha256": preview_sha256,
        "reason": reason,
        "user_confirmed_history_preserved": True,
        "user_confirmed_no_automatic_migration": True,
    }


def transition_semantics(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION,
        "target": deepcopy(request["target"]),
        "action": request["action"],
        "expected_head_sequence": int(request["expected_head_sequence"]),
        "expected_head_sha256": request["expected_head_sha256"],
        "replacement": deepcopy(request.get("replacement")),
        "impact_preview_sha256": request["impact_preview_sha256"],
        "reason": request["reason"],
        "user_confirmed_history_preserved": True,
        "user_confirmed_no_automatic_migration": True,
    }


def plugin_lifecycle_targets() -> list[dict[str, Any]]:
    catalog = plugin_registry_catalog()
    packs = {str(item["id"]): item for item in catalog["capability_packs"]}
    rows: list[dict[str, Any]] = []
    for pack_id, pack in packs.items():
        rows.append({
            "kind": "capability_pack",
            "id": pack_id,
            "version": str(pack["pack_version"]),
            "sha256": str(pack["manifest_sha256"]),
            "label": str(pack.get("name") or pack_id),
            "system_managed": pack.get("system_managed") is True,
            "owner_pack_ids": [pack_id],
            "dependencies": list(pack.get("dependencies") or []),
            "snapshot": deepcopy(pack),
        })
    for adapter in catalog["domain_adapters"]:
        adapter_id = str(adapter["adapter_id"])
        owner_pack_ids = [str(item) for item in adapter.get("pack_ids") or []]
        rows.append({
            "kind": "domain_adapter",
            "id": adapter_id,
            "version": str(adapter["adapter_version"]),
            "sha256": str(adapter["contract_sha256"]),
            "label": adapter_id,
            "system_managed": bool(owner_pack_ids) and all(
                packs.get(pack_id, {}).get("system_managed") is True
                for pack_id in owner_pack_ids
            ),
            "owner_pack_ids": owner_pack_ids,
            "dependencies": [],
            "snapshot": deepcopy(adapter),
        })
    for contribution in catalog["ui_contributions"]:
        contribution_id = str(contribution["contribution_id"])
        owner_pack_id = str(contribution.get("pack_id") or "")
        rows.append({
            "kind": "ui_contribution",
            "id": contribution_id,
            "version": str(contribution["contribution_version"]),
            "sha256": str(contribution["contract_sha256"]),
            "label": str(contribution.get("label") or contribution_id),
            "system_managed": packs.get(owner_pack_id, {}).get("system_managed") is True,
            "owner_pack_ids": [owner_pack_id] if owner_pack_id else [],
            "dependencies": [],
            "snapshot": deepcopy(contribution),
        })
    return rows


def initial_lifecycle_state() -> dict[str, str]:
    return {
        "catalog_state": "active",
        "activation_state": "enabled",
        "resume_activation_state": "enabled",
    }


def apply_lifecycle_action(state: dict[str, Any], action: str) -> dict[str, str]:
    catalog_state = str(state.get("catalog_state") or "")
    activation_state = str(state.get("activation_state") or "")
    resume_state = str(state.get("resume_activation_state") or "enabled")
    if catalog_state not in CATALOG_STATES or activation_state not in ACTIVATION_STATES:
        _fail("生命周期当前状态无效。", "PLUGIN_LIFECYCLE_STATE_INVALID")
    if catalog_state == "tombstoned":
        _fail("墓碑状态不可恢复或改写。", "PLUGIN_LIFECYCLE_TOMBSTONE_TERMINAL", status=409)

    if action == "disable":
        if activation_state != "enabled":
            _fail("只有已启用目标可以停用。", "PLUGIN_LIFECYCLE_TRANSITION_INVALID", status=409)
        activation_state = "disabled"
        resume_state = "disabled"
    elif action == "enable":
        if activation_state != "disabled":
            _fail("只有已停用目标可以恢复启用。", "PLUGIN_LIFECYCLE_TRANSITION_INVALID", status=409)
        activation_state = "enabled"
        resume_state = "enabled"
    elif action == "quarantine":
        if activation_state not in {"enabled", "disabled"}:
            _fail("目标已经处于隔离状态。", "PLUGIN_LIFECYCLE_TRANSITION_INVALID", status=409)
        resume_state = activation_state
        activation_state = "quarantined"
    elif action == "clear_quarantine":
        if activation_state != "quarantined" or resume_state not in {"enabled", "disabled"}:
            _fail("目标当前不处于可解除的隔离状态。", "PLUGIN_LIFECYCLE_TRANSITION_INVALID", status=409)
        activation_state = resume_state
    elif action == "deprecate":
        if catalog_state != "active":
            _fail("只有正常目录目标可以标记弃用。", "PLUGIN_LIFECYCLE_TRANSITION_INVALID", status=409)
        catalog_state = "deprecated"
    elif action == "reinstate":
        if catalog_state != "deprecated":
            _fail("只有已弃用目标可以恢复目录状态。", "PLUGIN_LIFECYCLE_TRANSITION_INVALID", status=409)
        catalog_state = "active"
    elif action == "tombstone":
        catalog_state = "tombstoned"
        activation_state = "disabled"
        resume_state = "disabled"
    else:  # pragma: no cover - normalized before this boundary
        _fail("生命周期动作不受支持。", "PLUGIN_LIFECYCLE_ACTION_INVALID")
    return {
        "catalog_state": catalog_state,
        "activation_state": activation_state,
        "resume_activation_state": resume_state,
    }


def lifecycle_runtime_state(state: dict[str, Any], *, integrity_ok: bool = True) -> str:
    if not integrity_ok:
        return "lifecycle_integrity_failed"
    if state.get("catalog_state") == "tombstoned":
        return "tombstoned"
    if state.get("activation_state") == "quarantined":
        return "quarantined"
    if state.get("catalog_state") == "deprecated":
        return "deprecated"
    if state.get("activation_state") == "disabled":
        return "disabled"
    return "ready"


def available_lifecycle_actions(
    state: dict[str, Any],
    *,
    system_managed: bool,
    implementation_available: bool = True,
) -> list[str]:
    if system_managed or state.get("catalog_state") == "tombstoned":
        return []
    actions: list[str] = []
    activation_state = state.get("activation_state")
    catalog_state = state.get("catalog_state")
    if activation_state == "enabled":
        actions.extend(["disable", "quarantine"])
    elif activation_state == "disabled":
        actions.extend(["enable", "quarantine"])
    elif activation_state == "quarantined":
        actions.append("clear_quarantine")
    if catalog_state == "active":
        actions.append("deprecate")
    elif catalog_state == "deprecated":
        actions.append("reinstate")
    actions.append("tombstone")
    if not implementation_available:
        actions = [
            action
            for action in actions
            if action not in LIFECYCLE_ACTIONS_REQUIRING_IMPLEMENTATION
        ]
    return actions


def lifecycle_safety() -> dict[str, Any]:
    return deepcopy(FIXED_PLUGIN_SAFETY)


def snapshot_target_refs(snapshot: Any) -> list[dict[str, str]]:
    if not isinstance(snapshot, dict):
        _fail("插件 registry snapshot 无效。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
    refs: list[dict[str, str]] = []
    for row in snapshot.get("capability_packs") or []:
        if not isinstance(row, dict):
            _fail("能力包冻结身份无效。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
        refs.append(_target_ref({
            "kind": "capability_pack",
            "id": row.get("id"),
            "version": row.get("pack_version"),
            "sha256": row.get("manifest_sha256"),
        }))
    for row in snapshot.get("domain_adapters") or []:
        if not isinstance(row, dict):
            _fail("领域适配器冻结身份无效。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
        refs.append(_target_ref({
            "kind": "domain_adapter",
            "id": row.get("adapter_id"),
            "version": row.get("adapter_version"),
            "sha256": row.get("contract_sha256"),
        }))
    for row in snapshot.get("ui_contributions") or []:
        if not isinstance(row, dict):
            _fail("界面贡献冻结身份无效。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
        refs.append(_target_ref({
            "kind": "ui_contribution",
            "id": row.get("contribution_id"),
            "version": row.get("contribution_version"),
            "sha256": row.get("contract_sha256"),
        }))
    identities = [(row["kind"], row["id"], row["version"]) for row in refs]
    if len(identities) != len(set(identities)):
        _fail("生命周期解析目标重复。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
    return refs


def build_lifecycle_resolution(
    snapshot: dict[str, Any],
    state_by_identity: dict[tuple[str, str, str], dict[str, Any]],
    *,
    binding_source: str,
) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for ref in snapshot_target_refs(snapshot):
        key = (ref["kind"], ref["id"], ref["version"])
        state = state_by_identity.get(key)
        if not isinstance(state, dict):
            _fail(
                f"插件生命周期目标缺失：{ref['kind']}:{ref['id']}@{ref['version']}。",
                "PLUGIN_LIFECYCLE_TARGET_UNAVAILABLE",
                status=409,
            )
        if str(state.get("target_sha256") or "") != ref["sha256"]:
            _fail("插件生命周期目标哈希漂移。", "PLUGIN_LIFECYCLE_TARGET_UNAVAILABLE", status=409)
        if state.get("integrity_ok") is not True:
            _fail("插件生命周期链无法验证。", "PLUGIN_LIFECYCLE_INTEGRITY_FAILED", status=409)
        if state.get("runtime_state") != "ready":
            _fail(
                f"插件当前不可用于新解析：{ref['id']}（{state.get('runtime_state') or 'unknown'}）。",
                "PLUGIN_LIFECYCLE_TARGET_UNAVAILABLE",
                status=409,
            )
        targets.append({
            **ref,
            "head_sequence": int(state.get("head_sequence") or 0),
            "head_sha256": str(state.get("head_sha256") or ""),
            "catalog_state": str(state.get("catalog_state") or ""),
            "activation_state": str(state.get("activation_state") or ""),
            "runtime_state": "ready",
        })
    payload = {
        "version": PLUGIN_LIFECYCLE_RESOLUTION_VERSION,
        "binding_source": str(binding_source or "runtime_resolution"),
        "registry_snapshot_sha256": str(snapshot.get("registry_snapshot_sha256") or ""),
        "lifecycle_head_set_sha256": canonical_sha256({
            "version": "plugin_lifecycle_head_set_v1",
            "registry_snapshot_sha256": str(snapshot.get("registry_snapshot_sha256") or ""),
            "targets": targets,
        }),
        "targets": targets,
        "resolution_policy": deepcopy(LIFECYCLE_RESOLUTION_POLICY),
        "safety": lifecycle_safety(),
    }
    payload["resolution_sha256"] = canonical_sha256(payload)
    return payload


def validate_lifecycle_resolution(value: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("生命周期解析封印必须是对象。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
    if set(value) != {
        "version",
        "binding_source",
        "registry_snapshot_sha256",
        "lifecycle_head_set_sha256",
        "targets",
        "resolution_policy",
        "safety",
        "resolution_sha256",
    }:
        _fail(
            "lifecycle resolution fields are invalid",
            "PLUGIN_LIFECYCLE_RESOLUTION_INVALID",
        )
    resolution = deepcopy(value)
    stored_sha256 = str(resolution.pop("resolution_sha256", "") or "").lower()
    if not SHA256_PATTERN.fullmatch(stored_sha256) or canonical_sha256(resolution) != stored_sha256:
        _fail("生命周期解析封印完整性校验失败。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
    resolution["resolution_sha256"] = stored_sha256
    if resolution.get("version") != PLUGIN_LIFECYCLE_RESOLUTION_VERSION:
        _fail("生命周期解析封印版本不受支持。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
    if resolution.get("binding_source") not in LIFECYCLE_RESOLUTION_BINDING_SOURCES:
        _fail(
            "lifecycle resolution binding source is invalid",
            "PLUGIN_LIFECYCLE_RESOLUTION_INVALID",
        )
    if resolution.get("registry_snapshot_sha256") != snapshot.get("registry_snapshot_sha256"):
        _fail("生命周期解析封印与插件合同不一致。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
    if resolution.get("resolution_policy") != LIFECYCLE_RESOLUTION_POLICY:
        _fail(
            "lifecycle resolution policy is invalid",
            "PLUGIN_LIFECYCLE_RESOLUTION_INVALID",
        )
    expected_head_set_sha256 = canonical_sha256({
        "version": "plugin_lifecycle_head_set_v1",
        "registry_snapshot_sha256": str(snapshot.get("registry_snapshot_sha256") or ""),
        "targets": resolution.get("targets"),
    })
    if resolution.get("lifecycle_head_set_sha256") != expected_head_set_sha256:
        _fail("生命周期 head 集合封印无效。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
    if resolution.get("safety") != lifecycle_safety():
        _fail("生命周期解析封印安全字段漂移。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
    expected_refs = snapshot_target_refs(snapshot)
    raw_targets = resolution.get("targets")
    if not isinstance(raw_targets, list) or len(raw_targets) != len(expected_refs):
        _fail("生命周期解析封印目标集合不一致。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
    for expected, raw in zip(expected_refs, raw_targets):
        if (
            not isinstance(raw, dict)
            or set(raw) != {
                "kind",
                "id",
                "version",
                "sha256",
                "head_sequence",
                "head_sha256",
                "catalog_state",
                "activation_state",
                "runtime_state",
            }
            or any(raw.get(field) != expected[field] for field in expected)
        ):
            _fail("生命周期解析封印目标身份不一致。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
        if (
            raw.get("catalog_state") != "active"
            or raw.get("activation_state") != "enabled"
            or raw.get("runtime_state") != "ready"
            or not isinstance(raw.get("head_sequence"), int)
            or isinstance(raw.get("head_sequence"), bool)
            or int(raw.get("head_sequence")) < 0
            or int(raw.get("head_sequence")) > 2**63 - 1
            or not SHA256_PATTERN.fullmatch(str(raw.get("head_sha256") or ""))
        ):
            _fail("生命周期解析封印包含不可用状态。", "PLUGIN_LIFECYCLE_RESOLUTION_INVALID")
    return resolution


__all__ = [
    "ACTIVATION_STATES",
    "CATALOG_STATES",
    "PLUGIN_LIFECYCLE_CATALOG_VERSION",
    "PLUGIN_LIFECYCLE_EVENT_VERSION",
    "PLUGIN_LIFECYCLE_EVENT_VERSION_V1",
    "PLUGIN_LIFECYCLE_EVENT_VERSIONS",
    "PLUGIN_LIFECYCLE_HEAD_VERSION",
    "PLUGIN_LIFECYCLE_PREVIEW_REQUEST_VERSION",
    "PLUGIN_LIFECYCLE_PREVIEW_VERSION",
    "PLUGIN_LIFECYCLE_PREVIEW_VERSION_V1",
    "PLUGIN_LIFECYCLE_PREVIEW_VERSIONS",
    "PLUGIN_LIFECYCLE_RESOLUTION_VERSION",
    "PLUGIN_LIFECYCLE_TARGET_VERSION",
    "PLUGIN_LIFECYCLE_TRANSITION_REQUEST_VERSION",
    "LIFECYCLE_ACTIONS_REQUIRING_IMPLEMENTATION",
    "PluginLifecycleError",
    "apply_lifecycle_action",
    "available_lifecycle_actions",
    "build_lifecycle_resolution",
    "initial_lifecycle_state",
    "lifecycle_runtime_state",
    "lifecycle_safety",
    "normalize_preview_request",
    "normalize_lifecycle_target_ref",
    "normalize_transition_request",
    "plugin_lifecycle_targets",
    "snapshot_target_refs",
    "transition_semantics",
    "validate_lifecycle_resolution",
]
