from __future__ import annotations

import copy
import ctypes
import json
import queue
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol


class TDLibError(RuntimeError):
    pass


class TDLibTransportError(TDLibError):
    pass


class TDLibRequestError(TDLibError):
    def __init__(self, message: str, *, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


class TDLibRequestTimeout(TDLibError):
    pass


class TDLibAuthInputRequired(TDLibError):
    pass


class TDLibUpdateOverflow(TDLibError):
    """The bounded update handoff overflowed; live collection must restart."""


class TDLibReadOnlyViolation(TDLibError):
    """Raised before TDLib receives a request outside this read-only product."""


# This is a capability boundary, not merely a list used by the CLI.  It contains
# only the reads required by the collector/history flow plus the minimum
# authorization, reconnect, and close lifecycle calls needed for an existing
# Telegram account.  Message sending, joining/leaving chats, reactions, member
# administration, forwarding, and every other mutation are deliberately absent.
TDLIB_ALLOWED_REQUEST_TYPES = frozenset(
    {
        "getAuthorizationState",
        "getOption",
        "getMe",
        "getChats",
        "getChat",
        "getMessage",
        "getChatHistory",
        "getTextEntities",
        "setTdlibParameters",
        "checkDatabaseEncryptionKey",
        "setAuthenticationPhoneNumber",
        "checkAuthenticationCode",
        "checkAuthenticationPassword",
        "setNetworkType",
        "close",
    }
)


def _validate_request_type(value: Mapping[str, Any]) -> str:
    request_type = value.get("@type")
    if type(request_type) is not str or not request_type:
        raise TDLibTransportError("TDLib request requires a non-empty @type")
    if request_type not in TDLIB_ALLOWED_REQUEST_TYPES:
        raise TDLibReadOnlyViolation("TDLib request is outside the read-only allowlist")
    return request_type


class TDLibTransport(Protocol):
    def send(self, request: Mapping[str, Any]) -> None: ...

    def receive(self, timeout: float) -> dict[str, Any] | None: ...

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any] | None: ...

    def close(self) -> None: ...


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    _validate_request_type(value)
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TDLibTransportError("TDLib request is not valid JSON") from exc


def _decode_payload(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode("utf-8")
    else:
        try:
            raw = ctypes.cast(value, ctypes.c_char_p).value
        except (TypeError, ValueError) as exc:
            raise TDLibTransportError("TDLib returned an invalid pointer") from exc
        if raw is None:
            return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise TDLibTransportError("TDLib returned invalid JSON") from exc
    if type(parsed) is not dict:
        raise TDLibTransportError("TDLib response must be a JSON object")
    return parsed


def _set_signature(
    function: Any,
    *,
    argtypes: list[Any],
    restype: Any,
) -> None:
    try:
        function.argtypes = argtypes
        function.restype = restype
    except (AttributeError, TypeError):
        # Test doubles can be ordinary Python callables.
        return


class CTypesTDLibTransport:
    """Official TDLib JSON C API transport.

    Current TDLib builds expose td_create_client_id/td_send/td_receive.
    Older supported builds expose the td_json_client_* family.  Exactly one
    caller may enter receive at a time.
    """

    def __init__(
        self,
        library_path: str | Path | None = None,
        *,
        library: Any | None = None,
    ) -> None:
        if library is None:
            if library_path is None:
                raise TDLibTransportError("a TDLib shared-library path is required")
            try:
                library = ctypes.CDLL(str(Path(library_path)))
            except OSError as exc:
                raise TDLibTransportError("unable to load the TDLib library") from exc
        self._library = library
        self._send_lock = threading.Lock()
        self._receive_lock = threading.Lock()
        self._closed = False

        modern_names = ("td_create_client_id", "td_send", "td_receive")
        if all(callable(getattr(library, name, None)) for name in modern_names):
            self.mode = "modern"
            self._create = library.td_create_client_id
            self._send = library.td_send
            self._receive = library.td_receive
            self._execute = getattr(library, "td_execute", None)
            _set_signature(self._create, argtypes=[], restype=ctypes.c_int)
            _set_signature(
                self._send,
                argtypes=[ctypes.c_int, ctypes.c_char_p],
                restype=None,
            )
            _set_signature(
                self._receive,
                argtypes=[ctypes.c_double],
                restype=ctypes.c_char_p,
            )
            if callable(self._execute):
                _set_signature(
                    self._execute,
                    argtypes=[ctypes.c_char_p],
                    restype=ctypes.c_char_p,
                )
            self.client_id = int(self._create())
            if self.client_id <= 0:
                raise TDLibTransportError("TDLib returned an invalid client id")
            self._legacy_client = None
            self._destroy = None
            return

        legacy_names = (
            "td_json_client_create",
            "td_json_client_send",
            "td_json_client_receive",
            "td_json_client_destroy",
        )
        if not all(callable(getattr(library, name, None)) for name in legacy_names):
            raise TDLibTransportError("unsupported TDLib JSON API")
        self.mode = "legacy"
        self._create = library.td_json_client_create
        self._send = library.td_json_client_send
        self._receive = library.td_json_client_receive
        self._execute = getattr(library, "td_json_client_execute", None)
        self._destroy = library.td_json_client_destroy
        _set_signature(self._create, argtypes=[], restype=ctypes.c_void_p)
        _set_signature(
            self._send,
            argtypes=[ctypes.c_void_p, ctypes.c_char_p],
            restype=None,
        )
        _set_signature(
            self._receive,
            argtypes=[ctypes.c_void_p, ctypes.c_double],
            restype=ctypes.c_char_p,
        )
        if callable(self._execute):
            _set_signature(
                self._execute,
                argtypes=[ctypes.c_void_p, ctypes.c_char_p],
                restype=ctypes.c_char_p,
            )
        _set_signature(
            self._destroy,
            argtypes=[ctypes.c_void_p],
            restype=None,
        )
        self._legacy_client = self._create()
        if not self._legacy_client:
            raise TDLibTransportError("TDLib failed to create a legacy client")
        self.client_id = None

    def send(self, request: Mapping[str, Any]) -> None:
        payload = _json_bytes(request)
        with self._send_lock:
            if self._closed:
                raise TDLibTransportError("TDLib transport is closed")
            if self.mode == "modern":
                self._send(self.client_id, payload)
            else:
                self._send(self._legacy_client, payload)

    def receive(self, timeout: float) -> dict[str, Any] | None:
        if type(timeout) not in {int, float} or isinstance(timeout, bool) or timeout < 0:
            raise ValueError("receive timeout must be non-negative")
        if not self._receive_lock.acquire(blocking=False):
            raise TDLibTransportError("TDLib receive already has an active caller")
        try:
            if self._closed:
                return None
            deadline = time.monotonic() + float(timeout)
            while True:
                remaining = max(0.0, deadline - time.monotonic())
                if self.mode == "modern":
                    raw = self._receive(remaining)
                else:
                    raw = self._receive(self._legacy_client, remaining)
                payload = _decode_payload(raw)
                if payload is None:
                    return None
                payload_client_id = payload.get("@client_id")
                if (
                    self.mode == "modern"
                    and type(payload_client_id) is int
                    and payload_client_id != self.client_id
                ):
                    if time.monotonic() >= deadline:
                        return None
                    continue
                return payload
        finally:
            self._receive_lock.release()

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        if not callable(self._execute):
            raise TDLibTransportError("this TDLib build does not expose execute")
        payload = _json_bytes(request)
        if self._closed:
            raise TDLibTransportError("TDLib transport is closed")
        if self.mode == "modern":
            raw = self._execute(payload)
        else:
            raw = self._execute(self._legacy_client, payload)
        return _decode_payload(raw)

    def close(self) -> None:
        with self._send_lock:
            if self._closed:
                return
            self._closed = True
            if self.mode == "legacy" and self._legacy_client:
                self._destroy(self._legacy_client)
                self._legacy_client = None


class FakeTDLibTransport:
    """Deterministic in-memory transport for completely offline tests."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.executed: list[dict[str, Any]] = []
        self._incoming: queue.Queue[dict[str, Any] | object] = queue.Queue()
        self._closed_marker = object()
        self._closed = False
        self._receive_guard = threading.Lock()
        self.receive_calls = 0
        self.max_concurrent_receivers = 0
        self._active_receivers = 0
        self._state_lock = threading.Lock()

    def send(self, request: Mapping[str, Any]) -> None:
        if self._closed:
            raise TDLibTransportError("fake TDLib transport is closed")
        _validate_request_type(request)
        self.sent.append(copy.deepcopy(dict(request)))

    def receive(self, timeout: float) -> dict[str, Any] | None:
        if not self._receive_guard.acquire(blocking=False):
            raise TDLibTransportError("fake transport received concurrently")
        with self._state_lock:
            self._active_receivers += 1
            self.max_concurrent_receivers = max(
                self.max_concurrent_receivers,
                self._active_receivers,
            )
            self.receive_calls += 1
        try:
            try:
                item = self._incoming.get(timeout=max(0.0, float(timeout)))
            except queue.Empty:
                return None
            if item is self._closed_marker:
                return None
            return copy.deepcopy(item)
        finally:
            with self._state_lock:
                self._active_receivers -= 1
            self._receive_guard.release()

    def execute(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        if self._closed:
            raise TDLibTransportError("fake TDLib transport is closed")
        _validate_request_type(request)
        self.executed.append(copy.deepcopy(dict(request)))
        return {"@type": "ok"}

    def push(self, payload: Mapping[str, Any]) -> None:
        if self._closed:
            raise TDLibTransportError("fake TDLib transport is closed")
        self._incoming.put_nowait(copy.deepcopy(dict(payload)))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._incoming.put_nowait(self._closed_marker)


@dataclass(frozen=True, slots=True)
class TDLibSettings:
    api_id: int
    api_hash: str = field(repr=False)
    database_directory: str
    files_directory: str
    database_encryption_key: str = field(default="", repr=False)
    system_language_code: str = "en"
    device_model: str = "AI Collaboration Studio"
    system_version: str = "Windows"
    application_version: str = "1.0"

    def __post_init__(self) -> None:
        if type(self.api_id) is not int or self.api_id <= 0:
            raise ValueError("api_id must be a positive integer")
        if type(self.api_hash) is not str or not self.api_hash.strip():
            raise ValueError("api_hash must be non-empty")
        for name in ("database_directory", "files_directory"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True, slots=True)
class InteractiveAuthCallbacks:
    phone_number: Callable[[], str] | None = field(default=None, repr=False)
    authentication_code: Callable[[], str] | None = field(default=None, repr=False)
    password: Callable[[], str] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class TDLibChat:
    chat_id: int
    title: str
    chat_type: str
    username: str = ""


@dataclass(frozen=True, slots=True)
class TDLibUser:
    user_id: int
    display_name: str


@dataclass(slots=True)
class _PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    response: dict[str, Any] | None = None


class TDLibClient:
    """Threaded TDLib session with one receive loop and @extra dispatch."""

    def __init__(
        self,
        transport: TDLibTransport,
        settings: TDLibSettings,
        *,
        auth: InteractiveAuthCallbacks | None = None,
        receive_timeout: float = 0.2,
        update_queue_size: int = 10_000,
        metadata_cache_size: int = 10_000,
    ) -> None:
        if receive_timeout <= 0:
            raise ValueError("receive_timeout must be positive")
        if type(update_queue_size) is not int or update_queue_size < 1:
            raise ValueError("update_queue_size must be positive")
        if (
            type(metadata_cache_size) is not int
            or not 100 <= metadata_cache_size <= 100_000
        ):
            raise ValueError("metadata_cache_size must be between 100 and 100000")
        self.transport = transport
        self.settings = settings
        self.auth = auth or InteractiveAuthCallbacks()
        self.receive_timeout = float(receive_timeout)
        self._updates: queue.Queue[dict[str, Any]] = queue.Queue(update_queue_size)
        self._pending: dict[str, _PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._update_overflowed = threading.Event()
        self._receiver: threading.Thread | None = None
        self._metadata_cache_size = metadata_cache_size
        self._chats: OrderedDict[int, TDLibChat] = OrderedDict()
        self._users: OrderedDict[int, TDLibUser] = OrderedDict()
        self._cache_lock = threading.Lock()
        self.authorization_state = "authorizationStateUnknown"
        self.connection_state = "connectionStateUnknown"
        self.last_transport_error = ""
        self.last_auth_error = ""
        self.dropped_update_count = 0

    @property
    def is_running(self) -> bool:
        receiver = self._receiver
        return receiver is not None and receiver.is_alive() and not self._stop.is_set()

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.is_running:
                return
            if self._closed.is_set():
                raise TDLibError("a closed TDLib client cannot be restarted")
            self._stop.clear()
            self._receiver = threading.Thread(
                target=self._receive_loop,
                name="telegram-tdlib-receiver",
                daemon=True,
            )
            self._receiver.start()
        self.send({"@type": "getAuthorizationState"})

    def _next_extra(self) -> str:
        return f"telegram:{uuid.uuid4().hex}"

    def _prepare_request(
        self,
        request: Mapping[str, Any],
        *,
        extra: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        if type(request) is not dict:
            request = dict(request)
        else:
            request = dict(request)
        _validate_request_type(request)
        if "@extra" in request:
            raise ValueError("@extra is owned by TDLibClient")
        correlation = extra or self._next_extra()
        request["@extra"] = correlation
        return request, correlation

    def send(self, request: Mapping[str, Any]) -> str:
        prepared, correlation = self._prepare_request(request)
        with self._send_lock:
            self.transport.send(prepared)
        return correlation

    def call(
        self,
        request: Mapping[str, Any],
        *,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        if not self.is_running:
            raise TDLibError("TDLib client must be started before call")
        if timeout <= 0:
            raise ValueError("call timeout must be positive")
        prepared, correlation = self._prepare_request(request)
        pending = _PendingRequest()
        with self._pending_lock:
            self._pending[correlation] = pending
        try:
            with self._send_lock:
                self.transport.send(prepared)
            if not pending.event.wait(timeout):
                raise TDLibRequestTimeout(
                    f"TDLib request timed out: {prepared['@type']}"
                )
            response = pending.response
            if response is None:
                raise TDLibError("TDLib request completed without a response")
            if response.get("@type") == "error":
                code = response.get("code")
                raise TDLibRequestError(
                    str(response.get("message") or "TDLib request failed"),
                    code=code if type(code) is int else 0,
                )
            return response
        finally:
            with self._pending_lock:
                self._pending.pop(correlation, None)

    def _receive_loop(self) -> None:
        backoff = 0.05
        while not self._stop.is_set():
            try:
                payload = self.transport.receive(self.receive_timeout)
                backoff = 0.05
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.last_transport_error = type(exc).__name__
                self.connection_state = "connectionStateTransportRetry"
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 2.0)
                continue
            if payload is None:
                continue
            try:
                self._dispatch(payload)
            except Exception as exc:
                # Keep the sole receiver alive; never include payload or secrets.
                self.last_transport_error = type(exc).__name__

    def _dispatch(self, payload: dict[str, Any]) -> None:
        payload_type = payload.get("@type")
        if payload_type == "updateAuthorizationState":
            state = payload.get("authorization_state")
            if type(state) is dict:
                self._handle_authorization_state(state)
        elif isinstance(payload_type, str) and payload_type.startswith(
            "authorizationState"
        ):
            self._handle_authorization_state(payload)
        if payload_type == "updateConnectionState":
            state = payload.get("state")
            if type(state) is dict and type(state.get("@type")) is str:
                self.connection_state = state["@type"]
        self._update_metadata_cache(payload)

        correlation = payload.get("@extra")
        delivered = False
        if isinstance(correlation, str):
            with self._pending_lock:
                pending = self._pending.get(correlation)
            if pending is not None:
                pending.response = payload
                pending.event.set()
                delivered = True
        if (
            isinstance(payload_type, str)
            and payload_type.startswith("update")
        ) or (not delivered and payload_type == "error"):
            try:
                self._updates.put_nowait(payload)
            except queue.Full:
                # Never block the sole TDLib receiver: it also dispatches
                # correlated replies and authorization state.  Overflow is
                # explicit and observable so operators can stop/recover rather
                # than silently deadlocking all requests.
                self.dropped_update_count += 1
                self.last_transport_error = "update_queue_full"
                self.connection_state = "connectionStateUpdateQueueFull"
                self._update_overflowed.set()
                self._stop.set()

    def _auth_value(
        self,
        callback: Callable[[], str] | None,
        label: str,
        *,
        strip: bool,
    ) -> str:
        if callback is None:
            raise TDLibAuthInputRequired(f"interactive {label} callback is required")
        value = callback()
        if type(value) is not str:
            raise TDLibAuthInputRequired(f"interactive {label} must return text")
        clean = value.strip() if strip else value
        if not clean:
            raise TDLibAuthInputRequired(f"interactive {label} returned no value")
        return clean

    def _handle_authorization_state(self, state: Mapping[str, Any]) -> None:
        state_type = state.get("@type")
        if type(state_type) is not str:
            return
        self.authorization_state = state_type
        try:
            if state_type == "authorizationStateWaitTdlibParameters":
                self.send(
                    {
                        "@type": "setTdlibParameters",
                        "use_test_dc": False,
                        "database_directory": self.settings.database_directory,
                        "files_directory": self.settings.files_directory,
                        "database_encryption_key": self.settings.database_encryption_key,
                        "use_file_database": False,
                        "use_chat_info_database": True,
                        "use_message_database": True,
                        "use_secret_chats": False,
                        "api_id": self.settings.api_id,
                        "api_hash": self.settings.api_hash,
                        "system_language_code": self.settings.system_language_code,
                        "device_model": self.settings.device_model,
                        "system_version": self.settings.system_version,
                        "application_version": self.settings.application_version,
                    }
                )
            elif state_type == "authorizationStateWaitEncryptionKey":
                self.send(
                    {
                        "@type": "checkDatabaseEncryptionKey",
                        "encryption_key": self.settings.database_encryption_key,
                    }
                )
            elif state_type == "authorizationStateWaitPhoneNumber":
                phone = self._auth_value(
                    self.auth.phone_number,
                    "phone number",
                    strip=True,
                )
                self.send(
                    {
                        "@type": "setAuthenticationPhoneNumber",
                        "phone_number": phone,
                        "settings": {
                            "@type": "phoneNumberAuthenticationSettings",
                        },
                    }
                )
            elif state_type == "authorizationStateWaitCode":
                code = self._auth_value(
                    self.auth.authentication_code,
                    "authentication code",
                    strip=True,
                )
                self.send({"@type": "checkAuthenticationCode", "code": code})
            elif state_type == "authorizationStateWaitPassword":
                password = self._auth_value(
                    self.auth.password,
                    "2FA password",
                    strip=False,
                )
                self.send(
                    {"@type": "checkAuthenticationPassword", "password": password}
                )
            elif state_type == "authorizationStateReady":
                self.last_auth_error = ""
                self._ready.set()
            elif state_type in {
                "authorizationStateLoggingOut",
                "authorizationStateClosing",
                "authorizationStateClosed",
            }:
                self._ready.clear()
                if state_type == "authorizationStateClosed":
                    self._closed.set()
        except Exception as exc:
            self.last_auth_error = type(exc).__name__

    @staticmethod
    def _chat_from_object(value: Mapping[str, Any]) -> TDLibChat | None:
        chat_id = value.get("id")
        if type(chat_id) is not int:
            return None
        title = value.get("title")
        chat_type_value = value.get("type")
        chat_type = ""
        if type(chat_type_value) is dict:
            raw_type = chat_type_value.get("@type")
            if type(raw_type) is str:
                chat_type = raw_type.removeprefix("chatType")
        username = ""
        usernames = value.get("usernames")
        if type(usernames) is dict:
            active = usernames.get("active_usernames")
            if type(active) is list and active and type(active[0]) is str:
                username = active[0]
        return TDLibChat(
            chat_id=chat_id,
            title=title if type(title) is str else "",
            chat_type=chat_type,
            username=username,
        )

    def _update_metadata_cache(self, payload: Mapping[str, Any]) -> None:
        payload_type = payload.get("@type")
        chat: TDLibChat | None = None
        if payload_type == "chat":
            chat = self._chat_from_object(payload)
        elif payload_type == "updateNewChat":
            raw_chat = payload.get("chat")
            if type(raw_chat) is dict:
                chat = self._chat_from_object(raw_chat)
        if chat is not None:
            with self._cache_lock:
                self._cache_store(self._chats, chat.chat_id, chat)
        if payload_type == "updateChatTitle":
            chat_id = payload.get("chat_id")
            title = payload.get("title")
            if type(chat_id) is int and type(title) is str:
                with self._cache_lock:
                    current = self._chats.get(chat_id)
                    self._cache_store(
                        self._chats,
                        chat_id,
                        (
                            replace(current, title=title)
                            if current is not None
                            else TDLibChat(chat_id, title, "")
                        ),
                    )
        user_value: Mapping[str, Any] | None = None
        if payload_type == "user":
            user_value = payload
        elif payload_type == "updateUser" and type(payload.get("user")) is dict:
            user_value = payload["user"]
        if user_value is not None:
            user_id = user_value.get("id")
            if type(user_id) is int:
                first = user_value.get("first_name")
                last = user_value.get("last_name")
                display = " ".join(
                    part
                    for part in (
                        first if type(first) is str else "",
                        last if type(last) is str else "",
                    )
                    if part
                )
                with self._cache_lock:
                    self._cache_store(
                        self._users,
                        user_id,
                        TDLibUser(user_id, display),
                    )

    def _cache_store(
        self,
        cache: OrderedDict[Any, Any],
        key: Any,
        value: Any,
    ) -> None:
        cache.pop(key, None)
        cache[key] = value
        while len(cache) > self._metadata_cache_size:
            cache.popitem(last=False)

    def get_update(self, timeout: float | None = None) -> dict[str, Any] | None:
        try:
            return self._updates.get(timeout=timeout)
        except queue.Empty:
            if self._update_overflowed.is_set():
                raise TDLibUpdateOverflow(
                    "TDLib update queue overflowed; bounded history recovery is required"
                )
            return None

    def chat(self, chat_id: int) -> TDLibChat | None:
        with self._cache_lock:
            chat = self._chats.get(chat_id)
            if chat is not None:
                self._chats.move_to_end(chat_id)
            return chat

    def sender_name(self, sender: Any) -> str:
        if type(sender) is not dict:
            return ""
        sender_type = sender.get("@type")
        if sender_type == "messageSenderUser":
            user_id = sender.get("user_id")
            if type(user_id) is int:
                with self._cache_lock:
                    user = self._users.get(user_id)
                    if user is not None:
                        self._users.move_to_end(user_id)
                return user.display_name if user is not None else ""
        if sender_type == "messageSenderChat":
            chat_id = sender.get("chat_id")
            if type(chat_id) is int:
                chat = self.chat(chat_id)
                return chat.title if chat is not None else ""
        return ""

    def get_message(
        self,
        chat_id: int,
        message_id: int,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        if type(chat_id) is not int or type(message_id) is not int:
            raise ValueError("chat_id and message_id must be integers")
        response = self.call(
            {
                "@type": "getMessage",
                "chat_id": chat_id,
                "message_id": message_id,
            },
            timeout=timeout,
        )
        if response.get("@type") != "message":
            raise TDLibError("getMessage returned an invalid message")
        return response

    def list_chats(
        self,
        *,
        limit: int = 100,
        timeout: float = 10.0,
    ) -> tuple[TDLibChat, ...]:
        if type(limit) is not int or not 1 <= limit <= 1_000:
            raise ValueError("chat limit must be from 1 to 1000")
        if type(timeout) not in {int, float} or timeout <= 0:
            raise ValueError("chat listing timeout must be positive")
        deadline = time.monotonic() + float(timeout)

        def remaining() -> float:
            value = deadline - time.monotonic()
            if value <= 0:
                raise TDLibRequestTimeout("chat listing exceeded its overall deadline")
            return value

        response = self.call(
            {
                "@type": "getChats",
                "chat_list": {"@type": "chatListMain"},
                "limit": limit,
            },
            timeout=remaining(),
        )
        ids = response.get("chat_ids")
        if type(ids) is not list:
            raise TDLibError("getChats returned no chat_ids list")
        result: list[TDLibChat] = []
        for chat_id in ids:
            if type(chat_id) is not int:
                continue
            chat = self.chat(chat_id)
            if chat is None:
                fetched = self.call(
                    {"@type": "getChat", "chat_id": chat_id},
                    timeout=remaining(),
                )
                self._update_metadata_cache(fetched)
                chat = self.chat(chat_id)
            if chat is not None:
                result.append(chat)
        return tuple(result)

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def request_reconnect(self) -> str:
        if self._closed.is_set():
            raise TDLibError("TDLib client is closed")
        if not self.is_running:
            self.start()
            return ""
        return self.send(
            {"@type": "setNetworkType", "type": {"@type": "networkTypeOther"}}
        )

    def close(self, *, timeout: float = 2.0) -> None:
        if timeout < 0:
            raise ValueError("close timeout must be non-negative")
        with self._lifecycle_lock:
            receiver = self._receiver
            if receiver is None:
                self.transport.close()
                self._closed.set()
                return
            if receiver.is_alive() and not self._closed.is_set():
                try:
                    self.send({"@type": "close"})
                except TDLibError:
                    pass
        self._closed.wait(timeout)
        self._stop.set()
        if receiver is not threading.current_thread():
            receiver.join(timeout=max(timeout, self.receive_timeout * 2))
        self.transport.close()
        with self._pending_lock:
            pending_values = tuple(self._pending.values())
        for pending in pending_values:
            pending.event.set()
