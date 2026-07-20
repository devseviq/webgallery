# Content Rating Collections

The library browser separates indexed pictures into four derived collections
without moving or copying canonical image files:

| Collection | Meaning |
|---|---|
| `sfw` | The source explicitly marked the image safe and no more-restrictive tag evidence exists. |
| `suggestive` | The source marked it sketchy/questionable, or a conservative suggestive tag matched. |
| `nsfw` | The source marked it NSFW, or an explicit adult tag matched. |
| `unknown` | No reliable rating signal exists. Unknown is never treated as SFW. |

The implementation is deterministic and explainable. Each result includes its
basis, confidence, and matching reasons. Source purity is authoritative, but
explicit evidence may make an apparently safe row more restrictive. Tags can
never infer SFW from silence.

The rules live in `src/dl_engine/content_rating.py`. They use normalized,
whole-token phrase matching so short adult terms do not match inside unrelated
words. Visual/AI moderation is not currently used; weakly tagged images remain
`unknown` until a separately calibrated classifier or human review provides
evidence.

## Browser

Start the existing local dashboard server:

```powershell
Set-Location F:\Wallpapers\reports
python .\dashboard_server.py
```

Open:

```text
http://127.0.0.1:8090/library
```

The browser starts in the SFW collection. NSFW is a separate tab and its
thumbnails remain blurred until **Reveal NSFW thumbnails** is selected. Other
filters include source, orientation, resolution, exact tag, and franchise.
Filter and sort state are stored in the URL. Results are fetched in bounded 48-picture
batches and the next batch is appended automatically as the scroll position
approaches the end of the loaded grid. Sorting runs over the full filtered
collection before those batches are returned. The default is most recently
downloaded; the menu also offers oldest, resolution, file size, franchise,
source, filename, and canonical folder/path order. Cards display dimensions,
download time, and file size so the active ordering is visible and auditable.

The page displays whether the current SQLite file still matches the most recent
successful maintenance verification. It may expose a clearly labelled working
snapshot while downloads are active, but it never claims that snapshot is
verified.

## CLI queries

Content rating can be combined with existing index filters:

```powershell
Set-Location F:\Wallpapers\dl-engine
python -m dl_engine.index_library --query "rating=nsfw limit=100"
python -m dl_engine.index_library --query "rating=suggestive orientation=portrait limit=100"
python -m dl_engine.index_library --query "rating=unknown source=zerochan limit=100 offset=100"
python -m dl_engine.index_library --query "rating=sfw bucket=4K source=wallhaven sort=resolution_desc"
```

Supported rating values are `sfw`, `suggestive`, `nsfw`, and `unknown`.
`sort` accepts `path`, `newest`, `oldest`, `resolution_desc`, `resolution_asc`,
`size_desc`, `size_asc`, `filename`, `franchise`, or `source`. `offset` provides
stable pagination within the selected order.

## API

The loopback dashboard server exposes read-only, allowlisted endpoints:

```text
GET /api/library/status
GET /api/library/facets
GET /api/library?rating=nsfw&sort=newest&limit=48&offset=0
```

The query endpoint accepts only `rating`, `source`, `orientation`, `bucket`,
`tag`, `franchise`, `sort`, `limit`, and `offset`. Page size is capped at 100, paths
outside `F:\Wallpapers\library` are never converted into served URLs, and
directory listings remain disabled.

## Verification

```powershell
python -m pytest -q tests/test_content_rating.py tests/test_library_browser.py
python -m pytest -q tests/test_index_library.py::QueryTests
```

The content split is a derived navigation view. Canonical images, authoritative
sidecars, duplicate quarantine, queue trees, and maintenance ownership do not
change.
