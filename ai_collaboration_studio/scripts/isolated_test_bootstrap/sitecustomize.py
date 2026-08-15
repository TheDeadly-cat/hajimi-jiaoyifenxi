"""Fail-closed socket bootstrap for Python children of the isolated test runner.

Python imports ``sitecustomize`` before executing ``-c``, ``-m`` or a script.
This directory is placed first on ``PYTHONPATH`` only by
``run_backend_tests_isolated.py``.  A forbidden connection terminates the
child immediately, so a test cannot catch the socket error and accidentally
turn an external access attempt into a passing result.
"""

from __future__ import annotations

import atexit
import os


_GUARD = None
_AUDIT = None


def _close_guard() -> None:
    global _GUARD, _AUDIT
    guard = _GUARD
    _GUARD = None
    _AUDIT = None
    if guard is not None:
        guard.__exit__(None, None, None)


if os.environ.get("AI_STUDIO_TEST_NETWORK_GUARD") == "1":
    from scripts.run_backend_tests_isolated import (
        _CHILD_NETWORK_BLOCK_EXIT_CODE,
        isolated_backend_test_network_guard,
    )

    _GUARD = isolated_backend_test_network_guard(
        fatal_exit_code=_CHILD_NETWORK_BLOCK_EXIT_CODE
    )
    _AUDIT = _GUARD.__enter__()
    os.environ["AI_STUDIO_TEST_NETWORK_CHILD_GUARD_ACTIVE"] = str(os.getpid())
    atexit.register(_close_guard)
