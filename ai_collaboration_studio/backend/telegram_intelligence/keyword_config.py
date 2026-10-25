from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_KEYWORD_CONFIG_BYTES = 128 * 1024
MAX_KEYWORD_CONFIG_LINES = 4_000
MAX_RULE_GROUPS = 64
MAX_RULE_ITEMS = 512
_ROOT_KEYS = frozenset(
    {
        "groups",
        "ticker_whitelist",
        "ambiguous_tickers",
        "ticker_context",
        "breaking_keywords",
    }
)
_GROUP_KEYS = frozenset({"keywords", "regex", "tickers"})
_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,79}\Z")


class KeywordConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class KeywordRuleConfig:
    rule_config: dict[str, Any]
    breaking_keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _YamlLine:
    indent: int
    text: str
    number: int


def _strip_comment(value: str, line_number: int) -> str:
    single = False
    double = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and double:
            escaped = True
        elif character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
        elif character == "#" and not single and not double:
            return value[:index].rstrip()
    if single or double:
        raise KeywordConfigError(f"line {line_number}: unterminated quote")
    return value.rstrip()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise KeywordConfigError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise KeywordConfigError(f"non-finite JSON value is forbidden: {value}")


def _scalar(value: str, line_number: int) -> Any:
    clean = value.strip()
    if not clean:
        raise KeywordConfigError(f"line {line_number}: empty scalar")
    if clean.startswith(('"', "'")):
        if clean.startswith('"'):
            try:
                parsed = json.loads(clean)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise KeywordConfigError(
                    f"line {line_number}: invalid quoted string"
                ) from exc
            if type(parsed) is not str:
                raise KeywordConfigError(f"line {line_number}: expected string")
            return parsed
        if len(clean) < 2 or not clean.endswith("'"):
            raise KeywordConfigError(f"line {line_number}: invalid quoted string")
        return clean[1:-1].replace("''", "'")
    if clean in {"[]", "{}"}:
        return [] if clean == "[]" else {}
    lowered = clean.casefold()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", clean):
        return int(clean)
    if clean[0] in "&*!|>@`" or " <<:" in f" {clean}":
        raise KeywordConfigError(f"line {line_number}: unsupported YAML feature")
    if any(ord(character) < 32 for character in clean):
        raise KeywordConfigError(f"line {line_number}: control character forbidden")
    return clean


def _mapping_pair(value: str, line_number: int) -> tuple[str, str]:
    single = False
    double = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and double:
            escaped = True
        elif character == "'" and not double:
            single = not single
        elif character == '"' and not single:
            double = not double
        elif character == ":" and not single and not double:
            key = value[:index].strip()
            if _KEY_RE.fullmatch(key) is None:
                raise KeywordConfigError(f"line {line_number}: invalid key")
            return key, value[index + 1 :]
    raise KeywordConfigError(f"line {line_number}: expected key: value")


def _tokenize_yaml(text: str) -> tuple[_YamlLine, ...]:
    rows: list[_YamlLine] = []
    for number, raw in enumerate(text.splitlines(), 1):
        if number > MAX_KEYWORD_CONFIG_LINES:
            raise KeywordConfigError("keyword config has too many lines")
        if "\t" in raw:
            raise KeywordConfigError(f"line {number}: tabs are forbidden")
        line = _strip_comment(raw, number)
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2 or indent > 10:
            raise KeywordConfigError(f"line {number}: invalid indentation")
        rows.append(_YamlLine(indent, line[indent:], number))
    return tuple(rows)


def _parse_yaml_block(
    rows: tuple[_YamlLine, ...],
    index: int,
    indent: int,
) -> tuple[Any, int]:
    if index >= len(rows) or rows[index].indent != indent:
        raise KeywordConfigError("invalid YAML block indentation")
    is_list = rows[index].text.startswith("- ")
    if is_list:
        output: list[Any] = []
        while index < len(rows) and rows[index].indent == indent:
            row = rows[index]
            if not row.text.startswith("- "):
                raise KeywordConfigError(f"line {row.number}: mixed list and mapping")
            if len(output) >= MAX_RULE_ITEMS:
                raise KeywordConfigError("too many items in YAML list")
            raw_value = row.text[2:].strip()
            if not raw_value:
                raise KeywordConfigError(f"line {row.number}: empty list item")
            output.append(_scalar(raw_value, row.number))
            index += 1
            if index < len(rows) and rows[index].indent > indent:
                raise KeywordConfigError(
                    f"line {rows[index].number}: nested list items are unsupported"
                )
        return output, index

    output_map: dict[str, Any] = {}
    while index < len(rows) and rows[index].indent == indent:
        row = rows[index]
        if row.text.startswith("- "):
            raise KeywordConfigError(f"line {row.number}: mixed mapping and list")
        key, raw_value = _mapping_pair(row.text, row.number)
        if key in output_map:
            raise KeywordConfigError(f"line {row.number}: duplicate key: {key}")
        index += 1
        if raw_value.strip():
            output_map[key] = _scalar(raw_value, row.number)
            if index < len(rows) and rows[index].indent > indent:
                raise KeywordConfigError(
                    f"line {rows[index].number}: scalar cannot own a nested block"
                )
            continue
        if index >= len(rows) or rows[index].indent <= indent:
            raise KeywordConfigError(f"line {row.number}: missing nested value")
        if rows[index].indent != indent + 2:
            raise KeywordConfigError(
                f"line {rows[index].number}: indentation must increase by two"
            )
        nested, index = _parse_yaml_block(rows, index, indent + 2)
        output_map[key] = nested
    return output_map, index


def _decode(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        if len(raw) > MAX_KEYWORD_CONFIG_BYTES:
            raise KeywordConfigError("keyword config exceeds size limit")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise KeywordConfigError("keyword config must be UTF-8") from exc
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_KEYWORD_CONFIG_BYTES:
            raise KeywordConfigError("keyword config exceeds size limit")
        text = raw.removeprefix("\ufeff")
    else:
        raise TypeError("keyword config must be text or bytes")
    if "\x00" in text:
        raise KeywordConfigError("NUL bytes are forbidden")
    if text.lstrip().startswith(('{', '[')):
        try:
            return json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except KeywordConfigError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise KeywordConfigError("invalid JSON keyword config") from exc
    rows = _tokenize_yaml(text)
    if not rows:
        raise KeywordConfigError("keyword config is empty")
    if rows[0].indent != 0:
        raise KeywordConfigError("YAML root must start at column zero")
    value, index = _parse_yaml_block(rows, 0, 0)
    if index != len(rows):
        raise KeywordConfigError(f"line {rows[index].number}: invalid block")
    return value


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if type(value) is not list or len(value) > MAX_RULE_ITEMS:
        raise KeywordConfigError(f"{field} must be a bounded list")
    output: list[str] = []
    for item in value:
        if type(item) is not str or not item.strip() or len(item.strip()) > 256:
            raise KeywordConfigError(f"{field} contains an invalid string")
        clean = item.strip()
        if clean not in output:
            output.append(clean)
    return tuple(output)


def parse_keywords_config(raw: str | bytes) -> KeywordRuleConfig:
    value = _decode(raw)
    if type(value) is not dict:
        raise KeywordConfigError("keyword config root must be a mapping")
    unknown = set(value) - _ROOT_KEYS
    if unknown:
        raise KeywordConfigError(
            "unknown keyword config fields: " + ", ".join(sorted(unknown))
        )
    groups = value.get("groups")
    if type(groups) is not dict or not groups or len(groups) > MAX_RULE_GROUPS:
        raise KeywordConfigError("groups must be a non-empty bounded mapping")
    clean_groups: dict[str, dict[str, list[str]]] = {}
    for name, group in groups.items():
        if _KEY_RE.fullmatch(str(name)) is None or type(group) is not dict:
            raise KeywordConfigError("invalid rule group")
        if set(group) - _GROUP_KEYS:
            raise KeywordConfigError(f"group {name} contains unknown fields")
        clean_group = {
            key: list(_string_list(group.get(key, []), f"groups.{name}.{key}"))
            for key in ("keywords", "regex", "tickers")
        }
        if not any(clean_group.values()):
            raise KeywordConfigError(f"group {name} has no rules")
        clean_groups[str(name)] = clean_group

    ticker_whitelist = value.get("ticker_whitelist", {})
    if type(ticker_whitelist) is not dict or len(ticker_whitelist) > MAX_RULE_ITEMS:
        raise KeywordConfigError("ticker_whitelist must be a bounded mapping")
    clean_tickers: dict[str, list[str]] = {}
    for ticker, aliases in ticker_whitelist.items():
        clean = str(ticker).strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", clean) is None:
            raise KeywordConfigError(f"invalid ticker: {ticker}")
        clean_tickers[clean] = list(_string_list(aliases, f"ticker_whitelist.{clean}"))

    rule_config = {
        "groups": clean_groups,
        "ticker_whitelist": clean_tickers,
        "ambiguous_tickers": list(
            _string_list(value.get("ambiguous_tickers", []), "ambiguous_tickers")
        ),
        "ticker_context": list(
            _string_list(value.get("ticker_context", []), "ticker_context")
        ),
    }
    # Empty optional lists mean RuleEngine's explicit defaults should be used.
    if not rule_config["ambiguous_tickers"]:
        rule_config.pop("ambiguous_tickers")
    if not rule_config["ticker_context"]:
        rule_config.pop("ticker_context")
    return KeywordRuleConfig(
        rule_config=rule_config,
        breaking_keywords=_string_list(
            value.get("breaking_keywords", []),
            "breaking_keywords",
        ),
    )


def load_keywords_config(path: str | Path) -> KeywordRuleConfig:
    target = Path(path)
    try:
        if target.stat().st_size > MAX_KEYWORD_CONFIG_BYTES:
            raise KeywordConfigError("keyword config exceeds size limit")
        raw = target.read_bytes()
    except KeywordConfigError:
        raise
    except OSError as exc:
        raise KeywordConfigError("unable to read keyword config") from exc
    return parse_keywords_config(raw)


__all__ = [
    "KeywordConfigError",
    "KeywordRuleConfig",
    "load_keywords_config",
    "parse_keywords_config",
]
