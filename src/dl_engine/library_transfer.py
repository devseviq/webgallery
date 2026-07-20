"""Safe, asynchronous SCP transfers for the local wallpaper browser.

The browser submits index IDs, never filesystem paths or command fragments.
Destinations come from a server-side allowlist and SCP is invoked without a
shell so request data cannot become executable command text.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from typing import Any, Callable, Sequence
from uuid import uuid4

from . import index_library as index


MAX_TRANSFER_ITEMS = 100
MAX_RECENT_JOBS = 50
MAX_COMMAND_LENGTH = 28_000
_TARGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_REMOTE_DESTINATION_RE = re.compile(
    r"^[A-Za-z0-9._-]+@(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\]):[^\x00\r\n]+$"
)


class TransferConfigurationError(RuntimeError):
    """The server-side transfer target configuration is unusable."""


class TransferRequestError(ValueError):
    """A browser transfer request failed validation."""


class TransferUnavailableError(RuntimeError):
    """The configured transfer service cannot currently run."""


@dataclass(frozen=True)
class TransferTarget:
    id: str
    label: str
    destination: str
    machine_id: str
    verifier_path: str

    @property
    def ssh_endpoint(self) -> str:
        if "@[" in self.destination:
            return self.destination[: self.destination.index("]:") + 1]
        return self.destination.split(":", 1)[0]

    def public_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "destination": self.destination,
            "machine_id": self.machine_id,
        }


@dataclass(frozen=True)
class TransferSource:
    id: int
    path: Path
    filename: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_transfer_targets(config_path: Path) -> tuple[TransferTarget, ...]:
    """Load and validate named SCP destinations from *config_path*.

    A missing file means transfers are intentionally unconfigured. Malformed
    files fail closed so a typo can never silently change the destination.
    """

    path = Path(config_path)
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransferConfigurationError(
            f"transfer target config is unreadable: {exc}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise TransferConfigurationError("transfer target config must use version 1")
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        raise TransferConfigurationError("transfer target config needs a targets list")
    if len(raw_targets) > 20:
        raise TransferConfigurationError("at most 20 transfer targets are allowed")

    targets: list[TransferTarget] = []
    seen_ids: set[str] = set()
    for position, raw in enumerate(raw_targets, start=1):
        if not isinstance(raw, dict):
            raise TransferConfigurationError(f"transfer target {position} must be an object")
        target_id = raw.get("id")
        label = raw.get("label")
        destination = raw.get("destination")
        machine_id = raw.get("machine_id")
        verifier_path = raw.get("verifier_path")
        if not isinstance(target_id, str) or not _TARGET_ID_RE.fullmatch(target_id):
            raise TransferConfigurationError(
                f"transfer target {position} has an invalid id"
            )
        if target_id in seen_ids:
            raise TransferConfigurationError(f"duplicate transfer target id: {target_id}")
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise TransferConfigurationError(
                f"transfer target {target_id} has an invalid label"
            )
        if (
            not isinstance(destination, str)
            or len(destination) > 500
            or not _REMOTE_DESTINATION_RE.fullmatch(destination)
        ):
            raise TransferConfigurationError(
                f"transfer target {target_id} must be user@host:remote-path"
            )
        if (
            not isinstance(machine_id, str)
            or not _TARGET_ID_RE.fullmatch(machine_id)
        ):
            raise TransferConfigurationError(
                f"transfer target {target_id} has an invalid machine_id"
            )
        if (
            not isinstance(verifier_path, str)
            or len(verifier_path) > 500
            or not re.fullmatch(r"[A-Za-z]:\\[^\x00\r\n]+", verifier_path)
        ):
            raise TransferConfigurationError(
                f"transfer target {target_id} has an invalid verifier_path"
            )
        seen_ids.add(target_id)
        targets.append(
            TransferTarget(
                target_id,
                label.strip(),
                destination,
                machine_id,
                verifier_path,
            )
        )
    return tuple(targets)


def _validate_image_ids(image_ids: object) -> tuple[int, ...]:
    if not isinstance(image_ids, list):
        raise TransferRequestError("image_ids must be a JSON list")
    if not image_ids:
        raise TransferRequestError("select at least one wallpaper")
    if len(image_ids) > MAX_TRANSFER_ITEMS:
        raise TransferRequestError(
            f"at most {MAX_TRANSFER_ITEMS} wallpapers can be sent at once"
        )
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in image_ids):
        raise TransferRequestError("image_ids must contain positive integers")
    if len(set(image_ids)) != len(image_ids):
        raise TransferRequestError("image_ids must not contain duplicates")
    return tuple(image_ids)


def resolve_transfer_sources(
    db_path: Path,
    library_root: Path,
    image_ids: object,
) -> tuple[TransferSource, ...]:
    """Resolve selected index IDs to existing files inside *library_root*."""

    ids = _validate_image_ids(image_ids)
    root = Path(library_root).resolve(strict=True)
    placeholders = ",".join("?" for _ in ids)
    conn = index.open_index_read_only(Path(db_path))
    try:
        rows = conn.execute(
            f"SELECT id, path, filename FROM images WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    finally:
        conn.close()
    by_id = {int(row["id"]): row for row in rows}
    missing_ids = [value for value in ids if value not in by_id]
    if missing_ids:
        raise TransferRequestError(
            "unknown wallpaper id(s): " + ", ".join(str(value) for value in missing_ids)
        )

    sources: list[TransferSource] = []
    filenames: set[str] = set()
    for image_id in ids:
        row = by_id[image_id]
        path = Path(row["path"])
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(root)
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise TransferRequestError(
                f"wallpaper {image_id} is missing or outside the library"
            ) from exc
        if not resolved.is_file():
            raise TransferRequestError(f"wallpaper {image_id} is not a file")
        filename = str(row["filename"] or resolved.name)
        folded = filename.casefold()
        if folded in filenames:
            raise TransferRequestError(
                "selection contains duplicate filenames; send those wallpapers separately"
            )
        filenames.add(folded)
        sources.append(TransferSource(image_id, resolved, filename))
    return tuple(sources)


class ScpTransferManager:
    """Run one SCP batch at a time while exposing small in-memory job records."""

    def __init__(
        self,
        db_path: Path,
        library_root: Path,
        config_path: Path,
        *,
        scp_command: str = "scp",
        ssh_command: str = "ssh",
        timeout_seconds: int = 900,
        runner: Callable[..., Any] = subprocess.run,
        executable_resolver: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.db_path = Path(db_path)
        self.library_root = Path(library_root)
        self.config_path = Path(config_path)
        self.scp_command = scp_command
        self.ssh_command = ssh_command
        self.timeout_seconds = timeout_seconds
        self._runner = runner
        self._executable_resolver = executable_resolver
        self._jobs: dict[str, dict[str, Any]] = {}
        self._jobs_lock = threading.Lock()
        self._transfer_lock = threading.Lock()

    def service_status(self) -> dict[str, Any]:
        targets = load_transfer_targets(self.config_path)
        scp_executable = self._executable_resolver(self.scp_command)
        ssh_executable = self._executable_resolver(self.ssh_command)
        with self._jobs_lock:
            jobs = [dict(job) for job in reversed(tuple(self._jobs.values()))]
        return {
            "enabled": bool(targets and scp_executable and ssh_executable),
            "scp_available": bool(scp_executable),
            "ssh_available": bool(ssh_executable),
            "max_items": MAX_TRANSFER_ITEMS,
            "copy_only": True,
            "targets": [target.public_dict() for target in targets],
            "recent_jobs": jobs[:10],
        }

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None

    def create_job(self, target_id: object, image_ids: object) -> dict[str, Any]:
        if not isinstance(target_id, str):
            raise TransferRequestError("target must be a configured target id")
        targets = {target.id: target for target in load_transfer_targets(self.config_path)}
        target = targets.get(target_id)
        if target is None:
            raise TransferRequestError("unknown or disabled transfer target")
        scp_executable = self._executable_resolver(self.scp_command)
        ssh_executable = self._executable_resolver(self.ssh_command)
        if not scp_executable:
            raise TransferUnavailableError("scp executable is not available")
        if not ssh_executable:
            raise TransferUnavailableError("ssh executable is not available")
        sources = resolve_transfer_sources(self.db_path, self.library_root, image_ids)
        scp_command = [
            scp_executable,
            "-B",
            *(str(source.path) for source in sources),
            target.destination,
        ]
        if sum(len(part) + 3 for part in scp_command) > MAX_COMMAND_LENGTH:
            raise TransferRequestError("selected paths are too long; send fewer wallpapers")
        verify_command = self._verification_command(target, ssh_executable)

        job_id = str(uuid4())
        job = {
            "id": job_id,
            "status": "queued",
            "target_id": target.id,
            "target_label": target.label,
            "destination": target.destination,
            "expected_machine_id": target.machine_id,
            "item_count": len(sources),
            "filenames": [source.filename for source in sources],
            "created_at": _utc_now(),
            "started_at": None,
            "finished_at": None,
            "returncode": None,
            "error": None,
            "verified_machine_id": None,
            "verified_at": None,
        }
        with self._jobs_lock:
            self._prune_jobs_locked()
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run_job,
            args=(job_id, target, verify_command, scp_command),
            name=f"wallpaper-scp-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return dict(job)

    def _prune_jobs_locked(self) -> None:
        if len(self._jobs) < MAX_RECENT_JOBS:
            return
        removable = [
            key for key, job in self._jobs.items()
            if job["status"] in {"completed", "failed"}
        ]
        for key in removable[: max(1, len(self._jobs) - MAX_RECENT_JOBS + 1)]:
            self._jobs.pop(key, None)

    def _update_job(self, job_id: str, **changes: object) -> None:
        with self._jobs_lock:
            self._jobs[job_id].update(changes)

    def _verification_command(
        self,
        target: TransferTarget,
        ssh_executable: str,
    ) -> list[str]:
        escaped_path = target.verifier_path.replace("'", "''")
        script = (
            "$ProgressPreference = 'SilentlyContinue'; "
            f"$result = & '{escaped_path}'; "
            "$result | ConvertTo-Json -Compress -Depth 4"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return [
            ssh_executable,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "StrictHostKeyChecking=yes",
            target.ssh_endpoint,
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]

    def _runner_kwargs(self, timeout: int) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": timeout,
            "check": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return kwargs

    @staticmethod
    def _result_detail(result: object, fallback: str) -> str:
        stderr = getattr(result, "stderr", "") or ""
        stdout = getattr(result, "stdout", "") or ""
        return (stderr or stdout or fallback).strip()[-2000:]

    def _fail_job(
        self,
        job_id: str,
        error: str,
        *,
        returncode: int | None = None,
    ) -> None:
        self._update_job(
            job_id,
            status="failed",
            finished_at=_utc_now(),
            returncode=returncode,
            error=error,
        )

    def _run_job(
        self,
        job_id: str,
        target: TransferTarget,
        verify_command: Sequence[str],
        scp_command: Sequence[str],
    ) -> None:
        with self._transfer_lock:
            self._update_job(job_id, status="verifying", started_at=_utc_now())
            try:
                verification = self._runner(
                    list(verify_command),
                    **self._runner_kwargs(min(self.timeout_seconds, 30)),
                )
            except subprocess.TimeoutExpired:
                self._fail_job(job_id, "remote machine identity verification timed out")
                return
            except OSError as exc:
                self._fail_job(job_id, f"could not start remote identity verification: {exc}")
                return
            if verification.returncode != 0:
                self._fail_job(
                    job_id,
                    "remote machine identity verification failed: "
                    + self._result_detail(verification, "ssh failed"),
                    returncode=verification.returncode,
                )
                return
            lines = [line.strip() for line in (verification.stdout or "").splitlines() if line.strip()]
            try:
                identity = json.loads(lines[-1] if lines else "")
            except (json.JSONDecodeError, IndexError):
                self._fail_job(job_id, "remote machine identity verifier returned invalid JSON")
                return
            actual_machine_id = str(identity.get("machineId") or "").casefold()
            if identity.get("status") != "VERIFIED" or actual_machine_id != target.machine_id.casefold():
                self._fail_job(
                    job_id,
                    "remote machine identity mismatch: expected "
                    f"{target.machine_id}, received {actual_machine_id or 'unverified'}",
                )
                return
            self._update_job(
                job_id,
                status="running",
                verified_machine_id=actual_machine_id,
                verified_at=identity.get("verifiedAtUtc") or _utc_now(),
            )
            try:
                result = self._runner(
                    list(scp_command),
                    **self._runner_kwargs(self.timeout_seconds),
                )
                if result.returncode == 0:
                    self._update_job(
                        job_id,
                        status="completed",
                        finished_at=_utc_now(),
                        returncode=0,
                    )
                    return
                self._fail_job(
                    job_id,
                    self._result_detail(result, "scp failed"),
                    returncode=result.returncode,
                )
            except subprocess.TimeoutExpired:
                self._fail_job(job_id, f"scp timed out after {self.timeout_seconds} seconds")
            except OSError as exc:
                self._fail_job(job_id, f"could not start scp: {exc}")
