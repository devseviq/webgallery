# Web Gallery Roadmap

This is the durable implementation and operations record for the six approved
gallery improvements. The gallery is a view and curation surface over a
rebuildable SQLite index. It never moves, rewrites, or physically groups
canonical wallpapers or their sidecars.

## Status language

- **Planned** — specified but not implemented.
- **Implemented** — repository code and tests exist, but the live campaign gates
  have not all run.
- **Working** — a live smoke passed, while the maintenance boundary remains
  unverified.
- **Verified** — all campaign exit checks and current index verification passed.
- **Deferred** — explicitly outside this campaign.

The repository is currently **Implemented**. Static and synthetic integration
checks pass, but this run did not start or restart a listener, create live
thumbnails, migrate the live database, contact a provider, review a live
suggestion, or run browser automation. No item is labelled Working or Verified
from unit tests alone.

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

**Verification:** `python -m pytest -q tests/test_dashboard_server.py`, then—only
after verified SND-HOST identity—start an alternate loopback listener and probe
the allowlist plus known denied queue/config/database paths without reading
sensitive response bodies.

**Rollback:** stop the replacement listener, restore the previous server and
report source, and restart the previous listener only after the machine identity
gate. Canonical images and SQLite evidence are not part of this rollback.

**Status:** **Implemented**.

**Remaining work:** perform the identity-gated alternate-listener HTTP smoke,
then the deliberate live cutover. Do not infer a runtime root from this Git
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

**Verification:** `python -m pytest -q tests/test_gallery_thumbnails.py
tests/test_dashboard_server.py tests/test_gallery_browser_contract.py`. After
the identity gate, measure the summed response bytes for one cold and warm
48-card thumbnail page separately from one explicitly opened original.

**Rollback:** stop the server, disable thumbnail routes/client use, and remove
only the explicitly configured derived thumbnail-cache root. Never delete from
the canonical library.

**Status:** **Implemented**.

**Remaining work:** run the live cold/warm cache and byte comparison. The
218.5 MiB figure is a baseline, not a claim about the unmeasured new page.

## 3. Batched and materialized gallery API

**Goal:** remove per-card tag queries and repeated rating/facet aggregation,
while adding stable triage and shuffle queries without exposing local paths.

**Delivered artifacts:** schema 3 in `src/dl_engine/index_library.py`
materializes content rating, confidence, basis, reasons, tag count, and global
facets. Page reads use one image query, one batch provider-tag hydration query,
and one batch suggestion query. Stable indexes and the `least_tagged`,
`rating_confidence`, and seeded `shuffle` sorts support discovery and triage.
`src/dl_engine/library_browser.py` serializes typed/provenanced tags, counted
autocomplete, provider coverage, path-free thumbnail/original identities, and a
separate suggestion collection. Migration, query-shape, WAL freshness, API, and
rating boundaries are covered by index/browser/content-rating tests.

**Verification:** `python -m pytest -q tests/test_index_library.py
tests/test_library_browser.py tests/test_content_rating.py`, followed by
`python -m dl_engine.index_library --verify-json --library-root <explicit-root>
--db-path <explicit-db>` against the current live snapshot after the identity
gate for any migration or rebuild. Record warm API timings separately from the
older roughly two-second discovery measurements.

**Rollback:** retain canonical media, sidecars, and provider ledgers; stop
writers; discard only the rebuildable schema-3 database and its stopped WAL/SHM
siblings; restore prior code and rebuild the earlier schema from durable
evidence.

**Status:** **Implemented**.

**Remaining work:** migrate or rebuild a copy, prove parity, then run the
identity-gated live publication and current verification. Deep keyset
pagination remains a measured follow-up; this campaign retains bounded offset
pagination.

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

**Verification:** `python -m pytest -q tests/test_gallery_browser_contract.py
tests/test_library_browser.py tests/test_dashboard_server.py
tests/test_gallery_thumbnails.py`, then the full `python -m pytest -q`. A live
browser smoke should cover autocomplete keys, each preset and reload, two
shuffle pages with one seed, dialog focus/arrows/Escape, density/fit, NSFW
blur/reveal, selection, and a non-sending transfer-status path.

**Rollback:** restore the previous `reports/library-browser.html`. URL views are
ordinary filters and create no collection data to undo; density/crop never
changes files.

**Status:** **Implemented**.

**Remaining work:** run the browser smoke and visual/responsive inspection
against an identity-gated alternate listener. No transfer should be sent as part
of that smoke.

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

**Status:** **Implemented** (infrastructure only; backlog completion is not
claimed).

**Remaining work:** a read-only refresh at **2026-07-20T02:40:25Z** found
**11,923 pending Wallhaven rows**; **28,310 Zerochan rows** in `ok` status with
an average tag count of **1.000** (28,309 one-tag rows and one zero-tag row); and
**21,083 Anime-Pictures rows** in `ok` status. These are current snapshot counts,
not completed enrichment claims. The active download queue blocked the live
Wallhaven canary in this run, so no provider attempt was made. Run the bounded
canary only after that gate clears, then begin an explicitly authorized
resumable campaign. Capture legitimate Zerochan evidence before importing it.
A one-item canary does not complete either backlog.

## 6. Reviewable visual-tag suggestions

**Goal:** keep automated labels confidence-scored, provenance-bearing, and
reviewable without allowing them to masquerade as provider tags or safety
evidence.

**Delivered artifacts:** schema 3 stores suggestion label, normalized label,
confidence, generator, model version, provenance, timestamps, review status,
reviewer, and decision note. Only atomic pending-to-accepted or
pending-to-rejected transitions are allowed. The path-free API keeps suggestions
separate for every status. The gallery displays them in a labelled review
section and submits same-origin JSON decisions; it never adds them to provider
chips, autocomplete, tag counts, franchises, ratings, or facets. Accepted
suggestions remain suggestions.

**Verification:** `python -m pytest -q tests/test_index_library.py
tests/test_library_browser.py tests/test_gallery_browser_contract.py`. A live
review smoke requires verified SND-HOST identity because it writes SQLite;
confirm afterward that authoritative tags, tag count, materialized rating, and
facets are unchanged.

**Rollback:** disable the suggestion POST route and review controls while
retaining existing suggestion records and decisions for audit. Rebuildable
SQLite can be reconstructed from the chosen suggestion input/decision ledger
when that durable ingest is operationally adopted.

**Status:** **Implemented**.

**Remaining work:** choose and document an authorized generator/ingest workflow,
run a labelled live review canary, and define maintenance/retention boundaries.
Automated generation itself and promotion to human manual tags are **Deferred**;
neither is implied by this review-only UI.

## Campaign gate

Promotion from Implemented to Working requires the identity-gated alternate
listener smoke, live thumbnail-byte measurement, current API timing, a bounded
database/enrichment canary where authorized, and the browser interaction smoke.
Promotion to Verified additionally requires current `--verify-json` success and
every campaign exit check. Until then, the UI must continue to display the live
verification API result and must never infer safety or verification from missing
evidence.
