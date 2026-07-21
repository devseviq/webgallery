"""Regression tests for the fail-closed gallery publication core.

Every artifact in this module lives below a ``TemporaryDirectory`` on the
current volume.  The tests deliberately exercise byte identities and SQLite
files rather than substituting live wallpaper, queue, listener, or ledger
paths.
"""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from dl_engine import gallery_publication as publication
from dl_engine import index_library as index


class _SteppingClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        observed = self.current
        self.current += timedelta(seconds=1)
        return observed


def _write_canonical_json(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(publication.canonical_json_bytes(document) + b"\n")


def _create_schema4_database(path: Path, marker: str) -> None:
    connection = index.connect(path)
    try:
        connection.execute(
            "INSERT OR REPLACE INTO enrichment_progress"
            "(source, last_processed_source_site_id, updated_at) VALUES (?,?,?)",
            (marker, marker, "2026-07-21T12:00:00Z"),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        connection.close()
    publication.cleanup_owned_sqlite_sidecars(path)


class _PublicationFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.clock = _SteppingClock()
        self.library = root / "library"
        self.library.mkdir()
        metadata = self.library / "_metadata"
        metadata.mkdir()
        self.wallhaven_ledger = metadata / "wallhaven.jsonl"
        self.provider_ledger = metadata / "provider.jsonl"
        self.wallhaven_ledger.write_text("", encoding="utf-8")
        self.provider_ledger.write_text("", encoding="utf-8")
        self.paths = publication.PublicationPaths(
            canonical_database=root / "canonical.sqlite",
            candidate_database=root / "candidate.sqlite",
            library_root=self.library,
            wallhaven_ledger=self.wallhaven_ledger,
            provider_ledger=self.provider_ledger,
            verification_report_root=root / "reports",
            manifest=root / "manifests" / "publication.json",
            backup_directory=root / "backups" / "transaction-1",
            recovery_journal=root / "journals" / "transaction-1.jsonl",
            recovery_result_root=root / "recovery-results",
            queue_state=root / "queue",
            hold_path=root / "hold.json",
            sibling_database=root / "protected-sibling.sqlite",
        )
        self.paths.verification_report_root.mkdir()
        self.machine_identity = {
            "status": "VERIFIED",
            "machine_id": "snd-host",
            "instance_id": "13af5dd3-9cfe-4c8f-82ef-806f256cc1c2",
            "computer_name": "SND-HOST",
            "qualified_user": "SND-HOST\\Dev",
            "verified_at": publication.utc_now_text(self.clock()),
            "verifier_path": str(root / "Get-VerifiedMachineIdentity.ps1"),
        }
        self.hooks = publication.PublicationHooks(now=self.clock)

    def prepare(self) -> publication.PrepareOutcome:
        def builder(
            database: Path,
            library_root: Path,
            wallhaven_ledger: Path,
            provider_ledger: Path,
        ) -> Mapping[str, object]:
            connection = index.connect(database)
            try:
                stats = index.ingest_library(
                    connection,
                    library_root,
                    wallhaven_ledger,
                    provider_ledger,
                )
                connection.execute(
                    "INSERT OR REPLACE INTO enrichment_progress"
                    "(source, last_processed_source_site_id, updated_at)"
                    " VALUES (?,?,?)",
                    ("fixture", "candidate", "2026-07-21T12:00:00Z"),
                )
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                return stats
            finally:
                connection.close()

        return publication.prepare_candidate(
            self.paths,
            machine_identity=self.machine_identity,
            builder=builder,
            verifier=index.verify_library,
            generation_id="generation-1",
            hooks=self.hooks,
        )

    def publish_runtime(
        self,
        outcome: publication.PrepareOutcome,
        canonical: publication.DatabaseSetIdentity,
    ) -> dict[str, object]:
        """Create complete publication authority below this disposable root."""

        queue = self.paths.queue_state
        queue.mkdir()
        pause_path = queue / "pause.flag"
        state_path = queue / "state.json"
        pause_path.write_bytes(b"")
        queue_document = {
            "schemaVersion": 1,
            "lastWorkerAt": "2026-07-21T11:58:56.000000000Z",
            "updatedAt": "2026-07-21T11:58:57.000000000Z",
            "lastMessage": "Worker paused by dashboard (pause.flag present).",
            "jobs": [],
        }
        _write_canonical_json(state_path, queue_document)
        pause_identity = publication.fingerprint_file(pause_path).as_dict()
        state_identity = publication.fingerprint_file(
            state_path, require_nonempty=True
        ).as_dict()

        task = {
            "path": "\\",
            "name": "Wallpaper Download Queue",
            "definition_sha256": "1" * 64,
            "state": "Disabled",
            "last_result": 0,
            "instance_id": None,
            "observed_at": "2026-07-21T11:58:55.000000000Z",
            "acknowledged_at": "2026-07-21T11:58:58.000000000Z",
        }
        absent_hold = publication.fingerprint_file(
            self.paths.hold_path, allow_absent=True
        ).as_dict()
        hold: dict[str, object] = {
            "externally_owned": True,
            "owner": "fixture-hold-owner",
            "token_id": "publish-token-1",
            "token_sha256": "0" * 64,
            "hold_file": absent_hold,
            "pause_file": pause_identity,
            "queue_state": state_identity,
            "queue_state_schema_version": 1,
            "acquired_at": "2026-07-21T11:58:50.000000000Z",
            "expires_at": "2026-07-21T12:30:00.000000000Z",
            "acknowledged_at": "2026-07-21T11:58:58.000000000Z",
            "acknowledgement_sha256": "0" * 64,
            "task_acknowledged": True,
            "publisher_may_release": False,
        }
        hold["acknowledgement_sha256"] = (
            publication.hold_acknowledgement_sha256(hold, task)
        )
        token_document = {
            "hold_format_version": 1,
            "owner": hold["owner"],
            "token_id": hold["token_id"],
            "acquired_at": hold["acquired_at"],
            "expires_at": hold["expires_at"],
            "acknowledged_at": hold["acknowledged_at"],
            "acknowledgement_sha256": hold["acknowledgement_sha256"],
            "task_acknowledged": True,
            "hold_path": str(absent_hold["path"]),
            "pause_path": str(pause_identity["path"]),
            "pause_file_sha256": pause_identity["sha256"],
            "queue_state_path": str(state_identity["path"]),
            "queue_state_sha256": state_identity["sha256"],
            "queue_state_schema_version": 1,
            "task_path": task["path"],
            "task_name": task["name"],
            "task_definition_sha256": task["definition_sha256"],
            "task_observed_at": task["observed_at"],
        }
        _write_canonical_json(self.paths.hold_path, token_document)
        hold_file = publication.fingerprint_file(
            self.paths.hold_path, require_nonempty=True
        ).as_dict()
        hold["hold_file"] = hold_file
        hold["token_sha256"] = hold_file["sha256"]
        self.paths.backup_directory.parent.mkdir(parents=True)

        listener_executable = self.root / "fixture-listener.exe"
        listener_executable.write_bytes(b"fixture listener executable")
        listener_executable_identity = publication.fingerprint_file(
            listener_executable, require_nonempty=True
        )
        runbook = self.root / "listener-recovery.md"
        runbook.write_text("Restart the fixture listener manually.\n", encoding="utf-8")
        listener: dict[str, object] = {
            "before_snapshot": {
                "scope": "all-local-addresses",
                "port": 8090,
                "listen_count": 1,
                "bindings": [
                    {"address": "127.0.0.1", "port": 8090, "pid": 4242}
                ],
                "observed_at": "2026-07-21T11:59:31.000000000Z",
            },
            "before_process": {
                "pid": 4242,
                "qualified_owner": "SND-HOST\\Dev",
                "executable_path": str(listener_executable),
                "executable_sha256": listener_executable_identity.sha256,
                "process_started_at": "2026-07-21T11:30:00.000000000Z",
                "working_directory": str(self.root),
                "argument_vector": [str(listener_executable), "--fixture"],
                "observed_at": "2026-07-21T11:59:32.000000000Z",
            },
            "after_snapshot": {
                "scope": "all-local-addresses",
                "port": 8090,
                "listen_count": 0,
                "bindings": [],
                "observed_at": "2026-07-21T11:59:33.000000000Z",
            },
            "stopped_acknowledged_at": "2026-07-21T11:59:34.000000000Z",
            "stopped_acknowledged_by": "fixture-listener-owner",
            "stopped_acknowledgement_sha256": "0" * 64,
            "recovery_runbook": publication.fingerprint_file(
                runbook, require_nonempty=True
            ).as_dict(),
            "recovery_step": "Restart only after reviewing publication state.",
            "external_recovery_owner": "fixture-listener-owner",
            "restart_automatic": False,
        }
        listener["stopped_acknowledgement_sha256"] = publication.canonical_sha256(
            {
                key: listener[key]
                for key in (
                    "before_snapshot",
                    "before_process",
                    "after_snapshot",
                    "stopped_acknowledged_at",
                    "stopped_acknowledged_by",
                    "recovery_runbook",
                    "recovery_step",
                    "external_recovery_owner",
                    "restart_automatic",
                )
            }
        )
        writer_samples = [
            {
                "sequence": 0,
                "elapsed_seconds": 0.0,
                "sampled_at": "2026-07-21T11:59:00.000000000Z",
                "downloader_descendant_count": 0,
                "index_writer_count": 0,
                "process_ids": [],
            },
            {
                "sequence": 1,
                "elapsed_seconds": 30.0,
                "sampled_at": "2026-07-21T11:59:30.000000000Z",
                "downloader_descendant_count": 0,
                "index_writer_count": 0,
                "process_ids": [],
            },
        ]
        settled_samples = [
            {
                "sequence": sequence,
                "elapsed_seconds": elapsed,
                "sampled_at": sampled_at,
                "candidate": outcome.candidate.as_dict(),
                "canonical": canonical.as_dict(),
                "durable_inputs_sha256": outcome.durable_inputs.aggregate_sha256,
            }
            for sequence, elapsed, sampled_at in (
                (0, 0.0, "2026-07-21T11:59:00.000000000Z"),
                (1, 30.0, "2026-07-21T11:59:30.000000000Z"),
            )
        ]
        maintenance = {
            "hold": hold,
            "scheduled_task": task,
            "window_started_at": "2026-07-21T11:59:00.000000000Z",
            "window_ended_at": "2026-07-21T11:59:30.000000000Z",
            "minimum_window_seconds": 30,
            "actual_window_seconds": 30.0,
            "maximum_evidence_age_seconds": 300,
            "writer_samples": writer_samples,
            "settled_samples": settled_samples,
            "verification_started_at": "2026-07-21T11:59:40.000000000Z",
            "verification_completed_at": "2026-07-21T11:59:45.000000000Z",
            "evidence_checked_at": "2026-07-21T11:59:50.000000000Z",
            "fingerprints_stable": True,
            "durable_inputs_current": True,
            "canonical_wal_checkpointed": True,
            "canonical_shm_handle_free": True,
            "cutover_listener": listener,
            "smoke_port": {
                "scope": "all-local-addresses",
                "port": 8091,
                "listen_count": 0,
                "bindings": [],
                "observed_at": "2026-07-21T11:59:35.000000000Z",
            },
        }
        return {
            "apply": True,
            "cutover_authorized": True,
            "machine_identity": dict(self.machine_identity),
            "maintenance": maintenance,
        }


class PublicationManifestAndReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.fixture = _PublicationFixture(Path(self._temporary.name))
        self.outcome = self.fixture.prepare()

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _validate_report(self, evidence: Mapping[str, object]) -> None:
        publication.validate_verification_evidence(
            evidence,
            expected_database=self.fixture.paths.candidate_database,
            expected_library=self.fixture.paths.library_root,
            expected_database_sha256=str(self.outcome.candidate.main.sha256),
            expected_durable_inputs_sha256=self.outcome.durable_inputs.aggregate_sha256,
            expected_generation_id=self.outcome.generation_id,
            report_root=self.fixture.paths.verification_report_root,
            require_success=True,
            require_under_hold=False,
        )

    def test_manifest_report_and_sqlite_versions_are_separate_contracts(self) -> None:
        document = self.outcome.manifest.document
        verification = document["verification"]
        sqlite_identity = document["candidate"]["sqlite"]  # type: ignore[index]

        self.assertEqual(document["manifest_schema_version"], 1)
        self.assertEqual(document["semantic_contract_version"], 1)
        self.assertEqual(verification["report_format_version"], 1)  # type: ignore[index]
        report = publication.load_json_strict(Path(verification["report"]["path"]))  # type: ignore[index]
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(sqlite_identity["pragma_user_version"], 4)
        self.assertEqual(sqlite_identity["metadata_schema_version"], 4)
        publication.validate_manifest_file(
            self.fixture.paths.manifest,
            expected_paths=self.fixture.paths.explicit_mapping(),
        )

        substitutions = (
            ("manifest-from-sqlite", ("manifest_schema_version",), 4),
            (
                "sqlite-from-report",
                ("candidate", "sqlite", "pragma_user_version"),
                1,
            ),
            (
                "metadata-from-report",
                ("candidate", "sqlite", "metadata_schema_version"),
                1,
            ),
            (
                "report-from-sqlite",
                ("verification", "report_format_version"),
                4,
            ),
        )
        for name, keys, value in substitutions:
            with self.subTest(name=name):
                mutated = copy.deepcopy(document)
                target = mutated
                for key in keys[:-1]:
                    target = target[key]  # type: ignore[index,assignment]
                target[keys[-1]] = value  # type: ignore[index]
                with self.assertRaises(publication.PublicationError):
                    publication.validate_manifest_document(
                        mutated,
                        manifest_path=self.fixture.paths.manifest,
                        expected_paths=self.fixture.paths.explicit_mapping(),
                    )

    def test_manifest_path_and_candidate_hash_mutations_fail_closed(self) -> None:
        document = self.outcome.manifest.document
        mutations = []

        wrong_path = copy.deepcopy(document)
        wrong_path["paths"]["manifest"] = str(self.fixture.root / "other.json")  # type: ignore[index]
        mutations.append(("manifest-path", wrong_path))

        wrong_hash = copy.deepcopy(document)
        wrong_hash["candidate"]["database"]["main"]["sha256"] = "0" * 64  # type: ignore[index]
        mutations.append(("candidate-hash", wrong_hash))

        for name, mutated in mutations:
            with self.subTest(name=name), self.assertRaises(
                publication.PublicationError
            ):
                publication.validate_manifest_document(
                    mutated,
                    manifest_path=self.fixture.paths.manifest,
                    expected_paths=self.fixture.paths.explicit_mapping(),
                )

    def test_candidate_bytes_cannot_change_after_manifest_binding(self) -> None:
        with self.fixture.paths.candidate_database.open("ab") as handle:
            handle.write(b"injected-after-verification")

        with self.assertRaisesRegex(
            publication.SemanticValidationError, "candidate database bytes"
        ):
            publication.validate_manifest_file(
                self.fixture.paths.manifest,
                expected_paths=self.fixture.paths.explicit_mapping(),
            )

    def test_report_byte_path_hash_and_timestamp_mutations_are_rejected(self) -> None:
        original_evidence = self.outcome.verification
        original_report_path = Path(str(original_evidence["report"]["path"]))  # type: ignore[index]

        original_report_path.write_bytes(original_report_path.read_bytes() + b" ")
        with self.subTest(case="report-bytes"), self.assertRaisesRegex(
            publication.SemanticValidationError, "file identity"
        ):
            self._validate_report(original_evidence)

        cases = (
            ("database-path", "database_path", str(self.fixture.paths.canonical_database)),
            ("database-hash", "database_sha256", "f" * 64),
            ("missing-generated-at", "generated_at", None),
        )
        for case, key, value in cases:
            with self.subTest(case=case):
                report_path = self.fixture.paths.verification_report_root / f"{case}.json"
                report_document = publication.load_json_strict(
                    Path(str(original_evidence["report"]["path"]))  # type: ignore[index]
                )
                evidence = copy.deepcopy(original_evidence)
                if value is None:
                    report_document.pop(key)
                else:
                    report_document[key] = value
                    evidence[key] = value
                _write_canonical_json(report_path, report_document)
                evidence["report"] = publication.fingerprint_file(
                    report_path, require_nonempty=True
                ).as_dict()
                with self.assertRaises(publication.PublicationError):
                    self._validate_report(evidence)

    def test_report_identity_cannot_be_reused_across_attempt_roles(self) -> None:
        seen_paths: dict[str, tuple[str, str]] = {}
        seen_hashes: dict[str, tuple[str, str]] = {}
        publication._register_report_identity(
            self.outcome.verification,
            role="candidate-verification",
            seen_paths=seen_paths,
            seen_hashes=seen_hashes,
        )
        with self.assertRaisesRegex(
            publication.SemanticValidationError, "reused across distinct attempts"
        ):
            publication._register_report_identity(
                self.outcome.verification,
                role="under-hold-verification",
                seen_paths=seen_paths,
                seen_hashes=seen_hashes,
            )

    def test_verifier_requires_exhaustive_observer_coverage(self) -> None:
        def incomplete_verifier(
            _database: Path,
            _library: Path,
            **_kwargs: object,
        ) -> Mapping[str, object]:
            return {"schema_version": 1, "issues_total": 1, "ok": False}

        with self.assertRaisesRegex(
            publication.SemanticValidationError, "exhaustive issue set"
        ):
            publication.run_exhaustive_verification(
                incomplete_verifier,
                self.fixture.paths.candidate_database,
                self.fixture.paths.library_root,
                generation_id=self.outcome.generation_id,
                database_sha256=str(self.outcome.candidate.main.sha256),
                durable_inputs_sha256=self.outcome.durable_inputs.aggregate_sha256,
                verified_under_hold=False,
            )

    def test_settled_sample_timing_and_identity_drift_are_rejected(self) -> None:
        candidate = self.outcome.candidate.as_dict()
        canonical = self.outcome.candidate.as_dict()
        base = {
            "sequence": 0,
            "sampled_at": "2026-07-21T12:00:00Z",
            "elapsed_seconds": 0.0,
            "candidate": candidate,
            "canonical": canonical,
            "durable_inputs_sha256": self.outcome.durable_inputs.aggregate_sha256,
        }
        valid = [
            base,
            {
                **copy.deepcopy(base),
                "sequence": 1,
                "sampled_at": "2026-07-21T12:00:30Z",
                "elapsed_seconds": 30.0,
            },
        ]
        publication.validate_settled_samples(valid)

        too_short = copy.deepcopy(valid)
        too_short[1]["sampled_at"] = "2026-07-21T12:00:29Z"
        too_short[1]["elapsed_seconds"] = 29.0
        drifted = copy.deepcopy(valid)
        drifted[1]["durable_inputs_sha256"] = "0" * 64
        for case, samples in (("short", too_short), ("drift", drifted)):
            with self.subTest(case=case), self.assertRaises(
                publication.SemanticValidationError
            ):
                publication.validate_settled_samples(samples)

    def test_publish_candidate_runs_complete_bound_workflow_and_reentry_is_read_only(self) -> None:
        _create_schema4_database(
            self.fixture.paths.canonical_database, "canonical-before"
        )
        canonical_before = publication.fingerprint_database_set(
            self.fixture.paths.canonical_database,
            require_closed=True,
            require_checkpointed=True,
        )
        runtime = self.fixture.publish_runtime(self.outcome, canonical_before)

        published = publication.publish_candidate(
            self.fixture.paths,
            runtime_evidence=runtime,
            verifier=index.verify_library,
            canonical_handle_free=lambda _database: True,
            authorized_by="fixture-operator",
            hooks=self.fixture.hooks,
        )

        document = published.manifest.document
        self.assertEqual(document["state"], "published")
        self.assertEqual(document["state_transition"]["from_state"], "ready-to-publish")  # type: ignore[index]
        self.assertEqual(document["activation"]["status"], "verified")  # type: ignore[index]
        self.assertEqual(document["activation"]["journal"]["status"], "canonical-verified")  # type: ignore[index]
        self.assertFalse(document["activation"]["rollback_required"])  # type: ignore[index]
        self.assertEqual(document["result"]["status"], "succeeded")  # type: ignore[index]
        self.assertTrue(document["result"]["release_eligible"])  # type: ignore[index]
        self.assertFalse(document["result"]["listener_restore_required"])  # type: ignore[index]

        canonical_after = publication.fingerprint_database_set(
            self.fixture.paths.canonical_database,
            require_closed=True,
            require_checkpointed=True,
        )
        self.assertTrue(
            publication._database_content_matches(
                canonical_after, self.outcome.candidate
            )
        )
        self.assertEqual(
            document["post_publish"]["canonical_database_sha256"],  # type: ignore[index]
            self.outcome.candidate.main.sha256,
        )
        self.assertEqual(
            document["post_publish"]["candidate_database_sha256"],  # type: ignore[index]
            self.outcome.candidate.main.sha256,
        )

        post_verification = document["post_publish"]["verification"]  # type: ignore[index]
        self.assertTrue(post_verification["verified_under_hold"])
        self.assertEqual(
            publication.path_key(str(post_verification["database_path"])),
            publication.path_key(self.fixture.paths.canonical_database),
        )
        self.assertEqual(
            post_verification["database_sha256"],
            self.outcome.candidate.main.sha256,
        )
        report_paths = {
            publication.path_key(str(self.outcome.verification["report"]["path"])),  # type: ignore[index]
            publication.path_key(str(document["verification"]["report"]["path"])),  # type: ignore[index]
            publication.path_key(str(post_verification["report"]["path"])),
        }
        self.assertEqual(len(report_paths), 3)

        backup_main = (
            self.fixture.paths.backup_directory
            / self.fixture.paths.canonical_database.name
        )
        backup = publication.fingerprint_database_set(
            backup_main, require_checkpointed=True
        )
        self.assertTrue(
            publication._database_content_matches(backup, canonical_before)
        )
        journal = publication.parse_journal_chain(
            self.fixture.paths.recovery_journal
        )
        self.assertEqual(journal.derived_status, "canonical-verified")
        self.assertEqual(journal.generation_id, self.outcome.generation_id)

        ready_identity = document["state_transition"]["previous_manifest"]  # type: ignore[index]
        ready_document = publication.load_json_strict(Path(str(ready_identity["path"])))
        candidate_verified_identity = ready_document["state_transition"]["previous_manifest"]  # type: ignore[index]
        candidate_verified_document = publication.load_json_strict(
            Path(str(candidate_verified_identity["path"]))
        )
        candidate_built_identity = candidate_verified_document["state_transition"]["previous_manifest"]  # type: ignore[index]
        candidate_built_document = publication.load_json_strict(
            Path(str(candidate_built_identity["path"]))
        )
        self.assertEqual(
            [
                ready_document["state"],
                candidate_verified_document["state"],
                candidate_built_document["state"],
            ],
            ["ready-to-publish", "candidate-verified", "candidate-built"],
        )
        self.assertTrue(ready_document["verification"]["verified_under_hold"])  # type: ignore[index]
        self.assertTrue(ready_document["authorization"]["apply"])  # type: ignore[index]
        self.assertTrue(ready_document["authorization"]["cutover"])  # type: ignore[index]
        self.assertEqual(
            publication.sha256_file(Path(str(ready_identity["path"]))),
            ready_identity["sha256"],
        )
        publication.validate_manifest_file(
            self.fixture.paths.manifest,
            expected_paths=self.fixture.paths.explicit_mapping(),
        )

        before_reentry = {
            str(path.relative_to(self.fixture.root)): publication.sha256_file(path)
            for path in self.fixture.root.rglob("*")
            if path.is_file()
        }
        with self.assertRaisesRegex(
            publication.SemanticValidationError,
            "candidate-verified manifest",
        ):
            publication.publish_candidate(
                self.fixture.paths,
                runtime_evidence=runtime,
                verifier=index.verify_library,
                canonical_handle_free=lambda _database: True,
                authorized_by="fixture-operator",
                hooks=self.fixture.hooks,
            )
        self.assertEqual(
            {
                str(path.relative_to(self.fixture.root)): publication.sha256_file(path)
                for path in self.fixture.root.rglob("*")
                if path.is_file()
            },
            before_reentry,
        )

    def test_publish_candidate_post_activation_verifier_failure_restores_exact_prior_set(self) -> None:
        _create_schema4_database(
            self.fixture.paths.canonical_database, "canonical-before-failure"
        )
        # A checkpointed zero-byte WAL is a real member of the preactivation
        # set and must be retained as present; SHM is absent because the handle
        # boundary requires a closed database.
        Path(str(self.fixture.paths.canonical_database) + "-wal").write_bytes(b"")
        canonical_before = publication.fingerprint_database_set(
            self.fixture.paths.canonical_database,
            require_closed=True,
            require_checkpointed=True,
        )
        candidate_before = publication.fingerprint_database_set(
            self.fixture.paths.candidate_database,
            require_closed=True,
            require_checkpointed=True,
        )
        sibling_before = publication.fingerprint_file(
            self.fixture.paths.sibling_database, allow_absent=True
        )
        inputs_before = self.outcome.durable_inputs
        runtime = self.fixture.publish_runtime(self.outcome, canonical_before)
        hold_before = copy.deepcopy(runtime["maintenance"]["hold"])  # type: ignore[index]
        verifier_calls = 0

        def fail_only_after_activation(
            database: Path,
            library_root: Path,
            **kwargs: object,
        ) -> Mapping[str, object]:
            nonlocal verifier_calls
            verifier_calls += 1
            observer = kwargs.get("issue_observer")
            report = dict(
                index.verify_library(
                    database,
                    library_root,
                    issue_observer=observer,  # type: ignore[arg-type]
                )
            )
            if verifier_calls == 2:
                issue = {
                    "code": "injected-post-activation-failure",
                    "path": str(database),
                    "message": "synthetic canonical verification rejection",
                }
                self.assertTrue(callable(observer))
                observer(issue)  # type: ignore[operator]
                report.update(
                    {
                        "ok": False,
                        "status": "failed",
                        "issues_total": 1,
                        "issues_truncated": False,
                        "issues": [issue],
                    }
                )
            return report

        with self.assertRaisesRegex(
            publication.ActivationError,
            "prior database was restored",
        ):
            publication.publish_candidate(
                self.fixture.paths,
                runtime_evidence=runtime,
                verifier=fail_only_after_activation,
                canonical_handle_free=lambda _database: True,
                authorized_by="fixture-operator",
                hooks=self.fixture.hooks,
            )
        self.assertEqual(verifier_calls, 2)

        canonical_after = publication.fingerprint_database_set(
            self.fixture.paths.canonical_database,
            require_closed=True,
            require_checkpointed=True,
        )
        for name in ("main", "wal", "shm"):
            before_member = getattr(canonical_before, name)
            after_member = getattr(canonical_after, name)
            with self.subTest(member=name):
                self.assertEqual(
                    (
                        after_member.exists,
                        after_member.size_bytes,
                        after_member.sha256,
                    ),
                    (
                        before_member.exists,
                        before_member.size_bytes,
                        before_member.sha256,
                    ),
                )
        self.assertTrue(canonical_after.wal.exists)
        self.assertEqual(canonical_after.wal.size_bytes, 0)
        self.assertFalse(canonical_after.shm.exists)

        chain = publication.parse_journal_chain(
            self.fixture.paths.recovery_journal
        )
        self.assertEqual(chain.derived_status, "restored")
        self.assertTrue(
            any(
                record["action"] == "canonical-verify"
                and record["phase"] == "error"
                for record in chain.records
            )
        )
        self.assertTrue(
            any(
                record["action"] == "rollback-verify"
                and record["phase"] == "complete"
                for record in chain.records
            )
        )

        rolled_back = publication.validate_manifest_file(
            self.fixture.paths.manifest,
            expected_paths=self.fixture.paths.explicit_mapping(),
        )
        document = rolled_back.document
        self.assertEqual(document["state"], "rolled-back")
        self.assertEqual(document["state_transition"]["from_state"], "ready-to-publish")  # type: ignore[index]
        self.assertEqual(document["activation"]["status"], "failed")  # type: ignore[index]
        self.assertTrue(document["activation"]["rollback_required"])  # type: ignore[index]
        self.assertEqual(document["activation"]["journal"]["status"], "restored")  # type: ignore[index]
        self.assertTrue(document["rollback"]["succeeded"])  # type: ignore[index]
        self.assertTrue(document["rollback"]["restored_sha256_matches_backup"])  # type: ignore[index]
        self.assertTrue(document["rollback"]["hold_must_remain"])  # type: ignore[index]
        self.assertTrue(document["rollback"]["listener_restore_required"])  # type: ignore[index]
        self.assertEqual(document["result"]["status"], "rolled-back")  # type: ignore[index]
        self.assertFalse(document["result"]["release_eligible"])  # type: ignore[index]
        self.assertTrue(document["result"]["listener_restore_required"])  # type: ignore[index]
        self.assertIsNone(document["post_publish"])

        backup_main = (
            self.fixture.paths.backup_directory
            / self.fixture.paths.canonical_database.name
        )
        backup = publication.fingerprint_database_set(
            backup_main, require_checkpointed=True
        )
        self.assertTrue(
            publication._database_content_matches(backup, canonical_before)
        )
        candidate_after = publication.fingerprint_database_set(
            self.fixture.paths.candidate_database,
            require_closed=True,
            require_checkpointed=True,
        )
        self.assertEqual(candidate_after.as_dict(), candidate_before.as_dict())
        inputs_after = publication.fingerprint_durable_inputs(
            self.fixture.paths.library_root,
            self.fixture.paths.wallhaven_ledger,
            self.fixture.paths.provider_ledger,
            self.outcome.generation_id,
            now=self.fixture.clock(),
        )
        publication.compare_durable_inputs(inputs_before.as_dict(), inputs_after)
        self.assertEqual(
            publication.fingerprint_file(
                self.fixture.paths.sibling_database, allow_absent=True
            ).as_dict(),
            sibling_before.as_dict(),
        )
        current_hold = runtime["maintenance"]["hold"]  # type: ignore[index]
        self.assertEqual(current_hold, hold_before)
        for identity_name in ("hold_file", "pause_file", "queue_state"):
            publication._assert_file_identity(
                current_hold[identity_name],  # type: ignore[index]
                allow_absent=False,
            )


class JournalClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.clock = _SteppingClock()
        self.hooks = publication.PublicationHooks(now=self.clock)
        self.manifest_path = self.root / "manifest.json"
        _write_canonical_json(self.manifest_path, {"fixture": True})
        self.manifest_identity = publication.fingerprint_file(
            self.manifest_path, require_nonempty=True
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _writer(self, path: Path) -> publication.JournalWriter:
        return publication.JournalWriter(
            path,
            transaction_id=f"tx-{path.stem}",
            generation_id="generation-1",
            manifest_path=self.manifest_path,
            pre_activation_manifest=self.manifest_identity,
            hooks=self.hooks,
        )

    def test_missing_partial_header_only_and_torn_first_record_classification(self) -> None:
        missing = self.root / "missing.jsonl"
        self.assertFalse(publication._journal_has_valid_header(missing))
        with self.assertRaises((OSError, publication.PublicationError)):
            publication.parse_journal_chain(missing)

        partial = self.root / "partial.jsonl"
        partial.write_bytes(b'{"journal_format_version":1')
        self.assertFalse(publication._journal_has_valid_header(partial))
        with self.assertRaises(publication.PublicationError):
            publication.parse_journal_chain(partial)

        header_only = self.root / "header-only.jsonl"
        self._writer(header_only)
        self.assertTrue(publication._journal_has_valid_header(header_only))
        chain = publication.parse_journal_chain(header_only)
        self.assertEqual(chain.records, ())
        self.assertIsNone(chain.head_sha256)
        self.assertEqual(chain.derived_status, "recovery-required")

        torn = self.root / "torn-first.jsonl"
        writer = self._writer(torn)
        target = self.root / "target.bin"
        target.write_bytes(b"before")
        before = publication.fingerprint_file(target)
        writer.intent(
            "backup",
            self.root / "backup" / target.name,
            publication._project_file_identity(
                before, self.root / "backup" / target.name, exists=False
            ).as_dict(),
            publication._project_file_identity(
                before, self.root / "backup" / target.name
            ).as_dict(),
        )
        payload = torn.read_bytes()
        header_end = payload.index(b"\n") + 1
        torn.write_bytes(payload[: header_end + (len(payload) - header_end) // 2])
        self.assertTrue(publication._journal_has_valid_header(torn))
        chain = publication.parse_journal_chain(torn)
        self.assertEqual(chain.records, ())
        self.assertIsNone(chain.head_sha256)
        self.assertTrue(chain.segments[0]["torn_final_record"])
        self.assertEqual(chain.derived_status, "recovery-required")

    def test_torn_tail_after_a_valid_terminal_record_requires_recovery(self) -> None:
        journal = self.root / "torn-after-terminal.jsonl"
        writer = self._writer(journal)
        canonical = self.root / "canonical.sqlite"
        canonical.write_bytes(b"unchanged")
        publication._append_transaction_abort(
            writer, publication.fingerprint_file(canonical)
        )
        journal.write_bytes(journal.read_bytes() + b'{"transaction_id":')

        chain = publication.parse_journal_chain(journal)

        self.assertTrue(chain.segments[0]["torn_final_record"])
        self.assertEqual(chain.derived_status, "recovery-required")


class ActivationAndReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.clock = _SteppingClock()
        self.library = self.root / "library"
        self.library.mkdir()
        self.manifest = self.root / "manifest.json"
        _write_canonical_json(self.manifest, {"state": "ready-fixture"})
        self.manifest_identity = publication.fingerprint_file(
            self.manifest, require_nonempty=True
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _new_activation_databases(self) -> tuple[Path, Path]:
        canonical = self.root / "canonical.sqlite"
        candidate = self.root / "candidate.sqlite"
        _create_schema4_database(canonical, "old")
        _create_schema4_database(candidate, "new")
        return canonical, candidate

    def _activate(
        self,
        *,
        canonical: Path,
        candidate: Path,
        journal: Path,
        backup: Path,
        hooks: publication.PublicationHooks,
        finalize: object = None,
        terminal_commit_observed: object = None,
    ) -> publication.ActivationOutcome:
        backup.parent.mkdir(parents=True, exist_ok=True)
        return publication.activate_database(
            canonical_database=canonical,
            candidate_database=candidate,
            backup_directory=backup,
            recovery_journal=journal,
            pre_activation_manifest=self.manifest_identity,
            generation_id="generation-1",
            post_verify=lambda database: index.verify_library(
                database, self.library
            ),
            canonical_handle_free=lambda _database: True,
            manifest_path=self.manifest,
            finalize=finalize,  # type: ignore[arg-type]
            terminal_commit_observed=terminal_commit_observed,  # type: ignore[arg-type]
            hooks=hooks,
        )

    def test_main_replace_error_after_apply_is_observed_and_exactly_rolled_back(self) -> None:
        canonical, candidate = self._new_activation_databases()
        before = publication.fingerprint_database_set(
            canonical, require_closed=True, require_checkpointed=True
        )
        candidate_identity = publication.fingerprint_database_set(
            candidate, require_closed=True, require_checkpointed=True
        )
        journal = self.root / "journals" / "replace-error.jsonl"
        backup = self.root / "backups" / "replace-error"
        replace_targets: list[Path] = []

        def applied_then_error(source: Path, target: Path) -> None:
            replace_targets.append(Path(target))
            os.replace(source, target)
            if len(replace_targets) == 1:
                raise OSError("injected return-path error after main replacement")

        hooks = publication.PublicationHooks(
            now=self.clock,
            replace=applied_then_error,
        )
        with self.assertRaisesRegex(
            publication.ActivationError, "prior database was restored"
        ):
            self._activate(
                canonical=canonical,
                candidate=candidate,
                journal=journal,
                backup=backup,
                hooks=hooks,
            )

        observed = publication.fingerprint_database_set(
            canonical, require_closed=True, require_checkpointed=True
        )
        self.assertTrue(publication._database_content_matches(observed, before))
        self.assertNotEqual(observed.main.sha256, candidate_identity.main.sha256)
        chain = publication.parse_journal_chain(journal)
        self.assertEqual(chain.derived_status, "restored")
        main_outcomes = [
            record
            for record in chain.records
            if record["action"] == "main-replace"
            and record["phase"] == "complete"
        ]
        self.assertEqual(len(main_outcomes), 1)
        self.assertEqual(main_outcomes[0]["outcome"], "applied")
        self.assertEqual(main_outcomes[0]["observed_after"]["sha256"], candidate_identity.main.sha256)  # type: ignore[index]
        self.assertTrue(
            any(record["action"] == "rollback-verify" for record in chain.records)
        )

    def test_terminal_manifest_commit_interrupt_does_not_roll_back_published_bytes(self) -> None:
        canonical, candidate = self._new_activation_databases()
        candidate_identity = publication.fingerprint_database_set(
            candidate, require_closed=True, require_checkpointed=True
        )
        journal = self.root / "journals" / "published-interrupt.jsonl"
        backup = self.root / "backups" / "published-interrupt"
        manifest_archive = self.root / "manifest.ready.archive.json"
        committed = False

        def finalize(_outcome: publication.ActivationOutcome) -> None:
            nonlocal committed
            publication.replace_json_compare_and_swap(
                self.manifest,
                {"state": "published"},
                expected_sha256=str(self.manifest_identity.sha256),
                archive_path=manifest_archive,
                hooks=publication.PublicationHooks(now=self.clock),
            )
            committed = True
            raise KeyboardInterrupt("injected after published manifest CAS")

        with self.assertRaisesRegex(
            KeyboardInterrupt, "published manifest CAS"
        ):
            self._activate(
                canonical=canonical,
                candidate=candidate,
                journal=journal,
                backup=backup,
                hooks=publication.PublicationHooks(now=self.clock),
                finalize=finalize,
                terminal_commit_observed=lambda: committed,
            )

        observed = publication.fingerprint_database_set(
            canonical, require_closed=True, require_checkpointed=True
        )
        self.assertTrue(
            publication._database_content_matches(observed, candidate_identity)
        )
        self.assertEqual(publication.load_json_strict(self.manifest)["state"], "published")
        self.assertEqual(manifest_archive.read_bytes(), b'{"state":"ready-fixture"}\n')
        chain = publication.parse_journal_chain(journal)
        self.assertEqual(chain.derived_status, "canonical-verified")
        self.assertFalse(
            any(record["action"] == "rollback-restore" for record in chain.records)
        )

    def test_partial_main_wal_shm_restore_replay_skips_completed_main(self) -> None:
        canonical = self.root / "canonical.sqlite"
        replacement = self.root / "replacement.sqlite"
        _create_schema4_database(canonical, "old")
        Path(str(canonical) + "-wal").write_bytes(b"")
        Path(str(canonical) + "-shm").write_bytes(b"old-shm")
        canonical_before = publication.fingerprint_database_set(
            canonical, require_checkpointed=True
        )
        canonical_before_sqlite = publication.read_sqlite_identity(
            canonical, canonical_before, require_schema4=True
        )

        backup_directory = self.root / "backups" / "partial-replay"
        backup_directory.mkdir(parents=True)
        backup_main = backup_directory / canonical.name
        for source, target in zip(
            publication._database_member_paths(canonical),
            publication._database_member_paths(backup_main),
        ):
            shutil.copy2(source, target)
        backup = publication.fingerprint_database_set(
            backup_main, require_checkpointed=True
        )

        _create_schema4_database(replacement, "new")
        shutil.copy2(replacement, canonical)
        Path(str(canonical) + "-wal").write_bytes(b"")
        Path(str(canonical) + "-shm").write_bytes(b"new-shm")
        replaced = publication.fingerprint_database_set(
            canonical, require_checkpointed=True
        )

        journal_path = self.root / "journals" / "partial-replay.jsonl"
        writer = publication.JournalWriter(
            journal_path,
            transaction_id="tx-partial-replay",
            generation_id="generation-1",
            manifest_path=self.manifest,
            pre_activation_manifest=self.manifest_identity,
            hooks=publication.PublicationHooks(now=self.clock),
        )
        for source_member, backup_member in zip(
            (canonical_before.main, canonical_before.wal, canonical_before.shm),
            (backup.main, backup.wal, backup.shm),
        ):
            absent = publication._project_file_identity(
                source_member, backup_member.path, exists=False
            )
            operation, sequence = writer.intent(
                "backup",
                backup_member.path,
                absent.as_dict(),
                backup_member.as_dict(),
            )
            writer.complete(
                operation,
                sequence,
                "backup",
                backup_member.path,
                absent.as_dict(),
                backup_member.as_dict(),
                backup_member.as_dict(),
            )
        operation, sequence = writer.intent(
            "main-replace",
            canonical,
            canonical_before.main.as_dict(),
            replaced.main.as_dict(),
        )
        writer.complete(
            operation,
            sequence,
            "main-replace",
            canonical,
            canonical_before.main.as_dict(),
            replaced.main.as_dict(),
            replaced.main.as_dict(),
        )

        replace_targets: list[Path] = []
        interrupted = False

        def trace_replace(source: Path, target: Path) -> None:
            replace_targets.append(Path(target))
            os.replace(source, target)

        def interrupt_after_main(name: str) -> None:
            nonlocal interrupted
            if name == "rollback-main-completed" and not interrupted:
                interrupted = True
                raise OSError("injected between main and WAL restoration")

        hooks = publication.PublicationHooks(
            now=self.clock,
            replace=trace_replace,
            checkpoint=interrupt_after_main,
        )
        with self.assertRaisesRegex(OSError, "between main and WAL"):
            publication._restore_from_backup(
                canonical_before=canonical_before,
                canonical_before_sqlite=canonical_before_sqlite,
                backup=backup,
                canonical_path=canonical,
                journal=writer,
                hooks=hooks,
            )

        completed_main = next(
            record
            for record in writer.records
            if record["action"] == "rollback-restore"
            and record["phase"] == "complete"
            and publication.path_key(str(record["target_path"]))
            == publication.path_key(canonical)
        )
        restored, restored_sqlite = publication._restore_from_backup(
            canonical_before=canonical_before,
            canonical_before_sqlite=canonical_before_sqlite,
            backup=backup,
            canonical_path=canonical,
            journal=writer,
            hooks=hooks,
            completed_restores={
                publication.path_key(canonical): completed_main["observed_after"]  # type: ignore[dict-item]
            },
            required_restore_targets={publication.path_key(canonical)},
        )

        self.assertTrue(publication._database_content_matches(restored, backup))
        self.assertEqual(restored_sqlite, canonical_before_sqlite)
        self.assertEqual(
            sum(publication.path_key(target) == publication.path_key(canonical) for target in replace_targets),
            1,
        )
        self.assertEqual(
            {
                publication.path_key(target)
                for target in replace_targets
            },
            {
                publication.path_key(path)
                for path in publication._database_member_paths(canonical)
            },
        )
        chain = publication.parse_journal_chain(journal_path)
        self.assertEqual(chain.derived_status, "restored")
        self.assertEqual(len(list(backup_directory.iterdir())), 3)
        self.assertEqual(list(self.root.rglob("*.publication-stage")), [])

        # Re-inspecting a terminal recovery chain is read-only and produces no
        # continuation/result artifacts or journal growth.
        before_bytes = journal_path.read_bytes()
        before_artifacts = sorted(
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file()
        )
        self.assertEqual(
            publication.parse_journal_chain(journal_path).derived_status,
            "restored",
        )
        self.assertEqual(journal_path.read_bytes(), before_bytes)
        self.assertEqual(
            sorted(
                str(path.relative_to(self.root))
                for path in self.root.rglob("*")
                if path.is_file()
            ),
            before_artifacts,
        )

        # A crash after the rollback-verify intent but before its completion is
        # explicitly nonterminal and resumes at verification, not restoration.
        lines = journal_path.read_bytes().splitlines(keepends=True)
        unmatched_verify = self.root / "journals" / "unmatched-verify.jsonl"
        unmatched_verify.write_bytes(b"".join(lines[:-1]))
        unmatched = publication.parse_journal_chain(unmatched_verify)
        self.assertEqual(unmatched.records[-1]["action"], "rollback-verify")
        self.assertEqual(unmatched.records[-1]["phase"], "intent")
        self.assertEqual(unmatched.derived_status, "rollback-started")


if __name__ == "__main__":
    unittest.main()
