from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} must be timezone-aware") from exc
    if offset is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _int_id(value: int, field_name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field_name} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class MessageEntity:
    kind: str
    offset: int
    length: int
    text: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("entity kind must be non-empty")
        if type(self.offset) is not int or self.offset < 0:
            raise ValueError("entity offset must be a non-negative integer")
        if type(self.length) is not int or self.length < 0:
            raise ValueError("entity length must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ForwardOrigin:
    kind: str
    sender_id: int | None = None
    sender_name: str = ""
    chat_id: int | None = None
    message_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("forward origin kind must be non-empty")
        for field_name in ("sender_id", "chat_id", "message_id"):
            value = getattr(self, field_name)
            if value is not None:
                _int_id(value, field_name)


@dataclass(frozen=True, slots=True)
class SourceMessage:
    source: str
    chat_id: int
    chat_title: str
    chat_type: str
    message_id: int
    sender_id: int | None
    sender_name: str
    timestamp: datetime
    edit_timestamp: datetime | None
    text: str
    caption: str
    urls: tuple[str, ...]
    entities: tuple[MessageEntity, ...]
    reply_to_message_id: int | None
    forward_origin: ForwardOrigin | None
    media_type: str
    media_file_id: str
    telegram_link: str
    raw_language: str
    is_edited: bool
    is_forwarded: bool
    ingested_at: datetime

    def __post_init__(self) -> None:
        if self.source != "telegram":
            raise ValueError("SourceMessage.source must be 'telegram'")
        _int_id(self.chat_id, "chat_id")
        _int_id(self.message_id, "message_id")
        if self.sender_id is not None:
            _int_id(self.sender_id, "sender_id")
        if self.reply_to_message_id is not None:
            _int_id(self.reply_to_message_id, "reply_to_message_id")
        if type(self.is_edited) is not bool or type(self.is_forwarded) is not bool:
            raise TypeError("edit and forward flags must be booleans")
        if type(self.urls) is not tuple or not all(
            isinstance(item, str) for item in self.urls
        ):
            raise TypeError("urls must be a tuple of strings")
        if type(self.entities) is not tuple or not all(
            isinstance(item, MessageEntity) for item in self.entities
        ):
            raise TypeError("entities must be a tuple of MessageEntity values")
        object.__setattr__(self, "timestamp", _utc_datetime(self.timestamp, "timestamp"))
        if self.edit_timestamp is not None:
            object.__setattr__(
                self,
                "edit_timestamp",
                _utc_datetime(self.edit_timestamp, "edit_timestamp"),
            )
        object.__setattr__(
            self,
            "ingested_at",
            _utc_datetime(self.ingested_at, "ingested_at"),
        )

    @property
    def content_text(self) -> str:
        return self.text if self.text else self.caption

    @property
    def identity_key(self) -> tuple[str, int, int]:
        return (self.source, self.chat_id, self.message_id)


@dataclass(frozen=True, slots=True)
class DeletedMessageEvent:
    source: str
    chat_id: int
    message_ids: tuple[int, ...]
    is_permanent: bool
    from_cache: bool
    received_at: datetime

    def __post_init__(self) -> None:
        if self.source != "telegram":
            raise ValueError("DeletedMessageEvent.source must be 'telegram'")
        _int_id(self.chat_id, "chat_id")
        if type(self.message_ids) is not tuple or not self.message_ids:
            raise ValueError("message_ids must be a non-empty tuple")
        if len(self.message_ids) > 500:
            raise ValueError("message_ids may contain at most 500 values")
        if len(set(self.message_ids)) != len(self.message_ids):
            raise ValueError("message_ids must not contain duplicates")
        for message_id in self.message_ids:
            _int_id(message_id, "message_id")
        if type(self.is_permanent) is not bool or type(self.from_cache) is not bool:
            raise TypeError("delete flags must be booleans")
        object.__setattr__(
            self,
            "received_at",
            _utc_datetime(self.received_at, "received_at"),
        )


@dataclass(frozen=True, slots=True)
class IgnoredMessageMetadata:
    ignored_chat_id: str
    received_at: datetime
    reason: str

    def __post_init__(self) -> None:
        if (
            type(self.ignored_chat_id) is not str
            or not self.ignored_chat_id.startswith("ignored_")
            or len(self.ignored_chat_id) != 24
            or any(
                character not in "0123456789abcdef"
                for character in self.ignored_chat_id.removeprefix("ignored_")
            )
        ):
            raise ValueError("ignored_chat_id must be a run-local opaque identifier")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("ignored reason must be non-empty")
        object.__setattr__(
            self,
            "received_at",
            _utc_datetime(self.received_at, "received_at"),
        )


@dataclass(frozen=True, slots=True)
class CollectorEvent:
    kind: str
    received_at: datetime
    message: SourceMessage | None = None
    deleted: DeletedMessageEvent | None = None
    ignored: IgnoredMessageMetadata | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("collector event kind must be non-empty")
        object.__setattr__(
            self,
            "received_at",
            _utc_datetime(self.received_at, "received_at"),
        )
