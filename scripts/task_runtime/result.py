from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .state import coerce_int


def cmd_attach(
    args: Any,
    *,
    load_state_fn: Callable[[], dict],
    save_state_fn: Callable[[dict], None],
    ensure_task_runtime_fields_fn: Callable[[dict], bool],
    resolve_recorded_path_fn: Callable[[str], Path],
    display_runtime_path_fn: Callable[[Path], str],
    now_iso_fn: Callable[[], str],
    emit_json_fn: Callable[[dict], None],
    error_type: type[Exception] = RuntimeError,
) -> None:
    state = load_state_fn()
    agent_id = args.agent.lower()
    task = state.get("tasks", {}).get(agent_id)
    if not task:
        raise error_type(f"Agent {agent_id.upper()} not found.")

    ensure_task_runtime_fields_fn(task)
    resolved = resolve_recorded_path_fn(args.worktree_path)
    task["launch"]["worktree_path"] = display_runtime_path_fn(resolved)
    task["launch"]["branch"] = str(getattr(args, "branch", "") or "")
    task["launch"]["recorded_at"] = now_iso_fn()

    save_state_fn(state)
    payload = {
        "agent": agent_id,
        "status": task.get("status", ""),
        "launch": dict(task["launch"]),
    }
    if getattr(args, "json", False):
        emit_json_fn(payload)
        return
    print(f"Attached Agent {agent_id.upper()} to {payload['launch']['worktree_path']}")


def cmd_result(
    args: Any,
    *,
    load_state_fn: Callable[[], dict],
    save_state_fn: Callable[[dict], None],
    ensure_task_runtime_fields_fn: Callable[[dict], bool],
    empty_agent_result_fn: Callable[[], dict],
    empty_merge_record_fn: Callable[[], dict],
    normalize_string_list_fn: Callable[[object], list[str]],
    recompute_ready_fn: Callable[[dict], None],
    load_json_payload_fn: Callable[[Any], dict],
    now_iso_fn: Callable[[], str],
    emit_json_fn: Callable[[dict], None],
    error_type: type[Exception] = RuntimeError,
) -> None:
    state = load_state_fn()
    agent_id = args.agent.lower()
    task = state.get("tasks", {}).get(agent_id)
    if not task:
        raise error_type(f"Agent {agent_id.upper()} not found.")

    payload = load_json_payload_fn(args)
    payload_id = str(payload.get("id", "") or "").strip().lower()
    if payload_id != agent_id:
        raise error_type(f"Payload ID {payload.get('id')!r} does not match requested agent {agent_id.upper()}.")

    status = str(payload.get("status", "") or "").strip().lower()
    if status not in {"done", "failed"}:
        raise error_type("Agent result status must be 'done' or 'failed'.")

    ensure_task_runtime_fields_fn(task)
    now = now_iso_fn()
    files_modified = [item.replace("\\", "/") for item in normalize_string_list_fn(payload.get("files_modified", []))]
    issues = normalize_string_list_fn(payload.get("issues", []))
    summary = str(payload.get("summary", "") or "")
    worktree_path = str(payload.get("worktree_path", "") or task["launch"].get("worktree_path", "") or "")
    branch = str(payload.get("branch", "") or task["launch"].get("branch", "") or "")

    task["agent_result"] = empty_agent_result_fn()
    task["agent_result"].update(
        {
            "status": status,
            "files_modified": files_modified,
            "tests_passed": coerce_int(payload.get("tests_passed", 0) or 0),
            "tests_failed": coerce_int(payload.get("tests_failed", 0) or 0),
            "issues": issues,
            "input_tokens": coerce_int(payload.get("input_tokens", 0) or 0),
            "output_tokens": coerce_int(payload.get("output_tokens", 0) or 0),
            "summary": summary,
            "worktree_path": worktree_path,
            "branch": branch,
            "reported_at": now,
        }
    )
    if worktree_path:
        task["launch"]["worktree_path"] = worktree_path
    if branch:
        task["launch"]["branch"] = branch
    if not task["launch"].get("recorded_at"):
        task["launch"]["recorded_at"] = now

    task["completed_at"] = now
    task["merge"] = empty_merge_record_fn()
    if status == "done":
        task["status"] = "done"
        task["summary"] = summary
        task["error"] = ""
        recompute_ready_fn(state)
    else:
        task["status"] = "failed"
        task["summary"] = summary
        task["error"] = "; ".join(issues) or summary

    save_state_fn(state)

    next_ready = [
        {"id": item["id"], "name": item["name"]}
        for item in sorted(state.get("tasks", {}).values(), key=lambda item: item["id"])
        if item.get("status") == "ready" and item.get("id") != agent_id
    ]
    output = {
        "agent": agent_id,
        "status": task["status"],
        "summary": task.get("summary", ""),
        "error": task.get("error", ""),
        "next_ready": next_ready,
        "agent_result": dict(task["agent_result"]),
    }
    if getattr(args, "json", False):
        emit_json_fn(output)
        return
    print(f"Recorded result for Agent {agent_id.upper()} [{task['status']}]")
