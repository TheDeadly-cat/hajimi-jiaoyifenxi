from __future__ import annotations

import ast
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.market import ir_releases as ir_module
from backend.market import sec_edgar as sec_module
from backend.market.ir_releases import IR_FEEDS, IR_MAX_RESPONSE_BYTES
from backend.market.sec_edgar import SEC_MONITOR_SYMBOLS, SEC_TICKERS_URL
from backend.source_monitoring import sec_ir_live_preflight as preflight
from backend.source_monitoring.sec_ir_live_preflight import (
    SEC_IR_LIVE_PREFLIGHT_CONFIRMATION,
    SEC_IR_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256,
    SEC_IR_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES,
    SecIrLivePreflightConfirmationError,
    SecIrLivePreflightDependencies,
    _run_sec_ir_live_preflight_injected,
    run_sec_ir_live_preflight,
    validate_sec_ir_live_preflight_report,
)
from scripts import run_sec_ir_live_preflight as cli


FIXED_NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
FIXED_NOW_MS = int(FIXED_NOW.timestamp() * 1_000)
FIXTURE_USER_AGENT = "AI Studio fixture preflight@example.invalid"


class SecFixtureFetcher:
    def __init__(self, *, symbol_count: int | None = None) -> None:
        self.symbols = (
            tuple(SEC_MONITOR_SYMBOLS[:symbol_count])
            if symbol_count is not None
            else tuple(SEC_MONITOR_SYMBOLS)
        )
        self.ciks = {
            symbol: f"{1_000_000 + index:010d}"
            for index, symbol in enumerate(self.symbols, start=1)
        }
        self.calls: list[tuple[str, str]] = []

    def __call__(self, endpoint: str, user_agent: str) -> dict:
        self.calls.append((endpoint, user_agent))
        if endpoint == SEC_TICKERS_URL:
            return {
                str(index): {
                    "cik_str": int(self.ciks[symbol]),
                    "ticker": symbol.removeprefix("US."),
                    "title": f"Fixture {symbol}",
                }
                for index, symbol in enumerate(self.symbols)
            }
        cik = endpoint.removesuffix(".json").rsplit("CIK", 1)[-1]
        return {
            "cik": cik,
            "name": f"Fixture issuer {cik}",
            "filings": {
                "recent": {
                    "accessionNumber": [f"{cik}-26-000001"],
                    "form": ["8-K"],
                    "filingDate": ["2026-09-02"],
                    "reportDate": ["2026-09-02"],
                    "acceptanceDateTime": ["2026-09-02T20:00:00Z"],
                    "primaryDocument": ["fixture.htm"],
                    "primaryDocDescription": ["Fixture current report"],
                    "items": ["2.02"],
                }
            },
        }


class IrFixtureFetcher:
    def __init__(self, *, oversized: bool = False) -> None:
        self.oversized = oversized
        self.calls: list[tuple[str, frozenset[str]]] = []

    def __call__(self, endpoint: str, allowed_hosts: set[str]) -> bytes:
        self.calls.append((endpoint, frozenset(allowed_hosts)))
        if self.oversized:
            return b"x" * (IR_MAX_RESPONSE_BYTES + 1)
        host = urlparse(endpoint).hostname
        symbol = next(
            symbol for symbol, config in IR_FEEDS.items() if config["url"] == endpoint
        )
        return f"""<?xml version="1.0" encoding="utf-8"?>
        <rss version="2.0"><channel><item>
          <title>{symbol} reports financial results for fourth quarter 2026</title>
          <guid>{symbol}-fixture-guid</guid>
          <link>https://{host}/news/fixture-release</link>
          <pubDate>Wed, 02 Sep 2026 20:00:00 +0000</pubDate>
          <description>Bounded offline company statement fixture.</description>
        </item></channel></rss>""".encode("utf-8")


def dependencies(
    sec_fetcher: SecFixtureFetcher | None = None,
    ir_fetcher: IrFixtureFetcher | None = None,
    *,
    user_agent: str = FIXTURE_USER_AGENT,
) -> tuple[SecIrLivePreflightDependencies, SecFixtureFetcher, IrFixtureFetcher]:
    sec = sec_fetcher or SecFixtureFetcher()
    ir = ir_fetcher or IrFixtureFetcher()
    return (
        SecIrLivePreflightDependencies(
            sec_fetch_json=sec,
            ir_fetch_bytes=ir,
            sec_user_agent=user_agent,
            clock=lambda: FIXED_NOW,
            monotonic=lambda: 1.0,
        ),
        sec,
        ir,
    )


class SecIrLivePreflightTests(unittest.TestCase):
    def run_fixture(
        self,
        deps: SecIrLivePreflightDependencies | None = None,
    ) -> dict:
        fixture_dependencies = deps or dependencies()[0]
        return _run_sec_ir_live_preflight_injected(
            confirmation=SEC_IR_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=fixture_dependencies,
        )

    def test_public_boundary_has_only_exact_confirmation_parameter(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(run_sec_ir_live_preflight).parameters),
            ("confirmation",),
        )
        deps, sec, ir = dependencies()
        for value in (None, "", "RUN_SEC_IR_LIVE_PREFLIGHT_ONCE ", 1, True):
            with self.subTest(value=value):
                with self.assertRaises(SecIrLivePreflightConfirmationError):
                    _run_sec_ir_live_preflight_injected(
                        confirmation=value,
                        dependencies=deps,
                    )
        self.assertEqual(sec.calls, [])
        self.assertEqual(ir.calls, [])

    def test_direct_public_runner_without_isolated_import_guard_is_zero_network(self) -> None:
        self.assertFalse(preflight._ISOLATED_CLI_IMPORT_GUARD_ATTESTED)
        with (
            mock.patch.object(
                preflight,
                "_run_sec",
                side_effect=AssertionError("SEC network path opened"),
            ),
            mock.patch.object(
                preflight,
                "_run_ir",
                side_effect=AssertionError("IR network path opened"),
            ),
            self.assertRaises(preflight.SecIrLivePreflightEnvironmentError),
        ):
            run_sec_ir_live_preflight(
                confirmation=SEC_IR_LIVE_PREFLIGHT_CONFIRMATION,
            )

    def test_injected_fixture_passes_but_is_explicitly_not_a_receipt(self) -> None:
        deps, sec, ir = dependencies()
        report = self.run_fixture(deps)

        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["captured_at_ms"], FIXED_NOW_MS)
        self.assertEqual(report["scope"], "sec_and_company_ir_only")
        self.assertTrue(report["sec_included"])
        self.assertTrue(report["company_ir_included"])
        self.assertFalse(report["official_macro_included"])
        self.assertEqual(report["evidence_class"], "injected_offline_fixture")
        self.assertEqual(report["receipt_class"], "injected_offline_fixture_not_receipt")
        self.assertEqual(
            report["evidence_profile_sha256"],
            SEC_IR_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256,
        )
        self.assertEqual(report["source_manifest_sha256"], "")
        self.assertEqual(report["transport_identity_sha256"], "")
        self.assertEqual(report["counts"]["endpoint_fetch_attempt_count"], 12)
        self.assertEqual(report["counts"]["endpoint_success_count"], 12)
        self.assertEqual(report["counts"]["record_count"], 11)
        self.assertEqual(len(sec.calls), 8)
        self.assertEqual(len({endpoint for endpoint, _ua in sec.calls}), 8)
        self.assertEqual(len(ir.calls), 4)
        self.assertEqual(len({endpoint for endpoint, _hosts in ir.calls}), 4)
        safety = report["safety"]
        self.assertFalse(safety["live_network_attested"])
        self.assertFalse(safety["source_truth_verified"])
        self.assertEqual(safety["production_acceptance_verdict"], "NOT_EVALUATED")
        self.assertEqual(safety["overall_acceptance"], "NOT_CLAIMED")
        self.assertIsNone(safety["database_writes_performed"])
        self.assertIsNone(safety["provider_calls_performed"])
        self.assertIsNone(safety["futu_calls_performed"])
        self.assertEqual(safety["execution_capability"], "unknown")

        encoded = json.dumps(report, sort_keys=True)
        self.assertNotIn(FIXTURE_USER_AGENT, encoded)
        self.assertNotIn("example.invalid", encoded)
        self.assertNotIn("https://", encoded)
        validate_sec_ir_live_preflight_report(report)

    def test_missing_sec_user_agent_is_zero_sec_network_action(self) -> None:
        deps, sec, ir = dependencies(user_agent="")
        report = self.run_fixture(deps)

        self.assertEqual(sec.calls, [])
        self.assertEqual(len(ir.calls), 4)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["adapters"][0]["error_code"], "SEC_USER_AGENT_REQUIRED")
        self.assertEqual(report["adapters"][0]["endpoint_fetch_attempt_count"], 0)
        self.assertFalse(report["safety"]["sec_user_agent_declared"])
        self.assertNotIn("User-Agent", json.dumps(report))

    def test_sec_dynamic_submission_endpoints_are_ticker_response_bound(self) -> None:
        deps, sec, ir = dependencies(sec_fetcher=SecFixtureFetcher(symbol_count=1))
        report = self.run_fixture(deps)

        self.assertEqual(len(sec.calls), 2)
        self.assertEqual(len(ir.calls), 4)
        sec_row = report["adapters"][0]
        self.assertEqual(sec_row["status"], "degraded")
        self.assertEqual(sec_row["endpoint_fetch_attempt_count"], 2)
        self.assertEqual(sec_row["endpoint_success_count"], 2)
        self.assertEqual(sec_row["error_code"], "ENDPOINT_COVERAGE_INCOMPLETE")
        self.assertEqual(sec_row["record_count"], 1)

    def test_sec_boundary_rejects_unbound_and_duplicate_endpoints(self) -> None:
        calls: list[str] = []

        def fetch(endpoint: str, _user_agent: str) -> dict:
            calls.append(endpoint)
            return {}

        boundary = preflight._SecFetchBoundary(
            fetch,
            user_agent=FIXTURE_USER_AGENT,
            production=False,
        )
        with self.assertRaises(ValueError):
            boundary("https://data.sec.gov/submissions/CIK0000000001.json", FIXTURE_USER_AGENT)
        self.assertEqual(calls, [])
        boundary(SEC_TICKERS_URL, FIXTURE_USER_AGENT)
        with self.assertRaises(ValueError):
            boundary(SEC_TICKERS_URL, FIXTURE_USER_AGENT)
        self.assertEqual(calls, [SEC_TICKERS_URL])
        self.assertEqual(boundary.attempts, 1)

    def test_ir_boundary_requires_exact_feed_and_host_set_once(self) -> None:
        calls: list[str] = []

        def fetch(endpoint: str, _allowed_hosts: set[str]) -> bytes:
            calls.append(endpoint)
            return b"<rss />"

        boundary = preflight._IrFetchBoundary(fetch, production=False)
        symbol = next(iter(IR_FEEDS))
        endpoint = str(IR_FEEDS[symbol]["url"])
        hosts = set(IR_FEEDS[symbol]["hosts"])
        with self.assertRaises(ValueError):
            boundary(endpoint, {"evil.invalid"})
        self.assertEqual(calls, [])
        self.assertEqual(boundary(endpoint, hosts), b"<rss />")
        with self.assertRaises(ValueError):
            boundary(endpoint, hosts)
        self.assertEqual(calls, [endpoint])
        self.assertEqual(boundary.attempts, 1)

    def test_production_uses_exact_default_ir_fetch_and_discloses_redirect_scope(self) -> None:
        closure = inspect.getclosurevars(run_sec_ir_live_preflight).nonlocals
        self.assertIs(closure["ir_fetch_token"], preflight._IR_DEFAULT_FETCH_TOKEN)
        self.assertIs(
            preflight._IR_DEFAULT_FETCH_TOKEN,
            ir_module.OfficialIrReleaseAdapter._default_fetch_bytes,
        )
        safety = preflight._safety(
            0,
            production=True,
            user_agent_declared=True,
        )
        self.assertEqual(
            safety["transport_mode"],
            "guarded_default_sec_ir_https_path",
        )
        self.assertTrue(safety["initial_endpoint_allowlist_enforced"])
        self.assertFalse(safety["final_endpoint_identity_attested"])
        self.assertEqual(
            safety["redirect_policy_scope"],
            "production_default_fixed_hosts_not_exact_final_url",
        )
        self.assertEqual(safety["network_redirect_limit_per_fetch"], 5)
        self.assertEqual(safety["network_redirect_repeat_limit_per_fetch"], 2)

    def test_response_bounds_fail_closed_and_output_is_redacted(self) -> None:
        class OversizedSec(SecFixtureFetcher):
            def __call__(self, endpoint: str, user_agent: str) -> dict:
                if endpoint == SEC_TICKERS_URL:
                    self.calls.append((endpoint, user_agent))
                    return {"oversized": "x" * (sec_module.SEC_MAX_RESPONSE_BYTES + 1)}
                return super().__call__(endpoint, user_agent)

        for sec_fetcher, ir_fetcher in (
            (OversizedSec(), IrFixtureFetcher()),
            (SecFixtureFetcher(), IrFixtureFetcher(oversized=True)),
        ):
            with self.subTest(sec=type(sec_fetcher).__name__, ir=ir_fetcher.oversized):
                deps, _sec, _ir = dependencies(sec_fetcher, ir_fetcher)
                report = self.run_fixture(deps)
                encoded = json.dumps(report, sort_keys=True)
                self.assertFalse(report["ok"])
                self.assertIn("TRANSPORT_POLICY_REJECTED", encoded)
                self.assertNotIn("https://", encoded)
                self.assertNotIn(FIXTURE_USER_AGENT, encoded)
                self.assertLess(
                    len(encoded.encode("utf-8")),
                    SEC_IR_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES,
                )

    def test_transport_exception_text_is_never_reported(self) -> None:
        class SecretSec(SecFixtureFetcher):
            def __call__(self, endpoint: str, user_agent: str) -> dict:
                self.calls.append((endpoint, user_agent))
                raise RuntimeError("SECRET_TOKEN https://secret.invalid/")

        deps, _sec, _ir = dependencies(SecretSec(), IrFixtureFetcher())
        report = self.run_fixture(deps)
        encoded = json.dumps(report, sort_keys=True)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("secret.invalid", encoded)
        self.assertEqual(report["adapters"][0]["error_category"], "internal")

    def test_internal_production_flag_rejects_injected_transports(self) -> None:
        deps, sec, ir = dependencies()
        with self.assertRaises(preflight.SecIrLivePreflightEnvironmentError):
            preflight._run_boundary(
                confirmation=SEC_IR_LIVE_PREFLIGHT_CONFIRMATION,
                sec_fetch=deps.sec_fetch_json,
                ir_fetch=deps.ir_fetch_bytes,
                sec_user_agent=deps.sec_user_agent,
                clock=deps.clock,
                monotonic=deps.monotonic,
                production=True,
            )
        self.assertEqual(sec.calls, [])
        self.assertEqual(ir.calls, [])

    def test_closed_validator_rejects_fixture_as_production(self) -> None:
        report = self.run_fixture()
        attacks = []
        changed_class = dict(report)
        changed_class["evidence_class"] = "production_path_observation"
        attacks.append(changed_class)
        changed_receipt = dict(report)
        changed_receipt["receipt_class"] = "production_path_observation_not_attestation"
        attacks.append(changed_receipt)
        extra = dict(report)
        extra["secret"] = "https://secret.invalid"
        attacks.append(extra)
        changed_safety = dict(report)
        changed_safety["safety"] = dict(report["safety"])
        changed_safety["safety"]["live_network_attested"] = True
        attacks.append(changed_safety)
        false_user_agent = dict(report)
        false_user_agent["safety"] = dict(report["safety"])
        false_user_agent["safety"]["sec_user_agent_declared"] = False
        attacks.append(false_user_agent)
        impossible_empty = dict(report)
        impossible_empty["adapters"] = [dict(row) for row in report["adapters"]]
        impossible_empty["counts"] = dict(report["counts"])
        impossible_empty["adapters"][0]["status"] = "degraded"
        impossible_empty["adapters"][0]["error_code"] = "SOURCE_EMPTY"
        impossible_empty["adapters"][0]["error_category"] = "source_payload"
        impossible_empty["status"] = "degraded"
        impossible_empty["ok"] = False
        impossible_empty["counts"]["passed_count"] -= 1
        impossible_empty["counts"]["degraded_count"] += 1
        attacks.append(impossible_empty)
        impossible_transport = dict(report)
        impossible_transport["adapters"] = [
            dict(row) for row in report["adapters"]
        ]
        impossible_transport["counts"] = dict(report["counts"])
        impossible_transport["adapters"][0]["status"] = "degraded"
        impossible_transport["adapters"][0]["error_code"] = "HTTP_NOT_FOUND"
        impossible_transport["adapters"][0]["error_category"] = "http"
        impossible_transport["adapters"][0]["transport_failure_count"] = 1
        impossible_transport["status"] = "degraded"
        impossible_transport["ok"] = False
        impossible_transport["counts"]["passed_count"] -= 1
        impossible_transport["counts"]["degraded_count"] += 1
        impossible_transport["counts"]["transport_failure_count"] += 1
        attacks.append(impossible_transport)
        for attack in attacks:
            with self.subTest(keys=attack.keys()):
                with self.assertRaisesRegex(ValueError, "invalid SEC/IR"):
                    validate_sec_ir_live_preflight_report(attack)

    def test_module_import_does_not_load_stateful_services(self) -> None:
        project_root = Path(__file__).parents[1]
        program = textwrap.dedent(
            """
            import json
            import sys
            import backend.source_monitoring.sec_ir_live_preflight as module
            forbidden = (
                "backend.store",
                "backend.source_inbox_service",
                "backend.provider_gateway",
                "backend.source_monitoring.runtime",
                "backend.source_monitoring.state_repository",
                "backend.source_monitoring.supervisor",
            )
            print(json.dumps({name: name in sys.modules for name in forbidden}, sort_keys=True))
            """
        )
        environment = os.environ.copy()
        environment.update({
            "AI_STUDIO_SKIP_LOCAL_ENV": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        self.assertTrue(all(value is False for value in json.loads(completed.stdout).values()))


class SecIrLivePreflightCliTests(unittest.TestCase):
    def invoke(self, arguments: list[str], *, runner) -> tuple[int, str, dict]:
        output = io.StringIO()
        exit_code = cli._main_injected(arguments, output=output, runner=runner)
        text = output.getvalue()
        return exit_code, text, json.loads(text)

    def test_public_cli_signature_and_top_level_imports_are_closed(self) -> None:
        self.assertEqual(tuple(inspect.signature(cli.main).parameters), ("argv", "output"))
        script_path = Path(__file__).parents[1] / "scripts" / "run_sec_ir_live_preflight.py"
        tree = ast.parse(script_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("backend"))
            if isinstance(node, ast.Import):
                self.assertTrue(all(not alias.name.startswith("backend") for alias in node.names))

    def test_help_bad_arguments_and_confirmation_are_zero_action(self) -> None:
        calls: list[str] = []

        def forbidden_runner(confirmation: str) -> dict:
            calls.append(confirmation)
            raise AssertionError("runner must remain closed")

        cases = (
            (["--help"], 0, "help", None),
            ([], 2, "not_started", "PREFLIGHT_CONFIRMATION_REQUIRED"),
            (["--confirm", "wrong"], 2, "not_started", "PREFLIGHT_CONFIRMATION_REQUIRED"),
            (["--unknown"], 2, "not_started", "PREFLIGHT_ARGUMENTS_INVALID"),
            (["--confirm"], 2, "not_started", "PREFLIGHT_ARGUMENTS_INVALID"),
            (
                ["--confirm", cli.PREFLIGHT_CONFIRMATION, "--confirm", cli.PREFLIGHT_CONFIRMATION],
                2,
                "not_started",
                "PREFLIGHT_ARGUMENTS_INVALID",
            ),
        )
        for arguments, expected_exit, status, error_code in cases:
            with self.subTest(arguments=arguments):
                exit_code, text, payload = self.invoke(arguments, runner=forbidden_runner)
                self.assertEqual(exit_code, expected_exit)
                self.assertEqual(payload["status"], status)
                if error_code is not None:
                    self.assertEqual(payload["error_code"], error_code)
                self.assertEqual(payload["safety"]["network_requests_performed"], 0)
                self.assertEqual(payload["safety"]["database_writes_performed"], 0)
                self.assertFalse(payload["safety"]["live_network_attested"])
                self.assertLess(len(text.encode("ascii")), cli.MAX_OUTPUT_BYTES)
        self.assertEqual(calls, [])

    def test_injected_fixture_cannot_be_emitted_as_production(self) -> None:
        report = _run_sec_ir_live_preflight_injected(
            confirmation=SEC_IR_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=dependencies()[0],
        )
        confirmations: list[str] = []

        def runner(confirmation: str) -> dict:
            confirmations.append(confirmation)
            return report

        exit_code, text, payload = self.invoke(
            ["--confirm", cli.PREFLIGHT_CONFIRMATION],
            runner=runner,
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(confirmations, [SEC_IR_LIVE_PREFLIGHT_CONFIRMATION])
        self.assertEqual(payload["status"], "indeterminate")
        self.assertEqual(payload["error_code"], "PREFLIGHT_PRODUCTION_REQUIRED")
        self.assertNotIn("injected_offline_fixture", text)
        self.assertIsNone(payload["safety"]["database_writes_performed"])

    def test_runner_exception_and_untrusted_output_are_generic(self) -> None:
        runners = (
            lambda _confirmation: {"secret": "https://secret.invalid/TOKEN"},
            lambda _confirmation: (_ for _ in ()).throw(
                RuntimeError("SECRET_USER_AGENT secret@example.invalid")
            ),
        )
        for runner in runners:
            with self.subTest(runner=runner):
                exit_code, text, payload = self.invoke(
                    ["--confirm", cli.PREFLIGHT_CONFIRMATION],
                    runner=runner,
                )
                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["status"], "indeterminate")
                self.assertEqual(payload["error_code"], "PREFLIGHT_INTERNAL_ERROR")
                self.assertNotIn("SECRET", text)
                self.assertNotIn("secret.invalid", text)
                self.assertIsNone(payload["safety"]["network_requests_performed"])

    def test_public_cli_requires_isolated_process_before_backend_import(self) -> None:
        project_root = Path(__file__).parents[1]
        script_path = project_root / "scripts" / "run_sec_ir_live_preflight.py"
        with tempfile.TemporaryDirectory(prefix="sec-ir-preflight-") as root:
            database_sentinel = Path(root) / "must-not-exist.sqlite3"
            environment = os.environ.copy()
            environment.update({
                "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                "AI_STUDIO_DATABASE_PATH": str(database_sentinel),
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1",
                "PYTHONDONTWRITEBYTECODE": "1",
            })
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    "--confirm",
                    cli.PREFLIGHT_CONFIRMATION,
                ],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(completed.stderr, "")
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["error_code"], "PREFLIGHT_ISOLATED_PROCESS_REQUIRED")
            self.assertEqual(payload["safety"]["network_requests_performed"], 0)
            self.assertFalse(database_sentinel.exists())

    def test_backend_import_redirects_config_paths_to_existing_directory(self) -> None:
        project_root = Path(__file__).parents[1]
        expected_unrelated_config_env = frozenset({
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
        })
        self.assertEqual(
            frozenset(cli._UNRELATED_CONFIG_ENV),
            expected_unrelated_config_env,
        )
        self.assertEqual(
            len(cli._UNRELATED_CONFIG_ENV),
            len(expected_unrelated_config_env),
        )
        with tempfile.TemporaryDirectory(prefix="sec-ir-import-") as root:
            runtime_sentinel = Path(root) / "runtime-must-not-exist"
            database_parent = Path(root) / "database-parent-must-not-exist"
            program = textwrap.dedent(
                f"""
                import json
                import os
                import sys
                from datetime import datetime, timezone
                from pathlib import Path
                sys.dont_write_bytecode = True
                sys.path.insert(0, {str(project_root)!r})
                from scripts import run_sec_ir_live_preflight as cli
                unrelated_environment = {{
                    key: "SECRET_UNRELATED_" + str(index)
                    for index, key in enumerate(cli._UNRELATED_CONFIG_ENV)
                }}
                unrelated_environment.update({{
                    "AI_STUDIO_PORT": "not-an-integer",
                    "FUTU_PORT": "not-an-integer",
                    "FUTU_CACHE_TTL_SECONDS": "not-a-float",
                    "SEC_CACHE_TTL_SECONDS": "not-a-float",
                }})
                controlled_environment = {{
                    "AI_STUDIO_SKIP_LOCAL_ENV": "0",
                    "AI_STUDIO_RUNTIME_DIR": {str(runtime_sentinel)!r},
                    "AI_STUDIO_DATABASE_PATH": {str(database_parent / 'db.sqlite3')!r},
                    "AI_STUDIO_SEC_IR_PREFLIGHT_IMPORT_GUARD": "original-guard",
                }}
                sec_user_agent = "AI Studio SEC preflight contact@example.invalid"
                expected_environment = dict(unrelated_environment)
                expected_environment.update(controlled_environment)
                expected_environment["SEC_USER_AGENT"] = sec_user_agent
                for key, value in expected_environment.items():
                    os.environ[key] = value
                local_env_reads = []
                unrelated_value_reads = []
                original_is_file = Path.is_file
                original_read_text = Path.read_text
                original_getenv = os.getenv
                def observed_is_file(path):
                    if path.name == ".env.local":
                        local_env_reads.append("is_file")
                        return True
                    return original_is_file(path)
                def observed_read_text(path, *args, **kwargs):
                    if path.name == ".env.local":
                        local_env_reads.append("read_text")
                        return "OPENAI_API_KEY=SECRET_PROVIDER_KEY"
                    return original_read_text(path, *args, **kwargs)
                def observed_getenv(name, default=None):
                    value = original_getenv(name, default)
                    if (
                        name in unrelated_environment
                        and value == unrelated_environment[name]
                    ):
                        unrelated_value_reads.append(name)
                    return value
                Path.is_file = observed_is_file
                Path.read_text = observed_read_text
                os.getenv = observed_getenv
                try:
                    runner = cli._load_backend_runner({str(project_root)!r})
                finally:
                    os.getenv = original_getenv
                module = sys.modules[runner.__module__]
                config = module._config_module
                configuration = {{
                    "OPENAI_API_KEY": config.OPENAI_API_KEY,
                    "OPENAI_BASE_URL": config.OPENAI_BASE_URL,
                    "OPENAI_MODEL": config.OPENAI_MODEL,
                    "DEEPSEEK_API_KEY": config.DEEPSEEK_API_KEY,
                    "DEEPSEEK_BASE_URL": config.DEEPSEEK_BASE_URL,
                    "DEEPSEEK_MODEL": config.DEEPSEEK_MODEL,
                    "ARK_API_KEY": config.ARK_API_KEY,
                    "ARK_BASE_URL": config.ARK_BASE_URL,
                    "ARK_MODEL": config.ARK_MODEL,
                    "GLM_API_KEY": config.GLM_API_KEY,
                    "GLM_BASE_URL": config.GLM_BASE_URL,
                    "GLM_MODEL": config.GLM_MODEL,
                    "DEFAULT_PROVIDER": config.DEFAULT_PROVIDER,
                    "DISABLED_PROVIDER_IDS": sorted(config.DISABLED_PROVIDER_IDS),
                    "HOST": config.HOST,
                    "PORT": config.PORT,
                    "PROJECT_CAPABILITY_SIGNING_SECRET": config.PROJECT_CAPABILITY_SIGNING_SECRET,
                    "FUTU_HOST": config.FUTU_HOST,
                    "FUTU_PORT": config.FUTU_PORT,
                    "FUTU_CACHE_TTL_SECONDS": config.FUTU_CACHE_TTL_SECONDS,
                    "SEC_USER_AGENT": config.SEC_USER_AGENT,
                    "SEC_CACHE_TTL_SECONDS": config.SEC_CACHE_TTL_SECONDS,
                }}
                dependencies_current = module._dependencies_are_current()
                original_template = module._sec_module.SEC_SUBMISSIONS_URL
                module._sec_module.SEC_SUBMISSIONS_URL = "https://evil.invalid/CIK{{cik}}.json"
                endpoint_drift_detected = not module._dependencies_are_current()
                module._sec_module.SEC_SUBMISSIONS_URL = original_template
                handler = module._HTTPS_REDIRECT_HANDLER_TOKEN
                original_repeats = handler.max_repeats
                handler.max_repeats = 99
                repeat_drift_detected = not module._dependencies_are_current()
                handler.max_repeats = original_repeats
                report = module._early_failure(
                    production=True,
                    user_agent_declared=True,
                )
                dependency_checks = iter((True, False))
                module._dependencies_are_current = lambda: next(dependency_checks)
                module._run_sec = lambda **_kwargs: {{}}
                module._run_ir = lambda **_kwargs: {{}}
                post_action_guard_raised = False
                try:
                    module._run_boundary(
                        confirmation=module.SEC_IR_LIVE_PREFLIGHT_CONFIRMATION,
                        sec_fetch=module._SEC_DEFAULT_FETCH_TOKEN,
                        ir_fetch=module._IR_DEFAULT_FETCH_TOKEN,
                        sec_user_agent=module._SEC_USER_AGENT_TOKEN,
                        clock=lambda: datetime(2026, 9, 3, tzinfo=timezone.utc),
                        monotonic=lambda: 1.0,
                        production=True,
                    )
                except module.SecIrLivePreflightIndeterminateError:
                    post_action_guard_raised = True
                print(json.dumps({{
                    "callable": callable(runner),
                    "runtime_exists": os.path.exists({str(runtime_sentinel)!r}),
                    "database_parent_exists": os.path.exists({str(database_parent)!r}),
                    "local_env_reads": local_env_reads,
                    "unrelated_value_reads": unrelated_value_reads,
                    "environment_restored": {{
                        key: os.environ.get(key)
                        for key in expected_environment
                    }} == expected_environment,
                    "configuration": configuration,
                    "import_guard_attested": module._ISOLATED_CLI_IMPORT_GUARD_ATTESTED,
                    "dependencies_current": dependencies_current,
                    "endpoint_drift_detected": endpoint_drift_detected,
                    "repeat_drift_detected": repeat_drift_detected,
                    "report": report,
                    "post_action_guard_raised": post_action_guard_raised,
                }}, sort_keys=True))
                """
            )
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-c", program],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            result = json.loads(completed.stdout)
            portable_report = result.pop("report")
            self.assertEqual(
                result,
                {
                    "callable": True,
                    "runtime_exists": False,
                    "database_parent_exists": False,
                    "local_env_reads": [],
                    "unrelated_value_reads": [],
                    "environment_restored": True,
                    "configuration": {
                        "OPENAI_API_KEY": "",
                        "OPENAI_BASE_URL": "https://api.openai.com/v1",
                        "OPENAI_MODEL": "gpt-5.4-mini",
                        "DEEPSEEK_API_KEY": "",
                        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
                        "DEEPSEEK_MODEL": "deepseek-v4-pro",
                        "ARK_API_KEY": "",
                        "ARK_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
                        "ARK_MODEL": "doubao-seed-2-0-lite-260215",
                        "GLM_API_KEY": "",
                        "GLM_BASE_URL": "https://open.bigmodel.cn/api/paas/v4",
                        "GLM_MODEL": "glm-5.2",
                        "DEFAULT_PROVIDER": "deepseek",
                        "DISABLED_PROVIDER_IDS": ["openai"],
                        "HOST": "127.0.0.1",
                        "PORT": 8770,
                        "PROJECT_CAPABILITY_SIGNING_SECRET": "",
                        "FUTU_HOST": "127.0.0.1",
                        "FUTU_PORT": 11111,
                        "FUTU_CACHE_TTL_SECONDS": 5.0,
                        "SEC_USER_AGENT": (
                            "AI Studio SEC preflight contact@example.invalid"
                        ),
                        "SEC_CACHE_TTL_SECONDS": 300.0,
                    },
                    "import_guard_attested": True,
                    "dependencies_current": True,
                    "endpoint_drift_detected": True,
                    "repeat_drift_detected": True,
                    "post_action_guard_raised": True,
                },
            )
            validated = validate_sec_ir_live_preflight_report(portable_report)
            self.assertTrue(
                validated["safety"]["isolated_cli_import_guard_attested"]
            )
            self.assertEqual(
                validated["safety"]["application_file_writes_performed"],
                0,
            )
            self.assertTrue(validated["safety"]["local_env_loading_disabled"])
            self.assertEqual(
                validated["safety"]["sec_user_agent_source"],
                "explicit_process_environment_only",
            )

    def test_output_bound_has_fixed_redacted_fallback(self) -> None:
        encoded = cli._bounded_json({"untrusted": "SECRET" * cli.MAX_OUTPUT_BYTES})
        payload = json.loads(encoded)
        self.assertLess(len(encoded.encode("ascii")), cli.MAX_OUTPUT_BYTES)
        self.assertEqual(payload["error_code"], "PREFLIGHT_OUTPUT_BOUND_EXCEEDED")
        self.assertEqual(payload["status"], "indeterminate")
        self.assertNotIn("SECRET", encoded)


if __name__ == "__main__":
    unittest.main()
