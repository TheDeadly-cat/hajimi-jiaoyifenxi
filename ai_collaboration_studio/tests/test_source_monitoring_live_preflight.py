from __future__ import annotations

import ast
import copy
import inspect
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest import mock


os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"

from backend.market import official_macro  # noqa: E402
from backend.market.official_macro import (  # noqa: E402
    BLS_RELEASE_CALENDAR_URL,
    BLS_SERIES_IDS,
    BLS_SERIES_URLS,
    FEDERAL_RESERVE_FOMC_CALENDAR_URL,
    FEDERAL_RESERVE_MONETARY_RSS_URL,
    OfficialMacroSourceClient,
    TREASURY_DEBT_TO_PENNY_URL,
    TREASURY_RELEASE_CALENDAR_URL,
)
from backend.source_monitoring.contracts import (  # noqa: E402
    AdapterPollResult,
    canonical_sha256,
)
from backend.source_monitoring import live_preflight as live_preflight_module  # noqa: E402
from backend.source_monitoring.live_preflight import (  # noqa: E402
    OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION,
    OFFICIAL_SOURCE_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256,
    OFFICIAL_SOURCE_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES,
    OFFICIAL_SOURCE_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256,
    OFFICIAL_SOURCE_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256,
    OFFICIAL_SOURCE_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256,
    OfficialSourceLivePreflightConfirmationError,
    OfficialSourceLivePreflightDependencies,
    _run_official_source_live_preflight_injected,
    run_official_source_live_preflight,
    validate_live_preflight_report,
)
from scripts.run_official_source_live_preflight import (  # noqa: E402
    PREFLIGHT_CONFIRMATION,
    _main_injected as cli_injected_main,
    main as cli_main,
)
from scripts import run_official_source_live_preflight as cli_module  # noqa: E402


FIXED_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
FIXED_NOW_MS = int(FIXED_NOW.timestamp() * 1_000)
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "official_macro"
EXPECTED_ENDPOINTS = (
    FEDERAL_RESERVE_MONETARY_RSS_URL,
    *(BLS_SERIES_URLS[series_id] for series_id in BLS_SERIES_IDS),
    TREASURY_DEBT_TO_PENNY_URL,
    FEDERAL_RESERVE_FOMC_CALENDAR_URL,
    BLS_RELEASE_CALENDAR_URL,
    TREASURY_RELEASE_CALENDAR_URL,
)
FIXTURE_NAMES = {
    FEDERAL_RESERVE_MONETARY_RSS_URL: "fed_press_monetary.xml",
    FEDERAL_RESERVE_FOMC_CALENDAR_URL: "fed_fomc_calendar.html",
    BLS_SERIES_URLS["CUSR0000SA0"]: "bls_cpi.json",
    BLS_SERIES_URLS["LNS14000000"]: "bls_unemployment.json",
    BLS_SERIES_URLS["CES0000000001"]: "bls_payrolls.json",
    BLS_RELEASE_CALENDAR_URL: "bls_calendar.ics",
    TREASURY_DEBT_TO_PENNY_URL: "treasury_debt_to_penny.json",
    TREASURY_RELEASE_CALENDAR_URL: "treasury_release_calendar.json",
}


def _fixture_payloads() -> dict[str, bytes]:
    return {
        endpoint: (FIXTURE_ROOT / name).read_bytes()
        for endpoint, name in FIXTURE_NAMES.items()
    }


class FixtureFetcher:
    def __init__(self) -> None:
        self.payloads = _fixture_payloads()
        self.calls: list[str] = []

    def __call__(self, endpoint: str) -> bytes:
        self.calls.append(endpoint)
        return self.payloads[endpoint]


def _dependencies(fetch_bytes) -> OfficialSourceLivePreflightDependencies:
    return OfficialSourceLivePreflightDependencies(
        fetch_bytes=fetch_bytes,
        clock=lambda: FIXED_NOW,
        monotonic=lambda: 100.0,
    )


class OfficialSourceLivePreflightTests(unittest.TestCase):
    def test_exact_confirmation_is_checked_inside_public_network_boundary(self) -> None:
        events: list[str] = []

        class PretendConfirmation:
            def __eq__(self, _other: object) -> bool:
                return True

        def forbidden_fetch(_endpoint: str) -> bytes:
            events.append("fetch")
            raise AssertionError("network must remain closed")

        def forbidden_clock() -> datetime:
            events.append("clock")
            raise AssertionError("clock must remain closed")

        dependencies = OfficialSourceLivePreflightDependencies(
            fetch_bytes=forbidden_fetch,
            clock=forbidden_clock,
            monotonic=lambda: 0.0,
        )
        for confirmation in (
            None,
            "",
            "run_official_source_live_preflight_once",
            PretendConfirmation(),
        ):
            with self.subTest(confirmation=confirmation):
                with self.assertRaises(OfficialSourceLivePreflightConfirmationError):
                    _run_official_source_live_preflight_injected(
                        confirmation=confirmation,
                        dependencies=dependencies,
                    )
                self.assertEqual(events, [])

    def test_fixture_preflight_runs_each_fixed_endpoint_once_and_is_bounded(self) -> None:
        fetcher = FixtureFetcher()
        report = _run_official_source_live_preflight_injected(
            confirmation=OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=_dependencies(fetcher),
        )

        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["scope"], "official_macro_only")
        self.assertFalse(report["sec_included"])
        self.assertFalse(report["company_ir_included"])
        self.assertEqual(report["evidence_class"], "injected_offline_fixture")
        self.assertEqual(
            report["evidence_profile_sha256"],
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256,
        )
        self.assertEqual(report["source_manifest_sha256"], "")
        self.assertEqual(report["transport_identity_sha256"], "")
        self.assertEqual(report["captured_at_ms"], FIXED_NOW_MS)
        self.assertEqual(fetcher.calls, list(EXPECTED_ENDPOINTS))
        self.assertEqual(
            [row["adapter_key"] for row in report["adapters"]],
            [
                "federal_reserve",
                "bls_releases",
                "treasury_releases",
                "official_macro_calendar",
            ],
        )
        self.assertEqual(
            [row["endpoint_fetch_attempt_count"] for row in report["adapters"]],
            [1, 3, 1, 3],
        )
        self.assertEqual(
            [row["record_count"] for row in report["adapters"][:3]],
            [1, 12, 2],
        )
        self.assertTrue(all(row["status"] == "passed" for row in report["adapters"]))
        self.assertEqual(report["counts"]["endpoint_fetch_attempt_count"], 8)
        self.assertEqual(report["counts"]["source_error_count"], 0)
        self.assertIsNone(report["safety"]["network_requests_performed"])
        self.assertEqual(
            report["safety"]["network_requests_accounting"],
            "not_instrumented",
        )
        self.assertEqual(
            report["safety"]["endpoint_fetch_attempts_performed"],
            8,
        )
        self.assertEqual(
            report["safety"]["endpoint_fetch_attempts_accounting"],
            "exact",
        )
        self.assertIsNone(report["safety"]["retries_performed"])
        self.assertEqual(
            report["safety"]["transport_mode"],
            "injected_offline",
        )
        self.assertFalse(report["safety"]["live_network_attested"])
        self.assertFalse(
            report["safety"]["in_process_tamper_resistant"]
        )
        self.assertIsNone(report["safety"]["proxy_configuration_overridden"])
        self.assertIsNone(report["safety"]["tls_verification_disabled"])
        self.assertIsNone(report["safety"]["database_reads_performed"])
        self.assertIsNone(report["safety"]["database_writes_performed"])
        self.assertIsNone(report["safety"]["application_file_writes_performed"])
        self.assertIsNone(report["safety"]["provider_calls_performed"])
        self.assertIsNone(report["safety"]["futu_calls_performed"])
        self.assertIsNone(report["safety"]["live_trading_allowed"])

        encoded = json.dumps(report, ensure_ascii=True, sort_keys=True)
        self.assertLess(
            len(encoded.encode("ascii")),
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES,
        )
        for forbidden in (
            "https://",
            "Federal Reserve issues FOMC statement",
            "UNEXPECTED_EOF_WHILE_READING",
            "SECRET",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_ssl_eof_is_reduced_to_fixed_code_without_raw_exception(self) -> None:
        calls: list[str] = []

        def eof_fetch(endpoint: str) -> bytes:
            calls.append(endpoint)
            raise URLError(
                ssl.SSLEOFError(
                    8,
                    "UNEXPECTED_EOF_WHILE_READING SECRET_PROXY_DETAIL",
                )
            )

        report = _run_official_source_live_preflight_injected(
            confirmation=OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=_dependencies(eof_fetch),
        )

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(calls, list(EXPECTED_ENDPOINTS))
        self.assertEqual(report["counts"]["endpoint_fetch_attempt_count"], 8)
        self.assertEqual(report["counts"]["endpoint_success_count"], 0)
        self.assertEqual(report["counts"]["source_error_count"], 8)
        for row in report["adapters"]:
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["error_code"], "TLS_HANDSHAKE_EOF")
            self.assertEqual(row["error_category"], "tls")
        self.assertEqual(report["safety"]["transport_mode"], "injected_offline")
        self.assertIsNone(report["safety"]["retries_performed"])
        self.assertIsNone(report["safety"]["proxy_configuration_overridden"])
        self.assertIsNone(report["safety"]["tls_verification_disabled"])
        self.assertIsNone(report["safety"]["database_writes_performed"])
        encoded = json.dumps(report, ensure_ascii=True, sort_keys=True)
        self.assertNotIn("UNEXPECTED_EOF", encoded)
        self.assertNotIn("SECRET_PROXY_DETAIL", encoded)

    def test_other_transport_failures_use_only_fixed_categories(self) -> None:
        cases = (
            (
                lambda: HTTPError(
                    "https://SECRET.invalid/",
                    403,
                    "SECRET_HTTP_DETAIL",
                    None,
                    None,
                ),
                "HTTP_ACCESS_DENIED",
                "http",
            ),
            (
                lambda: socket.gaierror(-2, "SECRET_DNS_DETAIL"),
                "DNS_RESOLUTION_FAILED",
                "dns",
            ),
            (
                lambda: TimeoutError("SECRET_TIMEOUT_DETAIL"),
                "NETWORK_TIMEOUT",
                "timeout",
            ),
            (
                lambda: ValueError("SECRET_POLICY_DETAIL"),
                "TRANSPORT_POLICY_REJECTED",
                "transport_policy",
            ),
        )
        for error_factory, code, category in cases:
            with self.subTest(code=code):
                def failing_fetch(_endpoint: str) -> bytes:
                    raise error_factory()

                report = _run_official_source_live_preflight_injected(
                    confirmation=OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION,
                    dependencies=_dependencies(failing_fetch),
                )
                self.assertTrue(
                    all(row["error_code"] == code for row in report["adapters"])
                )
                self.assertTrue(
                    all(
                        row["error_category"] == category
                        for row in report["adapters"]
                    )
                )
                encoded = json.dumps(report, ensure_ascii=True)
                self.assertNotIn("SECRET", encoded)
                self.assertNotIn("https://", encoded)

    def test_partial_transport_failure_is_degraded_without_retry(self) -> None:
        fetcher = FixtureFetcher()
        failed_endpoint = BLS_SERIES_URLS["LNS14000000"]

        def partial_fetch(endpoint: str) -> bytes:
            if endpoint == failed_endpoint:
                fetcher.calls.append(endpoint)
                raise URLError(ssl.SSLEOFError(8, "SECRET_PARTIAL_EOF"))
            return fetcher(endpoint)

        report = _run_official_source_live_preflight_injected(
            confirmation=OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=_dependencies(partial_fetch),
        )

        self.assertEqual(fetcher.calls, list(EXPECTED_ENDPOINTS))
        self.assertEqual(fetcher.calls.count(failed_endpoint), 1)
        bls = report["adapters"][1]
        self.assertEqual(bls["status"], "degraded")
        self.assertEqual(bls["endpoint_success_count"], 2)
        self.assertEqual(bls["source_error_count"], 1)
        self.assertEqual(bls["error_code"], "TLS_HANDSHAKE_EOF")
        self.assertEqual(report["status"], "degraded")
        self.assertEqual(report["counts"]["degraded_count"], 1)
        self.assertNotIn("SECRET_PARTIAL_EOF", json.dumps(report))

    def test_rejected_result_is_degraded_without_fabricating_source_error(self) -> None:
        class RejectedAdapter:
            max_candidates_per_poll = 50

            def __init__(self, *, client) -> None:
                self.client = client

            def poll(
                self,
                _checkpoint: dict,
                *,
                observed_at_ms: int,
                max_items: int,
            ) -> AdapterPollResult:
                payload = self.client.federal_reserve_releases(limit=max_items)
                if payload["source_errors"]:
                    raise AssertionError("fixture parse unexpectedly failed")
                return AdapterPollResult.build(
                    "federal_reserve",
                    {},
                    {},
                    (),
                    (),
                    captured_at_ms=observed_at_ms,
                    rejected_count=1,
                )

        row = live_preflight_module._run_adapter(
            live_preflight_module._AdapterSpec(
                "federal_reserve",
                (FEDERAL_RESERVE_MONETARY_RSS_URL,),
                50,
                1,
                RejectedAdapter,
            ),
            fetch_bytes=FixtureFetcher(),
            observed_at=FIXED_NOW,
            captured_at_ms=FIXED_NOW_MS,
            monotonic=lambda: 1.0,
        )

        self.assertEqual(row["status"], "degraded")
        self.assertEqual(row["error_code"], "SOURCE_PAYLOAD_INVALID")
        self.assertEqual(row["rejected_count"], 1)
        self.assertEqual(row["source_error_count"], 0)

    def test_invalid_clock_is_bounded_and_precedes_fetch(self) -> None:
        calls: list[str] = []

        def forbidden_fetch(_endpoint: str) -> bytes:
            calls.append("fetch")
            raise AssertionError("fetch must not run")

        def bad_clock() -> datetime:
            raise RuntimeError("SECRET_CLOCK_PATH")

        report = _run_official_source_live_preflight_injected(
            confirmation=OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=OfficialSourceLivePreflightDependencies(
                fetch_bytes=forbidden_fetch,
                clock=bad_clock,
                monotonic=lambda: 0.0,
            ),
        )

        self.assertEqual(calls, [])
        self.assertEqual(report["captured_at_ms"], 0)
        self.assertEqual(report["counts"]["endpoint_fetch_attempt_count"], 0)
        self.assertTrue(
            all(
                row["error_code"] == "PREFLIGHT_INTERNAL_ERROR"
                for row in report["adapters"]
            )
        )
        self.assertNotIn("SECRET_CLOCK_PATH", json.dumps(report))

    def test_report_binds_production_manifest_and_transport_identity(self) -> None:
        self.assertEqual(
            PREFLIGHT_CONFIRMATION,
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION,
        )
        signature = inspect.signature(run_official_source_live_preflight)
        self.assertEqual(tuple(signature.parameters), ("confirmation",))
        closure = inspect.getclosurevars(run_official_source_live_preflight)
        self.assertIs(
            closure.nonlocals["guarded_fetch"],
            live_preflight_module._GUARDED_PRODUCTION_FETCH,
        )
        self.assertNotIn("dependencies", signature.parameters)
        replacement_calls: list[str] = []

        def replacement(_endpoint: str) -> bytes:
            replacement_calls.append("called")
            raise AssertionError("mutable aliases must not replace the guard")

        with mock.patch.object(
            live_preflight_module,
            "DEFAULT_OFFICIAL_MACRO_FETCH_BYTES",
            new=replacement,
        ):
            resealed = inspect.getclosurevars(run_official_source_live_preflight)
            self.assertIs(
                resealed.nonlocals["guarded_fetch"],
                live_preflight_module._GUARDED_PRODUCTION_FETCH,
            )
            with self.assertRaises(OfficialSourceLivePreflightConfirmationError):
                run_official_source_live_preflight(confirmation="wrong")
        self.assertEqual(replacement_calls, [])
        self.assertEqual(
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256,
            canonical_sha256(OfficialMacroSourceClient().config_basis()),
        )
        self.assertRegex(
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256,
            r"[0-9a-f]{64}\Z",
        )
        self.assertRegex(
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256,
            r"[0-9a-f]{64}\Z",
        )
        self.assertRegex(
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256,
            r"[0-9a-f]{64}\Z",
        )
        self.assertNotEqual(
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256,
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_INJECTED_EVIDENCE_PROFILE_SHA256,
        )

    def test_transitive_transport_patch_cannot_forge_production_pass(self) -> None:
        opener_calls: list[str] = []

        def forbidden_opener(*_args, **_kwargs):
            opener_calls.append("opener")
            raise AssertionError("patched opener must not be called")

        with (
            mock.patch.object(
                official_macro,
                "open_official_https",
                new=forbidden_opener,
            ),
            mock.patch(
                "socket.socket.connect",
                side_effect=AssertionError("network must remain closed"),
            ),
        ):
            report = run_official_source_live_preflight(
                confirmation=OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(
            report["evidence_class"],
            "production_path_observation",
        )
        self.assertFalse(report["safety"]["live_network_attested"])
        self.assertFalse(
            report["safety"]["in_process_tamper_resistant"]
        )
        self.assertEqual(opener_calls, [])
        self.assertTrue(
            all(
                row["error_code"] == "PREFLIGHT_DEPENDENCY_GUARD_FAILED"
                for row in report["adapters"]
            )
        )
        self.assertTrue(
            all(row["error_category"] == "internal" for row in report["adapters"])
        )

    def test_validator_rejects_unknown_or_inconsistent_fields_without_echo(self) -> None:
        report = _run_official_source_live_preflight_injected(
            confirmation=OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=_dependencies(FixtureFetcher()),
        )

        class StringSubclass(str):
            pass

        for field in (
            "status",
            "evidence_profile_sha256",
            "source_manifest_sha256",
            "transport_identity_sha256",
        ):
            tampered = copy.deepcopy(report)
            tampered[field] = StringSubclass(tampered[field])
            with self.subTest(native_top_level_string=field):
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid live preflight report",
                ):
                    validate_live_preflight_report(tampered)

        tampered = copy.deepcopy(report)
        tampered["SECRET_URL"] = "https://secret.invalid/"
        with self.assertRaisesRegex(ValueError, "invalid live preflight report") as raised:
            validate_live_preflight_report(tampered)
        self.assertNotIn("SECRET", str(raised.exception))

        tampered = copy.deepcopy(report)
        tampered["counts"]["record_count"] += 1
        with self.assertRaisesRegex(ValueError, "invalid live preflight report"):
            validate_live_preflight_report(tampered)

        tampered = copy.deepcopy(report)
        delta = 13 - tampered["adapters"][1]["record_count"]
        tampered["adapters"][1]["record_count"] = 13
        tampered["counts"]["record_count"] += delta
        with self.assertRaisesRegex(ValueError, "invalid live preflight report"):
            validate_live_preflight_report(tampered)

        for field, replacement in (
            ("endpoint_success_count", 0),
            ("endpoint_fetch_attempt_count", 0),
            ("rejected_count", 1),
        ):
            tampered = copy.deepcopy(report)
            original = tampered["adapters"][0][field]
            tampered["adapters"][0][field] = replacement
            tampered["counts"][field] += replacement - original
            with self.subTest(passed_invariant=field):
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid live preflight report",
                ):
                    validate_live_preflight_report(tampered)

        tampered = copy.deepcopy(report)
        tampered["captured_at_ms"] = 0
        with self.assertRaisesRegex(ValueError, "invalid live preflight report"):
            validate_live_preflight_report(tampered)

        for index in (0, 1, 2):
            tampered = copy.deepcopy(report)
            original = tampered["adapters"][index]["record_count"]
            tampered["adapters"][index]["record_count"] = 0
            tampered["counts"]["record_count"] -= original
            with self.subTest(minimum_records=index):
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid live preflight report",
                ):
                    validate_live_preflight_report(tampered)

        tampered = copy.deepcopy(report)
        tampered["adapters"][0]["duplicate_count"] = 50
        tampered["counts"]["duplicate_count"] = 50
        with self.assertRaisesRegex(ValueError, "invalid live preflight report"):
            validate_live_preflight_report(tampered)

        production_shaped = copy.deepcopy(report)
        production_shaped["evidence_class"] = "production_path_observation"
        production_shaped["evidence_profile_sha256"] = (
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_PRODUCTION_EVIDENCE_PROFILE_SHA256
        )
        production_shaped["source_manifest_sha256"] = (
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_SOURCE_MANIFEST_SHA256
        )
        production_shaped["transport_identity_sha256"] = (
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_TRANSPORT_IDENTITY_SHA256
        )
        production_shaped["safety"] = live_preflight_module._safety(
            8,
            production=True,
        )
        validate_live_preflight_report(production_shaped)
        for field, replacement in (
            ("read_only", 1),
            ("endpoint_fetch_attempt_limit", 8.0),
            ("live_network_attested", True),
            ("in_process_tamper_resistant", 0),
        ):
            tampered = copy.deepcopy(production_shaped)
            tampered["safety"][field] = replacement
            with self.subTest(strict_safety_scalar=field):
                with self.assertRaisesRegex(
                    ValueError,
                    "invalid live preflight report",
                ):
                    validate_live_preflight_report(tampered)

    def test_source_contains_no_proxy_or_tls_downgrade(self) -> None:
        module_path = Path(
            __import__(
                "backend.source_monitoring.live_preflight",
                fromlist=["__file__"],
            ).__file__
        )
        source = module_path.read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn("DEFAULT_OFFICIAL_MACRO_FETCH_BYTES", source)
        for forbidden in (
            "ProxyHandler",
            "proxy_bypass",
            "CERT_NONE",
            "_create_unverified_context",
            "check_hostname = False",
            "urlopen(",
        ):
            self.assertNotIn(forbidden, source)

    def test_production_boundary_import_isolated_from_config_and_futu(self) -> None:
        project_root = Path(__file__).parents[1]
        program = textwrap.dedent(
            """
            import http.client
            import json
            import sys
            from unittest import mock

            with mock.patch.object(
                http.client.HTTPConnection,
                "connect",
                side_effect=ConnectionRefusedError("controlled pre-connect stop"),
            ):
                from backend.source_monitoring.live_preflight import (
                    OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION,
                    run_official_source_live_preflight,
                )
                report = run_official_source_live_preflight(
                    confirmation=OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION
                )

            forbidden = (
                "backend.config",
                "backend.market.futu_readonly",
                "backend.source_monitoring.adapters.futu_anomaly",
            )
            print(json.dumps({
                "forbidden_loaded": {
                    name: name in sys.modules for name in forbidden
                },
                "evidence_class": report["evidence_class"],
                "source_manifest_present": bool(
                    report["source_manifest_sha256"]
                ),
                "transport_identity_present": bool(
                    report["transport_identity_sha256"]
                ),
                "transport_mode": report["safety"]["transport_mode"],
                "live_network_attested": report["safety"][
                    "live_network_attested"
                ],
                "in_process_tamper_resistant": report["safety"][
                    "in_process_tamper_resistant"
                ],
                "attempts": report["counts"][
                    "endpoint_fetch_attempt_count"
                ],
                "retries": report["safety"]["retries_performed"],
                "database_writes": report["safety"][
                    "database_writes_performed"
                ],
            }, sort_keys=True))
            """
        )
        with tempfile.TemporaryDirectory(prefix="official-macro-import-") as root:
            temporary_root = Path(root)
            runtime_sentinel = temporary_root / "runtime-must-not-exist"
            environment = os.environ.copy()
            environment.update({
                "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                "AI_STUDIO_RUNTIME_DIR": str(runtime_sentinel),
                "AI_STUDIO_DATABASE_PATH": str(runtime_sentinel / "studio.sqlite3"),
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
            payload = json.loads(completed.stdout)
            self.assertEqual(
                payload["forbidden_loaded"],
                {
                    "backend.config": False,
                    "backend.market.futu_readonly": False,
                    "backend.source_monitoring.adapters.futu_anomaly": False,
                },
            )
            self.assertEqual(
                payload["evidence_class"],
                "production_path_observation",
            )
            self.assertTrue(payload["source_manifest_present"])
            self.assertTrue(payload["transport_identity_present"])
            self.assertEqual(
                payload["transport_mode"],
                "default_official_https_path",
            )
            self.assertFalse(payload["live_network_attested"])
            self.assertFalse(payload["in_process_tamper_resistant"])
            self.assertEqual(payload["attempts"], 8)
            self.assertEqual(payload["retries"], 0)
            self.assertEqual(payload["database_writes"], 0)
            self.assertFalse(runtime_sentinel.exists())

    def test_lazy_packages_preserve_all_public_exports(self) -> None:
        project_root = Path(__file__).parents[1]
        program = textwrap.dedent(
            """
            import json
            import sys
            import backend.market as market
            import backend.source_monitoring.adapters as adapters

            target_modules = (
                "backend.market.earnings_materials",
                "backend.market.futu_readonly",
                "backend.market.industry_proxies",
                "backend.market.storage_service",
                "backend.source_monitoring.adapters.base",
                "backend.source_monitoring.adapters.macro_official",
                "backend.source_monitoring.adapters.futu_anomaly",
            )
            before = {name: name in sys.modules for name in target_modules}
            market_values = {
                name: getattr(market, name) is not None for name in market.__all__
            }
            adapter_values = {
                name: getattr(adapters, name) is not None
                for name in adapters.__all__
            }
            print(json.dumps({
                "before": before,
                "market_all": market.__all__,
                "adapter_all": adapters.__all__,
                "market_values": market_values,
                "adapter_values": adapter_values,
                "market_dir_complete": set(market.__all__) <= set(dir(market)),
                "adapter_dir_complete": set(adapters.__all__) <= set(dir(adapters)),
            }, sort_keys=True))
            """
        )
        with tempfile.TemporaryDirectory(prefix="lazy-package-exports-") as root:
            temporary_root = Path(root)
            environment = os.environ.copy()
            environment.update({
                "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                "AI_STUDIO_RUNTIME_DIR": str(temporary_root / "runtime"),
                "AI_STUDIO_DATABASE_PATH": str(
                    temporary_root / "runtime" / "studio.sqlite3"
                ),
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
        payload = json.loads(completed.stdout)
        self.assertTrue(all(value is False for value in payload["before"].values()))
        self.assertEqual(
            payload["market_all"],
            [
                "FutuUsMarketAdapter",
                "FredIndustryProxyAdapter",
                "OfficialEarningsMaterialsAdapter",
                "STORAGE_MARKET",
                "STORAGE_SYMBOLS",
                "StorageResearchMarketService",
            ],
        )
        self.assertEqual(
            payload["adapter_all"],
            [
                "SOURCE_ADAPTER_CONTRACT_VERSION",
                "MAX_POLL_INTERVAL_MS",
                "MIN_POLL_INTERVAL_MS",
                "SourceAdapter",
                "SourceAdapterContractError",
                "SourceAdapterMetadata",
                "validate_poll_context",
                "validate_source_adapter",
                "BlsReleaseSourceAdapter",
                "FederalReserveSourceAdapter",
                "OfficialMacroCalendarSourceAdapter",
                "TreasuryReleaseSourceAdapter",
                "FUTU_ANOMALY_ADAPTER_KEY",
                "FutuAnomalySourceAdapter",
            ],
        )
        self.assertTrue(all(payload["market_values"].values()))
        self.assertTrue(all(payload["adapter_values"].values()))
        self.assertTrue(payload["market_dir_complete"])
        self.assertTrue(payload["adapter_dir_complete"])


class OfficialSourceLivePreflightCliTests(unittest.TestCase):
    def invoke(self, arguments: list[str], *, runner) -> tuple[int, str, dict]:
        output = io.StringIO()
        exit_code = cli_injected_main(arguments, output=output, runner=runner)
        text = output.getvalue()
        return exit_code, text, json.loads(text)

    def test_public_cli_has_no_runner_injection_parameter(self) -> None:
        self.assertEqual(
            tuple(inspect.signature(cli_main).parameters),
            ("argv", "output"),
        )

    def test_public_cli_requires_isolated_process_before_live_import(self) -> None:
        project_root = Path(__file__).parents[1]
        script_path = (
            project_root / "scripts" / "run_official_source_live_preflight.py"
        )
        with tempfile.TemporaryDirectory(prefix="preflight-isolation-gate-") as root:
            temporary_root = Path(root)
            database_sentinel = temporary_root / "must-not-exist.sqlite3"
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
                    PREFLIGHT_CONFIRMATION,
                ],
                cwd=temporary_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(completed.stderr, "")
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "not_started")
            self.assertEqual(
                payload["error_code"],
                "PREFLIGHT_ISOLATED_PROCESS_REQUIRED",
            )
            self.assertEqual(payload["error_category"], "environment")
            self.assertTrue(payload["safety"]["confirmation_verified"])
            self.assertEqual(
                payload["safety"]["network_requests_performed"],
                0,
            )
            self.assertEqual(
                payload["safety"]["endpoint_fetch_attempts_performed"],
                0,
            )
            self.assertFalse(payload["safety"]["live_network_attested"])
            self.assertFalse(
                payload["safety"]["in_process_tamper_resistant"]
            )
            self.assertFalse(database_sentinel.exists())

    def test_isolated_public_cli_rejects_preloaded_backend_modules(self) -> None:
        project_root = Path(__file__).parents[1]
        program = textwrap.dedent(
            f"""
            import io
            import json
            import sys
            import types

            sys.path.insert(0, {str(project_root)!r})
            from scripts import run_official_source_live_preflight as cli
            sys.modules["backend"] = types.ModuleType("backend")

            output = io.StringIO()
            exit_code = cli.main(
                ["--confirm", cli.PREFLIGHT_CONFIRMATION],
                output=output,
            )
            print(json.dumps({{
                "exit_code": exit_code,
                "payload": json.loads(output.getvalue()),
            }}, sort_keys=True))
            """
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", program],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        result = json.loads(completed.stdout)
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["payload"]["status"], "indeterminate")
        self.assertEqual(
            result["payload"]["error_code"],
            "PREFLIGHT_BACKEND_PRELOADED",
        )
        self.assertEqual(result["payload"]["error_category"], "environment")
        self.assertIsNone(
            result["payload"]["safety"]["network_requests_performed"]
        )

    def test_help_bad_arguments_and_wrong_confirmation_are_zero_action(self) -> None:
        calls: list[str] = []

        def forbidden_runner(_confirmation: str) -> dict:
            calls.append("runner")
            raise AssertionError("live runner must remain closed")

        cases = (
            (["--help"], 0, "help", ""),
            ([], 2, "not_started", "PREFLIGHT_CONFIRMATION_REQUIRED"),
            (["--confirm", "wrong"], 2, "not_started", "PREFLIGHT_CONFIRMATION_REQUIRED"),
            (["--unknown"], 2, "not_started", "PREFLIGHT_ARGUMENTS_INVALID"),
            (["--confirm"], 2, "not_started", "PREFLIGHT_ARGUMENTS_INVALID"),
            (
                ["--confirm", PREFLIGHT_CONFIRMATION, "--confirm", PREFLIGHT_CONFIRMATION],
                2,
                "not_started",
                "PREFLIGHT_ARGUMENTS_INVALID",
            ),
        )
        for arguments, expected_exit, status, error_code in cases:
            with self.subTest(arguments=arguments):
                with (
                    mock.patch(
                        "builtins.open",
                        side_effect=AssertionError("file open"),
                    ),
                    mock.patch.object(
                        Path,
                        "write_text",
                        side_effect=AssertionError("file write"),
                    ),
                    mock.patch.object(
                        Path,
                        "write_bytes",
                        side_effect=AssertionError("file write"),
                    ),
                ):
                    exit_code, text, payload = self.invoke(
                        list(arguments),
                        runner=forbidden_runner,
                    )
                self.assertEqual(exit_code, expected_exit)
                self.assertLess(
                    len(text.encode("ascii")),
                    OFFICIAL_SOURCE_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES,
                )
                self.assertEqual(payload["status"], status)
                self.assertEqual(payload["scope"], "official_macro_only")
                self.assertFalse(payload["sec_included"])
                self.assertFalse(payload["company_ir_included"])
                if error_code:
                    self.assertEqual(payload["error_code"], error_code)
                self.assertEqual(payload["safety"]["network_requests_performed"], 0)
                self.assertEqual(
                    payload["safety"]["endpoint_fetch_attempts_performed"],
                    0,
                )
                self.assertEqual(payload["safety"]["database_reads_performed"], 0)
                self.assertEqual(payload["safety"]["database_writes_performed"], 0)
                self.assertEqual(
                    payload["safety"]["application_file_writes_performed"],
                    0,
                )
        self.assertEqual(calls, [])

    def test_cli_rejects_injected_report_after_exact_confirmation(self) -> None:
        report = _run_official_source_live_preflight_injected(
            confirmation=OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION,
            dependencies=_dependencies(FixtureFetcher()),
        )
        confirmations: list[str] = []

        def runner(confirmation: str) -> dict:
            confirmations.append(confirmation)
            return report

        exit_code, text, payload = self.invoke(
            ["--confirm", PREFLIGHT_CONFIRMATION],
            runner=runner,
        )
        self.assertEqual(exit_code, 2)
        self.assertEqual(confirmations, [OFFICIAL_SOURCE_LIVE_PREFLIGHT_CONFIRMATION])
        self.assertEqual(payload["status"], "indeterminate")
        self.assertEqual(payload["error_code"], "PREFLIGHT_PRODUCTION_REQUIRED")
        self.assertIsNone(payload["safety"]["database_writes_performed"])
        self.assertEqual(payload["safety"]["execution_capability"], "unknown")
        self.assertEqual(text.count("\n"), 1)

    def test_untrusted_runner_output_and_exception_are_generic_and_redacted(self) -> None:
        for runner in (
            lambda _confirmation: {"SECRET_URL": "https://secret.invalid/"},
            lambda _confirmation: (_ for _ in ()).throw(
                RuntimeError("SECRET_PROXY_PASSWORD")
            ),
        ):
            with self.subTest(runner=runner):
                exit_code, text, payload = self.invoke(
                    ["--confirm", PREFLIGHT_CONFIRMATION],
                    runner=runner,
                )
                self.assertEqual(exit_code, 1)
                self.assertEqual(payload["status"], "indeterminate")
                self.assertEqual(payload["error_code"], "PREFLIGHT_INTERNAL_ERROR")
                self.assertIsNone(payload["safety"]["network_requests_performed"])
                self.assertEqual(
                    payload["safety"]["endpoint_fetch_attempts_accounting"],
                    "unknown",
                )
                for field in (
                    "application_file_writes_performed",
                    "database_reads_performed",
                    "database_writes_performed",
                    "provider_calls_performed",
                    "futu_calls_performed",
                    "live_trading_allowed",
                ):
                    self.assertIsNone(payload["safety"][field])
                self.assertEqual(
                    payload["safety"]["execution_capability"],
                    "unknown",
                )
                self.assertNotIn("SECRET", text)
                self.assertNotIn("https://", text)

    def test_output_bound_falls_back_to_fixed_error(self) -> None:
        encoded = cli_module._bounded_json({
            "untrusted": "SECRET" * OFFICIAL_SOURCE_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES,
        })
        payload = json.loads(encoded)
        self.assertLess(
            len(encoded.encode("ascii")),
            OFFICIAL_SOURCE_LIVE_PREFLIGHT_MAX_OUTPUT_BYTES,
        )
        self.assertEqual(
            payload["error_code"],
            "PREFLIGHT_OUTPUT_BOUND_EXCEEDED",
        )
        self.assertEqual(payload["status"], "indeterminate")
        self.assertNotIn("SECRET", encoded)

    def test_help_and_missing_confirmation_are_clean_in_fresh_processes(self) -> None:
        project_root = Path(__file__).parents[1]
        script_path = (
            project_root / "scripts" / "run_official_source_live_preflight.py"
        )

        def pyc_snapshot() -> dict[str, tuple[int, int]]:
            names = (
                "run_official_source_live_preflight*.pyc",
                "live_preflight*.pyc",
            )
            paths = [
                path
                for pattern in names
                for path in project_root.rglob(pattern)
            ]
            return {
                str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                for path in paths
            }

        before_pyc = pyc_snapshot()
        with tempfile.TemporaryDirectory(prefix="official-macro-preflight-cli-") as root:
            temporary_root = Path(root)
            database_sentinel = temporary_root / "must-not-exist.sqlite3"
            environment = os.environ.copy()
            environment.update({
                "AI_STUDIO_SKIP_LOCAL_ENV": "1",
                "AI_STUDIO_DATABASE_PATH": str(database_sentinel),
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1",
            })
            cases = (("--help",), ())
            for arguments in cases:
                with self.subTest(arguments=arguments):
                    completed = subprocess.run(
                        [sys.executable, str(script_path), *arguments],
                        cwd=temporary_root,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    self.assertIn(completed.returncode, {0, 2})
                    self.assertEqual(completed.stderr, "")
                    payload = json.loads(completed.stdout)
                    self.assertIn(payload["status"], {"help", "not_started"})
                    self.assertEqual(
                        payload["safety"]["network_requests_performed"],
                        0,
                    )
                    self.assertFalse(database_sentinel.exists())
        self.assertEqual(before_pyc, pyc_snapshot())

    def test_script_has_no_top_level_backend_import(self) -> None:
        script_path = (
            Path(__file__).parents[1]
            / "scripts"
            / "run_official_source_live_preflight.py"
        )
        tree = ast.parse(script_path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("backend"))
            if isinstance(node, ast.Import):
                self.assertTrue(
                    all(not alias.name.startswith("backend") for alias in node.names)
                )


if __name__ == "__main__":
    unittest.main()
