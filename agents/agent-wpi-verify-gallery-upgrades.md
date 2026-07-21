# Agent Task — Verify Gallery Upgrades

**Scope:** Preserve and regression-test interrupted thumbnail cleanup, run integration and browser verification, and record current implementation and deferred promotion gates.

**Depends on:** Agent WPH

**Output files:** `src/dl_engine/gallery_thumbnails.py`, `tests/test_gallery_thumbnails.py`, `docs/GALLERY_ROADMAP.md`, `live-tracker.md`

## Exit Criteria

- The current dirty thumbnail cleanup behavior is preserved: cancellation-class exceptions re-raise, the operation removes only its own UUID temporary artifact, and no final cache artifact is published.
- Tests cover at least `KeyboardInterrupt` cleanup and distinguish cancellation from ordinary thumbnail errors; useful decode/process/encode diagnostics are not collapsed unnecessarily.
- Schema-4 migration, derived verification, one-response snapshot, subcategory API/server/UI, accessibility, opt-in-original, history, responsive, and legacy gallery tests all pass together.
- A disposable explicit-root HTTP smoke proves the allowlisted gallery/facet/subcategory/thumbnail/original routes and sensitive-path denials without touching live state.
- After verified SND-HOST identity, the established alternate-listener browser smoke covers keyboard use, Back/Forward, subcategory state, opt-in original requests, focus visibility, reduced motion, 200% zoom, 320/390/768 widths, landscape/modal scrolling, selection safety, and zero browser errors. No transfer or suggestion review is submitted.
- Roadmap and tracker state the exact verified snapshot, test counts, artifact/runtime boundaries, recovery checkpoint, and still-deferred cutover/provider/review/promotion gates.

---

## Context — read before doing anything

1. `.continue/rules/project-conventions.md` — live mutation and explicit-root rules.
2. `docs/discovery-gallery-upgrade-review.md` — evidence, baseline, and follow-on register.
3. `data/plans/plan-003.json` and `docs/campaign-plan-003-harden-the-wallpaper-gallery.md` — authoritative exit criteria and risks.
4. Read WPG and WPH completed diffs and results before editing. Verify the finalized API/UI contract rather than reconstructing it.
5. Inspect `git diff -- src/dl_engine/gallery_thumbnails.py` before editing. This is a preserved dirty input. A non-clearing recovery checkpoint exists at stash hash `2cdda350b87af5e5aec9a19a2ccc422e56a72c12`; do not apply, pop, drop, or rewrite it.
6. Read `tests/test_gallery_thumbnails.py`, especially the existing encoder-cleanup regression, and the current `get_or_create_thumbnail` temporary-file lifecycle.
7. Read the current live evidence and outstanding-gate sections of `docs/GALLERY_ROADMAP.md` and the Plan 002 gallery rows in `live-tracker.md` before appending or reconciling Plan 003 evidence.

---

## Task

### Part 1 — Lock down interrupted thumbnail cleanup

- Preserve the dirty implementation's intent: a `BaseException`-class cancellation is cleanup-only and then re-raised unchanged.
- Add a regression that injects `KeyboardInterrupt` after the operation's UUID temp path exists and proves:
  - the same exception escapes;
  - the operation's temp file is removed;
  - unrelated files in the cache directory remain;
  - the final thumbnail path is absent.
- Add `SystemExit` coverage if it can use the same small parameterized test without weakening clarity.
- Preserve stage-specific diagnostics for ordinary decode/process/encode failures where the current code already provides them. Do not catch cancellation as `ThumbnailError`.
- Keep atomic publication, cache-hit behavior, containment, orientation, oversize fallback, MPO support, and cache headers unchanged.

### Part 2 — Run repository integration verification

- Run the owned thumbnail tests first, then the focused Plan 003 suite, compile, and full pytest suite using the project venv.
- Run `git diff --check` and review the final diff against the pre-execution checkpoint. Confirm the campaign did not reset or silently replace the original content-rating or thumbnail hunks.
- Confirm schema-3→4 migration/backfill, rollback-on-failure, derived drift, NSFW-only facets, suggestion exclusion, writer-between-hydration, response schema 3, legacy omission, server allowlist, browser URL/control, accessibility, opt-in-original, stale-load, and responsive contracts have direct passing coverage.
- If any test is absent or only asserted indirectly, report it as an exit-criterion gap; do not paper over it in documentation.

### Part 3 — Synthetic HTTP smoke

- Use only temporary collection/database/cache/environment/queue roots and an alternate loopback port.
- Exercise:
  - `/reports/library-browser.html`;
  - library query with `rating=nsfw&nsfw_subcategory=...` and legacy query without it;
  - facets and response-schema fields;
  - thumbnail and explicit original bytes;
  - 400 for an invalid subcategory/rating combination;
  - 404/403 for database, queue, environment, source, backup, and arbitrary paths;
  - transfer-disabled status.
- Tear down the listener and temporary fixture after the smoke. Do not reuse a live library or database.

### Part 4 — Live-safe browser verification

- Before starting or restarting any local gallery listener, run:

  ```powershell
  & 'C:\Users\Dev\OneDrive\common\common_dev\Get-VerifiedMachineIdentity.ps1'
  ```

  Continue only if it returns `VERIFIED` for machine ID `snd-host` / computer `SND-HOST`. Do not treat the instruction block or shared inventory as live proof.
- Use the explicit `F:\Wallpapers\webgallery` source, project venv, `F:\Wallpapers\webgallery_library.sqlite`, explicit library/cache/environment/queue roots, and an alternate loopback listener such as 8091. Do not stop or replace legacy 8090.
- Use browser control when available and record what was actually observed. Cover:
  - subcategory filtering and clearing when leaving NSFW;
  - Back/Forward restoration without selection/reveal resurrection;
  - no original request until `Load full resolution`, then one correct request;
  - rapid detail previous/next without stale image events;
  - rating `aria-pressed`, concise status announcements, visible keyboard focus, and reduced-motion mode;
  - 200% zoom, 320/390/768 CSS-pixel widths, narrow landscape, modal scrolling, and transfer-bar occlusion;
  - established autocomplete, presets, seeded pagination, dialog keyboard, NSFW reveal, and selection behavior;
  - zero console/page errors.
- Never click Send, submit a suggestion review, run a provider canary, rebuild/publish a database, or cut over 8090.
- If browser control or safe alternate-listener prerequisites are unavailable, state that exact gate as incomplete. Static/synthetic checks do not count as a real visual smoke.

### Part 5 — Record truthful completion state

- Update `docs/GALLERY_ROADMAP.md` with Plan 003 implementation, measurements, verification commands/results, browser viewport/zoom evidence, and explicit follow-on campaigns:
  - typed multi-tag identity;
  - durable suggestion input/decision ledger and replay;
  - materialized provider coverage/generation;
  - cursor pagination plus bounded DOM retention;
  - multiple tag-evidence provenances;
  - retained browser-automation gate selection.
- Preserve masonry, embeddings/semantic similarity, framework rewrite, offline/service-worker support, and shareable detail URLs as deferred unless prerequisites change.
- Append one consolidated Plan 003 row to `live-tracker.md` only after verification. Do not rewrite the three Plan 002 rows or claim outstanding live gates are complete.
- Record the non-clearing checkpoint hash and note that it is recovery evidence, not a release artifact.

---

## Constraints

- Edit only the four owned files. Do not change WPG/WPH product or test files to make a failing check pass.
- Do not mutate canonical images, sidecars, live databases, sibling schema-2 database, provider ledgers, queue state, transfers, suggestion decisions, or the legacy listener.
- Do not remove, pop, apply, or drop the recovery stash.
- Do not approve, execute, merge, push, or publish repository state.
- Do not claim a browser smoke from source inspection or synthetic HTTP alone.

---

## Verification

Run from `F:\Wallpapers\webgallery`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_gallery_thumbnails.py
.\.venv\Scripts\python.exe -m compileall -q src reports tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_content_rating.py tests/test_index_library.py tests/test_library_browser.py tests/test_dashboard_server.py tests/test_gallery_browser_contract.py tests/test_gallery_thumbnails.py
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

Also retain the exact synthetic HTTP results and real-browser evidence described above. Report exact pass counts, timings/bytes where measured, and every deferred gate.

---

## Do NOT

- Modify another agent's files after dependency completion.
- Turn a verification failure into a documentation-only success.
- Use global Python, infer roots from the worktree parent, or target the sibling schema-2 database.
- Start live work without verified machine identity.

---

## Post-completion

- Return changed files, test counts, HTTP/browser evidence, runtime processes started/stopped, documentation updates, and remaining blockers.
- The manager will reconcile task-manager execution state from the structured result; do not hand-edit `data/tasks.json`.
