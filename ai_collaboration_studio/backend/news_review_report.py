"""Durable run observations and scoped acceptance metrics, never billing claims."""
from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from contextlib import closing

from .decision_lineage import canonical_sha256
from .news_review_contracts import VERSION, encoded, freshness, require
from .news_review_service import _policy
from .provider_call_ledger import ProviderCallLedger
from .source_monitoring.state_repository import SourceMonitoringStateRepository


class NewsReviewJournal:
    def __init__(self, service, policy_id):
        self.service, self.policy_id = service, policy_id
        self.session_id = uuid.uuid4().hex

    def record(self, kind, *, runtime_status="", source_run_id=""):
        require(kind in {"session_started","heartbeat","session_stopped","source_poll","window_closed"}, "invalid_observation_kind")
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
                     "monotonic_ms":int(time.monotonic()*1000),"runtime_status":runtime_status,
                     "source_run_id":source_run_id,"source_run_sha256":canonical_sha256(run) if run else "",
                     "queue_counts":snapshot["job_counts"],"events_observed":snapshot["events_observed"],
                     "calls_reserved":snapshot["calls_reserved"],"documents_reserved":snapshot["documents_reserved"]}
            db.execute("INSERT INTO news_review_observations VALUES(?,?,?,?)",
                       (self.policy_id,value["sequence"],encoded(value),canonical_sha256(value)))

    def observe_source_cycle(self, observation):
        require(observation.get("state_recorded") is True and observation.get("provider_calls_performed") == 0
                and observation.get("formal_rounds_created") == 0, "source_observation_mismatch")
        self.record("source_poll",runtime_status="running",source_run_id=observation["run_id"])


def build_news_review_report(service, policy_id):
    snapshot = service.snapshot(policy_id)
    source_repository = SourceMonitoringStateRepository(service.store)
    with service.store._lock, closing(service.store._connect()) as db:
        row, p = _policy(db,policy_id)
        observations = db.execute("SELECT * FROM news_review_observations WHERE policy_id=? ORDER BY sequence",(policy_id,)).fetchall()
        events = db.execute("SELECT * FROM news_review_events WHERE policy_id=?",(policy_id,)).fetchall()
        jobs = db.execute("SELECT * FROM news_review_jobs WHERE policy_id=?",(policy_id,)).fetchall()
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
        elapsed = max(0,min(service.clock(),p["expires_at_ms"])-p["not_before_ms"])
        last_completed = max((r["completed_at_ms"] for r in selected),default=0)
        source_metrics[adapter] = {"poll_runs_observed":len(selected),"statuses":dict(status),
                                  "success_rate":status["SUCCEEDED"]/len(selected) if selected else None,
                                  "nominal_poll_opportunities":(elapsed+299_999)//300_000,
                                  "last_completed_at_ms":last_completed or None,
                                  "actual_http_request_count":None}
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
        if view["versions"]:
            coverage["has_body_version"] += 1
            if view["versions"][-1].get("body_located"):
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
    full_window = (p["expires_at_ms"]-p["not_before_ms"] == 86_400_000
                   and service.clock() >= p["expires_at_ms"])
    coverage_observed = bool(full_window and len(sessions) == 1 and len(heartbeat) >= 2880
        and heartbeat[0]["kind"] == "session_started" and heartbeat[-1]["kind"] == "window_closed"
        and heartbeat[0]["wall_ms"] <= p["not_before_ms"]+30_000
        and heartbeat[-1]["wall_ms"] >= p["expires_at_ms"] and gaps and max(gaps) <= 30_000
        and min(gaps) >= 0 and not clock_anomalies)
    latencies.sort()
    return {"version":VERSION,"policy_sha256":row["policy_sha256"],"snapshot":snapshot,
            "source_checks":source_metrics,"source_grants":source_grants,
            "source_runs_without_terminal_observation":[dict(r) for r in source_rows if r["run_id"] not in runs],
            "body_coverage":dict(coverage),
            "publish_to_discovery_ms":{"observed_count":len(latencies),
            "median":latencies[len(latencies)//2] if latencies else None,"maximum":max(latencies) if latencies else None},
            "queue_backlog":snapshot["job_counts"].get("QUEUED",0),"duplicate_review_attempts":duplicate_count,
            "unknown_results":snapshot["job_counts"].get("UNKNOWN",0),"call_ledger":attempts,
            "reserved_without_ledger_attempt":max(0,row["calls_reserved"]-len(attempts)),
            "continuity":{"session_count":len(sessions),"heartbeat_count":len(heartbeat),
            "clean_shutdown_recorded":bool(samples and samples[-1]["kind"] == "session_stopped"),
            "maximum_sample_gap_ms":max(gaps) if gaps else None,"clock_anomalies":clock_anomalies,
            "full_24h_window_elapsed":full_window,"continuous_window_observed":coverage_observed},
            "chain_sha256":previous,"supplier_bill_verified":False,
            "event_end_to_end_recorded":any(j["status"] in {"REVIEWED","MATERIAL_INSUFFICIENT"}
                and bool(j["attempt_id"]) for j in jobs),
            "independent_factual_verification":False,"release_approved":False}
