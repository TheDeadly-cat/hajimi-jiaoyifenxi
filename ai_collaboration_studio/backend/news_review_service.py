"""Native inbox review queue with immutable inputs and permanent reservations.

Importing/reading the inbox does not start this service. An owned host must
explicitly approve a bounded policy and run separate enrichment/model workers.
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from .controlled_manual_review import verify_candidate
from .decision_lineage import canonical_sha256
from .document_evidence import DocumentEvidenceService, bound_source, current_document
from .execution_boundary import (AuthorizedTextRequest, authorized_text_request,
                                 build_text_provider_request, text_generation_body)
from .news_review_contracts import (DISABLED, ENDPOINT, INSTRUCTIONS, MODEL, STRATEGY_SHA, VERSION,
                                    NewsReviewError, decimal, encoded, event_key, freshness,
                                    importance, job_key, require, review_document, validate_policy, validate_result)
from .path_identity import first_reparse_component
from .provider_call_ledger import ProviderCallLedger, normalized_token_usage
from .providers.doubao_provider import DoubaoProvider
from .source_inbox_service import SourceInboxError


_PAID_STOPS = frozenset({'unknown_result', 'review_failure', 'model_budget_exhausted'})


def _policy(db, policy_id):
    row = db.execute("SELECT * FROM news_review_policies WHERE id=?", (policy_id,)).fetchone()
    require(row is not None, "policy_not_found")
    p = validate_policy(json.loads(row["policy_json"]))
    require(canonical_sha256(p) == row["policy_sha256"] and p["policy_id"] == row["id"], "policy_integrity")
    return dict(row), p


def _window(row, p, now):
    require(type(now) is int and p["not_before_ms"] <= now < p["expires_at_ms"], "policy_expired")
    require(row["status"] == "ACTIVE", "policy_not_active")


def _execution_table(job, kind='jobs'):
    require(job['storage'] in {'original', 'successor'} and kind in {'jobs', 'receipts', 'links'}, 'invalid_execution_storage')
    return ('news_review_' if job['storage'] == 'original' else 'news_review_successor_') + kind


def _retire_expired_jobs(db, now):
    """Only definitely unsent executions can await a different authorization."""
    for entry in db.execute('SELECT id FROM news_review_policies').fetchall():
        row, p = _policy(db, entry['id'])
        reason = ('authorization_expired' if now >= p['expires_at_ms'] else
                  'authorization_revoked' if row['status'] == 'STOPPED' else '')
        if not reason:
            continue
        for table in ('news_review_jobs', 'news_review_successor_jobs'):
            db.execute(f"""UPDATE {table} SET status='CANCELLED',error_code=?,finished_at=?
                WHERE policy_id=? AND status='QUEUED' AND started_at=0 AND attempt_id='' AND http_attempted=0""",
                (reason, now, entry['id']))
        db.execute("""UPDATE news_review_events SET status='WAITING_AUTHORIZATION',error_code=?
            WHERE policy_id=? AND review_job_id IN (SELECT id FROM news_review_all_jobs
                WHERE status='CANCELLED' AND error_code IN ('authorization_expired','authorization_revoked'))""",
            (reason, entry['id']))
        code = 'DOCUMENT_'+reason.upper()
        db.execute("UPDATE source_document_jobs SET status='cancelled',error_code=?,completed_at=? WHERE session_id=? AND status='waiting'",
                   (code, now, 'news_review:'+entry['id']))
        db.execute("""UPDATE news_review_events SET status='DOCUMENT_CANCELLED',error_code=?
            WHERE policy_id=? AND review_job_id='' AND document_job_id IN
                (SELECT id FROM source_document_jobs WHERE status='cancelled' AND error_code=?)""",
            (code,entry['id'],code))


def check_document_send(store, db, job, now):
    """Mandatory for any runner executing a native-review document job."""
    row, p = _policy(db, job["session_id"][len("news_review:"):])
    _window(row, p, now)
    require(Path(p["database_path"]).resolve() == store.path.resolve(), "database_mismatch")
    event = db.execute("SELECT * FROM news_review_events WHERE policy_id=? AND item_id=?",
                       (row["id"], job["item_id"])).fetchone()
    require(event is not None and event["document_job_id"] == job["id"]
            and 0 < row["documents_reserved"] <= p["max_document_requests"]
            and job["expires_at"] == p["expires_at_ms"], "document_reservation_mismatch")
    verify_candidate(p["candidate_sha"])


def _request(generation, provider):
    body = text_generation_body(api="responses", **generation, json_output=True, thinking_disabled=True)
    http = build_text_provider_request(provider._base_url, "responses", body, headers={})
    require(http.full_url == ENDPOINT, "endpoint_mismatch")
    return http


class NewsReviewService:
    def __init__(self, store, providers, *, instance_owner, documents=None, clock=None, monotonic_ms=None):
        self.store, self.providers, self.owner = store, providers, instance_owner
        self.clock = clock or (lambda: int(time.time()*1000))
        self.monotonic_ms = monotonic_ms or (lambda: int(time.monotonic()*1000))
        self.clock_anchor = (self.clock(),self.monotonic_ms())
        self.documents = documents or DocumentEvidenceService(store, clock=self.clock)
        self._startup_recovered = False

    def prepare_startup(self):
        """Recover inherited state before this service session's approval.

        Construct a new service for each owned host session. Both the launcher
        and host call this gate; the host must not revoke a decision just made
        by approve/resume in the same startup.
        """
        self.owner.assert_held_for(self.store.path)
        with self.store._lock:
            if not self._startup_recovered:
                self.recover()

    def end_host_session(self):
        """Called only after all host workers have drained."""
        with self.store._lock:
            self._startup_recovered = False

    def close_expired_work(self):
        self.owner.assert_held_for(self.store.path)
        with self.store._lock, closing(self.store._connect()) as db, db:
            _retire_expired_jobs(db, self.clock())

    def check_clock(self):
        wall,monotonic = self.clock(),self.monotonic_ms()
        require(type(wall) is int and type(monotonic) is int
                and abs((wall-self.clock_anchor[0])-(monotonic-self.clock_anchor[1])) <= 2000,
                "clock_changed")

    def _identity(self, p):
        self.owner.assert_held_for(self.store.path)
        self.check_clock()
        path = Path(p["database_path"])
        require(path.is_absolute() and first_reparse_component(path) is None and path.resolve() == self.store.path.resolve(), "database_mismatch")
        require(path.name == "studio.sqlite3" and path.parent.name == "data"
                and path.parent.parent.name.startswith("NewsReviewTrial"), "isolated_database_required")
        verify_candidate(p["candidate_sha"])

    def _provider(self):
        require(getattr(self.providers, "uses_production_providers", False)
                and DISABLED.issubset(self.providers.disabled_provider_ids), "provider_registry_mismatch")
        provider = self.providers.get("doubao")
        require(type(provider) is DoubaoProvider and provider._base_url + "/responses" == ENDPOINT,
                "provider_route_mismatch")
        return provider

    def preview(self, policy):
        p = validate_policy(policy)
        self._identity(p)
        self._provider()
        with closing(self.store._connect()) as db:
            require(db.execute("SELECT 1 FROM rooms WHERE id=?", (p["room_id"],)).fetchone(), "room_not_found")
        return {"policy": p, "policy_sha256": canonical_sha256(p), "claim_status": "unverified_model_output",
                "cost_basis": "complete_http_utf8_bytes_plus_256_and_max_output",
                "supplier_spend_limit_verified": False, "supplier_bill_verified": False}

    def approve(self, policy, *, approved_policy_sha256):
        preview = self.preview(policy)
        p = preview["policy"]
        require(approved_policy_sha256 == preview["policy_sha256"], "approval_mismatch")
        now = self.clock()
        require(p["not_before_ms"] <= now < p["expires_at_ms"], "policy_expired")
        self.prepare_startup()
        # Keep the global store lock while moving between ledger and policy
        # transactions. A crash may leave an empty idempotent ledger, never calls.
        with self.store._lock:
            with closing(self.store._connect()) as db:
                existing = db.execute("SELECT * FROM news_review_policies WHERE id=?", (p["policy_id"],)).fetchone()
                if existing:
                    require(existing["policy_sha256"] == preview["policy_sha256"], "policy_id_conflict")
                    return self.snapshot(p["policy_id"])
                active = db.execute("SELECT id FROM news_review_policies WHERE status IN ('ACTIVE','PAUSED')").fetchall()
                for row in active:
                    _, old = _policy(db, row["id"])
                    require(now >= old["expires_at_ms"], "another_policy_active")
            ledger = ProviderCallLedger.create(self.store, p["room_id"], scope="news_event_review",
                client_request_id=p["policy_id"], plan_hash=preview["policy_sha256"],
                max_calls=p["max_model_calls"], skip_provider_ids=sorted(DISABLED))
            with closing(self.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                cursor = db.execute("SELECT COALESCE(MAX(rowid),0) FROM source_inbox_items").fetchone()[0]
                db.execute("INSERT INTO news_review_policies(id,policy_json,policy_sha256,provider_run_id,approved_at,cursor) VALUES(?,?,?,?,?,?)",
                           (p["policy_id"], encoded(p), preview["policy_sha256"], ledger.run_id, now, cursor))
        return self.snapshot(p["policy_id"])

    def pause(self, policy_id):
        self.owner.assert_held_for(self.store.path)
        with self.store._lock, closing(self.store._connect()) as db, db:
            _policy(db, policy_id)
            db.execute("""UPDATE news_review_policies SET status='PAUSED',
                stop_reason=CASE WHEN stop_reason='' THEN 'operator_pause' ELSE stop_reason END
                WHERE id=? AND status='ACTIVE'""", (policy_id,))
        return self.snapshot(policy_id)

    def prepare_sources(self, policy_id, runtime):
        """Apply the new policy's source authority through the existing state API.

        No manual-preview confirmation is fabricated. The native policy is the
        authorization for two sealed official sources and their first seed-only
        poll. The normal supervisor still builds and persists that real baseline.
        """
        from .source_monitoring.profiles import require_profile_registry, source_profile_manifest
        self.owner.assert_held_for(self.store.path)
        require(not runtime.snapshot().get("thread_alive"), "runtime_already_started")
        with self.store._lock:
            with closing(self.store._connect()) as db:
                row,p = _policy(db,policy_id)
                _window(row,p,self.clock())
            self._identity(p)
            settings = runtime.settings
            require(settings.source_profile == p["profile"] and settings.enabled and settings.auto_start
                    and settings.official_only and not settings.allow_readonly_market and not settings.dry_run
                    and settings.initial_mode == "seed_only" and not settings.trading_impact_rules_enabled,
                    "source_runtime_mismatch")
            registry,repository = runtime.scheduler.registry,runtime.repository
            require(repository.store.path.resolve() == self.store.path.resolve(), "database_mismatch")
            require_profile_registry(registry,p["profile"])
            for key in registry.adapter_keys:
                metadata = registry.metadata_for(key)
                state = repository.get_or_create_state(key,config_version=metadata.config_version)
                with closing(self.store._connect()) as db,db:
                    db.execute("BEGIN IMMEDIATE")
                    current,current_p = _policy(db,policy_id)
                    _window(current,current_p,self.clock())
                    existing = db.execute("SELECT * FROM news_review_source_grants WHERE policy_id=? AND adapter_key=?",(policy_id,key)).fetchone()
                    if existing:
                        grant = json.loads(existing["grant_json"])
                        require(canonical_sha256(grant) == existing["grant_sha256"]
                                and grant["policy_sha256"] == row["policy_sha256"]
                                and grant["config_version"] == metadata.config_version, "source_grant_integrity")
                        if existing["status"] != "PREPARED":
                            continue  # an operator's later disablement survives restart
                    else:
                        grant = {"version":VERSION,"policy_sha256":row["policy_sha256"],"adapter_key":key,
                                 "config_version":metadata.config_version,"state_version":state["state_version"],
                                 "checkpoint_sha256":state["checkpoint_sha256"],"initialization":"seed_only",
                                 "source_profile_sha256":source_profile_manifest(p["profile"])["scope_sha256"],
                                 "approved_at":self.clock()}
                        db.execute("INSERT INTO news_review_source_grants(policy_id,adapter_key,grant_json,grant_sha256) VALUES(?,?,?,?)",
                                   (policy_id,key,encoded(grant),canonical_sha256(grant)))
                # A crash after recording intent can finish the same CAS on
                # restart; a changed, disabled state is an operator withdrawal.
                status = "APPLIED"
                if not state["enabled"]:
                    if state["state_version"] != grant["state_version"]:
                        status = "WITHDRAWN"
                    else:
                        repository.set_enabled(key,config_version=metadata.config_version,enabled=True,
                                               expected_state_version=state["state_version"])
                with closing(self.store._connect()) as db,db:
                    db.execute("UPDATE news_review_source_grants SET status=? WHERE policy_id=? AND adapter_key=?",(status,policy_id,key))

    def resume(self, policy_id, *, approved_policy_sha256):
        self.owner.assert_held_for(self.store.path)
        self.prepare_startup()
        with self.store._lock, closing(self.store._connect()) as db, db:
            row, p = _policy(db, policy_id)
            self._identity(p)
            require(row["policy_sha256"] == approved_policy_sha256 and row["status"] == "PAUSED"
                    and row["stop_reason"] in ({"operator_pause", "restart_confirmation_required"} | _PAID_STOPS), "resume_not_allowed")
            require(p["not_before_ms"] <= self.clock() < p["expires_at_ms"], "policy_expired")
            # Resuming collection is not authorization to clear a paid-lane
            # stop. Unknown/failure/budget stops survive every operator pause.
            paid_stop = row['stop_reason'] if row['stop_reason'] in _PAID_STOPS else ''
            db.execute("UPDATE news_review_policies SET status='ACTIVE',stop_reason=? WHERE id=?", (paid_stop,policy_id))
        return self.snapshot(policy_id)

    def discover(self, policy_id):
        """Only events inserted after this policy's persisted starting cursor."""
        self.owner.assert_held_for(self.store.path)
        with self.store._lock, closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row, p = _policy(db, policy_id)
            _window(row, p, self.clock())
            rows = db.execute("SELECT rowid,id FROM source_inbox_items WHERE rowid>? ORDER BY rowid LIMIT 50", (row["cursor"],)).fetchall()
            for entry in rows:
                record = self.documents.item(entry["id"])
                try:
                    source = bound_source(record)
                except SourceInboxError as exc:
                    if exc.code != "DOCUMENT_SCOPE_UNSUPPORTED":
                        raise
                else:
                    db.execute("INSERT OR IGNORE INTO news_review_events(policy_id,item_id,event_key,discovered_at) VALUES(?,?,?,?)",
                               (policy_id, entry["id"], event_key(source), record["created_at"]))
                db.execute("UPDATE news_review_policies SET cursor=? WHERE id=?", (entry["rowid"], policy_id))
        return len(rows)

    def request_document(self, policy_id, item_id):
        self.owner.assert_held_for(self.store.path)
        with self.store._lock, closing(self.store._connect()) as db:
            row, p = _policy(db, policy_id)
            _window(row, p, self.clock())
            event = db.execute("SELECT * FROM news_review_events WHERE policy_id=? AND item_id=?", (policy_id, item_id)).fetchone()
            require(event is not None, "event_not_discovered")
            if event["document_job_id"] or event["status"] != "WAITING_DOCUMENT":
                return event["document_job_id"]
        self._identity(p)
        def reserve(db, job_id):
            row, current = _policy(db, policy_id)
            _window(row, current, self.clock())
            event = db.execute("SELECT * FROM news_review_events WHERE policy_id=? AND item_id=?", (policy_id, item_id)).fetchone()
            require(event and not event["document_job_id"], "document_already_reserved")
            require(row["documents_reserved"] < current["max_document_requests"], "document_budget_exhausted")
            db.execute("UPDATE news_review_events SET document_job_id=?,status='READING_DOCUMENT' WHERE policy_id=? AND item_id=?", (job_id, policy_id, item_id))
            db.execute("UPDATE news_review_policies SET documents_reserved=documents_reserved+1 WHERE id=?", (policy_id,))
        job = self.documents.request(item_id, session_id="news_review:"+policy_id, confirmation=True,
                                     expires_at=p["expires_at_ms"], before_reserve=reserve)
        # An already existing manual job/version is reusable, but is not claimed
        # or executed under a different authorization. Its own worker owns it.
        return job["id"] if job["session_id"] == "news_review:"+policy_id else ""

    def queue(self, policy_id, item_id):
        self.owner.assert_held_for(self.store.path)
        record = self.documents.item(item_id)
        view = self.documents.view(item_id)
        document = current_document(view)
        if document is None:
            if view["status"] in {"failed", "cancelled"}:
                state = "DOCUMENT_CANCELLED" if view["status"] == "cancelled" else "MATERIAL_INSUFFICIENT"
                with self.store._lock, closing(self.store._connect()) as db, db:
                    db.execute("UPDATE news_review_events SET status=?,error_code=? WHERE policy_id=? AND item_id=?",
                               (state, view["job"]["error_code"], policy_id, item_id))
            return None
        source = bound_source(record)
        with self.store._lock, closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row, p = _policy(db, policy_id)
            _window(row, p, self.clock())
            event = db.execute("SELECT * FROM news_review_events WHERE policy_id=? AND item_id=?", (policy_id, item_id)).fetchone()
            require(event and event["event_key"] == event_key(source), "event_not_discovered")
            key = job_key(source, document)
            _retire_expired_jobs(db, self.clock())
            previous = db.execute("""SELECT * FROM news_review_all_jobs WHERE dedupe_key=?
                ORDER BY (started_at!=0 OR attempt_id!='' OR http_attempted!=0) DESC,
                         (policy_id=?) DESC,created_at DESC,id DESC LIMIT 1""", (key,policy_id)).fetchone()
            claim = db.execute('SELECT job_id FROM news_review_content_claims WHERE dedupe_key=?', (key,)).fetchone()
            if claim:
                previous = db.execute('SELECT * FROM news_review_all_jobs WHERE id=?', (claim['job_id'],)).fetchone()
                require(previous is not None and previous['dedupe_key'] == key, 'content_claim_integrity')
            successor = bool(previous and not claim and previous['policy_id'] != policy_id
                and previous['status'] == 'CANCELLED'
                and previous['error_code'] in {'authorization_expired','authorization_revoked'}
                and not previous['started_at'] and not previous['attempt_id'] and not previous['http_attempted'])
            if previous and not successor:
                db.execute(f"INSERT OR IGNORE INTO {_execution_table(previous, 'links')} VALUES(?,?,?,?)", (policy_id,item_id,previous["id"],self.clock()))
                db.execute("UPDATE news_review_events SET review_job_id=?,status='LINKED_REVIEW' WHERE policy_id=? AND item_id=?",
                           (previous["id"], policy_id, item_id))
                return previous["id"]
            evidence = {"event_id": item_id, "event_key": event_key(source), "headline": record["item"]["headline"],
                        "document": review_document(document), "importance": importance(record, document),
                        "freshness": freshness(record, self.clock()), "claim_status": "external_unverified"}
            generation = {"instructions": INSTRUCTIONS, "input_text": encoded(evidence), "model": MODEL,
                          "max_output_tokens": p["max_output_tokens"]}
            http = _request(generation, self._provider())
            byte_count = len(http.data)
            input_tokens = byte_count+256
            tokens = input_tokens+p["max_output_tokens"]
            cost = (input_tokens*decimal(p["rate_card"]["input_per_million"])
                    + p["max_output_tokens"]*decimal(p["rate_card"]["output_per_million"]))/1_000_000
            insufficient = (byte_count > p["max_request_bytes"] or not document.get("body_located")
                            or any("截断" in w or "结束标记" in w for w in document.get("warnings", [])))
            if insufficient:
                # No paid task is frozen until usable evidence fits the policy.
                # A later complete version with the same normalized text may
                # become reviewable; an existing paid task is still reused above.
                db.execute("UPDATE news_review_events SET status='MATERIAL_INSUFFICIENT',error_code='evidence_incomplete_or_too_large' WHERE policy_id=? AND item_id=?",
                           (policy_id,item_id))
                return None
            job_id = ("news_review_successor_"+canonical_sha256({'content':key,'policy':row['policy_sha256']})
                      if successor else "news_review_"+key)
            storage = {'storage':'successor' if successor else 'original'}
            db.execute(f"""INSERT INTO {_execution_table(storage)}(id,dedupe_key,policy_id,item_id,document_version_id,
                       strategy_sha256,input_json,input_sha256,request_sha256,request_bytes,tokens_reserved,
                       cost_reserved,created_at,priority,status,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (job_id,key,policy_id,item_id,document["id"],STRATEGY_SHA,encoded(generation),
                        canonical_sha256(generation),hashlib.sha256(http.data).hexdigest(),byte_count,tokens,str(cost),self.clock(),
                        0 if evidence["importance"]["level"] == "high" else 1,
                        "QUEUED", ""))
            db.execute("UPDATE news_review_events SET review_job_id=?,status='LINKED_REVIEW',error_code='' WHERE policy_id=? AND item_id=?", (job_id,policy_id,item_id))
            db.execute(f"INSERT INTO {_execution_table(storage, 'links')} VALUES(?,?,?,?)", (policy_id,item_id,job_id,self.clock()))
        return job_id

    def _verified_job(self, db, job_id):
        row = db.execute("SELECT * FROM news_review_all_jobs WHERE id=?", (job_id,)).fetchone()
        require(row is not None, "job_not_found")
        generation = json.loads(row["input_json"])
        require(canonical_sha256(generation) == row["input_sha256"] and row["strategy_sha256"] == STRATEGY_SHA,
                "job_integrity")
        policy, p = _policy(db, row["policy_id"])
        require(set(generation) == {"instructions", "input_text", "model", "max_output_tokens"}
                and generation["instructions"] == INSTRUCTIONS and generation["model"] == MODEL
                and generation["max_output_tokens"] == p["max_output_tokens"], "generation_mismatch")
        source = json.loads(generation["input_text"])
        record = self.documents.item(row["item_id"])
        versions = self.documents.view(row["item_id"])["versions"]
        document = next((v for v in versions if v["id"] == row["document_version_id"]), None)
        require(document and source["document"] == review_document(document) and source["event_id"] == row["item_id"]
                and row["dedupe_key"] == job_key(bound_source(record), document), "document_binding_mismatch")
        http = _request(generation, self._provider())
        require(hashlib.sha256(http.data).hexdigest() == row["request_sha256"]
                and len(http.data) == row["request_bytes"], "request_binding_mismatch")
        return dict(row), policy, p, generation, document

    def run_one(self, policy_id, *, send_gate=None):
        """One principal-model request, with no retry/fallback or held DB lock."""
        if send_gate is not None and send_gate.cancelled:
            return False
        self.owner.assert_held_for(self.store.path)
        provider = self._provider()
        if not provider.status().get("configured"):
            return False
        with self.store._lock:
            with closing(self.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row, p = _policy(db, policy_id)
                _window(row, p, self.clock())
                if row["stop_reason"]:
                    return False  # paid lane stopped; collection/enrichment can continue
                if db.execute("SELECT 1 FROM news_review_all_jobs WHERE status='RUNNING'").fetchone():
                    return False
                next_job = db.execute("SELECT id FROM news_review_all_jobs WHERE policy_id=? AND status='QUEUED' ORDER BY priority,created_at,id LIMIT 1", (policy_id,)).fetchone()
                if not next_job:
                    return False
                job, row, p, generation, document = self._verified_job(db, next_job["id"])
                require(not job["started_at"] and not job["attempt_id"] and not job["http_attempted"], "job_already_reserved")
                claimed = db.execute("SELECT tokens_reserved,cost_reserved FROM news_review_all_jobs WHERE policy_id=? AND started_at>0",(policy_id,)).fetchall()
                require(row["calls_reserved"] == len(claimed)
                        and row["tokens_reserved"] == sum(j[0] for j in claimed)
                        and Decimal(row["cost_reserved"]) == sum((Decimal(j[1]) for j in claimed),Decimal(0)), "budget_integrity")
                self._identity(p)
                cost = Decimal(row["cost_reserved"])+Decimal(job["cost_reserved"])
                if (row["calls_reserved"] >= p["max_model_calls"]
                        or row["tokens_reserved"]+job["tokens_reserved"] > p["max_total_tokens"]
                        or cost > decimal(p["spend_limit_cny"])):
                    db.execute("UPDATE news_review_policies SET stop_reason='model_budget_exhausted' WHERE id=?", (policy_id,))
                    return False
                require(provider._api_key not in job["input_json"], "credential_in_input")
                require(not db.execute('SELECT 1 FROM news_review_content_claims WHERE dedupe_key=?', (job['dedupe_key'],)).fetchone(), 'content_already_claimed')
                if send_gate is not None and send_gate.cancelled:
                    return False
                # One permanent content claim across every authorization. This
                # transaction also consumes the new policy's own counters.
                db.execute('INSERT INTO news_review_content_claims VALUES(?,?,?)', (job['dedupe_key'],job['id'],self.clock()))
                db.execute("UPDATE news_review_policies SET calls_reserved=calls_reserved+1,tokens_reserved=tokens_reserved+?,cost_reserved=? WHERE id=?",
                           (job["tokens_reserved"],str(cost),policy_id))
                db.execute(f"UPDATE {_execution_table(job)} SET status='RUNNING',started_at=? WHERE id=?", (self.clock(),job["id"]))
            ledger = ProviderCallLedger.resume(self.store, row["provider_run_id"])
            attempt = ledger.reserve(kind="news_event_review", provider="doubao", model=MODEL,
                                     target_type="news_event", target_id=job["id"])
            with closing(self.store._connect()) as db, db:
                db.execute(f"UPDATE {_execution_table(job)} SET attempt_id=? WHERE id=?", (attempt["id"],job["id"]))

        def before_send():
            require(send_gate is None or not send_gate.cancelled, 'host_stopped_before_send')
            self._identity(p)
            with self.store._lock, closing(self.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                current, auth, current_p, _, _ = self._verified_job(db, job["id"])
                _window(auth, current_p, self.clock())
                require(current["status"] == "RUNNING" and current["attempt_id"] == attempt["id"]
                        and not current["http_attempted"] and not auth["stop_reason"], "send_reservation_mismatch")
                attempts = ledger.attempts()
                require(len(attempts) == auth["calls_reserved"] and any(
                    a["id"] == attempt["id"] and a["status"] == "STARTED"
                    and a["operation_target_type"] == "news_event" and a["operation_target_id"] == job["id"]
                    and a["provider"] == "doubao" and a["model"] == MODEL for a in attempts), "ledger_binding_mismatch")
                claim = db.execute('SELECT job_id FROM news_review_content_claims WHERE dedupe_key=?', (job['dedupe_key'],)).fetchone()
                require(claim is not None and claim['job_id'] == job['id'], 'content_claim_integrity')

        bound = AuthorizedTextRequest(ENDPOINT,job["request_sha256"],p["max_request_bytes"],240,
                                      before_send=before_send,send_gate=send_gate)
        result, usage, cost, error = None, {}, None, ""
        status = "FAILED"
        response_digest = ""
        try:
            with authorized_text_request(bound):
                response = provider.generate_json(**generation)
            require(bound.attempted, "http_not_attempted")
            require(provider._api_key not in encoded({"content":response.content,"usage":response.usage}), "credential_echo")
            response_digest = hashlib.sha256(response.content.encode()).hexdigest()
            usage = normalized_token_usage(response.usage)
            known_usage = (type(usage) is dict and not any(k.startswith("usage_") for k in usage)
                           and all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens","output_tokens")))
            require(known_usage, "usage_unknown")
            usage = {k: usage[k] for k in ("input_tokens", "output_tokens")}
            cost = (usage["input_tokens"]*decimal(p["rate_card"]["input_per_million"])
                    + usage["output_tokens"]*decimal(p["rate_card"]["output_per_million"]))/1_000_000
            require(usage["input_tokens"] <= job["request_bytes"]+256
                    and usage["output_tokens"] <= p["max_output_tokens"], "reported_usage_exceeds_reservation")
            require(response.ok and response.provider == "doubao" and response.reported_model == MODEL
                    and response.response_status == "completed" and not response.refused
                    and not response.incomplete_reason, "response_not_complete")
            result = validate_result(response.content, document)
            status = "MATERIAL_INSUFFICIENT" if result["assessment"] == "material_insufficient" else "REVIEWED"
        except Exception as exc:
            error = exc.code if isinstance(exc, NewsReviewError) else "review_failed"
            if not bound.attempted and send_gate is not None and send_gate.cancelled:
                status, error = "CANCELLED", "host_stopped_before_send"
            elif bound.attempted and (cost is None or error == "reported_usage_exceeds_reservation"):
                status = "UNKNOWN"
        receipt = {"version":VERSION,"job_id":job["id"],"policy_sha256":row["policy_sha256"],
                   "input_sha256":job["input_sha256"],"request_sha256":job["request_sha256"],
                   "document_version_id":job["document_version_id"],"provider_attempt_id":attempt["id"],
                   "status":status,"http_attempted":bound.attempted,"error_code":error,
                   "result":result,"response_content_sha256":response_digest,
                   "usage":usage if cost is not None else {},"cost_estimate_cny":str(cost) if cost is not None else None,
                   "supplier_bill_verified":False,"claim_status":"unverified_model_output"}
        ledger.finish(attempt["id"],attempt["attempt_token"], status="RESPONDED" if result else "FAILED",
                      error_code=error,usage=receipt["usage"])
        with self.store._lock, closing(self.store._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(f"INSERT INTO {_execution_table(job, 'receipts')} VALUES(?,?,?,?)", (job["id"],encoded(receipt),canonical_sha256(receipt),self.clock()))
            # RUNNING + attempt_id + the permanent claim already make a crash
            # conservative UNKNOWN. Persist actual admission only after it is
            # known, so pre-send cancellation never needs to undo a send flag.
            db.execute(f"UPDATE {_execution_table(job)} SET status=?,finished_at=?,error_code=?,http_attempted=? WHERE id=?",
                       (status,self.clock(),error,int(bound.attempted),job["id"]))
            if error and status != "CANCELLED":
                db.execute("UPDATE news_review_policies SET stop_reason=? WHERE id=?", ("unknown_result" if status == "UNKNOWN" else "review_failure",policy_id))
        return True

    def recover(self):
        """Call once before starting workers, with the same database owner held."""
        self.owner.assert_held_for(self.store.path)
        with self.store._lock:
            with closing(self.store._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                interrupted = db.execute("SELECT DISTINCT policy_id FROM news_review_all_jobs WHERE status='RUNNING'").fetchall()
                for entry in interrupted:
                    db.execute("UPDATE news_review_policies SET stop_reason='unknown_result' WHERE id=?", (entry[0],))
                for table in ('news_review_jobs', 'news_review_successor_jobs'):
                    db.execute(f"UPDATE {table} SET status='UNKNOWN',error_code='process_interrupted',finished_at=? WHERE status='RUNNING'", (self.clock(),))
                _retire_expired_jobs(db, self.clock())
                policies = db.execute("SELECT id,provider_run_id FROM news_review_policies").fetchall()
                for entry in policies:
                    row, p = _policy(db, entry["id"])
                    if row["status"] == "ACTIVE" and not p["resume_within_window"]:
                        db.execute("UPDATE news_review_policies SET status='PAUSED',stop_reason=? WHERE id=?",
                                   (row['stop_reason'] or 'restart_confirmation_required',entry['id']))
                # 'waiting' has never entered the fetch lane. Keep its original
                # reservation even when restart requires confirmation. Every
                # send rechecks the original policy; fetching is never replayed.
                db.execute("UPDATE source_document_jobs SET status='cancelled',error_code='DOCUMENT_INTERRUPTED',completed_at=? WHERE session_id LIKE 'news_review:%' AND status='fetching'", (self.clock(),))
                db.execute("""UPDATE news_review_events SET status='DOCUMENT_CANCELLED',error_code=(
                    SELECT error_code FROM source_document_jobs WHERE id=document_job_id)
                    WHERE document_job_id IN (SELECT id FROM source_document_jobs WHERE status='cancelled')
                    AND review_job_id=''""")
            for entry in policies:
                ProviderCallLedger.resume(self.store,entry["provider_run_id"]).abandon_started(error_code="news_review_interrupted")
            self._startup_recovered = True

    def snapshot(self, policy_id):
        with self.store._lock, closing(self.store._connect()) as db:
            row, p = _policy(db,policy_id)
            counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM news_review_all_jobs WHERE policy_id=? GROUP BY status",(policy_id,))}
            events = db.execute("SELECT COUNT(*) FROM news_review_events WHERE policy_id=?",(policy_id,)).fetchone()[0]
        return {"version":VERSION,"policy":p,"policy_sha256":row["policy_sha256"],"state":row["status"],
                "expired":self.clock() >= p["expires_at_ms"],"paid_stop_reason":row["stop_reason"],
                "documents_reserved":row["documents_reserved"],"calls_reserved":row["calls_reserved"],
                "tokens_reserved":row["tokens_reserved"],"cost_reserved_cny":row["cost_reserved"],
                "events_observed":events,"job_counts":counts,"supplier_bill_verified":False,
                "observation":"new_events_observed" if events else "no_new_event_observed"}


def item_review_projection(store, item_id, *, clock=None):
    """Read-only sidecar projection; never changes inbox state, severity or rounds."""
    documents = DocumentEvidenceService(store)
    record = documents.item(item_id)
    view = documents.view(item_id)
    latest_document = current_document(view)
    current_key = job_key(bound_source(record), latest_document) if latest_document else None
    now = (clock or (lambda: int(time.time()*1000)))()
    with store._lock, closing(store._connect()) as db:
        events = db.execute("SELECT * FROM news_review_events WHERE item_id=? ORDER BY discovered_at,policy_id",(item_id,)).fetchall()
        policies = [_policy(db,row[0]) for row in db.execute("SELECT id FROM news_review_policies WHERE status='ACTIVE'")]
        observation_until = max((p["expires_at_ms"] for row,p in policies if now < p["expires_at_ms"]),default=0)
        jobs = db.execute("SELECT DISTINCT j.* FROM news_review_all_jobs j JOIN news_review_all_links e ON j.id=e.job_id WHERE e.item_id=? ORDER BY j.created_at,j.id",(item_id,)).fetchall()
        reviews = []
        for job in jobs:
            generation = json.loads(job["input_json"])
            require(canonical_sha256(generation) == job["input_sha256"], "job_integrity")
            evidence = json.loads(generation["input_text"])
            original_versions = documents.view(job["item_id"])["versions"]
            require(evidence["document"] in [review_document(v) for v in original_versions]
                    and event_key(bound_source(record)) == evidence["event_key"], "document_binding_mismatch")
            receipt_row = db.execute("SELECT * FROM news_review_all_receipts WHERE job_id=?",(job["id"],)).fetchone()
            receipt = json.loads(receipt_row["receipt_json"]) if receipt_row else None
            if receipt:
                require(canonical_sha256(receipt) == receipt_row["receipt_sha256"]
                        and receipt["job_id"] == job["id"] and receipt["input_sha256"] == job["input_sha256"]
                        and receipt["request_sha256"] == job["request_sha256"], "receipt_integrity")
                if receipt.get("result") is not None:
                    validate_result(encoded(receipt["result"]),evidence["document"])
            doc = evidence["document"]
            reviews.append({"id":job["id"],"state":job["status"],"error_code":job["error_code"],
                            "original_event_id":job["item_id"],"document_version_id":job["document_version_id"],
                            "importance":evidence["importance"],"coverage":{"scope":doc["scope"],"warnings":doc["warnings"],
                            "attachment_reading":doc["attachment_reading"],"paragraph_count":len(doc["paragraphs"])},
                            "receipt":receipt,"claim_status":"unverified_model_output"})
    current_coverage = ({"scope":latest_document["scope"],"warnings":latest_document["warnings"],
                         "attachment_reading":latest_document["attachment_reading"],
                         "paragraph_count":len(latest_document["paragraphs"])} if latest_document else None)
    current_job_id = next((job["id"] for job in reversed(jobs)
                           if current_key and job["dedupe_key"] == current_key), None)
    current_review = next((r for r in reviews if r["id"] == current_job_id), None)
    current_state = current_review["state"] if current_review else (events[-1]["status"] if events else "UNREVIEWED")
    if current_state == "LINKED_REVIEW":
        current_state = "UNREVIEWED"
    if events and events[-1]["status"] in {"MATERIAL_INSUFFICIENT", "DOCUMENT_CANCELLED", "WAITING_AUTHORIZATION"}:
        current_state = events[-1]["status"]
    return {"version":VERSION,"item_id":item_id,"importance":importance(record,latest_document),"freshness":freshness(record,now),
            "coverage":current_coverage,
            "current_document_version_id":latest_document["id"] if latest_document else None,
            "current_review_id":current_review["id"] if current_review else None,
            "observation_until":observation_until,
            "state":current_state,
            "reviews":reviews,"claim_status":"external_unverified"}
