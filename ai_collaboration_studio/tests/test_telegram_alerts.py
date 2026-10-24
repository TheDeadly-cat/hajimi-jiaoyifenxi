from __future__ import annotations

import json
import re
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from backend.telegram_intelligence import bot as bot_module
from backend.telegram_intelligence.alerts import AlertAction, AlertCooldown
from backend.telegram_intelligence.bot import FakeBot, TelegramBot, format_alert


NOW = datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc)


class FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class OversizedHTTPResponse(FakeHTTPResponse):
    def __init__(self) -> None:
        super().__init__({})

    def read(self, *_args: object) -> bytes:
        return b"x" * (512 * 1024 + 1)


def _event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_id": "evt-1",
        "verification_status": "rumor",
        "direction": "neutral",
        "trust_score": 25,
        "last_alert_at": (NOW - timedelta(seconds=60)).isoformat(),
    }
    event.update(overrides)
    return event


class AlertCooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = AlertCooldown(window_seconds=300)

    def test_first_event_alerts_and_ordinary_duplicate_is_suppressed(self) -> None:
        first = self.policy.decide(None, _event(), now=NOW)
        duplicate = self.policy.decide(_event(), _event(), now=NOW)

        self.assertEqual(first.action, AlertAction.ALERT)
        self.assertEqual(first.reason, "first_event_alert")
        self.assertTrue(first.should_send)
        self.assertEqual(duplicate.action, AlertAction.SUPPRESS)
        self.assertEqual(duplicate.reason, "ordinary_duplicate_within_cooldown")

    def test_higher_trust_confirmation_is_an_update_inside_cooldown(self) -> None:
        decision = self.policy.decide(
            _event(verification_status="rumor", trust_score=25),
            _event(verification_status="confirmed", trust_score=95),
            now=NOW,
        )

        self.assertEqual(decision.action, AlertAction.UPDATE)
        self.assertEqual(decision.reason, "higher_trust_confirmation")
        self.assertTrue(decision.is_update)

    def test_direction_change_is_an_update_inside_cooldown(self) -> None:
        decision = self.policy.decide(
            _event(direction="bullish"),
            _event(direction="bearish"),
            now=NOW,
        )

        self.assertEqual(decision.action, AlertAction.UPDATE)
        self.assertEqual(decision.reason, "direction_changed")

    def test_official_denial_is_an_urgent_update(self) -> None:
        decision = self.policy.decide(
            _event(),
            _event(official_denial=True, verification_status="false_or_misleading"),
            now=NOW,
        )

        self.assertEqual(decision.action, AlertAction.URGENT_UPDATE)
        self.assertTrue(decision.urgent)
        self.assertEqual(decision.reason, "official_denial")

    def test_model_claims_cannot_trigger_official_denial_bypass(self) -> None:
        decision = self.policy.decide(
            _event(),
            _event(
                event_type="official_denial",
                risk_flags=["official_denial", "official_source"],
                verification_status="false_or_misleading",
            ),
            now=NOW,
        )

        self.assertEqual(decision.action, AlertAction.SUPPRESS)
        self.assertEqual(decision.reason, "ordinary_duplicate_within_cooldown")
        self.assertFalse(decision.urgent)

    def test_elapsed_cooldown_allows_a_new_alert(self) -> None:
        previous = _event(last_alert_at=(NOW - timedelta(seconds=301)).isoformat())

        decision = self.policy.decide(previous, _event(), now=NOW)

        self.assertEqual(decision.action, AlertAction.ALERT)
        self.assertEqual(decision.reason, "cooldown_elapsed")

    def test_invalid_or_future_previous_timestamp_suppresses_ordinary_duplicate(self) -> None:
        invalid = self.policy.decide(
            _event(last_alert_at="not-a-date"),
            _event(),
            now=NOW,
        )
        future = self.policy.decide(
            _event(last_alert_at=(NOW + timedelta(seconds=1)).isoformat()),
            _event(),
            now=NOW,
        )

        self.assertEqual(invalid.reason, "previous_alert_time_invalid")
        self.assertEqual(future.reason, "clock_skew")
        self.assertFalse(invalid.should_send)
        self.assertFalse(future.should_send)


class TelegramBotTests(unittest.TestCase):
    def test_rumor_and_confirmed_have_visually_distinct_escaped_headers(self) -> None:
        common = {
            "importance_score": 92,
            "confidence_score": 60,
            "tickers": ["NVDA"],
            "category": "AI <Hardware>",
            "direction": "bullish",
            "summary": "Untrusted <b>markup</b>",
            "recommended_action": "alert",
        }
        rumor = format_alert({**common, "verification_status": "rumor"})
        confirmed = format_alert({**common, "verification_status": "confirmed"})

        self.assertIn("RUMOR / UNCONFIRMED", rumor)
        self.assertNotIn("✅ CONFIRMED", rumor)
        self.assertIn("✅ CONFIRMED", confirmed)
        self.assertNotIn("RUMOR / UNCONFIRMED", confirmed)
        self.assertIn("AI &lt;Hardware&gt;", rumor)
        self.assertIn("Untrusted &lt;b&gt;markup&lt;/b&gt;", rumor)

    def test_first_seen_defaults_to_asia_shanghai_display_time(self) -> None:
        rendered = format_alert(
            {
                "verification_status": "confirmed",
                "summary": "fixture",
                "first_seen_at": "2026-08-29T04:00:00Z",
            }
        )

        self.assertIn("2026-08-29 12:00:00 UTC+08:00", rendered)

    def test_expansion_heavy_html_stays_bounded_with_complete_markup(self) -> None:
        expansion_heavy = "<&" * 2_000
        rendered = format_alert(
            {
                "verification_status": expansion_heavy,
                "tickers": expansion_heavy,
                "category": expansion_heavy,
                "direction": expansion_heavy,
                "summary": expansion_heavy,
                "source_name": expansion_heavy,
                "first_seen_at": expansion_heavy,
                "risk_flags": [expansion_heavy],
                "recommended_action": expansion_heavy,
                "telegram_link": "https://t.me/" + expansion_heavy,
            }
        )

        self.assertLessEqual(len(rendered), 4_000)
        self.assertIn("&lt;&amp;", rendered)
        stack: list[str] = []
        for match in re.finditer(r"<(/?)(b|i|a)(?:\s+[^<>]*)?>", rendered):
            closing, tag = match.groups()
            if not closing:
                stack.append(tag)
                continue
            self.assertTrue(stack, f"unexpected closing tag: {match.group(0)}")
            self.assertEqual(stack.pop(), tag)
        self.assertEqual(stack, [])
        without_markup = re.sub(
            r"</?(?:b|i|a)(?:\s+[^<>]*)?>",
            "",
            rendered,
        )
        without_entities = re.sub(
            r"&(?:amp|lt|gt|quot|#x27);",
            "",
            without_markup,
        )
        self.assertNotRegex(without_entities, r"[<>&]")

    def test_dry_run_returns_would_send_without_network_or_token(self) -> None:
        bot = TelegramBot(
            token="",
            destination_chat_id="-100123",
            dry_run=True,
        )
        with patch("backend.telegram_intelligence.bot._urlopen_no_redirect") as urlopen:
            result = bot.send_alert({
                "verification_status": "unconfirmed",
                "summary": "fixture",
            })

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "WOULD_SEND")
        self.assertEqual(result.destination_chat_id, "-100123")
        urlopen.assert_not_called()

    def test_live_send_uses_only_the_fixed_destination(self) -> None:
        bot = TelegramBot(
            token="1234567890:fixture_TOKEN",
            destination_chat_id="-100999",
            api_base_url="https://telegram.invalid",
        )
        with patch(
            "backend.telegram_intelligence.bot._urlopen_no_redirect",
            return_value=FakeHTTPResponse({"ok": True, "result": {"message_id": 77}}),
        ) as urlopen:
            result = bot.send_alert({
                "verification_status": "confirmed",
                "summary": "fixture",
                "destination_chat_id": "-100ATTACKER",
            })

        request = urlopen.call_args.args[0]
        body = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(
            request.full_url,
            "https://telegram.invalid/bot1234567890:fixture_TOKEN/sendMessage",
        )
        self.assertEqual(body["chat_id"], ["-100999"])
        self.assertEqual(body["parse_mode"], ["HTML"])
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "SENT")
        self.assertEqual(result.message_id, "77")

    def test_live_send_rejects_redirect_without_falling_back_to_urlopen(self) -> None:
        bot = TelegramBot(
            token="1234567890:fixture_TOKEN",
            destination_chat_id="-100999",
            api_base_url="https://telegram.invalid",
        )
        handler = next(
            item
            for item in bot_module._NO_REDIRECT_OPENER.handlers
            if isinstance(item, bot_module._NoRedirectHandler)
        )
        request = urllib.request.Request("https://telegram.invalid/original")
        self.assertIsNone(
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {"Location": "https://redirect.invalid/target"},
                "https://redirect.invalid/target",
            )
        )
        redirect = urllib.error.HTTPError(
            request.full_url,
            302,
            "Found",
            {"Location": "https://redirect.invalid/target"},
            None,
        )
        self.addCleanup(redirect.close)
        with (
            patch.object(
                bot_module._NO_REDIRECT_OPENER,
                "open",
                side_effect=redirect,
            ) as opener,
            patch.object(
                urllib.request,
                "urlopen",
                side_effect=AssertionError("redirect fallback must stay offline"),
            ) as urlopen,
        ):
            result = bot.send_alert(
                {"verification_status": "confirmed", "summary": "fixture"}
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "http_status")
        opener.assert_called_once()
        urlopen.assert_not_called()

    def test_constructor_rejects_username_and_nonnumeric_destinations(self) -> None:
        for destination in ("@market_alerts", "not-a-number", "-100abc"):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, "numeric chat ID"):
                    TelegramBot(
                        token="1234567890:fixture_TOKEN",
                        destination_chat_id=destination,
                        dry_run=True,
                    )

    def test_oversized_bot_response_fails_closed(self) -> None:
        bot = TelegramBot(
            token="1234567890:fixture_TOKEN",
            destination_chat_id="-100999",
            api_base_url="https://telegram.invalid",
        )
        with patch(
            "backend.telegram_intelligence.bot._urlopen_no_redirect",
            return_value=OversizedHTTPResponse(),
        ):
            result = bot.send_alert(
                {"verification_status": "confirmed", "summary": "fixture"}
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error_code, "invalid_response")

    def test_fake_bot_records_output_without_side_effects(self) -> None:
        bot = FakeBot(destination_chat_id="fixture-destination")

        result = bot.send_alert({
            "verification_status": "analysis",
            "summary": "fixture",
        })

        self.assertEqual(result.status, "WOULD_SEND")
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("OPINION / ANALYSIS", bot.sent[0]["text"])


if __name__ == "__main__":
    unittest.main()
