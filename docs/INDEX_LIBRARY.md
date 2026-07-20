# Wallpaper Library Index

`dl_engine.index_library` builds the rebuildable SQLite view used by the web
gallery. It may update SQLite and explicitly selected provider ledgers, but it
never moves, renames, deletes, sorts, or rewrites wallpaper images or their
`.wallpaper.json` sidecars.

The normal SND-HOST runtime paths are examples, not inferred defaults:

```text
Library:         F:\Wallpapers\library
Index:           F:\Wallpapers\wallpaper_library.sqlite
Wallhaven ledger:F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl
Provider ledger: F:\Wallpapers\library\_metadata\provider-enrichment.v1.jsonl
```

Every live-capable CLI operation requires its applicable paths explicitly. A
Git worktree parent is never treated as the collection root.

## Rebuild and reconciliation

```powershell
Set-Location F:\Wallpapers\webgallery
python -m dl_engine.index_library `
  --library-root F:\Wallpapers\library `
  --db-path F:\Wallpapers\wallpaper_library.sqlite `
  --ledger-path F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl `
  --provider-ledger-path F:\Wallpapers\library\_metadata\provider-enrichment.v1.jsonl
```

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

## Schema 3

Schema 3 is an additive migration of the disposable index. The migration
rejects future or contradictory version markers before writing. Schema and
derived publication happen in one transaction, and `PRAGMA user_version` plus
`schema_metadata.schema_version` are written last. A schema-2 database is
backfilled from its current purity and authoritative `image_tags` without
changing sidecars, raw provider ledgers, tags, or enrichment progress.

| Table | Purpose |
|---|---|
| `images` | File/source evidence plus materialized `content_rating`, `rating_confidence`, `rating_basis`, stable JSON reasons, and authoritative `tag_count`. |
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

## Read-only verification

```powershell
python -m dl_engine.index_library --verify-json `
  --library-root F:\Wallpapers\library `
  --db-path F:\Wallpapers\wallpaper_library.sqlite |
  Set-Content -Encoding utf8 F:\Wallpapers\reports\library-verify.json
$verifyExit = $LASTEXITCODE
```

Verification opens SQLite in `mode=ro` and never invokes migration. It checks
quick-check, foreign keys, required schema-3 columns/tables/version markers,
filesystem/index/sidecar agreement, canonical layout and SHA identity,
materialized rating/count parity, stable JSON reasons, confidence bounds,
suggestion decisions, and the complete facet snapshot. Exit codes are `0` for
healthy, `1` for invariant failures, and `2` for incompatible/missing inputs or
invalid command use.

## Queries and discovery

Read-only query and stats modes require only the explicit database path:

```powershell
python -m dl_engine.index_library `
  --db-path F:\Wallpapers\wallpaper_library.sqlite `
  --query "orientation=portrait source=wallhaven rating=sfw sort=newest"

python -m dl_engine.index_library `
  --db-path F:\Wallpapers\wallpaper_library.sqlite `
  --query "sort=least_tagged limit=250"

python -m dl_engine.index_library `
  --db-path F:\Wallpapers\wallpaper_library.sqlite `
  --query "sort=shuffle shuffle_seed=42 limit=250 offset=250"
```

Filters include orientation, franchise, bucket, source, exact tag, and
materialized content rating. Stable sorts include the existing file/date/
resolution/size/source orders plus `least_tagged`, `rating_confidence`, and
`shuffle`. Shuffle requires an integer seed from 0 through 2147483647; the same
seed/filter/page parameters reproduce the same order without duplicates.
Offset pagination remains for this campaign; measured keyset pagination is a
follow-up.

A normal page performs one image SELECT, one `IN` query for every returned
typed provider tag, and one batch suggestion query. It does not aggregate the
tag table for rating filters or issue one tag query per card.
`counted_tag_autocomplete` validates prefix length 1-120 and limit 1-50,
treats `%`, `_`, and backslash literally, counts distinct images, and orders by
count then stable name/type/source/provenance. Suggestions are never included.

## Wallhaven enrichment and resume

```powershell
python -m dl_engine.index_library --enrich --max-attempts 50 `
  --library-root F:\Wallpapers\library `
  --db-path F:\Wallpapers\wallpaper_library.sqlite `
  --ledger-path F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl `
  --provider-ledger-path F:\Wallpapers\library\_metadata\provider-enrichment.v1.jsonl `
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
python -m dl_engine.index_library `
  --db-path F:\Wallpapers\wallpaper_index.sqlite `
  --export-wallhaven-ledger F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl
```

## Offline provider evidence and coverage

The generic v1 ledger accepts captured Zerochan or Anime-Pictures evidence;
it is not a scraper. A strict record carries source, source ID, status, typed
raw tags, exact provenance, capture time, and optional error. No label is
invented from filenames, search staging, or visual output.

```powershell
python -m dl_engine.index_library --apply-provider-ledger `
  --db-path F:\Wallpapers\wallpaper_library.sqlite `
  --provider-ledger-path F:\Wallpapers\library\_metadata\provider-enrichment.v1.jsonl
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

## Rollback

Stop gallery writers, retain canonical images, sidecars, and both ledgers, then
discard only the rebuildable SQLite database and its stopped WAL/SHM siblings.
Restore the prior code and rebuild the prior schema from durable evidence. Do
not edit images or sidecars to roll back an index migration. Suggestion review
can be disabled without deleting provenance or reviewer decisions.
