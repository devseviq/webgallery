# Campaign — Safe Schema-4 Gallery Publication

**Plan ID:** plan-004
**Date:** 2026-07-21
**Status:** executed
**Plan file:** data/plans/plan-004.json
**Plan doc:** docs/campaign-plan-004-package-a-fail-closed.md
**Planner kind:** planner-refactor
**Source roadmap:** docs/GALLERY_ROADMAP.md
**Source discovery docs:** docs/discovery-picture-category-navigation.md

---

## 1. Goal

Provide a tested, operator-safe schema-4 gallery-index packaging and publication path so SND-HOST can build and prove a candidate while downloads continue, then activate it only inside an explicit maintenance and cutover boundary without risking the last verified gallery snapshot, canonical media, durable evidence, or the sibling maintenance index.

## 2. Exit Criteria

- A versioned JSON manifest requires an existing schema-4 candidate main database, exact size and SHA-256, verified library/database identity, zero-or-absent WAL, and rejects orphan WAL/SHM-only artifacts.
- The manifest and tests distinguish publication-manifest schema, exhaustive-verification report schema, and SQLite user/metadata schema; a report's top-level `schema_version` is never treated as the database schema.
- WhatIf and candidate-only modes do not modify the canonical gallery database, its WAL/SHM set, queue state, listener state, canonical media, sidecars, ledgers, or sibling schema-2 database.
- Apply fails closed unless exact VERIFIED snd-host identity, an authoritative queue hold, zero downloader descendants and index writers, a settled DB/WAL/SHM interval, verified candidate identity, same-volume activation, and an explicitly stopped 8090 cutover listener are all proven.
- Activation retains a hashed rollback artifact, uses atomic same-volume replacement, verifies the canonical path after reopen, restores the prior snapshot on injected activation or verification failure, and releases no hold before success.
- Every ingest or publication exception rolls back and closes SQLite connections without partially publishing schema, derived data, reports, manifests, or live files.
- Failure-injection tests cover candidate build failure, manifest drift, open handles, nonzero WAL, orphan sidecars, writer/listener activity, replacement failure, post-publish verification failure, rollback restoration, and unchanged-live guarantees.
- The schema-4 candidate passes exhaustive verification and the complete alternate-8091 HTTP/browser matrix while the canonical schema-3 database and legacy 8090 tuple remain unchanged; live cutover still requires its explicit apply flag.
- Compile, full pytest, targeted publication tests, diff-check, plan validation, and documentation drift checks pass with zero failures.

## 3. Impact Assessment

| File | Current Lines | Change Type | Risk |
| --- | --- | --- | --- |
| schemas/gallery-publication-manifest.schema.json | new | create | medium |
| docs/GALLERY_INDEX_PUBLICATION.md | new | create | medium |
| src/dl_engine/gallery_publication.py | new | create | high |
| src/dl_engine/index_library.py | 4939 | modify | high |
| scripts/publish_gallery_index.py | new | create | high |
| scripts/Invoke-GalleryIndexPublication.ps1 | new | create | high |
| tests/test_gallery_publication.py | new | create | high |
| tests/test_gallery_publication_contract.py | new | create | high |
| tests/test_index_library.py |  | modify | high |
| docs/INDEX_LIBRARY.md |  | modify | high |
| reports/README_live_dashboard.md |  | modify | high |
| docs/GALLERY_ROADMAP.md |  | modify | high |
| live-tracker.md |  | modify | high |

## 4. Agent Roster

| Letter | Name | Scope | Deps | Files Owned | Group | Complexity |
| --- | --- | --- | --- | --- | --- | --- |
| wpj | define-publication-contract | Define the fail-closed publication state machine, manifest schema, queue-hold proof, candidate identity, activation, verification, and rollback contracts without changing runtime behavior. |  | schemas/gallery-publication-manifest.schema.json, docs/GALLERY_INDEX_PUBLICATION.md | 0 | medium |
| wpk | implement-gallery-publication | Implement candidate preparation, manifest validation, settled writer and listener preflight, same-volume backup and atomic activation, automatic rollback, post-publication verification, and complete exception rollback using explicit paths and WhatIf-first orchestration. | wpj | src/dl_engine/gallery_publication.py, src/dl_engine/index_library.py, scripts/publish_gallery_index.py, scripts/Invoke-GalleryIndexPublication.ps1 | 1 | high |
| wpl | verify-publication-workflow | Add failure-injection and Windows contract tests, reconcile schema-4 operator documentation and roadmap state, and execute repository, candidate, alternate-listener, rollback, and unchanged-live verification without performing an unauthorized cutover. | wpk | tests/test_gallery_publication.py, tests/test_gallery_publication_contract.py, tests/test_index_library.py, docs/INDEX_LIBRARY.md, reports/README_live_dashboard.md, docs/GALLERY_ROADMAP.md, live-tracker.md | 2 | high |

## 5. Dependency Graph

```text
Group 0: wpj
Group 1: wpk
Group 2: wpl
```

## 6. File Ownership Map

| File | Owner |
| --- | --- |
| schemas/gallery-publication-manifest.schema.json | wpj |
| docs/GALLERY_INDEX_PUBLICATION.md | wpj |
| src/dl_engine/gallery_publication.py | wpk |
| src/dl_engine/index_library.py | wpk |
| scripts/publish_gallery_index.py | wpk |
| scripts/Invoke-GalleryIndexPublication.ps1 | wpk |
| tests/test_gallery_publication.py | wpl |
| tests/test_gallery_publication_contract.py | wpl |
| tests/test_index_library.py | wpl |
| docs/INDEX_LIBRARY.md | wpl |
| reports/README_live_dashboard.md | wpl |
| docs/GALLERY_ROADMAP.md | wpl |
| live-tracker.md | wpl |

## 7. Conflict Zone Analysis

| Conflict Zone | Affected? | Mitigation |
| --- | --- | --- |
| {'files': ['docs/INDEX_LIBRARY.md', 'src/dl_engine/index_library.py'], 'reason': 'Schema and query documentation', 'mitigation': 'WPK owns the implementation; dependent WPL updates the operator contract only after WPK completes.'} |  |  |

## 8. Integration Points

- WPJ freezes the manifest and state-machine contract before WPK implements it.
- WPK exposes deterministic, side-effect-injected publication primitives before WPL writes failure-injection tests and operator documentation.
- WPL verifies a separate schema-4 candidate and alternate 8091 listener while preserving the canonical schema-3 DB and legacy 8090 until explicit cutover authority exists.

## 9. Schema Changes

- No schema changes required.

## 10. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| An in-place migration or partial replacement could strand legacy 8090 or lose the last verified schema-3 snapshot. | high | high | Build and verify a separate candidate; require an explicit cutover flag, stopped listener, hashed backup, same-volume activation, and automatic rollback. |
| The queue can mark a job failed while downloader descendants remain alive, so queue status alone is not a writer gate. | high | high | Require both an authoritative hold and zero descendant/index-writer proof over a settled interval; never terminate unknown processes automatically. |
| WAL/SHM sidecars or open Windows handles can make a main-file-only replacement inconsistent or impossible. | medium | high | Reject nonzero WAL and orphan sidecars, close/checkpoint candidates, require stopped consumers, and cover open-handle and rollback paths with same-volume tests. |
| Path/mtime-only verification evidence can become stale after publication. | medium | medium | Bind manifests and post-publish reports to exact DB path, schema, byte length, SHA-256, library root, and generated timestamp. |
| The exhaustive verifier can reject an otherwise healthy schema-4 database when canonical media layout has drifted. | medium | high | Treat every layout mismatch as a publication blocker; leave media moves to the existing maintenance authority at a verified zero-child boundary, then rebuild and reverify a fresh candidate. |
| The verification report's `schema_version` describes the report format, not the SQLite database schema, and can be misread during manifest validation. | medium | high | Namespace all version fields and validate `PRAGMA user_version` plus `schema_metadata` independently from the report-format version. |
| The analyzer is heuristic-only and reports unrelated files as unassigned. | low | medium | Use three sequential agents, explicit exclusive file lists, dirty-worktree preflight, and final diff review. |

## 11. Verification Strategy

- python -m compileall -q src reports tests
- python -m pytest -q
- .venv\Scripts\python.exe -m compileall -q src reports tests
- .venv\Scripts\python.exe -m pytest -q
- .venv\Scripts\python.exe -m pytest -q tests/test_gallery_publication.py tests/test_gallery_publication_contract.py tests/test_index_library.py
- .venv\Scripts\python.exe scripts\task_manager.py plan validate plan-004 --json
- git diff --check
- Run WhatIf and failure-injection publication scenarios against temporary same-volume databases; compare live DB/WAL/SHM hashes and 8090 listener tuple before and after.
- Validate publication-manifest, verification-report, `PRAGMA user_version`, and `schema_metadata` versions as distinct fields.
- Build and exhaustively verify a schema-4 candidate, then execute the Plan 003 alternate-8091 HTTP/browser, viewport, zoom, keyboard, history, original-load, and cleanup matrix without POSTs or 8090 cutover.
- If exhaustive verification fails, record the exact issue taxonomy and allow only explicitly labelled diagnostics; no partial browser pass may satisfy promotion or authorize media repair.

## 12. Documentation Updates

- Add docs/GALLERY_INDEX_PUBLICATION.md and the manifest schema as the publication authority, including state transitions, queue hold ownership, WhatIf, candidate preparation, activation, rollback, and recovery.
- Reconcile docs/INDEX_LIBRARY.md, reports/README_live_dashboard.md, docs/GALLERY_ROADMAP.md, and live-tracker.md so schema-4 candidate, verified, published, cutover, and rollback states are explicit and stale schema-3 wording is removed.


## R1. Roadmap Phase

Phase: 6 Packaging
Roadmap reference: docs/GALLERY_ROADMAP.md

## R2. Behavioral Invariants

- Canonical images, sidecars, provider ledgers, queue records, and the sibling schema-2 wallpaper_library.sqlite are never rewritten by gallery publication.
- Candidate preparation or verification failure leaves the canonical schema-3 gallery database and legacy 8090 listener unchanged.
- Publication fails closed unless SND-HOST identity, queue hold, zero descendants/index writers, stable DB-WAL-SHM fingerprints, verified schema-4 candidate identity, and stopped cutover listener are proven.

## R3. Rollback Strategy

Retain a hashed same-volume backup of the canonical DB set, restore it automatically on activation or post-publish verification failure, and release any queue hold only after the canonical read-only contract passes.
