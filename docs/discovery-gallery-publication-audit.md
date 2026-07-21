# Discovery — Gallery Publication Audit

**Date:** 2026-07-21
**Scope:** Read-only review of the Plan 003 handoff, the blocked schema-4
candidate, and the contracts required before a canonical gallery-index cutover.

## Current evidence

- The canonical `F:\Wallpapers\webgallery_library.sqlite` remains the schema-3
  legacy database. Plan 003 repository changes do not authorize migrating or
  replacing it.
- The retained diagnostic candidate
  `F:\Wallpapers\webgallery_library.schema4.20260721T003322Z.candidate.sqlite`
  has SHA-256
  `F138D243A8A7DD2BE4164CAB3461D27517699C82EDEEA6829A429A7DA211B5D3`.
- Its exhaustive report at
  `F:\Wallpapers\reports\maintenance-webgallery-candidate-20260721T003322Z\verify.json`
  has SHA-256
  `0E02210EB48A4B406286FE90BA9499AC677F95A68C3BE27948F0EC1BA477E896`,
  `ok=false`, and exactly 200 `layout-mismatch` issues. It has no publication
  manifest and is not a publishable input.
- Bounded alternate-8091 diagnostics were useful UI evidence only. They cannot
  replace exhaustive verification or authorize a live cutover.

## Contract gaps found in review

1. A candidate verified before the maintenance hold can become stale. Apply
   therefore needs a fresh exhaustive verification and durable-input fingerprint
   comparison after the externally owned hold and zero-writer window exist.
2. Main, WAL, and SHM files cannot be replaced as one filesystem-atomic set.
   The honest contract is a recoverable journaled transaction with a hashed
   backup and one same-volume atomic replacement of the closed main database;
   unexpected sidecars or open handles fail closed.
3. An interrupt after the first canonical mutation must attempt rollback before
   `KeyboardInterrupt` or `SystemExit` is re-raised. Narrow activation cleanup
   may catch `BaseException`; it must never swallow it.
4. The publisher does not own an externally acquired queue hold. It may return
   `release_eligible=true` only after canonical verification, but it never
   releases, resumes, disables, stops, or kills queue/process/task state.
5. Candidate-browser QA needs unique cache, report, environment, and queue roots
   so even a blocked diagnostic run cannot write through live runtime paths.

## Recommended status vocabulary

Keep task-manager lifecycle state separate from operational truth:
`planned`, `implemented`, `repository-verified`, `candidate-blocked`,
`ready-to-publish`, `published`, `cut-over`, and `rolled-back`. A plan marked
`executed` means its agent tasks were registered; it never proves publication.

## Boundary

This audit changed no database, media, sidecar, ledger, queue, task, process,
listener, or cache. Layout reconciliation remains owned by the existing
maintenance workflow at a separately verified zero-child boundary.
