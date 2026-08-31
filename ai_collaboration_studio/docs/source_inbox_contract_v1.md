# Source inbox contract v1

`backend.source_inbox_contracts` is the side-effect-free intake boundary for
GitHub, CI, ChatGPT Scheduled Task, and future domain adapters. It parses and
normalizes untrusted external output without opening a database, resolving a
hostname, fetching a URL, calling a Provider, reading a market, or granting an
execution capability.

The contract itself remains side-effect free. The host integration is now
implemented separately in `backend.source_inbox_service` and
`backend.http_server`: it persists imports atomically, exposes a global inbox,
requires an explicit read-only acknowledgement before attachment, and can
produce a round **draft** only. It never creates or starts a formal round and
never calls a Provider or market interface.

This is a source-code capability, not evidence that the formal desktop database
has been migrated or that the formal service is running. Formal activation still
requires the database migration gate and a separately reviewed exact
authorization.

## Implemented host workflow

```text
POST /api/monitoring/imports/chatgpt
GET  /api/monitoring/inbox
GET  /api/monitoring/events/{item_id}
POST /api/monitoring/events/{item_id}/acknowledge
POST /api/monitoring/events/{item_id}/attach
POST /api/monitoring/events/{item_id}/round-draft
```

All writes reuse the loopback-only host guard and the in-memory UI session
token. Attachment requires the exact current `state_version` and the explicit
statement “已阅，不代表事实确认”. A target room is never inferred or
preselected by the server. The draft endpoint stores a sealed launch-plan
preview with `formal_round_created=false`, zero Provider/market calls, and a
fresh user confirmation still required before any existing formal round launch
flow can be entered.

## Public API

```python
from backend.source_inbox_contracts import accept_source_import

packet, receipt = accept_source_import(
    raw_json,
    received_at_ms=server_observed_epoch_ms,
)
```

Lower-level integrations may use:

- `parse_source_import_json(raw)`
- `normalize_source_import_packet(value, received_at_ms=...)`
- `canonicalize_source_url(value)`
- `project_source_item_fingerprint(item)`
- `build_source_import_receipt(...)`

`accept_source_import` returns an immutable-value projection only. The default
receipt state is `AWAITING_USER`.

## Input packet

The root object has version `source_import_packet_v1` and exactly these fields:

```json
{
  "version": "source_import_packet_v1",
  "source_channel": "chatgpt_scheduled_task",
  "source_key": "github_ci_watch",
  "external_run_id": "2026-08-28T13:00:00Z-github-ci-watch",
  "checked_at": "2026-08-28T13:03:00Z",
  "cutoff_at": "2026-08-28T13:00:00Z",
  "meaningful_change": true,
  "items": [],
  "generation": {
    "channel": "chatgpt_scheduled_task",
    "model": "",
    "cost": {
      "status": "unavailable",
      "amount": null,
      "currency": "",
      "usage_source": "subscription_unavailable"
    },
    "correlated_output": true
  }
}
```

`checked_at`, `cutoff_at`, model identity, cost, item/source timestamps, and
recommended routes remain `external_unverified`. The server-observed
`received_at_ms` is separate. `cutoff_at` must not be later than either
`checked_at` or the server receipt time.

`meaningful_change=false` requires an empty `items` array. A true value requires
at least one item.

## Source item

Each item has version `project_source_item_v1` and exactly these fields:

```json
{
  "version": "project_source_item_v1",
  "external_item_id": "github-run-100",
  "item_type": "ci_run_failure",
  "severity": "high",
  "occurred_at": "2026-08-28T12:55:00Z",
  "published_at": "2026-08-28T12:56:00Z",
  "entities": [
    {
      "kind": "repository",
      "id": "TheDeadly-cat/hajimi-jiaoyifenxi",
      "label": "hajimi-jiaoyifenxi"
    }
  ],
  "headline": "Isolated validation failed",
  "summary": "The workflow reported a failing unit-test step.",
  "facts": [
    {
      "claim": "The workflow conclusion is failure.",
      "source_indexes": [0]
    }
  ],
  "sources": [
    {
      "url": "https://github.com/TheDeadly-cat/hajimi-jiaoyifenxi/actions/runs/100",
      "publisher": "GitHub",
      "source_type": "official_platform",
      "published_at": "2026-08-28T12:56:00Z",
      "content_sha256": "0000000000000000000000000000000000000000000000000000000000000001"
    }
  ],
  "impact_hypotheses": [
    {
      "statement": "The current revision may not satisfy isolated checks.",
      "affected_area": "release readiness",
      "time_horizon": "before next publication",
      "source_indexes": [0],
      "confidence": 0.72
    }
  ],
  "unknowns": ["The exact failing assertion has not been imported."],
  "confidence": 0.93,
  "recommended_route": "open_round_draft",
  "extensions": {
    "github_v1": {
      "workflow": "isolated-validation",
      "run_status": "failure"
    }
  }
}
```

The core is deliberately domain-neutral. Trading-only fields such as
`bullish_implications` and `bearish_implications` are not accepted. Adapter data
belongs under a bounded, versioned extension key such as `github_v1`, `ci_v1`,
or `trading_v1`.

The normalized item adds only server-owned fields:

- `server_fingerprint_version`
- `server_fingerprint`
- `external_claims_verification="external_unverified"`

Clients must not submit a fingerprint. The v1 fingerprint binds item type,
canonical occurrence time, normalized headline, entity identities, canonical
source URLs, and supplied content hashes. A store should place a uniqueness
constraint on `(server_fingerprint_version, server_fingerprint)` and classify a
replay as `DUPLICATE`; it must not silently discard a hash collision.

The continuous-monitoring channels `official_source_monitor` and
`futu_anomaly_monitor` are reserved for the in-process
`source_monitoring_worker`. Local-user and ChatGPT/manual imports receive a
403 before transaction start if they claim either channel. Manual JSON import
remains available through its non-reserved channels and cannot manufacture
official-monitor provenance by choosing a `source_key` label.

The worker actor is an in-process routing boundary, not a sandbox against
arbitrary Python already executing inside the trusted host. Dynamic third-party
code execution is outside this version; code with host-process or raw-SQL
authority can already bypass application provenance controls and must not be
treated as an untrusted Source Inbox client.

## Receipt

`source_import_receipt_v1` includes:

- raw UTF-8 byte length and SHA-256;
- normalized packet SHA-256;
- a deterministic import key derived from
  `source_channel + source_key + external_run_id`;
- server item fingerprints and item/source counts;
- external claim trust state;
- a zero-side-effect safety declaration;
- a receipt SHA-256.

Supported lifecycle constants are:

```text
RECEIVED
VALIDATED
AWAITING_USER
ATTACHED
ROUND_DRAFTED
REJECTED
DUPLICATE
EXPIRED
```

These states do not confer truth, readiness, execution, or final-decision
authority. `ATTACHED` requires a separate immutable event-to-material binding.
`ROUND_DRAFTED` must refer only to a read-only launch-plan preview; it must never
call `StudioStore.create_round` or start a round stream.

## Fail-closed limits and URL policy

- input: 256 KiB UTF-8 maximum;
- JSON nesting: 12 levels maximum;
- items: 50 per packet;
- sources: 12 per item and 200 per packet;
- exact field allowlists at every core object level;
- duplicate keys, non-finite numbers, invalid source references, duplicate
  canonical URLs, and duplicate in-packet fingerprints are rejected;
- execution/account/order/trade/payment/wallet/tool/function/shell/command
  field names are rejected recursively, including inside extensions.

URL validation is parse-only and performs no DNS or HTTP request. It accepts
only public-format `http`/`https` URLs, strips fragments, normalizes IDNA host
names and percent escapes, and rejects userinfo, non-default ports, single-label
hosts, noncanonical numeric IP forms, and IP literals that are not globally
routable. A later fetcher must still perform its own DNS resolution, redirect,
rebinding, response-size, and content-type controls.

## Integration boundary

A future store/HTTP integration should:

1. call `accept_source_import` before opening the write transaction;
2. recheck the deterministic import key and item fingerprints inside one
   transaction;
3. persist import receipt, items, and sources atomically;
4. return `DUPLICATE` on an exact replay and a conflict on reused external run
   identity with different normalized semantics;
5. keep raw packet retention policy-driven and default to hash/manifest-only;
6. expose only sanitized, bounded projections in a separately scoped read-only
   MCP capability;
7. require an explicit user action before attachment or round drafting.

## Versioned deterministic impact projections

`trading_impact_rules_v1` does not extend or rewrite the normalized
`project_source_item_v1`. Its result is an immutable sibling record exposed as
`impact_rule_projections`; the stored item JSON, full-item SHA-256, server
fingerprint, import receipt, attachment material, and round-draft hashes remain
unchanged.

The sidecar binds the exact source item and admitted monitoring source, stores
both matched and explicit no-match evaluations, and declares
`external_unverified`, zero model/provider/network/extra-market calls, zero
formal rounds, and no execution capability. A same-version projection drift is
a conflict. Exact import-key replay reads existing sidecars without recomputing
or backfilling them. Its nested `trading_impact_source_semantics_v1` binding
pins the parent-derived rule, security/form or macro/Futu selectors, actual
evidence source index, and time anchor semantics. Projection validation binds
every statement, area, evidence index, and time dimension to that value;
insert/readback separately reproduce only the parent binding and require exact
equality, without generating a projection or writing data. See
`trading_impact_rules_phase5.md` for the closed rules,
confidence meaning, counterevidence state, migration, and rollback boundary.
