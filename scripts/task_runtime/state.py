from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class TaskRuntimeError(RuntimeError):
    """Raised when runtime paths or persisted state are invalid."""


def empty_execution_manifest() -> dict:
    return {
        "plan_id": "",
        "status": "",
        "updated_at": "",
        "launch": {
            "status": "",
            "launched": [],
            "running": [],
            "failed": [],
        },
        "merge": {
            "status": "",
            "completed_at": "",
            "merged_agents": [],
            "conflict_agents": [],
            "cleanup": [],
        },
        "verify": {
            "status": "",
            "completed_at": "",
            "passed": None,
            "failed_commands": [],
        },
    }


def default_state() -> dict:
    return {
        "version": 2,
        "tasks": {},
        "groups": {},
        "plans": [],
        "updated_at": "",
        "execution_manifest": empty_execution_manifest(),
        "sync_audit": [],
    }


def relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


def safe_resolve(untrusted: str | Path, root: Path) -> Path:
    resolved = (root / untrusted).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise TaskRuntimeError(f"Path escapes project root: {untrusted}") from exc
    return resolved


def coerce_int(value: object, default: int = 0) -> int:
    """Safely coerce a value to int, returning default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    closed = False
    try:
        os.write(fd, content.encode("utf-8"))
        if os.name != "nt" and hasattr(os, "fchmod"):
            os.fchmod(fd, 0o644)
        os.close(fd)
        closed = True
        os.replace(tmp, str(path))
    except BaseException:
        if not closed:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_state_file(state_file: Path, state: dict, *, update_timestamp: bool = True) -> None:
    if update_timestamp:
        state["updated_at"] = now_iso()
    atomic_write(state_file, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def load_state(
    state_file: Path,
    *,
    default_factory=default_state,
    normalize_state=None,
    write_back=None,
) -> dict:
    state = default_factory()
    if state_file.exists():
        try:
            state.update(json.loads(state_file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise TaskRuntimeError(f"Cannot read state file {state_file}: {exc}") from exc

    migrated = normalize_state(state) if normalize_state else False
    if migrated:
        writer = write_back or (
            lambda payload, update_timestamp=False: write_state_file(state_file, payload, update_timestamp=update_timestamp)
        )
        writer(state, update_timestamp=False)
    return state


def save_state(
    state_file: Path,
    state: dict,
    *,
    normalize_state=None,
    write_back=None,
) -> None:
    if normalize_state:
        normalize_state(state)
    writer = write_back or (lambda payload, update_timestamp=True: write_state_file(state_file, payload, update_timestamp=update_timestamp))
    writer(state, update_timestamp=True)
