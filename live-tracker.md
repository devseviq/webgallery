# Wallpaper DL Engine Work Tracker

This table records completed or active campaign work registered through the project task manager.

**Current operational state (verified 2026-07-16):** The bulk-sort batch is
complete and the rebuilt canonical library verifies cleanly. Maintenance is
`degraded` only for one stale 5,406,736-byte `.part`; there is no remaining
eligible sorter work.

| ID | Status | Owner | Scope | Issue | Update |
|---|---|---|---|---|---|
| WP-VERIFY-001 | Done | agent-wpa | `index_library.py`, index tests, index/metadata docs | Add read-only canonical-library verification | Added stable `--verify-json` and exit-code contracts. The metadata/index selection now passes 110 tests; the rebuilt index has 35,658 canonical images and verifies with `ok=true`, zero issues. |
| WP-MAINT-001 | Done | agent-wpb | maintenance command and isolated tests | Add safe, auditable intake finalization | Added preview/apply phase reports and atomic summaries; isolated suite passes, with active downloaders, inspection failure, or lock contention safely returning `deferred`. |
| WP-QUEUE-001 | Done | agent-wpc | queue, installer, queue tests, README, queue guide | Wire guarded periodic finalization | Added zero-child maintenance, additive queue state, and an exact six-hour installed action; queue suite passes and reinstalling with interval `0` is the rollback. |
| WP-SORT-001 | Done | sorting agents | canonical sorter, maintenance wrapper, regression suites | Harden and execute the bulk sort | All six discovery `implement-now` items are present. SortDownloads and WallpaperLibraryMaintenance suites pass. A 9,048-row / 50.318-GiB preview was fully applied by `maintenance-20260716T1313071695720Z-p10148-f7354787`: 6,091 canonical moves, 2,957 exact duplicates quarantined, and 149 empty directories pruned. |
| WP-ORIENT-001 | Done | sorting agents | legacy orientation sorter and tests | Close filesystem-link escape and report-publication gaps | Added report hardlink replacement, report symlink rejection, and output junction/reparse-point guards; SortByOrientation tests pass. |
| WP-QUEUE-RECOVERY-001 | Done | queue recovery agent | queue supervisor and regression suite | Make interrupted-job retry state truthful | The next lock-holding worker already converts stale `running` rows to failed. Recovery now preserves the consumed attempt count but writes a delayed retry only below `MaxAttempts`; terminal rows have no misleading retry timestamp. The queue suite passes. |
| WP-PART-001 | Follow-up | operator | `temp_downloads\zerochan\Sweatdrop,Red Hair\4606461.jpg.part` | Resolve the sole degraded maintenance item | Read-only inspection confirmed a JPEG header but no required end-of-image marker, so the 5,406,736-byte payload is truncated and must not be renamed or ingested. Re-download or quarantine it under the recovery policy; the post-Apply preview `sort-downloads-post-apply-20260716-143624.csv` has zero rows. |
