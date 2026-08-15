# Versioned source backup (without Git)

`scripts/create_versioned_source_backup.py` creates an immutable ZIP snapshot of
the project source tree. It is a source-code safeguard, not a database backup,
restore utility, deployment package, or substitute for version control.

## Create a backup

The destination is always explicit and must be outside the source tree. When
`--source-root` is omitted, the source defaults to this project root.

```powershell
python scripts/create_versioned_source_backup.py create `
  --destination-root "D:\AIStudioSourceBackups"
```

For a different source directory, pass both its path and a stable, non-path
label:

```powershell
python scripts/create_versioned_source_backup.py create `
  --source-root "C:\path\to\ai_collaboration_studio" `
  --source-root-label "ai_collaboration_studio" `
  --destination-root "D:\AIStudioSourceBackups"
```

Creation fails if the same version already exists. It never overwrites an
archive. The ZIP is written to an exclusive temporary file, flushed to disk,
verified, and atomically published with no-clobber semantics.

## Preflight a future destination without writing

When a permanent destination is being selected, run `preflight` first. It
scans and hashes the source using the same exclusion rules as `create`, checks
that the destination is outside the source and has no link/reparse chain, and
reports the exact future archive name and version. It does not create the
destination directory, archive, or any temporary file.

```powershell
python scripts/create_versioned_source_backup.py preflight `
  --source-root "C:\path\to\ai_collaboration_studio" `
  --source-root-label "ai_collaboration_studio" `
  --destination-root "D:\AIStudioSourceBackups"
```

`ready:false` means the exact version already exists and a new archive would
be rejected; it never overwrites that archive.

The preflight result also reports `source_total_sha256`, `backup_version`,
`archive_path`, and whether the destination or exact archive already exists.

## Verify a backup offline

```powershell
python scripts/create_versioned_source_backup.py verify `
  "D:\AIStudioSourceBackups\ai_collaboration_studio-source-YYYYMMDDTHHMMSSZ-HASH.zip"
```

Verification reads the ZIP in place and checks its closed manifest, exact
member set, canonical paths, byte sizes, per-file SHA-256 values, and aggregate
hash. The successful JSON result also includes the final archive byte size and
archive SHA-256, so a copied archive can be checked as an exact byte object.
It does not use the network, extract files, restore files, or overwrite any
local path. A successful command prints one JSON object with `"ok":true`;
failure prints a JSON error to stderr and exits with status 2.

## Closed manifest and version identity

Every archive contains `SOURCE_BACKUP_MANIFEST.json` with exactly these fields:

- `version`: `source_backup_manifest_v1`
- `backup_version`: UTC second plus the first 12 characters of the aggregate
  content hash
- `created_at_utc`: canonical UTC timestamp
- `source_root_label`: a label only, never the absolute source path
- `file_count` and `total_size`
- `files`: sorted entries containing only `path`, `size`, and `sha256`
- `total_sha256`: SHA-256 of the canonical versioned file-entry list

The filename includes the source label and `backup_version`. Identical source
bytes, paths, label, and timestamp produce the same deterministic manifest and
ZIP bytes; an existing filename is treated as a conflict, not replaced.

## Safety boundaries and exclusions

The scanner does not follow symlinks, junctions/reparse points, or ambiguous
hard links. It rejects non-regular source entries, source changes observed
during creation, archive path escape forms, and any destination equal to or
inside the source tree. The source root, destination chain, and archive being
verified must also have unambiguous path identities.

The following are excluded from source backups:

- `.git`, `runtime`, `node_modules`, `dist`, `__pycache__`, and `secrets`
  directories (matched case-insensitively at any source-tree depth)
- `*.pyc`
- local environment and credential files such as `.env`, deployment-specific
  `.env.*` files (while keeping `.env.example`/`.env.sample`/`.env.template`),
  private key/keystore/secret suffixes, common JSON/YAML credential and token
  filenames, API-token text files, and filenames marked `密钥` or `私钥`
- the backup destination itself, enforced by requiring it to be outside source

Because `runtime` is excluded, this process does not back up the formal SQLite
database or its WAL files. Database backup and migration verification remain a
separate governed workflow. There is intentionally no restore subcommand;
recovery is a manual, separately reviewed operation.
