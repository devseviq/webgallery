# webgallery

A local-first wallpaper gallery with a rebuildable SQLite index, explicit
allowlisted HTTP routes, cached thumbnails, metadata discovery, and reviewable
tag suggestions.

The repository contains application code, tests, and documentation only.
Wallpaper originals, sidecars, databases, queues, credentials, generated
dashboards, and caches remain outside Git.

## Development

The current SND-HOST development environment uses standard CPython 3.14. The
package minimum remains Python 3.10; free-threaded Python needs its own
compatibility pass and is not implied by the standard 3.14 test result.

    py -3.14 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
    .\.venv\Scripts\python.exe -m pytest -q

Runtime collection paths are supplied explicitly; see
`reports/README_live_dashboard.md`.
