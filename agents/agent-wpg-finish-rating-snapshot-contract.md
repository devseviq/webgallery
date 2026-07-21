# Agent Task — Finish Rating Snapshot Contract

**Scope:** Complete the in-progress NSFW-subcategory classifier, schema-4 materialization, query/API/server contract, and snapshot-consistent page reads while preserving explicit runtime and database boundaries.

**Depends on:** none

**Output files:** `src/dl_engine/content_rating.py`, `src/dl_engine/index_library.py`, `src/dl_engine/library_browser.py`, `reports/dashboard_server.py`, `tests/test_content_rating.py`, `tests/test_index_library.py`, `tests/test_library_browser.py`, `tests/test_dashboard_server.py`, `docs/CONTENT_RATING.md`, `docs/INDEX_LIBRARY.md`, `reports/README_live_dashboard.md`

## Exit Criteria

- A one-shot iterable and a repeatable list produce identical subcategory results. Precedence is explicit, fetish, nudity, then unspecified; a subcategory term does not independently elevate a non-NSFW item.
- Rebuildable SQLite schema version 4 stores a validated `images.nsfw_subcategory`, migrates/backfills schema 3 transactionally, publishes NSFW-only subcategory facets, and verifies derived-field and facet drift.
- Only authoritative provider/sidecar tags contribute to the materialized value. Pending, rejected, or accepted machine suggestions remain excluded from content-rating evidence.
- `nsfw_subcategory` is accepted end to end by the index, CLI query, library API, server allowlist, item serialization, filters, facets, and response schema. Supplying it without `rating=nsfw` is rejected.
- Count, item, tag, and suggestion hydration for one page share one explicit SQLite read snapshot, including when another WAL connection commits between hydration stages.
- Documentation uses `F:\Wallpapers\webgallery`, the project venv, explicit runtime/database roots, alternate-listener verification, `webgallery_library.sqlite`, and preserves the sibling schema-2 boundary.
- Focused tests, compile, full tests, and `git diff --check` pass with no unexpected failures.

---

## Context — read before doing anything

1. `.continue/rules/project-conventions.md` — repository/runtime ownership and safety rules.
2. `docs/discovery-gallery-upgrade-review.md` — evidence, constraints, risks, and deferred scope.
3. `data/plans/plan-003.json` and `docs/campaign-plan-003-harden-the-wallpaper-gallery.md` — authoritative campaign contracts.
4. Inspect `git diff -- docs/CONTENT_RATING.md src/dl_engine/content_rating.py` before editing. These are preserved in-progress inputs, not disposable changes. A non-clearing recovery checkpoint exists at stash hash `2cdda350b87af5e5aec9a19a2ccc422e56a72c12`; do not apply, pop, drop, or rewrite it.
5. Read all owned source and test files. In particular inspect:
   - `classify_content`, `classify_nsfw_subcategory`, `nsfw_subcategory_tag_blob`, and `register_sqlite_function` in `src/dl_engine/content_rating.py`.
   - `DB_SCHEMA_VERSION`, `_VERIFY_REQUIRED_COLUMNS`, `_SCHEMA_SQL`, `_migrate_schema`, `ImageRow`, `refresh_derived_metadata`, `_refresh_library_facets`, `verify_library`, `query`, and CLI query wiring in `src/dl_engine/index_library.py`.
   - `RESPONSE_SCHEMA_VERSION`, `query_library_page`, `library_facets`, serialization, and suggestion hydration in `src/dl_engine/library_browser.py`.
   - `Handler._library_query_params` and library/facet route error handling in `reports/dashboard_server.py`.

---

## Task

### Part 1 — Preserve and correct the dirty classifier

- Materialize `tags` exactly once at the start of `classify_nsfw_subcategory`, then pass the same immutable collection to `classify_content` and subcategory-label normalization. Do not consume a caller iterator twice.
- Preserve the current four public constants: `nudity`, `explicit`, `fetish`, and `unspecified`.
- Preserve classifier precedence: explicit first, fetish second, nudity third, unspecified otherwise.
- Preserve the safety boundary: the helper classifies a subcategory only after the existing overall classifier returns NSFW. Subcategory-only terms must not silently elevate SFW, suggestive, or unknown content.
- Keep `wallpaper_nsfw_subcategory` deterministic and prove Python/SQLite-scalar parity.
- Add direct tests for list/generator parity, each precedence collision, purity-only NSFW, non-NSFW inputs, tag normalization, and SQL scalar parity.

### Part 2 — Materialize schema version 4 safely

- Increment only the rebuildable gallery database `DB_SCHEMA_VERSION` from 3 to 4. Do not change wallpaper sidecar schema version 1.
- Add `images.nsfw_subcategory` to required-column verification, new-schema DDL, additive migration, `ImageRow`, and row construction.
- Restrict stored values to the four public constants. New databases should enforce the value domain and prevent a specific NSFW subcategory on a non-NSFW row. For additive migrations that cannot add a table-level check in place, enforce and verify the same invariant transactionally.
- Extend `_migrate_schema` so a schema-3 database receives the column, index, marker, and derived backfill in one safe migration. A failure must roll back without advancing the version.
- Compute the field inside `refresh_derived_metadata` from the already hydrated authoritative tags and update it in the same statement/transaction as the other derived rating fields. Never read `tag_suggestions` as rating evidence.
- Add a supporting `(content_rating, nsfw_subcategory, id)` index.
- Add `nsfw_subcategory` facet rows only for `content_rating='nsfw'`; do not let the large non-NSFW population inflate `unspecified`.
- Extend exhaustive verification to recompute the value and facet counts, report drift deterministically, and accept a correctly migrated schema-4 database.
- Cover new database creation, schema-3 migration/backfill, rollback-on-failure, authoritative tag refresh, suggestion exclusion, facet counts, index presence, and verifier drift with synthetic temporary databases/images only.

### Part 3 — Complete the query/API/server contract

- Add optional `nsfw_subcategory` query support to the index and CLI, validated against the four constants.
- Fail closed when a subcategory is supplied without `content_rating='nsfw'`; do not interpret `unspecified` across non-NSFW rows.
- Serialize the field on every item, echo it in `filters`, expose constant-shape NSFW subcategory facets, and add it to the server query allowlist.
- Increment the additive library `RESPONSE_SCHEMA_VERSION` from 2 to 3. Existing clients and requests that omit the new field must continue to work.
- Preserve path-free responses, typed/provenanced tag payloads, provider coverage, review-only suggestions, seeded sorts, and all existing route allowlisting.
- Add index, API, server, and response-schema tests for valid/invalid combinations, legacy omission, item fields, facets, query forwarding, and server allowlisting.

### Part 4 — Give each page one read snapshot

- Ensure the page count, image rows, batched tags, and suggestion rows are read inside one explicit SQLite transaction on the same connection.
- Close/rollback cleanly on errors without converting read-only operations into writes.
- Add a deterministic WAL regression in which a second connection commits between page-row and hydration stages. The returned page must describe one consistent snapshot, not a mixture of commits.
- Preserve current busy-timeout/WAL behavior and read availability during rebuilds.

### Part 5 — Reconcile the documentation

- Rewrite the dirty `docs/CONTENT_RATING.md` sections so they describe the implemented schema-4 materialized contract rather than an on-the-fly filter that does not exist.
- Use the repository venv and explicit paths. Examples must target `F:\Wallpapers\webgallery_library.sqlite` and alternate listener `8091` for verification; do not instruct operators to run global Python against legacy `8090` or the sibling schema-2 database.
- Update `docs/INDEX_LIBRARY.md` with schema-4 migration, field/facet/index semantics, verification, snapshot consistency, and rebuild rollback.
- Update `reports/README_live_dashboard.md` only where needed for response schema 3, the additive query field, and the already-established alternate-listener workflow.

---

## Constraints

- Work directly from the current dirty snapshot. Preserve unrelated lines in both dirty files and keep the smallest reviewable diff.
- Do not edit `reports/library-browser.html`, `tests/test_gallery_browser_contract.py`, thumbnail files, roadmap, tracker, plan-002 artifacts, or any file outside declared ownership.
- Do not open, migrate, rebuild, or write `F:\Wallpapers\webgallery_library.sqlite`, `F:\Wallpapers\wallpaper_library.sqlite`, provider ledgers, queue state, canonical images, sidecars, or thumbnail caches.
- Do not start/stop listeners, run provider canaries, submit suggestion reviews, or send transfers.
- Do not add dependencies or redesign overall rating semantics.
- Do not implement typed multi-tag identity, durable suggestion ledgers, provider-coverage materialization, cursor pagination, bounded DOM retention, or multi-provenance evidence in this agent.

---

## Verification

Run from `F:\Wallpapers\webgallery`:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src reports tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_content_rating.py tests/test_index_library.py tests/test_library_browser.py tests/test_dashboard_server.py
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

All schema, WAL, API, and server tests must use temporary roots and databases. Report exact pass counts and any skipped check.

---

## Do NOT

- Reset, restore, stash, pop, apply, drop, or overwrite the pre-existing dirty changes.
- Edit task-manager state or mark downstream agents ready manually.
- Claim a live schema migration, live listener smoke, browser smoke, provider canary, or review submission.
- Weaken the rule that unknown content is not SFW and suggestions are not rating evidence.

---

## Post-completion

- Do not edit `live-tracker.md`; WPI owns the consolidated verified tracker update.
- Return a concise result containing changed files, schema/API decisions, tests and pass counts, remaining risks, and confirmation that no live/runtime artifact was mutated.
