from __future__ import annotations

import json
import threading
import time
import unittest
from unittest.mock import patch

from backend.telegram_intelligence.tdlib import (
    CTypesTDLibTransport,
    FakeTDLibTransport,
    InteractiveAuthCallbacks,
    TDLibClient,
    TDLibReadOnlyViolation,
    TDLibRequestError,
    TDLibRequestTimeout,
    TDLibSettings,
    TDLibUpdateOverflow,
)


def _settings() -> TDLibSettings:
    return TDLibSettings(
        api_id=12345,
        api_hash="api-hash-not-a-real-secret",
        database_directory="C:/temp/tdlib-data",
        files_directory="C:/temp/tdlib-files",
    )


def _wait_for(callback, timeout: float = 1.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("timed out waiting for TDLib test state")


def _last_sent(transport: FakeTDLibTransport, request_type: str):
    matches = [
        request
        for request in transport.sent
        if request.get("@type") == request_type
    ]
    return matches[-1] if matches else None


class TDLibClientTests(unittest.TestCase):
    def test_read_only_allowlist_rejects_message_and_membership_mutations(self) -> None:
        transport = FakeTDLibTransport()
        client = TDLibClient(transport, _settings())

        for request_type in ("sendMessage", "joinChat", "leaveChat", "setMessageReaction"):
            with self.subTest(request_type=request_type):
                with self.assertRaises(TDLibReadOnlyViolation):
                    client.send({"@type": request_type})
        self.assertEqual(transport.sent, [])

    def test_one_receiver_and_extra_correlates_success(self) -> None:
        transport = FakeTDLibTransport()
        client = TDLibClient(transport, _settings(), receive_timeout=0.02)
        client.start()
        first = _wait_for(lambda: _last_sent(transport, "getAuthorizationState"))
        self.assertTrue(first["@extra"].startswith("telegram:"))

        result: list[object] = []

        def call() -> None:
            result.append(client.call({"@type": "getOption", "name": "version"}))

        worker = threading.Thread(target=call)
        worker.start()
        request = _wait_for(lambda: _last_sent(transport, "getOption"))
        transport.push(
            {
                "@type": "optionValueString",
                "value": "1.8",
                "@extra": request["@extra"],
            }
        )
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["value"], "1.8")
        self.assertEqual(transport.max_concurrent_receivers, 1)
        client.close(timeout=0.05)

    def test_exact_message_fetch_uses_read_only_get_message(self) -> None:
        transport = FakeTDLibTransport()
        client = TDLibClient(transport, _settings(), receive_timeout=0.02)
        client.start()
        result: list[object] = []

        worker = threading.Thread(
            target=lambda: result.append(client.get_message(-1001, 77))
        )
        worker.start()
        request = _wait_for(lambda: _last_sent(transport, "getMessage"))
        self.assertEqual(request["chat_id"], -1001)
        self.assertEqual(request["message_id"], 77)
        transport.push(
            {
                "@type": "message",
                "id": 77,
                "chat_id": -1001,
                "date": 1_775_000_000,
                "content": {"@type": "messageText", "text": {"text": "edited"}},
                "@extra": request["@extra"],
            }
        )
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["id"], 77)
        client.close(timeout=0.05)

    def test_update_queue_overflow_fails_fast_instead_of_deadlocking_receiver(self) -> None:
        transport = FakeTDLibTransport()
        client = TDLibClient(
            transport,
            _settings(),
            receive_timeout=0.02,
            update_queue_size=1,
        )
        client.start()
        transport.push(
            {
                "@type": "updateConnectionState",
                "state": {"@type": "connectionStateReady"},
            }
        )
        _wait_for(lambda: client.connection_state == "connectionStateReady")
        transport.push(
            {
                "@type": "updateChatTitle",
                "chat_id": -1001,
                "title": "overflow",
            }
        )
        _wait_for(lambda: client.dropped_update_count == 1)
        self.assertEqual(client.last_transport_error, "update_queue_full")
        self.assertEqual(client.connection_state, "connectionStateUpdateQueueFull")
        self.assertFalse(client.is_running)
        self.assertEqual(client.get_update(timeout=0.05)["@type"], "updateConnectionState")
        with self.assertRaises(TDLibUpdateOverflow):
            client.get_update(timeout=0.05)
        client.close(timeout=0.05)

    def test_correlated_error_is_typed(self) -> None:
        transport = FakeTDLibTransport()
        client = TDLibClient(transport, _settings(), receive_timeout=0.02)
        client.start()
        captured: list[BaseException] = []

        def call() -> None:
            try:
                client.call({"@type": "getOption", "name": "bad"})
            except BaseException as exc:
                captured.append(exc)

        worker = threading.Thread(target=call)
        worker.start()
        request = _wait_for(lambda: _last_sent(transport, "getOption"))
        transport.push(
            {
                "@type": "error",
                "code": 400,
                "message": "bad option",
                "@extra": request["@extra"],
            }
        )
        worker.join(1)
        self.assertIsInstance(captured[0], TDLibRequestError)
        self.assertEqual(captured[0].code, 400)
        client.close(timeout=0.05)

    def test_authorization_phone_code_password_and_ready(self) -> None:
        transport = FakeTDLibTransport()
        callback_calls: list[str] = []
        client = TDLibClient(
            transport,
            _settings(),
            auth=InteractiveAuthCallbacks(
                phone_number=lambda: callback_calls.append("phone") or "+15550001111",
                authentication_code=lambda: callback_calls.append("code") or "12345",
                password=lambda: callback_calls.append("password") or "2fa value",
            ),
            receive_timeout=0.02,
        )
        client.start()
        states = [
            "authorizationStateWaitTdlibParameters",
            "authorizationStateWaitPhoneNumber",
            "authorizationStateWaitCode",
            "authorizationStateWaitPassword",
            "authorizationStateReady",
        ]
        expected_requests = [
            "setTdlibParameters",
            "setAuthenticationPhoneNumber",
            "checkAuthenticationCode",
            "checkAuthenticationPassword",
            None,
        ]
        for state, expected in zip(states, expected_requests, strict=True):
            transport.push(
                {
                    "@type": "updateAuthorizationState",
                    "authorization_state": {"@type": state},
                }
            )
            if expected is not None:
                _wait_for(lambda value=expected: _last_sent(transport, value))
        self.assertTrue(client.wait_ready(1))
        self.assertEqual(callback_calls, ["phone", "code", "password"])
        parameters = _last_sent(transport, "setTdlibParameters")
        self.assertEqual(parameters["api_id"], 12345)
        self.assertEqual(
            _last_sent(transport, "setAuthenticationPhoneNumber")["phone_number"],
            "+15550001111",
        )
        client.close(timeout=0.05)

    def test_existing_session_ready_does_not_prompt(self) -> None:
        transport = FakeTDLibTransport()

        def forbidden():
            raise AssertionError("session reuse must not prompt")

        client = TDLibClient(
            transport,
            _settings(),
            auth=InteractiveAuthCallbacks(forbidden, forbidden, forbidden),
            receive_timeout=0.02,
        )
        client.start()
        transport.push(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateReady"},
            }
        )
        self.assertTrue(client.wait_ready(1))
        self.assertIsNone(_last_sent(transport, "setAuthenticationPhoneNumber"))
        client.close(timeout=0.05)

    def test_network_chat_and_user_updates_are_cached_without_messages(self) -> None:
        transport = FakeTDLibTransport()
        client = TDLibClient(transport, _settings(), receive_timeout=0.02)
        client.start()
        transport.push(
            {
                "@type": "updateNewChat",
                "chat": {
                    "@type": "chat",
                    "id": -1001,
                    "title": "Market",
                    "type": {"@type": "chatTypeSupergroup"},
                    "last_message": {"content": {"text": "must not be cached"}},
                },
            }
        )
        transport.push(
            {
                "@type": "updateUser",
                "user": {
                    "@type": "user",
                    "id": 9,
                    "first_name": "Ada",
                    "last_name": "Lovelace",
                    "phone_number": "not retained",
                },
            }
        )
        transport.push(
            {
                "@type": "updateConnectionState",
                "state": {"@type": "connectionStateReady"},
            }
        )
        _wait_for(lambda: client.chat(-1001))
        _wait_for(lambda: client.connection_state == "connectionStateReady")
        self.assertEqual(client.chat(-1001).title, "Market")
        self.assertEqual(
            client.sender_name(
                {"@type": "messageSenderUser", "user_id": 9}
            ),
            "Ada Lovelace",
        )
        self.assertFalse(hasattr(client.chat(-1001), "last_message"))
        client.close(timeout=0.05)

    def test_metadata_caches_evict_the_least_recently_used_entry_over_cap(self) -> None:
        client = TDLibClient(
            FakeTDLibTransport(),
            _settings(),
            metadata_cache_size=100,
        )
        for identifier in range(1, 101):
            client._update_metadata_cache(
                {
                    "@type": "chat",
                    "id": identifier,
                    "title": f"Chat {identifier}",
                    "type": {"@type": "chatTypePrivate"},
                }
            )
            client._update_metadata_cache(
                {
                    "@type": "user",
                    "id": identifier,
                    "first_name": f"User {identifier}",
                    "last_name": "",
                }
            )

        self.assertEqual(client.chat(1).title, "Chat 1")
        self.assertEqual(
            client.sender_name({"@type": "messageSenderUser", "user_id": 1}),
            "User 1",
        )
        client._update_metadata_cache(
            {
                "@type": "chat",
                "id": 101,
                "title": "Chat 101",
                "type": {"@type": "chatTypePrivate"},
            }
        )
        client._update_metadata_cache(
            {
                "@type": "user",
                "id": 101,
                "first_name": "User 101",
                "last_name": "",
            }
        )

        self.assertIsNone(client.chat(2))
        self.assertEqual(client.chat(1).title, "Chat 1")
        self.assertEqual(client.chat(101).title, "Chat 101")
        self.assertEqual(
            client.sender_name({"@type": "messageSenderUser", "user_id": 2}),
            "",
        )
        self.assertEqual(
            client.sender_name({"@type": "messageSenderUser", "user_id": 1}),
            "User 1",
        )
        self.assertEqual(
            client.sender_name({"@type": "messageSenderUser", "user_id": 101}),
            "User 101",
        )

    def test_list_chats_uses_one_overall_deadline_for_metadata_fetches(self) -> None:
        client = TDLibClient(FakeTDLibTransport(), _settings())
        calls: list[tuple[str, float]] = []

        def fake_call(request, *, timeout):
            calls.append((request["@type"], timeout))
            if request["@type"] == "getChats":
                return {"@type": "chats", "chat_ids": [1, 2]}
            if request.get("chat_id") == 1:
                return {
                    "@type": "chat",
                    "id": 1,
                    "title": "First",
                    "type": {"@type": "chatTypePrivate"},
                }
            raise AssertionError("the expired deadline must prevent another request")

        with (
            patch.object(client, "call", side_effect=fake_call),
            patch(
                "backend.telegram_intelligence.tdlib.time.monotonic",
                side_effect=[100.0, 100.1, 100.4, 101.0],
            ),
        ):
            with self.assertRaisesRegex(TDLibRequestTimeout, "overall deadline"):
                client.list_chats(limit=2, timeout=1.0)

        self.assertEqual([request_type for request_type, _ in calls], ["getChats", "getChat"])
        self.assertAlmostEqual(calls[0][1], 0.9)
        self.assertAlmostEqual(calls[1][1], 0.6)

    def test_missing_auth_callback_fails_closed_without_stopping_receiver(self) -> None:
        transport = FakeTDLibTransport()
        client = TDLibClient(transport, _settings(), receive_timeout=0.02)
        client.start()
        transport.push(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {
                    "@type": "authorizationStateWaitPhoneNumber"
                },
            }
        )
        _wait_for(lambda: client.last_auth_error)
        self.assertEqual(client.last_auth_error, "TDLibAuthInputRequired")
        self.assertTrue(client.is_running)
        self.assertIsNone(_last_sent(transport, "setAuthenticationPhoneNumber"))
        client.close(timeout=0.05)


class _ModernLibrary:
    def __init__(self) -> None:
        self.sent: list[tuple[int, dict[str, object]]] = []
        self.incoming: list[bytes] = []

    def td_create_client_id(self):
        return 7

    def td_send(self, client_id, raw):
        self.sent.append((client_id, json.loads(raw.decode("utf-8"))))

    def td_receive(self, timeout):
        return self.incoming.pop(0) if self.incoming else None

    def td_execute(self, raw):
        return b'{"@type":"ok"}'


class _LegacyLibrary:
    def __init__(self) -> None:
        self.handle = object()
        self.sent: list[tuple[object, dict[str, object]]] = []
        self.incoming: list[bytes] = []
        self.destroyed = None

    def td_json_client_create(self):
        return self.handle

    def td_json_client_send(self, handle, raw):
        self.sent.append((handle, json.loads(raw.decode("utf-8"))))

    def td_json_client_receive(self, handle, timeout):
        return self.incoming.pop(0) if self.incoming else None

    def td_json_client_execute(self, handle, raw):
        return b'{"@type":"ok"}'

    def td_json_client_destroy(self, handle):
        self.destroyed = handle


class TDLibCTypesTransportTests(unittest.TestCase):
    def test_transport_enforces_same_read_only_capability_boundary(self) -> None:
        library = _ModernLibrary()
        transport = CTypesTDLibTransport(library=library)

        with self.assertRaises(TDLibReadOnlyViolation):
            transport.send({"@type": "forwardMessages"})
        with self.assertRaises(TDLibReadOnlyViolation):
            transport.execute({"@type": "deleteMessages"})
        self.assertEqual(library.sent, [])
        transport.close()

    def test_modern_api_send_receive_execute(self) -> None:
        library = _ModernLibrary()
        transport = CTypesTDLibTransport(library=library)
        self.assertEqual(transport.mode, "modern")
        transport.send({"@type": "getMe"})
        self.assertEqual(library.sent[0][0], 7)
        library.incoming.append(b'{"@type":"ok","@client_id":7}')
        self.assertEqual(transport.receive(0.01)["@type"], "ok")
        self.assertEqual(transport.execute({"@type": "getTextEntities"})["@type"], "ok")
        transport.close()

    def test_modern_api_ignores_another_client(self) -> None:
        library = _ModernLibrary()
        library.incoming.extend(
            [
                b'{"@type":"ok","@client_id":99}',
                b'{"@type":"ok","@client_id":7,"value":"ours"}',
            ]
        )
        transport = CTypesTDLibTransport(library=library)
        self.assertEqual(transport.receive(0.1)["value"], "ours")

    def test_legacy_fallback_send_receive_and_destroy(self) -> None:
        library = _LegacyLibrary()
        transport = CTypesTDLibTransport(library=library)
        self.assertEqual(transport.mode, "legacy")
        transport.send({"@type": "getMe"})
        library.incoming.append(b'{"@type":"ok"}')
        self.assertEqual(transport.receive(0.01)["@type"], "ok")
        transport.close()
        self.assertIs(library.destroyed, library.handle)


if __name__ == "__main__":
    unittest.main()
