from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    # Set isolation before importing any application module.  The migration
    # target is always the explicit --database argument handled by the gate;
    # this disposable configured path can never become the target by accident.
    with tempfile.TemporaryDirectory(
        prefix="ai-studio-migration-tool-runtime-"
    ) as temp_dir:
        project_root = Path(__file__).resolve().parents[1]
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        runtime_path = Path(temp_dir).resolve()
        os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
        os.environ["AI_STUDIO_RUNTIME_DIR"] = str(runtime_path)
        os.environ["AI_STUDIO_DATABASE_PATH"] = str(
            runtime_path / "tool-default-must-not-be-used.sqlite3"
        )
        for name in (
            "OPENAI_API_KEY",
            "DEEPSEEK_API_KEY",
            "ARK_API_KEY",
            "DOUBAO_API_KEY",
            "GLM_API_KEY",
            "ZHIPU_API_KEY",
            "ZHIPUAI_API_KEY",
        ):
            os.environ.pop(name, None)

        from backend.database_migration import main as migration_main

        return migration_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
