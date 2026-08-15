from __future__ import annotations

import re
from typing import Any


USER_DECISION_VERSION_V1 = "artifact_user_decision_v1"
USER_DECISION_VERSION = "artifact_user_decision_v2"
USER_DECISION_ACTIONS = {"support", "hold", "return"}
USER_DECISION_LABELS = {
    "support": "支持候选",
    "hold": "暂时保留",
    "return": "退回修订",
}
USER_DECISION_SELECTION_FIELDS = (
    "selected_option_id",
    "expected_candidate_revision",
    "expected_candidate_origin_message_id",
    "expected_candidate_latest_message_id",
    "expected_governance_attestation_sha256",
)
USER_DECISION_REQUEST_FIELDS = frozenset({
    "expected_version",
    "action",
    "rationale",
    *USER_DECISION_SELECTION_FIELDS,
})
USER_DECISION_FIELD_UNSET = object()


def _positive_integer_token(value: Any, error_message: str) -> int:
    if isinstance(value, bool):
        raise ValueError(error_message)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value.strip()):
        parsed = int(value.strip())
    else:
        raise ValueError(error_message)
    if parsed <= 0:
        raise ValueError(error_message)
    return parsed


def preferred_option_id(artifact: dict[str, Any]) -> str:
    """Return a valid preferred option from a structured artifact, if any."""

    content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
    decision = content.get("decision") if isinstance(content.get("decision"), dict) else {}
    preferred = str(decision.get("preferred_option_id") or "").strip()[:120]
    options = decision.get("options") if isinstance(decision.get("options"), list) else []
    option_ids = {
        str(option.get("id") or "").strip()[:120]
        for option in options
        if isinstance(option, dict) and str(option.get("id") or "").strip()
    }
    if str(decision.get("status") or "undecided").lower() != "candidate":
        return ""
    return preferred if preferred and preferred in option_ids else ""


def normalize_user_decision(
    artifact: dict[str, Any],
    *,
    expected_version: Any,
    action: Any,
    rationale: Any,
    selected_option_id: Any = USER_DECISION_FIELD_UNSET,
    expected_candidate_revision: Any = USER_DECISION_FIELD_UNSET,
    expected_candidate_origin_message_id: Any = USER_DECISION_FIELD_UNSET,
    expected_candidate_latest_message_id: Any = USER_DECISION_FIELD_UNSET,
    expected_governance_attestation_sha256: Any = USER_DECISION_FIELD_UNSET,
) -> dict[str, Any]:
    """Validate an immutable, version-bound final user decision request.

    This validates only a human decision about a research artifact. It never
    grants execution authority and intentionally has no account/order fields.
    """

    expected = _positive_integer_token(
        expected_version,
        "最终决定必须绑定有效产物版本",
    )
    current_version = int(artifact.get("version") or 0)
    if expected <= 0 or expected != current_version:
        raise ValueError("产物版本已变化，请刷新后再记录最终决定")
    if str(artifact.get("status") or "DRAFT").upper() != "CONFIRMED":
        raise ValueError("只有完成证据确认的产物才能记录最终决定")

    clean_action = str(action or "").strip().lower()
    if clean_action not in USER_DECISION_ACTIONS:
        raise ValueError("最终决定只能是支持候选、暂时保留或退回修订")
    clean_rationale = str(rationale or "").strip()
    if len(clean_rationale) < 3:
        raise ValueError("请填写最终决定理由")
    if len(clean_rationale) > 4000:
        raise ValueError("最终决定理由不能超过 4000 字")

    selection_values = {
        "selected_option_id": selected_option_id,
        "expected_candidate_revision": expected_candidate_revision,
        "expected_candidate_origin_message_id": (
            expected_candidate_origin_message_id
        ),
        "expected_candidate_latest_message_id": (
            expected_candidate_latest_message_id
        ),
        "expected_governance_attestation_sha256": (
            expected_governance_attestation_sha256
        ),
    }
    provided_selection_fields = {
        field
        for field, value in selection_values.items()
        if value is not USER_DECISION_FIELD_UNSET
    }
    ai_preferred = preferred_option_id(artifact)
    selected = ""
    candidate_revision = 0
    candidate_origin_message_id = ""
    candidate_latest_message_id = ""
    expected_attestation_sha256 = ""
    if clean_action == "support":
        if "selected_option_id" not in provided_selection_fields:
            raise ValueError("支持候选必须明确提交 selected_option_id")
        selected = str(selected_option_id or "").strip()[:120]
        if not selected:
            raise ValueError("支持候选必须明确提交 selected_option_id")
        content = (
            artifact.get("content")
            if isinstance(artifact.get("content"), dict)
            else {}
        )
        decision = (
            content.get("decision")
            if isinstance(content.get("decision"), dict)
            else {}
        )
        options = (
            decision.get("options")
            if isinstance(decision.get("options"), list)
            else []
        )
        matching_options = [
            option
            for option in options
            if isinstance(option, dict)
            and str(option.get("id") or "").strip() == selected
        ]
        if len(matching_options) != 1:
            raise ValueError("所选候选不存在或候选 ID 不唯一")
        if "expected_candidate_revision" in provided_selection_fields:
            candidate_revision = _positive_integer_token(
                expected_candidate_revision,
                "候选修订版本令牌无效",
            )
        if "expected_candidate_origin_message_id" in provided_selection_fields:
            candidate_origin_message_id = str(
                expected_candidate_origin_message_id or ""
            ).strip()[:120]
            if not candidate_origin_message_id:
                raise ValueError("候选来源消息令牌无效")
        if "expected_candidate_latest_message_id" in provided_selection_fields:
            candidate_latest_message_id = str(
                expected_candidate_latest_message_id or ""
            ).strip()[:120]
            if not candidate_latest_message_id:
                raise ValueError("候选最新消息令牌无效")
        if "expected_governance_attestation_sha256" in provided_selection_fields:
            expected_attestation_sha256 = str(
                expected_governance_attestation_sha256 or ""
            ).strip().lower()
            if (
                len(expected_attestation_sha256) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in expected_attestation_sha256
                )
            ):
                raise ValueError("治理证明并发令牌无效")
    elif provided_selection_fields:
        raise ValueError("暂时保留或退回修订不得提交任何候选选择字段")
    return {
        "decision_version": USER_DECISION_VERSION,
        "artifact_version": current_version,
        "action": clean_action,
        "action_label": USER_DECISION_LABELS[clean_action],
        "rationale": clean_rationale,
        "ai_preferred_option_id": ai_preferred,
        "selected_option_id": selected,
        "preferred_option_id": selected,
        "expected_candidate_revision": candidate_revision,
        "expected_candidate_origin_message_id": candidate_origin_message_id,
        "expected_candidate_latest_message_id": candidate_latest_message_id,
        "expected_governance_attestation_sha256": expected_attestation_sha256,
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    }


__all__ = [
    "USER_DECISION_FIELD_UNSET",
    "USER_DECISION_ACTIONS",
    "USER_DECISION_LABELS",
    "USER_DECISION_REQUEST_FIELDS",
    "USER_DECISION_SELECTION_FIELDS",
    "USER_DECISION_VERSION",
    "USER_DECISION_VERSION_V1",
    "normalize_user_decision",
    "preferred_option_id",
]
