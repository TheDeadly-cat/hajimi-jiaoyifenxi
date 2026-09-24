# News review trial 20260922C: interrupted acceptance

The authorized trial did not establish a complete 24-hour acceptance window.
The monitor observed the approved launcher process missing before expiry, with
no stop receipt, observer-drain receipt or final window report. Its terminal
classification is `PROCESS_ENDED_WITHOUT_FINAL_REPORT`. At that closeout, the
cause, exit status, exact exit time, final usage and child-host terminal state
were unknown. The September 24 read-only investigation below establishes the
current process termination and preserved application ledger, while retaining
the unresolved historical cause and supplier-billing boundary.

## Fixed candidate and authorization

- Candidate: `32a7ffba7b4f9fe226b55cd77859d578cb1d2253`.
- Activation proposal SHA-256:
  `77acf9e0cdcb69a447e12640036c79ae9acd35139b7d2d8f801ba953b71746b7`.
- Activated policy SHA-256:
  `93ab86e9b1ef195f9f35b01a5ae47232f47951b63a5f261a428e6e4b1bb627a6`.
- Independent trial identity: `NewsReviewTrial20260922C`.
- Actual window, UTC+8: **2026-09-22 17:16:30.814 through
  2026-09-23 17:16:30.814**. The template's pre-activation deadline was not
  the active trial's expiry.
- Sources: NVIDIA 8-K filings and Micron official announcements; five-minute
  discovery with the first pass used only to seed a baseline.
- Limits: 24 body-request reservations, 3 calls to
  `doubao-seed-2-1-pro-260915`, 210,000 tokens and CNY 1.50 in application
  estimates. The estimate is not a supplier billing cap.
- No automatic restart, renewed window, replacement model, unknown-result
  retry or reservation refund was authorized.

An initial launcher failed before credential submission because `Get-FileHash`
was unavailable. A separately approved, single pre-activation retry through
PowerShell 7 succeeded. The valid launcher receipt is `launch-receipt-v2.json`,
PID 20060, created at `2026-09-22T09:16:17.2144336Z`. The historical first
failure is retained and is not the later terminal incident. Subsequent process
checks compared parsed UTC ticks as well as PID, avoiding date-string mismatch.

## Observed outcome

All times in this table are UTC+8 on 2026-09-22.

| Time | Evidence |
| --- | --- |
| 17:16:30.814 | One-time activation resolved the fixed 24-hour window. |
| 22:54:05.538 | Last successful monitor snapshot verified the launcher identity, live host/workers and unchanged policy. |
| 23:17:28.380 | The approved launcher was absent; no stop/drain receipt or final report existed. |
| 23:19:11.971 | A further process and report check reconfirmed the terminal monitoring fault. |
| 23:20:00.139 | Heartbeat `24-20260922c` was verified `PAUSED`; evidence was preserved without restarting. |

These observations do not establish continuous operation between samples, an
exact termination time, or a six-hour successful acceptance run. The absence of
a report left the final ledger unreconciled at the original monitor closeout.

At the **last successful snapshot**, events observed, document reservations,
model-call reservations, token reservations and application cost reservations
were all zero, and the job-count map was empty. These are stale, last-known
counters, **not final totals** and not proof of zero supplier charges.

At that snapshot SEC was healthy. Micron was failed with `IR_FEED_ERROR`,
eight consecutive failures and no successful check since 21:25:20.495.
Earlier samples recorded SEC ticker-map/submissions errors and Micron
metadata/revalidation errors, with recoveries and coverage gaps preserved.
Source failures do not establish the cause of the launcher disappearance.

No natural new-event review sample was observed before the last successful
snapshot. Discovery delay and body coverage therefore lack an end-to-end
sample. Final per-source success rates, backlog, duplicate or cross-policy
attempts, unknown outcomes, model ledger and continuous-window gaps were not
reconciled. They must not be inferred from isolated health snapshots.
Model content, external facts and supplier billing remain independently
unverified.

## Engineering evidence and remaining work

The fixed candidate `32a7ffb` passed
[push CI 35642528593](https://github.com/TheDeadly-cat/hajimi-jiaoyifenxi/actions/runs/35642528593)
and [PR CI 35642534474, attempt 2](https://github.com/TheDeadly-cat/hajimi-jiaoyifenxi/actions/runs/35642534474/attempts/2).
Each recorded 2,310 backend tests, 666 frontend tests and the required
five-test/seven-row historical-reader matrix, plus clean-source smoke and the
synthetic release drill. PR attempt 1 exceeded the existing 90-minute job limit;
its cancelled result and partial evidence remain preserved.

Those checks belong to that exact candidate. They do not prove this live trial
completed, explain its exit, validate provider output or approve a release.
This result summary does not validate any later runtime change. A follow-up fix
requires engineering checks bound to its own commit.

A follow-up read-only investigation was explicitly requested and is recorded
below. The old activation and reservations must not be reused. The monitor
remains paused; no restart or renewed trial was performed.

## September 24 read-only incident reconciliation

The original launcher PID 20060 and the database-owner PID 936 are absent.
Windows reports a later boot at **2026-09-24 22:48:53.500 UTC+8**, so the original
process tree cannot still be running. The recorded loopback port 59570 has no
listener. The retained owner file is stale metadata: a nonblocking read lock
on its first byte succeeded and was released without changing the file.
This proves current termination and an unheld owner lock, not the historical
exit time, exit code or cause. No matching process-exit audit events were
available in the failure interval, and the inspected System/Application events
did not establish a launcher/host shutdown cause.

The original database had no WAL, SHM or journal sidecar. A cold copy was made
while holding that read lock; original/copy hashes matched, and the original
database, owner file and launcher-log hashes remained unchanged afterward.
Only the copy was opened with SQLite `mode=ro&immutable=1` and `query_only`.
Integrity checking returned `ok`. No migrations, recovery, budget resets,
provider requests or trial restart were performed. Logs remain preserved;
their contents and any model output were not inspected.

The preserved application ledger contains **zero** news events, original or
successor review jobs, review receipts, content claims, document jobs/versions
and provider-call attempts. The policy has zero document/call/token/cost
reservations; its matching provider run has zero reserved/completed calls.
The stale policy `ACTIVE` and provider-run `OPEN` values are retained historical
rows, not evidence of a live or valid authorization. The authorization expired.
There are no recorded unknown or duplicate attempts in this isolated database.
These are reconciled persisted application counters, not supplier billing proof.

All **2,179** journal entries pass their hash-chain/sequence/policy checks:
one session start, 2,065 heartbeats and 113 source-poll records, with one session
identity. The last persisted heartbeat is **2026-09-22 23:04:17.221 UTC+8**.
The largest recorded heartbeat gap is 10.238 seconds within that recorded
interval. No stop-request, session-stop or window-closed journal entry exists.
This does not prove operation after the last durable heartbeat or a full window.

| Source | Persisted successful runs / all runs | Last persisted condition |
| --- | --- | --- |
| SEC/NVIDIA | 30/48 (62.5%) | `SEC_SUBMISSIONS_ERROR`, four consecutive failures |
| Micron | 20/65 (30.77%) | `IR_FEED_ERROR`, eight consecutive failures |

These are adapter-run rates, not HTTP request counts or announcement capture
rates. The later durable SEC state differs from the earlier healthy monitor
snapshot; both original records are preserved. There is no natural end-to-end
sample, discovery-delay sample or successful body/model coverage to claim.
Historical exit cause/status and supplier billing remain unresolved. Source
failures alone still do not identify the process-exit cause.

Any later acceptance monitor must identify the launcher **and** actual owner/
host process by PID and creation time, check descendants and listening-port
ownership, and reconcile stop/drain/report evidence before calling a process
tree stopped. Parent disappearance must trigger investigation, not an automatic
restart or a claim that every child stopped.

| Investigation artifact (local only) | SHA-256 |
| --- | --- |
| Cold-copy preservation receipt | `73a94cc6b0859a8bfba4a2e884333e05975591c22a43efdb0bfd463c5b59fd90` |
| Application ledger reconciliation | `9cb9886a65714a8e92ee5df09024cecf00bc15b254e5010870c8dc4a3053e176` |
| Source reconciliation | `c45a2358c08b76b880d82ee9ca2befa171cef8620a455fbd7b39f964b6fa4aea` |
| Current termination verification | `57e56350e527a7e0b078a728e992472a7ce37eb97922b611797e017de3dfbc0d` |

## Local evidence anchors

Only this summary and evidence digests are published. Original databases,
credential/approval material, source bodies, model replies and runtime logs
remain local. Original receipts were not rewritten to match this summary.

| Local artifact | SHA-256 |
| --- | --- |
| Activation receipt | `37b664bb186a7a034cfc731cd702a98d51b63e43a53dbfaac9e89ac494d02d38` |
| Last successful monitor snapshot | `7dcf8f4a4128bebd56fe09d420fae67ebff3f09ce06a6b8e8963a47b84ce7e67` |
| First terminal monitor snapshot | `a63c26c392a40883dbf8b8a2433d2e07a566df21282819e7a84130c40411a348` |
| Terminal-fault receipt | `e7dd59844e9ff8a6f4a08514ea14c27d1fd0b40385c9dcb1b8f20afbd97850e9` |
| Monitor-pause receipt | `e158e0fa0a998db7c51b7234e5bef6a316fc15059b7b597a23899f52fe2e76be` |

See [the feature contract](news_event_review_v1.md) for the queue, evidence,
migration and authorization rules. This result is incomplete live acceptance.
