# Agent Task — Harden Gallery Interactions

**Scope:** Add the NSFW-subcategory discovery control, accessible state and announcements, opt-in full-resolution loading, Back/Forward URL hydration, and responsive control hardening.

**Depends on:** Agent WPG

**Output files:** `reports/library-browser.html`, `tests/test_gallery_browser_contract.py`

## Exit Criteria

- The finalized WPG response-schema-3 `nsfw_subcategory` field and facet are usable through a control, URL state, API request, active chip, preset reset, and detail facts; the control is active only when rating is NSFW.
- Rating tabs expose `aria-pressed`; the filter form and gallery are labelled; concise status regions announce loading/results; bulk card insertion is not itself a live region.
- All actionable controls have a consistent visible focus indicator, tag chips meet practical touch-target sizing, and nonessential motion is disabled under `prefers-reduced-motion`.
- Detail navigation keeps the thumbnail by default and requests the original only after an explicit `Load full resolution` action. Rapid previous/next navigation cannot apply stale image success/error events.
- Intentional state changes create usable browser history; initial normalization uses replacement; Back/Forward rehydrates filters, preset, seed, density, and fit without persisting selection or NSFW reveal state.
- Small-screen transfer controls collapse until selection exists, the dialog uses dynamic viewport sizing, and controls do not occlude content at narrow portrait or landscape widths.
- Static contracts, JavaScript syntax, focused tests, compile, full tests, and `git diff --check` pass.

---

## Context — read before doing anything

1. `.continue/rules/project-conventions.md` — DOM safety and runtime boundaries.
2. `docs/discovery-gallery-upgrade-review.md` — evidence and deferred scope.
3. `data/plans/plan-003.json` and `docs/campaign-plan-003-harden-the-wallpaper-gallery.md` — campaign contracts.
4. Read WPG's completed changes in `src/dl_engine/library_browser.py`, `reports/dashboard_server.py`, and their tests. Consume the finalized field names, validation semantics, facets, response schema, and error shape; do not invent a second contract.
5. Read all of `reports/library-browser.html` and `tests/test_gallery_browser_contract.py`, especially `readUrl`, `syncUrl`, `applyPreset`, `apiUrl`, `paintRatingTabs`, autocomplete selection, `openDetail`, detail image loading, card append/reset, selection state, dialog focus, and existing keyboard/URL assertions.

---

## Task

### Part 1 — Add the NSFW-subcategory discovery surface

- Add an `nsfw_subcategory` state field and a labelled control populated from the API's constant-shape facet/allowed values.
- Show/enable the control only for `rating=nsfw`. Clear it whenever rating leaves NSFW, including preset changes and Back/Forward restoration.
- Round-trip it through URL parsing/serialization, API query generation, active filters, clear actions, preset defaults, and detail facts.
- Keep legacy URLs without the field unchanged. Do not treat `unspecified` as a global non-NSFW category.
- Extend static contracts for valid parameter wiring, reset semantics, control visibility, active chips, detail facts, and response-schema compatibility.

### Part 2 — Correct accessibility state and announcements

- Make rating tabs expose their current pressed/selected state programmatically on every repaint, following the existing preset-button pattern where appropriate.
- Give the filter form and gallery meaningful accessible names without duplicating visible headings.
- Move result/load announcements to the concise status elements. Remove `aria-live` from the card grid so appending 48 interactive cards does not flood announcements.
- Make the index banner a status region with appropriate atomicity.
- Add a consistent `:focus-visible` treatment for every actionable control, not only tag buttons.
- Increase tag-chip and compact-control hit areas without making the dense desktop layout unusable.
- Add a `prefers-reduced-motion: reduce` rule that removes nonessential transitions and animations while preserving state changes.

### Part 3 — Make originals explicitly opt in

- `openDetail` and previous/next navigation must render the thumbnail and facts without scheduling the original automatically.
- Add a clearly labelled `Load full resolution` action. Only that action assigns/requests the `original_url`; retain the separate direct `Open original` link/action.
- Reset the load action and original state when detail navigation changes items.
- Use an item/request generation token or equivalent identity check so late success/error events from a previous item cannot replace the current image or message.
- Preserve focus trapping, arrow navigation, Escape, focus return, NSFW reveal, and missing-thumbnail/original fallbacks.
- Update the existing static test that currently requires automatic original loading, and add opt-in plus stale-event guards.

### Part 4 — Make URL state navigable

- Separate initial URL normalization from intentional navigation. Use `replaceState` only for initialization/canonicalization and `pushState` for deliberate filter, sort, preset, seed, density, fit, and subcategory changes.
- Add a `popstate` handler that re-reads the URL, normalizes state, repaints controls, resets the feed/request epoch safely, and reloads from offset zero without creating another history entry.
- Keep selection, transfer state, dialog state, and NSFW reveal ephemeral. Back/Forward must not resurrect them.
- Avoid duplicate history entries when the canonical serialized state is unchanged.
- Extend static contracts for push versus replace use, one popstate handler, hydration/reset behavior, and ephemeral-state isolation.

### Part 5 — Refine small-screen behavior

- Collapse or minimize the sticky transfer bar when no images are selected; expand it accessibly when selection exists.
- Use dynamic viewport units with safe fallbacks for the detail dialog and prevent vertical control occlusion.
- Cover 320px, 390px, 768px, and landscape-oriented layout rules without changing deterministic card/infinite-scroll order.
- Keep density and contain/crop behavior intact.

---

## Constraints

- Edit only the two owned files. WPG owns all backend/API/schema files; WPI owns roadmap, tracker, thumbnail files, and final live evidence.
- Build all untrusted text with `textContent`, `createElement`, attribute setters, and existing DOM helpers. Do not introduce `innerHTML` with runtime data.
- Preserve provider tags as authoritative navigation and suggestions as separate review-only data.
- Preserve deterministic positive shuffle seeds, pagination uniqueness, rating defaults, selection, transfer enablement, infinite scroll, missing-path handling, and dialog keyboard behavior.
- Do not add a framework, package, service worker, browser dependency, masonry layout, semantic embeddings, multi-tag groups, cursor pagination, or DOM virtualization.
- Do not start/stop a live listener or submit transfer/review actions.

---

## Verification

Run from `F:\Wallpapers\webgallery`:

```powershell
.\.venv\Scripts\python.exe -m compileall -q src reports tests
.\.venv\Scripts\python.exe -m pytest -q tests/test_gallery_browser_contract.py tests/test_library_browser.py tests/test_dashboard_server.py
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

The contract suite must parse the inline JavaScript with Node and assert the subcategory, accessibility, opt-in-original, history, stale-load, and responsive hooks. Report exact pass counts.

---

## Do NOT

- Modify backend contracts to make the frontend easier; report any mismatch instead.
- Reintroduce automatic full-resolution loading on dialog open or arrow navigation.
- Push history entries during initial load or while handling `popstate`.
- Persist selection, reveal, transfer, or dialog state in the URL.
- Edit `live-tracker.md`; WPI owns the consolidated verified update.

---

## Post-completion

- Return changed files, user-visible behavior, static/full test counts, and any browser behaviors that still require WPI's real smoke.
- Do not mark WPI complete or update the tracker.
