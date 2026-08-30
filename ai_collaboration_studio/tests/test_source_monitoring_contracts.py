from __future__ import annotations

import hashlib
import math
import unittest

from backend.source_monitoring.contracts import (
    MAX_CHECKPOINT_BYTES,
    MAX_JSON_DEPTH,
    MAX_OBSERVED_ITEMS_PER_POLL,
    MAX_SOURCE_ERRORS_PER_POLL,
    AdapterPollResult,
    SourceMonitoringContractError,
    SourcePollError,
    canonical_json,
    canonical_sha256,
    normalize_adapter_key,
    normalize_checkpoint,
)
from backend.source_monitoring.health import (
    SOURCE_ADAPTER_HEALTH_VERSION,
    SOURCE_MONITORING_HEALTH_VERSION,
    SOURCE_MONITOR_HEALTH_STATES,
    project_adapter_health,
    project_monitoring_health,
)


CAPTURED_AT_MS = 1_787_845_600_000


class NativeIntSubclass(int):
    pass


def _result(**overrides) -> AdapterPollResult:
    values = {
        "adapter_key": "official_source",
        "started_checkpoint": {"cursor": "before", "page": 1},
        "next_checkpoint": {"cursor": "after", "page": 2},
        "observed_items": [{"external_item_id": "item-1", "revision": 1}],
        "source_errors": (),
        "retry_after_ms": 0,
        "captured_at_ms": CAPTURED_AT_MS,
        "etag": '"etag-v1"',
        "last_modified": "Sun, 30 Aug 2026 17:00:00 GMT",
        "duplicate_count": 0,
        "rejected_count": 0,
    }
    values.update(overrides)
    return AdapterPollResult.build(**values)


class SourcePollErrorContractTests(unittest.TestCase):
    def assert_contract_code(self, expected: str, callback) -> None:
        with self.assertRaises(SourceMonitoringContractError) as captured:
            callback()
        self.assertEqual(captured.exception.code, expected)

    def test_build_normalizes_and_to_dict_is_independent(self) -> None:
        error = SourcePollError.build(
            "source_timeout",
            "  upstream timed out\r\nretry later  ",
            "  official_feed  ",
        )

        self.assertEqual(error.code, "SOURCE_TIMEOUT")
        self.assertEqual(error.message, "upstream timed out\nretry later")
        self.assertEqual(error.scope, "official_feed")

        first = error.to_dict()
        second = error.to_dict()
        first["message"] = "mutated"
        self.assertEqual(second["message"], "upstream timed out\nretry later")
        self.assertEqual(error.message, "upstream timed out\nretry later")

    def test_invalid_error_codes_and_control_characters_fail_closed(self) -> None:
        for code in ("", "1INVALID", "INVALID-CODE", "INVALID CODE"):
            with self.subTest(code=code):
                self.assert_contract_code(
                    "SOURCE_MONITORING_ERROR_CODE_INVALID"
                    if code
                    else "SOURCE_MONITORING_TEXT_INVALID",
                    lambda code=code: SourcePollError.build(code, "message"),
                )

        for field, value in (
            ("message", "line one\x00line two"),
            ("scope", "feed\nforged"),
        ):
            with self.subTest(field=field):
                self.assert_contract_code(
                    "SOURCE_MONITORING_TEXT_INVALID",
                    lambda field=field, value=value: SourcePollError.build(
                        "SOURCE_FAILURE",
                        value if field == "message" else "message",
                        value if field == "scope" else "",
                    ),
                )


class CheckpointContractTests(unittest.TestCase):
    def assert_contract_code(self, expected: str, callback) -> None:
        with self.assertRaises(SourceMonitoringContractError) as captured:
            callback()
        self.assertEqual(captured.exception.code, expected)

    def test_checkpoint_is_a_defensive_canonical_native_json_object(self) -> None:
        original = {
            "z": [{"cursor": 2}],
            "a": {"nested": [True, None, 1.25]},
        }
        normalized = normalize_checkpoint(original)
        original["z"][0]["cursor"] = 99
        normalized["a"]["nested"].append("local mutation")

        self.assertEqual(normalize_checkpoint({
            "z": [{"cursor": 2}],
            "a": {"nested": [True, None, 1.25]},
        })["z"][0]["cursor"], 2)
        self.assertNotIn("local mutation", original["a"]["nested"])

    def test_checkpoint_rejects_non_dict_cycle_depth_nan_and_size_overflow(self) -> None:
        self.assert_contract_code(
            "SOURCE_MONITORING_CHECKPOINT_INVALID",
            lambda: normalize_checkpoint([]),
        )

        cycle: dict[str, object] = {}
        cycle["self"] = cycle
        self.assert_contract_code(
            "SOURCE_MONITORING_JSON_CYCLE",
            lambda: normalize_checkpoint(cycle),
        )

        deep: dict[str, object] = {}
        for _ in range(MAX_JSON_DEPTH + 1):
            deep = {"next": deep}
        self.assert_contract_code(
            "SOURCE_MONITORING_JSON_TOO_DEEP",
            lambda: normalize_checkpoint(deep),
        )

        self.assert_contract_code(
            "SOURCE_MONITORING_JSON_NONFINITE_NUMBER",
            lambda: normalize_checkpoint({"value": math.nan}),
        )
        self.assert_contract_code(
            "SOURCE_MONITORING_CHECKPOINT_TOO_LARGE",
            lambda: normalize_checkpoint({"payload": "x" * MAX_CHECKPOINT_BYTES}),
        )


class AdapterPollResultContractTests(unittest.TestCase):
    def assert_contract_code(self, expected: str, callback) -> None:
        with self.assertRaises(SourceMonitoringContractError) as captured:
            callback()
        self.assertEqual(captured.exception.code, expected)

    def test_build_and_to_dict_defensively_copy_all_mutable_values(self) -> None:
        started = {"cursor": {"page": 1}}
        next_checkpoint = {"cursor": {"page": 2}}
        observed = [{"id": "official-1", "facts": [{"value": 7}]}]
        source_error = SourcePollError.build("SOURCE_TIMEOUT", "timed out", "feed")

        result = _result(
            adapter_key="official_source",
            started_checkpoint=started,
            next_checkpoint=next_checkpoint,
            observed_items=observed,
            source_errors=[source_error],
            duplicate_count=3,
            rejected_count=2,
            etag="  etag-value  ",
        )

        started["cursor"]["page"] = 91
        next_checkpoint["cursor"]["page"] = 92
        observed[0]["facts"][0]["value"] = 93

        self.assertEqual(result.adapter_key, "official_source")
        self.assertEqual(result.started_checkpoint["cursor"]["page"], 1)
        self.assertEqual(result.next_checkpoint["cursor"]["page"], 2)
        self.assertEqual(result.observed_items[0]["facts"][0]["value"], 7)
        self.assertIsNot(result.source_errors[0], source_error)
        self.assertEqual(result.etag, "etag-value")
        self.assertEqual(result.observed_count, 6)

        projection = result.to_dict()
        self.assertEqual(projection["observed_count"], 6)
        self.assertEqual(projection["duplicate_count"], 3)
        self.assertEqual(projection["rejected_count"], 2)
        projection["started_checkpoint"]["cursor"]["page"] = 101
        projection["next_checkpoint"]["cursor"]["page"] = 102
        projection["observed_items"][0]["facts"][0]["value"] = 103
        projection["source_errors"][0]["message"] = "mutated"

        fresh = result.to_dict()
        self.assertEqual(fresh["started_checkpoint"]["cursor"]["page"], 1)
        self.assertEqual(fresh["next_checkpoint"]["cursor"]["page"], 2)
        self.assertEqual(fresh["observed_items"][0]["facts"][0]["value"], 7)
        self.assertEqual(fresh["source_errors"][0]["message"], "timed out")

    def test_native_integer_fields_reject_bool_subclasses_and_out_of_range_values(self) -> None:
        for field in (
            "retry_after_ms",
            "captured_at_ms",
            "duplicate_count",
            "rejected_count",
        ):
            for invalid in (True, NativeIntSubclass(1), -1):
                with self.subTest(field=field, invalid=invalid):
                    self.assert_contract_code(
                        "SOURCE_MONITORING_INTEGER_INVALID",
                        lambda field=field, invalid=invalid: _result(**{field: invalid}),
                    )

    def test_item_and_error_limits_are_enforced(self) -> None:
        self.assert_contract_code(
            "SOURCE_MONITORING_ITEMS_TOO_MANY",
            lambda: _result(
                observed_items=[
                    {"id": f"item-{index}"}
                    for index in range(MAX_OBSERVED_ITEMS_PER_POLL + 1)
                ]
            ),
        )
        error = SourcePollError.build("SOURCE_FAILURE", "failed")
        self.assert_contract_code(
            "SOURCE_MONITORING_ERRORS_TOO_MANY",
            lambda: _result(
                source_errors=[error] * (MAX_SOURCE_ERRORS_PER_POLL + 1)
            ),
        )

    def test_invalid_adapter_keys_and_header_controls_fail_closed(self) -> None:
        for adapter_key in (
            "",
            "1official",
            "Official_Source",
            "official-source",
            "official source",
        ):
            with self.subTest(adapter_key=adapter_key):
                expected = (
                    "SOURCE_MONITORING_TEXT_INVALID"
                    if not adapter_key
                    else "SOURCE_MONITORING_ADAPTER_KEY_INVALID"
                )
                self.assert_contract_code(
                    expected,
                    lambda adapter_key=adapter_key: normalize_adapter_key(adapter_key),
                )

        for header, value in (
            ("etag", "safe\r\nX-Forged: value"),
            ("last_modified", "Sun, 30 Aug 2026\nX-Forged: value"),
        ):
            with self.subTest(header=header):
                self.assert_contract_code(
                    "SOURCE_MONITORING_TEXT_INVALID",
                    lambda header=header, value=value: _result(**{header: value}),
                )

    def test_canonical_json_and_hash_are_stable_across_mapping_order(self) -> None:
        first = {
            "z": [3, {"beta": False, "alpha": None}],
            "a": {"two": 2, "one": 1},
        }
        second = {
            "a": {"one": 1, "two": 2},
            "z": [3, {"alpha": None, "beta": False}],
        }

        rendered = canonical_json(first)
        self.assertEqual(rendered, canonical_json(second))
        self.assertEqual(canonical_sha256(first), canonical_sha256(second))
        self.assertEqual(
            canonical_sha256(first),
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        )


class SourceMonitoringHealthContractTests(unittest.TestCase):
    @staticmethod
    def state(adapter_key: str, **overrides) -> dict[str, object]:
        value: dict[str, object] = {
            "adapter_key": adapter_key,
            "enabled": True,
            "consecutive_failures": 0,
            "last_started_at_ms": 90,
            "last_success_at_ms": 0,
            "last_event_at_ms": 0,
            "next_due_at_ms": 0,
            "discovery_delay_ms": 0,
            "last_error_code": "",
        }
        value.update(overrides)
        return value

    def test_all_seven_health_states_and_aggregate_counts_are_projected(self) -> None:
        states = [
            self.state("disabled", enabled=False),
            self.state("idle"),
            self.state("running"),
            self.state("healthy", last_success_at_ms=80),
            self.state("degraded", consecutive_failures=1, next_due_at_ms=100),
            self.state("backing_off", consecutive_failures=1, next_due_at_ms=101),
            self.state("failed", consecutive_failures=5),
        ]
        expected = {
            "disabled": "disabled",
            "idle": "idle",
            "running": "running",
            "healthy": "healthy",
            "degraded": "degraded",
            "backing_off": "backing_off",
            "failed": "failed",
        }

        for state in states:
            adapter_key = str(state["adapter_key"])
            with self.subTest(adapter_key=adapter_key):
                projected = project_adapter_health(
                    state,
                    now_ms=100,
                    running=adapter_key == "running",
                )
                self.assertEqual(projected["version"], SOURCE_ADAPTER_HEALTH_VERSION)
                self.assertEqual(projected["state"], expected[adapter_key])
                self.assertEqual(projected["execution_capability"], "none")
                self.assertFalse(projected["live_trading_allowed"])

        aggregate = project_monitoring_health(
            list(reversed(states)),
            100,
            running_adapter_keys=["running"],
        )
        self.assertEqual(aggregate["version"], SOURCE_MONITORING_HEALTH_VERSION)
        self.assertEqual(aggregate["state"], "failed")
        self.assertEqual(aggregate["adapter_count"], 7)
        self.assertEqual(
            aggregate["counts"],
            {health_state: 1 for health_state in SOURCE_MONITOR_HEALTH_STATES},
        )
        self.assertEqual(
            [adapter["adapter_key"] for adapter in aggregate["adapters"]],
            sorted(expected),
        )
        self.assertEqual(aggregate["execution_capability"], "none")
        self.assertFalse(aggregate["live_trading_allowed"])

    def test_invalid_enabled_and_native_type_confusion_fail_closed(self) -> None:
        for invalid_enabled in (1, 0, "true", NativeIntSubclass(1), None):
            with self.subTest(enabled=invalid_enabled):
                projected = project_adapter_health(
                    self.state("adapter", enabled=invalid_enabled),
                    now_ms=100,
                )
                self.assertFalse(projected["enabled"])
                self.assertEqual(projected["state"], "disabled")

        invalid_failure = project_adapter_health(
            self.state("adapter", consecutive_failures=True),
            now_ms=100,
        )
        invalid_clock = project_adapter_health(
            self.state("adapter"),
            now_ms=True,
        )
        invalid_persisted_clock = project_adapter_health(
            self.state("adapter", next_due_at_ms="100"),
            now_ms=100,
            running=True,
        )
        non_native_running = project_adapter_health(
            self.state("adapter"),
            now_ms=100,
            running=1,
        )
        self.assertEqual(invalid_failure["state"], "failed")
        self.assertEqual(invalid_clock["state"], "failed")
        self.assertEqual(invalid_persisted_clock["state"], "failed")
        self.assertFalse(invalid_persisted_clock["running"])
        self.assertFalse(non_native_running["running"])
        self.assertEqual(non_native_running["state"], "idle")

    def test_invalid_aggregate_inputs_remain_bounded_and_nonexecuting(self) -> None:
        projected = project_monitoring_health(
            {"not": "a native sequence"},
            True,
            running_adapter_keys="adapter",
        )
        self.assertEqual(projected["captured_at_ms"], 0)
        self.assertEqual(projected["state"], "idle")
        self.assertEqual(projected["adapter_count"], 0)
        self.assertEqual(
            projected["counts"],
            {health_state: 0 for health_state in SOURCE_MONITOR_HEALTH_STATES},
        )
        self.assertEqual(projected["execution_capability"], "none")
        self.assertFalse(projected["live_trading_allowed"])


if __name__ == "__main__":
    unittest.main()
