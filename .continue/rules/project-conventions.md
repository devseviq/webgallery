---
name: webgallery project conventions
alwaysApply: true
---

# webgallery conventions

This repository contains the portable application, tests, and documentation for
the local wallpaper gallery. It does not own the wallpaper collection.

## Boundaries

- \`src/dl_engine/\` contains the index, rating, transfer, and gallery helpers.
- \`reports/\` contains server, generator, and browser source. Generated
  dashboards and runtime snapshots are ignored.
- \`tests/\` must use synthetic temporary databases and images.
- Canonical images, \`.wallpaper.json\` sidecars, provider ledgers, queues,
  SQLite files, credentials, caches, and operational logs remain outside Git.
- Runtime collection, database, environment, queue, and cache paths must be
  explicit configuration. Never infer live state from a Git worktree parent.

## Safety

- Gallery code may read canonical images and sidecars but never move, rename,
  delete, rewrite, or physically regroup them.
- SQLite is a rebuildable index. Provider ledgers and sidecars remain durable
  evidence.
- Unknown content is never promoted to SFW because evidence is absent.
- Visual-model tags remain suggestions with confidence and provenance; they do
  not become provider tags or rating evidence through review.
- Before starting/restarting a live server or mutating a live database, cache,
  queue, or provider ledger on SND-HOST, run
  \`Get-VerifiedMachineIdentity.ps1\` and require \`VERIFIED\`.

## Python and web style

- Require Python 3.10+ and use \`pathlib.Path\`.
- Type-hint public functions and keep filesystem containment checks fail-closed.
- Prefer standard-library browser/server code; Pillow is the thumbnail
  dependency.
- Build untrusted DOM content with \`textContent\` and DOM methods, not
  \`innerHTML\`.
- Mutating HTTP actions use same-origin POST; read endpoints expose no absolute
  filesystem or database paths.

## Verification

- Compile: \`python -m compileall -q src reports tests\`
- Tests: \`python -m pytest -q\`
- Run live probes only against an alternate loopback listener after identity
  verification.
