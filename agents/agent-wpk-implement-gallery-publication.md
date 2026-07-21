# Agent Task — Implement Gallery Publication

**Scope:** Implement candidate preparation, manifest validation, settled writer and listener preflight, same-volume backup and atomic activation, automatic rollback, post-publication verification, and complete exception rollback using explicit paths and WhatIf-first orchestration.

**Depends on:** Agent WPJ

**Output files:** `src/dl_engine/gallery_publication.py`, `src/dl_engine/index_library.py`, `scripts/publish_gallery_index.py`, `scripts/Invoke-GalleryIndexPublication.ps1`

## Exit Criteria

- Candidate preparation writes only a unique explicit candidate/report/manifest path and never modifies the canonical gallery DB set.
- Manifest validation implements WPJ's schema/state contract and binds the actual candidate/report bytes, paths, SQLite versions, counts, WAL/SHM state, and library identity.
- Manifest validation reads the verification report's format version separately from SQLite `PRAGMA user_version` and `schema_metadata`; it never accepts report `schema_version` as proof that the candidate is schema 4.
- WhatIf is the default operational posture; live activation requires explicit apply and cutover authorization and fails closed on any missing identity/hold/writer/listener/stability/same-volume proof.
- Activation creates and hashes a recoverable same-volume backup, replaces the stopped canonical DB set without a partial-success state, verifies the reopened canonical path, and restores the prior snapshot on any failure.
- The implementation never terminates an unknown process, edits canonical media/sidecars/ledgers, touches `wallpaper_library.sqlite`, submits POSTs, or silently releases a queue hold.
- All index ingest exceptions roll back and close their connection; `KeyboardInterrupt` and `SystemExit` retain interruption semantics.

---

## Context — read before doing anything

1. `.continue/rules/project-conventions.md`.
2. `data/plans/plan-004.json` and both WPJ outputs; the manifest/document are the contract authority.
3. `src/dl_engine/index_library.py:968-1004,1271-1368,2293-2440,3091-3352,4628-4967` — migration, derived refresh, verifier, ingest, and CLI lifecycle.
4. `src/dl_engine/library_browser.py:428-500` — canonical verification report matching.
5. `docs/INDEX_LIBRARY.md:52-112,136-163,266-273` — explicit paths and current rollback limitations.
6. `reports/README_live_dashboard.md:118-164` — exact alternate-listener and cutover boundaries.
7. `tests/test_index_library.py:2151-2238` — migration rollback patterns; WPL owns new tests.

---

## Task

### Part 1 — Deterministic publication core

Create `src/dl_engine/gallery_publication.py`. Keep filesystem/process observations injectable so WPL can failure-test without live resources. Provide typed records and functions for:

- database-set fingerprinting (`main`, `-wal`, `-shm`) with path, existence, size, mtime and SHA-256 where required;
- read-only SQLite identity (`quick_check`, journal mode, user/metadata schema versions, table counts, required schema-4 columns);
- candidate validation, verification-report validation, and manifest read/write with atomic temp-and-replace publication;
- settled fingerprint comparison across a caller-supplied interval/sample set;
- same-volume validation and collision-proof backup paths;
- activation and rollback with explicit journaled steps so an exception can restore the last complete DB set;
- post-activation canonical-path verification before a published result can be returned.

Reject symlink/reparse escapes, canonical/candidate/backup aliasing, missing main DBs, orphan sidecars, nonzero WAL, schema drift, manifest drift, future manifest versions, and overwrites. Never delete a file until its replacement/backup identity is recorded. Catch `Exception` for rollback; do not swallow `BaseException`.

Treat any exhaustive verifier issue, including `layout-mismatch`, as a blocked candidate. The publisher may report the issue taxonomy but must not move or rename canonical media, downgrade the verifier, or promote a diagnostic browser result.

### Part 2 — CLI and SND-HOST wrapper

Create `scripts/publish_gallery_index.py` as the portable CLI over the core, with explicit subcommands such as `inspect`, `prepare`, `validate`, `publish`, and `rollback`. Require all runtime paths; never infer a live root. `prepare` uses the existing index ingest/verifier to build a unique schema-4 candidate from the canonical library and both ledgers. `publish` consumes a verified manifest; it does not rebuild during activation.

Create `scripts/Invoke-GalleryIndexPublication.ps1` with `SupportsShouldProcess` and strict error handling. It must:

- run `Get-VerifiedMachineIdentity.ps1` and require exact `snd-host`, `SND-HOST`, and `SND-HOST\Dev`;
- verify the project venv resolves `dl_engine` beneath this worktree;
- accept only explicit literal paths;
- gather queue-hold, scheduled-task, descendant/index-writer, listener, volume, and settled-fingerprint evidence;
- treat the pause/hold token as necessary but not sufficient;
- refuse apply while 8090 listens unless a separate explicit cutover flag is supplied and the listener is then proven stopped;
- never stop/kill/pause/resume anything implicitly;
- pass `-WhatIf` without creating candidate, manifest, backup, report, hold, or temp files.

The wrapper may prepare/validate a candidate while legacy 8090 runs because it targets a separate DB. It may not activate the canonical path until all cutover gates pass.

### Part 3 — Close the existing transaction gap

In `index_library.main`, restructure the ingest lifecycle so every ordinary exception rolls back, closes, logs a bounded error, and returns 2. Successful ingest still commits exactly once through the existing contract. `KeyboardInterrupt`/`SystemExit` must trigger cleanup and re-raise. Do not combine migration and a full 85k-row ingest into an unbounded live transaction; Plan 004 avoids that risk by publishing verified candidates.

---

## Constraints

- Modify only the four owned files; WPL owns all new tests and existing operator docs.
- Preserve all Plan 003 schema/query/UI behavior and existing dirty hunks.
- Standard library only; do not add dependencies.
- No live runtime mutation during implementation or self-test.
- Make operations idempotent or fail on explicit existing artifacts; never silently overwrite.

---

## Verification

```powershell
.\.venv\Scripts\python.exe -m compileall -q src reports tests scripts\publish_gallery_index.py
.\.venv\Scripts\python.exe -m pytest -q tests\test_index_library.py
.\.venv\Scripts\python.exe -m pytest -q
git diff --check -- src/dl_engine/gallery_publication.py src/dl_engine/index_library.py scripts/publish_gallery_index.py scripts/Invoke-GalleryIndexPublication.ps1
```

Run inspect/validate/WhatIf only against temporary paths and prove their trees are byte-for-byte unchanged.

---

## Do NOT

- Do not target `F:\Wallpapers\webgallery_library.sqlite` or `wallpaper_library.sqlite` during development tests.
- Do not stop 8090, create/remove a queue hold, terminate the orphan downloader, or touch scheduled tasks.
- Do not publish a candidate because quick-check alone passed; require the full manifest and verifier identity.
- Do not catch `BaseException`, ignore failed rollback, or leave an ambiguous partially-published result.

## Post-completion

Return the public function/CLI contract, exact files changed, tests run, failure-injection seams exposed to WPL, and any operational gate still requiring external authority. `live-tracker.md` is owned by WPL.
