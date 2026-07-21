"""Portable contract tests for the gallery publication entry points.

These tests deliberately inspect source and exercise only ``--help`` or
non-applying CLI paths.  They never collect live process/task/listener state,
touch the canonical database, or invoke the SND-HOST wrapper.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL_WRAPPER = ROOT / "scripts" / "Invoke-GalleryIndexPublication.ps1"
PYTHON_CLI = ROOT / "scripts" / "publish_gallery_index.py"
PUBLICATION_CORE = ROOT / "src" / "dl_engine" / "gallery_publication.py"
MANIFEST_SCHEMA = ROOT / "schemas" / "gallery-publication-manifest.schema.json"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _tree_snapshot(root: Path) -> tuple[tuple[str, str, int], ...]:
    return tuple(
        sorted(
            (
                str(path.relative_to(root)),
                "directory" if path.is_dir() else "file",
                0 if path.is_dir() else path.stat().st_size,
            )
            for path in root.rglob("*")
        )
    )


class PowerShellWrapperContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.wrapper = _read(POWERSHELL_WRAPPER)
        cls.cli = _read(PYTHON_CLI)
        cls.core = _read(PUBLICATION_CORE)
        cls.schema = json.loads(_read(MANIFEST_SCHEMA))

    def test_wrapper_parses_when_powershell_is_available(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is not installed")
        wrapper_path = str(POWERSHELL_WRAPPER).replace("'", "''")
        command = (
            f"$path='{wrapper_path}'; "
            "$tokens=$null; $errors=$null; "
            "[Management.Automation.Language.Parser]::ParseFile("
            "$path,[ref]$tokens,[ref]$errors) | Out-Null; "
            "if ($errors.Count) { $errors | ForEach-Object { "
            "[Console]::Error.WriteLine($_.Message) }; exit 1 }"
        )
        completed = subprocess.run(
            [
                pwsh,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_exact_verified_snd_host_identity_is_required(self) -> None:
        required = (
            "$script:ExpectedMachineId = 'snd-host'",
            "$script:ExpectedInstanceId = '13af5dd3-9cfe-4c8f-82ef-806f256cc1c2'",
            "$script:ExpectedComputerName = 'SND-HOST'",
            "$script:ExpectedQualifiedUser = 'SND-HOST\\Dev'",
            "$script:IdentityVerifier = 'C:\\Users\\Dev\\OneDrive\\common\\common_dev\\Get-VerifiedMachineIdentity.ps1'",
            "$raw = & $script:IdentityVerifier -AsJson",
            "status = 'VERIFIED'",
            "machineId = $script:ExpectedMachineId",
            "instanceId = $script:ExpectedInstanceId",
            "computerName = $script:ExpectedComputerName",
            "qualifiedUser = $script:ExpectedQualifiedUser",
        )
        for contract in required:
            with self.subTest(contract=contract):
                self.assertIn(contract, self.wrapper)

    def test_should_process_and_separate_apply_cutover_gates_are_present(self) -> None:
        self.assertIn(
            "[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]",
            self.wrapper,
        )
        self.assertIn("[switch]$Apply", self.wrapper)
        self.assertIn("[switch]$CutoverAuthorized", self.wrapper)
        self.assertIn(
            "Publish -Apply requires the separate -CutoverAuthorized flag.",
            self.wrapper,
        )
        self.assertIn(
            "$applyRequested = $Mode -in @('Prepare', 'Publish', 'Recover') -and",
            self.wrapper,
        )
        self.assertIn("$Apply.IsPresent -and -not [bool]$WhatIfPreference", self.wrapper)
        self.assertIn("$shouldApply = $PSCmdlet.ShouldProcess($target, $operation)", self.wrapper)
        self.assertIn("$executeApply = $applyRequested -and $shouldApply", self.wrapper)

        self.assertIn("if mode == \"publish\" and apply and not cutover_authorized:", self.cli)
        self.assertIn("publish --apply requires --cutover-authorized", self.cli)
        self.assertIn("if apply and mode in {\"prepare\", \"publish\", \"recover\"}:", self.cli)
        self.assertIn("requires --runtime-evidence-stdin", self.cli)

    def test_explicit_paths_manifest_and_ordered_recovery_segments(self) -> None:
        for parameter in (
            "CanonicalDatabase",
            "CandidateDatabase",
            "LibraryRoot",
            "WallhavenLedger",
            "ProviderLedger",
            "SiblingDatabase",
            "VerificationReportRoot",
            "ManifestPath",
            "BackupDirectory",
            "RecoveryJournal",
            "RecoveryResultRoot",
            "QueueStatePath",
            "HoldPath",
        ):
            with self.subTest(parameter=parameter):
                self.assertRegex(self.wrapper, rf"\[string\]\${parameter}\b")

        self.assertIn("[string[]]$ContinuationSegments = @()", self.wrapper)
        self.assertIn("continuation_segments = @($ContinuationSegments)", self.wrapper)
        self.assertIn(
            '$paths["ContinuationSegment$index"] = [string]$ContinuationSegments[$index]',
            self.wrapper,
        )
        self.assertIn("for index, value in enumerate(raw):", self.cli)
        self.assertIn("result.append(Path(value))", self.cli)
        self.assertIn("return tuple(result)", self.cli)

        for option in (
            "--canonical-database",
            "--candidate-database",
            "--library-root",
            "--wallhaven-ledger",
            "--provider-ledger",
            "--sibling-database",
            "--verification-report-root",
            "--manifest",
            "--backup-directory",
            "--recovery-journal",
            "--recovery-result-root",
            "--queue-state-path",
            "--hold-path",
        ):
            with self.subTest(option=option):
                self.assertIn(f'parser.add_argument("{option}"', self.cli)

        self.assertIn("$full = [IO.Path]::GetFullPath($Path)", self.wrapper)
        self.assertGreaterEqual(self.wrapper.count("Test-Path -LiteralPath"), 12)
        self.assertGreaterEqual(self.wrapper.count("Resolve-Path -LiteralPath"), 3)
        self.assertIn("Get-Item -LiteralPath", self.wrapper)

    def test_apply_allowlist_is_pinned_to_exact_snd_host_paths(self) -> None:
        expected_assignments = (
            "$script:CanonicalProjectRoot = 'F:\\Wallpapers\\webgallery'",
            "$script:CanonicalDatabase = 'F:\\Wallpapers\\webgallery_library.sqlite'",
            "$script:CanonicalLibraryRoot = 'F:\\Wallpapers\\library'",
            "$script:CanonicalWallhavenLedger = 'F:\\Wallpapers\\library\\_metadata\\wallhaven-enrichment.v1.jsonl'",
            "$script:CanonicalProviderLedger = 'F:\\Wallpapers\\library\\_metadata\\provider-enrichment.v1.jsonl'",
            "$script:ProtectedSiblingDatabase = 'F:\\Wallpapers\\wallpaper_library.sqlite'",
            "$script:CanonicalRecoveryResultRoot = Join-Path $script:PublicationRoot 'recovery-results'",
            "$script:CanonicalQueueStateRoot = 'F:\\Wallpapers\\.wallpaper-download-queue'",
            "$script:CanonicalHoldPath = 'F:\\Wallpapers\\.wallpaper-library-maintenance\\gallery-publication-hold.json'",
        )
        for assignment in expected_assignments:
            with self.subTest(assignment=assignment):
                self.assertIn(assignment, self.wrapper)

        exact_checks = {
            "ProjectRoot": "$script:CanonicalProjectRoot",
            "CanonicalDatabase": "$script:CanonicalDatabase",
            "RecoveryResultRoot": "$script:CanonicalRecoveryResultRoot",
            "LibraryRoot": "$script:CanonicalLibraryRoot",
            "WallhavenLedger": "$script:CanonicalWallhavenLedger",
            "ProviderLedger": "$script:CanonicalProviderLedger",
            "SiblingDatabase": "$script:ProtectedSiblingDatabase",
        }
        for actual, expected in exact_checks.items():
            with self.subTest(actual=actual):
                self.assertIn(
                    f"-Actual ${actual} -Expected {expected} -Label '{actual}'",
                    self.wrapper,
                )

        for root_name, value_name in (
            ("CandidateRoot", "CandidateDatabase"),
            ("ManifestRoot", "ManifestPath"),
            ("BackupRoot", "BackupDirectory"),
            ("JournalRoot", "RecoveryJournal"),
        ):
            with self.subTest(root=root_name):
                self.assertIn(
                    f"-Path ${value_name} -Root $script:{root_name} -Label '{value_name}'",
                    self.wrapper,
                )
        self.assertIn("Apply requires one exact volume identity.", self.wrapper)

    def test_hold_task_descendant_writer_and_listener_proofs_fail_closed(self) -> None:
        for contract in (
            "Get-GalleryPublicationHoldEvidence",
            "task_acknowledged",
            "acknowledgement_sha256",
            "publisher_may_release = $false",
            "Get-GalleryPublicationQueueTaskEvidence",
            "Queue task must have exactly one action",
            "-Drain",
            "-CompactCompleted",
            "Get-CimInstance Win32_Process",
            "$childrenByParent",
            "$pending.Enqueue($childId)",
            "downloader_descendant_count",
            "index_writer_count",
            "$script:MinimumObservationMilliseconds = 30000",
            "zero_writers",
            "Get-GalleryPublicationListenerSnapshot -Port 8090",
            "Get-GalleryPublicationListenerSnapshot -Port 8091",
            "port 8090 still has a listener",
            "smoke port 8091 is not free",
            "canonical_handle_free",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, self.wrapper)

        self.assertIn(
            'cutover_listener = runtime.get("cutover_listener")', self.cli
        )
        self.assertIn(
            "publish requires a complete externally acknowledged cutover_listener receipt",
            self.cli,
        )
        # The wrapper observes stopped ports but cannot fabricate the external
        # pre-cutover process/stop acknowledgement required by the core.
        self.assertNotRegex(self.wrapper, r"(?m)^\s*cutover_listener\s*=")
        self.assertIn("publisher_mutated_external_authority = $false", self.wrapper)

    def test_external_listener_receipt_pins_full_launch_tuple(self) -> None:
        definitions = self.schema["$defs"]
        listener_process = definitions["listenerProcess"]
        self.assertEqual(
            set(listener_process["required"]),
            {
                "pid",
                "qualified_owner",
                "executable_path",
                "executable_sha256",
                "process_started_at",
                "working_directory",
                "argument_vector",
                "observed_at",
            },
        )
        cutover = definitions["cutoverListener"]
        self.assertTrue(
            {
                "before_snapshot",
                "before_process",
                "after_snapshot",
                "stopped_acknowledged_at",
                "stopped_acknowledged_by",
                "stopped_acknowledgement_sha256",
                "recovery_runbook",
                "external_recovery_owner",
                "restart_automatic",
            }.issubset(cutover["required"])
        )
        self.assertEqual(cutover["properties"]["restart_automatic"]["const"], False)
        self.assertIn(
            'binding.get("pid") != process.get(',
            self.core,
        )
        self.assertIn("port 8090 has not been proven stopped", self.core)
        self.assertIn("listener stop acknowledgement timing is invalid", self.core)

    def test_protected_sibling_is_distinct_and_forwarded_to_core(self) -> None:
        self.assertIn(
            "$script:ProtectedSiblingDatabase = 'F:\\Wallpapers\\wallpaper_library.sqlite'",
            self.wrapper,
        )
        self.assertIn("$distinct.SiblingDatabase = $PathEvidence.SiblingDatabase.resolved_path", self.wrapper)
        self.assertIn("@('--sibling-database', $SiblingDatabase)", self.wrapper)
        self.assertIn("sibling_database=args.sibling_database", self.cli)
        self.assertIn("sibling_database", self.core)

    def test_wrapper_contains_no_external_mutator_commands(self) -> None:
        prohibited = (
            "Stop-Process",
            "Start-Process",
            "Stop-ScheduledTask",
            "Start-ScheduledTask",
            "Disable-ScheduledTask",
            "Enable-ScheduledTask",
            "Unregister-ScheduledTask",
            "Resume-ScheduledTask",
            "Remove-Item",
            "Move-Item",
            "Copy-Item",
        )
        for command in prohibited:
            with self.subTest(command=command):
                self.assertNotIn(command, self.wrapper)
        self.assertIn("publisher_may_release = $false", self.wrapper)
        self.assertIn("restart_automatic", self.schema["$defs"]["cutoverListener"]["properties"])
        for python_mutator in (
            "subprocess.", "os.kill(", "os.replace(", "shutil.move(",
            "Stop-Process", "Disable-ScheduledTask",
        ):
            with self.subTest(python_mutator=python_mutator):
                self.assertNotIn(python_mutator, self.cli)


class PythonCliNoWriteContractTests(unittest.TestCase):
    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-B", str(PYTHON_CLI), *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def _full_arguments(root: Path) -> list[str]:
        return [
            "--canonical-database", str(root / "canonical.sqlite"),
            "--backup-directory", str(root / "backup"),
            "--recovery-journal", str(root / "journal.jsonl"),
            "--recovery-result-root", str(root / "recovery-results"),
            "--queue-state-path", str(root / "queue"),
            "--hold-path", str(root / "hold.json"),
            "--manifest", str(root / "manifest.json"),
            "--candidate-database", str(root / "candidate.sqlite"),
            "--library-root", str(root / "library"),
            "--wallhaven-ledger", str(root / "wallhaven.jsonl"),
            "--provider-ledger", str(root / "provider.jsonl"),
            "--sibling-database", str(root / "wallpaper_library.sqlite"),
            "--verification-report-root", str(root / "reports"),
        ]

    def test_help_for_every_mode_is_successful_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = _tree_snapshot(root)
            invocations = [
                ("--help",),
                *( (mode, "--help") for mode in (
                    "inspect", "prepare", "validate", "publish", "recover"
                ) ),
            ]
            for invocation in invocations:
                with self.subTest(invocation=invocation):
                    result = self._run(*invocation)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage:", result.stdout.lower())
            self.assertEqual(_tree_snapshot(root), before)

    def test_cli_prefers_adjacent_source_over_conflicting_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            conflict_root = Path(temporary)
            conflict_package = conflict_root / "dl_engine"
            conflict_package.mkdir()
            conflict_package.joinpath("__init__.py").write_text(
                "raise RuntimeError('conflicting dl_engine imported')\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(conflict_root)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, "-B", str(PYTHON_CLI), "--help"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage:", result.stdout.lower())
            self.assertNotIn("conflicting dl_engine imported", result.stderr)

    def test_prepare_without_apply_does_not_create_any_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = _tree_snapshot(root)
            result = self._run("prepare", *self._full_arguments(root))
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["applied"])
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["mode"], "prepare")
            self.assertEqual(_tree_snapshot(root), before)

    def test_publish_apply_requires_cutover_then_runtime_evidence_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = _tree_snapshot(root)

            missing_cutover = self._run(
                "publish", *self._full_arguments(root), "--apply"
            )
            self.assertEqual(missing_cutover.returncode, 2)
            missing_cutover_payload = json.loads(missing_cutover.stdout)
            self.assertEqual(
                missing_cutover_payload["error"]["code"],
                "cutover-authorization-required",
            )
            self.assertFalse(missing_cutover_payload["applied"])

            missing_runtime = self._run(
                "publish",
                *self._full_arguments(root),
                "--apply",
                "--cutover-authorized",
            )
            self.assertEqual(missing_runtime.returncode, 2)
            missing_runtime_payload = json.loads(missing_runtime.stdout)
            self.assertEqual(
                missing_runtime_payload["error"]["code"],
                "runtime-evidence-required",
            )
            self.assertFalse(missing_runtime_payload["applied"])
            self.assertEqual(_tree_snapshot(root), before)


if __name__ == "__main__":
    unittest.main()
