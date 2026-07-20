# Content Rating Collections

The library browser separates indexed pictures into four derived collections
without moving or copying canonical image files:

| Collection | Meaning |
|---|---|
| `sfw` | The source explicitly marked the image safe and no more-restrictive tag evidence exists. |
| `suggestive` | The source marked it sketchy/questionable, or a conservative suggestive tag matched. |
| `nsfw` | The source marked it NSFW, or an explicit adult tag matched. |
| `unknown` | No reliable rating signal exists. Unknown is never treated as SFW. |

The implementation is deterministic and explainable. Each indexed result
materializes its rating, confidence, basis, and stable JSON reasons. Source
purity is authoritative, but explicit provider-tag evidence may make an
apparently safe row more restrictive. Tags can never infer SFW from silence.

The classifier authority remains `src/dl_engine/content_rating.py`. It uses
normalized, whole-token phrase matching so short adult terms do not match
inside unrelated words. Schema 3 stores that classifier's output in the
rebuildable SQLite index; it does not duplicate or replace the rules.

Materialized rating fields are refreshed after a rebuild or ingest, after a
schema-2 migration, and after authoritative provider tags change. The same
transaction refreshes `tag_count` and the global facet snapshot. Rebuilding
from sidecars and provider ledgers recomputes these fields, so SQLite remains a
cache rather than rating evidence.

Visual-model output is stored only in `tag_suggestions`. Pending, accepted, and
rejected suggestions are all excluded from provider tags, raw tag counts,
autocomplete, content ratings, purity, and franchise derivation. A reviewer
decision records curation state; it does not turn a model label into provider
evidence. Weakly tagged images therefore remain `unknown` until authoritative
provider evidence changes.

## Browser

Start the allowlisted dashboard server only with the explicit collection,
library, database, queue, report, cache, transfer, and environment paths
documented in `reports/README_live_dashboard.md`. The application deliberately
does not infer live roots from a Git worktree. To inspect its arguments:

```powershell
python .\reports\dashboard_server.py --help
```

Open:

```text
http://127.0.0.1:8090/library
```

The browser starts in the SFW collection. NSFW is a separate tab and its
thumbnails remain blurred until **Reveal NSFW thumbnails** is selected. Other
filters include source, orientation, resolution, exact tag, and franchise.
Filter and sort state are stored in the URL. Results are fetched in bounded
48-picture batches and the next batch is appended automatically as the scroll
position approaches the end of the loaded grid. Sorting runs over the full
filtered collection before those batches are returned. In addition to the
existing stable sorts, schema 3 provides least-tagged and lowest-rating-
confidence triage plus deterministic shuffle with a validated integer seed.
The API supplies the materialized rating explanation and keeps visual
suggestions in a separate collection for a clearly labelled review section.

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
python -m dl_engine.index_library --query "rating=unknown sort=rating_confidence limit=100"
python -m dl_engine.index_library --query "sort=least_tagged limit=100"
python -m dl_engine.index_library --query "sort=shuffle shuffle_seed=20260720 limit=100"
```

Supported rating values are `sfw`, `suggestive`, `nsfw`, and `unknown`.
`sort` accepts `path`, `newest`, `oldest`, `resolution_desc`, `resolution_asc`,
`size_desc`, `size_asc`, `filename`, `franchise`, `source`, `least_tagged`,
`rating_confidence`, or `shuffle`. Shuffle requires `shuffle_seed`; the seed
must be retained across every offset page for a duplicate-free deterministic
order.

## API

The loopback dashboard server exposes read-only, allowlisted endpoints:

```text
GET /api/library/status
GET /api/library/facets
GET /api/library?rating=nsfw&sort=newest&limit=48&offset=0
GET /api/library/tags?prefix=land&limit=20
POST /api/library/suggestions/123
```

The query endpoint accepts only `rating`, `source`, `orientation`, `bucket`,
`tag`, `franchise`, `sort`, `shuffle_seed`, `limit`, and `offset`. Page size is
capped at 100. API payloads contain no absolute image or database paths;
canonical files receive only `/thumb/<sha256>.webp` and `/original/<image-id>`
URLs. The temporary `url` compatibility field aliases `original_url` and should
be removed after all clients consume the explicit field.

Autocomplete searches authoritative typed tags only and returns distinct image
counts. Suggestion review accepts only same-origin JSON `POST` requests with an
`accepted` or `rejected` decision and a reviewer. The decision remains in the
suggestion layer and does not trigger a rating refresh.

## Verification

```powershell
python -m pytest -q tests/test_index_library.py tests/test_library_browser.py tests/test_content_rating.py
```

The content split is a derived navigation view. Canonical images, authoritative
sidecars, duplicate quarantine, queue trees, and maintenance ownership do not
change.
