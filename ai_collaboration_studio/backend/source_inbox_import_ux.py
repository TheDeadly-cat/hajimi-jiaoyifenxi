from __future__ import annotations

"""Pure Phase 7 projections for manual Source Inbox import UX.

The builders in this module do not open SQLite, call a Provider, fetch a URL,
control a browser, or create a ChatGPT task.  The existing import transaction
remains the only authority for replay, conflict, and persistence decisions.
"""

import copy
import json
from typing import Any

from .source_inbox_contracts import (
    EXTERNAL_UNVERIFIED,
    MAX_SOURCE_IMPORT_BYTES,
    MAX_SOURCE_ITEMS,
    MAX_SOURCES_PER_ITEM,
    MAX_TOTAL_SOURCES,
    PROJECT_SOURCE_ITEM_VERSION,
    SOURCE_IMPORT_KEY_VERSION,
    SOURCE_IMPORT_PACKET_VERSION,
    SOURCE_RECOMMENDED_ROUTES,
    SOURCE_SEVERITIES,
    canonical_sha256,
)


SOURCE_IMPORT_PREVIEW_VERSION = "source_import_preview_v1"
SOURCE_MONITORING_PROMPT_TEMPLATE_VERSION = "source_monitoring_prompt_template_v1"
SOURCE_MONITORING_PROMPT_TEMPLATE_ID = "manual_chatgpt_source_monitoring"
MANUAL_CHATGPT_SOURCE_CHANNEL = "chatgpt_manual"
RESERVED_MONITORING_SOURCE_CHANNELS = (
    "futu_anomaly_monitor",
    "official_source_monitor",
)


def build_source_import_preview(
    packet: dict[str, Any],
    provisional_receipt: dict[str, Any],
    *,
    received_at_ms: int,
) -> dict[str, Any]:
    """Project one already accepted packet without consulting persistence."""

    candidate = {
        "source_payload_bytes": provisional_receipt["source_payload_bytes"],
        "source_payload_sha256": provisional_receipt["source_payload_sha256"],
        "normalized_packet_sha256": provisional_receipt["normalized_packet_sha256"],
        "import_key_version": provisional_receipt["import_key_version"],
        "import_key_sha256": provisional_receipt["import_key_sha256"],
        "item_count": provisional_receipt["item_count"],
        "source_count": provisional_receipt["source_count"],
        "item_fingerprints": list(provisional_receipt["item_fingerprints"]),
    }
    preview: dict[str, Any] = {
        "version": SOURCE_IMPORT_PREVIEW_VERSION,
        "valid": True,
        "received_at_ms": received_at_ms,
        "packet": copy.deepcopy(packet),
        "candidate": candidate,
        "store_disposition": {
            "evaluated": False,
            "reason": "preview_does_not_open_database",
        },
        "external_claims_verification": EXTERNAL_UNVERIFIED,
        "safety": {
            "database_reads_performed": 0,
            "database_writes_performed": 0,
            "provider_calls_performed": 0,
            "market_calls_performed": 0,
            "network_requests_performed": 0,
            "formal_rounds_created": 0,
            "chatgpt_page_controlled": False,
            "chatgpt_automation_performed": False,
            "external_task_created": False,
            "import_performed": False,
            "execution_capability": "none",
            "revalidation_required": True,
            "user_confirmation_required": True,
        },
    }
    preview["preview_sha256"] = canonical_sha256(preview)
    return preview


def _result_template() -> dict[str, Any]:
    """Return an intentionally non-importable skeleton with visible placeholders."""

    return {
        "version": SOURCE_IMPORT_PACKET_VERSION,
        "source_channel": MANUAL_CHATGPT_SOURCE_CHANNEL,
        "source_key": "{{source_key_slug}}",
        "external_run_id": "{{unique_external_run_id}}",
        "checked_at": "{{checked_at_rfc3339}}",
        "cutoff_at": "{{cutoff_at_rfc3339}}",
        "meaningful_change": True,
        "items": [
            {
                "version": PROJECT_SOURCE_ITEM_VERSION,
                "external_item_id": "{{stable_external_item_id}}",
                "item_type": "{{lowercase_item_type}}",
                "severity": "info",
                "occurred_at": "{{occurred_at_rfc3339}}",
                "published_at": "{{published_at_rfc3339}}",
                "entities": [
                    {
                        "kind": "{{entity_kind}}",
                        "id": "{{entity_id}}",
                        "label": "{{entity_label}}",
                    }
                ],
                "headline": "{{fact_only_headline}}",
                "summary": "{{bounded_fact_only_summary}}",
                "facts": [
                    {
                        "claim": "{{source_supported_claim}}",
                        "source_indexes": [0],
                    }
                ],
                "sources": [
                    {
                        "url": "{{public_http_or_https_url}}",
                        "publisher": "{{publisher}}",
                        "source_type": "{{source_type}}",
                        "published_at": "{{source_published_at_rfc3339}}",
                        "content_sha256": "{{verified_content_sha256}}",
                    }
                ],
                "impact_hypotheses": [
                    {
                        "statement": "{{non_directional_research_hypothesis}}",
                        "affected_area": "{{research_area}}",
                        "time_horizon": "{{time_horizon}}",
                        "source_indexes": [0],
                        "confidence": 0.5,
                    }
                ],
                "unknowns": ["{{material_unknown_or_counterevidence}}"],
                "confidence": 0.5,
                "recommended_route": "notify_only",
                "extensions": {},
            }
        ],
        "generation": {
            "channel": MANUAL_CHATGPT_SOURCE_CHANNEL,
            "model": "",
            "cost": {
                "status": "unavailable",
                "amount": None,
                "currency": "",
                "usage_source": "subscription_unavailable",
            },
            "correlated_output": True,
        },
    }


def build_source_monitoring_prompt_template() -> dict[str, Any]:
    """Build a version-pinned prompt for manual copy/paste into ChatGPT."""

    result_template = _result_template()
    prompt = "\n".join(
        [
            "你是只读公开来源监控助手。监控范围：{{monitoring_scope}}。",
            "只使用用户明确授权且你当前可访问的公开 HTTP(S) 来源；不登录新账户，不读取私有数据、Cookie、本地文件或凭据。",
            "本次运行只生成来源数据。不得下单、交易、转账、访问账户或资金，不得调用本地 Provider，不得创建正式 round，不得声称执行授权。",
            "成功时只输出一个完整 JSON object，不要 Markdown 围栏、解释或额外文本。",
            f"version 必须是 {SOURCE_IMPORT_PACKET_VERSION}，source_channel 和 generation.channel 必须同为 {MANUAL_CHATGPT_SOURCE_CHANNEL}。",
            "所有核心 object 只能包含骨架中的字段。不得新增 execution、trade、order、account、funds、wallet、payment、tool、function、shell 或 command 字段。",
            "时间必须是带时区的 RFC3339；cutoff_at 不得晚于 checked_at。不得伪造时间、引用、正文哈希、模型或费用。",
            "content_sha256 必须是你通过可靠工具从实际证据得到的 64 位小写 SHA-256；无法可靠计算时停止并用普通文本说明失败，不要输出可导入 JSON。",
            "事实和假设必须绑定 source_indexes。影响假设只是待人工复核的研究线索，不是方向预测、因果结论、盈利声明或交易建议。",
            "没有有意义变化时设置 meaningful_change=false 且 items=[]。有变化时必须设置 meaningful_change=true 并输出 1 到 50 个 item。",
            "所有输出的时间、事实、模型、费用、路由建议和影响假设在本地导入后仍是 external_unverified，必须由用户预览并确认。",
            "请替换下面骨架中每个 {{...}} 占位符；未替换的骨架必须视为失败，不得直接返回。",
            json.dumps(result_template, ensure_ascii=False, indent=2),
        ]
    )
    template: dict[str, Any] = {
        "version": SOURCE_MONITORING_PROMPT_TEMPLATE_VERSION,
        "template_id": SOURCE_MONITORING_PROMPT_TEMPLATE_ID,
        "default_source_channel": MANUAL_CHATGPT_SOURCE_CHANNEL,
        "packet_version": SOURCE_IMPORT_PACKET_VERSION,
        "item_version": PROJECT_SOURCE_ITEM_VERSION,
        "prompt": prompt,
        "result_template": result_template,
        "constraints": {
            "one_json_object_only": True,
            "markdown_fence_tolerated": True,
            "manual_copy_paste_only": True,
            "unmodified_template_is_importable": False,
            "public_http_sources_only": True,
            "reserved_source_channels": list(RESERVED_MONITORING_SOURCE_CHANNELS),
            "severities": sorted(SOURCE_SEVERITIES),
            "recommended_routes": sorted(SOURCE_RECOMMENDED_ROUTES),
            "max_payload_bytes": MAX_SOURCE_IMPORT_BYTES,
            "max_items": MAX_SOURCE_ITEMS,
            "max_sources_per_item": MAX_SOURCES_PER_ITEM,
            "max_total_sources": MAX_TOTAL_SOURCES,
        },
        "safety": {
            "database_reads_performed": 0,
            "database_writes_performed": 0,
            "provider_calls_performed": 0,
            "market_calls_performed": 0,
            "network_requests_performed": 0,
            "formal_rounds_created": 0,
            "chatgpt_page_controlled": False,
            "chatgpt_automation_performed": False,
            "external_task_created": False,
            "execution_capability": "none",
            "user_review_required": True,
        },
    }
    template["template_sha256"] = canonical_sha256(template)
    return template


__all__ = [
    "MANUAL_CHATGPT_SOURCE_CHANNEL",
    "RESERVED_MONITORING_SOURCE_CHANNELS",
    "SOURCE_IMPORT_PREVIEW_VERSION",
    "SOURCE_MONITORING_PROMPT_TEMPLATE_ID",
    "SOURCE_MONITORING_PROMPT_TEMPLATE_VERSION",
    "build_source_import_preview",
    "build_source_monitoring_prompt_template",
]
