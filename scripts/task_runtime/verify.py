from __future__ import annotations

from typing import Any, Callable


def verify_runtime(
    plan_id: str | None = None,
    *,
    profile: str = "default",
    recover_runtime_fn: Callable[..., dict],
    sync_state_fn: Callable[[], dict],
    resolve_plan_summary_for_runtime_fn: Callable[[str | None], dict],
    load_plan_from_summary_fn: Callable[[dict], dict],
    resolve_plan_for_verify_fn: Callable[[dict], dict | None],
    explain_verify_resolution_failure_fn: Callable[[dict], str],
    normalize_verify_profile_fn: Callable[[str | None], str],
    plan_owned_files_fn: Callable[[dict, dict | None], list[str]],
    commands_cfg_fn: Callable[[], dict],
    configured_runtime_commands_fn: Callable[..., list[tuple[str, str]]],
    placeholder_command_reason_fn: Callable[[str], str],
    run_runtime_command_fn: Callable[[str, str], dict],
    plan_exit_criteria_fn: Callable[[dict], list[str]],
    persist_execution_manifest_fn: Callable[..., None],
    save_state_fn: Callable[[dict], None],
    now_iso_fn: Callable[[], str],
    error_type: type[Exception] = RuntimeError,
) -> dict:
    recovery = recover_runtime_fn(prune_orphans=False)
    state = sync_state_fn()
    plan: dict | None
    if plan_id:
        summary = resolve_plan_summary_for_runtime_fn(plan_id)
        plan = load_plan_from_summary_fn(summary)
    else:
        plan = resolve_plan_for_verify_fn(state)
        if not plan:
            raise error_type("No valid executable plan available for verify. " + explain_verify_resolution_failure_fn(state))
    assert plan is not None

    normalized_profile = normalize_verify_profile_fn(profile)
    plan_files = plan_owned_files_fn(plan, state)
    warnings: list[str] = []
    commands_run: list[dict] = []

    if normalized_profile == "fast" and not str(commands_cfg_fn().get("test_fast", "") or "").strip():
        test_command = str(commands_cfg_fn().get("test", "") or "").strip()
        if test_command:
            warnings.append("Verify profile 'fast' requested, but [commands].test_fast is not configured; using [commands].test.")
    if normalized_profile == "full" and not str(commands_cfg_fn().get("test_full", "") or "").strip():
        test_command = str(commands_cfg_fn().get("test", "") or "").strip()
        if test_command:
            warnings.append("Verify profile 'full' requested, but [commands].test_full is not configured; using [commands].test.")

    compile_command = str(commands_cfg_fn().get("compile", "") or "").strip()
    if "{files}" in compile_command and not plan_files:
        warnings.append("Configured [commands].compile uses {files}, but the active plan does not declare owned files.")

    for label, command in configured_runtime_commands_fn(profile=normalized_profile, files=plan_files):
        if not command:
            continue
        reason = placeholder_command_reason_fn(command)
        if reason:
            warnings.append(f"Skipped {label} command because it {reason}.")
            continue
        commands_run.append(run_runtime_command_fn(label, command))

    failed_commands = [entry for entry in commands_run if not entry["passed"]]
    failed_tasks = [
        {"id": task["id"], "name": task["name"]}
        for task in sorted(state.get("tasks", {}).values(), key=lambda item: item["id"])
        if task.get("status") == "failed"
    ]
    incomplete_tasks = [
        {"id": task["id"], "name": task["name"], "status": task["status"]}
        for task in sorted(state.get("tasks", {}).values(), key=lambda item: item["id"])
        if task.get("status") not in {"done", "failed"}
    ]
    merge_blockers = [
        {"id": task["id"], "status": task.get("merge", {}).get("status", ""), "conflicts": task.get("merge", {}).get("conflicts", [])}
        for task in sorted(state.get("tasks", {}).values(), key=lambda item: item["id"])
        if task.get("status") == "done" and task.get("merge", {}).get("status", "") not in {"merged", "noop"}
    ]

    passed = not failed_commands and not failed_tasks and not incomplete_tasks and not merge_blockers
    verify_status = "passed" if passed else "failed"
    criteria = [{"criterion": item, "passed": passed} for item in plan_exit_criteria_fn(plan)]
    persist_execution_manifest_fn(
        state,
        plan_id=plan["id"],
        status="verified" if passed else "verification_failed",
        verify={
            "status": verify_status,
            "completed_at": now_iso_fn(),
            "passed": passed,
            "failed_commands": [entry["label"] for entry in failed_commands],
        },
    )
    save_state_fn(state)
    return {
        "plan_id": plan["id"],
        "profile": normalized_profile,
        "status": verify_status,
        "passed": passed,
        "criteria": criteria,
        "commands": commands_run,
        "warnings": warnings,
        "failed_tasks": failed_tasks,
        "incomplete_tasks": incomplete_tasks,
        "merge_blockers": merge_blockers,
        "recovery": recovery,
    }


def cmd_verify(
    args: Any,
    *,
    verify_runtime_fn: Callable[..., dict],
    emit_json_fn: Callable[[dict], None],
) -> None:
    payload = verify_runtime_fn(getattr(args, "plan_id", None), profile=getattr(args, "profile", "default"))
    if getattr(args, "json", False):
        emit_json_fn(payload)
    else:
        print(f"Verify {payload['plan_id']}: {'passed' if payload['passed'] else 'failed'}")
        for entry in payload["commands"]:
            print(f"  {entry['label']}: {'passed' if entry['passed'] else 'failed'}")
        for warning in payload["warnings"]:
            print(f"  warning: {warning}")
    if not payload["passed"]:
        raise SystemExit(1)
