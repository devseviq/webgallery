#!/usr/bin/env python3
"""
Task Manager — Multi-agent campaign orchestration.

Tracks agent specs, dependencies, status, and parallel groups.
Reads project config from .codex/skills/project.toml (optional).
State persists in data/tasks.json. Auto-syncs from agents/ dir + tracker.

Usage:
    # --- Analysis ---
    python scripts/task_manager.py analyze [--json]           # Scan project → structured map

    # --- Planning ---
    python scripts/task_manager.py plan create "description"  # Create a draft plan
    python scripts/task_manager.py plan show [plan-id]        # Show plan details
    python scripts/task_manager.py plan list                  # List all plans
    python scripts/task_manager.py plan preflight [--json]    # Check autonomous execution prerequisites
    python scripts/task_manager.py plan finalize [plan-id]    # Fill required plan elements
    python scripts/task_manager.py plan go [plan-id]          # Preflight + finalize + approve + execute
    python scripts/task_manager.py plan approve [plan-id]     # Approve a plan
    python scripts/task_manager.py plan execute [plan-id]     # Register agents + templates
    python scripts/task_manager.py plan reject [plan-id]      # Reject a plan
    python scripts/task_manager.py plan diff [plan-id]        # Show changes between plan and current state
    python scripts/task_manager.py plan-add-agent <plan-id> <letter> <name> [opts]

    # --- Execution ---
    python scripts/task_manager.py sync                       # Rebuild state from specs + tracker
    python scripts/task_manager.py status [--json]            # Show task status (text or JSON)
    python scripts/task_manager.py ready [--json]             # List agents ready to launch
    python scripts/task_manager.py graph                      # ASCII dependency graph
    python scripts/task_manager.py next                       # What to do next (auto-advance)
    python scripts/task_manager.py run <agents|ready|all>     # Mark running + emit launch specs
    python scripts/task_manager.py attach <agent> ...         # Record worktree metadata for a running task
    python scripts/task_manager.py result <agent> ...         # Record structured agent output
    python scripts/task_manager.py recover [--json]           # Reset stale running tasks + report orphan worktrees
    python scripts/task_manager.py merge [agents] [--json]    # Merge completed worktree files into root
    python scripts/task_manager.py verify [plan-id] [--profile default|fast|full] [--json]  # Run post-merge verification
    python scripts/task_manager.py go [plan-id] [--json] [--poll SECONDS]  # Resume lifecycle: plan-go + launch/merge/verify
    python scripts/task_manager.py complete <agent> [-s msg]  # Mark done + unblock dependents
    python scripts/task_manager.py fail <agent> [-r reason]   # Mark failed
    python scripts/task_manager.py reset <agent>              # Reset to pending/ready
    python scripts/task_manager.py add <letter> <name> [opts] # Register a new task
    python scripts/task_manager.py template <letter> <name>   # Generate spec file from template
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import time
from contextlib import redirect_stdout

# Force UTF-8 output on Windows (cp1252 can't handle our status symbols)
if (sys.stdout.encoding or "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

import task_runtime.artifacts as _runtime_artifacts
import task_runtime.bootstrap as _runtime_bootstrap
import task_runtime.commands as _runtime_commands
import task_runtime.execution as _runtime_execution
import task_runtime.merge as _runtime_merge
import task_runtime.orchestration as _runtime_orchestration
import task_runtime.plans as _runtime_plans
import task_runtime.result as _runtime_result
import task_runtime.specs as _runtime_specs
import task_runtime.telemetry as _runtime_telemetry
import task_runtime.validation as _runtime_validation
import task_runtime.verify as _runtime_verify
from analysis.basic_provider import _normalize_string_list
from analysis.engine import run_analysis
from analysis.models import AnalysisRequest
from task_runtime import (
    TaskRuntimeError,
    atomic_write as _runtime_atomic_write,
    coerce_int as _coerce_int,
    config_path as _runtime_config_path,
    default_state as _runtime_default_state,
    derive_runtime_paths as _runtime_derive_runtime_paths,
    empty_execution_manifest as _runtime_empty_execution_manifest,
    get_conflict_zones as _runtime_get_conflict_zones,
    get_first_party as _runtime_get_first_party,
    get_module_map as _runtime_get_module_map,
    load_config as _runtime_load_config,
    load_state as _runtime_load_state,
    load_toml_file as _runtime_load_toml_file,
    now_iso as _runtime_now_iso,
    parse_toml_simple as _runtime_parse_toml_simple,
    relative_path as _runtime_relative_path,
    safe_resolve as _runtime_safe_resolve,
    save_state as _runtime_save_state,
    write_state_file as _runtime_write_state_file,
)

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Project config — loaded from .codex/skills/project.toml
# ---------------------------------------------------------------------------


def _config_path_for(root: Path | None = None) -> Path:
    return _runtime_config_path(root or ROOT)


def _skills_dir_for(root: Path | None = None) -> Path:
    return _config_path_for(root).parent


def _contract_path_for(root: Path | None = None) -> Path:
    return _skills_dir_for(root) / "planning-contract.md"


def _template_path_for(root: Path | None = None) -> Path:
    return _skills_dir_for(root) / "project.toml.template"


_CONFIG_PATH = _config_path_for()


def _parse_toml_simple(path: Path) -> dict:
    return _runtime_parse_toml_simple(path)


def _load_toml_file(path: Path) -> dict:
    return _runtime_load_toml_file(path)


def _load_config() -> dict:
    """Load project config. Returns empty dict if no config file."""
    return _runtime_load_config(_CONFIG_PATH)


def _refresh_runtime_bindings() -> None:
    global _CONFIG_PATH, _CFG, _RUNTIME_PATHS, AGENTS_DIR, STATE_FILE, ANALYSIS_CACHE_FILE, PLANS_DIR
    global _tracker_str, TRACKER_FILE, CONVENTIONS_FILE

    _CONFIG_PATH = _config_path_for()
    _CFG = _runtime_load_config(_CONFIG_PATH)
    _RUNTIME_PATHS = _runtime_derive_runtime_paths(ROOT, _CFG)

    AGENTS_DIR = _RUNTIME_PATHS["agents_dir"]
    STATE_FILE = _RUNTIME_PATHS["state_file"]
    ANALYSIS_CACHE_FILE = _RUNTIME_PATHS["analysis_cache_file"]
    PLANS_DIR = _RUNTIME_PATHS["plans_dir"]
    _tracker_str = _RUNTIME_PATHS["tracker_path"]
    TRACKER_FILE = _RUNTIME_PATHS["tracker_file"]
    CONVENTIONS_FILE = _CFG.get("project", {}).get("conventions", "AGENTS.md")


def _get_module_map() -> dict:
    return _runtime_get_module_map(ROOT, _CFG)


def _get_first_party() -> set:
    return _runtime_get_first_party(ROOT, _CFG)


def _get_conflict_zones() -> list:
    return _runtime_get_conflict_zones(_CFG)


_refresh_runtime_bindings()
_analysis_cache: dict | None = None
_analysis_cache_key_value: str | None = None
_analysis_cache_file_mtime: int | None = None
_agents_dir_mtime: int | None = None
_tracker_file_mtime: int | None = None
_state_file_mtime: int | None = None
_last_sync_state: dict | None = None

class TaskManagerError(TaskRuntimeError):
    """Raised when task-manager state or inputs are invalid."""


def _default_state() -> dict:
    return _runtime_default_state()


def _relative_path(path: Path) -> str:
    return _runtime_relative_path(path, ROOT)


def _safe_resolve(untrusted: str | Path, base: Path | None = None) -> Path:
    """Resolve *untrusted* relative to *base* (default ROOT) and verify it stays inside."""
    if base is None:
        base = ROOT
    try:
        return _runtime_safe_resolve(untrusted, base)
    except TaskRuntimeError as exc:
        raise TaskManagerError(str(exc)) from exc


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via temp-file + rename."""
    _runtime_atomic_write(path, content)


def _slugify(text: str, max_words: int = 4) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:max_words]) or "plan"


def _validate_agent_id(agent_id: str):
    if not re.fullmatch(r"[a-z]+", agent_id):
        raise TaskManagerError(f"Invalid agent ID '{agent_id}'. Use lowercase letters only.")


def _plan_file_path(plan_id: str) -> Path:
    return PLANS_DIR / f"{plan_id}.json"


def _plan_doc_path(plan: dict) -> str:
    slug = plan.get("slug") or _slugify(plan.get("description", "") or plan.get("id", "plan"))
    return f"docs/campaign-{plan['id']}-{slug}.md"


def _commands_cfg() -> dict:
    return _CFG.get("commands", {})


def _analysis_cache_file() -> Path:
    return _runtime_derive_runtime_paths(ROOT, _CFG)["analysis_cache_file"]


def _analysis_cache_key_segments() -> dict[str, str] | None:
    """Return per-provider cache key segments.

    Returns a dict with keys ``base``, ``basic``, and ``dotnet-cli``, or
    ``None`` if an :exc:`OSError` is encountered.

    - **base**: SHA-256 of the resolved ``ROOT`` path and the serialised
      ``_CFG`` config (provider-independent identity).
    - **basic**: SHA-256 of mtime/size for source files only
      (``.py``, ``.cs``, ``.xaml``, ``.cpp``, ``.h``, ``.ts``, ``.js``,
      ``.csproj``, ``.sln``).  Documentation-only extensions (``.md``,
      ``.txt``, ``.rst``, ``.json``) are excluded.
    - **dotnet-cli**: SHA-256 of mtime/size for ``.csproj``, ``.sln``,
      ``.targets``, and ``.props`` files only.
    """
    skip_dirs = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "venv",
    }
    cache_file = _analysis_cache_file().resolve()

    _source_exts = {".py", ".cs", ".xaml", ".cpp", ".h", ".ts", ".js", ".csproj", ".sln"}
    _dotnet_exts = {".csproj", ".sln", ".targets", ".props"}

    base_hasher = hashlib.sha256()
    basic_hasher = hashlib.sha256()
    dotnet_hasher = hashlib.sha256()

    try:
        root_str = str(ROOT.resolve()).encode("utf-8")
        cfg_bytes = json.dumps(_CFG, sort_keys=True, ensure_ascii=False).encode("utf-8")
        base_hasher.update(root_str)
        base_hasher.update(cfg_bytes)

        for path in sorted(ROOT.rglob("*")):
            if path.is_dir():
                continue
            if any(part in skip_dirs for part in path.parts):
                continue
            if path.resolve() == cache_file:
                continue
            stat = path.stat()
            suffix = path.suffix.lower()
            rel = _relative_path(path).encode("utf-8")
            size = str(stat.st_size).encode("ascii")
            content_digest: bytes | None = None
            if suffix in _source_exts or suffix in _dotnet_exts:
                content_hasher = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(65536), b""):
                        content_hasher.update(chunk)
                content_digest = content_hasher.hexdigest().encode("ascii")
            if suffix in _source_exts:
                basic_hasher.update(rel)
                basic_hasher.update(size)
                if content_digest is not None:
                    basic_hasher.update(content_digest)
            if suffix in _dotnet_exts:
                dotnet_hasher.update(rel)
                dotnet_hasher.update(size)
                if content_digest is not None:
                    dotnet_hasher.update(content_digest)
    except OSError:
        return None

    return {
        "base": base_hasher.hexdigest(),
        "basic": basic_hasher.hexdigest(),
        "dotnet-cli": dotnet_hasher.hexdigest(),
    }


def _analysis_cache_key() -> str | None:
    global _analysis_cache_file_mtime, _analysis_cache_key_value
    # Fast path: if the cache file mtime hasn't changed and we already have
    # a computed key, skip the expensive rglob walk.
    try:
        cf = _analysis_cache_file()
        current_mtime = cf.stat().st_mtime_ns if cf.exists() else 0
    except OSError:
        current_mtime = 0
    if (
        _analysis_cache_key_value is not None
        and _analysis_cache_file_mtime is not None
        and current_mtime == _analysis_cache_file_mtime
    ):
        return _analysis_cache_key_value
    segments = _analysis_cache_key_segments()
    if segments is None:
        return None
    hasher = hashlib.sha256()
    for key in sorted(segments):
        hasher.update(segments[key].encode("ascii"))
    result = hasher.hexdigest()
    _analysis_cache_key_value = result
    _analysis_cache_file_mtime = current_mtime
    return result


def _load_analysis_cache_snapshot(cache_file: Path, cache_key: str) -> dict | None:
    if not cache_file.exists():
        return None
    try:
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    analysis = payload.get("analysis") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or payload.get("key") != cache_key or not isinstance(analysis, dict):
        return None
    return analysis


def _save_analysis_cache_snapshot(cache_file: Path, cache_key: str, analysis: dict) -> None:
    try:
        _atomic_write(
            cache_file,
            json.dumps({"key": cache_key, "analysis": analysis}, separators=(",", ":"), ensure_ascii=False) + "\n",
        )
    except OSError:
        return


def _tracker_path_display() -> str:
    if TRACKER_FILE is None:
        return ""
    if _tracker_str and (ROOT / _tracker_str) == TRACKER_FILE:
        return _tracker_str
    return _relative_path(TRACKER_FILE)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def load_state() -> dict:
    try:
        return _runtime_load_state(
            STATE_FILE,
            default_factory=_default_state,
            normalize_state=_normalize_state,
            write_back=_write_state_file,
        )
    except TaskRuntimeError as exc:
        raise TaskManagerError(str(exc)) from exc


def save_state(state: dict):
    global _state_file_mtime, _last_sync_state
    try:
        _runtime_save_state(
            STATE_FILE,
            state,
            normalize_state=_normalize_state,
            write_back=_write_state_file,
        )
        # Invalidate the sync cache after explicit writes so the next sync_state()
        # re-reads and reconciles the saved state against current specs/tracker.
        _state_file_mtime = None
        _last_sync_state = None
    except TaskRuntimeError as exc:
        raise TaskManagerError(str(exc)) from exc


def _write_state_file(state: dict, update_timestamp: bool = True):
    _runtime_write_state_file(STATE_FILE, state, update_timestamp=update_timestamp)


def _now_iso() -> str:
    return _runtime_now_iso()


def _empty_plan_elements(title: str = "") -> dict:
    return _runtime_plans.empty_plan_elements(
        title,
        default_verification_strategy=_plan_default_verification_strategy(),
    )


def _plan_planning_context(plan: dict) -> dict:
    return _runtime_plans.plan_planning_context(plan)


def _planning_context_conflict_zone_analysis(planning_context: dict) -> list[dict]:
    return _runtime_plans.planning_context_conflict_zone_analysis(planning_context)


def _planning_context_integration_points(planning_context: dict) -> list[str]:
    return _runtime_plans.planning_context_integration_points(
        planning_context,
        normalize_string_list=_normalize_string_list,
    )


def _refresh_plan_elements(plan: dict):
    _runtime_plans.refresh_plan_elements(
        plan,
        empty_plan_elements_factory=_empty_plan_elements,
        normalize_string_list=_normalize_string_list,
    )


def _plan_summary(plan: dict) -> dict:
    return _runtime_plans.plan_summary(
        plan,
        relative_path=_relative_path,
        plan_file_path=_plan_file_path,
        plan_doc_path=_plan_doc_path,
    )


def _looks_like_full_plan(entry: dict) -> bool:
    return _runtime_plans.looks_like_full_plan(entry)


def _plan_default_verification_strategy() -> list[str]:
    strategy: list[str] = []
    command_cfg = _commands_cfg()
    for key in ("compile", "test", "build"):
        cmd = str(command_cfg.get(key, "")).strip()
        if cmd:
            strategy.append(cmd)
    if not strategy:
        strategy.append("No configured verification commands.")
    return strategy


def _default_plan_fields(plan: dict) -> dict:
    return _runtime_plans.default_plan_fields(
        plan,
        empty_plan_elements_factory=_empty_plan_elements,
        plan_default_verification_strategy=_plan_default_verification_strategy,
        slugify=_slugify,
        relative_path=_relative_path,
        plan_file_path=_plan_file_path,
        plan_doc_path=_plan_doc_path,
        normalize_string_list=_normalize_string_list,
    )


def _markdown_escape(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    return _runtime_artifacts.markdown_table(headers, rows)


def _render_markdown_list(items, empty_text: str) -> str:
    return _runtime_artifacts.render_markdown_list(
        items,
        empty_text=empty_text,
        normalize_string_list=_normalize_string_list,
    )


def _render_dependency_graph(plan: dict) -> str:
    return _runtime_artifacts.render_dependency_graph(plan)


def _render_plan_doc(plan: dict) -> str:
    return _runtime_artifacts.render_plan_doc(
        plan,
        default_plan_fields=_default_plan_fields,
        refresh_plan_elements=_refresh_plan_elements,
        normalize_string_list=_normalize_string_list,
    )


def _write_plan_doc(plan: dict) -> str:
    doc_path = _safe_resolve(plan.get("plan_doc") or _plan_doc_path(plan))
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(doc_path, _render_plan_doc(plan))
    return _relative_path(doc_path)


def _persist_plan_artifacts(plan: dict) -> dict:
    return _runtime_artifacts.persist_plan_artifacts(
        plan,
        default_plan_fields=_default_plan_fields,
        refresh_plan_elements=_refresh_plan_elements,
        now_iso=_now_iso,
        write_plan_doc_fn=_write_plan_doc,
        plan_file_path=_plan_file_path,
        atomic_write=_atomic_write,
    )


def _write_plan_file(plan: dict) -> dict:
    """Backward-compatible wrapper for older call sites."""
    return _persist_plan_artifacts(plan)


def _load_plan_from_summary(summary: dict) -> dict:
    if _looks_like_full_plan(summary):
        return _backfill_legacy_plan(summary)

    plan_file = summary.get("plan_file")
    candidates: list[Path] = []
    if plan_file:
        candidates.append(_safe_resolve(plan_file))
    if summary.get("id"):
        candidates.append(_plan_file_path(summary["id"]))

    for path in candidates:
        if path.exists():
            try:
                raw_plan = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TaskManagerError(f"Cannot read plan file {path}: {exc}") from exc
            plan = _backfill_legacy_plan(raw_plan)
            if plan != raw_plan:
                _persist_plan_artifacts(plan)
            return plan

    raise TaskManagerError(f"Plan {summary.get('id', '?')} has no readable plan file.")


def _upsert_plan_summary(state: dict, plan: dict):
    summary = _plan_summary(plan)
    plans = state.setdefault("plans", [])
    for idx, existing in enumerate(plans):
        if existing.get("id") == summary["id"]:
            plans[idx] = summary
            return
    plans.append(summary)


def _normalize_state(state: dict) -> bool:
    mutated = False
    defaults = _default_state()
    for key, value in defaults.items():
        if key not in state:
            state[key] = value
            mutated = True
    if state.get("version", 1) < 2:
        state["version"] = 2
        mutated = True
    if _ensure_execution_manifest(state):
        mutated = True

    normalized_plans: list[dict] = []
    for entry in state.get("plans", []):
        if not isinstance(entry, dict) or not entry.get("id"):
            mutated = True
            continue
        if _looks_like_full_plan(entry):
            migrated = _backfill_legacy_plan(entry)
            migrated = _write_plan_file(migrated)
            normalized_plans.append(_plan_summary(migrated))
            mutated = True
        else:
            summary = dict(entry)
            if not summary.get("plan_file"):
                summary["plan_file"] = _relative_path(_plan_file_path(summary["id"]))
                mutated = True
            if not summary.get("plan_doc"):
                summary["plan_doc"] = _plan_doc_path(summary)
                mutated = True
            try:
                full_plan = _load_plan_from_summary(summary)
                if summary.get("legacy_status") != full_plan.get("legacy_status"):
                    summary["legacy_status"] = full_plan.get("legacy_status", "")
                    mutated = True
            except TaskManagerError:
                pass
            normalized_plans.append(summary)
    if normalized_plans != state.get("plans", []):
        state["plans"] = normalized_plans
        mutated = True

    tasks = state.get("tasks", {})
    if not isinstance(tasks, dict):
        state["tasks"] = {}
        tasks = state["tasks"]
        mutated = True

    for task_id, task in list(tasks.items()):
        if not isinstance(task, dict):
            state["tasks"][task_id] = _new_task_record(
                task_id,
                str(task_id),
                spec_file="",
                scope="",
                status="pending",
                deps=[],
                files=[],
                group=0,
            )
            mutated = True
            continue

        task.setdefault("id", task_id)
        task.setdefault("name", f"agent-{task_id}")
        task.setdefault("spec_file", "")
        task.setdefault("scope", "")
        task.setdefault("status", "pending")
        task.setdefault("deps", [])
        task.setdefault("files", [])
        task.setdefault("group", 0)
        task.setdefault("tracker_id", "")
        task.setdefault("started_at", "")
        task.setdefault("completed_at", "")
        task.setdefault("summary", "")
        task.setdefault("error", "")
        task.setdefault("complexity", "low")
        task["deps"] = [dep.lower() for dep in _normalize_string_list(task.get("deps", []))]
        task["files"] = [item.replace("\\", "/") for item in _normalize_string_list(task.get("files", []))]
        if _ensure_task_runtime_fields(task):
            mutated = True
    return mutated


def _command_signature(command: str) -> str:
    return _runtime_validation.command_signature(command)


def _validation_contains_command(strategy: list[str], command: str) -> bool:
    return _runtime_validation.validation_contains_command(strategy, command)


def _empty_agent_result() -> dict:
    return {
        "status": "",
        "files_modified": [],
        "tests_passed": 0,
        "tests_failed": 0,
        "issues": [],
        "input_tokens": 0,
        "output_tokens": 0,
        "summary": "",
        "worktree_path": "",
        "branch": "",
        "reported_at": "",
    }


def _empty_launch_record() -> dict:
    return {
        "worktree_path": "",
        "branch": "",
        "recorded_at": "",
    }


def _empty_merge_record() -> dict:
    return {
        "status": "",
        "applied_files": [],
        "conflicts": [],
        "merged_at": "",
        "detail": "",
    }


def _empty_execution_manifest() -> dict:
    return _runtime_empty_execution_manifest()


def _ensure_execution_manifest(state: dict) -> bool:
    mutated = False
    manifest = state.get("execution_manifest")
    if not isinstance(manifest, dict):
        state["execution_manifest"] = _empty_execution_manifest()
        manifest = state["execution_manifest"]
        mutated = True

    defaults = _empty_execution_manifest()
    for key, value in defaults.items():
        if key not in manifest:
            manifest[key] = value
            mutated = True

    manifest["plan_id"] = str(manifest.get("plan_id", "") or "")
    manifest["status"] = str(manifest.get("status", "") or "")
    manifest["updated_at"] = str(manifest.get("updated_at", "") or "")

    for section_name in ("launch", "merge", "verify"):
        section = manifest.get(section_name)
        if not isinstance(section, dict):
            manifest[section_name] = defaults[section_name]
            mutated = True
            continue
        for key, value in defaults[section_name].items():
            if key not in section:
                section[key] = value
                mutated = True

    manifest["launch"]["status"] = str(manifest["launch"].get("status", "") or "")
    manifest["launch"]["launched"] = [item.lower() for item in _normalize_string_list(manifest["launch"].get("launched", []))]
    manifest["launch"]["running"] = [item.lower() for item in _normalize_string_list(manifest["launch"].get("running", []))]
    manifest["launch"]["failed"] = [item.lower() for item in _normalize_string_list(manifest["launch"].get("failed", []))]

    manifest["merge"]["status"] = str(manifest["merge"].get("status", "") or "")
    manifest["merge"]["completed_at"] = str(manifest["merge"].get("completed_at", "") or "")
    manifest["merge"]["merged_agents"] = [item.lower() for item in _normalize_string_list(manifest["merge"].get("merged_agents", []))]
    manifest["merge"]["conflict_agents"] = [item.lower() for item in _normalize_string_list(manifest["merge"].get("conflict_agents", []))]
    if not isinstance(manifest["merge"].get("cleanup"), list):
        manifest["merge"]["cleanup"] = []
        mutated = True

    manifest["verify"]["status"] = str(manifest["verify"].get("status", "") or "")
    manifest["verify"]["completed_at"] = str(manifest["verify"].get("completed_at", "") or "")
    verify_passed = manifest["verify"].get("passed")
    if verify_passed not in (True, False, None):
        manifest["verify"]["passed"] = None
        mutated = True
    manifest["verify"]["failed_commands"] = [item for item in _normalize_string_list(manifest["verify"].get("failed_commands", []))]
    return mutated


def _persist_execution_manifest(
    state: dict,
    *,
    plan_id: str,
    status: str,
    reset_follow_on: bool = False,
    launch: dict | None = None,
    merge: dict | None = None,
    verify: dict | None = None,
) -> None:
    _ensure_execution_manifest(state)
    manifest = state["execution_manifest"]
    if manifest.get("plan_id") != plan_id:
        state["execution_manifest"] = _empty_execution_manifest()
        manifest = state["execution_manifest"]
    manifest["plan_id"] = plan_id
    manifest["status"] = status
    manifest["updated_at"] = _now_iso()

    if reset_follow_on:
        manifest["merge"] = _empty_execution_manifest()["merge"]
        manifest["verify"] = _empty_execution_manifest()["verify"]

    if launch is not None:
        launch_section = manifest["launch"]
        launch_section.update(
            {
                "status": str(launch.get("status", "") or ""),
                "launched": [item.lower() for item in _normalize_string_list(launch.get("launched", []))],
                "running": [item.lower() for item in _normalize_string_list(launch.get("running", []))],
                "failed": [item.lower() for item in _normalize_string_list(launch.get("failed", []))],
            }
        )
    if merge is not None:
        merge_section = manifest["merge"]
        merge_section.update(
            {
                "status": str(merge.get("status", "") or ""),
                "completed_at": str(merge.get("completed_at", "") or ""),
                "merged_agents": [item.lower() for item in _normalize_string_list(merge.get("merged_agents", []))],
                "conflict_agents": [item.lower() for item in _normalize_string_list(merge.get("conflict_agents", []))],
                "cleanup": list(merge.get("cleanup", [])) if isinstance(merge.get("cleanup"), list) else [],
            }
        )
    if verify is not None:
        verify_section = manifest["verify"]
        verify_section.update(
            {
                "status": str(verify.get("status", "") or ""),
                "completed_at": str(verify.get("completed_at", "") or ""),
                "passed": verify.get("passed"),
                "failed_commands": [item for item in _normalize_string_list(verify.get("failed_commands", []))],
            }
        )


def _sync_execution_manifest_after_recover(state: dict, recovered: list[dict]) -> None:
    manifest = state.get("execution_manifest")
    if not isinstance(manifest, dict):
        return

    plan_id = str(manifest.get("plan_id", "") or "")
    if not plan_id:
        return

    running = sorted(task["id"] for task in state.get("tasks", {}).values() if task.get("status") == "running")
    failed = sorted(task["id"] for task in state.get("tasks", {}).values() if task.get("status") == "failed")
    ready = sorted(task["id"] for task in state.get("tasks", {}).values() if task.get("status") == "ready")

    if running:
        top_status = "awaiting_results"
        launch_status = "awaiting_results"
        launched = running
    elif ready and recovered:
        top_status = "recovered"
        launch_status = "recovered"
        launched = []
    elif failed:
        top_status = "blocked"
        launch_status = "blocked"
        launched = []
    elif recovered:
        top_status = "ready_for_merge"
        launch_status = "idle"
        launched = []
    else:
        return

    _persist_execution_manifest(
        state,
        plan_id=plan_id,
        status=top_status,
        launch={
            "status": launch_status,
            "launched": launched,
            "running": running,
            "failed": failed,
        },
    )


def _normalize_task_complexity(value: object) -> str:
    complexity = str(value or "").strip().lower()
    return complexity if complexity in {"low", "medium", "high"} else "medium"


def _ensure_task_runtime_fields(task: dict) -> bool:
    mutated = False
    complexity = _normalize_task_complexity(task.get("complexity", "low"))
    if task.get("complexity") != complexity:
        task["complexity"] = complexity
        mutated = True

    result = task.get("agent_result")
    if not isinstance(result, dict):
        task["agent_result"] = _empty_agent_result()
        mutated = True
    else:
        defaults = _empty_agent_result()
        for key, value in defaults.items():
            if key not in result:
                result[key] = value
                mutated = True
        result["files_modified"] = [item.replace("\\", "/") for item in _normalize_string_list(result.get("files_modified", []))]
        result["issues"] = _normalize_string_list(result.get("issues", []))
        result["tests_passed"] = _coerce_int(result.get("tests_passed", 0) or 0)
        result["tests_failed"] = _coerce_int(result.get("tests_failed", 0) or 0)
        result["input_tokens"] = _coerce_int(result.get("input_tokens", 0) or 0)
        result["output_tokens"] = _coerce_int(result.get("output_tokens", 0) or 0)
        for key in ("status", "summary", "worktree_path", "branch", "reported_at"):
            result[key] = str(result.get(key, "") or "")

    launch = task.get("launch")
    if not isinstance(launch, dict):
        task["launch"] = _empty_launch_record()
        mutated = True
    else:
        defaults = _empty_launch_record()
        for key, value in defaults.items():
            if key not in launch:
                launch[key] = value
                mutated = True
        for key in ("worktree_path", "branch", "recorded_at"):
            launch[key] = str(launch.get(key, "") or "")

    merge_record = task.get("merge")
    if not isinstance(merge_record, dict):
        task["merge"] = _empty_merge_record()
        mutated = True
    else:
        defaults = _empty_merge_record()
        for key, value in defaults.items():
            if key not in merge_record:
                merge_record[key] = value
                mutated = True
        merge_record["status"] = str(merge_record.get("status", "") or "")
        merge_record["detail"] = str(merge_record.get("detail", "") or "")
        merge_record["merged_at"] = str(merge_record.get("merged_at", "") or "")
        merge_record["applied_files"] = [item.replace("\\", "/") for item in _normalize_string_list(merge_record.get("applied_files", []))]
        merge_record["conflicts"] = [item.replace("\\", "/") for item in _normalize_string_list(merge_record.get("conflicts", []))]

    return mutated


def _new_task_record(
    letter: str,
    name: str,
    *,
    spec_file: str,
    scope: str,
    status: str,
    deps: list[str],
    files: list[str],
    group: int = 0,
    complexity: str = "low",
) -> dict:
    task = {
        "id": letter,
        "name": name,
        "spec_file": spec_file,
        "scope": scope,
        "status": status,
        "deps": list(deps),
        "files": [item.replace("\\", "/") for item in files],
        "group": group,
        "complexity": complexity,
        "tracker_id": "",
        "started_at": "",
        "completed_at": "",
        "summary": "",
        "error": "",
    }
    _ensure_task_runtime_fields(task)
    return task


def _validate_plan_elements(plan: dict, strict: bool = True) -> list[str]:
    if not strict:
        return []
    return _runtime_validation.validate_plan_elements(
        plan,
        default_plan_fields=_default_plan_fields,
        normalize_string_list=_normalize_string_list,
        commands_cfg=_commands_cfg,
    )


def _validate_agent_roster(plan: dict, strict: bool = True) -> list[str]:
    return _runtime_validation.validate_agent_roster(
        plan,
        default_plan_fields=_default_plan_fields,
        normalize_string_list=_normalize_string_list,
        compute_dependency_depths=_compute_dependency_depths,
        error_type=TaskManagerError,
        strict=strict,
    )


def _validate_file_ownership(plan: dict) -> list[str]:
    return _runtime_validation.validate_file_ownership(
        plan,
        default_plan_fields=_default_plan_fields,
        normalize_string_list=_normalize_string_list,
    )


def _plan_validation_warnings(plan: dict) -> list[str]:
    return _runtime_validation.plan_validation_warnings(
        plan,
        default_plan_fields=_default_plan_fields,
        normalize_string_list=_normalize_string_list,
        commands_cfg=_commands_cfg,
        safe_resolve=_safe_resolve,
        plan_planning_context=_plan_planning_context,
    )


def _mark_plan_needs_backfill(plan: dict) -> dict:
    return _runtime_validation.mark_plan_needs_backfill(
        plan,
        normalize_string_list=_normalize_string_list,
        validate_plan_fn=_validate_plan,
    )


def _backfill_legacy_plan(plan: dict) -> dict:
    return _runtime_validation.backfill_legacy_plan(
        plan,
        default_plan_fields=_default_plan_fields,
        refresh_plan_elements=_refresh_plan_elements,
        mark_plan_needs_backfill_fn=_mark_plan_needs_backfill,
    )


def _validate_plan(plan: dict, strict: bool = True) -> list[str]:
    return _runtime_validation.validate_plan(
        plan,
        validate_plan_elements_fn=_validate_plan_elements,
        validate_agent_roster_fn=_validate_agent_roster,
        validate_file_ownership_fn=_validate_file_ownership,
        strict=strict,
    )


# ---------------------------------------------------------------------------
# Spec + tracker parsing
# ---------------------------------------------------------------------------


def _extract_markdown_section(text: str, heading: str) -> str:
    return _runtime_specs.extract_markdown_section(text, heading)


def _extract_spec_exit_criteria(text: str) -> list[str]:
    return _runtime_specs.extract_spec_exit_criteria(text)


def _spec_has_placeholders(text: str) -> bool:
    return _runtime_specs.spec_has_placeholders(text)


def _validate_spec_file(path: Path, agent_id: str | None = None, strict: bool = True) -> list[str]:
    return _runtime_specs.validate_spec_file(
        path,
        relative_path=_relative_path,
        agent_id=agent_id,
        strict=strict,
    )


def parse_spec_file(path: Path) -> dict:
    return _runtime_specs.parse_spec_file(
        path,
        relative_path=_relative_path,
        error_type=TaskManagerError,
    )


def parse_tracker() -> dict[str, dict]:
    return _runtime_specs.parse_tracker(TRACKER_FILE)


def _build_tracker_prefix_map(state: dict) -> dict[str, str]:
    return _runtime_specs.build_tracker_prefix_map(state)


# ---------------------------------------------------------------------------
# Dependency graph helpers
# ---------------------------------------------------------------------------


def _compute_dependency_depths(deps_map: dict[str, list[str]], subject: str) -> dict[str, int]:
    return _runtime_execution.compute_dependency_depths(
        deps_map,
        subject,
        error_type=TaskManagerError,
    )


def _assign_groups(state: dict):
    _runtime_execution.assign_groups(
        state,
        compute_dependency_depths_fn=_compute_dependency_depths,
    )


def _recompute_ready(state: dict):
    _runtime_execution.recompute_ready(state)


# ---------------------------------------------------------------------------
# Sync — the core auto-discovery routine
# ---------------------------------------------------------------------------


def _sync_cache_mtimes() -> tuple[int | None, int | None, int | None]:
    try:
        mtimes = [AGENTS_DIR.stat().st_mtime_ns]
        mtimes.extend(path.stat().st_mtime_ns for path in AGENTS_DIR.glob("agent-*-*.md"))
        current_agents_mtime: int | None = max(mtimes)
    except OSError:
        current_agents_mtime = None

    try:
        current_tracker_mtime = TRACKER_FILE.stat().st_mtime_ns if TRACKER_FILE is not None else None
    except OSError:
        current_tracker_mtime = None

    try:
        current_state_mtime = STATE_FILE.stat().st_mtime_ns
    except OSError:
        current_state_mtime = None

    return current_agents_mtime, current_tracker_mtime, current_state_mtime


def sync_state() -> dict:
    global _agents_dir_mtime, _tracker_file_mtime, _state_file_mtime, _last_sync_state
    current_agents_mtime, current_tracker_mtime, current_state_mtime = _sync_cache_mtimes()
    if (
        _agents_dir_mtime is not None
        and current_agents_mtime == _agents_dir_mtime
        and current_tracker_mtime == _tracker_file_mtime
        and current_state_mtime == _state_file_mtime
        and _last_sync_state is not None
    ):
        return copy.deepcopy(_last_sync_state)
    result = _runtime_execution.sync_state(
        load_state_fn=load_state,
        parse_spec_file_fn=parse_spec_file,
        parse_tracker_fn=parse_tracker,
        build_tracker_prefix_map_fn=_build_tracker_prefix_map,
        save_state_fn=save_state,
        assign_groups_fn=_assign_groups,
        recompute_ready_fn=_recompute_ready,
        agents_dir=AGENTS_DIR,
        new_task_factory=_new_task_record,
        ensure_task_fields_fn=_ensure_task_runtime_fields,
        error_type=TaskManagerError,
    )
    _agents_dir_mtime, _tracker_file_mtime, _state_file_mtime = _sync_cache_mtimes()
    _last_sync_state = result
    return copy.deepcopy(result)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

_SYM = _runtime_execution.STATUS_SYMBOLS


def _emit_json(payload: dict | list):
    _runtime_execution.emit_json(payload)


def cmd_sync(_args):
    _runtime_execution.cmd_sync(_args, sync_state_fn=sync_state, state_file=STATE_FILE)


def cmd_status(_args):
    _runtime_execution.cmd_status(
        _args,
        sync_state_fn=sync_state,
        cfg=_CFG,
        sym_map=_SYM,
        emit_json_fn=_emit_json,
    )


def cmd_ready(args):
    _runtime_execution.cmd_ready(args, sync_state_fn=sync_state, emit_json_fn=_emit_json)


def cmd_run(args):
    _runtime_execution.cmd_run(
        args,
        sync_state_fn=sync_state,
        now_iso_fn=_now_iso,
        safe_resolve_fn=_safe_resolve,
        validate_spec_file_fn=_validate_spec_file,
        save_state_fn=save_state,
        build_agent_prompt_fn=_build_agent_prompt,
        emit_json_fn=_emit_json,
        cfg=_CFG,
        ensure_task_fields_fn=_ensure_task_runtime_fields,
    )


def _build_agent_prompt(task: dict, spec_text: str) -> str:
    return _runtime_execution.build_agent_prompt(
        task,
        spec_text,
        conventions_file=CONVENTIONS_FILE,
    )


def cmd_complete(args):
    _runtime_execution.cmd_complete(
        args,
        load_state_fn=load_state,
        now_iso_fn=_now_iso,
        recompute_ready_fn=_recompute_ready,
        save_state_fn=save_state,
        ensure_task_fields_fn=_ensure_task_runtime_fields,
        empty_merge_record_factory=_empty_merge_record,
    )


def cmd_fail(args):
    _runtime_execution.cmd_fail(
        args,
        load_state_fn=load_state,
        now_iso_fn=_now_iso,
        save_state_fn=save_state,
        ensure_task_fields_fn=_ensure_task_runtime_fields,
        normalize_string_list_fn=_normalize_string_list,
        empty_merge_record_factory=_empty_merge_record,
    )


def cmd_reset(args):
    _runtime_execution.cmd_reset(
        args,
        load_state_fn=load_state,
        recompute_ready_fn=_recompute_ready,
        save_state_fn=save_state,
        ensure_task_fields_fn=_ensure_task_runtime_fields,
        empty_agent_result_factory=_empty_agent_result,
        empty_merge_record_factory=_empty_merge_record,
    )


def cmd_graph(_args):
    _runtime_execution.cmd_graph(_args, sync_state_fn=sync_state, sym_map=_SYM)


def cmd_next(_args):
    _runtime_execution.cmd_next(_args, sync_state_fn=sync_state)


def cmd_add(args):
    _runtime_execution.cmd_add(
        args,
        sync_state_fn=sync_state,
        validate_agent_id_fn=_validate_agent_id,
        assign_groups_fn=_assign_groups,
        recompute_ready_fn=_recompute_ready,
        save_state_fn=save_state,
        safe_resolve_fn=_safe_resolve,
        new_task_factory=_new_task_record,
    )


def cmd_new(args):
    _runtime_execution.cmd_new(
        args,
        sync_state_fn=sync_state,
        next_agent_letter_fn=_next_agent_letter,
        cmd_add_fn=cmd_add,
        cmd_template_fn=cmd_template,
    )


def cmd_template(args):
    _runtime_execution.cmd_template(
        args,
        validate_agent_id_fn=_validate_agent_id,
        agents_dir=AGENTS_DIR,
        render_spec_template_fn=_render_spec_template,
    )


def _configured_verification_commands(files: list[str] | None = None) -> list[str]:
    return _runtime_specs.configured_verification_commands(_commands_cfg(), files)


def _configured_runtime_commands(profile: str = "default", files: list[str] | None = None) -> list[tuple[str, str]]:
    return _runtime_specs.configured_runtime_commands(_commands_cfg(), profile=profile, files=files)


def _default_exit_criteria(agent: dict, plan: dict | None = None) -> list[str]:
    return _runtime_specs.default_exit_criteria(
        agent,
        plan=plan,
        normalize_string_list_fn=_normalize_string_list,
    )


def _build_post_completion_section(tracker_prefix: str, scope: str, files_str: str, owner_label: str) -> str:
    return _runtime_specs.build_post_completion_section(
        _tracker_path_display(),
        tracker_prefix,
        scope,
        files_str,
        owner_label,
    )


def _render_spec_template(
    letter: str,
    name: str,
    scope: str,
    deps: list[str] | None = None,
    files: list[str] | None = None,
    plan: dict | None = None,
) -> str:
    return _runtime_specs.render_spec_template(
        letter,
        name,
        scope,
        deps=deps,
        files=files,
        plan=plan,
        conventions_file=CONVENTIONS_FILE,
        tracker_path=_tracker_path_display(),
        command_cfg=_commands_cfg(),
        normalize_string_list_fn=_normalize_string_list,
    )


# ---------------------------------------------------------------------------
# Codebase analysis
# ---------------------------------------------------------------------------


def analyze_project() -> dict:
    """Build a structured project map for planning decisions."""
    global _analysis_cache, _analysis_cache_key_value
    cache_key = _analysis_cache_key()
    if cache_key and _analysis_cache is not None and _analysis_cache_key_value == cache_key:
        return _analysis_cache

    cache_file = _analysis_cache_file()
    if cache_key:
        cached = _load_analysis_cache_snapshot(cache_file, cache_key)
        if cached is not None:
            _analysis_cache = cached
            _analysis_cache_key_value = cache_key
            return cached

    analysis = run_analysis(
        AnalysisRequest(
            root=ROOT,
            cfg=_CFG,
            generated_at=_now_iso(),
        )
    )
    _analysis_cache = analysis
    _analysis_cache_key_value = cache_key
    if cache_key:
        _save_analysis_cache_snapshot(cache_file, cache_key, analysis)
    return analysis


def cmd_analyze(args):
    """Scan the project and output a structured map."""
    with _runtime_telemetry.StepTimer("analyze") as analyze_timer:
        analysis = analyze_project()

    if getattr(args, "json", False):
        payload = dict(analysis)
        payload["telemetry"] = _runtime_telemetry.build_telemetry_payload(
            timers=[analyze_timer],
            analysis_json_bytes=_runtime_telemetry.measure_json_bytes(analysis),
        )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    # Human-readable summary
    print("=" * 74)
    print("  PROJECT ANALYSIS")
    print("=" * 74)

    print(f"\n  Root: {analysis.get('root', '.')}")
    totals = analysis.get("totals", {})
    print(f"  Files: {totals.get('files', 0)}  |  Lines: {totals.get('lines', 0):,}")
    if analysis.get("detected_stacks"):
        print(f"  Detected stacks: {', '.join(analysis['detected_stacks'])}")
    planning_context = analysis.get("analysis_v2", {}).get("planning_context", {})
    analysis_health = planning_context.get("analysis_health", {})
    if analysis_health.get("applied_providers"):
        print(f"  Providers: {', '.join(analysis_health['applied_providers'])}")
    if analysis_health.get("warnings"):
        print(f"  Analysis health: {analysis_health.get('confidence', 'unknown')}  |  {analysis_health['warnings'][0]}")
    graph = analysis.get("project_graph", {})
    if graph.get("nodes"):
        print(f"  Project graph: {len(graph['nodes'])} nodes  |  {len(graph.get('edges', []))} edges")
        startup_nodes = [node for node in graph["nodes"] if node.get("kind") == "solution" and node.get("startup_project")]
        if startup_nodes:
            print(f"  Startup project: {startup_nodes[0]['startup_project']}")

    print("\n  Modules:")
    for cat, info in sorted(analysis.get("modules", {}).items(), key=lambda x: -x[1].get("total_lines", 0)):
        print(f"    {cat:12s}  {info['file_count']:3d} files  {info['total_lines']:6,} lines")

    # Top files by size
    big_files = sorted(analysis.get("files", []), key=lambda x: -_coerce_int(x.get("lines", 0) or 0))[:10]
    print("\n  Largest files:")
    for f in big_files:
        line_count = _coerce_int(f.get("lines", 0) or 0)
        extras = ""
        if f.get("classes"):
            extras += f"  classes: {', '.join(f['classes'][:3])}"
        if f.get("imports"):
            extras += f"  imports: {', '.join(f['imports'])}"
        if f.get("types"):
            extras += f"  types: {', '.join(f['types'][:3])}"
        if f.get("includes"):
            extras += f"  includes: {', '.join(f['includes'][:3])}"
        if f.get("xaml_class"):
            extras += f"  xaml: {f['xaml_class']}"
        print(f"    {line_count:5,} lines  {f['path']}{extras}")

    # Dependency edges
    if analysis.get("dependency_edges"):
        print("\n  Cross-module imports:")
        for edge in analysis["dependency_edges"]:
            print(f"    {edge['from']}  \u2192  {edge['to']}")

    # Conflict zones
    print("\n  Conflict zones (files that often change together):")
    for cz in analysis.get("conflict_zones", []):
        print(f"    {', '.join(cz['files'])}  \u2014  {cz['reason']}")

    print(f"\n  Analyzed at: {analysis.get('analyzed_at', '')}")
    print("=" * 74)


# ---------------------------------------------------------------------------
# Plan workflow: create → show → approve → execute
# ---------------------------------------------------------------------------


def _next_plan_id(state: dict) -> str:
    max_id = 0
    for summary in state.get("plans", []):
        match = re.match(r"plan-(\d+)$", summary.get("id", ""))
        if match:
            max_id = max(max_id, int(match.group(1)))
    for plan_file in PLANS_DIR.glob("plan-*.json"):
        match = re.match(r"plan-(\d+)\.json$", plan_file.name)
        if match:
            max_id = max(max_id, int(match.group(1)))
    return f"plan-{max_id + 1:03d}"


def _next_agent_letter(state: dict) -> str:
    def to_number(agent_id: str) -> int:
        value = 0
        for char in agent_id:
            value = value * 26 + (ord(char) - ord("a") + 1)
        return value

    def to_agent_id(number: int) -> str:
        chars: list[str] = []
        while number > 0:
            number -= 1
            number, remainder = divmod(number, 26)
            chars.append(chr(ord("a") + remainder))
        return "".join(reversed(chars))

    existing = [task_id for task_id in state.get("tasks", {}) if re.fullmatch(r"[a-z]+", task_id)]
    if not existing:
        return "a"
    highest = max(to_number(task_id) for task_id in existing)
    return to_agent_id(highest + 1)


def _resolve_plan_for_verify(state: dict) -> dict | None:
    # Prefer the plan_id from the execution manifest
    active_plan_id = str(state.get("execution_manifest", {}).get("plan_id", "") or "").strip()
    if active_plan_id:
        for summary in state.get("plans", []):
            if summary.get("id") == active_plan_id:
                try:
                    plan = _load_plan_from_summary(summary)
                except TaskManagerError:
                    break
                if not _validate_plan(plan, strict=True):
                    return plan

    # Fallback: most recent valid executed/approved plan
    for summary in reversed(state.get("plans", [])):
        if summary.get("status") in ("executed", "partial", "approved"):
            try:
                plan = _load_plan_from_summary(summary)
            except TaskManagerError:
                continue
            if not _validate_plan(plan, strict=True):
                return plan
    return None


def _explain_verify_resolution_failure(state: dict) -> str:
    for summary in reversed(state.get("plans", [])):
        if summary.get("status") not in ("executed", "partial", "approved"):
            continue
        try:
            plan = _load_plan_from_summary(summary)
        except TaskManagerError:
            continue
        errors = _validate_plan(plan, strict=True)
        if errors:
            first_error = errors[0]
            if len(errors) > 1:
                first_error += f" (+{len(errors) - 1} more)"
            return f"Latest candidate {plan['id']} is invalid: {first_error}"
    return "No executed, partial, or approved plans are available for criteria lookup."


def _plan_exit_criteria(plan: dict) -> list[str]:
    plan = _default_plan_fields(plan)
    return _normalize_string_list(plan["plan_elements"].get("exit_criteria", []))


def _plan_owned_files(plan: dict, state: dict | None = None) -> list[str]:
    seen: set[str] = set()
    owned_files: list[str] = []

    for agent in plan.get("agents", []):
        for path in _normalize_string_list(agent.get("files", [])):
            normalized = path.replace("\\", "/")
            if normalized and normalized not in seen:
                seen.add(normalized)
                owned_files.append(normalized)

    # Runtime-reported files from completed tasks (augment, not replace)
    if state:
        plan_agent_ids = {
            str(a.get("letter", "")).strip().lower()
            for a in plan.get("agents", [])
        }
        for task_id, task in state.get("tasks", {}).items():
            if task_id not in plan_agent_ids:
                continue
            if task.get("status") not in ("done", "merged"):
                continue
            for path in _normalize_string_list(
                task.get("agent_result", {}).get("files_modified", [])
            ):
                normalized = path.replace("\\", "/")
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    owned_files.append(normalized)

    return owned_files


def _normalize_verify_profile(profile: str | None) -> str:
    normalized = str(profile or "").strip().lower()
    if normalized in {"fast", "full"}:
        return normalized
    return "default"


def _default_goal_statement(plan: dict) -> str:
    description = str(plan.get("description", "")).strip()
    if description:
        return description
    return f"Complete campaign {plan.get('id', 'plan')}."


def _default_plan_finalize_exit_criteria(plan: dict) -> list[str]:
    criteria: list[str] = []
    agents = plan.get("agents", [])
    if agents:
        criteria.append("All planned agent scopes are registered and executable.")
    else:
        criteria.append("Planned work is registered and executable.")

    configured_verification = [item for item in _plan_default_verification_strategy() if item != "No configured verification commands."]
    if configured_verification:
        criteria.append("Configured verification commands are ready for post-merge validation.")
    else:
        criteria.append("Verification requirements are recorded for follow-up execution.")
    return criteria


def _backfill_plan_optional_elements(plan: dict) -> None:
    if "plan_elements" not in plan:
        plan["plan_elements"] = _empty_plan_elements(plan.get("description", "") or plan.get("id", ""))
    elements = plan["plan_elements"]
    agents = plan.get("agents", [])
    planning_context = _plan_planning_context(plan)
    analysis_health = planning_context.get("analysis_health", {})
    ownership_summary = planning_context.get("ownership_summary", {})

    if not elements.get("impact_assessment") and agents:
        impact_rows: list[dict] = []
        for agent in agents:
            files = _normalize_string_list(agent.get("files", []))
            if not files:
                impact_rows.append(
                    {
                        "file": f"agent-{agent.get('letter', '')}",
                        "lines": "",
                        "change_type": "scoped-work",
                        "risk": str(agent.get("complexity", "low") or "low"),
                    }
                )
                continue
            for file_path in files:
                impact_rows.append(
                    {
                        "file": file_path,
                        "lines": "",
                        "change_type": "modify",
                        "risk": str(agent.get("complexity", "low") or "low"),
                    }
                )
        elements["impact_assessment"] = impact_rows

    if not elements.get("risk_assessment"):
        risks: list[dict] = []
        for warning in _normalize_string_list(analysis_health.get("warnings", [])):
            risks.append(
                {
                    "risk": warning,
                    "likelihood": "medium",
                    "impact": "medium",
                    "mitigation": "Plan conservatively and verify ownership before approval.",
                }
            )

        unassigned = int(ownership_summary.get("unassigned_file_count", 0) or 0)
        if unassigned > 0:
            risks.append(
                {
                    "risk": f"{unassigned} analysis files are still unassigned.",
                    "likelihood": "medium",
                    "impact": "medium",
                    "mitigation": "Assign ownership before launch and keep merge-sensitive files with one owner.",
                }
            )

        if any(str(agent.get("complexity", "")).strip().lower() == "high" for agent in agents):
            risks.append(
                {
                    "risk": "High-complexity agent scopes may increase merge and verification cost.",
                    "likelihood": "medium",
                    "impact": "high",
                    "mitigation": "Keep file ownership narrow and preserve dependency order during merge.",
                }
            )

        if risks:
            elements["risk_assessment"] = risks


def _set_plan_list_element(elements: dict, key: str, provided, default_items: list[str] | None = None) -> bool:
    values = _normalize_string_list(provided)
    if values:
        elements[key] = values
        return True
    if not _normalize_string_list(elements.get(key, [])) and default_items:
        elements[key] = list(default_items)
        return True
    return False


def _placeholder_command_reason(command: str, *, allow_files_placeholder: bool = False) -> str:
    normalized = str(command or "").strip().lower()
    if not normalized:
        return ""
    if "todo" in normalized:
        return "contains TODO"
    if "fill in" in normalized or "replace me" in normalized or "changeme" in normalized:
        return "contains placeholder text"
    if "{files}" in normalized and not allow_files_placeholder:
        return "contains file-scoped placeholders"
    if "<" in normalized and ">" in normalized:
        return "contains angle-bracket placeholders"
    if normalized in {"tbd", "placeholder", "example-command"}:
        return "is a placeholder token"
    return ""


def _run_git_preflight(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )


def _git_preflight_payload() -> tuple[list[str], list[str], dict]:
    errors: list[str] = []
    warnings: list[str] = []
    git_info = {
        "available": False,
        "repo_root": "",
        "worktree_support": False,
        "dirty": False,
    }

    try:
        repo = _run_git_preflight(["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"Git is unavailable for autonomous execution: {exc}")
        return errors, warnings, git_info

    if repo.returncode != 0:
        detail = (repo.stderr or repo.stdout or "").strip()
        suffix = f" ({detail})" if detail else ""
        errors.append(f"Git repository not available for autonomous worktree execution{suffix}.")
        return errors, warnings, git_info

    git_info["available"] = True
    git_info["repo_root"] = repo.stdout.strip()

    try:
        worktree = _run_git_preflight(["worktree", "list"])
    except subprocess.TimeoutExpired:
        errors.append("git worktree support is unavailable for autonomous execution (timeout).")
    else:
        if worktree.returncode != 0:
            detail = (worktree.stderr or worktree.stdout or "").strip()
            suffix = f" ({detail})" if detail else ""
            errors.append(f"git worktree support is unavailable for autonomous execution{suffix}.")
        else:
            git_info["worktree_support"] = True

    try:
        status = _run_git_preflight(["status", "--porcelain"])
    except subprocess.TimeoutExpired:
        warnings.append("Could not inspect git working tree state (timeout).")
    else:
        if status.returncode != 0:
            detail = (status.stderr or status.stdout or "").strip()
            suffix = f" ({detail})" if detail else ""
            warnings.append(f"Could not inspect git working tree state{suffix}.")
        elif status.stdout.strip():
            git_info["dirty"] = True
            warnings.append("Git working tree is dirty; autonomous merge may need extra review.")

    return errors, warnings, git_info


def _preflight_safe_fix() -> list[str]:
    """Apply safe, non-destructive fixes for common preflight failures.

    Returns a list of actions taken (empty if nothing was done).
    Only creates files that do not already exist.
    """
    actions: list[str] = []

    # Copy planning-contract.md to .codex/skills/ if source exists and dest does not
    source_contract = ROOT / "planning-contract.md"
    dest_contract = _contract_path_for()
    if source_contract.exists() and not dest_contract.exists():
        dest_contract.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source_contract), str(dest_contract))
        actions.append(f"Copied planning-contract.md to {_relative_path(dest_contract)}")

    # Create AGENTS.md stub if conventions file is the default and doesn't exist
    conventions_value = str(CONVENTIONS_FILE or "").strip()
    if conventions_value == "AGENTS.md":
        conventions_path = ROOT / conventions_value
        if not conventions_path.exists():
            _atomic_write(conventions_path, "# Project Conventions\n\nAdd project-specific conventions here.\n")
            actions.append(f"Created conventions stub: {conventions_value}")

    return actions


def _plan_preflight_payload() -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    config_path = _config_path_for()
    contract_path = _contract_path_for()
    live_cfg = _runtime_load_config(config_path) if config_path.exists() else {}
    effective_cfg = copy.deepcopy(_CFG)
    for section, value in live_cfg.items():
        if isinstance(value, dict) and isinstance(effective_cfg.get(section), dict):
            merged_section = dict(effective_cfg.get(section, {}))
            merged_section.update(value)
            effective_cfg[section] = merged_section
        else:
            effective_cfg[section] = value
    detected = _detect_project_type(ROOT)
    detected_language = str(detected.get("language", "unknown") or "unknown")
    language_labels = {
        "python": "Python",
        "node": "Node.js",
        "rust": "Rust",
        "go": "Go",
        "dotnet": ".NET",
        "cpp": "C++",
        "unknown": "Unknown",
    }
    detected_label = language_labels.get(detected_language, detected_language or "Unknown")
    requires_build_command = detected_language in {"dotnet", "cpp", "rust", "go"}

    if not config_path.exists():
        errors.append("Missing .codex/skills/project.toml. Run init --force first.")
    if not contract_path.exists():
        errors.append("Missing .codex/skills/planning-contract.md in the installed runtime.")

    project_cfg = effective_cfg.get("project", {}) if isinstance(effective_cfg.get("project"), dict) else {}
    conventions_value = str(project_cfg.get("conventions", CONVENTIONS_FILE) or "").strip()
    conventions_path = ""
    if not conventions_value:
        errors.append("Config is missing [project].conventions.")
    else:
        try:
            resolved_conventions = _safe_resolve(conventions_value)
            conventions_path = _relative_path(resolved_conventions)
            if not resolved_conventions.exists():
                errors.append(f"Configured conventions file not found: {conventions_value}")
        except TaskManagerError as exc:
            errors.append(str(exc))

    commands = effective_cfg.get("commands", {}) if isinstance(effective_cfg.get("commands"), dict) else {}
    test_command = str(commands.get("test", "")).strip()
    compile_command = str(commands.get("compile", "")).strip()
    build_command = str(commands.get("build", "")).strip()

    if not test_command:
        errors.append("Config is missing [commands].test; autonomous verify cannot run.")
    else:
        test_placeholder = _placeholder_command_reason(test_command)
        if test_placeholder:
            errors.append(f"Configured [commands].test looks like a placeholder ({test_placeholder}).")
    if not compile_command:
        if detected_language == "python":
            warnings.append("Detected Python project but [commands].compile is missing.")
    else:
        compile_placeholder = _placeholder_command_reason(compile_command, allow_files_placeholder=True)
        if compile_placeholder:
            warnings.append(f"Configured [commands].compile looks like a placeholder ({compile_placeholder}).")
    if not build_command:
        if requires_build_command:
            errors.append(
                f"Detected {detected_label} project but [commands].build is missing; autonomous verification should include a build step."
            )
        elif detected_language == "node" and str(detected.get("build", "") or "").strip():
            warnings.append("Detected Node.js build script but [commands].build is missing.")
    else:
        build_placeholder = _placeholder_command_reason(build_command)
        if build_placeholder:
            message = f"Configured [commands].build looks like a placeholder ({build_placeholder})."
            if requires_build_command:
                errors.append(message)
            else:
                warnings.append(message)
    if TRACKER_FILE is None:
        warnings.append("Config has no [paths].tracker entry.")
    elif not TRACKER_FILE.exists():
        warnings.append(f"Configured tracker file not found: {_tracker_path_display()}")

    git_errors, git_warnings, git_info = _git_preflight_payload()
    errors.extend(git_errors)
    warnings.extend(git_warnings)

    return {
        "ready": not errors,
        "errors": errors,
        "warnings": warnings,
        "paths": {
            "config": _relative_path(config_path),
            "planning_contract": _relative_path(contract_path),
            "conventions": conventions_path or conventions_value,
            "state": _relative_path(STATE_FILE),
            "plans": _relative_path(PLANS_DIR),
            "specs": _relative_path(AGENTS_DIR),
            "tracker": _tracker_path_display(),
        },
        "commands": {
            "test": test_command,
            "compile": compile_command,
            "build": build_command,
        },
        "detected_project": {
            "name": str(detected.get("name", "") or ""),
            "language": detected_language,
            "display_language": detected_label,
        },
        "git": git_info,
    }


def _finalize_plan_updates(plan: dict, args) -> tuple[dict, list[str], list[str], list[str]]:
    plan = _default_plan_fields(plan)
    elements = plan["plan_elements"]
    updated_fields: list[str] = []

    goal = str(getattr(args, "goal", "") or "").strip()
    if goal:
        elements["goal_statement"] = goal
        updated_fields.append("goal_statement")
    elif not str(elements.get("goal_statement", "")).strip():
        elements["goal_statement"] = _default_goal_statement(plan)
        updated_fields.append("goal_statement(auto)")

    if _set_plan_list_element(
        elements,
        "exit_criteria",
        getattr(args, "exit_criterion", []),
        default_items=_default_plan_finalize_exit_criteria(plan),
    ):
        updated_fields.append("exit_criteria")
    if _set_plan_list_element(
        elements,
        "verification_strategy",
        getattr(args, "verification_step", []),
        default_items=_plan_default_verification_strategy(),
    ):
        updated_fields.append("verification_strategy")
    if _set_plan_list_element(
        elements,
        "documentation_updates",
        getattr(args, "documentation_update", []),
        default_items=["No documentation updates required."],
    ):
        updated_fields.append("documentation_updates")

    _backfill_plan_optional_elements(plan)

    agents = plan.get("agents", [])
    if agents:
        _complexity_to_model = {"low": "mini", "medium": "standard", "high": "max"}
        agent_dicts = [
            {
                "model": _complexity_to_model.get(str(a.get("complexity", "medium") or "medium").lower(), "standard"),
                "complexity": str(a.get("complexity", "medium") or "medium").lower(),
            }
            for a in agents
        ]
        elements["cost_estimate"] = _runtime_telemetry.estimate_campaign_savings(agent_dicts, use_tiered=True)
        updated_fields.append("cost_estimate")

    _refresh_plan_elements(plan)
    errors = _validate_plan(plan, strict=True)
    warnings = _plan_validation_warnings(plan)
    return plan, updated_fields, errors, warnings


def cmd_plan(args):
    """Plan subcommands: create, show, list, preflight, finalize, go, validate, approve, execute, reject, criteria, diff."""
    subcmd = getattr(args, "plan_command", None) or "show"
    dispatch = {
        "create": _plan_create,
        "show": _plan_show,
        "list": _plan_list,
        "preflight": cmd_plan_preflight,
        "finalize": cmd_plan_finalize,
        "go": cmd_plan_go,
        "validate": cmd_plan_validate,
        "approve": _plan_approve,
        "execute": _plan_execute,
        "reject": _plan_reject,
        "criteria": cmd_plan_criteria,
        "diff": cmd_plan_diff,
    }
    fn = dispatch.get(subcmd)
    if fn:
        fn(args)
    else:
        print(f"Unknown plan subcommand: {subcmd}")
        print("Available: create, show, list, preflight, finalize, go, validate, approve, execute, reject, criteria, diff")


def cmd_plan_preflight(args):
    fix_actions: list[str] = []
    if getattr(args, "fix_safe", False):
        fix_actions = _preflight_safe_fix()
    _runtime_plans.cmd_plan_preflight(
        args,
        plan_preflight_payload_fn=_plan_preflight_payload,
        emit_json_fn=_emit_json,
        fix_actions=fix_actions,
    )


def cmd_plan_finalize(args):
    _runtime_plans.cmd_plan_finalize(
        args,
        load_state_fn=load_state,
        resolve_plan_summary_fn=_resolve_plan_summary,
        load_plan_from_summary_fn=_load_plan_from_summary,
        finalize_plan_updates_fn=_finalize_plan_updates,
        persist_plan_artifacts_fn=_persist_plan_artifacts,
        upsert_plan_summary_fn=_upsert_plan_summary,
        save_state_fn=save_state,
        emit_json_fn=_emit_json,
        error_type=TaskManagerError,
    )


def cmd_plan_go(args):
    _runtime_plans.cmd_plan_go(
        args,
        plan_preflight_payload_fn=_plan_preflight_payload,
        load_state_fn=load_state,
        resolve_plan_summary_fn=_resolve_plan_summary,
        load_plan_from_summary_fn=_load_plan_from_summary,
        finalize_plan_updates_fn=_finalize_plan_updates,
        persist_plan_artifacts_fn=_persist_plan_artifacts,
        upsert_plan_summary_fn=_upsert_plan_summary,
        save_state_fn=save_state,
        plan_approve_fn=_plan_approve,
        plan_execute_fn=_plan_execute,
        emit_json_fn=_emit_json,
        error_type=TaskManagerError,
    )


def cmd_plan_validate(args):
    _runtime_plans.cmd_plan_validate(
        args,
        load_state_fn=load_state,
        resolve_plan_summary_fn=_resolve_plan_summary,
        load_plan_from_summary_fn=_load_plan_from_summary,
        validate_plan_fn=_validate_plan,
        plan_validation_warnings_fn=_plan_validation_warnings,
        emit_json_fn=_emit_json,
    )


def cmd_plan_criteria(args):
    _runtime_plans.cmd_plan_criteria(
        args,
        load_state_fn=load_state,
        resolve_plan_summary_fn=_resolve_plan_summary,
        load_plan_from_summary_fn=_load_plan_from_summary,
        resolve_plan_for_verify_fn=_resolve_plan_for_verify,
        explain_verify_resolution_failure_fn=_explain_verify_resolution_failure,
        plan_exit_criteria_fn=_plan_exit_criteria,
        emit_json_fn=_emit_json,
        error_type=TaskManagerError,
    )


def _plan_create(args):
    _runtime_plans.cmd_plan_create(
        args,
        sync_state_fn=sync_state,
        next_plan_id_fn=_next_plan_id,
        next_agent_letter_fn=_next_agent_letter,
        analyze_project_fn=analyze_project,
        now_iso_fn=_now_iso,
        slugify_fn=_slugify,
        empty_plan_elements_factory=_empty_plan_elements,
        persist_plan_artifacts_fn=_persist_plan_artifacts,
        upsert_plan_summary_fn=_upsert_plan_summary,
        save_state_fn=save_state,
        emit_json_fn=_emit_json,
    )


def _plan_show(args):
    _runtime_plans.cmd_plan_show(
        args,
        load_state_fn=load_state,
        resolve_plan_summary_fn=_resolve_plan_summary,
        load_plan_from_summary_fn=_load_plan_from_summary,
        emit_json_fn=_emit_json,
        print_plan_fn=_print_plan,
    )


def _plan_list(args):
    _runtime_plans.cmd_plan_list(
        args,
        load_state_fn=load_state,
        emit_json_fn=_emit_json,
    )


def _print_plan(plan: dict):
    """Pretty-print a plan."""
    SYM = {"draft": "\u270e", "approved": "\u2713", "rejected": "\u2717", "executed": "\u25ba", "partial": "\u25cb"}

    sym = SYM.get(plan["status"], "?")
    print(f"\n  {sym} {plan['id']} \u2014 {plan.get('description', '')}")
    print(f"  Status: {plan['status']}  |  Created: {plan['created_at'][:19]}")
    if plan.get("plan_file"):
        print(f"  Plan file: {plan['plan_file']}")
    if plan.get("plan_doc"):
        print(f"  Plan doc:  {plan['plan_doc']}")
    planning_context = _plan_planning_context(plan)
    analysis_health = planning_context.get("analysis_health", {})
    if analysis_health:
        providers = ", ".join(analysis_health.get("applied_providers", [])) or "none"
        print(f"  Analysis: {analysis_health.get('confidence', 'unknown')}  |  Providers: {providers}")
        warnings = _normalize_string_list(analysis_health.get("warnings", []))
        if warnings:
            print(f"  Analysis warning: {warnings[0]}")
    priority = planning_context.get("priority_projects", {})
    startup_projects = _normalize_string_list(priority.get("startup", []))
    packaging_projects = _normalize_string_list(priority.get("packaging", []))
    if startup_projects or packaging_projects:
        labels: list[str] = []
        if startup_projects:
            labels.append(f"startup={', '.join(startup_projects)}")
        if packaging_projects:
            labels.append(f"packaging={', '.join(packaging_projects)}")
        print(f"  Priority surfaces: {' | '.join(labels)}")

    agents = plan.get("agents", [])
    if not agents:
        print("\n  No agents defined yet.")
    else:
        # Group agents
        by_group: dict[int, list[dict]] = {}
        for a in agents:
            by_group.setdefault(a.get("group", 0), []).append(a)

        for g in sorted(by_group):
            group_agents = by_group[g]
            deps_label = ""
            if g > 0:
                deps_label = f"  (depends on group {g - 1})"
            print(f"\n  Group {g}{deps_label}:")
            for a in group_agents:
                deps = f" \u2190 {','.join(d.upper() for d in a.get('deps', []))}" if a.get("deps") else ""
                cplx = f"  [{a['complexity']}]" if a.get("complexity") else ""
                print(f"    Agent {a['letter'].upper()} \u2014 {a['name']}{deps}{cplx}")
                print(f"      Scope: {a.get('scope', 'N/A')[:68]}")
                print(f"      Files: {', '.join(a.get('files', [])) or 'N/A'}")

    conflicts = plan.get("conflicts", [])
    if conflicts:
        print("\n  Conflicts:")
        for c in conflicts:
            print(f"    \u26a0 {c}")

    steps = plan.get("integration_steps", [])
    if steps:
        print("\n  Integration steps:")
        for i, s in enumerate(steps, 1):
            print(f"    {i}. {s}")

    print()


def _plan_approve(args):
    _runtime_plans.cmd_plan_approve(
        args,
        load_state_fn=load_state,
        resolve_plan_summary_fn=_resolve_plan_summary,
        load_plan_from_summary_fn=_load_plan_from_summary,
        validate_plan_fn=_validate_plan,
        now_iso_fn=_now_iso,
        persist_plan_artifacts_fn=_persist_plan_artifacts,
        upsert_plan_summary_fn=_upsert_plan_summary,
        save_state_fn=save_state,
        error_type=TaskManagerError,
    )


def _plan_reject(args):
    _runtime_plans.cmd_plan_reject(
        args,
        load_state_fn=load_state,
        resolve_plan_summary_fn=_resolve_plan_summary,
        load_plan_from_summary_fn=_load_plan_from_summary,
        persist_plan_artifacts_fn=_persist_plan_artifacts,
        upsert_plan_summary_fn=_upsert_plan_summary,
        save_state_fn=save_state,
    )


def _plan_execute(args):
    _runtime_plans.cmd_plan_execute(
        args,
        load_state_fn=load_state,
        resolve_plan_summary_fn=_resolve_plan_summary,
        load_plan_from_summary_fn=_load_plan_from_summary,
        validate_plan_fn=_validate_plan,
        new_task_factory=_new_task_record,
        agents_dir=AGENTS_DIR,
        write_spec_template_fn=_write_spec_template,
        assign_groups_fn=_assign_groups,
        recompute_ready_fn=_recompute_ready,
        now_iso_fn=_now_iso,
        refresh_plan_elements_fn=_refresh_plan_elements,
        persist_plan_artifacts_fn=_persist_plan_artifacts,
        upsert_plan_summary_fn=_upsert_plan_summary,
        save_state_fn=save_state,
        sym_map=_SYM,
        error_type=TaskManagerError,
    )


def _resolve_plan_summary(plans: list[dict], plan_id: str | None) -> dict | None:
    return _runtime_plans.resolve_plan_summary(plans, plan_id)


def _write_spec_template(spec_path: Path, agent: dict):
    """Write an agent spec template from plan data."""
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(
        spec_path,
        _render_spec_template(
            agent["letter"],
            agent["name"],
            agent.get("scope", f"Implement the assigned scope for Agent {str(agent.get('letter', '?')).upper()}."),
            deps=list(agent.get("deps", [])),
            files=list(agent.get("files", [])),
            plan=agent.get("_plan"),
        ),
    )


def cmd_plan_add_agent(args):
    _runtime_plans.cmd_plan_add_agent(
        args,
        load_state_fn=load_state,
        resolve_plan_summary_fn=_resolve_plan_summary,
        load_plan_from_summary_fn=_load_plan_from_summary,
        validate_agent_id_fn=_validate_agent_id,
        default_plan_fields_fn=_default_plan_fields,
        plan_assign_groups_fn=_plan_assign_groups,
        validate_plan_fn=_validate_plan,
        next_agent_letter_fn=_next_agent_letter,
        persist_plan_artifacts_fn=_persist_plan_artifacts,
        upsert_plan_summary_fn=_upsert_plan_summary,
        save_state_fn=save_state,
        error_type=TaskManagerError,
    )


def _plan_assign_groups(plan: dict, allow_missing: bool = False):
    """Assign groups to plan agents based on dependency depth."""
    agents = plan.get("agents", [])
    by_letter = {a["letter"]: a for a in agents}
    deps_map = {letter: list(agent.get("deps", [])) for letter, agent in by_letter.items()}
    if allow_missing:
        deps_map = {letter: [dep for dep in deps if dep in by_letter] for letter, deps in deps_map.items()}
    depths = _compute_dependency_depths(deps_map, f"plan {plan.get('id', '?')}")

    for agent in agents:
        agent["group"] = depths.get(agent["letter"], int(agent.get("group", 0) or 0))

    groups: dict[str, list[str]] = {}
    for a in agents:
        groups.setdefault(str(a["group"]), []).append(a["letter"])
    plan["groups"] = groups
    _refresh_plan_elements(plan)


# ---------------------------------------------------------------------------
# Plan diff
# ---------------------------------------------------------------------------


def _plan_diff(plan_id: str | None) -> dict:
    """Compare a plan's agents against current state tasks and return a diff."""
    summary = _resolve_plan_summary_for_runtime(plan_id)
    plan = _load_plan_from_summary(summary)
    state = load_state()

    plan_agents = {a["letter"]: a for a in plan.get("agents", [])}
    state_tasks = state.get("tasks", {})

    added = []
    for letter, agent in plan_agents.items():
        if letter not in state_tasks:
            added.append({"id": letter, "name": agent.get("name", "")})

    removed = []
    for task_id, task in state_tasks.items():
        if task_id not in plan_agents:
            removed.append({"id": task_id, "name": task.get("name", "")})

    changed = []
    for letter, agent in plan_agents.items():
        if letter not in state_tasks:
            continue
        task = state_tasks[letter]
        changes = []
        plan_deps = sorted(str(d) for d in agent.get("deps", []))
        task_deps = sorted(str(d) for d in task.get("deps", []))
        if plan_deps != task_deps:
            changes.append(f"deps: plan={plan_deps} state={task_deps}")
        plan_complexity = str(agent.get("complexity", "")).lower()
        task_complexity = str(task.get("complexity", "")).lower()
        if plan_complexity and task_complexity and plan_complexity != task_complexity:
            changes.append(f"complexity: plan={plan_complexity} state={task_complexity}")
        plan_files = sorted(str(f) for f in agent.get("files", []))
        task_files = sorted(str(f) for f in task.get("files", []))
        if plan_files != task_files:
            changes.append(f"files: plan={plan_files} state={task_files}")
        if changes:
            changed.append({"id": letter, "changes": changes})

    summary_str = f"{len(added)} added, {len(removed)} removed, {len(changed)} changed"
    return {
        "plan_id": plan.get("id", plan_id or ""),
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": summary_str,
    }


def cmd_plan_diff(args):
    payload = _plan_diff(getattr(args, "plan_id", None))
    if getattr(args, "json", False):
        _emit_json(payload)
        return
    print(f"Plan diff for {payload['plan_id']}: {payload['summary']}")
    for entry in payload["added"]:
        print(f"  + {entry['id']} ({entry['name']})")
    for entry in payload["removed"]:
        print(f"  - {entry['id']} ({entry['name']})")
    for entry in payload["changed"]:
        print(f"  ~ {entry['id']}: {', '.join(entry['changes'])}")


# ---------------------------------------------------------------------------
# Init — bootstrap a new project
# ---------------------------------------------------------------------------


def _detect_project_type(root: Path) -> dict:
    return _runtime_bootstrap.detect_project_type(root)


def _build_init_config(detected: dict) -> str:
    rendered, _used_template = _runtime_bootstrap.build_init_config(
        ROOT,
        detected,
        template_path=_template_path_for(),
        conventions_path="AGENTS.md",
    )
    return rendered


def _resolve_plan_summary_for_runtime(plan_id: str | None = None) -> dict:
    state = load_state()
    plans = state.get("plans", [])
    if not plans:
        raise TaskManagerError('No plans available. Create one with: python scripts/task_manager.py plan create "description"')
    if plan_id:
        for summary in plans:
            if summary.get("id") == plan_id:
                return summary
        raise TaskManagerError(f"Plan {plan_id} not found.")
    return plans[-1]


def _capture_json_command(invoker) -> dict:
    buf = io.StringIO()
    with redirect_stdout(buf):
        invoker()
    text = buf.getvalue().strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TaskManagerError(f"Command did not emit valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TaskManagerError("Expected JSON object output from runtime command.")
    return payload


def _load_json_payload(args) -> dict:
    raw = str(getattr(args, "payload", "") or "").strip()
    payload_file = str(getattr(args, "payload_file", "") or "").strip()
    inline_payload = str(getattr(args, "payload_json", "") or "").strip()
    if not raw and inline_payload:
        raw = inline_payload
    if not raw and payload_file:
        try:
            raw = Path(payload_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise TaskManagerError(f"Cannot read payload file {payload_file}: {exc}") from exc
    if not raw:
        if sys.stdin.isatty():
            raise TaskManagerError("No payload provided. Pass --payload or --payload-file, or pipe JSON to stdin.")
        raw = sys.stdin.read()
    if not raw.strip():
        raise TaskManagerError("Provide JSON via positional payload, --payload, --payload-file, or stdin.")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskManagerError(f"Invalid JSON payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise TaskManagerError("Payload must be a JSON object.")
    return payload


def _resolve_recorded_path(path_text: str) -> Path:
    return _runtime_merge.resolve_recorded_path(path_text, root=ROOT)


def _display_runtime_path(path: Path) -> str:
    return _runtime_merge.display_runtime_path(path, relative_path_fn=_relative_path)


def _command_payload_entry(label: str, command: str, result: subprocess.CompletedProcess[str]) -> dict:
    return _runtime_commands.command_payload_entry(label, command, result)


_RUNTIME_COMMAND_TIMEOUT = _runtime_commands._RUNTIME_COMMAND_TIMEOUT

_DEFAULT_COMMAND_TIMEOUTS: dict[str, int] = dict(_runtime_commands._DEFAULT_COMMAND_TIMEOUTS)


def _resolve_command_timeout(label: str) -> int:
    return _runtime_commands.resolve_command_timeout(label, cfg=_CFG)


def _run_runtime_command(label: str, command: str) -> dict:
    return _runtime_commands.run_runtime_command(label, command, root=ROOT, cfg=_CFG)


def _candidate_worktree_roots(state: dict) -> set[Path]:
    return _runtime_merge.candidate_worktree_roots(
        state,
        root=ROOT,
        resolve_recorded_path_fn=_resolve_recorded_path,
    )


def _run_git_runtime(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return _runtime_merge.run_git_runtime(args, root=ROOT, timeout=timeout)


def _git_worktree_inventory() -> dict:
    return _runtime_merge.git_worktree_inventory(root=ROOT)


def _match_worktree_record(recorded_path: str, recorded_branch: str, inventory: dict) -> dict | None:
    return _runtime_merge.match_worktree_record(
        recorded_path,
        recorded_branch,
        inventory,
        root=ROOT,
    )


def _cleanup_task_worktree(path_text: str, branch_text: str, inventory: dict) -> dict:
    return _runtime_merge.cleanup_task_worktree(
        path_text,
        branch_text,
        inventory,
        root=ROOT,
        load_state_fn=load_state,
        resolve_recorded_path_fn=_resolve_recorded_path,
        candidate_worktree_roots_fn=_candidate_worktree_roots,
        match_worktree_record_fn=_match_worktree_record,
        git_worktree_inventory_fn=_git_worktree_inventory,
        run_git_runtime_fn=_run_git_runtime,
    )


def cmd_attach(args):
    _runtime_result.cmd_attach(
        args,
        load_state_fn=load_state,
        save_state_fn=save_state,
        ensure_task_runtime_fields_fn=_ensure_task_runtime_fields,
        resolve_recorded_path_fn=_resolve_recorded_path,
        display_runtime_path_fn=_display_runtime_path,
        now_iso_fn=_now_iso,
        emit_json_fn=_emit_json,
        error_type=TaskManagerError,
    )


def cmd_result(args):
    _runtime_result.cmd_result(
        args,
        load_state_fn=load_state,
        save_state_fn=save_state,
        ensure_task_runtime_fields_fn=_ensure_task_runtime_fields,
        empty_agent_result_fn=_empty_agent_result,
        empty_merge_record_fn=_empty_merge_record,
        normalize_string_list_fn=_normalize_string_list,
        recompute_ready_fn=_recompute_ready,
        load_json_payload_fn=_load_json_payload,
        now_iso_fn=_now_iso,
        emit_json_fn=_emit_json,
        error_type=TaskManagerError,
    )


def _recover_runtime(*, prune_orphans: bool = False) -> dict:
    return _runtime_orchestration.recover_runtime(
        prune_orphans=prune_orphans,
        load_state_fn=load_state,
        save_state_fn=save_state,
        now_iso_fn=_now_iso,
        ensure_task_runtime_fields_fn=_ensure_task_runtime_fields,
        empty_agent_result_fn=_empty_agent_result,
        empty_launch_record_fn=_empty_launch_record,
        empty_merge_record_fn=_empty_merge_record,
        recompute_ready_fn=_recompute_ready,
        sync_execution_manifest_after_recover_fn=_sync_execution_manifest_after_recover,
        resolve_recorded_path_fn=_resolve_recorded_path,
        display_runtime_path_fn=_display_runtime_path,
        candidate_worktree_roots_fn=_candidate_worktree_roots,
        match_worktree_record_fn=_match_worktree_record,
        cleanup_task_worktree_fn=_cleanup_task_worktree,
        git_worktree_inventory_fn=_git_worktree_inventory,
        safe_resolve_fn=_safe_resolve,
        error_type=TaskManagerError,
    )


def cmd_recover(args):
    _runtime_orchestration.cmd_recover(
        args,
        recover_runtime_fn=_recover_runtime,
        emit_json_fn=_emit_json,
    )


def _merge_runtime(agent_ids: list[str] | None = None, *, plan_id: str | None = None) -> dict:
    return _runtime_merge.merge_runtime(
        agent_ids,
        plan_id=plan_id,
        load_state_fn=load_state,
        save_state_fn=save_state,
        now_iso_fn=_now_iso,
        ensure_task_runtime_fields_fn=_ensure_task_runtime_fields,
        normalize_string_list_fn=_normalize_string_list,
        safe_resolve_fn=_safe_resolve,
        persist_execution_manifest_fn=_persist_execution_manifest,
        resolve_recorded_path_fn=_resolve_recorded_path,
        display_runtime_path_fn=_display_runtime_path,
        match_worktree_record_fn=_match_worktree_record,
        cleanup_task_worktree_fn=_cleanup_task_worktree,
        git_worktree_inventory_fn=_git_worktree_inventory,
        run_git_runtime_fn=_run_git_runtime,
    )


def cmd_merge(args):
    _runtime_merge.cmd_merge(
        args,
        merge_runtime_fn=_merge_runtime,
        emit_json_fn=_emit_json,
    )


def _verify_runtime(plan_id: str | None = None, *, profile: str = "default") -> dict:
    return _runtime_verify.verify_runtime(
        plan_id,
        profile=profile,
        recover_runtime_fn=_recover_runtime,
        sync_state_fn=sync_state,
        resolve_plan_summary_for_runtime_fn=_resolve_plan_summary_for_runtime,
        load_plan_from_summary_fn=_load_plan_from_summary,
        resolve_plan_for_verify_fn=_resolve_plan_for_verify,
        explain_verify_resolution_failure_fn=_explain_verify_resolution_failure,
        normalize_verify_profile_fn=_normalize_verify_profile,
        plan_owned_files_fn=_plan_owned_files,
        commands_cfg_fn=_commands_cfg,
        configured_runtime_commands_fn=_configured_runtime_commands,
        placeholder_command_reason_fn=_placeholder_command_reason,
        run_runtime_command_fn=_run_runtime_command,
        plan_exit_criteria_fn=_plan_exit_criteria,
        persist_execution_manifest_fn=_persist_execution_manifest,
        save_state_fn=save_state,
        now_iso_fn=_now_iso,
        error_type=TaskManagerError,
    )


def cmd_verify(args):
    _runtime_verify.cmd_verify(
        args,
        verify_runtime_fn=_verify_runtime,
        emit_json_fn=_emit_json,
    )


def _go_runtime(args) -> dict:
    return _runtime_orchestration.go_runtime(
        args,
        resolve_plan_summary_for_runtime_fn=_resolve_plan_summary_for_runtime,
        load_plan_from_summary_fn=_load_plan_from_summary,
        capture_json_command_fn=_capture_json_command,
        cmd_plan_go_fn=cmd_plan_go,
        plan_preflight_payload_fn=_plan_preflight_payload,
        recover_runtime_fn=_recover_runtime,
        sync_state_fn=sync_state,
        load_state_fn=load_state,
        save_state_fn=save_state,
        ensure_execution_manifest_fn=_ensure_execution_manifest,
        persist_execution_manifest_fn=_persist_execution_manifest,
        cmd_run_fn=cmd_run,
        merge_runtime_fn=_merge_runtime,
        verify_runtime_fn=_verify_runtime,
    )


def cmd_go(args):
    _runtime_orchestration.cmd_go(
        args,
        go_runtime_fn=_go_runtime,
        emit_json_fn=_emit_json,
        sleep_fn=time.sleep,
    )


def cmd_init(args):
    result = _runtime_bootstrap.init_project(
        ROOT,
        force=getattr(args, "force", False),
        config_path=_config_path_for(),
        template_path=_template_path_for(),
        conventions_path="AGENTS.md",
        detect_project_type_fn=_detect_project_type,
        load_toml_file_fn=_load_toml_file,
        atomic_write_fn=_atomic_write,
        safe_resolve_fn=lambda path, root=ROOT: _safe_resolve(str(path), root),
        default_state_factory=_default_state,
    )
    _refresh_runtime_bindings()
    for line in _runtime_bootstrap.format_init_messages(result):
        print(line)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Campaign Task Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("init", help="Bootstrap campaign skills for a new project")
    p.add_argument("--force", action="store_true", help="Overwrite existing config")
    sub.add_parser("sync", help="Rebuild state from agents/ + tracker")
    p = sub.add_parser("status", help="Show all tasks with status")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("ready", help="List agents ready to launch")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("run", help="Mark running + emit launch specs (JSON)")
    p.add_argument("agents", help="Comma-separated letters, 'ready', or 'all'")
    p.add_argument("--json", action="store_true", help="Retained for compatibility; run already emits JSON")

    p = sub.add_parser("attach", help="Record worktree metadata for a running agent")
    p.add_argument("agent")
    p.add_argument("--worktree-path", required=True)
    p.add_argument("--branch", default="")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("result", help="Record structured agent result JSON")
    p.add_argument("agent")
    p.add_argument("payload_json", nargs="?")
    p.add_argument("--payload", default="")
    p.add_argument("--payload-file", default="")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("recover", help="Reset stale running tasks and report orphan worktrees")
    p.add_argument("--prune-orphans", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("merge", help="Merge completed task outputs into the main working tree")
    p.add_argument("agents", nargs="?", default="all")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("verify", help="Run post-merge verification")
    p.add_argument("plan_id", nargs="?")
    p.add_argument("--profile", choices=["default", "fast", "full"], default="default")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("go", help="Resume full lifecycle from the latest or specified plan")
    p.add_argument("plan_id", nargs="?")
    p.add_argument("--goal", default="")
    p.add_argument("--exit-criterion", action="append", default=[])
    p.add_argument("--verification-step", action="append", default=[])
    p.add_argument("--documentation-update", action="append", default=[])
    p.add_argument("--json", action="store_true")
    p.add_argument("--poll", type=int, default=0, metavar="SECONDS",
                   help="Poll interval in seconds; repeat until terminal state")

    p = sub.add_parser("complete", help="Mark agent as done")
    p.add_argument("agent")
    p.add_argument("-s", "--summary", default="")

    p = sub.add_parser("fail", help="Mark agent as failed")
    p.add_argument("agent")
    p.add_argument("-r", "--reason", default="")

    p = sub.add_parser("reset", help="Reset agent to pending/ready")
    p.add_argument("agent")

    sub.add_parser("graph", help="ASCII dependency graph")
    sub.add_parser("next", help="What to do next")

    p = sub.add_parser("add", help="Register a new task")
    p.add_argument("letter")
    p.add_argument("name")
    p.add_argument("--scope", default="")
    p.add_argument("--deps", default="")
    p.add_argument("--files", default="")
    p.add_argument("--complexity", default="low", choices=["low", "medium", "high"])

    p = sub.add_parser("new", help="Auto-allocate a letter, register a task, and create a template")
    p.add_argument("name")
    p.add_argument("scope", nargs="?", default="")
    p.add_argument("--deps", default="")
    p.add_argument("--files", default="")
    p.add_argument("--complexity", default="low", choices=["low", "medium", "high"])
    p.add_argument("--no-template", action="store_true")

    p = sub.add_parser("template", help="Generate agent spec file")
    p.add_argument("letter")
    p.add_argument("name")
    p.add_argument("--scope", default="")

    # --- Analyze ---
    p = sub.add_parser("analyze", help="Scan project and output structured map")
    p.add_argument("--json", action="store_true", help="Output as JSON")

    # --- Plan subcommands ---
    p = sub.add_parser(
        "plan", help="Plan workflow: create, show, list, preflight, finalize, go, validate, approve, execute, reject, criteria"
    )
    plan_sub = p.add_subparsers(dest="plan_command")

    pc = plan_sub.add_parser("create", help="Create a new draft plan")
    pc.add_argument("description", nargs="?", default="")
    pc.add_argument("--json", action="store_true")
    pc.add_argument("--planner-kind", default="planner", choices=["planner", "planner-refactor", "manager-go"])
    pc.add_argument("--discovery-doc", action="append", default=[])
    pc.add_argument("--roadmap", default="")
    pc.add_argument("--phase", default="")
    pc.add_argument("--behavioral-invariant", action="append", default=[])
    pc.add_argument("--rollback-strategy", default="")

    ps = plan_sub.add_parser("show", help="Show a plan (latest if no ID)")
    ps.add_argument("plan_id", nargs="?")
    ps.add_argument("--json", action="store_true")

    pl = plan_sub.add_parser("list", help="List all plans")
    pl.add_argument("--json", action="store_true")

    ppf = plan_sub.add_parser("preflight", help="Check autonomous execution prerequisites")
    ppf.add_argument("--json", action="store_true")
    ppf.add_argument("--fix-safe", action="store_true", dest="fix_safe", help="Apply safe non-destructive fixes")

    pfin = plan_sub.add_parser("finalize", help="Fill required plan elements before approval")
    pfin.add_argument("plan_id", nargs="?")
    pfin.add_argument("--goal", default="")
    pfin.add_argument("--exit-criterion", action="append", default=[])
    pfin.add_argument("--verification-step", action="append", default=[])
    pfin.add_argument("--documentation-update", action="append", default=[])
    pfin.add_argument("--json", action="store_true")

    pgo = plan_sub.add_parser("go", help="Preflight + finalize + approve + execute a draft plan")
    pgo.add_argument("plan_id", nargs="?")
    pgo.add_argument("--goal", default="")
    pgo.add_argument("--exit-criterion", action="append", default=[])
    pgo.add_argument("--verification-step", action="append", default=[])
    pgo.add_argument("--documentation-update", action="append", default=[])
    pgo.add_argument("--json", action="store_true")

    pv = plan_sub.add_parser("validate", help="Validate a plan without mutating it")
    pv.add_argument("plan_id", nargs="?")
    pv.add_argument("--json", action="store_true")

    pa = plan_sub.add_parser("approve", help="Approve a plan")
    pa.add_argument("plan_id", nargs="?")

    pe = plan_sub.add_parser("execute", help="Execute: register agents + generate templates")
    pe.add_argument("plan_id", nargs="?")

    pr = plan_sub.add_parser("reject", help="Reject a plan")
    pr.add_argument("plan_id", nargs="?")

    pcr = plan_sub.add_parser("criteria", help="Show canonical exit criteria for a plan")
    pcr.add_argument("plan_id", nargs="?")
    pcr.add_argument("--json", action="store_true")

    diff_parser = plan_sub.add_parser("diff", help="Show changes between plan and current state")
    diff_parser.add_argument("plan_id", nargs="?", help="Plan ID")
    diff_parser.add_argument("--json", action="store_true")
    diff_parser.set_defaults(func=cmd_plan_diff)

    # --- Plan-add-agent (top-level for convenience) ---
    p = sub.add_parser("plan-add-agent", help="Add an agent to a draft plan")
    p.add_argument("plan_id")
    p.add_argument("letter")
    p.add_argument("name")
    p.add_argument("--scope", default="")
    p.add_argument("--deps", default="")
    p.add_argument("--files", default="")
    p.add_argument("--group", default="")
    p.add_argument("--complexity", default="low", choices=["low", "medium", "high"])

    args = parser.parse_args()
    if not args.command:
        args = parser.parse_args(["status"])

    dispatch = {
        "init": cmd_init,
        "sync": cmd_sync,
        "status": cmd_status,
        "ready": cmd_ready,
        "run": cmd_run,
        "attach": cmd_attach,
        "result": cmd_result,
        "recover": cmd_recover,
        "merge": cmd_merge,
        "verify": cmd_verify,
        "go": cmd_go,
        "complete": cmd_complete,
        "fail": cmd_fail,
        "reset": cmd_reset,
        "graph": cmd_graph,
        "next": cmd_next,
        "add": cmd_add,
        "new": cmd_new,
        "template": cmd_template,
        "analyze": cmd_analyze,
        "plan": cmd_plan,
        "plan-add-agent": cmd_plan_add_agent,
    }

    fn = dispatch.get(args.command)
    if fn:
        try:
            fn(args)
        except TaskManagerError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            print(f"Unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
            sys.exit(2)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
