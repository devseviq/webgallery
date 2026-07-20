# Wallpaper Download Dashboard - Live Mode

The `download-queue-dashboard.html` is the live operations dashboard. The
KPIs at the top, the ribbon, the failure / running / pending badges, the
worker heartbeat, and the "today" tile are all kept in sync with
`F:\Wallpapers\.wallpaper-download-queue\state.json` while the page is
open. The detailed tables and charts are still labelled as the snapshot
they were last built from.

## Live-mode controls (in the header)

- **Live dot + status text** (e.g. `Live * refreshing every 30s`).
- **Every [10s | 15s | 30s | 60s | 2m | 5m]** - poll interval. Choice is
  persisted in `localStorage` (key `wpp.intervalMs`).
- **Play / Pause Live** - master switch. Persisted in `localStorage`
  (key `wpp.liveOn`).
- **Refresh** - fetch `state.json` immediately.
- **Rebuild & Reload** - call `/_rebuild` against the local server,
  then reload the page so the charts and tables are also fresh.
- **`now HH:MM:SS`** - local clock that ticks every second.

Live mode is **ON by default** on every visit, unless the user has
paused it (or the URL has `?live=0`).

## URL parameters

- `?live=0` - start paused.
- `?live=1` - force start live (redundant, since it's default).
- `?interval=15000` - set the poll interval to 15s for this visit.

## Server (the `/_rebuild` endpoint)

The Rebuild & Reload button needs a local server. Two options:

### A) `dashboard_server.py` (recommended)

A tiny Python server that serves `F:\Wallpapers\reports\` and exposes
`/_rebuild` (which runs `_build_dashboard.py` then `_render_dashboard.py`).

```powershell
# in F:\Wallpapers\reports\
python dashboard_server.py            # default: http://127.0.0.1:8090/
python dashboard_server.py --port 9000
```

Then open `http://127.0.0.1:8090/` in a browser and the Rebuild button
will work.

### B) `watch_dashboard.ps1` (background, no browser needed)

Runs the snapshot+HTML pipeline in the background on a schedule. Useful
for keeping the disk copy fresh even when nobody is watching the
dashboard.

```powershell
# in any directory
powershell -ExecutionPolicy Bypass -File F:\Wallpapers\reports\watch_dashboard.ps1 `
    -IntervalSeconds 120      # 2 minutes (+/- 15s jitter)

# run once and exit (good for Task Scheduler)
powershell -ExecutionPolicy Bypass -File F:\Wallpapers\reports\watch_dashboard.ps1 -Once

# stop
powershell -ExecutionPolicy Bypass -File F:\Wallpapers\reports\watch_dashboard.ps1 -Stop
```

Logs go to `F:\Wallpapers\reports\_watch.log`. The current watcher pid
is in `F:\Wallpapers\reports\_watch.pid` so `-Stop` is idempotent.

## How live polling works

The dashboard fetches `../.wallpaper-download-queue/state.json?cb=<ts>`
on the configured interval, recomputes the totals and KPI tiles, and
updates the ribbon without reloading the page. Failed fetches are
reported via the status text and the live dot turns red; the rest of
the page keeps showing the last good data.

The script also reads `state.json` directly from disk when the page is
opened via `file://`, so the dashboard is functional even without the
local server - you just don't get the on-demand rebuild.

## Manual full rebuild

If neither A nor B is set up, the same two commands work standalone:

```powershell
cd F:\Wallpapers\reports
python _build_dashboard.py
python _render_dashboard.py
```

This regenerates `_snapshot.json` and the two HTML dashboards.

## Preview gallery (live dashboard)

The live dashboard has a **Last downloaded** gallery with up to 160 recent
images. Discovery is dynamic across every top-level folder under
`temp_downloads`, plus a bounded sample from the canonical `library` so manual
or other non-queue/non-list wallpapers are not omitted.

The source filters use explicit Queue Browser labels:

- **Queue previews**: `temp_downloads/anime-pictures`
- **Queue originals**: `temp_downloads/anime-pictures-full`
- **Sorted library / other**: canonical library images outside active intake
- Any additional `temp_downloads/<folder>` source is added automatically.

The selection reserves space for every available source before filling the
remaining tiles by newest local arrival time. This prevents a busy source from
crowding Queue Browser originals or manual/library images out of the gallery.
Downloads that preserve an old server timestamp use the newer local creation
time for arrival ordering.

Image and video tiles show source, dimensions when available, resolution bucket, orientation, relative
arrival time, and file size. Anime-Pictures dimensions are recognized from both
underscore- and hyphen-delimited filenames. Filters narrow by source,
orientation, and resolution. The Sort control supports newest/oldest arrival,
largest/smallest file, highest resolution, and source/folder ordering; choices
persist locally and can be shared with the `g_sort` URL parameter.

The gallery is recomputed at every snapshot rebuild; the watcher in
`watch_dashboard.ps1` keeps it fresh within roughly two minutes of new files
landing.

### HTTP serving note

`dashboard_server.py` is now rooted at `F:\Wallpapers` (was
`F:\Wallpapers\reports`) so the gallery can pull
`../temp_downloads/...` and `../library/...` over HTTP. `/`
redirects to `/reports/download-queue-dashboard.html`, and
directory listings are disabled.

## Library browser SCP transfers

Open `http://127.0.0.1:8090/library` to select indexed wallpapers while
scrolling or sorting, then use **Send selected**. The browser sends only image
IDs and a named target to the local server. The server resolves those IDs back
to existing files under `F:\Wallpapers\library` and invokes OpenSSH `scp`
without a shell.

Targets are configured in:

```text
F:\Wallpapers\.wallpaper-transfer-targets.json
```

The configuration is re-read for each new transfer, so changing it only needs
a page reload. Its version-1 shape is:

```json
{
  "version": 1,
  "targets": [
    {
      "id": "movedir",
      "label": "movedir on 192.168.2.2",
      "destination": "sev@192.168.2.2:f:\\movedir",
      "machine_id": "snd-desk",
      "verifier_path": "C:\\Users\\Sev\\OneDrive\\common\\common_dev\\Get-VerifiedMachineIdentity.ps1"
    }
  ]
}
```

The target file is explicitly blocked from static HTTP serving. Target IDs,
labels, and destinations are server-side configuration; the HTML cannot submit
an arbitrary destination, path, SCP option, or command. Transfers run in the
background, one batch at a time, with a maximum of 100 files per batch.

Immediately before each SCP batch, the server runs the configured verifier over
SSH and requires both `status: VERIFIED` and the configured `machine_id`. A
failed, unreachable, or mismatched identity stops the job before any file is
sent.

This is deliberately **copy-only**. A successful transfer does not remove or
re-index the originals. SCP uses batch mode, so SSH keys and host-key trust must
already work non-interactively for the configured destination; otherwise the
job fails and the selected wallpapers remain selected for retry.
