from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Back up and recover an ordinary crashed SQLite WAL session")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--database", required=True)
    prepare.add_argument("--recovery-directory", required=True)
    apply = commands.add_parser("apply")
    apply.add_argument("--database", required=True)
    apply.add_argument("--prepared", required=True)
    apply.add_argument("--authorize", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ai-studio-wal-recovery-tool-") as temporary:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        os.environ["AI_STUDIO_RUNTIME_DIR"] = temporary
        os.environ["AI_STUDIO_DATABASE_PATH"] = str(Path(temporary) / "unused-default.sqlite3")
        for name in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "ARK_API_KEY", "DOUBAO_API_KEY", "GLM_API_KEY",
                     "ZHIPU_API_KEY", "ZHIPUAI_API_KEY"):
            os.environ.pop(name, None)
        from backend.database_startup_recovery import prepare_startup_recovery, apply_startup_recovery
        from backend.instance_ownership import DatabaseInstanceOwner
        with DatabaseInstanceOwner(args.database) as owner:
            if args.command == "prepare":
                result = prepare_startup_recovery(args.database, args.recovery_directory, instance_owner=owner)
                output = {"prepared": str(Path(args.recovery_directory) / "prepared.json"),
                          "authorization_token": result["authorization_token"],
                          "source_opened_by_sqlite": False,
                          "logical_sha256": result["recovered_logical"]["logical_sha256"]}
            else:
                output = apply_startup_recovery(args.database, args.prepared, authorization_token=args.authorize,
                                               instance_owner=owner)
        print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
