"""Isolated CLI gate for the one-shot SEC/company-IR live preflight."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from typing import Any, TextIO


PREFLIGHT_VERSION = "sec_ir_live_preflight_v1"
PREFLIGHT_CONFIRMATION = "RUN_SEC_IR_LIVE_PREFLIGHT_ONCE"
MAX_OUTPUT_BYTES = 16_384
_IMPORT_GUARD_ENV = "AI_STUDIO_SEC_IR_PREFLIGHT_IMPORT_GUARD"
_IMPORT_GUARD_VALUE = "sec-ir-isolated-import-v1"
_UNRELATED_CONFIG_ENV = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "ARK_API_KEY",
    "ARK_BASE_URL",
    "ARK_MODEL",
    "GLM_API_KEY",
    "ZHIPUAI_API_KEY",
    "GLM_BASE_URL",
    "GLM_MODEL",
    "AI_STUDIO_DEFAULT_PROVIDER",
    "AI_STUDIO_DISABLED_PROVIDERS",
    "AI_STUDIO_MANUAL_CHATGPT_REVIEW_RATE_LABEL",
    "AI_STUDIO_MANUAL_CHATGPT_REVIEW_INPUT_USD_PER_MILLION",
    "AI_STUDIO_MANUAL_CHATGPT_REVIEW_OUTPUT_USD_PER_MILLION",
    "AI_STUDIO_PROJECT_CAPABILITY_SIGNING_SECRET",
    "AI_STUDIO_HOST",
    "AI_STUDIO_PORT",
    "FUTU_HOST",
    "FUTU_PORT",
    "FUTU_CACHE_TTL_SECONDS",
    "SEC_CACHE_TTL_SECONDS",
)


class _ArgumentsInvalid(ValueError):
    pass


class _BoundedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentsInvalid(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            raise _ArgumentsInvalid(message or "invalid arguments")


def _zero_safety(*, confirmation_verified: bool, indeterminate: bool = False) -> dict[str, Any]:
    unknown = None if indeterminate else 0
    return {
        "confirmation_required": True,
        "confirmation_verified": confirmation_verified,
        "network_requests_performed": unknown,
        "endpoint_fetch_attempts_performed": unknown,
        "application_file_writes_performed": unknown,
        "database_reads_performed": unknown,
        "database_writes_performed": unknown,
        "checkpoint_writes_performed": unknown,
        "source_inbox_writes_performed": unknown,
        "provider_calls_performed": unknown,
        "model_calls_performed": unknown,
        "futu_calls_performed": unknown,
        "formal_rounds_created": unknown,
        "execution_capability": "unknown" if indeterminate else "none",
        "live_trading_allowed": None if indeterminate else False,
        "live_network_attested": False,
        "source_truth_verified": False,
        "production_acceptance_verdict": "NOT_EVALUATED",
        "overall_acceptance": "NOT_CLAIMED",
    }


def _error_payload(
    code: str,
    *,
    confirmation_verified: bool = False,
    indeterminate: bool = False,
    category: str = "input",
) -> dict[str, Any]:
    return {
        "version": PREFLIGHT_VERSION,
        "scope": "sec_and_company_ir_only",
        "sec_included": True,
        "company_ir_included": True,
        "official_macro_included": False,
        "ok": False,
        "status": "indeterminate" if indeterminate else "not_started",
        "error_code": code,
        "error_category": category,
        "required_confirmation": PREFLIGHT_CONFIRMATION,
        "safety": _zero_safety(
            confirmation_verified=confirmation_verified,
            indeterminate=indeterminate,
        ),
    }


def _help_payload() -> dict[str, Any]:
    return {
        "version": PREFLIGHT_VERSION,
        "scope": "sec_and_company_ir_only",
        "sec_included": True,
        "company_ir_included": True,
        "official_macro_included": False,
        "ok": True,
        "status": "help",
        "required_confirmation": PREFLIGHT_CONFIRMATION,
        "safety": _zero_safety(confirmation_verified=False),
    }


def _parser() -> argparse.ArgumentParser:
    parser = _BoundedArgumentParser(
        prog="python -I scripts/run_sec_ir_live_preflight.py",
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
        value == "--confirm" or value.startswith("--confirm=") for value in raw
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
    if len(encoded.encode("ascii")) >= MAX_OUTPUT_BYTES:
        encoded = json.dumps(
            _error_payload(
                "PREFLIGHT_OUTPUT_BOUND_EXCEEDED",
                confirmation_verified=True,
                indeterminate=True,
                category="internal",
            ),
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
        name == "backend" or name.startswith("backend.") for name in tuple(sys.modules)
    )


def _production_runner(confirmation: str) -> dict[str, Any]:
    # Backend resolution happens only after argument, confirmation, isolation,
    # and preload gates have all passed.
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
        run_sec_ir_live_preflight = _load_backend_runner(project_root)
        return run_sec_ir_live_preflight(confirmation=confirmation)
    finally:
        sys.dont_write_bytecode = previous_bytecode_setting


def _load_backend_runner(project_root: str) -> Callable[..., dict[str, Any]]:
    """Import config-dependent adapters without creating their runtime paths."""

    from pathlib import Path

    root = str(Path(project_root).resolve())
    controlled_keys = (
        "AI_STUDIO_RUNTIME_DIR",
        "AI_STUDIO_DATABASE_PATH",
        "AI_STUDIO_SKIP_LOCAL_ENV",
        _IMPORT_GUARD_ENV,
    )
    previous = {
        key: os.environ.get(key)
        for key in (*controlled_keys, *_UNRELATED_CONFIG_ENV)
    }
    os.environ["AI_STUDIO_RUNTIME_DIR"] = root
    os.environ["AI_STUDIO_DATABASE_PATH"] = str(
        Path(root) / ".sec-ir-preflight-database-must-not-open.sqlite3"
    )
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    os.environ[_IMPORT_GUARD_ENV] = _IMPORT_GUARD_VALUE
    for key in _UNRELATED_CONFIG_ENV:
        os.environ.pop(key, None)
    try:
        from backend.source_monitoring.sec_ir_live_preflight import (
            run_sec_ir_live_preflight,
        )

        return run_sec_ir_live_preflight
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _validated_report(value: Any) -> dict[str, Any]:
    previous_bytecode_setting = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        from backend.source_monitoring.sec_ir_live_preflight import (
            validate_sec_ir_live_preflight_report,
        )

        return validate_sec_ir_live_preflight_report(value)
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
        report = _validated_report(runner(confirmation))
        if (
            production_runner is not True
            or report["evidence_class"] != "production_path_observation"
            or report["receipt_class"] != "production_path_observation_not_attestation"
            or report["safety"]["transport_mode"]
            != "guarded_default_sec_ir_https_path"
            or report["safety"]["isolated_cli_import_guard_attested"] is not True
        ):
            _emit(
                stream,
                _error_payload(
                    "PREFLIGHT_PRODUCTION_REQUIRED",
                    confirmation_verified=True,
                    indeterminate=True,
                    category="evidence",
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
                category="internal",
            ),
        )
        return 1
    _emit(stream, report)
    return 0 if report["ok"] else 2


def _build_public_main(
    production_runner_token: Callable[[str], dict[str, Any]],
) -> Callable[..., int]:
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
    return public_main


main = _build_public_main(_production_runner)


def _main_injected(
    argv: Sequence[str] | None = None,
    *,
    output: TextIO | None = None,
    runner: Callable[[str], dict[str, Any]],
) -> int:
    """Test seam that can never emit an injected report as production."""

    return _run_cli(
        argv,
        output=output,
        runner=runner,
        production_runner=False,
        require_isolated_process=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MAX_OUTPUT_BYTES", "PREFLIGHT_CONFIRMATION", "PREFLIGHT_VERSION", "main"]
