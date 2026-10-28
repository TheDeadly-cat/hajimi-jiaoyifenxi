"""One process-wide SEC request gate shared by news and research tasks."""
from contextlib import contextmanager
import threading
import time

_lock = threading.Lock()
_last_request = 0.0


@contextmanager
def sec_request_slot(min_interval=0.11):
    global _last_request
    with _lock:
        remaining = max(0.11, min_interval) - (time.monotonic() - _last_request)
        if remaining > 0:
            time.sleep(remaining)
        try:
            yield
        finally:
            _last_request = time.monotonic()
