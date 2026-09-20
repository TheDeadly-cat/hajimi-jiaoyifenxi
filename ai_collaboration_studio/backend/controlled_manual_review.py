"""Operator-only, approval-bound standard reviews using the existing ledger.

Only the reviewed Doubao route is enabled here. This does not enable the normal
HTTP endpoint, relax an import contract, or reuse the evidence-pilot budget.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
import time
from contextlib import closing
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

from .api_pilot import _decimal, write_record
from .decision_lineage import canonical_sha256
from .execution_boundary import (
    AuthorizedTextRequest, authorized_text_request,
    build_text_provider_request, text_generation_body,
)
from .manual_chatgpt import ManualChatGPTService, _review_input, _review_instructions, validate_api_review
from .path_identity import first_reparse_component
from .provider_call_ledger import normalized_token_usage
from .providers.base import ProviderResponse
from .providers.doubao_provider import DoubaoProvider
from .providers.output import OUTPUT_MODE_JSON_OBJECT

VERSION = "controlled_manual_review_v1"
MODEL = "doubao-seed-2-1-pro-260915"
ENDPOINT = "https://ark.cn-beijing.volces.com/api/v3/responses"
DISABLED = frozenset({"openai", "deepseek", "qwen", "glm"})
KINDS = ("fact_check", "counterargument", "risk_review")
APP = Path(__file__).resolve().parents[1]


class ControlledReviewError(ValueError):
    def __init__(self, message, *, code="controlled_review_rejected"):
        super().__init__(message)
        self.code = code


def verify_candidate(sha: str) -> None:
    head = subprocess.check_output(["git", "-C", str(APP), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(APP), "status", "--porcelain"], text=True).strip()
    if head != sha or dirty:
        raise ControlledReviewError("候选源码已变化或不干净；未发送请求", code="candidate_changed")


def isolated_review_database(value: str) -> Path:
    raw = Path(value).expanduser()
    if first_reparse_component(raw) is not None:
        raise ControlledReviewError("复核目录不能包含重解析路径")
    path = raw.resolve()
    if (path.name != "studio.sqlite3" or path.parent.name != "data"
            or not path.parent.parent.name.startswith("ChatGPTTrial") or not path.is_file()):
        raise ControlledReviewError("只允许已有 ChatGPTTrial 独立副本的 data/studio.sqlite3")
    return path


class ControlledManualReview:
    def __init__(self, store, providers, *, instance_owner, clock=None):
        self.store, self.providers, self.owner = store, providers, instance_owner
        self.clock = clock or (lambda: int(time.time() * 1000))

    def _window(self, config):
        self.owner.assert_held_for(self.store.path)
        verify_candidate(config["candidate_sha"])
        now = self.clock()
        if type(now) is not int or not config["not_before_ms"] <= now < config["expires_at_ms"]:
            raise ControlledReviewError("复核授权尚未生效或已经过期", code="authorization_expired")

    def prepare(self, config):
        self.owner.assert_held_for(self.store.path)
        config = copy.deepcopy(config)
        fields = {"version", "pilot_id", "candidate_sha", "database_path", "room_id", "session_id",
                  "expected_result_sha256", "provider", "model", "max_output_tokens", "max_request_bytes",
                  "not_before_ms", "expires_at_ms", "rate_card", "spend_plan_limit"}
        if type(config) is not dict or set(config) != fields or config["version"] != VERSION:
            raise ControlledReviewError("复核配置字段无效；配置不得包含密钥")
        for field, pattern in (("pilot_id", r"[a-z0-9][a-z0-9_-]{0,79}"),
                               ("candidate_sha", r"[0-9a-f]{40}"),
                               ("expected_result_sha256", r"[0-9a-f]{64}"),
                               ("session_id", r"mcg_[a-z0-9]{1,80}"),
                               ("room_id", r"room_[a-z0-9_]{1,80}")):
            if not isinstance(config[field], str) or not re.fullmatch(pattern, config[field]):
                raise ControlledReviewError("复核身份字段无效")
        if isolated_review_database(config["database_path"]) != self.store.path.resolve():
            raise ControlledReviewError("复核数据库身份不一致")
        verify_candidate(config["candidate_sha"])
        if config["provider"] != "doubao" or config["model"] != MODEL:
            raise ControlledReviewError("本受控入口只启用已核对的豆包固定模型")
        if type(config["max_output_tokens"]) is not int or config["max_output_tokens"] != 1400:
            raise ControlledReviewError("冻结任务要求每次 1400 输出 Token")
        if type(config["max_request_bytes"]) is not int or not 1 <= config["max_request_bytes"] <= 65536:
            raise ControlledReviewError("请求字节上限无效")
        start, end = config["not_before_ms"], config["expires_at_ms"]
        if type(start) is not int or type(end) is not int or not 0 < end - start <= 1_800_000:
            raise ControlledReviewError("必须固定最多 30 分钟的授权窗口")
        rate = config["rate_card"]
        if (type(rate) is not dict or set(rate) != {"currency", "input_per_million", "output_per_million", "source_url", "checked_at"}
                or rate["currency"] != "CNY"):
            raise ControlledReviewError("需要明确的人民币计价依据")
        url = urlparse(rate["source_url"])
        if (url.scheme != "https" or url.hostname not in {"www.volcengine.com", "volcengine.com", "docs.volcengine.com"}
                or url.username or url.password or url.query or url.fragment or not rate["checked_at"]):
            raise ControlledReviewError("价格依据必须指向方舟官方页面并记录核对时间")
        input_rate, output_rate = _decimal(rate["input_per_million"], "input rate"), _decimal(rate["output_per_million"], "output rate")
        limit = _decimal(config["spend_plan_limit"], "spend limit")
        if min(input_rate, output_rate, limit) <= 0:
            raise ControlledReviewError("计价和消费计划必须大于零")
        provider = self.providers.get("doubao")
        if (not getattr(self.providers, "uses_production_providers", False)
                or not DISABLED.issubset(self.providers.disabled_provider_ids)
                or type(provider) is not DoubaoProvider
                or OUTPUT_MODE_JSON_OBJECT not in provider.output_capabilities().modes):
            raise ControlledReviewError("必须使用生产豆包 JSON 路由并禁用其他 Provider")
        session = ManualChatGPTService(self.store).get(config["room_id"], config["session_id"])
        if (not session or session.get("integrity", {}).get("ok") is not True
                or session["result_sha256"] != config["expected_result_sha256"]
                or session["mode"] != "standard" or not session.get("result")):
            raise ControlledReviewError("需要完整性有效的 standard 已导入结果")
        bundle = session["bundle"]
        if (bundle["budget"].get("independent_api_reviews") != 3
                or bundle["planning"]["workload"].get("api_review_output_token_budget") != 4200):
            raise ControlledReviewError("冻结的三次复核预算不一致")
        requests = []
        for index, kind in enumerate(KINDS, 1):
            source = _review_input(session_id=session["id"], bundle=bundle, result=session["result"],
                                   result_sha256=session["result_sha256"], review_kind=kind, review_index=index)
            generation = {"instructions": _review_instructions(kind),
                          "input_text": json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                          "model": MODEL, "max_output_tokens": 1400}
            body = text_generation_body(api="responses", **generation, json_output=True, thinking_disabled=True)
            http = build_text_provider_request(provider._base_url, "responses", body, headers={})
            if http.full_url != ENDPOINT or len(http.data) > config["max_request_bytes"]:
                raise ControlledReviewError("端点或完整请求大小不在计划范围")
            cost = (Decimal(len(http.data) + 256) * input_rate + 1400 * output_rate) / 1_000_000
            requests.append({"index": index, "kind": kind, "generation": generation,
                             "generation_sha256": canonical_sha256(generation), "http_body": body,
                             "http_body_sha256": hashlib.sha256(http.data).hexdigest(), "http_body_bytes": len(http.data),
                             "estimated_input_tokens": len(http.data) + 256, "reserved_cost": str(cost)})
        total = sum(Decimal(row["reserved_cost"]) for row in requests)
        if total > limit:
            raise ControlledReviewError("发送前三次消费计划检查不通过")
        plan = {"version": VERSION, "config": config, "bundle_sha256": session["bundle_sha256"],
                "context_sha256": session["context_sha256"], "result_sha256": session["result_sha256"],
                "endpoint": ENDPOINT, "method": "generate_json", "timeout_seconds": 240,
                "max_calls": 3, "total_output_token_limit": 4200, "requests": requests,
                "estimated_cost_ceiling": str(total), "input_estimation_method": "complete_http_utf8_bytes_plus_256",
                "not_a_bill": True, "supplier_spend_limit_verified": False}
        plan["plan_sha256"] = canonical_sha256(plan)
        return plan

    def run(self, config, *, approved_plan_sha256=""):
        plan = self.prepare(config)
        if not approved_plan_sha256 or approved_plan_sha256 != plan["plan_sha256"]:
            raise ControlledReviewError("未批准这份完整请求计划；未调用")
        config = plan["config"]
        self._window(config)
        provider = self.providers.get("doubao")
        if not provider.status().get("configured"):
            raise ControlledReviewError("本机未提供所选 Provider 密钥")
        if provider._api_key in json.dumps(plan, ensure_ascii=False):
            raise ControlledReviewError("密钥出现在请求材料中；拒绝执行")
        folder = self.store.path.parent / "controlled-review-receipts" / config["session_id"] / plan["plan_sha256"]
        if (folder / "plan.json").exists():
            if json.loads((folder / "plan.json").read_text(encoding="utf-8")) != plan:
                raise ControlledReviewError("既有授权回执发生变化；禁止重发")
        else:
            write_record(folder / "plan.json", plan)
        guarded = _ApprovedProvider(self, provider, plan, folder)
        session = ManualChatGPTService(self.store, providers=guarded).run_api_review(
            config["room_id"], config["session_id"], provider_id="doubao", model=MODEL,
            client_request_id="controlled:" + plan["plan_sha256"], expected_result_sha256=plan["result_sha256"],
        )
        receipts = []
        for path in sorted(folder.glob("response-*.json")):
            receipt = json.loads(path.read_text(encoding="utf-8"))
            unsigned = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
            if canonical_sha256(unsigned) != receipt.get("receipt_sha256") or receipt.get("plan_sha256") != plan["plan_sha256"]:
                raise ControlledReviewError("复核回执完整性失败；禁止重发")
            receipts.append(receipt)
        with closing(self.store._connect()) as connection:
            run = connection.execute("SELECT provider_execution_run_id FROM manual_chatgpt_review_runs WHERE session_id=?", (config["session_id"],)).fetchone()
        attempts = self.store.list_provider_call_attempts(run[0]) if run else []
        if len(receipts) != len(attempts):
            raise ControlledReviewError("复核回执缺失或与账本数量不一致；禁止重发")
        for receipt, attempt in zip(receipts, attempts, strict=True):
            if (receipt["provider_attempt_id"] != attempt["id"] or receipt["usage"] != attempt["usage"]
                    or (receipt["status"] == "RESPONDED") != (attempt["status"] == "RESPONDED")):
                raise ControlledReviewError("复核回执与持久账本不一致；禁止重发")
        return {"version": VERSION, "plan_sha256": plan["plan_sha256"], "session": session,
                "receipts": receipts, "new_http_attempts": guarded.http_attempts,
                "supplier_bill_verified": False, "user_decision_recorded": bool(session.get("confirmation"))}


class _ApprovedProvider:
    """A single-route registry/provider view, used only for one approved run."""
    def __init__(self, control, provider, plan, folder):
        self.control, self.provider, self.plan, self.folder = control, provider, plan, folder
        self.index, self.http_attempts = 0, 0
        self.charged_estimate = Decimal(0)

    def get(self, provider_id):
        return self if provider_id == "doubao" else None

    def status(self):
        return self.provider.status()

    def generate_json(self, **generation):
        config = self.plan["config"]
        if self.index >= 3:
            raise ControlledReviewError("三次复核次数已经用尽")
        row = self.plan["requests"][self.index]
        with closing(self.control.store._connect()) as connection:
            run = connection.execute("SELECT * FROM manual_chatgpt_review_runs WHERE session_id=?", (config["session_id"],)).fetchone()
        if not run or run["client_request_id"] != "controlled:" + self.plan["plan_sha256"]:
            raise ControlledReviewError("调用没有绑定当前授权账本")
        attempts = self.control.store.list_provider_call_attempts(run["provider_execution_run_id"])
        if len(attempts) != self.index + 1 or attempts[-1]["status"] != "STARTED":
            raise ControlledReviewError("复核预约顺序不一致")
        attempt = attempts[-1]
        self.index += 1

        def before_send():
            self.control._window(config)
            remaining = sum(Decimal(r["reserved_cost"]) for r in self.plan["requests"][self.index - 1:])
            if self.charged_estimate + remaining > _decimal(config["spend_plan_limit"], "spend limit"):
                raise ControlledReviewError("发送前的剩余消费计划不足", code="spend_plan_exhausted")

        policy = AuthorizedTextRequest(ENDPOINT, row["http_body_sha256"], config["max_request_bytes"], 240,
                                       before_send=before_send)
        response = ProviderResponse(ok=False, provider="doubao", model=MODEL, error_code="controlled_review_rejected")
        started = time.monotonic()
        error = ""
        usage, cost = {}, None
        try:
            if canonical_sha256(generation) != row["generation_sha256"]:
                raise ControlledReviewError("生成参数与获准请求不一致")
            before_send()
            with authorized_text_request(policy):
                response = self.provider.generate_json(**generation)
            usage = self.control.store._clean_provider_usage(normalized_token_usage(response.usage))
            if not policy.attempted:
                error = "http_attempt_missing"
            elif not response.ok:
                error = response.error_code or "provider_error"
            elif response.provider != "doubao" or response.reported_model != MODEL:
                error = "model_identity_mismatch"
            elif response.response_status != "completed" or response.refused or response.incomplete_reason:
                error = "response_not_complete"
            elif (any(key.startswith("usage_") for key in usage)
                  or type(usage.get("input_tokens")) is not int or type(usage.get("output_tokens")) is not int):
                error = "usage_unknown"
            else:
                rate = config["rate_card"]
                cost = (Decimal(usage["input_tokens"]) * _decimal(rate["input_per_million"], "input rate")
                        + Decimal(usage["output_tokens"]) * _decimal(rate["output_per_million"], "output rate")) / 1_000_000
                if usage["output_tokens"] > 1400 or self.charged_estimate + cost > _decimal(config["spend_plan_limit"], "spend limit"):
                    error = "reported_usage_exceeds_plan"
                else:
                    source = json.loads(row["generation"]["input_text"])
                    validate_api_review(response.content, review_kind=row["kind"],
                                        allowed_evidence_ids={e["evidence_id"] for e in source["evidence_index"]})
        except Exception as exc:
            error = exc.code if isinstance(exc, ControlledReviewError) else "controlled_review_rejected"
        self.http_attempts += int(policy.attempted)
        raw = asdict(response)
        if self.provider._api_key in json.dumps(raw, ensure_ascii=False):
            raw = {"content": "", "credential_echo_redacted": True}
            usage, cost, error = {}, None, "credential_echo_redacted"
            response = ProviderResponse(ok=False, provider="doubao", model=MODEL)
        if cost is not None:
            self.charged_estimate += max(cost, Decimal(row["reserved_cost"]))
        receipt = {"version": VERSION, "plan_sha256": self.plan["plan_sha256"], "review_index": self.index,
                   "provider_attempt_id": attempt["id"], "http_body_sha256": row["http_body_sha256"],
                   "http_attempted": policy.attempted, "status": "FAILED" if error else "RESPONDED",
                   "error_code": error, "response": raw, "usage": usage,
                   "cost_estimate_cny": str(cost) if cost is not None else None,
                   "supplier_bill_verified": False, "claim_status": "unverified_model_output",
                   "elapsed_ms": max(0, int((time.monotonic() - started) * 1000))}
        receipt["receipt_sha256"] = canonical_sha256(receipt)
        write_record(self.folder / f"response-{self.index}.json", receipt)
        return replace(response, ok=not error, error_code=error, usage=usage)
