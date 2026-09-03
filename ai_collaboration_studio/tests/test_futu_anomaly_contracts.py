from __future__ import annotations

import copy
import math
import unittest
from datetime import datetime, timedelta, timezone

from backend.market.futu_readonly import STORAGE_SYMBOLS, validate_storage_quote_snapshot
from backend.source_monitoring.contracts import (
    FUTU_ANOMALY_SOURCE_CHANNEL,
    canonical_sha256,
)
from backend.source_monitoring.futu_anomaly_contracts import (
    AMPLITUDE_RULE_ID,
    FUTU_ANOMALY_CHECKPOINT_VERSION,
    FUTU_ANOMALY_RULE_IDS,
    FUTU_ANOMALY_SOURCE_URL,
    MAX_FUTU_ANOMALY_SNAPSHOT_BYTES,
    PRICE_DOWN_RULE_ID,
    PRICE_UP_RULE_ID,
    VOLUME_RATIO_RULE_ID,
    futu_anomaly_policy_manifest,
    normalize_futu_anomaly_checkpoint,
    project_futu_anomaly_snapshot,
)
from backend.source_monitoring.packet_builder import build_source_import_packet


BASE_UPDATED_AT = datetime(2026, 8, 3, 14, 0, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    utc_moment = moment.astimezone(timezone.utc)
    timespec = (
        "milliseconds"
        if utc_moment.microsecond % 1_000 == 0
        else "microseconds"
    )
    return (
        utc_moment
        .isoformat(timespec=timespec)
        .replace("+00:00", "Z")
    )


def _observed_ms(snapshot: dict[str, object], *, seconds_after_capture: int = 30) -> int:
    captured = datetime.fromisoformat(
        str(snapshot["captured_at"]).replace("Z", "+00:00")
    )
    return int((captured + timedelta(seconds=seconds_after_capture)).timestamp() * 1_000)


def make_snapshot(
    *,
    updated_at: datetime = BASE_UPDATED_AT,
    metrics_by_symbol: dict[str, dict[str, float | int]] | None = None,
    snapshot_id: str = "futu_contract_fixture_1",
    captured_delay_seconds: int = 30,
    row_order: tuple[str, ...] | list[str] = STORAGE_SYMBOLS,
) -> dict[str, object]:
    metrics_by_symbol = metrics_by_symbol or {}
    captured_at = updated_at + timedelta(seconds=captured_delay_seconds)
    rows: list[dict[str, object]] = []
    for symbol in row_order:
        index = STORAGE_SYMBOLS.index(symbol)
        metrics = {
            "last": 100.0 + index,
            "change_rate": 0.5,
            "amplitude": 1.0,
            "volume_ratio": 1.0,
            **metrics_by_symbol.get(symbol, {}),
        }
        rows.append({
            "symbol": symbol,
            "name": symbol,
            "updated_at": _iso(updated_at),
            "market_time": updated_at.astimezone(
                timezone(timedelta(hours=-4))
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "age_seconds": captured_delay_seconds,
            "quality": "ready",
            "research_ready": True,
            "quote_is_live": True,
            "market_state": None,
            "freshness_basis": "live_20m_window",
            **metrics,
            "security_status": "NORMAL",
            "suspended": False,
        })
    return {
        "snapshot_id": snapshot_id,
        "source": "futu_opend",
        "market": "US",
        "symbols": list(STORAGE_SYMBOLS),
        "captured_at": _iso(captured_at),
        "captured_at_ms": int(captured_at.timestamp() * 1_000),
        "rows": rows,
        "missing_symbols": [],
        "source_errors": [],
        "execution_capability": "none",
        "live_trading_allowed": False,
        "ok": True,
        "state": "ready",
        "data_quality": {
            "requested": len(STORAGE_SYMBOLS),
            "received": len(STORAGE_SYMBOLS),
            "ready": len(STORAGE_SYMBOLS),
            "stale_or_invalid": 0,
        },
        "cache": {"hit": False, "ttl_seconds": 0},
    }


def _entry(checkpoint: dict[str, object], symbol: str) -> dict[str, object]:
    return next(
        entry
        for entry in checkpoint["symbols"]  # type: ignore[index]
        if entry["symbol"] == symbol
    )


def _codes(result: object) -> list[str]:
    return [error.code for error in result.errors]  # type: ignore[attr-defined]


class FutuAnomalyContractTests(unittest.TestCase):
    def project(
        self,
        snapshot: dict[str, object],
        checkpoint: dict[str, object] | None = None,
        *,
        observed_at_ms: int | None = None,
    ):
        return project_futu_anomaly_snapshot(
            snapshot,
            started_checkpoint=checkpoint or {},
            observed_at_ms=(
                _observed_ms(snapshot)
                if observed_at_ms is None
                else observed_at_ms
            ),
        )

    def assert_atomic_failure(
        self,
        snapshot: dict[str, object],
        checkpoint: dict[str, object] | None = None,
        *,
        observed_at_ms: int | None = None,
    ):
        checkpoint = checkpoint or {}
        result = self.project(
            snapshot,
            checkpoint,
            observed_at_ms=observed_at_ms,
        )
        self.assertEqual(result.items, ())
        self.assertEqual(result.duplicate_count, 0)
        self.assertGreaterEqual(result.rejected_count, 1)
        self.assertTrue(result.errors)
        self.assertEqual(result.next_checkpoint, checkpoint)
        return result

    def test_v1_entry_boundaries_and_neutral_source_item_contract(self) -> None:
        snapshot = make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5.0},
            "US.SNDK": {"change_rate": -5.0},
            "US.STX": {"amplitude": 8.0},
            "US.WDC": {"volume_ratio": 3.0},
        })
        result = self.project(snapshot)

        self.assertEqual(result.errors, ())
        self.assertEqual(
            [item["extensions"]["futu_anomaly_v1"]["rule_id"] for item in result.items],
            [
                PRICE_UP_RULE_ID,
                PRICE_DOWN_RULE_ID,
                AMPLITUDE_RULE_ID,
                VOLUME_RATIO_RULE_ID,
            ],
        )
        for item in result.items:
            self.assertEqual(item["version"], "project_source_item_v1")
            self.assertEqual(item["item_type"], "market_anomaly_signal")
            self.assertEqual(item["severity"], "info")
            self.assertEqual(item["recommended_route"], "notify_only")
            self.assertEqual(item["impact_hypotheses"], [])
            self.assertEqual(item["sources"][0]["url"], FUTU_ANOMALY_SOURCE_URL)
            self.assertRegex(item["sources"][0]["content_sha256"], r"^[0-9a-f]{64}$")
            extension = item["extensions"]["futu_anomaly_v1"]
            self.assertIs(extension["news_attribution_performed"], False)
            self.assertEqual(extension["causal_attribution"], "none")
            self.assertIs(extension["signal_only"], True)
            self.assertTrue(any("cause" in value.lower() for value in item["unknowns"]))
            self.assertTrue(any("news attribution" in value.lower() for value in item["unknowns"]))
            self.assertTrue(any("trading implication" in value.lower() for value in item["unknowns"]))
            self.assertEqual(item["occurred_at"], "2026-08-03T13:30:00.000Z")

        packet = build_source_import_packet(
            adapter_key="futu_anomaly",
            external_run_id="futu-contract-test",
            captured_at_ms=_observed_ms(snapshot),
            observed_items=result.items,
            source_channel=FUTU_ANOMALY_SOURCE_CHANNEL,
        )
        self.assertEqual(packet["items"], list(result.items))

    def test_below_entry_boundaries_do_not_trigger(self) -> None:
        snapshot = make_snapshot(metrics_by_symbol={
            "US.MU": {
                "change_rate": 4.999,
                "amplitude": 7.999,
                "volume_ratio": 2.999,
            },
            "US.SNDK": {"change_rate": -4.999},
        })
        result = self.project(snapshot)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.items, ())
        self.assertEqual(result.duplicate_count, 0)

    def test_hysteresis_and_once_per_rule_per_session(self) -> None:
        first_snapshot = make_snapshot(
            metrics_by_symbol={"US.MU": {"change_rate": 5.0}}
        )
        first = self.project(first_snapshot)
        self.assertEqual(len(first.items), 1)

        at_exit_boundary = make_snapshot(
            updated_at=BASE_UPDATED_AT + timedelta(minutes=1),
            metrics_by_symbol={"US.MU": {"change_rate": 4.0}},
        )
        held = self.project(at_exit_boundary, first.next_checkpoint)
        self.assertEqual(held.items, ())
        self.assertIn(
            PRICE_UP_RULE_ID,
            _entry(held.next_checkpoint, "US.MU")["active_rule_ids"],
        )

        below_exit = make_snapshot(
            updated_at=BASE_UPDATED_AT + timedelta(minutes=2),
            metrics_by_symbol={"US.MU": {"change_rate": 3.999}},
        )
        exited = self.project(below_exit, held.next_checkpoint)
        self.assertNotIn(
            PRICE_UP_RULE_ID,
            _entry(exited.next_checkpoint, "US.MU")["active_rule_ids"],
        )
        self.assertIn(
            PRICE_UP_RULE_ID,
            _entry(exited.next_checkpoint, "US.MU")["emitted_rule_ids"],
        )

        reentered = self.project(
            make_snapshot(
                updated_at=BASE_UPDATED_AT + timedelta(minutes=3),
                metrics_by_symbol={"US.MU": {"change_rate": 6.0}},
            ),
            exited.next_checkpoint,
        )
        self.assertEqual(reentered.items, ())
        self.assertIn(
            PRICE_UP_RULE_ID,
            _entry(reentered.next_checkpoint, "US.MU")["active_rule_ids"],
        )

    def test_amplitude_and_volume_ratio_exit_boundaries_use_decimal_comparison(self) -> None:
        first = self.project(make_snapshot(metrics_by_symbol={
            "US.MU": {"amplitude": 8, "volume_ratio": 3},
        }))
        held = self.project(
            make_snapshot(
                updated_at=BASE_UPDATED_AT + timedelta(minutes=1),
                metrics_by_symbol={"US.MU": {"amplitude": 6.0, "volume_ratio": 2.50}},
            ),
            first.next_checkpoint,
        )
        active = _entry(held.next_checkpoint, "US.MU")["active_rule_ids"]
        self.assertIn(AMPLITUDE_RULE_ID, active)
        self.assertIn(VOLUME_RATIO_RULE_ID, active)
        exited = self.project(
            make_snapshot(
                updated_at=BASE_UPDATED_AT + timedelta(minutes=2),
                metrics_by_symbol={"US.MU": {"amplitude": 5.999, "volume_ratio": 2.499}},
            ),
            held.next_checkpoint,
        )
        active = _entry(exited.next_checkpoint, "US.MU")["active_rule_ids"]
        self.assertNotIn(AMPLITUDE_RULE_ID, active)
        self.assertNotIn(VOLUME_RATIO_RULE_ID, active)

    def test_new_us_eastern_session_can_emit_the_same_rule_again(self) -> None:
        first = self.project(make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5},
        }))
        second_snapshot = make_snapshot(
            updated_at=BASE_UPDATED_AT + timedelta(days=1),
            metrics_by_symbol={"US.MU": {"change_rate": 5}},
        )
        second = self.project(second_snapshot, first.next_checkpoint)
        self.assertEqual(len(second.items), 1)
        self.assertNotEqual(
            first.items[0]["external_item_id"],
            second.items[0]["external_item_id"],
        )
        self.assertEqual(
            _entry(second.next_checkpoint, "US.MU")["session_date"],
            "2026-08-04",
        )

    def test_row_and_rule_sorting_are_deterministic(self) -> None:
        metrics = {
            symbol: {"change_rate": 6, "amplitude": 9, "volume_ratio": 4}
            for symbol in STORAGE_SYMBOLS
        }
        ordered = self.project(make_snapshot(metrics_by_symbol=metrics))
        reversed_rows = self.project(make_snapshot(
            metrics_by_symbol=metrics,
            row_order=tuple(reversed(STORAGE_SYMBOLS)),
        ))
        self.assertEqual(ordered.items, reversed_rows.items)
        self.assertEqual(ordered.next_checkpoint, reversed_rows.next_checkpoint)
        pairs = [
            (
                item["extensions"]["futu_anomaly_v1"]["symbol"],
                item["extensions"]["futu_anomaly_v1"]["rule_id"],
            )
            for item in ordered.items
        ]
        expected = [
            (symbol, rule_id)
            for symbol in sorted(STORAGE_SYMBOLS)
            for rule_id in (
                PRICE_UP_RULE_ID,
                AMPLITUDE_RULE_ID,
                VOLUME_RATIO_RULE_ID,
            )
        ]
        self.assertEqual(pairs, expected)
        self.assertEqual(
            [entry["symbol"] for entry in ordered.next_checkpoint["symbols"]],
            sorted(STORAGE_SYMBOLS),
        )

    def test_snapshot_id_captured_at_and_poll_time_do_not_change_item_identity(self) -> None:
        original_snapshot = make_snapshot(
            metrics_by_symbol={"US.MU": {"change_rate": 5}},
            snapshot_id="random-a",
        )
        changed_envelope = copy.deepcopy(original_snapshot)
        changed_envelope["snapshot_id"] = "random-b"
        captured = datetime.fromisoformat(
            str(changed_envelope["captured_at"]).replace("Z", "+00:00")
        ) + timedelta(seconds=30)
        changed_envelope["captured_at"] = _iso(captured)
        changed_envelope["captured_at_ms"] = int(captured.timestamp() * 1_000)
        for row in changed_envelope["rows"]:
            row["age_seconds"] = 60

        first = self.project(original_snapshot)
        second = self.project(
            changed_envelope,
            observed_at_ms=_observed_ms(changed_envelope, seconds_after_capture=90),
        )
        self.assertEqual(first.items, second.items)
        self.assertEqual(first.next_checkpoint, second.next_checkpoint)

    def test_exact_replay_is_duplicate_only_when_that_observation_emitted(self) -> None:
        emitting_snapshot = make_snapshot(
            metrics_by_symbol={"US.MU": {"change_rate": 5}}
        )
        first = self.project(emitting_snapshot)
        replay = self.project(emitting_snapshot, first.next_checkpoint)
        self.assertEqual(replay.items, ())
        self.assertEqual(replay.duplicate_count, 1)
        self.assertEqual(replay.next_checkpoint, first.next_checkpoint)

        quiet_snapshot = make_snapshot()
        quiet = self.project(quiet_snapshot)
        quiet_replay = self.project(quiet_snapshot, quiet.next_checkpoint)
        self.assertEqual(quiet_replay.items, ())
        self.assertEqual(quiet_replay.duplicate_count, 0)

    def test_exact_replay_duplicate_count_matches_items_actually_emitted(self) -> None:
        snapshot = make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5, "amplitude": 8, "volume_ratio": 3},
        })
        first = self.project(snapshot)
        self.assertEqual(len(first.items), 3)
        state = _entry(first.next_checkpoint, "US.MU")
        self.assertEqual(
            state["last_emitted_rule_ids"],
            [PRICE_UP_RULE_ID, AMPLITUDE_RULE_ID, VOLUME_RATIO_RULE_ID],
        )
        replay = self.project(snapshot, first.next_checkpoint)
        self.assertEqual(replay.items, ())
        self.assertEqual(replay.duplicate_count, 3)

    def test_same_timestamp_semantic_change_is_an_explicit_atomic_conflict(self) -> None:
        snapshot = make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5},
        })
        first = self.project(snapshot)
        conflicting = copy.deepcopy(snapshot)
        conflicting["rows"][0]["change_rate"] = 5.1
        result = self.assert_atomic_failure(conflicting, first.next_checkpoint)
        self.assertEqual(_codes(result), ["FUTU_ANOMALY_OBSERVATION_CONFLICT"])

    def test_submillisecond_timestamp_is_lossless_for_replay_reverse_and_conflict(self) -> None:
        newest_time = BASE_UPDATED_AT.replace(microsecond=900)
        snapshot = make_snapshot(
            updated_at=newest_time,
            metrics_by_symbol={"US.MU": {"change_rate": 5}},
        )
        first = self.project(snapshot)
        self.assertEqual(
            _entry(first.next_checkpoint, "US.MU")["last_observed_at"],
            "2026-08-03T14:00:00.000900Z",
        )
        replay = self.project(snapshot, first.next_checkpoint)
        self.assertEqual(replay.duplicate_count, 1)

        older = make_snapshot(
            updated_at=BASE_UPDATED_AT.replace(microsecond=100),
            metrics_by_symbol={"US.MU": {"change_rate": 5.1}},
        )
        reversed_result = self.assert_atomic_failure(older, first.next_checkpoint)
        self.assertEqual(
            _codes(reversed_result),
            ["FUTU_ANOMALY_OBSERVATION_REVERSED"],
        )

        conflict = copy.deepcopy(snapshot)
        conflict["rows"][0]["change_rate"] = 5.1
        conflict_result = self.assert_atomic_failure(conflict, first.next_checkpoint)
        self.assertEqual(
            _codes(conflict_result),
            ["FUTU_ANOMALY_OBSERVATION_CONFLICT"],
        )

    def test_reverse_timestamp_is_rejected_atomically(self) -> None:
        first = self.project(make_snapshot(updated_at=BASE_UPDATED_AT + timedelta(minutes=1)))
        older = make_snapshot(updated_at=BASE_UPDATED_AT)
        result = self.assert_atomic_failure(older, first.next_checkpoint)
        self.assertEqual(_codes(result), ["FUTU_ANOMALY_OBSERVATION_REVERSED"])

    def test_non_live_closed_stale_future_partial_duplicate_nonfinite_and_unsafe_fail(self) -> None:
        base = make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 8},
        })

        non_live = copy.deepcopy(base)
        non_live["rows"][0]["quote_is_live"] = False

        closed = copy.deepcopy(base)
        closed["rows"][0].update({
            "quote_is_live": False,
            "freshness_basis": "closed_session_latest_snapshot",
            "market_state": "CLOSED",
        })

        stale = copy.deepcopy(base)
        stale["rows"][0]["age_seconds"] = 1_201

        future = copy.deepcopy(base)
        future_observed = _observed_ms(future) - 120_000

        partial = copy.deepcopy(base)
        missing_symbol = partial["rows"].pop()["symbol"]
        partial["missing_symbols"] = [missing_symbol]

        duplicated = copy.deepcopy(base)
        duplicated["rows"][-1] = copy.deepcopy(duplicated["rows"][0])

        nonfinite = copy.deepcopy(base)
        nonfinite["rows"][0]["volume_ratio"] = math.inf

        unsafe = copy.deepcopy(base)
        unsafe["rows"][0]["account_id"] = "forbidden"

        camel_api_key = copy.deepcopy(base)
        camel_api_key["apiKey"] = "forbidden"

        camel_order_id = copy.deepcopy(base)
        camel_order_id["rows"][0]["orderId"] = "forbidden"

        unsafe_capability = copy.deepcopy(base)
        unsafe_capability["live_trading_allowed"] = True

        cases = {
            "non_live": (non_live, None),
            "closed": (closed, None),
            "stale": (stale, None),
            "future": (future, future_observed),
            "partial": (partial, None),
            "duplicate": (duplicated, None),
            "nonfinite": (nonfinite, None),
            "unsafe": (unsafe, None),
            "camel_api_key": (camel_api_key, None),
            "camel_order_id": (camel_order_id, None),
            "unsafe_capability": (unsafe_capability, None),
        }
        for label, (snapshot, observed_at_ms) in cases.items():
            with self.subTest(label=label):
                self.assert_atomic_failure(
                    snapshot,
                    observed_at_ms=observed_at_ms,
                )

    def test_storage_admitted_closed_quote_is_still_rejected_by_live_gate(self) -> None:
        snapshot = make_snapshot()
        captured = BASE_UPDATED_AT + timedelta(seconds=1_201)
        snapshot["captured_at"] = _iso(captured)
        snapshot["captured_at_ms"] = int(captured.timestamp() * 1_000)
        for index, row in enumerate(snapshot["rows"]):
            if index == 0:
                row.update({
                    "updated_at": _iso(BASE_UPDATED_AT),
                    "market_time": "2026-08-03 10:00:00",
                    "age_seconds": 1_201,
                    "quote_is_live": False,
                    "freshness_basis": "closed_session_latest_snapshot",
                    "market_state": "CLOSED",
                })
            else:
                live_time = captured - timedelta(seconds=30)
                row.update({
                    "updated_at": _iso(live_time),
                    "market_time": "2026-08-03 10:19:31",
                    "age_seconds": 30,
                })
        self.assertTrue(validate_storage_quote_snapshot(snapshot)["ready"])
        result = self.assert_atomic_failure(snapshot)
        self.assertEqual(_codes(result), ["FUTU_ANOMALY_QUOTE_NOT_LIVE"])

    def test_partial_metric_and_negative_unsigned_metrics_fail_atomically(self) -> None:
        for field, value in (
            ("change_rate", None),
            ("amplitude", None),
            ("volume_ratio", None),
            ("amplitude", -0.01),
            ("volume_ratio", -0.01),
        ):
            with self.subTest(field=field, value=value):
                snapshot = make_snapshot()
                snapshot["rows"][0][field] = value
                self.assert_atomic_failure(snapshot)

    def test_oversized_canonical_snapshot_is_rejected_before_projection(self) -> None:
        snapshot = make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 8},
        })
        snapshot["padding"] = "x" * MAX_FUTU_ANOMALY_SNAPSHOT_BYTES
        result = self.assert_atomic_failure(snapshot)
        self.assertEqual(_codes(result), ["FUTU_ANOMALY_SNAPSHOT_TOO_LARGE"])

    def test_checkpoint_tamper_is_rejected_and_never_advanced(self) -> None:
        snapshot = make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5},
        })
        first = self.project(snapshot)

        unknown_rule = copy.deepcopy(first.next_checkpoint)
        _entry(unknown_rule, "US.MU")["active_rule_ids"] = ["unknown_rule"]
        result = self.assert_atomic_failure(snapshot, unknown_rule)
        self.assertEqual(_codes(result), ["FUTU_ANOMALY_CHECKPOINT_INVALID"])

        bad_session = copy.deepcopy(first.next_checkpoint)
        _entry(bad_session, "US.MU")["session_date"] = "2026-08-02"
        result = self.assert_atomic_failure(snapshot, bad_session)
        self.assertEqual(_codes(result), ["FUTU_ANOMALY_CHECKPOINT_INVALID"])

        changed_hash = copy.deepcopy(first.next_checkpoint)
        _entry(changed_hash, "US.MU")["last_observation_sha256"] = "0" * 64
        result = self.assert_atomic_failure(snapshot, changed_hash)
        self.assertEqual(_codes(result), ["FUTU_ANOMALY_OBSERVATION_CONFLICT"])

    def test_checkpoint_rejects_impossible_price_rule_combinations(self) -> None:
        first = self.project(make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5},
        }))
        down_snapshot = make_snapshot(
            updated_at=BASE_UPDATED_AT + timedelta(minutes=1),
            metrics_by_symbol={"US.MU": {"change_rate": -5}},
        )
        second = self.project(down_snapshot, first.next_checkpoint)
        self.assertEqual(len(second.items), 1)
        replay = self.project(down_snapshot, second.next_checkpoint)
        self.assertEqual(replay.duplicate_count, 1)

        impossible_active = copy.deepcopy(second.next_checkpoint)
        _entry(impossible_active, "US.MU")["active_rule_ids"] = [
            PRICE_UP_RULE_ID,
            PRICE_DOWN_RULE_ID,
        ]
        self.assertEqual(
            _codes(self.assert_atomic_failure(down_snapshot, impossible_active)),
            ["FUTU_ANOMALY_CHECKPOINT_INVALID"],
        )

        impossible_last_emitted = copy.deepcopy(second.next_checkpoint)
        _entry(impossible_last_emitted, "US.MU")["last_emitted_rule_ids"] = [
            PRICE_UP_RULE_ID,
            PRICE_DOWN_RULE_ID,
        ]
        self.assertEqual(
            _codes(
                self.assert_atomic_failure(
                    down_snapshot,
                    impossible_last_emitted,
                )
            ),
            ["FUTU_ANOMALY_CHECKPOINT_INVALID"],
        )

        wrong_current_emission = copy.deepcopy(second.next_checkpoint)
        _entry(wrong_current_emission, "US.MU")["last_emitted_rule_ids"] = [
            PRICE_UP_RULE_ID,
        ]
        self.assertEqual(
            _codes(
                self.assert_atomic_failure(
                    down_snapshot,
                    wrong_current_emission,
                )
            ),
            ["FUTU_ANOMALY_CHECKPOINT_INVALID"],
        )

    def test_checkpoint_is_closed_complete_and_normalizable(self) -> None:
        result = self.project(make_snapshot())
        checkpoint, states = normalize_futu_anomaly_checkpoint(result.next_checkpoint)
        self.assertEqual(checkpoint["version"], FUTU_ANOMALY_CHECKPOINT_VERSION)
        self.assertEqual(set(states), set(STORAGE_SYMBOLS))
        self.assertEqual(
            [entry["symbol"] for entry in checkpoint["symbols"]],
            sorted(STORAGE_SYMBOLS),
        )
        for state in states.values():
            self.assertRegex(state["last_observation_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(state["active_rule_ids"], [])
            self.assertEqual(state["emitted_rule_ids"], [])
            self.assertEqual(state["last_emitted_rule_ids"], [])

    def test_content_hash_binds_stable_session_rule_signal_semantics(self) -> None:
        base = self.project(make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5},
        }))
        changed = self.project(make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5.1},
        }))
        first_item = base.items[0]
        changed_item = changed.items[0]
        self.assertEqual(first_item["external_item_id"], changed_item["external_item_id"])
        self.assertEqual(
            first_item["sources"][0]["content_sha256"],
            changed_item["sources"][0]["content_sha256"],
        )
        self.assertEqual(canonical_sha256(first_item), canonical_sha256(changed_item))

        next_session = self.project(make_snapshot(
            updated_at=BASE_UPDATED_AT + timedelta(days=1),
            metrics_by_symbol={"US.MU": {"change_rate": 5.1}},
        ))
        self.assertNotEqual(
            canonical_sha256(first_item),
            canonical_sha256(next_session.items[0]),
        )

    def test_same_session_rule_episode_is_stable_across_tick_time_and_value(self) -> None:
        first = self.project(make_snapshot(
            updated_at=BASE_UPDATED_AT,
            metrics_by_symbol={"US.MU": {"change_rate": 5}},
        ))
        later = self.project(make_snapshot(
            updated_at=BASE_UPDATED_AT + timedelta(hours=2),
            metrics_by_symbol={"US.MU": {"change_rate": 9.75}},
        ))
        self.assertEqual(len(first.items), 1)
        self.assertEqual(len(later.items), 1)
        self.assertEqual(canonical_sha256(first.items[0]), canonical_sha256(later.items[0]))
        self.assertEqual(first.items[0], later.items[0])

    def test_numeric_spellings_share_one_decimal_observation_semantics(self) -> None:
        integer_metric = self.project(make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5, "amplitude": 8, "volume_ratio": 3},
        }))
        float_metric = self.project(make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5.0, "amplitude": 8.0, "volume_ratio": 3.00},
        }))
        self.assertEqual(integer_metric.items, float_metric.items)
        self.assertEqual(integer_metric.next_checkpoint, float_metric.next_checkpoint)

    def test_security_status_must_be_explicit_normal_and_nonsuspended(self) -> None:
        for status, suspended in (
            (None, False),
            ("", False),
            ("DELISTED", False),
            ("NORMAL", True),
        ):
            with self.subTest(status=status, suspended=suspended):
                snapshot = make_snapshot()
                snapshot["rows"][0]["security_status"] = status
                snapshot["rows"][0]["suspended"] = suspended
                self.assert_atomic_failure(snapshot)

        enum_normal = make_snapshot()
        enum_normal["rows"][0]["security_status"] = "SecurityStatus.NORMAL"
        accepted = self.project(enum_normal)
        self.assertEqual(accepted.errors, ())

    def test_observed_time_must_be_native_and_snapshot_cannot_be_future(self) -> None:
        snapshot = make_snapshot()
        for invalid in (True, -1, 1.5, "1"):
            with self.subTest(observed_at_ms=invalid):
                self.assert_atomic_failure(snapshot, observed_at_ms=invalid)  # type: ignore[arg-type]

        before_capture = int(
            (
                datetime.fromisoformat(str(snapshot["captured_at"]).replace("Z", "+00:00"))
                - timedelta(milliseconds=1)
            ).timestamp()
            * 1_000
        )
        result = self.assert_atomic_failure(snapshot, observed_at_ms=before_capture)
        self.assertEqual(_codes(result), ["FUTU_ANOMALY_OBSERVATION_FUTURE"])

    def test_snapshot_self_claimed_live_but_stale_at_poll_time_is_rejected(self) -> None:
        snapshot = make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 8},
        })
        stale_observed_ms = _observed_ms(snapshot) + 7 * 24 * 60 * 60 * 1_000
        result = self.assert_atomic_failure(
            snapshot,
            observed_at_ms=stale_observed_ms,
        )
        self.assertEqual(_codes(result), ["FUTU_ANOMALY_OBSERVATION_STALE"])

    def test_all_rule_ids_are_sealed_and_checkpoint_lists_use_that_order(self) -> None:
        self.assertEqual(
            FUTU_ANOMALY_RULE_IDS,
            (
                PRICE_UP_RULE_ID,
                PRICE_DOWN_RULE_ID,
                AMPLITUDE_RULE_ID,
                VOLUME_RATIO_RULE_ID,
            ),
        )
        result = self.project(make_snapshot(metrics_by_symbol={
            "US.MU": {"change_rate": 5, "amplitude": 8, "volume_ratio": 3},
        }))
        self.assertEqual(
            _entry(result.next_checkpoint, "US.MU")["active_rule_ids"],
            [PRICE_UP_RULE_ID, AMPLITUDE_RULE_ID, VOLUME_RATIO_RULE_ID],
        )

    def test_policy_manifest_is_complete_and_defensively_copied(self) -> None:
        first = futu_anomaly_policy_manifest()
        self.assertEqual(first["storage_symbols"], list(STORAGE_SYMBOLS))
        self.assertEqual(first["source_url"], FUTU_ANOMALY_SOURCE_URL)
        self.assertEqual(
            [rule["rule_id"] for rule in first["rules"]],
            list(FUTU_ANOMALY_RULE_IDS),
        )
        self.assertEqual(
            [
                (
                    rule["metric"],
                    rule["entry_threshold"],
                    rule["exit_threshold"],
                    rule["comparison"],
                    rule["direction"],
                    rule["unit"],
                )
                for rule in first["rules"]
            ],
            [
                ("change_rate", "5", "4", "positive", "up_observation", "percent"),
                ("change_rate", "-5", "-4", "negative", "down_observation", "percent"),
                ("amplitude", "8", "6", "positive", "none", "percent"),
                ("volume_ratio", "3", "2.5", "positive", "none", "ratio"),
            ],
        )
        first["storage_symbols"].append("US.BAD")
        first["rules"][0]["entry_threshold"] = "999"
        first["episode_policy"]["emission"] = "mutated"
        second = futu_anomaly_policy_manifest()
        self.assertEqual(second["storage_symbols"], list(STORAGE_SYMBOLS))
        self.assertEqual(second["rules"][0]["entry_threshold"], "5")
        self.assertEqual(
            second["episode_policy"]["emission"],
            "once_per_us_eastern_market_date_per_rule",
        )


if __name__ == "__main__":
    unittest.main()
