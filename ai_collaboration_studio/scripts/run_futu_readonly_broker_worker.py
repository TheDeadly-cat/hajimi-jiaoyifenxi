"""Private isolated worker for the read-only Futu monitoring broker."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    root = str(project_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    from backend.source_monitoring.futu_readonly_broker import (
        run_futu_readonly_broker_worker,
    )

    return run_futu_readonly_broker_worker(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())

