"""Durable run observations and scoped acceptance metrics, never billing claims."""
from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from contextlib import closing

from .decision_lineage import canonical_sha256
from .document_evidence import current_document
from .news_review_contracts import VERSION, encoded, freshness, is_dual_policy, require
from .news_review_service import _policy
from .news_review_source_analysis import (analyze_source_runs, source_timing_standards,
                                         SOURCE_TIMING_STANDARD_VERSION)
from .provider_call_ledger import ProviderCallLedger
from .source_monitoring.state_repository import SourceMonitoringStateRepository


class NewsReviewJournal:
    def __init__(self, service, policy_id):
        self.service, self.policy_id = service, policy_id
        self.session_id = uuid.uuid4().hex

    def record(self, kind, *, runtime_status="", source_run_id="", stop=None):
        require(kind in {"session_started","heartbeat","session_stopped","source_poll","window_closed","stop_requested"}, "invalid_observation_kind")
        require((kind == 'stop_requested') == (stop is not None), 'invalid_stop_observation')
        require(type(runtime_status) is str and len(runtime_status) <= 80
                and type(source_run_id) is str and len(source_run_id) <= 200, "invalid_observation")
        self.service.owner.assert_held_for(self.service.store.path)
        run = None
        if source_run_id:
            require(kind == "source_poll", "invalid_observation")
            run = SourceMonitoringStateRepository(self.service.store).get_run(source_run_id)
            require(run is not None and run["adapter_key"] in {"sec_filings","company_ir"}, "source_run_mismatch")
        snapshot = self.service.snapshot(self.policy_id)
        with self.service.store._lock, closing(self.service.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT sequence,observation_sha256 FROM news_review_observations WHERE policy_id=? ORDER BY sequence DESC LIMIT 1",(self.policy_id,)).fetchone()
            value = {"version":VERSION,"policy_id":self.policy_id,"policy_sha256":snapshot["policy_sha256"],
                     "session_id":self.session_id,"sequence":previous[0]+1 if previous else 1,
                     "previous_sha256":previous[1] if previous else "","kind":kind,"wall_ms":self.service.clock(),
                     "monotonic_ms":(self.service.monotonic_ms() if is_dual_policy(snapshot['policy']) else int(time.monotonic()*1000)),"runtime_status":runtime_status,
                     "source_run_id":source_run_id,"source_run_sha256":canonical_sha256(run) if run else "",
                     "queue_counts":snapshot["job_counts"],"events_observed":snapshot["events_observed"],
                     "calls_reserved":snapshot["calls_reserved"],"documents_reserved":snapshot["documents_reserved"]}
            value['clock_diagnostics'] = self.service.clock_diagnostics()
            if stop is not None:
                require(stop['policy_id'] == self.policy_id and stop['session_id'] == self.session_id, 'stop_identity_mismatch')
                value['stop'] = stop
            db.execute("INSERT INTO news_review_observations VALUES(?,?,?,?)",
                       (self.policy_id,value["sequence"],encoded(value),canonical_sha256(value)))

    def observe_source_cycle(self, observation):
        require(observation.get("state_recorded") is True and observation.get("provider_calls_performed") == 0
                and observation.get("formal_rounds_created") == 0, "source_observation_mismatch")
        self.record("source_poll",runtime_status="running",source_run_id=observation["run_id"])


def build_news_review_report(service, policy_id, *, source_standards=None):
    standards = source_timing_standards(source_standards)
    snapshot = service.snapshot(policy_id)
    source_repository = SourceMonitoringStateRepository(service.store)
    with service.store._lock, closing(service.store._connect()) as db:
        row, p = _policy(db,policy_id)
        observations = db.execute("SELECT * FROM news_review_observations WHERE policy_id=? ORDER BY sequence",(policy_id,)).fetchall()
        events = db.execute("SELECT * FROM news_review_events WHERE policy_id=?",(policy_id,)).fetchall()
        jobs = db.execute("SELECT * FROM news_review_all_jobs WHERE policy_id=?",(policy_id,)).fetchall()
        duplicate_content = db.execute("""SELECT COALESCE(SUM(n-1),0) FROM (
            SELECT COUNT(*) AS n FROM news_review_all_jobs WHERE started_at!=0
            GROUP BY dedupe_key HAVING COUNT(*)>1)""").fetchone()[0]
        grants = db.execute("SELECT * FROM news_review_source_grants WHERE policy_id=?",(policy_id,)).fetchall()
        source_rows = db.execute("SELECT run_id,adapter_key,status FROM source_adapter_runs WHERE started_at_ms>=? AND started_at_ms<? AND adapter_key IN ('sec_filings','company_ir')",
                                 (p["not_before_ms"],p["expires_at_ms"])).fetchall()
    source_grants = []
    for grant_row in grants:
        grant = json.loads(grant_row["grant_json"])
        require(canonical_sha256(grant) == grant_row["grant_sha256"] and grant["policy_sha256"] == row["policy_sha256"],
                "source_grant_integrity")
        source_grants.append({"grant":grant,"status":grant_row["status"],"grant_sha256":grant_row["grant_sha256"]})
    samples, runs = [], {}
    previous = ""
    for sequence, observation in enumerate(observations,1):
        value = json.loads(observation["observation_json"])
        digest = canonical_sha256(value)
        require(digest == observation["observation_sha256"] and value["sequence"] == sequence
                and value["policy_id"] == policy_id and value["policy_sha256"] == row["policy_sha256"]
                and value["previous_sha256"] == previous, "observation_integrity")
        previous = digest
        if value["source_run_id"]:
            run = source_repository.get_run(value["source_run_id"])
            require(run is not None and canonical_sha256(run) == value["source_run_sha256"], "source_run_integrity")
            runs[value["source_run_id"]] = run
        samples.append(value)
    source_metrics = {}
    for adapter in ("sec_filings","company_ir"):
        selected = [r for r in runs.values() if r["adapter_key"] == adapter]
        status = Counter(r["status"] for r in selected)
        completed = len(selected)-status['RUNNING']
        elapsed = max(0,min(service.clock(),p["expires_at_ms"])-p["not_before_ms"])
        last_completed = max((r["completed_at_ms"] for r in selected),default=0)
        state = source_repository.get_state(adapter)
        source_metrics[adapter] = {"poll_runs_observed":len(selected),"statuses":dict(status),
                                  "success_rate":status["SUCCEEDED"]/completed if completed else None,
                                  "completed_poll_runs_observed":completed,
                                  "in_flight_excluded":status['RUNNING'],
                                  "success_rate_basis":"completed_adapter_polls_not_http_requests_or_announcement_capture",
                                  "nominal_poll_opportunities":(elapsed+299_999)//300_000,
                                  "last_completed_at_ms":last_completed or None,
                                  "observed_error_codes":dict(Counter(r['error_code'] for r in selected if r.get('error_code'))),
                                  "last_success_at_ms":state['last_success_at_ms'] if state else None,
                                  "last_error_code":state['last_error_code'] if state else None,
                                  "consecutive_failures":state['consecutive_failures'] if state else None,
                                  "next_due_at_ms":state['next_due_at_ms'] if state else None,
                                  "actual_http_request_count":None}
        try:
            observation_start = (p['clock_activation']['wall_ms'] if is_dual_policy(p) else p['not_before_ms'])
            source_metrics[adapter]['diagnostics'] = analyze_source_runs(selected,
                observed_until_ms=service.clock(), observed_since_ms=observation_start,
                maximum_initial_success_delay_ms=standards[adapter]['first_success_ms'],
                maximum_success_gap_ms=standards[adapter]['maximum_gap_ms'])
        except ValueError:
            # Clock regressions or incomplete metadata must not prevent the
            # existing stop/ledger report from being persisted.
            source_metrics[adapter]['diagnostics'] = {'available':False,'error_code':'SOURCE_DIAGNOSTIC_UNCONFIRMED'}
    coverage = Counter()
    latencies = []
    for event in events:
        item = service.documents.item(event["item_id"])
        fresh = freshness(item,service.clock())
        latency = fresh["publish_to_discovery_ms"]
        if latency is not None and latency >= 0:
            latencies.append(latency)
        view = service.documents.view(event["item_id"])
        coverage[view["status"]] += 1
        document = current_document(view)
        if document:
            coverage["has_body_version"] += 1
            if document.get("body_located"):
                coverage["main_body_located"] += 1
    attempts = ProviderCallLedger.resume(service.store,row["provider_run_id"]).attempts()
    counts = Counter(a["operation_target_id"] for a in attempts)
    duplicate_count = sum(max(0,n-1) for n in counts.values())
    known_jobs = {j["id"]:j for j in jobs}
    require(all(a["operation_target_id"] in known_jobs and a["operation_target_type"] == "news_event"
                and a["provider"] == "doubao" for a in attempts), "ledger_target_mismatch")
    heartbeat = [s for s in samples if s["kind"] in {"session_started","heartbeat","window_closed"}]
    sessions = {s["session_id"] for s in heartbeat}
    gaps = []
    clock_anomalies = 0
    for left,right in zip(heartbeat,heartbeat[1:]):
        delta = right["wall_ms"]-left["wall_ms"]
        gaps.append(delta)
        if right["session_id"] == left["session_id"]:
            monotonic_delta = right["monotonic_ms"]-left["monotonic_ms"]
            if monotonic_delta < 0 or abs(monotonic_delta-delta) > 2000:
                clock_anomalies += 1
    dual = is_dual_policy(p)
    clock_data = service.clock_diagnostics()
    if dual and clock_data.get('policy_sha256') != row['policy_sha256']:
        clock_data = next((s['clock_diagnostics'] for s in reversed(samples)
            if s.get('clock_diagnostics', {}).get('policy_sha256') == row['policy_sha256']),
            {'available': False, 'error_code': 'CLOCK_BINDING_EVIDENCE_UNAVAILABLE'})
    reference = clock_data.get('reference', {}) if dual else {}
    final_clock = reference.get('first_stop') or reference.get('latest') or {}
    end_reading = final_clock.get('reading') or {}
    elapsed_lower = None
    elapsed_upper = None
    if dual and end_reading:
        anchor = p['clock_activation']
        end_limit = reference.get('effective_elapsed_deadline_ms', end_reading['monotonic_before_ms'])
        elapsed_lower = max(0, min(end_reading['monotonic_before_ms'], end_limit)-anchor['monotonic_after_ms'])
        elapsed_upper = max(0, min(end_reading['monotonic_after_ms'], end_limit)-anchor['monotonic_before_ms'])
    expiry_reasons = {'absolute_deadline_reached', 'elapsed_deadline_reached'}
    authorized_end = (final_clock.get('stop_reason') in expiry_reasons if dual else service.window_reached(p))
    if dual:
        from .news_review_runtime_clock import clock_faults
        snapshot['expired'] = bool(snapshot['expired'] or authorized_end)
        gaps = [right['monotonic_ms']-left['monotonic_ms'] for left, right in zip(heartbeat, heartbeat[1:])]
        clock_anomalies = int(bool(reference.get('first_stop') and (
            final_clock.get('stop_reason') not in expiry_reasons or clock_faults(final_clock))))
    full_window = (p["expires_at_ms"]-p["not_before_ms"] == 86_400_000
                   and service.clock() >= p["expires_at_ms"])
    if dual:
        full_window = bool(p['expires_at_ms']-p['not_before_ms'] == 86_400_000
                           and elapsed_lower is not None and elapsed_lower >= 86_400_000)
    coverage_observed = bool(full_window and len(sessions) == 1 and len(heartbeat) >= 2880
        and heartbeat[0]["kind"] == "session_started" and heartbeat[-1]["kind"] == "window_closed"
        and heartbeat[0]["wall_ms"] <= p["not_before_ms"]+30_000
        and heartbeat[-1]["wall_ms"] >= p["expires_at_ms"] and gaps and max(gaps) <= 30_000
        and min(gaps) >= 0 and not clock_anomalies)
    authorized_coverage = coverage_observed
    if dual:
        authorized_coverage = bool(authorized_end and len(sessions) == 1 and len(heartbeat) >= 2880
            and heartbeat[0]['kind'] == 'session_started' and heartbeat[-1]['kind'] == 'window_closed'
            and 0 <= heartbeat[0]['monotonic_ms']-p['clock_activation']['monotonic_before_ms'] <= 30000
            and gaps and 0 <= min(gaps) and max(gaps) <= 30000 and not clock_anomalies)
        coverage_observed = bool(full_window and authorized_coverage)
    latencies.sort()
    stops = [s['stop'] for s in samples if s['kind'] == 'stop_requested']
    clean = bool(samples and samples[-1]['kind'] == 'session_stopped')
    work_completed = bool(stops and stops[0]['stop_type'] == 'window_elapsed'
        and stops[0]['window_reached'] and all(s['stop_type'] == 'window_elapsed' for s in stops))
    return {"version":VERSION,"policy_sha256":row["policy_sha256"],"snapshot":snapshot,
            "stop_events":stops,
            "outcome":{"work_completed":work_completed,"cleanup_clean":clean,
                       "acceptance_passed":False,"stop_provenance_available":bool(stops)},
            "source_checks":source_metrics,"source_grants":source_grants,
            "source_timing_standards":{"version":SOURCE_TIMING_STANDARD_VERSION,
                "basis":"caller_supplied" if source_standards is not None else "profile_diagnostic_defaults",
                "standards":standards,"sha256":canonical_sha256(standards),
                "monitoring_approval_verified":False,"acceptance_authority":False},
            "source_runs_without_terminal_observation":[dict(r) for r in source_rows if r["run_id"] not in runs],
            "body_coverage":dict(coverage),
            "publish_to_discovery_ms":{"observed_count":len(latencies),
            "median":latencies[len(latencies)//2] if latencies else None,"maximum":max(latencies) if latencies else None},
            "queue_backlog":snapshot["job_counts"].get("QUEUED",0),"duplicate_review_attempts":duplicate_count,
            "duplicate_content_reservations_all_policies":duplicate_content,
            "unknown_results":snapshot["job_counts"].get("UNKNOWN",0),"call_ledger":attempts,
            "reserved_without_ledger_attempt":max(0,row["calls_reserved"]-len(attempts)),
            "continuity":{"session_count":len(sessions),"heartbeat_count":len(heartbeat),
            "clean_shutdown_recorded":bool(samples and samples[-1]["kind"] == "session_stopped"),
            "maximum_sample_gap_ms":max(gaps) if gaps else None,"clock_anomalies":clock_anomalies,
            "full_24h_window_elapsed":full_window,"continuous_window_observed":coverage_observed,
            "authorized_window_coverage_observed":authorized_coverage,
            "sample_gap_clock_basis":"monotonic" if dual else "wall"},
            "clock_guard_diagnostics":clock_data,
            "clock_anomalies_basis":"bound_dual_clock" if dual else "adjacent_heartbeat_samples",
            "authorization_time":{"strategy":"dual_deadline" if dual else "cumulative_two_seconds",
                "authorized_window_end_observed":bool(authorized_end),
                "stop_reason":final_clock.get('stop_reason') if dual else None,
                "authorized_elapsed_lower_ms":elapsed_lower, "authorized_elapsed_upper_ms":elapsed_upper,
                "actual_full_24h_proven":full_window if dual else None},
            "chain_sha256":previous,"supplier_bill_verified":False,
            "event_end_to_end_recorded":any(j["status"] in {"REVIEWED","MATERIAL_INSUFFICIENT"}
                and bool(j["attempt_id"]) for j in jobs),
            "independent_factual_verification":False,"release_approved":False}
