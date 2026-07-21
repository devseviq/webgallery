# Content Rating Collections

The library browser separates indexed pictures into four derived collections
without moving or copying canonical image files:

| Collection | Meaning |
|---|---|
| `sfw` | The source explicitly marked the image safe and no more-restrictive tag evidence exists. |
| `suggestive` | The source marked it sketchy/questionable, or a conservative suggestive tag matched. |
| `nsfw` | The source marked it NSFW, or an explicit adult tag matched. |
| `unknown` | No reliable rating signal exists. Unknown is never treated as SFW. |

### NSFW subcategory (second axis)

The single `nsfw` collection is split into a derived `nsfw_subcategory` so the
gallery no longer lumps every adult image together. It is only meaningful when
the overall rating is `nsfw`; everything else reports `unspecified`.

| Subcategory | Meaning |
|---|---|
| `nudity` | Bare bodies without a depicted sex act (solo / artistic / glamour nudity). |
| `explicit` | Depicted sexual acts, or the umbrella drawn-explicit term (`hentai`). |
| `fetish` | BDSM / bondage / latex / domination and other kink themes. |
| `unspecified` | Provider-marked NSFW with no matching finer tag. This is the triage bucket — exactly the images that used to be invisible inside the lump. |

Precedence is `explicit` > `fetish` > `nudity` > `unspecified`. Schema 4
materializes the split in `images.nsfw_subcategory` from purity plus
authoritative `tags`/`image_tags`, in the same transaction as the overall
rating and facet snapshot. Review-only tag suggestions are excluded. The
field is always `unspecified` for non-NSFW rows; no canonical file moves or
sidecar changes are involved. Term lists live in
`src/dl_engine/content_rating.py`.

The implementation is deterministic and explainable. Each indexed result
materializes its rating, subcategory, confidence, basis, and stable JSON
reasons. Source purity is authoritative, but explicit provider-tag evidence may
make an apparently safe row more restrictive. Tags can never infer SFW from
silence. The classifier authority remains
`src/dl_engine/content_rating.py`; schema 4 stores its output but does not
duplicate or replace its normalized whole-token rules.

Materialized fields are refreshed after rebuild/ingest, supported schema
migration, and authoritative provider-tag changes. The same transaction
refreshes `tag_count` and global facets. Rebuilding from sidecars and provider
ledgers recomputes them, so SQLite remains a cache rather than rating evidence.

Visual-model output is stored only in `tag_suggestions`. Pending, accepted,
and rejected suggestions are excluded from provider tags, raw tag counts,
autocomplete, content ratings, subcategories, purity, and franchise
derivation. A reviewer decision records curation state; it does not turn a
model label into provider evidence.

## Browser

Start an alternate loopback listener with this worktree's venv and every
runtime root explicit:

```powershell
$identity = & 'C:\Users\Dev\OneDrive\common\common_dev\Get-VerifiedMachineIdentity.ps1'
if ($identity.status -ne 'VERIFIED' -or
    $identity.machineId -ne 'snd-host' -or
    $env:COMPUTERNAME -ne 'SND-HOST') {
    throw 'Refusing live gallery start: verified SND-HOST identity is required.'
}
Set-Location F:\Wallpapers\webgallery
$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $python .\reports\dashboard_server.py `
  --app-reports-root F:\Wallpapers\webgallery\reports `
  --collection-root F:\Wallpapers `
  --library-root F:\Wallpapers\library `
  --database-path F:\Wallpapers\webgallery_library.sqlite `
  --queue-state-path F:\Wallpapers\.wallpaper-download-queue\state.json `
  --report-output-root F:\Wallpapers\reports `
  --thumbnail-cache-root F:\Wallpapers\.gallery-thumbnail-cache `
  --preview-root F:\Wallpapers\temp_downloads `
  --pause-flag-path F:\Wallpapers\.wallpaper-download-queue\pause.flag `
  --transfer-config-path F:\Wallpapers\.wallpaper-transfer-targets.json `
  --environment-path F:\Wallpapers\.env `
  --host 127.0.0.1 --port 8091
```

Open:

```text
http://127.0.0.1:8091/library
```

The browser starts in the SFW collection. NSFW is a separate tab and its
thumbnails remain blurred until **Reveal NSFW thumbnails** is selected. Other
filters include source, orientation, resolution, exact tag, and franchise.
Filter and sort state are stored in the URL. Results are fetched in bounded 48-picture
batches and the next batch is appended automatically as the scroll position
approaches the end of the loaded grid. Sorting runs over the full filtered
collection before those batches are returned. In addition to the existing
file/date/resolution/size/source sorts, the API provides least-tagged and
lowest-rating-confidence triage plus deterministic shuffle with a validated
integer seed. Cards display dimensions, download time, and file size so the
active ordering is visible and auditable. Materialized explanations and
review-only suggestions remain separate in the response.

The page displays whether the current SQLite file still matches the most
recent verification report for that exact database and library root. It may
expose a clearly labelled working snapshot while downloads are active, but it
never claims that snapshot is verified.

## CLI queries

Content rating can be combined with existing index filters:

```powershell
Set-Location F:\Wallpapers\webgallery
$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
$galleryDb = 'F:\Wallpapers\webgallery_library.sqlite'
& $python -m dl_engine.index_library --db-path $galleryDb --query "rating=nsfw limit=100"
& $python -m dl_engine.index_library --db-path $galleryDb --query "rating=nsfw nsfw_subcategory=explicit limit=100"
& $python -m dl_engine.index_library --db-path $galleryDb --query "rating=suggestive orientation=portrait limit=100"
& $python -m dl_engine.index_library --db-path $galleryDb --query "rating=unknown source=zerochan limit=100 offset=100"
& $python -m dl_engine.index_library --db-path $galleryDb --query "rating=unknown sort=rating_confidence limit=100"
& $python -m dl_engine.index_library --db-path $galleryDb --query "sort=least_tagged limit=100"
& $python -m dl_engine.index_library --db-path $galleryDb --query "sort=shuffle shuffle_seed=20260720 limit=100"
```

The index query and `library_browser` API accept an `nsfw_subcategory` filter
that only applies within the `nsfw` collection. Plan 003's WPH work supplies
the visible browser controls; until that lands, use the CLI or API contract
documented here.

It is rejected unless the overall rating filter is `nsfw`. Supported values
are `nudity`, `explicit`, `fetish`, and `unspecified`.

Supported rating values are `sfw`, `suggestive`, `nsfw`, and `unknown`.
`sort` accepts `path`, `newest`, `oldest`, `resolution_desc`, `resolution_asc`,
`size_desc`, `size_asc`, `filename`, `franchise`, `source`, `least_tagged`,
`rating_confidence`, or `shuffle`. Shuffle requires `shuffle_seed`; retain the
same seed across offset pages for a deterministic duplicate-free order.

## API

The loopback dashboard server exposes read-only, allowlisted endpoints:

```text
GET /api/library/status
GET /api/library/facets
GET /api/library?rating=nsfw&nsfw_subcategory=explicit&sort=newest&limit=48&offset=0
GET /api/library/tags?prefix=land&limit=20
POST /api/library/suggestions/123
```

The response schema is version 3. The query endpoint accepts only `rating`,
`nsfw_subcategory`, `source`, `orientation`, `bucket`, `tag`, `franchise`,
`sort`, `shuffle_seed`, `limit`, and `offset`. Page size is capped at 100,
paths outside `F:\Wallpapers\library` are never converted into served URLs,
and directory listings remain disabled. Count, image rows, authoritative tags,
and suggestions are hydrated from one explicit SQLite read snapshot.
Autocomplete searches authoritative typed tags only and returns distinct image
counts. Suggestion review accepts same-origin JSON only; decisions remain in
the suggestion layer and do not trigger rating or subcategory refresh. API
payloads expose only ID/SHA media URLs, never absolute image or database paths.

## Verification

```powershell
Set-Location F:\Wallpapers\webgallery
$python = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $python -m pytest -q tests/test_content_rating.py tests/test_index_library.py tests/test_library_browser.py tests/test_dashboard_server.py
```

The content split is rebuildable index metadata. Canonical images,
authoritative sidecars, duplicate quarantine, queue trees, and maintenance
ownership do not change. `F:\Wallpapers\wallpaper_library.sqlite` remains the
schema-2 sibling maintenance database and must not be migrated by this project.
