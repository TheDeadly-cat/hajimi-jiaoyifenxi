from __future__ import annotations

import getpass
import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping

from ..providers.registry import PROVIDERS
from .analyzer import OpenAITelegramAnalyzer
from .bot import TelegramBot
from .collector import TelegramCollector
from .config import TelegramCollectorConfig, load_chats_config
from .health import HealthServer, HealthState
from .history import sync_history
from .keyword_config import KeywordRuleConfig, load_keywords_config
from .pipeline import PROCESS_MESSAGE_JOB, PipelineConfig, PipelineService
from .repository import TelegramRepository
from .rules import RuleEngine
from .scoring import PreScoreConfig, PreScoreEngine
from .settings import TelegramSettings
from .tdlib import (
    CTypesTDLibTransport,
    InteractiveAuthCallbacks,
    TDLibClient,
    TDLibSettings,
)


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeDependencies:
    """Offline-test hooks; production callers normally use all defaults."""

    chat_loader: Callable[[Any], Any] = load_chats_config
    keyword_loader: Callable[[Any], Any] = load_keywords_config
    repository_factory: Callable[..., Any] = TelegramRepository
    transport_factory: Callable[..., Any] = CTypesTDLibTransport
    client_factory: Callable[[TelegramSettings], Any] | None = None
    collector_factory: Callable[..., Any] = TelegramCollector
    pipeline_factory: Callable[..., Any] = PipelineService
    bot_factory: Callable[..., Any] = TelegramBot
    analyzer_factory: Callable[..., Any] = OpenAITelegramAnalyzer
    health_state_factory: Callable[..., Any] = HealthState
    health_server_factory: Callable[..., Any] = HealthServer
    history_sync: Callable[..., Any] = sync_history
    provider_registry: Any = None
    getpass_fn: Callable[[str], str] = getpass.getpass


def _build_client(settings: TelegramSettings, dependencies: RuntimeDependencies) -> Any:
    if dependencies.client_factory is not None:
        return dependencies.client_factory(settings)
    transport = dependencies.transport_factory(settings.tdlib_library_path)
    td_settings = TDLibSettings(
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        database_directory=str(settings.tdlib_database_dir),
        files_directory=str(settings.tdlib_files_dir),
        database_encryption_key=settings.tdlib_database_encryption_key,
    )
    auth = InteractiveAuthCallbacks(
        phone_number=lambda: settings.phone,
        authentication_code=lambda: dependencies.getpass_fn("Telegram login code: "),
        password=lambda: dependencies.getpass_fn("Telegram 2FA password: "),
    )
    return TDLibClient(transport, td_settings, auth=auth)


def _keyword_parts(value: Any) -> tuple[RuleEngine, tuple[str, ...]]:
    """Accept the bounded loader object, a mapping, or an injected engine."""

    if isinstance(value, RuleEngine):
        return value, ()
    if isinstance(value, KeywordRuleConfig):
        return RuleEngine.from_config(value.rule_config), value.breaking_keywords
    if isinstance(value, tuple) and len(value) == 2:
        config, breaking = value
        if isinstance(config, RuleEngine):
            return config, tuple(str(item) for item in breaking)
        if isinstance(config, Mapping):
            return RuleEngine.from_config(config), tuple(str(item) for item in breaking)
    if isinstance(value, Mapping):
        breaking = value.get("breaking_keywords", ())
        rule_config = value.get("rule_config", value)
        if not isinstance(rule_config, Mapping):
            raise TypeError("keyword rule_config must be a mapping")
        return RuleEngine.from_config(rule_config), tuple(str(item) for item in breaking)
    rule_config = getattr(value, "rule_config", None)
    if isinstance(rule_config, Mapping):
        return RuleEngine.from_config(rule_config), tuple(
            str(item) for item in getattr(value, "breaking_keywords", ())
        )
    raise TypeError("keyword loader returned an unsupported value")


def _registry_analyzer(
    settings: TelegramSettings,
    dependencies: RuntimeDependencies,
) -> tuple[Any, str, str, str]:
    if not settings.enable_ai:
        return None, settings.analyzer_provider, "", "disabled"
    registry = (
        dependencies.provider_registry
        if dependencies.provider_registry is not None
        else PROVIDERS
    )
    provider_id = str(settings.analyzer_provider or "openai").strip().lower()
    getter = getattr(registry, "get", None)
    try:
        provider = getter(provider_id) if callable(getter) else None
    except Exception:
        return None, provider_id, "", "unavailable"
    if provider is None:
        disabled = getattr(registry, "disabled_provider_ids", ())
        status = "policy_disabled" if provider_id in disabled else "unavailable"
        return None, provider_id, "", status
    # Phase 7 deliberately has one strict OpenAI structured-output adapter.
    # Other registry providers remain unavailable rather than being silently
    # routed through an incompatible prompt/output contract.
    if provider_id != "openai":
        return None, provider_id, "", "unsupported_provider"
    resolver = getattr(registry, "resolved_model", None)
    try:
        model = resolver(provider_id, "") if callable(resolver) else ""
    except Exception:
        return None, provider_id, "", "unavailable"
    pricing: dict[str, float] = {}
    if settings.ai_input_usd_per_million is not None:
        pricing["input_usd_per_million"] = settings.ai_input_usd_per_million
    if settings.ai_output_usd_per_million is not None:
        pricing["output_usd_per_million"] = settings.ai_output_usd_per_million
    try:
        analyzer = dependencies.analyzer_factory(
            provider=provider,
            model=str(model or ""),
            pricing=pricing,
        )
    except Exception:
        return None, provider_id, str(model or ""), "unavailable"
    return analyzer, provider_id, str(model or ""), "ready"


def _pipeline_stack(
    settings: TelegramSettings,
    dependencies: RuntimeDependencies,
    *,
    health: Any = None,
    allow_alerts: bool = True,
    recover_pending_on_startup: bool = True,
) -> tuple[Any, Any, TelegramCollectorConfig]:
    chat_config = dependencies.chat_loader(settings.chats_config_path)
    if not isinstance(chat_config, TelegramCollectorConfig):
        raise TypeError("chat loader must return TelegramCollectorConfig")
    rules, breaking_keywords = _keyword_parts(
        dependencies.keyword_loader(settings.keywords_config_path)
    )
    repository = dependencies.repository_factory(settings.database_path)
    set_storage_limit = getattr(repository, "set_storage_limit", None)
    if callable(set_storage_limit):
        set_storage_limit(settings.max_database_bytes)
    synchronize_policies = getattr(repository, "synchronize_chat_policies", None)
    if callable(synchronize_policies):
        synchronize_policies(chat_config.chats)
    analyzer, provider_id, model, ai_status = _registry_analyzer(settings, dependencies)
    bot = None
    if settings.enable_alerts and allow_alerts:
        bot = dependencies.bot_factory(
            token=settings.bot_token,
            destination_chat_id=settings.destination_chat_id,
            dry_run=settings.dry_run,
            timezone_name=settings.timezone_name,
        )
    state_setter = getattr(health, "set_states", None)
    if callable(state_setter):
        state_setter(
            ai_status=ai_status,
            bot_status=(
                "disabled"
                if bot is None
                else "dry_run"
                if settings.dry_run
                else "ready"
            ),
        )
    pre_score = PreScoreEngine(
        PreScoreConfig(
            store_only_below=settings.pre_score_light_threshold,
            full_analysis_above=settings.pre_score_full_threshold,
        )
    )
    pipeline = dependencies.pipeline_factory(
        repository,
        rules,
        analyzer=analyzer,
        bot=bot,
        policy_resolver=chat_config.policy_for,
        pre_score=pre_score,
        config=PipelineConfig(
            ai_enabled=settings.enable_ai and analyzer is not None,
            alerts_enabled=settings.enable_alerts and allow_alerts and bot is not None,
            provider=provider_id,
            model=model,
            alert_cooldown_seconds=settings.alert_cooldown_seconds,
        ),
        breaking_keywords=breaking_keywords,
        health=health,
        recover_pending_on_startup=recover_pending_on_startup,
    )
    return pipeline, repository, chat_config


def _collector(
    settings: TelegramSettings,
    dependencies: RuntimeDependencies,
    pipeline: Any,
    chat_config: TelegramCollectorConfig,
    *,
    message_sink: Callable[[Any], Any] | None = None,
) -> tuple[Any, Any]:
    client = _build_client(settings, dependencies)
    collector = dependencies.collector_factory(
        client,
        chat_config,
        message_sink=(message_sink if message_sink is not None else pipeline.message_sink),
        delete_sink=pipeline.delete_sink,
    )
    return client, collector


def _install_signal_handlers(
    stop_event: threading.Event,
    collector: Any,
) -> dict[int, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, Any] = {}

    def stop_handler(signum: int, frame: Any) -> None:
        del signum, frame
        stop_event.set()
        stop = getattr(collector, "stop", None)
        if callable(stop):
            stop()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, stop_handler)
        except (AttributeError, OSError, ValueError):
            continue
    return previous


def _watch_stop_request(
    stop_event: threading.Event,
    collector_done: threading.Event,
    collector: Any,
) -> None:
    """Bridge a caller-owned stop event to a blocking collector loop."""

    while not collector_done.is_set():
        if stop_event.wait(0.1):
            stop = getattr(collector, "stop", None)
            if callable(stop) and not collector_done.is_set():
                stop()
            return


def _restore_signal_handlers(previous: Mapping[int, Any]) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            continue


def _run_health_monitor(
    health: Any,
    client: Any,
    stop_event: threading.Event,
    *,
    interval_seconds: float = 0.25,
) -> None:
    updater = getattr(health, "update_tdlib", None)
    if not callable(updater):
        return
    while not stop_event.is_set():
        updater(client)
        if stop_event.wait(interval_seconds):
            break
    updater(client)


def run_service(
    settings: TelegramSettings,
    *,
    dependencies: RuntimeDependencies | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Run live whitelisted collection until stopped, with durable workers."""

    active = dependencies or RuntimeDependencies()
    settings.validate_runtime(require_alerts=True)
    settings.ensure_runtime_directories()
    health = active.health_state_factory()
    pipeline, repository, chat_config = _pipeline_stack(
        settings,
        active,
        health=health,
    )
    client, collector = _collector(settings, active, pipeline, chat_config)
    health_server = active.health_server_factory(
        health,
        host=settings.health_host,
        port=settings.health_port,
    )
    stopping = stop_event or threading.Event()
    worker_stopping = threading.Event()
    collector_done = threading.Event()
    previous = _install_signal_handlers(stopping, collector)
    worker = None
    monitor = None
    stop_watcher = None
    drained_updates = 0
    service_error: BaseException | None = None
    service_traceback = None
    cleanup_errors: list[Exception] = []
    try:
        health_server.start()
        worker = pipeline.start_worker(stop_event=worker_stopping)
        monitor = threading.Thread(
            target=_run_health_monitor,
            args=(health, client, stopping),
            name="telegram-health-monitor",
            daemon=True,
        )
        monitor.start()
        stop_watcher = threading.Thread(
            target=_watch_stop_request,
            args=(stopping, collector_done, collector),
            name="telegram-stop-watcher",
            daemon=True,
        )
        stop_watcher.start()
        collector.run()
    except BaseException as exc:
        service_error = exc
        service_traceback = exc.__traceback__
    finally:
        collector_done.set()
        stopping.set()
        stop = getattr(collector, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception as exc:
                cleanup_errors.append(exc)
        if stop_watcher is not None and stop_watcher is not threading.current_thread():
            stop_watcher.join(timeout=1.0)
        # Freeze the sole TDLib receiver before inspecting its bounded queue.
        # Only after every accepted update has traversed the ordinary durable
        # message sink do we stop the asynchronous processing worker.
        close_client = getattr(client, "close", None)
        if callable(close_client):
            try:
                close_client()
            except Exception as exc:
                cleanup_errors.append(exc)
        drain_updates = getattr(collector, "drain_pending_updates", None)
        if callable(drain_updates):
            try:
                drained_updates = int(drain_updates())
            except Exception as exc:
                cleanup_errors.append(exc)
        worker_stopping.set()
        try:
            stopped_cleanly = pipeline.stop(
                timeout=float(settings.shutdown_timeout_seconds)
            )
            if stopped_cleanly is False:
                cleanup_errors.append(
                    RuntimeError(
                        "Telegram worker did not stop before the graceful shutdown deadline"
                    )
                )
        except Exception as exc:
            cleanup_errors.append(exc)
        if worker is not None and worker is not threading.current_thread():
            # Injected/fake pipelines may not return a boolean. Always verify
            # the actual thread before reporting a clean stopped state.
            worker.join(timeout=0.1)
            if worker.is_alive() and not any(
                "graceful shutdown deadline" in str(error)
                for error in cleanup_errors
            ):
                cleanup_errors.append(
                    RuntimeError(
                        "Telegram worker did not stop before the graceful shutdown deadline"
                    )
                )
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=2.0)
        try:
            health_server.close()
        except Exception as exc:
            cleanup_errors.append(exc)
        updater = getattr(health, "update_tdlib", None)
        if callable(updater):
            try:
                updater(client)
            except Exception as exc:
                cleanup_errors.append(exc)
        _restore_signal_handlers(previous)
    if service_error is not None:
        for error in cleanup_errors:
            LOGGER.error(
                "telegram_shutdown_cleanup_failed",
                extra={"error_type": type(error).__name__.lower()[:80]},
            )
        raise service_error.with_traceback(service_traceback)
    if cleanup_errors:
        for error in cleanup_errors[1:]:
            LOGGER.error(
                "telegram_shutdown_cleanup_failed",
                extra={"error_type": type(error).__name__.lower()[:80]},
            )
        raise cleanup_errors[0]
    return {
        "status": "stopped",
        "drained_updates": drained_updates,
        "repository": repository.status_counts(),
        "health": health.snapshot(),
    }


def run_history(
    settings: TelegramSettings,
    *,
    chat_id: int,
    limit: int = 100,
    since_time: datetime | None = None,
    dependencies: RuntimeDependencies | None = None,
) -> dict[str, Any]:
    """Synchronize exactly one enabled chat; never enumerate account chats."""

    active = dependencies or RuntimeDependencies()
    settings.validate_runtime(require_alerts=False)
    settings.ensure_runtime_directories()
    pipeline, repository, chat_config = _pipeline_stack(
        settings,
        active,
        allow_alerts=False,
        recover_pending_on_startup=False,
    )
    history_job_ids: list[str] = []

    def history_message_sink(message: Any) -> dict[str, Any]:
        accepted = pipeline.message_sink(message)
        job_id = str(accepted.get("job", {}).get("id") or "")
        if job_id and job_id not in history_job_ids:
            history_job_ids.append(job_id)
        return accepted

    client, collector = _collector(
        settings,
        active,
        pipeline,
        chat_config,
        message_sink=history_message_sink,
    )
    try:
        client.start()
        waiter = getattr(client, "wait_ready", None)
        if callable(waiter) and not waiter(30.0):
            raise RuntimeError("Telegram authorization did not become ready")
        result = active.history_sync(
            client,
            collector,
            chat_id,
            limit=limit,
            since_time=since_time,
            max_limit=settings.history_max_limit,
        )
        # Only jobs that are ready in SQLite are drained. Failed provider/Bot
        # work remains durable for the long-running service to retry later.
        if history_job_ids:
            for offset in range(0, len(history_job_ids), 500):
                batch = tuple(history_job_ids[offset : offset + 500])
                pipeline.drain(
                    max_jobs=max(1, len(batch) * 3),
                    kinds=(PROCESS_MESSAGE_JOB,),
                    job_ids=batch,
                )
    finally:
        close_client = getattr(client, "close", None)
        if callable(close_client):
            close_client()
    payload = result.as_dict() if hasattr(result, "as_dict") else dict(result)
    return {
        **payload,
        "repository": repository.status_counts(),
    }


__all__ = ["RuntimeDependencies", "run_history", "run_service"]
