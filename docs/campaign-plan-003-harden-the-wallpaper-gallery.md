# Campaign — Harden the wallpaper gallery by completing the NSFW-subcategory contract, ensuring snapshot-consistent reads, improving accessibility, full-resolution loading, URL navigation, and responsive behavior, and adding regression verification while preserving current dirty work and deferring larger pagination and provenance changes.

**Plan ID:** plan-003
**Date:** 2026-07-20
**Status:** executed
**Plan file:** data/plans/plan-003.json
**Plan doc:** docs/campaign-plan-003-harden-the-wallpaper-gallery.md
**Planner kind:** planner

---

## 1. Goal

Finish the in-progress NSFW-subcategory work as a schema-4, snapshot-consistent, end-to-end gallery contract; harden discovery interactions for accessibility, bandwidth, browser navigation, and small screens; and add regression evidence without mutating canonical assets, sibling databases, active queues, provider ledgers, or Plan 002 history. Larger typed multi-tag, cursor and bounded-DOM, durable suggestion-ledger, provider-coverage materialization, and multi-provenance work remains explicit follow-on.

## 2. Exit Criteria

- One-shot and repeatable tag iterables produce identical NSFW-subcategory classifications, with explicit, fetish, nudity, and unspecified precedence covered by tests.
- SQLite schema version 4 materializes validated NSFW subcategories from authoritative evidence, migrates schema 3 safely, publishes NSFW-only facets and a supporting index, excludes pending suggestions, and verifies/backfills without canonical-media mutation.
- The index, library API, server allowlist, response schema, browser control, active chips, detail facts, and URL state support nsfw_subcategory only with rating=nsfw while legacy gallery queries remain compatible.
- Each gallery page response reads count, items, tags, and suggestions from one explicit SQLite snapshot, with a writer-between-hydration regression test.
- Rating controls expose programmatic state, announcements are concise, focus and reduced-motion behavior are consistent, touch targets and small-screen controls remain usable, and Back/Forward restores bookmarkable gallery state.
- Detail navigation keeps thumbnails by default, loads originals only on explicit request, suppresses stale navigation load events, and preserves the direct Open original action.
- Interrupted thumbnail generation re-raises cancellation, removes only its UUID temporary artifact, publishes no final artifact, and retains useful stage-specific diagnostics.
- Content-rating, index, live-dashboard, roadmap, and tracker documentation uses the webgallery repository, project venv, explicit roots, alternate-listener verification, schema-4 gallery database, and preserves the sibling schema-2 boundary.
- Compile, focused tests, full tests, static browser contracts, synthetic HTTP probes, schema migration and WAL concurrency tests, browser keyboard and responsive smoke, and git diff checks pass with zero unexpected failures.
- No live provider, review, transfer, queue, canonical-media, or listener cutover mutation occurs; blocked promotion gates remain explicit until identity and zero-child prerequisites are met.

## 3. Impact Assessment

| File | Current Lines | Change Type | Risk |
| --- | --- | --- | --- |
| src/dl_engine/content_rating.py |  | modify | high |
| src/dl_engine/index_library.py |  | modify | high |
| src/dl_engine/library_browser.py |  | modify | high |
| reports/dashboard_server.py |  | modify | high |
| tests/test_content_rating.py |  | modify | high |
| tests/test_index_library.py |  | modify | high |
| tests/test_library_browser.py |  | modify | high |
| tests/test_dashboard_server.py |  | modify | high |
| docs/CONTENT_RATING.md |  | modify | high |
| docs/INDEX_LIBRARY.md |  | modify | high |
| reports/README_live_dashboard.md |  | modify | high |
| reports/library-browser.html |  | modify | high |
| tests/test_gallery_browser_contract.py |  | modify | high |
| src/dl_engine/gallery_thumbnails.py |  | modify | medium |
| tests/test_gallery_thumbnails.py |  | modify | medium |
| docs/GALLERY_ROADMAP.md |  | modify | medium |
| live-tracker.md |  | modify | medium |

## 4. Agent Roster

| Letter | Name | Scope | Deps | Files Owned | Group | Complexity |
| --- | --- | --- | --- | --- | --- | --- |
| wpg | finish-rating-snapshot-contract | Complete the in-progress NSFW-subcategory classifier, schema-4 materialization, query/API/server contract, and snapshot-consistent page reads while preserving explicit runtime and database boundaries. |  | src/dl_engine/content_rating.py, src/dl_engine/index_library.py, src/dl_engine/library_browser.py, reports/dashboard_server.py, tests/test_content_rating.py, tests/test_index_library.py, tests/test_library_browser.py, tests/test_dashboard_server.py, docs/CONTENT_RATING.md, docs/INDEX_LIBRARY.md, reports/README_live_dashboard.md | 0 | high |
| wph | harden-gallery-interactions | Add the NSFW-subcategory discovery control, accessible state and announcements, opt-in full-resolution loading, Back/Forward URL hydration, and responsive control hardening. | wpg | reports/library-browser.html, tests/test_gallery_browser_contract.py | 1 | high |
| wpi | verify-gallery-upgrades | Preserve and regression-test interrupted thumbnail cleanup, run integration and browser verification, and record current implementation and deferred promotion gates. | wph | src/dl_engine/gallery_thumbnails.py, tests/test_gallery_thumbnails.py, docs/GALLERY_ROADMAP.md, live-tracker.md | 2 | medium |

## 5. Dependency Graph

```text
Group 0: wpg
Group 1: wph
Group 2: wpi
```

## 6. File Ownership Map

| File | Owner |
| --- | --- |
| src/dl_engine/content_rating.py | wpg |
| src/dl_engine/index_library.py | wpg |
| src/dl_engine/library_browser.py | wpg |
| reports/dashboard_server.py | wpg |
| tests/test_content_rating.py | wpg |
| tests/test_index_library.py | wpg |
| tests/test_library_browser.py | wpg |
| tests/test_dashboard_server.py | wpg |
| docs/CONTENT_RATING.md | wpg |
| docs/INDEX_LIBRARY.md | wpg |
| reports/README_live_dashboard.md | wpg |
| reports/library-browser.html | wph |
| tests/test_gallery_browser_contract.py | wph |
| src/dl_engine/gallery_thumbnails.py | wpi |
| tests/test_gallery_thumbnails.py | wpi |
| docs/GALLERY_ROADMAP.md | wpi |
| live-tracker.md | wpi |

## 7. Conflict Zone Analysis

| Conflict Zone | Affected? | Mitigation |
| --- | --- | --- |
| reports/dashboard_server.py, src/dl_engine/library_browser.py | yes | WPG owns both the HTTP query allowlist and response builder so the additive schema-4 field ships as one contract. |
| reports/library-browser.html, src/dl_engine/library_browser.py | yes | WPG defines and tests the API first; WPH depends on WPG and consumes only the finalized field, validation, facet, and URL semantics. |
| docs/INDEX_LIBRARY.md, src/dl_engine/index_library.py | yes | WPG owns both schema/query code and its migration, verification, facet, and rollback documentation. |
| pre-existing dirty product files | yes | Treat the current dirty snapshot as input: WPG preserves and finishes rating changes, WPI preserves and tests thumbnail cleanup, and no reset, stash, or blanket replacement is allowed. |

## 8. Integration Points

- WPG materializes schema-4 nsfw_subcategory values and exposes validated facets, item fields, filters, and response schema; WPH consumes that exact contract in browser state, controls, chips, details, and URLs.
- WPG wraps count, image, tag, and suggestion hydration in one read snapshot; WPI retains the concurrency regression in focused and full verification.
- WPH changes original loading, history navigation, accessibility state, and responsive layout; WPI verifies those behaviors through static contracts, synthetic HTTP, and the established alternate-listener browser smoke.
- WPI preserves the existing interrupted-thumbnail cleanup semantics and adds cancellation regression coverage without changing WPG or WPH files.
- Plan 002 artifacts and live runtime resources are read-only inputs; Plan 003 records new evidence without rewriting Plan 002 lifecycle history or crossing sibling database and queue boundaries.

## 9. Schema Changes

- Increment rebuildable gallery SQLite DB_SCHEMA_VERSION from 3 to 4; wallpaper sidecar schema version 1 is unchanged.
- Add images.nsfw_subcategory with values nudity, explicit, fetish, or unspecified; default and backfill to unspecified, and prevent non-NSFW rows from holding a specific NSFW subcategory.
- Compute nsfw_subcategory in refresh_derived_metadata from authoritative provider/sidecar tags only, with precedence explicit then fetish then nudity then unspecified; pending or rejected suggestions remain excluded.
- Publish nsfw_subcategory facets only for content_rating='nsfw' and add a supporting (content_rating, nsfw_subcategory, id) index.
- Extend ImageRow, exhaustive verification, schema-3 migration/backfill, query validation, CLI/API serialization, and RESPONSE_SCHEMA_VERSION while retaining backward-compatible queries that omit the new filter.
- Rollback remains rebuild-based: stop the alternate listener, discard the disposable schema-4 gallery database/cache, and rebuild schema 3 with the prior committed code; canonical images, sidecars, ledgers, and the sibling schema-2 database are never rewritten.

## 10. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| The analyzer is heuristic-only and leaves unrelated inventory files unassigned. | high | medium | Use three sequential agents, exact owned-file lists, source-grounded specs, and full integration verification. Unassigned files are out of scope, not missing owners. |
| The current dirty rating and thumbnail hunks could be overwritten or only partially promoted. | high | high | Treat dirty content as the starting contract, preserve smallest diffs, add direct regressions, and review the final diff against the recovered snapshot. |
| Schema-4 migration or derived refresh could misclassify rows or disturb the wrong database. | medium | high | Use synthetic schema-3 fixtures, transactional migration/backfill, exhaustive verification, explicit database paths, and no live publication during implementation. |
| The unspecified subcategory could accidentally include non-NSFW content. | high | high | Require content_rating=nsfw whenever nsfw_subcategory is filtered or faceted and cover validation plus non-NSFW cases. |
| A WAL writer could commit between page count, item, tag, and suggestion reads. | medium | high | Use one explicit read transaction for the response and a writer-between-hydration concurrency regression. |
| UI hardening could regress keyboard behavior, URL presets, NSFW reveal, selection, or infinite scroll. | medium | medium | Preserve established contracts, add Back/Forward and opt-in-original assertions, and repeat keyboard, responsive, and zoom browser smoke. |
| Live verification could mutate active queue, provider, review, transfer, or listener state. | medium | high | Use synthetic fixtures first; require VERIFIED SND-HOST identity for an alternate listener; never submit mutation actions; keep cutover and provider/review canaries deferred. |

## 11. Verification Strategy

- python -m compileall -q src reports tests
- python -m pytest -q
- .venv\Scripts\python.exe -m compileall -q src reports tests
- .venv\Scripts\python.exe -m pytest -q tests/test_content_rating.py tests/test_index_library.py tests/test_library_browser.py tests/test_dashboard_server.py tests/test_gallery_browser_contract.py tests/test_gallery_thumbnails.py
- .venv\Scripts\python.exe -m pytest -q
- Run synthetic schema-3 to schema-4 migration, derived-verifier drift, and writer-between-hydration tests using temporary databases only.
- Run a disposable explicit-root HTTP listener and prove allowlisted gallery, facets, subcategory query, thumbnail, original, and sensitive-path denial contracts.
- After VERIFIED SND-HOST identity, run the established alternate-listener real-browser smoke at 320, 390, and 768 CSS-pixel widths plus 200-percent zoom; do not submit transfer or review actions.
- git diff --check
- Review the final diff against the pre-existing dirty hunks and confirm no canonical assets, sibling databases, runtime queues, provider ledgers, or Plan 002 artifacts were overwritten.

## 12. Documentation Updates

- Reconcile docs/CONTENT_RATING.md with the implemented schema-4 classifier, filter semantics, explicit-root runbook, and schema-2 sibling boundary.
- Update docs/INDEX_LIBRARY.md for schema-4 migration, derived subcategory field, facet/index, snapshot semantics, and verification.
- Update reports/README_live_dashboard.md for the additive response/filter contract and alternate-listener verification path.
- Update docs/GALLERY_ROADMAP.md with verified immediate upgrades and explicit follow-on campaigns for typed multi-tag identity, durable review ledgers, provider materialization, cursor and bounded-DOM pagination, and multi-provenance evidence.
- Update live-tracker.md with exact tests, browser/HTTP evidence, artifact boundaries, and remaining live promotion gates.
