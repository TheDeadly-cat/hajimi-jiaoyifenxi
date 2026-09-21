"""Two non-daemon workers, independent of the existing source collector."""
from __future__ import annotations

import threading
import time
from contextlib import closing

from .news_review_contracts import NewsReviewError
from .source_inbox_service import SourceInboxError
from .news_review_service import _policy, _window
from .source_poll_control import source_poll_authorization, SourcePollCancelled


class NewsReviewController:
    def __init__(self, service, policy_id):
        self.service, self.policy_id = service, policy_id
        self.stop_event = threading.Event()
        self.threads = []
        self.errors = {}
        self.lock = threading.Lock()

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
        self.stop_event.set()

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
        self.service._identity(policy)
        def check():
            self.service.owner.assert_held_for(self.service.store.path)
            self.service.check_clock()
            if self.stop_event.is_set():
                raise SourcePollCancelled("NEWS_REVIEW_STOPPED", "news review host stopped")
            with self.service.store._lock, closing(self.service.store._connect()) as db:
                row, p = _policy(db,self.policy_id)
                if self.service.clock() >= p['expires_at_ms']:
                    raise SourcePollCancelled('NEWS_REVIEW_WINDOW_EXPIRED', 'news review window elapsed')
                try:
                    _window(row,p,self.service.clock())
                except NewsReviewError as exc:
                    raise SourcePollCancelled("NEWS_REVIEW_POLICY_INACTIVE", "news review policy inactive") from exc
        return source_poll_authorization(check)

    def enrich_cycle(self):
        if self.stop_event.is_set():
            return
        self.service.check_clock()
        self.service.discover(self.policy_id)
        with self.service.store._lock, closing(self.service.store._connect()) as db:
            events = db.execute("SELECT * FROM news_review_events WHERE policy_id=? ORDER BY discovered_at,item_id",(self.policy_id,)).fetchall()
        for event in events:
            if self.stop_event.is_set():
                return
            # Checking all discovered events also notices a new immutable body
            # version supplied by an independently authorized manual refresh.
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
            with source_poll_authorization(self.service.check_clock):
                self.service.documents.run(job["id"],cancel_event=self.stop_event,
                                           deadline_monotonic_ms=int(time.monotonic()*1000)+12_000)
            self.service.queue(self.policy_id,job["item_id"])

    def model_cycle(self):
        if not self.stop_event.is_set():
            self.service.run_one(self.policy_id)

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
                self.stop_event.set()
            except Exception:
                self.errors[lane] = "worker_failed"
                self.stop_event.set()
