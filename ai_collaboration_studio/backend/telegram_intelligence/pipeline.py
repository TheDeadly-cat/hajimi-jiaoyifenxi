from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Mapping

from ..structured_logging import emit_event
from .alerts import AlertAction, AlertCooldown
from .dedup import Deduplicator
from .events import ClusterMatch, EventClusterer, EventObservation, EventState
from .models import DeletedMessageEvent, ForwardOrigin, MessageEntity, SourceMessage
from .repository import TelegramRepository, canonical_json, utc_iso
from .rules import RuleEngine
from .scoring import PreScoreEngine, ScoreEngine


PROCESS_MESSAGE_JOB = "process_message"
SEND_ALERT_JOB = "send_alert"


class PipelineJobError(RuntimeError):
    """A sanitized, retryable worker failure."""

    def __init__(self, code: str) -> None:
        clean = str(code or "pipeline_job_failed").strip().lower()[:80]
        super().__init__(clean)
        self.code = clean


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    ai_enabled: bool = True
    alerts_enabled: bool = True
    provider: str = "openai"
    model: str = ""
    duplicate_ai_threshold: float = 0.94
    dedup_window_seconds: int = 6 * 60 * 60
    recent_message_limit: int = 250
    recent_event_limit: int = 250
    lease_seconds: int = 180
    max_attempts: int = 5
    base_backoff_seconds: int = 5
    max_backoff_seconds: int = 3_600
    alert_cooldown_seconds: int = 300
    alert_max_age_seconds: int = 15 * 60
    poll_seconds: float = 0.5

    def __post_init__(self) -> None:
        if type(self.ai_enabled) is not bool or type(self.alerts_enabled) is not bool:
            raise TypeError("pipeline enable flags must be booleans")
        if not 0.5 <= float(self.duplicate_ai_threshold) <= 1.0:
            raise ValueError("duplicate_ai_threshold must be between 0.5 and 1.0")
        for name in (
            "dedup_window_seconds",
            "recent_message_limit",
            "recent_event_limit",
            "lease_seconds",
            "max_attempts",
            "base_backoff_seconds",
            "max_backoff_seconds",
            "alert_cooldown_seconds",
            "alert_max_age_seconds",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.dedup_window_seconds < 1:
            raise ValueError("dedup_window_seconds must be positive")
        if not 1 <= self.recent_message_limit <= 1_000:
            raise ValueError("recent_message_limit must be between 1 and 1000")
        if not 1 <= self.recent_event_limit <= 500:
            raise ValueError("recent_event_limit must be between 1 and 500")
        if not 1 <= self.lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        if self.max_backoff_seconds < self.base_backoff_seconds:
            raise ValueError("max_backoff_seconds must be >= base_backoff_seconds")
        if not 0 <= self.alert_cooldown_seconds <= 86_400:
            raise ValueError("alert_cooldown_seconds is invalid")
        if not 60 <= self.alert_max_age_seconds <= 86_400:
            raise ValueError("alert_max_age_seconds must be between 60 and 86400")
        if type(self.poll_seconds) not in {int, float} or self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")


def _aware_utc(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise PipelineJobError(f"invalid_{field}") from exc
    else:
        raise PipelineJobError(f"invalid_{field}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PipelineJobError(f"invalid_{field}")
    return parsed.astimezone(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return utc_iso(value)
    if is_dataclass(value):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if value is None or type(value) in {str, bool, int, float}:
        return value
    return str(value)


def _analysis_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        converter = getattr(value, "as_dict", None)
        if not callable(converter):
            raise PipelineJobError("invalid_analysis_result")
        payload = converter()
    converted = _jsonable(payload)
    if not isinstance(converted, dict):
        raise PipelineJobError("invalid_analysis_result")
    return converted


def _source_message(row: Mapping[str, Any]) -> SourceMessage:
    entities: list[MessageEntity] = []
    for raw in row.get("entities") or ():
        if isinstance(raw, Mapping):
            entities.append(
                MessageEntity(
                    kind=str(raw.get("kind") or "unknown"),
                    offset=int(raw.get("offset") or 0),
                    length=int(raw.get("length") or 0),
                    text=str(raw.get("text") or ""),
                    url=str(raw.get("url") or ""),
                )
            )
    raw_origin = row.get("forward_origin")
    origin = None
    if isinstance(raw_origin, Mapping) and raw_origin:
        origin = ForwardOrigin(
            kind=str(raw_origin.get("kind") or "unknown"),
            sender_id=raw_origin.get("sender_id") if type(raw_origin.get("sender_id")) is int else None,
            sender_name=str(raw_origin.get("sender_name") or ""),
            chat_id=raw_origin.get("chat_id") if type(raw_origin.get("chat_id")) is int else None,
            message_id=(
                raw_origin.get("message_id")
                if type(raw_origin.get("message_id")) is int
                else None
            ),
        )
    edit_timestamp = row.get("edit_timestamp")
    return SourceMessage(
        source=str(row.get("source") or "telegram"),
        chat_id=int(row["chat_id"]),
        chat_title=str(row.get("chat_title") or ""),
        chat_type=str(row.get("chat_type") or ""),
        message_id=int(row["message_id"]),
        sender_id=row.get("sender_id") if type(row.get("sender_id")) is int else None,
        sender_name=str(row.get("sender_name") or ""),
        timestamp=_aware_utc(row.get("timestamp"), "message_timestamp"),
        edit_timestamp=(
            _aware_utc(edit_timestamp, "edit_timestamp") if edit_timestamp else None
        ),
        text=str(row.get("text") or ""),
        caption=str(row.get("caption") or ""),
        urls=tuple(str(item) for item in (row.get("urls") or ())),
        entities=tuple(entities),
        reply_to_message_id=(
            row.get("reply_to_message_id")
            if type(row.get("reply_to_message_id")) is int
            else None
        ),
        forward_origin=origin,
        media_type=str(row.get("media_type") or ""),
        media_file_id=str(row.get("media_file_id") or ""),
        telegram_link=str(row.get("telegram_link") or ""),
        raw_language=str(row.get("raw_language") or ""),
        is_edited=bool(row.get("is_edited")),
        is_forwarded=bool(row.get("is_forwarded")),
        ingested_at=_aware_utc(row.get("ingested_at"), "ingested_at"),
    )


class _LeaseHeartbeat:
    """Best-effort lease renewal for provider and Bot calls."""

    def __init__(
        self,
        repository: TelegramRepository,
        job_id: str,
        lease_token: str,
        *,
        lease_seconds: int,
        clock: Callable[[], datetime],
    ) -> None:
        self.repository = repository
        self.job_id = job_id
        self.lease_token = lease_token
        self.lease_seconds = lease_seconds
        self.clock = clock
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        interval = max(1.0, min(30.0, self.lease_seconds / 3))
        while not self._stop.wait(interval):
            try:
                renewed = self.repository.renew_job_lease(
                    self.job_id,
                    self.lease_token,
                    lease_seconds=self.lease_seconds,
                    now=self.clock(),
                )
            except Exception:
                # The owning worker still performs the authoritative finish.
                # A transient SQLite error here must not kill that worker.
                continue
            if not renewed:
                self.lost.set()
                return

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._run,
            name="telegram-job-lease-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


class PipelineService:
    """Durable Telegram processing boundary.

    Collector callbacks only persist and enqueue.  The SQLite ``retry_jobs``
    table is authoritative; ``_wake`` merely reduces idle polling latency.
    """

    def __init__(
        self,
        repository: TelegramRepository,
        rules: RuleEngine,
        *,
        analyzer: Any = None,
        bot: Any = None,
        policy_resolver: Callable[[int], Any] | None = None,
        pre_score: PreScoreEngine | None = None,
        score_engine: ScoreEngine | None = None,
        deduplicator: Deduplicator | None = None,
        clusterer: EventClusterer | None = None,
        config: PipelineConfig | None = None,
        breaking_keywords: tuple[str, ...] = (),
        health: Any = None,
        worker_id: str = "telegram-worker",
        clock: Callable[[], datetime] | None = None,
        logger: logging.Logger | None = None,
        event_emitter: Callable[..., Any] = emit_event,
        recover_pending_on_startup: bool = True,
    ) -> None:
        required_repository_methods = {
            "transaction",
            "store_message_and_enqueue",
            "get_message",
            "message_version_is_current",
            "claim_job",
            "enqueue_job",
        }
        missing_repository_methods = sorted(
            name
            for name in required_repository_methods
            if not callable(getattr(repository, name, None))
        )
        if missing_repository_methods:
            raise TypeError(
                "repository does not implement the Telegram repository contract: "
                + ",".join(missing_repository_methods)
            )
        if not isinstance(rules, RuleEngine):
            raise TypeError("rules must be a RuleEngine")
        if type(recover_pending_on_startup) is not bool:
            raise TypeError("recover_pending_on_startup must be boolean")
        self.repository = repository
        self.rules = rules
        self.analyzer = analyzer
        self.bot = bot
        self.policy_resolver = policy_resolver
        self.pre_score = pre_score or PreScoreEngine()
        self.score_engine = score_engine or ScoreEngine()
        self.deduplicator = deduplicator or Deduplicator()
        self.clusterer = clusterer or EventClusterer()
        self.config = config or PipelineConfig()
        self.breaking_keywords = tuple(
            str(item).strip().casefold()
            for item in breaking_keywords
            if str(item).strip()
        )
        self.health = health
        self.worker_id = str(worker_id or "telegram-worker")[:120]
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.logger = logger
        self.event_emitter = event_emitter
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lease_context = threading.local()
        self._claim_alert_next = False
        self._alert_cooldown = (
            AlertCooldown(window_seconds=self.config.alert_cooldown_seconds)
            if self.config.alert_cooldown_seconds > 0
            else None
        )
        self.repository.reconcile_alert_job_modes(
            self._delivery_mode(),
            destination_fingerprint=self._destination_fingerprint(),
        )
        if recover_pending_on_startup:
            self.recover_pending_messages()
        self._update_health_counts()

    def _now(self) -> datetime:
        return _aware_utc(self.clock(), "clock")

    def _log(self, level: int, event: str, **metadata: Any) -> None:
        safe = {
            key: value
            for key, value in metadata.items()
            if key
            in {
                "source",
                "chat_id",
                "message_id",
                "event_id",
                "worker",
                "status",
                "error_type",
                "job_id",
                "job_kind",
                "attempt_count",
            }
        }
        if self.logger is not None:
            self.logger.log(level, event, extra={"telegram": {"event": event, **safe}})
            return
        severity = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warning",
            logging.ERROR: "error",
            logging.CRITICAL: "critical",
        }.get(level, "info")
        self.event_emitter(event, severity=severity, fields=safe)

    def _set_health_states(self, **states: str) -> None:
        setter = getattr(self.health, "set_states", None)
        if callable(setter):
            setter(**states)

    def _fence_job_lease(self) -> None:
        heartbeat = getattr(self._lease_context, "heartbeat", None)
        if heartbeat is None:
            return
        if heartbeat.lost.is_set():
            raise PipelineJobError("lease_lost")
        try:
            renewed = self.repository.renew_job_lease(
                heartbeat.job_id,
                heartbeat.lease_token,
                lease_seconds=heartbeat.lease_seconds,
                now=self._now(),
            )
        except Exception as exc:
            raise PipelineJobError("lease_fence_failed") from exc
        if not renewed:
            heartbeat.lost.set()
            raise PipelineJobError("lease_lost")

    @staticmethod
    def _policy_values(policy: Any) -> dict[str, Any]:
        def value(name: str, default: Any) -> Any:
            return policy.get(name, default) if isinstance(policy, Mapping) else getattr(policy, name, default)

        priority = str(value("priority", "medium") or "medium").strip().lower()
        if priority not in {"low", "medium", "high"}:
            priority = "medium"
        trust = value("trust_score", 50)
        trust = int(trust) if type(trust) is int else 50
        enabled = value("enabled", False)
        return {
            "enabled": enabled if type(enabled) is bool else False,
            "name": str(value("name", "") or "")[:160],
            "category": str(value("category", "general") or "general")[:80],
            "priority": priority,
            "trust_score": max(0, min(100, trust)),
        }

    def _delivery_mode(self) -> str:
        if not self.config.alerts_enabled or self.bot is None:
            return "disabled"
        dry_run = getattr(self.bot, "dry_run", None)
        return "dry_run" if dry_run is True else "live" if dry_run is False else "disabled"

    def _destination_fingerprint(self) -> str:
        destination = str(
            getattr(self.bot, "destination_chat_id", "") or ""
        ).strip()
        if not destination:
            return ""
        material = "telegram-bot-destination-v1\0" + destination
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _policy(self, chat_id: int) -> dict[str, Any]:
        policy = self.policy_resolver(chat_id) if self.policy_resolver is not None else None
        if policy is None:
            raise PermissionError("chat_not_whitelisted")
        values = self._policy_values(policy)
        if not values["enabled"]:
            raise PermissionError("chat_whitelist_disabled")
        return values

    @staticmethod
    def _policy_fingerprint(chat_id: int, policy: Mapping[str, Any]) -> str:
        material = canonical_json(
            {
                "version": "telegram-alert-policy-v1",
                "chat_id": chat_id,
                "enabled": policy.get("enabled") is True,
                "name": str(policy.get("name") or ""),
                "category": str(policy.get("category") or ""),
                "priority": str(policy.get("priority") or ""),
                "trust_score": int(policy.get("trust_score") or 0),
            }
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _message_trust(
        self,
        message: SourceMessage,
        policy: Mapping[str, Any],
    ) -> int:
        trust = int(policy["trust_score"])
        origin = message.forward_origin
        if origin is None:
            return trust
        if origin.chat_id is not None and self.policy_resolver is not None:
            try:
                origin_policy = self._policy_values(
                    self.policy_resolver(origin.chat_id)
                )
            except Exception:
                origin_policy = {"enabled": False, "trust_score": 0}
            if origin_policy.get("enabled") is True:
                return min(trust, int(origin_policy["trust_score"]))
        # A forwarding chat's reputation does not transfer to an unknown
        # original author/channel.
        return min(trust, 30)

    def message_sink(self, message: SourceMessage) -> dict[str, Any]:
        """Persist a whitelisted message and durably schedule its record version."""

        if type(message) is not SourceMessage:
            raise TypeError("message_sink requires SourceMessage")
        policy = self._policy(message.chat_id)
        self.repository.upsert_chat(
            message.chat_id,
            enabled=policy["enabled"],
            name=policy["name"],
            title=message.chat_title,
            chat_type=message.chat_type,
            category=policy["category"],
            priority=policy["priority"],
            trust_score=policy["trust_score"],
        )
        accepted = self.repository.store_message_and_enqueue(
            message,
            kind=PROCESS_MESSAGE_JOB,
            dedupe_key_prefix=(
                f"message:{message.source}:{message.chat_id}:{message.message_id}"
            ),
            max_attempts=self.config.max_attempts,
        )
        stored = accepted["message"]
        job = accepted["job"]
        self._wake.set()
        recorder = getattr(self.health, "record_message", None)
        if callable(recorder):
            recorder(message.timestamp)
        self._update_health_counts()
        self._log(
            logging.INFO,
            "telegram_message_persisted",
            source=message.source,
            chat_id=message.chat_id,
            message_id=message.message_id,
            job_id=job.get("id", ""),
            status=job.get("status", ""),
        )
        return {"message": stored, "job": job}

    ingest_message = message_sink

    def delete_sink(self, event: DeletedMessageEvent) -> dict[str, Any]:
        if type(event) is not DeletedMessageEvent:
            raise TypeError("delete_sink requires DeletedMessageEvent")
        result = self.repository.mark_deleted(event)
        self._log(
            logging.INFO,
            "telegram_messages_deleted",
            source=event.source,
            chat_id=event.chat_id,
            status="persisted",
        )
        return result

    ingest_delete = delete_sink

    def recover_pending_messages(self, *, page_size: int = 200) -> int:
        """Page over pre-upgrade/orphan rows and restore current-version jobs."""

        if type(page_size) is not int or not 1 <= page_size <= 1_000:
            raise ValueError("page_size must be between 1 and 1000")
        recovered = 0
        last_id = 0
        while True:
            with self.repository.transaction(immediate=False) as connection:
                rows = connection.execute(
                    """SELECT id,source,chat_id,message_id,record_version
                       FROM telegram_messages
                       WHERE id>? AND is_deleted=0 ORDER BY id LIMIT ?""",
                    (last_id, page_size),
                ).fetchall()
            if not rows:
                break
            for row in rows:
                last_id = int(row["id"])
                try:
                    self._policy(int(row["chat_id"]))
                except PermissionError:
                    continue
                dedupe_key = (
                    f"message:{row['source']}:{row['chat_id']}:{row['message_id']}:"
                    f"v{row['record_version']}"
                )
                with self.repository.transaction(immediate=False) as connection:
                    existing = connection.execute(
                        """SELECT 1 FROM retry_jobs
                           WHERE kind=? AND dedupe_key=? LIMIT 1""",
                        (PROCESS_MESSAGE_JOB, dedupe_key),
                    ).fetchone()
                if existing is not None:
                    continue
                job = self.repository.enqueue_job(
                    PROCESS_MESSAGE_JOB,
                    {
                        "source": str(row["source"]),
                        "chat_id": int(row["chat_id"]),
                        "message_id": int(row["message_id"]),
                        "message_row_id": int(row["id"]),
                        "record_version": int(row["record_version"]),
                    },
                    dedupe_key=dedupe_key,
                    max_attempts=self.config.max_attempts,
                )
                if not job.get("idempotent"):
                    recovered += 1
            if len(rows) < page_size:
                break
        if recovered:
            self._wake.set()
            self._log(
                logging.INFO,
                "telegram_jobs_recovered",
                worker=self.worker_id,
                status="recovered",
            )
        return recovered

    def _update_health_counts(self) -> None:
        setter = getattr(self.health, "set_counts", None)
        if not callable(setter):
            return
        counts = self.repository.job_counts()
        health_counts = {
            "queue_size": counts.get("PENDING", 0) + counts.get("RETRY", 0),
            "retry_jobs": counts.get("RETRY", 0),
            "dead_letter_jobs": counts.get("DEAD", 0),
        }
        storage_usage = getattr(self.repository, "storage_usage", None)
        if callable(storage_usage):
            try:
                storage = storage_usage()
                storage_status = str(storage.get("storage_status") or "unavailable")
                database_bytes = storage.get("database_bytes")
                database_limit_bytes = storage.get("database_limit_bytes")
                if type(database_bytes) is int and database_bytes >= 0:
                    health_counts["database_bytes"] = database_bytes
                if type(database_limit_bytes) is int and database_limit_bytes >= 0:
                    health_counts["database_limit_bytes"] = database_limit_bytes
                self._set_health_states(storage_status=storage_status)
            except Exception:
                # Capacity measurement failure is operationally unsafe, but it
                # must not crash the collector after its durable ingress step.
                self._set_health_states(storage_status="unavailable")
        setter(
            **health_counts,
        )

    def _storage_ready_for_worker_growth(self) -> bool:
        usage_getter = getattr(self.repository, "storage_usage", None)
        if not callable(usage_getter):
            return True
        try:
            usage = usage_getter()
        except Exception:
            self._set_health_states(storage_status="unavailable")
            return False
        status = str(usage.get("storage_status") or "unavailable")
        self._set_health_states(storage_status=status)
        return status == "ready"

    def _persist_dedup(
        self,
        message_row_id: int,
        record_version: int,
        result: Any,
    ) -> bool:
        duplicate_of = list(result.duplicate_of) if result.duplicate_of else []
        with self.repository.transaction() as connection:
            cursor = connection.execute(
                """UPDATE telegram_messages SET content_hash=?,simhash_hex=?,
                          duplicate_of_json=?,duplicate_score=?,duplicate_reason=?,updated_at=?
                   WHERE id=? AND record_version=? AND is_deleted=0""",
                (
                    result.content_hash,
                    f"{result.simhash:016x}",
                    canonical_json(duplicate_of),
                    float(result.duplicate_score),
                    str(result.duplicate_reason or "")[:80],
                    utc_iso(self._now()),
                    message_row_id,
                    record_version,
                ),
            )
        return int(cursor.rowcount) == 1

    def _save_event_and_link(
        self,
        event: EventState,
        message_row_id: int,
        *,
        record_version: int,
        similarity: float,
    ) -> bool:
        """Atomically maintain the event counts and their durable link."""

        now = utc_iso(self._now())
        tickers = tuple(sorted(set(event.tickers)))
        keywords = tuple(sorted(set(event.keywords)))
        record = (
            event.event_id,
            utc_iso(event.first_seen_at),
            utc_iso(event.last_seen_at),
            event.title[:500],
            event.category[:80],
            canonical_json(tickers),
            canonical_json(keywords),
            tickers[0] if tickers else "",
            event.message_count,
            event.source_count,
            event.highest_trust_source[:200],
            event.highest_trust_score,
            event.importance_score,
            event.confidence_score,
            event.status,
            f"{event.centroid_simhash:016x}",
            now,
            now,
        )
        with self.repository.transaction() as connection:
            current = connection.execute(
                """SELECT 1 FROM telegram_messages
                   WHERE id=? AND record_version=? AND is_deleted=0""",
                (message_row_id, record_version),
            ).fetchone()
            if current is None:
                return False
            connection.execute(
                """INSERT INTO events(
                       event_id,first_seen_at,last_seen_at,title,category,tickers_json,
                       keywords_json,primary_ticker,message_count,source_count,
                       highest_trust_source,highest_trust_score,importance_score,
                       confidence_score,status,centroid_simhash_hex,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET
                       first_seen_at=excluded.first_seen_at,last_seen_at=excluded.last_seen_at,
                       title=excluded.title,category=excluded.category,
                       tickers_json=excluded.tickers_json,keywords_json=excluded.keywords_json,
                       primary_ticker=excluded.primary_ticker,message_count=excluded.message_count,
                       source_count=excluded.source_count,
                       highest_trust_source=excluded.highest_trust_source,
                       highest_trust_score=excluded.highest_trust_score,
                       importance_score=excluded.importance_score,
                       confidence_score=excluded.confidence_score,status=excluded.status,
                       centroid_simhash_hex=excluded.centroid_simhash_hex,
                       updated_at=excluded.updated_at""",
                record,
            )
            connection.execute("DELETE FROM event_tickers WHERE event_id=?", (event.event_id,))
            connection.executemany(
                "INSERT INTO event_tickers(event_id,ticker) VALUES(?,?)",
                ((event.event_id, ticker) for ticker in tickers),
            )
            connection.execute(
                """INSERT INTO event_messages(
                       event_id,message_row_id,similarity,relation,added_at
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(event_id,message_row_id) DO UPDATE SET
                       similarity=excluded.similarity,
                       relation='member',
                       added_at=excluded.added_at""",
                (event.event_id, message_row_id, max(0.0, min(1.0, similarity)), "member", now),
            )
        return True

    def _event_for_message(
        self,
        message: SourceMessage,
        *,
        rule_match: Any,
        policy: Mapping[str, Any],
        message_row_id: int,
        record_version: int,
    ) -> tuple[EventState, ClusterMatch] | None:
        since = message.timestamp - self.clusterer.time_window
        states = self.repository.load_event_states(
            since=since,
            limit=self.config.recent_event_limit,
        )
        category = next(iter(rule_match.categories), str(policy.get("category") or "general"))
        keywords = tuple(
            item.split(":", 1)[-1]
            for item in (*rule_match.keyword_matches, *rule_match.regex_matches)
        )
        observation = EventObservation.from_message(
            message,
            tickers=rule_match.tickers,
            keywords=keywords,
            category=category,
            trust_score=self._message_trust(message, policy),
        )
        existing = next(
            (state for state in states if message.identity_key in state.message_keys),
            None,
        )
        if existing is not None:
            event = self.clusterer.merge(existing, observation)
            match = ClusterMatch(existing.event_id, 1.0, ("existing_message",), False)
        else:
            event, match = self.clusterer.add(observation, states)
        if not self._save_event_and_link(
            event,
            message_row_id,
            record_version=record_version,
            similarity=match.score,
        ):
            return None
        return event, match

    def _cache_key(
        self,
        content_hash: str,
        *,
        analysis_message: Mapping[str, Any],
        analysis_context: Mapping[str, Any],
        occurred_at: datetime,
    ) -> str:
        material = "\0".join(
            (
                "telegram-analysis-v2",
                self.config.provider,
                self.config.model,
                occurred_at.astimezone(timezone.utc).date().isoformat(),
                content_hash,
                canonical_json(analysis_message),
                canonical_json(analysis_context),
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _analysis_message(message: SourceMessage) -> dict[str, Any]:
        """Content-only AI envelope aligned with the cache identity.

        Provenance and source trust stay in deterministic scoring, so a cached
        semantic classification cannot inherit stronger evidence from another
        chat or event.
        """

        return {
            "source": "telegram",
            "text": message.text,
            "caption": message.caption,
            "urls": list(message.urls),
            "entities": [_jsonable(item) for item in message.entities],
            "media_type": message.media_type,
            "raw_language": message.raw_language,
            "is_forwarded": message.is_forwarded,
        }

    def _analysis_exists(
        self,
        *,
        message_row_id: int,
        record_version: int,
        event_id: str,
        provider: str,
        model: str,
        payload: Mapping[str, Any],
    ) -> bool:
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        with self.repository.transaction(immediate=False) as connection:
            row = connection.execute(
                """SELECT 1 FROM analysis_results
                   WHERE message_row_id=? AND record_version=? AND event_id=?
                         AND provider=? AND model=?
                         AND result_sha256=? LIMIT 1""",
                (
                    message_row_id,
                    record_version,
                    event_id,
                    provider,
                    model,
                    digest,
                ),
            ).fetchone()
        return row is not None

    def _process_message(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        source = str(payload.get("source") or "")
        chat_id = payload.get("chat_id")
        message_id = payload.get("message_id")
        payload_row_id = payload.get("message_row_id")
        record_version = payload.get("record_version")
        if (
            type(chat_id) is not int
            or type(message_id) is not int
            or type(payload_row_id) is not int
            or payload_row_id < 1
            or type(record_version) is not int
            or record_version < 1
        ):
            raise PipelineJobError("invalid_message_job")
        row = self.repository.get_message(source, chat_id, message_id)
        if row is None:
            raise PipelineJobError("message_missing")
        message_row_id = int(row.get("id") or 0)
        if message_row_id != payload_row_id:
            raise PipelineJobError("message_identity_mismatch")
        if int(row.get("record_version") or 0) != record_version:
            return {"status": "stale_record_version"}
        if bool(row.get("is_deleted")):
            return {"status": "message_deleted"}
        message = _source_message(row)
        try:
            policy = self._policy(chat_id)
        except PermissionError:
            return {"status": "policy_no_longer_enabled"}
        rule_match = self.rules.match(message.content_text)
        recent_rows = self.repository.recent_messages(
            since=message.timestamp - timedelta(seconds=self.config.dedup_window_seconds),
            limit=self.config.recent_message_limit,
            exclude_identity=message.identity_key,
        )
        candidates = tuple(_source_message(item) for item in recent_rows if not item.get("is_deleted"))
        dedup = self.deduplicator.evaluate(message, candidates)
        if not self._persist_dedup(message_row_id, record_version, dedup):
            return {"status": "stale_record_version"}
        event_match = self._event_for_message(
            message,
            rule_match=rule_match,
            policy=policy,
            message_row_id=message_row_id,
            record_version=record_version,
        )
        if event_match is None:
            return {"status": "stale_record_version"}
        event, match = event_match
        breaking = any(item in message.content_text.casefold() for item in self.breaking_keywords)
        pre = self.pre_score.score(
            source_priority=policy["trust_score"],
            chat_priority=policy["priority"],
            keyword_matches=len(rule_match.keyword_matches) + len(rule_match.regex_matches),
            ticker_matches=len(rule_match.tickers),
            breaking_keyword=breaking,
            message_length=len(message.content_text),
            forward_count=1 if message.is_forwarded else 0,
            same_event_frequency=event.message_count,
            has_url=bool(message.urls),
        )
        skip_reason = ""
        if not self.config.ai_enabled or self.analyzer is None:
            skip_reason = "ai_disabled"
        elif pre.tier == "store_only":
            skip_reason = "store_only"
        if skip_reason:
            return {
                "status": "processed_without_ai",
                "reason": skip_reason,
                "event_id": event.event_id,
                "pre_score": pre.score,
                "duplicate_score": dedup.duplicate_score,
            }
        analysis_message = self._analysis_message(message)
        analysis_context = {"analysis_tier": pre.tier}
        cache_key = self._cache_key(
            dedup.content_hash,
            analysis_message=analysis_message,
            analysis_context=analysis_context,
            occurred_at=message.timestamp,
        )
        cached = self.repository.get_ai_cache(cache_key)
        cache_hit = cached is not None
        if (
            dedup.duplicate_score >= self.config.duplicate_ai_threshold
            and cached is None
        ):
            return {
                "status": "processed_without_ai",
                "reason": "high_duplicate",
                "event_id": event.event_id,
                "pre_score": pre.score,
                "duplicate_score": dedup.duplicate_score,
            }
        if cached is not None:
            analysis = dict(cached.get("result") or {})
        else:
            if not self.repository.message_version_is_current(
                message_row_id,
                record_version,
            ):
                return {"status": "stale_record_version"}
            self._fence_job_lease()
            try:
                result = self.analyzer.analyze(analysis_message, analysis_context)
            except Exception as exc:
                self._set_health_states(ai_status="failed")
                raise PipelineJobError("analyzer_failed") from exc
            self._set_health_states(ai_status="ready")
            self._fence_job_lease()
            analysis = _analysis_mapping(result)
            usage = analysis.get("usage") if isinstance(analysis.get("usage"), Mapping) else {}
            provider = str(usage.get("provider") or self.config.provider)
            model = str(usage.get("model") or self.config.model)
            self.repository.put_ai_cache_and_usage(
                cache_key,
                analysis,
                provider=provider,
                model=model,
                content_hash=dedup.content_hash,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                estimated_cost=usage.get("estimated_cost_usd"),
                latency_ms=int(usage.get("latency_ms") or 0),
                request_id=str(usage.get("request_id") or ""),
                analysis_time=usage.get("analyzed_at") or self._now(),
                message_row_id=message_row_id,
                event_id=event.event_id,
            )

        if not self.repository.message_version_is_current(
            message_row_id,
            record_version,
        ):
            return {"status": "stale_record_version"}

        model_verification_status = str(
            analysis.get("verification_status") or "unknown"
        ).lower()
        # This Telegram-only phase has no independent fact-checking tool. A
        # model may classify a post's wording, but it cannot turn that post into
        # verified fact. Preserve the claimed label for audit and downgrade the
        # operational label so alerts never overstate confirmation.
        verification_status = (
            "likely"
            if model_verification_status == "confirmed"
            else model_verification_status
        )
        final_score = self.score_engine.score(
            source_trust=event.highest_trust_score,
            cross_source_count=max(1, event.source_count),
            # Distinct Telegram identities are not proof of organizational
            # independence.  This phase has no deterministic independence
            # verifier, so corroboration credit stays at one source.
            independent_sources=1,
            message_type=verification_status,
            ai_importance=analysis.get("importance_score", 0),
            ai_confidence=analysis.get("confidence_score", 0),
            novelty_score=analysis.get("novelty_score", 0),
            pre_score=pre.score,
            anonymous_source=event.highest_trust_score <= 30,
        )
        status = event.status
        if verification_status == "false_or_misleading":
            status = "disputed"
        elif status == "new" and event.source_count >= 2:
            status = "developing"
        ai_tickers = (
            analysis.get("tickers")
            if isinstance(analysis.get("tickers"), list)
            else []
        )
        model_ticker_suggestions = tuple(
            sorted(
                {
                    item.strip().upper()
                    for item in ai_tickers
                    if type(item) is str and item.strip()
                }
            )
        )
        deterministic_tickers = set(rule_match.tickers)
        approved_ai_tickers = {
            item
            for item in model_ticker_suggestions
            if item in self.rules.ticker_whitelist
            and item in deterministic_tickers
        }
        event = replace(
            event,
            tickers=tuple(sorted(set(event.tickers) | approved_ai_tickers)),
            importance_score=final_score.importance_score,
            confidence_score=final_score.confidence_score,
            status=status,
        )
        if not self._save_event_and_link(
            event,
            message_row_id,
            record_version=record_version,
            similarity=match.score,
        ):
            return {"status": "stale_record_version"}
        persisted = dict(analysis)
        risk_flags = list(
            analysis.get("risk_flags")
            if isinstance(analysis.get("risk_flags"), list)
            else ()
        )
        if model_verification_status == "confirmed":
            risk_flags.append("model_claimed_confirmed_without_external_verification")
        if set(model_ticker_suggestions) - approved_ai_tickers:
            risk_flags.append("unapproved_model_ticker_suggestion")
        persisted.update(
            {
                "model_claimed_verification_status": model_verification_status,
                "verification_status": verification_status,
                "risk_flags": list(dict.fromkeys(str(item) for item in risk_flags)),
                "importance_score": final_score.importance_score,
                "confidence_score": final_score.confidence_score,
                "recommended_action": final_score.recommended_action,
                "score_components": dict(final_score.components),
                "score_caps": list(final_score.applied_caps),
                "pre_score": pre.score,
                "pre_score_tier": pre.tier,
                "tickers": list(event.tickers),
                "model_suggested_tickers": list(model_ticker_suggestions),
            }
        )
        usage = analysis.get("usage") if isinstance(analysis.get("usage"), Mapping) else {}
        provider = str(usage.get("provider") or self.config.provider)
        model = str(usage.get("model") or self.config.model)
        if not self._analysis_exists(
            message_row_id=message_row_id,
            record_version=record_version,
            event_id=event.event_id,
            provider=provider,
            model=model,
            payload=persisted,
        ):
            saved_analysis = self.repository.save_analysis_result(
                persisted,
                provider=provider,
                model=model,
                message_row_id=message_row_id,
                record_version=record_version,
                event_id=event.event_id,
                importance_score=final_score.importance_score,
                confidence_score=final_score.confidence_score,
            )
            if saved_analysis.get("stale_record_version") is True:
                return {"status": "stale_record_version"}
        if self.config.alerts_enabled and self.bot is not None and final_score.recommended_action in {
            "alert",
            "urgent_alert",
        }:
            if not self.repository.message_version_is_current(
                message_row_id,
                record_version,
            ):
                return {"status": "stale_record_version"}
            alert_payload = {
                **persisted,
                "event_id": event.event_id,
                "first_seen_at": utc_iso(event.first_seen_at),
                "source_count": event.source_count,
                "highest_trust_score": event.highest_trust_score,
                "chat_title": message.chat_title,
                "telegram_link": message.telegram_link,
                "status": event.status,
                # Only a future deterministic verifier may set this true.
                "official_denial": False,
            }
            material_hash = hashlib.sha256(canonical_json(alert_payload).encode("utf-8")).hexdigest()
            delivery_mode = self._delivery_mode()
            if delivery_mode == "disabled":
                return {
                    "status": "processed",
                    "event_id": event.event_id,
                    "pre_score": pre.score,
                    "cache_hit": cache_hit,
                    "recommended_action": final_score.recommended_action,
                    "alert_status": "delivery_not_configured",
                }
            destination_fingerprint = self._destination_fingerprint()
            if not destination_fingerprint:
                return {
                    "status": "processed",
                    "event_id": event.event_id,
                    "pre_score": pre.score,
                    "cache_hit": cache_hit,
                    "recommended_action": final_score.recommended_action,
                    "alert_status": "destination_not_configured",
                }
            alert_created_at = self._now()
            alert_not_after = alert_created_at + timedelta(
                seconds=self.config.alert_max_age_seconds
            )
            event_token = hashlib.sha256(
                event.event_id.encode("utf-8")
            ).hexdigest()[:24]
            alert_key = (
                f"alert:{delivery_mode}:{destination_fingerprint[:16]}:"
                f"{event_token}:{material_hash[:32]}"
            )
            self.repository.enqueue_job(
                SEND_ALERT_JOB,
                {
                    "event_id": event.event_id,
                    "message_row_id": message_row_id,
                    "record_version": record_version,
                    "alert_kind": final_score.recommended_action,
                    "message_hash": material_hash,
                    "alert_key": alert_key,
                    "delivery_mode": delivery_mode,
                    "destination_fingerprint": destination_fingerprint,
                    "policy_fingerprint": self._policy_fingerprint(chat_id, policy),
                    "created_at": utc_iso(alert_created_at),
                    "not_after": utc_iso(alert_not_after),
                    "alert": alert_payload,
                },
                dedupe_key=alert_key,
                max_attempts=self.config.max_attempts,
            )
            self._wake.set()
        return {
            "status": "processed",
            "event_id": event.event_id,
            "pre_score": pre.score,
            "cache_hit": cache_hit,
            "recommended_action": final_score.recommended_action,
        }

    @staticmethod
    def _snapshot_state_key(event_id: str) -> str:
        digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        return f"alert_snapshot:{digest}"

    def _process_alert(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        event_id = str(payload.get("event_id") or "")
        alert = payload.get("alert")
        if not event_id or not isinstance(alert, Mapping):
            raise PipelineJobError("invalid_alert_job")
        delivery_mode = self._delivery_mode()
        if delivery_mode == "disabled":
            raise PipelineJobError("alert_delivery_disabled")
        if str(payload.get("delivery_mode") or "") != delivery_mode:
            raise PipelineJobError("alert_delivery_mode_mismatch")
        destination_fingerprint = str(
            payload.get("destination_fingerprint") or ""
        )
        if (
            len(destination_fingerprint) != 64
            or destination_fingerprint != self._destination_fingerprint()
        ):
            return {
                "status": "SUPPRESSED",
                "event_id": event_id,
                "reason": "destination_changed_or_unbound",
            }
        try:
            not_after = _aware_utc(payload.get("not_after"), "alert_not_after")
        except PipelineJobError:
            return {
                "status": "SUPPRESSED",
                "event_id": event_id,
                "reason": "alert_freshness_deadline_missing",
            }
        if self._now() > not_after:
            return {
                "status": "SUPPRESSED",
                "event_id": event_id,
                "reason": "alert_expired",
            }
        message_row_id = payload.get("message_row_id")
        if type(message_row_id) is not int or message_row_id < 1:
            raise PipelineJobError("invalid_alert_message")
        record_version = payload.get("record_version")
        if type(record_version) is not int or record_version < 1:
            raise PipelineJobError("invalid_alert_record_version")
        evidence = self.repository.event_delivery_state(
            event_id,
            message_row_id=message_row_id,
            record_version=record_version,
        )
        if not evidence["exists"]:
            raise PipelineJobError("event_missing")
        trigger_chat_id = evidence.get("trigger_chat_id")
        if type(trigger_chat_id) is int:
            try:
                current_policy = self._policy(trigger_chat_id)
            except PermissionError:
                return {
                    "status": "SUPPRESSED",
                    "event_id": event_id,
                    "reason": "source_policy_revoked",
                }
            if str(payload.get("policy_fingerprint") or "") != self._policy_fingerprint(
                trigger_chat_id,
                current_policy,
            ):
                return {
                    "status": "SUPPRESSED",
                    "event_id": event_id,
                    "reason": "source_policy_changed",
                }
        message_hash = str(payload.get("message_hash") or "")
        alert_key = str(payload.get("alert_key") or "")
        if not alert_key:
            raise PipelineJobError("invalid_alert_key")
        exact = self.repository.alert_by_idempotency_key(alert_key)
        if exact is not None and str(exact.get("status") or "").upper() in {
            "SENT",
            "WOULD_SEND",
        }:
            snapshot = dict(alert)
            snapshot["event_id"] = event_id
            snapshot["last_alert_at"] = exact.get("sent_at") or exact.get("created_at")
            self.repository.set_system_state(self._snapshot_state_key(event_id), snapshot)
            return {
                "status": "ALREADY_RECORDED",
                "event_id": event_id,
                "reason": "matching_idempotency_record",
            }
        latest = self.repository.latest_alert(event_id)
        if (
            latest is not None
            and str(latest.get("status") or "").upper() in {"SENT", "WOULD_SEND"}
            and message_hash
            and str(latest.get("message_hash") or "") == message_hash
        ):
            snapshot = dict(alert)
            snapshot["event_id"] = event_id
            snapshot["last_alert_at"] = latest.get("sent_at") or latest.get("created_at")
            self.repository.set_system_state(self._snapshot_state_key(event_id), snapshot)
            return {
                "status": "ALREADY_RECORDED",
                "event_id": event_id,
                "reason": "matching_delivery_record",
            }
        if (
            int(evidence["active_message_count"]) < 1
            or evidence["trigger_message_active"] is not True
            or str(evidence["status"]) == "resolved"
        ):
            self.repository.record_alert(
                event_id,
                alert_kind=str(payload.get("alert_kind") or "alert"),
                status="SUPPRESSED",
                cooldown_seconds=self.config.alert_cooldown_seconds,
                destination=str(getattr(self.bot, "destination_chat_id", "") or ""),
                message_hash=message_hash,
                idempotency_key=f"{alert_key}:retracted",
                at=self._now(),
            )
            return {
                "status": "SUPPRESSED",
                "event_id": event_id,
                "reason": "source_evidence_retracted",
            }
        previous = self.repository.get_system_state(self._snapshot_state_key(event_id), None)
        if not isinstance(previous, Mapping):
            previous = None
        if previous is not None:
            previous = dict(previous)
            previous.setdefault("event_id", event_id)
        elif latest is not None:
            previous = {
                "event_id": event_id,
                "last_alert_at": latest.get("sent_at") or latest.get("created_at"),
            }
        current = dict(alert)
        current["event_id"] = event_id
        now = self._now()
        if self._alert_cooldown is None:
            decision_action = AlertAction.ALERT
            decision_reason = "cooldown_disabled"
        else:
            decision = self._alert_cooldown.decide(previous, current, now=now)
            decision_action = decision.action
            decision_reason = decision.reason
        kind = str(payload.get("alert_kind") or "alert")
        destination = str(getattr(self.bot, "destination_chat_id", "") or "")
        if decision_action is AlertAction.SUPPRESS:
            self.repository.record_alert(
                event_id,
                alert_kind=kind,
                status="SUPPRESSED",
                cooldown_seconds=self.config.alert_cooldown_seconds,
                destination=destination,
                message_hash=message_hash,
                idempotency_key=f"{alert_key}:suppressed",
                at=now,
            )
            return {"status": "SUPPRESSED", "reason": decision_reason, "event_id": event_id}
        evidence = self.repository.event_delivery_state(
            event_id,
            message_row_id=message_row_id,
            record_version=record_version,
        )
        if (
            not evidence["exists"]
            or int(evidence["active_message_count"]) < 1
            or evidence["trigger_message_active"] is not True
            or str(evidence["status"]) == "resolved"
        ):
            return {
                "status": "SUPPRESSED",
                "event_id": event_id,
                "reason": "source_evidence_changed_before_send",
            }
        trigger_chat_id = evidence.get("trigger_chat_id")
        if type(trigger_chat_id) is int:
            try:
                current_policy = self._policy(trigger_chat_id)
            except PermissionError:
                return {
                    "status": "SUPPRESSED",
                    "event_id": event_id,
                    "reason": "source_policy_revoked_before_send",
                }
            if str(payload.get("policy_fingerprint") or "") != self._policy_fingerprint(
                trigger_chat_id,
                current_policy,
            ):
                return {
                    "status": "SUPPRESSED",
                    "event_id": event_id,
                    "reason": "source_policy_changed_before_send",
                }
        self._fence_job_lease()
        try:
            send_result = self.bot.send_alert(current)
        except Exception as exc:
            self._set_health_states(bot_status="failed")
            raise PipelineJobError("bot_send_failed") from exc
        if not bool(getattr(send_result, "ok", False)):
            self._set_health_states(bot_status="failed")
            raise PipelineJobError("bot_send_failed")
        result_status = str(getattr(send_result, "status", "") or "").upper()
        if result_status not in {"SENT", "WOULD_SEND"}:
            self._set_health_states(bot_status="failed")
            raise PipelineJobError("bot_invalid_result")
        self._fence_job_lease()
        self._set_health_states(
            bot_status="dry_run" if result_status == "WOULD_SEND" else "ready"
        )
        recorded = self.repository.record_alert(
            event_id,
            alert_kind=kind,
            status=result_status,
            cooldown_seconds=self.config.alert_cooldown_seconds,
            destination=destination,
            message_hash=message_hash,
            idempotency_key=alert_key,
            at=now,
        )
        snapshot = dict(current)
        snapshot["last_alert_at"] = recorded.get("sent_at") or utc_iso(now)
        self.repository.set_system_state(self._snapshot_state_key(event_id), snapshot)
        recorder = getattr(self.health, "record_alert", None)
        if callable(recorder):
            recorder(now)
        return {"status": result_status, "event_id": event_id, "reason": decision_reason}

    def process_once(
        self,
        *,
        now: datetime | None = None,
        kinds: tuple[str, ...] = (PROCESS_MESSAGE_JOB, SEND_ALERT_JOB),
        job_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        if type(kinds) is not tuple or not kinds or any(
            item not in {PROCESS_MESSAGE_JOB, SEND_ALERT_JOB} for item in kinds
        ):
            raise ValueError("kinds must contain supported job kinds")
        if job_ids is not None and (
            type(job_ids) is not tuple
            or any(type(item) is not str for item in job_ids)
        ):
            raise ValueError("job_ids must be null or a tuple of strings")
        # At the 90% watermark, stop claiming backlog before analysis/event/
        # cache writes can consume the reserved shutdown headroom. The
        # collector's durable admission path independently rejects new rows.
        if not self._storage_ready_for_worker_growth():
            self._update_health_counts()
            return None
        delivery_mode = self._delivery_mode()
        effective_kinds = tuple(
            dict.fromkeys(
                item
                for item in kinds
                if item != SEND_ALERT_JOB or delivery_mode != "disabled"
            )
        )
        if not effective_kinds:
            self._update_health_counts()
            return None
        if len(effective_kinds) == 2:
            claim_order = (
                (SEND_ALERT_JOB, PROCESS_MESSAGE_JOB)
                if self._claim_alert_next
                else (PROCESS_MESSAGE_JOB, SEND_ALERT_JOB)
            )
        else:
            claim_order = effective_kinds
        claim_time = now or self._now()
        job = None
        for kind in claim_order:
            job = self.repository.claim_job(
                self.worker_id,
                kinds=(kind,),
                job_ids=job_ids,
                dedupe_prefix=(
                    f"alert:{delivery_mode}:"
                    if kind == SEND_ALERT_JOB
                    else None
                ),
                lease_seconds=self.config.lease_seconds,
                now=claim_time,
            )
            if job is not None:
                self._claim_alert_next = kind == PROCESS_MESSAGE_JOB
                break
        if job is None:
            self._update_health_counts()
            return None
        payload = job.get("payload")
        work_error: Exception | None = None
        result: dict[str, Any] = {}
        heartbeat = _LeaseHeartbeat(
            self.repository,
            str(job["id"]),
            str(job["lease_token"]),
            lease_seconds=self.config.lease_seconds,
            clock=self._now,
        )
        with heartbeat:
            self._lease_context.heartbeat = heartbeat
            try:
                if not isinstance(payload, Mapping):
                    raise PipelineJobError("invalid_job_payload")
                if job["kind"] == PROCESS_MESSAGE_JOB:
                    result = self._process_message(payload)
                    recorder = getattr(self.health, "record_processed", None)
                    if callable(recorder):
                        recorder()
                elif job["kind"] == SEND_ALERT_JOB:
                    result = self._process_alert(payload)
                else:
                    raise PipelineJobError("unsupported_job_kind")
                self._fence_job_lease()
            except Exception as exc:
                work_error = exc
            finally:
                try:
                    del self._lease_context.heartbeat
                except AttributeError:
                    pass

        finish_time = self._now()
        if work_error is None:
            try:
                final = self.repository.complete_job(
                    job["id"],
                    job["lease_token"],
                    now=finish_time,
                )
            except ValueError:
                current = self.repository.get_job(job["id"]) or job
                self._log(
                    logging.WARNING,
                    "telegram_job_lease_lost",
                    job_id=job["id"],
                    job_kind=job["kind"],
                    worker=self.worker_id,
                    status=current.get("status", ""),
                    error_type="lease_lost",
                )
                self._update_health_counts()
                return {
                    "job": current,
                    "result": result,
                    "error_code": "lease_lost",
                }
            self._log(
                logging.INFO,
                "telegram_job_completed",
                job_id=job["id"],
                job_kind=job["kind"],
                worker=self.worker_id,
                status=final.get("status", ""),
            )
            output = {"job": final, "result": result}
        else:
            error_code = (
                work_error.code
                if isinstance(work_error, PipelineJobError)
                else type(work_error).__name__.lower()[:80]
            )
            try:
                final = self.repository.fail_job(
                    job["id"],
                    job["lease_token"],
                    error_code=error_code,
                    base_backoff_seconds=self.config.base_backoff_seconds,
                    max_backoff_seconds=self.config.max_backoff_seconds,
                    now=finish_time,
                )
            except ValueError:
                current = self.repository.get_job(job["id"]) or job
                self._log(
                    logging.WARNING,
                    "telegram_job_lease_lost",
                    job_id=job["id"],
                    job_kind=job["kind"],
                    worker=self.worker_id,
                    status=current.get("status", ""),
                    error_type="lease_lost",
                )
                self._update_health_counts()
                return {"job": current, "error_code": "lease_lost"}
            self._log(
                logging.WARNING,
                "telegram_job_failed",
                job_id=job["id"],
                job_kind=job["kind"],
                worker=self.worker_id,
                status=final.get("status", ""),
                error_type=error_code,
                attempt_count=final.get("attempt_count", 0),
            )
            output = {"job": final, "error_code": error_code}
        self._update_health_counts()
        return output

    def drain(
        self,
        *,
        max_jobs: int = 1_000,
        kinds: tuple[str, ...] = (PROCESS_MESSAGE_JOB, SEND_ALERT_JOB),
        job_ids: tuple[str, ...] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        if type(max_jobs) is not int or not 1 <= max_jobs <= 100_000:
            raise ValueError("max_jobs must be between 1 and 100000")
        output: list[dict[str, Any]] = []
        for _ in range(max_jobs):
            result = self.process_once(kinds=kinds, job_ids=job_ids)
            if result is None:
                break
            output.append(result)
        return tuple(output)

    def run_worker(self, *, stop_event: threading.Event | None = None) -> None:
        external_stop = stop_event
        self._stop.clear()
        failures = 0
        state_setter = getattr(self.health, "set_states", None)
        if callable(state_setter):
            state_setter(worker_status="starting")
        try:
            while not self._stop.is_set() and not (
                external_stop and external_stop.is_set()
            ):
                try:
                    result = self.process_once()
                except Exception as exc:
                    failures += 1
                    if callable(state_setter):
                        state_setter(worker_status="failed")
                    self._log(
                        logging.ERROR,
                        "telegram_worker_iteration_failed",
                        worker=self.worker_id,
                        status="retrying",
                        error_type=type(exc).__name__.lower()[:80],
                    )
                    delay = min(
                        5.0,
                        max(
                            float(self.config.poll_seconds),
                            float(self.config.poll_seconds) * (2 ** min(failures, 6)),
                        ),
                    )
                    if self._stop.wait(delay):
                        break
                    continue
                failures = 0
                if callable(state_setter):
                    state_setter(worker_status="ready")
                if result is None:
                    self._wake.wait(float(self.config.poll_seconds))
                    self._wake.clear()
        finally:
            if callable(state_setter):
                state_setter(worker_status="stopped")

    def start_worker(self, *, stop_event: threading.Event | None = None) -> threading.Thread:
        thread = self._thread
        if thread is not None and thread.is_alive():
            return thread
        self._thread = threading.Thread(
            target=self.run_worker,
            kwargs={"stop_event": stop_event},
            name="telegram-pipeline-worker",
            # A process must not silently exit while an accepted external AI
            # or Bot request is still resolving its durable completion state.
            daemon=False,
        )
        self._thread.start()
        return self._thread

    def stop(self, *, timeout: float = 5.0) -> bool:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))
            return not thread.is_alive()
        return True


__all__ = [
    "PROCESS_MESSAGE_JOB",
    "SEND_ALERT_JOB",
    "PipelineConfig",
    "PipelineJobError",
    "PipelineService",
]
