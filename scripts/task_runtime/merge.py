from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

from .state import coerce_int

DEFAULT_WORKTREE_ROOT_CANDIDATES = (
    Path(".worktrees"),
    Path(".codex/worktrees"),
    Path(".claude/worktrees"),
    Path("worktrees"),
)


def resolve_recorded_path(path_text: str, *, root: Path) -> Path:
    candidate = Path(str(path_text or "").strip())
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def display_runtime_path(path: Path, *, relative_path_fn: Callable[[Path], str]) -> str:
    try:
        return relative_path_fn(path)
    except (ValueError, OSError):
        return str(path)


def candidate_worktree_roots(
    state: dict,
    *,
    root: Path,
    resolve_recorded_path_fn: Callable[[str], Path],
) -> set[Path]:
    roots: set[Path] = set()
    # Prefer the provider-neutral Codex default, but keep legacy roots visible
    # during recovery so older installed runtimes can still be reconciled.
    for relative in DEFAULT_WORKTREE_ROOT_CANDIDATES:
        candidate = (root / relative).resolve()
        if candidate.exists():
            roots.add(candidate)
    for task in state.get("tasks", {}).values():
        for path_text in (
            str(task.get("launch", {}).get("worktree_path", "") or ""),
            str(task.get("agent_result", {}).get("worktree_path", "") or ""),
        ):
            if not path_text:
                continue
            try:
                resolved = resolve_recorded_path_fn(path_text)
            except OSError:
                continue
            roots.add(resolved.parent)
    return {r for r in roots if r.exists()}


def run_git_runtime(args: list[str], *, root: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def git_worktree_inventory(*, root: Path) -> dict:
    try:
        result = run_git_runtime(["worktree", "list", "--porcelain"], root=root)
    except OSError as exc:
        return {"available": False, "error": str(exc), "worktrees": []}

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {"available": False, "error": detail, "worktrees": []}

    worktrees: list[dict] = []
    current: dict[str, object] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = str(Path(value).resolve())
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["detached"] = True
        elif key == "bare":
            current["bare"] = True
        elif key == "prunable":
            current["prunable"] = value
        elif key == "locked":
            current["locked"] = value
    if current:
        worktrees.append(current)

    return {"available": True, "error": "", "worktrees": worktrees}


def match_worktree_record(
    recorded_path: str,
    recorded_branch: str,
    inventory: dict,
    *,
    root: Path,
) -> dict | None:
    if not inventory.get("available"):
        return None
    branch = str(recorded_branch or "").strip()
    path = str(recorded_path or "").strip()
    normalized_path = ""
    if path:
        try:
            normalized_path = str(resolve_recorded_path(path, root=root))
        except OSError:
            normalized_path = str((root / path).resolve())

    for item in inventory.get("worktrees", []):
        item_path = str(item.get("path", "") or "")
        item_branch = str(item.get("branch", "") or "")
        if normalized_path and item_path == normalized_path:
            return item
        if branch and item_branch == branch:
            return item
    return None


def cleanup_task_worktree(
    path_text: str,
    branch_text: str,
    inventory: dict,
    *,
    root: Path,
    load_state_fn: Callable[[], dict],
    resolve_recorded_path_fn: Callable[[str], Path],
    candidate_worktree_roots_fn: Callable[[dict], set[Path]],
    match_worktree_record_fn: Callable[[str, str, dict], dict | None],
    git_worktree_inventory_fn: Callable[[], dict],
    run_git_runtime_fn: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> dict:
    warnings: list[str] = []
    result: dict[str, object] = {
        "worktree_removed": False,
        "branch_removed": False,
        "warnings": warnings,
    }
    path_text = str(path_text or "").strip()
    branch_text = str(branch_text or "").strip()
    if not path_text and not branch_text:
        return result

    resolved_path: Path | None = None
    if path_text:
        try:
            resolved_path = resolve_recorded_path_fn(path_text)
        except OSError:
            resolved_path = (root / path_text).resolve()

    matched = match_worktree_record_fn(path_text, branch_text, inventory)
    if matched and resolved_path and str(matched.get("path", "")) != str(root.resolve()):
        remove_result = run_git_runtime_fn(["worktree", "remove", str(resolved_path), "--force"])
        if remove_result.returncode == 0:
            result["worktree_removed"] = True
            inventory.update(git_worktree_inventory_fn())
        else:
            detail = (remove_result.stderr or remove_result.stdout or "").strip()
            warnings.append(f"git worktree remove failed for {path_text}: {detail or 'unknown error'}")
    elif resolved_path and resolved_path.exists():
        allowed_roots = candidate_worktree_roots_fn(load_state_fn())
        if any(r == resolved_path or r in resolved_path.parents for r in allowed_roots):
            shutil.rmtree(resolved_path, ignore_errors=True)
            result["worktree_removed"] = True

    if branch_text and inventory.get("available"):
        refreshed = inventory if result["worktree_removed"] else git_worktree_inventory_fn()
        branch_still_attached = any(str(item.get("branch", "") or "") == branch_text for item in refreshed.get("worktrees", []))
        if not branch_still_attached:
            delete_result = run_git_runtime_fn(["branch", "-D", branch_text])
            if delete_result.returncode == 0:
                result["branch_removed"] = True
            else:
                detail = (delete_result.stderr or delete_result.stdout or "").strip()
                if detail:
                    warnings.append(f"git branch -D {branch_text} failed: {detail}")
    return result


def merge_runtime(
    agent_ids: list[str] | None = None,
    *,
    plan_id: str | None = None,
    load_state_fn: Callable[[], dict],
    save_state_fn: Callable[[dict], None],
    now_iso_fn: Callable[[], str],
    ensure_task_runtime_fields_fn: Callable[[dict], bool],
    normalize_string_list_fn: Callable[[object], list[str]],
    safe_resolve_fn: Callable[[str], Path],
    persist_execution_manifest_fn: Callable[..., None],
    resolve_recorded_path_fn: Callable[[str], Path],
    display_runtime_path_fn: Callable[[Path], str],
    match_worktree_record_fn: Callable[[str, str, dict], dict | None],
    cleanup_task_worktree_fn: Callable[[str, str, dict], dict],
    git_worktree_inventory_fn: Callable[[], dict],
    run_git_runtime_fn: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> dict:
    state = load_state_fn()
    inventory = git_worktree_inventory_fn()
    selected = {item.lower() for item in (agent_ids or []) if str(item or "").strip()}
    candidates = [
        task for task in state.get("tasks", {}).values() if task.get("status") == "done" and (not selected or task.get("id") in selected)
    ]
    candidates.sort(key=lambda item: (coerce_int(item.get("group", 0) or 0), item.get("id", "")))

    owners: dict[str, dict] = {}
    merged: list[dict] = []
    skipped: list[dict] = []
    conflicts: list[dict] = []
    cleanup: list[dict] = []
    now = now_iso_fn()

    backup_method = "none"
    if inventory.get("available"):
        try:
            stash_result = run_git_runtime_fn(["stash", "push", "-m", f"pre-merge-backup-{plan_id or 'manual'}"])
            if stash_result.returncode == 0:
                backup_method = "git_stash"
        except (OSError, subprocess.CalledProcessError):
            pass
    else:
        backup_method = "file_copy"

    for task in candidates:
        ensure_task_runtime_fields_fn(task)
        existing_merge = task.get("merge", {})
        if str(existing_merge.get("status", "") or "") in {"merged", "noop"} and str(existing_merge.get("merged_at", "") or ""):
            skipped.append({"id": task["id"], "reason": "already_merged", "status": existing_merge.get("status", "")})
            continue

        task_result = task.get("agent_result", {})
        if str(task_result.get("status", "") or "").lower() != "done":
            skipped.append({"id": task["id"], "reason": "no_done_result"})
            continue

        files = [item.replace("\\", "/") for item in normalize_string_list_fn(task_result.get("files_modified", []))]
        worktree_text = str(task_result.get("worktree_path", "") or task.get("launch", {}).get("worktree_path", "") or "")
        branch_text = str(task_result.get("branch", "") or task.get("launch", {}).get("branch", "") or "")
        if not files:
            task["merge"] = {
                "status": "noop",
                "applied_files": [],
                "conflicts": [],
                "merged_at": now,
                "detail": "No files were reported by the agent result.",
            }
            merged.append({"id": task["id"], "status": "noop", "applied_files": []})
            cleanup_entry = cleanup_task_worktree_fn(worktree_text, branch_text, inventory)
            cleanup.append({"id": task["id"], **cleanup_entry})
            continue

        if not worktree_text:
            task["merge"] = {
                "status": "conflict",
                "applied_files": [],
                "conflicts": files,
                "merged_at": now,
                "detail": "Missing recorded worktree path for merge.",
            }
            conflicts.append({"id": task["id"], "files": files, "reason": "missing_worktree"})
            continue

        matched = match_worktree_record_fn(worktree_text, branch_text, inventory)
        if matched:
            worktree_root = Path(str(matched.get("path", "")))
            if matched.get("branch"):
                branch_text = str(matched.get("branch", "") or "")
        else:
            worktree_root = resolve_recorded_path_fn(worktree_text)
        if not worktree_root.exists():
            task["merge"] = {
                "status": "conflict",
                "applied_files": [],
                "conflicts": files,
                "merged_at": now,
                "detail": f"Recorded worktree path not found: {worktree_text}",
            }
            conflicts.append({"id": task["id"], "files": files, "reason": "missing_worktree_path"})
            continue

        applied_files: list[str] = []
        task_conflicts: list[str] = []
        notes: list[str] = []
        group = coerce_int(task.get("group", 0) or 0)
        for rel_path in files:
            prior = owners.get(rel_path)
            if prior and prior["id"] != task["id"] and group <= prior["group"]:
                task_conflicts.append(rel_path)
                notes.append(f"{rel_path} also modified by Agent {prior['id'].upper()} in group {prior['group']}")
                continue
            source = (worktree_root / Path(rel_path)).resolve()
            try:
                source.relative_to(worktree_root.resolve())
            except ValueError:
                task_conflicts.append(rel_path)
                notes.append(f"{rel_path} escapes worktree root")
                continue
            if not source.exists() or not source.is_file():
                task_conflicts.append(rel_path)
                notes.append(f"{rel_path} missing from recorded worktree")
                continue
            dest = safe_resolve_fn(rel_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if backup_method == "file_copy" and dest.exists():
                try:
                    shutil.copy2(str(dest), str(dest) + ".bak")
                except OSError as exc:
                    notes.append(f"{rel_path} backup failed: {exc}")
            shutil.copy2(source, dest)
            applied_files.append(rel_path)
            owners[rel_path] = {"id": task["id"], "group": group}
            if prior and prior["id"] != task["id"] and group > prior["group"]:
                notes.append(f"{rel_path} superseded Agent {prior['id'].upper()} by dependency order")

        merge_status = "merged"
        if task_conflicts:
            merge_status = "conflict"
        elif not applied_files:
            merge_status = "noop"
        task["merge"] = {
            "status": merge_status,
            "applied_files": applied_files,
            "conflicts": task_conflicts,
            "merged_at": now,
            "detail": "; ".join(notes) if notes else "",
        }
        if task_conflicts:
            conflicts.append({"id": task["id"], "files": task_conflicts, "reason": task["merge"]["detail"] or "merge conflict"})
        else:
            merged.append({"id": task["id"], "status": merge_status, "applied_files": applied_files})
            cleanup_entry = cleanup_task_worktree_fn(worktree_text, branch_text, inventory)
            cleanup.append({"id": task["id"], **cleanup_entry})

    overall_merge_status = "nothing_to_merge"
    if conflicts:
        overall_merge_status = "conflicts"
    elif merged:
        if any(item.get("status") == "merged" for item in merged):
            overall_merge_status = "merged"
        else:
            overall_merge_status = "noop"
    elif skipped and all(item.get("reason") == "already_merged" for item in skipped):
        overall_merge_status = "already_merged"

    if plan_id:
        persist_execution_manifest_fn(
            state,
            plan_id=plan_id,
            status="merge_conflicts" if overall_merge_status == "conflicts" else "merged",
            merge={
                "status": overall_merge_status,
                "completed_at": now,
                "merged_agents": [item["id"] for item in merged],
                "conflict_agents": [item["id"] for item in conflicts],
                "cleanup": cleanup,
            },
        )

    save_state_fn(state)
    return {
        "status": overall_merge_status,
        "merged": merged,
        "skipped": skipped,
        "conflicts": conflicts,
        "cleanup": cleanup,
        "backup_method": backup_method,
    }


def cmd_merge(
    args: Any,
    *,
    merge_runtime_fn: Callable[..., dict],
    emit_json_fn: Callable[[dict], None],
) -> None:
    raw_agents = str(getattr(args, "agents", "") or "").strip().lower()
    agent_ids = None
    if raw_agents and raw_agents != "all":
        agent_ids = [item.strip().lower() for item in raw_agents.split(",") if item.strip()]
    payload = merge_runtime_fn(agent_ids)
    if getattr(args, "json", False):
        emit_json_fn(payload)
        return
    print(f"Merged {len(payload['merged'])} task(s); {len(payload['conflicts'])} conflict set(s); {len(payload['skipped'])} skipped.")
