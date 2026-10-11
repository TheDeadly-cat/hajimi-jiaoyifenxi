# Prepared observer cadence for a future independent run

The observer previously prepared only a 300,000 ms active-read interval while its successful-read silence limit remained 360,000 ms. With that cadence, one unsuccessful scheduled observation can separate successful observations by roughly 600,000 ms. This scheduling relationship does not establish why a particular native inspection failed.

A future preparation can explicitly select a 150,000 ms interval. The default remains 300,000 ms. Both values are recorded in the existing `poll_interval_ms` plan field and covered by the complete plan hash. Other values, booleans, floating point values and strings are rejected by the preparation API. The CLI accepts the selection only with `--prepare`; `--run` cannot override it.

The same selected cadence is retained when a pre-activation observer binds the actual activation receipts and transitions to active observation. The unresolved policy identity, actual activation window, process pins, native inspector instance, runtime/source hashes and receipt bindings retain their existing checks. Preparing or verifying a plan does not authorize or start a trial.

For a new, independently approved preparation, the additional option is:

```text
--poll-interval-ms 150000
```

Use it when generating a new observer plan and review that exact plan hash. Do not edit a running observer, its frozen plan, its receipt chain or its output directory. Source/model request limits and paid execution approval remain separate. The active loop makes one normal scheduled status-read attempt per opportunity; it gains no immediate retry, catch-up, restart or resume behavior.

The silence threshold remains 360,000 ms. Source freshness limits, inspection/status timeouts, the 30,000 ms pre-activation cadence, dual schedule deadlines and the native-only drain window are unchanged. Native inspection and loopback owner checks remain prerequisites for a status read; this change adds no strict-v1 inspection fields and accepts no incomplete inspection.

With 150,000 ms spacing, the nominal successful-read gap around one failed opportunity is 300,000 ms. The isolated scheduler regression uses actual receipt writing and virtual clocks, with a 45,000 ms failed probe between successful 5,000 ms probes. It verifies the 300,000 ms gap for the shorter schedule, the 600,000 ms gap for the legacy schedule, the unchanged absolute/elapsed deadline and the failed receipt's unchanged unsuccessful status. This example does not guarantee continuity under longer probes, scheduling delays or repeated failures.

A shorter interval increases scheduled local inspection/status-read opportunities (nominally 576 instead of 288 over 24 hours). It does not prove HTTP counts, source capture, model quality, historical fault attribution or full-window acceptance. Any failed observation remains in the receipt chain and must still be evaluated independently. A future real run requires its own exact candidate/plan/CI bindings, concrete approval and fresh local credential input; existing approvals or outputs cannot be transferred.
