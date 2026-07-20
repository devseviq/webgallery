# Wallpaper Library Index

`dl_engine.index_library` builds a searchable SQLite view of the canonical
wallpaper library. It may create or update the database, but it never moves,
renames, or deletes wallpaper image files.

Canonical runtime paths on this machine are:

```text
Library: F:\Wallpapers\library
Index:   F:\Wallpapers\wallpaper_library.sqlite
Ledger:  F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl
```

Sibling `.wallpaper.json` files are the portable per-image authority. The
path-independent Wallhaven enrichment ledger is the durable authority for
API-only tags, franchise, purity, and enrichment status. SQLite is disposable:
it can be deleted and rebuilt from both authorities without losing metadata.
See
[`WALLPAPER_METADATA.md`](WALLPAPER_METADATA.md) for the storage contract.

## Physical layout

Normal intake writes directly to:

```text
library\<resolution-bucket>\<orientation>\<source>\<canonical-image>
```

The canonical source tokens are `wallhaven`, `zerochan`, `anime-pictures`, and
`unknown`. The index scans this tree recursively, including migrated libraries
where images are temporarily deeper or shallower. `_ExactDuplicates` is a
quarantine rather than a canonical resolution bucket.

## Rebuild and reconciliation

```powershell
Set-Location F:\Wallpapers\dl-engine
python -m dl_engine.index_library
```

An offline rebuild:

1. Recursively discovers supported image files below the library root.
2. Loads and validates each sibling `.wallpaper.json` sidecar.
3. Uses valid sidecar fields as authoritative metadata.
4. Loads `_metadata/wallhaven-enrichment.v1.jsonl`; the last valid record for
   each Wallhaven source ID wins, independent of image paths.
5. Falls back to canonical-filename parsing, then legacy filename/path
   inference when a sidecar is absent or invalid.
6. Upserts current image and sidecar tags, then applies matching Wallhaven API
   metadata so both provenance streams coexist.
7. Removes stale database rows for files no longer present and clears orphaned
   tag rows.

Unreadable images are still total records: width and height are `0`,
orientation is `unknown`, and the resolution bucket is `_UnknownResolution`.
Invalid sidecars are reported and ignored for authority; they do not prevent a
recoverable image from being indexed.

This reconciliation is what makes a post-migration refresh meaningful. If an
image moved, the refreshed database and any subsequently emitted manifest point
at its current path rather than retaining the vanished pre-move row.

The writable index connection uses SQLite WAL mode. A rebuild may hold one
large transaction while it scans the library, but read-only consumers such as
the HTML gallery continue seeing the last committed snapshot instead of
failing with `database is locked`. Writer and reader connections also use an
explicit busy timeout for short schema/checkpoint transitions. The temporary
`wallpaper_library.sqlite-wal` and `wallpaper_library.sqlite-shm` siblings are
normal while a rebuild is active and must not be deleted independently.

Override paths only for isolated tests or alternate local collections:

```powershell
python -m dl_engine.index_library `
  --library-root F:\AlternateWallpapers\library `
  --db-path F:\AlternateWallpapers\wallpaper_library.sqlite
```

## Read-only verification

Run verification immediately after a successful rebuild, before treating the
new SQLite view as ready for queries or downstream maintenance:

```powershell
python -m dl_engine.index_library
if ($LASTEXITCODE -ne 0) { throw "Wallpaper index rebuild failed." }

# Human-readable status and bounded issue samples.
python -m dl_engine.index_library --verify
if ($LASTEXITCODE -ne 0) { throw "Wallpaper library verification failed." }
```

`--verify` opens the existing database with SQLite `mode=ro`; it does not call
the schema creation/migration path. It also leaves images, sidecars, and the
Wallhaven enrichment ledger unchanged. The verifier checks:

- SQLite quick-check, foreign-key, schema-column, and schema-version health.
- A one-to-one match between indexed paths and recursively scanned canonical
  images, with no missing, outside-root, unsupported, or quarantine rows.
- A valid sibling `.wallpaper.json` for every canonical image and agreement
  among its filename, sidecar, database row, byte size, and classification.
- Exact `<resolution-bucket>/<orientation>/<source>` placement.
- A nonempty 64-hex SHA-256 on every indexed canonical row and no duplicate
  normalized SHA-256 groups. Routine verification compares the recorded
  sidecar/database values; it does not re-hash every image.

Automation should use the JSON form, which writes one stable report document
to stdout while diagnostics remain on stderr:

```powershell
python -m dl_engine.index_library --verify-json |
  Set-Content -Encoding utf8 F:\Wallpapers\reports\library-verify.json
$verifyExit = $LASTEXITCODE
```

The top-level JSON fields are `schema_version`, `ok`, `status`,
`library_root`, `db_path`, `quick_check`, `counts`, `issues_total`,
`issues_truncated`, and `issues`. Detailed issues are deterministic and capped
at 200; aggregate counters remain exact when details are truncated.

| Exit code | Meaning |
|---|---|
| `0` | Inputs are compatible and every invariant passes. |
| `1` | Inputs are readable, but one or more library/index invariants fail. |
| `2` | Command use is invalid, an input is missing/unreadable, or the database schema is incompatible. |

Verification modes cannot be combined with enrichment, queries, stats, plan
emission, ledger export, or other ingest options.

## Schema

The database schema is versioned and migrated additively. Its primary tables
are:

| Table | Purpose |
|---|---|
| `images` | One current row per image path, including source identity, canonical/original filenames, source URL, dimensions, classification, SHA-256, byte size, transport, source-relative path, recorded time, search origins, and enrichment state. |
| `tags` | Source-qualified normalized tags, including display name, slug, and type/category. |
| `image_tags` | Many-to-many relationship between images and tags, including the provenance of that claim for the individual image. |
| `enrichment_progress` | Resume checkpoint for optional site enrichment. |
| `schema_metadata` | Database schema version and migration metadata. |

Generated tag IDs are deterministic and source-qualified. Rebuilding in a new
process therefore produces the same tag identity and does not recreate the old
randomized-hash/orphan-tag problem. A valid sidecar's tags replace inferred
tags for that image; Wallhaven ledger tags are then applied alongside them.

## Queries

Queries do not rescan or mutate the library:

```powershell
# Summary counts.
python -m dl_engine.index_library --stats

# Filters can be combined.
python -m dl_engine.index_library --query "orientation=portrait source=wallhaven"
python -m dl_engine.index_library --query "source=anime-pictures tag=Evertsen"
python -m dl_engine.index_library --query "bucket=4K source=anime-pictures"
python -m dl_engine.index_library --query "orientation=landscape limit=250"
```

Supported query keys are `orientation`, `franchise`, `bucket`, `source`, `tag`,
and `limit`. Tag and franchise matching is case-insensitive; source values use
the canonical tokens above.

Tags remain many-to-many data. Saved searches and generated collections should
query the index rather than copying images into tag-named folders.

## Optional Wallhaven enrichment

Wallhaven filename metadata contains identity and dimensions but not the site's
full tag set. The optional network pass obtains those fields from the Wallhaven
detail API and is resumable:

```powershell
# Put WALLHAVEN_API_KEY in dl-engine\.env first when private-content access is
# required. SFW enrichment can operate without a key.
python -m dl_engine.index_library --enrich
python -m dl_engine.index_library --enrich --max-fetch 50
```

The client paces requests below the 45-requests-per-minute ceiling. Before a
successful API result is committed to SQLite, it is appended and flushed to
the default ledger. HTTP 401 results are also recorded as `skipped`, avoiding
the same unauthorized request after a rebuild. A malformed or truncated JSONL
line is reported and ignored; later valid records still load. Ingest statistics
report loaded, invalid, superseded, applied, and unmatched ledger records.

Use `--ledger-path` to override the ledger for an isolated library. To preserve
the API metadata in an older index, export it before discarding that database:

```powershell
python -m dl_engine.index_library `
  --db-path F:\Wallpapers\wallpaper_index.sqlite `
  --export-wallhaven-ledger `
    F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl
```

Export is a separate operation: it opens `--db-path` with SQLite `mode=ro`,
does not run schema creation or migration, merges duplicate image rows by
Wallhaven source ID, and atomically replaces the output with deterministic
JSONL. On schemas that carry association provenance, only `wallhaven-api` tags
are exported; sidecar/search claims are not relabelled as API data.

## Legacy orientation migration/repair

Normal `sort-downloads.ps1` intake already lands in
`<bucket>/<orientation>/<source>`. `sort-by-orientation.ps1` is retained for
older libraries and explicit repair manifests only.

```powershell
# Rebuild first so the manifest starts from current paths.
python -m dl_engine.index_library

# Optional: inspect the exact handoff manifest.
python -m dl_engine.index_library `
  --emit-plan F:\Wallpapers\orientation_plan.csv

# Preview is non-destructive and writes an audit report.
Set-Location F:\Wallpapers
.\sort-by-orientation.ps1

# Apply only after reviewing the report.
.\sort-by-orientation.ps1 -Apply

# Reconcile SQLite with the moved paths.
Set-Location F:\Wallpapers\dl-engine
python -m dl_engine.index_library
```

The CSV handoff contains `SourcePath`, `DestinationDir`, `Orientation`, and
`Source`. `DestinationDir` is always relative to the library and includes the
source component, for example `4K/portrait/anime-pictures` or
`_UnknownResolution/unknown/unknown`.

The PowerShell tool validates the complete manifest before moving the first
file, confines every source and destination to the library root, and refuses an
apply while queue-browser is open. Canonical images and their
`.wallpaper.json` sidecars move as one pair. An exact destination is a no-op;
any conflicting image or sidecar aborts before mutation rather than inventing
a filename that would disagree with the metadata. This makes a freshly rebuilt
manifest idempotent without stranding or invalidating sidecars.

## Tests

```powershell
# Index parsing, metadata authority, reconciliation, queries, and plan output.
python -m pytest tests/test_index_library.py

# Preview/apply/recursive-refresh/idempotency integration.
pwsh -File tests\powershell\SortByOrientation.Tests.ps1

# Canonical filename/sidecar generation during normal intake.
pwsh -File tests\powershell\SortDownloads.Tests.ps1
```

The PowerShell regression verifies that the refreshed database and manifest
contain the moved current paths before asserting that a second apply performs
no move or suffix rename.
