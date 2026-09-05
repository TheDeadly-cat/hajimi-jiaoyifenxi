# Source Monitoring reader compatibility and isolated rollback

Source archive integrity and a successful source pointer change do not establish
data compatibility. The existing isolated release tool now checks the actual
target reader before every activation and rollback. It remains a system-TEMP
exercise and does not authorize or access a formal database.

## Publication contract

`activate_release(..., database_path=...)` and
`rollback_release(..., database_path=...)` require an existing explicit temporary
database. Both enter `_publish_pointer`; there is no compatibility bypass flag.
Installation remains source-only and does not activate a release.

The closed `release_activation_pointer_v2` includes a SHA-256 binding to the
canonical database path and file identity. All subsequent generations must use
that same data binding. Offering an unrelated empty database, replacing the
bound file, or changing the pointer during the reader check prevents publication.
An old v1 pointer lacks this binding and is rejected. It must not be relabelled
or resealed as v2; transitioning an existing deployment requires separate review.

Before writing the pointer or activation receipt, the tool:

1. Validates the installed target manifest, its closed receipt and every source
   file hash.
2. Uses the existing monitoring read-only snapshot helper and SQLite backup to
   create one consistent TEMP snapshot, including committed WAL records.
3. Imports the target source in a fresh isolated Python process. It constructs
   `StudioStore._open_existing_schema`, never `StudioStore(...)`, and replaces
   both Store and monitoring repository connections with immutable,
   `query_only` connections. No schema initialization or migration is invoked.
4. Checks rooms, room snapshots, every material, monitoring states, strict SEC
   and IR checkpoint parsers, the unfiltered Inbox list and every Inbox detail.
   Detail reads verify the existing hashes, sidecars, attachments and research
   draft bindings. The list's 200-item limit cannot hide an incompatible detail;
   a database with more than 10,000 Inbox items stops the bounded check.
5. Revalidates installed source, pointer and the original database file family.
   Main/WAL/SHM/journal bytes, identities and modification times must remain
   unchanged. Aliased or multiply linked database family files are rejected.

Missing data, unavailable readers, invalid or unknown formats, failed probes and
data/source drift prevent publication. `RELEASE_READER_INCOMPATIBLE` leaves the
current pointer and activation receipts unchanged. A checker result is tied to
the observed snapshot; it does not authorize later writes or bypass a future
check. This isolated tool is not a production concurrent deployment executor.

The only recognized legacy exception is a **disabled** source whose actual target
parser returns `SEC_BASELINE_UPGRADE_REQUIRED` or
`COMPANY_IR_BASELINE_UPGRADE_REQUIRED`. That state is reported as readable and
requiring an explicit baseline upgrade. The checker does not enable the source,
clear its checkpoint, reset the Inbox or migrate anything. The same error while
the source is enabled prevents publication; `CHECKPOINT_INVALID` is never waived.

## Actual reader matrix

The minimum tested reader for the following persisted format group is
`25f61d00e3ec49e9034dfc3139033e4ff3b3487e`. This is a tested compatibility boundary,
not an assertion based on a package version or filename. The gate executes each
target's parser instead of trusting a self-declared reader version.

| Data/read path | Actual 67fdb4ad reader | Actual 25f61d00 and current reader |
| --- | --- | --- |
| Existing rooms and ordinary materials | Readable | Readable |
| Existing ordinary Inbox item | Readable | Readable |
| SEC checkpoint v2 | `SEC_FILINGS_CHECKPOINT_INVALID` | Readable |
| Company IR checkpoint v2 | `COMPANY_IR_CHECKPOINT_INVALID` | Readable |
| Q4 JSON v2 item with neutral/no_match sidecar | `SOURCE_INBOX_RECORD_CORRUPT` | Readable |
| Unfiltered Inbox containing that Q4 item | `SOURCE_INBOX_RECORD_CORRUPT` | Readable |
| Material attached from that Q4 item | Readable as material | Readable with verified Inbox binding |
| Research draft bound to Q4 item | Inbox detail cannot validate | Readable and verified |

The Q4 fixture is produced by the real Company IR adapter with in-memory official
transport responses. The real import, acknowledgement, attachment and draft
operations produce the persisted records. The sidecar retains
`trading_impact_source_semantics_v2`, `evaluation=no_match` and no hypotheses;
this means no applicable rule, not a mapping failure. The draft remains a draft:
Provider ledgers and formal rounds stay at zero.

The test activates the old source against ordinary data, adds the new writer
formats to **the same database**, and activates the compatible source. Both
`rollback_release(old)` and `activate_release(old)` then fail, as does an attempt
to substitute an unrelated empty database. Pointer, receipts and data remain
unchanged after each refused switch.

PR A's initialization selection seal changes cross-batch behavior without adding
a checkpoint or persisted receipt format. The 25f61d00 data-reader result does
not prove PR A's initialization behavior. Any later persistent cache or new
writer format needs its own actual reader checks; it is not covered by this
minimum-reader claim.

## Reproduction

From the application directory, with the two immutable Git objects already
present locally:

```powershell
python -B scripts/run_backend_tests_isolated.py tests.test_release_drill --verbosity 2
python -B scripts/run_backend_tests_isolated.py tests.test_source_backup --verbosity 2
```

The historical test uses local `git archive` for exact commits 67fdb4ad and
25f61d00, with lazy fetching disabled. If either object is unavailable, it reports
a skip and does not claim a historical matrix pass. It never downloads history.

### Required CI evidence

The dedicated `Required historical reader compatibility matrix` step now uses
an explicit required mode. Checkout retains its pinned Action and sets
`fetch-depth: 0`. The source preparation step checks both exact historical
commit objects and their reader source, plus the triggering candidate. The
isolated runner repeats the object checks from its own actual application
source directory with `GIT_NO_LAZY_FETCH=1`; tests never fetch missing history.

```powershell
python -B scripts/run_backend_tests_isolated.py `
  tests.test_release_drill.ReleaseReaderDataContractTests `
  --require-historical-readers `
  --historical-reader-report "$env:TEMP\historical-reader-matrix.json" `
  --verbosity 2
```

Use a fresh report filename. Missing objects, omitted required tests, a skipped
required test, incomplete matrix rows, unexpected rejection, network access or
Provider evidence invalidate the required result. A source-only archive still
supports ordinary offline tests with an explicit historical-evidence skip;
running the required command in that archive fails and writes a failure receipt.

The uploaded `required_historical_reader_matrix_v1` receipt records the candidate
SHA separately from the actual checked-out commit and tree. A pull-request
workflow normally tests a merge checkout, so these commit identities may differ.
The candidate must be an ancestor of that checkout. The receipt also records
whether tracked source is clean, fixture generator/schema identities, actual
reader file hashes, per-check `PASS` / `EXPECTED_REJECTION` / `FAIL`, scoped
`skip_count`, whole-suite counts, network audit and Provider/round counts.
Dirty source can produce diagnostic test results but cannot produce a successful
required exact-commit receipt. Required mode also rejects layer-listing without
test execution; its network totals must be zero, including loopback/simulation.
The exact historical identities are:

- `67fdb4ad548059506302298ee4d87846abfcece9`
- `25f61d00e3ec49e9034dfc3139033e4ff3b3487e`

The required matrix has seven scenario/reader rows: both historical readers on
legacy and current data, current reader on current data and committed WAL, and
the old-reader activation/rollback/unrelated-database rejections. Rejected
switches preserve pointer bytes, receipt bytes and the original TEMP data family.
This receipt remains separate from the full backend regression, clean-source
startup smoke and synthetic release drill; each CI step must finish successfully.

Windows 8.3 TEMP spelling is canonicalized only after every requested path
component is checked for symlinks/reparse points. Database and sidecar hard-link
rejections remain in place; accepting a short spelling does not authorize an
alias to another database or remove the system-TEMP restriction.

For a separate reusable read-only check, provide an existing TEMP database and
an explicit reader source tree; the command creates its own consistent copy:

```powershell
$env:AI_STUDIO_SKIP_LOCAL_ENV = '1'
python -I -B scripts/run_isolated_release_drill.py `
  --reader-source-root 'C:\path\to\exact-reader\ai_collaboration_studio' `
  --reader-database "$env:TEMP\reader-fixture\studio.sqlite3"
```

Exit 0 means this snapshot is readable, exit 2 means a valid probe found an
incompatibility, and exit 1 means the probe could not be completed. This standalone
probe does not publish a pointer. Publication always verifies the installed
source manifest and repeats the data check.

The ordinary `run_drill()` still creates two synthetic versions of current
source, executes their actual readers against temporary data, and reports
`historical_upgrade_compatibility_proven=false`. Its lifecycle success remains
separate from the actual historical matrix.

Source backups continue using the closed v1 manifest and unchanged hash
algorithm. Database files and their WAL/SHM/journal family are excluded at every
directory depth, and a manifest that includes them is rejected even when its
content hashes are valid. The formal promotion planner remains source-only and
does not inspect databases or infer reader compatibility.
