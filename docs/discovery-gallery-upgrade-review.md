# Discovery — Gallery Upgrade Review

**Goal:** Resume the gallery campaign, review the current work, and put evidence-backed upgrades into the next plan.
**Date:** 2026-07-20
**Status:** complete
**Recommended next:** `/planner Harden gallery contracts, accessibility, navigation, and regression coverage using this discovery`

---

## Questions

1. What exactly landed in the previous gallery pass, and what remains incomplete?
2. Which campaign tasks are ready, blocked, or stale?
3. Do the current UI, API, index, and documentation contracts agree?
4. Which tests and live checks pass on the current snapshot?
5. Which usability, tagging, accessibility, correctness, and performance upgrades are evidenced by the code?
6. Which upgrades are safe to implement now, and which belong in later campaigns?

---

## Findings

### Q1: What landed, and what remains incomplete?

**Answer:** Plan 002's three implementation agents are complete. The schema-3 index, allowlisted server, thumbnail pipeline, discovery UI, and live browser smoke are implemented. Promotion remains intentionally incomplete: the legacy 8090 listener has not been cut over, the active queue blocks the bounded Wallhaven canary, and no live suggestion-review decision has been submitted.

**Evidence:**

- `live-tracker.md:19-21` — records implemented server, index, and UI work plus side-by-side live verification.
- `docs/GALLERY_ROADMAP.md:310-318` — records deliberate cutover, provider canary, review canary, and promotion-boundary verification as outstanding.
- `data/plans/plan-002.json:6` — the authoritative plan lifecycle status is `executed`.

**Implications:**

- Do not relaunch Plan 002 or redefine its delivered contracts.
- Register upgrades as a new campaign and keep operational promotion gates distinct from code completion.

### Q2: Which task state is trustworthy?

**Answer:** The per-agent task rows and durable tracker are newer than the stale execution ledger. All three agents are `done`, but the same state file still reports `verification_failed`, a running WPF agent, and an obsolete `merge` next action.

**Evidence:**

- `data/tasks.json:9`, `data/tasks.json:72`, `data/tasks.json:122` — WPD, WPE, and WPF are `done`.
- `data/tasks.json:192-215` — the execution ledger still reports failed verification and awaiting results without a failing command.
- `live-tracker.md:19-21` — later integration and live evidence supersedes that stale execution snapshot.
- `python scripts/task_manager.py plan preflight --json` — returned `ready=true`, no errors, and only the dirty-worktree warning on 2026-07-20.

**Implications:**

- Treat the old execution ledger as reconciliation work, not as authority to rerun or merge completed agents.
- New work must use a new plan identifier and explicit file ownership.

### Q3: Do the current contracts agree?

**Answer:** The committed gallery contracts agree, but the current dirty NSFW-subcategory work is incomplete and must not be promoted as written. It contains an iterable-consumption bug, advertises a filter absent from server/API/index/UI code, and regresses the runbook to the sibling schema-2 database and legacy listener.

**Evidence:**

- `src/dl_engine/content_rating.py:306-315` — the dirty classifier passes `tags` to `classify_content()` and then iterates it again; a one-shot generator therefore loses tag evidence.
- `docs/CONTENT_RATING.md:84-88` — claims `nsfw_subcategory` is accepted by the gallery query.
- `reports/dashboard_server.py:659-670` — the query allowlist has no subcategory field.
- `src/dl_engine/library_browser.py:198-240` and `src/dl_engine/index_library.py:3486-3547` — neither the API wrapper nor SQL query accepts the field.
- `reports/README_live_dashboard.md:125-140` — the gallery owns `webgallery_library.sqlite`; the sibling `wallpaper_library.sqlite` remains schema 2.
- `docs/CONTENT_RATING.md:46-88` — the dirty rewrite points back to global Python, port 8090, sibling source, and the schema-2 database.

**Implications:**

- Materialize the input iterable once before both classifiers.
- Either implement the subcategory end to end or remove the public claim. The recommended path is an indexed, materialized schema/API/UI filter that implies `rating=nsfw`.
- Reconcile the documentation against the explicit-root, venv, alternate-listener, schema-3 runbook before promotion.

### Q4: Which verification is current?

**Answer:** The current dirty snapshot compiles and passes the full repository suite, but it lacks tests for the new subcategory contract and for interruption cleanup in the dirty thumbnail change.

**Evidence:**

- Current run: `196 passed, 61 subtests passed` for the full suite.
- Current run: `171 passed, 61 subtests passed` for focused server, thumbnail, index, browser, and rating tests.
- Current run: `python -m compileall -q src reports tests` passed.
- Current run: `git diff --check` passed.
- `tests/test_gallery_thumbnails.py:180-193` — covers encoder cleanup but not `KeyboardInterrupt` or `SystemExit` cleanup.
- A disposable `KeyboardInterrupt` probe re-raised the interruption, removed the UUID temporary file, and published no final thumbnail.
- No current test references `nsfw_subcategory`.

**Implications:**

- Existing green tests are a regression baseline, not proof that the dirty feature is complete.
- Add generator/list parity, precedence, schema/query/API/server/UI, URL-state, and interruption-cleanup tests before promotion.

### Q5: Which upgrades are evidenced?

**Answer:** The highest-value immediate tranche is correctness and interaction hardening. Larger pagination, evidence-model, and provider-discovery changes should remain separately planned.

**Evidence:**

- `reports/library-browser.html:647-654` — rating tabs change visual state without `aria-pressed`.
- `reports/library-browser.html:393`, `reports/library-browser.html:1550-1575` — the entire growing grid is a polite live region even though a concise feed status already exists at lines 395-397.
- `reports/library-browser.html:1412-1419`, `reports/library-browser.html:1447-1449` — detail navigation automatically upgrades every thumbnail to the original.
- `docs/GALLERY_ROADMAP.md:73-84`, `docs/GALLERY_ROADMAP.md:97-104` — the measured 48-card original baseline was 218.5 MiB versus 619,440 thumbnail bytes.
- `reports/library-browser.html:581-603`, `reports/library-browser.html:1781-1787` — URL state is bookmarkable through `replaceState`, but there is no Back/Forward hydration.
- `src/dl_engine/library_browser.py:226-243`, `src/dl_engine/index_library.py:3571-3614` — count, page, tags, and suggestions are read without one explicit SQLite snapshot.
- `src/dl_engine/library_browser.py:101-107`, `reports/library-browser.html:959-970`, `reports/library-browser.html:1108-1120` — typed tag identity is displayed but selection collapses it to the name.
- `src/dl_engine/index_library.py:259-275`, `src/dl_engine/index_library.py:1925-1928` — an image/tag association retains only one provenance and later evidence is ignored.
- `reports/library-browser.html:486-490`, `reports/library-browser.html:1550-1567` — long sessions retain all loaded IDs, items, selections, and DOM cards while offset pagination can skip/repeat across changing snapshots.

**Implications:**

- Implement now: NSFW-subcategory contract repair, one-response read snapshot, accessibility state/live-region/focus hardening, opt-in full-resolution loading, Back/Forward URL hydration, responsive control fixes, and targeted regression coverage.
- Plan next: typed multi-tag identity, durable suggestion-decision replay, provider-coverage query materialization, cursor pagination with bounded DOM retention, and multiple evidence provenances.
- Defer: masonry, embeddings/semantic similarity, framework rewrite, offline support, and shareable detail URLs until stable prerequisite contracts exist.

### Q6: What is safe to place in the next campaign?

**Answer:** A three-agent sequential campaign fits the degraded analyzer and dirty-worktree constraints: backend contract repair first, UI hardening second, then integration/browser verification. Larger schema and pagination initiatives should be captured as follow-on work rather than folded into this tranche.

**Evidence:**

- `data/plans/plan-002.json:548-570` — analysis is heuristic-only and low-confidence.
- `.Codex/skills/project.toml:22-27` — API/server, browser/API, and index/docs are declared conflict zones.
- `.continue/rules/project-conventions.md:16-20` — tests use synthetic roots and runtime paths must be explicit.
- Current dirty files are `docs/CONTENT_RATING.md`, `src/dl_engine/content_rating.py`, and `src/dl_engine/gallery_thumbnails.py`; none may be reset or silently replaced.

**Implications:**

- Use at most three sequential owners with exact file lists.
- The first owner must preserve and finish the existing dirty content-rating work; the final verifier must preserve and cover the thumbnail cleanup change.
- Require a clean reviewable diff and current focused/full suite before any live gate or promotion.

---

## Cross-Cutting Analysis

### Constraints

- Canonical images, sidecars, provider ledgers, queues, and sibling databases are outside Git and must not be mutated by implementation tests.
- Live server, database, cache, queue, or provider mutations require verified SND-HOST identity.
- The active queue currently has child downloaders, so the Wallhaven canary remains deferred.
- Unknown content must never become SFW from missing evidence; pending visual suggestions must never become provider tags or rating evidence.
- The analyzer is heuristic-only, so the plan must use no more than three agents, sequential dependencies, and exact file ownership.
- Plan 002 is executed; upgrades require a new plan rather than retroactive lifecycle mutation.

### Risks

| Risk | Likelihood | Impact | Notes |
|---|---|---|---|
| Dirty classifier/docs are overwritten or promoted partially | High | High | Preserve current hunks and close the end-to-end contract before promotion. |
| `unspecified` subcategory includes non-NSFW rows | High | High | A subcategory filter must imply `rating=nsfw`. |
| A gallery response mixes multiple WAL commits | Medium | High | Wrap count, page, tag, and suggestion hydration in one explicit read transaction. |
| UI hardening regresses established keyboard/URL behavior | Medium | Medium | Expand static contracts and retain a real browser smoke. |
| Automatic originals restore high bandwidth use | High | Medium | Make full-resolution loading explicit and suppress stale navigation loads. |
| Backlog scope overwhelms one campaign | High | Medium | Keep multi-provenance, cursor/DOM windowing, and durable review ledgers in follow-on plans. |
| Stale task ledger triggers duplicate work | Medium | High | Reconcile state; never rerun completed Plan 002 agents from the obsolete execution snapshot. |

### Open Questions

- Which retained browser automation dependency, if any, should become the repository-level DOM regression gate? The immediate plan can use the existing static suite plus the established live browser smoke while this is selected.
- Suggestion-review durability needs a dedicated ledger contract before any live review canary is allowed to create decisions worth preserving.

---

## Optimization Register

| Candidate | Type | Evidence | Risk | Confidence | Decision |
|---|---|---|---|---|---|
| Opt-in full-resolution detail loads | hot-path | `library-browser.html:1412-1449`; roadmap byte baseline | low | high | implement-now |
| One explicit read snapshot per page | hot-path/correctness | `library_browser.py:226-243`; `index_library.py:3571-3614` | medium | high | implement-now |
| Accessibility state/live-region/focus hardening | verification/usability | `library-browser.html:393`, `647-654`, `1550-1575` | low | high | implement-now |
| Browser Back/Forward hydration | usability | `library-browser.html:581-603`, `1781-1787` | medium | high | implement-now |
| Materialized provider-coverage response | hot-path | `index_library.py:1200-1217`, `1954-2008`; `library_browser.py:293-309` | medium | medium | suggest-only |
| Typed multi-tag identity | structural/discovery | `library_browser.py:101-107`; `library-browser.html:959-970` | medium | high | defer to next campaign |
| Cursor pagination plus bounded DOM | hot-path/structural | `index_library.py:3569-3571`; `library-browser.html:486-490`, `1550-1567` | high | high | defer to next campaign |
| Multiple tag-evidence provenances | structural/schema | `index_library.py:259-275`, `1925-1928` | high | high | defer to next campaign |

## Baseline

- Gallery API: about 56.05 ms warm and 84,316 bytes for 48 items on the verified 85,509-image schema-3 index.
- Card media: 619,440 thumbnail bytes for the verified 48-item page versus 229,156,017 original bytes.
- Browser: live-root keyboard, presets, seeded pagination, dialog, NSFW, selection, and 390x844 responsive smoke passed with zero browser errors.
- Repository: 196 tests plus 61 subtests pass on the current dirty snapshot.

---

## Recommendation

Findings support a new three-agent sequential campaign. First finish and test the dirty NSFW-subcategory/index/API contract plus snapshot consistency; then harden the gallery UI's accessibility, full-resolution loading, URL navigation, and responsive controls; finally add integration/interruption coverage and repeat static, full-suite, synthetic HTTP, and live-browser checks. Record typed multi-tag identity, durable suggestion decisions, provider-coverage materialization, cursor/DOM windowing, and multi-provenance evidence as explicit follow-on campaigns.
