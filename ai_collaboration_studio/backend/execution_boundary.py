from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any
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
        "stream",
        "response_format",
    },
}


class ExecutionBoundaryViolation(ValueError):
    pass


_TELEGRAM_BOT_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{10,256}$")
_TELEGRAM_DESTINATION_PATTERN = re.compile(r"^-?[1-9]\d*$")
_MAX_TELEGRAM_ALERT_CHARS = 4_000


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
    token_field = "max_output_tokens" if endpoint == "responses" else "max_tokens"
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
    return urllib.request.Request(
        url,
        data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        headers=dict(headers),
        method="POST",
    )


def build_output_only_telegram_bot_request(
    api_base_url: str,
    *,
    token: str,
    destination_chat_id: str,
    text: str,
) -> urllib.request.Request:
    """Build the fixed, output-only Telegram ``sendMessage`` request.

    This boundary exposes no arbitrary Bot method, destination, request body,
    account action, forwarding field, or trading/execution capability.
    """

    base = str(api_base_url or "").rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ExecutionBoundaryViolation(
            "Telegram Bot API base URL must be an HTTPS origin"
        )
    clean_token = str(token or "").strip()
    clean_destination = str(destination_chat_id or "").strip()
    if not _TELEGRAM_BOT_TOKEN_PATTERN.fullmatch(clean_token):
        raise ExecutionBoundaryViolation("Telegram Bot token is invalid")
    if not _TELEGRAM_DESTINATION_PATTERN.fullmatch(clean_destination):
        raise ExecutionBoundaryViolation(
            "Telegram Bot destination must be a non-zero numeric chat ID"
        )
    if type(text) is not str or not text or len(text) > _MAX_TELEGRAM_ALERT_CHARS:
        raise ExecutionBoundaryViolation("Telegram Bot alert text is invalid")
    body = urllib.parse.urlencode(
        {
            "chat_id": clean_destination,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    return urllib.request.Request(
        f"{base}/bot{clean_token}/sendMessage",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "AICollaborationStudio/0.2",
        },
        method="POST",
    )
