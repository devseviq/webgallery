from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest

from dl_engine import index_library as idx
from dl_engine import library_transfer


class LibraryTransferTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.db = self.root / "index.sqlite"
        self.config = self.root / "targets.json"
        self.paths = [self.library / "one.jpg", self.library / "two.png"]
        for path in self.paths:
            path.write_bytes(b"image")
        conn = idx.connect(self.db)
        try:
            for path in self.paths:
                conn.execute(
                    "INSERT INTO images ("
                    "path, filename, source, enrichment_status, indexed_at"
                    ") VALUES (?, ?, 'test', 'ok', 'now')",
                    (str(path), path.name),
                )
            conn.commit()
        finally:
            conn.close()
        self._write_config()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_config(self, *, destination: str = "sev@192.168.2.2:f:\\movedir") -> None:
        self.config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "targets": [
                        {
                            "id": "movedir",
                            "label": "Movedir",
                            "destination": destination,
                            "machine_id": "snd-desk",
                            "verifier_path": (
                                "C:\\Users\\Sev\\OneDrive\\common\\common_dev\\"
                                "Get-VerifiedMachineIdentity.ps1"
                            ),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _ids(self) -> list[int]:
        conn = idx.open_index_read_only(self.db)
        try:
            return [row["id"] for row in conn.execute("SELECT id FROM images ORDER BY id")]
        finally:
            conn.close()

    def test_loads_named_remote_targets_and_missing_config_disables_service(self) -> None:
        targets = library_transfer.load_transfer_targets(self.config)
        self.assertEqual(targets[0].id, "movedir")
        self.assertEqual(targets[0].destination, "sev@192.168.2.2:f:\\movedir")
        self.assertEqual(targets[0].machine_id, "snd-desk")
        self.assertEqual(
            library_transfer.load_transfer_targets(self.root / "missing.json"),
            (),
        )

    def test_invalid_or_local_destination_fails_closed(self) -> None:
        self._write_config(destination=r"F:\movedir")
        with self.assertRaisesRegex(
            library_transfer.TransferConfigurationError,
            "user@host",
        ):
            library_transfer.load_transfer_targets(self.config)

    def test_resolves_ids_only_to_existing_files_inside_library(self) -> None:
        ids = self._ids()
        sources = library_transfer.resolve_transfer_sources(
            self.db,
            self.library,
            ids,
        )
        self.assertEqual([source.filename for source in sources], ["one.jpg", "two.png"])

        with self.assertRaisesRegex(library_transfer.TransferRequestError, "unknown"):
            library_transfer.resolve_transfer_sources(self.db, self.library, [999999])
        with self.assertRaisesRegex(library_transfer.TransferRequestError, "duplicates"):
            library_transfer.resolve_transfer_sources(self.db, self.library, [ids[0], ids[0]])

    def test_rejects_indexed_path_outside_library(self) -> None:
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"outside")
        conn = idx.connect(self.db)
        try:
            cursor = conn.execute(
                "INSERT INTO images ("
                "path, filename, source, enrichment_status, indexed_at"
                ") VALUES (?, 'outside.jpg', 'test', 'ok', 'now')",
                (str(outside),),
            )
            outside_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(library_transfer.TransferRequestError, "outside"):
            library_transfer.resolve_transfer_sources(
                self.db,
                self.library,
                [outside_id],
            )

    def test_manager_runs_fixed_batch_mode_argv_without_a_shell(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []
        invoked = threading.Event()

        def fake_runner(command: list[str], **kwargs: object) -> SimpleNamespace:
            calls.append((command, kwargs))
            invoked.set()
            if command[0].endswith("ssh.exe"):
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "status": "VERIFIED",
                            "machineId": "snd-desk",
                            "verifiedAtUtc": "2026-07-19T00:00:00Z",
                        }
                    ),
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        scp_executable = r"C:\Windows\System32\OpenSSH\scp.exe"
        ssh_executable = r"C:\Windows\System32\OpenSSH\ssh.exe"
        manager = library_transfer.ScpTransferManager(
            self.db,
            self.library,
            self.config,
            runner=fake_runner,
            executable_resolver=lambda command: {
                "scp": scp_executable,
                "ssh": ssh_executable,
            }.get(command),
        )
        created = manager.create_job("movedir", self._ids())
        self.assertTrue(invoked.wait(2), "fake scp runner was not invoked")
        deadline = time.time() + 2
        while time.time() < deadline:
            current = manager.get_job(created["id"])
            if current and current["status"] == "completed":
                break
            time.sleep(0.01)
        self.assertEqual(current["status"], "completed")
        self.assertEqual(len(calls), 2)
        verify_command, _verify_kwargs = calls[0]
        self.assertEqual(verify_command[0], ssh_executable)
        self.assertIn("BatchMode=yes", verify_command)
        self.assertIn("sev@192.168.2.2", verify_command)
        command, kwargs = calls[1]
        self.assertEqual(command[0:2], [scp_executable, "-B"])
        self.assertEqual(command[-1], "sev@192.168.2.2:f:\\movedir")
        self.assertEqual(command[2:-1], [str(path.resolve()) for path in self.paths])
        self.assertNotIn("shell", kwargs)

    def test_machine_identity_mismatch_never_runs_scp(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(command: list[str], **_kwargs: object) -> SimpleNamespace:
            calls.append(command)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"status": "VERIFIED", "machineId": "remote"}),
                stderr="",
            )

        manager = library_transfer.ScpTransferManager(
            self.db,
            self.library,
            self.config,
            runner=fake_runner,
            executable_resolver=lambda command: command + ".exe",
        )
        created = manager.create_job("movedir", self._ids())
        deadline = time.time() + 2
        while time.time() < deadline:
            current = manager.get_job(created["id"])
            if current and current["status"] == "failed":
                break
            time.sleep(0.01)
        self.assertEqual(current["status"], "failed")
        self.assertIn("identity mismatch", current["error"])
        self.assertEqual(len(calls), 1)

    def test_unknown_target_never_invokes_runner(self) -> None:
        manager = library_transfer.ScpTransferManager(
            self.db,
            self.library,
            self.config,
            runner=lambda *_args, **_kwargs: self.fail("runner should not execute"),
            executable_resolver=lambda _command: "scp.exe",
        )
        with self.assertRaisesRegex(library_transfer.TransferRequestError, "unknown"):
            manager.create_job("other", self._ids())


if __name__ == "__main__":
    unittest.main()
