from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

from .dedup import content_fingerprint, normalize_text, simhash64
from .events import EventState, source_key_for_values
from .models import DeletedMessageEvent, SourceMessage


SCHEMA_VERSION = "telegram_intelligence_schema_v1"
JOB_STATES = frozenset({"PENDING", "CLAIMED", "RETRY", "COMPLETED", "DEAD"})
ALERT_STATES = frozenset({"PENDING", "SENT", "FAILED", "WOULD_SEND", "SUPPRESSED"})
MAX_CLUSTER_EVENT_MESSAGES = 500
DEFAULT_MAX_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_DATABASE_BYTES = 2**63 - 1


class TelegramStorageCapacityError(RuntimeError):
    """Raised when durable Telegram storage cannot safely accept more data."""


def _utc_datetime(value: Any, *, field: str, allow_none: bool = False) -> datetime | None:
    if value is None and allow_none:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO datetime") from exc
    elif type(value) in {int, float}:
        parsed = datetime.fromtimestamp(float(value), timezone.utc)
    else:
        raise TypeError(f"{field} must be a timezone-aware datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def utc_iso(value: Any, *, field: str = "timestamp", allow_none: bool = False) -> str:
    parsed = _utc_datetime(value, field=field, allow_none=allow_none)
    if parsed is None:
        return ""
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return utc_iso(value)
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def _json_object(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    canonical = canonical_json(dict(value))
    decoded = json.loads(canonical)
    if type(decoded) is not dict:
        raise TypeError(f"{field} must encode to an object")
    return decoded


def _safe_identifier(value: Any, *, field: str, maximum: int = 120) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(character in text for character in "\r\n\x00"):
        raise ValueError(f"{field} is invalid")
    return text


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    output = dict(row)
    for key in (
        "urls_json",
        "entities_json",
        "forward_origin_json",
        "duplicate_of_json",
        "tickers_json",
        "keywords_json",
        "result_json",
        "payload_json",
        "value_json",
    ):
        if key in output:
            raw = output.pop(key)
            public_key = key[:-5]
            try:
                output[public_key] = json.loads(raw) if raw else ([] if key.endswith("s_json") else {})
            except (TypeError, json.JSONDecodeError):
                output[public_key] = None
    for key in (
        "enabled",
        "is_edited",
        "is_forwarded",
        "is_deleted",
    ):
        if key in output:
            output[key] = bool(output[key])
    return output


class TelegramRepository:
    """Standalone SQLite persistence for the read-only Telegram pipeline.

    This repository deliberately refuses the Studio formal database path. It owns
    only its dedicated file and never imports or initializes ``backend.store``.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock: Callable[[], Any] | None = None,
        max_database_bytes: int = DEFAULT_MAX_DATABASE_BYTES,
    ) -> None:
        requested = Path(path).expanduser()
        self.path = requested.resolve()
        project_root = Path(__file__).resolve().parents[2]
        formal_default = (project_root / "runtime" / "collaboration_studio.sqlite3").resolve()
        configured_formal = str(os.getenv("AI_STUDIO_DATABASE_PATH") or "").strip()
        forbidden = {formal_default}
        if configured_formal:
            forbidden.add(Path(configured_formal).expanduser().resolve())
        if self.path in forbidden or self.path.name.casefold() == "collaboration_studio.sqlite3":
            raise ValueError("TelegramRepository must not use the formal Studio database")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.path.parent, 0o700)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._max_database_bytes = DEFAULT_MAX_DATABASE_BYTES
        self.set_storage_limit(max_database_bytes)
        self._initialize()

    def _now(self) -> datetime:
        return _utc_datetime(self._clock(), field="repository clock")  # type: ignore[return-value]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        if os.name == "posix":
            for candidate in (
                self.path,
                Path(str(self.path) + "-wal"),
                Path(str(self.path) + "-shm"),
            ):
                if candidate.exists():
                    os.chmod(candidate, 0o600)
        return connection

    @property
    def max_database_bytes(self) -> int:
        with self._lock:
            return self._max_database_bytes

    def set_storage_limit(self, max_bytes: int) -> None:
        if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_DATABASE_BYTES:
            raise ValueError("max database bytes must be a positive bounded integer")
        with self._lock:
            self._max_database_bytes = max_bytes

    def storage_usage(self) -> dict[str, Any]:
        """Return bounded size metadata for the database and its WAL sidecars."""

        candidates = (
            ("database_file_bytes", self.path),
            ("wal_file_bytes", Path(str(self.path) + "-wal")),
            ("shm_file_bytes", Path(str(self.path) + "-shm")),
        )
        with self._lock:
            components: dict[str, int] = {}
            for name, candidate in candidates:
                try:
                    size = candidate.stat().st_size
                except FileNotFoundError:
                    size = 0
                except OSError as exc:
                    raise TelegramStorageCapacityError(
                        "unable to measure Telegram database capacity"
                    ) from exc
                if type(size) is not int or size < 0:
                    raise TelegramStorageCapacityError(
                        "invalid Telegram database capacity measurement"
                    )
                components[name] = size
            total = sum(components.values())
            limit = self._max_database_bytes
        if total >= limit:
            status = "full"
        elif total * 10 >= limit * 9:
            status = "warning"
        else:
            status = "ready"
        return {
            **components,
            "database_bytes": total,
            "database_limit_bytes": limit,
            "database_usage_ratio": total / limit,
            "storage_status": status,
        }

    def _assert_storage_capacity(self) -> None:
        usage = self.storage_usage()
        # Stop admission at the warning watermark, leaving ten percent of the
        # configured ceiling for the single already-claimed job, SQLite
        # bookkeeping, and clean shutdown/recovery state.
        if usage["storage_status"] != "ready":
            raise TelegramStorageCapacityError(
                "Telegram database capacity limit reached"
            )

    @contextmanager
    def transaction(
        self,
        *,
        immediate: bool = True,
        admit_growth: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                if admit_growth:
                    # BEGIN IMMEDIATE also fences writers from other processes
                    # before measuring the main database and WAL sidecars.
                    self._assert_storage_capacity()
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_chats (
                    chat_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                    name TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    chat_type TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    priority TEXT NOT NULL DEFAULT 'medium',
                    trust_score INTEGER NOT NULL DEFAULT 50 CHECK(trust_score BETWEEN 0 AND 100),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS telegram_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL CHECK(source='telegram'),
                    chat_id INTEGER NOT NULL,
                    chat_title TEXT NOT NULL DEFAULT '',
                    chat_type TEXT NOT NULL DEFAULT '',
                    message_id INTEGER NOT NULL,
                    sender_id INTEGER,
                    sender_name TEXT NOT NULL DEFAULT '',
                    timestamp TEXT NOT NULL,
                    edit_timestamp TEXT NOT NULL DEFAULT '',
                    text TEXT NOT NULL DEFAULT '',
                    caption TEXT NOT NULL DEFAULT '',
                    urls_json TEXT NOT NULL DEFAULT '[]',
                    entities_json TEXT NOT NULL DEFAULT '[]',
                    reply_to_message_id INTEGER,
                    forward_origin_json TEXT NOT NULL DEFAULT '{}',
                    media_type TEXT NOT NULL DEFAULT '',
                    media_file_id TEXT NOT NULL DEFAULT '',
                    telegram_link TEXT NOT NULL DEFAULT '',
                    raw_language TEXT NOT NULL DEFAULT '',
                    is_edited INTEGER NOT NULL DEFAULT 0 CHECK(is_edited IN (0,1)),
                    is_forwarded INTEGER NOT NULL DEFAULT 0 CHECK(is_forwarded IN (0,1)),
                    ingested_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    simhash_hex TEXT NOT NULL,
                    duplicate_of_json TEXT NOT NULL DEFAULT '[]',
                    duplicate_score REAL NOT NULL DEFAULT 0 CHECK(duplicate_score BETWEEN 0 AND 1),
                    duplicate_reason TEXT NOT NULL DEFAULT '',
                    is_deleted INTEGER NOT NULL DEFAULT 0 CHECK(is_deleted IN (0,1)),
                    deleted_at TEXT NOT NULL DEFAULT '',
                    delete_is_permanent INTEGER NOT NULL DEFAULT 0 CHECK(delete_is_permanent IN (0,1)),
                    delete_from_cache INTEGER NOT NULL DEFAULT 0 CHECK(delete_from_cache IN (0,1)),
                    record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version > 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source,chat_id,message_id)
                );
                CREATE TABLE IF NOT EXISTS telegram_message_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_row_id INTEGER NOT NULL,
                    record_version INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64),
                    created_at TEXT NOT NULL,
                    UNIQUE(message_row_id,record_version),
                    FOREIGN KEY(message_row_id) REFERENCES telegram_messages(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    tickers_json TEXT NOT NULL DEFAULT '[]',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    primary_ticker TEXT NOT NULL DEFAULT '',
                    message_count INTEGER NOT NULL DEFAULT 0 CHECK(message_count >= 0),
                    source_count INTEGER NOT NULL DEFAULT 0 CHECK(source_count >= 0),
                    highest_trust_source TEXT NOT NULL DEFAULT '',
                    highest_trust_score INTEGER NOT NULL DEFAULT 0 CHECK(highest_trust_score BETWEEN 0 AND 100),
                    importance_score INTEGER NOT NULL DEFAULT 0 CHECK(importance_score BETWEEN 0 AND 100),
                    confidence_score INTEGER NOT NULL DEFAULT 0 CHECK(confidence_score BETWEEN 0 AND 100),
                    status TEXT NOT NULL CHECK(status IN ('new','developing','confirmed','disputed','resolved')),
                    centroid_simhash_hex TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_messages (
                    event_id TEXT NOT NULL,
                    message_row_id INTEGER NOT NULL,
                    similarity REAL NOT NULL DEFAULT 0 CHECK(similarity BETWEEN 0 AND 1),
                    relation TEXT NOT NULL DEFAULT 'member',
                    added_at TEXT NOT NULL,
                    PRIMARY KEY(event_id,message_row_id),
                    FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE CASCADE,
                    FOREIGN KEY(message_row_id) REFERENCES telegram_messages(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS event_tickers (
                    event_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    PRIMARY KEY(event_id,ticker),
                    FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS analysis_results (
                    id TEXT PRIMARY KEY,
                    message_row_id INTEGER,
                    record_version INTEGER NOT NULL DEFAULT 1 CHECK(record_version > 0),
                    event_id TEXT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64),
                    importance_score INTEGER NOT NULL DEFAULT 0 CHECK(importance_score BETWEEN 0 AND 100),
                    confidence_score INTEGER NOT NULL DEFAULT 0 CHECK(confidence_score BETWEEN 0 AND 100),
                    created_at TEXT NOT NULL,
                    CHECK(message_row_id IS NOT NULL OR event_id IS NOT NULL),
                    FOREIGN KEY(message_row_id) REFERENCES telegram_messages(id) ON DELETE CASCADE,
                    FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    alert_kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING','SENT','FAILED','WOULD_SEND','SUPPRESSED')),
                    destination TEXT NOT NULL DEFAULT '',
                    message_hash TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL UNIQUE,
                    sent_at TEXT NOT NULL DEFAULT '',
                    cooldown_until TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS ai_usage (
                    id TEXT PRIMARY KEY,
                    message_row_id INTEGER,
                    event_id TEXT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK(input_tokens >= 0),
                    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK(output_tokens >= 0),
                    estimated_cost REAL,
                    latency_ms INTEGER NOT NULL DEFAULT 0 CHECK(latency_ms >= 0),
                    request_id TEXT NOT NULL DEFAULT '',
                    analysis_time TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(message_row_id) REFERENCES telegram_messages(id) ON DELETE SET NULL,
                    FOREIGN KEY(event_id) REFERENCES events(event_id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS retry_jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL DEFAULT '',
                    event_id TEXT NOT NULL DEFAULT '',
                    message_row_id INTEGER,
                    record_version INTEGER,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
                    status TEXT NOT NULL CHECK(status IN ('PENDING','CLAIMED','RETRY','COMPLETED','DEAD')),
                    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
                    next_attempt_at TEXT NOT NULL,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_token TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    dead_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_result_cache (
                    cache_key TEXT PRIMARY KEY,
                    content_hash TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64),
                    expires_at TEXT NOT NULL DEFAULT '',
                    hit_count INTEGER NOT NULL DEFAULT 0 CHECK(hit_count >= 0),
                    last_used_at TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_telegram_messages_chat_message
                    ON telegram_messages(chat_id,message_id);
                CREATE INDEX IF NOT EXISTS idx_telegram_messages_timestamp
                    ON telegram_messages(timestamp DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_telegram_messages_content_hash
                    ON telegram_messages(content_hash);
                CREATE INDEX IF NOT EXISTS idx_events_status_time
                    ON events(status,last_seen_at DESC,event_id);
                CREATE INDEX IF NOT EXISTS idx_events_primary_ticker
                    ON events(primary_ticker,last_seen_at DESC);
                CREATE INDEX IF NOT EXISTS idx_event_tickers_ticker
                    ON event_tickers(ticker,event_id);
                CREATE INDEX IF NOT EXISTS idx_event_messages_message
                    ON event_messages(message_row_id,event_id);
                CREATE INDEX IF NOT EXISTS idx_analysis_results_event
                    ON analysis_results(event_id,created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_alerts_event_status
                    ON alerts(event_id,status,cooldown_until DESC);
                CREATE INDEX IF NOT EXISTS idx_ai_usage_event_time
                    ON ai_usage(event_id,analysis_time DESC);
                CREATE INDEX IF NOT EXISTS idx_retry_jobs_ready
                    ON retry_jobs(status,next_attempt_at,created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_retry_jobs_kind_dedupe
                    ON retry_jobs(kind,dedupe_key) WHERE dedupe_key<>'';
                """
            )
            # Additive migration for databases created before analysis results
            # were bound to an immutable Telegram record version.  Keep the
            # v1 marker because this remains backwards-compatible on disk.
            analysis_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(analysis_results)"
                ).fetchall()
            }
            if "record_version" not in analysis_columns:
                connection.execute(
                    "ALTER TABLE analysis_results "
                    "ADD COLUMN record_version INTEGER NOT NULL DEFAULT 1"
                )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_analysis_results_message_version
                   ON analysis_results(message_row_id,record_version,created_at DESC)"""
            )
            retry_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(retry_jobs)").fetchall()
            }
            for name, definition in (
                ("event_id", "TEXT NOT NULL DEFAULT ''"),
                ("message_row_id", "INTEGER"),
                ("record_version", "INTEGER"),
            ):
                if name not in retry_columns:
                    connection.execute(
                        f"ALTER TABLE retry_jobs ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_retry_jobs_event_status
                   ON retry_jobs(event_id,status,kind)"""
            )
            now = utc_iso(self._now())
            existing = connection.execute(
                "SELECT value_json FROM system_state WHERE key='schema_version'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO system_state(key,value_json,updated_at) VALUES('schema_version',?,?)",
                    (canonical_json(SCHEMA_VERSION), now),
                )
            else:
                try:
                    value = json.loads(existing["value_json"])
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Telegram repository schema marker is invalid") from exc
                if value != SCHEMA_VERSION:
                    raise RuntimeError("Telegram repository schema version is unsupported")
            connection.execute("PRAGMA user_version=1")

    @property
    def schema_version(self) -> str:
        return SCHEMA_VERSION

    def synchronize_chat_policies(self, policies: Iterable[Any]) -> dict[str, int]:
        """Make the durable enabled flags mirror the current private config."""

        rows: list[tuple[int, int, str, str, str, int]] = []
        seen: set[int] = set()
        for policy in policies:
            def value(name: str, default: Any) -> Any:
                return (
                    policy.get(name, default)
                    if isinstance(policy, Mapping)
                    else getattr(policy, name, default)
                )

            chat_id = value("chat_id", 0)
            enabled = value("enabled", False)
            trust = value("trust_score", 50)
            if type(chat_id) is not int or chat_id == 0 or chat_id in seen:
                raise ValueError("chat policies contain an invalid or duplicate chat_id")
            if type(enabled) is not bool or type(trust) is not int:
                raise ValueError("chat policy types are invalid")
            seen.add(chat_id)
            rows.append(
                (
                    chat_id,
                    int(enabled),
                    str(value("name", "") or "")[:160],
                    str(value("category", "general") or "general")[:80],
                    str(value("priority", "medium") or "medium")[:20],
                    max(0, min(100, trust)),
                )
            )
        now = utc_iso(self._now())
        disabled = 0
        with self.transaction() as connection:
            for chat_id, enabled, name, category, priority, trust in rows:
                connection.execute(
                    """INSERT INTO telegram_chats(
                           chat_id,enabled,name,title,chat_type,category,priority,
                           trust_score,created_at,updated_at
                       ) VALUES(?,?,?,'','',?,?,?,?,?)
                       ON CONFLICT(chat_id) DO UPDATE SET
                           enabled=excluded.enabled,name=excluded.name,
                           category=excluded.category,priority=excluded.priority,
                           trust_score=excluded.trust_score,updated_at=excluded.updated_at""",
                    (
                        chat_id,
                        enabled,
                        name,
                        category,
                        priority,
                        trust,
                        now,
                        now,
                    ),
                )
            if rows:
                placeholders = ",".join("?" for _ in rows)
                cursor = connection.execute(
                    f"""UPDATE telegram_chats SET enabled=0,updated_at=?
                       WHERE enabled<>0 AND chat_id NOT IN ({placeholders})""",
                    (now, *(item[0] for item in rows)),
                )
            else:
                cursor = connection.execute(
                    "UPDATE telegram_chats SET enabled=0,updated_at=? WHERE enabled<>0",
                    (now,),
                )
            disabled = int(cursor.rowcount)
        return {"configured": len(rows), "disabled_missing": disabled}

    def upsert_chat(
        self,
        chat_id: int,
        *,
        enabled: bool = True,
        name: str = "",
        title: str = "",
        chat_type: str = "",
        category: str = "",
        priority: str = "medium",
        trust_score: int = 50,
    ) -> dict[str, Any]:
        if type(chat_id) is not int or chat_id == 0:
            raise ValueError("chat_id must be a non-zero integer")
        if type(enabled) is not bool or type(trust_score) is not int or not 0 <= trust_score <= 100:
            raise ValueError("chat policy values are invalid")
        clean_priority = str(priority or "medium").strip().lower()
        if clean_priority not in {"low", "medium", "high"}:
            raise ValueError("priority must be low, medium, or high")
        now = utc_iso(self._now())
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO telegram_chats(
                       chat_id,enabled,name,title,chat_type,category,priority,trust_score,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                       enabled=excluded.enabled,name=excluded.name,title=excluded.title,
                       chat_type=excluded.chat_type,category=excluded.category,
                       priority=excluded.priority,trust_score=excluded.trust_score,
                       updated_at=excluded.updated_at""",
                (
                    chat_id,
                    int(enabled),
                    str(name or "")[:200],
                    str(title or "")[:200],
                    str(chat_type or "")[:40],
                    str(category or "")[:80],
                    clean_priority,
                    trust_score,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM telegram_chats WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return _row_dict(row) or {}

    @staticmethod
    def _message_record(message: SourceMessage) -> dict[str, Any]:
        if type(message) is not SourceMessage:
            raise TypeError("message must be a SourceMessage")
        forward_origin = asdict(message.forward_origin) if message.forward_origin else {}
        return {
            "source": message.source,
            "chat_id": message.chat_id,
            "chat_title": message.chat_title,
            "chat_type": message.chat_type,
            "message_id": message.message_id,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "timestamp": utc_iso(message.timestamp),
            "edit_timestamp": utc_iso(message.edit_timestamp, allow_none=True),
            "text": message.text,
            "caption": message.caption,
            "urls_json": canonical_json(message.urls),
            "entities_json": canonical_json([asdict(item) for item in message.entities]),
            "reply_to_message_id": message.reply_to_message_id,
            "forward_origin_json": canonical_json(forward_origin),
            "media_type": message.media_type,
            "media_file_id": message.media_file_id,
            "telegram_link": message.telegram_link,
            "raw_language": message.raw_language,
            "is_edited": int(message.is_edited),
            "is_forwarded": int(message.is_forwarded),
            "ingested_at": utc_iso(message.ingested_at),
        }

    @staticmethod
    def _message_semantic_hash(record: Mapping[str, Any]) -> str:
        semantic = {
            key: value
            for key, value in record.items()
            if key not in {"ingested_at"}
        }
        return hashlib.sha256(canonical_json(semantic).encode("utf-8")).hexdigest()

    @staticmethod
    def _revision_snapshot(row: sqlite3.Row) -> tuple[str, str]:
        snapshot = {
            key: row[key]
            for key in row.keys()
            if key not in {"id", "created_at", "updated_at"}
        }
        body = canonical_json(snapshot)
        return body, hashlib.sha256(body.encode("utf-8")).hexdigest()

    def store_message(
        self,
        message: SourceMessage,
        *,
        dedup_result: Any = None,
    ) -> dict[str, Any]:
        record = self._message_record(message)
        content_hash = str(getattr(dedup_result, "content_hash", "") or content_fingerprint(message))
        hash_value = getattr(dedup_result, "simhash", None)
        if type(hash_value) is not int:
            hash_value = simhash64(normalize_text(message.content_text))
        duplicate_of = getattr(dedup_result, "duplicate_of", None)
        duplicate_score = float(getattr(dedup_result, "duplicate_score", 0.0) or 0.0)
        duplicate_reason = str(getattr(dedup_result, "duplicate_reason", "") or "")[:80]
        if not 0 <= duplicate_score <= 1:
            raise ValueError("duplicate score must be between 0 and 1")
        record.update(
            {
                "content_hash": content_hash,
                "simhash_hex": f"{hash_value:016x}",
                "duplicate_of_json": canonical_json(list(duplicate_of) if duplicate_of else []),
                "duplicate_score": duplicate_score,
                "duplicate_reason": duplicate_reason,
            }
        )
        semantic_hash = self._message_semantic_hash(record)
        now = utc_iso(self._now())
        with self.transaction(admit_growth=True) as connection:
            existing = connection.execute(
                "SELECT * FROM telegram_messages WHERE source=? AND chat_id=? AND message_id=?",
                message.identity_key,
            ).fetchone()
            if existing is None:
                columns = tuple(record)
                connection.execute(
                    f"INSERT INTO telegram_messages({','.join(columns)},created_at,updated_at) "
                    f"VALUES({','.join('?' for _ in columns)},?,?)",
                    (*record.values(), now, now),
                )
                row = connection.execute(
                    "SELECT * FROM telegram_messages WHERE source=? AND chat_id=? AND message_id=?",
                    message.identity_key,
                ).fetchone()
                result = _row_dict(row) or {}
                result.update({"created": True, "updated": False, "idempotent": False})
                return result
            existing_record = {
                key: existing[key]
                for key in record
            }
            if self._message_semantic_hash(existing_record) == semantic_hash:
                result = _row_dict(existing) or {}
                result.update({"created": False, "updated": False, "idempotent": True})
                return result
            snapshot, snapshot_sha = self._revision_snapshot(existing)
            next_version = int(existing["record_version"]) + 1
            connection.execute(
                """INSERT INTO telegram_message_revisions(
                       message_row_id,record_version,snapshot_json,snapshot_sha256,created_at
                   ) VALUES(?,?,?,?,?)""",
                (existing["id"], existing["record_version"], snapshot, snapshot_sha, now),
            )
            assignments = ",".join(f"{column}=?" for column in record)
            connection.execute(
                f"UPDATE telegram_messages SET {assignments},record_version=?,updated_at=? WHERE id=?",
                (*record.values(), next_version, now, existing["id"]),
            )
            row = connection.execute(
                "SELECT * FROM telegram_messages WHERE id=?", (existing["id"],)
            ).fetchone()
            event_rows = connection.execute(
                "SELECT event_id FROM event_messages WHERE message_row_id=?",
                (existing["id"],),
            ).fetchall()
            affected_event_ids = {str(item["event_id"]) for item in event_rows}
            if affected_event_ids:
                connection.execute(
                    "UPDATE event_messages SET relation='retracted' WHERE message_row_id=?",
                    (existing["id"],),
                )
                cancelled = self._recompute_events_after_retraction(
                    connection,
                    affected_event_ids,
                    when=now,
                    cancellation_error="cancelled_source_edited",
                )
            else:
                cancelled = 0
            result = _row_dict(row) or {}
            result.update(
                {
                    "created": False,
                    "updated": True,
                    "idempotent": False,
                    "retracted_event_ids": tuple(sorted(affected_event_ids)),
                    "cancelled_alert_jobs": cancelled,
                }
            )
            return result

    insert_message = store_message
    apply_edit = store_message

    def store_message_and_enqueue(
        self,
        message: SourceMessage,
        *,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        dedupe_key_prefix: str,
        max_attempts: int = 5,
        dedup_result: Any = None,
    ) -> dict[str, Any]:
        """Atomically persist one record version and its durable worker job."""

        clean_kind = _safe_identifier(kind, field="kind", maximum=80)
        clean_prefix = str(dedupe_key_prefix or "").strip()[:180]
        if not clean_prefix:
            raise ValueError("dedupe_key_prefix is required")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        base_payload = _json_object(payload or {}, field="payload")
        record = self._message_record(message)
        content_hash = str(
            getattr(dedup_result, "content_hash", "") or content_fingerprint(message)
        )
        hash_value = getattr(dedup_result, "simhash", None)
        if type(hash_value) is not int:
            hash_value = simhash64(normalize_text(message.content_text))
        duplicate_of = getattr(dedup_result, "duplicate_of", None)
        duplicate_score = float(
            getattr(dedup_result, "duplicate_score", 0.0) or 0.0
        )
        duplicate_reason = str(
            getattr(dedup_result, "duplicate_reason", "") or ""
        )[:80]
        if not 0 <= duplicate_score <= 1:
            raise ValueError("duplicate score must be between 0 and 1")
        record.update(
            {
                "content_hash": content_hash,
                "simhash_hex": f"{hash_value:016x}",
                "duplicate_of_json": canonical_json(
                    list(duplicate_of) if duplicate_of else []
                ),
                "duplicate_score": duplicate_score,
                "duplicate_reason": duplicate_reason,
            }
        )
        semantic_hash = self._message_semantic_hash(record)
        now = utc_iso(self._now())
        with self.transaction(admit_growth=True) as connection:
            existing = connection.execute(
                "SELECT * FROM telegram_messages WHERE source=? AND chat_id=? AND message_id=?",
                message.identity_key,
            ).fetchone()
            if existing is None:
                columns = tuple(record)
                connection.execute(
                    f"INSERT INTO telegram_messages({','.join(columns)},created_at,updated_at) "
                    f"VALUES({','.join('?' for _ in columns)},?,?)",
                    (*record.values(), now, now),
                )
                row = connection.execute(
                    "SELECT * FROM telegram_messages WHERE source=? AND chat_id=? AND message_id=?",
                    message.identity_key,
                ).fetchone()
                message_result = _row_dict(row) or {}
                message_result.update(
                    {"created": True, "updated": False, "idempotent": False}
                )
            else:
                existing_record = {key: existing[key] for key in record}
                if self._message_semantic_hash(existing_record) == semantic_hash:
                    row = existing
                    message_result = _row_dict(existing) or {}
                    message_result.update(
                        {"created": False, "updated": False, "idempotent": True}
                    )
                else:
                    snapshot, snapshot_sha = self._revision_snapshot(existing)
                    next_version = int(existing["record_version"]) + 1
                    connection.execute(
                        """INSERT INTO telegram_message_revisions(
                               message_row_id,record_version,snapshot_json,
                               snapshot_sha256,created_at
                           ) VALUES(?,?,?,?,?)""",
                        (
                            existing["id"],
                            existing["record_version"],
                            snapshot,
                            snapshot_sha,
                            now,
                        ),
                    )
                    assignments = ",".join(f"{column}=?" for column in record)
                    connection.execute(
                        f"UPDATE telegram_messages SET {assignments},record_version=?,updated_at=? WHERE id=?",
                        (*record.values(), next_version, now, existing["id"]),
                    )
                    row = connection.execute(
                        "SELECT * FROM telegram_messages WHERE id=?", (existing["id"],)
                    ).fetchone()
                    event_rows = connection.execute(
                        "SELECT event_id FROM event_messages WHERE message_row_id=?",
                        (existing["id"],),
                    ).fetchall()
                    affected_event_ids = {
                        str(item["event_id"]) for item in event_rows
                    }
                    if affected_event_ids:
                        connection.execute(
                            """UPDATE event_messages SET relation='retracted'
                               WHERE message_row_id=?""",
                            (existing["id"],),
                        )
                        cancelled = self._recompute_events_after_retraction(
                            connection,
                            affected_event_ids,
                            when=now,
                            cancellation_error="cancelled_source_edited",
                        )
                    else:
                        cancelled = 0
                    message_result = _row_dict(row) or {}
                    message_result.update(
                        {
                            "created": False,
                            "updated": True,
                            "idempotent": False,
                            "retracted_event_ids": tuple(sorted(affected_event_ids)),
                            "cancelled_alert_jobs": cancelled,
                        }
                    )
            assert row is not None
            final_payload = {
                **base_payload,
                "source": str(row["source"]),
                "chat_id": int(row["chat_id"]),
                "message_id": int(row["message_id"]),
                "message_row_id": int(row["id"]),
                "record_version": int(row["record_version"]),
            }
            body = canonical_json(final_payload)
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            dedupe_key = f"{clean_prefix}:v{row['record_version']}"[:200]
            job_row = connection.execute(
                "SELECT * FROM retry_jobs WHERE kind=? AND dedupe_key=?",
                (clean_kind, dedupe_key),
            ).fetchone()
            if job_row is not None:
                if job_row["payload_sha256"] != digest:
                    raise ValueError("job dedupe key already binds different payload")
                job_result = _row_dict(job_row) or {}
                job_result["idempotent"] = True
            else:
                job_id = "job_" + uuid.uuid4().hex
                connection.execute(
                    """INSERT INTO retry_jobs(
                           id,kind,dedupe_key,event_id,message_row_id,record_version,
                           payload_json,payload_sha256,status,
                           attempt_count,max_attempts,next_attempt_at,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?, 'PENDING',0,?,?,?,?)""",
                    (
                        job_id,
                        clean_kind,
                        dedupe_key,
                        "",
                        int(row["id"]),
                        int(row["record_version"]),
                        body,
                        digest,
                        max_attempts,
                        now,
                        now,
                        now,
                    ),
                )
                job_row = connection.execute(
                    "SELECT * FROM retry_jobs WHERE id=?", (job_id,)
                ).fetchone()
                job_result = _row_dict(job_row) or {}
                job_result["idempotent"] = False
        return {"message": message_result, "job": job_result}

    def get_message(self, source: str, chat_id: int, message_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM telegram_messages WHERE source=? AND chat_id=? AND message_id=?",
                (source, chat_id, message_id),
            ).fetchone()
        return _row_dict(row)

    def message_version_is_current(
        self,
        message_row_id: int,
        record_version: int,
    ) -> bool:
        if type(message_row_id) is not int or message_row_id < 1:
            raise ValueError("message_row_id must be a positive integer")
        if type(record_version) is not int or record_version < 1:
            raise ValueError("record_version must be a positive integer")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT 1 FROM telegram_messages
                   WHERE id=? AND record_version=? AND is_deleted=0""",
                (message_row_id, record_version),
            ).fetchone()
        return row is not None

    def message_revisions(self, message_row_id: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM telegram_message_revisions WHERE message_row_id=? ORDER BY record_version",
                (message_row_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _recompute_events_after_retraction(
        self,
        connection: sqlite3.Connection,
        event_ids: Iterable[str],
        *,
        when: str,
        cancellation_error: str,
    ) -> int:
        """Rebuild safety fields after a delete/edit retracts source evidence.

        The caller owns the transaction.  Historical links and analyses remain
        auditable, while only active message links and the current immutable
        record version can contribute to the current event state.
        """

        clean_event_ids = tuple(sorted({str(item) for item in event_ids if str(item)}))
        if not clean_event_ids:
            return 0
        policy_rows = connection.execute(
            "SELECT chat_id,enabled,trust_score FROM telegram_chats"
        ).fetchall()
        origin_policies = {
            int(item["chat_id"]): (
                bool(item["enabled"]),
                max(0, min(100, int(item["trust_score"]))),
            )
            for item in policy_rows
        }
        for event_id in clean_event_ids:
            active_rows = connection.execute(
                """SELECT m.id,m.record_version,m.source,m.chat_id,m.chat_title,
                          m.timestamp,m.forward_origin_json,
                          COALESCE(c.trust_score,50) AS trust_score
                   FROM event_messages em
                   JOIN telegram_messages m ON m.id=em.message_row_id
                   LEFT JOIN telegram_chats c ON c.chat_id=m.chat_id
                   WHERE em.event_id=? AND em.relation<>'retracted'
                         AND m.is_deleted=0
                   ORDER BY m.timestamp,m.id LIMIT ?""",
                (event_id, MAX_CLUSTER_EVENT_MESSAGES + 2),
            ).fetchall()
            if len(active_rows) > MAX_CLUSTER_EVENT_MESSAGES + 1:
                raise RuntimeError("event retraction exceeds the safe member bound")
            trust_by_source: dict[str, tuple[int, str]] = {}
            for active in active_rows:
                try:
                    origin = json.loads(active["forward_origin_json"] or "{}")
                except json.JSONDecodeError:
                    origin = {}
                source_key = source_key_for_values(
                    source=active["source"],
                    chat_id=active["chat_id"],
                    forward_origin=origin,
                )
                trust = max(0, min(100, int(active["trust_score"])))
                if origin:
                    origin_chat_id = origin.get("chat_id")
                    origin_policy = (
                        origin_policies.get(origin_chat_id)
                        if type(origin_chat_id) is int
                        else None
                    )
                    trust = (
                        min(trust, origin_policy[1])
                        if origin_policy is not None and origin_policy[0]
                        else min(trust, 30)
                    )
                label = str(active["chat_title"] or active["chat_id"])[:200]
                previous = trust_by_source.get(source_key)
                if previous is None or trust > previous[0]:
                    trust_by_source[source_key] = (trust, label)
            highest = max(
                trust_by_source.values(),
                key=lambda item: (item[0], item[1]),
                default=(0, ""),
            )
            importance = 0
            confidence = 0
            disputed = False
            if active_rows:
                analyses = connection.execute(
                    """SELECT ar.importance_score,ar.confidence_score,ar.result_json
                        FROM analysis_results ar
                        JOIN telegram_messages m ON m.id=ar.message_row_id
                        JOIN event_messages em
                          ON em.event_id=ar.event_id
                         AND em.message_row_id=ar.message_row_id
                        WHERE ar.event_id=? AND em.relation<>'retracted'
                              AND m.is_deleted=0
                              AND ar.record_version=m.record_version""",
                    (event_id,),
                ).fetchall()
                for analysis in analyses:
                    importance = max(importance, int(analysis["importance_score"]))
                    confidence = max(confidence, int(analysis["confidence_score"]))
                    try:
                        result = json.loads(analysis["result_json"] or "{}")
                    except json.JSONDecodeError:
                        result = {}
                    if (
                        str(result.get("verification_status") or "").lower()
                        == "false_or_misleading"
                    ):
                        disputed = True
            status = (
                "resolved"
                if not active_rows
                else "disputed"
                if disputed
                else "developing"
                if len(trust_by_source) >= 2
                else "new"
            )
            values: list[Any] = [
                len(active_rows),
                len(trust_by_source),
                highest[1],
                highest[0],
                importance,
                confidence,
                status,
            ]
            time_clause = ""
            if active_rows:
                time_clause = ",first_seen_at=?,last_seen_at=?"
                values.extend((active_rows[0]["timestamp"], active_rows[-1]["timestamp"]))
            values.extend((when, event_id))
            connection.execute(
                f"""UPDATE events SET message_count=?,source_count=?,
                           highest_trust_source=?,highest_trust_score=?,
                           importance_score=?,confidence_score=?,status=?
                           {time_clause},updated_at=? WHERE event_id=?""",
                values,
            )
            snapshot_key = "alert_snapshot:" + hashlib.sha256(
                event_id.encode("utf-8")
            ).hexdigest()
            connection.execute("DELETE FROM system_state WHERE key=?", (snapshot_key,))

        cancelled = 0
        for offset in range(0, len(clean_event_ids), 400):
            batch = clean_event_ids[offset : offset + 400]
            placeholders = ",".join("?" for _ in batch)
            cursor = connection.execute(
                f"""UPDATE retry_jobs SET status='COMPLETED',completed_at=?,
                           error_code=?,updated_at=?
                   WHERE kind='send_alert' AND status IN ('PENDING','RETRY')
                         AND event_id IN ({placeholders})""",
                (when, cancellation_error[:120], when, *batch),
            )
            cancelled += int(cursor.rowcount)
        return cancelled

    def mark_deleted(
        self,
        event_or_chat_id: DeletedMessageEvent | int,
        message_ids: Iterable[int] | None = None,
        *,
        source: str = "telegram",
        deleted_at: Any = None,
        is_permanent: bool = True,
        from_cache: bool = False,
    ) -> dict[str, Any]:
        if isinstance(event_or_chat_id, DeletedMessageEvent):
            event = event_or_chat_id
            chat_id = event.chat_id
            ids = event.message_ids
            source = event.source
            timestamp = event.received_at
            is_permanent = event.is_permanent
            from_cache = event.from_cache
        else:
            chat_id = event_or_chat_id
            ids = tuple(message_ids or ())
            timestamp = deleted_at or self._now()
        if type(chat_id) is not int or not ids or any(type(item) is not int for item in ids):
            raise ValueError("delete identity is invalid")
        when = utc_iso(timestamp, field="deleted_at")
        updated = 0
        missing: list[int] = []
        affected_event_ids: set[str] = set()
        cancelled_alert_jobs = 0
        with self.transaction() as connection:
            message_row_ids: list[int] = []
            for message_id in ids:
                row = connection.execute(
                    "SELECT id,is_deleted FROM telegram_messages WHERE source=? AND chat_id=? AND message_id=?",
                    (source, chat_id, message_id),
                ).fetchone()
                if row is None:
                    missing.append(message_id)
                    continue
                message_row_ids.append(int(row["id"]))
                event_rows = connection.execute(
                    "SELECT event_id FROM event_messages WHERE message_row_id=?",
                    (row["id"],),
                ).fetchall()
                affected_event_ids.update(str(item["event_id"]) for item in event_rows)
                if not bool(row["is_deleted"]):
                    updated += 1
                connection.execute(
                    """UPDATE telegram_messages SET is_deleted=1,deleted_at=?,
                           delete_is_permanent=?,delete_from_cache=?,updated_at=? WHERE id=?""",
                    (when, int(is_permanent), int(from_cache), when, row["id"]),
                )
            if message_row_ids:
                placeholders = ",".join("?" for _ in message_row_ids)
                connection.execute(
                    f"""UPDATE event_messages SET relation='retracted'
                        WHERE message_row_id IN ({placeholders})""",
                    message_row_ids,
                )

            # A retracted message remains in the immutable audit chain, but it
            # must no longer count as current event evidence.  Recompute the
            # safety-relevant aggregate fields from active linked rows and
            # deliberately require fresh processing before an event can become
            # confirmed again.
            policy_rows = connection.execute(
                "SELECT chat_id,enabled,trust_score FROM telegram_chats"
            ).fetchall()
            origin_policies = {
                int(item["chat_id"]): (
                    bool(item["enabled"]),
                    max(0, min(100, int(item["trust_score"]))),
                )
                for item in policy_rows
            }
            for event_id in sorted(affected_event_ids):
                active_rows = connection.execute(
                    """SELECT m.id,m.source,m.chat_id,m.chat_title,m.timestamp,
                              m.forward_origin_json,
                              COALESCE(c.trust_score,50) AS trust_score
                       FROM event_messages em
                       JOIN telegram_messages m ON m.id=em.message_row_id
                       LEFT JOIN telegram_chats c ON c.chat_id=m.chat_id
                       WHERE em.event_id=? AND em.relation<>'retracted'
                             AND m.is_deleted=0
                       ORDER BY m.timestamp,m.id LIMIT ?""",
                    (event_id, MAX_CLUSTER_EVENT_MESSAGES + 2),
                ).fetchall()
                if len(active_rows) > MAX_CLUSTER_EVENT_MESSAGES + 1:
                    raise RuntimeError("event deletion exceeds the safe member bound")
                trust_by_source: dict[str, tuple[int, str]] = {}
                for active in active_rows:
                    try:
                        origin = json.loads(active["forward_origin_json"] or "{}")
                    except json.JSONDecodeError:
                        origin = {}
                    source_key = source_key_for_values(
                        source=active["source"],
                        chat_id=active["chat_id"],
                        forward_origin=origin,
                    )
                    trust = max(0, min(100, int(active["trust_score"])))
                    if origin:
                        origin_chat_id = origin.get("chat_id")
                        origin_policy = (
                            origin_policies.get(origin_chat_id)
                            if type(origin_chat_id) is int
                            else None
                        )
                        trust = (
                            min(trust, origin_policy[1])
                            if origin_policy is not None and origin_policy[0]
                            else min(trust, 30)
                        )
                    label = str(active["chat_title"] or active["chat_id"])[:200]
                    previous = trust_by_source.get(source_key)
                    if previous is None or trust > previous[0]:
                        trust_by_source[source_key] = (trust, label)
                highest = max(
                    trust_by_source.values(),
                    key=lambda item: (item[0], item[1]),
                    default=(0, ""),
                )
                importance = 0
                confidence = 0
                disputed = False
                if active_rows:
                    analyses = connection.execute(
                        """SELECT ar.importance_score,ar.confidence_score,ar.result_json
                            FROM analysis_results ar
                            JOIN telegram_messages m ON m.id=ar.message_row_id
                            JOIN event_messages em
                              ON em.event_id=ar.event_id
                             AND em.message_row_id=ar.message_row_id
                            WHERE ar.event_id=? AND em.relation<>'retracted'
                                  AND m.is_deleted=0
                                  AND ar.record_version=m.record_version""",
                        (event_id,),
                    ).fetchall()
                    for analysis in analyses:
                        importance = max(importance, int(analysis["importance_score"]))
                        confidence = max(confidence, int(analysis["confidence_score"]))
                        try:
                            result = json.loads(analysis["result_json"] or "{}")
                        except json.JSONDecodeError:
                            result = {}
                        if str(result.get("verification_status") or "").lower() == "false_or_misleading":
                            disputed = True
                if not active_rows:
                    status = "resolved"
                elif disputed:
                    status = "disputed"
                elif len(trust_by_source) >= 2:
                    status = "developing"
                else:
                    status = "new"
                values: list[Any] = [
                    len(active_rows),
                    len(trust_by_source),
                    highest[1],
                    highest[0],
                    importance,
                    confidence,
                    status,
                ]
                time_clause = ""
                if active_rows:
                    time_clause = ",first_seen_at=?,last_seen_at=?"
                    values.extend((active_rows[0]["timestamp"], active_rows[-1]["timestamp"]))
                values.extend((when, event_id))
                connection.execute(
                    f"""UPDATE events SET message_count=?,source_count=?,
                               highest_trust_source=?,highest_trust_score=?,
                               importance_score=?,confidence_score=?,status=?
                               {time_clause},updated_at=? WHERE event_id=?""",
                    values,
                )
                snapshot_key = "alert_snapshot:" + hashlib.sha256(
                    event_id.encode("utf-8")
                ).hexdigest()
                connection.execute("DELETE FROM system_state WHERE key=?", (snapshot_key,))

            if affected_event_ids:
                event_ids = tuple(sorted(affected_event_ids))
                for offset in range(0, len(event_ids), 400):
                    batch = event_ids[offset : offset + 400]
                    placeholders = ",".join("?" for _ in batch)
                    cursor = connection.execute(
                        f"""UPDATE retry_jobs SET status='COMPLETED',completed_at=?,
                                   error_code='cancelled_source_deleted',updated_at=?
                           WHERE kind='send_alert' AND status IN ('PENDING','RETRY')
                                 AND event_id IN ({placeholders})""",
                        (when, when, *batch),
                    )
                    cancelled_alert_jobs += int(cursor.rowcount)
        return {
            "updated_count": updated,
            "missing_message_ids": tuple(missing),
            "deleted_at": when,
            "affected_event_ids": tuple(sorted(affected_event_ids)),
            "cancelled_alert_jobs": cancelled_alert_jobs,
        }

    def save_event(self, event: Any) -> dict[str, Any]:
        def value(name: str, default: Any = None) -> Any:
            return getattr(event, name, default) if not isinstance(event, Mapping) else event.get(name, default)

        event_id = _safe_identifier(value("event_id"), field="event_id", maximum=160)
        status = str(value("status", "new") or "new").strip().lower()
        if status not in {"new", "developing", "confirmed", "disputed", "resolved"}:
            raise ValueError("event status is invalid")
        tickers = tuple(sorted({str(item).strip().upper() for item in value("tickers", ()) if str(item).strip()}))
        keywords = tuple(sorted({str(item).strip() for item in value("keywords", ()) if str(item).strip()}))
        now = utc_iso(self._now())
        first = utc_iso(value("first_seen_at"), field="first_seen_at")
        last = utc_iso(value("last_seen_at"), field="last_seen_at")
        record = (
            event_id,
            first,
            last,
            str(value("title", "") or "")[:500],
            str(value("category", "") or "")[:80],
            canonical_json(tickers),
            canonical_json(keywords),
            tickers[0] if tickers else "",
            int(value("message_count", len(value("message_keys", ()))) or 0),
            int(value("source_count", len(value("source_keys", ()))) or 0),
            str(value("highest_trust_source", "") or "")[:200],
            int(value("highest_trust_score", 0) or 0),
            int(value("importance_score", 0) or 0),
            int(value("confidence_score", 0) or 0),
            status,
            f"{int(value('centroid_simhash', 0) or 0):016x}",
            now,
            now,
        )
        with self.transaction() as connection:
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
            connection.execute("DELETE FROM event_tickers WHERE event_id=?", (event_id,))
            connection.executemany(
                "INSERT INTO event_tickers(event_id,ticker) VALUES(?,?)",
                ((event_id, ticker) for ticker in tickers),
            )
            row = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return _row_dict(row) or {}

    def link_event_message(
        self,
        event_id: str,
        message_row_id: int,
        *,
        similarity: float = 1.0,
        relation: str = "member",
    ) -> bool:
        if not 0 <= float(similarity) <= 1:
            raise ValueError("similarity must be between 0 and 1")
        now = utc_iso(self._now())
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO event_messages(
                       event_id,message_row_id,similarity,relation,added_at
                   ) VALUES(?,?,?,?,?)""",
                (event_id, message_row_id, float(similarity), str(relation or "member")[:40], now),
            )
            return cursor.rowcount == 1

    def enqueue_job(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        dedupe_key: str = "",
        available_at: Any = None,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        clean_kind = _safe_identifier(kind, field="kind", maximum=80)
        clean_dedupe = str(dedupe_key or "").strip()[:200]
        if type(max_attempts) is not int or not 1 <= max_attempts <= 100:
            raise ValueError("max_attempts must be between 1 and 100")
        clean_payload = _json_object(payload, field="payload")
        body = canonical_json(clean_payload)
        raw_event_id = clean_payload.get("event_id")
        event_id = (
            str(raw_event_id).strip()[:160]
            if type(raw_event_id) is str
            and raw_event_id.strip()
            and not any(character in raw_event_id for character in "\r\n\x00")
            else ""
        )
        raw_message_row_id = clean_payload.get("message_row_id")
        message_row_id = (
            raw_message_row_id
            if type(raw_message_row_id) is int and raw_message_row_id > 0
            else None
        )
        raw_record_version = clean_payload.get("record_version")
        record_version = (
            raw_record_version
            if type(raw_record_version) is int and raw_record_version > 0
            else None
        )
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        now_dt = self._now()
        now = utc_iso(now_dt)
        ready = utc_iso(available_at or now_dt, field="available_at")
        job_id = "job_" + uuid.uuid4().hex
        with self.transaction(admit_growth=True) as connection:
            if clean_dedupe:
                existing = connection.execute(
                    "SELECT * FROM retry_jobs WHERE kind=? AND dedupe_key=?",
                    (clean_kind, clean_dedupe),
                ).fetchone()
                if existing is not None:
                    if existing["payload_sha256"] != digest:
                        raise ValueError("job dedupe key already binds different payload")
                    result = _row_dict(existing) or {}
                    result["idempotent"] = True
                    return result
            connection.execute(
                """INSERT INTO retry_jobs(
                       id,kind,dedupe_key,event_id,message_row_id,record_version,
                       payload_json,payload_sha256,status,
                       attempt_count,max_attempts,next_attempt_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,'PENDING',0,?,?,?,?)""",
                (
                    job_id,
                    clean_kind,
                    clean_dedupe,
                    event_id,
                    message_row_id,
                    record_version,
                    body,
                    digest,
                    max_attempts,
                    ready,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM retry_jobs WHERE id=?", (job_id,)).fetchone()
        result = _row_dict(row) or {}
        result["idempotent"] = False
        return result

    @staticmethod
    def _recover_expired(connection: sqlite3.Connection, now: str) -> None:
        connection.execute(
            """UPDATE retry_jobs SET
                   status=CASE WHEN attempt_count>=max_attempts THEN 'DEAD' ELSE 'RETRY' END,
                   next_attempt_at=CASE WHEN attempt_count>=max_attempts THEN next_attempt_at ELSE ? END,
                   dead_at=CASE WHEN attempt_count>=max_attempts THEN ? ELSE dead_at END,
                   lease_owner='',lease_token='',lease_expires_at='',
                   error_code='lease_expired',updated_at=?
               WHERE status='CLAIMED' AND lease_expires_at<>'' AND lease_expires_at<=?""",
            (now, now, now, now),
        )

    def claim_job(
        self,
        worker: str,
        *,
        kinds: Iterable[str] = (),
        job_ids: Iterable[str] | None = None,
        dedupe_prefix: str | None = None,
        lease_seconds: int = 60,
        now: Any = None,
    ) -> dict[str, Any] | None:
        clean_worker = _safe_identifier(worker, field="worker", maximum=120)
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        timestamp = _utc_datetime(now or self._now(), field="claim time")
        assert timestamp is not None
        now_text = utc_iso(timestamp)
        kind_rows = tuple(dict.fromkeys(_safe_identifier(item, field="kind", maximum=80) for item in kinds))
        id_rows = (
            None
            if job_ids is None
            else tuple(
                dict.fromkeys(
                    _safe_identifier(item, field="job_id", maximum=120)
                    for item in job_ids
                )
            )
        )
        if id_rows is not None and len(id_rows) > 1_000:
            raise ValueError("job_ids may contain at most 1000 values")
        if id_rows == ():
            return None
        clean_prefix = (
            None
            if dedupe_prefix is None
            else _safe_identifier(
                dedupe_prefix,
                field="dedupe_prefix",
                maximum=200,
            )
        )
        with self.transaction() as connection:
            self._recover_expired(connection, now_text)
            clause = ""
            parameters: list[Any] = [now_text]
            if kind_rows:
                clause = f" AND kind IN ({','.join('?' for _ in kind_rows)})"
                parameters.extend(kind_rows)
            if id_rows is not None:
                clause += f" AND id IN ({','.join('?' for _ in id_rows)})"
                parameters.extend(id_rows)
            if clean_prefix is not None:
                clause += " AND substr(dedupe_key,1,?)=?"
                parameters.extend((len(clean_prefix), clean_prefix))
            row = connection.execute(
                f"""SELECT * FROM retry_jobs
                    WHERE status IN ('PENDING','RETRY') AND next_attempt_at<=?{clause}
                    ORDER BY next_attempt_at,created_at,id LIMIT 1""",
                parameters,
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            lease_until = utc_iso(timestamp + timedelta(seconds=lease_seconds))
            cursor = connection.execute(
                """UPDATE retry_jobs SET status='CLAIMED',attempt_count=attempt_count+1,
                       lease_owner=?,lease_token=?,lease_expires_at=?,updated_at=?
                   WHERE id=? AND status IN ('PENDING','RETRY')""",
                (clean_worker, token, lease_until, now_text, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            claimed = connection.execute("SELECT * FROM retry_jobs WHERE id=?", (row["id"],)).fetchone()
        return _row_dict(claimed)

    def renew_job_lease(
        self,
        job_id: str,
        lease_token: str,
        *,
        lease_seconds: int,
        now: Any = None,
    ) -> bool:
        """Extend an owned, unexpired lease without changing attempt state."""

        clean_job = _safe_identifier(job_id, field="job_id", maximum=120)
        clean_token = _safe_identifier(
            lease_token,
            field="lease_token",
            maximum=120,
        )
        if type(lease_seconds) is not int or not 1 <= lease_seconds <= 3_600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        timestamp = _utc_datetime(now or self._now(), field="lease renewal time")
        assert timestamp is not None
        now_text = utc_iso(timestamp)
        expires = utc_iso(timestamp + timedelta(seconds=lease_seconds))
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE retry_jobs SET lease_expires_at=?,updated_at=?
                   WHERE id=? AND status='CLAIMED' AND lease_token=?
                         AND lease_expires_at>?""",
                (expires, now_text, clean_job, clean_token, now_text),
            )
        return cursor.rowcount == 1

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        clean_job = _safe_identifier(job_id, field="job_id", maximum=120)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM retry_jobs WHERE id=?",
                (clean_job,),
            ).fetchone()
        return _row_dict(row)

    def _finish_job(
        self,
        job_id: str,
        lease_token: str,
        *,
        success: bool,
        error_code: str = "",
        base_backoff_seconds: int = 5,
        max_backoff_seconds: int = 3600,
        now: Any = None,
    ) -> dict[str, Any]:
        timestamp = _utc_datetime(now or self._now(), field="finish time")
        assert timestamp is not None
        now_text = utc_iso(timestamp)
        with self.transaction() as connection:
            row = connection.execute(
                """SELECT * FROM retry_jobs
                   WHERE id=? AND status='CLAIMED' AND lease_token=?
                         AND lease_expires_at>?""",
                (job_id, lease_token, now_text),
            ).fetchone()
            if row is None:
                raise ValueError("job claim is stale or invalid")
            if success:
                connection.execute(
                    """UPDATE retry_jobs SET status='COMPLETED',completed_at=?,
                           lease_owner='',lease_token='',lease_expires_at='',error_code='',updated_at=?
                       WHERE id=?""",
                    (now_text, now_text, job_id),
                )
            else:
                clean_error = str(error_code or "job_failed").strip().lower()[:80]
                if int(row["attempt_count"]) >= int(row["max_attempts"]):
                    connection.execute(
                        """UPDATE retry_jobs SET status='DEAD',dead_at=?,error_code=?,
                               lease_owner='',lease_token='',lease_expires_at='',updated_at=?
                           WHERE id=?""",
                        (now_text, clean_error, now_text, job_id),
                    )
                else:
                    if type(base_backoff_seconds) is not int or type(max_backoff_seconds) is not int:
                        raise TypeError("backoff values must be integers")
                    delay = min(
                        max_backoff_seconds,
                        max(0, base_backoff_seconds) * (2 ** max(0, int(row["attempt_count"]) - 1)),
                    )
                    connection.execute(
                        """UPDATE retry_jobs SET status='RETRY',next_attempt_at=?,error_code=?,
                               lease_owner='',lease_token='',lease_expires_at='',updated_at=?
                           WHERE id=?""",
                        (utc_iso(timestamp + timedelta(seconds=delay)), clean_error, now_text, job_id),
                    )
            final = connection.execute("SELECT * FROM retry_jobs WHERE id=?", (job_id,)).fetchone()
        return _row_dict(final) or {}

    def complete_job(self, job_id: str, lease_token: str, *, now: Any = None) -> dict[str, Any]:
        return self._finish_job(job_id, lease_token, success=True, now=now)

    def fail_job(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_code: str,
        base_backoff_seconds: int = 5,
        max_backoff_seconds: int = 3600,
        now: Any = None,
    ) -> dict[str, Any]:
        return self._finish_job(
            job_id,
            lease_token,
            success=False,
            error_code=error_code,
            base_backoff_seconds=base_backoff_seconds,
            max_backoff_seconds=max_backoff_seconds,
            now=now,
        )

    def record_alert(
        self,
        event_id: str,
        *,
        alert_kind: str,
        status: str,
        cooldown_seconds: int = 300,
        destination: str = "",
        message_hash: str = "",
        idempotency_key: str = "",
        at: Any = None,
    ) -> dict[str, Any]:
        clean_status = str(status or "").strip().upper()
        if clean_status not in ALERT_STATES:
            raise ValueError("alert status is invalid")
        if type(cooldown_seconds) is not int or not 0 <= cooldown_seconds <= 86_400:
            raise ValueError("cooldown_seconds is invalid")
        timestamp = _utc_datetime(at or self._now(), field="alert time")
        assert timestamp is not None
        now = utc_iso(timestamp)
        cooldown = utc_iso(timestamp + timedelta(seconds=cooldown_seconds))
        clean_kind = _safe_identifier(alert_kind, field="alert_kind", maximum=80)
        clean_key = str(idempotency_key or "").strip()
        if not clean_key:
            material = f"{event_id}\0{clean_kind}\0{message_hash}"
            clean_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        alert_id = "alert_" + uuid.uuid4().hex
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM alerts WHERE idempotency_key=?", (clean_key,)
            ).fetchone()
            if existing is not None:
                result = _row_dict(existing) or {}
                result["idempotent"] = True
                return result
            connection.execute(
                """INSERT INTO alerts(
                       id,event_id,alert_kind,status,destination,message_hash,
                       idempotency_key,sent_at,cooldown_until,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    alert_id,
                    event_id,
                    clean_kind,
                    clean_status,
                    str(destination or "")[:200],
                    str(message_hash or "")[:128],
                    clean_key,
                    now if clean_status in {"SENT", "WOULD_SEND"} else "",
                    cooldown,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
        result = _row_dict(row) or {}
        result["idempotent"] = False
        return result

    def alert_cooldown_active(self, event_id: str, *, at: Any = None) -> bool:
        now = utc_iso(at or self._now(), field="cooldown time")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT 1 FROM alerts WHERE event_id=?
                   AND status IN ('SENT','WOULD_SEND') AND cooldown_until>?
                   ORDER BY cooldown_until DESC LIMIT 1""",
                (event_id, now),
            ).fetchone()
        return row is not None

    def reconcile_alert_job_modes(
        self,
        active_mode: str,
        *,
        destination_fingerprint: str = "",
    ) -> int:
        """Obsolete unsafe legacy/dry-run work without consuming live work."""

        mode = str(active_mode or "").strip().lower()
        if mode not in {"disabled", "dry_run", "live"}:
            raise ValueError("active alert mode is invalid")
        now_dt = self._now()
        now = utc_iso(now_dt)
        obsolete: list[str] = []
        with self.transaction() as connection:
            rows = connection.execute(
                """SELECT id,dedupe_key,payload_json FROM retry_jobs
                   WHERE kind='send_alert' AND status IN ('PENDING','RETRY')"""
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                job_mode = str(payload.get("delivery_mode") or "").strip().lower()
                if job_mode not in {"dry_run", "live"}:
                    obsolete.append(str(row["id"]))
                    continue
                deadline = payload.get("not_after")
                destination = str(
                    payload.get("destination_fingerprint") or ""
                )
                try:
                    parsed_deadline = _utc_datetime(
                        deadline,
                        field="alert_not_after",
                    )
                except (TypeError, ValueError):
                    parsed_deadline = None
                if (
                    parsed_deadline is None
                    or parsed_deadline < now_dt
                    or len(destination) != 64
                ):
                    obsolete.append(str(row["id"]))
                    continue
                if (
                    mode == "live"
                    and destination_fingerprint
                    and destination != destination_fingerprint
                ):
                    obsolete.append(str(row["id"]))
                    continue
                # A dry-run record is an observation, never deferred live work.
                # Once dry-run is no longer active it is safe to obsolete. Live
                # work is intentionally preserved while delivery is disabled or
                # while the process is exercising dry-run output.
                if job_mode == "dry_run" and mode != "dry_run":
                    obsolete.append(str(row["id"]))
            if obsolete:
                placeholders = ",".join("?" for _ in obsolete)
                connection.execute(
                    f"""UPDATE retry_jobs SET status='COMPLETED',completed_at=?,
                               error_code='obsolete_delivery_mode',updated_at=?
                        WHERE id IN ({placeholders})
                              AND status IN ('PENDING','RETRY')""",
                    (now, now, *obsolete),
                )
        return len(obsolete)

    def put_ai_cache(
        self,
        cache_key: str,
        result: Mapping[str, Any],
        *,
        provider: str,
        model: str,
        content_hash: str = "",
        expires_at: Any = None,
    ) -> dict[str, Any]:
        clean_key = _safe_identifier(cache_key, field="cache_key", maximum=200)
        body = canonical_json(_json_object(result, field="result"))
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        now = utc_iso(self._now())
        expires = utc_iso(expires_at, field="expires_at", allow_none=True)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO ai_result_cache(
                       cache_key,content_hash,provider,model,result_json,result_sha256,
                       expires_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       content_hash=excluded.content_hash,provider=excluded.provider,
                       model=excluded.model,result_json=excluded.result_json,
                       result_sha256=excluded.result_sha256,expires_at=excluded.expires_at,
                       hit_count=0,last_used_at='',updated_at=excluded.updated_at""",
                (clean_key, content_hash, provider, model, body, digest, expires, now, now),
            )
            row = connection.execute(
                "SELECT * FROM ai_result_cache WHERE cache_key=?", (clean_key,)
            ).fetchone()
        return _row_dict(row) or {}

    def get_ai_cache(self, cache_key: str, *, at: Any = None) -> dict[str, Any] | None:
        now = utc_iso(at or self._now(), field="cache time")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM ai_result_cache WHERE cache_key=?", (cache_key,)
            ).fetchone()
            if row is None or (row["expires_at"] and row["expires_at"] <= now):
                return None
            connection.execute(
                "UPDATE ai_result_cache SET hit_count=hit_count+1,last_used_at=?,updated_at=? WHERE cache_key=?",
                (now, now, cache_key),
            )
            final = connection.execute(
                "SELECT * FROM ai_result_cache WHERE cache_key=?", (cache_key,)
            ).fetchone()
        return _row_dict(final)

    def put_ai_cache_and_usage(
        self,
        cache_key: str,
        result: Mapping[str, Any],
        *,
        provider: str,
        model: str,
        content_hash: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost: float | None = None,
        latency_ms: int = 0,
        request_id: str = "",
        analysis_time: Any = None,
        message_row_id: int | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically cache one provider response and account for that call once."""

        clean_key = _safe_identifier(cache_key, field="cache_key", maximum=200)
        for name, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("latency_ms", latency_ms),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if estimated_cost is not None and (
            isinstance(estimated_cost, bool)
            or type(estimated_cost) not in {int, float}
            or not math.isfinite(float(estimated_cost))
            or estimated_cost < 0
        ):
            raise ValueError("estimated_cost must be non-negative or null")
        body = canonical_json(_json_object(result, field="result"))
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        now = utc_iso(self._now())
        analyzed_at = utc_iso(analysis_time or self._now(), field="analysis_time")
        usage_material = "\0".join(
            (
                clean_key,
                str(provider),
                str(model),
                str(request_id or ""),
                str(message_row_id or ""),
                str(event_id or ""),
                analyzed_at,
            )
        )
        usage_id = "usage_cache_" + hashlib.sha256(
            usage_material.encode("utf-8")
        ).hexdigest()[:32]
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO ai_result_cache(
                       cache_key,content_hash,provider,model,result_json,result_sha256,
                       expires_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,'',?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       content_hash=excluded.content_hash,provider=excluded.provider,
                       model=excluded.model,result_json=excluded.result_json,
                       result_sha256=excluded.result_sha256,expires_at='',
                       hit_count=0,last_used_at='',updated_at=excluded.updated_at""",
                (
                    clean_key,
                    content_hash,
                    provider,
                    model,
                    body,
                    digest,
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT OR IGNORE INTO ai_usage(
                       id,message_row_id,event_id,provider,model,input_tokens,
                       output_tokens,estimated_cost,latency_ms,request_id,
                       analysis_time,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    usage_id,
                    message_row_id,
                    event_id,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    float(estimated_cost) if estimated_cost is not None else None,
                    latency_ms,
                    str(request_id or "")[:200],
                    analyzed_at,
                    now,
                ),
            )
            cache_row = connection.execute(
                "SELECT * FROM ai_result_cache WHERE cache_key=?", (clean_key,)
            ).fetchone()
            usage_row = connection.execute(
                "SELECT * FROM ai_usage WHERE id=?", (usage_id,)
            ).fetchone()
        return {
            "cache": _row_dict(cache_row) or {},
            "usage": _row_dict(usage_row) or {},
        }

    def save_analysis_result(
        self,
        result: Mapping[str, Any],
        *,
        provider: str,
        model: str,
        message_row_id: int | None = None,
        record_version: int = 1,
        event_id: str | None = None,
        importance_score: int = 0,
        confidence_score: int = 0,
    ) -> dict[str, Any]:
        if message_row_id is None and not event_id:
            raise ValueError("analysis must bind a message or event")
        if type(record_version) is not int or record_version < 1:
            raise ValueError("record_version must be a positive integer")
        body = canonical_json(_json_object(result, field="result"))
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        result_id = "analysis_" + uuid.uuid4().hex
        now = utc_iso(self._now())
        with self.transaction() as connection:
            if message_row_id is not None:
                current = connection.execute(
                    """SELECT 1 FROM telegram_messages
                       WHERE id=? AND record_version=? AND is_deleted=0""",
                    (message_row_id, record_version),
                ).fetchone()
                if current is None:
                    return {
                        "stale_record_version": True,
                        "message_row_id": message_row_id,
                        "record_version": record_version,
                    }
            connection.execute(
                """INSERT INTO analysis_results(
                       id,message_row_id,record_version,event_id,provider,model,result_json,result_sha256,
                       importance_score,confidence_score,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result_id,
                    message_row_id,
                    record_version,
                    event_id,
                    provider,
                    model,
                    body,
                    digest,
                    max(0, min(100, int(importance_score))),
                    max(0, min(100, int(confidence_score))),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM analysis_results WHERE id=?", (result_id,)
            ).fetchone()
        return _row_dict(row) or {}

    def record_ai_usage(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost: float | None = None,
        latency_ms: int = 0,
        request_id: str = "",
        analysis_time: Any = None,
        message_row_id: int | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        for name, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("latency_ms", latency_ms),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if estimated_cost is not None and (
            isinstance(estimated_cost, bool)
            or type(estimated_cost) not in {int, float}
            or not math.isfinite(float(estimated_cost))
            or estimated_cost < 0
        ):
            raise ValueError("estimated_cost must be non-negative or null")
        now = utc_iso(self._now())
        analysis_at = utc_iso(analysis_time or self._now(), field="analysis_time")
        usage_id = "usage_" + uuid.uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO ai_usage(
                       id,message_row_id,event_id,provider,model,input_tokens,output_tokens,
                       estimated_cost,latency_ms,request_id,analysis_time,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    usage_id,
                    message_row_id,
                    event_id,
                    provider,
                    model,
                    input_tokens,
                    output_tokens,
                    float(estimated_cost) if estimated_cost is not None else None,
                    latency_ms,
                    str(request_id or "")[:200],
                    analysis_at,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM ai_usage WHERE id=?", (usage_id,)).fetchone()
        return _row_dict(row) or {}

    def set_system_state(self, key: str, value: Any) -> None:
        clean_key = _safe_identifier(key, field="state key", maximum=120)
        if clean_key == "schema_version":
            raise ValueError("schema_version is repository-owned")
        body = canonical_json(value)
        now = utc_iso(self._now())
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO system_state(key,value_json,updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                       updated_at=excluded.updated_at""",
                (clean_key, body, now),
            )

    def get_system_state(self, key: str, default: Any = None) -> Any:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value_json FROM system_state WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError as exc:
            raise RuntimeError("system state is corrupt") from exc

    @staticmethod
    def _bounded_limit(value: int, *, maximum: int = 1_000) -> int:
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"limit must be between 1 and {maximum}")
        return value

    def recent_messages(
        self,
        *,
        since: Any,
        limit: int = 100,
        exclude_identity: tuple[str, int, int] | None = None,
    ) -> list[dict[str, Any]]:
        clean_limit = self._bounded_limit(limit)
        since_text = utc_iso(since, field="since")
        parameters: list[Any] = [since_text]
        exclusion = ""
        if exclude_identity is not None:
            if (
                type(exclude_identity) is not tuple
                or len(exclude_identity) != 3
                or type(exclude_identity[0]) is not str
                or type(exclude_identity[1]) is not int
                or type(exclude_identity[2]) is not int
            ):
                raise ValueError("exclude_identity must be (source, chat_id, message_id)")
            exclusion = " AND NOT(source=? AND chat_id=? AND message_id=?)"
            parameters.extend(exclude_identity)
        parameters.append(clean_limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT * FROM telegram_messages
                    WHERE timestamp>=?{exclusion}
                    ORDER BY timestamp DESC,id DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return [_row_dict(row) or {} for row in rows]

    def list_events_recent(self, *, since: Any, limit: int = 100) -> list[dict[str, Any]]:
        clean_limit = self._bounded_limit(limit)
        since_text = utc_iso(since, field="since")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT * FROM events WHERE last_seen_at>=?
                   ORDER BY last_seen_at DESC,event_id LIMIT ?""",
                (since_text, clean_limit),
            ).fetchall()
        return [_row_dict(row) or {} for row in rows]

    def load_event_states(self, *, since: Any, limit: int = 100) -> tuple[EventState, ...]:
        """Rebuild a bounded clustering window from durable message links."""

        clean_limit = self._bounded_limit(limit, maximum=500)
        since_text = utc_iso(since, field="since")
        states: list[EventState] = []
        with closing(self._connect()) as connection:
            policy_rows = connection.execute(
                "SELECT chat_id,enabled,trust_score FROM telegram_chats"
            ).fetchall()
            origin_policies = {
                int(item["chat_id"]): (
                    bool(item["enabled"]),
                    max(0, min(100, int(item["trust_score"]))),
                )
                for item in policy_rows
            }
            event_rows = connection.execute(
                """SELECT * FROM events WHERE last_seen_at>=?
                          AND message_count>0 AND message_count<=?
                          AND status<>'resolved'
                   ORDER BY last_seen_at DESC,event_id LIMIT ?""",
                (since_text, MAX_CLUSTER_EVENT_MESSAGES, clean_limit),
            ).fetchall()
            for event_row in event_rows:
                links = connection.execute(
                    """SELECT m.source,m.chat_id,m.message_id,m.chat_title,
                              m.forward_origin_json,COALESCE(c.trust_score,50) AS trust_score,
                              em.added_at,m.id AS message_row_id
                       FROM event_messages em
                       JOIN telegram_messages m ON m.id=em.message_row_id
                       LEFT JOIN telegram_chats c ON c.chat_id=m.chat_id
                       WHERE em.event_id=? AND em.relation<>'retracted'
                             AND m.is_deleted=0
                       ORDER BY em.added_at,m.id LIMIT ?""",
                    (event_row["event_id"], MAX_CLUSTER_EVENT_MESSAGES + 1),
                ).fetchall()
                if len(links) > MAX_CLUSTER_EVENT_MESSAGES:
                    raise RuntimeError("persisted event exceeds the clustering member cap")
                message_keys: list[tuple[Any, ...]] = []
                message_key_set: set[tuple[Any, ...]] = set()
                trust_by_source: dict[str, tuple[str, int, str]] = {}
                for link in links:
                    try:
                        origin = json.loads(link["forward_origin_json"] or "{}")
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            "persisted Telegram forward origin is invalid"
                        ) from exc
                    source_key = source_key_for_values(
                        source=link["source"],
                        chat_id=link["chat_id"],
                        forward_origin=origin,
                    )
                    message_key = (
                        str(link["source"]),
                        int(link["chat_id"]),
                        int(link["message_id"]),
                    )
                    if message_key not in message_key_set:
                        message_key_set.add(message_key)
                        message_keys.append(message_key)
                    trust = max(0, min(100, int(link["trust_score"])))
                    if origin:
                        origin_chat_id = origin.get("chat_id")
                        origin_policy = (
                            origin_policies.get(origin_chat_id)
                            if type(origin_chat_id) is int
                            else None
                        )
                        trust = (
                            min(trust, origin_policy[1])
                            if origin_policy is not None and origin_policy[0]
                            else min(trust, 30)
                        )
                    label = str(link["chat_title"] or link["chat_id"])[:200]
                    previous = trust_by_source.get(source_key)
                    if previous is None or trust > previous[1]:
                        trust_by_source[source_key] = (source_key, trust, label)
                source_keys = tuple(trust_by_source)
                if len(message_keys) != int(event_row["message_count"]):
                    raise RuntimeError(
                        "persisted event message count does not match event links"
                    )
                if len(source_keys) != int(event_row["source_count"]):
                    raise RuntimeError(
                        "persisted event source count does not match source identities"
                    )
                try:
                    tickers = tuple(json.loads(event_row["tickers_json"]))
                    keywords = tuple(json.loads(event_row["keywords_json"]))
                    centroid = int(str(event_row["centroid_simhash_hex"] or "0"), 16)
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        "persisted event clustering state is invalid"
                    ) from exc
                first_seen = _utc_datetime(
                    event_row["first_seen_at"], field="first_seen_at"
                )
                last_seen = _utc_datetime(
                    event_row["last_seen_at"], field="last_seen_at"
                )
                assert first_seen is not None and last_seen is not None
                states.append(
                    EventState(
                        event_id=str(event_row["event_id"]),
                        first_seen_at=first_seen,
                        last_seen_at=last_seen,
                        title=str(event_row["title"]),
                        category=str(event_row["category"]),
                        tickers=tickers,
                        keywords=keywords,
                        message_keys=tuple(message_keys),
                        source_keys=source_keys,
                        source_trust=tuple(sorted(trust_by_source.values())),
                        centroid_simhash=centroid,
                        importance_score=int(event_row["importance_score"]),
                        confidence_score=int(event_row["confidence_score"]),
                        status=str(event_row["status"]),
                    )
                )
        return tuple(states)

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
        return _row_dict(row)

    def latest_alert(self, event_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM alerts WHERE event_id=?
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                (event_id,),
            ).fetchone()
        return _row_dict(row)

    def alert_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        clean_key = _safe_identifier(
            idempotency_key,
            field="idempotency_key",
            maximum=240,
        )
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM alerts WHERE idempotency_key=?",
                (clean_key,),
            ).fetchone()
        return _row_dict(row)

    def event_delivery_state(
        self,
        event_id: str,
        *,
        message_row_id: int | None = None,
        record_version: int | None = None,
    ) -> dict[str, Any]:
        """Return only the current evidence state needed before Bot delivery."""

        clean_event = _safe_identifier(event_id, field="event_id", maximum=160)
        if message_row_id is not None and (
            type(message_row_id) is not int or message_row_id < 1
        ):
            raise ValueError("message_row_id must be a positive integer or null")
        if record_version is not None and (
            type(record_version) is not int or record_version < 1
        ):
            raise ValueError("record_version must be a positive integer or null")
        if record_version is not None and message_row_id is None:
            raise ValueError("record_version requires message_row_id")
        with closing(self._connect()) as connection:
            event = connection.execute(
                "SELECT status FROM events WHERE event_id=?",
                (clean_event,),
            ).fetchone()
            active = connection.execute(
                """SELECT COUNT(*) AS count FROM event_messages em
                   JOIN telegram_messages m ON m.id=em.message_row_id
                   WHERE em.event_id=? AND em.relation<>'retracted'
                         AND m.is_deleted=0""",
                (clean_event,),
            ).fetchone()
            trigger_active = True
            trigger_chat_id: int | None = None
            if message_row_id is not None:
                version_clause = ""
                parameters: tuple[Any, ...] = (clean_event, message_row_id)
                if record_version is not None:
                    version_clause = " AND m.record_version=?"
                    parameters = (*parameters, record_version)
                trigger_row = connection.execute(
                    f"""SELECT m.chat_id FROM event_messages em
                       JOIN telegram_messages m ON m.id=em.message_row_id
                       WHERE em.event_id=? AND em.message_row_id=?
                             AND em.relation<>'retracted' AND m.is_deleted=0
                             {version_clause}""",
                    parameters,
                ).fetchone()
                trigger_active = trigger_row is not None
                if trigger_row is not None:
                    trigger_chat_id = int(trigger_row["chat_id"])
        return {
            "exists": event is not None,
            "status": str(event["status"]) if event is not None else "",
            "active_message_count": int(active["count"] if active is not None else 0),
            "trigger_message_active": trigger_active,
            "trigger_chat_id": trigger_chat_id,
        }

    def job_counts(self) -> dict[str, int]:
        counts = {state: 0 for state in sorted(JOB_STATES)}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM retry_jobs GROUP BY status"
            ).fetchall()
        counts.update({str(row["status"]): int(row["count"]) for row in rows})
        return counts

    def status_counts(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            messages = connection.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(CASE WHEN is_deleted=1 THEN 1 ELSE 0 END),0) AS deleted
                   FROM telegram_messages"""
            ).fetchone()
            event_rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM events GROUP BY status"
            ).fetchall()
            alert_rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM alerts GROUP BY status"
            ).fetchall()
        return {
            "messages": int(messages["total"] if messages else 0),
            "deleted_messages": int(messages["deleted"] if messages else 0),
            "events": {str(row["status"]): int(row["count"]) for row in event_rows},
            "alerts": {str(row["status"]): int(row["count"]) for row in alert_rows},
            "jobs": self.job_counts(),
        }


__all__ = [
    "ALERT_STATES",
    "DEFAULT_MAX_DATABASE_BYTES",
    "JOB_STATES",
    "SCHEMA_VERSION",
    "TelegramRepository",
    "TelegramStorageCapacityError",
    "canonical_json",
    "utc_iso",
]
