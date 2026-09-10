from __future__ import annotations

import sys
from pathlib import Path


sys.dont_write_bytecode = True
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    from backend.source_monitoring_soak_cli import main as soak_main

    return soak_main()


if __name__ == "__main__":
    raise SystemExit(main())
