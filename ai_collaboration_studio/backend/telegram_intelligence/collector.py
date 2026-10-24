from __future__ import annotations

import copy
import hashlib
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .config import ChatPolicy, TelegramCollectorConfig
from .models import (
    CollectorEvent,
    DeletedMessageEvent,
    IgnoredMessageMetadata,
    SourceMessage,
)
from .normalizer import normalize_tdlib_message
from .tdlib import TDLibChat, TDLibClient, TDLibError


MessageSink = Callable[[SourceMessage], None]
DeleteSink = Callable[[DeletedMessageEvent], None]
IgnoredSink = Callable[[IgnoredMessageMetadata], None]
Normalizer = Callable[..., SourceMessage]


class TelegramCollector:
    """Whitelist-first projection of raw TDLib updates.

    Raw message bodies are cached only after a chat passes the enabled
    whitelist.  This class has no Analyzer, Bot, media-download, or trading
    dependency.
    """

    def __init__(
        self,
        client: TDLibClient,
        config: TelegramCollectorConfig,
        *,
        message_sink: MessageSink | None = None,
        delete_sink: DeleteSink | None = None,
        ignored_sink: IgnoredSink | None = None,
        normalizer: Normalizer = normalize_tdlib_message,
        clock: Callable[[], datetime] | None = None,
        message_cache_size: int = 5_000,
        edit_fetch_timeout: float = 5.0,
    ) -> None:
        if type(message_cache_size) is not int or message_cache_size < 1:
            raise ValueError("message_cache_size must be positive")
        if type(edit_fetch_timeout) not in {int, float} or not 0 < float(edit_fetch_timeout) <= 30:
            raise ValueError("edit_fetch_timeout must be between 0 and 30 seconds")
        self.client = client
        self.config = config
        self.message_sink = message_sink
        self.delete_sink = delete_sink
        self.ignored_sink = ignored_sink
        self.normalizer = normalizer
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.message_cache_size = message_cache_size
        self.edit_fetch_timeout = float(edit_fetch_timeout)
        self._messages: OrderedDict[tuple[int, int], dict[str, Any]] = OrderedDict()
        self._ignored_identity_salt = secrets.token_bytes(32)
        self._cache_lock = threading.Lock()
        self._stop = threading.Event()

    def _now(self) -> datetime:
        value = self.clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collector clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _policy(self, chat_id: Any) -> ChatPolicy | None:
        return self.config.policy_for(chat_id) if type(chat_id) is int else None

    def _ignored(
        self,
        chat_id: int,
        *,
        received_at: datetime,
        reason: str,
    ) -> CollectorEvent:
        opaque_chat_id = "ignored_" + hashlib.sha256(
            self._ignored_identity_salt + str(chat_id).encode("ascii")
        ).hexdigest()[:16]
        metadata = IgnoredMessageMetadata(
            ignored_chat_id=opaque_chat_id,
            received_at=received_at,
            reason=reason,
        )
        if self.ignored_sink is not None:
            self.ignored_sink(metadata)
        return CollectorEvent(
            kind="ignored",
            received_at=received_at,
            ignored=metadata,
        )

    def _allow_or_ignore(
        self,
        chat_id: Any,
        *,
        received_at: datetime,
    ) -> tuple[ChatPolicy | None, CollectorEvent | None]:
        if type(chat_id) is not int:
            return None, None
        policy = self._policy(chat_id)
        if policy is None:
            return None, self._ignored(
                chat_id,
                received_at=received_at,
                reason="not_whitelisted",
            )
        if not policy.enabled:
            return None, self._ignored(
                chat_id,
                received_at=received_at,
                reason="whitelist_disabled",
            )
        return policy, None

    def _cache_message(self, message: Mapping[str, Any]) -> None:
        chat_id = message.get("chat_id")
        message_id = message.get("id")
        if type(chat_id) is not int or type(message_id) is not int:
            return
        key = (chat_id, message_id)
        with self._cache_lock:
            self._messages[key] = copy.deepcopy(dict(message))
            self._messages.move_to_end(key)
            while len(self._messages) > self.message_cache_size:
                self._messages.popitem(last=False)

    def _cached_message(self, chat_id: int, message_id: int) -> dict[str, Any] | None:
        key = (chat_id, message_id)
        with self._cache_lock:
            value = self._messages.get(key)
            if value is not None:
                self._messages.move_to_end(key)
                return copy.deepcopy(value)
        return None

    def _delete_cached(self, chat_id: int, message_ids: tuple[int, ...]) -> None:
        with self._cache_lock:
            for message_id in message_ids:
                self._messages.pop((chat_id, message_id), None)

    def _chat(self, chat_id: int, policy: ChatPolicy) -> TDLibChat:
        chat = self.client.chat(chat_id)
        if chat is None:
            return TDLibChat(
                chat_id=chat_id,
                title=policy.name,
                chat_type="",
            )
        if not chat.title and policy.name:
            return TDLibChat(
                chat_id=chat.chat_id,
                title=policy.name,
                chat_type=chat.chat_type,
                username=chat.username,
            )
        return chat

    def _normalize_and_emit(
        self,
        raw_message: Mapping[str, Any],
        *,
        policy: ChatPolicy,
        received_at: datetime,
        kind: str,
    ) -> CollectorEvent:
        chat_id = raw_message["chat_id"]
        sender_name = self.client.sender_name(raw_message.get("sender_id"))
        message = self.normalizer(
            raw_message,
            chat=self._chat(chat_id, policy),
            sender_name=sender_name,
            ingested_at=received_at,
        )
        self._cache_message(raw_message)
        if self.message_sink is not None:
            self.message_sink(message)
        return CollectorEvent(
            kind=kind,
            received_at=received_at,
            message=message,
        )

    def process_update(self, update: Mapping[str, Any]) -> CollectorEvent | None:
        if type(update) is not dict:
            raise TypeError("TDLib update must be an object")
        received_at = self._now()
        update_type = update.get("@type")
        if update_type == "updateNewMessage":
            raw_message = update.get("message")
            if type(raw_message) is not dict:
                return None
            # Deliberately inspect only chat_id before the whitelist decision.
            chat_id = raw_message.get("chat_id")
            policy, ignored = self._allow_or_ignore(
                chat_id,
                received_at=received_at,
            )
            if ignored is not None:
                return ignored
            if policy is None:
                return None
            return self._normalize_and_emit(
                raw_message,
                policy=policy,
                received_at=received_at,
                kind="message",
            )

        if update_type in {"updateMessageContent", "updateMessageEdited"}:
            chat_id = update.get("chat_id")
            policy, ignored = self._allow_or_ignore(
                chat_id,
                received_at=received_at,
            )
            if ignored is not None:
                return ignored
            message_id = update.get("message_id")
            if policy is None or type(chat_id) is not int or type(message_id) is not int:
                return None
            raw_message = self._cached_message(chat_id, message_id)
            if raw_message is None:
                getter = getattr(self.client, "get_message", None)
                if callable(getter):
                    try:
                        fetched = getter(
                            chat_id,
                            message_id,
                            timeout=self.edit_fetch_timeout,
                        )
                    except TDLibError:
                        fetched = None
                    if (
                        type(fetched) is dict
                        and fetched.get("chat_id") == chat_id
                        and fetched.get("id") == message_id
                    ):
                        raw_message = copy.deepcopy(fetched)
                if raw_message is None:
                    return CollectorEvent(
                        kind="edit_without_cached_message",
                        received_at=received_at,
                    )
            if update_type == "updateMessageContent":
                new_content = update.get("new_content")
                if type(new_content) is not dict:
                    return None
                raw_message["content"] = copy.deepcopy(new_content)
            edit_date = update.get("edit_date")
            if type(edit_date) is int and edit_date > 0:
                raw_message["edit_date"] = edit_date
            elif not raw_message.get("edit_date"):
                raw_message["edit_date"] = int(received_at.timestamp())
            return self._normalize_and_emit(
                raw_message,
                policy=policy,
                received_at=received_at,
                kind="message_edited",
            )

        if update_type == "updateDeleteMessages":
            chat_id = update.get("chat_id")
            policy, ignored = self._allow_or_ignore(
                chat_id,
                received_at=received_at,
            )
            if ignored is not None:
                return ignored
            raw_ids = update.get("message_ids")
            if policy is None or type(chat_id) is not int or type(raw_ids) is not list:
                return None
            if len(raw_ids) > 500:
                raise TDLibError(
                    "delete update exceeds the 500-message safety bound; bounded recovery is required"
                )
            if any(type(value) is not int for value in raw_ids):
                raise TDLibError("delete update contains an invalid message identity")
            message_ids = tuple(dict.fromkeys(raw_ids))
            if not message_ids:
                return None
            event = DeletedMessageEvent(
                source="telegram",
                chat_id=chat_id,
                message_ids=message_ids,
                is_permanent=bool(update.get("is_permanent", False)),
                from_cache=bool(update.get("from_cache", False)),
                received_at=received_at,
            )
            self._delete_cached(chat_id, message_ids)
            if self.delete_sink is not None:
                self.delete_sink(event)
            return CollectorEvent(
                kind="messages_deleted",
                received_at=received_at,
                deleted=event,
            )

        if update_type == "updateConnectionState":
            return CollectorEvent(kind="network_state", received_at=received_at)
        if update_type in {
            "updateNewChat",
            "updateChatTitle",
            "updateChatPhoto",
            "updateChatPosition",
        }:
            return CollectorEvent(kind="chat_metadata", received_at=received_at)
        return None

    def run(self, *, poll_timeout: float = 0.5) -> None:
        if poll_timeout <= 0:
            raise ValueError("poll_timeout must be positive")
        self._stop.clear()
        self.client.start()
        while not self._stop.is_set():
            update = self.client.get_update(timeout=poll_timeout)
            if update is not None:
                self.process_update(update)

    def drain_pending_updates(self, *, max_updates: int = 100_000) -> int:
        """Persist the bounded TDLib queue after its receiver has been stopped.

        The runtime closes the client before calling this method, so a ``None``
        result is a stable empty-queue observation rather than a race with the
        sole TDLib receiver.  Processing still follows the normal whitelist-
        first path and therefore cannot bypass collection policy during
        shutdown.
        """

        if type(max_updates) is not int or not 1 <= max_updates <= 100_000:
            raise ValueError("max_updates must be between 1 and 100000")
        getter = getattr(self.client, "get_update", None)
        if not callable(getter):
            return 0
        drained = 0
        while drained < max_updates:
            update = getter(timeout=0)
            if update is None:
                return drained
            self.process_update(update)
            drained += 1
        # The production TDLib queue is capped at 10,000. Reaching this much
        # larger bound means an injected client violated that contract; do not
        # pretend shutdown completed cleanly.
        raise TDLibError(
            f"shutdown drain exceeded its {max_updates}-update safety bound; "
            "bounded history recovery is required"
        )

    def stop(self) -> None:
        self._stop.set()
