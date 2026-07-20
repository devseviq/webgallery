# Agent Task — Secure Thumbnail Serving

**Scope:** Replace broad static serving with explicit allowlisted report and media routes, add SHA-keyed cached thumbnails, and preserve the operations dashboard through sanitized contracts.

**Depends on:** none

**Output files:** reports/dashboard_server.py, reports/_build_dashboard.py, reports/_render_dashboard.py, reports/README_live_dashboard.md, reports/watch_dashboard.ps1, src/dl_engine/gallery_thumbnails.py, tests/test_gallery_thumbnails.py, tests/test_dashboard_server.py, pyproject.toml, requirements.txt, live-tracker.md

## Exit Criteria

- Arbitrary paths below F:/Wallpapers are no longer mapped by SimpleHTTPRequestHandler; direct probes for queue state, environment/config files, backup files, SQLite, logs, CSV files, and unknown report files return 404 or 403.
- The operations dashboard, library browser, dashboard summary, rebuild/status, pause/status, transfer, library API, explicit preview media, canonical-original, and thumbnail routes remain available through a documented allowlist.
- Live queue refresh reads a sanitized aggregate endpoint that never emits job URLs, arguments, credentials, raw queue rows, or filesystem paths.
- Canonical thumbnails are keyed by indexed SHA-256 plus transform version, generated atomically with bounded dimensions/pixels, reused on cache hits, and served with immutable cache headers.
- Canonical originals are addressable only through validated image identity; preview media resolves only beneath temp_downloads or library with supported image extensions.
- The server is threaded so API and thumbnail requests are not serialized behind one slow response.
- Focused thumbnail and route-denial tests pass, and the generated operations dashboard uses the sanitized endpoint and explicit media URLs.

---

## Context — read before doing anything

1. .continue/rules/project-conventions.md — images never enter the repo; Python may write derived cache/index data but may not move, sort, delete, or rewrite canonical images.
2. docs/campaign-plan-002-secure-and-improve-the.md — server allowlist, invariants, rollback, and WPD-to-WPE route contracts.
3. docs/discovery-gallery-sorting-presentation.md — 218.5 MiB first-page baseline and confirmed direct-path exposure.
4. reports/dashboard_server.py — current SERVE_ROOT, Handler.do_GET, Handler.do_POST, control routes, transfer API, and single-threaded TCPServer.
5. reports/_build_dashboard.py — STATE, preview collection, and rel_url production.
6. reports/_render_dashboard.py — fetchState, preview rendering, rebuild, pause/resume, and generated HTML contract.
7. reports/library-browser.html — read only for current library and transfer expectations; WPF owns edits.
8. src/dl_engine/index_library.py — read only for IMAGE_EXTENSIONS, image identity, SHA-256, and canonical-root invariants.
9. src/dl_engine/library_browser.py — read only for current serialization; WPE will add thumbnail_url and original_url.
10. tests/test_library_browser.py and tests/test_library_transfer.py — preserve current API and transfer behavior.

All report source is now inside this repository. Generated dashboard HTML and
snapshots are runtime artifacts and must remain ignored. Treat the Git root as
the application root only; never infer a live collection, database, queue,
environment file, or cache from the worktree's parent directory.

---

## Task

### Part 1 — Add a bounded thumbnail cache module

Create src/dl_engine/gallery_thumbnails.py using Pillow. Provide a frozen ThumbnailSpec with max_width 640, max_height 640, max_source_pixels 120000000, format WEBP, quality 82, and version 1. Provide typed thumbnail_cache_path and ensure_thumbnail functions.

- Validate a full 64-hex SHA-256 and use a sharded path like v1/ab/<sha>-640x640-q82.webp.
- Treat SHA as indexed identity; do not hash a multi-megabyte original per request. The server obtains it from a read-only indexed row and proves source-root containment before calling.
- Refuse unsupported formats, non-files, invalid dimensions, and source pixel counts over the cap. Convert Pillow decompression warnings/errors to controlled failures.
- Apply EXIF orientation, preserve aspect ratio, never upscale, strip metadata, and save WEBP RGB/RGBA output.
- Write a unique temporary sibling, close it, then Path.replace the final. Clean only the temporary file created by the failed call.
- Use a per-cache-key lock so concurrent requests produce one final artifact. A valid nonempty final file is a cache hit.
- Do not prune in request handling. The versioned cache is disposable and removable only while the server is stopped.

Confirm Pillow remains declared consistently in pyproject.toml and
requirements.txt without altering unrelated dependency edits.

### Part 2 — Replace broad static serving with explicit routes

Refactor reports/dashboard_server.py so no fallback maps a URL to arbitrary F:/Wallpapers content. Inheriting BaseHTTPRequestHandler is preferred; super().do_GET() against SERVE_ROOT = ROOT must disappear.

Separate APP_ROOT from runtime roots. Add explicit CLI/configuration inputs for
the collection root, library root, database path, queue-state path, report
output root, thumbnail-cache root, and any environment/config path. Tests must
pass every root explicitly. A worktree parent is never a runtime default.

Implement exact, method-aware routes:

- / redirects to /reports/download-queue-dashboard.html.
- /library redirects to /reports/library-browser.html.
- Only download-queue-dashboard.html, dashboard.html, and library-browser.html are static report allowlist entries.
- /api/operations/status returns aggregate counts, failed_by_handler, UTC-today completed count, updatedAt, lastWorkerAt, lastMessage truncated to 80 display characters, and paused. Never return jobs, URL, args, target paths, raw state, or secrets.
- Preserve /api/library, /api/library/status, /api/library/facets, and transfer GET/POST routes.
- Reserve /api/library/tags for WPE counted autocomplete and /api/library/suggestions/<integer-id> for same-origin JSON review writes. WPD tests may mock WPE helper methods.
- /_pause_status stays GET. Convert pause, resume, and rebuild mutations to same-origin POST. Keep /_rebuild_status as exact-match GET and fix ordering so it cannot be captured by rebuild.
- /thumb/<64-hex-sha256>.webp resolves exactly one canonical row by SHA with a read-only DB, proves containment under LIBRARY_ROOT, calls ensure_thumbnail, and returns image/webp with immutable cache headers and nosniff.
- /original/<positive-image-id> resolves by indexed ID, proves canonical-root containment and supported extension, and streams with the correct image MIME type. Never accept a filesystem path.
- /media/preview/<encoded-relative-path> resolves only under temp_downloads. /media/library/<encoded-relative-path> resolves only under library where required by the operations snapshot. Reject traversal, reparse escape, sidecars, configs, archives, and non-image extensions.

Use http.server.ThreadingHTTPServer or a ThreadingMixIn equivalent with daemon request threads and allow_reuse_address. Stream files in bounded chunks. Add CSP, Referrer-Policy no-referrer, and X-Content-Type-Options nosniff without breaking current inline dashboard script/style.

Error contract: malformed input 400, unknown/denied paths 404 (403 only for recognized denied routes), missing DB/cache/source or transient SQLite/Pillow failures 503, and unsupported method 405 with Allow.

### Part 3 — Preserve the operations dashboard

Update reports/_build_dashboard.py and reports/_render_dashboard.py together:

- Replace direct fetches of ../.wallpaper-download-queue/state.json with /api/operations/status.
- Consume pre-aggregated counts; do not reconstruct them from raw jobs.
- Emit explicit /media/preview or /media/library URLs for the bounded recent gallery, encoding every segment.
- Use POST for pause, resume, and rebuild; status polling stays GET.
- Preserve snapshot tables/charts, pause controls, live counters, gallery filters, and navigation to /library.
- Make input/output roots explicit so generation can target a temporary
  directory in tests. Generated download-queue-dashboard.html remains ignored.
- Update reports/watch_dashboard.ps1 to launch the in-repository server source
  while passing explicit live runtime roots.
- Regenerate live download-queue-dashboard.html only after
  Get-VerifiedMachineIdentity.ps1 returns VERIFIED for snd-host. Generation
  must not start/stop the server or alter queue state.

Do not expose _snapshot.json, logs, CSVs, PIDs, scripts, diagnostics, or README files through HTTP.

### Part 4 — Add regression coverage

Create tests/test_gallery_thumbnails.py covering stable cache identity, transform-version separation, landscape/portrait/transparent/EXIF fixtures, no upscaling, dimension/pixel caps, cache hit without mtime rewrite, invalid SHA, corrupt/unsupported image, oversized source, encoder-failure cleanup, and concurrent calls.

Create tests/test_dashboard_server.py. Import the in-repository
reports/dashboard_server.py with importlib.util, pass temporary roots/managers,
and use an ephemeral loopback server. Cover:

- all allowlisted static/API routes;
- denial of /.wallpaper-download-queue/state.json, /.env, .env backups, anime_pictures_config.json, zerochan_config.json, wallpaper_library.sqlite, logs, CSVs, Python files, traversal, encoded traversal, and arbitrary report files;
- sanitized JSON contains no jobs, URL, args, path, credential, cookie, or raw state;
- thumbnail cache-hit headers and original streaming;
- outside-root DB rows, unsupported extension, missing file, malformed ID/SHA, and method restrictions;
- rebuild-status ordering, same-origin mutations, transfer preservation, and concurrency.

Tests must not bind port 8090, read live secret bodies, mutate the live DB, or write the live cache.

### Part 5 — Documentation and tracker

Update reports/README_live_dashboard.md with the exact allowlist, sanitized status shape, thumbnail/original routes, cache root/version/lifecycle, stopped-server cleanup, loopback/threading behavior, start/stop, and rollback. State that loopback remains a local trust surface.

Add WP-GALLERY-SERVE-001 to live-tracker.md. Record focused/full tests and whether any live server/cache/database state changed. Do not call live verification complete unless identity and HTTP probes ran.

---

## Constraints

- Modify only listed files. library-browser.html, index_library.py, and library_browser.py are read-only dependencies owned later.
- Preserve unrelated dirty edits, especially pyproject.toml, dependency, report, and tracker content.
- Never move, rename, delete, rewrite, sort, deduplicate, or quarantine an image or sidecar.
- Do not migrate or update wallpaper_library.sqlite.
- Do not start/stop the live listener, create the live cache, regenerate live HTML, or touch pause/rebuild state until the verifier returns VERIFIED.
- Never expose a generic static root even if listings are disabled.

---

## Verification

Run:

    python -m compileall -q src tests
    python -m pytest -q tests/test_gallery_thumbnails.py tests/test_dashboard_server.py
    python -m pytest -q
    git diff --check -- reports/dashboard_server.py reports/_build_dashboard.py reports/_render_dashboard.py reports/README_live_dashboard.md reports/watch_dashboard.ps1 src/dl_engine/gallery_thumbnails.py tests/test_gallery_thumbnails.py tests/test_dashboard_server.py pyproject.toml requirements.txt live-tracker.md

For live smoke only, first run:

    & 'C:/Users/Dev/OneDrive/common/common_dev/Get-VerifiedMachineIdentity.ps1'

Require VERIFIED, snd-host, and SND-HOST\Dev. Use header-only probes for denied paths and bounded GETs for allowlisted API/thumbnail routes. Record status and bytes without reading secret bodies.

---

## Do NOT

- Serve the collection root or reports directory as a generic directory.
- Derive a live collection/database/queue/cache path from the repository or
  worktree parent.
- Return raw jobs or accept filesystem paths over HTTP.
- Use GET for pause, resume, rebuild, transfer creation, or suggestion review.
- Key thumbnails by path/mtime alone, trust caller SHA, or write outside the derived cache.
- Silence decompression warnings without an explicit bound.
- Clean unrelated files, logs, caches, images, sidecars, queues, or rows.

---

## Post-completion

Update live-tracker.md:

| ID | Status | Owner | Scope | Issue | Update |
|---|---|---|---|---|---|
| WP-GALLERY-SERVE-001 | Done or Working | agent-wpd | allowlisted server, sanitized operations API, SHA thumbnail cache | Remove broad exposure and establish lightweight card serving | Record files, tests, identity gate, live probe result, and deferred restart/cache generation. |
