from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from backend.telegram_intelligence.events import EventClusterer, EventObservation
from backend.telegram_intelligence.models import ForwardOrigin, SourceMessage


NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def message(
    text: str,
    *,
    chat_id: int,
    message_id: int,
    timestamp: datetime = NOW,
    forward_origin: ForwardOrigin | None = None,
) -> SourceMessage:
    return SourceMessage(
        source="telegram",
        chat_id=chat_id,
        chat_title=f"Source {chat_id}",
        chat_type="channel",
        message_id=message_id,
        sender_id=None,
        sender_name="",
        timestamp=timestamp,
        edit_timestamp=None,
        text=text,
        caption="",
        urls=(),
        entities=(),
        reply_to_message_id=None,
        forward_origin=forward_origin,
        media_type="",
        media_file_id="",
        telegram_link="",
        raw_language="en",
        is_edited=False,
        is_forwarded=forward_origin is not None,
        ingested_at=timestamp,
    )


class TelegramEventTests(unittest.TestCase):
    def test_related_messages_cluster_and_unique_sources_drive_developing_state(self) -> None:
        clusterer = EventClusterer()
        first_observation = EventObservation.from_message(
            message("NVIDIA launches a Blackwell GPU platform", chat_id=-1001, message_id=1),
            tickers=("NVDA",),
            keywords=("blackwell", "gpu"),
            category="AI",
            trust_score=75,
        )
        event, created = clusterer.add(first_observation, [])
        self.assertTrue(created.created)
        self.assertEqual(event.status, "new")

        second_observation = EventObservation.from_message(
            message(
                "NVDA announces its new Blackwell GPU launch",
                chat_id=-1002,
                message_id=2,
                timestamp=NOW + timedelta(minutes=5),
            ),
            tickers=("NVDA",),
            keywords=("blackwell", "gpu"),
            category="AI",
            trust_score=85,
        )
        merged, match = clusterer.add(second_observation, [event])
        self.assertFalse(match.created)
        self.assertGreaterEqual(match.score, 0.62)
        self.assertEqual(merged.message_count, 2)
        self.assertEqual(merged.source_count, 2)
        self.assertEqual(merged.status, "developing")
        self.assertEqual(merged.highest_trust_source, "Source -1002")

    def test_same_forward_origin_is_not_counted_as_independent_sources(self) -> None:
        origin = ForwardOrigin(
            kind="channel",
            chat_id=-2000,
            message_id=88,
            sender_name="Original Wire",
        )
        clusterer = EventClusterer()
        observations = [
            EventObservation.from_message(
                message("Micron raises HBM guidance", chat_id=-1001, message_id=1, forward_origin=origin),
                tickers=("MU",),
                keywords=("hbm", "guidance"),
                trust_score=60,
            ),
            EventObservation.from_message(
                message(
                    "Micron raises HBM guidance",
                    chat_id=-1002,
                    message_id=2,
                    timestamp=NOW + timedelta(minutes=1),
                    forward_origin=origin,
                ),
                tickers=("MU",),
                keywords=("hbm", "guidance"),
                trust_score=70,
            ),
        ]
        event = clusterer.create(observations[0])
        merged = clusterer.merge(event, observations[1])
        self.assertEqual(merged.message_count, 2)
        self.assertEqual(merged.source_count, 1)
        self.assertEqual(merged.status, "new")

    def test_same_forward_channel_different_posts_are_one_source(self) -> None:
        first_origin = ForwardOrigin(kind="channel", chat_id=-2000, message_id=88)
        second_origin = ForwardOrigin(kind="channel", chat_id=-2000, message_id=99)
        clusterer = EventClusterer()
        first = EventObservation.from_message(
            message(
                "Micron raises HBM guidance",
                chat_id=-1001,
                message_id=1,
                forward_origin=first_origin,
            ),
            tickers=("MU",),
            keywords=("hbm", "guidance"),
            trust_score=30,
        )
        second = EventObservation.from_message(
            message(
                "Micron raises HBM guidance",
                chat_id=-1002,
                message_id=2,
                forward_origin=second_origin,
            ),
            tickers=("MU",),
            keywords=("hbm", "guidance"),
            trust_score=30,
        )

        merged = clusterer.merge(clusterer.create(first), second)

        self.assertEqual(merged.source_count, 1)

    def test_conflicting_ticker_or_time_window_creates_new_event(self) -> None:
        clusterer = EventClusterer(time_window=timedelta(hours=2))
        first = EventObservation.from_message(
            message("NVDA launches a GPU", chat_id=-1001, message_id=1),
            tickers=("NVDA",),
            keywords=("gpu",),
        )
        event = clusterer.create(first)
        conflicting = EventObservation.from_message(
            message("AMD launches a GPU", chat_id=-1002, message_id=2),
            tickers=("AMD",),
            keywords=("gpu",),
        )
        new_event, result = clusterer.add(conflicting, [event])
        self.assertTrue(result.created)
        self.assertNotEqual(new_event.event_id, event.event_id)

        late = EventObservation.from_message(
            message(
                "NVDA launches another GPU",
                chat_id=-1003,
                message_id=3,
                timestamp=NOW + timedelta(hours=3),
            ),
            tickers=("NVDA",),
            keywords=("gpu",),
        )
        _late_event, late_result = clusterer.add(late, [event])
        self.assertTrue(late_result.created)

    def test_status_requires_evidence_and_trust_not_just_strong_language(self) -> None:
        clusterer = EventClusterer()
        rumor = EventObservation.from_message(
            message("BREAKING!!! Anonymous source says deal is done", chat_id=-1001, message_id=1),
            tickers=("AMD",),
            keywords=("deal",),
            trust_score=20,
            evidence_status="rumor",
        )
        self.assertEqual(clusterer.create(rumor).status, "new")
        confirmed = EventObservation.from_message(
            message("Company confirms the transaction", chat_id=-1002, message_id=2),
            tickers=("AMD",),
            keywords=("deal",),
            trust_score=95,
            evidence_status="confirmed",
        )
        self.assertEqual(clusterer.create(confirmed).status, "confirmed")
        denial = EventObservation.from_message(
            message("Official denial identifies the claim as false", chat_id=-1003, message_id=3),
            tickers=("AMD",),
            keywords=("deal",),
            trust_score=95,
            evidence_status="false_or_misleading",
        )
        self.assertEqual(clusterer.create(denial).status, "disputed")


if __name__ == "__main__":
    unittest.main()
