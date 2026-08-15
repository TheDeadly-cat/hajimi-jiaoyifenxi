from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from .decision_lineage import canonical_sha256


CORE_ROOM_CAPABILITIES = [
    "collaboration.chat",
    "materials.shared",
    "artifacts.meeting",
]

CAPABILITY_PACKS: dict[str, dict[str, Any]] = {
    "football_research_readonly": {
        "id": "football_research_readonly",
        "manifest_version": "capability_pack_manifest_v2",
        "pack_version": "1.0.0",
        "core_protocol_range": ">=1.0.0 <2.0.0",
        "dependencies": [],
        "domain_adapter_ids": ["football_research"],
        "domain_adapter_port_requirements": [{
            "port_id": "core.football.match_context/v1",
            "requirement": "required",
            "cardinality": "one",
            "version_range": ">=1.0.0 <2.0.0",
        }],
        "ui_contribution_ids": ["football_research.room_inspector/v1"],
        "name": "足球赛事只读研究",
        "category": "体育研究 / 足球",
        "description": (
            "从精确材料版本封印赛事身份、赛程与旅行、阵容可用性、"
            "战术和近期表现；赔率只作为代理信息，不生成未来胜率。"
        ),
        "mode_label": "只读 / 无投注",
        "capabilities": [
            "research.football.match_context.readonly",
            "research.football.evidence_classification",
        ],
        "discussion_protocol": {
            "title": "足球赛事只读研究协议 v1",
            "rules": [
                "只使用房间内精确封印的材料版本，并统一比赛、开球 UTC、场地和数据截止时间。",
                "联赛赛季、赛程旅行、阵容伤停停赛、战术与近期表现必须逐项标注官方事实、媒体信息、模型推断或赔率代理。",
                "赔率只保留原始报价与抓取时间；在没有独立真实校准前不得生成未来胜率、置信度或校准指标。",
                "不得投注、连接钱包、自动下注或替代用户作出决定，也不得复用股票候选历史实验。",
            ],
            "director_focus": (
                "优先补齐同一截止时间下的官方赛事实体、精确材料绑定、"
                "阵容发布时间、赛程旅行和可反驳的战术推断。"
            ),
        },
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "can_replace_user_decision": False,
        "arbitrary_code_loading_allowed": False,
        "user_final_decision_required": True,
    },
    "stock_research_readonly": {
        "id": "stock_research_readonly",
        "manifest_version": "capability_pack_manifest_v2",
        "pack_version": "1.0.0",
        "core_protocol_range": ">=1.0.0 <2.0.0",
        "dependencies": ["structured_project_research"],
        "domain_adapter_ids": ["stock_research"],
        "domain_adapter_port_requirements": [{
            "port_id": "core.market.readonly_context/v1",
            "requirement": "required",
            "cardinality": "one",
            "version_range": ">=1.0.0 <2.0.0",
        }],
        "ui_contribution_ids": ["stock_research.room_inspector/v1"],
        "name": "通用股票只读研究",
        "category": "交易研究 / 股票",
        "description": (
            "由房间显式股票池和统一数据截止时间约束，逐标的封印 Futu、SEC、IR、"
            "复权与公司行动预检；复用宿主证据图、治理、行动台和用户决定链。"
        ),
        "mode_label": "只读 / 无交易",
        "capabilities": [
            "research.stock.readonly_context",
            "research.stock.evidence_classification",
        ],
        "discussion_protocol": {
            "title": "通用股票只读研究协议 v1",
            "rules": [
                "只研究房间显式封印的股票池；所有标的使用同一数据截止时间。",
                "每个标的必须逐项展示 Futu、SEC、IR、复权和公司行动预检，缺失或过期时保持不可用，不得补造。",
                "每项主张严格区分官方事实、媒体信息、模型推断和市场代理，并绑定来源、材料版本、内容哈希与快照哈希。",
                "复用结构化项目研究的证据图、治理、行动台和用户决定链；本能力包不复制其实现，也不产生候选实验或模拟组合动作。",
                "不得下单、连接钱包、自动交易、替代用户决定或声称已经获得用户授权。",
            ],
            "director_focus": (
                "优先补齐同一截止时间下的股票池范围、五项来源预检、公司行动与复权链，"
                "再将分歧和待核验行动交给宿主治理与用户决定链。"
            ),
        },
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "can_replace_user_decision": False,
        "arbitrary_code_loading_allowed": False,
        "user_final_decision_required": True,
    },
    "storage_research_readonly": {
        "id": "storage_research_readonly",
        "manifest_version": "capability_pack_manifest_v1",
        "pack_version": "1.1.0",
        "core_protocol_range": ">=1.0.0 <2.0.0",
        "dependencies": [],
        "domain_adapter_ids": ["storage_research"],
        "ui_contribution_ids": [
            "storage_research.room_inspector/v1",
            "storage_research.artifact_workspace/v1",
        ],
        "name": "美国存储产业只读研究",
        "category": "交易研究 / 美股",
        "description": "为 MU、SNDK、WDC、STX 启用 Futu 只读行情、模拟观察和纸面组合风控。",
        "mode_label": "只读 / 模拟",
        "capabilities": [
            "market.storage.readonly",
            "analytics.storage",
            "simulation.observations",
            "simulation.paper_portfolio",
            "decision.observation_proposals",
        ],
        "discussion_protocol": {
            "title": "存储产业只读研究协议",
            "rules": [
                "所有成员必须使用本轮冻结的共同证据截面，并区分事实、推断和待验证信息。",
                "MU、SNDK、WDC、STX 必须在同一市场时间和数据质量口径下比较，缺失或过期时不得补造行情。",
                "候选方案必须保留最强反证、失效条件和风险复核，只能形成研究或模拟观察。",
                "模型置信度不是统计胜率；真实胜率只能来自已到期样本和滚动验证。",
            ],
            "director_focus": "优先调度能补齐共同数据截面、产业证据、多空反证、模拟方案和风险复核的成员。",
        },
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "can_replace_user_decision": False,
        "arbitrary_code_loading_allowed": False,
        "user_final_decision_required": True,
    },
    "structured_turn_contract_v1": {
        "id": "structured_turn_contract_v1",
        "manifest_version": "capability_pack_manifest_v1",
        "pack_version": "1.0.0",
        "core_protocol_range": ">=1.0.0 <2.0.0",
        "dependencies": [],
        "domain_adapter_ids": [],
        "ui_contribution_ids": ["core.capability_pack_settings/v1"],
        "name": "结构化专业发言合同 v1",
        "category": "通用共创",
        "description": "让正式轮次的每位 AI 同时提交可见发言与可核验的主张、回应、候选、风险和下一步。",
        "mode_label": "可审计讨论",
        # Historical room/version snapshots may still contain this ID.  New
        # formal rounds enforce the protocol in the room kernel regardless of
        # mutable pack selection, so clients render it as system-managed.
        "system_managed": True,
        "scope": "formal_round_core",
        "capabilities": [
            "discussion.turn_contract_v1",
        ],
        "discussion_protocol": {
            "title": "结构化专业发言协议",
            "rules": [
                "每条正式发言必须提交 turn_contract_v1，并只引用系统允许的本轮消息、冻结资料或唯一冻结市场快照。",
                "角色交付必须匹配其阶段、立场和能力；无效合同不计入角色覆盖或会议收敛。",
                "模型主观置信度不是统计胜率、概率或收益承诺。",
                "合同不得包含账户、密钥、工具调用、订单、支付或其他执行字段。",
            ],
            "director_focus": "只把具有合格结构化发言合同的成员计入专业覆盖，并优先修复未满足的角色交付。",
        },
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "can_replace_user_decision": False,
        "arbitrary_code_loading_allowed": False,
        "user_final_decision_required": True,
    },
    "structured_project_research": {
        "id": "structured_project_research",
        "manifest_version": "capability_pack_manifest_v1",
        "pack_version": "1.0.0",
        "core_protocol_range": ">=1.0.0 <2.0.0",
        "dependencies": [],
        "domain_adapter_ids": [],
        "ui_contribution_ids": ["project_research.artifact_workspace/v1"],
        "name": "结构化项目研究",
        "category": "项目研究",
        "description": "把需求证据、候选方案、资源约束、风险登记和用户验收组织成可复核研究。",
        "mode_label": "仅研究",
        "capabilities": [
            "research.project.evidence_map",
            "research.project.option_matrix",
            "research.project.risk_register",
            "decision.project_recommendation",
        ],
        "discussion_protocol": {
            "title": "结构化项目研究协议",
            "rules": [
                "先把用户原话、已验证事实、工作假设和待补证据分开记录，不把假设冒充需求。",
                "至少比较两个真实讨论过的候选方案，并使用价值、成本、周期、依赖和可逆性等共同维度。",
                "关键风险必须写明触发信号、影响、责任角色、缓解动作和验收条件。",
                "最终只能提交条件化候选建议、保留方案和下一项验证，由用户确认是否采用。",
            ],
            "director_focus": "优先调度能补齐需求证据、共同评价维度、资源约束、失败路径和验收条件的成员。",
        },
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "can_replace_user_decision": False,
        "arbitrary_code_loading_allowed": False,
        "user_final_decision_required": True,
    },
    "project_readiness_review": {
        "id": "project_readiness_review",
        "manifest_version": "capability_pack_manifest_v2",
        "pack_version": "1.0.0",
        "core_protocol_range": ">=1.0.0 <2.0.0",
        "dependencies": ["structured_project_research"],
        "domain_adapter_ids": ["project_readiness"],
        "domain_adapter_port_requirements": [{
            "port_id": "core.artifact.projection/v1",
            "requirement": "required",
            "cardinality": "one",
            "version_range": ">=1.0.0 <2.0.0",
        }],
        "ui_contribution_ids": ["project_readiness.artifact_workspace/v1"],
        "name": "项目就绪度只读复核",
        "category": "项目研究",
        "description": "从精确产物版本和证据关系封印生成确定性的结构缺口投影。",
        "mode_label": "只读复核",
        "capabilities": ["research.project.readiness_review"],
        "discussion_protocol": {
            "title": "项目就绪度只读复核协议",
            "rules": [
                "只读取精确产物版本与已封印证据关系，不调用模型、行情或外部服务。",
                "只列出结构缺口、证据缺口与阻断条件，不排名、不推荐赢家、不产生批准。",
                "复核结果不能替代或修改用户最终决定。",
            ],
            "director_focus": "保持只读结构复核边界，不把确定性缺口投影解释成最终选择。",
        },
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "can_replace_user_decision": False,
        "arbitrary_code_loading_allowed": False,
        "user_final_decision_required": True,
    },
    "project_round_focus": {
        "id": "project_round_focus",
        "manifest_version": "capability_pack_manifest_v2",
        "pack_version": "1.0.0",
        "core_protocol_range": ">=1.0.0 <2.0.0",
        "dependencies": ["project_readiness_review"],
        "domain_adapter_ids": ["project_round_focus"],
        "domain_adapter_port_requirements": [{
            "port_id": "core.round.context/v1",
            "requirement": "required",
            "cardinality": "one",
            "version_range": ">=1.0.0 <2.0.0",
        }],
        "ui_contribution_ids": ["project_round_focus.room_inspector/v1"],
        "name": "Project next-round focus",
        "category": "Project research",
        "description": (
            "Build a deterministic, read-only next-round gap checklist from an "
            "exact project-readiness projection."
        ),
        "mode_label": "Read-only planning",
        "capabilities": ["research.project.round_focus"],
        "discussion_protocol": {
            "title": "Project next-round focus protocol",
            "rules": [
                (
                    "Artifact-backed checklists require exact sealed references; bootstrap "
                    "explicitly seals the absence of a confirmed artifact."
                ),
                "The checklist may prefill a user-editable objective but never starts a round.",
                "No ranking, winner, approval, member assignment, or workflow mutation is produced.",
            ],
            "director_focus": (
                "Use the frozen checklist only to repair evidence, structural, and blocker gaps."
            ),
        },
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
        "can_replace_user_decision": False,
        "arbitrary_code_loading_allowed": False,
        "user_final_decision_required": True,
    },
}


def clean_capability_pack_ids(
    value: Any,
    *,
    default: Iterable[str] | None = None,
) -> list[str]:
    candidate = list(default or []) if value is None else value
    if not isinstance(candidate, list):
        raise ValueError("capability_pack_ids 必须是字符串数组")
    if len(candidate) > 12:
        raise ValueError("单个房间最多启用 12 个领域能力包")
    clean: list[str] = []
    for raw_id in candidate:
        pack_id = str(raw_id or "").strip().lower()
        if not pack_id:
            continue
        if pack_id not in CAPABILITY_PACKS:
            raise ValueError(f"未知领域能力包：{pack_id}")
        pack = CAPABILITY_PACKS[pack_id]
        if (
            str(pack.get("execution_capability") or "").strip().lower() != "none"
            or pack.get("live_trading_allowed") is not False
        ):
            raise ValueError(f"领域能力包违反不可执行安全边界：{pack_id}")
        if pack_id not in clean:
            clean.append(pack_id)
    return clean


def capabilities_for_packs(pack_ids: Iterable[str] | None) -> list[str]:
    capabilities = list(CORE_ROOM_CAPABILITIES)
    for pack_id in clean_capability_pack_ids(list(pack_ids or [])):
        capabilities.extend(CAPABILITY_PACKS[pack_id]["capabilities"])
    return list(dict.fromkeys(str(item) for item in capabilities))


def room_has_capability(room: dict[str, Any] | None, capability: str) -> bool:
    return str(capability or "") in {
        str(item or "") for item in (room or {}).get("capabilities") or []
    }


def capability_pack_prompt(pack_ids: Iterable[str] | None) -> str:
    sections: list[str] = []
    for pack_id in clean_capability_pack_ids(list(pack_ids or [])):
        pack = CAPABILITY_PACKS[pack_id]
        protocol = pack.get("discussion_protocol") or {}
        rules = protocol.get("rules") if isinstance(protocol, dict) else []
        clean_rules = [str(rule).strip() for rule in rules or [] if str(rule).strip()]
        if not clean_rules:
            continue
        title = str(protocol.get("title") or pack["name"]).strip()
        numbered = "\n".join(
            f"{index}. {rule}" for index, rule in enumerate(clean_rules, start=1)
        )
        sections.append(f"领域能力协议【{title}】：\n{numbered}")
    return "\n".join(sections)


def capability_pack_director_prompt(pack_ids: Iterable[str] | None) -> str:
    focus_items: list[str] = []
    for pack_id in clean_capability_pack_ids(list(pack_ids or [])):
        pack = CAPABILITY_PACKS[pack_id]
        protocol = pack.get("discussion_protocol") or {}
        focus = str(protocol.get("director_focus") or "").strip() if isinstance(protocol, dict) else ""
        if focus:
            focus_items.append(f"{pack['name']}：{focus}")
    return "领域调度重点：" + " ".join(focus_items) if focus_items else ""


def capability_pack_catalog() -> list[dict[str, Any]]:
    clean_capability_pack_ids(list(CAPABILITY_PACKS))
    catalog: list[dict[str, Any]] = []
    for pack in CAPABILITY_PACKS.values():
        public = deepcopy(pack)
        public["manifest_sha256"] = canonical_sha256(public)
        catalog.append(public)
    return catalog
