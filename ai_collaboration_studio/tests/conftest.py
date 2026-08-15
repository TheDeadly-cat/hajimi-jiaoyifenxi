from __future__ import annotations

import os
import tempfile
from pathlib import Path


# backend.store owns a module-level default Store for the local server. Pytest
# imports application modules while collecting tests, so route that unavoidable
# initialization to one disposable database before any test module is imported.
# Individual tests remain free to create their own isolated StudioStore objects.
_PYTEST_RUNTIME = tempfile.TemporaryDirectory(
    prefix="ai-collaboration-studio-pytest-",
)
_PYTEST_RUNTIME_PATH = Path(_PYTEST_RUNTIME.name).resolve()

os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
os.environ["AI_STUDIO_RUNTIME_DIR"] = str(_PYTEST_RUNTIME_PATH)
os.environ["AI_STUDIO_DATABASE_PATH"] = str(
    _PYTEST_RUNTIME_PATH / "collection-default.sqlite3"
)


def pytest_unconfigure(config) -> None:  # noqa: ARG001
    _PYTEST_RUNTIME.cleanup()
