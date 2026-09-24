from __future__ import annotations

import json
import re
import urllib.request
import hashlib
import threading
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing import Any, Callable
from urllib.parse import urlparse


FORBIDDEN_API_WORDS = {
    "account",
    "accounts",
    "bet",
    "bets",
    "brokerage",
    "execute",
    "execution",
    "order",
    "orders",
    "payment",
    "payments",
    "trade",
    "trades",
    "transfer",
    "transfers",
    "wallet",
    "wallets",
    "withdraw",
    "withdrawals",
}
FORBIDDEN_PROVIDER_KEYS = {
    "functioncall",
    "functions",
    "paralleltoolcalls",
    "toolchoice",
    "tools",
}
PROVIDER_ENDPOINT_SUFFIXES = {
    "responses": "/responses",
    "chat_completions": "/chat/completions",
}
PROVIDER_ALLOWED_FIELDS = {
    "responses": {
        "model",
        "input",
        "instructions",
        "max_output_tokens",
        "store",
        "thinking",
        "text",
    },
    "chat_completions": {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "stream",
        "response_format",
    },
}


class ExecutionBoundaryViolation(ValueError):
    pass


class TextRequestCancelled(ExecutionBoundaryViolation):
    pass


class TextRequestSendGate:
    """Linearize stop and send admission without holding a lock during I/O."""
    def __init__(self):
        self.event = threading.Event()
        self._lock = threading.Lock()
        self._host_stop_events = ()

    def bind_host_stop_event(self, event):
        with self._lock:
            self._host_stop_events += (event,)

    def cancel(self):
        with self._lock:
            self.event.set()

    @property
    def cancelled(self):
        return self.event.is_set() or any(event.is_set() for event in self._host_stop_events)

    @contextmanager
    def admission(self):
        with self._lock:
            if self.cancelled:
                raise TextRequestCancelled("request cancelled before send admission")
            yield


@dataclass(slots=True)
class AuthorizedTextRequest:
    endpoint: str
    body_sha256: str
    max_body_bytes: int
    timeout_seconds: int
    attempted: bool = False
    before_send: Callable[[], None] | None = None
    send_gate: TextRequestSendGate | None = None

    def check(self, request: urllib.request.Request) -> None:
        body = request.data or b""
        if (request.get_method() != "POST" or request.full_url != self.endpoint
                or len(body) > self.max_body_bytes
                or hashlib.sha256(body).hexdigest() != self.body_sha256):
            raise ExecutionBoundaryViolation("实际 Provider 请求与获准端点或完整参数不一致")


_authorized_text_request: ContextVar[AuthorizedTextRequest | None] = ContextVar("authorized_text_request", default=None)


@contextmanager
def authorized_text_request(policy: AuthorizedTextRequest):
    """Limit one synchronous pilot operation without changing ordinary callers."""
    if _authorized_text_request.get() is not None:
        raise ExecutionBoundaryViolation("受控请求授权不能嵌套")
    token = _authorized_text_request.set(policy)
    try:
        yield policy
    finally:
        _authorized_text_request.reset(token)


class _NoProviderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ExecutionBoundaryViolation("受控 Provider 请求不跟随重定向")


def open_text_provider_request(request: urllib.request.Request, *, timeout: int):
    policy = _authorized_text_request.get()
    if policy is None:
        return urllib.request.urlopen(request, timeout=timeout)
    policy.check(request)
    if policy.attempted:
        raise ExecutionBoundaryViolation("本次授权只允许一个 Provider HTTP 请求")
    if timeout != policy.timeout_seconds:
        raise ExecutionBoundaryViolation("实际 Provider 超时参数与授权不一致")
    opener = urllib.request.build_opener(_NoProviderRedirect())
    if policy.before_send is not None:
        policy.before_send()
    # All possibly blocking preparation precedes this final, in-memory gate.
    # Admission winning the lock starts this attempt; a later stop drains it.
    # Stop winning the lock forbids the opener call. Never hold this lock while
    # connecting, waiting for a response, or persisting a receipt.
    with policy.send_gate.admission() if policy.send_gate else nullcontext():
        if policy.attempted:
            raise ExecutionBoundaryViolation("本次授权只允许一个 Provider HTTP 请求")
        policy.attempted = True
    return opener.open(request, timeout=timeout)


def read_text_provider_response(response) -> str:
    if _authorized_text_request.get() is None:
        return response.read().decode("utf-8")
    content = response.read(2_000_001)
    if len(content) > 2_000_000:
        raise ExecutionBoundaryViolation("受控 Provider 响应超过读取上限")
    return content.decode("utf-8")


def text_generation_body(*, api: str, model: str, instructions: str, input_text: str,
                         max_output_tokens: int, json_output: bool = False,
                         thinking_disabled: bool = False,
                         completion_token_limit: bool = False) -> dict[str, Any]:
    if api == "chat_completions":
        body = {"model": model, "messages": [{"role": "system", "content": instructions},
                                              {"role": "user", "content": input_text}],
                "max_tokens": max_output_tokens, "stream": False}
        if completion_token_limit:
            body["max_completion_tokens"] = body.pop("max_tokens")
        if json_output:
            body["response_format"] = {"type": "json_object"}
    elif api == "responses":
        body = {"model": model, "instructions": instructions, "input": input_text,
                "max_output_tokens": max_output_tokens}
        if thinking_disabled:
            body["thinking"] = {"type": "disabled"}
        body["store"] = False
        if json_output:
            body["text"] = {"format": {"type": "json_object"}}
    else:
        raise ExecutionBoundaryViolation("不支持的文本生成协议")
    return body


def canonical_identifier(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _path_words(value: str) -> set[str]:
    path = urlparse(str(value or "")).path
    words: set[str] = set()
    for segment in path.split("/"):
        snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", segment)
        words.update(re.findall(r"[a-z0-9]+", snake.lower()))
    return words


def ensure_safe_api_path(path: str) -> None:
    dangerous = sorted(_path_words(path) & FORBIDDEN_API_WORDS)
    if dangerous:
        raise ExecutionBoundaryViolation(
            "服务端未启用账户、下单、支付、钱包或其他资金执行接口"
        )


def _provider_payload_issues(value: Any, *, path: str = "$") -> list[str]:
    issues: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            clean_key = canonical_identifier(key)
            if clean_key in FORBIDDEN_PROVIDER_KEYS:
                issues.append(f"{path}.{key}")
            issues.extend(_provider_payload_issues(nested, path=f"{path}.{key}"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, nested in enumerate(value):
            issues.extend(_provider_payload_issues(nested, path=f"{path}[{index}]"))
    return issues


def ensure_text_only_provider_payload(payload: Mapping[str, Any]) -> None:
    issues = _provider_payload_issues(payload)
    if issues:
        raise ExecutionBoundaryViolation(
            "Provider 请求只允许文本生成，禁止 tools/functions/tool_choice"
        )


def _validated_provider_url(base_url: str, endpoint: str) -> str:
    suffix = PROVIDER_ENDPOINT_SUFFIXES.get(endpoint)
    if not suffix:
        raise ExecutionBoundaryViolation("Provider 端点类型不在文本生成白名单中")
    parsed = urlparse(str(base_url or ""))
    hostname = str(parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise ExecutionBoundaryViolation("Provider Base URL 必须是有效 HTTP(S) 地址")
    if parsed.scheme == "http" and hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ExecutionBoundaryViolation("远程 Provider Base URL 必须使用 HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ExecutionBoundaryViolation("Provider Base URL 不能包含凭据、查询参数或片段")
    ensure_safe_api_path(parsed.path)
    return f"{str(base_url).rstrip('/')}{suffix}"


def _ensure_text_generation_schema(endpoint: str, payload: Mapping[str, Any]) -> None:
    allowed = PROVIDER_ALLOWED_FIELDS.get(endpoint)
    if not allowed:
        raise ExecutionBoundaryViolation("Provider 端点类型不在文本生成白名单中")
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        raise ExecutionBoundaryViolation(
            "Provider 文本请求包含未允许字段：" + ",".join(unknown[:8])
        )
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ExecutionBoundaryViolation("Provider 文本请求缺少有效 model")
    if endpoint == "responses":
        if not isinstance(payload.get("input"), str):
            raise ExecutionBoundaryViolation("Responses 请求只接受文本 input")
        if "instructions" in payload and not isinstance(payload.get("instructions"), str):
            raise ExecutionBoundaryViolation("Responses instructions 必须是文本")
        if "thinking" in payload and payload.get("thinking") != {"type": "disabled"}:
            raise ExecutionBoundaryViolation("Responses thinking 只允许显式 disabled")
    else:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ExecutionBoundaryViolation("Chat Completions 请求缺少 messages")
        for message in messages:
            if not isinstance(message, Mapping) or set(message) - {"role", "content"}:
                raise ExecutionBoundaryViolation("消息只允许 role/content 字段")
            if message.get("role") not in {"system", "user", "assistant"}:
                raise ExecutionBoundaryViolation("消息 role 不在文本对话白名单中")
            if not isinstance(message.get("content"), str):
                raise ExecutionBoundaryViolation("消息 content 必须是文本")
    if "max_tokens" in payload and "max_completion_tokens" in payload:
        raise ExecutionBoundaryViolation("输出上限不能同时使用两种计数参数")
    for token_field in ("max_output_tokens", "max_tokens", "max_completion_tokens"):
        if token_field in payload:
            token_value = payload.get(token_field)
            if isinstance(token_value, bool) or not isinstance(token_value, int) or token_value <= 0:
                raise ExecutionBoundaryViolation(f"{token_field} 必须是正整数")
    for boolean_field in ("store", "stream"):
        if boolean_field in payload and not isinstance(payload.get(boolean_field), bool):
            raise ExecutionBoundaryViolation(f"{boolean_field} 必须是布尔值")


def build_text_provider_request(
    base_url: str,
    endpoint: str,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str],
) -> urllib.request.Request:
    """Build the only permitted mutating HTTP request: text-only model inference."""

    ensure_text_only_provider_payload(payload)
    _ensure_text_generation_schema(endpoint, payload)
    url = _validated_provider_url(base_url, endpoint)
    request = urllib.request.Request(
        url,
        data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )
    policy = _authorized_text_request.get()
    if policy is not None:
        policy.check(request)
    return request
