"""Single-batch reservation and admission checks; no provider or source I/O.

The executor binds each send to AuthorizedTextRequest and retains visible
provider output separately. This component's receipts remain caller-reported;
only the executor's correlated evidence supports observed admission claims.
"""
from __future__ import annotations

import copy
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import threading
import time

from .controlled_manual_review import verify_candidate
from .decision_lineage import canonical_sha256
from .news_review_clock import NewsReviewClock
from .news_review_contracts import MODEL
from .news_review_monitor import publish_json_once
from .news_review_quality_preparation import INPUT_RATE, MAX_CALLS, OUTPUT_RATE, prepare, require
from .path_identity import first_reparse_component
from .provider_call_ledger import ProviderCallLedger

VERSION = "news_review_quality_batch_plan_v1"
CLOCK_MODE = "strict_cumulative_two_seconds_v1"


def build_batch_plan(corpus_raw, reference_raw, *, candidate_sha, database_path,
                     room_id, not_before_ms, expires_at_ms):
    require(reference_raw is not None, "signed_reference_required")
    prepared = prepare(corpus_raw, candidate_sha=candidate_sha, reference_raw=reference_raw)
    require(prepared["references"]["declared_approved_cases"] == 36, "reference_signoff_incomplete")
    require(type(not_before_ms) is int and type(expires_at_ms) is int
            and 0 < not_before_ms < expires_at_ms <= not_before_ms + 10_800_000,
            "invalid_quality_window")
    path = Path(database_path)
    require(path.is_absolute() and first_reparse_component(path) is None
            and path.name == "studio.sqlite3" and path.parent.name == "data"
            and path.parent.parent.name.startswith("NewsQualityBenchmark"), "quality_database_required")
    require(type(room_id) is str and room_id.startswith("room_") and len(room_id) <= 85, "quality_room_required")
    plan = {"version": VERSION, "candidate_sha": candidate_sha,
            "database_path": str(path.resolve()), "room_id": room_id,
            "not_before_ms": not_before_ms, "expires_at_ms": expires_at_ms,
            "clock_mode": CLOCK_MODE, "resume_allowed": False,
            "human_reference_review_acknowledgement_required": True,
            "prepared": prepared}
    plan["plan_sha256"] = canonical_sha256(plan)
    return plan


class QualityBatchLedger:
    """An owner-bound, non-resumable batch with permanent request claims.

    The store lock serializes duplicate checking and reservation. The process
    owner excludes another process; the exclusive start receipt refuses a
    second batch even after an otherwise empty or interrupted first run.
    """
    def __init__(self, store, owner, *, corpus_raw, reference_raw, candidate_sha,
                 room_id, not_before_ms, expires_at_ms, approved_plan_sha256,
                 human_reference_review_acknowledged, wall_ms=None, monotonic_ms=None):
        self.store, self.owner = store, owner
        self.wall_ms = wall_ms or (lambda: int(time.time() * 1000))
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic() * 1000))
        self._plan = build_batch_plan(corpus_raw, reference_raw, candidate_sha=candidate_sha,
                                    database_path=store.path, room_id=room_id,
                                    not_before_ms=not_before_ms, expires_at_ms=expires_at_ms)
        require(approved_plan_sha256 == self._plan["plan_sha256"], "quality_approval_mismatch")
        require(human_reference_review_acknowledged is True, "human_reference_acknowledgement_missing")
        owner.assert_held_for(store.path)
        verify_candidate(candidate_sha)
        self._clock = NewsReviewClock(self.wall_ms, self.monotonic_ms)
        self._duration_limit_ms = expires_at_ms - self._clock.anchor[0]
        self._closed, self._stop_code = False, ""
        self._state_lock = threading.Lock()
        self._stop_codes = []
        self._requests = {r["id"]: copy.deepcopy(r) for r in self._plan["prepared"]["requests"]}
        self._inflight = None
        self._receipts = store.path.parent.parent / "quality-batch-receipts"
        self.admission_check()
        require(first_reparse_component(self._receipts) is None, "quality_receipt_path_changed")
        self._receipts.mkdir(exist_ok=True)
        self.owner.assert_held_for(store.path)
        with store._lock:
            with closing(store._connect()) as db:
                require(db.execute("SELECT COUNT(*) FROM provider_execution_runs").fetchone()[0] == 0,
                        "quality_database_not_fresh")
            publish_json_once(self._receipts / "start.json", {
                "version": VERSION, "phase": "claimed_no_resume", "plan_sha256": self._plan["plan_sha256"],
                "candidate_sha": candidate_sha, "claimed_at_ms": self.wall_ms(),
                "not_before_ms": not_before_ms, "expires_at_ms": expires_at_ms})
            self._ledger = ProviderCallLedger.create(store, room_id, scope="news_quality_benchmark_v1",
                client_request_id=self._plan["plan_sha256"], plan_hash=self._plan["plan_sha256"],
                max_calls=MAX_CALLS, kind_call_limits={"news_quality_review": MAX_CALLS})
            require(not self._ledger.attempts(), "quality_batch_not_fresh")

    @property
    def plan(self):
        return copy.deepcopy(self._plan)

    def _stop_once(self, code):
        # This lock never covers DB, file or network I/O. A late response or
        # persistence fault records its own outcome without replacing the
        # first reason that closed admission.
        with self._state_lock:
            if code not in self._stop_codes:
                self._stop_codes.append(code)
            if not self._closed:
                self._closed, self._stop_code = True, code

    def _require_open(self):
        with self._state_lock:
            require(not self._closed, self._stop_code or "quality_batch_closed")

    def admission_state(self):
        """Nonblocking with respect to persistence; safe for the host watchdog."""
        with self._state_lock:
            return {"closed": self._closed, "stop_code": self._stop_code,
                    "observed_stop_codes": list(self._stop_codes)}

    def stop(self, code):
        require(code in {"quality_operator_interrupted", "quality_worker_failed",
                        "quality_request_timeout", "quality_shutdown_incomplete",
                        "quality_response_persistence_failed", "quality_credential_echo"},
                "quality_stop_code_invalid")
        self._stop_once(code)

    def admission_check(self):
        """Bounded in-memory final check, suitable for the existing send gate."""
        self._require_open()
        sample = self._clock.sample()
        now = sample["wall_ms"]
        elapsed = sample["monotonic_since_anchor_ms"]
        if (not sample["accepted"] or elapsed is None or elapsed < 0):
            self._stop_once("quality_clock_changed")
        elif not self._plan["not_before_ms"] <= now < self._plan["expires_at_ms"]:
            self._stop_once("quality_window_expired")
        elif elapsed >= self._duration_limit_ms:
            self._stop_once("quality_duration_expired")
        self._require_open()

    def _preflight(self):
        self.owner.assert_held_for(self.store.path)
        verify_candidate(self._plan["candidate_sha"])
        require(first_reparse_component(self.store.path) is None
                and str(self.store.path.resolve()) == self._plan["database_path"], "quality_database_changed")
        self.admission_check()

    def reserve(self, request_id):
        with self.store._lock:
            self._preflight()
            require(self._inflight is None, "quality_request_inflight")
            require(request_id in self._requests, "quality_request_not_in_plan")
            attempts = self._ledger.attempts()
            require(not any(a["operation_target_id"] == request_id for a in attempts), "quality_request_already_claimed")
            require(all(a["provider"] == "doubao" and a["model"] == MODEL
                        and a["operation_target_type"] == "quality_request"
                        and a["operation_target_id"] in self._requests for a in attempts), "quality_attempt_identity_changed")
            attempt = self._ledger.reserve(kind="news_quality_review", provider="doubao", model=MODEL,
                                           target_type="quality_request", target_id=request_id,
                                           database_max_calls=MAX_CALLS)
            self._inflight = copy.deepcopy(attempt)
            return copy.deepcopy(attempt)

    def before_send(self, attempt_id):
        with self.store._lock:
            self._preflight()
            require(self._inflight is not None and self._inflight["id"] == attempt_id, "quality_attempt_not_inflight")
            attempts = self._ledger.attempts()
            require(any(a["id"] == attempt_id and a["status"] == "STARTED"
                        and a["operation_target_id"] == self._inflight["operation_target_id"] for a in attempts),
                    "quality_attempt_changed")

    def final_admission_check(self, attempt_id):
        """Bind the last in-memory check to the still-reserved attempt."""
        self.admission_check()
        require(self._inflight is not None and self._inflight["id"] == attempt_id,
                "quality_attempt_not_inflight")

    def finish(self, attempt_id, *, status, http_attempted, response_sha256="", usage=None):
        """Record a caller-reported result; never infer model quality from it.

        Missing/over-reservation usage after admission is UNKNOWN. Persist the
        receipt before completing the generic ledger; a partial write/finish
        leaves an unrecoverable batch, not an invitation to repeat a request.
        """
        with self.store._lock:
            self.owner.assert_held_for(self.store.path)
            require(first_reparse_component(self._receipts) is None, "quality_receipt_path_changed")
            require(self._inflight is not None and self._inflight["id"] == attempt_id, "quality_attempt_not_inflight")
            require(status in {"RESPONDED", "FAILED", "CANCELLED", "UNKNOWN"}
                    and type(http_attempted) is bool, "quality_outcome_invalid")
            require(not (status == "RESPONDED" and not http_attempted), "quality_response_without_send")
            request = self._requests[self._inflight["operation_target_id"]]
            known = (type(usage) is dict and set(usage) == {"input_tokens", "output_tokens"}
                     and all(type(v) is int and v >= 0 for v in usage.values())
                     and usage["input_tokens"] <= request["reserved_input_tokens"]
                     and usage["output_tokens"] <= request["reserved_output_tokens"])
            if http_attempted and not known:
                status = "UNKNOWN"
            require(type(response_sha256) is str and (not response_sha256 or len(response_sha256) == 64
                    and all(c in "0123456789abcdef" for c in response_sha256)), "quality_response_hash_invalid")
            require(status != "RESPONDED" or response_sha256, "quality_response_hash_missing")
            receipt = {"version": "news_quality_outcome_v1", "plan_sha256": self._plan["plan_sha256"],
                       "request_id": request["id"], "request_sha256": request["request_sha256"],
                       "document_sha256": request["document_sha256"], "provider_attempt_id": attempt_id,
                       "status": status, "http_attempted": http_attempted, "response_sha256": response_sha256,
                       "usage": usage if known else {}, "model_quality_verified": False,
                       "supplier_bill_verified": False, "outcome_basis": "caller_reported"}
            receipt["receipt_sha256"] = canonical_sha256(receipt)
            try:
                publish_json_once(self._receipts / (attempt_id + ".json"), receipt)
                self._ledger.finish(attempt_id, self._inflight["attempt_token"],
                                    status="ABANDONED" if status == "UNKNOWN" else status,
                                    error_code="quality_result_unknown" if status == "UNKNOWN" else "",
                                    usage=receipt["usage"])
            except BaseException:
                self._stop_once("quality_outcome_persistence_failed")
                raise
            self._inflight = None
            if status != "RESPONDED":
                self._stop_once("quality_" + status.lower())
            elif len(self._ledger.attempts()) == MAX_CALLS:
                self._stop_once("quality_batch_completed")
            return copy.deepcopy(receipt)

    def snapshot(self):
        with self.store._lock:
            self.owner.assert_held_for(self.store.path)
            attempts = self._ledger.attempts()
            claimed = [self._requests[a["operation_target_id"]] for a in attempts]
            with self._state_lock:
                stop = {"closed": self._closed, "stop_code": self._stop_code, "observed_stop_codes": list(self._stop_codes)}
            return {"plan_sha256": self._plan["plan_sha256"], "calls_reserved": len(claimed),
                    "tokens_reserved": sum(r["reserved_input_tokens"] + r["reserved_output_tokens"] for r in claimed),
                    "cost_reserved_cny": str(sum((r["reserved_input_tokens"] * INPUT_RATE
                        + r["reserved_output_tokens"] * OUTPUT_RATE for r in claimed), Decimal(0)) / 1_000_000),
                    **stop,
                    "inflight_attempt_id": self._inflight["id"] if self._inflight else None,
                    "real_execution_command_available": True}
