"""Emit one bounded JSON-only official-source live preflight report."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Sequence, TextIO


PREFLIGHT_VERSION = "official_source_live_preflight_v1"
PREFLIGHT_CONFIRMATION = "RUN_OFFICIAL_SOURCE_LIVE_PREFLIGHT_ONCE"
MAX_OUTPUT_BYTES = 16_384


class _ArgumentsInvalid(ValueError):
    pass


class _BoundedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _ArgumentsInvalid("invalid live preflight arguments")


def _zero_safety(
    *,
    confirmation_verified: bool,
    indeterminate: bool = False,
) -> dict[str, Any]:
    return {
        "read_only": None if indeterminate else True,
        "one_shot": None if indeterminate else True,
        "confirmation_required": True,
        "confirmation_verified": confirmation_verified,
        "network_requests_performed": None if indeterminate else 0,
        "network_requests_accounting": (
            "unknown" if indeterminate else "exact_zero"
        ),
        "endpoint_fetch_attempts_performed": (
            None if indeterminate else 0
        ),
        "endpoint_fetch_attempts_accounting": (
            "unknown" if indeterminate else "exact"
        ),
        "endpoint_fetch_attempt_limit": 8,
        "retries_performed": None if indeterminate else 0,
        "endpoint_allowlist_enforced": None if indeterminate else True,
        "transport_mode": "unknown" if indeterminate else "not_started",
        "live_network_attested": None if indeterminate else False,
        "in_process_tamper_resistant": None if indeterminate else False,
        "proxy_configuration_overridden": None if indeterminate else False,
        "tls_verification_disabled": None if indeterminate else False,
        "application_file_writes_performed": None if indeterminate else 0,
        "database_reads_performed": None if indeterminate else 0,
        "database_writes_performed": None if indeterminate else 0,
        "checkpoint_writes_performed": None if indeterminate else 0,
        "source_inbox_writes_performed": None if indeterminate else 0,
        "provider_calls_performed": None if indeterminate else 0,
        "model_calls_performed": None if indeterminate else 0,
        "futu_calls_performed": None if indeterminate else 0,
        "formal_rounds_created": None if indeterminate else 0,
        "execution_capability": "unknown" if indeterminate else "none",
        "live_trading_allowed": None if indeterminate else False,
        "http_listener_started": None if indeterminate else False,
    }


def _error_payload(
    code: str,
    *,
    confirmation_verified: bool = False,
    indeterminate: bool = False,
    category: str | None = None,
) -> dict[str, Any]:
    return {
        "version": PREFLIGHT_VERSION,
        "scope": "official_macro_only",
        "sec_included": False,
        "company_ir_included": False,
        "ok": False,
        "status": "indeterminate" if indeterminate else "not_started",
        "error_code": code,
        "error_category": (
            category
            if category is not None
            else "input" if not confirmation_verified else "internal"
        ),
        "safety": _zero_safety(
            confirmation_verified=confirmation_verified,
            indeterminate=indeterminate,
        ),
    }


def _help_payload() -> dict[str, Any]:
    return {
        "version": PREFLIGHT_VERSION,
        "scope": "official_macro_only",
        "sec_included": False,
        "company_ir_included": False,
        "ok": True,
        "status": "help",
        "required_confirmation": PREFLIGHT_CONFIRMATION,
        "safety": _zero_safety(confirmation_verified=False),
    }


def _parser() -> argparse.ArgumentParser:
    parser = _BoundedArgumentParser(
        prog="python scripts/run_official_source_live_preflight.py",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("--confirm", default=None)
    return parser


def _parse(argv: Sequence[str]) -> tuple[str, str]:
    raw = list(argv)
    if any(type(value) is not str for value in raw):
        raise _ArgumentsInvalid("arguments must be native strings")
    help_count = sum(value in {"-h", "--help"} for value in raw)
    confirm_count = sum(
        value == "--confirm" or value.startswith("--confirm=")
        for value in raw
    )
    if help_count:
        if len(raw) == 1 and help_count == 1:
            return "help", ""
        raise _ArgumentsInvalid("help must be the only argument")
    if confirm_count > 1:
        raise _ArgumentsInvalid("confirmation must be supplied once")
    parsed = _parser().parse_args(raw)
    confirmation = parsed.confirm if type(parsed.confirm) is str else ""
    return "run", confirmation


def _bounded_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    if len(encoded.encode("ascii")) > MAX_OUTPUT_BYTES:
        fallback = _error_payload(
            "PREFLIGHT_OUTPUT_BOUND_EXCEEDED",
            confirmation_verified=True,
            indeterminate=True,
        )
        return json.dumps(
            fallback,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
    return encoded


def _emit(output: TextIO, payload: dict[str, Any]) -> None:
    output.write(_bounded_json(payload))


def _backend_modules_preloaded() -> bool:
    return any(
        name == "backend" or name.startswith("backend.")
        for name in tuple(sys.modules)
    )


def _production_runner(confirmation: str) -> dict[str, Any]:
    # This import intentionally happens only after exact confirmation passes.
    from pathlib import Path

    if sys.flags.isolated != 1:
        raise RuntimeError("isolated Python process is required")
    if _backend_modules_preloaded():
        raise RuntimeError("backend modules must not be preloaded")

    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        project_root = str(Path(__file__).resolve().parents[1])
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from backend.source_monitoring.live_preflight import (
            run_official_source_live_preflight,
        )

        return run_official_source_live_preflight(confirmation=confirmation)
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting


def _validated_report(value: Any) -> dict[str, Any]:
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        from backend.source_monitoring.live_preflight import (
            validate_live_preflight_report,
        )

        return validate_live_preflight_report(value)
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting


def _run_cli(
    argv: Sequence[str] | None = None,
    *,
    output: TextIO | None = None,
    runner: Callable[[str], dict[str, Any]],
    production_runner: bool,
    require_isolated_process: bool,
) -> int:
    """Shared parser/output path; only the public wrapper may use live I/O."""

    stream = sys.stdout if output is None else output
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        mode, confirmation = _parse(raw)
    except (argparse.ArgumentError, _ArgumentsInvalid):
        _emit(stream, _error_payload("PREFLIGHT_ARGUMENTS_INVALID"))
        return 2
    if mode == "help":
        _emit(stream, _help_payload())
        return 0
    if confirmation != PREFLIGHT_CONFIRMATION:
        _emit(stream, _error_payload("PREFLIGHT_CONFIRMATION_REQUIRED"))
        return 2
    if require_isolated_process and sys.flags.isolated != 1:
        _emit(
            stream,
            _error_payload(
                "PREFLIGHT_ISOLATED_PROCESS_REQUIRED",
                confirmation_verified=True,
                category="environment",
            ),
        )
        return 2
    if production_runner and _backend_modules_preloaded():
        _emit(
            stream,
            _error_payload(
                "PREFLIGHT_BACKEND_PRELOADED",
                confirmation_verified=True,
                indeterminate=True,
                category="environment",
            ),
        )
        return 2

    try:
        result = runner(confirmation)
        report = _validated_report(result)
        if (
            production_runner is not True
            or report["evidence_class"] != "production_path_observation"
            or report["safety"]["transport_mode"]
            != "default_official_https_path"
        ):
            _emit(
                stream,
                _error_payload(
                    "PREFLIGHT_PRODUCTION_REQUIRED",
                    confirmation_verified=True,
                    indeterminate=True,
                ),
            )
            return 2
    except BaseException:
        _emit(
            stream,
            _error_payload(
                "PREFLIGHT_INTERNAL_ERROR",
                confirmation_verified=True,
                indeterminate=True,
            ),
        )
        return 1
    _emit(stream, report)
    return 0 if report["ok"] else 2


def _build_public_main(
    production_runner_token: Callable[[str], dict[str, Any]],
) -> Callable[..., int]:
    """Bind the production runner outside the mutable module lookup."""

    def public_main(
        argv: Sequence[str] | None = None,
        *,
        output: TextIO | None = None,
    ) -> int:
        return _run_cli(
            argv,
            output=output,
            runner=production_runner_token,
            production_runner=True,
            require_isolated_process=True,
        )

    public_main.__name__ = "main"
    public_main.__qualname__ = "main"
    public_main.__doc__ = (
        "Parse and gate an isolated process before resolving live dependencies."
    )
    return public_main


main = _build_public_main(_production_runner)


def _main_injected(
    argv: Sequence[str] | None = None,
    *,
    output: TextIO | None = None,
    runner: Callable[[str], dict[str, Any]],
) -> int:
    """Test-only runner path that can never emit a live success receipt."""

    return _run_cli(
        argv,
        output=output,
        runner=runner,
        production_runner=False,
        require_isolated_process=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_OUTPUT_BYTES",
    "PREFLIGHT_CONFIRMATION",
    "PREFLIGHT_VERSION",
    "main",
]
