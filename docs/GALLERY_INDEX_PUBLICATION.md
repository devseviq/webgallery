# Gallery Index Publication

This document is the operator and implementation contract for publishing the
schema-4 webgallery index on SND-HOST. It does not authorize a live cutover.
Until Plan 004 implementation and verification are complete, the commands shown
below describe the required interface rather than a currently approved live
procedure.

The gallery database is a rebuildable index. Canonical images, metadata
sidecars, provider ledgers, queue records, and the sibling schema-2 maintenance
database remain durable inputs outside this publication transaction.

## Status vocabulary

Task-manager lifecycle and operational publication state are separate:

| Term | Meaning |
|---|---|
| task-manager `executed` | Agent tasks were registered. It proves no implementation or publication outcome. |
| planned | Contract/specification exists; executable workflow is incomplete. |
| implemented | Repository code exists and its owned tests pass. |
| repository-verified | Full repository compile/test/diff gates pass. |
| candidate-blocked | A candidate or exhaustive report failed a gate. It is diagnostic only. |
| ready-to-publish | Fresh under-hold proof and explicit cutover authorization pass. No canonical byte has changed yet. |
| published | The canonical path reopened, exhaustively verified, and matched the activated candidate. |
| cut-over | The external listener owner launched the verified canonical database on the intended listener. |
| rolled-back | The exact prior database set was restored and verified. |

The manifest state is deliberately narrower and has exactly five values:
`candidate-built`, `candidate-verified`, `ready-to-publish`, `published`, and
`rolled-back`. Before terminal publication, a failed verification leaves the
manifest at its last valid state, records the rejected attempt in
`last_failed_verification`, and records a blocked or failed result; failure never
advances the state. If restoration
fails, the manifest likewise retains its last valid `ready-to-publish` or
`published` state with `result.status=failed`, a failed rollback object,
`release_eligible=false`, and `listener_restore_required=true`.
A later health audit does not rewrite a terminal published manifest; it creates
separate immutable audit/recovery evidence and starts rollback only as a new
anchored recovery operation.

## Authority and fixed SND-HOST roots

The portable Python core accepts explicit temporary paths for tests. The
SND-HOST Apply wrapper must resolve final paths, reject reparse escapes and
aliases, compare volume identity, and pin these production inputs:

```text
Canonical DB:    F:\Wallpapers\webgallery_library.sqlite
Library:         F:\Wallpapers\library
Wallhaven ledger:F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl
Provider ledger: F:\Wallpapers\library\_metadata\provider-enrichment.v1.jsonl
Sibling DB:      F:\Wallpapers\wallpaper_library.sqlite (protected; never a target)
Publication root:F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication
Candidate root:  F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication\candidates
Manifest root:   F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication\manifests
Backup root:     F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication\backups
Journal root:    F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication\journals
Recovery results:F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication\recovery-results
Report root:     F:\Wallpapers\reports\gallery-publication
Queue state dir: F:\Wallpapers\.wallpaper-download-queue
Queue state file:F:\Wallpapers\.wallpaper-download-queue\state.json
Queue pause file:F:\Wallpapers\.wallpaper-download-queue\pause.flag
Hold token file:F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication-hold.json
```

Candidate, manifest, backup-directory, and first-journal paths plus the report,
journal, and recovery-result roots must be explicit and on the `F:` volume.
Evidence-owned report/continuation/result files are create-new children of those
exact roots. A backup directory is one unique child of the backup root. Every mode
rejects the root itself as a file target and rejects a typo or reparse target
outside the allowlist. Collision rules are mode-specific:

- Prepare requires a new candidate, initial report, and manifest path;
- Publish requires that candidate/current manifest/accepted report to exist,
  then requires new under-hold report, canonical report, prior-manifest snapshot,
  backup directory, and first journal-segment targets;
- Recover requires the identified hold, backup, and first journal segment to
  exist and creates only a unique continuation segment/recovery-result target;
  and
- manifest transitions replace the one expected current manifest only after its
  compare-and-swap hash matches.

Literal-path syntax alone is not sufficient protection.

Before any live-capable invocation, the wrapper runs:

```powershell
& 'C:\Users\Dev\OneDrive\common\common_dev\Get-VerifiedMachineIdentity.ps1'
```

It requires `VERIFIED`, machine ID `snd-host`, computer `SND-HOST`, account
`SND-HOST\Dev`, and the enrolled installation identity. The portable core does
not infer machine identity from paths, environment, or repository history.

## Manifest identity

`schemas/gallery-publication-manifest.schema.json` is JSON Schema Draft 2020-12
and manifest-format version 1. Unknown properties are rejected at every owned
object boundary. These versions are independent:

- `manifest_schema_version` describes this publication manifest and is `1`;
- `semantic_contract_version` describes cross-field validation and canonical
  digest rules and is `1`;
- `verification.report_format_version` describes the exhaustive report and is
  currently `1`;
- `activation.journal.journal_format_version` describes recovery JSONL and is
  `1`;
- `activation.journal.recovery_result.document.recovery_result_schema_version`
  describes emergency recovery output, when present, and is `1`;
- `candidate.sqlite.pragma_user_version` describes SQLite and must be `4`;
- `candidate.sqlite.metadata_schema_version` is the
  `schema_metadata.schema_version` value and must also be `4`.

The verifier report's top-level `schema_version=1` is report-format evidence,
not proof of a schema-4 database. The publisher reads SQLite markers itself and
requires both markers to agree.

`table_counts` is the one deliberately data-keyed object: its keys are observed
SQLite table names and its values are nonnegative counts. Prior-canonical and
rollback identities may describe an earlier healthy schema; schema-4 candidate,
activation-after, and post-publish identities additionally require `images`,
`tags`, `image_tags`, `tag_suggestions`, and `library_facets` counts.

Every advancing rewrite archives the previous manifest as a unique immutable
file under the manifest root. `state_transition.from_state` names that archived
snapshot, `previous_manifest` carries its full file identity, and
`state_transition.entered_at` records the new state. `candidate-built` alone has
a null source/snapshot. The semantic validator recursively validates the prior
snapshot chain, rejects cycles or missing bytes, recomputes every snapshot hash,
and requires only these edges: built to verified, verified to ready, ready to
published/rolled-back, and published to rolled-back. Updating failure evidence
without advancing state leaves the established transition edge unchanged.
Each rewrite uses compare-and-swap semantics: archive and flush the exact current
bytes, rehash the current manifest immediately before replacement, abort if it
changed, then atomically replace the manifest file with a flushed temporary on
the same volume. Concurrent or missing prior snapshots fail closed.

`verification` is the last accepted successful verification. It may be null at
`candidate-built`; `last_failed_verification` retains the most recent rejected
attempt without overwriting accepted evidence. A failed attempt has a nonzero
exit, `ok=false`, a nonzero issue count, or a nonempty taxonomy and never
advances state. `verification.verified_under_hold` may be false for the initial
`candidate-verified` transition, but the schema requires it to be true for
`ready-to-publish`, `published`, and `rolled-back`, as well as for the nested
post-publish verification.
`activation.main_replace_same_volume_atomic=true` describes only the main-file
replace; `activation.multi_file_atomic_claim` is always false.

Verification reports are immutable evidence-owned files under
`paths.verification_report_root`, never one reused output. Candidate preparation
writes an initial report, the ready transition writes a distinct fresh
under-hold report, each failed attempt writes another unique report, and
post-publish verification writes a distinct canonical report. Archived manifest
snapshots continue to name and hash their original reports. Names include the
generation ID, phase (`initial`, `under-hold`, `failed`, or `canonical`), and a
unique attempt ID; creation uses create-new semantics and never overwrites.

Every present file carries its literal path, final resolved path, byte length,
SHA-256, UTC mtime, and volume identity. An absent WAL or SHM is represented by
a complete absent-file identity; a boolean such as `wal_present=false` is not
enough. Candidate and canonical identities cover the main, WAL, and SHM set.

The manifest also binds:

- library and sidecar inventory fingerprints;
- Wallhaven and generic provider-ledger fingerprints;
- candidate generation ID and aggregate durable-input hash;
- exhaustive report bytes, generation time, exact database/library paths,
  candidate hash, input hash, exit code, issue count, and taxonomy;
- externally owned hold/token/expiry/task acknowledgement;
- repeated descendant, index-writer, listener, and settled-fingerprint samples;
- the exact pre-cutover 8090 PID, owner, and launch command plus its external
  recovery owner;
- backup, journal, activation, post-publish, rollback, release-eligibility, and
  listener-recovery results.

## Semantic contract and canonical digests

JSON Schema validates closed object shapes, required fields, primitive types,
constants, and state-specific success/failure forms. It cannot prove that two
separate strings or hashes describe the same bytes. Therefore
`semantic_contract_version=1` is mandatory and the WPK standard-library
validator must recompute every equality below. A self-asserted boolean or hash
inside a manifest is never accepted as proof.

Canonical JSON v1 is defined as follows:

1. Unicode strings are normalized to NFC. Path-valued fields are resolved with
   the Windows final-path API, use `\` separators, normalize `.`/`..`, uppercase
   the drive letter, and are case-folded only for comparison and digest input.
   The original literal and resolved final path are both retained in evidence.
   For an absent WAL/SHM or not-yet-created output, resolve the nearest existing
   parent, verify every existing ancestor is non-reparse and inside its allowlist,
   then append the normalized missing leaf segments. That projected final path
   is evidence; the tool never calls final-path APIs on a nonexistent leaf.
2. UTC timestamp fields must match the schema's strict `...Z` grammar, parse as
   a real date-time, and normalize to UTC with exactly nine fractional-second
   digits for digest input by right-padding the parsed 0-9 digit fraction with
   zeros; no digit is truncated. Offset forms, leap-second text, and impossible
   calendar dates are rejected.
3. Canonical objects use sorted keys and no insignificant whitespace. The exact
   encoder is `json.dumps(value, sort_keys=True, separators=(',', ':'),
   ensure_ascii=False, allow_nan=False).encode('utf-8')`. Hash text is lowercase
   SHA-256 of those bytes. Floating-point values are not used in fingerprint or
   journal digest inputs.
4. Library and sidecar inventory records contain normalized relative path,
   byte length, integer `mtime_ns`, and file SHA-256. They are sorted by the
   case-folded normalized relative path and then the original normalized path.
   Symlinks, junctions, reparse points, duplicate case-folded paths, and paths
   escaping the library root are rejected.
5. A ledger fingerprint hashes the canonical envelope
   `{"bytes_sha256":H,"entry_count":C,"exists":true,"length":L,"path":P}`.
   An absent optional ledger hashes the explicit envelope
   `{"entry_count":0,"exists":false,"path":P}`; absence is never an empty-file
   hash. The manifest stores `exists`, entry count, content size, content hash,
   and envelope `sha256`; all must match the current observation. Library and
   sidecar fingerprints use the same envelope around their canonical inventory
   bytes.
6. A database-set aggregate hashes canonical JSON for the named `main`, `wal`,
   and `shm` members, including each complete present/absent identity; sorted-key
   canonical JSON determines byte order. The durable-input aggregate hashes the
   four named input fingerprints and generation ID. No aggregate is copied from
   the manifest without recomputing its components. All emitted SHA-256 text is
   lowercase.

The semantic validator enforces this equality table before any transition:

| Evidence | Required equality or ordering |
|---|---|
| paths | Every literal CLI path equals its corresponding `paths.*` value after normalization; every existing final path equals a fresh final-path resolution and every absent leaf uses the projected-parent rule. Candidate, every evidence-owned report, manifest, prior-manifest snapshots, backup directory, journal segment, and recovery result are descendants of their exact dedicated roots. |
| generation | `candidate.generation_id == durable_inputs.generation_id`; all accepted and failed reports name that generation through the recomputed durable-input aggregate. |
| transition | Current state equals its transition edge; the archived previous-manifest bytes validate recursively, hash correctly, stay under the manifest root, and form one allowed acyclic chain. |
| candidate | Candidate main SHA-256 equals accepted verification `database_sha256`, every settled-sample candidate main hash, and post-publish `candidate_database_sha256`. |
| inputs | `durable_inputs.aggregate_sha256` equals verification and settled-sample input hashes; each inventory/ledger path equals the matching fixed path. |
| reports | Every report file identity is freshly recomputed and its path is a unique descendant of `paths.verification_report_root`. Initial and fresh under-hold candidate reports name the candidate path; post-publish reports name the canonical path; all name the exact library root and no report path/hash is reused or overwritten. |
| issues | Each issue code is unique and `issue_count` equals the sum of taxonomy counts; success requires both to be zero/empty. |
| failed attempt | A nonnull `last_failed_verification` is newer than the accepted attempt it rejected and forces result status `blocked`, `failed`, or `rolled-back`; it cannot coexist with an in-progress/succeeded claim. |
| pre-mutation failure | `pre_activation_failure` is permitted only while retaining ready state, activation/rollback are null, canonical bytes equal the final settled sample, and every partial output is under its dedicated root. |
| hold | `hold_file == paths.hold_path`; pause/state files are exactly `pause.flag`/`state.json` under `paths.queue_state`; parsed token owner/ID/times and token hash equal the manifest; queue state schema is 1 and records pause acknowledgement after acquisition; task path/name are exactly `\` / `Wallpaper Download Queue`; hold/task acknowledgement times agree; its canonical exported-definition hash, observation, pause/state hashes, and token hash recompute `acknowledgement_sha256`. |
| volumes | Canonical, candidate, backup directory, every journal segment, and every database-set member share the verified canonical volume serial. |
| backup | Map canonical `main`, `-wal`, and `-shm` to unique backup children. Compare presence, size, and content SHA-256 plus source SQLite identity; path and mtime deliberately differ. The mapped source equals the final settled canonical sample. |
| activation | Both main paths resolve to the canonical path. Compare candidate to successful activation-after by main size/hash and SQLite identity, not whole identity objects; both WALs are absent/zero and both SHMs are absent. The one main-file replacement stays on the same volume. |
| publication | Post-publish canonical hash equals activation-after and candidate main hashes; its SQLite identity and exhaustive report are recomputed from the reopened canonical path. |
| rollback | Map backup members back to canonical `main`, `-wal`, and `-shm`; compare presence, size, and content hash while allowing path/mtime to differ. Recompute restored SQLite identity and `restored_sha256_matches_backup`. |
| samples | Sample sequence numbers are contiguous, timestamps and elapsed seconds increase, first-to-last duration is at least 30 seconds, and every settled candidate/canonical/input aggregate is identical. |
| listener | Bindings are unique, `listen_count == len(bindings)`, and every before binding belongs to the one captured process PID. The after snapshot covers all local IPv4/IPv6/wildcard bindings, has no listener, and its external acknowledgement precedes authorization. |
| journal | Transaction/generation/manifest anchors agree; segment and record sequences are contiguous; each segment binds its predecessor bytes/head/torn-tail hash; `tail_segment_sha256` equals the last segment file hash; every recovery continuation embeds fresh recovery-boundary evidence; every mutation outcome names exactly one earlier intent; record hashes and the head are recomputed; `last_sequence` equals the final valid record. Status is derived from unresolved terminal work, not trusted. Any segment may contain only its valid header when its first record tears, but completed journal evidence has at least one valid record globally. |
| journal target | `backup` targets only mapped children of the unique backup directory; `main-replace`, `canonical-verify`, `rollback-verify`, and the nonmutating `transaction-abort` control record target only canonical main; `database-sidecar-remove` targets only canonical `-wal`/`-shm`; `rollback-restore` targets only mapped canonical main/WAL/SHM. Media, `.wallpaper.json`, ledgers, queue/hold files, sibling DB, candidate, and arbitrary paths are forbidden. |
| recovery boundary | Every noninitial continuation records purpose, recovery-attempt ID, authorized journal head, current external hold, pause/state identities, scheduled-task observation, integer-millisecond repeated zero-writer samples, all-address zero-listener snapshot, canonical no-handle proof, and verification time. Its new token ID/hash differ from the historical publication hold, which it names without rewriting historical evidence, and it remains unexpired/fresh through each protected recovery mutation. |
| recovery result | File bytes are exactly canonical-JSON-v1 encoding of the inline `document` plus one LF, and the wrapper file identity is recomputed. Attempt/transaction/generation/head/tail/paths and recovery-boundary evidence equal the final journal chain; `journal_tail_segment_sha256` binds the final segment even when it has no records. Terminal observations equal current canonical bytes and match the declared pre-activation/backup basis. It records only the CAS compare hash, pre-CAS observed hash/match, and intended target state. A later manifest that embeds this exact file identity proves CAS reconciliation; absence never becomes a false success claim. |
| time | Creation, original hold acquisition/acknowledgement, window, verification, listener-before/after/acknowledgement, authorization, activation, post-publish/rollback, update, and terminal times are monotonic; evidence is checked immediately before replacement and is at most 300 seconds old. The original hold expires after its original protected operation, while every delayed recovery uses a separately recorded current hold that expires after that recovery operation. |

WPK does not add a generic `jsonschema` runtime dependency. Its closed,
standard-library validator implements the owned schema-v1 keywords plus every
semantic rule above. WPL parity tests load the JSON Schema and prove the manual
validator rejects unknown/missing fields, bad constants, every invalid state
form, invalid UTC values, and cross-field mismatches. Its JSON loader also
rejects duplicate keys, `NaN`/`Infinity`, and Python `bool` values where the
schema requires an integer.

The retained `20260721T003322Z` candidate is not valid publication input. Its
exhaustive report has `ok=false`, exactly 200 `layout-mismatch` issues, and no
required generated timestamp or publication manifest.

## State machine

Only the following transitions are valid:

```text
candidate-built
  -> candidate-verified
  -> ready-to-publish
  -> published

ready-to-publish -> rolled-back
published        -> rolled-back
```

There is no direct built-to-ready, verified-to-published, or failed-to-published
transition. A new build creates a new unique manifest rather than rewinding an
old manifest. `rolled-back` is terminal and valid only with successful fresh
under-hold verification, maintenance and authorization evidence, a verified
backup, a completed activation record, a journal whose terminal status is
`restored`, a byte/SQLite-verified restoration matching that backup, and a
nonnull terminal timestamp. A rollback failure never claims `rolled-back`.

### 1. Candidate built

Preparation reads only explicit library, sidecar, and ledger inputs. It writes
a unique candidate, report target, and manifest outside the canonical database
set. It never migrates or copies the live database in place.

Before `candidate-built` is recorded:

- the candidate connection is closed;
- `quick_check` is `ok`;
- both SQLite schema markers are exactly 4;
- all required schema-4 columns and tables exist;
- the candidate WAL is absent or zero bytes and checkpointed;
- the closed candidate SHM is absent;
- WAL/SHM remnants without a main database are rejected;
- all candidate and durable-input hashes/counts are bound to one generation ID.

Candidate preparation may run while legacy 8090 serves the separate canonical
database. It does not prove freshness for a later Apply.

### 2. Candidate verified

The exhaustive verifier must exit 0 with `ok=true`, zero issues, an empty issue
taxonomy, the exact candidate/database/library paths, the candidate SHA-256,
the durable-input aggregate SHA-256, and a UTC generation timestamp. Quick-check
alone is never sufficient.

Every verifier issue is a blocker, including `layout-mismatch`. Publication code
must not move or rename media, downgrade the verifier, or reinterpret a bounded
8091 UI diagnostic as verification. Layout reconciliation belongs only to the
existing maintenance authority at a separately verified zero-child boundary;
after reconciliation, build and verify a new candidate.

### 3. Ready to publish

Ready-to-publish is evaluated again inside the real maintenance boundary. All
of the following must be true at the same time:

1. exact SND-HOST identity is freshly VERIFIED;
2. an external owner has acquired a non-expired hold and the scheduled workflow
   acknowledges it;
3. at least two samples over a measured interval show zero downloader
   descendants and zero index writers;
4. candidate and canonical main/WAL/SHM fingerprints are stable, both WALs are
   absent or zero/checkpointed, candidate SHM is absent, and canonical SHM has
   no open handle;
5. library, sidecar, and both ledger fingerprints still match the manifest;
6. exhaustive candidate verification is rerun after the hold/window and is
   bound to those current fingerprints;
7. candidate and canonical paths are distinct, non-aliasing, non-reparse paths
   on the same volume as the dedicated backup/recovery root;
8. smoke port 8091 is free;
9. a separate cutover decision is recorded, the external listener owner stops
   8090, and a subsequent sample proves it stopped;
10. the exact pre-cutover listener tuple and external restart owner are retained;
11. canonical handles/sidecars are in the fail-closed state required for backup
    and activation; an identified stale SHM may be handled only after its prior
    identity is journaled and the no-handle proof passes; and
12. Apply and cutover flags are both explicit.

Queue status alone is not proof. A failed queue job may still have living child
processes. The publisher observes processes and tasks but never terminates,
disables, pauses, resumes, or starts them.

Because readiness is recorded only after the external owner stops 8090, a
`ready-to-publish` result has `listener_restore_required=true`. Successful
publication clears restoration of the old launch; rollback or recovery failure
keeps it true for the external owner.

A backup-copy/hash failure, journal creation/first-flush failure, or final
pre-replace revalidation failure occurs before any canonical mutation. It keeps
state `ready-to-publish`, sets `pre_activation_failure` with
`canonical_unchanged=true`, records a fresh full canonical DB/SQLite identity
equal to the final pre-activation sample, records any partial outputs, leaves
activation and rollback null, and returns a terminal failed result with
listener restoration required. A complete verified backup may be retained; a
partial backup is only diagnostic output and never satisfies `backup`. If the failed revalidation was
an exhaustive verifier attempt, the same branch also retains its unique report
in `last_failed_verification` while preserving the earlier accepted proof.
Failures before a valid first intent retain only partial-file evidence. Once a
valid intent exists, the publisher records its error outcome and derives the
terminal journal state `aborted-before-mutation` only after freshly proving the
canonical main/WAL/SHM set still equals the final pre-activation sample. A crash
that leaves an unmatched main-replace or other forward intent uses phase
`pre-canonical-mutation-abort` and remains `recovery-required` until Recover
makes the same observation and closes it backward. An aborted transaction is never
resumed or reused; a later publication attempt starts a new candidate,
generation, manifest, backup directory, and journal while preserving the
aborted evidence.

A journal-create/first-flush failure with no valid header can be committed as
`synchronous-unstarted-failure` only while the original hold and evidence are
still current. A delayed invocation never borrows that expired authority: it
acquires the same fresh `recovery_hold` boundary required by continuations,
sets `closure_kind=pre-journal-recovery-close`, inventories the invalid/partial
output, rechecks the full canonical DB/SQLite set against the archived
pre-activation sample, and compare-and-swaps the failure manifest. With no
valid journal there is no recovery mutation or result document. A CAS conflict
leaves the hold asserted and the partial output quarantined for manual review;
it never creates a terminal claim from uncommitted evidence.

### 4. Published

Publication is a recoverable journaled transaction, not an atomic multi-file
swap:

1. record the exact canonical main/WAL/SHM identities;
2. archive/hash the ready manifest, create the first journal segment bound to
   that manifest/generation, and durably flush its first backup intent;
3. create collision-free same-volume backup names, copy every existing member
   under per-member intent/outcome records, and prove the mapped content matches
   the recorded source set;
4. durably flush the final backup verification and main-replace intent before
   the first canonical mutation;
5. require the candidate closed/checkpointed with an absent-or-zero WAL;
6. reject unexpected canonical WAL/SHM state or any open-handle failure;
7. perform one same-volume atomic replacement of the canonical main file;
8. record each owned sidecar action in the journal; never treat SHM as durable
   data or delete an unidentified sidecar;
9. reopen the canonical path, recompute byte/schema/table identity, and run the
   exhaustive verifier against that canonical path; and
10. close/checkpoint the canonical database, require canonical SHM absent, then
    mark `published` only after the canonical report passes and the canonical
    main size/hash plus schema-4 SQLite identity match the candidate.

No success, `release_eligible=true`, or cutover-complete claim is written before
step 10. The publisher does not start 8090; the external listener owner decides
when and how to do that after publication evidence is returned.

### 5. Rolled back

After the first canonical mutation, the activation boundary catches
`BaseException` only long enough to attempt exact restoration. It restores the
recorded main/WAL/SHM state, reopens and hashes the restored canonical set,
records both the primary and rollback errors when necessary, and then:

- re-raises `KeyboardInterrupt` or `SystemExit` after cleanup;
- reports an ordinary failure only after cleanup;
- leaves the external hold asserted;
- sets `release_eligible=false`; and
- sets `listener_restore_required=true` for the external listener owner.

`activation.error_code` and `activation.error_message` retain the primary
publication failure; `rollback.error` independently retains restoration
failure. A successful rollback may still carry the primary code/message in the
rolled-back result. A failed rollback carries both fields plus the journal's
per-action error record, so the secondary failure never overwrites the trigger.

The publisher never releases the hold or restarts 8090. If rollback itself
fails, the journal and backup remain immutable recovery evidence and the hold
must remain asserted for manual recovery. A later invocation must inspect an
unfinished journal before doing any new work.

## Recovery journal v1

One transaction is an immutable chain of one or more append-only UTF-8 JSONL
segments. The journal header binds `generation_id`, the exact current manifest
path, an immutable full identity of the archived `ready-to-publish` manifest,
and starting state. Recover can therefore restore bytes without candidate
inputs and can still locate and compare-and-swap the correct state chain.

Segment zero is `paths.recovery_journal`. The transaction's first valid record,
wherever it appears, has global sequence zero, a null previous-record hash, and
the unique transaction ID. Every later record repeats that ID and names the
prior valid record hash, including across segments. `record_sha256` is
canonical-JSON-v1 SHA-256 of the record with
`record_sha256` omitted. The journal's `last_sequence` and `head_sha256` cover
the concatenated valid records in segment order; parsed segment records are a
convenience view, never authority.

Each segment records its full file identity, valid-prefix byte length, parsed
records, and any torn-final-line hash/error. Its first complete line is a strict
segment header; `header_sha256` hashes canonical header JSON with that field
omitted. Header transaction/generation/manifest anchors must equal journal
evidence, including the archived ready-manifest path and hash. Segment zero is
kind `initial`, has a null recovery hold, and has null predecessor anchors.
Every continuation has the next segment index, binds the predecessor's full
file SHA-256 and last valid record hash, and embeds a freshly validated
`recovery_hold` boundary. Kind `torn-continuation` also binds the invalid-tail
hash; kind `recovery-continuation` follows a clean sealed segment and has a null
tail hash.
`valid_prefix_durably_flushed=true` applies only to that segment's valid prefix.
Without a tear, valid-prefix size equals full file size and tail/error are null;
with a tear it is smaller and the remaining bytes hash to the recorded tail.
Segment paths are unique children of the journal root and are never reused. A
segment may contain zero valid records after its valid header when the process
stops before record bytes are written or its first record tears. The next clean
or torn continuation anchors to the same last valid global record (null if none
yet), plus the predecessor bytes and any torn-tail hash. Completed journal
evidence has at least one valid record globally, which may be sequence zero in
a later continuation; a header-only tail cannot claim a derived terminal state.

Before each mutation, the publisher appends an `intent` record containing the
observed before state and intended after state, calls the Windows durable file
flush operation, and durably commits the parent-directory entry when creating
the journal. After the mutation it appends and flushes either `complete` with a
fresh observed state or `error` with the error. Backup creation, main replace,
owned sidecar handling, canonical verification, rollback restore, and rollback
verification each receive their own intent/outcome pair. No destructive action
may precede its flushed intent. Each intent has a unique `operation_id` and sets
`intent_sequence` to its own sequence; its complete/error outcome repeats the
operation ID and intent sequence. Intent and error records have `outcome=null`;
intent records also have no observed-after/error, while error records require a
nonempty error. A normal complete record has no error and uses `outcome=applied`
only when observed-after equals intended-after, or `outcome=not-applied` only
when observed-after equals before. Journal status is derived from the last
valid action, phase, outcome, and fresh observations, never accepted as a
self-asserted label.

Recover closes an unmatched mutation intent whose target still equals `before`
with a `not-applied` outcome bound to that intent. After proving that no
canonical-mutating action completed and the canonical set still equals the
pre-activation sample, the publisher or Recover appends one nonmutating
`transaction-abort` complete control record with `outcome=aborted` for that
abort attestation/recovery attempt. It uses its
own sequence as `intent_sequence`, targets canonical main, and records equal
before/intended/observed main identities. The full canonical DB/SQLite equality
is retained in `pre_activation_failure` and any recovery result. This is the
only complete record that does not pair with an earlier mutation intent; its
failure reason remains in the enclosing failure/result evidence.
If a crash occurs after this record is flushed but before its result/manifest
is committed, a later attempt creates a new hold-bound continuation and appends
another equal-state `transaction-abort` attestation as its own final record. It
then writes a new result bound to that new tail; it never reuses or rewrites the
earlier result.

Action authorization is closed: backup records target only the mapped unique
backup children; main replacement/canonical verification target only canonical
main; `database-sidecar-remove` targets only canonical `-wal` or `-shm` after
the recorded no-handle proof; rollback restore targets only mapped canonical
main/WAL/SHM; and rollback verification targets canonical main. No journal can
authorize a media, metadata-sidecar, ledger, queue/hold, sibling-DB, candidate,
or arbitrary-path mutation.

The derived status map is exact:

- `prepared`: the first backup intent is flushed and no mutation is complete;
- `backup-verified`: every backup intent has a matching successful completion;
- `main-replaced`: main replacement and any owned sidecar action are complete,
  with canonical verification not yet complete;
- `canonical-verified`: canonical verification completed successfully and no
  intent is unmatched;
- `rollback-started`: a rollback-restore intent is flushed but rollback
  verification is not complete;
- `restored`: rollback restore and rollback verification both completed and no
  rollback intent is unmatched. This terminal status supersedes historical
  forward error records, which remain immutable evidence;
- `aborted-before-mutation`: every started operation has a terminal outcome, no
  canonical-mutating action completed, and a fresh canonical observation still
  equals the final pre-activation set, with `transaction-abort` as the final
  valid record; and
- `recovery-required`: the latest chain still has an unresolved terminal error,
  torn segment without a valid continuation, invalid observed state, or
  unmatched intent.

Recovery parses every segment from byte zero. An invalid hash, sequence,
transaction/segment anchor, or non-final invalid JSONL record is a hard stop. A
torn final line seals that segment: Recover never appends to or truncates it.
Any segment whose file identity has been written to a manifest/recovery result
is likewise sealed even when clean. After validating the predecessor bytes and
valid prefix, Recover first acquires and validates a current externally owned
hold without changing the historical maintenance evidence. It captures the
current pause/state files, scheduled task, repeated zero-writer window,
all-address zero-listener snapshot, and canonical no-handle proof in the new
torn/recovery continuation. It flushes the continuation header and first
recovery intent, and only then may mutate.
If a continuation tears, another anchored segment is used. Replay is based on
freshly observed hashes.
Emergency Recover only converges backward; it never resumes candidate
activation:

- if an unmatched forward intent's target equals `before`, Recover records that
  the forward action was not applied and either proves terminal
  `aborted-before-mutation` when no canonical mutation completed or proceeds
  only to restoration checks;
- if a forward target equals `intended_after`, Recover records the observed
  completion without repeating it and proceeds to rollback;
- only a rollback-restore or rollback-verify intent may be executed by Recover,
  and only after the current target matches that intent's recorded before state;
- and if it matches neither, Recover stops for manual intervention.

Recover never overwrites a backup, blindly repeats a replace/remove, truncates
a segment, or starts a second transaction. Re-running it after a completed
record is idempotent: it verifies the recorded after state and advances only
the next incomplete rollback action. It writes a unique immutable recovery
result under `paths.recovery_result_root`. That result records the exact
manifest hash accepted as the compare value and the freshly observed manifest
hash, intended target state, recovery attempt ID, record head, and final segment
hash, but never predicts the outcome of a later write. After durably creating
and rehashing the result, Recover constructs a replacement manifest that embeds
the result's full file identity, re-reads the manifest, and attempts one
compare-and-swap from `manifest_cas_expected_sha256` only while the recovery
hold remains valid. The resulting manifest itself proves reconciliation only
when that CAS succeeds. On restart, an exact result identity already embedded
in the manifest proves success; the unchanged expected hash permits a retry
under a valid hold; anything else is a conflict. If the manifest changed before
or during CAS, the standalone result remains authoritative recovery evidence,
no reconciliation-success field exists to become a lie, and manual review is
required. Any other mismatch preserves all segments and backup, leaves the hold
asserted, and returns failed recovery evidence. A fresh continuation created
after result flush always requires a new immutable result bound to its new tail.

Recovery-result v1 is strict JSON containing transaction and generation IDs,
fresh exact machine identity, completion/status, canonical and backup paths,
terminal journal head, terminal DB/SQLite identities, the declared comparison
basis and recomputed match, the exact current recovery-hold boundary and attempt
ID, terminal segment hash, anchored manifest path, CAS expected/pre-CAS observed
hashes and their precondition match, intended target state,
mandatory hold/listener flags, and paired error code/message.
`aborted-before-mutation` requires a verified terminal set matching the
pre-activation basis and a paired publication error; `rolled-back` requires a
verified terminal set matching the backup basis; `failed` requires paired
errors. Its file bytes are the canonical-JSON-v1 document plus one final LF.
Journal evidence wraps the exactly parsed document with a separately recomputed
full file identity, avoiding a self-hash and never trusting a pathname alone.
Recovery-result presence is state-bound: ordinary forward/pre-recovery states
have none; `aborted-before-mutation` may carry only that result status,
`restored` may carry only `rolled-back`, and `recovery-required` may carry only
`failed`. A null result remains valid before Recover attempts terminal closure.

## Hold and listener ownership

The hold owner supplies the token file and creates the queue's authoritative
`pause.flag`; the publisher only validates them. The token is strict canonical
JSON with hold version 1, owner, token ID, acquisition/expiry UTC times, exact
pause-file path/hash, queue-state path/hash, and scheduled-task definition hash.
Unknown token fields, an expired token, or bytes that do not reproduce
`token_sha256` are rejected. The publisher has no API that creates, releases,
or resumes the hold.

The original maintenance hold remains immutable historical evidence. Delayed
Recover never treats its expiry as current authority and never rewrites that
manifest evidence. The external owner supplies a current hold token (which may
reuse the same fixed token path with new bytes), and every recovery
continuation snapshots its exact identity alongside current pause/state/task,
integer-millisecond zero-writer, zero-listener, and no-handle proof. It has
purpose `emergency-recovery`, a unique attempt ID, and the authorized record
head (nullable only before sequence zero). Its token ID/hash differ from the
historical publication token, and `historical_hold_token_sha256` points back to
the original manifest hold. The same recovery boundary is copied into the
terminal recovery result and must remain current and unexpired through result
flush and the immediate CAS attempt.

The publisher also parses exact
`F:\Wallpapers\.wallpaper-download-queue\state.json`, requires queue state schema
1 and a pause acknowledgement written after acquisition, and records the exact
possibly-empty `pause.flag` identity. The only accepted scheduled authority is
task path `\`, name `Wallpaper Download Queue`, whose canonical exported XML
hash and action pass the existing queue-task contract for the pinned queue
script, state directory, queue file, and destination. The acknowledgement hash
is canonical JSON over token, pause, state, task-definition, observation, and
acknowledgement hashes/times. A successful result means only that the external
owner is eligible to release the hold after reviewing publication and listener
state.

Likewise, cutover authorization allows publication only after an external owner
has stopped 8090. The wrapper never calls `Stop-Process`, disables a scheduled
task, or starts a replacement listener. On failure it returns the captured
launch tuple and `listener_restore_required`; recovery remains owned by the
explicit operator/runbook.

Listener snapshots enumerate the port across all local addresses, not only
`127.0.0.1`: IPv4, IPv6, wildcard, and dual-stack bindings are included. The
recorded process identity includes executable final path and hash, qualified
owner, start time, working directory, and argument vector. The before PID must
own a recorded binding, and the external owner must acknowledge the stopped
zero-listener snapshot before authorization is accepted.

## WhatIf and failure behavior

`-WhatIf` is the default operational posture and always wins over `-Apply`. It
performs validation and prints the intended state transition, but creates no
candidate, report, manifest, backup, journal, hold, cache, or temporary file and
changes no task, process, listener, queue, ledger, database, or media byte.
Read-only `Inspect` and `Validate` do not require `-Apply`. Every write-capable
mode (`Prepare`, `Publish`, and `Recover`) refuses to write unless `-Apply` is
present and `-WhatIf` is absent.

Every mode fails closed on its applicable identity, path/final-parent, reparse,
volume, schema/version, duplicate-JSON, and immutable-evidence rules. Prepare
also rejects output collisions, missing durable inputs, bad schema markers,
nonzero WAL, or orphan sidecars. Publish additionally rejects stale/missing
reports, changed inputs, weak/expired holds, living writers, unstable samples,
active 8090, open handles, backup/journal collisions, or any earlier unresolved
transaction. Recover instead requires a freshly acknowledged external recovery
hold plus the identified manifest anchor and journal chain and rejects any
target/evidence mismatch. Exact verified backup evidence is mandatory only
after a canonical mutation completed or when restoration uses the backup basis.
Before mutation, Recover instead proves the full canonical set against the
archived pre-activation sample and inventories any partial backup without
trusting it. A terminal `aborted-before-mutation` transaction is retained but is
not unresolved and cannot authorize reuse of its outputs.

Candidate-only diagnostics never authorize provider/review canaries, transfers,
POST requests, queue mutation, live cutover, or media repair.

## Planned command interface

These examples freeze the Plan 004 interface. They are not live authorization.
All paths are literal and explicit.

```powershell
Set-Location F:\Wallpapers\webgallery
$python = 'F:\Wallpapers\webgallery\.venv\Scripts\python.exe'
$wrapper = 'F:\Wallpapers\webgallery\scripts\Invoke-GalleryIndexPublication.ps1'
$canonical = 'F:\Wallpapers\webgallery_library.sqlite'
$library = 'F:\Wallpapers\library'
$wallhavenLedger = 'F:\Wallpapers\library\_metadata\wallhaven-enrichment.v1.jsonl'
$providerLedger = 'F:\Wallpapers\library\_metadata\provider-enrichment.v1.jsonl'
$sibling = 'F:\Wallpapers\wallpaper_library.sqlite'
$publicationRoot = 'F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication'
$candidateRoot = Join-Path $publicationRoot 'candidates'
$manifestRoot = Join-Path $publicationRoot 'manifests'
$backupRoot = Join-Path $publicationRoot 'backups'
$journalRoot = Join-Path $publicationRoot 'journals'
$recoveryResultRoot = Join-Path $publicationRoot 'recovery-results'
$reportRoot = 'F:\Wallpapers\reports\gallery-publication'
$queueState = 'F:\Wallpapers\.wallpaper-download-queue'
$holdPath = 'F:\Wallpapers\.wallpaper-library-maintenance\gallery-publication-hold.json'
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$candidate = Join-Path $candidateRoot "webgallery_library.schema4.$stamp.candidate.sqlite"
$manifest = Join-Path $manifestRoot "publication-$stamp.manifest.json"
$backupDirectory = Join-Path $backupRoot $stamp
$journal = Join-Path $journalRoot "activation-$stamp.journal.jsonl"
```

Inspect and WhatIf (no writes):

```powershell
& $wrapper -Mode Inspect -WhatIf `
  -CanonicalDatabase $canonical -CandidateDatabase $candidate `
  -LibraryRoot $library -WallhavenLedger $wallhavenLedger `
  -ProviderLedger $providerLedger -SiblingDatabase $sibling `
  -VerificationReportRoot $reportRoot -ManifestPath $manifest `
  -BackupDirectory $backupDirectory -RecoveryJournal $journal `
  -RecoveryResultRoot $recoveryResultRoot `
  -QueueStatePath $queueState -HoldPath $holdPath
```

Prepare a unique candidate (candidate/report/manifest writes only):

```powershell
& $wrapper -Mode Prepare -Apply `
  -CanonicalDatabase $canonical -CandidateDatabase $candidate `
  -LibraryRoot $library -WallhavenLedger $wallhavenLedger `
  -ProviderLedger $providerLedger -SiblingDatabase $sibling `
  -VerificationReportRoot $reportRoot -ManifestPath $manifest `
  -BackupDirectory $backupDirectory -RecoveryJournal $journal `
  -RecoveryResultRoot $recoveryResultRoot `
  -QueueStatePath $queueState -HoldPath $holdPath
```

Validate the manifest and current bytes without activation:

```powershell
& $python scripts\publish_gallery_index.py validate `
  --manifest $manifest --canonical-database $canonical `
  --candidate-database $candidate --library-root $library `
  --wallhaven-ledger $wallhavenLedger --provider-ledger $providerLedger `
  --verification-report-root $reportRoot `
  --backup-directory $backupDirectory `
  --recovery-journal $journal --recovery-result-root $recoveryResultRoot
```

Publish only after the external hold, zero-writer window, fresh verification,
and external 8090 stop have been independently established:

```powershell
& $wrapper -Mode Publish -Apply -CutoverAuthorized `
  -CanonicalDatabase $canonical -CandidateDatabase $candidate `
  -LibraryRoot $library -WallhavenLedger $wallhavenLedger `
  -ProviderLedger $providerLedger -SiblingDatabase $sibling `
  -VerificationReportRoot $reportRoot -ManifestPath $manifest `
  -BackupDirectory $backupDirectory -RecoveryJournal $journal `
  -RecoveryResultRoot $recoveryResultRoot `
  -QueueStatePath $queueState -HoldPath $holdPath
```

Recover one identified journal/backup transaction (a current external recovery
hold remains asserted). Emergency recovery needs no candidate, report, ledgers,
or new cutover authorization:

```powershell
& $wrapper -Mode Recover -Apply `
  -CanonicalDatabase $canonical `
  -BackupDirectory $backupDirectory -RecoveryJournal $journal `
  -RecoveryResultRoot $recoveryResultRoot `
  -QueueStatePath $queueState -HoldPath $holdPath
```

The wrapper must use `SupportsShouldProcess`. Publish `-Apply` does not bypass
`-WhatIf`, identity, path, hold, freshness, listener, backup, or verification
gates. Recover `-Apply` does not bypass `-WhatIf`, identity, path, external-hold,
journal, or terminal-byte/SQLite verification gates. Its exact-backup gate is
conditional on a completed canonical mutation/backup-basis restore; it requires
no candidate freshness or new cutover authorization.

## Operator result checklist

For every attempted transition, retain:

- exact manifest snapshot/report/candidate/canonical/backup/journal-segment and
  recovery-result paths and hashes;
- all seven version fields: manifest schema, semantic contract, report format,
  journal format, recovery-result schema, SQLite `user_version`, and SQLite
  metadata schema;
- verifier exit code, issue count, and issue taxonomy;
- original and recovery hold owners/expiries, repeated zero-writer samples, and
  recovery-boundary evidence when applicable;
- before/after 8090 tuple and external recovery owner;
- canonical main/WAL/SHM before/after identities;
- any pre-activation failure/partial-output evidence;
- publication or exact rollback verification result;
- recovery-result expected/observed manifest hashes and the identity of any
  CAS-reconciled manifest;
- `release_eligible` and `listener_restore_required`; and
- the exact cleanup or retention outcome for QA-owned candidate/cache/report
  roots.

Do not label repository tests, a built candidate, a bounded browser diagnostic,
or task-manager `executed` as live publication or cutover.
