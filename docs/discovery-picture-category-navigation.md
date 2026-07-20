# Discovery — Picture Category Navigation

**Goal:** Assist with sorting algorithms for the pictures so they can be grouped into useful categories and navigated.
**Date:** 2026-07-19
**Status:** complete
**Recommended next:** Ready to plan — build a verified-snapshot, multi-label category navigator over the existing SQLite index and dashboard. Do not move canonical images into subject folders.

---

## Questions

1. What categories and directory conventions already exist?
2. Which reliable classification signals are available from paths, sidecars, tags, dimensions, and visual analysis?
3. What does the current sorter own, and which live or protected trees must remain outside category work?
4. What browsing/index surface can expose useful categories without duplicating or relocating canonical files?
5. Which staged classification algorithm provides useful navigation now and can improve safely later?

---

## Executive Finding

The collection should not be physically re-sorted by subject. Its canonical
layout already has one stable ownership rule:

```text
library/<resolution-bucket>/<orientation>/<source>/<canonical-image>
```

Subject, franchise, style, mood, purity, and character are overlapping facets.
They belong in a multi-label index and browser, not competing folder trees.
The existing sidecars and SQLite schema already provide most of the foundation.

The quickest useful result is a full-library gallery backed by a successfully
verified SQLite snapshot, with filters for:

- subject category;
- franchise, series, or character tag;
- style and background;
- orientation and resolution;
- source;
- purity, including a distinct `unknown` value.

A deterministic rules classifier can categorize high-confidence metadata now.
A local image-embedding classifier should be a later fallback for weakly tagged
Zerochan and unknown-source images, not the first or sole authority.

---

## Live Baseline

Read-only measurements on SND-HOST on 2026-07-19 found:

| Surface | Observation |
|---|---:|
| Canonical images on disk | 75,520 |
| Images in the current SQLite snapshot | 71,307 |
| Indexed images with at least one tag | 71,306 |
| Distinct index tags | 20,926 |
| Image-tag associations | 294,544 |
| Wallhaven rows | 30,151 |
| Zerochan rows | 26,091 |
| Anime-Pictures rows | 14,272 |
| Unknown-source rows | 793 |
| Landscape / portrait / square indexed rows | 36,729 / 33,149 / 1,379 |
| Purity marked `unknown` | 59,653 |

The database passed SQLite quick-check, schema, hash, sidecar, and layout
checks, but the full verifier failed because the live library and index are not
at the same maintenance boundary: 4,266 disk images were not indexed and 53
indexed paths were missing. The latest maintenance run was deferred while
downloaders were active. This is an eventual-consistency condition, not
permission for the navigator to ignore verification.

Commands used for this baseline:

```powershell
python -m dl_engine.index_library --stats
python -m dl_engine.index_library --verify-json
```

Additional read-only SQLite queries counted `images`, `tags`, `image_tags`,
tag provenance, tag types, search origins, and purity values.

---

## Findings

### Q1: What categories and conventions already exist?

**Answer:** Physical storage already classifies every canonical image by
resolution, orientation, and source. Tags are deliberately many-to-many
metadata. This is the correct storage boundary and should remain unchanged.

**Evidence:**

- `docs/WALLPAPER_METADATA.md:9-20` defines the canonical physical layout and paired sidecar.
- `docs/WALLPAPER_METADATA.md:23-36` defines sources, resolution buckets, orientations, quarantine, and why tags are not directories.
- `docs/INDEX_LIBRARY.md:129-140` defines `images`, source-qualified `tags`, and the `image_tags` many-to-many relationship with provenance.
- `docs/INDEX_LIBRARY.md:161-162` explicitly directs saved searches and generated collections to query the index instead of copying images into tag folders.

**Implications:**

- A picture may be simultaneously `anime-character`, `space`, `dark`, `4K`,
  `portrait`, and `Honkai Star Rail`.
- A single subject folder would discard useful dimensions or require duplicate
  copies/links with ambiguous ownership.
- The word "sorting" should mean deriving and browsing category membership,
  while the existing sorter continues to own physical finalization.

### Q2: Which classification signals are reliable?

**Answer:** Dimensions and source are authoritative. Tag semantics are useful
but uneven by provider. Nearly all indexed images have a tag, but provenance
must affect confidence.

**Evidence:**

- `docs/INDEX_LIBRARY.md:131-133` records source identity, dimensions, search origins, tag type/category, and per-image provenance.
- Representative sidecars showed Anime-Pictures URL tags and franchise labels, Wallhaven search-origin tags, Zerochan search-directory labels, and unknown-source search labels.
- Read-only SQLite counts found an average of 6.24 tag links per Anime-Pictures image, 5.92 per Wallhaven image, 1.0 per Zerochan image, and 2.0 per unknown-source image.
- Wallhaven API associations provide rich categories such as People, Art & Design, Nature, Landscapes, Space, Architecture, Animals, Technology, and Vehicles, but only a subset of Wallhaven rows currently has API-derived tags.
- Frequent low-information values include `None`, `single`, `long hair`, `tall image`, `a`, `1 pictures`, and `highres`; these must not independently choose a subject category.
- `pyproject.toml:10-13` shows no image-embedding or computer-vision dependency; the current runtime dependency is only `gallery-dl`.

**Implications:**

- Provider tags can drive the first useful classifier if provenance is retained.
- Zerochan needs either stronger metadata enrichment or visual inference for
  subject-level coverage; its current search label is still valuable as a
  franchise/search facet.
- Purity must remain tri-state or four-state (`sfw`, `sketchy`, `nsfw`,
  `unknown`). Absence of enrichment is not evidence of safety.
- Filename/slug inference should be weak evidence only; canonical filenames
  summarize metadata but are not its authority.

### Q3: What does the current sorter own?

**Answer:** The canonical sorter owns guarded, preview-first admission from
completed staging payloads into resolution/orientation/source storage. Category
classification must be downstream and read-only toward image files.

**Evidence:**

- `docs/ARCHITECTURE.md:6-21` separates download staging, finalization, indexing, and Queue Browser ownership.
- `docs/ARCHITECTURE.md:53-68` documents preview-only default behavior and protected-tree pruning.
- `docs/WALLPAPER_METADATA.md:263-270` states that verification reads authority without rewriting sidecars or images.
- `reports/maintenance-20260719T1331575823292Z-p36724-1c0773fd/summary.json:4-8` records the latest maintenance result as `deferred`.
- The same summary at `:30-39` records a preflight deferral because downloaders were active.

**Implications:**

- Category generation must never scan or move queue-owned preview/recovery
  trees as if they were canonical library entries.
- Category tables may be rebuilt from the verified canonical index at any time.
- Human corrections need a separate durable, path-independent override ledger;
  machine-generated scores remain derived data.
- A navigator should display its snapshot timestamp and verification state.

### Q4: What navigation surface exists, and what is missing?

**Answer:** The current dashboard is a useful operations view, not a complete
library navigator. It samples tags and shows a bounded recent gallery. The
SQLite query layer is exact but supports only one exact tag/franchise and a
fixed result limit.

**Evidence:**

- `reports/_build_dashboard.py:232-252` samples at most 6,000 sidecars for its tag cloud rather than querying exact index counts.
- `reports/_build_dashboard.py:429-437` defines the gallery as a bounded recent preview.
- `reports/_build_dashboard.py:518-576` caps candidates by source and the final gallery at 160 items.
- `reports/_render_dashboard.py:722-733` renders tag-cloud entries as non-interactive spans.
- `reports/_render_dashboard.py:915-1023` builds source, orientation, and resolution filters only over the bounded gallery items.
- `src/dl_engine/index_library.py:2161-2180` exposes optional orientation, franchise, bucket, source, one exact tag, and a result limit.
- `src/dl_engine/index_library.py:2185-2212` combines those filters but orders by path and has no cursor/offset pagination.
- `src/dl_engine/index_library.py:2215-2235` already returns the full tag set for each selected image, which a gallery detail panel can reuse.
- `reports/dashboard_server.py:23-36` already provides a local dashboard server and rebuild lock, making it the natural local integration point.

**Implications:**

- The dashboard should query SQLite rather than embed the entire library in
  generated HTML.
- Add a read-only, allowlisted query endpoint with cursor pagination and exact
  facet counts.
- Category, tag, franchise, source, orientation, bucket, purity, and sort state
  should be encoded in URL parameters so a view is bookmarkable.
- Thumbnails should be generated/cached separately; the canonical original
  remains the click-through target.

### Q5: Which algorithm should be used?

**Answer:** Use a staged, provenance-weighted multi-label classifier. Start
with exact metadata facets, then deterministic category rules, then optional
visual inference for uncertain images. Never force every image into one subject.

#### Stage 1 — exact facets, no inference

Expose current indexed values directly:

- `franchise`, exact tags, and search origins;
- `orientation`, `resolution_bucket`, source, extension, and dimensions;
- `purity`, retaining `unknown`;
- arrival/index time where available.

This makes the collection navigable immediately and preserves the meaning of
the source metadata.

#### Stage 2 — deterministic subject/style categories

Version a small taxonomy and synonym map in the tooling repo. A practical
initial taxonomy is:

| Facet | Initial values |
|---|---|
| Subject | Anime & Characters; People & Models; Landscapes & Nature; Space & Sci-Fi; Abstract & Minimal; Cities & Architecture; Vehicles & Machines; Animals; Technology; Other / Review |
| Universe | Existing franchise, series, character, and search-origin values |
| Style | Anime; Photography; Digital Art; Minimal; Dark; Monochrome; Pixel Art; AI-generated; Colorful |
| Composition | Simple Background; Character Portrait; Scenic; Wide; Tall |
| Technical | Landscape; Portrait; Square; 4K; 1440p; 1080p; 720p; SD; source; extension |
| Content | SFW; Sketchy; NSFW; Unknown |

Normalize each evidence value with Unicode case-folding, whitespace/punctuation
normalization, aliases, and a stop-list. Preserve its original name and
provenance for display/audit.

Seed evidence weights:

| Evidence | Seed weight |
|---|---:|
| Human include/exclude override | forced |
| Curated mapping from a provider API tag/category | 0.95 |
| Typed source URL tag | 0.85 |
| Franchise/series field | 0.80 |
| Search-origin or staging collection | 0.60 |
| Calibrated visual-model result | up to 0.70 |
| Filename/slug token | 0.25 |

For a category `c`, combine independent evidence without allowing repeated
near-duplicate tags to dominate:

```text
score(c) = min(1.0, max(matching_weights) + 0.15 * independent_extra_sources)
```

Suggested initial decision rule:

```text
membership: score >= 0.60
primary label for default grouping: top score >= 0.75 and margin >= 0.10
otherwise: keep all memberships and mark primary as needs-review
```

Thresholds are seeds, not facts. Calibrate them against a manually reviewed,
source-balanced sample before treating primary labels as reliable. Navigation
does not require a primary label; multi-label filters are the real product.

#### Stage 3 — visual fallback

For images with weak/no semantic metadata, compute local image embeddings and
compare them to both category prompts and reviewed nearest neighbours. Store:

- model name and version;
- preprocessing version;
- embedding or embedding-cache key;
- per-category score;
- chosen threshold and classifier version;
- evidence explaining why a category was assigned.

Use visual output only when calibrated for this collection. It should add
low/medium-confidence memberships, not overwrite source tags or purity.

#### Stage 4 — feedback and overrides

The browser should offer `include`, `exclude`, and `needs review`. Persist human
overrides in an append-only ledger keyed by canonical source identity plus
SHA-256. Overrides survive path changes and take precedence over generated
assignments.

**Implications:**

- Category assignment is deterministic and explainable before any model is
  introduced.
- Visual classification can be recomputed or upgraded without modifying image
  files or authoritative source metadata.
- Provider-specific quality differences become explicit confidence rather than
  hidden bias.

---

## Proposed Derived Data Contract

Keep the existing authority tables intact. Add rebuildable category data such
as:

```text
category_definitions(id, facet, label, taxonomy_version)
image_categories(image_id, category_id, score, is_primary,
                 classifier_version, evidence_json, generated_at)
```

Keep durable manual decisions outside the disposable database:

```text
category-overrides.v1.jsonl
  source + source_id + sha256 + category_id + action + recorded_at
```

An atomic navigator publication should be:

```text
zero-child maintenance
  -> rebuild SQLite candidate
  -> verify candidate against canonical disk/sidecars
  -> derive categories
  -> atomically publish verified database snapshot
  -> dashboard serves snapshot with generated-at + verify status
```

If rebuild or verification fails, keep serving the last verified snapshot and
show that it is stale. Do not publish a partially current index.

---

## Cross-Cutting Analysis

### Constraints

- The library is live and currently changing while a downloader is active.
- Canonical files and sidecars must remain paired under the current physical layout.
- Tags, categories, styles, and franchises are many-to-many.
- Source metadata quality is uneven; Zerochan is particularly sparse.
- Purity is unknown for most indexed images.
- The current dashboard cannot embed or filter all 75,000+ originals as one static page.
- Category work must not weaken the zero-child maintenance boundary or protected queue ownership.

### Risks

| Risk | Likelihood | Impact | Notes |
|---|---|---|---|
| Navigator serves an unverified/stale index as current | High without a gate | High | Live verification currently reports 4,319 path-set issues. |
| One-folder-per-category loses overlapping meaning | Certain | High | Tags and categories are many-to-many. |
| Generic tags dominate categories | High | Medium | Stop-list, provenance, and independent-evidence scoring are required. |
| Provider bias makes Wallhaven look better categorized | High | Medium | Anime-Pictures and Zerochan need source-aware confidence. |
| Unknown purity is treated as SFW | Medium | High | Unknown must remain explicit and default behavior must be user-configurable. |
| Visual model confidently mislabels niche anime/franchise content | Medium | Medium | Calibrate on a source-balanced sample and retain review state. |
| Full-size gallery is slow or memory-heavy | High without pagination | Medium | Use cursor pagination and cached thumbnails. |
| Generated data becomes mistaken for authority | Medium | High | Keep model/rule output derived; preserve sidecars and override ledger boundaries. |

### Open Questions

- Should the default view include all purity states, or start with SFW plus Unknown?
- Which 8-12 subject/style labels feel most natural to the user after seeing a first gallery prototype?
- Is local visual classification worth the compute/storage cost after rule-based coverage is measured?
- Should manual overrides be shared across machines or remain specific to this collection?

These choices affect defaults and later model work, but they do not block the
first verified, exact-facet navigator.

---

## Recommendation

Findings support proceeding in this order:

1. Wait for a zero-child maintenance boundary and require a successful rebuild
   plus verifier result before publishing a navigator snapshot.
2. Add exact, paginated, read-only library queries and facet counts to the
   local dashboard server.
3. Build the full-library thumbnail gallery with bookmarkable filters for
   tag/franchise, orientation, resolution, source, and purity.
4. Add the versioned deterministic taxonomy, evidence scoring, explainability,
   and manual override ledger.
5. Measure uncategorized/low-confidence coverage by source. Only then evaluate
   local visual embeddings for the remaining weak cases.

This is ready for a bounded implementation plan. The first implementation
should stop after exact facets plus deterministic rules; model-based visual
classification is a separately measurable follow-up.

---

## Appendix — Read-Only Evidence Queries

The following query shapes were used against
`F:\Wallpapers\wallpaper_library.sqlite` in SQLite read-only mode:

```sql
SELECT COUNT(*) FROM images;
SELECT COUNT(*) FROM tags;
SELECT COUNT(*) FROM image_tags;
SELECT COUNT(DISTINCT image_id) FROM image_tags;
SELECT source, COUNT(*) FROM images GROUP BY source;
SELECT i.source, it.provenance, COUNT(DISTINCT i.id), COUNT(*)
FROM images i JOIN image_tags it ON it.image_id = i.id
GROUP BY i.source, it.provenance;
SELECT i.source, t.tag_type, COUNT(DISTINCT i.id), COUNT(*)
FROM images i
JOIN image_tags it ON it.image_id = i.id
JOIN tags t ON t.id = it.tag_id
GROUP BY i.source, t.tag_type;
```

No image, sidecar, queue state, dashboard code, or maintenance state was
changed. No sort, apply, index rebuild, or enrichment command was run. This
discovery added only this document.
