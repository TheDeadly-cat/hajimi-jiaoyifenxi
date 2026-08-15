from __future__ import annotations

from typing import Any


RULES_FIRST_DIRECTOR_VERSION = "rules_first_director_v2"
DIRECTOR_SCHEDULING_CONTEXT_VERSION = "director_scheduling_context_v1"
# One scheduling decision, its prompt candidate set, its persisted audit
# context, and the provider-route authorization all share this ceiling.  A
# model must never see (and therefore select) a member omitted from the seal.
DIRECTOR_CANDIDATE_LIMIT = 256


def _clean_member_id(value: Any) -> str:
    return str(value or "").strip()[:80]


def _coverage_gap_code(prefix: str, item: dict[str, Any]) -> str:
    item_id = str(item.get("id") or "unknown").strip().lower()[:60]
    return f"{prefix}:{item_id}"


def _member_matches_coverage(
    member_id: str,
    item: dict[str, Any],
) -> bool:
    return member_id in {
        _clean_member_id(value)
        for value in item.get("configured_member_ids") or []
        if _clean_member_id(value)
    }


def _positive_coverage_deficit(item: dict[str, Any]) -> int:
    if not isinstance(item, dict) or item.get("ready"):
        return 0
    try:
        required_count = max(0, int(item.get("required_count") or 0))
        successful_count = max(0, int(item.get("successful_count") or 0))
    except (TypeError, ValueError):
        return 0
    return max(0, required_count - successful_count)


def _selection_stage(member: dict[str, Any], active_stage: str) -> str:
    if active_stage == "follow_up":
        return active_stage
    return str(member.get("workflow_stage") or active_stage or "flexible").strip().lower()


def stage_frontier_eligible_members(
    *,
    unspoken: list[dict[str, Any]],
    stage_order: list[str],
    stage_coverage: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    """Open only the causally reachable workflow-stage frontier.

    A dynamic moderator may choose any unspoken member from stages whose
    minimum coverage is already satisfied, plus members in the earliest stage
    whose minimum is still open.  A later-stage gap therefore cannot pull risk
    or decision synthesis ahead of the analysis/debate/plan evidence it must
    review.  Extra members in an already-satisfied earlier stage remain
    eligible and can still compete by gap contribution, so this is a causal
    frontier rather than a return to fixed polling.
    """

    candidates = [
        member for member in unspoken if _clean_member_id(member.get("id"))
    ][:DIRECTOR_CANDIDATE_LIMIT]
    ordered_stages = list(dict.fromkeys(
        str(stage or "").strip().lower()
        for stage in stage_order[:24]
        if str(stage or "").strip()
    ))
    if not candidates or not ordered_stages:
        return candidates, "flexible"

    coverage_by_stage = {
        str(item.get("id") or "").strip().lower(): item
        for item in stage_coverage[:24]
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    ranks = {stage: index for index, stage in enumerate(ordered_stages)}

    def fallback() -> tuple[list[dict[str, Any]], str]:
        active_stage = min(
            (
                str(member.get("workflow_stage") or "flexible").strip().lower()
                for member in candidates
            ),
            key=lambda stage: ranks.get(stage, len(ranks)),
        )
        return candidates, active_stage

    # Legacy/custom convergence fixtures may not contain the authoritative
    # per-stage snapshot.  Do not invent a dependency frontier from partial
    # data; preserve the prior dynamic candidate set in that case.
    if any(
        stage not in coverage_by_stage
        or not isinstance(coverage_by_stage[stage].get("ready"), bool)
        for stage in ordered_stages
    ):
        return fallback()

    candidate_stages = {
        str(member.get("workflow_stage") or "flexible").strip().lower()
        for member in candidates
    }
    frontier_index: int | None = None
    for index, stage in enumerate(ordered_stages):
        coverage = coverage_by_stage.get(stage)
        if coverage.get("ready") is not True and stage in candidate_stages:
            frontier_index = index
            break

    if frontier_index is None:
        # An earlier minimum can become unrepairable after its only member
        # fails.  Continue collecting auditable downstream views instead of
        # deadlocking the round; convergence will still remain partial.
        return fallback()

    open_stages = set(ordered_stages[: frontier_index + 1])
    eligible = [
        member
        for member in candidates
        if str(member.get("workflow_stage") or "flexible").strip().lower()
        in open_stages
    ]
    return eligible, ordered_stages[frontier_index]


def build_director_scheduling_context(
    *,
    eligible: list[dict[str, Any]],
    callable_members: list[dict[str, Any]],
    stage_coverage: list[dict[str, Any]],
    role_coverage: list[dict[str, Any]],
    successful_member_ids: set[str],
    successful_member_count: int,
    required_success_count: int,
    workspace_focus: dict[str, Any] | None,
    focus_covered: bool,
    global_remaining_calls: int | None = None,
    director_remaining_calls: int | None = None,
    force_formal_speaker: bool = False,
    continuation_required: bool = False,
) -> dict[str, Any]:
    """Build a bounded, domain-neutral snapshot for one scheduling decision.

    A candidate's contribution count is the number of *currently open* hard
    gaps that one successful visible turn can reduce: distinct-speaker count,
    stage coverage, role coverage, and the frozen workspace focus.  The
    remaining-call estimate uses the same monotone greedy set-cover strategy as
    round launch planning, but starts from the round's already successful set.
    It is deliberately deterministic and never invokes a model or data source.
    """

    successful_ids = {
        _clean_member_id(member_id)
        for member_id in successful_member_ids
        if _clean_member_id(member_id)
    }
    eligible_members = [
        member for member in eligible if _clean_member_id(member.get("id"))
    ][:DIRECTOR_CANDIDATE_LIMIT]
    callable_candidates = [
        member for member in callable_members if _clean_member_id(member.get("id"))
    ][:DIRECTOR_CANDIDATE_LIMIT]
    stage_gaps = [
        item
        for item in stage_coverage[:24]
        if isinstance(item, dict) and _positive_coverage_deficit(item) > 0
    ]
    role_gaps = [
        item
        for item in role_coverage[:24]
        if isinstance(item, dict) and _positive_coverage_deficit(item) > 0
    ]
    try:
        unique_deficit = max(
            0,
            int(required_success_count) - max(0, int(successful_member_count)),
        )
    except (TypeError, ValueError):
        unique_deficit = 0
    focus_gap_code = ""
    if isinstance(workspace_focus, dict) and not focus_covered:
        focus_code = str(workspace_focus.get("code") or "workspace_focus").strip().lower()
        focus_gap_code = f"workspace:{focus_code[:60] or 'workspace_focus'}"

    gap_codes: list[str] = []
    if unique_deficit:
        gap_codes.append("speaker_coverage")
    gap_codes.extend(_coverage_gap_code("stage", item) for item in stage_gaps)
    gap_codes.extend(_coverage_gap_code("role", item) for item in role_gaps)
    if focus_gap_code:
        gap_codes.append(focus_gap_code)
    gap_codes = list(dict.fromkeys(gap_codes))[:48]

    def contribution_codes(member: dict[str, Any]) -> list[str]:
        member_id = _clean_member_id(member.get("id"))
        codes: list[str] = []
        if member_id not in successful_ids:
            if unique_deficit:
                codes.append("speaker_coverage")
            codes.extend(
                _coverage_gap_code("stage", item)
                for item in stage_gaps
                if _member_matches_coverage(member_id, item)
            )
            codes.extend(
                _coverage_gap_code("role", item)
                for item in role_gaps
                if _member_matches_coverage(member_id, item)
            )
        if focus_gap_code and member_matches_workspace_focus(member, workspace_focus):
            codes.append(focus_gap_code)
        return list(dict.fromkeys(code for code in codes if code in gap_codes))[:32]

    candidate_contributions: list[dict[str, Any]] = []
    for member in eligible_members:
        codes = contribution_codes(member)
        candidate_contributions.append({
            "member_id": _clean_member_id(member.get("id")),
            "contribution_count": len(codes),
            "gap_codes": codes,
        })

    # Greedily choose visible speakers until every satisfiable monotone deficit
    # has been reduced.  A member is selected at most once because stage, role,
    # and distinct-speaker minima count distinct successful members.
    stage_deficits = {
        _coverage_gap_code("stage", item): _positive_coverage_deficit(item)
        for item in stage_gaps
    }
    role_deficits = {
        _coverage_gap_code("role", item): _positive_coverage_deficit(item)
        for item in role_gaps
    }
    remaining_deficits = {
        **stage_deficits,
        **role_deficits,
        **({"speaker_coverage": unique_deficit} if unique_deficit else {}),
        **({focus_gap_code: 1} if focus_gap_code else {}),
    }
    remaining_candidates = list(callable_candidates)
    planned_member_ids: list[str] = []
    while any(value > 0 for value in remaining_deficits.values()):
        best_index = -1
        best_codes: list[str] = []
        for index, member in enumerate(remaining_candidates):
            codes = [
                code
                for code in contribution_codes(member)
                if remaining_deficits.get(code, 0) > 0
            ]
            if len(codes) > len(best_codes):
                best_index = index
                best_codes = codes
        if best_index < 0 or not best_codes:
            break
        selected = remaining_candidates.pop(best_index)
        planned_member_ids.append(_clean_member_id(selected.get("id")))
        for code in best_codes:
            remaining_deficits[code] = max(0, remaining_deficits[code] - 1)

    if (
        (force_formal_speaker or continuation_required)
        and eligible_members
        and not planned_member_ids
    ):
        planned_member_ids.append(_clean_member_id(eligible_members[0].get("id")))
    plan_feasible = not any(
        value > 0 for value in remaining_deficits.values()
    )

    def clean_remaining(value: int | None) -> int | None:
        if value is None:
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    return {
        "version": DIRECTOR_SCHEDULING_CONTEXT_VERSION,
        "eligible_member_ids": [
            _clean_member_id(member.get("id")) for member in eligible_members
        ],
        "gap_codes": gap_codes,
        "candidate_contributions": candidate_contributions,
        "global_remaining_calls": clean_remaining(global_remaining_calls),
        "director_remaining_calls": clean_remaining(director_remaining_calls),
        "minimum_remaining_visible_speaker_calls": len(planned_member_ids),
        "remaining_visible_plan_feasible": plan_feasible,
    }


def member_matches_workspace_focus(
    member: dict[str, Any],
    focus: dict[str, Any] | None,
) -> bool:
    """Return whether a member can address the frozen workspace focus."""
    if not isinstance(focus, dict):
        return False
    target_capabilities = {
        str(item or "").strip().lower()
        for item in focus.get("target_capabilities") or []
        if str(item or "").strip()
    }
    member_capabilities = {
        str(item or "").strip().lower()
        for item in member.get("capabilities") or []
        if str(item or "").strip()
    }
    target_stances = {
        str(item or "").strip().lower()
        for item in focus.get("target_stances") or []
        if str(item or "").strip()
    }
    member_stance = str(member.get("stance") or "").strip().lower()
    return bool(
        target_capabilities.intersection(member_capabilities)
        or (member_stance and member_stance in target_stances)
    )


def _unique_role_coverage_closer(
    eligible: list[dict[str, Any]],
    role_coverage: list[dict[str, Any]],
    successful_member_ids: set[str],
) -> tuple[dict[str, Any], list[str]] | None:
    closers: dict[str, tuple[dict[str, Any], list[str]]] = {}
    for requirement in role_coverage:
        if not isinstance(requirement, dict) or requirement.get("ready"):
            continue
        try:
            required_count = max(1, int(requirement.get("required_count") or 1))
            configured_count = max(0, int(requirement.get("configured_count") or 0))
            successful_count = max(0, int(requirement.get("successful_count") or 0))
        except (TypeError, ValueError):
            continue
        # A speaker can close this requirement only when the room has enough
        # distinct configured members and exactly one new distinct success is
        # still needed. Repeating a successful member never counts twice.
        if (
            configured_count < required_count
            or successful_count >= required_count
            or successful_count + 1 < required_count
        ):
            continue
        configured_ids = {
            str(item or "")
            for item in requirement.get("configured_member_ids") or []
            if str(item or "")
        }
        already_successful = successful_member_ids.union({
            str(item or "")
            for item in requirement.get("successful_member_ids") or []
            if str(item or "")
        })
        label = str(
            requirement.get("label") or requirement.get("id") or "强制角色覆盖"
        ).strip()
        for member in eligible:
            member_id = str(member.get("id") or "")
            if member_id not in configured_ids or member_id in already_successful:
                continue
            if member_id not in closers:
                closers[member_id] = (member, [])
            closers[member_id][1].append(label)
    if len(closers) != 1:
        return None
    return next(iter(closers.values()))


def select_rules_first_director_decision(
    *,
    eligible: list[dict[str, Any]],
    active_stage: str,
    stage_label: str,
    post_coverage: bool,
    can_host_finish: bool,
    workspace_focus: dict[str, Any] | None,
    focus_covered: bool,
    role_coverage: list[dict[str, Any]],
    successful_member_ids: set[str],
    scheduling_context: dict[str, Any] | None = None,
    force_formal_speaker: bool = False,
) -> dict[str, Any] | None:
    """Resolve only unambiguous scheduling decisions without a model call.

    Returning ``None`` is deliberate: it means multiple candidates remain
    semantically plausible and the configured moderator model may arbitrate.
    """
    candidates = [
        member for member in eligible if str(member.get("id") or "")
    ][:DIRECTOR_CANDIDATE_LIMIT]
    if not candidates:
        return None

    if (
        not force_formal_speaker
        and post_coverage
        and can_host_finish
        and focus_covered
    ):
        return {
            "action": "finish",
            "reason": (
                "规则优先：服务端确认角色覆盖与当前工作区缺口均已完成，"
                "结束讨论并送交用户复核。"
            ),
            "source": "rules_first",
            "stage": active_stage,
            "policy_version": RULES_FIRST_DIRECTOR_VERSION,
            "rule_id": "safe_finish",
        }

    if len(candidates) == 1:
        selected = candidates[0]
        return {
            "action": "speak",
            "member": selected,
            "reason": (
                f"规则优先：当前“{stage_label}”阶段只有 {selected.get('name') or '该成员'} "
                "一位可执行候选，由其继续推进。"
            ),
            "source": "rules_first",
            "stage": _selection_stage(selected, active_stage),
            "policy_version": RULES_FIRST_DIRECTOR_VERSION,
            "rule_id": "single_eligible",
        }

    contributions = (
        scheduling_context.get("candidate_contributions")
        if isinstance(scheduling_context, dict)
        and isinstance(scheduling_context.get("candidate_contributions"), list)
        else []
    )
    focus_repair_scope = str(
        (scheduling_context or {}).get("workspace_focus_repair_scope") or ""
    ).strip().lower()
    if (
        focus_repair_scope == "next_round_only"
        and isinstance(workspace_focus, dict)
        and not focus_covered
    ):
        contribution_by_member = {
            _clean_member_id(item.get("member_id")): max(
                0,
                int(item.get("contribution_count") or 0),
            )
            for item in contributions[:DIRECTOR_CANDIDATE_LIMIT]
            if isinstance(item, dict)
            and _clean_member_id(item.get("member_id"))
        }
        focus_candidates = [
            member
            for member in candidates
            if member_matches_workspace_focus(member, workspace_focus)
        ]
        if focus_candidates:
            selected = min(
                focus_candidates,
                key=lambda member: (
                    -contribution_by_member.get(
                        _clean_member_id(member.get("id")),
                        0,
                    ),
                    int(member.get("position") or 0),
                    _clean_member_id(member.get("id")),
                ),
            )
            focus_title = str(
                workspace_focus.get("title") or "冻结证据缺口"
            ).strip()[:120]
            return {
                "action": "speak",
                "member": selected,
                "reason": (
                    f"规则优先：首要缺口“{focus_title}”只能在下一新轮重建；"
                    f"先由 {selected.get('name') or '匹配职责成员'} 明确受影响主张、"
                    "反证边界和下一轮补证条件，不得把文字说明冒充已修复。"
                ),
                "source": "rules_first",
                "stage": _selection_stage(selected, active_stage),
                "policy_version": RULES_FIRST_DIRECTOR_VERSION,
                "rule_id": "unrepairable_focus_explanation",
            }
    candidate_by_id = {
        _clean_member_id(member.get("id")): member for member in candidates
    }
    scored: list[tuple[int, dict[str, Any], list[str]]] = []
    for item in contributions[:DIRECTOR_CANDIDATE_LIMIT]:
        if not isinstance(item, dict):
            continue
        member = candidate_by_id.get(_clean_member_id(item.get("member_id")))
        if not member:
            continue
        raw_codes = item.get("gap_codes")
        codes = [
            str(code or "").strip()[:80]
            for code in (raw_codes if isinstance(raw_codes, list) else [])[:32]
            if str(code or "").strip()
        ]
        scored.append((len(set(codes)), member, list(dict.fromkeys(codes))))
    if scored:
        highest = max(score for score, _member, _codes in scored)
        leaders = [item for item in scored if item[0] == highest]
        if highest > 0 and len(leaders) == 1:
            score, selected, codes = leaders[0]
            focus_note = ""
            if any(code.startswith("workspace:") for code in codes):
                focus_title = str(
                    (workspace_focus or {}).get("title") or "当前首要缺口"
                ).strip()[:120]
                focus_note = f"；其中包含首要缺口“{focus_title}”"
            return {
                "action": "speak",
                "member": selected,
                "reason": (
                    f"规则优先：{selected.get('name') or '该成员'} 的下一次有效发言可同时推进 "
                    f"{score} 项当前缺口（{'、'.join(codes[:4])}），贡献数唯一最高"
                    f"{focus_note}。"
                ),
                "source": "rules_first",
                "stage": _selection_stage(selected, active_stage),
                "policy_version": RULES_FIRST_DIRECTOR_VERSION,
                "rule_id": "max_required_gap_contribution",
            }

    if isinstance(workspace_focus, dict) and not focus_covered:
        focus_candidates = [
            member
            for member in candidates
            if member_matches_workspace_focus(member, workspace_focus)
        ]
        if len(focus_candidates) == 1:
            selected = focus_candidates[0]
            focus_title = str(workspace_focus.get("title") or "当前首要缺口").strip()
            return {
                "action": "speak",
                "member": selected,
                "reason": (
                    f"规则优先：首要缺口“{focus_title}”仅与 "
                    f"{selected.get('name') or '该成员'} 的职责能力匹配，"
                    "由其优先补证、提出反证或修订方案。"
                ),
                "source": "rules_first",
                "stage": _selection_stage(selected, active_stage),
                "policy_version": RULES_FIRST_DIRECTOR_VERSION,
                "rule_id": "unique_workspace_focus",
            }

    role_closer = _unique_role_coverage_closer(
        candidates,
        role_coverage,
        successful_member_ids,
    )
    if role_closer:
        selected, labels = role_closer
        coverage_label = "、".join(labels[:3]) or "强制角色覆盖"
        return {
            "action": "speak",
            "member": selected,
            "reason": (
                f"规则优先：{selected.get('name') or '该成员'} 是唯一能补齐"
                f"尚未覆盖职责“{coverage_label}”的当前候选。"
            ),
            "source": "rules_first",
            "stage": _selection_stage(selected, active_stage),
            "policy_version": RULES_FIRST_DIRECTOR_VERSION,
            "rule_id": "unique_role_coverage_closer",
        }

    return None
