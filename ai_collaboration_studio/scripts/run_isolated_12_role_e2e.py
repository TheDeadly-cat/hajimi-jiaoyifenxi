from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo


# Direct invocations default to the disposable/local environment.  A real
# run must explicitly set AI_STUDIO_SKIP_LOCAL_ENV=0 in addition to the
# existing paid-call acknowledgement; dry-run below forces the safe value.
os.environ.setdefault("AI_STUDIO_SKIP_LOCAL_ENV", "1")

PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DB = PROJECT_DIR / "runtime" / "collaboration_studio.sqlite3"
SOURCE_ROOM_ID = "room_storage"
EXPECTED_MEMBER_COUNT = 12
MAX_PROVIDER_CALLS = 28
MAX_WALL_SECONDS = 15 * 60
REAL_RUN_ACK = "MAX_28_PROVIDER_CALLS"
STORAGE_SYMBOLS = ("US.MU", "US.SNDK", "US.WDC", "US.STX")
FIXTURE_IR_HOSTS = {
    "US.MU": "investors.micron.com",
    "US.SNDK": "investor.sandisk.com",
    "US.WDC": "investor.wdc.com",
    "US.STX": "investors.seagate.com",
}
ROUND_OBJECTIVE = (
    "基于同一份富途只读行情快照，比较 MU、SNDK、WDC、STX 的产业周期、"
    "基本面、技术与资金、新闻情绪、多空反证、数据质量和风险边界，形成仅供用户复核的"
    "候选研究方案、分歧、待验证事项与模拟观察条件；禁止真实下单。"
)


def _dry_run_earnings_pack(symbol: str) -> dict[str, Any]:
    fiscal_period = "FY2026-Q3"
    release_url = f"https://{FIXTURE_IR_HOSTS[symbol]}/fixture/fy2026-q3"
    pack_id = "earnings_" + hashlib.sha256(
        f"{symbol}|{fiscal_period}|{release_url}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "pack_id": pack_id,
        "version": "official_earnings_pack_v1",
        "symbol": symbol,
        "fiscal_period": fiscal_period,
        "release_url": release_url,
        "source_kind": "company_ir_release",
        "source_type": "company_ir",
        "source_tier": "primary",
        "claim_status": "company_statement",
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


class AcceptanceError(RuntimeError):
    code = "ACCEPTANCE_FAILED"


class SourceDatabaseChanged(AcceptanceError):
    code = "SOURCE_DATABASE_CHANGED"


class SourceRoomInvalid(AcceptanceError):
    code = "SOURCE_ROOM_INVALID"


class OpenAIForbidden(AcceptanceError):
    code = "OPENAI_FORBIDDEN"


class ProviderCallBudgetExceeded(AcceptanceError):
    code = "PROVIDER_CALL_BUDGET_EXCEEDED"


class WallTimeExceeded(AcceptanceError):
    code = "WALL_TIME_EXCEEDED"


class MarketGateFailed(AcceptanceError):
    code = "MARKET_GATE_FAILED"


class ProviderGateFailed(AcceptanceError):
    code = "PROVIDER_GATE_FAILED"


class RoundGateFailed(AcceptanceError):
    code = "ROUND_GATE_FAILED"


class ArtifactGateFailed(AcceptanceError):
    code = "ARTIFACT_GATE_FAILED"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def database_fingerprint(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    files: dict[str, Any] = {}
    for label, candidate in (
        ("main", resolved),
        ("wal", Path(f"{resolved}-wal")),
        ("shm", Path(f"{resolved}-shm")),
    ):
        if not candidate.is_file():
            files[label] = {"present": False}
            continue
        stat = candidate.stat()
        files[label] = {
            "present": True,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": _sha256(candidate),
        }
    return {"files": files}


def _readonly_sqlite_uri(path: Path) -> str:
    encoded = quote(path.resolve().as_posix(), safe="/:")
    return f"file:{encoded}?mode=ro&immutable=1"


def source_write_state_unchanged(
    before: dict[str, Any],
    after: dict[str, Any],
) -> bool:
    """Compare durable database content while ignoring read-lock SHM metadata."""
    before_files = before.get("files") or {}
    after_files = after.get("files") or {}
    before_main = before_files.get("main") or {}
    after_main = after_files.get("main") or {}
    if before_main != after_main:
        return False
    before_wal = before_files.get("wal") or {}
    after_wal = after_files.get("wal") or {}
    before_wal_size = int(before_wal.get("size") or 0)
    after_wal_size = int(after_wal.get("size") or 0)
    before_wal_content = {
        "size": before_wal_size,
        "sha256": (
            str(before_wal.get("sha256") or "")
            if before_wal_size
            else ""
        ),
    }
    after_wal_content = {
        "size": after_wal_size,
        "sha256": (
            str(after_wal.get("sha256") or "")
            if after_wal_size
            else ""
        ),
    }
    return before_wal_content == after_wal_content


def read_source_room(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if not path.is_file():
        raise SourceRoomInvalid("正式数据库不存在。")
    connection = sqlite3.connect(
        _readonly_sqlite_uri(path),
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = bool(connection.execute("PRAGMA query_only").fetchone()[0])
        changes_before = int(connection.total_changes)
        room_row = connection.execute(
            """SELECT
                   id,title,objective,domain,category,template_id,discussion_mode,
                   moderator_member_id,workflow_policy_json
               FROM rooms
              WHERE id=?""",
            (SOURCE_ROOM_ID,),
        ).fetchone()
        if not room_row:
            raise SourceRoomInvalid("正式数据库中不存在 room_storage。")
        member_rows = connection.execute(
            """SELECT
                   id,name,identity,instructions,responsibilities,boundaries,stance,
                   workflow_stage,capabilities_json,provider,model,enabled,position
               FROM members
              WHERE room_id=?
              ORDER BY position,id""",
            (SOURCE_ROOM_ID,),
        ).fetchall()
        changes_after = int(connection.total_changes)
    finally:
        connection.close()

    room = dict(room_row)
    try:
        workflow_policy = json.loads(str(room.pop("workflow_policy_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceRoomInvalid("正式房间工作流政策不是有效 JSON。") from exc
    if not isinstance(workflow_policy, dict):
        raise SourceRoomInvalid("正式房间工作流政策格式无效。")
    room["workflow_policy"] = workflow_policy

    members: list[dict[str, Any]] = []
    for row in member_rows:
        member = dict(row)
        try:
            capabilities = json.loads(str(member.pop("capabilities_json") or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            capabilities = []
        member["capabilities"] = capabilities if isinstance(capabilities, list) else []
        member["enabled"] = bool(member.get("enabled"))
        members.append(member)
    return {
        "room": room,
        "members": members,
    }, {
        "query_only": query_only,
        "total_changes_before": changes_before,
        "total_changes_after": changes_after,
    }


def validate_source_room(source: dict[str, Any]) -> dict[str, int]:
    room = source.get("room") if isinstance(source.get("room"), dict) else {}
    members = source.get("members") if isinstance(source.get("members"), list) else []
    if room.get("id") != SOURCE_ROOM_ID:
        raise SourceRoomInvalid("正式房间 ID 不符合验收目标。")
    if room.get("template_id") != "us_storage_committee":
        raise SourceRoomInvalid("正式房间不是存储产业委员会模板。")
    if room.get("discussion_mode") != "dynamic":
        raise SourceRoomInvalid("正式房间未启用动态主持。")
    if len(members) != EXPECTED_MEMBER_COUNT or any(not member.get("enabled") for member in members):
        raise SourceRoomInvalid("正式房间必须恰好包含 12 位启用成员。")
    if len({str(member.get("id") or "") for member in members}) != EXPECTED_MEMBER_COUNT:
        raise SourceRoomInvalid("正式房间成员 ID 必须唯一。")
    for member in members:
        if not all(
            str(member.get(field) or "").strip()
            for field in ("name", "identity", "stance", "workflow_stage", "provider", "model")
        ):
            raise SourceRoomInvalid("正式房间存在身份或模型路由不完整的成员。")

    moderator_member_id = str(room.get("moderator_member_id") or "").strip()
    if not moderator_member_id:
        raise SourceRoomInvalid("正式房间必须显式指定动态主持成员。")
    if not any(
        str(member.get("id") or "") == moderator_member_id
        for member in members
    ):
        raise SourceRoomInvalid("正式房间指定的动态主持不属于当前十二位启用成员。")

    provider_counts = Counter(
        str(member.get("provider") or "").strip().lower()
        for member in members
    )
    if provider_counts.get("openai", 0):
        raise OpenAIForbidden("正式房间存在 OpenAI 路由。")
    if set(provider_counts) != {"deepseek", "doubao"}:
        raise SourceRoomInvalid("正式房间只允许 DeepSeek 与豆包路由。")
    if sum(
        1 for member in members
        if str(member.get("stance") or "") == "data_guardian"
    ) != 1:
        raise SourceRoomInvalid("正式房间必须恰好包含一位数据质量官。")
    if sum(
        1 for member in members
        if str(member.get("stance") or "") == "facilitator"
    ) != 1:
        raise SourceRoomInvalid("正式房间必须恰好包含一位主持人。")

    safety = room.get("workflow_policy") if isinstance(room.get("workflow_policy"), dict) else {}
    if (
        safety.get("execution_capability") != "none"
        or safety.get("live_trading_allowed") is not False
        or safety.get("user_confirmation_required") is not True
    ):
        raise SourceRoomInvalid("正式房间只读与用户确认安全边界不完整。")
    return dict(sorted(provider_counts.items()))


def strengthen_policy(
    source_policy: dict[str, Any],
    *,
    policy_from_json: Callable[[Any, str], dict[str, Any]],
    validate_workflow_policy: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    policy = copy.deepcopy(
        policy_from_json(source_policy, "us_storage_committee")
    )
    canonical_policy = policy_from_json(None, "us_storage_committee")
    canonical_stage_coverage = canonical_policy.get("minimum_stage_coverage") or {}
    stage_coverage = policy.get("minimum_stage_coverage") or {}
    policy["minimum_stage_coverage"] = {
        stage: max(
            int(stage_coverage.get(stage) or 0),
            int(canonical_stage_coverage.get(stage) or 0),
        )
        for stage in canonical_policy.get("stage_order") or []
    }
    policy["minimum_successful_members"] = EXPECTED_MEMBER_COUNT
    policy["max_turns_per_member"] = 2
    policy["follow_up_budget"] = 1
    policy["user_confirmation_required"] = True
    policy["execution_capability"] = "none"
    policy["live_trading_allowed"] = False
    requirements = list(policy.get("required_coverage") or [])
    if not any(str(item.get("id") or "") == "data_quality" for item in requirements):
        requirements.append({
            "id": "data_quality",
            "label": "数据质量与防泄漏",
            "minimum": 1,
            "any_of": {
                "stances": ["data_guardian"],
                "capabilities": ["data_quality"],
            },
            "is_counterargument": False,
        })
    requirement_ids = {
        str(item.get("id") or "")
        for item in requirements
        if isinstance(item, dict)
    }
    for canonical_requirement in canonical_policy.get("required_coverage") or []:
        requirement_id = str(canonical_requirement.get("id") or "")
        if requirement_id and requirement_id not in requirement_ids:
            requirements.append(copy.deepcopy(canonical_requirement))
            requirement_ids.add(requirement_id)
    policy["required_coverage"] = requirements
    return validate_workflow_policy(policy)


def _numeric_usage(value: Any, prefix: str = "") -> dict[str, int]:
    totals: dict[str, int] = {}
    if not isinstance(value, dict):
        return totals
    for key, item in value.items():
        clean_key = str(key or "").strip().lower()
        path = f"{prefix}.{clean_key}" if prefix else clean_key
        if isinstance(item, bool):
            continue
        if isinstance(item, int) and ("token" in path or "cached" in path):
            totals[path] = max(0, item)
        elif isinstance(item, dict):
            for nested_key, nested_value in _numeric_usage(item, path).items():
                totals[nested_key] = totals.get(nested_key, 0) + nested_value
    return totals


@dataclass
class CallLedger:
    mode: str
    max_calls: int = MAX_PROVIDER_CALLS
    wall_seconds: int = MAX_WALL_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    records: list[dict[str, Any]] = field(default_factory=list)
    openai_rejections: int = 0

    def reject_openai(self) -> None:
        self.openai_rejections += 1
        raise OpenAIForbidden("OpenAI 已被验收器硬拒绝。")

    def begin(
        self,
        *,
        provider: str,
        model: str,
        kind: str,
        external: bool,
    ) -> tuple[int, float]:
        provider_id = str(provider or "").strip().lower()
        if provider_id == "openai":
            self.reject_openai()
        if time.monotonic() - self.started_at > self.wall_seconds:
            raise WallTimeExceeded("验收总时限已到。")
        if len(self.records) >= self.max_calls:
            raise ProviderCallBudgetExceeded(
                f"Provider 调用已达到 {self.max_calls} 次硬上限。"
            )
        self.records.append({
            "provider": provider_id,
            "model": str(model or "")[:100],
            "kind": str(kind or "unknown")[:40],
            "external": bool(external),
            "ok": False,
            "elapsed_ms": 0,
            "usage": {},
        })
        return len(self.records) - 1, time.perf_counter()

    def finish(
        self,
        handle: tuple[int, float],
        *,
        ok: bool,
        usage: Any = None,
    ) -> None:
        index, started = handle
        self.records[index]["ok"] = bool(ok)
        self.records[index]["elapsed_ms"] = int(
            (time.perf_counter() - started) * 1000
        )
        self.records[index]["usage"] = _numeric_usage(usage)

    def summary(self) -> dict[str, Any]:
        by_provider = Counter(record["provider"] for record in self.records)
        by_kind = Counter(record["kind"] for record in self.records)
        usage: dict[str, dict[str, int]] = {}
        for record in self.records:
            provider_usage = usage.setdefault(record["provider"], {})
            for key, value in record.get("usage", {}).items():
                provider_usage[key] = provider_usage.get(key, 0) + int(value)
        return {
            "hard_limit": self.max_calls,
            "total_calls": len(self.records),
            "external_network_calls": sum(
                1 for record in self.records if record["external"]
            ),
            "openai_network_calls": sum(
                1
                for record in self.records
                if record["external"] and record["provider"] == "openai"
            ),
            "openai_rejections": self.openai_rejections,
            "by_provider": dict(sorted(by_provider.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "failed_calls": sum(1 for record in self.records if not record["ok"]),
            "usage_tokens": usage,
            "retry_count": 0,
            "cross_provider_fallback_count": 0,
        }


class BudgetedProvider:
    def __init__(
        self,
        delegate: Any,
        ledger: CallLedger,
        *,
        external: bool,
    ) -> None:
        self.delegate = delegate
        self.ledger = ledger
        self.external = external
        self.provider_id = str(getattr(delegate, "provider_id", "")).lower()

    def status(self) -> dict[str, Any]:
        return self.delegate.status()

    def probe(self, *, model: str = "") -> Any:
        handle = self.ledger.begin(
            provider=self.provider_id,
            model=model,
            kind="preflight",
            external=self.external,
        )
        try:
            result = self.delegate.probe(model=model)
        except Exception:
            self.ledger.finish(handle, ok=False)
            raise
        ready = bool(
            getattr(result, "configured", False)
            and getattr(result, "reachable", False)
            and getattr(result, "model_access", False)
        )
        self.ledger.finish(handle, ok=ready)
        return result

    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> Any:
        if "隐藏主持调度器" in instructions:
            kind = "director"
        elif "会议产物整理器" in instructions:
            kind = "artifact"
        else:
            kind = "speaker"
        return self._generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
            kind=kind,
            structured=False,
        )

    def generate_json(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str = "",
    ) -> Any:
        return self._generate(
            instructions=instructions,
            input_text=input_text,
            model=model,
            kind="artifact",
            structured=True,
        )

    def _generate(
        self,
        *,
        instructions: str,
        input_text: str,
        model: str,
        kind: str,
        structured: bool,
    ) -> Any:
        handle = self.ledger.begin(
            provider=self.provider_id,
            model=model,
            kind=kind,
            external=self.external,
        )
        try:
            generate = (
                getattr(self.delegate, "generate_json", None)
                if structured
                else None
            )
            if not callable(generate):
                generate = self.delegate.generate
            response = generate(
                instructions=instructions,
                input_text=input_text,
                model=model,
            )
        except Exception:
            self.ledger.finish(handle, ok=False)
            raise
        response_provider = str(getattr(response, "provider", "") or "").lower()
        if response_provider and response_provider != self.provider_id:
            self.ledger.finish(
                handle,
                ok=False,
                usage=getattr(response, "usage", None),
            )
            raise ProviderGateFailed("Provider 返回了不一致的路由标识。")
        self.ledger.finish(
            handle,
            ok=bool(getattr(response, "ok", False)),
            usage=getattr(response, "usage", None),
        )
        return response


def build_registry(
    *,
    provider_registry_class: type,
    providers: dict[str, Any],
    ledger: CallLedger,
) -> Any:
    class StrictProviderRegistry(provider_registry_class):
        def get(self, provider_id: str) -> Any:
            clean_id = str(provider_id or "").strip().lower()
            if clean_id == "openai":
                ledger.reject_openai()
            return super().get(clean_id)

    return StrictProviderRegistry(providers)


def build_dry_provider(
    provider_id: str,
    default_model: str,
    *,
    provider_response_class: type,
    provider_probe_result_class: type,
    fixture_profile: str = "storage",
) -> Any:
    clean_fixture_profile = str(fixture_profile or "storage").strip().lower()
    if clean_fixture_profile not in {"storage", "generic"}:
        raise ValueError("dry-run fixture_profile 必须是 storage 或 generic。")

    class DryProvider:
        def __init__(self) -> None:
            self.provider_id = provider_id

        def status(self) -> dict[str, Any]:
            return {
                "id": provider_id,
                "name": f"Local dry-run {provider_id}",
                "configured": True,
                "model": default_model,
            }

        def probe(self, *, model: str = "") -> Any:
            return provider_probe_result_class(
                provider=provider_id,
                model=model or default_model,
                configured=True,
                reachable=True,
                model_access=True,
                latency_ms=0,
                message="本地 dry-run 路由可用。",
            )

        @staticmethod
        def _candidate_id(input_text: str) -> str:
            match = re.search(
                r"候选成员：(\[.*?\])\s*\n\n共享证据：",
                input_text,
                re.DOTALL,
            )
            if not match:
                return ""
            try:
                candidates = json.loads(match.group(1))
            except (TypeError, ValueError, json.JSONDecodeError):
                return ""
            if not isinstance(candidates, list) or not candidates:
                return ""
            return str((candidates[0] or {}).get("member_id") or "")

        @staticmethod
        def _canonical_candidate_snapshot(
            input_text: str,
            marker: str,
        ) -> dict[str, Any] | None:
            """Read one server-authored prompt snapshot without fuzzy parsing."""

            marker_index = input_text.find(marker)
            if marker_index < 0:
                return None
            payload_start = input_text.find("{", marker_index + len(marker))
            if payload_start < 0:
                return None
            try:
                payload, _ = json.JSONDecoder().raw_decode(input_text[payload_start:])
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

        @staticmethod
        def _artifact_json(input_text: str) -> str:
            message_ids = re.findall(r"\[消息:([A-Za-z0-9_-]+)\]", input_text)
            evidence_id = message_ids[-1] if message_ids else ""
            evidence = [{"type": "message", "id": evidence_id}] if evidence_id else []
            if clean_fixture_profile == "generic":
                summary = "本轮已完成四个可编辑职责的通用方案讨论，所有结论仍等待用户核验。"
                disagreement = "可逆小步验证与一次性完整推进的收益、成本和失败路径仍需复核。"
                unknown = "需由用户逐条核对前提、证据来源、资源约束和失效条件。"
                decision_options = [
                    {
                        "id": "option_reversible_pilot",
                        "title": "先做可逆小步验证",
                        "description": "先执行不含外部动作的有限验证，用明确验收条件决定是否扩大范围。",
                        "benefits": ["能够低成本暴露关键假设和失败路径"],
                        "risks": ["获得完整结论的速度较慢"],
                        "evidence": evidence,
                    },
                    {
                        "id": "option_full_scope",
                        "title": "直接推进完整方案",
                        "description": "一次性覆盖全部目标，但在关键证据未核验前保持为讨论候选。",
                        "benefits": ["路径更直接且整合成本更低"],
                        "risks": ["错误前提会放大返工范围"],
                        "evidence": evidence,
                    },
                ]
                preferred_option_id = "option_reversible_pilot"
                rationale = "关键前提仍待用户核验，先做可逆验证更符合证据和安全边界。"
            else:
                summary = "本轮已完成十二个职责的研究讨论，所有结论仍等待用户核验。"
                disagreement = "四家公司在周期暴露、技术路线和风险收益上的排序仍需复核。"
                unknown = "需由用户逐条核对行情截面、来源和失效条件。"
                decision_options = [
                    {
                        "id": "option_observe_first",
                        "title": "先观察并补证据",
                        "description": "保持纸面观察，等待关键证据通过用户核验后再调整候选排序。",
                        "benefits": ["避免在证据未核验时提前收敛"],
                        "risks": ["可能错过短期变化"],
                        "evidence": evidence,
                    },
                    {
                        "id": "option_paper_compare",
                        "title": "建立多标的纸面对照",
                        "description": "仅建立模拟观察和风险预算，用到期样本比较候选逻辑。",
                        "benefits": ["能够形成可验证记录"],
                        "risks": ["历史表现不能外推未来"],
                        "evidence": evidence,
                    },
                ]
                preferred_option_id = "option_observe_first"
                rationale = "当前证据仍待用户逐条核验，先观察更符合安全与数据质量边界。"
            payload = {
                "summary": summary,
                "summary_evidence": evidence,
                "conclusions": [{
                    "text": "当前只能形成候选研究方案，不能替代用户决策。",
                    "evidence": evidence,
                }],
                "disagreements": [{
                    "text": disagreement,
                    "positions": ["候选排序有支持证据", "反证与数据限制仍然存在"],
                    "evidence": evidence,
                }],
                "unknowns": [{
                    "text": unknown,
                    "evidence": evidence,
                }],
                "actions": [{
                    "text": "用户复核证据后决定支持、保留或退回。",
                    "owner": "用户",
                    "due": "",
                    "state": "open",
                    "evidence": evidence,
                }],
                "decision": {
                    "status": "candidate",
                    "options": decision_options,
                    "preferred_option_id": preferred_option_id,
                    "rationale": rationale,
                    "evidence": evidence,
                },
            }
            return json.dumps(payload, ensure_ascii=False)

        @staticmethod
        def _turn_contract_json(instructions: str, input_text: str) -> str:
            message_ids = re.findall(r"\[消息:([A-Za-z0-9_-]+)\]", input_text)
            message_id = message_ids[-1] if message_ids else ""
            prior_ai_match = re.search(
                r"本轮此前正式 AI 消息ID：([^\n]+)",
                input_text,
            )
            prior_ai_ids = [
                item.strip()
                for item in str(prior_ai_match.group(1) if prior_ai_match else "").split(",")
                if item.strip() and not item.strip().startswith("无（")
            ]
            prior_ai_message_id = prior_ai_ids[-1] if prior_ai_ids else ""
            evidence = (
                [{"type": "message", "id": message_id, "role": "support"}]
                if message_id
                else []
            )
            stage_match = re.search(r"流程阶段：([a-z_]+)。", instructions)
            stance_match = re.search(r"立场：([a-z_]+)。", instructions)
            capability_match = re.search(r"服务端能力：([^。]+)。", instructions)
            stage = str(stage_match.group(1) if stage_match else "flexible")
            stance = str(stance_match.group(1) if stance_match else "neutral")
            capabilities = {
                item.strip()
                for item in str(capability_match.group(1) if capability_match else "").split(",")
                if item.strip() and item.strip() != "无"
            }
            payload: dict[str, Any] = {
                "version": "turn_contract_v1",
                "claims": [],
                "responds_to": [],
                "candidate_updates": [],
                "risks": [],
                "next_actions": [],
                "confidence": {
                    "kind": "model_subjective",
                    "value": None,
                    "label": "unknown",
                    "basis": "",
                },
            }

            def candidate(
                candidate_id: str,
                title: str,
                action: str,
                *,
                with_evidence: bool = True,
            ) -> dict[str, Any]:
                generic = clean_fixture_profile == "generic"
                return {
                    "id": candidate_id,
                    "title": title,
                    "action": action,
                    "symbol": "" if generic else "US.MU",
                    "direction": "UNSPECIFIED" if generic else "UP",
                    "horizon_days": None if generic else 20,
                    "thesis": (
                        "仅基于本轮允许证据形成可撤回的通用方案候选。"
                        if generic
                        else "仅基于本轮冻结证据形成可撤回的纸面候选。"
                    ),
                    "invalidation": (
                        "关键前提未通过核验或主要反证成立时撤回。"
                        if generic
                        else "统一数据截面失效或主要反证成立时撤回。"
                    ),
                    "evidence": evidence if with_evidence else [],
                }

            def canonical_candidate(
                source: dict[str, Any],
                action: str,
                *,
                candidate_evidence: list[dict[str, Any]] | None = None,
            ) -> dict[str, Any]:
                return {
                    "id": str(source.get("id") or ""),
                    "title": str(source.get("title") or ""),
                    "action": action,
                    "symbol": str(source.get("symbol") or ""),
                    "direction": str(source.get("direction") or "UNSPECIFIED"),
                    "horizon_days": source.get("horizon_days"),
                    "thesis": str(source.get("thesis") or ""),
                    "invalidation": str(source.get("invalidation") or ""),
                    "evidence": (
                        list(candidate_evidence)
                        if candidate_evidence is not None
                        else list(evidence)
                    ),
                }

            profiles: set[str] = set()
            if stage in {"facilitate", "analysis", "debate", "plan", "risk", "decision"}:
                profiles.add(stage)
            if stance == "facilitator" or "facilitation" in capabilities:
                profiles.add("facilitate")
            if stance in {"sector", "fundamental", "technical", "sentiment", "data_guardian"} or capabilities.intersection({
                "evidence_review", "storage_sector_analysis", "fundamental_analysis",
                "technical_analysis", "sentiment_analysis", "data_quality_review",
            }):
                profiles.add("analysis")
            if stance in {"bull", "bear", "challenger"} or capabilities.intersection({"bull_case", "bear_case", "critical_review"}):
                profiles.add("debate")
            if stance == "paper_trader" or "simulation_planning" in capabilities:
                profiles.add("plan")
            if stance == "risk" or "risk_review" in capabilities:
                profiles.add("risk")
            if stance == "portfolio_manager" or "decision_synthesis" in capabilities:
                profiles.add("decision")

            # The first formal AI turn sees only the user's round-opening message.
            # Every later fixture turn sees that message plus at least one prior AI
            # message, so emit an auditable AI-to-AI edge for roles whose specialist
            # profile does not already add a stronger challenge/qualification edge.
            if prior_ai_message_id and not profiles.intersection({"debate", "risk"}):
                payload["responds_to"].append({
                    "type": "message",
                    "id": prior_ai_message_id,
                    "relation": "supports",
                    "reason": "承接上一位智能体的结论，并补充本角色负责的证据或约束。",
                })

            if "facilitate" in profiles:
                payload["claims"].append({
                    "id": "scope_claim",
                    "kind": "unknown",
                    "text": "需统一评价口径并明确待验证信息。",
                    "as_of": "",
                    "evidence": [],
                })
                payload["next_actions"].append({
                    "id": "facilitate_action",
                    "text": "请下一角色按共同证据口径核验。",
                    "owner": "下一位专业成员",
                    "state": "open",
                    "due": "本轮",
                    "evidence": [],
                })
            if "analysis" in profiles:
                payload["claims"].append({
                    "id": "analysis_fact",
                    "kind": "fact",
                    "text": "当前发言只使用本轮允许引用的冻结上下文。",
                    "as_of": "2026-07-30T20:00:01Z",
                    "evidence": evidence,
                })
            if (
                clean_fixture_profile == "generic"
                and not profiles.intersection({"decision", "risk"})
            ):
                # The generic template has no dedicated plan stage.  Its local
                # acceptance fixture therefore creates the two stable objects
                # before the decision role sees them; repeated identical
                # proposals keep the same object identity.
                payload["candidate_updates"].extend([
                    candidate("reversible_pilot", "先做可逆小步验证", "propose"),
                    candidate("full_scope", "直接推进完整方案", "propose"),
                ])
            if "debate" in profiles:
                payload["responds_to"].append({
                    "type": "message",
                    "id": prior_ai_message_id or message_id,
                    "relation": "challenges",
                    "reason": "主要反证和失效条件仍需显式保留。",
                })
                payload["candidate_updates"].append(
                    candidate("debate_candidate", "保留反证的候选", "challenge")
                )
            if "plan" in profiles:
                payload["candidate_updates"].extend([
                    candidate("observe_first", "先观察并补证据", "propose"),
                    candidate("paper_compare", "建立纸面对照", "propose"),
                ])
                payload["next_actions"].append({
                    "id": "paper_action",
                    "text": "定义只读模拟观察条件并等待到期样本。",
                    "owner": "模拟交易员",
                    "state": "open",
                    "due": "本轮后",
                    "evidence": evidence,
                })
            if "risk" in profiles:
                risk_snapshot = DryProvider._canonical_candidate_snapshot(
                    input_text,
                    "服务端规范候选只读快照（candidate_risk_review_v1，仅供风险复核角色）：",
                )
                canonical_candidates = (
                    risk_snapshot.get("candidates")
                    if isinstance(risk_snapshot, dict)
                    and isinstance(risk_snapshot.get("candidates"), list)
                    else []
                )
                for index, source_candidate in enumerate(canonical_candidates[:2]):
                    if not isinstance(source_candidate, dict):
                        continue
                    payload["candidate_updates"].append(
                        canonical_candidate(
                            source_candidate,
                            "support" if index == 0 else "challenge",
                        )
                    )
                    latest_message_id = str(
                        source_candidate.get("latest_message_id") or ""
                    )
                    if latest_message_id and not any(
                        str(item.get("id") or "") == latest_message_id
                        for item in payload["responds_to"]
                        if isinstance(item, dict)
                    ):
                        payload["responds_to"].append({
                            "type": "message",
                            "id": latest_message_id,
                            "relation": "qualifies",
                            "reason": "逐字段复核该候选的当前服务端版本。",
                        })
                payload["responds_to"].append({
                    "type": "message",
                    "id": prior_ai_message_id or message_id,
                    "relation": "qualifies",
                    "reason": "候选必须经过风险触发条件复核。",
                })
                payload["risks"].append({
                    "id": "risk_gate",
                    "text": "数据时间或来源完整性不足会使候选失效。",
                    "severity": "high",
                    "status": "open",
                    "trigger": "任一核心数据过期、未来穿越或来源报错。",
                    "mitigation": "停止收敛并重新冻结共同证据。",
                    "blocking": True,
                    "evidence": evidence,
                })
            if "decision" in profiles:
                decision_snapshot = DryProvider._canonical_candidate_snapshot(
                    input_text,
                    "服务端规范候选只读快照（candidate_lineage_v1，仅供决策角色）：",
                )
                canonical_candidates = (
                    decision_snapshot.get("candidates")
                    if isinstance(decision_snapshot, dict)
                    and isinstance(decision_snapshot.get("candidates"), list)
                    else []
                )
                decision_candidates: list[dict[str, Any]] = []
                for index, source_candidate in enumerate(canonical_candidates[:2]):
                    if not isinstance(source_candidate, dict):
                        continue
                    candidate_evidence = list(evidence)
                    if index == 0:
                        current_reviews = (
                            source_candidate.get("current_risk_reviews")
                            if isinstance(source_candidate.get("current_risk_reviews"), list)
                            else []
                        )
                        review_message_id = next((
                            str(review.get("review_message_id") or "")
                            for review in current_reviews
                            if isinstance(review, dict)
                            and str(review.get("review_message_id") or "")
                        ), "")
                        if review_message_id:
                            candidate_evidence = [{
                                "type": "message",
                                "id": review_message_id,
                                "role": "support",
                            }]
                    decision_candidates.append(
                        canonical_candidate(
                            source_candidate,
                            "select" if index == 0 else "reject",
                            candidate_evidence=candidate_evidence,
                        )
                    )
                if len(decision_candidates) < 2:
                    decision_candidates = (
                        [
                            candidate("reversible_pilot", "先做可逆小步验证", "select"),
                            candidate("full_scope", "直接推进完整方案", "reject"),
                        ]
                        if clean_fixture_profile == "generic"
                        else [
                            candidate("observe_first", "先观察并补证据", "select"),
                            candidate("paper_compare", "建立纸面对照", "reject"),
                        ]
                    )
                payload["candidate_updates"].extend(decision_candidates)
                payload["risks"].append({
                    "id": "decision_risk",
                    "text": "证据未经用户核验，候选不能升级为最终决定。",
                    "severity": "high",
                    "status": "open",
                    "trigger": "存在未核验证据或阻断性分歧。",
                    "mitigation": "保持候选状态并交由用户复核。",
                    "blocking": True,
                    "evidence": evidence,
                })
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

        def generate(
            self,
            *,
            instructions: str,
            input_text: str,
            model: str = "",
        ) -> Any:
            selected_model = model or default_model
            if "隐藏主持调度器" in instructions:
                post_coverage = "是否已进入角色回访阶段：True" in input_text
                discussion_ready = '"ready": true' in input_text
                if post_coverage and discussion_ready:
                    content = json.dumps({
                        "action": "finish",
                        "member_id": "",
                        "reason": "所有配置成员均已覆盖，送交用户复核。",
                    }, ensure_ascii=False)
                else:
                    content = json.dumps({
                        "action": "speak",
                        "member_id": self._candidate_id(input_text),
                        "reason": "选择当前阶段最能补齐证据的成员。",
                    }, ensure_ascii=False)
            elif "会议产物整理器" in instructions:
                content = self._artifact_json(input_text)
            else:
                if clean_fixture_profile == "generic":
                    content = (
                        "我依据本轮允许的共同上下文回应前序观点，并区分事实、推断与待验证信息。"
                        "当前证据只支持形成可撤回的候选方案，不能把模型信心写成统计概率。\n\n"
                        "下一步应核对关键前提、主要反证与失效条件；系统不执行外部动作，"
                        "最终决定属于用户。"
                    )
                else:
                    content = (
                        "我基于同一冻结快照回应前序观点，并区分已知事实、合理推断与待验证信息。"
                        "当前证据只支持形成候选研究判断，不能把模型信心写成统计胜率。\n\n"
                        "下一步应核对数据时间截面、主要反证与失效条件；任何方案都只限研究、回测或模拟观察，"
                        "最终决定属于用户，禁止真实下单。"
                    )
                if "turn_envelope_v1" in instructions:
                    content = json.dumps(
                        {
                            "version": "turn_envelope_v1",
                            "turn_contract": json.loads(
                                self._turn_contract_json(instructions, input_text)
                            ),
                            "visible_content": content,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                elif "turn_contract_v1" in instructions:
                    content += (
                        "\n<turn_contract>"
                        + self._turn_contract_json(instructions, input_text)
                        + "</turn_contract>"
                    )
            usage = (
                {"input_tokens": 120, "output_tokens": 80}
                if provider_id == "doubao"
                else {"prompt_tokens": 120, "completion_tokens": 80}
            )
            return provider_response_class(
                ok=True,
                content=content,
                provider=provider_id,
                model=selected_model,
                usage=usage,
            )

    return DryProvider()


class DryRunMarketService:
    def __init__(self) -> None:
        self.snapshot_calls = 0
        self.history_calls: list[str] = []

    def capture(self) -> dict[str, Any]:
        if self.snapshot_calls:
            raise MarketGateFailed("dry-run 行情快照被重复抓取。")
        self.snapshot_calls += 1
        return {
            "ok": True,
            "state": "ready",
            "source": "futu_opend",
            "snapshot_id": "dry-run-futu-snapshot",
            # 16:00 is a US/Eastern market timestamp. Capture completes one
            # second later in UTC so the fail-closed future-time gate accepts it.
            "captured_at": "2026-07-30T20:00:01Z",
            "rows": [
                {
                    "symbol": symbol,
                    "last": float(100 + index),
                    "quality": "ready",
                    "age_seconds": 1,
                    "quote_is_live": True,
                    "market_state": None,
                    "freshness_basis": "live_20m_window",
                    "market_time": "2026-07-30 16:00:00",
                }
                for index, symbol in enumerate(STORAGE_SYMBOLS)
            ],
            "missing_symbols": [],
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
            "evidence": {
                "version": "storage_market_evidence_v6",
                "state": "ready",
                "snapshot_id": "dry-run-futu-snapshot",
                "captured_at": "2026-07-30T20:00:01Z",
                "technical": {
                    "source": "futu_qfq_daily_history",
                    "rows": [
                        {
                            "symbol": symbol,
                            "as_of": "2026-07-29 00:00:00",
                            "quality": "ready",
                            "sample_count": 120,
                        }
                        for symbol in STORAGE_SYMBOLS
                    ],
                    "source_errors": [],
                },
                "official_filings": {
                    "source": "sec_edgar_submissions",
                    "rows": [
                        {
                            "symbol": symbol,
                            "filings": [{"form": "10-Q", "fixture": True}],
                        }
                        for symbol in STORAGE_SYMBOLS
                    ],
                    "source_errors": [],
                },
                "company_ir_releases": {
                    "source": "official_company_ir",
                    "rows": [
                        {
                            "symbol": symbol,
                            "releases": [{"title": "local fixture", "fixture": True}],
                        }
                        for symbol in STORAGE_SYMBOLS
                    ],
                    "source_errors": [],
                },
                "official_earnings_packs": {
                    "version": "official_earnings_pack_v1",
                    "state": "ready",
                    "source": "isolated_fixture_official_earnings",
                    "rows": [
                        {
                            "symbol": symbol,
                            "packs": [_dry_run_earnings_pack(symbol)],
                        }
                        for symbol in STORAGE_SYMBOLS
                    ],
                    "source_errors": [],
                    "execution_capability": "none",
                    "live_trading_allowed": False,
                },
            },
        }

    def snapshot(self) -> dict[str, Any]:
        raise MarketGateFailed("轮次试图重复抓取 dry-run 行情。")

    def history(self, symbol: str, *, limit: int = 260) -> dict[str, Any]:
        clean_symbol = str(symbol or "").strip().upper()
        if clean_symbol not in STORAGE_SYMBOLS:
            raise MarketGateFailed("dry-run history only supports storage symbols")
        self.history_calls.append(clean_symbol)
        start = date(2026, 1, 1)
        phase = STORAGE_SYMBOLS.index(clean_symbol) * 0.5
        close = 100.0
        rows: list[dict[str, Any]] = []
        for index in range(min(max(20, int(limit)), 140)):
            close *= 1 + (0.001 + 0.011 * math.sin(index / 5 + phase))
            session = start + timedelta(days=index)
            rows.append({
                "symbol": clean_symbol,
                "market_time": f"{session} 16:00:00",
                "time": datetime.combine(
                    session,
                    datetime.min.time().replace(hour=16),
                    tzinfo=ZoneInfo("America/New_York"),
                ).astimezone(timezone.utc).isoformat(),
                "open": round(close, 6),
                "high": round(close, 6),
                "low": round(close, 6),
                "close": round(close, 6),
                "volume": 0.0,
                "turnover": 0.0,
            })
        last_session = start + timedelta(days=len(rows) - 1)
        as_of = last_session + timedelta(days=1)
        return {
            "ok": True,
            "symbol": clean_symbol,
            "source": "futu_opend",
            "interval": "1d",
            "price_adjustment": "QFQ",
            "captured_at": f"{as_of.isoformat()}T20:00:00Z",
            "as_of_date": as_of.isoformat(),
            "last_completed_session": last_session.isoformat(),
            "actual_start": start.isoformat(),
            "actual_end": last_session.isoformat(),
            "rows": rows,
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }

    @staticmethod
    def prompt_context(snapshot: dict[str, Any]) -> str:
        return (
            "本地 dry-run 富途结构夹具；仅验证冻结与门禁，不代表实时行情。"
            f" snapshot_id={snapshot.get('snapshot_id')}"
        )

    @staticmethod
    def timeline_summary(snapshot: dict[str, Any]) -> str:
        return f"dry-run 统一快照 {snapshot.get('snapshot_id')}，四股 4/4 ready。"


class RealOneShotMarketService:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.snapshot_calls = 0

    def capture(self) -> dict[str, Any]:
        if self.snapshot_calls:
            raise MarketGateFailed("真实富途行情快照被重复抓取。")
        self.snapshot_calls += 1
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        with contextlib.redirect_stdout(stdout_buffer), contextlib.redirect_stderr(stderr_buffer):
            return self.delegate.snapshot(force=True)

    def snapshot(self) -> dict[str, Any]:
        raise MarketGateFailed("轮次试图重复抓取真实富途行情。")

    def prompt_context(self, snapshot: dict[str, Any]) -> str:
        return self.delegate.prompt_context(snapshot)

    def timeline_summary(self, snapshot: dict[str, Any]) -> str:
        return self.delegate.timeline_summary(snapshot)


def validate_market_snapshot(snapshot: dict[str, Any]) -> None:
    def has_nested_source_errors(value: Any) -> bool:
        if isinstance(value, dict):
            if "source_errors" in value:
                source_errors = value.get("source_errors")
                if not isinstance(source_errors, list) or source_errors:
                    return True
            return any(has_nested_source_errors(item) for item in value.values())
        if isinstance(value, list):
            return any(has_nested_source_errors(item) for item in value)
        return False

    def security_status_ready(row: dict[str, Any]) -> bool:
        if row.get("suspended") is True:
            return False
        status = str(row.get("security_status") or "").strip().upper()
        return not status or status == "NORMAL" or status.endswith(".NORMAL")

    rows = snapshot.get("rows") if isinstance(snapshot.get("rows"), list) else []
    ready = {
        str(row.get("symbol") or "")
        for row in rows
        if isinstance(row, dict)
        and str(row.get("quality") or "") == "ready"
        and isinstance(row.get("age_seconds"), int)
        and not isinstance(row.get("age_seconds"), bool)
        and (
            (
                row.get("quote_is_live") is True
                and row.get("freshness_basis") == "live_20m_window"
                and 0 <= int(row.get("age_seconds")) <= 20 * 60
            )
            or (
                row.get("quote_is_live") is False
                and row.get("freshness_basis") == "closed_session_latest_snapshot"
                and str(row.get("market_state") or "").strip().upper()
                in {"AFTER_HOURS_END", "CLOSED", "WAITING_OPEN"}
                and 20 * 60 < int(row.get("age_seconds")) <= 96 * 60 * 60
            )
        )
        and isinstance(row.get("last"), (int, float))
        and float(row.get("last") or 0) > 0
        and bool(str(row.get("market_time") or "").strip())
        and security_status_ready(row)
    }
    if not all((
        snapshot.get("source") == "futu_opend",
        snapshot.get("ok") is True,
        snapshot.get("state") == "ready",
        bool(str(snapshot.get("snapshot_id") or "").strip()),
        bool(str(snapshot.get("captured_at") or "").strip()),
        not (snapshot.get("missing_symbols") or []),
        not (snapshot.get("source_errors") or []),
        set(STORAGE_SYMBOLS) == ready,
        snapshot.get("execution_capability") == "none",
        snapshot.get("live_trading_allowed") is False,
    )):
        raise MarketGateFailed("Futu 四股只读快照未达到严格 4/4 ready。")

    evidence = snapshot.get("evidence")
    if (
        not isinstance(evidence, dict)
        or evidence.get("state") != "ready"
        or has_nested_source_errors(evidence)
    ):
        raise MarketGateFailed(
            "存储研究证据未达到严格 ready，或包含嵌套来源错误。"
        )


def _artifact_evidence(content: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for ref in content.get("summary_evidence") or []:
        if isinstance(ref, dict):
            evidence.append(ref)
    for section in (
        "requirements",
        "risks",
        "conclusions",
        "disagreements",
        "unknowns",
        "actions",
    ):
        for item in content.get(section) or []:
            if not isinstance(item, dict):
                continue
            for ref in item.get("evidence") or []:
                if isinstance(ref, dict):
                    evidence.append(ref)
    decision = content.get("decision") if isinstance(content.get("decision"), dict) else {}
    for ref in decision.get("evidence") or []:
        if isinstance(ref, dict):
            evidence.append(ref)
    for option in decision.get("options") or []:
        if not isinstance(option, dict):
            continue
        for ref in option.get("evidence") or []:
            if isinstance(ref, dict):
                evidence.append(ref)
    return evidence


def _fixture_reviewed_artifact_content(
    content: dict[str, Any],
    contract_bundle: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    reviewed = copy.deepcopy(content)
    review_note = (
        "Isolated fixture user checked this persisted source relation; "
        "this does not represent a real human review."
    )
    for ref in _artifact_evidence(reviewed):
        ref.pop("role", None)
        ref["evidence_role"] = (
            "context"
            if str(ref.get("type") or "") == "round_market_snapshot"
            else "support"
        )
        ref["verification_status"] = "source_checked"
        ref["review_note"] = review_note

    counter_message_id = ""
    for message in contract_bundle.get("messages") or []:
        if not isinstance(message, dict):
            continue
        member = (
            message.get("member_snapshot")
            if isinstance(message.get("member_snapshot"), dict)
            else {}
        )
        stance = str(member.get("stance") or "").strip().lower()
        capabilities = {
            str(item).strip().lower()
            for item in member.get("capabilities") or []
            if str(item).strip()
        }
        if stance == "bear" or capabilities.intersection(
            {"bear_case", "critical_review"}
        ):
            counter_message_id = str(message.get("id") or "")
            if counter_message_id:
                break
    if not counter_message_id:
        raise ArtifactGateFailed(
            "isolated fixture could not bind reviewed counter-evidence"
        )
    summary_evidence = reviewed.get("summary_evidence")
    if not isinstance(summary_evidence, list):
        summary_evidence = []
        reviewed["summary_evidence"] = summary_evidence
    summary_evidence.append({
        "type": "message",
        "id": counter_message_id,
        "evidence_role": "counter",
        "verification_status": "source_checked",
        "review_note": (
            "Isolated fixture user retained this qualified bear-case turn as "
            "counter-evidence; this is not a real human review."
        ),
    })

    for item in reviewed.get("risks") or []:
        if isinstance(item, dict):
            item["status"] = "accepted"
            item["blocking"] = False
            if not str(item.get("mitigation") or "").strip():
                item["mitigation"] = (
                    "Fixture-only acceptance for a read-only paper scenario."
                )
    for item in reviewed.get("disagreements") or []:
        if isinstance(item, dict):
            item["status"] = "accepted_risk"
            item["blocking"] = False
            item["resolution"] = (
                "Fixture-only user accepted the recorded disagreement for "
                "paper research; no live action is authorized."
            )
    return reviewed, counter_message_id


def _fixture_paper_plan(
    default_paper_portfolio_plan: Callable[[], dict[str, Any]],
    candidate_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = default_paper_portfolio_plan()
    if isinstance(candidate_seed, dict) and candidate_seed.get("ready") is True:
        target_symbol = str(candidate_seed.get("symbol") or "")
        target_side = str(candidate_seed.get("target_side") or "")
        for position in plan["positions"]:
            if position["symbol"] == target_symbol:
                position["side"] = target_side
                position["weight_pct"] = 25
                position["thesis"] = str(candidate_seed.get("thesis") or "")
                position["invalidation"] = str(
                    candidate_seed.get("invalidation") or ""
                )
            else:
                position["side"] = "FLAT"
                position["weight_pct"] = 0
                position["thesis"] = ""
                position["invalidation"] = ""
        return plan
    for position, side, weight in zip(
        plan["positions"],
        ("LONG", "LONG", "SHORT", "FLAT"),
        (25, 20, 10, 0),
    ):
        position["side"] = side
        position["weight_pct"] = weight
        if side != "FLAT":
            position["thesis"] = (
                "Fixture-only paper allocation for deterministic risk review."
            )
            position["invalidation"] = (
                "Return to user review when the frozen research premise fails."
            )
    return plan


def _safe_error(exc: Exception) -> dict[str, str]:
    if isinstance(exc, AcceptanceError):
        return {
            "code": exc.code,
            "message": str(exc)[:240],
        }
    return {
        "code": "UNCLASSIFIED_ACCEPTANCE_ERROR",
        "message": f"验收器遇到未分类错误：{type(exc).__name__}。",
    }


def _clone_room(
    store: Any,
    source: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    source_room = source["room"]
    created = store.create_room(
        f"{source_room.get('title')} · 隔离验收",
        str(source_room.get("objective") or ROUND_OBJECTIVE),
        domain=str(source_room.get("domain") or "market_research"),
        category=str(source_room.get("category") or "交易研究 / 美股"),
        template_id="us_storage_committee",
        workflow_policy=policy,
        capability_pack_ids=[
            "storage_research_readonly",
            "structured_turn_contract_v1",
        ],
    )
    room_id = str((created.get("room") or {}).get("id") or "")
    if not room_id:
        raise SourceRoomInvalid("无法在临时数据库创建验收房间。")
    for member in created.get("members") or []:
        store.delete_member(room_id, str(member.get("id") or ""))

    cloned: list[dict[str, Any]] = []
    cloned_by_source_id: dict[str, dict[str, Any]] = {}
    for source_member in source["members"]:
        member = store.add_member(room_id, {
            "name": source_member.get("name"),
            "identity": source_member.get("identity"),
            "instructions": source_member.get("instructions"),
            "responsibilities": source_member.get("responsibilities"),
            "boundaries": source_member.get("boundaries"),
            "stance": source_member.get("stance"),
            "workflow_stage": source_member.get("workflow_stage"),
            "capabilities": source_member.get("capabilities") or [],
            "provider": source_member.get("provider"),
            "model": source_member.get("model"),
            "enabled": True,
        })
        if not member:
            raise SourceRoomInvalid("无法在临时数据库复制正式成员身份。")
        cloned.append(member)
        cloned_by_source_id[str(source_member.get("id") or "")] = member
    cloned = store.reorder_members(
        room_id,
        [str(member["id"]) for member in cloned],
    )
    if len(cloned) != EXPECTED_MEMBER_COUNT:
        raise SourceRoomInvalid("临时房间未完整复制十二位成员。")

    source_moderator_id = str(source_room.get("moderator_member_id") or "").strip()
    cloned_moderator = cloned_by_source_id.get(source_moderator_id)
    if not cloned_moderator:
        raise SourceRoomInvalid("正式动态主持无法映射到临时房间成员。")
    room_snapshot = store.room_snapshot(room_id) or {}
    temporary_room = (
        room_snapshot.get("room")
        if isinstance(room_snapshot.get("room"), dict)
        else {}
    )
    updated_room = store.update_room(
        room_id,
        {
            "expected_settings_version": int(
                temporary_room.get("settings_version") or 1
            ),
            "moderator_member_id": str(cloned_moderator["id"]),
        },
    )
    if (
        not updated_room
        or str(updated_room.get("moderator_member_id") or "")
        != str(cloned_moderator["id"])
    ):
        raise SourceRoomInvalid("临时房间未保留正式动态主持配置。")
    return room_id, cloned, {
        "source_member_id": source_moderator_id,
        "cloned_member_id": str(cloned_moderator["id"]),
        "provider": str(cloned_moderator.get("provider") or "").lower(),
        "model": str(cloned_moderator.get("model") or ""),
        "mapped": True,
    }


def _collect_round(
    orchestrator: Any,
    *,
    room_id: str,
    member_ids: list[str],
    market_snapshot: dict[str, Any],
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    decisions: list[dict[str, str]] = []
    final: dict[str, Any] | None = None
    stream = orchestrator.run_round(
        room_id,
        ROUND_OBJECTIVE,
        member_ids,
        prefetched_market_snapshot=market_snapshot,
    )
    try:
        for event in stream:
            event_type = str(event.get("type") or "")
            if event_type == "message":
                message = event.get("message") or {}
                turn_contract = (
                    message.get("turn_contract")
                    if isinstance(message.get("turn_contract"), dict)
                    else {}
                )
                messages.append({
                    "id": str(message.get("id") or ""),
                    "sender_id": str(message.get("sender_id") or ""),
                    "provider": str(message.get("provider") or "").lower(),
                    "model": str(message.get("model") or ""),
                    "reply_to_message_id": str(message.get("reply_to_message_id") or ""),
                    "responds_to_ids": [
                        str(item.get("id") or "")
                        for item in turn_contract.get("responds_to") or []
                        if isinstance(item, dict) and str(item.get("id") or "")
                    ],
                    "turn_contract_version": message.get("turn_contract_version"),
                    "turn_contract_qualified": message.get("turn_contract_qualified") is True,
                    "hidden_block_leaked": "<turn_contract" in str(message.get("content") or "").lower(),
                })
            elif event_type == "director_decision":
                member = event.get("member") or {}
                decisions.append({
                    "action": str(event.get("action") or ""),
                    "source": str(event.get("source") or ""),
                    "stage": str(event.get("stage") or ""),
                    "member_id": str(member.get("id") or ""),
                })
            elif event_type in {"error", "speaker_failed"}:
                code = str(event.get("code") or event.get("error_code") or "ROUND_EVENT_FAILED")
                raise RoundGateFailed(f"轮次事件失败：{code}。")
            elif event_type == "round_completed":
                final = {
                    "round_id": str(event.get("round_id") or ""),
                    "status": str(event.get("status") or ""),
                    "completed": int(event.get("completed") or 0),
                    "failures": int(event.get("failures") or 0),
                    "skipped": int(event.get("skipped") or 0),
                }
    finally:
        stream.close()
    if not final:
        raise RoundGateFailed("轮次没有产生完成事件。")
    return {
        "final": final,
        "messages": messages,
        "decisions": decisions,
    }


def _run_with_temp_database(
    *,
    source: dict[str, Any],
    mode: str,
    ledger: CallLedger,
) -> dict[str, Any]:
    temp_db_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="ai-studio-e2e-12-") as temp_dir:
        temp_root = Path(temp_dir).resolve()
        temp_db_path = temp_root / "isolated_studio.sqlite3"
        if mode == "dry-run":
            os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        os.environ["AI_STUDIO_RUNTIME_DIR"] = str(temp_root)
        os.environ["AI_STUDIO_DATABASE_PATH"] = str(temp_db_path)
        if str(PROJECT_DIR) not in sys.path:
            sys.path.insert(0, str(PROJECT_DIR))

        from backend.artifact_service import ArtifactService
        from backend.candidate_simulation_contract import (
            CANDIDATE_SIMULATION_CONFIRMATION_VERSION,
            CANDIDATE_SIMULATION_RULE_ID,
        )
        from backend.convergence import ConvergenceService
        from backend.market.storage_service import STORAGE_MARKET
        from backend.orchestrator import DiscussionOrchestrator
        from backend.paper_portfolio import default_paper_portfolio_plan
        from backend.paper_portfolio_service import PaperPortfolioService
        from backend.provider_preflight import ProviderPreflightService
        from backend.providers.base import ProviderProbeResult, ProviderResponse
        from backend.providers.deepseek_provider import DeepSeekProvider
        from backend.providers.doubao_provider import DoubaoProvider
        from backend.providers.registry import ProviderRegistry
        from backend.storage_sample_acceptance import StorageSampleAcceptance
        from backend.store import StudioStore
        from backend.workflow_policy import policy_from_json, validate_workflow_policy

        store = StudioStore(temp_db_path)
        policy = strengthen_policy(
            source["room"]["workflow_policy"],
            policy_from_json=policy_from_json,
            validate_workflow_policy=validate_workflow_policy,
        )
        room_id, members, moderator_binding = _clone_room(store, source, policy)
        member_ids = [str(member["id"]) for member in members]

        if mode == "dry-run":
            raw_providers = {
                "deepseek": build_dry_provider(
                    "deepseek",
                    "deepseek-v4-pro",
                    provider_response_class=ProviderResponse,
                    provider_probe_result_class=ProviderProbeResult,
                ),
                "doubao": build_dry_provider(
                    "doubao",
                    "doubao-seed-2-0-lite-260215",
                    provider_response_class=ProviderResponse,
                    provider_probe_result_class=ProviderProbeResult,
                ),
            }
            market_service: Any = DryRunMarketService()
            external = False
        else:
            raw_providers = {
                "deepseek": DeepSeekProvider(),
                "doubao": DoubaoProvider(),
            }
            market_service = RealOneShotMarketService(STORAGE_MARKET)
            external = True
        wrapped = {
            provider_id: BudgetedProvider(provider, ledger, external=external)
            for provider_id, provider in raw_providers.items()
        }
        registry = build_registry(
            provider_registry_class=ProviderRegistry,
            providers=wrapped,
            ledger=ledger,
        )
        orchestrator = DiscussionOrchestrator(
            store,
            registry,
            market_service,
        )

        market_snapshot = market_service.capture()
        validate_market_snapshot(market_snapshot)
        market_gate, prefetched = orchestrator.preflight_market(
            room_id,
            prefetched_market_snapshot=market_snapshot,
        )
        if not market_gate.get("ready") or prefetched is not market_snapshot:
            raise MarketGateFailed("服务端行情门禁未接受单次冻结快照。")

        provider_gate = ProviderPreflightService(store, registry).check_room(
            room_id,
            member_ids=member_ids,
            skip_provider_ids={"openai"},
        )
        if (
            not provider_gate.get("ready")
            or int(provider_gate.get("provider_check_count") or 0) != 2
            or provider_gate.get("unavailable_members")
        ):
            raise ProviderGateFailed("DeepSeek/豆包会前检查未全部通过。")

        round_result = _collect_round(
            orchestrator,
            room_id=room_id,
            member_ids=member_ids,
            market_snapshot=market_snapshot,
        )
        final = round_result["final"]
        message_rows = round_result["messages"]
        decisions = round_result["decisions"]
        unique_success_ids = {
            str(message.get("sender_id") or "")
            for message in message_rows
            if str(message.get("sender_id") or "")
        }
        first_turn_provider_counts = Counter(
            message["provider"] for message in message_rows[:EXPECTED_MEMBER_COUNT]
        )
        qualified_turn_contract_count = sum(
            1 for message in message_rows
            if message.get("turn_contract_qualified") is True
        )
        unqualified_turn_contract_count = sum(
            1 for message in message_rows
            if message.get("turn_contract_version") == "turn_contract_v1"
            and message.get("turn_contract_qualified") is not True
        )
        hidden_block_leak_count = sum(
            1 for message in message_rows if message.get("hidden_block_leaked") is True
        )
        prior_ai_message_ids: set[str] = set()
        validated_response_edge_count = 0
        for message in message_rows:
            if prior_ai_message_ids:
                reply_target = str(message.get("reply_to_message_id") or "")
                contract_targets = set(message.get("responds_to_ids") or [])
                if reply_target in prior_ai_message_ids and reply_target in contract_targets:
                    validated_response_edge_count += 1
            message_id = str(message.get("id") or "")
            if message_id:
                prior_ai_message_ids.add(message_id)
        required_response_edge_count = max(0, len(message_rows) - 1)
        if not all((
            final["status"] == "COMPLETED",
            final["failures"] == 0,
            final["skipped"] == 0,
            final["completed"] in {12, 13},
            len(unique_success_ids) == EXPECTED_MEMBER_COUNT,
            first_turn_provider_counts == Counter({"deepseek": 9, "doubao": 3}),
            qualified_turn_contract_count == final["completed"],
            unqualified_turn_contract_count == 0,
            hidden_block_leak_count == 0,
            validated_response_edge_count == required_response_edge_count,
            any(
                decision["source"] in {"ai", "rules_first"}
                and decision["action"] == "speak"
                for decision in decisions
            ),
        )):
            raise RoundGateFailed("十二角色完整轮次没有满足成功与动态主持条件。")

        round_id = final["round_id"]
        checkpoint = store.get_round_checkpoint(room_id, round_id)
        director_attempts = store.list_director_attempts(
            room_id,
            round_id=round_id,
        )
        checkpoint_state = (checkpoint or {}).get("state") or {}
        frozen_market = checkpoint_state.get("frozen_market") or {}
        manifest = checkpoint_state.get("round_evidence_manifest") or {}
        manifest_market = manifest.get("market_snapshot") or {}
        if not all((
            checkpoint,
            len(checkpoint_state.get("member_ids") or []) == EXPECTED_MEMBER_COUNT,
            len(checkpoint_state.get("successful_member_ids") or []) == EXPECTED_MEMBER_COUNT,
            not (checkpoint_state.get("failed_member_ids") or []),
            checkpoint_state.get("market_snapshot") == market_snapshot,
            frozen_market.get("ready") is True,
            frozen_market.get("snapshot_id") == market_snapshot.get("snapshot_id"),
            manifest_market.get("snapshot_id") == market_snapshot.get("snapshot_id"),
            bool(str(manifest_market.get("snapshot_sha256") or "")),
            checkpoint_state.get("moderator_member_id")
            == moderator_binding["cloned_member_id"],
            bool(director_attempts),
            all(
                str(attempt.get("moderator_member_id") or "")
                == moderator_binding["cloned_member_id"]
                for attempt in director_attempts
            ),
        )):
            raise RoundGateFailed("轮次检查点、动态主持映射、冻结行情或证据清单不完整。")

        convergence_service = ConvergenceService(store)
        before_artifact = convergence_service.evaluate(
            room_id,
            round_id=round_id,
        )
        if not all((
            before_artifact.get("decision_status") == "DRAFT_REQUIRED",
            before_artifact.get("can_host_finish") is True,
            before_artifact.get("can_present_candidate_best") is False,
            before_artifact.get("can_autonomously_decide") is False,
            before_artifact.get("user_confirmation_required") is True,
            (before_artifact.get("discussion_gate") or {}).get("successful_member_count") == 12,
            (before_artifact.get("turn_contract_gate") or {}).get("ready") is True,
            (before_artifact.get("turn_contract_gate") or {}).get("qualified_message_count") == final["completed"],
            (before_artifact.get("data_gate") or {}).get("ready") is True,
        )):
            raise RoundGateFailed("产物生成前的收敛状态不符合用户确认边界。")

        artifact_synthesizers = [
            member
            for member in members
            if str(member.get("provider") or "").strip().lower() == "doubao"
            and str(member.get("stance") or "").strip().lower() == "paper_trader"
        ]
        if len(artifact_synthesizers) != 1:
            raise SourceRoomInvalid("隔离验收必须恰好找到一位豆包模拟交易员整理会议草稿。")
        artifact = ArtifactService(store, registry).generate_minutes(
            room_id,
            round_id,
            synthesizer_member_id=str(artifact_synthesizers[0]["id"]),
        )
        artifact_content = artifact.get("content") or {}
        evidence = _artifact_evidence(artifact_content)
        market_evidence = [
            ref
            for ref in evidence
            if str(ref.get("type") or "") == "round_market_snapshot"
        ]
        market_evidence_exact = bool(
            len(market_evidence) == 1
            and str(market_evidence[0].get("id") or "")
            == str(manifest_market.get("snapshot_id") or "")
            and str(market_evidence[0].get("source_revision") or "")
            == str(manifest_market.get("evidence_version") or "")
            and str(market_evidence[0].get("source_snapshot_sha256") or "")
            == str(manifest_market.get("snapshot_sha256") or "")
            and str(market_evidence[0].get("evidence_role") or "") == "context"
            and str(market_evidence[0].get("verification_status") or "") == "unreviewed"
            and market_evidence[0].get("execution_capability") == "none"
            and market_evidence[0].get("live_trading_allowed") is False
            and market_evidence[0].get("source_active") is True
            and str(market_evidence[0].get("version_status") or "") == "current"
        )
        decision = artifact_content.get("decision") if isinstance(artifact_content.get("decision"), dict) else {}
        decision_options = [
            item for item in decision.get("options") or []
            if isinstance(item, dict)
        ]
        decision_option_ids = {
            str(item.get("id") or "") for item in decision_options
            if str(item.get("id") or "")
        }
        if not all((
            artifact.get("status") == "DRAFT",
            int(artifact.get("version") or 0) == 1,
            artifact.get("round_id") == round_id,
            artifact.get("generation_source") != "template_fallback",
            bool(str(artifact.get("generation_source") or "").startswith("doubao:")),
            bool(str(artifact_content.get("summary") or "").strip()),
            decision.get("status") == "candidate",
            len(decision_options) >= 2,
            str(decision.get("preferred_option_id") or "") in decision_option_ids,
            bool(str(decision.get("rationale") or "").strip()),
            bool(evidence),
            market_evidence_exact,
            all(
                str(ref.get("verification_status") or "") == "unreviewed"
                for ref in evidence
            ),
            int(artifact.get("confirmed_at") or 0) == 0,
        )):
            raise ArtifactGateFailed("会议产物不是可审计的未确认模型草稿。")

        after_artifact = convergence_service.evaluate(
            room_id,
            round_id=round_id,
        )
        if not all((
            after_artifact.get("decision_status") == "EVIDENCE_REVIEW_REQUIRED",
            after_artifact.get("can_present_candidate_best") is False,
            after_artifact.get("can_autonomously_decide") is False,
            after_artifact.get("user_confirmation_required") is True,
            (after_artifact.get("evidence_gate") or {}).get("artifact_status") == "DRAFT",
            int((after_artifact.get("evidence_gate") or {}).get("unreviewed_evidence_count") or 0) > 0,
            (after_artifact.get("decision_gate") or {}).get("ready") is True,
        )):
            raise ArtifactGateFailed("产物生成后未保持等待用户证据复核状态。")

        observations = store.list_observations(room_id)
        portfolios = store.list_paper_portfolios(room_id)
        artifacts = store.list_artifacts(room_id)
        if (
            any(str(item.get("status") or "") == "CONFIRMED" for item in observations)
            or any(str(item.get("status") or "") == "CONFIRMED" for item in portfolios)
            or any(str(item.get("status") or "") == "CONFIRMED" for item in artifacts)
        ):
            raise ArtifactGateFailed("隔离验收产生了未经用户动作的确认状态。")

        final_artifact = artifact
        final_convergence = after_artifact
        fixture_actor = ""
        fixture_counter_message_id = ""
        fixture_user_decision: dict[str, Any] = {}
        fixture_portfolio: dict[str, Any] = {}
        storage_acceptance: dict[str, Any] = {}
        fixture_negative_checks = {
            "unreviewed_artifact_rejected": False,
            "stale_artifact_version_rejected": False,
            "stale_decision_version_rejected": False,
        }
        fixture_provider_calls_before = len(ledger.records)
        fixture_external_calls_before = ledger.summary()["external_network_calls"]
        fixture_market_calls_before = int(market_service.snapshot_calls) + len(
            getattr(market_service, "history_calls", [])
        )

        if mode == "dry-run":
            fixture_actor = "isolated_fixture_user"
            try:
                store.confirm_artifact(
                    room_id,
                    str(artifact.get("id") or ""),
                    expected_version=int(artifact.get("version") or 0),
                    confirmed_by=fixture_actor,
                )
            except ValueError:
                fixture_negative_checks["unreviewed_artifact_rejected"] = True
            else:
                raise ArtifactGateFailed(
                    "unreviewed artifact confirmation did not fail closed"
                )

            contract_bundle = store.round_turn_contract_bundle(room_id, round_id)
            reviewed_content, fixture_counter_message_id = (
                _fixture_reviewed_artifact_content(
                    artifact_content,
                    contract_bundle,
                )
            )
            reviewed_artifact = store.update_artifact(
                room_id,
                str(artifact.get("id") or ""),
                {
                    "expected_version": int(artifact.get("version") or 0),
                    "content": reviewed_content,
                },
            )
            # Workflow dispositions are evaluated claim fields.  Saving them
            # invalidates the earlier source review by design, so the fixture
            # performs a second explicit review of the exact saved revision.
            reviewed_content = copy.deepcopy(reviewed_artifact.get("content") or {})
            for ref in _artifact_evidence(reviewed_content):
                if str(ref.get("verification_status") or "") == "unreviewed":
                    ref["verification_status"] = "source_checked"
                    ref["review_note"] = (
                        "Fixture user rechecked the exact saved risk and "
                        "disagreement disposition; no live action is authorized."
                    )
            reviewed_artifact = store.update_artifact(
                room_id,
                str(artifact.get("id") or ""),
                {
                    "expected_version": int(reviewed_artifact.get("version") or 0),
                    "content": reviewed_content,
                },
            )
            review_state = (
                reviewed_artifact.get("evidence_review")
                if isinstance((reviewed_artifact or {}).get("evidence_review"), dict)
                else {}
            )
            if not all((
                reviewed_artifact,
                reviewed_artifact.get("status") == "DRAFT",
                int(reviewed_artifact.get("version") or 0) == 3,
                review_state.get("confirmation_ready") is True,
                int(review_state.get("unreviewed_relation_count") or 0) == 0,
                int(review_state.get("reviewed_relation_count") or 0)
                == int(review_state.get("relation_count") or 0),
            )):
                raise ArtifactGateFailed(
                    "fixture evidence review did not produce a confirmable exact revision"
                )

            try:
                store.confirm_artifact(
                    room_id,
                    str(reviewed_artifact.get("id") or ""),
                    expected_version=int(artifact.get("version") or 0),
                    confirmed_by=fixture_actor,
                )
            except ValueError:
                fixture_negative_checks["stale_artifact_version_rejected"] = True
            else:
                raise ArtifactGateFailed(
                    "stale artifact version confirmation did not fail closed"
                )

            final_artifact = store.confirm_artifact(
                room_id,
                str(reviewed_artifact.get("id") or ""),
                expected_version=int(reviewed_artifact.get("version") or 0),
                confirmed_by=fixture_actor,
            )
            if not all((
                final_artifact,
                final_artifact.get("status") == "CONFIRMED",
                int(final_artifact.get("version") or 0) == 4,
                final_artifact.get("confirmed_by") == fixture_actor,
                int(
                    ((final_artifact.get("evidence_review") or {}).get(
                        "unreviewed_relation_count"
                    ))
                    or 0
                ) == 0,
            )):
                raise ArtifactGateFailed(
                    "fixture user did not confirm the exact reviewed artifact revision"
                )

            artifact_decision = (
                (final_artifact.get("content") or {}).get("decision")
                if isinstance(final_artifact.get("content"), dict)
                and isinstance((final_artifact.get("content") or {}).get("decision"), dict)
                else {}
            )
            governance_snapshot = (
                final_artifact.get("governance_snapshot")
                if isinstance(final_artifact.get("governance_snapshot"), dict)
                else {}
            )
            candidate_lineage = (
                governance_snapshot.get("candidate_lineage")
                if isinstance(governance_snapshot.get("candidate_lineage"), dict)
                else {}
            )
            selected_option_id = str(
                artifact_decision.get("preferred_option_id") or ""
            )
            selected_lineage = next((
                candidate
                for candidate in candidate_lineage.get("candidates") or []
                if isinstance(candidate, dict)
                and str(candidate.get("id") or "") == selected_option_id
            ), {})
            fixture_decision_binding = {
                "selected_option_id": selected_option_id,
                "expected_candidate_revision": int(
                    selected_lineage.get("revision") or 0
                ),
                "expected_candidate_origin_message_id": str(
                    selected_lineage.get("origin_message_id") or ""
                ),
                "expected_candidate_latest_message_id": str(
                    selected_lineage.get("latest_message_id") or ""
                ),
                "expected_governance_attestation_sha256": str(
                    governance_snapshot.get("attestation_sha256") or ""
                ),
            }
            if not all((
                governance_snapshot.get("integrity_ok") is True,
                governance_snapshot.get("attestation_integrity_ok") is True,
                fixture_decision_binding["selected_option_id"],
                fixture_decision_binding["expected_candidate_revision"] > 0,
                fixture_decision_binding["expected_candidate_origin_message_id"],
                fixture_decision_binding["expected_candidate_latest_message_id"],
                len(
                    fixture_decision_binding[
                        "expected_governance_attestation_sha256"
                    ]
                ) == 64,
            )):
                raise ArtifactGateFailed(
                    "fixture decision is missing an exact governed candidate binding"
                )

            try:
                store.create_artifact_user_decision(
                    room_id,
                    str(final_artifact.get("id") or ""),
                    expected_version=int(reviewed_artifact.get("version") or 0),
                    action="support",
                    rationale=(
                        "Fixture-only support; this is not a real human decision."
                    ),
                    created_by=fixture_actor,
                    **fixture_decision_binding,
                )
            except ValueError:
                fixture_negative_checks["stale_decision_version_rejected"] = True
            else:
                raise ArtifactGateFailed(
                    "stale artifact decision binding did not fail closed"
                )

            fixture_user_decision = store.create_artifact_user_decision(
                room_id,
                str(final_artifact.get("id") or ""),
                expected_version=int(final_artifact.get("version") or 0),
                action="support",
                rationale=(
                    "Fixture-only support of the exact reviewed research artifact; "
                    "this is not a real human decision and authorizes no execution."
                ),
                created_by=fixture_actor,
                **fixture_decision_binding,
            ) or {}
            if not all((
                fixture_user_decision.get("action") == "support",
                fixture_user_decision.get("decision_version")
                == "artifact_user_decision_v2",
                fixture_user_decision.get("ai_preferred_option_id")
                == selected_option_id,
                fixture_user_decision.get("selected_option_id")
                == selected_option_id,
                fixture_user_decision.get("selected_is_ai_preferred") is True,
                fixture_user_decision.get("candidate_binding_integrity_ok") is True,
                fixture_user_decision.get("decision_record_integrity_ok") is True,
                fixture_user_decision.get("created_by") == fixture_actor,
                int(fixture_user_decision.get("artifact_version") or 0)
                == int(final_artifact.get("version") or 0),
                fixture_user_decision.get("is_current") is True,
            )):
                raise ArtifactGateFailed(
                    "fixture user decision is not bound to the exact confirmed artifact"
                )

            portfolio_service = PaperPortfolioService(store, market_service)
            candidate_context = store.candidate_simulation_context(
                room_id,
                user_decision_id=str(fixture_user_decision.get("id") or ""),
            )
            candidate_seed = candidate_context.get("seed") or {}
            if candidate_seed.get("ready") is not True:
                raise ArtifactGateFailed(
                    "fixture candidate cannot be mapped to an exact paper contract"
                )
            paper_plan = _fixture_paper_plan(
                default_paper_portfolio_plan,
                candidate_seed,
            )
            paper_plan["user_decision_id"] = str(
                fixture_user_decision.get("id") or ""
            )
            paper_plan["derivation_note"] = (
                "Fixture-only paper implementation of the supported option; "
                "no order or account authority exists."
            )
            paper_plan["candidate_simulation_confirmation"] = {
                "version": CANDIDATE_SIMULATION_CONFIRMATION_VERSION,
                "expected_source_sha256": str(
                    candidate_seed.get("source_sha256") or ""
                ),
                "expected_candidate_revision": int(
                    candidate_seed.get("candidate_revision") or 0
                ),
                "expected_candidate_snapshot_sha256": str(
                    candidate_seed.get("candidate_snapshot_sha256") or ""
                ),
                "expected_target_weight_pct": 25,
                "strategy_rule_id": CANDIDATE_SIMULATION_RULE_ID,
                "user_confirmed": True,
            }
            draft_portfolio = portfolio_service.create(
                room_id,
                paper_plan,
                created_by=fixture_actor,
            )
            draft_evaluation = (
                draft_portfolio.get("evaluation")
                if isinstance(draft_portfolio.get("evaluation"), dict)
                else {}
            )
            if not all((
                draft_portfolio.get("status") == "DRAFT",
                (draft_evaluation.get("risk_gate") or {}).get("ready") is True,
                draft_evaluation.get("execution_capability") == "none",
                draft_evaluation.get("live_trading_allowed") is False,
            )):
                raise ArtifactGateFailed(
                    "fixture paper portfolio did not pass the deterministic risk gate"
                )
            fixture_portfolio = portfolio_service.confirm(
                room_id,
                str(draft_portfolio.get("id") or ""),
                expected_version=int(draft_portfolio.get("version") or 0),
                confirmed_by=fixture_actor,
            )
            confirmed_evaluation = (
                fixture_portfolio.get("evaluation")
                if isinstance(fixture_portfolio.get("evaluation"), dict)
                else {}
            )
            if not all((
                fixture_portfolio.get("status") == "CONFIRMED",
                fixture_portfolio.get("confirmed_by") == fixture_actor,
                (confirmed_evaluation.get("risk_gate") or {}).get("ready") is True,
                confirmed_evaluation.get("execution_capability") == "none",
                confirmed_evaluation.get("live_trading_allowed") is False,
            )):
                raise ArtifactGateFailed(
                    "fixture paper portfolio was not safely confirmed"
                )

            final_convergence = convergence_service.evaluate(
                room_id,
                round_id=round_id,
            )
            if not all((
                final_convergence.get("decision_status") == "USER_SUPPORTED",
                final_convergence.get("research_ready") is True,
                final_convergence.get("can_autonomously_decide") is False,
                (final_convergence.get("evidence_gate") or {}).get("ready") is True,
                (final_convergence.get("user_decision_gate") or {}).get("action")
                == "support",
                (final_convergence.get("portfolio_gate") or {}).get("ready") is True,
                final_convergence.get("execution_capability") == "none",
                final_convergence.get("live_trading_allowed") is False,
            )):
                raise ArtifactGateFailed(
                    "real convergence service did not reach fixture research readiness"
                )

            storage_acceptance = StorageSampleAcceptance(store).evaluate(room_id)
            if not all((
                storage_acceptance.get("state") == "accepted",
                storage_acceptance.get("acceptance_ready") is True,
                storage_acceptance.get("meeting_reviewed") is True,
                storage_acceptance.get("research_sample_ready") is True,
                storage_acceptance.get("user_decision_action") == "support",
                (storage_acceptance.get("paper_portfolio_gate") or {}).get("ready")
                is True,
                storage_acceptance.get("provider_calls") == 0,
                storage_acceptance.get("market_calls") == 0,
                storage_acceptance.get("execution_capability") == "none",
                storage_acceptance.get("live_trading_allowed") is False,
                storage_acceptance.get("can_autonomously_decide") is False,
            )):
                failed_acceptance_checks = [
                    check_id
                    for check_id, check in (storage_acceptance.get("checks") or {}).items()
                    if isinstance(check, dict) and check.get("ready") is not True
                ]
                raise ArtifactGateFailed(
                    "storage sample acceptance remained blocked: "
                    + ",".join(failed_acceptance_checks[:12])
                )

            observations = store.list_observations(room_id)
            portfolios = store.list_paper_portfolios(room_id)
            artifacts = store.list_artifacts(room_id)

        fixture_provider_calls_delta = len(ledger.records) - fixture_provider_calls_before
        fixture_external_calls_delta = (
            ledger.summary()["external_network_calls"] - fixture_external_calls_before
        )
        fixture_market_calls_delta = (
            int(market_service.snapshot_calls)
            + len(getattr(market_service, "history_calls", []))
            - fixture_market_calls_before
        )
        if mode == "dry-run" and (
            fixture_provider_calls_delta != 0 or fixture_external_calls_delta != 0
        ):
            raise ProviderGateFailed(
                "fixture user review unexpectedly invoked a provider or external network"
            )

        result = {
            "isolation": {
                "temporary_database": True,
                "temporary_database_inside_source_runtime": (
                    PROJECT_DIR / "runtime"
                ) in temp_db_path.parents,
            },
            "market": {
                "mode": "dry_run_fixture" if mode == "dry-run" else "real_futu_opend",
                "snapshot_calls": int(market_service.snapshot_calls),
                "ready": bool(market_gate.get("ready")),
                "ready_symbol_count": len(STORAGE_SYMBOLS),
                "symbols": list(STORAGE_SYMBOLS),
                "snapshot_id_present": bool(str(market_snapshot.get("snapshot_id") or "")),
                "execution_capability": market_snapshot.get("execution_capability"),
                "live_trading_allowed": market_snapshot.get("live_trading_allowed"),
            },
            "provider_preflight": {
                "ready": bool(provider_gate.get("ready")),
                "unique_route_count": int(provider_gate.get("provider_check_count") or 0),
                "member_count": int(provider_gate.get("member_count") or 0),
            },
            "round": {
                "status": final["status"],
                "completed_turns": final["completed"],
                "unique_successful_members": len(unique_success_ids),
                "failures": final["failures"],
                "skipped": final["skipped"],
                "first_turn_provider_counts": dict(
                    sorted(first_turn_provider_counts.items())
                ),
                "turn_contract_version": checkpoint_state.get("turn_contract_version"),
                "qualified_turn_contract_count": qualified_turn_contract_count,
                "unqualified_turn_contract_count": unqualified_turn_contract_count,
                "hidden_block_leak_count": hidden_block_leak_count,
                "required_response_edge_count": required_response_edge_count,
                "validated_response_edge_count": validated_response_edge_count,
                "director_decisions": len(decisions),
                "ai_speak_decisions": sum(
                    1
                    for decision in decisions
                    if decision["source"] == "ai" and decision["action"] == "speak"
                ),
                "rules_first_speak_decisions": sum(
                    1
                    for decision in decisions
                    if decision["source"] == "rules_first"
                    and decision["action"] == "speak"
                ),
                "ai_finish_decisions": sum(
                    1
                    for decision in decisions
                    if decision["source"] == "ai" and decision["action"] == "finish"
                ),
            },
            "checkpoint": {
                "present": bool(checkpoint),
                "member_count": len(checkpoint_state.get("member_ids") or []),
                "successful_member_count": len(
                    checkpoint_state.get("successful_member_ids") or []
                ),
                "failed_member_count": len(
                    checkpoint_state.get("failed_member_ids") or []
                ),
                "frozen_market_ready": frozen_market.get("ready") is True,
                "snapshot_hash_present": bool(
                    str(manifest_market.get("snapshot_sha256") or "")
                ),
            },
            "moderator": {
                "source_explicit": bool(moderator_binding["source_member_id"]),
                "source_member_id": moderator_binding["source_member_id"],
                "mapped_to_cloned_member": bool(moderator_binding["mapped"]),
                "provider": moderator_binding["provider"],
                "model": moderator_binding["model"],
                "checkpoint_matches": (
                    checkpoint_state.get("moderator_member_id")
                    == moderator_binding["cloned_member_id"]
                ),
                "director_attempt_count": len(director_attempts),
                "director_attempts_match": bool(director_attempts) and all(
                    str(attempt.get("moderator_member_id") or "")
                    == moderator_binding["cloned_member_id"]
                    for attempt in director_attempts
                ),
            },
            "convergence": {
                "before_artifact": before_artifact.get("decision_status"),
                "after_artifact": after_artifact.get("decision_status"),
                "after_fixture_user": final_convergence.get("decision_status"),
                "research_ready": bool(final_convergence.get("research_ready")),
                "discussion_ready": bool(
                    (final_convergence.get("discussion_gate") or {}).get("ready")
                ),
                "data_ready": bool(
                    (final_convergence.get("data_gate") or {}).get("ready")
                ),
                "decision_slate_ready": bool(
                    (final_convergence.get("decision_gate") or {}).get("ready")
                ),
                "can_present_candidate_best": bool(
                    final_convergence.get("can_present_candidate_best")
                ),
                "can_autonomously_decide": bool(
                    final_convergence.get("can_autonomously_decide")
                ),
                "user_confirmation_required": bool(
                    final_convergence.get("user_confirmation_required")
                ),
            },
            "artifact": {
                "initial_status": artifact.get("status"),
                "initial_version": int(artifact.get("version") or 0),
                "status": final_artifact.get("status"),
                "version": int(final_artifact.get("version") or 0),
                "round_bound": artifact.get("round_id") == round_id,
                "model_generated": artifact.get("generation_source") != "template_fallback",
                "generation_mode": "external_provider" if external else "fixture_provider",
                "external_model_generated": bool(
                    external
                    and artifact.get("generation_source") != "template_fallback"
                ),
                "generation_provider": str(
                    artifact.get("generation_source") or ""
                ).split(":", 1)[0],
                "initial_evidence_count": len(evidence),
                "evidence_count": int(
                    ((final_artifact.get("evidence_review") or {}).get(
                        "relation_count"
                    ))
                    or len(evidence)
                ),
                "initial_unreviewed_evidence_count": sum(
                    1
                    for ref in evidence
                    if str(ref.get("verification_status") or "") == "unreviewed"
                ),
                "unreviewed_evidence_count": int(
                    ((final_artifact.get("evidence_review") or {}).get(
                        "unreviewed_relation_count"
                    ))
                    or 0
                ),
                "reviewed_evidence_count": int(
                    ((final_artifact.get("evidence_review") or {}).get(
                        "reviewed_relation_count"
                    ))
                    or 0
                ),
                "market_snapshot_evidence_count": len(market_evidence),
                "market_snapshot_evidence_exact": market_evidence_exact,
                "decision_option_count": len(decision_options),
                "preferred_option_recorded": (
                    str(decision.get("preferred_option_id") or "")
                    in decision_option_ids
                ),
                "decision_rationale_recorded": bool(
                    str(decision.get("rationale") or "").strip()
                ),
                "confirmed": final_artifact.get("status") == "CONFIRMED",
                "confirmed_by": str(final_artifact.get("confirmed_by") or ""),
            },
            "fixture_user_gate": {
                "applied": mode == "dry-run",
                "actor": fixture_actor,
                "simulated_user_action": mode == "dry-run",
                "represents_real_user": False,
                "human_confirmation_still_required_for_real_run": True,
                "counter_message_id": fixture_counter_message_id,
                "provider_calls_delta": fixture_provider_calls_delta,
                "external_provider_calls_delta": fixture_external_calls_delta,
                "market_fixture_calls_delta": fixture_market_calls_delta,
                "negative_checks": fixture_negative_checks,
            },
            "user_decision": {
                "present": bool(fixture_user_decision),
                "action": str(fixture_user_decision.get("action") or ""),
                "decision_version": str(
                    fixture_user_decision.get("decision_version") or ""
                ),
                "ai_preferred_option_id": str(
                    fixture_user_decision.get("ai_preferred_option_id") or ""
                ),
                "selected_option_id": str(
                    fixture_user_decision.get("selected_option_id") or ""
                ),
                "selected_is_ai_preferred": bool(
                    fixture_user_decision.get("selected_is_ai_preferred")
                ),
                "candidate_binding_integrity_ok": bool(
                    fixture_user_decision.get("candidate_binding_integrity_ok")
                ),
                "decision_record_integrity_ok": bool(
                    fixture_user_decision.get("decision_record_integrity_ok")
                ),
                "created_by": str(fixture_user_decision.get("created_by") or ""),
                "artifact_version": int(
                    fixture_user_decision.get("artifact_version") or 0
                ),
                "exact_artifact_version": bool(
                    fixture_user_decision
                    and int(fixture_user_decision.get("artifact_version") or 0)
                    == int(final_artifact.get("version") or 0)
                ),
                "execution_capability": "none",
                "live_trading_allowed": False,
            },
            "paper_portfolio": {
                "present": bool(fixture_portfolio),
                "status": str(fixture_portfolio.get("status") or ""),
                "version": int(fixture_portfolio.get("version") or 0),
                "confirmed_by": str(fixture_portfolio.get("confirmed_by") or ""),
                "candidate_simulation_binding_ready": bool(
                    (
                        fixture_portfolio.get("candidate_simulation_binding")
                        or {}
                    ).get("ready")
                ),
                "candidate_simulation_contract_version": str(
                    (
                        fixture_portfolio.get("candidate_simulation_contract")
                        or {}
                    ).get("version")
                    or ""
                ),
                "risk_gate_ready": bool(
                    ((fixture_portfolio.get("evaluation") or {}).get("risk_gate") or {}).get(
                        "ready"
                    )
                ),
                "execution_capability": (
                    (fixture_portfolio.get("evaluation") or {}).get(
                        "execution_capability"
                    )
                ),
                "live_trading_allowed": (
                    (fixture_portfolio.get("evaluation") or {}).get(
                        "live_trading_allowed"
                    )
                ),
            },
            "storage_sample_acceptance": {
                "evaluated": bool(storage_acceptance),
                "state": str(storage_acceptance.get("state") or ""),
                "acceptance_ready": bool(
                    storage_acceptance.get("acceptance_ready")
                ),
                "meeting_reviewed": bool(
                    storage_acceptance.get("meeting_reviewed")
                ),
                "research_sample_ready": bool(
                    storage_acceptance.get("research_sample_ready")
                ),
                "user_decision_action": str(
                    storage_acceptance.get("user_decision_action") or ""
                ),
                "paper_portfolio_gate_ready": bool(
                    (storage_acceptance.get("paper_portfolio_gate") or {}).get(
                        "ready"
                    )
                ),
                "statistical_validation_ready": bool(
                    storage_acceptance.get("statistical_validation_ready")
                ),
                "provider_calls": int(storage_acceptance.get("provider_calls") or 0),
                "market_calls": int(storage_acceptance.get("market_calls") or 0),
                "read_only": storage_acceptance.get("read_only") is True,
            },
            "user_confirmation": {
                "artifact_confirmed": any(
                    str(item.get("status") or "") == "CONFIRMED"
                    for item in artifacts
                ),
                "confirmed_observations": sum(
                    1
                    for item in observations
                    if str(item.get("status") or "") == "CONFIRMED"
                ),
                "confirmed_paper_portfolios": sum(
                    1
                    for item in portfolios
                    if str(item.get("status") or "") == "CONFIRMED"
                ),
            },
            "safety": {
                "execution_capability": final_convergence.get("execution_capability"),
                "live_trading_allowed": final_convergence.get("live_trading_allowed"),
                "openai_hard_forbidden": True,
                "provider_retries": False,
                "cross_provider_fallback": False,
            },
        }
    result["isolation"]["temporary_database_removed"] = bool(
        temp_db_path is not None and not temp_db_path.exists()
    )
    return result


def run_acceptance(
    *,
    source_db: Path,
    mode: str,
) -> dict[str, Any]:
    source_path = source_db.resolve()
    before = database_fingerprint(source_path)
    read_audit = {
        "query_only": False,
        "total_changes_before": -1,
        "total_changes_after": -1,
    }
    ledger = CallLedger(mode=mode)
    result: dict[str, Any] = {}
    error: dict[str, str] | None = None
    provider_counts: dict[str, int] = {}
    source_moderator_member_id = ""
    try:
        if int(
            (((before.get("files") or {}).get("wal") or {}).get("size") or 0)
        ) > 0:
            raise SourceDatabaseChanged(
                "正式数据库存在未归并的 WAL 内容，无法建立不可变只读身份快照。"
            )
        source, read_audit = read_source_room(source_path)
        after_read = database_fingerprint(source_path)
        if not source_write_state_unchanged(before, after_read):
            raise SourceDatabaseChanged(
                "正式数据库在只读身份快照期间发生变化，已在外部调用前停止。"
            )
        provider_counts = dict(sorted(Counter(
            str(member.get("provider") or "").strip().lower()
            for member in source.get("members") or []
        ).items()))
        source_moderator_member_id = str(
            (source.get("room") or {}).get("moderator_member_id") or ""
        ).strip()
        validate_source_room(source)
        result = _run_with_temp_database(
            source=source,
            mode=mode,
            ledger=ledger,
        )
    except Exception as exc:
        error = _safe_error(exc)

    after = database_fingerprint(source_path)
    unchanged = source_write_state_unchanged(before, after)
    read_connection_unchanged = (
        int(read_audit.get("total_changes_before") or 0)
        == int(read_audit.get("total_changes_after") or 0)
        == 0
    )
    if not read_connection_unchanged:
        unchanged = False
    if not unchanged:
        error = _safe_error(SourceDatabaseChanged(
            "正式数据库前后指纹不一致，验收结果作废。"
        ))

    report = {
        "schema_version": 1,
        "mode": mode,
        "ok": error is None,
        "source_database": {
            "query_only_asserted": bool(read_audit.get("query_only")),
            "read_connection_total_changes": int(
                read_audit.get("total_changes_after") or 0
            ),
            "unchanged": unchanged,
            "main_sha256_before": (
                (before.get("files") or {}).get("main") or {}
            ).get("sha256", ""),
            "main_sha256_after": (
                (after.get("files") or {}).get("main") or {}
            ).get("sha256", ""),
        },
        "source_room": {
            "id": SOURCE_ROOM_ID,
            "expected_member_count": EXPECTED_MEMBER_COUNT,
            "provider_counts": provider_counts,
            "openai_assignments": int(provider_counts.get("openai", 0)),
            "moderator_member_id": source_moderator_member_id,
            "moderator_explicit": bool(source_moderator_member_id),
        },
        "providers": ledger.summary(),
        **result,
    }
    if error is not None:
        report["error"] = error
    if report["providers"]["total_calls"] > MAX_PROVIDER_CALLS:
        report["ok"] = False
        report["error"] = _safe_error(ProviderCallBudgetExceeded(
            "Provider 调用超过 28 次硬上限。"
        ))
    if report["providers"]["openai_network_calls"] != 0:
        report["ok"] = False
        report["error"] = _safe_error(OpenAIForbidden(
            "检测到 OpenAI 网络调用。"
        ))
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="隔离的十二角色真实会议验收器；默认不会自动发起付费调用。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="使用本地假 Provider 与行情夹具，验证完整隔离流程。",
    )
    mode.add_argument(
        "--execute-real",
        action="store_true",
        help=(
            "使用真实 Futu、DeepSeek 与豆包；必须同时设置 "
            "AI_STUDIO_SKIP_LOCAL_ENV=0 并提供付费调用确认短语。"
        ),
    )
    parser.add_argument(
        "--acknowledge-paid-calls",
        default="",
        help=f"真实运行必须精确填写 {REAL_RUN_ACK}。",
    )
    parser.add_argument(
        "--source-db",
        type=Path,
        default=DEFAULT_SOURCE_DB,
        help="只读提取 room_storage 身份快照的正式 SQLite 路径。",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help=(
            "把不含提示、消息正文、密钥或上游错误体的最终 JSON 摘要"
            "独占写入一个尚不存在的文件；用于终端输出中断后的审计。"
        ),
    )
    return parser.parse_args(argv)


def _validated_report_file(path: Path | None) -> Path | None:
    if path is None:
        return None
    target = path.expanduser().resolve()
    if target.suffix.lower() != ".json":
        raise ValueError("report-file 必须使用 .json 扩展名。")
    if target.exists():
        raise ValueError("report-file 已存在；为避免覆盖审计记录，本次不会运行。")
    if not target.parent.is_dir():
        raise ValueError("report-file 的父目录不存在。")
    return target


def _emit_report(report: dict[str, Any], report_file: Path | None) -> None:
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    if report_file is not None:
        with report_file.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.write("\n")
    print(serialized)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report_file = _validated_report_file(args.report_file)
    except ValueError as exc:
        _emit_report(
            {
                "schema_version": 1,
                "mode": "real" if args.execute_real else "dry-run" if args.dry_run else "none",
                "ok": False,
                "error": {
                    "code": "REPORT_FILE_INVALID",
                    "message": str(exc),
                },
            },
            None,
        )
        return 2
    if not args.dry_run and not args.execute_real:
        report = {
            "schema_version": 1,
            "mode": "none",
            "ok": False,
            "error": {
                "code": "MODE_REQUIRED",
                "message": "请显式选择 --dry-run 或 --execute-real。",
            },
        }
        _emit_report(report, report_file)
        return 2
    if args.execute_real and args.acknowledge_paid_calls != REAL_RUN_ACK:
        report = {
            "schema_version": 1,
            "mode": "real",
            "ok": False,
            "error": {
                "code": "PAID_CALL_ACK_REQUIRED",
                "message": (
                    "真实运行未获得 28 次 Provider 硬上限的显式确认；"
                    "没有发起外部调用。"
                ),
            },
        }
        _emit_report(report, report_file)
        return 2
    if args.execute_real and os.environ.get(
        "AI_STUDIO_SKIP_LOCAL_ENV", ""
    ).strip().lower() not in {"0", "false", "no"}:
        report = {
            "schema_version": 1,
            "mode": "real",
            "ok": False,
            "error": {
                "code": "REAL_ENV_OPT_IN_REQUIRED",
                "message": (
                    "真实运行还必须显式设置 AI_STUDIO_SKIP_LOCAL_ENV=0；"
                    "没有发起外部调用。"
                ),
            },
        }
        _emit_report(report, report_file)
        return 2
    report = run_acceptance(
        source_db=args.source_db,
        mode="real" if args.execute_real else "dry-run",
    )
    _emit_report(report, report_file)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
