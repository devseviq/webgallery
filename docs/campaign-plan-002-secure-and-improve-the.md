# Campaign — Secure and improve the wallpaper web gallery with cached thumbnails, faster metadata queries, discovery controls, provider enrichment, and reviewable visual-tag suggestions

**Plan ID:** plan-002
**Date:** 2026-07-19
**Status:** executed
**Plan file:** data/plans/plan-002.json
**Plan doc:** docs/campaign-plan-002-secure-and-improve-the.md
**Planner kind:** planner
**Source discovery docs:** docs/discovery-gallery-sorting-presentation.md, docs/discovery-picture-category-navigation.md, docs/discovery-sorting-tag-pipeline.md

---

## 1. Goal

Deliver a safer and substantially lighter full-library gallery over the canonical rebuildable SQLite index: expose only explicit dashboard, API, thumbnail, and original-media routes; replace card originals with cached thumbnails; remove avoidable query work; make typed metadata navigable; resume provider enrichment without skipped rows; and keep visual-model output separate, confidence-scored, provenance-bearing, and reviewable. Canonical images and sidecars remain unchanged, unknown content never becomes SFW by absence of evidence, and every live mutation remains gated by verified SND-HOST identity.

## 2. Exit Criteria

- Known direct paths for queue state, environment/config files, backup files, the SQLite database, and arbitrary workspace files return 404 or 403, while allowlisted dashboard, library API, thumbnail, canonical-original, rebuild, pause, and transfer contracts still work.
- Library cards use bounded SHA-keyed cached thumbnails with atomic generation, orientation-safe decoding, cache-hit behavior, and explicit cache headers; originals are loaded only by the lightbox or direct-original action, and a 48-card SFW smoke page no longer transfers the prior 218.5 MiB of originals.
- SQLite schema migration and rebuild materialize content-rating, rating-confidence, tag-count, and facet data; a normal page batch-hydrates tags without one query per image, common sorts have supporting indexes, and existing read-only/query/verification behavior remains compatible.
- The API provides counted prefix tag autocomplete, typed/provenanced tags, deterministic seeded shuffle and triage sorts, thumbnail/original URLs, and reviewable tag-suggestion records without merging pending machine suggestions into authoritative provider tags or safety ratings.
- The gallery provides clickable tag chips, active filters, counted autocomplete, a keyboard-accessible lightbox/detail view, density and contain/crop controls, named bookmarkable URL presets, and deterministic shuffle whose seed survives reload and pagination.
- Wallhaven enrichment selects every pending row without a lexicographic cursor skip, a bounded canary proves durable resume behavior, and provider enrichment records preserve raw source tags; Zerochan and Anime-Pictures enrichment gaps are represented by resumable provenance-bearing ledgers/import paths with measured coverage rather than fabricated tags.
- Visual tags are stored only as suggestions with label, confidence, generator/model version, provenance, review status, timestamps, and reviewer decision; pending or rejected suggestions never enter canonical tags, filters, content ratings, or provider evidence.
- Python compile, full pytest, focused gallery/index/server tests, JavaScript contract tests, allowlist HTTP probes, warm API timing, thumbnail-byte measurement, and git diff checks pass with zero unexpected failures.
- Active gallery, index, content-rating, server, and roadmap documentation records operation, migration, rollback, cache cleanup, provider coverage, suggestion provenance, and the exact verified-versus-working snapshot distinction.

## 3. Impact Assessment

| File | Current Lines | Change Type | Risk |
| --- | --- | --- | --- |
| reports/dashboard_server.py | 395 | modify | high - live HTTP routing, control endpoints, and transfer API |
| reports/_build_dashboard.py | 675 | modify | medium - snapshot media URL producer |
| reports/_render_dashboard.py | 1704 | modify | high - operations dashboard generator and client API contract |
| reports/watch_dashboard.ps1 | 116 | modify | medium - operational launcher and explicit runtime-root wiring |
| reports/README_live_dashboard.md | 178 | modify | low - live server operations guide |
| src/dl_engine/gallery_thumbnails.py | new | create | medium - bounded decoder and derived-cache writer |
| tests/test_gallery_thumbnails.py | new | create | low - isolated thumbnail regression suite |
| tests/test_dashboard_server.py | new | create | medium - live route and denial contract tests |
| pyproject.toml | 23 | modify | medium - Pillow runtime dependency declaration |
| requirements.txt | 1 | modify | medium - dependency parity with pyproject.toml |
| live-tracker.md | 18 | modify | low - campaign completion record |
| src/dl_engine/index_library.py | 3513 | modify | high - central SQLite schema, ingest, query, and enrichment module |
| src/dl_engine/library_browser.py | 260 | modify | high - public gallery API serialization and validation |
| tests/test_index_library.py | 2151 | modify | medium - central index regression suite |
| tests/test_library_browser.py | 223 | modify | medium - gallery API contract suite |
| docs/INDEX_LIBRARY.md | 262 | modify | low - index and enrichment operations guide |
| docs/CONTENT_RATING.md | 96 | modify | medium - safety-sensitive rating contract |
| reports/library-browser.html | 784 | modify | high - full-library UI, infinite scroll, and transfer integration |
| tests/test_gallery_browser_contract.py | new | create | low - static JavaScript and accessibility contract tests |
| docs/GALLERY_ROADMAP.md | new | create | low - durable six-point roadmap and coverage record |

## 4. Agent Roster

| Letter | Name | Scope | Deps | Files Owned | Group | Complexity |
| --- | --- | --- | --- | --- | --- | --- |
| wpd | secure-thumbnail-serving | Replace broad static serving with explicit allowlisted report and media routes, add SHA-keyed cached thumbnails, and preserve the operations dashboard through sanitized contracts. |  | reports/dashboard_server.py, reports/_build_dashboard.py, reports/_render_dashboard.py, reports/README_live_dashboard.md, reports/watch_dashboard.ps1, src/dl_engine/gallery_thumbnails.py, tests/test_gallery_thumbnails.py, tests/test_dashboard_server.py, pyproject.toml, requirements.txt, live-tracker.md | 0 | high |
| wpe | optimize-enrich-index | Materialize gallery ratings and facets, batch tag hydration, add counted tag discovery and deterministic sorts, repair provider enrichment progress, and store visual tags only as reviewable suggestions. | wpd | src/dl_engine/index_library.py, src/dl_engine/library_browser.py, tests/test_index_library.py, tests/test_library_browser.py, docs/INDEX_LIBRARY.md, docs/CONTENT_RATING.md | 1 | high |
| wpf | build-gallery-discovery-ui | Add clickable typed tags, counted autocomplete, lightbox and keyboard navigation, density and fit controls, named URL presets, deterministic shuffle controls, and provenance-aware suggestion review presentation. | wpe | reports/library-browser.html, tests/test_gallery_browser_contract.py, docs/GALLERY_ROADMAP.md | 2 | high |

## 5. Dependency Graph

```text
Group 0: wpd
Group 1: wpe
Group 2: wpf
```

## 6. File Ownership Map

| File | Owner |
| --- | --- |
| reports/dashboard_server.py | wpd |
| reports/_build_dashboard.py | wpd |
| reports/_render_dashboard.py | wpd |
| reports/README_live_dashboard.md | wpd |
| reports/watch_dashboard.ps1 | wpd |
| src/dl_engine/gallery_thumbnails.py | wpd |
| tests/test_gallery_thumbnails.py | wpd |
| tests/test_dashboard_server.py | wpd |
| pyproject.toml | wpd |
| requirements.txt | wpd |
| live-tracker.md | wpd |
| src/dl_engine/index_library.py | wpe |
| src/dl_engine/library_browser.py | wpe |
| tests/test_index_library.py | wpe |
| tests/test_library_browser.py | wpe |
| docs/INDEX_LIBRARY.md | wpe |
| docs/CONTENT_RATING.md | wpe |
| reports/library-browser.html | wpf |
| tests/test_gallery_browser_contract.py | wpf |
| docs/GALLERY_ROADMAP.md | wpf |

## 7. Conflict Zone Analysis

| Conflict Zone | Affected? | Mitigation |
| --- | --- | --- |
| Repository source versus explicit runtime collection roots | yes | All report source is in-repository, generated dashboards remain ignored runtime artifacts, and server/index commands require explicit collection, database, environment, queue, and cache paths rather than deriving live state from a worktree parent. |
| reports/dashboard_server.py versus reports/_build_dashboard.py and reports/_render_dashboard.py | yes | WPD owns all three source contracts plus the launcher; route changes, sanitized queue status, and media URL generation ship together while generated dashboard HTML remains runtime-only. |
| src/dl_engine/index_library.py versus src/dl_engine/library_browser.py | yes | WPE owns both the schema/query implementation and API serialization, with one focused regression boundary for schema version, pagination, autocomplete, and suggestion visibility. |
| src/dl_engine/content_rating.py versus materialized rating fields and machine suggestions | interface only | The classifier remains untouched; WPE materializes only its existing output from provider and accepted human evidence, never from pending or rejected model suggestions. |
| live-tracker.md sequential campaign updates | yes | WPD is the registered owner. WPE and WPF report exact completion rows in their result payloads; WPF performs the final consolidated tracker update only after WPD and WPE results are preserved. |
| pre-existing heavily dirty worktree | yes | Every product file has one registered owner, agents must inspect the pre-task file state and smallest owned diff, and no cleanup, reset, or unrelated formatting is allowed. |

## 8. Integration Points

- WPD defines the allowlisted /api/library, /api/library/tags, /api/library/suggestions, /thumb/<sha256>, and /original/<image-id> route boundary; WPE supplies the validated library_browser functions and serialized URL fields behind that boundary.
- WPD defines the SHA-keyed thumbnail cache contract and cache-root policy; WPE emits thumbnail_url and original_url without exposing filesystem paths, and WPF uses thumbnails for cards while reserving originals for explicit detail actions.
- WPE migrates the disposable index to schema version 3, materializes rating/tag/facet values, and defines counted autocomplete, deterministic sort, provider coverage, and suggestion-review payloads; WPF consumes only those documented fields.
- WPE preserves provider and sidecar tags as evidence and exposes machine suggestions separately; WPF renders provider tags as navigation and suggestions as confidence/provenance-labelled review items.
- WPF consolidates completion evidence from WPD and WPE into docs/GALLERY_ROADMAP.md and the final live-tracker.md entry after full integration verification.

## 9. Schema Changes

- Increment disposable SQLite DB_SCHEMA_VERSION from 2 to 3; do not change wallpaper sidecar schema version 1.
- Add images.content_rating TEXT NOT NULL DEFAULT 'unknown', images.rating_confidence REAL NOT NULL DEFAULT 0, images.rating_basis TEXT NOT NULL DEFAULT 'no-signal', images.rating_reasons_json TEXT NOT NULL DEFAULT '[]', and images.tag_count INTEGER NOT NULL DEFAULT 0. Recompute them transactionally after ingest and provider-tag changes.
- Add library_facets(facet TEXT, value TEXT, count INTEGER, refreshed_at TEXT, PRIMARY KEY(facet,value)); refresh it in the same transaction as derived rating/tag-count publication so facet reads are constant-shape and snapshot-consistent.
- Add tag_suggestions(id INTEGER PRIMARY KEY, image_id INTEGER REFERENCES images(id) ON DELETE CASCADE, label TEXT, normalized_label TEXT, confidence REAL, generator TEXT, model_version TEXT, provenance TEXT, review_status TEXT, created_at TEXT, reviewed_at TEXT, reviewer TEXT, decision_note TEXT) with constraints for confidence 0 through 1 and review_status pending, accepted, or rejected plus a deterministic uniqueness key.
- Add indexes for materialized content_rating, tag_count, download_recorded_at, size_bytes, franchise, and the stable sort keys; retain existing source, orientation, bucket, site ID, and image-tag indexes.
- Define a durable provider-enrichment ledger record version for source, source_id, status, typed raw tags, provenance, captured_at, and error. SQLite may import the ledger but never replaces it as provider evidence.

## 10. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| The analyzer is heuristic-only and may not fully model report/browser contracts or assign all repository files. | certain | medium | Use only three sequential agents, exact file lists, source-grounded contracts, explicit runtime-root handling, and final full-suite plus live-route verification. |
| A Git worktree could accidentally infer its parent as the live collection root. | high | high | Keep report source inside the repository, keep generated output ignored, and require explicit library, database, environment, queue, and cache paths for every live command. |
| An allowlist breaks the current operations dashboard because it fetches queue state and embeds relative temp/library paths. | high | high | WPD owns server and generator changes together, adds a sanitized queue-status endpoint and explicit media routes, regenerates the dashboard, and tests every retained control. |
| Thumbnail decoding consumes excessive CPU or memory, follows a path outside the library, or serves stale content. | medium | high | Resolve image IDs through SQLite, require canonical-root containment, cap source dimensions and output pixels, verify format, generate atomically, key by SHA-256 plus transform version, and bound concurrent generation. |
| Schema migration or derived-field refresh mutates the live unverified index incorrectly. | medium | high | Test migration on copies, gate live writes on VERIFIED SND-HOST identity, preserve sidecars and provider ledgers, use one transaction, run verify-json afterward, and retain rebuild-from-evidence rollback. |
| Materialized ratings or facets drift after provider enrichment or suggestion review. | medium | high | Centralize derived refresh, call it from ingest and every authoritative tag mutation, exclude pending/rejected suggestions by construction, and add rebuild-versus-incremental parity tests. |
| Wallhaven resume still skips out-of-order IDs or provider throttling makes completion look successful. | medium | high | Select from pending status without a greater-than cursor predicate, ledger every attempt before advancing observability progress, use bounded canaries, retain rate limits, and report remaining rows explicitly. |
| Sparse Zerochan metadata encourages invented or low-confidence tags. | high | medium | Accept only captured provider evidence into the provider ledger, show coverage by source, retain one-tag rows as sparse, and route any visual inference to tag_suggestions instead. |
| The single-file gallery UI regresses infinite scroll, NSFW blur, selection, or transfer behavior. | medium | medium | Preserve existing URL state and transfer contracts, add static JavaScript/accessibility tests, and perform keyboard, pagination, filter, selection, and lightbox smoke checks. |
| Agents overwrite unrelated changes in the heavily dirty worktree. | high | medium | Prohibit broad formatting and cleanup, require pre-edit status and smallest owned diff review, and merge only in WPD then WPE then WPF order. |

## 11. Verification Strategy

- python -m compileall -q src tests
- python -m pytest -q
- python -m pytest -q tests/test_dashboard_server.py tests/test_gallery_thumbnails.py tests/test_index_library.py tests/test_library_browser.py tests/test_gallery_browser_contract.py
- Run loopback HEAD/GET probes proving allowlisted routes succeed and /.wallpaper-download-queue/state.json, /.env, /anime_pictures_config.json, and /wallpaper_library.sqlite are denied without reading sensitive bodies.
- Measure one warm 48-item SFW API response and its thumbnail response bytes on verified SND-HOST; record comparison with the 2026-07-20 baseline.
- git diff --check

## 12. Documentation Updates

- Update reports/README_live_dashboard.md for the allowlisted HTTP surface, sanitized live-state route, thumbnail/original routes, cache lifecycle, and rollback.
- Update docs/INDEX_LIBRARY.md and docs/CONTENT_RATING.md for schema migration, materialized gallery fields/facets, provider enrichment ledgers, and the invariant that unknown is not SFW.
- Create docs/GALLERY_ROADMAP.md as the durable six-point roadmap, including implementation order, live verification gates, provider coverage measures, and the review-only visual suggestion boundary.


## R2. Behavioral Invariants

- Canonical image and .wallpaper.json sidecar files remain paired and are never moved, renamed, deleted, rewritten, or physically regrouped by the gallery campaign.
- SQLite remains a rebuildable index; raw provider tags and durable provider ledgers remain evidence, while aliases and machine suggestions are additive layers.
- Unknown content is never promoted to SFW because tags or purity evidence are absent.
- Only explicitly allowlisted loopback HTTP routes may read report assets, sanitized queue status, thumbnails, or canonical originals; arbitrary workspace paths remain unreachable.
- Pending and rejected visual suggestions never become authoritative tags or content-rating evidence.
- Any live database, cache, process, or provider-ledger mutation runs only after Get-VerifiedMachineIdentity.ps1 returns VERIFIED for SND-HOST.

## R3. Rollback Strategy

Stop the replacement server, restore the prior dashboard_server.py and generated report HTML from the pre-campaign diff, and remove only the dedicated derived thumbnail cache. If schema version 3 or enrichment behavior fails, retain canonical images, sidecars, and provider ledgers, discard the rebuildable SQLite file, restore the previous code, and rebuild schema version 2. Disable suggestion review writes without deleting suggestion records so provenance and decisions remain auditable.
