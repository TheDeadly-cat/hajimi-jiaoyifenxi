"""Offline tests for the closeout receipt contract audit (X-07).

These run entirely inside temporary directories.  They analyse the bound
source statically and build synthetic receipt trees; they never touch a real
trial root, a database, the network, a credential or a model.

The point of most assertions is state separation.  A derived pattern that has
no bound-source writer, or whose output path comes from a command line, must be
reported UNVERIFIABLE -- never as absent, and never as evidence that some
historical code did not execute.
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from scripts.closeout_receipt_contract_audit import (  # noqa: E402
    NON_IMPLICATION,
    VERSION,
    ContractExtractionError,
    _incompleteness_reasons,
    _is_discriminating,
    _pattern_to_regex,
    audit,
    build_report,
    extract_contracts,
    list_scope,
)
import scripts.closeout_receipt_contract_audit as x07  # noqa: E402

SOURCE = APP
TOOL = Path(__file__).resolve().parents[1] / "scripts" / "closeout_receipt_contract_audit.py"


class SyntheticSourceMixin:
    """Build a tiny throwaway source tree inside a temporary directory."""

    def write_module(self, rel: str, body: str) -> None:
        path = Path(self.src) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8", newline="\n")


class ContractExtractionTests(SyntheticSourceMixin, unittest.TestCase):
    def test_bound_source_yields_stop_receipt_pattern_with_contract_source(self):
        contracts = extract_contracts(str(SOURCE))
        stops = [c for c in contracts if c["name_pattern"] == "stop-{*}-{*}.json"]
        self.assertTrue(stops, "the bound stop receipt pattern must be derivable")
        self.assertEqual(stops[0]["contract_source"] if "contract_source" in stops[0] else stops[0]["source"],
                         "backend/news_review_stop.py:47")
        self.assertEqual(stops[0]["writer_func"], "write_record")
        self.assertEqual(stops[0]["resolution"], "partial_dynamic")
        self.assertEqual(stops[0]["expected_base"], "trial_root")
        self.assertIn("news_review_stop.py:19", stops[0]["base_evidence"])

    def test_bound_source_yields_observer_lifecycle_patterns(self):
        contracts = extract_contracts(str(SOURCE))
        # Assert by (pattern, source file, expected base), not by an exact line
        # number: the contract lives wherever the writer call currently sits, so
        # a refactor (for example moving the exit publish into a shared helper)
        # must not silently break derivation.  Line numbers are brittle; the
        # file + base + pattern triple is the durable contract.
        by_pattern = collections.defaultdict(set)
        for c in contracts:
            if c["name_pattern"]:
                source_file = c["source"].split(":")[0]
                by_pattern[c["name_pattern"]].add((source_file, c["expected_base"]))
        for pattern, expected_file_base in (
            ("observer-exit.json", ("scripts/run_news_review_observer.py", "monitoring")),
            ("observer-drain-start.json", ("scripts/run_news_review_observer.py", "monitoring")),
            ("observer-drain-execution-{*}.json", ("scripts/run_news_review_observer.py", "monitoring")),
            ("host-{*}.json", ("scripts/run_news_review_trial.py", "trial_root")),
        ):
            self.assertIn(pattern, by_pattern, f"{pattern} must be derived from the bound source")
            self.assertIn(expected_file_base, by_pattern[pattern],
                          f"{pattern} must derive from {expected_file_base}")

    def test_fallback_receipt_pattern_is_derived_from_bound_source(self):
        # X-03 moved the exit publish into publish_exit_receipt, which also
        # writes a fallback receipt.  The tool must derive that name family too,
        # proving it tracks the real writer rather than a hardcoded list.
        contracts = extract_contracts(str(SOURCE))
        fallbacks = [c for c in contracts
                     if c["name_pattern"] and "observer-exit-fallback" in c["name_pattern"]]
        self.assertTrue(fallbacks, "the fallback receipt pattern must be derivable")
        contract = fallbacks[0]
        # R-01 added a second branch (an unidentified name used when the native
        # creation time is unavailable).  Both branches are statically known, so
        # the tool reports them together instead of collapsing to one guess.
        self.assertEqual(contract["resolution"], "conditional_complete")
        self.assertEqual(sorted(contract["alternatives"]),
                         sorted(["observer-exit-fallback-{*}-{*}.json",
                                 "observer-exit-fallback-unidentified-{*}.json"]))
        # Neither branch may be mistaken for a command-line supplied path.
        self.assertFalse(contract["cli_tainted"])
        self.assertEqual(contract["expected_base"], "monitoring")

    def test_cli_supplied_output_is_never_guessed(self):
        contracts = extract_contracts(str(SOURCE))
        cli = [c for c in contracts if c["expected_base"] == "cli_supplied"]
        self.assertTrue(cli, "at least one --output path must be recognised as CLI supplied")

        # Every CLI-supplied contract must trace its unknowability to the
        # command line, never to a guessed literal name.
        for contract in cli:
            self.assertTrue(contract["cli_tainted"], contract["source"])
            self.assertIn("command-line argument", contract["base_evidence"])
            self.assertIsNone(contract["name_pattern"], contract["source"])

        # The trial host report path specifically must be among them.
        trial_sources = {c["source"] for c in cli}
        self.assertTrue(
            any(s.startswith("scripts/run_news_review_trial.py:") for s in trial_sources),
            "run_news_review_trial --output must be recognised as CLI supplied",
        )

    def test_non_product_sources_are_excluded_by_default(self):
        included = extract_contracts(str(SOURCE))
        everything = extract_contracts(str(SOURCE), include_non_product=True)
        self.assertLess(len(included), len(everything))
        for contract in included:
            self.assertTrue(contract["product_code"])
            self.assertFalse(contract["source"].startswith("tests/"))

    def test_unreadable_source_root_is_reported_not_silently_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "does-not-exist")
            with self.assertRaises(ContractExtractionError):
                extract_contracts(missing)

    def test_parse_failure_is_recorded_as_a_contract_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.src = tmp
            self.write_module("broken.py", "def f(:\n    pass\n")
            contracts = extract_contracts(tmp)
            self.assertEqual(len(contracts), 1)
            self.assertEqual(contracts[0]["resolution"], "unresolved")
            self.assertIn("source_parse_failed", contracts[0]["reason"])


class StateSeparationTests(SyntheticSourceMixin, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = os.path.join(self._tmp.name, "src")
        self.trial_root = os.path.join(self._tmp.name, "TrialRoot")
        self.monitoring = os.path.join(self.trial_root, "monitoring")
        os.makedirs(self.monitoring)

        self.write_module("backend/writers.py", """
            from .sink import write_record, publish_json_once

            def emit(root, directory, session, n, args):
                write_record(root/f'stop-{session}-{n}.json', {})
                write_record(root/'activation-policy.json', {})
                publish_json_once(directory/'observer-exit.json', {})
                publish_json_once(directory/f'observer-drain-execution-{n:06d}.json', {})
                write_record(args.output, {})
        """)
        self.contracts = extract_contracts(self.src)
        # The synthetic tree has no run_news_review_trial.py, so pin the base
        # semantics the way the real tool derives them.  A CLI-tainted name is
        # already marked cli_supplied by the extractor and is left as-is.
        for contract in self.contracts:
            if contract["cli_tainted"]:
                contract["expected_base"] = "cli_supplied"
            elif contract["name_pattern"] is None:
                contract["expected_base"] = "cli_supplied"
            elif contract["base_expr"] == "root":
                contract["expected_base"] = "trial_root"
            elif contract["base_expr"] == "directory":
                contract["expected_base"] = "monitoring"

    def scopes(self):
        return {
            "trial_root": list_scope(self.trial_root, recursive=False),
            "monitoring": list_scope(self.monitoring, recursive=False),
        }

    def find(self, findings, pattern):
        matches = [r for r in findings["derived_contracts"] if r["name_pattern"] == pattern]
        self.assertEqual(len(matches), 1, f"expected exactly one entry for {pattern}")
        return matches[0]

    def test_absent_and_exists_are_separate_states(self):
        findings = audit(contracts=self.contracts, scopes=self.scopes(), requested_names=[])
        absent = self.find(findings, "stop-{*}-{*}.json")
        self.assertEqual(absent["status"], "ABSENT_IN_CHECKED_SCOPE")
        self.assertEqual(absent["matched_count"], 0)
        self.assertEqual(absent["non_implication"], NON_IMPLICATION)

        Path(self.trial_root, "stop-bdbc268c-1.json").write_text("{}", encoding="utf-8")
        found = self.find(
            audit(contracts=self.contracts, scopes=self.scopes(), requested_names=[]),
            "stop-{*}-{*}.json",
        )
        self.assertEqual(found["status"], "EXISTS")
        self.assertEqual(found["matched_files"], ["stop-bdbc268c-1.json"])
        self.assertEqual(found["non_implication"], "")

    def test_cli_supplied_stays_unverifiable_even_when_files_exist(self):
        Path(self.trial_root, "report-abc123.json").write_text("{}", encoding="utf-8")
        Path(self.trial_root, "window-report.json").write_text("{}", encoding="utf-8")
        findings = audit(contracts=self.contracts, scopes=self.scopes(), requested_names=[])
        unresolved = [r for r in findings["derived_contracts"] if r["status"] == "UNVERIFIABLE"]
        self.assertTrue(unresolved)
        self.assertEqual(unresolved[0]["reason"], "output_path_cli_supplied")

    def test_unlisted_base_is_unverifiable_not_absent(self):
        findings = audit(
            contracts=self.contracts,
            scopes={"trial_root": list_scope(self.trial_root, recursive=False)},
            requested_names=[],
        )
        entry = self.find(findings, "observer-exit.json")
        self.assertEqual(entry["status"], "UNVERIFIABLE")
        self.assertIn("expected_base_not_in_checked_scope:monitoring", entry["reason"])
        self.assertNotEqual(entry["status"], "ABSENT_IN_CHECKED_SCOPE")

    def test_unreadable_scope_is_unverifiable(self):
        missing = os.path.join(self._tmp.name, "never-created")
        findings = audit(
            contracts=self.contracts,
            scopes={
                "trial_root": list_scope(self.trial_root, recursive=False),
                "monitoring": list_scope(missing, recursive=False),
            },
            requested_names=[],
        )
        entry = self.find(findings, "observer-exit.json")
        self.assertEqual(entry["status"], "UNVERIFIABLE")
        self.assertIn("directory_not_found", entry["reason"])

    def test_absent_state_never_claims_the_writer_did_not_run(self):
        findings = audit(contracts=self.contracts, scopes=self.scopes(), requested_names=[])
        for entry in findings["derived_contracts"]:
            if entry["status"] == "ABSENT_IN_CHECKED_SCOPE":
                self.assertTrue(entry["non_implication"])
                self.assertNotIn("did not execute", entry["reason"])
                self.assertNotIn("never", entry["reason"].lower())


class RequestedNameTests(SyntheticSourceMixin, unittest.TestCase):
    """The defect found in the 2026-09-30 closeout checklist, encoded as tests."""

    LEGACY_REQUESTED = [
        "launcher-exit.json",
        "stop-request.json",
        "stop-receipt.json",
        "observer-drain-receipt.json",
        "window-report.json",
        r"monitoring\observer-exit.json",
    ]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = os.path.join(self._tmp.name, "src")
        self.trial_root = os.path.join(self._tmp.name, "TrialRoot")
        self.monitoring = os.path.join(self.trial_root, "monitoring")
        os.makedirs(self.monitoring)
        self.write_module("backend/writers.py", """
            from .sink import write_record, publish_json_once

            def emit(root, directory, session, n):
                write_record(root/f'stop-{session}-{n}.json', {})
                publish_json_once(directory/'observer-exit.json', {})
        """)
        self.contracts = extract_contracts(self.src)
        for contract in self.contracts:
            if contract["base_expr"] == "root":
                contract["expected_base"] = "trial_root"
            elif contract["base_expr"] == "directory":
                contract["expected_base"] = "monitoring"

    def scopes(self):
        return {
            "trial_root": list_scope(self.trial_root, recursive=False),
            "monitoring": list_scope(self.monitoring, recursive=False),
        }

    def test_names_without_a_writer_contract_are_unverifiable(self):
        requested = [{"name": n, "origin": "legacy_closeout_checklist"} for n in self.LEGACY_REQUESTED]
        findings = audit(contracts=self.contracts, scopes=self.scopes(), requested_names=requested)
        by_name = {r["requested_name"]: r for r in findings["requested_names"]}

        for name in ("stop-request.json", "stop-receipt.json", "observer-drain-receipt.json",
                     "launcher-exit.json", "window-report.json"):
            self.assertEqual(by_name[name]["status"], "UNVERIFIABLE", name)
            self.assertEqual(by_name[name]["reason"], "writer_contract_not_found_in_bound_source", name)
            self.assertNotEqual(by_name[name]["status"], "ABSENT_IN_CHECKED_SCOPE")

        # The one legacy name that does match a bound writer is checkable.
        exit_entry = by_name[r"monitoring\observer-exit.json"]
        self.assertEqual(exit_entry["status"], "ABSENT_IN_CHECKED_SCOPE")
        self.assertEqual(exit_entry["contract_source"], "backend/writers.py:5")

    def test_only_derivable_names_can_support_an_absent_finding(self):
        requested = [{"name": n, "expected_base": "trial_root"} for n in self.LEGACY_REQUESTED]
        findings = audit(contracts=self.contracts, scopes=self.scopes(), requested_names=requested)
        absent = [r for r in findings["requested_names"] if r["status"] == "ABSENT_IN_CHECKED_SCOPE"]
        for entry in absent:
            self.assertTrue(entry["contract_source"], "an absent finding must cite a writer contract")

    def test_requested_bare_name_without_base_is_unverifiable(self):
        # A bare name with no explicit base: this tool must not pick a
        # directory for it, because that choice would be a guess.
        requested = [{"name": "observer-exit.json", "expected_base": ""}]
        findings = audit(contracts=self.contracts, scopes=self.scopes(), requested_names=requested)
        entry = findings["requested_names"][0]
        self.assertEqual(entry["status"], "UNVERIFIABLE")
        self.assertEqual(entry["reason"], "expected_base_not_determined_for_bare_name")
        self.assertNotEqual(entry["status"], "ABSENT_IN_CHECKED_SCOPE")

    def test_requested_name_with_explicit_unlisted_base_is_unverifiable(self):
        requested = [{"name": "observer-exit.json", "expected_base": "not_a_checked_scope"}]
        findings = audit(contracts=self.contracts, scopes=self.scopes(), requested_names=requested)
        entry = findings["requested_names"][0]
        self.assertEqual(entry["status"], "UNVERIFIABLE")
        self.assertEqual(entry["reason"], "explicit_base_not_in_checked_scope:not_a_checked_scope")

    def test_requested_name_directory_prefix_honoured_when_listed(self):
        # The directory is part of the request, not an inference by this tool.
        requested = [{"name": r"monitoring\observer-exit.json"}]
        findings = audit(contracts=self.contracts, scopes=self.scopes(), requested_names=requested)
        entry = findings["requested_names"][0]
        self.assertEqual(entry["status"], "ABSENT_IN_CHECKED_SCOPE")
        self.assertEqual(entry["resolved_scope"], "monitoring")
        self.assertTrue(entry["contract_source"])

    def test_requested_name_directory_prefix_unlisted_is_unverifiable(self):
        requested = [{"name": r"elsewhere\observer-exit.json"}]
        findings = audit(contracts=self.contracts, scopes=self.scopes(), requested_names=requested)
        entry = findings["requested_names"][0]
        self.assertEqual(entry["status"], "UNVERIFIABLE")
        self.assertEqual(entry["reason"], "name_directory_not_in_checked_scope:elsewhere")

    def test_requested_name_exists_when_the_file_is_present(self):
        Path(self.monitoring, "observer-exit.json").write_text("{}", encoding="utf-8")
        requested = [{"name": "observer-exit.json", "expected_base": "monitoring"}]
        findings = audit(contracts=self.contracts, scopes=self.scopes(), requested_names=requested)
        entry = findings["requested_names"][0]
        self.assertEqual(entry["status"], "EXISTS")
        self.assertEqual(entry["matched_files"], ["observer-exit.json"])
        self.assertEqual(entry["non_implication"], "")


class ReportShapeTests(SyntheticSourceMixin, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.trial_root = os.path.join(self._tmp.name, "TrialRoot")
        self.monitoring = os.path.join(self.trial_root, "monitoring")
        os.makedirs(self.monitoring)
        Path(self.monitoring, "observer-exit.json").write_text("{}", encoding="utf-8")
        Path(self.monitoring, "observer-drain-execution-000001.json").write_text("{}", encoding="utf-8")

    def test_report_does_not_reproduce_directory_listings(self):
        report = build_report(
            source_root=str(SOURCE),
            scopes={
                "trial_root": {"path": self.trial_root, "recursive": False},
                "monitoring": {"path": self.monitoring, "recursive": False},
            },
            requested_names=[],
            candidate_sha="300855a5c971f1179b59bba5097741792d9898aa",
            notes=["synthetic scope"],
        )
        self.assertEqual(report["version"], VERSION)
        self.assertEqual(report["checked_scope"]["names_retained_in_report"], False)
        payload = json.dumps(report, ensure_ascii=False)
        for scope in report["checked_scope"]["directories"]:
            self.assertNotIn('"names"', json.dumps(scope))
        self.assertNotIn("activation-proposal", payload)
        self.assertEqual(report["source_scope"]["candidate_sha_basis"],
                         "caller_supplied_not_independently_verified")
        self.assertEqual(report["non_implication"], NON_IMPLICATION)
        self.assertIn("synthetic scope", report["limitations"])

    def test_derived_state_counts_are_consistent(self):
        report = build_report(
            source_root=str(SOURCE),
            scopes={
                "trial_root": {"path": self.trial_root, "recursive": False},
                "monitoring": {"path": self.monitoring, "recursive": False},
            },
            requested_names=[],
            candidate_sha="UNKNOWN",
            notes=[],
        )
        total = sum(report["status_counts"].values())
        self.assertEqual(total, len(report["derived_contracts"]))
        allowed = {"EXISTS", "ABSENT_IN_CHECKED_SCOPE", "UNVERIFIABLE"}
        self.assertEqual(set(report["status_counts"]), set(report["status_counts"]) & allowed)
        for entry in report["derived_contracts"]:
            self.assertIn(entry["status"], allowed)
            self.assertIn(entry["base_location_basis"],
                          {"asserted_with_source_reference", "unknown"})

    def test_real_patterns_are_found_in_the_synthetic_monitoring_scope(self):
        report = build_report(
            source_root=str(SOURCE),
            scopes={
                "trial_root": {"path": self.trial_root, "recursive": False},
                "monitoring": {"path": self.monitoring, "recursive": False},
            },
            requested_names=[],
            candidate_sha="UNKNOWN",
            notes=[],
        )
        by_pattern = {r["name_pattern"]: r for r in report["derived_contracts"]}
        self.assertEqual(by_pattern["observer-exit.json"]["status"], "EXISTS")
        self.assertEqual(by_pattern["observer-drain-execution-{*}.json"]["status"], "EXISTS")
        # Nothing was placed in trial_root, so the stop pattern must be absent
        # in scope -- with the non-implication text attached.
        stop = by_pattern["stop-{*}-{*}.json"]
        self.assertEqual(stop["status"], "ABSENT_IN_CHECKED_SCOPE")
        self.assertEqual(stop["non_implication"], NON_IMPLICATION)


class PatternMatchingTests(unittest.TestCase):
    def test_wildcard_segment_requires_at_least_one_character(self):
        regex = _pattern_to_regex("stop-{*}-{*}.json")
        self.assertIsNotNone(regex.match("stop-bdbc268c912c-1.json"))
        self.assertIsNone(regex.match("stop--1.json"))
        self.assertIsNone(regex.match("stop-bdbc-1.json.bak"))

    def test_literal_pattern_does_not_match_siblings(self):
        regex = _pattern_to_regex("observer-exit.json")
        self.assertIsNotNone(regex.match("observer-exit.json"))
        self.assertIsNone(regex.match("observer-exit-fallback-9232-639.json"))
        self.assertIsNone(regex.match("xobserver-exit.json"))

    def test_regex_metacharacters_in_names_are_escaped(self):
        regex = _pattern_to_regex("monitor-execution-{*}.json")
        self.assertIsNotNone(regex.match("monitor-execution-000258.json"))
        self.assertIsNone(regex.match("monitor-executionX000258.json"))


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.trial_root = os.path.join(self._tmp.name, "TrialRoot")
        self.monitoring = os.path.join(self.trial_root, "monitoring")
        os.makedirs(self.monitoring)

    def run_tool(self, *extra):
        out = os.path.join(self._tmp.name, "audit.json")
        result = subprocess.run(
            [sys.executable, str(TOOL), "--source", str(SOURCE),
             "--scope", f"trial_root={self.trial_root}",
             "--scope", f"monitoring={self.monitoring}",
             "--requested-name", "stop-request.json|origin=legacy_checklist",
             "--json-out", out, *extra],
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result, out

    def test_summary_is_printed_and_report_is_written_once(self):
        result, out = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(summary["version"], VERSION)
        self.assertEqual(summary["unverifiable_requested"], 1)
        self.assertTrue(os.path.isfile(out))
        report = json.loads(Path(out).read_text(encoding="utf-8"))
        self.assertEqual(report["status_counts"], summary["status_counts"])

    def test_existing_output_is_never_overwritten(self):
        out = os.path.join(self._tmp.name, "audit.json")
        Path(out).write_text("PRE-EXISTING EVIDENCE", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(TOOL), "--source", str(SOURCE),
             "--scope", f"trial_root={self.trial_root}", "--json-out", out],
            capture_output=True, text=True, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("json_out_already_exists", result.stderr)
        self.assertEqual(Path(out).read_text(encoding="utf-8"), "PRE-EXISTING EVIDENCE")

    def test_tool_performs_no_network_or_database_access(self):
        result, out = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        text = Path(out).read_text(encoding="utf-8")
        for forbidden in ("sqlite", "studio.sqlite3", "http://", "https://", "api_key"):
            self.assertNotIn(forbidden, text.lower() if forbidden.islower() else text)


class CounterExampleRegressionTests(SyntheticSourceMixin, unittest.TestCase):
    """The six synthetic counter-examples from the 2026-10-06 review.

    Each one previously produced a wrong state; these lock the corrected
    behaviour.  They run on synthetic source and temporary receipt trees only,
    and never touch the historic B run root.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = os.path.join(self._tmp.name, "src")
        self.trial_root = os.path.join(self._tmp.name, "TrialRoot")
        self.monitoring = os.path.join(self.trial_root, "monitoring")
        os.makedirs(self.monitoring)
        self.write_module("backend/writers.py", """
            from .sink import write_record, publish_json_once

            def emit(root, directory, session, n):
                write_record(root/f'stop-{session}-{n}.json', {})
                publish_json_once(directory/'observer-exit.json', {})
        """)
        self.contracts = extract_contracts(self.src)
        for contract in self.contracts:
            if contract["base_expr"] == "root":
                contract["expected_base"] = "trial_root"
            elif contract["base_expr"] == "directory":
                contract["expected_base"] = "monitoring"

    def scopes(self, *, recursive=False):
        return {
            "trial_root": list_scope(self.trial_root, recursive=recursive),
            "monitoring": list_scope(self.monitoring, recursive=recursive),
        }

    def derived(self, findings, pattern):
        hits = [r for r in findings["derived_contracts"] if r["name_pattern"] == pattern]
        self.assertEqual(len(hits), 1, f"expected exactly one derived entry for {pattern}")
        return hits[0]

    def cap(self, value):
        self.addCleanup(setattr, x07, "MAX_LISTING_PER_DIR", x07.MAX_LISTING_PER_DIR)
        x07.MAX_LISTING_PER_DIR = value

    # --- counter-example 1: monitoring directory does not exist -------------
    def test_missing_scope_requested_name_is_unverifiable_not_absent(self):
        absent_monitoring = os.path.join(self._tmp.name, "never-created")
        findings = audit(
            contracts=self.contracts,
            scopes={
                "trial_root": list_scope(self.trial_root, recursive=False),
                "monitoring": list_scope(absent_monitoring, recursive=False),
            },
            requested_names=[{"name": r"monitoring\observer-exit.json"}],
        )
        requested = findings["requested_names"][0]
        derived = self.derived(findings, "observer-exit.json")
        # Derived and requested must agree: both UNVERIFIABLE, never ABSENT.
        self.assertEqual(derived["status"], "UNVERIFIABLE")
        self.assertEqual(requested["status"], "UNVERIFIABLE")
        self.assertIn("directory_not_found", requested["reason"])
        self.assertNotEqual(requested["status"], "ABSENT_IN_CHECKED_SCOPE")

    # --- counter-example 2: requested base conflicts with the writer's base --
    def test_requested_base_conflicting_with_writer_is_unverifiable(self):
        Path(self.monitoring, "observer-exit.json").write_text("{}", encoding="utf-8")
        findings = audit(
            contracts=self.contracts,
            scopes=self.scopes(),
            requested_names=[{"name": "observer-exit.json", "expected_base": "trial_root"}],
        )
        # The correct base still verifies positively...
        derived = self.derived(findings, "observer-exit.json")
        self.assertEqual(derived["status"], "EXISTS")
        self.assertEqual(derived["expected_base"], "monitoring")
        # ...but a request naming a different base is a conflict, not a miss.
        requested = findings["requested_names"][0]
        self.assertEqual(requested["status"], "UNVERIFIABLE")
        self.assertIn("requested_base_conflicts_with_writer_base", requested["reason"])
        self.assertNotEqual(requested["status"], "ABSENT_IN_CHECKED_SCOPE")

    def test_writers_with_divergent_bases_are_ambiguous_not_merged(self):
        self.write_module("backend/writers2.py", """
            from .sink import publish_json_once

            def emit_elsewhere(root, directory):
                publish_json_once(root/'observer-exit.json', {})
        """)
        contracts = extract_contracts(self.src)
        for contract in contracts:
            if contract["base_expr"] == "root":
                contract["expected_base"] = "trial_root"
            elif contract["base_expr"] == "directory":
                contract["expected_base"] = "monitoring"
        findings = audit(
            contracts=contracts,
            scopes=self.scopes(),
            requested_names=[{"name": "observer-exit.json", "expected_base": "monitoring"}],
        )
        requested = findings["requested_names"][0]
        self.assertEqual(requested["status"], "UNVERIFIABLE")
        self.assertIn("writer_bases_ambiguous", requested["reason"])

    # --- counter-example 3: truncated listing, target beyond the cap --------
    def test_truncated_listing_dynamic_target_beyond_cap_is_unverifiable(self):
        self.cap(1)
        # 'a-first.json' sorts ahead of the stop receipt, so with a cap of 1 the
        # stop file is real but sits in the un-listed remainder.  Recursive mode
        # sorts filenames during the walk, making which entry the cap keeps
        # deterministic (scandir order in non-recursive mode is not).
        Path(self.trial_root, "a-first.json").write_text("{}", encoding="utf-8")
        Path(self.trial_root, "stop-bdbc-1.json").write_text("{}", encoding="utf-8")
        findings = audit(
            contracts=self.contracts, scopes=self.scopes(recursive=True), requested_names=[]
        )
        entry = self.derived(findings, "stop-{*}-{*}.json")
        self.assertEqual(entry["status"], "UNVERIFIABLE")
        self.assertIn("listing_truncated", entry["reason"])
        self.assertNotEqual(entry["status"], "ABSENT_IN_CHECKED_SCOPE")

    def test_truncated_listing_absent_requested_name_is_unverifiable(self):
        self.cap(1)
        Path(self.monitoring, "a-first.json").write_text("{}", encoding="utf-8")
        Path(self.monitoring, "b-second.json").write_text("{}", encoding="utf-8")
        findings = audit(
            contracts=self.contracts,
            scopes=self.scopes(recursive=True),
            requested_names=[{"name": "observer-exit.json", "expected_base": "monitoring"}],
        )
        requested = findings["requested_names"][0]
        self.assertEqual(requested["status"], "UNVERIFIABLE")
        self.assertIn("listing_truncated", requested["reason"])

    def test_exact_probeable_target_still_exists_under_truncation(self):
        # Requirement 3: a directly probed exact path is positive evidence even
        # when another region of the same scope was truncated.
        self.cap(1)
        Path(self.monitoring, "a-first.json").write_text("{}", encoding="utf-8")
        Path(self.monitoring, "observer-exit.json").write_text("{}", encoding="utf-8")
        findings = audit(
            contracts=self.contracts,
            scopes=self.scopes(recursive=True),
            requested_names=[{"name": "observer-exit.json", "expected_base": "monitoring"}],
        )
        self.assertEqual(self.derived(findings, "observer-exit.json")["status"], "EXISTS")
        self.assertEqual(findings["requested_names"][0]["status"], "EXISTS")

    # --- counter-example 4: same-named file in a subdirectory ---------------
    def test_subdirectory_same_name_does_not_satisfy_root_contract(self):
        child = os.path.join(self.monitoring, "child")
        os.makedirs(child)
        Path(child, "observer-exit.json").write_text("{}", encoding="utf-8")
        findings = audit(
            contracts=self.contracts,
            scopes=self.scopes(recursive=True),
            requested_names=[],
        )
        entry = self.derived(findings, "observer-exit.json")
        self.assertNotEqual(entry["status"], "EXISTS")
        self.assertEqual(entry["status"], "ABSENT_IN_CHECKED_SCOPE")
        self.assertNotIn("child/observer-exit.json", entry["matched_files"])
        self.assertEqual(entry["matched_files"], [])

    # --- counter-example 5: f-string embedding args.output ------------------
    def test_fstring_embedding_cli_argument_stays_unverifiable(self):
        self.write_module("backend/cli_out.py", """
            from .sink import write_record

            def emit(directory, args):
                write_record(directory/f'{args.output}.json', {})
        """)
        contracts = extract_contracts(self.src)
        for contract in contracts:
            if contract["base_expr"] == "directory":
                contract["expected_base"] = "monitoring"
        cli = [c for c in contracts if c["source"].endswith("cli_out.py:4")]
        self.assertEqual(len(cli), 1)
        self.assertTrue(cli[0]["cli_tainted"], "args.output inside an f-string must taint")
        findings = audit(contracts=contracts, scopes=self.scopes(), requested_names=[])
        entry = [r for r in findings["derived_contracts"] if r["contract_source"].endswith("cli_out.py:4")][0]
        self.assertEqual(entry["status"], "UNVERIFIABLE")
        self.assertEqual(entry["reason"], "output_path_cli_supplied")
        self.assertNotEqual(entry["status"], "ABSENT_IN_CHECKED_SCOPE")

    # --- counter-example 6: conditional with one resolvable branch ----------
    def test_conditional_with_unknown_branch_stays_unverifiable(self):
        self.write_module("backend/cond.py", """
            from .sink import publish_json_once

            def emit(directory, choose, runtime_name):
                publish_json_once(directory/('observer-exit.json' if choose else runtime_name()), {})
        """)
        contracts = extract_contracts(self.src)
        for contract in contracts:
            if contract["base_expr"] == "directory":
                contract["expected_base"] = "monitoring"
        cond = [c for c in contracts if c["source"].endswith("cond.py:4")]
        self.assertEqual(len(cond), 1)
        self.assertEqual(cond[0]["resolution"], "conditional")
        self.assertIsNone(cond[0]["name_pattern"], "an unknown branch must not be promoted")
        findings = audit(contracts=contracts, scopes=self.scopes(), requested_names=[])
        entry = [r for r in findings["derived_contracts"] if r["contract_source"].endswith("cond.py:4")][0]
        self.assertEqual(entry["status"], "UNVERIFIABLE")
        self.assertNotEqual(entry["status"], "ABSENT_IN_CHECKED_SCOPE")


class RecursivePartialFailureTests(SyntheticSourceMixin, unittest.TestCase):
    """A recursive walk that cannot enumerate a subdirectory is incomplete.

    Source-only finding in the review: ``os.walk`` had no ``onerror`` handler, so
    a skipped subdirectory was silently treated as a complete check.  Simulated
    here by driving ``onerror`` once; not reproduced against a live permission
    fault, and never against the historic run root.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.src = os.path.join(self._tmp.name, "src")
        self.monitoring = os.path.join(self._tmp.name, "monitoring")
        os.makedirs(self.monitoring)
        self.write_module("backend/writers.py", """
            from .sink import publish_json_once

            def emit(directory):
                publish_json_once(directory/'observer-exit.json', {})
        """)
        self.contracts = extract_contracts(self.src)
        for contract in self.contracts:
            if contract["base_expr"] == "directory":
                contract["expected_base"] = "monitoring"

    def test_walk_error_marks_scope_incomplete_and_target_unverifiable(self):
        real_walk = os.walk

        def walk_with_error(top, onerror=None, **kwargs):
            yield top, ["sub"], ["placeholder.json"]
            if onerror is not None:
                onerror(OSError("simulated enumeration failure"))
            return
            yield  # pragma: no cover - makes this a generator

        with mock.patch.object(x07.os, "walk", walk_with_error):
            scope = list_scope(self.monitoring, recursive=True)
        self.assertTrue(scope["walk_errors"])
        self.assertFalse(scope["check_complete"])
        self.assertIn("walk_error", _incompleteness_reasons(scope))

        findings = audit(
            contracts=self.contracts,
            scopes={"monitoring": scope},
            requested_names=[],
        )
        entry = [r for r in findings["derived_contracts"] if r["name_pattern"] == "observer-exit.json"][0]
        # No exact hit and the walk was incomplete -> cannot claim absence.
        self.assertEqual(entry["status"], "UNVERIFIABLE")
        self.assertIn("walk_error", entry["reason"])
        self.assertEqual(entry["scope_check_complete"], False)

    def test_complete_walk_is_marked_complete(self):
        scope = list_scope(self.monitoring, recursive=True)
        self.assertTrue(scope["check_complete"])
        self.assertEqual(_incompleteness_reasons(scope), "")


class PatternDiscriminationTests(unittest.TestCase):
    def test_only_extension_wildcard_is_not_discriminating(self):
        self.assertFalse(_is_discriminating("{*}.json"))
        self.assertFalse(_is_discriminating("{*}-{*}.json"))

    def test_real_bound_patterns_are_discriminating(self):
        for pattern in ("stop-{*}-{*}.json", "host-{*}.json", "observer-exit.json",
                        "monitor-start-{*}.json", "observer-drain-execution-{*}.json"):
            self.assertTrue(_is_discriminating(pattern), pattern)

    def test_non_discriminating_pattern_is_unverifiable_not_loosely_matched(self):
        contracts = [{
            "product_code": True, "name_pattern": "{*}.json", "expected_base": "monitoring",
            "reason": "fstring_dynamic_segment", "alternatives": [], "cli_tainted": False,
            "base_expr": "directory", "base_evidence": "x:1", "writer_func": "publish_json_once",
            "resolution": "partial_dynamic", "source": "backend/x.py:1", "path_expression": "d/f'{n}.json'",
        }]
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "unrelated.json").write_text("{}", encoding="utf-8")
            findings = audit(
                contracts=contracts,
                scopes={"monitoring": list_scope(tmp, recursive=False)},
                requested_names=[],
            )
        entry = findings["derived_contracts"][0]
        self.assertEqual(entry["status"], "UNVERIFIABLE")
        self.assertIn("pattern_not_discriminating", entry["reason"])
        self.assertNotEqual(entry["status"], "EXISTS")


if __name__ == "__main__":
    unittest.main()
