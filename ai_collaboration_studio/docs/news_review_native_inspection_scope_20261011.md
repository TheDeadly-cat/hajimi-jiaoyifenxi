# Bound native inspection consistency

The inspector previously required the entire CIM PID list to match a later
system-wide native PID list. An unrelated process starting or exiting between
those queries rejected a stable, correctly pinned owner and its loopback
listener. The producer contract tests reproduce both cases against the baseline
inspector without sending HTTP or opening a database.

Inspection now brackets native creation times with two CIM ancestry snapshots.
Both snapshots include every registered PID and all numeric descendants of all
registered roots, including descendants whose generation cannot yet be bound.
PID membership, creation time and parent relationships in that scope must agree
across both snapshots. The scoped native PID set must also agree with the final
CIM snapshot, using the existing microsecond comparison before retaining native
100 ns creation times. A pinned PID found natively but missing from CIM remains
unconfirmed rather than becoming a dead target.

`enumeration_consistent` now describes that registered-tree consistency. The
strict existing v1 JSON field inventory is unchanged. The candidate identity
and inspector source hash identify this producer behavior; an old plan cannot
silently consume the changed source. No observer, probe or receipt reader was
modified to tolerate additional fields.

Existing guards still reject reused generations, unidentified descendants,
ancestry changes, parent mismatch, and changed or non-loopback listener
ownership. The original generation-aware tree resolver, 128-process limit,
input validation and port checks remain. Previously registered processes remain
in the output even after disappearing. This does not verify ownership locks,
infer historical exit status, perform HTTP, or open a database.

## Validation

- The corrected baseline run executed 16 producer contract tests and failed
  five assertions: two unwanted unrelated-process refusals and three assertions
  requiring consistency across the new second ancestry snapshot. The original
  safe-read guards already rejected the latter unsafe cases.
- The final six-module regression executed 127 tests in 39.700 seconds, all OK.
  This includes 16 tests running the real PowerShell script against synthetic
  CIM/native/listener enumerations, plus the unchanged Windows integration that
  launches its own server and parent, retains the child after parent exit, and
  rejects a deliberately incorrect 100 ns generation.
- The network audit recorded eight owned loopback fixture connections, zero
  blocked external attempts, zero child blocked attempts, and zero simulated
  offline connections. No real source or provider requests were made. Temporary
  test databases do not establish anything about a historical trial database.

The initial test fixture lost subsecond precision when PowerShell JSON decoding
returned DateTime values; it now preserves DateTime ticks without changing its
assertions. The initial implementation added two diagnostic output fields, which
the unchanged native integration correctly rejected under the strict v1 shape;
those fields were removed. Both failed runs remain separate evidence. A guessed
watchdog module name was rejected by the driver before any tests ran; the final
selection uses the existing observer waiting and startup modules.

## Scope and historical limits

Only the inspector, a new producer test module and this note change. Micron,
model routing, budgets, deadlines, retries, UNKNOWN handling, the ledger, report,
watchdog, exit recorder and all existing test assertions remain untouched.

The stopped e6a78c25 trial used the original source, not this candidate. Its
failed observation intervals, missing closeout evidence and unknown exit codes
are preserved. Synthetic counterexamples demonstrate a refusal mechanism, not
the cause of each historical failed read or of the computer restart.

This engineering change does not establish a successful new short or 24-hour
trial. Any future run requires its own exact candidate, source hashes, reviewed
window, budget, approval and fresh local input. The separate quality plan remains
bound to 82e64cb; this inspector change does not alter that candidate or authorize
additional model requests. No merge, deployment, real-data migration or historical
trial restart is included.
