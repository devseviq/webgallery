# Discovery — Sorting and Tag Pipeline

**Goal:** Review how completed downloads from Wallhaven, Zerochan, and Anime-Pictures enter the sorted library, determine which source/tag metadata survives, and define a safe boundary for the separate Anime-Pictures browser downloader.
**Date:** 2026-07-15
**Status:** complete
**Recommended next:** Locate or restore the canonical sorted library first, then repair recursive/stale-path indexing and orientation-sort idempotency before using the SQLite tags for organization. Preserve source metadata at the staging-to-library handoff; keep tags as searchable many-to-many metadata rather than physical folders.

---

## Questions

1. How do normal downloads, Anime-Pictures previews, and browser originals enter sorting?
2. Which source IDs, URLs, search terms, and tags survive each handoff?
3. What rules currently determine resolution, orientation, destination, and duplicate handling?
4. Does the SQLite index remain accurate after files move, and can its tags be searched reliably?
5. Where should tag-aware organization live without merging queue-browser into the scheduled downloader?

---

## Executive Finding

The intended boundary is right:

```text
scheduled Anime-Pictures scraper
    -> preview AVIF + lossless .url sidecar
    -> queue-browser retrieves the authenticated original
    -> anime-pictures-full
    -> common resolution sorter
    -> library index / optional orientation layout
```

`anime-pictures` is protected as a live queue, while `anime-pictures-full` is deliberately sortable. Queue-browser is a serial, visible-browser pipeline and should stay separate from the scheduled gallery downloader. Its current saved inter-item delay is 9 seconds. It is not, however, represented in the scheduled worker's per-site slot accounting, so simultaneous preview scraping and browser-original retrieval can add Anime-Pictures traffic independently.

The sorting/tagging system is not ready to organize the current backlog safely:

- The default `images` library does not exist. `WALLPAPERSSORTED` exists but is empty and is not referenced by the tools. Both default sort commands fail before planning work.
- The SQLite index contains 14,339 rows, but all 14,339 paths point into the absent library. It is wholly stale as a physical manifest.
- Resolution sorting is solid for dimensions and exact duplicates, but it flattens staging directories. That destroys Zerochan source/search context before the index sees it.
- Orientation re-indexing is non-recursive, never removes stale paths, and currently passes an idempotence test for the wrong reason: it sees no moved files.
- Even after recursive indexing is repaired, the orientation sorter would rename an already-correct file to `_0001` because it resolves collisions before testing source/destination equality.
- Tag coverage in the stale database looks strong for old Wallhaven and Anime-Pictures data, but Anime-Pictures tag IDs are process-randomized and have already produced 681 orphaned duplicate tag rows.

The right design is one canonical physical layout by resolution/orientation, with source identity and tags stored durably in SQLite (or a source manifest joined into SQLite). Tag folders would force one many-to-many image into a single taxonomy and should not be the canonical storage layout.

---

## Live Baseline

Snapshot taken on SND-HOST on 2026-07-15:

| Surface | Observation |
|---|---:|
| `temp_downloads\wallhaven` | 21,519 images |
| `temp_downloads\zerochan` | 2,783 images plus one `.part` |
| `temp_downloads\anime-pictures` | 8,555 preview images, 8,753 `.url` sidecars, one queue JSONL |
| `temp_downloads\anime-pictures-full` | 198 normally named images plus five extensionless image binaries |
| Queue-browser manifest | 931 completed records; 198 recorded download paths still exist at their logged locations |
| Default library | `D:\wallpapers\images` / `F:\Wallpapers\images` absent |
| Alternative-looking folder | `F:\Wallpapers\WALLPAPERSSORTED` exists but is empty and unused by code/docs |
| SQLite images | 14,339 total: 11,616 Wallhaven, 2,399 unknown, 324 Anime-Pictures |
| SQLite tag coverage | 11,615 Wallhaven and 324 Anime-Pictures images linked to tags; no unknown images linked |
| SQLite path validity | 0 of 14,339 indexed paths exist |
| SQLite tag hygiene | 681 orphaned tag rows and 681 duplicate lowercase tag names |

The database passed `PRAGMA quick_check`; the problem is semantic staleness rather than SQLite corruption.

---

## Findings

### Q1: How do files enter sorting?

**Answer:** Normal Wallhaven and Zerochan images are staged under site/search directories. Anime-Pictures previews and sidecars remain in a protected queue; only completed browser originals are admitted to the common sorter.

**Evidence:**

- `src/dl_engine/anime_pictures.py:349-355` names previews `ap-<post_id>-<WxH>.avif`.
- `src/dl_engine/anime_pictures.py:489-510` writes the complete original download URL beside the preview as `.url`.
- `F:/Wallpapers/Start-QueueBrowser.ps1:20-23,33-44` fixes the queue and original-output roots, then delegates to the canonical queue-browser launcher.
- `D:/Development/DesktopApps/queue-browser/scripts/Start-QueueBrowser.ps1:122-175` applies those roots to the app settings.
- `D:/Development/DesktopApps/queue-browser/src/main/queue/runner.ts:79-85,255-257` processes originals serially with an inter-item delay; the current runtime setting is 9,000 ms.
- `scripts/sort-downloads.ps1:82-94,187-217` excludes `anime-pictures`, processed/failed/archive/quarantine trees, and other protected names.
- `scripts/sort-downloads.ps1:615-626` also detects any directory containing `queue-browser.jsonl` and excludes that entire queue-owned subtree.
- `scripts/sort-downloads.ps1:91,441-472` includes `anime-pictures-full` and gives it keeper preference during source/source duplicate selection.

**Implications:**

- The queue page and its previews remain available, as required.
- Browser-original retrieval remains a different transport with its own state and cadence.
- The common sorter sees only completed originals, except for five unfinalized GUID files discussed below.
- The scheduled worker's active-process detection covers its Python/gallery-dl downloaders but not queue-browser (`scripts/wallpaper-download-queue.ps1:1114-1140`). Aggregate Anime-Pictures pacing is therefore not coordinated across the two systems.

### Q2: Which source and tag data survives?

**Answer:** Wallhaven identity survives well and tags can be enriched later. Anime-Pictures has a lossless URL manifest but currently indexes a lossy filename reconstruction. Zerochan loses the most: its numeric filename survives, but source and search/tag directory context do not.

#### Wallhaven

- Filenames retain site ID and dimensions; `src/dl_engine/index_library.py:199-256` recognizes them.
- Tags require a later API pass; `src/dl_engine/index_library.py:1021-1069` writes API metadata.
- Franchise is heuristic: the first tag in selected categories becomes one scalar franchise (`index_library.py:127-130,1050-1051`).

#### Anime-Pictures

- Preview names intentionally contain only ID and dimensions and do not match the tag-bearing Anime-Pictures index regex (`anime_pictures.py:349-355`; `index_library.py:205-210`). They would be `unknown` if indexed, but the preview tree is correctly excluded.
- Normal browser-original filenames contain ID, dimensions, and a long tag suffix and match `index_library.py:205-266`.
- The raw `.url`/JSONL URL is more faithful than the filename. Queue-browser sanitizes filesystem-invalid characters, so a URL tag such as a percent-encoded colon can become `_` on disk. The JSONL record preserves both values (`queue-browser/src/main/queue/log.ts:4-20`; live `queue-browser.jsonl`).
- The parser takes the first hyphen-delimited segment as franchise and every remaining segment as an untyped tag; character/artist/attribute types are not recoverable and `characters` is always empty (`index_library.py:278-339`). Literal hyphens and multi-franchise images are ambiguous.
- Browser ` (1)` or sorter `_0001` collision suffixes can become part of the last parsed tag.

#### Zerochan

- Live Zerochan staging uses bare numeric filenames under meaningful query/search directories.
- `index_library.py:213-275` explicitly classifies bare numeric names as `unknown` even though it retains the numeric ID.
- `scripts/sort-downloads.ps1:666-708` emits only the original filename into a resolution bucket, flattening away the `zerochan\<search>` parents.
- Unknown rows are marked `skipped` and never enriched (`index_library.py:631-635`).

**Implications:**

- Metadata capture must happen before or during the first physical sort, not after directory flattening.
- The minimum durable provenance is: source site, source-site ID, source URL, staging search/collection, downloader transport, and the raw source tag payload or URL.
- Queue-browser already supplies enough Anime-Pictures identity in its processed sidecars/JSONL; the index simply does not consume it.
- Zerochan needs an explicit source classifier/manifest before its current folders are flattened.

### Q3: How does physical sorting work?

**Answer:** The first stage is dimension/deduplication sorting. The second is an index-driven orientation move. Tags do not affect either destination.

#### Resolution stage

- `scripts/sort-downloads.ps1:372-419` reads image dimensions through Windows Shell metadata and then `System.Drawing`; unreadable images go to `_UnknownResolution`.
- `scripts/sort-downloads.ps1:119-126,421-438` buckets by the longer side: 4K >= 3840, 1440p >= 2560, 1080p >= 1920, 720p >= 1280, else SD.
- `scripts/sort-downloads.ps1:502-596` gates exact duplicates by length and SHA-256, with optional byte comparison.
- Existing library copies win; among staged copies, `anime-pictures-full`, clean names, shorter names, and lexical order establish keeper preference (`sort-downloads.ps1:441-472`).
- Duplicates move to `_ExactDuplicates`; they are not deleted.
- `sort-downloads.ps1:693-708` preserves the basename but flattens source folders into `<library>\<bucket>`.

#### Orientation stage

- `index_library.py:346-383` derives orientation and mirrors the resolution buckets; ratios within 3% of square are `square`.
- `index_library.py:770-793` plans known Wallhaven/Anime-Pictures files under `<bucket>/<orientation>` and all unknowns under `_UnknownSource/<orientation>`.
- Unknown/Zerochan files therefore lose even their physical resolution bucket in the orientation layout.
- `index_library.py:825-838` puts only path, source, bucket, and orientation in the move plan. Tags, franchise, purity, and search provenance are absent.

#### Duplicate/collision gaps

- The destination duplicate scan covers the five resolution buckets and `_UnknownResolution`, but not `_UnknownSource` (`sort-downloads.ps1:92-94,640-652`). After orientation sorting, later duplicate Zerochan/unknown downloads are no longer checked against their canonical copies.
- Sorter collision suffixes can break the anchored Wallhaven classifier or contaminate the final Anime-Pictures tag (`sort-downloads.ps1:220-247`; `index_library.py:199-210`).
- Five live browser-original files have no extension even though magic bytes show two JPEGs and three PNGs. The sorter accepts only named extensions (`sort-downloads.ps1:108-117,620-622`), so these are invisible. Queue-browser's Chrome path uses GUID working names before finalizing to the suggested name (`queue-browser/src/main/chrome/realChrome.ts:724-753,852-908`); none of the five has a JSONL record, so they should be reconciled or quarantined rather than guessed.

### Q4: Is the SQLite tag index reliable after sorting?

**Answer:** No. Its schema can answer basic exact-match queries, but path lifecycle and Anime-Pictures tag identity are currently incorrect.

#### Stale paths and false idempotency

- `index_library.py:603-626` scans only immediate files inside each resolution bucket.
- Orientation sorting moves known files into nested bucket/orientation folders and unknown files under `_UnknownSource/orientation` (`index_library.py:770-793`). The re-ingest cannot see either layout correctly.
- Ingest upserts by path but never removes missing rows (`index_library.py:646-681`).
- Plan emission silently skips missing paths (`index_library.py:825-840`).
- `tests/powershell/SortByOrientation.Tests.ps1:119-143` accepts the resulting empty plan as proof of idempotency without asserting that database paths point at the moved files.
- If recursive indexing were fixed alone, the PowerShell sorter would still mishandle an already-correct file: it calls the collision resolver first, sees the existing target, selects `_0001`, and only then tests path equality (`scripts/sort-by-orientation.ps1:97-123,223-231`).

The live database confirms the failure mode: every one of its 14,339 paths is missing.

#### Tag identity and query limitations

- `index_library.py:685-709` claims to synthesize stable negative Anime-Pictures tag IDs but uses Python's process-randomized `hash()`. Two fresh processes produced different IDs for the same tag during this review.
- The live database has 1,362 negative tag rows; only 681 are linked. The other 681 are orphaned duplicates from another process seed.
- Re-ingest deliberately omits `franchise` from the upsert (`index_library.py:653-667`). That preserves enriched Wallhaven rows but also prevents corrected Anime-Pictures parsing from updating a prior franchise.
- The schema stores one scalar franchise and untyped tags, without tag provenance/confidence/source (`index_library.py:150-179`).
- `index_library.py:712-763` supports one exact tag and one exact franchise filter. `ImageRow` and CLI output do not return the tag set (`index_library.py:540-574,1134-1147`).

**Implications:**

- Do not drive tag organization or another physical move from the current database.
- Rebuild/migrate the index only after the canonical library location and path-update rules are settled.
- Use deterministic, source-qualified tag identities and reconcile stale rows transactionally.
- Search/view generation is a better consumer for tags than another folder move.

### Q5: Where should tag-aware organization live?

**Answer:** At the metadata/query layer, downstream of a durable source manifest and upstream of optional views—not in queue-browser and not as canonical tag folders.

Recommended contract:

```text
site downloader
    -> staged image + source manifest
Anime-Pictures preview/sidecar
    -> queue-browser original + processed sidecar + JSONL
all completed originals
    -> resolution/dedupe sorter, recording source -> destination
canonical physical library
    -> reconciled SQLite index keyed by source + source-site ID + current path
    -> tag/franchise queries, saved collections, playlists/manifests, or optional links
```

This preserves the browser boundary and allows one image to participate in many tags/franchises without duplicating or arbitrarily relocating the canonical file.

---

## Verification

- `python -m pytest -q tests/test_index_library.py`: **50 passed**.
- `tests/powershell/SortDownloads.Tests.ps1`: **passed**.
- `tests/powershell/SortByOrientation.Tests.ps1`: **passed**, but its idempotence assertion is a false positive for the stale-path reason above.
- Live SQLite `PRAGMA quick_check`: **ok**.
- Read-only live scans confirmed current staging counts, five extensionless image binaries, queue-browser JSONL shape, and 14,339 missing indexed paths.

Missing regression coverage includes recursive post-orientation re-index with current-path assertions, already-correct destination handling, stale-row reconciliation, deterministic tag IDs across processes, Zerochan provenance, browser/sorter filename suffixes, processed-sidecar URL joins, and multiple-tag query results.

---

## Risks

| Risk | Likelihood | Impact | Notes |
|---|---|---|---|
| Running either sorter with defaults is blocked | Certain/current | High | Default library is absent. |
| Choosing the wrong new library root scatters or duplicates files | Medium | High | `WALLPAPERSSORTED` looks intentional but has no code/config reference. User intent is required before moving anything. |
| Re-index claims success while all paths remain stale | Certain/current | High | Non-recursive scan plus no stale-row removal. |
| Correctly placed orientation files get `_0001` names after index repair | High | High | Equality check occurs after collision resolution. |
| Zerochan source/search metadata is permanently lost on flattening | Certain for current design | High | Numeric filename alone is classified unknown. |
| AP tag catalog grows duplicate/orphan rows across processes | Certain on re-ingest | Medium | Built-in hash is randomized per process. |
| Browser GUID originals never enter sorting | Current | Medium | Five valid images are extensionless and unlogged. |
| Scheduled AP scraping and queue-browser exceed an unknown aggregate allowance | Unknown | Medium | Separate pacing systems; no shared quota model. |

---

## Open Questions

- Is the intended canonical destination a restored `images` directory, the empty `WALLPAPERSSORTED` directory, or another external location?
- Were the former 14,339 library files intentionally moved off this volume, or is their absence unexpected?
- Which Zerochan staging parent names are true user tags/collections versus transient search labels?
- Should saved tag collections be manifests/playlists, filesystem links, or an application/query UI?
- Is there an authoritative private Anime-Pictures allowance that should coordinate scheduled preview requests with queue-browser originals?

---

## Optimization Register

| Candidate | Type | Evidence | Risk | Confidence | Decision |
|---|---|---|---|---|---|
| Resolve canonical library root before any Apply | correctness | Default root absent; all DB paths missing | Low | High | needs user decision |
| Make ingest recursive and reconcile stale/current paths | correctness | `index_library.py:603-681`; live stale DB | Medium | High | implement-now after root decision |
| Check same-path before collision allocation | correctness | `sort-by-orientation.ps1:97-123,223-231` | Low | High | implement-now |
| Preserve source manifest through resolution sort | structural | Zerochan parent context lost at `sort-downloads.ps1:693-708` | Medium | High | implement-now |
| Join Anime-Pictures metadata from raw URL by post ID | data quality | Sidecar/JSONL is lossless; filename is sanitized | Medium | High | implement after path fixes |
| Replace process hash with deterministic source-qualified tag identity | data correctness | `index_library.py:685-709`; 681 live orphans | Medium/migration | High | implement after schema plan |
| Keep one canonical physical layout; build tag views | architecture | Tags/franchises are many-to-many | Low | High | retain |
| Reconcile five GUID binaries | recovery | Valid JPEG/PNG magic; no JSONL mapping | Medium | High | needs careful recovery |
| Coordinate queue-browser with scheduled AP quota | policy | Separate pacing; no public numeric allowance established | Medium | Medium | suggest-only |

### Verification Gate

The code corrections are well-supported, but physical application is blocked on identifying the canonical library root and the status of the former library files. No download, sort, rename, database migration, or queue-browser state was changed during this review.

---

## Recommendation

Treat this as three ordered pieces:

1. **Recover the storage contract:** identify the real canonical library and make every default/config point to it.
2. **Repair correctness:** recursive/reconciling index ingest, same-path orientation handling, duplicate coverage after orientation layout, durable source provenance, and extensionless-browser recovery.
3. **Build tag organization:** deterministic source-qualified tags, raw Anime-Pictures URL joins, explicit Zerochan provenance, richer multi-tag queries, then saved collections/views rather than canonical tag folders.

The browser downloader does not need to be merged into the scheduled queue to achieve this. It only needs to emit/retain a reliable manifest that the common metadata index consumes after the original completes.
