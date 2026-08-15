"""Persistent, fail-closed provider-call budgets.

Callers must reserve immediately before every provider request. A reservation
is permanent even when the request fails, is cancelled, produces invalid
output, or is recovered as abandoned. This module never accepts or stores
prompts, responses, request bodies, or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .decision_lineage import canonical_sha256
from .store import (
    ProviderCallBudgetExceeded,
    ProviderCallKindBudgetExceeded,
    StudioStore,
)


_PLAN_NOT_SUPPLIED = object()


@dataclass(frozen=True, slots=True)
class ProviderCallLedger:
    """Thin service bound to one persisted provider execution run."""

    store: StudioStore
    run_id: str

    @staticmethod
    def fingerprint_plan(plan: Any) -> str:
        """Hash an execution plan without persisting its potentially sensitive body."""

        try:
            return canonical_sha256({
                "version": "provider_execution_plan_v1",
                "plan": plan,
            })
        except (TypeError, ValueError) as exc:
            raise ValueError("provider execution plan must be JSON serializable") from exc

    @staticmethod
    def route_target_id(provider: str, model: str = "") -> str:
        """Return the non-secret stable target id used by preflight operations."""

        return canonical_sha256({
            "version": "provider_route_target_v1",
            "provider": str(provider or "").strip().lower(),
            "model": str(model or "").strip(),
        })

    @classmethod
    def create(
        cls,
        store: StudioStore,
        room_id: str,
        *,
        scope: str,
        client_request_id: str,
        plan: Any = _PLAN_NOT_SUPPLIED,
        plan_hash: str = "",
        max_calls: int,
        skip_provider_ids: Any = None,
        artifact_route: Any = None,
        member_routes: Any = None,
        kind_call_limits: Any = None,
        operation_binding_version: str = "",
    ) -> "ProviderCallLedger":
        """Create or idempotently resume a run identified by scope/request id."""

        supplied_plan = plan is not _PLAN_NOT_SUPPLIED
        supplied_hash = bool(str(plan_hash or "").strip())
        if supplied_plan == supplied_hash:
            raise ValueError("supply exactly one of plan or verified plan_hash")
        persisted_plan_hash = (
            str(plan_hash or "").strip().lower()
            if supplied_hash
            else cls.fingerprint_plan(plan)
        )
        run = store.create_provider_execution_run(
            room_id,
            scope=scope,
            client_request_id=client_request_id,
            plan_hash=persisted_plan_hash,
            max_calls=max_calls,
            skip_provider_ids=skip_provider_ids,
            artifact_route=artifact_route,
            member_routes=member_routes,
            kind_call_limits=kind_call_limits,
            operation_binding_version=operation_binding_version,
        )
        return cls(store=store, run_id=str(run["id"]))

    @classmethod
    def resume(cls, store: StudioStore, run_id: str) -> "ProviderCallLedger":
        """Resume an existing run after restart without changing its budget."""

        run = store.get_provider_execution_run(run_id)
        if not run:
            raise ValueError("provider execution run does not exist")
        return cls(store=store, run_id=str(run["id"]))

    @classmethod
    def resume_for_round(
        cls,
        store: StudioStore,
        room_id: str,
        round_id: str,
        *,
        scope: str = "",
    ) -> "ProviderCallLedger":
        """Resume the unique run bound to a room/round, optionally by scope."""

        run = store.get_provider_execution_run_for_round(
            room_id,
            round_id,
            scope=scope,
        )
        if not run:
            raise ValueError("provider execution round run does not exist")
        return cls(store=store, run_id=str(run["id"]))

    def snapshot(self) -> dict[str, Any]:
        run = self.store.get_provider_execution_run(self.run_id)
        if not run:
            raise ValueError("provider execution run does not exist")
        return run

    def bind_round(self, round_id: str) -> dict[str, Any]:
        return self.store.bind_provider_execution_round(self.run_id, round_id)

    def reserve(
        self,
        *,
        kind: str,
        provider: str,
        model: str = "",
        member_id: str = "",
        member_version: int = 0,
        target_type: str = "",
        target_id: str = "",
    ) -> dict[str, Any]:
        """Spend one slot before the caller begins an external provider request."""

        return self.store.reserve_provider_call(
            self.run_id,
            kind=kind,
            provider=provider,
            model=model,
            member_id=member_id,
            member_version=member_version,
            target_type=target_type,
            target_id=target_id,
        )

    def finish(
        self,
        attempt_id: str,
        attempt_token: str,
        *,
        status: str,
        error_code: str = "",
        elapsed_ms: int = 0,
        usage: Any = None,
    ) -> dict[str, Any]:
        return self.store.finish_provider_call(
            self.run_id,
            attempt_id,
            attempt_token,
            status=status,
            error_code=error_code,
            elapsed_ms=elapsed_ms,
            usage=usage,
        )

    def abandon_started(
        self,
        *,
        error_code: str = "provider_call_abandoned",
    ) -> int:
        return self.store.abandon_started_provider_calls(
            self.run_id,
            error_code=error_code,
        )

    def close(self, *, status: str = "COMPLETED") -> dict[str, Any]:
        return self.store.finish_provider_execution_run(
            self.run_id,
            status=status,
        )

    def attempts(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        return self.store.list_provider_call_attempts(self.run_id, limit=limit)


__all__ = [
    "ProviderCallBudgetExceeded",
    "ProviderCallKindBudgetExceeded",
    "ProviderCallLedger",
]
