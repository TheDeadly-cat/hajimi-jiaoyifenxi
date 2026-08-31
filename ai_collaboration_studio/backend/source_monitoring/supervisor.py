"""Failure-isolated, at-least-once official source monitoring supervisor."""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable

from ..source_inbox_contracts import accept_source_import
from ..source_inbox_service import SourceInboxService
from .contracts import (
    MAX_NATIVE_INTEGER,
    AdapterPollResult,
    SourceMonitoringContractError,
    canonical_json,
)
from .packet_builder import (
    build_packet_from_poll_result,
    canonical_source_import_payload,
)
from .registry import SourceAdapterRegistry
from .scheduler import BackoffPolicy
from .settings import SourceMonitoringSettings
from .state_repository import (
    RUN_STATUS_DEGRADED,
    RUN_STATUS_DRY_RUN,
    RUN_STATUS_FAILED,
    RUN_STATUS_SUCCEEDED,
    SourceMonitoringStateRepository,
)


SOURCE_MONITORING_RUN_RESULT_VERSION = "source_monitoring_run_result_v1"
SOURCE_MONITORING_WORKER_ACTOR = "source_monitoring_worker"
RUN_STATUS_DRY_RUN_FAILED = "DRY_RUN_FAILED"

_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")


class SourceMonitoringSupervisorError(SourceMonitoringContractError):
    """Raised before a worker run when the monitor is not authorized to run."""


def _clean_error(exc: Exception) -> tuple[str, str]:
    candidate = getattr(exc, "code", "")
    code = (
        candidate
        if type(candidate) is str and _ERROR_CODE_RE.fullmatch(candidate)
        else "SOURCE_MONITORING_RUN_FAILED"
    )
    message = " ".join(str(exc).split())[:500]
    return code, message or exc.__class__.__name__[:500]


class SourceMonitoringSupervisor:
    """Poll, validate, import, then commit a checkpoint in that exact order."""

    def __init__(
        self,
        *,
        registry: SourceAdapterRegistry,
        repository: SourceMonitoringStateRepository,
        source_inbox: SourceInboxService,
        settings: SourceMonitoringSettings,
        backoff_policy: BackoffPolicy | None = None,
        clock_ms: Callable[[], Any] | None = None,
        after_import_hook: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        if type(registry) is not SourceAdapterRegistry:
            raise SourceMonitoringSupervisorError(
                "SOURCE_MONITORING_REGISTRY_INVALID",
                "registry must be SourceAdapterRegistry",
            )
        if type(repository) is not SourceMonitoringStateRepository:
            raise SourceMonitoringSupervisorError(
                "SOURCE_MONITORING_REPOSITORY_INVALID",
                "repository must be SourceMonitoringStateRepository",
            )
        if type(source_inbox) is not SourceInboxService:
            raise SourceMonitoringSupervisorError(
                "SOURCE_MONITORING_INBOX_INVALID",
                "source_inbox must be SourceInboxService",
            )
        if type(settings) is not SourceMonitoringSettings:
            raise SourceMonitoringSupervisorError(
                "SOURCE_MONITORING_SETTINGS_INVALID",
                "settings must be SourceMonitoringSettings",
            )
        if settings.official_only is not registry.official_only:
            raise SourceMonitoringSupervisorError(
                "SOURCE_MONITORING_SOURCE_MODE_MISMATCH",
                "settings and registry must use the same closed source mode",
            )
        if registry.official_only is not True:
            if settings.allow_readonly_market is not True:
                raise SourceMonitoringSupervisorError(
                    "SOURCE_MONITORING_READONLY_MARKET_NOT_ALLOWED",
                    "read-only market adapters require explicit opt-in",
                )
            if not any(
                registry.metadata_for(adapter_key).official_source is False
                for adapter_key in registry.adapter_keys
            ):
                raise SourceMonitoringSupervisorError(
                    "SOURCE_MONITORING_READONLY_MARKET_ADAPTER_REQUIRED",
                    "read-only market mode requires a non-official market adapter",
                )
        if after_import_hook is not None and not callable(after_import_hook):
            raise SourceMonitoringSupervisorError(
                "SOURCE_MONITORING_IMPORT_HOOK_INVALID",
                "after_import_hook must be callable",
            )
        for adapter_key in registry.adapter_keys:
            metadata = registry.metadata_for(adapter_key)
            if settings.max_items_per_run < metadata.max_candidates_per_poll:
                raise SourceMonitoringSupervisorError(
                    "SOURCE_MONITORING_ITEM_CAPACITY_TOO_LOW",
                    (
                        f"max_items_per_run={settings.max_items_per_run} is lower "
                        f"than adapter {adapter_key} candidate bound "
                        f"{metadata.max_candidates_per_poll}"
                    ),
                )
        self.registry = registry
        self.repository = repository
        self.source_inbox = source_inbox
        self.settings = settings
        self.backoff_policy = backoff_policy or BackoffPolicy()
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._after_import_hook = after_import_hook
        self._initialization_lock = threading.Lock()
        self._recovery_completed = False

    def _now_ms(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
            raise SourceMonitoringSupervisorError(
                "SOURCE_MONITORING_CLOCK_INVALID",
                "supervisor clock must return a non-negative native integer",
            )
        return value

    def initialize(self) -> int:
        """Recover abandoned RUNNING rows exactly once for this worker instance."""

        with self._initialization_lock:
            if self._recovery_completed:
                return 0
            recovered = self.repository.recover_incomplete_runs(
                error_code="WORKER_RESTARTED",
                next_due_at_ms=self._now_ms(),
            )
            self._recovery_completed = True
            return recovered

    def _base_result(
        self,
        *,
        adapter_key: str,
        run_id: str,
        status: str,
        market_calls_performed: int | None = 0,
        market_calls_possible_max: int = 0,
    ) -> dict[str, Any]:
        return {
            "version": SOURCE_MONITORING_RUN_RESULT_VERSION,
            "adapter_key": adapter_key,
            "run_id": run_id,
            "status": status,
            "safety": {
                "execution_capability": "none",
                "live_trading_allowed": False,
                "provider_calls_performed": 0,
                "market_calls_performed": market_calls_performed,
                "market_calls_accounting": (
                    "exact" if type(market_calls_performed) is int else "unknown"
                ),
                "market_calls_possible_max": market_calls_possible_max,
                "formal_rounds_created": 0,
            },
        }

    def run_once(self, adapter_key: Any) -> dict[str, Any]:
        if self.settings.enabled is not True:
            raise SourceMonitoringSupervisorError(
                "SOURCE_MONITORING_DISABLED",
                "source monitoring is globally disabled",
            )
        self.initialize()
        adapter = self.registry.require(adapter_key)
        metadata = self.registry.metadata_for(adapter.adapter_key)
        state = self.repository.get_or_create_state(
            metadata.adapter_key,
            config_version=metadata.config_version,
        )
        if state["enabled"] is not True:
            raise SourceMonitoringSupervisorError(
                "SOURCE_MONITORING_ADAPTER_DISABLED",
                f"adapter {metadata.adapter_key} is disabled",
            )
        started = self.repository.start_run(
            metadata.adapter_key,
            config_version=metadata.config_version,
            dry_run=self.settings.dry_run,
        )
        run_id = started["run"]["run_id"]
        poll_result: AdapterPollResult | None = None
        try:
            observed_at_ms = self._now_ms()
            candidate = adapter.poll(
                started["run"]["started_checkpoint"],
                observed_at_ms=observed_at_ms,
                etag=started["state"]["etag"],
                last_modified=started["state"]["last_modified"],
                max_items=self.settings.max_items_per_run,
            )
            if type(candidate) is not AdapterPollResult:
                raise SourceMonitoringSupervisorError(
                    "SOURCE_MONITORING_POLL_RESULT_INVALID",
                    "adapter.poll must return an exact AdapterPollResult",
                )
            poll_result = candidate
            if candidate.adapter_key != metadata.adapter_key:
                raise SourceMonitoringSupervisorError(
                    "SOURCE_MONITORING_ADAPTER_RESULT_MISMATCH",
                    "poll result adapter_key does not match the active adapter",
                )
            if candidate.market_calls_performed > metadata.max_market_calls_per_poll:
                raise SourceMonitoringSupervisorError(
                    "SOURCE_MONITORING_MARKET_CALL_BOUND_EXCEEDED",
                    (
                        f"adapter {metadata.adapter_key} reported "
                        f"{candidate.market_calls_performed} market calls above its "
                        f"sealed bound of {metadata.max_market_calls_per_poll}"
                    ),
                )
            if canonical_json(candidate.started_checkpoint) != canonical_json(
                started["run"]["started_checkpoint"]
            ):
                raise SourceMonitoringSupervisorError(
                    "SOURCE_MONITORING_CHECKPOINT_START_MISMATCH",
                    "poll result is not bound to the persisted starting checkpoint",
                )
            packet = build_packet_from_poll_result(
                candidate,
                external_run_id=run_id,
                max_items=self.settings.max_items_per_run,
                source_channel=metadata.source_channel,
            )
            validation_time_ms = max(self._now_ms(), candidate.captured_at_ms)
            payload = canonical_source_import_payload(
                packet,
                received_at_ms=validation_time_ms,
            )

            if self.settings.dry_run:
                accept_source_import(payload, received_at_ms=validation_time_ms)
                projected_due = self.backoff_policy.success_due_at_ms(
                    self._now_ms(),
                    metadata.poll_interval_ms,
                )
                completed = self.repository.complete_run(
                    run_id,
                    next_checkpoint=candidate.next_checkpoint,
                    status=RUN_STATUS_DRY_RUN,
                    observed_count=candidate.observed_count,
                    accepted_count=0,
                    duplicate_count=candidate.duplicate_count,
                    rejected_count=candidate.rejected_count,
                    next_due_at_ms=projected_due,
                    source_errors=list(candidate.source_errors),
                    source_channel=metadata.source_channel,
                    etag=candidate.etag,
                    last_modified=candidate.last_modified,
                )
                result = self._base_result(
                    adapter_key=metadata.adapter_key,
                    run_id=run_id,
                    status=RUN_STATUS_DRY_RUN,
                    market_calls_performed=candidate.market_calls_performed,
                    market_calls_possible_max=metadata.max_market_calls_per_poll,
                )
                result.update({
                    "candidate_count": len(candidate.observed_items),
                    "projected_next_due_at_ms": projected_due,
                    "run": completed["run"],
                    "state": completed["state"],
                    "state_recorded": True,
                })
                return result

            import_result: dict[str, Any] | None = None
            accepted_count = 0
            inbox_duplicate_count = 0
            receipt_id = ""
            if packet["items"]:
                import_result = self.source_inbox.import_packet(
                    payload,
                    actor=SOURCE_MONITORING_WORKER_ACTOR,
                )
                accepted_count = import_result.get("created_item_count")
                inbox_duplicate_count = import_result.get("duplicate_item_count")
                receipt_id = import_result.get("import_id")
                if (
                    type(accepted_count) is not int
                    or type(inbox_duplicate_count) is not int
                    or type(receipt_id) is not str
                    or not receipt_id
                ):
                    raise SourceMonitoringSupervisorError(
                        "SOURCE_MONITORING_IMPORT_RESULT_INVALID",
                        "Source Inbox returned an invalid import result",
                    )
                if self._after_import_hook is not None:
                    self._after_import_hook(run_id, import_result)
            else:
                accept_source_import(
                    payload,
                    received_at_ms=validation_time_ms,
                )

            degraded = bool(candidate.source_errors) or candidate.rejected_count > 0
            terminal_status = RUN_STATUS_DEGRADED if degraded else RUN_STATUS_SUCCEEDED
            if degraded:
                next_due_at_ms = self.backoff_policy.failure_due_at_ms(
                    self._now_ms(),
                    started["state"]["consecutive_failures"] + 1,
                    retry_after_ms=candidate.retry_after_ms,
                )
                if candidate.source_errors:
                    error_code = candidate.source_errors[0].code
                    error_message = candidate.source_errors[0].message
                else:
                    error_code = "SOURCE_ITEMS_REJECTED"
                    error_message = (
                        f"Adapter rejected {candidate.rejected_count} observed source item(s)."
                    )
            else:
                next_due_at_ms = self.backoff_policy.success_due_at_ms(
                    self._now_ms(),
                    metadata.poll_interval_ms,
                )
                error_code = ""
                error_message = ""
            completed = self.repository.complete_run(
                run_id,
                next_checkpoint=candidate.next_checkpoint,
                status=terminal_status,
                observed_count=candidate.observed_count,
                accepted_count=accepted_count,
                duplicate_count=candidate.duplicate_count + inbox_duplicate_count,
                rejected_count=candidate.rejected_count,
                next_due_at_ms=next_due_at_ms,
                source_errors=list(candidate.source_errors),
                receipt_id=receipt_id,
                source_channel=metadata.source_channel,
                etag=candidate.etag,
                last_modified=candidate.last_modified,
                error_code=error_code,
                error_message=error_message,
            )
            result = self._base_result(
                adapter_key=metadata.adapter_key,
                run_id=run_id,
                status=terminal_status,
                market_calls_performed=candidate.market_calls_performed,
                market_calls_possible_max=metadata.max_market_calls_per_poll,
            )
            result.update({
                "run": completed["run"],
                "state": completed["state"],
                "state_recorded": True,
                "import": (
                    {
                        "import_id": import_result["import_id"],
                        "created_item_count": accepted_count,
                        "duplicate_item_count": inbox_duplicate_count,
                        "idempotent_replay": import_result["idempotent_replay"],
                    }
                    if import_result is not None
                    else None
                ),
            })
            return result
        except Exception as exc:
            error_code, error_message = _clean_error(exc)
            retry_after_ms = poll_result.retry_after_ms if poll_result is not None else 0
            try:
                failure_clock_ms = self._now_ms()
            except Exception:
                failure_clock_ms = started["run"]["started_at_ms"]
            try:
                next_due_at_ms = self.backoff_policy.failure_due_at_ms(
                    failure_clock_ms,
                    started["state"]["consecutive_failures"] + 1,
                    retry_after_ms=retry_after_ms,
                )
            except Exception:
                next_due_at_ms = min(
                    MAX_NATIVE_INTEGER,
                    failure_clock_ms + self.backoff_policy.maximum_delay_ms,
                )
            state_recorded = False
            failure: dict[str, Any] | None = None
            recording_error = ""
            try:
                if self.settings.dry_run:
                    failure = self.repository.complete_run(
                        run_id,
                        next_checkpoint=(
                            poll_result.next_checkpoint
                            if poll_result is not None
                            else started["run"]["started_checkpoint"]
                        ),
                        status=RUN_STATUS_DRY_RUN,
                        observed_count=(
                            poll_result.observed_count if poll_result is not None else 0
                        ),
                        accepted_count=0,
                        duplicate_count=(
                            poll_result.duplicate_count if poll_result is not None else 0
                        ),
                        rejected_count=(
                            poll_result.rejected_count if poll_result is not None else 0
                        ),
                        next_due_at_ms=next_due_at_ms,
                        source_errors=(
                            list(poll_result.source_errors)
                            if poll_result is not None
                            else []
                        ),
                        source_channel=metadata.source_channel,
                        error_code=error_code,
                        error_message=error_message,
                    )
                else:
                    failure = self.repository.fail_run(
                        run_id,
                        error_code=error_code,
                        error_message=error_message,
                        next_due_at_ms=next_due_at_ms,
                        observed_count=(
                            poll_result.observed_count if poll_result is not None else 0
                        ),
                        rejected_count=(
                            poll_result.observed_count if poll_result is not None else 0
                        ),
                    )
                state_recorded = True
            except Exception as record_exc:
                _record_code, recording_error = _clean_error(record_exc)
                failure = None
                self._recovery_completed = False
                try:
                    self.repository.recover_incomplete_runs(
                        error_code="WORKER_FAILURE_RECOVERY",
                        next_due_at_ms=next_due_at_ms,
                    )
                    recovered_run = self.repository.get_run(run_id)
                    recovered_state = self.repository.get_state(metadata.adapter_key)
                    if (
                        recovered_run is not None
                        and recovered_run["status"] != "RUNNING"
                        and recovered_state is not None
                    ):
                        failure = {"run": recovered_run, "state": recovered_state}
                        state_recorded = True
                except Exception:
                    failure = None
            result = self._base_result(
                adapter_key=metadata.adapter_key,
                run_id=run_id,
                status=(
                    RUN_STATUS_DRY_RUN_FAILED
                    if self.settings.dry_run
                    else RUN_STATUS_FAILED
                ),
                market_calls_performed=(
                    poll_result.market_calls_performed
                    if poll_result is not None
                    else 0
                    if metadata.max_market_calls_per_poll == 0
                    else None
                ),
                market_calls_possible_max=metadata.max_market_calls_per_poll,
            )
            result.update({
                "error_code": error_code,
                "error_message": error_message,
                "next_due_at_ms": next_due_at_ms,
                "state_recorded": state_recorded,
                "run": failure["run"] if failure is not None else None,
                "state": failure["state"] if failure is not None else None,
                "import": None,
                "recording_error": recording_error,
            })
            return result


__all__ = [
    "SOURCE_MONITORING_RUN_RESULT_VERSION",
    "SOURCE_MONITORING_WORKER_ACTOR",
    "RUN_STATUS_DRY_RUN_FAILED",
    "SourceMonitoringSupervisor",
    "SourceMonitoringSupervisorError",
]
