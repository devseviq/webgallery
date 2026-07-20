# Discovery — Gallery Sorting, Presentation, and Tag Enrichment

**Goal:** Work on good ideas for sorting and presentation in the web gallery, along with possibly getting the wallpapers more tagged.
**Date:** 2026-07-20
**Status:** complete
**Recommended next:** Ready to plan — run `/planner Improve the web gallery sorting, presentation, and tag enrichment (see docs/discovery-gallery-sorting-presentation.md)`.

---

## Questions

1. What actually powers the web gallery and what data does it receive?
2. Which sorting, filtering, and presentation controls already exist?
3. Where can richer tags come from, and how complete is current metadata?
4. What performance and exposure constraints shape safe changes?
5. Which improvements provide the most value with the least disruption?

---

## Executive Finding

The full-library browser requested in the earlier category-navigation discovery
now exists. It is backed by the canonical SQLite index and already provides
stable sorting, rating/source/orientation/resolution filters, exact tag and
franchise filters, infinite scroll, selection, and copy-to-target actions.
Building another gallery would duplicate working foundations.

The strongest next move is a three-part improvement:

1. **Make browsing light and isolated:** serve cached thumbnails and only
   allowlisted gallery resources from a dedicated handler. The current gallery
   loads full-resolution originals and the same server exposes the whole
   workspace by known path.
2. **Turn metadata into navigation:** make tags clickable, add counted tag
   suggestions, active-filter chips, a lightbox/detail drawer, density/fit
   controls, useful presets, and deterministic shuffle or target-display sorts.
3. **Improve tag quality by source:** finish safe provider enrichment, preserve
   raw tags, add canonical aliases/roles, and keep any machine-generated tags in
   a separate reviewable suggestion layer.

The collection is already almost universally tagged: 81,924 of 81,927 indexed
images have at least one tag. The real problem is **depth, type, and quality**.
40,199 images have exactly one tag, 60,182 have no confident content rating,
and tag quality differs sharply by source.

---

## Live Baseline

Read-only measurements from the live SND-HOST workspace on 2026-07-20:

| Surface | Current observation |
|---|---:|
| Indexed images | 81,927 |
| Images with at least one tag | 81,924 |
| Distinct source-qualified tags | 28,230 |
| Image-tag associations | 449,364 |
| Images with exactly one tag | 40,199 |
| Images with 6-10 / 11+ tags | 28,346 / 10,013 |
| SFW / suggestive / NSFW / unknown | 10,886 / 3,800 / 7,059 / 60,182 |
| Wallhaven rows | 31,620 |
| Zerochan rows | 28,310 |
| Anime-Pictures rows | 21,083 |
| Unknown-source rows | 914 |
| Typical API page size | 48 items |
| First 48 SFW originals | 218.5 MiB total, 4.55 MiB average |
| Warm facet response | about 2 ms |
| 48-item SFW newest response | about 1.95 s before image loading |
| 48-item Unknown newest response | about 2.24 s before image loading |
| 48-item Unknown resolution response | about 1.99 s before image loading |

The live index currently reports **unverified** because it changed after the
last successful maintenance verification. The last report covered 71,307
images while the current database contains 81,927. This does not prove
corruption; it means the browser must not present the current snapshot as a
verified maintenance boundary.

The relevant browser and content-rating unit tests pass:

```text
19 passed in 0.34s
```

---

## Findings

### Q1: What powers the gallery and what data does it receive?

**Answer:** There are two gallery surfaces. The operations dashboard has a
bounded recent-preview gallery, while `reports/library-browser.html` is the
actual full-library browser. Its API reads `wallpaper_library.sqlite` through
`dl_engine.library_browser` and returns image identity, dimensions, source,
rating explanation, timestamps, hashes, and typed/provenanced tags.

**Evidence:**

- `reports/README_live_dashboard.md:94-121` defines the bounded recent gallery
  in the operations dashboard.
- `reports/dashboard_server.py:35-41` binds the full browser to the canonical
  library and database.
- `reports/dashboard_server.py:211-245` exposes status, facet, and paginated
  library APIs.
- `src/dl_engine/library_browser.py:39-85` serializes the full item contract,
  including rating confidence/reasons and tag type/provenance.
- `reports/library-browser.html:178-225` defines the full collection, filter,
  transfer, and grid surfaces.
- `reports/library-browser.html:493-502` maps UI state to the paginated API.
- `reports/dashboard_server.py:309-314` routes `/library` to the full browser.

**Implications:**

- The full browser is the correct home for discovery and curation features.
- The recent operations gallery should remain a lightweight operational sample,
  not grow into another full collection UI.
- Future UI work can reuse the API's existing rich tag and rating explanation
  instead of changing image files or sidecars.

### Q2: What sorting, filtering, and presentation already exist?

**Answer:** The browser already has a strong technical filter base, but weak
discovery interactions. It offers ten stable sorts and six filter dimensions,
yet tag/franchise entry requires exact text, displayed tags are not clickable,
and image clicks leave the app for a raw original in a new tab.

**Evidence:**

- `src/dl_engine/index_library.py:2208-2265` implements path, newest/oldest,
  resolution, size, filename, franchise, and source sorts with deterministic
  tie-breakers.
- `src/dl_engine/index_library.py:2268-2351` combines exact orientation,
  franchise, bucket, source, tag, and content-rating filters with pagination.
- `reports/library-browser.html:191-210` exposes those sorts and filters, but the
  tag and franchise inputs are exact free-text fields without discovery help.
- `reports/library-browser.html:77-105` uses a fixed 220-pixel media box and
  `object-fit: contain`; there is no density, crop, masonry, or target-display
  mode.
- `reports/library-browser.html:557-568` opens the original in a new tab rather
  than providing a lightbox/detail view.
- `reports/library-browser.html:574-588` renders useful metadata and up to seven
  tag names, but discards tag type/provenance in the visible UI and gives tags
  no interaction.
- `reports/library-browser.html:272-299` keeps filters bookmarkable in the URL,
  so saved views can be implemented as named URL presets rather than copies.
- `reports/DASHBOARD_PLAN.md:442-466` already captured related ideas such as
  more-like-this, keyboard navigation, a lightbox, and desktop-friendly
  detection; they are still in the backlog.

**Implications:**

- More sort options alone will not materially improve discovery.
- Clickable tags, suggestions with counts, and a detail/lightbox workflow will
  make the metadata the user can already see useful.
- Presentation should offer at least two modes: dense `contain` browsing and a
  more visual crop/masonry mode. Neither should change canonical files.
- Bookmarkable smart collections such as `Desktop 4K`, `Portrait`, `Recently
  added`, `Needs rating`, and `Least tagged` can remain ordinary query URLs.

### Q3: How complete and useful are current tags?

**Answer:** Raw coverage is excellent, but semantic coverage is uneven. Rich
Wallhaven rows average 14.05 tags, pending Wallhaven rows and all Zerochan rows
average only one, and Anime-Pictures rows average 6.2 but carry generic typing
and several high-frequency structural tags.

**Evidence:**

- The live read-only query found 81,924 tagged images out of 81,927 and 449,364
  image-tag associations.
- 19,697 enriched Wallhaven rows average 14.05 tags, while 11,923 pending rows
  average exactly 1.0.
- All 28,310 Zerochan rows average exactly 1.0 tag; 27,971 of them remain in the
  `unknown` content-rating collection.
- Anime-Pictures has 21,083 rows and averages 6.2 tags, but 20,065 remain
  `unknown` for content rating.
- High-frequency low-discovery tags include `single` (13,148 images), `tall
  image` (7,050), `None` (6,729), `a` (2,166), `1 pictures` (2,153), and
  `highres` (1,733).
- `src/dl_engine/index_library.py:1933-2043` prefers complete sidecar metadata,
  retains provenance, and replaces source tags without moving originals.
- `src/dl_engine/index_library.py:3111-3125` still advances pending Wallhaven
  work with a lexicographic `source_site_id > cursor` predicate; the earlier
  review identified this as unsafe for out-of-order pending IDs.
- `tests/fixtures/ap_post.html:254` proves the captured Anime-Pictures response
  contains typed tag IDs, tag types, parents, score, color, and other metadata
  richer than the current generic `tag`/`franchise` projection.
- `src/dl_engine/content_rating.py:137-175` deliberately refuses to infer SFW
  from missing evidence, which correctly keeps sparse metadata in `unknown`.

**Implications:**

- "More tagged" should not mean adding uncontrolled labels to every image.
- First recover higher-quality provider metadata for the sparse sources.
- Keep raw provider tags immutable and auditable; add aliases, canonical labels,
  display roles, and suggestions alongside them.
- Structural tags can still be useful for filters but should not crowd the
  first seven visible chips or drive subject categories.
- Content rating must continue to treat absence of evidence as `unknown`.

### Q4: What performance and exposure constraints shape safe changes?

**Answer:** The current browser is expensive before and after the API response.
Rating queries aggregate tags across the collection, each returned row triggers
another tag query, and the UI then requests original-resolution images from a
single-threaded server. The server also serves the entire workspace by known
path, including operational state and the database itself.

**Evidence:**

- `src/dl_engine/index_library.py:2302-2320` builds a tag aggregate for
  content-rating filtering at query time.
- `src/dl_engine/index_library.py:2353-2374` performs a separate tag query for
  every returned image (48 extra queries for a normal page).
- `src/dl_engine/index_library.py:265-269` indexes source, orientation, bucket,
  site ID, and tag ID, but not the common sort fields such as recorded time,
  size, franchise, or the resolution expression.
- `src/dl_engine/library_browser.py:105-129` uses offset pagination; deep pages
  may become more expensive as the collection grows (inference from the query
  shape; deep offsets were not benchmarked in this discovery).
- `reports/library-browser.html:563-568` points `<img>` directly at the original;
  the first live SFW page represented 218.5 MiB of source images.
- `reports/dashboard_server.py:26-34` sets the served root to all of
  `F:\Wallpapers`.
- `reports/dashboard_server.py:106-113` disables directory listings but does
  not restrict direct known paths.
- `reports/dashboard_server.py:155-161,190-194` denies only the transfer target
  config before falling through to static file serving.
- Live header-only probes returned HTTP 200 for the 1.46 MB queue state, the
  Anime-Pictures config, and the 133.9 MB SQLite database.
- `reports/dashboard_server.py:381-382` uses `socketserver.TCPServer`, so static
  original requests and API work share a single request thread.

**Implications:**

- Thumbnail generation is the highest-value presentation optimization.
- A dedicated allowlisted server surface must precede broader gallery
  functionality: gallery HTML/API, cached thumbnails, and canonical originals
  should be explicit routes; queue/config/database files should not be static
  resources.
- Precompute or materialize rating/facet values during verified index
  publication, batch-load tags for a page, index common sorts, and prefer
  cursor/keyset pagination for deep scrolling.
- Request concurrency can help thumbnail delivery, but it should follow the
  allowlist boundary and bounded caching rather than merely making full-original
  flooding faster.

### Q5: Which improvements have the best value-to-disruption ratio?

**Answer:** The following register ranks improvements by user value, current
evidence, and risk. The first two rows are enabling work rather than optional
polish.

| Priority | Candidate | Why now | Effort | Risk |
|---:|---|---|---|---|
| 1 | Allowlisted gallery server surface | Current server exposes operational/config/database files by known path | Medium | Medium |
| 2 | SHA-keyed cached thumbnails | A single 48-card page references 218.5 MiB of originals | Medium | Low |
| 3 | Batch tag reads + materialized rating/facets + useful indexes | Normal API pages take about two seconds before images | Medium | Medium |
| 4 | Clickable typed tag chips + counted autocomplete | 28,230 tags are otherwise discoverable only by exact spelling | Medium | Low |
| 5 | Lightbox/detail drawer + keyboard navigation | Current click opens a raw original and hides rich metadata context | Medium | Low |
| 6 | Density and fit/crop controls | Fixed 220px `contain` is safe but visually inflexible | Small | Low |
| 7 | Named URL presets + deterministic shuffle | Adds useful collections without copying or moving files | Small | Low |
| 8 | Least-tagged, rating-confidence, and target-display sorts | Makes review and actual wallpaper selection easier | Small/Medium | Low |
| 9 | Repair and resume Wallhaven enrichment | 11,923 pending rows currently have only one tag | Medium | Medium |
| 10 | Typed Anime-Pictures enrichment | Existing captured response contains richer typed metadata | Medium | Medium |
| 11 | Zerochan enrichment ledger | 28,310 images currently average one search-context tag | Medium/High | Medium |
| 12 | Canonical tag aliases/roles | Preserves raw provenance while reducing noisy/redundant presentation | Medium | Medium |
| 13 | User curation/override layer | Current gallery transfers files but cannot favourite, hide, or manually tag | Medium | Medium |
| 14 | Reviewable local visual suggestions | Useful for sparse/unknown metadata only after provider work is measured | High | High |

Useful presentation groupings are:

- **Everyday browse:** newest, deterministic shuffle, recently viewed, and
  favourites.
- **Target display:** landscape/portrait/square plus a selected display ratio
  and minimum resolution; rank by aspect-ratio fit before raw megapixels.
- **Discovery:** franchise, canonical tag, source, color palette, and
  more-like-this.
- **Triage:** unknown rating, least tagged, pending enrichment, missing path,
  low-confidence classification, and rejected/hidden.

Useful tile behavior is:

- primary line: franchise or best canonical subject label;
- secondary line: dimensions, bucket, orientation, and source;
- compact top chips: diverse informative tags, not simply alphabetical first
  seven;
- detail drawer: all raw tags grouped by type/source/provenance, source link,
  rating reasons/confidence, dates, file facts, and transfer/curation actions;
- optional badges: exact display fit, desktop-friendly/negative-space feature,
  enrichment pending, and manual override.

---

## Tag Enrichment Boundary

Provider, inferred, and human metadata should not be merged into one
indistinguishable tag stream.

Recommended conceptual layers:

```text
provider tags          immutable source evidence
canonical aliases      display/search normalization over provider evidence
derived features       palette, aspect fit, negative space, embeddings
machine suggestions    label + confidence + model/version + review status
human curation         favourite/hide/manual tag/rating override, highest priority
```

A safe derived contract would add concepts such as:

```text
canonical_tags(id, label, facet, display_role)
tag_aliases(raw_tag_id, canonical_tag_id, rule_version)
image_features(image_id, feature, value, generator_version)
tag_suggestions(image_id, label, confidence, model_version, review_status)
user_curation(source, source_id, sha256, action, value, recorded_at)
```

Raw `tags` and `image_tags` remain the evidence and are not destructively
rewritten. Machine suggestions do not become authoritative until accepted.

The existing `swipewall_decisions.jsonl` contains 334 valid paired records
(330 reject actions and 4 sort actions). It is a useful starting signal for a
curation ledger, but it should be joined by stable source identity and SHA-256,
not treated as a replacement for provider metadata.

---

## Cross-Cutting Analysis

### Constraints

- Canonical image and sidecar files must remain paired and must not be
  physically re-sorted by overlapping subject tags.
- The library is live and the current database has changed since its last
  successful verification report.
- Content rating is safety-sensitive; unknown cannot be silently promoted to
  SFW.
- Provider metadata quality differs substantially by source.
- The dashboard server is loopback-only, but direct local HTTP exposure still
  matters for credentials, operational state, and the database.
- The current HTML is a hand-maintained static file and has no browser-level
  regression suite.
- Browser automation was unavailable during this discovery, so presentation
  findings are based on generated HTML/CSS/JS, API behavior, server logs, and
  live HTTP measurements rather than a claimed visual inspection.

### Risks

| Risk | Likelihood | Impact | Notes |
|---|---|---|---|
| Gallery expansion increases workspace exposure | High without isolation | High | The current static root is the whole workspace. |
| Full-size image loading makes the UI appear broken or memory-heavy | High | Medium | 218.5 MiB for the first 48 SFW cards. |
| Deep scrolling slows as offsets and computed ratings grow | Medium/High | Medium | Keyset pagination and materialized ratings avoid repeated work. |
| Tag cleanup destroys provider evidence | Medium | High | Use aliases/display roles; retain raw source tags. |
| Provider-specific richness biases discovery | High | Medium | Show provenance and measure coverage by source. |
| Unknown is presented as safe | Medium | High | Preserve conservative rating behavior and blur rules. |
| Visual tagger confidently mislabels niche content | Medium | Medium/High | Suggestions must be versioned, scored, and reviewable. |
| UI changes regress transfer or infinite scroll behavior | Medium | Medium | Add browser/API contract tests before substantial UI changes. |

### Open Questions

- Should the default collection remain SFW, or should the first screen present
  explicit collection choices without loading a gallery?
- Is the preferred visual mode a dense metadata browser, a cinematic masonry
  wall, or a switch between both?
- Which target display resolutions/aspect ratios should be named presets?
- Should favourites and manual tags be shared across peers or remain local to
  this library?
- After provider enrichment, is the remaining weak-tag set large enough to
  justify local visual-model compute and storage?

None of these block the safety, thumbnail, query, and clickable-tag foundation.

---

## Recommendation

Findings support proceeding. Run:

```text
/planner Improve the web gallery sorting, presentation, and tag enrichment
  (see docs/discovery-gallery-sorting-presentation.md)
```

The planning boundary should preserve this order:

1. isolate the HTTP surface and introduce cached thumbnails;
2. remove avoidable query work and establish browser/API regression coverage;
3. add discovery interactions and presentation modes;
4. improve provider metadata and canonical aliases;
5. add human curation, then evaluate visual suggestions only for the measured
   remainder.

No image, sidecar, queue, database, maintenance, transfer, or runtime state was
changed during this discovery. Only this findings document was added.

---

## Appendix — Evidence Commands

The live measurements used read-only SQLite URI connections and HTTP requests.
Representative query shapes:

```sql
SELECT COUNT(DISTINCT images.id), COUNT(DISTINCT image_tags.image_id),
       COUNT(DISTINCT tags.id), COUNT(image_tags.tag_id)
FROM images
LEFT JOIN image_tags ON image_tags.image_id=images.id
LEFT JOIN tags ON tags.id=image_tags.tag_id;

SELECT images.source, images.enrichment_status,
       COUNT(DISTINCT images.id),
       ROUND(1.0 * COUNT(image_tags.tag_id) / COUNT(DISTINCT images.id), 2)
FROM images
LEFT JOIN image_tags ON image_tags.image_id=images.id
GROUP BY images.source, images.enrichment_status;

SELECT CASE
         WHEN n=0 THEN '0'
         WHEN n=1 THEN '1'
         WHEN n BETWEEN 2 AND 5 THEN '2-5'
         WHEN n BETWEEN 6 AND 10 THEN '6-10'
         ELSE '11+'
       END AS bucket,
       COUNT(*)
FROM (
  SELECT images.id, COUNT(image_tags.tag_id) AS n
  FROM images
  LEFT JOIN image_tags ON image_tags.image_id=images.id
  GROUP BY images.id
)
GROUP BY bucket;
```

HTTP timing used the live `/api/library/facets` and `/api/library` endpoints.
Header-only direct-path probes avoided reading sensitive response bodies.
