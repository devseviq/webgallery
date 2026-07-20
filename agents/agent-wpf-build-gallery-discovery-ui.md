# Agent Task — Build Gallery Discovery UI

**Scope:** Add clickable typed tags, counted autocomplete, lightbox and keyboard navigation, density and fit controls, named URL presets, deterministic shuffle controls, and provenance-aware suggestion review presentation.

**Depends on:** Agent WPE, transitively Agent WPD

**Output files:** reports/library-browser.html, tests/test_gallery_browser_contract.py, docs/GALLERY_ROADMAP.md

## Exit Criteria

- Gallery cards load thumbnail_url, never original_url, until a user explicitly opens the detail/lightbox or chooses the original action.
- Typed/provenanced tag chips are clickable exact filters; autocomplete returns counted suggestions and active filter chips make every applied constraint visible/removable.
- A keyboard-accessible dialog shows a large image, all metadata, grouped provider tags, rating reasons/confidence, source link, and reviewable visual suggestions without losing grid/filter position.
- Density and contain/crop controls work responsively and persist in bookmarkable URL state.
- Named URL presets and seeded deterministic shuffle persist across reload and infinite-scroll pages.
- Existing rating separation, NSFW blur/reveal, infinite scroll, selection, transfer, missing-path behavior, and verification banner remain intact.
- Static JavaScript, accessibility, route-contract, and full Python tests pass. The roadmap distinguishes implemented code, live-verified state, working snapshot state, and provider backlogs.

---

## Context — read before doing anything

1. .continue/rules/project-conventions.md — the browser is a view/curation surface and must never move or rewrite originals.
2. docs/campaign-plan-002-secure-and-improve-the.md — complete six-point goal, UI exit criteria, suggestion boundary, and live verification gates.
3. docs/discovery-gallery-sorting-presentation.md — current UI behavior, desired presentation groupings, baseline bytes, and open design questions.
4. Agent WPD completion summary and reports/README_live_dashboard.md — exact allowlisted static, thumbnail, original, autocomplete, and suggestion-review routes.
5. Agent WPE completion summary, src/dl_engine/library_browser.py, and tests/test_library_browser.py — exact API schema, sort values, seed validation, facets, tag autocomplete, thumbnail/original URLs, rating fields, and suggestion payload.
6. reports/library-browser.html — read the complete current HTML/CSS/JS before editing, especially readUrl, syncUrl, loadStatus, apiUrl, renderItem, appendBatch, resetFeed, infinite scroll, NSFW reveal, selection, and transfer polling.
7. reports/_render_dashboard.py — read only for navigation conventions; WPD owns it.
8. reports/DASHBOARD_PLAN.md — read-only backlog context for lightbox, keyboard navigation, and more-like-this; do not edit it.

reports/library-browser.html is ordinary in-repository source. Record its
pre-edit hash and verify its final diff/hash; generated dashboard files remain
runtime-only and must not be added.

If WPD or WPE delivered a safer route/field name than the campaign draft, consume the implemented tested contract. If a required field is absent, stop and report the dependency gap; do not invent client-side filesystem access or duplicate rating/tag logic.

---

## Task

### Part 1 — Make URL state the single navigation contract

Extend the current state/readUrl/syncUrl/apiUrl flow with:

- seed for deterministic shuffle;
- density values compact, comfortable, and cinematic;
- fit values contain and crop;
- any WPE triage sort names;
- a named preset identifier only when it exactly matches current filters.

Validate every URL value against an explicit allowlist and fall back safely. Do not persist page offset. Preserve the current default SFW collection.

When shuffle is selected without a valid seed, create one bounded positive integer with crypto.getRandomValues, store it in state, and immediately write it to the URL. Changing filters may retain the seed; an explicit Reshuffle control generates a new seed. Infinite-scroll requests always send the same seed.

Implement presets as ordinary state/URL changes, not copied collections:

- Recently added;
- Desktop 4K: SFW, landscape, 4K;
- Portrait: SFW, portrait;
- Needs rating: unknown rating and lowest-confidence/least-tagged ordering as supported;
- Least tagged;
- Shuffle.

If a preset cannot be represented by the actual API contract, omit it and document the dependency rather than silently approximating it.

### Part 2 — Add visible filters and counted tag discovery

Replace the exact-tag dead-end with a combobox-style autocomplete:

- debounce input by about 200 ms;
- cancel stale fetches with AbortController;
- query WPD /api/library/tags using the WPE prefix/limit contract;
- render label, type/source, and distinct image count;
- support ArrowUp, ArrowDown, Enter, Escape, click, and focus-out;
- escape through DOM textContent only, never innerHTML;
- apply the selected exact tag and reset the feed.

Render active-filter chips for nondefault rating, source, orientation, bucket, tag, franchise, sort, and preset state. Each chip removes only its own constraint, updates controls/URL, and resets the feed. Include a clear-all action.

On every card:

- display a diverse bounded set of typed provider tags, preferring franchise/series/character/subject before structural/search tags;
- make each chip a button that applies its exact tag;
- expose type, source, and provenance in an accessible title/label;
- prevent chip clicks and selection toggles from opening the lightbox.

Do not show machine suggestions among provider tag chips.

### Part 3 — Use thumbnails for cards and originals only on demand

Refactor renderItem so:

- card img.src is item.thumbnail_url;
- no card element assigns item.original_url to an img source;
- missing thumbnails show a controlled placeholder;
- loading lazy, decoding async, explicit width/height or aspect-ratio, and alt text are retained;
- the media control opens the in-app detail dialog instead of a raw new tab.

Keep an explicit Open original action inside the dialog using original_url with target blank and noopener. The main detail image may begin with thumbnail_url and switch to original_url only after the dialog opens. Show a loading state and fall back to the thumbnail on original failure.

NSFW images remain blurred in cards and the dialog until the existing Reveal control is enabled. Unknown and suggestive content remain visibly labelled; unknown is never styled as SFW.

### Part 4 — Add an accessible lightbox/detail dialog

Use a native dialog where supported, with a safe fallback. It must:

- have an accessible name, close button, focus management, and return focus to the opening card;
- close on Escape and backdrop click;
- navigate loaded items with Left/Right buttons and ArrowLeft/ArrowRight while ignoring key events from inputs/buttons;
- preserve grid scroll position, filters, selection, and infinite-scroll state;
- show franchise/title, dimensions, bucket, orientation, source, source link, downloaded date, bytes, extension, rating/confidence/basis/reasons, tag_count, and enrichment status;
- group all provider tags by type and show provenance/source;
- display visual suggestions in a separate labelled section with confidence, generator/model version, provenance, and review status.

For pending suggestions, provide Accept and Reject controls that POST the exact WPD/WPE same-origin JSON contract. Disable controls during the request, update only from the successful response, and surface errors without changing provider tags. Accepted suggestions remain in the suggestion section and are not visually relabelled as provider evidence.

Never display absolute image paths or DB paths.

### Part 5 — Add density and fit controls

Implement CSS custom properties driven by body data attributes:

- compact: smaller minimum card width and media height for metadata triage;
- comfortable: current general-purpose density;
- cinematic: larger cards/media for visual browsing.

Fit controls switch all card/detail preview images between contain and cover/crop. Crop must never alter files. Controls are keyboard-operable segmented buttons or selects with visible active state and URL persistence.

Keep the existing responsive breakpoint usable on narrow windows. Avoid masonry if it breaks deterministic reading order, keyboard navigation, or incremental append; document it as deferred if omitted.

### Part 6 — Preserve existing workflows

Retain:

- rating tabs and counts;
- default SFW collection and explicit Unknown separation;
- NSFW reveal control;
- verification-status banner;
- abortable infinite scroll and duplicate-ID protection;
- item selection, select-loaded, clear, target selection, transfer creation, and job polling;
- missing-path exclusion from transfer;
- source/orientation/bucket/franchise filters.

Update code references from item.url to original_url or thumbnail_url deliberately. Selection remains ID-based. A lightbox open/close must never toggle selection.

### Part 7 — Add static regression tests

Create tests/test_gallery_browser_contract.py. It must read the in-repository
reports/library-browser.html directly and verify:

- exactly one executable inline application script and valid JavaScript syntax through node --check when Node is available;
- card image assignment uses thumbnail_url and original_url assignment occurs only in the detail-open path;
- no ../library, ../temp_downloads, raw queue-state, filesystem path, or SQLite URL appears;
- autocomplete endpoint, debounce/cancellation, keyboard combobox attributes, counted option rendering, and exact-tag application exist;
- dialog semantics, accessible labels, focus return, Escape/arrow handling, and explicit original action exist;
- density/fit allowlists and URL persistence exist;
- shuffle seed generation, URL persistence, and API transmission exist;
- named presets are represented by ordinary filters;
- suggestion review uses POST and keeps suggestions separate from provider tags;
- existing NSFW blur, reveal, infinite scroll, selection, transfer, and verification-status hooks remain.

Use robust parsing/string boundaries rather than snapshotting the entire HTML. Tests do not need the live server and must not write reports.

### Part 8 — Write the durable roadmap and consolidate status

Create docs/GALLERY_ROADMAP.md with the six approved items in order:

1. allowlisted gallery server;
2. SHA-keyed cached thumbnails;
3. batched/materialized API;
4. clickable tags, autocomplete, lightbox, density, presets, shuffle;
5. provider enrichment with current Wallhaven/Zerochan baselines;
6. reviewable visual suggestions with confidence/provenance.

For each item include goal, delivered artifacts, verification command, rollback, status, and remaining work. Use exact states:

- Planned: specified but not implemented;
- Implemented: code/tests exist;
- Working: live smoke passed but maintenance boundary is unverified;
- Verified: all campaign exit checks and current index verification passed;
- Deferred: explicitly not part of this campaign.

Do not label provider backlogs complete from an import path or one-item canary. Record current counts only if refreshed read-only in this run; otherwise label the 11,923 Wallhaven and 28,310 Zerochan values as the 2026-07-20 discovery baseline.

After all tests and integration evidence, append/consolidate WP-GALLERY-UI-001 and campaign status in live-tracker.md without deleting WPD/WPE rows. Because live-tracker is not a WPF-owned plan file, make only the required append after re-reading it and call out the sequential coordination in the completion summary.

---

## Constraints

- Modify only library-browser.html, the new contract test, GALLERY_ROADMAP.md, and the required final tracker append.
- Do not edit server, index, transfer, classifier, sidecar schema, images, sidecars, DB, queue, cache, or provider ledger.
- Preserve every existing transfer, rating, and infinite-scroll behavior unless the dependency contract explicitly changed.
- Use plain browser APIs; add no frontend framework, build system, external CDN, or analytics.
- Use textContent and DOM construction for untrusted API values. Do not introduce innerHTML with API data.
- Browser automation may be unavailable; never claim visual inspection unless it actually ran. Static tests plus live HTTP are distinct evidence.

---

## Verification

Run:

    python -m compileall -q src tests
    python -m pytest -q tests/test_gallery_browser_contract.py tests/test_library_browser.py tests/test_dashboard_server.py tests/test_gallery_thumbnails.py
    python -m pytest -q
    git diff --check -- tests/test_gallery_browser_contract.py docs/GALLERY_ROADMAP.md live-tracker.md

Also run the contract test against the exact reports/library-browser.html hash.
If browser control is available, smoke:

- tag keyboard autocomplete;
- each preset and URL reload;
- shuffle reload plus two appended pages;
- dialog open/close/focus/arrow keys;
- contain/crop and density;
- NSFW blur/reveal;
- selection and a non-sending transfer-status path.

Do not send files during a UI smoke unless separately authorized.

Live byte/timing measurements require VERIFIED snd-host identity before starting/restarting the server or creating cache files. Record card thumbnail bytes separately from an explicitly opened original.

---

## Do NOT

- Put original_url on card images or preload originals.
- Merge suggestions into provider tags, rating chips, autocomplete, or content-rating evidence.
- Lose seed, filters, selection, scroll, or focus when opening details.
- Use client-side path construction to access library/temp files.
- Invent provider coverage, tag types, or verified status.
- Reformat or regenerate unrelated report files.

---

## Post-completion

Update live-tracker.md sequentially:

| ID | Status | Owner | Scope | Issue | Update |
|---|---|---|---|---|---|
| WP-GALLERY-UI-001 | Done or Working | agent-wpf | clickable metadata, autocomplete, lightbox, density/fit, presets, seeded shuffle, suggestion review UI | Turn indexed metadata into a lightweight discovery workflow | Record tests, repository HTML hash, live/browser evidence, byte comparison, and remaining provider/live-verification work. |
