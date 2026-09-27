"""Freeze synthetic requests or export reference signoff, without network/DB access."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

APP = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--references")
    parser.add_argument("--signoff-template", action="store_true")
    args = parser.parse_args(argv)
    if args.signoff_template and args.references:
        parser.error("signoff template does not accept references")
    # Validate Git before imports; never replace the candidate verifier with a
    # fixture mock, and never read the local provider configuration.
    head = subprocess.check_output(["git", "-C", str(APP), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(APP), "status", "--porcelain"], text=True).strip()
    if head != args.candidate_sha or dirty:
        parser.error("candidate must be exact and clean")
    sys.path.insert(0, str(APP))
    from backend.path_identity import first_reparse_component
    output = Path(args.output)
    if (not output.is_absolute() or output.exists() or not output.parent.is_dir()
            or output.resolve().is_relative_to(APP.parent) or first_reparse_component(output) is not None):
        parser.error("output must be a new absolute path without reparse components")
    os.environ["AI_STUDIO_SKIP_LOCAL_ENV"] = "1"
    for key in ("OPENAI_API_KEY", "ARK_API_KEY", "DOUBAO_API_KEY", "DEEPSEEK_API_KEY", "QWEN_API_KEY",
                "DASHSCOPE_API_KEY", "GLM_API_KEY", "ZHIPU_API_KEY", "ZHIPUAI_API_KEY", "BAILIAN_TOKEN_PLAN_API_KEY"):
        os.environ.pop(key, None)
    blocked = []
    def deny_runtime_access(event, _args):
        if event.startswith("socket.") or event == "sqlite3.connect":
            blocked.append(event)
            raise RuntimeError("quality_preparation_runtime_access_forbidden")
    sys.addaudithook(deny_runtime_access)
    with tempfile.TemporaryDirectory(prefix="news-quality-prepare-") as temporary:
        os.environ["AI_STUDIO_RUNTIME_DIR"] = temporary
        os.environ["AI_STUDIO_DATABASE_PATH"] = str(Path(temporary) / "must-not-open.sqlite3")
        from backend.news_review_quality_preparation import prepare, signoff_template
        from backend.news_review_monitor import publish_json_once
        from backend.decision_lineage import canonical_sha256
        corpus_raw = (APP / "tests/fixtures/news_review_quality_v1.json").read_bytes()
        if args.signoff_template:
            value = signoff_template(corpus_raw)
        else:
            reference_raw = Path(args.references).read_bytes() if args.references else None
            value = prepare(corpus_raw, candidate_sha=head, reference_raw=reference_raw)
            value["candidate_verified_by_cli"] = True
            value["preparer_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            value["runtime_access_audit"] = {"network_requests": 0, "database_connections": 0, "blocked_attempts": blocked}
            if blocked:
                raise RuntimeError("quality_preparation_runtime_access_attempted")
            value["preparation_sha256"] = canonical_sha256(value)
        final_head = subprocess.check_output(["git", "-C", str(APP), "rev-parse", "HEAD"], text=True).strip()
        final_dirty = subprocess.check_output(["git", "-C", str(APP), "status", "--porcelain"], text=True).strip()
        if final_head != head or final_dirty:
            raise RuntimeError("candidate_changed_during_preparation")
        publish_json_once(output, value)
    print(json.dumps({"output": str(output), "candidate_sha": head,
                      "request_count": value.get("request_count", 0), "request_authorized": False,
                      "declared_approved_cases": value.get("references", {}).get("declared_approved_cases", 0)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
