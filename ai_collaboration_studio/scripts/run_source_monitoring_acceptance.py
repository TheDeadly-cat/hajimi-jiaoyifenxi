from __future__ import annotations

import sys


sys.dont_write_bytecode = True

_NON_ISOLATED_ERROR_JSON = (
    '{"command":"verify","error_code":'
    '"SOURCE_MONITORING_ACCEPTANCE_ISOLATED_PROCESS_REQUIRED",'
    '"ok":false,"overall_acceptance":"NOT_CLAIMED","safety":{'
    '"database_discovery_performed":false,"database_reads_performed":0,'
    '"database_writes_performed":0,"execution_capability":"none",'
    '"formal_rounds_created":0,"live_trading_allowed":false,'
    '"market_calls_performed":0,"model_calls_performed":0,'
    '"network_requests_performed":0,"provider_calls_performed":0},'
    '"source_acceptance_verdict":"FAIL",'
    '"version":"source_monitoring_acceptance_cli_v1"}\n'
)


def main() -> int:
    if sys.flags.isolated != 1:
        # Keep this failure independent of project path resolution and backend
        # imports so a non-isolated invocation cannot reach acceptance code.
        sys.stdout.write(_NON_ISOLATED_ERROR_JSON)
        return 2

    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    from backend.source_monitoring_acceptance_cli import main as acceptance_main

    return acceptance_main()


if __name__ == "__main__":
    raise SystemExit(main())
