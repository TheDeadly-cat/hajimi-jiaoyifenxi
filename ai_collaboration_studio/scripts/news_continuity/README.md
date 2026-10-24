# Windowless observation guardian

This package observes an existing local news-observation process. It never starts,
stops, restarts, extends, or merges observation windows. The collector and its data
are outside the guardian's write scope.

## Failure addressed

The previous Task Scheduler action used `python.exe`. Its `Hidden` setting hid the
task entry, not the Python console. A guardian exited with `0xC000013A` without
running Python cleanup while its `CREATE_NO_WINDOW` child continued sampling.
This is consistent with native console-control termination; the initiating actor
and specific event are not proven.

`register_task.ps1` now uses the same installation's **pythonw.exe**. The guardian
also checks that it has no console before opening any observation handle. It uses
finite native waits, saves real process exit evidence, and cannot mistake a zero
exit code without a verified terminal for successful completion.

References: [Task Scheduler Hidden](https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-hidden-settingstype-element),
[Windows console control handling](https://learn.microsoft.com/en-us/windows/console/handlerroutine),
[Python windowless launchers](https://docs.python.org/3/using/windows.html).

## Files and write boundaries

- `observer.py`: the frozen read-only sampling/verification implementation. Its
  isolated import has no network or process-launch side effects. Existing runs
  keep their original script, start receipt, and log; they are not migrated.
- `guardian.py`: attaches to one already-running PID **and full creation
  FILETIME**, retains its native query/synchronization handle, and writes a new
  `lifecycle/<output_name>/` directory. No `Popen` or process-termination path.
- `check_readonly.py`: bounded heartbeat with only three fixed loopback GETs.
  New monitoring receipts and dedup state live in `heartbeat-observations/` next
  to the deployed tool directory. No source fetches, model calls, business DB
  access, environment reads, or raw stderr capture.
- `register_task.ps1`: creates one uniquely named, demand-start-only task using
  `pythonw.exe`, with no triggers, restart policy, or execution-time limit. It
  refuses replacement and saves registration evidence before dispatch.
- `verify_windows_lifecycle.py`: two bounded Windows integration scenarios using
  only its own fake processes and unique temporary task registrations.

Deploy a source-only copy into a new evidence directory; do not edit files under
an active run. Hash the scripts and configuration before dispatch. Keep real
configuration, PID/FILETIME bindings, receipts and all business data out of Git.
An uncertain launch result is inspected rather than retried automatically.

## Attaching without discarding a valid sampling window

Use an explicit JSON configuration in the deployed guardian directory. The
configuration binds `output_name`, `run_directory`, `observer_script`,
`observer_sha256`, `started_sha256`, `observer_pid`, `observer_creation_ticks`,
`host_pid`, `host_creation_ticks`, `backend_sha256`, and
`prior_guardian_exit_utc`. See `guardian.py` for the exact schema.

The guardian requires the original observer and host to still be alive with their
exact identities. It verifies the original started receipt, helper fingerprint,
and complete log prefix before attaching. It records the prior guardian's exit
through the actual attachment time as a **supervision gap**. The original observer
start and deadline stay unchanged. This is recovery of supervision of the same
process, not a new or spliced observation window.

The heartbeat binding is a separately hashed `observation-binding.json`. It names
the original run directory, new lifecycle directory, exact identities, original
observer script, script/startup receipt hashes, and the supervision gap. Run the
checker with the actual trigger UTC and that binding hash. Preserve source alerts
from the prior monitoring state; missing data or `fetching` is not recovery.

## Completion and limits

Completion requires the full log/start/terminal binding, the original observer's
actual exit code of integer zero, the configured duration, and a guardian
closeout. Even then, `continuous_supervision_passed` remains false after takeover.
The final report must distinguish sampling continuity, supervision coverage,
collector health, per-source success, and content quality. Rolling 24-hour API
counts and post-terminal live reads are not window totals.

Completion notification generation is scoped to the original `started.json`
SHA256 and observer PID plus full creation FILETIME, then to the verified terminal
content. A replacement guardian does not create a new observation identity. An
unbound legacy `completion_reported` boolean is not delivery evidence and cannot
suppress a new run. When the state belongs to another run, the binding's explicit
alert baseline is used; the old log anchors and completion flags are not reused.
Existing receipts and state snapshots remain on disk.

After a successful completed heartbeat receipt has been flushed and synced, the
checker writes and syncs a temporary credential, then atomically publishes one
`completion-<identity>-generated.json` credential. A write/sync/rename failure
leaves no final generation credential; temporary files are retained but never
treated as generated notifications. The published credential names
that receipt and its exact SHA256. Later checks verify the credential, original
run/completion identity, and referenced receipt before suppressing another
completion notification. A state-write failure after generation does not erase
this credential or regenerate the same completion on retry. Missing or corrupt
generation evidence fails closed rather than being silently recreated. A small
OS file lock serializes concurrent heartbeat state updates and is automatically
released when the checker exits; it does not launch or supervise any process.

`generation_status=generated` means a durable local completion notification was
generated. `delivery_status=unknown` remains separate: this checker has no
external sender and never claims the user received it. The heartbeat's completed
receipt can be saved before later publication fails; only a validated generation
credential and successful state publication produce the returned notification
decision. External sending/acknowledgement, if added, needs its own delivery
record and must not rewrite the immutable generation credential.

OS shutdown, forced termination of the guardian itself, or storage failure can
still prevent a closeout. A heartbeat reports the missing evidence; it does not
restart or fabricate a terminal. No third guardian layer is added.

## Targeted verification policy

For this lifecycle repair, validate the actual Windows console boundary and the
windowless attachment/exit-receipt path. Do not rerun product backend/UI suites,
historical assertion matrices, external providers, or a 24-hour simulation merely
to increase a passing-test count. Short fixtures never prove real 24-hour
stability. Rerun a targeted case only when its code changed or a failure demands
it. CI execution is a separate decision from local validation and publication.

For completion-notification changes, run only the offline temporary-file cases:

```powershell
python -I -S -B -X utf8 scripts/news_continuity/tests/test_completion_notification.py
```

These cases exercise the actual publication functions, repeated checks, a new
run and reused PID, guardian replacement, write failures, evidence validation,
and concurrent checks. They do not execute a Windows observation lifecycle,
contact a service or source, send a notification, or validate external delivery.
