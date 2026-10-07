"""Two non-daemon workers, independent of the existing source collector."""
from __future__ import annotations

import threading
import time
from contextlib import closing, nullcontext

from .news_review_contracts import NewsReviewError, is_dual_policy
from .execution_boundary import TextRequestSendGate
from .source_inbox_service import SourceInboxError
from .news_review_service import _policy, _window
from .source_poll_control import source_poll_authorization, SourcePollCancelled


class NewsReviewController:
    # Discovery itself inserts at most 50 inbox rows per cycle. Fresh events have
    # their own allowance; historical catch-up cannot consume that allowance.
    NEW_EVENT_BATCH = 64
    DOCUMENT_VERSION_BATCH = 32
    HISTORY_BATCH = 16

    def __init__(self, service, policy_id):
        self.service, self.policy_id = service, policy_id
        self.send_gate = TextRequestSendGate()
        self.stop_event = self.send_gate.event
        self.threads = []
        self.errors = {}
        self.lock = threading.Lock()
        self._scan_initialized = False
        self._event_cursor = 0
        self._document_cursor = 0
        self._history_after = ''

    def start(self, *, recover=True):
        with self.lock:
            if self.threads:
                raise RuntimeError("news review controller already started")
            if recover:
                self.service.prepare_startup()
            self.threads = [threading.Thread(target=self._work,args=(lane,cycle),
                            name="news-review-"+lane,daemon=False) for lane,cycle in
                            (("evidence",self.enrich_cycle),("model",self.model_cycle))]
            for thread in self.threads:
                thread.start()

    def request_stop(self):
        self.send_gate.cancel()

    def bind_host_stop_event(self, event):
        self.send_gate.bind_host_stop_event(event)

    def stop(self, timeout=15):
        self.request_stop()
        deadline = time.monotonic()+timeout
        for thread in self.threads:
            if thread.ident is not None:
                thread.join(max(0,deadline-time.monotonic()))
        # Caller must retain the database owner while a provider is in flight.
        # Never perform recovery concurrently with the still-live worker.
        return not any(t.is_alive() for t in self.threads)

    def pause(self):
        return self.service.pause(self.policy_id)

    def snapshot(self):
        return {**self.service.snapshot(self.policy_id), "worker_errors":dict(self.errors),
                "stopping":self.stop_event.is_set(),
                "workers_alive":{t.name:t.is_alive() for t in self.threads}}

    def source_authorization(self, adapter_key):
        """Rechecked by shared poll controls at fetch/read boundaries, not just start."""
        if adapter_key not in {"sec_filings", "company_ir"}:
            raise NewsReviewError("source_scope_mismatch")
        with self.service.store._lock, closing(self.service.store._connect()) as db:
            _, policy = _policy(db,self.policy_id)
        try:
            self.service._identity(policy)
        except NewsReviewError as exc:
            if exc.code == 'policy_expired':
                raise SourcePollCancelled('NEWS_REVIEW_WINDOW_EXPIRED', 'news review window elapsed') from None
            raise
        def check():
            self.service.owner.assert_held_for(self.service.store.path)
            try:
                self.service.check_clock()
            except NewsReviewError as exc:
                if exc.code == 'policy_expired':
                    raise SourcePollCancelled('NEWS_REVIEW_WINDOW_EXPIRED', 'news review window elapsed') from None
                raise
            if self.stop_event.is_set():
                raise SourcePollCancelled("NEWS_REVIEW_STOPPED", "news review host stopped")
            with self.service.store._lock, closing(self.service.store._connect()) as db:
                row, p = _policy(db,self.policy_id)
                if self.service.window_reached(p):
                    raise SourcePollCancelled('NEWS_REVIEW_WINDOW_EXPIRED', 'news review window elapsed')
                try:
                    _window(row,p,self.service.clock(),self.service.store)
                except NewsReviewError as exc:
                    if exc.code == 'policy_expired':
                        raise SourcePollCancelled('NEWS_REVIEW_WINDOW_EXPIRED', 'news review window elapsed') from None
                    raise SourcePollCancelled("NEWS_REVIEW_POLICY_INACTIVE", "news review policy inactive") from exc
        def deadline():
            check()
            return self.service._dual_clock.reference.snapshot()['effective_elapsed_deadline_ms']
        return source_poll_authorization(check, deadline=deadline if is_dual_policy(policy) else None,
                                         expiry_code='NEWS_REVIEW_WINDOW_EXPIRED')

    def _initialize_scan(self):
        if self._scan_initialized:
            return
        # These cursors only prioritize work within this controller lifetime.
        # Durable discovery/paid-content claims remain in the existing ledger.
        # On restart, the bounded history lane still visits every stored event.
        with self.service.store._lock, closing(self.service.store._connect()) as db:
            self._event_cursor = db.execute("SELECT COALESCE(MAX(rowid),0) FROM news_review_events").fetchone()[0]
            recent_versions = db.execute("SELECT rowid FROM source_document_versions ORDER BY rowid DESC LIMIT ?",
                                         (self.DOCUMENT_VERSION_BATCH,)).fetchall()
            # A body may have arrived while the controller was down. Recheck a
            # bounded recent tail immediately rather than waiting for a complete
            # historical rotation. Existing content claims still prevent replay.
            self._document_cursor = recent_versions[-1][0]-1 if recent_versions else 0
        self._scan_initialized = True

    def _event_batch(self):
        with self.service.store._lock, closing(self.service.store._connect()) as db:
            db.execute("BEGIN")
            # Bound the physical append scan before filtering by policy, so rows
            # belonging to other policies cannot turn it into a full-table read.
            fresh = db.execute("SELECT rowid AS scan_rowid,* FROM news_review_events WHERE rowid>? ORDER BY rowid LIMIT ?",
                               (self._event_cursor, self.NEW_EVENT_BATCH)).fetchall()
            if fresh:
                self._event_cursor = fresh[-1]['scan_rowid']
            revisions = db.execute("SELECT rowid AS scan_rowid,item_id FROM source_document_versions WHERE rowid>? ORDER BY rowid LIMIT ?",
                                   (self._document_cursor, self.DOCUMENT_VERSION_BATCH)).fetchall()
            if revisions:
                self._document_cursor = revisions[-1]['scan_rowid']
            revision_events = []
            if revisions:
                item_ids = tuple(dict.fromkeys(row['item_id'] for row in revisions))
                marks = ','.join('?' for _ in item_ids)
                revision_events = db.execute(f"SELECT * FROM news_review_events WHERE policy_id=? AND item_id IN ({marks})",
                                             (self.policy_id, *item_ids)).fetchall()
            # The composite primary key supports this bounded range scan. A
            # wraparound keeps retries and observations of an old body version
            # fair without retaining an ever-growing in-memory event list.
            def history():
                return db.execute("SELECT * FROM news_review_events WHERE policy_id=? AND item_id>? ORDER BY item_id LIMIT ?",
                                  (self.policy_id, self._history_after, self.HISTORY_BATCH)).fetchall()
            historical = history()
            if not historical and self._history_after:
                self._history_after = ''
                historical = history()
            if historical:
                self._history_after = historical[-1]['item_id']
        seen, result = set(), []
        for event in (*fresh, *revision_events, *historical):
            if event['policy_id'] == self.policy_id and event['item_id'] not in seen:
                seen.add(event['item_id'])
                result.append(event)
        return result

    def enrich_cycle(self):
        if self.stop_event.is_set():
            return
        self.service.check_clock()
        self._initialize_scan()
        self.service.discover(self.policy_id)
        with self.service.store._lock, closing(self.service.store._connect()) as db:
            _, policy = _policy(db, self.policy_id)
        events = self._event_batch()
        for event in events:
            if self.stop_event.is_set():
                return
            # Fresh events and immutable body revisions are checked first; the
            # rotating history lane also notices a new successful observation of
            # a previously stored body and retries quota-limited document work.
            if self.service.queue(self.policy_id,event["item_id"]):
                continue
            if event["status"] == "WAITING_DOCUMENT":
                try:
                    self.service.request_document(self.policy_id,event["item_id"])
                except (NewsReviewError,SourceInboxError) as exc:
                    if exc.code not in {"DOCUMENT_RETRY_LATER","DOCUMENT_BUDGET_EXHAUSTED","document_budget_exhausted"}:
                        raise
                    # Wait for rolling quota/Retry-After without advancing or
                    # inventing a successful read. Absolute quota never resets.
                    with self.service.store._lock, closing(self.service.store._connect()) as db, db:
                        db.execute("UPDATE news_review_events SET error_code=? WHERE policy_id=? AND item_id=?",(exc.code,self.policy_id,event["item_id"]))
        with self.service.store._lock, closing(self.service.store._connect()) as db:
            job = db.execute("SELECT id,item_id FROM source_document_jobs WHERE session_id=? AND status='waiting' ORDER BY requested_at,rowid LIMIT 1",("news_review:"+self.policy_id,)).fetchone()
        if job and not self.stop_event.is_set():
            # Dual-policy document jobs establish their mandatory context in
            # DocumentEvidenceService, including when called independently.
            with (nullcontext() if is_dual_policy(policy) else source_poll_authorization(self.service.check_clock)):
                self.service.documents.run(job["id"],cancel_event=self.stop_event,
                                           deadline_monotonic_ms=int(time.monotonic()*1000)+12_000)
            self.service.queue(self.policy_id,job["item_id"])

    def model_cycle(self):
        if not self.stop_event.is_set():
            self.service.run_one(self.policy_id, send_gate=self.send_gate)

    def _work(self, lane, cycle):
        while not self.stop_event.wait(1):
            try:
                cycle()
            except NewsReviewError as exc:
                if exc.code == "policy_not_active":
                    continue
                if exc.code == "policy_expired":
                    return
                self.errors[lane] = exc.code
                self.request_stop()
            except Exception:
                self.errors[lane] = "worker_failed"
                self.request_stop()
