# Agent Task — Verify Publication Workflow

**Scope:** Add failure-injection and Windows contract tests, reconcile schema-4 operator documentation and roadmap state, and execute repository, candidate, alternate-listener, rollback, and unchanged-live verification without performing an unauthorized cutover.

**Depends on:** Agent WPK

**Output files:** `tests/test_gallery_publication.py`, `tests/test_gallery_publication_contract.py`, `tests/test_index_library.py`, `docs/INDEX_LIBRARY.md`, `reports/README_live_dashboard.md`, `docs/GALLERY_ROADMAP.md`, `live-tracker.md`

## Exit Criteria

- Deterministic tests cover candidate/manifest success and every Plan 004 failure class, including unchanged-live and exact rollback restoration.
- Static Windows-wrapper tests prove exact identity, literal paths, WhatIf no-write behavior, queue-hold plus descendant proof, explicit cutover, no implicit process/task mutation, and sibling DB protection.
- Tests prove manifest-format, verification-report-format, `PRAGMA user_version`, and `schema_metadata` versions are separate contracts and cannot be substituted for one another.
- Tests prove a candidate verified before the hold is rejected when current library, sidecar, or either ledger fingerprint changes, and that Apply requires a fresh exhaustive under-hold report.
- Tests prove post-mutation `KeyboardInterrupt`/`SystemExit` attempts exact restoration before re-raise, external holds are never released/resumed, and failed cutover reports listener recovery without starting 8090.
- Index tests prove ordinary ingest failure rolls back/closes and interruption cleanup re-raises.
- Documentation distinguishes candidate-built, verified, ready, published, cut over, rolled back, and blocked states and removes stale schema-3 publication wording.
- A real separate schema-4 candidate is exhaustively verified and tested on alternate 8091 only if identity and candidate-only safety gates pass; canonical DB/8090 hashes and listener tuple remain unchanged.
- No live cutover, queue/task/process mutation, provider/review canary, transfer, or canonical-media mutation occurs.

---

## Context — read before doing anything

1. `.continue/rules/project-conventions.md`.
2. `data/plans/plan-004.json`, `docs/discovery-gallery-publication-audit.md`, both WPJ outputs, and all WPK outputs.
3. `agents/agent-wpi-verify-gallery-upgrades.md:66-109` — full HTTP/browser/viewport/zoom/cleanup gate.
4. `tests/test_index_library.py`, `tests/test_dashboard_server.py`, `tests/test_gallery_browser_contract.py`, and existing fixture conventions.
5. `docs/INDEX_LIBRARY.md`, `reports/README_live_dashboard.md`, `docs/GALLERY_ROADMAP.md`, and `live-tracker.md` — update only claims supported by current evidence.
6. Current live baseline from the Plan 004 audit: canonical DB schema 3, SHA-256 `1AE393A92BE5DA25168D2F57C292B89B04EFD34B37B09B6BC34C4681AF2F6402`; legacy 8090 PID 39260; 8091 free; orphan downloader descendants present. Re-inventory because this is time-sensitive.
7. Existing diagnostic evidence, not a verified/publishable input: `F:\Wallpapers\webgallery_library.schema4.20260721T003322Z.candidate.sqlite` (SHA-256 `F138D243A8A7DD2BE4164CAB3461D27517699C82EDEEA6829A429A7DA211B5D3`) and `F:\Wallpapers\reports\maintenance-webgallery-candidate-20260721T003322Z\verify.json` (SHA-256 `0E02210EB48A4B406286FE90BA9499AC677F95A68C3BE27948F0EC1BA477E896`). The DB is SQLite schema 4 and quick-check clean, but exhaustive verification failed closed with exactly 200 `layout-mismatch` issues; the report also lacks the required generated timestamp. Its limited 8091 browser diagnostics passed observed HTTP, privacy, history, 320/390/768 responsive, opt-in-original, dialog, focus-return, and console checks while status correctly remained `verified=false`; zoom, reduced-motion, stale-load, and the rest of the promotion matrix were not run. Re-inventory and build a fresh candidate after implementation; never reinterpret this artifact as verified.

---

## Task

### Part 1 — Publication-core regression suite

Create `tests/test_gallery_publication.py` with temporary same-volume directories and synthetic SQLite databases. Cover:

- valid schema-4 candidate and manifest binding;
- distinct manifest/report/SQLite schema versions, including a report-format `schema_version=1` paired with a valid SQLite schema-4 candidate and negative substitution cases;
- missing report timestamp, missing main DB with WAL/SHM remnants, future/invalid manifest, wrong schema/metadata, hash/size/report/library mismatch, nonzero WAL, orphan sidecars, alias/reparse/path escape, different volume, and unsettled fingerprints;
- stale pre-hold verification plus changed library inventory, sidecar, Wallhaven-ledger, and provider-ledger fingerprints;
- WhatIf/no-apply byte-for-byte unchanged trees and no temp artifacts;
- active listener, absent/weak hold, living descendants/indexer, and changed fingerprint refusal;
- backup collision, open handle/replacement error, mid-activation error, `KeyboardInterrupt`, `SystemExit`, post-publish verification error, and rollback error reporting;
- exact prior main/WAL/SHM restoration and hash equality after each recoverable injected failure;
- no external hold release/resume and no 8090 stop/start; return values identify release eligibility and listener restoration responsibility;
- atomic manifest/report writes and no published state before canonical verification succeeds.

Use injected clocks, process/listener samples, file operations, and verifier callbacks. No test may touch live paths or the network.

### Part 2 — Wrapper and index contracts

Create `tests/test_gallery_publication_contract.py` to statically parse the PowerShell/Python entry points and, where safe, invoke help/WhatIf against temporary roots. Assert exact identity verifier use, `SupportsShouldProcess`, explicit literal path parameters, a resolved SND-HOST Apply allowlist for the canonical DB/library/both ledgers/recovery root, cutover flag, scheduled-task/descendant/listener launch-tuple checks, no `Stop-Process`/task disable/queue-resume/listener-restart automation, and hard protection for `wallpaper_library.sqlite`.

Extend `tests/test_index_library.py` with injected ingest failures after migration/open. Assert rollback, close, exit 2, no partial derived publication, and re-raised `KeyboardInterrupt`/`SystemExit` after cleanup.

### Part 3 — Documentation and status truth

Update the four owned documentation/tracker files to point at `docs/GALLERY_INDEX_PUBLICATION.md`, the manifest, WhatIf/prepare/validate/publish/rollback commands, backup identity, and report locations. Reconcile `GALLERY_ROADMAP.md` schema-3 wording. Use exact planned/implemented/repository-verified/candidate-blocked/ready-to-publish/published/cut-over/rolled-back vocabulary; task-manager `executed` alone proves none of those operational states. Record Plan 004 as implemented only if code/tests pass; record live candidate/browser/publish/cutover separately. Never label canonical publication or 8090 cutover complete without direct evidence.

### Part 4 — Candidate and browser evidence

After a fresh exact identity check and read-only runtime inventory:

1. capture canonical DB/WAL/SHM hashes and 8090 tuple;
2. run WhatIf and require no filesystem/process/listener change;
3. prepare a unique `F:\Wallpapers\webgallery_library.schema4.<UTC>.candidate.sqlite` plus manifest/report without touching canonical paths;
4. require exhaustive verifier exit 0, `ok=true`, zero issues, exact candidate/library identity, schema 4, and checkpointed/zero WAL;
5. create unique QA-owned cache, report, environment, and queue roots, then start only the explicit-root 8091 foreground listener against the candidate;
6. execute the WPI HTTP, browser interaction, 320/390/768 widths, narrow landscape, 200% zoom, reduced-motion, history, opt-in original, stale-load, keyboard, dialog, selection, and cleanup matrix with zero POSTs;
7. stop only the owned 8091 listener, prove canonical/8090 baselines unchanged, and report the exact cleanup or retained status of every QA-owned root.

If layout drift, the orphan downloader, or the queue prevents candidate verification, or browser control is unavailable, record a blocker and retain repository test evidence; do not weaken the gate, move canonical media, or perform cutover. A blocked candidate may receive a deliberately bounded diagnostic UI pass only when isolated on 8091 and labelled non-promotional; it cannot satisfy any browser exit criterion until exhaustive verification passes.

---

## Constraints

- Modify only the seven owned files.
- Preserve existing Plan 003 dirty work and recovery stash.
- Runtime checks are candidate-only and read-only toward canonical DB/media/queue/ledgers/8090.
- Do not delete orphan pending sidecars or any candidate unless its exact ownership and recovery status are proven.

---

## Verification

```powershell
.\.venv\Scripts\python.exe -m compileall -q src reports tests scripts\publish_gallery_index.py
.\.venv\Scripts\python.exe -m pytest -q tests\test_gallery_publication.py tests\test_gallery_publication_contract.py tests\test_index_library.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\task_manager.py plan validate plan-004 --json
git diff --check
```

Also run the documented WhatIf/failure-injection checks and, when safe, exhaustive candidate verification plus the full alternate-8091 browser matrix.

---

## Do NOT

- Do not stop/replace 8090, publish over the canonical DB, or release/acquire a live queue hold.
- Do not restart 8090 or imply the publisher owns listener recovery after an operator-controlled cutover.
- Do not kill the orphan downloader, alter scheduled tasks, send a transfer, or submit suggestion review/provider requests.
- Do not claim browser coverage from static/synthetic tests.
- Do not rewrite Plan 002/003 history; append precise Plan 004 state.

## Post-completion

Update `live-tracker.md` with distinct repository, candidate, browser, publication, cutover, and blocker fields. Return exact test counts, candidate/report/manifest paths and hashes, verifier issue taxonomy, browser matrix result (including explicitly unobserved cases), canonical/8090 before-and-after proof, exact cleanup outcome for QA-owned artifacts, and any external gate still unresolved.
