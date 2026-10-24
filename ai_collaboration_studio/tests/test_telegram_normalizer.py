from __future__ import annotations

import unittest
from collections import deque
from datetime import datetime, timedelta, timezone

from backend.telegram_intelligence.collector import TelegramCollector
from backend.telegram_intelligence.config import ChatPolicy, TelegramCollectorConfig
from backend.telegram_intelligence.normalizer import (
    TelegramNormalizationError,
    normalize_tdlib_message,
)
from backend.telegram_intelligence.tdlib import TDLibChat


NOW = datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc)


def _text_message(
    *,
    chat_id: int = -100123,
    message_id: int = 77,
    text: str = "NVDA https://example.com/news",
) -> dict[str, object]:
    return {
        "@type": "message",
        "id": message_id,
        "chat_id": chat_id,
        "sender_id": {"@type": "messageSenderUser", "user_id": 42},
        "date": 1_775_000_000,
        "edit_date": 0,
        "content": {
            "@type": "messageText",
            "text": {
                "@type": "formattedText",
                "text": text,
                "entities": [],
            },
        },
    }


class _StubClient:
    def __init__(self) -> None:
        self.chats = {-100123: TDLibChat(-100123, "Official", "supergroup")}
        self.message_fetches: list[tuple[int, int, float]] = []
        self.updates = deque()

    def chat(self, chat_id: int):
        return self.chats.get(chat_id)

    def sender_name(self, sender) -> str:
        return "Alice" if sender else ""

    def start(self) -> None:
        return None

    def get_update(self, timeout=None):
        return self.updates.popleft() if self.updates else None

    def get_message(self, chat_id: int, message_id: int, *, timeout: float):
        self.message_fetches.append((chat_id, message_id, timeout))
        return _text_message(chat_id=chat_id, message_id=message_id, text="persisted")


class TelegramNormalizerTests(unittest.TestCase):
    def test_text_message_has_exact_identity_and_aware_utc_times(self) -> None:
        raw = _text_message()
        raw["edit_date"] = 1_775_000_100
        raw["reply_to"] = {
            "@type": "messageReplyToMessage",
            "message_id": 70,
        }
        raw["forward_info"] = {
            "origin": {
                "@type": "messageOriginChannel",
                "chat_id": -100900,
                "message_id": 11,
                "author_signature": "Desk",
            }
        }
        message = normalize_tdlib_message(
            raw,
            chat=TDLibChat(-100123, "Official", "supergroup"),
            sender_name="Alice",
            ingested_at=NOW.astimezone(timezone(timedelta(hours=8))),
        )
        self.assertEqual(message.identity_key, ("telegram", -100123, 77))
        self.assertEqual(message.content_text, "NVDA https://example.com/news")
        self.assertEqual(message.urls, ("https://example.com/news",))
        self.assertEqual(message.reply_to_message_id, 70)
        self.assertEqual(message.forward_origin.chat_id, -100900)
        self.assertTrue(message.is_edited)
        self.assertTrue(message.is_forwarded)
        self.assertEqual(message.timestamp.tzinfo, timezone.utc)
        self.assertEqual(message.ingested_at, NOW)

    def test_utf16_entity_offsets_and_text_url_are_preserved(self) -> None:
        raw = _text_message(text="🚀News")
        raw["content"]["text"]["entities"] = [
            {
                "@type": "textEntity",
                "offset": 2,
                "length": 4,
                "type": {
                    "@type": "textEntityTypeTextUrl",
                    "url": "https://example.com/item",
                },
            }
        ]
        message = normalize_tdlib_message(raw, ingested_at=NOW)
        self.assertEqual(message.entities[0].text, "News")
        self.assertEqual(message.entities[0].kind, "text_url")
        self.assertEqual(message.urls, ("https://example.com/item",))

    def test_media_caption_is_metadata_only_and_not_downloaded(self) -> None:
        raw = _text_message()
        raw["content"] = {
            "@type": "messagePhoto",
            "caption": {
                "@type": "formattedText",
                "text": "Chart https://example.com/chart",
                "entities": [],
            },
            "photo": {
                "sizes": [
                    {
                        "photo": {
                            "@type": "file",
                            "id": 9,
                            "remote": {"unique_id": "photo-unique"},
                        }
                    }
                ]
            },
        }
        message = normalize_tdlib_message(raw, ingested_at=NOW)
        self.assertEqual(message.text, "")
        self.assertEqual(message.caption, "Chart https://example.com/chart")
        self.assertEqual(message.media_type, "photo")
        self.assertEqual(message.media_file_id, "photo-unique")

    def test_malformed_message_and_naive_ingest_time_fail(self) -> None:
        with self.assertRaises(TelegramNormalizationError):
            normalize_tdlib_message({}, ingested_at=NOW)
        with self.assertRaises(TelegramNormalizationError):
            normalize_tdlib_message(
                _text_message(),
                ingested_at=datetime(2026, 8, 29),
            )


class TelegramCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.allowed = ChatPolicy(
            chat_id=-100123,
            enabled=True,
            name="Official",
            category="market",
            priority="high",
            trust_score=95,
        )
        self.config = TelegramCollectorConfig((self.allowed,))
        self.client = _StubClient()

    def test_non_whitelisted_message_is_never_normalized_or_persisted(self) -> None:
        normalized: list[object] = []
        persisted: list[object] = []
        ignored: list[object] = []

        def forbidden_normalizer(*args, **kwargs):
            normalized.append((args, kwargs))
            raise AssertionError("normalizer must not see denied message content")

        collector = TelegramCollector(
            self.client,
            self.config,
            normalizer=forbidden_normalizer,
            message_sink=persisted.append,
            ignored_sink=ignored.append,
            clock=lambda: NOW,
        )
        event = collector.process_update(
            {
                "@type": "updateNewMessage",
                "message": _text_message(chat_id=-999, text="private body"),
            }
        )
        self.assertEqual(normalized, [])
        self.assertEqual(persisted, [])
        self.assertRegex(event.ignored.ignored_chat_id, r"^ignored_[0-9a-f]{16}$")
        self.assertNotIn("999", event.ignored.ignored_chat_id)
        self.assertEqual(event.ignored.reason, "not_whitelisted")
        self.assertFalse(hasattr(event.ignored, "text"))

    def test_allowed_message_edit_and_delete_flow(self) -> None:
        persisted = []
        deleted = []
        collector = TelegramCollector(
            self.client,
            self.config,
            message_sink=persisted.append,
            delete_sink=deleted.append,
            clock=lambda: NOW,
        )
        created = collector.process_update(
            {"@type": "updateNewMessage", "message": _text_message()}
        )
        edited = collector.process_update(
            {
                "@type": "updateMessageContent",
                "chat_id": -100123,
                "message_id": 77,
                "new_content": {
                    "@type": "messageText",
                    "text": {
                        "@type": "formattedText",
                        "text": "edited",
                        "entities": [],
                    },
                },
                "edit_date": 1_775_000_200,
            }
        )
        removed = collector.process_update(
            {
                "@type": "updateDeleteMessages",
                "chat_id": -100123,
                "message_ids": [77],
                "is_permanent": True,
                "from_cache": False,
            }
        )
        self.assertEqual(created.kind, "message")
        self.assertEqual(edited.message.content_text, "edited")
        self.assertTrue(edited.message.is_edited)
        self.assertEqual(len(persisted), 2)
        self.assertEqual(removed.deleted.message_ids, (77,))
        self.assertEqual(deleted[0].chat_id, -100123)

    def test_edit_cache_miss_fetches_only_the_exact_whitelisted_message(self) -> None:
        persisted = []
        collector = TelegramCollector(
            self.client,
            self.config,
            message_sink=persisted.append,
            clock=lambda: NOW,
        )

        edited = collector.process_update(
            {
                "@type": "updateMessageContent",
                "chat_id": -100123,
                "message_id": 501,
                "new_content": {
                    "@type": "messageText",
                    "text": {
                        "@type": "formattedText",
                        "text": "edited after restart",
                        "entities": [],
                    },
                },
                "edit_date": 1_775_000_200,
            }
        )

        self.assertEqual(self.client.message_fetches, [(-100123, 501, 5.0)])
        self.assertEqual(edited.message.content_text, "edited after restart")
        self.assertEqual(len(persisted), 1)

    def test_disabled_whitelist_entry_is_denied(self) -> None:
        collector = TelegramCollector(
            self.client,
            TelegramCollectorConfig(
                (ChatPolicy(chat_id=-100123, enabled=False),)
            ),
            clock=lambda: NOW,
        )
        event = collector.process_update(
            {"@type": "updateNewMessage", "message": _text_message()}
        )
        self.assertEqual(event.ignored.reason, "whitelist_disabled")

    def test_shutdown_drain_uses_the_normal_whitelist_first_sink(self) -> None:
        persisted = []
        ignored = []
        self.client.updates.extend(
            (
                {"@type": "updateNewMessage", "message": _text_message(message_id=81)},
                {
                    "@type": "updateNewMessage",
                    "message": _text_message(
                        chat_id=-999,
                        message_id=82,
                        text="must remain outside the accepted sink",
                    ),
                },
            )
        )
        collector = TelegramCollector(
            self.client,
            self.config,
            message_sink=persisted.append,
            ignored_sink=ignored.append,
            clock=lambda: NOW,
        )

        drained = collector.drain_pending_updates()

        self.assertEqual(drained, 2)
        self.assertEqual([message.message_id for message in persisted], [81])
        self.assertEqual(len(ignored), 1)
        self.assertEqual(ignored[0].reason, "not_whitelisted")


if __name__ == "__main__":
    unittest.main()
