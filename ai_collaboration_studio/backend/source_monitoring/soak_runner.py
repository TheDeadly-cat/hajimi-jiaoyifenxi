"""Owner-scoped runtime orchestration for one fixed source-monitoring soak.

This module deliberately does not discover a database, acquire its instance
owner, migrate schema, install signal handlers, or expose a duration override.
The production entry point must perform those operations explicitly and keep
the database owner for the entire call to :meth:`SourceMonitoringSoakRunner.run`.

The v1 ledger proves process-local continuity and binds terminal runtime
observations to immutable SQLite row hashes.  It does *not* decide whether a
remote source was live, fresh, complete, or semantically correct.
"""

from __future__ import annotations

import copy
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .contracts import MAX_NATIVE_INTEGER, canonical_sha256
from .soak_db_inventory import (
    build_soak_db_inventory,
    read_soak_db_run_evidence,
    validate_soak_db_inventory_delta,
)
from .soak_evidence import (
    SOAK_EVENT_RUN_TERMINAL,
    SOAK_EVENT_RUNTIME_SAMPLE,
    SOAK_EVENT_SESSION_ENDED,
    SOAK_EVENT_SESSION_STARTED,
    SoakEvidenceWriter,
)
from .soak_plan import (
    SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS,
    SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS,
    SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS,
)


SOURCE_MONITORING_SOAK_RUNNER_VERSION = "source_monitoring_soak_runner_v1"
SOURCE_MONITORING_SOAK_OBSERVER_BIND_TIMEOUT_SECONDS = 30.0

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RUNTIME_ID_RE = re.compile(r"source_monitor_runtime_[0-9a-f]{32}\Z")
_CAMPAIGN_ID_RE = re.compile(r"source_soak_campaign_[0-9a-f]{32}\Z")
_SESSION_ID_RE = re.compile(r"source_soak_session_[0-9a-f]{32}\Z")
_RUN_ID_RE = re.compile(r"source_run_[0-9a-f]{32}\Z")
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")
_ADAPTER_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_CONFIG_VERSION_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_EXPECTED_ADAPTER_FIELDS = frozenset(
    {"adapter_key", "config_version", "state_version", "checkpoint_sha256"}
)
_TERMINAL_STATUSES = frozenset(
    {"SUCCEEDED", "DEGRADED", "FAILED", "DRY_RUN", "DRY_RUN_FAILED", "ABANDONED"}
)


class SourceMonitoringSoakRunnerError(RuntimeError):
    """A bounded failure while preparing or running one soak session."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _raise(code: str, message: str) -> None:
    raise SourceMonitoringSoakRunnerError(code, message)


def _sha256(value: Any, *, field: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _raise("SOURCE_MONITORING_SOAK_INPUT_INVALID", f"{field} must be SHA-256")
    return value


def _native_non_negative(value: Any, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
        _raise(
            "SOURCE_MONITORING_SOAK_CLOCK_INVALID",
            f"{field} must be a non-negative native signed 64-bit integer",
        )
    return value


def _positive_duration(value: Any, *, field: str) -> int:
    clean = _native_non_negative(value, field=field)
    if clean == 0:
        _raise("SOURCE_MONITORING_SOAK_INPUT_INVALID", f"{field} must be positive")
    return clean


def _validate_identity(value: Any, pattern: re.Pattern[str], *, field: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _raise("SOURCE_MONITORING_SOAK_INPUT_INVALID", f"{field} is invalid")
    return value


def _default_wall_time_ms() -> int:
    return int(time.time() * 1_000)


def _default_monotonic_ns() -> int:
    return time.monotonic_ns()


def _default_wait(stop_event: threading.Event, timeout_seconds: float) -> bool:
    return stop_event.wait(timeout_seconds)


def _normalize_expected_enabled_adapters(value: Any) -> tuple[dict[str, Any], ...]:
    if type(value) not in {list, tuple} or not 1 <= len(value) <= 50:
        _raise(
            "SOURCE_MONITORING_SOAK_ENABLED_ADAPTERS_INVALID",
            "expected enabled adapters must be a non-empty bounded sequence",
        )
    normalized: list[dict[str, Any]] = []
    for index, descriptor in enumerate(value):
        if type(descriptor) is not dict or set(descriptor) != _EXPECTED_ADAPTER_FIELDS:
            _raise(
                "SOURCE_MONITORING_SOAK_ENABLED_ADAPTERS_INVALID",
                "expected enabled adapter fields are not closed",
            )
        adapter_key = descriptor["adapter_key"]
        config_version = descriptor["config_version"]
        state_version = descriptor["state_version"]
        checkpoint_sha256 = descriptor["checkpoint_sha256"]
        if (
            type(adapter_key) is not str
            or _ADAPTER_KEY_RE.fullmatch(adapter_key) is None
            or type(config_version) is not str
            or _CONFIG_VERSION_RE.fullmatch(config_version) is None
            or type(state_version) is not int
            or not 1 <= state_version <= MAX_NATIVE_INTEGER
            or type(checkpoint_sha256) is not str
            or _SHA256_RE.fullmatch(checkpoint_sha256) is None
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_ENABLED_ADAPTERS_INVALID",
                f"expected enabled adapter {index} is invalid",
            )
        normalized.append(
            {
                "adapter_key": adapter_key,
                "config_version": config_version,
                "state_version": state_version,
                "checkpoint_sha256": checkpoint_sha256,
            }
        )
    keys = [descriptor["adapter_key"] for descriptor in normalized]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        _raise(
            "SOURCE_MONITORING_SOAK_ENABLED_ADAPTERS_INVALID",
            "expected enabled adapters must be uniquely sorted by adapter_key",
        )
    return tuple(normalized)


class SoakRuntimeObserver:
    """Bridge terminal runtime receipts into the session's fsynced ledger.

    The runtime may reach its first cycle before the runner learns the generated
    runtime ID.  Such a callback blocks on ``activate``; it can never race ahead
    of ``SESSION_STARTED``.  Every ledger timestamp is sampled while holding the
    same append lock, so two threads cannot create a false monotonic regression.
    """

    def __init__(
        self,
        database_path: str | Path,
        registry: Any | None = None,
        *,
        terminal_reader: Callable[[str | Path, str], Any] = read_soak_db_run_evidence,
        wall_time_ms: Callable[[], Any] = _default_wall_time_ms,
        monotonic_ns: Callable[[], Any] = _default_monotonic_ns,
        bind_timeout_seconds: float = SOURCE_MONITORING_SOAK_OBSERVER_BIND_TIMEOUT_SECONDS,
    ) -> None:
        if not callable(terminal_reader) or not callable(wall_time_ms) or not callable(
            monotonic_ns
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                "observer dependencies must be callable",
            )
        if (
            type(bind_timeout_seconds) not in {int, float}
            or isinstance(bind_timeout_seconds, bool)
            or not 0 < float(bind_timeout_seconds) <= 300
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                "observer bind timeout must be between zero and 300 seconds",
            )
        if registry is not None and not callable(getattr(registry, "metadata_for", None)):
            _raise(
                "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                "observer registry must expose metadata_for",
            )
        self.database_path = Path(database_path).expanduser().absolute()
        self.registry = registry
        self._terminal_reader = terminal_reader
        self._wall_time_ms = wall_time_ms
        self._monotonic_ns = monotonic_ns
        self._bind_timeout_seconds = float(bind_timeout_seconds)
        self._append_lock = threading.RLock()
        self._binding_event = threading.Event()
        self._writer: SoakEvidenceWriter | None = None
        self._runtime_id = ""
        self._start_monotonic_ns: int | None = None
        self._activated = False
        self._aborted = False
        self._expected_adapter_keys: frozenset[str] | None = None
        self._run_ids: list[str] = []
        self._declarations: list[dict[str, Any]] = []

    def bind_registry(self, registry: Any) -> None:
        with self._append_lock:
            if self._writer is not None or self._activated or self._aborted:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "registry must be bound before runtime start",
                )
            if not callable(getattr(registry, "metadata_for", None)):
                _raise(
                    "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                    "observer registry must expose metadata_for",
                )
            if self.registry is not None and self.registry is not registry:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "observer registry was already bound to another instance",
                )
            self.registry = registry

    @property
    def run_ids(self) -> list[str]:
        with self._append_lock:
            return list(self._run_ids)

    @property
    def declarations(self) -> list[dict[str, Any]]:
        with self._append_lock:
            return copy.deepcopy(self._declarations)

    @property
    def start_monotonic_ns(self) -> int:
        with self._append_lock:
            if self._start_monotonic_ns is None or not self._activated:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "observer activation origin is unavailable",
                )
            return self._start_monotonic_ns

    def prepare(
        self,
        writer: SoakEvidenceWriter,
        *,
        runtime_id: str,
        start_monotonic_ns: int,
    ) -> None:
        clean_runtime_id = _validate_identity(
            runtime_id,
            _RUNTIME_ID_RE,
            field="runtime_id",
        )
        clean_start = _native_non_negative(
            start_monotonic_ns,
            field="start_monotonic_ns",
        )
        with self._append_lock:
            if self._writer is not None or self._activated or self._aborted:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "observer can be prepared exactly once",
                )
            if type(writer) is not SoakEvidenceWriter or writer.runtime_id != clean_runtime_id:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "writer is not bound to the generated runtime ID",
                )
            self._writer = writer
            self._runtime_id = clean_runtime_id
            self._start_monotonic_ns = clean_start

    def activate(self) -> None:
        with self._append_lock:
            if self._writer is None or self._activated or self._aborted:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "observer cannot be activated in its current state",
                )
            if self._writer.record_count != 1:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "SESSION_STARTED must be durable before observer activation",
                )
            # The actual 24-hour window begins only after START is durable and
            # immediately before the worker gate is released.  Runtime
            # construction, synchronous initialization, and ledger fsync do not
            # count as live soak time.
            activation_origin = _native_non_negative(
                self._monotonic_ns(),
                field="monotonic clock",
            )
            if (
                self._start_monotonic_ns is not None
                and activation_origin < self._start_monotonic_ns
            ):
                _raise(
                    "SOURCE_MONITORING_SOAK_CLOCK_INVALID",
                    "monotonic clock moved backward before activation",
                )
            self._start_monotonic_ns = activation_origin
            self._activated = True
            self._binding_event.set()

    def abort(self) -> None:
        with self._append_lock:
            self._aborted = True
            self._binding_event.set()

    def await_activation(self) -> None:
        """Block a runtime worker before its first loop until START is durable."""

        if not self._binding_event.wait(self._bind_timeout_seconds):
            _raise(
                "SOURCE_MONITORING_SOAK_OBSERVER_BIND_TIMEOUT",
                "runtime start gate timed out before durable session binding",
            )
        with self._append_lock:
            if self._aborted or not self._activated:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_ABORTED",
                    "runtime start gate was aborted before evidence binding",
                )

    def bind_expected_adapter_keys(self, adapter_keys: tuple[str, ...]) -> None:
        with self._append_lock:
            if self._writer is not None or self._activated or self._aborted:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "expected adapter keys must be bound before runtime start",
                )
            if (
                type(adapter_keys) is not tuple
                or not 1 <= len(adapter_keys) <= 50
                or tuple(sorted(adapter_keys)) != adapter_keys
                or len(set(adapter_keys)) != len(adapter_keys)
                or any(
                    type(key) is not str or _ADAPTER_KEY_RE.fullmatch(key) is None
                    for key in adapter_keys
                )
            ):
                _raise(
                    "SOURCE_MONITORING_SOAK_ENABLED_ADAPTERS_INVALID",
                    "expected adapter keys are not a non-empty canonical tuple",
                )
            self._expected_adapter_keys = frozenset(adapter_keys)

    def _elapsed_locked(self) -> int:
        if self._start_monotonic_ns is None:
            _raise(
                "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                "observer has no monotonic origin",
            )
        now = _native_non_negative(self._monotonic_ns(), field="monotonic clock")
        if now < self._start_monotonic_ns:
            _raise(
                "SOURCE_MONITORING_SOAK_CLOCK_INVALID",
                "monotonic clock moved before the session origin",
            )
        return now - self._start_monotonic_ns

    def _append_locked(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        force_elapsed_ns: int | None = None,
    ) -> dict[str, Any]:
        writer = self._writer
        if writer is None:
            _raise(
                "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                "observer writer is not prepared",
            )
        elapsed = (
            self._elapsed_locked()
            if force_elapsed_ns is None
            else _native_non_negative(force_elapsed_ns, field="elapsed_ns")
        )
        wall = _native_non_negative(self._wall_time_ms(), field="wall clock")
        return writer.append(
            event_type,
            wall_time_ms=wall,
            monotonic_elapsed_ns=elapsed,
            payload=payload,
        )

    def append_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._append_lock:
            if self._writer is None or self._activated or self._aborted:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "SESSION_STARTED cannot be appended in the current state",
                )
            return self._append_locked(
                SOAK_EVENT_SESSION_STARTED,
                payload,
                force_elapsed_ns=0,
            )

    def append_sample(self, snapshot: Any) -> dict[str, Any]:
        if type(snapshot) is not dict:
            _raise(
                "SOURCE_MONITORING_SOAK_RUNTIME_SNAPSHOT_INVALID",
                "runtime snapshot must be an object",
            )
        with self._append_lock:
            if not self._activated or self._aborted:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "runtime sample cannot be appended before activation",
                )
            if snapshot.get("runtime_id") != self._runtime_id:
                _raise(
                    "SOURCE_MONITORING_SOAK_RUNTIME_ID_DRIFT",
                    "runtime snapshot identity drifted",
                )
            payload = {
                "runtime_status": snapshot.get("status"),
                "thread_alive": snapshot.get("thread_alive"),
                "liveness_verified": snapshot.get("liveness_verified"),
                "heartbeat_age_ms": snapshot.get("heartbeat_age_ms"),
                "active_adapter": snapshot.get("active_adapter"),
                "last_loop_at": snapshot.get("last_loop_at"),
            }
            return self._append_locked(SOAK_EVENT_RUNTIME_SAMPLE, payload)

    def append_end(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._append_lock:
            if not self._activated or self._aborted:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "SESSION_ENDED cannot be appended before activation",
                )
            elapsed = self._elapsed_locked()
            closed = dict(payload)
            closed["elapsed_ns"] = elapsed
            return self._append_locked(
                SOAK_EVENT_SESSION_ENDED,
                closed,
                force_elapsed_ns=elapsed,
            )

    def __call__(self, observation: Any) -> None:
        self.await_activation()
        with self._append_lock:
            if self._aborted or not self._activated:
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_ABORTED",
                    "runtime observation was aborted before evidence binding",
                )
            if type(observation) is not dict:
                _raise(
                    "SOURCE_MONITORING_SOAK_RUNTIME_OBSERVATION_INVALID",
                    "runtime observation must be an object",
                )
            run_id = observation.get("run_id")
            if (
                observation.get("runtime_id") != self._runtime_id
                or type(run_id) is not str
                or _RUN_ID_RE.fullmatch(run_id) is None
                or observation.get("state_recorded") is not True
            ):
                _raise(
                    "SOURCE_MONITORING_SOAK_RUNTIME_OBSERVATION_INVALID",
                    "runtime terminal observation is not durably recordable",
                )
            if run_id in self._run_ids:
                _raise(
                    "SOURCE_MONITORING_SOAK_DUPLICATE_RUN",
                    "runtime emitted a duplicate terminal run ID",
                )
            terminal = self._terminal_reader(self.database_path, run_id)
            if type(terminal) is not dict:
                _raise(
                    "SOURCE_MONITORING_SOAK_TERMINAL_ROW_MISSING",
                    "terminal run row is unavailable from the stable snapshot",
                )
            adapter_key = observation.get("adapter_key")
            status = observation.get("status")
            if (
                type(adapter_key) is not str
                or self._expected_adapter_keys is None
                or adapter_key not in self._expected_adapter_keys
                or terminal.get("run_id") != run_id
                or terminal.get("adapter_key") != adapter_key
                or terminal.get("status") != status
                or type(status) is not str
                or status not in _TERMINAL_STATUSES
            ):
                _raise(
                    "SOURCE_MONITORING_SOAK_TERMINAL_ROW_MISMATCH",
                    "terminal runtime receipt does not match its sealed database row",
                )
            registry = self.registry
            if registry is None:  # pragma: no cover - guarded before activation
                _raise(
                    "SOURCE_MONITORING_SOAK_OBSERVER_STATE_INVALID",
                    "observer registry is unavailable",
                )
            metadata = registry.metadata_for(adapter_key)
            error_code = terminal.get("error_code")
            if not (
                type(error_code) is str
                and (error_code == "" or _ERROR_CODE_RE.fullmatch(error_code))
            ):
                _raise(
                    "SOURCE_MONITORING_SOAK_TERMINAL_ROW_INVALID",
                    "terminal error code is outside the ledger bound",
                )
            counts = {
                key: terminal.get(key)
                for key in (
                    "observed_count",
                    "accepted_count",
                    "duplicate_count",
                    "rejected_count",
                )
            }
            payload = {
                "adapter_key": adapter_key,
                # source_adapter_runs v1 has no general config_version column.
                # This value is the in-process registry declaration bound by the
                # START registry hash, not an independent historical DB claim.
                "config_version": metadata.config_version,
                "run_id": run_id,
                "status": status,
                "state_recorded": True,
                "run_record_sha256": terminal.get("row_sha256"),
                "import_receipt_sha256": terminal.get("receipt_sha256"),
                "counts": counts,
                "error_code": error_code,
                "market_calls_performed": observation.get("market_calls_performed"),
                "source_evidence_status": "not_evaluated",
            }
            self._append_locked(SOAK_EVENT_RUN_TERMINAL, payload)
            self._run_ids.append(run_id)
            self._declarations.append(
                {
                    "run_id": run_id,
                    "status": status,
                    "state_recorded": True,
                    "run_record_sha256": terminal.get("row_sha256"),
                    "import_receipt_sha256": terminal.get("receipt_sha256"),
                }
            )


class SourceMonitoringSoakRunner:
    """Run and seal one non-resumable source-monitoring runtime session."""

    def __init__(
        self,
        *,
        runtime: Any,
        observer: SoakRuntimeObserver,
        database_path: str | Path,
        ledger_path: str | Path,
        campaign_id: str,
        session_id: str,
        preview_sha256: str,
        expected_enabled_adapters: Any,
        database_owner: Any,
        code_identity_sha256: str,
        code_identity_checker: Callable[[], Any],
        db_startup_identity_sha256: str,
        db_schema_sha256: str,
        stop_event: threading.Event | None = None,
        inventory_builder: Callable[[str | Path], Any] = build_soak_db_inventory,
        baseline_inventory_sink: Callable[[dict[str, Any]], Any] | None = None,
        final_inventory_sink: Callable[[dict[str, Any]], Any] | None = None,
        writer_factory: Callable[..., SoakEvidenceWriter] = SoakEvidenceWriter,
        monotonic_ns: Callable[[], Any] = _default_monotonic_ns,
        waiter: Callable[[threading.Event, float], Any] = _default_wait,
        _required_duration_ns: int = SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS,
        _sample_interval_ns: int = SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS,
        _maximum_sample_gap_ns: int = SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS,
    ) -> None:
        if not callable(inventory_builder) or not callable(writer_factory):
            _raise(
                "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                "runner storage dependencies must be callable",
            )
        if baseline_inventory_sink is not None and not callable(
            baseline_inventory_sink
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                "baseline_inventory_sink must be callable",
            )
        if final_inventory_sink is not None and not callable(final_inventory_sink):
            _raise(
                "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                "final_inventory_sink must be callable",
            )
        if (
            not callable(monotonic_ns)
            or not callable(waiter)
            or not callable(code_identity_checker)
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                "runner time and code-identity dependencies must be callable",
            )
        if type(observer) is not SoakRuntimeObserver:
            _raise(
                "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                "runner observer must be SoakRuntimeObserver",
            )
        if getattr(runtime, "_cycle_observer", None) is not observer:
            _raise(
                "SOURCE_MONITORING_SOAK_OBSERVER_MISMATCH",
                "runtime must be constructed with the same soak observer",
            )
        start_gate = getattr(runtime, "_start_gate", None)
        if (
            getattr(start_gate, "__self__", None) is not observer
            or getattr(start_gate, "__func__", None)
            is not SoakRuntimeObserver.await_activation
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_START_GATE_MISMATCH",
                "runtime must use the observer's durable start gate",
            )
        if Path(database_path).expanduser().absolute() != observer.database_path:
            _raise(
                "SOURCE_MONITORING_SOAK_DATABASE_MISMATCH",
                "runner and observer database paths differ",
            )
        self.runtime = runtime
        self.observer = observer
        self.database_path = observer.database_path
        self.ledger_path = Path(ledger_path).expanduser().absolute()
        self.campaign_id = _validate_identity(
            campaign_id, _CAMPAIGN_ID_RE, field="campaign_id"
        )
        self.session_id = _validate_identity(
            session_id, _SESSION_ID_RE, field="session_id"
        )
        self.preview_sha256 = _sha256(preview_sha256, field="preview_sha256")
        self.expected_enabled_adapters = _normalize_expected_enabled_adapters(
            expected_enabled_adapters
        )
        self.expected_enabled_adapter_keys = tuple(
            descriptor["adapter_key"]
            for descriptor in self.expected_enabled_adapters
        )
        if not callable(getattr(database_owner, "assert_held_for", None)):
            _raise(
                "SOURCE_MONITORING_SOAK_OWNER_INVALID",
                "database_owner must expose assert_held_for",
            )
        self.database_owner = database_owner
        self.code_identity_sha256 = _sha256(
            code_identity_sha256, field="code_identity_sha256"
        )
        self._code_identity_checker = code_identity_checker
        self.db_startup_identity_sha256 = _sha256(
            db_startup_identity_sha256, field="db_startup_identity_sha256"
        )
        self.db_schema_sha256 = _sha256(db_schema_sha256, field="db_schema_sha256")
        self.stop_event = stop_event or threading.Event()
        if type(self.stop_event) is not threading.Event:
            _raise(
                "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                "stop_event must be threading.Event",
            )
        self._inventory_builder = inventory_builder
        self._baseline_inventory_sink = baseline_inventory_sink
        self._final_inventory_sink = final_inventory_sink
        self._writer_factory = writer_factory
        self._monotonic_ns = monotonic_ns
        self._waiter = waiter
        self._required_duration_ns = _positive_duration(
            _required_duration_ns, field="required_duration_ns"
        )
        self._sample_interval_ns = _positive_duration(
            _sample_interval_ns, field="sample_interval_ns"
        )
        self._maximum_sample_gap_ns = _positive_duration(
            _maximum_sample_gap_ns, field="maximum_sample_gap_ns"
        )
        if (
            self._sample_interval_ns > self._required_duration_ns
            or self._maximum_sample_gap_ns < self._sample_interval_ns
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_INPUT_INVALID",
                "runner timing bounds are inconsistent",
            )

    def _settings_and_registry(self) -> tuple[Any, Any, str]:
        settings = getattr(self.runtime, "settings", None)
        scheduler = getattr(self.runtime, "scheduler", None)
        registry = getattr(scheduler, "registry", None)
        if not callable(getattr(settings, "to_dict", None)) or not callable(
            getattr(registry, "to_dict", None)
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_RUNTIME_INVALID",
                "runtime settings or registry cannot be sealed",
            )
        if (
            getattr(settings, "enabled", None) is not True
            or getattr(settings, "auto_start", None) is not True
            or getattr(settings, "dry_run", None) is not False
            or getattr(settings, "trading_impact_rules_enabled", None) is not False
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_SETTINGS_UNSAFE",
                "soak requires enabled auto-start, non-dry-run monitoring with impact rules off",
            )
        official = getattr(settings, "official_only", None)
        readonly_market = getattr(settings, "allow_readonly_market", None)
        if (official, readonly_market) != (True, False):
            _raise(
                "SOURCE_MONITORING_SOAK_SETTINGS_UNSAFE",
                "v1 soak runner is official-only",
            )
        mode = "official"
        if self.observer.registry is None:
            self.observer.bind_registry(registry)
        if registry is not self.observer.registry:
            _raise(
                "SOURCE_MONITORING_SOAK_OBSERVER_MISMATCH",
                "observer registry does not match runtime registry",
            )
        repository = getattr(getattr(self.runtime.scheduler, "supervisor", None), "repository", None)
        if not callable(getattr(repository, "get_state", None)) or type(
            getattr(registry, "adapter_keys", None)
        ) is not tuple:
            _raise(
                "SOURCE_MONITORING_SOAK_RUNTIME_INVALID",
                "runtime cannot project effective enabled adapters",
            )
        effective: list[dict[str, Any]] = []
        for adapter_key in registry.adapter_keys:
            metadata = registry.metadata_for(adapter_key)
            state = repository.get_state(adapter_key)
            if state is None or state.get("enabled") is not True:
                continue
            if state.get("config_version") != metadata.config_version:
                _raise(
                    "SOURCE_MONITORING_SOAK_CONFIG_CONFLICT",
                    "an enabled adapter has a persisted config-version conflict",
                )
            effective.append(
                {
                    "adapter_key": adapter_key,
                    "config_version": metadata.config_version,
                    "state_version": state.get("state_version"),
                    "checkpoint_sha256": state.get("checkpoint_sha256"),
                }
            )
        effective_adapters = _normalize_expected_enabled_adapters(effective)
        if effective_adapters != self.expected_enabled_adapters:
            _raise(
                "SOURCE_MONITORING_SOAK_ENABLED_ADAPTERS_DRIFT",
                "enabled adapter state or checkpoint differs from the confirmed preview",
            )
        return settings, registry, mode

    def _now_ns(self) -> int:
        return _native_non_negative(self._monotonic_ns(), field="monotonic clock")

    def _assert_owner(self) -> None:
        try:
            self.database_owner.assert_held_for(self.database_path)
        except BaseException as exc:
            raise SourceMonitoringSoakRunnerError(
                "SOURCE_MONITORING_SOAK_OWNER_NOT_HELD",
                "matching database ownership is not held",
            ) from exc

    def _assert_code_identity(self) -> None:
        try:
            observed = self._code_identity_checker()
        except BaseException as exc:
            raise SourceMonitoringSoakRunnerError(
                "SOURCE_MONITORING_SOAK_CODE_IDENTITY_UNAVAILABLE",
                "the production code identity could not be recomputed",
            ) from exc
        if (
            type(observed) is not str
            or _SHA256_RE.fullmatch(observed) is None
            or observed != self.code_identity_sha256
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_CODE_IDENTITY_DRIFT",
                "production code identity differs from the confirmed preview",
            )

    def _stop_runtime(self) -> bool:
        """Stop within the configured bound, then retain ownership until quiescent.

        ``False`` from ``runtime.stop`` means only that its bounded join timed
        out.  It is never permission to inspect a supposedly final database or
        let the caller release the database owner while the worker is live.
        """

        try:
            stopped = self.runtime.stop()
        except BaseException:
            stopped = False
        stopped_within_bound = stopped is True
        try:
            snapshot = self.runtime.snapshot()
        except BaseException:
            snapshot = None
        thread_alive = (
            snapshot.get("thread_alive") is True
            if type(snapshot) is dict
            else not stopped_within_bound
        )
        if not stopped_within_bound or thread_alive:
            waiter = getattr(self.runtime, "wait_until_stopped", None)
            if not callable(waiter):
                _raise(
                    "SOURCE_MONITORING_SOAK_RUNTIME_NOT_QUIESCENT",
                    "runtime stop timed out and no quiescence wait is available",
                )
            try:
                quiescent = waiter()
            except BaseException as exc:
                raise SourceMonitoringSoakRunnerError(
                    "SOURCE_MONITORING_SOAK_RUNTIME_NOT_QUIESCENT",
                    "runtime could not be proven quiescent",
                ) from exc
            if quiescent is not True:
                _raise(
                    "SOURCE_MONITORING_SOAK_RUNTIME_NOT_QUIESCENT",
                    "runtime remained live after the ownership-retaining wait",
                )
            try:
                snapshot = self.runtime.snapshot()
            except BaseException:
                snapshot = None
        if type(snapshot) is not dict:
            return False
        if snapshot.get("thread_alive") is True:
            _raise(
                "SOURCE_MONITORING_SOAK_RUNTIME_NOT_QUIESCENT",
                "runtime reported a live worker after quiescence",
            )
        if (
            snapshot.get("status") not in {"stopped", "disabled"}
            or snapshot.get("last_fatal_error_code", "") != ""
        ):
            return False
        # A stop timeout is safe after the unbounded wait, but never clean.
        return stopped_within_bound

    def run(self) -> dict[str, Any]:
        """Run one session; caller must retain exclusive DB ownership throughout."""

        self._assert_owner()
        # The runtime graph has already been constructed by this point.  Rehash
        # before it can execute so lazy imports cannot silently outrun preview.
        self._assert_code_identity()
        settings, registry, mode = self._settings_and_registry()
        self.observer.bind_expected_adapter_keys(self.expected_enabled_adapter_keys)
        supervisor = getattr(getattr(self.runtime, "scheduler", None), "supervisor", None)
        if not callable(getattr(supervisor, "initialize", None)):
            _raise(
                "SOURCE_MONITORING_SOAK_RUNTIME_INVALID",
                "runtime supervisor cannot initialize",
            )
        baseline = self._inventory_builder(self.database_path)
        if type(baseline) is not dict or type(baseline.get("runs")) is not list:
            _raise(
                "SOURCE_MONITORING_SOAK_INVENTORY_INVALID",
                "baseline inventory is invalid",
            )
        if any(
            type(entry) is dict and entry.get("status") == "RUNNING"
            for entry in baseline["runs"]
        ):
            _raise(
                "SOURCE_MONITORING_SOAK_RUNNING_ROWS_PRESENT",
                "a new soak refuses pre-existing RUNNING rows without mutating them",
            )

        recovered = supervisor.initialize()
        if type(recovered) is not int or recovered < 0:
            _raise(
                "SOURCE_MONITORING_SOAK_RECOVERY_INVALID",
                "supervisor returned an invalid recovery count",
            )
        if recovered != 0:
            _raise(
                "SOURCE_MONITORING_SOAK_RECOVERED_RUNNING_ROWS",
                "a new soak cannot begin after recovering incomplete runs",
            )
        if self._baseline_inventory_sink is not None:
            self._baseline_inventory_sink(copy.deepcopy(baseline))
        self._assert_owner()

        writer: SoakEvidenceWriter | None = None
        started = False
        activated = False
        reason = "start_failed"
        runtime_stopped_cleanly = False
        final: dict[str, Any] | None = None
        database_verdict: dict[str, Any] | None = None
        try:
            started = self.runtime.start() is True
            if not started:
                _raise(
                    "SOURCE_MONITORING_SOAK_RUNTIME_START_FAILED",
                    "managed runtime did not start",
                )
            snapshot = self.runtime.snapshot()
            runtime_id = snapshot.get("runtime_id") if type(snapshot) is dict else None
            clean_runtime_id = _validate_identity(
                runtime_id, _RUNTIME_ID_RE, field="runtime_id"
            )
            writer = self._writer_factory(
                self.ledger_path,
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                runtime_id=clean_runtime_id,
            )
            if type(writer) is not SoakEvidenceWriter:
                _raise(
                    "SOURCE_MONITORING_SOAK_WRITER_INVALID",
                    "writer_factory must return SoakEvidenceWriter",
                )
            prepared_monotonic_ns = self._now_ns()
            self.observer.prepare(
                writer,
                runtime_id=clean_runtime_id,
                start_monotonic_ns=prepared_monotonic_ns,
            )
            self.observer.append_start(
                {
                    "mode": mode,
                    "preview_sha256": self.preview_sha256,
                    "required_duration_ns": self._required_duration_ns,
                    "sample_interval_ns": self._sample_interval_ns,
                    "maximum_sample_gap_ns": self._maximum_sample_gap_ns,
                    "settings_sha256": canonical_sha256(settings.to_dict()),
                    "registry_sha256": canonical_sha256(registry.to_dict()),
                    "code_identity_sha256": self.code_identity_sha256,
                    "db_startup_identity_sha256": self.db_startup_identity_sha256,
                    "db_schema_sha256": self.db_schema_sha256,
                    "baseline_run_count": baseline.get("run_count"),
                    "baseline_run_inventory_sha256": baseline.get("inventory_sha256"),
                    "recovered_running_count": recovered,
                    "enabled_adapter_count": len(
                        self.expected_enabled_adapter_keys
                    ),
                    "enabled_adapter_keys_sha256": canonical_sha256(
                        list(self.expected_enabled_adapter_keys)
                    ),
                }
            )
            self.observer.activate()
            activated = True
            start_monotonic_ns = self.observer.start_monotonic_ns

            deadline = start_monotonic_ns + self._required_duration_ns
            while True:
                snapshot = self.runtime.snapshot()
                if type(snapshot) is not dict:
                    reason = "runtime_failed"
                    break
                status = snapshot.get("status")
                if status == "stalled":
                    reason = "runtime_stalled"
                    break
                if status == "failed" or snapshot.get("thread_alive") is not True:
                    reason = "runtime_failed"
                    break
                if snapshot.get("liveness_verified") is not True:
                    # A just-created thread may still be in "starting".  Give it
                    # a bounded fraction of the declared evidence gap to become
                    # live, without writing a sample that could look healthy.
                    now = self._now_ns()
                    if now - start_monotonic_ns >= self._maximum_sample_gap_ns:
                        reason = "runtime_stalled"
                        break
                    interrupted = self._waiter(
                        self.stop_event,
                        min(0.05, (deadline - now) / 1_000_000_000),
                    )
                    if interrupted is True:
                        reason = "operator_interrupted"
                        break
                    continue

                self.observer.append_sample(snapshot)
                now = self._now_ns()
                if now >= deadline:
                    reason = "duration_reached"
                    break
                wait_ns = min(self._sample_interval_ns, deadline - now)
                interrupted = self._waiter(
                    self.stop_event,
                    wait_ns / 1_000_000_000,
                )
                if interrupted is True:
                    reason = "operator_interrupted"
                    break

            runtime_stopped_cleanly = self._stop_runtime()
            if not runtime_stopped_cleanly and reason == "duration_reached":
                reason = "runtime_failed"
            self._assert_owner()
            # A changed tree can never receive SESSION_ENDED, even when the
            # runtime otherwise stopped cleanly after the full duration.
            self._assert_code_identity()
            final = self._inventory_builder(self.database_path)
            if type(final) is not dict:
                _raise(
                    "SOURCE_MONITORING_SOAK_INVENTORY_INVALID",
                    "final inventory is invalid",
                )
            database_verdict = validate_soak_db_inventory_delta(
                baseline,
                final,
                session_terminal_run_ids=self.observer.run_ids,
                session_run_declarations=self.observer.declarations,
            )
            if self._final_inventory_sink is not None:
                self._final_inventory_sink(copy.deepcopy(final))
            self._assert_owner()
            self.observer.append_end(
                {
                    "reason": reason,
                    "runtime_stopped_cleanly": runtime_stopped_cleanly,
                    "session_run_count": len(self.observer.run_ids),
                    "final_run_inventory_sha256": final.get("inventory_sha256"),
                    "safety": {
                        "provider_calls_performed": 0,
                        "model_calls_performed": 0,
                        "formal_rounds_created": 0,
                        "execution_capability": "none",
                        "live_trading_allowed": False,
                    },
                }
            )
            return {
                "version": SOURCE_MONITORING_SOAK_RUNNER_VERSION,
                "campaign_id": self.campaign_id,
                "session_id": self.session_id,
                "runtime_id": clean_runtime_id,
                "mode": mode,
                "end_reason": reason,
                "runtime_stopped_cleanly": runtime_stopped_cleanly,
                "ledger_record_count": writer.record_count,
                "ledger_sha256": writer.last_record_sha256,
                "baseline_run_count": baseline["run_count"],
                "baseline_inventory_sha256": baseline["inventory_sha256"],
                "final_run_count": final["run_count"],
                "final_inventory_sha256": final["inventory_sha256"],
                "session_run_count": len(self.observer.run_ids),
                "database_verdict": database_verdict,
                "source_acceptance_verdict": "NOT_EVALUATED",
                "overall_acceptance": "NOT_CLAIMED",
            }
        except BaseException:
            if not activated:
                self.observer.abort()
            if started:
                self._stop_runtime()
            raise
        finally:
            if writer is not None:
                writer.close()


__all__ = [
    "SOURCE_MONITORING_SOAK_MAXIMUM_SAMPLE_GAP_NS",
    "SOURCE_MONITORING_SOAK_REQUIRED_DURATION_NS",
    "SOURCE_MONITORING_SOAK_RUNNER_VERSION",
    "SOURCE_MONITORING_SOAK_SAMPLE_INTERVAL_NS",
    "SoakRuntimeObserver",
    "SourceMonitoringSoakRunner",
    "SourceMonitoringSoakRunnerError",
]
