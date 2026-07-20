# webgallery

A local-first wallpaper gallery with a rebuildable SQLite index, explicit
allowlisted HTTP routes, cached thumbnails, metadata discovery, and reviewable
tag suggestions.

The repository contains application code, tests, and documentation only.
Wallpaper originals, sidecars, databases, queues, credentials, generated
dashboards, and caches remain outside Git.

## Development

    python -m pip install -e ".[dev]"
    python -m pytest -q

Runtime collection paths are supplied explicitly; see
\`reports/README_live_dashboard.md\`.
