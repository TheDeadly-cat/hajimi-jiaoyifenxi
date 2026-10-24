from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend.telegram_intelligence.config import (
    ChatPolicy,
    TelegramCollectorConfig,
)
from backend.telegram_intelligence.history import HistorySyncError, sync_history


CHAT_ID = -100123
BASE_TIME = datetime(2026, 8, 29, 6, 0, tzinfo=timezone.utc)


def _message(message_id: int, seconds_ago: int = 0, *, chat_id: int = CHAT_ID):
    return {
        "@type": "message",
        "id": message_id,
        "chat_id": chat_id,
        "date": int((BASE_TIME - timedelta(seconds=seconds_ago)).timestamp()),
        "sender_id": {"@type": "messageSenderUser", "user_id": 1},
        "content": {
            "@type": "messageText",
            "text": {
                "@type": "formattedText",
                "text": f"message {message_id}",
                "entities": [],
            },
        },
    }


class _Client:
    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def call(self, request):
        self.requests.append(dict(request))
        messages = self.pages.pop(0) if self.pages else []
        return {"@type": "messages", "messages": messages}


class _Event:
    kind = "message"


class _Collector:
    def __init__(self, *, enabled: bool = True):
        self.config = TelegramCollectorConfig(
            (ChatPolicy(chat_id=CHAT_ID, enabled=enabled),)
        )
        self.updates = []

    def process_update(self, update):
        self.updates.append(update)
        return _Event()


class TelegramHistoryTests(unittest.TestCase):
    def test_only_requested_whitelisted_chat_is_read_and_processed(self) -> None:
        client = _Client([[_message(3), _message(2), _message(1)]])
        collector = _Collector()
        result = sync_history(client, collector, CHAT_ID, limit=3)
        self.assertEqual(result.fetched_count, 3)
        self.assertEqual(result.processed_count, 3)
        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertEqual(request["chat_id"], CHAT_ID)
        self.assertEqual(request["@type"], "getChatHistory")
        self.assertNotIn("chat_list", request)
        self.assertEqual(
            [update["message"]["id"] for update in collector.updates],
            [3, 2, 1],
        )

    def test_paginates_at_tdlib_page_boundary_without_account_scan(self) -> None:
        first = [_message(message_id) for message_id in range(200, 100, -1)]
        second = [_message(100)]
        client = _Client([first, second])
        collector = _Collector()
        result = sync_history(client, collector, CHAT_ID, limit=101)
        self.assertEqual(result.fetched_count, 101)
        self.assertEqual(result.pages, 2)
        self.assertEqual(client.requests[0]["from_message_id"], 0)
        self.assertEqual(client.requests[1]["from_message_id"], 101)
        self.assertTrue(
            all(request["chat_id"] == CHAT_ID for request in client.requests)
        )

    def test_since_time_is_aware_and_stops_before_old_message(self) -> None:
        client = _Client(
            [[_message(3, 0), _message(2, 60), _message(1, 120)]]
        )
        collector = _Collector()
        cutoff = BASE_TIME - timedelta(seconds=90)
        result = sync_history(
            client,
            collector,
            CHAT_ID,
            limit=10,
            since_time=cutoff.astimezone(timezone(timedelta(hours=8))),
        )
        self.assertEqual(result.fetched_count, 2)
        self.assertTrue(result.stopped_at_since_time)
        self.assertEqual(len(collector.updates), 2)
        self.assertTrue(result.since_time.endswith("Z"))

    def test_disabled_or_missing_whitelist_fails_before_tdlib_call(self) -> None:
        client = _Client([[_message(1)]])
        with self.assertRaises(HistorySyncError):
            sync_history(client, _Collector(enabled=False), CHAT_ID)
        self.assertEqual(client.requests, [])
        missing = _Collector()
        missing.config = TelegramCollectorConfig()
        with self.assertRaises(HistorySyncError):
            sync_history(client, missing, CHAT_ID)
        self.assertEqual(client.requests, [])

    def test_limits_naive_time_and_cross_chat_response_fail_closed(self) -> None:
        collector = _Collector()
        for limit, maximum in ((0, 1000), (1001, 1000), (1, 0)):
            with self.subTest(limit=limit, maximum=maximum):
                with self.assertRaises(HistorySyncError):
                    sync_history(
                        _Client([]),
                        collector,
                        CHAT_ID,
                        limit=limit,
                        max_limit=maximum,
                    )
        with self.assertRaises(HistorySyncError):
            sync_history(
                _Client([]),
                collector,
                CHAT_ID,
                since_time=datetime(2026, 8, 29),
            )
        with self.assertRaises(HistorySyncError):
            sync_history(
                _Client([[_message(1, chat_id=-999)]]),
                collector,
                CHAT_ID,
            )

    def test_repeated_sync_is_passed_to_sink_for_idempotent_upsert(self) -> None:
        collector = _Collector()
        sync_history(_Client([[_message(1)]]), collector, CHAT_ID, limit=1)
        sync_history(_Client([[_message(1)]]), collector, CHAT_ID, limit=1)
        self.assertEqual(len(collector.updates), 2)


if __name__ == "__main__":
    unittest.main()
