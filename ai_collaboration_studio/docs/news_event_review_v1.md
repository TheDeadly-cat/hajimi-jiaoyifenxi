# Native news-event review v1

This feature connects newly discovered official events, immutable main-HTML
evidence, mechanical priority, a persistent principal-review queue, the existing
provider-call ledger, validated model output, and a read-only inbox projection.
It is off in the ordinary application. It does not create a manual ChatGPT
session, acknowledge a source, attach room material, start a round, or freeze a
user decision. The named room is the ledger's explicit budget owner only.

## Approved scope

The operator prepares and approves a closed `news_event_review_v1` policy. The
approval is its canonical SHA-256, binding the clean Git candidate, absolute
`NewsReviewTrial*/data/studio.sqlite3` identity, room, strategy, two official
sources, provider/model/endpoint, original start and expiry, quotas, rate card,
spend estimate, concurrency, restart preference, and stop-on-unknown policy.

The initial source profile remains `sec_micron_trial_v1`: NVIDIA 8-K main HTML
and Micron recent-30 releases, target polling every 300 seconds, `seed_only`.
Publisher backoff and Retry-After can delay a poll. Source activation is recorded
in native policy grants and uses the existing repository enablement API. The
normal supervisor acquires the first real baseline; native activation performs
no request, fabricates no publication, and does not reset a checkpoint. An
operator's later source disablement survives restart of the same policy.

Only `doubao-seed-2-1-pro-260915` at the fixed Ark Responses endpoint is admitted.
The registry must disable OpenAI, DeepSeek, Qwen and GLM for this entry point.
There is one principal call per reviewable event/body/strategy, without retries,
fallback, tools, or parallel model execution. This is not independent voting.

The closed maximum policy window is 24 hours. It can authorize at most 144
document reservations and 100 model reservations, with explicit lower quotas,
request bytes, per-call output tokens, total tokens and CNY spend limits.
The original document service's rolling six reservations per twenty minutes,
five-minute refresh interval, shared publisher cooldown and no-redirect policy
still apply. The old twenty-minute automatic-document UI and thirty-minute
manual ChatGPT review entry points retain their own restrictions.

## Evidence and charging identity

The inbox remains a side-effect-free import/read workflow with respect to model
calls. Explicitly started workers observe only rows after the policy's persisted
initial cursor. Evidence enrichment and principal review run in two separate
non-daemon threads, independent of the source polling worker.

The event identity is the validated official source URL and source kind. The
review identity adds normalized body text SHA-256 and the strategy SHA-256.
Metadata revisions, unchanged content under a new raw-HTML hash, or a renewed
policy cannot cause a second paid attempt for that identity. Substantive body
changes can create new tasks. Immutable links keep older reviews and their
original paragraph citations; no receipt is re-signed against a newer document.
This identity is enforced across policies and restarts in the same database;
it is not an account-wide index across unrelated fresh databases. Continue a
trial in its original database to preserve its deduplication and spent budget.

An incomplete or oversized body is a durable `MATERIAL_INSUFFICIENT` event, with
no paid task or model reservation. A later complete body can become eligible.
There is no silent text crop: the prompt includes every available paragraph
once and retains all coverage warnings. Attachments are not read. Review of
available main HTML does not imply review of attachments or external truth.

Each paid task freezes its event, document version, strategy, full generation
input, exact HTTP body hash, policy and conservative reservation. Reservation
uses the complete serialized HTTP UTF-8 byte count plus 256 input tokens and
the maximum output tokens. These estimates are conservative application
constraints, not a verified supplier billing cap. Reservations are never
refunded. Before sending, the candidate, owner, policy, expiry, pause, ledger
attempt, document and exact HTTP request are checked again.

Loss of a response, unknown usage, timeout, process interruption or excess
reported usage produces `UNKNOWN` and stops further paid reviews. Invalid
model identity, completion, schema or citation produces a stopped failed lane.
Collection and document enrichment may continue within the same remaining
authority. A global pause stops new work across the native lanes; an already
sent request may finish and be recorded. No outcome is automatically retried.

## Result and UI boundaries

Mechanical priority explains matched event terms and affected securities; it is
not a truth probability or market-direction score. No substantive supported
official event is discarded merely for lacking a keyword match. High-priority
tasks precede uncertain tasks within the authorized queue; the principal model
supplies its separate assessment.

Results must be closed JSON with an assessment, summary, importance reason,
quoted source-supported statements, separately identified inferences and their
limitations, counterevidence, open questions and explicit coverage limitations.
Paragraph identities and exact quotes must match the frozen input. These checks
establish engineering consistency, not that each model interpretation is true.
All displayed opinions remain `unverified_model_output`, and source claims
remain `external_unverified`.

The inbox shows publication/discovery times, priority, body/attachment coverage,
queue state, prior review versions and the principal's opinion. It distinguishes
unreviewed, pending, running, material insufficient, reviewed, failed and unknown.
The run summary is compact and offers a pause control. Reading it is local and
grants no network authority. Native control HTTP supports pause only, behind the
normal loopback/session-token/owner guards; it cannot approve a policy.

## Operator entry point

Use `scripts/run_news_review_trial.py` with the normal isolated interpreter.
Initialization requires a new explicit root outside the repository whose name
starts with `NewsReviewTrial`, and the exact clean candidate SHA. It creates an
empty independent database and a named budget-owning research room, returning
their identities in `initialized.json`. It never opens or migrates a formal or
historical database.

Schema creation runs only in system temp, retaining the store's existing
initialization guard. A fully checkpointed new image is copied to the exclusively
created trial destination under its owner, synced to disk, and checked by normal
startup preflight. An interrupted initialization is preserved and cannot be
overwritten by repeating `--initialize`.

`--prepare --config <policy.json> --output <new-file.json>` validates the complete
policy without source/provider requests or authorization writes. The rate card
must contain explicit positive CNY input/output prices, an official Ark source
URL and its checked date. No price is silently supplied by the launcher.

`--run --config <policy.json> --approve-policy-sha256 <exact-hash>` requires a
valid original window and a public SEC product/contact User-Agent. Use
`--password-dialog` for masked local credential entry. Ambient model keys are
cleared in the child process; the selected key is held only in that process.
The independent host binds a random loopback port by default and records its
local URL. Ports 8770 and 11111 are rejected. Source scope and authority are
rechecked during polling, and the host stops at expiry or a failed worker.

For an entire window starting after credential entry, use `--prepare-activation`
and then `--activate --approve-activation-sha256 <exact-hash>`. The approved
envelope fixes every scope/budget field, a launch admission window and a duration
(24 hours by default). Only the two execution timestamps may be resolved from
the first successful local key submission. Exclusive `activation-policy.json`
and `activation-receipt.json` files bind that original approval to the resolved
policy. They permanently consume the one activation, even after a crash.
Reactivation cannot slide the window; restart must use the saved resolved policy.
This avoids spending the observation window waiting for the operator's key.

Restart can resume unsent work only within the original policy's explicit
restart preference and window. Previously started work becomes unknown, never
replayed. `--resume-paused` additionally requires the same approved policy hash;
it cannot clear a paid-lane failure or unknown outcome. No key is persisted for
unattended restart. Database ownership is retained on incomplete worker shutdown.

Wall/monotonic drift beyond two seconds stops new work. At expiry the journal
records the window boundary and starts no new requests. Already sent requests
can finish during a bounded 255-second shutdown grace; failure to join still
retains the owner. Window continuity and clean shutdown are reported separately.

`--report` reads the owned trial through the normal startup preflight and writes
a new report under the trial directory. Existing schemas needing migration are
rejected; the launcher does not silently upgrade them.

## Continuous-run evidence

The journal records process sessions, monotonic/wall-clock samples, source-run
identities and their hashes, and queue/reservation counts in an immutable hash
chain. Reports separate per-source observed poll success, publication-to-discovery
latency, body coverage, queued work, duplicate paid attempts, unknown outcomes,
permanent call ledger and process continuity. Poll runs are not mislabeled as
actual HTTP request counts. Price estimates are not supplier bills.

The continuous-window indicator requires a complete 24-hour policy window,
start/end samples, one process session, at least 2,880 samples, gaps no larger
than thirty seconds and no detected wall/monotonic drift. It is only continuity
evidence. Source health, complete natural-event processing, disconnection/restart
recovery and factual quality remain separate acceptance checks. Multiple
sessions and missing samples remain visible.

No natural new announcement means `no_new_event_observed`. Do not alter source
times, event identities or cursors to manufacture an online success. Offline
fixtures and passing CI cannot establish a real 24-hour run or release approval.

## Local implementation validation (2026-09-21)

The isolated related backend regression passed 156 tests. After the final
clock, activation, shutdown and health changes, the focused native/host/health
suite passed 51 tests (five allowed loopback connections, zero external or child
blocked attempts). These overlapping groups are not added together. The safe
frontend runner passed all 664 tests across 119 files, and the production build
passed. Required historical-reader validation is a separate clean-commit gate.

Rendered QA used installed Chrome 153 with Playwright 1.62.1 because the Browser
plugin was unavailable, on disposable synthetic data at a random loopback port.
Desktop 1440x1000 and mobile 390x844 checks covered the reviewed/unverified,
unknown, material-insufficient and persisted pause states. The final run had no
console errors, failed responses, external requests, framework overlay or
horizontal overflow. Twenty parallel health/bootstrap/review-read rounds also
returned successful health responses. The fixture's paid-attempt count stayed
at its two synthetic calls, with no manual session, discussion round or draft.

Earlier browser runs exposed intermittent Windows WAL-file permission errors
during health snapshot acquisition. Read-only snapshot acquisition now retries
at most three times before yielding a snapshot. Deterministic regressions cover
last-reader WAL removal, transient permission denial and persistent denial;
source files remain untouched and persistent failure remains visible. Earlier
failed preparation and browser results remain in local evidence. Raw logs,
screenshots, databases and response fixtures are not part of this source commit.

Real source connectivity, natural new-event processing, real model output,
supplier bills, 24-hour continuity and release acceptance are not established by
these local results. Any new bounded trial requires its own concrete approval.

## Real trial result (2026-09-21)

Following explicit approval, candidate
`eb91fc243da43d957a0e3003864724dc50107f8b` ran the fixed SEC/Micron profile in
an independent trial database. The approved window was 2026-09-21 01:59:06 to
2026-09-22 01:59:06, UTC+8. The launcher and known Python processes exited early;
the report was written at 11:34:55 on September 21, about 9 hours 36 minutes after
the window began. **The full 24-hour acceptance was not completed.**

The report records clean shutdown, one process session, 3,420 heartbeat samples
and an 11.321-second maximum sample gap. It contains no explicit stop-cause
field, so clean shutdown does not explain the early exit. Source errors alone
are not evidence of its cause. The persisted policy's `ACTIVE` label does not
mean the process is still running.

- Micron: 66 observed poll runs, comprising 41 succeeded, 24 degraded and one
  failed; success rate 62.12%.
- SEC/NVIDIA: 41 observed poll runs, comprising 27 succeeded and 14 degraded;
  success rate 65.85%.
- No natural new event, document reservation, model attempt, queue backlog or
  unknown result was recorded. The call ledger was empty. Body coverage and
  discovery latency have no new-event sample, rather than a passing score.

These are poll-run counts, not actual HTTP request counts. Zero model attempts
does not validate the credential, real model output or the natural-event review
path; supplier billing remains unverified. The monitor was disabled after exit,
without restarting, renewing or adding requests. Raw runtime evidence remains
local; the preserved report SHA-256 is
`d5d00f6cef42c2ae10dc77c328f0ab6e3192b14bc9151dff332461c847c250cd`.
This result is incomplete live acceptance, not release approval.

## Recovery and stop provenance revision (2026-09-22)

The review of `5c8e80b` found four real lifecycle gaps. Recovery now runs before
the current service session's approval or explicit resume. The real host passes
through the same startup gate without undoing that decision. A subsequent host
session performs recovery again; `resume_within_window=false` still requires
explicit confirmation after restart and never changes to `true` implicitly.
Paid-lane stops (unknown result, validation failure or exhausted budget) survive
operator pause and explicit resume. Resume can reopen collection in the original
valid window but cannot reopen paid review. A false automatic-resume setting
still pauses the restarted host even when a paid stop was already recorded.

Native document jobs still in `waiting` retain their original job ID, expiry and
permanent reservation. Both native and generic document recovery leave that
unsent work to the native controller, which rechecks authorization at send.
`fetching` remains cancelled as potentially sent. Expired/revoked waiting work
is cancelled with an authorization code; interruption is shown separately from
material insufficiency. Manual document recovery retains its existing behavior.

An additive, controlled schema migration introduces successor execution tables
and a permanent content-claim table. Original job/receipt/link tables and their
immutable identities remain in place. When an old authorization expires, its
definitely unsent queued execution becomes cancelled/awaiting authorization.
A newly discovered event with the same content can freeze a new execution under
a separately approved policy, using that policy's own limits. Shared read views
cover both generations. A content claim is consumed with the model reservation
and is never refunded; sent, completed, failed or unknown attempts cannot be
charged again under another policy. No historical task, dedupe key or receipt
is deleted or reassigned. Existing databases require the normal explicit
migration procedure; the trial launcher does not auto-upgrade them.

The launcher now writes an immutable stop observation plus a separate local
stop receipt containing the time, trigger, bounded error code, exception type,
runtime status and whether the window was reached. The file receipt remains
available if journal writes fail. Exception messages and credentials are not
serialized. A fault or unexpected early host return produces exit code `2`;
host exceptions retain their error exit and owner-retention behavior. Reports
separate work completion, clean cleanup and acceptance. Planned expiry during
a source request is recognized as window closure rather than a runtime fault.
Source metrics additionally retain observed error-code counts, last success,
consecutive failures and next due time; these are not HTTP request counts.

New regressions use isolated databases and the actual HTTP host/runtime lifecycle
at random loopback ports. Synthetic transports/schedulers cover first approval,
explicit resume, queued document recovery without a second reservation,
cross-policy continuation and unknown-result non-replay. Fresh child processes
exercise the actual launcher for runtime faults, observer faults and normal
deadline closure; the host itself is not replaced in those tests. An additional
journal-failure test checks the independent bounded stop receipt. These tests
do not establish live source stability, natural-event coverage or provider bills.

The earlier trial remains closed and its early-exit cause remains unknown.
These fixes are not retrospective proof of what stopped that process. A fresh
bounded online acceptance requires a new candidate, isolated database, concrete
approval and locally entered credential; no old activation or budget is reused.

## Send cancellation and current-body selection (2026-09-23)

The controller and the last HTTP admission check now share a cancellation gate.
The real host also binds its shutdown event before starting review workers.
Cancellation is checked before claiming work and after all request construction,
identity, policy and ledger checks, immediately at send admission. The gate's
short lock orders admission against controller stop; it is never held while
connecting, waiting for a response or persisting evidence. An attempt admitted
first drains under its existing authorization; cancellation winning admission
prevents the opener call.

Definitely unsent reserved work finishes as `CANCELLED` with
`host_stopped_before_send`. The original attempt, content claim, token/cost
reservation and counters remain consumed. It is not converted into an unknown
sent result or refunded. Admitted attempts with uncertain results remain
`UNKNOWN` and cannot replay. A crash before the final receipt still has the
durable `RUNNING` job, attempt and content claim and recovers conservatively.
The finalized `http_attempted` flag records actual admission; an in-flight
zero flag is not proof that a request was unsent.

Stop requests publish cancellation and the host stop signal before taking the
receipt lock or writing files/database events. Blocked or failed persistence
cannot leave new sends enabled. Persistence faults remain visible and preserve
fault exit behavior. The database owner is retained until workers drain.

Document history keeps its original ordering and immutable records. The
`current_version_id` and `current_observation` fields instead identify the most
recent completed or partial successful read, ordered by completion time with a
deterministic tie-break. Failed or cancelled reads do not replace that pointer.
Queue selection, coverage reports and the review projection use the same
selector; the UI defaults to that body while allowing explicit history choices.
The projection's `current_review_id` uses the existing content/strategy dedupe
identity, including reuse by another event. A to B to A selects A's original
review without reordering history or reserving another model call.

Regression coverage uses real services, SQLite, provider protocol and controller
boundaries with synthetic transport: stop before claim, stop after preflight
while receipt writing blocks, journal blocking, actual-host shutdown, admitted
success/unknown completion, A to B to A, failed rereads and restored-body report
coverage. DOM checks cover the current body and corresponding historical review.
These checks do not establish a new full CI run or real continuous acceptance.
The interrupted fixed-candidate trial is recorded separately in
[the 20260922C result](news_review_trial_20260922c.md).

## Read-only status refresh recovery (2026-09-24)

The detail and runtime-status panels retry transient status GET failures at
2, 4, 8, 16 and 30 seconds, then stop until a manual read. A successful read
resets that retry count and returns to ten-second polling. The first read is
bounded to 90 seconds if no observation window has yet been confirmed. Once
confirmed, its deadline bounds retries and cancels outstanding reads; automatic
responses cannot extend it. A failed manual read cannot renew an expired retry
window. Unmount, item replacement and cancellation also stop further reads.

The panels show the most recent successful refresh time and keep the current
state explicitly unconfirmed after an error. Invalid JSON, protocol/identity
mismatch, application error codes and non-transient HTTP refusals stop automatic
retry. A changed runtime policy identity is rejected until an explicit refresh.
The feature-disabled response remains valid. The retry loop never invokes pause,
approval, body fetch, review execution or budget-reservation actions. Pause stays
a single explicit action and its failure is not automatically retried.
