from __future__ import annotations

"""Pure contracts for importing untrusted, externally generated source items.

The module deliberately has no database, HTTP, Provider, scheduler, or network
dependency.  Callers supply the server-observed receipt time and persist the
returned packet/receipt through a separate integration layer.
"""

import copy
import hashlib
import ipaddress
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit


SOURCE_IMPORT_PACKET_VERSION = "source_import_packet_v1"
PROJECT_SOURCE_ITEM_VERSION = "project_source_item_v1"
SOURCE_IMPORT_RECEIPT_VERSION = "source_import_receipt_v1"
SOURCE_ITEM_FINGERPRINT_VERSION = "project_source_item_fingerprint_v1"
SOURCE_IMPORT_KEY_VERSION = "source_import_key_v1"
EXTERNAL_UNVERIFIED = "external_unverified"

SOURCE_STATUS_RECEIVED = "RECEIVED"
SOURCE_STATUS_VALIDATED = "VALIDATED"
SOURCE_STATUS_AWAITING_USER = "AWAITING_USER"
SOURCE_STATUS_ATTACHED = "ATTACHED"
SOURCE_STATUS_ROUND_DRAFTED = "ROUND_DRAFTED"
SOURCE_STATUS_REJECTED = "REJECTED"
SOURCE_STATUS_DUPLICATE = "DUPLICATE"
SOURCE_STATUS_EXPIRED = "EXPIRED"
SOURCE_IMPORT_STATUSES = frozenset({
    SOURCE_STATUS_RECEIVED,
    SOURCE_STATUS_VALIDATED,
    SOURCE_STATUS_AWAITING_USER,
    SOURCE_STATUS_ATTACHED,
    SOURCE_STATUS_ROUND_DRAFTED,
    SOURCE_STATUS_REJECTED,
    SOURCE_STATUS_DUPLICATE,
    SOURCE_STATUS_EXPIRED,
})

MAX_SOURCE_IMPORT_BYTES = 256 * 1024
MAX_SOURCE_IMPORT_DEPTH = 12
MAX_SOURCE_ITEMS = 50
MAX_SOURCES_PER_ITEM = 12
MAX_TOTAL_SOURCES = 200
MAX_ENTITIES_PER_ITEM = 50
MAX_FACTS_PER_ITEM = 50
MAX_IMPACT_HYPOTHESES_PER_ITEM = 20
MAX_UNKNOWNS_PER_ITEM = 30
MAX_EXTENSION_BYTES_PER_ITEM = 32 * 1024
MAX_URL_CHARS = 2_000
MAX_CLOCK_SKEW_MS = 5 * 60 * 1_000

SOURCE_SEVERITIES = frozenset({"info", "low", "medium", "high", "critical"})
SOURCE_RECOMMENDED_ROUTES = frozenset({
    "notify_only",
    "attach_to_room",
    "open_round_draft",
})
SOURCE_COST_STATUSES = frozenset({"unavailable", "declared"})

_PACKET_FIELDS = frozenset({
    "version",
    "source_channel",
    "source_key",
    "external_run_id",
    "checked_at",
    "cutoff_at",
    "meaningful_change",
    "items",
    "generation",
})
_ITEM_FIELDS = frozenset({
    "version",
    "external_item_id",
    "item_type",
    "severity",
    "occurred_at",
    "published_at",
    "entities",
    "headline",
    "summary",
    "facts",
    "sources",
    "impact_hypotheses",
    "unknowns",
    "confidence",
    "recommended_route",
    "extensions",
})
_ENTITY_FIELDS = frozenset({"kind", "id", "label"})
_FACT_FIELDS = frozenset({"claim", "source_indexes"})
_SOURCE_FIELDS = frozenset({
    "url",
    "publisher",
    "source_type",
    "published_at",
    "content_sha256",
})
_IMPACT_FIELDS = frozenset({
    "statement",
    "affected_area",
    "time_horizon",
    "source_indexes",
    "confidence",
})
_GENERATION_FIELDS = frozenset({"channel", "model", "cost", "correlated_output"})
_COST_FIELDS = frozenset({"status", "amount", "currency", "usage_source"})

_SLUG_RE = re.compile(r"[a-z][a-z0-9_-]{0,79}\Z")
_EXTENSION_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_CURRENCY_RE = re.compile(r"[A-Z]{3}\Z")
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,9})?\Z")
_RFC3339_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_JSON_FENCE_RE = re.compile(
    r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z",
    re.IGNORECASE | re.DOTALL,
)
_INVALID_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")

_FORBIDDEN_FIELD_WORDS = frozenset({
    "account",
    "accounts",
    "bet",
    "bets",
    "brokerage",
    "command",
    "commands",
    "execute",
    "execution",
    "function",
    "functions",
    "mcp",
    "order",
    "orders",
    "payment",
    "payments",
    "shell",
    "tool",
    "tools",
    "trade",
    "trades",
    "transfer",
    "transfers",
    "wallet",
    "wallets",
    "withdraw",
    "withdrawals",
})
_FORBIDDEN_COMPACT_FIELDS = frozenset({
    "functioncall",
    "paralleltoolcalls",
    "toolchoice",
})
_SENSITIVE_FIELD_WORDS = frozenset({
    "auth",
    "authorization",
    "cookie",
    "credential",
    "jwt",
    "key",
    "password",
    "secret",
    "session",
    "sig",
    "signature",
    "token",
})
_SENSITIVE_COMPACT_FIELDS = frozenset({
    "accesstoken",
    "apikey",
    "authtoken",
    "clientsecret",
    "encryptionkey",
    "privatekey",
    "refreshtoken",
    "signingkey",
    "xamzalgorithm",
    "xamzcredential",
    "xamzdate",
    "xamzsecuritytoken",
    "xamzsignature",
})
_LOCAL_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home",
)


@dataclass(frozen=True)
class SourceContractIssue:
    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


class SourceInboxContractError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "SOURCE_IMPORT_INVALID",
        issues: list[SourceContractIssue] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.issues = list(issues or [])


class _JSONObjectPairs(list[tuple[str, Any]]):
    pass


class _DuplicateJSONKey(ValueError):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class _NonFiniteJSONNumber(ValueError):
    pass


def _error(path: str, code: str, message: str) -> SourceInboxContractError:
    return SourceInboxContractError(
        message,
        code=code,
        issues=[SourceContractIssue(path, code, message)],
    )


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _raw_import_bytes(raw: Any) -> bytes:
    if type(raw) is bytes:
        payload = raw
    elif type(raw) is str:
        try:
            payload = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise _error("$", "SOURCE_IMPORT_UTF8_INVALID", "导入内容不是有效 UTF-8 文本。") from exc
    else:
        raise _error("$", "SOURCE_IMPORT_TYPE_INVALID", "导入内容必须是原生字符串或 bytes。")
    if len(payload) > MAX_SOURCE_IMPORT_BYTES:
        raise _error(
            "$",
            "SOURCE_IMPORT_TOO_LARGE",
            f"导入内容超过 {MAX_SOURCE_IMPORT_BYTES} UTF-8 字节上限。",
        )
    return payload


def _jsonpath_child(path: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def _materialize_json_pairs(value: Any, path: str = "$", depth: int = 1) -> Any:
    if depth > MAX_SOURCE_IMPORT_DEPTH:
        raise _error(
            path,
            "SOURCE_IMPORT_DEPTH_INVALID",
            f"JSON 嵌套超过 {MAX_SOURCE_IMPORT_DEPTH} 层上限。",
        )
    if isinstance(value, _JSONObjectPairs):
        result: dict[str, Any] = {}
        for key, item in value:
            child_path = _jsonpath_child(path, key)
            if key in result:
                raise _DuplicateJSONKey(child_path)
            result[key] = _materialize_json_pairs(item, child_path, depth + 1)
        return result
    if isinstance(value, list):
        return [
            _materialize_json_pairs(item, f"{path}[{index}]", depth + 1)
            for index, item in enumerate(value)
        ]
    return value


def parse_source_import_json(raw: Any) -> dict[str, Any]:
    """Parse exactly one JSON object, optionally inside one JSON code fence."""

    payload = _raw_import_bytes(raw)
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _error("$", "SOURCE_IMPORT_UTF8_INVALID", "导入 bytes 不是有效 UTF-8。") from exc
    candidate = text.strip()
    fenced = _JSON_FENCE_RE.fullmatch(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    def reject_constant(value: str) -> None:
        raise _NonFiniteJSONNumber(value)

    try:
        pairs = json.loads(
            candidate,
            object_pairs_hook=_JSONObjectPairs,
            parse_constant=reject_constant,
        )
        parsed = _materialize_json_pairs(pairs)
    except _DuplicateJSONKey as exc:
        raise _error(
            exc.path,
            "SOURCE_IMPORT_DUPLICATE_KEY",
            "同一 JSON 路径不能出现重复字段。",
        ) from exc
    except _NonFiniteJSONNumber as exc:
        raise _error(
            "$",
            "SOURCE_IMPORT_NONFINITE_NUMBER",
            "JSON 不能包含 NaN 或 Infinity。",
        ) from exc
    except json.JSONDecodeError as exc:
        raise _error(
            f"$[line={exc.lineno},column={exc.colno}]",
            "SOURCE_IMPORT_JSON_INVALID",
            "导入内容必须只包含一个完整 JSON 对象。",
        ) from exc
    except RecursionError as exc:
        raise _error(
            "$",
            "SOURCE_IMPORT_DEPTH_INVALID",
            f"JSON 嵌套超过 {MAX_SOURCE_IMPORT_DEPTH} 层上限。",
        ) from exc
    if type(parsed) is not dict:
        raise _error("$", "SOURCE_IMPORT_ROOT_INVALID", "JSON 根节点必须是 object。")
    return parsed


def _require_exact_mapping(
    value: Any,
    fields: frozenset[str],
    path: str,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise _error(path, "SOURCE_IMPORT_TYPE_INVALID", "字段必须是 object。")
    actual = set(value)
    missing = sorted(fields - actual)
    unknown = sorted(actual - fields)
    if missing:
        raise _error(
            f"{path}.{missing[0]}",
            "SOURCE_IMPORT_FIELD_REQUIRED",
            f"缺少必需字段 {missing[0]}。",
        )
    if unknown:
        raise _error(
            f"{path}.{unknown[0]}",
            "SOURCE_IMPORT_FIELD_UNKNOWN",
            f"字段 {unknown[0]} 不在合同白名单中。",
        )
    return value


def _clean_text(
    value: Any,
    path: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str:
        raise _error(path, "SOURCE_IMPORT_TYPE_INVALID", "字段必须是字符串。")
    clean = unicodedata.normalize("NFC", value).replace("\r\n", "\n").strip()
    if any(ord(character) < 32 and character not in "\n\t" for character in clean):
        raise _error(path, "SOURCE_IMPORT_TEXT_INVALID", "字符串包含不允许的控制字符。")
    if not clean and not allow_empty:
        raise _error(path, "SOURCE_IMPORT_FIELD_REQUIRED", "字符串不能为空。")
    if len(clean) > maximum:
        raise _error(
            path,
            "SOURCE_IMPORT_TEXT_TOO_LONG",
            f"字符串不能超过 {maximum} 个字符。",
        )
    return clean


def _clean_slug(value: Any, path: str) -> str:
    clean = _clean_text(value, path, maximum=80).lower()
    if not _SLUG_RE.fullmatch(clean):
        raise _error(path, "SOURCE_IMPORT_ENUM_INVALID", "字段必须是小写 slug。")
    return clean


def _clean_sha256(value: Any, path: str, *, allow_empty: bool = False) -> str:
    clean = _clean_text(value, path, maximum=64, allow_empty=allow_empty).lower()
    if clean or not allow_empty:
        if not _SHA256_RE.fullmatch(clean):
            raise _error(path, "SOURCE_IMPORT_SHA256_INVALID", "字段必须是 64 位小写 SHA-256。")
    return clean


def _clean_confidence(value: Any, path: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value):
        raise _error(path, "SOURCE_IMPORT_NUMBER_INVALID", "置信度必须是有限原生数字。")
    clean = float(value)
    if not 0 <= clean <= 1:
        raise _error(path, "SOURCE_IMPORT_NUMBER_INVALID", "置信度必须位于 0 到 1。")
    return clean


def _canonical_rfc3339(value: Any, path: str, *, allow_empty: bool = False) -> str:
    clean = _clean_text(value, path, maximum=40, allow_empty=allow_empty)
    if not clean and allow_empty:
        return ""
    if not _RFC3339_RE.fullmatch(clean):
        raise _error(path, "SOURCE_IMPORT_TIME_INVALID", "时间必须是带时区的 RFC3339。")
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(path, "SOURCE_IMPORT_TIME_INVALID", "时间不是有效 RFC3339。") from exc
    if parsed.tzinfo is None:
        raise _error(path, "SOURCE_IMPORT_TIME_INVALID", "时间必须包含时区。")
    utc = parsed.astimezone(timezone.utc)
    base = utc.strftime("%Y-%m-%dT%H:%M:%S")
    fraction = f".{utc.microsecond:06d}".rstrip("0") if utc.microsecond else ""
    return f"{base}{fraction}Z"


def _timestamp_ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1_000)


def _field_words(key: str) -> set[str]:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    return set(re.findall(r"[a-z0-9]+", separated.lower()))


def _sensitive_field_name(key: str, *, query: bool = False) -> bool:
    normalized = unicodedata.normalize("NFKC", key)
    # Extension property names and URL query parameter names are protocol
    # identifiers, not display labels.  Keeping them ASCII after NFKC closes
    # Unicode homoglyph spellings such as ``api_tоken`` (Cyrillic ``о``)
    # without rejecting compatibility characters that normalize to ASCII.
    if not normalized.isascii():
        return True
    words = _field_words(normalized)
    compact = re.sub(r"[^a-z0-9]", "", normalized.lower())
    return bool(
        words & _SENSITIVE_FIELD_WORDS
        or query and "code" in words
        or compact in _SENSITIVE_COMPACT_FIELDS
    )


def _reject_sensitive_fields(value: Any, path: str) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _error(path, "SOURCE_IMPORT_FIELD_INVALID", "JSON 字段名必须是字符串。")
            if _sensitive_field_name(key):
                raise _error(
                    _jsonpath_child(path, key),
                    "SOURCE_IMPORT_SENSITIVE_FIELD_FORBIDDEN",
                    "extensions 不得携带 token、credential、secret、session 或签名字段。",
                )
            _reject_sensitive_fields(item, _jsonpath_child(path, key))
    elif type(value) is list:
        for index, item in enumerate(value):
            _reject_sensitive_fields(item, f"{path}[{index}]")


def _reject_sensitive_query(query: str, path: str) -> None:
    try:
        pairs = parse_qsl(
            query.replace(";", "&"),
            keep_blank_values=True,
            strict_parsing=False,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeError, ValueError) as exc:
        raise _error(path, "SOURCE_URL_INVALID", "来源 URL query 无法解析。") from exc
    for raw_key, _value in pairs:
        key = raw_key
        # Each successful unquote shortens the string, so this reaches a fixed
        # point without an arbitrary decode-depth ceiling.  Check before every
        # decode, including the final stable value.
        while True:
            if _sensitive_field_name(key, query=True):
                raise _error(
                    path,
                    "SOURCE_URL_SENSITIVE_QUERY_FORBIDDEN",
                    "来源 URL query 不得携带 token、credential、secret、session 或签名参数。",
                )
            try:
                decoded = unquote(key, encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, UnicodeError, ValueError) as exc:
                raise _error(path, "SOURCE_URL_INVALID", "来源 URL query 无法解析。") from exc
            if decoded == key:
                break
            key = decoded


def _reject_execution_fields(value: Any, path: str = "$") -> None:
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _error(path, "SOURCE_IMPORT_FIELD_INVALID", "JSON 字段名必须是字符串。")
            compact = re.sub(r"[^a-z0-9]", "", key.lower())
            if (
                _field_words(key) & _FORBIDDEN_FIELD_WORDS
                or compact in _FORBIDDEN_COMPACT_FIELDS
            ):
                raise _error(
                    _jsonpath_child(path, key),
                    "SOURCE_IMPORT_EXECUTION_FIELD_FORBIDDEN",
                    "来源包不得包含执行、账户、交易、支付、工具或命令字段。",
                )
            _reject_execution_fields(item, _jsonpath_child(path, key))
    elif type(value) is list:
        for index, item in enumerate(value):
            _reject_execution_fields(item, f"{path}[{index}]")
    elif value is not None and type(value) not in {str, int, float, bool}:
        raise _error(path, "SOURCE_IMPORT_TYPE_INVALID", "扩展字段只能包含标准 JSON 类型。")
    elif type(value) is float and not math.isfinite(value):
        raise _error(path, "SOURCE_IMPORT_NONFINITE_NUMBER", "JSON 数字必须有限。")


def _normalize_uri_component(value: str, *, safe: str, path: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _error(path, "SOURCE_URL_INVALID", "URL 包含控制字符。")
    if _INVALID_PERCENT_RE.search(value):
        raise _error(path, "SOURCE_URL_INVALID", "URL 包含无效百分号转义。")
    encoded = quote(value, safe=safe + "%", encoding="utf-8", errors="strict")
    return _PERCENT_ESCAPE_RE.sub(lambda match: match.group(0).upper(), encoded)


def canonicalize_source_url(value: Any, *, path: str = "$.url") -> str:
    """Canonicalize one public HTTP(S) source URL without resolving or fetching it."""

    raw = _clean_text(value, path, maximum=MAX_URL_CHARS)
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise _error(path, "SOURCE_URL_INVALID", "来源 URL 无法解析。") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise _error(path, "SOURCE_URL_SCHEME_FORBIDDEN", "来源 URL 只允许 http 或 https。")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise _error(path, "SOURCE_URL_USERINFO_FORBIDDEN", "来源 URL 不得包含 userinfo。")
    hostname = str(parsed.hostname or "").rstrip(".").lower()
    if not hostname or "%" in hostname:
        raise _error(path, "SOURCE_URL_HOST_INVALID", "来源 URL 缺少有效主机。")
    try:
        ascii_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise _error(path, "SOURCE_URL_HOST_INVALID", "来源 URL 主机名无效。") from exc

    if ascii_host == "localhost" or ascii_host.endswith(_LOCAL_HOST_SUFFIXES):
        raise _error(path, "SOURCE_URL_PRIVATE_HOST_FORBIDDEN", "来源 URL 不得指向本地或私网主机。")
    ip_value: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        ip_value = ipaddress.ip_address(ascii_host)
    except ValueError:
        if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9.]+)", ascii_host, re.IGNORECASE):
            raise _error(path, "SOURCE_URL_HOST_INVALID", "来源 URL 不接受非规范 IP 主机。")
        labels = ascii_host.split(".")
        if len(labels) < 2 or any(not _HOST_LABEL_RE.fullmatch(label) for label in labels):
            raise _error(path, "SOURCE_URL_HOST_INVALID", "来源 URL 主机名不是公开域名格式。")
    if ip_value is not None and not ip_value.is_global:
        raise _error(
            path,
            "SOURCE_URL_PRIVATE_HOST_FORBIDDEN",
            "来源 URL 不得指向私网、回环、链路本地或保留地址。",
        )

    allowed_port = 80 if scheme == "http" else 443
    if port is not None and port != allowed_port:
        raise _error(path, "SOURCE_URL_PORT_FORBIDDEN", "来源 URL 使用了非默认端口。")
    _reject_sensitive_query(parsed.query, path)
    rendered_host = f"[{ascii_host}]" if ip_value is not None and ip_value.version == 6 else ascii_host
    normalized_path = _normalize_uri_component(
        parsed.path or "/",
        safe="/:@-._~!$&'()*+,;=",
        path=path,
    )
    normalized_query = _normalize_uri_component(
        parsed.query,
        safe="=&?/:@-._~!$'()*+,;",
        path=path,
    )
    return urlunsplit((scheme, rendered_host, normalized_path, normalized_query, ""))


def _source_indexes(
    value: Any,
    path: str,
    *,
    source_count: int,
    allow_empty: bool = False,
) -> list[int]:
    if type(value) is not list:
        raise _error(path, "SOURCE_IMPORT_TYPE_INVALID", "source_indexes 必须是数组。")
    if not value and not allow_empty:
        raise _error(path, "SOURCE_IMPORT_FIELD_REQUIRED", "source_indexes 不能为空。")
    if len(value) > MAX_SOURCES_PER_ITEM:
        raise _error(path, "SOURCE_IMPORT_SOURCE_LIMIT", "source_indexes 超过来源数上限。")
    result: list[int] = []
    for index, item in enumerate(value):
        if type(item) is not int or not 0 <= item < source_count:
            raise _error(
                f"{path}[{index}]",
                "SOURCE_IMPORT_SOURCE_REFERENCE_INVALID",
                "source index 不在当前 item 的来源表内。",
            )
        if item in result:
            raise _error(
                f"{path}[{index}]",
                "SOURCE_IMPORT_SOURCE_REFERENCE_DUPLICATE",
                "source index 不能重复。",
            )
        result.append(item)
    return result


def _clean_extensions(value: Any, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise _error(path, "SOURCE_IMPORT_TYPE_INVALID", "extensions 必须是 object。")
    for key in value:
        if type(key) is not str or not _EXTENSION_KEY_RE.fullmatch(key):
            raise _error(
                _jsonpath_child(path, str(key)),
                "SOURCE_IMPORT_EXTENSION_KEY_INVALID",
                "extension key 必须是小写、可版本化的命名空间。",
            )
    _reject_execution_fields(value, path)
    _reject_sensitive_fields(value, path)
    try:
        size = len(_canonical_json_bytes(value))
    except (TypeError, ValueError) as exc:
        raise _error(path, "SOURCE_IMPORT_TYPE_INVALID", "extensions 不是严格 JSON。") from exc
    if size > MAX_EXTENSION_BYTES_PER_ITEM:
        raise _error(
            path,
            "SOURCE_IMPORT_EXTENSION_TOO_LARGE",
            f"extensions 不能超过 {MAX_EXTENSION_BYTES_PER_ITEM} UTF-8 字节。",
        )
    return copy.deepcopy(value)


def _normalize_cost(value: Any, path: str) -> dict[str, Any]:
    raw = _require_exact_mapping(value, _COST_FIELDS, path)
    status = _clean_slug(raw["status"], f"{path}.status")
    if status not in SOURCE_COST_STATUSES:
        raise _error(f"{path}.status", "SOURCE_IMPORT_ENUM_INVALID", "cost status 不在白名单中。")
    usage_source = _clean_slug(raw["usage_source"], f"{path}.usage_source")
    if status == "unavailable":
        if raw["amount"] is not None or raw["currency"] != "":
            raise _error(
                path,
                "SOURCE_IMPORT_COST_INVALID",
                "unavailable cost 必须使用 amount=null 和 currency 空字符串。",
            )
        amount: str | None = None
        currency = ""
    else:
        if type(raw["amount"]) is not str or not _DECIMAL_RE.fullmatch(raw["amount"]):
            raise _error(f"{path}.amount", "SOURCE_IMPORT_COST_INVALID", "声明费用必须是规范十进制字符串。")
        try:
            parsed_amount = Decimal(raw["amount"])
        except InvalidOperation as exc:
            raise _error(f"{path}.amount", "SOURCE_IMPORT_COST_INVALID", "声明费用无效。") from exc
        if not parsed_amount.is_finite() or not Decimal("0") <= parsed_amount <= Decimal("1000000000"):
            raise _error(f"{path}.amount", "SOURCE_IMPORT_COST_INVALID", "声明费用超出允许范围。")
        amount = format(parsed_amount, "f")
        currency = _clean_text(raw["currency"], f"{path}.currency", maximum=3).upper()
        if not _CURRENCY_RE.fullmatch(currency):
            raise _error(f"{path}.currency", "SOURCE_IMPORT_COST_INVALID", "currency 必须是三位大写代码。")
    return {
        "status": status,
        "amount": amount,
        "currency": currency,
        "usage_source": usage_source,
    }


def _normalize_generation(value: Any, path: str) -> dict[str, Any]:
    raw = _require_exact_mapping(value, _GENERATION_FIELDS, path)
    if type(raw["correlated_output"]) is not bool:
        raise _error(
            f"{path}.correlated_output",
            "SOURCE_IMPORT_TYPE_INVALID",
            "correlated_output 必须是布尔值。",
        )
    return {
        "channel": _clean_slug(raw["channel"], f"{path}.channel"),
        "model": _clean_text(raw["model"], f"{path}.model", maximum=160, allow_empty=True),
        "cost": _normalize_cost(raw["cost"], f"{path}.cost"),
        "correlated_output": raw["correlated_output"],
    }


def _normalize_sources(value: Any, path: str) -> list[dict[str, Any]]:
    if type(value) is not list or not value:
        raise _error(path, "SOURCE_IMPORT_FIELD_REQUIRED", "每个来源项必须至少包含一个 source。")
    if len(value) > MAX_SOURCES_PER_ITEM:
        raise _error(
            path,
            "SOURCE_IMPORT_SOURCE_LIMIT",
            f"每个 item 最多允许 {MAX_SOURCES_PER_ITEM} 个 source。",
        )
    result: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        raw = _require_exact_mapping(item, _SOURCE_FIELDS, item_path)
        url = canonicalize_source_url(raw["url"], path=f"{item_path}.url")
        if url in seen_urls:
            raise _error(
                f"{item_path}.url",
                "SOURCE_IMPORT_SOURCE_DUPLICATE",
                "同一 item 中 canonical source URL 不能重复。",
            )
        seen_urls.add(url)
        result.append({
            "url": url,
            "publisher": _clean_text(raw["publisher"], f"{item_path}.publisher", maximum=200),
            "source_type": _clean_slug(raw["source_type"], f"{item_path}.source_type"),
            "published_at": _canonical_rfc3339(
                raw["published_at"],
                f"{item_path}.published_at",
                allow_empty=True,
            ),
            "content_sha256": _clean_sha256(
                raw["content_sha256"],
                f"{item_path}.content_sha256",
                allow_empty=True,
            ),
        })
    return result


def _normalize_entities(value: Any, path: str) -> list[dict[str, str]]:
    if type(value) is not list:
        raise _error(path, "SOURCE_IMPORT_TYPE_INVALID", "entities 必须是数组。")
    if len(value) > MAX_ENTITIES_PER_ITEM:
        raise _error(path, "SOURCE_IMPORT_ITEM_LIMIT", "entities 超过单项上限。")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        raw = _require_exact_mapping(item, _ENTITY_FIELDS, item_path)
        entity = {
            "kind": _clean_slug(raw["kind"], f"{item_path}.kind"),
            "id": _clean_text(raw["id"], f"{item_path}.id", maximum=200),
            "label": _clean_text(raw["label"], f"{item_path}.label", maximum=240),
        }
        key = (entity["kind"], entity["id"])
        if key in seen:
            raise _error(item_path, "SOURCE_IMPORT_ENTITY_DUPLICATE", "entity kind/id 不能重复。")
        seen.add(key)
        result.append(entity)
    return result


def _normalize_facts(value: Any, path: str, *, source_count: int) -> list[dict[str, Any]]:
    if type(value) is not list or not value:
        raise _error(path, "SOURCE_IMPORT_FIELD_REQUIRED", "facts 必须是非空数组。")
    if len(value) > MAX_FACTS_PER_ITEM:
        raise _error(path, "SOURCE_IMPORT_ITEM_LIMIT", "facts 超过单项上限。")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        raw = _require_exact_mapping(item, _FACT_FIELDS, item_path)
        result.append({
            "claim": _clean_text(raw["claim"], f"{item_path}.claim", maximum=4_000),
            "source_indexes": _source_indexes(
                raw["source_indexes"],
                f"{item_path}.source_indexes",
                source_count=source_count,
            ),
        })
    return result


def _normalize_hypotheses(
    value: Any,
    path: str,
    *,
    source_count: int,
) -> list[dict[str, Any]]:
    if type(value) is not list:
        raise _error(path, "SOURCE_IMPORT_TYPE_INVALID", "impact_hypotheses 必须是数组。")
    if len(value) > MAX_IMPACT_HYPOTHESES_PER_ITEM:
        raise _error(path, "SOURCE_IMPORT_ITEM_LIMIT", "impact_hypotheses 超过单项上限。")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        raw = _require_exact_mapping(item, _IMPACT_FIELDS, item_path)
        result.append({
            "statement": _clean_text(raw["statement"], f"{item_path}.statement", maximum=4_000),
            "affected_area": _clean_text(raw["affected_area"], f"{item_path}.affected_area", maximum=240),
            "time_horizon": _clean_text(raw["time_horizon"], f"{item_path}.time_horizon", maximum=160),
            "source_indexes": _source_indexes(
                raw["source_indexes"],
                f"{item_path}.source_indexes",
                source_count=source_count,
            ),
            "confidence": _clean_confidence(raw["confidence"], f"{item_path}.confidence"),
        })
    return result


def _normalize_unknowns(value: Any, path: str) -> list[str]:
    if type(value) is not list:
        raise _error(path, "SOURCE_IMPORT_TYPE_INVALID", "unknowns 必须是数组。")
    if len(value) > MAX_UNKNOWNS_PER_ITEM:
        raise _error(path, "SOURCE_IMPORT_ITEM_LIMIT", "unknowns 超过单项上限。")
    return [
        _clean_text(item, f"{path}[{index}]", maximum=2_000)
        for index, item in enumerate(value)
    ]


def _fingerprint_text(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def project_source_item_fingerprint(item: Any) -> str:
    """Derive a server-owned event identity; supplied fingerprints are never accepted."""

    if type(item) is not dict:
        raise _error("$.item", "SOURCE_IMPORT_TYPE_INVALID", "source item 必须是 object。")
    entities = item.get("entities") if type(item.get("entities")) is list else []
    sources = item.get("sources") if type(item.get("sources")) is list else []
    basis = {
        "version": SOURCE_ITEM_FINGERPRINT_VERSION,
        "item_type": str(item.get("item_type") or ""),
        "occurred_at": str(item.get("occurred_at") or ""),
        "headline_key": _fingerprint_text(item.get("headline")),
        "entities": sorted(
            (
                {
                    "kind": str(entity.get("kind") or ""),
                    "id": _fingerprint_text(entity.get("id")),
                }
                for entity in entities
                if type(entity) is dict
            ),
            key=lambda value: (value["kind"], value["id"]),
        ),
        "sources": sorted(
            (
                {
                    "url": str(source.get("url") or ""),
                    "content_sha256": str(source.get("content_sha256") or ""),
                }
                for source in sources
                if type(source) is dict
            ),
            key=lambda value: (value["url"], value["content_sha256"]),
        ),
    }
    return canonical_sha256(basis)


def _normalize_item(value: Any, path: str) -> dict[str, Any]:
    raw = _require_exact_mapping(value, _ITEM_FIELDS, path)
    if raw["version"] != PROJECT_SOURCE_ITEM_VERSION:
        raise _error(f"{path}.version", "SOURCE_IMPORT_SCHEMA_UNSUPPORTED", "source item version 不受支持。")
    sources = _normalize_sources(raw["sources"], f"{path}.sources")
    severity = _clean_slug(raw["severity"], f"{path}.severity")
    if severity not in SOURCE_SEVERITIES:
        raise _error(f"{path}.severity", "SOURCE_IMPORT_ENUM_INVALID", "severity 不在白名单中。")
    route = _clean_slug(raw["recommended_route"], f"{path}.recommended_route")
    if route not in SOURCE_RECOMMENDED_ROUTES:
        raise _error(
            f"{path}.recommended_route",
            "SOURCE_IMPORT_ENUM_INVALID",
            "recommended_route 不在白名单中。",
        )
    normalized: dict[str, Any] = {
        "version": PROJECT_SOURCE_ITEM_VERSION,
        "external_item_id": _clean_text(
            raw["external_item_id"],
            f"{path}.external_item_id",
            maximum=200,
            allow_empty=True,
        ),
        "item_type": _clean_slug(raw["item_type"], f"{path}.item_type"),
        "severity": severity,
        "occurred_at": _canonical_rfc3339(raw["occurred_at"], f"{path}.occurred_at"),
        "published_at": _canonical_rfc3339(
            raw["published_at"],
            f"{path}.published_at",
            allow_empty=True,
        ),
        "entities": _normalize_entities(raw["entities"], f"{path}.entities"),
        "headline": _clean_text(raw["headline"], f"{path}.headline", maximum=500),
        "summary": _clean_text(raw["summary"], f"{path}.summary", maximum=8_000),
        "facts": _normalize_facts(
            raw["facts"],
            f"{path}.facts",
            source_count=len(sources),
        ),
        "sources": sources,
        "impact_hypotheses": _normalize_hypotheses(
            raw["impact_hypotheses"],
            f"{path}.impact_hypotheses",
            source_count=len(sources),
        ),
        "unknowns": _normalize_unknowns(raw["unknowns"], f"{path}.unknowns"),
        "confidence": _clean_confidence(raw["confidence"], f"{path}.confidence"),
        "recommended_route": route,
        "extensions": _clean_extensions(raw["extensions"], f"{path}.extensions"),
        "external_claims_verification": EXTERNAL_UNVERIFIED,
    }
    normalized["server_fingerprint_version"] = SOURCE_ITEM_FINGERPRINT_VERSION
    normalized["server_fingerprint"] = project_source_item_fingerprint(normalized)
    return normalized


def normalize_source_import_packet(
    value: Any,
    *,
    received_at_ms: int,
) -> dict[str, Any]:
    """Validate and normalize a parsed packet using a server-observed receipt time."""

    if type(received_at_ms) is not int or received_at_ms < 0:
        raise _error(
            "$.received_at_ms",
            "SOURCE_IMPORT_RECEIPT_TIME_INVALID",
            "received_at_ms 必须是非负原生整数。",
        )
    _reject_execution_fields(value)
    raw = _require_exact_mapping(value, _PACKET_FIELDS, "$")
    if raw["version"] != SOURCE_IMPORT_PACKET_VERSION:
        raise _error("$.version", "SOURCE_IMPORT_SCHEMA_UNSUPPORTED", "source import packet version 不受支持。")
    if type(raw["meaningful_change"]) is not bool:
        raise _error("$.meaningful_change", "SOURCE_IMPORT_TYPE_INVALID", "meaningful_change 必须是布尔值。")
    checked_at = _canonical_rfc3339(raw["checked_at"], "$.checked_at")
    cutoff_at = _canonical_rfc3339(raw["cutoff_at"], "$.cutoff_at")
    if _timestamp_ms(cutoff_at) > _timestamp_ms(checked_at):
        raise _error("$.cutoff_at", "SOURCE_IMPORT_TIME_ORDER_INVALID", "cutoff_at 不得晚于 checked_at。")
    if _timestamp_ms(cutoff_at) > received_at_ms:
        raise _error("$.cutoff_at", "SOURCE_IMPORT_TIME_FUTURE", "cutoff_at 不得晚于服务端接收时间。")
    if _timestamp_ms(checked_at) > received_at_ms + MAX_CLOCK_SKEW_MS:
        raise _error("$.checked_at", "SOURCE_IMPORT_TIME_FUTURE", "checked_at 超出允许时钟偏差。")

    items_value = raw["items"]
    if type(items_value) is not list:
        raise _error("$.items", "SOURCE_IMPORT_TYPE_INVALID", "items 必须是数组。")
    if len(items_value) > MAX_SOURCE_ITEMS:
        raise _error(
            "$.items",
            "SOURCE_IMPORT_ITEM_LIMIT",
            f"每个 packet 最多允许 {MAX_SOURCE_ITEMS} 个 item。",
        )
    if raw["meaningful_change"] is True and not items_value:
        raise _error("$.items", "SOURCE_IMPORT_FIELD_REQUIRED", "meaningful_change=true 时 items 不能为空。")
    if raw["meaningful_change"] is False and items_value:
        raise _error("$.items", "SOURCE_IMPORT_MEANING_CONFLICT", "meaningful_change=false 时 items 必须为空。")
    items = [
        _normalize_item(item, f"$.items[{index}]")
        for index, item in enumerate(items_value)
    ]
    total_sources = sum(len(item["sources"]) for item in items)
    if total_sources > MAX_TOTAL_SOURCES:
        raise _error(
            "$.items",
            "SOURCE_IMPORT_SOURCE_LIMIT",
            f"每个 packet 最多允许 {MAX_TOTAL_SOURCES} 个 source。",
        )
    seen_fingerprints: set[str] = set()
    for index, item in enumerate(items):
        fingerprint = item["server_fingerprint"]
        if fingerprint in seen_fingerprints:
            raise _error(
                f"$.items[{index}]",
                "SOURCE_IMPORT_ITEM_DUPLICATE",
                "同一 packet 中不能包含重复 server fingerprint。",
            )
        seen_fingerprints.add(fingerprint)

    source_channel = _clean_slug(raw["source_channel"], "$.source_channel")
    generation = _normalize_generation(raw["generation"], "$.generation")
    if generation["channel"] != source_channel:
        raise _error(
            "$.generation.channel",
            "SOURCE_IMPORT_CHANNEL_BINDING_INVALID",
            "generation.channel 必须与 source_channel 完全一致。",
        )
    normalized = {
        "version": SOURCE_IMPORT_PACKET_VERSION,
        "source_channel": source_channel,
        "source_key": _clean_text(raw["source_key"], "$.source_key", maximum=160),
        "external_run_id": _clean_text(
            raw["external_run_id"],
            "$.external_run_id",
            maximum=200,
        ),
        "checked_at": checked_at,
        "cutoff_at": cutoff_at,
        "meaningful_change": raw["meaningful_change"],
        "items": items,
        "generation": generation,
        "external_claims_verification": {
            "checked_at": EXTERNAL_UNVERIFIED,
            "cutoff_at": EXTERNAL_UNVERIFIED,
            "item_times": EXTERNAL_UNVERIFIED,
            "source_times": EXTERNAL_UNVERIFIED,
            "model": EXTERNAL_UNVERIFIED,
            "cost": EXTERNAL_UNVERIFIED,
            "recommended_routes": EXTERNAL_UNVERIFIED,
        },
        "safety": {
            "execution_fields_present": False,
            "execution_capability": "none",
            "provider_calls_performed": 0,
            "market_calls_performed": 0,
            "network_requests_performed": 0,
            "user_action_required": True,
        },
    }
    return normalized


def _verified_normalized_packet(packet: Any, *, received_at_ms: int) -> dict[str, Any]:
    output_fields = _PACKET_FIELDS | {"external_claims_verification", "safety"}
    raw = _require_exact_mapping(packet, frozenset(output_fields), "$.packet")
    item_output_fields = _ITEM_FIELDS | {
        "external_claims_verification",
        "server_fingerprint_version",
        "server_fingerprint",
    }
    raw_items = raw.get("items")
    if type(raw_items) is not list:
        raise _error("$.packet.items", "SOURCE_IMPORT_RECEIPT_PACKET_INVALID", "packet items 无效。")
    input_items: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        item_path = f"$.packet.items[{index}]"
        item_map = _require_exact_mapping(item, frozenset(item_output_fields), item_path)
        if item_map.get("external_claims_verification") != EXTERNAL_UNVERIFIED:
            raise _error(
                f"{item_path}.external_claims_verification",
                "SOURCE_IMPORT_RECEIPT_PACKET_INVALID",
                "item external trust 标记无效。",
            )
        if item_map.get("server_fingerprint_version") != SOURCE_ITEM_FINGERPRINT_VERSION:
            raise _error(
                f"{item_path}.server_fingerprint_version",
                "SOURCE_IMPORT_RECEIPT_PACKET_INVALID",
                "item fingerprint version 无效。",
            )
        input_items.append({
            field: copy.deepcopy(item_map[field])
            for field in _ITEM_FIELDS
        })
    input_packet = {
        field: copy.deepcopy(raw[field])
        for field in _PACKET_FIELDS
        if field != "items"
    }
    input_packet["items"] = input_items
    verified = normalize_source_import_packet(
        input_packet,
        received_at_ms=received_at_ms,
    )
    if verified != packet:
        raise _error(
            "$.packet",
            "SOURCE_IMPORT_RECEIPT_PACKET_INVALID",
            "packet 不是当前合同生成的完整规范化投影。",
        )
    return verified


def build_source_import_receipt(
    packet: Any,
    *,
    received_at_ms: int,
    source_payload_bytes: int,
    source_payload_sha256: str,
    status: str = SOURCE_STATUS_AWAITING_USER,
) -> dict[str, Any]:
    """Build an integrity-sealed receipt for one already normalized packet."""

    if type(received_at_ms) is not int or received_at_ms < 0:
        raise _error("$.received_at_ms", "SOURCE_IMPORT_RECEIPT_TIME_INVALID", "received_at_ms 无效。")
    packet = _verified_normalized_packet(packet, received_at_ms=received_at_ms)
    if type(source_payload_bytes) is not int or not 0 <= source_payload_bytes <= MAX_SOURCE_IMPORT_BYTES:
        raise _error("$.source_payload_bytes", "SOURCE_IMPORT_RECEIPT_SIZE_INVALID", "source payload 字节数无效。")
    clean_payload_sha256 = _clean_sha256(
        source_payload_sha256,
        "$.source_payload_sha256",
    )
    clean_status = _clean_text(status, "$.status", maximum=40)
    if clean_status not in SOURCE_IMPORT_STATUSES:
        raise _error("$.status", "SOURCE_IMPORT_STATUS_INVALID", "source import status 不受支持。")
    items = packet.get("items") if type(packet.get("items")) is list else []
    item_fingerprints = [
        str(item.get("server_fingerprint") or "")
        for item in items
        if type(item) is dict
    ]
    if len(item_fingerprints) != len(items) or any(
        not _SHA256_RE.fullmatch(value) for value in item_fingerprints
    ):
        raise _error("$.packet.items", "SOURCE_IMPORT_RECEIPT_PACKET_INVALID", "source item fingerprint 缺失。")
    import_key = {
        "version": SOURCE_IMPORT_KEY_VERSION,
        "source_channel": packet.get("source_channel"),
        "source_key": packet.get("source_key"),
        "external_run_id": packet.get("external_run_id"),
    }
    receipt: dict[str, Any] = {
        "version": SOURCE_IMPORT_RECEIPT_VERSION,
        "status": clean_status,
        "received_at_ms": received_at_ms,
        "source_payload_bytes": source_payload_bytes,
        "source_payload_sha256": clean_payload_sha256,
        "normalized_packet_sha256": canonical_sha256(packet),
        "import_key_version": SOURCE_IMPORT_KEY_VERSION,
        "import_key_sha256": canonical_sha256(import_key),
        "source_channel": str(packet.get("source_channel") or ""),
        "source_key": str(packet.get("source_key") or ""),
        "external_run_id": str(packet.get("external_run_id") or ""),
        "item_count": len(items),
        "source_count": sum(
            len(item.get("sources") or [])
            for item in items
            if type(item) is dict
        ),
        "item_fingerprints": item_fingerprints,
        "external_claims_verification": EXTERNAL_UNVERIFIED,
        "safety": {
            "database_writes_performed": 0,
            "provider_calls_performed": 0,
            "market_calls_performed": 0,
            "network_requests_performed": 0,
            "execution_capability": "none",
            "user_action_required": True,
        },
    }
    receipt["receipt_sha256"] = canonical_sha256(receipt)
    return receipt


def accept_source_import(
    raw: Any,
    *,
    received_at_ms: int,
    receipt_status: str = SOURCE_STATUS_AWAITING_USER,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse, normalize, fingerprint, and receipt one import without side effects."""

    payload = _raw_import_bytes(raw)
    parsed = parse_source_import_json(payload)
    packet = normalize_source_import_packet(parsed, received_at_ms=received_at_ms)
    receipt = build_source_import_receipt(
        packet,
        received_at_ms=received_at_ms,
        source_payload_bytes=len(payload),
        source_payload_sha256=hashlib.sha256(payload).hexdigest(),
        status=receipt_status,
    )
    return packet, receipt


__all__ = [
    "EXTERNAL_UNVERIFIED",
    "MAX_SOURCE_IMPORT_BYTES",
    "MAX_SOURCE_IMPORT_DEPTH",
    "MAX_SOURCE_ITEMS",
    "MAX_SOURCES_PER_ITEM",
    "MAX_TOTAL_SOURCES",
    "PROJECT_SOURCE_ITEM_VERSION",
    "SOURCE_IMPORT_PACKET_VERSION",
    "SOURCE_IMPORT_RECEIPT_VERSION",
    "SOURCE_IMPORT_STATUSES",
    "SOURCE_ITEM_FINGERPRINT_VERSION",
    "SOURCE_RECOMMENDED_ROUTES",
    "SOURCE_STATUS_ATTACHED",
    "SOURCE_STATUS_AWAITING_USER",
    "SOURCE_STATUS_DUPLICATE",
    "SOURCE_STATUS_EXPIRED",
    "SOURCE_STATUS_RECEIVED",
    "SOURCE_STATUS_REJECTED",
    "SOURCE_STATUS_ROUND_DRAFTED",
    "SOURCE_STATUS_VALIDATED",
    "SourceContractIssue",
    "SourceInboxContractError",
    "accept_source_import",
    "build_source_import_receipt",
    "canonical_sha256",
    "canonicalize_source_url",
    "normalize_source_import_packet",
    "parse_source_import_json",
    "project_source_item_fingerprint",
]
