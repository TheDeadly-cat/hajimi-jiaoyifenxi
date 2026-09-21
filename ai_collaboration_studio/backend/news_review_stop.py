"""Bounded stop provenance for the owned trial host; no exception messages."""
from __future__ import annotations

import re
import threading
import time

from .api_pilot import write_record
from .news_review_contracts import NewsReviewError


def bounded_code(value, fallback='unclassified_error'):
    return value if type(value) is str and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,99}', value) else fallback


class NewsReviewStop:
    def __init__(self, service, policy, controller, journal, stop_event, root):
        self.service, self.policy, self.controller = service, policy, controller
        self.journal, self.event, self.root = journal, stop_event, root
        self.events = []
        self.persistence_errors = []
        self.lock = threading.Lock()

    def request(self, kind, *, trigger, code='', runtime_status='', exception_type=''):
        with self.lock:
            value = {'version':'news_review_stop_v1', 'policy_id':self.policy['policy_id'],
                     'session_id':self.journal.session_id, 'stop_type':kind,
                     'wall_ms':self.service.clock(), 'trigger_thread':bounded_code(trigger),
                     'error_code':bounded_code(code) if code else '',
                     'exception_type':bounded_code(exception_type) if exception_type else '',
                     'runtime_status':bounded_code(runtime_status, 'unavailable'),
                     'window_reached':self.service.clock() >= self.policy['expires_at_ms']}
            if any(all(old[k] == value[k] for k in ('stop_type','trigger_thread','error_code')) for old in self.events):
                self.event.set()
                return
            self.events.append(value)
            # A separate exclusive receipt survives failure of the journal/DB.
            # It contains only bounded codes, identity and timing, never str(exc).
            try:
                write_record(self.root/f'stop-{self.journal.session_id}-{len(self.events)}.json', value)
            except Exception as exc:
                self.persistence_errors.append({'sink':'file','exception_type':bounded_code(type(exc).__name__)})
            try:
                self.journal.record('stop_requested', runtime_status=value['runtime_status'], stop=value)
            except Exception as exc:
                self.persistence_errors.append({'sink':'journal','exception_type':bounded_code(type(exc).__name__)})
            finally:
                self.controller.request_stop()
                self.event.set()

    def summary(self):
        fault = bool(self.persistence_errors or any(e['stop_type'] != 'window_elapsed' for e in self.events))
        completed = bool(self.events and self.events[0]['stop_type'] == 'window_elapsed'
                         and self.events[0]['window_reached'] and not fault)
        return {'stop_events':list(self.events), 'persistence_errors':list(self.persistence_errors),
                'work_completed':completed, 'exit_code':0 if completed else 2}

    def watch(self, holder, *, interval_seconds=1):
        last_sample = 0
        status = 'starting'
        try:
            while not self.event.wait(interval_seconds):
                self.service.check_clock()
                runtime = holder.get('runtime')
                snapshot = runtime.snapshot() if runtime else {}
                status = snapshot.get('status', 'starting')
                # Faults take precedence even if first noticed at the deadline.
                if self.controller.errors:
                    for lane, code in sorted(self.controller.errors.copy().items()):
                        self.request('worker_failure', trigger=lane, code=code, runtime_status=status)
                    return
                expired = self.service.clock() >= self.policy['expires_at_ms']
                window_stop = expired and snapshot.get('stop_reason_code') == 'NEWS_REVIEW_WINDOW_EXPIRED'
                if holder.get('ready') and (status in {'failed','stalled'} or status == 'stopped' and not window_stop):
                    self.request('runtime_failure', trigger='source_runtime', runtime_status=status,
                                 code=snapshot.get('last_fatal_error_code') or 'source_runtime_'+status)
                    return
                if time.monotonic()-last_sample >= 10:
                    self.journal.record('heartbeat', runtime_status=status)
                    last_sample = time.monotonic()
                if expired:
                    self.service.close_expired_work()
                    self.journal.record('window_closed', runtime_status=status)
                    self.request('window_elapsed', trigger='observer', runtime_status=status)
                    return
        except Exception as exc:
            self.controller.errors['observer'] = (exc.code if isinstance(exc, NewsReviewError) else 'observer_failed')
            self.request('observer_failure', trigger='observer', code=self.controller.errors['observer'],
                         runtime_status=status, exception_type=type(exc).__name__)
