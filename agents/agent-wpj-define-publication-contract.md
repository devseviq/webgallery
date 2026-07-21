# Agent Task — Define Publication Contract

**Scope:** Define the fail-closed publication state machine, manifest schema, queue-hold proof, candidate identity, activation, verification, and rollback contracts without changing runtime behavior.

**Depends on:** none

**Output files:** `schemas/gallery-publication-manifest.schema.json`, `docs/GALLERY_INDEX_PUBLICATION.md`

## Exit Criteria

- A JSON Schema Draft 2020-12 manifest contract rejects a missing candidate main DB, schema other than 4, mismatched `PRAGMA user_version` and metadata markers, absent verification identity, nonzero/uncheckpointed WAL, orphan sidecars, missing hashes/sizes, and unknown fields.
- Manifest fields explicitly separate manifest-format version, verification-report-format version, `PRAGMA user_version`, and `schema_metadata.schema_version`; the report's top-level `schema_version` is never used as SQLite schema evidence.
- The document defines candidate-built, candidate-verified, ready-to-publish, published, and rolled-back states plus the only valid transitions.
- Ready-to-publish requires exact VERIFIED `snd-host` identity, an externally owned queue hold, zero downloader descendants/index writers over a settled interval, current library/ledger fingerprints, a fresh exhaustive under-hold candidate verification, a free smoke port, an explicitly stopped 8090 cutover listener, same-volume paths, and a verified schema-4 candidate.
- WhatIf, apply, backup, recoverable journaled activation, one atomic main-database replacement, post-publish verification, interrupt-safe rollback, external listener recovery, and queue-hold ownership semantics are unambiguous and testable.
- Task-manager `executed` is explicitly distinguished from implemented, repository-verified, candidate-blocked, ready-to-publish, published, cut-over, and rolled-back operational states.
- Canonical media, sidecars, ledgers, queue records, the sibling schema-2 database, and legacy 8090 stay outside candidate preparation.

---

## Context — read before doing anything

1. `.continue/rules/project-conventions.md` — explicit runtime roots, SQLite disposability, and identity gates.
2. `data/plans/plan-004.json` — authoritative goal, invariants, exit criteria, risks, and rollback strategy.
3. `docs/discovery-picture-category-navigation.md:289-319` — zero-child candidate/verify/publish design.
4. `docs/discovery-gallery-publication-audit.md` — current candidate evidence and the freshness, interrupt, journal, hold, listener, and path-pin gaps this contract must close.
5. `docs/INDEX_LIBRARY.md:25-112,136-163,266-273` — database ownership, rebuild, verification identity, WAL, exact SND-HOST roots, and current rollback contract.
6. `src/dl_engine/index_library.py:105,968-1004,2293-2440,3091-3352,4628-4967` — schema, migration, verification, ingest, and CLI transaction boundaries.
7. `src/dl_engine/library_browser.py:428-500` — verification freshness and path matching consumed by status.
8. Existing JSON-schema style in `schemas/wallpaper-metadata.schema.json`.

---

## Task

### Part 1 — Freeze the manifest

Create `schemas/gallery-publication-manifest.schema.json` with `additionalProperties: false` at every owned object boundary. Require:

- `manifest_schema_version: 1`, state, timestamps, and exact machine identity fields;
- explicit canonical DB, candidate DB, library root, verification report, backup, queue-state, and pause/hold paths;
- candidate byte length, SHA-256, SQLite schema/user/metadata versions, journal mode, quick-check result, table counts, WAL/SHM presence and size, and closed/checkpointed evidence;
- verification exit code, `ok`, issue count and taxonomy, report-format version, report SHA-256, database path, library root, and generated timestamp;
- current canonical-library inventory, sidecar, Wallhaven-ledger, provider-ledger, and candidate-generation fingerprints so freshness can be reproved under the hold;
- queue-hold owner/token/acquisition evidence, scheduled-task observation, descendant/index-writer samples, listener samples, and a settled DB/WAL/SHM fingerprint window;
- exact pre-cutover 8090 ownership/launch tuple plus its external recovery owner;
- apply/cutover authorization, backup identity, durable journal identity, activation identity, post-publish report identity, rollback identity, `release_eligible`, `listener_restore_required`, and terminal result.

Hashes are 64 uppercase/lowercase hexadecimal characters; timestamps are UTC date-times; sizes and counts are nonnegative integers. A sidecar entry can be absent or a fully identified file, never an unnamed boolean.

### Part 2 — Define the state machine and gates

In `docs/GALLERY_INDEX_PUBLICATION.md`, define:

1. candidate preparation from explicit durable inputs without touching the canonical DB;
2. exhaustive candidate verification and manifest binding;
3. maintenance proof: exact identity, an externally owned authoritative pause/hold, zero live descendants/indexers over a measured interval, stable file fingerprints, current durable-input fingerprints, and listener ownership;
4. a fresh exhaustive candidate verification performed after that hold/window and bound to the current library/ledger evidence;
5. ready-to-publish only after a separate explicit cutover decision stops 8090 and captures the exact external restart owner/runbook;
6. same-volume hashed backup plus durable journal, fail-closed WAL/SHM/handle checks, and one atomic main-database replacement as a recoverable file-set operation rather than a multi-file atomic claim;
7. canonical-path reopen and exhaustive verification before success or `release_eligible=true`;
8. automatic restoration on any activation or post-publish failure, including interrupt cleanup followed by re-raise.

Queue status alone is not proof: the current audit found a failed job with living descendants. The tool must never kill unknown processes automatically. The publisher never releases/resumes an externally acquired hold and never stops/starts 8090; it only reports release eligibility and listener recovery requirements to their owners. State clearly that candidate/browser verification does not authorize live cutover, provider/review canaries, transfers, or queue mutation.

An exhaustive `layout-mismatch` is also a hard publication blocker. The publication workflow must never move canonical media to repair it; only the existing maintenance authority may reconcile layout at a separately verified zero-child boundary, followed by a fresh candidate build and verification.

### Part 3 — Operator examples

Add copy-pasteable examples for inspect/WhatIf, prepare candidate, validate manifest, publish with explicit apply/cutover flags, and rollback. Every example uses the project venv and explicit `F:\Wallpapers` roots. Do not include secrets or infer paths from the worktree parent.

The SND-HOST wrapper examples pin the resolved Apply roots to
`F:\Wallpapers\webgallery_library.sqlite`, `F:\Wallpapers\library`, both
documented `_metadata` ledgers, and a dedicated same-volume backup/recovery
root. Arbitrary roots remain available only to the portable core and tests.

---

## Constraints

- Modify only the two owned files.
- This is a contract task: do not add executable code or mutate any runtime resource.
- Preserve schema 4 and response schema 3; Plan 004 introduces no database schema change.
- Treat the generated analyzer warning as degraded context, not a reason to widen file ownership.

---

## Verification

```powershell
.\.venv\Scripts\python.exe -m json.tool schemas\gallery-publication-manifest.schema.json > $null
.\.venv\Scripts\python.exe -m compileall -q src reports tests
.\.venv\Scripts\python.exe -m pytest -q
git diff --check -- schemas/gallery-publication-manifest.schema.json docs/GALLERY_INDEX_PUBLICATION.md
```

---

## Do NOT

- Do not open or copy a live SQLite database.
- Do not create/delete a pause flag, stop a process/task/listener, or claim a maintenance window.
- Do not make a main-file-only publication safe by ignoring WAL/SHM.
- Do not claim the database set is atomically replaced, release an external hold, or imply the publisher owns 8090 restart continuity.
- Do not leave TODOs, placeholders, optional core identity fields, or ambiguous transition language.

## Post-completion

Report the exact manifest version, states, required preflight evidence, verification results, and any unresolved contract question. `live-tracker.md` is owned by WPL; do not edit it.
