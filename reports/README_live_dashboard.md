# Wallpaper gallery and operations server

The local server presents two deliberately separate surfaces:

- `/reports/download-queue-dashboard.html` is the generated operations view.
- `/reports/library-browser.html` is the full indexed-library browser.

The application is loopback-only by default, but loopback is still a local
trust surface. Other programs and users on the machine may be able to connect.
The server therefore has no generic filesystem root and never falls back to a
directory-serving handler.

## HTTP allowlist

Only the following GET/HEAD routes are supported:

- `/` redirects to `/reports/download-queue-dashboard.html`.
- `/library` redirects to `/reports/library-browser.html`.
- `/reports/download-queue-dashboard.html`, `/reports/dashboard.html`, and
  `/reports/library-browser.html` serve those three named report assets only.
- `/api/operations/status` serves sanitized queue aggregates.
- `/api/library`, `/api/library/status`, `/api/library/facets`,
  `/api/library/tags`, `/api/library/transfers`, and
  `/api/library/transfers/<job-id>` preserve the gallery/query/transfer API.
- `/_pause_status` and `/_rebuild_status` report control state.
- `/thumb/<64-hex-sha256>.webp` serves or creates one indexed cached thumbnail.
- `/original/<positive-image-id>` streams one indexed canonical image.
- `/media/preview/<encoded-relative-image>` serves an image beneath the
  configured preview root.
- `/media/library/<encoded-relative-image>` serves an image beneath the
  configured canonical library root for the bounded operations preview.

Mutations are same-origin JSON POST requests only:

- `/_pause`, `/_resume`, and `/_rebuild` (optionally `?wait=0` or `?wait=1`).
- `/api/library/transfers`.
- `/api/library/suggestions/<positive-suggestion-id>` (reserved for the
  reviewable suggestion workflow).

Unsupported methods return `405` with `Allow`. Malformed identities return
`400`; unknown or denied paths return `404` (or `403` for a recognized denied
route); missing runtime dependencies and transient SQLite/Pillow failures
return `503`.

There is no route for `_snapshot.json`, PID files, README/source scripts, logs,
CSVs, environment/config files, queue state, SQLite files, backups, sidecars,
archives, or arbitrary report/collection paths. Encoded traversal and
filesystem/reparse-point escapes are rejected.

## Sanitized operations status

The live dashboard polls `/api/operations/status`; it never fetches the queue
state file. The response has this shape:

```json
{
  "ok": true,
  "schema_version": 1,
  "counts": {
    "total": 0,
    "completed": 0,
    "failed": 0,
    "pending": 0,
    "running": 0,
    "removed": 0
  },
  "failed_by_handler": {},
  "completed_today_utc": 0,
  "updatedAt": null,
  "lastWorkerAt": null,
  "lastMessage": "",
  "paused": false
}
```

`lastMessage` is limited to 80 display characters. The endpoint never emits
jobs, URLs, command arguments, credentials, cookies, target paths, raw queue
rows, the queue filename, or other filesystem paths. Charts and detailed job
tables in the generated page remain labelled build-time snapshots; live
polling updates only the aggregate counters and heartbeat.

## Thumbnail and original lifecycle

Thumbnail identity comes from the read-only indexed SHA-256, not from a client
path or a request-time hash of the original. Version 1 thumbnails use a
sharded key such as:

```text
<thumbnail-cache-root>/v1/ab/<sha256>-640x640-q82.webp
```

The transform applies EXIF orientation, preserves aspect ratio, never
upscales, caps source pixels, strips metadata, and atomically publishes WebP
output. Concurrent requests for one key share a per-key lock. Successful
responses use immutable cache headers and `X-Content-Type-Options: nosniff`.

The cache is derived and disposable; canonical images, sidecars, provider
ledgers, queues, and SQLite remain authoritative. Do not prune during request
handling. To clean or replace a cache version, stop the server first and remove
only the explicitly configured thumbnail-cache root.

Originals are not accepted as paths. `/original/<image-id>` looks up the ID in
the configured read-only database, proves the resolved file remains beneath
the canonical library root, checks its image extension, and streams it in
bounded chunks. The two `/media/` routes likewise accept only segment-encoded
relative image paths under their configured roots; video files such as WebM
and MP4 are not served.

## Starting the allowlisted server

Run the identity verifier before a live start/restart or any live cache,
database, queue, or provider-ledger mutation:

```powershell
$identity = & 'C:\Users\Dev\OneDrive\common\common_dev\Get-VerifiedMachineIdentity.ps1'
if ($identity.status -ne 'VERIFIED' -or $identity.machineId -ne 'snd-host') {
    throw 'Refusing live gallery start: SND-HOST identity is not verified.'
}
```

Require `VERIFIED`, machine ID `snd-host`, computer `SND-HOST`, and account
`SND-HOST\Dev`. Then launch the in-repository source with every runtime root
named explicitly:

`F:\Wallpapers\wallpaper_library.sqlite` is not the gallery database. It is a
schema-2 index owned by the sibling `F:\Wallpapers\dl-engine` scheduled
maintenance path. The allowlisted gallery must use the separately owned
schema-3 `F:\Wallpapers\webgallery_library.sqlite`; see
`docs/INDEX_LIBRARY.md` for its rebuild and verification gate.

```powershell
$repo = 'F:\Wallpapers\webgallery'
$live = 'F:\Wallpapers'
$python = "$repo\.venv\Scripts\python.exe"

& $python "$repo\reports\dashboard_server.py" `
  --app-reports-root "$repo\reports" `
  --collection-root $live `
  --library-root "$live\library" `
  --database-path "$live\webgallery_library.sqlite" `
  --queue-state-path "$live\.wallpaper-download-queue\state.json" `
  --report-output-root "$live\reports" `
  --thumbnail-cache-root "$live\.gallery-thumbnail-cache" `
  --preview-root "$live\temp_downloads" `
  --pause-flag-path "$live\.wallpaper-download-queue\pause.flag" `
  --transfer-config-path "$live\.wallpaper-transfer-targets.json" `
  --environment-path "$live\.env" `
  --host 127.0.0.1 `
  --port 8091
```

The server uses a reusable `ThreadingHTTPServer` with daemon request threads so
one image or thumbnail response does not serialize unrelated API requests.
Port 8091 is the alternate-listener smoke target and does not replace the
existing listener. Stop it with Ctrl+C in its owning console. A deliberate
cutover to port 8090 requires stopping the old listener first; never run two
listeners on the same port.

## Snapshot generation and watcher

`_build_dashboard.py` and `_render_dashboard.py` are import-safe and have no
live defaults. The server's rebuild route invokes them with explicit roots.
For a standalone rebuild, run:

```powershell
$repo = 'F:\Wallpapers\webgallery'
$live = 'F:\Wallpapers'
$out = "$live\reports"
$python = "$repo\.venv\Scripts\python.exe"

& $python "$repo\reports\_build_dashboard.py" `
  --collection-root $live `
  --queue-state-path "$live\.wallpaper-download-queue\state.json" `
  --library-root "$live\library" `
  --preview-root "$live\temp_downloads" `
  --report-output-root $out `
  --pause-flag-path "$live\.wallpaper-download-queue\pause.flag"

& $python "$repo\reports\_render_dashboard.py" `
  --snapshot-path "$out\_snapshot.json" `
  --output-path "$out\download-queue-dashboard.html"
```

The generated preview contains only image entries. Every URL is an explicit
`/media/preview/` or `/media/library/` URL with each path segment encoded; the
snapshot contains no absolute media path. Embedded snapshot JSON escapes data
that could otherwise terminate the surrounding script element.

The watcher always runs builder/renderer source from the repository and sends
the same explicit runtime roots:

```powershell
powershell -ExecutionPolicy Bypass `
  -File 'F:\Wallpapers\webgallery\reports\watch_dashboard.ps1' `
  -CollectionRoot 'F:\Wallpapers' `
  -QueueStatePath 'F:\Wallpapers\.wallpaper-download-queue\state.json' `
  -LibraryRoot 'F:\Wallpapers\library' `
  -PreviewRoot 'F:\Wallpapers\temp_downloads' `
  -ReportOutputRoot 'F:\Wallpapers\reports' `
  -PauseFlagPath 'F:\Wallpapers\.wallpaper-download-queue\pause.flag' `
  -IntervalSeconds 120
```

Use `-Once` for one cycle. To stop the watcher, pass `-Stop` and the same
`-ReportOutputRoot`. Its duplicate-instance check reads the previous PID before
publishing the new PID; stale PID files are removed safely.

## Rollback

1. Stop the allowlisted server and watcher.
2. Restore the prior server/generator source and the last known generated HTML.
3. If desired, remove only the dedicated thumbnail-cache root while the server
   is stopped.
4. Restart the prior listener only after the identity gate and bounded probes.

The server does not migrate its configured database or alter canonical media.
Live cutover, live cache generation, listener restart, and live HTTP
verification remain gated until the separate schema-3 gallery database has
been published and verified.
