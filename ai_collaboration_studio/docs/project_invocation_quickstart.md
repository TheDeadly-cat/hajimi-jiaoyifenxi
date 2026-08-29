# Project invocation v1 quickstart

This contract is for trusted local projects that need to create one bounded Studio collaboration room and later read a portable result. Source implementation and isolated tests do not activate the formal desktop instance: schema migration, a dedicated signing secret, and a restart remain separate operator decisions.

## Discover the exact contracts

- `GET /api/integration/manifest` returns `studio_integration_manifest_v2`.
- `GET /api/plugin-registry/catalog/v3` returns exact-version, hash-chained plugin histories and sealed latest aliases.
- Neither endpoint proves that the project capability secret, MCP process, Provider, or formal database is ready.

## Build and seal an envelope

Callers should use `backend.project_invocation.seal_project_invocation_envelope` rather than reproduce canonical JSON and room-ID derivation. New domains that do not have a registered room template must use `template_id="open_collaboration"`; their domain and category may remain domain-specific. Unknown templates fail instead of silently falling back.

The v1 envelope carries source identity, content hash, byte count, domain schema hashes, retention, budgets and the explicit user-confirmation boundary. It intentionally does not carry or fetch raw input content.

Retention is enforced conservatively. `no_payload_retention` redacts the requested room title/objective for every classification, not only sensitive ones. `ephemeral_24h` and `bounded_days` persist an exact expiry for the invocation, return `410 PROJECT_INVOCATION_RETENTION_EXPIRED` at the boundary, and also avoid persisting the caller's title/objective from the start. Only `project_default` may retain those two display fields; sensitive classifications cannot select that policy. Hashes and bounded audit metadata remain so idempotency and tamper checks are still possible.

```python
from backend.project_invocation import (
    PROJECT_INVOCATION_ENVELOPE_VERSION,
    derive_project_invocation_room_id,
    seal_project_invocation_envelope,
)

caller_id = "bazi_desktop"
project_id = "bazi_case_workspace"
client_request_id = "case-20260826-0001"

envelope = seal_project_invocation_envelope({
    "version": PROJECT_INVOCATION_ENVELOPE_VERSION,
    "caller_id": caller_id,
    "project_id": project_id,
    "client_request_id": client_request_id,
    "room_id": derive_project_invocation_room_id(
        caller_id, project_id, client_request_id
    ),
    "source": {"item_id": "case_0001", "revision": "1"},
    "workflow_kind": "research",
    "result_profile": "research_report_v1",
    "room_spec": {
        "title": "Bazi research case",
        "objective": "Separate deterministic chart facts from advisory interpretation.",
        "domain": "bazi_research",
        "category": "Bazi research",
        "template_id": "open_collaboration",
        "capability_pack_ids": [],
    },
    "domain_context": {
        "schema_version": "bazi_context_v1",
        "schema_sha256": "<64 lowercase hex>",
        "payload_sha256": "<64 lowercase hex>",
    },
    "input_manifest": {
        "content_sha256": "<64 lowercase hex>",
        "content_bytes": 1234,
    },
    "data_handling": {
        "classification": "sensitive_personal",
        "retention_policy": "no_payload_retention",
        "retention_days": None,
    },
    "budget": {
        "max_provider_calls": 0,
        "max_context_bytes": 100000,
        "max_result_bytes": 200000,
    },
    "user_confirmation": {
        "required": True,
        "boundary": "before_room_creation",
    },
    "safety": {
        "execution_capability": "none",
        "live_trading_allowed": False,
        "can_autonomously_decide": False,
    },
})
```

## Mint only in the offline operator issuer

Do not distribute `AI_STUDIO_PROJECT_CAPABILITY_SIGNING_SECRET` to ordinary caller processes or put it in URLs, source files, command arguments, shell history, logs or HTTP responses. `backend.project_capability_issuer` is the local operator boundary: it reads one explicit sealed envelope and one secret-free registration policy, then delegates token construction and verification to the existing `ProjectCapabilityAuthorizer`. It does not open SQLite, start a service or contact a Provider, market source or remote endpoint. There is deliberately no anonymous mint HTTP endpoint.

The policy is a closed allowlist. Registrations must be sorted by `(caller_id, project_id)`; actions must also be sorted and may only use the two actions already implemented by the host:

```json
{
  "version": "project_capability_issuer_policy_v1",
  "projects": [
    {
      "caller_id": "bazi_desktop",
      "project_id": "bazi_case_workspace",
      "enabled": true,
      "allowed_actions": [
        "project_invocation.intake",
        "project_invocation.result.read"
      ],
      "max_ttl_seconds": 300
    }
  ]
}
```

Inspect and dry-run validate the envelope, request hash, derived room identity, registration, action and TTL without reading the signing secret or minting a token:

```powershell
python -m backend.project_capability_issuer inspect `
  --envelope "<sealed-envelope.json>" `
  --policy "<reviewed-issuer-policy.json>"

python -m backend.project_capability_issuer dry-run `
  --envelope "<sealed-envelope.json>" `
  --policy "<reviewed-issuer-policy.json>" `
  --action project_invocation.intake `
  --ttl-seconds 120
```

After reviewing that output, inject the signing secret into the operator process environment through the approved local secret mechanism, then supply the exact acknowledgement. Do not paste the secret into the command itself:

```powershell
python -m backend.project_capability_issuer mint `
  --envelope "<sealed-envelope.json>" `
  --policy "<reviewed-issuer-policy.json>" `
  --action project_invocation.intake `
  --ttl-seconds 120 `
  --acknowledgement APPROVE_PROJECT_INVOCATION_CAPABILITY
```

The default stdout JSON contains the bearer token beside a separate audit receipt. The receipt contains only token/JTI hashes, exact request binding, action, TTL, timestamps, policy/plan hashes and zero-I/O safety facts; it contains neither the bearer nor signing secret. `--output <new-file.json>` may be used to create one exclusive, non-overwriting file outside the source tree; its operating-system ACL is still inherited from the chosen parent directory, so the operator must choose a protected directory. `--secret-stdin` is available for an operator-controlled one-line secret input instead of the environment.

Least privilege requires a separate single-action token for `project_invocation.result.read`; do not mint a combined intake/read token. Each mint has a unique `jti`, but the current host does not maintain a token-consumption ledger: intake replay is idempotent and result reads may repeat until expiry. The issuer reports `single_use_consumption_enforced_by_host=false` rather than presenting a unique `jti` as one-time enforcement. Disabling a policy entry stops future issuance; it does not revoke a token that was already minted and has not expired.

## Call and poll

Send the sealed envelope as the entire JSON body of `POST /api/integration/project-invocations` with exactly one `Authorization: Bearer <capability>` header. A first creation returns 201; an exact idempotent replay returns 200 and the same room. A reused `client_request_id` with different semantics returns a conflict.

Read `GET /api/integration/project-invocations/{client_request_id}/result` with a capability containing `project_invocation.result.read`. Before verified work exists, the endpoint returns a valid `collaboration_result_v1` whose profile is pending/withheld. It does not invent findings, trigger a Provider, read a market, place an order or bet, export a file, or make the user decision.

The bootstrap UI token is invalid on both integration endpoints. Supplying both credentials is also rejected. Browser cross-origin writes, iframe embedding, arbitrary callback URLs and external code loading remain disabled.

## Domain mapping

| Caller | Workflow/profile | Template | Required deterministic boundary |
| --- | --- | --- | --- |
| Bazi | `research` / `research_report_v1` | `open_collaboration` | Versioned calendar, timezone, solar-term and chart receipt; sensitive no-payload retention |
| Trading | `decision` / `decision_v1` | `stock_research` or `market_research` | Point-in-time evidence and read-only research; no order/wallet/live permission |
| PPT | `artifact_authoring` / `artifact_draft_v1` | `open_collaboration` | PPTX ingest hash, render package and explicit user verification receipt |
| Football | `research` / `research_report_v1` | `football_research` | Match identity, kickoff UTC and evidence cutoff; no betting or uncalibrated win-rate claim |
