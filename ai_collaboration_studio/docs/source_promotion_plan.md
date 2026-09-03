# Formal source promotion plan

`scripts/plan_formal_source_promotion.py` produces a sealed, comparison-only
plan for an exact committed source delta and one explicit formal source root.
It never copies, merges, deletes, or creates files, and a successful plan never
authorizes a later write.

Use full immutable commit object IDs. The repository and the
`ai_collaboration_studio/` project prefix are fixed by the reviewed script.
The first version requires a standalone checkout with its own real `.git`
directory; linked worktrees and bare repositories fail closed:

```powershell
python -I -B scripts/plan_formal_source_promotion.py compare `
  --formal-source-root "C:\path\to\ai_collaboration_studio" `
  --base-commit '<full-base-commit-oid>' `
  --tip-commit '<full-tip-commit-oid>'
```

The command prints one deterministic `formal_source_promotion_plan_v1` JSON
object. `plan_sha256` seals every field except itself. There is no output-file
option, timestamp, random identifier, `apply` command, or automatic merge.

## Classification

Only added and content-modified regular files are supported:

- `clean_add`: the path is absent from both the base and formal target.
- `clean_apply`: the formal target still has the canonical base content.
- `already_tip`: the formal target already has the canonical tip content.
- `manual_merge_required`: the target is occupied, missing unexpectedly, or
  differs from both immutable blobs.

`separate_write_review.eligible` is false whenever any manual merge is required.
Even when it is true, `writes_authorized` remains false, the observation is not
an atomic snapshot, and the plan is explicitly invalid as a write precondition.
A later executor must create a fresh locked preview and obtain separate write
authorization; this tool is not that executor.

## Closed read surface

The comparison surface is derived only from the NUL-delimited Git delta between
the exact base and tip commits. The formal directory is never enumerated. Only
the mapped changed paths and their existing parent components are inspected, so
formal-only modules remain outside the read surface and are not interpreted as
deletions.

Before any formal target access, the planner rejects deletes, renames, type or
mode-only changes, submodules, duplicate or case-colliding paths, path escapes,
Windows device/ADS aliases, generated/runtime/log/cache directories, SQLite
files and sidecars, and secret-like filenames. Only `.env.example`,
`.env.sample`, and `.env.template` are permitted environment templates;
`.env.local` is never eligible.

The formal root must be an explicit local fixed-drive directory, disjoint from
the repository and free of symlink/reparse aliases. Existing target files must
be independent regular files. Each is read twice through one verified file
descriptor, with content and identity rechecked, so metadata-only comparison
cannot hide a same-size mutation.

CRLF/LF comparison never sends formal bytes through a Git filter. The planner
computes raw Git blob identities in process and permits one additional
CRLF-to-LF identity only for strict UTF-8 text without unsafe control bytes.
Any custom `filter`, `ident`, or `working-tree-encoding` attribute is rejected,
and repository-local `info/attributes` is not accepted. Git queries scrub
ambient Git routing, trace, alternate-object, and configuration variables and
disable optional locks, prompts, replacement objects, and lazy object fetching.
Git is resolved once to a stable absolute executable outside the reviewed
repository, so a candidate-tree `git.exe` cannot enter command resolution. All
Git stdout is consumed through hard byte limits while the child process is still
running, and exact tree lookups use literal pathspecs rather than globs.

The sealed body binds a hash of the canonical formal-root path, its physical
root identity, and every existing or first-missing parent identity needed by a
changed target. This prevents an otherwise identical plan from being replayed
for a different formal directory or swapped parent tree.

Every observed target is read again before sealing, but those checks are still
sequential rather than one atomic filesystem snapshot. A concurrent local
writer can make the report stale after a target's final read or after the
process exits. The plan therefore supports review only and must never be used
as the direct precondition for a copy or merge.

## Boundaries

The report does not prove that formal-only modules are semantically compatible
with the candidate source. It does not back up or restore source, read or
migrate SQLite, start the application, access a network, call a Provider or
Futu/OpenD, enable monitoring, grant trading capability, merge a PR, or approve
a release. Source backup, any three-way merge, formal database migration,
runtime acceptance, and publication remain separately reviewed and authorized
operations.
