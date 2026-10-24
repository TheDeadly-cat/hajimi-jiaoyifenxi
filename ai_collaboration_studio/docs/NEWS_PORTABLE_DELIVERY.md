# Portable news workspace and clean-checkout verification

This source tree contains the complete Python host, frontend, dependency locks,
startup and packaging helpers, and tests. It is not a seven-file overlay.
The portable entry below uses its own data directory and never searches for,
stops, or reuses a service on port 8770.

## Toolchain and bootstrap

Use Windows x64 CPython 3.14, Node **v24.16.0**, and npm **11.13.0**. The observed
Python patch version for this delivery is **3.14.6**. The Python dependency lock
is for CPython 3.14 on Windows x64; every distribution is pinned with its hash.
`frontend/package-lock.json` fixes the frontend dependency resolution.
Bootstrap checks the Node/npm versions before installation and records the
observed versions, lock hashes, resolved Python packages and built index hash.

From this application's directory in a new clean checkout:

```powershell
python -I -B -X utf8 scripts/bootstrap_ai_collaboration_studio.py --check-only
python -I -B -X utf8 scripts/bootstrap_ai_collaboration_studio.py --allow-dependency-downloads
```

The second command explicitly allows downloading the pinned software packages.
It creates `runtime/bootstrap/python`, performs the hash-checked Python install
and `npm ci`, and builds the frontend. It does not start the application or call
a model. The receipt is `runtime/bootstrap/bootstrap-receipt.json`; `--report`
can also save a receipt in the evidence directory. Raw dependency stderr is not
copied into that receipt. No CI invocation is required.

## Start with separate, newly initialized data

```powershell
runtime/bootstrap/python/Scripts/python.exe -I -B -X utf8 scripts/run_news_workspace.py --initialize-new
```

The defaults are `127.0.0.1:8780` and **this checkout's**
`runtime/news-workspace`. No parent directory's database is selected. Initial
collection is disabled, paid providers are disabled, and no AI credential is
configured. Local `.env` files are skipped, supported inherited provider keys
and signing secrets are cleared, and inherited runtime/database/port overrides
are discarded before applying the explicit arguments.

For online startup, supported original proxy environment variables and explicit
NO_PROXY intent are retained after validation. The effective standard-library
view may come from Windows registry settings; it is not copied into environment
variables, which would change Windows bypass precedence. Every proxy variable
retained for the whole application must be supported, including an unused ALL
or HTTP value; invalid values cause startup refusal, not silent removal or direct
fallback. Offline/bootstrap/test defaults still clear proxy environment values.
See [the source and wording repair record](NEWS_SOURCE_REVIEW_REPAIR_20261025.md).

Login auto-start registration is unavailable in portable mode: the historical
shortcut does not bind this instance's data directory and port. Enabling or
disabling it is refused without inspecting or changing an existing registration.

Choose another independent location and port with `--data-root` and `--port`.
Ports 8770, 11111 and 18787 are rejected. An occupied port causes refusal;
no existing process is stopped. `--initialize-new` requires a new or completely
empty directory and will not overwrite even a zero-byte database. Initialization
uses the existing temp-only schema initializer, then publishes fresh database
images without overwriting an existing file.

On subsequent starts, omit `--initialize-new` and keep the same `--data-root`
and `--port` if they were supplied. All three existing databases must be present
and pass read-only preflight. Missing schema, unreadable evidence, or SQLite
sidecars cause refusal. This entry does not implicitly migrate or recover old
data; use the separately authorized preview/prepare/apply migration and backup
workflow for that. Preserve any refused directory for diagnosis.

The old `start_daily_workspace.ps1` and general launcher remain historical
compatibility entries. They can refer to a parent runtime or port 8770. **Do not
use them for clean-checkout validation or portable news startup.**

## Minimal offline verification, including synthetic rollback

After bootstrap, select a new evidence subdirectory outside the checkout:

```powershell
runtime/bootstrap/python/Scripts/python.exe -I -B -X utf8 scripts/verify_news_clean_checkout.py --project-root . --work-root C:/Temp/news-clean-checkout-evidence-new --run-focused-tests
```

`--work-root` must not exist. It retains all generated data, source archives and
safe receipts. The verifier chooses an unused non-protected loopback port, or
accepts an explicit `--port`. Its child starts with an empty data directory and
`--offline`; the process blocks outgoing connections, DNS, datagrams and child
process creation. Incoming loopback HTTP and replies remain possible. This is
an application acceptance guard, not an operating-system security sandbox.

The verifier only GETs readiness, version, news overview, and the built frontend
entry/JavaScript asset. It checks the loaded backend digest, served frontend
bytes, disabled collection, living collector thread, unconfigured/inactive AI,
zero reserved calls, and no fresh source checks or items. It requests orderly
shutdown through the stdin pipe of the child it created. A forced cleanup is
limited to that retained process handle and fails the acceptance result.

It then creates a complete source archive with the existing source-backup
helper, installs two immutable versions under a separate release root, activates
them in order, and rolls back using the existing install/activate/rollback
functions. The baseline differs only by a marked README addition. The receipt
checks that all three synthetic data-file families are byte-for-byte unchanged.
This proves the isolated packaging/pointer rollback mechanism, **not a rollback
of the user's production installation or production data**. The original release
drill's protected-port restrictions remain unchanged; its broad `run_drill`
entry is not called.

`--run-focused-tests` runs only `tests.test_news_portable_delivery`. Other focused
tests can be selected explicitly through `scripts/run_backend_tests_isolated.py`;
do not invoke the broad fresh-source smoke or full suite merely for this check.
`verification.json` and `child-receipt.json` distinguish actual verification from
UNKNOWN/failure and preserve errors as bounded types/codes. Successful offline
acceptance is not evidence that BLS/Seagate recovered or remained stable.
