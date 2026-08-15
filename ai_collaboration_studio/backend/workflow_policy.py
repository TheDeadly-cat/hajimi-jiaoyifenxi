from __future__ import annotations

import copy
import re
from typing import Any


POLICY_VERSION = 1
SAFETY_POLICY = {
    "user_confirmation_required": True,
    "execution_capability": "none",
    "live_trading_allowed": False,
}
_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_POLICY_KEYS = {
    "version",
    "stage_order",
    "minimum_stage_coverage",
    "required_coverage",
    "minimum_successful_members",
    "max_turns_per_member",
    "follow_up_budget",
    *SAFETY_POLICY,
}
_REQUIREMENT_KEYS = {"id", "label", "minimum", "any_of", "is_counterargument"}
_SELECTOR_KEYS = {"stances", "capabilities"}


GENERIC_WORKFLOW_POLICY: dict[str, Any] = {
    "version": POLICY_VERSION,
    "stage_order": ["facilitate", "flexible", "decision"],
    "minimum_stage_coverage": {
        "facilitate": 1,
        "flexible": 1,
        "decision": 1,
    },
    "required_coverage": [
        {
            "id": "facilitation",
            "label": "主持与目标守门",
            "minimum": 1,
            "any_of": {
                "stances": ["facilitator"],
                "capabilities": ["facilitation"],
            },
            "is_counterargument": False,
        },
        {
            "id": "critical_review",
            "label": "反证或风险审查",
            "minimum": 1,
            "any_of": {
                "stances": ["challenger", "bear", "risk"],
                "capabilities": ["critical_review", "risk_review"],
            },
            "is_counterargument": True,
        },
        {
            "id": "decision_synthesis",
            "label": "候选方案整合",
            "minimum": 1,
            "any_of": {
                "stances": ["builder", "decision"],
                "capabilities": ["decision_synthesis"],
            },
            "is_counterargument": False,
        },
    ],
    "minimum_successful_members": 2,
    "max_turns_per_member": 2,
    "follow_up_budget": 2,
    **SAFETY_POLICY,
}


LEGACY_STORAGE_WORKFLOW_POLICY_V1: dict[str, Any] = {
    "version": POLICY_VERSION,
    "stage_order": ["facilitate", "analysis", "debate", "plan", "risk", "decision"],
    "minimum_stage_coverage": {
        "facilitate": 1,
        "analysis": 5,
        "debate": 2,
        "plan": 1,
        "risk": 1,
        "decision": 1,
    },
    "required_coverage": [
        {
            "id": "facilitation",
            "label": "主持与目标守门",
            "minimum": 1,
            "any_of": {
                "stances": ["facilitator"],
                "capabilities": ["facilitation"],
            },
            "is_counterargument": False,
        },
        {
            "id": "storage_sectors",
            "label": "DRAM/NAND 与 HDD 产业",
            "minimum": 2,
            "any_of": {
                "stances": ["sector"],
                "capabilities": ["storage_sector_analysis"],
            },
            "is_counterargument": False,
        },
        {
            "id": "fundamental",
            "label": "基本面",
            "minimum": 1,
            "any_of": {
                "stances": ["fundamental"],
                "capabilities": ["fundamental_analysis"],
            },
            "is_counterargument": False,
        },
        {
            "id": "technical",
            "label": "技术与资金",
            "minimum": 1,
            "any_of": {
                "stances": ["technical"],
                "capabilities": ["technical_analysis"],
            },
            "is_counterargument": False,
        },
        {
            "id": "sentiment",
            "label": "新闻与情绪",
            "minimum": 1,
            "any_of": {
                "stances": ["sentiment"],
                "capabilities": ["sentiment_analysis"],
            },
            "is_counterargument": False,
        },
        {
            "id": "bull_case",
            "label": "多头论证",
            "minimum": 1,
            "any_of": {
                "stances": ["bull"],
                "capabilities": ["bull_case"],
            },
            "is_counterargument": False,
        },
        {
            "id": "counterargument",
            "label": "空头反证",
            "minimum": 1,
            "any_of": {
                "stances": ["bear"],
                "capabilities": ["bear_case", "critical_review"],
            },
            "is_counterargument": True,
        },
        {
            "id": "simulation_plan",
            "label": "模拟方案",
            "minimum": 1,
            "any_of": {
                "stances": ["paper_trader"],
                "capabilities": ["simulation_planning"],
            },
            "is_counterargument": False,
        },
        {
            "id": "risk_review",
            "label": "风险复核",
            "minimum": 1,
            "any_of": {
                "stances": ["risk"],
                "capabilities": ["risk_review"],
            },
            "is_counterargument": True,
        },
        {
            "id": "decision_synthesis",
            "label": "投委会整合",
            "minimum": 1,
            "any_of": {
                "stances": ["portfolio_manager"],
                "capabilities": ["decision_synthesis"],
            },
            "is_counterargument": False,
        },
    ],
    "minimum_successful_members": 11,
    "max_turns_per_member": 2,
    "follow_up_budget": 6,
    **SAFETY_POLICY,
}

STORAGE_WORKFLOW_POLICY: dict[str, Any] = copy.deepcopy(LEGACY_STORAGE_WORKFLOW_POLICY_V1)
STORAGE_WORKFLOW_POLICY["minimum_stage_coverage"]["analysis"] = 6
STORAGE_WORKFLOW_POLICY["required_coverage"].insert(
    5,
    {
        "id": "data_quality",
        "label": "数据质量与防泄漏",
        "minimum": 1,
        "any_of": {
            "stances": ["data_guardian"],
            "capabilities": ["data_quality_review"],
        },
        "is_counterargument": False,
    },
)
STORAGE_WORKFLOW_POLICY["minimum_successful_members"] = 12


def default_workflow_policy(template_id: str) -> dict[str, Any]:
    policy = STORAGE_WORKFLOW_POLICY if template_id == "us_storage_committee" else GENERIC_WORKFLOW_POLICY
    return copy.deepcopy(policy)


def clean_capabilities(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("成员 capabilities 必须是字符串数组")
    clean: list[str] = []
    for raw in value:
        tag = str(raw or "").strip().lower()
        if not _SLUG_PATTERN.fullmatch(tag):
            raise ValueError("成员 capability 必须是小写英文 slug，最长 40 个字符")
        if tag not in clean:
            clean.append(tag)
    if len(clean) > 24:
        raise ValueError("单个成员最多配置 24 个 capabilities")
    return clean


def validate_workflow_policy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("workflow_policy 必须是 JSON 对象")
    unknown = set(value) - _POLICY_KEYS
    missing = _POLICY_KEYS - set(value)
    if unknown:
        raise ValueError(f"workflow_policy 包含未知字段：{', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"workflow_policy 缺少字段：{', '.join(sorted(missing))}")
    if value.get("version") != POLICY_VERSION:
        raise ValueError(f"workflow_policy.version 仅支持 {POLICY_VERSION}")

    stage_order = _clean_slug_list(value.get("stage_order"), "stage_order", minimum=1, maximum=12)
    if "follow_up" in stage_order:
        raise ValueError("follow_up 是系统保留的追问阶段，不能放入 stage_order")

    raw_stage_coverage = value.get("minimum_stage_coverage")
    if not isinstance(raw_stage_coverage, dict):
        raise ValueError("minimum_stage_coverage 必须是对象")
    unknown_stages = set(raw_stage_coverage) - set(stage_order)
    if unknown_stages:
        raise ValueError(
            "minimum_stage_coverage 包含未在 stage_order 中声明的阶段："
            + ", ".join(sorted(unknown_stages))
        )
    missing_stages = set(stage_order) - set(raw_stage_coverage)
    if missing_stages:
        raise ValueError(
            "minimum_stage_coverage 缺少 stage_order 中的阶段："
            + ", ".join(sorted(missing_stages))
        )
    stage_coverage: dict[str, int] = {}
    for stage in stage_order:
        stage_coverage[stage] = _clean_int(
            raw_stage_coverage[stage],
            f"minimum_stage_coverage.{stage}",
            minimum=1,
            maximum=50,
        )

    raw_requirements = value.get("required_coverage")
    if not isinstance(raw_requirements, list):
        raise ValueError("required_coverage 必须是数组")
    if len(raw_requirements) > 24:
        raise ValueError("required_coverage 最多包含 24 项")
    requirements: list[dict[str, Any]] = []
    requirement_ids: set[str] = set()
    for index, raw_requirement in enumerate(raw_requirements):
        if not isinstance(raw_requirement, dict):
            raise ValueError(f"required_coverage[{index}] 必须是对象")
        unknown_requirement_keys = set(raw_requirement) - _REQUIREMENT_KEYS
        missing_requirement_keys = {"id", "label", "minimum", "any_of"} - set(raw_requirement)
        if unknown_requirement_keys:
            raise ValueError(
                f"required_coverage[{index}] 包含未知字段："
                + ", ".join(sorted(unknown_requirement_keys))
            )
        if missing_requirement_keys:
            raise ValueError(
                f"required_coverage[{index}] 缺少字段："
                + ", ".join(sorted(missing_requirement_keys))
            )
        requirement_id = str(raw_requirement.get("id") or "").strip().lower()
        if not _SLUG_PATTERN.fullmatch(requirement_id):
            raise ValueError(f"required_coverage[{index}].id 必须是小写英文 slug")
        if requirement_id in requirement_ids:
            raise ValueError(f"required_coverage id 重复：{requirement_id}")
        requirement_ids.add(requirement_id)
        label = str(raw_requirement.get("label") or "").strip()
        if not label or len(label) > 80:
            raise ValueError(f"required_coverage[{index}].label 必须为 1 到 80 个字符")
        any_of = raw_requirement.get("any_of")
        if not isinstance(any_of, dict):
            raise ValueError(f"required_coverage[{index}].any_of 必须是对象")
        unknown_selector_keys = set(any_of) - _SELECTOR_KEYS
        if unknown_selector_keys:
            raise ValueError(
                f"required_coverage[{index}].any_of 包含未知字段："
                + ", ".join(sorted(unknown_selector_keys))
            )
        stances = _clean_slug_list(any_of.get("stances", []), "stances", minimum=0, maximum=24)
        capabilities = _clean_slug_list(
            any_of.get("capabilities", []),
            "capabilities",
            minimum=0,
            maximum=24,
        )
        if not stances and not capabilities:
            raise ValueError(f"required_coverage[{index}].any_of 至少声明 stance 或 capability")
        is_counterargument = raw_requirement.get("is_counterargument", False)
        if not isinstance(is_counterargument, bool):
            raise ValueError(f"required_coverage[{index}].is_counterargument 必须是布尔值")
        requirements.append({
            "id": requirement_id,
            "label": label,
            "minimum": _clean_int(
                raw_requirement.get("minimum"),
                f"required_coverage[{index}].minimum",
                minimum=1,
                maximum=50,
            ),
            "any_of": {
                "stances": stances,
                "capabilities": capabilities,
            },
            "is_counterargument": is_counterargument,
        })

    if value.get("user_confirmation_required") is not True:
        raise ValueError("user_confirmation_required 是不可关闭的安全边界")
    if value.get("execution_capability") != "none":
        raise ValueError("execution_capability 必须保持 none")
    if value.get("live_trading_allowed") is not False:
        raise ValueError("live_trading_allowed 必须保持 false")

    return {
        "version": POLICY_VERSION,
        "stage_order": stage_order,
        "minimum_stage_coverage": stage_coverage,
        "required_coverage": requirements,
        "minimum_successful_members": _clean_int(
            value.get("minimum_successful_members"),
            "minimum_successful_members",
            minimum=1,
            maximum=100,
        ),
        "max_turns_per_member": _clean_int(
            value.get("max_turns_per_member"),
            "max_turns_per_member",
            minimum=1,
            maximum=5,
        ),
        "follow_up_budget": _clean_int(
            value.get("follow_up_budget"),
            "follow_up_budget",
            minimum=0,
            maximum=50,
        ),
        **SAFETY_POLICY,
    }


def policy_from_json(raw_value: Any, template_id: str) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        candidate = raw_value
    elif isinstance(raw_value, str) and raw_value.strip():
        try:
            import json

            candidate = json.loads(raw_value)
        except (TypeError, ValueError):
            candidate = None
    else:
        candidate = None
    try:
        return validate_workflow_policy(candidate)
    except ValueError:
        return default_workflow_policy(template_id)


def member_matches_requirement(member: dict[str, Any], requirement: dict[str, Any]) -> bool:
    selectors = requirement.get("any_of") if isinstance(requirement.get("any_of"), dict) else {}
    stances = {str(item) for item in selectors.get("stances") or []}
    capabilities = {str(item) for item in selectors.get("capabilities") or []}
    member_capabilities = {str(item) for item in member.get("capabilities") or []}
    return (
        bool(str(member.get("stance") or "") in stances)
        or bool(member_capabilities.intersection(capabilities))
    )


def _clean_slug_list(value: Any, field: str, *, minimum: int, maximum: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是字符串数组")
    clean: list[str] = []
    for raw in value:
        tag = str(raw or "").strip().lower()
        if not _SLUG_PATTERN.fullmatch(tag):
            raise ValueError(f"{field} 中的值必须是小写英文 slug，最长 40 个字符")
        if tag not in clean:
            clean.append(tag)
    if len(clean) < minimum or len(clean) > maximum:
        raise ValueError(f"{field} 数量必须在 {minimum} 到 {maximum} 之间")
    return clean


def _clean_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 必须是整数")
    if value < minimum or value > maximum:
        raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return value
