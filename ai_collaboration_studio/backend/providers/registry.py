from __future__ import annotations

import re
import threading
import time
from typing import TYPE_CHECKING, Any

from ..config import DISABLED_PROVIDER_IDS, HARD_DISABLED_PROVIDER_IDS
from .base import ChatProvider, ProviderProbeResult
from .deepseek_provider import DeepSeekProvider
from .doubao_provider import DoubaoProvider
from .glm_provider import GLMProvider
from .openai_provider import OpenAIProvider
from .output import provider_output_capability_dict
from .probe import skipped_probe

if TYPE_CHECKING:
    from ..provider_call_ledger import ProviderCallLedger


class ProviderRegistry:
    def __init__(
        self,
        providers: dict[str, ChatProvider] | None = None,
        *,
        disabled_provider_ids: set[str] | frozenset[str] | None = None,
    ) -> None:
        uses_production_providers = providers is None
        self._providers: dict[str, ChatProvider] = (
            providers
            if providers is not None
            else {
                "openai": OpenAIProvider(),
                "deepseek": DeepSeekProvider(),
                "doubao": DoubaoProvider(),
                "glm": GLMProvider(),
            }
        )
        configured_disabled_ids: set[str] | frozenset[str]
        if uses_production_providers:
            configured_disabled_ids = (
                set(DISABLED_PROVIDER_IDS)
                | set(HARD_DISABLED_PROVIDER_IDS)
                | set(disabled_provider_ids or set())
            )
        else:
            configured_disabled_ids = disabled_provider_ids or set()
        self._disabled_provider_ids = frozenset(
            str(provider_id or "").strip().lower()
            for provider_id in configured_disabled_ids
            if str(provider_id or "").strip()
        )
        self._preflight_cache_lock = threading.Lock()
        self._preflight_cache: dict[
            tuple[str, str],
            tuple[float, dict[str, Any]],
        ] = {}

    @property
    def disabled_provider_ids(self) -> frozenset[str]:
        """Deployment policy that callers may extend but never subtract from."""

        return self._disabled_provider_ids

    def get(self, provider_id: str) -> ChatProvider | None:
        normalized_id = str(provider_id or "").strip().lower()
        if normalized_id in self._disabled_provider_ids:
            return None
        return self._providers.get(normalized_id)

    def status(self) -> list[dict[str, Any]]:
        statuses: list[dict[str, Any]] = []
        for provider_id, provider in self._providers.items():
            status = dict(provider.status())
            status["policy_disabled"] = provider_id in self._disabled_provider_ids
            status["output_capabilities"] = provider_output_capability_dict(provider)
            statuses.append(status)
        return statuses

    def resolved_model(self, provider_id: str, model: str = "") -> str:
        selected = str(model or "").strip()
        if selected:
            return selected
        provider = self._providers.get(str(provider_id or "").strip().lower())
        if not provider:
            return ""
        try:
            return str(provider.status().get("model") or "").strip()
        except Exception:
            return ""

    def preflight(
        self,
        assignments: list[dict[str, Any]],
        *,
        skip_provider_ids: set[str] | None = None,
        cache_ttl_seconds: float = 30.0,
        ledger: ProviderCallLedger | None = None,
    ) -> list[dict[str, Any]]:
        """Probe each unique (provider, model) pair once without provider fallback."""
        skipped = {
            str(provider_id or "").strip().lower()
            for provider_id in (skip_provider_ids or set())
            if str(provider_id or "").strip()
        }
        unique: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for assignment in assignments:
            provider_id = str(assignment.get("provider") or "").strip().lower()
            selected_model = self.resolved_model(
                provider_id,
                str(assignment.get("model") or ""),
            )
            key = (provider_id, selected_model)
            if key not in seen:
                seen.add(key)
                unique.append(key)

        checks: list[dict[str, Any]] = []
        for provider_id, selected_model in unique:
            provider = self._providers.get(provider_id)
            if not provider:
                checks.append(ProviderProbeResult(
                    provider=provider_id,
                    model=selected_model,
                    configured=False,
                    reachable=False,
                    model_access=False,
                    latency_ms=0,
                    error_code="provider_not_supported",
                    message="该成员尚未配置可用的模型服务适配器。",
                ).as_dict())
                continue
            try:
                status = provider.status()
            except Exception:
                status = {}
            configured = bool(status.get("configured"))
            display_name = str(status.get("name") or provider_id or "模型服务")[:80]
            if provider_id in self._disabled_provider_ids:
                checks.append(ProviderProbeResult(
                    provider=provider_id,
                    model=selected_model,
                    configured=configured,
                    reachable=False,
                    model_access=False,
                    latency_ms=0,
                    error_code="PROVIDER_POLICY_DISABLED",
                    message=f"{display_name} 已被服务端固定策略禁用，未发送网络请求。",
                ).as_dict())
                continue
            if provider_id in skipped:
                checks.append(skipped_probe(
                    provider_id=provider_id,
                    model=selected_model,
                    configured=configured,
                    display_name=display_name,
                ).as_dict())
                continue
            if not configured:
                checks.append(ProviderProbeResult(
                    provider=provider_id,
                    model=selected_model,
                    configured=False,
                    reachable=False,
                    model_access=False,
                    latency_ms=0,
                    error_code="not_configured",
                    message=f"{display_name} 尚未配置。",
                ).as_dict())
                continue
            cached: dict[str, Any] | None = None
            now = time.monotonic()
            if cache_ttl_seconds > 0:
                with self._preflight_cache_lock:
                    cached_entry = self._preflight_cache.get(
                        (provider_id, selected_model)
                    )
                    if cached_entry and cached_entry[0] > now:
                        cached = dict(cached_entry[1])
                    elif cached_entry:
                        self._preflight_cache.pop(
                            (provider_id, selected_model),
                            None,
                        )
            if cached is not None:
                cached["cached"] = True
                checks.append(cached)
                continue
            reservation: dict[str, Any] | None = None
            if ledger is not None:
                try:
                    reservation = ledger.reserve(
                        kind="preflight_probe",
                        provider=provider_id,
                        model=selected_model,
                        target_type="provider_route",
                        target_id=ledger.route_target_id(
                            provider_id,
                            selected_model,
                        ),
                    )
                except Exception as exc:
                    budget_exhausted = (
                        str(getattr(exc, "code", "") or "").strip().lower()
                        == "provider_call_budget_exhausted"
                    )
                    blocked = ProviderProbeResult(
                        provider=provider_id,
                        model=selected_model,
                        configured=configured,
                        reachable=False,
                        model_access=False,
                        latency_ms=0,
                        error_code=(
                            "PROVIDER_CALL_BUDGET_EXCEEDED"
                            if budget_exhausted
                            else "PROVIDER_CALL_LEDGER_FAILED"
                        ),
                        message=(
                            "Provider 调用预算已用尽，未发送探测请求。"
                            if budget_exhausted
                            else "Provider 调用账本不可用，未发送探测请求。"
                        ),
                    ).as_dict()
                    blocked["cached"] = False
                    checks.append(blocked)
                    continue
            probe_started = time.monotonic()
            try:
                result = provider.probe(model=selected_model)
            except Exception:
                result = ProviderProbeResult(
                    provider=provider_id,
                    model=selected_model,
                    configured=configured,
                    reachable=False,
                    model_access=False,
                    latency_ms=0,
                    error_code="probe_failed",
                    message=f"{display_name} 探测失败。",
                )
            try:
                safe_result = result.as_dict()
            except Exception:
                safe_result = ProviderProbeResult(
                    provider=provider_id,
                    model=selected_model,
                    configured=configured,
                    reachable=False,
                    model_access=False,
                    latency_ms=0,
                    error_code="invalid_response",
                    message=f"{display_name} 返回了无效的探测结果。",
                ).as_dict()
            safe_result["provider"] = provider_id
            safe_result["model"] = selected_model
            safe_result["cached"] = False
            if ledger is not None and reservation is not None:
                terminal_status = (
                    "RESPONDED" if bool(safe_result.get("ready")) else "FAILED"
                )
                raw_error_code = str(
                    safe_result.get("error_code") or ""
                ).strip().lower()
                ledger_error_code = re.sub(
                    r"[^a-z0-9._-]+",
                    "_",
                    raw_error_code,
                ).strip("._-")[:80]
                if terminal_status != "RESPONDED" and not ledger_error_code:
                    ledger_error_code = "provider_probe_failed"
                elapsed_ms = min(
                    604_800_000,
                    max(0, int((time.monotonic() - probe_started) * 1000)),
                )
                try:
                    ledger.finish(
                        str(reservation.get("id") or ""),
                        str(reservation.get("attempt_token") or ""),
                        status=terminal_status,
                        error_code=ledger_error_code,
                        elapsed_ms=elapsed_ms,
                    )
                except Exception:
                    safe_result = ProviderProbeResult(
                        provider=provider_id,
                        model=selected_model,
                        configured=configured,
                        reachable=False,
                        model_access=False,
                        latency_ms=0,
                        error_code="PROVIDER_CALL_LEDGER_FAILED",
                        message="Provider 调用账本未能完成记录，本次检查按失败处理。",
                    ).as_dict()
                    safe_result["cached"] = False
                    checks.append(safe_result)
                    continue
            if cache_ttl_seconds > 0:
                with self._preflight_cache_lock:
                    self._preflight_cache[(provider_id, selected_model)] = (
                        time.monotonic() + cache_ttl_seconds,
                        dict(safe_result),
                    )
            checks.append(safe_result)
        return checks


PROVIDERS = ProviderRegistry()
