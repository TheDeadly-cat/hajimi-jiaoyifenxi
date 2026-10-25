from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


AMBIGUOUS_TICKERS = frozenset({"AI", "IT", "ON", "CAT"})
DEFAULT_TICKER_CONTEXT = (
    "stock",
    "stocks",
    "share",
    "shares",
    "equity",
    "earnings",
    "guidance",
    "nasdaq",
    "nyse",
    "ticker",
    "market cap",
    "price target",
    "股票",
    "股价",
    "股份",
    "美股",
    "财报",
    "指引",
    "市值",
    "目标价",
    "代码",
)
_EXPLICIT_TICKER_RE_TEMPLATE = (
    r"(?:\${ticker}\b|\b(?:NASDAQ|NYSE|AMEX)\s*[:：]\s*{ticker}\b"
    r"|\bTICKER\s*[:：]?\s*{ticker}\b|代码\s*[:：]?\s*{ticker}(?![A-Za-z0-9]))"
)
_TICKER_TOKEN_TEMPLATE = r"(?<![A-Za-z0-9]){ticker}(?![A-Za-z0-9])"


def _native_text(value: Any) -> str:
    if type(value) is not str:
        raise TypeError("rule text must be a native string")
    return unicodedata.normalize("NFKC", value)


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    clean = unicodedata.normalize("NFKC", keyword).strip()
    if not clean:
        raise ValueError("keywords must not be empty")
    escaped = re.escape(clean)
    if all(ord(character) < 128 for character in clean) and clean[0].isalnum() and clean[-1].isalnum():
        escaped = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
    return re.compile(escaped, re.IGNORECASE)


def _compile_regex(value: Any) -> re.Pattern[str]:
    if type(value) is not str:
        raise TypeError("regex rules must be native strings")
    pattern = value.strip()
    if not pattern or len(pattern) > 256:
        raise ValueError("regex rules must contain 1 to 256 characters")
    _assert_safe_regex(pattern)
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid regex rule: {exc}") from exc


def _assert_safe_regex(pattern: str) -> None:
    """Reject common catastrophic-backtracking constructs.

    Operator-supplied patterns intentionally use a conservative subset of
    Python regex: ordinary groups and non-capturing groups are supported, while
    lookarounds, backreferences, nested repetition, quantified alternations,
    and all unbounded repeats are not.  This is a reliability boundary, not a
    general-purpose regular-expression sandbox.
    """

    if len(re.findall(r"(?<!\\)\.\s*[+*]", pattern)) > 1:
        raise ValueError("regex rules may contain at most one unbounded wildcard")
    def state() -> dict[str, Any]:
        return {
            "repeat": False,
            "alternation": False,
            "branch_unbounded": 0,
            "has_unbounded": False,
            "branch_variable": 0,
            "has_variable": False,
        }

    def mark_variable(current: dict[str, Any]) -> None:
        if int(current["branch_variable"]) >= 1:
            raise ValueError(
                "regex rules allow at most one variable repeat per branch"
            )
        current["branch_variable"] = int(current["branch_variable"]) + 1
        current["has_variable"] = True
        current["repeat"] = True

    def mark_unbounded(current: dict[str, Any]) -> None:
        del current
        # Patterns are applied with unanchored search to untrusted Telegram
        # text.  Even a single ``+``/``*``/``{m,}`` can become quadratic on a
        # long non-match because the engine retries at every start position.
        raise ValueError("regex unbounded repeats are not supported")

    stack: list[dict[str, Any]] = [state()]
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            if index + 1 >= len(pattern):
                break
            if pattern[index + 1].isdigit():
                raise ValueError("regex backreferences are not supported")
            index += 2
            continue
        if char == "[" and not in_class:
            in_class = True
            index += 1
            continue
        if char == "]" and in_class:
            in_class = False
            index += 1
            continue
        if in_class:
            index += 1
            continue
        if char == "(":
            if pattern.startswith("(?:", index):
                index += 3
            elif index + 1 < len(pattern) and pattern[index + 1] == "?":
                raise ValueError("regex lookarounds and special groups are not supported")
            else:
                index += 1
            stack.append(state())
            continue
        if char == "|":
            stack[-1]["alternation"] = True
            stack[-1]["branch_unbounded"] = 0
            stack[-1]["branch_variable"] = 0
            index += 1
            continue
        if char == ")":
            if len(stack) == 1:
                index += 1
                continue
            child = stack.pop()
            next_index = index + 1
            quantifier = ""
            if next_index < len(pattern) and pattern[next_index] in "*+?":
                quantifier = pattern[next_index]
                next_index += 1
            elif next_index < len(pattern) and pattern[next_index] == "{":
                closing = pattern.find("}", next_index + 1)
                if closing >= 0:
                    quantifier = pattern[next_index : closing + 1]
                    next_index = closing + 1
            if quantifier:
                unbounded = quantifier in {"*", "+"} or (
                    quantifier.startswith("{") and ",}" in quantifier
                )
                if child["repeat"] or child["alternation"]:
                    raise ValueError("regex nested or ambiguous repetition is not supported")
                if unbounded:
                    mark_unbounded(stack[-1])
                elif quantifier == "?" or "," in quantifier:
                    mark_variable(stack[-1])
                else:
                    stack[-1]["repeat"] = True
                index = next_index
                continue
            if child["repeat"]:
                stack[-1]["repeat"] = True
            if child["has_unbounded"]:
                mark_unbounded(stack[-1])
            elif child["has_variable"]:
                mark_variable(stack[-1])
            index += 1
            continue
        if char in "*+?":
            if index + 1 < len(pattern) and pattern[index + 1] in "*+?":
                raise ValueError("regex stacked quantifiers are not supported")
            if char in "*+":
                mark_unbounded(stack[-1])
            else:
                mark_variable(stack[-1])
            index += 1
            continue
        if char == "{":
            closing = pattern.find("}", index + 1)
            if closing >= 0 and re.fullmatch(r"\{\d+(?:,\d*)?\}", pattern[index : closing + 1]):
                quantifier = pattern[index : closing + 1]
                values = quantifier[1:-1].split(",", 1)
                lower = int(values[0])
                upper = (
                    None
                    if len(values) == 2 and values[1] == ""
                    else int(values[-1])
                )
                if lower > 100 or (upper is not None and upper > 100):
                    raise ValueError("regex repeat bounds may not exceed 100")
                if upper is not None and upper < lower:
                    raise ValueError("regex repeat bounds are invalid")
                if ",}" in quantifier:
                    mark_unbounded(stack[-1])
                elif len(values) == 2 and upper != lower:
                    if upper is not None and upper - lower > 20:
                        raise ValueError("regex variable repeat span may not exceed 20")
                    mark_variable(stack[-1])
                else:
                    stack[-1]["repeat"] = True
                index = closing + 1
                continue
        index += 1


def _iter_strings(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"{field} must be a sequence of strings")
    output: list[str] = []
    for item in value:
        if type(item) is not str or not item.strip():
            raise ValueError(f"{field} contains an invalid string")
        clean = unicodedata.normalize("NFKC", item).strip()
        if clean not in output:
            output.append(clean)
    return tuple(output)


@dataclass(frozen=True, slots=True)
class RuleGroup:
    name: str
    keywords: tuple[str, ...] = ()
    regex: tuple[str, ...] = ()
    tickers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuleMatch:
    categories: tuple[str, ...]
    keyword_matches: tuple[str, ...]
    regex_matches: tuple[str, ...]
    tickers: tuple[str, ...]
    ticker_evidence: tuple[str, ...]

    @property
    def matched(self) -> bool:
        return bool(self.categories or self.tickers)

    @property
    def match_count(self) -> int:
        return len(self.keyword_matches) + len(self.regex_matches) + len(self.tickers)


class RuleEngine:
    """Fast local keyword, regex, and guarded ticker matching."""

    def __init__(
        self,
        groups: Mapping[str, Any],
        *,
        ticker_whitelist: Mapping[str, Iterable[str]] | Iterable[str] = (),
        ambiguous_tickers: Iterable[str] = AMBIGUOUS_TICKERS,
        ticker_context: Iterable[str] = DEFAULT_TICKER_CONTEXT,
    ) -> None:
        if not isinstance(groups, Mapping):
            raise TypeError("groups must be a mapping")
        normalized_groups: list[RuleGroup] = []
        for raw_name, raw_value in groups.items():
            name = str(raw_name or "").strip()
            if not name:
                raise ValueError("rule group names must not be empty")
            if isinstance(raw_value, Mapping):
                keywords = _iter_strings(raw_value.get("keywords", ()), field=f"{name}.keywords")
                regex = _iter_strings(
                    raw_value.get("regex", raw_value.get("patterns", ())),
                    field=f"{name}.regex",
                )
                tickers = tuple(
                    item.upper()
                    for item in _iter_strings(raw_value.get("tickers", ()), field=f"{name}.tickers")
                )
            else:
                keywords = _iter_strings(raw_value, field=f"{name}.keywords")
                regex = ()
                tickers = ()
            normalized_groups.append(RuleGroup(name, keywords, regex, tickers))
        self.groups = tuple(normalized_groups)
        self._keyword_patterns = {
            group.name: tuple((item, _keyword_pattern(item)) for item in group.keywords)
            for group in self.groups
        }
        self._regex_patterns = {
            group.name: tuple((item, _compile_regex(item)) for item in group.regex)
            for group in self.groups
        }

        whitelist: dict[str, tuple[str, ...]] = {}
        if isinstance(ticker_whitelist, Mapping):
            rows = ticker_whitelist.items()
        else:
            rows = ((ticker, ()) for ticker in ticker_whitelist)
        for raw_ticker, raw_aliases in rows:
            ticker = str(raw_ticker or "").strip().upper()
            if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker):
                raise ValueError(f"invalid ticker: {raw_ticker}")
            aliases = tuple(
                unicodedata.normalize("NFKC", str(alias or "")).strip()
                for alias in raw_aliases
                if str(alias or "").strip()
            )
            whitelist[ticker] = tuple(dict.fromkeys(aliases))
        for group in self.groups:
            for ticker in group.tickers:
                if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker):
                    raise ValueError(f"invalid ticker: {ticker}")
                whitelist.setdefault(ticker, ())
        self.ticker_whitelist = whitelist
        self.ambiguous_tickers = frozenset(str(item).strip().upper() for item in ambiguous_tickers)
        self.ticker_context = tuple(
            unicodedata.normalize("NFKC", str(item or "")).strip().casefold()
            for item in ticker_context
            if str(item or "").strip()
        )

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "RuleEngine":
        groups = config.get("groups", config.get("categories", config))
        ticker_whitelist = config.get("ticker_whitelist", {})
        if groups is config:
            groups = {
                key: value
                for key, value in config.items()
                if key not in {"ticker_whitelist", "ambiguous_tickers", "ticker_context"}
            }
        return cls(
            groups,
            ticker_whitelist=ticker_whitelist,
            ambiguous_tickers=config.get("ambiguous_tickers", AMBIGUOUS_TICKERS),
            ticker_context=config.get("ticker_context", DEFAULT_TICKER_CONTEXT),
        )

    def _has_context(self, text: str, start: int, end: int) -> bool:
        window = text[max(0, start - 64) : min(len(text), end + 64)].casefold()
        return any(context in window for context in self.ticker_context)

    def _match_tickers(self, text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        found: list[str] = []
        evidence: list[str] = []
        for ticker, aliases in self.ticker_whitelist.items():
            explicit = re.search(
                _EXPLICIT_TICKER_RE_TEMPLATE.format(ticker=re.escape(ticker)),
                text,
                flags=re.IGNORECASE,
            )
            if explicit:
                found.append(ticker)
                evidence.append(f"{ticker}:explicit")
                continue
            alias = next(
                (
                    item
                    for item in aliases
                    if _keyword_pattern(item).search(text)
                ),
                "",
            )
            if alias:
                found.append(ticker)
                evidence.append(f"{ticker}:alias")
                continue
            if ticker in self.ambiguous_tickers:
                # Ordinary uses of AI/IT/ON/CAT are far more common than their
                # ticker meanings. Only cashtags, venue/ticker notation, or a
                # configured company alias may unlock these symbols.
                continue
            for token_match in re.finditer(
                _TICKER_TOKEN_TEMPLATE.format(ticker=re.escape(ticker)),
                text,
                flags=re.IGNORECASE,
            ):
                token = token_match.group(0)
                uppercase_token = token == ticker
                needs_context = not uppercase_token
                if needs_context and not self._has_context(text, *token_match.span()):
                    continue
                found.append(ticker)
                evidence.append(
                    f"{ticker}:context" if needs_context else f"{ticker}:uppercase_token"
                )
                break
        return tuple(dict.fromkeys(found)), tuple(dict.fromkeys(evidence))

    def match(self, text: str) -> RuleMatch:
        # TDLib messages are naturally bounded, but injected/offline inputs may
        # not be.  Keep deterministic rule work bounded as defense in depth.
        clean_text = _native_text(text)[:32_768]
        categories: list[str] = []
        keywords: list[str] = []
        regexes: list[str] = []
        for group in self.groups:
            group_matched = False
            for keyword, pattern in self._keyword_patterns[group.name]:
                if pattern.search(clean_text):
                    keywords.append(f"{group.name}:{keyword}")
                    group_matched = True
            for expression, pattern in self._regex_patterns[group.name]:
                if pattern.search(clean_text):
                    regexes.append(f"{group.name}:{expression}")
                    group_matched = True
            if group_matched:
                categories.append(group.name)
        tickers, evidence = self._match_tickers(clean_text)
        ticker_categories = {
            group.name
            for group in self.groups
            if set(group.tickers) & set(tickers)
        }
        categories.extend(group.name for group in self.groups if group.name in ticker_categories)
        return RuleMatch(
            categories=tuple(dict.fromkeys(categories)),
            keyword_matches=tuple(dict.fromkeys(keywords)),
            regex_matches=tuple(dict.fromkeys(regexes)),
            tickers=tickers,
            ticker_evidence=evidence,
        )


KeywordRuleEngine = RuleEngine


__all__ = [
    "AMBIGUOUS_TICKERS",
    "DEFAULT_TICKER_CONTEXT",
    "KeywordRuleEngine",
    "RuleEngine",
    "RuleGroup",
    "RuleMatch",
]
