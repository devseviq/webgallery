# Agent Task — Optimize and Enrich Gallery Index

**Scope:** Materialize gallery ratings and facets, batch tag hydration, add counted tag discovery and deterministic sorts, repair provider enrichment progress, and store visual tags only as reviewable suggestions.

**Depends on:** Agent WPD

**Output files:** src/dl_engine/index_library.py, src/dl_engine/library_browser.py, tests/test_index_library.py, tests/test_library_browser.py, docs/INDEX_LIBRARY.md, docs/CONTENT_RATING.md

## Exit Criteria

- Disposable SQLite schema version 3 materializes content rating, confidence, basis, reasons, tag count, and global facet counts without changing sidecar schema version 1.
- A normal page query selects images once and batch-loads all page tags in one query; content-rating filters no longer aggregate the whole tag table at request time.
- Counted prefix tag autocomplete, least-tagged/rating-confidence triage, and seeded deterministic shuffle are validated API contracts with stable pagination.
- API items contain safe thumbnail/original URLs, typed provider tags, materialized rating explanation, provider coverage, and separately serialized suggestions; they do not expose arbitrary filesystem or database paths.
- Wallhaven enrichment processes every pending row regardless of lexicographic relation to an old cursor and writes durable attempt/result evidence before progress telemetry advances.
- A generic durable provider ledger can import captured typed Zerochan or Anime-Pictures evidence without fabricating labels or rewriting raw sidecar/provider tags.
- Visual suggestions preserve label, confidence, generator/model version, provenance, review status, timestamps, and reviewer decision. Suggestions never become provider tags or content-rating evidence.
- Migration, rebuild, incremental refresh, query-count, provider-resume, suggestion-boundary, and browser API tests pass.

---

## Context — read before doing anything

1. .continue/rules/project-conventions.md — SQLite is rebuildable; Python must never move, sort, finalize, delete, or rewrite wallpaper files.
2. docs/campaign-plan-002-secure-and-improve-the.md — schema version 3 contract, invariants, risk controls, and WPE-to-WPF API.
3. docs/discovery-gallery-sorting-presentation.md — current tag/source counts, N+1 query evidence, runtime rating aggregate, unsafe Wallhaven cursor, and provider-quality gaps.
4. Agent WPD result and src/dl_engine/gallery_thumbnails.py — consume the exact /thumb, /original, tag-autocomplete, and suggestion-review route shapes; do not edit WPD files.
5. src/dl_engine/index_library.py — DB_SCHEMA_VERSION, _SCHEMA_SQL, _migrate_schema, ImageRow, ingest_library, _replace_image_tags, query, enrich_wallhaven, provider ledger helpers, verify_library, and main.
6. src/dl_engine/library_browser.py — _serialize_row, query_library_page, library_facets, filter validation, paging, and verification status.
7. src/dl_engine/content_rating.py — classify_content is the unchanged authority for derived rating. Missing evidence must remain unknown.
8. src/dl_engine/wallpaper_metadata.py and schemas/wallpaper-metadata.schema.json — read only. Sidecar schema version 1 and raw tags remain unchanged.
9. tests/test_index_library.py — migration fixtures, WAL/read-only tests, typed tags, Wallhaven ledger, enrichment retry/resume, verifier, and surface constraints.
10. tests/test_library_browser.py — current API serialization, ratings, facets, sorts, pagination, and outside-root behavior.
11. tests/fixtures/ap_post.html — evidence that captured Anime-Pictures data may contain richer typed tag metadata; do not infer provider fields absent from a captured record.

Before editing, read the complete WPD completion summary and confirm its route signatures. If WPD changed a contract from the campaign document, preserve the safer behavior and report the mismatch rather than editing WPD-owned files.

---

## Task

### Part 1 — Migrate the rebuildable index to schema version 3

Increment DB_SCHEMA_VERSION from 2 to 3. Keep migration additive and idempotent.

Add to images:

- content_rating TEXT NOT NULL DEFAULT 'unknown'
- rating_confidence REAL NOT NULL DEFAULT 0
- rating_basis TEXT NOT NULL DEFAULT 'no-signal'
- rating_reasons_json TEXT NOT NULL DEFAULT '[]'
- tag_count INTEGER NOT NULL DEFAULT 0

Add library_facets with facet, value, count, and refreshed_at; primary key is facet plus value.

Add tag_suggestions with:

- integer primary key and image_id foreign key with ON DELETE CASCADE;
- label and normalized_label;
- confidence constrained from 0 through 1;
- generator, model_version, and provenance;
- review_status constrained to pending, accepted, or rejected;
- created_at, reviewed_at, reviewer, and decision_note;
- a deterministic uniqueness constraint over image, normalized label, generator, and model version.

Add indexes for content_rating, tag_count, download_recorded_at, size_bytes, franchise, and stable suggestion lookup. Retain all existing indexes.

Update _VERIFY_REQUIRED_COLUMNS and verify_library so schema 3, facet rows, derived fields, JSON reasons, rating values, confidence bounds, tag counts, suggestion statuses/confidence, and foreign keys are checked read-only. Sidecar schema remains 1.

Migration from schema 2 must backfill derived values from current purity plus authoritative image_tags, refresh facets, preserve tags/ledgers, and be safe to rerun. Tests must prove migration changes only SQLite.

### Part 2 — Centralize derived metadata publication

Add one typed refresh function that accepts all images or a bounded image-ID collection. For each affected image:

- batch-read authoritative tags from image_tags/tags;
- call the existing classify_content with purity and those tags;
- store rating, confidence, basis, stable JSON reasons, and tag_count;
- exclude tag_suggestions at every status.

Refresh library_facets transactionally after ingest/rebuild and provider-tag changes. Materialize at least rating, source, orientation, and resolution_bucket. Include provider enrichment coverage and tag-count buckets either in the table or a separately documented constant-shape query.

Call the refresh function after ingest_library replaces tags, after Wallhaven/provider ledger application, and after migration. A suggestion review must not refresh ratings because suggestions are not rating evidence.

Add parity tests comparing a clean rebuild, a schema-2 migration, and incremental provider-tag updates.

### Part 3 — Remove avoidable query work and add discovery sorts

Refactor query:

- filter content_rating directly on images.content_rating;
- run the image SELECT once;
- collect returned IDs and load all typed/provenanced tags with one IN query;
- group tags in Python by image ID with stable case-insensitive name/type ordering;
- never run one tag SELECT per image.

Keep current filters and deterministic tie breakers. Add:

- least_tagged: tag_count ascending, then newest/id tie breakers;
- rating_confidence: confidence ascending, then tag_count/id;
- shuffle: deterministic pseudo-random order from integer shuffle_seed plus image ID, then image ID. The seed is validated and must survive every page; the same seed/query returns the same order without duplicates.

Use safe parameterization or a validated bounded integer; never interpolate arbitrary order expressions. Add an SQLite trace/query-count test proving a normal page does not scale query count with item count. Add supporting indexes and query-plan assertions that are stable enough for SQLite fixtures.

Offset pagination may remain for this campaign if seeded shuffle and all existing sorts remain stable. Document keyset pagination as a measured follow-up rather than combining it with this schema migration.

### Part 4 — Add counted tag autocomplete and safe API serialization

In index_library.py add a counted prefix lookup over authoritative tags. Validate prefix length 1 through 120 and limit 1 through 50. Return name, type, provenance/source grouping as appropriate, and distinct image count, ordered by count descending then case-insensitive name/type. Do not include pending/rejected/accepted machine suggestions in provider-tag autocomplete.

In library_browser.py:

- validate seed and new sort names;
- add a tag_autocomplete entry point for WPD /api/library/tags;
- read library_facets instead of runtime rating aggregation;
- serialize materialized rating fields and typed provider tags;
- emit thumbnail_url from full SHA-256 and original_url from integer image ID only when the canonical file exists;
- stop emitting absolute image paths and the absolute DB path. Keep url as a temporary alias of original_url only if required by current WPD/WPF compatibility, and document its removal.
- include tag_count, enrichment_status, and separately grouped tag_suggestions.

Bump the browser response schema version. Keep transfer selection by image ID unchanged. Outside-root/missing rows get no media URLs.

### Part 5 — Repair Wallhaven resume and add provider evidence import

Replace the enrich_wallhaven candidate predicate that requires source_site_id greater than last_processed_source_site_id. Pending status is the work queue:

- SELECT source wallhaven and enrichment_status pending;
- order deterministically by normalized source_site_id and image ID;
- process a bounded batch;
- append the durable ledger record before applying it to SQLite;
- update progress only as observability, never as an exclusion predicate.

Add tests with pending IDs both below and above a stored cursor, mixed case, failures, skips, interruption after ledger append, rebuild from ledger, and a second resume. Every pending row must remain discoverable until it receives a durable terminal record.

Define one generic provider-enrichment ledger v1 under library/_metadata for captured Zerochan or Anime-Pictures evidence. A normalized record contains source, source_id, status, typed raw tags, provenance, captured_at, and optional error. Provide:

- strict normalize/load/append helpers;
- an offline CLI import/apply mode;
- stable source plus source_id matching;
- provider-tag replacement only for the record's exact provenance;
- coverage reporting by source, status, tag-count bucket, and provenance.

Do not implement speculative HTML/visual classification as provider evidence. Zerochan rows with only a staging search tag remain sparse until captured provider data exists. Anime-Pictures typed data may be imported only when present in captured provider material. Never rewrite sidecars to make them appear enriched.

After tests pass, a live Wallhaven canary of at most one successful fetch may run only after the machine verifier returns VERIFIED and the current DB/ledger paths are explicitly recorded. Report before/after pending counts and durable ledger evidence. Do not launch the full 11,923-row network run in this agent.

Make library, database, ledger, and environment paths explicit CLI inputs for
all live-capable index/enrichment operations. Do not derive the live collection
from REPO_ROOT.parent: an agent worktree parent is not the collection root.

### Part 6 — Keep visual tags reviewable and separate

Add typed functions to:

- insert/upsert a suggestion with normalized label, bounded confidence, nonempty generator/model/provenance, and pending status;
- list suggestions for a page in one batch;
- review one suggestion with accepted or rejected status, reviewed_at, reviewer, and optional note;
- reject invalid transitions and unknown IDs.

Expose library_browser review helpers matching WPD's same-origin POST route. Open a short-lived WAL-compatible write connection that validates schema version 3 without invoking migration. Return a stable JSON object.

Accepted suggestions remain in tag_suggestions and are not copied into tags/image_tags. No suggestion status may affect provider autocomplete, content_rating, purity, franchise, or raw tag counts. Tests must prove this with explicit adult-looking suggestion labels at pending, accepted, and rejected statuses.

### Part 7 — Documentation and completion

Update docs/INDEX_LIBRARY.md with schema 3, migration/rebuild behavior, materialized fields/facets, query batching, sorts/seed, autocomplete, provider ledger, coverage report, repaired Wallhaven resume, canary/full-run boundary, and rollback to rebuildable SQLite.

Update docs/CONTENT_RATING.md with materialization timing, unchanged classifier authority, unknown-is-not-SFW rule, and total exclusion of visual suggestions from ratings.

Append WP-GALLERY-INDEX-001 to live-tracker.md after re-reading current content. Although live-tracker is registered to WPD, this update is sequential after WPD; preserve WPD's row. Record schema/tests/canary separately and never claim the 11,923 or 28,310 backlogs were completed if only infrastructure/canary shipped.

---

## Constraints

- Modify only listed source/test/docs files plus the required sequential live-tracker entry. Do not edit WPD server/report files or WPF HTML/roadmap files.
- Preserve unrelated dirty changes in index_library.py, tests, and docs.
- Do not change content_rating.py, wallpaper sidecar schema, wallpaper_metadata.py, provider raw sidecars, canonical images, or physical layout.
- Do not infer SFW from absent evidence and do not let suggestions enter authoritative tags or ratings.
- Do not run migration, provider imports, suggestion writes, or enrichment against live state without VERIFIED snd-host identity.
- Keep network code limited to the existing Wallhaven path and its rate/retry contracts. Generic provider ledger import is offline evidence ingestion, not a new scraper.

---

## Verification

Run:

    python -m compileall -q src tests
    python -m pytest -q tests/test_index_library.py tests/test_library_browser.py tests/test_content_rating.py
    python -m pytest -q
    git diff --check -- src/dl_engine/index_library.py src/dl_engine/library_browser.py tests/test_index_library.py tests/test_library_browser.py docs/INDEX_LIBRARY.md docs/CONTENT_RATING.md live-tracker.md

Add a fixture-level query counter proving page tag hydration is constant-shape. Record before/after timing on a copied database; do not use the live database for development tests.

For the optional one-item live canary, first require:

    & 'C:/Users/Dev/OneDrive/common/common_dev/Get-VerifiedMachineIdentity.ps1'

Then record and pass exact --library-root, --db-path, --ledger-path, and
--env-path values, run at most one successful Wallhaven fetch, run verify-json,
and report remaining pending count. Stop on identity mismatch, auth/rate error,
ledger write failure, or verification failure.

---

## Do NOT

- Use last_processed_source_site_id as a greater-than work exclusion.
- Run one tag query per returned row or aggregate all tags during each rating-filter request.
- Merge provider tags, canonical aliases, visual suggestions, and human decisions into one indistinguishable table.
- Copy accepted suggestions into provider tags or use them for safety classification.
- Rewrite sidecars, images, queue state, transfer state, or report HTML.
- Claim provider coverage improved without measured durable evidence.

---

## Post-completion

Update live-tracker.md sequentially:

| ID | Status | Owner | Scope | Issue | Update |
|---|---|---|---|---|---|
| WP-GALLERY-INDEX-001 | Done or Working | agent-wpe | schema 3, materialized facets/ratings, batch tags, discovery API, provider ledger, suggestion layer | Make gallery queries fast and metadata richer without losing provenance | Record tests, migration proof, query count, canary status, remaining provider counts, and any deferred live rebuild. |
