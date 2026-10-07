"""Closeout receipt contract audit (X-07).

Every receipt name this tool expects is derived from the bound source's own
write calls, so each expectation carries a ``file:line`` contract source.  A
name that appears only in an external checklist and has no writer in the bound
source is reported as UNVERIFIABLE, never as missing.

Three states are kept strictly apart:

``EXISTS``
    A file matched the derived pattern **at the contract's own base and full
    relative path**, or the exact expected path was directly probed and found.
    A same-named file in another directory or another scope never establishes
    this state; it is only reported under ``observed_in_scopes``.
``ABSENT_IN_CHECKED_SCOPE``
    The pattern was fully resolved, its expected base directory was readable,
    the check over that base was complete, and no file matched.  This says
    nothing about whether the writing code ran.
``UNVERIFIABLE``
    Anything that cannot support a positive or negative claim, including:

    * the pattern could not be resolved statically (a command-line supplied
      output path, an unresolved conditional branch, a runtime-only value);
    * no writer contract for that name exists in the bound source;
    * the expected base directory was not part of the checked scope;
    * the base directory does not exist or could not be listed;
    * the listing was truncated, or a recursive walk reported an error, so the
      check over that base did not complete;
    * an external request names a base that conflicts with the writer's base,
      or the name maps to several writers with different bases.

Absence is a statement about the checked scope only.  It must never be read as
"the historical code did not execute": the bound source routes write failures
into in-memory structures (``backend/news_review_stop.py`` catches its own
receipt write and records only a bounded exception type), so a missing file and
an unexecuted writer are indistinguishable from file evidence alone.  Equally,
an incomplete check must never be reported as absence.

A *complete contract* (a literal name, or a name family with a literal stem
such as ``stop-{*}-{*}.json``) identifies one receipt.  A pattern with no
literal stem cannot discriminate the target from any other JSON file, so it is
reported UNVERIFIABLE rather than matched loosely.

Read-only static analysis plus read-only directory listing.  No network,
database, process, credential or model access.
"""
from __future__ import annotations

import argparse
import ast
import collections
import datetime
import fnmatch
import hashlib
import json
import os
import re
import sys

VERSION = "closeout_receipt_contract_audit_v2"
WRITER_FUNCS = frozenset({"write_record", "publish_json_once"})
MAX_LISTING_PER_DIR = 200_000
DYNAMIC = "{*}"

NON_IMPLICATION = (
    "ABSENT_IN_CHECKED_SCOPE means no file in the recorded scope matched the "
    "derived pattern. It does not imply the writing code failed to execute: "
    "write_record failures are caught in-process (backend/news_review_stop.py) "
    "and recorded only as a bounded exception type inside a report that may "
    "itself be absent."
)

# Semantic location of each base directory expression, with the assignment that
# establishes it.  These are assertions made by this tool and are therefore
# labelled as such in the output; every entry cites its own evidence so a
# reviewer can confirm or reject it without trusting the table.
BASE_SEMANTICS = {
    ("scripts/run_news_review_trial.py", "root"): (
        "trial_root",
        "scripts/run_news_review_trial.py:84 root = database.parent.parent",
    ),
    ("scripts/run_news_review_trial.py", "Path(args.output).resolve()"): (
        "cli_supplied",
        "scripts/run_news_review_trial.py:137 --output or default report-<uuid>.json",
    ),
    ("backend/news_review_stop.py", "self.root"): (
        "trial_root",
        "scripts/run_news_review_trial.py:238 NewsReviewStop(..., root); "
        "backend/news_review_stop.py:19 self.root = root",
    ),
    ("scripts/run_news_review_observer.py", "directory"): (
        "monitoring",
        "scripts/run_news_review_observer.py:336 directory = safe_path(plan['output_directory']); "
        "prepare() sets output_directory = str(root/'monitoring')",
    ),
    ("backend/news_review_monitor.py", "self.directory"): (
        "monitoring",
        "scripts/run_news_review_observer.py:387 MonitorReceiptWriter(directory, ...); "
        "backend/news_review_monitor.py:70 self.directory = Path(directory)",
    ),
    ("scripts/run_news_review_observer.py", "self.directory"): (
        "monitoring",
        "scripts/run_news_review_observer.py:221 NativeInspector(plan, directory)",
    ),
    ("scripts/news_review_observer_wait.py", "directory"): (
        "monitoring",
        "scripts/news_review_observer_wait.py:234 directory = core.safe_path(plan['output_directory'])",
    ),
}


class ContractExtractionError(RuntimeError):
    """Raised when the bound source cannot be read for contract extraction."""


def _relative(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _iter_python_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in {"node_modules", "__pycache__", ".git", "dist", ".venv"}
        )
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _is_non_product(rel: str) -> bool:
    """Tests and rehearsal scripts are fixtures, not receipt contracts."""
    return (
        rel.startswith("tests/")
        or "/tests/" in rel
        or os.path.basename(rel).startswith("test_")
        or os.path.basename(rel).startswith("rehearse_")
    )


def _single_assignments(scope: ast.AST) -> dict:
    """Map a name to its expression when assigned exactly once in this scope."""
    counts = collections.Counter()
    exprs = {}
    for node in ast.walk(scope):
        pairs = []
        if isinstance(node, ast.Assign):
            pairs = [(t, node.value) for t in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs = [(node.target, node.value)]
        for target, value in pairs:
            if isinstance(target, ast.Name):
                counts[target.id] += 1
                exprs[target.id] = value
    return {name: exprs[name] for name in exprs if counts[name] == 1}


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _new(pattern=None, resolution="unresolved", reason="", alternatives=(), cli_tainted=False):
    return {
        "pattern": pattern,
        "resolution": resolution,
        "reason": reason,
        "alternatives": list(alternatives),
        "cli_tainted": cli_tainted,
    }


def _resolve_name(node: ast.AST, scope_locals: dict, depth: int = 0) -> dict:
    """Resolve a filename expression to a pattern with wildcard segments.

    ``cli_tainted`` is set when any part of the name traces back to the
    ``--output`` command-line argument.  A tainted name is never guessed: the
    caller must report it UNVERIFIABLE, because the actual value only exists in
    the invoking command line, not in the bound source.
    """
    if depth > 8:
        return _new(reason="resolution_depth_exceeded")

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return _new(pattern=node.value, resolution="resolved")

    if isinstance(node, ast.JoinedStr):
        parts = []
        dynamic = False
        tainted = False
        reasons = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
                continue
            if not isinstance(value, ast.FormattedValue):
                parts.append(DYNAMIC)
                dynamic = True
                reasons.append("fstring_unknown_segment:" + type(value).__name__)
                continue
            # The interpolated expression lives in ``value.value``; resolving the
            # FormattedValue node itself would fall through to the unsupported
            # branch and silently drop a command-line origin such as
            # ``f"{args.output}.json"``.
            inner = _resolve_name(value.value, scope_locals, depth + 1)
            tainted = tainted or inner["cli_tainted"]
            if inner["reason"]:
                reasons.append(inner["reason"])
            if inner["alternatives"]:
                for item in inner["alternatives"]:
                    if item not in reasons:
                        reasons.append("alternative:" + item)
            if value.conversion not in (-1, None):
                # ``!r`` / ``!s`` / ``!a`` change the rendered text, so even a
                # statically known value cannot be embedded verbatim.
                dynamic = True
                reasons.append("fstring_conversion:" + str(value.conversion))
            spec = value.format_spec
            if spec is not None:
                inner_spec = _resolve_name(spec, scope_locals, depth + 1)
                tainted = tainted or inner_spec["cli_tainted"]
                dynamic = True
                reasons.append("fstring_format_spec:" + (inner_spec["reason"] or "present"))
            embeddable = (
                inner["pattern"] is not None
                and inner["resolution"] in {"resolved", "partial_dynamic"}
                and value.conversion in (-1, None)
                and spec is None
            )
            if embeddable:
                parts.append(inner["pattern"])
            else:
                parts.append(DYNAMIC)
                dynamic = True
        return _new(
            pattern="".join(parts),
            resolution="partial_dynamic" if dynamic else "resolved",
            reason=";".join(dict.fromkeys(reasons)),
            cli_tainted=tainted,
        )

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _resolve_name(node.left, scope_locals, depth + 1)
        right = _resolve_name(node.right, scope_locals, depth + 1)
        parts = []
        dynamic = False
        for side in (left, right):
            if side["pattern"] is None or side["resolution"] == "partial_dynamic":
                dynamic = True
            parts.append(side["pattern"] if side["pattern"] is not None else DYNAMIC)
        return _new(
            pattern="".join(parts),
            resolution="partial_dynamic" if dynamic else "resolved",
            reason="concat_dynamic_segment" if dynamic else "",
            cli_tainted=left["cli_tainted"] or right["cli_tainted"],
        )

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        # Only the right-hand side carries the filename; the left is a base dir.
        return _resolve_name(node.right, scope_locals, depth + 1)

    if isinstance(node, ast.Name):
        if node.id in scope_locals:
            return _resolve_name(scope_locals[node.id], scope_locals, depth + 1)
        return _new(reason="name_not_single_assigned:" + node.id)

    if isinstance(node, ast.Call):
        name = _call_name(node)
        if name in {"safe_path", "Path", "str"} and node.args:
            return _resolve_name(node.args[0], scope_locals, depth + 1)
        if name in {"resolve", "expanduser", "absolute"} and not node.args:
            return _resolve_name(node.func.value, scope_locals, depth + 1)
        return _new(reason="call_not_unwrappable:" + name)

    if isinstance(node, ast.Attribute):
        # argparse namespace access: the value exists only on the command line.
        if isinstance(node.value, ast.Name) and node.value.id in {"args", "options", "parsed"}:
            return _new(
                reason="cli_argument:" + node.attr,
                cli_tainted=True,
                alternatives=["--" + node.attr.replace("_", "-")],
            )
        return _new(reason="attribute_base:" + ast.unparse(node))

    if isinstance(node, ast.IfExp):
        taken = _resolve_name(node.body, scope_locals, depth + 1)
        other = _resolve_name(node.orelse, scope_locals, depth + 1)
        alternatives = [b["pattern"] for b in (taken, other) if b["pattern"] is not None]
        tainted = taken["cli_tainted"] or other["cli_tainted"]
        reasons = [b["reason"] for b in (taken, other) if b["reason"]]
        reasons.append("conditional_expression")
        # Both branches must be statically known for this to be a complete
        # contract.  When one branch is a runtime call the written name is not
        # determined by the source, and the known branch must not stand in for
        # the whole contract: reporting its absence would claim more than the
        # evidence supports.
        complete = all(
            b["resolution"] in {"resolved", "partial_dynamic"} and b["pattern"] is not None
            for b in (taken, other)
        )
        return _new(
            pattern="|".join(alternatives) if complete else None,
            resolution="conditional_complete" if complete else "conditional",
            reason=";".join(dict.fromkeys(reasons)),
            alternatives=alternatives,
            cli_tainted=tainted,
        )

    return _new(reason="unsupported_expression:" + type(node).__name__)


def _base_of(node: ast.AST) -> str:
    """Return the unparsed base-directory expression of a path join."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return ast.unparse(node.left)
    return ""


def extract_contracts(source_root: str, *, include_non_product: bool = False) -> list:
    """Derive every receipt write contract from the bound source."""
    if not os.path.isdir(source_root):
        raise ContractExtractionError("source_root_not_a_directory:" + source_root)
    contracts = []
    for path in _iter_python_files(source_root):
        rel = _relative(path, source_root)
        if not include_non_product and _is_non_product(rel):
            continue
        try:
            with open(path, encoding="utf-8-sig") as handle:
                tree = ast.parse(handle.read(), filename=rel)
        except (OSError, SyntaxError) as exc:
            contracts.append({
                "source": f"{rel}:0",
                "name_pattern": None,
                "resolution": "unresolved",
                "reason": "source_parse_failed:" + type(exc).__name__,
                "alternatives": [],
                "cli_tainted": False,
                "base_expr": "",
                "expected_base": "unknown",
                "base_evidence": "",
                "writer_func": "",
                "product_code": not _is_non_product(rel),
            })
            continue
        scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        scope_of = {}
        for scope in scopes:
            for child in ast.walk(scope):
                scope_of.setdefault(id(child), scope)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in WRITER_FUNCS:
                continue
            if not node.args:
                continue
            writer = _call_name(node)
            scope = scope_of.get(id(node))
            scope_locals = _single_assignments(scope) if scope is not None else {}
            arg = node.args[0]
            resolved = _resolve_name(arg, scope_locals)
            base_expr = _base_of(arg)
            semantic, evidence = BASE_SEMANTICS.get((rel, base_expr), ("unknown", ""))
            # The CLI taint discovered during resolution outranks the base
            # table: an --output path cannot be inferred from the source.
            if resolved["cli_tainted"]:
                semantic = "cli_supplied"
                evidence = (
                    f"{rel}:{node.lineno} output path derives from a command-line "
                    f"argument ({', '.join(resolved['alternatives']) or resolved['reason']}); "
                    "the bound source cannot determine the actual filename"
                )
            elif semantic == "unknown":
                attribute_key = (rel, ast.unparse(arg))
                semantic, evidence = BASE_SEMANTICS.get(attribute_key, (semantic, evidence))
            contracts.append({
                "source": f"{rel}:{node.lineno}",
                "name_pattern": resolved["pattern"],
                "resolution": resolved["resolution"],
                "reason": resolved["reason"],
                "alternatives": resolved["alternatives"],
                "cli_tainted": resolved["cli_tainted"],
                "base_expr": base_expr,
                "expected_base": semantic,
                "base_evidence": evidence,
                "writer_func": writer,
                "product_code": not _is_non_product(rel),
                "path_expression": ast.unparse(arg),
            })
    return contracts


def _pattern_to_regex(pattern: str) -> re.Pattern:
    escaped = re.escape(pattern).replace(re.escape(DYNAMIC), ".+")
    return re.compile("^" + escaped + "$")


def _literal_runs(pattern: str) -> list:
    """Literal fragments of a pattern, with dynamic segments as separators."""
    return [r for r in re.split(re.escape(DYNAMIC), pattern) if r]


def _is_discriminating(pattern: str) -> bool:
    """True when a pattern can tell the target apart from unrelated JSON files.

    A pattern whose only literal text is the ``.json`` extension (for example
    ``{*}.json``) matches every JSON file in the directory, so a match proves
    nothing about the receipt and a non-match cannot be localised.  Such a
    pattern is reported UNVERIFIABLE instead of being matched loosely.
    """
    runs = [r for r in _literal_runs(pattern) if r.lower() not in {".json", ".jsonl"}]
    return any(len(r) >= 4 for r in runs)


def _exact_probe(scope_path: str, expected_rel: str) -> bool:
    """Directly probe one exact path; a hit is positive evidence on its own."""
    parts = [p for p in expected_rel.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    candidate = os.path.join(scope_path, *parts)
    try:
        return os.path.isfile(candidate)
    except OSError:
        return False


SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def list_scope(directory: str, *, recursive: bool) -> dict:
    """Record exactly which directory was listed, and how completely.

    ``check_complete`` is true only when the directory existed, no error was
    raised, no listing cap was hit, and (for a recursive walk) no subdirectory
    failed to enumerate.  An incomplete check can never support ABSENT.
    """
    record = {
        "path": os.path.abspath(directory),
        "recursive": recursive,
        "exists": os.path.isdir(directory),
        "file_count": None,
        "names": [],
        "listing_truncated": False,
        "walk_errors": [],
        "excluded_directories": sorted(SKIP_DIRS),
        "error": "",
        "check_complete": False,
    }
    if not record["exists"]:
        record["error"] = "directory_not_found"
        return record
    names = []
    truncated = False

    def _on_walk_error(exc: OSError) -> None:
        record["walk_errors"].append(
            {"filename": getattr(exc, "filename", ""), "error": type(exc).__name__}
        )

    try:
        if recursive:
            for dirpath, dirnames, filenames in os.walk(directory, onerror=_on_walk_error):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
                for name in sorted(filenames):
                    names.append(_relative(os.path.join(dirpath, name), directory))
                    if len(names) >= MAX_LISTING_PER_DIR:
                        truncated = True
                        break
                if truncated:
                    break
        else:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False):
                        names.append(entry.name)
                        if len(names) >= MAX_LISTING_PER_DIR:
                            truncated = True
                            break
    except OSError as exc:
        record["error"] = "listing_failed:" + type(exc).__name__
        record["listing_truncated"] = truncated
        record["names"] = sorted(names)
        record["file_count"] = len(names)
        return record
    record["listing_truncated"] = truncated
    record["file_count"] = len(names)
    record["names"] = sorted(names)
    record["check_complete"] = not truncated and not record["walk_errors"]
    return record


def _incompleteness_reasons(scope: dict) -> str:
    """Why this scope cannot support a negative claim, or '' when it can."""
    reasons = []
    if scope.get("error"):
        reasons.append("scope_unreadable:" + scope["error"])
    if scope.get("listing_truncated"):
        reasons.append("listing_truncated_at:" + str(MAX_LISTING_PER_DIR))
    for item in scope.get("walk_errors") or []:
        reasons.append("walk_error:" + item["error"])
    if not reasons and not scope.get("check_complete"):
        reasons.append("check_not_complete")
    return ";".join(reasons)


def _match_in_scope(scope: dict, expected_rel: str) -> list:
    """Match by full relative path within the base, never by basename alone.

    R-05: the previous basename fallback contradicted this docstring.  A target
    carrying a directory prefix (``right/observer-exit.json``) must never be
    satisfied by a same-named file elsewhere (``left/observer-exit.json``), so
    only the full relative path is matched.  The unused ``pattern`` argument was
    dropped together with that fallback.
    """
    regex = _pattern_to_regex(expected_rel.replace("\\", "/"))
    return [n for n in scope["names"] if regex.match(n.replace("\\", "/"))]


def _requested_scope(name: str, scopes: dict, explicit_base: str):
    """Resolve which checked scope a requested name refers to.

    A directory written inside the requested name is honoured, because it is
    part of the request rather than an inference by this tool.  A bare name
    with no explicit base stays unresolved: picking a directory for it would be
    a guess, and guessing is exactly what turns "cannot verify" into a false
    "absent".
    """
    normalised = name.replace("\\", "/")
    if explicit_base:
        if explicit_base in scopes:
            return explicit_base, scopes[explicit_base], ""
        return "", None, "explicit_base_not_in_checked_scope:" + explicit_base
    directory = os.path.dirname(normalised)
    if directory:
        tail = directory.split("/")[-1]
        if tail in scopes:
            return tail, scopes[tail], ""
        return "", None, "name_directory_not_in_checked_scope:" + tail
    return "", None, "expected_base_not_determined_for_bare_name"


def _requested_relative(name: str, explicit_base: str, label: str) -> str:
    """Path of a requested name relative to the scope base it resolved to.

    R-05: a requested name may carry its own directory prefix
    (``child/observer-exit.json``).  That prefix is part of the request and must
    be preserved when comparing against writer contracts; dropping it and
    matching on basename alone is what let a root-level file stand in for a
    child-directory target.  The base label itself is stripped because the scope
    path already denotes it.
    """
    normalised = name.replace("\\", "/").lstrip("/")
    for prefix in (explicit_base, label):
        if not prefix:
            continue
        head = prefix.rstrip("/") + "/"
        if normalised.startswith(head):
            return normalised[len(head):]
    return normalised


def _observed_basename(basename: str, scopes: dict) -> list:
    """Report where a basename actually appears, without claiming a contract."""
    found = []
    for label, scope in scopes.items():
        if scope.get("error") or not scope.get("names"):
            continue
        hits = [n for n in scope["names"] if os.path.basename(n) == basename]
        if hits:
            found.append({"scope": label, "files": sorted(hits)[:64]})
    return found


def _expected_relative(pattern: str) -> str:
    """Path of the contract target inside its base directory.

    The derived pattern is the filename expression of ``base / name``, so the
    target sits at the base root.  Should a pattern ever carry a directory
    prefix, that prefix is honoured rather than dropped: matching on basename
    alone is what lets ``child/observer-exit.json`` stand in for a root-level
    contract.
    """
    return pattern.replace("\\", "/").lstrip("/")


def audit(*, contracts: list, scopes: dict, requested_names: list) -> dict:
    """Classify each derived pattern and each externally requested name."""
    results = []
    seen = set()
    for contract in contracts:
        if not contract["product_code"]:
            continue
        pattern = contract["name_pattern"]
        base = contract["expected_base"]
        resolution = contract["resolution"]
        scope = scopes.get(base)
        matched = []
        exact_hit = False
        incomplete = ""

        # Precedence matters.  A CLI-supplied destination is unknowable from
        # source alone, so it is UNVERIFIABLE even when a same-named file
        # happens to sit in a listed directory.
        if base == "cli_supplied" or contract.get("cli_tainted"):
            status, reason = "UNVERIFIABLE", "output_path_cli_supplied"
        elif pattern is None:
            status, reason = "UNVERIFIABLE", contract["reason"] or "name_pattern_unresolved"
        elif resolution not in {"resolved", "partial_dynamic", "conditional_complete"}:
            # An unresolved conditional, a runtime call, an alias or any other
            # expression that does not yield a definite name family.
            status, reason = "UNVERIFIABLE", contract["reason"] or (
                "name_not_statically_determined:" + resolution
            )
        elif "|" in pattern:
            # Several statically known alternatives: the source does not say
            # which one was written, so no single target can be checked.
            status, reason = "UNVERIFIABLE", "multiple_candidate_names:" + contract["reason"]
        elif not _is_discriminating(pattern):
            status, reason = "UNVERIFIABLE", "pattern_not_discriminating:" + pattern
        elif scope is None:
            status, reason = "UNVERIFIABLE", "expected_base_not_in_checked_scope:" + base
        else:
            expected_rel = _expected_relative(pattern)
            exact_hit = _exact_probe(scope["path"], expected_rel)
            incomplete = _incompleteness_reasons(scope)
            matched = _match_in_scope(scope, expected_rel)
            if exact_hit and expected_rel not in matched:
                # The probe hit is the evidence for EXISTS, so it must appear in
                # the report even when the listing did not carry it.
                matched = sorted(matched + [expected_rel])
            if exact_hit or matched:
                # A directly probed path is positive evidence on its own, so it
                # stands even when some other region of the scope was truncated
                # or failed to enumerate.
                status, reason = "EXISTS", ""
            elif incomplete:
                status, reason = "UNVERIFIABLE", incomplete
            else:
                status, reason = "ABSENT_IN_CHECKED_SCOPE", ""

        key = (contract["source"], pattern)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "name_pattern": pattern,
            "status": status,
            "reason": reason,
            "contract_source": contract["source"],
            "writer_func": contract["writer_func"],
            "expected_base": base,
            "expected_relative_path": _expected_relative(pattern) if pattern else "",
            "exact_path_probed": exact_hit,
            "base_expr": contract["base_expr"],
            "base_location_basis": (
                "asserted_with_source_reference" if contract["base_evidence"] else "unknown"
            ),
            "base_evidence": contract["base_evidence"],
            "resolution": resolution,
            "cli_tainted": contract.get("cli_tainted", False),
            "alternatives": contract["alternatives"],
            "path_expression": contract.get("path_expression", ""),
            "matched_files": sorted(matched)[:64],
            "matched_count": len(matched),
            "scope_check_complete": bool(
                scope and scope.get("check_complete") and not scope.get("error")
            ) if scope is not None else None,
            "scope_incompleteness": incomplete,
            "non_implication": NON_IMPLICATION if status == "ABSENT_IN_CHECKED_SCOPE" else "",
        })

    # Index writers by basename, but keep each one's base and pattern so a
    # requested name can be checked against the writer it actually claims.
    # Storing only the basename is what let a request point at one base while
    # borrowing a contract written at another.
    writer_names = collections.defaultdict(list)
    for contract in contracts:
        if not contract["product_code"] or not contract["name_pattern"]:
            continue
        if contract.get("cli_tainted") or contract["expected_base"] == "cli_supplied":
            continue
        writer_names[os.path.basename(contract["name_pattern"])].append({
            "source": contract["source"],
            "expected_base": contract["expected_base"],
            "name_pattern": contract["name_pattern"],
        })

    requested = []
    for entry in requested_names:
        name = entry["name"] if isinstance(entry, dict) else entry
        origin = entry.get("origin", "") if isinstance(entry, dict) else ""
        explicit_base = entry.get("expected_base", "") if isinstance(entry, dict) else ""
        normalised = name.replace("\\", "/")
        base_name = os.path.basename(normalised)
        sources = writer_names.get(base_name, [])
        record = {
            "requested_name": name,
            "origin": origin,
            "contract_source": "; ".join(w["source"] for w in sources),
            "writer_bases": sorted({w["expected_base"] for w in sources}),
            "observed_in_scopes": _observed_basename(base_name, scopes),
        }
        if not sources:
            # No bound-source writer produces this literal name, so nothing can
            # be concluded from looking for it.  The observation list above
            # still records whether such a file happens to exist.
            record.update(
                status="UNVERIFIABLE",
                reason="writer_contract_not_found_in_bound_source",
                note=(
                    "No bound-source writer produces this literal name. Checking "
                    "for it cannot support any conclusion about whether a receipt "
                    "was written; see observed_in_scopes for what the listing did "
                    "or did not contain."
                ),
                non_implication="",
            )
            requested.append(record)
            continue

        label, scope, why = _requested_scope(name, scopes, explicit_base)
        if scope is None:
            record.update(status="UNVERIFIABLE", reason=why, note="", non_implication="")
            requested.append(record)
            continue

        # R-05: compare against the requested name's OWN full relative path.
        # Taking sources[0] dropped that prefix, so a root-level file could
        # stand in for a child-directory target, and a request for right/...
        # borrowed the left/... contract.  A requested path that no writer
        # contract can produce is unverifiable here; such a file is only
        # reported under observed_in_scopes.
        requested_rel = _requested_relative(name, explicit_base, label)
        matching = [w for w in sources
                    if _pattern_to_regex(_expected_relative(w["name_pattern"])).match(requested_rel)]
        if not matching:
            record.update(
                status="UNVERIFIABLE",
                reason="no_writer_contract_matches_requested_path:" + requested_rel,
                resolved_scope=label,
                requested_relative_path=requested_rel,
                note=(
                    "A bound-source writer produces this basename, but none of "
                    "their contract paths match the requested relative path. A "
                    "file at the requested path is therefore not this receipt, "
                    "and its absence supports no conclusion; see "
                    "observed_in_scopes for what the listing did contain."
                ),
                non_implication="",
            )
            requested.append(record)
            continue
        record["contract_source"] = "; ".join(sorted({w["source"] for w in matching}))
        record["writer_bases"] = sorted({w["expected_base"] for w in matching})

        # The requested base must agree with where the matching writer writes.
        # Several writers with different bases, or a request naming a base that
        # no writer uses, is a conflict: neither can be resolved to one target.
        writer_bases = {w["expected_base"] for w in matching}
        conflicting = [w for w in matching if w["expected_base"] != label]
        if len(writer_bases) > 1:
            record.update(
                status="UNVERIFIABLE",
                reason="writer_bases_ambiguous:" + ",".join(sorted(writer_bases)),
                resolved_scope=label,
                requested_relative_path=requested_rel,
                note=(
                    "This name maps to several writers with different bases; "
                    "they cannot be merged into one unambiguous contract."
                ),
                non_implication="",
            )
            requested.append(record)
            continue
        if conflicting:
            record.update(
                status="UNVERIFIABLE",
                reason="requested_base_conflicts_with_writer_base:"
                       + ",".join(sorted({w["expected_base"] for w in conflicting}))
                       + "!=" + label,
                resolved_scope=label,
                requested_relative_path=requested_rel,
                note=(
                    "The request names base '" + label + "' but the bound-source "
                    "writer for this name writes to "
                    + ",".join(sorted({w['expected_base'] for w in conflicting}))
                    + ". A name found or missing under the requested base is not "
                    "this receipt; see observed_in_scopes."
                ),
                non_implication="",
            )
            requested.append(record)
            continue

        # The target is the requested path itself, matched against every writer
        # contract that can produce it (never just the first one).
        expected_rel = requested_rel
        exact_hit = _exact_probe(scope["path"], expected_rel)
        incomplete = _incompleteness_reasons(scope)
        matched = sorted({n for w in matching
                          for n in _match_in_scope(scope, _expected_relative(w["name_pattern"]))})
        matched = [n for n in matched if n == expected_rel or
                   _pattern_to_regex(expected_rel).match(n)]
        if exact_hit and expected_rel not in matched:
            matched = sorted(matched + [expected_rel])
        if exact_hit or matched:
            status, reason = "EXISTS", ""
        elif incomplete:
            status, reason = "UNVERIFIABLE", incomplete
        else:
            status, reason = "ABSENT_IN_CHECKED_SCOPE", ""
        record.update(
            status=status,
            reason=reason,
            resolved_scope=label,
            expected_relative_path=expected_rel,
            requested_relative_path=requested_rel,
            exact_path_probed=exact_hit,
            scope_check_complete=bool(scope.get("check_complete") and not scope.get("error")),
            scope_incompleteness=incomplete,
            matched_files=sorted(matched)[:64],
            note="",
            non_implication="" if status != "ABSENT_IN_CHECKED_SCOPE" else NON_IMPLICATION,
        )
        requested.append(record)

    return {"derived_contracts": results, "requested_names": requested}


def build_report(*, source_root, scopes, requested_names, candidate_sha, notes) -> dict:
    contracts = extract_contracts(source_root)
    scope_records = {}
    for label, spec in scopes.items():
        scope_records[label] = list_scope(spec["path"], recursive=spec.get("recursive", False))
    findings = audit(contracts=contracts, scopes=scope_records, requested_names=requested_names)
    counts = collections.Counter(r["status"] for r in findings["derived_contracts"])
    return {
        "version": VERSION,
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_scope": {
            "root": os.path.abspath(source_root),
            "candidate_sha": candidate_sha,
            "candidate_sha_basis": "caller_supplied_not_independently_verified",
            "writer_functions": sorted(WRITER_FUNCS),
            "product_contracts": sum(1 for c in contracts if c["product_code"]),
            "non_product_contracts_excluded": sum(1 for c in contracts if not c["product_code"]),
        },
        "checked_scope": {
            "directories": [
                {k: v for k, v in rec.items() if k != "names"} | {"file_count": rec["file_count"]}
                for rec in scope_records.values()
            ],
            "names_retained_in_report": False,
            "note": (
                "Directory listings were used for matching only and are not "
                "reproduced here; only matched filenames are reported."
            ),
        },
        "status_vocabulary": {
            "EXISTS": (
                "the exact expected path under the contract's own base was "
                "probed and found, or a file matched the derived pattern at "
                "that base's relative path"
            ),
            "ABSENT_IN_CHECKED_SCOPE": (
                "the pattern was fully resolved, its base was readable, the "
                "check over that base completed, and nothing matched"
            ),
            "UNVERIFIABLE": (
                "no definite claim is possible: the name could not be resolved "
                "statically, several names or bases remain possible, no "
                "bound-source writer produces it, its base was not in the "
                "checked scope, the base was unreadable, or the check over that "
                "base did not complete (truncation or walk error)"
            ),
        },
        "status_counts": dict(counts),
        "non_implication": NON_IMPLICATION,
        **findings,
        "limitations": [
            "Static analysis only: a name reachable through runtime-only "
            "computation (CLI arguments, configuration files, database rows) is "
            "reported UNVERIFIABLE rather than guessed.",
            "Base-directory semantics come from the asserted BASE_SEMANTICS "
            "table; each entry cites its evidence and is labelled "
            "base_location_basis so a reviewer can reject it.",
            "This tool never lists a directory that was not explicitly passed "
            "to it, and never reads file contents.",
            "ABSENT_IN_CHECKED_SCOPE is not evidence that a writer did not "
            "execute; see non_implication.",
            "An incomplete check is never reported as absence: a truncated "
            "listing, an enumeration error or a missing base directory yields "
            "UNVERIFIABLE, with the reason recorded in scope_incompleteness.",
            "Recursive mode reports what the walk reached. A walk that skipped "
            "subdirectories on error records them in walk_errors and the scope "
            "is not marked complete; this tool never claims a whole tree was "
            "fully checked when it was not.",
            "Directories excluded from the walk are listed per scope "
            "(excluded_directories); files inside them are invisible to this "
            "tool and their absence proves nothing.",
            "Matching keeps the base and the full relative path. A same-named "
            "file in another directory or scope never satisfies a contract; it "
            "is only reported under observed_in_scopes, which is separate from "
            "the contract verdict.",
            "A directly probed exact path is positive evidence on its own, so "
            "EXISTS can be reported even when another region of the same scope "
            "was truncated or failed to enumerate.",
            "A pattern with no discriminating literal stem (for example "
            "'{*}.json') matches unrelated files and is reported UNVERIFIABLE "
            "rather than loosely matched.",
        ] + list(notes or []),
    }


def _parse_scope(value: str) -> tuple:
    if "=" not in value:
        raise argparse.ArgumentTypeError("scope must be label=path[:recursive]")
    label, _, spec = value.partition("=")
    recursive = spec.endswith(":recursive")
    path = spec[: -len(":recursive")] if recursive else spec
    if not label or not path:
        raise argparse.ArgumentTypeError("scope must be label=path[:recursive]")
    return label, {"path": path, "recursive": recursive}


def _parse_requested(value: str) -> dict:
    parts = value.split("|")
    entry = {"name": parts[0]}
    for extra in parts[1:]:
        key, _, val = extra.partition("=")
        if key in {"origin", "expected_base"}:
            entry[key] = val
    return entry


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", required=True, help="Bound source root to extract contracts from")
    parser.add_argument("--candidate-sha", default="UNKNOWN", help="Recorded as caller-supplied")
    parser.add_argument(
        "--scope", action="append", type=_parse_scope, default=[], metavar="LABEL=PATH[:recursive]",
        help="Semantic label must match an expected_base (trial_root, monitoring, ...)",
    )
    parser.add_argument(
        "--requested-name", action="append", type=_parse_requested, default=[], metavar="NAME[|origin=X][|expected_base=Y]",
        help="A name from an external checklist; reported UNVERIFIABLE without a bound-source writer",
    )
    parser.add_argument("--json-out", default="", help="New unique path; an existing file is never overwritten")
    parser.add_argument("--note", action="append", default=[], help="Extra limitation text")
    args = parser.parse_args(argv)

    scopes = dict(args.scope)
    report = build_report(
        source_root=args.source,
        scopes=scopes,
        requested_names=args.requested_name,
        candidate_sha=args.candidate_sha,
        notes=args.note,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.json_out:
        out = os.path.abspath(args.json_out)
        if os.path.exists(out):
            raise SystemExit("json_out_already_exists: refusing to overwrite " + out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        data = payload.encode("utf-8")
        with open(out, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        report["json_out"] = out
        report["json_out_sha256"] = hashlib.sha256(data).hexdigest()

    counts = report["status_counts"]
    summary = {
        "version": VERSION,
        "status_counts": counts,
        "derived_contracts": len(report["derived_contracts"]),
        "requested_names": len(report["requested_names"]),
        "unverifiable_requested": sum(
            1 for r in report["requested_names"] if r["status"] == "UNVERIFIABLE"
        ),
        "json_out": report.get("json_out", ""),
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
