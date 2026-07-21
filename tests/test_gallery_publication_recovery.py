"""Temp-only regression tests for public gallery publication recovery."""
from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

from dl_engine import gallery_publication as publication
from dl_engine import index_library as index


NOW = datetime(2026, 7, 21, 12, 10, tzinfo=timezone.utc)


def _utc(value: datetime) -> str:
    return publication.utc_now_text(value)


def _write_json(path: Path, document: Mapping[str, object]) -> None:
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


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class _RecoveryFixture:
    """Create one complete disposable publish boundary and its evidence."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.library = root / "library"
        self.library.mkdir()
        metadata = self.library / "_metadata"
        metadata.mkdir()
        self.wallhaven_ledger = metadata / "wallhaven.jsonl"
        self.provider_ledger = metadata / "provider.jsonl"
        self.wallhaven_ledger.write_text("", encoding="utf-8")
        self.provider_ledger.write_text("", encoding="utf-8")
        self.runbook = root / "recovery-runbook.md"
        self.runbook.write_text("Restore the retained listener only after review.\n", encoding="utf-8")
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
        self.paths.backup_directory.parent.mkdir()
        self.paths.recovery_result_root.mkdir()
        _create_schema4_database(self.paths.canonical_database, "canonical-old")
        self.canonical_before = publication.fingerprint_database_set(
            self.paths.canonical_database,
            require_closed=True,
            require_checkpointed=True,
        )
        self.machine_identity = {
            "status": "VERIFIED",
            "machine_id": "snd-host",
            "instance_id": "13af5dd3-9cfe-4c8f-82ef-806f256cc1c2",
            "computer_name": "SND-HOST",
            "qualified_user": "SND-HOST\\Dev",
            "verified_at": _utc(NOW),
            "verifier_path": str(root / "Get-VerifiedMachineIdentity.ps1"),
        }
        self.prepare_outcome = self._prepare()
        self.publish_maintenance = self._publish_maintenance()

    @staticmethod
    def _now() -> datetime:
        return NOW

    def _prepare(self) -> publication.PrepareOutcome:
        def builder(
            database: Path,
            library_root: Path,
            wallhaven_ledger: Path,
            provider_ledger: Path,
        ) -> Mapping[str, object]:
            connection = index.connect(database)
            try:
                statistics = index.ingest_library(
                    connection,
                    library_root,
                    wallhaven_ledger,
                    provider_ledger,
                )
                connection.execute(
                    "INSERT OR REPLACE INTO enrichment_progress"
                    "(source, last_processed_source_site_id, updated_at)"
                    " VALUES (?,?,?)",
                    ("fixture", "candidate-new", "2026-07-21T12:00:00Z"),
                )
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
                return statistics
            finally:
                connection.close()

        return publication.prepare_candidate(
            self.paths,
            machine_identity=self.machine_identity,
            builder=builder,
            verifier=index.verify_library,
            generation_id="generation-recovery-1",
            hooks=publication.PublicationHooks(now=self._now),
        )

    def _hold(
        self,
        *,
        token_id: str,
        acquired_at: datetime,
        task_observed_at: datetime,
        acknowledged_at: datetime,
    ) -> tuple[dict[str, object], dict[str, object]]:
        queue = self.paths.queue_state
        queue.mkdir(parents=True, exist_ok=True)
        pause = queue / "pause.flag"
        pause.write_text(f"paused:{token_id}\n", encoding="utf-8")
        state = queue / "state.json"
        _write_json(
            state,
            {
                "schemaVersion": 1,
                "lastWorkerAt": _utc(task_observed_at),
                "updatedAt": _utc(task_observed_at + timedelta(seconds=1)),
                "lastMessage": "Worker paused by dashboard (pause.flag present).",
                "jobs": [],
            },
        )
        task = {
            "path": "\\",
            "name": "Wallpaper Download Queue",
            "definition_sha256": "d" * 64,
            "state": "Disabled",
            "last_result": 0,
            "instance_id": None,
            "observed_at": _utc(task_observed_at),
            "acknowledged_at": _utc(acknowledged_at),
        }
        hold: dict[str, object] = {
            "externally_owned": True,
            "owner": "temp-recovery-test",
            "token_id": token_id,
            "token_sha256": "0" * 64,
            "hold_file": {"path": str(self.paths.hold_path)},
            "pause_file": publication.fingerprint_file(
                pause, require_nonempty=True
            ).as_dict(),
            "queue_state": publication.fingerprint_file(
                state, require_nonempty=True
            ).as_dict(),
            "queue_state_schema_version": 1,
            "acquired_at": _utc(acquired_at),
            "expires_at": _utc(NOW + timedelta(hours=1)),
            "acknowledged_at": _utc(acknowledged_at),
            "acknowledgement_sha256": "0" * 64,
            "task_acknowledged": True,
            "publisher_may_release": False,
        }
        acknowledgement = publication.hold_acknowledgement_sha256(hold, task)
        hold["acknowledgement_sha256"] = acknowledgement
        pause_identity = hold["pause_file"]
        state_identity = hold["queue_state"]
        assert isinstance(pause_identity, Mapping)
        assert isinstance(state_identity, Mapping)
        _write_json(
            self.paths.hold_path,
            {
                "hold_format_version": 1,
                "owner": hold["owner"],
                "token_id": token_id,
                "acquired_at": hold["acquired_at"],
                "expires_at": hold["expires_at"],
                "acknowledged_at": hold["acknowledged_at"],
                "acknowledgement_sha256": acknowledgement,
                "task_acknowledged": True,
                "hold_path": str(self.paths.hold_path),
                "pause_path": str(pause_identity["path"]),
                "pause_file_sha256": pause_identity["sha256"],
                "queue_state_path": str(state_identity["path"]),
                "queue_state_sha256": state_identity["sha256"],
                "queue_state_schema_version": 1,
                "task_path": task["path"],
                "task_name": task["name"],
                "task_definition_sha256": task["definition_sha256"],
                "task_observed_at": task["observed_at"],
            },
        )
        hold_file = publication.fingerprint_file(
            self.paths.hold_path, require_nonempty=True
        ).as_dict()
        hold["hold_file"] = hold_file
        hold["token_sha256"] = hold_file["sha256"]
        return hold, task

    def _publish_maintenance(self) -> dict[str, object]:
        hold, task = self._hold(
            token_id="publish-hold",
            acquired_at=NOW - timedelta(minutes=2),
            task_observed_at=NOW - timedelta(seconds=110),
            acknowledged_at=NOW - timedelta(seconds=100),
        )
        candidate = publication.fingerprint_database_set(
            self.paths.candidate_database,
            require_closed=True,
            require_checkpointed=True,
        )
        canonical = publication.fingerprint_database_set(
            self.paths.canonical_database,
            require_closed=True,
            require_checkpointed=True,
        )
        durable_hash = self.prepare_outcome.durable_inputs.aggregate_sha256
        window_start = NOW - timedelta(seconds=60)
        window_end = NOW - timedelta(seconds=30)
        before_at = NOW - timedelta(seconds=29)
        process_at = NOW - timedelta(seconds=28)
        after_at = NOW - timedelta(seconds=27)
        stopped_at = NOW - timedelta(seconds=26)
        listener: dict[str, object] = {
            "before_snapshot": {
                "scope": "all-local-addresses",
                "port": 8090,
                "listen_count": 1,
                "bindings": [{"address": "127.0.0.1", "port": 8090, "pid": 4242}],
                "observed_at": _utc(before_at),
            },
            "before_process": {
                "pid": 4242,
                "qualified_owner": "SND-HOST\\Dev",
                "executable_path": str(self.root / "python.exe"),
                "executable_sha256": "e" * 64,
                "process_started_at": _utc(NOW - timedelta(minutes=10)),
                "working_directory": str(self.root),
                "argument_vector": ["python", "dashboard_server.py"],
                "observed_at": _utc(process_at),
            },
            "after_snapshot": {
                "scope": "all-local-addresses",
                "port": 8090,
                "listen_count": 0,
                "bindings": [],
                "observed_at": _utc(after_at),
            },
            "stopped_acknowledged_at": _utc(stopped_at),
            "stopped_acknowledged_by": "SND-HOST\\Dev",
            "stopped_acknowledgement_sha256": "0" * 64,
            "recovery_runbook": publication.fingerprint_file(
                self.runbook, require_nonempty=True
            ).as_dict(),
            "recovery_step": "Restore the listener after terminal review.",
            "external_recovery_owner": "temp-recovery-test",
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
        return {
            "hold": hold,
            "scheduled_task": task,
            "window_started_at": _utc(window_start),
            "window_ended_at": _utc(window_end),
            "minimum_window_seconds": 30,
            "actual_window_seconds": 30.0,
            "maximum_evidence_age_seconds": 300,
            "writer_samples": [
                {
                    "sequence": 0,
                    "elapsed_seconds": 0.0,
                    "sampled_at": _utc(window_start),
                    "downloader_descendant_count": 0,
                    "index_writer_count": 0,
                    "process_ids": [],
                },
                {
                    "sequence": 1,
                    "elapsed_seconds": 30.0,
                    "sampled_at": _utc(window_end),
                    "downloader_descendant_count": 0,
                    "index_writer_count": 0,
                    "process_ids": [],
                },
            ],
            "settled_samples": [
                {
                    "sequence": 0,
                    "elapsed_seconds": 0.0,
                    "sampled_at": _utc(window_start),
                    "candidate": candidate.as_dict(),
                    "canonical": canonical.as_dict(),
                    "durable_inputs_sha256": durable_hash,
                },
                {
                    "sequence": 1,
                    "elapsed_seconds": 30.0,
                    "sampled_at": _utc(window_end),
                    "candidate": candidate.as_dict(),
                    "canonical": canonical.as_dict(),
                    "durable_inputs_sha256": durable_hash,
                },
            ],
            "verification_started_at": _utc(NOW - timedelta(seconds=25)),
            "verification_completed_at": _utc(NOW - timedelta(seconds=24)),
            "evidence_checked_at": _utc(NOW - timedelta(seconds=23)),
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
                "observed_at": _utc(stopped_at),
            },
        }

    def interrupt_publish_after_main_replace(self) -> None:
        interrupted = False

        def checkpoint(name: str) -> None:
            nonlocal interrupted
            if name == "main-replace-returned" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt("injected after canonical main replacement")

        publication.publish_candidate(
            self.paths,
            runtime_evidence={
                "apply": True,
                "cutover_authorized": True,
                "machine_identity": self.machine_identity,
                "maintenance": self.publish_maintenance,
            },
            verifier=index.verify_library,
            canonical_handle_free=lambda _path: True,
            authorized_by="SND-HOST\\Dev",
            hooks=publication.PublicationHooks(
                now=self._now,
                checkpoint=checkpoint,
            ),
        )

    def recovery_runtime(
        self,
        chain: publication.JournalChain,
        *,
        attempt_id: str,
    ) -> dict[str, object]:
        historical_hold = self.publish_maintenance["hold"]
        assert isinstance(historical_hold, Mapping)
        hold, task = self._hold(
            token_id=f"{attempt_id}-hold",
            acquired_at=NOW - timedelta(seconds=58),
            task_observed_at=NOW - timedelta(seconds=57),
            acknowledged_at=NOW - timedelta(seconds=55),
        )
        started = NOW - timedelta(seconds=50)
        sample_end = NOW - timedelta(seconds=20)
        listener_at = NOW - timedelta(seconds=19)
        verified = NOW - timedelta(seconds=18)
        return {
            "machine_identity": dict(self.machine_identity),
            "recovery_hold": {
                "purpose": "emergency-recovery",
                "recovery_attempt_id": attempt_id,
                "authorized_head_sha256": chain.head_sha256,
                "recovery_started_at": _utc(started),
                "historical_hold_token_sha256": historical_hold["token_sha256"],
                "hold": hold,
                "scheduled_task": task,
                "minimum_window_milliseconds": 30_000,
                "actual_window_milliseconds": 30_000,
                "writer_samples": [
                    {
                        "sequence": 0,
                        "elapsed_milliseconds": 0,
                        "sampled_at": _utc(started),
                        "downloader_descendant_count": 0,
                        "index_writer_count": 0,
                        "process_ids": [],
                    },
                    {
                        "sequence": 1,
                        "elapsed_milliseconds": 30_000,
                        "sampled_at": _utc(sample_end),
                        "downloader_descendant_count": 0,
                        "index_writer_count": 0,
                        "process_ids": [],
                    },
                ],
                "listener_snapshot": {
                    "scope": "all-local-addresses",
                    "port": 8090,
                    "listen_count": 0,
                    "bindings": [],
                    "observed_at": _utc(listener_at),
                },
                "canonical_handle_free": True,
                "maximum_evidence_age_seconds": 300,
                "verified_at": _utc(verified),
            },
        }


class PublicRecoveryRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.fixture = _RecoveryFixture(self.root)
        with self.assertRaisesRegex(
            KeyboardInterrupt, "after canonical main replacement"
        ):
            self.fixture.interrupt_publish_after_main_replace()
        self.initial_chain = publication.parse_journal_chain(
            self.fixture.paths.recovery_journal
        )
        self.assertEqual(self.initial_chain.derived_status, "restored")
        observed = publication.fingerprint_database_set(
            self.fixture.paths.canonical_database,
            require_closed=True,
            require_checkpointed=True,
        )
        self.assertTrue(
            publication._database_content_matches(
                observed, self.fixture.canonical_before
            )
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_recovery_hold_must_authorize_exact_journal_head(self) -> None:
        runtime = self.fixture.recovery_runtime(
            self.initial_chain, attempt_id="invalid-head"
        )
        invalid = copy.deepcopy(runtime)
        recovery_hold = invalid["recovery_hold"]
        assert isinstance(recovery_hold, dict)
        recovery_hold["authorized_head_sha256"] = "0" * 64
        before = _tree_hashes(self.root)
        canonical_before = self.fixture.paths.canonical_database.read_bytes()

        with self.assertRaisesRegex(
            publication.SemanticValidationError,
            "anchors are invalid|authorize the current journal head",
        ):
            publication.recover_publication(
                canonical_database=self.fixture.paths.canonical_database,
                backup_directory=self.fixture.paths.backup_directory,
                recovery_journal=self.fixture.paths.recovery_journal,
                recovery_result_root=self.fixture.paths.recovery_result_root,
                runtime_evidence=invalid,
                manifest=self.fixture.paths.manifest,
                canonical_handle_free=lambda _path: True,
                hooks=publication.PublicationHooks(now=self.fixture._now),
            )

        self.assertEqual(self.fixture.paths.canonical_database.read_bytes(), canonical_before)
        self.assertEqual(_tree_hashes(self.root), before)
        self.assertEqual(
            list(self.fixture.paths.recovery_journal.parent.glob("*.segment.jsonl")),
            [],
        )
        self.assertEqual(list(self.fixture.paths.recovery_result_root.iterdir()), [])

    def test_recovery_emits_anchored_receipts_and_terminal_reentry_is_read_only(self) -> None:
        attempt_id = "recover-restored"
        runtime = self.fixture.recovery_runtime(
            self.initial_chain, attempt_id=attempt_id
        )
        replace_targets: list[Path] = []

        def trace_replace(source: Path, target: Path) -> None:
            replace_targets.append(Path(target))
            os.replace(source, target)

        recovered = publication.recover_publication(
            canonical_database=self.fixture.paths.canonical_database,
            backup_directory=self.fixture.paths.backup_directory,
            recovery_journal=self.fixture.paths.recovery_journal,
            recovery_result_root=self.fixture.paths.recovery_result_root,
            runtime_evidence=runtime,
            manifest=self.fixture.paths.manifest,
            canonical_handle_free=lambda _path: True,
            hooks=publication.PublicationHooks(
                now=self.fixture._now,
                replace=trace_replace,
            ),
        )

        continuation = self.fixture.paths.recovery_journal.with_name(
            f"{self.fixture.paths.recovery_journal.stem}.recovery-{attempt_id}.segment.jsonl"
        )
        self.assertTrue(continuation.is_file())
        completed = publication.parse_journal_chain(
            self.fixture.paths.recovery_journal,
            continuation_segments=(continuation,),
        )
        self.assertEqual(completed.derived_status, "restored")
        continuation_header = completed.segments[-1]["header"]
        self.assertEqual(continuation_header["segment_index"], 1)
        self.assertEqual(
            continuation_header["previous_segment_sha256"],
            self.initial_chain.tail_segment_sha256,
        )
        self.assertEqual(
            continuation_header["anchor_record_sha256"],
            self.initial_chain.head_sha256,
        )
        self.assertEqual(
            continuation_header["recovery_hold"], runtime["recovery_hold"]
        )

        result_path = self.fixture.paths.recovery_result_root / (
            f"{self.initial_chain.transaction_id}.{attempt_id}.recovery-result.json"
        )
        result = publication.load_json_strict(result_path)
        self.assertEqual(result["status"], "rolled-back")
        self.assertEqual(result["manifest_cas_target_state"], "rolled-back")
        self.assertTrue(result["manifest_pre_cas_matches_expected"])
        self.assertTrue(result["terminal_matches_expected"])
        self.assertEqual(result["journal_head_sha256"], completed.head_sha256)
        self.assertEqual(recovered.document["state"], "rolled-back")
        self.assertEqual(recovered.document["result"]["status"], "rolled-back")  # type: ignore[index]
        self.assertTrue(recovered.document["rollback"]["succeeded"])  # type: ignore[index]
        self.assertFalse(
            any(
                publication.path_key(target)
                == publication.path_key(self.fixture.paths.canonical_database)
                for target in replace_targets
            )
        )
        terminal_database = publication.fingerprint_database_set(
            self.fixture.paths.canonical_database,
            require_closed=True,
            require_checkpointed=True,
        )
        self.assertTrue(
            publication._database_content_matches(
                terminal_database, self.fixture.canonical_before
            )
        )

        terminal_manifest = self.fixture.paths.manifest.read_bytes()
        terminal_canonical = self.fixture.paths.canonical_database.read_bytes()
        terminal_tree = _tree_hashes(self.root)
        replace_targets.clear()
        reentered = publication.recover_publication(
            canonical_database=self.fixture.paths.canonical_database,
            backup_directory=self.fixture.paths.backup_directory,
            recovery_journal=self.fixture.paths.recovery_journal,
            recovery_result_root=self.fixture.paths.recovery_result_root,
            runtime_evidence={},
            manifest=self.fixture.paths.manifest,
            canonical_handle_free=lambda _path: False,
            continuation_segments=(continuation,),
            hooks=publication.PublicationHooks(
                now=self.fixture._now,
                replace=trace_replace,
            ),
        )

        self.assertEqual(reentered.sha256, recovered.sha256)
        self.assertEqual(reentered.document, recovered.document)
        self.assertEqual(self.fixture.paths.manifest.read_bytes(), terminal_manifest)
        self.assertEqual(
            self.fixture.paths.canonical_database.read_bytes(), terminal_canonical
        )
        self.assertEqual(_tree_hashes(self.root), terminal_tree)
        self.assertEqual(replace_targets, [])


if __name__ == "__main__":
    unittest.main()
