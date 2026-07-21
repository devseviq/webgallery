# Wallpaper Library Index

`dl_engine.index_library` builds the rebuildable SQLite view used by the web
gallery. It may update SQLite and explicitly selected provider ledgers, but it
never moves, renames, deletes, sorts, or rewrites wallpaper images or their
`.wallpaper.json` sidecars.

The normal SND-HOST runtime paths are examples, not inferred defaults:

```text
Library:         F:\Wallpapers\library
Maintenance DB:  F:\Wallpapers\wallpaper_library.sqlite
Gallery DB:      F:\Wallpapers\webgallery_library.sqlite
Wallhaven ledger:F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl
Provider ledger: F:\Wallpapers\library\_metadata\provider-enrichment.v1.jsonl
```

Every live-capable CLI operation requires its applicable paths explicitly. A
Git worktree parent is never treated as the collection root.

**Module-resolution hazard (confirmed 2026-07-20 on SND-HOST):** this
repository's own `.venv` has a proper editable install
(`__editable__.webgallery-0.1.0.pth`) and resolves `dl_engine` correctly to
this worktree's `src\dl_engine`; commands below assume that venv. The *global*
interpreter is different: a separate, actively developed sibling project at
`F:\Wallpapers\dl-engine` (this gallery's `dl_engine` package was originally
derived from it) has its own user-site editable install, and its top-level
package is also named `dl_engine`. Invoking the global `python` instead of
this project's venv silently imports that sibling project's code. At the
2026-07-20 observation its `DB_SCHEMA_VERSION` was `2`, matching the sibling
database it maintained, not this schema-4 gallery index. Confirm the resolved
module before any live command:

```powershell
F:\Wallpapers\webgallery\.venv\Scripts\python.exe -c "import dl_engine; print(dl_engine.__file__)"
```

It must print a path under this worktree's `src\dl_engine`. If a command below
is ever run with a bare `python` instead of this venv's interpreter and prints
`F:\Wallpapers\dl-engine\src\dl_engine\__init__.py`, it silently ran the
sibling project's code, not this one — expect false `schema-version-mismatch`
issues if pointed at a schema-4 index for that reason alone.

**Database-ownership boundary (confirmed 2026-07-20):**
`F:\Wallpapers\wallpaper_library.sqlite` was a schema-2 maintenance index owned
by the sibling `F:\Wallpapers\dl-engine` project and its scheduled queue
maintenance. This docs pass did not recheck its current schema, and webgallery
must not migrate that protected file in place regardless. The sibling
maintainer observed then wrote schema version 2 and could invalidate a
schema-4 publication. Use a separately owned schema-4 database such as
`F:\Wallpapers\webgallery_library.sqlite`. All commands below use that separate
path unless they explicitly say they are reading a legacy database.

## Plan 004 publication authority and current state

[`GALLERY_INDEX_PUBLICATION.md`](GALLERY_INDEX_PUBLICATION.md) and
[`gallery-publication-manifest.schema.json`](../schemas/gallery-publication-manifest.schema.json)
are the authority for packaging and publishing schema 4. The Plan 004
implementation is merged and `repository-verified`: Python compilation, the
148-test targeted gate (plus 125 subtests), the 252-test full gate (plus 186
subtests), PowerShell parser/help checks, plan validation, and diff checks all
passed. This repository state does not imply a candidate, publication, or
cutover state.

This documentation reconciliation ran no new live candidate build, exhaustive
candidate verification, alternate-listener browser pass, publication, cutover,
recovery, queue/task/process observation, or canonical-database observation or
mutation. The retained
`F:\Wallpapers\webgallery_library.schema4.20260721T003322Z.candidate.sqlite`
and its report under
`F:\Wallpapers\reports\maintenance-webgallery-candidate-20260721T003322Z`
are dated diagnostics only: verification failed with 200 `layout-mismatch`
issues, the report lacked the required generated timestamp, and no Plan 004
manifest binds them. They remain `candidate-blocked` and cannot be promoted.

Keep repository status, closed manifest state, and external/diagnostic status
separate:

| Repository status | Meaning |
|---|---|
| `planned` | The contract exists, but executable implementation is incomplete. |
| `implemented` | Repository implementation exists; full repository gates are not yet recorded. |
| `repository-verified` | The current full compile, test, plan-validation, and diff gates all passed. |

| Manifest state | Meaning |
|---|---|
| `candidate-built` | A unique schema-4 candidate and initial manifest exist; verification has not been accepted. |
| `candidate-verified` | The initial exhaustive candidate report was accepted; this is still not under-hold publication authority. |
| `ready-to-publish` | Fresh under-hold verification and every pre-mutation gate passed; canonical bytes are unchanged. |
| `published` | The canonical path was activated, reopened, and exhaustively verified against the candidate. |
| `rolled-back` | The prior canonical database set was restored and verified. |

| Diagnostic/external status | Meaning |
|---|---|
| `candidate-blocked` | Candidate or report evidence failed closed. This is diagnostic shorthand, never a manifest state. |
| `cut-over` | The external listener owner started the intended listener on the verified canonical database. This is not a manifest state. |

Task-manager `executed` proves only task registration. It does not prove any of
the states above.

Before a live-capable command, establish the interpreter and paths and require
the enrolled SND-HOST identity:

```powershell
Set-Location F:\Wallpapers\webgallery
$identity = & 'C:\Users\Dev\OneDrive\common\common_dev\Get-VerifiedMachineIdentity.ps1'
if ($identity.status -ne 'VERIFIED' -or $identity.machineId -ne 'snd-host') {
    throw 'Refusing live-capable webgallery command: SND-HOST identity is not verified.'
}

$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$libraryRoot = 'F:\Wallpapers\library'
$galleryDb = 'F:\Wallpapers\webgallery_library.sqlite'
$wallhavenLedger = Join-Path $libraryRoot '_metadata\wallhaven-enrichment.v1.jsonl'
$providerLedger = Join-Path $libraryRoot '_metadata\provider-enrichment.v1.jsonl'

& $python -c "import dl_engine; print(dl_engine.__file__)"
```

The final command must resolve beneath
`F:\Wallpapers\webgallery\src\dl_engine`. A live rebuild should also run only
at an explicit maintenance boundary so the library snapshot is stable.

## Rebuild and reconciliation

The direct indexer command is a low-level offline/scratch rebuild interface.
Do not use it to replace the canonical
`F:\Wallpapers\webgallery_library.sqlite` during Plan 004 operations: it does
not create the required candidate identity, manifest chain, immutable reports,
backup, or recovery journal. Use `Prepare` and `Publish` below for a canonical
schema-4 publication.

```powershell
$scratchRoot = 'F:\Wallpapers\.wallpaper-library-maintenance\gallery-index-scratch'
New-Item -ItemType Directory -Path $scratchRoot -Force | Out-Null
$scratchDb = Join-Path $scratchRoot `
  ('webgallery_library.schema4.' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') + '.scratch.sqlite')

& $python -m dl_engine.index_library `
  --library-root $libraryRoot `
  --db-path $scratchDb `
  --ledger-path $wallhavenLedger `
  --provider-ledger-path $providerLedger
```

That output remains scratch evidence. Do not rename, copy, or reinterpret it as
a Plan 004 candidate or canonical database.

An offline rebuild recursively discovers supported images, validates sibling
sidecars, loads both durable ledgers, and falls back to canonical filename or
legacy path inference only when portable evidence is absent. It then:

1. upserts current image and sidecar tags;
2. applies Wallhaven records and captured Zerochan/Anime-Pictures records by
   stable source identity;
3. removes stale database rows and orphaned tags;
4. materializes ratings, explanations, tag counts, and global facets in the
   same transaction.

Unreadable images remain total records with unknown dimensions/layout. Invalid
sidecars are reported and ignored rather than rewritten. SQLite uses WAL with
a busy timeout so read-only consumers continue to see the last committed
snapshot during a rebuild. WAL and SHM siblings are part of the database and
must not be deleted independently while a process is using it.

## Schema 4

Schema 4 is an additive migration of the disposable gallery index. A schema-3
database receives the validated `images.nsfw_subcategory` column, its
`(content_rating, nsfw_subcategory, id)` index, an authoritative-tag backfill,
and an NSFW-only facet in one transaction. The migration
rejects future or contradictory version markers before writing. Schema and
derived publication happen in one transaction, and `PRAGMA user_version` plus
`schema_metadata.schema_version` are written last. Older supported schema-2
databases are also backfilled without changing sidecars, raw provider ledgers,
tags, or enrichment progress.

| Table | Purpose |
|---|---|
| `images` | File/source evidence plus materialized `content_rating`, `nsfw_subcategory`, `rating_confidence`, `rating_basis`, stable JSON reasons, and authoritative `tag_count`. Non-NSFW rows are constrained to `unspecified`. |
| `tags` / `image_tags` | Typed, source-qualified authoritative tags and per-image provenance. |
| `library_facets` | Snapshot counts for rating, source, orientation, resolution bucket, enrichment status, tag-count bucket, provider coverage, and tag provenance. |
| `tag_suggestions` | Review-only labels with confidence, generator/model/provenance, status, timestamps, reviewer, and note. |
| `enrichment_progress` | Provider progress telemetry; never an exclusion authority. |
| `schema_metadata` | Database schema version marker. |

`refresh_derived_metadata(conn, image_ids=None, refresh_facets=True)` is the
single publication path after ingest and authoritative provider-tag changes.
It does not commit; the caller owns the transaction. It calls the unchanged
conservative classifier using only `tags`/`image_tags`. Suggestions at every
review status are excluded, and missing evidence stays `unknown`, never SFW.
NSFW subcategory precedence is `explicit` > `fetish` > `nudity` >
`unspecified`; second-axis terms alone never bypass the overall-rating gate.
Fresh schema-4 tables enforce the allowed domain and non-NSFW cross-field
invariant with SQL `CHECK` constraints. SQLite cannot add the table-level
cross-field check in place, so schema-3 migrations install idempotent
`BEFORE INSERT`/`BEFORE UPDATE` triggers after adding the column. Transactional
backfill and exhaustive verification provide the migration and drift checks.

## Read-only verification

```powershell
$verifyDir = Join-Path 'F:\Wallpapers\reports' `
  ('maintenance-webgallery-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ'))
New-Item -ItemType Directory -Path $verifyDir | Out-Null
$verifyPath = Join-Path $verifyDir 'verify.json'

& $python -m dl_engine.index_library --verify-json `
  --library-root $libraryRoot `
  --db-path $galleryDb |
  Set-Content -Encoding utf8 $verifyPath
$verifyExit = $LASTEXITCODE
```

Verification opens SQLite in `mode=ro` and never invokes migration. It checks
quick-check, foreign keys, required schema-4 columns/tables/version markers,
filesystem/index/sidecar agreement, canonical layout and SHA identity,
materialized rating/subcategory/count parity, the non-NSFW invariant, stable
JSON reasons, confidence bounds, suggestion decisions, and the complete facet
snapshot. Exit codes are `0` for
healthy, `1` for invariant failures, and `2` for incompatible/missing inputs or
invalid command use.

The timestamped `maintenance-webgallery-*\verify.json` location is the legacy
manual-verification identity consumed by the server status API. Reports for the
sibling maintenance database may share the parent directory; database and
library-root identity must match before a report can mark a gallery snapshot
verified. A manual report does not advance a Plan 004 manifest state.

Plan 004 writes immutable, per-attempt reports beneath
`F:\Wallpapers\reports\gallery-publication`. Initial candidate, fresh
under-hold, failed-attempt, and canonical post-publication reports have distinct
paths and hashes and are bound into the manifest. Never overwrite or reuse one
as another phase's evidence.

## Plan 004 operator workflow

These commands expose the implemented interface; they do not authorize an
operation. Run them only from the canonical project root after reviewing
[`GALLERY_INDEX_PUBLICATION.md`](GALLERY_INDEX_PUBLICATION.md). The wrapper
performs the SND-HOST identity checks and pins exact production paths for
Apply. `Inspect`/`-WhatIf` is the first step and write-capable modes require
both `-Apply` and the absence of `-WhatIf`.

```powershell
Set-Location F:\Wallpapers\webgallery
$python = 'F:\Wallpapers\webgallery\.venv\Scripts\python.exe'
$publisher = 'F:\Wallpapers\webgallery\scripts\Invoke-GalleryIndexPublication.ps1'
$canonical = 'F:\Wallpapers\webgallery_library.sqlite'
$library = 'F:\Wallpapers\library'
$wallhavenLedger = 'F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl'
$providerLedger = 'F:\Wallpapers\library\_metadata\provider-enrichment.v1.jsonl'
$sibling = 'F:\Wallpapers\wallpaper_library.sqlite'
$publicationRoot = 'F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication'
$candidateRoot = Join-Path $publicationRoot 'candidates'
$manifestRoot = Join-Path $publicationRoot 'manifests'
$backupRoot = Join-Path $publicationRoot 'backups'
$journalRoot = Join-Path $publicationRoot 'journals'
$recoveryResultRoot = Join-Path $publicationRoot 'recovery-results'
$reportRoot = 'F:\Wallpapers\reports\gallery-publication'
$queueState = 'F:\Wallpapers\.wallpaper-download-queue'
$holdPath = 'F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication-hold.json'
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$candidate = Join-Path $candidateRoot "webgallery_library.schema4.$stamp.candidate.sqlite"
$manifest = Join-Path $manifestRoot "publication-$stamp.manifest.json"
$backupDirectory = Join-Path $backupRoot $stamp
$journal = Join-Path $journalRoot "activation-$stamp.journal.jsonl"
```

Inspect and preview without writes:

```powershell
& $publisher -Mode Inspect -WhatIf `
  -CanonicalDatabase $canonical -CandidateDatabase $candidate `
  -LibraryRoot $library -WallhavenLedger $wallhavenLedger `
  -ProviderLedger $providerLedger -SiblingDatabase $sibling `
  -VerificationReportRoot $reportRoot -ManifestPath $manifest `
  -BackupDirectory $backupDirectory -RecoveryJournal $journal `
  -RecoveryResultRoot $recoveryResultRoot `
  -QueueStatePath $queueState -HoldPath $holdPath
```

Prepare a unique candidate, initial exhaustive report, and manifest without
changing the canonical database:

```powershell
& $publisher -Mode Prepare -Apply `
  -CanonicalDatabase $canonical -CandidateDatabase $candidate `
  -LibraryRoot $library -WallhavenLedger $wallhavenLedger `
  -ProviderLedger $providerLedger -SiblingDatabase $sibling `
  -VerificationReportRoot $reportRoot -ManifestPath $manifest `
  -BackupDirectory $backupDirectory -RecoveryJournal $journal `
  -RecoveryResultRoot $recoveryResultRoot `
  -QueueStatePath $queueState -HoldPath $holdPath
```

Validate the manifest and its current bound bytes read-only. The Python
`validate` subcommand intentionally uses a reduced path bundle and does not
accept the wrapper-only sibling, queue, or hold arguments:

```powershell
& $python scripts\publish_gallery_index.py validate `
  --manifest $manifest --canonical-database $canonical `
  --candidate-database $candidate --library-root $library `
  --wallhaven-ledger $wallhavenLedger --provider-ledger $providerLedger `
  --verification-report-root $reportRoot `
  --backup-directory $backupDirectory `
  --recovery-journal $journal --recovery-result-root $recoveryResultRoot
```

Publish only after the external hold owner has established the required hold
and zero-writer window and the external listener owner has stopped and
acknowledged 8090. The publisher never performs those external mutations:

```powershell
& $publisher -Mode Publish -Apply -CutoverAuthorized `
  -CanonicalDatabase $canonical -CandidateDatabase $candidate `
  -LibraryRoot $library -WallhavenLedger $wallhavenLedger `
  -ProviderLedger $providerLedger -SiblingDatabase $sibling `
  -VerificationReportRoot $reportRoot -ManifestPath $manifest `
  -BackupDirectory $backupDirectory -RecoveryJournal $journal `
  -RecoveryResultRoot $recoveryResultRoot `
  -QueueStatePath $queueState -HoldPath $holdPath
```

Recover one identified transaction backward while a current external recovery
hold remains asserted. The manifest path is mandatory even though candidate,
library, ledger, report, and cutover arguments are not:

```powershell
& $publisher -Mode Recover -Apply `
  -CanonicalDatabase $canonical -ManifestPath $manifest `
  -BackupDirectory $backupDirectory -RecoveryJournal $journal `
  -RecoveryResultRoot $recoveryResultRoot `
  -QueueStatePath $queueState -HoldPath $holdPath
```

If recovery already produced continuation journal segments, pass their exact
paths in order with `-ContinuationSegments`; never discover or reorder them by
an unreviewed wildcard.

### Publication artifacts

| Artifact | Meaning |
|---|---|
| Candidate database | A unique, separately owned schema-4 SQLite main file. It is never the canonical path and is not publishable merely because it exists. |
| Manifest | The strict current state/evidence document under `...\manifests`, with immutable prior snapshots and compare-and-swap transitions. It is the publication authority, not a task-manager row. |
| Verification report | Immutable exhaustive evidence for exactly one initial, under-hold, failed, or canonical attempt under `F:\Wallpapers\reports\gallery-publication`. A report is accepted only when its bytes and identities are bound by the manifest. |
| Backup directory | A unique child under `...\backups` containing hashed copies of the exact pre-activation canonical main/WAL/SHM set. It is rollback basis, not a candidate or success marker. |
| Recovery journal | Durable append-only activation/recovery evidence under `...\journals`. It records intents and outcomes and determines whether recovery is required. |
| Recovery result | Immutable emergency-recovery evidence under `...\recovery-results`; it is reconciled to the manifest only through the documented compare-and-swap rule. |

## Queries and discovery

Read-only query and stats modes require only the explicit database path:

```powershell
& $python -m dl_engine.index_library `
  --db-path $galleryDb `
  --query "orientation=portrait source=wallhaven rating=sfw sort=newest"

& $python -m dl_engine.index_library `
  --db-path $galleryDb `
  --query "rating=nsfw nsfw_subcategory=fetish sort=newest"

& $python -m dl_engine.index_library `
  --db-path $galleryDb `
  --query "sort=least_tagged limit=250"

& $python -m dl_engine.index_library `
  --db-path $galleryDb `
  --query "sort=shuffle shuffle_seed=42 limit=250 offset=250"
```

Filters include orientation, franchise, bucket, source, exact tag,
materialized content rating, and the four NSFW subcategories. A subcategory
filter is rejected unless `rating=nsfw`. Stable sorts include the existing file/date/
resolution/size/source orders plus `least_tagged`, `rating_confidence`, and
`shuffle`. Shuffle requires an integer seed from 0 through 2147483647; the same
seed/filter/page parameters reproduce the same order without duplicates.
Offset pagination remains for this campaign; measured keyset pagination is a
follow-up.

A normal API page starts an explicit read transaction, then performs one image
SELECT, one `IN` query for every returned typed provider tag, and one batch
suggestion query. It does not aggregate the tag table for rating filters or
issue one tag query per card. The count, image rows, tags, and suggestions
therefore share one WAL snapshot; the connection rolls the read transaction
back before closing.
`counted_tag_autocomplete` validates prefix length 1-120 and limit 1-50,
treats `%`, `_`, and backslash literally, counts distinct images, and orders by
count then stable name/type/source/provenance. Suggestions are never included.

## Wallhaven enrichment and resume

```powershell
& $python -m dl_engine.index_library --enrich --max-attempts 50 `
  --library-root $libraryRoot `
  --db-path $galleryDb `
  --ledger-path $wallhavenLedger `
  --provider-ledger-path $providerLedger `
  --env-path F:\Wallpapers\webgallery\.env
```

Every `pending` Wallhaven row is discoverable, ordered by normalized source ID
and image ID. `last_processed_source_site_id` is observability only; it is not
a greater-than work predicate. Before SQLite status/progress advances, the
client appends and fsyncs an attempt record and then a success, skip, or failure
record. If execution stops after terminal ledger append but before SQLite
commit, the next rebuild restores that result.

`--max-attempts` bounds all requests. `--max-fetch` bounds successes and also
bounds attempts when no explicit attempt cap is supplied. After tests pass, a
live canary is at most one attempted row and requires a `VERIFIED` SND-HOST
identity plus recorded exact paths. A canary does not authorize the full
pending backlog.

Legacy Wallhaven metadata can be exported read-only before discarding an old
index:

```powershell
& $python -m dl_engine.index_library `
  --db-path F:\Wallpapers\wallpaper_index.sqlite `
  --export-wallhaven-ledger F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl
```

## Offline provider evidence and coverage

The generic v1 ledger accepts captured Zerochan or Anime-Pictures evidence;
it is not a scraper. A strict record carries source, source ID, status, typed
raw tags, exact provenance, capture time, and optional error. No label is
invented from filenames, search staging, or visual output.

```powershell
& $python -m dl_engine.index_library --apply-provider-ledger `
  --db-path $galleryDb `
  --provider-ledger-path $providerLedger
```

Import matches canonical source plus normalized source ID and replaces tag
associations only for that exact provenance. It never rewrites sidecars. Sparse
rows remain sparse until captured evidence exists. `provider_coverage` returns
`total_images` and groups by source/status, source/tag-count bucket, and
source/provenance; these are measurements, not completion claims.

## Review-only visual suggestions

Suggestion upsert/list/review primitives preserve label, bounded confidence,
generator, model version, provenance, status, timestamps, reviewer, and note.
Only atomic pending-to-accepted or pending-to-rejected transitions are valid.
Accepted suggestions remain in `tag_suggestions`; they are never copied into
provider tags, autocomplete, tag counts, ratings, purity, or franchise.

## Rollback and recovery

Do not manually discard or overwrite the canonical gallery database after a
Plan 004 publication attempt. If an activation journal exists, keep the
external recovery hold asserted and run the exact `Recover` command above for
that manifest, backup directory, first journal segment, and any known
continuations. Recovery converges backward only and verifies the restored
canonical set. The publisher does not release the hold or restart 8090; those
remain explicit external-owner decisions after evidence review.

If no Plan 004 transaction ever started, code rollback can restore earlier
gallery source while retaining canonical images, sidecars, both ledgers, and
the sibling-owned `wallpaper_library.sqlite`. Only a proven scratch or isolated
database with no publication transaction may be discarded and rebuilt from
durable evidence. Suggestion review can be disabled without deleting
provenance or reviewer decisions.
