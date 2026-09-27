"""Prepare a fixed synthetic model benchmark; no execution or approval authority.

Reference answers are carried in a separate signoff sheet and never projected
into requests. This is intentionally not an import into the live news inbox.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from decimal import Decimal

from .decision_lineage import canonical_sha256
from .document_evidence import PARSER, extract_document
from .execution_boundary import build_text_provider_request, text_generation_body
from .news_review_contracts import (
    ENDPOINT, INSTRUCTIONS, MODEL, STRATEGY_SHA, encoded, freshness,
    importance, review_document,
)
from .news_review_quality_scorecard import RUBRIC

CORPUS_SHA256 = "8c2706423a9563a5dd0c9789ededed918bf1fa9a2bf98c6f7c9073d850c6fe3d"
VERSION = "news_review_quality_preparation_v1"
SIGNOFF_VERSION = "news_review_quality_reference_signoff_v1"
INTENDED_USE = "synthetic_material_model_only_quality_evaluation"
OBSERVED_AT_MS = 1790463867701
MAX_CALLS, MAX_BYTES, MAX_OUTPUT = 36, 4096, 1400
MAX_TOKENS, MAX_COST = 207072, Decimal("2.50")
INPUT_RATE, OUTPUT_RATE = Decimal("6"), Decimal("30")


def require(condition, code):
    if not condition:
        raise ValueError(code)


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate_json_key")
            value[key] = item
        return value
    require(type(raw) is bytes and len(raw) <= 2_000_000, "input_size_invalid")
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: require(False, "nonfinite_json"))


def _corpus(raw):
    require(hashlib.sha256(raw).hexdigest() == CORPUS_SHA256, "fixed_corpus_changed")
    value = strict_json(raw)
    require(len(value["cases"]) == 36 and all(c["synthetic"] is True for c in value["cases"]),
            "corpus_scope_invalid")
    return value


def signoff_template(corpus_raw):
    corpus = _corpus(corpus_raw)
    return {"version": SIGNOFF_VERSION, "corpus_sha256": CORPUS_SHA256,
            "intended_use": INTENDED_USE, "rubric": RUBRIC.copy(),
            "pass_rule": "each_model_output_at_least_9_and_zero_critical_failures",
            "cases": [{"case_id": case["id"], "case_sha256": canonical_sha256(case),
                       "reference": {k: copy.deepcopy(v) for k, v in case["reference_draft"].items()
                                     if k not in {"human_reviewer", "human_approved_at"}},
                       "approved": False, "reviewer": None, "reviewed_at": None}
                      for case in corpus["cases"]]}


def _signoffs(corpus_raw, reference_raw):
    template = signoff_template(corpus_raw)
    if reference_raw is None:
        return template, 0
    value = strict_json(reference_raw)
    require(type(value) is dict and set(value) == set(template), "reference_fields_invalid")
    for key in ("version", "corpus_sha256", "intended_use", "rubric", "pass_rule"):
        require(canonical_sha256(value[key]) == canonical_sha256(template[key]), "reference_scope_mismatch")
    expected = {case["case_id"]: case for case in template["cases"]}
    require(type(value["cases"]) is list and len(value["cases"]) == len(expected), "reference_cases_missing")
    seen, count = set(), 0
    for entry in value["cases"]:
        require(type(entry) is dict and set(entry) == set(template["cases"][0]), "reference_case_invalid")
        case_id = entry["case_id"]
        require(type(case_id) is str and case_id in expected and case_id not in seen, "reference_case_identity_invalid")
        seen.add(case_id)
        require(entry["case_sha256"] == expected[case_id]["case_sha256"], "reference_case_hash_mismatch")
        reference = entry["reference"]
        require(type(reference) is dict and set(reference) == set(expected[case_id]["reference"]), "reference_answer_invalid")
        require(reference["assessment"] in {"reviewed", "material_insufficient"}
                and reference["importance"] in {"high", "normal", "uncertain"}, "reference_answer_invalid")
        for field in ("must_preserve", "numeric_unit_period_check"):
            require(type(reference[field]) is str and 0 < len(reference[field].strip()) <= 8000,
                    "reference_answer_invalid")
        for field in ("supported_facts", "forbidden_inferences"):
            require(type(reference[field]) is list and len(reference[field]) <= 40
                    and all(type(v) is str and 0 < len(v.strip()) <= 8000 for v in reference[field]),
                    "reference_answer_invalid")
        require(type(entry["approved"]) is bool, "reference_approval_invalid")
        if entry["approved"]:
            require(type(entry["reviewer"]) is str and 0 < len(entry["reviewer"].strip()) <= 200,
                    "reference_reviewer_missing")
            require(type(entry["reviewed_at"]) is str, "reference_time_invalid")
            try:
                reviewed = datetime.fromisoformat(entry["reviewed_at"].replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("reference_time_invalid") from exc
            require(reviewed.tzinfo is not None, "reference_time_invalid")
            count += 1
        else:
            require(entry["reviewer"] is None and entry["reviewed_at"] is None, "unsigned_reference_has_attribution")
    return value, count


def _material(case, body, observed_at_ms):
    # Reuse the native HTML parser; synthetic identity remains explicit. No
    # official URL, source run, document fetch or live discovery is fabricated.
    ending = "" if case["body_mode"] == "truncated" else "</body></html>"
    markup = ("<html><body><p>Item 8.01 Other Events</p><p>" + html.escape(body)
              + "</p><p>Synthetic offline filing for contract evaluation. This document does not describe an actual issuer event.</p>"
              + ending)
    if case["body_mode"] == "missing":
        markup = "<html><body></body></html>"
    parsed = extract_document(markup.encode("utf-8"), "text/html", {"kind": "sec", "scope": "synthetic_main_html"})
    identity = canonical_sha256({"case": case["id"], "body": parsed["body_text_sha256"], "parser": PARSER})
    document = {"format": "synthetic_quality_document_v1", "id": "quality_document_" + identity,
                "parser_version": PARSER, "origin": "synthetic_fixture", **parsed}
    document["paragraphs"] = [{"id": document["id"] + f":p{i+1:04d}", "text": block}
                              for i, block in enumerate(document.pop("blocks"))]
    published = observed_at_ms - case["age_hours"] * 3_600_000
    record = {"created_at": observed_at_ms, "item": {
        "headline": "Synthetic official-news review fixture", "entities": [],
        "published_at": datetime.fromtimestamp(published / 1000, timezone.utc).isoformat()}}
    evidence = {"event_id": "quality_" + case["id"], "event_key": canonical_sha256({"synthetic_case": case["id"]}),
                "headline": record["item"]["headline"], "document": review_document(document),
                "importance": importance(record, document), "freshness": freshness(record, observed_at_ms),
                "claim_status": "synthetic_not_an_actual_event"}
    return document, evidence


def prepare(corpus_raw, *, candidate_sha, reference_raw=None, observed_at_ms=OBSERVED_AT_MS):
    """Return a deterministic draft with whole-request bytes and signoff binding.

    Even complete declared signatures confer no network, execution or release
    authority. A separate reviewed executor and new approval are required.
    """
    require(type(candidate_sha) is str and re.fullmatch(r"[a-f0-9]{40}", candidate_sha), "candidate_invalid")
    require(type(observed_at_ms) is int and 0 < observed_at_ms < 253000000000000, "synthetic_time_invalid")
    corpus = _corpus(corpus_raw)
    reference, signed = _signoffs(corpus_raw, reference_raw)
    requests, cases = [], []
    for case in corpus["cases"]:
        bodies = [case["body"]]
        if case["workflow"] == "duplicate":
            bodies.append(case["body"])
        elif case["workflow"] in {"revision", "restore"}:
            revised = case["body"].replace("USD 10 million", "USD 11 million")
            require(revised != case["body"], "revision_fixture_unchanged")
            bodies.append(revised)
            if case["workflow"] == "restore":
                bodies.append(case["body"])
        observed, existing = [], {}
        for body in bodies:
            document, evidence = _material(case, body, observed_at_ms)
            version_sha = canonical_sha256(document)
            if version_sha in existing:
                observed.append({"document_sha256": version_sha, "action": "reuse",
                                 "request_id": existing[version_sha]})
                continue
            if not document["body_located"] or any("截断" in w or "结束标记" in w for w in document["warnings"]):
                observed.append({"document_sha256": version_sha, "action": "do_not_send",
                                 "reason": "evidence_incomplete", "request_id": None})
                continue
            generation = {"instructions": INSTRUCTIONS, "input_text": encoded(evidence),
                          "model": MODEL, "max_output_tokens": MAX_OUTPUT}
            payload = text_generation_body(api="responses", **generation, json_output=True, thinking_disabled=True)
            request = build_text_provider_request(ENDPOINT.removesuffix("/responses"), "responses", payload, headers={})
            raw = request.data
            require(len(raw) <= MAX_BYTES, "request_budget_exceeded_no_truncation")
            digest = hashlib.sha256(raw).hexdigest()
            request_id = "quality_request_" + digest
            existing[version_sha] = request_id
            requests.append({"id": request_id, "case_id": case["id"], "document_sha256": version_sha,
                             "request_sha256": digest, "request_utf8": raw.decode("utf-8"),
                             "request_bytes": len(raw), "reserved_input_tokens": len(raw) + 256,
                             "reserved_output_tokens": MAX_OUTPUT})
            observed.append({"document_sha256": version_sha, "action": "send_once", "request_id": request_id})
        cases.append({"case_id": case["id"], "workflow": case["workflow"], "observations": observed})
    require(len(requests) == MAX_CALLS and len({r["id"] for r in requests}) == MAX_CALLS, "request_inventory_invalid")
    input_tokens = sum(r["reserved_input_tokens"] for r in requests)
    output_tokens = sum(r["reserved_output_tokens"] for r in requests)
    cost = (input_tokens * INPUT_RATE + output_tokens * OUTPUT_RATE) / 1_000_000
    require(input_tokens + output_tokens <= MAX_TOKENS and cost <= MAX_COST, "total_budget_exceeded")
    return {"version": VERSION, "candidate_sha": candidate_sha, "candidate_verified_by_cli": False,
            "corpus_sha256": CORPUS_SHA256, "synthetic_observed_at_ms": observed_at_ms,
            "parser_version": PARSER, "strategy_sha256": STRATEGY_SHA, "model": MODEL, "endpoint": ENDPOINT,
            "intended_use": INTENDED_USE, "case_count": len(cases), "request_count": len(requests),
            "requests": requests, "cases": cases,
            "references": {"file_sha256": hashlib.sha256(reference_raw).hexdigest() if reference_raw else None,
                           "content_sha256": canonical_sha256(reference), "declared_approved_cases": signed,
                           "status": "all_signoffs_declared" if signed == 36 else "human_signoff_pending",
                           "human_identity_independently_verified": False},
            "reservation": {"input_tokens": input_tokens, "output_tokens": output_tokens,
                            "total_tokens": input_tokens + output_tokens, "estimated_cny": str(cost),
                            "max_calls": MAX_CALLS, "max_request_bytes": MAX_BYTES, "max_output_tokens": MAX_OUTPUT,
                            "max_total_tokens": MAX_TOKENS, "max_estimated_cny": str(MAX_COST),
                            "input_cny_per_million": str(INPUT_RATE), "output_cny_per_million": str(OUTPUT_RATE),
                            "prices_independently_verified": False, "supplier_bill_limit": False},
            "request_authorized": False, "executor_verified": False, "real_provider_calls": 0,
            "natural_event_acceptance": False, "model_quality_verified": False}
