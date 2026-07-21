# Web Gallery Roadmap

This is the durable implementation and operations record for the six approved
gallery improvements. The gallery is a view and curation surface over a
rebuildable SQLite index. It never moves, rewrites, or physically groups
canonical wallpapers or their sidecars.

## Status language

Task-manager state, repository state, manifest state, and operational state are
separate. Use these exact terms without treating one category as another:

| Repository/campaign status | Meaning |
|---|---|
| `planned` | Contract/specification exists; executable implementation is incomplete. |
| `implemented` | Repository implementation exists; the current full repository gate has not been recorded. |
| `repository-verified` | Current compile, full and targeted tests, plan validation, and diff checks all pass. |
| `deferred` | Explicitly outside the current campaign. |

| Closed manifest state | Meaning |
|---|---|
| `candidate-built` | A unique candidate and initial manifest exist, without accepted exhaustive verification. |
| `candidate-verified` | Initial exhaustive candidate evidence is accepted, but it is not fresh under an external hold. |
| `ready-to-publish` | Fresh under-hold verification and all pre-mutation gates pass; canonical bytes remain unchanged. |
| `published` | The activated canonical path reopened, exhaustively verified, and matched the candidate. |
| `rolled-back` | The exact prior canonical database set was restored and verified. |

| Diagnostic/external status | Meaning |
|---|---|
| `candidate-blocked` | A candidate/report failed a gate. This is diagnostic shorthand, never a manifest state. |
| `cut-over` | The external listener owner started the intended listener on the verified canonical database. This is not a manifest state. |

Task-manager `executed` proves only that agent tasks were registered. It proves
none of the repository, candidate, publication, cutover, or rollback states.

## Current Plan 004 state

Plan 004 publication implementation is merged and `repository-verified`.
Python compilation, the 148-test targeted gate (plus 125 subtests), the
252-test full gate (plus 186 subtests), PowerShell parser/help checks, plan
validation, and diff checks all passed. This repository result grants no live
candidate, publication, or cutover state.

No post-merge live candidate, exhaustive report, alternate-listener browser
matrix, publication, cutover, recovery, queue/task/process inventory, listener
inventory, or canonical-database observation or mutation was run during this
documentation reconciliation. Consequently there is no current
`candidate-built`, `candidate-verified`, `ready-to-publish`, `published`,
`cut-over`, or `rolled-back` result to report.

All 2026-07-20 and earlier 2026-07-21 runtime evidence retained below is
historical and date-qualified. It may be stale and does not satisfy Plan 004
acceptance. In particular, the retained schema-4 diagnostic candidate from
2026-07-21 failed exhaustive verification with exactly 200 `layout-mismatch`
issues, its report lacked the required generated timestamp, and it had no
publication manifest; it remains `candidate-blocked`. Earlier schema-3 and
alternate-8091 evidence remains useful history, but it does not prove the
current canonical schema, queue/task/process/listener state, a schema-4
publication, or cutover.

## 1. Allowlisted gallery server

**Goal:** expose only named report assets, sanitized APIs, cached thumbnails,
and indexed media identities. Queue state, environment/config files, databases,
backups, source scripts, logs, sidecars, and arbitrary workspace paths must not
be web resources—even on loopback.

**Delivered artifacts:** `reports/dashboard_server.py` now uses an explicit
`ThreadingHTTPServer` handler and explicit runtime roots, with no generic static
root or `SimpleHTTPRequestHandler` fallback. `reports/_build_dashboard.py`,
`reports/_render_dashboard.py`, `reports/watch_dashboard.ps1`, and
`reports/README_live_dashboard.md` preserve the operations view through
sanitized status and allowlisted media contracts. `tests/test_dashboard_server.py`
probes allowed, denied, traversal, method, origin, and path-free payload cases.

**Verification:** `.venv\Scripts\python.exe -m pytest -q tests/test_dashboard_server.py`, then—only
after verified SND-HOST identity—start an alternate loopback listener and probe
the allowlist plus known denied queue/config/database paths without reading
sensitive response bodies.

**Rollback:** stop the replacement listener, restore the previous server and
report source, and restart the previous listener only after the machine identity
gate. Canonical images and SQLite evidence are not part of this rollback.

**Status:** `implemented`.

**Historical live side-by-side evidence (2026-07-20; stale for Plan 004
acceptance):** after verified SND-HOST identity, the then-current server ran
under Python 3.14.6 on `127.0.0.1:8091` with explicitly recorded live roots.
Allowlisted HEAD routes returned 200; sensitive runtime, source, and database
paths returned 404. The existing `8090` listener was not touched.

**Remaining work:** for that dated run, the identity-gated
alternate-listener boundary completed.
The deliberate `8090` cutover remains outstanding and must retain its own identity,
ownership, and rollback gate. Do not infer a runtime root from this Git
worktree.

## 2. SHA-keyed cached thumbnails

**Goal:** make cards lightweight while retaining explicit, on-demand access to
the original. The 2026-07-20 discovery baseline for the first 48 SFW card
originals was **218.5 MiB** total.

**Delivered artifacts:** `src/dl_engine/gallery_thumbnails.py` creates bounded,
orientation-correct, metadata-stripped WebP derivatives beneath a dedicated
versioned cache. Keys derive from indexed SHA-256 plus transform version;
generation is concurrency-bounded, per-key locked, and atomically published.
The server exposes only `/thumb/<sha256>.webp` and `/original/<image-id>`.
`reports/library-browser.html` assigns card image sources only from
`thumbnail_url`; it requests `original_url` only after the user opens the detail
dialog, and keeps the explicit Open original action inside that dialog.

**Verification:** `.venv\Scripts\python.exe -m pytest -q tests/test_gallery_thumbnails.py
tests/test_dashboard_server.py tests/test_gallery_browser_contract.py`. After
the identity gate, measure the summed response bytes for one cold and warm
48-card thumbnail page separately from one explicitly opened original.

**Rollback:** stop the server, disable thumbnail routes/client use, and remove
only the explicitly configured derived thumbnail-cache root. Never delete from
the canonical library.

**Status:** `implemented`.

**Historical live side-by-side evidence (2026-07-20; stale for Plan 004
acceptance):** the 48-item page exposed 47 thumbnail URLs and one deliberate
original-only fallback for a
17,485×9,000 image whose 157,365,000 pixels exceed the 120-million-pixel
thumbnail safety bound. The cold 47-thumbnail pass took 8,128.22 ms and
transferred 619,440 bytes; the warm pass took 89.78 ms and transferred the same
619,440 bytes. The corresponding originals totalled 229,156,017 bytes, so the
thumbnail payload was 0.270% of the original payload. An explicit original HEAD
request matched its expected 10,542,747-byte length.

The browser pass also surfaced one valid JPEG/MPO source that Pillow decoded
but the source-format allowlist initially rejected. Bounded MPO support now
serves that exact live thumbnail as a 12,868-byte WebP with HTTP 200 while
retaining the same 120-million-pixel cap and metadata-stripping transform.

**Remaining work:** the 2026-07-20 cold/warm cache and byte-comparison gate
completed for that run. Current operational adoption still requires the Plan
004 gates and separately controlled cutover.

## 3. Batched and materialized gallery API

**Goal:** remove per-card tag queries and repeated rating/facet aggregation,
while adding stable triage and shuffle queries without exposing local paths.

**Delivered artifacts:** current repository schema 4 in
`src/dl_engine/index_library.py` includes the schema-3 materialized rating,
confidence, basis, reasons, tag-count, and global-facet work plus the validated
NSFW subcategory contract. The HTTP library response remains schema 3. Page
reads use one image query, one batch provider-tag hydration query, and one batch
suggestion query. Stable indexes and the `least_tagged`, `rating_confidence`,
and seeded `shuffle` sorts support discovery and triage.
`src/dl_engine/library_browser.py` serializes typed/provenanced tags, counted
autocomplete, provider coverage, path-free thumbnail/original identities, and a
separate suggestion collection. Migration, query-shape, WAL freshness, API, and
rating boundaries are covered by index/browser/content-rating tests.

**Verification:** run repository tests, then the explicit read-only verifier
after the identity gate:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_index_library.py tests/test_library_browser.py tests/test_content_rating.py
.\.venv\Scripts\python.exe -m dl_engine.index_library --verify-json --library-root F:\Wallpapers\library --db-path F:\Wallpapers\webgallery_library.sqlite
```

Canonical schema-4 packaging and replacement must instead use the Plan 004
candidate/manifest workflow. Record warm API timings separately from the older
roughly two-second discovery measurements.

**Rollback:** retain canonical media, sidecars, provider ledgers, and the
sibling database. If a Plan 004 activation transaction exists, keep the
external hold asserted and use its exact manifest, hashed backup directory,
first journal, continuation segments, and `Recover -Apply` path; do not discard
or hand-copy the canonical main/WAL/SHM set. Source-only rollback remains
separate from database recovery.

**Status:** `implemented`.

**Historical live side-by-side evidence (2026-07-20; stale for Plan 004
acceptance):** after verified SND-HOST identity,
the separately owned `F:\Wallpapers\webgallery_library.sqlite` was published as
schema 3 with 85,509 images, 29,716 tags, and 466,715 `image_tags` rows. An
exhaustive verification against `F:\Wallpapers\library` returned `ok=true` with
zero issues; the retained report is
`F:\Wallpapers\reports\maintenance-webgallery-20260720T145433Z\verify.json`.
At that observation, the sibling-maintained
`F:\Wallpapers\wallpaper_library.sqlite` was schema 2. Warm 48-item API requests
averaged about 56.05 ms; the path-free JSON response was 84,316 bytes. No
current canonical/schema/listener claim is inferred from this snapshot.

**Remaining work:** the dated schema-3 publication above completed its own
historical run. Current schema-4 publication is not complete: after repository
verification, Plan 004 still needs a fresh manifest-bound candidate and
exhaustive zero-issue report, alternate-listener QA, separately authorized
publication, and separately owned cutover. The sibling database must not be
migrated in place. Deep keyset pagination remains a measured follow-up; this
campaign retains bounded offset pagination.

**Historical aggregate copy-migration check (2026-07-20; stale for Plan 004
acceptance, verified SND-HOST identity, read-only on the observed database and
library):** a disposable
`sqlite3.Connection.backup()` copy of the live
`F:\Wallpapers\wallpaper_library.sqlite` (schema 2, 85,509 images,
29,716 tags, 466,715 `image_tags` rows) was migrated in-process to schema 3 via
this module's own `connect()`. Pre/post aggregate counts matched for `images`,
`tags`, `image_tags`, source/orientation distributions, and null SHA values;
sampled paths also matched. `content_rating` differs from `purity` only because
it is a newly materialized derivation, not a copy.
`--verify-json` against the migrated copy and the real `F:\Wallpapers\library`
returned `"ok": true, "status": "ok"`, zero issues, 85,509 disk images against
85,509 indexed images, zero missing/unindexed/mismatched paths, zero duplicate
SHA groups, zero schema/facet/suggestion failures. The live `.sqlite` file was
only ever opened `mode=ro`; all writes landed in a session scratch copy, which
was not published anywhere during that earlier check. That check provided
strong migration evidence on a copy but was not a row-by-row parity artifact.
The later identity-gated publication and exhaustive verification of the
separately owned schema-3 gallery database are recorded above; neither event
changed the sibling schema-2 database or constituted the `8090` server cutover.

This run also surfaced and fixed a documentation hazard: see the
module-resolution warning added to `docs/INDEX_LIBRARY.md`. This worktree's
own `.venv` correctly resolves `dl_engine` via its editable install; the
*global* interpreter instead resolves `dl_engine` to a separate, actively
developed sibling project at `F:\Wallpapers\dl-engine` (this gallery's
`dl_engine` package was originally derived from it) that shares the same
top-level import name and maintained the sibling database's observed schema 2.
The first verification attempt in this session used the global interpreter by
mistake and got a false `schema-version-mismatch` failure from that sibling
project's code before this was diagnosed and corrected to use this worktree's
own `.venv`. That sibling also owns the live schema-2 database, which is why
gallery publication must use the separate schema-4 candidate and canonical path
defined by Plan 004, never the sibling database.

## 4. Gallery discovery and presentation controls

**Goal:** turn indexed metadata into an accessible browsing workflow without
losing existing rating separation, NSFW reveal, infinite scroll, selection,
transfer, missing-path, or verification behavior.

**Delivered artifacts:** `reports/library-browser.html` now has clickable,
typed provider-tag chips; counted and cancellable ARIA combobox autocomplete;
visible removable filter chips; named URL presets; positive seeded shuffle;
compact, comfortable, and cinematic density; contain/crop presentation; and a
keyboard-accessible detail dialog. The dialog preserves grid state, groups all
provider tags, shows rating and file metadata, returns focus, navigates loaded
items, and loads originals only on demand. `tests/test_gallery_browser_contract.py`
parses the source contract and runs `node --check` when Node is available.
Deterministic CSS Grid reading order is retained; masonry is deferred because
it would complicate keyboard and incremental-append order.

**Verification:** `.venv\Scripts\python.exe -m pytest -q tests/test_gallery_browser_contract.py
tests/test_library_browser.py tests/test_dashboard_server.py
tests/test_gallery_thumbnails.py`, then the full
`.venv\Scripts\python.exe -m pytest -q`. A live
browser smoke should cover autocomplete keys, each preset and reload, two
shuffle pages with one seed, dialog focus/arrows/Escape, density/fit, NSFW
blur/reveal, selection, and a non-sending transfer-status path.

**Rollback:** restore the previous `reports/library-browser.html`. URL views are
ordinary filters and create no collection data to undo; density/crop never
changes files.

**Status:** `implemented`.

**Historical live side-by-side evidence (2026-07-20; stale for Plan 004
acceptance):** the identity-gated browser smoke passed counted autocomplete
with ArrowUp/ArrowDown wrap, all six named presets plus query-state reload, and
two seeded-shuffle pages containing 96 unique cards with no page overlap. It
also passed dialog focus, arrow navigation, Escape, and focus return;
density/fit changes; NSFW blur/reveal; and a one-item selection whose Send
action became enabled but was never clicked. At a 390×844 viewport the page had
no horizontal overflow. The final pass reported zero browser errors.

The smoke exposed and drove fixes for query-bearing reloads, initial ArrowUp
selection, a post-dialog rating-tab selector crash, and the valid JPEG/MPO
thumbnail case. After those fixes, the full suite passed 196 tests plus 61
subtests on Python 3.14.6.

**Remaining work:** the dated alternate-listener browser and visual/responsive
gate completed for that pass. Plan 004 requires a new isolated candidate pass;
repeat the final smoke after any separately authorized live cutover. No
transfer or suggestion review was submitted in the historical run.

## 5. Provider enrichment priority

**Goal:** improve sparse metadata from durable provider evidence before using
visual inference, with resumable attempts and no skipped pending Wallhaven rows.

**Delivered artifacts:** Wallhaven pending selection no longer treats the
lexicographic progress cursor as an exclusion boundary. Attempts and terminal
outcomes are appended and flushed to the durable ledger before SQLite progress
advances. The generic v1 provider ledger imports captured Zerochan and
Anime-Pictures typed evidence by stable source identity and exact provenance,
without scraping, inventing labels, rewriting sidecars, or replacing unrelated
evidence. Provider-coverage facets measure progress.

**Verification:** run the isolated ledger/enrichment tests in
`tests/test_index_library.py`; then, after verified machine identity and an
offline copy/migration check, run at most a one-attempt Wallhaven canary and
verify durable resume plus `--verify-json`. Refresh coverage read-only before
reporting current backlog counts.

**Rollback:** stop enrichment, retain both append-only ledgers, restore prior
code, and rebuild the disposable index. Never roll back by deleting provider
evidence or changing image sidecars.

**Status:** `implemented` (infrastructure only; backlog completion is not
claimed).

**Historical backlog snapshot:** a read-only refresh at
**2026-07-20T02:40:25Z** found
**11,923 pending Wallhaven rows**; **28,310 Zerochan rows** in `ok` status with
an average tag count of **1.000** (28,309 one-tag rows and one zero-tag row); and
**21,083 Anime-Pictures rows** in `ok` status. These are dated snapshot counts,
not current or completed enrichment claims. The download queue observed in that
run blocked the live Wallhaven canary, so no provider attempt was made. Recheck
the queue and counts before any bounded canary, then begin only an explicitly
authorized resumable campaign. Capture legitimate Zerochan evidence before
importing it. A one-item canary does not complete either backlog.

## 6. Reviewable visual-tag suggestions

**Goal:** keep automated labels confidence-scored, provenance-bearing, and
reviewable without allowing them to masquerade as provider tags or safety
evidence.

**Delivered artifacts:** current schema 4 retains the suggestion label,
normalized label,
confidence, generator, model version, provenance, timestamps, review status,
reviewer, and decision note. Only atomic pending-to-accepted or
pending-to-rejected transitions are allowed. The path-free API keeps suggestions
separate for every status. The gallery displays them in a labelled review
section and submits same-origin JSON decisions; it never adds them to provider
chips, autocomplete, tag counts, franchises, ratings, or facets. Accepted
suggestions remain suggestions.

**Verification:** `.venv\Scripts\python.exe -m pytest -q tests/test_index_library.py
tests/test_library_browser.py tests/test_gallery_browser_contract.py`. A live
review smoke requires verified SND-HOST identity because it writes SQLite;
confirm afterward that authoritative tags, tag count, materialized rating, and
facets are unchanged.

**Rollback:** disable the suggestion POST route and review controls while
retaining existing suggestion records and decisions for audit. Rebuildable
SQLite can be reconstructed from the chosen suggestion input/decision ledger
when that durable ingest is operationally adopted.

**Status:** `implemented`.

**Remaining work:** choose and document an authorized generator/ingest workflow,
run a labelled live review canary, and define maintenance/retention boundaries.
Automated generation itself and promotion to human manual tags are **Deferred**;
neither is implied by this review-only UI.

## Campaign gate

The identity-gated alternate-listener smoke, thumbnail-byte measurement, API
timing, schema-3 database verification, and browser interaction evidence from
2026-07-20 are retained historical results. They are stale for Plan 004 and do
not prove the current queue, database, listener, or browser state.

The current campaign state is `repository-verified`. Promotion still requires
a fresh manifest-bound `candidate-verified` result, full isolated
alternate-8091 browser evidence, explicit maintenance and cutover authority, a
`published` manifest, and a separately observed `cut-over` listener. Provider
and suggestion-review canaries remain separate authorizations. Until then, the
UI must display the applicable verification API result and never infer safety
or verification from missing evidence.

## Plan 004 schema-4 publication

The operator and implementation authority is
[`GALLERY_INDEX_PUBLICATION.md`](GALLERY_INDEX_PUBLICATION.md); the strict
manifest authority is
[`gallery-publication-manifest.schema.json`](../schemas/gallery-publication-manifest.schema.json).
The exact `Inspect -WhatIf`, `Prepare -Apply`, direct Python `validate`,
`Publish -Apply -CutoverAuthorized`, and `Recover -Apply` commands are recorded
with literal SND-HOST paths in
[`INDEX_LIBRARY.md`](INDEX_LIBRARY.md#plan-004-operator-workflow).

The canonical database is `F:\Wallpapers\webgallery_library.sqlite`; the
protected sibling is `F:\Wallpapers\wallpaper_library.sqlite`. Candidates,
manifests, backups, journals, and recovery results are unique descendants of
`F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication`; immutable
verification reports are descendants of
`F:\Wallpapers\reports\gallery-publication`.

- A candidate is a separate schema-4 SQLite file, not canonical publication.
- The manifest is the strict, compare-and-swap state/evidence chain and the
  authority for candidate/publication state.
- Each initial, fresh under-hold, failed-attempt, and canonical report is an
  immutable per-attempt artifact; a report alone grants no authority.
- The backup is the hashed exact pre-activation canonical main/WAL/SHM set and
  rollback basis, not a candidate or success marker.
- The append-only journal records activation/recovery intent and outcome; an
  exact recovery result may reconcile the manifest only through the documented
  compare-and-swap rule.

Current Plan 004 evidence is deliberately incomplete:

| Evidence surface | Current status |
|---|---|
| Implementation | `implemented`; merged. |
| Repository | `repository-verified`; compile, 148 tests plus 125 subtests targeted, 252 tests plus 186 subtests full, PowerShell parser/help, plan validation, and diff checks passed. |
| Candidate | No fresh post-merge candidate run. The retained 2026-07-21 artifact is `candidate-blocked` by 200 `layout-mismatch` issues and invalid report metadata. |
| Browser | No post-merge Plan 004 browser matrix. Older diagnostics are stale and non-promotional. |
| Publication | Not run; no `ready-to-publish` or `published` result claimed. |
| Cutover | Not run; no `cut-over` result claimed. |
| Recovery | Not run; no `rolled-back` result claimed. |
| External/live state | Queue, task, process, listener, and canonical identities were not re-inventoried in this docs pass. |

## Historical Plan 003 hardening snapshot (2026-07-20)

At this checkpoint, Plan 003 superseded Plan 002's repository-contract
descriptions without rewriting Plan 002's historical live evidence. It does not
supersede the Plan 004 publication section above. Repository code at the Plan
003 checkpoint targeted rebuildable gallery schema 4 and library response
schema 3. It materialized the validated NSFW subcategory and migrated/backfilled
schema-3 databases to schema 4 transactionally,
enforces the NSFW-only invariant, verifies derived and facet drift, and reads
each page from one SQLite snapshot. The browser consumes that contract through
an NSFW-only category control, URL state and details; it also adds pressed
rating state, concise announcements, consistent focus, reduced motion,
Back/Forward hydration, explicit full-resolution loading with stale-request
guards, and narrow-screen transfer/dialog behavior. Thumbnail interruption
cleanup now re-raises cancellation unchanged, removes only the operation's UUID
temporary file, publishes no final file, and retains ordinary generation versus
encoder diagnostics.

Direct regression coverage exists for every repository-side Plan 003 contract:

- `test_schema3_migration_backfills_subcategory_and_publishes_last`,
  `test_schema3_migration_rolls_back_subcategory_and_version`,
  `test_refresh_uses_authoritative_tags_and_nsfw_only_facets`, and
  `test_verifier_detects_subcategory_field_and_facet_drift` cover schema 4,
  rollback, authoritative evidence, NSFW-only facets, suggestion exclusion,
  and derived/facet drift.
- `test_query_page_pins_rows_tags_and_suggestions_to_one_wal_snapshot`, the
  response-schema/filter tests, and the exact server allowlist tests cover the
  one-response snapshot, response schema 3, legacy query compatibility, and
  HTTP forwarding/denial boundaries.
- The static browser suite directly covers subcategory wiring, accessibility,
  opt-in originals, stale-load guards, history without ephemeral selection or
  reveal resurrection, responsive controls, autocomplete, presets, pagination,
  dialog keyboard use, NSFW reveal, and selection safety.
- `test_interruption_re_raises_and_cleans_only_its_temporary_file` covers both
  `KeyboardInterrupt` and `SystemExit`; the adjacent ordinary-failure regression
  proves cancellation is not collapsed into `ThumbnailError` and the earlier
  diagnostic branches remain distinct.

### Historical repository verification

Python 3.14.6 in the project venv produced the following Plan 003 results on
2026-07-20. They do not establish Plan 004 `repository-verified` state:

| Command | Result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -q tests/test_gallery_thumbnails.py` | 14 passed, 11 subtests passed in 0.38 s |
| `.\.venv\Scripts\python.exe -m compileall -q src reports tests` | passed |
| focused Plan 003 pytest command | 192 passed, 65 subtests passed in 13.11 s |
| `.\.venv\Scripts\python.exe -m pytest -q` | 217 passed, 65 subtests passed in 13.23 s |
| `git diff --check` | passed; only checkout-dependent LF-to-CRLF warnings were emitted |

Pytest also emitted the existing non-fatal `.pytest_cache` WinError 183 warning;
there were no skipped checks or test failures.

### Historical disposable HTTP evidence

After a fresh `VERIFIED` `snd-host` / `SND-HOST` identity result, an ephemeral
loopback listener on port 61923 (PID 40112) used only a temporary schema-4
database, library, cache, environment, queue, and report fixture while serving
the then-current `F:\Wallpapers\webgallery\reports\library-browser.html`. The listener
stopped and the entire fixture was removed after these probes:

| Probe | Result |
|---|---|
| gallery HTML | 200, 90,085 bytes, 1.889 ms |
| explicit `rating=nsfw&nsfw_subcategory=explicit` page | 200, schema 3, 1 item, 1,389 bytes, 4.650 ms |
| legacy NSFW page without subcategory | 200, schema 3, null subcategory filter, 1 item, 1,383 bytes, 9.624 ms |
| facets | 200, 568 bytes, 14.173 ms; constant shape was explicit 1 and the other three values 0 |
| thumbnail / explicit original | 200 WebP, 440 bytes, 57.320 ms / 200 JPEG, 7,240 bytes, 28.291 ms |
| invalid SFW plus explicit subcategory | 400, 60 bytes, 21.723 ms |
| database, queue, environment, source, backup, and arbitrary paths | all 404; HEAD timings 0.669-14.781 ms |
| transfer status | 200, 132 bytes, 5.346 ms, `enabled=false`, zero targets |

### Historical live-browser gate

The browser controller was available and Chrome loaded the exact Plan 003 page
from the explicit-root `127.0.0.1:8091` listener. At that 2026-07-20
observation, `F:\Wallpapers\webgallery_library.sqlite` was schema 3 while the
reader required schema 4. `/api/library/status` therefore returned 503,
the initial SFW page returned 400, and the visible feed reported `index schema
version 3 is not 4` with zero cards. Browser console warning/error collection
was empty, but the visible API/page failure is a hard prerequisite blocker.

WPI was not authorized to migrate or publish that observed database, so it did not
claim subcategory, Back/Forward, opt-in-original, rapid navigation, focus,
reduced-motion, 200% zoom, 320/390/768 CSS-pixel, narrow-landscape, modal-scroll,
transfer-occlusion, autocomplete, preset, pagination, dialog, NSFW reveal, or
selection browser coverage. The WPI launcher and listener PIDs 4060 and 41964
were stopped, port 8091 was released, temporary logs were removed, and legacy
8090 was untouched on the then-observed PID 39260. Those PIDs are stale
diagnostic identities, not a current process inventory. No Send action,
suggestion review, provider canary, database rebuild/publication, queue
mutation, or canonical media mutation occurred.

### Recovery, promotion, and follow-on work

The 2026-07-20 record described non-clearing stash
`2cdda350b87af5e5aec9a19a2ccc422e56a72c12` as untouched recovery evidence for
the original content-rating and thumbnail hunks, not a release artifact. This
docs pass did not recheck it. At that runtime snapshot, the sibling
`F:\Wallpapers\wallpaper_library.sqlite` was schema 2.

Repository verification is complete. The immediate next gate is a fresh unique
candidate and manifest, followed by exhaustive zero-issue candidate
verification and the isolated alternate-listener browser matrix. Publication
requires a separate external maintenance/cutover authority after those gates;
deliberate 8090 cutover, the bounded provider canary, live suggestion-review
canary, and promotion-boundary verification remain separate and incomplete.

Follow-on campaigns remain explicit: typed multi-tag identity; a durable
suggestion input/decision ledger with replay; materialized provider
coverage/generation; cursor pagination with bounded DOM retention; multiple
tag-evidence provenances; and selection of a retained repository-level browser
automation gate. Until that last selection, retain the controlled browser
smoke as a required operational gate rather than treating the historical pass
as current evidence. Masonry, embeddings/semantic similarity,
a framework rewrite, offline/service-worker support, and shareable detail URLs
remain deferred unless their prerequisites change.
