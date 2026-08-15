from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .futu_readonly import STORAGE_SYMBOLS, validate_storage_quote_snapshot
from .manual_official_evidence import (
    MANUAL_SUBSTITUTION_STATE,
    apply_attested_earnings_overlay,
    effective_source_error_codes,
    trusted_manual_substitution_claimed,
    validate_manual_official_evidence,
)
from .storage_service import STORAGE_MARKET, StorageResearchMarketService


def _collect_source_errors(value: Any) -> list[dict[str, str]]:
    collected: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            raw_errors = item.get("source_errors")
            if isinstance(raw_errors, list):
                for raw_error in raw_errors:
                    if not isinstance(raw_error, dict):
                        continue
                    error = {
                        "source": str(raw_error.get("source") or "research_source")[:80],
                        "code": str(raw_error.get("code") or "SOURCE_ERROR")[:100],
                        "message": str(raw_error.get("message") or "数据源当前不可用")[:300],
                    }
                    key = (error["source"], error["code"], error["message"])
                    if key not in seen:
                        seen.add(key)
                        collected.append(error)
            for key, child in item.items():
                if key != "source_errors":
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return collected


def _source_error_codes(value: Any) -> list[str]:
    return sorted({error["code"] for error in _collect_source_errors(value)})


def _safe_boundary(value: dict[str, Any] | None) -> bool:
    payload = value or {}
    return (
        payload.get("execution_capability") == "none"
        and payload.get("live_trading_allowed") is False
    )


def _quote_source_error_message(code: str) -> str:
    """Return stable public copy without exposing adapter exception details."""
    messages = {
        "FUTU_SDK_UNAVAILABLE": "本机 Futu OpenAPI SDK 不可用。",
        "FUTU_OPEND_OFFLINE": "本机 Futu OpenD 未连接。",
        "FUTU_CONNECTION_ERROR": "Futu OpenD 只读行情连接失败。",
        "FUTU_READINESS_ERROR": "Futu OpenD 只读行情读取失败，请检查本机行情连接与服务状态。",
    }
    return messages.get(
        code,
        "Futu OpenD 只读行情源报告错误；底层异常详情已隐藏。",
    )


_QUOTE_SOURCE_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,100}$")


def _quote_source_error_code(value: Any) -> str:
    """Constrain the public quote error-code channel to stable machine codes."""
    code = str(value or "").strip()
    if _QUOTE_SOURCE_ERROR_CODE.fullmatch(code):
        return code
    return "FUTU_SOURCE_ERROR"


def _captured_at_is_valid(value: Any) -> bool:
    """Mirror the canonical ISO-8601 parse only for public error classification."""
    text = str(value or "").strip()
    if not text:
        return False
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return True


def _quote_validation_failures(
    quote_probe: dict[str, Any],
    validation: dict[str, Any],
    source_errors: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Explain the strict quote validator without creating a second admission gate."""
    failures: list[dict[str, str]] = []
    seen_codes: set[str] = set()

    def add(code: str, message: str) -> None:
        if code in seen_codes:
            return
        seen_codes.add(code)
        failures.append({
            "source": "futu_opend",
            "code": code,
            "message": message,
        })

    if not validation.get("safe_snapshot"):
        add(
            "FUTU_READ_ONLY_BOUNDARY_REQUIRED",
            "Futu 快照缺少明确的只读边界，不得用于讨论准入。",
        )

    # Preserve source codes for diagnosis, but never expose raw adapter messages.
    # Safety remains first when a payload violates both boundaries at once.
    for source_error in source_errors:
        code = _quote_source_error_code(source_error.get("code"))
        add(code, _quote_source_error_message(code))

    if quote_probe.get("source") != "futu_opend":
        add("FUTU_SOURCE_INVALID", "行情快照不是 Futu OpenD 标准来源。")
    if quote_probe.get("ok") is not True or quote_probe.get("state") != "ready":
        add("FUTU_SNAPSHOT_NOT_READY", "Futu OpenD 快照未声明为 ready。")
    if not str(quote_probe.get("snapshot_id") or "").strip():
        add("FUTU_SNAPSHOT_ID_REQUIRED", "Futu 四股快照缺少 snapshot_id。")
    captured_at_text = str(quote_probe.get("captured_at") or "").strip()
    captured_at_invalid = False
    if not captured_at_text:
        add("FUTU_CAPTURED_AT_REQUIRED", "Futu 四股快照缺少 captured_at。")
    elif not _captured_at_is_valid(captured_at_text):
        captured_at_invalid = True
        add("FUTU_CAPTURED_AT_INVALID", "Futu 四股快照的 captured_at 格式无效。")

    duplicate_symbols = list(validation.get("duplicate_symbols") or [])
    if duplicate_symbols:
        add(
            "FUTU_DUPLICATE_SYMBOLS",
            "Futu 四股快照含有重复标的：" + "、".join(duplicate_symbols) + "。",
        )

    invalid_market_time_symbols = list(
        validation.get("invalid_market_time_symbols") or []
    )
    if invalid_market_time_symbols and captured_at_text and not captured_at_invalid:
        add(
            "FUTU_MARKET_TIME_INVALID",
            "行情时间无法与快照时间核对："
            + "、".join(invalid_market_time_symbols)
            + "。",
        )

    future_market_time_symbols = list(
        validation.get("future_market_time_symbols") or []
    )
    if future_market_time_symbols:
        add(
            "FUTU_MARKET_TIME_FUTURE",
            "行情时间晚于 captured_at："
            + "、".join(future_market_time_symbols)
            + "。",
        )

    invalid_freshness_symbols = list(
        validation.get("invalid_freshness_symbols") or []
    )
    if invalid_freshness_symbols:
        add(
            "FUTU_FRESHNESS_INVALID",
            "行情新鲜度合同不完整或不一致："
            + "、".join(invalid_freshness_symbols)
            + "。",
        )

    expected_symbols = set(STORAGE_SYMBOLS)
    market_symbols = set(validation.get("market_symbols") or [])
    ready_symbols = set(validation.get("ready_symbols") or [])
    rows = [row for row in quote_probe.get("rows") or [] if isinstance(row, dict)]
    if market_symbols != expected_symbols or len(rows) != len(STORAGE_SYMBOLS):
        add(
            "FUTU_FOUR_SYMBOLS_INCOMPLETE",
            "MU、SNDK、WDC、STX 尚未形成精确且唯一的四行共同截面。",
        )
    elif (
        ready_symbols != expected_symbols
        and not invalid_market_time_symbols
        and not future_market_time_symbols
        and not invalid_freshness_symbols
    ):
        add(
            "FUTU_QUOTE_VALUE_INVALID",
            "MU、SNDK、WDC、STX 存在缺失、非有限或非正的行情价格。",
        )

    if not failures:
        add(
            "FUTU_SNAPSHOT_CONTRACT_INVALID",
            "Futu 四股快照未通过严格只读准入合同。",
        )
    return failures


class StorageResearchReadinessService:
    """Build a read-only preparation/admission matrix for the storage room."""

    def __init__(self, market_service: StorageResearchMarketService = STORAGE_MARKET) -> None:
        self.market_service = market_service

    def inspect(
        self,
        *,
        force: bool = False,
        room_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status = self.market_service.status()
        symbol_set = set(STORAGE_SYMBOLS)
        sdk_error = status.get("sdk_error") or {}
        sdk_error_code = _quote_source_error_code(
            sdk_error.get("code") or "FUTU_SDK_UNAVAILABLE"
        )

        quote_probe: dict[str, Any] = {
            "ok": False,
            "state": "offline",
            "rows": [],
            "missing_symbols": list(STORAGE_SYMBOLS),
            "source_errors": [],
            "execution_capability": "none",
            "live_trading_allowed": False,
        }
        if not status.get("sdk_available"):
            quote_probe["source_errors"] = [{
                "source": "futu_opend",
                "code": sdk_error_code,
                "message": _quote_source_error_message(sdk_error_code),
            }]
        elif not status.get("opend_reachable"):
            quote_probe["source_errors"] = [{
                "source": "futu_opend",
                "code": "FUTU_OPEND_OFFLINE",
                "message": "本机 Futu OpenD 未连接",
            }]
        else:
            try:
                quote_probe = self.market_service.adapter.quote_batch(
                    STORAGE_SYMBOLS,
                    force=force,
                )
            except Exception:
                quote_probe["source_errors"] = [{
                    "source": "futu_opend",
                    "code": "FUTU_READINESS_ERROR",
                    "message": _quote_source_error_message("FUTU_READINESS_ERROR"),
                }]

        quote_errors = _collect_source_errors(quote_probe)
        quote_validation = validate_storage_quote_snapshot(quote_probe)
        ready_market_symbols = set(quote_validation.get("ready_symbols") or [])
        round_ready = quote_validation.get("ready") is True
        validation_failures = (
            []
            if round_ready
            else _quote_validation_failures(
                quote_probe,
                quote_validation,
                quote_errors,
            )
        )
        first_quote_error = validation_failures[0] if validation_failures else {}
        round_reason_code = "READY" if round_ready else str(
            first_quote_error.get("code") or "FUTU_FOUR_SYMBOLS_INCOMPLETE"
        )
        round_reason = (
            "Futu 四股共同截面已通过只读准入检查。"
            if round_ready
            else str(first_quote_error.get("message") or "MU、SNDK、WDC、STX 尚未形成完整共同截面。")
        )

        independent = apply_attested_earnings_overlay(
            self.market_service.independent_evidence(force=force),
            room_snapshot,
        )
        room_payload = (
            room_snapshot.get("room")
            if isinstance(room_snapshot, dict) and isinstance(room_snapshot.get("room"), dict)
            else {}
        )
        expected_room_id = str(room_payload.get("id") or "").strip()
        evidence = independent.get("evidence") or {}
        coverage = independent.get("coverage") or {}

        filings = evidence.get("official_filings") or {}
        ir_releases = evidence.get("company_ir_releases") or {}
        earnings_packs = evidence.get("official_earnings_packs") or {}
        industry = evidence.get("industry_supply_demand") or {}
        filings_symbols = set(coverage.get("official_filings") or [])
        ir_symbols = set(coverage.get("company_ir_releases") or [])
        earnings_symbols = set(coverage.get("official_earnings_packs") or [])
        manual_evidence = (
            evidence.get("manual_official_evidence")
            if isinstance(evidence.get("manual_official_evidence"), dict)
            else {}
        )
        manual_evidence_valid = bool(
            not manual_evidence
            or (
                expected_room_id
                and validate_manual_official_evidence(
                    manual_evidence,
                    expected_room_id=expected_room_id,
                )
            )
        )
        manual_state_claimed = trusted_manual_substitution_claimed(independent)
        manual_resolution_ready = bool(
            manual_evidence_valid
            and manual_evidence.get("source_issue_resolutions")
        )
        manual_state_contract_invalid = bool(
            manual_state_claimed and not manual_resolution_ready
        )
        earnings_error_codes = effective_source_error_codes(
            earnings_packs,
            manual_evidence,
            expected_room_id=expected_room_id,
        )
        if (
            (manual_evidence and not manual_evidence_valid)
            or manual_state_contract_invalid
        ):
            earnings_error_codes = sorted({
                *earnings_error_codes,
                "MANUAL_OFFICIAL_EVIDENCE_INVALID",
            })

        sec_ready = filings_symbols == symbol_set and not _source_error_codes(filings)
        ir_ready = ir_symbols == symbol_set and not _source_error_codes(ir_releases)
        earnings_ready = (
            earnings_symbols == symbol_set
            and earnings_packs.get("state") in {"ready", MANUAL_SUBSTITUTION_STATE}
            and manual_evidence_valid
            and not manual_state_contract_invalid
            and not earnings_error_codes
        )
        industry_ready = (
            coverage.get("industry_supply_demand") is True
            and industry.get("state") == "ready"
            and not _source_error_codes(industry)
        )
        convergence_ready = all((sec_ready, ir_ready, earnings_ready, industry_ready))
        manual_substitution_ready = bool(
            earnings_ready
            and manual_resolution_ready
        )
        preparation_usable = bool(
            filings_symbols or ir_symbols or earnings_symbols
            or industry.get("rows") or industry.get("derived")
        )

        host = str(status.get("host") or "127.0.0.1")
        port = int(status.get("port") or 11111)
        sec_status = status.get("sec_edgar") or {}
        sources = [
            {
                "id": "futu_sdk",
                "label": "Futu OpenAPI SDK",
                "group": "round_admission",
                "state": "ready" if status.get("sdk_available") else "blocked",
                "ready": bool(status.get("sdk_available")),
                "coverage_ready": None,
                "coverage_total": None,
                "error_codes": [
                    sdk_error_code
                ] if not status.get("sdk_available") else [],
                "action": "安装项目 requirements.txt 中的 futu-api，并重启本地服务。",
            },
            {
                "id": "futu_opend",
                "label": "Futu OpenD 四股行情",
                "group": "round_admission",
                "state": "ready" if round_ready else "blocked",
                "ready": round_ready,
                "coverage_ready": len(ready_market_symbols),
                "coverage_total": len(STORAGE_SYMBOLS),
                "error_codes": [error["code"] for error in validation_failures],
                "action": f"安装或启动并登录 Futu OpenD，保持只读行情端口 {host}:{port}。",
            },
            {
                "id": "sec_edgar",
                "label": "SEC EDGAR 官方申报",
                "group": "convergence",
                "state": "ready" if sec_ready else "blocked",
                "ready": sec_ready,
                "coverage_ready": len(filings_symbols),
                "coverage_total": len(STORAGE_SYMBOLS),
                "error_codes": _source_error_codes(filings),
                "action": (
                    "在本机 .env.local 配置 SEC_USER_AGENT=产品或组织名 联系邮箱，然后重启服务。"
                    if not sec_status.get("configured")
                    else "检查 SEC 官方端点连通性并刷新官方资料。"
                ),
            },
            {
                "id": "company_ir",
                "label": "公司 IR 官方事件",
                "group": "convergence",
                "state": "ready" if ir_ready else "partial" if ir_symbols else "blocked",
                "ready": ir_ready,
                "coverage_ready": len(ir_symbols),
                "coverage_total": len(STORAGE_SYMBOLS),
                "error_codes": _source_error_codes(ir_releases),
                "action": "刷新公司 IR；有结果时可先冻结为共享资料。",
            },
            {
                "id": "earnings_materials",
                "label": "官方业绩材料包",
                "group": "convergence",
                "state": (
                    MANUAL_SUBSTITUTION_STATE
                    if manual_substitution_ready
                    else "ready" if earnings_ready
                    else "partial" if earnings_symbols
                    else "blocked"
                ),
                "ready": earnings_ready,
                "coverage_ready": len(earnings_symbols),
                "coverage_total": len(STORAGE_SYMBOLS),
                "error_codes": earnings_error_codes,
                "action": "受限回退或正文不可访问只算部分就绪；在官方入口核验可访问性后再冻结资料。",
            },
            {
                "id": "industry_proxies",
                "label": "FRED 行业供需代理",
                "group": "convergence",
                "state": "ready" if industry_ready else "partial" if preparation_usable else "blocked",
                "ready": industry_ready,
                "coverage_ready": int(industry.get("series_count") or len(industry.get("rows") or [])),
                "coverage_total": int(industry.get("series_count") or len(industry.get("rows") or [])),
                "error_codes": _source_error_codes(industry),
                "action": "刷新 FRED 官方序列；行业代理不等于公司方向或胜率。",
            },
        ]

        blockers = [
            {
                "source_id": source["id"],
                "label": source["label"],
                "action": source["action"],
                "error_codes": source["error_codes"],
            }
            for source in sources
            if source["group"] in {"round_admission", "convergence"}
            and not source["ready"]
        ]
        safety_ready = all((
            _safe_boundary(quote_probe),
            _safe_boundary(independent),
            all(
                _safe_boundary(source_payload)
                for source_payload in evidence.values()
                if isinstance(source_payload, dict)
            ),
        ))

        return {
            "version": "storage_research_readiness_v1",
            "captured_at": independent.get("captured_at"),
            "symbols": list(STORAGE_SYMBOLS),
            "round_admission": {
                "ready": round_ready,
                "state": "ready" if round_ready else "blocked",
                "reason_code": round_reason_code,
                "reason": round_reason,
                "coverage_ready": len(ready_market_symbols),
                "coverage_total": len(STORAGE_SYMBOLS),
            },
            "convergence_readiness": {
                "ready": convergence_ready,
                "state": (
                    MANUAL_SUBSTITUTION_STATE
                    if convergence_ready and manual_substitution_ready
                    else "ready" if convergence_ready
                    else "partial" if preparation_usable
                    else "blocked"
                ),
                "preparation_usable": preparation_usable,
                "blockers": blockers,
            },
            "sources": sources,
            "independent_evidence": independent,
            "manual_official_evidence": manual_evidence,
            "safety": {
                "ready": safety_ready,
                "execution_capability": "none",
                "live_trading_allowed": False,
                "futu_context": "OpenQuoteContext",
            },
            "ready": bool(round_ready and convergence_ready and safety_ready),
        }


STORAGE_READINESS = StorageResearchReadinessService()


__all__ = ["STORAGE_READINESS", "StorageResearchReadinessService"]
