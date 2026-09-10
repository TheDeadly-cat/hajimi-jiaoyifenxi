"""Read-only CLI for official-source operational soak acceptance.

The command consumes only one sealed four-file soak bundle.  It never discovers
or opens a database and cannot start a runtime, source request, Provider call,
market call, formal round, or execution path.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Sequence, TextIO


sys.dont_write_bytecode = True

SOURCE_MONITORING_ACCEPTANCE_CLI_VERSION = "source_monitoring_acceptance_cli_v1"
MAX_SOURCE_MONITORING_ACCEPTANCE_OUTPUT_BYTES = 64 * 1024
_ERROR_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,119}\Z")


class _CliFailure(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _CliFailure("SOURCE_MONITORING_ACCEPTANCE_ARGUMENT_INVALID")


def _safety() -> dict[str, object]:
    return {
        "database_discovery_performed": False,
        "database_reads_performed": 0,
        "database_writes_performed": 0,
        "network_requests_performed": 0,
        "provider_calls_performed": 0,
        "model_calls_performed": 0,
        "market_calls_performed": 0,
        "formal_rounds_created": 0,
        "execution_capability": "none",
        "live_trading_allowed": False,
    }


def _help_payload() -> dict[str, object]:
    return {
        "version": SOURCE_MONITORING_ACCEPTANCE_CLI_VERSION,
        "command": "help",
        "ok": True,
        "scope": "six_official_source_operational_acceptance_only",
        "command_usage": "verify --bundle <sealed-four-file-directory>",
        "content_truth_attested": False,
        "independent_network_witness": False,
        "overall_acceptance": "NOT_CLAIMED",
        "safety": _safety(),
    }


def _error_payload(code: str) -> dict[str, object]:
    clean = (
        code
        if type(code) is str and _ERROR_CODE_RE.fullmatch(code) is not None
        else "SOURCE_MONITORING_ACCEPTANCE_FAILED"
    )
    return {
        "version": SOURCE_MONITORING_ACCEPTANCE_CLI_VERSION,
        "command": "verify",
        "ok": False,
        "error_code": clean,
        "source_acceptance_verdict": "FAIL",
        "overall_acceptance": "NOT_CLAIMED",
        "safety": _safety(),
    }


def _encode(payload: dict[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError):
        encoded = json.dumps(
            _error_payload("SOURCE_MONITORING_ACCEPTANCE_OUTPUT_INVALID"),
            sort_keys=True,
            separators=(",", ":"),
        )
    if len(encoded.encode("utf-8")) > MAX_SOURCE_MONITORING_ACCEPTANCE_OUTPUT_BYTES:
        encoded = json.dumps(
            _error_payload("SOURCE_MONITORING_ACCEPTANCE_OUTPUT_LIMIT_EXCEEDED"),
            sort_keys=True,
            separators=(",", ":"),
        )
    return encoded


def _emit(output: TextIO, payload: dict[str, object]) -> None:
    output.write(_encode(payload))
    output.write("\n")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        add_help=False,
        allow_abbrev=False,
        prog="run_source_monitoring_acceptance.py",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser(
        "verify",
        add_help=False,
        allow_abbrev=False,
    )
    verify.add_argument("--bundle", required=True)
    return parser


def _reject_invalid_bundle_option_count(raw: Sequence[str]) -> None:
    if any(type(item) is not str for item in raw):
        raise _CliFailure("SOURCE_MONITORING_ACCEPTANCE_ARGUMENT_INVALID")
    bundle_option_count = sum(
        item == "--bundle" or item.startswith("--bundle=") for item in raw
    )
    if bundle_option_count != 1:
        raise _CliFailure("SOURCE_MONITORING_ACCEPTANCE_ARGUMENT_INVALID")


def main(
    argv: Sequence[str] | None = None,
    *,
    output: TextIO | None = None,
) -> int:
    """Run the closed public CLI without a dependency-injection surface."""

    stream = sys.stdout if output is None else output
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw in (["--help"], ["-h"], ["help"]):
        _emit(stream, _help_payload())
        return 0
    try:
        _reject_invalid_bundle_option_count(raw)
        arguments = _parser().parse_args(raw)
        if (
            arguments.command != "verify"
            or type(arguments.bundle) is not str
            or not arguments.bundle
            or arguments.bundle != arguments.bundle.strip()
        ):
            raise _CliFailure("SOURCE_MONITORING_ACCEPTANCE_ARGUMENT_INVALID")
        if sys.flags.isolated != 1:
            raise _CliFailure(
                "SOURCE_MONITORING_ACCEPTANCE_ISOLATED_PROCESS_REQUIRED"
            )
        from .source_monitoring.soak_acceptance import (
            verify_official_source_operational_acceptance,
        )

        verdict = verify_official_source_operational_acceptance(
            arguments.bundle
        )
        if type(verdict) is not dict:
            raise _CliFailure("SOURCE_MONITORING_ACCEPTANCE_OUTPUT_INVALID")
        _emit(stream, verdict)
        return 0 if verdict.get("source_acceptance_verdict") == "PASS" else 2
    except _CliFailure as exc:
        _emit(stream, _error_payload(exc.code))
        return 2
    except (OSError, RuntimeError, TypeError, ValueError):
        _emit(stream, _error_payload("SOURCE_MONITORING_ACCEPTANCE_FAILED"))
        return 2


__all__ = [
    "MAX_SOURCE_MONITORING_ACCEPTANCE_OUTPUT_BYTES",
    "SOURCE_MONITORING_ACCEPTANCE_CLI_VERSION",
    "main",
]
