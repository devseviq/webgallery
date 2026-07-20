from __future__ import annotations

import argparse
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


def _is_stale_running_task(started_iso: str, now_iso: str, *, max_hours: float = 2) -> bool:
    """Return True if a running task has exceeded the staleness threshold."""
    try:
        started = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
        now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        return (now - started).total_seconds() > max_hours * 3600
    except (ValueError, TypeError):
        return False


def recover_runtime(
    *,
    prune_orphans: bool = False,
    load_state_fn: Callable[[], dict],
    save_state_fn: Callable[[dict], None],
    now_iso_fn: Callable[[], str],
    ensure_task_runtime_fields_fn: Callable[[dict], bool],
    empty_agent_result_fn: Callable[[], dict],
    empty_launch_record_fn: Callable[[], dict],
    empty_merge_record_fn: Callable[[], dict],
    recompute_ready_fn: Callable[[dict], None],
    sync_execution_manifest_after_recover_fn: Callable[[dict, list[dict]], None],
    resolve_recorded_path_fn: Callable[[str], Path],
    display_runtime_path_fn: Callable[[Path], str],
    candidate_worktree_roots_fn: Callable[[dict], set[Path]],
    match_worktree_record_fn: Callable[[str, str, dict], dict | None],
    cleanup_task_worktree_fn: Callable[[str, str, dict], dict],
    git_worktree_inventory_fn: Callable[[], dict],
    safe_resolve_fn: Callable[[str], Path],
    error_type: type[Exception] = RuntimeError,
) -> dict:
    state = load_state_fn()
    recovered: list[dict] = []
    active: list[dict] = []
    inventory = git_worktree_inventory_fn()

    for task in sorted(state.get("tasks", {}).values(), key=lambda item: item["id"]):
        ensure_task_runtime_fields_fn(task)
        if task.get("status") != "running":
            continue

        result_status = str(task.get("agent_result", {}).get("status", "") or "").lower()
        if result_status == "done":
            task["status"] = "done"
            task["completed_at"] = task["agent_result"].get("reported_at", "") or now_iso_fn()
            task["summary"] = task["agent_result"].get("summary", "") or task.get("summary", "")
            task["error"] = ""
            task["merge"] = empty_merge_record_fn()
            recovered.append({"id": task["id"], "action": "done_from_recorded_result"})
            continue
        if result_status == "failed":
            task["status"] = "failed"
            task["completed_at"] = task["agent_result"].get("reported_at", "") or now_iso_fn()
            task["summary"] = task["agent_result"].get("summary", "") or task.get("summary", "")
            task["error"] = "; ".join(task["agent_result"].get("issues", [])) or task.get("error", "")
            task["merge"] = empty_merge_record_fn()
            recovered.append({"id": task["id"], "action": "failed_from_recorded_result"})
            continue

        recorded_path = str(task.get("launch", {}).get("worktree_path", "") or task.get("agent_result", {}).get("worktree_path", "") or "")
        recorded_branch = str(task.get("launch", {}).get("branch", "") or task.get("agent_result", {}).get("branch", "") or "")
        if not recorded_path:
            task["status"] = "pending"
            task["started_at"] = ""
            task["launch"] = empty_launch_record_fn()
            task["agent_result"] = empty_agent_result_fn()
            recovered.append({"id": task["id"], "action": "reset", "reason": "missing_worktree_record"})
            continue

        matched = match_worktree_record_fn(recorded_path, recorded_branch, inventory)
        if matched:
            resolved = Path(str(matched.get("path", "")))
            task["launch"]["worktree_path"] = display_runtime_path_fn(resolved)
            if matched.get("branch"):
                task["launch"]["branch"] = str(matched.get("branch", "") or "")
            active.append(
                {
                    "id": task["id"],
                    "worktree_path": display_runtime_path_fn(resolved),
                    "branch": task["launch"].get("branch", ""),
                    "source": "git-worktree",
                }
            )
            continue

        try:
            resolved = resolve_recorded_path_fn(recorded_path)
        except OSError:
            try:
                resolved = safe_resolve_fn(recorded_path)
            except error_type:
                task["status"] = "failed"
                task["started_at"] = ""
                task["launch"] = empty_launch_record_fn()
                task["agent_result"] = empty_agent_result_fn()
                recovered.append({"id": task["id"], "action": "reset", "reason": "path_escapes_root"})
                continue
        if not resolved.exists():
            task["status"] = "pending"
            task["started_at"] = ""
            task["launch"] = empty_launch_record_fn()
            task["agent_result"] = empty_agent_result_fn()
            recovered.append({"id": task["id"], "action": "reset", "reason": "missing_worktree_path"})
            continue

        started = task.get("started_at", "")
        if started and _is_stale_running_task(started, now_iso_fn()):
            task["status"] = "pending"
            task["started_at"] = ""
            task["launch"] = empty_launch_record_fn()
            task["agent_result"] = empty_agent_result_fn()
            recovered.append({"id": task["id"], "action": "reset", "reason": "stale_running_task"})
            continue

        active.append(
            {
                "id": task["id"],
                "worktree_path": display_runtime_path_fn(resolved),
                "branch": recorded_branch,
                "source": "filesystem",
            }
        )

    recompute_ready_fn(state)
    sync_execution_manifest_after_recover_fn(state, recovered)

    active_paths: set[str] = set()
    for task in state.get("tasks", {}).values():
        for path_text in (
            str(task.get("launch", {}).get("worktree_path", "") or ""),
            str(task.get("agent_result", {}).get("worktree_path", "") or ""),
        ):
            if not path_text:
                continue
            try:
                active_paths.add(str(resolve_recorded_path_fn(path_text)))
            except OSError:
                continue
    orphan_paths: list[str] = []
    orphan_details: list[dict] = []
    for root in sorted(candidate_worktree_roots_fn(state)):
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            resolved_path = child.resolve()
            if str(resolved_path) in active_paths:
                continue
            path_display = display_runtime_path_fn(resolved_path)
            orphan_paths.append(path_display)
            detail = {"worktree_path": path_display, "registered": False, "branch": ""}
            matched = match_worktree_record_fn(path_display, "", inventory)
            if matched:
                detail["registered"] = True
                detail["branch"] = str(matched.get("branch", "") or "")
            orphan_details.append(detail)
            if prune_orphans:
                if detail["registered"]:
                    cleanup_task_worktree_fn(path_display, str(detail["branch"] or ""), inventory)
                else:
                    try:
                        shutil.rmtree(child)
                    except OSError as exc:
                        detail["cleanup_error"] = str(exc)

    save_state_fn(state)
    return {
        "recovered": recovered,
        "active": active,
        "orphan_worktrees": orphan_paths,
        "orphan_details": orphan_details,
        "pruned_orphans": bool(prune_orphans and orphan_paths),
        "git_worktrees_available": bool(inventory.get("available")),
    }


def cmd_recover(
    args: Any,
    *,
    recover_runtime_fn: Callable[..., dict],
    emit_json_fn: Callable[[dict], None],
) -> None:
    payload = recover_runtime_fn(prune_orphans=bool(getattr(args, "prune_orphans", False)))
    if getattr(args, "json", False):
        emit_json_fn(payload)
        return
    print(f"Recovered {len(payload['recovered'])} task(s); {len(payload['active'])} active worktree(s) remain.")
    if payload["orphan_worktrees"]:
        print("Orphan worktrees:")
        for path in payload["orphan_worktrees"]:
            print(f"  - {path}")


def go_runtime(
    args: Any,
    *,
    resolve_plan_summary_for_runtime_fn: Callable[[str | None], dict],
    load_plan_from_summary_fn: Callable[[dict], dict],
    capture_json_command_fn: Callable[[Any], dict],
    cmd_plan_go_fn: Callable[[Any], None],
    plan_preflight_payload_fn: Callable[[], dict],
    recover_runtime_fn: Callable[..., dict],
    sync_state_fn: Callable[[], dict],
    load_state_fn: Callable[[], dict],
    save_state_fn: Callable[[dict], None],
    ensure_execution_manifest_fn: Callable[[dict], bool],
    persist_execution_manifest_fn: Callable[..., None],
    cmd_run_fn: Callable[[Any], None],
    merge_runtime_fn: Callable[..., dict],
    verify_runtime_fn: Callable[..., dict],
) -> dict:
    summary = resolve_plan_summary_for_runtime_fn(getattr(args, "plan_id", None))
    plan_payload: dict
    if summary.get("status") in {"draft", "approved"}:
        plan_payload = capture_json_command_fn(
            lambda: cmd_plan_go_fn(
                argparse.Namespace(
                    plan_id=summary["id"],
                    goal=getattr(args, "goal", ""),
                    exit_criterion=list(getattr(args, "exit_criterion", []) or []),
                    verification_step=list(getattr(args, "verification_step", []) or []),
                    documentation_update=list(getattr(args, "documentation_update", []) or []),
                    json=True,
                )
            )
        )
    else:
        plan = load_plan_from_summary_fn(summary)
        plan_payload = {
            "plan_id": plan["id"],
            "status": plan.get("status", ""),
            "plan_file": plan.get("plan_file", ""),
            "plan_doc": plan.get("plan_doc", ""),
            "updated_fields": [],
            "warnings": [],
            "preflight": plan_preflight_payload_fn(),
            "ready_agents": [],
        }

    recovery = recover_runtime_fn(prune_orphans=False)
    state = sync_state_fn()
    plan_id = str(plan_payload.get("plan_id", "") or "")
    ensure_execution_manifest_fn(state)
    manifest = state.get("execution_manifest", {})
    ready = [task for task in state.get("tasks", {}).values() if task.get("status") == "ready"]
    running = [task for task in state.get("tasks", {}).values() if task.get("status") == "running"]
    failed = [task for task in state.get("tasks", {}).values() if task.get("status") == "failed"]

    if ready:
        launch = capture_json_command_fn(lambda: cmd_run_fn(argparse.Namespace(agents="ready", json=True)))
        state = load_state_fn()
        running = [task for task in state.get("tasks", {}).values() if task.get("status") == "running"]
        persist_execution_manifest_fn(
            state,
            plan_id=plan_id,
            status="awaiting_results",
            reset_follow_on=True,
            launch={
                "status": "awaiting_results",
                "launched": launch.get("launched", []),
                "running": [task["id"] for task in running],
                "failed": [task["id"] for task in failed],
            },
        )
        save_state_fn(state)
        return {
            "plan": plan_payload,
            "recovery": recovery,
            "status": "awaiting_results",
            "resume": {"mode": "fresh", "reason": ""},
            "launch": launch,
            "running_agents": [{"id": task["id"], "name": task["name"]} for task in sorted(running, key=lambda item: item["id"])],
            "failed_agents": [{"id": task["id"], "name": task["name"]} for task in sorted(failed, key=lambda item: item["id"])],
        }

    if running:
        persist_execution_manifest_fn(
            state,
            plan_id=plan_id,
            status="awaiting_results",
            launch={
                "status": "awaiting_results",
                "launched": [],
                "running": [task["id"] for task in running],
                "failed": [task["id"] for task in failed],
            },
        )
        save_state_fn(state)
        return {
            "plan": plan_payload,
            "recovery": recovery,
            "status": "awaiting_results",
            "resume": {"mode": "fresh", "reason": ""},
            "launch": {"launched": [], "agents": [], "skipped": []},
            "running_agents": [{"id": task["id"], "name": task["name"]} for task in sorted(running, key=lambda item: item["id"])],
            "failed_agents": [{"id": task["id"], "name": task["name"]} for task in sorted(failed, key=lambda item: item["id"])],
        }

    blocked = [
        {"id": task["id"], "name": task["name"], "status": task["status"]}
        for task in sorted(state.get("tasks", {}).values(), key=lambda item: item["id"])
        if task.get("status") in {"pending", "blocked", "failed"}
    ]
    if blocked:
        persist_execution_manifest_fn(
            state,
            plan_id=plan_id,
            status="blocked",
            launch={
                "status": "blocked",
                "launched": [],
                "running": [],
                "failed": [task["id"] for task in state.get("tasks", {}).values() if task.get("status") == "failed"],
            },
        )
        save_state_fn(state)
        return {
            "plan": plan_payload,
            "recovery": recovery,
            "status": "blocked",
            "resume": {"mode": "fresh", "reason": ""},
            "launch": {"launched": [], "agents": [], "skipped": []},
            "blocked_agents": blocked,
        }

    resume = {"mode": "fresh", "reason": ""}
    if (
        manifest.get("plan_id") == plan_id
        and str(manifest.get("merge", {}).get("status", "") or "") in {"merged", "noop", "already_merged"}
        and str(manifest.get("verify", {}).get("status", "") or "") == "failed"
    ):
        resume = {"mode": "verify_only", "reason": "previous verify failed after merge"}
        merge = {
            "status": "reused_previous_merge",
            "merged": [],
            "skipped": [],
            "conflicts": [],
            "cleanup": [],
        }
    else:
        merge = merge_runtime_fn(plan_id=plan_id)

    verify = verify_runtime_fn(plan_id)
    final_status = "verified" if verify["passed"] else "verification_failed"
    if merge.get("conflicts"):
        final_status = "merge_conflicts"
    return {
        "plan": plan_payload,
        "recovery": recovery,
        "status": final_status,
        "resume": resume,
        "merge": merge,
        "verify": verify,
    }


def cmd_go(
    args: Any,
    *,
    go_runtime_fn: Callable[[Any], dict],
    emit_json_fn: Callable[[dict], None],
    sleep_fn: Callable[[float], None] = time.sleep,
) -> None:
    if getattr(args, "poll", 0) > 0:
        iteration = 0
        while True:
            iteration += 1
            payload = go_runtime_fn(args)
            status = payload.get("status", "")
            terminal_statuses = {"verified", "verification_failed", "merge_conflicts", "blocked"}
            if status in terminal_statuses:
                # Final output
                if getattr(args, "json", False):
                    emit_json_fn(payload)
                else:
                    print(f"[poll] Iteration {iteration}: terminal status '{status}'. Done.")
                break
            # Not terminal — suppress text output in JSON mode
            if not getattr(args, "json", False):
                print(f"[poll] Iteration {iteration}: status='{status}', waiting {args.poll}s...")
            sleep_fn(args.poll)
    else:
        payload = go_runtime_fn(args)
        if getattr(args, "json", False):
            emit_json_fn(payload)
            return
        print(f"Go status: {payload['status']}")
