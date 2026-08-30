# Official source monitoring baseline

## Scope

This baseline was recorded before implementing `feature/official-source-monitoring`.
The feature scope is limited to phases 0 through 2:

- monitoring contracts, persisted adapter state, run history, scheduling, and
  failure backoff;
- continuous SEC EDGAR and fixed-company IR polling;
- deterministic delivery into the existing Source Inbox;
- zero Provider calls, zero market calls, no formal round creation, and no
  execution capability.

Telegram, TDLib, Telegram Bot, ChatGPT browser automation, arbitrary-URL
crawling, live trading, order/account/payment capabilities, macro adapters,
Futu anomaly detection, trading-impact rules, notifications, and automatic
worker startup remain outside this implementation slice.

## Source identity

- Repository: `TheDeadly-cat/hajimi-jiaoyifenxi`
- Baseline branch: `main`
- Baseline commit: `a36e73bb3dc1d81fc468d40194a7b0f74adb4797`
- Development branch: `feature/official-source-monitoring`
- Baseline date: `2026-08-31` (`Asia/Shanghai`)

The remote `main`, clone `HEAD`, and `origin/main` were rechecked before the
baseline and matched the commit above. The public clone was clean.

The non-Git local source directory was compared without reading `.env.local`,
runtime data, SQLite files, or logs. After Git clean-filter normalization, 596
of 606 tracked project files matched. The ten real differences and 49 local
extra files belong mainly to the discontinued Telegram work and a separate set
of general OpenAI transport-hardening candidates. None were copied into this
branch, and the local source directory was not overwritten.

## Commands and results

All test commands used `AI_STUDIO_SKIP_LOCAL_ENV=1`. No formal database was
opened, no Provider or Futu/OpenD connection was made, and no formal service was
started.

### Full isolated backend regression

```powershell
python scripts/run_backend_tests_isolated.py --layer full --verbosity 1 --durations 30
```

- Started: `2026-08-31T01:06:14.2439357+08:00`
- Finished: `2026-08-31T01:24:03.4782579+08:00`
- Wrapper duration: `1069.208 s`
- Result: `Ran 1451 tests in 1066.926s` / `OK`
- Exit code: `0`
- Network audit: 425 allowed loopback connections, 0 blocked attempts, 0 child
  blocked attempts, non-loopback forbidden, protected ports `8770` and `11111`
  forbidden.
- The isolated runtime was deleted after completion.

### Guarded frontend regression

Dependencies were installed into the ignored clone-local `node_modules` using:

```powershell
npm.cmd --prefix frontend ci --ignore-scripts
npm.cmd --prefix frontend test
```

- Dependency install: `4.021 s`, exit `0`, 106 packages, 0 reported
  vulnerabilities from this npm audit invocation.
- Frontend tests: `38.055 s`, exit `0`, 109/109 files and 573/573 tests passed.
- The `test` script resolved to `frontend/scripts/run-tests-safe.ps1`; direct
  `node --test` was not used.

### Deterministic static security contract

```powershell
python scripts/run_static_security_checks.py --report <system-temp-report>
```

- Duration: `0.493 s`
- Result: 7/7 checks passed; 606 published files and 592 text files inspected;
  0 high-confidence secret findings and 0 network requests.
- Report: `static_security_scan_v1`
- This does not claim complete SAST, dependency CVE review, or penetration
  testing.

### Fresh-source smoke

```powershell
python scripts/run_fresh_source_smoke.py --report <system-temp-report> --allow-dependency-downloads
```

- Duration: approximately `110.338 s`
- Result: exit `0`; 606 manifest files; bootstrap passed; production build
  processed 1,755 modules; targeted backend contracts passed 9/9; guarded
  frontend tests passed 573/573.
- The temporary server used random loopback port `61581` and was stopped.
- Protected ports `8770`, `11111`, and `18787` were absent before, during, and
  after the smoke.
- `local_env_skipped=true`, `provider_credentials_removed=true`,
  `application_started=false`, `formal_database_opened=false`,
  `formal_database_unchanged=true`, and the temporary work directory was
  deleted.

## Existing migration and rollback boundary

`StudioStore` may initialize or migrate schemas only for isolated system-temp
databases. Formal startup opens an already verified schema and performs no
automatic migration. Any new monitoring tables must therefore be added to the
same controlled schema initializer and exercised through the existing
`database_migration` preview/prepare/apply gate. Disabling monitoring must leave
existing rooms, materials, Source Inbox items, Manual ChatGPT state, and round
draft behavior unchanged; monitoring data remains readable.

## Baseline conclusion

No pre-existing test failure was observed. Subsequent regressions in affected
contracts are therefore treated as feature changes until proven otherwise.
Passing this baseline does not establish browser acceptance, official-source
availability, domain truth, profitability, live-trading authorization, or
public-release authorization.
