from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any, Protocol
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..execution_boundary import (
    ExecutionBoundaryViolation,
    build_output_only_telegram_bot_request,
)


_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9:_-]{10,256}$")
_DESTINATION_PATTERN = re.compile(r"^-?[1-9]\d*$")
_MAX_BOT_RESPONSE_BYTES = 512 * 1024
_MAX_ALERT_HTML_CHARS = 4_000


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Any,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def _urlopen_no_redirect(request: Any, *, timeout: float) -> Any:
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


def _bounded_response_bytes(response: Any) -> bytes:
    try:
        raw = response.read(_MAX_BOT_RESPONSE_BYTES + 1)
    except TypeError:
        raw = response.read()
    if not isinstance(raw, bytes) or len(raw) > _MAX_BOT_RESPONSE_BYTES:
        raise ValueError("Bot response exceeds the allowed size")
    return raw


@dataclass(frozen=True, slots=True)
class BotSendResult:
    ok: bool
    status: str
    destination_chat_id: str
    text: str
    message_id: str = ""
    error_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "destination_chat_id": self.destination_chat_id,
            "text": self.text,
            "message_id": self.message_id,
            "error_code": self.error_code,
        }


class Bot(Protocol):
    def send_alert(self, alert: Mapping[str, Any] | Any) -> BotSendResult: ...


def _mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        payload = dict(value)
    else:
        converter = getattr(value, "as_dict", None)
        payload = converter() if callable(converter) else {}
    nested = payload.get("analysis")
    if isinstance(nested, Mapping):
        merged = dict(nested)
        for key, item in payload.items():
            if key != "analysis" and key not in merged:
                merged[key] = item
        return merged
    return payload


def _text(value: Any, *, default: str = "", limit: int = 2_000) -> str:
    if value is None:
        return default
    clean = str(value).replace("\x00", " ").strip()
    return clean[:limit] if clean else default


def _list_text(value: Any, *, limit: int = 10) -> str:
    if isinstance(value, str):
        return _text(value, limit=500)
    if not isinstance(value, (list, tuple)):
        return ""
    return ", ".join(_text(item, limit=100) for item in value[:limit] if _text(item, limit=100))


def _escape_bounded(value: str, *, limit: int, quote: bool = False) -> str:
    """Escape whole characters without ever cutting an HTML entity."""

    output: list[str] = []
    size = 0
    truncated = False
    for character in value:
        fragment = escape(character, quote=quote)
        if size + len(fragment) > limit:
            truncated = True
            break
        output.append(fragment)
        size += len(fragment)
    if truncated and size + 1 <= limit:
        output.append("…")
    return "".join(output)


def _score(value: Any) -> int:
    if type(value) is bool:
        return 0
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError, OverflowError):
        return 0


def _status_banner(status: str) -> str:
    normalized = status.strip().lower()
    if normalized == "confirmed":
        return "✅ CONFIRMED"
    if normalized == "likely":
        return "⚠️ LIKELY / NOT CONFIRMED"
    if normalized in {"rumor", "unconfirmed"}:
        return "🚨 RUMOR / UNCONFIRMED"
    if normalized in {"opinion", "analysis"}:
        return "ℹ️ OPINION / ANALYSIS"
    if normalized == "false_or_misleading":
        return "⛔ FALSE OR MISLEADING"
    return "⚠️ UNKNOWN / UNVERIFIED"


def _display_time(value: Any, timezone_name: str) -> str:
    raw = _text(value, default="unknown", limit=80)
    if raw == "unknown":
        return raw
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    else:
        candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return raw
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return raw
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    local = parsed.astimezone(zone)
    offset = local.strftime("%z")
    rendered_offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    return f"{local:%Y-%m-%d %H:%M:%S} {rendered_offset}"


def format_alert(
    alert: Mapping[str, Any] | Any,
    *,
    timezone_name: str = "Asia/Shanghai",
) -> str:
    """Render escaped HTML with provenance status visible in the first line."""

    data = _mapping(alert)
    verification_status = _text(data.get("verification_status"), default="unknown", limit=40)
    banner = _status_banner(verification_status)
    importance = _score(data.get("importance_score"))
    confidence = _score(data.get("confidence_score"))
    tickers = _list_text(data.get("tickers")) or "N/A"
    category = _text(data.get("category"), default="unknown", limit=100)
    direction = _text(data.get("direction"), default="unknown", limit=40).upper()
    summary = _text(data.get("summary"), default="No summary available.", limit=1_800)
    source = _text(
        data.get("source_name") or data.get("chat_title") or data.get("source"),
        default="Telegram",
        limit=180,
    )
    first_seen = _display_time(
        data.get("first_seen_at") or data.get("timestamp"),
        timezone_name,
    )
    source_count = max(1, _score(data.get("source_count") or 1))
    risk_flags = _list_text(data.get("risk_flags"), limit=12)
    recommended_action = _text(
        data.get("recommended_action"),
        default="watch",
        limit=40,
    ).upper()

    header = [
        f"<b>{_escape_bounded(banner, limit=120)} · {importance}/100</b>",
        "",
        f"<b>{_escape_bounded(tickers, limit=200)} | {_escape_bounded(category, limit=120)}</b>",
        f"<b>Status:</b> {_escape_bounded(verification_status.upper(), limit=80)}",
        f"<b>Direction:</b> {_escape_bounded(direction, limit=80)}",
        f"<b>Confidence:</b> {confidence}/100",
        f"<b>Information action:</b> {_escape_bounded(recommended_action, limit=80)}",
    ]
    footer = [
        "",
        f"<b>Source:</b> {_escape_bounded(source, limit=300)}",
        f"<b>First seen:</b> {_escape_bounded(first_seen, limit=120)}",
        f"<b>Distinct Telegram source identities:</b> {source_count}",
    ]
    fixed = "\n".join((*header, "", "<b>Summary:</b> ", *footer))
    summary_budget = max(
        0,
        min(1_800, _MAX_ALERT_HTML_CHARS - len(fixed) - 64),
    )
    lines = [
        *header,
        "",
        f"<b>Summary:</b> {_escape_bounded(summary, limit=summary_budget)}",
        *footer,
    ]
    if risk_flags:
        risk_line = (
            f"<b>Risk flags:</b> {_escape_bounded(risk_flags, limit=500)}"
        )
        candidate = "\n".join((*lines, "", risk_line))
        if len(candidate) <= _MAX_ALERT_HTML_CHARS:
            lines.extend(["", risk_line])
    link = _text(data.get("telegram_link"), limit=500)
    if link.startswith(("https://t.me/", "http://t.me/")):
        link_line = (
            f'<a href="{_escape_bounded(link, limit=700, quote=True)}">'
            "Original Telegram message</a>"
        )
        candidate = "\n".join((*lines, "", link_line))
        if len(candidate) <= _MAX_ALERT_HTML_CHARS:
            lines.extend(["", link_line])
    rendered = "\n".join(lines)
    if len(rendered) > _MAX_ALERT_HTML_CHARS:
        # Field budgets make this unreachable under the current template.  A
        # minimal complete-markup fallback is safer than slicing serialized HTML.
        rendered = (
            f"<b>{_escape_bounded(banner, limit=120)} · {importance}/100</b>\n\n"
            "<i>Alert content omitted because it exceeded the safe HTML budget.</i>"
        )
    return rendered


class TelegramBot:
    """Output-only Bot API adapter bound to one immutable destination."""

    def __init__(
        self,
        *,
        token: str,
        destination_chat_id: str | int,
        dry_run: bool = False,
        timezone_name: str = "Asia/Shanghai",
        api_base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._token = str(token or "").strip()
        self._destination = str(destination_chat_id or "").strip()
        if self._destination and not _DESTINATION_PATTERN.fullmatch(self._destination):
            raise ValueError("Bot destination must be a non-zero numeric chat ID")
        self._dry_run = bool(dry_run)
        self._timezone_name = str(timezone_name or "Asia/Shanghai")
        self._api_base_url = str(api_base_url or "https://api.telegram.org").rstrip("/")
        parsed_base = urllib.parse.urlsplit(self._api_base_url)
        if (
            parsed_base.scheme.casefold() != "https"
            or not parsed_base.hostname
            or parsed_base.username is not None
            or parsed_base.password is not None
            or parsed_base.query
            or parsed_base.fragment
            or parsed_base.path not in {"", "/"}
        ):
            raise ValueError("Bot API base URL must be an HTTPS origin")
        self._timeout_seconds = max(1.0, min(120.0, float(timeout_seconds)))

    @property
    def destination_chat_id(self) -> str:
        return self._destination

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def send_alert(self, alert: Mapping[str, Any] | Any) -> BotSendResult:
        rendered = format_alert(alert, timezone_name=self._timezone_name)
        if self._dry_run:
            return BotSendResult(
                ok=True,
                status="WOULD_SEND",
                destination_chat_id=self._destination,
                text=rendered,
            )
        if not _TOKEN_PATTERN.fullmatch(self._token) or not _DESTINATION_PATTERN.fullmatch(
            self._destination
        ):
            return BotSendResult(
                ok=False,
                status="FAILED",
                destination_chat_id=self._destination,
                text=rendered,
                error_code="not_configured",
            )

        try:
            request = build_output_only_telegram_bot_request(
                self._api_base_url,
                token=self._token,
                destination_chat_id=self._destination,
                text=rendered,
            )
        except ExecutionBoundaryViolation:
            return BotSendResult(
                ok=False,
                status="FAILED",
                destination_chat_id=self._destination,
                text=rendered,
                error_code="execution_boundary",
            )
        try:
            with _urlopen_no_redirect(
                request,
                timeout=self._timeout_seconds,
            ) as response:
                payload = json.loads(_bounded_response_bytes(response).decode("utf-8"))
        except urllib.error.HTTPError:
            return BotSendResult(
                ok=False,
                status="FAILED",
                destination_chat_id=self._destination,
                text=rendered,
                error_code="http_status",
            )
        except (TimeoutError, urllib.error.URLError):
            return BotSendResult(
                ok=False,
                status="FAILED",
                destination_chat_id=self._destination,
                text=rendered,
                error_code="network",
            )
        except (ValueError, UnicodeDecodeError, OSError):
            return BotSendResult(
                ok=False,
                status="FAILED",
                destination_chat_id=self._destination,
                text=rendered,
                error_code="invalid_response",
            )
        if not isinstance(payload, Mapping) or payload.get("ok") is not True:
            return BotSendResult(
                ok=False,
                status="FAILED",
                destination_chat_id=self._destination,
                text=rendered,
                error_code="invalid_response",
            )
        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, Mapping) else ""
        return BotSendResult(
            ok=True,
            status="SENT",
            destination_chat_id=self._destination,
            text=rendered,
            message_id=_text(message_id, limit=100),
        )


class FakeBot:
    def __init__(
        self,
        *,
        destination_chat_id: str | int = "fixture",
        dry_run: bool = True,
        timezone_name: str = "Asia/Shanghai",
    ) -> None:
        self.destination_chat_id = str(destination_chat_id)
        self.dry_run = bool(dry_run)
        self.timezone_name = str(timezone_name or "Asia/Shanghai")
        self.sent: list[dict[str, Any]] = []

    def send_alert(self, alert: Mapping[str, Any] | Any) -> BotSendResult:
        payload = _mapping(alert)
        rendered = format_alert(payload, timezone_name=self.timezone_name)
        self.sent.append({"alert": payload, "text": rendered})
        return BotSendResult(
            ok=True,
            status="WOULD_SEND" if self.dry_run else "SENT",
            destination_chat_id=self.destination_chat_id,
            text=rendered,
            message_id="" if self.dry_run else str(len(self.sent)),
        )
