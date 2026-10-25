from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .models import ForwardOrigin, MessageEntity, SourceMessage
from .tdlib import TDLibChat


_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)


class TelegramNormalizationError(ValueError):
    pass


def _required_int(value: Any, field_name: str) -> int:
    if type(value) is not int:
        raise TelegramNormalizationError(f"{field_name} must be an integer")
    return value


def _utc_from_unix(value: Any, field_name: str) -> datetime:
    timestamp = _required_int(value, field_name)
    if timestamp < 0:
        raise TelegramNormalizationError(f"{field_name} must be non-negative")
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise TelegramNormalizationError(f"{field_name} is outside datetime range") from exc


def _utc_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise TelegramNormalizationError("ingested_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _utf16_slice(text: str, offset: int, length: int) -> str:
    if offset < 0 or length < 0:
        return ""
    raw = text.encode("utf-16-le")
    start = offset * 2
    end = (offset + length) * 2
    if start > len(raw) or end > len(raw):
        return ""
    try:
        return raw[start:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return ""


def _clean_url(value: str) -> str:
    return value.rstrip(".,;:!?)]}，。；：！？）】")


def _formatted_text(value: Any) -> tuple[str, tuple[MessageEntity, ...], tuple[str, ...]]:
    if type(value) is not dict:
        return "", (), ()
    text = value.get("text")
    text = text if type(text) is str else ""
    raw_entities = value.get("entities")
    entities: list[MessageEntity] = []
    urls: list[str] = []
    if type(raw_entities) is list:
        for raw_entity in raw_entities:
            if type(raw_entity) is not dict:
                continue
            offset = raw_entity.get("offset")
            length = raw_entity.get("length")
            type_value = raw_entity.get("type")
            if (
                type(offset) is not int
                or offset < 0
                or type(length) is not int
                or length < 0
                or type(type_value) is not dict
            ):
                continue
            raw_kind = type_value.get("@type")
            if type(raw_kind) is not str:
                continue
            kind = _snake_case(raw_kind.removeprefix("textEntityType"))
            entity_text = _utf16_slice(text, offset, length)
            entity_url = ""
            if kind == "url":
                entity_url = entity_text
            elif kind == "text_url" and type(type_value.get("url")) is str:
                entity_url = type_value["url"]
            elif kind == "email_address" and entity_text:
                entity_url = f"mailto:{entity_text}"
            entity_url = _clean_url(entity_url)
            entities.append(
                MessageEntity(
                    kind=kind,
                    offset=offset,
                    length=length,
                    text=entity_text,
                    url=entity_url,
                )
            )
            if entity_url:
                urls.append(entity_url)
    urls.extend(_clean_url(match.group(0)) for match in _URL_RE.finditer(text))
    return text, tuple(entities), tuple(dict.fromkeys(urls))


def _sender_id(value: Any) -> int | None:
    if type(value) is not dict:
        return None
    sender_type = value.get("@type")
    key = (
        "user_id"
        if sender_type == "messageSenderUser"
        else "chat_id"
        if sender_type == "messageSenderChat"
        else ""
    )
    result = value.get(key) if key else None
    return result if type(result) is int else None


def _reply_message_id(message: Mapping[str, Any]) -> int | None:
    reply_to = message.get("reply_to")
    if type(reply_to) is dict:
        value = reply_to.get("message_id")
        if type(value) is int:
            return value
    legacy = message.get("reply_to_message_id")
    return legacy if type(legacy) is int else None


def _forward_origin(value: Any) -> ForwardOrigin | None:
    if type(value) is not dict:
        return None
    origin = value.get("origin")
    if type(origin) is not dict:
        return None
    raw_type = origin.get("@type")
    if type(raw_type) is not str:
        return None
    kind = _snake_case(raw_type.removeprefix("messageOrigin"))
    sender_id: int | None = None
    chat_id: int | None = None
    message_id: int | None = None
    sender_name = ""
    if raw_type == "messageOriginUser":
        value_id = origin.get("sender_user_id")
        sender_id = value_id if type(value_id) is int else None
    elif raw_type == "messageOriginHiddenUser":
        value_name = origin.get("sender_name")
        sender_name = value_name if type(value_name) is str else ""
    elif raw_type == "messageOriginChat":
        value_id = origin.get("sender_chat_id")
        chat_id = value_id if type(value_id) is int else None
        signature = origin.get("author_signature")
        sender_name = signature if type(signature) is str else ""
    elif raw_type == "messageOriginChannel":
        value_id = origin.get("chat_id")
        chat_id = value_id if type(value_id) is int else None
        value_message_id = origin.get("message_id")
        message_id = value_message_id if type(value_message_id) is int else None
        signature = origin.get("author_signature")
        sender_name = signature if type(signature) is str else ""
    return ForwardOrigin(
        kind=kind,
        sender_id=sender_id,
        sender_name=sender_name,
        chat_id=chat_id,
        message_id=message_id,
    )


def _media_file(value: Any) -> str:
    if type(value) is not dict:
        return ""
    remote = value.get("remote")
    if type(remote) is dict:
        for key in ("unique_id", "id"):
            remote_id = remote.get(key)
            if type(remote_id) is str and remote_id:
                return remote_id
    file_id = value.get("id")
    return str(file_id) if type(file_id) is int else ""


def _media_file_id(content: Mapping[str, Any], content_type: str) -> str:
    paths = {
        "messagePhoto": ("photo", "sizes"),
        "messageDocument": ("document", "document"),
        "messageVideo": ("video", "video"),
        "messageAnimation": ("animation", "animation"),
        "messageAudio": ("audio", "audio"),
        "messageVoiceNote": ("voice_note", "voice"),
        "messageVideoNote": ("video_note", "video"),
        "messageSticker": ("sticker", "sticker"),
    }
    path = paths.get(content_type)
    if path is None:
        return ""
    outer = content.get(path[0])
    if type(outer) is not dict:
        return ""
    nested = outer.get(path[1])
    if content_type == "messagePhoto":
        if type(nested) is not list or not nested:
            return ""
        size = nested[-1]
        if type(size) is not dict:
            return ""
        return _media_file(size.get("photo"))
    return _media_file(nested)


def _chat_values(chat: TDLibChat | Mapping[str, Any] | None) -> tuple[str, str]:
    if isinstance(chat, TDLibChat):
        return chat.title, chat.chat_type
    if type(chat) is not dict:
        return "", ""
    title = chat.get("title")
    title = title if type(title) is str else ""
    type_value = chat.get("type")
    if type(type_value) is dict:
        raw_type = type_value.get("@type")
        chat_type = (
            _snake_case(raw_type.removeprefix("chatType"))
            if type(raw_type) is str
            else ""
        )
    else:
        raw_type = chat.get("chat_type")
        chat_type = raw_type if type(raw_type) is str else ""
    return title, chat_type


def normalize_tdlib_message(
    message: Mapping[str, Any],
    *,
    chat: TDLibChat | Mapping[str, Any] | None = None,
    sender_name: str = "",
    ingested_at: datetime | None = None,
) -> SourceMessage:
    if type(message) is not dict:
        raise TelegramNormalizationError("TDLib message must be an object")
    message_id = _required_int(message.get("id"), "message.id")
    chat_id = _required_int(message.get("chat_id"), "message.chat_id")
    timestamp = _utc_from_unix(message.get("date"), "message.date")
    raw_edit_date = message.get("edit_date", 0)
    if type(raw_edit_date) is not int:
        raise TelegramNormalizationError("message.edit_date must be an integer")
    edit_timestamp = (
        _utc_from_unix(raw_edit_date, "message.edit_date")
        if raw_edit_date > 0
        else None
    )
    content = message.get("content")
    if type(content) is not dict:
        raise TelegramNormalizationError("message.content must be an object")
    content_type = content.get("@type")
    if type(content_type) is not str or not content_type:
        raise TelegramNormalizationError("message.content requires @type")
    text = ""
    caption = ""
    entities: tuple[MessageEntity, ...] = ()
    urls: tuple[str, ...] = ()
    if content_type == "messageText":
        text, entities, urls = _formatted_text(content.get("text"))
    else:
        caption, entities, urls = _formatted_text(content.get("caption"))
    title, chat_type = _chat_values(chat)
    raw_language = message.get("language_code")
    if type(raw_language) is not str:
        raw_language = content.get("language_code")
    if type(raw_language) is not str:
        raw_language = ""
    link = message.get("telegram_link")
    if type(link) is not str:
        link = message.get("link")
    if type(link) is not str:
        link = ""
    media_type = (
        ""
        if content_type == "messageText"
        else _snake_case(content_type.removeprefix("message"))
    )
    forward = _forward_origin(message.get("forward_info"))
    return SourceMessage(
        source="telegram",
        chat_id=chat_id,
        chat_title=title,
        chat_type=chat_type,
        message_id=message_id,
        sender_id=_sender_id(message.get("sender_id")),
        sender_name=sender_name if type(sender_name) is str else "",
        timestamp=timestamp,
        edit_timestamp=edit_timestamp,
        text=text,
        caption=caption,
        urls=urls,
        entities=entities,
        reply_to_message_id=_reply_message_id(message),
        forward_origin=forward,
        media_type=media_type,
        media_file_id=_media_file_id(content, content_type),
        telegram_link=link,
        raw_language=raw_language,
        is_edited=edit_timestamp is not None,
        is_forwarded=forward is not None,
        ingested_at=_utc_now(ingested_at),
    )
