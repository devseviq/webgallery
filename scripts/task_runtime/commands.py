from __future__ import annotations

import subprocess
from pathlib import Path

_RUNTIME_COMMAND_TIMEOUT = 600

_DEFAULT_COMMAND_TIMEOUTS: dict[str, int] = {
    "compile": 120,
    "test_fast": 300,
    "build": 300,
    "test": 600,
    "test_full": 600,
}


def command_payload_entry(label: str, command: str, result: subprocess.CompletedProcess[str]) -> dict:
    return {
        "label": label,
        "command": command,
        "returncode": int(result.returncode),
        "passed": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def resolve_command_timeout(label: str, *, cfg: dict) -> int:
    cfg_timeouts = cfg.get("timeouts", {}) if isinstance(cfg.get("timeouts"), dict) else {}
    raw = cfg_timeouts.get(label)
    if raw is not None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return _DEFAULT_COMMAND_TIMEOUTS.get(label, _RUNTIME_COMMAND_TIMEOUT)


def run_runtime_command(label: str, command: str, *, root: Path, cfg: dict) -> dict:
    timeout = resolve_command_timeout(label, cfg=cfg)
    try:
        result = subprocess.run(
            command,
            cwd=str(root),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        entry = command_payload_entry(label, command, result)
    except subprocess.TimeoutExpired:
        entry = {
            "label": label,
            "command": command,
            "returncode": -1,
            "passed": False,
            "stdout": "",
            "stderr": f"Command timed out after {timeout}s",
            "timed_out": True,
            "timeout_seconds": timeout,
        }
        return entry
    except FileNotFoundError as exc:
        entry = {
            "label": label,
            "command": command,
            "returncode": -1,
            "passed": False,
            "stdout": "",
            "stderr": str(exc),
            "timed_out": False,
            "timeout_seconds": timeout,
        }
        return entry
    entry["timed_out"] = False
    entry["timeout_seconds"] = timeout
    return entry
