from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


class HistorySyncError(ValueError):
    pass


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise HistorySyncError("since_time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _message_time(value: Any) -> datetime:
    if type(value) is not int or value < 0:
        raise HistorySyncError("history message date is invalid")
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OSError, OverflowError, ValueError) as exc:
        raise HistorySyncError("history message date is outside datetime range") from exc


@dataclass(frozen=True, slots=True)
class HistorySyncResult:
    chat_id: int
    requested_limit: int
    fetched_count: int
    processed_count: int
    ignored_count: int
    pages: int
    stopped_at_since_time: bool
    since_time: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def sync_history(
    client: Any,
    collector: Any,
    chat_id: int,
    limit: int = 100,
    since_time: datetime | None = None,
    max_limit: int = 1_000,
) -> HistorySyncResult:
    """Read a bounded history for one enabled whitelist chat.

    Every accepted message is sent through the normal collector sink.  The
    sink/repository remains the sole authority for idempotency.
    """

    if type(chat_id) is not int or chat_id == 0:
        raise HistorySyncError("chat_id must be a non-zero integer")
    if type(max_limit) is not int or not 1 <= max_limit <= 10_000:
        raise HistorySyncError("max_limit must be between 1 and 10000")
    if type(limit) is not int or not 1 <= limit <= max_limit:
        raise HistorySyncError(f"limit must be between 1 and {max_limit}")
    cutoff = _aware_utc(since_time)
    config = getattr(collector, "config", None)
    policy_for = getattr(config, "policy_for", None)
    policy = policy_for(chat_id) if callable(policy_for) else None
    if policy is None or not bool(getattr(policy, "enabled", False)):
        raise HistorySyncError("history chat must be enabled in the whitelist")
    process_update = getattr(collector, "process_update", None)
    if not callable(process_update):
        raise HistorySyncError("collector does not support message ingestion")
    call = getattr(client, "call", None)
    if not callable(call):
        raise HistorySyncError("TDLib client does not support requests")

    fetched = 0
    processed = 0
    ignored = 0
    pages = 0
    from_message_id = 0
    seen_ids: set[int] = set()
    stopped_at_cutoff = False

    while fetched < limit and not stopped_at_cutoff:
        remaining = limit - fetched
        # Some TDLib versions include from_message_id in the next page while
        # others resume just before it.  Ask for one bounded overlap slot so
        # the seen-id guard cannot consume the caller's final requested item.
        page_limit = min(100, remaining + (1 if from_message_id else 0))
        response = call(
            {
                "@type": "getChatHistory",
                "chat_id": chat_id,
                "from_message_id": from_message_id,
                "offset": 0,
                "limit": page_limit,
                "only_local": False,
            }
        )
        if type(response) is not dict or response.get("@type") != "messages":
            raise HistorySyncError("TDLib returned an invalid history response")
        raw_messages = response.get("messages")
        if type(raw_messages) is not list:
            raise HistorySyncError("TDLib returned no history message list")
        pages += 1
        if not raw_messages:
            break
        page_progress = False
        next_from_id: int | None = None
        for message in raw_messages:
            if fetched >= limit:
                break
            if type(message) is not dict:
                raise HistorySyncError("history message must be an object")
            returned_chat_id = message.get("chat_id")
            if type(returned_chat_id) is not int or returned_chat_id != chat_id:
                raise HistorySyncError("TDLib history crossed the requested chat boundary")
            message_id = message.get("id")
            if type(message_id) is not int:
                raise HistorySyncError("history message id is invalid")
            next_from_id = message_id
            if message_id in seen_ids:
                continue
            message_time = _message_time(message.get("date"))
            if cutoff is not None and message_time < cutoff:
                stopped_at_cutoff = True
                break
            seen_ids.add(message_id)
            page_progress = True
            fetched += 1
            event = process_update(
                {
                    "@type": "updateNewMessage",
                    "message": message,
                }
            )
            if event is None:
                ignored += 1
            elif getattr(event, "kind", "") == "ignored":
                ignored += 1
            else:
                processed += 1
        if stopped_at_cutoff or fetched >= limit:
            break
        if next_from_id is None or not page_progress:
            break
        if next_from_id == from_message_id:
            break
        from_message_id = next_from_id
        if len(raw_messages) < page_limit:
            break

    return HistorySyncResult(
        chat_id=chat_id,
        requested_limit=limit,
        fetched_count=fetched,
        processed_count=processed,
        ignored_count=ignored,
        pages=pages,
        stopped_at_since_time=stopped_at_cutoff,
        since_time=(
            cutoff.isoformat().replace("+00:00", "Z") if cutoff is not None else ""
        ),
    )


__all__ = ["HistorySyncError", "HistorySyncResult", "sync_history"]
