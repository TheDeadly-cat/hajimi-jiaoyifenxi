from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_CONFIG_BYTES = 64 * 1024
MAX_CONFIG_LINE_CHARS = 4_096
MAX_CHAT_POLICIES = 500
MAX_NAME_CHARS = 160
MAX_CATEGORY_CHARS = 80
ALLOWED_PRIORITIES = frozenset({"low", "medium", "high"})
_FIELD_NAMES = frozenset(
    {"chat_id", "enabled", "name", "category", "priority", "trust_score"}
)
_KEY_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


class ChatConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChatPolicy:
    chat_id: int
    enabled: bool = False
    name: str = ""
    category: str = "general"
    priority: str = "medium"
    trust_score: int = 50

    def __post_init__(self) -> None:
        if type(self.chat_id) is not int or self.chat_id == 0:
            raise ChatConfigError("chat_id must be a non-zero integer")
        if not (-(2**63) <= self.chat_id < 2**63):
            raise ChatConfigError("chat_id is outside signed 64-bit range")
        if type(self.enabled) is not bool:
            raise ChatConfigError("enabled must be a boolean")
        if type(self.name) is not str or len(self.name.strip()) > MAX_NAME_CHARS:
            raise ChatConfigError("name must be a bounded string")
        if (
            type(self.category) is not str
            or not self.category.strip()
            or len(self.category.strip()) > MAX_CATEGORY_CHARS
        ):
            raise ChatConfigError("category must be a non-empty bounded string")
        priority = self.priority.strip().lower() if type(self.priority) is str else ""
        if priority not in ALLOWED_PRIORITIES:
            raise ChatConfigError("priority must be low, medium, or high")
        if type(self.trust_score) is not int or not 0 <= self.trust_score <= 100:
            raise ChatConfigError("trust_score must be an integer from 0 to 100")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "category", self.category.strip())
        object.__setattr__(self, "priority", priority)


@dataclass(frozen=True, slots=True)
class TelegramCollectorConfig:
    chats: tuple[ChatPolicy, ...] = ()

    def __post_init__(self) -> None:
        if type(self.chats) is not tuple:
            raise ChatConfigError("chats must be a tuple")
        if len(self.chats) > MAX_CHAT_POLICIES:
            raise ChatConfigError("too many chat policies")
        seen: set[int] = set()
        for policy in self.chats:
            if not isinstance(policy, ChatPolicy):
                raise ChatConfigError("chats must contain ChatPolicy values")
            if policy.chat_id in seen:
                raise ChatConfigError(f"duplicate chat_id: {policy.chat_id}")
            seen.add(policy.chat_id)

    def policy_for(self, chat_id: int) -> ChatPolicy | None:
        if type(chat_id) is not int:
            return None
        for policy in self.chats:
            if policy.chat_id == chat_id:
                return policy
        return None

    def is_allowed(self, chat_id: int) -> bool:
        policy = self.policy_for(chat_id)
        return policy is not None and policy.enabled


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChatConfigError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ChatConfigError(f"non-finite JSON value is forbidden: {value}")


def _strip_yaml_comment(line: str) -> str:
    single = False
    double = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and double:
            escaped = True
            continue
        if char == "'" and not double:
            single = not single
            continue
        if char == '"' and not single:
            double = not double
            continue
        if char == "#" and not single and not double:
            return line[:index].rstrip()
    if single or double:
        raise ChatConfigError("unterminated quoted YAML scalar")
    return line.rstrip()


def _parse_yaml_scalar(raw_value: str, line_number: int) -> Any:
    value = raw_value.strip()
    if not value:
        raise ChatConfigError(f"line {line_number}: values may not be empty")
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ChatConfigError(
                f"line {line_number}: invalid double-quoted string"
            ) from exc
        if type(parsed) is not str:
            raise ChatConfigError(f"line {line_number}: scalar must be a string")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ChatConfigError(f"line {line_number}: invalid quoted string")
        return value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value):
        try:
            return int(value)
        except ValueError as exc:
            raise ChatConfigError(f"line {line_number}: invalid integer") from exc
    if any(char in value for char in "{}[]&*!|>@") or chr(96) in value:
        raise ChatConfigError(f"line {line_number}: unsupported YAML syntax")
    if value.startswith(("-", "?", ":")) or ": " in value:
        raise ChatConfigError(f"line {line_number}: ambiguous YAML scalar")
    return value


def _parse_key_value(value: str, line_number: int) -> tuple[str, Any]:
    if ":" not in value:
        raise ChatConfigError(f"line {line_number}: expected key: value")
    key, raw_value = value.split(":", 1)
    key = key.strip()
    if _KEY_RE.fullmatch(key) is None:
        raise ChatConfigError(f"line {line_number}: invalid field name")
    return key, _parse_yaml_scalar(raw_value, line_number)


def _parse_bounded_yaml(text: str) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    root_seen = False
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if len(raw_line) > MAX_CONFIG_LINE_CHARS:
            raise ChatConfigError(f"line {line_number}: line is too long")
        if "\t" in raw_line:
            raise ChatConfigError(f"line {line_number}: tabs are forbidden")
        line = _strip_yaml_comment(raw_line)
        if not line.strip():
            continue
        if not root_seen:
            if line != "chats:":
                raise ChatConfigError("YAML root must be exactly chats:")
            root_seen = True
            continue
        if line.startswith("  -"):
            if not line.startswith("  - ") or len(line) <= 4:
                raise ChatConfigError(
                    f"line {line_number}: list item must contain a field"
                )
            if len(records) >= MAX_CHAT_POLICIES:
                raise ChatConfigError("too many chat policies")
            current = {}
            records.append(current)
            key, parsed = _parse_key_value(line[4:], line_number)
        elif line.startswith("    ") and not line.startswith("     "):
            if current is None:
                raise ChatConfigError(
                    f"line {line_number}: field appears before a list item"
                )
            key, parsed = _parse_key_value(line[4:], line_number)
        else:
            raise ChatConfigError(
                f"line {line_number}: only two/four-space indentation is accepted"
            )
        if key in current:
            raise ChatConfigError(f"line {line_number}: duplicate key: {key}")
        current[key] = parsed
    if not root_seen:
        raise ChatConfigError("missing chats root")
    return {"chats": records}


def _build_config(value: Any) -> TelegramCollectorConfig:
    if type(value) is not dict or set(value) != {"chats"}:
        raise ChatConfigError("config must contain exactly one chats field")
    records = value["chats"]
    if type(records) is not list:
        raise ChatConfigError("chats must be a list")
    if len(records) > MAX_CHAT_POLICIES:
        raise ChatConfigError("too many chat policies")
    policies: list[ChatPolicy] = []
    seen: set[int] = set()
    for index, record in enumerate(records):
        if type(record) is not dict:
            raise ChatConfigError(f"chats[{index}] must be an object")
        unknown = set(record) - _FIELD_NAMES
        if unknown:
            raise ChatConfigError(
                f"chats[{index}] has unknown fields: {', '.join(sorted(unknown))}"
            )
        if "chat_id" not in record:
            raise ChatConfigError(f"chats[{index}] is missing chat_id")
        try:
            policy = ChatPolicy(
                chat_id=record["chat_id"],
                enabled=record.get("enabled", False),
                name=record.get("name", ""),
                category=record.get("category", "general"),
                priority=record.get("priority", "medium"),
                trust_score=record.get("trust_score", 50),
            )
        except ChatConfigError as exc:
            raise ChatConfigError(f"chats[{index}]: {exc}") from exc
        if policy.chat_id in seen:
            raise ChatConfigError(f"duplicate chat_id: {policy.chat_id}")
        seen.add(policy.chat_id)
        policies.append(policy)
    return TelegramCollectorConfig(tuple(policies))


def parse_chats_config(raw: str | bytes) -> TelegramCollectorConfig:
    if isinstance(raw, bytes):
        if len(raw) > MAX_CONFIG_BYTES:
            raise ChatConfigError("chat config exceeds size limit")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ChatConfigError("chat config must be UTF-8") from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ChatConfigError("chat config exceeds size limit")
        text = raw.removeprefix("\ufeff")
    else:
        raise TypeError("raw config must be str or bytes")
    if "\x00" in text:
        raise ChatConfigError("NUL bytes are forbidden")
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            value = json.loads(
                text,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except ChatConfigError:
            raise
        except (RecursionError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ChatConfigError("invalid JSON chat config") from exc
    else:
        value = _parse_bounded_yaml(text)
    return _build_config(value)


def load_chats_config(path: str | os.PathLike[str]) -> TelegramCollectorConfig:
    config_path = Path(path)
    try:
        size = config_path.stat().st_size
    except OSError as exc:
        raise ChatConfigError("unable to read chat config") from exc
    if size > MAX_CONFIG_BYTES:
        raise ChatConfigError("chat config exceeds size limit")
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ChatConfigError("unable to read chat config") from exc
    return parse_chats_config(raw)
